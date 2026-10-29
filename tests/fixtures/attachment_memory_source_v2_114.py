"""Validated attachment provenance within the existing external memory format."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any

from muli.attachments import ATTACHMENT_ID_PATTERN
from muli.document_reading import document_revision

MAX_ATTACHMENT_SOURCE_BYTES = 1000
_DIGEST = re.compile(r"[a-f0-9]{64}")
_SCOPES = {"pdf": "pdf_text_layer", "docx": "docx_main_body_text"}
_DOCUMENT_FIELDS = {"format", "scope", "source_revision", "revision"}


def attachment_source(
    identifier: str,
    content: str,
    checksum: str,
    created_at: int | float,
    *,
    document: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Keep one stable source ID; bind a document's full text to its read view."""
    if not isinstance(identifier, str) or re.fullmatch(ATTACHMENT_ID_PATTERN, identifier) is None:
        raise ValueError("附件来源 ID 无效")
    if not isinstance(content, str) or not content.strip() or "\x00" in content:
        raise ValueError("附件来源必须包含完整有效正文")
    raw = content.encode("utf-8", errors="strict")
    if not isinstance(checksum, str) or _DIGEST.fullmatch(checksum) is None:
        raise ValueError("附件原始摘要无效")
    if type(created_at) not in {int, float} or created_at < 0:
        raise ValueError("附件来源时间无效")
    try:
        if not math.isfinite(created_at):
            raise ValueError("附件来源时间无效")
    except OverflowError as error:
        raise ValueError("附件来源时间无效") from error
    source_id = "attachment:" + identifier
    if document is None:
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise ValueError("附件正文与原始摘要不一致")
        marker = source_id + ":" + checksum
    else:
        if set(document) != _DOCUMENT_FIELDS or any(
            not isinstance(value, str) for value in document.values()
        ):
            raise ValueError("文档来源元数据不完整")
        format_name = document["format"]
        if (
            format_name not in _SCOPES
            or document["scope"] != _SCOPES[format_name]
            or document["source_revision"] != checksum
            or _DIGEST.fullmatch(document["revision"]) is None
            or document_revision(checksum, raw) != document["revision"]
        ):
            raise ValueError("文档来源正文、范围或版本不一致")
        marker = ":".join(
            (
                source_id,
                "document",
                "v1",
                format_name,
                document["scope"],
                checksum,
                document["revision"],
            )
        )
    if len(marker.encode("utf-8")) > MAX_ATTACHMENT_SOURCE_BYTES:
        raise ValueError("附件来源标记超过核验预算")
    return {
        "id": source_id,
        "content": content,
        "source": marker,
        "session_id": "",
        "role": "tool",
        "principal": "external:attachment",
        "created_at": created_at,
    }


def restore_attachment_source(
    identifier: str, content: str, marker: str, created_at: int | float
) -> dict[str, Any]:
    """Validate a saved source without reopening an attachment or re-extracting it."""
    prefix = "attachment:" + identifier + ":"
    if (
        not isinstance(marker, str)
        or len(marker.encode("utf-8")) > MAX_ATTACHMENT_SOURCE_BYTES
        or not marker.startswith(prefix)
    ):
        raise ValueError("已收藏附件来源身份或标记无效")
    value = marker[len(prefix) :]
    if _DIGEST.fullmatch(value):
        return attachment_source(identifier, content, value, created_at)
    parts = value.split(":")
    if len(parts) != 6 or parts[:2] != ["document", "v1"]:
        raise ValueError("已收藏文档来源格式不受支持")
    _, _, format_name, scope, checksum, revision = parts
    return attachment_source(
        identifier,
        content,
        checksum,
        created_at,
        document={
            "format": format_name,
            "scope": scope,
            "source_revision": checksum,
            "revision": revision,
        },
    )
