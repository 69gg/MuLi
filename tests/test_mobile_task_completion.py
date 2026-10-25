"""Offline completion journeys through real control, Team, Store and channel lifecycles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from test_channels import terminal
from test_mobile_task_control_followup import (
    approve_reply_send,
    assert_one_replan,
    command_code,
    delivery_text,
    phone,
    processed_reply,
)
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.channels.manager import encoded
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app


@pytest.mark.parametrize(
    ("outcome", "notice_path", "damage"),
    [
        ("sent", "archive", None),
        ("blocked", "archive", None),
        ("unknown", "archive", None),
        ("sent", "ingress", None),
        ("sent", "archive", "binding_version"),
        ("sent", "archive", "binding_run"),
        ("sent", "archive", "binding_generation"),
        ("sent", "archive", "binding_revision"),
        ("sent", "archive", "source_version"),
        ("sent", "archive", "archive_message"),
        ("sent", "archive", "channel_binding_version"),
        ("sent", "archive", "notice_content_alias"),
    ],
)
async def test_completed_mobile_plan_returns_to_its_owner_after_another_chat_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["sent", "blocked", "unknown"],
    notice_path: Literal["archive", "ingress"],
    damage: str | None,
) -> None:
    initial_reply = "网页任务已登记，原队友正在核查。"
    requirement = "只交草稿，不得发布"
    plan = "核查项目现状，只交草稿，不得发布。"
    replan_reply = "方案已确认，原队友继续核查并交草稿。"
    other_reply = "已收到另一私聊的日常问候。"
    draft_content = "项目核查草稿：保留证据，未部署或发布。"
    worker_result = "核查完成，草稿已写入临时工作区，没有部署或发布。"
    completion_reply = "原任务已完成：队友已提交核查草稿，没有部署或发布。"
    forbidden_path = tmp_path / "must-not-be-published.txt"
    draft_path = tmp_path / "review-draft.txt"
    requests: list[dict[str, Any]] = []
    completion_attempts: list[httpx.Request] = []
    original_channel = damage == "channel_binding_version"

    async def platform(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.url.path == "/send_private_msg" and request.method == "POST"
        payload = json.loads(request.content)
        requests.append(payload)
        text = "".join(segment["data"]["text"] for segment in payload["message"])
        if text == completion_reply:
            completion_attempts.append(request)
            if outcome == "unknown":
                assert len(completion_attempts) == 1
                raise httpx.ReadTimeout(
                    "one explicit unconfirmed completion reply", request=request
                )
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(requests)}}
        )

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
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
    main_id = runtime.main_session()["id"]
    store_instance = runtime.store.instance_id
    task_id, worker_id = "", ""
    phase = "assignment"
    active_runtime = runtime
    main_seen: dict[str, list[list[Message]]] = {
        name: [] for name in ("assignment", "replan", "other_private", "completion")
    }
    main_runs: dict[str, str] = {}
    worker_seen: list[list[Message]] = []
    worker_launches = 0
    worker_entered, finish_old_request = asyncio.Event(), asyncio.Event()
    resumed_entered, finish_resumed_request = asyncio.Event(), asyncio.Event()
    completed_report_id = ""
    alias_id = ""
    removed_notice_ids: list[str] = []
    completion_receipt: dict[str, Any] = {}
    prior_delivery_ids: set[str] = set()

    async def remove_exact_notice_keep_ordinary_alias(
        session_id: str, messages: list[Message]
    ) -> list[Message]:
        if damage != "notice_content_alias" or session_id != worker_id or not alias_id:
            return messages
        delivery = runtime.store.list("task_plan_delivery")[0]
        notice = runtime.store.get("message", delivery["archive_message_id"])
        alias = runtime.store.get("message", alias_id)
        assert notice is not None and alias is not None
        assert notice["id"] != alias["id"] and notice["seq"] < alias["seq"]
        assert notice["content"] == alias["content"] and "task_control" not in alias
        assert alias["run_id"] == runtime.run_ids[worker_id]
        matching = [
            index
            for index, message in enumerate(messages)
            if message.role == "user" and message.content == notice["content"]
        ]
        assert len(matching) == 2
        # _history preserves these real user records' order; drop the earlier exact notice.
        removed_notice_ids.append(notice["id"])
        return [message for index, message in enumerate(messages) if index != matching[0]]

    runtime.context_hooks.append(remove_exact_notice_keep_ordinary_alias)

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id
            seen = main_seen[phase]
            seen.append(list(messages))
            run_id = active_runtime.run_ids[main_id]
            assert main_runs.setdefault(phase, run_id) == run_id
            if phase == "assignment":
                if len(seen) == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-task",
                                "commitment_save",
                                title="网页持续任务",
                                brief={"goal": "核查项目现状", "constraints": ["不可部署"]},
                            )
                        ]
                    )
                elif len(seen) == 2:
                    task_id = tool_result(messages, "save-task")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-original",
                                "spawn_agent",
                                task_name="原队友",
                                prompt="核查项目现状，等待完整后续要求。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert len(seen) == 3
                    worker_id = tool_result(messages, "spawn-original")["id"]
                    turn = ModelTurn(content=initial_reply)
            elif phase == "replan":
                if len(seen) == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "read-current",
                                "commitment_read",
                                json.dumps({"identifier": task_id}),
                            )
                        ]
                    )
                elif len(seen) == 2:
                    assert "error" not in tool_result(messages, "read-current")
                    assert any(
                        "完整受控任务说明" in message.content
                        and requirement in message.content
                        and '"epoch":2' in message.content
                        for message in messages
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "approve-current",
                                "task_approve_plan",
                                observed_epoch=2,
                                plan=plan,
                                targets=[worker_id],
                            )
                        ]
                    )
                else:
                    assert len(seen) == 3
                    assert (
                        tool_result(messages, "approve-current")["current"]["control"]["paused"]
                        is False
                    )
                    turn = ModelTurn(content=replan_reply)
            elif phase == "other_private":
                assert len(seen) == 1
                turn = ModelTurn(content=other_reply)
            else:
                assert phase == "completion"
                if len(seen) == 1:
                    report = active_runtime.store.get("team_report", completed_report_id)
                    assert report is not None and report["status"] == "delivered"
                    assert report["execution_status"] == "completed"
                    delivered = [
                        item
                        for item in active_runtime.store.messages(main_id)
                        if item.get("source") == f"team:{worker_id}"
                        and item.get("content") == report["content"]
                    ]
                    assert len(delivered) == 1
                    assert delivered[0]["run_id"] == run_id
                    assert delivered[0]["source"] == f"team:{worker_id}"
                    assert delivered[0]["principal"] == f"agent:{worker_id}"
                    assert delivered[0]["content"] == report["content"]
                    assert any(worker_result in message.content for message in messages)
                    if damage is None:
                        # The model explicitly chooses the still-bound owner; a
                        # report's stored origin alone no longer sends anything.
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "send-completion",
                                    "channel_send",
                                    channel="onebot",
                                    target="7",
                                    text=completion_reply,
                                    idempotency_key="mobile-task-completion",
                                )
                            ]
                        )
                    else:
                        turn = ModelTurn(content=completion_reply)
                else:
                    assert damage is None and len(seen) == 2
                    completion_receipt.update(tool_result(messages, "send-completion"))
                    if outcome == "blocked":
                        assert "owner_ids" in completion_receipt["error"]
                    else:
                        assert "error" not in completion_receipt
                        assert completion_receipt["state"] in {
                            "pending",
                            "sending",
                            "sent",
                            "unknown",
                        }
                    turn = ModelTurn(content=completion_reply)
            yield StreamEvent("done", turn=turn)

    class OriginalWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            worker_entered.set()
            await finish_old_request.wait()
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[
                        call(
                            "old-write", "fs_write", path=str(forbidden_path), content="not allowed"
                        )
                    ]
                ),
            )

    class ResumedWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            worker_seen.append(list(messages))
            if len(worker_seen) == 1:
                assert any(
                    requirement in message.content and plan in message.content
                    for message in messages
                )
                if notice_path == "ingress":
                    assert "report_binding" not in active_runtime.team.get(worker_id)
                resumed_entered.set()
                await finish_resumed_request.wait()
            if notice_path == "ingress" and len(worker_seen) == 1:
                # The real pending-input fence must consume the notice before any write.
                turn = ModelTurn(content="已收到内部补充，先承接准确的计划通知。")
            elif len(worker_seen) == (2 if notice_path == "ingress" else 1):
                if notice_path == "ingress":
                    binding = active_runtime.team.get(worker_id)["report_binding"]
                    assert binding["run_id"] == active_runtime.run_ids[worker_id]
                turn = ModelTurn(
                    tool_calls=[
                        call("write-draft", "fs_write", path=str(draft_path), content=draft_content)
                    ]
                )
            else:
                assert len(worker_seen) == (3 if notice_path == "ingress" else 2)
                assert tool_result(messages, "write-draft")["path"] == str(draft_path)
                assert draft_path.read_text(encoding="utf-8") == draft_content
                if notice_path == "ingress":
                    binding = active_runtime.team.get(worker_id)["report_binding"]
                    assert binding["run_id"] == active_runtime.run_ids[worker_id]
                if damage is None:
                    control = active_runtime.task_control
                    assert control is not None
                    agent = active_runtime.team.get(worker_id)
                    notice = active_runtime.store.get(
                        "message", agent["report_binding"]["notice_id"]
                    )
                    assert (
                        control._report_candidate(agent, agent["run_id"], notice)
                        == agent["report_binding"]
                    )
                    # The same genuine evidence cannot become a different real run's notice.
                    other_run = main_runs["other_private"]
                    assert (
                        control._report_candidate({**agent, "run_id": other_run}, other_run, notice)
                        is None
                    )
                    # A direct child's proof cannot be used with a teammate parent.
                    assert (
                        control._report_candidate(
                            {**agent, "parent_id": worker_id}, agent["run_id"], notice
                        )
                        is None
                    )
                turn = ModelTurn(content=worker_result)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        nonlocal worker_launches
        if config.model == "worker":
            worker_launches += 1
            assert worker_launches <= 2
            return OriginalWorker([], []) if worker_launches == 1 else ResumedWorker([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    # PluginManager captures its base factory during create_app; keep that lifecycle intact.
    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    original_origin: dict[str, Any] = {}
    control_origin: dict[str, Any] = {}
    paused_report: dict[str, Any] = {}
    generation, completion_revision = 0, 0
    adjustment_text = ""
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            manager = app.state.channel_application.manager
            if original_channel:
                assigned = await phone(web, "请核查项目现状，不可部署。", 100, sender=8)
                assert assigned.status_code == 200
                await eventually(runtime, lambda: bool(completed(runtime, initial_reply)))
                assert await runtime.wait(main_runs["assignment"]) == initial_reply
                initial_message = completed(runtime, initial_reply)[0]
                await processed_reply(runtime, manager, initial_message["id"])
                assert not any(
                    delivery_text(item) == initial_reply for item in manager.deliveries()
                )
            else:
                assigned = await web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={"content": "请核查项目现状，不可部署。"},
                )
                assert assigned.status_code == 200
                assert await runtime.wait(assigned.json()["run_id"]) == initial_reply
            await asyncio.wait_for(worker_entered.wait(), 3)
            worker = runtime.team.get(worker_id)
            assert worker["commitment_id"] == task_id and worker["parent_id"] == main_id
            original_origin = worker["reply_origin"]
            if original_channel:
                assignment_input = runtime.store.get("message", original_origin["input_id"])
                assert assignment_input is not None
                assert assignment_input["content"] == "请核查项目现状，不可部署。"
                assert assignment_input["source"] == "channel:onebot:123"
                assert assignment_input["principal"] == "owner"
                assert assignment_input["run_id"] == main_runs["assignment"]
                assert (
                    original_origin
                    == assignment_input["reply_origin"]
                    == initial_message["reply_origin"]
                )
                assert original_origin["kind"] == "channel"
                assert original_origin["route"] == assignment_input["route"]
                assert (
                    original_origin["route"]["target"]
                    == original_origin["route"]["sender_id"]
                    == "8"
                )
                assert str(original_origin["route"]["reply_to"]) == "100"
            else:
                assert original_origin == {
                    "kind": "web",
                    "input_id": assigned.json()["message"]["id"],
                }
            generation = worker["generation"]
            assert (await phone(web, "/任务", 101)).status_code == 200
            assert (
                await phone(web, f"/暂停 {command_code(manager, '暂停')}", 102)
            ).status_code == 200
            if notice_path == "archive":
                finish_old_request.set()
                await wait_agent(runtime, worker_id, "paused")
                old_report = next(
                    item
                    for item in runtime.store.list("team_report")
                    if item["agent_id"] == worker_id and item["execution_status"] == "paused"
                )
                assert old_report["status"] == "pending"
                assert old_report["reply_origin"] == original_origin
            else:
                queued = await web.post(
                    f"/api/team/{worker_id}/messages", json={"content": "保留这条真实内部补充。"}
                )
                assert queued.status_code == 200
                claim_inputs = runtime.store.claim_inputs

                def claim_one_worker_input(
                    session_id: str, run_id: str, *, limit: int
                ) -> list[dict[str, Any]]:
                    # Exercise a smaller real claim; do not manufacture or skip an ingress row.
                    return claim_inputs(
                        session_id,
                        run_id,
                        limit=min(limit, 1) if session_id == worker_id else limit,
                    )

                monkeypatch.setattr(runtime.store, "claim_inputs", claim_one_worker_input)
            pending_observed = False
            control_notify = runtime.bus.notify

            async def observe_pending_then_finish_old_request() -> None:
                nonlocal pending_observed, alias_id
                await control_notify()
                deliveries = runtime.store.list("task_plan_delivery")
                if (
                    damage == "notice_content_alias"
                    and not alias_id
                    and len(deliveries) == 1
                    and deliveries[0].get("path") == "archive"
                ):
                    notice = runtime.store.get("message", deliveries[0]["archive_message_id"])
                    assert notice is not None
                    alias, inserted = runtime.conversation.enqueue(
                        worker_id,
                        notice["content"],
                        source=f"team:{main_id}",
                        principal=f"agent:{main_id}",
                        mode=runtime.team.get(worker_id)["mode"],
                        max_pending=runtime.config.settings.limits.max_pending_runs,
                        internal=True,
                    )
                    assert inserted and alias["state"] == "pending"
                    assert alias["id"] != notice["id"] and "task_control" not in alias
                    alias_id = alias["id"]
                if (
                    notice_path == "ingress"
                    and not pending_observed
                    and asyncio.current_task() is runtime.tasks.get(main_id)
                    and len(deliveries) == 1
                    and deliveries[0].get("path") == "ingress"
                ):
                    delivery = deliveries[0]
                    row = runtime.store.db.execute(
                        "SELECT state,run_id FROM ingress WHERE id=?", (delivery["ingress_id"],)
                    ).fetchone()
                    assert tuple(row) == ("pending", None)
                    assert "report_binding" not in runtime.team.get(worker_id)
                    assert runtime.store.get("message", delivery["ingress_id"]) is None
                    pending_observed = True
                    finish_old_request.set()
                    await asyncio.wait_for(resumed_entered.wait(), 3)

            phase = "replan"
            adjustment_text = f"/调整 {command_code(manager, '调整')} {requirement}"
            with monkeypatch.context() as ingress_patch:
                ingress_patch.setattr(
                    runtime.bus, "notify", observe_pending_then_finish_old_request
                )
                assert (await phone(web, adjustment_text, 103)).status_code == 200
                await eventually(runtime, lambda: bool(completed(runtime, replan_reply)))
            assert await runtime.wait(main_runs["replan"]) == replan_reply
            await asyncio.wait_for(resumed_entered.wait(), 3)
            assert pending_observed is (notice_path == "ingress")
            assert len(main_seen["replan"]) == 3 and len(worker_seen) == 1
            completion_revision = runtime.team.get(worker_id)["completion_revision"]
            old_tool = next(
                item
                for item in runtime.store.messages(worker_id)
                if item.get("tool_call_id") == "old-write"
            )
            assert json.loads(old_tool["content"])["code"] == "task_paused"
            assert not forbidden_path.exists()
            old_report = next(
                item
                for item in runtime.store.list("team_report")
                if item["agent_id"] == worker_id and item["execution_status"] == "paused"
            )
            assert main_id not in runtime.tasks
            assert_one_replan(runtime, task_id, main_id)
            replan_receipt = next(
                item
                for item in runtime.store.list("task_control_receipt")
                if item["action"] == "request_replan"
            )
            control_input = next(
                item
                for item in runtime.store.messages(main_id)
                if item["id"] == replan_receipt["ingress_id"]
            )
            assert control_input["source"] == "task-control"
            assert control_input["principal"] == "owner" and control_input["route"] is None
            control_origin = control_input["reply_origin"]
            assert control_origin == {
                "kind": "channel",
                "input_id": hashlib.sha256(encoded(["onebot", "123", "103"]).encode()).hexdigest(),
                "route": {
                    "name": "onebot",
                    "account_id": "123",
                    "target": "7",
                    "sender_id": "7",
                    "kind": "private",
                },
            }
            replan_message = completed(runtime, replan_reply)[0]
            await processed_reply(runtime, manager, replan_message["id"])
            assert not any(delivery_text(item) == replan_reply for item in manager.deliveries())
            assert replan_message["reply_origin"] == control_origin
            paused_report = runtime.store.get("team_report", old_report["id"])
            assert paused_report["status"] == "delivered"
            assert paused_report["reply_origin"] == original_origin
            old_report_message = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("source") == f"team:{worker_id}"
                and item.get("content") == old_report["content"]
            )
            assert old_report_message["reply_origin"] == original_origin
            deliveries = runtime.store.list("task_plan_delivery")
            assert len(deliveries) == 1 and deliveries[0]["state"] == "delivered"
            assert deliveries[0]["target_session_id"] == worker_id
            notices = [
                item
                for item in runtime.store.messages(worker_id)
                if item.get("principal") == "system:task_control"
            ]
            assert deliveries[0]["path"] == notice_path
            if notice_path == "archive":
                assert len(notices) == 1
                assert notices[0]["id"] == deliveries[0]["archive_message_id"]
                assert notices[0]["run_id"] is None
                assert notices[0]["source"] == "task-control" and notices[0]["route"] is None
                assert notices[0]["task_control"] == {
                    "commitment_id": task_id,
                    "delivery_id": deliveries[0]["id"],
                    "epoch": 2,
                }
            else:
                assert notices == []
                pending_notice = runtime.store.db.execute(
                    "SELECT state,run_id FROM ingress WHERE id=?", (deliveries[0]["ingress_id"],)
                ).fetchone()
                assert tuple(pending_notice) == ("pending", None)
                assert "report_binding" not in runtime.team.get(worker_id)
            resumed = runtime.team.get(worker_id)
            assert resumed["generation"] == generation + 1
            assert resumed["reply_origin"] == original_origin
            completed_report_id = resumed["run_id"]
            if damage == "notice_content_alias":
                assert removed_notice_ids == [notices[0]["id"]]
                assert (
                    len(
                        [
                            message
                            for message in worker_seen[0]
                            if message.role == "user" and message.content == notices[0]["content"]
                        ]
                    )
                    == 1
                )
                assert runtime.task_control is not None
                assert (
                    runtime.task_control._report_candidate(resumed, completed_report_id, notices[0])
                    is not None
                )
                assert "report_binding" not in resumed, (
                    "准确通知已退出实际请求，另一真实消息的相同正文不得代替其身份"
                )
            elif notice_path == "archive":
                binding = resumed["report_binding"]
                assert binding["version"] == 1 and binding["run_id"] == completed_report_id
                assert binding["generation"] == generation + 1
                assert binding["completion_revision"] == completion_revision + 1
                assert binding["notice_id"] == notices[0]["id"]
                assert binding["delivery_id"] == deliveries[0]["id"]
                assert binding["reply_origin"] == control_origin

            phase = "other_private"
            assert (await phone(web, "日常问候：今天好。", 104, sender=8)).status_code == 200
            await eventually(runtime, lambda: bool(completed(runtime, other_reply)))
            assert await runtime.wait(main_runs["other_private"]) == other_reply
            other_message = completed(runtime, other_reply)[0]
            await processed_reply(runtime, manager, other_message["id"])
            assert not any(delivery_text(item) == other_reply for item in manager.deliveries())
            assert other_message["reply_origin"]["route"]["target"] == "8"
            assert other_message["reply_origin"]["input_id"] != control_origin["input_id"]
            assert main_id not in runtime.tasks and runtime.store.pending_inputs(main_id) == 0
            if damage is not None and damage != "notice_content_alias":
                # Each negative control changes exactly one field of genuine persisted proof.
                if damage == "source_version":
                    delivery = runtime.store.get("task_plan_delivery", deliveries[0]["id"])
                    source = {**delivery["report_source"], "version": 0}
                    runtime.store.put("task_plan_delivery", {**delivery, "report_source": source})
                elif damage == "archive_message":
                    delivery = runtime.store.get("task_plan_delivery", deliveries[0]["id"])
                    runtime.store.put(
                        "task_plan_delivery",
                        {**delivery, "archive_message_id": control_input["id"]},
                    )
                else:
                    fields: dict[str, object] = {
                        "binding_version": 0,
                        "channel_binding_version": 0,
                        "binding_run": main_runs["replan"],
                        "binding_generation": generation,
                        "binding_revision": completion_revision,
                    }
                    names = {
                        "binding_version": "version",
                        "channel_binding_version": "version",
                        "binding_run": "run_id",
                        "binding_generation": "generation",
                        "binding_revision": "completion_revision",
                    }
                    current_agent = runtime.team.get(worker_id)
                    altered = {**current_agent["report_binding"], names[damage]: fields[damage]}
                    runtime.store.put("agent", {**current_agent, "report_binding": altered})

            persisted, never_released = asyncio.Event(), asyncio.Event()
            notify = runtime.bus.notify

            async def stop_after_durable_completion() -> None:
                await notify()
                report = runtime.store.get("team_report", completed_report_id)
                if (
                    asyncio.current_task() in runtime.team._tasks
                    and not persisted.is_set()
                    and report is not None
                    and report["execution_status"] == "completed"
                    and report["status"] == "pending"
                ):
                    # Pause the real post-commit finalizer, never the worker execution or flush API.
                    persisted.set()
                    await never_released.wait()

            with monkeypatch.context() as completion_patch:
                completion_patch.setattr(runtime.bus, "notify", stop_after_durable_completion)
                finish_resumed_request.set()
                try:
                    await asyncio.wait_for(persisted.wait(), 3)
                except TimeoutError:
                    worker_run = runtime.store.get("run", completed_report_id) or {}
                    draft_result = next(
                        (
                            message["content"]
                            for message in reversed(runtime.store.messages(worker_id))
                            if message.get("tool_call_id") == "write-draft"
                        ),
                        None,
                    )
                    pytest.fail(
                        "等待真实队友完成报告超时："
                        + json.dumps(
                            {
                                "worker_run": {
                                    key: worker_run.get(key)
                                    for key in ("status", "error", "failure_diagnostic")
                                },
                                "write_draft_result": draft_result[:1000]
                                if isinstance(draft_result, str)
                                else None,
                            },
                            ensure_ascii=True,
                        ),
                        pytrace=False,
                    )
                finished = runtime.team.get(worker_id)
                assert (
                    finished["status"] == "completed" and finished["run_id"] == completed_report_id
                )
                assert finished["generation"] == generation + 1
                assert finished["completion_revision"] == completion_revision + 1
                assert runtime.store.get("run", completed_report_id)["status"] == "completed"
                finished_events = [
                    event["data"]
                    for event in runtime.store.events(session_id=worker_id)
                    if event["type"] == "team.finished"
                    and event["data"].get("run_id") == completed_report_id
                ]
                assert len(finished_events) == 1
                assert finished_events[0]["status"] == "completed"
                assert finished_events[0]["generation"] == generation + 1
                assert finished_events[0]["completion_revision"] == completion_revision + 1
                assert draft_path.read_text(encoding="utf-8") == draft_content
                assert not forbidden_path.exists()
                assert len(worker_seen) == (3 if notice_path == "ingress" else 2)
                assert worker_launches == 2
                pending_report = runtime.store.get("team_report", completed_report_id)
                assert pending_report["execution_status"] == "completed"
                assert pending_report["status"] == "pending"
                assert pending_report["agent_id"] == worker_id
                assert pending_report["parent_id"] == main_id
                assert pending_report["commitment_id"] == task_id
                assert worker_result in pending_report["content"]
                if damage is None:
                    binding = pending_report["report_binding"]
                    assert binding["version"] == 1 and binding["run_id"] == completed_report_id
                    assert binding["generation"] == generation + 1
                    assert binding["completion_revision"] == completion_revision + 1
                    assert binding["reply_origin"] == control_origin
                    assert finished["report_binding"] == binding
                    assert (
                        binding["notice_id"]
                        == deliveries[0][
                            "archive_message_id" if notice_path == "archive" else "ingress_id"
                        ]
                    )
                    notice = runtime.store.get("message", binding["notice_id"])
                    assert notice["principal"] == "system:task_control"
                    assert notice["task_control"]["delivery_id"] == deliveries[0]["id"]
                    if notice_path == "ingress":
                        row = runtime.store.db.execute(
                            "SELECT state,run_id,seq,delivered_seq FROM ingress WHERE id=?",
                            (notice["id"],),
                        ).fetchone()
                        assert tuple(row) == (
                            "delivered",
                            completed_report_id,
                            notice["input_seq"],
                            notice["seq"],
                        )
                assert not any(
                    item.get("source") == f"team:{worker_id}"
                    and item.get("content") == pending_report["content"]
                    for item in runtime.store.messages(main_id)
                )
                assert runtime.store.pending_inputs(main_id) == 0 and main_id not in runtime.tasks
                assert not main_seen["completion"] and not completion_attempts
                assert runtime.store.get("team_report", paused_report["id"]) == paused_report
                prior_delivery_ids = {item["id"] for item in manager.deliveries()}
                await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        runtime.store.db.execute("SELECT 1")
    config_path, data_dir = runtime.config.path, runtime.data_dir
    if outcome == "blocked":
        ConfigFile(config_path).update({"channels": {"onebot": {"owner_ids": ["8"]}}})
    phase = "completion"
    reopened = Runtime(ConfigFile(config_path), data_dir, client_factory=factory)  # type: ignore[arg-type]
    active_runtime = reopened
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    completion_delivery_id = ""
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert reopened.store.instance_id == store_instance
            assert reopened.main_session()["id"] == main_id
            if damage is None:
                await eventually(reopened, lambda: "completion" in main_runs)
                await approve_reply_send(reopened, web, main_runs["completion"], completion_reply)
            await eventually(reopened, lambda: bool(completed(reopened, completion_reply)))
            assert await reopened.wait(main_runs["completion"]) == completion_reply
            assert len(set(main_runs.values())) == 4
            assert [len(main_seen[name]) for name in main_seen] == [
                3,
                3,
                1,
                2 if damage is None else 1,
            ]
            finished = reopened.team.get(worker_id)
            assert finished["commitment_id"] == task_id and finished["parent_id"] == main_id
            assert finished["status"] == "completed" and finished["run_id"] == completed_report_id
            assert finished["generation"] == generation + 1
            assert finished["completion_revision"] == completion_revision + 1
            assert finished["reply_origin"] == original_origin and worker_launches == 2
            assert reopened.store.get("team_report", paused_report["id"]) == paused_report
            assert_one_replan(reopened, task_id, main_id)
            assert len(reopened.store.list("task_plan_delivery")) == 1
            current = reopened_app.state.local_services.commitments.get(task_id)
            assert current["brief"]["constraints"] == ["不可部署", requirement]
            assert current["control"]["epoch"] == current["control"]["approved_epoch"] == 2
            final = completed(reopened, completion_reply)
            assert len(final) == 1
            visible = await web.get(f"/api/sessions/{main_id}")
            assert visible.status_code == 200
            assert any(item.get("id") == final[0]["id"] for item in visible.json()["messages"])
            manager = reopened_app.state.channel_application.manager
            await processed_reply(reopened, manager, final[0]["id"])
            replies = [
                item for item in manager.deliveries() if delivery_text(item) == completion_reply
            ]
            if damage is not None:
                assert final[0]["reply_origin"] == original_origin
                report = reopened.store.get("team_report", completed_report_id)
                assert report["reply_origin"] == original_origin
                assert "report_binding" not in report
                assert replies == [] and completion_attempts == []
                assert final[0]["delivery_routes"] == (
                    [original_origin["route"]] if original_channel else []
                )
                assert {item["id"] for item in manager.deliveries()} == prior_delivery_ids
                return
            if outcome == "blocked":
                assert "owner_ids" in completion_receipt["error"] and replies == []
            else:
                assert len(replies) == 1, "主模型显式发送的真实完成报告进入手机7 outbox"
                reply = await terminal(manager, replies[0]["id"])
                completion_delivery_id = reply["id"]
                assert completion_delivery_id == completion_receipt["id"]
                assert reply["state"] == outcome
                assert reply["payload"].get("automatic") is not True
                assert reply["route"] == control_origin["route"]
            assert final[0]["reply_origin"] == control_origin
            assert final[0]["delivery_routes"] == [control_origin["route"]]
            report = reopened.store.get("team_report", completed_report_id)
            assert report["reply_origin"] == control_origin
            assert len(completion_attempts) == (0 if outcome == "blocked" else 1)
            assert all(
                str(item["user_id"]) == "7"
                for item in requests
                if any(segment["data"]["text"] == completion_reply for segment in item["message"])
            )
            assert not any(
                delivery_text(item) in {initial_reply, replan_reply, other_reply}
                for item in manager.deliveries()
            )
            additional = [
                item for item in manager.deliveries() if item["id"] not in prior_delivery_ids
            ]
            explicit = [item for item in additional if "approval_notice" not in item["payload"]]
            assert {item["id"] for item in explicit} == (
                set() if outcome == "blocked" else {completion_delivery_id}
            )
            for notice in additional:
                if "approval_notice" in notice["payload"]:
                    approval = reopened.store.get(
                        "approval", notice["payload"]["approval_notice"]["approval_id"]
                    )
                    assert approval is not None
                    assert approval["run_id"] == main_runs["completion"]
                    assert approval["tool"] == "channel_send"
                    assert approval["arguments"]["text"] == completion_reply
                    assert approval["status"] == "approved"
            assert (await phone(web, adjustment_text, 103)).status_code == 200
            assert_one_replan(reopened, task_id, main_id)
            assert len(main_seen["completion"]) == 2
            assert len(reopened.store.list("task_plan_delivery")) == 1
            assert len(reopened.store.messages(main_id)) <= 32
    finally:
        await reopened.close()

    if outcome == "unknown":
        unexpected: list[list[Message]] = []

        def never_model(config: ModelConfig) -> FakeClient:
            return FakeClient([], unexpected)

        recovered = Runtime(ConfigFile(config_path), data_dir, client_factory=never_model)  # type: ignore[arg-type]
        recovered_app = create_app(recovered.config, recovered.data_dir, recovered)
        try:
            async with (
                recovered_app.router.lifespan_context(recovered_app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=recovered_app),
                    base_url="http://localhost:8765",
                ) as web,
            ):
                assert recovered.store.instance_id == store_instance
                manager = recovered_app.state.channel_application.manager
                assert manager.delivery(completion_delivery_id)["state"] == "unknown"
                known_deliveries = {item["id"] for item in manager.deliveries()}
                assert (await phone(web, adjustment_text, 103)).status_code == 200
                assert (await phone(web, "/任务", 105)).status_code == 200
                status_reply = next(
                    item
                    for item in manager.deliveries()
                    if item["id"] not in known_deliveries and "任务列表：" in delivery_text(item)
                )
                assert (await terminal(manager, status_reply["id"]))["state"] == "sent"
                assert {item["id"] for item in manager.deliveries()} == known_deliveries | {
                    status_reply["id"]
                }
                assert manager.delivery(completion_delivery_id)["state"] == "unknown"
                assert len(completion_attempts) == 1 and not unexpected
                assert recovered.team.get(worker_id)["generation"] == generation + 1
                assert recovered.store.get("team_report", paused_report["id"]) == paused_report
                assert_one_replan(recovered, task_id, main_id)
                assert len(recovered.store.list("task_plan_delivery")) == 1
        finally:
            await recovered.close()
