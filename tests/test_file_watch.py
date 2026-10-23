from __future__ import annotations

import asyncio
import os
import struct
import sys
from pathlib import Path

import pytest

from muli.platforms.file_watch import DirectoryIdentity, FileSignal, NativeFileWatch, WatchError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux inotify adapter")


def fd_count() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


async def settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_create_move_remove_direct_files_and_no_start_scan(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "existing.txt").write_text("old")
    batches: list[list[FileSignal]] = []
    failures: list[str] = []
    baseline = fd_count()
    watch = NativeFileWatch(inbox, batches.append, failures.append)
    try:
        info = inbox.stat()
        assert watch.start() == DirectoryIdentity(info.st_dev, info.st_ino)
        assert not os.get_blocking(watch._fd)
        assert not os.get_inheritable(watch._fd)
        await settle()
        assert not batches
        (inbox / "existing.txt").write_text("edit")
        (inbox / "created.txt").write_text("created")
        (inbox / "subdirectory").mkdir()
        (inbox / "subdirectory" / "nested.txt").write_text("nested")
        moving = tmp_path / "moving.txt"
        moving.write_text("moved")
        moving.rename(inbox / "arrived.txt")
        await settle()
        signals = [signal for batch in batches for signal in batch]
        assert FileSignal("close_write", "existing.txt") in signals
        assert FileSignal("create", "existing.txt") not in signals
        assert FileSignal("create", "created.txt") in signals
        assert FileSignal("close_write", "created.txt") in signals
        assert FileSignal("moved_to", "arrived.txt") in signals
        assert not any(signal.name in {"subdirectory", "nested.txt"} for signal in signals)
        (inbox / "created.txt").unlink()
        (inbox / "arrived.txt").rename(tmp_path / "departed.txt")
        await settle()
        signals = [signal for batch in batches for signal in batch]
        assert FileSignal("removed", "created.txt") in signals
        assert FileSignal("removed", "arrived.txt") in signals
        assert not failures
    finally:
        watch.close()
        watch.close()
    assert fd_count() == baseline


@pytest.mark.asyncio
async def test_metadata_is_direct_regular_stat_without_reading_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "file.txt"
    source.write_text("private body")
    (inbox / "folder").mkdir()
    (inbox / "link").symlink_to(source)
    os.mkfifo(inbox / "pipe")
    watch = NativeFileWatch(inbox, lambda events: None, lambda code: None)
    watch.start()
    real_open = os.open

    def directories_only(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        assert flags & os.O_DIRECTORY
        return real_open(path, flags, mode, dir_fd=dir_fd)

    try:
        with monkeypatch.context() as context:
            context.setattr(os, "open", directories_only)
            metadata = watch.metadata("file.txt")
            assert metadata is not None
            info = source.stat()
            assert (metadata.device, metadata.inode, metadata.size) == (
                info.st_dev,
                info.st_ino,
                info.st_size,
            )
            assert metadata.mtime_ns == info.st_mtime_ns
            assert metadata.ctime_ns == info.st_ctime_ns
            for name in (
                "folder",
                "link",
                "pipe",
                "missing",
                "../file.txt",
                "/file.txt",
                "",
                ".",
                "..",
                "a\0b",
            ):
                assert watch.metadata(name) is None
    finally:
        watch.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["leaf", "ancestor_roundtrip", "symlink"])
async def test_identity_invalidation_precedes_same_batch_file_events(
    tmp_path: Path, replacement: str
) -> None:
    ancestor = tmp_path / "parent"
    inbox = ancestor / "inbox"
    inbox.mkdir(parents=True)
    batches: list[list[FileSignal]] = []
    failures: list[str] = []
    watch = NativeFileWatch(inbox, batches.append, failures.append)
    watch.start()
    (inbox / "before.txt").write_text("queued first")
    if replacement == "ancestor_roundtrip":
        moved = tmp_path / "away"
        ancestor.rename(moved)
        (moved / "inbox" / "during.txt").write_text("detached")
        moved.rename(ancestor)
    else:
        old = ancestor / "old"
        inbox.rename(old)
        if replacement == "symlink":
            inbox.symlink_to(old, target_is_directory=True)
        else:
            inbox.mkdir()
            (inbox / "replacement.txt").write_text("new inode")
    await settle()
    assert not batches
    assert failures == ["identity_changed"]
    assert watch._fd == -1
    watch.close()


