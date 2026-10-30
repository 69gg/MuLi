"""A synchronous confirmation may reuse bytes, never its real file guards."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import test_managed_backend as fixtures
from test_managed_backend_python_proof import CountedReader, PythonReads
from test_managed_backend_python_proof import shared_host as shared_host

from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role
from muli.platforms.installation import FileFingerprint
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_deployment import (
    ManagedDeploymentError,
    SelectorSnapshot,
    SelectorStore,
    SlotIdentity,
    register_local_slot,
)
from muli.platforms.managed_launcher import LaunchError, PackageProofs, package_fingerprint

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained package identities")

_PAYLOADS = {
    "batch_payload/first.bin": bytes(range(64)) * 4,
    "batch_payload/nested/second.bin": b"retained package payload\n" * 7,
}


@pytest.fixture(autouse=True)
def package_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    # Autouse precedes shared_host: its existing slot builder registers these
    # bounded nested leaves before the real Host publishes either installation.
    original = fixtures._slot

    def slot(root: Path, name: str) -> SlotIdentity:
        initial = original(root, name)
        package = Path(initial.package.root)
        for relative, data in _PAYLOADS.items():
            leaf = package / relative
            leaf.parent.mkdir(parents=True, exist_ok=True)
            leaf.write_bytes(data)
        return register_local_slot(Path(initial.prefix), Path(initial.lexical_python), package)

    monkeypatch.setattr(fixtures, "_slot", slot)


@dataclass
class LeafReads(PythonReads):
    fd_stats: int = 0
    path_stats: int = 0


@dataclass
class PackageReads:
    leaves: dict[Path, LeafReads]
    sizes: dict[Path, int]
    scans: dict[Path, int] = field(default_factory=dict)


def observe_payloads(host: fixtures.Host, monkeypatch: pytest.MonkeyPatch) -> PackageReads:
    roots = {Path(slot.package.root) for slot in host.slots.values()}
    sizes = {root / relative: len(data) for root in roots for relative, data in _PAYLOADS.items()}
    reads = PackageReads({path: LeafReads() for path in sizes}, sizes)
    directories: dict[tuple[int, int], Path] = {}
    for root in roots:
        for relative in _PAYLOADS:
            parent = (root / relative).parent
            while parent.is_relative_to(root):
                info = parent.stat()
                directories[info.st_dev, info.st_ino] = parent
                reads.scans[parent] = 0
                parent = parent.parent

    opened_files: dict[int, Path] = {}
    original_open, original_fdopen = os.open, os.fdopen
    original_fstat, original_stat, original_scandir = os.fstat, os.stat, os.scandir

    def leaf_path(path: Any, dir_fd: int | None) -> Path | None:
        if isinstance(path, int):
            return opened_files.get(path)
        value = Path(os.fsdecode(path))
        if not value.is_absolute():
            if dir_fd is None:
                return None
            info = original_fstat(dir_fd)
            parent = directories.get((info.st_dev, info.st_ino))
            if parent is None:
                return None
            value = parent / value
        return value if value in reads.leaves else None

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        leaf = leaf_path(path, kwargs.get("dir_fd"))
        descriptor = original_open(path, flags, *args, **kwargs)
        opened_files.pop(descriptor, None)
        if leaf is not None:
            opened_files[descriptor] = leaf
            reads.leaves[leaf].opened.append(flags)
        return descriptor

    def stream(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original_fdopen(descriptor, *args, **kwargs)
        leaf = opened_files.get(descriptor)
        return source if leaf is None else CountedReader(source, reads.leaves[leaf])

    def fstat(descriptor: int) -> os.stat_result:
        leaf = opened_files.get(descriptor)
        if leaf is not None:
            reads.leaves[leaf].fd_stats += 1
        return original_fstat(descriptor)

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        leaf = leaf_path(path, kwargs.get("dir_fd"))
        if leaf is not None:
            reads.leaves[leaf].path_stats += 1
        return original_stat(path, *args, **kwargs)

    def scandir(path: Any = ".") -> Any:
        if isinstance(path, int):
            info = original_fstat(path)
            directory = directories.get((info.st_dev, info.st_ino))
        else:
            directory = Path(os.fsdecode(path))
        if directory in reads.scans:
            reads.scans[directory] += 1
        return original_scandir(path)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", stream)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "scandir", scandir)
    return reads


def observe_checks(
    host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[TransactionRecordV2, Role | None]]:
    checks: list[tuple[TransactionRecordV2, Role | None]] = []
    original = host.backend._check

    def counted(
        record: TransactionRecordV2,
        role: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        checks.append((record, role))
        original(
            record,
            role,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )

    monkeypatch.setattr(host.backend, "_check", counted)
    return checks


async def prepare_confirmation(host: fixtures.Host, role: Role) -> TransactionRecordV2:
    record = host.install_intent()
    if role == "target":
        await host.backend.install(record, role, host.data[role])
    return record


def assert_complete_walks(
    reads: PackageReads, host: fixtures.Host, role: Role, confirmations: int
) -> None:
    roots = {Path(host.backend.controller_slot.package.root), Path(host.slots[role].package.root)}
    for leaf, observed in reads.leaves.items():
        selected = any(leaf.is_relative_to(root) for root in roots)
        guards = 7 * confirmations if selected else 0
        assert len(observed.opened) == guards
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.fd_stats >= 2 * guards
        assert observed.path_stats == guards
    for directory, scans in reads.scans.items():
        selected = any(directory.is_relative_to(root) for root in roots)
        assert scans == (7 * confirmations if selected else 0)


def verified_roots(host: fixtures.Host, role: Role) -> set[Path]:
    return {
        Path(host.backend.controller_slot.package.root),
        Path(host.slots[role].package.root),
    }


@pytest.mark.parametrize("role", ["base", "target"])
async def test_confirmation_reads_each_package_payload_once_but_keeps_all_seven_guards(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_confirmation(shared_host, role)
    before = shared_host.backend._disk()
    reads = observe_payloads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    with shared_host.journal.writer_lease() as lease:
        confirmed = shared_host.backend._confirm(
            record, role, lease, shared_host.probe.snapshot.unit.cgroup
        )

    assert confirmed == before == shared_host.backend._disk()
    assert checks == [(record, role)] * 7
    assert_complete_walks(reads, shared_host, role, 1)
    roots = verified_roots(shared_host, role)
    for leaf, observed in reads.leaves.items():
        expected = reads.sizes[leaf] if any(leaf.is_relative_to(root) for root in roots) else 0
        assert observed.bytes == expected, (leaf.name, observed.bytes, expected)
    assert shared_host.record() == record and shared_host.commands == []


@pytest.mark.parametrize("role", ["base", "target"])
async def test_independent_confirmations_without_an_await_each_read_the_package_again(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_confirmation(shared_host, role)
    before = shared_host.backend._disk()
    reads = observe_payloads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    with shared_host.journal.writer_lease() as lease:
        first = shared_host.backend._confirm(
            record, role, lease, shared_host.probe.snapshot.unit.cgroup
        )
        first_bytes = {leaf: observed.bytes for leaf, observed in reads.leaves.items()}
        second = shared_host.backend._confirm(
            record, role, lease, shared_host.probe.snapshot.unit.cgroup
        )

    assert first == second == before == shared_host.backend._disk()
    assert checks == [(record, role)] * 14
    assert_complete_walks(reads, shared_host, role, 2)
    roots = verified_roots(shared_host, role)
    for leaf, observed in reads.leaves.items():
        expected = reads.sizes[leaf] if any(leaf.is_relative_to(root) for root in roots) else 0
        assert first_bytes[leaf] == expected
        assert observed.bytes - first_bytes[leaf] == expected
    assert shared_host.record() == record and shared_host.commands == []


def at_selector_confirmation(
    monkeypatch: pytest.MonkeyPatch, mutate: Callable[[], None]
) -> list[bool]:
    original = SelectorStore.confirm
    entered: list[bool] = []

    def confirm(
        selector: SelectorStore,
        expected: SelectorSnapshot | None,
        *,
        guard: Callable[[], None],
    ) -> SelectorSnapshot | None:
        if not entered:
            entered.append(True)
            mutate()
        return original(selector, expected, guard=guard)

    monkeypatch.setattr(SelectorStore, "confirm", confirm)
    return entered


@pytest.mark.parametrize("role", ["base", "target"])
async def test_same_inode_rewrite_with_restored_mtime_is_read_again_and_rejected(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_confirmation(shared_host, role)
    before_disk = shared_host.backend._disk()
    leaf = Path(shared_host.slots[role].package.root) / "batch_payload/first.bin"
    contents, before = leaf.read_bytes(), leaf.stat()
    reads = observe_payloads(shared_host, monkeypatch)
    bytes_before_mutation: list[int] = []

    def rewrite() -> None:
        bytes_before_mutation.append(reads.leaves[leaf].bytes)
        with leaf.open("r+b") as output:
            output.write(bytes([contents[0] ^ 0xFF]) + contents[1:])
        os.utime(leaf, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = leaf.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        assert after.st_ctime_ns != before.st_ctime_ns

    entered = at_selector_confirmation(monkeypatch, rewrite)
    with shared_host.journal.writer_lease() as lease:
        with pytest.raises(ManagedDeploymentError):
            shared_host.backend._confirm(
                record, role, lease, shared_host.probe.snapshot.unit.cgroup
            )

    assert entered == [True] and len(bytes_before_mutation) == 1
    assert reads.leaves[leaf].bytes - bytes_before_mutation[0] >= len(contents)
    assert shared_host.backend._disk() == before_disk
    assert shared_host.record() == record and shared_host.commands == []


@pytest.mark.parametrize("changed", ["inode", "mode"])
async def test_same_package_bytes_with_new_identity_are_read_again_and_remain_accepted(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    record = await prepare_confirmation(shared_host, "target")
    before_disk = shared_host.backend._disk()
    leaf = Path(shared_host.slots["target"].package.root) / "batch_payload/first.bin"
    contents, before = leaf.read_bytes(), leaf.stat()
    reads = observe_payloads(shared_host, monkeypatch)
    bytes_before_mutation: list[int] = []

    def change_identity() -> None:
        bytes_before_mutation.append(reads.leaves[leaf].bytes)
        if changed == "inode":
            replacement = leaf.with_name("replacement.bin")
            replacement.write_bytes(contents)
            replacement.chmod(stat.S_IMODE(before.st_mode))
            replacement.replace(leaf)
            assert leaf.stat().st_ino != before.st_ino
        else:
            leaf.chmod(stat.S_IMODE(before.st_mode) ^ stat.S_IXUSR)
            assert leaf.stat().st_mode != before.st_mode

    entered = at_selector_confirmation(monkeypatch, change_identity)
    with shared_host.journal.writer_lease() as lease:
        confirmed = shared_host.backend._confirm(
            record, "target", lease, shared_host.probe.snapshot.unit.cgroup
        )

    # PackageFingerprint freezes names, sizes and bytes, not leaf inode/mode.
    # The changed identity revokes reuse, without inventing a persistent gate.
    assert entered == [True] and len(bytes_before_mutation) == 1
    assert reads.leaves[leaf].bytes - bytes_before_mutation[0] >= len(contents)
    assert confirmed == before_disk == shared_host.backend._disk()
    assert shared_host.record() == record and shared_host.commands == []


@pytest.mark.parametrize("changed", ["added", "deleted", "symlink", "controller"])
async def test_new_leaf_deleted_leaf_symlink_and_other_verified_slot_damage_are_rejected(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    record = await prepare_confirmation(shared_host, "target")
    before_disk = shared_host.backend._disk()
    reads = observe_payloads(shared_host, monkeypatch)
    role: Role = "base" if changed == "controller" else "target"
    package = Path(shared_host.slots[role].package.root)
    leaf = package / "batch_payload/first.bin"
    before_reads: list[int] = []

    def change_package() -> None:
        before_reads.append(reads.leaves[leaf].bytes)
        if changed == "added":
            (package / "batch_payload/nested/new.bin").write_bytes(b"new leaf")
        elif changed == "deleted":
            leaf.unlink()
        elif changed == "symlink":
            leaf.unlink()
            leaf.symlink_to(package / "batch_payload/nested/second.bin")
        else:
            with leaf.open("r+b") as output:
                output.write(b"changed")

    entered = at_selector_confirmation(monkeypatch, change_package)
    with shared_host.journal.writer_lease() as lease:
        with pytest.raises(ManagedDeploymentError):
            shared_host.backend._confirm(
                record, "target", lease, shared_host.probe.snapshot.unit.cgroup
            )

    assert entered == [True] and len(before_reads) == 1
    if changed == "controller":
        # The selected target's proof cannot authorize its distinct controller.
        assert reads.leaves[leaf].bytes - before_reads[0] >= reads.sizes[leaf]
    assert shared_host.backend._disk() == before_disk
    assert shared_host.record() == record and shared_host.commands == []


async def test_failed_first_guard_does_not_lend_partial_package_proof_to_the_next_confirmation(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = await prepare_confirmation(shared_host, "target")
    before = shared_host.backend._disk()
    reads = observe_payloads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    shared_host.events.write_bytes(b"populated 1\n")
    with shared_host.journal.writer_lease() as lease:
        with pytest.raises(ObservationError):
            shared_host.backend._confirm(
                record, "target", lease, shared_host.probe.snapshot.unit.cgroup
            )
        first_bytes = {leaf: observed.bytes for leaf, observed in reads.leaves.items()}
        assert shared_host.backend._disk() == before
        shared_host.events.write_bytes(b"populated 0\n")
        confirmed = shared_host.backend._confirm(
            record, "target", lease, shared_host.probe.snapshot.unit.cgroup
        )

    assert confirmed == before == shared_host.backend._disk()
    assert checks == [(record, "target")] * 8
    for leaf, observed in reads.leaves.items():
        assert first_bytes[leaf] == reads.sizes[leaf]
        assert observed.bytes - first_bytes[leaf] == reads.sizes[leaf]
    assert shared_host.record() == record and shared_host.commands == []


@pytest.mark.parametrize("budget", ["entries", "total_bytes", "file_bytes", "depth"])
def test_cached_package_walk_rejects_stricter_budgets(tmp_path: Path, budget: str) -> None:
    root = tmp_path / "package"
    payloads = {"first.bin": b"abc", "nested/deep/second.bin": b"12345"}
    for relative, data in payloads.items():
        leaf = root / relative
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(data)
    limits = {
        "entries": 4,  # Two leaves and the two nested directories.
        "total_bytes": sum(map(len, payloads.values())),
        "file_bytes": max(map(len, payloads.values())),
        "depth": 2,
    }
    leaves: dict[str, dict[str, Any]] = {}
    original = package_fingerprint(root, limits, observed=leaves)
    assert set(leaves) == {str(root / relative) for relative in payloads}
    assert original["files"] == len(payloads)
    assert original["size"] == limits["total_bytes"]
    assert package_fingerprint(root, limits, verified=leaves) == original

    # These are actual unchanged leaf proofs, still eligible for digest reuse.
    # A lower traversal/read budget must reject even with those exact proofs.
    stricter = {**limits, budget: limits[budget] - 1}
    with pytest.raises(LaunchError):
        package_fingerprint(root, stricter, verified=leaves)
