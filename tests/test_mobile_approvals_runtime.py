"""A bound owner's raw phone command resolves a real live permission waiter.

The model policy is fixed; Runtime, Team, OneBot ingress, approval futures,
filesystem effects and automatic outbox delivery retain their real lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import onebot_body, terminal
from test_mobile_channels import onebot_payload
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.channels import ChannelManager
from muli.channels.manager import encoded
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


async def test_onebot_owner_approves_live_worker_without_retargeting_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment = "请让队友写入一份合成核查结果；实际写入须等待主人批准。"
    acknowledgement = "队友已接到任务，实际写入仍需主人批准。"
    worker_result = "队友已按这次真实批准写入合成核查结果。"
    final_reply = "已核对队友的实际写入结果；成果仍回原任务私聊。"
    content = "合成核查结果：由真实 fs_write 写入。\nMOBILE-APPROVAL-59\n"
    target = tmp_path / "approved-result.txt"
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
            "server": {"password": "synthetic-mobile-approval-owner"},
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
    main_rounds = worker_rounds = 0
    worker_id = ""
    runs: dict[str, str] = {}

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_rounds, worker_id
            main_rounds += 1
            assert len(messages) <= 32 and main_rounds <= 3
            role = "assignment" if main_rounds <= 2 else "report"
            assert runs.setdefault(role, runtime.run_ids[main_id]) == runtime.run_ids[main_id]
            if main_rounds == 1:
                assert any(
                    message.role == "user" and message.content == assignment for message in messages
                )
                assert "spawn_agent" in {tool["function"]["name"] for tool in tools}
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-approved-worker",
                            "spawn_agent",
                            task_name="手机批准核查队友",
                            prompt="只调用一次 fs_write 写入合成核查结果；等待真实批准，不更换权限模式。",
                        )
                    ]
                )
            elif main_rounds == 2:
                spawned = tool_result(messages, "spawn-approved-worker")
                assert "error" not in spawned
                worker_id = str(spawned["id"])
                turn = ModelTurn(content=acknowledgement)
            else:
                report = runtime.store.get("team_report", runs["worker"])
                assert report is not None and report["execution_status"] == "completed"
                assert report["agent_id"] == worker_id and report["parent_id"] == main_id
                assert json.loads(report["content"].split("\n", 1)[1])["result"] == worker_result
                assert any(
                    message.role == "user" and worker_result in message.content
                    for message in messages
                )
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
                        call("write-approved-result", "fs_write", path=str(target), content=content)
                    ]
                )
            else:
                written = tool_result(messages, "write-approved-result")
                assert "error" not in written
                assert target.read_text(encoding="utf-8") == content
                turn = ModelTurn(content=worker_result)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "worker", "reviewer"}
        if config.model == "main":
            return Main([], [])
        if config.model == "worker":
            return Worker([], [])
        return FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    manager = app.state.channel_application.manager

    def diagnostic() -> str:
        states: dict[str, Any] = {}
        for role, identifier in runs.items():
            item = runtime.store.get("run", identifier)
            if item is not None:
                states[role] = {
                    "status": item["status"],
                    "error_type": (item.get("failure_diagnostic") or {}).get("exception_type"),
                    "error_present": bool(item.get("error")),
                }
        return json.dumps(
            {
                "runs": states,
                "rounds": [main_rounds, worker_rounds],
                "live": len(runtime.live_approvals()),
            },
            ensure_ascii=True,
        )[:1024]

    async def wait_reply(text: str, role: str) -> None:
        try:
            await eventually(runtime, lambda: bool(completed(runtime, text)))
        except TimeoutError:
            pytest.fail(diagnostic())
        async with asyncio.timeout(3):
            assert await runtime.wait(runs[role]) == text

    def command_key(message_id: int) -> str:
        return hashlib.sha256(
            encoded(["onebot", "123", str(message_id)]).encode("utf-8")
        ).hexdigest()

    def command_receipt(message_id: int) -> tuple[str, dict[str, Any]]:
        row = runtime.store.db.execute(
            "SELECT receipt FROM channel_inbound WHERE id=?", (command_key(message_id),)
        ).fetchone()
        assert row is not None and isinstance(row["receipt"], str)
        return row["receipt"], json.loads(row["receipt"])

    def write_events(kind: str) -> list[dict[str, Any]]:
        return [
            event
            for event in runtime.store.events(session_id=worker_id)
            if event["type"] == kind and event["data"].get("call_id") == "write-approved-result"
        ]

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            login = await web.post(
                "/api/auth/login", json={"password": "synthetic-mobile-approval-owner"}
            )
            assert login.status_code == 200
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "ask"})
            assert mode.status_code == 200 and mode.json()["mode"] == "ask"
            assert (await phone(web, assignment, 5901)).status_code == 200
            await eventually(
                runtime,
                lambda: any(item["tool"] == "spawn_agent" for item in runtime.live_approvals()),
            )
            spawn = runtime.live_approvals()[0]
            assert spawn["session_id"] == main_id and spawn["tool"] == "spawn_agent"
            # Use the existing authenticated Web approval only for dispatch, so
            # the phone feature is tested against the child's actual fs_write.
            dispatched = await web.post(
                f"/api/approvals/{spawn['id']}", json={"decision": "approve"}
            )
            assert dispatched.status_code == 200
            await wait_reply(acknowledgement, "assignment")
            await eventually(
                runtime,
                lambda: any(item["tool"] == "fs_write" for item in runtime.live_approvals()),
            )
            pending = runtime.live_approvals()
            assert len(pending) == 1
            approval = pending[0]
            worker = runtime.team.get(worker_id)
            runs["worker"] = str(worker["run_id"])
            assert worker["parent_id"] == main_id and worker["status"] == "running"
            assert worker["mode"] == runtime.store.get("session", worker_id)["mode"] == "ask"
            assert approval["session_id"] == worker_id and approval["run_id"] == runs["worker"]
            assert approval["arguments"] == {"path": str(target), "content": content}
            future = runtime.permissions.pending[approval["id"]]
            assert not future.done() and not target.exists() and write_events("tool.started") == []
            original_origin = worker["reply_origin"]
            assert (
                original_origin["route"]["target"] == original_origin["route"]["sender_id"] == "7"
            )
            original_inputs = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]

            raw_query = onebot_payload("/审批")
            raw_query.update(message_id=5902, user_id=8)
            incoming = manager.adapters["onebot"].webhook(raw_query)[0]
            assert incoming.sender_id == incoming.target == "8" and incoming.kind == "private"
            assert incoming.control_text == "/审批" and incoming.control_account_id == "123", (
                "审批命令须先由原适配器保留可信原始文本，不能退回普通 owner 模型输入"
            )
            assert (await phone(web, "/审批", 5902, sender=8)).status_code == 200
            _, listed = command_receipt(5902)
            assert not future.done() and runtime.team.get(worker_id)["status"] == "running"
            assert not target.exists() and main_rounds == 2 and worker_rounds == 1
            assert (
                runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
                == original_inputs
            )
            # Target red: the real waiter exists and the original trusted
            # command has completed; only the host approval dispatch is absent.
            assert listed.get("state") == "mobile_approval", (
                "真实队友正等待 fs_write 审批，手机8原始私聊已通过可信控制入口；"
                f"查询却未产生手机审批回执：{listed.get('state')!r}"
            )
            identifiers = set(re.findall(r"^[0-9a-f]{32}$", listed["reply_text"], re.MULTILINE))
            assert identifiers == {approval["id"]} and "fs_write" in listed["reply_text"]
            assert f"会话：{worker_id}" in listed["reply_text"]
            assert f"运行：{runs['worker']}" in listed["reply_text"]
            selected_id = next(iter(identifiers))
            assert (await phone(web, f"/审批 {selected_id}", 5903, sender=8)).status_code == 200
            _, detail = command_receipt(5903)
            assert detail["state"] == "mobile_approval"
            assert selected_id in detail["reply_text"] and "fs_write" in detail["reply_text"]
            assert f"会话：{worker_id}" in detail["reply_text"]
            assert f"运行：{runs['worker']}" in detail["reply_text"]
            assert not future.done() and not target.exists()
            assert (
                runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
                == original_inputs
            )

            command = f"/批准 {selected_id}"
            assert (await phone(web, command, 5904, sender=8)).status_code == 200
            raw_receipt, decision = command_receipt(5904)
            assert decision["state"] == "mobile_approval" and selected_id in decision["reply_text"]
            await wait_reply(final_reply, "report")
            agent = runtime.team.get(worker_id)
            assert agent["status"] == "completed" and agent["reply_origin"] == original_origin
            assert runtime.store.get("run", runs["worker"])["status"] == "completed"
            assert target.read_text(encoding="utf-8") == content
            assert len(write_events("tool.started")) == len(write_events("tool.completed")) == 1
            actual_result = next(
                message
                for message in runtime.store.messages(worker_id)
                if message.get("tool_call_id") == "write-approved-result"
            )
            assert actual_result["content_provenance"] == "accepted"
            assert "error" not in json.loads(actual_result["content"])
            assert runtime.live_approvals() == []
            assert runtime.store.get("approval", selected_id)["status"] == "approved"
            resolved = [
                event["id"]
                for event in runtime.store.events(session_id=worker_id)
                if event["type"] == "approval.resolved" and event["data"].get("id") == selected_id
            ]
            assert len(resolved) == 1
            reply = completed(runtime, final_reply)[0]
            assert reply["reply_origin"] == original_origin
            await processed_reply(runtime, manager, reply["id"])
            for item in manager.deliveries():
                delivered = await terminal(manager, item["id"])
                assert delivered["state"] == "sent"
            result_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == final_reply
            ]
            assert len(result_deliveries) == 1 and result_deliveries[0]["route"]["target"] == "7"
            confirmation = [
                item
                for item in manager.deliveries()
                if delivery_text(item) == decision["reply_text"]
            ]
            assert len(confirmation) == 1 and confirmation[0]["route"]["target"] == "8"
            actual_replies = [
                item
                for item in platform_calls
                if "".join(segment["data"]["text"] for segment in item["message"]) == final_reply
            ]
            assert len(actual_replies) == 1 and str(actual_replies[0]["user_id"]) == "7"
            assert all(
                str(item["user_id"]) == "7"
                for item in platform_calls
                if worker_result in "".join(segment["data"]["text"] for segment in item["message"])
            )
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(message["content"] == final_reply for message in history.json()["messages"])
            assert [
                message["content"]
                for message in runtime.store.messages(main_id)
                if message.get("principal") == "owner"
            ] == [assignment]

            before = {item["id"] for item in manager.deliveries()}
            assert (await phone(web, command, 5904, sender=8)).status_code == 200
            assert command_receipt(5904) == (raw_receipt, decision)
            assert {item["id"] for item in manager.deliveries()} == before
            assert [
                event["id"]
                for event in runtime.store.events(session_id=worker_id)
                if event["type"] == "approval.resolved" and event["data"].get("id") == selected_id
            ] == resolved
            assert len(write_events("tool.started")) == 1
            assert main_rounds == 3 and worker_rounds == 2
            assert (
                runtime.store.pending_inputs(main_id)
                == runtime.store.pending_inputs(worker_id)
                == 0
            )
    finally:
        await runtime.close()


@dataclass
class _LivePhoneApproval:
    runtime: Runtime
    web: httpx.AsyncClient
    manager: ChannelManager
    session_id: str
    run_id: str
    approval: dict[str, Any]
    path: Path
    seen: list[list[Message]]
    platform_calls: list[dict[str, Any]]


def _phone_key(message_id: int) -> str:
    return hashlib.sha256(encoded(["onebot", "123", str(message_id)]).encode("utf-8")).hexdigest()


def _phone_receipt(runtime: Runtime, message_id: int) -> tuple[str, dict[str, Any]]:
    row = runtime.store.db.execute(
        "SELECT receipt FROM channel_inbound WHERE id=?", (_phone_key(message_id),)
    ).fetchone()
    assert row is not None and isinstance(row["receipt"], str)
    return row["receipt"], json.loads(row["receipt"])


def _approval_events(waiter: _LivePhoneApproval, kind: str) -> list[dict[str, Any]]:
    return [
        item
        for item in waiter.runtime.store.events(session_id=waiter.session_id)
        if item["type"] == kind
        and (
            item["data"].get("id") == waiter.approval["id"]
            if kind == "approval.resolved"
            else item["data"].get("call_id") == "boundary-write"
        )
    ]


@asynccontextmanager
async def _waiting_phone_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    uncertain_ack: bool = False,
) -> AsyncIterator[_LivePhoneApproval]:
    """A small host-boundary fixture; the Team/phone user story is above."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "boundary-write.txt"
    content = "合成审批边界：真实工具副作用。"
    calls: list[dict[str, Any]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        calls.append(json.loads(request.content))
        if uncertain_ack and len(calls) == 1:
            raise httpx.ReadTimeout("one unconfirmed approval acknowledgement", request=request)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(calls)}}
        )

    transport = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(
                tool_calls=[call("boundary-write", "fs_write", path=str(path), content=content)]
            ),
            ModelTurn(content="本轮按真实审批结果结束。"),
        ],
    )
    runtime.config.update(
        {
            "default_mode": "ask",
            "server": {"password": "synthetic-mobile-boundary-owner"},
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
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://localhost:8765",
            ) as web,
        ):
            assert (
                await web.post(
                    "/api/auth/login", json={"password": "synthetic-mobile-boundary-owner"}
                )
            ).status_code == 200
            created = await web.post("/api/sessions", json={"title": "审批边界", "mode": "ask"})
            assert created.status_code == 200
            session_id = created.json()["id"]
            started = await web.post(
                f"/api/sessions/{session_id}/messages",
                json={"content": "写入临时合成资料，等待本次批准。"},
            )
            assert started.status_code == 200
            run_id = started.json()["run_id"]
            await eventually(runtime, lambda: len(runtime.live_approvals()) == 1)
            approval = runtime.live_approvals()[0]
            assert approval["session_id"] == session_id and approval["run_id"] == run_id
            assert approval["tool"] == "fs_write" and not path.exists() and len(seen) == 1
            assert not runtime.permissions.pending[approval["id"]].done()
            assert all(len(messages) <= 32 for messages in seen)
            yield _LivePhoneApproval(
                runtime,
                web,
                app.state.channel_application.manager,
                session_id,
                run_id,
                approval,
                path,
                seen,
                calls,
            )
    finally:
        await runtime.close()


