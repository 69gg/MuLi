"""GitHub-only native-systemd fixture; never run this harness on a workstation."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Protocol

MEMORY = 1024**3
HARNESS_MEMORY = MEMORY // 4
TASKS = 128
HARNESS_TASKS = 32
CASE_SECONDS = 60
CLEANUP_SECONDS = 5
HARNESS_SECONDS = 90
IMAGE_BYTES = 64 * 1024**2
OUTPUT_BYTES = 32768
ROOT_NAME = "muli-managed-native-ci"
VOLUME_PARENT = Path("/tmp")
CGROUP_ROOT = Path("/sys/fs/cgroup")
MANAGER_RUNTIME = Path("/run/systemd/system")
MANAGER_CONTROL_RUNTIME = Path("/run/systemd/system.control")
MANAGER_UNIT_ROOTS = (
    Path("/etc/systemd/system"),
    MANAGER_RUNTIME,
    Path("/etc/systemd/system.control"),
    MANAGER_CONTROL_RUNTIME,
)
ENVIRONMENT_NAME = "99-muli-native-environment.conf"


class FixtureError(ValueError):
    """Only fixed fixture errors are printed by the coordinator."""


@dataclass(frozen=True)
class Reply:
    code: int
    output: bytes = b""


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, seconds: float = 5) -> Reply: ...


def command(argv: Sequence[str], *, seconds: float = 5) -> Reply:
    """Bound both output and lifetime, and reap the one owned command group."""
    if not 0 < seconds <= HARNESS_SECONDS + CLEANUP_SECONDS:
        raise FixtureError("native_command_budget")
    end = time.monotonic() + seconds
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    output = bytearray()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise FixtureError("native_command_timeout")
                for key, _ in selector.select(remaining):
                    data = os.read(key.fd, min(4096, OUTPUT_BYTES + 1 - len(output)))
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(data)
                        if len(output) > OUTPUT_BYTES:
                            raise FixtureError("native_command_output_budget")
        return Reply(process.wait(timeout=max(0.001, end - time.monotonic())), bytes(output))
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.stdout.close()
        process.wait(timeout=CLEANUP_SECONDS)


def object_bytes(data: bytes) -> dict[str, Any]:
    if len(data) > OUTPUT_BYTES:
        raise FixtureError("native_metadata_budget")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise FixtureError("native_metadata_invalid")
    return value


COMMAND_CODES = frozenset(
    {
        "not_started",
        "ok",
        "command_failed",
        "timeout",
        "output_budget",
        "invalid_reply",
        "executable_permission",
        "executable_missing",
        "executable_lookup_failed",
        "bus_unavailable",
        "bus_permission",
        "permission_denied",
        "unsupported_argument",
        "property_rejected",
        "unit_unavailable",
    }
)
DIAGNOSTIC_STAGES = frozenset(
    {
        "not_started",
        "identity",
        "ancestors",
        "import",
        "manager",
        "listing",
        "get_unit",
        "unit_properties",
        "service_properties",
    }
)
DIAGNOSTIC_FAILURES = frozenset(
    {
        "not_started",
        "none",
        "identity_failed",
        "metadata_failed",
        "import_failed",
        "query_failed",
        "invalid_reply",
        "deadline",
        "observer_failed",
    }
)
SETUP_COMMANDS = frozenset({"daemon_reload", "manager_start", "manager_show"})
SETUP_PHASES = frozenset(
    {"initial"} | {f"{name}_{edge}" for name in SETUP_COMMANDS for edge in ("before", "after")}
)
PROCESS_CODES = frozenset(
    {
        "not_started",
        "observed",
        "pid_absent",
        "unavailable",
        "identity_changed",
        "output_budget",
        "timeout",
        "invalid_reply",
    }
)
PROCESS_STATES = frozenset("RSDZTtXxKWPI")


def command_code(status: int | None, output: bytes) -> str:
    """Classify bounded client output without exporting any part of its text."""
    if status is None:
        return "not_started"
    if status == 0:
        return "ok"
    body = output.lower()
    if b"failed to find executable" in body:
        if b"permission denied" in body:
            return "executable_permission"
        return "executable_missing" if b"no such file" in body else "executable_lookup_failed"
    if b"failed to connect to bus" in body or b"failed to connect to user" in body:
        return "bus_permission" if b"permission denied" in body else "bus_unavailable"
    if any(item in body for item in (b"unrecognized option", b"unknown option", b"invalid option")):
        return "unsupported_argument"
    if any(
        item in body
        for item in (b"unknown property", b"unknown assignment", b"failed to set unit properties")
    ):
        return "property_rejected"
    if any(
        item in body
        for item in (b"permission denied", b"access denied", b"interactive authentication required")
    ):
        return "permission_denied"
    if b"unit" in body and (b"not loaded" in body or b"not found" in body):
        return "unit_unavailable"
    return "command_failed"


def client_status(value: object) -> bool:
    return value is None or (type(value) is int and -(2**31) <= value < 2**31)


def command_error_code(error: Exception) -> str:
    """An unavailable exit status is not evidence that a command never ran."""
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) or (
        isinstance(error, FixtureError)
        and str(error) in {"native_diagnostic_deadline", "native_command_timeout"}
    ):
        return "timeout"
    if isinstance(error, FixtureError) and str(error) in {
        "native_command_output_budget",
        "native_diagnostic_output_budget",
    }:
        return "output_budget"
    return "command_failed"


MANAGER_FAILURE_CODES = frozenset(
    {
        "native_manager_observation",
        "native_manager_reply",
        "native_manager_conflict",
        "native_manager_missing",
        "native_manager_failed",
        "native_manager_changed",
        "native_manager_not_ready",
        "native_manager_process",
        "native_deadline_exceeded",
        "native_diagnostic_output_budget",
        "native_kernel_limit",
        "native_kernel_cpu",
        "native_kernel_controllers",
    }
)
MANAGER_STAGES = frozenset(
    {
        "list_units",
        "unit_path",
        "unit_properties",
        "service_properties",
        "unit_validation",
        "manager_validation",
        "process",
        "kernel",
    }
)


def manager_failure_facts(value: object) -> bool:
    """A failed observation is finite evidence, never readiness authority."""
    return (
        isinstance(value, dict)
        and set(value) == {"code", "stage", "query_status", "query_code", "used"}
        and isinstance(value["code"], str)
        and value["code"] in MANAGER_FAILURE_CODES
        and isinstance(value["stage"], str)
        and value["stage"] in MANAGER_STAGES
        and client_status(value["query_status"])
        and isinstance(value["query_code"], str)
        and value["query_code"] in COMMAND_CODES
        and type(value["used"]) is int
        and 0 <= value["used"] <= OUTPUT_BYTES - 1024
    )


def startup_facts(value: object, unit: str) -> bool:
    """Accept only the exact harness's finite, typed exit observation."""
    if (
        not isinstance(value, dict)
        or value.get("unit") != unit
        or re.fullmatch(r"muli-native-[a-f0-9]{32}\.service", unit) is None
        or value.get("code") not in ("observed", "unavailable", "unit_absent")
        or not client_status(value.get("client_status"))
        or not client_status(value.get("diagnostic_status"))
        or not client_status(value.get("query_status"))
        or any(
            not isinstance(value.get(key), str) or value[key] not in COMMAND_CODES
            for key in ("client_code", "diagnostic_code", "query_code")
        )
        or not isinstance(value.get("stage"), str)
        or value["stage"] not in DIAGNOSTIC_STAGES
        or not isinstance(value.get("failure"), str)
        or value["failure"] not in DIAGNOSTIC_FAILURES
    ):
        return False
    ancestors = value.get("ancestors")
    if not isinstance(ancestors, list) or len(ancestors) > 16:
        return False
    for index, item in enumerate(ancestors):
        if (
            not isinstance(item, dict)
            or set(item) != {"index", "mode", "uid", "gid"}
            or item["index"] != index
            or any(type(item[key]) is not int or not 0 <= item[key] < 2**32 for key in item)
            or item["mode"] > 0o177777
        ):
            return False
    base = {
        "unit",
        "code",
        "client_status",
        "client_code",
        "diagnostic_status",
        "diagnostic_code",
        "stage",
        "query_status",
        "query_code",
        "failure",
        "ancestors",
    }
    if value["code"] in {"unit_absent", "observed"} and (
        value["stage"] != ("listing" if value["code"] == "unit_absent" else "service_properties")
        or value["failure"] != "none"
        or value["query_status"] != 0
        or value["query_code"] != "ok"
    ):
        return False
    if value["code"] != "observed":
        return set(value) == base
    strings = {"load_state", "active_state", "sub_state", "result"}
    integers = {"exec_code", "exec_status", "pid"}
    return (
        set(value) == base | strings | integers
        and all(
            isinstance(value[key], str)
            and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value[key]) is not None
            for key in strings
        )
        and all(type(value[key]) is int and 0 <= value[key] < 2**31 for key in integers)
    )


