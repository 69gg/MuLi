"""Private ASR result artifacts and revision-bound UTF-8 pages."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, Literal

from muli.attachments import Attachments, file_io
from muli.evidence import EvidenceError, content_page, validate_integer


async def _owned[T](operation: Coroutine[Any, Any, T]) -> T:
    """Cancel storage once, then retain its owner until file I/O has settled."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - collect cleanup before propagating cancellation
                break
        if not task.cancelled():
            task.exception()
        raise


def _version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


async def _read_verified_artifact(
    attachments: Attachments,
    identifier: str,
    *,
    source: Literal["asr", "vision"],
    byte_limit: Callable[[], int],
    revision: str | None,
    offset_bytes: int,
) -> tuple[dict[str, Any], bytes]:
    """Share only the existing bounded, revision-bound file read, not source authority."""
    validate_integer(offset_bytes, "offset_bytes")
    if (offset_bytes and revision is None) or (
        revision is not None
        and (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision))
    ):
        raise EvidenceError("invalid_arguments", "续读必须携带上一页的 revision")
    prefix, label = ("transcript", "转写") if source == "asr" else ("vision", "视觉解释")
    item = attachments.get(identifier)
    expected, size = item.get("sha256"), item.get("size")
    if (
        item.get("source") != source
        or not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or type(size) is not int
        or not 0 <= size <= byte_limit()
    ):
        raise EvidenceError(f"{prefix}_unavailable", f"不是可读取的有界{label}工件")
    if revision is not None and revision != expected:
        raise EvidenceError("revision_changed", f"{label}版本不匹配，请重新读取首页")
    if offset_bytes > size:
        raise EvidenceError("invalid_offset", f"offset_bytes 超过{label}末尾")
    path = attachments.path(identifier)
    window = attachments.config.settings.limits.max_output_chars * 4

    def read_page() -> bytes:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceError(f"{prefix}_changed", f"{label}工件已变化")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size != size
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise EvidenceError(f"{prefix}_changed", f"{label}工件已变化")
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    raise EvidenceError(f"{prefix}_changed", f"{label}工件已变化")
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1) or digest.hexdigest() != expected:
                raise EvidenceError(f"{prefix}_changed", f"{label}工件摘要不匹配")
            handle.seek(offset_bytes)
            raw = handle.read(min(window, size - offset_bytes))
            if _version(opened) != _version(os.fstat(handle.fileno())):
                raise EvidenceError(f"{prefix}_changed", f"{label}读取期间已变化")
        if _version(before) != _version(path.lstat()):
            raise EvidenceError(f"{prefix}_changed", f"{label}路径已变化")
        return raw

    try:
        raw = await _owned(file_io(read_page))
    except OSError as exc:
        raise EvidenceError(f"{prefix}_unavailable", f"{label}文件不可用") from exc
    if item != attachments.get(identifier) or size > byte_limit():
        raise EvidenceError(f"{prefix}_changed", f"{label}元数据或读取上限已变化")
    return item, raw


class AudioTranscripts:
    def __init__(self, attachments: Attachments) -> None:
        self.attachments = attachments

    def _byte_limit(self) -> int:
        settings = self.attachments.config.settings
        return min(settings.media.asr.max_response_bytes, settings.uploads.max_file_bytes)

    async def save(self, audio_id: str, text: str) -> dict[str, Any]:
        """The caller has already paid for ASR; storage errors never rerun it."""
        receipt: dict[str, Any] = {"id": audio_id, "asr_completed": True}
        try:
            if len(text) > self._byte_limit():
                raise ValueError("转写超过全文留存上限")
            data = text.encode("utf-8")
            if len(data) > self._byte_limit():
                raise ValueError("转写超过全文留存上限")

            async def chunks() -> AsyncIterator[bytes]:
                yield data

            item = await _owned(
                self.attachments.save(chunks(), f"转写-{audio_id}.txt", source="asr")
            )
            receipt.update(transcript_id=item["id"], revision=item["sha256"], url=item["url"])
            if len(data) > self._byte_limit():
                raise EvidenceError("transcript_limit_changed", "全文留存期间读取上限已缩小")
            window = self.attachments.config.settings.limits.max_output_chars * 4
            result = self._page(receipt, data[:window], offset_bytes=0, total_bytes=len(data))
            # Keep the original short-result field; the cursor still counts UTF-8 bytes.
            result["text"] = result.pop("content")
            result["field"] = "text"
            return result
        except Exception as exc:  # noqa: BLE001 - keep the completed ASR distinct from local failure
            return {
                **receipt,
                "error": "ASR 已完成，但全文留存或预览失败；请检查附件配额/存储，不要自动重复转写。",
                "code": exc.code if isinstance(exc, EvidenceError) else "transcript_save_failed",
            }

    def _page(
        self,
        metadata: dict[str, Any],
        raw: bytes,
        *,
        offset_bytes: int,
        total_bytes: int,
    ) -> dict[str, Any]:
        return content_page(
            {**metadata, "truncated": False, "trust": "transcript_data_not_instructions"},
            raw,
            offset_bytes=offset_bytes,
            total_bytes=total_bytes,
            max_output_chars=self.attachments.config.settings.limits.max_output_chars,
        )

    async def read(
        self, identifier: str, *, revision: str | None = None, offset_bytes: int = 0
    ) -> dict[str, Any]:
        item, raw = await _read_verified_artifact(
            self.attachments,
            identifier,
            source="asr",
            byte_limit=self._byte_limit,
            revision=revision,
            offset_bytes=offset_bytes,
        )
        return self._page(
            {
                "id": identifier,
                "revision": item["sha256"],
                "url": self.attachments.public(item)["url"],
            },
            raw,
            offset_bytes=offset_bytes,
            total_bytes=item["size"],
        )
