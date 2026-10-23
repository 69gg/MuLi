"""Protocol 2 persistence through the existing journal, lock and writer lease.

These helpers do not own another ledger and do not authorize deployment effects.
The host supplies authenticated role bindings and actual projection receipts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from muli.compatibility import CompatibilityManifest, FormatState
from muli.managed_transaction import (
    ManagedAction,
    ProjectControllerTransferRecord,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransitionV2,
    advance_record_v2,
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
_SNAPSHOT_NAME = re.compile(
    r"[a-f0-9]{32}-(?:base|target)(?:\.json|-(?:configuration|selector)\.json|-unit\.bin)"
)


def _name(generation: str, role: Role, component: Component) -> str:
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

    def entries(self, generation: str, role: Role) -> dict[str, bytes]:
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


def read_snapshots(
    journal: TransactionJournal, record: TransactionRecordV2, role: Role
) -> RoleSnapshots:
    binding = record.binding(role)
    limit = journal.limits.max_snapshot_bytes

    def read(component: Component) -> bytes:
        return journal.directory.read(_name(record.generation, role, component), limit)

    value = RoleSnapshots(
        configuration=read("configuration"),
        selector=read("selector") if binding.selector is not None else None,
        unit=read("unit"),
    )
    value.verify(binding, limit)
    return value


def _collect(
    control: JournalControl,
    previous: TransactionRecord | TransactionRecordV2 | None,
    incoming: dict[str, bytes],
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
    }
    # A replace briefly needs one extra temporary file. Do not discover the
    # budget failure halfway through writing the new generation.
    retained = (entries - collectible) | incoming.keys() | {"state.json"}
    if len(retained) + 1 > limits.max_entries:
        raise TransactionError("directory_budget")
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
    if isinstance(previous, ProjectControllerTransferRecord):
        raise TransactionError("project_controller_transfer_unsupported")
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


def advance_managed(
    control: JournalControl, expected: Revision, transition: TransitionV2
) -> TransactionRecordV2:
    record = control.read_current()
    if not isinstance(record, TransactionRecordV2):
        raise TransactionError("managed_transaction_required")
    return control._write(advance_record_v2(record, expected, transition))
