"""Pure fixture tests: every native/system command is forbidden or injected."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

import pytest
from helpers import managed_native_worker as worker

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_managed_native_ci.py"
SPEC = importlib.util.spec_from_file_location("native_ci_fixture", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ci = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ci
SPEC.loader.exec_module(ci)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only CI coordinator")


@pytest.fixture(autouse=True)
def forbid_real_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("a pure fixture test attempted a real subprocess")

    monkeypatch.setattr(ci.subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


def kernel(path: Path, memory: int, tasks: int) -> None:
    path.mkdir(parents=True)
    for key, value in {
        "memory.max": memory,
        "memory.swap.max": 0,
        "pids.max": tasks,
        "cpu.max": "100000 100000",
        "cgroup.controllers": "cpu memory pids",
        "memory.oom.group": "1",
        "cgroup.events": "populated 0",
    }.items():
        (path / key).write_text(str(value), encoding="ascii")


class Commands:
    """A finite fake manager, mount registry and passwd/group database."""

    def __init__(self, parent: Path) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.mounts: dict[str, dict[str, Any]] = {}
        self.accounts: dict[str, tuple[int, int, str]] = {}
        self.groups: dict[str, tuple[int, tuple[str, ...]]] = {}
        self.fail_bind = False
        self.fail_cleanup = False
        self.fail_unmount = False
        self.fail_case = False
        self.auto_group = False
        self.uid = 23145
        self.group = f"/user.slice/user-{self.uid}.slice/user@{self.uid}.service"
        self.cgroup_root = parent / "cgroup"
        kernel(self.cgroup_root / self.group.lstrip("/"), ci.MEMORY, ci.TASKS)
        runtime, dependencies, package = (parent / name for name in ("python", "deps", "package"))
        for path in (runtime / "bin", dependencies, package):
            path.mkdir(parents=True)
        executable = runtime / "bin/python"
        executable.write_bytes(b"synthetic executable, never run")
        (package / "__init__.py").write_bytes(b"synthetic package")
        self.description = {
            "runtime": str(runtime),
            "dependencies": str(dependencies),
            "executable": str(executable),
            "package": str(package),
            "size": 17,
            "digest": "a" * 64,
            "limits": {},
            "http_bytes": ci.OUTPUT_BYTES,
        }

    def __call__(self, argv: Any, *, seconds: float = 5) -> Any:
        values = tuple(argv)
        assert 0 < seconds <= ci.HARNESS_SECONDS + ci.CLEANUP_SECONDS
        self.calls.append(values)
        if values[-1] == "describe":
            return ci.Reply(0, json.dumps(self.description).encode())
        if len(values) == 9 and values[3] == "diagnose":
            assert values[:2] == (sys.executable, "-I")
            assert values[4:7] == (
                next(iter(self.accounts)),
                str(self.uid),
                self.accounts[values[4]][2],
            )
            return ci.Reply(
                0,
                json.dumps(
                    {
                        "unit": values[-2],
                        "code": "observed",
                        "stage": "service_properties",
                        "failure": "none",
                        "query_status": 0,
                        "query_code": "ok",
                        "ancestors": [],
                        "load_state": "loaded",
                        "active_state": "failed",
                        "sub_state": "failed",
                        "result": "exit-code",
                        "exec_code": 1,
                        "exec_status": 203,
                        "pid": 0,
                    }
                ).encode(),
            )
        if values[0] == "findmnt":
            row = self.mounts.get(values[values.index("--mountpoint") + 1])
            return (
                ci.Reply(1)
                if row is None
                else ci.Reply(0, json.dumps({"filesystems": [row]}).encode())
            )
        if values[0] == "mkfs.ext4":
            return ci.Reply(0)
        assert values[:2] == ("sudo", "--non-interactive"), values
        args = values[2:]
        if args[0] == "cat":
            path = Path(args[1])
            return ci.Reply(0, path.read_bytes()) if os.path.exists(path) else ci.Reply(1)
        if args[0] == "stat":
            assert args[1:3] == ("--format=%d:%i:%f", "--")
            info = os.stat(args[3])  # Emulates root, not runner's Path.stat permission.
            return ci.Reply(0, f"{info.st_dev}:{info.st_ino}:{info.st_mode:x}\n".encode())
        if args[0] == "mount":
            target = args[-1]
            if "remount,bind,ro,nodev,nosuid" in args:
                self.mounts[target]["options"] = "ro,nodev,nosuid"
            else:
                if "--bind" in args and self.fail_bind:
                    return ci.Reply(1)
                self.mounts[target] = {
                    "target": target,
                    "source": args[-2],
                    "fstype": "ext4",
                    "options": "rw",
                    "id": len(self.mounts) + 1,
                }
            return ci.Reply(0)
        if args[0] == "umount":
            if self.fail_unmount:
                return ci.Reply(1)
            del self.mounts[args[1]]
            return ci.Reply(0)
        if args[0] == "useradd":
            name = args[-1]
            self.accounts[name] = (self.uid, self.uid, args[args.index("--home-dir") + 1])
            self.groups[name] = (self.uid, ())
            return ci.Reply(0)
        if args[0] == "userdel":
            del self.accounts[args[1]]
            if self.auto_group:
                self.groups.pop(args[1], None)
            return ci.Reply(0)
        if args[0] == "groupdel":
            del self.groups[args[1]]
            return ci.Reply(0)
        if args[:2] == ("systemctl", "show"):
            return ci.Reply(0, self.group.encode())
        if args[0] in {"systemctl", "chown", "chmod"}:
            return ci.Reply(0)
        assert args[:1] == ("--user",) and "systemd-run" in args
        if args[-2] == "drive":
            result = {
                "completed": not self.fail_case,
                "stage": "transaction",
                "code": "",
                "timeline": [{"stage": "transaction", "elapsed_ms": 100, "phase": "verifying"}],
                "generations": [],
                "resources": {},
            }
            (Path(args[-1]) / "result.json").write_text(json.dumps(result), encoding="utf-8")
            return ci.Reply(1 if self.fail_case else 0)
        assert args[-2] == "cleanup"
        return ci.Reply(1 if self.fail_cleanup else 0)


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Commands]:
    commands = Commands(tmp_path)
    temporary = tmp_path / "public-temporary"
    temporary.mkdir(mode=0o1777)
    os.chmod(temporary, 0o1777)
    original_lstat = Path.lstat

    def root_owned_parent(path: Path) -> os.stat_result:
        info = original_lstat(path)
        if path == temporary:
            fields = list(info)
            fields[4] = 0  # Synthetic /tmp ownership; no host account is changed.
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(ci, "VOLUME_PARENT", temporary)
    monkeypatch.setattr(Path, "lstat", root_owned_parent)
    monkeypatch.setattr(ci, "account", commands.accounts.get)
    monkeypatch.setattr(ci, "private_group", commands.groups.get)
    monkeypatch.setattr(ci, "CGROUP_ROOT", commands.cgroup_root)
    return ci.NativeCI(tmp_path / ci.ROOT_NAME, run=commands), commands


@pytest.mark.parametrize("kind", ["symlink", "file", "owner", "mode"])
def test_volume_parent_must_be_the_existing_root_owned_sticky_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "temporary"
    target = tmp_path / "target"
    target.mkdir()
    if kind == "symlink":
        path.symlink_to(target, target_is_directory=True)
    elif kind == "file":
        path.write_bytes(b"not a directory")
    else:
        path.mkdir()
    original = path.lstat()
    fields = list(original)
    fields[4] = 1 if kind == "owner" else 0
    if kind in {"owner", "mode"}:
        fields[0] = stat.S_IFDIR | (0o777 if kind == "mode" else 0o1777)
    monkeypatch.setattr(ci, "VOLUME_PARENT", path)
    monkeypatch.setattr(Path, "lstat", lambda value: os.stat_result(fields))
    with pytest.raises(ci.FixtureError, match="temporary_parent_invalid"):
        ci.temporary_identity()


def test_private_runner_ancestor_is_not_required_by_either_transient_or_fresh_cleanup(
    fixture: tuple[Any, Commands],
    tmp_path: Path,
) -> None:
    native, commands = fixture
    runner = tmp_path / "runner-home"
    runner.mkdir(mode=0o750)
    temporary = runner / "_temp"
    temporary.mkdir()
    native.root = temporary / ci.ROOT_NAME
    native.receipt_path = native.root / "receipt.json"

    def no_runner_access(argv: Any, *, seconds: float = 5) -> Any:
        if "systemd-run" in argv:
            executable = next(Path(value) for value in argv if value.endswith("/a/bin/python"))
            if executable.is_relative_to(runner):
                commands.calls.append(tuple(argv))
                return ci.Reply(1, b"Failed to find executable: Permission denied")
        return commands(argv, seconds=seconds)

    native.run = no_runner_access
    native.execute()
    assert not native.volume.is_relative_to(runner)
    assert native.receipt_path.is_relative_to(temporary)
    assert (native.root / "filesystem.ext4").is_file()
    directory = native.receipt.volume_directory
    recovered = ci.NativeCI(native.root, run=no_runner_access)
    recovered.cleanup()
    assert recovered.volume == native.volume and recovered.home == native.home
    assert recovered.receipt.volume_directory == directory
    assert not commands.mounts and not commands.accounts and not commands.groups
    assert (
        ci.fixture_result((native.root / "result.json").read_bytes())["resources"]["cleanup"]
        == "verified"
    )
    assert runner.stat().st_mode & 0o777 == 0o750
    assert not any("setfacl" in args or str(runner) in args for args in commands.calls)


@pytest.mark.parametrize("entry", ["directory", "dangling-symlink"])
def test_existing_volume_entry_is_not_adopted_or_deleted(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    from types import SimpleNamespace

    native, commands = fixture
    monkeypatch.setattr(ci.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    volume = ci.VOLUME_PARENT / f"{ci.ROOT_NAME}-muli-ci-{'a' * 12}"
    if entry == "directory":
        volume.mkdir(mode=0o700)
        (volume / "sentinel").write_bytes(b"unowned")
    else:
        volume.symlink_to(volume.with_name("missing-target"), target_is_directory=True)
    with pytest.raises(FileExistsError):
        native.prepare()
    assert ci.Receipt.read(native.receipt_path).volume_directory is None
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="volume_creation_unconfirmed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.calls
    if entry == "directory":
        assert (volume / "sentinel").read_bytes() == b"unowned"
    else:
        assert volume.is_symlink()


def test_changed_temporary_parent_rejects_cleanup_before_any_effect(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    temporary = ci.VOLUME_PARENT
    temporary.rename(temporary.with_name("original-temporary"))
    temporary.mkdir(mode=0o1777)
    os.chmod(temporary, 0o1777)
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="temporary_parent_changed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.calls and commands.accounts and commands.mounts


def test_changed_original_volume_directory_cannot_authorize_account_deletion(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    native.volume.rename(native.volume.with_name("replaced-volume"))
    native.volume.mkdir(mode=0o700)
    commands.mounts.clear()  # The recorded mounts have disappeared, not moved here.
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="volume_directory_changed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert commands.accounts and not any("userdel" in args for args in commands.calls)


def test_default_runner_cannot_accidentally_run_a_native_effect() -> None:
    with pytest.raises(AssertionError, match="real subprocess"):
        ci.command(("systemctl", "--user", "start", "muli.service"))


def test_workstation_entry_is_rejected_before_any_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ci.FixtureError, match="hosted_ci_required"):
        ci.require_ci(tmp_path / ci.ROOT_NAME)
    assert not (tmp_path / ci.ROOT_NAME).exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_ACTIONS", "false"),
        ("RUNNER_OS", "Windows"),
        ("RUNNER_TEMP", "/missing-native-fixture"),
    ],
)
def test_ci_identity_does_not_accept_partial_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    for name, item in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, item)
    monkeypatch.setenv(key, value)
    with pytest.raises((ci.FixtureError, FileNotFoundError)):
        ci.require_ci(tmp_path / ci.ROOT_NAME)


def test_setup_uses_only_two_readonly_binds_and_moves_volume_ownership_in_order(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    calls = commands.calls
    mounted = next(
        index
        for index, args in enumerate(calls)
        if args[:3] == ("sudo", "--non-interactive", "mount")
    )
    runner_chown = (
        "sudo",
        "--non-interactive",
        "chown",
        f"{os.getuid()}:{os.getgid()}",
        str(native.volume),
    )
    assert calls.index(runner_chown) > mounted
    handed_off = next(index for index, args in enumerate(calls) if "chown" in args and "-R" in args)
    binds = [index for index, args in enumerate(calls) if "--bind" in args]
    assert len(binds) == 2 and handed_off < min(binds)
    assert all(
        "ro" in row["options"]
        for target, row in commands.mounts.items()
        if target != str(native.volume)
    )
    assert native.receipt_path.stat().st_uid == os.getuid()
    assert native.receipt_path.stat().st_mode & 0o077 == 0
    assert not any("linger" in arg for args in calls for arg in args)
    assert all(
        "user.slice" not in args and not any(arg == f"user@{os.getuid()}.service" for arg in args)
        for args in calls
    )


def test_existing_root_and_account_conflicts_do_not_launch_any_service(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    monkeypatch.setattr(ci, "account", lambda name: (1, 1, "/unrelated"))
    with pytest.raises(ci.FixtureError, match="user_exists"):
        native.prepare()
    assert not any(
        "useradd" in args or "mount" in args or "start" in args for args in commands.calls
    )
    with pytest.raises(ci.FixtureError, match="root_exists"):
        native.prepare()


def test_mount_observation_does_not_need_runner_access_after_private_handoff(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    original = Path.stat
    handed_off = False

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal handed_off
        reply = commands(argv, seconds=seconds)
        if "chown" in argv and "-R" in argv:
            handed_off = True
        return reply

    def runner_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if (
            handed_off
            and path.is_relative_to(native.volume)
            and (path != native.volume or follow_symlinks)
        ):
            raise PermissionError("the runner cannot traverse the private 0700 volume")
        return original(path, follow_symlinks=follow_symlinks)

    native.run = run
    monkeypatch.setattr(Path, "stat", runner_stat)
    native.prepare()
    native.cleanup()
    assert not commands.mounts and not commands.accounts
    assert any("stat" in args for args in commands.calls)


def test_unconfirmed_useradd_return_never_authorizes_account_deletion_or_clean_success(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture

    def lost(argv: Any, *, seconds: float = 5) -> Any:
        reply = commands(argv, seconds=seconds)
        return ci.Reply(1) if "useradd" in argv else reply

    native.run = lost
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.prepare()
    assert commands.accounts and not ci.Receipt.read(native.receipt_path).user_created
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="account_creation_unconfirmed"):
        native.cleanup()
    assert commands.accounts and commands.mounts
    assert not any("umount" in args or "userdel" in args for args in commands.calls)


def test_partial_bind_failure_retains_exact_created_resources_for_cleanup(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    commands.fail_bind = True
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.prepare()
    assert len(native.receipt.mounts) == 1 and native.receipt.user_created
    assert not any("start" in args for args in commands.calls)
    native.cleanup()
    assert commands.mounts == {} and commands.accounts == {} and commands.groups == {}


@pytest.mark.parametrize("automatic_group", [False, True])
def test_cleanup_reclaims_same_readonly_bind_after_lost_remount_return_and_private_group(
    fixture: tuple[Any, Commands], automatic_group: bool
) -> None:
    native, commands = fixture
    commands.auto_group = automatic_group
    native.prepare()
    # Receipt intentionally retains the original rw observation of the same mount.
    assert native.receipt.mounts[-1]["options"] == "rw"
    assert commands.mounts[str(native.volume / "input/deps")]["options"].startswith("ro")
    native.cleanup()
    assert commands.mounts == {} and commands.accounts == {} and commands.groups == {}
    saved = ci.Receipt.read(native.receipt_path)
    assert saved.user_removed and saved.group_removed
    assert len([args for args in commands.calls if "groupdel" in args]) == (
        0 if automatic_group else 1
    )
    assert any("revert" in args for args in commands.calls)
    native.cleanup()  # Idempotent observation, no newly allocated authority.


@pytest.mark.parametrize("change", ["account", "mount", "image"])
def test_cleanup_rejects_replaced_resource_without_unmount_or_userdel(
    fixture: tuple[Any, Commands], change: str
) -> None:
    native, commands = fixture
    native.prepare()
    if change == "account":
        commands.accounts[native.receipt.user] = (commands.uid + 1, commands.uid, str(native.home))
    elif change == "mount":
        commands.mounts[str(native.volume / "input/deps")]["id"] += 1
    else:
        image = native.root / "filesystem.ext4"
        image.rename(image.with_suffix(".old"))
        image.write_bytes(b"replaced")
    commands.calls.clear()
    with pytest.raises(ci.FixtureError):
        native.cleanup()
    assert not any("umount" in args or "userdel" in args for args in commands.calls)


def test_cleanup_failure_is_not_hidden_even_after_dedicated_manager_is_stopped(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    commands.fail_cleanup = True
    with pytest.raises(ci.FixtureError, match="service_cleanup_failed"):
        native.cleanup()
    assert any("stop" in args and f"user@{commands.uid}.service" in args for args in commands.calls)
    assert not commands.mounts


def test_unmount_failure_preserves_account_receipt_and_private_tree(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    commands.fail_unmount = True
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.cleanup()
    assert commands.accounts and native.volume.exists() and native.receipt_path.exists()
    assert not any("userdel" in args for args in commands.calls)


def test_failed_case_exports_bounded_phase_evidence_before_cleanup(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    commands.fail_case = True
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.execute()
    result = json.loads((native.root / "result.json").read_bytes())
    assert result["completed"] is False and result["timeline"][0]["phase"] == "verifying"
    assert len((native.root / "result.json").read_bytes()) <= ci.OUTPUT_BYTES
    assert set(result) == {"completed", "stage", "code", "timeline", "generations", "resources"}
    assert not any("diagnose" in args for args in commands.calls)


@pytest.mark.parametrize(
    "name,value",
    [
        ("memory.max", "max"),
        ("memory.max", str(ci.MEMORY + 1)),
        ("memory.swap.max", "1"),
        ("cpu.max", "200000 100000"),
        ("pids.max", "129"),
    ],
)
def test_actual_kernel_limits_cannot_be_just_property_claims(
    tmp_path: Path, name: str, value: str
) -> None:
    path = tmp_path / "group"
    kernel(path, ci.MEMORY, ci.TASKS)
    (path / name).write_text(value, encoding="ascii")
    with pytest.raises(ci.FixtureError):
        ci.verify_kernel(path, ci.MEMORY, ci.TASKS)
    with pytest.raises(worker.NativeError):
        worker.kernel_limits(path, memory=ci.MEMORY, tasks=ci.TASKS)


@pytest.mark.parametrize("seconds", [0, -1, 61])
def test_functional_budget_is_not_extended(seconds: float, tmp_path: Path) -> None:
    with pytest.raises(worker.NativeError, match="case_budget"):
        worker.Case(tmp_path, seconds=seconds)


def test_case_deadline_and_timeline_are_bounded_without_printing_private_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = 100.0
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock)
    case = worker.Case(tmp_path)
    case.note("transaction", generation="a" * 32, phase="verifying")
    clock = 159
    assert worker.remaining(case.deadline) == 1
    case.note("transaction", generation="a" * 32, phase="verifying")
    assert len(case.result["timeline"]) == 1
    with pytest.raises(worker.NativeError, match="fact_invalid"):
        case.note("transaction", nonce="private")
    clock = 160
    with pytest.raises(worker.NativeError, match="deadline_exceeded"):
        worker.remaining(case.deadline)


async def test_actual_uid_import_layout_failure_precedes_any_manager_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "fixture", lambda root: {"uid": os.geteuid()})
    with pytest.raises(worker.NativeError, match="prefix_invalid"):
        await worker.Case(tmp_path).drive()


def test_initial_db_identity_cannot_move_together_with_the_record(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from muli.store import Store

    database = tmp_path / "data/muli.sqlite3"
    database.parent.mkdir()
    store = Store.open_runtime(database, default_mode="auto", durable=True)
    try:
        case = worker.Case(tmp_path)
        case.freeze_initial()
        baseline = case.result["resources"]["initial"]
        record = SimpleNamespace(
            identity=SimpleNamespace(store_instance="different", main_session=baseline["main"])
        )
        with pytest.raises(worker.NativeError, match="initial_identity_changed"):
            case.read_database(record, expect_latest=False)
        database.rename(database.with_suffix(".previous"))
        database.write_bytes(b"another database")
        with pytest.raises(worker.NativeError, match="database_replaced"):
            case.read_database(record, expect_latest=False)
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["freeze", "final"])
def test_readonly_database_observation_fails_immediately_on_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import sqlite3
    from types import SimpleNamespace

    from muli.format_evidence import FormatEvidenceError

    database = tmp_path / "data/muli.sqlite3"
    database.parent.mkdir()
    original = sqlite3.connect
    owner = original(database)
    owner.execute("CREATE TABLE fixture(value)")
    owner.commit()
    owner.execute("BEGIN EXCLUSIVE")
    readers: list[sqlite3.Connection] = []

    def readonly(path: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
        assert path == database.as_uri() + "?mode=ro" and uri is True and timeout == 0
        connection = original(path, uri=uri, timeout=timeout)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (0,)
        readers.append(connection)
        return connection

    case = worker.Case(tmp_path)
    identity = database.stat()
    case.result["resources"]["initial"] = {
        "store": "store",
        "main": "main",
        "device": identity.st_dev,
        "inode": identity.st_ino,
    }
    record = SimpleNamespace(identity=SimpleNamespace(store_instance="store", main_session="main"))
    monkeypatch.setattr(worker.sqlite3, "connect", readonly)
    try:
        with pytest.raises((sqlite3.OperationalError, FormatEvidenceError)):
            if operation == "freeze":
                case.freeze_initial()
            else:
                case.read_database(record, expect_latest=False)
        assert owner.in_transaction and len(readers) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            readers[0].execute("SELECT 1")
        assert not (tmp_path / "result.json").exists()
    finally:
        owner.rollback()
        owner.close()


def test_receipt_rejects_unbounded_or_foreign_metadata(fixture: tuple[Any, Commands]) -> None:
    native, _ = fixture
    native.prepare()
    value = asdict(native.receipt)
    value["mounts"] *= 2
    ci.write_json(native.receipt_path, value)
    with pytest.raises(ci.FixtureError, match="receipt_invalid"):
        ci.Receipt.read(native.receipt_path)


def test_workflow_has_always_cleanup_and_only_explicit_hosted_native_job() -> None:
    workflow = (SCRIPT.parents[1] / ".github/workflows/ci.yml").read_text()
    native = workflow.split("  managed-native:\n", 1)[1]
    assert "runs-on: ubuntu-24.04" in native and "timeout-minutes: 5" in native
    assert native.count("check_managed_native_ci.py") == 2
    assert 'cleanup "$RUNNER_TEMP/muli-managed-native-ci"' in native
    assert "if: always()" in native and "result.json" in native
    assert "uv pip" not in native and "uv tool" not in native


@pytest.mark.parametrize("found", [False, True])
async def test_standard_service_uses_exact_structured_listing_not_helper_name_gate(
    found: bool,
) -> None:
    from muli.platforms.linux_transaction import ObservationError, SystemdProbe

    commands: list[list[str]] = []

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        commands.append(argv)
        require_suffix = ["ListUnitsByPatterns", "asas", "0", "1", "muli.service"]
        assert argv[-5:] == require_suffix
        entry = [
            "muli.service",
            "fixture",
            "loaded",
            "inactive",
            "dead",
            "",
            "/org/freedesktop/systemd1/unit/muli_2eservice",
            0,
            "",
            "/",
        ]
        return {
            "exit_code": 0,
            "output": json.dumps({"type": "a(ssssssouso)", "data": [[entry] if found else []]}),
        }

    probe = SystemdProbe(runner=query)
    with pytest.raises(ObservationError, match="invalid_helper_identity"):
        await probe.helper_exists("muli.service")  # The product gate must stay strict.
    assert await worker.service_exists(probe) is found
    assert len(commands) == 1


async def test_standard_service_manager_failure_is_not_absence() -> None:
    from muli.platforms.linux_transaction import ObservationError, SystemdProbe

    async def unavailable(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        return {"exit_code": 1, "output": ""}

    with pytest.raises(ObservationError, match="observation_unavailable"):
        await worker.service_exists(SystemdProbe(runner=unavailable))


def test_cleanup_uses_one_absolute_budget_and_keeps_resources_when_it_expires(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    native.prepare()
    commands.calls.clear()
    saved = worker.Case(native.volume).result
    saved.update(completed=True, stage="complete")
    worker.write_json(native.volume / "result.json", saved)
    ci.write_json(native.root / "result.json", saved)
    clock = 100.0
    budgets: list[float] = []

    def slow(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal clock
        budgets.append(seconds)
        result = commands(argv, seconds=seconds)
        clock += 1.5
        return result

    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)
    native.run = slow
    with pytest.raises(ci.FixtureError, match="cleanup_deadline"):
        native.cleanup()
    assert budgets == [5, 3.5, 2, 0.5]
    cleanup = next(args for args in commands.calls if "systemd-run" in args)
    assert "--setenv=MULI_NATIVE_CLEANUP_DEADLINE=105.0" in cleanup
    assert commands.mounts and commands.accounts
    assert not any("umount" in args or "userdel" in args for args in commands.calls)
    assert ci.Receipt.read(native.receipt_path).mounts
    assert ci.fixture_result((native.root / "result.json").read_bytes()) == saved
    assert "cleanup" not in saved["resources"]


@pytest.mark.parametrize("end", ["100", "106", "nan", "inf"])
async def test_cleanup_worker_cannot_extend_coordinator_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    monkeypatch.setenv("MULI_NATIVE_CLEANUP_DEADLINE", end)
    called = False

    async def cleanup(case: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(worker.Case, "cleanup", cleanup)
    assert await worker.run("cleanup", tmp_path) == 1
    assert not called


def test_last_receipt_save_cannot_publish_cleanup_verified_after_deadline(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, _ = fixture
    native.prepare()
    saved = worker.Case(native.volume).result
    saved.update(completed=True, stage="complete")
    worker.write_json(native.volume / "result.json", saved)
    ci.write_json(native.root / "result.json", saved)
    clock = 100.0
    original = native.save

    def late_save() -> None:
        nonlocal clock
        original()
        if native.receipt.group_removed:
            clock = 105.0

    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)
    monkeypatch.setattr(native, "save", late_save)
    with pytest.raises(ci.FixtureError, match="cleanup_deadline"):
        native.cleanup()
    assert ci.Receipt.read(native.receipt_path).group_removed
    assert ci.fixture_result((native.root / "result.json").read_bytes()) == saved


async def test_cleanup_preserves_functional_timeline_and_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
    from muli.platforms import linux_transaction

    case = worker.Case(tmp_path, seconds=worker.CLEANUP_SECONDS)
    record = SimpleNamespace(identity=SimpleNamespace(deployment="a" * 64), generation="b" * 32)
    name = managed_helper_unit_name(record)
    anchor = SimpleNamespace(digest="a" * 64, controller_slot="c" * 64, journal_root=str(tmp_path))
    controller = SimpleNamespace(digest="c" * 64, lexical_python=str(tmp_path / "a/bin/python"))
    argv = managed_helper_arguments(anchor, controller)
    saved = {
        "completed": True,
        "stage": "complete",
        "code": "",
        "timeline": [{"stage": "complete", "elapsed_ms": 500}],
        "generations": [{"deployment": "a" * 64, "generation": "b" * 32, "helper": name}],
        "resources": {"initial": {"store": "store", "main": "main", "device": 1, "inode": 2}},
    }
    worker.write_json(tmp_path / "result.json", saved)
    state = tmp_path / "data/management/transaction-v1/state.json"
    state.parent.mkdir(parents=True)
    state.touch()

    class Probe:
        async def helper_exists(self, helper: str) -> bool:
            assert helper == name
            return True

        async def _unit_path(self, helper: str) -> str:
            return helper

        async def _snapshot(self, path: str) -> Any:
            return SimpleNamespace(argv=argv)

    async def command(*values: str) -> str:
        assert values == ("systemctl", "--user", "stop", name)
        return ""

    async def empty(probe: Any, helper: str, arguments: tuple[str, ...]) -> bool:
        case.note("helper_exit", helper=helper, state="inactive")
        return True

    async def absent(probe: Any) -> bool:
        return False

    verified: list[bool] = []
    journal = SimpleNamespace(writer_lease=lambda: nullcontext())
    monkeypatch.setattr(case, "journal", lambda: nullcontext(journal))
    monkeypatch.setattr(case, "record", lambda source: record)
    monkeypatch.setattr(case, "deployment", lambda source, item: (anchor, controller, {}))
    monkeypatch.setattr(case, "command", command)
    monkeypatch.setattr(case, "helper_empty", empty)
    monkeypatch.setattr(
        case, "read_database", lambda item, *, expect_latest: verified.append(expect_latest)
    )
    monkeypatch.setattr(worker, "fixture", lambda root: {"uid": os.geteuid()})
    monkeypatch.setattr(worker, "membership", lambda unit, uid: tmp_path)
    monkeypatch.setattr(worker, "service_exists", absent)
    monkeypatch.setattr(linux_transaction, "SystemdProbe", lambda **kwargs: Probe())
    await case.cleanup()
    assert verified == [True]
    assert case.result == saved
    assert worker.read_json(tmp_path / "result.json") == saved


@pytest.mark.parametrize("change", ["timeline", "resources", "value"])
def test_export_rejects_fields_outside_fixed_fixture_facts(
    fixture: tuple[Any, Commands], change: str
) -> None:
    native, _ = fixture
    native.prepare()
    value = worker.Case(native.volume).result
    if change == "timeline":
        value["timeline"] = [{"stage": "import", "elapsed_ms": 0, "nonce": "private"}]
    elif change == "resources":
        value["resources"] = {"config": {"password": "private"}}
    else:
        value["timeline"] = [{"stage": "import", "elapsed_ms": 0, "state": {"raw": "private"}}]
    worker.write_json(native.volume / "result.json", value)
    with pytest.raises(ci.FixtureError, match="result_invalid"):
        native.collect_result()
    assert not (native.root / "result.json").exists()


def test_worker_import_failure_still_leaves_coordinator_phase_evidence(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        if "systemd-run" in argv and argv[-2] == "drive":
            commands.calls.append(tuple(argv))
            return ci.Reply(1, b"private command stderr must not be exported")
        return commands(argv, seconds=seconds)

    native.run = fail
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.execute()
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["completed"] is False and saved["stage"] == "harness_start"
    assert saved["code"] == "worker_not_observed"
    facts = saved["resources"]["startup"]
    assert facts["client_status"] == 1 and facts["exec_status"] == 203
    assert facts["client_code"] == "command_failed"
    assert facts["diagnostic_status"] == 0 and facts["diagnostic_code"] == "ok"
    assert facts["exec_code"] == 1 and facts["active_state"] == "failed"
    assert saved["resources"]["manager"]["memory.max"] == str(ci.MEMORY)
    assert saved["generations"] == []
    assert b"private" not in (native.root / "result.json").read_bytes()
    launched = next(args for args in commands.calls if "systemd-run" in args)
    diagnostic = next(args for args in commands.calls if "diagnose" in args)
    unit = next(arg.removeprefix("--unit=") for arg in launched if arg.startswith("--unit="))
    assert facts["unit"] == unit == diagnostic[-2]
    assert not diagnostic[0].startswith(str(native.volume))  # Runner's trusted interpreter.
    assert diagnostic[2] == str(SCRIPT.parents[1] / "tests/helpers/managed_native_worker.py")
    assert len([args for args in commands.calls if "systemd-run" in args]) == 1


def test_retained_python_layout_and_systemd_arguments_remain_exact(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    config = worker.read_json(native.volume / "fixture.json")
    argv = native.transient(config, "drive", ci.HARNESS_SECONDS)
    assert "--wait" in argv and "--pipe" in argv and "--service-type=exec" in argv
    assert "--expand-environment=no" in argv and "--property=Restart=no" in argv
    assert f"--property=MemoryMax={ci.HARNESS_MEMORY}" in argv
    assert "--property=MemorySwapMax=0" in argv
    assert f"--property=RuntimeMaxSec={ci.HARNESS_SECONDS}" in argv
    assert argv[-5:] == (
        str(native.volume / "a/bin/python"),
        "-I",
        str(native.volume / "managed_native_worker.py"),
        "drive",
        str(native.volume),
    )
    original = Path(commands.description["executable"]).relative_to(commands.description["runtime"])
    assert (native.volume / "a/bin/python").readlink() == native.volume / "input/python" / original
    assert (native.volume / "a/pyvenv.cfg").read_text() == (
        f"home = {native.volume / 'input/python' / original.parent}\n"
        "include-system-site-packages = false\n"
    )


@pytest.mark.parametrize("change", ["body", "unit", "extra", "type"])
def test_untrusted_startup_diagnostic_cannot_replace_the_original_failure(
    fixture: tuple[Any, Commands], change: str
) -> None:
    native, commands = fixture

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        if "systemd-run" in argv and argv[-2] == "drive":
            return ci.Reply(1)
        reply = commands(argv, seconds=seconds)
        if "diagnose" not in argv:
            return reply
        if change == "body":
            return ci.Reply(1, b"private diagnostic body")
        value = json.loads(reply.output)
        if change == "unit":
            value["unit"] = "muli-native-" + "f" * 32 + ".service"
        elif change == "extra":
            value["stderr"] = "private diagnostic body"
        else:
            value["exec_status"] = True
        return ci.Reply(0, json.dumps(value).encode())

    native.run = fail
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.execute()
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["code"] == "worker_not_observed" and saved["generations"] == []
    assert saved["resources"]["startup"]["code"] == "unavailable"
    assert b"private" not in (native.root / "result.json").read_bytes()


def test_startup_diagnostic_cannot_extend_the_original_harness_deadline(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    clock = 100.0

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal clock
        if "systemd-run" in argv and argv[-2] == "drive":
            clock += ci.HARNESS_SECONDS + ci.CLEANUP_SECONDS
            return ci.Reply(1)
        return commands(argv, seconds=seconds)

    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)
    native.run = fail
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.execute()
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"]["startup"]["code"] == "unavailable"
    assert not any("diagnose" in args for args in commands.calls)


async def test_diagnostic_uses_typed_probe_and_only_the_verified_uid_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd
    from types import SimpleNamespace

    from muli import process

    user, uid, unit = "muli-ci-" + "a" * 12, 23145, "muli-native-" + "b" * 32 + ".service"
    home = tmp_path / "volume/home"
    path = "/org/freedesktop/systemd1/unit/fixture"
    calls: list[list[str]] = []
    rows = {
        "Id": ("s", unit),
        "LoadState": ("s", "loaded"),
        "ActiveState": ("s", "failed"),
        "SubState": ("s", "failed"),
        "Result": ("s", "exit-code"),
        "ExecMainCode": ("i", 1),
        "ExecMainStatus": ("i", 203),
        "MainPID": ("u", 0),
    }

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert argv[:7] == ["sudo", "--non-interactive", "--user", user, "--", "env", "-i"]
        assert f"HOME={home}" in argv and f"XDG_RUNTIME_DIR=/run/user/{uid}" in argv
        assert f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus" in argv
        assert 0 < timeout <= ci.CLEANUP_SECONDS and limit == ci.OUTPUT_BYTES
        assert merge_stderr is False and "--auto-start=no" in argv
        calls.append(argv)
        if "ListUnitsByPatterns" in argv:
            assert argv[-1] == unit
            output = json.dumps(
                {
                    "type": "a(ssssssouso)",
                    "data": [[[unit, "", "loaded", "failed", "failed", "", path, 0, "", "/"]]],
                }
            )
        elif "GetUnit" in argv:
            assert argv[-1] == unit
            output = json.dumps({"type": "o", "data": [path]})
        else:
            index = argv.index("get-property")
            if argv[index + 2] == "/org/freedesktop/systemd1":
                assert argv[index + 4 :] == ["Version"]
                return {
                    "exit_code": 0,
                    "output": json.dumps({"type": "s", "data": "255"}),
                    "stderr": "",
                }
            assert argv[index + 2] == path
            output = "\n".join(
                json.dumps({"type": rows[name][0], "data": rows[name][1]})
                for name in argv[index + 4 :]
            )
        return {"exit_code": 0, "output": output, "stderr": "not exported"}

    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid, pw_dir=str(home)))
    monkeypatch.setattr(process, "run_process", query)
    value = await worker.diagnose(user, uid, home, unit, worker.time.monotonic() + 5)
    assert value["unit"] == unit and value["exec_status"] == 203 and len(calls) == 5
    assert ci.startup_facts(
        {
            **value,
            "client_status": 1,
            "client_code": "command_failed",
            "diagnostic_status": 0,
            "diagnostic_code": "ok",
        },
        unit,
    )


@pytest.mark.parametrize("change", ["identity", "unit", "deadline"])
async def test_diagnostic_identity_and_deadline_reject_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import pwd
    from types import SimpleNamespace

    user, uid, unit = "muli-ci-" + "a" * 12, 23145, "muli-native-" + "b" * 32 + ".service"
    home = tmp_path / "home"
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=uid + (change == "identity"), pw_dir=str(home)),
    )
    if change == "unit":
        unit = "muli.service"
    deadline = worker.time.monotonic() + (0 if change == "deadline" else 5)
    if change == "identity":
        value = await worker.diagnose(user, uid, home, unit, deadline)
        assert value["stage"] == "identity" and value["failure"] == "identity_failed"
    else:
        with pytest.raises(worker.NativeError):
            await worker.diagnose(user, uid, home, unit, deadline)


def test_diagnostic_cli_dispatch_is_readonly_and_uses_exact_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed: list[tuple[str, int, Path, str, float]] = []
    unit = "muli-native-" + "b" * 32 + ".service"
    user = "muli-ci-" + "a" * 12

    async def diagnose(
        name: str, uid: int, home: Path, target: str, deadline: float
    ) -> dict[str, Any]:
        observed.append((name, uid, home, target, deadline))
        return {"unit": target, "code": "unavailable"}

    monkeypatch.setattr(worker, "diagnose", diagnose)
    monkeypatch.setattr(
        sys, "argv", ["worker", "diagnose", user, "23145", str(tmp_path), unit, "9"]
    )
    assert worker.main() == 0
    assert observed == [(user, 23145, tmp_path, unit, 9)]
    assert json.loads(capsys.readouterr().out) == {"unit": unit, "code": "unavailable"}


@pytest.mark.parametrize(
    "body,expected",
    [
        (b"Failed to find executable /private/python: Permission denied", "executable_permission"),
        (
            b"Failed to find executable /private/python: No such file or directory",
            "executable_missing",
        ),
        (b"Failed to connect to bus: No medium found", "bus_unavailable"),
        (b"Failed to connect to bus: Permission denied", "bus_permission"),
        (b"unrecognized option '--private'", "unsupported_argument"),
        (b"Failed to set unit properties: Unknown assignment private", "property_rejected"),
        (b"Access denied to private unit", "permission_denied"),
        (b"Unit private.service not loaded", "unit_unavailable"),
        (b"private arbitrary secret", "command_failed"),
    ],
    ids=[
        "exec-denied",
        "exec-missing",
        "bus-missing",
        "bus-denied",
        "option",
        "property",
        "denied",
        "unit",
        "unknown",
    ],
)
def test_start_client_classification_exports_only_fixed_labels(body: bytes, expected: str) -> None:
    assert ci.command_code(1, body) == expected
    assert ci.command_code(0, body) == "ok"
    assert ci.command_code(None, body) == "not_started"


def test_both_client_failures_are_preserved_without_raw_output(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        if "systemd-run" in argv and argv[-2] == "drive":
            return ci.Reply(1, b"Failed to find executable /private/python: Permission denied")
        if "diagnose" in argv:
            return ci.Reply(7, b"Failed to connect to bus: No medium found; private secret")
        return commands(argv, seconds=seconds)

    native.run = fail
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.execute()
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    facts = saved["resources"]["startup"]
    assert (facts["client_status"], facts["client_code"]) == (1, "executable_permission")
    assert (facts["diagnostic_status"], facts["diagnostic_code"]) == (7, "bus_unavailable")
    assert facts["code"] == "unavailable" and facts["stage"] == "not_started"
    assert "private" not in json.dumps(saved)


def test_start_client_timeout_is_not_reported_as_never_started(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture

    def timeout(argv: Any, *, seconds: float = 5) -> Any:
        if "systemd-run" in argv and argv[-2] == "drive":
            raise ci.FixtureError("native_command_timeout")
        return commands(argv, seconds=seconds)

    native.run = timeout
    with pytest.raises(ci.FixtureError, match="command_timeout"):
        native.execute()
    facts = ci.fixture_result((native.root / "result.json").read_bytes())["resources"]["startup"]
    assert facts["client_status"] is None and facts["client_code"] == "timeout"
    assert facts["diagnostic_status"] == 0 and facts["code"] == "observed"


@pytest.mark.parametrize(
    "failed_stage,kind,expected_code,expected_failure",
    [
        ("manager", "bus", "unavailable", "query_failed"),
        ("listing", "absent", "unit_absent", "none"),
        ("listing", "invalid", "unavailable", "invalid_reply"),
        ("get_unit", "unit", "unavailable", "query_failed"),
        ("unit_properties", "permission", "unavailable", "query_failed"),
        ("service_properties", "property", "unavailable", "query_failed"),
    ],
    ids=["bus", "absent", "bad-list", "get-unit", "unit-properties", "service-properties"],
)
async def test_diagnostic_keeps_the_exact_failed_stage_and_only_typed_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
    kind: str,
    expected_code: str,
    expected_failure: str,
) -> None:
    import pwd
    from types import SimpleNamespace

    from muli import process

    user, uid, unit = "muli-ci-" + "a" * 12, 23145, "muli-native-" + "b" * 32 + ".service"
    home, path = tmp_path / "volume/home", "/org/freedesktop/systemd1/unit/fixture"
    calls: list[str] = []
    rows = {
        "Id": ("s", unit),
        "LoadState": ("s", "loaded"),
        "ActiveState": ("s", "failed"),
        "SubState": ("s", "failed"),
        "Result": ("s", "exit-code"),
        "ExecMainCode": ("i", 1),
        "ExecMainStatus": ("i", 203),
        "MainPID": ("u", 0),
    }
    errors = {
        "bus": ("Failed to connect to bus: No medium found", "bus_unavailable"),
        "unit": ("Unit private.service not loaded", "unit_unavailable"),
        "permission": ("Access denied private", "permission_denied"),
        "property": ("Unknown property private", "property_rejected"),
    }

    async def query(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        if "ListUnitsByPatterns" in argv:
            stage = "listing"
            output = json.dumps(
                {
                    "type": "a(ssssssouso)",
                    "data": [[[unit, "", "loaded", "failed", "failed", "", path, 0, "", "/"]]],
                }
            )
        elif "GetUnit" in argv:
            stage, output = "get_unit", json.dumps({"type": "o", "data": [path]})
        else:
            fields = argv[argv.index("get-property") + 4 :]
            if fields == ["Version"]:
                stage, output = "manager", json.dumps({"type": "s", "data": "255"})
            else:
                stage = "unit_properties" if fields[0] == "Id" else "service_properties"
                output = "\n".join(
                    json.dumps({"type": rows[name][0], "data": rows[name][1]}) for name in fields
                )
        calls.append(stage)
        if stage == failed_stage:
            if kind == "absent":
                output = json.dumps({"type": "a(ssssssouso)", "data": [[]]})
            elif kind == "invalid":
                output = json.dumps({"type": "s", "data": "private"})
            else:
                return {"exit_code": 9, "output": "", "stderr": errors[kind][0] + " secret"}
        return {"exit_code": 0, "output": output, "stderr": ""}

    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid, pw_dir=str(home)))
    monkeypatch.setattr(process, "run_process", query)
    value = await worker.diagnose(user, uid, home, unit, worker.time.monotonic() + 5)
    assert value["stage"] == calls[-1] == failed_stage
    assert value["code"] == expected_code and value["failure"] == expected_failure
    assert value["query_status"] == (0 if kind in {"absent", "invalid"} else 9)
    assert value["query_code"] == ("ok" if kind in {"absent", "invalid"} else errors[kind][1])
    assert "private" not in json.dumps(value) and "secret" not in json.dumps(value)
    assert ci.startup_facts(
        {
            **value,
            "client_status": 1,
            "client_code": "command_failed",
            "diagnostic_status": 0,
            "diagnostic_code": "ok",
        },
        unit,
    )


async def test_diagnostic_import_failure_remains_distinct_and_does_not_run_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import pwd
    from types import SimpleNamespace

    original = builtins.__import__
    user, uid, unit = "muli-ci-" + "a" * 12, 23145, "muli-native-" + "b" * 32 + ".service"
    home = tmp_path / "volume/home"

    def deny(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "muli.platforms.linux_transaction":
            raise ImportError("private credential and path")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid, pw_dir=str(home)))
    monkeypatch.setattr(builtins, "__import__", deny)
    value = await worker.diagnose(user, uid, home, unit, worker.time.monotonic() + 5)
    assert value["stage"] == "import" and value["failure"] == "import_failed"
    assert value["query_status"] is None and value["query_code"] == "not_started"
    assert "private" not in json.dumps(value)


def test_diagnostic_ancestor_metadata_is_bounded_path_free_and_not_access_proof(
    tmp_path: Path,
) -> None:
    temporary = tmp_path / "runner-temp"
    temporary.mkdir(mode=0o700)
    home = temporary / f"{ci.ROOT_NAME}-muli-ci-{'a' * 12}" / "home"
    value = worker.diagnostic_ancestors(home)
    assert 0 < len(value) <= 16 and value[-1]["mode"] & 0o777 == 0o700
    assert value[-1]["uid"] == os.getuid() and value[-1]["gid"] == os.getgid()
    assert [row["index"] for row in value] == list(range(len(value)))
    assert str(tmp_path) not in json.dumps(value) and temporary.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "error,expected",
    [
        (worker.NativeError("native_prefix_invalid"), "native_prefix_invalid"),
        (RuntimeError("secret-password-body"), "RuntimeError"),
        (worker.NativeError("secret-password-body"), "NativeError"),
    ],
    ids=["fixed-code", "foreign-body", "invalid-native-body"],
)
async def test_worker_exports_fixed_failure_code_without_arbitrary_exception_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected: str,
) -> None:
    async def fail(case: Any) -> None:
        raise error

    monkeypatch.setattr(worker.Case, "drive", fail)
    assert await worker.run("drive", tmp_path) == 1
    result = ci.fixture_result((tmp_path / "result.json").read_bytes())
    assert result["code"] == expected and result["completed"] is False
    assert "secret-password-body" not in (tmp_path / "result.json").read_text()
    assert "secret-password-body" not in capsys.readouterr().err
