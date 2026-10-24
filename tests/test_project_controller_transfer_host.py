"""Real transfer host/boot/origin boundaries; no process or service is executed."""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest
from test_managed_boot import Host
from test_managed_boot import host as boot_host
from test_managed_host import Probe
from test_managed_host import filesystems as filesystems
from test_management_helper import Runner
from test_project_controller_transfer_runtime import (
    _FRESH_BOOT,
    TerminalProbe,
    _acquire_reopened_permit,
    _boot_environment,
    _install_material,
    _publish_terminal_transfer,
)

from muli.maintenance import MaintenanceError
from muli.managed_boot import ManagedBootError, ManagedBootPermit
from muli.managed_host import managed_helper_arguments, recover
from muli.managed_journal import _read_snapshots, advance_managed
from muli.managed_runtime_binding import RuntimeBindingError
from muli.managed_transaction import (
    ProjectControllerTransferRecord,
    TransactionRecordV2,
    TransitionV2,
)
from muli.management_host import RecoveryHost
from muli.management_projection import read_projection
from muli.management_transaction import TransactionError
from muli.platforms.linux_bootstrap import open_boot_session
from muli.platforms.managed_deployment import DeploymentStore, SelectorStore, SelectorValue
from muli.platforms.managed_launcher import LaunchError
from muli.runtime import Runtime
from muli.service import UserService

__all__ = ["boot_host", "filesystems"]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux transfer host/lease")


def _pending_record(
    terminal: ProjectControllerTransferRecord,
    *,
    phase: Literal["prepared", "activating"] = "prepared",
) -> ProjectControllerTransferRecord:
    return ProjectControllerTransferRecord.model_validate(
        {
            **terminal.model_dump(),
            "revision": 1,
            "phase": phase,
            "intent": "start_target" if phase == "activating" else "none",
            "activation": "pending",
            "health": None,
        }
    )


def _publish(host: Host, record: ProjectControllerTransferRecord | TransactionRecordV2) -> None:
    # Exact test material publication only; preparation/source admission is a
    # separate slice. All nine snapshots and both real anchors remain intact.
    with host.journal.locked() as control:
        control._replace("state.json", record.model_dump_json().encode())
    assert host.journal.read_current() == record


def _restore_origin(host: Host, record: ProjectControllerTransferRecord) -> None:
    material = _read_snapshots(host.journal, record.generation, record.origin.binding, "origin")
    assert record.origin.binding != record.base == record.target
    assert (
        material.unit != _read_snapshots(host.journal, record.generation, record.base, "base").unit
    )
    _install_material(
        host,
        material,
        SelectorValue(
            deployment=record.origin.deployment,
            installation=record.origin.binding.installation,
        ),
    )


