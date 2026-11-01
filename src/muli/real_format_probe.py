"""Read-only probe of the real SQLite database before and after a managed switch.

The probe opens an existing file with ``mode=ro&cache=private``. It never creates
the database, starts a transaction, runs candidate code, or writes format
metadata. Only the bounded format evidence reader is used, so an unreadable or
changed database becomes a fixed error code rather than an implicit migration.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from muli.compatibility import FormatState
from muli.format_evidence import (
    FormatEvidenceError,
    FormatReadLimits,
    read_optional_format_evidence,
)
from muli.managed_transaction import format_digest


class FormatProbeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RealFormatProbe:
    path: str
    store_instance: str
    main_session: str
    state: FormatState | None
    format_digest: str | None
    schema_version: int

    def verify(
        self,
        *,
        path: Path,
        store_instance: str,
        main_session: str,
        state: FormatState | None,
    ) -> None:
        expected = Path(path).resolve()
        if self.path != str(expected):
            raise FormatProbeError("format_probe_path_changed")
        if (self.store_instance, self.main_session) != (store_instance, main_session):
            raise FormatProbeError("format_probe_store_changed")
        if self.format_digest != (format_digest(state) if state is not None else None):
            raise FormatProbeError("format_probe_format_changed")


def probe_format(path: Path, *, limits: FormatReadLimits | None = None) -> RealFormatProbe:
    """Probe one existing database; symlinks and missing files are rejected."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise FormatProbeError("format_probe_unavailable") from None
    if Path(path).is_symlink() or not resolved.is_file():
        raise FormatProbeError("format_probe_not_regular_file")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro&cache=private"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        raise FormatProbeError("format_probe_unavailable") from None
    try:
        db.execute("PRAGMA busy_timeout=20")
        evidence = read_optional_format_evidence(db, limits=limits)
        if evidence is not None:
            store_instance, main_session = evidence.store_instance, evidence.main_session
            state = evidence.state
        else:
            values = dict(
                db.execute(
                    "SELECT name,CASE WHEN typeof(value)='text' "
                    "AND length(CAST(value AS BLOB)) BETWEEN 1 AND 96 THEN value END "
                    "FROM runtime_state WHERE name IN ('store_instance_v1','main_session')"
                ).fetchall()
            )
            store_instance, main_session = (
                values.get("store_instance_v1"),
                values.get("main_session"),
            )
            if not isinstance(store_instance, str) or not isinstance(main_session, str):
                raise FormatProbeError("format_probe_store_changed")
            state = None
        row = db.execute("PRAGMA schema_version").fetchone()
        schema_version = int(row[0]) if row is not None else 0
    except FormatProbeError:
        raise
    except (sqlite3.Error, FormatEvidenceError, TypeError, ValueError):
        raise FormatProbeError("format_probe_unreadable") from None
    finally:
        db.close()
    return RealFormatProbe(
        path=str(resolved),
        store_instance=store_instance,
        main_session=main_session,
        state=state,
        format_digest=format_digest(state) if state is not None else None,
        schema_version=schema_version,
    )
