"""Canonical provenance for explicitly collected, complete assistant replies."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, TypedDict, cast

from muli.evidence import validate_integer
from muli.management_transaction import _object
from muli.memory import source_digest

REPLY_SOURCE_PREFIX = "reply:"
MAX_REPLY_SOURCE_ID_CHARS = 200
MAX_REPLY_SOURCE_BYTES = 1000
_MARKER_PREFIX = "reply:v1:"
_FIELDS = {
    "main_session_id",
    "message_id",
    "seq",
    "message_revision",
    "content_sha256",
    "producing_run_id",
    "original_input_id",
}
_DIGEST = re.compile(r"[a-f0-9]{64}")


class ReplySourceMetadata(TypedDict):
    main_session_id: str
    message_id: str
    seq: int
    message_revision: str
    content_sha256: str
    producing_run_id: str
    original_input_id: str


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_REPLY_SOURCE_BYTES:
        raise ValueError("答复来源的完整身份字段无效")
    if len(value.encode("utf-8", errors="strict")) > MAX_REPLY_SOURCE_BYTES:
        raise ValueError("答复来源的完整身份字段超过核查预算")
    return value


def reply_source_id(message_id: str) -> str:
    identifier = REPLY_SOURCE_PREFIX + _text(message_id)
    if len(identifier) > MAX_REPLY_SOURCE_ID_CHARS:
        raise ValueError("答复来源 ID 超过既有来源上限")
    return identifier


def reply_source(
    message_id: str,
    content: str,
    created_at: int | float,
    *,
    main_session_id: str,
    seq: int,
    message_revision: str,
    producing_run_id: str,
    original_input_id: str,
) -> dict[str, Any]:
    """Bind exact UTF-8 text and its authenticated, stable production tuple."""
    identifier = reply_source_id(message_id)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("答复来源需要完整非空原文")
    raw = content.encode("utf-8", errors="strict")
    validate_integer(seq, "seq", minimum=1)
    if type(created_at) not in {int, float}:
        raise ValueError("答复来源时间必须是有限数值")
    try:
        if not math.isfinite(created_at):
            raise ValueError("答复来源时间必须是有限数值")
    except OverflowError as error:
        raise ValueError("答复来源时间必须是有限数值") from error
    metadata: ReplySourceMetadata = {
        "main_session_id": _text(main_session_id),
        "message_id": message_id,
        "seq": seq,
        "message_revision": _text(message_revision),
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "producing_run_id": _text(producing_run_id),
        "original_input_id": _text(original_input_id),
    }
    marker = _MARKER_PREFIX + json.dumps(
        metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if len(marker.encode("utf-8")) > MAX_REPLY_SOURCE_BYTES:
        raise ValueError("答复来源标记超过既有来源核查预算")
    return {
        "id": identifier,
        "content": content,
        "source": marker,
        "session_id": main_session_id,
        "role": "assistant",
        "principal": "agent:" + main_session_id,
        "trusted_owner": False,
        "created_at": created_at,
    }


def reply_source_metadata(marker: str) -> ReplySourceMetadata:
    """Accept only the complete, canonical v1 marker; no tolerant fallback."""
    if (
        not isinstance(marker, str)
        or len(marker) > MAX_REPLY_SOURCE_BYTES
        or len(marker.encode("utf-8", errors="strict")) > MAX_REPLY_SOURCE_BYTES
        or not marker.startswith(_MARKER_PREFIX)
    ):
        raise ValueError("已收藏答复的来源标记无效")
    metadata = _object(marker[len(_MARKER_PREFIX) :].encode("utf-8"))
    if set(metadata) != _FIELDS:
        raise ValueError("已收藏答复的来源字段不完整或含未知字段")
    for field in _FIELDS - {"seq"}:
        _text(metadata[field])
    validate_integer(metadata["seq"], "seq", minimum=1)
    reply_source_id(metadata["message_id"])
    if _DIGEST.fullmatch(metadata["content_sha256"]) is None:
        raise ValueError("已收藏答复的完整原文摘要无效")
    canonical = _MARKER_PREFIX + json.dumps(
        metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if marker != canonical:
        raise ValueError("已收藏答复的来源标记不是规范值")
    return cast(ReplySourceMetadata, metadata)


def restore_reply_source(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Repair only a previously authenticated archive, without reading chat history."""
    metadata = reply_source_metadata(saved["source"])
    source = reply_source(
        metadata["message_id"],
        saved["content"],
        saved["created_at"],
        main_session_id=metadata["main_session_id"],
        seq=metadata["seq"],
        message_revision=metadata["message_revision"],
        producing_run_id=metadata["producing_run_id"],
        original_input_id=metadata["original_input_id"],
    )
    if (
        any(
            source[field] != saved[field]
            for field in ("id", "source", "session_id", "role", "principal")
        )
        or saved["trusted_owner"] is not False
        or source_digest(source, trusted_owner=False) != saved["digest"]
    ):
        raise ValueError("已收藏答复的完整原文、身份或登记版本不一致")
    return source
