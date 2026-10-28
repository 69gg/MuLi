"""Hosted-CI-only worker. No manager, protocol, probe or backend is replaced."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import stat
import sys
import sysconfig
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing
from functools import wraps
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import FrameType, FunctionType, ModuleType
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast, get_args

if TYPE_CHECKING:
    from muli.managed_transaction import TransactionRecordV2
    from muli.management_transaction import JournalControl, Role, TransactionJournal
    from muli.platforms.linux_transaction import CommandRunner, SystemdProbe
    from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity
    from muli.store import Store

    from .managed_native_peer import PeerFacts

    HostRecordReader = Callable[
        [JournalControl, DeploymentAnchor],
        tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]],
    ]

MAX_BYTES = 32768
CONTEXT_REPLY_BYTES = 512
CASE_SECONDS = 60
CLEANUP_SECONDS = 5
HARNESS_SECONDS = 90
MEMORY = 1024**3
TASKS = 128
HARNESS_MEMORY = MEMORY // 4
HARNESS_TASKS = 32
HTTP_SECONDS = 2.0
POLL_SECONDS = 0.1
HELPER_COST_SLOT_METRICS = ("slot_files_controller", "slot_files_selected")
HELPER_START_BOUNDARIES = ("start_entered", "start_command_entered", "start_command_returned")
HELPER_START_MODES = (
    ("activating", "start_target", "target"),
    ("rolling_back", "start_base", "base"),
)
# Version-two diagnostic labels, readable by the stdlib-only coordinator.
HELPER_COST_PHASES = (
    "prepared",
    "draining",
    "activating",
    "verifying",
    "rolling_back",
    "committed",
    "rolled_back",
    "aborted",
    "recovery_required",
)
HELPER_COST_INTENTS = (
    "none",
    "stop_base",
    "install_target",
    "start_target",
    "stop_target",
    "restore_base",
    "start_base",
    "verify_base",
)
PROCESS_STATES = frozenset("RSDZTtXxKWPI")
SOCKET_SUB_STATES = frozenset(
    {
        "dead",
        "start-pre",
        "start-chown",
        "start-post",
        "listening",
        "running",
        "stop-pre",
        "stop-pre-sigterm",
        "stop-pre-sigkill",
        "stop-post",
        "final-sigterm",
        "final-sigkill",
        "failed",
        "cleaning",
    }
)
SYSTEM_EXECUTABLE_ROOTS = (Path("/usr/lib/systemd"), Path("/lib/systemd"))
BOOTSTRAP_PARENT = Path("/tmp")
JOURNAL_EVENTS = 32
# Fixed v255 sd-messages.h IDs describe jobs, except USER_STARTUP_FINISHED.
JOURNAL_MESSAGES = {
    "7d4958e842da4a758f6c1cdc7b36dcc5": "starting",
    "39f53479d3a045ac8e11786248231fbf": "started",
    "be02cf6855d2428ba40df7e9d022f03d": "failed",
    "eed00a68ffd84e31882105fd973abdd1": "user_startup_finished",
}
JOURNAL_FIELDS = (
    "_PID",
    "_UID",
    "_SYSTEMD_UNIT",
    "_SYSTEMD_INVOCATION_ID",
    "UNIT",
    "INVOCATION_ID",
    "USER_UNIT",
    "MESSAGE_ID",
    "JOB_TYPE",
    "JOB_RESULT",
)
JOURNAL_METADATA = frozenset(
    {"__CURSOR", "__REALTIME_TIMESTAMP", "__MONOTONIC_TIMESTAMP", "_BOOT_ID"}
)
RESOURCE_ENVIRONMENT = (
    "MULI_NATIVE_ENVELOPE_UNIT",
    "MULI_NATIVE_ENVELOPE_UID",
    "MULI_NATIVE_ENVELOPE_GID",
)
ROOT_RESOURCE_MODES = frozenset(
    {"project-manager-instance", "project-setup-context", "bootstrap-files"}
)
JOURNAL_JOB_RESULTS = frozenset(
    {
        "done",
        "canceled",
        "timeout",
        "failed",
        "dependency",
        "skipped",
        "invalid",
        "assert",
        "unsupported",
        "collected",
        "once",
    }
)


class NativeError(ValueError):
    """A fixed code, never an upstream response or a configuration value."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise NativeError(code)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, "native_metadata_budget")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise NativeError("native_metadata_invalid")
    return result


