"""An authorized future research DAG survives restart and reports to its original phone."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import terminal
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_reminders import ObservedEvent
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

import muli.reminders as reminders_module
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now


async def test_owner_schedules_a_reviewed_delegation_through_the_real_phone_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_request = (
        "请在一分钟后首次搜索并整理合成研究主题，以后每周执行一次。"
        "交给执行队友和独立评审，只将文字成果发回这里；不自动发送文件。"
    )
    prompt = "搜索合成研究主题，整理来源与未核验事项，经独立评审后交回文字摘要。"
    acknowledgement = "定时研究委托请求已处理；是否登记以实际工具回执为准。"
    other_request = "这里只是另一私聊的普通问候，不更改原定时委托。"
    other_reply = "已收到另一私聊问候，原定时研究委托保持原来源。"
    cancel_request = "请列出定时委托并取消刚才的每周研究，保留已有成果历史。"
    cancel_reply = "取消请求已处理，已有文字成果仍保留在原记录。"
    schedule_name = "每周合成研究摘要"
    query = "合成机器人研究进展57"
    evidence = {
        "title": "合成研究资料57",
        "url": "https://sources.invalid/research-57",
        "content": "离线合成资料：小型机器人可记录姿态；尚无真实实验复核。",
    }
    interval_seconds = 7 * 24 * 60 * 60
    requests: list[dict[str, Any]] = []
    search_requests: list[httpx.Request] = []

    async def platform(request: httpx.Request) -> httpx.Response:
        if request.url.host == "search.invalid":
            assert request.method == "GET" and request.url.path == "/search"
            assert dict(request.url.params) == {"q": query, "format": "json"}
            search_requests.append(request)
            return httpx.Response(200, json={"results": [evidence]})
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert all(part["type"] == "text" for part in body["message"])
        requests.append(body)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(requests)}}
        )

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

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
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
            "search": {"provider": "searxng", "base_url": "https://search.invalid"},
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
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    active = runtime
    phase = "setup"
    rounds = {"setup": 0, "other": 0, "worker": 0, "reviewer": 0, "cancel": 0}
    runs: dict[str, str] = {}
    node_sessions: dict[str, str] = {}
    due_at = 0.0
    schedule_available: bool | None = None
    clock_result: dict[str, Any] = {}
    schedule_result: dict[str, Any] = {}
    listing: dict[str, Any] = {}
    cancellation: dict[str, Any] = {}
    search_result: dict[str, Any] = {}
    worker_summary = reviewed_summary = ""

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal \
                due_at, \
                schedule_available, \
                clock_result, \
                schedule_result, \
                listing, \
                cancellation
            assert len(messages) <= 32
            rounds[phase] += 1
            current = rounds[phase]
            assert runs.setdefault(phase, active.run_ids[main_id]) == active.run_ids[main_id]
            names = {tool["function"]["name"] for tool in tools}
            assert {"clock_now", "reminder_schedule", "workflow_create"} <= names
            if phase == "setup":
                if current == 1:
                    assert any(message.content == owner_request for message in messages)
                    turn = ModelTurn(tool_calls=[call("check-delegation-time", "clock_now")])
                elif current == 2:
                    clock_result = tool_result(messages, "check-delegation-time")
                    due_at = float(clock_result["unix"]) + 60
                    schedule_available = "workflow_schedule" in names
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "schedule-reviewed-research",
                                "workflow_schedule",
                                json.dumps(
                                    {
                                        "name": schedule_name,
                                        "prompt": prompt,
                                        "due_at": due_at,
                                        "interval_seconds": interval_seconds,
                                    }
                                ),
                            )
                        ]
                    )
                else:
                    assert current == 3
                    # Missing-tool/schema results still let the owner run finish normally.
                    schedule_result = tool_result(messages, "schedule-reviewed-research")
                    turn = ModelTurn(content=acknowledgement)
            elif phase == "other":
                assert current == 1 and any(
                    message.content == other_request for message in messages
                )
                turn = ModelTurn(content=other_reply)
            else:
                assert phase == "cancel"
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[call("list-delegations", "workflow_schedule_list")]
                    )
                elif current == 2:
                    listing = tool_result(messages, "list-delegations")
                    candidates = [
                        item for item in listing["items"] if item["id"] == schedule_result["id"]
                    ]
                    assert len(candidates) == 1
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "cancel-delegation",
                                "workflow_schedule_cancel",
                                id=candidates[0]["id"],
                            )
                        ]
                    )
                else:
                    assert current == 3
                    cancellation = tool_result(messages, "cancel-delegation")
                    turn = ModelTurn(content=cancel_reply)
            yield StreamEvent("done", turn=turn)

    def observe_node(role: str) -> None:
        current_task = asyncio.current_task()
        sessions = [identifier for identifier, task in active.tasks.items() if task is current_task]
        assert len(sessions) == 1
        session_id = sessions[0]
        assert node_sessions.setdefault(role, session_id) == session_id
        assert runs.setdefault(role, active.run_ids[session_id]) == active.run_ids[session_id]

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal search_result, worker_summary
            assert phase == "due" and len(messages) <= 32
            rounds["worker"] += 1
            observe_node("worker")
            names = {tool["function"]["name"] for tool in tools}
            assert "web_search" in names and not {"workflow_create", "spawn_agent"} & names
            assert any(prompt in message.content for message in messages)
            if rounds["worker"] == 1:
                turn = ModelTurn(tool_calls=[call("research-sources", "web_search", query=query)])
            else:
                assert rounds["worker"] == 2
                search_result = tool_result(messages, "research-sources")
                assert search_result == {"query": query, "results": [evidence], "untrusted": True}
                found = search_result["results"][0]
                worker_summary = (
                    f"检索资料：{found['title']}；{found['content']}来源：{found['url']}"
                )
                turn = ModelTurn(content=worker_summary)
            yield StreamEvent("done", turn=turn)

    class Reviewer(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal reviewed_summary
            assert phase == "due" and len(messages) <= 32
            rounds["reviewer"] += 1
            assert rounds["reviewer"] == 1
            observe_node("reviewer")
            assert worker_summary and any(worker_summary in message.content for message in messages)
            names = {tool["function"]["name"] for tool in tools}
            assert not {"workflow_create", "spawn_agent", "fs_write", "channel_send"} & names
            reviewed_summary = (
                f"独立评审：{worker_summary}；尚未验证真实实验，建议主人核对原始资料。"
            )
            yield StreamEvent("done", turn=ModelTurn(content=reviewed_summary))

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], [])
        if config.model == "reviewer":
            return Reviewer([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)

    def finished() -> bool:
        identifier = runs.get(phase)
        run = active.store.get("run", identifier) if identifier else None
        return run is not None and run["status"] in {"completed", "failed"}

    def diagnostic() -> str:
        states = {}
        for role, identifier in runs.items():
            run = active.store.get("run", identifier)
            if run is not None:
                states[role] = {
                    "status": run.get("status"),
                    "error_type": (run.get("failure_diagnostic") or {}).get("exception_type"),
                }
        return json.dumps(
            {
                "runs": states,
                "rounds": rounds,
                "schedule_available": schedule_available,
                "pending_approvals": len(active.permissions.pending),
                "platform_calls": len(requests),
                "search_calls": len(search_requests),
            },
            ensure_ascii=True,
        )[:1024]

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
            assert (await phone(web, owner_request, 5701)).status_code == 200
            try:
                await eventually(runtime, finished)
            except TimeoutError:
                pytest.fail(diagnostic())
            assert runtime.store.get("run", runs[phase])["status"] == "completed", diagnostic()
            assert await runtime.wait(runs[phase]) == acknowledgement
            assert rounds["setup"] == 3 and not runtime.permissions.pending
            messages = runtime.store.messages(main_id)
            owner = next(
                item
                for item in messages
                if item.get("principal") == "owner" and item["content"] == owner_request
            )
            origin = owner["reply_origin"]
            assert origin["kind"] == "channel" and origin["input_id"] == owner["id"]
            assert origin["route"]["kind"] == "private"
            assert origin["route"]["target"] == origin["route"]["sender_id"] == "7"
            for identifier, expected in (
                ("check-delegation-time", clock_result),
                ("schedule-reviewed-research", schedule_result),
            ):
                actual = [item for item in messages if item.get("tool_call_id") == identifier]
                assert len(actual) == 1 and actual[0]["content_provenance"] == "accepted"
                assert json.loads(actual[0]["content"]) == expected
            assert due_at == float(clock_result["unix"]) + 60
            # Scheduling must not run the DAG during its creating owner request.
            assert runtime.store.list("workflow_run") == []
            reply = completed(runtime, acknowledgement)
            assert len(reply) == 1 and reply[0]["reply_origin"] == origin
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, reply[0]["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == acknowledgement
            ]
            assert len(deliveries) == 1
            delivered = await terminal(manager, deliveries[0]["id"])
            assert delivered["state"] == "sent" and delivered["route"] == origin["route"]
            assert len(requests) == 1 and str(requests[0]["user_id"]) == "7"
            assert (
                "".join(part["data"]["text"] for part in requests[0]["message"]) == acknowledgement
            )
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(item["content"] == acknowledgement for item in history.json()["messages"])
            # The first target red is the live tool directory, after all current prerequisites.
            assert schedule_available is True, (
                "真实phone7 owner输入、clock_now、accepted工具回执、正常主运行、"
                "原7文字投递和Web均已完成，但当前主工具表没有workflow_schedule；"
                f"实际工具结果：{schedule_result}"
            )
            assert "error" not in schedule_result and isinstance(schedule_result.get("id"), str)
            schedule_id = schedule_result["id"]
            scheduled = runtime.store.get("workflow_schedule", schedule_id)
            assert scheduled is not None and scheduled["active"] is True
            assert scheduled["next_due_at"] == due_at and scheduled["fired_count"] == 0
            assert scheduled["reply_origin"] == origin
            assert scheduled["creator_session_id"] == main_id
            assert scheduled["creator_run_id"] == runs["setup"]
            assert scheduled["interval_seconds"] == interval_seconds

            phase = "other"
            assert (await phone(web, other_request, 5702, sender=8)).status_code == 200
            await eventually(runtime, finished)
            assert await runtime.wait(runs[phase]) == other_reply
            other_message = completed(runtime, other_reply)
            assert len(other_message) == 1
            assert other_message[0]["reply_origin"]["route"]["target"] == "8"
            await processed_reply(runtime, manager, other_message[0]["id"])
            other_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == other_reply
            ]
            assert len(other_deliveries) == 1
            assert (await terminal(manager, other_deliveries[0]["id"]))["state"] == "sent"
            assert runtime.store.list("workflow_run") == [] and search_requests == []
            assert rounds["worker"] == rounds["reviewer"] == 0
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    phase = "due"
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
            assert reopened.store.get("workflow_schedule", schedule_id) == scheduled
            assert reopened.store.list("workflow_run") == [] and search_requests == []
            scheduler_now = due_at
            idle.set()

            def workflow_finished() -> bool:
                values = reopened.store.list("workflow_run", 3)
                return bool(
                    len(values) == 1
                    and (
                        values[0]["status"] in {"failed", "cancelled", "timed_out", "interrupted"}
                        or values[0].get("result_message_id")
                    )
                )

            try:
                await eventually(reopened, workflow_finished)
            except TimeoutError:
                pytest.fail(diagnostic())
            actual_runs = reopened.store.list("workflow_run", 3)
            assert len(actual_runs) == 1
            workflow = actual_runs[0]
            assert workflow["status"] == "completed", diagnostic()
            assert workflow["schedule_id"] == schedule_id
            assert workflow["schedule_version"] == scheduled["version"]
            assert workflow.get("parent_run_id") is None
            assert workflow["reply_origin"] == origin and workflow["mode"] == "yolo"
            assert workflow["result"] == reviewed_summary and reviewed_summary
            assert set(workflow["nodes"]) == {"work", "review"}
            assert workflow["nodes"]["review"]["depends_on"] == ["work"]
            for role, node_name in (("worker", "work"), ("reviewer", "review")):
                node = workflow["nodes"][node_name]
                assert node["status"] == "completed" and node["session_id"] == node_sessions[role]
                assert reopened.store.get("run", runs[role])["status"] == "completed"
                assert reopened.store.get("session", node["session_id"])["mode"] == "yolo"
            saved_search = [
                item
                for item in reopened.store.messages(node_sessions["worker"])
                if item.get("tool_call_id") == "research-sources"
            ]
            assert len(saved_search) == 1 and saved_search[0]["content_provenance"] == "accepted"
            assert json.loads(saved_search[0]["content"]) == search_result
            assert len(search_requests) == 1
            report_text = schedule_name + "\n" + reviewed_summary
            report = reopened.store.get("message", workflow["result_message_id"])
            assert report is not None and report["content"] == report_text
            assert report["session_id"] == main_id and report["reply_origin"] == origin
            assert report["workflow_run_id"] == workflow["id"]
            occurrences = reopened.store.list("workflow_occurrence", 3)
            assert len(occurrences) == 1
            occurrence = occurrences[0]
            assert occurrence["id"] == workflow["scheduled_occurrence_id"]
            assert (
                occurrence["schedule_id"] == schedule_id and occurrence["run_id"] == workflow["id"]
            )
            assert occurrence["version"] == scheduled["version"] and occurrence["due_at"] == due_at
            assert occurrence["status"] == "completed"
            manager = reopened_app.state.channel_application.manager
            await processed_reply(reopened, manager, report["id"])
            outputs = [item for item in manager.deliveries() if delivery_text(item) == report_text]
            assert len(outputs) == 1
            assert (await terminal(manager, outputs[0]["id"]))["state"] == "sent"
            assert outputs[0]["route"] == origin["route"]
            sent_reports = [
                item
                for item in requests
                if "".join(part["data"]["text"] for part in item["message"]) == report_text
            ]
            assert len(sent_reports) == 1 and str(sent_reports[0]["user_id"]) == "7"
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert sum(item["content"] == report_text for item in history.json()["messages"]) == 1
            assert rounds == {"setup": 3, "other": 1, "worker": 2, "reviewer": 1, "cancel": 0}
            after_first = reopened.store.get("workflow_schedule", schedule_id)
            assert after_first["fired_count"] == 1 and after_first["active"] is True
            assert after_first["next_due_at"] == due_at + interval_seconds
            await asyncio.wait_for(idle.waits.get(), 3)
            idle.set()
            await asyncio.wait_for(idle.waits.get(), 3)
            assert reopened.store.list("workflow_run", 3) == actual_runs
            assert reopened.store.list("workflow_occurrence", 3) == occurrences
            assert len(search_requests) == 1

            phase = "cancel"
            assert (await phone(web, cancel_request, 5703, sender=8)).status_code == 200
            await eventually(reopened, finished)
            assert await reopened.wait(runs[phase]) == cancel_reply
            assert "error" not in cancellation and rounds["cancel"] == 3
            cancelled = reopened.store.get("workflow_schedule", schedule_id)
            assert cancelled["active"] is False and cancelled["version"] > after_first["version"]
            cancel_message = completed(reopened, cancel_reply)
            assert (
                len(cancel_message) == 1
                and cancel_message[0]["reply_origin"]["route"]["target"] == "8"
            )
            await processed_reply(reopened, manager, cancel_message[0]["id"])
            cancel_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == cancel_reply
            ]
            assert len(cancel_deliveries) == 1
            assert (await terminal(manager, cancel_deliveries[0]["id"]))["state"] == "sent"
            # Observe the real scheduler after cancellation at the next weekly boundary.
            while not idle.waits.empty():
                idle.waits.get_nowait()
            scheduler_now = after_first["next_due_at"]
            idle.set()
            await asyncio.wait_for(idle.waits.get(), 3)
            assert reopened.store.list("workflow_run", 3) == actual_runs
            assert reopened.store.list("workflow_occurrence", 3) == occurrences
            assert len(search_requests) == 1 and rounds["worker"] == 2 and rounds["reviewer"] == 1
            assert len([item for item in requests if str(item["user_id"]) == "7"]) == 2
    finally:
        await reopened.close()
