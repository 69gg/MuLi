"""Read memory provenance and bounded source windows without loading the whole source."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from contextlib import closing
from typing import Any

from muli.evidence import EvidenceError, content_page, decode_utf8, json_size, validate_integer
from muli.memory import Memory
from muli.store import SQLITE_MAX_INTEGER

_SOURCE_METADATA = (
    "s.id,s.source,s.session_id,s.role,s.principal,s.trusted_owner,s.digest,s.created_at"
)
SOURCE_JOB_STATES = ("queued", "processing", "retry", "failed", "done", "deferred")
SOURCE_PAGE_LIMIT = 100
_SOURCE_TEXT = ("id", "source", "session_id", "role", "principal", "digest")


def query_result(result: dict[str, Any], *, max_output_chars: int) -> dict[str, Any]:
    """Project a cached query without slicing its identities or actual graph paths."""
    validate_integer(max_output_chars, "max_output_chars", minimum=1)
    sections = ("memories", "sources", "communities")
    omitted = {section: len(result[section]) for section in sections}
    page: dict[str, Any] = {
        "memories": [],
        "communities": [],
        "sources": [],
        "degraded": list(result["degraded"]),
        "revision": result["revision"],
        "truncated": False,
        "omitted": omitted,
    }
    if "source_ids" in result:
        page["source_ids"] = list(result["source_ids"])
    if json_size(page) > max_output_chars:
        raise EvidenceError("output_budget", "记忆查询定位元数据超过输出预算")
    # Reserve the longer false spelling throughout. Metadata, including counts,
    # remains complete; a large candidate must not hide smaller later matches.
    for section in sections:
        for item in result[section]:
            page[section].append(item)
            omitted[section] -= 1
            if json_size(page) > max_output_chars:
                page[section].pop()
                omitted[section] += 1
    page["truncated"] = any(omitted.values())
    return page


def memory_listing(
    items: list[dict[str, Any]], *, revision: int, max_path_chars: int
) -> dict[str, Any]:
    """Budget only added paths; keep the existing management list's full records."""
    validate_integer(max_path_chars, "max_path_chars", minimum=1)
    remaining = max_path_chars
    records = []
    omitted = False
    for item in items:
        record = dict(item)
        if "graph_via" in record:
            size = json_size({"graph_via": record["graph_via"]})
            if size <= remaining:
                remaining -= size
            else:
                del record["graph_via"]
                record["path_omitted"] = True
                omitted = True
        records.append(record)
    return {"items": records, "revision": revision, "truncated": omitted}


