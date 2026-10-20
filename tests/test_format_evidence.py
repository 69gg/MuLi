from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import muli.format_evidence as evidence_module
from muli.compatibility import FormatState
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatEvidenceError,
    FormatReadLimits,
    read_format_evidence,
)

_DOMAINS = (
    "database",
    "objects",
    "configuration",
    "ingress",
    "outbox",
    "memory",
    "install_profile",
)
_SCHEMA = """
CREATE TABLE runtime_state(name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE objects(kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL,
 PRIMARY KEY(kind,id));
"""


def _state(version: int = 1) -> str:
    # Synthetic IDs only: no released MuLi or existing database gains support.
    return json.dumps({"metadata_version": 1, "formats": dict.fromkeys(_DOMAINS, version)})


def _session(identifier: str = "main", **changes: Any) -> str:
    return json.dumps({"id": identifier, "kind": "main", "principal": "owner", **changes})


def _populate(db: sqlite3.Connection, *, instance: str = "instance", main: str = "main") -> None:
    db.executemany(
        "INSERT INTO runtime_state VALUES(?,?)",
        ((FORMAT_STATE_KEY, _state()), ("store_instance_v1", instance), ("main_session", main)),
    )
    db.execute("INSERT INTO objects VALUES('session',?,?,0)", (main, _session(main)))
    db.commit()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection, sqlite3.Connection]]:
    path = tmp_path / "evidence.sqlite3"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.executescript(_SCHEMA)
    _populate(writer)
    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, autocommit=True)
    try:
        yield path, writer, reader
    finally:
        reader.close()
        writer.close()


def _replace_marker(writer: sqlite3.Connection, value: object) -> None:
    writer.execute("UPDATE runtime_state SET value=? WHERE name=?", (value, FORMAT_STATE_KEY))
    writer.commit()


def _error(db: sqlite3.Connection, code: str, **limits: Any) -> None:
    with pytest.raises(FormatEvidenceError) as caught:
        read_format_evidence(db, limits=FormatReadLimits(**limits))
    assert caught.value.code == code
    assert str(caught.value) == code
    assert not db.in_transaction


def _facts(db: sqlite3.Connection) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    return (
        db.execute("SELECT * FROM runtime_state ORDER BY name").fetchall(),
        db.execute("SELECT * FROM objects ORDER BY kind,id").fetchall(),
    )