@pytest.mark.asyncio
async def test_guard_reads_queued_ancestor_invalidation_before_reader(tmp_path: Path) -> None:
    ancestor = tmp_path / "parent"
    inbox = ancestor / "inbox"
    inbox.mkdir(parents=True)
    batches: list[list[FileSignal]] = []
    failures: list[str] = []
    watch = NativeFileWatch(inbox, batches.append, failures.append)
    original = watch.start()
    away = tmp_path / "away"
    ancestor.rename(away)
    away.rename(ancestor)
    info = inbox.stat()
    assert original == DirectoryIdentity(info.st_dev, info.st_ino)
    with pytest.raises(WatchError, match="identity_changed"):
        watch.check_identity()
    await settle()
    assert failures == ["identity_changed"]
    assert not batches
    assert watch._fd == -1


@pytest.mark.asyncio
async def test_guard_preserves_file_events_and_close_cancels_pending_delivery(
    tmp_path: Path,
) -> None:
    batches: list[list[FileSignal]] = []
    failures: list[str] = []
    watch = NativeFileWatch(tmp_path, batches.append, failures.append)
    baseline = fd_count()
    watch.start()
    (tmp_path / "kept.txt").write_text("kept")
    watch.check_identity()
    await settle()
    assert FileSignal("create", "kept.txt") in [item for batch in batches for item in batch]
    batches.clear()
    (tmp_path / "cancelled.txt").write_text("pending")
    watch.check_identity()
    old_generation = watch._generation
    watch.close()
    watch.start()
    watch._ready(old_generation)
    await settle()
    assert not batches and not failures
    watch.close()
    assert fd_count() == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("read_bytes,event_limit", [(16384, 3), (32, 256)])
async def test_real_event_budgets_stop_once(
    tmp_path: Path, read_bytes: int, event_limit: int
) -> None:
    batches: list[list[FileSignal]] = []
    failures: list[str] = []
    watch = NativeFileWatch(
        tmp_path, batches.append, failures.append, read_bytes=read_bytes, event_limit=event_limit
    )
    baseline = fd_count()
    watch.start()
    for index in range(4):
        (tmp_path / f"file-{index}").write_text("data")
    await settle()
    assert failures == ["coverage_incomplete"]
    assert not batches
    assert fd_count() == baseline
    (tmp_path / "after.txt").write_text("after")
    await settle()
    assert failures == ["coverage_incomplete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [struct.pack("iIII", -1, 0x4000, 0, 0), b"partial"])
async def test_kernel_overflow_and_malformed_read_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes
) -> None:
    failures: list[str] = []
    watch = NativeFileWatch(tmp_path, lambda events: None, failures.append)
    baseline = fd_count()
    watch.start()

    def simulated_read(descriptor: int, limit: int) -> bytes:
        return data

    with monkeypatch.context() as context:
        context.setattr(os, "read", simulated_read)
        watch._ready(watch._generation)
    assert failures == ["coverage_incomplete"]
    assert fd_count() == baseline


@pytest.mark.asyncio
async def test_callback_exceptions_close_descriptors_even_when_failure_callback_raises(
    tmp_path: Path,
) -> None:
    failures: list[str] = []

    def broken_events(events: list[FileSignal]) -> None:
        raise RuntimeError("consumer failed")

    def broken_failure(code: str) -> None:
        failures.append(code)
        raise RuntimeError("failure handler failed")

    baseline = fd_count()
    watch = NativeFileWatch(tmp_path, broken_events, broken_failure)
    watch.start()
    (tmp_path / "new.txt").write_text("new")
    await settle()
    assert failures == ["callback_failed"]
    assert fd_count() == baseline


@pytest.mark.asyncio
async def test_symlink_missing_and_registration_failure_release_every_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    baseline = fd_count()
    for path in (link, tmp_path / "missing", tmp_path / ".." / "target"):
        watch = NativeFileWatch(path, lambda events: None, lambda code: None)
        with pytest.raises(WatchError, match="watch_unavailable"):
            watch.start()
        assert fd_count() == baseline

    def unavailable(*args: object) -> None:
        raise OSError("reader registration failed")

    loop = asyncio.get_running_loop()
    with monkeypatch.context() as context:
        context.setattr(loop, "add_reader", unavailable)
        watch = NativeFileWatch(target, lambda events: None, lambda code: None)
        with pytest.raises(WatchError, match="watch_unavailable"):
            watch.start()
    assert fd_count() == baseline
