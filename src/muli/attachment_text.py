"""Explicit revision-bound pages for ordinary UTF-8 attachments."""

from __future__ import annotations

import re
from typing import Any

from muli.attachment_io import read_verified_window
from muli.attachments import Attachments, file_io
from muli.evidence import EvidenceError, content_page, validate_integer
from muli.transcripts import _owned


async def read_attachment_text(
    attachments: Attachments,
    identifier: str,
    *,
    revision: str | None,
    offset_bytes: int,
) -> dict[str, Any]:
    """Keep ordinary text authority and limits separate from media archives."""
    validate_integer(offset_bytes, "offset_bytes")
    if (offset_bytes and revision is None) or (
        revision is not None
        and (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision))
    ):
        raise EvidenceError("invalid_arguments", "续读必须携带上一页的 revision")
    item = attachments.get(identifier)
    expected, size = item.get("sha256"), item.get("size")
    if (
        item.get("source") in {"asr", "vision"}
        or not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or type(size) is not int
        or not 0 <= size <= attachments.config.settings.limits.max_file_bytes
    ):
        raise EvidenceError("attachment_unavailable", "不是可读取的有界普通文本附件")
    if revision is not None and revision != expected:
        raise EvidenceError("revision_changed", "附件版本不匹配，请重新读取首页")
    if offset_bytes > size:
        raise EvidenceError("invalid_offset", "offset_bytes 超过附件末尾")
    window = attachments.config.settings.limits.max_output_chars * 4

    try:
        path = attachments.path(identifier)

        def read_page() -> bytes:
            return read_verified_window(
                path,
                expected=expected,
                size=size,
                offset_bytes=offset_bytes,
                window=window,
                prefix="attachment",
                label="附件",
                validate_text=True,
            )

        raw = await _owned(file_io(read_page))
    except OSError as exc:
        raise EvidenceError("attachment_unavailable", "附件文件不可用") from exc
    if (
        item != attachments.get(identifier)
        or size > attachments.config.settings.limits.max_file_bytes
    ):
        raise EvidenceError("attachment_changed", "附件元数据或读取上限已变化")
    return content_page(
        {"id": identifier, "revision": expected, "truncated": False},
        raw,
        offset_bytes=offset_bytes,
        total_bytes=size,
        max_output_chars=attachments.config.settings.limits.max_output_chars,
    )
