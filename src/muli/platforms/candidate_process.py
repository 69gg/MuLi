"""One bounded Linux candidate process; no installation policy or naked fallback."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import socket
import stat
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from muli.candidate_policy import CandidateError, CandidateLimits, ProcessOutput, ProcessSpec
from muli.platforms.candidate_process_state import AcceptedUnit, PersistProcess, ProcessPersistence
from muli.platforms.linux_controller import CgroupObservation
from muli.platforms.linux_transaction import (
    _BUS,
    _MANAGER,
    CommandRunner,
    ObservationError,
    ProbeLimits,
    ProcessIdentity,
    SystemdProbe,
    UnitSnapshot,
    _boolean,
    _number,
    _string,
    _strings,
    _typed,
)
from muli.platforms.transaction_files import _open_directory
from muli.process import _wait_cleanup, run_process
from muli.service import UNIT

_UNIT_FIELDS = (
    ("Transient", "b"),
    ("CollectMode", "s"),
    ("PartOf", "as"),
    ("BindsTo", "as"),
    ("StopPropagatedFrom", "as"),
    ("Requires", "as"),
    ("Requisite", "as"),
    ("Conflicts", "as"),
)
_SERVICE_FIELDS = (
    ("Restart", "s"),
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


class CandidateProcessError(CandidateError):
    """False cleanup proof means the attempt and all its paths must be retained."""

    def __init__(self, code: str, *, unit_name: str = "", cleanup_confirmed: bool = True) -> None:
        super().__init__(code)
        self.unit_name = unit_name
        self.cleanup_confirmed = cleanup_confirmed


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    unit: UnitSnapshot
    process: ProcessIdentity | None = None


def _name(name: str) -> None:
    if not re.fullmatch(r"muli-candidate-[a-f0-9]{32}\.service", name):
        raise ObservationError("candidate_unit_identity")


def _layout(unit: UnitSnapshot, name: str, argv: tuple[str, ...]) -> None:
    _name(name)
    if (
        unit.name != name
        or unit.load_state != "loaded"
        or unit.source
        or unit.drop_ins
        or unit.needs_reload
        or unit.service_type != "exec"
        or unit.argv != argv
        or unit.executable != argv[0]
        or unit.ignore_failure
        or unit.kill_mode != "control-group"
        or not unit.send_sigkill
        or unit.invocation == "0" * 32
        or (unit.cgroup and Path(unit.cgroup).name != name)
        or UNIT in Path(unit.cgroup).parts
    ):
        raise ObservationError("candidate_unit_layout")


class CandidateProbe(SystemdProbe):
    """Reuse typed D-Bus, proc and kernel parsers; candidate lifetime is separate."""

    async def exists(self, name: str) -> bool:
        _name(name)
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
        if not isinstance(reply, list) or len(reply) != 1 or not isinstance(reply[0], list):
            raise ObservationError("candidate_unit_listing")
        if not reply[0]:
            return False
        if len(reply[0]) != 1:
            raise ObservationError("candidate_unit_listing")
        entry = reply[0][0]
        if not isinstance(entry, list) or len(entry) != 10 or entry[0] != name:
            raise ObservationError("candidate_unit_listing")
        for index in (0, 1, 2, 3, 4, 5, 6, 8, 9):
            _string(entry[index])
        _number(entry[7])
        if not re.fullmatch(r"/org/freedesktop/systemd1/unit/[a-zA-Z0-9_]+", entry[6]):
            raise ObservationError("invalid_unit_path")
        return True

    async def owned(self, name: str, argv: tuple[str, ...]) -> CandidateObservation | None:
        if not await self.exists(name):
            return None
        path = await self._unit_path(name)
        before = await self._snapshot(path)
        _layout(before, name, argv)
        if before != await self._snapshot(path):
            raise ObservationError("candidate_observation_changed")
        return CandidateObservation(before)

    async def _resources(self, path: str, limits: CandidateLimits, runtime_us: int) -> None:
        expected: dict[str, object] = {
            "Transient": True,
            "CollectMode": "inactive",
            "PartOf": (),
            "BindsTo": (),
            "StopPropagatedFrom": (),
            "Restart": "no",
            "RuntimeMaxUSec": runtime_us,
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
        for interface, fields in (("Unit", _UNIT_FIELDS), ("Service", _SERVICE_FIELDS)):
            raw = await self._properties(path, interface, fields)
            for name, signature in fields:
                item = raw[name]
                value = (
                    _strings(item)
                    if signature == "as"
                    else _boolean(item)
                    if signature == "b"
                    else _string(item)
                    if signature == "s"
                    else _number(item, 64 if signature == "t" else 32)
                )
                if name in {"Requires", "Requisite", "Conflicts"}:
                    if UNIT in _strings(item):
                        raise ObservationError("candidate_lifetime_dependency")
                elif value != expected[name]:
                    raise ObservationError("candidate_resource_limits")

    async def waiting(
        self, name: str, argv: tuple[str, ...], limits: CandidateLimits, runtime_us: int
    ) -> CandidateObservation:
        path = await self._unit_path(name)
        unit = await self._snapshot(path)
        _layout(unit, name, argv)
        if (
            (unit.active_state, unit.sub_state) != ("active", "running")
            or not unit.pid
            or not unit.cgroup
            or unit.job != (0, "/")
        ):
            raise ObservationError("candidate_not_waiting")
        await self._resources(path, limits, runtime_us)
        process = self._process(unit.pid)
        if (
            process.uid != os.geteuid()
            or process.argv != argv
            or process.executable != str(Path(argv[0]).resolve(strict=True))
            or process.cgroup != unit.cgroup
        ):
            raise ObservationError("candidate_foreign_process")
        kernel = self._kernel_limits(unit.cgroup, limits)
        after = await self._snapshot(path)
        await self._resources(path, limits, runtime_us)
        if (
            unit != after
            or process != self._process(unit.pid)
            or kernel != self._kernel_limits(unit.cgroup, limits)
        ):
            raise ObservationError("candidate_observation_changed")
        return CandidateObservation(unit, process)

    async def recoverable(
        self, name: str, argv: tuple[str, ...], limits: CandidateLimits, runtime_us: int
    ) -> CandidateObservation | None:
        before = await self.owned(name, argv)
        if before is not None:
            await self._resources(await self._unit_path(name), limits, runtime_us)
            if before != await self.owned(name, argv):
                raise ObservationError("candidate_observation_changed")
        return before

    async def stop(self, name: str) -> None:
        _name(name)
        _typed(
            await self._command(
                "call", _BUS, _MANAGER, _BUS + ".Manager", "StopUnit", "ss", name, "replace"
            ),
            "o",
        )

    async def kill(self, name: str) -> None:
        _name(name)
        reply = await self._command(
            "call", _BUS, _MANAGER, _BUS + ".Manager", "KillUnit", "ssi", name, "all", "9"
        )
        if reply.strip() and _typed(reply, "") != []:
            raise ObservationError("invalid_bus_reply")

    def empty(self, cgroup: str) -> bool:
        if not cgroup:
            raise ObservationError("candidate_unknown_cgroup")
        return CgroupObservation(self.cgroup_root, self.limits.file_bytes).empty(cgroup)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _arguments(name: str, worker: tuple[str, ...], spec: ProcessSpec, runtime_us: int) -> list[str]:
    properties = {
        "Type": "exec",
        "Restart": "no",
        "CollectMode": "inactive",
        "RuntimeMaxSec": f"{runtime_us}us",
        "TimeoutStopSec": f"{spec.limits.stop_seconds}s",
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "OOMPolicy": "kill",
        "MemoryMax": str(spec.limits.memory_bytes),
        "MemorySwapMax": "0",
        "CPUQuota": f"{spec.limits.cpu_percent}%",
        "TasksMax": str(spec.limits.tasks),
        "LimitCORE": "0",
        "UMask": "0077",
        "NoNewPrivileges": "yes",
    }
    return [
        "systemd-run",
        "--user",
        "--quiet",
        "--pipe",
        "--wait",
        "--expand-environment=no",
        f"--unit={name}",
        *(f"--property={key}={value}" for key, value in properties.items()),
        "--",
        *worker,
    ]


def _validate(spec: ProcessSpec) -> None:
    if sys.platform != "linux":
        raise CandidateProcessError("candidate_platform_unsupported")
    if spec.disk_mode != "threshold":
        raise CandidateProcessError("candidate_disk_quota_unsupported")
    if (
        not math.isfinite(spec.deadline)
        or not 0 < spec.deadline - time.monotonic() <= spec.limits.seconds
        or not spec.argv
        or not all(isinstance(a, str) and "\x00" not in a for a in spec.argv)
        or not Path(spec.argv[0]).is_absolute()
        or not spec.private_root.is_absolute()
        or ".." in spec.private_root.parts
        or not spec.cwd.is_absolute()
        or ".." in spec.cwd.parts
        or not spec.cwd.is_relative_to(spec.private_root)
        or len(spec.argv) + len(spec.env) > spec.limits.metadata_bytes // 2
        or len(dict(spec.env)) != len(spec.env)
        or any(not k or "=" in k or "\x00" in k + v for k, v in spec.env)
        or sum(len(a) for a in spec.argv) + sum(len(k) + len(v) for k, v in spec.env)
        > spec.limits.metadata_bytes
    ):
        raise CandidateProcessError("candidate_process_spec")


def _directory_check(path: Path, descriptor: int) -> None:
    actual = _open_directory(path)
    try:
        before, after = os.fstat(descriptor), os.fstat(actual)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_uid != os.geteuid()
            or stat.S_IMODE(after.st_mode) != 0o700
        ):
            raise CandidateProcessError("candidate_private_directory")
    finally:
        os.close(actual)


async def _receive(connection: socket.socket, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = await asyncio.get_running_loop().sock_recv(connection, size - len(output))
        if not chunk:
            raise CandidateProcessError("candidate_gate_disconnected")
        output.extend(chunk)
    return bytes(output)


async def _permit(
    listener: socket.socket,
    proof: CandidateObservation,
    nonce: str,
    *,
    probe: CandidateProbe,
    name: str,
    worker: tuple[str, ...],
    spec: ProcessSpec,
    runtime_us: int,
    directory: int,
    permit_deadline: float,
) -> None:
    assert proof.process is not None
    loop = asyncio.get_running_loop()
    for _ in range(8):
        connection, _ = await loop.sock_accept(listener)
        try:
            connection.setblocking(False)
            pid, uid, _ = struct.unpack(
                "iII", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if (pid, uid) != (proof.process.pid, os.geteuid()):
                continue
            if await _receive(connection, 64) != nonce.encode("ascii"):
                raise CandidateProcessError("candidate_gate_identity")
            if proof != await probe.waiting(name, worker, spec.limits, runtime_us):
                raise CandidateProcessError("candidate_observation_changed")
            spec.observe()
            _directory_check(spec.private_root, directory)
            _remaining(min(spec.deadline, permit_deadline))
            await loop.sock_sendall(connection, b"G")
            return
        finally:
            connection.close()
    raise CandidateProcessError("candidate_gate_attempts")


async def _settle_subtree(
    probe: CandidateProbe,
    name: str,
    worker: tuple[str, ...],
    limits: CandidateLimits,
    accepted: AcceptedUnit | None,
    on_accepted: Callable[[AcceptedUnit], None] | None = None,
) -> bool:
    """Shared bounded stop/empty proof; a missing unobserved start is ambiguous."""
    confirmed = False
    try:
        async with asyncio.timeout(2 * limits.stop_seconds):
            current = await probe.owned(name, worker)
            if current is None:
                # Without a previously observed unit, an in-flight StartTransientUnit
                # may still succeed after this absence observation. Never claim empty.
                confirmed = accepted is not None and (
                    probe.empty(accepted.cgroup) if accepted.cgroup else accepted.pid == 0
                )
            else:
                if accepted is not None:
                    if current.unit.invocation != accepted.invocation or (
                        current.unit.cgroup
                        and accepted.cgroup
                        and current.unit.cgroup != accepted.cgroup
                    ):
                        raise ObservationError("candidate_invocation_changed")
                else:
                    accepted = AcceptedUnit.capture(current.unit, current.process)
                    if on_accepted is not None:
                        try:
                            on_accepted(accepted)
                        except Exception:
                            # Continue owning/stopping the tree after a lost DB return.
                            # A final settled receipt must still persist before recovery returns.
                            pass
                cgroup = current.unit.cgroup or accepted.cgroup
                if (
                    current.unit.active_state in {"inactive", "failed"}
                    and current.unit.pid == 0
                    and current.unit.job == (0, "/")
                    and (not cgroup or probe.empty(cgroup))
                ):
                    confirmed = True
                else:
                    # The static worker ExecStart still identifies this unit after execve.
                    await probe.stop(name)
                    kill_at = time.monotonic() + limits.stop_seconds / 2
                    killed = False
                    while True:
                        if cgroup and probe.empty(cgroup):
                            latest = await probe.owned(name, worker)
                            if latest is None or (
                                latest.unit.invocation == current.unit.invocation
                                and latest.unit.active_state in {"inactive", "failed"}
                                and latest.unit.pid == 0
                                and latest.unit.job == (0, "/")
                            ):
                                confirmed = True
                                break
                        if not killed and time.monotonic() >= kill_at:
                            # Recheck static ownership before a forced all-process kill.
                            latest = await probe.owned(name, worker)
                            if latest is None:
                                raise ObservationError("candidate_unit_disappeared")
                            if latest.unit.invocation != current.unit.invocation:
                                raise ObservationError("candidate_invocation_changed")
                            await probe.kill(name)
                            killed = True
                        await asyncio.sleep(min(limits.poll_interval, 0.05))
    except Exception:
        # Reporting unconfirmed is intentional; never turn failure to observe into empty.
        confirmed = False
    return confirmed


async def _cleanup(
    probe: CandidateProbe,
    name: str,
    worker: tuple[str, ...],
    spec: ProcessSpec,
    proof: CandidateObservation | None,
    launch: asyncio.Task[dict[str, Any]],
    persistence: ProcessPersistence | None = None,
) -> bool:
    """Own both manager subtree and CLI; cancellation cannot abandon either."""
    confirmed = False
    try:
        accepted = persistence.value.accepted if persistence is not None else None
        if accepted is None and proof is not None:
            accepted = AcceptedUnit.capture(proof.unit, proof.process)
        confirmed = await _settle_subtree(
            probe,
            name,
            worker,
            spec.limits,
            accepted,
            persistence.accept if persistence is not None else None,
        )
    finally:
        if not launch.done():
            launch.cancel()
        try:
            await launch
        except (ValueError, TimeoutError, asyncio.CancelledError):
            pass  # run_process reaps before these execution outcomes escape.
        except Exception:
            confirmed = False  # A client cleanup failure is not a reap receipt.
    return confirmed


async def _run_candidate_process(
    spec: ProcessSpec,
    *,
    probe: CandidateProbe | None = None,
    runner: CommandRunner = run_process,
    persist: PersistProcess | None = None,
) -> ProcessOutput:
    """Return only with a reaped CLI and confirmed-empty owned service subtree.

    A cancelled call propagates cancellation after cleanup. If cleanup is unknown,
    CandidateProcessError(cleanup_confirmed=False) takes precedence: retain paths.
    """
    _validate(spec)
    spec.observe()
    name = "muli-candidate-" + secrets.token_hex(16) + ".service"
    control = "process-" + secrets.token_hex(16) + ".json"
    gate = "muli-candidate-" + secrets.token_hex(16)
    nonce = secrets.token_hex(32)
    directory = _open_directory(spec.private_root)
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except BaseException:
        os.close(directory)
        raise
    launch: asyncio.Task[dict[str, Any]] | None = None
    proof: CandidateObservation | None = None
    confirmed = True
    result: dict[str, Any] | None = None
    original: BaseException | None = None
    persistence: ProcessPersistence | None = None
    try:
        _directory_check(spec.private_root, directory)
        payload = json.dumps(
            {
                "argv": spec.argv,
                "env": dict(spec.env),
                "cwd": str(spec.cwd),
                "deadline": spec.deadline,
                "parent": os.getpid(),
                "uid": os.geteuid(),
                "gate": gate,
                "nonce": nonce,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > spec.limits.metadata_bytes:
            raise CandidateProcessError("candidate_metadata_budget")
        fd = os.open(
            control,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as target:
            target.write(payload)
            if persist is not None:
                target.flush()
                os.fsync(target.fileno())
        if persist is not None:
            os.fsync(directory)
        listener.bind("\x00" + gate)
        listener.listen(8)
        listener.setblocking(False)
        worker = (
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).with_name("_candidate_worker.py")),
            str(spec.private_root / control),
        )
        runtime_us = max(1, int(_remaining(spec.deadline) * 1_000_000))
        observer = probe or CandidateProbe(
            limits=ProbeLimits(
                seconds=min(5.0, spec.limits.preflight_seconds / 4, spec.limits.stop_seconds / 4),
                command_bytes=spec.limits.output_bytes,
            )
        )
        _directory_check(spec.private_root, directory)
        arguments = _arguments(name, worker, spec, runtime_us)
        if persist is not None:
            persistence = ProcessPersistence.create(spec, name, worker, runtime_us, persist)
            persistence.value.check_files()
        timeout = _remaining(spec.deadline)

        async def run_client() -> dict[str, Any]:
            return await runner(
                arguments,
                timeout=timeout,
                limit=spec.limits.output_bytes,
                merge_stderr=False,
            )

        launch = asyncio.create_task(run_client())
        confirmed = False
        try:
            permit_deadline = min(spec.deadline, time.monotonic() + spec.limits.preflight_seconds)
            async with asyncio.timeout_at(permit_deadline):
                while not await observer.exists(name):
                    if launch.done():
                        launch.result()
                        raise CandidateProcessError("candidate_launch_failed")
                    await asyncio.sleep(spec.limits.poll_interval)
                while True:
                    try:
                        proof = await observer.waiting(name, worker, spec.limits, runtime_us)
                        break
                    except ObservationError as error:
                        if str(error) != "candidate_not_waiting":
                            raise
                        await asyncio.sleep(spec.limits.poll_interval)
                if persistence is not None:
                    persistence.accept(AcceptedUnit.capture(proof.unit, proof.process))
                    persistence.value.check_files()
                await _permit(
                    listener,
                    proof,
                    nonce,
                    probe=observer,
                    name=name,
                    worker=worker,
                    spec=spec,
                    runtime_us=runtime_us,
                    directory=directory,
                    permit_deadline=permit_deadline,
                )
            async with asyncio.timeout_at(spec.deadline):
                while not launch.done():
                    await asyncio.wait(
                        {launch}, timeout=min(spec.limits.poll_interval, _remaining(spec.deadline))
                    )
                    spec.observe()
                    _directory_check(spec.private_root, directory)
                    _remaining(spec.deadline)
                result = launch.result()
                _remaining(spec.deadline)
        except BaseException as error:
            original = error
        finally:
            listener.close()  # EOF can only refuse permission, never grant it.
            cleanup = asyncio.create_task(
                _cleanup(observer, name, worker, spec, proof, launch, persistence)
            )

            async def settle() -> None:
                nonlocal confirmed
                confirmed = await cleanup

            if await _wait_cleanup(asyncio.create_task(settle())):
                original = asyncio.CancelledError()
        if not confirmed:
            raise CandidateProcessError(
                "candidate_cleanup_unconfirmed", unit_name=name, cleanup_confirmed=False
            ) from None
        if persistence is not None:
            persistence.value.check_files()
            persistence.settle()
        if original is not None:
            if isinstance(original, asyncio.CancelledError):
                raise original
            code = (
                original.code
                if isinstance(original, CandidateError)
                else (
                    "candidate_process_timeout"
                    if isinstance(original, TimeoutError)
                    else "candidate_process_failed"
                )
            )
            raise CandidateProcessError(code, unit_name=name) from None
        if result is None or type(result.get("exit_code")) is not int or result.get("truncated"):
            raise CandidateProcessError("candidate_process_result", unit_name=name)
        stdout, stderr = result.get("output", ""), result.get("stderr", "")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise CandidateProcessError("candidate_process_result", unit_name=name)
        if len(stdout.encode()) + len(stderr.encode()) > spec.limits.output_bytes:
            raise CandidateProcessError("candidate_output_budget", unit_name=name)
        return ProcessOutput(result["exit_code"], stdout, stderr)
    finally:
        listener.close()
        # Keep metadata with the attempt when ownership could not be settled.
        try:
            if confirmed and (
                persistence is None
                or (persistence.value.phase == "settled" and persistence.acknowledged)
            ):
                try:
                    os.unlink(control, dir_fd=directory)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory)


async def run_candidate_process(
    spec: ProcessSpec,
    *,
    probe: CandidateProbe | None = None,
    runner: CommandRunner = run_process,
    persist: PersistProcess | None = None,
) -> ProcessOutput:
    """Expose fixed errors without environment, arguments or filesystem diagnostics."""
    try:
        return await _run_candidate_process(spec, probe=probe, runner=runner, persist=persist)
    except (CandidateProcessError, asyncio.CancelledError):
        raise
    except Exception:
        raise CandidateProcessError("candidate_process_unavailable") from None
