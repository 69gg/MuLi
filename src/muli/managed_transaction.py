"""Protocol 2 records and pure validation; the existing journal owns all I/O.

These declarations do not authenticate installations, open data, or authorize an
effect. The host binds fresh role evidence and persists results under the one
controller lock and writer lease, using the existing atomic file primitives.
"""

from __future__ import annotations

import re
import secrets
from typing import Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from muli.compatibility import (
    _METADATA_BYTES,
    CompatibilityManifest,
    FormatState,
    assess_compatibility,
)
from muli.management_transaction import (
    _EDGES,
    _INTENTS,
    Activation,
    Code,
    Deadline,
    Digest,
    Identifier,
    Intent,
    JournalLimits,
    Phase,
    ProjectionReceipt,
    Revision,
    Role,
    SnapshotReference,
    StrictRecord,
    TransactionError,
    TransactionRecord,
    _digest,
    _object,
    _snapshot,
)

ManagedAction = Literal["adopt", "switch", "configure", "restart"]
ProofRole = Literal["origin", "base", "target"]


class RawSnapshotReference(StrictRecord):
    digest: Digest
    size: int = Field(ge=1, le=4_194_304)


def raw_snapshot(data: bytes, limit: int) -> RawSnapshotReference:
    """Reference exact non-JSON bytes, without decoding or writing them."""
    if not isinstance(data, bytes) or not data:
        raise TransactionError("invalid_snapshot")
    if type(limit) is not int or not 1 <= limit <= 4_194_304 or len(data) > limit:
        raise TransactionError("snapshot_budget")
    return RawSnapshotReference(digest=_digest(data), size=len(data))


def compatibility_digest(manifest: CompatibilityManifest) -> str:
    return _snapshot(manifest.model_dump_json().encode(), _METADATA_BYTES).semantic_digest


def format_digest(state: FormatState) -> str:
    return _snapshot(state.model_dump_json().encode(), _METADATA_BYTES).semantic_digest


class _Protocol2(StrictRecord):
    protocol: Literal[2] = 2

    @field_validator("protocol", mode="before")
    @classmethod
    def exact_protocol(cls, value: object) -> object:
        # Literal equality alone must not admit 2.0 or a coerced string.
        if type(value) is not int or value != 2:
            raise ValueError("invalid_protocol")
        return value


class ServiceIdentityV2(StrictRecord):
    deployment: Digest
    store_instance: Identifier
    main_session: Identifier
    origin_boot: Identifier


class RoleBinding(StrictRecord):
    entry: Literal["legacy", "managed"]
    installation: Digest
    compatibility: Digest
    configuration: SnapshotReference
    selector: SnapshotReference | None
    unit: RawSnapshotReference

    @model_validator(mode="after")
    def selector_matches_entry(self) -> Self:
        if (self.entry == "managed") != (self.selector is not None):
            raise ValueError("invalid_role_selector")
        return self


class HealthProofV2(_Protocol2):
    generation: Identifier
    challenge: Identifier = Field(repr=False)
    boot: Identifier
    role: ProofRole
    deployment: Digest
    installation: Digest
    compatibility: Digest
    configuration: Digest
    selector: Digest | None
    unit: Digest
    store_instance: Identifier
    main_session: Identifier
    format_digest: Digest
    core_ready: bool
    admission: Literal["paused", "released", "released_degraded", "blocked"]