class MemoryEvidence:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory

    def _source_cursor(self, token: str | None, state: str | None, budget: int) -> int:
        if token is None:
            return SQLITE_MAX_INTEGER
        if not isinstance(token, str) or not token:
            raise EvidenceError("record_cursor_invalid", "来源目录游标无效，请从首页重新读取")
        if len(token) > budget:
            raise EvidenceError("output_budget", "来源目录游标超过当前输出预算")
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
            if not isinstance(payload, list) or len(payload) != 5:
                raise ValueError
            version, section, instance, selected_state, before = payload
            if (
                type(version) is not int
                or version != 1
                or section != "memory_sources"
                or instance != self.memory.store.instance_id
                or selected_state != state
                or type(before) is not int
                or not 1 <= before <= SQLITE_MAX_INTEGER
            ):
                raise ValueError
            return before - 1
        except (ValueError, TypeError, RecursionError, binascii.Error) as error:
            raise EvidenceError(
                "record_cursor_invalid", "来源目录游标不属于当前资料库或状态筛选"
            ) from error

    def _encode_source_cursor(self, sequence: int, state: str | None) -> str:
        payload = [1, "memory_sources", self.memory.store.instance_id, state, sequence]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _source_item(row: dict[str, Any]) -> dict[str, Any] | None:
        fields: dict[str, Any] = {}
        for name in (*_SOURCE_TEXT, "job_state", "last_error"):
            raw = row[name]
            if not isinstance(raw, bytes):
                if row[name + "_type"] == "text":
                    raise EvidenceError("output_budget", "完整来源元数据超过当前输出预算")
                return None
            try:
                fields[name] = decode_utf8(raw, allow_incomplete_tail=False)
            except EvidenceError:
                return None
        if (
            not fields["id"]
            or fields["role"] not in {"user", "assistant", "tool"}
            or re.fullmatch(r"[a-f0-9]{64}", fields["digest"]) is None
            or type(row["trusted_owner"]) is not int
            or row["trusted_owner"] not in {0, 1}
            or (row["trusted_owner"] == 1 and fields["role"] != "user")
            or type(row["created_at"]) not in {int, float}
            or not math.isfinite(row["created_at"])
            or type(row["total_bytes"]) is not int
            or row["total_bytes"] < 0
            or fields["job_state"] not in SOURCE_JOB_STATES
            or type(row["attempts"]) is not int
            or row["attempts"] < 0
            or (
                fields["last_error"]
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", fields["last_error"]) is None
            )
        ):
            return None
        return {
            "source_id": fields["id"],
            **{name: fields[name] for name in ("source", "session_id", "role", "principal")},
            "trusted_owner": bool(row["trusted_owner"]),
            "revision": fields["digest"],
            "created_at": row["created_at"],
            "total_bytes": row["total_bytes"],
            "job": {
                "state": fields["job_state"],
                "attempts": row["attempts"],
                "last_error": fields["last_error"],
            },
        }

    def sources(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
        state: str | None = None,
        max_output_chars: int,
    ) -> dict[str, Any]:
        """Inspect a finite source window, without reading bodies or running jobs."""
        validate_integer(limit, "limit", minimum=1)
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        if limit > SOURCE_PAGE_LIMIT or (
            state is not None and (not isinstance(state, str) or state not in SOURCE_JOB_STATES)
        ):
            raise EvidenceError("invalid_arguments", "来源目录的页数或状态筛选无效")
        before = self._source_cursor(cursor, state, max_output_chars)
        page: dict[str, Any] = {
            "items": [],
            "omitted": 0,
            "examined": 0,
            "has_more": False,
            "next_cursor": None,
            "consistency": "live",
            "order": "newest_registered_first",
        }
        if json_size(page) > max_output_chars:
            raise EvidenceError("output_budget", "来源目录页元数据超过输出预算")
        # Project only complete, bounded identity fields. Never fetch content or
        # checkpoint JSON; state filtering consumes this finite rowid window too.
        text_fields = ",".join(
            f"CASE WHEN typeof(s.{name})='text' AND length(CAST(s.{name} AS BLOB))<=? "
            f"THEN CAST(s.{name} AS BLOB) END AS {name},typeof(s.{name}) AS {name}_type"
            for name in _SOURCE_TEXT
        )
        sql = (
            f"SELECT s.rowid AS sequence,{text_fields},"
            "CASE WHEN typeof(s.trusted_owner)='integer' THEN s.trusted_owner END AS trusted_owner,"
            "CASE WHEN typeof(s.created_at) IN ('integer','real') THEN s.created_at END "
            "AS created_at,CASE WHEN typeof(s.content)='text' THEN "
            "length(CAST(s.content AS BLOB)) END AS total_bytes,"
            "CASE WHEN j.source_id IS NULL THEN CAST('deferred' AS BLOB) "
            "WHEN typeof(j.state)='text' AND length(CAST(j.state AS BLOB))<=? "
            "THEN CAST(j.state AS BLOB) END AS job_state,"
            "CASE WHEN j.source_id IS NULL THEN 'text' ELSE typeof(j.state) END AS job_state_type,"
            "CASE WHEN j.source_id IS NULL THEN 0 WHEN typeof(j.attempts)='integer' "
            "THEN j.attempts END AS attempts,"
            "CASE WHEN j.source_id IS NULL THEN CAST('' AS BLOB) "
            "WHEN typeof(j.last_error)='text' AND length(CAST(j.last_error AS BLOB))<=? "
            "THEN CAST(j.last_error AS BLOB) END AS last_error "
            ",CASE WHEN j.source_id IS NULL THEN 'text' ELSE typeof(j.last_error) END "
            "AS last_error_type "
            "FROM memory_sources s LEFT JOIN memory_jobs j ON j.source_id=s.id "
            "WHERE s.rowid>0 AND s.rowid<=? AND NOT EXISTS "
            "(SELECT 1 FROM memory_forgotten_sources f WHERE f.id=s.id) "
            "ORDER BY s.rowid DESC LIMIT ?"
        )
        parameters = [max_output_chars * 4] * (len(_SOURCE_TEXT) + 2) + [before, limit + 1]
        with closing(self.memory.store.db.execute(sql, parameters)) as rows:
            row = rows.fetchone()
            for _ in range(limit):
                if row is None:
                    break
                try:
                    item = (
                        self._source_item(dict(row))
                        if state is None or row["job_state"] == state.encode("ascii")
                        else None
                    )
                except EvidenceError as error:
                    if error.code == "output_budget" and page["examined"]:
                        return page
                    raise
                accepted = item is not None and (state is None or item["job"]["state"] == state)
                following = rows.fetchone()
                candidate = {
                    **page,
                    "items": [*page["items"], item] if accepted else page["items"],
                    "omitted": page["omitted"] + int(not accepted),
                    "examined": page["examined"] + 1,
                    "has_more": following is not None,
                    "next_cursor": (
                        self._encode_source_cursor(row["sequence"], state)
                        if following is not None
                        else None
                    ),
                }
                if json_size(candidate) > max_output_chars:
                    if not page["examined"]:
                        raise EvidenceError("output_budget", "完整来源身份与续页元数据超过输出预算")
                    return page
                page = candidate
                row = following
        return page

    def detail(self, identifier: str) -> dict[str, Any]:
        item = self.memory.get(identifier)
        if item is None:
            raise EvidenceError("evidence_not_found", "记忆不存在或已忘记")
        row = self.memory.store.db.execute(
            f"SELECT {_SOURCE_METADATA} FROM memory_sources s WHERE s.id=?",
            (item["source_id"],),
        ).fetchone()
        origin = dict(row) if row is not None else None
        if origin is not None:
            origin["trusted_owner"] = bool(origin["trusted_owner"])
        return {**item, "origin": origin}

    def read(
        self,
        identifier: str,
        *,
        by_memory: bool = True,
        offset_bytes: int = 0,
        length_bytes: int | None = None,
        revision: str | None = None,
        max_output_chars: int,
    ) -> dict[str, Any]:
        if not isinstance(identifier, str) or not identifier:
            raise EvidenceError("invalid_arguments", "需要有效的记忆或来源 ID")
        validate_integer(offset_bytes, "offset_bytes")
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        if length_bytes is not None:
            validate_integer(length_bytes, "length_bytes", minimum=1)
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise EvidenceError("invalid_arguments", "revision 必须是非空字符串")
        if offset_bytes and revision is None:
            raise EvidenceError("invalid_arguments", "续读必须提供上一页 revision")
        # BLOB lengths/slices retain NUL and use the same byte coordinates as conversation_read.
        join = "JOIN memory_meta m ON m.source_id=s.id" if by_memory else ""
        lookup = "m.memory_id" if by_memory else "s.id"
        row = self.memory.store.db.execute(
            f"SELECT {_SOURCE_METADATA},length(CAST(s.content AS BLOB)) AS total_bytes,"
            f"substr(CAST(s.content AS BLOB),?,?) AS content FROM memory_sources s {join} "
            f"WHERE {lookup}=?",
            (
                offset_bytes + 1,
                min(length_bytes or max_output_chars * 4, max_output_chars * 4),
                identifier,
            ),
        ).fetchone()
        if row is None:
            raise EvidenceError("evidence_not_found", "记忆或原始来源不存在、没有原文或已忘记")
        metadata = dict(row)
        raw, total = metadata.pop("content"), metadata.pop("total_bytes")
        metadata["trusted_owner"] = bool(metadata["trusted_owner"])
        metadata.update(source_id=metadata["id"], revision=metadata["digest"], truncated=False)
        if by_memory:
            metadata["memory_id"] = identifier
        if revision is not None and revision != metadata["revision"]:
            raise EvidenceError("evidence_changed", "来源已变化，请重新核查当前记忆")
        # Source identity is mandatory: do not shorten or invent a principal to fit a page.
        return content_page(
            metadata,
            raw,
            offset_bytes=offset_bytes,
            total_bytes=total,
            max_output_chars=max_output_chars,
        )
