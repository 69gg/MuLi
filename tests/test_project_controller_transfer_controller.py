"""Transfer driver evidence, with real Journal and controlled effects only."""

from __future__ import annotations

import sys

import pytest
from test_managed_controller import FakeClock
from test_managed_journal import journal as journal
from test_project_controller_transfer import TransferEvidence
from test_project_controller_transfer import transfer as transfer
from test_project_controller_transfer_advance import _health, _publish

from muli.managed_controller import (
    BindingConfirmation,
    ManagedProcessIdentity,
    ManagedServiceObservation,
    TransferTransactionController,
    _files,
    _process,
)
from muli.managed_journal import RoleSnapshots
from muli.managed_transaction import ProjectControllerTransferRecord, raw_snapshot
from muli.management_transaction import Role, TransactionError, TransactionJournal, _snapshot

__all__ = ["journal", "transfer"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Journal/flock")


class Effects:
    def __init__(self, record: ProjectControllerTransferRecord, evidence: TransferEvidence) -> None:
        self.evidence = evidence
        self.data = evidence.origin
        self.process: ManagedProcessIdentity | None = _process(record.origin.binding)
        self.boot: str | None = record.identity.origin_boot
        self.role: Role = "target"
        self.drained = self.released = False
        self.confirmation: BindingConfirmation | None = None
        self.calls: list[str] = []
        self.mode = "normal"

    async def observe(self, record: ProjectControllerTransferRecord) -> ManagedServiceObservation:
        health = None
        origin = self.boot == record.identity.origin_boot
        if self.boot is not None and not origin:
            health = _health(
                self.evidence,
                record,
                role=self.role,
                admission="released" if self.released else "paused",
            )
        return ManagedServiceObservation(
            identity_matches=True,
            configuration=_snapshot(self.data.configuration, 4096).digest,
            selector=_snapshot(self.data.selector, 4096).digest if self.data.selector else None,
            unit=raw_snapshot(self.data.unit, 4096).digest,
            service="running" if self.process else "stopped",
            boot=self.boot,
            writer_free=self.process is None,
            process=self.process,
            drained=self.drained,
            confirmation=self.confirmation,
            health=health,
            health_failed=not origin and self.role == "target" and self.mode == "rollback",
        )

    async def drain(self, record: ProjectControllerTransferRecord) -> None:
        assert self.boot == record.identity.origin_boot and self.data == self.evidence.origin
        self.calls.append("drain:origin")
        self.drained = True

    async def resume(self, record: ProjectControllerTransferRecord) -> None:
        pytest.fail("Transfer must never resume the old authority")

    async def stop(self, record: ProjectControllerTransferRecord, role: Role) -> None:
        if self.boot == record.identity.origin_boot:
            assert self.data == self.evidence.origin
        self.calls.append(
            "stop:" + ("origin" if self.boot == record.identity.origin_boot else role)
        )
        self.process, self.boot, self.confirmation = None, None, None

    async def install(
        self, record: ProjectControllerTransferRecord, role: Role, data: RoleSnapshots
    ) -> None:
        assert self.process is None and data == self.evidence.selected
        self.calls.append("install:" + role)
        self.data = data
        self.confirmation = BindingConfirmation(
            record.position,
            role,
            *_files(record.binding(role)),
            loaded_unit=record.binding(role).unit.digest,
        )

    async def start(self, record: ProjectControllerTransferRecord, role: Role) -> None:
        assert self.process is None and self.data == self.evidence.selected
        self.calls.append("start:" + role)
        if self.mode == "lost-start" and role == "target":
            return  # Durable intent has no proof the command reached a process.
        self.process = _process(record.binding(role))
        self.boot, self.role = f"transfer-{role}-boot", role
        self.confirmation, self.released = None, False

    async def activate(self, record: ProjectControllerTransferRecord, role: Role) -> None:
        assert self.boot != record.identity.origin_boot
        self.calls.append("activate:" + role)
        self.released = True


@pytest.mark.parametrize("mode", ["normal", "rollback", "lost-start"])
async def test_transfer_driver_keeps_old_authority_separate_and_starts_each_intent_once(
    journal: TransactionJournal, transfer: TransferEvidence, mode: str
) -> None:
    initial = _publish(journal, transfer)
    effects = Effects(initial, transfer)
    effects.mode = mode
    with journal.locked() as control:
        driver = TransferTransactionController(control, effects, FakeClock())
        for _ in range(28):
            final = await driver.step()
            assert final.origin == initial.origin and final.deadline == initial.deadline
            if final.resolved or final.phase == "recovery_required":
                break
        else:
            pytest.fail("Transfer did not converge within the existing fixture bound")
    assert final.resolved
    assert final.phase == ("committed" if mode == "normal" else "rolled_back")
    assert effects.calls.count("stop:origin") == effects.calls.count("drain:origin") == 1
    assert effects.calls.count("start:target") == 1
    assert effects.calls.count("start:base") == (0 if mode == "normal" else 1)
    assert effects.data == transfer.selected
    assert final.failure_code == (
        None if mode == "normal" else "health_failed" if mode == "rollback" else "ambiguous_start"
    )


async def test_transfer_drain_reserve_never_aborts_or_resumes(
    journal: TransactionJournal, transfer: TransferEvidence
) -> None:
    initial = _publish(journal, transfer)
    effects, clock = Effects(initial, transfer), FakeClock()
    clock.elapsed = initial.deadline.seconds - initial.deadline.rollback_reserve
    with journal.locked() as control:
        final = await TransferTransactionController(control, effects, clock).step()
    assert final.phase == "recovery_required" and final.failure_code == "drain_timeout"
    assert not effects.calls and effects.data == transfer.origin


@pytest.mark.parametrize(
    "damage", ["origin-as-base", "reverse-prefix", "busy-prefix", "changed-origin"]
)
async def test_transfer_driver_rejects_wrong_authority_or_unowned_prefix(
    journal: TransactionJournal, transfer: TransferEvidence, damage: str
) -> None:
    initial = _publish(journal, transfer)
    effects = Effects(initial, transfer)
    with journal.locked() as control:
        driver = TransferTransactionController(control, effects, FakeClock())
        if damage == "origin-as-base":
            effects.data = transfer.selected
        elif damage == "changed-origin":
            await driver.step()
            current = control.read_current()
            assert isinstance(current, ProjectControllerTransferRecord)
            changed = ProjectControllerTransferRecord.model_validate(
                {
                    **current.model_dump(),
                    "origin": {
                        **current.origin.model_dump(),
                        "projected": {
                            **current.origin.projected.model_dump(),
                            "revision": current.origin.projected.revision + 1,
                        },
                    },
                }
            )
            control._replace("state.json", changed.model_dump_json().encode())
            with pytest.raises(TransactionError, match="transaction_changed"):
                await driver.step()
            assert not effects.calls
            return
        else:
            for _ in range(5):
                current = await driver.step()
            assert (current.phase, current.intent) == ("activating", "install_target")
            effects.data = RoleSnapshots(
                transfer.origin.configuration, transfer.origin.selector, transfer.selected.unit
            )
            if damage == "busy-prefix":
                # A valid forward X prefix cannot be reconciled while a writer lives.
                effects.data = RoleSnapshots(
                    transfer.origin.configuration, transfer.selected.selector, transfer.origin.unit
                )
                effects.process = _process(initial.origin.binding)
                effects.boot = initial.identity.origin_boot
        before = list(effects.calls)
        final = await driver.step()
    assert final.phase == "recovery_required" and final.failure_code == "foreign_state"
    assert effects.calls == before
