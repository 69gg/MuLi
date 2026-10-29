"""Exact stopped Darwin admission; Linux branch checks are not macOS evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_channel_format_upgrade import legacy_database, snapshot, upgrade
from test_runtime_writer_lease import run_store_script

from muli.channel_format_upgrade import FormatUpgradeCloseError
from muli.compatibility import FormatState
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    LEGACY_FORMAT_STATE,
    PRE_MEMORY_WATCH_FORMAT_STATE,
    PRE_TRANSCRIPT_MEMORY_FORMAT_STATE,
    PRE_WEBPAGE_MEMORY_FORMAT_STATE,
    PREVIOUS_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.managed_boot import _DatabaseAnchor
from muli.store import Store
from muli.store_initialization import StoreConstructionLock, StoreInitializationError

POSIX = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="POSIX stopped upgrade")


def darwin_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Linux branch check is useful fault injection, not native macOS evidence.
    if sys.platform != "darwin":
        monkeypatch.setattr(sys, "platform", "darwin")


def forbid_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    raise AssertionError("Refusal must happen before opening a SQLite connection")


@POSIX
def test_darwin_exact_memory2_upgrade_preserves_rows_and_store_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path, omit_marker=True)
    with closing(sqlite3.connect(path)) as db:
        original = read_format_evidence(db)
    # The same positive case executes natively, without a platform override, on macOS CI.
    if sys.platform != "darwin":
        monkeypatch.setattr(sys, "platform", "darwin")
    assert upgrade(path.parent) is True
    assert snapshot(path, omit_marker=True) == before
    with closing(sqlite3.connect(path)) as db:
        current = read_format_evidence(db)
        assert current.state == CURRENT_FORMAT_STATE
        assert (current.store_instance, current.main_session) == (
            original.store_instance,
            original.main_session,
        )
    assert upgrade(path.parent) is False
    assert snapshot(path, omit_marker=True) == before


@POSIX
@pytest.mark.parametrize("replacement", [False, True], ids=["moved", "replaced"])
def test_existing_parent_lock_never_recreates_or_writes_a_replacement_directory(
    tmp_path: Path, replacement: bool
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    with closing(_DatabaseAnchor(path)) as anchor:
        assert anchor.parent is not None
        construction = StoreConstructionLock(path, existing_parent=anchor.parent)
        assert construction.database == path
        moved = path.parent.with_name("moved-data")
        path.parent.rename(moved)
        if replacement:
            path.parent.mkdir()
        try:
            with pytest.raises((OSError, ValueError)):
                with construction:
                    pytest.fail("An unavailable or replaced anchored parent must be refused")
        finally:
            construction.__exit__(None, None, None)
        assert anchor.parent is not None  # The lock borrows the descriptor; the anchor owns it.
        assert os.fstat(anchor.parent).st_ino == moved.stat().st_ino
        if replacement:
            assert list(path.parent.iterdir()) == []
        else:
            assert not path.parent.exists()
        assert snapshot(moved / path.name) == before


@pytest.mark.skipif(sys.platform != "linux", reason="Original Linux predecessor contracts")
@pytest.mark.parametrize(
    "state",
    [
        LEGACY_FORMAT_STATE,
        PREVIOUS_FORMAT_STATE,
        PRE_MEMORY_WATCH_FORMAT_STATE,
        PRE_TRANSCRIPT_MEMORY_FORMAT_STATE,
        PRE_WEBPAGE_MEMORY_FORMAT_STATE,
    ],
    ids=["legacy", "channel2", "channel3", "memory1", "memory2"],
)
def test_linux_preserves_all_five_exact_predecessors(tmp_path: Path, state: FormatState) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=state)
    before = snapshot(path, omit_marker=True)
    assert upgrade(path.parent) is True
    assert upgrade(path.parent) is False
    assert snapshot(path, omit_marker=True) == before


@POSIX
@pytest.mark.parametrize(
    "source",
    ["legacy", "channel2", "channel3", "memory1", "mixed", "unknown", "unmarked", "metadata2"],
)
def test_darwin_refuses_every_other_source_without_changing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    states = {
        "legacy": LEGACY_FORMAT_STATE,
        "channel2": PREVIOUS_FORMAT_STATE,
        "channel3": PRE_MEMORY_WATCH_FORMAT_STATE,
        "memory1": PRE_TRANSCRIPT_MEMORY_FORMAT_STATE,
    }
    state = states.get(source, PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    path = legacy_database(tmp_path.resolve() / "data", state=state)
    if source in {"mixed", "unknown", "unmarked", "metadata2"}:
        with closing(sqlite3.connect(path)) as db:
            if source == "unmarked":
                db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
            else:
                value = state.model_dump(mode="json")
                if source == "metadata2":
                    value["metadata_version"] = 2
                else:
                    value["formats"]["objects" if source == "mixed" else "memory"] = 9
                db.execute(
                    "UPDATE runtime_state SET value=? WHERE name=?",
                    (json.dumps(value), FORMAT_STATE_KEY),
                )
            db.commit()
    before = snapshot(path)
    darwin_branch(monkeypatch)
    with pytest.raises(ValueError):
        upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755])
def test_darwin_accepts_private_owner_writable_directory_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    path.parent.chmod(mode)
    darwin_branch(monkeypatch)
    assert upgrade(path.parent)
    assert path.parent.stat().st_mode & 0o777 == mode
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (path.parent / "management").exists()
    assert not (path.parent / ".muli.sqlite3-writer").exists()


@POSIX
@pytest.mark.parametrize(
    "target,mode",
    [
        ("directory", 0o500),
        ("directory", 0o770),
        ("directory", 0o777),
        ("database", 0o644),
        ("database", 0o660),
        ("database", 0o400),
    ],
)
def test_darwin_refuses_permissions_without_repair_or_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, mode: int
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    changed = path.parent if target == "directory" else path
    changed.chmod(mode)
    try:
        darwin_branch(monkeypatch)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", forbid_connection)
            with pytest.raises(FormatAdmissionError, match="store_format_upgrade_permissions"):
                upgrade(path.parent)
        assert changed.stat().st_mode & 0o777 == mode
    finally:
        changed.chmod(0o700 if target == "directory" else 0o600)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize(
    "trace",
    [
        "empty-journal",
        "journal-file",
        "management-file",
        "management-link",
        "management-unreadable",
        "writer-directory",
        "writer-link",
    ],
)
def test_darwin_refuses_standard_managed_traces_before_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trace: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    management = path.parent / "management"
    if trace in {"empty-journal", "journal-file"}:
        management.mkdir()
        journal = management / "transaction-v1"
        if trace == "empty-journal":
            journal.mkdir()
        else:
            journal.write_bytes(b"")
    elif trace == "management-file":
        management.write_bytes(b"")
    elif trace == "management-link":
        management.symlink_to(tmp_path.resolve(), target_is_directory=True)
    elif trace == "management-unreadable":
        management.mkdir(mode=0o000)
    else:
        writer = path.parent / ".muli.sqlite3-writer"
        if trace == "writer-directory":
            writer.mkdir()
        else:
            writer.symlink_to(tmp_path.resolve(), target_is_directory=True)
    try:
        darwin_branch(monkeypatch)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", forbid_connection)
            with pytest.raises(
                FormatAdmissionError, match="store_format_managed_upgrade_unsupported"
            ):
                upgrade(path.parent)
    finally:
        if trace == "management-unreadable":
            management.chmod(0o700)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize(
    "managed", ["projection", "adopt", "switch", "transaction", "invalid-json"]
)
def test_darwin_refuses_managed_sql_facts_without_changing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    data = (
        {"action": managed}
        if managed != "transaction"
        else {"action": "restart", "transaction": {}}
    )
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO objects(kind,id,data,updated) VALUES(?,?,?,1)",
            (
                "management_projection" if managed == "projection" else "management",
                "transaction-v1" if managed == "projection" else "managed-fixture",
                "invalid" if managed == "invalid-json" else json.dumps(data),
            ),
        )
        db.commit()
    before = snapshot(path)
    darwin_branch(monkeypatch)
    with pytest.raises(FormatAdmissionError, match="store_format_managed_upgrade_unsupported"):
        upgrade(path.parent)
    assert snapshot(path) == before


@POSIX
@pytest.mark.parametrize("replacement", [False, True], ids=["moved", "replaced"])
def test_darwin_lock_entry_race_never_connects_or_rebuilds_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    import muli.channel_format_upgrade as module

    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    moved = path.parent.with_name("original-data")
    borrowed: list[int] = []

    def move_before_lock(
        database: Path, *, existing_parent: int | None = None
    ) -> StoreConstructionLock:
        assert existing_parent is not None
        borrowed.append(existing_parent)
        path.parent.rename(moved)
        if replacement:
            path.parent.mkdir()
        return StoreConstructionLock(database, existing_parent=existing_parent)

    darwin_branch(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(module, "StoreConstructionLock", move_before_lock)
        patch.setattr(sqlite3, "connect", forbid_connection)
        with pytest.raises((OSError, ValueError)):
            upgrade(path.parent)
    assert len(borrowed) == 1
    if replacement:
        assert list(path.parent.iterdir()) == []
    else:
        assert not path.parent.exists()
    assert snapshot(moved / path.name) == before


@POSIX
def test_existing_parent_lock_open_cannot_create_a_lock_in_a_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    moved = path.parent.with_name("original-data")
    check_parent = StoreConstructionLock._check_parent
    checks = 0

    def replace_after_check(lock: StoreConstructionLock) -> None:
        nonlocal checks
        check_parent(lock)
        checks += 1
        if checks == 1:
            path.parent.rename(moved)
            path.parent.mkdir()

    with closing(_DatabaseAnchor(path)) as anchor:
        assert anchor.parent is not None
        with monkeypatch.context() as patch:
            patch.setattr(StoreConstructionLock, "_check_parent", replace_after_check)
            with pytest.raises(
                StoreInitializationError, match="store_initialization_parent_changed"
            ):
                with StoreConstructionLock(path, existing_parent=anchor.parent):
                    pytest.fail("The second identity check must reject the replaced parent")
        assert list(path.parent.iterdir()) == []
        assert snapshot(moved / path.name) == before


@POSIX
def test_darwin_database_replacement_after_anchoring_is_refused_before_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.channel_format_upgrade as module

    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    original = path.with_name("original.sqlite3")

    def replace_before_lock(
        database: Path, *, existing_parent: int | None = None
    ) -> StoreConstructionLock:
        assert existing_parent is not None
        path.rename(original)
        path.write_bytes(b"replacement must not be connected")
        path.chmod(0o600)
        return StoreConstructionLock(database, existing_parent=existing_parent)

    darwin_branch(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(module, "StoreConstructionLock", replace_before_lock)
        patch.setattr(sqlite3, "connect", forbid_connection)
        with pytest.raises(ValueError, match="managed_database_changed"):
            upgrade(path.parent)
    assert path.read_bytes() == b"replacement must not be connected"
    assert snapshot(original) == before


@POSIX
@pytest.mark.parametrize("when", ["before-update", "after-update"])
@pytest.mark.parametrize("managed", ["sql", "filesystem"])
def test_darwin_rechecks_new_managed_facts_inside_the_marker_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str, managed: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    before = snapshot(path)
    connect = sqlite3.connect
    injected: list[str] = []

    class ManagedDuringWrite(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if (when == "before-update" and sql == "BEGIN IMMEDIATE") or (
                when == "after-update" and sql.startswith("UPDATE runtime_state")
            ):
                injected.append(managed)
                if managed == "sql":
                    super().execute(
                        "INSERT INTO objects(kind,id,data,updated) "
                        "VALUES('management','late-adopt','{\"action\":\"adopt\"}',1)"
                    )
                else:
                    (path.parent / ".muli.sqlite3-writer").mkdir()
            return cursor

    def guarded(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(*args, **kwargs, factory=ManagedDuringWrite)

    darwin_branch(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", guarded)
        with pytest.raises(FormatAdmissionError, match="store_format_managed_upgrade_unsupported"):
            upgrade(path.parent)
    assert injected == [managed]
    assert snapshot(path) == before
    if managed == "filesystem":
        assert (path.parent / ".muli.sqlite3-writer").is_dir()


@POSIX
def test_darwin_missing_nofollow_capability_refuses_before_path_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    darwin_branch(monkeypatch)
    data_dir = tmp_path.resolve() / "not-created"
    with monkeypatch.context() as patch:
        patch.delattr(os, "O_NOFOLLOW")
        patch.setattr(sqlite3, "connect", forbid_connection)
        with pytest.raises(FormatAdmissionError, match="store_format_upgrade_platform_unsupported"):
            upgrade(data_dir)
    assert not data_dir.exists()


@POSIX
@pytest.mark.parametrize(
    "change", ["missing-db", "db-symlink", "ancestor-symlink", "hardlink", "dotdot"]
)
def test_darwin_refuses_missing_or_aliased_database_paths_before_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    original = path
    before = snapshot(path)
    data_dir = path.parent
    if change == "missing-db":
        original = path.with_name("original.sqlite3")
        path.rename(original)
    elif change == "db-symlink":
        original = path.with_name("original.sqlite3")
        path.rename(original)
        path.symlink_to(original)
    elif change == "ancestor-symlink":
        data_dir = path.parent.with_name("alias-data")
        data_dir.symlink_to(path.parent, target_is_directory=True)
    elif change == "hardlink":
        os.link(path, path.with_name("second.sqlite3"))
    else:
        data_dir = path.parent / ".." / path.parent.name
    darwin_branch(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", forbid_connection)
        with pytest.raises((OSError, ValueError)):
            upgrade(data_dir)
    assert snapshot(original) == before


@POSIX
def test_darwin_missing_directory_and_explicit_managed_root_have_no_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    darwin_branch(monkeypatch)
    data_dir = tmp_path.resolve() / "not-created"
    empty = tmp_path.resolve() / "existing-empty"
    empty.mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", forbid_connection)
        with pytest.raises(OSError):
            upgrade(data_dir)
        with pytest.raises(OSError):
            upgrade(empty)
        with pytest.raises(FormatAdmissionError, match="store_format_managed_upgrade_unsupported"):
            upgrade(data_dir, managed_root=tmp_path / "also-not-created")
    assert not data_dir.exists()
    assert list(empty.iterdir()) == []
    assert not (tmp_path / "also-not-created").exists()


@POSIX
def test_existing_parent_lock_is_noninheritable_fixed_and_borrowed(tmp_path: Path) -> None:
    path = legacy_database(tmp_path.resolve() / "data", state=PRE_WEBPAGE_MEMORY_FORMAT_STATE)
    with closing(_DatabaseAnchor(path)) as anchor:
        assert anchor.parent is not None
        parent = anchor.parent
        with StoreConstructionLock(path, existing_parent=parent) as first:
            assert first.fd is not None
            identity = os.fstat(first.fd).st_ino
            assert not os.get_inheritable(first.fd)
            with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
                with StoreConstructionLock(path):
                    pass
        assert os.fstat(parent).st_ino == path.parent.stat().st_ino
        with StoreConstructionLock(path, existing_parent=parent) as second:
            assert second.fd is not None and os.fstat(second.fd).st_ino == identity
            second.path.rename(second.path.with_suffix(".replaced"))
            second.path.write_bytes(b"")
            with pytest.raises(StoreInitializationError, match="store_initialization_lock_changed"):
                second.check()


@POSIX
@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("outcome", ["upgraded", "current", "before-commit", "after-commit"])
def test_close_failure_retains_exclusion_and_retry_never_repeats_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, outcome: str
) -> None:
    if platform == "linux" and sys.platform != "linux":
        pytest.skip("The journal path remains Linux-only")
    state = CURRENT_FORMAT_STATE if outcome == "current" else PRE_WEBPAGE_MEMORY_FORMAT_STATE
    path = legacy_database(tmp_path.resolve() / "data", state=state)
    before = snapshot(path, omit_marker=True)
    connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []
    updates: list[str] = []
    refuse_close = True

    class RefusingClose(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith("UPDATE runtime_state"):
                updates.append(sql)
            cursor = super().execute(sql, parameters)
            if outcome == "after-commit" and sql == "COMMIT":
                raise RuntimeError("injected failure after the real COMMIT")
            return cursor

        def close(self) -> None:
            if refuse_close:
                raise sqlite3.OperationalError("injected close refusal")
            super().close()

    def authorize(action: int, first: str | None, second: str | None, *rest: Any) -> int:
        if (
            outcome == "before-commit"
            and action == sqlite3.SQLITE_TRANSACTION
            and first == "COMMIT"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def refusing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs, factory=RefusingClose)
        connection.set_authorizer(authorize)
        connections.append(connection)
        return connection

    if platform == "darwin":
        darwin_branch(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", refusing_connect)
        with pytest.raises(FormatUpgradeCloseError) as failed:
            upgrade(path.parent)
    failure = failed.value
    assert failure.code == "store_format_upgrade_close_failed"
    assert isinstance(failure.__cause__, sqlite3.OperationalError)
    assert (failure.operation_error is None) == (outcome in {"upgraded", "current"})
    if outcome == "before-commit":
        assert isinstance(failure.operation_error, sqlite3.DatabaseError)
    elif outcome == "after-commit":
        assert isinstance(failure.operation_error, RuntimeError)
    assert len(connections) == 1
    db = connections[0]
    lock = path.with_name(".muli.sqlite3-initialization.lock")
    identity = lock.stat().st_ino
    try:
        assert db.execute("SELECT 1").fetchone() == (1,)
        db.execute("UPDATE runtime_state SET value=value WHERE name=?", (FORMAT_STATE_KEY,))
        db.rollback()
        assert len(updates) == (0 if outcome == "current" else 1) + 1
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            with StoreConstructionLock(path):
                pass
        if platform == "darwin" and outcome == "upgraded":
            result = run_store_script(
                path,
                """
