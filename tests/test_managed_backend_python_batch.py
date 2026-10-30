"""Reuse Python bytes within one confirmation, without skipping real guards."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_managed_backend import Host, track_slot_scans
from test_managed_backend_python_proof import observe_python_reads
from test_managed_backend_python_proof import shared_host as shared_host

from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role
from muli.platforms.managed_deployment import (
    ManagedDeploymentError,
    SelectorSnapshot,
    SelectorStore,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained slot identities")


def observe_checks(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[TransactionRecordV2, Role | None]]:
    checks: list[tuple[TransactionRecordV2, Role | None]] = []
    original = host.backend._check

    def checked(current: TransactionRecordV2, role: Role | None = None, **kwargs: Any) -> None:
        original(current, role, **kwargs)
        checks.append((current, role))

    monkeypatch.setattr(host.backend, "_check", checked)
    return checks


@pytest.mark.parametrize("role", ["base", "target"])
async def test_confirm_batch_hashes_python_once_but_keeps_all_seven_real_guards(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = shared_host.install_intent()
    if role == "target":
        await shared_host.backend.install(record, role, shared_host.data[role])
    before = shared_host.backend._disk()
    assert before.selector is not None
    reads = observe_python_reads(shared_host, monkeypatch)
    scans = track_slot_scans(monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    package_roots = [Path(shared_host.slots["base"].package.root)]
    if role == "target":
        package_roots.append(Path(shared_host.slots[role].package.root))

    with shared_host.journal.writer_lease() as lease:
        confirmed = shared_host.backend._confirm(
            record, role, lease, shared_host.probe.snapshot.unit.cgroup
        )

    assert confirmed == before == shared_host.backend._disk()
    assert checks == [(record, role)] * 7
    assert scans == package_roots * 7
    assert len(reads.opened) == len(package_roots) * 7
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in reads.opened)
    assert shared_host.record() == record and shared_host.commands == []
    # Count real bytes read from the shared interpreter, not SHA object creation:
    # even the existing verified-file path creates an unused empty SHA object.
    assert reads.bytes == shared_host.slots["base"].resolved_python.identity.size


async def test_next_confirm_batch_rehashes_python_even_without_an_await_between_calls(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = shared_host.install_intent()
    await shared_host.backend.install(record, "target", shared_host.data["target"])
    before = shared_host.backend._disk()
    reads = observe_python_reads(shared_host, monkeypatch)
    scans = track_slot_scans(monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    bytes_per_batch: list[int] = []

    with shared_host.journal.writer_lease() as lease:
        for _ in range(2):
            previous = reads.bytes
            assert (
                shared_host.backend._confirm(
                    record, "target", lease, shared_host.probe.snapshot.unit.cgroup
                )
                == before
            )
            bytes_per_batch.append(reads.bytes - previous)

    assert checks == [(record, "target")] * 14
    assert (
        scans
        == [
            Path(shared_host.slots["base"].package.root),
            Path(shared_host.slots["target"].package.root),
        ]
        * 14
    )
    assert len(reads.opened) == 28
    assert shared_host.backend._disk() == before
    assert shared_host.record() == record and shared_host.commands == []
    size = shared_host.slots["base"].resolved_python.identity.size
    assert bytes_per_batch == [size, size]


async def test_confirm_batch_rehashes_and_rejects_a_changed_python_identity(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = shared_host.install_intent()
    await shared_host.backend.install(record, "target", shared_host.data["target"])
    before = shared_host.backend._disk()
    binary = Path(shared_host.slots["base"].resolved_python.path)
    identity = binary.stat()
    reads = observe_python_reads(shared_host, monkeypatch)
    scans = track_slot_scans(monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    bytes_before_fault: list[int] = []
    original = SelectorStore.confirm

    def confirm(
        selector: SelectorStore,
        expected: SelectorSnapshot | None,
        *,
        guard: Callable[[], None],
    ) -> SelectorSnapshot | None:
        assert not bytes_before_fault
        # Configuration confirmation has completed its two real guards. The
        # selector still delegates to its original guard before any fsync.
        assert checks == [(record, "target")] * 2
        bytes_before_fault.append(reads.bytes)
        with binary.open("r+b") as output:
            output.write(b"BAD!")
        os.utime(binary, ns=(identity.st_atime_ns, identity.st_mtime_ns))
        changed = binary.stat()
        assert (changed.st_dev, changed.st_ino, changed.st_size, changed.st_mtime_ns) == (
            identity.st_dev,
            identity.st_ino,
            identity.st_size,
            identity.st_mtime_ns,
        )
        assert changed.st_ctime_ns != identity.st_ctime_ns
        return original(selector, expected, guard=guard)

    monkeypatch.setattr(SelectorStore, "confirm", confirm)
    with shared_host.journal.writer_lease() as lease:
        with pytest.raises(ManagedDeploymentError, match="^anchor_unavailable$"):
            shared_host.backend._confirm(
                record, "target", lease, shared_host.probe.snapshot.unit.cgroup
            )

    assert len(bytes_before_fault) == 1
    assert reads.bytes - bytes_before_fault[0] == identity.st_size
    assert checks == [(record, "target")] * 2
    assert (
        scans
        == [
            Path(shared_host.slots["base"].package.root),
            Path(shared_host.slots["target"].package.root),
        ]
        * 2
    )
    assert shared_host.backend._disk() == before
    assert shared_host.record() == record and shared_host.commands == []
