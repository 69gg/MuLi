"""Entry-point red tests: an ordinary session is not a resource envelope."""

from __future__ import annotations

import asyncio
import io
import os
import runpy
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from helpers import managed_native_worker as worker
from test_managed_native_ci import ci
from test_managed_native_ci import forbid_real_effects as forbid_real_effects

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only CI envelope")


@pytest.fixture(autouse=True)
def ordinary_unlimited_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never borrow the test runner's real cgroup or effective kernel limits."""
    original_open = Path.open

    def open_path(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == Path("/proc/self/cgroup"):
            data = "0::/user.slice/user-1001.slice/session-9.scope\n"
        elif path.is_relative_to(Path("/sys/fs/cgroup")):
            values = {
                "memory.max": "max\n",
                "memory.swap.max": "max\n",
                "pids.max": "max\n",
                "cpu.max": "max 100000\n",
                "cgroup.controllers": "cpu memory pids\n",
            }
            if path.name not in values:
                raise FileNotFoundError(path.name)
            data = values[path.name]
        else:
            return original_open(path, mode, *args, **kwargs)
        assert mode in {"r", "rt", "rb"}, "the entry gate must only read kernel facts"
        return io.BytesIO(data.encode("ascii")) if "b" in mode else io.StringIO(data)

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(worker.os, "getuid", lambda: 1001)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1001)
    monkeypatch.delenv("MULI_NATIVE_ENVELOPE_UNIT", raising=False)


@pytest.mark.parametrize("mode", ["run", "cleanup"])
def test_coordinator_rejects_no_envelope_before_native_ci(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ci.ROOT_NAME
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["ci", mode, str(root)])
    calls: list[str] = []

    def construct(path: Path) -> SimpleNamespace:
        assert path == root
        calls.append("NativeCI")
        return SimpleNamespace(
            execute=lambda: calls.append("execute"), cleanup=lambda: calls.append("cleanup")
        )

    monkeypatch.setattr(ci, "NativeCI", construct)
    with pytest.raises(ci.FixtureError, match="^native_resource_envelope$"):
        ci.main()
    assert calls == [] and not root.exists()


