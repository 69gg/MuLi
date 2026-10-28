"""Finite failure periods for explicitly collected, still-current attachments."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from muli.memory import source_digest
from muli.proactive import FrozenRouting

if TYPE_CHECKING:
    from muli.store import Store

FAILURE_CHECKPOINT = "source_failure"
MAX_FAILURE_PROOF_BYTES = 4096
_FIELDS = {"version", "id", "source_id", "revision", "failed_at", "code", "routing"}
_CODE = "memory_source_failed"
_DIGEST = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class MemorySourceFailure:
    identifier: str
    source_id: str
    revision: str
    failed_at: float
    routing: FrozenRouting

    def dump(self) -> dict[str, Any]:
        return {
            "version": 1,
            "id": self.identifier,
            "source_id": self.source_id,
            "revision": self.revision,
            "failed_at": self.failed_at,
            "code": _CODE,
            "routing": self.routing.dump(),
        }

    @classmethod
    def load(cls, value: object, *, max_bytes: int) -> MemorySourceFailure | None:
        from muli.attachments import ATTACHMENT_ID_PATTERN

        if not isinstance(value, dict) or set(value) != _FIELDS:
            return None
        identifier, source_id, revision = (
            value.get(key) for key in ("id", "source_id", "revision")
        )
        if (
            type(value.get("version")) is not int
            or value["version"] != 1
            or value.get("code") != _CODE
            or not isinstance(identifier, str)
            or re.fullmatch(ATTACHMENT_ID_PATTERN, identifier) is None
            or not isinstance(source_id, str)
            or len(source_id) != len("attachment:") + 32
            or not source_id.startswith("attachment:")
            or re.fullmatch(ATTACHMENT_ID_PATTERN, source_id[len("attachment:") :]) is None
            or not isinstance(revision, str)
            or _DIGEST.fullmatch(revision) is None
        ):
            return None
        timestamp = value.get("failed_at")
        if type(timestamp) is not int and type(timestamp) is not float:
            return None
        try:
            if timestamp < 0 or not math.isfinite(timestamp):
                return None
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(raw) > min(max_bytes, MAX_FAILURE_PROOF_BYTES):
                return None
            routing = FrozenRouting.load(value["routing"])
            if routing.classification != "independent" or routing.dump() != value["routing"]:
                return None
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            return None
        return cls(identifier, source_id, revision, float(timestamp), routing)

    def detail(self) -> str:
        return json.dumps(
            {
                "source_id": self.source_id,
                "revision": self.revision,
                "code": _CODE,
                "failed_at": self.failed_at,
                "notice": "原文已保存，后台整理失败；先核对来源，不自动重试。",
            },
            ensure_ascii=False,
        )


def attachment_revision(store: Store, source_id: str, *, max_source_chars: int) -> str | None:
    """Authenticate the saved complete source, never reopen or re-extract its file."""
    from muli.attachment_memory_source import MAX_ATTACHMENT_SOURCE_BYTES, restore_attachment_source
    from muli.attachments import ATTACHMENT_ID_PATTERN

    prefix = "attachment:"
    if (
        not isinstance(source_id, str)
        or not source_id.startswith(prefix)
        or re.fullmatch(ATTACHMENT_ID_PATTERN, source_id[len(prefix) :]) is None
        or type(max_source_chars) is not int
        or max_source_chars < 1
    ):
        return None
    try:
        row = store.db.execute(
            "SELECT CAST(content AS BLOB) AS content,"
            "CASE WHEN typeof(source)='text' AND length(CAST(source AS BLOB))<=? "
            "THEN CAST(source AS BLOB) END AS source,"
            "CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 "
            "THEN CAST(digest AS BLOB) END AS digest,"
            "CASE WHEN typeof(created_at) IN ('integer','real') THEN created_at END AS created_at "
            "FROM memory_sources WHERE id=? AND role='tool' AND principal='external:attachment' "
            "AND trusted_owner=0 AND session_id='' AND typeof(content)='text' "
            "AND length(CAST(content AS BLOB))<=? "
            "AND NOT EXISTS (SELECT 1 FROM memory_forgotten_sources f WHERE f.id=?)",
            (
                MAX_ATTACHMENT_SOURCE_BYTES,
                source_id,
                min(max_source_chars * 4, sys.maxsize),
                source_id,
            ),
        ).fetchone()
        if (
            row is None
            or not isinstance(row["source"], bytes)
            or not isinstance(row["digest"], bytes)
        ):
            return None
        content = row["content"].decode("utf-8", errors="strict")
        if len(content) > max_source_chars:
            return None
        restored = restore_attachment_source(
            source_id[len(prefix) :],
            content,
            row["source"].decode("utf-8", errors="strict"),
            row["created_at"],
        )
        revision = row["digest"].decode("ascii")
        return revision if revision == source_digest(restored, trusted_owner=False) else None
    except (ValueError, TypeError, UnicodeError, OverflowError, sqlite3.Error):
        return None


def failure_current(
    store: Store,
    proof: MemorySourceFailure,
    *,
    max_source_chars: int,
    max_output_chars: int,
    verify_attachment: bool = False,
) -> bool:
    """Point-read only the proof and metadata; optionally recheck complete provenance."""
    budget = min(max_output_chars * 4, MAX_FAILURE_PROOF_BYTES)
    if MemorySourceFailure.load(proof.dump(), max_bytes=budget) != proof:
        return False
    try:
        row = store.db.execute(
            "SELECT CASE WHEN typeof(j.checkpoint)='text' "
            "AND length(CAST(j.checkpoint AS BLOB))<=? THEN CASE WHEN json_valid(j.checkpoint) "
            "THEN CASE WHEN length(CAST(json_extract(j.checkpoint,'$.source_failure') AS BLOB))<=? "
            "THEN CAST(json_extract(j.checkpoint,'$.source_failure') AS BLOB) END END END AS proof "
            "FROM memory_jobs j JOIN memory_sources s ON s.id=j.source_id "
            "WHERE j.source_id=? AND j.state='failed' AND j.updated_at=? "
            "AND s.digest=? AND s.role='tool' AND s.principal='external:attachment' "
            "AND s.trusted_owner=0 AND s.session_id='' AND typeof(s.content)='text' "
            "AND length(CAST(s.content AS BLOB))<=? "
            "AND NOT EXISTS (SELECT 1 FROM memory_forgotten_sources f WHERE f.id=s.id)",
            (
                min(max_output_chars * 4, sys.maxsize),
                budget,
                proof.source_id,
                proof.failed_at,
                proof.revision,
                min(max_source_chars * 4, sys.maxsize),
            ),
        ).fetchone()
        if row is None or not isinstance(row["proof"], bytes):
            return False
        raw = row["proof"]
        if (
            len(raw) > budget
            or MemorySourceFailure.load(json.loads(raw), max_bytes=budget) != proof
        ):
            return False
        return (
            not verify_attachment
            or attachment_revision(store, proof.source_id, max_source_chars=max_source_chars)
            == proof.revision
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
        return False


def input_failure(
    store: Store, identifier: str, *, max_output_chars: int
) -> tuple[bool, MemorySourceFailure | None]:
    """Read one host ingress proof without loading its text or old receipt history."""
    budget = min(max_output_chars * 4, MAX_FAILURE_PROOF_BYTES)
    try:
        row = store.db.execute(
            "SELECT json_type(payload,'$.attention_memory_failure') AS marked,"
            "CASE WHEN length(CAST(json_extract(payload,'$.attention_memory_failure') AS BLOB))<=? "
            "THEN CAST(json_extract(payload,'$.attention_memory_failure') AS BLOB) END AS proof "
            "FROM ingress WHERE id=? AND json_extract(payload,'$.principal')='system:attention'",
            (budget, identifier),
        ).fetchone()
        if row is None or row["marked"] is None:
            return False, None
        if row["marked"] != "object" or not isinstance(row["proof"], bytes):
            return True, None
        return True, MemorySourceFailure.load(json.loads(row["proof"]), max_bytes=budget)
    except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
        return True, None


class MemorySourceFailureExpired(ValueError):
    """A once-valid notice was revoked before its model/output boundary."""
