"""Both real inbound adapters reach the main assistant before its history request."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import onebot_body, terminal
from test_mobile_channels import onebot_payload, telegram_payload
from test_mobile_task_control_followup import processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.channels.crypto import aes_media, sign_qq
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now


def assert_host_origin(
    messages: list[Message], runtime: Runtime, channel: str, incoming: str
) -> None:
    prefix = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
    main_id = runtime.main_session()["id"]
    persisted = next(
        message
        for message in reversed(runtime.store.messages(main_id))
        if message["role"] == "user" and message["content"] == incoming
    )
    supplied = [message for message in messages if message.source_message_id == persisted["id"]]
    assert len(supplied) == 1 and supplied[0].role == "user"
    header, separator, original = supplied[0].content.partition("\n")
    assert header.startswith(prefix) and separator and original == incoming
    route = persisted["route"]
    assert json.loads(header[len(prefix) :]) == {
        "channel": channel,
        "account_id": route["account_id"],
        "sender_id": route["sender_id"],
        "target": route["target"],
        "kind": "private",
        "input_id": persisted["id"],
    }
    assert all(
        "context_token" not in message.content
        and "FAKE_LOCAL_REPLY_CONTEXT" not in message.content
        and "FAKE_ILINK_IMAGE_CONTEXT" not in message.content
        for message in messages
    )


async def test_onebot_and_ilink_owner_inputs_offer_local_channel_history_to_the_real_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await shared_main_history_journey(tmp_path, monkeypatch, while_running=False)


async def test_ilink_input_waits_for_the_active_onebot_model_boundary_without_rerouting_its_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await shared_main_history_journey(tmp_path, monkeypatch, while_running=True)


async def shared_main_history_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, while_running: bool
) -> None:
    first_input = "这是主人从 OneBot 发来的上下文：稍后请在另一个渠道核查本地聊天记录。"
    first_reply = "已保存这轮 OneBot 对话，后续核查以本地记录为准。"
    second_input = "这是主人从微信发来的后续：请找回 MuLi 已保存的聊天历史。"
    second_reply = "已收到微信历史查询请求；是否具备读取能力以实际工具目录为准。"
    first_sent = "主人明确选择发送到 OneBot 的第一条工具消息。"
    cross_sent = "微信来源这一轮明确选择发送到已绑定的 OneBot 7。"
    release_weixin = asyncio.Event()
    release_second = asyncio.Event()
    hold_poll = asyncio.Event()
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    poll_calls = 0
    sends: list[tuple[str, dict[str, Any]]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        assert request.method == "POST"
        body = json.loads(request.content)
        if request.url.host == "onebot.invalid":
            assert request.url.path == "/send_private_msg" and body["user_id"] == "7"
            sends.append(("onebot", body))
            return httpx.Response(
                200,
                json={"status": "ok", "retcode": 0, "data": {"message_id": "onebot-reply"}},
            )
        assert request.url.host == "ilink.invalid"
        if request.url.path == "/ilink/bot/getupdates":
            poll_calls += 1
            assert isinstance(body["get_updates_buf"], str)
            if poll_calls == 1:
                await release_weixin.wait()
                return httpx.Response(
                    200,
                    json={
                        "ret": 0,
                        "get_updates_buf": "local-history-after-one",
                        "msgs": [
                            {
                                "message_type": 1,
                                "message_id": "weixin-history-request",
                                "from_user_id": "wx-owner",
                                "context_token": "FAKE_LOCAL_REPLY_CONTEXT",
                                "item_list": [{"type": 1, "text_item": {"text": second_input}}],
                            }
                        ],
                    },
                )
            await hold_poll.wait()
            raise AssertionError("后续真实长轮询应由应用关闭取消")
        raise AssertionError("来源为微信不应自动决定外发目的地")

    transport = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://onebot.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7"],
                },
                "weixin": {
                    "enabled": True,
                    "account_id": "wx-account",
                    "user_id": "wx-bot",
                    "base_url": "https://ilink.invalid",
                    "token": "FAKE_ILINK_TOKEN",
                    "owner_ids": ["wx-owner"],
                },
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    history_available: bool | None = None
    runs: dict[str, str] = {}
    rounds = {"onebot": 0, "weixin": 0}
    delivery_ids: dict[str, str] = {}

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal history_available
            assert len(messages) <= 32 and all(not message.images for message in messages)
            names = {tool["function"]["name"] for tool in tools}
            assert {"conversation_read", "channel_status"} <= names
            incoming = [message.content for message in messages if message.role == "user"]
            is_second = any(content.endswith(second_input) for content in incoming)
            channel = "weixin" if is_second else "onebot"
            runs[channel] = runtime.run_ids[main_id]
            rounds[channel] += 1
            if is_second:
                await asyncio.wait_for(release_second.wait(), 3)
                assert any(content.endswith(first_input) for content in incoming)
                assert_host_origin(messages, runtime, "onebot", first_input)
                assert_host_origin(messages, runtime, "weixin", second_input)
                history_available = "channel_history" in names
                channel, sent = "weixin", cross_sent
                reply = second_reply
            else:
                assert len(incoming) == 1 and incoming[0].endswith(first_input)
                assert_host_origin(messages, runtime, "onebot", first_input)
                channel, sent = "onebot", first_sent
                first_entered.set()
                if while_running:
                    await asyncio.wait_for(release_first.wait(), 3)
                reply = first_reply
            if channel == "onebot" and while_running:
                assert rounds[channel] == 1
            elif rounds[channel] == 1:
                assert "channel_send" in names
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            call(
                                "explicit-" + channel,
                                "channel_send",
                                channel="onebot",
                                target="7",
                                text=sent,
                                idempotency_key="history-explicit-" + channel,
                            )
                        ]
                    ),
                )
                return
            else:
                assert rounds[channel] == 2
                receipt = tool_result(messages, "explicit-" + channel)
                assert "error" not in receipt and receipt["name"] == "onebot"
                assert receipt["state"] in {"pending", "sending", "sent"}
                delivery_ids[channel] = receipt["id"]
            yield StreamEvent("text_delta", text=reply)
            yield StreamEvent("done", turn=ModelTurn(content=reply))

    def factory(_config: ModelConfig) -> FakeClient:
        return Main([], [])

    runtime.client_factory = factory
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        manager = app.state.channel_application.manager
        mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
        assert mode.status_code == 200
        raw, headers = onebot_body(onebot_payload(first_input))
        headers["Content-Type"] = "application/json"
        accepted = await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
        assert accepted.status_code == 200
        if while_running:
            await asyncio.wait_for(first_entered.wait(), 3)
            active = runtime.tasks[main_id]
            active_run = runtime.run_ids[main_id]
            release_weixin.set()

            def weixin_pending() -> bool:
                return (
                    runtime.store.db.execute(
                        "SELECT 1 FROM ingress WHERE session_id=? AND state='pending' "
                        "AND json_extract(payload,'$.source')='channel:weixin:wx-account' "
                        "AND json_extract(payload,'$.content')=?",
                        (main_id, second_input),
                    ).fetchone()
                    is not None
                )

            await eventually(runtime, weixin_pending)
            assert rounds == {"onebot": 1, "weixin": 0} and not active.done()
            assert runtime.run_ids[main_id] == active_run
            assert not completed(runtime, first_reply) and not completed(runtime, second_reply)
            assert sends == []
            release_first.set()
        await eventually(runtime, lambda: bool(completed(runtime, first_reply)))
        if not while_running:
            assert await runtime.wait(runs["onebot"]) == first_reply
        await processed_reply(runtime, manager, completed(runtime, first_reply)[0]["id"])
        first_delivery = [
            delivery for delivery in manager.deliveries() if delivery["name"] == "onebot"
        ]
        assert len(first_delivery) == (0 if while_running else 1)
        for delivery in first_delivery:
            assert (await terminal(manager, delivery["id"]))["state"] == "sent"
        release_second.set()
        release_weixin.set()
        await eventually(runtime, lambda: bool(completed(runtime, second_reply)))
        assert set(runs) == {"onebot", "weixin"}
        assert (runs["onebot"] == runs["weixin"]) is while_running
        assert await runtime.wait(runs["weixin"]) == second_reply
        await processed_reply(runtime, manager, completed(runtime, second_reply)[0]["id"])
        deliveries = manager.deliveries()
        assert len(deliveries) == (1 if while_running else 2)
        assert {delivery["id"] for delivery in deliveries} == set(delivery_ids.values())
        for delivery in deliveries:
            assert (await terminal(manager, delivery["id"]))["state"] == "sent"
        archived = runtime.store.messages(main_id)
        owner_messages = [message for message in archived if message["role"] == "user"]
        assert [message["content"] for message in owner_messages] == [first_input, second_input]
        assert all(message["principal"] == "owner" for message in owner_messages)
        assert [message["source"] for message in owner_messages] == [
            "channel:onebot:123",
            "channel:weixin:wx-account",
        ]
        assert [message["route"]["sender_id"] for message in owner_messages] == ["7", "wx-owner"]
        assert all(
            runtime.store.get("run", run_id)["status"] == "completed" for run_id in runs.values()
        )
        assert [name for name, _body in sends] == ["onebot"] * len(deliveries)
        assert ["".join(part["data"]["text"] for part in body["message"]) for _, body in sends] == (
            [cross_sent] if while_running else [first_sent, cross_sent]
        )
        assert history_available is True, "真实双渠道上下文与回执已完成，主助手缺少 channel_history"


async def history_runtime(tmp_path: Path) -> Runtime:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://onebot.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7"],
                    "receive_files": True,
                },
                "weixin": {
                    "enabled": True,
                    "account_id": "wx-account",
                    "user_id": "wx-bot",
                    "base_url": "https://ilink.invalid",
                    "token": "FAKE_ILINK_TOKEN",
                    "owner_ids": ["wx-owner"],
                    "receive_files": True,
                },
            },
        }
    )
    await runtime.reconfigure()
    return runtime


def assert_local_page(
    page: dict[str, Any], channel: str, main_id: str, *, direction: str = "both"
) -> None:
    assert "error" not in page, page
    assert set(page) == {
        "source",
        "coverage",
        "consistency",
        "channel",
        "account_id",
        "owner_id",
        "session_id",
        "incoming",
        "outgoing",
    }
    assert page["source"] == "local" and page["coverage"] == "saved_by_muli"
    assert page["consistency"] == "live" and page["channel"] == channel
    assert page["session_id"] == main_id
    assert page["account_id"] == ("123" if channel == "onebot" else "wx-account")
    assert page["owner_id"] == ("7" if channel == "onebot" else "wx-owner")
    for name in ("incoming", "outgoing"):
        stream = page[name]
        if direction not in {"both", name}:
            assert stream is None
            continue
        assert set(stream) == {"items", "omitted", "next_cursor", "has_more"}
        assert type(stream["omitted"]) is int and stream["omitted"] >= 0
        assert type(stream["has_more"]) is bool
        assert [item["seq"] for item in stream["items"]] == sorted(
            item["seq"] for item in stream["items"]
        )
        assert all("content" not in item for item in stream["items"])
        if name == "incoming":
            assert all(item["role"] == "user" for item in stream["items"])
        else:
            assert all(
                item["kind"] == "delivery" and item["role"] == "assistant"
                for item in stream["items"]
            )
            assert all("platform_message_id" not in item for item in stream["items"])
    assert "FAKE_ILINK" not in json.dumps(page)


async def test_local_channel_history_survives_reopen_and_reads_downloaded_attachment_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9WQAAAAASUVORK5CYII="
    )
    key = bytes(range(16))
    encrypted = aes_media(image, key, encrypt=True)
    onebot_text = "ONEBOT-LOCAL-IMAGE：请保留此附件，稍后只核本地历史。"
    weixin_text = "ILINK-LOCAL-IMAGE：请保留这份附件，不自动看图。"
    replies = {onebot_text: "OneBot 附件已登记。", weixin_text: "微信附件已登记。"}
    release_weixin, hold_poll = asyncio.Event(), asyncio.Event()
    supplied_weixin = False
    sends: list[str] = []
    downloads: list[str] = []

    async def fake_public_url(url: str) -> tuple[httpx.URL, str, str]:
        parsed = httpx.URL(url)
        assert parsed.host == "cdn.invalid" and parsed.path in {"/onebot.png", "/weixin.enc"}
        return parsed, parsed.host, parsed.host

    async def platform_response(request: httpx.Request) -> httpx.Response:
        nonlocal supplied_weixin
        if request.url.host == "cdn.invalid":
            assert request.method == "GET"
            downloads.append(request.url.path)
            return httpx.Response(
                200, content=image if request.url.path == "/onebot.png" else encrypted
            )
        assert request.method == "POST"
        body = json.loads(request.content)
        if request.url.host == "onebot.invalid":
            if request.url.path == "/get_image":
                assert body == {"file": "onebot-image-id"}
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "retcode": 0,
                        "data": {"url": "https://cdn.invalid/onebot.png"},
                    },
                )
            assert request.url.path == "/send_private_msg" and body["user_id"] == "7"
            sends.append("onebot")
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": "local-ob-reply"}}
            )
        assert request.url.host == "ilink.invalid"
        if request.url.path == "/ilink/bot/getupdates":
            if not supplied_weixin:
                await release_weixin.wait()
                supplied_weixin = True
                return httpx.Response(
                    200,
                    json={
                        "ret": 0,
                        "get_updates_buf": "local-image-cursor",
                        "msgs": [
                            {
                                "message_type": 1,
                                "message_id": "wx-image-message",
                                "from_user_id": "wx-owner",
                                "context_token": "FAKE_ILINK_IMAGE_CONTEXT",
                                "item_list": [
                                    {"type": 1, "text_item": {"text": weixin_text}},
                                    {
                                        "type": 2,
                                        "image_item": {
                                            "aeskey": key.hex(),
                                            "media": {"url": "https://cdn.invalid/weixin.enc"},
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                )
            await hold_poll.wait()
            raise AssertionError("后台长轮询只能由正常关闭取消")
        assert request.url.path == "/ilink/bot/sendmessage"
        assert body["msg"]["to_user_id"] == "wx-owner"
        sends.append("weixin")
        return httpx.Response(200, json={"ret": 0})

    transport = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    monkeypatch.setattr("muli.channels.http.public_url", fake_public_url)
    runtime = await history_runtime(tmp_path)
    main_id = runtime.main_session()["id"]
    origins: dict[str, dict[str, Any]] = {}
    explicit_texts = {"onebot": "显式 OneBot 附件登记通知。", "weixin": "显式微信附件登记通知。"}
    delivery_ids: dict[str, str] = {}
    ack_rounds = {"onebot": 0, "weixin": 0}
    model_requests = 0

    class Acknowledge(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal model_requests
            model_requests += 1
            assert all(not message.images for message in messages)
            supplied = next(
                message.content for message in reversed(messages) if message.role == "user"
            )
            incoming = next(text for text in replies if supplied.endswith(text))
            channel = "onebot" if incoming == onebot_text else "weixin"
            assert_host_origin(messages, runtime, channel, incoming)
            ack_rounds[channel] += 1
            if ack_rounds[channel] == 1:
                assert "channel_send" in {tool["function"]["name"] for tool in tools}
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            call(
                                "ack-" + channel,
                                "channel_send",
                                channel=channel,
                                target="7" if channel == "onebot" else "wx-owner",
                                text=explicit_texts[channel],
                                idempotency_key="image-history-" + channel,
                            )
                        ]
                    ),
                )
                return
            assert ack_rounds[channel] == 2
            receipt = tool_result(messages, "ack-" + channel)
            assert "error" not in receipt and receipt["name"] == channel
            assert receipt["state"] in {"pending", "sending", "sent"}
            delivery_ids[channel] = receipt["id"]
            yield StreamEvent("text_delta", text=replies[incoming])
            yield StreamEvent("done", turn=ModelTurn(content=replies[incoming]))

    runtime.client_factory = lambda _config: Acknowledge([], [])
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as web,
    ):
        manager = app.state.channel_application.manager
        mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
        assert mode.status_code == 200
        payload = onebot_payload(onebot_text)
        payload["message_id"] = 901
        payload["message"].append({"type": "image", "data": {"file": "onebot-image-id"}})
        raw, headers = onebot_body(payload)
        headers["Content-Type"] = "application/json"
        assert (
            await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
        ).status_code == 200
        await eventually(runtime, lambda: bool(completed(runtime, replies[onebot_text])))
        await processed_reply(runtime, manager, completed(runtime, replies[onebot_text])[0]["id"])
        release_weixin.set()
        await eventually(runtime, lambda: bool(completed(runtime, replies[weixin_text])))
        await processed_reply(runtime, manager, completed(runtime, replies[weixin_text])[0]["id"])
        for delivery in manager.deliveries():
            assert (await terminal(manager, delivery["id"]))["state"] == "sent"
        assert sends == ["onebot", "weixin"] and model_requests == 4
        assert {delivery["id"] for delivery in manager.deliveries()} == set(delivery_ids.values())
        for message in runtime.store.messages(main_id):
            if message["role"] == "user":
                channel = message["route"]["name"]
                assert message["principal"] == "owner" and len(message["attachment_ids"]) == 1
                origins[channel] = message
                item = app.state.attachments.get(message["attachment_ids"][0])
                assert (
                    item["mime_type"] == "image/png"
                    and item["sha256"] == hashlib.sha256(image).hexdigest()
                )
                assert app.state.attachments.path(item["id"]).read_bytes() == image
        assert set(origins) == {"onebot", "weixin"} and len(app.state.attachments.list()) == 2

    reopened, _ = make_runtime(tmp_path, [])
    pages: dict[str, dict[str, Any]] = {}
    rounds = 0

    class ReadHistory(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            assert len(messages) <= 32 and all(not message.images for message in messages)
            assert "channel_history" in {tool["function"]["name"] for tool in tools}
            if rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call("directory-onebot", "channel_history", channel="onebot", owner_id="7")
                    ]
                )
            elif rounds in {2, 4}:
                channel = "onebot" if rounds == 2 else "weixin"
                page = tool_result(messages, "directory-" + channel)
                assert_local_page(page, channel, main_id)
                pages[channel] = page
                original = origins[channel]
                item = next(
                    item for item in page["incoming"]["items"] if item["id"] == original["id"]
                )
                assert (
                    item["role"] == "user" and item["attachment_ids"] == original["attachment_ids"]
                )
                assert item["platform_message_id"] == original["route"]["reply_to"]
                assert len(page["incoming"]["items"]) == 1
                assert len(page["outgoing"]["items"]) == 1
                delivery = page["outgoing"]["items"][0]
                assert delivery["id"] == delivery_ids[channel] and delivery["state"] == "sent"
                assert delivery["confirmed_parts"] == delivery["total_parts"] == 1
                assert delivery["attachment_ids"] == []
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "body-" + channel,
                            "conversation_read",
                            session_id=main_id,
                            message_id=item["id"],
                        )
                    ]
                )
            elif rounds == 3:
                body = tool_result(messages, "body-onebot")
                assert "error" not in body and body["content"] == onebot_text
                assert body["revision"] == next(
                    item["revision"]
                    for item in pages["onebot"]["incoming"]["items"]
                    if item["id"] == origins["onebot"]["id"]
                )
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "directory-weixin",
                            "channel_history",
                            channel="weixin",
                            owner_id="wx-owner",
                        )
                    ]
                )
            elif rounds == 5:
                body = tool_result(messages, "body-weixin")
                assert "error" not in body and body["content"] == weixin_text
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "delivery-onebot",
                            "channel_history",
                            channel="onebot",
                            owner_id="7",
                            delivery_id=delivery_ids["onebot"],
                            part=0,
                            offset_bytes=0,
                        )
                    ]
                )
            elif rounds == 6:
                body = tool_result(messages, "delivery-onebot")
                assert "error" not in body and body["content"] == explicit_texts["onebot"]
                assert body["state"] == "sent" and body["delivery_id"] == delivery_ids["onebot"]
                assert isinstance(body["revision"], str) and body["has_more"] is False
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "delivery-weixin",
                            "channel_history",
                            channel="weixin",
                            owner_id="wx-owner",
                            delivery_id=delivery_ids["weixin"],
                            part=0,
                            offset_bytes=0,
                        )
                    ]
                )
            else:
                assert rounds == 7
                body = tool_result(messages, "delivery-weixin")
                assert "error" not in body and body["content"] == explicit_texts["weixin"]
                assert body["state"] == "sent" and body["delivery_id"] == delivery_ids["weixin"]
                assert isinstance(body["revision"], str) and body["has_more"] is False
                turn = ModelTurn(
                    content="已从重开后的本地目录定位并核对两渠道原文；图片仍只是本地附件。"
                )
            if turn.content:
                yield StreamEvent("text_delta", text=turn.content)
            yield StreamEvent("done", turn=turn)

    reopened.client_factory = lambda _config: ReadHistory([], [])
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    async with (
        reopened_app.router.lifespan_context(reopened_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(reopened_app), base_url="http://localhost:8765"
        ) as web,
    ):
        assert reopened.main_session()["id"] == main_id
        accepted = await web.post(
            f"/api/sessions/{main_id}/messages",
            json={"content": "请从本地目录核对两渠道刚才的原文，不自动看图。"},
        )
        assert accepted.status_code == 200
        async with asyncio.timeout(4):
            reply = await reopened.wait(accepted.json()["run_id"])
        assert reply == "已从重开后的本地目录定位并核对两渠道原文；图片仍只是本地附件。"
        assert rounds == 7 and set(pages) == {"onebot", "weixin"}
        assert downloads == ["/onebot.png", "/weixin.enc"] and sends == ["onebot", "weixin"]
        assert {
            delivery["id"]
            for delivery in reopened_app.state.channel_application.manager.deliveries()
        } == set(delivery_ids.values())
        assert len(reopened_app.state.attachments.list()) == 2


@pytest.mark.parametrize("boundary", ["ambiguous_owner", "revoked_owner", "changed_account_cursor"])
async def test_local_history_rechecks_current_owner_and_cursor_account_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    hold_poll = asyncio.Event()
    sends: list[dict[str, Any]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ilink.invalid":
            assert request.url.path == "/ilink/bot/getupdates"
            await hold_poll.wait()
            raise AssertionError("正常关闭应取消长轮询")
        assert request.url.host == "onebot.invalid" and request.url.path == "/send_private_msg"
        sends.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "policy-seed-reply"}}
        )

    transport = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime = await history_runtime(tmp_path)
    main_id = runtime.main_session()["id"]
    phase = "seed"
    round_number = 0
    page: dict[str, Any] = {}
    rejected: dict[str, Any] = {}

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal round_number, page, rejected
            assert all(not message.images for message in messages)
            if phase == "seed":
                turn = ModelTurn(content="真实主人私聊已保存，未发送其它资料。")
            else:
                round_number += 1
                assert "channel_history" in {tool["function"]["name"] for tool in tools}
                if round_number == 1:
                    arguments: dict[str, Any] = {"channel": "onebot"}
                    if boundary != "ambiguous_owner":
                        arguments["owner_id"] = "7"
                    if phase == "cursor":
                        arguments["limit"] = 1
                        arguments["direction"] = "incoming"
                    elif boundary == "changed_account_cursor":
                        arguments["direction"] = "incoming"
                        arguments["cursor"] = page["incoming"]["next_cursor"]
                    turn = ModelTurn(
                        tool_calls=[call("read-policy-history", "channel_history", **arguments)]
                    )
                else:
                    assert round_number == 2
                    result = tool_result(messages, "read-policy-history")
                    if phase == "cursor":
                        assert_local_page(result, "onebot", main_id, direction="incoming")
                        assert result["incoming"]["has_more"] is True and isinstance(
                            result["incoming"]["next_cursor"], str
                        )
                        page = result
                    else:
                        rejected = result
                    turn = ModelTurn(content="已依据真实工具回执结束本次目录核查。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Main([], [])
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as web,
    ):
        raw, headers = onebot_body(onebot_payload("本地目录权限策略的真实主人消息。"))
        headers["Content-Type"] = "application/json"
        assert (
            await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
        ).status_code == 200
        seed_reply = "真实主人私聊已保存，未发送其它资料。"
        await eventually(runtime, lambda: bool(completed(runtime, seed_reply)))
        await processed_reply(
            runtime, app.state.channel_application.manager, completed(runtime, seed_reply)[0]["id"]
        )
        for delivery in app.state.channel_application.manager.deliveries():
            assert (await terminal(app.state.channel_application.manager, delivery["id"]))[
                "state"
            ] == "sent"
        assert sends == [] and app.state.channel_application.manager.deliveries() == []
        if boundary == "changed_account_cursor":
            phase = "cursor"
            accepted = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "请只读一页本地目录并保留宿主游标。"},
            )
            assert accepted.status_code == 200
            async with asyncio.timeout(4):
                assert (
                    await runtime.wait(accepted.json()["run_id"])
                    == "已依据真实工具回执结束本次目录核查。"
                )
            assert page["account_id"] == "123"
            runtime.config.update({"channels": {"onebot": {"account_id": "456"}}})
        elif boundary == "ambiguous_owner":
            runtime.config.update({"channels": {"onebot": {"owner_ids": ["7", "8"]}}})
        else:
            runtime.config.update({"channels": {"onebot": {"owner_ids": ["8"]}}})
        await app.state.channel_application.reconfigure()
        phase, round_number = "reject", 0
        previous_outbox = len(app.state.channel_application.manager.deliveries())
        accepted = await web.post(
            f"/api/sessions/{main_id}/messages",
            json={"content": "请按当前渠道绑定核查目录；不可猜主人或借旧游标。"},
        )
        assert accepted.status_code == 200
        async with asyncio.timeout(4):
            assert (
                await runtime.wait(accepted.json()["run_id"])
                == "已依据真实工具回执结束本次目录核查。"
            )
        assert round_number == 2 and rejected.get("error"), rejected
        assert "incoming" not in rejected and "outgoing" not in rejected
        assert sends == []
        assert len(app.state.channel_application.manager.deliveries()) == previous_outbox


@pytest.mark.parametrize("channel", ["telegram", "qq"])
@pytest.mark.parametrize(
    "boundary", ["current_owner", "disabled", "foreign_owner", "changed_account_cursor", "group"]
)
async def test_telegram_and_qq_history_uses_real_private_origin_and_current_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str, boundary: str
) -> None:
    """Real polling/signed webhook -> main tools; no manufactured request scope."""
    hold_poll = asyncio.Event()
    poll_advanced = asyncio.Event()
    requests: list[str] = []
    supplied_telegram = False

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        nonlocal supplied_telegram
        requests.append(request.url.path)
        assert channel == "telegram" and request.url.host == "history.invalid"
        assert request.url.path.endswith("/getUpdates")
        if not supplied_telegram:
            supplied_telegram = True
            update = telegram_payload(original_text)
            if boundary == "group":
                update["message"]["chat"] = {"id": 99, "type": "group"}
            return httpx.Response(200, json={"ok": True, "result": [update]})
        poll_advanced.set()
        await hold_poll.wait()
        raise AssertionError("正常关闭或换代应取消真实 Telegram 长轮询")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    profile: dict[str, Any] = {
        "enabled": True,
        "account_id": "123",
        "owner_ids": ["7"],
        "allowed_groups": ["99"],
        "base_url": "https://history.invalid",
        "token": "123:FAKE_HISTORY_TOKEN",
        "webhook_secret": "FAKE_HISTORY_WEBHOOK",
        "app_id": "history-app",
        "client_secret": "FAKE_HISTORY_QQ_SECRET",
    }
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "channels": {channel: profile},
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    original_text = f"{channel} 主人的真实本地历史消息。"
    phase = "initial"
    rounds = 0
    cursor: str | None = None
    rejection: dict[str, Any] = {}
    original: dict[str, Any] = {}
    final_text = "已按真实渠道权限完成只读核查，未发送外部消息。"

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds, cursor, rejection, original
            assert len(messages) <= 32 and all(not message.images for message in messages)
            schema = next(
                tool["function"] for tool in tools if tool["function"]["name"] == "channel_history"
            )
            assert {"onebot", "telegram", "qq", "weixin"} <= set(
                schema["parameters"]["properties"]["channel"]["enum"]
            )
            if phase == "initial" and boundary not in {"current_owner", "group"}:
                turn = ModelTurn(content=final_text)
            else:
                rounds += 1
                if rounds == 1:
                    arguments: dict[str, Any] = {}
                    if phase != "initial" or boundary == "group":
                        arguments = {"channel": channel, "owner_id": "7"}
                    if phase == "cursor":
                        arguments.update(direction="incoming", limit=1)
                    elif phase == "reject" and boundary == "changed_account_cursor":
                        assert isinstance(cursor, str)
                        arguments.update(direction="incoming", cursor=cursor)
                    turn = ModelTurn(
                        tool_calls=[call("read-extra-channel", "channel_history", **arguments)]
                    )
                elif rounds == 2:
                    result = tool_result(messages, "read-extra-channel")
                    if phase == "reject":
                        assert (
                            result.get("error")
                            and "incoming" not in result
                            and "outgoing" not in result
                        )
                        rejection = result
                        turn = ModelTurn(content=final_text)
                    elif phase == "cursor":
                        assert "error" not in result and result["channel"] == channel
                        assert result["incoming"]["has_more"] is True
                        cursor = result["incoming"]["next_cursor"]
                        assert isinstance(cursor, str) and result["outgoing"] is None
                        turn = ModelTurn(content=final_text)
                    else:
                        assert "error" not in result
                        assert {
                            name: result[name]
                            for name in (
                                "source",
                                "coverage",
                                "consistency",
                                "channel",
                                "account_id",
                                "owner_id",
                                "session_id",
                            )
                        } == {
                            "source": "local",
                            "coverage": "saved_by_muli",
                            "consistency": "live",
                            "channel": channel,
                            "account_id": "123",
                            "owner_id": "7",
                            "session_id": main_id,
                        }
                        if boundary == "group":
                            assert result["incoming"]["items"] == []
                            assert result["outgoing"]["items"] == []
                            turn = ModelTurn(content=final_text)
                        else:
                            assert (
                                len(result["incoming"]["items"]) == 1
                                and result["outgoing"]["items"] == []
                            )
                            item = result["incoming"]["items"][0]
                            stored_original = runtime.store.get("message", item["id"])
                            assert isinstance(stored_original, dict)
                            original = stored_original
                            assert (
                                original["content"] == original_text
                                and original["route"]["kind"] == "private"
                            )
                            assert original["principal"] == "owner" and item["role"] == "user"
                            assert_host_origin(messages, runtime, channel, original_text)
                            turn = ModelTurn(
                                tool_calls=[
                                    call(
                                        "read-extra-channel-body",
                                        "conversation_read",
                                        session_id=main_id,
                                        message_id=item["id"],
                                    )
                                ]
                            )
                else:
                    assert rounds == 3 and phase == "initial" and boundary == "current_owner"
                    result = tool_result(messages, "read-extra-channel-body")
                    assert "error" not in result and result["content"] == original_text
                    turn = ModelTurn(content=final_text)
            if turn.content:
                yield StreamEvent("text_delta", text=turn.content)
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Main([], [])
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as web,
    ):
        if channel == "qq":
            data: dict[str, Any] = {
                "id": "history-qq-message",
                "author": {"user_openid": "7"},
                "content": original_text,
            }
            if boundary == "group":
                data["group_openid"] = "99"
                data["author"] = {"member_openid": "7"}
            payload = {
                "id": "history-qq-event",
                "op": 0,
                "t": "GROUP_AT_MESSAGE_CREATE" if boundary == "group" else "C2C_MESSAGE_CREATE",
                "d": data,
            }
            raw = json.dumps(payload).encode()
            timestamp = str(int(now()))
            headers = {
                "X-Bot-Appid": "history-app",
                "X-Signature-Timestamp": timestamp,
                "X-Signature-Ed25519": sign_qq("FAKE_HISTORY_QQ_SECRET", timestamp.encode() + raw),
            }
            headers["Content-Type"] = "application/json"
            accepted = await web.post(
                f"/api/channels/{channel}/webhook", content=raw, headers=headers
            )
            assert accepted.status_code == 200
        group_query = "请从网页只读核对当前主人私聊目录；被忽略的群消息不应在其中。"
        if boundary == "group":
            if channel == "telegram":
                async with asyncio.timeout(4):
                    await poll_advanced.wait()
            assert rounds == 0
            assert (
                runtime.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 0
            )
            assert not [
                message for message in runtime.store.messages(main_id) if message["role"] == "user"
            ]
            queried = await web.post(
                f"/api/sessions/{main_id}/messages", json={"content": group_query}
            )
            assert queried.status_code == 200
        await eventually(runtime, lambda: bool(completed(runtime, final_text)))
        first = completed(runtime, final_text)[0]
        await processed_reply(runtime, app.state.channel_application.manager, first["id"])
        assert app.state.channel_application.manager.deliveries() == []
        stored = [
            message for message in runtime.store.messages(main_id) if message["role"] == "user"
        ]
        assert len(stored) == 1 and stored[0]["principal"] == "owner"
        if boundary == "group":
            assert stored[0]["content"] == group_query and stored[0]["source"] == "web"
        else:
            assert stored[0]["content"] == original_text
            assert stored[0]["source"] == f"channel:{channel}:123"
            assert stored[0]["route"]["kind"] == "private"
        if boundary == "current_owner":
            assert rounds == 3 and original["id"] == stored[0]["id"]
        elif boundary == "group":
            assert rounds == 2 and not rejection
        else:
            if boundary == "changed_account_cursor":
                phase = "cursor"
                queried = await web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={"content": "请保存当前私聊目录的一页游标。"},
                )
                assert queried.status_code == 200
                async with asyncio.timeout(4):
                    assert await runtime.wait(queried.json()["run_id"]) == final_text
                assert isinstance(cursor, str)
                change: dict[str, Any] = {"account_id": "456"}
            elif boundary == "disabled":
                change = {"enabled": False}
            else:
                change = {"owner_ids": ["8"]}
            runtime.config.update({"channels": {channel: change}})
            await app.state.channel_application.reconfigure()
            phase, rounds = "reject", 0
            queried = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "请按当前绑定查询，不能借旧账号或旧主人。"},
            )
            assert queried.status_code == 200
            async with asyncio.timeout(4):
                assert await runtime.wait(queried.json()["run_id"]) == final_text
            assert rounds == 2 and rejection.get("error")
        assert app.state.channel_application.manager.deliveries() == []
        assert all(path.endswith("/getUpdates") for path in requests)
