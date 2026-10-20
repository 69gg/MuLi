"""Single-step transaction driver with injected observations and effects.

There is deliberately no Linux service backend, process launcher, CLI or polling
loop here yet. Passing its fake-backend tests does not prove a service restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from muli.management_transaction import (
    ClockReading,
    Code,
    HealthProof,
    JournalControl,
    Role,
    TransactionError,
    TransactionRecord,
    Transition,
)


@dataclass(frozen=True, slots=True)
class ServiceObservation:
    identity_matches: bool
    configuration: str
    service: Literal["running", "stopped", "starting", "stopping", "foreign"]
    boot: str | None
    writer_free: bool
    drained: bool = False
    resumed: bool = False
    health: HealthProof | None = None
    health_failed: bool = False
    activation_failed: bool = False


class ControllerBackend(Protocol):
    """Every operation must check the expected installation/boot again.

    drain/resume/activate are same-generation idempotent host commands. stop is
    scoped to the observed owner; install requires an empty cgroup and exclusive
    writer lease plus raw-config CAS. start is issued once per persisted intent.
    These obligations still require real-backend tests in a later block.
    """

    async def observe(self, record: TransactionRecord) -> ServiceObservation: ...
    async def drain(self, record: TransactionRecord) -> None: ...
    async def resume(self, record: TransactionRecord) -> None: ...
    async def stop(self, record: TransactionRecord, role: Role) -> None: ...
    async def install(self, record: TransactionRecord, role: Role, data: bytes) -> None: ...
    async def start(self, record: TransactionRecord, role: Role) -> None: ...
    async def activate(self, record: TransactionRecord, role: Role) -> None: ...


T = TypeVar("T")


class _BackendFailure(Exception):
    def __init__(self, code: Code) -> None:
        self.code = code


class TransactionController:
    def __init__(
        self,
        control: JournalControl,
        backend: ControllerBackend,
        clock: Callable[[], ClockReading],
        *,
        operation_timeout: float = 5.0,
    ) -> None:
        if not 0 < operation_timeout <= 30:
            raise ValueError("invalid_operation_timeout")
        self.control, self.backend, self.clock = control, backend, clock
        self.operation_timeout = operation_timeout
        self._step_lock = asyncio.Lock()

    def _move(self, record: TransactionRecord, transition: Transition) -> TransactionRecord:
        return self.control.advance(record.position, transition)

    async def _call(self, record: TransactionRecord, action: Callable[[], Awaitable[T]]) -> T:
        remaining = record.deadline.remaining(self.clock())
        if record.phase in {"activating", "verifying"}:
            remaining -= record.deadline.rollback_reserve
        if remaining <= 0:
            raise _BackendFailure("deadline_exceeded")
        try:
            async with asyncio.timeout(min(self.operation_timeout, remaining)):
                return await action()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise _BackendFailure("deadline_exceeded") from None
        except Exception:
            raise _BackendFailure("backend_failed") from None

    def _failure(self, record: TransactionRecord, code: Code) -> TransactionRecord:
        if record.phase in {"committed", "rolled_back"}:
            if record.activation == "blocked":
                return record
            return self._move(
                record, Transition(phase=record.phase, activation="blocked", code=code)
            )
        if record.phase in {"activating", "verifying"} and code != "clock_invalid":
            return self._move(
                record, Transition(phase="rolling_back", intent="stop_target", code=code)
            )
        return self._move(record, Transition(phase="recovery_required", code=code))

    def _deadline_result(
        self, record: TransactionRecord, remaining: float
    ) -> TransactionRecord | None:
        if remaining <= 0:
            if record.phase in {"committed", "rolled_back"}:
                return self._failure(record, "deadline_exceeded")
            return self._move(
                record, Transition(phase="recovery_required", code="deadline_exceeded")
            )
        if remaining <= record.deadline.rollback_reserve and record.phase in {
            "activating",
            "verifying",
        }:
            return self._failure(record, "deadline_exceeded")
        return None

    async def step(self) -> TransactionRecord:
        """Do one bounded observation/effect step under an already-owned flock.

        Cancellation is propagated: the prior durable intent remains available
        to a new driver. No finally block retries an effect or writes success.
        """
        async with self._step_lock:
            self.control.check()
            record = self.control.read()
            if record is None:
                raise TransactionError("transaction_missing")
            if record.resolved or record.phase == "recovery_required":
                return record
            try:
                self.control.journal.snapshot(record, "base")
                self.control.journal.snapshot(record, "target")
            except (OSError, ValueError):
                # Never try to restore an unverifiable snapshot.
                if record.phase in {"committed", "rolled_back"}:
                    return self._failure(record, "snapshot_invalid")
                return self._move(
                    record, Transition(phase="recovery_required", code="snapshot_invalid")
                )
            try:
                remaining = record.deadline.remaining(self.clock())
                if failed := self._deadline_result(record, remaining):
                    return failed
                observation = await self._call(record, lambda: self.backend.observe(record))
                # Recheck after the await before accepting any observed proof.
                remaining = record.deadline.remaining(self.clock())
                if failed := self._deadline_result(record, remaining):
                    return failed
                if (
                    not observation.identity_matches
                    or observation.service == "foreign"
                    or observation.configuration not in {record.base.digest, record.target.digest}
                ):
                    if record.phase in {"committed", "rolled_back"}:
                        return self._failure(record, "foreign_state")
                    return self._move(
                        record, Transition(phase="recovery_required", code="foreign_state")
                    )
                return await self._step(record, observation, remaining)
            except asyncio.CancelledError:
                raise
            except TransactionError as exc:
                if exc.code == "clock_invalid":
                    current = self.control.read()
                    if current is None:
                        raise TransactionError("transaction_missing") from None
                    return self._failure(current, "clock_invalid")
                raise
            except _BackendFailure as exc:
                # Do not serialize exception text: it can contain credentials.
                current = self.control.read()
                if current is None:
                    raise TransactionError("transaction_missing") from None
                return self._failure(current, exc.code)

    async def _step(
        self,
        record: TransactionRecord,
        observation: ServiceObservation,
        remaining: float,
    ) -> TransactionRecord:
        if record.phase in {"prepared", "draining"} and (
            observation.configuration != record.base.digest
            or observation.service != "running"
            or observation.boot != record.identity.origin_boot
        ):
            return self._failure(record, "foreign_state")
        if record.phase == "prepared":
            if remaining <= record.deadline.rollback_reserve:
                if observation.resumed:
                    return self._move(record, Transition(phase="aborted", code="drain_timeout"))
                await self._call(record, lambda: self.backend.resume(record))
                return record
            return self._move(record, Transition(phase="draining"))
        if record.phase == "draining":
            if remaining <= record.deadline.rollback_reserve:
                return self._move(record, Transition(phase="prepared", code="drain_timeout"))
            if observation.drained:
                return self._move(record, Transition(phase="activating", intent="stop_base"))
            await self._call(record, lambda: self.backend.drain(record))
            return record
        if record.phase == "activating":
            if record.intent == "stop_base":
                if observation.service == "running":
                    if (
                        observation.boot != record.identity.origin_boot
                        or observation.configuration != record.base.digest
                    ):
                        return self._failure(record, "foreign_state")
                    await self._call(record, lambda: self.backend.stop(record, "base"))
                elif observation.service == "stopped" and observation.writer_free:
                    return self._move(
                        record, Transition(phase="activating", intent="install_target")
                    )
                return record
            if record.intent == "install_target":
                if observation.service != "stopped" or not observation.writer_free:
                    return self._failure(record, "foreign_state")
                if observation.configuration != record.target.digest:
                    data = self.control.journal.snapshot(record, "target")
                    await self._call(record, lambda: self.backend.install(record, "target", data))
                    return record
                started = self._move(record, Transition(phase="activating", intent="start_target"))
                await self._call(started, lambda: self.backend.start(started, "target"))
                return started
            if (
                observation.service == "running"
                and observation.boot != record.identity.origin_boot
                and observation.configuration == record.target.digest
            ):
                return self._move(record, Transition(phase="verifying"))
            if observation.service in {"starting", "stopping"}:
                return record
            # The start intent may have survived a crash before or after the
            # command. An inactive target is ambiguous, never an automatic retry.
            return self._failure(record, "ambiguous_start")
        if record.phase == "verifying":
            if (
                observation.configuration != record.target.digest
                or observation.health_failed
                or observation.service != "running"
                or observation.writer_free
            ):
                return self._failure(record, "health_failed")
            proof = observation.health
            if proof is not None:
                if proof.boot != observation.boot or not record.accepts(proof, "target"):
                    return self._failure(record, "health_failed")
                return self._move(record, Transition(phase="committed", proof=proof))
            return record
        if record.phase == "rolling_back":
            if record.intent == "stop_target":
                if observation.service == "running":
                    role: Role = (
                        "base" if observation.boot == record.identity.origin_boot else "target"
                    )
                    await self._call(record, lambda: self.backend.stop(record, role))
                elif observation.service == "stopped" and observation.writer_free:
                    return self._move(
                        record,
                        Transition(phase="rolling_back", intent="restore_base", code=record.code),
                    )
                return record
            if record.intent == "restore_base":
                if observation.service != "stopped" or not observation.writer_free:
                    return self._failure(record, "foreign_state")
                if observation.configuration != record.base.digest:
                    data = self.control.journal.snapshot(record, "base")
                    await self._call(record, lambda: self.backend.install(record, "base", data))
                    return record
                started = self._move(
                    record, Transition(phase="rolling_back", intent="start_base", code=record.code)
                )
                await self._call(started, lambda: self.backend.start(started, "base"))
                return started
            if record.intent == "start_base":
                if (
                    observation.service == "running"
                    and observation.boot != record.identity.origin_boot
                    and observation.configuration == record.base.digest
                ):
                    return self._move(
                        record,
                        Transition(phase="rolling_back", intent="verify_base", code=record.code),
                    )
                if observation.service in {"starting", "stopping"}:
                    return record
                return self._failure(record, "ambiguous_start")
            proof = observation.health
            if (
                observation.configuration != record.base.digest
                or observation.service != "running"
                or observation.writer_free
                or observation.health_failed
                or (
                    proof is not None
                    and (proof.boot != observation.boot or not record.accepts(proof, "base"))
                )
            ):
                return self._failure(record, "health_failed")
            if proof is not None:
                return self._move(
                    record, Transition(phase="rolled_back", proof=proof, code=record.code)
                )
            return record
        if record.phase in {"committed", "rolled_back"}:
            proof = observation.health
            role = "target" if record.phase == "committed" else "base"
            reference = record.target if role == "target" else record.base
            if (
                observation.configuration != reference.digest
                or observation.service != "running"
                or observation.writer_free
            ):
                return self._failure(record, "health_failed")
            if observation.activation_failed or observation.health_failed:
                return self._failure(
                    record,
                    "activation_failed" if observation.activation_failed else "health_failed",
                )
            if (
                proof is not None
                and record.accepts(proof, role, released=True)
                and proof.boot == observation.boot
            ):
                activation: Literal["released", "released_degraded"] = (
                    "released_degraded" if proof.admission == "released_degraded" else "released"
                )
                return self._move(
                    record,
                    Transition(
                        phase=record.phase, activation=activation, proof=proof, code=record.code
                    ),
                )
            if proof is None or proof.boot != observation.boot or not record.accepts(proof, role):
                return self._failure(record, "health_failed")
            if record.health is not None and proof.boot != record.health.boot:
                return self._move(
                    record, Transition(phase=record.phase, proof=proof, code=record.code)
                )
            if record.activation == "blocked":
                return record
            await self._call(record, lambda: self.backend.activate(record, role))
            return record
        raise TransactionError("unsupported_phase")
