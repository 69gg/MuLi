"""有界读取已持久化正文；分页证据不产生任何新的身份授权。"""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any

from muli.store import Store

_SQLITE_MAX_INTEGER = (1 << 63) - 1
_UTF8_MAX_BYTES = 4
_OPTIONAL_METADATA = ("source", "principal")


class EvidenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < _SQLITE_MAX_INTEGER:
        raise EvidenceError(
            "invalid_arguments",
            f"{name} 必须是有效的非负整数" if not minimum else f"{name} 必须是有效的正整数",
        )


def _size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _decode(raw: bytes, *, allow_incomplete_tail: bool) -> str:
    if raw and 0x80 <= raw[0] <= 0xBF:
        raise EvidenceError(
            "invalid_offset", "offset_bytes 位于 UTF-8 字符中间，请使用上一页 next_offset_bytes"
        )
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        if (
            allow_incomplete_tail
            and exc.reason == "unexpected end of data"
            and exc.end == len(raw)
            and len(raw) - exc.start < _UTF8_MAX_BYTES
        ):
            return raw[: exc.start].decode("utf-8", errors="strict")
        raise EvidenceError(
            "archive_encoding_error", "持久正文不是有效 UTF-8，未返回替代或丢失字符"
        ) from exc


class HistoryEvidence:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _session(self, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise EvidenceError("invalid_arguments", "session_id 必须是非空字符串")
        if (
            self.store.db.execute(
                "SELECT 1 FROM objects WHERE kind='session' AND id=?", (session_id,)
            ).fetchone()
            is None
        ):
            raise EvidenceError("evidence_not_found", "会话不存在或已删除")

    def _metadata(self, row: dict[str, Any], budget: int) -> dict[str, Any]:
        revision_type = row["evidence_revision_type"]
        if revision_type in {None, "null"}:
            revision = f"legacy:{self.store.instance_id}:{row['seq']}"
        elif revision_type == "text" and row["evidence_revision_bytes"] == len(
            row["evidence_revision"]
        ):
            revision = _decode(row["evidence_revision"], allow_incomplete_tail=False)
            if not revision:
                raise EvidenceError("content_unavailable", "持久消息的版本号无效")
        else:
            raise EvidenceError("output_budget", "持久消息版本元数据无法在预算内完整返回")
        result: dict[str, Any] = {
            "id": row["id"],
            "seq": row["seq"],
            "revision": revision,
            "role": "unknown",
            "content_provenance": row["content_provenance"],
        }
        omitted: list[str] = []
        for field in ("role", *_OPTIONAL_METADATA):
            if row[field + "_type"] in {None, "null"}:
                continue
            if row[field + "_type"] != "text" or row[field + "_bytes"] != len(row[field]):
                omitted.append(field)
                continue
            value = _decode(row[field], allow_incomplete_tail=False)
            if len(json.dumps(value, ensure_ascii=False)) > budget:
                omitted.append(field)
            else:
                result[field] = value
        if omitted:
            result["metadata_omitted"] = omitted
        return result

    @staticmethod
    def _omit_optional(value: dict[str, Any]) -> bool:
        for field in (*reversed(_OPTIONAL_METADATA), "role"):
            if field in value and (field != "role" or value[field] != "unknown"):
                candidate = {**value, "metadata_omitted": list(value.get("metadata_omitted", []))}
                if field == "role":
                    candidate[field] = "unknown"
                else:
                    del candidate[field]
                if field not in candidate["metadata_omitted"]:
                    candidate["metadata_omitted"].append(field)
                if _size(candidate) < _size(value):
                    value.clear()
                    value.update(candidate)
                    return True
        return False

    def read(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 20,
        max_output_chars: int,
    ) -> dict[str, Any]:
        _integer(after, "after")
        _integer(limit, "limit", minimum=1)
        _integer(max_output_chars, "max_output_chars", minimum=1)
        self._session(session_id)
        result: dict[str, Any] = {
            "session_id": session_id,
            "records": [],
            "next_after": after,
            "has_more": False,
        }
        if _size(result) > max_output_chars:
            raise EvidenceError("output_budget", "目录元数据超过工具输出预算")
        # One bounded row is assembled at a time; SQL does not materialize all previews in Python.
        previews = max_output_chars * _UTF8_MAX_BYTES
        rows = self.store.message_evidence_directory(
            session_id,
            after=after,
            limit=min(limit, max_output_chars) + 1,
            preview_bytes=previews,
            metadata_bytes=max_output_chars * _UTF8_MAX_BYTES,
        )
        with closing(rows):
            for row in rows:
                if len(result["records"]) >= limit:
                    result["has_more"] = True
                    break
                record = self._metadata(row, max_output_chars)
                if row["content_type"] == "text":
                    record["content"] = _decode(row["content"], allow_incomplete_tail=True)
                    record["content_bytes"] = row["content_bytes"]
                    record["content_truncated"] = (
                        len(record["content"].encode("utf-8")) < row["content_bytes"]
                    )
                else:
                    record.update(
                        content=None,
                        content_bytes=None,
                        content_truncated=False,
                        content_unavailable=True,
                    )
                result["records"].append(record)
                previous = result["next_after"]
                result["next_after"] = record["seq"]
                # false is one JSON character longer than true; reserve the EOF form.
                result["has_more"] = False
                while _size(result) > max_output_chars and self._omit_optional(record):
                    pass
                content = record["content"]
                if _size(result) > max_output_chars and isinstance(content, str):
                    record["content"] = ""
                    record["content_truncated"] = bool(row["content_bytes"])
                if _size(result) > max_output_chars:
                    result["records"].pop()
                    result["next_after"] = previous
                    result["has_more"] = True
                    if not result["records"]:
                        raise EvidenceError(
                            "output_budget", "预算不足以返回一条消息的完整定位元数据"
                        )
                    break
                if isinstance(content, str) and record["content"] != content:
                    low, high = 0, len(content)
                    while low < high:
                        middle = (low + high + 1) // 2
                        record["content"] = content[:middle]
                        record["content_truncated"] = (
                            len(record["content"].encode("utf-8")) < row["content_bytes"]
                        )
                        if _size(result) <= max_output_chars:
                            low = middle
                        else:
                            high = middle - 1
                    record["content"] = content[:low]
                    record["content_truncated"] = (
                        len(record["content"].encode("utf-8")) < row["content_bytes"]
                    )
            else:
                result["has_more"] = False
        if _size(result) > max_output_chars:
            raise EvidenceError("output_budget", "目录页无法在工具输出预算内完整返回")
        return result

    def read_content(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        seq: int | None = None,
        offset_bytes: int = 0,
        length_bytes: int | None = None,
        revision: str | None = None,
        max_output_chars: int,
    ) -> dict[str, Any]:
        _integer(offset_bytes, "offset_bytes")
        _integer(max_output_chars, "max_output_chars", minimum=1)
        if (message_id is None) == (seq is None) or (
            message_id is not None and (not isinstance(message_id, str) or not message_id)
        ):
            raise EvidenceError(
                "invalid_arguments", "message_id 和 seq 必须且只能提供一个有效定位值"
            )
        if seq is not None:
            _integer(seq, "seq", minimum=1)
        if length_bytes is not None:
            _integer(length_bytes, "length_bytes", minimum=1)
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise EvidenceError("invalid_arguments", "revision 必须是非空字符串")
        if offset_bytes and revision is None:
            raise EvidenceError("invalid_arguments", "续读必须提供上一页 revision")
        self._session(session_id)
        row = self.store.message_evidence_slice(
            session_id,
            message_id=message_id,
            seq=seq,
            offset_bytes=offset_bytes,
            length_bytes=min(
                length_bytes or max_output_chars * _UTF8_MAX_BYTES,
                max_output_chars * _UTF8_MAX_BYTES,
            ),
            metadata_bytes=max_output_chars * _UTF8_MAX_BYTES,
        )
        if row is None:
            raise EvidenceError("evidence_not_found", "消息不存在、已删除或不属于此会话")
        result = self._metadata(row, max_output_chars)
        if revision is not None and revision != result["revision"]:
            raise EvidenceError(
                "evidence_changed", "消息已更新；请从 offset_bytes=0 重新读取当前版本"
            )
        if row["content_type"] != "text":
            raise EvidenceError("content_unavailable", "此消息没有可读取的字符串 content 字段")
        if offset_bytes > row["content_bytes"]:
            raise EvidenceError("invalid_offset", "offset_bytes 超过持久正文末尾")
        text = _decode(row["content"], allow_incomplete_tail=True)
        result.update(
            session_id=session_id,
            message_id=result.pop("id"),
            field="content",
            offset_bytes=offset_bytes,
            length_bytes=0,
            next_offset_bytes=offset_bytes,
            total_bytes=row["content_bytes"],
            has_more=offset_bytes < row["content_bytes"],
            content="",
        )

        def select(count: int) -> None:
            content = text[:count]
            size = len(content.encode("utf-8"))
            result.update(
                content=content,
                length_bytes=size,
                next_offset_bytes=offset_bytes + size,
                has_more=offset_bytes + size < row["content_bytes"],
            )

        select(min(1, len(text)))
        while _size(result) > max_output_chars and self._omit_optional(result):
            pass
        if _size(result) > max_output_chars:
            raise EvidenceError("output_budget", "正文页定位元数据超过工具输出预算")
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            select(middle)
            if _size(result) <= max_output_chars:
                low = middle
            else:
                high = middle - 1
        select(low)
        if not low and result["has_more"]:
            raise EvidenceError("output_budget", "预算不足以返回一个完整字符和续读元数据")
        if _size(result) > max_output_chars:
            raise EvidenceError("output_budget", "正文页无法在工具输出预算内完整返回")
        return result
