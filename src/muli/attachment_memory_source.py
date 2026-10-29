"""Validated attachment provenance within the existing external memory format."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any, TypedDict

from muli.attachments import ATTACHMENT_ID_PATTERN
from muli.document_reading import document_revision

MAX_ATTACHMENT_SOURCE_BYTES = 1000
TRANSCRIPT_SCOPE = "asr_transcript_text"
TRANSCRIPT_NOTICE = "ASR 转写文本；仅核验已保存转写，不证明原音频、说话者或转写准确性。"
_DIGEST = re.compile(r"[a-f0-9]{64}")
_SCOPES = {"pdf": "pdf_text_layer", "docx": "docx_main_body_text"}
_DOCUMENT_FIELDS = {"format", "scope", "source_revision", "revision"}


class AttachmentSourceMetadata(TypedDict):
    format: str
    scope: str
    source_revision: str
    document_revision: str | None


def attachment_source(
    identifier: str,
    content: str,
    checksum: str,
    created_at: int | float,
    *,
    document: Mapping[str, str] | None = None,
    transcript: bool = False,
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
    if type(transcript) is not bool or (transcript and document is not None):
        raise ValueError("转写与文档来源范围不能混用")
    if document is None:
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise ValueError("附件正文与原始摘要不一致")
        marker = source_id + (":transcript:v1:asr_text:" if transcript else ":") + checksum
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


def attachment_source_metadata(identifier: str, marker: str) -> AttachmentSourceMetadata:
    """Decode only known source scopes; body authenticity is checked by restore."""
    prefix = "attachment:" + identifier + ":"
    if (
        not isinstance(identifier, str)
        or re.fullmatch(ATTACHMENT_ID_PATTERN, identifier) is None
        or not isinstance(marker, str)
        or len(marker.encode("utf-8")) > MAX_ATTACHMENT_SOURCE_BYTES
        or not marker.startswith(prefix)
    ):
        raise ValueError("已收藏附件来源身份或标记无效")
    value = marker[len(prefix) :]
    if _DIGEST.fullmatch(value):
        return {
            "format": "text",
            "scope": "utf8_text",
            "source_revision": value,
            "document_revision": None,
        }
    parts = value.split(":")
    if len(parts) == 4 and parts[:3] == ["transcript", "v1", "asr_text"]:
        if _DIGEST.fullmatch(parts[3]) is None:
            raise ValueError("已收藏转写摘要无效")
        return {
            "format": "asr",
            "scope": TRANSCRIPT_SCOPE,
            "source_revision": parts[3],
            "document_revision": None,
        }
    if (
        len(parts) != 6
        or parts[:2] != ["document", "v1"]
        or parts[2] not in _SCOPES
        or parts[3] != _SCOPES[parts[2]]
        or any(_DIGEST.fullmatch(value) is None for value in parts[4:])
    ):
        raise ValueError("已收藏附件来源格式不受支持")
    _, _, format_name, scope, checksum, revision = parts
    return {
        "format": format_name,
        "scope": scope,
        "source_revision": checksum,
        "document_revision": revision,
    }


def restore_attachment_source(
    identifier: str, content: str, marker: str, created_at: int | float
) -> dict[str, Any]:
    """Validate a saved source without reopening an attachment or re-extracting it."""
    metadata = attachment_source_metadata(identifier, marker)
    revision = metadata["document_revision"]
    if revision is None:
        return attachment_source(
            identifier,
            content,
            metadata["source_revision"],
            created_at,
            transcript=metadata["format"] == "asr",
        )
    return attachment_source(
        identifier,
        content,
        metadata["source_revision"],
        created_at,
        document={
            "format": metadata["format"],
            "scope": metadata["scope"],
            "source_revision": metadata["source_revision"],
            "revision": revision,
        },
    )
