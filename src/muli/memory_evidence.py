"""Read memory provenance and bounded source windows without loading the whole source."""

from __future__ import annotations

from typing import Any

from muli.evidence import EvidenceError, content_page, json_size, validate_integer
from muli.memory import Memory

_SOURCE_METADATA = (
    "s.id,s.source,s.session_id,s.role,s.principal,s.trusted_owner,s.digest,s.created_at"
)


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
