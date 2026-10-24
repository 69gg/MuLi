"""Single-step transaction driver with injected observations and effects.

The Linux backend is a separate module. No process launcher, CLI or polling loop
lives here; passing fake-backend tests does not prove a service restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from muli.managed_transaction import (
    HealthProofV2,
    ProjectControllerTransferRecord,
    TransactionRecordV2,
)
from muli.management_transaction import (
    Activation,
    ClockReading,
    Code,
    HealthProof,
    Intent,
    JournalControl,
    Phase,
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


class _ServiceState(Protocol):
    @property
    def identity_matches(self) -> bool: ...
    @property
    def service(self) -> Literal["running", "stopped", "starting", "stopping", "foreign"]: ...
    @property
    def boot(self) -> str | None: ...
    @property
    def writer_free(self) -> bool: ...
    @property
    def drained(self) -> bool: ...
    @property
    def resumed(self) -> bool: ...
    @property
    def health_failed(self) -> bool: ...
    @property
    def activation_failed(self) -> bool: ...


class _ControllerEffects[RecordT, ObservationT](Protocol):
    async def observe(self, record: RecordT) -> ObservationT: ...
    async def drain(self, record: RecordT) -> None: ...
    async def resume(self, record: RecordT) -> None: ...
    async def stop(self, record: RecordT, role: Role) -> None: ...
    async def start(self, record: RecordT, role: Role) -> None: ...
    async def activate(self, record: RecordT, role: Role) -> None: ...


class _ControllerPolicy[RecordT, ObservationT, ProofT](Protocol):
    def read(self) -> RecordT | None: ...
    def snapshots(self, record: RecordT) -> None: ...
    def move(
        self,
        record: RecordT,
        *,
        phase: Phase,
        intent: Intent,
        activation: Activation,
        code: Code | None,
        proof: ProofT | None,
    ) -> RecordT: ...
    def allowed(self, record: RecordT, observation: ObservationT) -> bool: ...
    def restore_prefix_observed(self, record: RecordT, observation: ObservationT) -> bool: ...
    def matches(self, record: RecordT, observation: ObservationT, role: Role) -> bool: ...
    def installed(self, record: RecordT, observation: ObservationT, role: Role) -> bool: ...
    async def install(self, record: RecordT, role: Role) -> None: ...
    def proof(self, observation: ObservationT) -> ProofT | None: ...
    def accepts(
        self, record: RecordT, proof: ProofT, role: Role, *, released: bool = False
    ) -> bool: ...


class _Controller[
    RecordT: TransactionRecord | TransactionRecordV2 | ProjectControllerTransferRecord,
    ObservationT: _ServiceState,
    ProofT: HealthProof | HealthProofV2,
]:
    def __init__(
        self,
        control: JournalControl,
        backend: _ControllerEffects[RecordT, ObservationT],
        clock: Callable[[], ClockReading],
        policy: _ControllerPolicy[RecordT, ObservationT, ProofT],
        *,
        operation_timeout: float = 5.0,
    ) -> None:
        if not 0 < operation_timeout <= 30:
            raise ValueError("invalid_operation_timeout")
        self.control, self._effects, self.clock = control, backend, clock
        self._policy = policy
        self.operation_timeout = operation_timeout
        self._step_lock = asyncio.Lock()

    def _move(
        self,
        record: RecordT,
        *,
        phase: Phase,
        intent: Intent = "none",
        activation: Activation = "pending",
        code: Code | None = None,
        proof: ProofT | None = None,
    ) -> RecordT:
        return self._policy.move(
            record, phase=phase, intent=intent, activation=activation, code=code, proof=proof
        )

    async def _call(self, record: RecordT, action: Callable[[], Awaitable[T]]) -> T:
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
        except TransactionError as exc:
            if exc.code == "clock_invalid":
                raise
            raise _BackendFailure("backend_failed") from None
        except Exception:
            raise _BackendFailure("backend_failed") from None

    def _failure(self, record: RecordT, code: Code) -> RecordT:
        if record.phase in {"committed", "rolled_back"}:
            if record.activation == "blocked":
                return record
            return self._move(record, phase=record.phase, activation="blocked", code=code)
        if record.phase in {"activating", "verifying"} and code != "clock_invalid":
            return self._move(record, phase="rolling_back", intent="stop_target", code=code)
        return self._move(record, phase="recovery_required", code=code)

    def _deadline_result(self, record: RecordT, remaining: float) -> RecordT | None:
        if remaining <= 0:
            if record.phase in {"committed", "rolled_back"}:
                return self._failure(record, "deadline_exceeded")
            return self._move(record, phase="recovery_required", code="deadline_exceeded")
        if remaining <= record.deadline.rollback_reserve and record.phase in {
            "activating",
            "verifying",
        }:
            return self._failure(record, "deadline_exceeded")
        return None

    async def step(self) -> RecordT:
        """Do one bounded observation/effect step under an already-owned flock.

        Cancellation is propagated: the prior durable intent remains available
        to a new driver. No finally block retries an effect or writes success.
        """
        async with self._step_lock:
            self.control.check()
            record = self._policy.read()
            if record is None:
                raise TransactionError("transaction_missing")
            if record.resolved or record.phase == "recovery_required":
                return record
            try:
                self._policy.snapshots(record)
            except (OSError, ValueError):
                # Never try to restore an unverifiable snapshot.
                if record.phase in {"committed", "rolled_back"}:
                    return self._failure(record, "snapshot_invalid")
                return self._move(record, phase="recovery_required", code="snapshot_invalid")
            try:
                remaining = record.deadline.remaining(self.clock())
                if failed := self._deadline_result(record, remaining):
                    return failed
                observation = await self._call(record, lambda: self._effects.observe(record))
                # Recheck after the await before accepting any observed proof.
                remaining = record.deadline.remaining(self.clock())
                if failed := self._deadline_result(record, remaining):
                    return failed
                if observation.identity_matches and self._policy.restore_prefix_observed(
                    record, observation
                ):
                    # A failed multi-file install may leave a known prefix.
                    # Only advance its durable restore intent; this is neither
                    # acceptance as healthy nor permission for a file effect.
                    return self._move(
                        record, phase="rolling_back", intent="restore_base", code=record.code
                    )
                if (
                    not observation.identity_matches
                    or observation.service == "foreign"
                    or not self._policy.allowed(record, observation)
                ):
                    if record.phase in {"committed", "rolled_back"}:
                        return self._failure(record, "foreign_state")
                    return self._move(record, phase="recovery_required", code="foreign_state")
                return await self._step(record, observation, remaining)
            except asyncio.CancelledError:
                raise
            except TransactionError as exc:
                if exc.code == "clock_invalid":
                    current = self._policy.read()
                    if current is None:
                        raise TransactionError("transaction_missing") from None
                    return self._failure(current, "clock_invalid")
                raise
            except _BackendFailure as exc:
                # Do not serialize exception text: it can contain credentials.
                current = self._policy.read()
                if current is None:
                    raise TransactionError("transaction_missing") from None
                if (
                    exc.code == "deadline_exceeded"
                    and current.phase in {"committed", "rolled_back"}
                    and current.generation == record.generation
                    and not current.resolved
                ):
                    try:
                        remaining = current.deadline.remaining(self.clock())
                    except TransactionError as clock_error:
                        if clock_error.code != "clock_invalid":
                            raise
                        return self._failure(current, "clock_invalid")
                    if remaining > 0:
                        # A short observation/HTTP budget expiring does not
                        # revoke the durable decision or extend its deadline.
                        return current
                return self._failure(current, exc.code)

    async def _step(
        self,
        record: RecordT,
        observation: ObservationT,
        remaining: float,
    ) -> RecordT:
        if record.phase in {"prepared", "draining"} and (
            not self._policy.matches(record, observation, "base")
            or observation.service != "running"
            or observation.boot != record.identity.origin_boot
        ):
            return self._failure(record, "foreign_state")
        if record.phase == "prepared":
            if remaining <= record.deadline.rollback_reserve:
                if observation.resumed:
                    return self._move(record, phase="aborted", code="drain_timeout")
                await self._call(record, lambda: self._effects.resume(record))
                return record
            return self._move(record, phase="draining")
        if record.phase == "draining":
            if remaining <= record.deadline.rollback_reserve:
                return self._move(record, phase="prepared", code="drain_timeout")
            if observation.drained:
                return self._move(record, phase="activating", intent="stop_base")
            await self._call(record, lambda: self._effects.drain(record))
            return record
        if record.phase == "activating":
            if record.intent == "stop_base":
                if observation.service == "running":
                    if observation.boot != record.identity.origin_boot or not self._policy.matches(
                        record, observation, "base"
                    ):
                        return self._failure(record, "foreign_state")
                    await self._call(record, lambda: self._effects.stop(record, "base"))
                elif observation.service == "stopped" and observation.writer_free:
                    return self._move(record, phase="activating", intent="install_target")
                return record
            if record.intent == "install_target":
                if observation.service != "stopped" or not observation.writer_free:
                    return self._failure(record, "foreign_state")
                if not self._policy.installed(record, observation, "target"):
                    await self._call(record, lambda: self._policy.install(record, "target"))
                    return record
                started = self._move(record, phase="activating", intent="start_target")
                await self._call(started, lambda: self._effects.start(started, "target"))
                return started
            if (
                observation.service == "running"
                and observation.boot != record.identity.origin_boot
                and self._policy.matches(record, observation, "target")
            ):
                return self._move(record, phase="verifying")
            if observation.service in {"starting", "stopping"}:
                return record
            # The start intent may have survived a crash before or after the
            # command. An inactive target is ambiguous, never an automatic retry.
            return self._failure(record, "ambiguous_start")
        if record.phase == "verifying":
            if (
                not self._policy.matches(record, observation, "target")
                or observation.health_failed
                or observation.service != "running"
                or observation.writer_free
            ):
                return self._failure(record, "health_failed")
            proof = self._policy.proof(observation)
            if proof is not None:
                if proof.boot != observation.boot or not self._policy.accepts(
                    record, proof, "target"
                ):
                    return self._failure(record, "health_failed")
                return self._move(record, phase="committed", proof=proof)
            return record
        if record.phase == "rolling_back":
            if record.intent == "stop_target":
                if observation.service == "running":
                    role: Role = (
                        "base" if observation.boot == record.identity.origin_boot else "target"
                    )
                    await self._call(record, lambda: self._effects.stop(record, role))
                elif observation.service == "stopped" and observation.writer_free:
                    return self._move(
                        record,
                        phase="rolling_back",
                        intent="restore_base",
                        code=record.code,
                    )
                return record
            if record.intent == "restore_base":
                if observation.service != "stopped" or not observation.writer_free:
                    return self._failure(record, "foreign_state")
                if not self._policy.installed(record, observation, "base"):
                    await self._call(record, lambda: self._policy.install(record, "base"))
                    return record
                started = self._move(
                    record, phase="rolling_back", intent="start_base", code=record.code
                )
                await self._call(started, lambda: self._effects.start(started, "base"))
                return started
            if record.intent == "start_base":
                if (
                    observation.service == "running"
                    and observation.boot != record.identity.origin_boot
                    and self._policy.matches(record, observation, "base")
                ):
                    return self._move(
                        record,
                        phase="rolling_back",
                        intent="verify_base",
                        code=record.code,
                    )
                if observation.service in {"starting", "stopping"}:
                    return record
                return self._failure(record, "ambiguous_start")
            proof = self._policy.proof(observation)
            if (
                not self._policy.matches(record, observation, "base")
                or observation.service != "running"
                or observation.writer_free
                or observation.health_failed
                or (
                    proof is not None
                    and (
                        proof.boot != observation.boot
                        or not self._policy.accepts(record, proof, "base")
                    )
                )
            ):
                return self._failure(record, "health_failed")
            if proof is not None:
                return self._move(record, phase="rolled_back", proof=proof, code=record.code)
            return record
        if record.phase in {"committed", "rolled_back"}:
            proof = self._policy.proof(observation)
            role = "target" if record.phase == "committed" else "base"
            if (
                not self._policy.matches(record, observation, role)
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
                and self._policy.accepts(record, proof, role, released=True)
                and proof.boot == observation.boot
            ):
                activation: Literal["released", "released_degraded"] = (
                    "released_degraded" if proof.admission == "released_degraded" else "released"
                )
                return self._move(
                    record,
                    phase=record.phase,
                    activation=activation,
                    proof=proof,
                    code=record.code,
                )
            if proof is None:
                # A missing HTTP observation is not an explicit host failure.
                # Keep the original deadline and do not resend activation.
                return record
            if proof.boot != observation.boot or not self._policy.accepts(record, proof, role):
                return self._failure(record, "health_failed")
            if record.health is not None and proof.boot != record.health.boot:
                return self._move(record, phase=record.phase, proof=proof, code=record.code)
            if record.activation == "blocked":
                return record
            await self._call(record, lambda: self._effects.activate(record, role))
            return record
        raise TransactionError("unsupported_phase")


class _V1Policy:
    def __init__(self, control: JournalControl, backend: ControllerBackend) -> None:
        self.control, self.backend = control, backend

    def read(self) -> TransactionRecord | None:
        return self.control.read()

    def snapshots(self, record: TransactionRecord) -> None:
        self.control.journal.snapshot(record, "base")
        self.control.journal.snapshot(record, "target")

    def move(
        self,
        record: TransactionRecord,
        *,
        phase: Phase,
        intent: Intent,
        activation: Activation,
        code: Code | None,
        proof: HealthProof | None,
    ) -> TransactionRecord:
        return self.control.advance(
            record.position,
            Transition(phase=phase, intent=intent, activation=activation, code=code, proof=proof),
        )

    def allowed(self, record: TransactionRecord, observation: ServiceObservation) -> bool:
        return observation.configuration in {record.base.digest, record.target.digest}

    def restore_prefix_observed(
        self, record: TransactionRecord, observation: ServiceObservation
    ) -> bool:
        return False

    def matches(
        self, record: TransactionRecord, observation: ServiceObservation, role: Role
    ) -> bool:
        return (
            observation.configuration == (record.base if role == "base" else record.target).digest
        )

    def installed(
        self, record: TransactionRecord, observation: ServiceObservation, role: Role
    ) -> bool:
        return self.matches(record, observation, role)

    async def install(self, record: TransactionRecord, role: Role) -> None:
        await self.backend.install(record, role, self.control.journal.snapshot(record, role))

    def proof(self, observation: ServiceObservation) -> HealthProof | None:
        return observation.health

    def accepts(
        self, record: TransactionRecord, proof: HealthProof, role: Role, *, released: bool = False
    ) -> bool:
        return record.accepts(proof, role, released=released)


class TransactionController(_Controller[TransactionRecord, ServiceObservation, HealthProof]):
    """The protocol 1 public API; protocol-specific checks stay in its adapter."""

    def __init__(
        self,
        control: JournalControl,
        backend: ControllerBackend,
        clock: Callable[[], ClockReading],
        *,
        operation_timeout: float = 5.0,
    ) -> None:
        self.backend = backend
        super().__init__(
            control,
            backend,
            clock,
            _V1Policy(control, backend),
            operation_timeout=operation_timeout,
        )
