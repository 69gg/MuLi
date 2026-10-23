"""Channel settings, explicit sending tools and runtime lifecycle composition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from muli.attachments import Attachments
from muli.channels import ChannelError, ChannelManager, ChannelProfile, ChannelsConfig
from muli.channels.types import IncomingMessage
from muli.mobile_task_control import MobileControlRejected, MobileControlSource
from muli.proactive import FrozenRouting, guard_delivery
from muli.proactive_memory import check_preference
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
        data.pop("proactive")
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

        async def control_submit(
            *,
            name: str,
            message: IncomingMessage,
            route: dict[str, Any],
            event_id: str,
            receipt: dict[str, Any] | None,
        ) -> dict[str, Any]:
            # Even a completed inbound receipt must pass today's authorization.
            # Runtime owns locking and durable replay; this layer never holds
            # configuration_lock while waiting for an inbound lock or shutdown.
            def guard() -> None:
                if (
                    generation != self._generation
                    or manager is not self.manager
                    or expected != self.runtime.config.settings.channels.model_dump()
                    or manager.adapters.get(name) is not adapters.get(name)
                ):
                    raise ChannelError(
                        "渠道配置或授权已改变，请由当前渠道重新提交任务控制", status_code=403
                    )
                manager.control_guard(name, message, route)

            try:
                return await self.runtime.submit_mobile_task_control(
                    inbound_id=event_id,
                    source=MobileControlSource(
                        name=name,
                        account_id=message.control_account_id,
                        owner_id=message.sender_id,
                        target=message.target,
                    ),
                    text=message.control_text,
                    admission_guard=guard,
                )
            except MobileControlRejected:
                # Only an explicit pre-write rejection is terminal to polling.
                # Storage and post-commit notification failures remain retryable.
                raise ChannelError(
                    "手机任务控制无法核验，请重新发送 /任务", status_code=403
                ) from None

        def delivery_guard(route: Mapping[str, Any], metadata: object) -> None:
            guard_delivery(self.runtime.config.settings, route, metadata, db=self.runtime.store.db)

        for name in ("onebot", "telegram", "qq", "weixin"):
            data[name] = ChannelProfile(**data[name])
        manager = ChannelManager(
            self.runtime.store,
            self.runtime.bus,
            ChannelsConfig(**data),
            submit,
            self.attachments,
            delivery_guard=delivery_guard,
            admission=self.runtime.maintenance.admission,
            control_submit=control_submit,
        )
        adapters = dict(manager.adapters)
        manager.recover()
        return manager

    def status(self) -> dict[str, Any]:
        settings = self.runtime.config.settings
        selected = FrozenRouting.independent(settings, self.runtime.store.db)
        _, reason = check_preference(settings, self.runtime.store.db)
        return {
            **self.manager.status(),
            "proactive": {
                **settings.channels.proactive.model_dump(),
                "next_destination": selected.dump()["destination"],
                "attention_enabled": settings.attention.enabled,
                "model_configured": settings.models.main.configured,
                "independent_sources": [
                    "startup_update_check",
                    "extensions_watch",
                    "mcp_connection",
                    "chiliarch_message",
                    "file_inbox",
                ],
                "memory_status": {"valid": reason == "valid", "reason": reason},
            },
        }

    async def start(self) -> None:
        await self.manager.start()

    async def close(self) -> None:
        self._generation += 1
        await self.manager.close()

    async def reconfigure(self) -> None:
        self.runtime.store.revalidate_format()
        await self.close()
        self.manager = self._manager()
        await self.start()

    def tools(self, runtime: Runtime) -> list[Tool]:
        async def status(args: dict[str, Any]) -> Any:
            return {**self.status(), "deliveries": self.manager.deliveries()}

        async def send(args: dict[str, Any]) -> Any:
            key = args.get("idempotency_key")
            if key is not None and (
                not isinstance(key, str)
                or not 1 <= len(key) <= 200
                or any(ord(character) < 32 or ord(character) == 127 for character in key)
            ):
                raise ValueError("idempotency_key 需要 1–200 字符的非控制字符串")
            return await self.manager.enqueue(
                str(args["channel"]),
                str(args["target"]),
                str(args.get("text", "")),
                args.get("attachment_ids", []),
                route=args.get("route"),
                media_mode=args.get("media_mode", "file"),
                event_id="tool:" + key if key is not None else None,
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
                "向已绑定渠道发送文字或附件；media_mode=voice显式发送单个语音附件，默认保持文件路径；重试同一逻辑发送保持idempotency_key，结果未知不自动重发，返回排队ID不代表送达",
                parameters(
                    {
                        "channel": {
                            "type": "string",
                            "enum": ["onebot", "telegram", "qq", "weixin"],
                        },
                        "target": string(),
                        "text": string(),
                        "media_mode": {
                            "type": "string",
                            "enum": ["file", "voice"],
                            "description": "默认file保留既有附件行为；voice仅Telegram/OneBot，需一个兼容音频附件，不自动转码或降级",
                        },
                        "idempotency_key": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "同一逻辑发送保持稳定；同键内容或模式改变会拒绝。省略表示新发送，不能用换键规避未知结果",
                        },
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