def write_json(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    require(len(data) <= MAX_BYTES, "native_metadata_budget")
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    require(value > 0, "native_deadline_exceeded")
    return value


def kernel_limits(
    group: Path,
    *,
    memory: int,
    tasks: int,
    read: Callable[[Path], str] | None = None,
) -> dict[str, str]:
    read = read or (lambda path: path.read_text())
    result = {}
    for name, maximum in (("memory.max", memory), ("memory.swap.max", 0), ("pids.max", tasks)):
        value = read(group / name).strip()
        require(value.isdecimal() and int(value) <= maximum, "native_kernel_limit")
        require(not maximum or int(value) > 0, "native_kernel_limit")
        result[name] = value
    values = read(group / "cpu.max").split()
    require(len(values) == 2 and all(item.isdecimal() for item in values), "native_kernel_cpu")
    quota, period = map(int, values)
    require(0 < quota <= period, "native_kernel_cpu")
    result["cpu.max"] = " ".join(values)
    controllers = read(group / "cgroup.controllers").split()
    require({"cpu", "memory", "pids"}.issubset(controllers), "native_kernel_controllers")
    return result


def resource_identity() -> tuple[str, int, int]:
    """These labels select a kernel identity; they never prove membership."""
    unit, uid, gid = (os.environ.get(name, "") for name in RESOURCE_ENVIRONMENT)
    require(
        re.fullmatch(
            r"muli-native-envelope-(run|cleanup)-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}\.service",
            unit,
        )
        is not None
        and re.fullmatch(r"[1-9][0-9]{0,9}", uid) is not None
        and re.fullmatch(r"[1-9][0-9]{0,9}", gid) is not None,
        "native_resource_envelope",
    )
    require(int(uid) < 2**31 and int(gid) < 2**31, "native_resource_envelope")
    return unit, int(uid), int(gid)


def require_resource_envelope(mode: str | None = None, *, root: bool = False) -> None:
    """Check this process, including a sudo child, before any effect or FFI."""
    try:
        unit, uid, gid = resource_identity()
        if mode is not None:
            require(
                mode in {"run", "cleanup"}
                and unit
                == f"muli-native-envelope-{mode}-{os.environ.get('GITHUB_RUN_ID', '')}"
                f"-{os.environ.get('GITHUB_RUN_ATTEMPT', '')}.service",
                "native_resource_envelope",
            )
        expected_uid, expected_gid = (0, 0) if root else (uid, gid)

        def read(path: Path) -> str:
            with path.open("rb") as source:
                data = source.read(513)
            require(len(data) <= 512, "native_resource_envelope")
            return data.decode("ascii")

        def identity() -> None:
            require(
                os.getresuid() == (expected_uid,) * 3 and os.getresgid() == (expected_gid,) * 3,
                "native_resource_envelope",
            )
            require(
                read(Path("/proc/self/cgroup")) == f"0::/system.slice/{unit}\n",
                "native_resource_envelope",
            )

        identity()
        group = Path("/sys/fs/cgroup/system.slice") / unit
        kernel_limits(group, memory=MEMORY, tasks=TASKS, read=read)
        require(read(group / "memory.oom.group").strip() == "1", "native_resource_envelope")
        identity()
    except (OSError, ValueError) as error:
        raise NativeError("native_resource_envelope") from error


def resource_sudo_arguments(argv: Sequence[str]) -> list[str]:
    """Pass only the fixed selector labels to our own root-Python entry modes."""
    values = list(argv)
    if (
        len(values) >= 7
        and values[:3] == ["sudo", "--non-interactive", "--"]
        and values[3:5] == [sys.executable, "-I"]
        and Path(values[5]).resolve() == Path(__file__).resolve()
        and values[6] in ROOT_RESOURCE_MODES
        and os.environ.get(RESOURCE_ENVIRONMENT[0])
    ):
        resource_identity()
        values.insert(2, "--preserve-env=" + ",".join(RESOURCE_ENVIRONMENT))
    return values


def membership(unit: str, uid: int) -> Path:
    value = Path("/proc/self/cgroup").read_text().strip()
    require(value.startswith("0::/") and "\n" not in value, "native_cgroup_version")
    group = Path(value.removeprefix("0::"))
    require(group.name == unit and f"user@{uid}.service" in group.parts, "native_membership")
    return Path("/sys/fs/cgroup") / str(group).lstrip("/")


def environment_facts(root: Path, uid: int) -> dict[str, bool | str]:
    """Export only comparisons, never values from the manager's environment."""
    home = root / "home"
    explicit = "XDG_CONFIG_HOME" in os.environ
    config = Path(os.environ["XDG_CONFIG_HOME"]) if explicit else Path.home() / ".config"
    return {
        "uid_matches": os.getuid() == uid and os.geteuid() == uid,
        "home_matches": os.environ.get("HOME") == str(home) and Path.home() == home,
        "config_source": "explicit" if explicit else "default",
        "config_matches": config == home / ".config",
        "runtime_matches": os.environ.get("XDG_RUNTIME_DIR") == f"/run/user/{uid}",
        "bus_matches": os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        == f"unix:path=/run/user/{uid}/bus",
    }


def require_environment(root: Path, uid: int) -> dict[str, bool | str]:
    facts = environment_facts(root, uid)
    require(
        all(value is True for key, value in facts.items() if key != "config_source"),
        "native_environment_mismatch",
    )
    return facts


async def service_exists(probe: SystemdProbe) -> bool:
    """Exact standard-unit listing; helper_exists deliberately rejects this name."""
    from muli.platforms.linux_transaction import _typed

    async with asyncio.timeout(probe.limits.seconds):
        reply = _typed(
            await probe._command(
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "ListUnitsByPatterns",
                "asas",
                "0",
                "1",
                "muli.service",
            ),
            "a(ssssssouso)",
        )
        if reply == [[]]:
            return False
        require(
            isinstance(reply, list)
            and len(reply) == 1
            and isinstance(reply[0], list)
            and len(reply[0]) == 1
            and isinstance(reply[0][0], list)
            and len(reply[0][0]) == 10
            and reply[0][0][0] == "muli.service",
            "native_service_listing",
        )
        return True


async def observe_harness(probe: SystemdProbe, unit: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Read the exact failed harness through the product's typed D-Bus parser."""
    require(
        re.fullmatch(r"muli-native-[a-f0-9]{32}\.service", unit) is not None,
        "native_fixture_unit",
    )
    from muli.platforms.linux_transaction import _typed

    facts["stage"] = "manager"
    manager = await probe._properties("/org/freedesktop/systemd1", "Manager", (("Version", "s"),))
    require(
        isinstance(manager["Version"], str) and 0 < len(manager["Version"]) <= 256,
        "native_diagnostic_invalid",
    )
    facts["stage"] = "listing"
    listed = _typed(
        await probe._command(
            "call",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "ListUnitsByPatterns",
            "asas",
            "0",
            "1",
            unit,
        ),
        "a(ssssssouso)",
    )
    if listed == [[]]:
        return {**facts, "code": "unit_absent", "failure": "none"}
    require(
        isinstance(listed, list)
        and len(listed) == 1
        and isinstance(listed[0], list)
        and len(listed[0]) == 1
        and isinstance(listed[0][0], list)
        and len(listed[0][0]) == 10
        and listed[0][0][0] == unit,
        "native_diagnostic_invalid",
    )
    facts["stage"] = "get_unit"
    path = await probe._unit_path(unit)
    facts["stage"] = "unit_properties"
    state = await probe._properties(
        path,
        "Unit",
        (("Id", "s"), ("LoadState", "s"), ("ActiveState", "s"), ("SubState", "s")),
    )
    facts["stage"] = "service_properties"
    service = await probe._properties(
        path,
        "Service",
        (("Result", "s"), ("ExecMainCode", "i"), ("ExecMainStatus", "i"), ("MainPID", "u")),
    )
    require(state["Id"] == unit, "native_fixture_unit")
    strings = {
        "load_state": state["LoadState"],
        "active_state": state["ActiveState"],
        "sub_state": state["SubState"],
        "result": service["Result"],
    }
    integers = {
        "exec_code": service["ExecMainCode"],
        "exec_status": service["ExecMainStatus"],
        "pid": service["MainPID"],
    }
    require(
        all(
            isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value) is not None
            for value in strings.values()
        )
        and all(type(value) is int and 0 <= value < 2**31 for value in integers.values()),
        "native_diagnostic_invalid",
    )
    return {**facts, "code": "observed", "failure": "none", **strings, **integers}


def diagnostic_command_code(status: object, stderr: object) -> str:
    """Only fixed diagnostic labels leave this bounded client wrapper."""
    if type(status) is not int:
        return "invalid_reply"
    if status == 0:
        return "ok"
    body = stderr.lower() if isinstance(stderr, str) else ""
    if "failed to connect to bus" in body or "failed to connect to user" in body:
        return "bus_permission" if "permission denied" in body else "bus_unavailable"
    if any(item in body for item in ("unrecognized option", "unknown option", "invalid option")):
        return "unsupported_argument"
    if any(item in body for item in ("unknown property", "unknown assignment")):
        return "property_rejected"
    if any(
        item in body
        for item in ("permission denied", "access denied", "interactive authentication required")
    ):
        return "permission_denied"
    if "unit" in body and ("not loaded" in body or "not found" in body):
        return "unit_unavailable"
    return "command_failed"


def diagnostic_ancestors(home: Path) -> list[dict[str, int]]:
    """Observe only the volume parent's ancestor chain, never its contents."""
    temporary = home.parents[1]
    paths = tuple(reversed((temporary, *temporary.parents)))
    require(len(paths) <= 16, "native_diagnostic_invalid")
    result: list[dict[str, int]] = []
    for index, path in enumerate(paths):
        info = path.lstat()
        result.append(
            {"index": index, "mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid}
        )
    return result


async def diagnose(user: str, uid: int, home: Path, unit: str, deadline: float) -> dict[str, Any]:
    """Runner owns this interpreter; only busctl changes to the verified UID."""
    import pwd

    require(
        re.fullmatch(r"muli-ci-[a-f0-9]{12}", user) is not None
        and uid > 0
        and home.is_absolute()
        and len(home.parents) >= 3
        and re.fullmatch(r"muli-native-[a-f0-9]{32}\.service", unit) is not None,
        "native_diagnostic_identity",
    )
    require(remaining(deadline) <= CLEANUP_SECONDS, "native_diagnostic_deadline")
    facts: dict[str, Any] = {
        "unit": unit,
        "code": "unavailable",
        "stage": "identity",
        "failure": "not_started",
        "query_status": None,
        "query_code": "not_started",
        "ancestors": [],
    }
    try:
        account = pwd.getpwnam(user)
        require((account.pw_uid, account.pw_dir) == (uid, str(home)), "native_diagnostic_identity")
        facts["stage"] = "ancestors"
        facts["ancestors"] = diagnostic_ancestors(home)
        facts["stage"] = "import"
        from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe
        from muli.process import run_process

        async def query(
            argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
        ) -> dict[str, Any]:
            require(argv[0] == "busctl" and not merge_stderr, "native_diagnostic_command")
            facts["query_status"], facts["query_code"] = None, "not_started"
            result = await run_process(
                [
                    "sudo",
                    "--non-interactive",
                    "--user",
                    user,
                    "--",
                    "env",
                    "-i",
                    f"HOME={home}",
                    f"USER={user}",
                    f"LOGNAME={user}",
                    "PATH=/usr/bin:/bin",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    f"XDG_RUNTIME_DIR=/run/user/{uid}",
                    f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                    *argv,
                ],
                timeout=min(timeout, remaining(deadline)),
                limit=limit,
                merge_stderr=False,
            )
            status = result.get("exit_code")
            facts["query_status"] = (
                status if type(status) is int and -(2**31) <= status < 2**31 else None
            )
            facts["query_code"] = diagnostic_command_code(status, result.get("stderr"))
            return result

        async with asyncio.timeout(remaining(deadline)):
            return await observe_harness(
                SystemdProbe(
                    runner=query,
                    limits=ProbeLimits(seconds=CLEANUP_SECONDS, command_bytes=MAX_BYTES),
                ),
                unit,
                facts,
            )
    except TimeoutError:
        facts["failure"] = "deadline"
        facts["query_code"] = "timeout"
    except Exception as error:
        if isinstance(error, NativeError) and str(error) == "native_deadline_exceeded":
            facts["failure"], facts["query_code"] = "deadline", "timeout"
        elif facts["stage"] in {"identity", "ancestors", "import"}:
            facts["failure"] = {
                "identity": "identity_failed",
                "ancestors": "metadata_failed",
                "import": "import_failed",
            }[facts["stage"]]
        else:
            facts["failure"] = "query_failed" if facts["query_code"] != "ok" else "invalid_reply"
    return facts


def _system_executable(name: str, roots: tuple[Path, ...]) -> str | None:
    for parent in roots:
        try:
            path = (parent / name).resolve(strict=True)
            info = path.stat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022:
                return str(path)
        except OSError:
            continue
    return None


def system_executables() -> dict[str, str]:
    """Preserve the manager/executor classification used by the older diagnostic."""
    result: dict[str, str] = {}
    for kind, name in (("manager", "systemd"), ("executor", "systemd-executor")):
        path = _system_executable(name, SYSTEM_EXECUTABLE_ROOTS)
        if path is not None:
            result[kind] = path
    return result


def systemctl_executable() -> str | None:
    """Classify the control image without extending the older diagnostic's tags."""
    return _system_executable("systemctl", (Path("/usr/bin"), Path("/bin")))


def bootstrap_data() -> bytes:
    return (
        b"[Unit]\nDescription=MuLi native bootstrap\nDefaultDependencies=no\n"
        b"Requires=basic.target dbus.socket\nAfter=basic.target dbus.socket\n"
    )


def bootstrap_socket_data(uid: int) -> bytes:
    return f'[Socket]\nEnvironment="XDG_RUNTIME_DIR=/run/user/{uid}"\n'.encode("ascii")


def bootstrap_files(
    mode: str, user: str, uid: int, gid: int, manifest: str, deadline: float, limit: int
) -> dict[str, Any]:
    """Operate only on this receipt's fixed tree through pinned directory descriptors."""
    import pwd

    from muli.platforms.linux_transaction import _read_regular

    used = 0
    handles: list[int] = []
    socket_descriptor: int | None = None
    try:
        require(mode in {"create", "check", "remove"}, "native_bootstrap_arguments")
        require(
            os.geteuid() == 0 and 0 < uid < 2**31 and 0 < gid < 2**31, "native_bootstrap_identity"
        )
        require(
            re.fullmatch(r"muli-ci-[a-f0-9]{12}", user) is not None, "native_bootstrap_identity"
        )
        account = pwd.getpwnam(user)
        # The caller cannot supply a path, a unit name or file contents to this root mode.
        volume = BOOTSTRAP_PARENT / f"muli-managed-native-ci-{user}"
        require(
            (account.pw_uid, account.pw_gid, account.pw_dir) == (uid, gid, str(volume / "home")),
            "native_bootstrap_identity",
        )
        require(
            math.isfinite(deadline) and remaining(deadline) <= HARNESS_SECONDS + CLEANUP_SECONDS,
            "native_bootstrap_deadline",
        )
        require(type(limit) is int and 0 < limit <= MAX_BYTES, "native_diagnostic_output_budget")

        def charge(data: bytes) -> None:
            nonlocal used
            used += len(data)
            require(used <= limit, "native_diagnostic_output_budget")
            remaining(deadline)

        charge(manifest.encode())
        value = json.loads(manifest)
        require(
            isinstance(value, dict)
            and set(value)
            in (
                {"directories", "file"},
                {"directories", "file", "mask"},
                {"directories", "file", "mask", "socket_directory", "socket_file"},
            ),
            "native_bootstrap_invalid",
        )
        directories, identity = value["directories"], value["file"]
        mask_identity = value.get("mask")
        socket_directory = value.get("socket_directory")
        socket_file = value.get("socket_file")

        def pair(item: object) -> bool:
            return (
                isinstance(item, list)
                and len(item) == 2
                and all(type(part) is int and 0 <= part < 2**64 for part in item)
            )

        require(
            isinstance(directories, list)
            and 2 <= len(directories) <= 5
            and all(pair(item) for item in directories)
            and (identity is None or (pair(identity) and len(directories) == 5))
            and (
                mask_identity is None
                or (pair(mask_identity) and identity is not None and len(directories) == 5)
            )
            and (
                (socket_directory is None and socket_file is None)
                or (
                    pair(socket_directory)
                    and pair(socket_file)
                    and identity is not None
                    and len(directories) == 5
                )
            ),
            "native_bootstrap_invalid",
        )
        require(
            mode != "create"
            or (
                len(directories) == 2
                and identity is None
                and mask_identity is None
                and socket_directory is None
                and socket_file is None
            ),
            "native_bootstrap_invalid",
        )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        handles.append(os.open(volume, flags))
        names = ("home", ".config", "systemd", "user")

        def check_directory(index: int) -> None:
            info = os.fstat(handles[index])
            require(
                stat.S_ISDIR(info.st_mode)
                and stat.S_IMODE(info.st_mode) == 0o700
                and (info.st_uid, info.st_gid) == (uid, gid)
                and [info.st_dev, info.st_ino] == directories[index],
                "native_bootstrap_changed",
            )
            if index:
                linked = os.stat(names[index - 1], dir_fd=handles[index - 1], follow_symlinks=False)
            else:
                linked = os.stat(volume, follow_symlinks=False)
            require(
                (linked.st_dev, linked.st_ino, linked.st_mode, linked.st_uid, linked.st_gid)
                == (info.st_dev, info.st_ino, info.st_mode, uid, gid),
                "native_bootstrap_changed",
            )

        check_directory(0)
        for index, name in enumerate(names, 1):
            remaining(deadline)
            if mode == "create" and index >= 2:
                os.mkdir(name, mode=0o700, dir_fd=handles[-1])  # No exist_ok/adoption.
                descriptor = os.open(name, flags, dir_fd=handles[-1])
                handles.append(descriptor)
                os.fchmod(descriptor, 0o700)
                os.fchown(descriptor, uid, gid)
                info = os.fstat(descriptor)
                directories.append([info.st_dev, info.st_ino])
            else:
                try:
                    descriptor = os.open(name, flags, dir_fd=handles[-1])
                except FileNotFoundError:
                    require(
                        mode == "remove" and identity is not None and index >= 2,
                        "native_bootstrap_unconfirmed",
                    )
                    directories = directories[:index]
                    break  # A previous remove may have finished before its reply was lost.
                handles.append(descriptor)
                require(index < len(directories), "native_bootstrap_unconfirmed")
            check_directory(index)
            if mode != "create" and index + 1 == len(directories) and index < 4:
                try:
                    os.stat(names[index], dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    break
                raise NativeError("native_bootstrap_unconfirmed")
        target_name = user + ".target"
        mask_name = "gpg-agent-ssh.socket"
        socket_name = "dbus.socket.d"
        socket_leaf = "muli-runtime.conf"
        if len(handles) == 5:
            if mode == "create":
                for name, data in ((target_name, bootstrap_data()), (mask_name, b"")):
                    charge(data)
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o644,
                        dir_fd=handles[-1],
                    )
                    with os.fdopen(descriptor, "wb") as output:
                        os.fchmod(output.fileno(), 0o644)
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                        info = os.fstat(output.fileno())
                        if name == target_name:
                            identity = [info.st_dev, info.st_ino]
                        else:
                            mask_identity = [info.st_dev, info.st_ino]

            if mode == "create" and "socket_directory" in value:
                os.mkdir(socket_name, mode=0o700, dir_fd=handles[-1])
                socket_descriptor = os.open(socket_name, flags, dir_fd=handles[-1])
                os.fchmod(socket_descriptor, 0o700)
                os.fchown(socket_descriptor, uid, gid)
                info = os.fstat(socket_descriptor)
                socket_directory = [info.st_dev, info.st_ino]
                data = bootstrap_socket_data(uid)
                charge(data)
                descriptor = os.open(
                    socket_leaf,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o644,
                    dir_fd=socket_descriptor,
                )
                with os.fdopen(descriptor, "wb") as output:
                    os.fchmod(output.fileno(), 0o644)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    info = os.fstat(output.fileno())
                    socket_file = [info.st_dev, info.st_ino]
            elif socket_directory is not None:
                try:
                    socket_descriptor = os.open(socket_name, flags, dir_fd=handles[-1])
                except FileNotFoundError:
                    require(mode == "remove", "native_bootstrap_changed")
                    socket_directory = socket_file = None

            def check_socket_directory() -> None:
                try:
                    linked = os.stat(socket_name, dir_fd=handles[-1], follow_symlinks=False)
                except FileNotFoundError:
                    require(socket_directory is None, "native_bootstrap_changed")
                    return
                require(
                    socket_directory is not None and socket_descriptor is not None,
                    "native_bootstrap_unconfirmed",
                )
                assert socket_descriptor is not None
                current = os.fstat(socket_descriptor)
                require(
                    stat.S_ISDIR(current.st_mode)
                    and stat.S_IMODE(current.st_mode) == 0o700
                    and (current.st_uid, current.st_gid) == (uid, gid)
                    and [current.st_dev, current.st_ino] == socket_directory
                    and all(
                        getattr(linked, key) == getattr(current, key)
                        for key in ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")
                    ),
                    "native_bootstrap_changed",
                )
                # An unowned sibling must block all removals, including target/mask.
                with os.scandir(socket_descriptor) as entries:
                    first = next(entries, None)
                    require(
                        (first is None or first.name == socket_leaf)
                        and next(entries, None) is None,
                        "native_bootstrap_unconfirmed",
                    )

            check_socket_directory()
            leaves: list[tuple[int, str, os.stat_result | None]] = []
            materials = [
                (handles[-1], target_name, identity, bootstrap_data()),
                (handles[-1], mask_name, mask_identity, b""),
            ]
            if socket_descriptor is not None:
                materials.append(
                    (socket_descriptor, socket_leaf, socket_file, bootstrap_socket_data(uid))
                )
            for directory_fd, name, expected, content in materials:
                try:
                    leaf = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    require(mode == "remove" or expected is None, "native_bootstrap_changed")
                    leaves.append((directory_fd, name, None))
                    continue
                require(
                    expected is not None
                    and [leaf.st_dev, leaf.st_ino] == expected
                    and stat.S_ISREG(leaf.st_mode)
                    and stat.S_IMODE(leaf.st_mode) == 0o644
                    and (leaf.st_uid, leaf.st_gid) == (0, 0),
                    "native_bootstrap_changed",
                )
                # The shared reader may consume one extra byte after fstat to
                # detect growth. Keep that probe inside this existing raw grant.
                require(len(content) < limit - used, "native_diagnostic_output_budget")
                data = _read_regular(
                    Path(name), min(limit - used - 1, len(content)), directory_fd=directory_fd
                )
                charge(data)
                require(data == content, "native_bootstrap_changed")
                leaves.append((directory_fd, name, leaf))

            # All leaves must be safe before deleting any one. Recheck the
            # complete set after all reads, including identity and content size.
            def check_leaves() -> None:
                check_socket_directory()
                for directory_fd, name, leaf in leaves:
                    try:
                        linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        require(leaf is None, "native_bootstrap_changed")
                        continue
                    require(
                        leaf is not None
                        and all(
                            getattr(linked, key) == getattr(leaf, key)
                            for key in (
                                "st_dev",
                                "st_ino",
                                "st_mode",
                                "st_uid",
                                "st_gid",
                                "st_size",
                                "st_mtime_ns",
                                "st_ctime_ns",
                            )
                        ),
                        "native_bootstrap_changed",
                    )

            check_leaves()
            for index in range(len(handles)):
                check_directory(index)
            if mode == "remove":
                check_leaves()
                for directory_fd, name, saved_leaf in leaves:
                    if saved_leaf is not None:
                        remaining(deadline)
                        os.unlink(name, dir_fd=directory_fd)
                if socket_descriptor is not None:
                    check_socket_directory()
                    remaining(deadline)
                    os.rmdir(socket_name, dir_fd=handles[-1])
                socket_directory = socket_file = None
                identity = None
                mask_identity = None
        if mode == "remove":
            for index in range(len(handles) - 1, 1, -1):
                remaining(deadline)
                check_directory(index)
                with os.scandir(handles[index]) as entries:
                    if next(entries, None) is not None:
                        break  # Retain nonempty directories; never remove other entries.
                os.rmdir(names[index - 1], dir_fd=handles[index - 1])
                directories.pop()
            identity = None
            mask_identity = None
            socket_directory = socket_file = None
        for index in range(len(directories)):
            check_directory(index)
        account = pwd.getpwnam(user)
        require(
            (account.pw_uid, account.pw_gid, account.pw_dir) == (uid, gid, str(volume / "home")),
            "native_bootstrap_identity",
        )
        remaining(deadline)
        return {
            "code": "verified",
            "directories": directories,
            "file": identity,
            "mask": mask_identity,
            "socket_directory": socket_directory,
            "socket_file": socket_file,
            "used": used,
        }
    except Exception as error:
        code = str(error) if isinstance(error, NativeError) else "native_bootstrap_invalid"
        return {"code": code, "used": limit}
    finally:
        if socket_descriptor is not None:
            os.close(socket_descriptor)
        for descriptor in reversed(handles):
            os.close(descriptor)


async def manager_template(
    user: str, uid: int, home: Path, deadline: float, limit: int
) -> dict[str, Any]:
    """Check the idle instance's typed command before any instance ExecStart override."""
    import pwd

    from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe, _typed

    require(re.fullmatch(r"muli-ci-[a-f0-9]{12}", user) is not None, "native_manager_identity")
    account = pwd.getpwnam(user)
    require((account.pw_uid, account.pw_dir) == (uid, str(home)), "native_manager_identity")
    require(
        math.isfinite(deadline) and remaining(deadline) <= HARNESS_SECONDS + CLEANUP_SECONDS,
        "native_manager_deadline",
    )
    io = _SetupIO(deadline, limit, {})
    try:
        async with asyncio.timeout(remaining(deadline)):
            probe = SystemdProbe(
                runner=io.query,
                limits=ProbeLimits(seconds=CLEANUP_SECONDS, command_bytes=MAX_BYTES),
            )
            name = f"user@{uid}.service"
            # Load only metadata for the already-confirmed idle instance. No StartUnit/activation.
            loaded = _typed(
                await probe._command(
                    "call",
                    "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager",
                    "LoadUnit",
                    "s",
                    name,
                ),
                "o",
            )
            require(
                isinstance(loaded, list) and len(loaded) == 1 and isinstance(loaded[0], str),
                "native_manager_template",
            )
            path = loaded[0]
            require(
                re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", path) is not None,
                "native_manager_template",
            )
            before = await probe._properties(
                path,
                "Unit",
                (("Id", "s"), ("ActiveState", "s"), ("SubState", "s"), ("Job", "(uo)")),
            )
            fields = (
                ("MainPID", "u"),
                ("ExecStartEx", "a(sasasttttuii)"),
            )
            service = await probe._properties(path, "Service", fields)
            program = system_executables().get("manager")
            if program is None:
                raise NativeError("native_manager_template")
            program_path = Path(program)

            def check_idle(value: dict[str, Any]) -> None:
                require(
                    value
                    == {"Id": name, "ActiveState": "inactive", "SubState": "dead", "Job": [0, "/"]}
                    and isinstance(value["Job"], list)
                    and type(value["Job"][0]) is int,
                    "native_manager_template",
                )

            def check_command(value: dict[str, Any]) -> None:
                entries = value["ExecStartEx"]
                require(
                    isinstance(entries, list)
                    and len(entries) == 1
                    and isinstance(entries[0], list)
                    and len(entries[0]) == 10,
                    "native_manager_template",
                )
                entry = entries[0]
                require(
                    isinstance(entry[0], str)
                    and Path(entry[0]).resolve(strict=True) == program_path
                    and entry[1] == [entry[0], "--user"]
                    and entry[2] == []
                    and all(type(item) is int and 0 <= item < 2**64 for item in entry[3:7])
                    and type(entry[7]) is int
                    and 0 <= entry[7] < 2**32
                    and all(type(item) is int and -(2**31) <= item < 2**31 for item in entry[8:])
                    and type(value["MainPID"]) is int
                    and value["MainPID"] == 0,
                    "native_manager_template",
                )

            check_idle(before)
            check_command(service)
            after = await probe._properties(
                path,
                "Unit",
                (("Id", "s"), ("ActiveState", "s"), ("SubState", "s"), ("Job", "(uo)")),
            )
            final_service = await probe._properties(path, "Service", fields)
            check_idle(after)
            check_command(final_service)
            require(after == before and final_service == service, "native_manager_template")
            remaining(deadline)
            account = pwd.getpwnam(user)
            require((account.pw_uid, account.pw_dir) == (uid, str(home)), "native_manager_identity")
            return {"code": "verified", "manager": program, "used": io.used}
    except (Exception, asyncio.CancelledError):
        return {"code": "native_manager_template", "used": io.limit}


def process_unavailable(code: str = "not_started") -> dict[str, Any]:
    return {
        "code": code,
        "exe": "unavailable",
        "uid_matches": None,
        "state": "",
        "wait": "unavailable",
    }


def process_wait(first: bytes, last: bytes) -> str:
    """Classify exact known kernel symbols without exporting their raw text."""
    first, last = first.removesuffix(b"\n"), last.removesuffix(b"\n")
    if first != last or re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_.]*", first) is None:
        return "unavailable"
    return {
        b"do_wait": "child_wait",
        b"ep_poll": "epoll_wait",
        b"do_epoll_wait": "epoll_wait",
    }.get(first, "other")


async def observe_setup_process(
    probe: SystemdProbe,
    path: str,
    uid: int,
    pid: int,
    group: str,
    read: Callable[[int, str], Awaitable[bytes | None]],
    deadline: float,
    *,
    observe_wait: bool = True,
) -> dict[str, Any]:
    """A bounded observation window, not an atomic snapshot or process authority."""
    from muli.platforms.linux_transaction import _cgroup, _start_ticks

    if pid == 0:
        return process_unavailable("pid_absent")
    try:
        require(0 < pid < 2**31 and bool(_cgroup(group)), "native_diagnostic_invalid")
        binding = {"MainPID": pid, "ControlGroup": group}
        fields = (("MainPID", "u"), ("ControlGroup", "s"))
        before = await probe._properties(path, "Service", fields)
        require(before == binding, "native_process_changed")

        async def required(name: str) -> bytes:
            value = await read(pid, name)
            if value is None:
                raise OSError
            return value

        async def cgroup() -> str:
            value = (await required("cgroup")).decode("ascii")
            lines = value.splitlines()
            require(len(lines) == 1 and lines[0].startswith("0::"), "native_diagnostic_invalid")
            current = _cgroup(lines[0][3:])
            require(current == group or current.startswith(group + "/"), "native_process_changed")
            return current

        async def uids() -> tuple[int, ...]:
            rows = [
                row[4:].split()
                for row in (await required("status")).splitlines()
                if row.startswith(b"Uid:")
            ]
            require(
                len(rows) == 1 and len(rows[0]) == 4 and all(item.isdigit() for item in rows[0]),
                "native_diagnostic_invalid",
            )
            values = tuple(int(item) for item in rows[0])
            require(all(0 <= item < 2**32 for item in values), "native_diagnostic_invalid")
            return values

        async def image() -> str:
            value = (await required("exe")).decode("utf-8").removesuffix("\n")
            require(
                value.startswith("/") and not any(item in value for item in ("\n", "\r", "\0")),
                "native_diagnostic_invalid",
            )
            return value

        first = await required("stat")
        ticks = _start_ticks(first, pid)
        first_group = await cgroup()
        first_image = await image()
        first_uids = await uids()
        last_uids = await uids()
        last_image = await image()
        last_group = await cgroup()
        last = await required("stat")
        last_ticks = _start_ticks(last, pid)
        after = await probe._properties(path, "Service", fields)
        require(
            ticks == last_ticks
            and before == after
            and first_group == last_group
            and first_image == last_image
            and first_uids == last_uids,
            "native_process_changed",
        )
        state = last.rsplit(b")", 1)[1].split()[0].decode("ascii")
        require(state in PROCESS_STATES, "native_diagnostic_invalid")
        known = system_executables()
        remaining(deadline)
        result = {
            "code": "observed",
            "exe": next((kind for kind, value in known.items() if value == last_image), "other"),
            "uid_matches": all(value == uid for value in last_uids),
            "state": state,
            "wait": "unavailable",
        }
        if not observe_wait:
            return result
        # Preserve the completed identity observation if this optional read fails.
        # Its final gate extends the same window and uses the same absolute deadline.
        try:
            first_wait = await required("wchan")
            last_wait = await required("wchan")
            wait = process_wait(first_wait, last_wait)
            if wait == "unavailable":
                return result
            wait_uids = await uids()
            wait_image = await image()
            wait_group = await cgroup()
            wait_ticks = _start_ticks(await required("stat"), pid)
            wait_binding = await probe._properties(path, "Service", fields)
            require(
                last_uids == wait_uids
                and last_image == wait_image
                and last_group == wait_group
                and last_ticks == wait_ticks
                and after == wait_binding,
                "native_process_changed",
            )
            remaining(deadline)
            result["wait"] = wait
        except (OSError, ValueError):
            pass
        return result
    except TimeoutError:
        return process_unavailable("timeout")
    except OSError:
        return process_unavailable("unavailable")
    except ValueError as error:
        code = {
            "native_deadline_exceeded": "timeout",
            "native_diagnostic_output_budget": "output_budget",
            "native_process_changed": "identity_changed",
        }.get(str(error), "invalid_reply")
        return process_unavailable(code)


def journal_unavailable(code: str = "not_started") -> dict[str, Any]:
    return {"code": code, "incomplete": False, "events": []}


def context_unavailable(code: str = "not_started") -> dict[str, str]:
    return dict.fromkeys(
        (
            "notify_env",
            "runtime_env",
            "bus_env",
            "listen_env",
            "parent_notify",
            "user_notify",
            "private_socket",
        ),
        "unavailable",
    ) | {"code": code}


def notification_unavailable(code: str = "not_started") -> dict[str, str]:
    return dict.fromkeys(
        (
            "status_text",
            "notify_access",
            "service_type",
            "result",
            "active_state_after",
            "sub_state_after",
        ),
        "unavailable",
    ) | {"code": code}


def notification_projection(service: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    """Keep fixed PID1 labels, never STATUS text or a claim that READY arrived."""
    require(
        set(service) == {"StatusText", "NotifyAccess", "Type", "Result"}
        and set(state) == {"ActiveState", "SubState"}
        and all(isinstance(value, str) for value in (*service.values(), *state.values())),
        "native_diagnostic_invalid",
    )

    def label(value: str, allowed: set[str]) -> str:
        return value if value in allowed else "other"

    return {
        "code": "observed",
        "status_text": {
            "": "empty",
            "Reached basic.target.": "reached_basic",
            "Ready.": "ready",
        }.get(service["StatusText"], "other"),
        "notify_access": label(service["NotifyAccess"], {"none", "main", "exec", "all"}),
        "service_type": label(service["Type"], {"notify", "notify-reload"}),
        "result": label(service["Result"], {"success"}),
        "active_state_after": label(
            state["ActiveState"],
            {
                "active",
                "activating",
                "deactivating",
                "inactive",
                "failed",
                "reloading",
                "maintenance",
            },
        ),
        "sub_state_after": label(
            state["SubState"], {"start", "start-post", "running", "exited", "dead", "failed"}
        ),
    }


def context_projection(value: object) -> dict[str, str]:
    allowed = {
        "code": {
            "observed",
            "not_started",
            "unavailable",
            "identity_changed",
            "invalid_reply",
            "timeout",
            "output_budget",
        },
        "notify_env": {"parent", "user", "missing", "other", "unavailable"},
        "runtime_env": {"expected", "missing", "other", "unavailable"},
        "bus_env": {"expected", "missing", "other", "unavailable"},
        "listen_env": {"present", "absent", "unavailable"},
        "parent_notify": {"socket_owner_matches", "absent", "unexpected", "unavailable"},
        "user_notify": {"socket_owner_matches", "absent", "unexpected", "unavailable"},
        "private_socket": {"socket_owner_matches", "absent", "unexpected", "unavailable"},
    }
    if not isinstance(value, dict):
        raise NativeError("native_diagnostic_invalid")
    require(set(value) == set(allowed), "native_diagnostic_invalid")
    require(
        all(
            isinstance(value[key], str) and value[key] in choices
            for key, choices in allowed.items()
        ),
        "native_diagnostic_invalid",
    )
    require(
        value["code"] == "observed" or value == context_unavailable(value["code"]),
        "native_diagnostic_invalid",
    )
    return {key: value[key] for key in allowed}


def context_environment(data: bytes, uid: int) -> dict[str, str]:
    """Classify only initial exec environment; no arbitrary value can escape."""
    require(not data or data.endswith(b"\0"), "native_diagnostic_invalid")
    selected = {
        b"NOTIFY_SOCKET",
        b"XDG_RUNTIME_DIR",
        b"DBUS_SESSION_BUS_ADDRESS",
        b"LISTEN_PID",
        b"LISTEN_FDS",
        b"LISTEN_FDNAMES",
    }
    values: dict[bytes, bytes] = {}
    for row in data.split(b"\0")[:-1]:
        key, separator, value = row.partition(b"=")
        require(bool(key) and bool(separator), "native_diagnostic_invalid")
        if key in selected:
            require(key not in values, "native_diagnostic_invalid")
            values[key] = value
    runtime = f"/run/user/{uid}".encode("ascii")

    def match(key: bytes, expected: bytes) -> str:
        return (
            "missing" if key not in values else "expected" if values[key] == expected else "other"
        )

    notify = values.get(b"NOTIFY_SOCKET")
    return {
        "notify_env": "missing"
        if notify is None
        else (
            "parent"
            if notify == b"/run/systemd/notify"
            else "user"
            if notify == runtime + b"/systemd/notify"
            else "other"
        ),
        "runtime_env": match(b"XDG_RUNTIME_DIR", runtime),
        "bus_env": match(b"DBUS_SESSION_BUS_ADDRESS", b"unix:path=" + runtime + b"/bus"),
        "listen_env": "present" if any(key.startswith(b"LISTEN_") for key in values) else "absent",
    }


def context_socket(path: Path, owner: int) -> str:
    """Inspect metadata only, rejecting symlinks in every directory component."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        return (
            "socket_owner_matches"
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == owner
            else "unexpected"
        )
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unavailable"
    finally:
        os.close(descriptor)


def pending_job_unavailable() -> dict[str, str]:
    return {"code": "unavailable", "identifier": "", "type": ""}


def pending_job_projection(value: object) -> dict[str, str]:
    """Extract a safe STATUS identifier hint, not a verified unit ID or raw text."""
    other = {"code": "other", "identifier": "", "type": ""}
    if not isinstance(value, str) or len(value) > 512 or not value.isascii():
        return other
    fixed = {"": "empty", "Reached basic.target.": "reached_basic", "Ready.": "ready"}
    if value in fixed:
        return {"code": fixed[value], "identifier": "", "type": ""}
    # v255 manager.c::manager_print_jobs_in_progress, not console status prose.
    match = re.fullmatch(
        r"(?:\(([1-9][0-9]{0,9}) of ([1-9][0-9]{0,9})\) )?"
        r"User job ([A-Za-z0-9_:@.\\-]{1,128})/([a-z-]{1,15}) running "
        r"\([0-9A-Za-z. ]{1,64} / [0-9A-Za-z. ]{1,64}\)\.\.\.",
        value,
    )
    if match is None:
        return other
    index, total, unit, job_type = match.groups()
    if index is not None and not (1 < int(total) < 2**32 and int(index) <= int(total)):
        return other
    if not pending_job_facts({"code": "user_job", "identifier": unit, "type": job_type}):
        return other
    return {"code": "user_job", "identifier": unit, "type": job_type}


def pending_job_facts(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"code", "identifier", "type"}:
        return False
    if value["code"] != "user_job":
        return (
            value["code"] in ("empty", "reached_basic", "ready", "other", "unavailable")
            and value["identifier"] == value["type"] == ""
        )
    unit = value["identifier"]
    return (
        isinstance(unit, str)
        and len(unit) <= 128
        and re.fullmatch(
            r"(?:[A-Za-z0-9_:@.-]|\\x[0-9a-fA-F]{2})+"
            r"\.(?:service|socket|target|timer|path|slice|scope|device|mount|automount|swap)",
            unit,
        )
        is not None
        and value["type"]
        in (
            "start",
            "verify-active",
            "stop",
            "reload",
            "reload-or-start",
            "restart",
            "try-restart",
            "try-reload",
            "nop",
        )
    )


def instance_projection(value: object) -> dict[str, Any]:
    require(
        isinstance(value, dict)
        and set(value)
        in (
            {"code", "reason", "exe", "uids", "used"},
            {"code", "reason", "exe", "uids", "used", "internal"},
        ),
        "native_diagnostic_invalid",
    )
    assert isinstance(value, dict)
    require(
        type(value["used"]) is int
        and 0 <= value["used"] <= MAX_BYTES
        and (
            (
                value["code"] == "observed"
                and value["reason"] == "none"
                and value["exe"] in ("manager", "executor", "other")
                and value["uids"] in ("all_expected", "mismatch")
            )
            or (
                value["code"] == "not_observed"
                and value["reason"]
                in ("timeout", "output_budget", "identity_changed", "invalid_reply", "unavailable")
                and value["exe"] == "unavailable"
                and value["uids"] == "unavailable"
            )
        ),
        "native_diagnostic_invalid",
    )
    internal = value.get("internal", "not_observed")
    require(
        internal == "not_observed"
        or (
            value["code"] == "observed"
            and value["exe"] == "manager"
            and value["uids"] == "all_expected"
            and peer_facts(internal, control=True)
            and isinstance(internal, dict)
            and internal["reason"] != "identity_changed"
        ),
        "native_diagnostic_invalid",
    )
    return dict(value)


def control_unavailable(code: str = "not_observed") -> dict[str, Any]:
    return {
        "code": code,
        "start_ticks": None,
        "exe": "unavailable",
        "uids": "unavailable",
        "env": "---",
        "cmd": "unavailable",
    }


def control_facts(value: object) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"code", "start_ticks", "exe", "uids", "env", "cmd"}
        or not all(isinstance(value[key], str) for key in ("code", "exe", "uids", "env", "cmd"))
    ):
        return False
    if value["code"] != "observed":
        return value["code"] in {"absent", "not_observed"} and value == control_unavailable(
            value["code"]
        )
    return (
        type(value["start_ticks"]) is int
        and 0 <= value["start_ticks"] < 2**64
        and value["exe"] in {"systemctl", "other"}
        and value["uids"] == "all_expected"
        and re.fullmatch(r"[emod]{2}[10mod]", value["env"]) is not None
        and value["cmd"] in {"set_bus", "set_other", "other"}
        and (value["exe"] == "systemctl" or value["cmd"] == "other")
    )


def peer_facts(value: object, *, control: bool = False) -> bool:
    """Current fixed result only; old basic/jobs are artifact-readback facts."""
    keys = {"code", "reason", "sub_state", "control_pid"} | ({"control"} if control else set())
    if not isinstance(value, dict) or set(value) != keys:
        return False
    observed = (
        value["code"] == "observed"
        and value["reason"] == "none"
        and isinstance(value["sub_state"], str)
        and value["sub_state"] in SOCKET_SUB_STATES
        and type(value["control_pid"]) is int
        and 0 <= value["control_pid"] < 2**31
    )
    unavailable = (
        value["code"] == "not_observed"
        and value["reason"] in ("timeout", "identity_changed", "invalid_reply", "unavailable")
        and value["sub_state"] is None
        and value["control_pid"] is None
    )
    if not (observed or unavailable):
        return False
    if not control:
        return True
    child = value["control"]
    return control_facts(child) and child["code"] == (
        "not_observed" if unavailable else "absent" if value["control_pid"] == 0 else "observed"
    )


def instance_unavailable(reason: str, used: int) -> dict[str, Any]:
    return {
        "code": "not_observed",
        "reason": reason,
        "exe": "unavailable",
        "uids": "unavailable",
        "used": used,
    }


def instance_error(error: BaseException) -> str:
    from muli.process import ProcessOutputLimitError

    if isinstance(error, ProcessOutputLimitError):
        return "output_budget"
    if isinstance(error, TimeoutError):
        return "timeout"
    return {
        "native_process_changed": "identity_changed",
        "native_deadline_exceeded": "timeout",
        "native_diagnostic_output_budget": "output_budget",
    }.get(str(error), "unavailable" if isinstance(error, OSError) else "invalid_reply")


def manager_peer(pid: int, uid: int, deadline: float) -> PeerFacts:
    if __package__:
        from .managed_native_peer import read_manager_peer

        return read_manager_peer(pid, uid, deadline)
    import importlib.util

    name = "_muli_managed_native_peer"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().with_name("managed_native_peer.py")
        )
        require(spec is not None and spec.loader is not None, "native_diagnostic_invalid")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return cast("PeerFacts", module.read_manager_peer(pid, uid, deadline))


def read_manager_instance(
    pid: int,
    uid: int,
    ticks: int,
    group: str,
    deadline: float,
    limit: int,
) -> dict[str, Any]:
    """Bracket the one fixed peer observation with both bounded proc bindings."""
    from muli.platforms.linux_transaction import _cgroup, _read_regular, _start_ticks

    used = 0
    try:
        require(
            os.geteuid() == 0
            and type(pid) is int
            and 0 < pid < 2**31
            and type(uid) is int
            and 0 < uid < 2**31
            and type(ticks) is int
            and 0 <= ticks < 2**64
            and bool(_cgroup(group))
            and Path(group).name == f"user@{uid}.service"
            and type(limit) is int
            and 0 < limit <= MAX_BYTES,
            "native_diagnostic_invalid",
        )
        require(
            math.isfinite(deadline) and remaining(deadline) <= CLEANUP_SECONDS,
            "native_diagnostic_invalid",
        )
        known = system_executables()

        def read(root: Path, name: str, *, maximum: int | None = None) -> bytes:
            nonlocal used
            remaining(deadline)
            require(used < limit, "native_diagnostic_output_budget")
            allowance = limit - used
            if maximum is not None:
                # The regular-file reader may fetch one extra overflow byte.
                # Reserve it within this same grant; failure consumes the grant.
                require(allowance > 1, "native_diagnostic_output_budget")
                allowance = min(maximum, allowance - 1)
            data = _read_regular(root / name, allowance)
            used += len(data)
            require(used <= limit, "native_diagnostic_output_budget")
            remaining(deadline)
            return data

        def binding(
            selected_pid: int, expected_ticks: int | None = None
        ) -> tuple[int, tuple[int, ...], str, str]:
            nonlocal used
            root = Path("/proc") / str(selected_pid)
            start = _start_ticks(read(root, "stat"), selected_pid)
            rows = [
                row.split()[1:]
                for row in read(root, "status").splitlines()
                if row.startswith(b"Uid:")
            ]
            require(
                len(rows) == 1
                and len(rows[0]) == 4
                and all(re.fullmatch(rb"[0-9]{1,10}", item) for item in rows[0]),
                "native_diagnostic_invalid",
            )
            identities = tuple(int(item) for item in rows[0])
            require(all(item < 2**32 for item in identities), "native_diagnostic_invalid")
            groups = read(root, "cgroup").decode("ascii").splitlines()
            require(len(groups) == 1 and groups[0].startswith("0::"), "native_diagnostic_invalid")
            current = _cgroup(groups[0][3:])
            require(current == group or current.startswith(group + "/"), "native_process_changed")
            require(used < limit, "native_diagnostic_output_budget")
            image = os.readlink(root / "exe")
            used += len(os.fsencode(image))
            require(used <= limit, "native_diagnostic_output_budget")
            remaining(deadline)
            require(expected_ticks is None or start == expected_ticks, "native_process_changed")
            require(
                image.startswith("/") and not any(char in image for char in "\x00\r\n"),
                "native_diagnostic_invalid",
            )
            return start, identities, current, image

        before = binding(pid, ticks)
        internal: dict[str, Any] | str = "not_observed"
        if known.get("manager") == before[3] and before[1] == (uid,) * 4:
            require(used < limit, "native_diagnostic_output_budget")
            remaining(deadline)
            from dataclasses import asdict

            internal = asdict(manager_peer(pid, uid, deadline))
            require(peer_facts(internal), "native_diagnostic_invalid")
            require(internal["reason"] != "identity_changed", "native_process_changed")
            child = control_unavailable()
            if internal["code"] == "observed":
                child_pid = internal["control_pid"]
                if child_pid == 0:
                    child = control_unavailable("absent")
                else:
                    require(child_pid != pid, "native_process_changed")
                    child_before = binding(child_pid)
                    require(child_before[1] == (uid,) * 4, "native_process_changed")
                    environment = read(Path("/proc") / str(child_pid), "environ")
                    require(
                        not environment or environment.endswith(b"\x00"),
                        "native_diagnostic_invalid",
                    )
                    selected: dict[bytes, list[bytes]] = {
                        b"XDG_RUNTIME_DIR": [],
                        b"DBUS_SESSION_BUS_ADDRESS": [],
                        b"SYSTEMCTL_FORCE_BUS": [],
                    }
                    for row in environment.split(b"\x00"):
                        name, separator, value = row.partition(b"=")
                        if separator and name in selected:
                            # Keep at most two bounded references; duplicates
                            # are a fixed classification, not a last-wins value.
                            if len(selected[name]) < 2:
                                selected[name].append(value)

                    def classified(name: bytes) -> str:
                        values = selected[name]
                        if not values:
                            return "m"
                        if len(values) != 1:
                            return "d"
                        value = values[0]
                        runtime = f"/run/user/{uid}".encode("ascii")
                        if name in {b"XDG_RUNTIME_DIR", b"DBUS_SESSION_BUS_ADDRESS"}:
                            expected = (
                                runtime
                                if name == b"XDG_RUNTIME_DIR"
                                else b"unix:path=" + runtime + b"/bus"
                            )
                            return "e" if value == expected else "o"
                        # v255 parse_boolean accepts these case-insensitive words.
                        if value.lower() in {b"1", b"yes", b"y", b"true", b"t", b"on"}:
                            return "1"
                        if value.lower() in {b"0", b"no", b"n", b"false", b"f", b"off"}:
                            return "0"
                        return "o"

                    command = read(Path("/proc") / str(child_pid), "cmdline", maximum=512)
                    require(command.endswith(b"\x00"), "native_diagnostic_invalid")
                    arguments = command[:-1].split(b"\x00")
                    require(all(arguments), "native_diagnostic_invalid")
                    image = "systemctl" if systemctl_executable() == child_before[3] else "other"
                    label = "other"
                    if (
                        image == "systemctl"
                        and len(arguments) == 4
                        and arguments[0].rsplit(b"/", 1)[-1] == b"systemctl"
                        and arguments[1:3] == [b"--user", b"set-environment"]
                        and arguments[3].startswith(b"DBUS_SESSION_BUS_ADDRESS=")
                    ):
                        expected = f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus".encode(
                            "ascii"
                        )
                        label = "set_bus" if arguments[3] == expected else "set_other"
                    require(child_before == binding(child_pid), "native_process_changed")
                    child = {
                        "code": "observed",
                        "start_ticks": child_before[0],
                        "exe": image,
                        "uids": "all_expected",
                        "env": "".join(classified(name) for name in selected),
                        "cmd": label,
                    }
            internal["control"] = child
            require(peer_facts(internal, control=True), "native_diagnostic_invalid")
        require(before == binding(pid, ticks), "native_process_changed")
        remaining(deadline)
        image = before[3]
        return {
            "code": "observed",
            "reason": "none",
            "exe": next(
                (key for key in ("manager", "executor") if known.get(key) == image), "other"
            ),
            "uids": "all_expected" if before[1] == (uid,) * 4 else "mismatch",
            "used": used,
            "internal": internal,
        }
    except Exception as error:
        # A partial/failed read has no reusable allowance; retain no positive tags.
        return instance_unavailable(instance_error(error), limit)


def read_setup_context(
    pid: int,
    uid: int,
    ticks: int,
    group: str,
    deadline: float,
    limit: int,
) -> dict[str, Any]:
    """One privileged projection. Raw proc bytes never leave this process."""
    from muli.platforms.linux_transaction import _cgroup, _read_regular, _start_ticks

    used = 0
    try:
        require(
            os.geteuid() == 0
            and 0 < pid < 2**31
            and 0 < uid < 2**31
            and ticks > 0
            and bool(_cgroup(group))
            and 0 < limit <= MAX_BYTES,
            "native_diagnostic_invalid",
        )
        require(
            math.isfinite(deadline) and remaining(deadline) <= CLEANUP_SECONDS,
            "native_diagnostic_invalid",
        )
        root = Path("/proc") / str(pid)

        def read(name: str) -> bytes:
            nonlocal used
            remaining(deadline)
            require(used < limit, "native_diagnostic_output_budget")
            data = _read_regular(root / name, limit - used)
            used += len(data)
            require(used <= limit, "native_diagnostic_output_budget")
            return data

        def binding() -> tuple[int, bytes, bytes, str]:
            nonlocal used
            start = _start_ticks(read("stat"), pid)
            status = [row for row in read("status").splitlines() if row.startswith(b"Uid:")]
            require(len(status) == 1, "native_diagnostic_invalid")
            require(status[0].split()[1:] == [str(uid).encode()] * 4, "native_process_changed")
            raw_group = read("cgroup")
            rows = raw_group.decode("ascii").splitlines()
            require(len(rows) == 1 and rows[0].startswith("0::"), "native_diagnostic_invalid")
            current = _cgroup(rows[0][3:])
            require(current == group or current.startswith(group + "/"), "native_process_changed")
            image = os.readlink(root / "exe")
            used += len(os.fsencode(image))
            require(used <= limit, "native_diagnostic_output_budget")
            require(
                start == ticks and image == system_executables().get("manager"),
                "native_process_changed",
            )
            return start, status[0], raw_group, image

        before = binding()
        facts = context_environment(read("environ"), uid)
        runtime = Path("/run/user") / str(uid) / "systemd"
        facts.update(
            parent_notify=context_socket(Path("/run/systemd/notify"), 0),
            user_notify=context_socket(runtime / "notify", uid),
            private_socket=context_socket(runtime / "private", uid),
        )
        require(before == binding(), "native_process_changed")
        remaining(deadline)
        return {"facts": {"code": "observed", **facts}, "used": used}
    except Exception as error:
        code = {
            "native_process_changed": "identity_changed",
            "native_deadline_exceeded": "timeout",
            "native_diagnostic_output_budget": "output_budget",
        }.get(str(error), "unavailable" if isinstance(error, OSError) else "invalid_reply")
        # Failed reads have no trustworthy byte count; never recycle their allowance.
        return {"facts": context_unavailable(code), "used": limit}


async def observe_setup_context(
    probe: SystemdProbe,
    path: str,
    uid: int,
    pid: int,
    group: str,
    read: Callable[[int, str], Awaitable[bytes | None]],
    io: _SetupIO,
    deadline: float,
) -> tuple[dict[str, str], dict[str, str]]:
    from muli.platforms.linux_transaction import _start_ticks

    try:

        async def binding() -> tuple[dict[str, Any], dict[str, Any], int, dict[str, Any]]:
            state = await probe._properties(
                path,
                "Unit",
                (("Id", "s"), ("InvocationID", "ay"), ("ActiveState", "s"), ("SubState", "s")),
            )
            invocation = state["InvocationID"]
            require(
                state["Id"] == f"user@{uid}.service"
                and isinstance(invocation, list)
                and len(invocation) == 16
                and any(invocation)
                and all(type(item) is int and 0 <= item <= 255 for item in invocation),
                "native_diagnostic_invalid",
            )
            service = await probe._properties(
                path, "Service", (("MainPID", "u"), ("ControlGroup", "s"))
            )
            require(service == {"MainPID": pid, "ControlGroup": group}, "native_process_changed")
            raw = await read(pid, "stat")
            if raw is None:
                raise OSError
            return (
                {key: state[key] for key in ("Id", "InvocationID")},
                service,
                _start_ticks(raw, pid),
                {key: state[key] for key in ("ActiveState", "SubState")},
            )

        before = await binding()
        service = await probe._properties(
            path,
            "Service",
            (("StatusText", "s"), ("NotifyAccess", "s"), ("Type", "s"), ("Result", "s")),
        )
        notification_projection(service, before[3])
        result = await io.context(pid, uid, before[2], group)
        after = await binding()
        require(before[:3] == after[:3], "native_process_changed")
        # These state getters follow STATUS, but are separate calls, not an atomic snapshot.
        notification = notification_projection(service, after[3])
        remaining(deadline)
        return result, (
            notification
            if result["code"] == "observed"
            else notification_unavailable(result["code"])
        )
    except TimeoutError:
        code = "timeout"
    except OSError:
        code = "unavailable"
    except ValueError as error:
        code = {
            "native_process_changed": "identity_changed",
            "native_deadline_exceeded": "timeout",
            "native_diagnostic_output_budget": "output_budget",
        }.get(str(error), "invalid_reply")
    return context_unavailable(code), notification_unavailable(code)


def journal_events(output: str, uid: int, pid: int, invocation: str) -> dict[str, Any]:
    """Keep only fixed positive facts; absent or rejected rows prove no absence."""
    lines = output.splitlines()
    require(len(lines) <= JOURNAL_EVENTS + 1, "native_diagnostic_invalid")
    allowed = set(JOURNAL_FIELDS) | JOURNAL_METADATA
    events: list[dict[str, str]] = []
    unit = f"user@{uid}.service"

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            require(key not in value, "native_diagnostic_invalid")
            value[key] = item
        return value

    # journalctl emits the most recent records in chronological order. If there
    # are 33, the oldest is only a sentinel; no claim covers earlier records.
    for line in lines[-JOURNAL_EVENTS:]:
        try:
            row = json.loads(line, object_pairs_hook=unique)
        except (ValueError, RecursionError):
            continue
        if (
            not isinstance(row, dict)
            or not set(row) <= allowed
            or not JOURNAL_METADATA <= set(row)
            or any(not isinstance(value, str) for value in row.values())
            or not row["__CURSOR"]
            or not all(
                re.fullmatch(r"[0-9]{1,20}", row[key])
                for key in ("__REALTIME_TIMESTAMP", "__MONOTONIC_TIMESTAMP")
            )
            or re.fullmatch(r"[a-f0-9]{32}", row["_BOOT_ID"]) is None
        ):
            continue
        inner = row.get("USER_UNIT", "")
        if (
            row.get("_PID") == "1"
            and row.get("_UID") == "0"
            and row.get("UNIT") == unit
            and row.get("INVOCATION_ID") == invocation
        ):
            source, name = "system_manager", "manager"
        elif (
            row.get("_PID") == str(pid)
            and row.get("_UID") == str(uid)
            and row.get("_SYSTEMD_UNIT") == unit
            and row.get("_SYSTEMD_INVOCATION_ID") == invocation
        ):
            source = "user_manager"
            name = {
                "basic.target": "basic",
                "default.target": "default",
                "dbus.service": "dbus",
                "dbus.socket": "dbus",
            }.get(inner, "other")
        else:
            continue
        event = JOURNAL_MESSAGES.get(row.get("MESSAGE_ID", ""), "")
        result = row.get("JOB_RESULT", "unknown")
        if event == "user_startup_finished":
            if source != "user_manager" or any(
                key in row for key in ("USER_UNIT", "JOB_TYPE", "JOB_RESULT")
            ):
                continue
            name = "manager"
        elif event in {"starting", "started", "failed"}:
            if row.get("JOB_TYPE") != "start" or (
                source == "user_manager" and (not inner or len(inner) > 256)
            ):
                continue
            if event == "starting":
                if "JOB_RESULT" in row:
                    continue
            elif result not in JOURNAL_JOB_RESULTS or (event == "started") != (result == "done"):
                continue
        else:
            continue
        events.append({"source": source, "event": event, "unit": name, "job_result": result})
    return {
        "code": "observed" if events else "no_known_event",
        "incomplete": len(lines) > JOURNAL_EVENTS,
        "events": events,
    }


async def observe_setup_journal(
    probe: SystemdProbe,
    path: str,
    uid: int,
    pid: int,
    group: str,
    read: Callable[[int, str], Awaitable[bytes | None]],
    runner: CommandRunner,
    deadline: float,
) -> dict[str, Any]:
    """Read one exact invocation, without connecting to or activating its user bus."""
    from muli.platforms.linux_transaction import _cgroup, _start_ticks

    if pid == 0:
        return journal_unavailable("pid_absent")
    try:
        require(0 < pid < 2**31 and bool(_cgroup(group)), "native_diagnostic_invalid")
        unit = f"user@{uid}.service"

        async def binding() -> tuple[str, int, str]:
            state = await probe._properties(path, "Unit", (("Id", "s"), ("InvocationID", "ay")))
            invocation = state["InvocationID"]
            require(
                state["Id"] == unit
                and isinstance(invocation, list)
                and len(invocation) == 16
                and all(type(value) is int and 0 <= value <= 255 for value in invocation)
                and any(invocation),
                "native_diagnostic_invalid",
            )
            service = await probe._properties(
                path, "Service", (("MainPID", "u"), ("ControlGroup", "s"))
            )
            require(service == {"MainPID": pid, "ControlGroup": group}, "native_process_changed")
            raw_stat, raw_group = await read(pid, "stat"), await read(pid, "cgroup")
            if raw_stat is None or raw_group is None:
                raise OSError
            lines = raw_group.decode("ascii").splitlines()
            require(len(lines) == 1 and lines[0].startswith("0::"), "native_diagnostic_invalid")
            current_group = _cgroup(lines[0][3:])
            require(
                current_group == group or current_group.startswith(group + "/"),
                "native_process_changed",
            )
            remaining(deadline)
            return bytes(invocation).hex(), _start_ticks(raw_stat, pid), current_group

        before = await binding()
        identifiers = [f"MESSAGE_ID={value}" for value in JOURNAL_MESSAGES]
        result = await runner(
            [
                "sudo",
                "--non-interactive",
                "--",
                "journalctl",
                "--boot=0",
                "--no-pager",
                "--quiet",
                "--output=json",
                f"--lines={JOURNAL_EVENTS + 1}",
                "--output-fields=" + ",".join(JOURNAL_FIELDS),
                "_PID=1",
                "_UID=0",
                f"UNIT={unit}",
                f"INVOCATION_ID={before[0]}",
                *identifiers,
                "+",
                f"_PID={pid}",
                f"_UID={uid}",
                f"_SYSTEMD_UNIT={unit}",
                f"_SYSTEMD_INVOCATION_ID={before[0]}",
                *identifiers,
            ],
            timeout=remaining(deadline),
            limit=MAX_BYTES,
            merge_stderr=False,
        )
        if result.get("exit_code") != 0:
            return journal_unavailable("command_failed")
        output = result.get("output")
        if not isinstance(output, str):
            raise NativeError("native_diagnostic_invalid")
        observed = journal_events(output, uid, pid, before[0])
        require(before == await binding(), "native_process_changed")
        remaining(deadline)
        return observed
    except TimeoutError:
        return journal_unavailable("timeout")
    except OSError:
        return journal_unavailable("unavailable")
    except ValueError as error:
        code = {
            "native_deadline_exceeded": "timeout",
            "native_diagnostic_output_budget": "output_budget",
            "native_process_changed": "identity_changed",
        }.get(str(error), "invalid_reply")
        return journal_unavailable(code)


async def observe_setup_units(
    probe: SystemdProbe,
    uid: int,
    facts: dict[str, Any],
    read: Callable[[int, str], Awaitable[bytes | None]],
    deadline: float,
    runner: CommandRunner,
    *,
    context_io: _SetupIO | None = None,
) -> dict[str, Any]:
    """Read the two dedicated system units without depending on their user bus."""
    from muli.platforms.linux_transaction import _typed

    names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
    facts["stage"] = "manager"
    manager = await probe._properties("/org/freedesktop/systemd1", "Manager", (("Version", "s"),))
    require(
        isinstance(manager["Version"], str) and 0 < len(manager["Version"]) <= 256,
        "native_diagnostic_invalid",
    )
    facts["stage"] = "listing"
    listed = _typed(
        await probe._command(
            "call",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "ListUnitsByPatterns",
            "asas",
            "0",
            "2",
            *names,
        ),
        "a(ssssssouso)",
    )
    require(
        isinstance(listed, list)
        and len(listed) == 1
        and isinstance(listed[0], list)
        and len(listed[0]) <= len(names)
        and all(isinstance(row, list) and len(row) == 10 and row[0] in names for row in listed[0]),
        "native_diagnostic_invalid",
    )
    present = {row[0] for row in listed[0]}
    require(len(present) == len(listed[0]), "native_diagnostic_invalid")
    for name in names:
        facts["unit"] = name
        if name not in present:
            facts["units"].append({"unit": name, "code": "unit_absent"})
            continue
        facts["stage"] = "get_unit"
        path = await probe._unit_path(name)
        facts["stage"] = "unit_properties"
        state = await probe._properties(
            path,
            "Unit",
            (
                ("Id", "s"),
                ("LoadState", "s"),
                ("ActiveState", "s"),
                ("SubState", "s"),
                ("Job", "(uo)"),
            ),
        )
        facts["stage"] = "service_properties"
        service = await probe._properties(
            path,
            "Service",
            (
                ("Result", "s"),
                ("ExecMainCode", "i"),
                ("ExecMainStatus", "i"),
                ("MainPID", "u"),
                ("ControlGroup", "s"),
            ),
        )
        job, group = state["Job"], service["ControlGroup"]
        require(
            state["Id"] == name
            and isinstance(job, list)
            and len(job) == 2
            and type(job[0]) is int
            and 0 <= job[0] < 2**31
            and job[1] == (f"/org/freedesktop/systemd1/job/{job[0]}" if job[0] else "/")
            and isinstance(group, str)
            and len(group) <= 1024
            and (not group or (group.startswith("/") and ".." not in Path(group).parts)),
            "native_diagnostic_invalid",
        )
        require(
            all(
                isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value) is not None
                for value in (
                    state["LoadState"],
                    state["ActiveState"],
                    state["SubState"],
                    service["Result"],
                )
            )
            and all(
                type(value) is int and 0 <= value < 2**31
                for value in (
                    service["ExecMainCode"],
                    service["ExecMainStatus"],
                    service["MainPID"],
                )
            ),
            "native_diagnostic_invalid",
        )
        facts["units"].append(
            {
                "unit": name,
                "code": "observed",
                "load_state": state["LoadState"],
                "active_state": state["ActiveState"],
                "sub_state": state["SubState"],
                "result": service["Result"],
                "exec_code": service["ExecMainCode"],
                "exec_status": service["ExecMainStatus"],
                "pid": service["MainPID"],
                "job": job[0],
                "cgroup_present": bool(group),
            }
        )
        if name == names[-1]:
            facts["process"] = await observe_setup_process(
                probe, path, uid, service["MainPID"], group, read, deadline
            )
            if (
                facts["process"]["code"] == "observed"
                and facts["process"]["exe"] == "manager"
                and facts["process"]["uid_matches"] is True
            ):
                if context_io is not None:
                    facts["context"], facts["notification"] = await observe_setup_context(
                        probe, path, uid, service["MainPID"], group, read, context_io, deadline
                    )
                facts["journal"] = await observe_setup_journal(
                    probe, path, uid, service["MainPID"], group, read, runner, deadline
                )
    facts["failure"] = "none"
    return facts


class _SetupIO:
    """Share the existing deadline and actual byte allowance across system observations."""

    def __init__(self, deadline: float, limit: int, facts: dict[str, Any]) -> None:
        require(type(limit) is int and 0 < limit <= MAX_BYTES, "native_diagnostic_output_budget")
        self.deadline, self.limit, self.facts, self.used = deadline, limit, facts, 0

    def charge(self, data: bytes) -> None:
        self.used += len(data)
        require(self.used <= self.limit, "native_diagnostic_output_budget")
        remaining(self.deadline)

    def file(self, path: Path) -> str:
        from muli.platforms.linux_transaction import _read_regular

        if "manager_stage" in self.facts:
            self.facts["manager_stage"] = "kernel"
        require(self.used < self.limit, "native_diagnostic_output_budget")
        value = _read_regular(path, self.limit - self.used)
        self.charge(value)
        return value.decode("ascii")

    async def command(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool = False
    ) -> dict[str, Any]:
        from muli.process import run_process

        require(not merge_stderr, "native_diagnostic_command")
        require(self.used < self.limit, "native_diagnostic_output_budget")
        try:
            result = await run_process(
                resource_sudo_arguments(argv),
                timeout=min(timeout, CLEANUP_SECONDS, remaining(self.deadline)),
                limit=min(limit, self.limit - self.used),
                merge_stderr=False,
            )
        except (Exception, asyncio.CancelledError):
            # A failed command returns no byte count; its reserved allowance
            # cannot be spent again by another query or a later diagnostic.
            self.used = self.limit
            raise
        output, stderr = result.get("output"), result.get("stderr")
        if not isinstance(output, str) or not isinstance(stderr, str):
            raise NativeError("native_diagnostic_invalid")
        # Account for both returned pipes before the deadline check can raise.
        self.charge(output.encode() + stderr.encode())
        return result

    async def query(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        from muli.process import ProcessOutputLimitError

        require(argv[:2] == ["busctl", "--user"] and not merge_stderr, "native_diagnostic_command")
        self.facts["query_status"], self.facts["query_code"] = None, "not_started"
        if "manager_stage" in self.facts:
            self.facts["manager_stage"] = (
                "list_units"
                if "ListUnitsByPatterns" in argv
                else "unit_path"
                if "GetUnit" in argv
                else "unit_properties"
                if "org.freedesktop.systemd1.Unit" in argv
                else "service_properties"
            )
        try:
            result = await self.command(
                [argv[0], "--system", *argv[2:]], timeout=timeout, limit=limit
            )
        except (OSError, ValueError) as error:
            if isinstance(error, ProcessOutputLimitError) or (
                isinstance(error, NativeError) and str(error) == "native_diagnostic_output_budget"
            ):
                self.facts["query_code"] = "output_budget"
            elif isinstance(error, NativeError) and str(error) == "native_diagnostic_invalid":
                self.facts["query_code"] = "invalid_reply"
            elif isinstance(error, NativeError) and str(error) == "native_deadline_exceeded":
                self.facts["query_code"] = "timeout"
            elif isinstance(error, TimeoutError):
                self.facts["query_code"] = "timeout"
            else:
                self.facts["query_code"] = "command_failed"
            raise
        status = result.get("exit_code")
        self.facts["query_status"] = (
            status if type(status) is int and -(2**31) <= status < 2**31 else None
        )
        self.facts["query_code"] = diagnostic_command_code(status, result.get("stderr"))
        return result

    async def read(self, pid: int, name: str) -> bytes | None:
        from muli.platforms.linux_transaction import _read_regular

        if "manager_stage" in self.facts:
            self.facts["manager_stage"] = "process"
        require(
            0 < pid < 2**31 and name in {"stat", "status", "cgroup", "exe", "wchan"},
            "native_diagnostic_command",
        )
        remaining(self.deadline)
        if name in {"exe", "wchan"}:
            result = await self.command(
                [
                    "sudo",
                    "--non-interactive",
                    "--",
                    "readlink" if name == "exe" else "cat",
                    "--",
                    f"/proc/{pid}/{name}",
                ],
                timeout=CLEANUP_SECONDS,
                limit=self.limit,
            )
            return result["output"].encode() if result.get("exit_code") == 0 else None
        require(self.used < self.limit, "native_diagnostic_output_budget")
        data = _read_regular(Path("/proc") / str(pid) / name, self.limit - self.used)
        self.charge(data)
        return data

    async def instance(self, pid: int, uid: int, ticks: int, group: str) -> dict[str, Any]:
        allowance = self.limit - self.used - CONTEXT_REPLY_BYTES
        require(allowance > 0, "native_diagnostic_output_budget")
        self.used += allowance
        try:
            result = await self.command(
                [
                    "sudo",
                    "--non-interactive",
                    "--",
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "project-manager-instance",
                    str(pid),
                    str(uid),
                    str(ticks),
                    group,
                    str(self.deadline),
                    str(allowance),
                ],
                timeout=remaining(self.deadline),
                limit=CONTEXT_REPLY_BYTES,
            )
        except (Exception, asyncio.CancelledError):
            self.used = self.limit
            raise
        require(result.get("exit_code") == 0, "native_diagnostic_invalid")
        require(
            len(result["output"].encode()) + len(result["stderr"].encode()) <= CONTEXT_REPLY_BYTES,
            "native_diagnostic_output_budget",
        )

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                require(key not in value, "native_diagnostic_invalid")
                value[key] = item
            return value

        value = instance_projection(json.loads(result["output"], object_pairs_hook=unique))
        require(value["used"] <= allowance, "native_diagnostic_invalid")
        # command() charged the inner wire; only unused raw proc allowance is refunded.
        self.used -= allowance - value["used"]
        return value

    async def context(self, pid: int, uid: int, ticks: int, group: str) -> dict[str, str]:
        # This inner wire reserve comes from the existing allowance, not in addition to it.
        wire_limit = CONTEXT_REPLY_BYTES
        allowance = self.limit - self.used - wire_limit
        require(allowance > 0, "native_diagnostic_output_budget")
        self.used += allowance
        result = await self.command(
            [
                "sudo",
                "--non-interactive",
                "--",
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "project-setup-context",
                str(pid),
                str(uid),
                str(ticks),
                group,
                str(self.deadline),
                str(allowance),
            ],
            timeout=remaining(self.deadline),
            limit=wire_limit,
        )
        require(result.get("exit_code") == 0, "native_diagnostic_invalid")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                require(key not in value, "native_diagnostic_invalid")
                value[key] = item
            return value

        value = json.loads(result["output"], object_pairs_hook=unique)
        require(
            isinstance(value, dict) and set(value) == {"facts", "used"}, "native_diagnostic_invalid"
        )
        facts = context_projection(value["facts"])
        used = value["used"]
        require(type(used) is int and 0 <= used <= allowance, "native_diagnostic_invalid")
        self.used -= allowance - used
        return facts


async def _manager_list(
    probe: SystemdProbe, uid: int, facts: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Validate the exact loaded units and their state/path in this round."""
    from muli.platforms.linux_transaction import _typed

    names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
    listed = _typed(
        await probe._command(
            "call",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "ListUnitsByPatterns",
            "asas",
            "0",
            "2",
            *names,
        ),
        "a(ssssssouso)",
    )
    require(
        isinstance(listed, list)
        and len(listed) == 1
        and isinstance(listed[0], list)
        and len(listed[0]) <= len(names)
        and all(isinstance(row, list) and len(row) == 10 and row[0] in names for row in listed[0]),
        "native_manager_reply",
    )
    facts["manager_stage"] = "unit_validation"
    result: dict[str, dict[str, Any]] = {}
    for row in listed[0]:
        require(
            all(isinstance(row[index], str) for index in (*range(7), 8, 9))
            and all(not any(char in value for char in "\x00\r\n") for value in row[:7])
            and all(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", row[index]) for index in (2, 3, 4))
            and re.fullmatch(r"/org/freedesktop/systemd1/unit/[a-zA-Z0-9_]+", row[6]) is not None
            and type(row[7]) is int
            and 0 <= row[7] < 2**31
            and (
                re.fullmatch(r"[a-z][a-z0-9-]{0,63}", row[8]) is not None
                if row[7]
                else row[8] == ""
            )
            and row[9] == (f"/org/freedesktop/systemd1/job/{row[7]}" if row[7] else "/")
            and row[0] not in result,
            "native_manager_reply",
        )
        result[row[0]] = {
            "LoadState": row[2],
            "ActiveState": row[3],
            "SubState": row[4],
            "Job": [row[7], row[9]],
            "path": row[6],
        }
    require(len({unit["path"] for unit in result.values()}) == len(result), "native_manager_reply")
    return result


async def _manager_units(
    probe: SystemdProbe, uid: int, facts: dict[str, Any], *, pending: bool = False
) -> dict[str, dict[str, Any]]:
    """Pending rounds bind fresh identity; ready/idle retain full observations."""
    from muli.platforms.linux_transaction import _cgroup

    names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
    listed = await _manager_list(probe, uid, facts)
    lightweight = pending and not (
        set(listed) == set(names)
        and all(
            unit["ActiveState"] == "active" and unit["Job"] == [0, "/"] for unit in listed.values()
        )
    )
    result = {}
    for name in names:
        if name not in listed:
            continue
        if lightweight and name == names[0]:
            result[name] = listed[name]
            continue
        path = listed[name]["path"] if lightweight else await probe._unit_path(name)
        require(path == listed[name]["path"], "native_manager_changed")
        unit = await probe._properties(
            path,
            "Unit",
            (("Id", "s"), ("InvocationID", "ay"))
            if lightweight
            else (
                ("Id", "s"),
                ("LoadState", "s"),
                ("ActiveState", "s"),
                ("SubState", "s"),
                ("Job", "(uo)"),
                ("InvocationID", "ay"),
            ),
        )
        service = await probe._properties(
            path,
            "Service",
            (("MainPID", "u"), ("ControlGroup", "s"))
            if lightweight
            else (
                ("Type", "s"),
                ("MainPID", "u"),
                ("ControlGroup", "s"),
                ("Result", "s"),
                ("ExecMainCode", "i"),
                ("ExecMainStatus", "i"),
            ),
        )
        facts["manager_stage"] = "unit_validation"
        job, invocation = listed[name]["Job"] if lightweight else unit["Job"], unit["InvocationID"]
        require(
            unit["Id"] == name
            and isinstance(job, list)
            and len(job) == 2
            and type(job[0]) is int
            and 0 <= job[0] < 2**31
            and job[1] == (f"/org/freedesktop/systemd1/job/{job[0]}" if job[0] else "/")
            and isinstance(invocation, list)
            and len(invocation) == 16
            and all(type(item) is int and 0 <= item <= 255 for item in invocation)
            and all(
                type(service[key]) is int and 0 <= service[key] < 2**31
                for key in (
                    ("MainPID",) if lightweight else ("MainPID", "ExecMainCode", "ExecMainStatus")
                )
            ),
            "native_manager_reply",
        )
        group = _cgroup(service["ControlGroup"])
        require(
            all(
                isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value)
                for value in (
                    ()
                    if lightweight
                    else (
                        unit["LoadState"],
                        unit["ActiveState"],
                        unit["SubState"],
                        service["Type"],
                        service["Result"],
                    )
                )
            )
            and (not group or Path(group).name == name),
            "native_manager_reply",
        )
        if pending and not lightweight:
            require(
                all(
                    unit[key] == listed[name][key]
                    for key in ("LoadState", "ActiveState", "SubState", "Job")
                ),
                "native_manager_changed",
            )
        result[name] = {
            **(listed[name] if lightweight else {}),
            **unit,
            **service,
            "path": path,
            "invocation": bytes(invocation).hex(),
        }
    return result


def _manager_checkpoint(
    runtime: dict[str, Any],
    manager: dict[str, Any],
    binding: tuple[int, int, str] | None,
    invocation: str,
    elapsed: float,
) -> dict[str, Any] | None:
    """Project an already validated pending round; never fail the observation."""
    if (
        binding is None
        or not invocation
        or invocation == "0" * 32
        or re.fullmatch(r"[a-f0-9]{32}", invocation) is None
        or manager["invocation"] != invocation
        or manager["MainPID"] != binding[0]
        or manager["ControlGroup"] != binding[2]
        or type(binding[0]) is not int
        or not 0 < binding[0] < 2**31
        or type(binding[1]) is not int
        or not 0 <= binding[1] < 2**64
        or not math.isfinite(elapsed)
        or not 0 <= elapsed * 1000 <= (HARNESS_SECONDS + CLEANUP_SECONDS) * 1000
    ):
        return None
    projected: dict[str, Any] = {
        "version": 1,
        "code": "observed",
        "elapsed_ms": round(elapsed * 1000),
    }
    for name, unit in (("runtime", runtime), ("manager", manager)):
        state, sub, job = unit["ActiveState"], unit["SubState"], unit["Job"][0]
        if (
            not isinstance(state, str)
            or state not in {"active", "activating", "inactive"}
            or not isinstance(sub, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", sub) is None
            or type(job) is not int
            or not 0 <= job < 2**31
            or (state == "inactive" and job == 0)
        ):
            return None
        projected[name] = {"active_state": state, "sub_state": sub, "job": job}
    projected["manager"].update(pid=binding[0], start_ticks=binding[1], invocation=invocation)
    return projected


async def manager_state(
    mode: str,
    user: str,
    uid: int,
    home: Path,
    deadline: float,
    byte_limit: int,
    *,
    started: float = 0.0,
    previous: str = "",
) -> dict[str, Any]:
    """One bounded read-only system-manager observation, never another start."""
    import pwd

    from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe, _start_ticks

    require(mode in {"manager-idle", "manager-ready"}, "native_manager_arguments")
    require(re.fullmatch(r"muli-ci-[a-f0-9]{12}", user) is not None, "native_manager_identity")
    account = pwd.getpwnam(user)
    require((account.pw_uid, account.pw_dir) == (uid, str(home)), "native_manager_identity")
    require(
        math.isfinite(deadline) and remaining(deadline) <= HARNESS_SECONDS + CLEANUP_SECONDS,
        "native_manager_deadline",
    )
    facts: dict[str, Any] = {
        "manager_stage": "list_units",
        "query_status": None,
        "query_code": "not_started",
    }
    io = _SetupIO(deadline, byte_limit, facts)
    result: dict[str, Any] = {"code": "native_manager_observation", "used": 0}
    last_completed_round: dict[str, Any] = {"version": 1, "code": "not_observed"}
    try:
        probe = SystemdProbe(
            runner=io.query, limits=ProbeLimits(seconds=CLEANUP_SECONDS, command_bytes=MAX_BYTES)
        )
        names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
        async with asyncio.timeout(remaining(deadline)):
            if mode == "manager-idle":
                units = await _manager_units(probe, uid, facts)
                facts["manager_stage"] = "manager_validation"
                require(
                    all(
                        unit["LoadState"] == "loaded"
                        and unit["ActiveState"] == "inactive"
                        and unit["SubState"] == "dead"
                        and unit["Job"] == [0, "/"]
                        and unit["MainPID"] == 0
                        and unit["ControlGroup"] == ""
                        for unit in units.values()
                    ),
                    "native_manager_conflict",
                )
                result = {
                    "code": "idle",
                    "invocation": units.get(names[-1], {}).get("invocation", ""),
                    "used": 0,
                }
            else:
                require(
                    math.isfinite(started)
                    and 0 < started <= time.monotonic()
                    and (not previous or re.fullmatch(r"[a-f0-9]{32}", previous) is not None),
                    "native_manager_arguments",
                )
                invocation, binding, late, delay = "", None, False, POLL_SECONDS
                while True:
                    observed_since = time.monotonic()
                    units = await _manager_units(probe, uid, facts, pending=True)
                    facts["manager_stage"] = "manager_validation"
                    require(set(units) == set(names), "native_manager_missing")
                    runtime, manager = (units[name] for name in names)
                    require(
                        all(unit["LoadState"] == "loaded" for unit in units.values()),
                        "native_manager_failed",
                    )
                    current = manager["invocation"]
                    require(not invocation or current == invocation, "native_manager_changed")
                    if current != "0" * 32 and current != previous:
                        require(not invocation or current == invocation, "native_manager_changed")
                        invocation = current
                    pid, group = manager["MainPID"], manager["ControlGroup"]
                    if pid:
                        require(bool(group), "native_manager_changed")
                        facts["manager_stage"] = "process"
                        data = await io.read(pid, "stat")
                        require(data is not None, "native_manager_changed")
                        assert data is not None
                        observed = (pid, _start_ticks(data, pid), group)
                        require(binding is None or binding == observed, "native_manager_changed")
                        binding = observed
                    elif binding is not None:
                        raise NativeError("native_manager_changed")
                    facts["manager_stage"] = "manager_validation"
                    require(
                        all(
                            unit["ActiveState"] in {"activating", "active"}
                            or (unit["ActiveState"] == "inactive" and unit["Job"][0] > 0)
                            for unit in (runtime, manager)
                        ),
                        "native_manager_failed",
                    )
                    if manager["ActiveState"] == "activating" and invocation:
                        late |= observed_since > started + CLEANUP_SECONDS
                    if (
                        manager["ActiveState"] == "active"
                        and manager["Job"] == [0, "/"]
                        and runtime["ActiveState"] == "active"
                        and runtime["Job"] == [0, "/"]
                    ):
                        if not invocation or binding is None:
                            raise NativeError("native_manager_not_ready")
                        require(
                            manager["SubState"] == "running"
                            and manager["Type"] == "notify-reload"
                            and manager["Job"] == [0, "/"]
                            and manager["Result"] == "success"
                            and runtime["ActiveState"] == "active"
                            and runtime["SubState"] == "exited"
                            and runtime["Type"] == "oneshot"
                            and runtime["Job"] == [0, "/"]
                            and runtime["MainPID"] == 0
                            and runtime["Result"] == "success"
                            and runtime["ExecMainCode"] == 1
                            and runtime["ExecMainStatus"] == 0,
                            "native_manager_not_ready",
                        )
                        facts["manager_stage"] = "process"
                        process = await observe_setup_process(
                            probe,
                            manager["path"],
                            uid,
                            pid,
                            group,
                            io.read,
                            deadline,
                            observe_wait=False,
                        )
                        require(
                            process["code"] == "observed"
                            and process["exe"] == "manager"
                            and process["uid_matches"] is True,
                            "native_manager_process",
                        )
                        facts["manager_stage"] = "kernel"
                        kernel_limits(
                            Path("/sys/fs/cgroup") / group.lstrip("/"),
                            memory=MEMORY,
                            tasks=TASKS,
                            read=io.file,
                        )
                        final = await _manager_units(probe, uid, facts)
                        facts["manager_stage"] = "process"
                        last = await io.read(pid, "stat")
                        facts["manager_stage"] = "manager_validation"
                        require(
                            final == units
                            and last is not None
                            and _start_ticks(last, pid) == binding[1],
                            "native_manager_changed",
                        )
                        remaining(deadline)
                        result = {
                            "code": "ready",
                            "group": group,
                            "invocation": invocation,
                            "pid": pid,
                            "elapsed_ms": round((time.monotonic() - started) * 1000),
                            "late_ready": late,
                            "used": 0,
                        }
                        break
                    pause = min(delay, remaining(deadline))
                    checkpoint = _manager_checkpoint(
                        runtime, manager, binding, invocation, observed_since - started
                    )
                    if checkpoint is not None:
                        last_completed_round = checkpoint
                    await asyncio.sleep(pause)
                    delay = min(CLEANUP_SECONDS, delay * 2)
    except TimeoutError:
        result["code"] = "native_deadline_exceeded"
        if facts["query_status"] is None and facts["query_code"] == "not_started":
            facts["query_code"] = "timeout"
            io.used = io.limit  # The interrupted query never returned its byte count.
    except (OSError, ValueError) as error:
        if isinstance(error, NativeError) and re.fullmatch(r"native_[a-z_]{1,57}", str(error)):
            result["code"] = str(error)
    if result["code"] not in {"idle", "ready"}:
        result.update(
            stage=facts["manager_stage"],
            query_status=facts["query_status"],
            query_code=facts["query_code"],
            last_completed_round=last_completed_round,
        )
        if io.used > byte_limit:
            # Saturation reserves the whole allowance; this is explicitly a failed read.
            io.used = byte_limit
            result["code"] = "native_diagnostic_output_budget"
    result["used"] = io.used
    return result


def manager_instance_anchor(checkpoint: object) -> dict[str, Any]:
    require(isinstance(checkpoint, dict), "native_diagnostic_invalid")
    assert isinstance(checkpoint, dict)
    require(
        set(checkpoint) == {"version", "code", "elapsed_ms", "runtime", "manager"}
        and type(checkpoint["version"]) is int
        and checkpoint["version"] == 1
        and checkpoint["code"] == "observed"
        and type(checkpoint["elapsed_ms"]) is int
        and 0 <= checkpoint["elapsed_ms"] <= (HARNESS_SECONDS + CLEANUP_SECONDS) * 1000,
        "native_diagnostic_invalid",
    )
    for name in ("runtime", "manager"):
        item = checkpoint[name]
        require(
            isinstance(item, dict)
            and set(item)
            == {"active_state", "sub_state", "job"}
            | ({"pid", "start_ticks", "invocation"} if name == "manager" else set()),
            "native_diagnostic_invalid",
        )
        require(
            item["active_state"] in ("active", "activating", "inactive")
            and isinstance(item["sub_state"], str)
            and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item["sub_state"]) is not None
            and type(item["job"]) is int
            and 0 <= item["job"] < 2**31
            and not (item["active_state"] == "inactive" and item["job"] == 0),
            "native_diagnostic_invalid",
        )
    anchor = checkpoint["manager"]
    require(
        type(anchor["pid"]) is int
        and 0 < anchor["pid"] < 2**31
        and type(anchor["start_ticks"]) is int
        and 0 <= anchor["start_ticks"] < 2**64
        and isinstance(anchor["invocation"], str)
        and re.fullmatch(r"[a-f0-9]{32}", anchor["invocation"]) is not None
        and anchor["invocation"] != "0" * 32,
        "native_diagnostic_invalid",
    )
    return {key: anchor[key] for key in ("pid", "start_ticks", "invocation")}


async def diagnose_manager_instance(
    user: str,
    uid: int,
    home: Path,
    deadline: float,
    byte_limit: int,
    checkpoint: object,
) -> dict[str, Any]:
    """A later system-manager/proc identity window, not a deadline snapshot."""
    import pwd

    from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe, _cgroup

    anchor = manager_instance_anchor(checkpoint)
    require(type(uid) is int and 0 < uid < 2**31, "native_diagnostic_identity")
    require(
        type(byte_limit) is int and 0 < byte_limit <= MAX_BYTES - 1024,
        "native_diagnostic_output_budget",
    )
    facts = {
        "version": 1,
        "uid": uid,
        **anchor,
        "internal": "not_observed",
        "pending_job": pending_job_unavailable(),
        **instance_unavailable("invalid_reply", 0),
    }
    io = _SetupIO(deadline, byte_limit, {})
    try:
        require(
            re.fullmatch(r"muli-ci-[a-f0-9]{12}", user) is not None and home.is_absolute(),
            "native_diagnostic_identity",
        )
        require(
            math.isfinite(deadline) and remaining(deadline) <= CLEANUP_SECONDS,
            "native_diagnostic_invalid",
        )
        account = pwd.getpwnam(user)
        require((account.pw_uid, account.pw_dir) == (uid, str(home)), "native_diagnostic_identity")
        async with asyncio.timeout(remaining(deadline)):
            probe = SystemdProbe(
                runner=io.query,
                limits=ProbeLimits(seconds=CLEANUP_SECONDS, command_bytes=MAX_BYTES),
            )
            path = await probe._unit_path(f"user@{uid}.service")

            async def binding(*, status: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
                unit = await probe._properties(path, "Unit", (("Id", "s"), ("InvocationID", "ay")))
                service = await probe._properties(
                    path,
                    "Service",
                    (("MainPID", "u"), ("ControlGroup", "s"))
                    + ((("StatusText", "s"),) if status else ()),
                )
                invocation = unit["InvocationID"]
                require(
                    isinstance(invocation, list)
                    and len(invocation) == 16
                    and all(type(item) is int and 0 <= item <= 255 for item in invocation),
                    "native_diagnostic_invalid",
                )
                group = _cgroup(service["ControlGroup"])
                require(
                    unit["Id"] == f"user@{uid}.service"
                    and bytes(invocation).hex() == anchor["invocation"]
                    and type(service["MainPID"]) is int
                    and service["MainPID"] == anchor["pid"]
                    and bool(group)
                    and Path(group).name == f"user@{uid}.service",
                    "native_process_changed",
                )
                return unit, service

            before = await binding(status=True)
            pending_job = pending_job_projection(before[1].pop("StatusText"))
            projected = await io.instance(
                anchor["pid"], uid, anchor["start_ticks"], before[1]["ControlGroup"]
            )
            if projected["code"] == "observed":
                require(before == await binding(), "native_process_changed")
                account = pwd.getpwnam(user)
                require(
                    (account.pw_uid, account.pw_dir) == (uid, str(home)), "native_process_changed"
                )
            remaining(deadline)
            facts.update({key: projected[key] for key in ("code", "reason", "exe", "uids")})
            facts["internal"] = projected.get("internal", "not_observed")
            if projected["code"] == "observed":
                facts["pending_job"] = pending_job
    except Exception as error:
        facts.update(instance_unavailable(instance_error(error), 0))
    facts["used"] = min(io.used, byte_limit)
    # All strings/integers above are fixed or bounded; the complete CLI JSON is <1024B.
    return facts


async def diagnose_setup(
    user: str,
    uid: int,
    home: Path,
    deadline: float,
    byte_limit: int = MAX_BYTES,
    *,
    include_usage: bool = False,
) -> dict[str, Any]:
    """The working runner interpreter observes only its exact new UID's manager."""
    import pwd

    require(
        re.fullmatch(r"muli-ci-[a-f0-9]{12}", user) is not None
        and type(uid) is int
        and 0 < uid < 2**31
        and home.is_absolute(),
        "native_diagnostic_identity",
    )
    require(remaining(deadline) <= CLEANUP_SECONDS, "native_diagnostic_deadline")
    facts: dict[str, Any] = {
        "stage": "identity",
        "query_status": None,
        "query_code": "not_started",
        "failure": "not_started",
        "unit": "",
        "units": [],
        "process": process_unavailable(),
        "journal": journal_unavailable(),
        "context": context_unavailable(),
        "notification": notification_unavailable(),
    }
    io = _SetupIO(deadline, byte_limit, facts)
    try:
        account = pwd.getpwnam(user)
        require((account.pw_uid, account.pw_dir) == (uid, str(home)), "native_diagnostic_identity")
        facts["stage"] = "import"
        from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe

        async with asyncio.timeout(remaining(deadline)):
            await observe_setup_units(
                SystemdProbe(
                    runner=io.query,
                    limits=ProbeLimits(seconds=CLEANUP_SECONDS, command_bytes=MAX_BYTES),
                ),
                uid,
                facts,
                io.read,
                deadline,
                io.command,
                context_io=io,
            )
    except TimeoutError:
        facts["failure"], facts["query_code"] = "deadline", "timeout"
    except Exception as error:
        if isinstance(error, NativeError) and str(error) == "native_deadline_exceeded":
            facts["failure"], facts["query_code"] = "deadline", "timeout"
        elif facts["stage"] in {"identity", "import"}:
            facts["failure"] = (
                "identity_failed" if facts["stage"] == "identity" else "import_failed"
            )
        else:
            facts["failure"] = "query_failed" if facts["query_code"] != "ok" else "invalid_reply"
    if include_usage:
        facts["used"] = io.used
    return facts


def fixture(root: Path) -> dict[str, Any]:
    require(root.is_absolute() and root.resolve(strict=True) == root, "native_root_invalid")
    value = read_json(root / "fixture.json")
    require(value.get("uid") == os.geteuid() and os.geteuid() != 0, "native_uid_invalid")
    expected = {
        "memory": MEMORY,
        "tasks": TASKS,
        "harness_memory": HARNESS_MEMORY,
        "harness_tasks": HARNESS_TASKS,
        "seconds": CASE_SECONDS,
        "cleanup_seconds": CLEANUP_SECONDS,
        "harness_seconds": HARNESS_SECONDS,
        "http_bytes": MAX_BYTES,
    }
    require(
        all(type(value.get(key)) is int and value[key] == item for key, item in expected.items()),
        "native_fixture_budget",
    )
    require(
        isinstance(value.get("harness_unit"), str)
        and re.fullmatch(r"muli-native-[a-f0-9]{32}\.service", value["harness_unit"]) is not None,
        "native_fixture_unit",
    )
    return value


def describe() -> dict[str, Any]:
    """Read only the installed runtime; this mode never launches any command."""
    import muli
    from muli.management_transaction import JournalLimits
    from muli.platforms.installation import PackageLimits
    from muli.platforms.managed_launcher import package_fingerprint

    limits = PackageLimits()
    package = Path(muli.__file__).resolve(strict=True).parent
    observed = package_fingerprint(package, limits.model_dump())
    runtime = Path(sys.base_prefix).resolve(strict=True)
    executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
    require(executable.is_relative_to(runtime), "native_runtime_layout")
    return {
        "runtime": str(runtime),
        "executable": str(executable),
        "dependencies": str(Path(sysconfig.get_paths()["purelib"]).resolve(strict=True)),
        "package": str(package),
        "size": observed["size"],
        "digest": observed["digest"],
        "limits": limits.model_dump(mode="json"),
        "http_bytes": JournalLimits().max_record_bytes,
    }


def forbidden_model(*args: Any, **kwargs: Any) -> NoReturn:
    raise NativeError("native_model_forbidden")


def write_latest(store: Store, record: TransactionRecordV2) -> None:
    """Only called after the real target has acquired its writer and Store."""
    store.put("fixture", {"id": "latest", "after_prepare": record.generation})
    store.enqueue_input(
        {
            "id": "latest-input",
            "session_id": record.identity.main_session,
            "principal": "owner",
            "source": "web",
            "content": "after prepare",
        },
        max_pending=2,
    )
    with store.db:
        store.db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES('latest-outbox','telegram',?,?,'unknown',0,0,0)",
            (
                json.dumps({"target": "fixture-owner", "kind": "private"}),
                json.dumps({"parts": [{"text": "after prepare"}]}),
            ),
        )


def helper_costs_facts(value: object, source: object) -> bool:
    """The optional observer has no authority beyond the existing helper proof."""
    if not isinstance(value, dict):
        return False
    if value.get("code") == "not_observed":
        return set(value) == {"code", "reason"} and value.get("reason") in (
            "unavailable",
            "identity_changed",
            "output_budget",
        )
    identity = {"generation", "helper", "uid", "pid", "invocation"}
    version = value.get("version")
    extra = {"host_record", "check_breakdown"} if version in (2, 3) else set()
    if version == 3:
        extra.update(HELPER_COST_SLOT_METRICS)
    if (
        not isinstance(source, dict)
        or set(source) != identity
        or set(value) != identity | {"version", "code", "checks", "bus", "http"} | extra
        or type(version) is not int
        or version not in (1, 2, 3)
        or value["code"] != "observed"
        or any(
            type(value[key]) is not type(source[key]) or value[key] != source[key]
            for key in identity
        )
    ):
        return False

    def metric(item: object) -> bool:
        return (
            isinstance(item, dict)
            and set(item) == {"count", "total_ms"}
            and all(type(number) is int and 0 <= number <= 2**63 - 1 for number in item.values())
        )

    if not all(metric(value[name]) for name in ("checks", "bus", "http")):
        return False
    if version == 1:
        return True
    if version == 3:
        if not all(metric(value[name]) for name in HELPER_COST_SLOT_METRICS):
            return False
        controller, selected = (value[name] for name in HELPER_COST_SLOT_METRICS)
        if (
            selected["count"] > controller["count"]
            or controller["count"] > value["checks"]["count"]
            or controller["total_ms"] + selected["total_ms"] > value["checks"]["total_ms"]
        ):
            return False
    rows = value["check_breakdown"]
    phases, intents = HELPER_COST_PHASES, HELPER_COST_INTENTS
    if (
        not metric(value["host_record"])
        or not isinstance(rows, list)
        or len(rows) > len(phases) * len(intents)
    ):
        return False
    seen: set[tuple[str, str]] = set()
    count, milliseconds = 0, 0
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"phase", "intent", "count", "total_ms"}
            or type(row["phase"]) is not str
            or type(row["intent"]) is not str
            or row["phase"] not in phases
            or row["intent"] not in intents
            or not metric({key: row[key] for key in ("count", "total_ms")})
            or row["count"] == 0
        ):
            return False
        key = row["phase"], row["intent"]
        if key in seen:
            return False
        seen.add(key)
        count += row["count"]
        milliseconds += row["total_ms"]
    # Per-bucket integer truncation can lose milliseconds, never add them.
    return count == value["checks"]["count"] and milliseconds <= value["checks"]["total_ms"]


