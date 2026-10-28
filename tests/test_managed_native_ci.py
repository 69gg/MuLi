"""Pure fixture tests: every native/system command is forbidden or injected."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
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
        self.bootstrap: Callable[..., dict[str, Any]]
        self.auto_group = False
        self.uid = 23145
        self.group = f"/user.slice/user-{self.uid}.slice/user@{self.uid}.service"
        self.manager_runtime = parent / "systemd-run/system"
        self.manager_control_runtime = parent / "systemd-run/system.control"
        self.manager_unit_roots = (
            parent / "systemd-etc/system",
            self.manager_runtime,
            parent / "systemd-etc/system.control",
            self.manager_control_runtime,
        )
        for path in self.manager_unit_roots:
            path.mkdir(parents=True)
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
        if len(values) == 9 and values[3] == "manager-template":
            return ci.Reply(
                0,
                json.dumps(
                    {"code": "verified", "used": 128, "manager": "/usr/lib/systemd/systemd"}
                ).encode(),
            )
        if len(values) == 14 and values[6] == "bootstrap-files":
            assert values[:5] == ("sudo", "--non-interactive", "--", sys.executable, "-I")
            value = self.bootstrap(
                values[7],
                values[8],
                int(values[9]),
                int(values[10]),
                values[11],
                float(values[12]),
                int(values[13]),
            )
            return ci.Reply(0, json.dumps(value).encode())
        if len(values) in {9, 11} and values[3] in {"manager-idle", "manager-ready"}:
            assert values[:2] == (sys.executable, "-I")
            assert values[4:7] == (
                next(iter(self.accounts)),
                str(self.uid),
                self.accounts[values[4]][2],
            )
            value: dict[str, Any] = {"code": "idle", "invocation": "", "used": 128}
            if values[3] == "manager-ready":
                value.update(
                    code="ready",
                    group=self.group,
                    pid=42,
                    invocation="a" * 32,
                    elapsed_ms=0,
                    late_ready=False,
                )
            return ci.Reply(0, json.dumps(value).encode())
        if len(values) == 9 and values[3] == "diagnose-setup":
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
                        "used": 128,
                        "stage": "listing",
                        "query_status": 0,
                        "query_code": "ok",
                        "failure": "none",
                        "unit": f"user@{self.uid}.service",
                        "process": worker.process_unavailable(),
                        "journal": worker.journal_unavailable(),
                        "context": worker.context_unavailable(),
                        "notification": worker.notification_unavailable(),
                        "units": [
                            {"unit": name, "code": "unit_absent"}
                            for name in (
                                f"user-runtime-dir@{self.uid}.service",
                                f"user@{self.uid}.service",
                            )
                        ],
                    }
                ).encode(),
            )
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
        if args[0] == "mkdir":
            assert args[1:3] == ("--mode=0755", "--")
            Path(args[3]).mkdir(mode=0o755)
            return ci.Reply(0)
        if args[0] == "install":
            assert args[1:3] == ("--mode=0644", "--")
            target = Path(args[-1])
            target.write_bytes(Path(args[-2]).read_bytes())
            target.chmod(0o644)
            return ci.Reply(0)
        if args[0] == "rmdir":
            assert args[1] == "--"
            Path(args[2]).rmdir()
            return ci.Reply(0)
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
        if args[:2] == ("systemctl", "revert"):
            assert args[2] == f"user@{self.uid}.service"
            for root in self.manager_unit_roots:
                directory = root / f"{args[2]}.d"
                if directory.exists():
                    for path in directory.iterdir():
                        path.unlink()
                    directory.rmdir()
            return ci.Reply(0)
        if args[0] in {"systemctl", "chown", "chmod"}:
            return ci.Reply(0)
        if "systemctl" in args:
            action = args[args.index("systemctl") :]
            if action[:3] == ("systemctl", "--user", "set-environment"):
                assert args[:5] == ("--user", next(iter(self.accounts)), "--", "env", "-i")
                home = self.accounts[args[1]][2]
                assert len(action) == 5 and set(action[3:]) == {
                    f"XDG_CONFIG_HOME={Path(home) / '.config'}",
                    f"XDG_RUNTIME_DIR=/run/user/{self.uid}",
                }
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
        if path == temporary or any(
            path.is_relative_to(root) for root in commands.manager_unit_roots
        ):
            fields = list(info)
            fields[4] = 0  # Synthetic /tmp ownership; no host account is changed.
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(ci, "VOLUME_PARENT", temporary)
    monkeypatch.setattr(Path, "lstat", root_owned_parent)
    monkeypatch.setattr(ci, "account", commands.accounts.get)
    monkeypatch.setattr(ci, "private_group", commands.groups.get)
    monkeypatch.setattr(ci, "CGROUP_ROOT", commands.cgroup_root)
    monkeypatch.setattr(ci, "MANAGER_RUNTIME", commands.manager_runtime)
    monkeypatch.setattr(ci, "MANAGER_CONTROL_RUNTIME", commands.manager_control_runtime)
    monkeypatch.setattr(ci, "MANAGER_UNIT_ROOTS", commands.manager_unit_roots)

    def bootstrap(
        mode: str, user: str, uid: int, gid: int, manifest: str, deadline: float, limit: int
    ) -> dict[str, Any]:
        import pwd

        original_stat, original_fstat = os.stat, os.fstat

        def virtual_owner(info: os.stat_result) -> os.stat_result:
            values = list(info)
            values[4:6] = [uid, gid] if stat.S_ISDIR(info.st_mode) else [0, 0]
            return os.stat_result(values)

        def virtual_stat(*args: Any, **kwargs: Any) -> os.stat_result:
            return virtual_owner(original_stat(*args, **kwargs))

        def virtual_fstat(fd: int) -> os.stat_result:
            return virtual_owner(original_fstat(fd))

        with monkeypatch.context() as context:
            context.setattr(worker, "BOOTSTRAP_PARENT", temporary)
            context.setattr(
                pwd,
                "getpwnam",
                lambda name: SimpleNamespace(
                    pw_uid=commands.accounts[name][0],
                    pw_gid=commands.accounts[name][1],
                    pw_dir=commands.accounts[name][2],
                ),
            )
            context.setattr(os, "geteuid", lambda: 0)
            context.setattr(os, "fchown", lambda *args: None)
            context.setattr(os, "stat", virtual_stat)
            context.setattr(os, "fstat", virtual_fstat)
            return worker.bootstrap_files(mode, user, uid, gid, manifest, deadline, limit)

    commands.bootstrap = bootstrap
    return ci.NativeCI(tmp_path / ci.ROOT_NAME, run=commands), commands


def private_environment(monkeypatch: pytest.MonkeyPatch, root: Path, uid: int) -> None:
    for key, value in {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "home/.config"),
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }.items():
        monkeypatch.setenv(key, value)


def test_prepare_normalizes_only_the_new_manager_xdg_after_ready_and_owned_checks(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    order: list[str] = []
    dispatched: list[tuple[str, ...]] = []
    original_environment = native.check_manager_environment
    original_bootstrap = native.bootstrap_files
    monkeypatch.setenv("XDG_CONFIG_HOME", "/foreign/config")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/foreign/runtime")

    def check_environment() -> bool:
        result: bool = original_environment()
        if result:
            order.append("environment_verified")
        return result

    def check_bootstrap(mode: str) -> None:
        original_bootstrap(mode)
        if mode == "check":
            order.append("bootstrap_verified")

    def invoke(argv: Any, *, seconds: float = 5) -> Any:
        values = tuple(argv)
        if "systemctl" in values:
            action = values[values.index("systemctl") :]
            if action[:3] == ("systemctl", "--user", "set-environment"):
                receipt = native.receipt
                assert receipt is not None and receipt.uid == commands.uid
                assert values[:7] == (
                    "sudo",
                    "--non-interactive",
                    "--user",
                    receipt.user,
                    "--",
                    "env",
                    "-i",
                )
                assert len(action) == 5 and set(action[3:]) == {
                    f"XDG_CONFIG_HOME={native.home / '.config'}",
                    f"XDG_RUNTIME_DIR=/run/user/{commands.uid}",
                }
                assert "ready" in order
                after_ready = order[order.index("ready") + 1 :]
                assert "environment_verified" in after_ready
                assert "bootstrap_verified" in after_ready
                assert native.manager_limits
                assert not any("systemd-run" in call for call in commands.calls)
                assert 0 < seconds <= ci.CLEANUP_SECONDS
                commands.calls.append(values)
                dispatched.append(action)
                order.append("normalized")
                return ci.Reply(0)
        reply = commands(values, seconds=seconds)
        if len(values) == 11 and values[3] == "manager-ready":
            order.append("ready")
        return reply

    monkeypatch.setattr(native, "check_manager_environment", check_environment)
    monkeypatch.setattr(native, "bootstrap_files", check_bootstrap)
    native.run = invoke
    native.prepare()
    assert len(dispatched) == 1, (
        "READY 后须真实 dispatch 两键 manager normalize，不能只改客户端环境"
    )


def test_execute_inherits_the_dispatched_manager_environment_at_the_original_worker_gate(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    manager_environment: dict[str, str] = {}
    normalizations: list[tuple[str, ...]] = []
    admitted: list[str] = []
    initial_rejections: list[str] = []

    def child_environment() -> dict[str, bool | str]:
        # Only the command runner's synthetic account/manager is substituted.
        # The original worker comparison and rejection implementation runs intact.
        with monkeypatch.context() as context:
            for key, value in manager_environment.items():
                context.setenv(key, value)
            context.setattr(worker.os, "getuid", lambda: commands.uid)
            context.setattr(worker.os, "geteuid", lambda: commands.uid)
            return worker.require_environment(native.volume, commands.uid)

    def invoke(argv: Any, *, seconds: float = 5) -> Any:
        values = tuple(argv)
        if "systemctl" in values:
            action = values[values.index("systemctl") :]
            if action[:3] == ("systemctl", "--user", "set-environment"):
                assert native.receipt is not None
                assert values[:5] == (
                    "sudo",
                    "--non-interactive",
                    "--user",
                    native.receipt.user,
                    "--",
                )
                assert len(action) == 5 and set(action[3:]) == {
                    f"XDG_CONFIG_HOME={native.home / '.config'}",
                    f"XDG_RUNTIME_DIR=/run/user/{commands.uid}",
                }
                assert initial_rejections == ["native_environment_mismatch"]
                assert native.execute_deadline is not None
                assert 0 < seconds <= ci.CLEANUP_SECONDS
                commands.calls.append(values)
                normalizations.append(action)
                # Updating the systemd-run client's env never reaches this branch.
                manager_environment.update(value.split("=", 1) for value in action[3:])
                return ci.Reply(0)
        if "systemd-run" in values:
            facts = child_environment()
            assert all(value is True for key, value in facts.items() if key != "config_source")
            admitted.append(values[-2])
        reply = commands(values, seconds=seconds)
        if "useradd" in values:
            manager_environment.update(
                HOME=str(native.home),
                XDG_CONFIG_HOME="/synthetic-manager/config-other",
                XDG_RUNTIME_DIR="/synthetic-manager/runtime-other",
                DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{commands.uid}/bus",
            )
            with pytest.raises(
                worker.NativeError, match="^native_environment_mismatch$"
            ) as failure:
                child_environment()
            initial_rejections.append(str(failure.value))
        return reply

    native.run = invoke
    native.execute()
    ci.NativeCI(native.root, run=invoke).cleanup()
    assert len(normalizations) == 1
    assert admitted == ["drive", "cleanup"]
    assert not commands.accounts and not commands.mounts


@pytest.mark.parametrize("failure", ["nonzero", "output", "nonzero_output", "lost"])
def test_manager_normalization_failure_keeps_original_budget_and_starts_no_transient(
    fixture: tuple[Any, Commands], failure: str
) -> None:
    native, commands = fixture
    before: list[int] = []

    def invoke(argv: Any, *, seconds: float = 5) -> Any:
        values = tuple(argv)
        if "set-environment" in values:
            before.append(native.setup_bytes)
            commands.calls.append(values)
            if failure == "lost":
                raise ci.FixtureError("native_command_timeout")
            if failure in {"output", "nonzero_output"}:
                return ci.Reply(
                    int(failure == "nonzero_output"),
                    b"x" * (ci.OUTPUT_BYTES - native.setup_bytes),
                )
            return ci.Reply(1, b"fixed failure")
        return commands(values, seconds=seconds)

    native.run = invoke
    expected = {
        "nonzero": "native_command_failed",
        "output": "native_diagnostic_output_budget",
        "nonzero_output": "native_diagnostic_output_budget",
        "lost": "native_command_timeout",
    }[failure]
    with pytest.raises(ci.FixtureError, match=f"^{expected}$"):
        native.prepare()
    assert len(before) == 1
    assert native.setup_bytes == (
        before[0] + len(b"fixed failure") if failure == "nonzero" else ci.OUTPUT_BYTES
    )
    assert not any("systemd-run" in call for call in commands.calls)


def test_manager_normalization_without_remaining_output_never_dispatches(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    ready = False
    original_environment = native.check_manager_environment

    def check_environment() -> bool:
        result: bool = original_environment()
        if ready:
            native.setup_bytes = ci.OUTPUT_BYTES
        return result

    def invoke(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal ready
        values = tuple(argv)
        result = commands(values, seconds=seconds)
        if len(values) == 11 and values[3] == "manager-ready":
            ready = True
        return result

    monkeypatch.setattr(native, "check_manager_environment", check_environment)
    native.run = invoke
    with pytest.raises(ci.FixtureError, match="^native_diagnostic_output_budget$"):
        native.prepare()
    assert not any("set-environment" in call or "systemd-run" in call for call in commands.calls)


def test_manager_normalization_rejects_unconfirmed_environment_before_dispatch(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    ready = False
    original_environment = native.check_manager_environment

    def check_environment() -> bool:
        return False if ready else bool(original_environment())

    def invoke(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal ready
        values = tuple(argv)
        result = commands(values, seconds=seconds)
        if len(values) == 11 and values[3] == "manager-ready":
            ready = True
        return result

    monkeypatch.setattr(native, "check_manager_environment", check_environment)
    native.run = invoke
    with pytest.raises(ci.FixtureError, match="^native_manager_environment_unconfirmed$"):
        native.prepare()
    assert not any("set-environment" in call or "systemd-run" in call for call in commands.calls)


@pytest.mark.parametrize("material", ["environment", "bootstrap", "limits"])
def test_manager_normalization_rechecks_owned_material_after_ready(
    fixture: tuple[Any, Commands], material: str
) -> None:
    native, commands = fixture

    def invoke(argv: Any, *, seconds: float = 5) -> Any:
        values = tuple(argv)
        result = commands(values, seconds=seconds)
        if len(values) == 11 and values[3] == "manager-ready":
            assert native.receipt is not None
            if material == "environment":
                path = native.environment_directory() / ci.ENVIRONMENT_NAME
                data = path.read_bytes()
                path.write_bytes(b"!" + data[1:])
            elif material == "bootstrap":
                path = native.home / ".config/systemd/user" / (native.receipt.user + ".target")
                path.write_bytes(b"!" + path.read_bytes()[1:])
            else:
                path = commands.cgroup_root / commands.group.lstrip("/") / "memory.max"
                path.write_text("max", encoding="ascii")
        return result

    native.run = invoke
    with pytest.raises(ci.FixtureError):
        native.prepare()
    assert not any("set-environment" in call or "systemd-run" in call for call in commands.calls)


def test_bootstrap_target_is_owned_and_verified_before_manager_start(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    receipt = ci.Receipt.read(native.receipt_path)
    target = native.home / ".config/systemd/user" / (receipt.user + ".target")
    assert target.read_bytes() == worker.bootstrap_data()
    assert target.read_text().splitlines() == [
        "[Unit]",
        "Description=MuLi native bootstrap",
        "DefaultDependencies=no",
        "Requires=basic.target dbus.socket",
        "After=basic.target dbus.socket",
    ]
    assert len(receipt.bootstrap_directories) == 5 and receipt.bootstrap_file is not None
    for path in (target.parent, target.parent.parent, target.parent.parent.parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    created = next(i for i, args in enumerate(commands.calls) if "bootstrap-files" in args)
    template = next(i for i, args in enumerate(commands.calls) if "manager-template" in args)
    started = next(i for i, args in enumerate(commands.calls) if "start" in args)
    assert template < created < started
    assert sum("bootstrap-files" in args and "check" in args for args in commands.calls) == 1
    ci.NativeCI(native.root, run=commands).cleanup()
    receipt = ci.Receipt.read(native.receipt_path)
    assert not target.parent.parent.parent.exists()
    assert receipt.bootstrap_directories == [] and receipt.bootstrap_file is None


def test_bootstrap_private_dbus_runtime_is_created_checked_and_removed(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    receipt = ci.Receipt.read(native.receipt_path)
    directory = native.home / ".config/systemd/user/dbus.socket.d"
    leaf = directory / "muli-runtime.conf"
    # Exercise the existing create path before inspecting any new receipt field.
    assert leaf.is_file() and not leaf.is_symlink()
    assert leaf.read_bytes() == (
        f'[Socket]\nEnvironment="XDG_RUNTIME_DIR=/run/user/{receipt.uid}"\n'.encode("ascii")
    )
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o644
    saved = json.loads(native.receipt_path.read_bytes())
    assert saved["bootstrap_socket_directory"] == [directory.stat().st_dev, directory.stat().st_ino]
    assert saved["bootstrap_socket_file"] == [leaf.stat().st_dev, leaf.stat().st_ino]
    assert sorted(path.name for path in directory.iterdir()) == [leaf.name]
    native.bootstrap_files("check")
    assert commands.accounts and commands.mounts
    ci.NativeCI(native.root, run=commands).cleanup()
    assert not directory.exists()
    saved = json.loads(native.receipt_path.read_bytes())
    assert saved["bootstrap_socket_directory"] is None
    assert saved["bootstrap_socket_file"] is None
    assert not commands.accounts and not commands.mounts


@pytest.mark.parametrize("mode", ["check", "remove"])
def test_bootstrap_dbus_runtime_identity_change_preserves_all_owned_leaves(
    fixture: tuple[Any, Commands], mode: str
) -> None:
    native, commands = fixture
    native.prepare()
    assert native.receipt is not None
    user_directory = native.home / ".config/systemd/user"
    directory = user_directory / "dbus.socket.d"
    leaf = directory / "muli-runtime.conf"
    content = f'[Socket]\nEnvironment="XDG_RUNTIME_DIR=/run/user/{native.receipt.uid}"\n'.encode(
        "ascii"
    )
    # The old implementation has no owned drop-in; insert an unconfirmed tree.
    # The candidate creates it, so retaining the old inode proves actual drift.
    if not directory.exists():
        directory.mkdir(mode=0o700)
        leaf.write_bytes(content)
    leaf.rename(directory / "saved-runtime")
    leaf.write_bytes(content)
    target = user_directory / (native.receipt.user + ".target")
    mask = user_directory / "gpg-agent-ssh.socket"
    before = native.receipt_path.read_bytes()
    rejected: str | None = None
    try:
        native.bootstrap_files(mode)
    except ci.FixtureError as error:
        rejected = str(error)
    # A late not-empty error after deleting target/mask is insufficient.
    assert target.is_file() and target.read_bytes() == worker.bootstrap_data()
    assert mask.is_file() and mask.read_bytes() == b""
    assert leaf.read_bytes() == content and (directory / "saved-runtime").is_file()
    assert native.receipt_path.read_bytes() == before
    assert commands.accounts and commands.mounts
    assert rejected == "native_bootstrap_unconfirmed"


def test_bootstrap_private_ssh_socket_mask_is_verified_and_removed(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    directory = native.home / ".config/systemd/user"
    mask = directory / "gpg-agent-ssh.socket"
    assert mask.is_file() and not mask.is_symlink()
    assert mask.read_bytes() == b"" and stat.S_IMODE(mask.stat().st_mode) == 0o644
    saved = json.loads(native.receipt_path.read_bytes())
    assert saved["bootstrap_mask"] == [mask.stat().st_dev, mask.stat().st_ino]
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        [native.receipt.user + ".target", "gpg-agent-ssh.socket", "dbus.socket.d"]
    )
    native.bootstrap_files("check")
    ci.NativeCI(native.root, run=commands).cleanup()
    assert not directory.exists()
    assert json.loads(native.receipt_path.read_bytes())["bootstrap_mask"] is None
    assert not commands.accounts and not commands.mounts


@pytest.mark.parametrize("mode", ["check", "remove"])
@pytest.mark.parametrize(
    "change", ["identity", "content", "symlink", "directory", "directory-symlink"]
)
def test_bootstrap_dbus_runtime_drift_never_deletes_other_owned_leaves(
    fixture: tuple[Any, Commands], mode: str, change: str
) -> None:
    native, commands = fixture
    native.prepare()
    assert native.receipt is not None
    parent = native.home / ".config/systemd/user"
    directory = parent / "dbus.socket.d"
    leaf = directory / "muli-runtime.conf"
    content = leaf.read_bytes()
    if change == "content":
        leaf.write_bytes(content + b"# foreign\n")
    elif change in {"directory", "directory-symlink"}:
        directory.rename(parent / "saved-dbus-directory")
        if change == "directory-symlink":
            directory.symlink_to("saved-dbus-directory", target_is_directory=True)
        else:
            directory.mkdir(mode=0o700)
            leaf.write_bytes(content)
    else:
        leaf.rename(parent / "saved-dbus-runtime")
        if change == "symlink":
            leaf.symlink_to("../saved-dbus-runtime")
        else:
            leaf.write_bytes(content)
    before = native.receipt_path.read_bytes()
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        native.bootstrap_files(mode)
    assert (parent / (native.receipt.user + ".target")).read_bytes() == worker.bootstrap_data()
    assert (parent / "gpg-agent-ssh.socket").read_bytes() == b""
    assert leaf.is_symlink() if change == "symlink" else leaf.is_file()
    assert native.receipt_path.read_bytes() == before
    assert commands.accounts and commands.mounts


@pytest.mark.parametrize("foreign_directory", [False, True])
def test_bootstrap_old_receipt_does_not_own_a_private_dbus_dropin(
    fixture: tuple[Any, Commands], foreign_directory: bool
) -> None:
    native, commands = fixture
    native.prepare()
    assert native.receipt is not None
    parent = native.home / ".config/systemd/user"
    directory = parent / "dbus.socket.d"
    leaf = directory / "muli-runtime.conf"
    content = leaf.read_bytes()
    if not foreign_directory:
        leaf.unlink()
        directory.rmdir()
    saved = json.loads(native.receipt_path.read_bytes())
    del saved["bootstrap_socket_directory"]
    del saved["bootstrap_socket_file"]
    ci.write_json(native.receipt_path, saved)
    restored = ci.Receipt.read(native.receipt_path)
    assert restored.bootstrap_socket_directory is None and restored.bootstrap_socket_file is None
    if foreign_directory:
        before = native.receipt_path.read_bytes()
        with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
            ci.NativeCI(native.root, run=commands).cleanup()
        assert leaf.read_bytes() == content
        assert (parent / (restored.user + ".target")).read_bytes() == worker.bootstrap_data()
        assert native.receipt_path.read_bytes() == before
        assert commands.accounts and commands.mounts
    else:
        ci.NativeCI(native.root, run=commands).cleanup()
        assert not parent.exists() and not commands.accounts and not commands.mounts


@pytest.mark.parametrize("mode", ["check", "remove"])
def test_bootstrap_legacy_manifest_cannot_adopt_the_private_dbus_tree(
    fixture: tuple[Any, Commands], mode: str
) -> None:
    native, commands = fixture
    native.prepare()
    receipt = native.receipt
    manifest = json.dumps(
        {
            "directories": receipt.bootstrap_directories,
            "file": receipt.bootstrap_file,
            "mask": receipt.bootstrap_mask,
        }
    )
    before = native.receipt_path.read_bytes()
    value = commands.bootstrap(
        mode,
        receipt.user,
        receipt.uid,
        receipt.gid,
        manifest,
        worker.time.monotonic() + worker.CLEANUP_SECONDS,
        worker.MAX_BYTES,
    )
    assert value["code"] == "native_bootstrap_unconfirmed"
    parent = native.home / ".config/systemd/user"
    assert (parent / (receipt.user + ".target")).read_bytes() == worker.bootstrap_data()
    assert (parent / "gpg-agent-ssh.socket").is_file()
    assert (parent / "dbus.socket.d/muli-runtime.conf").is_file()
    assert native.receipt_path.read_bytes() == before


def test_bootstrap_private_dbus_body_is_charged_before_accepting_the_complete_reply(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    receipt = native.receipt
    manifest = json.dumps(
        {
            "directories": receipt.bootstrap_directories,
            "file": receipt.bootstrap_file,
            "mask": receipt.bootstrap_mask,
            "socket_directory": receipt.bootstrap_socket_directory,
            "socket_file": receipt.bootstrap_socket_file,
        },
        separators=(",", ":"),
    )
    full = commands.bootstrap(
        "check",
        receipt.user,
        receipt.uid,
        receipt.gid,
        manifest,
        worker.time.monotonic() + worker.CLEANUP_SECONDS,
        worker.MAX_BYTES,
    )
    expected = (
        len(manifest.encode())
        + len(worker.bootstrap_data())
        + len(worker.bootstrap_socket_data(receipt.uid))
    )
    assert full["code"] == "verified" and full["used"] == expected
    limited = commands.bootstrap(
        "check",
        receipt.user,
        receipt.uid,
        receipt.gid,
        manifest,
        worker.time.monotonic() + worker.CLEANUP_SECONDS,
        expected - 1,
    )
    assert limited == {"code": "native_diagnostic_output_budget", "used": expected - 1}
    assert (native.home / ".config/systemd/user/dbus.socket.d/muli-runtime.conf").read_bytes() == (
        worker.bootstrap_socket_data(receipt.uid)
    )


def test_bootstrap_create_never_adopts_an_existing_dbus_runtime_directory(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    original = worker.bootstrap_data

    def foreign_tree() -> bytes:
        directory = native.home / ".config/systemd/user/dbus.socket.d"
        if not directory.exists():
            directory.mkdir(mode=0o700)
            (directory / "muli-runtime.conf").write_bytes(b"foreign socket environment")
        return original()

    monkeypatch.setattr(worker, "bootstrap_data", foreign_tree)
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        native.prepare()
    leaf = native.home / ".config/systemd/user/dbus.socket.d/muli-runtime.conf"
    assert leaf.read_bytes() == b"foreign socket environment"
    receipt = ci.Receipt.read(native.receipt_path)
    assert receipt.bootstrap_socket_directory is None and receipt.bootstrap_socket_file is None
    assert not any("start" in args for args in commands.calls)
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert leaf.read_bytes() == b"foreign socket environment"
    assert commands.accounts and commands.mounts


@pytest.mark.parametrize("mode", ["check", "remove"])
@pytest.mark.parametrize("change", ["identity", "content", "symlink"])
def test_bootstrap_ssh_mask_change_rejects_before_removing_owned_target(
    fixture: tuple[Any, Commands], mode: str, change: str
) -> None:
    native, commands = fixture
    native.prepare()
    directory = native.home / ".config/systemd/user"
    mask = directory / "gpg-agent-ssh.socket"
    target = directory / (native.receipt.user + ".target")
    # On the old collector this creates an untracked leaf. On the candidate it
    # changes the exact leaf recorded by the real bootstrap create operation.
    if mask.exists():
        if change == "content":
            mask.write_bytes(b"foreign nonempty unit")
        else:
            mask.rename(mask.with_name("saved-mask"))
    if change == "identity":
        mask.write_bytes(b"")
    elif change == "content" and not mask.exists():
        mask.write_bytes(b"foreign nonempty unit")
    elif change == "symlink":
        mask.symlink_to(target.name)
    before = native.receipt_path.read_bytes()
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        native.bootstrap_files(mode)
    assert target.read_bytes() == worker.bootstrap_data()
    assert mask.is_symlink() if change == "symlink" else mask.exists()
    assert native.receipt_path.read_bytes() == before
    assert commands.accounts and commands.mounts
    assert not any("revert" in args or "umount" in args for args in commands.calls)


def test_bootstrap_create_never_adopts_existing_ssh_socket_leaf(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    original = worker.bootstrap_data

    def foreign_leaf() -> bytes:
        mask = native.home / ".config/systemd/user/gpg-agent-ssh.socket"
        if not mask.exists():
            mask.write_bytes(b"foreign unit")
        return original()

    monkeypatch.setattr(worker, "bootstrap_data", foreign_leaf)
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        native.prepare()
    mask = native.home / ".config/systemd/user/gpg-agent-ssh.socket"
    assert mask.read_bytes() == b"foreign unit"
    assert not any("start" in args for args in commands.calls)
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert mask.read_bytes() == b"foreign unit" and commands.accounts and commands.mounts


def test_bootstrap_ssh_mask_lost_create_reply_stays_unconfirmed(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture

    def lost(argv: Any, *, seconds: float = 5) -> Any:
        reply = commands(argv, seconds=seconds)
        return ci.Reply(1) if "bootstrap-files" in argv and "create" in argv else reply

    native.run = lost
    with pytest.raises(ci.FixtureError, match="bootstrap_invalid"):
        native.prepare()
    mask = native.home / ".config/systemd/user/gpg-agent-ssh.socket"
    assert mask.is_file() and mask.read_bytes() == b""
    before = native.receipt_path.read_bytes()
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert mask.is_file() and native.receipt_path.read_bytes() == before
    assert commands.accounts and commands.mounts
    assert not any(
        any(effect in args for effect in ("start", "revert", "umount", "userdel", "groupdel"))
        for args in commands.calls
    )


@pytest.mark.parametrize("foreign_mask", [False, True])
def test_bootstrap_legacy_receipt_never_adopts_a_socket_mask(
    fixture: tuple[Any, Commands], foreign_mask: bool
) -> None:
    native, commands = fixture
    native.prepare()
    mask = native.home / ".config/systemd/user/gpg-agent-ssh.socket"
    mask.unlink()
    socket_directory = mask.parent / "dbus.socket.d"
    (socket_directory / "muli-runtime.conf").unlink()
    socket_directory.rmdir()
    saved = json.loads(native.receipt_path.read_bytes())
    del saved["bootstrap_mask"]
    del saved["bootstrap_socket_directory"]
    del saved["bootstrap_socket_file"]
    ci.write_json(native.receipt_path, saved)
    restored = ci.Receipt.read(native.receipt_path)
    assert restored.bootstrap_mask is None
    target = mask.with_name(restored.user + ".target")
    if foreign_mask:
        mask.write_bytes(b"")
        with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
            ci.NativeCI(native.root, run=commands).cleanup()
        assert mask.read_bytes() == b"" and target.read_bytes() == worker.bootstrap_data()
        assert commands.accounts and commands.mounts
    else:
        ci.NativeCI(native.root, run=commands).cleanup()
        assert not mask.parent.exists()
        assert not commands.accounts and not commands.mounts


def test_bootstrap_ssh_mask_lost_remove_reply_rechecks_only_owned_cleanup(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    before = native.receipt_path.read_bytes()

    def lost(argv: Any, *, seconds: float = 5) -> Any:
        reply = commands(argv, seconds=seconds)
        return ci.Reply(1) if "bootstrap-files" in argv and "remove" in argv else reply

    native.run = lost
    with pytest.raises(ci.FixtureError, match="bootstrap_invalid"):
        native.bootstrap_files("remove")
    assert not (native.home / ".config").exists()
    assert native.receipt_path.read_bytes() == before
    ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.accounts and not commands.mounts
    assert ci.Receipt.read(native.receipt_path).bootstrap_mask is None
    assert ci.Receipt.read(native.receipt_path).bootstrap_socket_file is None


def test_bootstrap_worker_legacy_manifest_preserves_original_target_contract(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    mask = native.home / ".config/systemd/user/gpg-agent-ssh.socket"
    mask.unlink()
    socket_directory = mask.parent / "dbus.socket.d"
    (socket_directory / "muli-runtime.conf").unlink()
    socket_directory.rmdir()
    receipt = native.receipt
    manifest = json.dumps(
        {"directories": receipt.bootstrap_directories, "file": receipt.bootstrap_file}
    )
    for mode in ("check", "remove"):
        observed = commands.bootstrap(
            mode,
            receipt.user,
            receipt.uid,
            receipt.gid,
            manifest,
            worker.time.monotonic() + worker.CLEANUP_SECONDS,
            worker.MAX_BYTES,
        )
        assert observed["code"] == "verified" and observed["mask"] is None
    assert not mask.parent.exists()


def test_bootstrap_create_is_the_only_complete_prestart_leaf_observation(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    check_environment = native.check_manager_environment
    rereads: list[tuple[int, int]] = []

    def checked_environment() -> bool:
        before = native.setup_bytes
        verified = check_environment()
        rereads.append((len(commands.calls), native.setup_bytes - before))
        return bool(verified)

    def recorded(argv: Any, *, seconds: float = 5) -> Any:
        if "manager-idle" in argv:
            saved = ci.Receipt.read(native.receipt_path)
            assert len(saved.bootstrap_directories) == 5 and saved.bootstrap_file is not None
            assert saved.environment_file is not None
        return commands(argv, seconds=seconds)

    native.check_manager_environment = checked_environment
    native.run = recorded
    native.prepare()
    stages: list[str] = []
    for args in commands.calls:
        if "manager-template" in args:
            stages.append("template")
        elif "install" in args:
            stages.append("install")
        elif "daemon-reload" in args:
            stages.append("reload")
        elif "bootstrap-files" in args:
            stages.append(args[7])
        elif "manager-idle" in args:
            stages.append("idle")
        elif "start" in args:
            stages.append("start")
        elif "manager-ready" in args:
            stages.append("ready")
        elif "set-environment" in args:
            stages.append("normalize")
    assert stages == [
        "template",
        "install",
        "reload",
        "create",
        "idle",
        "start",
        "ready",
        "check",
        "normalize",
    ]
    install = next(i for i, args in enumerate(commands.calls) if "install" in args)
    reload = next(i for i, args in enumerate(commands.calls) if "daemon-reload" in args)
    ready = next(i for i, args in enumerate(commands.calls) if "manager-ready" in args)
    normalize = next(i for i, args in enumerate(commands.calls) if "set-environment" in args)
    prestart_reads = [item for item in rereads if item[0] <= ready]
    assert len(prestart_reads) == 1 and install < prestart_reads[0][0] <= reload
    # The new write requires its own post-READY ownership check; it does not
    # reintroduce the removed duplicate complete bootstrap check before start.
    normalize_reads = [item for item in rereads if ready < item[0] <= normalize]
    assert len(normalize_reads) == 1
    assert rereads == prestart_reads + normalize_reads
    expected_bytes = len(native.environment_data())
    assert prestart_reads[0][1] == normalize_reads[0][1] == expected_bytes
    assert sum(cost for _, cost in rereads) == 2 * expected_bytes
    assert native.setup_bytes <= ci.OUTPUT_BYTES
    assert native.receipt.bootstrap_file is not None


def test_bootstrap_removed_duplicate_check_has_exact_manifest_body_and_reply_cost(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    receipt = native.receipt
    manifest = json.dumps(
        {
            "directories": receipt.bootstrap_directories,
            "file": receipt.bootstrap_file,
            "mask": receipt.bootstrap_mask,
            "socket_directory": receipt.bootstrap_socket_directory,
            "socket_file": receipt.bootstrap_socket_file,
        },
        separators=(",", ":"),
    )
    before = native.setup_bytes
    output = b""

    def recorded(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal output
        reply = commands(argv, seconds=seconds)
        assert "bootstrap-files" in argv and "check" in argv
        output = reply.output
        return reply

    native.run = recorded
    native.bootstrap_files("check")  # Reproduce only the removed check, on the owned fixture tree.
    inner = (
        len(manifest.encode())
        + len(worker.bootstrap_data())
        + len(worker.bootstrap_socket_data(receipt.uid))
    )
    assert json.loads(output)["used"] == inner
    assert native.setup_bytes - before == inner + len(output)
    assert native.setup_bytes <= ci.OUTPUT_BYTES


@pytest.mark.parametrize("change", ["content", "file", "directory", "symlink"])
def test_bootstrap_changed_identity_or_content_is_preserved(
    fixture: tuple[Any, Commands],
    change: str,
) -> None:
    native, commands = fixture
    native.prepare()
    target = native.home / ".config/systemd/user" / (native.receipt.user + ".target")
    if change == "content":
        target.write_bytes(b"unrelated content")
    elif change == "file":
        target.rename(target.with_suffix(".saved"))
        target.write_bytes(worker.bootstrap_data())
    else:
        target.parent.rename(target.parent.with_name("saved"))
        if change == "symlink":
            target.parent.symlink_to("saved", target_is_directory=True)
        else:
            target.parent.mkdir(mode=0o700)
            target.write_bytes(worker.bootstrap_data())
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        native.bootstrap_files("check")
    assert target.exists()
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert target.exists() and commands.accounts and commands.mounts
    assert not any("revert" in args for args in commands.calls)


def test_bootstrap_cleanup_retains_foreign_entries_and_their_directories(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    target = native.home / ".config/systemd/user" / (native.receipt.user + ".target")
    foreign = target.with_name("foreign.service")
    foreign.write_bytes(b"unrelated")
    with pytest.raises(ci.FixtureError, match="bootstrap_not_empty"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not target.exists() and foreign.read_bytes() == b"unrelated"
    receipt = ci.Receipt.read(native.receipt_path)
    assert len(receipt.bootstrap_directories) == 5 and receipt.bootstrap_file is None
    assert commands.accounts and commands.mounts
    assert not any("revert" in args or "umount" in args for args in commands.calls)


def test_bootstrap_create_lost_reply_never_adopts_or_removes_unconfirmed_tree(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture

    def lost(argv: Any, *, seconds: float = 5) -> Any:
        reply = commands(argv, seconds=seconds)
        return ci.Reply(1) if "bootstrap-files" in argv and "create" in argv else reply

    native.run = lost
    with pytest.raises(ci.FixtureError, match="bootstrap_invalid"):
        native.prepare()
    receipt = ci.Receipt.read(native.receipt_path)
    assert len(receipt.bootstrap_directories) == 2 and receipt.bootstrap_file is None
    assert receipt.bootstrap_socket_directory is None and receipt.bootstrap_socket_file is None
    assert receipt.environment_file is not None
    assert (
        native.environment_directory() / ci.ENVIRONMENT_NAME
    ).read_bytes() == native.environment_data()
    target = native.home / ".config/systemd/user" / (receipt.user + ".target")
    assert target.read_bytes() == worker.bootstrap_data()
    leaf = target.parent / "dbus.socket.d/muli-runtime.conf"
    assert leaf.read_bytes() == worker.bootstrap_socket_data(receipt.uid)
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert target.exists() and commands.accounts and commands.mounts
    assert leaf.is_file()
    assert not any(
        any(effect in args for effect in ("start", "revert", "umount", "userdel", "groupdel"))
        for args in commands.calls
    )


@pytest.mark.parametrize("failure", ["budget", "deadline", "schema"])
def test_bootstrap_fixed_helper_rejects_without_deleting(
    fixture: tuple[Any, Commands],
    failure: str,
) -> None:
    native, commands = fixture
    native.prepare()
    receipt = native.receipt
    manifest = json.dumps(
        {"directories": receipt.bootstrap_directories, "file": receipt.bootstrap_file}
    )
    if failure == "schema":
        manifest = json.dumps(
            {
                "directories": receipt.bootstrap_directories,
                "file": receipt.bootstrap_file,
                "path": "/must-not-be-used",
            }
        )
    value = commands.bootstrap(
        "remove",
        receipt.user,
        receipt.uid,
        receipt.gid,
        manifest,
        worker.time.monotonic() + (-1 if failure == "deadline" else 5),
        1 if failure == "budget" else worker.MAX_BYTES,
    )
    assert value["code"] != "verified" and "directories" not in value
    assert (native.home / ".config/systemd/user" / (receipt.user + ".target")).exists()


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "ex-only",
        "flags",
        "unknown-flags",
        "ignore-failure",
        "argv",
        "multiple",
        "active",
        "drift",
        "budget",
        "deadline",
        "empty",
        "shape",
        "timestamp-bool",
        "timestamp-negative",
        "timestamp-overflow",
        "exec-pid-bool",
        "exec-pid-overflow",
        "exec-code-bool",
        "exec-code-underflow",
        "exec-status-overflow",
        "unit-job-bool",
        "main-pid-bool",
        "after-pid-bool",
        "after-ex-bool",
        "unit-drift",
        "pwd-drift",
    ],
)
async def test_manager_template_is_typed_idle_single_unmodified_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    import pwd

    from muli import process

    user, uid = "muli-ci-" + "a" * 12, 23145
    home = tmp_path / "home"
    image = tmp_path / "systemd"
    image.write_bytes(b"not executable")
    account_reads = 0

    def account(_: str) -> SimpleNamespace:
        nonlocal account_reads
        account_reads += 1
        return SimpleNamespace(
            pw_uid=uid + (change == "pwd-drift" and account_reads > 1), pw_dir=str(home)
        )

    monkeypatch.setattr(pwd, "getpwnam", account)
    monkeypatch.setattr(worker, "system_executables", lambda: {"manager": str(image)})
    extended = [str(image), [str(image), "--user"], [], 1, 2, 3, 4, 5, 0, 0]
    entry = [extended[0], extended[1], False, *extended[3:]]
    if change == "flags":
        extended[2] = ["privileged"]
    if change == "unknown-flags":
        extended[2] = ["fixture-unknown-prefix"]
    if change == "ignore-failure":
        extended[2] = ["ignore-failure"]
    if change == "argv":
        extended[1] = [str(image), "--user", "--unit=foreign.target"]
        entry[1] = extended[1]
    bad_numbers: dict[str, tuple[int, Any]] = {
        "timestamp-bool": (3, True),
        "timestamp-negative": (4, -1),
        "timestamp-overflow": (5, 2**64),
        "exec-pid-bool": (7, False),
        "exec-pid-overflow": (7, 2**32),
        "exec-code-bool": (8, False),
        "exec-code-underflow": (8, -(2**31) - 1),
        "exec-status-overflow": (9, 2**31),
    }
    if change in bad_numbers:
        index, invalid = bad_numbers[change]
        extended[index] = invalid
    if change == "shape":
        extended.pop()
    fields: dict[str, tuple[str, Any]] = {
        "Id": ("s", f"user@{uid}.service"),
        "ActiveState": ("s", "active" if change == "active" else "inactive"),
        "SubState": ("s", "dead"),
        "Job": ("(uo)", [0, "/"]),
        "MainPID": ("u", 0),
        "ExecStart": ("a(sasbttttuii)", [entry] * (2 if change == "multiple" else 1)),
        "ExecStartEx": ("a(sasasttttuii)", [extended] * (2 if change == "multiple" else 1)),
    }
    if change == "ex-only":
        fields.pop("ExecStart")
    if change == "empty":
        fields["ExecStartEx"] = ("a(sasasttttuii)", [])
    if change == "unit-job-bool":
        fields["Job"] = ("(uo)", [False, "/"])
    if change == "main-pid-bool":
        fields["MainPID"] = ("u", False)
    seen: list[list[str]] = []
    charged = 0
    historical_charged = 0

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal charged, historical_charged
        seen.append(argv)
        assert argv[:2] == ["busctl", "--system"] and not merge_stderr
        assert "--auto-start=no" in argv and 0 < timeout <= 5
        if change == "budget":
            return {"exit_code": 0, "output": "x" * (limit + 1), "stderr": ""}
        if "call" in argv:
            assert argv[-3:] == ["LoadUnit", "s", f"user@{uid}.service"]
            data = {"type": "o", "data": ["/org/freedesktop/systemd1/unit/fixture"]}
            output = json.dumps(data)
            historical_output = output
        else:
            names = argv[argv.index("get-property") + 4 :]
            if change == "drift" and len(seen) > 3:
                fields["ExecStartEx"] = ("a(sasasttttuii)", [[*extended[:3], 99, *extended[4:]]])
            if change == "unit-drift" and len(seen) > 3:
                fields["Job"] = ("(uo)", [1, "/job"])
            if change == "after-pid-bool" and len(seen) > 3:
                fields["MainPID"] = ("u", False)
            if change == "after-ex-bool" and len(seen) > 3:
                fields["ExecStartEx"] = ("a(sasasttttuii)", [[*extended[:3], True, *extended[4:]]])
            if "ExecStartEx" in names:
                assert names == ["MainPID", "ExecStartEx"]
            output = "\n".join(
                json.dumps({"type": fields[name][0], "data": fields[name][1]}) for name in names
            )
            historical_output = output
            if "ExecStartEx" in names:
                historical_output = "\n".join(
                    (
                        json.dumps({"type": fields["MainPID"][0], "data": fields["MainPID"][1]}),
                        json.dumps({"type": "a(sasbttttuii)", "data": [entry]}),
                        json.dumps(
                            {"type": fields["ExecStartEx"][0], "data": fields["ExecStartEx"][1]}
                        ),
                    )
                )
        charged += len(output.encode()) + len(b"bounded diagnostic")
        historical_charged += len(historical_output.encode()) + len(b"bounded diagnostic")
        return {"exit_code": 0, "output": output, "stderr": "bounded diagnostic"}

    monkeypatch.setattr(process, "run_process", query)
    deadline = worker.time.monotonic() + (-1 if change == "deadline" else 5)
    if change == "deadline":
        with pytest.raises(worker.NativeError, match="deadline"):
            await worker.manager_template(user, uid, home, deadline, worker.MAX_BYTES)
        assert not seen
    else:
        value = await worker.manager_template(user, uid, home, deadline, worker.MAX_BYTES)
        if change in {"none", "ex-only"}:
            assert value == {"code": "verified", "manager": str(image), "used": charged}
            assert len(seen) == 5
            one_legacy_line = (
                json.dumps({"type": "a(sasbttttuii)", "data": [entry]}) + "\n"
            ).encode()
            # The historical five calls differ only by this duplicated line in each Service reply.
            assert historical_charged - value["used"] == 2 * len(one_legacy_line) > 0
        else:
            assert value == {"code": "native_manager_template", "used": worker.MAX_BYTES}
        assert not any("StartUnit" in args or "systemctl" in args for args in seen)


@pytest.mark.parametrize("change", ["reply", "local-deadline", "outer-budget"])
def test_bootstrap_coordinator_never_accepts_invalid_or_late_reply(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    native, commands = fixture
    native.prepare()
    old = native.receipt.bootstrap_file
    clock = worker.time.monotonic()

    def invalid(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal clock
        reply = commands(argv, seconds=seconds)
        value = json.loads(reply.output)
        if change == "reply":
            value["directories"] = [[True, 1]] * 5
        elif change == "local-deadline":
            clock += 5
        else:
            value["padding"] = "x" * ci.OUTPUT_BYTES
        return ci.Reply(0, json.dumps(value).encode())

    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)
    native.run = invalid
    with pytest.raises(
        ci.FixtureError, match="bootstrap_invalid|bootstrap_deadline|metadata_budget|output_budget"
    ):
        native.bootstrap_files("check")
    assert native.receipt.bootstrap_file == old
    assert (native.home / ".config/systemd/user" / (native.receipt.user + ".target")).exists()


def test_bootstrap_low_remaining_allowance_never_calls_helper(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    commands.calls.clear()
    native.setup_bytes = ci.OUTPUT_BYTES - 1024
    with pytest.raises(ci.FixtureError, match="output_budget"):
        native.bootstrap_files("check")
    assert commands.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("manager_program", "/tmp/../systemd"),
        ("bootstrap_directories", [[True, 1], [2, 3]]),
        ("bootstrap_directories", [[1, 2]]),
        ("bootstrap_file", [True, 1]),
        ("bootstrap_mask", [True, 1]),
        ("bootstrap_mask", [1]),
        ("bootstrap_mask", "not-an-inode"),
        ("bootstrap_socket_directory", [True, 1]),
        ("bootstrap_socket_file", [1]),
        ("bootstrap_socket_file", None),
    ],
)
def test_bootstrap_receipt_rejects_unbounded_or_ambiguous_metadata(
    fixture: tuple[Any, Commands],
    field: str,
    value: object,
) -> None:
    native, _ = fixture
    native.prepare()
    values = asdict(native.receipt)
    values[field] = value
    ci.write_json(native.receipt_path, values)
    with pytest.raises(ci.FixtureError, match="receipt_invalid"):
        ci.Receipt.read(native.receipt_path)


def test_bootstrap_parent_replacement_during_read_is_rejected(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli.platforms import linux_transaction

    native, _ = fixture
    native.prepare()
    original = linux_transaction._read_regular
    directory = native.home / ".config/systemd/user"

    def read(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        value = original(path, limit, directory_fd=directory_fd)
        directory.rename(directory.with_name("saved"))
        directory.mkdir(mode=0o700)
        return value

    monkeypatch.setattr(linux_transaction, "_read_regular", read)
    with pytest.raises(ci.FixtureError, match="bootstrap_unconfirmed"):
        native.bootstrap_files("check")
    assert (directory.with_name("saved") / (native.receipt.user + ".target")).exists()


def test_private_manager_environment_is_confirmed_before_start_and_reclaimed_by_fresh_owner(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    receipt = ci.Receipt.read(native.receipt_path)
    assert receipt.environment_directory is not None and receipt.environment_file is not None
    path = native.environment_directory() / ci.ENVIRONMENT_NAME
    assert path.read_bytes() == native.environment_data()
    assert path.read_text().splitlines() == [
        "[Service]",
        f"MemoryMax={ci.MEMORY}",
        "MemorySwapMax=0",
        "CPUQuota=100%",
        f"TasksMax={ci.TASKS}",
        f'Environment="HOME={native.home}"',
        f'Environment="XDG_CONFIG_HOME={native.home / ".config"}"',
        f'Environment="XDG_RUNTIME_DIR=/run/user/{commands.uid}"',
        f'Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{commands.uid}/bus"',
        "ExecStart=",
        f"ExecStart={receipt.manager_program} --user --unit={receipt.user}.target",
    ]
    installed = next(index for index, values in enumerate(commands.calls) if "install" in values)
    reloaded = next(
        index for index, values in enumerate(commands.calls) if "daemon-reload" in values
    )
    started = next(index for index, values in enumerate(commands.calls) if "start" in values)
    assert installed < reloaded < started
    assert not any("set-property" in values for values in commands.calls)
    assert not (commands.manager_control_runtime / path.parent.name).exists()
    assert native.check_manager_environment()
    ci.NativeCI(native.root, run=commands).cleanup()
    assert not path.exists() and not path.parent.exists()
    receipt = ci.Receipt.read(native.receipt_path)
    assert receipt.environment_directory is None and receipt.environment_file is None


@pytest.mark.parametrize("root_index", range(4))
def test_existing_instance_dropin_is_not_overwritten_or_reverted(
    fixture: tuple[Any, Commands], root_index: int
) -> None:
    native, commands = fixture
    directory = commands.manager_unit_roots[root_index] / f"user@{commands.uid}.service.d"
    directory.mkdir()
    sentinel = directory / "existing.conf"
    sentinel.write_bytes(b"unrelated existing configuration")
    with pytest.raises(ci.FixtureError, match="manager_environment_exists"):
        native.prepare()
    assert native.receipt.environment_directory is None
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="manager_environment_(changed|unconfirmed)"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.calls and sentinel.read_bytes() == b"unrelated existing configuration"


def test_existing_instance_unit_override_does_not_grant_revert_authority(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    override = commands.manager_unit_roots[0] / f"user@{commands.uid}.service"
    override.write_bytes(b"unrelated service override")
    with pytest.raises(ci.FixtureError, match="manager_environment_exists"):
        native.prepare()
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="manager_environment_changed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.calls and override.read_bytes() == b"unrelated service override"


def test_extra_instance_control_dropin_never_grants_revert_authority(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    directory = commands.manager_control_runtime / f"user@{commands.uid}.service.d"
    directory.mkdir()
    foreign = directory / "foreign.conf"
    foreign.write_bytes(b"unrelated resource control")
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="manager_environment_changed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.calls
    assert foreign.read_bytes() == b"unrelated resource control"
    assert commands.accounts and commands.mounts


@pytest.mark.parametrize("effect", ["mkdir", "install"])
def test_manager_environment_lost_return_preserves_unconfirmed_evidence(
    fixture: tuple[Any, Commands], effect: str
) -> None:
    native, commands = fixture

    def lost(argv: Any, *, seconds: float = 5) -> Any:
        reply = commands(argv, seconds=seconds)
        return ci.Reply(1) if effect in argv else reply

    native.run = lost
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.prepare()
    receipt = ci.Receipt.read(native.receipt_path)
    assert (receipt.environment_directory is None) == (effect == "mkdir")
    assert receipt.environment_file is None
    directory = native.environment_directory()
    assert directory.is_dir()
    if effect == "install":
        assert (directory / ci.ENVIRONMENT_NAME).read_bytes() == native.environment_data()
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="manager_environment_unconfirmed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.calls and directory.is_dir()


def test_confirmed_empty_environment_directory_can_be_reclaimed_after_install_never_started(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        if "install" in argv:
            commands.calls.append(tuple(argv))
            return ci.Reply(1)
        return commands(argv, seconds=seconds)

    native.run = fail
    with pytest.raises(ci.FixtureError, match="command_failed"):
        native.prepare()
    assert ci.Receipt.read(native.receipt_path).environment_directory is not None
    directory = native.environment_directory()
    assert not tuple(directory.iterdir())
    ci.NativeCI(native.root, run=commands).cleanup()
    assert not directory.exists() and not commands.accounts and not commands.mounts


@pytest.mark.parametrize("change", ["directory", "content", "extra"])
def test_changed_manager_environment_is_never_reverted_or_used_by_cleanup(
    fixture: tuple[Any, Commands], change: str
) -> None:
    native, commands = fixture
    native.prepare()
    directory = native.environment_directory()
    if change == "directory":
        directory.rename(directory.with_suffix(".old"))
        directory.mkdir(mode=0o755)
        (directory / ci.ENVIRONMENT_NAME).write_bytes(native.environment_data())
    elif change == "content":
        (directory / ci.ENVIRONMENT_NAME).write_bytes(b"[Service]\nEnvironment=HOME=/different\n")
    else:
        (directory / "foreign.conf").write_bytes(b"unrelated")
    commands.calls.clear()
    with pytest.raises(ci.FixtureError, match="manager_environment_changed"):
        ci.NativeCI(native.root, run=commands).cleanup()
    assert not commands.calls and commands.accounts and directory.is_dir()


@pytest.mark.parametrize("source", ["explicit", "default"])
def test_worker_environment_facts_use_the_real_default_service_path_without_exporting_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    from muli.service import UserService

    private_environment(monkeypatch, tmp_path, os.geteuid())
    if source == "default":
        monkeypatch.delenv("XDG_CONFIG_HOME")
    facts = worker.require_environment(tmp_path, os.geteuid())
    assert facts["config_source"] == source
    assert all(value is True for key, value in facts.items() if key != "config_source")
    service = UserService(
        tmp_path / "config.json", tmp_path / "data", executable=tmp_path / "a/bin/python"
    )
    assert service.path == tmp_path / "home/.config/systemd/user/muli.service"
    value = worker.Case(tmp_path).result
    value["resources"]["environment"] = facts
    assert ci.fixture_result(json.dumps(value).encode()) == value
    assert str(tmp_path) not in json.dumps(facts)


@pytest.mark.parametrize(
    "key", ["HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "uid"]
)
def test_worker_environment_mismatch_is_fixed_and_does_not_export_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    uid = os.geteuid()
    private_environment(monkeypatch, tmp_path, uid)
    if key != "uid":
        monkeypatch.setenv(key, "/private-upstream-value")
    facts = worker.environment_facts(tmp_path, uid + 1 if key == "uid" else uid)
    assert False in facts.values() and "/private-upstream-value" not in json.dumps(facts)
    with pytest.raises(worker.NativeError, match="^native_environment_mismatch$"):
        worker.require_environment(tmp_path, uid + 1 if key == "uid" else uid)


@pytest.mark.parametrize("change", ["extra", "text", "source"])
def test_environment_export_rejects_values_instead_of_comparison_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    private_environment(monkeypatch, tmp_path, os.geteuid())
    facts: dict[str, Any] = worker.environment_facts(tmp_path, os.geteuid())
    if change == "extra":
        facts["HOME"] = "private-path"
    elif change == "text":
        facts["home_matches"] = "private-path"
    else:
        facts["config_source"] = {"private": "path"}
    value = worker.Case(tmp_path).result
    value["resources"]["environment"] = facts
    with pytest.raises(ci.FixtureError, match="result_invalid"):
        ci.fixture_result(json.dumps(value).encode())


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
    with pytest.raises(ci.FixtureError, match="volume_directory_changed|bootstrap_unconfirmed"):
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


async def test_actual_case_reuses_bootstrap_directory_before_first_manager_command(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real bootstrap/slot files and Case.drive; no subprocess, socket or service."""
    from test_managed_launcher import _slot

    import muli
    from muli.platforms import filesystem_identity
    from muli.platforms.managed_deployment import SlotIdentity, register_local_slot
    from muli.service import UserService

    native, commands = fixture
    # This path now proves real file ownership as well as directory reuse.
    # Keep the account/manager synthetic, but align its UID with actual temp files.
    commands.uid = os.geteuid()
    commands.group = f"/user.slice/user-{commands.uid}.slice/user@{commands.uid}.service"
    kernel(commands.cgroup_root / commands.group.lstrip("/"), ci.MEMORY, ci.TASKS)
    slots: dict[str, SlotIdentity] = {}

    def copy_slot(name: str, description: dict[str, Any]) -> None:
        assert description == commands.description
        slots[name] = _slot(native.volume, name)

    monkeypatch.setattr(native, "copy_slot", copy_slot)
    native.prepare()  # Executes the real bootstrap create/check and receipt path.
    receipt = ci.Receipt.read(native.receipt_path)
    assert receipt.bootstrap_file is not None and len(receipt.bootstrap_directories) == 5
    unit_directory = native.home / ".config/systemd/user"
    before = unit_directory.stat()
    assert [before.st_dev, before.st_ino] == receipt.bootstrap_directories[-1]
    assert stat.S_IMODE(before.st_mode) == 0o700

    # The existing retained-slot helper embeds the slot name in two tiny files.
    # Match both fixture packages, then use the original registration in drive.
    for name in ("__main__.py", "managed_host.py"):
        (Path(slots["b"].package.root) / name).write_bytes(
            (Path(slots["a"].package.root) / name).read_bytes()
        )
    settings = worker.read_json(native.volume / "fixture.json")
    settings["package_digest"] = register_local_slot(
        native.volume / "a", native.volume / "a/bin/python", Path(slots["a"].package.root)
    ).package.digest
    worker.write_json(native.volume / "fixture.json", settings)
    harness_group = commands.cgroup_root / commands.group.lstrip("/") / settings["harness_unit"]
    kernel(harness_group, worker.HARNESS_MEMORY, worker.HARNESS_TASKS)

    class Listener:
        def __enter__(self) -> Listener:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 0)

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 18765)

    class ManagerStartReached(Exception):
        pass

    def membership(unit: str, uid: int) -> Path:
        assert unit == settings["harness_unit"] and uid == commands.uid
        return harness_group

    def local_filesystems(paths: list[Path]) -> None:
        assert paths == [native.volume]

    seen: list[tuple[str, ...]] = []
    case = worker.Case(native.volume)
    case.drive_deadline = worker.time.monotonic() + ci.HARNESS_SECONDS

    async def command(*argv: str) -> str:
        assert argv == ("systemctl", "--user", "daemon-reload")
        assert case.stage == "manager_start"
        service = UserService(
            native.volume / "config.json",
            native.volume / "data",
            executable=slots["a"].lexical_python,
        )
        assert service.path == unit_directory / "muli.service"
        assert service.path.read_text(encoding="utf-8") == service.render()
        assert stat.S_IMODE(service.path.stat().st_mode) == 0o600
        after = unit_directory.stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert (unit_directory / (receipt.user + ".target")).read_bytes() == worker.bootstrap_data()
        seen.append(argv)
        raise ManagerStartReached

    private_environment(monkeypatch, native.volume, commands.uid)
    monkeypatch.setattr(worker.os, "getuid", lambda: commands.uid)
    monkeypatch.setattr(worker.os, "geteuid", lambda: commands.uid)
    monkeypatch.setattr(worker.sys, "prefix", str(native.volume / "a"))
    monkeypatch.setattr(worker.sys, "executable", str(native.volume / "a/bin/python"))
    monkeypatch.setattr(muli, "__file__", str(Path(slots["a"].package.root) / "__init__.py"))
    monkeypatch.setattr(worker, "membership", membership)
    monkeypatch.setattr(filesystem_identity, "verify_local_filesystems", local_filesystems)
    monkeypatch.setattr(worker.socket, "socket", Listener)
    monkeypatch.setattr(case, "command", command)
    case.note("import")
    with pytest.raises(ManagerStartReached):
        await case.drive()
    assert seen == [("systemctl", "--user", "daemon-reload")]


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
    saved["resources"]["manager_readiness"] = native.manager_readiness
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
    saved["resources"]["manager_readiness"] = native.manager_readiness
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

    private_environment(monkeypatch, tmp_path, os.geteuid())
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
    checks: list[str] = []
    control = SimpleNamespace(check=lambda: checks.append("controller"))
    lease = SimpleNamespace(check=lambda: checks.append("writer"))
    journal = SimpleNamespace(
        locked=lambda: nullcontext(control), writer_lease=lambda: nullcontext(lease)
    )
    removed: list[Path] = []

    def remove(root: Path, *, limit: int, guard: Callable[[], None]) -> None:
        # This test owns the unchanged functional projection oracle. Real birth
        # verification/deletion is covered by test_managed_native_unit_birth.py.
        assert root == tmp_path and limit == worker.MAX_BYTES
        guard()
        removed.append(root)

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
    monkeypatch.setattr(worker, "unit_files", lambda: SimpleNamespace(remove=remove))
    monkeypatch.setattr(linux_transaction, "SystemdProbe", lambda **kwargs: Probe())
    await case.cleanup()
    assert verified == [True]
    assert checks == ["controller", "writer"] and removed == [tmp_path]
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
    with pytest.raises(ci.FixtureError, match="^native_execute_deadline$"):
        native.execute()
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert "startup" not in saved["resources"] and saved["code"] == "worker_not_observed"
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

    def admitted(*, root: bool = False) -> None:
        assert root is False

    async def diagnose(
        name: str, uid: int, home: Path, target: str, deadline: float
    ) -> dict[str, Any]:
        observed.append((name, uid, home, target, deadline))
        return {"unit": target, "code": "unavailable"}

    monkeypatch.setattr(worker, "diagnose", diagnose)
    # Resource verification itself is covered by the dedicated envelope tests.
    monkeypatch.setattr(worker, "require_resource_envelope", admitted)
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
    monkeypatch.setenv(
        "MULI_NATIVE_EXECUTE_DEADLINE", str(worker.time.monotonic() + worker.HARNESS_SECONDS)
    )
    assert await worker.run("drive", tmp_path) == 1
    result = ci.fixture_result((tmp_path / "result.json").read_bytes())
    assert result["code"] == expected and result["completed"] is False
    assert "secret-password-body" not in (tmp_path / "result.json").read_text()
    assert "secret-password-body" not in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["daemon_reload", "manager_start", "manager_show"])
