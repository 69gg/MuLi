from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from muli.store import Store


def test_durable_connection_overrides_normal_before_first_schema_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect
    writes: list[int] = []

    def normal_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")

        def trace(sql: str) -> None:
            if sql.lstrip().startswith(("CREATE", "INSERT", "UPDATE")):
                writes.append(db.execute("PRAGMA synchronous").fetchone()[0])

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr("muli.store.sqlite3.connect", normal_connection)
    store = Store(tmp_path / "durable.db", durable=True)
    try:
        session = store.main_session()
        store.event("test", {"id": "receipt"}, session["id"])
        assert writes and set(writes) == {2}
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        store.close()


def test_existing_callers_keep_selected_connection_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect

    def normal_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    monkeypatch.setattr("muli.store.sqlite3.connect", normal_connection)
    store = Store(tmp_path / "ordinary.db")
    try:
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        store.close()


def test_constructor_failure_closes_open_sqlite_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    databases: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def capture(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        database = connect(*args, **kwargs)
        databases.append(database)
        return database

    def fail(self: Store, *, durable: bool) -> None:
        self.db.execute("CREATE TABLE partial (value)")
        raise ValueError("simulated initialization failure")

    monkeypatch.setattr("muli.store.sqlite3.connect", capture)
    monkeypatch.setattr(Store, "_initialize", fail)
    with pytest.raises(ValueError, match="initialization failure"):
        Store(tmp_path / "partial.db", durable=True)
    assert len(databases) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        databases[0].execute("SELECT 1")


@pytest.mark.parametrize("value", [None, "", "x" * 97, b"binary", "existing-main"])
def test_existing_identity_read_never_creates_or_returns_invalid_marker(
    tmp_path: Path, value: str | bytes | None
) -> None:
    store = Store(tmp_path / "identity.db")
    try:
        if value is not None:
            store.db.execute(
                "INSERT INTO runtime_state(name,value) VALUES('main_session',?)", (value,)
            )
            store.db.commit()
        writes = store.db.total_changes
        assert store.existing_main_session_id() == (
            "existing-main" if value == "existing-main" else None
        )
        assert store.db.total_changes == writes
        assert store.list("session") == []
    finally:
        store.close()
