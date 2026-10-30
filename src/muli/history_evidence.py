"""有界读取已持久化正文；分页证据不产生任何新的身份授权。"""

from __future__ import annotations

import base64
import binascii
import json
import math
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from muli.attachments import MAX_ATTACHMENT_IDS
from muli.evidence import (
    EvidenceError,
    MessageEvidenceField,
    content_page,
    decode_utf8,
    json_size,
    validate_integer,
)
from muli.management_transaction import _object
from muli.reply_origin import ReplyOrigin
from muli.store import Store

_UTF8_MAX_BYTES = 4
_SQLITE_MAX_PARAMETER = (1 << 63) - 1
_OPTIONAL_METADATA = ("source", "principal")
_CONTINUATION_CURSOR_VERSION = 1
_CONTINUATION_FILTER = "agents_parent_unfinished"


@dataclass(frozen=True, slots=True)
class CompletedReplyEvidence:
    message_id: str
    session_id: str
    seq: int
    message_revision: str
    content: str
    created_at: int | float
    producing_run_id: str
    original_input_id: str


def _reply_origin(value: object) -> ReplyOrigin:
    origin = ReplyOrigin.load(value)
    if origin.input_id is None or not isinstance(value, dict) or value != origin.dump():
        raise EvidenceError("content_unavailable", "答复缺少完整规范的原始输入来源")
    return origin


def _archive_sequence(value: int, name: str, *, minimum: int = 0) -> None:
    # Byte offsets reserve one integer for +1; these sequence predicates do not.
    if type(value) is int and value == _SQLITE_MAX_PARAMETER:
        return
    validate_integer(value, name, minimum=minimum)


