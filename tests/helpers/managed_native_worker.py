"""Hosted-CI-only worker. No manager, protocol, probe or backend is replaced."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
CASE_SECONDS = 60
CLEANUP_SECONDS = 5
MEMORY = 1024**3
TASKS = 128
HARNESS_MEMORY = MEMORY // 4
HARNESS_TASKS = 32
HTTP_SECONDS = 2.0
POLL_SECONDS = 0.1
PROCESS_STATES = frozenset("RSDZTtXxKWPI")
SYSTEM_EXECUTABLE_ROOTS = (Path("/usr/lib/systemd"), Path("/lib/systemd"))
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


def kernel_limits(group: Path, *, memory: int, tasks: int) -> dict[str, str]:
    result = {}
    for name, maximum in (("memory.max", memory), ("memory.swap.max", 0), ("pids.max", tasks)):
        value = (group / name).read_text().strip()
        require(value.isdecimal() and int(value) <= maximum, "native_kernel_limit")
        require(not maximum or int(value) > 0, "native_kernel_limit")
        result[name] = value
    values = (group / "cpu.max").read_text().split()
    require(len(values) == 2 and all(item.isdecimal() for item in values), "native_kernel_cpu")
    quota, period = map(int, values)
    require(0 < quota <= period, "native_kernel_cpu")
    result["cpu.max"] = " ".join(values)
    controllers = (group / "cgroup.controllers").read_text().split()
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
                facts["journal"] = await observe_setup_journal(
                    probe, path, uid, service["MainPID"], group, read, runner, deadline
                )
    facts["failure"] = "none"
    return facts


async def diagnose_setup(user: str, uid: int, home: Path, deadline: float) -> dict[str, Any]:
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
    }
    try:
        account = pwd.getpwnam(user)
        require((account.pw_uid, account.pw_dir) == (uid, str(home)), "native_diagnostic_identity")
        facts["stage"] = "import"
        from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe
        from muli.process import run_process

        used = 0

        def charge(data: bytes) -> None:
            nonlocal used
            used += len(data)
            require(used <= MAX_BYTES, "native_diagnostic_output_budget")

        async def command(
            argv: list[str], *, timeout: float, limit: int, merge_stderr: bool = False
        ) -> dict[str, Any]:
            require(not merge_stderr, "native_diagnostic_command")
            require(used < MAX_BYTES, "native_diagnostic_output_budget")
            result = await run_process(
                argv,
                timeout=min(timeout, remaining(deadline)),
                limit=min(limit, MAX_BYTES - used),
                merge_stderr=False,
            )
            output, stderr = result.get("output"), result.get("stderr")
            if not isinstance(output, str) or not isinstance(stderr, str):
                raise NativeError("native_diagnostic_invalid")
            charge(output.encode())
            charge(stderr.encode())
            remaining(deadline)
            return result

        async def query(
            argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
        ) -> dict[str, Any]:
            require(
                argv[:2] == ["busctl", "--user"] and not merge_stderr, "native_diagnostic_command"
            )
            facts["query_status"], facts["query_code"] = None, "not_started"
            try:
                result = await command(
                    [argv[0], "--system", *argv[2:]], timeout=timeout, limit=limit
                )
            except NativeError as error:
                if str(error) == "native_diagnostic_output_budget":
                    facts["query_code"] = "output_budget"
                raise
            status = result.get("exit_code")
            facts["query_status"] = (
                status if type(status) is int and -(2**31) <= status < 2**31 else None
            )
            facts["query_code"] = diagnostic_command_code(status, result.get("stderr"))
            return result

        async def read(pid: int, name: str) -> bytes | None:
            from muli.platforms.linux_transaction import _read_regular

            require(
                0 < pid < 2**31 and name in {"stat", "status", "cgroup", "exe", "wchan"},
                "native_diagnostic_command",
            )
            remaining(deadline)
            if name in {"exe", "wchan"}:
                result = await command(
                    [
                        "sudo",
                        "--non-interactive",
                        "--",
                        "readlink" if name == "exe" else "cat",
                        "--",
                        f"/proc/{pid}/{name}",
                    ],
                    timeout=CLEANUP_SECONDS,
                    limit=MAX_BYTES,
                )
                return result["output"].encode() if result.get("exit_code") == 0 else None
            require(used < MAX_BYTES, "native_diagnostic_output_budget")
            data = _read_regular(Path("/proc") / str(pid) / name, MAX_BYTES - used)
            charge(data)
            remaining(deadline)
            return data

        async with asyncio.timeout(remaining(deadline)):
            return await observe_setup_units(
                SystemdProbe(
                    runner=query,
                    limits=ProbeLimits(seconds=CLEANUP_SECONDS, command_bytes=MAX_BYTES),
                ),
                uid,
                facts,
                read,
                deadline,
                command,
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
        "harness_seconds": 90,
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
        self.note("manager_start")
        await self.command("systemctl", "--user", "daemon-reload")
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
    if len(sys.argv) == 6 and sys.argv[1] == "diagnose-setup":
        value = asyncio.run(
            diagnose_setup(sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]), float(sys.argv[5]))
        )
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    require(len(sys.argv) == 3 and sys.argv[1] in {"drive", "cleanup"}, "native_arguments")
    return asyncio.run(run(sys.argv[1], Path(sys.argv[2])))


if __name__ == "__main__":
    raise SystemExit(main())
