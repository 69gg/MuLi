"""Package classification must share the leaf's real descriptor checks."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Self

import pytest
import test_managed_backend as fixtures
from test_managed_backend_package_batch import observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import shared_host as shared_host

from muli.platforms.managed_launcher import LaunchError, fingerprint, package_fingerprint

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained package identities")


@dataclass
class DescriptorTrace:
    opened: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    active: set[int] = field(default_factory=set)


def observe_descriptors(monkeypatch: pytest.MonkeyPatch) -> DescriptorTrace:
    trace = DescriptorTrace()
    original_open, original_close = os.open, os.close

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        trace.opened.append(descriptor)
        trace.active.add(descriptor)
        return descriptor

    def closed(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor in trace.active:
            trace.closed.append(descriptor)
            trace.active.remove(descriptor)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "close", closed)
    return trace


class FailingView:
    def __init__(self, source: BinaryIO, stage: str) -> None:
        self.source, self.stage = source, stage

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.source.close()
        if self.stage == "view_close":
            raise OSError("fault_view_close")

    def fileno(self) -> int:
        return self.source.fileno()

    def read(self, size: int = -1) -> bytes:
        if self.stage == "read":
            raise OSError("fault_read")
        if self.stage == "cancel":
            raise asyncio.CancelledError("fault_cancel")
        return self.source.read(size)


async def test_one_walk_classifies_each_leaf_from_its_open_fd_without_a_second_path_stat(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    reads = observe_payloads(shared_host, monkeypatch)

    actual = package_fingerprint(root, slot.package_limits.model_dump())

    assert actual == {
        "root": slot.package.root,
        "digest": slot.package.digest,
        "files": slot.package.files,
        "size": slot.package.size,
    }
    expected_opens = {str(leaf): int(leaf.is_relative_to(root)) for leaf in reads.leaves}
    assert {str(leaf): len(value.opened) for leaf, value in reads.leaves.items()} == expected_opens
    for leaf, value in reads.leaves.items():
        selected = leaf.is_relative_to(root)
        assert value.fd_stats >= (2 if selected else 0)
        assert selected or value.fd_stats == 0
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in value.opened)
    assert {str(leaf): value.bytes for leaf, value in reads.leaves.items()} == {
        str(leaf): size if leaf.is_relative_to(root) else 0 for leaf, size in reads.sizes.items()
    }
    assert {str(directory): count for directory, count in reads.scans.items()} == {
        str(directory): int(directory.is_relative_to(root)) for directory in reads.scans
    }
    # This comes last: the old walk passes the real I/O and traversal gates
    # above, then fails with two path stats for each selected ordinary leaf.
    assert {str(leaf): value.path_stats for leaf, value in reads.leaves.items()} == expected_opens


async def test_replacing_an_opened_package_directory_still_rejects_the_walk(
    shared_host: fixtures.Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    nested = root / "batch_payload/nested"
    parent = nested.parent.stat()
    before = nested.stat()
    replacement = tmp_path / "replacement-nested"
    replacement.mkdir()
    (replacement / "second.bin").write_bytes((nested / "second.bin").read_bytes())
    moved = tmp_path / "opened-nested"
    original_open = os.open
    replaced = False

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        descriptor = original_open(path, flags, *args, **kwargs)
        try:
            parent_fd = kwargs.get("dir_fd")
            if not replaced and path == nested.name and type(parent_fd) is int:
                current_parent = os.fstat(parent_fd)
                if (current_parent.st_dev, current_parent.st_ino) == (parent.st_dev, parent.st_ino):
                    current = os.fstat(descriptor)
                    assert stat.S_ISDIR(current.st_mode)
                    assert (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino)
                    replaced = True
                    nested.rename(moved)
                    replacement.rename(nested)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(LaunchError, match="^managed_(?:file|directory)_changed$"):
        package_fingerprint(root, slot.package_limits.model_dump())

    assert replaced
    assert moved.stat().st_ino == before.st_ino
    assert nested.stat().st_ino != before.st_ino


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
async def test_nonregular_package_leaves_are_rejected_before_reading_bytes(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    leaf = root / "batch_payload/first.bin"
    leaf.unlink()
    if kind == "symlink":
        leaf.symlink_to(root / "batch_payload/nested/second.bin")
    else:
        os.mkfifo(leaf, mode=0o600)
    reads = observe_payloads(shared_host, monkeypatch)

    with pytest.raises((LaunchError, OSError)) as rejected:
        package_fingerprint(root, slot.package_limits.model_dump())

    error = rejected.value
    if isinstance(error, LaunchError):
        assert str(error) == "managed_file_unsupported"
    else:
        assert error.errno == errno.ELOOP
    observed = reads.leaves[leaf]
    assert observed.bytes == 0
    assert len(observed.opened) == int(kind == "fifo")
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)


@pytest.mark.parametrize("api", ["package", "standalone"])
@pytest.mark.parametrize(
    "stage", ["classify", "fdopen", "read", "cancel", "after_read", "path", "view_close"]
)
async def test_the_descriptor_owner_closes_once_after_each_fingerprint_failure(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, api: str, stage: str
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    leaf = root / "batch_payload/first.bin"
    expected = leaf.stat()
    parent = leaf.parent.stat()
    original_fstat, original_fdopen, original_stat = os.fstat, os.fdopen, os.stat
    observations = 0
    failed: list[str] = []

    def is_leaf(descriptor: int) -> bool:
        current = original_fstat(descriptor)
        return (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)

    def fstat(descriptor: int) -> os.stat_result:
        nonlocal observations
        current = original_fstat(descriptor)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            observations += 1
            if (stage == "classify" and observations == 1) or (
                stage == "after_read" and observations == 2
            ):
                failed.append(stage)
                raise OSError(f"fault_{stage}")
        return current

    def fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        if is_leaf(descriptor):
            assert kwargs.get("closefd") is False
            if stage == "fdopen":
                failed.append(stage)
                raise OSError("fault_fdopen")
            source = original_fdopen(descriptor, *args, **kwargs)
            if stage in {"read", "cancel", "view_close"}:
                failed.append(stage)
                return FailingView(source, stage)
            return source
        return original_fdopen(descriptor, *args, **kwargs)

    def stat_path(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        current = original_stat(path, *args, **kwargs)
        parent_fd = kwargs.get("dir_fd")
        if stage == "path" and path == leaf.name and type(parent_fd) is int:
            current_parent = original_fstat(parent_fd)
            if (current_parent.st_dev, current_parent.st_ino) == (parent.st_dev, parent.st_ino):
                failed.append(stage)
                raise OSError("fault_path")
        return current

    with monkeypatch.context() as scoped:
        trace = observe_descriptors(scoped)
        scoped.setattr(os, "fstat", fstat)
        scoped.setattr(os, "fdopen", fdopen)
        scoped.setattr(os, "stat", stat_path)
        exception = asyncio.CancelledError if stage == "cancel" else OSError
        with pytest.raises(exception, match=f"^fault_{stage}$"):
            if api == "package":
                package_fingerprint(root, slot.package_limits.model_dump())
            else:
                fingerprint(leaf, slot.package_limits.file_bytes)
        assert failed == [stage]
        assert trace.opened
        assert not trace.active
        assert sorted(trace.closed) == sorted(trace.opened)


@pytest.mark.parametrize("api", ["package", "standalone"])
async def test_success_closes_the_caller_owned_descriptor_once(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, api: str
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    with monkeypatch.context() as scoped:
        trace = observe_descriptors(scoped)
        if api == "package":
            actual = package_fingerprint(root, slot.package_limits.model_dump())
            assert actual["digest"] == slot.package.digest
        else:
            actual = fingerprint(root / "batch_payload/first.bin", slot.package_limits.file_bytes)
            assert actual["path"] == str(root / "batch_payload/first.bin")
        assert trace.opened
        assert not trace.active
        assert sorted(trace.closed) == sorted(trace.opened)


@pytest.mark.parametrize("changed", ["inode", "rewrite"])
async def test_the_final_leaf_path_check_rejects_changes_after_the_fd_check(
    shared_host: fixtures.Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    leaf = root / "batch_payload/first.bin"
    contents, before = leaf.read_bytes(), leaf.stat()
    replacement = tmp_path / "replacement-leaf"
    replacement.write_bytes(contents)
    replacement.chmod(stat.S_IMODE(before.st_mode))
    original_fstat = os.fstat
    observations = 0
    mutated = False

    def fstat(descriptor: int) -> os.stat_result:
        nonlocal observations, mutated
        current = original_fstat(descriptor)
        if (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino):
            observations += 1
            if observations == 2:
                mutated = True
                if changed == "inode":
                    replacement.replace(leaf)
                else:
                    with leaf.open("r+b") as output:
                        output.write(bytes([contents[0] ^ 0xFF]) + contents[1:])
                    os.utime(leaf, ns=(before.st_atime_ns, before.st_mtime_ns))
        return current

    with monkeypatch.context() as scoped:
        trace = observe_descriptors(scoped)
        scoped.setattr(os, "fstat", fstat)
        with pytest.raises(LaunchError, match="^managed_file_changed$"):
            package_fingerprint(root, slot.package_limits.model_dump())
        assert mutated
        assert not trace.active
        assert sorted(trace.closed) == sorted(trace.opened)
    after = leaf.stat()
    if changed == "inode":
        assert after.st_ino != before.st_ino
    else:
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        assert after.st_ctime_ns != before.st_ctime_ns


async def test_the_final_directory_path_check_rejects_replacement_after_recursion(
    shared_host: fixtures.Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    nested = root / "batch_payload/nested"
    before = nested.stat()
    replacement = tmp_path / "replacement-after-recursion"
    replacement.mkdir()
    (replacement / "second.bin").write_bytes((nested / "second.bin").read_bytes())
    moved = tmp_path / "recursed-nested"
    original_fstat = os.fstat
    observations = 0
    replaced = False

    def fstat(descriptor: int) -> os.stat_result:
        nonlocal observations, replaced
        current = original_fstat(descriptor)
        if (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino):
            observations += 1
            if observations == 2:
                replaced = True
                nested.rename(moved)
                replacement.rename(nested)
        return current

    with monkeypatch.context() as scoped:
        trace = observe_descriptors(scoped)
        scoped.setattr(os, "fstat", fstat)
        with pytest.raises(LaunchError, match="^managed_file_changed$"):
            package_fingerprint(root, slot.package_limits.model_dump())
        assert replaced
        assert not trace.active
        assert sorted(trace.closed) == sorted(trace.opened)
    assert moved.stat().st_ino == before.st_ino
    assert nested.stat().st_ino != before.st_ino


@pytest.mark.parametrize("stage", ["scandir", "path"])
async def test_a_recursive_directory_failure_closes_every_owned_descriptor(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    nested = root / "batch_payload/nested"
    before, parent = nested.stat(), nested.parent.stat()
    original_fstat, original_stat, original_scandir = os.fstat, os.stat, os.scandir
    failed: list[str] = []

    def scandir(path: Any = ".") -> Any:
        if stage == "scandir" and type(path) is int:
            current = original_fstat(path)
            if (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino):
                failed.append(stage)
                raise OSError("fault_directory_scandir")
        return original_scandir(path)

    def stat_path(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        current = original_stat(path, *args, **kwargs)
        parent_fd = kwargs.get("dir_fd")
        if stage == "path" and path == nested.name and type(parent_fd) is int:
            current_parent = original_fstat(parent_fd)
            if (current_parent.st_dev, current_parent.st_ino) == (parent.st_dev, parent.st_ino):
                failed.append(stage)
                raise OSError("fault_directory_path")
        return current

    with monkeypatch.context() as scoped:
        trace = observe_descriptors(scoped)
        scoped.setattr(os, "scandir", scandir)
        scoped.setattr(os, "stat", stat_path)
        with pytest.raises(OSError, match=f"^fault_directory_{stage}$"):
            package_fingerprint(root, slot.package_limits.model_dump())
        assert failed == [stage]
        assert not trace.active
        assert sorted(trace.closed) == sorted(trace.opened)


@pytest.mark.parametrize("initial", ["directory", "file"])
async def test_a_type_change_between_enumeration_and_open_uses_the_actual_fd(
    shared_host: fixtures.Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: str
) -> None:
    slot = shared_host.slots["target"]
    root = Path(slot.package.root)
    node = root / ("batch_payload" if initial == "directory" else "batch_payload/first.bin")
    parent = node.parent.stat()
    moved = tmp_path / "original-type"
    original_open, original_fstat = os.open, os.fstat
    actual_types: list[str] = []

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        parent_fd = kwargs.get("dir_fd")
        changing = False
        if not actual_types and path == node.name and type(parent_fd) is int:
            current_parent = original_fstat(parent_fd)
            changing = (current_parent.st_dev, current_parent.st_ino) == (
                parent.st_dev,
                parent.st_ino,
            )
        if changing:
            node.rename(moved)
            if initial == "directory":
                node.write_bytes(b"now a regular file")
            else:
                node.mkdir()
                (node / "child.bin").write_bytes(b"now a directory")
        descriptor = original_open(path, flags, *args, **kwargs)
        try:
            if changing:
                current = original_fstat(descriptor)
                actual_types.append("directory" if stat.S_ISDIR(current.st_mode) else "file")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    with monkeypatch.context() as scoped:
        trace = observe_descriptors(scoped)
        # Preserve the observer's real open/close delegation around the change.
        original_open = os.open
        scoped.setattr(os, "open", opened)
        with pytest.raises(LaunchError, match="^managed_(?:file|directory)_changed$"):
            package_fingerprint(root, slot.package_limits.model_dump())
        assert actual_types == ["file" if initial == "directory" else "directory"]
        assert not trace.active
        assert sorted(trace.closed) == sorted(trace.opened)


@pytest.mark.parametrize("budget", ["entries", "total_bytes", "file_bytes", "depth"])
async def test_each_budget_rejection_closes_all_open_package_descriptors(
    shared_host: fixtures.Host, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    slot = shared_host.slots["target"]
    limits = {**slot.package_limits.model_dump(), budget: 1}
    expected_code = {
        "entries": "managed_package_entry_budget",
        "total_bytes": "managed_file_unsupported",
        "file_bytes": "managed_file_unsupported",
        "depth": "managed_package_depth_budget",
    }[budget]
    with monkeypatch.context() as scoped:
        trace = observe_descriptors(scoped)
        with pytest.raises(LaunchError, match=f"^{expected_code}$"):
            package_fingerprint(Path(slot.package.root), limits)
        assert trace.opened
        assert not trace.active
        assert sorted(trace.closed) == sorted(trace.opened)
