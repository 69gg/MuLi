from __future__ import annotations

import sys
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest
from test_managed_boot import Host as BootHost
from test_managed_boot import _snapshot as database_snapshot
from test_managed_boot import host as boot_host
from test_managed_preparation import Probe
from test_managed_transaction import deadline, move, prepared, proof, verifying

from muli import managed_preparation, managed_runtime_binding
from muli.maintenance import MaintenanceError
from muli.managed_boot import ManagedBootError, ManagedBootPermit
from muli.managed_journal import RoleSnapshots, advance_managed
from muli.managed_preparation import ManagedPreparation
from muli.managed_runtime_binding import RuntimeBindingError, verify_running_binding
from muli.managed_transaction import (
    HealthProofV2,
    ProjectControllerTransferRecord,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    TransferOrigin,
    TransitionV2,
    format_digest,
    raw_snapshot,
)
from muli.management_projection import project_final, read_projection
from muli.management_transaction import (
    ClockReading,
    ProjectionReceipt,
    Role,
    TransactionJournal,
    _snapshot,
)
from muli.platforms.linux_bootstrap import BootSession
from muli.platforms.linux_transaction import ServiceSnapshot
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    EntryPlan,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    resolve_entry,
)
from muli.platforms.managed_probe import (
    ManagedProbeResult,
    ManagedUserService,
    managed_process_arguments,
)
from muli.proactive import FrozenRouting
from muli.runtime import Runtime

__all__ = ["boot_host"]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed admission")

_FRESH_BOOT = "fresh-runtime-after-transfer"
_HISTORICAL_BOOT = "historical-transfer-role"
_ORIGIN_BOOT = "bridge-before-controller-transfer"


@dataclass(frozen=True, slots=True)
class TerminalTransfer:
    record: ProjectControllerTransferRecord
    anchor: DeploymentAnchor
    service: ManagedUserService
    origin_receipt: ProjectionReceipt