class TransactionRecordV2(_Protocol2):
    request_id: Identifier
    generation: Identifier
    revision: int = Field(ge=1)
    action: ManagedAction
    identity: ServiceIdentityV2
    base: RoleBinding
    target: RoleBinding
    observed_format: FormatState
    deadline: Deadline
    phase: Phase = "prepared"
    intent: Intent = "none"
    challenge: Identifier = Field(repr=False)
    rollback_challenge: Identifier | None = Field(default=None, repr=False)
    rollback_attempted: bool = False
    health: HealthProofV2 | None = Field(default=None, repr=False)
    activation: Activation = "pending"
    code: Code | None = None
    failure_code: Code | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        base, target = self.base, self.target
        if self.action == "adopt":
            if (
                base.entry != "legacy"
                or target.entry != "managed"
                or base.installation != target.installation
                or base.compatibility != target.compatibility
                or base.configuration != target.configuration
            ):
                raise ValueError("invalid_adoption")
        else:
            if base.entry != "managed" or target.entry != "managed":
                raise ValueError("managed_roles_required")
            if self.action == "switch":
                if base.installation == target.installation or base.unit != target.unit:
                    raise ValueError("invalid_switch")
            elif self.action == "configure":
                if base.model_dump(exclude={"configuration"}) != target.model_dump(
                    exclude={"configuration"}
                ):
                    raise ValueError("configure_changes_installation")
            elif base != target:
                raise ValueError("restart_changes_binding")
        intents = {
            "activating": {"stop_base", "install_target", "start_target"},
            "rolling_back": {"stop_target", "restore_base", "start_base", "verify_base"},
        }
        if self.intent not in intents.get(self.phase, {"none"}):
            raise ValueError("invalid_intent")
        if self.rollback_attempted != (self.rollback_challenge is not None):
            raise ValueError("invalid_rollback")
        if self.rollback_challenge == self.challenge:
            raise ValueError("invalid_rollback_challenge")
        if self.phase in {"rolling_back", "rolled_back"} and not self.rollback_attempted:
            raise ValueError("missing_rollback")
        if self.rollback_attempted and self.phase not in {
            "rolling_back",
            "rolled_back",
            "recovery_required",
        }:
            raise ValueError("invalid_rollback_phase")
        if self.phase not in {"committed", "rolled_back"} and self.activation != "pending":
            raise ValueError("invalid_activation")
        if self.phase in {"committed", "rolled_back"} and self.health is None:
            raise ValueError("missing_health")
        if self.health is not None:
            role: Role = "base" if self.phase == "rolled_back" else "target"
            released = self.activation in {"released", "released_degraded"}
            if self.phase not in {"committed", "rolled_back"} or not self.accepts(
                self.health, role, released=released
            ):
                raise ValueError("invalid_health")
            if released and self.activation != self.health.admission:
                raise ValueError("invalid_release")
        return self

    def binding(self, role: Role) -> RoleBinding:
        if role not in {"base", "target"}:
            raise TransactionError("invalid_role")
        return self.base if role == "base" else self.target

    @property
    def boot_role(self) -> Role | None:
        if self.phase in {"verifying", "committed"} or (
            self.phase == "activating" and self.intent == "start_target"
        ):
            return "target"
        if self.phase == "rolled_back" or (
            self.phase == "rolling_back" and self.intent in {"start_base", "verify_base"}
        ):
            return "base"
        return None

    @property
    def position(self) -> Revision:
        return Revision(generation=self.generation, revision=self.revision)

    @property
    def resolved(self) -> bool:
        return self.phase == "aborted" or (
            self.phase in {"committed", "rolled_back"}
            and self.activation in {"released", "released_degraded"}
        )

    def public(self) -> dict[str, str | int | bool | None]:
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "generation": self.generation,
            "revision": self.revision,
            "action": self.action,
            "phase": self.phase,
            "activation": self.activation,
            "resolved": self.resolved,
            "code": self.code,
            "failure_code": self.failure_code,
        }

    def accepts(self, proof: HealthProofV2, role: Role, *, released: bool = False) -> bool:
        if role not in {"base", "target"} or self.boot_role != role:
            return False
        binding = self.binding(role)
        return (
            proof.protocol == 2
            and proof.role == role
            and proof.generation == self.generation
            and proof.challenge == (self.challenge if role == "target" else self.rollback_challenge)
            and proof.boot != self.identity.origin_boot
            and proof.deployment == self.identity.deployment
            and proof.installation == binding.installation
            and proof.compatibility == binding.compatibility
            and proof.configuration == binding.configuration.digest
            and proof.selector == (binding.selector.digest if binding.selector else None)
            and proof.unit == binding.unit.digest
            and proof.store_instance == self.identity.store_instance
            and proof.main_session == self.identity.main_session
            and proof.format_digest == format_digest(self.observed_format)
            and proof.core_ready
            and (
                proof.admission in {"released", "released_degraded"}
                if released
                else proof.admission == "paused"
            )
        )


