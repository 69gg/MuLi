from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

import pytest
from pydantic import ValidationError
from test_managed_journal import journal as journal
from test_managed_preparation import Host as PreparationHost
from test_managed_preparation import host as preparation_host
from test_managed_transaction import (
    CONFIG,
    UNIT,
    binding,
    deadline,
    manifest,
    move,
    prepared,
    proof,
    receipt,
    verifying,
)
from test_management_projection import Host as ProjectionHost
from test_management_projection import host as projection_host

from muli.maintenance import Maintenance, MaintenanceError
from muli.managed_journal import RoleSnapshots, prepare_managed
from muli.managed_transaction import (
    ProjectControllerTransferRecord,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransitionV2,
    parse_managed_record,
    parse_record_v2,
    raw_snapshot,
)
from muli.management_projection import ProjectionError, project_final, read_projection
from muli.management_transaction import (
    JournalLimits,
    Role,
    TransactionError,
    TransactionJournal,
    _snapshot,
)
from muli.platforms.managed_deployment import SelectorValue

# Re-export the existing real temporary host fixtures under distinct names.
__all__ = ["preparation_host", "projection_host"]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal primitive")


@dataclass(frozen=True, slots=True)
class TransferEvidence:
    previous: TransactionRecordV2
    ordinary: TransactionRecordV2
    origin: RoleSnapshots
    selected: RoleSnapshots
    values: dict[str, object]

    @property
    def data(self) -> bytes:
        return json.dumps(self.values, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def transfer() -> TransferEvidence:
    """Complete parser evidence; it does not attest a real anchor or process."""
    bridge = binding("a")
    origin_selector = SelectorValue(
        deployment="d" * 64, installation=bridge.installation
    ).canonical()
    origin_binding = RoleBinding.model_validate(
        {**bridge.model_dump(), "selector": _snapshot(origin_selector, 4096)}
    )
    before = verifying(
        prepared(
            "restart",
            request="before-transfer",
            generation="1" * 32,
            base=origin_binding,
            target=origin_binding,
        )
    )
    committed = move(
        before,
        TransitionV2(phase="committed", proof=proof(before, boot="running-bridge")),
    )
    previous = move(
        committed,
        TransitionV2(
            phase="committed",
            activation="released",
            proof=proof(committed, boot="running-bridge", admission="released"),
        ),
    )
    assert previous.resolved and previous.health is not None

    selected_selector = SelectorValue(
        deployment="e" * 64, installation=bridge.installation
    ).canonical()
    selected_unit = b"[Service]\nExecStart=/bridge/python -I /retained/launcher.py\n"
    selected_binding = RoleBinding.model_validate(
        {
            **bridge.model_dump(),
            "selector": _snapshot(selected_selector, 4096),
            "unit": raw_snapshot(selected_unit, 4096),
        }
    )
    ordinary = prepared(
        "restart",
        request="transfer-current-bridge",
        generation="2" * 32,
        base=selected_binding,
        target=selected_binding,
        principal=ServiceIdentityV2(
            deployment="e" * 64,
            store_instance=previous.identity.store_instance,
            main_session=previous.identity.main_session,
            origin_boot=previous.health.boot,
        ),
    )
    values: dict[str, object] = {
        **ordinary.model_dump(mode="json"),
        "action": "project_controller_transfer",
        "origin": {
            "deployment": previous.identity.deployment,
            "binding": previous.target.model_dump(mode="json"),
            "projected": receipt(previous).model_dump(mode="json"),
        },
    }
    return TransferEvidence(
        previous=previous,
        ordinary=ordinary,
        origin=RoleSnapshots(CONFIG, origin_selector, UNIT),
        selected=RoleSnapshots(CONFIG, selected_selector, selected_unit),
        values=values,
    )


def test_journal_dispatches_complete_transfer_record(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    # First prove the existing fixture has a valid, released predecessor and a
    # valid ordinary envelope. Only the new action/origin require new dispatch.
    assert parse_record_v2(transfer.previous.model_dump_json().encode()) == transfer.previous
    assert parse_record_v2(transfer.ordinary.model_dump_json().encode()) == transfer.ordinary
    transfer.origin.verify(transfer.previous.target, journal.limits.max_snapshot_bytes)
    transfer.selected.verify(transfer.ordinary.base, journal.limits.max_snapshot_bytes)
    assert transfer.ordinary.base == transfer.ordinary.target
    assert transfer.ordinary.base.installation == transfer.previous.target.installation
    assert len(transfer.data) <= journal.limits.max_record_bytes

    generation = transfer.ordinary.generation
    origin_entries = {
        name.replace("-base-", "-origin-"): data
        for name, data in transfer.origin.entries(generation, "base").items()
    }
    entries = {
        **origin_entries,
        **transfer.selected.entries(generation, "base"),
        **transfer.selected.entries(generation, "target"),
    }
    assert len(entries) == 9
    with journal.locked() as control:
        control._write(transfer.previous)
        for name, data in entries.items():
            control._replace(name, data)
        # Fixture publication only: production preparation must authenticate
        # the live origin before it can authorize this durable replacement.
        control._replace("state.json", transfer.data)

    record = journal.read_current()
    assert record is not None and not isinstance(record, TransactionRecordV2)
    assert record.model_dump(mode="json") == transfer.values
    assert record.boot_role is None and not record.resolved
    with journal.locked() as control:
        assert control.read_current() == record
    assert (journal.directory.path / "state.json").read_bytes() == transfer.data


@pytest.mark.parametrize("reader", ["journal", "ordinary"])
def test_ordinary_v2_still_rejects_origin_field(
    journal: TransactionJournal,
    transfer: TransferEvidence,
    reader: Literal["journal", "ordinary"],
) -> None:
    ordinary = transfer.ordinary.model_dump_json().encode()
    assert parse_record_v2(ordinary) == transfer.ordinary
    with journal.locked() as control:
        control._replace("state.json", ordinary)
    assert journal.read_current() == transfer.ordinary

    values = {**transfer.ordinary.model_dump(mode="json"), "origin": transfer.values["origin"]}
    data = json.dumps(values).encode()
    with journal.locked() as control:
        control._replace("state.json", data)
    with pytest.raises(TransactionError, match="invalid_record"):
        if reader == "journal":
            journal.read_current()
        else:
            parse_record_v2(data)
    assert (journal.directory.path / "state.json").read_bytes() == data


def test_ordinary_parser_does_not_accept_transfer_action(transfer: TransferEvidence) -> None:
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_record_v2(transfer.data)


def _replace_field(
    values: dict[str, object], path: tuple[str, ...], value: object
) -> dict[str, object]:
    changed = deepcopy(values)
    current = changed
    for key in path[:-1]:
        nested = current[key]
        assert isinstance(nested, dict)
        current = nested
    current[path[-1]] = value
    return changed


@pytest.mark.parametrize(
    "path,value",
    [
        (("protocol",), True),
        (("protocol",), 2.0),
        (("protocol",), "2"),
        (("action",), "project_controller_Transfer"),
        (("action",), ["project_controller_transfer"]),
        (("revision",), True),
        (("revision",), 0),
        (("generation",), "not-a-snapshot-generation"),
        (("unexpected",), "untrusted"),
        (("origin", "unexpected"), "untrusted"),
        (("origin", "deployment"), "e" * 64),
        (("origin", "projected", "generation"), "2" * 32),
        (("origin", "projected", "store_instance"), "another-store"),
        (("origin", "projected", "revision"), True),
        (("origin", "binding", "compatibility"), "f" * 64),
        (("origin", "binding", "configuration", "digest"), "f" * 64),
        (("origin", "binding", "selector"), None),
        (("origin", "binding", "entry"), "legacy"),
        (("target", "installation"), "b" * 64),
        (("target", "unit", "digest"), "f" * 64),
        (("base", "selector", "semantic_digest"), "f" * 64),
        (("observed_format", "metadata_version"), True),
        (("observed_format", "formats", "objects"), 2),
        (("observed_format", "formats", "ingress"), 2),
        (("observed_format", "formats", "memory"), 2),
        (("phase",), "aborted"),
        (("phase",), "activating"),
        (("phase",), "committed"),
        (("intent",), "stop_base"),
        (("activation",), "released"),
        (("rollback_challenge",), "rollback-nonce"),
        (("rollback_attempted",), True),
    ],
)
def test_transfer_dispatch_rejects_invalid_fields(
    transfer: TransferEvidence, path: tuple[str, ...], value: object
) -> None:
    assert isinstance(parse_managed_record(transfer.data), ProjectControllerTransferRecord)
    values = _replace_field(transfer.values, path, value)
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_managed_record(json.dumps(values).encode())


@pytest.mark.parametrize(
    "phase,intent,role,rollback",
    [
        ("prepared", "none", None, False),
        ("draining", "none", None, False),
        ("activating", "stop_base", None, False),
        ("activating", "install_target", None, False),
        ("activating", "start_target", "target", False),
        ("verifying", "none", "target", False),
        ("rolling_back", "stop_target", None, True),
        ("rolling_back", "restore_base", None, True),
        ("rolling_back", "start_base", "base", True),
        ("rolling_back", "verify_base", "base", True),
        ("recovery_required", "none", None, False),
    ],
)
def test_transfer_boot_window_matches_retained_launcher(
    transfer: TransferEvidence,
    phase: str,
    intent: str,
    role: Role | None,
    rollback: bool,
) -> None:
    values = {
        **transfer.values,
        "phase": phase,
        "intent": intent,
        "rollback_attempted": rollback,
        "rollback_challenge": "rollback-nonce" if rollback else None,
    }
    record = parse_managed_record(json.dumps(values).encode())
    assert isinstance(record, ProjectControllerTransferRecord)
    assert record.boot_role == role and not record.resolved


def _terminal_values(
    transfer: TransferEvidence,
    phase: Literal["committed", "rolled_back"],
    activation: Literal["pending", "released", "released_degraded", "blocked"] = "pending",
) -> dict[str, object]:
    rollback = phase == "rolled_back"
    health = proof(transfer.ordinary, boot="replacement-bridge").model_dump(mode="json")
    health.update(
        role="base" if rollback else "target",
        challenge="rollback-nonce" if rollback else transfer.ordinary.challenge,
        admission=activation if activation in {"released", "released_degraded"} else "paused",
    )
    return {
        **deepcopy(transfer.values),
        "phase": phase,
        "rollback_attempted": rollback,
        "rollback_challenge": "rollback-nonce" if rollback else None,
        "activation": activation,
        "health": health,
    }


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
@pytest.mark.parametrize("activation", ["pending", "released", "released_degraded", "blocked"])
def test_transfer_terminal_requires_own_role_proof_and_release(
    transfer: TransferEvidence,
    phase: Literal["committed", "rolled_back"],
    activation: Literal["pending", "released", "released_degraded", "blocked"],
) -> None:
    values = _terminal_values(transfer, phase, activation)
    record = parse_managed_record(json.dumps(values).encode())
    assert isinstance(record, ProjectControllerTransferRecord)
    role: Role = "base" if phase == "rolled_back" else "target"
    assert record.boot_role == role
    assert record.resolved == (activation in {"released", "released_degraded"})
    assert record.health is not None
    assert record.accepts(record.health, role, released=record.resolved)
    public = record.public()
    assert public["action"] == "project_controller_transfer"
    assert not {"origin", "identity", "base", "target", "challenge", "health"} & public.keys()


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", True),
        ("role", "origin"),
        ("role", "base"),
        ("generation", "1" * 32),
        ("challenge", "different-nonce"),
        ("boot", "running-bridge"),
        ("deployment", "d" * 64),
        ("installation", "b" * 64),
        ("compatibility", "f" * 64),
        ("configuration", "f" * 64),
        ("selector", "f" * 64),
        ("unit", "f" * 64),
        ("store_instance", "another-store"),
        ("main_session", "another-main"),
        ("format_digest", "f" * 64),
        ("core_ready", False),
        ("admission", "released"),
    ],
)
def test_transfer_terminal_rejects_foreign_or_origin_health(
    transfer: TransferEvidence, field: str, value: object
) -> None:
    values = _terminal_values(transfer, "committed")
    assert isinstance(
        parse_managed_record(json.dumps(values).encode()), ProjectControllerTransferRecord
    )
    invalid = _replace_field(values, ("health", field), value)
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_managed_record(json.dumps(invalid).encode())


