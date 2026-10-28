from __future__ import annotations

import errno
import json
import os
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import muli.format_evidence as evidence_module
from muli.format_admission import CURRENT_FORMAT_STATE, FormatAdmissionError
from muli.format_evidence import FormatEvidenceError, read_optional_format_evidence
from muli.store import Store
from muli.store_initialization import CreatedDatabase, StoreInitializationError


@pytest.fixture
def legacy_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "legacy.sqlite3"
    legacy = Store(path)
    try:
        legacy.put("fixture", {"id": "retained", "value": "legacy data"})
    finally:
        legacy.close()
    return path.read_bytes()


def test_same_inode_legacy_content_cannot_receive_the_creation_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_bytes: bytes
) -> None:
    path = tmp_path / "claimed.sqlite3"
    connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def inject_then_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        before = path.stat()
        path.write_bytes(legacy_bytes)
        after = path.stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", inject_then_connect)
        with pytest.raises(StoreInitializationError) as caught:
            Store.open_runtime(path, default_mode="auto").close()
    assert caught.value.code == "store_creation_not_pristine"
    assert path.read_bytes() == legacy_bytes
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")
    with closing(connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        assert read_optional_format_evidence(db) is None
        assert (
            db.execute("SELECT count(*) FROM sqlite_schema WHERE name='memory_state'").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("timing", ["before_claim", "during_connect"])
def test_sidecars_reject_creation_without_deleting_or_initializing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, timing: str
) -> None:
    path = tmp_path / "sidecar.sqlite3"
    sidecar = path.with_name(path.name + suffix)
    content = b"unowned SQLite sidecar"
    connect = sqlite3.connect
    attempts = 0

    def observed_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal attempts
        attempts += 1
        if timing == "during_connect":
            sidecar.write_bytes(content)
        return connect(*args, **kwargs)

    if timing == "before_claim":
        sidecar.write_bytes(content)
    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    with pytest.raises(StoreInitializationError) as caught:
        Store.open_runtime(path, default_mode="auto").close()
    assert caught.value.code == "store_creation_not_pristine"
    assert attempts == (0 if timing == "before_claim" else 1)
    assert path.read_bytes() == b""
    assert sidecar.read_bytes() == content


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_dangling_sidecar_symlinks_are_not_absence(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "link.sqlite3"
    sidecar = path.with_name(path.name + suffix)
    target = tmp_path / "absent-sidecar-target"
    try:
        sidecar.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Creating a real symlink requires Windows symlink permission")
        raise
    assert not sidecar.exists() and sidecar.is_symlink()
    retained_target = sidecar.readlink()
    with pytest.raises(StoreInitializationError) as caught:
        Store.open_runtime(path, default_mode="auto").close()
    assert caught.value.code == "store_creation_not_pristine"
    assert not sidecar.exists()
    assert sidecar.is_symlink() and sidecar.readlink() == retained_target
    assert path.read_bytes() == b""


@pytest.mark.parametrize("operation", ["fd_stat", "main_lstat", "sidecar_lstat"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_pristine_check_errors_have_a_fixed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, error_number: int
) -> None:
    path = tmp_path / "unreadable.sqlite3"
    creation = CreatedDatabase.claim(path)
    assert creation is not None
    fstat, lstat = os.fstat, Path.lstat
    sidecar = path.with_name(path.name + "-wal")

    def checked_fstat(descriptor: int) -> os.stat_result:
        if operation == "fd_stat" and descriptor == creation.fd:
            raise OSError(error_number, "private inspection detail")
        return fstat(descriptor)

    def checked_lstat(current: Path) -> os.stat_result:
        if (operation == "main_lstat" and current == path) or (
            operation == "sidecar_lstat" and current == sidecar
        ):
            raise OSError(error_number, "private inspection detail")
        return lstat(current)

    with creation, monkeypatch.context() as patch:
        patch.setattr(os, "fstat", checked_fstat)
        patch.setattr(Path, "lstat", checked_lstat)
        with pytest.raises(StoreInitializationError) as caught:
            creation.check_pristine()
    assert str(caught.value) == caught.value.code == "store_creation_not_pristine"


def test_pristine_check_rechecks_content_after_scanning_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "changed-during-scan.sqlite3"
    creation = CreatedDatabase.claim(path)
    assert creation is not None
    lstat = Path.lstat
    last_sidecar = path.with_name(path.name + "-journal")
    content = b"changed in the claimed inode"

    def inject_during_scan(current: Path) -> os.stat_result:
        if current == last_sidecar:
            path.write_bytes(content)
        return lstat(current)

    with creation, monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", inject_during_scan)
        with pytest.raises(StoreInitializationError) as caught:
            creation.check_pristine()
    assert caught.value.code == "store_creation_not_pristine"
    assert path.read_bytes() == content


def delay_initial_setup(
    path: Path, monkeypatch: pytest.MonkeyPatch, *, empty_only: bool
) -> list[str]:
    clock = [100.0]
    delayed: list[str] = []
    rows = evidence_module._rows

    def delayed_rows(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        result = rows(db, sql, parameters, limit=limit)
        if (
            sql == "PRAGMA read_uncommitted"
            and not delayed
            and (not empty_only or path.stat().st_size == 0)
        ):
            clock[0] += 1.25
            delayed.append(sql)
        return result

    monkeypatch.setattr(evidence_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(evidence_module, "_rows", delayed_rows)
    return delayed


def test_pristine_runtime_avoids_cold_legacy_setup_and_publishes_complete_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fresh # directory" / "muli.sqlite3"
    delay_initial_setup(path, monkeypatch, empty_only=True)
    store = Store.open_runtime(path, default_mode="auto")
    try:
        assert store.format_state == CURRENT_FORMAT_STATE and not store.migration_allowed
        assert store.main_session()["mode"] == "auto"
        assert store.db.execute("SELECT count(*) FROM memory_state").fetchone()[0] > 0
        assert store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
    finally:
        store.close()
    reopened = Store.open_runtime(path, default_mode="yolo")
    try:
        assert reopened.format_state == CURRENT_FORMAT_STATE and not reopened.migration_allowed
        assert reopened.main_session()["mode"] == "auto"
    finally:
        reopened.close()


@pytest.mark.parametrize("previous", ["empty", "marked"])
def test_existing_files_still_fail_closed_when_initial_setup_exceeds_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: str
) -> None:
    path = tmp_path / "existing.sqlite3"
    if previous == "empty":
        path.touch()
    else:
        Store.open_runtime(path, default_mode="auto").close()
    before = path.read_bytes()
    delayed = delay_initial_setup(path, monkeypatch, empty_only=False)
    with pytest.raises(FormatEvidenceError) as caught:
        Store.open_runtime(path, default_mode="auto").close()
    assert str(caught.value) == caught.value.code == "format_read_timeout"
    assert delayed == ["PRAGMA read_uncommitted"]
    observation = json.loads(caught.value.__notes__[0].split("=", 1)[1])
    assert observation["failure_phase"] == "setup" and observation["queries"] == 0
    assert observation["begin_call"]["returned"] is False
    assert path.read_bytes() == before


def test_untouched_failed_creation_is_not_reissued_on_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "interrupted.sqlite3"

    def fail_initialize(store: Store, *, durable: bool) -> None:
        raise ValueError("injected failure before schema creation")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_initialize", fail_initialize)
        with pytest.raises(ValueError, match="injected failure before schema creation"):
            Store.open_runtime(path, default_mode="auto").close()
    assert path.read_bytes() == b""
    with pytest.raises(FormatAdmissionError) as caught:
        Store.open_runtime(path, default_mode="auto").close()
    assert caught.value.code == "store_format_upgrade_required"
    assert path.read_bytes() == b""