class TransferOrigin(StrictRecord):
    """Frozen old authority; never a boot role under the replacement anchor."""

    deployment: Digest
    binding: RoleBinding
    projected: ProjectionReceipt

    @model_validator(mode="after")
    def managed_origin(self) -> Self:
        if self.binding.entry != "managed":
            raise ValueError("managed_origin_required")
        return self


class ProjectControllerTransferRecord(_Protocol2):
    """Strict transfer shape; parsing grants no prepare or effect authority.

    The host must prove this installation is the currently executing bridge,
    bind both immutable anchors and authenticate the live origin separately.
    Ordinary protocol-2 actions deliberately retain their original model.
    """

    request_id: Identifier
    generation: Identifier
    revision: int = Field(ge=1)
    action: Literal["project_controller_transfer"]
    identity: ServiceIdentityV2
    origin: TransferOrigin
    base: RoleBinding
    target: RoleBinding
    observed_format: FormatState
    deadline: Deadline
    phase: Phase = "prepared"
    intent: Intent = "none"
    challenge: Identifier = Field(repr=False)
    rollback_challenge: Identifier | None = Field(default=None, repr=False)
    rollback_attempted: bool = False
    health: HealthProofV2 | None = Field(default=None, repr=False)
    activation: Activation = "pending"
    code: Code | None = None
    failure_code: Code | None = None

    @field_validator("generation")
    @classmethod
    def snapshot_generation(cls, value: str) -> str:
        if re.fullmatch(r"[a-f0-9]{32}", value) is None:
            raise ValueError("invalid_generation")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        origin, base = self.origin, self.base
        if (
            base.entry != "managed"
            or base != self.target
            or base.installation != origin.binding.installation
            or base.compatibility != origin.binding.compatibility
            or base.configuration != origin.binding.configuration
            or base.selector == origin.binding.selector
        ):
            raise ValueError("invalid_transfer_binding")
        if (
            origin.deployment == self.identity.deployment
            or origin.projected.generation == self.generation
            or origin.projected.store_instance != self.identity.store_instance
        ):
            raise ValueError("invalid_transfer_origin")
        for selected in (origin.binding, base):
            selector = selected.selector
            if selector is None or selector.digest != selector.semantic_digest:
                raise ValueError("selector_not_canonical")
        # This action only transfers control of the complete format-1 bridge.
        # CURRENT_FORMAT_STATE may change in a later release; it is not the
        # authority to broaden this specific, retained action's contract.
        if any(version != 1 for version in self.observed_format.formats.model_dump().values()):
            raise ValueError("transfer_format_required")
        intents = {
            "activating": {"stop_base", "install_target", "start_target"},
            "rolling_back": {"stop_target", "restore_base", "start_base", "verify_base"},
        }
        if self.phase == "aborted" or self.intent not in intents.get(self.phase, {"none"}):
            raise ValueError("invalid_transfer_phase")
        if self.rollback_attempted != (self.rollback_challenge is not None):
            raise ValueError("invalid_rollback")
        if self.rollback_challenge == self.challenge:
            raise ValueError("invalid_rollback_challenge")
        if self.phase in {"rolling_back", "rolled_back"} and not self.rollback_attempted:
            raise ValueError("missing_rollback")
        if self.rollback_attempted and self.phase not in {
            "rolling_back",
            "rolled_back",
            "recovery_required",
        }:
            raise ValueError("invalid_rollback_phase")
        if self.phase not in {"committed", "rolled_back"} and self.activation != "pending":
            raise ValueError("invalid_activation")
        if self.phase in {"committed", "rolled_back"} and self.health is None:
            raise ValueError("missing_health")
        if self.health is not None:
            role: Role = "base" if self.phase == "rolled_back" else "target"
            released = self.activation in {"released", "released_degraded"}
            if self.phase not in {"committed", "rolled_back"} or not self.accepts(
                self.health, role, released=released
            ):
                raise ValueError("invalid_health")
            if released and self.activation != self.health.admission:
                raise ValueError("invalid_release")
        return self

    def binding(self, role: Role) -> RoleBinding:
        if role not in {"base", "target"}:
            raise TransactionError("invalid_role")
        return self.base if role == "base" else self.target

    @property
    def boot_role(self) -> Role | None:
        if self.phase in {"verifying", "committed"} or (
            self.phase == "activating" and self.intent == "start_target"
        ):
            return "target"
        if self.phase == "rolled_back" or (
            self.phase == "rolling_back" and self.intent in {"start_base", "verify_base"}
        ):
            return "base"
        return None

    @property
    def position(self) -> Revision:
        return Revision(generation=self.generation, revision=self.revision)

    @property
    def resolved(self) -> bool:
        return self.phase in {"committed", "rolled_back"} and self.activation in {
            "released",
            "released_degraded",
        }

    def public(self) -> dict[str, str | int | bool | None]:
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "generation": self.generation,
            "revision": self.revision,
            "action": self.action,
            "phase": self.phase,
            "activation": self.activation,
            "resolved": self.resolved,
            "code": self.code,
            "failure_code": self.failure_code,
        }

    def accepts(self, proof: HealthProofV2, role: Role, *, released: bool = False) -> bool:
        if role not in {"base", "target"} or self.boot_role != role:
            return False
        binding = self.binding(role)
        return (
            proof.protocol == 2
            and proof.role == role
            and proof.generation == self.generation
            and proof.challenge == (self.challenge if role == "target" else self.rollback_challenge)
            and proof.boot != self.identity.origin_boot
            and proof.deployment == self.identity.deployment
            and proof.installation == binding.installation
            and proof.compatibility == binding.compatibility
            and proof.configuration == binding.configuration.digest
            and proof.selector == (binding.selector.digest if binding.selector else None)
            and proof.unit == binding.unit.digest
            and proof.store_instance == self.identity.store_instance
            and proof.main_session == self.identity.main_session
            and proof.format_digest == format_digest(self.observed_format)
            and proof.core_ready
            and (
                proof.admission in {"released", "released_degraded"}
                if released
                else proof.admission == "paused"
            )
        )


