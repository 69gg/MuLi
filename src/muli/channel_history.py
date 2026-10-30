"""Bounded local channel inputs and actual persisted outgoing deliveries."""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import json
import math
from collections.abc import Generator
from contextlib import closing
from typing import Any

from muli.channels.types import MAX_OUTGOING_TEXT_CHARS, NAMES
from muli.evidence import EvidenceError, content_page, decode_utf8, json_size, validate_integer
from muli.history_evidence import HistoryEvidence
from muli.store import SQLITE_MAX_INTEGER, Store

_CURSOR_VERSION = 2
CHANNEL_HISTORY_PAGE_LIMIT = 100
_UTF8_MAX_BYTES = 4
_DELIVERY_CHUNK_BYTES = 64 * 1024
_DELIVERY_STATES = {
    "pending",
    "retry",
    "sending",
    "sent",
    "failed",
    "blocked",
    "unknown",
    "cancelled",
}


class ChannelHistory:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.evidence = HistoryEvidence(store)

    @staticmethod
    def _scope(name: str, account: str, owner: str, main: str) -> tuple[str, str, str, str]:
        scope = (name, account, owner, main)
        if (
            any(not isinstance(value, str) or not value or "\x00" in value for value in scope)
            or name not in NAMES
        ):
            raise EvidenceError("invalid_arguments", "渠道历史需要准确渠道、账号、主人与主会话")
        try:
            for value in scope:
                value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise EvidenceError("invalid_arguments", "渠道历史范围包含无效 Unicode 字符") from exc
        return scope

    @staticmethod
    def _header(scope: tuple[str, str, str, str]) -> dict[str, Any]:
        name, account, owner, main = scope
        return {
            "source": "local",
            "coverage": "saved_by_muli",
            "consistency": "live",
            "channel": name,
            "account_id": account,
            "owner_id": owner,
            "session_id": main,
        }

    def _cursor(
        self,
        token: str | None,
        scope: tuple[str, str, str, str],
        direction: str,
        max_output_chars: int,
    ) -> int | None:
        if token is None:
            return None
        if not isinstance(token, str) or not token:
            raise EvidenceError("record_cursor_invalid", "渠道历史游标无效；请从首页重新读取")
        if len(token) > max_output_chars:
            raise EvidenceError("output_budget", "渠道历史游标超过当前工具输出预算")
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
            if not isinstance(payload, list) or len(payload) != 9:
                raise ValueError
            version, section, instance, main, name, account, owner, stream, before = payload
            if (
                type(version) is not int
                or version != _CURSOR_VERSION
                or section != "channel_history"
                or instance != self.store.instance_id
                or (name, account, owner, main) != scope
                or stream != direction
                or type(before) is not int
                or not 1 <= before <= SQLITE_MAX_INTEGER
            ):
                raise ValueError
            return before
        except (ValueError, TypeError, RecursionError, binascii.Error) as exc:
            raise EvidenceError(
                "record_cursor_invalid", "游标不属于当前资料库、主会话、私聊或翻页方向"
            ) from exc

    def _encode(self, scope: tuple[str, str, str, str], direction: str, before: int) -> str:
        name, account, owner, main = scope
        payload = [
            _CURSOR_VERSION,
            "channel_history",
            self.store.instance_id,
            main,
            name,
            account,
            owner,
            direction,
            before,
        ]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _created(value: Any) -> bool:
        if type(value) not in {int, float} or value <= 0:
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    @staticmethod
    def _text(row: dict[str, Any], field: str) -> str:
        if row[field + "_type"] != "text":
            raise EvidenceError("content_unavailable", "渠道历史定位元数据格式无效")
        raw = row[field]
        if not isinstance(raw, bytes) or len(raw) != row[field + "_bytes"]:
            raise EvidenceError("output_budget", "渠道历史定位元数据无法在预算内完整核查")
        return decode_utf8(raw, allow_incomplete_tail=False)

    def _incoming(self, row: dict[str, Any], budget: int) -> dict[str, Any] | None:
        if row["channel_role"] != "user" or not self._created(row["created_at"]):
            return None
        identifier = self._text(row, "id")
        if not identifier or "\x00" in identifier:
            return None
        for field in ("source", "principal"):
            self._text(row, field)
        metadata = self.evidence._metadata({**row, "id": identifier}, budget)
        if any(field not in metadata for field in ("source", "principal")):
            raise EvidenceError("output_budget", "渠道历史完整文本元数据超过输出预算")
        result = {
            "id": metadata["id"],
            "seq": metadata["seq"],
            "role": "user",
            "revision": metadata["revision"],
            "attachment_ids": metadata["attachment_ids"],
            "created_at": row["created_at"],
            "source": metadata["source"],
            "principal": metadata["principal"],
            "session_id": row["actual_session_id"],
        }
        # This is the original incoming platform ID, never a send confirmation.
        if row["platform_message_id_type"] == "text":
            result["platform_message_id"] = self._text(row, "platform_message_id")
        return result

    def _delivery_metadata(self, row: dict[str, Any]) -> dict[str, Any]:
        state = self._text(row, "state")
        total, confirmed = row["total_parts"], row["confirmed_parts"]
        if (
            state not in _DELIVERY_STATES
            or row["parts_type"] != "array"
            or type(total) is not int
            or total < 1
            or type(confirmed) is not int
            or not 0 <= confirmed <= total
            or not self._created(row["created_at"])
        ):
            raise EvidenceError("content_unavailable", "渠道投递状态或分段元数据无效")
        return {
            "state": state,
            "confirmed_parts": confirmed,
            "total_parts": total,
            "created_at": row["created_at"],
        }

    @staticmethod
    def _attachment_list(row: dict[str, Any]) -> list[str]:
        # Outbox profiles can permit more than the generic message envelope's
        # attachment count. Keep the real references; the complete page is bounded.
        raw = row["attachment_ids"]
        if not isinstance(raw, bytes) or len(raw) != row["attachment_ids_bytes"]:
            raise EvidenceError("output_budget", "渠道投递附件引用超过完整元数据预算")
        try:
            identifiers = json.loads(decode_utf8(raw, allow_incomplete_tail=False))
        except (ValueError, TypeError, RecursionError) as exc:
            raise EvidenceError("content_unavailable", "渠道投递附件引用无效") from exc
        if not isinstance(identifiers, list) or any(
            not isinstance(item, str) or not item or len(item) > 256 for item in identifiers
        ):
            raise EvidenceError("content_unavailable", "渠道投递附件引用无效")
        try:
            for identifier in identifiers:
                identifier.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise EvidenceError("content_unavailable", "渠道投递附件引用包含无效字符") from exc
        return identifiers

    def _outgoing(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if row["channel_match"] != 1:
            return None
        identifier = self._text(row, "id")
        if not identifier or "\x00" in identifier or row["invalid_parts"]:
            return None
        return {
            "id": identifier,
            "seq": row["seq"],
            "kind": "delivery",
            "role": "assistant",
            **self._delivery_metadata(row),
            "attachment_ids": self._attachment_list(row),
        }

    def _page(
        self,
        rows: Generator[dict[str, Any], None, None],
        scope: tuple[str, str, str, str],
        direction: str,
        limit: int,
        budget: int,
    ) -> dict[str, Any]:
        page: dict[str, Any] = {
            "items": [],
            "omitted": 0,
            "next_cursor": None,
            "has_more": False,
        }
        with closing(rows):
            row = next(rows, None)
            consumed = 0
            while row is not None and consumed < limit:
                following = next(rows, None)
                try:
                    item = (
                        self._incoming(row, budget)
                        if direction == "incoming"
                        else self._outgoing(row)
                    )
                except EvidenceError as exc:
                    if exc.code == "output_budget":
                        raise
                    item = None
                if item is None:
                    page["omitted"] += 1
                else:
                    page["items"].insert(0, item)
                consumed += 1
                page["has_more"] = following is not None
                page["next_cursor"] = (
                    self._encode(scope, direction, row["seq"]) if following is not None else None
                )
                if json_size(page) > budget:
                    raise EvidenceError("output_budget", "渠道历史完整目录及续页游标超过输出预算")
                row = following
        return page

    def read(
        self,
        name: str,
        account_id: str,
        owner_id: str,
        main_id: str,
        *,
        direction: str = "both",
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
    ) -> dict[str, Any]:
        scope = self._scope(name, account_id, owner_id, main_id)
        if (
            not isinstance(direction, str)
            or direction not in {"both", "incoming", "outgoing"}
            or (direction == "both" and cursor is not None)
        ):
            raise EvidenceError("invalid_arguments", "续页请指定 incoming 或 outgoing 及其游标")
        if type(limit) is not int or not 1 <= limit <= CHANNEL_HISTORY_PAGE_LIMIT:
            raise EvidenceError("invalid_arguments", "渠道历史每页候选数量必须是 1–100 的整数")
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        before = self._cursor(cursor, scope, direction, max_output_chars)
        result = {**self._header(scope), "incoming": None, "outgoing": None}
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "渠道历史页定位元数据超过输出预算")
        metadata_bytes = min(max_output_chars * _UTF8_MAX_BYTES, SQLITE_MAX_INTEGER)
        if direction in {"both", "incoming"}:
            result["incoming"] = self._page(
                self.store.channel_history_rows(
                    main_id,
                    name=name,
                    account_id=account_id,
                    owner_id=owner_id,
                    before_seq=before,
                    limit=limit + 1,
                    metadata_bytes=metadata_bytes,
                ),
                scope,
                "incoming",
                limit,
                max_output_chars,
            )
        if direction in {"both", "outgoing"}:
            result["outgoing"] = self._page(
                self.store.channel_delivery_rows(
                    name=name,
                    account_id=account_id,
                    owner_id=owner_id,
                    before_rowid=before,
                    limit=limit + 1,
                    metadata_bytes=metadata_bytes,
                ),
                scope,
                "outgoing",
                limit,
                max_output_chars,
            )
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "渠道历史两流及完整续页游标超过输出预算")
        return result

    def read_delivery(
        self,
        name: str,
        account_id: str,
        owner_id: str,
        main_id: str,
        *,
        delivery_id: str,
        part: int = 0,
        offset_bytes: int = 0,
        revision: str | None = None,
        max_output_chars: int,
    ) -> dict[str, Any]:
        scope = self._scope(name, account_id, owner_id, main_id)
        validate_integer(part, "part")
        validate_integer(offset_bytes, "offset_bytes")
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        if (
            not isinstance(delivery_id, str)
            or not delivery_id
            or "\x00" in delivery_id
            or (revision is not None and (not isinstance(revision, str) or not revision))
        ):
            raise EvidenceError("invalid_arguments", "投递 ID 与正文版本必须是有效的非空字符串")
        if offset_bytes and revision is None:
            raise EvidenceError("invalid_arguments", "续读投递正文必须提供上一页 revision")
        try:
            delivery_id.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise EvidenceError("invalid_arguments", "投递 ID 包含无效 Unicode 字符") from exc
        metadata = {**self._header(scope), "delivery_id": delivery_id, "part": part}
        if json_size(metadata) > max_output_chars:
            raise EvidenceError("output_budget", "投递正文定位元数据超过输出预算")
        rows = self.store.channel_delivery_slices(
            name=name,
            account_id=account_id,
            owner_id=owner_id,
            delivery_id=delivery_id,
            part=part,
            chunk_bytes=_DELIVERY_CHUNK_BYTES,
            source_bytes=MAX_OUTGOING_TEXT_CHARS * _UTF8_MAX_BYTES,
            metadata_bytes=min(max_output_chars * _UTF8_MAX_BYTES, SQLITE_MAX_INTEGER),
        )
        with closing(rows):
            first = next(rows, None)
            if first is None:
                raise EvidenceError("evidence_not_found", "投递不存在或不属于当前账号与主人私聊")
            metadata.update(self._delivery_metadata(first))
            if first["part_type"] != "object" or part >= first["total_parts"]:
                raise EvidenceError("evidence_not_found", "投递没有所选分段")
            text = first["text_type"] == "text" and first["attachment_id_type"] is None
            attachment = first["text_type"] is None and first["attachment_id_type"] == "text"
            if not text and not attachment:
                raise EvidenceError("content_unavailable", "投递分段不是可核验的文本或附件")
            identifiers: list[str] = []
            if attachment:
                identifier = self._text(first, "attachment_id")
                if not identifier or len(identifier) > 256:
                    raise EvidenceError("content_unavailable", "投递附件引用无效")
                identifiers.append(identifier)
            metadata["attachment_ids"] = identifiers
            total = first["total_bytes"]
            if (
                type(total) is not int
                or not 0 <= total <= MAX_OUTGOING_TEXT_CHARS * _UTF8_MAX_BYTES
            ):
                raise EvidenceError("content_unavailable", "投递分段超过原发送正文上限")
            if offset_bytes > total:
                raise EvidenceError("invalid_offset", "offset_bytes 超过投递正文末尾")
            digest = hashlib.sha256(
                json.dumps(
                    [self.store.instance_id, delivery_id, part, total, identifiers],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            window = bytearray()
            window_end = offset_bytes + min(
                max_output_chars * _UTF8_MAX_BYTES, _DELIVERY_CHUNK_BYTES
            )
            observed = 0
            characters = 0
            row: dict[str, Any] | None = first
            while row is not None:
                raw = row["content"]
                if (
                    not isinstance(raw, bytes)
                    or len(raw) > _DELIVERY_CHUNK_BYTES
                    or row["offset_bytes"] != observed
                ):
                    raise EvidenceError("content_unavailable", "投递正文窗口不完整")
                digest.update(raw)
                try:
                    characters += len(decoder.decode(raw, final=observed + len(raw) == total))
                except UnicodeDecodeError as exc:
                    raise EvidenceError("archive_encoding_error", "投递正文不是完整 UTF-8") from exc
                if characters > MAX_OUTGOING_TEXT_CHARS:
                    raise EvidenceError("content_unavailable", "投递分段超过原发送字符上限")
                start, end = max(offset_bytes, observed), min(window_end, observed + len(raw))
                if start < end:
                    window.extend(raw[start - observed : end - observed])
                observed += len(raw)
                row = next(rows, None)
            if observed != total:
                raise EvidenceError("content_unavailable", "投递正文窗口未覆盖完整分段")
        current = "sha256:" + digest.hexdigest()
        if revision is not None and revision != current:
            raise EvidenceError("evidence_changed", "投递正文已更新；请从 offset_bytes=0 重新读取")
        metadata["revision"] = current
        return content_page(
            metadata,
            bytes(window),
            offset_bytes=offset_bytes,
            total_bytes=total,
            max_output_chars=max_output_chars,
        )
