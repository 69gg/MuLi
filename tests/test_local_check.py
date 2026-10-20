"""The local check launcher must fail closed, without creating real services in CI."""

from __future__ import annotations

import importlib.util
import subprocess
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
        "memory.max": str(1024**3),
        "memory.swap.max": "0",
        "pids.max": "128",
        "cpu.max": "100000 100000",
        "memory.oom.group": "1",
    }.items():
        (tmp_path / name).write_text(value)
    return tmp_path


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("memory.max", "max"),
        ("memory.max", str(2 * 1024**3)),
        ("memory.swap.max", "1"),
        ("pids.max", "max"),
        ("pids.max", "129"),
        ("cpu.max", "max 100000"),
        ("cpu.max", "100001 100000"),
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


def test_failed_guard_never_launches_command(checker: ModuleType, monkeypatch: Any) -> None:
    def refuse(*args: Any) -> None:
        raise RuntimeError("no limits")

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unguarded command was launched")

    monkeypatch.setattr(checker, "verify_inside", refuse)
    monkeypatch.setattr(checker.subprocess, "run", unexpected)
    for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGPIPE"):
        monkeypatch.setattr(checker.signal, name, 1, raising=False)
    monkeypatch.setattr(checker.signal, "signal", lambda *args: None)
    with pytest.raises(RuntimeError, match="no limits"):
        checker.run_inside("unit.service", 1, ["target"])


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
        assert "--setenv=NODE_OPTIONS=--max-old-space-size=256" in command
        raise KeyboardInterrupt

    def stop(unit: str) -> bool:
        stopped.append(unit)
        return cleaned

    monkeypatch.setattr(checker.sys, "platform", "linux")
    monkeypatch.setattr(checker.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(checker.Path, "is_file", lambda _: True)
    monkeypatch.setattr(checker, "manager", record)
    monkeypatch.setattr(checker, "stop_service", stop)
    monkeypatch.setattr(checker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(checker.subprocess, "run", cancelled)
    assert checker.run(["/tools/python", "-V"], 1) == (124 if cleaned else 125)
    assert len(stopped) == 1 and stopped[0].endswith(".service")
    assert calls[0][2] == checker.SLICE
    assert all("stop" not in call for call in calls)


def test_no_unbounded_fallback(checker: ModuleType, monkeypatch: Any) -> None:
    monkeypatch.setattr(checker.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="no unbounded fallback"):
        checker.run(["target"], 1)
