"""Hosted-CI-only worker. No manager, protocol, probe or backend is replaced."""

from __future__ import annotations

import asyncio
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
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from muli.managed_transaction import TransactionRecordV2
    from muli.management_transaction import Role, TransactionJournal
    from muli.platforms.linux_transaction import CommandRunner, SystemdProbe
    from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity
    from muli.store import Store

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
PROCESS_STATES = frozenset("RSDZTtXxKWPI")
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


def system_executables() -> dict[str, str]:
    """Classify only existing, root-owned system images at the fixed system paths."""
    result: dict[str, str] = {}
    for kind, name in (("manager", "systemd"), ("executor", "systemd-executor")):
        for parent in SYSTEM_EXECUTABLE_ROOTS:
            try:
                path = (parent / name).resolve(strict=True)
                info = path.stat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022:
                    result[kind] = str(path)
                    break
            except OSError:
                continue
    return result


def bootstrap_data() -> bytes:
    return (
        b"[Unit]\nDescription=MuLi native bootstrap\nDefaultDependencies=no\n"
        b"Requires=basic.target dbus.socket\nAfter=basic.target dbus.socket\n"
    )


def bootstrap_files(
    mode: str, user: str, uid: int, gid: int, manifest: str, deadline: float, limit: int
) -> dict[str, Any]:
    """Operate only on this receipt's fixed tree through pinned directory descriptors."""
    import pwd

    from muli.platforms.linux_transaction import _read_regular

    used = 0
    handles: list[int] = []
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
            isinstance(value, dict) and set(value) == {"directories", "file"},
            "native_bootstrap_invalid",
        )
        directories, identity = value["directories"], value["file"]

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
            and (identity is None or (pair(identity) and len(directories) == 5)),
            "native_bootstrap_invalid",
        )
        require(
            mode != "create" or (len(directories) == 2 and identity is None),
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
        name = user + ".target"
        if len(handles) == 5:
            if mode == "create":
                data = bootstrap_data()
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
                    identity = [info.st_dev, info.st_ino]
            leaf: os.stat_result | None
            try:
                leaf = os.stat(name, dir_fd=handles[-1], follow_symlinks=False)
            except FileNotFoundError:
                require(mode == "remove" or identity is None, "native_bootstrap_changed")
                leaf = None
            if leaf is not None:
                require(
                    identity is not None
                    and [leaf.st_dev, leaf.st_ino] == identity
                    and stat.S_ISREG(leaf.st_mode)
                    and stat.S_IMODE(leaf.st_mode) == 0o644
                    and (leaf.st_uid, leaf.st_gid) == (0, 0),
                    "native_bootstrap_changed",
                )
                data = _read_regular(Path(name), limit - used, directory_fd=handles[-1])
                charge(data)
                require(data == bootstrap_data(), "native_bootstrap_changed")
                linked = os.stat(name, dir_fd=handles[-1], follow_symlinks=False)
                require(
                    all(
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
            for index in range(len(handles)):
                check_directory(index)
            if mode == "remove":
                if leaf is not None:
                    remaining(deadline)
                    os.unlink(name, dir_fd=handles[-1])
                identity = None
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
        for index in range(len(directories)):
            check_directory(index)
        account = pwd.getpwnam(user)
        require(
            (account.pw_uid, account.pw_gid, account.pw_dir) == (uid, gid, str(volume / "home")),
            "native_bootstrap_identity",
        )
        remaining(deadline)
        return {"code": "verified", "directories": directories, "file": identity, "used": used}
    except Exception as error:
        code = str(error) if isinstance(error, NativeError) else "native_bootstrap_invalid"
        return {"code": code, "used": limit}
    finally:
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
                ("ExecStart", "a(sasbttttuii)"),
                ("ExecStartEx", "a(sasasttttuii)"),
            )
            service = await probe._properties(path, "Service", fields)
            entries = service["ExecStart"]
            program = system_executables().get("manager")
            if program is None:
                raise NativeError("native_manager_template")
            require(
                isinstance(entries, list)
                and len(entries) == 1
                and isinstance(entries[0], list)
                and len(entries[0]) == 10,
                "native_manager_template",
            )
            entry = entries[0]
            extended = service["ExecStartEx"]
            require(
                isinstance(entry[0], str)
                and Path(entry[0]).resolve(strict=True) == Path(program)
                and entry[1] == [entry[0], "--user"]
                and entry[2] is False
                and all(type(value) is int and 0 <= value < 2**64 for value in entry[3:7])
                and type(entry[7]) is int
                and 0 <= entry[7] < 2**32
                and all(type(value) is int and -(2**31) <= value < 2**31 for value in entry[8:])
                and extended == [[entry[0], entry[1], [], *entry[3:]]]
                and service["MainPID"] == 0
                and before
                == {"Id": name, "ActiveState": "inactive", "SubState": "dead", "Job": [0, "/"]}
                and await probe._properties(
                    path,
                    "Unit",
                    (("Id", "s"), ("ActiveState", "s"), ("SubState", "s"), ("Job", "(uo)")),
                )
                == before
                and await probe._properties(path, "Service", fields) == service,
                "native_manager_template",
            )
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
                argv,
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
                    await asyncio.sleep(min(delay, remaining(deadline)))
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
        )
        if io.used > byte_limit:
            # Saturation reserves the whole allowance; this is explicitly a failed read.
            io.used = byte_limit
            result["code"] = "native_diagnostic_output_budget"
    result["used"] = io.used
    return result


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
        }
        require(set(facts).issubset(allowed), "native_fact_invalid")
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

    async def helper_empty(self, probe: SystemdProbe, name: str, argv: tuple[str, ...]) -> bool:
        from muli.platforms.linux_controller import CgroupObservation

        if not await probe.helper_exists(name):
            self.note("helper_exit", helper=name, state="collected")
            return True
        snapshot = await probe._snapshot(await probe._unit_path(name))
        require(snapshot.name == name and snapshot.argv == argv, "native_helper_identity")
        require(
            f"user@{os.geteuid()}.service" in Path(snapshot.cgroup).parts or not snapshot.cgroup,
            "native_helper_membership",
        )
        details = await probe._properties(
            await probe._unit_path(name), "Service", (("Result", "s"), ("ExecMainStatus", "i"))
        )
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
                    record = self.record(journal)
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
                            )
                            anchor, controller, _ = self.deployment(journal, record)
                            await self.helper_empty(
                                probe, name, managed_helper_arguments(anchor, controller)
                            )
                            position = fresh_position
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
        service.path.parent.mkdir(parents=True, mode=0o700)
        service.path.write_text(service.render(), encoding="utf-8")
        service.path.chmod(0o600)
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
        if await service_exists(probe):
            unit = await probe._snapshot(await probe._unit_path("muli.service"))
            require(
                Path(unit.fragment) == self.root / "home/.config/systemd/user/muli.service",
                "native_cleanup_fragment",
            )
            await self.command("systemctl", "--user", "stop", "muli.service")
            unit = await probe._snapshot(await probe._unit_path("muli.service"))
            require(
                unit.pid == 0
                and unit.job == (0, "/")
                and CgroupObservation(Path("/sys/fs/cgroup"), MAX_BYTES).empty(unit.cgroup),
                "native_service_not_empty",
            )
        if state.exists():
            with self.journal() as journal, journal.writer_lease():
                record = self.record(journal)
                if saved.get("completed") is True:
                    self.read_database(record, expect_latest=True)
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
        return 1


def main() -> int:
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
    raise SystemExit(main())
