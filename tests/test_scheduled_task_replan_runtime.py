"""Real paused-task prerequisites for an owner's one-shot scheduled replan."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_mobile_task_control_followup import (
    assert_one_replan,
    command_code,
    delivery_text,
    phone,
    processed_reply,
)
from test_reminders import ObservedEvent
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

import muli.reminders as reminders_module
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now


async def test_owner_can_schedule_replanning_the_real_paused_original_teammate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await exercise_original_task_schedule(tmp_path, monkeypatch, complete_journey=False)


async def test_scheduled_original_task_survives_another_phone_and_reopen_then_reports_to_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await exercise_original_task_schedule(tmp_path, monkeypatch, complete_journey=True)


async def exercise_original_task_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, complete_journey: bool
) -> None:
    evidence = tmp_path / "project-evidence.txt"
    evidence_text = "SCHEDULE-REPLAN-EVIDENCE：只核查现状，尚未部署或发布。"
    evidence.write_text(evidence_text, encoding="utf-8")
    forbidden = tmp_path / "must-not-publish.txt"
    assignment = "请让原队友读取项目资料核查现状，不可部署，保留证据。"
    assigned_reply = "原任务已登记，队友正在读取资料。"
    schedule_request = "原任务先保持暂停；一分钟后按现有要求重新规划，两分钟后过期。"
    schedule_reply = "一次性续办请求已处理，是否登记以实际工具回执为准。"
    other_request = "这里是手机8的日常问候，不改动手机7的暂停任务约定。"
    other_reply = "已收到日常问候，原暂停任务约定保持原来源。"
    plan = "先重新读取原项目资料和旧工具结果，只写本地核查草稿，不部署，不重做未知操作。"
    replan_reply = "已按约定核对完整要求，原队友将先检查旧证据，再交本地草稿。"
    worker_reply = "原资料与旧工具回执已核对；旧写入明确未执行，本次只交本地草稿。"
    completion_reply = "原任务续办完成：已交本地核查草稿，没有部署，也没有重做未知操作。"
    draft = tmp_path / "safe-review-draft.txt"
    draft_text = "续办核查草稿：原证据仍在，未部署或发布。"
    brief = {"goal": "核查项目现状", "constraints": ["不可部署", "保留证据"]}
    sent: list[dict[str, Any]] = []

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert all(part["type"] == "text" for part in body["message"])
        sent.append(body)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(sent)}}
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    scheduler_now = now()
    monkeypatch.setattr(reminders_module, "now", lambda: scheduler_now)
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
    instance_id = runtime.store.instance_id
    active = runtime
    phase = "assignment"
    rounds = {
        "assignment": 0,
        "schedule": 0,
        "worker": 0,
        "other": 0,
        "replan": 0,
        "completion": 0,
        "resumed": 0,
    }
    task_id = worker_id = ""
    schedule_available: bool | None = None
    current_brief: dict[str, Any] = {}
    schedule_result: dict[str, Any] = {}
    clock_origins: list[dict[str, Any]] = []
    worker_read = asyncio.Event()
    release_worker = asyncio.Event()
    schedule_run = ""
    due_at = 0.0
    blocked_id = ""
    generation = 0
    completion_revision = 0
    replan_entered = asyncio.Event()
    allow_replan = asyncio.Event()
    resumed_ready = asyncio.Event()
    allow_worker_final = asyncio.Event()
    replan_runs: list[str] = []

    def diagnostic() -> str:
        runs: list[dict[str, Any]] = []
        for run in active.store.list("run")[-4:]:
            failure = run.get("failure_diagnostic")
            kind = failure.get("exception_type") if isinstance(failure, dict) else None
            runs.append(
                {
                    "status": run.get("status"),
                    "has_error": bool(run.get("error")),
                    "error_type": kind if isinstance(kind, str) and len(kind) <= 64 else None,
                }
            )
        results: list[dict[str, Any]] = []
        for message in active.store.messages(main_id):
            if message.get("role") != "tool":
                continue
            value = json.loads(message["content"])
            results.append(
                {
                    "accepted": message.get("content_provenance") == "accepted",
                    "has_error": isinstance(value, dict) and bool(value.get("error")),
                }
            )
        return json.dumps(
            {"rounds": rounds, "runs": runs, "tool_results": results[-6:]},
            ensure_ascii=False,
        )

    async def wait_reply(content: str) -> None:
        try:
            await eventually(active, lambda: bool(completed(active, content)))
        except TimeoutError:
            pytest.fail(diagnostic())

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id, schedule_available, schedule_result, schedule_run, due_at
            assert len(messages) <= 32
            rounds[phase] += 1
            current = rounds[phase]
            if phase == "assignment":
                if current == 1:
                    incoming = next(
                        message
                        for message in messages
                        if message.role == "user" and message.content.endswith(assignment)
                    )
                    header, content = incoming.content.split("\n", 1)
                    assert header.startswith(
                        "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                    )
                    assert json.loads(header.split("：", 1)[1]) == {
                        "channel": "onebot",
                        "account_id": "123",
                        "sender_id": "7",
                        "target": "7",
                        "kind": "private",
                        "input_id": incoming.source_message_id,
                    }
                    assert content == assignment
                    turn = ModelTurn(
                        tool_calls=[
                            call("save-original", "commitment_save", title="项目核查", brief=brief)
                        ]
                    )
                elif current == 2:
                    task_id = tool_result(messages, "save-original")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-original",
                                "spawn_agent",
                                task_name="原核查队友",
                                prompt=f"读取 {evidence} 并核查现状，不可部署。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert current == 3
                    worker_id = tool_result(messages, "spawn-original")["id"]
                    turn = ModelTurn(content=assigned_reply)
            elif phase == "other":
                assert current == 1
                incoming = next(
                    message
                    for message in messages
                    if message.role == "user" and message.content.endswith(other_request)
                )
                header, content = incoming.content.split("\n", 1)
                assert header.startswith("渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）：")
                assert json.loads(header.split("：", 1)[1]) == {
                    "channel": "onebot",
                    "account_id": "123",
                    "sender_id": "8",
                    "target": "8",
                    "kind": "private",
                    "input_id": incoming.source_message_id,
                }
                assert content == other_request
                turn = ModelTurn(content=other_reply)
            elif phase == "replan":
                run_id = active.run_ids[main_id]
                if current == 1:
                    replan_runs.append(run_id)
                    replan_entered.set()
                    await allow_replan.wait()
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "read-due-task",
                                "commitment_read",
                                json.dumps({"identifier": task_id}),
                            )
                        ]
                    )
                elif current == 2:
                    assert replan_runs == [run_id]
                    page = tool_result(messages, "read-due-task")
                    assert page["has_more"] is False and json.loads(page["text"]) == current_brief
                    assert any(
                        "完整受控任务说明" in message.content
                        and task_id in message.content
                        and brief["goal"] in message.content
                        and '"epoch":2' in message.content
                        for message in messages
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "approve-due-plan",
                                "task_approve_plan",
                                observed_epoch=2,
                                plan=plan,
                                targets=[worker_id],
                            )
                        ]
                    )
                else:
                    assert current == 3 and replan_runs == [run_id]
                    result = tool_result(messages, "approve-due-plan")
                    assert result["current"]["control"]["paused"] is False
                    assert result["current"]["control"]["approved_epoch"] == 2
                    turn = ModelTurn(content=replan_reply)
            elif phase == "completion":
                assert current == 1
                assert any(worker_reply in message.content for message in messages)
                reports = [
                    item
                    for item in active.store.list("team_report")
                    if item["agent_id"] == worker_id and item["execution_status"] == "completed"
                ]
                assert len(reports) == 1 and reports[0]["status"] == "delivered"
                turn = ModelTurn(content=completion_reply)
            else:
                assert phase == "schedule"
                active_run = active.run_ids[main_id]
                if current == 1:
                    schedule_run = active_run
                    incoming = next(
                        message
                        for message in messages
                        if message.role == "user" and message.content.endswith(schedule_request)
                    )
                    header, content = incoming.content.split("\n", 1)
                    assert header.startswith(
                        "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                    )
                    assert json.loads(header.split("：", 1)[1]) == {
                        "channel": "onebot",
                        "account_id": "123",
                        "sender_id": "7",
                        "target": "7",
                        "kind": "private",
                        "input_id": incoming.source_message_id,
                    }
                    assert content == schedule_request
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "read-paused-task",
                                "commitment_read",
                                json.dumps({"identifier": task_id}),
                            ),
                            call("read-schedule-clock", "clock_now"),
                        ]
                    )
                elif current == 2:
                    assert active_run == schedule_run
                    read = tool_result(messages, "read-paused-task")
                    assert read["id"] == task_id and read["has_more"] is False
                    assert json.loads(read["text"]) == current_brief
                    clock = tool_result(messages, "read-schedule-clock")
                    due = float(clock["unix"]) + 60
                    due_at = due
                    schedule_available = "task_schedule_replan" in {
                        tool["function"]["name"] for tool in tools
                    }
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "schedule-original-task",
                                "task_schedule_replan",
                                task_id=task_id,
                                target_id=worker_id,
                                due_at=due,
                                expires_at=due + 60,
                                key="original-task-one-shot",
                            )
                        ]
                    )
                else:
                    assert current == 3 and active_run == schedule_run
                    schedule_result = tool_result(messages, "schedule-original-task")
                    turn = ModelTurn(content=schedule_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            rounds["worker"] += 1
            if rounds["worker"] == 1:
                turn = ModelTurn(
                    tool_calls=[call("read-original-evidence", "fs_read", path=str(evidence))]
                )
            else:
                assert rounds["worker"] == 2
                assert tool_result(messages, "read-original-evidence")["content"] == evidence_text
                worker_read.set()
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "blocked-old-write", "fs_write", path=str(forbidden), content="禁止发布"
                        )
                    ]
                )
            yield StreamEvent("done", turn=turn)

    class ResumedWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            rounds["resumed"] += 1
            current = rounds["resumed"]
            assert len(messages) <= 32
            agent = active.team.get(worker_id)
            assert agent["generation"] == generation + 1
            assert agent["session_id"] == worker_id and agent["commitment_id"] == task_id
            if current == 1:
                assert agent["completion_revision"] == completion_revision
                assert any(
                    plan in message.content and brief["goal"] in message.content
                    for message in messages
                )
                turn = ModelTurn(
                    tool_calls=[
                        call("reread-original-evidence", "fs_read", path=str(evidence)),
                        call(
                            "check-old-tool-result",
                            "conversation_read",
                            session_id=worker_id,
                            message_id=blocked_id,
                        ),
                    ]
                )
            elif current == 2:
                assert tool_result(messages, "reread-original-evidence")["content"] == evidence_text
                old = tool_result(messages, "check-old-tool-result")
                assert old["has_more"] is False and old["message_id"] == blocked_id
                prior = json.loads(old["content"])
                assert prior["status"] == "not_executed" and prior["code"] == "task_paused"
                # The old effect has an actual known non-execution receipt. It is
                # not replayed; only a new local draft is requested below.
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "write-new-safe-draft", "fs_write", path=str(draft), content=draft_text
                        )
                    ]
                )
            else:
                assert current == 3
                assert tool_result(messages, "write-new-safe-draft")["path"] == str(draft)
                assert draft.read_text(encoding="utf-8") == draft_text
                resumed_ready.set()
                await allow_worker_final.wait()
                turn = ModelTurn(content=worker_reply)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model != "worker":
            return Main([], [])
        return Worker([], []) if active is runtime else ResumedWorker([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
            assert (await phone(web, assignment, 6501)).status_code == 200
            await eventually(runtime, lambda: bool(completed(runtime, assigned_reply)))
            await asyncio.wait_for(worker_read.wait(), 3)
            original = runtime.team.get(worker_id)
            assert original["parent_id"] == main_id and original["commitment_id"] == task_id
            generation, run_id = original["generation"], original["run_id"]
            manager = app.state.channel_application.manager
            assert (await phone(web, "/任务", 6502)).status_code == 200
            assert (
                await phone(web, f"/暂停 {command_code(manager, '暂停')}", 6503)
            ).status_code == 200
            release_worker.set()
            parked = await wait_agent(runtime, worker_id, "paused")
            assert parked["generation"] == generation == 1 and parked["run_id"] == run_id
            assert parked["completion_revision"] == 1 and parked["observed_epoch"] == 0
            completion_revision = parked["completion_revision"]
            assert runtime.store.get("run", run_id)["status"] == "paused"  # type: ignore[index]
            assert worker_id not in runtime.team._active and not forbidden.exists()
            worker_messages = runtime.store.messages(worker_id)
            old_read = next(
                item
                for item in worker_messages
                if item.get("tool_call_id") == "read-original-evidence"
            )
            assert old_read["content_provenance"] == "accepted"
            assert json.loads(old_read["content"])["content"] == evidence_text
            blocked = next(
                item for item in worker_messages if item.get("tool_call_id") == "blocked-old-write"
            )
            blocked_id = blocked["id"]
            assert json.loads(blocked["content"])["code"] == "task_paused"
            control = runtime.task_control_current(task_id)["control"]
            assert (
                control["paused"] is True
                and control["epoch"] == 1
                and control["approved_epoch"] == 0
            )
            assert runtime.task_control is not None
            current_brief = runtime.task_control.commitments.get(task_id)["brief"]
            assert (
                current_brief["goal"] == brief["goal"]
                and current_brief["constraints"] == brief["constraints"]
            )
            original_clock = runtime.tools["clock_now"].handler

            async def observe_clock_context(arguments: dict[str, Any]) -> Any:
                # Runtime._tool binds the request only around the trusted handler,
                # not while the provider's stream produces the tool call.
                assert runtime.tool_session_id == main_id
                request = runtime.request_context(main_id)
                assert request is not None
                incoming = next(
                    message
                    for message in request.messages
                    if message.role == "user" and message.content.endswith(schedule_request)
                )
                header, content = incoming.content.split("\n", 1)
                assert header.startswith("渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）：")
                assert json.loads(header.split("：", 1)[1]) == {
                    "channel": "onebot",
                    "account_id": "123",
                    "sender_id": "7",
                    "target": "7",
                    "kind": "private",
                    "input_id": incoming.source_message_id,
                }
                assert content == schedule_request
                origin = request.reply_origin.dump()
                assert origin["kind"] == "channel" and origin["route"]["target"] == "7"
                # Pause alone must not manufacture a future replan approval.
                assert request.selected_replan is None
                clock_origins.append(origin)
                return await original_clock(arguments)

            monkeypatch.setattr(runtime.tools["clock_now"], "handler", observe_clock_context)
            phase = "schedule"
            assert (await phone(web, schedule_request, 6504)).status_code == 200
            try:
                await eventually(runtime, lambda: bool(completed(runtime, schedule_reply)))
            except TimeoutError:
                pytest.fail(diagnostic())
            assert await runtime.wait(schedule_run) == schedule_reply
            assert runtime.store.get("run", schedule_run)["status"] == "completed"  # type: ignore[index]
            assert rounds["assignment"] == rounds["schedule"] == 3 and rounds["worker"] == 2
            assert rounds["resumed"] == rounds["replan"] == rounds["completion"] == 0
            messages = runtime.store.messages(main_id)
            owner = next(
                item
                for item in messages
                if item.get("principal") == "owner" and item["content"] == schedule_request
            )
            assert owner["reply_origin"]["route"]["target"] == "7"
            assert owner["reply_origin"]["input_id"] == owner["id"]
            assert clock_origins == [owner["reply_origin"]]
            for identifier in ("read-paused-task", "read-schedule-clock", "schedule-original-task"):
                result = next(item for item in messages if item.get("tool_call_id") == identifier)
                assert result["content_provenance"] == "accepted"
            reply = completed(runtime, schedule_reply)[0]
            assert reply["reply_origin"] == owner["reply_origin"]
            await processed_reply(runtime, manager, reply["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == schedule_reply
            ]
            assert deliveries == []
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(
                item["id"] == reply["id"] and item["content"] == schedule_reply
                for item in history.json()["messages"]
            )
            assert not any(
                str(item["user_id"]) == "7"
                and any(part["data"]["text"] == schedule_reply for part in item["message"])
                for item in sent
            )
            assert schedule_available is True, (
                "原队友已真实读取资料并按TaskControl暂停，同任务/原代次/完整brief、"
                "phone7当前主工具上下文与accepted回执均成立，但缺少task_schedule_replan；"
                f"实际工具回执：{schedule_result}"
            )
            if not complete_journey:
                return
            assert "error" not in schedule_result and isinstance(schedule_result.get("id"), str)
            schedule_id = schedule_result["id"]
            scheduled = runtime.store.get("task_replan_schedule", schedule_id)
            assert scheduled is not None
            assert scheduled["task_id"] == task_id and scheduled["target_id"] == worker_id
            assert scheduled["due_at"] == due_at and scheduled["expires_at"] == due_at + 60
            assert runtime.store.list("task_replan_occurrence") == []
            phase = "other"
            assert (await phone(web, other_request, 6505, sender=8)).status_code == 200
            await wait_reply(other_reply)
            other_message = completed(runtime, other_reply)[0]
            assert other_message["reply_origin"]["route"]["target"] == "8"
            await processed_reply(runtime, manager, other_message["id"])
            other_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == other_reply
            ]
            assert other_deliveries == []
            assert runtime.task_control_current(task_id)["control"] == control
            assert not any(
                item["action"] == "request_replan"
                for item in runtime.store.list("task_control_receipt")
            )
            assert runtime.team.get(worker_id)["generation"] == generation
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        release_worker.set()
        await runtime.close()

    # A normal application close/reopen shares the actual Store and ingress;
    # it does not manufacture a crash receipt or an already-approved plan.
    phase = "replan"
    reopened = Runtime(ConfigFile(runtime.config.path), runtime.data_dir, client_factory=factory)  # type: ignore[arg-type]
    active = reopened
    idle = ObservedEvent()
    reopened.reminders.changed = idle
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                reopened.store.instance_id == instance_id
                and reopened.main_session()["id"] == main_id
            )
            await asyncio.wait_for(idle.waits.get(), 3)
            assert not any(
                item["action"] == "request_replan"
                for item in reopened.store.list("task_control_receipt")
            )
            assert rounds["replan"] == rounds["resumed"] == 0
            assert reopened.task_control_current(task_id)["control"] == control
            assert reopened.store.list("task_replan_occurrence") == []
            scheduler_now = due_at
            idle.set()
            await asyncio.wait_for(replan_entered.wait(), 3)
            assert_one_replan(reopened, task_id, main_id)
            receipt = next(
                item
                for item in reopened.store.list("task_control_receipt")
                if item["action"] == "request_replan"
            )
            assert receipt["commitment_id"] == task_id and receipt["epoch"] == 2
            occurrences = reopened.store.list("task_replan_occurrence")
            assert len(occurrences) == 1
            occurrence = occurrences[0]
            assert occurrence["schedule_id"] == schedule_id
            assert occurrence["schedule_version"] == scheduled["version"]
            assert occurrence["owner_receipt_id"] == receipt["id"]
            assert occurrence["ingress_id"] == receipt["ingress_id"] and occurrence["epoch"] == 2
            due_control = reopened.task_control_current(task_id)["control"]
            assert (
                due_control["paused"] is True
                and due_control["approved_epoch"] == 0
                and due_control["epoch"] == 2
            )
            parked = reopened.team.get(worker_id)
            assert parked["status"] == "paused" and parked["generation"] == generation
            assert parked["completion_revision"] == completion_revision
            assert rounds["resumed"] == 0 and not draft.exists()
            due_input = next(
                item
                for item in reopened.store.messages(main_id)
                if item["id"] == receipt["ingress_id"]
            )
            assert due_input["principal"] == "owner" and due_input["source"] == "task-control"
            assert due_input["reply_origin"] == owner["reply_origin"]
            assert due_input["run_id"] == replan_runs[0]
            ingress = reopened.store.db.execute(
                "SELECT payload,run_id FROM ingress WHERE id=?", (receipt["ingress_id"],)
            ).fetchall()
            assert len(ingress) == 1 and ingress[0]["run_id"] == replan_runs[0]
            payload = json.loads(ingress[0]["payload"])
            assert payload["id"] == due_input["id"] and payload["principal"] == "owner"
            assert payload["reply_origin"] == due_input["reply_origin"]
            allow_replan.set()
            await wait_reply(replan_reply)
            assert await reopened.wait(replan_runs[0]) == replan_reply
            await asyncio.wait_for(resumed_ready.wait(), 3)
            resumed = reopened.team.get(worker_id)
            assert resumed["session_id"] == worker_id and resumed["parent_id"] == main_id
            assert resumed["generation"] == generation + 1 and resumed["run_id"] != run_id
            assert resumed["reply_origin"] == original["reply_origin"]
            deliveries = reopened.store.list("task_plan_delivery")
            assert len(deliveries) == 1 and deliveries[0]["state"] == "delivered"
            assert (
                deliveries[0]["target_session_id"] == worker_id
                and deliveries[0]["path"] == "archive"
            )
            notice = reopened.store.get("message", deliveries[0]["archive_message_id"])
            assert notice is not None and notice["principal"] == "system:task_control"
            binding = resumed["report_binding"]
            assert (
                binding["generation"] == generation + 1
                and binding["completion_revision"] == completion_revision + 1
            )
            assert binding["run_id"] == resumed["run_id"] and binding["notice_id"] == notice["id"]
            assert binding["reply_origin"] == owner["reply_origin"]
            manager = reopened_app.state.channel_application.manager
            planned_message = completed(reopened, replan_reply)[0]
            assert planned_message["reply_origin"] == owner["reply_origin"]
            await processed_reply(reopened, manager, planned_message["id"])
            planned_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == replan_reply
            ]
            assert planned_deliveries == []
            phase = "completion"
            allow_worker_final.set()
            await wait_agent(reopened, worker_id, "completed")
            await wait_reply(completion_reply)
            final_agent = reopened.team.get(worker_id)
            assert final_agent["generation"] == generation + 1
            assert final_agent["completion_revision"] == completion_revision + 1
            reports = [
                item
                for item in reopened.store.list("team_report")
                if item["agent_id"] == worker_id and item["execution_status"] == "completed"
            ]
            assert len(reports) == 1 and reports[0]["reply_origin"] == owner["reply_origin"]
            assert reports[0]["status"] == "delivered"
            final_message = completed(reopened, completion_reply)[0]
            assert final_message["reply_origin"] == owner["reply_origin"]
            await processed_reply(reopened, manager, final_message["id"])
            final_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == completion_reply
            ]
            assert final_deliveries == []
            actual_sent = [
                (str(item["user_id"]), "".join(part["data"]["text"] for part in item["message"]))
                for item in sent
            ]
            assert (
                actual_sent.count(("7", replan_reply))
                == actual_sent.count(("7", completion_reply))
                == 0
            )
            assert ("8", replan_reply) not in actual_sent and (
                "8",
                completion_reply,
            ) not in actual_sent
            assert actual_sent.count(("8", other_reply)) == 0
            assert draft.read_text(encoding="utf-8") == draft_text and not forbidden.exists()
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            for text in (other_reply, replan_reply, completion_reply):
                assert sum(item["content"] == text for item in history.json()["messages"]) == 1
            receipts = reopened.store.list("task_control_receipt")
            settled_occurrences = reopened.store.list("task_replan_occurrence")
            assert (
                len(settled_occurrences) == 1 and settled_occurrences[0]["id"] == occurrence["id"]
            )
            completions = reopened.store.team_completions(worker_id, 0, limit=10)
            assert [event["data"]["status"] for event in completions] == ["paused", "completed"]
            # Wake the real clock again without synthesizing a duplicate input.
            while not idle.waits.empty():
                idle.waits.get_nowait()
            idle.set()
            await asyncio.wait_for(idle.waits.get(), 3)
            assert reopened.store.list("task_control_receipt") == receipts
            assert reopened.store.list("task_replan_occurrence") == settled_occurrences
            assert reopened.team.get(worker_id)["generation"] == generation + 1
            assert len(replan_runs) == 1 and rounds["resumed"] == 3
            await asyncio.wait_for(reopened_app.state.close(), 3)
    finally:
        allow_replan.set()
        allow_worker_final.set()
        await reopened.close()

    last = Runtime(ConfigFile(runtime.config.path), runtime.data_dir, client_factory=factory)  # type: ignore[arg-type]
    active = last
    last_idle = ObservedEvent()
    last.reminders.changed = last_idle
    last_app = create_app(last.config, last.data_dir, last)
    try:
        async with last_app.router.lifespan_context(last_app):
            await asyncio.wait_for(last_idle.waits.get(), 3)
            assert last.store.instance_id == instance_id
            assert last.store.list("task_control_receipt") == receipts
            assert last.store.list("task_replan_occurrence") == settled_occurrences
            assert last.team.get(worker_id)["generation"] == generation + 1
            assert last.store.team_completions(worker_id, 0, limit=10) == completions
            assert len(replan_runs) == 1 and rounds["completion"] == 1
            assert (
                len(
                    [
                        item
                        for item in last.store.list("agent")
                        if item.get("commitment_id") == task_id
                    ]
                )
                == 1
            )
    finally:
        await last.close()
