"""Current persisted contracts, not an installation or cross-version permit."""

from __future__ import annotations

import sqlite3

from muli.compatibility import FormatState, FormatVersions
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatEvidence,
    read_optional_format_evidence,
)
from muli.store_initialization import CreatedDatabase

# Opaque contract IDs, independently versioned when a domain's interpretation
# changes. These do not claim any resolver context or actual install profile.
LEGACY_FORMAT_STATE = FormatState(
    metadata_version=1,
    formats=FormatVersions(
        database=1,
        objects=1,
        configuration=1,
        ingress=1,
        outbox=1,
        memory=1,
        install_profile=1,
    ),
)
PREVIOUS_FORMAT_STATE = FormatState(
    metadata_version=1,
    formats=FormatVersions(
        database=1,
        objects=2,
        configuration=1,
        ingress=2,
        outbox=2,
        memory=1,
        install_profile=1,
    ),
)
CURRENT_FORMAT_STATE = FormatState(
    metadata_version=1,
    formats=FormatVersions(
        database=1,
        objects=3,
        configuration=1,
        ingress=3,
        outbox=3,
        memory=1,
        install_profile=1,
    ),
)


class FormatAdmissionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_current_format(evidence: FormatEvidence) -> None:
    if evidence.state in (LEGACY_FORMAT_STATE, PREVIOUS_FORMAT_STATE):
        raise FormatAdmissionError("store_format_upgrade_required")
    if evidence.state != CURRENT_FORMAT_STATE:
        raise FormatAdmissionError("store_format_unsupported")


def require_channel_ownership_format(state: FormatState | None) -> None:
    if state != CURRENT_FORMAT_STATE:
        raise FormatAdmissionError("store_format_upgrade_required")


def publish_created_format(db: sqlite3.Connection, creation: CreatedDatabase) -> None:
    """The locked factory already initialized and validated all three groups.

    Only this last marker transaction is atomic. Earlier preparation can commit;
    failure leaves unmarked legacy data and never creates reusable provenance.
    """
    creation.check()
    if db.in_transaction:
        raise FormatAdmissionError("store_format_transaction_active")
    if read_optional_format_evidence(db) is not None:
        raise FormatAdmissionError("store_format_already_present")
    db.execute("PRAGMA synchronous=FULL")
    if db.execute("PRAGMA synchronous").fetchone()[0] != 2:
        raise FormatAdmissionError("store_format_durability_unsupported")
    try:
        db.execute("BEGIN IMMEDIATE")
        creation.check()
        db.execute(
            "INSERT INTO runtime_state(name,value) VALUES(?,?)",
            (FORMAT_STATE_KEY, CURRENT_FORMAT_STATE.model_dump_json()),
        )
        db.execute("COMMIT")
        creation.check()
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