class ProjectFormatSource(StrictRecord):
    binding: RoleBinding
    projected: ProjectionReceipt


class ProjectFormatDatabase(StrictRecord):
    device: int = Field(ge=0, lt=2**64)
    inode: int = Field(gt=0, lt=2**64)


class ProjectFormatMigration(StrictRecord):
    id: Literal["project_review_1_to_2"]
    version: int = Field(ge=1, le=1)
    database: ProjectFormatDatabase
    source_marker: SnapshotReference
    target_marker: SnapshotReference


class ProjectFormatTransitionRecord(_Protocol2):
    """Frozen SQL/confirmation prefix; no driver or boot admission."""

    request_id: Identifier
    generation: Identifier
    revision: int = Field(default=1, ge=1, le=5)
    action: Literal["project_review_1_to_2"]
    identity: ServiceIdentityV2
    source: ProjectFormatSource
    candidate: RoleBinding
    fallback: RoleBinding
    base: RoleBinding
    target: RoleBinding
    source_format: FormatState
    target_format: FormatState
    observed_format: FormatState
    migration: ProjectFormatMigration
    deadline: Deadline
    phase: Literal["prepared", "draining", "activating"] = "prepared"
    intent: Literal["none", "stop_base", "install_target"] = "none"
    activation: Literal["pending"] = "pending"
    challenge: Identifier = Field(repr=False)
    rollback_challenge: None = None
    rollback_attempted: bool = False
    health: None = None
    code: None = None
    failure_code: None = None

    @field_validator("generation")
    @classmethod
    def snapshot_generation(cls, value: str) -> str:
        if re.fullmatch(r"[a-f0-9]{32}", value) is None:
            raise ValueError("invalid_generation")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.revision, self.phase, self.intent) not in {
            (1, "prepared", "none"),
            (2, "draining", "none"),
            (3, "activating", "stop_base"),
            (4, "activating", "install_target"),
            (5, "activating", "install_target"),
        }:
            raise ValueError("invalid_project_format_prefix")
        source = self.source
        if (
            self.rollback_attempted
            or self.base != (self.fallback if self.revision == 5 else source.binding)
            or self.target != self.candidate
            or len(
                {
                    source.binding.installation,
                    self.candidate.installation,
                    self.fallback.installation,
                }
            )
            != 3
            or source.projected.generation == self.generation
            or source.projected.store_instance != self.identity.store_instance
        ):
            raise ValueError("invalid_project_format_roles")
        for binding in (source.binding, self.candidate, self.fallback):
            if (
                binding.entry != "managed"
                or binding.configuration != source.binding.configuration
                or binding.unit != source.binding.unit
                or binding.selector is None
                or binding.selector.digest != binding.selector.semantic_digest
            ):
                raise ValueError("invalid_project_format_binding")
        if (
            self.observed_format
            != (self.target_format if self.revision == 5 else self.source_format)
            or self.source_format.metadata_version != 1
            or self.target_format.metadata_version != 1
            or any(version != 1 for version in self.source_format.formats.model_dump().values())
            or any(
                version != (2 if domain in {"objects", "ingress"} else 1)
                for domain, version in self.target_format.formats.model_dump().items()
            )
            or self.migration.source_marker.semantic_digest != format_digest(self.source_format)
            or self.migration.source_marker.size > _METADATA_BYTES
            or self.migration.target_marker
            != _snapshot(self.target_format.model_dump_json().encode(), _METADATA_BYTES)
        ):
            raise ValueError("invalid_project_format_evidence")
        return self

    def binding(self, role: Role) -> RoleBinding:
        if role not in {"base", "target"}:
            raise TransactionError("invalid_role")
        return self.base if role == "base" else self.target

    @property
    def position(self) -> Revision:
        return Revision(generation=self.generation, revision=self.revision)

    @property
    def resolved(self) -> bool:
        return False

    @property
    def boot_role(self) -> Role | None:
        return None

    def public(self) -> dict[str, str | int | bool | None]:
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "generation": self.generation,
            "revision": self.revision,
            "action": self.action,
            "phase": self.phase,
            "activation": self.activation,
            "resolved": False,
            "code": None,
            "failure_code": None,
        }


