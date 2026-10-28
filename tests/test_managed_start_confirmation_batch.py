"""Start may reuse bytes only inside its adjacent writer-held confirmation."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from test_managed_backend import Host
from test_managed_backend_package_batch import observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import observe_python_reads
from test_managed_backend_python_proof import shared_host as shared_host

from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import FixedLock, Role, TransactionJournal
from muli.platforms.managed_backend import _DiskBinding, _FileProofs
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    ManagedDeploymentError,
    SlotIdentity,
)
from muli.platforms.managed_probe import ManagedProbeResult
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real start file identities")


async def prepare_start(host: Host, role: Role) -> TransactionRecordV2:
    """Use real install/CAS/leases; only the existing manager runner is fake."""
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    if role == "target":
        record = host.move("activating", "start_target")
    else:
        host.move("rolling_back", "stop_target", code="backend_failed")
        record = host.move("rolling_back", "restore_base", code="backend_failed")
        await host.backend.install(record, "base", host.data["base"])
        record = host.move("rolling_back", "start_base", code="backend_failed")
    host.probe.count = 0
    return record


def rewrite_with_restored_mtime(path: Path, contents: bytes, before: os.stat_result) -> None:
    with path.open("r+b") as output:
        output.write(bytes([contents[0] ^ 0xFF]) + contents[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert after.st_ctime_ns != before.st_ctime_ns


@pytest.mark.parametrize("role", ["target", "base"])
async def test_start_confirmation_reuses_adjacent_guard_bytes_but_keeps_all_guards(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_start(shared_host, role)
    disk = shared_host.backend._disk().digests
    commands = list(shared_host.commands)
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    original = shared_host.backend._confirm
    confirmation_bytes: dict[Path, int] = {}
    confirmation_python: list[int] = []
    confirmation_checks: list[int] = []

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        assert not confirmation_checks and selected == role
        lease.check()
        before = {leaf: observed.bytes for leaf, observed in reads.leaves.items()}
        before_python, before_checks = python.bytes, len(checks)
        result = original(current, selected, lease, cgroup, proofs=proofs)
        confirmation_bytes.update(
            {leaf: observed.bytes - before[leaf] for leaf, observed in reads.leaves.items()}
        )
        confirmation_python.append(python.bytes - before_python)
        confirmation_checks.append(len(checks) - before_checks)
        lease.check()
        return result

    monkeypatch.setattr(shared_host.backend, "_confirm", confirm)

    await shared_host.backend.start(record, role)

    assert confirmation_checks == [7]
    assert len(checks) == 14
    assert all(current == record for current, _selected in checks)
    assert sum(selected is None for _current, selected in checks) == 4
    assert sum(selected == role for _current, selected in checks) == 10
    assert shared_host.probe.count == 2
    assert shared_host.commands == [*commands, "start"]
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.probe.snapshot.process is not None
    assert shared_host.probe.snapshot.process.argv == shared_host.arguments(role)
    assert shared_host.writer is not None
    shared_host.writer.check()

    controller = Path(shared_host.backend.controller_slot.package.root)
    selected_root = Path(shared_host.slots[role].package.root)
    for leaf, observed in reads.leaves.items():
        walks = (
            14
            if leaf.is_relative_to(controller)
            else 10
            if leaf.is_relative_to(selected_root)
            else 0
        )
        assert len(observed.opened) == walks, (leaf.name, len(observed.opened), walks)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.fd_stats >= 2 * walks, (leaf.name, observed.fd_stats, walks)
        assert observed.path_stats == walks, (leaf.name, observed.path_stats, walks)
    for directory, count in reads.scans.items():
        walks = (
            14
            if directory.is_relative_to(controller)
            else 10
            if directory.is_relative_to(selected_root)
            else 0
        )
        assert count == walks, (directory.name, count, walks)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)

    # The real adjacent guard already authenticated both installations. The
    # old start path re-reads one complete payload/Python here; all guards,
    # independent opens, identity checks and successful start above pass first.
    actual = {str(leaf): count for leaf, count in confirmation_bytes.items()}
    actual["python"] = sum(confirmation_python)
    assert actual == {key: 0 for key in actual}


@pytest.mark.parametrize("changed", ["controller", "selected", "both", "python"])
async def test_start_confirmation_rehashes_changed_files_and_keeps_controller_first(
    shared_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    changed: Literal["controller", "selected", "both", "python"],
) -> None:
    record = await prepare_start(shared_host, "target")
    disk = shared_host.backend._disk().digests
    commands = list(shared_host.commands)
    roles: tuple[Role, ...] = (
        ("target", "base")
        if changed == "both"
        else ("target",)
        if changed == "selected"
        else ("base",)
    )
    paths = (
        [Path(shared_host.backend.controller_slot.resolved_python.path)]
        if changed == "python"
        else [
            Path(shared_host.slots[role].package.root) / "batch_payload/first.bin" for role in roles
        ]
    )
    originals = {path: (path.read_bytes(), path.stat()) for path in paths}
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    original = ConfigurationFile.confirm
    entered: list[bool] = []
    before_bytes: dict[Path, int] = {}
    before_python: list[int] = []

    def confirm(
        file: ConfigurationFile, expected: str, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        if not entered and file.path == Path(shared_host.anchor.config_path):
            entered.append(True)
            # Two initial probe guards and the writer-held guard have passed.
            # The real file confirmation still runs its own original guards.
            assert len(checks) == 3
            before_bytes.update({leaf: observed.bytes for leaf, observed in reads.leaves.items()})
            before_python.append(python.bytes)
            for path in paths:
                contents, identity = originals[path]
                rewrite_with_restored_mtime(path, contents, identity)
        return original(file, expected, guard=guard)

    monkeypatch.setattr(ConfigurationFile, "confirm", confirm)
    code = "managed_slot_changed" if changed == "selected" else "anchor_unavailable"

    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        await shared_host.backend.start(record, "target")

    assert entered == [True] and len(before_python) == 1
    assert shared_host.probe.count == 1
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.commands == commands
    with shared_host.journal.writer_lease() as lease:
        lease.check()
    if changed == "python":
        assert python.bytes - before_python[0] >= len(originals[paths[0]][0])
    else:
        controller = Path(shared_host.backend.controller_slot.package.root)
        damaged = paths[-1] if changed == "both" else paths[0]
        assert reads.leaves[damaged].bytes - before_bytes[damaged] >= reads.sizes[damaged]
        if changed == "both":
            # Both real files changed, selected first. Controller authentication
            # must fail before the distinct selected package is even revisited.
            selected = paths[0]
            assert not selected.is_relative_to(controller)
            assert reads.leaves[selected].bytes == before_bytes[selected]


@pytest.mark.parametrize("role", ["target", "base"])
async def test_start_confirmation_proof_does_not_cross_the_next_probe_await(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_start(shared_host, role)
    disk, commands = shared_host.backend._disk().digests, list(shared_host.commands)
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = shared_host.probe.read_managed

    async def gated(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller_slot: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        if shared_host.probe.count == 1:
            assert not entered.is_set()
            entered.set()
            await release.wait()
        return await original(journal, current, anchor, controller_slot, slots)

    monkeypatch.setattr(shared_host.probe, "read_managed", gated)
    before_bytes: dict[Path, int] = {}
    before_opens: dict[Path, int] = {}
    before_scans: dict[Path, int] = {}
    before_python, before_checks = 0, 0
    # The existing backend operation budget bounds this fake-manager gate.
    # TaskGroup owns and joins the operation even when an assertion fails.
    async with asyncio.timeout(shared_host.backend.operation_timeout):
        async with asyncio.TaskGroup() as tasks:
            operation = tasks.create_task(shared_host.backend.start(record, role))
            await entered.wait()
            assert not operation.done()
            before_checks = len(checks)
            before_bytes = {leaf: observed.bytes for leaf, observed in reads.leaves.items()}
            before_opens = {leaf: len(observed.opened) for leaf, observed in reads.leaves.items()}
            before_scans = dict(reads.scans)
            before_python = python.bytes
            release.set()

    assert entered.is_set() and shared_host.probe.count == 2
    assert before_checks == 11 and len(checks) - before_checks == 3
    assert shared_host.commands == [*commands, "start"]
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    controller = Path(shared_host.backend.controller_slot.package.root)
    selected_root = Path(shared_host.slots[role].package.root)
    for leaf, observed in reads.leaves.items():
        copies = (
            3 if leaf.is_relative_to(controller) else 2 if leaf.is_relative_to(selected_root) else 0
        )
        actual = observed.bytes - before_bytes[leaf]
        assert actual == reads.sizes[leaf] * copies, (leaf.name, actual, copies)
        assert len(observed.opened) - before_opens[leaf] == copies
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
    for directory, count in reads.scans.items():
        copies = (
            3
            if directory.is_relative_to(controller)
            else 2
            if directory.is_relative_to(selected_root)
            else 0
        )
        assert count - before_scans[directory] == copies
    python_size = shared_host.backend.controller_slot.resolved_python.identity.size
    assert python.bytes - before_python == 3 * python_size
    assert shared_host.writer is not None
    shared_host.writer.check()
