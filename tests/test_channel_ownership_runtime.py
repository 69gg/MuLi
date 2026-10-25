"""A real owner can select an existing direct child for a channel assignment."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_history_runtime import assert_host_origin
from test_channels import onebot_body
from test_mobile_channels import onebot_payload
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.permissions import Mode
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.server import create_app


async def complete_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_mode: Mode = "yolo",
    owner_ids: list[str] | None = None,
) -> None:
    network_requests = 0

    async def no_network(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        nonlocal network_requests
        network_requests += 1
        if owner_mode == "ask":
            assert request.url.host == "onebot.invalid" and request.url.path == "/send_private_msg"
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": network_requests}}
            )
        raise AssertionError("归属登记不应发出平台或模型网络请求")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    evidence = tmp_path / "channel-helper-brief.txt"
    evidence_text = "资料队友先读完这份本地资料，正常完成后再由主人选择是否移交渠道。"
    evidence.write_text(evidence_text, encoding="utf-8")
    initial = "请派一个资料队友读取指定文件，核对原文后正常报告：" + str(evidence)
    assignment = "把当前 OneBot 主人私聊交给刚才那位资料队友；普通回答不自动外发。"
    ready_reply = "资料队友已经完成本地核对，等待主人下一步选择。"
    assignment_reply = "归属工具已尝试；结果以实际回执为准。"
    worker_reply = "本地资料已逐字核对，可以由主人决定接下来的职责。"
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "extensions": {"watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://onebot.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": owner_ids if owner_ids is not None else ["7"],
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    child_id = ""
    first_rounds = 0
    assignment_rounds = 0
    worker_rounds = 0
    accepted_reads: list[dict[str, Any]] = []
    main_runs: dict[str, str] = {}
    delegate_available: bool | None = None
    delegate_receipt: dict[str, Any] | None = None

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal child_id, first_rounds, assignment_rounds, worker_rounds
            nonlocal delegate_available, delegate_receipt
            assert len(messages) <= 32 and all(not message.images for message in messages)
            names = {tool["function"]["name"] for tool in tools}
            if self.role == "worker":
                worker_rounds += 1
                if worker_rounds == 1:
                    assert "fs_read" in names
                    turn = ModelTurn(
                        tool_calls=[call("read-channel-brief", "fs_read", path=str(evidence))]
                    )
                else:
                    assert worker_rounds == 2
                    result = tool_result(messages, "read-channel-brief")
                    assert set(result) == {"path", "content"}
                    assert result["path"] == str(evidence.resolve())
                    assert result["content"] == evidence_text
                    accepted_reads.append(result)
                    turn = ModelTurn(content=worker_reply)
            elif any(
                message.role == "user" and message.content.endswith(assignment)
                for message in messages
            ):
                assert self.role == "main"
                assert_host_origin(messages, runtime, "onebot", assignment)
                main_runs["assignment"] = runtime.run_ids[main_id]
                assignment_rounds += 1
                if assignment_rounds == 1:
                    child = runtime.team.get(child_id)
                    assert child["parent_id"] == main_id and child["status"] == "completed"
                    assert child["generation"] == 1 and child["result"] == worker_reply
                    delegate_available = "channel_delegate" in names
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "assign-owner-channel",
                                "channel_delegate",
                                action="assign",
                                channel="onebot",
                                scope="account",
                                agent_id=child_id,
                            )
                        ]
                    )
                else:
                    assert assignment_rounds == 2
                    delegate_receipt = tool_result(messages, "assign-owner-channel")
                    turn = ModelTurn(content=assignment_reply)
            else:
                assert self.role == "main" and {"spawn_agent", "wait_agents"} <= names
                main_runs["initial"] = runtime.run_ids[main_id]
                first_rounds += 1
                if first_rounds == 1:
                    assert_host_origin(messages, runtime, "onebot", initial)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "create-channel-child",
                                "spawn_agent",
                                task_name="渠道资料队友",
                                prompt="先读取并核对本地文件，不改文件、不外发：" + str(evidence),
                            )
                        ]
                    )
                elif first_rounds == 2:
                    spawned = tool_result(messages, "create-channel-child")
                    assert "error" not in spawned
                    child_id = spawned["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call("wait-channel-child", "wait_agents", targets=[child_id], timeout=2)
                        ]
                    )
                else:
                    if first_rounds == 3:
                        waited = tool_result(messages, "wait-channel-child")
                        assert waited["timed_out"] is False
                        assert any(
                            agent["id"] == child_id and agent["status"] == "completed"
                            for agent in waited["agents"]
                        )
                    turn = ModelTurn(content=ready_reply)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Client(config.model)

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": owner_mode})
        assert mode.status_code == 200
        for number, text, reply in (
            (101, initial, ready_reply),
            (102, assignment, assignment_reply),
        ):
            payload = onebot_payload(text)
            payload["message_id"] = number
            raw, headers = onebot_body(payload)
            headers["Content-Type"] = "application/json"
            response = await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            if owner_mode == "ask":

                async def approve_prefix(expected: str = reply) -> None:
                    while not completed(runtime, expected):
                        revision = runtime.bus.revision
                        for approval in runtime.live_approvals():
                            assert approval["tool"] in {
                                "spawn_agent",
                                "wait_agents",
                                "fs_read",
                                "channel_delegate",
                            }
                            await runtime.permissions.resolve(approval["id"], "approve")
                        if completed(runtime, expected):
                            return
                        async with runtime.bus.changed:
                            await runtime.bus.changed.wait_for(
                                lambda observed=revision: runtime.bus.revision != observed
                            )

                await asyncio.wait_for(approve_prefix(), timeout=4)
            else:
                await eventually(runtime, lambda expected=reply: bool(completed(runtime, expected)))
        assert (
            await asyncio.wait_for(runtime.wait(main_runs["assignment"]), timeout=4)
            == assignment_reply
        )
        child = runtime.team.get(child_id)
        report = runtime.store.get("team_report", child["run_id"])
        assert len(accepted_reads) == 1 and worker_rounds == 2
        assert child["session_id"] == child_id and child["parent_id"] == main_id
        assert child["status"] == "completed" and child["generation"] == 1
        assert report is not None and report["execution_status"] == "completed"
        assert runtime.store.get("run", child["run_id"])["status"] == "completed"
        assert runtime.store.get("run", main_runs["assignment"])["status"] == "completed"
        if owner_mode == "yolo":
            assert (
                network_requests == 0 and app.state.channel_application.manager.deliveries() == []
            )
        else:
            assert all(
                "approval_notice" in item["payload"]
                for item in app.state.channel_application.manager.deliveries()
            )
        assert delegate_receipt is not None
        assert delegate_available is True, (
            "真实主人/直属队友/资料读取均完成，缺少渠道归属工具",
            delegate_receipt,
        )
        assert "error" not in delegate_receipt


async def test_onebot_owner_can_assign_an_existing_completed_direct_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await complete_assignment(tmp_path, monkeypatch)


async def test_assigned_channel_reopens_in_the_same_child_and_reclaim_preserves_original_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reuse the complete real owner -> spawn -> file -> report -> assign prefix.
    await test_onebot_owner_can_assign_an_existing_completed_direct_child(tmp_path, monkeypatch)
    runtime, _ = make_runtime(tmp_path, [])
    main_id = runtime.main_session()["id"]
    members = runtime.team.list()
    assert len(members) == 1
    child_id = members[0]["id"]
    assert members[0]["generation"] == 1 and members[0]["status"] == "completed"
    first = "移交后第一封：保留之前核对的资料，本次只在网页回答。"
    second = "移交后第二封：请明确发送已核对的答复到 OneBot 主人7，然后在网页收口。"
    after_reclaim = "收回后由主助手处理这条消息，只在网页回答。"
    web_query = "请核查接管后的本地渠道历史，再强制收回该接入。"
    first_reply, second_reply = "接管队友的网页回答一。", "接管队友的网页回答二。"
    sent_text = "这是接管队友通过显式工具发送的答复。"
    final_reply, main_reply = "历史已核查，接入已经收回。", "主助手已接回这封新消息。"
    sent: list[dict[str, Any]] = []
    rounds: dict[str, int] = {}
    origins: dict[str, dict[str, Any]] = {}
    deliveries: list[str] = []
    history_checked = False

    async def platform(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "onebot.invalid" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert body["user_id"] == "7"
        assert body["message"] == [{"type": "text", "data": {"text": sent_text}}]
        sent.append(body)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        )

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    def source(messages: list[Message], text: str) -> None:
        stored = next(
            item
            for item in runtime.store.messages(child_id)
            if item["role"] == "user" and item["content"] == text
        )
        supplied = next(item for item in messages if item.source_message_id == stored["id"])
        prefix = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
        header, separator, original = supplied.content.partition("\n")
        assert separator and original == text and header.startswith(prefix)
        assert json.loads(header[len(prefix) :]) == {
            "channel": "onebot",
            "account_id": "123",
            "sender_id": "7",
            "target": "7",
            "kind": "private",
            "input_id": stored["id"],
        }
        assert stored["channel_ownership"]["agent_id"] == child_id
        assert stored["run_id"] == runtime.run_ids[child_id]
        origins[text] = stored

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal history_checked
            assert len(messages) <= 32 and all(not item.images for item in messages)
            latest = next(item.content for item in reversed(messages) if item.role == "user")
            phase = (
                second
                if latest.endswith(second)
                else first
                if latest.endswith(first)
                else after_reclaim
                if latest.endswith(after_reclaim)
                else web_query
            )
            rounds[phase] = rounds.get(phase, 0) + 1
            count = rounds[phase]
            if self.role == "worker":
                assert phase in {first, second}
                source(messages, phase)
                assert any("本地资料已逐字核对" in item.content for item in messages)
                if phase == first:
                    assert count == 1
                    turn = ModelTurn(content=first_reply)
                elif count == 1:
                    assert "channel_send" in {item["function"]["name"] for item in tools}
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "child-explicit-send",
                                "channel_send",
                                channel="onebot",
                                target="7",
                                text=sent_text,
                                idempotency_key="owned-message-104",
                            )
                        ]
                    )
                else:
                    assert count == 2
                    result = tool_result(messages, "child-explicit-send")
                    assert "error" not in result and result["state"] in {"pending", "sent"}
                    deliveries.append(result["id"])
                    turn = ModelTurn(content=second_reply)
            elif phase == after_reclaim:
                assert self.role == "main" and count == 1
                assert_host_origin(messages, runtime, "onebot", after_reclaim)
                turn = ModelTurn(content=main_reply)
            else:
                assert self.role == "main" and phase == web_query
                if count == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "owned-local-history",
                                "channel_history",
                                channel="onebot",
                                owner_id="7",
                                limit=40,
                            )
                        ]
                    )
                elif count == 2:
                    page = tool_result(messages, "owned-local-history")
                    assert "error" not in page and page["session_id"] == main_id
                    assert page["coverage"] == "saved_by_muli"
                    for text in (first, second):
                        item = next(
                            item
                            for item in page["incoming"]["items"]
                            if item["id"] == origins[text]["id"]
                        )
                        assert item["session_id"] == child_id and "content" not in item
                    delivery = next(
                        item for item in page["outgoing"]["items"] if item["id"] == deliveries[0]
                    )
                    assert (
                        delivery["state"] == "sent"
                        and delivery["confirmed_parts"] == delivery["total_parts"] == 1
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "owned-original-body",
                                "conversation_read",
                                session_id=child_id,
                                message_id=origins[first]["id"],
                            )
                        ]
                    )
                elif count == 3:
                    body = tool_result(messages, "owned-original-body")
                    assert "error" not in body and body["content"] == first
                    history_checked = True
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "reclaim-channel",
                                "channel_delegate",
                                action="reclaim",
                                channel="onebot",
                                scope="account",
                            )
                        ]
                    )
                else:
                    assert count == 4
                    result = tool_result(messages, "reclaim-channel")
                    assert "error" not in result and result["state"] == "main"
                    turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    async def receive(web: httpx.AsyncClient, number: int, text: str) -> None:
        payload = onebot_payload(text)
        payload["message_id"] = number
        raw, headers = onebot_body(payload)
        headers["Content-Type"] = "application/json"
        response = await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
        assert response.status_code == 200

    async def child_finished(expected: str, generation: int) -> None:
        await eventually(
            runtime,
            lambda: (
                runtime.team.get(child_id)["status"] == "completed"
                and runtime.team.get(child_id)["result"] == expected
            ),
        )
        child = runtime.team.get(child_id)
        assert child["generation"] == generation
        report = runtime.store.get("team_report", child["run_id"])
        assert (
            report is not None
            and report["status"] == "local"
            and report["execution_status"] == "completed"
        )
        assert (
            report["reply_origin"]["input_id"]
            == origins[first if generation == 2 else second]["id"]
        )
        assert runtime.store.pending_inputs(main_id) == 0 and main_id not in runtime.tasks

    runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        initial_main_runs = runtime.store.db.execute(
            "SELECT count(*) FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=?",
            (main_id,),
        ).fetchone()[0]
        await receive(web, 103, first)
        await child_finished(first_reply, 2)
        assert sent == [] and app.state.channel_application.manager.deliveries() == []
        assert (
            runtime.store.db.execute(
                "SELECT count(*) FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=?",
                (main_id,),
            ).fetchone()[0]
            == initial_main_runs
        )

    runtime, _ = make_runtime(tmp_path, [])
    runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        assert (
            runtime.main_session()["id"] == main_id
            and runtime.team.get(child_id)["generation"] == 2
        )
        count_before = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
        await receive(web, 103, first)
        assert runtime.team.get(child_id)["generation"] == 2
        assert (
            runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count_before
        )
        await receive(web, 104, second)
        await child_finished(second_reply, 3)
        manager = app.state.channel_application.manager
        await eventually(
            runtime,
            lambda: (
                bool(deliveries)
                and next(item for item in manager.deliveries() if item["id"] == deliveries[0])[
                    "state"
                ]
                == "sent"
            ),
        )
        assert len(sent) == 1
        accepted = await runtime.submit(web_query)
        assert await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4) == final_reply
        assert history_checked and runtime.team.get(child_id)["generation"] == 3
        await receive(web, 105, after_reclaim)
        await eventually(runtime, lambda: bool(completed(runtime, main_reply)))
        assert runtime.team.get(child_id)["generation"] == 3
        count_before = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
        await receive(web, 104, second)
        assert (
            runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count_before
        )
        assert len(sent) == 1 and len(manager.deliveries()) == 1
