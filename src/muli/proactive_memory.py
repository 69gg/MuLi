"""Exact notification-preference evidence, without loading or searching memory text."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from muli.config import Settings

# All three joins are primary-key lookups. Malformed metadata cannot cause a
# large value to be returned; neither memory nor source content is selected.
PREFERENCE_SQL = (
    "SELECT CASE WHEN typeof(m.version)='integer' THEN m.version END AS version,"
    "m.layer='preference' AS preference,m.status='active' AS active,"
    "m.trusted_owner=1 AS fact_trusted,s.trusted_owner=1 AS source_trusted,"
    "s.role='user' AND s.principal='owner' AS owner_source,"
    "substr(s.id,1,201) AS source_id,"
    "CASE WHEN length(s.digest)=64 THEN s.digest END AS digest "
    "FROM memory_meta m JOIN memories f ON f.id=m.memory_id "
    "JOIN memory_sources s ON s.id=m.source_id WHERE m.memory_id=?"
)


def _digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True, slots=True)
class PreferenceEvidence:
    memory_id: str
    version: int
    source_id: str
    source_digest: str

    def dump(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, value: object) -> PreferenceEvidence | None:
        if not isinstance(value, dict) or set(value) != {
            "memory_id",
            "version",
            "source_id",
            "source_digest",
        }:
            return None
        if any(
            not isinstance(value.get(key), str) or not 0 < len(value[key]) <= 200
            for key in ("memory_id", "source_id")
        ):
            return None
        if (
            type(value.get("version")) is not int
            or value["version"] < 1
            or not _digest(value.get("source_digest"))
        ):
            return None
        return cls(value["memory_id"], value["version"], value["source_id"], value["source_digest"])


def check_preference(
    settings: Settings, db: sqlite3.Connection | None
) -> tuple[PreferenceEvidence | None, str]:
    """Read one selected fact's finite metadata; never commit the caller's transaction."""
    preference = settings.channels.proactive.memory_preference
    if preference is None:
        return None, "not_configured"
    if not settings.memory.enabled:
        return None, "memory_disabled"
    if db is None:
        return None, "unavailable"
    try:
        row = db.execute(PREFERENCE_SQL, (preference.memory_id,)).fetchone()
    except sqlite3.OperationalError:
        return None, "unavailable"
    if row is None:
        return None, "missing"
    if not row["preference"]:
        return None, "not_preference"
    if not row["active"]:
        return None, "inactive"
    if row["version"] != preference.version:
        return None, "version_changed"
    if not row["fact_trusted"] or not row["source_trusted"] or not row["owner_source"]:
        return None, "untrusted"
    evidence = PreferenceEvidence.load(
        {
            "memory_id": preference.memory_id,
            "version": row["version"],
            "source_id": row["source_id"],
            "source_digest": row["digest"],
        }
    )
    return (evidence, "valid") if evidence is not None else (None, "source_invalid")
