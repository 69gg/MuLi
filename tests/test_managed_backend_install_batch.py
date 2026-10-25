"""An install batch may reuse bytes only before its next real probe await."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from test_managed_backend import Host
from test_managed_backend_package_batch import observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import shared_host as shared_host

from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionJournal
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    ManagedDeploymentError,
    SelectorSnapshot,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
)
from muli.platforms.managed_probe import ManagedProbeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained install identities")


async def test_install_batch_reuses_payload_bytes_without_reducing_guards(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = shared_host.install_intent()
    roots = {
        "controller": Path(shared_host.backend.controller_slot.package.root),
        "target": Path(shared_host.slots["target"].package.root),
    }
    assert roots["controller"] != roots["target"]
    reads = observe_payloads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)

    await shared_host.backend.install(record, "target", shared_host.data["target"])

    # All original guards, walks and independent file identities precede the
    # byte-count oracle. A cheaper or omitted guard must not make this pass.
    assert len(checks) == 31
    assert checks.count((record, "target")) == 25
    assert checks.count((record, None)) == 6
    for leaf, observed in reads.leaves.items():
        walks = 31 if leaf.is_relative_to(roots["controller"]) else 25
        assert len(observed.opened) == walks, (leaf.name, len(observed.opened), walks)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.fd_stats >= 2 * walks, (leaf.name, observed.fd_stats, walks)
        assert observed.path_stats >= 2 * walks, (leaf.name, observed.path_stats, walks)
    for directory, scans in reads.scans.items():
        walks = 31 if directory.is_relative_to(roots["controller"]) else 25
        assert scans == walks, (directory.name, scans, walks)
    assert shared_host.probe.count == 3
    assert shared_host.commands == []
    assert shared_host.record().position == record.position
    disk = shared_host.backend._disk()
    assert disk.configuration.data == shared_host.data["target"].configuration
    assert disk.selector is not None and disk.selector.value == shared_host.selector("target")
    assert disk.unit.data == shared_host.data["target"].unit

    actual_bytes: dict[str, int] = {}
    expected_bytes: dict[str, int] = {}
    for leaf, observed in reads.leaves.items():
        name = "controller" if leaf.is_relative_to(roots["controller"]) else "target"
        key = f"{name}/{leaf.relative_to(roots[name]).as_posix()}"
        actual_bytes[key] = observed.bytes
        # The synchronous component loop and its first confirmation become one
        # batch. Pre-probe guards and the final independent confirmation remain.
        expected_bytes[key] = reads.sizes[leaf] * (10 if name == "controller" else 4)
    # Before batching, this reads 19/13 copies respectively. Keep this
    # failure to plain strings and integers, never a Host/identity object graph.
    assert actual_bytes == expected_bytes


async def test_install_batch_proof_ends_before_the_third_probe_await(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = shared_host.install_intent()
    controller = Path(shared_host.backend.controller_slot.package.root)
    reads = observe_payloads(shared_host, monkeypatch)
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
        if shared_host.probe.count == 2:
            assert not entered.is_set()
            entered.set()
            await release.wait()
        return await original(journal, current, anchor, controller_slot, slots)

    monkeypatch.setattr(shared_host.probe, "read_managed", gated)
    bytes_at_await: dict[Path, int] = {}
    checks_at_await = 0
    # TaskGroup owns and joins the fake operation even if an assertion fails;
    # the original backend operation budget bounds a gate that is never reached.
    async with asyncio.timeout(shared_host.backend.operation_timeout):
        async with asyncio.TaskGroup() as tasks:
            operation = tasks.create_task(
                shared_host.backend.install(record, "target", shared_host.data["target"])
            )
            await entered.wait()
            assert not operation.done()
            bytes_at_await = {leaf: observed.bytes for leaf, observed in reads.leaves.items()}
            checks_at_await = len(checks)
            release.set()

    assert entered.is_set() and shared_host.probe.count == 3
    assert len(checks) - checks_at_await == 8
    for leaf, observed in reads.leaves.items():
        # The probe's post-await controller guard hashes once, then the final
        # confirmation must start fresh for both controller and selected slot.
        copies = 2 if leaf.is_relative_to(controller) else 1
        actual = observed.bytes - bytes_at_await[leaf]
        assert actual == reads.sizes[leaf] * copies, (leaf.name, actual, copies)
    assert shared_host.record().position == record.position
    assert shared_host.commands == []


async def test_install_batch_rehashes_a_cross_component_ctime_change_and_rejects_it(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = shared_host.install_intent()
    before = shared_host.backend._disk()
    leaf = Path(shared_host.slots["target"].package.root) / "batch_payload/first.bin"
    contents, identity = leaf.read_bytes(), leaf.stat()
    reads = observe_payloads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    original = SelectorStore.replace
    bytes_before_change: list[int] = []

    def changed(
        selector: SelectorStore,
        expected: SelectorSnapshot | None,
        desired: SelectorValue | None,
        *,
        guard: Callable[[], None],
    ) -> SelectorSnapshot | None:
        assert not bytes_before_change
        # The first component completed two real guards. The selector must
        # still delegate its first guard before any selector publication.
        assert len(checks) == 8
        bytes_before_change.append(reads.leaves[leaf].bytes)
        with leaf.open("r+b") as output:
            output.write(bytes([contents[0] ^ 0xFF]) + contents[1:])
        os.utime(leaf, ns=(identity.st_atime_ns, identity.st_mtime_ns))
        after = leaf.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            identity.st_dev,
            identity.st_ino,
            identity.st_size,
            identity.st_mtime_ns,
        )
        assert after.st_ctime_ns != identity.st_ctime_ns
        return original(selector, expected, desired, guard=guard)

    monkeypatch.setattr(SelectorStore, "replace", changed)
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        await shared_host.backend.install(record, "target", shared_host.data["target"])

    assert len(bytes_before_change) == 1
    assert reads.leaves[leaf].bytes - bytes_before_change[0] >= len(contents)
    assert shared_host.backend._disk().digests == before.digests
    assert shared_host.record().position == record.position
    assert shared_host.probe.count == 2 and shared_host.commands == []