class TerminalProbe(Probe):
    """Reuse the controlled probe; distinguish loaded launcher from app argv."""

    async def read_managed(
        self,
        journal: TransactionJournal,
        record: TransactionRecordV2 | ProjectControllerTransferRecord,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        service = ManagedUserService(
            Path(anchor.config_path),
            Path(anchor.data_dir),
            Path(anchor.unit_path).parent,
            controller=controller,
            launcher=Path(anchor.launcher.path),
            journal_root=journal.directory.path,
        )
        observed = await self.read(service)
        assert observed.process is not None
        process = replace(
            observed.process,
            executable=slots["target"].resolved_python.path,
            argv=managed_process_arguments(anchor, slots["target"]),
        )
        return ManagedProbeResult(
            ServiceSnapshot(observed.unit, process),
            ("base", "target"),
            frozenset({record.base.unit.digest, record.target.unit.digest}),
            record.target.unit.digest,
        )


def _role_material(
    host: BootHost, anchor: DeploymentAnchor, slot: SlotIdentity, unit: bytes
) -> tuple[RoleBinding, RoleSnapshots]:
    limit = host.journal.limits.max_snapshot_bytes
    configuration = Path(anchor.config_path).read_bytes()
    selector = SelectorValue(deployment=anchor.digest, installation=slot.digest).canonical()
    return (
        RoleBinding(
            entry="managed",
            installation=slot.digest,
            compatibility=slot.compatibility_digest,
            configuration=_snapshot(configuration, limit),
            selector=_snapshot(selector, limit),
            unit=raw_snapshot(unit, limit),
        ),
        RoleSnapshots(configuration, selector, unit),
    )


def _install_material(host: BootHost, data: RoleSnapshots, selector: SelectorValue) -> None:
    Path(host.anchor.config_path).write_bytes(data.configuration)
    Path(host.anchor.unit_path).write_bytes(data.unit)
    with SelectorStore(host.journal.directory.path) as selected:
        selected.replace(selected.read(), selector, guard=lambda: None)


def _publish_terminal_transfer(
    host: BootHost,
    phase: Literal["committed", "rolled_back"],
    *,
    released: bool = True,
) -> TerminalTransfer:
    """Freeze real files/SQLite plus a strict record; no controller is executed."""
    journal, limit = host.journal, host.journal.limits.max_snapshot_bytes
    old_service = ManagedUserService(
        Path(host.anchor.config_path),
        Path(host.anchor.data_dir),
        Path(host.anchor.unit_path).parent,
        controller=host.base,
        launcher=Path(host.anchor.launcher.path),
        journal_root=journal.directory.path,
    )
    old_unit = old_service.render().encode()
    old_anchor = DeploymentAnchor.model_validate(
        {**host.anchor.model_dump(), "managed_unit": raw_snapshot(old_unit, limit)}
    )
    new_service = ManagedUserService(
        Path(old_anchor.config_path),
        Path(old_anchor.data_dir),
        Path(old_anchor.unit_path).parent,
        controller=host.target,
        launcher=Path(old_anchor.launcher.path),
        journal_root=journal.directory.path,
    )
    new_unit = new_service.render().encode()
    anchor = DeploymentAnchor.model_validate(
        {
            **old_anchor.model_dump(),
            "controller_slot": host.target.digest,
            "managed_unit": raw_snapshot(new_unit, limit),
        }
    )
    assert old_anchor.controller_slot != anchor.controller_slot == host.target.digest
    assert old_unit != new_unit and old_anchor.launcher == anchor.launcher
    with DeploymentStore(journal.directory.path, journal.limits) as deployments:
        deployments.publish_anchor(old_anchor, guard=lambda: None)
        deployments.publish_anchor(anchor, guard=lambda: None)
    old_base, old_base_data = _role_material(host, old_anchor, host.base, old_unit)
    old_target, origin_data = _role_material(host, old_anchor, host.target, old_unit)
    before = verifying(
        prepared(
            "switch",
            request="before-real-transfer",
            generation="1" * 32,
            base=old_base,
            target=old_target,
            principal=ServiceIdentityV2(
                deployment=old_anchor.digest,
                store_instance=host.evidence.store_instance,
                main_session=host.evidence.main_session,
                origin_boot="earlier-installation",
            ),
        )
    )
    before = move(
        before,
        TransitionV2(phase="committed", proof=proof(before, boot=_ORIGIN_BOOT)),
    )
    before = move(
        before,
        TransitionV2(
            phase="committed",
            activation="released",
            proof=proof(before, boot=_ORIGIN_BOOT, admission="released"),
        ),
    )
    with journal.locked() as control:
        for name, data in {
            **old_base_data.entries(before.generation, "base"),
            **origin_data.entries(before.generation, "target"),
        }.items():
            control._replace(name, data)
        control._write(before)
    _install_material(
        host,
        origin_data,
        SelectorValue(deployment=old_anchor.digest, installation=host.target.digest),
    )
    old_permit = host.acquire(boot=_ORIGIN_BOOT)
    try:
        with closing(old_permit.open_store()) as store:
            store.put(
                "management",
                {
                    "id": before.request_id,
                    "action": before.action,
                    "status": "applying",
                    "transaction": before.public(),
                },
            )
            origin_receipt = project_final(
                old_permit,
                store,
                verify_binding=lambda record, role: verify_running_binding(
                    old_permit, store, record, role
                ),
            )
            assert origin_receipt is not None and read_projection(store) == origin_receipt
            # The original live writer freezes the new plan before its first
            # durable journal reference, just as ordinary preparation does.
            store.put(
                "management",
                {
                    "id": "real-controller-transfer",
                    "action": "project_controller_transfer",
                    "status": "applying",
                    "transaction": {"generation": "2" * 32},
                    "routing": FrozenRouting.independent(
                        old_permit.config.settings, store.db
                    ).dump(),
                },
            )
    finally:
        old_permit.close_after_writers()

    selected, selected_data = _role_material(host, anchor, host.target, new_unit)
    role: Role = "base" if phase == "rolled_back" else "target"
    nonce = "base-nonce" if role == "base" else "target-nonce"
    record = ProjectControllerTransferRecord(
        request_id="real-controller-transfer",
        generation="2" * 32,
        revision=8,
        action="project_controller_transfer",
        identity=ServiceIdentityV2(
            deployment=anchor.digest,
            store_instance=host.evidence.store_instance,
            main_session=host.evidence.main_session,
            origin_boot=_ORIGIN_BOOT,
        ),
        origin=TransferOrigin(
            deployment=old_anchor.digest, binding=old_target, projected=origin_receipt
        ),
        base=selected,
        target=selected,
        observed_format=host.evidence.state,
        deadline=deadline(),
        phase=phase,
        challenge="target-nonce",
        rollback_challenge="base-nonce" if role == "base" else None,
        rollback_attempted=role == "base",
        activation="released" if released else "pending",
        health=HealthProofV2(
            generation="2" * 32,
            challenge=nonce,
            boot=_HISTORICAL_BOOT,
            role=role,
            deployment=anchor.digest,
            installation=host.target.digest,
            compatibility=host.target.compatibility_digest,
            configuration=selected.configuration.digest,
            selector=selected.selector.digest if selected.selector else None,
            unit=selected.unit.digest,
            store_instance=host.evidence.store_instance,
            main_session=host.evidence.main_session,
            format_digest=format_digest(host.evidence.state),
            core_ready=True,
            admission="released" if released else "paused",
        ),
    )
    _install_material(
        host,
        selected_data,
        SelectorValue(deployment=anchor.digest, installation=host.target.digest),
    )
    with journal.locked() as control:
        origin_entries = {
            name.replace("-base-", "-origin-"): data
            for name, data in origin_data.entries(record.generation, "base").items()
        }
        entries = {
            **origin_entries,
            **selected_data.entries(record.generation, "base"),
            **selected_data.entries(record.generation, "target"),
        }
        assert len(entries) == 9
        for name, data in entries.items():
            control._replace(name, data)
        control._replace("state.json", record.model_dump_json().encode())
    assert journal.read_current() == record
    return TerminalTransfer(record, anchor, new_service, origin_receipt)


def _boot_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same filesystem portability boundary as the existing managed fixtures.
    # Do not patch any record reader, permit, Maintenance or projection checker.
    monkeypatch.setattr(managed_preparation, "verify_local_filesystems", lambda paths: ())
    monkeypatch.setattr(managed_runtime_binding, "verify_local_filesystems", lambda paths: ())


def _acquire_reopened_permit(
    host: BootHost, boot: str, *, plan: EntryPlan | None = None
) -> ManagedBootPermit:
    # The BootHost fixture retains its own descriptors. BootSession owns this
    # fresh handle set to the same journal directory/DB and original lease file.
    journal = TransactionJournal(
        host.journal.directory.path, host.journal.database, host.journal.limits, create=False
    )
    try:
        selected_plan = plan or resolve_entry(journal.directory.path, purpose="service")
        return ManagedBootPermit.acquire(
            journal, boot=boot, plan=selected_plan, lease=journal.writer_lease().__enter__()
        )
    except ManagedBootError as error:
        if error.code != "managed_boot_cleanup_failed":
            journal.__exit__()
        raise
    except BaseException:
        journal.__exit__()
        raise


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
async def test_terminal_transfer_real_permit_runtime_observe_and_restart(
    boot_host: BootHost,
    monkeypatch: pytest.MonkeyPatch,
    phase: Literal["committed", "rolled_back"],
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, phase)
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    assert type(permit) is ManagedBootPermit and permit.record == source.record
    assert permit.boot not in {_HISTORICAL_BOOT, source.record.identity.origin_boot}
    assert permit._record() == (source.record, "base" if phase == "rolled_back" else "target")
    session = BootSession(permit.journal, permit.lease, permit.config_path, permit)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_session=session)
    try:
        assert runtime.maintenance.permit is permit
        assert runtime.maintenance.current() == source.record
        assert read_projection(runtime.store) == source.origin_receipt
        assert runtime.maintenance.configuration_blocked
        changes = runtime.store.db.total_changes
        with pytest.raises(RuntimeBindingError, match="^runtime_binding_role_changed$"):
            verify_running_binding(permit, runtime.store, source.record, None)
        assert runtime.store.db.total_changes == changes
        state_bytes = (boot_host.journal.directory.path / "state.json").read_bytes()
        events = runtime.store.events()
        admission = runtime.maintenance.admission.allowed
        if permit.role == "base":
            # A rollback role must never borrow the target's primary nonce.
            with pytest.raises(MaintenanceError, match="^transaction_unauthorized$"):
                runtime.maintenance.readiness(
                    source.record.generation, permit.boot, source.record.challenge
                )
        nonce = (
            source.record.rollback_challenge if permit.role == "base" else source.record.challenge
        )
        assert nonce is not None
        # RPC dispatch now exists, but this Runtime has not sealed coverage.
        with pytest.raises(MaintenanceError, match="^maintenance_incomplete$"):
            runtime.maintenance.readiness(source.record.generation, permit.boot, nonce)
        assert runtime.store.db.total_changes == changes
        assert runtime.store.events() == events
        assert read_projection(runtime.store) == source.origin_receipt
        assert runtime.maintenance.admission.allowed == admission
        assert (boot_host.journal.directory.path / "state.json").read_bytes() == state_bytes
        projected = project_final(
            permit, runtime.store, verify_binding=runtime.maintenance.verify_managed
        )
        assert projected == ProjectionReceipt(
            **source.record.position.model_dump(), store_instance=runtime.store.instance_id
        )
        runtime.maintenance.reconcile()
        assert not runtime.maintenance.configuration_blocked
        probe = TerminalProbe()
        probe.boot = _FRESH_BOOT
        subject = ManagedPreparation(
            runtime,
            source.service,
            probe=probe,
            clock=lambda: ClockReading(boot_id="kernel", wall=1200.0, monotonic=200.0),
        )
        async with runtime.configuration_lock:
            observed = await subject.observe_current()
        assert observed.record == source.record
        assert observed.anchor == source.anchor and observed.slot == boot_host.target
        observed.verify()
        plan = {
            "id": "restart-after-terminal-transfer",
            "action": "restart",
            "status": "pending",
            "routing": FrozenRouting.independent(runtime.config.settings, runtime.store.db).dump(),
        }
        runtime.store.put("management", plan)
        async with runtime.configuration_lock:
            result = await subject.prepare(plan, runtime.config.settings)
        assert result.record.action == "restart" and result.record.phase == "prepared"
        assert result.record.identity.origin_boot == _FRESH_BOOT
        assert source.record.health is not None
        assert result.record.identity.origin_boot != source.record.health.boot
        assert result.record.identity.deployment == source.anchor.digest
        assert result.record.base == result.record.target == source.record.target
        assert result.anchor == source.anchor and probe.calls == 2
        assert boot_host.journal.read_current() == result.record
    finally:
        await runtime.close()