class TransitionV2(StrictRecord):
    phase: Phase
    intent: Intent = "none"
    activation: Activation = "pending"
    code: Code | None = None
    proof: HealthProofV2 | None = None


def parse_record_v2(data: bytes, *, limits: JournalLimits | None = None) -> TransactionRecordV2:
    limits = limits or JournalLimits()
    if not isinstance(data, bytes):
        raise TransactionError("invalid_record")
    if len(data) > limits.max_record_bytes:
        raise TransactionError("record_budget")
    try:
        values = _object(data)
        if type(values.get("protocol")) is not int or values["protocol"] != 2:
            raise TransactionError("invalid_record")
        return TransactionRecordV2.model_validate(values)
    except (ValidationError, ValueError, RecursionError):
        raise TransactionError("invalid_record") from None


def parse_managed_record(
    data: bytes, *, limits: JournalLimits | None = None
) -> TransactionRecordV2 | ProjectControllerTransferRecord | ProjectFormatTransitionRecord:
    """Bounded action dispatch; ordinary V2 validation remains unchanged."""
    limits = limits or JournalLimits()
    if not isinstance(data, bytes):
        raise TransactionError("invalid_record")
    if len(data) > limits.max_record_bytes:
        raise TransactionError("record_budget")
    try:
        values = _object(data)
        if type(values.get("protocol")) is not int or values["protocol"] != 2:
            raise TransactionError("invalid_record")
        if values.get("action") == "project_controller_transfer":
            return ProjectControllerTransferRecord.model_validate(values)
        if values.get("action") == "project_review_1_to_2":
            return ProjectFormatTransitionRecord.model_validate(values)
    except (ValidationError, ValueError, RecursionError):
        raise TransactionError("invalid_record") from None
    return parse_record_v2(data, limits=limits)