def process_facts(value: object) -> bool:
    """Export a finite observation window, never raw proc fields or paths."""
    if (
        not isinstance(value, dict)
        or set(value) != {"code", "exe", "uid_matches", "state", "wait"}
        or not isinstance(value["code"], str)
        or value["code"] not in PROCESS_CODES
        or not isinstance(value["exe"], str)
        or value["exe"] not in {"manager", "executor", "other", "unavailable"}
        or not isinstance(value["state"], str)
        or not isinstance(value["wait"], str)
        or value["wait"] not in {"child_wait", "epoll_wait", "other", "unavailable"}
    ):
        return False
    if value["code"] == "observed":
        return (
            value["exe"] != "unavailable"
            and type(value["uid_matches"]) is bool
            and value["state"] in PROCESS_STATES
        )
    return (
        value["exe"] == "unavailable"
        and value["uid_matches"] is None
        and value["state"] == ""
        and value["wait"] == "unavailable"
    )


def journal_facts(value: object) -> bool:
    """A start-job fact is not a unit-state or READY acknowledgement."""
    if (
        not isinstance(value, dict)
        or set(value) != {"code", "incomplete", "events"}
        or not isinstance(value["code"], str)
        or value["code"] not in PROCESS_CODES | {"no_known_event", "command_failed"}
        or type(value["incomplete"]) is not bool
        or not isinstance(value["events"], list)
        or len(value["events"]) > 32
    ):
        return False
    if value["code"] not in {"observed", "no_known_event"}:
        return not value["incomplete"] and value["events"] == []
    if bool(value["events"]) != (value["code"] == "observed"):
        return False
    results = {
        "unknown",
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
    for event in value["events"]:
        if (
            not isinstance(event, dict)
            or set(event) != {"source", "event", "unit", "job_result"}
            or any(not isinstance(item, str) for item in event.values())
            or event["source"] not in {"system_manager", "user_manager"}
            or event["event"] not in {"starting", "started", "failed", "user_startup_finished"}
            or event["unit"] not in {"manager", "basic", "default", "dbus", "other"}
            or event["job_result"] not in results
            or (event["source"] == "system_manager" and event["unit"] != "manager")
        ):
            return False
        if event["event"] == "user_startup_finished":
            if (
                event["source"] != "user_manager"
                or event["unit"] != "manager"
                or event["job_result"] != "unknown"
            ):
                return False
        elif (
            (event["source"] == "user_manager" and event["unit"] == "manager")
            or (event["event"] == "starting" and event["job_result"] != "unknown")
            or (event["event"] == "started" and event["job_result"] != "done")
            or (event["event"] == "failed" and event["job_result"] in {"unknown", "done"})
        ):
            return False
    return True


def context_facts(value: object) -> bool:
    """Accept finite initial-environment/socket labels, never their source values."""
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
    return (
        isinstance(value, dict)
        and set(value) == set(allowed)
        and all(
            isinstance(value[key], str) and value[key] in choices
            for key, choices in allowed.items()
        )
        and (
            value["code"] == "observed"
            or all(value[key] == "unavailable" for key in allowed if key != "code")
        )
    )


def notification_facts(value: object) -> bool:
    """Accept fixed PID1 projections, never STATUS text or a READY receipt claim."""
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
        "status_text": {"empty", "reached_basic", "ready", "other", "unavailable"},
        "notify_access": {"none", "main", "exec", "all", "other", "unavailable"},
        "service_type": {"notify", "notify-reload", "other", "unavailable"},
        "result": {"success", "other", "unavailable"},
        "active_state_after": {
            "active",
            "activating",
            "deactivating",
            "inactive",
            "failed",
            "reloading",
            "maintenance",
            "other",
            "unavailable",
        },
        "sub_state_after": {
            "start",
            "start-post",
            "running",
            "exited",
            "dead",
            "failed",
            "other",
            "unavailable",
        },
    }
    return (
        isinstance(value, dict)
        and set(value) == set(allowed)
        and all(
            isinstance(value[key], str) and value[key] in choices
            for key, choices in allowed.items()
        )
        and all(
            (value[key] != "unavailable") == (value["code"] == "observed")
            for key in allowed
            if key != "code"
        )
    )


def setup_facts(value: object) -> bool:
    """Only the confirmed UID's two system units may enter setup evidence."""
    fields = {
        "uid",
        "phase",
        "client_code",
        "diagnostic_status",
        "diagnostic_code",
        "stage",
        "query_status",
        "query_code",
        "failure",
        "unit",
        "units",
        "process",
        "journal",
        "context",
        "notification",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["uid"]) is not int
        or not 0 < value["uid"] < 2**31
        or not isinstance(value["phase"], str)
        or value["phase"] not in SETUP_PHASES
        or not client_status(value["diagnostic_status"])
        or not client_status(value["query_status"])
        or any(
            not isinstance(value[key], str) or value[key] not in COMMAND_CODES
            for key in ("client_code", "diagnostic_code", "query_code")
        )
        or not isinstance(value["stage"], str)
        or value["stage"] not in DIAGNOSTIC_STAGES
        or not isinstance(value["failure"], str)
        or value["failure"] not in DIAGNOSTIC_FAILURES
        or not process_facts(value["process"])
        or not journal_facts(value["journal"])
        or not context_facts(value["context"])
        or not notification_facts(value["notification"])
        or (value["notification"]["code"] == "observed" and value["context"]["code"] != "observed")
    ):
        return False
    uid = value["uid"]
    names = (f"user-runtime-dir@{uid}.service", f"user@{uid}.service")
    if value["unit"] not in ("", *names) or not isinstance(value["units"], list):
        return False
    if len(value["units"]) > len(names):
        return False
    for name, item in zip(names, value["units"], strict=False):
        if not isinstance(item, dict) or item.get("unit") != name:
            return False
        if item.get("code") == "unit_absent":
            if set(item) != {"unit", "code"}:
                return False
            continue
        strings = {"load_state", "active_state", "sub_state", "result"}
        integers = {"exec_code", "exec_status", "pid", "job"}
        if (
            item.get("code") != "observed"
            or set(item) != {"unit", "code", "cgroup_present"} | strings | integers
            or type(item["cgroup_present"]) is not bool
            or any(
                not isinstance(item[key], str)
                or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item[key]) is None
                for key in strings
            )
            or any(type(item[key]) is not int or not 0 <= item[key] < 2**31 for key in integers)
        ):
            return False
    return True


