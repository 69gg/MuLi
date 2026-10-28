"""HTTP host commands retain full authentication around both fresh probes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import pytest
from test_managed_activation_receipt import decide, original_writer_is_held
from test_managed_backend import Host
from test_managed_backend import host as host
from test_managed_backend_package_batch import observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_deployment_authentication_batch import bounded_slots as bounded_slots
from test_managed_observation_authentication import (
    assert_actual_payload_reads,
    observe_complete_package_scans,
    observe_probes,
)

from muli.managed_controller import ManagedProcessIdentity, ManagedServiceObservation
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionError
from muli.platforms.managed_deployment import ManagedDeploymentError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real journal and leases")

type Command = Literal["drain", "resume", "activate-target", "activate-base"]
type HostAction = Literal["drain", "resume", "activate"]


async def prepare_command(
    host: Host, command: Command
) -> tuple[TransactionRecordV2, HostAction, Role | None]:
    """Only manager and HTTP are fake; activation uses the real decision chain."""
    if command == "drain":
        return host.move("draining"), "drain", None
    if command == "resume":
        host.admission = "paused"
        return host.record(), "resume", None
    role: Role = "base" if command == "activate-base" else "target"
    _, record = await decide(host, role)
    return record, "activate", role


@pytest.mark.parametrize("command", ["drain", "resume", "activate-target", "activate-base"])
async def test_host_command_authenticates_the_post_http_probe_boundary_once(
    host: Host, monkeypatch: pytest.MonkeyPatch, command: Command
) -> None:
    record, action, role = await prepare_command(host, command)
    selected: Role = "base" if role is None else role
    writer = host.writer
    assert writer is not None
    original_writer_is_held(host, writer)
    disk, service = host.backend._disk(), host.probe.snapshot
    commands, http_start = list(host.commands), len(host.transport.calls)
    host.probe.count = 0
    scans = observe_complete_package_scans(host, monkeypatch)
    probes = observe_probes(host, monkeypatch)
    reads = observe_payloads(host, monkeypatch) if command == "activate-target" else None
    post_probe_scans: dict[Path, int] = {}

    def before_read(index: int) -> None:
        if index == 2:
            # The fake manager reads no package bytes. This marks the final
            # fresh probe, before its real synchronous authentication begins.
            assert not post_probe_scans
            post_probe_scans.update(scans)

    host.probe.before_read = before_read

    result = await host.backend._host_command(record, action, role)

    assert host.transport.calls[http_start:] == [action]
    assert len(probes) == host.probe.count == 2 and post_probe_scans
    for probe in probes:
        same_service = probe.service == service
        assert same_service
        assert selected in probe.process_roles
        assert record.binding(selected).unit.digest in probe.loaded_units
        assert probe.disk_unit == record.binding(selected).unit.digest and probe.unit_confirmed
    same_disk = host.backend._disk() == disk
    same_record = host.record() == record
    assert same_disk and same_record and host.commands == commands
    original_writer_is_held(host, writer)
    if action == "activate":
        binding = record.binding(selected)
        expected = ManagedServiceObservation(
            identity_matches=True,
            configuration=disk.configuration.digest,
            selector=None if disk.selector is None else disk.selector.digest,
            unit=disk.unit.digest,
            service="running",
            boot=service.unit.invocation,
            writer_free=False,
            process=ManagedProcessIdentity(
                binding.entry, binding.installation, binding.compatibility
            ),
            health=host.proof(),
        )
        complete = result == expected
        assert complete and result is not None and result.health is not None
        assert record.accepts(result.health, selected, released=True)
        assert record.activation == "pending" and not record.resolved
    else:
        assert result is None
    assert host.admission == ("paused" if action == "drain" else "released")
    if reads is not None:
        assert_actual_payload_reads(reads, scans)
    controller = Path(host.backend.controller_slot.package.root)
    selected_root = Path(host.slots[selected].package.root)
    if selected_root != controller:
        assert scans[selected_root] - post_probe_scans[selected_root] == 1
    for root in scans.keys() - {controller, selected_root}:
        assert scans[root] == 0
    # All real identity, full bytes/SHA, proof, HTTP and lease assertions above
    # precede the old-code failure: the adjacent tail checks scan controller twice.
    assert scans[controller] - post_probe_scans[controller] == 1


@pytest.mark.parametrize("fault", ["controller", "selected", "both"])
async def test_post_http_probe_still_rejects_changed_packages_in_controller_first_order(
    host: Host, fault: Literal["controller", "selected", "both"]
) -> None:
    record, action, role = await prepare_command(host, "activate-target")
    writer = host.writer
    assert writer is not None
    commands, http_start = list(host.commands), len(host.transport.calls)
    host.probe.count = 0
    changed: list[Path] = []

    def before_read(index: int) -> None:
        if index != 2:
            return
        roles: tuple[Role, ...]
        if fault == "both":
            roles = ("base", "target")
        else:
            roles = ("base" if fault == "controller" else "target",)
        for changed_role in roles:
            leaf = Path(host.slots[changed_role].package.root) / "__init__.py"
            leaf.write_bytes(leaf.read_bytes() + b"\n# actual post-HTTP package mutation\n")
            changed.append(leaf)

    host.probe.before_read = before_read
    code = "managed_slot_changed" if fault == "selected" else "anchor_unavailable"
    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        await host.backend._host_command(record, action, role)

    assert len(changed) == (2 if fault == "both" else 1)
    assert host.transport.calls[http_start:] == [action] and host.probe.count == 2
    same_record = host.record() == record
    assert same_record and host.commands == commands
    assert record.activation == "pending" and not record.resolved
    original_writer_is_held(host, writer)


@pytest.mark.parametrize("command", ["drain", "resume", "activate-base"])
async def test_base_host_commands_do_not_authenticate_the_opposite_damaged_package(
    host: Host, command: Literal["drain", "resume", "activate-base"]
) -> None:
    record, action, role = await prepare_command(host, command)
    writer = host.writer
    assert writer is not None
    target = Path(host.slots["target"].package.root) / "__init__.py"
    target.write_bytes(target.read_bytes() + b"\n# damaged opposite package\n")
    disk, commands = host.backend._disk(), list(host.commands)
    http_start = len(host.transport.calls)
    host.probe.count = 0

    result = await host.backend._host_command(record, action, role)

    assert host.transport.calls[http_start:] == [action] and host.probe.count == 2
    same_record = host.record() == record
    same_disk = host.backend._disk() == disk
    assert same_record and same_disk and host.commands == commands
    if action == "activate":
        assert result is not None and result.health is not None
        assert record.accepts(result.health, "base", released=True)
    else:
        assert result is None
    original_writer_is_held(host, writer)


async def test_post_http_probe_rechecks_the_actual_journal_revision(host: Host) -> None:
    record, action, role = await prepare_command(host, "activate-target")
    writer = host.writer
    assert writer is not None
    commands, http_start = list(host.commands), len(host.transport.calls)
    host.probe.count = 0
    moved: list[TransactionRecordV2] = []

    def before_read(index: int) -> None:
        if index == 2:
            moved.append(host.move("committed", proof=record.health))

    host.probe.before_read = before_read
    with pytest.raises(TransactionError, match="^stale_revision$"):
        await host.backend._host_command(record, action, role)

    assert len(moved) == 1 and moved[0].revision == record.revision + 1
    same_record = host.record() == moved[0]
    assert same_record and moved[0].activation == "pending" and not moved[0].resolved
    assert moved[0].deadline == record.deadline and moved[0].generation == record.generation
    assert host.transport.calls[http_start:] == [action] and host.probe.count == 2
    assert host.commands == commands
    original_writer_is_held(host, writer)
