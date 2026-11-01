"""跨渠道连续摘要：真实移交、子 Agent 回复、外发关联、失败状态与重启可读。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_ownership_runtime import complete_assignment
from test_channels import onebot_body
from test_mobile_channels import onebot_payload
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.providers import Message, ModelTurn, StreamEvent
from muli.server import create_app


def summaries(runtime: Any) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in runtime.store.db.execute(
            "SELECT data FROM objects WHERE kind='channel_continuity' ORDER BY rowid"
        ).fetchall()
    ]


def injected(messages: list[Message]) -> dict[str, Any] | None:
    content = next(
        (
            message.content
            for message in messages
            if message.role == "system" and message.content.startswith("跨渠道连续摘要")
        ),
        None,
    )
    if content is None:
        return None
    _, _, raw = content.partition("：\n")
    return {"content": content, "items": json.loads(raw)}


async def receive(web: httpx.AsyncClient, number: int, text: str) -> None:
    payload = onebot_payload(text)
    payload["message_id"] = number
    raw, headers = onebot_body(payload)
    headers["Content-Type"] = "application/json"
    response = await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
    assert response.status_code == 200


async def test_continuity_digest_is_off_by_default(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        assert runtime.channel_continuity.enabled is False
        assert (
            runtime.channel_continuity.reference(
                {"main_id": "main", "agent_id": "agent", "run_id": "run"}
            )
            is None
        )
        assert (
            runtime.channel_continuity.record(
                {"id": "agent", "name": "队友"},
                status="completed",
                result="不应写入",
                run_id="run",
            )
            is None
        )
        assert summaries(runtime) == []
    finally:
        await runtime.close()


async def test_delegated_channel_turn_writes_bounded_summary_and_links_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await complete_assignment(tmp_path, monkeypatch)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "channels": {"continuity_digest": True},
            "limits": {"max_context_chars": 32000},
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    child_id = runtime.team.list()[0]["id"]
    assert runtime.channel_ownership.for_agent(child_id)["state"] == "active"

    inbound = "接管后请核对资料并给出完整答复。"
    failed_inbound = "这一轮模拟渠道处理失败。"
    sent_text = "接管队友通过显式工具发出的短答复。"
    long_reply = "接管队友的完整答复：" + "逐字核对后的细节。" * 120
    final_reply = "连续摘要已核查，原文可以读取。"
    restart_reply = "重启后仍能看到渠道连续摘要。"
    delivery_id = ""
    phases: dict[str, int] = {}

    async def platform(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "onebot.invalid" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert body["user_id"] == "7" and body["message"] == [
            {"type": "text", "data": {"text": sent_text}}
        ]
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9101}}
        )

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal delivery_id
            latest = next(item.content for item in reversed(messages) if item.role == "user")
            if self.role == "worker":
                if latest.endswith(failed_inbound):
                    raise RuntimeError("模拟渠道处理失败")
                assert latest.endswith(inbound)
                names = {item["function"]["name"] for item in tools}
                count = phases["worker"] = phases.get("worker", 0) + 1
                if count == 1:
                    assert "channel_send" in names
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "continuity-send",
                                "channel_send",
                                channel="onebot",
                                target="7",
                                text=sent_text,
                                idempotency_key="continuity-send",
                            )
                        ]
                    )
                else:
                    assert count == 2
                    result = tool_result(messages, "continuity-send")
                    assert "error" not in result and result["state"] in {"pending", "sent"}
                    delivery_id = result["id"]
                    turn = ModelTurn(content=long_reply)
            elif latest.endswith("重启后核查连续摘要"):
                assert self.role == "main"
                entry = injected(messages)
                assert entry is not None
                assert {item["status"] for item in entry["items"]} == {"completed", "failed"}
                turn = ModelTurn(content=restart_reply)
            else:
                assert self.role == "main" and latest.endswith("请核查连续摘要并读取原文")
                count = phases["main"] = phases.get("main", 0) + 1
                entry = injected(messages)
                assert entry is not None
                completed = next(
                    item for item in entry["items"] if item["status"] == "completed"
                )
                assert completed["outcome"] == long_reply[:400]
                assert long_reply[400:] not in entry["content"]
                assert completed["input_id"] and completed["result_message_id"]
                assert completed["delivery_ids"] == [delivery_id]
                if count == 1:
                    assert all(long_reply not in item.content for item in messages)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "read-channel-input",
                                "conversation_read",
                                session_id=child_id,
                                message_id=completed["input_id"],
                            ),
                            call(
                                "read-child-reply",
                                "conversation_read",
                                session_id=child_id,
                                message_id=completed["result_message_id"],
                            ),
                        ]
                    )
                else:
                    assert count == 2
                    assert tool_result(messages, "read-channel-input")["content"] == inbound
                    assert tool_result(messages, "read-child-reply")["content"] == long_reply
                    turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        await receive(web, 201, inbound)
        await eventually(
            runtime,
            lambda: (
                runtime.team.get(child_id)["status"] == "completed"
                and runtime.team.get(child_id)["result"] == long_reply
            ),
        )
        records = summaries(runtime)
        assert len(records) == 1
        record = records[0]
        assert record["main_id"] == main_id and record["agent_id"] == child_id
        assert record["channel"] == "onebot"
        assert record["owner_id"] is None and record["scope"] == "account"
        assert record["sender_id"] == "7"
        assert record["status"] == "completed"
        assert record["outcome"] == long_reply[:400] and record["truncated"] is True
        assert record["delivery_ids"] == [delivery_id]
        outbox = runtime.store.db.execute(
            "SELECT payload FROM channel_outbox WHERE id=?", (delivery_id,)
        ).fetchone()
        payload = json.loads(outbox[0])
        assert payload["channel_continuity"]["id"] == record["id"]
        assert payload["channel_continuity"]["run_id"] == record["run_id"]

        accepted = await runtime.submit("请核查连续摘要并读取原文")
        assert await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4) == final_reply
        # The main turn itself only sees references; the full child reply stays in
        # the child session and is read back explicitly.
        assert not any(
            long_reply in item["content"]
            for item in runtime.store.messages(main_id)
            if item["role"] == "assistant"
        )

        await receive(web, 202, failed_inbound)
        await eventually(runtime, lambda: runtime.team.get(child_id)["status"] == "failed")
        records = summaries(runtime)
        assert len(records) == 2
        failed = records[-1]
        assert failed["status"] == "failed" and failed["outcome"] == ""
        assert failed["input_id"] and failed["run_id"] != record["run_id"]

        # 收回后旧代不能再写“成功”摘要。
        revoked = runtime.channel_ownership.control(
            main_id, {"action": "reclaim", "channel": "onebot", "scope": "account"}
        )
        assert revoked["state"] == "main"
        assert (
            runtime.channel_continuity.record(
                {"id": child_id, "name": "渠道资料队友"},
                status="completed",
                result="撤销后不应写入",
                run_id="revoked-run",
            )
            is None
        )
        assert len(summaries(runtime)) == 2

    # A fresh process must still inject the persisted summaries.
    runtime, _ = make_runtime(tmp_path, [])
    runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        assert runtime.main_session()["id"] == main_id
        assert {item["status"] for item in summaries(runtime)} == {"completed", "failed"}
        accepted = await runtime.submit("重启后核查连续摘要")
        assert await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4) == restart_reply
