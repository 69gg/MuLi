"""Reuse an already held Python parent without weakening real file checks."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Self, cast

import pytest

from muli.platforms import managed_launcher as launcher

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained Python paths")

# This is an executable regular file for identity checks, never a process.
_PAYLOAD = b"\x7fELF" + bytes(range(256)) * 257


def _identity(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "modified_ns": info.st_mtime_ns,
        "changed_ns": info.st_ctime_ns,
        "mode": info.st_mode,
        "uid": info.st_uid,
    }


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    binary = tmp_path / "retained/bin/python"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(_PAYLOAD)
    binary.chmod(0o700)
    return binary


def _entry(root: Path, binary: Path, links: int) -> Path:
    if not links:
        return binary
    directory = root / "links"
    directory.mkdir()
    entry = binary
    for index in reversed(range(links)):
        link = directory / f"python-{index}"
        link.symlink_to(os.path.relpath(entry, directory))
        entry = link
    return entry


@dataclass
class _Reads:
    data: bytearray = field(default_factory=bytearray)
    parents: list[Path] = field(default_factory=list)
    opened: list[tuple[int, dict[str, int] | None]] = field(default_factory=list)
    fd_identities: list[dict[str, int]] = field(default_factory=list)
    path_identities: list[dict[str, int]] = field(default_factory=list)


class _ObservedReader:
    def __init__(
        self,
        source: BinaryIO,
        reads: _Reads,
        after_read: Callable[[bytes], None] | None,
    ) -> None:
        self.source, self.reads, self.after_read = source, reads, after_read

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.source.close()

    def fileno(self) -> int:
        return self.source.fileno()

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.reads.data.extend(data)
        if self.after_read is not None:
            self.after_read(data)
        return data


def _observe(
    binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    after_read: Callable[[bytes], None] | None = None,
) -> _Reads:
    expected = binary.stat()
    target = (expected.st_dev, expected.st_ino)
    parent = binary.parent.stat()
    parent_identity = (parent.st_dev, parent.st_ino)
    reads = _Reads()
    original_open, original_fdopen = os.open, os.fdopen
    original_fstat, original_stat = os.fstat, os.stat
    original_parent = launcher._open_directory

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        info = original_fstat(descriptor)
        if (info.st_dev, info.st_ino) == target:
            directory = kwargs.get("dir_fd")
            actual_parent = (
                _identity(original_fstat(directory)) if isinstance(directory, int) else None
            )
            reads.opened.append((flags, actual_parent))
        return descriptor

    def fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original_fdopen(descriptor, *args, **kwargs)
        info = original_fstat(descriptor)
        if (info.st_dev, info.st_ino) == target:
            return _ObservedReader(cast(BinaryIO, source), reads, after_read)
        return source

    def fstat(descriptor: int) -> os.stat_result:
        info = original_fstat(descriptor)
        if (info.st_dev, info.st_ino) == target:
            reads.fd_identities.append(_identity(info))
        return info

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = original_stat(path, *args, **kwargs)
        directory = kwargs.get("dir_fd")
        if (
            path == binary.name
            and isinstance(directory, int)
            and kwargs.get("follow_symlinks") is False
        ):
            current_parent = original_fstat(directory)
            if (current_parent.st_dev, current_parent.st_ino) == parent_identity:
                reads.path_identities.append(_identity(info))
        return info

    def open_parent(path: Path) -> int:
        reads.parents.append(path)
        return original_parent(path)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", fdopen)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(launcher, "_open_directory", open_parent)
    return reads


@pytest.mark.parametrize("links", [0, 3], ids=["regular", "three-links"])
def test_terminal_parent_is_walked_twice_after_full_file_verification(
    tmp_path: Path, executable: Path, monkeypatch: pytest.MonkeyPatch, links: int
) -> None:
    entry = _entry(tmp_path, executable, links)
    expected_identity = _identity(executable.stat())
    expected_parent = _identity(executable.parent.stat())
    reads = _observe(executable, monkeypatch)

    result = launcher.python_fingerprint(entry, len(_PAYLOAD))

    # The performance oracle comes last: omitting bytes, the independent leaf
    # open or either descriptor/path identity checks must not satisfy it.
    assert stat.S_ISREG(expected_identity["mode"]) and expected_identity["mode"] & 0o111
    assert result == {
        "path": str(executable),
        "identity": expected_identity,
        "digest": hashlib.sha256(_PAYLOAD).hexdigest(),
    }
    assert bytes(reads.data) == _PAYLOAD
    assert len(reads.opened) == 1
    flags, parent = reads.opened[0]
    assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK and flags & os.O_CLOEXEC
    assert parent == expected_parent
    assert len(reads.fd_identities) >= 2
    assert all(info == expected_identity for info in reads.fd_identities)
    assert len(reads.path_identities) >= 2
    assert all(info == expected_identity for info in reads.path_identities)
    # Old code enters fingerprint() with a parent already held: four complete
    # ancestor walks. The held fd needs only its own entry and final recheck.
    assert reads.parents.count(executable.parent) == 2


def test_terminal_parent_replacement_after_read_is_rejected(
    tmp_path: Path, executable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = launcher._fingerprint_at
    expected = _identity(executable.stat())
    old_parent = executable.parent.stat()
    replaced: list[bool] = []

    def replace_parent(
        parent: int,
        name: str,
        path: Path,
        limit: int,
        *,
        verified: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        result = original(parent, name, path, limit, verified=verified)
        assert path == executable and result["identity"] == expected
        assert result["digest"] == hashlib.sha256(_PAYLOAD).hexdigest()
        executable.parent.rename(tmp_path / "replaced-parent")
        executable.parent.mkdir()
        executable.write_bytes(_PAYLOAD)
        executable.chmod(0o700)
        replaced.append(True)
        return result

    monkeypatch.setattr(launcher, "_fingerprint_at", replace_parent)
    with pytest.raises(launcher.LaunchError, match="^managed_directory_changed$"):
        launcher.python_fingerprint(executable, len(_PAYLOAD))

    assert replaced == [True]
    current_parent = executable.parent.stat()
    assert (old_parent.st_dev, old_parent.st_ino) != (
        current_parent.st_dev,
        current_parent.st_ino,
    )
    assert executable.read_bytes() == _PAYLOAD


def test_earlier_python_link_retargeted_after_terminal_read_is_rejected(
    tmp_path: Path, executable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path, executable, 3)
    previous = os.readlink(entry)
    alternate = tmp_path / "other-python"
    alternate.write_bytes(_PAYLOAD)
    alternate.chmod(0o700)
    original = launcher._fingerprint_at
    changed: list[bool] = []

    def retarget(
        parent: int,
        name: str,
        path: Path,
        limit: int,
        *,
        verified: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        result = original(parent, name, path, limit, verified=verified)
        assert path == executable
        assert result["digest"] == hashlib.sha256(_PAYLOAD).hexdigest()
        entry.unlink()
        entry.symlink_to(os.path.relpath(alternate, entry.parent))
        changed.append(True)
        return result

    monkeypatch.setattr(launcher, "_fingerprint_at", retarget)
    with pytest.raises(launcher.LaunchError, match="^managed_python_changed$"):
        launcher.python_fingerprint(entry, len(_PAYLOAD))

    assert changed == [True] and os.readlink(entry) != previous


def test_same_inode_rewrite_with_restored_mtime_during_hash_is_rejected(
    executable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = executable.stat()
    changed: list[bool] = []

    def rewrite(data: bytes) -> None:
        if not data or changed:
            return
        with executable.open("r+b") as output:
            output.write(bytes([_PAYLOAD[0] ^ 0xFF]) + _PAYLOAD[1:])
        os.utime(executable, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = executable.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        assert after.st_ctime_ns != before.st_ctime_ns
        changed.append(True)

    reads = _observe(executable, monkeypatch, after_read=rewrite)
    with pytest.raises(launcher.LaunchError, match="^managed_file_changed$"):
        launcher.python_fingerprint(executable, len(_PAYLOAD))

    assert changed == [True] and reads.data
    assert len(reads.opened) == 1


def test_next_independent_call_hashes_the_complete_python_again(
    tmp_path: Path, executable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(tmp_path, executable, 3)
    reads = _observe(executable, monkeypatch)

    first = launcher.python_fingerprint(entry, len(_PAYLOAD))
    assert bytes(reads.data) == _PAYLOAD
    assert len(reads.opened) == 1
    reads.data.clear()
    second = launcher.python_fingerprint(entry, len(_PAYLOAD))

    assert second == first
    assert bytes(reads.data) == _PAYLOAD
    assert len(reads.opened) == 2


def test_verified_python_bytes_do_not_bypass_a_smaller_file_budget(executable: Path) -> None:
    verified = launcher.python_fingerprint(executable, len(_PAYLOAD))

    with pytest.raises(launcher.LaunchError, match="^managed_file_unsupported$"):
        launcher.python_fingerprint(executable, len(_PAYLOAD) - 1, verified=verified)