def prepare_record_v2(
    *,
    request_id: str,
    action: ManagedAction,
    identity: ServiceIdentityV2,
    base: RoleBinding,
    target: RoleBinding,
    observed_format: FormatState,
    base_manifest: CompatibilityManifest,
    target_manifest: CompatibilityManifest,
    deadline: Deadline,
    previous: TransactionRecord
    | TransactionRecordV2
    | ProjectControllerTransferRecord
    | None = None,
    projected: ProjectionReceipt | None = None,
    generation: str | None = None,
) -> TransactionRecordV2:
    """Validate a prepare request; the host authenticates and writes snapshots.

    A retry returns the original record, challenge and deadline. Passing a
    receipt is not proof of durability: the journal bridge must read its actual
    Store projection under the existing locks before calling this helper.
    """
    if generation is not None and (
        not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation)
    ):
        raise TransactionError("invalid_generation")
    if isinstance(previous, ProjectControllerTransferRecord) and (
        not previous.resolved or action != "restart"
    ):
        raise TransactionError("project_controller_transfer_unsupported")
    assessment = assess_compatibility(
        base_manifest, target_manifest, observed_format, handoff_protocol=2
    )
    if not assessment.format_compatible:
        raise TransactionError(assessment.code)
    if (
        compatibility_digest(base_manifest) != base.compatibility
        or compatibility_digest(target_manifest) != target.compatibility
    ):
        raise TransactionError("compatibility_changed")
    try:
        record = TransactionRecordV2.model_validate(
            {
                "request_id": request_id,
                "action": action,
                "identity": identity.model_dump(),
                "base": base.model_dump(),
                "target": target.model_dump(),
                "observed_format": observed_format.model_dump(),
                "deadline": deadline.model_dump(),
                "generation": generation or secrets.token_hex(16),
                "revision": 1,
                "challenge": secrets.token_hex(32),
            }
        )
    except (ValidationError, ValueError, RecursionError):
        raise TransactionError("invalid_prepare") from None
    if previous is None:
        return record
    if previous.request_id == request_id:
        if (
            not isinstance(previous, TransactionRecordV2)
            or previous.action != action
            or previous.identity != identity
            or previous.base != base
            or previous.target != target
            or previous.observed_format != observed_format
            or (generation is not None and generation != previous.generation)
        ):
            raise TransactionError("request_conflict")
        return previous
    if not previous.resolved:
        raise TransactionError("transaction_in_progress")
    if projected != ProjectionReceipt(
        **previous.position.model_dump(), store_instance=previous.identity.store_instance
    ):
        raise TransactionError("projection_required")
    if (
        previous.identity.store_instance != identity.store_instance
        or previous.identity.main_session != identity.main_session
    ):
        raise TransactionError("store_changed")
    if isinstance(previous, ProjectControllerTransferRecord):
        selected = previous.binding("base" if previous.phase == "rolled_back" else "target")
        if identity.deployment != previous.identity.deployment or base.model_dump(
            exclude={"configuration"}
        ) != selected.model_dump(exclude={"configuration"}):
            raise TransactionError("managed_source_changed")
        if observed_format != previous.observed_format:
            raise TransactionError("managed_format_changed")
        # Configuration may have changed through the ordinary live settings
        # path. Its actual bytes and fresh process permit remain host checks;
        # neither the old configuration nor historical health grants admission.
    if record.generation == previous.generation:
        raise TransactionError("generation_conflict")
    return record


def advance_record_v2(
    record: TransactionRecordV2, expected: Revision, transition: TransitionV2
) -> TransactionRecordV2:
    """Return one validated next revision without observing or performing effects."""
    if not isinstance(record, TransactionRecordV2):
        raise TransactionError("managed_transaction_required")
    return _advance_protocol2(record, expected, transition)


def advance_transfer_record(
    record: ProjectControllerTransferRecord, expected: Revision, transition: TransitionV2
) -> ProjectControllerTransferRecord:
    """Advance only frozen transfer state; never authenticate or perform an effect."""
    if not isinstance(record, ProjectControllerTransferRecord):
        raise TransactionError("project_controller_transfer_required")
    return _advance_protocol2(record, expected, transition)


def advance_project_format_record(
    record: ProjectFormatTransitionRecord, expected: Revision, transition: TransitionV2
) -> ProjectFormatTransitionRecord:
    """Persist only the fixed pre-SQL intent; no observation is implied."""
    if record.position != expected:
        raise TransactionError("stale_revision")
    following = {
        ("prepared", "none"): ("draining", "none"),
        ("draining", "none"): ("activating", "stop_base"),
        ("activating", "stop_base"): ("activating", "install_target"),
    }
    if (
        following.get((record.phase, record.intent)) != (transition.phase, transition.intent)
        or transition.activation != "pending"
        or transition.code is not None
        or transition.proof is not None
    ):
        raise TransactionError("invalid_transition")
    return ProjectFormatTransitionRecord.model_validate(
        {
            **record.model_dump(),
            "revision": record.revision + 1,
            "phase": transition.phase,
            "intent": transition.intent,
        }
    )


