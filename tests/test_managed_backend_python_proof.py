"""A Python digest may be reused only inside one real synchronous guard."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Self

import pytest
import test_managed_backend as fixtures

from muli.management_transaction import TransactionJournal
from muli.platforms import managed_backend, managed_deployment
from muli.platforms.managed_deployment import ManagedDeploymentError, SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained slot identities")


@pytest.fixture
async def shared_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[fixtures.Host]:
    # The existing Host performs real retained-slot registration/publication,
    # journal CAS and backend guards; its interpreter is never executed.
    binary = tmp_path / "shared-python"
    binary.write_bytes(b"\x7fELF" + bytes(range(256)) * 257)
    binary.chmod(0o700)
    original_slot = fixtures._slot
    variant = getattr(request, "param", "shared")

    def slot(root: Path, name: str) -> SlotIdentity:
        initial = original_slot(root, name)
        target = binary
        if name == "target" and variant == "different-binary":
            target = root / "other-python"
            target.write_bytes(binary.read_bytes())
            target.chmod(0o700)
        python = Path(initial.lexical_python)
        python.unlink()
        python.symlink_to(target)
        python_limit = (
            target.stat().st_size + 1
            if name == "target" and variant == "different-limit"
            else initial.python_bytes
        )
        return managed_deployment.register_local_slot(
            Path(initial.prefix), python, Path(initial.package.root), python_bytes=python_limit
        )

    monkeypatch.setattr(fixtures, "_slot", slot)
    with TransactionJournal(tmp_path / "journal", tmp_path / "data/muli.db") as journal:
        with journal.locked() as control:
            host = fixtures.Host(tmp_path, journal, control, "switch", change_config=False)
            try:
                assert host.slots["base"] != host.slots["target"]
                if variant != "different-binary":
                    assert (
                        host.slots["base"].resolved_python == host.slots["target"].resolved_python
                    )
                yield host
            finally:
                host.release_writer()
                await host.backend.__aexit__()


@dataclass
class PythonReads:
    bytes: int = 0
    opened: list[int] = field(default_factory=list)


class CountedReader:
    def __init__(self, source: BinaryIO, reads: PythonReads) -> None:
        self.source, self.reads = source, reads

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.source.close()

    def fileno(self) -> int:
        return self.source.fileno()

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.reads.bytes += len(data)
        return data


def observe_python_reads(host: fixtures.Host, monkeypatch: pytest.MonkeyPatch) -> PythonReads:
    identities = [Path(slot.resolved_python.path).stat() for slot in host.slots.values()]
    expected = {(info.st_dev, info.st_ino) for info in identities}
    reads = PythonReads()
    original_open, original_fdopen = os.open, os.fdopen

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) in expected:
            reads.opened.append(flags)
        return descriptor

    def stream(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original_fdopen(descriptor, *args, **kwargs)
        current = os.fstat(source.fileno())
        if (current.st_dev, current.st_ino) in expected:
            return CountedReader(source, reads)
        return source

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", stream)
    return reads


async def test_one_check_reads_shared_python_once_but_opens_each_slot_independently(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = observe_python_reads(shared_host, monkeypatch)
    record = shared_host.record()
    size = shared_host.slots["base"].resolved_python.identity.size
    shared_host.backend._check(record, "target")
    assert reads.bytes == size
    assert len(reads.opened) == 2
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in reads.opened)
    assert shared_host.commands == [] and shared_host.record() == record


async def test_next_check_hashes_the_shared_python_again(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = observe_python_reads(shared_host, monkeypatch)
    record = shared_host.record()
    size = shared_host.slots["base"].resolved_python.identity.size
    shared_host.backend._check(record, "target")
    first = reads.bytes
    shared_host.backend._check(record, "target")
    assert first == size and reads.bytes - first == size
    assert len(reads.opened) == 4 and shared_host.commands == []


@pytest.mark.parametrize("shared_host", ["different-limit"], indirect=True)
async def test_different_legal_slot_limits_still_share_the_same_verified_bytes(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, target = shared_host.slots["base"], shared_host.slots["target"]
    assert base.python_bytes != target.python_bytes
    assert base.resolved_python.identity.size < min(base.python_bytes, target.python_bytes)
    reads = observe_python_reads(shared_host, monkeypatch)
    shared_host.backend._check(shared_host.record(), "target")
    assert reads.bytes == base.resolved_python.identity.size
    assert len(reads.opened) == 2 and shared_host.commands == []


@pytest.mark.parametrize("shared_host", ["different-binary"], indirect=True)
async def test_distinct_python_files_each_require_a_complete_hash(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, target = shared_host.slots["base"], shared_host.slots["target"]
    assert base.resolved_python.path != target.resolved_python.path
    assert base.resolved_python.digest == target.resolved_python.digest
    reads = observe_python_reads(shared_host, monkeypatch)
    shared_host.backend._check(shared_host.record(), "target")
    assert reads.bytes == base.resolved_python.identity.size + target.resolved_python.identity.size
    assert len(reads.opened) == 2 and shared_host.commands == []


def at_selected_slot(
    host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, inject: Callable[[], None]
) -> list[bool]:
    original = managed_backend.verify_slot_files
    entered: list[bool] = []

    def verify(slot: SlotIdentity, **kwargs: Any) -> None:
        assert slot == host.slots["target"]
        entered.append(True)
        inject()
        original(slot, **kwargs)

    monkeypatch.setattr(managed_backend, "verify_slot_files", verify)
    return entered


async def test_same_inode_rewrite_with_restored_mtime_cannot_reuse_the_digest(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = Path(shared_host.slots["base"].resolved_python.path)
    before = binary.stat()
    reads = observe_python_reads(shared_host, monkeypatch)

    def rewrite() -> None:
        assert reads.bytes == before.st_size
        with binary.open("r+b") as output:
            output.write(b"BAD!")
        os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = binary.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        assert after.st_ctime_ns != before.st_ctime_ns

    entered = at_selected_slot(shared_host, monkeypatch, rewrite)
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        shared_host.backend._check(shared_host.record(), "target")
    assert entered == [True] and shared_host.commands == []


async def test_same_path_and_bytes_on_a_replaced_inode_cannot_reuse_the_digest(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = Path(shared_host.slots["base"].resolved_python.path)
    before = binary.stat()
    reads = observe_python_reads(shared_host, monkeypatch)

    def replace() -> None:
        assert reads.bytes == before.st_size
        replacement = binary.with_name("replacement-python")
        replacement.write_bytes(binary.read_bytes())
        replacement.chmod(before.st_mode & 0o777)
        replacement.replace(binary)
        assert binary.stat().st_ino != before.st_ino

    entered = at_selected_slot(shared_host, monkeypatch, replace)
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        shared_host.backend._check(shared_host.record(), "target")
    assert entered == [True] and shared_host.commands == []


async def test_selected_python_link_replacement_during_walk_is_rejected(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = Path(shared_host.slots["target"].lexical_python)
    parent = entry.parent.stat()
    original = os.readlink
    changed: list[bool] = []

    def readlink(path: Any, *, dir_fd: int | None = None) -> Any:
        value = original(path, dir_fd=dir_fd)
        info = os.fstat(dir_fd) if dir_fd is not None else None
        if (
            not changed
            and path == entry.name
            and info is not None
            and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino)
        ):
            entry.unlink()
            entry.symlink_to(value)
            changed.append(True)
        return value

    monkeypatch.setattr(os, "readlink", readlink)
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        shared_host.backend._check(shared_host.record(), "target")
    assert changed == [True] and shared_host.commands == []


async def test_shared_python_proof_does_not_skip_the_selected_package(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def corrupt() -> None:
        (Path(shared_host.slots["target"].package.root) / "__main__.py").write_bytes(b"changed")

    entered = at_selected_slot(shared_host, monkeypatch, corrupt)
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        shared_host.backend._check(shared_host.record(), "target")
    assert entered == [True] and shared_host.commands == []


async def test_selected_slot_smaller_python_byte_limit_is_not_bypassed(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = managed_deployment._verify_slot_files
    selected = shared_host.slots["target"]
    tightened: list[bool] = []

    def verify(slot: dict[str, Any], **kwargs: Any) -> None:
        if slot["prefix"] == selected.prefix:
            # Inject the tighter limit at the real standalone verifier's
            # boundary, after the backend has authenticated frozen metadata.
            slot = {**slot, "python_bytes": selected.resolved_python.identity.size - 1}
            tightened.append(True)
        original(slot, **kwargs)

    monkeypatch.setattr(managed_deployment, "_verify_slot_files", verify)
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_changed$"):
        shared_host.backend._check(shared_host.record(), "target")
    assert tightened == [True] and shared_host.commands == []
