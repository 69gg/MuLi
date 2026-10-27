"""The local check launcher must fail closed, without creating real services in CI."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def checker() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "check_local.py"
    spec = importlib.util.spec_from_file_location("local_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def group(tmp_path: Path) -> Path:
    for name, value in {
        "memory.max": str(768 * 1024**2),
        "memory.swap.max": "0",
        "pids.max": "96",
        "cpu.max": "50000 100000",
        "memory.oom.group": "1",
    }.items():
        (tmp_path / name).write_text(value)
    return tmp_path


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory.max", "max"),
        ("memory.max", str(2 * 1024**3)),
        ("memory.max", str(1024**3)),
        ("memory.swap.max", "1"),
        ("pids.max", "max"),
        ("pids.max", "97"),
        ("cpu.max", "max 100000"),
        ("cpu.max", "100001 100000"),
        ("cpu.max", "50001 100000"),
        ("memory.oom.group", "0"),
    ],
)
def test_missing_or_excessive_limits_refuse_execution(
    checker: ModuleType, group: Path, name: str, value: str
) -> None:
    checker.verify_group(group, checker.MEMORY_BYTES, service=True)
    (group / name).write_text(value)
    with pytest.raises(RuntimeError):
        checker.verify_group(group, checker.MEMORY_BYTES, service=True)
    (group / name).unlink()
    with pytest.raises(OSError):
        checker.verify_group(group, checker.MEMORY_BYTES, service=True)


def test_frontend_uses_serial_finite_tests(checker: ModuleType, monkeypatch: Any) -> None:
    monkeypatch.setattr(checker.shutil, "which", lambda name: f"/tools/{name}")
    command = checker.command_for("frontend", ["tests/frontend/api.test.mjs"])
    assert command[0] == str(Path("/tools/node").absolute())
    assert "--test-concurrency=1" in command
    assert f"--test-timeout={checker.NODE_TEST_TIMEOUT_MS}" in command
    with pytest.raises(ValueError):
        checker.command_for("frontend", ["--test-concurrency=20"])


@pytest.mark.parametrize("backend", [False, True])
def test_failed_guard_never_launches_command(
    checker: ModuleType, monkeypatch: Any, backend: bool
) -> None:
    def refuse(*args: Any) -> None:
        raise RuntimeError("no limits")

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unguarded command was launched")

    monkeypatch.setattr(checker, "verify_inside", refuse)
    monkeypatch.setattr(checker, "backend_paths", unexpected)
    monkeypatch.setattr(checker.subprocess, "run", unexpected)
    for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGPIPE"):
        monkeypatch.setattr(checker.signal, name, 1, raising=False)
    monkeypatch.setattr(checker.signal, "signal", lambda *args: None)
    with pytest.raises(RuntimeError, match="no limits"):
        checker.run_inside("unit.service", 1, ["target", "test_one.py"], backend=backend)


@pytest.mark.parametrize(("code", "expected"), [(0, 0), (7, 7), (-15, 143), (-9, 137)])
def test_command_failure_is_not_systemd_clean_signal_exit(
    checker: ModuleType, monkeypatch: Any, code: int, expected: int
) -> None:
    monkeypatch.setattr(checker, "verify_inside", lambda *args: None)
    for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGPIPE"):
        monkeypatch.setattr(checker.signal, name, 1, raising=False)
    monkeypatch.setattr(checker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        checker.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], code)
    )
    assert checker.run_inside("unit.service", 1, ["target"]) == expected


@pytest.mark.parametrize("cleaned", [True, False])
def test_cancel_cleans_own_service_and_keeps_shared_budget(
    checker: ModuleType, monkeypatch: Any, cleaned: bool
) -> None:
    calls: list[tuple[str, ...]] = []
    stopped: list[str] = []

    def record(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout="")

    def cancelled(command: list[str], **kwargs: Any) -> None:
        assert "--slice=muli-checks.slice" in command
        assert "--setenv=UV_OFFLINE=1" in command
        assert "--setenv=NODE_OPTIONS=--max-old-space-size=192" in command
        assert "--property=MemoryMax=805306368" in command
        assert "--property=CPUQuota=50%" in command
        assert "--property=TasksMax=96" in command
        assert "--setenv=UV_CACHE_DIR=/existing-uv-cache" in command
        assert "--setenv=UV_PYTHON_DOWNLOADS=never" in command
        assert all("PRIVATE_TEST_TOKEN" not in argument for argument in command)
        raise KeyboardInterrupt

    def stop(unit: str) -> bool:
        stopped.append(unit)
        return cleaned

    monkeypatch.setattr(checker.sys, "platform", "linux")
    monkeypatch.setenv("UV_CACHE_DIR", "/existing-uv-cache")
    monkeypatch.setenv("PRIVATE_TEST_TOKEN", "synthetic-not-forwarded")
    monkeypatch.setattr(checker.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(checker.Path, "is_file", lambda _: True)
    monkeypatch.setattr(checker, "manager", record)
    monkeypatch.setattr(checker, "stop_service", stop)
    monkeypatch.setattr(checker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(checker.subprocess, "run", cancelled)
    assert checker.run(["/tools/python", "-V"], 1) == (124 if cleaned else 125)
    assert len(stopped) == 1 and stopped[0].endswith(".service")
    assert calls[0][2] == checker.SLICE
    assert "MemoryMax=1073741824" in calls[0]
    assert "CPUQuota=50%" in calls[0]
    assert "TasksMax=96" in calls[0]
    assert all("stop" not in call for call in calls)


def test_no_unbounded_fallback(checker: ModuleType, monkeypatch: Any) -> None:
    monkeypatch.setattr(checker.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="no unbounded fallback"):
        checker.run(["target"], 1)


def test_backend_discovers_each_test_file_and_requires_existing_python(
    checker: ModuleType, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests/nested").mkdir(parents=True)
    for name in ("test_one.py", "nested/test_two.py", "nested/three_test.py", "helper.py"):
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    expected = ["tests/nested/test_two.py", "tests/nested/three_test.py", "tests/test_one.py"]
    command = checker.command_for("backend", [sys.executable])
    assert command[0] == str(Path(sys.executable).absolute())
    assert command[1:] == []
    assert [Path(path).as_posix() for path in checker.backend_paths([])] == expected
    assert checker.backend_paths([expected[2], expected[2]]) == [expected[2]]
    for args in ([], [str(tmp_path / "missing-python")]):
        with pytest.raises(ValueError):
            checker.command_for("backend", args)
    for paths in (["--maxfail=1"], ["tests"], ["missing.py"]):
        with pytest.raises(ValueError):
            checker.backend_paths(paths)


def test_backend_empty_discovery_refuses_execution(
    checker: ModuleType, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="No backend tests"):
        checker.backend_paths([])


def test_backend_command_preparation_does_not_scan_tests(
    checker: ModuleType, monkeypatch: Any
) -> None:
    def unexpected(*args: Any) -> None:
        pytest.fail("backend discovered tests before entering the guard")

    monkeypatch.setattr(checker.Path, "rglob", unexpected)
    monkeypatch.setattr(checker, "backend_paths", unexpected)
    assert checker.command_for("backend", [sys.executable]) == [
        str(Path(sys.executable).absolute())
    ]


@pytest.mark.parametrize("fail_at", [None, 2])
def test_backend_uses_fresh_serial_processes_and_stops_after_failure(
    checker: ModuleType, monkeypatch: Any, tmp_path: Path, fail_at: int | None
) -> None:
    # A tiny local pytest stand-in records the actual process boundary; it does
    # not invoke pytest recursively or start services, models or other tools.
    package = tmp_path / "pytest"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "path: Path = Path('runs.jsonl')\n"
        "number: int = len(path.read_text(encoding='utf-8').splitlines()) + 1 "
        "if path.exists() else 1\n"
        "with path.open('a', encoding='utf-8') as output:\n"
        "    output.write(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:]}) + '\\n')\n"
        f"raise SystemExit(7 if number == {fail_at!r} else 0)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    paths = [f"test_{index}.py" for index in range(checker.BACKEND_BATCH_FILES * 2 + 1)]
    for path in paths:
        Path(path).write_text("", encoding="utf-8")
    assert checker.run_backend([sys.executable, *paths]) == (7 if fail_at else 0)
    rows = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == (fail_at or 3)
    assert len({row["pid"] for row in rows}) == len(rows)
    assert all(row["args"][:2] == ["-q", "--"] for row in rows)
    assert all(1 <= len(row["args"][2:]) <= checker.BACKEND_BATCH_FILES for row in rows)
    assert [path for row in rows for path in row["args"][2:]] == paths[
        : checker.BACKEND_BATCH_FILES * (fail_at or 3)
    ]


@pytest.mark.parametrize(("code", "expected"), [(-9, 137), (-15, 143), (7, 7)])
def test_backend_signal_failure_keeps_nonzero_status(
    checker: ModuleType, monkeypatch: Any, code: int, expected: int
) -> None:
    monkeypatch.setattr(checker, "verify_inside", lambda *args: None)
    for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGPIPE"):
        monkeypatch.setattr(checker.signal, name, 1, raising=False)
    monkeypatch.setattr(checker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(checker, "run_backend", lambda command: code)
    assert (
        checker.run_inside("unit.service", 1, ["python", "test_one.py"], backend=True) == expected
    )