def project_format_identity_matches(
    previous: ProjectFormatTransitionRecord,
    current: ProjectFormatTransitionRecord,
) -> bool:
    """Only validated, forward state may change the two derived routing fields."""
    mutable = {"revision", "phase", "intent", "base", "observed_format"}
    return previous.revision <= current.revision and previous.model_dump(
        exclude=mutable
    ) == current.model_dump(exclude=mutable)


def confirm_project_format_record(
    record: ProjectFormatTransitionRecord,
    expected: Revision,
) -> ProjectFormatTransitionRecord:
    """Derive the confirmation shape; the caller must authenticate the SQL pair."""
    if record.position != expected:
        raise TransactionError("stale_revision")
    if record.revision == 5:
        return record
    if (record.revision, record.phase, record.intent) != (4, "activating", "install_target"):
        raise TransactionError("invalid_transition")
    return ProjectFormatTransitionRecord.model_validate(
        {
            **record.model_dump(),
            "revision": 5,
            "observed_format": record.target_format,
            "base": record.fallback,
        }
    )


def _advance_protocol2[RecordT: (TransactionRecordV2, ProjectControllerTransferRecord)](
    record: RecordT, expected: Revision, transition: TransitionV2
) -> RecordT:
    if record.position != expected:
        raise TransactionError("stale_revision")
    if isinstance(record, ProjectControllerTransferRecord) and (
        transition.phase == "aborted"
        or (record.phase == "draining" and transition.phase == "prepared")
    ):
        raise TransactionError("invalid_transition")
    if transition.phase not in _EDGES.get(record.phase, set()):
        raise TransactionError("invalid_transition")
    if (
        transition.phase == record.phase
        and record.phase in {"activating", "rolling_back"}
        and transition.intent != _INTENTS.get(record.intent)
    ):
        raise TransactionError("invalid_intent_transition")
    if (
        transition.phase == "activating"
        and record.phase != "activating"
        and transition.intent != "stop_base"
    ):
        raise TransactionError("invalid_intent_transition")
    if transition.phase == "verifying" and record.intent != "start_target":
        raise TransactionError("invalid_intent_transition")
    if (
        transition.phase == "rolled_back"
        and record.phase != "rolled_back"
        and record.intent != "verify_base"
    ):
        raise TransactionError("invalid_intent_transition")
    if transition.phase not in {"committed", "rolled_back"} and transition.proof is not None:
        raise TransactionError("unexpected_health_proof")
    updates: dict[str, object] = transition.model_dump(exclude={"proof"})
    updates["revision"] = record.revision + 1
    if record.failure_code is None and transition.code is not None:
        updates["failure_code"] = transition.code
    if transition.phase == "rolling_back" and record.phase != "rolling_back":
        if record.rollback_attempted or transition.intent != "stop_target":
            raise TransactionError("rollback_already_attempted")
        updates.update(rollback_attempted=True, rollback_challenge=secrets.token_hex(32))
    if transition.phase in {"committed", "rolled_back"}:
        role: Role = "target" if transition.phase == "committed" else "base"
        proof = transition.proof
        decided = transition.phase == record.phase
        if decided and transition.activation == "blocked":
            if transition.code is None or record.activation != "pending" or proof is not None:
                raise TransactionError("invalid_release_proof")
        else:
            released = transition.activation in {"released", "released_degraded"}
            if proof is None or not record.accepts(proof, role, released=released):
                raise TransactionError("invalid_health_proof")
            if decided:
                if (
                    record.activation not in {"pending", "blocked"}
                    or record.health is None
                    or (released and transition.activation != proof.admission)
                    or (
                        not released
                        and record.activation == "blocked"
                        and proof.boot == record.health.boot
                    )
                ):
                    raise TransactionError("invalid_release_proof")
            elif transition.activation != "pending":
                raise TransactionError("invalid_activation")
            updates["health"] = proof.model_dump()
    try:
        return type(record).model_validate({**record.model_dump(), **updates})
    except (ValidationError, ValueError, RecursionError):
        raise TransactionError("invalid_transition") from None
