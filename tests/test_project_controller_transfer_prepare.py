from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_managed_boot import _slot
from test_managed_preparation import Host
from test_managed_preparation import host as host
from test_managed_transaction import move, proof, verifying

import muli
from muli.managed_boot import ManagedBootPermit
from muli.managed_journal import RoleSnapshots, read_snapshots
from muli.managed_runtime_binding import verify_running_binding
from muli.managed_transaction import ProjectControllerTransferRecord, TransitionV2
from muli.management_projection import project_final, read_projection
from muli.management_transaction import JournalControl, Role
from muli.platforms import installation, managed_deployment
from muli.platforms.managed_deployment import (
    DeploymentStore,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    resolve_entry,
)
from muli.platforms.managed_probe import ManagedUserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed preparation")


async def running_bridge(host: Host) -> SlotIdentity:
    host.complete(await host.run(host.plan("adopt", name="adopt"), candidate=host.slot))
    bridge = _slot(host.root, "running-bridge")
    switched = await host.run(host.plan("switch", name="switch"), candidate=bridge)
    system = SimpleNamespace(
        platform="linux",
        executable=bridge.lexical_python,
        prefix=bridge.prefix,
        base_prefix=str(host.root / "base-python"),
    )
    host.monkeypatch.setattr(managed_deployment, "sys", system)
    host.monkeypatch.setattr(installation, "sys", system)
    host.monkeypatch.setattr(muli, "__file__", str(Path(bridge.package.root) / "__init__.py"))
    # The second boot must differ from the first completion's "fresh" boot.
    record = verifying(switched.record)
    record = move(record, TransitionV2(phase="committed", proof=proof(record, boot="bridge-live")))
    record = move(
        record,
        TransitionV2(
            phase="committed",
            activation="released",
            proof=proof(record, boot="bridge-live", admission="released"),
        ),
    )
    data = read_snapshots(host.journal, record, "target")
    Path(switched.anchor.config_path).write_bytes(data.configuration)
    Path(switched.anchor.unit_path).write_bytes(data.unit)
    with SelectorStore(host.journal.directory.path) as selector:
        selector.replace(
            selector.read(), SelectorValue.model_validate_json(data.selector), guard=lambda: None
        )
    with host.journal.locked() as control:
        control._write(record)
    previous = host.runtime.maintenance.permit
    assert previous is not None and host.runtime.boot_session is not None
    permit = ManagedBootPermit.acquire(
        host.journal,
        boot="bridge-live",
        plan=resolve_entry(host.journal.directory.path, purpose="service"),
        lease=previous.lease,
    )
    host.runtime.maintenance.permit = host.runtime.boot_session.permit = permit
    host.runtime.config = permit.config
    host.probe.boot = permit.boot
    assert (
        project_final(
            permit,
            host.runtime.store,
            verify_binding=lambda value, role: verify_running_binding(
                permit, host.runtime.store, value, role
            ),
        )
        is not None
    )
    return bridge


def transfer_plan(host: Host) -> dict[str, Any]:
    # Use the existing persisted management envelope, not a future fixture API.
    item = {**host.plan("restart", name="transfer"), "action": "project_controller_transfer"}
    host.runtime.store.put("management", item)
    return item


async def test_formal_prepare_publishes_new_anchor_and_nine_exact_snapshots(host: Host) -> None:
    bridge = await running_bridge(host)
    host.runtime.config.update({"persona": {"user_name": "hot configuration before transfer"}})
    previous = host.journal.read_current()
    assert previous is not None
    projected = read_projection(host.runtime.store)
    original = RoleSnapshots(
        host.runtime.config.path.read_bytes(),
        (host.journal.directory.path / "selector.json").read_bytes(),
        host.service.path.read_bytes(),
    )
    item = transfer_plan(host)

    replacements: list[str] = []
    replace = JournalControl._replace

    def observed(control: JournalControl, name: str, data: bytes) -> None:
        replacements.append(name)
        if name == "state.json":
            assert sum(value != "state.json" for value in replacements) == 9
        replace(control, name, data)

    host.monkeypatch.setattr(JournalControl, "_replace", observed)
    prepared = await host.run(item)

    record = prepared.record
    assert isinstance(record, ProjectControllerTransferRecord)
    assert record == host.journal.read_current()
    assert record.origin.deployment == previous.identity.deployment
    assert record.origin.projected == projected
    assert record.origin.binding.installation == bridge.digest
    assert record.base == record.target
    assert record.base.installation == prepared.anchor.controller_slot == bridge.digest
    assert prepared.anchor.digest != record.origin.deployment
    assert record.identity.deployment == prepared.anchor.digest
    assert record.identity.origin_boot == host.probe.boot
    expected_unit = (
        ManagedUserService(
            host.runtime.config.path,
            host.runtime.data_dir,
            host.service.path.parent,
            controller=bridge,
            launcher=Path(prepared.anchor.launcher.path),
            journal_root=host.journal.directory.path,
        )
        .render()
        .encode()
    )
    roles: tuple[Role, ...] = ("base", "target")
    for role in roles:
        data = read_snapshots(host.journal, record, role)
        assert data.configuration == original.configuration
        assert data.unit == expected_unit
        assert SelectorValue.model_validate_json(data.selector) == SelectorValue(
            deployment=prepared.anchor.digest, installation=bridge.digest
        )
    for component, expected in (
        ("configuration.json", original.configuration),
        ("selector.json", original.selector),
        ("unit.bin", original.unit),
    ):
        assert (
            host.journal.directory.path / f"{record.generation}-origin-{component}"
        ).read_bytes() == expected
    assert len(list(host.journal.directory.path.glob(f"{record.generation}-*"))) == 9
    assert replacements[-1] == "state.json"
    assert host.runtime.config.path.read_bytes() == original.configuration
    assert host.service.path.read_bytes() == original.unit
    assert (host.journal.directory.path / "selector.json").read_bytes() == original.selector
    with DeploymentStore(host.journal.directory.path, host.journal.limits) as deployments:
        assert deployments.read_anchor(record.origin.deployment).controller_slot == host.slot.digest
        deployments.verify_anchor(prepared.anchor)


