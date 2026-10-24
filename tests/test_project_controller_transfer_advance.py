"""Pure transfer progression through the existing durable Journal boundary.

Fixture publication is not source admission. No backend, clock observation,
process, service, or actual start is supplied by these tests.
"""

from __future__ import annotations

import sys
from typing import Literal

import pytest
from test_managed_controller import FakeManagedService
from test_managed_journal import journal as journal
from test_managed_transaction import proof
from test_project_controller_transfer import TransferEvidence, _transfer_entries
from test_project_controller_transfer import transfer as transfer

from muli.managed_controller import _ManagedPolicy
from muli.managed_journal import advance_managed
from muli.managed_transaction import (
    HealthProofV2,
    ProjectControllerTransferRecord,
    TransitionV2,
)
from muli.management_transaction import JournalControl, Role, TransactionError, TransactionJournal

__all__ = ["journal", "transfer"]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Journal primitives")

_IMMUTABLE = {
    "protocol",
    "request_id",
    "generation",
    "action",
    "identity",
    "origin",
    "base",
    "target",
    "observed_format",
    "deadline",
    "challenge",
}


def _directory_bytes(journal: TransactionJournal) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes() for path in journal.directory.path.iterdir() if path.is_file()
    }


def _publish(
    journal: TransactionJournal, evidence: TransferEvidence
) -> ProjectControllerTransferRecord:
    record = ProjectControllerTransferRecord.model_validate(evidence.values)
    evidence.origin.verify(record.origin.binding, journal.limits.max_snapshot_bytes)
    evidence.selected.verify(record.base, journal.limits.max_snapshot_bytes)
    evidence.selected.verify(record.target, journal.limits.max_snapshot_bytes)
    entries = _transfer_entries(evidence)
    assert len(entries) == 9
    with journal.locked() as control:
        for name, data in entries.items():
            control._replace(name, data)
        # Only test material publication; no production Transfer prepare exists.
        control._replace("state.json", record.model_dump_json().encode())
    assert journal.read_current() == record
    return record


def _advance(
    control: JournalControl, record: ProjectControllerTransferRecord, change: TransitionV2
) -> ProjectControllerTransferRecord:
    updated = advance_managed(control, record.position, change)
    assert isinstance(updated, ProjectControllerTransferRecord)
    assert updated.revision == record.revision + 1
    assert updated.model_dump(include=_IMMUTABLE) == record.model_dump(include=_IMMUTABLE)
    assert control.read_current() == updated
    return updated


def _start_target(
    control: JournalControl, record: ProjectControllerTransferRecord
) -> ProjectControllerTransferRecord:
    for change in (
        TransitionV2(phase="draining"),
        TransitionV2(phase="activating", intent="stop_base"),
        TransitionV2(phase="activating", intent="install_target"),
        TransitionV2(phase="activating", intent="start_target"),
    ):
        record = _advance(control, record, change)
    return record


def _health(
    evidence: TransferEvidence,
    record: ProjectControllerTransferRecord,
    *,
    role: Role = "target",
    admission: Literal["paused", "released", "released_degraded"] = "paused",
) -> HealthProofV2:
    # Reuse the existing proof fixture's complete binding/format identity.
    # Transfer base == target; only the role, its independent nonce and boot
    # differ. The actual record passed to advance is never an ordinary record.
    assert record.base == record.target == evidence.ordinary.base
    assert record.identity == evidence.ordinary.identity
    template = proof(evidence.ordinary, boot=f"transfer-{role}-boot", admission=admission)
    nonce = record.challenge if role == "target" else record.rollback_challenge
    assert nonce is not None
    return HealthProofV2.model_validate({**template.model_dump(), "role": role, "challenge": nonce})


