"""Project only fixed probe stages after the existing lease owner settles."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, cast

from muli.tools.ax_lease import MAX_STDERR_BYTES, AXHelperLease, AXLeaseError

STAGES = (
    "script_entered",
    "utility_importing",
    "utility_ready",
    "input_waiting",
    "input_line_received",
    "request_decoded",
    "probe_received",
    "watch_starting",
    "watch_ready",
    "uia_initializing",
    "uia_host_validated",
    "uia_add_type_returned",
    "uia_ready",
    "walker_initializing",
    "walker_ready",
    "probe_process_ready",
    "probe_encoded",
    "probe_write_returned",
)
_STAGE_LINES = {b"muli-uia-stage=1:" + stage.encode("ascii"): stage for stage in STAGES}
_NOTE_PREFIX = "uia_probe_diagnostic="
_NOTE_BYTES = 4096
_PROBE_PHASES = ("spawn", "send", "response", "cleanup")
_MAX_MILLISECONDS = 2**31 - 1

PENDING_STAGES = ("script_entered", "input_received")
_PENDING_LINES = {b"muli-uia-pending=1:" + stage.encode("ascii"): stage for stage in PENDING_STAGES}
PENDING_SCRIPT = (
    "try { [Console]::Error.WriteLine('muli-uia-pending=1:script_entered'); "
    "[Console]::Error.Flush() } catch { }\n"
    "$null = [Console]::ReadLine()\n"
    "try { [Console]::Error.WriteLine('muli-uia-pending=1:input_received'); "
    "[Console]::Error.Flush() } catch { }\n"
    "[Threading.Thread]::Sleep(30000)\n"
)
_PENDING_NOTE_PREFIX = "uia_pending_diagnostic="


def _stage_facts(
    data: bytes, names: tuple[str, ...], lines: dict[bytes, str]
) -> tuple[list[str], str]:
    stages: list[str] = []
    invalid = False
    previous = -1
    for line in data.split(b"\n")[:-1]:
        stage = lines.get(line.removesuffix(b"\r"))
        if stage is None:
            continue
        position = names.index(stage)
        if position <= previous or stage in stages:
            invalid = True
        if stage not in stages:
            stages.append(stage)
        previous = position
    return stages, "invalid" if invalid else "valid" if stages else "unobserved"


def _stderr_prefix(lease: AXHelperLease) -> bytes:
    if isinstance(lease, ProbeHelperLease) and lease._observed_stderr_attached:
        return bytes(lease._observed_stderr)
    return bytes(lease._stderr[:MAX_STDERR_BYTES])


def probe_facts(lease: AXHelperLease, *, cleanup_failed: bool) -> dict[str, Any]:
    data = _stderr_prefix(lease)
    stages, sequence = _stage_facts(data, STAGES, _STAGE_LINES)

    process = lease._process
    exit_code = process.returncode if process is not None else None
    exit_class = "unavailable" if process is None else "running"
    if type(exit_code) is int:
        exit_class = "zero" if exit_code == 0 else "nonzero"
    readers_settled = all(reader.done() for reader in lease._readers)
    cleanup = (
        not cleanup_failed
        and lease.closed
        and (process is None or type(exit_code) is int)
        and readers_settled
    )
    facts = {
        "stages": stages,
        "stage_sequence": sequence,
        "stderr_prefix_capped": len(data) == MAX_STDERR_BYTES,
        "sent": lease._count
        if type(lease._count) is int and lease._count in {0, 1}
        else "unavailable",
        "lease_closed": lease.closed,
        "process_exists": process is not None,
        "exit": exit_class,
        "readers_settled": readers_settled,
        "cleanup": "confirmed" if cleanup else "unconfirmed",
    }
    if isinstance(lease, PendingHelperLease):
        facts["before_cleanup"] = lease.before_cleanup
        facts["after_cleanup"] = lease._snapshot()
    return facts


def _add_probe_note(
    error: BaseException,
    lease: AXHelperLease,
    *,
    cleanup_failed: bool,
    command: Literal["probe", "targets"] = "probe",
) -> None:
    try:
        facts = probe_facts(lease, cleanup_failed=cleanup_failed)
        if command == "targets":
            facts["command"] = "targets"
        encoded = _NOTE_PREFIX + json.dumps(
            facts, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode("ascii")) <= _NOTE_BYTES:
            BaseException.add_note(error, encoded)
    except Exception:
        pass  # Evidence must never replace the original failure.


async def observe_probe(lease: AXHelperLease) -> dict[str, Any]:
    """Keep the original error/outcome, including cancellation and sticky close."""
    failure: BaseException | None = None
    cleanup_failed = False
    try:
        result: dict[str, Any] = await lease.request("probe", {})
        return result
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            await lease.aclose()
        except BaseException:
            cleanup_failed = True
            if failure is None:
                raise
        if failure is not None:
            _add_probe_note(failure, lease, cleanup_failed=cleanup_failed)


class _PendingInput:
    """Observe the existing stdin operations; never add a write, drain or wait."""

    def __init__(self, writer: asyncio.StreamWriter, lease: PendingHelperLease) -> None:
        self.writer, self.lease = writer, lease

    def write(self, data: bytes) -> None:
        self.lease.write_started = True
        self.writer.write(data)
        self.lease.write_returned = True

    async def drain(self) -> None:
        self.lease.drain_started = True
        await self.writer.drain()
        self.lease.drain_returned = True
        self.lease._input_drained()

    def close(self) -> None:
        self.writer.close()

    async def wait_closed(self) -> None:
        await self.writer.wait_closed()


class PendingHelperLease(AXHelperLease):
    """Test-only observation of the fourth native case's single owned helper."""

    def __init__(
        self,
        argv: list[str],
        *,
        timeout: float,
        ttl: float,
        max_nodes: int,
        once: bool = False,
    ) -> None:
        super().__init__(argv, timeout=timeout, ttl=ttl, max_nodes=max_nodes, once=once)
        self.input_observed = False
        self.write_started = self.write_returned = False
        self.drain_started = self.drain_returned = False
        self.before_cleanup: dict[str, Any] | None = None

    async def _start(self) -> None:
        await super()._start()
        if (
            not self.input_observed
            and self._process is not None
            and self._process.stdin is not None
        ):
            self._process.stdin = cast(
                asyncio.StreamWriter, _PendingInput(self._process.stdin, self)
            )
            self.input_observed = True

    def _input_drained(self) -> None:
        pass

    def _snapshot(self) -> dict[str, Any]:
        return pending_snapshot(self)

    def _retire(self, error: AXLeaseError | None = None) -> None:
        if self._state not in {"closing", "closed"}:
            try:
                self.before_cleanup = self._snapshot()
            except Exception:
                pass  # First retirement and original error still belong to the base owner.
        super()._retire(error)


