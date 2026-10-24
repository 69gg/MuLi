"""Protocol 2 persistence through the existing journal, lock and writer lease.

These helpers do not own another ledger and do not authorize deployment effects.
The host supplies authenticated role bindings and actual projection receipts.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from muli.compatibility import CompatibilityManifest, FormatState, assess_compatibility
from muli.managed_transaction import (
    ManagedAction,
    ProjectControllerTransferRecord,
    ProjectFormatTransitionRecord,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransferOrigin,
    TransitionV2,
    advance_project_format_record,
    advance_record_v2,
    advance_transfer_record,
    compatibility_digest,
    confirm_project_format_record,
    prepare_record_v2,
    raw_snapshot,
)
from muli.management_transaction import (
    Deadline,
    FixedLock,
    JournalControl,
    ProjectionReceipt,
    Revision,
    Role,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    _identity,
    _snapshot,
)

Component = Literal["configuration", "selector", "unit"]
_SnapshotRole = Literal["origin", "base", "target", "source", "candidate", "fallback"]
_SNAPSHOT_NAME = re.compile(
    r"[a-f0-9]{32}-(?:(?:base|target)(?:\.json|-(?:configuration|selector)\.json|-unit\.bin)"
    r"|(?:origin|source|candidate|fallback)-(?:(?:configuration|selector)\.json|unit\.bin))"
)


def _name(generation: str, role: _SnapshotRole, component: Component) -> str:
    suffix = "bin" if component == "unit" else "json"
    return f"{generation}-{role}-{component}.{suffix}"


@dataclass(frozen=True, slots=True)
class RoleSnapshots:
    configuration: bytes
    selector: bytes | None
    unit: bytes

    def verify(self, binding: RoleBinding, limit: int) -> None:
        if _snapshot(self.configuration, limit) != binding.configuration:
            raise TransactionError("snapshot_invalid")
        selector = None if self.selector is None else _snapshot(self.selector, limit)
        if selector != binding.selector or raw_snapshot(self.unit, limit) != binding.unit:
            raise TransactionError("snapshot_invalid")
        if selector is not None and selector.digest != selector.semantic_digest:
            raise TransactionError("selector_not_canonical")

    def entries(self, generation: str, role: _SnapshotRole) -> dict[str, bytes]:
        values: dict[Component, bytes | None] = {
            "configuration": self.configuration,
            "selector": self.selector,
            "unit": self.unit,
        }
        return {
            _name(generation, role, component): data
            for component, data in values.items()
            if data is not None
        }


def _read_snapshots(
    journal: TransactionJournal,
    generation: str,
    binding: RoleBinding,
    role: _SnapshotRole,
) -> RoleSnapshots:
    limit = journal.limits.max_snapshot_bytes

    def read(component: Component) -> bytes:
        return journal.directory.read(_name(generation, role, component), limit)

    value = RoleSnapshots(
        configuration=read("configuration"),
        selector=read("selector") if binding.selector is not None else None,
        unit=read("unit"),
    )
    value.verify(binding, limit)
    return value


def read_snapshots(
    journal: TransactionJournal,
    record: TransactionRecordV2 | ProjectControllerTransferRecord | ProjectFormatTransitionRecord,
    role: Role,
) -> RoleSnapshots:
    if isinstance(record, ProjectFormatTransitionRecord):
        selected: _SnapshotRole = (
            "candidate" if role == "target" else "fallback" if record.revision == 5 else "source"
        )
        return _read_snapshots(
            journal,
            record.generation,
            record.binding(role),
            selected,
        )
    return _read_snapshots(journal, record.generation, record.binding(role), role)


def _collect(
    control: JournalControl,
    previous: TransactionRecord | TransactionRecordV2 | ProjectControllerTransferRecord | None,
    incoming: dict[str, bytes],
    *,
    collect: bool = True,
    retain_incoming: bool = False,
) -> None:
    """Collect only unreferenced snapshots; preserve the entire current generation."""
    control.check()
    directory, limits = control.directory, control.journal.limits
    entries: set[str] = set()
    with os.scandir(directory.fd) as iterator:
        for entry in iterator:
            entries.add(entry.name)
            if len(entries) > limits.max_entries:
                raise TransactionError("directory_budget")
    collectible = {
        name
        for name in entries
        if (_SNAPSHOT_NAME.fullmatch(name) or re.fullmatch(r"\.tmp-[a-f0-9]{32}", name))
        and not (previous is not None and name.startswith(previous.generation + "-"))
        and not (retain_incoming and name in incoming)
    }
    # A replace briefly needs one extra temporary file. Do not discover the
    # budget failure halfway through writing the new generation.
    retained = (entries - collectible) | incoming.keys() | {"state.json"}
    if len(retained) + 1 > limits.max_entries:
        raise TransactionError("directory_budget")
    if not collect:
        return
    for name in sorted(collectible):
        directory.read(name, max(limits.max_snapshot_bytes, limits.max_record_bytes))
        os.unlink(name, dir_fd=directory.fd)
    os.fsync(directory.fd)


def prepare_managed(
    control: JournalControl,
    *,
    request_id: str,
    action: ManagedAction,
    identity: ServiceIdentityV2,
    base: RoleBinding,
    target: RoleBinding,
    base_data: RoleSnapshots,
    target_data: RoleSnapshots,
    observed_format: FormatState,
    base_manifest: CompatibilityManifest,
    target_manifest: CompatibilityManifest,
    deadline: Deadline,
    writer: FixedLock,
    projected: ProjectionReceipt | None = None,
    generation: str | None = None,
    guard: Callable[[], None] | None = None,
) -> TransactionRecordV2:
    control.check()
    writer.check()
    if writer.name != "lease" or _identity(os.fstat(writer.directory.fd)) != _identity(
        os.fstat(control.journal.writer_directory.fd)
    ):
        raise TransactionError("writer_lease_required")
    limit = control.journal.limits.max_snapshot_bytes
    base_data.verify(base, limit)
    target_data.verify(target, limit)
    previous = control.read_current()
    if isinstance(previous, ProjectFormatTransitionRecord):
        raise TransactionError("project_format_transition_unsupported")
    record = prepare_record_v2(
        request_id=request_id,
        action=action,
        identity=identity,
        base=base,
        target=target,
        observed_format=observed_format,
        base_manifest=base_manifest,
        target_manifest=target_manifest,
        deadline=deadline,
        previous=previous,
        projected=projected,
        generation=generation,
    )
    if isinstance(previous, ProjectControllerTransferRecord):
        # The pure predecessor gate above only opens released -> restart.
        # Authenticate all nine retained files before any GC or new snapshot,
        # including origin even though only base/target can supply the source.
        _read_snapshots(control.journal, previous.generation, previous.origin.binding, "origin")
        read_snapshots(control.journal, previous, "base")
        read_snapshots(control.journal, previous, "target")
    # Validate the record budget before writing even unreferenced snapshots.
    if len(record.model_dump_json().encode()) > control.journal.limits.max_record_bytes:
        raise TransactionError("record_budget")
    if previous == record:
        # A retry must not silently repair changed durable evidence.
        read_snapshots(control.journal, record, "base")
        read_snapshots(control.journal, record, "target")
        if guard is not None:
            guard()
        return record
    incoming = {
        **base_data.entries(record.generation, "base"),
        **target_data.entries(record.generation, "target"),
    }
    if guard is not None:
        guard()
    _collect(control, previous, incoming)
    for name, data in incoming.items():
        control._replace(name, data)
    if guard is not None:
        guard()
    return control._write(record)


def prepare_transfer_managed(
    control: JournalControl,
    *,
    request_id: str,
    identity: ServiceIdentityV2,
    origin: TransferOrigin,
    base: RoleBinding,
    target: RoleBinding,
    origin_data: RoleSnapshots,
    base_data: RoleSnapshots,
    target_data: RoleSnapshots,
    observed_format: FormatState,
    manifest: CompatibilityManifest,
    deadline: Deadline,
    writer: FixedLock,
    generation: str,
    guard: Callable[[], None],
    materials: dict[str, bytes],
    publish: Callable[[], None],
) -> ProjectControllerTransferRecord:
    """Publish one authenticated transfer through the original journal only.

    The host authenticates anchors, the running slot and actual projection.
    This layer checks the predecessor and full finite publication budget before
    calling the host publisher or touching snapshots.
    """
    control.check()
    writer.check()
    if writer.name != "lease" or _identity(os.fstat(writer.directory.fd)) != _identity(
        os.fstat(control.journal.writer_directory.fd)
    ):
        raise TransactionError("writer_lease_required")
    limit = control.journal.limits.max_snapshot_bytes
    origin_data.verify(origin.binding, limit)
    base_data.verify(base, limit)
    target_data.verify(target, limit)
    if origin_data.configuration != base_data.configuration or base_data != target_data:
        raise TransactionError("managed_source_changed")
    if compatibility_digest(manifest) != base.compatibility:
        raise TransactionError("compatibility_changed")
    assessment = assess_compatibility(manifest, manifest, observed_format, handoff_protocol=2)
    if not assessment.format_compatible:
        raise TransactionError(assessment.code)
    previous = control.read_current()
    if isinstance(previous, ProjectControllerTransferRecord) and previous.request_id == request_id:
        if (
            previous.identity != identity
            or previous.origin != origin
            or previous.base != base
            or previous.target != target
            or previous.observed_format != observed_format
            or previous.generation != generation
        ):
            raise TransactionError("request_conflict")
        _read_snapshots(control.journal, previous.generation, previous.origin.binding, "origin")
        read_snapshots(control.journal, previous, "base")
        read_snapshots(control.journal, previous, "target")
        guard()
        return previous
    if not isinstance(previous, TransactionRecordV2):
        raise TransactionError("managed_transfer_source_required")
    if not previous.resolved or previous.phase not in {"committed", "rolled_back"}:
        raise TransactionError("transaction_in_progress")
    if previous.request_id == request_id:
        raise TransactionError("request_conflict")
    if origin.projected != ProjectionReceipt(
        **previous.position.model_dump(), store_instance=previous.identity.store_instance
    ):
        raise TransactionError("projection_required")
    selected = previous.binding("target" if previous.phase == "committed" else "base")
    if (
        selected.entry != "managed"
        or origin.deployment != previous.identity.deployment
        or origin.binding.model_dump(exclude={"configuration"})
        != selected.model_dump(exclude={"configuration"})
        or (identity.store_instance, identity.main_session)
        != (previous.identity.store_instance, previous.identity.main_session)
        or observed_format != previous.observed_format
    ):
        raise TransactionError("managed_source_changed")
    if generation == previous.generation:
        raise TransactionError("generation_conflict")
    read_snapshots(control.journal, previous, "base")
    read_snapshots(control.journal, previous, "target")
    record = ProjectControllerTransferRecord(
        action="project_controller_transfer",
        request_id=request_id,
        generation=generation,
        revision=1,
        identity=identity,
        origin=origin,
        base=base,
        target=target,
        observed_format=observed_format,
        deadline=deadline,
        challenge=secrets.token_hex(32),
    )
    if len(record.model_dump_json().encode()) > control.journal.limits.max_record_bytes:
        raise TransactionError("record_budget")
    incoming = {
        **origin_data.entries(generation, "origin"),
        **base_data.entries(generation, "base"),
        **target_data.entries(generation, "target"),
    }
    # A retry after an incomplete publication may reuse exact orphaned files,
    # but must never repair changed evidence at the frozen generation.
    for name, data in incoming.items():
        try:
            durable = control.directory.read(name, limit)
        except FileNotFoundError:
            continue
        if durable != data:
            raise TransactionError("snapshot_invalid")
    # Count anchor/slot publications and the atomic-replace temporary entry as
    # part of the same directory, rather than failing after publishing them.
    guard()
    _collect(control, previous, {**materials, **incoming}, collect=False, retain_incoming=True)
    _collect(control, previous, {**materials, **incoming}, retain_incoming=True)
    publish()
    guard()
    for name, data in incoming.items():
        control._replace(name, data)
    guard()
    return control._write(record)


def prepare_project_format_managed(
    control: JournalControl,
    *,
    record: ProjectFormatTransitionRecord,
    source_data: RoleSnapshots,
    candidate_data: RoleSnapshots,
    fallback_data: RoleSnapshots,
    source_manifest: CompatibilityManifest,
    candidate_manifest: CompatibilityManifest,
    fallback_manifest: CompatibilityManifest,
    writer: FixedLock,
    guard: Callable[[], None],
    materials: dict[str, bytes],
    publish: Callable[[], None],
) -> ProjectFormatTransitionRecord:
    """Publish only prepared fixed-1-to-2 materials, never change a DB marker."""
    control.check()
    writer.check()
    if writer.name != "lease" or _identity(os.fstat(writer.directory.fd)) != _identity(
        os.fstat(control.journal.writer_directory.fd)
    ):
        raise TransactionError("writer_lease_required")
    limit = control.journal.limits.max_snapshot_bytes
    for snapshots, binding, manifest, state in (
        (source_data, record.source.binding, source_manifest, record.source_format),
        (candidate_data, record.candidate, candidate_manifest, record.target_format),
        (fallback_data, record.fallback, fallback_manifest, record.target_format),
    ):
        snapshots.verify(binding, limit)
        if compatibility_digest(manifest) != binding.compatibility:
            raise TransactionError("compatibility_changed")
        assessment = assess_compatibility(manifest, manifest, state, handoff_protocol=2)
        if not assessment.format_compatible:
            raise TransactionError(assessment.code)
    if not (
        source_data.configuration == candidate_data.configuration == fallback_data.configuration
        and source_data.unit == candidate_data.unit == fallback_data.unit
    ):
        raise TransactionError("managed_source_changed")
    previous = control.read_current()
    if not isinstance(previous, (TransactionRecordV2, ProjectControllerTransferRecord)):
        raise TransactionError("managed_project_source_required")
    if not previous.resolved or previous.phase not in {"committed", "rolled_back"}:
        raise TransactionError("transaction_in_progress")
    if previous.request_id == record.request_id or previous.generation == record.generation:
        raise TransactionError("request_conflict")
    if record.source.projected != ProjectionReceipt(
        **previous.position.model_dump(), store_instance=previous.identity.store_instance
    ):
        raise TransactionError("projection_required")
    selected = previous.binding("target" if previous.phase == "committed" else "base")
    if (
        record.source.binding.model_dump(exclude={"configuration"})
        != selected.model_dump(exclude={"configuration"})
        or record.identity.deployment != previous.identity.deployment
        or (record.identity.store_instance, record.identity.main_session)
        != (previous.identity.store_instance, previous.identity.main_session)
        or record.source_format != previous.observed_format
    ):
        raise TransactionError("managed_source_changed")
    read_snapshots(control.journal, previous, "base")
    read_snapshots(control.journal, previous, "target")
    if isinstance(previous, ProjectControllerTransferRecord):
        _read_snapshots(control.journal, previous.generation, previous.origin.binding, "origin")
    if len(record.model_dump_json().encode()) > control.journal.limits.max_record_bytes:
        raise TransactionError("record_budget")
    incoming = {
        **source_data.entries(record.generation, "source"),
        **candidate_data.entries(record.generation, "candidate"),
        **fallback_data.entries(record.generation, "fallback"),
    }
    for name, data in incoming.items():
        try:
            durable = control.directory.read(name, limit)
        except FileNotFoundError:
            continue
        if durable != data:
            raise TransactionError("snapshot_invalid")
    guard()
    _collect(control, previous, {**materials, **incoming}, collect=False, retain_incoming=True)
    _collect(control, previous, {**materials, **incoming}, retain_incoming=True)
    publish()
    guard()
    for name, data in incoming.items():
        control._replace(name, data)
    guard()
    return control._write(record)


def confirm_project_format_managed(
    control: JournalControl,
    record: ProjectFormatTransitionRecord,
    *,
    writer: FixedLock,
    guard: Callable[[], None],
) -> ProjectFormatTransitionRecord:
    """Called inside the exact SQL-pair observer while all its locks remain held."""
    control.check()
    writer.check()
    if writer.name != "lease" or _identity(os.fstat(writer.directory.fd)) != _identity(
        os.fstat(control.journal.writer_directory.fd)
    ):
        raise TransactionError("writer_lease_required")
    if control.read_current() != record:
        raise TransactionError("stale_revision")
    confirmed = confirm_project_format_record(record, record.position)
    bindings: tuple[tuple[Literal["source", "candidate", "fallback"], RoleBinding], ...] = (
        ("source", record.source.binding),
        ("candidate", record.candidate),
        ("fallback", record.fallback),
    )
    for name, binding in bindings:
        _read_snapshots(control.journal, record.generation, binding, name)
    if len(confirmed.model_dump_json().encode()) > control.journal.limits.max_record_bytes:
        raise TransactionError("record_budget")
    guard()
    if control.read_current() != record:
        raise TransactionError("stale_revision")
    return record if confirmed == record else control._write(confirmed)


def advance_managed(
    control: JournalControl, expected: Revision, transition: TransitionV2
) -> TransactionRecordV2 | ProjectControllerTransferRecord | ProjectFormatTransitionRecord:
    record = control.read_current()
    if isinstance(record, ProjectFormatTransitionRecord):
        return control._write(advance_project_format_record(record, expected, transition))
    if isinstance(record, ProjectControllerTransferRecord):
        return control._write(advance_transfer_record(record, expected, transition))
    if not isinstance(record, TransactionRecordV2):
        raise TransactionError("managed_transaction_required")
    return control._write(advance_record_v2(record, expected, transition))