async def test_mobile_approval_receipt_failure_rolls_back_decision_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _waiting_phone_approval(tmp_path, monkeypatch) as waiter:
        runtime = waiter.runtime
        future = runtime.permissions.pending[waiter.approval["id"]]
        with runtime.store.db:
            runtime.store.db.execute(
                "CREATE TEMP TRIGGER fail_mobile_approval_receipt BEFORE UPDATE OF receipt "
                "ON channel_inbound WHEN NEW.receipt IS NOT NULL BEGIN "
                "SELECT RAISE(ABORT,'synthetic receipt write failure'); END"
            )
        response = await phone(waiter.web, f"/批准 {waiter.approval['id']}", 5910, sender=8)
        assert response.status_code == 500
        row = runtime.store.db.execute(
            "SELECT receipt FROM channel_inbound WHERE id=?", (_phone_key(5910),)
        ).fetchone()
        assert row is not None and row["receipt"] is None
        assert runtime.store.get("approval", waiter.approval["id"])["status"] == "pending"
        assert not future.done() and runtime.live_approvals() == [waiter.approval]
        assert _approval_events(waiter, "approval.resolved") == []
        assert _approval_events(waiter, "tool.started") == []
        assert not waiter.path.exists() and len(waiter.seen) == 1
        assert waiter.manager.deliveries() == [] and waiter.platform_calls == []
        with runtime.store.db:
            runtime.store.db.execute("DROP TRIGGER fail_mobile_approval_receipt")
        # The same authentic envelope can now commit once; no waiter was replaced.
        repeated = await phone(waiter.web, f"/批准 {waiter.approval['id']}", 5910, sender=8)
        assert repeated.status_code == 200
        async with asyncio.timeout(3):
            await runtime.wait(waiter.run_id)
        assert waiter.path.read_text(encoding="utf-8") == "合成审批边界：真实工具副作用。"
        assert len(_approval_events(waiter, "approval.resolved")) == 1
        assert len(_approval_events(waiter, "tool.started")) == 1


