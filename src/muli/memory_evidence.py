"""Read memory provenance and bounded source windows without loading the whole source."""

from __future__ import annotations

from typing import Any

from muli.evidence import EvidenceError, content_page, validate_integer
from muli.memory import Memory

_SOURCE_METADATA = (
    "s.id,s.source,s.session_id,s.role,s.principal,s.trusted_owner,s.digest,s.created_at"
)


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