def helper_cost_sample_facts(value: object, source: object) -> bool:
    """A partial install snapshot never replaces complete helper-exit costs."""
    if not isinstance(value, dict) or set(value) != {"sample", "costs"}:
        return False
    sample, costs = value["sample"], value["costs"]
    return (
        isinstance(sample, dict)
        and set(sample) == {"kind", "revision"}
        and sample["kind"] == "install_returned"
        and type(sample["revision"]) is int
        and 1 <= sample["revision"] <= 2**63 - 1
        and isinstance(costs, dict)
        and costs.get("code") == "observed"
        and helper_costs_facts(costs, source)
    )


def helper_start_sample_facts(value: object, source: object) -> bool:
    """Only fixed positive start facts may accompany an authenticated cost snapshot."""
    if not isinstance(value, dict) or set(value) != {"sample", "costs"}:
        return False
    sample, costs = value["sample"], value["costs"]
    return (
        isinstance(sample, dict)
        and set(sample) == {"kind", "revision", "phase", "intent", "role", "elapsed_ms"}
        and sample["kind"] in HELPER_START_BOUNDARIES
        and (sample["phase"], sample["intent"], sample["role"]) in HELPER_START_MODES
        and type(sample["revision"]) is int
        and 1 <= sample["revision"] <= 2**63 - 1
        and type(sample["elapsed_ms"]) is int
        and 0 <= sample["elapsed_ms"] <= 2**63 - 1
        and isinstance(costs, dict)
        and costs.get("code") == "observed"
        and helper_costs_facts(costs, source)
    )


