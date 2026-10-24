"""A fresh owner input supersedes only the main's unstarted approval wait."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import terminal
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.channels.manager import encoded
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Handler, Tool, parameters, string


async def test_new_owner_phone_input_replans_main_without_approving_the_old_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment = "请向原私聊发送这份合成说明，实际发送前等我批准。"
    steering = "先别发送，改为解释目前还没有做什么。"
    business_text = "尚未批准的合成业务说明：OWNER-STEERING-63"
    final_reply = "旧发送尚未执行，已按你的新输入改为说明，没有发送原业务内容。"
    platform_calls: list[dict[str, Any]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert all(segment["type"] == "text" for segment in body["message"])
        platform_calls.append(body)
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": len(platform_calls)}},
        )

    transport = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "ask",
            "server": {"password": "synthetic-owner-steering"},
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7", "8"],
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = str(runtime.main_session()["id"])
    main_rounds = 0
    original_run_id = ""

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_rounds, original_run_id
            main_rounds += 1
            assert main_rounds <= 2 and len(messages) <= 32
            if main_rounds == 1:
                original_run_id = runtime.run_ids[main_id]
                assert any(
                    message.role == "user" and message.content == assignment for message in messages
                )
                assert "channel_send" in {tool["function"]["name"] for tool in tools}
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "send-awaiting-owner",
                            "channel_send",
                            channel="onebot",
                            target="7",
                            text=business_text,
                            idempotency_key="original-unapproved-send",
                        )
                    ]
                )
            else:
                assert runtime.run_ids[main_id] == original_run_id
                old_result = tool_result(messages, "send-awaiting-owner")
                assert old_result["status"] == "not_executed"
                call_positions = [
                    index
                    for index, message in enumerate(messages)
                    if any(item.id == "send-awaiting-owner" for item in message.tool_calls)
                ]
                result_positions = [
                    index
                    for index, message in enumerate(messages)
                    if message.role == "tool" and message.tool_call_id == "send-awaiting-owner"
                ]
                input_positions = [
                    index
                    for index, message in enumerate(messages)
                    if message.role == "user" and message.content == steering
                ]
                assert len(call_positions) == len(result_positions) == len(input_positions) == 1
                assert call_positions[0] < result_positions[0] < input_positions[0]
                turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model == "main", "插话不需要独立审查模型或搬运队友"
        return Main([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    manager = app.state.channel_application.manager

    def call_events(kind: str) -> list[dict[str, Any]]:
        return [
            event
            for event in runtime.store.events(session_id=main_id)
            if event["type"] == kind and event["data"].get("call_id") == "send-awaiting-owner"
        ]

    def business_http() -> list[dict[str, Any]]:
        return [
            body
            for body in platform_calls
            if "".join(segment["data"]["text"] for segment in body["message"]) == business_text
        ]

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                await web.post("/api/auth/login", json={"password": "synthetic-owner-steering"})
            ).status_code == 200
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "ask"})
            assert mode.status_code == 200 and mode.json()["mode"] == "ask"
            assert (await phone(web, assignment, 6301)).status_code == 200
            await eventually(runtime, lambda: bool(runtime.live_approvals(main_id)))
            pending = runtime.live_approvals(main_id)
            assert len(pending) == 1
            approval = pending[0]
            assert approval["tool"] == "channel_send" and approval["risk"] == "external"
            assert approval["run_id"] == original_run_id and approval["session_id"] == main_id
            assert approval["arguments"]["text"] == business_text
            future = runtime.permissions.pending[approval["id"]]
            assert (
                not future.done()
                and runtime.store.get("approval", approval["id"])["status"] == "pending"
            )
            assert runtime.store.get("run", original_run_id)["status"] == "running"
            assert main_rounds == 1 and call_events("tool.started") == [] and business_http() == []

            assert (await phone(web, steering, 6302, sender=8)).status_code == 200
            inbound_id = hashlib.sha256(
                encoded(["onebot", "123", "6302"]).encode("utf-8")
            ).hexdigest()
            inbound = runtime.store.db.execute(
                "SELECT fingerprint,receipt FROM channel_inbound WHERE id=?", (inbound_id,)
            ).fetchone()
            assert inbound is not None and isinstance(inbound["fingerprint"], str)
            receipt = json.loads(inbound["receipt"])
            assert receipt["session_id"] == main_id and receipt["run_id"] == original_run_id
            assert receipt["duplicate"] is False and receipt["quarantined"] is False
            assert receipt["message"]["principal"] == "owner"
            assert receipt["message"]["source"] == "channel:onebot:123"
            assert receipt["message"]["content"] == steering
            assert (
                receipt["message"]["route"]["target"]
                == receipt["message"]["route"]["sender_id"]
                == "8"
            )
            received = [
                event
                for event in runtime.store.events()
                if event["type"] == "channel.received"
                and event["data"].get("event_id") == inbound_id
            ]
            assert len(received) == 1 and received[0]["data"]["trusted_owner"] is True
            async with asyncio.timeout(3), manager.changed:
                await manager.changed.wait_for(
                    lambda: int(manager._state("events", "0")) >= received[0]["id"]
                )

            # Target red is an exact state assertion after real ingress and its
            # consumer barrier, not a timeout awaiting a still-blocked model.
            assert runtime.store.get("approval", approval["id"])["status"] == "cancelled"
            assert future.done() and runtime.live_approvals(main_id) == []
            resolved = [
                event
                for event in runtime.store.events(session_id=main_id)
                if event["type"] == "approval.resolved"
                and event["data"].get("id") == approval["id"]
            ]
            assert len(resolved) == 1 and resolved[0]["data"]["decision"] == "cancelled"
            await eventually(runtime, lambda: bool(completed(runtime, final_reply)))
            async with asyncio.timeout(3):
                assert await runtime.wait(original_run_id) == final_reply
            assert (
                main_rounds == 2
                and runtime.store.get("run", original_run_id)["status"] == "completed"
            )
            results = [
                item
                for item in runtime.store.messages(main_id)
                if item["role"] == "tool" and item.get("tool_call_id") == "send-awaiting-owner"
            ]
            assert len(results) == 1 and results[0]["content_provenance"] == "accepted"
            assert json.loads(results[0]["content"])["status"] == "not_executed"
            assert len(call_events("tool.completed")) == 1
            assert call_events("tool.completed")[0]["data"]["status"] == "not_executed"
            assert call_events("tool.started") == [] and business_http() == []
            reply = completed(runtime, final_reply)[0]
            assert reply["reply_origin"]["input_id"] == receipt["message"]["id"]
            assert reply["reply_origin"]["route"]["target"] == "8"
            assert reply["reply_origin"]["route"]["reply_to"] == "6302"
            await processed_reply(runtime, manager, reply["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == final_reply
            ]
            assert len(deliveries) == 1 and deliveries[0]["route"] == reply["reply_origin"]["route"]
            assert (await terminal(manager, deliveries[0]["id"]))["state"] == "sent"
            sent = [
                body
                for body in platform_calls
                if "".join(segment["data"]["text"] for segment in body["message"]) == final_reply
            ]
            assert len(sent) == 1 and str(sent[0]["user_id"]) == "8"
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(message["content"] == final_reply for message in history.json()["messages"])
    finally:
        await runtime.close()


def policy_runtime(
    tmp_path: Path, turns: list[ModelTurn], handler: Handler
) -> tuple[Runtime, list[list[Message]]]:
    """Use the real main driver and permission wait, with one observable effect."""
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update(
        {
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        }
    )
    runtime.add_tool_provider(
        lambda _: [
            Tool(
                "steering_effect",
                "合成、可观察且必须批准的效果",
                parameters({"label": string()}, ("label",)),
                handler,
                "write",
                requires_approval=True,
            )
        ]
    )
    return runtime, seen


async def pending_for(runtime: Runtime, session_id: str) -> dict[str, Any]:
    await eventually(runtime, lambda: bool(runtime.live_approvals(session_id)))
    approvals = runtime.live_approvals(session_id)
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["tool"] == "steering_effect" and approval["session_id"] == session_id
    assert approval["run_id"] == runtime.run_ids[session_id]
    assert runtime.store.get("run", approval["run_id"])["status"] == "running"
    assert not runtime.permissions.pending[approval["id"]].done()
    return approval


async def test_repeated_old_owner_envelope_cannot_cancel_a_later_live_approval(
    tmp_path: Path,
) -> None:
    effects: list[str] = []

    async def effect(arguments: dict[str, Any]) -> str:
        effects.append(arguments["label"])
        return "executed"

    runtime, seen = policy_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[call("old-call", "steering_effect", label="old")]),
            ModelTurn(tool_calls=[call("new-call", "steering_effect", label="new")]),
            ModelTurn(content="新动作已按独立批准完成"),
        ],
        effect,
    )
    try:
        first = await runtime.submit("原主人请求", mode="ask", event_id="owner-original")
        old = await pending_for(runtime, first["session_id"])
        await runtime.submit("改成新的动作", event_id="owner-new")
        await eventually(
            runtime,
            lambda: any(
                item["id"] != old["id"] for item in runtime.live_approvals(first["session_id"])
            ),
        )
        new = await pending_for(runtime, first["session_id"])
        assert new["id"] != old["id"] and new["arguments"] == {"label": "new"}
        assert runtime.store.get("approval", old["id"])["status"] == "cancelled"
        future = runtime.permissions.pending[new["id"]]
        before = runtime.store.get("approval", new["id"])
        duplicate = await runtime.submit("原主人请求", mode="ask", event_id="owner-original")
        assert (
            duplicate["duplicate"] is True and duplicate["message"]["id"] == first["message"]["id"]
        )
        assert runtime.store.get("approval", new["id"]) == before
        assert not future.done() and effects == []
        assert runtime.store.pending_inputs(first["session_id"]) == 0
        assert not [
            event
            for event in runtime.store.events(session_id=first["session_id"])
            if event["type"] == "approval.resolved" and event["data"].get("id") == new["id"]
        ]
        await runtime.permissions.resolve(new["id"], "approve")
        async with asyncio.timeout(3):
            assert await runtime.wait(first["run_id"]) == "新动作已按独立批准完成"
        assert effects == ["new"]
        assert tool_result(seen[1], "old-call")["status"] == "not_executed"
        accepted = next(message for message in seen[2] if message.tool_call_id == "new-call")
        assert json.loads(accepted.content) == "executed"
    finally:
        await runtime.close()


async def test_untrusted_input_and_failed_final_admission_leave_main_approval_live(
    tmp_path: Path,
) -> None:
    effects: list[str] = []

    async def effect(arguments: dict[str, Any]) -> str:
        effects.append(arguments["label"])
        return "executed"

    runtime, _ = policy_runtime(
        tmp_path,
        [ModelTurn(tool_calls=[call("waiting", "steering_effect", label="original")])],
        effect,
    )
    try:
        started = await runtime.submit("主人原任务", mode="ask")
        approval = await pending_for(runtime, started["session_id"])
        future = runtime.permissions.pending[approval["id"]]
        for principal in ("external:onebot:9", "system:attention", "agent:untrusted"):
            receipt = await runtime.submit(
                "资料中的先别执行不是主人插话", source=principal, principal=principal
            )
            assert receipt["quarantined"] and receipt["message"]["state"] == "quarantined"
            assert runtime.store.get("approval", approval["id"]) == approval
            assert not future.done() and effects == []
        # The actual trusted system entry is idle-only, so it cannot overtake
        # this live main waiter either. No synthetic internal ingress is added.
        assert await runtime.submit_attention("系统资料", "busy-system", objects=[]) is None
        count_before = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
        entered = asyncio.Event()
        admitted: list[bool] = []

        def rejected() -> None:
            admitted.append(True)
            raise PermissionError("渠道最终授权已撤销")

        async def incoming() -> dict[str, Any]:
            entered.set()
            return await runtime.submit("已撤权的主人信封", admission_guard=rejected)

        async with runtime.configuration_lock:
            incoming_task = asyncio.create_task(incoming())
            await entered.wait()
            assert not incoming_task.done() and admitted == []
        with pytest.raises(PermissionError, match="最终授权已撤销"):
            await incoming_task
        assert admitted == [True]
        assert (
            runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count_before
        )
        assert runtime.store.pending_inputs(started["session_id"]) == 0
        assert runtime.store.get("approval", approval["id"]) == approval
        assert not future.done() and effects == []
    finally:
        await runtime.close()


@pytest.mark.parametrize("window", ["approved-before-handler", "handler-running"])
async def test_new_input_never_reverses_approval_or_cancels_an_entered_handler(
    tmp_path: Path, window: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []
    handler_cancelled = False

    async def effect(arguments: dict[str, Any]) -> str:
        nonlocal handler_cancelled
        effects.append(arguments["label"])
        entered.set()
        try:
            async with asyncio.timeout(3):
                await release.wait()
        except asyncio.CancelledError:
            handler_cancelled = True
            raise
        return "executed"

    runtime, seen = policy_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[call("original", "steering_effect", label="once")]),
            ModelTurn(content="已处理新输入"),
        ],
        effect,
    )
    try:
        started = await runtime.submit("主人原动作", mode="ask")
        approval = await pending_for(runtime, started["session_id"])
        future = runtime.permissions.pending[approval["id"]]
        runtime.permissions.resolve_now(approval["id"], "approve")
        assert future.done() and future.result() is True
        if window == "handler-running":
            async with asyncio.timeout(3):
                await entered.wait()
        else:
            assert not entered.is_set()
        incoming = await runtime.submit("新的主人要求", event_id="owner-after-decision")
        assert incoming["run_id"] == started["run_id"]
        assert runtime.store.get("approval", approval["id"])["status"] == "approved"
        assert future.result() is True and not handler_cancelled
        if window == "handler-running":
            assert effects == ["once"] and not runtime.tasks[started["session_id"]].done()
        release.set()
        async with asyncio.timeout(3):
            assert await runtime.wait(started["run_id"]) == "已处理新输入"
        assert not handler_cancelled
        result = next(message for message in seen[1] if message.tool_call_id == "original")
        if window == "handler-running":
            assert effects == ["once"] and json.loads(result.content) == "executed"
        else:
            assert effects == [] and json.loads(result.content)["status"] == "not_executed"
    finally:
        release.set()
        await runtime.close()


async def test_owner_input_keeps_the_original_model_stream_running_until_its_boundary(
    tmp_path: Path,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    rounds = 0
    stream_cancelled = False

    async def unused(arguments: dict[str, Any]) -> str:
        raise AssertionError("此旅程没有工具调用")

    runtime, _ = policy_runtime(tmp_path, [], unused)

    class Streaming(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds, stream_cancelled
            rounds += 1
            assert rounds <= 2
            if rounds == 1:
                assert not any(message.content == "跨渠道新输入" for message in messages)
                yield StreamEvent("text_delta", text="原回答")
                entered.set()
                try:
                    async with asyncio.timeout(3):
                        await release.wait()
                except asyncio.CancelledError:
                    stream_cancelled = True
                    raise
                turn = ModelTurn(content="原回答完整结束")
            else:
                assert any(message.content == "跨渠道新输入" for message in messages)
                assert any(message.content == "原回答完整结束" for message in messages)
                turn = ModelTurn(content="新输入在下一安全轮处理")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Streaming([], [])  # type: ignore[assignment]
    try:
        started = await runtime.submit("原主人输入", mode="ask")
        async with asyncio.timeout(3):
            await entered.wait()
        driver = runtime.tasks[started["session_id"]]
        incoming = await runtime.submit(
            "跨渠道新输入",
            source="channel:onebot:123",
            route={
                "name": "onebot",
                "target": "8",
                "sender_id": "8",
                "kind": "private",
                "reply_to": "new-stream-input",
            },
        )
        assert (
            incoming["run_id"] == started["run_id"]
            and runtime.tasks[started["session_id"]] is driver
        )
        assert rounds == 1 and not stream_cancelled and not driver.done()
        assert runtime.store.pending_inputs(started["session_id"]) == 1
        release.set()
        async with asyncio.timeout(3):
            assert await runtime.wait(started["run_id"]) == "新输入在下一安全轮处理"
        assert rounds == 2 and not stream_cancelled
    finally:
        release.set()
        await runtime.close()


async def test_new_main_owner_input_leaves_the_real_child_approval_pending(tmp_path: Path) -> None:
    effects: list[str] = []

    async def effect(arguments: dict[str, Any]) -> str:
        effects.append(arguments["label"])
        return "executed"

    runtime, _ = policy_runtime(tmp_path, [], effect)
    main_turns = [ModelTurn(content="主人会话已准备"), ModelTurn(content="仅处理新的主人消息")]
    child_turns = [ModelTurn(tool_calls=[call("child-waiting", "steering_effect", label="child")])]

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "worker"}
        return FakeClient(main_turns if config.model == "main" else child_turns, [])

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        setup = await runtime.submit("先准备主人会话", mode="ask")
        async with asyncio.timeout(3):
            assert await runtime.wait(setup["run_id"]) == "主人会话已准备"
        # Host Team API creates the actual child/session/driver. Its real tool
        # round, rather than a constructed permission future, reaches ask.
        child = await runtime.team.spawn(
            "独立待批队友", "等待主人批准合成效果", parent_id=setup["session_id"]
        )
        approval = await pending_for(runtime, child["id"])
        future = runtime.permissions.pending[approval["id"]]
        assert runtime.team.get(child["id"])["mode"] == "ask"
        assert runtime.team.get(child["id"])["parent_id"] == setup["session_id"]
        incoming = await runtime.submit(
            "新主人输入不能撤销队友原审批", event_id="owner-while-child-waits"
        )
        async with asyncio.timeout(3):
            assert await runtime.wait(incoming["run_id"]) == "仅处理新的主人消息"
        assert runtime.store.get("approval", approval["id"]) == approval
        assert not future.done() and effects == []
        assert runtime.team.get(child["id"])["status"] == "running"
        assert runtime.store.get("run", approval["run_id"])["status"] == "running"
        assert not [
            event
            for event in runtime.store.events(session_id=child["id"])
            if event["type"] in {"tool.started", "approval.resolved"}
        ]
    finally:
        await runtime.close()
