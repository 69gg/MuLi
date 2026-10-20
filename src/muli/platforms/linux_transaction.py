"""Bounded read-only systemd observations; not permission to stop or replace a service."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from muli.management_transaction import ClockReading, TransactionError
from muli.process import run_process
from muli.service import UserService

_BUS = "org.freedesktop.systemd1"
_MANAGER = "/org/freedesktop/systemd1"
_UNIT_FIELDS = (
    ("Id", "s"),
    ("LoadState", "s"),
    ("ActiveState", "s"),
    ("SubState", "s"),
    ("FragmentPath", "s"),
    ("SourcePath", "s"),
    ("DropInPaths", "as"),
    ("NeedDaemonReload", "b"),
    ("InvocationID", "ay"),
    ("Job", "(uo)"),
)
_SERVICE_FIELDS = (
    ("Type", "s"),
    ("MainPID", "u"),
    ("ControlGroup", "s"),
    ("ExecStart", "a(sasbttttuii)"),
    ("KillMode", "s"),
    ("SendSIGKILL", "b"),
)


class ObservationError(ValueError):
    """Only fixed reason codes cross the management boundary."""


class CommandRunner(Protocol):
    def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> Awaitable[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ProbeLimits:
    seconds: float = 5.0
    command_bytes: int = 65_536
    file_bytes: int = 32_768

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds) or not 0 < self.seconds <= 30:
            raise ValueError("invalid_probe_timeout")
        if not 1024 <= self.command_bytes <= 262_144 or not 1024 <= self.file_bytes <= 262_144:
            raise ValueError("invalid_probe_budget")


@dataclass(frozen=True, slots=True)
class UnitSnapshot:
    name: str
    load_state: str
    active_state: str
    sub_state: str
    fragment: str
    source: str
    drop_ins: tuple[str, ...]
    needs_reload: bool
    invocation: str
    job: tuple[int, str]
    service_type: str
    pid: int
    cgroup: str
    executable: str
    argv: tuple[str, ...]
    ignore_failure: bool
    kill_mode: str
    send_sigkill: bool


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    uid: int
    executable: str
    argv: tuple[str, ...]
    cgroup: str


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    unit: UnitSnapshot
    process: ProcessIdentity | None


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ObservationError("invalid_bus_reply")
        result[name] = value
    return result


def _typed(line: str, signature: str) -> Any:
    try:
        value = json.loads(line, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise ObservationError("invalid_bus_reply") from None
    if not isinstance(value, dict) or set(value) != {"type", "data"}:
        raise ObservationError("invalid_bus_reply")
    if value["type"] != signature:
        raise ObservationError("invalid_bus_type")
    return value["data"]


def _string(value: object) -> str:
    if not isinstance(value, str) or any(c in value for c in "\x00\n\r"):
        raise ObservationError("invalid_bus_string")
    return value


def _number(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**32:
        raise ObservationError("invalid_bus_integer")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ObservationError("invalid_bus_boolean")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ObservationError("invalid_bus_array")
    return tuple(_string(item) for item in value)


def _cgroup(value: object) -> str:
    path = _string(value)
    if path and (
        not path.startswith("/") or any(p in {"", ".", ".."} for p in path[1:].split("/"))
    ):
        raise ObservationError("invalid_cgroup_path")
    return path


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read_regular(path: Path, limit: int, *, directory_fd: int | None = None) -> bytes:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd
    )
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ObservationError("invalid_observation_file")
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    current = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
    identities = [_file_identity(s) for s in (before, after, current)]
    if len(data) > limit or identities[0] != identities[1] or identities[0] != identities[2]:
        raise ObservationError("observation_file_changed")
    return data


def clock_reading(boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id")) -> ClockReading:
    """The persistent deadline uses kernel boot identity, never a unit InvocationID."""
    if sys.platform != "linux":
        raise TransactionError("clock_invalid")
    try:
        boot_id = _read_regular(boot_id_path, 128).decode("ascii").removesuffix("\n")
        if not re.fullmatch(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", boot_id
        ):
            raise ValueError
        return ClockReading(boot_id=boot_id, wall=time.time(), monotonic=time.monotonic())
    except (OSError, ValueError):
        raise TransactionError("clock_invalid") from None


def _start_ticks(value: bytes, pid: int) -> int:
    try:
        prefix, tail = value.rsplit(b")", 1)
        fields = tail.split()
        if int(prefix.split(b" ", 1)[0]) != pid or fields[0] in {b"Z", b"X"}:
            raise ValueError
        ticks = int(fields[19])
        if ticks < 0:
            raise ValueError
        return ticks
    except (ValueError, IndexError):
        raise ObservationError("invalid_process_identity") from None


class SystemdProbe:
    def __init__(
        self,
        *,
        runner: CommandRunner = run_process,
        limits: ProbeLimits | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.runner, self.limits, self.proc_root = runner, limits or ProbeLimits(), proc_root

    async def _command(self, *arguments: str) -> str:
        result = await self.runner(
            [
                "busctl",
                "--user",
                "--json=short",
                "--auto-start=no",
                "--allow-interactive-authorization=no",
                f"--timeout={self.limits.seconds}s",
                *arguments,
            ],
            timeout=self.limits.seconds,
            limit=self.limits.command_bytes,
            merge_stderr=False,
        )
        if result.get("exit_code") != 0:
            raise ObservationError("systemd_observation_unavailable")
        output = result.get("output")
        if not isinstance(output, str) or len(output.encode("utf-8")) > self.limits.command_bytes:
            raise ObservationError("invalid_bus_output")
        return output

    async def _properties(
        self, path: str, interface: str, fields: tuple[tuple[str, str], ...]
    ) -> dict[str, Any]:
        output = await self._command(
            "get-property", _BUS, path, _BUS + "." + interface, *(name for name, _ in fields)
        )
        lines = output.splitlines()
        if len(lines) != len(fields):
            raise ObservationError("invalid_bus_reply_count")
        return {
            name: _typed(line, signature)
            for line, (name, signature) in zip(lines, fields, strict=True)
        }

    async def _snapshot(self, path: str) -> UnitSnapshot:
        unit = await self._properties(path, "Unit", _UNIT_FIELDS)
        service = await self._properties(path, "Service", _SERVICE_FIELDS)
        invocation, job, entries = unit["InvocationID"], unit["Job"], service["ExecStart"]
        if not isinstance(invocation, list) or len(invocation) != 16:
            raise ObservationError("invalid_invocation")
        if any(type(item) is not int or not 0 <= item <= 255 for item in invocation):
            raise ObservationError("invalid_invocation")
        if not isinstance(job, list) or len(job) != 2:
            raise ObservationError("invalid_job")
        if not isinstance(entries, list) or len(entries) != 1:
            raise ObservationError("unsupported_exec_start")
        entry = entries[0]
        if not isinstance(entry, list) or len(entry) != 10:
            raise ObservationError("invalid_exec_start")
        return UnitSnapshot(
            _string(unit["Id"]),
            _string(unit["LoadState"]),
            _string(unit["ActiveState"]),
            _string(unit["SubState"]),
            _string(unit["FragmentPath"]),
            _string(unit["SourcePath"]),
            _strings(unit["DropInPaths"]),
            _boolean(unit["NeedDaemonReload"]),
            bytes(invocation).hex(),
            (_number(job[0]), _string(job[1])),
            _string(service["Type"]),
            _number(service["MainPID"]),
            _cgroup(service["ControlGroup"]),
            _string(entry[0]),
            _strings(entry[1]),
            _boolean(entry[2]),
            _string(service["KillMode"]),
            _boolean(service["SendSIGKILL"]),
        )

    def _process(self, pid: int) -> ProcessIdentity:
        descriptor = os.open(
            self.proc_root / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:

            def read(name: str) -> bytes:
                return _read_regular(Path(name), self.limits.file_bytes, directory_fd=descriptor)

            start = _start_ticks(read("stat"), pid)
            argv = read("cmdline")
            if not argv.endswith(b"\x00"):
                raise ObservationError("invalid_process_arguments")
            cgroup = read("cgroup").decode("utf-8").strip()
            if not cgroup.startswith("0::/") or "\n" in cgroup or "\r" in cgroup:
                raise ObservationError("unsupported_process_cgroup")
            executable = os.readlink("exe", dir_fd=descriptor)
            if start != _start_ticks(read("stat"), pid):
                raise ObservationError("process_identity_changed")
            return ProcessIdentity(
                pid,
                start,
                os.fstat(descriptor).st_uid,
                executable,
                tuple(os.fsdecode(item) for item in argv[:-1].split(b"\x00")),
                _cgroup(cgroup[3:]),
            )
        finally:
            os.close(descriptor)

    async def read(self, service: UserService) -> ServiceSnapshot:
        if service.platform != "linux":
            raise ObservationError("platform_unsupported")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.seconds
        try:
            async with asyncio.timeout(self.limits.seconds):
                reply = _typed(
                    await self._command(
                        "call", _BUS, _MANAGER, _BUS + ".Manager", "GetUnit", "s", service.unit
                    ),
                    "o",
                )
                if not isinstance(reply, list) or len(reply) != 1 or not isinstance(reply[0], str):
                    raise ObservationError("invalid_unit_path")
                path = reply[0]
                if not re.fullmatch(r"/org/freedesktop/systemd1/unit/[a-zA-Z0-9_]+", path):
                    raise ObservationError("invalid_unit_path")
                before = await self._snapshot(path)
                expected = tuple(service._arguments())
                if (
                    before.name != service.unit
                    or before.load_state != "loaded"
                    or before.fragment != str(service.path.absolute())
                    or before.source
                    or before.drop_ins
                    or before.needs_reload
                    or before.service_type != "simple"
                    or before.argv != expected
                    or before.executable != expected[0]
                    or before.ignore_failure
                    or before.kill_mode != "control-group"
                    or not before.send_sigkill
                ):
                    raise ObservationError("unsupported_service_layout")
                unit_identity = _file_identity(service.path.stat(follow_symlinks=False))
                unit_bytes = _read_regular(service.path, self.limits.file_bytes)
                if unit_bytes != service.render().encode("utf-8"):
                    raise ObservationError("unsupported_unit_file")
                process = self._process(before.pid) if before.pid else None
                if process and (
                    process.uid != os.geteuid()
                    or process.argv != expected
                    or process.executable != str(Path(service.executable).resolve(strict=True))
                    or process.cgroup != before.cgroup
                    or before.invocation == "0" * 32
                ):
                    raise ObservationError("foreign_process")
                after = await self._snapshot(path)
                if before != after or unit_bytes != _read_regular(
                    service.path, self.limits.file_bytes
                ):
                    raise ObservationError("service_observation_changed")
                if unit_identity != _file_identity(service.path.stat(follow_symlinks=False)):
                    raise ObservationError("service_observation_changed")
                if process is not None and process != self._process(process.pid):
                    raise ObservationError("process_identity_changed")
                if loop.time() >= deadline:
                    raise TimeoutError
                return ServiceSnapshot(after, process)
        except TimeoutError:
            raise
        except (OSError, UnicodeError):
            raise ObservationError("systemd_observation_unavailable") from None
