"""Protocol 2 observations and policy for the shared single-step controller.

Backend evidence is not self-authenticating. A real backend must bind fresh
process/unit observations, the writer lease, durable file confirmation and this
exact journal revision before constructing a BindingConfirmation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from muli.managed_journal import RoleSnapshots, read_snapshots
from muli.managed_transaction import (
    HealthProofV2,
    ProjectControllerTransferRecord,
    RoleBinding,
    TransactionRecordV2,
    TransitionV2,
    advance_record_v2,
    advance_transfer_record,
)
from muli.management_controller import _Controller, _ControllerEffects
from muli.management_transaction import (
    Activation,
    ClockReading,
    Code,
    Intent,
    JournalControl,
    Phase,
    Revision,
    Role,
    TransactionError,
)


@dataclass(frozen=True, slots=True)
class ManagedProcessIdentity:
    entry: Literal["legacy", "managed"]
    installation: str
    compatibility: str


@dataclass(frozen=True, slots=True)
class BindingConfirmation:
    """Fresh backend attestation, never a durable authorization token.

    Under the actual writer lease and empty cgroup, target installation confirms
    configuration, selector (including proven absence), then unit. Base restore
    changes and confirms those files in reverse order. It
    reloads when needed and compares fresh loaded unit properties to the unit
    referenced by ``loaded_unit``. That field is the attested raw unit digest,
    not a claim that systemd exposes the original file bytes. start() must
    independently repeat these guards immediately before its effect.
    """

    position: Revision
    role: Role
    configuration: str
    selector: str | None
    unit: str
    loaded_unit: str


@dataclass(frozen=True, slots=True)
class ManagedServiceObservation:
    identity_matches: bool
    configuration: str
    selector: str | None
    unit: str
    service: Literal["running", "stopped", "starting", "stopping", "foreign"]
    boot: str | None
    writer_free: bool
    process: ManagedProcessIdentity | None = None
    confirmation: BindingConfirmation | None = None
    drained: bool = False
    resumed: bool = False
    health: HealthProofV2 | None = None
    health_failed: bool = False
    activation_failed: bool = False


class ManagedControllerBackend(
    _ControllerEffects[TransactionRecordV2, ManagedServiceObservation], Protocol
):
    """Actual slot/probe/CAS obligations remain with the backend.

    install() only reconciles the current generation's allowed file prefix.
    Reopening already-desired files still confirms their data/parent durability;
    bytes alone do not establish completion. No start is repeated for a durable
    start intent. observe() must freshly establish a confirmation on reentry.
    """

    async def install(
        self, record: TransactionRecordV2, role: Role, data: RoleSnapshots
    ) -> None: ...


def _files(binding: RoleBinding) -> tuple[str, str | None, str]:
    return (
        binding.configuration.digest,
        None if binding.selector is None else binding.selector.digest,
        binding.unit.digest,
    )


def _process(binding: RoleBinding) -> ManagedProcessIdentity:
    return ManagedProcessIdentity(binding.entry, binding.installation, binding.compatibility)


def _prefixes(
    record: TransactionRecordV2 | ProjectControllerTransferRecord,
) -> set[tuple[str, str | None, str]]:
    if isinstance(record, ProjectControllerTransferRecord):
        origin, target = _files(record.origin.binding), _files(record.target)
        forward = {origin, (origin[0], target[1], origin[2]), target}
        if record.phase == "rolling_back":
            return forward | {(origin[0], origin[1], target[2])}
        return forward
    base, target = _files(record.base), _files(record.target)
    return {
        base,
        (target[0], base[1], base[2]),
        (target[0], target[1], base[2]),
        target,
    }


class _ManagedPolicy:
    def __init__(self, control: JournalControl, backend: ManagedControllerBackend) -> None:
        self.control, self.backend = control, backend

    def read(self) -> TransactionRecordV2 | None:
        record = self.control.read_current()
        if record is not None and not isinstance(record, TransactionRecordV2):
            raise TransactionError("managed_transaction_required")
        return record

    def snapshots(self, record: TransactionRecordV2) -> None:
        read_snapshots(self.control.journal, record, "base")
        read_snapshots(self.control.journal, record, "target")

    def move(
        self,
        record: TransactionRecordV2,
        *,
        phase: Phase,
        intent: Intent,
        activation: Activation,
        code: Code | None,
        proof: HealthProofV2 | None,
    ) -> TransactionRecordV2:
        # The ordinary driver must reject a changed action before any write;
        # accepting the Journal's new Transfer branch here would grant effects.
        current = self.read()
        if current is None:
            raise TransactionError("managed_transaction_required")
        return self.control._write(
            advance_record_v2(
                current,
                record.position,
                TransitionV2(
                    phase=phase, intent=intent, activation=activation, code=code, proof=proof
                ),
            )
        )

    def allowed(self, record: TransactionRecordV2, observation: ManagedServiceObservation) -> bool:
        actual = observation.configuration, observation.selector, observation.unit
        bindings = record.base, record.target
        complete = tuple(binding for binding in bindings if actual == _files(binding))
        if observation.service == "running":
            return any(observation.process == _process(binding) for binding in complete)
        if observation.service in {"starting", "stopping"}:
            return bool(complete) and (
                observation.process is None
                or any(observation.process == _process(binding) for binding in complete)
            )
        if observation.service != "stopped" or observation.process is not None:
            return False
        if complete:
            return True
        if not observation.writer_free or (record.phase, record.intent) not in {
            ("activating", "install_target"),
            ("rolling_back", "restore_base"),
        }:
            return False
        # Forward installation changes config -> selector -> unit. Restoration
        # traverses exactly the same frozen prefixes in reverse order.
        return actual in _prefixes(record)

    def restore_prefix_observed(
        self, record: TransactionRecordV2, observation: ManagedServiceObservation
    ) -> bool:
        # No effect is allowed in this window. A prior install failure can
        # leave a valid prefix while the durable rollback first says stop.
        return (
            (record.phase, record.intent) == ("rolling_back", "stop_target")
            and observation.service == "stopped"
            and observation.writer_free
            and observation.process is None
            and (observation.configuration, observation.selector, observation.unit)
            in _prefixes(record)
        )

    def matches(
        self, record: TransactionRecordV2, observation: ManagedServiceObservation, role: Role
    ) -> bool:
        binding = record.binding(role)
        if (observation.configuration, observation.selector, observation.unit) != _files(binding):
            return False
        if observation.service == "stopped":
            return observation.process is None
        return observation.process == _process(binding)

    def installed(
        self, record: TransactionRecordV2, observation: ManagedServiceObservation, role: Role
    ) -> bool:
        if (
            observation.service != "stopped"
            or not observation.writer_free
            or not self.matches(record, observation, role)
        ):
            return False
        configuration, selector, unit = _files(record.binding(role))
        return observation.confirmation == BindingConfirmation(
            position=record.position,
            role=role,
            configuration=configuration,
            selector=selector,
            unit=unit,
            loaded_unit=unit,
        )

    async def install(self, record: TransactionRecordV2, role: Role) -> None:
        await self.backend.install(record, role, read_snapshots(self.control.journal, record, role))

    def proof(self, observation: ManagedServiceObservation) -> HealthProofV2 | None:
        return observation.health

    def accepts(
        self,
        record: TransactionRecordV2,
        proof: HealthProofV2,
        role: Role,
        *,
        released: bool = False,
    ) -> bool:
        return record.accepts(proof, role, released=released)


class ManagedTransactionController(
    _Controller[TransactionRecordV2, ManagedServiceObservation, HealthProofV2]
):
    def __init__(
        self,
        control: JournalControl,
        backend: ManagedControllerBackend,
        clock: Callable[[], ClockReading],
        *,
        operation_timeout: float = 5.0,
    ) -> None:
        self.backend = backend
        super().__init__(
            control,
            backend,
            clock,
            _ManagedPolicy(control, backend),
            operation_timeout=operation_timeout,
        )


class TransferControllerBackend(
    _ControllerEffects[ProjectControllerTransferRecord, ManagedServiceObservation], Protocol
):
    async def install(
        self, record: ProjectControllerTransferRecord, role: Role, data: RoleSnapshots
    ) -> None: ...


def transfer_identity_matches(
    original: ProjectControllerTransferRecord, current: ProjectControllerTransferRecord
) -> bool:
    mutable = {
        "revision",
        "phase",
        "intent",
        "health",
        "activation",
        "code",
        "failure_code",
        "rollback_challenge",
        "rollback_attempted",
    }
    return (
        original.model_dump(exclude=mutable) == current.model_dump(exclude=mutable)
        and current.revision >= original.revision
        and (not original.rollback_attempted or current.rollback_attempted)
        and (
            original.rollback_challenge is None
            or original.rollback_challenge == current.rollback_challenge
        )
        and (original.failure_code is None or original.failure_code == current.failure_code)
    )


class _TransferPolicy:
    def __init__(self, control: JournalControl, backend: TransferControllerBackend) -> None:
        self.control, self.backend = control, backend
        self._previous: ProjectControllerTransferRecord | None = None

    def read(self) -> ProjectControllerTransferRecord | None:
        record = self.control.read_current()
        if record is not None and not isinstance(record, ProjectControllerTransferRecord):
            raise TransactionError("transfer_transaction_required")
        if record is not None:
            if self._previous is not None and not transfer_identity_matches(self._previous, record):
                raise TransactionError("transaction_changed")
            self._previous = record
        return record

    def snapshots(self, record: ProjectControllerTransferRecord) -> None:
        from muli.managed_journal import _read_snapshots

        _read_snapshots(self.control.journal, record.generation, record.origin.binding, "origin")
        read_snapshots(self.control.journal, record, "base")
        read_snapshots(self.control.journal, record, "target")

    def move(
        self,
        record: ProjectControllerTransferRecord,
        *,
        phase: Phase,
        intent: Intent,
        activation: Activation,
        code: Code | None,
        proof: HealthProofV2 | None,
    ) -> ProjectControllerTransferRecord:
        current = self.read()
        if current is None:
            raise TransactionError("transfer_transaction_required")
        return self.control._write(
            advance_transfer_record(
                current,
                record.position,
                TransitionV2(
                    phase=phase, intent=intent, activation=activation, code=code, proof=proof
                ),
            )
        )

    def origin_matches(
        self, record: ProjectControllerTransferRecord, observation: ManagedServiceObservation
    ) -> bool:
        return (
            (observation.configuration, observation.selector, observation.unit)
            == _files(record.origin.binding)
            and observation.process == _process(record.origin.binding)
            and observation.boot == record.identity.origin_boot
        )

    def allowed(
        self, record: ProjectControllerTransferRecord, observation: ManagedServiceObservation
    ) -> bool:
        actual = observation.configuration, observation.selector, observation.unit
        if observation.boot == record.identity.origin_boot:
            return (
                (
                    record.phase in {"prepared", "draining"}
                    or (record.phase, record.intent)
                    in {("activating", "stop_base"), ("rolling_back", "stop_target")}
                )
                and self.origin_matches(record, observation)
                and observation.service in {"running", "stopping"}
            )
        complete = actual == _files(record.base)
        if observation.service in {"running", "starting", "stopping"}:
            return complete and (
                observation.process == _process(record.base)
                or observation.service != "running"
                and observation.process is None
            )
        if observation.service != "stopped" or observation.process is not None:
            return False
        if complete:
            return True
        if not observation.writer_free:
            return False
        if (record.phase, record.intent) == ("activating", "stop_base"):
            return actual == _files(record.origin.binding)
        return (record.phase, record.intent) in {
            ("activating", "install_target"),
            ("rolling_back", "restore_base"),
        } and actual in _prefixes(record)

    def restore_prefix_observed(
        self, record: ProjectControllerTransferRecord, observation: ManagedServiceObservation
    ) -> bool:
        return (
            (record.phase, record.intent) == ("rolling_back", "stop_target")
            and observation.service == "stopped"
            and observation.writer_free
            and observation.process is None
            and (observation.configuration, observation.selector, observation.unit)
            in _prefixes(record)
        )

    def matches(
        self,
        record: ProjectControllerTransferRecord,
        observation: ManagedServiceObservation,
        role: Role,
    ) -> bool:
        return (observation.configuration, observation.selector, observation.unit) == _files(
            record.binding(role)
        ) and (
            observation.process is None
            if observation.service == "stopped"
            else observation.process == _process(record.binding(role))
        )

    def installed(
        self,
        record: ProjectControllerTransferRecord,
        observation: ManagedServiceObservation,
        role: Role,
    ) -> bool:
        return (
            observation.service == "stopped"
            and observation.writer_free
            and self.matches(record, observation, role)
            and observation.confirmation
            == BindingConfirmation(
                record.position,
                role,
                *_files(record.binding(role)),
                loaded_unit=record.binding(role).unit.digest,
            )
        )

    async def install(self, record: ProjectControllerTransferRecord, role: Role) -> None:
        await self.backend.install(record, role, read_snapshots(self.control.journal, record, role))

    def proof(self, observation: ManagedServiceObservation) -> HealthProofV2 | None:
        return observation.health

    def accepts(
        self,
        record: ProjectControllerTransferRecord,
        proof: HealthProofV2,
        role: Role,
        *,
        released: bool = False,
    ) -> bool:
        return record.accepts(proof, role, released=released)


class TransferTransactionController(
    _Controller[ProjectControllerTransferRecord, ManagedServiceObservation, HealthProofV2]
):
    def __init__(
        self,
        control: JournalControl,
        backend: TransferControllerBackend,
        clock: Callable[[], ClockReading],
        *,
        operation_timeout: float = 5.0,
    ) -> None:
        self.backend = backend
        self.transfer_policy = _TransferPolicy(control, backend)
        super().__init__(
            control, backend, clock, self.transfer_policy, operation_timeout=operation_timeout
        )

    async def _step(
        self,
        record: ProjectControllerTransferRecord,
        observation: ManagedServiceObservation,
        remaining: float,
    ) -> ProjectControllerTransferRecord:
        if record.phase in {"prepared", "draining"}:
            if observation.service != "running" or not self.transfer_policy.origin_matches(
                record, observation
            ):
                return self._failure(record, "foreign_state")
            if remaining <= record.deadline.rollback_reserve:
                return self._move(record, phase="recovery_required", code="drain_timeout")
            if record.phase == "prepared":
                return self._move(record, phase="draining")
            if observation.drained:
                return self._move(record, phase="activating", intent="stop_base")
            await self._call(record, lambda: self._effects.drain(record))
            return record
        if (record.phase, record.intent) == ("activating", "stop_base"):
            if observation.service == "running":
                if not self.transfer_policy.origin_matches(record, observation):
                    return self._failure(record, "foreign_state")
                await self._call(record, lambda: self._effects.stop(record, "base"))
            elif observation.service == "stopped" and observation.writer_free:
                return self._move(record, phase="activating", intent="install_target")
            return record
        return await super()._step(record, observation, remaining)