def test_reads_latest_committed_wal_without_opening_files(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, writer, reader = database
    _replace_marker(writer, _state(7))
    assert path.with_name(path.name + "-wal").stat().st_size > 0
    before = _facts(writer)

    def no_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("the caller already supplied a connection")

    monkeypatch.setattr(sqlite3, "connect", no_connect)
    result = read_format_evidence(reader)
    assert result.state.formats.database == result.state.formats.outbox == 7
    assert (result.store_instance, result.main_session) == ("instance", "main")
    assert {fact.name for fact in result.schema} >= {"runtime_state", "objects"}
    assert _facts(writer) == before and reader.total_changes == 0
    assert not reader.in_transaction
    assert reader.execute("SELECT 1").fetchone() == (1,)
    assert not hasattr(result, "compatible")
    with pytest.raises(FrozenInstanceError):
        result.main_session = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.state.formats.outbox = 9


def test_writer_commit_between_format_and_identity_does_not_mix_snapshots(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, writer, reader = database
    original = FormatState.from_json
    changed = False

    def parse_then_commit(raw: bytes) -> FormatState:
        nonlocal changed
        result = original(raw)
        if not changed:
            changed = True
            with writer:
                writer.execute(
                    "UPDATE runtime_state SET value=? WHERE name=?", (_state(2), FORMAT_STATE_KEY)
                )
                writer.execute(
                    "UPDATE runtime_state SET value='next-instance' WHERE name='store_instance_v1'"
                )
                writer.execute(
                    "UPDATE runtime_state SET value='next-main' WHERE name='main_session'"
                )
                writer.execute(
                    "INSERT INTO objects VALUES('session','next-main',?,0)",
                    (_session("next-main"),),
                )
        return result

    monkeypatch.setattr(FormatState, "from_json", parse_then_commit)
    old = read_format_evidence(reader)
    assert (old.state.formats.database, old.store_instance, old.main_session) == (
        1,
        "instance",
        "main",
    )
    new = read_format_evidence(reader)
    assert (new.state.formats.database, new.store_instance, new.main_session) == (
        2,
        "next-instance",
        "next-main",
    )


def test_shared_cache_read_uncommitted_cannot_supply_evidence(tmp_path: Path) -> None:
    uri = (tmp_path / "shared.sqlite3").as_uri() + "?cache=shared"
    writer = sqlite3.connect(uri, uri=True)
    reader = sqlite3.connect(uri + "&mode=ro", uri=True, autocommit=True)
    try:
        writer.executescript(_SCHEMA)
        _populate(writer)
        reader.execute("PRAGMA read_uncommitted=1")
        writer.execute(
            "UPDATE runtime_state SET value=? WHERE name=?", (_state(7), FORMAT_STATE_KEY)
        )
        writer.execute(
            "UPDATE runtime_state SET value='dirty-instance' WHERE name='store_instance_v1'"
        )
        assert writer.in_transaction
        try:
            observed = read_format_evidence(reader)
        except FormatEvidenceError as error:
            assert error.code == "format_isolation_unsupported"
        else:
            pytest.fail(
                "accepted uncommitted evidence: "
                f"format={observed.state.formats.database}, instance={observed.store_instance}"
            )
        assert not reader.in_transaction
        assert reader.execute("PRAGMA read_uncommitted").fetchone()[0] == 1
        writer.rollback()
        assert (
            reader.execute(
                "SELECT value FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
            ).fetchone()[0]
            == _state()
        )
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("key", [FORMAT_STATE_KEY, "store_instance_v1", "main_session"])
def test_missing_fixed_source_is_not_synthesized(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], key: str
) -> None:
    _, writer, reader = database
    writer.execute("DELETE FROM runtime_state WHERE name=?", (key,))
    writer.commit()
    before = _facts(writer)
    code = {
        FORMAT_STATE_KEY: "format_marker_missing",
        "store_instance_v1": "format_store_identity_missing",
        "main_session": "format_main_identity_missing",
    }[key]
    _error(reader, code)
    assert _facts(writer) == before and reader.total_changes == 0


def test_legacy_markers_do_not_prove_complete_formats(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection],
) -> None:
    _, writer, reader = database
    writer.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
    writer.executemany(
        "INSERT INTO runtime_state VALUES(?,?)",
        (("message_sequence_v1", "done"), ("alias_evidence_version", "1")),
    )
    writer.commit()
    _error(reader, "format_marker_missing")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not JSON; supplied-secret",
        "[]",
        "{}",
        _state().replace('"metadata_version": 1', '"metadata_version": true'),
        _state().replace('"metadata_version": 1', '"metadata_version": 2'),
        _state().replace('"metadata_version": 1', '"metadata_version": 1, "metadata_version": 1'),
        _state().replace('"database": 1', '"database": 1, "unknown": 1'),
        _state().replace('"outbox": 1, ', ""),
        b"binary marker",
    ],
)
def test_invalid_full_marker_has_fixed_error(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], raw: object
) -> None:
    _, writer, reader = database
    _replace_marker(writer, raw)
    _error(reader, "format_marker_invalid")


