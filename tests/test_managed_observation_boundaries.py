"""Observation batching preserves error order and authenticates an origin's base."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import pytest
import test_managed_backend as fixtures
from test_managed_authentication_boundaries import bounded_slots as bounded_slots
from test_managed_backend import CHANGED_CONFIG, Host, track_slot_scans
from test_managed_backend import host as host

from muli.managed_journal import RoleSnapshots, prepare_managed
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import ProjectionReceipt, Role, TransactionJournal, _snapshot
from muli.platforms.linux_transaction import ObservationError, ProcessIdentity, UnitSnapshot
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    ManagedDeploymentError,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
)
from muli.platforms.managed_probe import ManagedProbeResult, ManagedSystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed observation")


@pytest.mark.parametrize("failure", ["foreign", "mismatch"])
@pytest.mark.parametrize("controller_healthy", [False, True])
async def test_observation_error_order_survives_package_failure_after_probe(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["foreign", "mismatch"],
    controller_healthy: bool,
) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    host.move("activating", "start_target")
    host.running("target")
    record = host.move("verifying")
    disk = host.backend._disk()
    original = host.probe.read_managed
    baseline = host.probe.count
    wanted = 1 if failure == "foreign" else 2
    injected: list[int] = []

    async def read_managed(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        result = await original(journal, current, anchor, controller, slots)
        if host.probe.count - baseline == wanted:
            assert not injected
            injected.append(host.probe.count)
            # Real disk drift makes _owned fail on the first observation, or
            # makes the frozen binding comparison fail on the second one.
            Path(anchor.config_path).write_bytes(CHANGED_CONFIG)
            damaged = slots["target"] if controller_healthy else controller
            (Path(damaged.package.root) / "__main__.py").write_bytes(b"changed after probe")
        return result

    monkeypatch.setattr(host.probe, "read_managed", read_managed)
    error: type[ValueError] = ManagedDeploymentError
    code = "anchor_unavailable"
    if controller_healthy:
        error = ObservationError
        code = (
            "foreign_transaction_process"
            if failure == "foreign"
            else "transaction_observation_changed"
        )

    with pytest.raises(error, match=f"^{code}$"):
        await host.backend.observe(record)

    assert injected == [baseline + wanted]
    assert host.record() == record
    after = host.backend._disk()
    assert after.configuration.data == CHANGED_CONFIG
    assert (after.selector, after.unit) == (disk.selector, disk.unit)
    assert host.writer is not None
    host.writer.check()
    assert host.commands == []
    assert host.transport.calls == ([] if failure == "foreign" else ["readiness"])


def distinct_origin_base(host: Host, root: Path) -> TransactionRecordV2:
    """Publish and prepare a real third installation, retaining the controller."""
    old = host.move("aborted", code="cancelled")
    host.release_writer()
    base = fixtures._slot(root, "origin-base")
    with DeploymentStore(host.journal.directory.path) as store:
        store.publish_slot(base, guard=host.control.check)
    previous = host.data["base"]
    selector = SelectorValue(deployment=host.anchor.digest, installation=base.digest)
    data = RoleSnapshots(previous.configuration, selector.canonical(), previous.unit)
    binding = old.base.model_copy(
        update={
            "installation": base.digest,
            "compatibility": base.compatibility_digest,
            "selector": _snapshot(selector.canonical(), 16384),
        }
    )
    with host.journal.writer_lease() as writer:
        with SelectorStore(host.journal.directory.path) as selections:
            selections.replace(selections.read(), selector, guard=host.control.check)
        record = prepare_managed(
            host.control,
            request_id="independent-origin-base",
            action="switch",
            identity=old.identity,
            base=binding,
            target=old.target,
            base_data=data,
            target_data=host.data["target"],
            observed_format=old.observed_format,
            base_manifest=base.compatibility,
            target_manifest=host.slots["target"].compatibility,
            deadline=old.deadline,
            writer=writer,
            projected=ProjectionReceipt(
                **old.position.model_dump(), store_instance=old.identity.store_instance
            ),
        )
    host.slots["base"] = host.backend.slots["base"] = base
    host.data["base"] = data
    host.running("base", origin=True)
    return record


async def test_origin_checks_its_complete_base_when_controller_is_a_distinct_installation(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = distinct_origin_base(host, tmp_path)
    controller, base = host.backend.controller_slot, host.slots["base"]
    assert len({controller.digest, base.digest, host.slots["target"].digest}) == 3
    assert controller.resolved_python == base.resolved_python
    assert controller.package.root != base.package.root
    # The shared Host's convenience probe assumes controller == base. Keep its
    # manager/process evidence, but use production role matching for this case.
    probe = ManagedSystemdProbe(limits=host.probe.limits)

    async def snapshot(path: str) -> UnitSnapshot:
        assert path.startswith("/org/freedesktop/systemd1/unit/")
        return host.probe.snapshot.unit

    def process(pid: int) -> ProcessIdentity:
        value = host.probe.snapshot.process
        assert value is not None and value.pid == pid
        return value

    monkeypatch.setattr(probe, "_snapshot", snapshot)
    monkeypatch.setattr(probe, "_process", process)
    monkeypatch.setattr(host.backend, "probe", probe)
    scans = track_slot_scans(monkeypatch)
    disk = host.backend._disk()

    observation = await host.backend.observe(record)

    assert observation.service == "running"
    assert observation.process is not None and observation.process.installation == base.digest
    assert observation.health is not None and observation.health.role == "origin"
    assert Path(controller.package.root) in scans and Path(base.package.root) in scans
    assert host.transport.calls == ["readiness"]
    host.transport.calls.clear()
    (Path(base.package.root) / "__main__.py").write_bytes(b"changed origin base package")

    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        await host.backend.observe(record)

    assert host.record() == record and host.backend._disk() == disk
    assert host.writer is not None
    host.writer.check()
    assert host.commands == [] and host.transport.calls == []