async def test_web_and_phone_decisions_only_accept_the_first_live_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for first in ("web", "phone"):
        async with _waiting_phone_approval(tmp_path / first, monkeypatch) as waiter:
            identifier = waiter.approval["id"]
            if first == "web":
                response = await waiter.web.post(
                    f"/api/approvals/{identifier}", json={"decision": "reject"}
                )
                assert response.status_code == 200
                async with asyncio.timeout(3):
                    await waiter.runtime.wait(waiter.run_id)
                stale = await phone(waiter.web, f"/批准 {identifier}", 5911, sender=8)
                assert stale.status_code == 200
                _, receipt = _phone_receipt(waiter.runtime, 5911)
                assert receipt["state"] == "mobile_approval"
                assert receipt["mobile_approval"]["error"] == "not_live"
                expected = "rejected"
                assert not waiter.path.exists() and _approval_events(waiter, "tool.started") == []
            else:
                response = await phone(waiter.web, f"/批准 {identifier}", 5912, sender=8)
                assert response.status_code == 200
                async with asyncio.timeout(3):
                    await waiter.runtime.wait(waiter.run_id)
                stale = await waiter.web.post(
                    f"/api/approvals/{identifier}", json={"decision": "reject"}
                )
                assert stale.status_code == 400
                expected = "approved"
                assert waiter.path.read_text(encoding="utf-8") == "合成审批边界：真实工具副作用。"
                assert len(_approval_events(waiter, "tool.started")) == 1
            assert waiter.runtime.store.get("approval", identifier)["status"] == expected
            assert len(_approval_events(waiter, "approval.resolved")) == 1
            assert waiter.runtime.live_approvals() == [] and len(waiter.seen) == 2