def pending_snapshot(lease: PendingHelperLease) -> dict[str, Any]:
    data = _stderr_prefix(lease)
    stages, sequence = _stage_facts(data, PENDING_STAGES, _PENDING_LINES)
    starting = lease._starting
    spawn = "not_started"
    if starting is not None:
        if not starting.done():
            spawn = "pending"
        elif starting.cancelled():
            spawn = "cancelled"
        else:
            spawn = "failed" if starting.exception() is not None else "returned"
    process = lease._process
    exit_code = process.returncode if process is not None else None
    exit_class = "unavailable" if process is None else "running"
    if type(exit_code) is int:
        exit_class = "zero" if exit_code == 0 else "nonzero"
    return {
        "spawn": spawn,
        "lease_state": lease.state
        if lease.state in {"new", "starting", "in_flight", "ready", "closing", "closed"}
        else "unavailable",
        "process_exists": process is not None,
        "exit": exit_class,
        "readers_settled": all(reader.done() for reader in lease._readers),
        "sent": lease._count
        if type(lease._count) is int and lease._count in {0, 1}
        else "unavailable",
        "input_observed": lease.input_observed,
        "write_started": lease.write_started,
        "write_returned": lease.write_returned,
        "drain_started": lease.drain_started,
        "drain_returned": lease.drain_returned,
        "script_stages": stages,
        "stage_sequence": sequence,
        "stderr_bytes": len(data),
        "stderr_prefix_capped": len(data) == MAX_STDERR_BYTES,
    }