async def test_transfer_prepare_rechecks_plan_after_actual_process_observation(host: Host) -> None:
    await running_bridge(host)
    item = transfer_plan(host)
    previous = host.journal.read_current()
    entries = set(host.journal.directory.path.iterdir())
    position = host.runtime.store.event_position()

    def changed() -> None:
        host.runtime.store.put("management", {**item, "status": "cancelled"})

    host.probe.during = changed
    with pytest.raises(ValueError, match="managed_preparation_plan_changed"):
        await host.run(item)
    assert host.journal.read_current() == previous
    assert set(host.journal.directory.path.iterdir()) == entries
    assert host.runtime.store.event_position() == position


async def test_transfer_full_directory_budget_rejects_before_new_materials_or_gc(
    host: Host,
) -> None:
    await running_bridge(host)
    item = transfer_plan(host)
    root = host.journal.directory.path
    count = len(list(root.iterdir()))
    for index in range(host.journal.limits.max_entries - count):
        (root / f"foreign-{index}").write_bytes(b"preserve")
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    position = host.runtime.store.event_position()
    with pytest.raises(ValueError, match="directory_budget"):
        await host.run(item)
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before
    assert host.runtime.store.event_position() == position
    assert host.runtime.store.get("management", item["id"])["status"] == "pending"


@pytest.mark.parametrize("candidate", [False, True])
async def test_transfer_cannot_select_an_arbitrary_or_unchanged_controller(
    host: Host, candidate: bool
) -> None:
    host.complete(await host.run(host.plan("adopt", name="adopt"), candidate=host.slot))
    item = transfer_plan(host)
    before = host.journal.read_current()
    position = host.runtime.store.event_position()
    with pytest.raises(
        ValueError,
        match="managed_candidate_paths_required"
        if candidate
        else "managed_transfer_source_required",
    ):
        await host.run(item, candidate=_slot(host.root, "untrusted") if candidate else None)
    assert host.journal.read_current() == before
    assert host.runtime.store.event_position() == position


@pytest.mark.parametrize("corrupt", [False, True])
async def test_transfer_partial_publication_keeps_generation_and_never_repairs_evidence(
    host: Host, corrupt: bool
) -> None:
    await running_bridge(host)
    item = transfer_plan(host)
    previous = host.journal.read_current()
    replace = JournalControl._replace

    def lose_reply(control: JournalControl, name: str, data: bytes) -> None:
        replace(control, name, data)
        if name.endswith("-origin-unit.bin"):
            raise OSError("lost_snapshot_reply")

    host.monkeypatch.setattr(JournalControl, "_replace", lose_reply)
    with pytest.raises(OSError, match="lost_snapshot_reply"):
        await host.run(item)
    host.monkeypatch.setattr(JournalControl, "_replace", replace)
    assert host.journal.read_current() == previous
    saved = host.runtime.store.get("management", item["id"])
    generation = saved["transaction"]["generation"]
    orphan = host.journal.directory.path / f"{generation}-origin-unit.bin"
    if corrupt:
        orphan.write_bytes(b"foreign evidence")
        before = {
            p.name: p.read_bytes() for p in host.journal.directory.path.iterdir() if p.is_file()
        }
        with pytest.raises(ValueError, match="snapshot_invalid"):
            await host.run(item)
        assert {
            p.name: p.read_bytes() for p in host.journal.directory.path.iterdir() if p.is_file()
        } == before
        assert host.journal.read_current() == previous
    else:
        prepared = await host.run(item)
        assert prepared.record.generation == generation
        assert isinstance(prepared.record, ProjectControllerTransferRecord)