class HistoryEvidence:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _reply_owner_input(
        self,
        session_id: str,
        input_id: str,
        *,
        max_bytes: int,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Point-prove one exact delivered owner input and its complete archive."""
        row = self.store.db.execute(
            "SELECT i.seq,i.delivered_seq,i.received,"
            "CASE WHEN typeof(i.payload)='text' AND length(CAST(i.payload AS BLOB))<=? "
            "THEN CAST(i.payload AS BLOB) END AS payload,"
            "CASE WHEN typeof(i.run_id)='text' AND length(CAST(i.run_id AS BLOB))<=? "
            "THEN CAST(i.run_id AS BLOB) END AS run_id "
            "FROM ingress i JOIN message_order m ON m.message_id=i.id "
            "AND m.session_id=i.session_id AND m.seq=i.delivered_seq "
            "WHERE i.id=? AND i.session_id=? AND i.state='delivered'",
            (max_bytes, max_bytes, input_id, session_id),
        ).fetchone()
        if row is None:
            raise EvidenceError("content_unavailable", "原始主人输入的精确投递记录不可用")
        if not isinstance(row["payload"], bytes) or not isinstance(row["run_id"], bytes):
            raise EvidenceError("output_budget", "完整原始输入超过核查字节预算")
        try:
            payload = _object(row["payload"])
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            producing_run = row["run_id"].decode("utf-8", errors="strict")
            origin = ReplyOrigin.from_input(payload)
            if (
                payload.get("id") != input_id
                or payload.get("session_id") != session_id
                or payload.get("principal") != "owner"
                or not isinstance(payload.get("content"), str)
                or not isinstance(payload.get("source"), str)
                or not producing_run
                or (run_id is not None and producing_run != run_id)
                or origin.input_id != input_id
                or ("reply_origin" in payload and _reply_origin(payload["reply_origin"]) != origin)
                or type(row["seq"]) is not int
                or type(row["delivered_seq"]) is not int
                or type(row["received"]) not in {int, float}
                or not math.isfinite(row["received"])
            ):
                raise ValueError
            archive = self.store.bounded_object(
                "message", input_id, max_bytes=max_bytes, strict_json=True
            )
            projected = self.store.input_archive(
                payload,
                received=row["received"],
                run_id=producing_run,
                input_seq=row["seq"],
            )
            if (
                archive is None
                or archive.get("seq") != row["delivered_seq"]
                or any(archive.get(key) != value for key, value in projected.items())
                or _reply_origin(archive.get("reply_origin")) != origin
            ):
                raise ValueError
            return archive
        except EvidenceError:
            raise
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as error:
            raise EvidenceError("content_unavailable", "原始主人输入与投递存档不一致") from error

    def _completed_reply(
        self,
        session_id: str,
        message_id: str,
        revision: str,
        *,
        current_run_id: str,
        before_seq: int,
        max_context_chars: int,
        max_source_chars: int,
    ) -> CompletedReplyEvidence:
        """Authenticate one complete final reply; never enumerate past messages or runs."""
        budget = max_context_chars * _UTF8_MAX_BYTES
        # JSON projection itself parses the object. Gate the complete raw bytes
        # first, including non-content fields, before entering any slice query.
        message = self.store.bounded_object(
            "message", message_id, max_bytes=budget, strict_json=True
        )
        if message is None:
            raise EvidenceError("content_unavailable", "原答复不存在或不属于当前主会话")
        row = self.store.message_evidence_slice(
            session_id,
            message_id=message_id,
            seq=None,
            offset_bytes=0,
            length_bytes=max_source_chars * _UTF8_MAX_BYTES,
            metadata_bytes=budget,
        )
        if row is None:
            raise EvidenceError("content_unavailable", "原答复不存在或不属于当前主会话")
        metadata = self._metadata(row, max_context_chars)
        if metadata["revision"] != revision:
            raise EvidenceError("evidence_changed", "原答复版本已变化，请重新读取核对")
        if (
            row["content_type"] != "text"
            or type(row["seq"]) is not int
            or not 0 < row["seq"] < before_seq
        ):
            raise EvidenceError("content_unavailable", "只能收藏本次主人输入之前的完整答复")
        if row["content_bytes"] > max_source_chars * _UTF8_MAX_BYTES:
            raise EvidenceError("output_budget", "完整答复超过来源字节预算，未保存节选")
        content = decode_utf8(row["content"], allow_incomplete_tail=False)
        if len(content) > max_source_chars:
            raise EvidenceError("output_budget", "完整答复超过来源字符预算，未保存节选")
        if (
            message.get("session_id") != session_id
            or message.get("seq") != row["seq"]
            or message.get("role") != "assistant"
            or message.get("content") != content
            or not content.strip()
            or message.get("interrupted", False) is not False
            or message.get("tool_calls", []) != []
        ):
            raise EvidenceError("content_unavailable", "目标不是可完整核查的主助手最终答复")
        origin = _reply_origin(message.get("reply_origin"))
        assert origin.input_id is not None
        original = self._reply_owner_input(session_id, origin.input_id, max_bytes=budget)
        producing_run_id = original["run_id"]
        run = self.store.bounded_object("run", producing_run_id, max_bytes=budget, strict_json=True)
        if (
            producing_run_id == current_run_id
            or original["seq"] >= row["seq"]
            or _reply_origin(original.get("reply_origin")) != origin
            or run is None
            or run.get("session_id") != session_id
            or run.get("status") != "completed"
            or run.get("role") != "main"
            or run.get("result_message_id") != message_id
            or not isinstance(run.get("result"), str)
            or run["result"] != content
            or _reply_origin(run.get("reply_origin")) != origin
        ):
            raise EvidenceError("content_unavailable", "完整答复与已完成主运行的最终结果不一致")
        timestamp = message.get("created_at")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise EvidenceError("content_unavailable", "原答复缺少有限的持久时间")
        try:
            finite = math.isfinite(timestamp)
        except OverflowError as error:
            raise EvidenceError("content_unavailable", "原答复缺少有限的持久时间") from error
        if not finite:
            raise EvidenceError("content_unavailable", "原答复缺少有限的持久时间")
        return CompletedReplyEvidence(
            message_id,
            session_id,
            row["seq"],
            metadata["revision"],
            content,
            timestamp,
            producing_run_id,
            origin.input_id,
        )

    def _continuation_cursor(self, token: str | None, main_id: str, max_output_chars: int) -> int:
        if token is None:
            return 0
        if not isinstance(token, str) or not token:
            raise EvidenceError("record_cursor_invalid", "延续目录游标无效；请从首页重新读取")
        if len(token) > max_output_chars:
            raise EvidenceError("output_budget", "延续目录游标超过当前工具输出预算")
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
            if not isinstance(payload, list) or len(payload) != 6:
                raise ValueError
            version, section, instance, parent, filters, rowid = payload
            if (
                type(version) is not int
                or version != _CONTINUATION_CURSOR_VERSION
                or section != "continuations"
                or instance != self.store.instance_id
                or parent != main_id
                or filters != _CONTINUATION_FILTER
                or type(rowid) is not int
                or not 1 <= rowid <= _SQLITE_MAX_PARAMETER
            ):
                raise ValueError
            return rowid
        except (ValueError, TypeError, RecursionError, binascii.Error) as exc:
            raise EvidenceError(
                "record_cursor_invalid", "延续目录游标无效或不属于当前资料库和主会话"
            ) from exc

    def _encode_continuation_cursor(self, main_id: str, rowid: int) -> str:
        payload = [
            _CONTINUATION_CURSOR_VERSION,
            "continuations",
            self.store.instance_id,
            main_id,
            _CONTINUATION_FILTER,
            rowid,
        ]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _continuation_text(row: dict[str, Any], field: str) -> str | None:
        if row[field + "_type"] != "text":
            return None
        raw = row[field]
        if not isinstance(raw, bytes) or len(raw) != row[field + "_bytes"]:
            raise EvidenceError("output_budget", "延续目录无法在预算内核查完整身份元数据")
        try:
            return decode_utf8(raw, allow_incomplete_tail=False) or None
        except EvidenceError:
            return None

    def _continuation_item(self, row: dict[str, Any]) -> dict[str, Any] | None:
        # SQL classifies status only after selecting the bounded parent window.
        # Completed and malformed statuses consume a candidate without disclosure.
        if not isinstance(row["status"], bytes):
            return None
        identifier = self._continuation_text(row, "agent_key")
        stored_id = self._continuation_text(row, "agent_id")
        session_id = self._continuation_text(row, "session_id")
        if identifier is None or stored_id != identifier or session_id is None:
            return None
        status = row["status"].decode("ascii")
        run_id = self._continuation_text(row, "run_id")
        associated = run_id is not None and row["run_associated"] == 1
        item: dict[str, Any] = {
            "kind": "agent",
            "id": identifier,
            "session_id": session_id,
            "status": status,
            "run_id": run_id if associated and status != "queued" else None,
            "availability": (
                "historical"
                if associated and status == "queued"
                else "available"
                if associated
                else "unavailable"
            ),
        }
        for field in ("generation", "completion_revision"):
            if row[field] is not None:
                item[field] = row[field]
        commitment_id = self._continuation_text(row, "commitment_id")
        if commitment_id is not None:
            item["commitment_id"] = commitment_id
        return item

    def continuations(
        self,
        main_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
    ) -> dict[str, Any]:
        """Observe a finite direct-child window; never resume or infer effects."""
        validate_integer(limit, "limit", minimum=1)
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        after = self._continuation_cursor(cursor, main_id, max_output_chars)
        result: dict[str, Any] = {
            "items": [],
            "omitted": 0,
            "next_cursor": None,
            "has_more": False,
            "consistency": "live",
            "side_effect_status": "unknown",
        }
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "延续目录页元数据超过工具输出预算")
        rows = self.store.continuation_rows(
            main_id,
            after_rowid=after,
            limit=limit + 1,
            metadata_bytes=max_output_chars * _UTF8_MAX_BYTES,
        )
        with closing(rows):
            row = next(rows, None)
            consumed = 0
            while row is not None and consumed < limit:
                following = next(rows, None)
                item = self._continuation_item(row)
                if item is None:
                    result["omitted"] += 1
                else:
                    result["items"].append(item)
                consumed += 1
                result["has_more"] = following is not None
                result["next_cursor"] = (
                    self._encode_continuation_cursor(main_id, row["rowid"])
                    if following is not None
                    else None
                )
                # Include the actual cursor/envelope. A legal item is never
                # dropped or relabelled unavailable merely to fit the page.
                if json_size(result) > max_output_chars:
                    raise EvidenceError("output_budget", "延续目录页无法在输出预算内完整返回")
                row = following
        return result

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
            "attachment_ids": self._attachment_ids(row),
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
    def _attachment_ids(row: dict[str, Any]) -> list[str]:
        # An absent field predates structured handoff. Malformed present data must
        # not masquerade as an empty or partially recovered reference list.
        if row["attachment_ids_type"] is None:
            return []
        if row["attachment_ids_type"] != "array":
            raise EvidenceError("content_unavailable", "持久消息的附件引用格式无效")
        raw = row["attachment_ids"]
        if row["attachment_ids_bytes"] != len(raw):
            raise EvidenceError("output_budget", "附件引用无法在预算内完整返回")
        try:
            identifiers = json.loads(decode_utf8(raw, allow_incomplete_tail=False))
        except (ValueError, TypeError, RecursionError) as exc:
            raise EvidenceError("content_unavailable", "持久消息的附件引用格式无效") from exc
        # Preserve the original generic envelope contract, including opaque and
        # repeated legacy references. Only new local Team handoffs require hex IDs.
        if (
            not isinstance(identifiers, list)
            or len(identifiers) > MAX_ATTACHMENT_IDS
            or any(not isinstance(item, str) or not item or len(item) > 256 for item in identifiers)
        ):
            raise EvidenceError("content_unavailable", "持久消息的附件引用格式无效")
        try:
            for identifier in identifiers:
                identifier.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise EvidenceError("content_unavailable", "持久消息的附件引用格式无效") from exc
        return identifiers

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
        """Keep the original content-only API, output and validation order."""
        return self.read_message(
            session_id,
            message_id=message_id,
            seq=seq,
            offset_bytes=offset_bytes,
            length_bytes=length_bytes,
            revision=revision,
            max_output_chars=max_output_chars,
        )

    def read_message(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        seq: int | None = None,
        offset_bytes: int = 0,
        length_bytes: int | None = None,
        revision: str | None = None,
        field: MessageEvidenceField = "content",
        deadline: float | None = None,
        max_output_chars: int,
    ) -> dict[str, Any]:
        """Page one saved field; reading tool arguments never resumes a call."""
        if field not in ("content", "tool_calls"):
            raise EvidenceError("invalid_arguments", "field 仅支持 content 或 tool_calls")
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
        window_bytes = max_output_chars * _UTF8_MAX_BYTES
        if field == "tool_calls":
            window_bytes = min(window_bytes, _SQLITE_MAX_PARAMETER)
        row = self.store.message_evidence_slice(
            session_id,
            message_id=message_id,
            seq=seq,
            offset_bytes=offset_bytes,
            length_bytes=min(length_bytes or window_bytes, window_bytes),
            metadata_bytes=window_bytes,
            field=field,
            deadline=deadline,
        )
        if row is None:
            raise EvidenceError("evidence_not_found", "消息不存在、已删除或不属于此会话")
        result = self._metadata(row, max_output_chars)
        if revision is not None and revision != result["revision"]:
            raise EvidenceError(
                "evidence_changed", "消息已更新；请从 offset_bytes=0 重新读取当前版本"
            )
        if field == "content" and row["content_type"] != "text":
            raise EvidenceError("content_unavailable", "此消息没有可读取的字符串 content 字段")
        if field == "tool_calls":
            if row["content_type"] is None:
                raise EvidenceError(
                    "field_missing", "此消息未保存 tool_calls 字段，不能据此确认原调用为空"
                )
            if row["content_type"] != "array":
                raise EvidenceError("content_unavailable", "此消息的 tool_calls 字段不是有效数组")
            # content_provenance describes saved result text. It says nothing
            # about argument completeness or any external operation's outcome.
            result.pop("content_provenance")
            result["side_effect_status"] = "unknown"
        result.update(session_id=session_id, message_id=result.pop("id"))
        return content_page(
            result,
            row["content"],
            offset_bytes=offset_bytes,
            total_bytes=row["content_bytes"],
            max_output_chars=max_output_chars,
            omit_optional=self._omit_optional,
            field=field,
        )
