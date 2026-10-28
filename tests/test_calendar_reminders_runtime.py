"""Real owner tool turns and local management retain a calendar's identity and route."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

import muli.reminders as reminders_module
from muli.providers import Message, ModelTurn, StreamEvent
from muli.server import create_app
from muli.system_events import attention_source


async def test_owner_schedules_lists_reschedules_reopens_fires_and_cancels_one_calendar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "compaction": {"enabled": False},
            "memory": {"auto_extract": False},
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
        }
    )
    await runtime.reconfigure()
    created: dict[str, Any] = {}
    moved: dict[str, Any] = {}
    rounds = 0

    class Planner(FakeClient):
        async def stream(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]],
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds, created, moved
            rounds += 1
            functions = {item["function"]["name"]: item["function"] for item in tools}
            assert "reminder_schedule_calendar" in functions
            assert runtime.tools["reminder_schedule_calendar"].risk == "write"
            assert set(functions["reminder_schedule_calendar"]["parameters"]["required"]) == {
                "title",
                "timezone",
                "local_time",
                "weekdays",
            }
            if rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "calendar-create",
                            "reminder_schedule_calendar",
                            title="本地早晨",
                            timezone="Asia/Shanghai",
                            local_time="07:30",
                            weekdays=[1, 2, 3, 4, 5],
                        )
                    ]
                )
            elif rounds == 2:
                created = tool_result(messages, "calendar-create")
                assert created["kind"] == "calendar_reminder"
                turn = ModelTurn(tool_calls=[call("calendar-list", "reminder_list")])
            elif rounds == 3:
                listing_message = next(
                    message
                    for message in reversed(messages)
                    if message.tool_call_id == "calendar-list"
                )
                listing = json.loads(listing_message.content)
                assert isinstance(listing, list)
                assert len(listing) == 1
                assert listing[0]["id"] == created["id"]
                assert listing[0]["kind"] == created["kind"]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "calendar-move",
                            "reminder_reschedule",
                            id=listing[0]["id"],
                            kind=listing[0]["kind"],
                            due_at=timestamp + 120,
                        )
                    ]
                )
            else:
                assert rounds == 4
                moved = tool_result(messages, "calendar-move")
                assert moved["id"] == created["id"] and moved["calendar"] == created["calendar"]
                turn = ModelTurn(content="已设置当地作息，只将下一次改到两分钟后。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Planner([], seen)  # type: ignore[assignment,return-value]
    try:
        started = await runtime.submit(
            "上海时间工作日早晨提醒我，下一次先改到两分钟后。", mode="yolo"
        )
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == (
            "已设置当地作息，只将下一次改到两分钟后。"
        )
        assert rounds == 4 and created and moved
        assert moved["routing"] == created["routing"]
        assert moved["routing"]["source_input_id"] == started["message"]["id"]
        assert (
            not runtime.store.list("reminder") and len(runtime.store.list("calendar_reminder")) == 1
        )
        assert not runtime.store.list("notification")
    finally:
        await runtime.close()

    second, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(
                tool_calls=[
                    call(
                        "calendar-cancel",
                        "reminder_cancel",
                        id=created["id"],
                        kind=created["kind"],
                    )
                ]
            ),
            ModelTurn(content="这条当地作息提醒已取消。"),
        ],
    )
    try:
        timestamp = moved["due_at"]
        selected = second.reminders.records()[0]
        await second.reminders._fire(selected, timestamp)
        saved = second.store.get("calendar_reminder", selected["id"])
        assert saved is not None and saved["fired_count"] == 1 and saved["due_at"] > timestamp
        assert saved["calendar"] == created["calendar"] and saved["routing"] == created["routing"]
        event = next(
            item for item in second.store.events() if item["type"] == "notification.created"
        )
        source = attention_source(second.store, event, ["reminder"], max_output_chars=6000)
        assert source is not None and source.routing.origin.input_id == started["message"]["id"]
        assert source.reminder_proof is None
        cancelled = await second.submit("取消刚才的日历提醒。", mode="yolo")
        assert (
            await asyncio.wait_for(second.wait(cancelled["run_id"]), 5)
            == "这条当地作息提醒已取消。"
        )
        assert second.reminders.records() == []
        assert len(second.store.list("notification")) == 1
    finally:
        await second.close()


async def test_management_queries_are_exact_for_same_id_and_reject_coerced_weekdays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    monkeypatch.setattr(reminders_module, "new_id", lambda: "same-id")
    runtime, _ = make_runtime(tmp_path, [])
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8765",
        ) as web:
            body = {
                "title": "按日历",
                "timezone": "Asia/Shanghai",
                "local_time": "07:30",
                "weekdays": [1, 2],
            }
            invalid = await web.post("/api/calendar-reminders", json={**body, "weekdays": [True]})
            assert invalid.status_code == 422
            assert not runtime.store.list("calendar_reminder")
            created = await web.post("/api/calendar-reminders", json=body)
            assert created.status_code == 200 and created.json()["kind"] == "calendar_reminder"
            plain = runtime.reminders.create("保留普通", timestamp + 300)
            listing = await web.get("/api/reminders")
            assert {item["kind"] for item in listing.json()["items"]} == {
                "reminder",
                "calendar_reminder",
            }
            edited = await web.patch(
                "/api/reminders/same-id?kind=calendar_reminder",
                json={"title": "只改日历"},
            )
            assert edited.status_code == 200 and edited.json()["kind"] == "calendar_reminder"
            assert runtime.store.get("reminder", "same-id") == plain
            invalid = await web.delete("/api/reminders/same-id?kind=unknown")
            assert invalid.status_code == 422
            removed = await web.delete("/api/reminders/same-id?kind=calendar_reminder")
            assert removed.status_code == 200 and removed.json()["deleted"] is True
            assert runtime.store.get("reminder", "same-id") == plain
            assert runtime.store.get("calendar_reminder", "same-id") is None
            missing = await web.patch(
                "/api/reminders/same-id?kind=calendar_reminder",
                json={"title": "不得改普通"},
            )
            assert missing.status_code == 404 and runtime.store.get("reminder", "same-id") == plain
    finally:
        await runtime.close()
