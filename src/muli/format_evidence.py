"""Read bounded format evidence from a caller-owned, dedicated SQLite connection.

No path discovery, database creation, migration or compatibility authorization.
The caller owns provenance and supplies a pristine, exclusively used connection;
unknown old progress callbacks cannot be inspected or restored by sqlite3.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from time import monotonic
from typing import Any

from pydantic import Field

from muli.compatibility import CompatibilityError, FormatState
from muli.management_transaction import StrictRecord, _object

FORMAT_STATE_KEY = "format_state_v1"
_IDENTITY_BYTES = 96
_SCHEMA_NAME_BYTES = 256
_SQL_BYTES = 4096
_PHASES = ("setup", "begin", "optional", "schema", "marker", "identity", "main_object", "cleanup")
_OBSERVATION_BYTES = 1024
_MAX_MILLISECONDS = 2**31 - 1


class FormatEvidenceError(ValueError):
    """Only a fixed code, never supplied SQL, JSON or SQLite exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        self._observation_note: str | None = None
        super().__init__(code)

    def copy_observation_to(self, error: Exception) -> None:
        """Forward only this reader's generated observation, never arbitrary notes."""
        if self._observation_note is not None:
            error.add_note(self._observation_note)


class FormatReadLimits(StrictRecord):
    seconds: float = Field(default=1.0, gt=0, le=5, allow_inf_nan=False)
    vm_steps: int = Field(default=100_000, ge=1, le=1_000_000)
    queries: int = Field(default=8, ge=1, le=16)
    schema_rows: int = Field(default=32, ge=2, le=128)
    marker_bytes: int = Field(default=16_384, ge=1, le=16_384)
    object_bytes: int = Field(default=16_384, ge=1, le=65_536)
    schema_bytes: int = Field(default=16_384, ge=1, le=32_768)
    schema_total_bytes: int = Field(default=65_536, ge=1, le=262_144)
    busy_timeout_ms: int = Field(default=20, ge=0, le=100)


@dataclass(frozen=True, slots=True)
class SchemaFact:
    kind: str
    name: str
    table: str
    sql: str | None


@dataclass(frozen=True, slots=True)
class FormatEvidence:
    state: FormatState
    store_instance: str
    main_session: str
    schema: tuple[SchemaFact, ...]


def _rows(
    db: sqlite3.Connection, sql: str, parameters: Sequence[object] = (), *, limit: int = 1
) -> list[tuple[Any, ...]]:
    with closing(db.cursor()) as cursor:
        cursor.row_factory = None
        cursor.execute(sql, parameters)
        return cursor.fetchmany(limit + 1)


