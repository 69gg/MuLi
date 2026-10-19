"""Read-only, bounded title directory over all persisted session records."""

from __future__ import annotations

import base64
import binascii
import json
import math
from contextlib import closing
from typing import Any

from muli.evidence import EvidenceError, decode_utf8, json_size, validate_integer
from muli.store import Store

_CURSOR_VERSION = 1
_MAX_QUERY_CHARS = 120
_MAX_PAGE_ITEMS = 100
_SQLITE_MAX_INTEGER = (1 << 63) - 1
_UTF8_MAX_BYTES = 4


class SessionRecords:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _cursor(self, token: str | None, query: str) -> tuple[int | float, str] | None:
        if token is None:
            return None
        if not isinstance(token, str) or not token:
            raise EvidenceError("record_cursor_invalid", "记录目录游标无效；请从首页重新读取")
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
            if not isinstance(payload, list) or len(payload) != 5:
                raise ValueError
            version, instance, original_query, created, identifier = payload
            if (
                type(version) is not int
                or version != _CURSOR_VERSION
                or instance != self.store.instance_id
                or original_query != query
                or type(created) not in {int, float}
                or created < 0
                or (type(created) is int and created > _SQLITE_MAX_INTEGER)
                or not math.isfinite(created)
                or not isinstance(identifier, str)
                or not identifier
            ):
                raise ValueError
            identifier.encode("utf-8", errors="strict")
            return created, identifier
        except (
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
            binascii.Error,
        ) as exc:
            raise EvidenceError(
                "record_cursor_invalid",
                "记录目录游标无效、查询不匹配或来自另一资料库；请从首页重新读取",
            ) from exc

    def _encode(self, query: str, created: int | float, identifier: str) -> str:
        payload = [_CURSOR_VERSION, self.store.instance_id, query, created, identifier]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _text(row: dict[str, Any], field: str) -> str:
        raw = row[field]
        if not isinstance(raw, bytes):
            raise EvidenceError("record_metadata_invalid", "持久记录的文本元数据无效")
        if len(raw) != row[field + "_bytes"]:
            raise EvidenceError("output_budget", "记录目录预算不足以完整返回 ID、标题或状态")
        try:
            return decode_utf8(raw, allow_incomplete_tail=False)
        except EvidenceError as exc:
            raise EvidenceError(
                "record_metadata_invalid", "持久记录的文本元数据不是有效 UTF-8"
            ) from exc

    def _item(self, row: dict[str, Any]) -> dict[str, Any]:
        identifier = self._text(row, "id")
        if not identifier:
            raise EvidenceError("record_metadata_invalid", "持久记录没有可定位的 ID")
        item: dict[str, Any] = {
            "id": identifier,
            "title": None,
            "status": None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for field in ("title", "status"):
            if row[field + "_type"] in {None, "null"}:
                continue
            if row[field + "_type"] != "text":
                raise EvidenceError("record_metadata_invalid", "持久记录的标题或状态不是文本")
            item[field] = self._text(row, field)
        return item

    def read(
        self,
        *,
        query: str = "",
        cursor: str | None = None,
        limit: int = 50,
        max_output_chars: int,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > _MAX_QUERY_CHARS:
            raise EvidenceError(
                "record_query_invalid", "标题查询必须是最多 120 个 Unicode 字符的字符串"
            )
        try:
            query.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise EvidenceError("record_query_invalid", "标题查询包含无效 Unicode 字符") from exc
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_ITEMS:
            raise EvidenceError("record_limit_invalid", "记录目录每页数量必须是 1–100 的整数")
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        after = self._cursor(cursor, query)
        result: dict[str, Any] = {"items": [], "next_cursor": None, "has_more": False}
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "记录目录页元数据超过输出预算")
        rows = self.store.session_record_rows(
            query=query,
            after=after,
            limit=limit + 1,
            metadata_bytes=max_output_chars * _UTF8_MAX_BYTES,
        )
        with closing(rows):
            row = next(rows, None)
            while row is not None:
                following = next(rows, None)
                try:
                    item = self._item(row)
                except EvidenceError as exc:
                    if exc.code == "output_budget" and result["items"]:
                        break
                    raise
                has_more = following is not None
                candidate: dict[str, Any] = {
                    "items": [*result["items"], item],
                    "next_cursor": self._encode(query, row["sort_created_at"], item["id"])
                    if has_more
                    else None,
                    "has_more": has_more,
                }
                if json_size(candidate) > max_output_chars:
                    if not result["items"]:
                        raise EvidenceError(
                            "output_budget", "预算不足以返回第一条完整记录摘要及真实续页游标"
                        )
                    break
                result = candidate
                if len(result["items"]) == limit:
                    break
                row = following
        if json_size(result) > max_output_chars:
            raise EvidenceError("output_budget", "记录目录页无法在输出预算内完整返回")
        return result
