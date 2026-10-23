"""Bounded Linux directory events, anchored to verified directory descriptors.

This adapter observes metadata only. It does not recurse, poll, read file bodies,
or infer completion from CLOSE_WRITE. Inotify cannot cover remote filesystem
changes or mount-over events; directory checks are not an atomic namespace lock.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import stat
import struct
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from muli.platforms.transaction_files import _open_directory

_LOG = logging.getLogger(__name__)
_CLOSE_WRITE = 0x00000008
_MOVED_FROM = 0x00000040
_MOVED_TO = 0x00000080
_CREATE = 0x00000100
_DELETE = 0x00000200
_DELETE_SELF = 0x00000400
_MOVE_SELF = 0x00000800
_UNMOUNT = 0x00002000
_Q_OVERFLOW = 0x00004000
_IGNORED = 0x00008000
_ONLYDIR = 0x01000000
_MASK_ADD = 0x20000000
_ISDIR = 0x40000000
_SELF_EVENTS = _DELETE_SELF | _MOVE_SELF | _UNMOUNT | _IGNORED
_ENTRY_EVENTS = _MOVED_FROM | _MOVED_TO | _CREATE | _DELETE
_FILE_EVENTS = _ENTRY_EVENTS | _CLOSE_WRITE
_HEADER = struct.Struct("iIII")
_SIGNAL_MASKS: tuple[tuple[int, Literal["create", "close_write", "moved_to", "removed"]], ...] = (
    (_CREATE, "create"),
    (_CLOSE_WRITE, "close_write"),
    (_MOVED_TO, "moved_to"),
    (_MOVED_FROM | _DELETE, "removed"),
)


class WatchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class FileMetadata:
    name: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class FileSignal:
    kind: Literal["create", "close_write", "moved_to", "removed"]
    name: str


@dataclass(frozen=True, slots=True)
class _Event:
    watch: int
    mask: int
    name: str


def _identity(descriptor: int) -> DirectoryIdentity:
    info = os.fstat(descriptor)
    return DirectoryIdentity(info.st_dev, info.st_ino)


def _directory_chain(path: Path) -> list[int]:
    """Keep each NOFOLLOW anchor; never change the recovery writer primitive."""
    descriptors = [_open_directory(Path("/"))]
    try:
        for name in path.parts[1:]:
            descriptors.append(
                os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptors[-1],
                )
            )
        return descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _load_api() -> ctypes.CDLL:
    library = ctypes.CDLL(None, use_errno=True)
    library.inotify_init1.argtypes = [ctypes.c_int]
    library.inotify_init1.restype = ctypes.c_int
    library.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    library.inotify_add_watch.restype = ctypes.c_int
    return library


def _native_result(value: int) -> int:
    if value < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return value


def _valid_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and "/" not in name and "\0" not in name


class NativeFileWatch:
    """Own one inotify instance per start, on the calling asyncio loop thread.

    ``check_identity`` also checks queued invalidations and preserves any file
    events for asynchronous delivery. Failure closes this generation before the
    single failure callback. ``close`` is idempotent and never emits a failure.
    """

    def __init__(
        self,
        path: Path,
        on_events: Callable[[list[FileSignal]], None],
        on_failure: Callable[[str], None],
        *,
        read_bytes: int = 16384,
        event_limit: int = 256,
    ) -> None:
        if (
            type(read_bytes) is not int
            or type(event_limit) is not int
            or read_bytes < _HEADER.size
            or event_limit < 1
        ):
            raise WatchError("watch_budget")
        self.path = path.absolute()
        self._on_events = on_events
        self._on_failure = on_failure
        self._read_bytes = read_bytes
        self._event_limit = event_limit
        self._fd = -1
        self._directories: list[int] = []
        self._identities: list[DirectoryIdentity] = []
        self._entries: dict[int, set[str]] = {}
        self._leaf = -1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduled: asyncio.Handle | None = None
        self._pending: list[_Event] = []
        self._pending_bytes = 0
        self._generation = 0
        self._active = False

    def start(self) -> DirectoryIdentity:
        if self._active:
            raise WatchError("watch_already_started")
        if sys.platform != "linux":
            raise WatchError("platform_unsupported")
        if ".." in self.path.parts:
            raise WatchError("watch_unavailable")
        self._generation += 1
        try:
            self._loop = asyncio.get_running_loop()
            self._directories = _directory_chain(self.path)
            self._identities = [_identity(fd) for fd in self._directories]
            library = _load_api()
            self._fd = _native_result(int(library.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)))
            names = self.path.parts[1:]
            for index, descriptor in enumerate(self._directories):
                mask = _SELF_EVENTS | (_FILE_EVENTS if index == len(names) else _ENTRY_EVENTS)
                # The real path was opened NOFOLLOW. This proc magic-link MUST
                # be followed while its exclusively owned descriptor stays open.
                watch = _native_result(
                    int(
                        library.inotify_add_watch(
                            self._fd,
                            os.fsencode(f"/proc/self/fd/{descriptor}"),
                            mask | _ONLYDIR | _MASK_ADD,
                        )
                    )
                )
                self._entries.setdefault(watch, set())
                if index < len(names):
                    self._entries[watch].add(names[index])
                else:
                    self._leaf = watch
            self._check_path()
            self._capture()
            self._check_path()
            # Successful start defines the observation boundary: no historical
            # directory scan, and no candidate collected during installation.
            self._pending.clear()
            self._pending_bytes = 0
            self._active = True
            self._loop.add_reader(self._fd, self._ready, self._generation)
            return self._identities[-1]
        except WatchError:
            self.close()
            raise
        except (OSError, RuntimeError, AttributeError, ValueError) as error:
            self.close()
            raise WatchError("watch_unavailable") from error
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._active = False
        self._generation += 1
        if self._scheduled is not None:
            self._scheduled.cancel()
            self._scheduled = None
        descriptor, self._fd = self._fd, -1
        try:
            if descriptor >= 0 and self._loop is not None:
                with suppress(RuntimeError, ValueError):
                    self._loop.remove_reader(descriptor)
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            for directory in reversed(self._directories):
                with suppress(OSError):
                    os.close(directory)
            self._directories.clear()
            self._identities.clear()
            self._entries.clear()
            self._pending.clear()
            self._pending_bytes = 0
            self._leaf = -1

    def _check_path(self) -> None:
        try:
            current = _directory_chain(self.path)
            try:
                if [_identity(fd) for fd in current] != self._identities:
                    raise WatchError("identity_changed")
            finally:
                for descriptor in reversed(current):
                    os.close(descriptor)
        except OSError as error:
            raise WatchError("identity_changed") from error

    def check_identity(self) -> None:
        if not self._active:
            raise WatchError("watch_closed")
        try:
            self._capture()
            self._check_path()
        except WatchError as error:
            self._fail(error.code)
            raise
        if self._pending and self._scheduled is None and self._loop is not None:
            self._scheduled = self._loop.call_soon(self._ready, self._generation)

    def metadata(self, name: str) -> FileMetadata | None:
        if not _valid_name(name):
            return None
        self.check_identity()
        try:
            info = os.stat(name, dir_fd=self._directories[-1], follow_symlinks=False)
        except OSError:
            self.check_identity()
            return None
        self.check_identity()
        if not stat.S_ISREG(info.st_mode):
            return None
        return FileMetadata(
            name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        )

    def _capture(self) -> None:
        remaining = self._read_bytes - self._pending_bytes
        if remaining <= 0:
            raise WatchError("coverage_incomplete")
        try:
            data = os.read(self._fd, remaining)
        except BlockingIOError:
            return
        except OSError as error:
            raise WatchError("coverage_incomplete") from error
        if not data or len(data) >= remaining:
            raise WatchError("coverage_incomplete")
        offset = 0
        while offset < len(data):
            if len(data) - offset < _HEADER.size:
                raise WatchError("coverage_incomplete")
            watch, mask, _cookie, size = _HEADER.unpack_from(data, offset)
            end = offset + _HEADER.size + size
            if end > len(data):
                raise WatchError("coverage_incomplete")
            name = os.fsdecode(data[offset + _HEADER.size : end].split(b"\0", 1)[0])
            if mask & _Q_OVERFLOW:
                raise WatchError("coverage_incomplete")
            if watch not in self._entries:
                raise WatchError("coverage_incomplete")
            if mask & _SELF_EVENTS or (name in self._entries[watch] and mask & _ENTRY_EVENTS):
                raise WatchError("identity_changed")
            self._pending.append(_Event(watch, mask, name))
            if len(self._pending) >= self._event_limit:
                raise WatchError("coverage_incomplete")
            offset = end
        self._pending_bytes += len(data)

    def _ready(self, generation: int) -> None:
        if not self._active or generation != self._generation:
            return
        if self._scheduled is not None:
            self._scheduled.cancel()
            self._scheduled = None
        try:
            self._capture()
            self._check_path()
            events, self._pending = self._pending, []
            self._pending_bytes = 0
            signals: list[FileSignal] = []
            for event in events:
                if event.watch != self._leaf or event.mask & _ISDIR or not _valid_name(event.name):
                    continue
                for mask, kind in _SIGNAL_MASKS:
                    if event.mask & mask:
                        signals.append(FileSignal(kind, event.name))
            if signals and self._active and generation == self._generation:
                try:
                    self._on_events(signals)
                except Exception:
                    self._fail("callback_failed", generation)
        except WatchError as error:
            self._fail(error.code, generation)
        except Exception:
            self._fail("callback_failed", generation)
        except BaseException:
            if generation == self._generation:
                self.close()
            raise

    def _fail(self, code: str, generation: int | None = None) -> None:
        if not self._active or (generation is not None and generation != self._generation):
            return
        self.close()
        try:
            self._on_failure(code)
        except Exception:
            _LOG.exception("File watcher failure callback raised")
