"""Live approval notices use the requesting worker's frozen phone origin."""

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
from test_mobile_channels import telegram_payload
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.channels.manager import encoded
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


@pytest.mark.parametrize("channel", ["onebot", "telegram"])
async def test_live_worker_approval_notifies_original_phone_before_owner_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    await approval_notice_journey(tmp_path, monkeypatch, channel=channel)


@pytest.mark.parametrize(
    "outcome",
    ["web-approved", "stopped", "revoked", "maintenance", "unknown-reopened", "pending-reopened"],
)
async def test_queued_approval_notice_obeys_live_dispatch_and_reopen_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    await approval_notice_journey(tmp_path, monkeypatch, channel="onebot", outcome=outcome)


async def approval_notice_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    channel: str,
    outcome: str = "sent",
) -> None:
    assignment = "请派队友写入合成成果，实际写入须等待我批准。"
    acknowledgement = "队友已接到委托，实际写入仍待批准。"
    other_input = "这是手机8的独立消息，不变更手机7的委托。"
    other_reply = "已收到手机8的独立消息。"
    worker_result = "队友已在实际批准后写入合成成果。"
    final_reply = "已经核对真实写入结果，成果仍回原任务私聊。"
    stopped_reply = "这次执行已结束，没有自动重新批准或派工。"
    content = "合成私有成果，不得随审批通知外发：APPROVAL-NOTICE-62🙂"
    target = tmp_path / "approved-private-result.txt"
    platform_calls: list[dict[str, Any]] = []
    telegram_updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    notice_entered, release_notice = asyncio.Event(), asyncio.Event()
    gated_ids: list[str] = []
    unknown_attempts = 0
    unknown_notice: dict[str, Any] | None = None
    pending_notice: dict[str, Any] | None = None

    async def platform_response(request: httpx.Request) -> httpx.Response:
        nonlocal unknown_attempts
        assert request.url.host == "platform.invalid"
        assert request.method == "POST"
        body = json.loads(request.content)
        if channel == "telegram":
            assert request.url.path.startswith("/bot123:FAKE_TOKEN/")
            if request.url.path.endswith("/getUpdates"):
                assert body["allowed_updates"] == ["message"] and type(body["offset"]) is int
                async with asyncio.timeout(3):
                    update = await telegram_updates.get()
                return httpx.Response(200, json={"ok": True, "result": [update]})
            assert request.url.path.endswith("/sendMessage")
            assert set(body) == {"chat_id", "text"}
            platform_calls.append(
                {
                    "user_id": body["chat_id"],
                    "message": [{"type": "text", "data": {"text": body["text"]}}],
                }
            )
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": len(platform_calls)}}
            )
        assert request.url.path == "/send_private_msg"
        assert all(segment["type"] == "text" for segment in body["message"])
        platform_calls.append(body)
        text = "".join(segment["data"]["text"] for segment in body["message"])
        if outcome == "unknown-reopened" and any(
            item["tool"] == "fs_write" and item["id"] in text for item in runtime.live_approvals()
        ):
            unknown_attempts += 1
            raise httpx.ReadTimeout("one unconfirmed live approval notice", request=request)
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
            "server": {"password": "synthetic-approval-notice-owner"},
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
            "channels": {
                channel: {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "token": "123:FAKE_TOKEN",
                    "owner_ids": ["7", "8"],
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = str(runtime.main_session()["id"])
    main_rounds = worker_rounds = 0
    worker_id = ""
    runs: dict[str, str] = {}

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_rounds, worker_id
            main_rounds += 1
            assert len(messages) <= 32 and main_rounds <= 4
            if main_rounds <= 2:
                role = "assignment"
            else:
                role = "other" if main_rounds == 3 else "report"
            assert runs.setdefault(role, runtime.run_ids[main_id]) == runtime.run_ids[main_id]
            if main_rounds == 1:
                incoming = next(
                    message
                    for message in messages
                    if message.role == "user" and message.content.endswith(assignment)
                )
                header, text = incoming.content.split("\n", 1)
                assert header.startswith("渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）：")
                assert json.loads(header.split("：", 1)[1]) == {
                    "channel": channel,
                    "account_id": "123",
                    "sender_id": "7",
                    "target": "7",
                    "kind": "private",
                    "input_id": incoming.source_message_id,
                }
                assert text == assignment
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-notice-worker",
                            "spawn_agent",
                            task_name="审批通知核查队友",
                            prompt="只写入一次合成成果，必须等待真实审批，不自行改变权限。",
                        )
                    ]
                )
            elif main_rounds == 2:
                spawned = tool_result(messages, "spawn-notice-worker")
                assert "error" not in spawned
                worker_id = str(spawned["id"])
                turn = ModelTurn(content=acknowledgement)
            elif main_rounds == 3:
                incoming = next(
                    message
                    for message in messages
                    if message.role == "user" and message.content.endswith(other_input)
                )
                header, text = incoming.content.split("\n", 1)
                assert header.startswith("渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）：")
                assert json.loads(header.split("：", 1)[1]) == {
                    "channel": channel,
                    "account_id": "123",
                    "sender_id": "8",
                    "target": "8",
                    "kind": "private",
                    "input_id": incoming.source_message_id,
                }
                assert text == other_input
                assert not target.exists()
                turn = ModelTurn(content=other_reply)
            else:
                report = runtime.store.get("team_report", runs["worker"])
                assert report is not None
                if outcome in {
                    "stopped",
                    "revoked",
                    "maintenance",
                    "unknown-reopened",
                    "pending-reopened",
                }:
                    turn = ModelTurn(content=stopped_reply)
                    yield StreamEvent("done", turn=turn)
                    return
                assert report["execution_status"] == "completed"
                assert report["agent_id"] == worker_id and report["parent_id"] == main_id
                assert json.loads(report["content"].split("\n", 1)[1])["result"] == worker_result
                assert any(worker_result in message.content for message in messages)
                turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds
            worker_rounds += 1
            assert len(messages) <= 32 and worker_rounds <= 2
            if worker_rounds == 1:
                assert "fs_write" in {tool["function"]["name"] for tool in tools}
                turn = ModelTurn(
                    tool_calls=[
                        call("write-notified-result", "fs_write", path=str(target), content=content)
                    ]
                )
            else:
                written = tool_result(messages, "write-notified-result")
                assert "error" not in written
                assert target.read_bytes() == content.encode("utf-8")
                turn = ModelTurn(content=worker_result)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "worker", "reviewer"}
        if config.model == "main":
            return Main([], [])
        return Worker([], []) if config.model == "worker" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    manager = app.state.channel_application.manager

    async def send_phone(
        web: httpx.AsyncClient, text: str, message_id: int, *, sender: int = 7
    ) -> None:
        if channel == "onebot":
            assert (await phone(web, text, message_id, sender=sender)).status_code == 200
            return
        update = telegram_payload(text)
        update["update_id"] = message_id
        update["message"].update(message_id=message_id)
        update["message"]["from"]["id"] = sender
        update["message"]["chat"]["id"] = sender
        key = hashlib.sha256(encoded(["telegram", "123", str(message_id)]).encode()).hexdigest()
        await telegram_updates.put(update)

        def admitted() -> bool:
            row = runtime.store.db.execute(
                "SELECT receipt FROM channel_inbound WHERE id=?", (key,)
            ).fetchone()
            return row is not None and row["receipt"] is not None

        await eventually(runtime, admitted)

    original_send = manager._send_item

    async def gated_send(item: dict[str, Any]) -> None:
        if outcome not in {"sent", "unknown-reopened"} and any(
            approval["tool"] == "fs_write" and approval["id"] in delivery_text(item)
            for approval in runtime.live_approvals()
        ):
            gated_ids.append(item["id"])
            notice_entered.set()
            async with asyncio.timeout(3):
                await release_notice.wait()
        await original_send(item)

    if outcome not in {"sent", "unknown-reopened"}:
        monkeypatch.setattr(manager, "_send_item", gated_send)

    async def wait_reply(text: str, role: str) -> None:
        try:
            await eventually(runtime, lambda: bool(completed(runtime, text)))
        except TimeoutError:
            states = {
                name: {
                    "status": item.get("status"),
                    "exception_type": (item.get("failure_diagnostic") or {}).get("exception_type"),
                }
                for name, identifier in runs.items()
                if (item := runtime.store.get("run", identifier)) is not None
            }
            pytest.fail(json.dumps({"rounds": [main_rounds, worker_rounds], "runs": states})[:1024])
        async with asyncio.timeout(3):
            assert await runtime.wait(runs[role]) == text

    def write_events(kind: str) -> list[dict[str, Any]]:
        return [
            event
            for event in runtime.store.events(session_id=worker_id)
            if event["type"] == kind and event["data"].get("call_id") == "write-notified-result"
        ]

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            login = await web.post(
                "/api/auth/login", json={"password": "synthetic-approval-notice-owner"}
            )
            assert login.status_code == 200
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "ask"})
            assert mode.status_code == 200 and mode.json()["mode"] == "ask"
            await send_phone(web, assignment, 6201)
            await eventually(
                runtime,
                lambda: any(item["tool"] == "spawn_agent" for item in runtime.live_approvals()),
            )
            spawn = runtime.live_approvals()[0]
            assert spawn["tool"] == "spawn_agent" and spawn["session_id"] == main_id
            assert (
                await web.post(f"/api/approvals/{spawn['id']}", json={"decision": "approve"})
            ).status_code == 200
            await wait_reply(acknowledgement, "assignment")
            await eventually(
                runtime,
                lambda: any(item["tool"] == "fs_write" for item in runtime.live_approvals()),
            )
            approval = runtime.live_approvals()[0]
            worker = runtime.team.get(worker_id)
            runs["worker"] = str(worker["run_id"])
            assert worker["parent_id"] == main_id and worker["status"] == "running"
            assert worker["mode"] == runtime.store.get("session", worker_id)["mode"] == "ask"
            assert approval["session_id"] == worker_id and approval["run_id"] == runs["worker"]
            assert approval["arguments"] == {"path": str(target), "content": content}
            future = runtime.permissions.pending[approval["id"]]
            assert not future.done() and not target.exists() and write_events("tool.started") == []
            origin = worker["reply_origin"]
            assert origin["route"]["target"] == origin["route"]["sender_id"] == "7"
            assert origin["route"]["reply_to"] == "6201"

            # Change the main's current phone while the original worker really
            # remains suspended at the permission waiter.
            await send_phone(web, other_input, 6202, sender=8)
            await wait_reply(other_reply, "other")
            assert completed(runtime, other_reply)[0]["reply_origin"]["route"]["target"] == "8"
            requested = [
                event
                for event in runtime.store.events(session_id=worker_id)
                if event["type"] == "approval.requested"
                and event["data"].get("id") == approval["id"]
            ]
            assert len(requested) == 1
            async with asyncio.timeout(3), manager.changed:
                await manager.changed.wait_for(
                    lambda: int(manager._state("events", "0")) >= requested[0]["id"]
                )
            assert not future.done() and not target.exists()
            assert main_rounds == 3 and worker_rounds == 1
            notices = [
                item for item in manager.deliveries() if approval["id"] in delivery_text(item)
            ]
            # Exact target red: the durable event has already been consumed;
            # no timeout or invented approval is used to demonstrate the gap.
            assert len(notices) == 1, "真实approval.requested已消费，原私聊应有一份待审批通知"
            text = delivery_text(notices[0])
            assert approval["id"] in text and "fs_write" in text and approval["risk"] in text
            assert content not in text and str(target) not in text and "arguments" not in text
            assert len(text) <= manager.config.profile(channel).max_message_chars
            if outcome not in {"sent", "unknown-reopened"}:
                async with asyncio.timeout(3):
                    await notice_entered.wait()
                assert gated_ids == [notices[0]["id"]]
                assert manager.delivery(gated_ids[0])["state"] == "pending"
                assert not any(
                    approval["id"]
                    in "".join(segment["data"]["text"] for segment in body["message"])
                    for body in platform_calls
                )
                if outcome == "pending-reopened":
                    assert not future.done()
                    assert runtime.team.get(worker_id)["status"] == "running"
                    assert runtime.store.get("approval", approval["id"])["status"] == "pending"
                    assert write_events("tool.started") == [] and not target.exists()
                    pending_notice = manager.delivery(gated_ids[0])
                    # Exit the real lifespan with both the live waiter and
                    # durable notice untouched. Normal close must cancel the
                    # blocked sender; releasing it here would send the notice.
                    return
                if outcome == "web-approved":
                    resolved = await web.post(
                        f"/api/approvals/{approval['id']}", json={"decision": "approve"}
                    )
                    assert resolved.status_code == 200
                    await wait_reply(final_reply, "report")
                    assert target.read_bytes() == content.encode("utf-8")
                    assert len(write_events("tool.started")) == 1
                elif outcome == "stopped":
                    await runtime.stop(worker_id)
                    assert future.done()
                    assert runtime.store.get("approval", approval["id"])["status"] == "cancelled"
                    assert not target.exists() and write_events("tool.started") == []
                elif outcome == "revoked":
                    runtime.config.update({"channels": {"onebot": {"owner_ids": ["8"]}}})
                    assert not future.done() and not target.exists()
                else:
                    assert outcome == "maintenance"
                    runtime.maintenance.admission.pause()
                    assert not runtime.maintenance.admission.allowed
                    assert not future.done() and not target.exists()
                release_notice.set()
                blocked = await terminal(manager, gated_ids[0])
                assert blocked["state"] == "blocked" and blocked["receipts"] == []
                assert not any(
                    approval["id"]
                    in "".join(segment["data"]["text"] for segment in body["message"])
                    for body in platform_calls
                )
                return
            notice = await terminal(manager, notices[0]["id"])
            assert notice["route"] == origin["route"]
            sent_notices = [
                body
                for body in platform_calls
                if approval["id"] in "".join(segment["data"]["text"] for segment in body["message"])
            ]
            assert len(sent_notices) == 1 and str(sent_notices[0]["user_id"]) == "7"
            assert main_rounds == 3 and worker_rounds == 1
            if outcome == "unknown-reopened":
                assert notice["state"] == "unknown" and notice["attempts"] == 1
                assert notice["receipts"] == [] and notice["next_part"] == 0
                assert unknown_attempts == 1
                assert not future.done() and not target.exists()
                unknown_notice = notice
                # Stop the still-live worker through its real lifecycle before
                # reopening; the unknown remote notice itself remains intact.
                await runtime.stop(worker_id)
                await wait_reply(stopped_reply, "report")
                stopped_message = completed(runtime, stopped_reply)[0]
                await processed_reply(runtime, manager, stopped_message["id"])
                stopped_deliveries = [
                    item for item in manager.deliveries() if delivery_text(item) == stopped_reply
                ]
                assert stopped_deliveries == []
                history = await web.get(f"/api/sessions/{main_id}/messages")
                assert history.status_code == 200
                assert any(
                    message["id"] == stopped_message["id"] and message["content"] == stopped_reply
                    for message in history.json()["messages"]
                )
                assert manager.delivery(notice["id"]) == unknown_notice
                assert write_events("tool.started") == [] and not target.exists()
            else:
                assert notice["state"] == "sent"
                await send_phone(web, f"/批准 {approval['id']}", 6203)
                await wait_reply(final_reply, "report")
                assert target.read_bytes() == content.encode("utf-8")
                assert len(write_events("tool.started")) == len(write_events("tool.completed")) == 1
                accepted = next(
                    item
                    for item in runtime.store.messages(worker_id)
                    if item.get("tool_call_id") == "write-notified-result"
                )
                assert accepted["content_provenance"] == "accepted"
                assert "error" not in json.loads(accepted["content"])
                assert runtime.store.get("approval", approval["id"])["status"] == "approved"
                assert runtime.team.get(worker_id)["reply_origin"] == origin
                reply = completed(runtime, final_reply)[0]
                assert reply["reply_origin"] == origin
                await processed_reply(runtime, manager, reply["id"])
                deliveries = [
                    item for item in manager.deliveries() if delivery_text(item) == final_reply
                ]
                assert deliveries == []
                history = await web.get(f"/api/sessions/{main_id}/messages")
                assert history.status_code == 200
                assert any(
                    message["content"] == final_reply for message in history.json()["messages"]
                )
                assert main_rounds == 4 and worker_rounds == 2
                assert runtime.live_approvals() == []
    finally:
        release_notice.set()
        runtime.maintenance.admission.release()
        await runtime.close()
        if pending_notice is not None:
            assert future.done() and approval["id"] not in runtime.permissions.pending
            assert main_rounds == 3 and worker_rounds == 1
            model_calls_after_pending_close = 0

            def no_pending_model(config: ModelConfig) -> FakeClient:
                nonlocal model_calls_after_pending_close
                model_calls_after_pending_close += 1
                raise AssertionError("正常重开不得复活旧审批或重做待批工具")

            reopened_pending = Runtime(
                ConfigFile(runtime.config.path), runtime.data_dir, client_factory=no_pending_model
            )
            pending_app = create_app(
                reopened_pending.config, reopened_pending.data_dir, reopened_pending
            )
            try:
                async with pending_app.router.lifespan_context(pending_app):
                    pending_manager = pending_app.state.channel_application.manager
                    assert reopened_pending.live_approvals() == []
                    assert approval["id"] not in reopened_pending.permissions.pending
                    blocked_notice = await terminal(pending_manager, pending_notice["id"])
                    assert blocked_notice["state"] == "blocked"
                    assert blocked_notice["receipts"] == [] and blocked_notice["attempts"] == 0
                    assert blocked_notice["route"] == pending_notice["route"]
                    assert blocked_notice["payload"] == pending_notice["payload"]
                    barrier = await reopened_pending.bus.emit(
                        "synthetic.pending-approval-notice-reopen", {}
                    )
                    async with asyncio.timeout(3), pending_manager.changed:
                        await pending_manager.changed.wait_for(
                            lambda: int(pending_manager._state("events", "0")) >= barrier["id"]
                        )
                    assert pending_manager.delivery(pending_notice["id"]) == blocked_notice
                    assert not any(
                        approval["id"]
                        in "".join(segment["data"]["text"] for segment in body["message"])
                        for body in platform_calls
                    )
                    assert model_calls_after_pending_close == 0 and not target.exists()
                    assert not [
                        event
                        for event in reopened_pending.store.events(session_id=worker_id)
                        if event["type"] == "tool.started"
                    ]
            finally:
                await reopened_pending.close()

    if unknown_notice is not None:
        calls_before = len(platform_calls)
        model_calls = 0

        def no_model(config: ModelConfig) -> FakeClient:
            nonlocal model_calls
            model_calls += 1
            raise AssertionError("重开不得重新派工或再次询问模型")

        reopened = Runtime(
            ConfigFile(runtime.config.path), runtime.data_dir, client_factory=no_model
        )
        reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
        try:
            async with reopened_app.router.lifespan_context(reopened_app):
                reopened_manager = reopened_app.state.channel_application.manager
                assert reopened.live_approvals() == []
                assert reopened_manager.delivery(unknown_notice["id"]) == unknown_notice
                # A new independent event is a finite barrier through the real
                # resumed consumer; it cannot recreate the old live waiter.
                barrier = await reopened.bus.emit("synthetic.approval-notice-reopen", {})
                async with asyncio.timeout(3), reopened_manager.changed:
                    await reopened_manager.changed.wait_for(
                        lambda: int(reopened_manager._state("events", "0")) >= barrier["id"]
                    )
                assert reopened_manager.delivery(unknown_notice["id"]) == unknown_notice
                assert len(platform_calls) == calls_before and unknown_attempts == 1
                assert model_calls == 0 and not target.exists()
                assert not [
                    event
                    for event in reopened.store.events(session_id=worker_id)
                    if event["type"] == "tool.started"
                ]
        finally:
            await reopened.close()