def unit_files() -> ModuleType:
    """Load only the fixture's fixed sibling, without changing import paths."""
    if __package__:
        from . import managed_native_unit

        return managed_native_unit
    import importlib.util

    name = "_muli_managed_native_unit"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().with_name("managed_native_unit.py")
        )
        require(spec is not None and spec.loader is not None, "native_unit_birth_unconfirmed")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


def install_unit_birth_hook() -> None:
    """The fixture observer never changes the real helper's admission/outcome."""
    try:
        prefix = Path(sys.prefix)
        root = prefix.parent
        require(
            prefix.name in {"a", "b"}
            and sys.executable == str(prefix / "bin/python")
            and sys.orig_argv
            == [
                sys.executable,
                "-I",
                "-m",
                "muli.managed_host",
                "--root",
                str(root / "data/management/transaction-v1"),
                "run",
            ],
            "native_unit_birth_unconfirmed",
        )
        fixture(root)
        unit_files().install(root, limit=MAX_BYTES)
    except Exception:
        pass


def install_helper_cost_hook() -> None:
    """Observe only the exact isolated helper; never change its control flow."""
    try:
        import muli.managed_host as managed_host
        import muli.platforms.managed_backend as managed_backend
        import muli.platforms.managed_deployment as managed_deployment
        from muli.managed_controller import BindingConfirmation
        from muli.managed_host import managed_helper_unit_name
        from muli.managed_journal import RoleSnapshots
        from muli.managed_transaction import TransactionRecordV2
        from muli.management_transaction import Intent, Phase, TransactionJournal
        from muli.platforms.linux_transaction import SystemdProbe
        from muli.platforms.managed_backend import ManagedLinuxBackend

        prefix = Path(sys.prefix)
        root = prefix.parent
        journal_root = root / "data/management/transaction-v1"
        require(prefix.name in {"a", "b"}, "native_profile_role")
        require(
            sys.executable == str(prefix / "bin/python")
            and sys.orig_argv
            in (
                [sys.executable, "-I", "-m", "muli.management_helper", str(journal_root)],
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "muli.managed_host",
                    "--root",
                    str(journal_root),
                    "run",
                ],
            ),
            "native_profile_arguments",
        )
        fixture(root)
        require(
            get_args(Phase) == HELPER_COST_PHASES and get_args(Intent) == HELPER_COST_INTENTS,
            "native_profile_contract",
        )
        info = root.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            "native_profile_owner",
        )
        invocation = os.environ.get("INVOCATION_ID", "")
        require(
            re.fullmatch(r"[a-f0-9]{32}", invocation) is not None and invocation != "0" * 32,
            "native_profile_invocation",
        )
        with TransactionJournal(
            root / "data/management/transaction-v1", root / "data/muli.sqlite3", create=False
        ) as journal:
            record = journal.read_current()
        require(isinstance(record, TransactionRecordV2), "native_profile_record")
        assert isinstance(record, TransactionRecordV2)
        facts: dict[str, Any] = {
            "version": 3,
            "code": "observed",
            "generation": record.generation,
            "helper": managed_helper_unit_name(record),
            "uid": os.geteuid(),
            "pid": os.getpid(),
            "invocation": invocation,
        }
        counts = {
            name: 0 for name in ("checks", "bus", "http", "host_record", *HELPER_COST_SLOT_METRICS)
        }
        totals = {name: 0.0 for name in counts}
        phase_counts: dict[tuple[str, str], int] = {}
        phase_totals: dict[tuple[str, str], float] = {}
        usable, saved, sampled = True, False, False
        check_depth = 0
        clock = time.perf_counter

        def begin() -> float | None:
            nonlocal usable
            try:
                started = clock()
                if not math.isfinite(started):
                    raise ValueError
                return started
            except Exception:
                usable = False
                return None

        def finish(name: str, started: float | None) -> float | None:
            nonlocal usable
            try:
                if started is None:
                    return None
                elapsed = clock() - started
                if not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError
                counts[name] += 1
                totals[name] += elapsed
                return elapsed
            except Exception:
                usable = False
                return None

        original_check = ManagedLinuxBackend._check
        original_command = SystemdProbe._command
        original_request = ManagedLinuxBackend._request
        original_install = ManagedLinuxBackend.install
        original_start = ManagedLinuxBackend.start
        original_backend_command = ManagedLinuxBackend._command
        start_context: (
            tuple[ManagedLinuxBackend, TransactionRecordV2, Role, float, asyncio.Task[Any] | None]
            | None
        ) = None
        start_boundary = -1
        start_command_active = False
        managed_entry = sys.orig_argv[3] == "muli.managed_host"
        host_bound = not managed_entry
        host_spec = managed_host.__spec__
        host_origin = host_spec.origin if host_spec is not None else None

        @wraps(original_check)
        def check(*args: Any, **kwargs: Any) -> None:
            nonlocal usable, check_depth
            started = begin()
            check_depth += 1
            try:
                return original_check(*args, **kwargs)
            finally:
                check_depth -= 1
                elapsed = finish("checks", started)
                try:
                    current = args[1] if len(args) > 1 else kwargs["record"]
                    if not isinstance(current, TransactionRecordV2) or elapsed is None:
                        raise ValueError
                    key = current.phase, current.intent
                    if key[0] not in get_args(Phase) or key[1] not in get_args(Intent):
                        raise ValueError
                    phase_counts[key] = phase_counts.get(key, 0) + 1
                    phase_totals[key] = phase_totals.get(key, 0.0) + elapsed
                except Exception:
                    usable = False

        def observe_slot_files(name: str, original: Callable[..., None]) -> Callable[..., None]:
            @wraps(original)
            def slot_files(*args: Any, **kwargs: Any) -> None:
                # _check is synchronous: the scope ends before any await and
                # excludes the host's independent controller authentication.
                if not check_depth:
                    return original(*args, **kwargs)
                started = begin()
                try:
                    return original(*args, **kwargs)
                finally:
                    finish(name, started)

            return slot_files

        def observe_record(original: HostRecordReader) -> HostRecordReader:
            @wraps(original)
            def host_record(
                control: JournalControl, anchor: DeploymentAnchor
            ) -> tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]]:
                started = begin()
                try:
                    return original(control, anchor)
                finally:
                    finish("host_record", started)

            return host_record

        def stop_waiting() -> None:
            nonlocal usable
            try:
                if sys.getprofile() is bind_main:
                    sys.setprofile(None)
            except BaseException:
                usable = False  # Diagnostic cleanup cannot replace a real outcome.

        def bind_main(frame: FrameType, event: str, argument: object) -> None:
            nonlocal host_bound, usable
            if event != "call" or frame.f_code.co_name != "main":
                return
            reached_main = False
            try:
                module = sys.modules.get("__main__")
                if module is None or frame.f_globals is not module.__dict__:
                    return
                reached_main = True
                namespace = frame.f_globals
                spec = namespace.get("__spec__")
                main = namespace.get("main")
                original = namespace.get("_record")
                require(
                    isinstance(spec, ModuleSpec)
                    and spec.name == "muli.managed_host"
                    and isinstance(host_origin, str)
                    and spec.origin == host_origin
                    and namespace.get("__file__") == host_origin
                    and frame.f_code.co_filename == host_origin
                    and isinstance(main, FunctionType)
                    and main.__globals__ is namespace
                    and main.__code__ is frame.f_code
                    and isinstance(original, FunctionType)
                    and original.__globals__ is namespace
                    and original.__code__.co_filename == host_origin
                    and original.__code__.co_name == "_record",
                    "native_profile_main_unconfirmed",
                )
                namespace["_record"] = observe_record(cast("HostRecordReader", original))
                host_bound = True
            except BaseException:
                reached_main = True
                usable = False  # A failed observer cannot prevent main from running.
            finally:
                # Only the first real __main__.main call is observed. Never
                # clear a profiler installed by a subsequent owner.
                if reached_main:
                    stop_waiting()

        @wraps(original_command)
        async def command(*args: Any, **kwargs: Any) -> str:
            started = begin()
            try:
                return await original_command(*args, **kwargs)
            finally:
                finish("bus", started)

        @wraps(original_request)
        async def request(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            started = begin()
            try:
                return await original_request(*args, **kwargs)
            finally:
                finish("http", started)

        def publish(path: Path, value: dict[str, Any]) -> None:
            data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            require(len(data) <= MAX_BYTES, "native_metadata_budget")
            temporary = path.with_suffix(".tmp")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        def snapshot() -> dict[str, Any]:
            require(usable and host_bound, "native_profile_invalid")
            value = {
                **facts,
                **{
                    name: {"count": counts[name], "total_ms": int(elapsed * 1000)}
                    for name, elapsed in totals.items()
                },
                "check_breakdown": [
                    {
                        "phase": phase,
                        "intent": intent,
                        "count": phase_counts[phase, intent],
                        "total_ms": int(phase_totals[phase, intent] * 1000),
                    }
                    for phase, intent in sorted(phase_counts)
                ],
            }
            source = {
                key: facts[key] for key in ("generation", "helper", "uid", "pid", "invocation")
            }
            require(helper_costs_facts(value, source), "native_profile_invalid")
            return value

        def start_checkpoint(
            kind: str,
            context: tuple[
                ManagedLinuxBackend, TransactionRecordV2, Role, float, asyncio.Task[Any] | None
            ],
        ) -> None:
            nonlocal start_boundary
            try:
                index = HELPER_START_BOUNDARIES.index(kind)
                if start_context is not context or index <= start_boundary:
                    return
                # An observer failure consumes this attempt; never retry in a loop.
                start_boundary = index
                _, current, role, started, _ = context
                elapsed = clock() - started
                require(math.isfinite(elapsed) and elapsed >= 0, "native_profile_invalid")
                publish(
                    root / "helper-start-checkpoint.json",
                    {
                        "sample": {
                            "kind": kind,
                            "revision": current.revision,
                            "phase": current.phase,
                            "intent": current.intent,
                            "role": role,
                            "elapsed_ms": int(elapsed * 1000),
                        },
                        "costs": snapshot(),
                    },
                )
            except Exception:
                pass  # Ordinary diagnostic failures cannot change the real operation.

        @wraps(original_start)
        async def start(
            backend: ManagedLinuxBackend, record: TransactionRecordV2, role: Role
        ) -> None:
            nonlocal start_context, start_boundary, start_command_active
            context: (
                tuple[
                    ManagedLinuxBackend, TransactionRecordV2, Role, float, asyncio.Task[Any] | None
                ]
                | None
            ) = None
            try:
                try:
                    if (
                        start_context is None
                        and record.generation == facts["generation"]
                        and managed_helper_unit_name(record) == facts["helper"]
                        and (record.phase, record.intent, role) in HELPER_START_MODES
                    ):
                        started = clock()
                        require(math.isfinite(started), "native_profile_invalid")
                        context = backend, record, role, started, asyncio.current_task()
                        start_context, start_boundary = context, -1
                        start_checkpoint("start_entered", context)
                except Exception:
                    pass
                return await original_start(backend, record, role)
            finally:
                if context is not None and start_context is context:
                    start_context = None  # Identity only; never retain any file/security proof.
                    start_boundary = -1
                    start_command_active = False

        @wraps(original_backend_command)
        async def backend_command(
            backend: ManagedLinuxBackend, action: Literal["start", "stop", "daemon-reload"]
        ) -> None:
            nonlocal start_command_active
            context = start_context
            observed = False
            try:
                observed = (
                    context is not None
                    and context[0] is backend
                    and context[4] is asyncio.current_task()
                    and action == "start"
                    and not start_command_active
                )
            except Exception:
                pass
            if observed:
                start_command_active = True
            try:
                if observed:
                    assert context is not None
                    start_checkpoint("start_command_entered", context)
                await original_backend_command(backend, action)
                if observed:
                    assert context is not None
                    start_checkpoint("start_command_returned", context)
            finally:
                if observed:
                    start_command_active = False

        @wraps(original_install)
        async def install(
            backend: ManagedLinuxBackend,
            record: TransactionRecordV2,
            role: Role,
            data: RoleSnapshots,
        ) -> BindingConfirmation:
            nonlocal sampled
            confirmation = await original_install(backend, record, role, data)
            if not sampled:
                sampled = True
                try:
                    require(
                        isinstance(confirmation, BindingConfirmation)
                        and confirmation.position == record.position
                        and record.generation == facts["generation"],
                        "native_profile_invalid",
                    )
                    publish(
                        root / "helper-costs-checkpoint.json",
                        {
                            "sample": {"kind": "install_returned", "revision": record.revision},
                            "costs": snapshot(),
                        },
                    )
                except Exception:
                    pass  # Partial observation cannot change a successful install.
            return confirmation

        def save() -> None:
            nonlocal saved
            if saved:
                return
            saved = True
            stop_waiting()
            try:
                publish(root / "helper-costs.json", snapshot())
            except Exception:
                pass  # Observation never replaces the real helper's outcome.

        atexit.register(save)
        # Dedicated CI observation hooks intentionally replace these methods.
        managed_deployment.verify_slot_files = observe_slot_files(
            HELPER_COST_SLOT_METRICS[0], managed_deployment.verify_slot_files
        )
        managed_backend.verify_slot_files = observe_slot_files(
            HELPER_COST_SLOT_METRICS[1], managed_backend.verify_slot_files
        )
        ManagedLinuxBackend._check = check  # type: ignore[method-assign]
        SystemdProbe._command = command  # type: ignore[method-assign]
        ManagedLinuxBackend._request = request  # type: ignore[method-assign]
        ManagedLinuxBackend.install = install  # type: ignore[method-assign,assignment]
        ManagedLinuxBackend.start = start  # type: ignore[method-assign]
        ManagedLinuxBackend._command = backend_command  # type: ignore[method-assign]
        if managed_entry:
            # -m re-executes this source as __main__; the canonical import is
            # not the helper's execution namespace. Bind once before main runs.
            try:
                if sys.getprofile() is None:
                    sys.setprofile(bind_main)
                else:
                    usable = False
            except BaseException:
                usable = False
                stop_waiting()
        else:
            # Legacy helper and explicit offline fixtures do not run this main.
            managed_host._record = observe_record(managed_host._record)
    except Exception:
        return  # A missing observer must not prevent the original helper starting.


def _read_helper_cost_json(path: Path, *, deadline: float | None) -> Any:
    if deadline is not None:
        remaining(deadline)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and not before.st_mode & 0o077
            and before.st_nlink == 1
            and before.st_size <= MAX_BYTES,
            "native_profile_owner",
        )
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        require(
            len(data) <= MAX_BYTES
            and all(
                getattr(before, field) == getattr(after, field) == getattr(current, field)
                for field in fields
            ),
            "native_profile_changed",
        )

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            require(key not in value, "native_profile_invalid")
            value[key] = item
        return value

    value = json.loads(data, object_pairs_hook=unique)
    if deadline is not None:
        remaining(deadline)
    return value