class _ProbeStderr:
    """Delegate the original pipe read; retain only arrival times of fixed lines."""

    def __init__(self, stream: asyncio.StreamReader, lease: ProbeHelperLease) -> None:
        self.stream, self.lease = stream, lease

    async def read(self, size: int) -> bytes:
        chunk = await self.stream.read(size)
        self.lease._record_stage_arrivals(chunk)
        return chunk


class ProbeHelperLease(PendingHelperLease):
    """Observe the first real probe through the same owned lease operations."""

    def __init__(
        self,
        argv: list[str],
        *,
        timeout: float,
        ttl: float,
        max_nodes: int,
        once: bool = True,
    ) -> None:
        super().__init__(argv, timeout=timeout, ttl=ttl, max_nodes=max_nodes, once=once)
        self._observed_started: float | None = None
        self._observed_ended: float | None = None
        self._observed_phase = "not_started"
        self._observed_phase_started = 0.0
        self._observed_durations = dict.fromkeys(_PROBE_PHASES, 0.0)
        self._stage_arrival_ms: dict[str, int] = {}
        self._observed_stderr = bytearray()
        self._observed_stderr_attached = False
        self._observed_stderr_failed = False
        self._observed_stderr_eof = False

    def _observe_stderr_reads(self, stream: asyncio.StreamReader) -> None:
        """Observe existing reads, including cleanup's discard of this same stream."""
        if self._observed_stderr_attached:
            return
        try:
            original_read = stream.read

            async def observed_read(n: int = -1) -> bytes:
                chunk = await original_read(n)
                try:
                    remaining = max(0, MAX_STDERR_BYTES - len(self._observed_stderr))
                    self._observed_stderr.extend(chunk[:remaining])
                    if n != 0 and not chunk:
                        self._observed_stderr_eof = True
                except Exception:
                    self._observed_stderr_failed = True
                return chunk

            # Keep Process.stderr, its transport and at_eof() unchanged. Both
            # the lease reader and existing cleanup discard call this method.
            stream.read = observed_read  # type: ignore[method-assign]
            self._observed_stderr_attached = True
        except Exception:
            self._observed_stderr_failed = True

    def _observation_time(self) -> float:
        return self._loop.time()

    def _observe(self, phase: str) -> None:
        try:
            now = self._observation_time()
            if self._observed_started is None:
                self._observed_started = now
            elif self._observed_phase in self._observed_durations:
                self._observed_durations[self._observed_phase] += max(
                    0.0, now - self._observed_phase_started
                )
            self._observed_phase, self._observed_phase_started = phase, now
            if phase == "settled":
                self._observed_ended = now
        except Exception:
            pass  # Observation must never change the original lease operation.

    async def _start(self) -> None:
        self._observe("spawn")
        await super()._start()
        self._observe("send")

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        self._observe_stderr_reads(stream)
        await super()._read_stderr(cast(asyncio.StreamReader, _ProbeStderr(stream, self)))

    def _record_stage_arrivals(self, chunk: bytes) -> None:
        try:
            if not chunk or self._observed_started is None:
                return
            room = max(0, MAX_STDERR_BYTES - len(self._stderr))
            if room == 0:
                return
            data = bytes(self._stderr[:MAX_STDERR_BYTES]) + chunk[:room]
            stages, _ = _stage_facts(data, STAGES, _STAGE_LINES)
            pending = [stage for stage in stages if stage not in self._stage_arrival_ms]
            if not pending:
                return
            elapsed = self._observation_time() - self._observed_started
            if not math.isfinite(elapsed) or elapsed < 0:
                return
            milliseconds = _milliseconds(elapsed)
            for stage in pending:
                self._stage_arrival_ms[stage] = milliseconds
        except Exception:
            pass  # Pipe ownership, byte charging and the original error remain unchanged.

    def _input_drained(self) -> None:
        self._observe("response")

    def _retire(self, error: AXLeaseError | None = None) -> None:
        first = self._state not in {"closing", "closed"}
        super()._retire(error)
        if first:
            self._observe("cleanup")

    async def _finish(self) -> None:
        try:
            await super()._finish()
        finally:
            self._observe("settled")

    def _snapshot(self) -> dict[str, Any]:
        facts = pending_snapshot(self)
        data = _stderr_prefix(self)
        stages, sequence = _stage_facts(data, STAGES, _STAGE_LINES)
        now = self._observation_time() if self._observed_ended is None else self._observed_ended
        durations = self._observed_durations.copy()
        if self._observed_phase in durations:
            durations[self._observed_phase] += max(0.0, now - self._observed_phase_started)
        elapsed = 0.0 if self._observed_started is None else now - self._observed_started
        facts.update(
            script_stages=stages,
            stage_sequence=sequence,
            stderr_kind="empty" if not data else "known_stage" if stages else "unrecognized",
            stderr_observation="attached"
            if self._observed_stderr_attached and not self._observed_stderr_failed
            else "unavailable",
            stderr_eof_observed=self._observed_stderr_eof
            if self._observed_stderr_attached and not self._observed_stderr_failed
            else None,
            phase=self._observed_phase,
            elapsed_ms=_milliseconds(elapsed),
            phase_ms={phase: _milliseconds(value) for phase, value in durations.items()},
            stage_arrival_ms={
                stage: self._stage_arrival_ms[stage]
                for stage in stages
                if stage in self._stage_arrival_ms
            },
        )
        return facts


