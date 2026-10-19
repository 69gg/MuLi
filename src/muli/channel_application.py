"""Channel settings, explicit sending tools and runtime lifecycle composition."""

from __future__ import annotations

from typing import Any

from muli.attachments import Attachments
from muli.channels import ChannelManager, ChannelProfile, ChannelsConfig
from muli.runtime import Runtime
from muli.tools import Tool, parameters, string


class ChannelApplication:
    def __init__(self, runtime: Runtime, attachments: Attachments) -> None:
        self.runtime, self.attachments = runtime, attachments
        self._generation = 0
        self.manager = self._manager()
        runtime.add_tool_provider(self.tools)

    def _manager(self) -> ChannelManager:
        data = self.runtime.config.settings.channels.model_dump()
        expected = self.runtime.config.settings.channels.model_dump()
        generation = self._generation

        async def submit(**kwargs: Any) -> dict[str, Any]:
            def guard() -> None:
                if (
                    generation != self._generation
                    or expected != self.runtime.config.settings.channels.model_dump()
                ):
                    raise ValueError("渠道配置或授权已改变，请由当前渠道重新提交消息")

            return await self.runtime.submit(**kwargs, admission_guard=guard)

        for name in ("onebot", "telegram", "qq", "weixin"):
            data[name] = ChannelProfile(**data[name])
        return ChannelManager(
            self.runtime.store,
            self.runtime.bus,
            ChannelsConfig(**data),
            submit,
            self.attachments,
        )

    async def start(self) -> None:
        await self.manager.start()

    async def close(self) -> None:
        self._generation += 1
        await self.manager.close()

    async def reconfigure(self) -> None:
        await self.close()
        self.manager = self._manager()
        await self.start()

    def tools(self, runtime: Runtime) -> list[Tool]:
        async def status(args: dict[str, Any]) -> Any:
            return {**self.manager.status(), "deliveries": self.manager.deliveries()}

        async def send(args: dict[str, Any]) -> Any:
            return await self.manager.enqueue(
                str(args["channel"]),
                str(args["target"]),
                str(args.get("text", "")),
                args.get("attachment_ids", []),
                route=args.get("route"),
            )

        return [
            Tool(
                "channel_status",
                "查看可选渠道与持久投递记录；不把排队当作已送达",
                parameters({}),
                status,
            ),
            Tool(
                "channel_send",
                "向已绑定的渠道目标发送文字或本地附件；返回投递ID，未成功前不得声称已送达",
                parameters(
                    {
                        "channel": {
                            "type": "string",
                            "enum": ["onebot", "telegram", "qq", "weixin"],
                        },
                        "target": string(),
                        "text": string(),
                        "attachment_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 32,
                        },
                        "route": {
                            "type": "object",
                            "description": "群消息需从可信入站复制name/target/kind/sender_id/reply_to路由，不猜测收件人",
                        },
                    },
                    ("channel", "target"),
                ),
                send,
                "external",
            ),
        ]