def test_setup_timeout_keeps_exact_before_phase_and_original_failure(
    fixture: tuple[Any, Commands], phase: str
) -> None:
    native, commands = fixture
    suffixes = {
        "daemon_reload": ("systemctl", "daemon-reload"),
        "manager_start": (
            "systemctl",
            "--system",
            "--no-block",
            "start",
            f"user-runtime-dir@{commands.uid}.service",
            f"user@{commands.uid}.service",
        ),
    }
    attempted = 0

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal attempted
        if (phase == "manager_show" and argv[3:4] == ("manager-ready",)) or (
            phase != "manager_show" and tuple(argv[2:]) == suffixes[phase]
        ):
            attempted += 1
            assert ci.Receipt.read(native.receipt_path).setup_phase == f"{phase}_before"
            raise ci.FixtureError("native_command_timeout")
        return commands(argv, seconds=seconds)

    native.run = fail
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.execute()
    receipt = ci.Receipt.read(native.receipt_path)
    assert receipt.setup_phase == f"{phase}_before" and receipt.environment_file is not None
    assert len(receipt.mounts) == 3 and commands.accounts and not receipt.user_removed
    result = ci.fixture_result((native.root / "result.json").read_bytes())
    facts = result["resources"]["setup"]
    assert result["stage"] == "manager_setup" and result["generations"] == []
    assert facts["phase"] == f"{phase}_before" and facts["client_code"] == "timeout"
    if phase == "manager_show":
        assert facts["diagnostic_status"] is None and facts["diagnostic_code"] == "output_budget"
        assert native.setup_bytes == ci.OUTPUT_BYTES
        assert not any("diagnose-setup" in args for args in commands.calls)
    else:
        assert facts["diagnostic_status"] == 0 and facts["failure"] == "none"
    assert attempted == 1 and not any("systemd-run" in args for args in commands.calls)
    if phase != "manager_show":
        diagnostic = next(args for args in commands.calls if "diagnose-setup" in args)
        assert diagnostic[0] == sys.executable and not diagnostic[0].startswith(str(native.volume))


