"""Format-transition preparation only; retained fixture interpreters are never executed."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_managed_boot import Host as BootHost
from test_managed_boot import _slot
from test_managed_boot import host as boot_host
from test_managed_host import filesystems as filesystems
from test_project_controller_transfer_runtime import (
    _FRESH_BOOT,
    TerminalProbe,
    TerminalTransfer,
    _acquire_reopened_permit,
    _boot_environment,
    _publish_terminal_transfer,
)

from muli.compatibility import _METADATA_BYTES, FormatState, assess_compatibility
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.managed_boot import ManagedBootError, ManagedBootPermit
from muli.managed_host import _entry as read_helper_entry
from muli.managed_host import _record as read_helper_record
from muli.managed_journal import advance_managed
from muli.managed_preparation import LocalSlotPaths, ManagedPreparation
from muli.managed_transaction import (
    ProjectControllerTransferRecord,
    ProjectFormatTransitionRecord,
    TransactionRecordV2,
    TransitionV2,
    format_digest,
    parse_managed_record,
    parse_record_v2,
    raw_snapshot,
)
from muli.management_projection import project_final, read_projection
from muli.management_transaction import ClockReading, ProjectionReceipt, TransactionError, _snapshot
from muli.platforms.linux_bootstrap import BootSession
from muli.platforms.managed_deployment import (
    DeploymentStore,
    SelectorValue,
    SlotIdentity,
    register_local_slot,
    verify_slot_files,
)
from muli.proactive import FrozenRouting
from muli.runtime import Runtime

__all__ = ["boot_host", "filesystems"]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed preparation")


def project_format() -> FormatState:
    values = CURRENT_FORMAT_STATE.model_dump()
    values["formats"].update(objects=2, ingress=2)
    return FormatState.model_validate(values)


def retained_format_two(root: Path, name: str) -> SlotIdentity:
    """Register actual bounded fixture files; this does not supply an executable format-2 reader."""
    initial = _slot(root, name)
    declaration = Path(initial.package.root) / "compatibility.json"
    values = json.loads(declaration.read_bytes())
    for domain in ("objects", "ingress"):
        values["formats"][domain] = {"reads": [1, 2], "writes": 2}
    declaration.write_text(json.dumps(values), encoding="utf-8")
    slot = register_local_slot(
        Path(initial.prefix), Path(initial.lexical_python), Path(initial.package.root)
    )
    verify_slot_files(slot)
    assert slot.digest != initial.digest
    assert assess_compatibility(
        slot.compatibility, slot.compatibility, project_format(), handoff_protocol=2
    ).format_compatible
    return slot


@dataclass(frozen=True, slots=True)
class TransitionHost:
    host: BootHost
    runtime: Runtime
    source: TerminalTransfer
    prepare: ManagedPreparation
    probe: TerminalProbe

    def plan(self, fallback: SlotIdentity | None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": "prepare-project-format-transition",
            "action": "project_review_1_to_2",
            "status": "pending",
            "routing": FrozenRouting.independent(
                self.runtime.config.settings, self.runtime.store.db
            ).dump(),
        }
        if fallback is not None:
            # This new action's fixed digest names an already retained slot.
            # No future fallback= keyword, arbitrary path or supplied SlotIdentity DTO.
            item["fallback_installation"] = fallback.digest
        self.runtime.store.put("management", item)
        return item


@pytest.fixture
async def transition_host(
    boot_host: BootHost, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TransitionHost]:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    session = BootSession(permit.journal, permit.lease, permit.config_path, permit)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_session=session)
    try:
        projected = project_final(
            permit, runtime.store, verify_binding=runtime.maintenance.verify_managed
        )
        assert projected == ProjectionReceipt(
            **source.record.position.model_dump(), store_instance=runtime.store.instance_id
        )
        runtime.maintenance.reconcile()
        assert not runtime.maintenance.configuration_blocked
        probe = TerminalProbe()
        probe.boot = permit.boot
        subject = ManagedPreparation(
            runtime,
            source.service,
            probe=probe,
            clock=lambda: ClockReading(boot_id="kernel", wall=1200.0, monotonic=200.0),
        )
        async with runtime.configuration_lock:
            observed = await subject.observe_current()
        observed.verify()
        assert observed.record == source.record
        assert observed.slot.digest == source.anchor.controller_slot == boot_host.target.digest
        assert permit.boot != source.record.identity.origin_boot
        assert read_format_evidence(runtime.store.db).state == CURRENT_FORMAT_STATE
        yield TransitionHost(boot_host, runtime, source, subject, probe)
    finally:
        await runtime.close()


def material_bytes(subject: TransitionHost) -> dict[str, bytes]:
    root = subject.host.journal.directory.path
    return {
        str(path): path.read_bytes()
        for path in (
            *sorted(path for path in root.iterdir() if path.is_file()),
            Path(subject.source.anchor.config_path),
            Path(subject.source.anchor.unit_path),
        )
    }


def marker(subject: TransitionHost) -> str:
    row = subject.runtime.store.db.execute(
        "SELECT value FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    return row[0]


async def test_formal_project_format_prepare_freezes_three_roles_without_migration(
    transition_host: TransitionHost,
) -> None:
    subject = transition_host
    root = subject.host.journal.database.parent.parent
    candidate = retained_format_two(root, "candidate-two")
    fallback = retained_format_two(Path(candidate.prefix).parent, "fallback-two")
    with DeploymentStore(subject.host.journal.directory.path) as retained:
        retained.publish_slot(fallback, guard=lambda: None)
        assert retained.read_slot(fallback.digest) == fallback
    item = subject.plan(fallback)
    previous = subject.host.journal.read_current()
    receipt = read_projection(subject.runtime.store)
    original = material_bytes(subject)
    original_marker = marker(subject)
    inode = os.stat(subject.host.journal.database)
    candidate_paths = LocalSlotPaths(
        Path(candidate.prefix), Path(candidate.lexical_python), Path(candidate.package.root)
    )

    async with subject.runtime.configuration_lock:
        prepared = await subject.prepare.prepare(
            item, subject.runtime.config.settings, candidate=candidate_paths
        )

    record = prepared.record
    values = record.model_dump(mode="json")
    assert record.action == "project_review_1_to_2" and record.phase == "prepared"
    assert subject.host.journal.read_current() == record
    assert prepared.anchor == subject.source.anchor
    assert previous == subject.source.record and receipt is not None
    assert values["source"]["binding"]["installation"] == subject.host.target.digest
    assert values["source"]["projected"] == receipt.model_dump(mode="json")
    assert values["candidate"]["installation"] == candidate.digest
    assert values["fallback"]["installation"] == fallback.digest
    assert len({subject.host.target.digest, candidate.digest, fallback.digest}) == 3
    assert values["base"] == values["source"]["binding"]
    assert values["target"] == values["candidate"]
    assert (
        values["source_format"]
        == values["observed_format"]
        == CURRENT_FORMAT_STATE.model_dump(mode="json")
    )
    assert values["target_format"] == project_format().model_dump(mode="json")
    assert isinstance(record, ProjectFormatTransitionRecord)
    assert record.revision == 1 and record.intent == "none" and record.activation == "pending"
    assert not record.resolved and record.boot_role is None
    assert record.migration.database.device == inode.st_dev
    assert record.migration.database.inode == inode.st_ino
    assert record.migration.source_marker == _snapshot(original_marker.encode(), _METADATA_BYTES)
    assert record.migration.target_marker == _snapshot(
        project_format().model_dump_json().encode(), _METADATA_BYTES
    )
    for role, installation in (
        ("source", subject.host.target.digest),
        ("candidate", candidate.digest),
        ("fallback", fallback.digest),
    ):
        selected = values[role]["binding"] if role == "source" else values[role]
        for component in ("configuration.json", "selector.json", "unit.bin"):
            path = subject.host.journal.directory.path / f"{record.generation}-{role}-{component}"
            data = path.read_bytes()
            assert data
            expected_reference = selected[
                "unit" if component == "unit.bin" else component.removesuffix(".json")
            ]
            actual_reference = (
                raw_snapshot(data, subject.host.journal.limits.max_snapshot_bytes)
                if component == "unit.bin"
                else _snapshot(data, subject.host.journal.limits.max_snapshot_bytes)
            )
            assert actual_reference.model_dump(mode="json") == expected_reference
            if component == "selector.json":
                assert SelectorValue.model_validate_json(data) == SelectorValue(
                    deployment=prepared.anchor.digest, installation=installation
                )
        assert selected["installation"] == installation
    assert len(list(subject.host.journal.directory.path.glob(f"{record.generation}-*"))) == 9
    # Publication may GC genuinely unreferenced snapshots, but live source
    # files, marker, inode and the precise preceding projection do not move.
    for path in (
        Path(prepared.anchor.config_path),
        Path(prepared.anchor.unit_path),
        subject.host.journal.directory.path / "selector.json",
    ):
        assert path.read_bytes() == original[str(path)]
    assert marker(subject) == original_marker
    assert read_projection(subject.runtime.store) == receipt
    current_inode = os.stat(subject.host.journal.database)
    assert (current_inode.st_dev, current_inode.st_ino) == (inode.st_dev, inode.st_ino)
    assert read_format_evidence(subject.runtime.store.db).state == CURRENT_FORMAT_STATE


@pytest.mark.parametrize("change", ["missing", "format-one"])
async def test_formal_project_format_prepare_rejects_unusable_fallback_before_effects(
    transition_host: TransitionHost, change: str
) -> None:
    subject = transition_host
    root = subject.host.journal.database.parent.parent
    candidate = retained_format_two(root, "candidate-two")
    # A real, retained format-1 slot is not a format-2 fallback, even though
    # its bytes, compatibility declaration and predecessor identity are valid.
    fallback = subject.host.target if change == "format-one" else None
    item = subject.plan(fallback)
    before = material_bytes(subject)
    event_position = subject.runtime.store.event_position()
    changes = subject.runtime.store.db.total_changes
    raw_marker = marker(subject)
    receipt = read_projection(subject.runtime.store)
    candidate_paths = LocalSlotPaths(
        Path(candidate.prefix), Path(candidate.lexical_python), Path(candidate.package.root)
    )
    with pytest.raises(
        ValueError,
        match="^managed_project_fallback_required$"
        if change == "missing"
        else "^current_format_unreadable$",
    ):
        async with subject.runtime.configuration_lock:
            await subject.prepare.prepare(
                item, subject.runtime.config.settings, candidate=candidate_paths
            )
    assert material_bytes(subject) == before
    assert subject.runtime.store.db.total_changes == changes
    assert subject.runtime.store.event_position() == event_position
    assert marker(subject) == raw_marker
    assert read_projection(subject.runtime.store) == receipt
    assert subject.host.journal.read_current() == subject.source.record
    assert subject.runtime.store.get("management", item["id"]) == item


async def prepared_transition(subject: TransitionHost) -> ProjectFormatTransitionRecord:
    root = subject.host.journal.database.parent.parent
    candidate = retained_format_two(root, "candidate-two")
    fallback = retained_format_two(root, "fallback-two")
    with DeploymentStore(subject.host.journal.directory.path) as retained:
        retained.publish_slot(fallback, guard=lambda: None)
    item = subject.plan(fallback)
    async with subject.runtime.configuration_lock:
        prepared = await subject.prepare.prepare(
            item,
            subject.runtime.config.settings,
            candidate=LocalSlotPaths(
                Path(candidate.prefix), Path(candidate.lexical_python), Path(candidate.package.root)
            ),
        )
    assert isinstance(prepared.record, ProjectFormatTransitionRecord)
    return prepared.record


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "nested-extra",
        "missing-source",
        "missing-candidate",
        "missing-fallback",
        "candidate-is-source",
        "base-is-fallback",
        "target-is-fallback",
        "source-format-two",
        "observed-format-two",
        "target-format-one",
        "marker-mismatch",
        "migration-version",
        "database-inode",
        "revision",
        "phase",
        "intent",
        "activation",
        "rollback",
        "rollback-int",
    ],
)
async def test_project_format_parser_strict_prepared_materials(
    transition_host: TransitionHost, change: str
) -> None:
    record = await prepared_transition(transition_host)
    values = deepcopy(record.model_dump(mode="json"))
    if change == "extra":
        values["untrusted"] = True
    elif change == "nested-extra":
        values["source"]["untrusted"] = True
    elif change.startswith("missing-"):
        del values[change.removeprefix("missing-")]
    elif change == "candidate-is-source":
        values["candidate"] = values["target"] = deepcopy(values["source"]["binding"])
    elif change == "base-is-fallback":
        values["base"] = deepcopy(values["fallback"])
    elif change == "target-is-fallback":
        values["target"] = deepcopy(values["fallback"])
    elif change == "source-format-two":
        values["source_format"] = project_format().model_dump(mode="json")
    elif change == "observed-format-two":
        values["observed_format"] = project_format().model_dump(mode="json")
    elif change == "target-format-one":
        values["target_format"] = CURRENT_FORMAT_STATE.model_dump(mode="json")
    elif change == "marker-mismatch":
        values["migration"]["target_marker"] = values["migration"]["source_marker"]
    elif change == "migration-version":
        values["migration"]["version"] = 2
    elif change == "database-inode":
        values["migration"]["database"]["inode"] = 0
    elif change == "revision":
        values["revision"] = 2
    elif change == "phase":
        values["phase"] = "draining"
    elif change == "intent":
        values["intent"] = "stop_base"
    elif change == "activation":
        values["activation"] = "released"
    elif change == "rollback":
        values["rollback_attempted"] = True
    else:
        assert change == "rollback-int"
        values["rollback_attempted"] = 0
    with pytest.raises(TransactionError, match="^invalid_record$"):
        parse_managed_record(json.dumps(values).encode())
    assert transition_host.host.journal.read_current() == record


async def test_project_format_prepared_does_not_widen_ordinary_transfer_or_effects(
    transition_host: TransitionHost,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    raw = record.model_dump_json().encode()
    assert parse_managed_record(raw) == record
    with pytest.raises(TransactionError, match="^invalid_record$"):
        parse_record_v2(raw)
    with pytest.raises(ValueError):
        ProjectControllerTransferRecord.model_validate_json(raw)
    ordinary_values = subject.source.record.model_dump()
    ordinary_values.pop("origin")
    ordinary_values["action"] = "restart"
    ordinary = TransactionRecordV2.model_validate(ordinary_values)
    assert parse_record_v2(ordinary.model_dump_json().encode()) == ordinary
    for field in ("source", "migration"):
        injected = {
            **ordinary.model_dump(mode="json"),
            field: record.model_dump(mode="json")[field],
        }
        with pytest.raises(TransactionError, match="^invalid_record$"):
            parse_managed_record(json.dumps(injected).encode())
    changed_transfer = subject.source.record.model_dump(mode="json")
    changed_transfer["observed_format"] = project_format().model_dump(mode="json")
    changed_transfer["health"]["format_digest"] = format_digest(project_format())
    with pytest.raises(ValueError):
        ProjectControllerTransferRecord.model_validate(changed_transfer)
    before = material_bytes(subject)
    changes = subject.runtime.store.db.total_changes
    receipt = read_projection(subject.runtime.store)
    permit = subject.runtime.maintenance.permit
    assert isinstance(permit, ManagedBootPermit)
    with pytest.raises(ValueError, match="^project_format_transition_unsupported$"):
        subject.runtime.maintenance.current()
    with pytest.raises(ValueError, match="^project_format_transition_unsupported$"):
        project_final(
            permit, subject.runtime.store, verify_binding=subject.runtime.maintenance.verify_managed
        )
    with pytest.raises(ManagedBootError, match="^managed_boot_not_permitted$"):
        permit._record()
    with pytest.raises(TransactionError, match="^managed_transaction_required$"):
        read_helper_entry(subject.host.journal.directory.path)
    with subject.host.journal.locked() as control:
        with pytest.raises(TransactionError, match="^managed_transaction_required$"):
            advance_managed(control, record.position, TransitionV2(phase="draining"))
        with pytest.raises(TransactionError, match="^managed_transaction_required$"):
            read_helper_record(control, subject.source.anchor)
    assert material_bytes(subject) == before
    assert subject.runtime.store.db.total_changes == changes
    assert read_projection(subject.runtime.store) == receipt


async def test_project_format_directory_budget_rejects_before_plan_or_snapshot_write(
    transition_host: TransitionHost,
) -> None:
    subject = transition_host
    root = subject.host.journal.database.parent.parent
    candidate = retained_format_two(root, "candidate-two")
    fallback = retained_format_two(root, "fallback-two")
    with DeploymentStore(subject.host.journal.directory.path) as retained:
        retained.publish_slot(fallback, guard=lambda: None)
    item = subject.plan(fallback)
    journal_root = subject.host.journal.directory.path
    # Fill only unrelated names. The independent entry union cannot fit the
    # candidate registration, nine snapshots, state and atomic temporary item.
    free = subject.host.journal.limits.max_entries - len(list(journal_root.iterdir())) - 1
    for index in range(free):
        (journal_root / f"foreign-{index}.json").write_bytes(b"{}")
    before = material_bytes(subject)
    changes = subject.runtime.store.db.total_changes
    with pytest.raises(TransactionError, match="^directory_budget$"):
        async with subject.runtime.configuration_lock:
            await subject.prepare.prepare(
                item,
                subject.runtime.config.settings,
                candidate=LocalSlotPaths(
                    Path(candidate.prefix),
                    Path(candidate.lexical_python),
                    Path(candidate.package.root),
                ),
            )
    assert material_bytes(subject) == before
    assert subject.runtime.store.db.total_changes == changes
    assert subject.runtime.store.get("management", item["id"]) == item
    assert subject.host.journal.read_current() == subject.source.record
