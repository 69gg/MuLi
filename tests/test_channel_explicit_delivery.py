"""External delivery is explicit; ordinary model events remain in WebUI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from test_channels import Harness, consumed, profile, terminal

from muli.channels import ChannelsConfig


async def test_all_active_channels_ignore_assistant_tool_and_reasoning_events(
    tmp_path: Path,
) -> None:
    polling = asyncio.Event()
    deliveries: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/getUpdates", "/getupdates")):
            async with asyncio.timeout(3):
                await polling.wait()
            raise AssertionError("poll must be cancelled when the manager closes")
        deliveries.append(request.url.path)
        raise AssertionError("ordinary model events must not make delivery HTTP requests")

    config = ChannelsConfig(
        onebot=profile(),
        telegram=profile(),
        qq=profile(app_id="qq-app", client_secret="FAKE_QQ_SECRET"),
        weixin=profile(account_id="wx-account", user_id="wx-user"),
    )
    harness = Harness(tmp_path, config, handler)
    try:
        await harness.manager.start()
        assert set(harness.manager._active) == {"onebot", "telegram", "qq", "weixin"}
        last = 0
        for name in ("onebot", "telegram", "qq", "weixin"):
            route = {
                "name": name,
                "account_id": harness.manager._account(name),
                "sender_id": "7",
                "target": "7",
                "kind": "private",
            }
            message = {
                "id": f"ordinary-{name}",
                "role": "assistant",
                "content": "正文仅显示在 WebUI",
                "reasoning": "不得外发的思考",
                "tool_calls": [{"id": "call", "name": "fs_read", "arguments": "{}"}],
                "attachment_ids": ["not-an-explicit-send"],
                "delivery_routes": [route],
            }
            await harness.bus.emit("message.completed", {"message": message}, "main")
            for kind, data in (
                ("message.delta", {"content": "流式正文"}),
                ("reasoning.delta", {"content": "流式思考"}),
                ("tool.started", {"name": "fs_read", "arguments": {"path": "private"}}),
                ("tool.completed", {"name": "fs_read", "result": "private result"}),
            ):
                event = await harness.bus.emit(kind, data, "main")
                last = event["id"]
        await consumed(harness.manager, last)
        assert harness.manager.deliveries() == []
        assert deliveries == []
    finally:
        await harness.close()


async def test_reopen_sends_persisted_explicit_outbox_without_replaying_ordinary_replies(
    tmp_path: Path,
) -> None:
    sent: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert body["user_id"] == "7"
        sent.append(body["message"][0]["data"]["text"])
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "explicit"}}
        )

    config = ChannelsConfig(onebot=profile())
    harness = Harness(tmp_path, config, handler)
    message = {
        "id": "historical-ordinary-reply",
        "role": "assistant",
        "content": "历史普通回复",
        "delivery_routes": [{"name": "onebot", "sender_id": "7", "target": "7", "kind": "private"}],
    }
    try:
        # Initialize the original durable cursor, then leave later events unread
        # alongside a genuinely explicit pending delivery at normal close.
        harness.manager.recover()
        event = await harness.bus.emit("message.completed", {"message": message}, "main")
        queued = await harness.manager.enqueue("onebot", "7", "显式发送", event_id="explicit")
        assert queued["state"] == "pending" and sent == []
    finally:
        await harness.close()

    harness = Harness(tmp_path, config, handler)
    try:
        await harness.manager.start()
        assert (await terminal(harness.manager, queued["id"]))["state"] == "sent"
        await consumed(harness.manager, event["id"])
        assert sent == ["显式发送"]
        assert [item["id"] for item in harness.manager.deliveries()] == [queued["id"]]
    finally:
        await harness.close()

    harness = Harness(tmp_path, config, handler)
    try:
        await harness.manager.start()
        repeated = await harness.bus.emit("message.completed", {"message": message}, "main")
        await consumed(harness.manager, repeated["id"])
        assert sent == ["显式发送"]
        assert [item["id"] for item in harness.manager.deliveries()] == [queued["id"]]
    finally:
        await harness.close()