def test_transfer_journal_advances_complete_forward_path(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    initial = record = _publish(journal, transfer)
    snapshots = _transfer_entries(transfer)
    with journal.locked() as control:
        record = _start_target(control, record)
        record = _advance(control, record, TransitionV2(phase="verifying"))
        record = _advance(
            control,
            record,
            TransitionV2(phase="committed", proof=_health(transfer, record)),
        )
        assert record.activation == "pending" and not record.resolved
        record = _advance(
            control,
            record,
            TransitionV2(
                phase="committed",
                activation="released",
                proof=_health(transfer, record, admission="released"),
            ),
        )
    assert record.resolved and record.health is not None
    assert record.health.boot != initial.identity.origin_boot
    assert (
        record.origin == initial.origin
        and record.base.installation == initial.origin.binding.installation
    )
    assert record.identity.deployment != record.origin.deployment
    assert all(
        (journal.directory.path / name).read_bytes() == data for name, data in snapshots.items()
    )


def test_transfer_journal_stale_revision_cannot_write(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    initial = _publish(journal, transfer)
    with journal.locked() as control:
        backend = FakeManagedService(transfer.ordinary, transfer.selected)
        policy = _ManagedPolicy(control, backend)
        before = _directory_bytes(journal)
        with pytest.raises(TransactionError, match="^managed_transaction_required$"):
            policy.move(
                transfer.ordinary,
                phase="draining",
                intent="none",
                activation="pending",
                code=None,
                proof=None,
            )
        assert not backend.calls and _directory_bytes(journal) == before
        current = _advance(control, initial, TransitionV2(phase="draining"))
        before = _directory_bytes(journal)
        with pytest.raises(TransactionError, match="^stale_revision$"):
            advance_managed(
                control, initial.position, TransitionV2(phase="activating", intent="stop_base")
            )
        assert control.read_current() == current
        assert _directory_bytes(journal) == before


def test_transfer_journal_never_aborts_or_returns_to_prepared(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    record = _publish(journal, transfer)
    with journal.locked() as control:
        record = _advance(control, record, TransitionV2(phase="draining"))
        for invalid in (
            TransitionV2(phase="aborted", code="cancelled"),
            TransitionV2(phase="prepared", code="drain_timeout"),
        ):
            before = _directory_bytes(journal)
            with pytest.raises(TransactionError, match="^invalid_transition$"):
                advance_managed(control, record.position, invalid)
            assert _directory_bytes(journal) == before
        recovery = _advance(
            control, record, TransitionV2(phase="recovery_required", code="drain_timeout")
        )
        assert recovery.failure_code == recovery.code == "drain_timeout"
        assert not recovery.resolved and recovery.boot_role is None


def test_transfer_journal_one_rollback_keeps_nonce_and_first_failure(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    record = _publish(journal, transfer)
    with journal.locked() as control:
        record = _start_target(control, record)
        before = _directory_bytes(journal)
        with pytest.raises(TransactionError, match="^invalid_intent_transition$"):
            advance_managed(
                control, record.position, TransitionV2(phase="activating", intent="start_target")
            )
        assert _directory_bytes(journal) == before
        record = _advance(
            control,
            record,
            TransitionV2(phase="rolling_back", intent="stop_target", code="ambiguous_start"),
        )
        nonce = record.rollback_challenge
        assert record.rollback_attempted and nonce is not None and nonce != record.challenge
        before = _directory_bytes(journal)
        with pytest.raises(TransactionError, match="^invalid_intent_transition$"):
            advance_managed(
                control,
                record.position,
                TransitionV2(phase="rolling_back", intent="stop_target", code="backend_failed"),
            )
        assert _directory_bytes(journal) == before
        for change in (
            TransitionV2(phase="rolling_back", intent="restore_base", code="backend_failed"),
            TransitionV2(phase="rolling_back", intent="start_base", code="backend_failed"),
            TransitionV2(phase="rolling_back", intent="verify_base", code="backend_failed"),
        ):
            record = _advance(control, record, change)
            assert record.rollback_challenge == nonce
            assert record.failure_code == "ambiguous_start"
        record = _advance(
            control,
            record,
            TransitionV2(phase="rolled_back", proof=_health(transfer, record, role="base")),
        )
        record = _advance(
            control,
            record,
            TransitionV2(
                phase="rolled_back",
                activation="released",
                proof=_health(transfer, record, role="base", admission="released"),
            ),
        )
    assert record.resolved and record.failure_code == "ambiguous_start"
    assert record.rollback_challenge == nonce
    assert record.health is not None and record.health.role == "base"


def test_transfer_journal_requires_own_role_paused_then_release_proof(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    record = _publish(journal, transfer)
    with journal.locked() as control:
        record = _start_target(control, record)
        record = _advance(control, record, TransitionV2(phase="verifying"))
        valid = _health(transfer, record)
        for patch in (
            {"boot": record.identity.origin_boot},
            {"deployment": record.origin.deployment},
            {"role": "base"},
            {"challenge": "foreign-nonce"},
            {"format_digest": "f" * 64},
        ):
            invalid = HealthProofV2.model_validate({**valid.model_dump(), **patch})
            before = _directory_bytes(journal)
            with pytest.raises(TransactionError, match="^invalid_health_proof$"):
                advance_managed(
                    control, record.position, TransitionV2(phase="committed", proof=invalid)
                )
            assert _directory_bytes(journal) == before
        before = _directory_bytes(journal)
        with pytest.raises(TransactionError, match="^invalid_activation$"):
            advance_managed(
                control,
                record.position,
                TransitionV2(
                    phase="committed",
                    activation="released",
                    proof=_health(transfer, record, admission="released"),
                ),
            )
        assert _directory_bytes(journal) == before
        record = _advance(control, record, TransitionV2(phase="committed", proof=valid))
        assert not record.resolved and record.activation == "pending"
