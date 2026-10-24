"""One fixture-owned app unit; never a product ownership or deletion API."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot

_RECEIPT = ".muli-native-unit.json"
_PENDING = ".muli-native-unit.pending"
_UNIT = Path("home/.config/systemd/user/muli.service")


def _require(value: bool) -> None:
    if not value:
        raise ValueError("native_unit_birth_unconfirmed")


def _pair(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino]


def _anchors(root: Path, unit: ConfigurationFile) -> tuple[list[int], list[int]]:
    info = root.lstat()
    _require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )
    unit._check()
    return _pair(info), _pair(os.fstat(unit.fd))


def _snapshot(file: ConfigurationFile) -> ConfigurationSnapshot:
    value = file.read()
    info = os.stat(file.path.name, dir_fd=file.fd, follow_symlinks=False)
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        == value.identity
    )
    file._check()
    return value


def _data(root: Path, unit: ConfigurationFile, value: ConfigurationSnapshot) -> bytes:
    root_id, parent_id = _anchors(root, unit)
    return json.dumps(
        {
            "version": 1,
            "root": root_id,
            "parent": parent_id,
            "file": list(value.identity),
            "digest": value.digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _read(receipt: ConfigurationFile) -> tuple[ConfigurationSnapshot, dict[str, Any]]:
    snapshot = _snapshot(receipt)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            _require(key not in value)
            value[key] = item
        return value

    value: Any = json.loads(snapshot.data.decode("utf-8"), object_pairs_hook=unique)
    _require(
        isinstance(value, dict)
        and set(value)
        == {
            "version",
            "root",
            "parent",
            "file",
            "digest",
        }
    )
    _require(type(value["version"]) is int and value["version"] == 1)
    for key, length in (("root", 2), ("parent", 2), ("file", 5)):
        item = value[key]
        _require(
            isinstance(item, list)
            and len(item) == length
            and all(type(part) is int and 0 <= part < 2**64 for part in item)
        )
    _require(
        isinstance(value["digest"], str)
        and re.fullmatch(r"[a-f0-9]{64}", value["digest"]) is not None
    )
    return snapshot, value


def _matches(
    root: Path, unit: ConfigurationFile, value: ConfigurationSnapshot, birth: dict[str, Any]
) -> None:
    root_id, parent_id = _anchors(root, unit)
    _require(
        birth["root"] == root_id
        and birth["parent"] == parent_id
        and birth["file"] == list(value.identity)
        and birth["digest"] == value.digest
    )


def _exclusive(file: ConfigurationFile, data: bytes) -> ConfigurationSnapshot:
    _require(bool(data) and len(data) <= file.limit)
    file._check()
    descriptor = os.open(
        file.path.name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=file.fd,
    )
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.fsync(file.fd)
    installed = _snapshot(file)
    _require(installed.data == data)
    return installed


def _begin(root: Path, limit: int) -> ConfigurationSnapshot:
    with ConfigurationFile(root / _PENDING, limit) as pending:
        return _exclusive(pending, b"pending\n")


def _clear(root: Path, limit: int, expected: ConfigurationSnapshot) -> None:
    with ConfigurationFile(root / _PENDING, limit) as pending:
        _require(_snapshot(pending) == expected)
        os.unlink(pending.path.name, dir_fd=pending.fd)
        os.fsync(pending.fd)


def create(root: Path, data: bytes, *, limit: int) -> ConfigurationSnapshot:
    """Only this O_EXCL creation can start a birth chain."""
    pending = _begin(root, limit)
    with ConfigurationFile(root / _UNIT, limit) as unit:
        value = _exclusive(unit, data)
        with ConfigurationFile(root / _RECEIPT, limit) as receipt:
            _exclusive(receipt, _data(root, unit, value))
        _require(_snapshot(unit) == value)
        _clear(root, limit, pending)
        return value


def install(root: Path, *, limit: int) -> None:
    """Observe genuine successful CAS returns for exactly this fixture unit."""
    _require(not os.path.lexists(root / _PENDING))
    with (
        ConfigurationFile(root / _UNIT, limit) as unit,
        ConfigurationFile(root / _RECEIPT, limit) as receipt,
    ):
        _, birth = _read(receipt)
        _matches(root, unit, _snapshot(unit), birth)
    original = ConfigurationFile.replace

    @wraps(original)
    def replace(
        file: ConfigurationFile, expected_digest: str, data: bytes, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        if file.path != root / _UNIT:
            return original(file, expected_digest, data, guard=guard)
        previous: ConfigurationSnapshot | None = None
        pending: ConfigurationSnapshot | None = None
        try:
            with ConfigurationFile(root / _RECEIPT, limit) as receipt:
                previous, birth = _read(receipt)
            _matches(root, file, _snapshot(file), birth)
            pending = _begin(root, limit)
        except Exception:
            previous = None
        # No observer exception can replace the original result or exception.
        installed = original(file, expected_digest, data, guard=guard)
        if previous is not None and pending is not None:
            try:
                with ConfigurationFile(root / _RECEIPT, limit) as receipt:

                    def current() -> None:
                        _require(_snapshot(receipt) == previous)
                        _require(_snapshot(file) == installed)
                        with ConfigurationFile(root / _PENDING, limit) as marker:
                            _require(_snapshot(marker) == pending)
                        _anchors(root, file)

                    current()
                    original(receipt, previous.digest, _data(root, file, installed), guard=current)
                    _clear(root, limit, pending)
            except Exception:
                pass  # Stale/missing ownership remains unusable; never re-adopt.
        return installed

    ConfigurationFile.replace = replace


def remove(root: Path, *, limit: int, guard: Callable[[], None]) -> None:
    """Caller retains the real stop, cgroup and writer-lease guards."""
    guard()
    _require(not os.path.lexists(root / _PENDING))
    with ConfigurationFile(root / _UNIT, limit) as unit:
        try:
            with ConfigurationFile(root / _RECEIPT, limit) as receipt:
                _, birth = _read(receipt)
        except FileNotFoundError:
            _require(not os.path.lexists(root / _UNIT))
            return
        try:
            value = _snapshot(unit)
        except FileNotFoundError:
            root_id, parent_id = _anchors(root, unit)
            _require(birth["root"] == root_id and birth["parent"] == parent_id)
            return  # A successful unlink may have lost its cleanup response.
        _matches(root, unit, value, birth)
        guard()
        _require(not os.path.lexists(root / _PENDING))
        _require(_snapshot(unit) == value)
        _matches(root, unit, value, birth)
        os.unlink(unit.path.name, dir_fd=unit.fd)
        os.fsync(unit.fd)
