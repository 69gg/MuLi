"""One stopped Linux store upgrade; never a managed cross-format transaction."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from muli.compatibility import FormatState
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    LEGACY_FORMAT_STATE,
    PREVIOUS_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatReadLimits,
    read_optional_format_evidence,
)
from muli.managed_boot import _DatabaseAnchor
from muli.management_transaction import TransactionJournal
from muli.platforms.managed_deployment import require_unselected_without_record
from muli.store_initialization import StoreConstructionLock
from muli.store_schema import validate_existing_schema


def upgrade_channel_ownership_format(data_dir: Path, *, managed_root: Path | None = None) -> bool:
    """Upgrade either exact supported predecessor while owning cooperative writers.

    Existing receipts, ingress and history keep their bytes and identifiers. The
    new reader owns their legacy replay semantics; no ownership is inferred here.
    """
    if sys.platform != "linux":
        raise FormatAdmissionError("store_format_upgrade_platform_unsupported")
    if managed_root is not None:
        raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
    path = data_dir.absolute() / "muli.sqlite3"
    root = data_dir.absolute() / "management" / "transaction-v1"
    with closing(_DatabaseAnchor(path)) as database, TransactionJournal(root, path) as journal:
        with (
            journal.locked() as control,
            journal.writer_lease() as writer,
            StoreConstructionLock(path) as construction,
        ):
            if control.read_current() is not None:
                raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
            try:
                require_unselected_without_record(root)
            except ValueError:
                raise FormatAdmissionError("store_format_managed_upgrade_unsupported") from None
            database.check()
            with closing(
                sqlite3.connect(
                    path.as_uri() + "?mode=rw&cache=private",
                    uri=True,
                    timeout=0,
                    isolation_level="DEFERRED",
                )
            ) as db:
                database.check()
                evidence = read_optional_format_evidence(db)
                if evidence is None:
                    raise FormatAdmissionError("store_format_upgrade_source_unsupported")
                validate_existing_schema(db)
                if (
                    db.execute(
                        "SELECT 1 FROM objects WHERE kind='management_projection' "
                        "AND id='transaction-v1'"
                    ).fetchone()
                    is not None
                    or db.execute(
                        "SELECT 1 FROM objects WHERE kind='management' AND CASE "
                        "WHEN json_valid(data) THEN json_extract(data,'$.action') IN ('adopt','switch') "
                        "OR json_type(data,'$.transaction') IS NOT NULL ELSE 1 END LIMIT 1"
                    ).fetchone()
                    is not None
                ):
                    raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
                if evidence.state == CURRENT_FORMAT_STATE:
                    return False
                if evidence.state not in (LEGACY_FORMAT_STATE, PREVIOUS_FORMAT_STATE):
                    raise FormatAdmissionError("store_format_upgrade_source_unsupported")
                db.execute("PRAGMA synchronous=FULL")
                if db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise FormatAdmissionError("store_format_durability_unsupported")
                try:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT value FROM runtime_state WHERE name=? AND typeof(value)='text' "
                        "AND length(CAST(value AS BLOB)) BETWEEN 1 AND ?",
                        (FORMAT_STATE_KEY, FormatReadLimits().marker_bytes),
                    ).fetchone()
                    if (
                        row is None
                        or FormatState.from_json(row[0].encode("utf-8")) != evidence.state
                    ):
                        raise FormatAdmissionError("store_format_changed")
                    control.check()
                    writer.check()
                    construction.check()
                    database.check()
                    cursor = db.execute(
                        "UPDATE runtime_state SET value=? WHERE name=? AND value=?",
                        (CURRENT_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY, row[0]),
                    )
                    if cursor.rowcount != 1:
                        raise FormatAdmissionError("store_format_changed")
                    control.check()
                    writer.check()
                    construction.check()
                    database.check()
                    db.execute("COMMIT")
                finally:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                database.check()
                after = read_optional_format_evidence(db)
                if (
                    after is None
                    or after.state != CURRENT_FORMAT_STATE
                    or after.store_instance != evidence.store_instance
                    or after.main_session != evidence.main_session
                ):
                    raise FormatAdmissionError("store_format_changed")
                return True
