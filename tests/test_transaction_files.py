from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from muli.maintenance import MaintenanceError, configuration_digest
from muli.platforms.transaction_files import ConfigurationError, ConfigurationFile

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file protocol")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config-files"
    directory.mkdir()
    return directory


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_replace_exact_bytes_with_existing_nonprivate_parent(config_dir: Path) -> None:
    config_dir.chmod(0o755)
    path = config_dir / "config.json"
    original, candidate = b'{ "value": 1 }\n', b'{"value":2}\n'
    path.write_bytes(original)
    calls: list[None] = []
    with ConfigurationFile(path, 128) as config:
        assert config.read().digest == configuration_digest(path, 128)
        receipt = config.replace(digest(original), candidate, guard=lambda: calls.append(None))
        assert receipt.data == candidate and receipt.digest == digest(candidate)
    assert len(calls) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
    assert list(config_dir.iterdir()) == [path]


@pytest.mark.parametrize("replacement", [b'{"value":1}', b'{ "value": 1 }\n'])
def test_reject_replaced_inode_even_with_same_bytes(config_dir: Path, replacement: bytes) -> None:
    path = config_dir / "config.json"
    original = b'{ "value": 1 }\n'
    path.write_bytes(original)
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            other = config_dir / "manual.json"
            other.write_bytes(replacement)
            other.replace(path)

    with ConfigurationFile(path, 128) as config:
        with pytest.raises(ConfigurationError, match="configuration_changed"):
            config.replace(digest(original), b"{}", guard=guard)
    assert path.read_bytes() == replacement
    assert list(config_dir.iterdir()) == [path]


def test_reject_wrong_digest_without_temporary(config_dir: Path) -> None:
    path = config_dir / "config.json"
    path.write_bytes(b"{}")
    with ConfigurationFile(path, 128) as config:
        with pytest.raises(ConfigurationError, match="configuration_changed"):
            config.replace(digest(b"[]"), b"{}", guard=lambda: None)
    assert list(config_dir.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["file_link", "parent_link", "fifo", "directory"])
def test_nonregular_and_link_paths_are_rejected(config_dir: Path, kind: str) -> None:
    real = config_dir / "real"
    real.mkdir()
    source = real / "config.json"
    source.write_bytes(b"{}")
    target = config_dir / "config.json"
    if kind == "file_link":
        target.symlink_to(source)
    elif kind == "parent_link":
        link = config_dir / "link"
        link.symlink_to(real, target_is_directory=True)
        target = link / source.name
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises((OSError, ConfigurationError)):
        with ConfigurationFile(target, 128) as config:
            config.read()
    assert source.read_bytes() == b"{}"


def test_parent_rename_does_not_write_detached_directory(config_dir: Path) -> None:
    directory = config_dir / "parent"
    directory.mkdir()
    path = directory / "config.json"
    path.write_bytes(b"{}")
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            directory.rename(config_dir / "old")
            directory.mkdir()
            path.write_bytes(b"manual")

    with ConfigurationFile(path, 128) as config:
        with pytest.raises(ConfigurationError, match="configuration_changed"):
            config.replace(digest(b"{}"), b"[]", guard=guard)
    assert path.read_bytes() == b"manual"
    old = config_dir / "old"
    assert (old / path.name).read_bytes() == b"{}"
    assert list(old.iterdir()) == [old / path.name]


@pytest.mark.parametrize("failure_call", [1, 2])
def test_guard_failure_is_not_a_write_receipt(config_dir: Path, failure_call: int) -> None:
    path = config_dir / "config.json"
    path.write_bytes(b"{}")
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise RuntimeError("lease_lost")

    with ConfigurationFile(path, 128) as config:
        with pytest.raises(RuntimeError, match="lease_lost"):
            config.replace(digest(b"{}"), b"[]", guard=guard)
    assert path.read_bytes() == b"{}"
    assert list(config_dir.iterdir()) == [path]


@pytest.mark.parametrize("failure_call", [1, 2])
def test_fsync_failure_never_reports_success(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    path = config_dir / "config.json"
    path.write_bytes(b"{}")
    fsync = os.fsync
    calls = 0

    def fail_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("disk_sync_failed")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    with ConfigurationFile(path, 128) as config:
        with pytest.raises(OSError, match="disk_sync_failed"):
            config.replace(digest(b"{}"), b"[]", guard=lambda: None)
        if failure_call == 2:
            with pytest.raises(ConfigurationError, match="configuration_unconfirmed"):
                config.read()
        else:
            assert config.read().data == b"{}"
    assert path.read_bytes() == (b"{}" if failure_call == 1 else b"[]")
    assert list(config_dir.iterdir()) == [path]


def test_replace_failure_poisoned_and_temp_removed(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_dir / "config.json"
    path.write_bytes(b"{}")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("replace_failed")

    monkeypatch.setattr(os, "replace", fail)
    with ConfigurationFile(path, 128) as config:
        with pytest.raises(OSError, match="replace_failed"):
            config.replace(digest(b"{}"), b"[]", guard=lambda: None)
        with pytest.raises(ConfigurationError, match="configuration_unconfirmed"):
            config.read()
    assert path.read_bytes() == b"{}"
    assert list(config_dir.iterdir()) == [path]


def test_read_and_write_budgets_and_error_compatibility(config_dir: Path) -> None:
    path = config_dir / "config.json"
    path.write_bytes(b"{}")
    with pytest.raises(MaintenanceError, match="configuration_budget"):
        configuration_digest(path, 1)
    with ConfigurationFile(path, 2) as config:
        for data in (b"", b"too long"):
            with pytest.raises(ConfigurationError, match="configuration_budget"):
                config.replace(digest(b"{}"), data, guard=lambda: None)
    assert list(config_dir.iterdir()) == [path]


def test_missing_directory_is_not_created(config_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ConfigurationFile(config_dir / "missing" / "config.json", 128)
    assert not list(config_dir.iterdir())


def test_parent_traversal_rejected(config_dir: Path) -> None:
    with pytest.raises(ConfigurationError, match="configuration_path"):
        ConfigurationFile(config_dir / ".." / "config.json", 128)


def test_reopened_desired_file_needs_successful_adoption(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_dir / "config.json"
    path.write_bytes(b"{}")
    fsync = os.fsync

    def directory_failure(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory_sync_failed")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", directory_failure)
    with ConfigurationFile(path, 128) as config:
        with pytest.raises(OSError, match="directory_sync_failed"):
            config.replace(digest(b"{}"), b"[]", guard=lambda: None)
    with ConfigurationFile(path, 128) as reopened:
        assert reopened.read().digest == digest(b"[]")
        with pytest.raises(OSError, match="directory_sync_failed"):
            reopened.confirm(digest(b"[]"), guard=lambda: None)
        with pytest.raises(ConfigurationError, match="configuration_unconfirmed"):
            reopened.read()
    monkeypatch.setattr(os, "fsync", fsync)
    with ConfigurationFile(path, 128) as confirmed:
        assert confirmed.confirm(digest(b"[]"), guard=lambda: None).data == b"[]"


def test_adoption_does_not_accept_changed_file(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_dir / "config.json"
    path.write_bytes(b"{}")
    fsync = os.fsync

    def mutate(fd: int) -> None:
        fsync(fd)
        path.write_bytes(b"manual")

    monkeypatch.setattr(os, "fsync", mutate)
    with ConfigurationFile(path, 128) as config:
        with pytest.raises(ConfigurationError, match="configuration_changed"):
            config.confirm(digest(b"{}"), guard=lambda: None)
    assert path.read_bytes() == b"manual"