def fixture_result(data: bytes) -> dict[str, Any]:
    """Export only finite fixture facts, never arbitrary worker JSON fields."""
    value = object_bytes(data)
    if (
        set(value) != {"completed", "stage", "code", "timeline", "generations", "resources"}
        or type(value["completed"]) is not bool
        or not isinstance(value["stage"], str)
        or re.fullmatch(r"[a-z_]{1,40}", value["stage"]) is None
        or not isinstance(value["code"], str)
        or re.fullmatch(r"[A-Za-z_]{0,64}", value["code"]) is None
    ):
        raise FixtureError("native_result_invalid")
    timeline = value["timeline"]
    integers = {"elapsed_ms", "pid", "exit_status"}
    strings = {
        "stage",
        "phase",
        "activation",
        "request_id",
        "generation",
        "helper",
        "state",
        "boot",
        "result",
    }
    if not isinstance(timeline, list) or len(timeline) > 64:
        raise FixtureError("native_result_invalid")
    for row in timeline:
        if (
            not isinstance(row, dict)
            or not {"stage", "elapsed_ms"}.issubset(row)
            or not set(row).issubset(integers | strings)
        ):
            raise FixtureError("native_result_invalid")
        for name, fact in row.items():
            if name in integers:
                valid = type(fact) is int and 0 <= fact <= 2**63 - 1
            else:
                valid = (
                    isinstance(fact, str)
                    and re.fullmatch(r"[A-Za-z0-9_.-]{0,100}", fact) is not None
                )
            if not valid:
                raise FixtureError("native_result_invalid")
    history = value["generations"]
    if not isinstance(history, list) or len(history) > 3:
        raise FixtureError("native_result_invalid")
    for row in history:
        if (
            not isinstance(row, dict)
            or set(row) != {"deployment", "generation", "helper"}
            or any(
                not isinstance(row[name], str) or re.fullmatch(pattern, row[name]) is None
                for name, pattern in (
                    ("deployment", r"[a-f0-9]{64}"),
                    ("generation", r"[a-f0-9]{32}"),
                    ("helper", r"muli-transaction-[a-f0-9]{64}\.service"),
                )
            )
        ):
            raise FixtureError("native_result_invalid")
    resources = value["resources"]
    if not isinstance(resources, dict) or not set(resources).issubset(
        {
            "manager",
            "harness",
            "manager_peak",
            "initial",
            "cleanup",
            "startup",
            "environment",
            "setup",
            "manager_readiness",
            "manager_failure",
        }
    ):
        raise FixtureError("native_result_invalid")
    for name, facts in resources.items():
        if name in {"manager", "harness"}:
            valid = (
                isinstance(facts, dict)
                and set(facts) == {"memory.max", "memory.swap.max", "cpu.max", "pids.max"}
                and all(
                    isinstance(item, str)
                    and re.fullmatch(r"[0-9]{1,20}( [0-9]{1,20})?", item) is not None
                    for item in facts.values()
                )
            )
        elif name == "initial":
            valid = (
                isinstance(facts, dict)
                and set(facts) == {"store", "main", "device", "inode"}
                and all(
                    isinstance(facts[key], str)
                    and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", facts[key]) is not None
                    for key in ("store", "main")
                )
                and all(type(facts[key]) is int and facts[key] >= 0 for key in ("device", "inode"))
            )
        elif name == "startup":
            valid = isinstance(facts, dict) and isinstance(facts.get("unit"), str)
            valid = valid and startup_facts(facts, facts["unit"])
        elif name == "setup":
            valid = setup_facts(facts)
        elif name == "manager_failure":
            valid = manager_failure_facts(facts)
        elif name == "manager_readiness":
            valid = (
                isinstance(facts, dict)
                and set(facts) == {"elapsed_ms", "late_ready"}
                and type(facts["elapsed_ms"]) is int
                and 0 <= facts["elapsed_ms"] <= (HARNESS_SECONDS + CLEANUP_SECONDS) * 1000
                and type(facts["late_ready"]) is bool
            )
        elif name == "environment":
            names = {
                "uid_matches",
                "home_matches",
                "config_matches",
                "runtime_matches",
                "bus_matches",
            }
            valid = (
                isinstance(facts, dict)
                and set(facts) == names | {"config_source"}
                and isinstance(facts["config_source"], str)
                and facts["config_source"] in {"explicit", "default"}
                and all(type(facts[key]) is bool for key in names)
            )
        elif name == "manager_peak":
            valid = isinstance(facts, str) and re.fullmatch(r"[0-9]{1,20}", facts) is not None
        else:
            valid = facts in ("verified", "failed")
        if not valid:
            raise FixtureError("native_result_invalid")
    return value


def write_json(path: Path, value: object) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > OUTPUT_BYTES:
        raise FixtureError("native_metadata_budget")
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as output:
        os.chmod(temporary, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_kernel(path: Path, memory: int, tasks: int) -> dict[str, str]:
    """The same four kernel limits used by the local resource guard."""
    observed: dict[str, str] = {}
    for name, maximum in (("memory.max", memory), ("memory.swap.max", 0), ("pids.max", tasks)):
        value = (path / name).read_text().strip()
        if not value.isdecimal() or int(value) > maximum or (maximum and int(value) == 0):
            raise FixtureError("native_kernel_limit")
        observed[name] = value
    values = (path / "cpu.max").read_text().split()
    if len(values) != 2 or not all(value.isdecimal() for value in values):
        raise FixtureError("native_kernel_cpu")
    quota, period = map(int, values)
    if not 0 < quota <= period:
        raise FixtureError("native_kernel_cpu")
    observed["cpu.max"] = " ".join(values)
    return observed


def require_ci(root: Path) -> None:
    if (
        sys.platform != "linux"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_OS") != "Linux"
        or not os.environ.get("RUNNER_TEMP")
        or root.name != ROOT_NAME
        or root.parent != Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        or not root.is_absolute()
        or any(character in str(root) for character in "\r\n\x00")
    ):
        raise FixtureError("native_hosted_ci_required")


def temporary_identity() -> tuple[int, int]:
    """Only the existing root-owned sticky directory may host the private volume."""
    info = VOLUME_PARENT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o1777:
        raise FixtureError("native_temporary_parent_invalid")
    return info.st_dev, info.st_ino


def account(name: str) -> tuple[int, int, str] | None:
    import pwd

    try:
        value = pwd.getpwnam(name)
    except KeyError:
        return None
    return value.pw_uid, value.pw_gid, value.pw_dir


def private_group(name: str) -> tuple[int, tuple[str, ...]] | None:
    import grp

    try:
        value = grp.getgrnam(name)
    except KeyError:
        return None
    return value.gr_gid, tuple(value.gr_mem)


@dataclass
class Receipt:
    user: str
    uid: int | None = None
    gid: int | None = None
    user_created: bool = False
    user_removed: bool = False
    group_removed: bool = False
    setup_phase: str = "initial"
    manager_group: str = ""
    image: tuple[int, int] | None = None
    temporary: tuple[int, int] | None = None
    volume_directory: tuple[int, int] | None = None
    environment_directory: tuple[int, int] | None = None
    environment_file: tuple[int, int] | None = None
    manager_program: str = ""
    bootstrap_directories: list[list[int]] = field(default_factory=list)
    bootstrap_file: list[int] | None = None
    mounts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def read(cls, path: Path) -> Receipt:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise FixtureError("native_receipt_owner")
        with path.open("rb") as source:
            value = object_bytes(source.read(OUTPUT_BYTES + 1))
        if set(value) != set(cls.__dataclass_fields__):
            raise FixtureError("native_receipt_invalid")
        result = cls(**value)
        if not re.fullmatch(r"muli-ci-[a-f0-9]{12}", result.user):
            raise FixtureError("native_receipt_invalid")
        if any(
            type(getattr(result, name)) is not bool
            for name in ("user_created", "user_removed", "group_removed")
        ):
            raise FixtureError("native_receipt_invalid")
        if not isinstance(result.setup_phase, str) or result.setup_phase not in SETUP_PHASES:
            raise FixtureError("native_receipt_invalid")
        if any(
            value is not None and (type(value) is not int or value <= 0)
            for value in (result.uid, result.gid)
        ):
            raise FixtureError("native_receipt_invalid")
        if not isinstance(result.manager_group, str) or (
            result.manager_group
            and (
                not result.manager_group.startswith("/")
                or ".." in Path(result.manager_group).parts
                or Path(result.manager_group).name != f"user@{result.uid}.service"
            )
        ):
            raise FixtureError("native_receipt_invalid")
        for name in (
            "image",
            "temporary",
            "volume_directory",
            "environment_directory",
            "environment_file",
        ):
            identity = getattr(result, name)
            if identity is None:
                continue
            if (
                not isinstance(identity, (list, tuple))
                or len(identity) != 2
                or any(type(item) is not int or item < 0 for item in identity)
            ):
                raise FixtureError("native_receipt_invalid")
            setattr(result, name, (identity[0], identity[1]))
        if result.volume_directory is not None and result.temporary is None:
            raise FixtureError("native_receipt_invalid")
        if result.environment_file is not None and result.environment_directory is None:
            raise FixtureError("native_receipt_invalid")
        if result.environment_directory is not None and not result.user_created:
            raise FixtureError("native_receipt_invalid")
        if (
            not isinstance(result.manager_program, str)
            or (
                result.manager_program
                and (
                    not Path(result.manager_program).is_absolute()
                    or re.fullmatch(r"/[A-Za-z0-9_./-]{1,255}", result.manager_program) is None
                    or ".." in Path(result.manager_program).parts
                    or Path(result.manager_program).name != "systemd"
                )
            )
            or not isinstance(result.bootstrap_directories, list)
            or (result.bootstrap_directories and not 2 <= len(result.bootstrap_directories) <= 5)
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or any(type(part) is not int or not 0 <= part < 2**64 for part in item)
                for item in result.bootstrap_directories
            )
            or (
                result.bootstrap_file is not None
                and (
                    len(result.bootstrap_directories) != 5
                    or not isinstance(result.bootstrap_file, list)
                    or len(result.bootstrap_file) != 2
                    or any(
                        type(part) is not int or not 0 <= part < 2**64
                        for part in result.bootstrap_file
                    )
                )
            )
            or (result.bootstrap_directories and not result.user_created)
            or (result.environment_file is not None and not result.manager_program)
        ):
            raise FixtureError("native_receipt_invalid")
        if (
            not isinstance(result.mounts, list)
            or len(result.mounts) > 3
            or any(
                not isinstance(item, dict)
                or set(item) != {"target", "source", "fstype", "options", "id", "device", "inode"}
                or any(
                    not isinstance(item[key], str)
                    for key in ("target", "source", "fstype", "options")
                )
                or any(type(item[key]) is not int for key in ("id", "device", "inode"))
                for item in result.mounts
            )
        ):
            raise FixtureError("native_receipt_invalid")
        return result