def test_setup_success_persists_each_exact_command_boundary(fixture: tuple[Any, Commands]) -> None:
    native, commands = fixture
    seen: list[str] = []

    def observe(argv: Any, *, seconds: float = 5) -> Any:
        if argv[2:4] in (
            ("systemctl", "daemon-reload"),
            ("systemctl", "--system"),
        ) or argv[3:4] == ("manager-ready",):
            seen.append(ci.Receipt.read(native.receipt_path).setup_phase)
        return commands(argv, seconds=seconds)

    native.run = observe
    native.prepare()
    assert seen == [
        f"{phase}_before" for phase in ("daemon_reload", "manager_start", "manager_show")
    ]
    assert ci.Receipt.read(native.receipt_path).setup_phase == "manager_show_after"


@pytest.mark.parametrize("change", ["body", "extra", "unit", "type"])
def test_setup_diagnostic_rejects_untrusted_reply_without_hiding_timeout(
    fixture: tuple[Any, Commands], change: str
) -> None:
    native, commands = fixture

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        if argv[2:4] == ("systemctl", "daemon-reload"):
            raise ci.FixtureError("native_command_timeout")
        reply = commands(argv, seconds=seconds)
        if "diagnose-setup" not in argv:
            return reply
        if change == "body":
            return ci.Reply(2, b"secret error body")
        value = json.loads(reply.output)
        if change == "extra":
            value["stderr"] = "secret error body"
        elif change == "unit":
            value["units"][0]["unit"] = "user@1.service"
        else:
            value["query_status"] = True
        return ci.Reply(0, json.dumps(value).encode())

    native.run = fail
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.execute()
    raw = (native.root / "result.json").read_bytes()
    facts = ci.fixture_result(raw)["resources"]["setup"]
    assert facts["units"] == [] and facts["diagnostic_code"] != "ok"
    assert b"secret" not in raw and ci.Receipt.read(native.receipt_path).mounts