def test_transfer_nonce_pair_and_frozen_origin_cannot_change(transfer: TransferEvidence) -> None:
    values = {
        **transfer.values,
        "phase": "rolling_back",
        "intent": "stop_target",
        "rollback_attempted": True,
        "rollback_challenge": transfer.ordinary.challenge,
    }
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_managed_record(json.dumps(values).encode())
    record = parse_managed_record(transfer.data)
    assert isinstance(record, ProjectControllerTransferRecord)
    with pytest.raises(ValidationError):
        record.phase = "draining"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.identity.origin_boot = "different-boot"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.origin.deployment = "f" * 64  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.deadline.seconds = 900.0  # type: ignore[misc]


def test_transfer_dispatch_keeps_original_whole_record_budget(transfer: TransferEvidence) -> None:
    limits = JournalLimits(max_record_bytes=4096)
    assert len(transfer.data) <= limits.max_record_bytes
    exact = transfer.data + b" " * (limits.max_record_bytes - len(transfer.data))
    assert len(exact) == limits.max_record_bytes
    assert isinstance(parse_managed_record(exact, limits=limits), ProjectControllerTransferRecord)
    with pytest.raises(TransactionError, match="record_budget"):
        parse_managed_record(exact + b" ", limits=limits)
    with pytest.raises(TransactionError, match="record_budget"):
        parse_managed_record(b"{" * (limits.max_record_bytes + 1), limits=limits)


