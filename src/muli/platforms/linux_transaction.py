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
from muli.platforms.installation import HelperLimits
from muli.platforms.transaction_files import _open_directory
from muli.process import run_process
from muli.service import UNIT, UserService

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
_HELPER_UNIT_FIELDS = (
    ("Transient", "b"),
    ("CollectMode", "s"),
    ("PartOf", "as"),
    ("BindsTo", "as"),
    ("StopPropagatedFrom", "as"),
    ("Requires", "as"),
    ("Requisite", "as"),
    ("Conflicts", "as"),
    ("StartLimitIntervalUSec", "t"),
    ("StartLimitBurst", "u"),
)
_HELPER_SERVICE_FIELDS = (
    ("Restart", "s"),
    ("RestartUSec", "t"),
    ("RuntimeMaxUSec", "t"),
    ("TimeoutStopUSec", "t"),
    ("MemoryMax", "t"),
    ("MemorySwapMax", "t"),
    ("CPUQuotaPerSecUSec", "t"),
    ("TasksMax", "t"),
    ("LimitCORE", "t"),
    ("UMask", "u"),
    ("NoNewPrivileges", "b"),
    ("OOMPolicy", "s"),
)


class ObservationError(ValueError):
    """Only fixed reason codes cross the management boundary."""


class CommandRunner(Protocol):
    def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> Awaitable[dict[str, Any]]: ...


class _KernelResourceLimits(Protocol):
    @property
    def memory_bytes(self) -> int: ...

    @property
    def cpu_percent(self) -> int: ...

    @property
    def tasks(self) -> int: ...


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


@dataclass(frozen=True, slots=True)
class HelperSnapshot:
    unit: UnitSnapshot
    process: ProcessIdentity
    limits: HelperLimits


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