def test_marker_budget_checks_bytes_before_json_parsing(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, writer, reader = database
    _replace_marker(writer, " " * 16_385)

    def no_parse(raw: bytes) -> FormatState:
        raise AssertionError("oversized marker must not be parsed")

    monkeypatch.setattr(FormatState, "from_json", no_parse)
    _error(reader, "format_marker_budget")


def test_duplicate_fixed_marker_is_rejected(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "duplicates.sqlite3") as db:
        db.executescript(_SCHEMA.replace("name TEXT PRIMARY KEY", "name TEXT"))
        _populate(db)
        db.execute("INSERT INTO runtime_state VALUES(?,?)", (FORMAT_STATE_KEY, _state()))
        db.commit()
        _error(db, "format_marker_invalid")


@pytest.mark.parametrize("key", ["store_instance_v1", "main_session"])
@pytest.mark.parametrize(
    "value, suffix", [("", "invalid"), (b"binary", "invalid"), ("界" * 33, "budget")]
)
def test_identity_checks_utf8_byte_budget_and_type(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection],
    key: str,
    value: object,
    suffix: str,
) -> None:
    _, writer, reader = database
    writer.execute("UPDATE runtime_state SET value=? WHERE name=?", (value, key))
    writer.commit()
    identity = "store" if key == "store_instance_v1" else "main"
    _error(reader, f"format_{identity}_identity_{suffix}")


def test_identity_allows_exactly_96_utf8_bytes(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "identity.sqlite3") as db:
        db.executescript(_SCHEMA)
        _populate(db, instance="界" * 32, main="本" * 32)
        result = read_format_evidence(db)
        assert result.store_instance == "界" * 32 and result.main_session == "本" * 32


@pytest.mark.parametrize(
    "raw",
    [
        _session("other"),
        _session(kind="conversation"),
        _session(principal="stranger"),
        '{"id":"main","id":"other","kind":"main","principal":"owner"}',
        "private invalid JSON",
        "[]",
        b"not text",
    ],
)
def test_main_object_identity_is_not_weaker_than_projection(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], raw: object
) -> None:
    _, writer, reader = database
    writer.execute("UPDATE objects SET data=?", (raw,))
    writer.commit()
    _error(reader, "format_main_object_invalid")


def test_missing_main_object_is_not_created(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection],
) -> None:
    _, writer, reader = database
    writer.execute("DELETE FROM objects")
    writer.commit()
    _error(reader, "format_main_object_missing")
    assert writer.execute("SELECT count(*) FROM objects").fetchone()[0] == 0


def test_main_object_budget_is_bounded(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection],
) -> None:
    _, writer, reader = database
    writer.execute("UPDATE objects SET data=?", (_session(extra="x" * 256),))
    writer.commit()
    _error(reader, "format_main_object_budget", object_bytes=128)


def test_schema_is_observed_without_implying_compatibility(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection],
) -> None:
    _, writer, reader = database
    before = read_format_evidence(reader)
    writer.execute("CREATE INDEX additional_observation ON objects(updated)")
    writer.commit()
    after = read_format_evidence(reader)
    assert after.state == before.state
    assert after.schema != before.schema
    assert "additional_observation" in {fact.name for fact in after.schema}


def test_view_cannot_impersonate_required_table(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "view.sqlite3") as db:
        db.executescript(
            "CREATE VIEW runtime_state AS SELECT 'name' AS name,'value' AS value;"
            "CREATE TABLE objects(kind TEXT,id TEXT,data TEXT);"
        )
        _error(db, "format_schema_unsupported")


@pytest.mark.parametrize(
    "limits,code",
    [
        ({"queries": 1}, "format_query_budget"),
        ({"vm_steps": 1}, "format_query_budget"),
        ({"schema_rows": 2}, "format_schema_budget"),
        ({"schema_bytes": 16}, "format_schema_budget"),
        ({"schema_total_bytes": 16}, "format_schema_budget"),
        ({"marker_bytes": 16}, "format_marker_budget"),
    ],
)
def test_small_budgets_stop_without_writes_or_active_transaction(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], limits: dict[str, Any], code: str
) -> None:
    _, writer, reader = database
    before = _facts(writer)
    _error(reader, code, **limits)
    assert _facts(writer) == before and reader.total_changes == 0
    # The reader's interrupted progress handler must be gone after failure.
    assert (
        reader.execute(
            "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<20) SELECT sum(x) FROM n"
        ).fetchone()[0]
        == 210
    )