async def test_mobile_approval_rejects_unproved_sources_and_rechecks_locked_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _waiting_phone_approval(tmp_path, monkeypatch) as waiter:
        runtime = waiter.runtime
        command = f"/批准 {waiter.approval['id']}"
        for index, damage in enumerate(("group", "quoted", "foreign"), 5920):
            payload = onebot_payload(command)
            payload.update(message_id=index, user_id=8)
            if damage == "group":
                payload.update(message_type="group", group_id=99)
            elif damage == "quoted":
                payload["message"].insert(0, {"type": "reply", "data": {"id": "prior"}})
            else:
                payload["user_id"] = 9
            raw, headers = onebot_body(payload)
            headers["Content-Type"] = "application/json"
            response = await waiter.web.post(
                "/api/channels/onebot/webhook", content=raw, headers=headers
            )
            # Signed webhooks acknowledge deliberately rejected messages, while
            # the host refuses authority and creates no control receipt.
            assert response.status_code == 200
            assert (
                runtime.store.db.execute(
                    "SELECT count(*) FROM channel_inbound WHERE id=?", (_phone_key(index),)
                ).fetchone()[0]
                == 0
            )
        entered = asyncio.Event()
        original_submit = runtime.submit_mobile_approval

        async def observed_submit(**kwargs: Any) -> dict[str, Any]:
            entered.set()
            return await original_submit(**kwargs)

        monkeypatch.setattr(runtime, "submit_mobile_approval", observed_submit)
        operation: asyncio.Task[httpx.Response] | None = None
        try:
            async with runtime.configuration_lock:
                operation = asyncio.create_task(phone(waiter.web, command, 5923, sender=8))
                await asyncio.wait_for(entered.wait(), 3)
                runtime.config.update({"channels": {"onebot": {"owner_ids": ["7"]}}})
            response = await operation
            assert response.status_code == 200
            row = runtime.store.db.execute(
                "SELECT receipt FROM channel_inbound WHERE id=?", (_phone_key(5923),)
            ).fetchone()
            assert row is not None and row["receipt"] is None
            assert not runtime.permissions.pending[waiter.approval["id"]].done()
            assert runtime.store.get("approval", waiter.approval["id"])["status"] == "pending"
            assert _approval_events(waiter, "approval.resolved") == []
            assert _approval_events(waiter, "tool.started") == [] and not waiter.path.exists()
            assert waiter.manager.deliveries() == [] and waiter.platform_calls == []
            assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0
            assert len(waiter.seen) == 1
        finally:
            if operation is not None and not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)


