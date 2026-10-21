"""Bounded, metadata-only search over the persisted attachment directory."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from contextlib import closing
from typing import Any

from muli.evidence import EvidenceError, decode_utf8, json_size, validate_integer
from muli.store import Store

_CURSOR_VERSION = 1
_MAX_CURSOR_CHARS = 512
_MAX_QUERY_CHARS = 120
_MAX_PAGE_ITEMS = 100
_MAX_SCANNED = 200
_UTF8_MAX_BYTES = 4
_ITEM_FIELDS = ("id", "name", "source", "mime_type", "size", "sha256", "created_at")


class AttachmentRecords:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _query(value: str | None, field: str) -> None:
        if value is None and field == "source":
            return
        if not isinstance(value, str) or len(value) > _MAX_QUERY_CHARS:
            raise EvidenceError(
                "attachment_query_invalid", "来源与文件名查询必须是最多 120 个 Unicode 字符的字符串"
            )
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise EvidenceError(
                "attachment_query_invalid", "附件查询包含无效 Unicode 字符"
            ) from exc

    def _cursor(self, token: str | None, filters: str) -> str | None:
        if token is None:
            return None
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= _MAX_CURSOR_CHARS:
                raise ValueError
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
            if not isinstance(payload, list) or len(payload) != 4:
                raise ValueError
            version, instance, original_filters, identifier = payload
            if (
                type(version) is not int
                or version != _CURSOR_VERSION
                or instance != self.store.instance_id
                or original_filters != filters
                or not isinstance(identifier, str)
                or re.fullmatch(r"[a-f0-9]{32}", identifier) is None
            ):
                raise ValueError
            return identifier
        except (ValueError, TypeError, RecursionError, binascii.Error) as exc:
            raise EvidenceError(
                "attachment_cursor_invalid",
                "附件目录游标无效、筛选不匹配或来自另一资料库；请从首页重新读取",
            ) from exc

    def _encode(self, filters: str, identifier: str) -> str:
        payload = [_CURSOR_VERSION, self.store.instance_id, filters, identifier]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _item(row: dict[str, Any]) -> dict[str, Any]:
        raw = row["metadata"]
        if not isinstance(raw, bytes) or len(raw) != row["metadata_bytes"]:
            raise EvidenceError("output_budget", "附件目录预算不足以完整检查这一条元数据")
        try:
            item = json.loads(decode_utf8(raw, allow_incomplete_tail=False))
            if not isinstance(item, dict):
                raise ValueError
            identifier = item.get("id")
            if (
                not isinstance(identifier, str)
                or re.fullmatch(r"[a-f0-9]{32}", identifier) is None
                or row["id_bytes"] != 32
                or row["id"] != identifier.encode("ascii")
                or not all(
                    isinstance(item.get(key), str) for key in ("name", "source", "mime_type")
                )
                or not item["name"]
                or len(item["name"].encode("utf-8")) > 240
                or type(item.get("size")) is not int
                or not 0 <= item["size"] < (1 << 63) - 1
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None
                or type(item.get("created_at")) not in {int, float}
                or item["created_at"] < 0
                or not math.isfinite(item["created_at"])
            ):
                raise ValueError
            result = {key: item[key] for key in _ITEM_FIELDS}
            # JSON escapes can encode invalid surrogate characters in otherwise
            # valid UTF-8 bytes. Do not pass these to the eventual tool envelope.
            json.dumps(result, ensure_ascii=False).encode("utf-8", errors="strict")
            return {**result, "url": f"/api/uploads/{identifier}/download"}
        except (ValueError, TypeError, OverflowError, RecursionError, EvidenceError) as exc:
            raise EvidenceError("attachment_metadata_invalid", "持久附件的目录元数据无效") from exc

    def read(
        self,
        *,
        source: str | None = None,
        name_query: str = "",
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
    ) -> dict[str, Any]:
        self._query(source, "source")
        self._query(name_query, "name_query")
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_ITEMS:
            raise EvidenceError("attachment_limit_invalid", "附件目录每页数量必须是 1–100 的整数")
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        filters = hashlib.sha256(
            json.dumps([source, name_query], ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        before_id = self._cursor(cursor, filters)
        result: dict[str, Any] = {"items": [], "next_cursor": None, "has_more": False, "scanned": 0}
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "附件目录页元数据超过输出预算")
        rows = self.store.attachment_record_rows(
            before_id=before_id,
            limit=_MAX_SCANNED + 1,
            metadata_bytes=max_output_chars * _UTF8_MAX_BYTES,
        )
        with closing(rows):
            row = next(rows, None)
            while row is not None and result["scanned"] < _MAX_SCANNED:
                following = next(rows, None)
                try:
                    item = self._item(row)
                except EvidenceError as exc:
                    if exc.code == "output_budget" and result["items"]:
                        break
                    raise
                matches = (source is None or item["source"] == source) and name_query in item[
                    "name"
                ]
                has_more = following is not None
                candidate = {
                    "items": [*result["items"], item] if matches else result["items"],
                    "next_cursor": self._encode(filters, item["id"]) if has_more else None,
                    "has_more": has_more,
                    "scanned": result["scanned"] + 1,
                }
                if json_size(candidate) > max_output_chars:
                    if not result["items"]:
                        raise EvidenceError(
                            "output_budget", "预算不足以完整返回附件摘要、扫描位置与真实续页游标"
                        )
                    break
                result = candidate
                if len(result["items"]) == limit:
                    break
                row = following
        return result
