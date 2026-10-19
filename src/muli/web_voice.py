"""Explicit browser audio transcription produces drafts, never conversation input."""

from __future__ import annotations

import asyncio
from typing import Any

from muli.attachments import SAFE_INLINE
from muli.config import WebVoiceSettings
from muli.local_services import LocalServices


class WebVoiceError(ValueError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class WebVoiceInput:
    def __init__(self, local: LocalServices) -> None:
        self.local, self.runtime = local, local.runtime
        self._operations: set[asyncio.Task[dict[str, Any]]] = set()
        self._closed = False

    def status(self) -> dict[str, Any]:
        settings = self.runtime.config.settings
        policy = settings.web_voice
        return {
            **policy.model_dump(),
            "configured": settings.media.asr.configured,
            "available": policy.enabled and settings.media.asr.configured and not self._closed,
            "active": len(self._operations),
            "max_file_bytes": min(
                policy.max_file_bytes,
                settings.uploads.max_file_bytes,
                settings.media.asr.max_input_bytes,
            ),
        }

    async def transcribe(self, identifier: str) -> dict[str, Any]:
        async with self.runtime.configuration_lock:
            settings = self.runtime.config.settings
            policy = settings.web_voice.model_copy(deep=True)
            if self._closed or not policy.enabled:
                raise WebVoiceError("网页语音输入未启用", 403)
            if not settings.media.asr.configured:
                raise WebVoiceError("请先配置 ASR 模型服务", 409)
            if len(self._operations) >= policy.max_concurrent:
                raise WebVoiceError("转写任务已达并发上限，请稍后重试", 429)
            item = self.local.attachments.get(identifier)
            if item["mime_type"] not in SAFE_INLINE or not item["mime_type"].startswith("audio/"):
                raise WebVoiceError("网页转写需要音频附件", 415)
            limit = int(self.status()["max_file_bytes"])
            if item["size"] <= 0 or item["size"] > limit:
                raise WebVoiceError("音频为空或超过网页转写大小限制", 413)
            task = asyncio.create_task(
                self._transcribe(identifier, limit, policy),
                name="muli-web-transcription",
            )
            self._operations.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise WebVoiceError("转写因服务关闭或配置变更取消", 409) from None
        finally:
            self._operations.discard(task)

    async def _transcribe(
        self, identifier: str, limit: int, policy: WebVoiceSettings
    ) -> dict[str, Any]:
        async with asyncio.timeout(policy.timeout):
            item, data = await self.local.read_bytes(identifier, limit)
            async with self.local.media() as media:
                text = await media.transcribe(data, item["name"], item["mime_type"])
            if not text.strip():
                raise WebVoiceError("没有识别到文字，请重试或手动输入", 422)
            if len(text) > policy.max_text_chars:
                raise WebVoiceError("转写文字超过草稿上限，请拆分录音", 413)
            return {"text": text, "attachment_id": identifier, "draft": True}

    async def aclose(self) -> None:
        self._closed = True
        tasks = tuple(self._operations)
        for task in tasks:
            task.cancel()
        cleanup = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        self._operations.difference_update(tasks)
        if cancelled:
            raise asyncio.CancelledError

    async def reconfigure(self) -> None:
        await self.aclose()
        self._closed = False
