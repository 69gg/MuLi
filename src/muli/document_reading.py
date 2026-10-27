"""Explicit, versioned text views of verified local document attachments."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from muli.attachment_io import read_verified_window
from muli.attachments import Attachments, file_io
from muli.document_settings import DocumentSettings, DocumentSettingsStore
from muli.document_xml import extract_docx
from muli.evidence import EvidenceError, content_page, validate_integer
from muli.process import run_process
from muli.transcripts import _owned


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EvidenceError("document_timeout", "文档读取已超过期限")
    return remaining


async def read_document(
    attachments: Attachments,
    identifier: str,
    *,
    revision: str | None = None,
    offset_bytes: int = 0,
) -> dict[str, Any]:
    validate_integer(offset_bytes, "offset_bytes")
    if (offset_bytes and revision is None) or (
        revision is not None
        and (not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{64}", revision) is None)
    ):
        raise EvidenceError("invalid_arguments", "续读文档必须携带上一页的 revision")
    item = attachments.get(identifier)
    source_revision, size = item.get("sha256"), item.get("size")
    settings = attachments.config.settings
    policy = DocumentSettingsStore(attachments.store)
    documents = policy.read()
    if (
        item.get("source") in {"asr", "vision"}
        or not isinstance(source_revision, str)
        or re.fullmatch(r"[a-f0-9]{64}", source_revision) is None
        or type(size) is not int
        or not 0 <= size <= settings.limits.max_file_bytes
    ):
        raise EvidenceError("document_unavailable", "不是可核验的有界普通文档附件")
    deadline = time.monotonic() + documents.timeout_seconds

    def check_current() -> None:
        current = attachments.config.settings
        if (
            attachments.get(identifier) != item
            or size > current.limits.max_file_bytes
            or policy.read() != documents
        ):
            raise EvidenceError("document_changed", "文档元数据或读取配置已变化")
        _remaining(deadline)

    try:
        path = attachments.path(identifier)

        def read_source() -> bytes:
            return read_verified_window(
                path,
                expected=source_revision,
                size=size,
                offset_bytes=0,
                window=size,
                prefix="document",
                label="文档",
            )

        source = await _owned(file_io(read_source))
    except OSError as error:
        raise EvidenceError("document_unavailable", "文档文件不可用") from error
    check_current()
    if source.startswith(b"%PDF-"):
        format_name, scope = "pdf", "pdf_text_layer"
        extracted = await _read_pdf(source, documents, deadline)
    elif source.startswith(b"PK\x03\x04"):
        format_name, scope = "docx", "docx_main_body_text"

        def read_docx() -> bytes:
            return extract_docx(
                source,
                max_bytes=documents.max_extracted_bytes,
                max_entries=documents.max_archive_entries,
                max_depth=documents.max_xml_depth,
                deadline=deadline,
            )

        extracted = await _owned(file_io(read_docx))
    else:
        raise EvidenceError(
            "document_unsupported", "此工具只读取 PDF 或 DOCX；普通文本请用 attachment_read"
        )
    check_current()
    if len(extracted) > documents.max_extracted_bytes:
        raise EvidenceError("document_budget", "文档提取正文超过限制，未返回节选")
    if b"\x00" in extracted:
        raise EvidenceError("document_encoding", "提取正文包含无效文本，未返回替代字符")
    digest = hashlib.sha256(
        b"muli-document-text-v1\x00" + source_revision.encode("ascii") + b"\x00"
    )
    digest.update(extracted)
    current_revision = digest.hexdigest()
    if revision is not None and revision != current_revision:
        raise EvidenceError("revision_changed", "文档或提取结果版本已变化，请重新读取首页")
    current_limit = attachments.config.settings.limits.max_output_chars
    return content_page(
        {
            "id": identifier,
            "format": format_name,
            "scope": scope,
            "source_revision": source_revision,
            "revision": current_revision,
        },
        extracted[offset_bytes : offset_bytes + current_limit * 4],
        offset_bytes=offset_bytes,
        total_bytes=len(extracted),
        max_output_chars=current_limit,
    )


async def _read_pdf(source: bytes, settings: DocumentSettings, deadline: float) -> bytes:
    remaining = _remaining(deadline)
    try:
        result = await run_process(
            [*settings.pdf_command, "-enc", "UTF-8", "-eol", "unix", "-", "-"],
            input_bytes=source,
            timeout=remaining,
            limit=settings.max_extracted_bytes,
            merge_stderr=False,
            strict_output=True,
        )
    except FileNotFoundError as error:
        raise EvidenceError(
            "document_converter_unavailable",
            "未找到 PDF 转换器，请用 document_configure 配置 pdf_command",
        ) from error
    except TimeoutError as error:
        raise EvidenceError("document_timeout", "PDF 转换已超过期限，进程已收尾") from error
    except UnicodeDecodeError as error:
        raise EvidenceError("document_encoding", "PDF 转换结果不是有效 UTF-8") from error
    except ValueError as error:
        raise EvidenceError("document_budget", "PDF 转换输出超过限制，未返回节选") from error
    except OSError as error:
        raise EvidenceError("document_conversion_failed", "PDF 转换器无法运行") from error
    if result["exit_code"] != 0:
        raise EvidenceError(
            "document_conversion_failed", f"PDF 转换器失败，退出码 {result['exit_code']}"
        )
    output = result["output"]
    if not isinstance(output, str):
        raise EvidenceError("document_encoding", "PDF 转换器未返回文本")
    return output.encode("utf-8")
