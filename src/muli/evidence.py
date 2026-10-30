"""Shared UTF-8 windows and complete JSON budgets for persisted evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

_SQLITE_MAX_INTEGER = (1 << 63) - 1
_UTF8_MAX_BYTES = 4
MessageEvidenceField = Literal["content", "tool_calls"]


class EvidenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def validate_integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < _SQLITE_MAX_INTEGER:
        raise EvidenceError(
            "invalid_arguments",
            f"{name} 必须是有效的非负整数" if not minimum else f"{name} 必须是有效的正整数",
        )


def json_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def decode_utf8(raw: bytes, *, allow_incomplete_tail: bool) -> str:
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


def content_page(
    metadata: dict[str, Any],
    raw: bytes,
    *,
    offset_bytes: int,
    total_bytes: int,
    max_output_chars: int,
    omit_optional: Callable[[dict[str, Any]], bool] | None = None,
    field: MessageEvidenceField = "content",
) -> dict[str, Any]:
    """Fit one SQL byte window without changing provenance or UTF-8 coordinates."""
    if offset_bytes > total_bytes:
        raise EvidenceError("invalid_offset", "offset_bytes 超过持久正文末尾")
    text = decode_utf8(raw, allow_incomplete_tail=True)
    result = {
        **metadata,
        "field": field,
        "offset_bytes": offset_bytes,
        "total_bytes": total_bytes,
    }

    def select(count: int) -> None:
        content = text[:count]
        size = len(content.encode("utf-8"))
        result.update(
            content=content,
            length_bytes=size,
            next_offset_bytes=offset_bytes + size,
            has_more=offset_bytes + size < total_bytes,
        )
        if "truncated" in result:
            result["truncated"] = bool(offset_bytes) or result["has_more"]

    select(min(1, len(text)))
    while (
        json_size(result) > max_output_chars and omit_optional is not None and omit_optional(result)
    ):
        pass
    if json_size(result) > max_output_chars:
        raise EvidenceError("output_budget", "正文页定位元数据超过输出预算")
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        select(middle)
        if json_size(result) <= max_output_chars:
            low = middle
        else:
            high = middle - 1
    select(low)
    if not low and result["has_more"]:
        raise EvidenceError("output_budget", "预算不足以返回一个完整字符和续读元数据")
    if json_size(result) > max_output_chars:
        raise EvidenceError("output_budget", "正文页无法在输出预算内完整返回")
    return result