def test_late_setup_observation_cannot_restart_its_five_second_window(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    clock = 100.0
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal clock
        if argv[2:4] == ("systemctl", "daemon-reload"):
            raise ci.FixtureError("native_command_timeout")
        reply = commands(argv, seconds=seconds)
        if "diagnose-setup" in argv:
            assert float(argv[-2]) == 105.0 and seconds == 5.0
            clock = 105.0
        return reply

    native.run = fail
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.execute()
    facts = ci.fixture_result((native.root / "result.json").read_bytes())["resources"]["setup"]
    assert facts["diagnostic_status"] == 0 and facts["diagnostic_code"] == "timeout"
    assert (
        facts["units"] == []
        and len([args for args in commands.calls if "diagnose-setup" in args]) == 1
    )


def test_setup_evidence_write_failure_does_not_replace_original_timeout(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    original = ci.write_json

    def write(path: Path, value: object) -> None:
        if path == native.root / "result.json":
            raise OSError("secret evidence failure")
        original(path, value)

    def fail(argv: Any, *, seconds: float = 5) -> Any:
        if argv[2:4] == ("systemctl", "daemon-reload"):
            raise ci.FixtureError("native_command_timeout")
        return commands(argv, seconds=seconds)

    monkeypatch.setattr(ci, "write_json", write)
    native.run = fail
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.execute()
    assert ci.Receipt.read(native.receipt_path).setup_phase == "daemon_reload_before"
    assert commands.accounts and not (native.root / "result.json").exists()


@pytest.mark.parametrize("phase", ["secret", []], ids=["unknown", "wrong-type"])
def test_setup_receipt_phase_is_a_strict_fixed_enum(
    fixture: tuple[Any, Commands], phase: object
) -> None:
    native, _ = fixture
    native.prepare()
    data = json.loads(native.receipt_path.read_bytes())
    data["setup_phase"] = phase
    ci.write_json(native.receipt_path, data)
    with pytest.raises(ci.FixtureError, match="^native_receipt_invalid$"):
        ci.Receipt.read(native.receipt_path)


@pytest.mark.parametrize(
    "case",
    [
        "present",
        "absent",
        "bad-list",
        "get-unit",
        "property",
        "bus",
        "budget",
        "deadline",
        "executor",
        "other-image",
        "exe-permission",
        "uid-mismatch",
        "typed-pid-change",
        "typed-group-change",
        "ticks-change",
        "proc-group-change",
        "exe-change",
        "uid-change",
        "state-change",
        "proc-budget",
        "proc-deadline",
        "no-pid",
        "proc-missing",
        "bad-stat",
        "bad-uid",
        "wait-child",
        "wait-ep-poll",
        "wait-epoll",
        "wait-other",
        "wait-suffix",
        "wait-zero",
        "wait-empty",
        "wait-whitespace",
        "wait-nonascii",
        "wait-change",
        "wait-permission",
        "wait-oserror",
        "wait-budget",
        "wait-stderr-budget",
        "wait-deadline",
        "wait-cancel",
        "wait-pid-change",
        "wait-group-change",
        "wait-ticks-change",
        "wait-proc-group-change",
        "wait-exe-change",
        "wait-uid-change",
    ],
)
async def test_setup_system_manager_observation_is_typed_bounded_and_readonly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import pwd
    from types import SimpleNamespace

    from muli import process
    from muli.platforms import linux_transaction

    user, uid, home = "muli-ci-" + "a" * 12, 23145, tmp_path / "private-home"
    names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
    paths = {
        name: f"/org/freedesktop/systemd1/unit/fixture{index}" for index, name in enumerate(names)
    }
    calls: list[list[str]] = []
    clock = 100.0
    proc_calls: list[str] = []
    proc_limits: list[int] = []
    image_calls = 0
    binding_calls = 0
    wait_calls = 0
    wait_limits: list[int] = []
    group = f"/user.slice/{names[-1]}"
    images = {
        "manager": "/usr/lib/systemd/systemd",
        "executor": "/usr/lib/systemd/systemd-executor",
    }
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock)
    monkeypatch.setattr(worker, "system_executables", lambda: images)

    def read_proc(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        nonlocal clock
        assert directory_fd is None and path.parent == Path("/proc/42")
        assert path.name in {"stat", "status", "cgroup"} and 0 < limit < ci.OUTPUT_BYTES
        if proc_limits:
            assert limit < proc_limits[-1]
        proc_limits.append(limit)
        proc_calls.append(path.name)
        if case == "proc-missing":
            raise PermissionError("secret proc body")
        if case == "proc-budget":
            return b"x" * (limit + 1)
        if case == "proc-deadline":
            clock = 105.0
        count = proc_calls.count(path.name)
        if path.name == "stat":
            if case == "bad-stat":
                return b"secret malformed stat"
            state = "R" if case == "state-change" and count == 1 else "S"
            ticks = (
                101
                if (case == "ticks-change" and count > 1)
                or (case == "wait-ticks-change" and count > 2)
                else 100
            )
            return f"42 (secret ) (name) {state} {'0 ' * 18}{ticks}\n".encode()
        if path.name == "status":
            if case == "bad-uid":
                return b"Uid:\ttrue 1 1 1\n"
            value = (
                0
                if case == "uid-mismatch"
                or (case == "uid-change" and count > 1)
                or (case == "wait-uid-change" and count > 2)
                else uid
            )
            return f"Name:\tsecret\nUid:\t{value}\t{value}\t{value}\t{value}\n".encode()
        changed = (case == "proc-group-change" and count > 1) or (
            case == "wait-proc-group-change" and count > 2
        )
        current = "/foreign" if changed else group + "/init.scope"
        return f"0::{current}\n".encode()

    monkeypatch.setattr(linux_transaction, "_read_regular", read_proc)

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal clock, image_calls, binding_calls, wait_calls
        assert not merge_stderr
        assert 0 < timeout <= 5 and 0 < limit <= ci.OUTPUT_BYTES
        calls.append(argv)
        if argv[:4] == ["sudo", "--non-interactive", "--", "readlink"]:
            assert argv == ["sudo", "--non-interactive", "--", "readlink", "--", "/proc/42/exe"]
            image_calls += 1
            if case == "exe-permission":
                return {"exit_code": 1, "output": "", "stderr": "secret permission body"}
            kind = (
                "executor"
                if case == "executor"
                or (case == "exe-change" and image_calls > 1)
                or (case == "wait-exe-change" and image_calls > 2)
                else "manager"
            )
            image = "/private/secret/systemd" if case == "other-image" else images[kind]
            return {"exit_code": 0, "output": image + "\n", "stderr": ""}
        if argv[:4] == ["sudo", "--non-interactive", "--", "cat"]:
            assert argv == ["sudo", "--non-interactive", "--", "cat", "--", "/proc/42/wchan"]
            assert binding_calls == 2 and proc_calls.count("stat") == 2
            if wait_limits:
                assert limit < wait_limits[-1] or (
                    case == "wait-empty" and limit == wait_limits[-1]
                )
            wait_limits.append(limit)
            wait_calls += 1
            if case == "wait-permission":
                return {"exit_code": 1, "output": "", "stderr": "secret permission body"}
            if case == "wait-oserror":
                raise OSError("secret read failure")
            if case == "wait-cancel":
                raise asyncio.CancelledError
            if case == "wait-deadline":
                clock = 105.0
            token = {
                "wait-child": "do_wait\n",
                "wait-ep-poll": "ep_poll",
                "wait-epoll": "do_epoll_wait\n",
                "wait-other": "unknown_private_symbol\n",
                "wait-suffix": "do_wait.isra.0\n",
                "wait-empty": "",
                "wait-whitespace": "do_wait secret\n",
                "wait-nonascii": "\u79d8\u5bc6\n",
            }.get(case, "0\n")
            if case == "wait-change":
                token = "do_wait\n" if wait_calls == 1 else "ep_poll\n"
            if case in {
                "wait-pid-change",
                "wait-group-change",
                "wait-ticks-change",
                "wait-proc-group-change",
                "wait-exe-change",
                "wait-uid-change",
            }:
                token = "do_wait\n"
            return {
                "exit_code": 0,
                "output": "x" * (limit + 1) if case == "wait-budget" else token,
                "stderr": "x" * (limit + 1) if case == "wait-stderr-budget" else "",
            }
        assert argv[:2] == ["busctl", "--system"] and "--user" not in argv
        assert "--auto-start=no" in argv
        assert all(
            command not in argv for command in ("start", "stop", "set-property", "journalctl")
        )
        if case == "bus":
            return {"exit_code": 7, "output": "", "stderr": "Failed to connect to bus secret"}
        if "ListUnitsByPatterns" in argv:
            assert argv[-2:] == list(names)
            rows = (
                []
                if case == "absent"
                else [
                    [name, "", "loaded", "activating", "start", "", path, 1, "start", "/job/1"]
                    for name, path in paths.items()
                ]
            )
            output = (
                json.dumps({"type": "s", "data": "secret"})
                if case == "bad-list"
                else json.dumps(
                    {
                        "type": "a(ssssssouso)",
                        "data": [rows],
                    }
                )
            )
        elif "GetUnit" in argv:
            if case == "get-unit" and argv[-1] == names[-1]:
                return {"exit_code": 6, "output": "", "stderr": "Unit secret not loaded"}
            output = json.dumps({"type": "o", "data": [paths[argv[-1]]]})
        else:
            index = argv.index("get-property")
            fields = argv[index + 4 :]
            if fields == ["Version"]:
                output = json.dumps({"type": "s", "data": "255"})
                if case == "deadline":
                    clock = 105.0
            else:
                name = next(name for name, path in paths.items() if path == argv[index + 2])
                rows = {
                    "Id": ("s", name),
                    # These earlier proc-only cases deliberately provide no
                    # usable invocation; journal cases below exercise that gate.
                    "InvocationID": ("ay", []),
                    "LoadState": ("s", "loaded"),
                    "ActiveState": ("s", "activating"),
                    "SubState": ("s", "start"),
                    "Job": ("(uo)", [11, "/org/freedesktop/systemd1/job/11"]),
                    "Result": ("s", "success"),
                    "ExecMainCode": ("i", 0),
                    "ExecMainStatus": ("i", 0),
                    "MainPID": ("u", 0 if case == "no-pid" else 42),
                    "ControlGroup": ("s", f"/user.slice/{name}"),
                }
                if case == "property":
                    rows["MainPID"] = ("u", True)
                if name == names[-1] and fields == ["MainPID", "ControlGroup"]:
                    binding_calls += 1
                    if binding_calls > 1:
                        if case == "typed-pid-change":
                            rows["MainPID"] = ("u", 43)
                        if case == "typed-group-change":
                            rows["ControlGroup"] = ("s", "/foreign")
                    if binding_calls > 2:
                        if case == "wait-pid-change":
                            rows["MainPID"] = ("u", 43)
                        if case == "wait-group-change":
                            rows["ControlGroup"] = ("s", "/foreign")
                output = "\n".join(
                    json.dumps({"type": rows[field][0], "data": rows[field][1]}) for field in fields
                )
        return {"exit_code": 0, "output": output, "stderr": "x" * 12000 if case == "budget" else ""}

    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid, pw_dir=str(home)))
    monkeypatch.setattr(process, "run_process", query)
    if case == "wait-cancel":
        with pytest.raises(asyncio.CancelledError):
            await worker.diagnose_setup(user, uid, home, 105.0)
        assert wait_calls == 1 and calls[-1][-1] == "/proc/42/wchan"
        return
    value = await worker.diagnose_setup(user, uid, home, 105.0)
    assert "secret" not in json.dumps(value) and len(value["units"]) <= 2
    proc_cases = {
        "present",
        "executor",
        "other-image",
        "exe-permission",
        "uid-mismatch",
        "typed-pid-change",
        "typed-group-change",
        "ticks-change",
        "proc-group-change",
        "exe-change",
        "uid-change",
        "state-change",
        "proc-budget",
        "proc-deadline",
        "no-pid",
        "proc-missing",
        "bad-stat",
        "bad-uid",
    }
    wait_case = case.startswith("wait-")
    if wait_case:
        proc_cases.add(case)
    if case in proc_cases | {"absent"}:
        assert value["failure"] == "none" and len(value["units"]) == 2
        assert [item["unit"] for item in value["units"]] == list(names)
        if case in proc_cases:
            assert all(
                item["pid"] == (0 if case == "no-pid" else 42)
                and item["job"] == 11
                and item["cgroup_present"]
                for item in value["units"]
            )
            observed = value["process"]
            if wait_case or case in {
                "present",
                "executor",
                "other-image",
                "uid-mismatch",
                "state-change",
            }:
                assert observed == {
                    "code": "observed",
                    "exe": {"executor": "executor", "other-image": "other"}.get(case, "manager"),
                    "uid_matches": case != "uid-mismatch",
                    "state": "S",
                    "wait": {
                        "wait-child": "child_wait",
                        "wait-ep-poll": "epoll_wait",
                        "wait-epoll": "epoll_wait",
                        "wait-other": "other",
                        "wait-suffix": "other",
                    }.get(case, "unavailable"),
                }
                assert image_calls >= 2 and proc_calls.count("stat") >= 2
                assert proc_calls.count("status") >= 2
                assert 1 <= wait_calls <= 2
                if not wait_case:
                    assert image_calls == 2 and proc_calls.count("stat") == 2
                    assert proc_calls.count("status") == 2
                if observed["wait"] != "unavailable":
                    assert wait_calls == 2 and binding_calls == 3
                    assert image_calls == 3 and proc_calls.count("stat") == 3
                    assert proc_calls.count("status") == 3
            else:
                code = {
                    "exe-permission": "unavailable",
                    "proc-missing": "unavailable",
                    "no-pid": "pid_absent",
                    "proc-budget": "output_budget",
                    "proc-deadline": "timeout",
                    "bad-stat": "invalid_reply",
                    "bad-uid": "invalid_reply",
                }.get(case, "identity_changed")
                assert observed == {
                    "code": code,
                    "exe": "unavailable",
                    "uid_matches": None,
                    "state": "",
                    "wait": "unavailable",
                }
                assert wait_calls == 0
                if case == "no-pid":
                    assert proc_calls == [] and image_calls == 0
        else:
            assert all(item["code"] == "unit_absent" for item in value["units"]) and len(calls) == 2
    elif case == "get-unit":
        assert value["stage"] == "get_unit" and value["query_code"] == "unit_unavailable"
        assert len(value["units"]) == 1 and value["units"][0]["code"] == "observed"
    elif case == "deadline":
        assert value["failure"] == "deadline" and len(calls) == 1
    else:
        assert value["failure"] in {"invalid_reply", "query_failed"}
        assert value["query_code"] == {"bus": "bus_unavailable", "budget": "output_budget"}.get(
            case, "ok"
        )
    assert ci.setup_facts(
        {
            **value,
            "uid": uid,
            "phase": "manager_start_before",
            "client_code": "timeout",
            "diagnostic_status": 0,
            "diagnostic_code": "ok",
        }
    )


@pytest.mark.parametrize("change", ["user", "account", "deadline"])
async def test_setup_observer_identity_and_deadline_refuse_before_system_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import pwd
    from types import SimpleNamespace

    user, uid, home = "muli-ci-" + "a" * 12, 23145, tmp_path / "home"
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid + 1, pw_dir=str(home))
    )
    deadline = worker.time.monotonic() + 5
    if change == "account":
        value = await worker.diagnose_setup(user, uid, home, deadline)
        assert value["failure"] == "identity_failed" and value["units"] == []
    else:
        with pytest.raises(worker.NativeError):
            await worker.diagnose_setup(
                "root" if change == "user" else user,
                uid,
                home,
                deadline - 6 if change == "deadline" else deadline,
            )


@pytest.mark.parametrize("usage", [False, True])
def test_setup_diagnostic_cli_keeps_separate_readonly_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], usage: bool
) -> None:
    called: list[tuple[str, int, Path, float, int, bool]] = []

    def admitted(*, root: bool = False) -> None:
        assert root is False

    async def observe(
        user: str,
        uid: int,
        home: Path,
        deadline: float,
        byte_limit: int,
        *,
        include_usage: bool,
    ) -> dict[str, Any]:
        called.append((user, uid, home, deadline, byte_limit, include_usage))
        return {"stage": "manager"}

    monkeypatch.setattr(worker, "diagnose_setup", observe)
    monkeypatch.setattr(worker, "require_resource_envelope", admitted)
    monkeypatch.setattr(
        sys,
        "argv",
        ["worker", "diagnose-setup", "muli-ci-" + "a" * 12, "23145", str(tmp_path), "105"]
        + (["1024"] if usage else []),
    )
    assert worker.main() == 0
    assert called == [
        ("muli-ci-" + "a" * 12, 23145, tmp_path, 105.0, 1024 if usage else worker.MAX_BYTES, usage)
    ]
    assert json.loads(capsys.readouterr().out) == {"stage": "manager"}


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "exe-path",
        "unknown-code",
        "uid-type",
        "state-text",
        "unavailable-detail",
        "observed-unavailable",
        "wait-symbol",
        "wait-type",
        "missing-wait",
        "unavailable-wait",
    ],
)
def test_process_diagnostic_export_rejects_noncanonical_or_private_fields(change: str) -> None:
    value: dict[str, Any] = {
        "code": "observed",
        "exe": "manager",
        "uid_matches": True,
        "state": "S",
        "wait": "child_wait",
    }
    assert ci.process_facts(value)
    if change == "extra":
        value["path"] = "/private/secret"
    elif change == "exe-path":
        value["exe"] = "/private/secret/systemd"
    elif change == "unknown-code":
        value["code"] = "private-error-body"
    elif change == "uid-type":
        value["uid_matches"] = 1
    elif change == "state-text":
        value["state"] = "private-state"
    elif change == "unavailable-detail":
        value["code"] = "unavailable"
    elif change == "observed-unavailable":
        value["exe"] = "unavailable"
    elif change == "wait-symbol":
        value["wait"] = "do_wait"
    elif change == "wait-type":
        value["wait"] = []
    elif change == "missing-wait":
        del value["wait"]
    else:
        value.update(worker.process_unavailable())
        value["wait"] = "child_wait"
    assert not ci.process_facts(value)


