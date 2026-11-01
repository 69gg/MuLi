"""Conservative, read-only construction requirements, not format certification."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Any, Literal

from pydantic import Field

from muli.management_transaction import StrictRecord, _object
from muli.memory_schema import SCHEMA as MEMORY_SCHEMA

SchemaGroup = Literal["all", "core", "memory", "channel"]
_IDENTITY_BYTES = 96
_MAIN_OBJECT_BYTES = 16_384
_PROGRESS_INTERVAL = 100
_SQL_BYTES = 4096
_OBSERVATION_BYTES = 512
_MAX_MILLISECONDS = 2**31 - 1
thread_time: Callable[[], float] | None = getattr(time, "thread_time", None)


def _sample_observation_clock(clock: Callable[[], float] | None) -> float | None:
    try:
        value = None if clock is None else clock()
        return value if value is not None and math.isfinite(value) else None
    except Exception:
        return None  # Optional timing must not change admission or replace its failure.


def _observation_milliseconds(started: float | None, ended: float | None) -> int | None:
    if started is None or ended is None:
        return None
    elapsed = ended - started
    if not math.isfinite(elapsed) or elapsed < 0:
        return None
    return int(min(_MAX_MILLISECONDS, elapsed * 1000))


def session_record_time(field: str) -> str:
    """Share the current session directory expression with its actual DDL."""
    value = f"json_extract(data,'$.{field}')"
    return (
        f"CASE WHEN json_type(data,'$.{field}') IN ('integer','real') "
        f"AND {value}>0 AND {value}<={sys.float_info.max!r} THEN {value} END"
    )


SESSION_RECORD_CREATED = session_record_time("created_at")
SESSION_RECORD_SORT = f"coalesce(({SESSION_RECORD_CREATED}),0)"
SESSION_DIRECTORY_SCHEMA = (
    "CREATE INDEX IF NOT EXISTS sessions_created_directory "
    f"ON objects(kind,({SESSION_RECORD_SORT}) DESC,id DESC) WHERE kind='session'"
)

# This optional performance index is deliberately outside CORE_SCHEMA and the
# objects4 admission contract. Only explicit, owned server activation prepares it.
NOTIFICATION_RECORD_BYTES = 65_536
_NOTIFICATION_CREATED_VALUE = "json_extract(data,'$.created_at')"
NOTIFICATION_RECORD_SORT = (
    "CASE WHEN typeof(data)='text' "
    f"AND length(CAST(data AS BLOB))<={NOTIFICATION_RECORD_BYTES} THEN "
    "CASE WHEN json_valid(data) THEN CASE WHEN json_type(data)='object' "
    "AND json_type(data,'$.created_at') IN ('integer','real') "
    f"AND {_NOTIFICATION_CREATED_VALUE}>0 "
    f"AND {_NOTIFICATION_CREATED_VALUE}<={sys.float_info.max!r} "
    f"THEN {_NOTIFICATION_CREATED_VALUE} ELSE 0 END ELSE 0 END ELSE 0 END"
)
NOTIFICATION_DIRECTORY_SCHEMA = (
    "CREATE INDEX IF NOT EXISTS notifications_created_directory "
    f"ON objects(kind,({NOTIFICATION_RECORD_SORT}) DESC,id DESC) WHERE kind='notification'"
)

# These are the ordinary constructors' declarations, not a second schema model.
CORE_SCHEMA = """
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(kind,id)
            );
            CREATE INDEX IF NOT EXISTS objects_updated ON objects(kind,updated DESC);
            CREATE INDEX IF NOT EXISTS agents_parent ON objects(json_extract(data,'$.parent_id')) WHERE kind='agent';
            CREATE INDEX IF NOT EXISTS agents_status ON objects(json_extract(data,'$.status')) WHERE kind='agent';
            CREATE INDEX IF NOT EXISTS agents_commitment ON objects(json_extract(data,'$.commitment_id')) WHERE kind='agent';
            CREATE INDEX IF NOT EXISTS workflow_runs_status ON objects(json_extract(data,'$.status')) WHERE kind='workflow_run';
            CREATE INDEX IF NOT EXISTS objects_unfinished_lifecycle ON objects(kind,id)
                WHERE kind IN ('session','run','approval','workflow_run')
                AND json_extract(data,'$.status') IN ('pending','queued','running');
            CREATE INDEX IF NOT EXISTS workflow_runs_by_parent
                ON objects(json_extract(data,'$.parent_run_id'),id) WHERE kind='workflow_run';
            CREATE INDEX IF NOT EXISTS task_plan_pending ON objects(id) WHERE kind='task_plan_delivery' AND json_extract(data,'$.state')='pending';
            CREATE TABLE IF NOT EXISTS message_order (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS message_order_session ON message_order(session_id,seq);
            CREATE TABLE IF NOT EXISTS runtime_state (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingress (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                received REAL NOT NULL, delivered_seq INTEGER, run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS ingress_pending ON ingress(session_id,state,seq);
            CREATE INDEX IF NOT EXISTS ingress_system_log_run
                ON ingress(run_id) WHERE run_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                session_id TEXT, data TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
            CREATE UNIQUE INDEX IF NOT EXISTS team_completion_revision
                ON events(session_id,json_extract(data,'$.completion_revision'))
                WHERE type='team.finished';
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, envelope TEXT NOT NULL, received REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL,
                source TEXT NOT NULL, entities TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, confidence REAL NOT NULL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS relations (
                memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
                PRIMARY KEY(memory_id,subject,predicate,object)
            );
            CREATE INDEX IF NOT EXISTS relations_subject ON relations(subject);
            CREATE INDEX IF NOT EXISTS relations_object ON relations(object);
"""

CHANNEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_state (name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channel_inbound (
 id TEXT PRIMARY KEY,name TEXT NOT NULL,attachment_ids TEXT NOT NULL DEFAULT '[]',
 receipt TEXT,created_at REAL NOT NULL,fingerprint TEXT);
CREATE TABLE IF NOT EXISTS channel_outbox (
 id TEXT PRIMARY KEY,name TEXT NOT NULL,route TEXT NOT NULL,payload TEXT NOT NULL,
 state TEXT NOT NULL,next_part INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL,last_error TEXT NOT NULL DEFAULT '',receipts TEXT NOT NULL DEFAULT '[]',
 created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS channel_outbox_ready ON channel_outbox(state,next_attempt,created_at);
CREATE INDEX IF NOT EXISTS channel_outbox_continuity
 ON channel_outbox(json_extract(payload,'$.channel_continuity.id'));
"""


class ExistingStoreError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExistingStoreLimits(StrictRecord):
    seconds: float = Field(default=1.0, gt=0, le=5, allow_inf_nan=False)
    vm_steps: int = Field(default=200_000, ge=1, le=1_000_000)
    source_bytes: int = Field(default=32_768, ge=1, le=65_536)
    schema_objects: int = Field(default=96, ge=1, le=128)
    sql_bytes: int = Field(default=4096, ge=1, le=16_384)
    main_object_bytes: int = Field(default=_MAIN_OBJECT_BYTES, ge=1, le=65_536)


@dataclass(frozen=True, slots=True)
class _Definition:
    kind: str
    name: str
    sql: bytes


def _statements(source: str) -> Iterator[str]:
    """Split only repository declarations with SQLite's statement boundary API."""
    pending = ""
    for line in source.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip().removesuffix(";")
            pending = ""
    if pending.strip():
        raise ExistingStoreError("existing_store_declaration_invalid")


def _definitions(group: SchemaGroup, limits: ExistingStoreLimits) -> tuple[_Definition, ...]:
    sources = {
        "core": CORE_SCHEMA + SESSION_DIRECTORY_SCHEMA + ";",
        "memory": MEMORY_SCHEMA,
        "channel": CHANNEL_SCHEMA,
    }
    selected = tuple(sources.values()) if group == "all" else (sources[group],)
    if sum(len(source.encode("utf-8")) for source in selected) > limits.source_bytes:
        raise ExistingStoreError("existing_store_declaration_budget")
    definitions: list[_Definition] = []
    for source in selected:
        for statement in _statements(source):
            # This one ordinary initialization write is deliberately not executed.
            if statement == "INSERT OR IGNORE INTO memory_state VALUES('revision',0)":
                continue
            for prefix, kind in (
                ("CREATE TABLE IF NOT EXISTS ", "table"),
                ("CREATE INDEX IF NOT EXISTS ", "index"),
                ("CREATE UNIQUE INDEX IF NOT EXISTS ", "index"),
            ):
                if statement.startswith(prefix):
                    body = statement[len(prefix) :]
                    parts = body.split(maxsplit=1)
                    if len(parts) != 2:
                        raise ExistingStoreError("existing_store_declaration_invalid")
                    name = parts[0]
                    if not name.isascii() or not name.isidentifier():
                        raise ExistingStoreError("existing_store_declaration_invalid")
                    sql = (prefix.replace(" IF NOT EXISTS", "") + body).encode("utf-8")
                    if len(sql) > limits.sql_bytes:
                        raise ExistingStoreError("existing_store_declaration_budget")
                    definitions.append(_Definition(kind, name, sql))
                    break
            else:
                raise ExistingStoreError("existing_store_declaration_invalid")
    if len(definitions) > limits.schema_objects:
        raise ExistingStoreError("existing_store_declaration_budget")
    if len({definition.name for definition in definitions}) != len(definitions):
        raise ExistingStoreError("existing_store_declaration_invalid")
    return tuple(definitions)


def _row(db: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> tuple[Any, ...]:
    with closing(db.cursor()) as cursor:
        cursor.row_factory = None
        rows = cursor.execute(sql, parameters).fetchmany(2)
    if len(rows) != 1:
        raise ExistingStoreError("existing_store_evidence_missing")
    return tuple(rows[0])


def _validate_definitions(
    db: sqlite3.Connection, definitions: tuple[_Definition, ...], limits: ExistingStoreLimits
) -> None:
    """Check required names in one bounded scan, streaming one SQL value at a time."""
    remaining = {definition.name: definition for definition in definitions}
    placeholders = ",".join("?" for _ in remaining)
    with closing(db.cursor()) as cursor:
        cursor.row_factory = None
        cursor.execute(
            "SELECT name,type,CASE WHEN length(CAST(sql AS BLOB))<=? "
            "THEN CAST(sql AS BLOB) END FROM main.sqlite_schema "
            f"WHERE name IN ({placeholders}) LIMIT ?",
            (limits.sql_bytes, *remaining, len(remaining) + 1),
        )
        for name, kind, raw in cursor:
            definition = remaining.pop(name, None)
            if definition is None:
                raise ExistingStoreError("existing_store_evidence_missing")
            if kind != definition.kind or raw != definition.sql:
                raise ExistingStoreError("existing_store_schema_unsupported")
    if remaining:
        raise ExistingStoreError("existing_store_evidence_missing")


def _text(db: sqlite3.Connection, name: str, maximum: int = _IDENTITY_BYTES) -> str:
    kind, size, raw = _row(
        db,
        "SELECT typeof(value),length(CAST(value AS BLOB)),"
        "CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) BETWEEN 1 AND ? "
        "THEN CAST(value AS BLOB) END FROM main.runtime_state WHERE name=? LIMIT 2",
        (maximum, name),
    )
    if kind != "text" or type(size) is not int or not 1 <= size <= maximum:
        raise ExistingStoreError("existing_store_identity_invalid")
    if not isinstance(raw, bytes):
        raise ExistingStoreError("existing_store_identity_invalid")
    return raw.decode("utf-8")


def read_existing_main(
    db: sqlite3.Connection, identifier: str | None, *, maximum: int = _MAIN_OBJECT_BYTES
) -> dict[str, Any]:
    """Bounded PK lookup; never manufacture an identity or repair its owner."""
    if (
        not isinstance(identifier, str)
        or not 1 <= len(identifier.encode("utf-8")) <= _IDENTITY_BYTES
    ):
        raise ExistingStoreError("existing_store_identity_invalid")
    try:
        kind, size, raw = _row(
            db,
            "SELECT typeof(data),length(CAST(data AS BLOB)),"
            "CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(data AS BLOB) END FROM main.objects WHERE kind='session' AND id=? LIMIT 2",
            (maximum, identifier),
        )
        if (
            kind != "text"
            or type(size) is not int
            or not 1 <= size <= maximum
            or not isinstance(raw, bytes)
        ):
            raise ExistingStoreError("existing_store_identity_invalid")
        session = _object(raw)
        if (
            session.get("id") != identifier
            or session.get("kind") != "main"
            or session.get("principal") != "owner"
        ):
            raise ExistingStoreError("existing_store_identity_invalid")
        return session
    except ExistingStoreError:
        raise
    except (sqlite3.Error, ValueError, RecursionError):
        raise ExistingStoreError("existing_store_identity_invalid") from None


def validate_existing_schema(
    db: sqlite3.Connection,
    group: SchemaGroup = "all",
    *,
    limits: ExistingStoreLimits | None = None,
) -> None:
    """Own a read snapshot and progress callback on the Store-owned connection."""
    limits = limits or ExistingStoreLimits()
    definitions = _definitions(group, limits)
    deadline = monotonic() + limits.seconds

    # Independent observations leave the budget clock's reads and gates unchanged.
    def sample() -> tuple[float | None, float | None]:
        return _sample_observation_clock(perf_counter), _sample_observation_clock(thread_time)

    started = sample()
    schema_started: tuple[float | None, float | None] | None = None
    schema_ended: tuple[float | None, float | None] | None = None
    rejected: tuple[float | None, float | None] | None = None
    steps = 0
    interval = min(_PROGRESS_INTERVAL, limits.vm_steps)
    begun = installed = False
    interrupted: Literal["vm_steps", "wall_clock"] | None = None
    phase: Literal["setup", "schema", "core_markers", "memory_markers", "complete"] = "setup"
    query_only: int | None = None
    busy_timeout: int | None = None
    saved_limits: dict[int, int] = {}

    def progress() -> int:
        nonlocal steps, interrupted, rejected
        steps += interval
        # Preserve the original short-circuit and clock reads. This records the
        # first rejecting gate; a VM rejection does not sample the wall clock.
        if steps >= limits.vm_steps:
            interrupted = "vm_steps"
        elif monotonic() >= deadline:
            interrupted = "wall_clock"
        else:
            interrupted = None
        if interrupted is not None:
            rejected = sample()
        return int(interrupted is not None)

    def budget_error(trigger: Literal["vm_steps", "wall_clock"]) -> ExistingStoreError:
        error = ExistingStoreError("existing_store_read_budget")

        def add_observation(value: dict[str, Any]) -> bool:
            note = "existing_store_read_observation=" + json.dumps(
                value, sort_keys=True, separators=(",", ":")
            )
            if len(note.encode("utf-8")) > _OBSERVATION_BYTES:
                return False
            error.add_note(note)
            return True

        observation: dict[str, Any] = {
            "group": group,
            "phase": phase,
            "trigger": trigger,
            "steps": steps,
            "vm_limit": limits.vm_steps,
        }
        try:
            # Snapshot the rejecting gate, not SQLite unwinding or connection cleanup.
            ended = rejected or (None, None)
            boundary = schema_started or ended
            schema_end = schema_ended or ended
            if add_observation(
                {
                    **observation,
                    "timing": {
                        "elapsed_ms": _observation_milliseconds(started[0], ended[0]),
                        "before_schema_ms": _observation_milliseconds(started[0], boundary[0]),
                        "schema_ms": None
                        if schema_started is None
                        else _observation_milliseconds(schema_started[0], schema_end[0]),
                        "thread_cpu_ms": _observation_milliseconds(started[1], ended[1]),
                        "before_schema_cpu_ms": _observation_milliseconds(started[1], boundary[1]),
                        "schema_cpu_ms": None
                        if schema_started is None
                        else _observation_milliseconds(schema_started[1], schema_end[1]),
                    },
                }
            ):
                return error
        except Exception:
            pass  # Preserve the original five fields if the new observation cannot be added.
        try:
            add_observation(observation)
        except Exception:
            pass  # Optional diagnostics must not replace the original rejection.
        return error

    try:
        try:
            if db.in_transaction:
                raise ExistingStoreError("existing_store_outer_transaction")
            if (
                db.text_factory is not str
                or db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
                or db.isolation_level is None
            ):
                raise ExistingStoreError("existing_store_connection_unsupported")
            for category, maximum in (
                (sqlite3.SQLITE_LIMIT_SQL_LENGTH, _SQL_BYTES),
                (
                    sqlite3.SQLITE_LIMIT_LENGTH,
                    max(limits.sql_bytes, limits.main_object_bytes) + 1024,
                ),
            ):
                saved_limits[category] = db.getlimit(category)
                db.setlimit(category, min(saved_limits[category], maximum))
            db.set_progress_handler(progress, interval)
            installed = True
            if db.execute("PRAGMA read_uncommitted").fetchone()[0] != 0:
                raise ExistingStoreError("existing_store_isolation_unsupported")
            query_only = int(db.execute("PRAGMA query_only").fetchone()[0])
            busy_timeout = int(db.execute("PRAGMA busy_timeout").fetchone()[0])
            db.execute("PRAGMA query_only=ON")
            timeout = min(busy_timeout, int(max(0, deadline - monotonic()) * 1000))
            db.execute(f"PRAGMA busy_timeout={timeout}")
            if db.execute("PRAGMA main.journal_mode").fetchone()[0] != "wal":
                raise ExistingStoreError("existing_store_journal_unsupported")
            db.execute("BEGIN")
            begun = True
            phase = "schema"
            schema_started = sample()
            _validate_definitions(db, definitions, limits)
            schema_ended = sample()
            if group in {"all", "core"}:
                phase = "core_markers"
                if _text(db, "message_sequence_v1") != "done":
                    raise ExistingStoreError("existing_store_marker_invalid")
                _text(db, "store_instance_v1")
                read_existing_main(db, _text(db, "main_session"), maximum=limits.main_object_bytes)
            if group in {"all", "memory"}:
                phase = "memory_markers"
                for name, expected in (("revision", None), ("alias_evidence_version", 1)):
                    kind, value = _row(
                        db,
                        "SELECT typeof(value),CASE WHEN typeof(value)='integer' THEN value END "
                        "FROM main.memory_state WHERE name=? LIMIT 2",
                        (name,),
                    )
                    if (
                        kind != "integer"
                        or value < 0
                        or (expected is not None and value != expected)
                    ):
                        raise ExistingStoreError("existing_store_marker_invalid")
            phase = "complete"
            if monotonic() >= deadline:
                rejected = sample()
                raise budget_error("wall_clock")
        finally:
            failed = False
            if installed:
                try:
                    db.set_progress_handler(None, 0)
                except sqlite3.Error:
                    failed = True
            try:
                if begun and db.in_transaction:
                    db.execute("ROLLBACK")
            except sqlite3.Error:
                failed = True
            finally:
                for category, value in saved_limits.items():
                    try:
                        db.setlimit(category, value)
                    except sqlite3.Error:
                        failed = True
                for name, value in (("query_only", query_only), ("busy_timeout", busy_timeout)):
                    if value is not None:
                        try:
                            db.execute(f"PRAGMA {name}={value}")
                        except sqlite3.Error:
                            failed = True
            if failed:
                raise ExistingStoreError("existing_store_cleanup_failed")
    except (sqlite3.Error, UnicodeError):
        if interrupted is not None:
            raise budget_error(interrupted) from None
        raise ExistingStoreError("existing_store_unavailable") from None
