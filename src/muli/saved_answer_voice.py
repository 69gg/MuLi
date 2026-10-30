"""Explicit owner actions synthesize one proven, complete saved main answer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from muli.config import Settings
from muli.evidence import EvidenceError
from muli.local_services import LocalServices
from muli.providers import ProviderError
from muli.reply_origin import ReplyOrigin
from muli.transcripts import _owned


class SavedAnswerVoiceError(ValueError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class SavedAnswerVoice:
    def __init__(self, local: LocalServices, *, closing: Callable[[], bool]) -> None:
        self.local, self.runtime, self.closing = local, local.runtime, closing
        self._operations: dict[tuple[str, str, str, str], asyncio.Task[Any]] = {}
        self._epoch = 0
        self._closed = False

    @property
    def idle(self) -> bool:
        return not self._operations

    @staticmethod
    def _policy_values(settings: Settings) -> dict[str, Any]:
        # Settings sections can be changed in place. Retain only in-memory values;
        # the resolved key is compared privately and never logged or persisted.
        return {
            "tts": settings.media.tts.model_dump(),
            "resolved_key": settings.media.tts.key(),
            "limits": settings.limits.model_dump(),
            "uploads": settings.uploads.model_dump(),
            "disabled_tools": tuple(settings.disabled_tools),
        }

    def _admit(self) -> None:
        runtime = self.runtime
        if self._closed or self.closing() or runtime._closing:
            raise SavedAnswerVoiceError("语音生成因服务关闭或配置变化失效")
        if not runtime.maintenance.admission.allowed or runtime.maintenance.configuration_blocked:
            raise SavedAnswerVoiceError("维护期间不能生成语音")
        settings = runtime.config.settings
        if "speech_synthesize" in settings.disabled_tools:
            raise SavedAnswerVoiceError("语音合成已禁用", 403)
        if not settings.media.tts.configured or not settings.media.tts.voice.strip():
            raise SavedAnswerVoiceError("请先配置 TTS 服务和音色")

    def _source(self, key: tuple[str, str, str, str], settings: Settings) -> str:
        session_id, run_id, message_id, revision = key
        store = self.runtime.store
        if any(not value or len(value) > 200 for value in key):
            raise SavedAnswerVoiceError("答复定位需要准确的有界身份和版本", 422)
        if session_id != store.existing_main_session_id():
            raise SavedAnswerVoiceError("只能为当前主会话的完整最终答复生成语音")
        limits = settings.limits
        budget = max(limits.max_context_chars, limits.max_output_chars) * 4
        try:
            session = store.bounded_object(
                "session", session_id, max_bytes=budget, strict_json=True
            )
            run = store.bounded_object("run", run_id, max_bytes=budget, strict_json=True)
            message = store.bounded_object(
                "message", message_id, max_bytes=budget, strict_json=True
            )
        except EvidenceError as error:
            raise SavedAnswerVoiceError("完整答复证明无法在核查预算内读取", 413) from error
        if session is None or session.get("kind") != "main" or run is None or message is None:
            raise SavedAnswerVoiceError("完整最终答复的持久证明已不可用")
        content = message.get("content")
        provenance = message.get("content_provenance")
        origin = ReplyOrigin.load(run.get("reply_origin"))
        if (
            run.get("session_id") != session_id
            or run.get("status") != "completed"
            or run.get("result_message_id") != message_id
            or message.get("session_id") != session_id
            or message.get("role") != "assistant"
            or message.get("tool_calls")
            or message.get("interrupted")
            or message.get("content_truncated")
            or message.get("truncated")
            or (
                provenance is not None
                and (not isinstance(provenance, str) or provenance not in {"unknown", "accepted"})
            )
            or not isinstance(content, str)
            or not content.strip()
            or run.get("result") != content
            or not origin.input_id
            or ReplyOrigin.load(message.get("reply_origin")) != origin
        ):
            raise SavedAnswerVoiceError("此消息没有准确完整的最终答复证明")
        if len(content) > limits.max_output_chars:
            raise SavedAnswerVoiceError("完整答复超过语音生成上限，未截取正文", 413)
        actual_revision = message.get("evidence_revision")
        if actual_revision is None:
            row = store.db.execute(
                "SELECT seq FROM message_order WHERE session_id=? AND message_id=?",
                (session_id, message_id),
            ).fetchone()
            if row is None or type(row["seq"]) is not int or row["seq"] < 1:
                raise SavedAnswerVoiceError("历史答复缺少准确的持久序号")
            actual_revision = f"legacy:{store.instance_id}:{row['seq']}"
        if actual_revision != revision:
            raise SavedAnswerVoiceError("答复版本已变化，请重新查看后确认")
        return content

    async def synthesize(
        self, session_id: str, run_id: str, message_id: str, revision: str
    ) -> dict[str, Any]:
        key = session_id, run_id, message_id, revision
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("语音生成缺少请求拥有者")
        # Capture the actual Settings instance and the existing media factory's
        # independent service/profile/key while holding only this short lock.
        async with self.runtime.configuration_lock:
            self._admit()
            settings, epoch = self.runtime.config.settings, self._epoch
            content = self._source(key, settings)
            if key in self._operations:
                raise SavedAnswerVoiceError("该版本正在生成语音，请等待原请求完成")
            if len(self._operations) >= settings.uploads.max_concurrent:
                raise SavedAnswerVoiceError("语音生成已达并发上限，请稍后主动重试", 429)
            policy = self._policy_values(settings)
            media = self.local.media()
            self._operations[key] = owner

        attempted = False

        def guard() -> None:
            try:
                self._admit()
                if (
                    owner.cancelling()
                    or self.runtime.config.settings is not settings
                    or self._policy_values(settings) != policy
                    or self._epoch != epoch
                    or self._operations.get(key) is not owner
                ):
                    raise SavedAnswerVoiceError("本次语音生成已失效")
                if self._source(key, settings) != content:
                    raise SavedAnswerVoiceError("答复正文已变化，请重新查看后确认")
            except SavedAnswerVoiceError as error:
                if attempted:
                    raise SavedAnswerVoiceError(
                        str(error) + "；语音请求可能已计费，请勿自动重新生成", error.status_code
                    ) from error
                raise

        try:
            try:
                async with asyncio.timeout(settings.media.tts.timeout):
                    guard()
                    attempted = True
                    try:
                        result = await media.synthesize(content)
                    except ProviderError as error:
                        raise SavedAnswerVoiceError(
                            "语音服务请求失败或结果未知，可能已计费；请勿自动重新生成", 502
                        ) from error
                    guard()
                    try:
                        attachment = await _owned(
                            self.local.save_speech(result, commit_guard=guard)
                        )
                    except (OSError, ValueError) as error:
                        raise SavedAnswerVoiceError(
                            "TTS 已完成，但附件保存失败或本次操作失效，可能已计费；请勿自动重新生成"
                        ) from error
            except TimeoutError as error:
                raise SavedAnswerVoiceError(
                    "语音生成达到时间上限，执行结果未知且可能已计费；请勿自动重新生成", 504
                ) from error
            finally:
                await _owned(media.aclose())
            # Closing the service may itself await: publish only after its last guard.
            guard()
            return {
                "session_id": session_id,
                "run_id": run_id,
                "message_id": message_id,
                "revision": revision,
                "attachment": attachment,
            }
        finally:
            # A stale owner's cleanup cannot release a newer generation's slot.
            if self._operations.get(key) is owner:
                del self._operations[key]

    def invalidate(self) -> None:
        """Invalidate synchronously; configuration callers must never join under their lock."""
        self._closed = True
        self._epoch += 1
        for task in tuple(self._operations.values()):
            if not task.done() and not task.cancelling():
                task.cancel()

    def reopen(self) -> None:
        self._closed = False

    async def aclose(self) -> None:
        self.invalidate()
        cleanup = asyncio.gather(*tuple(self._operations.values()), return_exceptions=True)
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