async def test_transfer_recover_routes_new_controller_with_origin_selector(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = _pending_record(source.record)
    _restore_origin(boot_host, record)
    _publish(boot_host, record)
    with DeploymentStore(boot_host.journal.directory.path) as retained:
        controller = retained.read_slot(source.anchor.controller_slot)
        origin_anchor = retained.read_anchor(record.origin.deployment)
    assert controller.digest == record.base.installation == boot_host.target.digest
    assert origin_anchor.controller_slot != controller.digest
    runner = Runner()
    assert (
        await recover(boot_host.journal.directory.path, probe=Probe(), runner=runner) == "accepted"
    )
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert tuple(argv[argv.index("--") + 1 :]) == managed_helper_arguments(
        source.anchor, controller
    )
    with SelectorStore(boot_host.journal.directory.path) as selected:
        actual = selected.read()
        assert actual is not None and actual.value.deployment == record.origin.deployment
    assert boot_host.journal.read_current() == record


def test_transfer_start_target_opens_real_probationary_permit(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = _pending_record(source.record, phase="activating")
    _publish(boot_host, record)
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    try:
        assert permit.record == record and permit.role == "target" and permit.probation
        assert permit.boot != record.identity.origin_boot
        assert permit.anchor == source.anchor and permit.installation == record.target.installation
        with closing(permit.open_store()) as store:
            assert store.instance_id == record.identity.store_instance
            assert store.main_session()["id"] == record.identity.main_session
            assert store.format_state == record.observed_format
            assert not store.migration_allowed
    finally:
        permit.close_after_writers()
        permit.journal.__exit__()


async def test_transfer_origin_real_runtime_drains_only_frozen_old_binding(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    previous = boot_host.permits[-1].record
    assert isinstance(previous, TransactionRecordV2) and previous.resolved
    assert previous.identity.deployment == source.record.origin.deployment
    assert previous.target == source.record.origin.binding
    _restore_origin(boot_host, source.record)
    _publish(boot_host, previous)
    permit = boot_host.acquire(boot=source.record.identity.origin_boot)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_permit=permit)
    try:
        assert runtime.maintenance.permit is permit and not permit.probation
        runtime.maintenance.seal({"runtime"})
        record = _pending_record(source.record)
        _publish(boot_host, record)
        with boot_host.journal.locked() as control:
            draining = advance_managed(control, record.position, TransitionV2(phase="draining"))
        assert isinstance(draining, ProjectControllerTransferRecord)
        async with runtime.configuration_lock:
            result = runtime.maintenance.drain(draining.generation, permit.boot, draining.challenge)
        assert result["state"] == "drained" and result["boot"] == permit.boot
        assert result["owners"] == {"runtime": True, "effects": True}
        assert not runtime.maintenance.admission.allowed
        assert runtime.maintenance.accepts_external_input
        assert boot_host.journal.read_current() == draining
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "phase", ["prepared", "draining", "stop_base", "install_target", "origin_boot"]
)
def test_transfer_boot_never_grants_origin_or_prestart_role(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = _pending_record(source.record)
    if phase == "draining":
        record = ProjectControllerTransferRecord.model_validate(
            {**record.model_dump(), "phase": "draining"}
        )
    elif phase in {"stop_base", "install_target"}:
        record = ProjectControllerTransferRecord.model_validate(
            {**record.model_dump(), "phase": "activating", "intent": phase}
        )
    elif phase == "origin_boot":
        record = _pending_record(source.record, phase="activating")
    _publish(boot_host, record)
    if phase == "origin_boot":
        with pytest.raises(ManagedBootError, match="managed_boot_not_permitted"):
            _acquire_reopened_permit(boot_host, record.identity.origin_boot)
    else:
        # The retained route refuses a pre-start phase before the formal permit
        # exists; do not bypass that earlier reader just to reach acquire.
        with pytest.raises(LaunchError, match="transaction_in_progress"):
            _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    assert boot_host.journal.read_current() == record


@pytest.mark.parametrize("damage", ["unit", "origin-snapshot", "anchor"])
async def test_transfer_recover_rejects_wrong_origin_material_before_launch(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = _pending_record(source.record)
    _restore_origin(boot_host, record)
    if damage == "unit":
        record = ProjectControllerTransferRecord.model_validate(
            {
                **record.model_dump(),
                "origin": {
                    **record.origin.model_dump(),
                    "binding": {
                        **record.origin.binding.model_dump(),
                        "unit": record.base.unit.model_dump(),
                    },
                },
            }
        )
    elif damage == "anchor":
        record = ProjectControllerTransferRecord.model_validate(
            {
                **record.model_dump(),
                "origin": {**record.origin.model_dump(), "deployment": "f" * 64},
            }
        )
    _publish(boot_host, record)
    if damage == "origin-snapshot":
        (boot_host.journal.directory.path / f"{record.generation}-origin-unit.bin").write_bytes(
            b"# damaged\n"
        )
    runner = Runner()
    with pytest.raises((ValueError, TransactionError)):
        await recover(boot_host.journal.directory.path, probe=Probe(), runner=runner)
    assert not runner.calls
    assert boot_host.journal.read_current() == record


@pytest.mark.parametrize("damage", ["nonce", "receipt", "unit", "resume"])
async def test_transfer_origin_never_borrows_new_role_or_repairs_evidence(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    previous = boot_host.permits[-1].record
    assert isinstance(previous, TransactionRecordV2)
    _restore_origin(boot_host, source.record)
    _publish(boot_host, previous)
    permit = boot_host.acquire(boot=source.record.identity.origin_boot)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_permit=permit)
    try:
        runtime.maintenance.seal({"runtime"})
        record = ProjectControllerTransferRecord.model_validate(
            {**_pending_record(source.record).model_dump(), "phase": "draining"}
        )
        if damage == "receipt":
            record = ProjectControllerTransferRecord.model_validate(
                {
                    **record.model_dump(),
                    "origin": {
                        **record.origin.model_dump(),
                        "projected": {
                            **record.origin.projected.model_dump(),
                            "revision": record.origin.projected.revision + 1,
                        },
                    },
                }
            )
        _publish(boot_host, record)
        if damage == "unit":
            Path(source.anchor.unit_path).write_bytes(source.service.render().encode())
        receipt = read_projection(runtime.store)
        changes = runtime.store.db.total_changes
        nonce = "wrong-primary" if damage == "nonce" else record.challenge
        with pytest.raises((MaintenanceError, RuntimeBindingError)):
            if damage == "resume":
                runtime.maintenance.resume(record.generation, permit.boot, nonce)
            else:
                runtime.maintenance.drain(record.generation, permit.boot, nonce)
        assert runtime.maintenance.admission.allowed
        assert runtime.store.db.total_changes == changes
        assert read_projection(runtime.store) == receipt == source.origin_receipt
        assert boot_host.journal.read_current() == record
    finally:
        await runtime.close()


async def test_transfer_formal_boot_dispatch_preserves_real_probe_and_writer_lease(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = _pending_record(source.record, phase="activating")
    _publish(boot_host, record)
    service = UserService(
        Path(source.anchor.config_path),
        Path(source.anchor.data_dir),
        Path(source.anchor.unit_path).parent,
        platform="linux",
        executable=boot_host.target.lexical_python,
    )
    probe = TerminalProbe()
    probe.boot = "4" * 32
    session = await open_boot_session(
        service,
        boot_host.journal.database,
        managed_root=boot_host.journal.directory.path,
        managed_probe=probe,
    )
    try:
        assert session.manifest is None and session.unsupported_reason is None
        permit = session.permit
        assert isinstance(permit, ManagedBootPermit)
        assert permit.record == record and permit.probation and permit.role == "target"
        assert permit.boot == probe.boot != record.identity.origin_boot
        assert permit.lease is session.lease and probe.calls == 1
        with pytest.raises(TransactionError, match="lock_busy"):
            boot_host.journal.writer_lease().__enter__()
        with closing(permit.open_store()) as store:
            assert not store.migration_allowed and store.db.total_changes == 0
    finally:
        session.close_after_writers()


async def test_transfer_probationary_runtime_final_projection_is_a_read_only_noop(
    boot_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot_environment(monkeypatch)
    source = _publish_terminal_transfer(boot_host, "committed")
    record = _pending_record(source.record, phase="activating")
    _publish(boot_host, record)
    permit = _acquire_reopened_permit(boot_host, _FRESH_BOOT)
    runtime = Runtime(permit.config, Path(source.anchor.data_dir), boot_permit=permit)
    try:
        before = runtime.store.db.total_changes
        recovery = RecoveryHost(runtime, source.service)
        await recovery.project(strict=True)
        assert recovery.projection_error is None
        assert read_projection(runtime.store) == source.origin_receipt
        assert runtime.store.db.total_changes == before
        assert boot_host.journal.read_current() == record
        assert not runtime.maintenance.admission.allowed
    finally:
        await runtime.close()
        permit.journal.__exit__()