@pytest.mark.parametrize("released", [True, False], ids=["released", "pending-probation"])
async def test_terminal_transfer_old_entry_plan_is_only_routing(
    boot_host: BootHost, monkeypatch: pytest.MonkeyPatch, released: bool
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    assert source.record.health is not None
    # Publish an eligible old prefix, then advance only through the formal
    # Journal state API. A released->pending rewrite is not a legal history.
    starting = ProjectControllerTransferRecord.model_validate(
        {
            **source.record.model_dump(),
            "revision": source.record.revision - 2,
            "phase": "activating",
            "intent": "start_target",
            "activation": "pending",
            "health": None,
        }
    )
    with boot_host.journal.locked() as control:
        control._write(starting)
    plan = resolve_entry(boot_host.journal.directory.path, purpose="service")
    paused = HealthProofV2.model_validate(
        {**source.record.health.model_dump(), "admission": "paused"}
    )
    with boot_host.journal.locked() as control:
        verifying_record = advance_managed(
            control, starting.position, TransitionV2(phase="verifying")
        )
        changed = advance_managed(
            control, verifying_record.position, TransitionV2(phase="committed", proof=paused)
        )
        if released:
            changed = advance_managed(
                control,
                changed.position,
                TransitionV2(phase="committed", activation="released", proof=source.record.health),
            )
    assert isinstance(changed, ProjectControllerTransferRecord)
    before = database_snapshot(boot_host.journal.database)
    record_bytes = (boot_host.journal.directory.path / "state.json").read_bytes()
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT, plan=plan)
    assert plan.revision < permit.record.revision
    assert permit._record() == (changed, "target")
    assert changed.health is not None
    assert permit.record == changed and permit.boot != changed.health.boot
    assert permit.probation is (not released)
    assert database_snapshot(boot_host.journal.database) == before
    session = BootSession(permit.journal, permit.lease, permit.config_path, permit)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_session=session)
    try:
        assert runtime.maintenance.current() == changed
        assert runtime.maintenance.admission.allowed is released
        assert not runtime.started and not runtime.tasks
        assert runtime.maintenance.configuration_blocked
        assert read_projection(runtime.store) == source.origin_receipt
    finally:
        await runtime.close()
    assert database_snapshot(boot_host.journal.database) == before
    assert (boot_host.journal.directory.path / "state.json").read_bytes() == record_bytes


