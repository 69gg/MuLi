"""Literal positions in the existing, fully verified attachment reading views."""

from __future__ import annotations

import re
import stat
from time import monotonic
from typing import Any, Literal

from muli.attachment_io import _version, read_verified_window
from muli.attachment_records import _MAX_PAGE_ITEMS, _MAX_QUERY_CHARS
from muli.attachments import ATTACHMENT_ID_PATTERN, Attachments, attachment_name, file_io
from muli.config import Limits
from muli.document_reading import document_view
from muli.document_settings import DocumentSettingsStore
from muli.evidence import EvidenceError, decode_utf8, json_size, validate_integer
from muli.transcripts import _owned

type AttachmentReader = Literal["attachment_read", "document_read"]


async def find_attachment(
    attachments: Attachments,
    identifier: str,
    *,
    reader: AttachmentReader,
    query: str,
    revision: str | None = None,
    offset_bytes: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Locate exact UTF-8 matches without creating a reading page or a collection."""
    validate_integer(offset_bytes, "offset_bytes")
    validate_integer(limit, "limit", minimum=1)
    if (
        not isinstance(identifier, str)
        or re.fullmatch(ATTACHMENT_ID_PATTERN, identifier) is None
        or reader not in ("attachment_read", "document_read")
        or limit > _MAX_PAGE_ITEMS
    ):
        raise EvidenceError("invalid_arguments", "附件 ID、reader 或 limit 无效")
    if (offset_bytes and revision is None) or (
        revision is not None
        and (not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{64}", revision) is None)
    ):
        raise EvidenceError("invalid_arguments", "续查必须携带所选阅读工具的完整 revision")
    if not isinstance(query, str) or not 1 <= len(query) <= _MAX_QUERY_CHARS or "\x00" in query:
        raise EvidenceError(
            "invalid_arguments", f"query 必须是 1–{_MAX_QUERY_CHARS} 个字符的非 NUL 字面串"
        )
    try:
        needle = query.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise EvidenceError("invalid_arguments", "query 包含无效 Unicode 字符") from error

    prefix = "document" if reader == "document_read" else "attachment"
    try:
        item = attachments.get(identifier)
    except KeyError as error:
        raise EvidenceError(f"{prefix}_unavailable", "附件元数据不可用") from error
    expected, size = item.get("sha256"), item.get("size")
    if (
        item.get("id") != identifier
        or item.get("source") in ("asr", "vision")
        or not isinstance(expected, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected) is None
        or type(size) is not int
        or not 0 <= size <= attachments.config.settings.limits.max_file_bytes
    ):
        raise EvidenceError(f"{prefix}_unavailable", "不是可核验的有界普通附件")

    policy = DocumentSettingsStore(attachments.store) if reader == "document_read" else None
    documents = policy.read() if policy is not None else None
    try:
        path = attachments.path(identifier)
        before = path.lstat()
    except (KeyError, OSError) as error:
        raise EvidenceError(f"{prefix}_unavailable", "附件文件不可用") from error
    if not stat.S_ISREG(before.st_mode) or before.st_size != size:
        raise EvidenceError(f"{prefix}_changed", "附件源文件已变化")
    identity = _version(before)

    def check_source_identity() -> None:
        try:
            current = path.lstat()
        except OSError as error:
            raise EvidenceError(f"{prefix}_changed", "附件源文件已变化") from error
        if not stat.S_ISREG(current.st_mode) or _version(current) != identity:
            raise EvidenceError(f"{prefix}_changed", "附件源文件身份已变化")

    def current_limits() -> Limits:
        current = attachments.config.settings.limits
        try:
            unchanged = attachments.get(identifier) == item
        except KeyError:
            unchanged = False
        if (
            not unchanged
            or size > current.max_file_bytes
            or (policy is not None and policy.read() != documents)
        ):
            raise EvidenceError(f"{prefix}_changed", "附件元数据或读取配置已变化")
        return current

    if reader == "document_read":
        view = await document_view(attachments, identifier, revision=revision)
        current_limits()
        check_source_identity()
        if view.source_revision != expected:
            raise EvidenceError("document_changed", "文档源版本已变化")
        body = view.content
        metadata: dict[str, Any] = {"id": identifier, "reader": reader, **view.metadata()}
    else:
        if revision is not None and revision != expected:
            raise EvidenceError("revision_changed", "附件版本不匹配，请从头查找")

        def read_source() -> bytes:
            return read_verified_window(
                path,
                expected=expected,
                size=size,
                offset_bytes=0,
                window=size,
                prefix="attachment",
                label="附件",
                validate_text=True,
            )

        try:
            body = await _owned(file_io(read_source))
        except OSError as error:
            raise EvidenceError("attachment_unavailable", "附件文件不可用") from error
        current_limits()
        check_source_identity()
        metadata = {
            "id": identifier,
            "reader": reader,
            "source_revision": expected,
            "revision": expected,
        }

    limits = current_limits()
    started = monotonic()
    deadline = started + limits.max_search_seconds

    def check_deadline() -> None:
        # Tightening settings cannot extend the search that is already in progress.
        current_deadline = started + attachments.config.settings.limits.max_search_seconds
        if monotonic() >= min(deadline, current_deadline):
            raise EvidenceError("attachment_find_timeout", "附件正文查找已超过期限，未返回部分结果")

    check_deadline()
    if offset_bytes > len(body):
        raise EvidenceError("invalid_offset", "offset_bytes 超过正文末尾")
    decode_utf8(body[offset_bytes : offset_bytes + 4], allow_incomplete_tail=True)
    cap = min(limit, limits.max_search_results)
    step = len(query[0].encode("utf-8"))
    starts: list[int] = []
    position = offset_bytes
    for _ in range(cap + 1):
        check_deadline()
        found = body.find(needle, position)
        check_deadline()
        if found < 0:
            break
        starts.append(found)
        position = found + step

    limits = current_limits()
    cap = min(cap, limits.max_search_results)
    metadata.update(query=query, offset_bytes=offset_bytes, total_bytes=len(body))
    name = item.get("name")
    if isinstance(name, str):
        try:
            if attachment_name(name) == name:
                metadata["name"] = name
        except ValueError:
            pass

    def page(count: int) -> dict[str, Any]:
        return {
            **metadata,
            "items": [
                {
                    "offset_bytes": start,
                    "length_bytes": len(needle),
                    "next_offset_bytes": start + len(needle),
                    "read_arguments": {
                        "id": identifier,
                        "revision": metadata["revision"],
                        "offset_bytes": start,
                    },
                }
                for start in starts[:count]
            ],
            "has_more": len(starts) > count,
            "next_offset_bytes": starts[count] if len(starts) > count else len(body),
        }

    minimum = 1 if starts else 0
    if json_size(page(minimum)) > limits.max_output_chars:
        metadata.pop("name", None)
    if json_size(page(minimum)) > limits.max_output_chars:
        raise EvidenceError("output_budget", "最小完整定位结果超过当前工具输出预算")
    low, high = minimum, min(cap, len(starts))
    while low < high:
        check_deadline()
        middle = (low + high + 1) // 2
        if json_size(page(middle)) <= limits.max_output_chars:
            low = middle
        else:
            high = middle - 1
    result = page(low)
    current_limits()
    check_source_identity()
    check_deadline()
    return result