def test_transfer_dispatch_rejects_duplicate_keys(transfer: TransferEvidence) -> None:
    invalid = transfer.data.replace(b'"revision":1', b'"revision":1,"revision":1', 1)
    assert invalid != transfer.data
    with pytest.raises(TransactionError, match="invalid_record"):
        parse_managed_record(invalid)


@contextmanager
def _published(journal: TransactionJournal, data: bytes) -> Iterator[None]:
    """Restore the fixture's original record before its ordinary host closes."""
    path = journal.directory.path / "state.json"
    previous = path.read_bytes() if path.exists() else None
    with journal.locked() as control:
        control._replace("state.json", data)
    try:
        yield
    finally:
        with journal.locked() as control:
            if previous is None:
                os.unlink("state.json", dir_fd=control.directory.fd)
                os.fsync(control.directory.fd)
            else:
                control._replace("state.json", previous)


def test_ordinary_journal_prepare_rejects_transfer_before_snapshot_gc(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    stale_name = "0" * 32 + "-base-unit.bin"
    with journal.locked() as control:
        control._replace(stale_name, b"uncollected-fixture")
    with _published(journal, transfer.data), journal.writer_lease() as writer:
        names = {path.name for path in journal.directory.path.iterdir()}
        with journal.locked() as control:
            with pytest.raises(TransactionError, match="project_controller_transfer_unsupported"):
                prepare_managed(
                    control,
                    request_id="ordinary-after-transfer",
                    action="restart",
                    identity=transfer.ordinary.identity,
                    base=transfer.ordinary.base,
                    target=transfer.ordinary.target,
                    base_data=transfer.selected,
                    target_data=transfer.selected,
                    observed_format=transfer.ordinary.observed_format,
                    base_manifest=manifest(),
                    target_manifest=manifest(),
                    deadline=deadline(),
                    writer=writer,
                )
        assert {path.name for path in journal.directory.path.iterdir()} == names
        assert (journal.directory.path / stale_name).read_bytes() == b"uncollected-fixture"
        assert (journal.directory.path / "state.json").read_bytes() == transfer.data


def test_ordinary_maintenance_rejects_transfer_instead_of_v1_fallback(
    projection_host: ProjectionHost, transfer: TransferEvidence
) -> None:
    subject = Maintenance(projection_host.permit)
    allowed = subject.admission.allowed
    with _published(projection_host.journal, transfer.data):
        with pytest.raises(MaintenanceError, match="project_controller_transfer_unsupported"):
            subject.current()
        assert subject.admission.allowed == allowed
        assert subject.permit is projection_host.permit


@pytest.mark.parametrize("terminal", [False, True])
def test_ordinary_projection_rejects_transfer_without_writes_or_verification(
    projection_host: ProjectionHost, transfer: TransferEvidence, terminal: bool
) -> None:
    host = projection_host
    values = _terminal_values(transfer, "committed", "released") if terminal else transfer.values
    actual_instance, actual_main = host.store.instance_id, host.store.main_session()["id"]
    for path, value in (
        (("identity", "store_instance"), actual_instance),
        (("identity", "main_session"), actual_main),
        (("origin", "projected", "store_instance"), actual_instance),
    ):
        values = _replace_field(values, path, value)
    if terminal:
        values = _replace_field(values, ("health", "store_instance"), actual_instance)
        values = _replace_field(values, ("health", "main_session"), actual_main)
    data = json.dumps(values).encode()
    assert isinstance(parse_managed_record(data), ProjectControllerTransferRecord)
    verified: list[str] = []

    def verify(record: TransactionRecordV2, role: Role | None) -> None:
        verified.append(record.generation)

    changes = host.store.db.total_changes
    plans, events, projected = (
        host.store.list("management", 100),
        host.store.events(),
        read_projection(host.store),
    )
    with _published(host.journal, data):
        with pytest.raises(ProjectionError, match="project_controller_transfer_unsupported"):
            project_final(host.permit, host.store, verify_binding=verify)
        assert host.store.db.total_changes == changes
        assert host.store.list("management", 100) == plans
        assert host.store.events() == events
        assert read_projection(host.store) == projected
        assert not verified
        assert (host.journal.directory.path / "state.json").read_bytes() == data


async def test_ordinary_source_rejects_transfer_before_process_or_new_plan(
    preparation_host: PreparationHost, transfer: TransferEvidence
) -> None:
    host = preparation_host
    permit = host.runtime.maintenance.permit
    assert permit is not None
    before = host.runtime.store.db.total_changes
    calls = host.probe.calls
    with _published(host.journal, transfer.data):
        async with host.runtime.configuration_lock:
            with pytest.raises(MaintenanceError, match="project_controller_transfer_unsupported"):
                host.prepare._source(permit, None, "switch", None)
        assert host.runtime.store.db.total_changes == before
        assert host.probe.calls == calls
        assert (host.journal.directory.path / "state.json").read_bytes() == transfer.data
