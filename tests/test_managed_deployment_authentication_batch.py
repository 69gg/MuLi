"""One synchronous guard authenticates actual metadata, never frozen DTOs."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import pytest
import test_managed_backend as fixtures
from test_managed_backend import Host, track_slot_scans
from test_managed_backend import host as host
from test_managed_backend_python_proof import CountedReader, PythonReads

from muli.platforms import managed_launcher
from muli.platforms.managed_deployment import (
    SlotIdentity,
    register_local_slot,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux retained deployment identities"
)


@pytest.fixture(autouse=True)
def bounded_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the existing real slot publisher, with an interpreter never executed."""
    binary = tmp_path / "shared-python"
    binary.write_bytes(b"\x7fELF" + bytes(range(64)) * 17)
    binary.chmod(0o700)
    original = fixtures._slot

    def slot(root: Path, name: str) -> SlotIdentity:
        initial = original(root, name)
        entry = Path(initial.lexical_python)
        entry.unlink()
        entry.symlink_to(binary)
        return register_local_slot(Path(initial.prefix), entry, Path(initial.package.root))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("authentication fixture must not spawn a process")

    monkeypatch.setattr(fixtures, "_slot", slot)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


@dataclass
class MetadataRead(PythonReads):
    payload: bytearray = field(default_factory=bytearray, repr=False)
    fd_stats: int = 0
    path_stats: int = 0


class MetadataStream(CountedReader):
    def __init__(self, source: BinaryIO, reads: MetadataRead) -> None:
        super().__init__(source, reads)
        self.observed = reads

    def read(self, size: int = -1) -> bytes:
        data = super().read(size)
        self.observed.payload.extend(data)
        return data


@dataclass
class MetadataReads:
    expected: dict[Path, bytes] = field(repr=False)
    reads: dict[Path, list[MetadataRead]]

    def complete(self) -> None:
        # These are actual os.fdopen reads, not calls to read_slot or model
        # validation. Check full bytes/SHA and nofollow/identity gates before
        # checking how many copies the synchronous operation needed.
        for path, expected in self.expected.items():
            observed = self.reads[path]
            assert observed, path.name
            digest = path.stem.split("-", 1)[1] if path.suffix == ".json" else None
            if digest is not None:
                assert hashlib.sha256(expected).hexdigest() == digest
            for item in observed:
                assert bytes(item.payload) == expected, path.name
                assert item.bytes == len(expected), path.name
                assert item.fd_stats >= 2 and item.path_stats >= 1, path.name
                assert len(item.opened) == 1, path.name
                assert item.opened[0] & os.O_NOFOLLOW and item.opened[0] & os.O_NONBLOCK


def observe_metadata(host: Host, monkeypatch: pytest.MonkeyPatch) -> MetadataReads:
    root = host.journal.directory.path
    slots = {host.backend.controller_slot.digest, *(slot.digest for slot in host.slots.values())}
    paths = {root / f"slot-{digest}.json" for digest in slots}
    paths.update({root / f"anchor-{host.anchor.digest}.json", Path(host.anchor.launcher.path)})
    reads = MetadataReads({path: path.read_bytes() for path in paths}, {path: [] for path in paths})
    parent = root.stat()
    opened_files: dict[int, tuple[Path, MetadataRead]] = {}
    original_open, original_fdopen = os.open, os.fdopen
    original_fstat, original_stat = os.fstat, os.stat

    def selected(path: Any, dir_fd: int | None) -> Path | None:
        if isinstance(path, int):
            item = opened_files.get(path)
            return None if item is None else item[0]
        value = Path(os.fsdecode(path))
        if not value.is_absolute():
            if dir_fd is None:
                return None
            info = original_fstat(dir_fd)
            if (info.st_dev, info.st_ino) != (parent.st_dev, parent.st_ino):
                return None
            value = root / value
        return value if value in reads.reads else None

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        leaf = selected(path, kwargs.get("dir_fd"))
        descriptor = original_open(path, flags, *args, **kwargs)
        opened_files.pop(descriptor, None)
        if leaf is not None:
            item = MetadataRead(opened=[flags])
            opened_files[descriptor] = leaf, item
            reads.reads[leaf].append(item)
        return descriptor

    def stream(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original_fdopen(descriptor, *args, **kwargs)
        item = opened_files.get(descriptor)
        return source if item is None else MetadataStream(source, item[1])

    def fstat(descriptor: int) -> os.stat_result:
        info = original_fstat(descriptor)
        item = opened_files.get(descriptor)
        if item is not None:
            item[1].fd_stats += 1
        return info

    def path_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        leaf = selected(path, kwargs.get("dir_fd"))
        if leaf is not None:
            assert kwargs.get("follow_symlinks") is False
            if opened_files:
                # File descriptors may already be closed at the final path
                # check; associate that observation with the latest real open.
                reads.reads[leaf][-1].path_stats += 1
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", stream)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "stat", path_stat)
    return reads


@pytest.mark.parametrize("host", [("adopt", False), ("switch", False)], indirect=True)
async def test_one_guard_reads_each_actual_slot_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, disk = host.record(), host.backend._disk()
    reads = observe_metadata(host, monkeypatch)
    scans = track_slot_scans(monkeypatch)

    host.backend._check(record, "target")

    reads.complete()
    roots = [Path(host.backend.controller_slot.package.root)]
    if host.slots["target"] != host.backend.controller_slot:
        roots.append(Path(host.slots["target"].package.root))
    assert scans == roots
    assert host.record() == record and host.backend._disk() == disk
    assert host.writer is not None
    host.writer.check()
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []
    # Old code reads the controller four times for adopt, or three times plus
    # one distinct target for switch. This is the final, necessary red oracle.
    counts = {
        path.name: len(items)
        for path, items in reads.reads.items()
        if path.name.startswith("slot-")
    }
    assert counts == {name: 1 for name in counts}


async def test_next_guard_independently_rereads_every_actual_metadata_file(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.record()
    reads = observe_metadata(host, monkeypatch)
    host.backend._check(record, "target")
    first = {path: len(items) for path, items in reads.reads.items()}

    host.backend._check(record, "target")

    reads.complete()
    assert all(first.values())
    assert {path: len(items) for path, items in reads.reads.items()} == {
        path: count * 2 for path, count in first.items()
    }
    assert host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("host", [("adopt", False), ("switch", False)], indirect=True)
async def test_one_guard_walks_journal_root_only_at_entry_and_exit(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, disk = host.record(), host.backend._disk()
    root = host.journal.directory.path
    reads = observe_metadata(host, monkeypatch)
    scans = track_slot_scans(monkeypatch)
    walked: list[tuple[Path, bool, tuple[Path, ...]]] = []
    original = managed_launcher._open_directory

    def open_directory(path: Path) -> int:
        descriptor = original(path)
        if path == root:
            walked.append((path, any(reads.reads.values()), tuple(scans)))
        return descriptor

    monkeypatch.setattr(managed_launcher, "_open_directory", open_directory)

    host.backend._check(record, "target")

    reads.complete()
    roots = [Path(host.backend.controller_slot.package.root)]
    if host.slots["target"] != host.backend.controller_slot:
        roots.append(Path(host.slots["target"].package.root))
    assert scans == roots
    assert host.record() == record and host.backend._disk() == disk
    assert host.writer is not None
    host.writer.check()
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []
    # Observe, never replace, the real walks. The first precedes metadata and
    # package reads; the final walk follows both controller and selected scans.
    assert walked == [(root, False, ()), (root, True, tuple(roots))]
