"""One real entry-contract red, then the due-time journey once binding exists."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attention import eventually as attention_eventually
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_reminders import ObservedEvent
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

import muli.reminders as reminders_module
from muli.config import ConfigFile
from muli.proactive import FrozenRouting
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now


async def test_goal_followup_reads_current_requirements_and_delivery_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    async def platform(request: httpx.Request) -> httpx.Response:
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
    due_at = scheduler_now + 60
    monkeypatch.setattr(reminders_module, "now", lambda: scheduler_now)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": True, "events": ["reminder"], "coalesce_seconds": 0},
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
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    old_brief = {"goal": "整理本周错题索引", "constraints": ["只使用临时模拟资料", "不发文件"]}
    new_brief = {
        "goal": "复盘本周错题原因并列出下一步",
        "constraints": ["只使用临时模拟资料", "不发文件", "外部成绩尚未核验"],
    }
    judgments = {
        "goal": {"status": "satisfied", "reason": "已读取本地草稿；这是模型判断。"},
        "constraints": [
            {"index": 0, "status": "satisfied", "reason": "仅使用测试生成的模拟资料。"},
            {"index": 1, "status": "satisfied", "reason": "只有本地附件副本。"},
            {"index": 2, "status": "unverified", "reason": "没有读取外部成绩。"},
        ],
    }
    source = tmp_path / "goal-review.txt"
    content = "模拟错题复盘：先说明错因，再做两道同类题；外部成绩尚未核验。"
    raw = content.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    replies = {
        "setup": "目标与复盘提醒的调用已结束，将按实际结果说明。",
        "progress": "按新要求交给直接队友，只保留本地草稿。",
        "delivery": "已按真实草稿和完成报告登记验收依据。",
        "other": "已收到另一私聊的日常问候。",
    }
    worker_reply = "模拟复盘草稿已保存本地副本，外部成绩没有核验。"
    phase = "setup"
    active_runtime = runtime
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    seen: dict[str, list[list[Message]]] = {}
    followup_tools: list[list[str]] = []
    task_id = worker_id = attachment_id = reminder_id = ""
    schedule_result: dict[str, Any] = {}
    delivery: dict[str, Any] = {}
    followup: dict[str, Any] = {}
    schedule_available = False
    worker_rounds = 0
    worker_entered, release_worker = asyncio.Event(), asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id, schedule_result, schedule_available, delivery
            current = rounds[phase] = rounds.get(phase, 0) + 1
            runs[phase] = active_runtime.run_ids[main_id]
            seen.setdefault(phase, []).append(deepcopy(messages))
            if phase == "setup":
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-goal",
                                "commitment_save",
                                title="个人错题复盘目标",
                                brief=old_brief,
                            )
                        ]
                    )
                elif current == 2:
                    task_id = tool_result(messages, "save-goal")["id"]
                    schedule_available = any(
                        tool["function"]["name"] == "reminder_schedule" for tool in tools
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "schedule-goal",
                                "reminder_schedule",
                                title="到约定时间复盘个人目标",
                                content="读取最新要求与实际交付依据，说明未验事项和下一步；不执行任务。",
                                due_at=due_at,
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert current == 3
                    schedule_result = tool_result(messages, "schedule-goal")
                    turn = ModelTurn(content=replies[phase])
            elif phase == "progress":
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "edit-goal",
                                "commitment_save",
                                json.dumps(
                                    {
                                        "identifier": task_id,
                                        "title": "个人错题复盘目标",
                                        "brief": new_brief,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        ]
                    )
                elif current == 2:
                    assert tool_result(messages, "edit-goal")["revision"] == 2
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-goal-worker",
                                "spawn_agent",
                                task_name="模拟错题草稿队友",
                                prompt="按最新完整要求写模拟草稿并发布本地副本，不发送文件。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert current == 3
                    worker_id = tool_result(messages, "spawn-goal-worker")["id"]
                    turn = ModelTurn(content=replies[phase])
            elif phase == "delivery":
                if current == 1:
                    assert any(worker_reply in message.content for message in messages)
                    turn = ModelTurn(
                        tool_calls=[call("read-goal-copy", "attachment_read", id=attachment_id)]
                    )
                elif current == 2:
                    assert tool_result(messages, "read-goal-copy") == {
                        "id": attachment_id,
                        "content": content,
                        "truncated": False,
                    }
                    agent = active_runtime.team.get(worker_id)
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "record-goal-delivery",
                                "task_delivery_record",
                                json.dumps(
                                    {
                                        "identifier": task_id,
                                        "observed_revision": 2,
                                        "observed_epoch": 0,
                                        "report_id": agent["run_id"],
                                        "attachment_id": attachment_id,
                                        "idempotency_key": "goal-draft",
                                        "judgments": judgments,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        ]
                    )
                else:
                    assert current == 3
                    delivery = tool_result(messages, "record-goal-delivery")
                    turn = ModelTurn(content=replies[phase])
            elif phase == "other":
                assert current == 1
                turn = ModelTurn(content=replies[phase])
            else:
                assert phase == "followup"
                followup_tools.append([tool["function"]["name"] for tool in tools])
                try:
                    if current == 1:
                        trigger = next(
                            message
                            for message in reversed(messages)
                            if message.role == "user"
                            and message.content.startswith("【系统注意事项")
                        )
                        facts = json.loads(trigger.content[trigger.content.index("[") :])
                        followup["facts"] = facts
                        binding = facts[0]["followup"]
                        identifier = binding["commitment_id"]
                        turn = ModelTurn(
                            tool_calls=[
                                ToolCall(
                                    "followup-read",
                                    "commitment_read",
                                    json.dumps({"identifier": identifier}),
                                ),
                                ToolCall(
                                    "followup-directory",
                                    "commitment_inspect",
                                    json.dumps(
                                        {
                                            "identifier": identifier,
                                            "section": "deliveries",
                                        }
                                    ),
                                ),
                            ]
                        )
                    elif current == 2:
                        followup["read"] = tool_result(messages, "followup-read")
                        followup["directory"] = tool_result(messages, "followup-directory")
                        item = followup["directory"]["items"][0]
                        turn = ModelTurn(
                            tool_calls=[
                                ToolCall(
                                    "followup-body",
                                    "commitment_inspect",
                                    json.dumps(
                                        {
                                            "identifier": followup["read"]["id"],
                                            "section": "deliveries",
                                            "record_id": item["id"],
                                        }
                                    ),
                                )
                            ]
                        )
                    else:
                        assert current == 3
                        followup["body"] = tool_result(messages, "followup-body")
                        record = json.loads(followup["body"]["content"])
                        requirements = json.loads(followup["read"]["text"])
                        pending = [
                            str(item["index"])
                            for item in record["model_judgments"]["constraints"]
                            if item["status"] == "unverified"
                        ]
                        followup["reply"] = (
                            f"按当前目标“{requirements['goal']}”复盘：已有附件副本字节核验记录。"
                            f"限制索引 {','.join(pending)} 仍未验；下一步请补充可核验依据。"
                        )
                        turn = ModelTurn(content=followup["reply"])
                except (KeyError, IndexError, StopIteration, ValueError, TypeError) as exc:
                    # A partial implementation must end normally, then fail an outer oracle.
                    followup["error"] = type(exc).__name__
                    turn = ModelTurn(content="到期资料不足以核定目标与交付依据，尚未完成复盘。")
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds, attachment_id
            worker_rounds += 1
            if worker_rounds == 1:
                worker_entered.set()
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[call("write-goal", "fs_write", path=str(source), content=content)]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "write-goal")["path"] == str(source)
                turn = ModelTurn(
                    tool_calls=[call("publish-goal", "file_publish", path=str(source))]
                )
            else:
                assert worker_rounds == 3
                copied = tool_result(messages, "publish-goal")
                attachment_id = copied["id"]
                assert copied["size"] == len(raw) and copied["sha256"] == digest
                turn = ModelTurn(content=worker_reply)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    original_origin: dict[str, Any] = {}
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                await phone(web, "请记下我的模拟错题目标，并在约定时间复盘实际依据。", 501)
            ).status_code == 200
            await eventually(runtime, lambda: bool(completed(runtime, replies["setup"])))
            assert await runtime.wait(runs["setup"]) == replies["setup"]
            setup_reply = completed(runtime, replies["setup"])[0]
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, setup_reply["id"])
            setup_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == replies["setup"]
            ]
            assert setup_deliveries == []
            original_origin = setup_reply["reply_origin"]
            assert (
                original_origin["route"]["target"] == original_origin["route"]["sender_id"] == "7"
            )
            phase = "progress"
            changed = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={
                    "content": "将目标改为复盘错因并列下一步，注明外部成绩未验；请队友仅交模拟草稿。",
                },
            )
            assert changed.status_code == 200
            assert await runtime.wait(changed.json()["run_id"]) == replies["progress"]
            await asyncio.wait_for(worker_entered.wait(), 3)
            phase = "delivery"
            release_worker.set()
            worker = await wait_agent(runtime, worker_id, "completed")
            await eventually(runtime, lambda: bool(completed(runtime, replies["delivery"])))
            assert await runtime.wait(runs["delivery"]) == replies["delivery"]
            assert worker["generation"] == worker["completion_revision"] == 1
            report = runtime.store.get("team_report", worker["run_id"])
            assert (
                report is not None
                and report["status"] == "delivered"
                and report["execution_status"] == "completed"
            )
            assert report["parent_id"] == main_id and report["commitment_id"] == task_id
            assert runtime.store.get("run", worker["run_id"])["status"] == "completed"
            assert delivery.get("id"), delivery
            assert delivery["task_revision"] == 2 and delivery["epoch"] == 0
            assert delivery["agent_id"] == worker_id and delivery["run_id"] == report["id"]
            assert delivery["model_judgments"] == judgments
            assert delivery["host_facts"]["sha256"] == digest
            assert app.state.attachments.path(attachment_id).read_bytes() == raw
            assert source.read_text(encoding="utf-8") == content
            task = app.state.local_services.commitments.get(task_id)
            assert task["status"] == "open" and task["revision"] == 2
            assert task["brief"]["goal"] == new_brief["goal"]
            actual_schedule = [
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "schedule-goal"
            ]
            assert (
                len(actual_schedule) == 1 and actual_schedule[0]["content_provenance"] == "accepted"
            )
            assert json.loads(actual_schedule[0]["content"]) == schedule_result
            assert schedule_available and rounds == {"setup": 3, "progress": 3, "delivery": 3}
            # Current target red: the existing tool returned and its main run ended normally.
            assert schedule_result.get("followup") == {
                "version": 1,
                "commitment_id": task_id,
                "revision": 1,
            }, f"真实任务、交付与工具结果均已持久，但提醒入口未建立正式任务关联：{schedule_result}"
            reminder_id = schedule_result["id"]
            stored = runtime.store.get("reminder", reminder_id)
            assert stored == schedule_result and stored["fired_count"] == 0
            assert (
                stored["routing"] == FrozenRouting.bound(ReplyOrigin.load(original_origin)).dump()
            )
            phase = "other"
            assert (
                await phone(web, "这里只是另一私聊的日常问候，不改目标与复盘约定。", 502, sender=8)
            ).status_code == 200
            await eventually(runtime, lambda: bool(completed(runtime, replies["other"])))
            assert await runtime.wait(runs["other"]) == replies["other"]
            other_reply = completed(runtime, replies["other"])[0]
            await processed_reply(runtime, manager, other_reply["id"])
            other_deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == replies["other"]
            ]
            assert other_deliveries == []
            assert other_reply["reply_origin"]["route"]["target"] == "8"
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    phase = "followup"
    scheduler_now = due_at
    reopened = Runtime(ConfigFile(runtime.config.path), runtime.data_dir, client_factory=factory)  # type: ignore[arg-type]
    active_runtime = reopened
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    notice: dict[str, Any] = {}
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
            await eventually(
                reopened,
                lambda: (
                    "followup" in runs
                    and reopened.store.get("run", runs["followup"])["status"]
                    in {"completed", "failed"}
                ),
            )
            await reopened.wait(runs["followup"])
            assert "error" not in followup, followup
            assert rounds["followup"] == 3
            notice = reopened.store.get("notification", f"reminder:{reminder_id}:{due_at}")
            assert notice is not None and notice["followup"] == schedule_result["followup"]
            stored = reopened.store.get("reminder", reminder_id)
            assert stored["fired_count"] == 1 and stored["enabled"] is False
            assert stored["followup"] == schedule_result["followup"]
            assert (
                len(followup["facts"]) == 1
                and followup["facts"][0]["followup"] == stored["followup"]
            )
            assert followup["read"]["id"] == task_id and followup["read"]["revision"] == 2
            assert followup["read"]["has_more"] is False
            assert json.loads(followup["read"]["text"])["goal"] == new_brief["goal"]
            assert [item["id"] for item in followup["directory"]["items"]] == [delivery["id"]]
            assert followup["directory"]["items"][0]["current"] is True
            assert followup["body"]["has_more"] is False
            assert json.loads(followup["body"]["content"]) == delivery
            assert all(
                not {
                    "spawn_agent",
                    "workflow_create",
                    "task_approve_plan",
                    "task_delivery_record",
                    "reminder_schedule",
                    "fs_write",
                }.intersection(names)
                for names in followup_tools
            )
            assert all(len(messages) <= 32 for messages in seen["followup"])
            assert tool_result(seen["followup"][1], "followup-read") == followup["read"]
            assert tool_result(seen["followup"][2], "followup-body") == followup["body"]
            final_reply = completed(reopened, followup["reply"])
            assert len(final_reply) == 1 and new_brief["goal"] in final_reply[0]["content"]
            assert final_reply[0]["reply_origin"] == original_origin
            manager = reopened_app.state.channel_application.manager
            await processed_reply(reopened, manager, final_reply[0]["id"])
            outbound = [
                item for item in manager.deliveries() if delivery_text(item) == followup["reply"]
            ]
            assert outbound == []
            web_messages = await web.get(f"/api/sessions/{main_id}/messages", params={"limit": 1})
            assert web_messages.status_code == 200
            visible = web_messages.json()["messages"]
            assert len(visible) == 1 and visible[0]["id"] == final_reply[0]["id"]
            assert visible[0]["content"] == followup["reply"]
            assert visible[0]["content_truncated"] is False
            duplicates = await reopened.bus.emit("notification.created", notice)
            # Skipping a duplicate advances the cursor without emitting another bus event.
            await attention_eventually(
                lambda: reopened_app.state.attention.cursor >= duplicates["id"]
            )
            assert rounds["followup"] == 3
            assert (
                len(
                    [
                        item
                        for item in manager.deliveries()
                        if delivery_text(item) == followup["reply"]
                    ]
                )
                == 0
            )
            await asyncio.wait_for(reopened_app.state.close(), 3)
    finally:
        await reopened.close()

    again = Runtime(ConfigFile(runtime.config.path), runtime.data_dir, client_factory=factory)  # type: ignore[arg-type]
    active_runtime = again
    idle = ObservedEvent()
    again.reminders.changed = idle
    again_app = create_app(again.config, again.data_dir, again)
    try:
        async with again_app.router.lifespan_context(again_app):
            await asyncio.wait_for(idle.waits.get(), 3)
            assert again.store.get("notification", notice["id"]) == notice
            assert again.store.get("reminder", reminder_id)["fired_count"] == 1
            assert rounds["followup"] == 3 and worker_rounds == 3
            assert completed(again, followup["reply"])[0]["id"] == final_reply[0]["id"]
            assert (
                len(
                    [
                        item
                        for item in again_app.state.channel_application.manager.deliveries()
                        if delivery_text(item) == followup["reply"]
                    ]
                )
                == 0
            )
            assert again_app.state.local_services.commitments.get(task_id)["status"] == "open"
            await asyncio.wait_for(again_app.state.close(), 3)
    finally:
        await again.close()