def collect_helper_costs(
    root: Path,
    result: dict[str, Any],
    *,
    deadline: float | None = None,
    completed_generation: str | None = None,
    retained: list[dict[str, Any]] | None = None,
    samples: list[dict[str, Any]] | None = None,
    start_samples: list[dict[str, Any]] | None = None,
) -> None:
    """One local read after a checked helper exit or final case settlement."""
    facts: dict[str, Any] = {"code": "not_observed", "reason": "unavailable"}
    source = result["resources"].get("helper_source")
    try:
        value = _read_helper_cost_json(root / "helper-costs.json", deadline=deadline)
        if helper_costs_facts(value, source) and value.get("code") == "observed":
            facts = value
        else:
            facts["reason"] = "identity_changed"
    except Exception:
        pass
    if completed_generation is not None:
        if (
            retained is not None
            and len(retained) < 3
            and facts.get("code") == "observed"
            and isinstance(source, dict)
            and source.get("generation") == completed_generation
            and any(
                row["generation"] == completed_generation and row["helper"] == source.get("helper")
                for row in result["generations"]
            )
            and not any(row["source"]["generation"] == completed_generation for row in retained)
        ):
            retained.append({"source": dict(source), "costs": facts})
        return  # Optional diagnostics cannot occupy later transaction-note space.
    # New optional starts may never displace already accepted cost/cleanup facts.
    previous_starts = result["resources"].pop("helper_start_samples", [])
    result["resources"]["helper_costs"] = facts
    if len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()) > MAX_BYTES:
        result["resources"]["helper_costs"] = {"code": "not_observed", "reason": "output_budget"}
        if len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()) > MAX_BYTES:
            result["resources"].pop("helper_costs")
    history = result["resources"].get("helper_cost_history", [])
    for row in retained or []:
        if len(history) >= 3:
            break
        if any(old["source"]["generation"] == row["source"]["generation"] for old in history):
            continue
        candidate = {
            **result,
            "resources": {
                **result["resources"],
                "helper_cost_history": [*history, row],
            },
        }
        if len(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()) > MAX_BYTES:
            budget = {
                **result,
                "resources": {
                    **result["resources"],
                    "helper_costs": {"code": "not_observed", "reason": "output_budget"},
                },
            }
            if len(json.dumps(budget, sort_keys=True, separators=(",", ":")).encode()) <= MAX_BYTES:
                result["resources"]["helper_costs"] = budget["resources"]["helper_costs"]
            break
        history = candidate["resources"]["helper_cost_history"]
        result["resources"]["helper_cost_history"] = history
    partials = result["resources"].get("helper_cost_samples", [])
    for row in samples or []:
        if len(partials) >= 3:
            break
        if any(old["source"]["generation"] == row["source"]["generation"] for old in partials):
            continue
        candidate = {
            **result,
            "resources": {**result["resources"], "helper_cost_samples": [*partials, row]},
        }
        if len(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()) > MAX_BYTES:
            break
        partials = candidate["resources"]["helper_cost_samples"]
        result["resources"]["helper_cost_samples"] = partials
    starts: list[dict[str, Any]] = []
    for row in previous_starts if start_samples is None else start_samples:
        if len(starts) >= 3:
            break
        if any(old["source"]["generation"] == row["source"]["generation"] for old in starts):
            continue
        candidate = {
            **result,
            "resources": {**result["resources"], "helper_start_samples": [*starts, row]},
        }
        if len(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()) > MAX_BYTES:
            break
        starts.append(row)
        result["resources"]["helper_start_samples"] = list(starts)