async def test_mobile_approval_lost_notification_replays_receipt_without_second_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _waiting_phone_approval(tmp_path, monkeypatch) as waiter:
        runtime = waiter.runtime
        original_notify = runtime.bus.notify
        failures = 0

        async def lost_notify() -> None:
            nonlocal failures
            row = runtime.store.db.execute(
                "SELECT receipt IS NOT NULL FROM channel_inbound WHERE id=?", (_phone_key(5930),)
            ).fetchone()
            if failures == 0 and row is not None and row[0]:
                failures += 1
                raise RuntimeError("synthetic post-commit notification failure")
            await original_notify()

        monkeypatch.setattr(runtime.bus, "notify", lost_notify)
        command = f"/批准 {waiter.approval['id']}"
        response = await phone(waiter.web, command, 5930, sender=8)
        assert response.status_code == 500 and failures == 1
        saved = _phone_receipt(runtime, 5930)
        assert saved[1]["state"] == "mobile_approval"
        assert runtime.store.get("approval", waiter.approval["id"])["status"] == "approved"
        async with asyncio.timeout(3):
            await runtime.wait(waiter.run_id)
        assert len(_approval_events(waiter, "approval.resolved")) == 1
        assert len(_approval_events(waiter, "tool.started")) == 1 and waiter.path.exists()
        assert waiter.manager.deliveries() == [] and waiter.platform_calls == []
        monkeypatch.setattr(runtime.bus, "notify", original_notify)
        for _ in range(2):
            replayed = await phone(waiter.web, command, 5930, sender=8)
            assert replayed.status_code == 200 and _phone_receipt(runtime, 5930) == saved
        deliveries = waiter.manager.deliveries()
        assert len(deliveries) == 1
        assert (await terminal(waiter.manager, deliveries[0]["id"]))["state"] == "sent"
        assert len(waiter.platform_calls) == 1 and len(waiter.seen) == 2
        assert len(_approval_events(waiter, "approval.resolved")) == 1
        assert len(_approval_events(waiter, "tool.started")) == 1