def test_terminal_transfer_origin_cannot_change_between_acquire_and_open_store(
    boot_host: BootHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    changed = ProjectControllerTransferRecord.model_validate(
        {
            **source.record.model_dump(),
            "origin": {**source.record.origin.model_dump(), "deployment": "f" * 64},
        }
    )
    with boot_host.journal.locked() as control:
        control._replace("state.json", changed.model_dump_json().encode())
    before = database_snapshot(boot_host.journal.database)
    try:
        # The unchanged revision first hits the existing EntryPlan raw-bytes
        # digest gate, before open_store's immutable-record comparison.
        with pytest.raises(ManagedBootError, match="^managed_entry_changed$"):
            permit.open_store()
        assert database_snapshot(boot_host.journal.database) == before
    finally:
        permit.close_after_writers()
        permit.journal.__exit__()


@pytest.mark.parametrize("invalid", ["origin-boot", "unreleased"])
async def test_terminal_transfer_boot_admission_distinguishes_origin_and_probation(
    boot_host: BootHost, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed", released=invalid != "unreleased")
    before = database_snapshot(boot_host.journal.database)
    record_bytes = (boot_host.journal.directory.path / "state.json").read_bytes()
    if invalid == "origin-boot":
        with pytest.raises(ManagedBootError, match="^managed_boot_not_permitted$"):
            unexpected = _acquire_reopened_permit(boot_host, source.record.identity.origin_boot)
            unexpected.close_after_writers()
            unexpected.journal.__exit__()
    else:
        permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
        assert permit.record == source.record and permit.role == "target"
        assert permit.probation and not source.record.resolved
        assert database_snapshot(boot_host.journal.database) == before
        session = BootSession(permit.journal, permit.lease, permit.config_path, permit)
        runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_session=session)
        try:
            assert runtime.maintenance.current() == source.record
            assert not runtime.maintenance.admission.allowed
            assert runtime.maintenance.configuration_blocked
            assert not runtime.started and not runtime.tasks
            assert read_projection(runtime.store) == source.origin_receipt
        finally:
            await runtime.close()
    assert database_snapshot(boot_host.journal.database) == before
    assert (boot_host.journal.directory.path / "state.json").read_bytes() == record_bytes


async def test_terminal_transfer_old_projection_blocks_observe_and_prepare_without_dml(
    boot_host: BootHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    session = BootSession(permit.journal, permit.lease, permit.config_path, permit)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_session=session)
    try:
        assert read_projection(runtime.store) == source.origin_receipt
        plan = {
            "id": "blocked-restart-after-transfer",
            "action": "restart",
            "status": "pending",
            "routing": FrozenRouting.independent(runtime.config.settings, runtime.store.db).dump(),
        }
        runtime.store.put("management", plan)
        probe = TerminalProbe()
        probe.boot = _FRESH_BOOT
        subject = ManagedPreparation(runtime, source.service, probe=probe)
        before = database_snapshot(boot_host.journal.database)
        changes = runtime.store.db.total_changes
        record_bytes = (boot_host.journal.directory.path / "state.json").read_bytes()
        async with runtime.configuration_lock:
            with pytest.raises(MaintenanceError, match="^transaction_in_progress$"):
                await subject.observe_current()
            with pytest.raises(MaintenanceError, match="^transaction_in_progress$"):
                await subject.prepare(plan, runtime.config.settings)
        assert runtime.store.db.total_changes == changes and probe.calls == 0
        assert database_snapshot(boot_host.journal.database) == before
        assert (boot_host.journal.directory.path / "state.json").read_bytes() == record_bytes
    finally:
        await runtime.close()


@pytest.mark.parametrize("drift", ["origin", "action-to-ordinary"])
async def test_terminal_transfer_runtime_rejects_changed_frozen_origin_before_probe(
    boot_host: BootHost, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    session = BootSession(permit.journal, permit.lease, permit.config_path, permit)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_session=session)
    try:
        receipt = project_final(
            permit, runtime.store, verify_binding=runtime.maintenance.verify_managed
        )
        runtime.maintenance.reconcile()
        assert not runtime.maintenance.configuration_blocked
        changed: TransactionRecordV2 | ProjectControllerTransferRecord
        if drift == "origin":
            changed = ProjectControllerTransferRecord.model_validate(
                {
                    **source.record.model_dump(),
                    "origin": {**source.record.origin.model_dump(), "deployment": "f" * 64},
                }
            )
        else:
            # A valid ordinary envelope must not erase this live permit's
            # immutable Transfer action merely by retaining its receipt position.
            changed = TransactionRecordV2.model_validate(
                {**source.record.model_dump(exclude={"origin"}), "action": "restart"}
            )
        assert changed.position == source.record.position
        with boot_host.journal.locked() as control:
            control._replace("state.json", changed.model_dump_json().encode())
        probe = TerminalProbe()
        probe.boot = _FRESH_BOOT
        subject = ManagedPreparation(runtime, source.service, probe=probe)
        before = database_snapshot(boot_host.journal.database)
        changes = runtime.store.db.total_changes
        with pytest.raises(RuntimeBindingError, match="^runtime_binding_record_changed$"):
            runtime.maintenance.verify_managed(changed, "target")
        assert runtime.maintenance.configuration_blocked
        async with runtime.configuration_lock:
            with pytest.raises(
                MaintenanceError, match="^managed_preparation_installation_changed$"
            ):
                subject._source(permit, None, "switch", None)
            with pytest.raises(MaintenanceError, match="^transaction_in_progress$"):
                await subject.observe_current()
        assert probe.calls == 0 and runtime.store.db.total_changes == changes
        assert read_projection(runtime.store) == receipt
        assert database_snapshot(boot_host.journal.database) == before
    finally:
        await runtime.close()
