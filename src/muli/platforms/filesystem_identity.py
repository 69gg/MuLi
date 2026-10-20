"""Bounded read-only admission for Linux recovery on local persistent mounts.

Mount IDs come from the already-open directory, rather than guessing which
overlapping mountinfo pathname is visible. This does not lock mount topology or
prove storage hardware honours fsync; the transaction still needs its leases,
directory adoption barriers and independent process deadline.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from muli.platforms.transaction_files import _open_directory

_LOCAL_FILESYSTEMS = frozenset({b"ext4", b"btrfs", b"xfs", b"f2fs"})
_UNSAFE_OPTIONS = frozenset(
    {
        b"nobarrier",
        b"barrier=0",
        b"fsync_mode=nobarrier",
        b"norecovery",
        b"noload",
        b"disable_roll_forward",
        b"nologreplay",
    }
)
_ESCAPES = {b"040": b" ", b"011": b"\t", b"012": b"\n", b"134": b"\\"}


class FilesystemError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FilesystemLimits:
    mountinfo_bytes: int = 524_288
    mount_entries: int = 4096
    fdinfo_bytes: int = 4096
    directories: int = 8
    path_bytes: int = 4096

    def __post_init__(self) -> None:
        for value, minimum, maximum in (
            (self.mountinfo_bytes, 1024, 2_097_152),
            (self.mount_entries, 1, 16_384),
            (self.fdinfo_bytes, 128, 16_384),
            (self.directories, 1, 16),
            (self.path_bytes, 128, 8192),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise FilesystemError("filesystem_invalid_limits")


@dataclass(frozen=True, slots=True)
class _Mount:
    path: Path
    filesystem: bytes
    options: frozenset[bytes]
    super_options: frozenset[bytes]


def _read_bounded(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise FilesystemError("filesystem_observation_not_regular")
        if info.st_size > limit:
            raise FilesystemError("filesystem_observation_budget")
        value = source.read(limit + 1)
    if len(value) > limit:
        raise FilesystemError("filesystem_observation_budget")
    return value


def _number(value: bytes) -> int:
    if not re.fullmatch(rb"[0-9]{1,20}", value) or int(value) >= 2**64:
        raise FilesystemError("filesystem_invalid_observation")
    return int(value)


def _path(path: Path, limit: int) -> Path:
    encoded = os.fsencode(path)
    if (
        not path.is_absolute()
        or path.anchor != "/"
        or ".." in path.parts
        or b"\x00" in encoded
        or len(encoded) > limit
    ):
        raise FilesystemError("filesystem_invalid_path")
    return path


def _unescape(value: bytes, limit: int) -> bytes:
    parts = value.split(b"\\")
    decoded = bytearray(parts[0])
    for part in parts[1:]:
        if part[:3] not in _ESCAPES:
            raise FilesystemError("filesystem_invalid_observation")
        decoded.extend(_ESCAPES[part[:3]])
        decoded.extend(part[3:])
    if len(decoded) > limit:
        raise FilesystemError("filesystem_invalid_path")
    return bytes(decoded)


def _options(value: bytes) -> frozenset[bytes]:
    options = value.split(b",")
    if not all(options):
        raise FilesystemError("filesystem_invalid_observation")
    return frozenset(options)


def _mounts(data: bytes, limits: FilesystemLimits) -> dict[int, _Mount]:
    if not data or not data.endswith(b"\n") or b"\x00" in data:
        raise FilesystemError("filesystem_invalid_observation")
    lines = data.split(b"\n")[:-1]
    if len(lines) > limits.mount_entries:
        raise FilesystemError("filesystem_mount_budget")
    result: dict[int, _Mount] = {}
    for line in lines:
        halves = line.split(b" - ")
        if len(halves) != 2:
            raise FilesystemError("filesystem_invalid_observation")
        head, tail = halves[0].split(b" "), halves[1].split(b" ")
        if len(head) < 6 or len(tail) != 3 or not all((*head, *tail)):
            raise FilesystemError("filesystem_invalid_observation")
        identifier = _number(head[0])
        _number(head[1])
        device = head[2].split(b":")
        if identifier == 0 or identifier in result or len(device) != 2:
            raise FilesystemError("filesystem_invalid_observation")
        for component in device:
            _number(component)
        # The unused filesystem root may be opaque, e.g. nsfs net:[inode].
        _unescape(head[3], limits.path_bytes)
        mountpoint = _path(
            Path(os.fsdecode(_unescape(head[4], limits.path_bytes))), limits.path_bytes
        )
        result[identifier] = _Mount(mountpoint, tail[0], _options(head[5]), _options(tail[2]))
        # Unknown optional tags are intentionally ignored, as the kernel specifies.
    return result


def _mount_id(descriptor: int, proc_root: Path, limit: int) -> int:
    value = _read_bounded(proc_root / "fdinfo" / str(descriptor), limit)
    if not value.endswith(b"\n") or b"\x00" in value:
        raise FilesystemError("filesystem_invalid_observation")
    matches = [
        line.partition(b":")[2].strip()
        for line in value.split(b"\n")
        if line.partition(b":")[0] == b"mnt_id"
    ]
    if len(matches) != 1 or (identifier := _number(matches[0])) == 0:
        raise FilesystemError("filesystem_invalid_observation")
    return identifier


def _supported(mount: _Mount, path: Path) -> None:
    if mount.path != path and mount.path not in path.parents:
        raise FilesystemError("filesystem_mount_mismatch")
    if mount.filesystem not in _LOCAL_FILESYSTEMS:
        raise FilesystemError("filesystem_unsupported")
    for options in (mount.options, mount.super_options):
        if b"rw" not in options or b"ro" in options:
            raise FilesystemError("filesystem_readonly")
        if options & _UNSAFE_OPTIONS or any(
            value == b"checkpoint=disable"
            or value.startswith(b"checkpoint=disable:")
            or (value.startswith(b"rescue=") and b"nologreplay" in value[7:].split(b":"))
            for value in options
        ):
            raise FilesystemError("filesystem_durability_unsupported")


def verify_local_filesystems(
    directories: Sequence[Path],
    *,
    limits: FilesystemLimits | None = None,
    proc_root: Path = Path("/proc/self"),
) -> None:
    """Reject unsupported recovery paths without creating or changing anything.

    Callers supply existing config/DB parents and journal/writer directories.
    Invoke before prepare and when revalidating a helper; normal foreground
    startup does not need this recovery capability check.
    """
    if sys.platform != "linux":
        raise FilesystemError("filesystem_platform_unsupported")
    budget = limits or FilesystemLimits()
    if not 1 <= len(directories) <= budget.directories:
        raise FilesystemError("filesystem_directory_budget")
    paths = tuple(_path(directories[index], budget.path_bytes) for index in range(len(directories)))
    try:
        before = _read_bounded(proc_root / "mountinfo", budget.mountinfo_bytes)
        mounts = _mounts(before, budget)
        with ExitStack() as stack:
            opened: list[tuple[Path, int, int, tuple[int, int]]] = []
            for path in paths:
                descriptor = _open_directory(path)
                stack.callback(os.close, descriptor)
                identifier = _mount_id(descriptor, proc_root, budget.fdinfo_bytes)
                mount = mounts.get(identifier)
                if mount is None:
                    raise FilesystemError("filesystem_mount_mismatch")
                _supported(mount, path)
                info = os.fstat(descriptor)
                opened.append((path, descriptor, identifier, (info.st_dev, info.st_ino)))
            for path, descriptor, identifier, identity in opened:
                current = _open_directory(path)
                try:
                    info = os.fstat(current)
                    if (
                        (info.st_dev, info.st_ino) != identity
                        or _mount_id(current, proc_root, budget.fdinfo_bytes) != identifier
                        or _mount_id(descriptor, proc_root, budget.fdinfo_bytes) != identifier
                    ):
                        raise FilesystemError("filesystem_observation_changed")
                finally:
                    os.close(current)
            if before != _read_bounded(proc_root / "mountinfo", budget.mountinfo_bytes):
                raise FilesystemError("filesystem_observation_changed")
    except OSError:
        raise FilesystemError("filesystem_observation_unavailable") from None
