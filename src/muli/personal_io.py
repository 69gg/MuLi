"""Compose opt-in desktop and voice capture with the common attachment store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from muli.attachments import Attachments
from muli.capture import Capture
from muli.local_services import LocalServices
from muli.runtime import Runtime
from muli.tools import Tool, parameters
from muli.voice import VoiceWorker


class PersonalIO:
    def __init__(self, runtime: Runtime, local: LocalServices, attachments: Attachments) -> None:
        self.runtime, self.local, self.attachments = runtime, local, attachments
        self.media = local.media()
        self.capture = Capture(
            runtime.config.settings.computer.capture,
            runtime.data_dir,
            python_path=runtime.config.settings.computer.python_path,
        )
        self.voice = VoiceWorker(
            runtime.config.settings.voice,
            runtime.data_dir,
            self.media,
            self.on_voice,
            on_status=self.on_voice_status,
            admission=runtime.maintenance.admission,
        )
        runtime.add_tool_provider(self.tools, coordinator=False)

    async def on_voice(self, event: dict[str, Any]) -> None:
        await self.runtime.submit(
            str(event["text"]),
            source="voice",
            principal="owner"
            if event.get("speaker_verified") is True
            else "external:voice:unverified",
        )

    async def start(self) -> None:
        await self.voice.start()

    @property
    def idle(self) -> bool:
        # refresh_portal owns a shielded child even if its HTTP observer is gone.
        return self.capture.idle

    async def on_voice_status(self, event: dict[str, Any]) -> None:
        await self.runtime.bus.emit("voice.status", event)

    async def close(self) -> None:
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
        self.voice = VoiceWorker(
            self.runtime.config.settings.voice,
            self.runtime.data_dir,
            self.media,
            self.on_voice,
            on_status=self.on_voice_status,
            admission=self.runtime.maintenance.admission,
        )
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