async def test_mobile_approval_unknown_confirmation_survives_reopen_without_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _waiting_phone_approval(tmp_path, monkeypatch, uncertain_ack=True) as waiter:
        identifier = waiter.approval["id"]
        command = f"/批准 {identifier}"
        response = await phone(waiter.web, command, 5940, sender=8)
        assert response.status_code == 200
        saved = _phone_receipt(waiter.runtime, 5940)
        async with asyncio.timeout(3):
            await waiter.runtime.wait(waiter.run_id)
        deliveries = waiter.manager.deliveries()
        assert len(deliveries) == 1
        unknown = await terminal(waiter.manager, deliveries[0]["id"])
        assert unknown["state"] == "unknown" and unknown["attempts"] == 1
        assert unknown["receipts"] == [] and unknown["next_part"] == 0
        assert unknown["route"]["target"] == "8" and len(waiter.platform_calls) == 1
        assert len(_approval_events(waiter, "approval.resolved")) == 1
        assert len(_approval_events(waiter, "tool.started")) == 1
        config_path, data_dir = waiter.runtime.config.path, waiter.runtime.data_dir
        before_runs = waiter.runtime.store.list("run")
        old_session_id, old_run_id = waiter.session_id, waiter.run_id
        calls = waiter.platform_calls
        path = waiter.path

    model_calls = 0

    def no_model(config: ModelConfig) -> FakeClient:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("normal reopen and approval receipts must not replay a model call")

    reopened = Runtime(ConfigFile(config_path), data_dir, client_factory=no_model)
    app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                await web.post(
                    "/api/auth/login", json={"password": "synthetic-mobile-boundary-owner"}
                )
            ).status_code == 200
            manager = app.state.channel_application.manager
            assert reopened.live_approvals() == [] and not reopened.permissions.pending
            assert reopened.store.get("run", old_run_id)["status"] == "completed"
            assert manager.delivery(unknown["id"]) == unknown
            replayed = await phone(web, command, 5940, sender=8)
            assert replayed.status_code == 200
            assert _phone_receipt(reopened, 5940) == saved
            assert manager.delivery(unknown["id"]) == unknown and len(calls) == 1
            assert reopened.store.list("run") == before_runs and model_calls == 0
            assert path.read_text(encoding="utf-8") == "合成审批边界：真实工具副作用。"
            # A new explicit command is not a retry of the old acknowledgement:
            # its old approval ID must now be refused, without recreating a waiter.
            stale = await phone(web, command, 5941, sender=8)
            assert stale.status_code == 200
            _, rejected = _phone_receipt(reopened, 5941)
            assert rejected["mobile_approval"]["error"] == "not_live"
            fresh = [item for item in manager.deliveries() if item["id"] != unknown["id"]]
            assert len(fresh) == 1 and (await terminal(manager, fresh[0]["id"]))["state"] == "sent"
            assert manager.delivery(unknown["id"]) == unknown and len(calls) == 2
            assert reopened.live_approvals() == [] and reopened.store.list("run") == before_runs
            assert (
                len(
                    [
                        event
                        for event in reopened.store.events(session_id=old_session_id)
                        if event["type"] == "approval.resolved"
                        and event["data"].get("id") == identifier
                    ]
                )
                == 1
            )
            assert model_calls == 0
    finally:
        await reopened.close()