def _number(value: object, bits: int = 32) -> int:
    if type(value) is not int or not 0 <= value < 2**bits:
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
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        join_interface_reads: bool = False,
    ) -> None:
        self.runner, self.limits, self.proc_root = runner, limits or ProbeLimits(), proc_root
        self.cgroup_root = cgroup_root
        self._join_interface_reads = join_interface_reads

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
            exit_code = result.get("exit_code")
            if type(exit_code) is int and exit_code < 0:
                raise ObservationError("systemd_observation_unavailable")
            # Distinct owned raise locations retain only these bounded failure
            # categories in the existing safe traceback, never manager stderr.
            if (
                type(exit_code) is int
                and exit_code > 0
                and arguments == ("call", _BUS, _MANAGER, _BUS + ".Manager", "GetUnit", "s", UNIT)
                and result.get("truncated") is False
                and type(result.get("stderr")) is str
                and result["stderr"] == f"Call failed: Unit {UNIT} not loaded.\n"
            ):
                raise ObservationError("systemd_observation_unavailable")
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

    async def _interface_properties(
        self,
        path: str,
        unit_fields: tuple[tuple[str, str], ...],
        service_fields: tuple[tuple[str, str], ...],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._join_interface_reads:
            unit = await self._properties(path, "Unit", unit_fields)
            service = await self._properties(path, "Service", service_fields)
            return unit, service

        async def read(
            interface: str, fields: tuple[tuple[str, str], ...]
        ) -> dict[str, Any] | Exception:
            try:
                return await self._properties(path, interface, fields)
            except Exception as error:
                # Keep Unit's original error priority without wrapping it in an
                # ExceptionGroup. Cancellation remains owned by the TaskGroup.
                return error

        async with asyncio.TaskGroup() as group:
            unit_task = group.create_task(read("Unit", unit_fields))
            service_task = group.create_task(read("Service", service_fields))
            unit_result = await unit_task
            if isinstance(unit_result, Exception):
                service_task.cancel()

        # Both branches have settled before returning data or the original error.
        if isinstance(unit_result, Exception):
            raise unit_result
        service_result = service_task.result()
        if isinstance(service_result, Exception):
            raise service_result
        return unit_result, service_result

    async def _snapshot(self, path: str) -> UnitSnapshot:
        unit, service = await self._interface_properties(path, _UNIT_FIELDS, _SERVICE_FIELDS)
        return self._snapshot_values(unit, service)

    @staticmethod
    def _snapshot_values(unit: dict[str, Any], service: dict[str, Any]) -> UnitSnapshot:
        invocation, job, entries = unit["InvocationID"], unit["Job"], service["ExecStart"]
        if not isinstance(invocation, list) or len(invocation) not in {0, 16}:
            raise ObservationError("invalid_invocation")
        if any(type(item) is not int or not 0 <= item <= 255 for item in invocation):
            raise ObservationError("invalid_invocation")
        if not isinstance(job, list) or len(job) != 2:
            raise ObservationError("invalid_job")
        active_state = _string(unit["ActiveState"])
        pid = _number(service["MainPID"])
        current_job = (_number(job[0]), _string(job[1]))
        if not invocation:
            # systemd encodes an absent ID as an empty ay, not sixteen zero bytes.
            if active_state not in {"inactive", "failed"} or pid != 0 or current_job != (0, "/"):
                raise ObservationError("invalid_invocation")
            invocation = [0] * 16
        if not isinstance(entries, list) or len(entries) != 1:
            raise ObservationError("unsupported_exec_start")
        entry = entries[0]
        if not isinstance(entry, list) or len(entry) != 10:
            raise ObservationError("invalid_exec_start")
        return UnitSnapshot(
            _string(unit["Id"]),
            _string(unit["LoadState"]),
            active_state,
            _string(unit["SubState"]),
            _string(unit["FragmentPath"]),
            _string(unit["SourcePath"]),
            _strings(unit["DropInPaths"]),
            _boolean(unit["NeedDaemonReload"]),
            bytes(invocation).hex(),
            current_job,
            _string(service["Type"]),
            pid,
            _cgroup(service["ControlGroup"]),
            _string(entry[0]),
            _strings(entry[1]),
            _boolean(entry[2]),
            _string(service["KillMode"]),
            _boolean(service["SendSIGKILL"]),
        )

    async def _unit_path(self, name: str) -> str:
        reply = _typed(
            await self._command("call", _BUS, _MANAGER, _BUS + ".Manager", "GetUnit", "s", name),
            "o",
        )
        if not isinstance(reply, list) or len(reply) != 1 or not isinstance(reply[0], str):
            raise ObservationError("invalid_unit_path")
        path = reply[0]
        if not re.fullmatch(r"/org/freedesktop/systemd1/unit/[a-zA-Z0-9_]+", path):
            raise ObservationError("invalid_unit_path")
        return path

    async def _helper_properties(self, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
        # Each round independently reads both interfaces. The returned values
        # belong only to this observation, never to a cached manager proof.
        return await self._interface_properties(
            path, _UNIT_FIELDS + _HELPER_UNIT_FIELDS, _SERVICE_FIELDS + _HELPER_SERVICE_FIELDS
        )

    @staticmethod
    def _helper_limits(unit: dict[str, Any], service: dict[str, Any], limits: HelperLimits) -> None:
        expected: dict[str, object] = {
            "Transient": True,
            "CollectMode": "inactive",
            "PartOf": (),
            "BindsTo": (),
            "StopPropagatedFrom": (),
            "StartLimitIntervalUSec": round(limits.seconds * 1_000_000),
            "StartLimitBurst": limits.restart_burst,
            "Restart": "on-failure",
            "RestartUSec": round(limits.restart_delay * 1_000_000),
            "RuntimeMaxUSec": round(limits.seconds * 1_000_000),
            "TimeoutStopUSec": round(limits.stop_seconds * 1_000_000),
            "MemoryMax": limits.memory_bytes,
            "MemorySwapMax": 0,
            "CPUQuotaPerSecUSec": limits.cpu_percent * 10_000,
            "TasksMax": limits.tasks,
            "LimitCORE": 0,
            "UMask": 0o077,
            "NoNewPrivileges": True,
            "OOMPolicy": "kill",
        }
        for values, fields in (
            (unit, _HELPER_UNIT_FIELDS),
            (service, _HELPER_SERVICE_FIELDS),
        ):
            for name, signature in fields:
                raw = values[name]
                value = (
                    _boolean(raw)
                    if signature == "b"
                    else _strings(raw)
                    if signature == "as"
                    else _string(raw)
                    if signature == "s"
                    else _number(raw, 64 if signature == "t" else 32)
                )
                if name in {"Requires", "Requisite", "Conflicts"}:
                    if UNIT in _strings(raw):
                        raise ObservationError("helper_lifetime_dependency")
                elif value != expected[name]:
                    raise ObservationError("helper_limits_mismatch")

    async def helper_exists(self, name: str) -> bool:
        """Only a successful exact-name empty list proves absence."""
        if sys.platform != "linux":
            raise ObservationError("platform_unsupported")
        if not re.fullmatch(r"muli-transaction-[a-f0-9]{64}\.service", name):
            raise ObservationError("invalid_helper_identity")
        async with asyncio.timeout(self.limits.seconds):
            reply = _typed(
                await self._command(
                    "call",
                    _BUS,
                    _MANAGER,
                    _BUS + ".Manager",
                    "ListUnitsByPatterns",
                    "asas",
                    "0",
                    "1",
                    name,
                ),
                "a(ssssssouso)",
            )
            if (
                not isinstance(reply, list)
                or len(reply) != 1
                or not isinstance(reply[0], list)
                or len(reply[0]) > 1
            ):
                raise ObservationError("invalid_helper_listing")
            if not reply[0]:
                return False
            entry = reply[0][0]
            if not isinstance(entry, list) or len(entry) != 10 or entry[0] != name:
                raise ObservationError("invalid_helper_listing")
            for index in (0, 1, 2, 3, 4, 5, 6, 8, 9):
                _string(entry[index])
            _number(entry[7])
            if not re.fullmatch(r"/org/freedesktop/systemd1/unit/[a-zA-Z0-9_]+", entry[6]):
                raise ObservationError("invalid_unit_path")
            return True

    def _kernel_limits(self, cgroup: str, limits: _KernelResourceLimits) -> tuple[object, ...]:
        path = self.cgroup_root / cgroup.removeprefix("/")
        descriptor = _open_directory(path)
        try:
            identity = os.fstat(descriptor)
            values: list[int] = []
            for name, maximum in (
                ("memory.max", limits.memory_bytes),
                ("memory.swap.max", 0),
                ("pids.max", limits.tasks),
            ):
                raw = _read_regular(Path(name), 128, directory_fd=descriptor).strip()
                if not re.fullmatch(rb"[0-9]{1,20}", raw):
                    raise ObservationError("helper_kernel_limits")
                value = int(raw)
                if value > maximum or (name != "memory.swap.max" and value == 0):
                    raise ObservationError("helper_kernel_limits")
                values.append(value)
            cpu = _read_regular(Path("cpu.max"), 128, directory_fd=descriptor).strip()
            if not re.fullmatch(rb"[0-9]{1,20} [0-9]{1,20}", cpu):
                raise ObservationError("helper_kernel_limits")
            quota, period = map(int, cpu.split())
            if min(quota, period) <= 0 or quota * 100 > period * limits.cpu_percent:
                raise ObservationError("helper_kernel_limits")
            if (
                _read_regular(Path("memory.oom.group"), 128, directory_fd=descriptor).strip()
                != b"1"
            ):
                raise ObservationError("helper_kernel_limits")
            current = _open_directory(path)
            try:
                actual = os.fstat(current)
                if (actual.st_dev, actual.st_ino) != (identity.st_dev, identity.st_ino):
                    raise ObservationError("helper_cgroup_changed")
            finally:
                os.close(current)
            return (identity.st_dev, identity.st_ino, *values, quota, period)
        finally:
            os.close(descriptor)

    async def read_helper(
        self, name: str, argv: tuple[str, ...], limits: HelperLimits
    ) -> HelperSnapshot:
        """Observe an already running helper; never create units or relax limits."""
        if sys.platform != "linux":
            raise ObservationError("platform_unsupported")
        if not re.fullmatch(r"muli-transaction-[a-f0-9]{64}\.service", name) or not argv:
            raise ObservationError("invalid_helper_identity")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.seconds
        try:
            async with asyncio.timeout(self.limits.seconds):
                path = await self._unit_path(name)
                unit, service = await self._helper_properties(path)
                before = self._snapshot_values(unit, service)
                if (
                    before.name != name
                    or before.load_state != "loaded"
                    or before.active_state != "active"
                    or before.sub_state != "running"
                    or before.service_type != "exec"
                    or before.source
                    or before.drop_ins
                    or before.needs_reload
                    or before.argv != argv
                    or before.executable != argv[0]
                    or before.ignore_failure
                    or before.kill_mode != "control-group"
                    or not before.send_sigkill
                    or before.pid == 0
                    or before.job != (0, "/")
                    or before.invocation == "0" * 32
                    or Path(before.cgroup).name != name
                    or UNIT in Path(before.cgroup).parts
                ):
                    raise ObservationError("unsupported_helper_layout")
                self._helper_limits(unit, service, limits)
                process = self._process(before.pid)
                if (
                    process.uid != os.geteuid()
                    or process.argv != argv
                    or process.executable != str(Path(argv[0]).resolve(strict=True))
                    or process.cgroup != before.cgroup
                ):
                    raise ObservationError("foreign_helper_process")
                kernel = self._kernel_limits(before.cgroup, limits)
                unit, service = await self._helper_properties(path)
                after = self._snapshot_values(unit, service)
                self._helper_limits(unit, service, limits)
                if (
                    before != after
                    or process != self._process(process.pid)
                    or kernel != self._kernel_limits(before.cgroup, limits)
                ):
                    raise ObservationError("helper_observation_changed")
                if loop.time() >= deadline:
                    raise TimeoutError
                return HelperSnapshot(after, process, limits)
        except TimeoutError:
            raise
        except (OSError, UnicodeError):
            raise ObservationError("helper_observation_unavailable") from None

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
                path = await self._unit_path(service.unit)
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
