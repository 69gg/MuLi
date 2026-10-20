"""Bounded compatibility declarations, without opening or migrating application data.

A positive assessment compares declarations only. It does not authenticate an
artifact, observe a database, or authorize a cross-version service transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from muli.management_transaction import StrictRecord, TransactionError, _object

_METADATA_BYTES = 16_384
_MAX_VERSION = 2_147_483_647
FormatVersion = Annotated[int, Field(ge=1, le=_MAX_VERSION)]
FormatDomain = Literal[
    "database", "objects", "configuration", "ingress", "outbox", "memory", "install_profile"
]
ManifestSide = Literal["base", "candidate"]
_DOMAINS: tuple[FormatDomain, ...] = (
    "database",
    "objects",
    "configuration",
    "ingress",
    "outbox",
    "memory",
    "install_profile",
)
ReleaseVersion = Annotated[
    str,
    Field(max_length=40, pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"),
]


class CompatibilityError(ValueError):
    """A fixed reason code, never a copy of supplied metadata."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FormatSupport(StrictRecord):
    # A version is an opaque contract ID: reading 3 does not imply reading 2.
    reads: tuple[FormatVersion, ...] = Field(min_length=1, max_length=8)
    writes: FormatVersion

    @model_validator(mode="after")
    def explicit_support(self) -> Self:
        if len(self.reads) != len(set(self.reads)) or self.writes not in self.reads:
            raise ValueError("invalid_format_support")
        return self


class FormatVersions(StrictRecord):
    """Caller-supplied evidence for each complete persisted domain, not table DDL."""

    database: FormatVersion
    objects: FormatVersion
    configuration: FormatVersion
    ingress: FormatVersion
    outbox: FormatVersion
    memory: FormatVersion
    install_profile: FormatVersion


class FormatContracts(StrictRecord):
    database: FormatSupport
    objects: FormatSupport
    configuration: FormatSupport
    ingress: FormatSupport
    outbox: FormatSupport
    memory: FormatSupport
    install_profile: FormatSupport


class _Metadata(StrictRecord):
    # Required, strict integer: Literal[1] alone can accept Python True.
    metadata_version: int = Field(ge=1, le=1)

    @classmethod
    def from_json(cls, data: bytes) -> Self:
        if not isinstance(data, bytes):
            raise CompatibilityError("invalid_metadata")
        if len(data) > _METADATA_BYTES:
            raise CompatibilityError("metadata_budget")
        try:
            # Reuse the journal's duplicate-key / non-finite JSON rejection.
            _object(data)
            # Strict JSON validation accepts arrays as immutable tuples, while
            # preserving strict integers and booleans in the declaration.
            return cls.model_validate_json(data)
        except (TransactionError, ValidationError, ValueError, RecursionError):
            raise CompatibilityError("invalid_metadata") from None


class CompatibilityManifest(_Metadata):
    distribution: Literal["muli-agent"]
    release: ReleaseVersion
    handoff_protocol: FormatVersion
    requires_migration: bool
    formats: FormatContracts


class FormatState(_Metadata):
    """No reader is supplied here: legacy or unproven state must remain absent."""

    formats: FormatVersions


AssessmentCode = Literal[
    "declared_format_compatible",
    "missing_base_manifest",
    "missing_candidate_manifest",
    "missing_format_state",
    "handoff_protocol_unsupported",
    "migration_required",
    "current_format_unreadable",
    "write_format_change",
]


@dataclass(frozen=True)
class CompatibilityAssessment:
    format_compatible: bool
    code: AssessmentCode
    domain: FormatDomain | None = None
    side: ManifestSide | None = None


def assess_compatibility(
    base: CompatibilityManifest | None,
    candidate: CompatibilityManifest | None,
    current: FormatState | None,
    *,
    handoff_protocol: int,
) -> CompatibilityAssessment:
    """Compare evidence for a same-format switch; perform no I/O or migration.

    The caller owns evidence provenance and freshness. Protocol agreement here
    does not extend protocol 1's single-installation transaction to A/B updates.
    """
    if base is None:
        return CompatibilityAssessment(False, "missing_base_manifest")
    if candidate is None:
        return CompatibilityAssessment(False, "missing_candidate_manifest")
    if current is None:
        return CompatibilityAssessment(False, "missing_format_state")
    if type(handoff_protocol) is not int or not 1 <= handoff_protocol <= _MAX_VERSION:
        return CompatibilityAssessment(False, "handoff_protocol_unsupported")
    manifests: tuple[tuple[ManifestSide, CompatibilityManifest], ...] = (
        ("base", base),
        ("candidate", candidate),
    )
    for side, manifest in manifests:
        if manifest.handoff_protocol != handoff_protocol:
            return CompatibilityAssessment(False, "handoff_protocol_unsupported", side=side)
        if manifest.requires_migration:
            return CompatibilityAssessment(False, "migration_required", side=side)
    for domain in _DOMAINS:
        version: int = getattr(current.formats, domain)
        for side, manifest in manifests:
            support: FormatSupport = getattr(manifest.formats, domain)
            if version not in support.reads:
                return CompatibilityAssessment(False, "current_format_unreadable", domain, side)
            # Same-format first: even a mutually readable new write format is
            # outside this slice, and cannot silently change the shared data.
            if support.writes != version:
                return CompatibilityAssessment(False, "write_format_change", domain, side)
    return CompatibilityAssessment(True, "declared_format_compatible")