@pytest.mark.parametrize("change", ["owned", "uid", "group-write", "world-write", "not-regular"])
def test_system_image_classification_requires_actual_fixed_root_owned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from types import SimpleNamespace

    directory = tmp_path / "systemd"
    directory.mkdir()
    paths = {"manager": directory / "systemd", "executor": directory / "systemd-executor"}
    for path in paths.values():
        path.write_bytes(b"not an executable fixture")
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    monkeypatch.setattr(worker, "SYSTEM_EXECUTABLE_ROOTS", (alias, tmp_path / "absent"))
    original = Path.stat

    def info(path: Path, *, follow_symlinks: bool = True) -> Any:
        if path in paths.values():
            mode = stat.S_IFDIR | 0o755 if change == "not-regular" else stat.S_IFREG | 0o755
            mode |= {"group-write": 0o020, "world-write": 0o002}.get(change, 0)
            return SimpleNamespace(st_mode=mode, st_uid=1000 if change == "uid" else 0)
        return original(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", info)
    assert worker.system_executables() == (
        {kind: str(path) for kind, path in paths.items()} if change == "owned" else {}
    )


def journal_row(*, source: str = "user", event: str = "finished") -> dict[str, str]:
    """A tiny v255 record, including JSON's four mandatory metadata fields."""
    value = {
        "__CURSOR": "s=private_cursor",
        "__REALTIME_TIMESTAMP": "123456789",
        "__MONOTONIC_TIMESTAMP": "1000000",
        "_BOOT_ID": "b" * 32,
        "MESSAGE_ID": {
            "finished": "eed00a68ffd84e31882105fd973abdd1",
            "starting": "7d4958e842da4a758f6c1cdc7b36dcc5",
            "started": "39f53479d3a045ac8e11786248231fbf",
            "failed": "be02cf6855d2428ba40df7e9d022f03d",
        }[event],
    }
    if source == "system":
        value.update(_PID="1", _UID="0", UNIT="user@23145.service", INVOCATION_ID="01" * 16)
    else:
        value.update(
            _PID="42",
            _UID="23145",
            _SYSTEMD_UNIT="user@23145.service",
            _SYSTEMD_INVOCATION_ID="01" * 16,
        )
        if event != "finished":
            value["USER_UNIT"] = "basic.target"
    if event != "finished":
        value["JOB_TYPE"] = "start"
        if event != "starting":
            value["JOB_RESULT"] = "done" if event == "started" else "dependency"
    return value


@pytest.mark.parametrize(
    "case",
    [
        "finished",
        "basic-job",
        "system-starting",
        "system-started",
        "system-failed",
        "user-failed",
        "other-unit",
        "empty",
        "wrong-pid",
        "wrong-uid",
        "wrong-unit",
        "wrong-invocation",
        "system-wrong-uid",
        "system-wrong-invocation",
        "system-finished",
        "duplicate",
        "array",
        "extra-message",
        "missing-metadata",
        "invalid-metadata",
        "malformed",
        "missing-job",
        "contradictory-job",
        "bad-then-good",
        "sentinel",
        "sentinel-positive",
        "too-many",
        "bad-invocation",
        "boolean-invocation",
        "changed-invocation",
        "changed-pid",
        "changed-group",
        "changed-ticks",
        "changed-proc-group",
        "permission",
        "oserror",
        "timeout",
        "output-budget",
        "stderr-budget",
        "response-deadline",
        "tail-deadline",
        "cancel",
    ],
)
async def test_setup_journal_uses_one_finite_identity_bound_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import pwd
    from types import SimpleNamespace

    from muli import process
    from muli.platforms import linux_transaction

    async def context(*args: Any, **kwargs: Any) -> tuple[dict[str, str], dict[str, str]]:
        return worker.context_unavailable(), worker.notification_unavailable()

    monkeypatch.setattr(worker, "observe_setup_context", context)

    user, uid, home = "muli-ci-" + "a" * 12, 23145, tmp_path / "home"
    names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
    paths = {
        name: f"/org/freedesktop/systemd1/unit/journal{index}" for index, name in enumerate(names)
    }
    group = f"/user.slice/{names[-1]}"
    clock = 100.0
    calls: list[list[str]] = []
    limits: list[int] = []
    invocations = 0
    journal_calls = 0
    journal_limit = 0
    journal_bytes = 0
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock)
    monkeypatch.setattr(
        worker, "system_executables", lambda: {"manager": "/usr/lib/systemd/systemd"}
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid, pw_dir=str(home)))

    def read_proc(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        assert directory_fd is None and path.parent == Path("/proc/42")
        assert 0 < limit < ci.OUTPUT_BYTES
        if limits:
            assert limit < limits[-1]
        limits.append(limit)
        if path.name == "stat":
            ticks = 101 if journal_calls and case == "changed-ticks" else 100
            return f"42 (private ) name) S {'0 ' * 18}{ticks}\n".encode()
        if path.name == "status":
            return f"Name:\tprivate\nUid:\t{uid} {uid} {uid} {uid}\n".encode()
        assert path.name == "cgroup"
        current = (
            "/foreign" if journal_calls and case == "changed-proc-group" else group + "/init.scope"
        )
        return f"0::{current}\n".encode()

    monkeypatch.setattr(linux_transaction, "_read_regular", read_proc)

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal clock, invocations, journal_calls, journal_limit, journal_bytes
        assert not merge_stderr and 0 < timeout <= 5 and 0 < limit <= ci.OUTPUT_BYTES
        calls.append(argv)
        if argv[:4] == ["sudo", "--non-interactive", "--", "readlink"]:
            assert argv[-2:] == ["--", "/proc/42/exe"]
            return {"exit_code": 0, "output": "/usr/lib/systemd/systemd\n", "stderr": ""}
        if argv[:4] == ["sudo", "--non-interactive", "--", "cat"]:
            assert argv[-2:] == ["--", "/proc/42/wchan"]
            return {"exit_code": 0, "output": "ep_poll\n", "stderr": ""}
        if argv[:4] == ["sudo", "--non-interactive", "--", "journalctl"]:
            journal_calls += 1
            journal_limit = limit
            assert journal_calls == 1 and invocations == 1
            identifiers = [
                "MESSAGE_ID=7d4958e842da4a758f6c1cdc7b36dcc5",
                "MESSAGE_ID=39f53479d3a045ac8e11786248231fbf",
                "MESSAGE_ID=be02cf6855d2428ba40df7e9d022f03d",
                "MESSAGE_ID=eed00a68ffd84e31882105fd973abdd1",
            ]
            assert argv == [
                "sudo",
                "--non-interactive",
                "--",
                "journalctl",
                "--boot=0",
                "--no-pager",
                "--quiet",
                "--output=json",
                "--lines=33",
                "--output-fields=_PID,_UID,_SYSTEMD_UNIT,_SYSTEMD_INVOCATION_ID,UNIT,INVOCATION_ID,USER_UNIT,MESSAGE_ID,JOB_TYPE,JOB_RESULT",
                "_PID=1",
                "_UID=0",
                f"UNIT={names[-1]}",
                "INVOCATION_ID=" + "01" * 16,
                *identifiers,
                "+",
                "_PID=42",
                f"_UID={uid}",
                f"_SYSTEMD_UNIT={names[-1]}",
                "_SYSTEMD_INVOCATION_ID=" + "01" * 16,
                *identifiers,
            ]
            if case == "cancel":
                raise asyncio.CancelledError
            if case == "oserror":
                raise OSError("private journal error")
            if case == "timeout":
                raise TimeoutError("private deadline error")
            if case == "permission":
                return {"exit_code": 1, "output": "", "stderr": "private access error"}
            source = "system" if case.startswith("system-") else "user"
            event = {
                "basic-job": "started",
                "other-unit": "started",
                "missing-job": "started",
                "contradictory-job": "started",
                "user-failed": "failed",
                "system-starting": "starting",
                "system-started": "started",
                "system-failed": "failed",
            }.get(case, "finished")
            row: dict[str, Any] = journal_row(source=source, event=event)
            change = {
                "wrong-pid": ("_PID", "43"),
                "wrong-uid": ("_UID", "0"),
                "wrong-unit": ("_SYSTEMD_UNIT", "user@999.service"),
                "wrong-invocation": ("_SYSTEMD_INVOCATION_ID", "02" * 16),
                "system-wrong-uid": ("_UID", "1"),
                "system-wrong-invocation": ("INVOCATION_ID", "02" * 16),
                "other-unit": ("USER_UNIT", "private-unlisted.service"),
                "array": ("_PID", ["42"]),
                "extra-message": ("MESSAGE", "private body"),
                "invalid-metadata": ("_BOOT_ID", []),
                "contradictory-job": ("JOB_RESULT", "failed"),
            }.get(case)
            if change is not None:
                row[change[0]] = change[1]
            if case in {"missing-metadata", "missing-job"}:
                del row["__CURSOR" if case == "missing-metadata" else "JOB_TYPE"]
            output = json.dumps(row)
            if case == "duplicate":
                output = output.replace('"MESSAGE_ID":', '"MESSAGE_ID":"private","MESSAGE_ID":', 1)
            elif case == "malformed":
                output = "private malformed json"
            elif case == "empty":
                output = ""
            elif case == "bad-then-good":
                output = json.dumps({**row, "_UID": "0"}) + "\n" + output
            elif case in {"sentinel", "sentinel-positive", "too-many"}:
                count = 34 if case == "too-many" else 33
                later = {**row, "_UID": "0"} if case == "sentinel" else row
                output = "\n".join([output] + [json.dumps(later)] * (count - 1))
            elif case == "output-budget":
                output = "x" * (limit + 1)
            stderr = "x" * (limit + 1) if case == "stderr-budget" else ""
            journal_bytes = len(output.encode()) + len(stderr.encode())
            if case == "response-deadline":
                clock = 105.0
            return {"exit_code": 0, "output": output, "stderr": stderr}
        assert argv[:2] == ["busctl", "--system"] and "--auto-start=no" in argv
        if journal_calls and invocations > 0 and case not in {"output-budget", "stderr-budget"}:
            assert limit <= journal_limit - journal_bytes
        if "ListUnitsByPatterns" in argv:
            rows = [
                [name, "", "loaded", "activating", "start", "", paths[name], 1, "start", "/job/1"]
                for name in names
            ]
            output = json.dumps({"type": "a(ssssssouso)", "data": [rows]})
        elif "GetUnit" in argv:
            output = json.dumps({"type": "o", "data": [paths[argv[-1]]]})
        else:
            index = argv.index("get-property")
            fields = argv[index + 4 :]
            if fields == ["Version"]:
                output = json.dumps({"type": "s", "data": "255"})
            else:
                name = next(name for name, path in paths.items() if path == argv[index + 2])
                if "InvocationID" in fields:
                    invocations += 1
                invocation: list[Any] = [
                    2 if invocations > 1 and case == "changed-invocation" else 1
                ] * 16
                if case == "bad-invocation":
                    invocation = []
                elif case == "boolean-invocation":
                    invocation[0] = True
                rows = {
                    "Id": ("s", name),
                    "InvocationID": ("ay", invocation),
                    "LoadState": ("s", "loaded"),
                    "ActiveState": ("s", "activating"),
                    "SubState": ("s", "start"),
                    "Job": ("(uo)", [11, "/org/freedesktop/systemd1/job/11"]),
                    "Result": ("s", "success"),
                    "ExecMainCode": ("i", 0),
                    "ExecMainStatus": ("i", 0),
                    "MainPID": ("u", 43 if invocations > 1 and case == "changed-pid" else 42),
                    "ControlGroup": (
                        "s",
                        "/foreign" if invocations > 1 and case == "changed-group" else group,
                    ),
                }
                if invocations > 1 and case == "tail-deadline":
                    clock = 105.0
                output = "\n".join(
                    json.dumps({"type": rows[field][0], "data": rows[field][1]}) for field in fields
                )
        return {"exit_code": 0, "output": output, "stderr": ""}

    monkeypatch.setattr(process, "run_process", query)
    if case == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await worker.diagnose_setup(user, uid, home, 105.0)
        assert journal_calls == 1
        return
    value = await worker.diagnose_setup(user, uid, home, 105.0)
    assert len(value["units"]) == 2 and value["failure"] == "none"
    assert value["process"] == {
        "code": "observed",
        "exe": "manager",
        "uid_matches": True,
        "state": "S",
        "wait": "epoll_wait",
    }
    observed = value["journal"]
    invalid = {
        "wrong-pid",
        "wrong-uid",
        "wrong-unit",
        "wrong-invocation",
        "system-wrong-uid",
        "system-wrong-invocation",
        "system-finished",
        "duplicate",
        "array",
        "extra-message",
        "missing-metadata",
        "invalid-metadata",
        "malformed",
        "missing-job",
        "contradictory-job",
        "empty",
        "sentinel",
    }
    if case in invalid:
        assert observed["code"] == "no_known_event" and observed["events"] == []
        assert observed["incomplete"] is (case == "sentinel")
    elif case in {"bad-invocation", "boolean-invocation", "too-many"}:
        assert observed == worker.journal_unavailable("invalid_reply")
    elif case.startswith("changed-"):
        assert observed == worker.journal_unavailable("identity_changed")
    elif case in {
        "permission",
        "oserror",
        "timeout",
        "output-budget",
        "stderr-budget",
        "response-deadline",
        "tail-deadline",
    }:
        code = {
            "permission": "command_failed",
            "oserror": "unavailable",
            "timeout": "timeout",
            "output-budget": "output_budget",
            "stderr-budget": "output_budget",
            "response-deadline": "timeout",
            "tail-deadline": "timeout",
        }[case]
        assert observed == worker.journal_unavailable(code)
    else:
        assert observed["code"] == "observed"
        assert observed["incomplete"] is (case == "sentinel-positive")
        assert len(observed["events"]) == (32 if case == "sentinel-positive" else 1)
        event = observed["events"][-1]
        assert event == {
            "source": "system_manager" if case.startswith("system-") else "user_manager",
            "event": {
                "basic-job": "started",
                "other-unit": "started",
                "system-starting": "starting",
                "system-started": "started",
                "system-failed": "failed",
                "user-failed": "failed",
            }.get(case, "user_startup_finished"),
            "unit": "other"
            if case == "other-unit"
            else "basic"
            if case in {"basic-job", "user-failed"}
            else "manager",
            "job_result": "done"
            if case in {"basic-job", "other-unit", "system-started"}
            else "dependency"
            if case in {"system-failed", "user-failed"}
            else "unknown",
        }
    assert journal_calls == (0 if case in {"bad-invocation", "boolean-invocation"} else 1)
    assert all(
        not any(word in argv for word in ("start", "stop", "set-property")) for argv in calls
    )
    assert not any(
        word in json.dumps(observed) for word in ("private", "INVOCATION", "_PID", "__CURSOR")
    )
    assert ci.setup_facts(
        {
            **value,
            "uid": uid,
            "phase": "manager_start_before",
            "client_code": "timeout",
            "diagnostic_status": 0,
            "diagnostic_code": "ok",
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        "raw",
        "unknown-code",
        "incomplete-type",
        "too-many",
        "unknown-source",
        "raw-unit",
        "raw-event",
        "raw-result",
        "false-finished",
        "false-state",
        "failed-done",
        "unavailable-detail",
    ],
)
def test_journal_export_rejects_raw_or_unsupported_claims(change: str) -> None:
    value: dict[str, Any] = {
        "code": "observed",
        "incomplete": False,
        "events": [
            {
                "source": "user_manager",
                "event": "user_startup_finished",
                "unit": "manager",
                "job_result": "unknown",
            }
        ],
    }
    assert ci.journal_facts(value)
    event = value["events"][0]
    if change == "raw":
        value["MESSAGE"] = "private body"
    elif change == "unknown-code":
        value["code"] = "private code"
    elif change == "incomplete-type":
        value["incomplete"] = 1
    elif change == "too-many":
        value["events"] *= 33
    elif change == "unavailable-detail":
        value["code"] = "unavailable"
    elif change == "failed-done":
        event.update(event="failed", unit="basic", job_result="done")
    else:
        key, new = {
            "unknown-source": ("source", "private source"),
            "raw-unit": ("unit", "private.service"),
            "raw-event": ("event", "private event"),
            "raw-result": ("job_result", "private result"),
            "false-finished": ("source", "system_manager"),
            "false-state": ("event", "active"),
        }[change]
        event[key] = new
    assert not ci.journal_facts(value)


def test_execute_does_not_restart_its_budget_after_preparation(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    clock = 100.0
    original_prepare = native.prepare

    def prepare() -> dict[str, Any]:
        nonlocal clock
        result: dict[str, Any] = original_prepare()
        clock += ci.HARNESS_SECONDS + ci.CLEANUP_SECONDS
        return result

    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)
    monkeypatch.setattr(native, "prepare", prepare)
    with pytest.raises(ci.FixtureError, match="^native_execute_deadline$"):
        native.execute()
    assert not any("systemd-run" in argv for argv in commands.calls)


def test_execute_clips_actual_harness_runtime_and_passes_absolute_deadline(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, commands = fixture
    clock = 100.0
    original_prepare = native.prepare

    def prepare() -> dict[str, Any]:
        nonlocal clock
        result: dict[str, Any] = original_prepare()
        clock += 10
        return result

    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)
    monkeypatch.setattr(native, "prepare", prepare)
    native.execute()
    launched = next(argv for argv in commands.calls if "systemd-run" in argv)
    runtime = next(
        float(value.removeprefix("--property=RuntimeMaxSec="))
        for value in launched
        if value.startswith("--property=RuntimeMaxSec=")
    )
    assert runtime == ci.HARNESS_SECONDS - 10
    assert "--setenv=MULI_NATIVE_EXECUTE_DEADLINE=195.0" in launched


async def test_drive_without_coordinator_deadline_never_enters_business(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def drive(self: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.delenv("MULI_NATIVE_EXECUTE_DEADLINE", raising=False)
    monkeypatch.setattr(worker.Case, "drive", drive)
    assert await worker.run("drive", tmp_path) == 1
    assert not called


@pytest.fixture
def manager_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real typed probe and proc parsing, with every command/file/clock injected."""
    import pwd
    from types import SimpleNamespace

    from muli import process
    from muli.platforms import linux_transaction

    uid = 23145
    names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
    paths = {
        name: "/org/freedesktop/systemd1/unit/" + str(index) for index, name in enumerate(names)
    }
    group = f"/user.slice/user-{uid}.slice/{names[-1]}"
    control: dict[str, Any] = {
        "clock": 100.0,
        "polls": 0,
        "calls": [],
        "files": [],
        "limits": [],
        "states": ["active"],
        "overrides": {},
        "absent": False,
        "ticks": 100,
        "uid": uid,
        "home": tmp_path,
        "user": "muli-ci-" + "a" * 12,
        "group": group,
        "read_count": 0,
        "exe": "manager",
        "cancel": False,
        "stderr": "",
    }
    monkeypatch.setattr(worker.time, "monotonic", lambda: control["clock"])
    monkeypatch.setattr(
        pwd, "getpwnam", lambda user: SimpleNamespace(pw_uid=uid, pw_dir=str(tmp_path))
    )
    monkeypatch.setattr(
        worker, "system_executables", lambda: {"manager": "/usr/lib/systemd/systemd"}
    )

    async def sleep(seconds: float) -> None:
        assert 0 < seconds <= worker.CLEANUP_SECONDS
        control["clock"] += seconds

    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    def read(path: Path, limit: int) -> bytes:
        control["files"].append(path)
        assert 0 < limit <= worker.MAX_BYTES
        if control.get("file_error"):
            raise PermissionError("private file error")
        if path.name == "stat":
            if control.get("cross_boundary") and control["polls"] == 1:
                control["clock"] = 105.1
            control["read_count"] += 1
            ticks = control["ticks"] + (
                control.get("change_ticks", False) and control["read_count"] > 1
            )
            return f"42 (private ) name) S {'0 ' * 18}{ticks}\n".encode()
        if path.name == "status":
            value = control.get("process_uid", uid)
            return f"Name:\tprivate\nUid:\t{value}\t{value}\t{value}\t{value}\n".encode()
        if path.name == "cgroup":
            return f"0::{control.get('process_group', group)}/init.scope\n".encode()
        values = {
            "memory.max": str(worker.MEMORY),
            "memory.swap.max": "0",
            "pids.max": str(worker.TASKS),
            "cpu.max": "100000 100000",
            "cgroup.controllers": "cpu memory pids",
        }
        if control.get("bad_kernel"):
            values["memory.max"] = "max"
        return values[path.name].encode()

    monkeypatch.setattr(linux_transaction, "_read_regular", read)

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert not merge_stderr and 0 < timeout <= worker.CLEANUP_SECONDS
        assert 0 < limit <= worker.MAX_BYTES
        control["calls"].append(argv)
        control["limits"].append(limit)
        if control["cancel"]:
            raise asyncio.CancelledError
        if control.get("query_deadline"):
            control["clock"] = 180.0
        if argv[:4] == ["sudo", "--non-interactive", "--", "readlink"]:
            assert argv[-2:] == ["--", "/proc/42/exe"]
            output = (
                "/usr/lib/systemd/systemd\n" if control["exe"] == "manager" else "/private/image\n"
            )
        else:
            assert argv[:2] == ["busctl", "--system"] and "--auto-start=no" in argv
            assert not any(action in argv for action in ("LoadUnit", "start", "stop", "journalctl"))
            if "ListUnitsByPatterns" in argv:
                assert argv[-2:] == list(names)
                control["polls"] += 1
                state = control["states"][min(control["polls"] - 1, len(control["states"]) - 1)]
                idle, active = state == "inactive", state == "active"
                values = (
                    []
                    if control["absent"]
                    else [
                        [
                            name,
                            "",
                            "loaded",
                            state,
                            "dead"
                            if idle
                            else "running"
                            if name == names[-1] and active
                            else "exited"
                            if active
                            else "start",
                            "",
                            paths[name],
                            0 if idle or active else 11,
                            "" if idle or active else "start",
                            "/" if idle or active else "/org/freedesktop/systemd1/job/11",
                        ]
                        for name in names
                    ]
                )
                if values:
                    for key, index in {"LoadState": 2, "ActiveState": 3, "SubState": 4}.items():
                        if key in control["overrides"]:
                            values[-1][index] = control["overrides"][key]
                    if "Job" in control["overrides"]:
                        job = control["overrides"]["Job"]
                        values[-1][7:] = [job[0], "start" if job[0] else "", job[1]]
                    for index, value in control.get("list_row_changes", {}).items():
                        values[-1][index] = value
                    if control.get("list_duplicate"):
                        values[0] = values[-1].copy()
                    if control.get("list_shared_path"):
                        values[0][6] = values[-1][6]
                output = json.dumps({"type": "a(ssssssouso)", "data": [values]})
            elif "GetUnit" in argv:
                path = paths[argv[-1]] + ("_changed" if control.get("changed_path") else "")
                output = json.dumps({"type": "o", "data": [path]})
            else:
                index = argv.index("get-property")
                fields = argv[index + 4 :]
                name = next(name for name, path in paths.items() if path == argv[index + 2])
                is_manager = name == names[-1]
                state = control["states"][min(control["polls"] - 1, len(control["states"]) - 1)]
                idle, active = state == "inactive", state == "active"
                values = {
                    "Id": ("s", name),
                    "LoadState": ("s", "loaded"),
                    "ActiveState": ("s", state),
                    "SubState": (
                        "s",
                        "dead"
                        if idle
                        else (
                            "running" if is_manager and active else "exited" if active else "start"
                        ),
                    ),
                    "InvocationID": ("ay", [0 if idle else 10] * 16),
                    "Job": (
                        "(uo)",
                        [0, "/"] if idle or active else [11, "/org/freedesktop/systemd1/job/11"],
                    ),
                    "Type": ("s", "notify-reload" if is_manager else "oneshot"),
                    "StatusText": ("s", ""),
                    "MainPID": ("u", 42 if is_manager and not idle else 0),
                    "ControlGroup": (
                        "s",
                        (group if is_manager else "/user.slice/" + name) if not idle else "",
                    ),
                    "Result": ("s", "success"),
                    "ExecMainCode": ("i", 0 if is_manager or idle else 1),
                    "ExecMainStatus": ("i", 0),
                }
                if is_manager:
                    for key, value in control["overrides"].items():
                        values[key] = (values[key][0], value)
                    if control.get("change_invocation") and control["polls"] > 1:
                        values["InvocationID"] = ("ay", [11] * 16)
                    if control["polls"] > 1:
                        for key, value in control.get("final_overrides", {}).items():
                            values[key] = (values[key][0], value)
                output = "\n".join(
                    json.dumps({"type": values[field][0], "data": values[field][1]})
                    for field in fields
                )
        if control.get("budget"):
            output = "x" * (limit + 1)
        return {"exit_code": 0, "output": output, "stderr": control["stderr"]}

    monkeypatch.setattr(process, "run_process", query)
    return control


@pytest.mark.parametrize(
    "case", ["absent", "idle", "active", "job", "pid", "group", "failed", "load"]
)
async def test_manager_idle_accepts_only_exact_absence_or_fully_stopped_instance(
    manager_observation: dict[str, Any], case: str
) -> None:
    control = manager_observation
    control["states"] = ["inactive"]
    if case == "absent":
        control["absent"] = True
    elif case in {"active", "failed"}:
        control["states"] = [case]
    elif case in {"job", "pid", "group", "load"}:
        key, value = {
            "job": ("Job", [11, "/org/freedesktop/systemd1/job/11"]),
            "pid": ("MainPID", 42),
            "group": ("ControlGroup", control["group"]),
            "load": ("LoadState", "not-found"),
        }[case]
        control["overrides"][key] = value
    value = await worker.manager_state(
        "manager-idle", control["user"], control["uid"], control["home"], 105.0, worker.MAX_BYTES
    )
    assert (value["code"] == "idle") == (case in {"absent", "idle"})
    assert value["used"] > 0
    if case == "absent":
        assert not any("GetUnit" in call for call in control["calls"])


@pytest.mark.parametrize(
    "case",
    [
        "ready",
        "late",
        "sample-late",
        "cross-boundary",
        "old",
        "changed",
        "ticks",
        "pid",
        "group",
        "type",
        "uid",
        "image",
        "kernel",
        "query-deadline",
        "budget",
        "stderr",
        "pending",
    ],
)
async def test_manager_ready_requires_same_new_instance_process_kernel_and_shared_budget(
    manager_observation: dict[str, Any], case: str
) -> None:
    control = manager_observation
    if case in {"late", "changed"}:
        control["states"] = ["activating"] * 8 + ["active"]
    if case in {"late", "sample-late"}:
        control["clock"] = 106.0
    if case == "cross-boundary":
        control["clock"] = 104.9
        control["states"] = ["activating", "active"]
        control["cross_boundary"] = True
    if case == "changed":
        control["change_invocation"] = True
    if case == "ticks":
        control["change_ticks"] = True
    if case in {"pid", "group", "type", "pending"}:
        key, value = {
            "pid": ("MainPID", 0),
            "group": ("ControlGroup", ""),
            "type": ("Type", "simple"),
            "pending": ("Job", [11, "/org/freedesktop/systemd1/job/11"]),
        }[case]
        control["overrides"][key] = value
    if case == "uid":
        control["process_uid"] = 0
    if case == "image":
        control["exe"] = "other"
    if case == "kernel":
        control["bad_kernel"] = True
    if case == "query-deadline":
        control["query_deadline"] = True
    if case == "budget":
        control["budget"] = True
    if case == "stderr":
        control["stderr"] = "x" * worker.MAX_BYTES
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES,
        started=100.0,
        previous="0a" * 16 if case == "old" else "",
    )
    assert (value["code"] == "ready") == (
        case in {"ready", "late", "sample-late", "cross-boundary"}
    )
    if value["code"] == "ready":
        assert value["late_ready"] is (case == "late")
        assert value["group"] == control["group"] and value["invocation"] == "0a" * 16
    assert all("/wchan" not in argument for call in control["calls"] for argument in call)
    assert all(limit <= worker.MAX_BYTES for limit in control["limits"])
    assert "private" not in json.dumps(value)


async def test_manager_ready_cancel_is_not_converted_to_not_ready(
    manager_observation: dict[str, Any],
) -> None:
    control = manager_observation
    control["cancel"] = True
    with pytest.raises(asyncio.CancelledError):
        await worker.manager_state(
            "manager-ready",
            control["user"],
            control["uid"],
            control["home"],
            180.0,
            worker.MAX_BYTES,
            started=100.0,
        )


async def test_manager_pending_rounds_refresh_identity_and_keep_full_ready_proof(
    manager_observation: dict[str, Any],
) -> None:
    control = manager_observation
    control["states"] = ["activating"] * 3 + ["active"]
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES - 1024,
        started=100.0,
    )
    assert value["code"] == "ready" and value["used"] <= worker.MAX_BYTES - 1024
    rounds: list[list[list[str]]] = []
    for call in control["calls"]:
        if call[0] != "busctl":
            continue
        if "ListUnitsByPatterns" in call:
            rounds.append([])
        rounds[-1].append(call)
    assert len(rounds) == 5  # Three pending, one complete candidate, one complete final read.
    for calls in rounds[:3]:
        assert len(calls) == 3 and not any("GetUnit" in call for call in calls)
        assert [call[call.index("get-property") + 4 :] for call in calls[1:]] == [
            ["Id", "InvocationID"],
            ["MainPID", "ControlGroup"],
        ]
    for calls in rounds[3:]:
        assert sum("GetUnit" in call for call in calls) == 2
        fields = [
            call[call.index("get-property") + 4 :] for call in calls if "get-property" in call
        ]
        assert (
            fields.count(["Id", "LoadState", "ActiveState", "SubState", "Job", "InvocationID"]) == 2
        )
        assert (
            fields.count(
                ["Type", "MainPID", "ControlGroup", "Result", "ExecMainCode", "ExecMainStatus"]
            )
            == 2
        )
    assert control["read_count"] == 7
    assert {path.name for path in control["files"]} >= {
        "status",
        "cgroup",
        "memory.max",
        "memory.swap.max",
        "pids.max",
        "cpu.max",
    }


async def test_manager_pending_same_round_output_is_smaller_than_complete_observation(
    manager_observation: dict[str, Any],
) -> None:
    from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe

    control = manager_observation
    control["states"] = ["activating"]
    facts: dict[str, Any] = {}
    counts: list[tuple[int, int]] = []
    for pending in (False, True):
        control["calls"].clear()
        io = worker._SetupIO(180.0, worker.MAX_BYTES - 1024, facts)
        probe = SystemdProbe(runner=io.query, limits=ProbeLimits(command_bytes=worker.MAX_BYTES))
        units = await worker._manager_units(probe, control["uid"], facts, pending=pending)
        manager = units[f"user@{control['uid']}.service"]
        assert manager["invocation"] == "0a" * 16 and manager["MainPID"] == 42
        counts.append((len(control["calls"]), io.used))
    assert counts[0][0] == 7 and counts[1][0] == 3
    assert 0 < counts[1][1] < counts[0][1] <= worker.MAX_BYTES - 1024


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "other.service"),
        (1, 0),
        (1, "private\ntext"),
        (2, True),
        (2, "bad state"),
        (3, "bad state"),
        (4, "bad state"),
        (5, None),
        (6, "/private"),
        (6, 0),
        (7, True),
        (7, -1),
        (7, 2**31),
        (8, ""),
        (8, 0),
        (9, "/"),
    ],
)
async def test_manager_pending_list_is_strict_before_identity_queries(
    manager_observation: dict[str, Any],
    index: int,
    value: Any,
) -> None:
    control = manager_observation
    control["states"] = ["activating"]
    control["list_row_changes"] = {index: value}
    result = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES,
        started=100.0,
    )
    assert result["code"] == "native_manager_reply"
    assert len(control["calls"]) == 1 and not control["files"]


@pytest.mark.parametrize("case", ["duplicate", "shared-path", "changed-path"])
async def test_manager_list_never_grants_another_unit_path(
    manager_observation: dict[str, Any],
    case: str,
) -> None:
    control = manager_observation
    control[
        {
            "duplicate": "list_duplicate",
            "shared-path": "list_shared_path",
            "changed-path": "changed_path",
        }[case]
    ] = True
    result = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES,
        started=100.0,
    )
    assert result["code"] == (
        "native_manager_changed" if case == "changed-path" else "native_manager_reply"
    )
    assert not control["files"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("Id", "other.service"),
        ("InvocationID", []),
        ("InvocationID", [True] * 16),
        ("MainPID", True),
        ("MainPID", -1),
        ("ControlGroup", "/other.service"),
    ],
)
async def test_manager_pending_identity_remains_strict(
    manager_observation: dict[str, Any],
    key: str,
    value: Any,
) -> None:
    control = manager_observation
    control["states"] = ["activating"]
    control["overrides"][key] = value
    result = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES,
        started=100.0,
    )
    assert result["code"] != "ready" and len(control["calls"]) == 3
    assert not control["files"]


@pytest.mark.parametrize("key", ["Id", "InvocationID", "MainPID", "ControlGroup"])
async def test_manager_pending_rechecks_identity_on_next_round(
    manager_observation: dict[str, Any],
    key: str,
) -> None:
    control = manager_observation
    control["states"] = ["activating"]
    control["final_overrides"] = {
        key: {
            "Id": "other.service",
            "InvocationID": [11] * 16,
            "MainPID": 0,
            "ControlGroup": "/changed" + control["group"],
        }[key]
    }
    result = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES,
        started=100.0,
    )
    assert result["code"] == ("native_manager_reply" if key == "Id" else "native_manager_changed")
    assert control["polls"] == 2 and len(control["calls"]) == 6


@pytest.mark.parametrize("index,value", [(2, "not-found"), (3, "activating"), (4, "exited")])
async def test_manager_ready_requires_same_round_list_and_complete_property_state(
    manager_observation: dict[str, Any],
    index: int,
    value: str,
) -> None:
    control = manager_observation
    control["list_row_changes"] = {index: value}
    result = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES,
        started=100.0,
    )
    assert result["code"] != "ready"


@pytest.mark.parametrize(
    "key,value",
    [
        ("Type", "simple"),
        ("Result", "exit-code"),
        ("ExecMainStatus", 1),
        ("InvocationID", [11] * 16),
        ("MainPID", 43),
    ],
)
async def test_manager_final_observation_still_reads_all_original_properties(
    manager_observation: dict[str, Any],
    key: str,
    value: Any,
) -> None:
    control = manager_observation
    control["final_overrides"] = {key: value}
    result = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        180.0,
        worker.MAX_BYTES,
        started=100.0,
    )
    assert result["code"] == "native_manager_changed" and control["polls"] == 2


def test_no_block_start_is_single_admission_and_lost_ack_cannot_enter_harness(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    starts = 0

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal starts
        value = commands(argv, seconds=seconds)
        if argv[2:6] == ("systemctl", "--system", "--no-block", "start"):
            starts += 1
            assert ci.Receipt.read(native.receipt_path).setup_phase == "manager_start_before"
            raise ci.FixtureError("native_command_timeout")
        return value

    native.run = run
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.execute()
    assert starts == 1 and not any(
        "systemd-run" in argv or "manager-ready" in argv for argv in commands.calls
    )
    assert ci.Receipt.read(native.receipt_path).setup_phase == "manager_start_before"


@pytest.mark.parametrize(
    "change", ["code", "invocation", "pid", "group", "elapsed", "late", "used", "extra"]
)
def test_coordinator_rejects_invalid_ready_receipt_before_harness(
    fixture: tuple[Any, Commands],
    change: str,
) -> None:
    native, commands = fixture

    def run(argv: Any, *, seconds: float = 5) -> Any:
        reply = commands(argv, seconds=seconds)
        if "manager-ready" in argv:
            value = json.loads(reply.output)
            key, item = {
                "code": ("code", []),
                "invocation": ("invocation", "0" * 32),
                "pid": ("pid", True),
                "group": ("group", "/foreign"),
                "elapsed": ("elapsed_ms", -1),
                "late": ("late_ready", "yes"),
                "used": ("used", True),
                "extra": ("private", "secret"),
            }[change]
            value[key] = item
            return ci.Reply(0, json.dumps(value).encode())
        return reply

    native.run = run
    with pytest.raises(ci.FixtureError):
        native.execute()
    assert not any("systemd-run" in argv for argv in commands.calls)
    assert b"secret" not in (native.root / "result.json").read_bytes()


def test_initial_ready_and_diagnostic_share_actual_byte_budget(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    allowances: list[int] = []

    def run(argv: Any, *, seconds: float = 5) -> Any:
        reply = commands(argv, seconds=seconds)
        if "manager-idle" in argv:
            allowances.append(int(argv[8]))
        if "manager-ready" in argv:
            allowances.append(int(argv[8]))
            value = json.loads(reply.output)
            value = {
                "code": "native_manager_failed",
                "used": value["used"],
                "stage": "manager_validation",
                "query_status": 0,
                "query_code": "ok",
            }
            return ci.Reply(0, json.dumps(value).encode())
        if "diagnose-setup" in argv:
            allowances.append(int(argv[-1]))
        return reply

    native.run = run
    with pytest.raises(ci.FixtureError, match="^native_manager_failed$"):
        native.execute()
    assert len(allowances) == 3 and allowances[0] > allowances[1] > allowances[2] > 0
    assert 0 < native.setup_bytes < ci.OUTPUT_BYTES


@pytest.fixture
def manager_transport(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Exercise the real parent transport with fake process/pipe/selector/time only."""
    native, commands = fixture
    native.prepare()
    native.manager_readiness = {}
    state: dict[str, Any] = {
        "native": native,
        "clock": 100.0,
        "offset": 0.1,
        "ready": False,
        "launched": 0,
        "kills": [],
        "closed": False,
    }
    native.execute_deadline = state["clock"] + ci.HARNESS_SECONDS + ci.CLEANUP_SECONDS
    monkeypatch.setattr(ci.time, "monotonic", lambda: state["clock"])

    class Pipe:
        def fileno(self) -> int:
            return 12345

        def close(self) -> None:
            state["closed"] = True

    pipe = Pipe()

    class Process:
        pid = 23456
        stdout = pipe

        def wait(self, *, timeout: float) -> int:
            assert timeout > 0
            return 0

    def popen(argv: Any, **kwargs: Any) -> Process:
        state["launched"] += 1
        assert argv[3] in {"manager-idle", "manager-ready"} and kwargs["start_new_session"] is True
        state["worker_deadline"] = float(argv[7])
        state["publish_at"] = (
            state["parent_deadline"] if state.get("same_deadline") else state["worker_deadline"]
        ) + state["offset"]
        reply = commands(argv)
        value = (
            json.loads(reply.output)
            if state["ready"]
            else {
                "code": "native_deadline_exceeded",
                "stage": "service_properties",
                "query_status": None,
                "query_code": "timeout",
                "used": int(argv[8]),
            }
        )
        value.update(state.get("receipt_change", {}))
        state["chunks"] = [json.dumps(value).encode(), b""]
        return Process()

    class Selector:
        def __init__(self) -> None:
            self.keys: dict[int, Any] = {}

        def __enter__(self) -> Selector:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def register(self, fileobj: Pipe, events: int) -> Any:
            assert fileobj is pipe and events == ci.selectors.EVENT_READ
            key = ci.selectors.SelectorKey(fileobj, fileobj.fileno(), events, None)
            self.keys[key.fd] = key
            return key

        def unregister(self, fileobj: Pipe) -> Any:
            assert fileobj is pipe
            return self.keys.pop(fileobj.fileno())

        def get_map(self) -> dict[int, Any]:
            return self.keys

        def select(self, timeout: float) -> list[tuple[Any, int]]:
            assert timeout > 0
            end = state["clock"] + timeout
            if state["publish_at"] > end:
                state["clock"] = end
                return []
            state["clock"] = max(state["clock"], state["publish_at"])
            return [(self.keys[pipe.fileno()], ci.selectors.EVENT_READ)]

    def read(fd: int, limit: int) -> bytes:
        assert fd == pipe.fileno()
        data: bytes = state["chunks"].pop(0)
        assert len(data) <= limit
        return data

    def blocking(fd: int, enabled: bool) -> None:
        assert fd == pipe.fileno() and enabled is False

    def kill(pid: int, sig: int) -> None:
        assert pid == Process.pid and sig == ci.signal.SIGKILL
        state["kills"].append(pid)

    def run(argv: Any, *, seconds: float = 5) -> Any:
        if argv[3:4] in (("manager-ready",), ("manager-idle",)):
            state["parent_deadline"] = state["clock"] + seconds
            return ci.command(argv, seconds=seconds)
        return commands(argv, seconds=seconds)

    monkeypatch.setattr(ci.subprocess, "Popen", popen)
    monkeypatch.setattr(ci.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(ci.os, "read", read)
    monkeypatch.setattr(ci.os, "set_blocking", blocking)
    monkeypatch.setattr(ci.os, "killpg", kill)
    native.run = run
    return state


@pytest.mark.parametrize("execute_bound", [True, False])
@pytest.mark.parametrize("mode", ["manager-idle", "manager-ready"])
def test_manager_deadline_failure_can_return_inside_original_parent_budget(
    manager_transport: dict[str, Any],
    execute_bound: bool,
    mode: str,
) -> None:
    state = manager_transport
    native = state["native"]
    if not execute_bound:
        native.execute_deadline = None
    expected_parent = 100.0 + (ci.HARNESS_SECONDS if execute_bound else ci.CLEANUP_SECONDS)
    with pytest.raises(ci.FixtureError, match="^native_deadline_exceeded$"):
        native.observe_manager(mode, started=99.0)
    assert state["parent_deadline"] == expected_parent
    assert state["worker_deadline"] == expected_parent - ci.CLEANUP_SECONDS / 2
    assert state["worker_deadline"] < state["clock"] < expected_parent
    value = ci.fixture_result((native.root / "result.json").read_bytes())
    assert value["resources"]["manager_failure"] == native.manager_failure
    assert native.manager_failure["query_code"] == "timeout"
    assert state["launched"] == 1 and state["closed"] and len(state["kills"]) == 1
    assert not native.manager_readiness and 0 < native.setup_bytes <= ci.OUTPUT_BYTES


def test_manager_same_deadline_control_loses_receipt_and_exhausts_unknown_allowance(
    manager_transport: dict[str, Any],
) -> None:
    state = manager_transport
    state["same_deadline"] = True  # The old contract cannot publish until parent expiry.
    native = state["native"]
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.observe_manager("manager-ready", started=99.0)
    assert native.setup_bytes == ci.OUTPUT_BYTES and not native.manager_failure
    assert state["publish_at"] > state["parent_deadline"]
    assert state["closed"] and len(state["kills"]) == 1


@pytest.mark.parametrize("offset", [0.0, 0.1])
@pytest.mark.parametrize("mode", ["manager-idle", "manager-ready"])
def test_manager_reply_window_never_accepts_a_success_receipt(
    manager_transport: dict[str, Any],
    offset: float,
    mode: str,
) -> None:
    state = manager_transport
    state.update(ready=True, offset=offset)
    native = state["native"]
    with pytest.raises(ci.FixtureError, match="^native_execute_deadline$"):
        native.observe_manager(mode, started=99.0)
    assert not native.manager_readiness and not native.manager_failure
    assert state["closed"] and len(state["kills"]) == 1


@pytest.mark.parametrize("mode", ["manager-idle", "manager-ready"])
def test_manager_success_before_observation_deadline_still_requires_original_schema(
    manager_transport: dict[str, Any],
    mode: str,
) -> None:
    state = manager_transport
    state.update(ready=True, offset=-0.1)
    native = state["native"]
    value = native.observe_manager(mode, started=99.0)
    assert value["code"] == ("ready" if mode == "manager-ready" else "idle")
    assert state["clock"] < state["worker_deadline"]
    assert state["parent_deadline"] - state["worker_deadline"] == ci.CLEANUP_SECONDS / 2


@pytest.mark.parametrize("offset", [ci.CLEANUP_SECONDS / 2, ci.CLEANUP_SECONDS])
def test_manager_receipt_at_or_after_parent_deadline_is_not_reusable(
    manager_transport: dict[str, Any],
    offset: float,
) -> None:
    state = manager_transport
    state["offset"] = offset
    native = state["native"]
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.observe_manager("manager-ready", started=99.0)
    assert native.setup_bytes == ci.OUTPUT_BYTES and not native.manager_failure
    assert state["closed"] and len(state["kills"]) == 1


def test_manager_observation_cannot_start_inside_reserved_reply_window(
    manager_transport: dict[str, Any],
) -> None:
    state = manager_transport
    native = state["native"]
    native.execute_deadline = state["clock"] + ci.CLEANUP_SECONDS * 1.5
    with pytest.raises(ci.FixtureError, match="^native_manager_observation_budget$"):
        native.observe_manager("manager-ready", started=99.0)
    assert state["launched"] == 0 and not native.manager_readiness


@pytest.mark.parametrize(
    "change",
    [
        {"used": True},
        {"query_status": True},
        {"stage": "private"},
        {"extra": "private"},
    ],
)
def test_manager_failure_reply_window_keeps_original_strict_schema(
    manager_transport: dict[str, Any],
    change: dict[str, Any],
) -> None:
    state = manager_transport
    state["receipt_change"] = change
    native = state["native"]
    with pytest.raises(ci.FixtureError, match="^native_manager_observation$"):
        native.observe_manager("manager-ready", started=99.0)
    assert native.setup_bytes == ci.OUTPUT_BYTES and not native.manager_failure
    assert not native.manager_readiness


def test_manager_failure_validation_cannot_overrun_parent_deadline(
    manager_transport: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = manager_transport
    native = state["native"]
    original = ci.manager_failure_facts

    def validate(value: dict[str, Any]) -> bool:
        valid: bool = original(value)
        state["clock"] = state["parent_deadline"]
        return valid

    monkeypatch.setattr(ci, "manager_failure_facts", validate)
    with pytest.raises(ci.FixtureError, match="^native_command_timeout$"):
        native.observe_manager("manager-ready", started=99.0)
    assert native.setup_bytes == ci.OUTPUT_BYTES and not native.manager_failure


def test_manager_success_validation_must_finish_before_observation_deadline(
    manager_transport: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = manager_transport
    state.update(ready=True, offset=-0.1)
    native = state["native"]
    original = ci.re.fullmatch

    def match(pattern: str, value: str, *args: Any, **kwargs: Any) -> Any:
        result = original(pattern, value, *args, **kwargs)
        if pattern == r"[a-f0-9]{32}":
            state["clock"] = state["worker_deadline"]
        return result

    monkeypatch.setattr(ci.re, "fullmatch", match)
    with pytest.raises(ci.FixtureError, match="^native_execute_deadline$"):
        native.observe_manager("manager-ready", started=99.0)
    assert not native.manager_readiness and not native.manager_failure


def test_success_exports_only_finite_manager_readiness_metadata(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.execute()
    value = ci.fixture_result((native.root / "result.json").read_bytes())
    assert value["resources"]["manager_readiness"] == {"elapsed_ms": 0, "late_ready": False}
    assert (
        len(
            [
                argv
                for argv in commands.calls
                if argv[2:6] == ("systemctl", "--system", "--no-block", "start")
            ]
        )
        == 1
    )


def test_manager_failure_summary_survives_exhausted_diagnostic_budget(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    summary: dict[str, Any] = {}

    def run(argv: Any, *, seconds: float = 5) -> Any:
        if "manager-ready" in argv:
            summary.update(
                code="native_manager_observation",
                stage="service_properties",
                query_status=1,
                query_code="bus_permission",
                used=int(argv[8]),
            )
            return ci.Reply(0, json.dumps(summary).encode())
        return commands(argv, seconds=seconds)

    native.run = run
    with pytest.raises(ci.FixtureError, match="^native_manager_observation$"):
        native.execute()
    value = ci.fixture_result((native.root / "result.json").read_bytes())
    assert value["resources"]["manager_failure"] == summary
    assert value["resources"]["setup"]["diagnostic_code"] == "output_budget"
    assert not any("diagnose-setup" in argv for argv in commands.calls)
    assert native.setup_bytes <= ci.OUTPUT_BYTES


@pytest.mark.parametrize(
    "change",
    [
        {"used": True},
        {"used": -1},
        {"used": ci.OUTPUT_BYTES - 1023},
        {"query_status": True},
        {"query_status": 2**31},
        {"query_status": -(2**31) - 1},
        {"query_code": "secret"},
        {"stage": "secret"},
        {"code": "ready"},
        {"private": "secret"},
    ],
)
def test_manager_failure_summary_rejects_untrusted_fields(change: dict[str, Any]) -> None:
    summary = {
        "code": "native_manager_observation",
        "used": 0,
        "stage": "list_units",
        "query_status": None,
        "query_code": "not_started",
    } | change
    value = {
        "completed": False,
        "stage": "manager_setup",
        "code": "setup_failed",
        "timeline": [],
        "generations": [],
        "resources": {"manager_failure": summary},
    }
    with pytest.raises(ci.FixtureError, match="native_result_invalid"):
        ci.fixture_result(json.dumps(value).encode())


def test_manager_failure_summary_maximum_enums_fit_existing_reply_reserve() -> None:
    for code in ci.MANAGER_FAILURE_CODES:
        for stage in ci.MANAGER_STAGES:
            for query in ci.COMMAND_CODES:
                summary = {
                    "code": code,
                    "stage": stage,
                    "query_status": -(2**31),
                    "query_code": query,
                    "used": ci.OUTPUT_BYTES - 1024,
                }
                assert ci.manager_failure_facts(summary)
                assert len(json.dumps(summary).encode()) + 1 <= 1024


@pytest.mark.parametrize("failure", ["status", "typed", "timeout", "budget"])
async def test_manager_failure_summary_preserves_same_query_without_retry(
    manager_observation: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from muli import process

    control = manager_observation
    calls = 0

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise TimeoutError("private timeout")
        if failure == "budget":
            return {"exit_code": 0, "output": "x" * (limit + 1), "stderr": ""}
        return {
            "exit_code": 1 if failure == "status" else 0,
            "output": "private invalid typed reply",
            "stderr": "Access denied",
        }

    monkeypatch.setattr(process, "run_process", query)
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=99.0,
    )
    assert ci.manager_failure_facts(value)
    assert value["stage"] == "list_units" and calls == 1
    assert value["query_status"] == (
        1 if failure == "status" else 0 if failure == "typed" else None
    )
    assert value["query_code"] == (
        "permission_denied"
        if failure == "status"
        else "ok"
        if failure == "typed"
        else "timeout"
        if failure == "timeout"
        else "output_budget"
    )
    assert "private" not in json.dumps(value)
    if failure in {"timeout", "budget"}:
        assert value["used"] == worker.MAX_BYTES - 1024


async def test_manager_failure_outer_timeout_marks_pending_query_without_hiding_cancel(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from muli import process

    control = manager_observation

    @asynccontextmanager
    async def timeout_context(seconds: float) -> AsyncIterator[None]:
        try:
            yield
        except asyncio.CancelledError:
            raise TimeoutError from None

    monkeypatch.setattr(worker.asyncio, "timeout", timeout_context)

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr(process, "run_process", query)
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=99.0,
    )
    assert ci.manager_failure_facts(value)
    assert value["code"] == "native_deadline_exceeded"
    assert value["query_status"] is None and value["query_code"] == "timeout"
    assert value["used"] == worker.MAX_BYTES - 1024


@pytest.mark.parametrize("outcome", ["cancel", "returned-deadline"])
async def test_setup_command_accounts_for_unknown_or_both_returned_pipes(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    from muli import process

    clock = 100.0
    cancellation = asyncio.CancelledError()

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal clock
        if outcome == "cancel":
            raise cancellation
        clock = 106.0
        return {"exit_code": 0, "output": "out", "stderr": "error"}

    monkeypatch.setattr(process, "run_process", query)
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock)
    facts: dict[str, Any] = {"query_status": 0, "query_code": "ok"}
    io = worker._SetupIO(105.0, 100, facts)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError) as caught:
            await io.command(["fake-proc-query"], timeout=5, limit=100)
        assert caught.value is cancellation and io.used == io.limit
    else:
        with pytest.raises(worker.NativeError, match="native_deadline_exceeded"):
            await io.command(["fake-proc-query"], timeout=5, limit=100)
        assert io.used == len(b"outerror")


@pytest.mark.parametrize("kind", ["output_limit", "value", "os"])
async def test_setup_query_classifies_output_limit_by_type_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from muli import process

    error = {
        "output_limit": process.ProcessOutputLimitError("private sentinel"),
        "value": ValueError("命令输出超过限制，已停止"),
        "os": OSError("命令输出超过限制，已停止"),
    }[kind]
    calls = 0

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(process, "run_process", query)
    facts: dict[str, Any] = {"manager_stage": "list_units"}
    io = worker._SetupIO(worker.time.monotonic() + worker.CLEANUP_SECONDS, worker.MAX_BYTES, facts)
    with pytest.raises(type(error)) as caught:
        await io.query(
            ["busctl", "--user", "ListUnitsByPatterns"], timeout=5, limit=100, merge_stderr=False
        )
    assert caught.value is error and calls == 1
    assert io.used == io.limit and facts["query_status"] is None
    assert facts["query_code"] == ("output_budget" if kind == "output_limit" else "command_failed")


def test_manager_failure_saved_before_optional_diagnostic_or_write_failure(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, commands = fixture
    summary = {
        "code": "native_manager_failed",
        "used": 0,
        "stage": "manager_validation",
        "query_status": 0,
        "query_code": "ok",
    }

    def run(argv: Any, *, seconds: float = 5) -> Any:
        if "manager-ready" in argv:
            return ci.Reply(0, json.dumps(summary).encode())
        return commands(argv, seconds=seconds)

    def diagnose(error: Exception) -> None:
        saved = ci.fixture_result((native.root / "result.json").read_bytes())
        assert saved["resources"]["manager_failure"] == summary
        raise OSError("private diagnostic failure")

    native.run = run
    monkeypatch.setattr(native, "observe_setup", diagnose)
    with pytest.raises(ci.FixtureError, match="^native_manager_failed$"):
        native.execute()


@pytest.mark.parametrize("corrupt", ["used", "reply", "extra"])
def test_manager_failure_invalid_wire_summary_cannot_reuse_allowance_or_leak(
    fixture: tuple[Any, Commands],
    corrupt: str,
) -> None:
    native, commands = fixture

    def run(argv: Any, *, seconds: float = 5) -> Any:
        if "manager-ready" in argv:
            summary = {
                "code": "native_manager_failed",
                "used": 0,
                "stage": "manager_validation",
                "query_status": 0,
                "query_code": "ok",
            }
            if corrupt == "used":
                summary["used"] = int(argv[8]) + 1
            else:
                summary["private"] = "secret" * (300 if corrupt == "reply" else 1)
            return ci.Reply(0, json.dumps(summary).encode())
        return commands(argv, seconds=seconds)

    native.run = run
    with pytest.raises(ci.FixtureError, match="^native_manager_observation$"):
        native.execute()
    raw = (native.root / "result.json").read_bytes()
    assert "manager_failure" not in ci.fixture_result(raw)["resources"]
    assert b"secret" not in raw
    assert native.setup_bytes == ci.OUTPUT_BYTES
    assert not any("diagnose-setup" in argv for argv in commands.calls)


def test_manager_failure_evidence_write_error_keeps_original_failure(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, commands = fixture

    def run(argv: Any, *, seconds: float = 5) -> Any:
        if "manager-ready" in argv:
            return ci.Reply(
                0,
                json.dumps(
                    {
                        "code": "native_manager_failed",
                        "used": 0,
                        "stage": "manager_validation",
                        "query_status": 0,
                        "query_code": "ok",
                    }
                ).encode(),
            )
        return commands(argv, seconds=seconds)

    def cannot_save(resources: dict[str, Any]) -> None:
        raise OSError("private write error")

    native.run = run
    monkeypatch.setattr(native, "save_setup_result", cannot_save)
    with pytest.raises(ci.FixtureError, match="^native_manager_failed$"):
        native.execute()
    assert native.manager_failure["code"] == "native_manager_failed"


@pytest.mark.parametrize("spent", [30.0, 95.0], ids=["case-margin", "execute-deadline"])
def test_preparation_cost_cannot_launch_a_case_with_insufficient_margin(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
    spent: float,
) -> None:
    native, commands = fixture
    clock = 100.0
    original = native.prepare

    def prepare() -> dict[str, Any]:
        nonlocal clock
        value: dict[str, Any] = original()
        clock += spent
        return value

    monkeypatch.setattr(ci.time, "monotonic", lambda: clock)
    monkeypatch.setattr(native, "prepare", prepare)
    with pytest.raises(ci.FixtureError):
        native.execute()
    assert not any("systemd-run" in argv for argv in commands.calls)


@pytest.mark.parametrize("left", [0.0, 60.0, 60.01])
def test_worker_rechecks_business_margin_without_resetting_case_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    left: float,
) -> None:
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    case = worker.Case(tmp_path, seconds=worker.CASE_SECONDS)
    case.drive_deadline = 100 + left
    if left > worker.CASE_SECONDS:
        case.require_start_time()
    else:
        with pytest.raises(worker.NativeError):
            case.require_start_time()
    assert case.deadline == 160.0


def test_result_collection_failure_preserves_first_client_error(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, commands = fixture
    commands.fail_case = True

    def fail() -> None:
        raise OSError("private collection body")

    monkeypatch.setattr(native, "collect_result", fail)
    with pytest.raises(ci.FixtureError, match="^native_command_failed$"):
        native.execute()


def _safe_worker_failure_line() -> bytes:
    diagnostic = {
        "schema_version": 1,
        "scope": "muli_package_only",
        "exception_type": "unknown",
        "frames": [{"module": "muli.platforms.systemd", "function": "Probe.read", "line": 42}],
        "truncated": False,
    }
    return b"native_failure_diagnostic:" + json.dumps(diagnostic).encode("utf-8")


def test_failed_client_projects_only_validated_safe_worker_frames(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    native, commands = fixture
    commands.fail_case = True
    original = native.call

    def call(argv: Any, *, seconds: float = 5, output_bytes: int | None = None) -> Any:
        reply = original(argv, seconds=seconds, output_bytes=output_bytes)
        if argv[-2] == "drive":
            return ci.Reply(
                reply.code, b"private/path secret\n" + _safe_worker_failure_line() + b"\n"
            )
        return reply

    monkeypatch.setattr(native, "call", call)
    with pytest.raises(ci.FixtureError, match="^native_command_failed$"):
        native.execute()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip().encode("utf-8") == _safe_worker_failure_line().replace(b" ", b"")
    assert "private" not in captured.err and "secret" not in captured.err
    assert json.loads((native.root / "result.json").read_text())["completed"] is False


@pytest.mark.parametrize(
    "change",
    ["absent", "duplicate", "utf8", "json", "keys", "private", "large", "budget", "extractor"],
)
def test_worker_failure_projection_rejects_untrusted_or_unavailable_diagnostics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    valid = _safe_worker_failure_line()
    prefix = b"native_failure_diagnostic:"
    data = {
        "absent": b"private arbitrary stderr\n",
        "duplicate": valid + b"\n" + valid,
        "utf8": prefix + b"\xff",
        "json": prefix + b"{",
        "keys": valid.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
        "private": valid.replace(b'"frames": [', b'"path": "/private/secret", "frames": ['),
        "large": prefix + b" " * 4097,
        "budget": b"x" * (ci.OUTPUT_BYTES + 1),
        "extractor": valid,
    }[change]
    if change == "extractor":

        def fail() -> NoReturn:
            raise ValueError("private extractor failure")

        monkeypatch.setattr(ci, "failure_diagnostic_module", fail)
    ci.project_worker_failure(data)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "elapsed-bool",
        "elapsed-negative",
        "elapsed-large",
        "late",
        "extra",
        "mode",
        "symlink",
        "budget",
    ],
)
def test_fresh_cleanup_preserves_only_valid_owned_finite_readiness(
    fixture: tuple[Any, Commands],
    change: str,
) -> None:
    native, commands = fixture
    native.prepare()
    value = worker.Case(native.volume).result
    value.update(completed=True, stage="complete")
    worker.write_json(native.volume / "result.json", value)
    readiness: dict[str, Any] = {"elapsed_ms": 6001, "late_ready": True}
    failure = {
        "code": "native_manager_observation",
        "stage": "list_units",
        "used": ci.OUTPUT_BYTES - 1024,
        "query_status": None,
        "query_code": "timeout",
    }
    saved = {**value, "resources": {"manager_readiness": readiness, "manager_failure": failure}}
    if change.startswith("elapsed-"):
        readiness["elapsed_ms"] = {
            "elapsed-bool": True,
            "elapsed-negative": -1,
            "elapsed-large": 95001,
        }[change]
    elif change == "late":
        readiness["late_ready"] = "private"
    elif change == "extra":
        readiness["private"] = "secret"
    path = native.root / "result.json"
    ci.write_json(path, saved)
    if change == "mode":
        path.chmod(0o644)
    elif change == "symlink":
        path.rename(native.root / "old-result.json")
        path.symlink_to(native.root / "old-result.json")
    elif change == "budget":
        path.write_bytes(b"x" * (ci.OUTPUT_BYTES + 1))
    fresh = ci.NativeCI(native.root, run=commands)
    fresh.cleanup()
    result = ci.fixture_result(path.read_bytes())
    assert result["completed"] is True and result["resources"]["cleanup"] == "verified"
    assert result["timeline"] == value["timeline"] and result["generations"] == value["generations"]
    assert (result["resources"].get("manager_readiness") == readiness) is (change == "valid")
    assert (result["resources"].get("manager_failure") == failure) is (change == "valid")
    assert commands.accounts == {} and commands.mounts == {}


@pytest.fixture
def context_reader(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from muli.platforms import linux_transaction

    control: dict[str, Any] = {
        "clock": 100.0,
        "reads": [],
        "round": 0,
        "change": "none",
        "environment": b"SECRET=never-export-me\xff\0NOTIFY_SOCKET=/run/systemd/notify\0"
        b"XDG_RUNTIME_DIR=/run/user/23145\0"
        b"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/23145/bus\0",
    }
    monkeypatch.setattr(worker.time, "monotonic", lambda: control["clock"])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        worker, "system_executables", lambda: {"manager": "/usr/lib/systemd/systemd"}
    )

    def read(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
        assert path.parent == Path("/proc/42") and directory_fd is None and limit > 0
        control["reads"].append((path.name, limit))
        change = control["change"]
        if path.name == "environ":
            if change == "permission":
                raise PermissionError("never-export-me")
            if change == "deadline":
                control["clock"] = 105.0
            data = control["environment"]
        elif path.name == "stat":
            control["round"] += 1
            ticks = 101 if change == "ticks" and control["round"] > 1 else 100
            data = f"42 (secret) S {'0 ' * 18}{ticks}\n".encode()
        elif path.name == "status":
            uid = 1 if change == "uid" and control["round"] > 1 else 23145
            data = f"Uid:\t{uid} {uid} {uid} {uid}\n".encode()
        else:
            assert path.name == "cgroup"
            group = (
                "/other"
                if change == "group" and control["round"] > 1
                else "/user.slice/user@23145.service/init.scope"
            )
            data = f"0::{group}\n".encode()
        if len(data) > limit:
            raise ValueError("invalid_observation_file")
        return data

    def image(path: Path) -> str:
        assert path == Path("/proc/42/exe")
        return (
            "/secret/image"
            if control["change"] == "image" and control["round"] > 1
            else "/usr/lib/systemd/systemd"
        )

    def metadata(path: Path, owner: int) -> str:
        assert (path, owner) in {
            (Path("/run/systemd/notify"), 0),
            (Path("/run/user/23145/systemd/notify"), 23145),
            (Path("/run/user/23145/systemd/private"), 23145),
        }
        return "socket_owner_matches"

    monkeypatch.setattr(linux_transaction, "_read_regular", read)
    monkeypatch.setattr(os, "readlink", image)
    monkeypatch.setattr(worker, "context_socket", metadata)
    return control


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "ticks",
        "uid",
        "group",
        "image",
        "permission",
        "deadline",
        "duplicate",
        "truncated",
        "budget",
    ],
)
def test_context_reader_is_bounded_identity_bound_and_never_exports_raw_environment(
    context_reader: dict[str, Any],
    change: str,
) -> None:
    control = context_reader
    control["change"] = change
    if change == "duplicate":
        control["environment"] += b"NOTIFY_SOCKET=never-export-me\0"
    if change == "truncated":
        control["environment"] = b"SECRET=never-export-me"
    limit = 32 if change == "budget" else 4096
    value = worker.read_setup_context(
        42, 23145, 100, "/user.slice/user@23145.service", 105.0, limit
    )
    facts = value["facts"]
    assert ci.context_facts(facts) and "never-export-me" not in json.dumps(value)
    if change == "none":
        assert facts == {
            "code": "observed",
            "notify_env": "parent",
            "runtime_env": "expected",
            "bus_env": "expected",
            "listen_env": "absent",
            "parent_notify": "socket_owner_matches",
            "user_notify": "socket_owner_matches",
            "private_socket": "socket_owner_matches",
        }
        assert 0 < value["used"] < limit
        limits = [item[1] for item in control["reads"]]
        assert all(first > second for first, second in zip(limits, limits[1:], strict=False))
        assert len(json.dumps(value, separators=(",", ":")).encode()) <= 512
    else:
        assert facts["code"] != "observed" and value["used"] == limit


@pytest.mark.parametrize(
    ("data", "notify", "runtime", "bus", "listen"),
    [
        (b"", "missing", "missing", "missing", "absent"),
        (
            b"NOTIFY_SOCKET=/run/user/42/systemd/notify\0LISTEN_PID=secret\0",
            "user",
            "missing",
            "missing",
            "present",
        ),
        (
            b"NOTIFY_SOCKET=@secret\0XDG_RUNTIME_DIR=secret\0DBUS_SESSION_BUS_ADDRESS=secret\0",
            "other",
            "other",
            "other",
            "absent",
        ),
    ],
)
def test_context_environment_distinguishes_parent_and_user_notify(
    data: bytes,
    notify: str,
    runtime: str,
    bus: str,
    listen: str,
) -> None:
    assert worker.context_environment(data, 42) == {
        "notify_env": notify,
        "runtime_env": runtime,
        "bus_env": bus,
        "listen_env": listen,
    }


@pytest.mark.parametrize(
    "case", ["socket", "owner", "regular", "symlink", "missing", "parent-symlink"]
)
def test_context_socket_only_inspects_fixed_metadata(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from types import SimpleNamespace

    opened: list[int] = []
    closed: list[int] = []

    def open_directory(path: Any, flags: int, *, dir_fd: int | None = None) -> int:
        assert flags & os.O_DIRECTORY and flags & os.O_CLOEXEC
        if dir_fd is not None:
            assert flags & os.O_NOFOLLOW and str(path) in {"run", "systemd"}
        if case == "parent-symlink" and path == "systemd":
            raise OSError("secret")
        descriptor = 100 + len(opened)
        opened.append(descriptor)
        return descriptor

    def metadata(path: str, *, dir_fd: int, follow_symlinks: bool) -> Any:
        assert path == "notify" and dir_fd == opened[-1] and not follow_symlinks
        if case == "missing":
            raise FileNotFoundError("secret")
        return SimpleNamespace(
            st_uid=1 if case == "owner" else 0,
            st_mode={"regular": stat.S_IFREG, "symlink": stat.S_IFLNK}.get(case, stat.S_IFSOCK),
        )

    monkeypatch.setattr(os, "open", open_directory)
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(os, "stat", metadata)
    value = worker.context_socket(Path("/run/systemd/notify"), 0)
    assert value == {
        "socket": "socket_owner_matches",
        "missing": "absent",
        "parent-symlink": "unavailable",
    }.get(case, "unexpected")
    assert closed == opened


@pytest.mark.parametrize("change", ["extra", "raw", "unknown", "partial"])
def test_context_exports_reject_unknown_or_private_fields(change: str) -> None:
    value = worker.context_unavailable()
    if change == "extra":
        value["environment"] = "secret"
    elif change == "raw":
        value["notify_env"] = "/secret/notify"
    elif change == "unknown":
        value["code"] = "ready"
    else:
        value["notify_env"] = "parent"
    assert not ci.context_facts(value)
    with pytest.raises(worker.NativeError):
        worker.context_projection(value)


def test_context_cli_never_prints_raw_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def admitted(*, root: bool = False) -> None:
        assert root is True

    def fail(*args: Any) -> NoReturn:
        raise ValueError("SECRET=never-export-me")

    monkeypatch.setattr(worker, "read_setup_context", fail)
    monkeypatch.setattr(worker, "require_resource_envelope", admitted)
    monkeypatch.setattr(
        sys,
        "argv",
        ["worker", "project-setup-context", "42", "23145", "100", "/owned", "105", "1024"],
    )
    assert worker.main() == 0
    output = capsys.readouterr()
    assert output.err == "" and "never-export-me" not in output.out
    value = json.loads(output.out)
    assert ci.context_facts(value["facts"]) and value["facts"]["code"] == "invalid_reply"
    assert len(output.out.encode()) <= worker.CONTEXT_REPLY_BYTES


@pytest.mark.parametrize("change", ["uid", "privilege", "expired", "extended"])
def test_context_reader_refuses_invalid_identity_or_time_before_proc_reads(
    context_reader: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    if change == "privilege":
        monkeypatch.setattr(os, "geteuid", lambda: 23145)
    value = worker.read_setup_context(
        42,
        0 if change == "uid" else 23145,
        100,
        "/owned",
        {"expired": 100.0, "extended": 106.0}.get(change, 105.0),
        1024,
    )
    assert value["facts"]["code"] != "observed" and value["used"] == 1024
    assert context_reader["reads"] == []


@pytest.mark.parametrize("failure", ["balance", "cancel", "exception", "stderr", "late"])
async def test_context_io_cannot_reuse_failed_allowance_or_extend_deadline(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from muli import process

    clock = 100.0
    calls = 0
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock)

    async def command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        nonlocal calls, clock
        calls += 1
        assert kwargs["timeout"] <= 5 and kwargs["limit"] == worker.CONTEXT_REPLY_BYTES
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "exception":
            raise OSError("SECRET=never-export-me")
        if failure == "late":
            clock = 105.0
        return {"exit_code": 0, "output": "", "stderr": "x" * (worker.CONTEXT_REPLY_BYTES + 1)}

    monkeypatch.setattr(process, "run_process", command)
    limit = worker.CONTEXT_REPLY_BYTES if failure == "balance" else 4096
    io = worker._SetupIO(105.0, limit, {})
    with pytest.raises((ValueError, OSError, asyncio.CancelledError)):
        await io.context(42, 23145, 100, "/owned")
    assert calls == (0 if failure == "balance" else 1)
    if calls:
        assert io.used >= io.limit


@pytest.mark.parametrize(
    "change", ["none", "invocation", "pid", "ticks", "deadline", "invalid-reply"]
)
async def test_context_outer_window_binds_invocation_pid_and_ticks(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from types import SimpleNamespace

    clock = 100.0
    rounds = 0
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock)

    async def properties(path: str, interface: str, fields: Any) -> dict[str, Any]:
        nonlocal rounds
        assert path == "/fixed/unit"
        if interface == "Unit":
            rounds += 1
            return {
                "Id": "user@23145.service",
                "InvocationID": [2 if change == "invocation" and rounds > 1 else 1] * 16,
                "ActiveState": "activating",
                "SubState": "start",
            }
        if fields[0][0] == "StatusText":
            return {
                "StatusText": "",
                "NotifyAccess": "main",
                "Type": "notify-reload",
                "Result": "success",
            }
        return {
            "MainPID": 43 if change == "pid" and rounds > 1 else 42,
            "ControlGroup": "/user.slice/user@23145.service",
        }

    async def read(pid: int, name: str) -> bytes:
        assert pid == 42 and name == "stat"
        ticks = 101 if change == "ticks" and rounds > 1 else 100
        return f"42 (private) S {'0 ' * 18}{ticks}\n".encode()

    async def command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        nonlocal clock
        commands.append(argv)
        assert argv[:3] == ["sudo", "--non-interactive", "--"]
        assert argv[6] == "project-setup-context" and argv[7:10] == ["42", "23145", "100"]
        assert float(argv[-2]) == 105.0 and 0 < int(argv[-1]) < 4096
        assert 0 < kwargs["timeout"] <= 5 and kwargs["limit"] <= 512
        if change == "deadline":
            clock = 105.0
        facts = worker.context_unavailable("unavailable")
        if change == "invalid-reply":
            facts["secret"] = "never-export-me"
        return {"exit_code": 0, "output": json.dumps({"facts": facts, "used": 20}), "stderr": ""}

    from muli import process

    monkeypatch.setattr(process, "run_process", command)
    io = worker._SetupIO(105.0, 4096, {})
    facts, notification = await worker.observe_setup_context(
        SimpleNamespace(_properties=properties),
        "/fixed/unit",
        23145,
        42,
        "/user.slice/user@23145.service",
        read,
        io,
        105.0,
    )
    assert ci.context_facts(facts) and len(commands) == 1
    assert ci.notification_facts(notification) and notification["code"] == facts["code"]
    assert facts["code"] == {
        "none": "unavailable",
        "deadline": "timeout",
        "invalid-reply": "invalid_reply",
    }.get(change, "identity_changed")
    assert "never-export-me" not in json.dumps(facts)
    assert 0 < io.used <= 4096


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("", "empty"),
        ("Reached basic.target.", "reached_basic"),
        ("Ready.", "ready"),
        ("Ready.\nSECRET=never-export-me", "other"),
        (" Ready.", "other"),
        ("User job private.service/start running ...", "other"),
    ],
)
def test_notification_projection_keeps_only_exact_fixed_text_labels(text: str, label: str) -> None:
    value = worker.notification_projection(
        {"StatusText": text, "NotifyAccess": "main", "Type": "notify-reload", "Result": "success"},
        {"ActiveState": "activating", "SubState": "start"},
    )
    assert ci.notification_facts(value) and value["status_text"] == label
    assert set(value) == {
        "code",
        "status_text",
        "notify_access",
        "service_type",
        "result",
        "active_state_after",
        "sub_state_after",
    }
    assert "never-export-me" not in json.dumps(value) and "private.service" not in json.dumps(value)
    # Even READY's fixed STATUS label carries no receipt or readiness decision.
    assert "ready_received" not in value and "ready_sent" not in value


@pytest.mark.parametrize("change", ["extra", "raw", "partial", "missing", "boolean"])
def test_notification_export_rejects_arbitrary_or_partial_facts(change: str) -> None:
    value: dict[str, Any] = worker.notification_unavailable()
    if change == "extra":
        value["StatusText"] = "never-export-me"
    elif change == "raw":
        value["status_text"] = "Ready."
    elif change == "partial":
        value["status_text"] = "ready"
    elif change == "missing":
        value.pop("notify_access")
    else:
        value["result"] = True
    assert not ci.notification_facts(value)


@pytest.mark.parametrize(
    "case",
    [
        "observed",
        "state-change",
        "other",
        "empty",
        "unknown-values",
        "invocation",
        "pid",
        "group",
        "ticks",
        "status-budget",
        "status-late",
        "tail-late",
        "inner-unavailable",
        "invalid-status",
        "balance",
    ],
)
async def test_notification_window_shares_identity_deadline_and_actual_byte_accounting(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from muli import process
    from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe

    clock = 100.0
    rounds = 0
    calls: list[tuple[list[str], int, float]] = []
    wire_bytes = 0
    helper_calls = 0
    group = "/user.slice/user@23145.service"
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock)

    async def command(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool = False
    ) -> dict[str, Any]:
        nonlocal clock, rounds, wire_bytes, helper_calls
        assert 0 < timeout <= 5 and not merge_stderr
        calls.append((argv, limit, timeout))
        if argv[0] == "sudo":
            helper_calls += 1
            assert argv[6] == "project-setup-context" and limit == worker.CONTEXT_REPLY_BYTES
            context = worker.context_unavailable(
                "unavailable" if case == "inner-unavailable" else "observed"
            )
            output = json.dumps({"facts": context, "used": 20})
        else:
            assert argv[:2] == ["busctl", "--system"] and "--auto-start=no" in argv
            index = argv.index("get-property")
            assert argv[index + 1 : index + 3] == ["org.freedesktop.systemd1", "/fixed/unit"]
            fields = argv[index + 4 :]
            if "Id" in fields:
                rounds += 1
            changed = rounds > 1
            status: Any = {
                "empty": "",
                "other": "SECRET=never-export-me",
                "invalid-status": True,
            }.get(case, "Ready.")
            rows: dict[str, tuple[str, Any]] = {
                "Id": ("s", "user@23145.service"),
                "InvocationID": ("ay", [2 if changed and case == "invocation" else 1] * 16),
                "MainPID": ("u", 43 if changed and case == "pid" else 42),
                "ControlGroup": ("s", "/foreign" if changed and case == "group" else group),
                "ActiveState": (
                    "s",
                    "active" if changed and case == "state-change" else "activating",
                ),
                "SubState": ("s", "running" if changed and case == "state-change" else "start"),
                "StatusText": ("s", status),
                "NotifyAccess": ("s", "never-export-me" if case == "unknown-values" else "main"),
                "Type": ("s", "never-export-me" if case == "unknown-values" else "notify-reload"),
                "Result": ("s", "never-export-me" if case == "unknown-values" else "success"),
            }
            output = "\n".join(
                json.dumps({"type": rows[field][0], "data": rows[field][1]}) for field in fields
            )
            if "StatusText" in fields and case == "status-budget":
                output = "x" * (limit + 1)
            if ("StatusText" in fields and case == "status-late") or (
                changed and case == "tail-late"
            ):
                clock = 105.0
        wire_bytes += len(output.encode())
        return {"exit_code": 0, "output": output, "stderr": ""}

    async def read(pid: int, name: str) -> bytes:
        assert pid == 42 and name == "stat"
        ticks = 101 if rounds > 1 and case == "ticks" else 100
        data = f"42 (private) S {'0 ' * 18}{ticks}\n".encode()
        io.charge(data)
        return data

    monkeypatch.setattr(process, "run_process", command)
    io = worker._SetupIO(105.0, 1 if case == "balance" else 4096, {})
    context, notification = await worker.observe_setup_context(
        SystemdProbe(runner=io.query, limits=ProbeLimits(seconds=5, command_bytes=4096)),
        "/fixed/unit",
        23145,
        42,
        group,
        read,
        io,
        105.0,
    )
    assert ci.context_facts(context) and ci.notification_facts(notification)
    assert "never-export-me" not in json.dumps((context, notification))
    failure = {
        "invocation": "identity_changed",
        "pid": "identity_changed",
        "group": "identity_changed",
        "ticks": "identity_changed",
        "status-budget": "output_budget",
        "status-late": "timeout",
        "tail-late": "timeout",
        "inner-unavailable": "unavailable",
        "invalid-status": "invalid_reply",
        "balance": "output_budget",
    }.get(case)
    if failure is not None:
        assert notification == worker.notification_unavailable(failure)
        assert context["code"] == failure
    else:
        assert notification["code"] == context["code"] == "observed"
        assert notification["status_text"] == {"empty": "empty", "other": "other"}.get(
            case, "ready"
        )
        assert notification["active_state_after"] == (
            "active" if case == "state-change" else "activating"
        )
        assert notification["sub_state_after"] == ("running" if case == "state-change" else "start")
        assert notification["result"] == ("other" if case == "unknown-values" else "success")
    assert helper_calls == (
        0 if case in {"status-budget", "status-late", "invalid-status", "balance"} else 1
    )
    assert io.used >= wire_bytes
    assert all(limit <= 4096 and timeout <= 5 for _, limit, timeout in calls)


@pytest.mark.parametrize("exhausted", [False, True])
def test_notification_reply_counts_actual_outer_bytes_without_replacing_manager_failure(
    fixture: tuple[Any, Commands],
    exhausted: bool,
) -> None:
    native, commands = fixture
    original = ci.FixtureError("native_manager_starting")
    summary = {
        "code": "native_deadline_exceeded",
        "stage": "manager_validation",
        "query_status": 0,
        "query_code": "ok",
        "used": 1,
    }
    native.manager_failure = summary
    native.prepare()
    assert native.receipt is not None
    native.receipt.setup_phase = "manager_show_before"
    before = native.setup_bytes
    returned = 0
    charged = 0

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal returned, charged
        if "diagnose-setup" not in argv:
            return commands(argv, seconds=seconds)
        assert int(argv[-1]) == ci.OUTPUT_BYTES - before - 1024
        reply = commands(argv, seconds=seconds)
        value = json.loads(reply.output)
        value["context"]["code"] = "observed"
        value["notification"] = worker.notification_projection(
            {
                "StatusText": "Ready.",
                "NotifyAccess": "main",
                "Type": "notify-reload",
                "Result": "success",
            },
            {"ActiveState": "activating", "SubState": "start"},
        )
        if exhausted:
            value["used"] = ci.OUTPUT_BYTES - before
        charged = value["used"]
        output = json.dumps(value).encode()
        returned = len(output)
        return ci.Reply(0, output)

    native.run = run
    native.observe_setup(original)
    value = ci.fixture_result((native.root / "result.json").read_bytes())
    assert value["resources"]["manager_failure"] == summary
    facts = value["resources"]["setup"]
    assert facts["client_code"] == "command_failed"
    if exhausted:
        assert facts["diagnostic_code"] == "output_budget"
        assert facts["notification"] == worker.notification_unavailable()
    else:
        assert facts["notification"]["status_text"] == "ready"
    assert native.setup_bytes == before + charged + returned


@pytest.mark.parametrize("case", ["exact", "one-over", "removed-check-room", "saturates-grant"])
def test_complete_setup_reply_obeys_total_bytes_beyond_minimum_reply_reserve(
    fixture: tuple[Any, Commands],
    case: str,
) -> None:
    native, commands = fixture
    native.prepare()
    native.receipt.setup_phase = "manager_show_before"
    summary = {
        "code": "native_deadline_exceeded",
        "stage": "manager_validation",
        "query_status": 0,
        "query_code": "ok",
        "used": 17468,
    }
    native.manager_failure = summary
    names = (f"user-runtime-dir@{commands.uid}.service", f"user@{commands.uid}.service")
    data: dict[str, Any] = {
        "stage": "service_properties",
        "query_status": 0,
        "query_code": "ok",
        "failure": "none",
        "unit": names[1],
        "units": [
            {
                "unit": name,
                "code": "observed",
                "load_state": "loaded",
                "active_state": "active" if index == 0 else "activating",
                "sub_state": "exited" if index == 0 else "start",
                "result": "success",
                "exec_code": 1 if index == 0 else 0,
                "exec_status": 0,
                "pid": 0 if index == 0 else 42,
                "job": 0 if index == 0 else 100,
                "cgroup_present": True,
            }
            for index, name in enumerate(names)
        ],
        "process": {
            "code": "observed",
            "exe": "manager",
            "uid_matches": True,
            "state": "S",
            "wait": "epoll_wait",
        },
        "journal": {"code": "no_known_event", "incomplete": False, "events": []},
        "context": {
            "code": "observed",
            "notify_env": "parent",
            "runtime_env": "expected",
            "bus_env": "expected",
            "listen_env": "absent",
            "parent_notify": "socket_owner_matches",
            "user_notify": "socket_owner_matches",
            "private_socket": "socket_owner_matches",
        },
        "notification": worker.notification_projection(
            {
                "StatusText": "fixture private text",
                "NotifyAccess": "main",
                "Type": "notify-reload",
                "Result": "success",
            },
            {"ActiveState": "activating", "SubState": "start"},
        ),
        "used": 1024,
    }

    def encoded() -> bytes:
        return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()

    output = encoded()
    assert len(output) > 1024  # Original minimum reserve is not a complete-JSON size cap.
    before = ci.OUTPUT_BYTES - data["used"] - len(output) + (case == "one-over")
    saving = 0
    if case == "removed-check-room":
        original_used = native.setup_bytes
        native.bootstrap_files("check")  # Measure the actual removed fixture helper/reply cost.
        saving = native.setup_bytes - original_used
        historical_before = ci.OUTPUT_BYTES - data["used"] - len(output) + 1
        assert historical_before + data["used"] + len(output) == ci.OUTPUT_BYTES + 1
        before = historical_before - saving
    if case == "saturates-grant":
        # A synthetic saving increases the grant, and the worker consumes that increase too.
        # This deliberately proves deduplication alone does not ensure the outer reply fits.
        before -= len(worker.bootstrap_data())
        data["used"] = ci.OUTPUT_BYTES - before - 1024
        output = encoded()
    native.setup_bytes = before
    calls = 0

    def observed(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal calls
        assert "diagnose-setup" in argv
        calls += 1
        assert int(argv[-1]) == ci.OUTPUT_BYTES - before - 1024
        assert data["used"] <= int(argv[-1])
        assert 0 < seconds <= ci.CLEANUP_SECONDS
        return ci.Reply(0, output)

    native.run = observed
    native.observe_setup(ci.FixtureError("native_deadline_exceeded"))
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"]["manager_failure"] == summary and calls == 1
    assert saved["timeline"] == [] and saved["generations"] == []
    assert b"private text" not in (native.root / "result.json").read_bytes()
    facts = saved["resources"]["setup"]
    assert facts["diagnostic_status"] == 0
    assert native.setup_bytes == before + data["used"] + len(output)
    if case in {"exact", "removed-check-room"}:
        assert native.setup_bytes == ci.OUTPUT_BYTES + (1 - saving if saving else 0)
        assert native.setup_bytes <= ci.OUTPUT_BYTES
        assert facts["diagnostic_code"] == "ok" and len(facts["units"]) == 2
        assert facts["notification"]["status_text"] == "other"
    else:
        assert native.setup_bytes > ci.OUTPUT_BYTES
        assert facts["diagnostic_code"] == "output_budget" and facts["units"] == []
        assert facts["context"]["code"] == "not_started"


def test_bootstrap_socket_growth_probe_never_reads_past_raw_grant(
    fixture: tuple[Any, Commands], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real regular-file reader, including its overflow probe."""
    native, commands = fixture
    native.prepare()
    receipt = native.receipt
    assert receipt is not None
    leaf = native.home / ".config/systemd/user/dbus.socket.d/muli-runtime.conf"
    saved = leaf.stat()
    manifest = json.dumps(
        {
            "directories": receipt.bootstrap_directories,
            "file": receipt.bootstrap_file,
            "mask": receipt.bootstrap_mask,
            "socket_directory": receipt.bootstrap_socket_directory,
            "socket_file": receipt.bootstrap_socket_file,
        },
        separators=(",", ":"),
    )
    grant = (
        len(manifest.encode())
        + len(worker.bootstrap_data())
        + len(worker.bootstrap_socket_data(receipt.uid))
    )
    actual_read = 0
    original_fdopen = os.fdopen

    class ReadCounter:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def __enter__(self) -> ReadCounter:
            return self

        def __exit__(self, *arguments: object) -> None:
            self.handle.close()

        def fileno(self) -> int:
            return int(self.handle.fileno())

        def read(self, size: int) -> bytes:
            nonlocal actual_read
            current = os.fstat(self.fileno())
            if (current.st_dev, current.st_ino) == (saved.st_dev, saved.st_ino):
                # The real reader has already taken its initial fstat. Grow
                # that same inode just before its real bounded read executes.
                with leaf.open("ab") as output:
                    output.write(b"x")
            data: bytes = self.handle.read(size)
            actual_read += len(data)
            return data

    def counted_fdopen(descriptor: int, mode: str, *args: Any, **kwargs: Any) -> Any:
        handle = original_fdopen(descriptor, mode, *args, **kwargs)
        return ReadCounter(handle) if mode == "rb" else handle

    monkeypatch.setattr(os, "fdopen", counted_fdopen)
    observed = commands.bootstrap(
        "check",
        receipt.user,
        receipt.uid,
        receipt.gid,
        manifest,
        worker.time.monotonic() + worker.CLEANUP_SECONDS,
        grant,
    )
    assert observed["code"] != "verified" and observed["used"] == grant
    assert leaf.exists() and receipt.bootstrap_socket_file == [saved.st_dev, saved.st_ino]
    actual_raw = len(manifest.encode()) + actual_read
    assert actual_raw <= grant, f"actual raw bytes {actual_raw} exceed grant {grant}"