import sys
from pathlib import Path
from muli.store_initialization import StoreConstructionLock, StoreInitializationError
try:
    with StoreConstructionLock(Path(sys.argv[1])):
        raise AssertionError("Failed close must retain the original fixed lock")
except StoreInitializationError as error:
    print(error.code)
""",
            )
            assert result.stdout.strip() == "store_initialization_busy"
        before_retry = len(updates)
        with pytest.raises(FormatUpgradeCloseError) as failed_again:
            failure.retry_close()
        assert failed_again.value.operation_error is failure.operation_error
        assert len(updates) == before_retry
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            with StoreConstructionLock(path):
                pass
    finally:
        refuse_close = False
        failure.retry_close()
    assert len(updates) == before_retry
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    with StoreConstructionLock(path) as released:
        assert released.fd is not None and os.fstat(released.fd).st_ino == identity
    with closing(connect(path)) as reopened:
        expected = (
            PRE_WEBPAGE_MEMORY_FORMAT_STATE if outcome == "before-commit" else CURRENT_FORMAT_STATE
        )
        assert read_format_evidence(reopened).state == expected
    assert snapshot(path, omit_marker=True) == before


@POSIX
def test_real_runtime_owner_must_close_before_a_child_can_upgrade(tmp_path: Path) -> None:
    path = tmp_path.resolve() / "data" / "muli.sqlite3"
    store = Store.open_runtime(path, default_mode="auto")
    try:
        with store.db:
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (PRE_WEBPAGE_MEMORY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
            )
        before = snapshot(path, omit_marker=True)
        script = """
import json
import sys
from pathlib import Path
from muli.channel_format_upgrade import upgrade_channel_ownership_format
from muli.store_initialization import StoreInitializationError
try:
    print(json.dumps({"upgraded": upgrade_channel_ownership_format(Path(sys.argv[1]).parent)}))
except StoreInitializationError as error:
    print(error.code)
"""
        assert run_store_script(path, script).stdout.strip() == "store_initialization_busy"
        assert snapshot(path, omit_marker=True) == before
    finally:
        store.close()
    assert json.loads(run_store_script(path, script).stdout) == {"upgraded": True}
    assert upgrade(path.parent) is False
    assert snapshot(path, omit_marker=True) == before