def install_hook() -> None:
    """Wrap only the app factory; the real bootstrap and health path stay intact."""
    from fastapi import FastAPI

    from muli import server
    from muli.managed_boot import ManagedBootPermit
    from muli.managed_transaction import TransactionRecordV2

    original = server.create_app

    def application(*args: Any, **kwargs: Any) -> FastAPI:
        app: FastAPI = original(*args, **kwargs)
        runtime = app.state.runtime
        require_environment(runtime.config.path.parent, os.geteuid())
        runtime.client_factory = forbidden_model
        permit = runtime.maintenance.permit
        if (
            not isinstance(permit, ManagedBootPermit)
            or permit.role != "target"
            or permit.record.action != "switch"
        ):
            return app
        root = Path(permit.config_path).parent
        if Path(sys.prefix) != root / "a":
            return app
        from muli.platforms.managed_deployment import DeploymentStore

        with DeploymentStore(permit.journal.directory.path) as source:
            base = source.read_slot(permit.record.base.installation)
        require(Path(base.prefix) == root / "b", "native_failure_role")
        permit.lease.check()
        permit.check_paths(runtime.config.path, runtime.data_dir / "muli.sqlite3")
        current = permit.journal.read_current()
        if not isinstance(current, TransactionRecordV2) or current != permit.record:
            raise NativeError("native_failure_generation")
        require(
            not runtime.started and not runtime.maintenance.admission.allowed,
            "native_failure_not_passive",
        )
        require(not runtime.store.migration_allowed, "native_failure_migration")
        runtime.store.revalidate_format()
        require(
            runtime.store.instance_id == current.identity.store_instance, "native_failure_store"
        )
        require(
            runtime.store.main_session()["id"] == current.identity.main_session,
            "native_failure_main",
        )
        require(
            runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2,
            "native_failure_durability",
        )
        write_latest(runtime.store, current)
        runtime.maintenance.unsupported("fixture_target_rejected", lambda: True)
        return app

    server.create_app = application