class _Reader:
    def __init__(self, db: sqlite3.Connection, limits: FormatReadLimits) -> None:
        self.db, self.limits = db, limits
        self.started = self.phase_started = monotonic()
        self.deadline = self.started + limits.seconds
        self.phase = self.failure_phase = "setup"
        self.durations = dict.fromkeys(_PHASES, 0.0)
        self.queries = self.steps = 0
        self.interval = min(100, limits.vm_steps)
        self.interrupted: str | None = None

    def move(self, phase: str) -> None:
        now = monotonic()
        self.durations[self.phase] += max(0.0, now - self.phase_started)
        self.phase, self.phase_started = phase, now

    def annotate(self, error: FormatEvidenceError) -> None:
        now = monotonic()
        self.durations[self.phase] += max(0.0, now - self.phase_started)

        def milliseconds(seconds: float) -> int:
            return int(min(_MAX_MILLISECONDS, max(0.0, seconds) * 1000))

        note = "format_read_observation=" + json.dumps(
            {
                "elapsed_ms": milliseconds(now - self.started),
                "phase_ms": {phase: milliseconds(value) for phase, value in self.durations.items()},
                "failure_phase": self.failure_phase,
                "queries": self.queries,
                "vm_steps": self.steps,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(note.encode("utf-8")) <= _OBSERVATION_BYTES:
            error._observation_note = note
            error.add_note(note)

    def check(self) -> None:
        if monotonic() >= self.deadline:
            self.interrupted = "format_read_timeout"
        if self.interrupted is not None:
            raise FormatEvidenceError(self.interrupted)

    def progress(self) -> int:
        self.steps += self.interval
        if monotonic() >= self.deadline:
            self.interrupted = "format_read_timeout"
        elif self.steps >= self.limits.vm_steps:
            self.interrupted = "format_query_budget"
        return int(self.interrupted is not None)

    def rows(
        self,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
        overflow: str = "format_record_invalid",
    ) -> list[tuple[Any, ...]]:
        self.check()
        self.queries += 1
        if self.queries > self.limits.queries:
            raise FormatEvidenceError("format_query_budget")
        rows = _rows(self.db, sql, parameters, limit=limit)
        self.check()
        if len(rows) > limit:
            raise FormatEvidenceError(overflow)
        return rows

    def text_record(self, key: str, maximum: int, *, code: str) -> bytes:
        rows = self.rows(
            "SELECT typeof(value),length(CAST(value AS BLOB)),"
            "CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=? "
            "THEN CAST(value AS BLOB) END FROM main.runtime_state WHERE name=? LIMIT 2",
            (maximum, key),
            overflow=code + "_invalid",
        )
        if not rows:
            raise FormatEvidenceError(code + "_missing")
        kind, length, raw = rows[0]
        if kind != "text" or type(length) is not int or length < 1:
            raise FormatEvidenceError(code + "_invalid")
        if length > maximum:
            raise FormatEvidenceError(code + "_budget")
        if not isinstance(raw, bytes):
            raise FormatEvidenceError(code + "_invalid")
        return raw

    def schema(self) -> tuple[SchemaFact, ...]:
        rows = self.rows(
            "SELECT type,name,tbl_name,typeof(sql),length(CAST(sql AS BLOB)),"
            "CASE WHEN length(CAST(sql AS BLOB))<=? THEN CAST(sql AS BLOB) END "
            "FROM main.sqlite_schema WHERE name IN ('runtime_state','objects') "
            "OR tbl_name IN ('runtime_state','objects') ORDER BY type,name LIMIT ?",
            (self.limits.schema_bytes, self.limits.schema_rows + 1),
            limit=self.limits.schema_rows,
            overflow="format_schema_budget",
        )
        facts: list[SchemaFact] = []
        total = 0
        for kind, name, table, sql_type, size, raw in rows:
            if (
                kind not in {"table", "index", "trigger", "view"}
                or not isinstance(name, str)
                or not isinstance(table, str)
                or not 1 <= len(name.encode("utf-8")) <= _SCHEMA_NAME_BYTES
                or not 1 <= len(table.encode("utf-8")) <= _SCHEMA_NAME_BYTES
            ):
                raise FormatEvidenceError("format_schema_invalid")
            if sql_type == "null" and kind == "index":
                sql = None
            else:
                if sql_type != "text" or type(size) is not int or size < 1:
                    raise FormatEvidenceError("format_schema_invalid")
                if size > self.limits.schema_bytes:
                    raise FormatEvidenceError("format_schema_budget")
                if not isinstance(raw, bytes):
                    raise FormatEvidenceError("format_schema_invalid")
                sql = raw.decode("utf-8")
            total += len(name.encode("utf-8")) + len(table.encode("utf-8"))
            total += len(kind.encode("utf-8")) + (len(raw) if isinstance(raw, bytes) else 0)
            if total > self.limits.schema_total_bytes:
                raise FormatEvidenceError("format_schema_budget")
            facts.append(SchemaFact(kind, name, table, sql))
        tables = {fact.name for fact in facts if fact.kind == "table"}
        if not {"runtime_state", "objects"} <= tables or len({f.name for f in facts}) != len(facts):
            raise FormatEvidenceError("format_schema_unsupported")
        return tuple(facts)

    def read(self) -> FormatEvidence:
        self.move("schema")
        schema = self.schema()
        self.move("marker")
        raw = self.text_record(FORMAT_STATE_KEY, self.limits.marker_bytes, code="format_marker")
        try:
            state = FormatState.from_json(raw)
        except CompatibilityError:
            raise FormatEvidenceError("format_marker_invalid") from None
        self.check()
        self.move("identity")
        instance = self.text_record(
            "store_instance_v1", _IDENTITY_BYTES, code="format_store_identity"
        ).decode("utf-8")
        main = self.text_record(
            "main_session", _IDENTITY_BYTES, code="format_main_identity"
        ).decode("utf-8")
        self.move("main_object")
        rows = self.rows(
            "SELECT typeof(data),length(CAST(data AS BLOB)),"
            "CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CAST(data AS BLOB) END FROM main.objects WHERE kind='session' AND id=? LIMIT 2",
            (self.limits.object_bytes, main),
            overflow="format_main_object_invalid",
        )
        if not rows:
            raise FormatEvidenceError("format_main_object_missing")
        kind, length, raw = rows[0]
        if kind != "text" or type(length) is not int or length < 1:
            raise FormatEvidenceError("format_main_object_invalid")
        if length > self.limits.object_bytes:
            raise FormatEvidenceError("format_main_object_budget")
        try:
            if not isinstance(raw, bytes):
                raise ValueError
            session = _object(raw)
        except (ValueError, RecursionError):
            raise FormatEvidenceError("format_main_object_invalid") from None
        if (
            session.get("id") != main
            or session.get("kind") != "main"
            or session.get("principal") != "owner"
        ):
            raise FormatEvidenceError("format_main_object_invalid")
        self.check()
        return FormatEvidence(state, instance, main, schema)

    def read_optional(self) -> FormatEvidence | None:
        self.move("optional")
        tables = self.rows(
            "SELECT name,type FROM main.sqlite_schema "
            "WHERE name='runtime_state' COLLATE NOCASE LIMIT 2",
            overflow="format_schema_unsupported",
        )
        if not tables:
            return None
        if tables != [("runtime_state", "table")]:
            raise FormatEvidenceError("format_schema_unsupported")
        # SQLite's non-STRICT TEXT affinity still permits BLOB primary keys.
        # Their separate PK range must not masquerade as absent metadata.
        if self.rows(
            "SELECT 1 FROM main.runtime_state WHERE name>=? AND name<? LIMIT 2",
            (b"format_state_", b"format_state`"),
            overflow="format_marker_unknown",
        ):
            raise FormatEvidenceError("format_marker_unknown")
        # A PK range, not a scan of all state values or JSON text. This prefix is
        # reserved: future names and partial/multiple markers must fail closed.
        markers = self.rows(
            "SELECT CASE WHEN typeof(name)='text' "
            "AND length(CAST(name AS BLOB))<=? THEN CAST(name AS BLOB) END "
            "FROM main.runtime_state WHERE name>=? AND name<? ORDER BY name LIMIT 2",
            (_IDENTITY_BYTES, "format_state_", "format_state`"),
            overflow="format_marker_unknown",
        )
        if not markers:
            return None
        if markers != [(FORMAT_STATE_KEY.encode("utf-8"),)]:
            raise FormatEvidenceError("format_marker_unknown")
        return self.read()


def read_format_evidence(
    db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
) -> FormatEvidence:
    """Read one explicit snapshot; the caller must own this dedicated connection.

    The connection must have no transaction, no previous progress handler or
    authorizer, use str decoding and have read_uncommitted=0. It owns progress
    callbacks during the call, clears its own callback and restores readable
    limits/PRAGMAs. It cannot detect unknown old callbacks. It never closes db.
    The caller authenticates the source; query_only does not prove path identity.
    """
    result = _read_evidence(db, limits=limits, optional=False)
    assert result is not None
    return result


def read_optional_format_evidence(
    db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
) -> FormatEvidence | None:
    """The same bounded snapshot, returning None only for absent format metadata.

    A missing runtime_state table is legacy. An unreadable table, unknown marker
    name, or malformed marker is an error, never a reason to initialize it.
    """
    return _read_evidence(db, limits=limits, optional=True)


def _read_evidence(
    db: sqlite3.Connection, *, limits: FormatReadLimits | None, optional: bool
) -> FormatEvidence | None:
    limits = limits or FormatReadLimits()
    reader = _Reader(db, limits)
    try:
        return _read_evidence_inner(db, reader, optional=optional)
    except FormatEvidenceError as error:
        reader.annotate(error)
        raise


def _read_evidence_inner(
    db: sqlite3.Connection, reader: _Reader, *, optional: bool
) -> FormatEvidence | None:
    limits = reader.limits
    saved_limits: dict[int, int] = {}
    query_only: int | None = None
    busy_timeout: int | None = None
    installed = begin_attempted = False
    try:
        if db.in_transaction:
            raise FormatEvidenceError("format_transaction_active")
        if db.text_factory is not str:
            raise FormatEvidenceError("format_connection_unsupported")
        for category, maximum in (
            (sqlite3.SQLITE_LIMIT_SQL_LENGTH, _SQL_BYTES),
            (
                sqlite3.SQLITE_LIMIT_LENGTH,
                max(limits.marker_bytes, limits.object_bytes, limits.schema_bytes) + 1024,
            ),
        ):
            saved_limits[category] = db.getlimit(category)
            db.setlimit(category, min(saved_limits[category], maximum))
        db.set_progress_handler(reader.progress, reader.interval)
        installed = True
        if _rows(db, "PRAGMA read_uncommitted") != [(0,)]:
            raise FormatEvidenceError("format_isolation_unsupported")
        query_only = _rows(db, "PRAGMA query_only")[0][0]
        busy_timeout = _rows(db, "PRAGMA busy_timeout")[0][0]
        _rows(db, "PRAGMA query_only=ON")
        timeout = min(limits.busy_timeout_ms, int(limits.seconds * 1000))
        _rows(db, f"PRAGMA busy_timeout={timeout}")
        reader.check()
        reader.move("begin")
        begin_attempted = True
        _rows(db, "BEGIN")
        result = reader.read_optional() if optional else reader.read()
    except sqlite3.Error as error:
        code = reader.interrupted or (
            "format_read_timeout"
            if monotonic() >= reader.deadline
            else (
                "format_query_budget"
                if getattr(error, "sqlite_errorcode", None)
                in {sqlite3.SQLITE_TOOBIG, sqlite3.SQLITE_NOMEM}
                else "format_read_failed"
            )
        )
        raise FormatEvidenceError(code) from None
    except UnicodeError:
        raise FormatEvidenceError("format_record_invalid") from None
    finally:
        reader.failure_phase = reader.phase
        reader.move("cleanup")
        failed = False
        if installed:
            try:
                db.set_progress_handler(None, 0)
            except sqlite3.Error:
                failed = True
        if begin_attempted:
            try:
                if db.in_transaction:
                    _rows(db, "ROLLBACK")
            except sqlite3.Error:
                failed = True
        for category, previous in saved_limits.items():
            try:
                db.setlimit(category, previous)
            except sqlite3.Error:
                failed = True
        for name, previous_pragma in (("query_only", query_only), ("busy_timeout", busy_timeout)):
            if previous_pragma is not None:
                try:
                    _rows(db, f"PRAGMA {name}={previous_pragma}")
                except sqlite3.Error:
                    failed = True
        if failed:
            reader.failure_phase = "cleanup"
            raise FormatEvidenceError("format_cleanup_failed") from None
    return result