@pytest.mark.parametrize("mode", ["describe", "manager-ready"])
def test_worker_rejects_no_envelope_before_dispatch(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def describe() -> dict[str, object]:
        calls.append("describe")
        return {"code": "synthetic"}

    async def state(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("manager-ready")
        return {"code": "ready"}

    args = ["worker", mode]
    if mode == "manager-ready":
        args.extend(["muli-ci-" + "a" * 12, "23145", "/owned", "105", "1024", "100", ""])
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(worker, "describe", describe)
    monkeypatch.setattr(worker, "manager_state", state)
    with pytest.raises(worker.NativeError, match="^native_resource_envelope$"):
        worker.main()
    assert calls == [] and capsys.readouterr().out == ""


@dataclass
class Envelope:
    unit: str = "muli-native-envelope-run-123-2.service"
    uid: int = 1001
    gid: int = 1002
    root: bool = False
    membership_reads: int = 0
    group: str | None = None
    group_after: str | None = None
    uid_changes: bool = False
    run_population: str | None = None
    run_group_present: bool = False
    run_group_changed: bool = False
    run_group_reads: int = 0
    limits: dict[str, str] = field(
        default_factory=lambda: {
            "memory.max": str(worker.MEMORY),
            "memory.swap.max": "0",
            "pids.max": str(worker.TASKS),
            "cpu.max": "100000 100000",
            "cgroup.controllers": "cpu memory pids",
            "memory.oom.group": "1",
        }
    )


@pytest.fixture
def envelope(ordinary_unlimited_session: None, monkeypatch: pytest.MonkeyPatch) -> Envelope:
    value = Envelope()
    original_open = Path.open
    original_stat = os.stat

    def stat_path(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if isinstance(path, (str, Path)) and Path(path) == (
            Path("/sys/fs/cgroup/system.slice") / value.unit.replace("-cleanup-", "-run-")
        ):
            if value.run_population is None and not value.run_group_present:
                raise FileNotFoundError(str(path))
            value.run_group_reads += 1
            inode = 21 + int(value.run_group_changed and value.run_group_reads > 1)
            return os.stat_result((stat.S_IFDIR | 0o755, inode, 4, 2, 0, 0, 0, 0, 0, 0))
        return original_stat(path, *args, **kwargs)

    def open_path(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == Path("/proc/self/cgroup"):
            group = value.group or f"/system.slice/{value.unit}"
            if value.membership_reads and value.group_after is not None:
                group = value.group_after
            value.membership_reads += 1
            data = f"0::{group}\n"
        elif path == (
            Path("/sys/fs/cgroup/system.slice")
            / value.unit.replace("-cleanup-", "-run-")
            / "cgroup.events"
        ):
            if value.run_population is None:
                raise FileNotFoundError(str(path))
            data = value.run_population
        elif path.is_relative_to(Path("/sys/fs/cgroup")):
            assert path.parent == Path("/sys/fs/cgroup/system.slice") / value.unit
            if path.name not in value.limits:
                raise FileNotFoundError(path.name)
            data = value.limits[path.name] + "\n"
        else:
            return original_open(path, mode, *args, **kwargs)
        assert mode in {"r", "rt", "rb"}
        return io.BytesIO(data.encode("latin1")) if "b" in mode else io.StringIO(data)

    def uid() -> tuple[int, int, int]:
        current = 0 if value.root else value.uid
        if value.uid_changes and value.membership_reads:
            current += 1
        return (current,) * 3

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(os, "stat", stat_path)
    monkeypatch.setattr(worker.os, "getresuid", uid)
    monkeypatch.setattr(worker.os, "getresgid", lambda: ((0 if value.root else value.gid),) * 3)
    for name, item in zip(
        worker.RESOURCE_ENVIRONMENT, (value.unit, str(value.uid), str(value.gid)), strict=True
    ):
        monkeypatch.setenv(name, item)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    return value


@pytest.mark.parametrize("mode", ["run", "cleanup"])
@pytest.mark.parametrize("failed", [False, True], ids=["success", "original-failure"])
def test_guarded_coordinator_preserves_original_exit(
    mode: str,
    failed: bool,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    envelope.unit = envelope.unit.replace("-run-", f"-{mode}-")
    monkeypatch.setenv(worker.RESOURCE_ENVIRONMENT[0], envelope.unit)
    root, lock = lifecycle
    monkeypatch.setattr(sys, "argv", ["ci", mode, str(root)])
    calls: list[str] = []
    code = "native_deadline_exceeded" if mode == "run" else "native_service_cleanup_failed"

    def effect(**kwargs: object) -> None:
        assert_locked(lock)
        calls.append(mode)
        if failed:
            raise ci.FixtureError(code)

    def construct(path: Path) -> SimpleNamespace:
        assert path == root and envelope.membership_reads == 2
        return SimpleNamespace(execute=effect, cleanup=effect)

    monkeypatch.setattr(ci, "NativeCI", construct)
    assert ci.main() == int(failed)
    assert calls == [mode]
    assert capsys.readouterr().err == (code + "\n" if failed else "")


@pytest.mark.parametrize(
    "change",
    [
        "memory",
        "swap",
        "tasks",
        "cpu",
        "controllers",
        "oom",
        "missing",
        "oversize",
        "label",
        "uid",
        "group",
        "group-after",
        "uid-after",
    ],
)
def test_environment_claim_cannot_replace_actual_envelope(
    change: str, envelope: Envelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    changes = {
        "memory": ("memory.max", "max"),
        "swap": ("memory.swap.max", "1"),
        "tasks": ("pids.max", str(worker.TASKS + 1)),
        "cpu": ("cpu.max", "100001 100000"),
        "controllers": ("cgroup.controllers", "cpu memory"),
        "oom": ("memory.oom.group", "0"),
        "oversize": ("memory.max", "1" * 513),
    }
    if change in changes:
        key, value = changes[change]
        envelope.limits[key] = value
    elif change == "missing":
        del envelope.limits["memory.max"]
    elif change == "label":
        monkeypatch.delenv(worker.RESOURCE_ENVIRONMENT[2])
    elif change == "uid":
        monkeypatch.setattr(worker.os, "getresuid", lambda: (0, 0, 0))
    elif change == "group":
        envelope.group = "/system.slice/foreign.service"
    elif change == "group-after":
        envelope.group_after = "/system.slice/foreign.service"
    else:
        envelope.uid_changes = True
    with pytest.raises(worker.NativeError, match="^native_resource_envelope$"):
        worker.require_resource_envelope("run")


@pytest.mark.parametrize("root", [False, True], ids=["runner", "sudo-root"])
def test_same_service_accepts_actual_runner_or_root_identity(
    root: bool, envelope: Envelope
) -> None:
    envelope.root = root
    worker.require_resource_envelope(root=root)
    assert envelope.membership_reads == 2


def test_root_helper_does_not_accept_runner_uid_even_with_correct_group(envelope: Envelope) -> None:
    with pytest.raises(worker.NativeError, match="^native_resource_envelope$"):
        worker.require_resource_envelope(root=True)


@pytest.mark.parametrize(
    "mode", ["describe", "manager-ready", "project-manager-instance", "bootstrap-files"]
)
def test_guarded_worker_dispatch_uses_the_actual_process_role(
    mode: str,
    envelope: Envelope,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    envelope.root = mode in worker.ROOT_RESOURCE_MODES
    calls: list[str] = []

    def effect(*args: object, **kwargs: object) -> dict[str, object]:
        assert envelope.membership_reads == 2
        calls.append(mode)
        return {"code": "synthetic"}

    async def state(*args: object, **kwargs: object) -> dict[str, object]:
        return effect()

    args = ["worker", mode]
    if mode == "manager-ready":
        args.extend(["muli-ci-" + "a" * 12, "23145", "/owned", "105", "1024", "100", ""])
    elif mode == "project-manager-instance":
        args.extend(["42", "23145", "100", "/owned", "105", "512"])
    elif mode == "bootstrap-files":
        args.extend(
            [
                "check",
                "muli-ci-" + "a" * 12,
                "23145",
                "23145",
                '{"directories":[[1,2],[1,3]],"file":null}',
                "105",
                "1024",
            ]
        )
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(worker, "describe", effect)
    monkeypatch.setattr(worker, "manager_state", state)
    monkeypatch.setattr(worker, "read_manager_instance", effect)
    monkeypatch.setattr(worker, "bootstrap_files", effect)
    assert worker.main() == 0
    output = capsys.readouterr()
    assert output.out == '{"code":"synthetic"}\n' and output.err == ""
    assert calls == [mode]


def test_coordinator_cannot_reuse_the_other_mode_service(envelope: Envelope) -> None:
    with pytest.raises(ci.FixtureError, match="^native_resource_envelope$"):
        ci.require_resource_envelope("cleanup")
    assert envelope.membership_reads == 0


@pytest.mark.parametrize("mode", ["drive", "cleanup"])
def test_user_harness_keeps_its_separate_resource_gate(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run(selected: str, root: Path) -> int:
        assert (selected, root) == (mode, tmp_path)
        return 7

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a user harness is not in the coordinator service")

    monkeypatch.setattr(worker, "require_resource_envelope", forbidden)
    monkeypatch.setattr(worker, "run", run)
    monkeypatch.setattr(sys, "argv", ["worker", mode, str(tmp_path)])
    assert worker.main() == 7


def test_fixed_worker_loader_reuses_existing_definitions() -> None:
    assert ci.resource_worker() is worker


def test_fixed_worker_loader_rejects_a_foreign_cached_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("_muli_native_resource_worker")
    module.__file__ = str(tmp_path / "foreign.py")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(ci.FixtureError, match="^native_resource_envelope$"):
        ci.resource_worker()


def test_copied_isolated_worker_has_its_own_gate_without_loading_peer(
    envelope: Envelope, tmp_path: Path
) -> None:
    copied = tmp_path / "managed_native_worker.py"
    copied.write_bytes(Path(worker.__file__).read_bytes())
    paths = tuple(sys.path)
    peer = sys.modules.get("_muli_managed_native_peer")
    definitions = runpy.run_path(str(copied), run_name="_native_resource_fixture")
    definitions["require_resource_envelope"]()
    assert envelope.membership_reads == 2
    assert tuple(sys.path) == paths and sys.modules.get("_muli_managed_native_peer") is peer


def test_actual_coordinator_sudo_preserves_only_three_resource_labels(
    envelope: Envelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    class Captured(Exception):
        pass

    def popen(argv: list[str], **kwargs: object) -> None:
        calls.append(argv)
        raise Captured

    original = [
        "sudo",
        "--non-interactive",
        "--",
        sys.executable,
        "-I",
        str(Path(worker.__file__).resolve()),
        "project-manager-instance",
    ]
    monkeypatch.setattr(ci.subprocess, "Popen", popen)
    with pytest.raises(Captured):
        ci.command(original)
    assert calls == [
        [*original[:2], "--preserve-env=" + ",".join(worker.RESOURCE_ENVIRONMENT), *original[2:]]
    ]
    assert worker.resource_sudo_arguments(["sudo", "--non-interactive", "cat", "/owned"]) == [
        "sudo",
        "--non-interactive",
        "cat",
        "/owned",
    ]


def test_setup_io_sudo_preserves_labels_without_changing_reply_accounting(
    envelope: Envelope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli import process

    calls: list[list[str]] = []

    async def run(argv: list[str], **kwargs: object) -> dict[str, object]:
        calls.append(argv)
        return {"exit_code": 0, "output": "ok", "stderr": ""}

    monkeypatch.setattr(process, "run_process", run)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    io = worker._SetupIO(105.0, 512, {})
    argv = [
        "sudo",
        "--non-interactive",
        "--",
        sys.executable,
        "-I",
        str(Path(worker.__file__).resolve()),
        "project-manager-instance",
    ]
    result = asyncio.run(io.command(argv, timeout=1, limit=512))
    assert result["output"] == "ok" and io.used == 2
    assert calls == [
        [*argv[:2], "--preserve-env=" + ",".join(worker.RESOURCE_ENVIRONMENT), *argv[2:]]
    ]


def test_native_workflow_guards_run_and_always_cleanup_with_original_limits() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    native = workflow.read_text().split("  managed-native:\n", 1)[1]
    for flag in (
        "exec sudo --non-interactive systemd-run --system",
        "--service-type=exec --expand-environment=no",
        '--uid="$runner_uid" --gid="$runner_gid" --working-directory="$GITHUB_WORKSPACE"',
        f"--property=MemoryMax={ci.MEMORY}",
        "--property=MemorySwapMax=0",
        "--property=CPUQuota=100%",
        f"--property=TasksMax={ci.TASKS}",
        "--property=OOMPolicy=kill",
        "--property=Restart=no --property=KillMode=control-group --property=SendSIGKILL=yes",
    ):
        assert native.count(flag) == 2
    assert f"--property=RuntimeMaxSec={ci.HARNESS_SECONDS + 2 * ci.CLEANUP_SECONDS}" in native
    assert f"--property=RuntimeMaxSec={2 * ci.CLEANUP_SECONDS} " in native
    assert (
        'unit="muli-native-envelope-run-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.service"' in native
    )
    assert (
        'unit="muli-native-envelope-cleanup-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.service"'
        in native
    )
    cleanup = native.split("      - name: Release only this fixture's user and mounts\n", 1)[1]
    assert cleanup.startswith("        if: always()\n") and "|| true" not in native


@pytest.mark.parametrize("mode", ["project-manager-instance", "bootstrap-files"])
def test_root_helper_rejects_no_envelope_before_proc_peer_or_files(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def effect(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(mode)
        return {"code": "synthetic", "used": 0}

    if mode == "project-manager-instance":
        args = ["worker", mode, "42", "23145", "100", "/owned", "105", "512"]
    else:
        args = [
            "worker",
            mode,
            "check",
            "muli-ci-" + "a" * 12,
            "23145",
            "23145",
            '{"directories":[[1,2],[1,3],[1,4],[1,5],[1,6]],"file":[1,7]}',
            "105",
            "1024",
        ]
    monkeypatch.setattr(worker.os, "getuid", lambda: 0)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(worker, "read_manager_instance", effect)
    monkeypatch.setattr(worker, "bootstrap_files", effect)
    with pytest.raises(worker.NativeError, match="^native_resource_envelope$"):
        worker.main()
    assert calls == [] and capsys.readouterr().out == ""


@pytest.fixture
def lifecycle(
    envelope: Envelope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Real inode/flock/mode/link count, with the synthetic runner's ownership."""
    root = tmp_path / ci.ROOT_NAME
    lock = tmp_path / ".muli-native-envelope-123-2.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)

    def owner(info: os.stat_result) -> os.stat_result:
        fields = list(info)
        fields[4:6] = [envelope.uid, envelope.gid]
        return os.stat_result(fields)

    original_stat, original_lstat, original_fstat = os.stat, os.lstat, os.fstat

    def stat_path(*args: Any, **kwargs: Any) -> os.stat_result:
        return owner(original_stat(*args, **kwargs))

    def lstat_path(*args: Any, **kwargs: Any) -> os.stat_result:
        return owner(original_lstat(*args, **kwargs))

    def fstat(fd: int) -> os.stat_result:
        return owner(original_fstat(fd))

    monkeypatch.setattr(os, "stat", stat_path)
    monkeypatch.setattr(os, "lstat", lstat_path)
    monkeypatch.setattr(os, "fstat", fstat)
    return root, lock


def select_entry(
    mode: str, envelope: Envelope, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope.unit = f"muli-native-envelope-{mode}-123-2.service"
    envelope.membership_reads = 0
    monkeypatch.setenv(worker.RESOURCE_ENVIRONMENT[0], envelope.unit)
    monkeypatch.setattr(sys, "argv", ["ci", mode, str(root)])


def assert_locked(path: Path) -> None:
    import fcntl

    with path.open("r+b") as competing:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("mode", ["run", "cleanup"])
def test_busy_lifecycle_entry_never_constructs_fixture(
    mode: str,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fcntl

    root, lock = lifecycle
    select_entry(mode, envelope, root, monkeypatch)
    calls: list[str] = []

    def construct(path: Path) -> SimpleNamespace:
        calls.append("NativeCI")
        return SimpleNamespace(execute=lambda: None, cleanup=lambda: None)

    monkeypatch.setattr(ci, "NativeCI", construct)
    with lock.open("r+b") as active:
        fcntl.flock(active.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ci.main() == 1
        assert lock.read_bytes() == b""
    assert calls == [] and not root.exists()
    if mode == "cleanup":
        assert capsys.readouterr().err == "native_service_cleanup_failed\n"


def test_cleanup_without_fixture_seals_same_inode_and_blocks_late_run(
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, lock = lifecycle
    identity = (lock.stat().st_dev, lock.stat().st_ino)
    calls: list[str] = []

    def construct(path: Path) -> SimpleNamespace:
        assert path == root and not path.exists()
        assert_locked(lock)  # Already exclusive before any NativeCI constructor.
        mode = sys.argv[1]
        assert lock.read_bytes() == (b"closed\n" if mode == "cleanup" else b"")
        calls.append(mode)
        return SimpleNamespace(execute=lambda: None, cleanup=lambda **kwargs: None)

    monkeypatch.setattr(ci, "NativeCI", construct)
    select_entry("run", envelope, root, monkeypatch)
    assert ci.main() == 0
    select_entry("cleanup", envelope, root, monkeypatch)
    assert ci.main() == 0
    assert lock.read_bytes() == b"closed\n"
    assert (lock.stat().st_dev, lock.stat().st_ino) == identity
    select_entry("run", envelope, root, monkeypatch)
    assert ci.main() == 1
    assert calls == ["run", "cleanup"] and not root.exists()
    assert "native_resource_closed" in capsys.readouterr().err


@pytest.mark.parametrize("population", ["populated", "malformed", "missing"])
def test_cleanup_seals_before_population_and_keeps_orphan_members_fail_closed(
    population: str,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, lock = lifecycle
    select_entry("cleanup", envelope, root, monkeypatch)
    envelope.run_population = {
        "populated": "populated 1\nfrozen 0\n",
        "malformed": "populated uncertain\n",
        "missing": None,
    }[population]
    calls: list[str] = []

    def construct(path: Path) -> SimpleNamespace:
        # A missing run group is acceptable only after the durable closed gate.
        assert lock.read_bytes() == b"closed\n"
        assert_locked(lock)
        calls.append("NativeCI")
        return SimpleNamespace(cleanup=lambda **kwargs: calls.append("cleanup"))

    monkeypatch.setattr(ci, "NativeCI", construct)
    assert ci.main() == (0 if population == "missing" else 1)
    assert lock.read_bytes() == b"closed\n" and not root.exists()
    assert calls == (["NativeCI", "cleanup"] if population == "missing" else [])
    if population != "missing":
        assert capsys.readouterr().err == "native_service_cleanup_failed\n"


def test_cleanup_lifecycle_passes_original_deadline_without_reset(
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, lock = lifecycle
    select_entry("cleanup", envelope, root, monkeypatch)
    now = [100.0]
    monkeypatch.setattr(ci.time, "monotonic", lambda: now[0])
    original_gate = ci.require_resource_envelope

    def slow_resource_gate(mode: str) -> None:
        original_gate(mode)
        now[0] = 103.0

    seen: list[float] = []
    instance = SimpleNamespace(cleanup_deadline=None)

    def cleanup(*, deadline: float | None = None) -> None:
        inherited = deadline if deadline is not None else instance.cleanup_deadline
        assert inherited == 105.0, "resource/lock/closed checks must consume the original 5s"
        assert now[0] == 103.0 and lock.read_bytes() == b"closed\n"
        seen.append(inherited)

    instance.cleanup = cleanup
    monkeypatch.setattr(ci, "require_resource_envelope", slow_resource_gate)
    monkeypatch.setattr(ci, "NativeCI", lambda path: instance)
    assert ci.main() == 0
    assert seen == [105.0]


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_lifecycle_lock_alias_cannot_authorize_fixture_effects(
    alias: str,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, lock = lifecycle
    select_entry("run", envelope, root, monkeypatch)
    other = lock.with_name("foreign-lock")
    other.write_bytes(b"sentinel\n")
    other.chmod(0o600)
    lock.unlink()  # Deliberate fixture corruption before entering production code.
    if alias == "symlink":
        lock.symlink_to(other)
    else:
        os.link(other, lock)
    calls: list[str] = []

    def construct(path: Path) -> SimpleNamespace:
        calls.append("NativeCI")
        return SimpleNamespace(execute=lambda: None)

    monkeypatch.setattr(ci, "NativeCI", construct)
    assert ci.main() == 1
    assert calls == [] and other.read_bytes() == b"sentinel\n" and not root.exists()


@pytest.mark.parametrize("body", [b"closed", b"unknown", b"closed\nx"])
def test_unknown_or_partial_lifecycle_body_is_never_repaired(
    body: bytes,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, lock = lifecycle
    lock.write_bytes(body)
    select_entry("cleanup", envelope, root, monkeypatch)
    calls: list[Path] = []
    monkeypatch.setattr(ci, "NativeCI", lambda path: calls.append(path))
    assert ci.main() == 1
    assert lock.read_bytes() == body and calls == [] and not root.exists()


@pytest.mark.parametrize("mode", ["run", "cleanup"])
def test_first_lifecycle_entry_creates_one_private_fixed_lock(
    mode: str,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, lock = lifecycle
    lock.unlink()  # A brand-new run/attempt, not a production deletion/rebuild.
    select_entry(mode, envelope, root, monkeypatch)
    observed: list[tuple[int, int]] = []

    def effect(**kwargs: object) -> None:
        assert_locked(lock)
        info = lock.lstat()
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 1 and info.st_uid == envelope.uid
        observed.append((info.st_dev, info.st_ino))

    monkeypatch.setattr(
        ci, "NativeCI", lambda path: SimpleNamespace(execute=effect, cleanup=effect)
    )
    assert ci.main() == 0
    assert observed == [(lock.stat().st_dev, lock.stat().st_ino)]
    assert lock.read_bytes() == (b"" if mode == "run" else b"closed\n")
    assert not root.exists()


@pytest.mark.parametrize(
    "case", ["empty", "events-missing", "duplicate", "oversized", "non-ascii", "changed"]
)
def test_cleanup_population_requires_complete_same_group_fact(
    case: str,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, lock = lifecycle
    select_entry("cleanup", envelope, root, monkeypatch)
    envelope.run_group_present = True
    envelope.run_population = {
        "empty": "populated 0\nfrozen 0\n",
        "events-missing": None,
        "duplicate": "populated 0\npopulated 0\n",
        "oversized": "populated 0\n" + " " * 512,
        "non-ascii": "populated 0\n\xff",
        "changed": "populated 0\n",
    }[case]
    envelope.run_group_changed = case == "changed"
    calls: list[str] = []

    def construct(path: Path) -> SimpleNamespace:
        assert lock.read_bytes() == b"closed\n"
        assert_locked(lock)
        calls.append("NativeCI")
        return SimpleNamespace(cleanup=lambda **kwargs: calls.append("cleanup"))

    monkeypatch.setattr(ci, "NativeCI", construct)
    assert ci.main() == (0 if case == "empty" else 1)
    assert calls == (["NativeCI", "cleanup"] if case == "empty" else [])
    assert lock.read_bytes() == b"closed\n" and not root.exists()
    if case != "empty":
        assert capsys.readouterr().err == "native_service_cleanup_failed\n"


def test_expired_cleanup_never_seals_or_constructs_after_resource_gate(
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, lock = lifecycle
    select_entry("cleanup", envelope, root, monkeypatch)
    now = [100.0]
    monkeypatch.setattr(ci.time, "monotonic", lambda: now[0])
    original_gate = ci.require_resource_envelope

    def expired(mode: str) -> None:
        original_gate(mode)
        now[0] = 105.0

    calls: list[Path] = []
    monkeypatch.setattr(ci, "require_resource_envelope", expired)
    monkeypatch.setattr(ci, "NativeCI", lambda path: calls.append(path))
    assert ci.main() == 1
    assert calls == [] and lock.read_bytes() == b"" and not root.exists()
    assert capsys.readouterr().err == "native_cleanup_deadline\n"


@pytest.mark.parametrize("failed", [False, True], ids=["changed-only", "preserve-first-error"])
def test_lifecycle_final_identity_check_releases_without_overwriting_first_error(
    failed: bool,
    envelope: Envelope,
    lifecycle: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fcntl

    root, lock = lifecycle
    select_entry("run", envelope, root, monkeypatch)
    displaced = lock.with_name("displaced")

    def execute() -> None:
        assert_locked(lock)
        lock.rename(displaced)  # Deliberate concurrent fixture corruption, not production.
        lock.write_bytes(b"")
        lock.chmod(0o600)
        if failed:
            raise ci.FixtureError("native_deadline_exceeded")

    monkeypatch.setattr(ci, "NativeCI", lambda path: SimpleNamespace(execute=execute))
    assert ci.main() == 1
    expected = "native_deadline_exceeded" if failed else "native_resource_lifecycle"
    assert capsys.readouterr().err == expected + "\n"
    with displaced.open("r+b") as source:
        fcntl.flock(source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not root.exists()