class Case:
    def __init__(self, root: Path, *, seconds: float = CASE_SECONDS) -> None:
        require(0 < seconds <= CASE_SECONDS, "native_case_budget")
        self.root, self.start = root, time.monotonic()
        self.deadline = self.start + seconds
        self.drive_deadline: float | None = None
        self.stage = "import"
        self.recording = True
        self._notes: dict[str, dict[str, Any]] = {}
        self._invalid_helper_sources: set[str] = set()
        self._helper_source_groups: dict[str, str] = {}
        self._helper_cost_history: list[dict[str, Any]] = []
        self._helper_cost_samples: list[dict[str, Any]] = []
        self._helper_start_samples: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {
            "completed": False,
            "stage": self.stage,
            "code": "",
            "timeline": [],
            "generations": [],
            "resources": {},
        }

    def note(self, stage: str, **facts: Any) -> None:
        require(re.fullmatch(r"[a-z_]{1,40}", stage) is not None, "native_stage_invalid")
        revision_fields = {"revision", "intent", "code", "failure_code", "rollback_attempted"}
        allowed = {
            "phase",
            "activation",
            "request_id",
            "generation",
            "helper",
            "state",
            "pid",
            "boot",
            "result",
            "exit_status",
        } | revision_fields
        require(set(facts).issubset(allowed), "native_fact_invalid")
        if revision_fields.intersection(facts):
            from typing import get_args

            from muli.management_transaction import Code, Intent

            require(
                stage == "transaction"
                and revision_fields.issubset(facts)
                and type(facts["revision"]) is int
                and 1 <= facts["revision"] <= 2**63 - 1
                and facts["intent"] in get_args(Intent)
                and facts["code"] in (None, *get_args(Code))
                and facts["failure_code"] in (None, *get_args(Code))
                and type(facts["rollback_attempted"]) is bool,
                "native_fact_invalid",
            )
        self.stage = stage
        if not self.recording:
            return
        self.result["stage"] = stage
        row = {"elapsed_ms": int((time.monotonic() - self.start) * 1000), "stage": stage, **facts}
        timeline = self.result["timeline"]
        if self._notes.get(stage) != facts:
            require(len(timeline) < 64, "native_timeline_budget")
            timeline.append(row)
            self._notes[stage] = facts
            write_json(self.root / "result.json", self.result)

    def require_start_time(self) -> None:
        """Imports or queueing cannot turn a late worker into a fresh 60s case."""
        require(
            self.drive_deadline is not None and remaining(self.drive_deadline) > CASE_SECONDS,
            "native_case_start_budget",
        )
        remaining(self.deadline)

    async def command(self, *argv: str) -> str:
        from muli.process import run_process

        result = await run_process(
            list(argv),
            timeout=min(5.0, remaining(self.deadline)),
            limit=MAX_BYTES,
            merge_stderr=False,
        )
        require(result.get("exit_code") == 0, "native_command_failed")
        output = result.get("output")
        if not isinstance(output, str):
            raise NativeError("native_command_output")
        remaining(self.deadline)
        return output

    async def pause(self) -> None:
        await asyncio.sleep(min(POLL_SECONDS, remaining(self.deadline)))

    async def http(
        self, client: Any, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        import httpx

        size = min(HTTP_SECONDS, remaining(self.deadline))
        try:
            async with asyncio.timeout(size):
                async with client.stream(method, path, json=body) as response:
                    require(
                        response.headers.get("content-encoding", "identity") == "identity",
                        "native_http_encoding",
                    )
                    data = bytearray()
                    async for part in response.aiter_raw():
                        data.extend(part)
                        require(len(data) <= MAX_BYTES, "native_http_budget")
                    result = json.loads(data)
                    require(isinstance(result, dict), "native_http_object")
                    return response.status_code, result
        except httpx.RequestError:
            raise NativeError("native_http_unavailable") from None

    def journal(self) -> TransactionJournal:
        from muli.management_transaction import TransactionJournal

        return TransactionJournal(
            self.root / "data/management/transaction-v1",
            self.root / "data/muli.sqlite3",
            create=False,
        )

    def record(self, journal: TransactionJournal) -> TransactionRecordV2:
        from muli.managed_transaction import TransactionRecordV2

        record = journal.read_current()
        if not isinstance(record, TransactionRecordV2):
            raise NativeError("native_managed_record_required")
        return record

    async def observed_record(self, journal: TransactionJournal) -> TransactionRecordV2 | None:
        """Defer an unaccepted read after atomic publication, within the same deadline."""
        from muli.management_transaction import TransactionError

        remaining(self.deadline)
        try:
            return self.record(journal)
        except TransactionError as error:
            if error.code != "file_changed":
                if error.code in {
                    "file_budget",
                    "file_not_private_regular",
                    "invalid_record",
                    "directory_changed",
                }:
                    self.note("record_read_failed", state=error.code)
                raise
            # No record or side effect was accepted. Keep all file validation
            # and let the original loop acquire a fresh complete snapshot.
            self.note("record_read_deferred", state="file_changed")
            await self.pause()
            return None

    def deployment(
        self, journal: TransactionJournal, record: TransactionRecordV2
    ) -> tuple[DeploymentAnchor, SlotIdentity, dict[Role, SlotIdentity]]:
        from muli.platforms.managed_deployment import DeploymentStore

        with DeploymentStore(journal.directory.path) as source:
            anchor = source.read_anchor(record.identity.deployment)
            controller = source.read_slot(anchor.controller_slot)
            roles: tuple[Role, ...] = ("base", "target")
            slots = {role: source.read_slot(record.binding(role).installation) for role in roles}
        require(Path(controller.prefix) == self.root / "a", "native_controller_changed")
        return anchor, controller, slots

    def _collect_helper_cost_sample(self, record: TransactionRecordV2) -> None:
        """Read optional history only after the original helper identity observation."""
        source = self.result["resources"].get("helper_source")
        if (
            len(self._helper_cost_samples) >= 3
            or record.generation in self._invalid_helper_sources
            or not isinstance(source, dict)
            or source.get("generation") != record.generation
            or any(
                row["source"]["generation"] == record.generation
                for row in self._helper_cost_samples
            )
        ):
            return
        try:
            value = _read_helper_cost_json(
                self.root / "helper-costs-checkpoint.json", deadline=self.deadline
            )
            if (
                helper_cost_sample_facts(value, source)
                and value["sample"]["revision"] <= record.revision
            ):
                self._helper_cost_samples.append({"source": dict(source), **value})
        except Exception:
            pass  # Missing partial facts never change the transaction or its result.

    def _discard_helper_start_samples(self, generation: str) -> None:
        self._helper_start_samples[:] = [
            row for row in self._helper_start_samples if row["source"]["generation"] != generation
        ]
        resources = self.result["resources"]
        if "helper_start_samples" in resources:
            rows = [
                row
                for row in resources["helper_start_samples"]
                if row["source"]["generation"] != generation
            ]
            if rows:
                resources["helper_start_samples"] = rows
            else:
                resources.pop("helper_start_samples")

    def _collect_helper_start_sample(
        self, record: TransactionRecordV2, *, deadline: float | None = None
    ) -> None:
        """Reuse an accepted source on the existing poll, including unchanged revisions."""
        source = self.result["resources"].get("helper_source")
        if (
            record.generation in self._invalid_helper_sources
            or not isinstance(source, dict)
            or source.get("generation") != record.generation
        ):
            self._discard_helper_start_samples(record.generation)
            return
        if not any(
            row["generation"] == source["generation"] and row["helper"] == source["helper"]
            for row in self.result["generations"]
        ):
            return
        old = next(
            (
                row
                for row in self._helper_start_samples
                if row["source"]["generation"] == record.generation
            ),
            None,
        )
        if old is not None and old["source"] != source:
            self._discard_helper_start_samples(record.generation)
            return
        try:
            value = _read_helper_cost_json(
                self.root / "helper-start-checkpoint.json",
                deadline=self.deadline if deadline is None else deadline,
            )
            if not helper_start_sample_facts(value, source):
                return
            sample = value["sample"]
            if sample["revision"] > record.revision or not any(
                row.get("stage") == "transaction"
                and row.get("generation") == record.generation
                and all(row.get(key) == sample[key] for key in ("revision", "phase", "intent"))
                for row in self.result["timeline"]
            ):
                return
            if old is not None:
                previous = old["sample"]
                current_key = sample["revision"], HELPER_START_BOUNDARIES.index(sample["kind"])
                old_key = previous["revision"], HELPER_START_BOUNDARIES.index(previous["kind"])
                if current_key <= old_key or (
                    sample["revision"] == previous["revision"]
                    and sample["elapsed_ms"] < previous["elapsed_ms"]
                ):
                    return
            accepted = {"source": dict(source), **value}
            if old is not None:
                self._helper_start_samples[self._helper_start_samples.index(old)] = accepted
            elif len(self._helper_start_samples) < 3:
                self._helper_start_samples.append(accepted)
        except Exception:
            pass  # Ordinary local observation failures cannot replace the real outcome.

    async def helper_empty(self, probe: SystemdProbe, name: str, argv: tuple[str, ...]) -> bool:
        from muli.platforms.linux_controller import CgroupObservation
        from muli.platforms.linux_transaction import ObservationError

        async def collected_after_failure(error: ObservationError) -> bool:
            # Transient units may be collected between any two bus calls. Only
            # a fresh exact empty listing proves this, never a failed query or
            # a snapshot validation error. Do not rebind any source identity.
            if error.args != ("systemd_observation_unavailable",):
                raise error
            try:
                exists = await probe.helper_exists(name)
            except ObservationError:
                raise error from None
            if exists:
                raise error
            self.note("helper_exit", helper=name, state="collected")
            return True

        if not await probe.helper_exists(name):
            self.note("helper_exit", helper=name, state="collected")
            return True
        try:
            snapshot = await probe._snapshot(await probe._unit_path(name))
        except ObservationError as error:
            return await collected_after_failure(error)
        generations = [row for row in self.result["generations"] if row["helper"] == name]
        generation = generations[0]["generation"] if len(generations) == 1 else None
        membership = (
            f"user@{os.geteuid()}.service" in Path(snapshot.cgroup).parts or not snapshot.cgroup
        )
        if generation is not None and (
            snapshot.name != name or snapshot.argv != argv or not membership
        ):
            self._invalid_helper_sources.add(generation)
            self._discard_helper_start_samples(generation)
            self.result["resources"].pop("helper_source", None)
        require(snapshot.name == name and snapshot.argv == argv, "native_helper_identity")
        require(membership, "native_helper_membership")
        if generation is not None:
            previous = self.result["resources"].get("helper_source")
            invocation = snapshot.invocation
            if (
                isinstance(previous, dict)
                and previous["generation"] == generation
                and (
                    previous["invocation"] != invocation
                    or (snapshot.pid != 0 and previous["pid"] != snapshot.pid)
                    or (
                        snapshot.pid != 0
                        and self._helper_source_groups.get(generation, snapshot.cgroup)
                        != snapshot.cgroup
                    )
                )
            ):
                self._invalid_helper_sources.add(generation)
            if generation in self._invalid_helper_sources:
                self._discard_helper_start_samples(generation)
                self.result["resources"].pop("helper_source", None)
            elif (
                0 < snapshot.pid < 2**31
                and re.fullmatch(r"[a-f0-9]{32}", invocation) is not None
                and invocation != "0" * 32
                and f"user@{os.geteuid()}.service" in Path(snapshot.cgroup).parts
            ):
                self.result["resources"]["helper_source"] = {
                    "generation": generation,
                    "helper": name,
                    "uid": os.geteuid(),
                    "pid": snapshot.pid,
                    "invocation": invocation,
                }
                self._helper_source_groups[generation] = snapshot.cgroup
        try:
            details = await probe._properties(
                await probe._unit_path(name), "Service", (("Result", "s"), ("ExecMainStatus", "i"))
            )
        except ObservationError as error:
            return await collected_after_failure(error)
        self.note(
            "helper_exit",
            helper=name,
            state=snapshot.active_state,
            pid=snapshot.pid,
            result=details["Result"],
            exit_status=details["ExecMainStatus"],
        )
        return (
            snapshot.active_state == "inactive"
            and snapshot.pid == 0
            and snapshot.job == (0, "/")
            and CgroupObservation(Path("/sys/fs/cgroup"), MAX_BYTES).empty(snapshot.cgroup)
        )

    def read_database(self, record: TransactionRecordV2, *, expect_latest: bool) -> dict[str, Any]:
        from muli.format_evidence import read_format_evidence
        from muli.management_projection import read_projection_connection

        database = self.root / "data/muli.sqlite3"
        initial = self.result["resources"].get("initial")
        require(isinstance(initial, dict), "native_initial_identity_missing")
        identity = database.stat()
        require(
            (identity.st_dev, identity.st_ino) == (initial["device"], initial["inode"]),
            "native_database_replaced",
        )
        require(
            (record.identity.store_instance, record.identity.main_session)
            == (initial["store"], initial["main"]),
            "native_initial_identity_changed",
        )
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA query_only=ON")
            receipt = read_projection_connection(db)
            require(
                receipt is not None
                and receipt.generation == record.generation
                and receipt.revision == record.revision,
                "native_exact_projection",
            )
            evidence = read_format_evidence(db)
            require(
                (evidence.store_instance, evidence.main_session)
                == (record.identity.store_instance, record.identity.main_session),
                "native_store_changed",
            )
            row = db.execute(
                "SELECT data FROM objects WHERE kind='management' AND id=?", (record.request_id,)
            ).fetchone()
            require(row is not None, "native_plan_missing")
            plan: dict[str, Any] = json.loads(row[0])
            require(plan["transaction"] == record.public(), "native_plan_projection")
            count = db.execute(
                "SELECT count(*) FROM events WHERE type='management.completed' AND json_extract(data,'$.id')=?",
                (record.request_id,),
            ).fetchone()[0]
            require(count == 1, "native_completion_count")
            if expect_latest:
                latest = db.execute(
                    "SELECT data FROM objects WHERE kind='fixture' AND id='latest'"
                ).fetchone()
                require(
                    latest is not None
                    and json.loads(latest[0])["after_prepare"] == record.generation,
                    "native_latest_missing",
                )
                require(
                    db.execute(
                        "SELECT state FROM channel_outbox WHERE id='latest-outbox'"
                    ).fetchone()
                    == ("unknown",),
                    "native_unknown_changed",
                )
                require(
                    db.execute("SELECT state FROM ingress WHERE id='latest-input'").fetchone()
                    == ("pending",),
                    "native_input_changed",
                )
            return plan

    def freeze_initial(self) -> None:
        from muli.format_evidence import read_format_evidence

        database = self.root / "data/muli.sqlite3"
        before = database.stat()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("PRAGMA query_only=ON")
            evidence = read_format_evidence(db)
        after = database.stat()
        require(
            (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
            "native_database_replaced",
        )
        self.result["resources"]["initial"] = {
            "store": evidence.store_instance,
            "main": evidence.main_session,
            "device": before.st_dev,
            "inode": before.st_ino,
        }
        self.note("identity_frozen")

    async def transaction(
        self, client: Any, password: str, action: str, slot: SlotIdentity, previous_boot: str
    ) -> str:
        from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
        from muli.platforms.linux_transaction import ProbeLimits
        from muli.platforms.managed_probe import ManagedSystemdProbe

        self.note("prepare", state=action)
        status, plan = await self.http(
            client,
            "POST",
            "/api/management/transactions",
            {
                "action": action,
                "candidate": {
                    "prefix": slot.prefix,
                    "python": slot.lexical_python,
                    "package_root": slot.package.root,
                },
            },
        )
        require(status == 200 and isinstance(plan.get("id"), str), "native_prepare_failed")
        request_id, routing = plan["id"], plan.get("routing")
        probe = ManagedSystemdProbe(
            limits=ProbeLimits(seconds=HTTP_SECONDS, command_bytes=MAX_BYTES)
        )
        journal: TransactionJournal | None = None
        position: tuple[str, int] | None = None
        try:
            while True:
                remaining(self.deadline)
                try:
                    status, current = await self.http(
                        client, "GET", "/api/management/transactions/current"
                    )
                    if status == 401:
                        status, _ = await self.http(
                            client, "POST", "/api/auth/login", {"password": password}
                        )
                        require(status == 200, "native_login_failed")
                        await self.pause()
                        continue
                except NativeError as error:
                    if str(error) != "native_http_unavailable":
                        raise
                    status, current = 0, {}
                if (
                    journal is None
                    and (self.root / "data/management/transaction-v1/state.json").exists()
                ):
                    journal = self.journal()
                if journal is not None:
                    record = await self.observed_record(journal)
                    if record is None:
                        continue
                    if record.request_id == request_id:
                        name = managed_helper_unit_name(record)
                        history = self.result["generations"]
                        if not any(item["generation"] == record.generation for item in history):
                            require(len(history) < 3, "native_generation_budget")
                            history.append(
                                {
                                    "deployment": record.identity.deployment,
                                    "generation": record.generation,
                                    "helper": name,
                                }
                            )
                        fresh_position = (record.generation, record.revision)
                        if fresh_position != position:
                            self.note(
                                "transaction",
                                request_id=request_id,
                                generation=record.generation,
                                phase=record.phase,
                                activation=record.activation,
                                revision=record.revision,
                                intent=record.intent,
                                code=record.code,
                                failure_code=record.failure_code,
                                rollback_attempted=record.rollback_attempted,
                            )
                            anchor, controller, _ = self.deployment(journal, record)
                            await self.helper_empty(
                                probe, name, managed_helper_arguments(anchor, controller)
                            )
                            self._collect_helper_cost_sample(record)
                            position = fresh_position
                        self._collect_helper_start_sample(record)
                        require(
                            record.phase != "recovery_required" and record.activation != "blocked",
                            "native_transaction_blocked",
                        )
                        if (
                            record.resolved
                            and status == 200
                            and current.get("transaction") == record.public()
                        ):
                            anchor, controller, slots = self.deployment(journal, record)
                            if not await self.helper_empty(
                                probe, name, managed_helper_arguments(anchor, controller)
                            ):
                                await self.pause()
                                continue
                            collect_helper_costs(
                                self.root,
                                self.result,
                                deadline=self.deadline,
                                completed_generation=record.generation,
                                retained=self._helper_cost_history,
                            )
                            # A resolved HTTP response can precede helper lock release.
                            with journal.locked():
                                require(self.record(journal) == record, "native_generation_changed")
                            expect_latest = (
                                action == "switch" and Path(slot.prefix) == self.root / "a"
                            )
                            require(
                                (record.phase, record.activation)
                                == ("rolled_back" if expect_latest else "committed", "released"),
                                "native_wrong_decision",
                            )
                            expected_role = "base" if expect_latest else "target"
                            observed = await probe.read_managed(
                                journal, record, anchor, controller, slots
                            )
                            require(
                                observed.service.process is not None
                                and expected_role in observed.process_roles,
                                "native_running_role",
                            )
                            unit = observed.service.unit
                            require(
                                unit.invocation != previous_boot
                                and unit.pid > 0
                                and f"user@{os.geteuid()}.service" in Path(unit.cgroup).parts,
                                "native_service_membership",
                            )
                            final = self.read_database(record, expect_latest=expect_latest)
                            require(final.get("routing") == routing, "native_routing_changed")
                            self.note(
                                "projection",
                                request_id=request_id,
                                generation=record.generation,
                                phase=record.phase,
                                activation=record.activation,
                                boot=unit.invocation,
                                pid=unit.pid,
                            )
                            return unit.invocation
                await self.pause()
        finally:
            if journal is not None:
                journal.__exit__()

    async def drive(self) -> None:
        import fastapi
        import httpx
        import pydantic
        import uvicorn

        import muli
        from muli.config import ConfigFile
        from muli.platforms.filesystem_identity import verify_local_filesystems
        from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe
        from muli.platforms.managed_deployment import register_local_slot
        from muli.service import UserService

        _ = fastapi, pydantic, uvicorn, sqlite3
        settings = fixture(self.root)
        require(
            Path(sys.prefix) == self.root / "a"
            and Path(sys.executable) == self.root / "a/bin/python",
            "native_prefix_invalid",
        )
        require(
            Path(muli.__file__).parent
            == self.root
            / "a/lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages/muli",
            "native_import_invalid",
        )
        group = membership(settings["harness_unit"], settings["uid"])
        self.result["resources"]["harness"] = kernel_limits(
            group, memory=HARNESS_MEMORY, tasks=HARNESS_TASKS
        )
        require((group / "memory.oom.group").read_text().strip() == "1", "native_harness_oom")
        manager = next(
            parent for parent in group.parents if parent.name == f"user@{os.geteuid()}.service"
        )
        self.result["resources"]["manager"] = kernel_limits(manager, memory=MEMORY, tasks=TASKS)
        self.result["resources"]["environment"] = environment_facts(self.root, settings["uid"])
        require_environment(self.root, settings["uid"])
        self.note("imports_verified")
        verify_local_filesystems([self.root])
        slots = {}
        for name in ("a", "b"):
            prefix = self.root / name
            package = (
                prefix
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages/muli"
            )
            slot = register_local_slot(prefix, prefix / "bin/python", package)
            require(slot.package.digest == settings["package_digest"], "native_package_changed")
            slots[name] = slot
        self.note("slots_verified")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        password = os.urandom(24).hex()
        self.require_start_time()
        config = ConfigFile(self.root / "config.json")
        config.update(
            {
                "server": {"host": "127.0.0.1", "port": port, "password": password},
                "workspace_roots": [str(self.root)],
                "onboarding_pending": False,
                "updates": {"check_on_start": False},
                "compaction": {"enabled": False},
                "memory": {"enabled": False, "auto_extract": False},
                "plugins": {"enabled": False},
                "skills": {"enabled": False},
                "extensions": {"watch_enabled": False},
                "attention": {"enabled": False},
                "notifications": {"enabled": False},
                "system_events": {"enabled": False},
                "mcp": {"mcpServers": {}},
            }
        )
        service = UserService(config.path, self.root / "data", executable=slots["a"].lexical_python)
        require(
            service.path == self.root / "home/.config/systemd/user/muli.service", "native_unit_path"
        )
        unit_files().create(self.root, service.render().encode("utf-8"), limit=MAX_BYTES)
        self.require_start_time()
        remaining(self.deadline)
        self.note("manager_start")
        await self.command("systemctl", "--user", "daemon-reload")
        self.require_start_time()
        await self.command("systemctl", "--user", "start", service.unit)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Origin": f"http://127.0.0.1:{port}", "Accept-Encoding": "identity"},
            trust_env=False,
            follow_redirects=False,
            timeout=HTTP_SECONDS,
        ) as client:
            while True:
                try:
                    status, _ = await self.http(
                        client, "POST", "/api/auth/login", {"password": password}
                    )
                    require(status == 200, "native_login_failed")
                    break
                except NativeError as error:
                    if str(error) != "native_http_unavailable":
                        raise
                    await self.pause()
            probe = SystemdProbe(limits=ProbeLimits(seconds=HTTP_SECONDS, command_bytes=MAX_BYTES))
            initial = await probe.read(service)
            require(
                initial.process is not None and initial.process.uid == os.geteuid(),
                "native_origin_process",
            )
            require(
                f"user@{os.geteuid()}.service" in Path(initial.unit.cgroup).parts,
                "native_origin_membership",
            )
            self.note("origin_ready", boot=initial.unit.invocation, pid=initial.unit.pid)
            self.freeze_initial()
            boot = await self.transaction(
                client, password, "adopt", slots["a"], initial.unit.invocation
            )
            boot = await self.transaction(client, password, "switch", slots["b"], boot)
            await self.transaction(client, password, "switch", slots["a"], boot)
        self.result["resources"]["manager_peak"] = (manager / "memory.peak").read_text().strip()
        self.result["completed"] = True
        self.note("complete")

    async def cleanup(self) -> None:
        from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
        from muli.platforms.linux_controller import CgroupObservation
        from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe
        from muli.platforms.managed_probe import _UNIT_PATH

        settings = fixture(self.root)
        require_environment(self.root, settings["uid"])
        membership("muli-native-cleanup.service", settings["uid"])
        saved = (
            read_json(self.root / "result.json")
            if (self.root / "result.json").exists()
            else self.result
        )
        self.result = saved
        self.recording = False
        history = saved.get("generations", [])
        require(isinstance(history, list) and len(history) <= 3, "native_cleanup_history")
        probe = SystemdProbe(
            limits=ProbeLimits(
                seconds=min(HTTP_SECONDS, remaining(self.deadline)), command_bytes=MAX_BYTES
            )
        )
        state = self.root / "data/management/transaction-v1/state.json"
        if state.exists():
            with self.journal() as journal:
                record = self.record(journal)
                if not any(item["generation"] == record.generation for item in history):
                    require(len(history) < 3, "native_cleanup_history")
                    history.append(
                        {
                            "deployment": record.identity.deployment,
                            "generation": record.generation,
                            "helper": managed_helper_unit_name(record),
                        }
                    )
                anchor, controller, _ = self.deployment(journal, record)
                expected_argv = managed_helper_arguments(anchor, controller)
                for item in history:
                    identity = json.dumps(
                        [2, item["deployment"], item["generation"]], separators=(",", ":")
                    ).encode()
                    name = "muli-transaction-" + hashlib.sha256(identity).hexdigest() + ".service"
                    require(
                        item["helper"] == name and item["deployment"] == anchor.digest,
                        "native_cleanup_helper",
                    )
                    if await probe.helper_exists(name):
                        unit = await probe._snapshot(await probe._unit_path(name))
                        require(unit.argv == expected_argv, "native_cleanup_helper")
                        await self.command("systemctl", "--user", "stop", name)
                        while not await self.helper_empty(probe, name, expected_argv):
                            await self.pause()
        service_group = ""
        service_present = await service_exists(probe)
        if service_present:
            unit = await probe._snapshot(_UNIT_PATH)
            require(
                Path(unit.fragment) == self.root / "home/.config/systemd/user/muli.service",
                "native_cleanup_fragment",
            )
            await self.command("systemctl", "--user", "stop", "muli.service")
            unit = await probe._snapshot(_UNIT_PATH)
            require(
                Path(unit.fragment) == self.root / "home/.config/systemd/user/muli.service",
                "native_cleanup_fragment",
            )
            require(
                unit.pid == 0
                and unit.job == (0, "/")
                and CgroupObservation(Path("/sys/fs/cgroup"), MAX_BYTES).empty(unit.cgroup),
                "native_service_not_empty",
            )
            service_group = unit.cgroup
        if state.exists():
            with (
                self.journal() as journal,
                journal.locked() as control,
                journal.writer_lease() as lease,
            ):
                record = self.record(journal)
                if saved.get("completed") is True:
                    self.read_database(record, expect_latest=True)

                def guard_unit() -> None:
                    remaining(self.deadline)
                    control.check()
                    lease.check()
                    require(
                        not service_present
                        or CgroupObservation(Path("/sys/fs/cgroup"), MAX_BYTES).empty(
                            service_group
                        ),
                        "native_service_not_empty",
                    )

                unit_files().remove(self.root, limit=MAX_BYTES, guard=guard_unit)
        else:
            # Without the real journal/lease there is no authority to remove
            # an existing app leaf, even when a stale receipt claims it.
            require(
                not os.path.lexists(self.root / "home/.config/systemd/user/muli.service"),
                "native_unit_birth_unconfirmed",
            )
        remaining(self.deadline)


async def run(mode: str, root: Path) -> int:
    case = Case(root, seconds=CLEANUP_SECONDS if mode == "cleanup" else CASE_SECONDS)
    try:
        if mode == "cleanup":
            deadline = float(os.environ.get("MULI_NATIVE_CLEANUP_DEADLINE", "nan"))
            require(case.start < deadline <= case.deadline, "native_cleanup_deadline")
            case.deadline = deadline
            async with asyncio.timeout(remaining(case.deadline)):
                await case.cleanup()
        else:
            end = float(os.environ.get("MULI_NATIVE_EXECUTE_DEADLINE", "nan"))
            require(
                math.isfinite(end)
                and case.start < end <= case.start + HARNESS_SECONDS + CLEANUP_SECONDS,
                "native_execute_deadline",
            )
            case.drive_deadline = end - CLEANUP_SECONDS
            require(remaining(case.drive_deadline) > CASE_SECONDS, "native_case_start_budget")
            case.deadline = min(case.deadline, case.drive_deadline)
            case.note("import")
            # The absolute budget also includes every synchronous import and
            # verification boundary before the first manager effect.
            async with asyncio.timeout(remaining(case.deadline)):
                await case.drive()
        return 0
    except (Exception, asyncio.CancelledError) as error:
        code = type(error).__name__
        if isinstance(error, NativeError) and re.fullmatch(r"native_[a-z_]{1,57}", str(error)):
            code = str(error)
        if mode != "cleanup":
            case.result["code"] = code
            case.note(case.stage)
            write_json(root / "result.json", case.result)
        print("native_" + mode + "_failed:" + case.stage + ":" + code, file=sys.stderr)
        try:
            from muli.failure_diagnostics import MAX_FAILURE_BYTES, capture_failure

            diagnostic = json.dumps(
                capture_failure(error), ensure_ascii=False, separators=(",", ":")
            )
            if len(diagnostic.encode("utf-8")) <= MAX_FAILURE_BYTES:
                print("native_failure_diagnostic:" + diagnostic, file=sys.stderr)
        except Exception:
            pass  # Optional safe frames cannot replace the first failure or cleanup.
        return 1
    finally:
        if mode != "cleanup":
            try:
                if isinstance(case.result["resources"].get("helper_source"), dict):
                    final_deadline = case.drive_deadline or case.deadline
                    remaining(final_deadline)
                    with case.journal() as journal:
                        case._collect_helper_start_sample(
                            case.record(journal), deadline=final_deadline
                        )
            except Exception:
                pass  # A missing final boundary cannot replace the original case outcome.
            try:
                collect_helper_costs(
                    root,
                    case.result,
                    deadline=case.drive_deadline,
                    retained=case._helper_cost_history,
                    samples=case._helper_cost_samples,
                    start_samples=case._helper_start_samples,
                )
                write_json(root / "result.json", case.result)
            except Exception:
                pass  # Optional observation cannot replace the case's first outcome.


def main() -> int:
    if sys.argv[1:2] not in (["drive"], ["cleanup"]):
        require_resource_envelope(root=bool(sys.argv[1:2] and sys.argv[1] in ROOT_RESOURCE_MODES))
    if len(sys.argv) == 8 and sys.argv[1] == "project-manager-instance":
        try:
            value = read_manager_instance(
                int(sys.argv[2]),
                int(sys.argv[3]),
                int(sys.argv[4]),
                sys.argv[5],
                float(sys.argv[6]),
                int(sys.argv[7]),
            )
        except Exception:
            value = instance_unavailable("invalid_reply", MAX_BYTES)
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) == 8 and sys.argv[1] == "diagnose-manager-instance":
        try:
            value = asyncio.run(
                diagnose_manager_instance(
                    sys.argv[2],
                    int(sys.argv[3]),
                    Path(sys.argv[4]),
                    float(sys.argv[5]),
                    int(sys.argv[6]),
                    json.loads(sys.argv[7]),
                )
            )
        except Exception:
            # Malformed arguments must never export arbitrary exception text.
            value = {"code": "native_diagnostic_invalid", "used": MAX_BYTES}
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) == 9 and sys.argv[1] == "bootstrap-files":
        try:
            value = bootstrap_files(
                sys.argv[2],
                sys.argv[3],
                int(sys.argv[4]),
                int(sys.argv[5]),
                sys.argv[6],
                float(sys.argv[7]),
                int(sys.argv[8]),
            )
        except Exception:
            value = {"code": "native_bootstrap_invalid", "used": MAX_BYTES}
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) == 7 and sys.argv[1] == "manager-template":
        try:
            value = asyncio.run(
                manager_template(
                    sys.argv[2],
                    int(sys.argv[3]),
                    Path(sys.argv[4]),
                    float(sys.argv[5]),
                    int(sys.argv[6]),
                )
            )
        except Exception:
            value = {"code": "native_manager_template", "used": MAX_BYTES}
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) == 8 and sys.argv[1] == "project-setup-context":
        # Never print exception text (which might contain untrusted environment data).
        try:
            value = read_setup_context(
                int(sys.argv[2]),
                int(sys.argv[3]),
                int(sys.argv[4]),
                sys.argv[5],
                float(sys.argv[6]),
                int(sys.argv[7]),
            )
        except Exception:
            value = {"facts": context_unavailable("invalid_reply"), "used": MAX_BYTES}
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "describe":
        print(json.dumps(describe(), sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) == 7 and sys.argv[1] == "diagnose":
        value = asyncio.run(
            diagnose(
                sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], float(sys.argv[6])
            )
        )
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) in {6, 7} and sys.argv[1] == "diagnose-setup":
        value = asyncio.run(
            diagnose_setup(
                sys.argv[2],
                int(sys.argv[3]),
                Path(sys.argv[4]),
                float(sys.argv[5]),
                int(sys.argv[6]) if len(sys.argv) == 7 else MAX_BYTES,
                include_usage=len(sys.argv) == 7,
            )
        )
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if (len(sys.argv) == 7 and sys.argv[1] == "manager-idle") or (
        len(sys.argv) == 9 and sys.argv[1] == "manager-ready"
    ):
        value = asyncio.run(
            manager_state(
                sys.argv[1],
                sys.argv[2],
                int(sys.argv[3]),
                Path(sys.argv[4]),
                float(sys.argv[5]),
                int(sys.argv[6]),
                started=float(sys.argv[7]) if len(sys.argv) == 9 else 0.0,
                previous=sys.argv[8] if len(sys.argv) == 9 else "",
            )
        )
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    require(len(sys.argv) == 3 and sys.argv[1] in {"drive", "cleanup"}, "native_arguments")
    return asyncio.run(run(sys.argv[1], Path(sys.argv[2])))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NativeError as error:
        if str(error) != "native_resource_envelope":
            raise
        print("native_resource_envelope", file=sys.stderr)
        raise SystemExit(1) from None