class NativeCI:
    def __init__(self, root: Path, *, run: Runner = command) -> None:
        self.root, self.run = root, run
        self.receipt_path = root / "receipt.json"
        self.receipt: Receipt | None = None
        self.cleanup_deadline: float | None = None
        self.execute_deadline: float | None = None
        self.manager_limits: dict[str, str] = {}
        self.setup_bytes = 0
        self.manager_readiness: dict[str, int | bool] = {}
        self.manager_failure: dict[str, Any] = {}

    @property
    def volume(self) -> Path:
        if self.receipt is None:
            raise FixtureError("native_receipt_required")
        return VOLUME_PARENT / f"{ROOT_NAME}-{self.receipt.user}"

    @property
    def home(self) -> Path:
        return self.volume / "home"

    def check_temporary(self) -> None:
        assert self.receipt is not None
        if self.receipt.temporary != temporary_identity():
            raise FixtureError("native_temporary_parent_changed")

    def check_volume_directory(self) -> None:
        """Check the original, unmounted entry, never adopt a name collision."""
        assert self.receipt is not None
        self.check_temporary()
        info = self.volume.lstat()
        if (
            self.receipt.volume_directory != (info.st_dev, info.st_ino)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise FixtureError("native_volume_directory_changed")

    def call(self, argv: Sequence[str], *, seconds: float = 5) -> Reply:
        deadline = (
            self.cleanup_deadline if self.cleanup_deadline is not None else self.execute_deadline
        )
        code = (
            "native_cleanup_deadline"
            if self.cleanup_deadline is not None
            else "native_execute_deadline"
        )
        if deadline is not None:
            seconds = min(seconds, deadline - time.monotonic())
            if seconds <= 0:
                raise FixtureError(code)
        reply = self.run(argv, seconds=seconds)
        if deadline is not None and time.monotonic() >= deadline:
            raise FixtureError(code)
        return reply

    def execute_remaining(self) -> float:
        """Use one same-host monotonic deadline, never a fresh phase allowance."""
        if self.execute_deadline is None:
            raise FixtureError("native_execute_deadline")
        value = self.execute_deadline - time.monotonic()
        if value <= 0:
            raise FixtureError("native_execute_deadline")
        return value

    def checked(self, argv: Sequence[str], *, seconds: float = 5) -> bytes:
        reply = self.call(argv, seconds=seconds)
        if reply.code:
            raise FixtureError("native_command_failed")
        return reply.output

    def sudo(self, *argv: str, seconds: float = 5) -> bytes:
        return self.checked(("sudo", "--non-interactive", *argv), seconds=seconds)

    def save(self) -> None:
        assert self.receipt is not None
        if self.execute_deadline is not None and self.cleanup_deadline is None:
            self.execute_remaining()
        write_json(self.receipt_path, asdict(self.receipt))
        if self.execute_deadline is not None and self.cleanup_deadline is None:
            self.execute_remaining()

    def setup_command(self, phase: str, *argv: str) -> bytes:
        """Persist the exact original command's boundary without changing its budget."""
        assert self.receipt is not None
        if phase not in SETUP_COMMANDS:
            raise FixtureError("native_setup_phase")
        self.receipt.setup_phase = f"{phase}_before"
        self.save()
        output = self.sudo(*argv)
        self.receipt.setup_phase = f"{phase}_after"
        self.save()
        return output

    def observe_manager(
        self, mode: str, *, started: float = 0.0, previous: str = ""
    ) -> dict[str, Any]:
        """Only the coordinator starts units; this existing runner worker is read-only."""
        assert self.receipt is not None
        uid, _ = self.current_account()
        end = (
            self.execute_deadline - CLEANUP_SECONDS
            if self.execute_deadline is not None
            else time.monotonic() + CLEANUP_SECONDS
        )
        # Cancel the observer before the unchanged transport deadline. This
        # fixed share of the existing budget is only for a failure receipt.
        observation_end = end - CLEANUP_SECONDS / 2
        allowance = OUTPUT_BYTES - self.setup_bytes
        # Reserve the small returned JSON as well as charging all inner stdout/stderr.
        if allowance <= 1024 or observation_end <= time.monotonic():
            raise FixtureError("native_manager_observation_budget")
        worker = Path(__file__).resolve().parents[1] / "tests/helpers/managed_native_worker.py"
        arguments: tuple[str, ...] = (
            sys.executable,
            "-I",
            str(worker),
            mode,
            self.receipt.user,
            str(uid),
            str(self.home),
            str(observation_end),
            str(allowance - 1024),
        )
        if mode == "manager-ready":
            arguments += (str(started), previous)
        try:
            reply = self.call(arguments, seconds=end - time.monotonic())
            if time.monotonic() >= end:
                raise FixtureError("native_command_timeout")
            observed = object_bytes(reply.output)
            used = observed.get("used")
            if (
                type(used) is not int
                or not 0 <= used <= allowance - 1024
                or len(reply.output) > 1024
                or reply.code != 0
            ):
                raise FixtureError("native_manager_observation")
        except (ValueError, OSError, subprocess.SubprocessError):
            self.setup_bytes = OUTPUT_BYTES  # No return means no reusable unspent allowance.
            raise
        self.setup_bytes += used + len(reply.output)
        if self.setup_bytes > OUTPUT_BYTES:
            raise FixtureError("native_diagnostic_output_budget")
        if observed.get("code") != ("idle" if mode == "manager-idle" else "ready"):
            if manager_failure_facts(observed):
                if time.monotonic() >= end:
                    self.setup_bytes = OUTPUT_BYTES
                    raise FixtureError("native_command_timeout")
                self.manager_failure = observed.copy()
                try:
                    self.save_setup_result({})
                except (ValueError, OSError):
                    pass  # Preserve the original observation error if evidence cannot be written.
                raise FixtureError(observed["code"])
            self.setup_bytes = OUTPUT_BYTES
            raise FixtureError("native_manager_observation")
        expected = {"code", "used", "invocation"}
        if mode == "manager-ready":
            expected |= {"group", "pid", "elapsed_ms", "late_ready"}
        invocation = observed.get("invocation")
        if (
            set(observed) != expected
            or not isinstance(invocation, str)
            or (invocation and re.fullmatch(r"[a-f0-9]{32}", invocation) is None)
        ):
            raise FixtureError("native_manager_observation")
        if mode == "manager-ready":
            group = observed["group"]
            if (
                not invocation
                or invocation == "0" * 32
                or invocation == previous
                or type(observed["pid"]) is not int
                or not 0 < observed["pid"] < 2**31
                or type(observed["elapsed_ms"]) is not int
                or not 0 <= observed["elapsed_ms"] <= (HARNESS_SECONDS + CLEANUP_SECONDS) * 1000
                or type(observed["late_ready"]) is not bool
                or not isinstance(group, str)
                or not group.startswith("/")
                or ".." in Path(group).parts
                or Path(group).name != f"user@{uid}.service"
            ):
                raise FixtureError("native_manager_observation")
        if time.monotonic() >= observation_end:
            raise FixtureError("native_execute_deadline")
        return observed

    def current_account(self) -> tuple[int, int]:
        receipt = self.receipt
        assert receipt is not None
        actual = account(receipt.user)
        if (
            not receipt.user_created
            or receipt.user_removed
            or actual != (receipt.uid, receipt.gid, str(self.home))
            or receipt.uid is None
            or receipt.gid is None
        ):
            raise FixtureError("native_account_changed")
        return receipt.uid, receipt.gid

    def environment_directory(self) -> Path:
        assert self.receipt is not None and self.receipt.uid is not None
        return MANAGER_RUNTIME / f"user@{self.receipt.uid}.service.d"

    def environment_data(self) -> bytes:
        assert self.receipt is not None and self.receipt.uid is not None
        uid = self.receipt.uid
        values = (
            f"HOME={self.home}",
            f"XDG_CONFIG_HOME={self.home / '.config'}",
            f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
        )
        resources = (
            f"MemoryMax={MEMORY}",
            "MemorySwapMax=0",
            "CPUQuota=100%",
            f"TasksMax={TASKS}",
        )
        program = self.receipt.manager_program
        if not program or re.fullmatch(r"/[A-Za-z0-9_./-]{1,255}", program) is None:
            raise FixtureError("native_manager_template")
        data = (
            "[Service]\n"
            + "\n".join((*resources, *(f'Environment="{value}"' for value in values)))
            + f"\nExecStart=\nExecStart={program} --user --unit={self.receipt.user}.target\n"
        ).encode()
        if len(data) > OUTPUT_BYTES:
            raise FixtureError("native_diagnostic_output_budget")
        return data

    def bootstrap_reply(self, argv: Sequence[str]) -> dict[str, Any]:
        """Charge the helper and its reply against the original setup allowance."""
        allowance = OUTPUT_BYTES - self.setup_bytes
        if allowance <= 1024:
            raise FixtureError("native_diagnostic_output_budget")
        deadline = (
            self.cleanup_deadline if self.cleanup_deadline is not None else self.execute_deadline
        )
        end = min(
            deadline or (time.monotonic() + CLEANUP_SECONDS), time.monotonic() + CLEANUP_SECONDS
        )
        try:
            reply = self.call(
                (*argv, str(end), str(allowance - 1024)), seconds=end - time.monotonic()
            )
            if time.monotonic() >= end:
                raise FixtureError("native_bootstrap_deadline")
            if reply.code:
                raise FixtureError("native_bootstrap_invalid")
            observed = object_bytes(reply.output)
            used = observed.get("used")
            if type(used) is not int or not 0 <= used <= allowance - 1024:
                raise FixtureError("native_bootstrap_invalid")
            self.setup_bytes += used + len(reply.output)
            if self.setup_bytes > OUTPUT_BYTES:
                raise FixtureError("native_diagnostic_output_budget")
            if observed.get("code") != "verified":
                raise FixtureError("native_bootstrap_unconfirmed")
            return observed
        except (ValueError, OSError, subprocess.SubprocessError):
            self.setup_bytes = OUTPUT_BYTES
            raise

    def bootstrap_files(self, mode: str) -> None:
        assert self.receipt is not None
        receipt = self.receipt
        uid, gid = self.current_account()
        if mode not in {"create", "check", "remove"} or not receipt.bootstrap_directories:
            raise FixtureError("native_bootstrap_invalid")
        worker = Path(__file__).resolve().parents[1] / "tests/helpers/managed_native_worker.py"
        manifest = json.dumps(
            {"directories": receipt.bootstrap_directories, "file": receipt.bootstrap_file},
            separators=(",", ":"),
        )
        observed = self.bootstrap_reply(
            (
                "sudo",
                "--non-interactive",
                "--",
                sys.executable,
                "-I",
                str(worker),
                "bootstrap-files",
                mode,
                receipt.user,
                str(uid),
                str(gid),
                manifest,
            )
        )
        if set(observed) != {"code", "directories", "file", "used"}:
            raise FixtureError("native_bootstrap_invalid")
        directories, identity = observed["directories"], observed["file"]

        def pair(item: object) -> bool:
            return (
                isinstance(item, list)
                and len(item) == 2
                and all(type(part) is int and 0 <= part < 2**64 for part in item)
            )

        if (
            not isinstance(directories, list)
            or not 2 <= len(directories) <= 5
            or not all(pair(item) for item in directories)
            or directories[:2] != receipt.bootstrap_directories[:2]
            or (identity is not None and (len(directories) != 5 or not pair(identity)))
        ):
            raise FixtureError("native_bootstrap_invalid")
        if mode == "check" and (
            directories != receipt.bootstrap_directories
            or identity != receipt.bootstrap_file
            or identity is None
        ):
            raise FixtureError("native_bootstrap_changed")
        if mode == "create" and (len(directories) != 5 or identity is None):
            raise FixtureError("native_bootstrap_invalid")
        if mode == "remove" and (
            identity is not None or directories != receipt.bootstrap_directories[: len(directories)]
        ):
            raise FixtureError("native_bootstrap_invalid")
        receipt.bootstrap_directories = (
            [] if mode == "remove" and len(directories) == 2 else directories
        )
        receipt.bootstrap_file = identity
        if self.current_account() != (uid, gid):
            raise FixtureError("native_bootstrap_identity")
        self.save()
        if mode == "remove" and receipt.bootstrap_directories:
            raise FixtureError("native_bootstrap_not_empty")

    def check_template(self) -> None:
        uid, _ = self.current_account()
        assert self.receipt is not None
        worker = Path(__file__).resolve().parents[1] / "tests/helpers/managed_native_worker.py"
        observed = self.bootstrap_reply(
            (
                sys.executable,
                "-I",
                str(worker),
                "manager-template",
                self.receipt.user,
                str(uid),
                str(self.home),
            )
        )
        program = observed.get("manager")
        if (
            set(observed) != {"code", "manager", "used"}
            or not isinstance(program, str)
            or re.fullmatch(r"/[A-Za-z0-9_./-]{1,255}", program) is None
            or ".." in Path(program).parts
            or Path(program).name != "systemd"
        ):
            raise FixtureError("native_manager_template")
        self.receipt.manager_program = program
        self.save()

    def check_manager_environment(self) -> bool:
        """Only the receipt's exclusive root-owned instance drop-in is ours."""
        assert self.receipt is not None
        receipt = self.receipt
        directory = self.environment_directory()
        if any(
            os.path.lexists(root / directory.name.removesuffix(".d"))
            or (root != MANAGER_RUNTIME and os.path.lexists(root / directory.name))
            for root in MANAGER_UNIT_ROOTS
        ):
            raise FixtureError("native_manager_environment_changed")
        if receipt.environment_directory is None:
            if os.path.lexists(directory):
                raise FixtureError("native_manager_environment_unconfirmed")
            return False
        try:
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o755
                or receipt.environment_directory != (info.st_dev, info.st_ino)
                or any(entry.name != ENVIRONMENT_NAME for entry in islice(directory.iterdir(), 2))
            ):
                raise FixtureError("native_manager_environment_changed")
            path = directory / ENVIRONMENT_NAME
            if receipt.environment_file is None:
                if os.path.lexists(path):
                    raise FixtureError("native_manager_environment_unconfirmed")
                return False
            info = path.lstat()
            expected = self.environment_data()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o644
                or receipt.environment_file != (info.st_dev, info.st_ino)
                or info.st_size != len(expected)
            ):
                raise FixtureError("native_manager_environment_changed")
            with path.open("rb") as source:
                data = source.read(len(expected) + 1)
                self.setup_bytes += len(data)
                if self.setup_bytes + 1024 > OUTPUT_BYTES:
                    raise FixtureError("native_diagnostic_output_budget")
                if data != expected:
                    raise FixtureError("native_manager_environment_changed")
        except FileNotFoundError:
            raise FixtureError("native_manager_environment_changed") from None
        return True

    def prepare_manager_environment(self) -> None:
        """Configure only this new UID before its manager builds user unit paths."""
        uid, _ = self.current_account()
        assert self.receipt is not None
        directory = self.environment_directory()
        parent = MANAGER_RUNTIME.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
            raise FixtureError("native_manager_environment_parent")
        if any(
            os.path.lexists(root / f"user@{uid}.service{suffix}")
            for root in MANAGER_UNIT_ROOTS
            for suffix in ("", ".d")
        ):
            raise FixtureError("native_manager_environment_exists")
        self.check_template()
        self.sudo("mkdir", "--mode=0755", "--", str(directory))
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o755
        ):
            raise FixtureError("native_manager_environment_changed")
        self.receipt.environment_directory = (info.st_dev, info.st_ino)
        self.save()
        source = self.root / "manager-environment.conf"
        data = self.environment_data()
        self.setup_bytes += len(data)
        if self.setup_bytes + 1024 > OUTPUT_BYTES:
            raise FixtureError("native_diagnostic_output_budget")
        with source.open("xb") as output:
            os.chmod(source, 0o600)
            output.write(data)
        path = directory / ENVIRONMENT_NAME
        self.sudo("install", "--mode=0644", "--", str(source), str(path))
        info = path.lstat()
        self.receipt.environment_file = (info.st_dev, info.st_ino)
        self.check_manager_environment()
        self.save()
        self.setup_command("daemon_reload", "systemctl", "daemon-reload")

    def user_arguments(self, *argv: str) -> tuple[str, ...]:
        uid, _ = self.current_account()
        assert self.receipt is not None
        return (
            "sudo",
            "--non-interactive",
            "--user",
            self.receipt.user,
            "--",
            "env",
            "-i",
            f"HOME={self.home}",
            f"XDG_CONFIG_HOME={self.home / '.config'}",
            f"USER={self.receipt.user}",
            f"LOGNAME={self.receipt.user}",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            *argv,
        )

    def as_user(self, *argv: str, seconds: float = 5) -> bytes:
        return self.checked(self.user_arguments(*argv), seconds=seconds)

    def mount(self, path: Path) -> dict[str, Any] | None:
        result = self.call(
            (
                "findmnt",
                "--json",
                "--mountpoint",
                str(path),
                "--output",
                "TARGET,SOURCE,FSTYPE,OPTIONS,ID",
            )
        )
        if result.code == 1:
            return None
        if result.code:
            raise FixtureError("native_mount_observation")
        rows = object_bytes(result.output).get("filesystems")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise FixtureError("native_mount_observation")
        value = rows[0]
        if set(value) != {"target", "source", "fstype", "options", "id"} or value["target"] != str(
            path
        ):
            raise FixtureError("native_mount_observation")
        # The handed-off 0700 volume is intentionally not traversable by the
        # runner. Only this fixed target is observed with the bounded root command.
        identity = self.sudo("stat", "--format=%d:%i:%f", "--", str(path)).decode().strip()
        if re.fullmatch(r"[0-9]+:[0-9]+:[a-f0-9]+", identity) is None:
            raise FixtureError("native_mount_observation")
        device, inode, mode = identity.split(":")
        if not stat.S_ISDIR(int(mode, 16)):
            raise FixtureError("native_mount_observation")
        return {**value, "device": int(device), "inode": int(inode)}

    def record_mount(self, path: Path, *, readonly: bool = False) -> None:
        assert self.receipt is not None
        value = self.mount(path)
        if value is None or (readonly and "ro" not in str(value["options"]).split(",")):
            raise FixtureError("native_mount_not_ready")
        self.receipt.mounts.append(value)
        self.save()

    def prepare(self) -> dict[str, Any]:
        if self.root.exists():
            raise FixtureError("native_root_exists")
        self.root.mkdir(mode=0o711)
        os.chmod(self.root, 0o711)
        self.receipt = Receipt("muli-ci-" + uuid.uuid4().hex[:12])
        self.save()  # Intent alone never authorizes deleting an existing account.
        if account(self.receipt.user) is not None:
            raise FixtureError("native_user_exists")
        worker = Path(__file__).resolve().parents[1] / "tests/helpers/managed_native_worker.py"
        description = object_bytes(self.checked((sys.executable, "-I", str(worker), "describe")))
        runtime, dependencies = (
            Path(description[key]).resolve(strict=True) for key in ("runtime", "dependencies")
        )
        for path in (runtime, dependencies):
            if any(
                output.is_relative_to(path) or path.is_relative_to(output)
                for output in (self.root, self.volume)
            ):
                raise FixtureError("native_recursive_input")
        if runtime.is_relative_to(dependencies) or dependencies.is_relative_to(runtime):
            raise FixtureError("native_recursive_input")
        self.receipt.temporary = temporary_identity()
        self.save()
        self.volume.mkdir(mode=0o700)
        info = self.volume.lstat()
        self.receipt.volume_directory = (info.st_dev, info.st_ino)
        self.check_volume_directory()
        self.save()
        image = self.root / "filesystem.ext4"
        with image.open("xb") as output:
            os.chmod(image, 0o600)
            output.truncate(IMAGE_BYTES)
        info = image.lstat()
        self.receipt.image = (info.st_dev, info.st_ino)
        self.save()
        self.checked(("mkfs.ext4", "-q", "-F", str(image)))
        self.check_volume_directory()
        self.sudo(
            "mount", "-t", "ext4", "-o", "loop,nodev,nosuid,noatime", str(image), str(self.volume)
        )
        self.record_mount(self.volume)
        # A freshly mounted ext4 root belongs to root. Only this new volume is
        # made writable for runner's preparation, before handing it to the UID.
        self.sudo("chown", f"{os.getuid()}:{os.getgid()}", str(self.volume))
        self.sudo("chmod", "0700", str(self.volume))
        for path in (self.home, self.volume / "input/python", self.volume / "input/deps"):
            path.mkdir(mode=0o700, parents=True)
        limits = description["limits"]
        if not isinstance(limits, dict) or type(description.get("size")) is not int:
            raise FixtureError("native_source_invalid")
        free = shutil.disk_usage(self.volume).free
        if free < 2 * description["size"] + 16 * 1024**2:
            raise FixtureError("native_filesystem_capacity")
        for name in ("a", "b"):
            self.copy_slot(name, description)
        shutil.copyfile(worker, self.volume / "managed_native_worker.py")
        (self.volume / "_muli_native_ci_hook.py").write_text(
            "import sys\nif sys.argv[-1:] == ['serve']:\n"
            "    from managed_native_worker import install_hook\n    install_hook()\n",
            encoding="utf-8",
        )
        self.sudo(
            "useradd",
            "--system",
            "--user-group",
            "--no-create-home",
            "--home-dir",
            str(self.home),
            "--shell",
            "/usr/sbin/nologin",
            self.receipt.user,
        )
        actual = account(self.receipt.user)
        if (
            actual is None
            or actual[2] != str(self.home)
            or actual[0] == 0
            or private_group(self.receipt.user) != (actual[1], ())
        ):
            raise FixtureError("native_account_creation")
        self.receipt.uid, self.receipt.gid, _ = actual
        self.receipt.user_created = True
        self.save()
        write_json(
            self.volume / "fixture.json",
            {
                "uid": actual[0],
                "memory": MEMORY,
                "tasks": TASKS,
                "harness_memory": HARNESS_MEMORY,
                "harness_tasks": HARNESS_TASKS,
                "seconds": CASE_SECONDS,
                "cleanup_seconds": CLEANUP_SECONDS,
                "harness_seconds": HARNESS_SECONDS,
                "http_bytes": description["http_bytes"],
                "package_digest": description["digest"],
                "harness_unit": "muli-native-" + uuid.uuid4().hex + ".service",
            },
        )
        # No mounted inputs exist yet: this cannot traverse or chown source trees.
        self.receipt.bootstrap_directories = [
            [info.st_dev, info.st_ino] for info in (self.volume.lstat(), self.home.lstat())
        ]
        self.save()
        self.sudo("chown", "--no-dereference", "-R", f"{actual[0]}:{actual[1]}", str(self.volume))
        for source, target in (
            (runtime, self.volume / "input/python"),
            (dependencies, self.volume / "input/deps"),
        ):
            self.sudo("mount", "--bind", str(source), str(target))
            self.record_mount(target)
            self.sudo("mount", "-o", "remount,bind,ro,nodev,nosuid", str(target))
            current = self.mount(target)
            if (
                current is None
                or not self.same_mount(current, self.receipt.mounts[-1])
                or "ro" not in current["options"].split(",")
            ):
                raise FixtureError("native_mount_not_readonly")
        self.bootstrap_files("create")
        self.prepare_manager_environment()
        self.bootstrap_files("check")
        initial = self.observe_manager("manager-idle")
        started = time.monotonic()
        self.setup_command(
            "manager_start",
            "systemctl",
            "--system",
            "--no-block",
            "start",
            f"user-runtime-dir@{actual[0]}.service",
            f"user@{actual[0]}.service",
        )
        self.receipt.setup_phase = "manager_show_before"
        self.save()
        ready = self.observe_manager(
            "manager-ready", started=started, previous=initial["invocation"]
        )
        group = ready["group"]
        self.manager_readiness = {key: ready[key] for key in ("elapsed_ms", "late_ready")}
        self.receipt.manager_group = group
        self.receipt.setup_phase = "manager_show_after"
        self.save()
        self.manager_limits = verify_kernel(CGROUP_ROOT / group.lstrip("/"), MEMORY, TASKS)
        self.bootstrap_files("check")
        if self.execute_deadline is not None:
            self.execute_remaining()
        return description

    def copy_slot(self, name: str, description: dict[str, Any]) -> None:
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        prefix = self.volume / name
        site = prefix / "lib" / version / "site-packages"
        site.mkdir(parents=True, mode=0o700)
        (prefix / "bin").mkdir(mode=0o700)
        base = Path(description["executable"]).relative_to(description["runtime"])
        (prefix / "bin/python").symlink_to(self.volume / "input/python" / base)
        (prefix / "pyvenv.cfg").write_text(
            f"home = {self.volume / 'input/python' / base.parent}\ninclude-system-site-packages = false\n",
            encoding="utf-8",
        )
        shutil.copytree(
            description["package"],
            site / "muli",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        (site / "fixture.pth").write_text(
            "import sys; sys.dont_write_bytecode = True\n"
            f"{self.volume / 'input/deps'}\n{self.volume}\nimport _muli_native_ci_hook\n",
            encoding="utf-8",
        )

    def transient(self, fixture: dict[str, Any], mode: str, seconds: float) -> tuple[str, ...]:
        unit = fixture["harness_unit"] if mode == "drive" else "muli-native-cleanup.service"
        cleanup: tuple[str, ...] = ()
        if mode == "cleanup":
            if self.cleanup_deadline is None:
                raise FixtureError("native_cleanup_deadline")
            cleanup = (f"--setenv=MULI_NATIVE_CLEANUP_DEADLINE={self.cleanup_deadline}",)
        elif self.execute_deadline is not None:
            seconds = min(seconds, self.execute_remaining() - CLEANUP_SECONDS)
            if seconds <= CASE_SECONDS:
                raise FixtureError("native_case_start_budget")
            cleanup = (f"--setenv=MULI_NATIVE_EXECUTE_DEADLINE={self.execute_deadline}",)
        return (
            "systemd-run",
            "--user",
            "--wait",
            "--pipe",
            "--service-type=exec",
            "--expand-environment=no",
            f"--unit={unit}",
            f"--property=MemoryMax={HARNESS_MEMORY}",
            "--property=MemorySwapMax=0",
            "--property=CPUQuota=100%",
            f"--property=TasksMax={HARNESS_TASKS}",
            f"--property=RuntimeMaxSec={seconds:g}",
            "--property=TimeoutStopSec=2",
            "--property=Restart=no",
            "--property=KillMode=control-group",
            "--property=SendSIGKILL=yes",
            "--property=OOMPolicy=kill",
            "--property=LimitCORE=0",
            "--property=UMask=0077",
            *cleanup,
            str(self.volume / "a/bin/python"),
            "-I",
            str(self.volume / "managed_native_worker.py"),
            mode,
            str(self.volume),
        )

    def collect_result(self) -> dict[str, Any] | None:
        # Only the fixture's fixed schema is exported. Never include subprocess
        # logs, configuration, nonce or a complete transaction record.
        reply = self.call(("sudo", "--non-interactive", "cat", str(self.volume / "result.json")))
        if reply.code:
            return None
        result = fixture_result(reply.output)
        readiness = self.manager_readiness
        failure = self.manager_failure
        if not readiness and self.cleanup_deadline is not None:
            try:
                descriptor = os.open(
                    self.root / "result.json",
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW,
                )
                with os.fdopen(descriptor, "rb") as source:
                    info = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_mode & 0o077
                    ):
                        raise FixtureError("native_result_owner")
                    saved = fixture_result(source.read(OUTPUT_BYTES + 1))
                readiness = saved["resources"].get("manager_readiness", {})
                failure = saved["resources"].get("manager_failure", {})
            except (OSError, ValueError):
                pass  # Optional old facts cannot grant cleanup success or hide its failure.
        if readiness:
            result["resources"]["manager_readiness"] = readiness
        if failure:
            result["resources"]["manager_failure"] = failure
        write_json(self.root / "result.json", result)
        return result

    def observe_startup(
        self,
        fixture: dict[str, Any],
        *,
        deadline: float,
        client_status: int | None,
        client_code: str = "not_started",
    ) -> None:
        """Use runner's working Python, not the failed retained interpreter."""
        unit = fixture["harness_unit"]
        facts: dict[str, Any] = {
            "unit": unit,
            "client_status": client_status,
            "client_code": client_code,
            "diagnostic_status": None,
            "diagnostic_code": "not_started",
            "code": "unavailable",
            "stage": "not_started",
            "query_status": None,
            "query_code": "not_started",
            "failure": "not_started",
            "ancestors": [],
        }
        try:
            uid, _ = self.current_account()
            assert self.receipt is not None
            end = min(deadline, time.monotonic() + CLEANUP_SECONDS)
            if end <= time.monotonic():
                raise FixtureError("native_diagnostic_deadline")
            worker = Path(__file__).resolve().parents[1] / "tests/helpers/managed_native_worker.py"
            reply = self.call(
                (
                    sys.executable,
                    "-I",
                    str(worker),
                    "diagnose",
                    self.receipt.user,
                    str(uid),
                    str(self.home),
                    unit,
                    str(end),
                ),
                seconds=end - time.monotonic(),
            )
            facts["diagnostic_status"] = reply.code
            facts["diagnostic_code"] = command_code(reply.code, reply.output)
            if reply.code == 0:
                observed = {
                    **object_bytes(reply.output),
                    "client_status": client_status,
                    "client_code": client_code,
                    "diagnostic_status": reply.code,
                    "diagnostic_code": "ok",
                }
                if startup_facts(observed, unit):
                    facts = observed
                else:
                    facts["diagnostic_code"] = "invalid_reply"
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            # Diagnostic failure never authorizes another launch or hides the first error.
            facts["diagnostic_code"] = (
                "invalid_reply" if facts["diagnostic_status"] == 0 else command_error_code(error)
            )
        result = fixture_result((self.root / "result.json").read_bytes())
        result["resources"]["startup"] = facts
        write_json(self.root / "result.json", fixture_result(json.dumps(result).encode()))

    def execute(self) -> None:
        deadline = time.monotonic() + HARNESS_SECONDS + CLEANUP_SECONDS
        self.execute_deadline = deadline
        try:
            self.prepare()
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            try:
                self.observe_setup(error)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass  # An evidence failure must preserve the original preparation failure.
            raise
        if self.execute_remaining() <= CASE_SECONDS + CLEANUP_SECONDS:
            raise FixtureError("native_case_start_budget")
        fixture = object_bytes(self.sudo("cat", str(self.volume / "fixture.json")))
        write_json(
            self.root / "result.json",
            {
                "completed": False,
                "stage": "harness_start",
                "code": "worker_not_observed",
                "timeline": [],
                "generations": [],
                "resources": {
                    "manager": self.manager_limits,
                    **(
                        {"manager_readiness": self.manager_readiness}
                        if self.manager_readiness
                        else {}
                    ),
                },
            },
        )
        client_status: int | None = None
        client_failure = "not_started"
        result: dict[str, Any] | None = None
        failed = False
        try:
            reply = self.call(
                self.user_arguments(*self.transient(fixture, "drive", HARNESS_SECONDS)),
                seconds=deadline - time.monotonic(),
            )
            client_status = reply.code
            client_failure = command_code(reply.code, reply.output)
            if client_status:
                raise FixtureError("native_command_failed")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            failed = True
            if client_status is None:
                client_failure = command_error_code(error)
            raise
        finally:
            if time.monotonic() < deadline:
                try:
                    result = self.collect_result()
                    if result is None:
                        self.observe_startup(
                            fixture,
                            deadline=deadline,
                            client_status=client_status,
                            client_code=client_failure,
                        )
                except (ValueError, OSError, subprocess.SubprocessError):
                    if not failed:
                        raise
        if result is None or result.get("completed") is not True:
            raise FixtureError("native_case_failed")

    def observe_setup(self, error: Exception) -> None:
        """Observe two exact system units; never launch a private user-bus client."""
        receipt = self.receipt
        if receipt is None or receipt.setup_phase == "initial":
            return
        uid, _ = self.current_account()
        facts: dict[str, Any] = {
            "uid": uid,
            "phase": receipt.setup_phase,
            "client_code": command_error_code(error),
            "diagnostic_status": None,
            "diagnostic_code": "not_started",
            "stage": "not_started",
            "query_status": None,
            "query_code": "not_started",
            "failure": "not_started",
            "unit": "",
            "units": [],
            "process": {
                "code": "not_started",
                "exe": "unavailable",
                "uid_matches": None,
                "state": "",
                "wait": "unavailable",
            },
            "journal": {"code": "not_started", "incomplete": False, "events": []},
            "context": {
                "code": "not_started",
                "notify_env": "unavailable",
                "runtime_env": "unavailable",
                "bus_env": "unavailable",
                "listen_env": "unavailable",
                "parent_notify": "unavailable",
                "user_notify": "unavailable",
                "private_socket": "unavailable",
            },
            "notification": {
                "code": "not_started",
                "status_text": "unavailable",
                "notify_access": "unavailable",
                "service_type": "unavailable",
                "result": "unavailable",
                "active_state_after": "unavailable",
                "sub_state_after": "unavailable",
            },
        }
        end = time.monotonic() + CLEANUP_SECONDS
        if self.execute_deadline is not None:
            end = min(end, self.execute_deadline)
        if time.monotonic() >= end:
            return
        try:
            allowance = OUTPUT_BYTES - self.setup_bytes
            if allowance <= 1024:
                raise FixtureError("native_diagnostic_output_budget")
            worker = Path(__file__).resolve().parents[1] / "tests/helpers/managed_native_worker.py"
            reply = self.call(
                (
                    sys.executable,
                    "-I",
                    str(worker),
                    "diagnose-setup",
                    receipt.user,
                    str(uid),
                    str(self.home),
                    str(end),
                    str(allowance - 1024),
                ),
                seconds=end - time.monotonic(),
            )
            facts["diagnostic_status"] = reply.code
            if time.monotonic() >= end:
                raise FixtureError("native_diagnostic_deadline")
            facts["diagnostic_code"] = command_code(reply.code, reply.output)
            if reply.code == 0:
                data = object_bytes(reply.output)
                used = data.pop("used", None)
                if type(used) is not int or not 0 <= used <= allowance:
                    raise FixtureError("native_diagnostic_output_budget")
                self.setup_bytes += used + len(reply.output)
                if self.setup_bytes > OUTPUT_BYTES:
                    raise FixtureError("native_diagnostic_output_budget")
                observed = {
                    **data,
                    **{
                        key: facts[key]
                        for key in (
                            "uid",
                            "phase",
                            "client_code",
                            "diagnostic_status",
                            "diagnostic_code",
                        )
                    },
                }
                if setup_facts(observed):
                    facts = observed
                else:
                    facts["diagnostic_code"] = "invalid_reply"
        except (ValueError, OSError, subprocess.SubprocessError) as failure:
            facts["diagnostic_code"] = command_error_code(failure)
        self.save_setup_result({"setup": facts})

    def save_setup_result(self, resources: dict[str, Any]) -> None:
        """Save the validated same-observation summary before optional diagnostics."""
        result = {
            "completed": False,
            "stage": "manager_setup",
            "code": "setup_failed",
            "timeline": [],
            "generations": [],
            "resources": {
                **resources,
                **({"manager_failure": self.manager_failure} if self.manager_failure else {}),
            },
        }
        write_json(self.root / "result.json", fixture_result(json.dumps(result).encode()))

    @staticmethod
    def same_mount(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
        # Remounting the same bind read-only may succeed before its response is
        # observed. Options are not a new mount identity and cannot orphan it.
        return all(actual[key] == expected[key] for key in expected if key != "options")

    def cleanup(self) -> None:
        if not self.root.exists():
            return
        self.cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        root_info = self.root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.getuid()
            or root_info.st_mode & 0o022
        ):
            raise FixtureError("native_root_owner")
        self.receipt = Receipt.read(self.receipt_path)
        receipt = self.receipt
        if receipt.temporary is not None:
            self.check_temporary()
            if receipt.volume_directory is None and os.path.lexists(self.volume):
                raise FixtureError("native_volume_creation_unconfirmed")
        elif receipt.volume_directory is not None or receipt.mounts:
            raise FixtureError("native_receipt_invalid")
        if not receipt.user_created and (
            account(receipt.user) is not None or private_group(receipt.user) is not None
        ):
            # A lost useradd response is not proof of ownership. Do not delete
            # an account merely because its name appeared in our setup intent.
            raise FixtureError("native_account_creation_unconfirmed")
        if receipt.image is not None:
            image = (self.root / "filesystem.ext4").lstat()
            if (
                not stat.S_ISREG(image.st_mode)
                or image.st_uid != os.getuid()
                or (image.st_dev, image.st_ino) != receipt.image
                or image.st_size != IMAGE_BYTES
            ):
                raise FixtureError("native_image_changed")
        failure = False
        result: dict[str, Any] | None = None
        if receipt.user_created and not receipt.user_removed:
            uid, _ = self.current_account()  # Recheck mapping before any stop or deletion.
            environment_ready = self.check_manager_environment()
            if not environment_ready and receipt.environment_directory is not None:
                self.sudo("rmdir", "--", str(self.environment_directory()))
                receipt.environment_directory = None
                self.save()
            try:
                fixture = object_bytes(self.sudo("cat", str(self.volume / "fixture.json")))
                self.as_user(
                    *self.transient(fixture, "cleanup", CLEANUP_SECONDS), seconds=CLEANUP_SECONDS
                )
            except (FixtureError, OSError, subprocess.SubprocessError):
                failure = True
            result = self.collect_result()
            # This dedicated UID has no unrelated services; never touch runner's manager.
            self.sudo(
                "systemctl",
                "stop",
                f"user@{uid}.service",
                f"user-runtime-dir@{uid}.service",
                seconds=CLEANUP_SECONDS,
            )
            if receipt.manager_group:
                group = CGROUP_ROOT / receipt.manager_group.lstrip("/")
                if (
                    group.exists()
                    and "populated 0" not in (group / "cgroup.events").read_text().splitlines()
                ):
                    raise FixtureError("native_manager_not_empty")
            self.check_manager_environment()
            if receipt.bootstrap_directories:
                self.bootstrap_files("remove")
            self.sudo("systemctl", "revert", f"user@{uid}.service")
            if os.path.lexists(self.environment_directory()):
                raise FixtureError("native_manager_environment_not_removed")
            receipt.environment_file = None
            receipt.environment_directory = None
            self.save()
        for value in list(reversed(receipt.mounts)):
            target = Path(value["target"])
            if target not in {
                self.volume,
                self.volume / "input/python",
                self.volume / "input/deps",
            }:
                raise FixtureError("native_mount_receipt_invalid")
            actual = self.mount(target)
            if actual is not None:
                if not self.same_mount(actual, value):
                    raise FixtureError("native_mount_changed")
                self.sudo("umount", str(target))
                if self.mount(target) is not None:
                    raise FixtureError("native_mount_not_released")
            if target == self.volume:
                self.check_volume_directory()
            receipt.mounts.remove(value)
            self.save()
        if any(
            self.mount(path) is not None
            for path in (self.volume / "input/deps", self.volume / "input/python", self.volume)
        ):
            raise FixtureError("native_unrecorded_mount")
        if receipt.volume_directory is not None:
            self.check_volume_directory()
        if receipt.user_created and not receipt.user_removed:
            self.current_account()
            self.sudo("userdel", receipt.user)
            if account(receipt.user) is not None:
                raise FixtureError("native_user_not_removed")
            receipt.user_removed = True
            self.save()
        if receipt.user_created and not receipt.group_removed:
            if account(receipt.user) is not None:
                raise FixtureError("native_account_changed")
            remaining_group = private_group(receipt.user)
            if remaining_group is not None:
                if remaining_group != (receipt.gid, ()):
                    raise FixtureError("native_group_changed")
                self.sudo("groupdel", receipt.user)
                if private_group(receipt.user) is not None:
                    raise FixtureError("native_group_not_removed")
            receipt.group_removed = True
            self.save()
        # Keep bounded owner receipt/result as evidence; never recursively remove a mount tree.
        if self.cleanup_deadline is None or time.monotonic() >= self.cleanup_deadline:
            raise FixtureError("native_cleanup_deadline")
        if result is not None:
            result["resources"]["cleanup"] = "failed" if failure else "verified"
            write_json(self.root / "result.json", result)
        if failure:
            raise FixtureError("native_service_cleanup_failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "cleanup"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    require_ci(args.root)
    fixture = NativeCI(args.root)
    try:
        if args.mode == "cleanup":
            fixture.cleanup()
        else:
            fixture.execute()
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(
            str(error) if isinstance(error, FixtureError) else "native_fixture_failed",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
