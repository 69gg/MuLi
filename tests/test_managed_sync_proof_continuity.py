"""Content proofs may flow only into adjacent real synchronous file guards."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_managed_backend import BASE_BOOT, TARGET_BOOT, Host
from test_managed_backend_package_batch import (
    _PAYLOADS,
    PackageReads,
    observe_checks,
    observe_payloads,
)
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import PythonReads, observe_python_reads
from test_managed_backend_python_proof import shared_host as shared_host
from test_managed_deployment_authentication_batch import MetadataReads, observe_metadata
from test_managed_start_confirmation_batch import prepare_start

from muli.managed_controller import BindingConfirmation
from muli.managed_journal import RoleSnapshots, read_snapshots
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import FixedLock, Role, TransactionJournal
from muli.platforms import managed_launcher
from muli.platforms.installation import FileFingerprint
from muli.platforms.managed_backend import _DiskBinding, _FileProofs
from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity
from muli.platforms.managed_launcher import PackageProofs
from muli.platforms.managed_probe import ManagedProbeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real synchronous proofs")


@dataclass(frozen=True)
class ReadSnapshot:
    leaves: dict[Path, tuple[int, int, int, int]]
    scans: dict[Path, int]
    python_bytes: int
    python_opens: int
    checks: int


def snapshot(
    reads: PackageReads, python: PythonReads, checks: list[tuple[TransactionRecordV2, Role | None]]
) -> ReadSnapshot:
    return ReadSnapshot(
        {
            path: (leaf.bytes, len(leaf.opened), leaf.fd_stats, leaf.path_stats)
            for path, leaf in reads.leaves.items()
        },
        dict(reads.scans),
        python.bytes,
        len(python.opened),
        len(checks),
    )


async def prepare_stopped(host: Host, role: Role, *, installed: bool) -> TransactionRecordV2:
    """Keep the real install/CAS path and two distinct retained package roots."""
    record = host.install_intent()
    if role == "base" or installed:
        await host.backend.install(record, "target", host.data["target"])
    if role == "base":
        host.move("rolling_back", "stop_target", code="backend_failed")
        record = host.move("rolling_back", "restore_base", code="backend_failed")
        if installed:
            await host.backend.install(record, "base", host.data["base"])
        # A nonselected damaged target must not obstruct base restoration.
        leaf = Path(host.slots["target"].package.root) / "batch_payload/first.bin"
        leaf.write_bytes(leaf.read_bytes() + b"damaged opposite package")
    host.probe.count = 0
    return record


def assert_span_walks(
    host: Host,
    role: Role,
    reads: PackageReads,
    python: PythonReads,
    before: ReadSnapshot,
    after: ReadSnapshot,
    guards: int,
) -> None:
    """Every old guard must still reopen and enumerate the selected real files."""
    controller = Path(host.backend.controller_slot.package.root)
    selected = Path(host.slots[role].package.root)
    assert after.checks - before.checks == guards
    for path, leaf in reads.leaves.items():
        walks = guards if path.is_relative_to(controller) or path.is_relative_to(selected) else 0
        assert after.leaves[path][1] - before.leaves[path][1] == walks, str(path)
        assert after.leaves[path][2] - before.leaves[path][2] >= 2 * walks, str(path)
        assert after.leaves[path][3] - before.leaves[path][3] == walks, str(path)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in leaf.opened)
    for path in reads.scans:
        walks = guards if path.is_relative_to(controller) or path.is_relative_to(selected) else 0
        assert after.scans[path] - before.scans[path] == walks, str(path)
    assert after.python_opens - before.python_opens == guards * (2 if role == "target" else 1)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)


def assert_only_unverified_selected_bytes(
    host: Host, role: Role, reads: PackageReads, before: ReadSnapshot, after: ReadSnapshot
) -> None:
    controller = Path(host.backend.controller_slot.package.root)
    selected = Path(host.slots[role].package.root)
    actual = {str(path): after.leaves[path][0] - before.leaves[path][0] for path in reads.leaves}
    actual["python"] = after.python_bytes - before.python_bytes
    expected = {
        str(path): size
        if path.is_relative_to(selected) and not path.is_relative_to(controller)
        else 0
        for path, size in reads.sizes.items()
    }
    expected["python"] = 0
    # The complete operation, old guard sequence and independent real opens
    # pass first. H fails here on exactly one redundant controller/Python copy.
    assert actual == expected


def expected_confirmation(record: TransactionRecordV2, role: Role) -> BindingConfirmation:
    binding = record.binding(role)
    return BindingConfirmation(
        record.position,
        role,
        binding.configuration.digest,
        None if binding.selector is None else binding.selector.digest,
        binding.unit.digest,
        binding.unit.digest,
    )


@pytest.mark.parametrize("role", ["target", "base"])
async def test_observe_second_probe_lends_controller_bytes_to_stopped_confirmation(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    host = shared_host
    record = await prepare_stopped(host, role, installed=True)
    disk, commands = host.backend._disk(), list(host.commands)
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    original = host.backend._confirm
    spans: list[tuple[ReadSnapshot, ReadSnapshot]] = []

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        assert host.probe.count == 2 and not spans and selected == role
        assert checks == [(record, None)] * 4
        lease.check()
        before = snapshot(reads, python, checks)
        result = original(current, selected, lease, cgroup, proofs=proofs)
        spans.append((before, snapshot(reads, python, checks)))
        lease.check()
        return result

    monkeypatch.setattr(host.backend, "_confirm", confirm)

    observed = await host.backend.observe(record)

    assert observed.confirmation == expected_confirmation(record, role)
    assert observed.service == "stopped" and observed.writer_free
    assert checks == [(record, None)] * 4 + [(record, role)] * 7 + [(record, None)] * 2
    assert host.probe.count == 3 and host.commands == commands and host.transport.calls == []
    same_disk, same_record = host.backend._disk() == disk, host.record() == record
    assert same_disk and same_record and state_path.read_bytes() == state
    assert host.record().deadline == record.deadline and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()
    assert len(spans) == 1
    before, after = spans[0]
    assert_span_walks(host, role, reads, python, before, after, 7)
    assert_only_unverified_selected_bytes(host, role, reads, before, after)


@pytest.mark.parametrize("role", ["target", "base"])
async def test_real_observe_confirm_lends_controller_content_to_third_probe_preflight(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    host = shared_host
    record = await prepare_stopped(host, role, installed=True)
    controller = Path(host.backend.controller_slot.package.root)
    target = Path(host.slots["target"].package.root)
    assert controller != target
    assert host.backend.controller_slot.resolved_python == host.slots[role].resolved_python
    python_size = host.backend.controller_slot.resolved_python.identity.size
    assert python_size > 0
    if role == "base":
        damaged = target / "batch_payload/first.bin"
        assert damaged.read_bytes().endswith(b"damaged opposite package")

    commands, transport_calls = list(host.commands), list(host.transport.calls)
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    disk = host.backend._disk()
    service = host.probe.snapshot
    cgroup_events = host.events.read_bytes()
    operation_timeout, probe_limits = host.backend.operation_timeout, host.probe.limits
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    original_confirm = host.backend._confirm
    original_read = host.probe.read_managed
    confirm_returns: list[ReadSnapshot] = []
    third_probe_entries: list[ReadSnapshot] = []
    borrowed: list[_FileProofs] = []
    leases: list[FixedLock] = []
    lease_identities: list[tuple[int, int]] = []
    probes: list[ManagedProbeResult] = []

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        assert host.probe.count == 2 and not confirm_returns and selected == role
        assert proofs is not None
        result = original_confirm(current, selected, lease, cgroup, proofs=proofs)
        assert proofs.python is not None and proofs.packages is not None
        confirm_returns.append(snapshot(reads, python, checks))
        borrowed.append(proofs)
        leases.append(lease)
        lease.check()
        assert lease.fd is not None
        info = os.fstat(lease.fd)
        lease_identities.append((info.st_dev, info.st_ino))
        return result

    async def read_managed(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller_slot: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        if host.probe.count == 2:
            # This executes after the real synchronous preflight and before the
            # original third manager read. No event/sleep/task/gate is added.
            assert len(borrowed) == 1
            assert borrowed[0].python is None and borrowed[0].packages is None
            third_probe_entries.append(snapshot(reads, python, checks))
            leases[-1].check()
        result = await original_read(journal, current, anchor, controller_slot, slots)
        probes.append(result)
        return result

    monkeypatch.setattr(host.backend, "_confirm", confirm)
    monkeypatch.setattr(host.probe, "read_managed", read_managed)

    observation = await host.backend.observe(record)

    # Keep the full preexisting public, state, lease and probe contract first.
    assert observation.confirmation == expected_confirmation(record, role)
    assert observation.service == "stopped" and observation.writer_free
    expected_roles: list[Role | None] = [None] * 4 + [role] * 7 + [None] * 2
    assert len(checks) == 13
    assert all(current is record for current, _selected in checks)
    assert [selected for _current, selected in checks] == expected_roles
    assert host.probe.count == len(probes) == 3
    assert all(result.service == service and result.process_roles == () for result in probes)
    assert all(result.unit_confirmed for result in probes)
    assert probes[-1].disk_unit == record.binding(role).unit.digest
    assert host.commands == commands and host.transport.calls == transport_calls == []
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    assert host.record().deadline == record.deadline
    assert host.backend.operation_timeout == operation_timeout and host.probe.limits == probe_limits
    assert host.events.read_bytes() == cgroup_events and host.writer is None
    assert len(leases) == len(lease_identities) == len(borrowed) == 1
    assert leases[0].fd is None
    assert borrowed[0].python is None and borrowed[0].packages is None
    with host.journal.writer_lease() as available:
        available.check()
        assert available.fd is not None
        info = os.fstat(available.fd)
        assert (info.st_dev, info.st_ino) == lease_identities[0]
    host.control.check()

    # Every original role, independent open, full walk and identity fence remains.
    for path, leaf in reads.leaves.items():
        walks = 13 if path.is_relative_to(controller) else 7 if role == "target" else 0
        assert len(leaf.opened) == walks, str(path)
        assert leaf.fd_stats >= 2 * walks and leaf.path_stats == walks, str(path)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in leaf.opened)
    for directory, count in reads.scans.items():
        walks = 13 if directory.is_relative_to(controller) else 7 if role == "target" else 0
        assert count == walks, str(directory)
    assert len(python.opened) == len(checks) + sum(selected == "target" for _, selected in checks)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)

    assert len(confirm_returns) == len(third_probe_entries) == 1
    before, after = confirm_returns[0], third_probe_entries[0]
    assert before.checks == 11 and after.checks == 12 and len(checks) - after.checks == 1
    assert after.python_opens - before.python_opens == 1
    for path in reads.leaves:
        walks = int(path.is_relative_to(controller))
        assert after.leaves[path][1] - before.leaves[path][1] == walks, str(path)
        assert after.leaves[path][2] - before.leaves[path][2] >= 2 * walks, str(path)
        assert after.leaves[path][3] - before.leaves[path][3] == walks, str(path)
        if not walks:
            assert after.leaves[path][0] == before.leaves[path][0], str(path)
    for directory in reads.scans:
        assert after.scans[directory] - before.scans[directory] == int(
            directory.is_relative_to(controller)
        ), str(directory)

    # The real manager await still forces a fresh, complete controller read.
    assert python.bytes - after.python_bytes == python_size
    for path, size in reads.sizes.items():
        copies = int(path.is_relative_to(controller))
        assert reads.leaves[path].bytes - after.leaves[path][0] == size * copies, str(path)

    # Only two registered fixture leaves plus fixture Python are metered. This
    # was the frozen baseline's sole expected red predicate.
    extra_content = {
        path.relative_to(controller).as_posix(): after.leaves[path][0] - before.leaves[path][0]
        for path in reads.leaves
        if path.is_relative_to(controller)
    }
    extra_content["python"] = after.python_bytes - before.python_bytes
    assert extra_content == dict.fromkeys(extra_content, 0)


@pytest.mark.parametrize("role", ["target", "base"])
async def test_install_first_probe_lends_controller_bytes_to_writer_preflight(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    host = shared_host
    record = await prepare_stopped(host, role, installed=False)
    commands = list(host.commands)
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    original = host.backend._probe
    handoff_start: ReadSnapshot | None = None
    spans: list[tuple[ReadSnapshot, ReadSnapshot]] = []
    # This positive window uses the existing shared-interpreter fixture. A
    # different selected Python still needs its own complete content read.
    assert host.backend.controller_slot.resolved_python == host.slots[role].resolved_python

    async def probe(
        current: TransactionRecordV2,
        *,
        preflight: Callable[[], None] | None = None,
        postprocess: Callable[[ManagedProbeResult], Role | None] | None = None,
        postproofs: _FileProofs | None = None,
    ) -> ManagedProbeResult:
        nonlocal handoff_start
        if host.probe.count == 1:
            assert preflight is not None
            original_preflight: Callable[[], None] = preflight

            def measured_preflight() -> None:
                assert handoff_start is not None and not spans
                assert checks == [(record, role), (record, None)]
                original_preflight()
                spans.append((handoff_start, snapshot(reads, python, checks)))

            preflight = measured_preflight
        result = await original(
            current, preflight=preflight, postprocess=postprocess, postproofs=postproofs
        )
        if host.probe.count == 1:
            assert handoff_start is None
            # Start only after the first real post-await authentication and
            # its final root/metadata fences returned. No new await is added.
            handoff_start = snapshot(reads, python, checks)
        return result

    monkeypatch.setattr(host.backend, "_probe", probe)

    confirmation = await host.backend.install(record, role, host.data[role])

    assert confirmation == expected_confirmation(record, role)
    assert checks == (
        [(record, value) for value in (role, None, role, None)]
        + [(record, role)] * 16
        + [(record, None)] * 2
        + [(record, role)] * 7
    )
    assert host.probe.count == 3 and host.commands == commands and host.transport.calls == []
    same_record = host.record() == record
    assert same_record and state_path.read_bytes() == state and host.writer is None
    assert host.record().deadline == record.deadline
    assert host.backend._disk().digests == (
        confirmation.configuration,
        confirmation.selector,
        confirmation.unit,
    )
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()
    assert len(spans) == 1
    before, after = spans[0]
    assert_span_walks(host, role, reads, python, before, after, 1)
    # The payload observer counts its two real fixture leaves, not every
    # package member. Keep the original guard/opens/fences before the bytes.
    assert_only_unverified_selected_bytes(host, role, reads, before, after)


@pytest.mark.parametrize("role", ["target", "base"])
async def test_install_writer_probe_lends_controller_bytes_to_file_effects(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    host = shared_host
    record = await prepare_stopped(host, role, installed=False)
    commands = list(host.commands)
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    original = host.backend._install_files
    spans: list[tuple[ReadSnapshot, ReadSnapshot]] = []

    def install_files(
        current: TransactionRecordV2,
        selected: Role,
        data: RoleSnapshots,
        lease: FixedLock,
        cgroup: str,
        **kwargs: Any,
    ) -> None:
        assert host.probe.count == 2 and not spans and selected == role
        assert checks == [(record, value) for value in (role, None, role, None)]
        lease.check()
        before = snapshot(reads, python, checks)
        original(current, selected, data, lease, cgroup, **kwargs)
        spans.append((before, snapshot(reads, python, checks)))
        lease.check()

    monkeypatch.setattr(host.backend, "_install_files", install_files)

    confirmation = await host.backend.install(record, role, host.data[role])

    assert confirmation == expected_confirmation(record, role)
    assert checks == (
        [(record, value) for value in (role, None, role, None)]
        + [(record, role)] * 16
        + [(record, None)] * 2
        + [(record, role)] * 7
    )
    assert host.probe.count == 3 and host.commands == commands and host.transport.calls == []
    same_record = host.record() == record
    assert same_record and state_path.read_bytes() == state and host.writer is None
    assert host.backend._disk().digests == (
        confirmation.configuration,
        confirmation.selector,
        confirmation.unit,
    )
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()
    assert len(spans) == 1
    before, after = spans[0]
    assert_span_walks(host, role, reads, python, before, after, 16)
    assert_only_unverified_selected_bytes(host, role, reads, before, after)


@pytest.mark.parametrize("role", ["target", "base"])
async def test_install_files_lends_controller_bytes_to_final_probe_preflight(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    host = shared_host
    record = await prepare_stopped(host, role, installed=False)
    controller = Path(host.backend.controller_slot.package.root)
    target = Path(host.slots["target"].package.root)
    assert controller != target
    assert host.backend.controller_slot.resolved_python == host.slots[role].resolved_python
    python_size = host.backend.controller_slot.resolved_python.identity.size
    if role == "base":
        damaged = target / "batch_payload/first.bin"
        assert damaged.read_bytes().endswith(b"damaged opposite package")
    commands, transport_calls = list(host.commands), list(host.transport.calls)
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    service, cgroup_events = host.probe.snapshot, host.events.read_bytes()
    operation_timeout, probe_limits = host.backend.operation_timeout, host.probe.limits
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    original_files, original_read = host.backend._install_files, host.probe.read_managed
    file_returns: list[ReadSnapshot] = []
    final_probe_entries: list[ReadSnapshot] = []
    file_batches: list[_FileProofs] = []
    file_leases: list[FixedLock] = []
    lease_identities: list[tuple[int, int]] = []
    probes: list[ManagedProbeResult] = []

    def install_files(
        current: TransactionRecordV2,
        selected: Role,
        data: RoleSnapshots,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> None:
        original_files(current, selected, data, lease, cgroup, proofs=proofs)
        # The actual final _confirm returned; no await or substitute file effect.
        file_returns.append(snapshot(reads, python, checks))
        assert proofs is not None and proofs.python is not None and proofs.packages is not None
        file_batches.append(proofs)
        file_leases.append(lease)
        lease.check()
        assert lease.fd is not None
        info = os.fstat(lease.fd)
        lease_identities.append((info.st_dev, info.st_ino))

    async def read_managed(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller_slot: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        if host.probe.count == 2:
            # Snapshot after the real synchronous preflight, before delegating
            # the third manager read. The borrowed proof must already be gone.
            final_probe_entries.append(snapshot(reads, python, checks))
            assert len(file_batches) == 1
            assert file_batches[0].python is None and file_batches[0].packages is None
            file_leases[0].check()
        result = await original_read(journal, current, anchor, controller_slot, slots)
        probes.append(result)
        return result

    monkeypatch.setattr(host.backend, "_install_files", install_files)
    monkeypatch.setattr(host.probe, "read_managed", read_managed)

    confirmation = await host.backend.install(record, role, host.data[role])

    assert confirmation == expected_confirmation(record, role)
    expected_roles: list[Role | None] = (
        [role, None, role, None] + [role] * 16 + [None] * 2 + [role] * 7
    )
    assert len(checks) == 29 and all(current is record for current, _ in checks)
    assert [selected for _, selected in checks] == expected_roles
    assert host.probe.count == len(probes) == 3
    assert all(result.service == service and result.process_roles == () for result in probes)
    assert all(result.unit_confirmed for result in probes)
    assert probes[-1].disk_unit == record.binding(role).unit.digest
    assert host.commands == commands and host.transport.calls == transport_calls == []
    same_record = host.record() == record
    assert same_record and state_path.read_bytes() == state
    assert host.record().deadline == record.deadline
    assert host.backend.operation_timeout == operation_timeout and host.probe.limits == probe_limits
    assert host.events.read_bytes() == cgroup_events and host.writer is None
    disk = host.backend._disk()
    assert disk.configuration.data == host.data[role].configuration
    assert disk.selector is not None and disk.selector.value == host.selector(role)
    assert disk.unit.data == host.data[role].unit
    assert disk.digests == (confirmation.configuration, confirmation.selector, confirmation.unit)
    assert len(file_leases) == len(lease_identities) == 1 and file_leases[0].fd is None
    assert file_batches[0].python is None and file_batches[0].packages is None
    with host.journal.writer_lease() as available:
        available.check()
        assert available.fd is not None
        info = os.fstat(available.fd)
        assert (info.st_dev, info.st_ino) == lease_identities[0]
    host.control.check()

    for path, leaf in reads.leaves.items():
        walks = 29 if path.is_relative_to(controller) else 25 if role == "target" else 0
        assert len(leaf.opened) == walks, str(path)
        assert leaf.fd_stats >= 2 * walks and leaf.path_stats == walks, str(path)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in leaf.opened)
    for directory, count in reads.scans.items():
        walks = 29 if directory.is_relative_to(controller) else 25 if role == "target" else 0
        assert count == walks, str(directory)
    assert len(python.opened) == len(checks) + sum(selected == "target" for _, selected in checks)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)
    assert len(file_returns) == len(final_probe_entries) == 1
    before, after = file_returns[0], final_probe_entries[0]
    assert before.checks == 20 and after.checks == 21 and len(checks) - after.checks == 8
    assert after.python_opens - before.python_opens == 1
    for path in reads.leaves:
        walks = 1 if path.is_relative_to(controller) else 0
        assert after.leaves[path][1] - before.leaves[path][1] == walks, str(path)
        assert after.leaves[path][2] - before.leaves[path][2] >= 2 * walks, str(path)
        assert after.leaves[path][3] - before.leaves[path][3] == walks, str(path)
        if not walks:
            assert after.leaves[path][0] == before.leaves[path][0], str(path)
    for directory in reads.scans:
        walks = 1 if directory.is_relative_to(controller) else 0
        assert after.scans[directory] - before.scans[directory] == walks, str(directory)

    # The next actual manager await keeps its independent full authentication.
    assert python.bytes - after.python_bytes == python_size
    for path, size in reads.sizes.items():
        copies = 1 if path.is_relative_to(controller) or role == "target" else 0
        assert reads.leaves[path].bytes - after.leaves[path][0] == size * copies, str(path)
    extra_content = {
        path.relative_to(controller).as_posix(): after.leaves[path][0] - before.leaves[path][0]
        for path in reads.leaves
        if path.is_relative_to(controller)
    }
    extra_content["python"] = after.python_bytes - before.python_bytes
    # Only the two fixture leaves are metered, not all package members.
    assert extra_content == dict.fromkeys(extra_content, 0)


@pytest.mark.parametrize("role", ["target", "base"])
@pytest.mark.parametrize("reload", [False, True], ids=["already-loaded", "reload"])
async def test_install_final_probe_lends_controller_bytes_to_confirmation(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role, reload: bool
) -> None:
    host = shared_host
    record = await prepare_stopped(host, role, installed=False)
    if reload:
        host.probe.loaded_unit = b"previously loaded unit bytes"
    commands = list(host.commands)
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    original = host.backend._confirm
    spans: list[tuple[ReadSnapshot, ReadSnapshot]] = []
    final_probe = 4 if reload else 3

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        if host.probe.count != final_probe:
            return original(current, selected, lease, cgroup, proofs=proofs)
        assert not spans and selected == role
        lease.check()
        before = snapshot(reads, python, checks)
        result = original(current, selected, lease, cgroup, proofs=proofs)
        spans.append((before, snapshot(reads, python, checks)))
        lease.check()
        return result

    monkeypatch.setattr(host.backend, "_confirm", confirm)

    confirmation = await host.backend.install(record, role, host.data[role])

    assert confirmation == expected_confirmation(record, role)
    expected_checks: list[tuple[TransactionRecordV2, Role | None]] = (
        [(record, value) for value in (role, None, role, None)]
        + [(record, role)] * 16
        + [(record, None)] * 2
    )
    if reload:
        expected_checks += [(record, role), (record, role), (record, None)]
    assert checks == expected_checks + [(record, role)] * 7
    assert host.probe.count == final_probe and host.transport.calls == []
    assert host.commands == commands + (["daemon-reload"] if reload else [])
    same_record = host.record() == record
    assert same_record and host.writer is None
    assert host.backend._disk().digests == (
        confirmation.configuration,
        confirmation.selector,
        confirmation.unit,
    )
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()
    assert len(spans) == 1
    before, after = spans[0]
    assert_span_walks(host, role, reads, python, before, after, 7)
    assert_only_unverified_selected_bytes(host, role, reads, before, after)


def metadata_counts(reads: MetadataReads) -> dict[Path, int]:
    return {path: len(items) for path, items in reads.reads.items()}


@dataclass(frozen=True)
class RootFence:
    check: int
    metadata: dict[Path, int]
    scans: dict[Path, int]
    identity: dict[str, int]


@pytest.mark.parametrize("role", ["target", "base"])
async def test_real_start_confirm_lends_controller_content_to_second_probe_preflight(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    host = shared_host
    record = await prepare_start(host, role)
    controller_slot, selected_slot = host.backend.controller_slot, host.slots[role]
    controller, target = Path(controller_slot.package.root), Path(host.slots["target"].package.root)
    selected_root = Path(selected_slot.package.root)
    assert controller != target
    assert controller_slot.resolved_python == selected_slot.resolved_python
    if role == "base":
        # Keep real base recovery independent of the damaged opposite executable.
        damaged = target / next(iter(_PAYLOADS))
        damaged.write_bytes(damaged.read_bytes() + b"damaged opposite package")
        assert damaged.read_bytes().endswith(b"damaged opposite package")

    python_path = Path(controller_slot.resolved_python.path)
    python_size = controller_slot.resolved_python.identity.size
    assert python_size > 0
    assert managed_launcher.identity(python_path.stat()) == (
        controller_slot.resolved_python.identity.model_dump(mode="json")
    )
    assert (
        managed_launcher.digest(python_path.read_bytes()) == controller_slot.resolved_python.digest
    )
    checked_slots = {slot.digest: slot for slot in (controller_slot, selected_slot)}
    expected_scopes = {
        slot.digest: managed_launcher.digest(
            managed_launcher.canonical(slot.model_dump(mode="json"))
        )
        for slot in checked_slots.values()
    }
    payload_identities: dict[Path, dict[str, int]] = {}
    for slot in checked_slots.values():
        root = Path(slot.package.root)
        # Verify each actual full package before installing observation counters.
        assert managed_launcher.package_fingerprint(
            root, slot.package_limits.model_dump(mode="json")
        ) == slot.package.model_dump(mode="json")
        for relative, contents in _PAYLOADS.items():
            path = root / relative
            assert path.read_bytes() == contents
            payload_identities[path] = managed_launcher.identity(path.stat(follow_symlinks=False))

    commands, transport_calls = list(host.commands), list(host.transport.calls)
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    roles: tuple[Role, ...] = ("base", "target")
    frozen_snapshots: dict[Role, RoleSnapshots] = {
        selected: read_snapshots(host.journal, record, selected) for selected in roles
    }
    disk, service = host.backend._disk(), host.probe.snapshot
    cgroup_events, clock_elapsed = host.events.read_bytes(), host.clock.elapsed
    operation_timeout, probe_limits = host.backend.operation_timeout, host.probe.limits
    helper_limits, journal_limits = host.anchor.helper, host.journal.limits
    journal_root = host.journal.directory.path
    root_identity = managed_launcher.identity(journal_root.stat(follow_symlinks=False))
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    metadata = observe_metadata(host, monkeypatch)
    metadata_identities = {
        path: managed_launcher.identity(path.stat(follow_symlinks=False))
        for path in metadata.expected
    }
    expected_roles: list[Role | None] = [None, None] + [role] * 8 + [None, None] + [role, role]
    original_confirm, original_read = host.backend._confirm, host.probe.read_managed
    original_check, original_open_directory = host.backend._check, managed_launcher._open_directory
    confirm_returns: list[ReadSnapshot] = []
    second_probe_entries: list[ReadSnapshot] = []
    confirm_metadata: list[dict[Path, int]] = []
    second_metadata: list[dict[Path, int]] = []
    confirmation_disks: list[_DiskBinding] = []
    batches: list[_FileProofs] = []
    leases: list[FixedLock] = []
    lease_identities: list[tuple[int, int]] = []
    probes: list[ManagedProbeResult] = []
    root_fences: list[RootFence] = []
    inside_check = False

    def checked(
        current: TransactionRecordV2,
        selected: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        nonlocal inside_check
        assert not inside_check
        inside_check = True
        try:
            # The existing observer forwards to the complete real authentication.
            original_check(
                current,
                selected,
                verified_python=verified_python,
                verified_packages=verified_packages,
                observed_packages=observed_packages,
            )
        finally:
            inside_check = False

    def open_directory(path: Path) -> int:
        descriptor = original_open_directory(path)
        if inside_check and path == journal_root:
            root_fences.append(
                RootFence(
                    len(checks),
                    metadata_counts(metadata),
                    dict(reads.scans),
                    managed_launcher.identity(os.fstat(descriptor)),
                )
            )
        return descriptor

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        assert current is record and selected == role
        assert host.probe.count == 1 and not confirm_returns
        assert [selected for _current, selected in checks] == expected_roles[:3]
        assert proofs is not None
        result = original_confirm(current, selected, lease, cgroup, proofs=proofs)
        assert [selected for _current, selected in checks] == expected_roles[:10]
        assert proofs.python == controller_slot.resolved_python and proofs.packages is not None
        assert set(proofs.packages) == set(expected_scopes.values())
        for slot in checked_slots.values():
            leaves = proofs.packages[expected_scopes[slot.digest]]
            for relative, contents in _PAYLOADS.items():
                path = Path(slot.package.root) / relative
                assert leaves[str(path)] == {
                    "path": str(path),
                    "identity": payload_identities[path],
                    "digest": managed_launcher.digest(contents),
                }
        lease.check()
        assert lease.fd is not None
        info = os.fstat(lease.fd)
        batches.append(proofs)
        leases.append(lease)
        lease_identities.append((info.st_dev, info.st_ino))
        confirmation_disks.append(result)
        confirm_returns.append(snapshot(reads, python, checks))
        confirm_metadata.append(metadata_counts(metadata))
        # Publish only the real final confirmation's successful content proof.
        return result

    async def read_managed(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        current_controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        assert journal is host.journal and current is record
        assert anchor is host.anchor and current_controller is controller_slot
        assert slots == host.slots
        if host.probe.count == 1:
            # This runs after the real None preflight and before the original
            # second read. No event, sleep, task, gate or extra await is inserted.
            assert len(confirm_returns) == len(leases) == len(batches) == 1
            assert batches[0].python is None and batches[0].packages is None
            assert [selected for _current, selected in checks] == expected_roles[:11]
            second_probe_entries.append(snapshot(reads, python, checks))
            second_metadata.append(metadata_counts(metadata))
            leases[0].check()
        result = await original_read(journal, current, anchor, current_controller, slots)
        probes.append(result)
        return result

    monkeypatch.setattr(host.backend, "_check", checked)
    monkeypatch.setattr(managed_launcher, "_open_directory", open_directory)
    monkeypatch.setattr(host.backend, "_confirm", confirm)
    monkeypatch.setattr(host.probe, "read_managed", read_managed)

    result = await host.backend.start(record, role)

    # The whole old product operation must succeed before its cost is judged.
    assert result is None
    assert len(checks) == 14 and all(current is record for current, _selected in checks)
    assert [selected for _current, selected in checks] == expected_roles
    assert host.probe.count == len(probes) == 2
    assert all(probe.service == service and probe.process_roles == () for probe in probes)
    assert all(probe.unit_confirmed for probe in probes)
    assert all(probe.disk_unit == record.binding(role).unit.digest for probe in probes)
    assert host.commands == [*commands, "start"] and host.transport.calls == transport_calls == []
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    assert host.record().position == record.position and host.record().deadline == record.deadline
    assert all(
        read_snapshots(host.journal, record, selected) == frozen
        for selected, frozen in frozen_snapshots.items()
    )
    assert host.backend.operation_timeout == operation_timeout and host.probe.limits == probe_limits
    assert host.anchor.helper == helper_limits and host.journal.limits == journal_limits
    assert host.events.read_bytes() == cgroup_events and host.clock.elapsed == clock_elapsed
    assert len(confirmation_disks) == 1 and confirmation_disks[0] == disk
    process = host.probe.snapshot.process
    assert process is not None and process.argv == host.arguments(role)
    assert process.executable == selected_slot.resolved_python.path
    assert process.cgroup == service.unit.cgroup
    assert host.probe.snapshot.unit.invocation == (TARGET_BOOT if role == "target" else BASE_BOOT)
    assert host.probe.snapshot.unit.active_state == "active"
    assert host.probe.snapshot.unit.pid == process.pid and host.admission == "paused"

    assert len(leases) == len(lease_identities) == len(batches) == 1 and leases[0].fd is None
    assert batches[0].python is None and batches[0].packages is None
    assert host.writer is not None and host.writer.fd is not None
    host.writer.check()
    started_writer = os.fstat(host.writer.fd)
    # The existing fake manager really reacquired the released same writer inode.
    assert (started_writer.st_dev, started_writer.st_ino) == lease_identities[0]
    host.control.check()

    # Preserve every old executable open, full tree walk and leaf identity fence.
    for path, leaf in reads.leaves.items():
        walks = 14 if path.is_relative_to(controller) else 10 if role == "target" else 0
        assert len(leaf.opened) == walks, str(path)
        assert leaf.fd_stats >= 2 * walks and leaf.path_stats == walks, str(path)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in leaf.opened)
    for directory, count in reads.scans.items():
        walks = 14 if directory.is_relative_to(controller) else 10 if role == "target" else 0
        assert count == walks, str(directory)
    assert len(python.opened) == 14 + sum(selected == "target" for selected in expected_roles)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)

    # Actual private bytes, hash, nofollow and FD/path fences remain per guard.
    metadata.complete()
    assert metadata_counts(metadata) == dict.fromkeys(metadata.expected, 14)
    assert all(
        managed_launcher.identity(path.stat(follow_symlinks=False)) == expected
        for path, expected in metadata_identities.items()
    )
    assert len(root_fences) == 28
    for index, selected in enumerate(expected_roles, start=1):
        entered, finished = root_fences[2 * (index - 1) : 2 * index]
        assert entered.check == finished.check == index
        assert entered.identity == finished.identity == root_identity
        assert entered.metadata == dict.fromkeys(metadata.expected, index - 1)
        assert finished.metadata == dict.fromkeys(metadata.expected, index)
        previous_selected = sum(value == "target" for value in expected_roles[: index - 1])
        for directory in reads.scans:
            before_walks = index - 1 if directory.is_relative_to(controller) else previous_selected
            this_walk = int(directory.is_relative_to(controller) or selected == "target")
            assert entered.scans[directory] == before_walks, str(directory)
            assert finished.scans[directory] == before_walks + this_walk, str(directory)

    assert len(confirm_returns) == len(second_probe_entries) == 1
    assert len(confirm_metadata) == len(second_metadata) == 1
    before, after = confirm_returns[0], second_probe_entries[0]
    assert before.checks == 10 and after.checks == 11 and len(checks) - after.checks == 3
    assert after.python_opens - before.python_opens == 1
    assert confirm_metadata[0] == dict.fromkeys(metadata.expected, 10)
    assert second_metadata[0] == dict.fromkeys(metadata.expected, 11)
    for path in reads.leaves:
        walks = int(path.is_relative_to(controller))
        assert after.leaves[path][1] - before.leaves[path][1] == walks, str(path)
        assert after.leaves[path][2] - before.leaves[path][2] >= 2 * walks, str(path)
        assert after.leaves[path][3] - before.leaves[path][3] == walks, str(path)
        if not walks:
            assert after.leaves[path][0] == before.leaves[path][0], str(path)
    for directory in reads.scans:
        assert after.scans[directory] - before.scans[directory] == int(
            directory.is_relative_to(controller)
        ), str(directory)

    # The actual manager await retains the complete independent controller read.
    # Its later original role guard separately reads a distinct selected package.
    assert python.bytes - after.python_bytes == python_size
    for path, size in reads.sizes.items():
        copies = int(path.is_relative_to(controller) or path.is_relative_to(selected_root))
        assert reads.leaves[path].bytes - after.leaves[path][0] == size * copies, str(path)

    # Test postlude: after verifying the successful started owner, explicitly
    # close that fixture owner and prove the same real inode can be obtained again.
    host.release_writer()
    assert host.writer is None
    with host.journal.writer_lease() as available:
        available.check()
        assert available.fd is not None
        info = os.fstat(available.fd)
        assert (info.st_dev, info.st_ino) == lease_identities[0]
    host.control.check()

    # Meter only the registered fixture leaves plus fixture Python. This final
    # predicate alone is the expected old-product red; do not move it earlier.
    extra_content = {
        path.relative_to(controller).as_posix(): after.leaves[path][0] - before.leaves[path][0]
        for path in reads.leaves
        if path.is_relative_to(controller)
    }
    extra_content["python"] = after.python_bytes - before.python_bytes
    assert extra_content == dict.fromkeys(extra_content, 0)