def test_deadline_also_checked_between_fast_statements(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, reader = database
    ticks = iter((0.0, 0.0))
    monkeypatch.setattr(evidence_module, "monotonic", lambda: next(ticks, 2.0))
    _error(reader, "format_read_timeout")


def test_database_lock_is_not_retried_or_used_to_create_state(tmp_path: Path) -> None:
    path = tmp_path / "locked.sqlite3"
    writer = sqlite3.connect(path)
    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, autocommit=True)
    try:
        writer.executescript(_SCHEMA)
        _populate(writer)
        before = _facts(writer)
        writer.execute("BEGIN EXCLUSIVE")
        _error(reader, "format_read_failed", busy_timeout_ms=0)
        assert _facts(writer) == before and reader.total_changes == 0
        writer.rollback()
        assert read_format_evidence(reader).main_session == "main"
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("invalid", [False, True])
def test_connection_limits_pragmas_and_row_factory_are_restored(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], invalid: bool
) -> None:
    _, writer, reader = database
    if invalid:
        _replace_marker(writer, "invalid")
    reader.execute("PRAGMA busy_timeout=47")
    reader.row_factory = sqlite3.Row
    categories = (sqlite3.SQLITE_LIMIT_LENGTH, sqlite3.SQLITE_LIMIT_SQL_LENGTH)
    previous = {category: reader.getlimit(category) for category in categories}
    query_only = reader.execute("PRAGMA query_only").fetchone()[0]
    if invalid:
        _error(reader, "format_marker_invalid")
    else:
        assert read_format_evidence(reader).main_session == "main"
    assert reader.row_factory is sqlite3.Row
    assert reader.execute("PRAGMA query_only").fetchone()[0] == query_only
    assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 47
    assert {category: reader.getlimit(category) for category in categories} == previous


def test_active_transaction_is_rejected_without_ending_it(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection],
) -> None:
    _, _, reader = database
    reader.execute("BEGIN")
    with pytest.raises(FormatEvidenceError, match="^format_transaction_active$"):
        read_format_evidence(reader)
    assert reader.in_transaction
    reader.execute("ROLLBACK")


def test_nonstandard_text_factory_is_rejected_without_reconfiguration(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection],
) -> None:
    _, _, reader = database
    reader.text_factory = bytes
    _error(reader, "format_connection_unsupported")
    assert reader.text_factory is bytes


def test_legacy_transaction_mode_uses_the_same_explicit_snapshot(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "legacy.sqlite3")
    try:
        db.executescript(_SCHEMA)
        _populate(db)
        assert db.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
        assert read_format_evidence(db).store_instance == "instance"
        assert not db.in_transaction
        # query_only is restored even for this writable fixture connection.
        db.execute("INSERT INTO runtime_state VALUES('caller', 'still open')")
        assert db.in_transaction
        db.rollback()
    finally:
        db.close()


def test_cleanup_failure_is_reported_instead_of_success(
    database: tuple[Path, sqlite3.Connection, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, reader = database
    original = evidence_module._rows

    def fail_rollback(
        db: sqlite3.Connection, sql: str, parameters: Any = (), *, limit: int = 1
    ) -> list[tuple[Any, ...]]:
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("private SQLite detail")
        return original(db, sql, parameters, limit=limit)

    monkeypatch.setattr(evidence_module, "_rows", fail_rollback)
    with pytest.raises(FormatEvidenceError, match="^format_cleanup_failed$"):
        read_format_evidence(reader)
    assert reader.in_transaction
    assert reader.execute("PRAGMA query_only").fetchone()[0] == 0
    reader.execute("ROLLBACK")


@pytest.mark.parametrize(
    "changes",
    [{"vm_steps": True}, {"seconds": float("nan")}, {"schema_rows": 129}, {"marker_bytes": 16_385}],
)
def test_limit_values_are_strict_and_capped(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FormatReadLimits(**changes)
