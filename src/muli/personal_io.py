"""Compose opt-in desktop and voice capture with the common attachment store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from muli.attachments import Attachments
from muli.capture import Capture
from muli.local_services import LocalServices
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.tools import Tool, parameters
from muli.voice import VoiceWorker

_Result = TypeVar("_Result")


class _ReplyRevoked(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _VoiceReply:
    worker: VoiceWorker
    generation: int
    worker_epoch: int
    runtime_epoch: int
    session_id: str
    input_id: str


class PersonalIO:
    def __init__(self, runtime: Runtime, local: LocalServices, attachments: Attachments) -> None:
        self.runtime, self.local, self.attachments = runtime, local, attachments
        self._generation = 0
        self._closed = False
        self._reply_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._reply_lock = asyncio.Lock()
        self.media = local.media()
        self.capture = Capture(
            runtime.config.settings.computer.capture,
            runtime.data_dir,
            python_path=runtime.config.settings.computer.python_path,
        )
        self.voice = self._make_voice()
        runtime.add_tool_provider(self.tools, coordinator=False)
        runtime.add_tool_provider(self.setup_tools, coordinator=True, session_scoped=True)

    def _setup_owner(self) -> bool:
        identifier = self.runtime.tool_session_id
        if identifier is None or identifier != self.runtime.store.existing_main_session_id():
            return False
        session = self.runtime.store.get("session", identifier)
        return bool(
            session and session.get("kind") == "main" and session.get("principal") == "owner"
        )

    def _setup_guard(self, worker: VoiceWorker, name: str) -> None:
        if self._closed or self.voice is not worker:
            raise ValueError("语音配置已变化，请重新查看状态")
        self.runtime.maintenance.admission.require()
        if name in self.runtime.config.settings.disabled_tools:
            raise PermissionError("该语音设置工具已禁用")

    def _setup_tool_guard(self, name: str) -> Callable[[], None]:
        runtime = self.runtime
        caller, execution = runtime.tool_session_id, runtime._tool_execution.get()
        request = runtime.request_context(caller) if caller is not None else None
        task = asyncio.current_task()
        if caller is None or execution is None or request is None or task is None:
            raise PermissionError("语音设置需要本次主助手的真实工具执行")

        def guard() -> None:
            if (
                not self._setup_owner()
                or runtime._closing
                or task.cancelling()
                or runtime.tasks.get(caller) is not task
                or runtime.run_ids.get(caller) != execution[1]
                or execution[0] != caller
                or caller in runtime._stopping
                or runtime.request_context(caller) is not request
                or name in runtime.config.settings.disabled_tools
            ):
                raise PermissionError("语音设置的主助手运行已结束或被替换")
            trigger = runtime.store.db.execute(
                "SELECT o.id=? AND json_extract(o.data,'$.principal')='owner' "
                "FROM message_order m JOIN objects o ON o.kind='message' AND o.id=m.message_id "
                "WHERE m.session_id=? AND json_extract(o.data,'$.role')='user' "
                "ORDER BY m.seq DESC LIMIT 1",
                (request.reply_origin.input_id, caller),
            ).fetchone()
            if trigger is None or trigger[0] != 1 or runtime.store.pending_inputs(caller):
                raise PermissionError("语音设置期间收到新输入，请按当前要求重新确认")
            runtime._guard_request(request, "write")

        guard()
        return guard

    async def prepare_voice_acoustic(
        self, settings: dict[str, Any], *, caller_guard: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        from muli.voice_configuration import prepare_acoustic

        worker = self.voice

        def guard() -> None:
            if caller_guard is not None:
                caller_guard()
            self._setup_guard(worker, "voice_acoustic_prepare")

        async with self.runtime.configuration_lock:
            guard()
            with self.runtime.maintenance.admission.operation():
                return await prepare_acoustic(
                    settings,
                    worker.files,
                    max_output_chars=self.runtime.config.settings.limits.max_output_chars,
                    guard=guard,
                )

    async def voice_setup(
        self,
        operation: str,
        seconds: float = 5,
        *,
        caller_guard: Callable[[], None] | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        worker = self.voice
        name = "voice_setup_" + operation

        def guard() -> None:
            self._setup_guard(worker, name)
            if caller_guard is not None:
                caller_guard()

        guard()
        if operation in {"enroll", "verify"}:
            identifier = identifier or str(uuid4())
        try:
            if operation == "enroll":
                result = await worker.setup_enroll(seconds, guard=guard, identifier=identifier)
            elif operation == "verify":
                result = await worker.setup_verify(seconds, guard=guard, identifier=identifier)
            elif operation == "cancel":
                result = await worker.cancel_setup(identifier)
            else:
                raise ValueError("未知语音设置操作")
        except asyncio.CancelledError:
            task = asyncio.current_task()
            # Another owner request can cancel the finite capture child. That
            # is an actual receipt, distinct from cancellation of this caller.
            if task is None or task.cancelling():
                raise
            result = {
                "id": identifier,
                "operation": operation,
                "requested_seconds": seconds,
                "state": "cancelled",
            }
        guard()
        return result

    def setup_tools(self, runtime: Runtime) -> list[Tool]:
        from muli.acoustic_bridge import BridgeConfig
        from muli.voice_configuration import setup_guidance

        if runtime.tool_session_id is not None and not self._setup_owner():
            return []

        def owner() -> None:
            if not self._setup_owner():
                raise PermissionError("语音设置请由真实主人主会话操作")

        async def status(args: dict[str, Any]) -> Any:
            owner()
            return setup_guidance(self.voice.status())

        async def prepare(args: dict[str, Any]) -> Any:
            owner()
            return await self.prepare_voice_acoustic(
                args, caller_guard=self._setup_tool_guard("voice_acoustic_prepare")
            )

        async def enroll(args: dict[str, Any]) -> Any:
            owner()
            return await self.voice_setup(
                "enroll",
                args.get("seconds", 5),
                caller_guard=self._setup_tool_guard("voice_setup_enroll"),
            )

        async def verify(args: dict[str, Any]) -> Any:
            owner()
            return await self.voice_setup(
                "verify",
                args.get("seconds", 5),
                caller_guard=self._setup_tool_guard("voice_setup_verify"),
            )

        async def cancel(args: dict[str, Any]) -> Any:
            owner()
            return await self.voice_setup(
                "cancel", caller_guard=self._setup_tool_guard("voice_setup_cancel")
            )

        duration = parameters({"seconds": {"type": "number", "minimum": 1, "maximum": 15}})
        return [
            Tool(
                "voice_setup_status",
                "只读检查语音配置与下一步；不录音、不加载模型",
                parameters({}),
                status,
            ),
            Tool(
                "voice_acoustic_prepare",
                "校验本地模型并准备私有配置快照，返回self_configure补丁；不应用、不启用录音、不下载",
                BridgeConfig.model_json_schema(),
                prepare,
                "write",
            ),
            Tool(
                "voice_setup_enroll",
                "后台唤醒关闭时显式录制短声纹参考；不训练、不上传",
                duration,
                enroll,
                "computer",
            ),
            Tool(
                "voice_setup_verify",
                "后台关闭时有限录音验证唤醒和声纹；不调用ASR、不播放、不提交对话",
                duration,
                verify,
                "computer",
            ),
            Tool(
                "voice_setup_cancel",
                "取消并结算当前语音设置录音，不停止其它业务",
                parameters({}),
                cancel,
                "write",
            ),
        ]

    def _make_voice(self) -> VoiceWorker:
        generation = self._generation

        async def on_text(event: dict[str, Any]) -> None:
            await self._on_voice(worker, generation, event)

        worker = VoiceWorker(
            self.runtime.config.settings.voice,
            self.runtime.data_dir,
            self.media,
            on_text,
            on_status=self.on_voice_status,
            admission=self.runtime.maintenance.admission,
        )
        return worker

    async def on_voice(self, event: dict[str, Any]) -> None:
        await self._on_voice(self.voice, self._generation, event)

    async def _on_voice(self, worker: VoiceWorker, generation: int, event: dict[str, Any]) -> None:
        worker_epoch = worker.reply_epoch

        def current_source() -> None:
            if (
                self._closed
                or worker is not self.voice
                or generation != self._generation
                or worker.reply_epoch != worker_epoch
                or worker.status()["closed"]
                or worker.status()["stopping"]
            ):
                raise _ReplyRevoked

        current_source()
        response = await self.runtime.submit(
            str(event["text"]),
            source="voice",
            principal="owner"
            if event.get("speaker_verified") is True
            else "external:voice:unverified",
            admission_guard=current_source,
        )
        if (
            event.get("speaker_verified") is not True
            or response["duplicate"]
            or not worker.config.enabled
            or not worker.config.reply_enabled
            or not worker.config.tts_enabled
            or worker.status()["speaker"].get("configured") is not True
            or worker.status()["speaker"].get("enrolled") is not True
        ):
            return
        ticket = _VoiceReply(
            worker,
            generation,
            worker_epoch,
            response["voice_reply_epoch"],
            response["session_id"],
            response["message"]["id"],
        )
        async with self._reply_lock:
            previous = self._reply_task
            if previous is not None:
                previous.cancel()
                cancelled = await self.runtime.team._settle([previous])
                if self._reply_task is previous:
                    self._reply_task = None
                if cancelled:
                    raise asyncio.CancelledError
            try:
                self._guard_reply(ticket)
            except _ReplyRevoked:
                return
            worker.last_reply = {"state": "waiting", "input_id": ticket.input_id}
            self._reply_task = asyncio.create_task(self._answer(ticket))
            self._reply_task.add_done_callback(self._reply_done)

    def _reply_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        if self._reply_task is task:
            self._reply_task = None

    def _guard_reply(self, ticket: _VoiceReply) -> None:
        config = self.runtime.config.settings.voice
        if (
            self._closed
            or self._generation != ticket.generation
            or self.voice is not ticket.worker
            or ticket.worker.reply_epoch != ticket.worker_epoch
            or self.runtime.voice_reply_epoch != ticket.runtime_epoch
            or not config.enabled
            or not config.tts_enabled
            or not config.reply_enabled
        ):
            raise _ReplyRevoked

    async def _changed(self, ticket: _VoiceReply, revision: int) -> None:
        bus = self.runtime.bus
        revoked = self.runtime.voice_reply_changed
        stopped = ticket.worker.reply_changed
        self._guard_reply(ticket)

        async def changed() -> None:
            async with bus.changed:
                await bus.changed.wait_for(lambda: bus.revision != revision)

        waits = [
            asyncio.create_task(changed()),
            asyncio.create_task(revoked.wait()),
            asyncio.create_task(stopped.wait()),
        ]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def _while_current(
        self, ticket: _VoiceReply, operation: Coroutine[Any, Any, _Result]
    ) -> _Result:
        task = asyncio.create_task(operation)
        changed: asyncio.Task[None] | None = None
        try:
            while not task.done():
                revision = self.runtime.bus.revision
                self._guard_reply(ticket)
                changed = asyncio.create_task(self._changed(ticket, revision))
                await asyncio.wait({task, changed}, return_when=asyncio.FIRST_COMPLETED)
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
                changed = None
            self._guard_reply(ticket)
            return await task
        finally:
            pending: list[asyncio.Task[Any]] = [task]
            if changed is not None:
                pending.append(changed)
            for child in pending:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _answer(self, ticket: _VoiceReply) -> None:
        worker = ticket.worker
        try:
            while True:
                revision = self.runtime.bus.revision
                self._guard_reply(ticket)
                row = self.runtime.store.db.execute(
                    "SELECT CASE WHEN state IN ('pending','delivered','cancelled') "
                    "THEN state END AS state,CASE WHEN typeof(run_id)='text' "
                    "AND length(CAST(run_id AS BLOB)) BETWEEN 1 AND 256 "
                    "THEN CAST(run_id AS BLOB) END AS run_id "
                    "FROM ingress WHERE id=? AND session_id=?",
                    (ticket.input_id, ticket.session_id),
                ).fetchone()
                if row is None or row["state"] not in {"pending", "delivered"}:
                    raise _ReplyRevoked
                if row["state"] == "delivered":
                    if row["run_id"] is None:
                        raise _ReplyRevoked
                    run_id = row["run_id"].decode("utf-8")
                    break
                await self._changed(ticket, revision)
            worker.last_reply["run_id"] = run_id
            content = await self._while_current(ticket, self.runtime.wait(run_id))
            limits = self.runtime.config.settings.limits
            budget = max(limits.max_context_chars, limits.max_output_chars) * 4
            run = self.runtime.store.bounded_object("run", run_id, max_bytes=budget)
            if (
                run is None
                or run.get("session_id") != ticket.session_id
                or run.get("status") != "completed"
                or not isinstance(run.get("result_message_id"), str)
                or not run["result_message_id"]
                or len(run["result_message_id"]) > 256
                or ReplyOrigin.load(run.get("reply_origin")).input_id != ticket.input_id
                or not content.strip()
                or len(content) > limits.max_output_chars
            ):
                raise _ReplyRevoked
            message = self.runtime.store.bounded_object(
                "message", run["result_message_id"], max_bytes=budget
            )
            if (
                message is None
                or message.get("session_id") != ticket.session_id
                or message.get("role") != "assistant"
                or message.get("tool_calls")
                or message.get("interrupted")
                or message.get("content") != content
                or ReplyOrigin.load(message.get("reply_origin"))
                != ReplyOrigin.load(run.get("reply_origin"))
            ):
                raise _ReplyRevoked
            worker.last_reply["message_id"] = message["id"]
            await self._while_current(
                ticket, worker.reply(content, admission_guard=lambda: self._guard_reply(ticket))
            )
        except (_ReplyRevoked, asyncio.CancelledError):
            worker.last_reply["state"] = (
                "unknown" if worker.last_reply.get("state") == "playing" else "cancelled"
            )
        except Exception as exc:
            worker.last_error = f"自动语音回复失败：{type(exc).__name__}"
            worker.last_reply["state"] = (
                "unknown" if worker.last_reply.get("state") == "playing" else "failed"
            )
        finally:
            try:
                await self.on_voice_status(
                    {"type": "voice.reply", "reply": dict(worker.last_reply)}
                )
            except Exception:
                pass  # An optional playback status never changes the main run.

    async def start(self) -> None:
        await self.voice.start()

    @property
    def idle(self) -> bool:
        # refresh_portal owns a shielded child even if its HTTP observer is gone.
        return self.capture.idle

    async def on_voice_status(self, event: dict[str, Any]) -> None:
        await self.runtime.bus.emit("voice.status", event)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._generation += 1
            self._close_task = asyncio.create_task(self._close_resources())
        cancelled = await self.runtime.team._settle([self._close_task])
        if cancelled:
            raise asyncio.CancelledError
        self._close_task.result()

    async def _close_resources(self) -> None:
        task = self._reply_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.capture.aclose()
        await self.voice.aclose()
        await self.media.aclose()

    async def reconfigure(self) -> None:
        await self.close()
        self.media = self.local.media()
        self.capture = Capture(
            self.runtime.config.settings.computer.capture,
            self.runtime.data_dir,
            python_path=self.runtime.config.settings.computer.python_path,
        )
        self._closed = False
        self._close_task = None
        self.voice = self._make_voice()
        await self.start()

    async def publish(self, item: dict[str, Any]) -> dict[str, Any]:
        attachment = await self.attachments.import_file(Path(item["path"]), source="capture")
        await self.runtime.bus.emit("attachment.published", attachment)
        return {**attachment, "local_path": item["path"], "backend": item.get("backend")}

    async def capture_status(self) -> dict[str, Any]:
        if self.runtime.config.settings.computer.enabled:
            await self.capture.refresh_portal()
        return self.capture.status()

    def tools(self, runtime: Runtime) -> list[Tool]:
        def check_capture() -> None:
            if not runtime.config.settings.computer.enabled:
                raise PermissionError("电脑操作未启用")

        async def screenshot(args: dict[str, Any]) -> Any:
            check_capture()
            return await self.publish(await self.capture.screenshot())

        async def record(args: dict[str, Any]) -> Any:
            check_capture()
            return await self.publish(await self.capture.record(int(args["seconds"])))

        async def status(args: dict[str, Any]) -> Any:
            return {"capture": await self.capture_status(), "voice": self.voice.status()}

        async def enroll(args: dict[str, Any]) -> Any:
            result = await self.voice.enroll(float(args.get("seconds", 5)))
            # Voice references remain private enrollment data, not downloadable chat attachments.
            return {"enrolled": True, "path": result.get("path"), "status": self.voice.status()}

        async def listen(args: dict[str, Any]) -> Any:
            return await self.voice.listen_once()

        return [
            Tool(
                "computer_screenshot",
                "按已配置能力截图，保存本地并返回附件下载引用",
                parameters({}),
                screenshot,
                "computer",
            ),
            Tool(
                "computer_record",
                "按已配置能力进行有界无声录屏，保存本地并返回附件引用",
                parameters(
                    {
                        "seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": runtime.config.settings.computer.capture.max_seconds,
                        }
                    },
                    ("seconds",),
                ),
                record,
                "computer",
            ),
            Tool(
                "media_capabilities",
                "查看截图、录屏、语音及声学运行时的实际可用状态",
                parameters({}),
                status,
            ),
            Tool(
                "voice_enroll",
                "在语音已开启后录制短声纹参考；不会把参考自动发送到模型或渠道",
                parameters(
                    {
                        "seconds": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": runtime.config.settings.voice.max_utterance_seconds,
                        }
                    }
                ),
                enroll,
                "computer",
            ),
            Tool(
                "voice_listen",
                "按已配置声学模型等待一次唤醒并转写；未验证声纹的输入隔离",
                parameters({}),
                listen,
                "computer",
            ),
        ]
