"""Only the fixed, stopped-service 1→2 SQL effect; no Store or boot admission."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from time import monotonic, time
from typing import Literal

from pydantic import field_validator

from muli.compatibility import _METADATA_BYTES
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatReadLimits,
    SchemaFact,
    _Reader,
    read_format_evidence,
)
from muli.managed_boot import _DatabaseAnchor
from muli.managed_transaction import (
    ProjectFormatDatabase,
    ProjectFormatTransitionRecord,
    format_digest,
)
from muli.management_projection import _database_identity, _read_projection
from muli.management_transaction import (
    Digest,
    Identifier,
    ProjectionReceipt,
    StrictRecord,
    _object,
    _snapshot,
)
from muli.platforms.linux_transaction import ObservationError
from muli.store_initialization import StoreConstructionLock
from muli.store_schema import (
    _IDENTITY_BYTES,
    ExistingStoreLimits,
    _text,
    read_existing_main,
    validate_existing_schema,
)

_RECEIPT_KIND = "managed_format_transition"


def _open_database(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        database.as_uri() + "?mode=rw&cache=private",
        uri=True,
        timeout=0,
        isolation_level="DEFERRED",
    )


class _Receipt(StrictRecord):
    id: Identifier
    version: Literal[1] = 1
    migration_id: Literal["project_review_1_to_2"] = "project_review_1_to_2"
    generation: Identifier
    database: ProjectFormatDatabase
    source_format_digest: Digest
    target_format_digest: Digest
    store_instance: Identifier
    main_session: Identifier
    source_projection: ProjectionReceipt
    source_configuration_digest: Digest

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid_migration_receipt_version")
        return value


def _expected(record: ProjectFormatTransitionRecord) -> _Receipt:
    return _Receipt(
        id=record.generation,
        generation=record.generation,
        database=record.migration.database,
        source_format_digest=format_digest(record.source_format),
        target_format_digest=format_digest(record.target_format),
        store_instance=record.identity.store_instance,
        main_session=record.identity.main_session,
        source_projection=record.source.projected,
        source_configuration_digest=record.source.binding.configuration.digest,
    )


def _marker(db: sqlite3.Connection, maximum: int) -> bytes:
    rows = db.execute(
        "SELECT CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) BETWEEN 1 AND ? "
        "THEN CAST(value AS BLOB) END FROM main.runtime_state WHERE name=? LIMIT 2",
        (maximum, FORMAT_STATE_KEY),
    ).fetchall()
    if len(rows) != 1:
        raise ObservationError("transaction_format_source_changed")
    raw = rows[0][0]
    if not isinstance(raw, bytes):
        raise ObservationError("transaction_format_source_changed")
    return raw


def _receipt(db: sqlite3.Connection, maximum: int) -> _Receipt | None:
    rows = db.execute(
        "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB)) BETWEEN 1 AND ? "
        "THEN CAST(id AS BLOB) END,"
        "CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB)) BETWEEN 1 AND ? "
        "THEN CAST(data AS BLOB) END FROM main.objects WHERE kind=? LIMIT 2",
        (_IDENTITY_BYTES, maximum, _RECEIPT_KIND),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or not all(isinstance(value, bytes) for value in rows[0]):
        raise ObservationError("transaction_format_receipt_invalid")
    try:
        identifier = rows[0][0].decode("utf-8")
        fields = _object(rows[0][1])
        if fields.keys() != _Receipt.model_fields.keys():
            raise ValueError
        value = _Receipt.model_validate(fields)
        if value.id != identifier or value.id != value.generation:
            raise ValueError
        return value
    except (ValueError, RecursionError):
        raise ObservationError("transaction_format_receipt_invalid") from None


def _writing_schema(schema: tuple[SchemaFact, ...]) -> None:
    """Only the two fixed DML targets must have no extension triggers."""
    if any(
        fact.kind == "trigger" and fact.table in {"objects", "runtime_state"} for fact in schema
    ):
        raise ObservationError("transaction_format_schema_unsupported")


def migrate_project_format(
    database: Path,
    record: ProjectFormatTransitionRecord,
    *,
    guard: Callable[[], None],
    seconds: float,
    metadata_bytes: int,
    confirm: Callable[[], None] | None = None,
) -> None:
    """Caller retains the controller/writer locks and stopped empty-cgroup guard.

    The optional confirmation runs only inside the authenticated pair's locks;
    it does not confirm file installation or admit a process. A missing callback
    deliberately preserves the SQL2/Journal1 recovery prefix for callers.
    """
    if (
        not math.isfinite(seconds)
        or seconds <= 0
        or type(metadata_bytes) is not int
        or metadata_bytes < 1
    ):
        raise ObservationError("transaction_format_budget_invalid")
    guard()
    deadline = monotonic() + seconds
    maximum = min(_METADATA_BYTES, metadata_bytes)
    expected = _expected(record)
    payload = expected.model_dump_json().encode()
    if len(payload) > maximum:
        raise ObservationError("transaction_format_receipt_budget")
    anchor = _DatabaseAnchor(database)
    try:
        with StoreConstructionLock(database) as construction:

            def check() -> None:
                guard()
                construction.check()
                anchor.check()
                if anchor.identity != (
                    record.migration.database.device,
                    record.migration.database.inode,
                ):
                    raise ObservationError("transaction_format_database_changed")
                if monotonic() >= deadline:
                    raise TimeoutError

            def remaining() -> float:
                check()
                return deadline - monotonic()

            def connect() -> sqlite3.Connection:
                check()
                db = _open_database(database)
                try:
                    check()
                    db.execute("PRAGMA synchronous=FULL")
                    if db.execute("PRAGMA synchronous").fetchone() != (2,):
                        raise ObservationError("transaction_format_durability_unsupported")
                    validate_existing_schema(
                        db, limits=ExistingStoreLimits(seconds=min(1.0, remaining()))
                    )
                    return db
                except BaseException:
                    db.close()
                    raise

            def inspect(db: sqlite3.Connection) -> bool:
                """True only for the exact committed pair, never a count/cache."""
                evidence = read_format_evidence(
                    db, limits=FormatReadLimits(seconds=min(1.0, remaining()))
                )
                _writing_schema(evidence.schema)
                check()
                identity = _database_identity(db)
                if (
                    identity.path != database
                    or (evidence.store_instance, evidence.main_session)
                    != (record.identity.store_instance, record.identity.main_session)
                    or (identity.instance, identity.main)
                    != (record.identity.store_instance, record.identity.main_session)
                    or _read_projection(db, identity) != record.source.projected
                ):
                    raise ObservationError("transaction_format_identity_changed")
                raw, receipt = _marker(db, maximum), _receipt(db, maximum)
                if evidence.state == record.target_format:
                    if (
                        raw != record.target_format.model_dump_json().encode()
                        or receipt != expected
                    ):
                        raise ObservationError("transaction_format_receipt_invalid")
                    check()
                    return True
                if (
                    evidence.state != record.source_format
                    or _snapshot(raw, maximum) != record.migration.source_marker
                ):
                    raise ObservationError("transaction_format_source_changed")
                if receipt is not None:
                    raise ObservationError("transaction_format_receipt_invalid")
                check()
                return False

            def complete() -> None:
                check()
                if confirm is not None:
                    confirm()
                check()

            commit_unknown = False
            with closing(connect()) as db:
                if inspect(db):
                    complete()
                    return
                if record.revision == 5:
                    raise ObservationError("transaction_format_source_changed")
                identity = _database_identity(db)
                steps = 0
                vm_steps = FormatReadLimits().vm_steps

                def progress() -> int:
                    nonlocal steps
                    steps += 100
                    return int(steps >= vm_steps or monotonic() >= deadline)

                db.setlimit(
                    sqlite3.SQLITE_LIMIT_SQL_LENGTH,
                    min(
                        db.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH),
                        ExistingStoreLimits().sql_bytes,
                    ),
                )
                db.setlimit(
                    sqlite3.SQLITE_LIMIT_LENGTH,
                    min(db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), maximum + 1024),
                )
                db.set_progress_handler(progress, 100)
                try:
                    check()
                    db.execute("BEGIN IMMEDIATE")
                    check()
                    # Reuse the bounded schema scanner inside this write
                    # snapshot. It owns neither a transaction nor callbacks;
                    # our existing progress/deadline gate remains installed.
                    _writing_schema(
                        _Reader(db, FormatReadLimits(seconds=min(1.0, remaining()))).schema()
                    )
                    check()
                    raw = _marker(db, maximum)
                    if _snapshot(raw, maximum) != record.migration.source_marker:
                        raise ObservationError("transaction_format_source_changed")
                    if (_text(db, "store_instance_v1"), _text(db, "main_session")) != (
                        record.identity.store_instance,
                        record.identity.main_session,
                    ) or _read_projection(db, identity) != record.source.projected:
                        raise ObservationError("transaction_format_identity_changed")
                    read_existing_main(db, record.identity.main_session, maximum=maximum)
                    if _receipt(db, maximum) is not None:
                        raise ObservationError("transaction_format_receipt_invalid")
                    check()
                    db.execute(
                        "INSERT INTO main.objects(kind,id,data,updated) VALUES(?,?,?,?)",
                        (_RECEIPT_KIND, record.generation, payload.decode(), time()),
                    )
                    changed = db.execute(
                        "UPDATE main.runtime_state SET value=? WHERE name=? AND typeof(value)='text' "
                        "AND CAST(value AS BLOB)=?",
                        (record.target_format.model_dump_json(), FORMAT_STATE_KEY, raw),
                    ).rowcount
                    if changed != 1:
                        raise ObservationError("transaction_format_source_changed")
                    check()
                    try:
                        db.execute("COMMIT")
                    except sqlite3.Error:
                        commit_unknown = True
                finally:
                    db.set_progress_handler(None, 0)
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                check()
                if not commit_unknown:
                    if not inspect(db):
                        raise ObservationError("transaction_format_commit_unknown")
                    complete()
                    return
            # Only a COMMIT exception reaches this path. The old connection is
            # closed and the same DB inode/locks are checked before opening anew.
            with closing(connect()) as reopened:
                if not inspect(reopened):
                    raise ObservationError("transaction_format_commit_unknown")
                complete()
    finally:
        anchor.close()
