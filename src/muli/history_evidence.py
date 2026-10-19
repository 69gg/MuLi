"""有界读取已持久化正文；分页证据不产生任何新的身份授权。"""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any

from muli.evidence import (
    EvidenceError,
    content_page,
    decode_utf8,
    json_size,
    validate_integer,
)
from muli.store import Store

_UTF8_MAX_BYTES = 4
_SQLITE_MAX_PARAMETER = (1 << 63) - 1
_OPTIONAL_METADATA = ("source", "principal")


def _archive_sequence(value: int, name: str, *, minimum: int = 0) -> None:
    # Byte offsets reserve one integer for +1; these sequence predicates do not.
    if type(value) is int and value == _SQLITE_MAX_PARAMETER:
        return
    validate_integer(value, name, minimum=minimum)


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
            revision = decode_utf8(row["evidence_revision"], allow_incomplete_tail=False)
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
            value = decode_utf8(row[field], allow_incomplete_tail=False)
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
                if json_size(candidate) < json_size(value):
                    value.clear()
                    value.update(candidate)
                    return True
        return False

    def _preview(self, row: dict[str, Any], budget: int) -> dict[str, Any]:
        record = self._metadata(row, budget)
        if row["content_type"] == "text":
            record["content"] = decode_utf8(row["content"], allow_incomplete_tail=True)
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
        return record

    def _fit_preview(self, result: dict[str, Any], record: dict[str, Any], budget: int) -> bool:
        """Fit the current record while accounting for the caller's complete envelope."""
        while json_size(result) > budget and self._omit_optional(record):
            pass
        content = record["content"]
        if json_size(result) > budget and isinstance(content, str):
            record["content"] = ""
            record["content_truncated"] = bool(record["content_bytes"])
        if json_size(result) > budget:
            return False
        if isinstance(content, str) and record["content"] != content:
            low, high = 0, len(content)
            while low < high:
                middle = (low + high + 1) // 2
                record["content"] = content[:middle]
                record["content_truncated"] = (
                    len(record["content"].encode("utf-8")) < record["content_bytes"]
                )
                if json_size(result) <= budget:
                    low = middle
                else:
                    high = middle - 1
            record["content"] = content[:low]
            record["content_truncated"] = (
                len(record["content"].encode("utf-8")) < record["content_bytes"]
            )
        return True

    def read(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 20,
        max_output_chars: int,
    ) -> dict[str, Any]:
        validate_integer(after, "after")
        validate_integer(limit, "limit", minimum=1)
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        self._session(session_id)
        result: dict[str, Any] = {
            "session_id": session_id,
            "records": [],
            "next_after": after,
            "has_more": False,
        }
        if json_size(result) > max_output_chars:
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
                record = self._preview(row, max_output_chars)
                result["records"].append(record)
                previous = result["next_after"]
                result["next_after"] = record["seq"]
                # false is one JSON character longer than true; reserve the EOF form.
                result["has_more"] = False
                if not self._fit_preview(result, record, max_output_chars):
                    result["records"].pop()
                    result["next_after"] = previous
                    result["has_more"] = True
                    if not result["records"]:
                        raise EvidenceError(
                            "output_budget", "预算不足以返回一条消息的完整定位元数据"
                        )
                    break
            else:
                result["has_more"] = False
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "目录页无法在工具输出预算内完整返回")
        return result

    def archive(
        self,
        session_id: str,
        *,
        before_seq: int | None = None,
        through_seq: int | None = None,
        limit: int = 50,
        max_output_chars: int,
    ) -> dict[str, Any]:
        """Page older persisted content independently from live messages and event replay."""
        if before_seq is not None:
            _archive_sequence(before_seq, "before_seq", minimum=1)
        if through_seq is not None:
            _archive_sequence(through_seq, "through_seq")
        validate_integer(limit, "limit", minimum=1)
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        self._session(session_id)
        if through_seq is None:
            through_seq = self.store.message_evidence_max_seq(session_id)
        result: dict[str, Any] = {
            "session_id": session_id,
            "messages": [],
            "through_seq": through_seq,
            "next_before_seq": None,
            "has_older": False,
        }
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "归档页元数据超过输出预算")
        preview_bytes = min(max_output_chars * _UTF8_MAX_BYTES, _SQLITE_MAX_PARAMETER)
        rows = self.store.message_evidence_archive(
            session_id,
            before_seq=before_seq,
            through_seq=through_seq,
            limit=min(limit, max_output_chars) + 1,
            preview_bytes=preview_bytes,
            metadata_bytes=preview_bytes,
        )
        with closing(rows):
            row = next(rows, None)
            while row is not None:
                # A single bounded lookahead determines the actual EOF envelope.
                following = next(rows, None)
                record = self._preview(row, max_output_chars)
                result["messages"].append(record)
                result["has_older"] = following is not None
                result["next_before_seq"] = record["seq"] if following is not None else None
                if not self._fit_preview(result, record, max_output_chars):
                    result["messages"].pop()
                    if not result["messages"]:
                        raise EvidenceError(
                            "output_budget", "预算不足以返回一条消息的完整定位元数据"
                        )
                    result["has_older"] = True
                    result["next_before_seq"] = result["messages"][-1]["seq"]
                    break
                if len(result["messages"]) >= limit:
                    break
                row = following
        result["messages"].reverse()
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "归档页无法在输出预算内完整返回")
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
        validate_integer(offset_bytes, "offset_bytes")
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        if (message_id is None) == (seq is None) or (
            message_id is not None and (not isinstance(message_id, str) or not message_id)
        ):
            raise EvidenceError(
                "invalid_arguments", "message_id 和 seq 必须且只能提供一个有效定位值"
            )
        if seq is not None:
            validate_integer(seq, "seq", minimum=1)
        if length_bytes is not None:
            validate_integer(length_bytes, "length_bytes", minimum=1)
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
        result.update(session_id=session_id, message_id=result.pop("id"))
        return content_page(
            result,
            row["content"],
            offset_bytes=offset_bytes,
            total_bytes=row["content_bytes"],
            max_output_chars=max_output_chars,
            omit_optional=self._omit_optional,
        )
