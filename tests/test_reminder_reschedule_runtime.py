"""Reschedule a real phone-created followup without replacing its identity or route."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attention import attention_inputs
from test_attention import eventually as attention_eventually
from test_channels import terminal
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_reminders import ObservedEvent
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

import muli.reminders as reminders_module
from muli.config import ConfigFile
from muli.proactive import FrozenRouting
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.reminder_followup import read_followup_notice
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now


async def test_phone_reschedule_preserves_goal_and_original_route_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _reschedule_journey(tmp_path, monkeypatch, stale_notice=False)


async def test_reschedule_revokes_persisted_unadmitted_followup_before_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _reschedule_journey(tmp_path, monkeypatch, stale_notice=True)


async def _reschedule_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stale_notice: bool
) -> None:
    sent: list[dict[str, Any]] = []

    async def platform(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert all(part["type"] == "text" for part in body["message"])
        sent.append(body)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(sent)}}
        )

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    clock = now()
    old_due, new_due = clock + 60, clock + 120
    monkeypatch.setattr(reminders_module, "now", lambda: clock)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": True, "events": ["reminder"], "coalesce_seconds": 0},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
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
    old_brief = {"goal": "复盘本周模拟错题", "constraints": ["只使用模拟资料"]}
    new_brief = {
        "goal": "今晚复盘模拟错题并列出两项下一步",
        "constraints": ["只使用模拟资料", "外部成绩尚未核验"],
    }
    normalized_new_brief = {"version": 1, **new_brief, "evidence_refs": []}
    replies = {
        "setup": "已按实际工具结果记录复盘目标与原私聊约定。",
        "reschedule": "改期调用已结束，将按真实结果说明约定状态。",
    }
    phase = "setup"
    active = runtime
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    seen: dict[str, list[list[Message]]] = {}
    task_id = reminder_id = ""
    schedule: dict[str, Any] = {}
    rescheduled: dict[str, Any] = {}
    followup: dict[str, Any] = {}
    reschedule_available = False

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, reminder_id, schedule, rescheduled, reschedule_available
            current = rounds[phase] = rounds.get(phase, 0) + 1
            runs[phase] = active.run_ids[main_id]
            seen.setdefault(phase, []).append(deepcopy(messages))
            if phase == "setup":
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "create-goal",
                                "commitment_save",
                                title="模拟错题复盘",
                                brief=old_brief,
                            )
                        ]
                    )
                elif current == 2:
                    task_id = tool_result(messages, "create-goal")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "create-followup",
                                "reminder_schedule",
                                title="约定复盘模拟错题",
                                content="只读当前目标，说明尚未核验事项与下一步，不执行任务。",
                                due_at=old_due,
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert current == 3
                    schedule = tool_result(messages, "create-followup")
                    reminder_id = schedule["id"]
                    turn = ModelTurn(content=replies[phase])
            elif phase == "reschedule":
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call("current-reminders", "reminder_list"),
                            ToolCall(
                                "update-goal",
                                "commitment_save",
                                json.dumps(
                                    {
                                        "identifier": task_id,
                                        "title": "模拟错题复盘",
                                        "brief": new_brief,
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        ]
                    )
                elif current == 2:
                    # Resolve the reminder from the actual preceding tool response.
                    listing = next(
                        message
                        for message in reversed(messages)
                        if message.tool_call_id == "current-reminders"
                    )
                    candidates = json.loads(listing.content)
                    assert len(candidates) == 1 and candidates[0]["id"] == reminder_id
                    assert tool_result(messages, "update-goal")["revision"] == 2
                    reschedule_available = any(
                        tool["function"]["name"] == "reminder_reschedule" for tool in tools
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "move-followup",
                                "reminder_reschedule",
                                id=candidates[0]["id"],
                                due_at=new_due,
                            )
                        ]
                    )
                else:
                    assert current == 3
                    # Missing-tool errors finish normally, then fail the outer target oracle.
                    rescheduled = tool_result(messages, "move-followup")
                    turn = ModelTurn(content=replies[phase])
            else:
                assert phase == "followup"
                assert "reminder_reschedule" not in {tool["function"]["name"] for tool in tools}
                if current == 1:
                    trigger = next(
                        message
                        for message in reversed(messages)
                        if message.role == "user" and message.content.startswith("【系统注意事项")
                    )
                    facts = json.loads(trigger.content[trigger.content.index("[") :])
                    followup["facts"] = facts
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "read-current-goal",
                                "commitment_read",
                                json.dumps(
                                    {"identifier": facts[0]["followup"]["commitment_id"]},
                                    ensure_ascii=False,
                                ),
                            )
                        ]
                    )
                else:
                    assert current == 2
                    followup["read"] = tool_result(messages, "read-current-goal")
                    requirements = json.loads(followup["read"]["text"])
                    followup["reply"] = (
                        f"改期后的复盘：{requirements['goal']}；"
                        + "；".join(requirements["constraints"])
                        + "。下一步先核查模拟错题；尚无外部成绩依据。"
                    )
                    turn = ModelTurn(content=followup["reply"])
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Main([], []) if config.model == "main" else FakeClient([], [])

    async def finish(expected_phase: str) -> dict[str, Any]:
        await eventually(
            active,
            lambda: (
                expected_phase in runs
                and active.store.get("run", runs[expected_phase])["status"]
                in {"completed", "failed"}
            ),
        )
        result = await active.wait(runs[expected_phase])
        assert result == replies[expected_phase]
        messages = completed(active, replies[expected_phase])
        assert len(messages) == 1
        return messages[0]

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    original_origin: dict[str, Any] = {}
    old_notice: dict[str, Any] | None = None
    attempted_old, release_old, rejected_old = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_submit = runtime.submit_attention

    async def held_submit(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        # Hold only the first real source capture, before its original final guard.
        # No receipt/ingress is synthesized and no admission result is substituted.
        assert not attempted_old.is_set()
        attempted_old.set()
        await release_old.wait()
        guard = kwargs["admission_guard"]

        def checked_guard() -> None:
            try:
                guard()
            except ValueError:
                rejected_old.set()
                raise

        return await original_submit(*args, **{**kwargs, "admission_guard": checked_guard})

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            patched = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert patched.status_code == 200 and patched.json()["mode"] == "yolo"
            assert (
                await phone(web, "记录这个模拟目标，并在约定时间复盘。", 5401)
            ).status_code == 200
            setup_reply = await finish("setup")
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, setup_reply["id"])
            original_origin = setup_reply["reply_origin"]
            assert original_origin["route"]["target"] == "7"
            before = deepcopy(runtime.store.get("reminder", reminder_id))
            assert before == schedule and before["due_at"] == old_due
            assert before["followup"] == {"version": 1, "commitment_id": task_id, "revision": 1}
            assert (
                before["routing"] == FrozenRouting.bound(ReplyOrigin.load(original_origin)).dump()
            )
            assert before["fired_count"] == 0 and before["last_fired_at"] is None

            if stale_notice:
                monkeypatch.setattr(runtime, "submit_attention", held_submit)
                clock = old_due
                runtime.reminders.changed.set()
                await asyncio.wait_for(attempted_old.wait(), 3)
                old_notice = runtime.store.get("notification", f"reminder:{reminder_id}:{old_due}")
                assert old_notice is not None and old_notice["followup"] == before["followup"]
                assert read_followup_notice(runtime.store, old_notice["id"], 6000) is not None
                assert not attention_inputs(runtime) and not runtime.store.list("attention_receipt")
                before = deepcopy(runtime.store.get("reminder", reminder_id))
                assert before["fired_count"] == 1 and before["last_fired_at"] == old_due
                assert before["enabled"] is False and before["followup_revision"] == 1

            phase = "reschedule"
            assert (
                await phone(
                    web,
                    "目标改为今晚复盘并列两项下一步，注明外部成绩未验；把原约定改期，仍在原私聊提醒。",
                    5402,
                    sender=8,
                )
            ).status_code == 200
            moved_reply = await finish("reschedule")
            await processed_reply(runtime, manager, moved_reply["id"])
            assert moved_reply["reply_origin"]["route"]["target"] == "8"
            actual_calls = [
                message
                for message in runtime.store.messages(main_id)
                if message.get("tool_call_id") == "move-followup"
            ]
            assert len(actual_calls) == 1 and json.loads(actual_calls[0]["content"]) == rescheduled
            assert rounds == {"setup": 3, "reschedule": 3}
            assert (
                app.state.local_services.commitments.get(task_id)["brief"] == normalized_new_brief
            )
            # Exact first target red: all real task/reminder/channel/run prerequisites passed.
            assert reschedule_available, (
                f"真实 main 未提供 reminder_reschedule；实际结果：{rescheduled}"
            )
            after = runtime.store.get("reminder", reminder_id)
            assert after == rescheduled and "error" not in rescheduled
            expected = {
                **before,
                "due_at": new_due,
                "enabled": True,
                "followup_revision": 2,
                "followup": {"version": 1, "commitment_id": task_id, "revision": 2},
            }
            assert after == expected
            assert [item["id"] for item in runtime.store.list("reminder")] == [reminder_id]
            if stale_notice:
                assert old_notice is not None
                assert read_followup_notice(runtime.store, old_notice["id"], 6000) is None
                assert runtime.store.get("notification", old_notice["id"]) == old_notice
                release_old.set()
                await asyncio.wait_for(rejected_old.wait(), 3)
                old_event = next(
                    event
                    for event in runtime.store.events()
                    if event["type"] == "notification.created"
                    and event["data"]["id"] == old_notice["id"]
                )
                await attention_eventually(lambda: app.state.attention.cursor >= old_event["id"])
                assert not attention_inputs(runtime) and not runtime.store.list("attention_receipt")
                assert rounds == {"setup": 3, "reschedule": 3}
                assert app.state.attention.last_error is None
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        release_old.set()
        await runtime.close()

    phase, clock = "followup", old_due
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
            assert reopened.store.list("notification") == ([old_notice] if old_notice else [])
            assert "followup" not in runs and not attention_inputs(reopened)
            assert not reopened.store.list("attention_receipt")
            assert reopened.store.get("reminder", reminder_id) == expected
            clock = new_due
            idle.set()
            await eventually(
                reopened,
                lambda: (
                    "followup" in runs
                    and reopened.store.get("run", runs["followup"])["status"]
                    in {"completed", "failed"}
                ),
            )
            assert await reopened.wait(runs["followup"]) == followup["reply"]
            assert rounds["followup"] == 2
            assert followup["read"]["id"] == task_id and followup["read"]["revision"] == 2
            assert followup["read"]["has_more"] is False
            assert json.loads(followup["read"]["text"]) == normalized_new_brief
            notices = reopened.store.list("notification")
            assert len(notices) == (2 if stale_notice else 1)
            notice = next(
                item for item in notices if item["id"] == f"reminder:{reminder_id}:{new_due}"
            )
            assert notice["followup"] == expected["followup"]
            assert notice["followup_occurrence"] == {
                "number": before["fired_count"] + 1,
                "due_at": new_due,
            }
            assert followup["facts"][0]["followup"] == expected["followup"]
            stored = reopened.store.get("reminder", reminder_id)
            assert (
                stored["fired_count"] == before["fired_count"] + 1
                and stored["last_fired_at"] == new_due
            )
            assert stored["routing"] == before["routing"] and stored["enabled"] is False
            reply = completed(reopened, followup["reply"])
            assert len(reply) == 1 and reply[0]["reply_origin"] == original_origin
            manager = reopened_app.state.channel_application.manager
            await processed_reply(reopened, manager, reply[0]["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == followup["reply"]
            ]
            assert len(deliveries) == 1 and deliveries[0]["route"]["target"] == "7"
            assert (await terminal(manager, deliveries[0]["id"]))["state"] == "sent"
            response = await web.get(f"/api/sessions/{main_id}/messages", params={"limit": 1})
            assert (
                response.status_code == 200
                and response.json()["messages"][0]["id"] == reply[0]["id"]
            )
            repeated = await reopened.bus.emit("notification.created", notice)
            await attention_eventually(
                lambda: reopened_app.state.attention.cursor >= repeated["id"]
            )
            assert rounds["followup"] == 2
            assert (
                len(
                    [
                        item
                        for item in manager.deliveries()
                        if delivery_text(item) == followup["reply"]
                    ]
                )
                == 1
            )
            assert all(len(messages) <= 32 for messages in seen["followup"])
            await asyncio.wait_for(reopened_app.state.close(), 3)
    finally:
        await reopened.close()