def _milliseconds(seconds: float) -> int:
    return int(min(_MAX_MILLISECONDS, max(0.0, seconds) * 1000))


def pending_received(lease: PendingHelperLease) -> bool:
    """The pre-read marker is diagnostic only, not an extra readiness condition."""
    stages, _ = _stage_facts(
        bytes(lease._stderr[:MAX_STDERR_BYTES]), PENDING_STAGES, _PENDING_LINES
    )
    return "input_received" in stages


def pending_facts(lease: PendingHelperLease, *, cleanup_failed: bool) -> dict[str, Any]:
    after = pending_snapshot(lease)
    cleanup = (
        not cleanup_failed
        and lease.closed
        and after["process_exists"]
        and after["exit"] in {"zero", "nonzero"}
        and after["readers_settled"]
    )
    return {
        "before_cleanup": lease.before_cleanup,
        "after_cleanup": after,
        "cleanup": "confirmed" if cleanup else "unconfirmed",
    }


@asynccontextmanager
async def observe_pending(
    lease: PendingHelperLease,
) -> AsyncIterator[asyncio.Task[dict[str, Any]]]:
    """Keep the original test fault/cancellation while the same lease owns cleanup."""
    reading = asyncio.create_task(lease.request("probe", {}))
    failure: BaseException | None = None
    cleanup_failed = False
    try:
        yield reading
    except BaseException as error:
        failure = error
        raise
    finally:
        reading.cancel()
        try:
            await asyncio.gather(reading, return_exceptions=True)
            await lease.aclose()
        except BaseException:
            cleanup_failed = True
            if failure is None:
                raise
        if failure is not None:
            try:
                encoded = _PENDING_NOTE_PREFIX + json.dumps(
                    pending_facts(lease, cleanup_failed=cleanup_failed),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if len(encoded.encode("ascii")) <= _NOTE_BYTES:
                    BaseException.add_note(failure, encoded)
            except Exception:
                pass  # Observation must never replace the original fault or cancellation.
