"""Calendar reminders share the durable clock without sharing object namespaces."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_reminders import ObservedEvent

import muli.reminders as reminders_module
from muli.events import EventBus
from muli.maintenance import Admission
from muli.reminders import Reminders
from muli.store import Store


def utc(day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(2024, 1, day, hour, minute, tzinfo=UTC).timestamp()


def test_create_calendar_stores_a_complete_separate_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    store = Store(tmp_path / "calendar.sqlite")
    engine = Reminders(store, EventBus(store))
    try:
        item = engine.create_calendar("当地早晨", "Asia/Shanghai", "07:30", [5, 1, 3], "保留正文")
        assert item["kind"] == "calendar_reminder"
        saved = store.get("calendar_reminder", item["id"])
        assert saved is not None
        assert saved["calendar"] == {
            "version": 1,
            "timezone": "Asia/Shanghai",
            "local_time": "07:30",
            "weekdays": [1, 3, 5],
        }
        assert saved["due_at"] == utc(2, 23, 30)
        assert saved["interval_seconds"] is None
        assert saved["enabled"] and saved["fired_count"] == 0
        assert saved["content"] == "保留正文" and saved["created_at"] == timestamp
        assert store.get("reminder", item["id"]) is None
        assert engine.records()[0]["kind"] == "calendar_reminder"
    finally:
        store.close()


async def test_restart_coalesces_missed_days_once_and_keeps_the_next_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    database = tmp_path / "calendar.sqlite"
    store = Store(database)
    try:
        item = Reminders(store, EventBus(store)).create_calendar(
            "每天背单词", "Asia/Shanghai", "07:30", list(range(1, 8))
        )
    finally:
        store.close()

    timestamp = utc(8)
    store = Store(database)
    bus = EventBus(store)
    engine = Reminders(store, bus)
    idle = ObservedEvent()
    engine.changed = idle
    try:
        await engine.start()
        async with asyncio.timeout(3):
            await idle.waits.get()
        notices = store.list("notification")
        assert len(notices) == 1
        assert notices[0]["id"] == f"calendar_reminder:{item['id']}:{item['due_at']}"
        assert notices[0]["source"] == "reminder"
        assert notices[0]["reminder_kind"] == "calendar_reminder"
        assert notices[0]["created_at"] == timestamp
        saved = store.get("calendar_reminder", item["id"])
        assert saved is not None and saved["fired_count"] == 1 and saved["enabled"]
        assert saved["last_fired_at"] == timestamp
        assert saved["due_at"] == utc(8, 23, 30) > timestamp
        engine.changed.set()
        async with asyncio.timeout(3):
            await idle.waits.get()
        assert store.list("notification") == notices
        assert store.get("calendar_reminder", item["id"]) == saved
    finally:
        await engine.close()
        store.close()

    store = Store(database)
    engine = Reminders(store, EventBus(store))
    idle = ObservedEvent()
    engine.changed = idle
    try:
        await engine.start()
        async with asyncio.timeout(3):
            await idle.waits.get()
        assert store.list("notification") == notices
        assert store.get("calendar_reminder", item["id"]) == saved
    finally:
        await engine.close()
        store.close()


def test_same_id_management_is_exact_and_never_falls_back_to_another_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    monkeypatch.setattr(reminders_module, "new_id", lambda: "same-id")
    store = Store(tmp_path / "calendar.sqlite")
    engine = Reminders(store, EventBus(store))
    try:
        plain = engine.create("普通提醒", timestamp + 60)
        calendar = engine.create_calendar("日历提醒", "Asia/Shanghai", "07:30", list(range(1, 8)))
        assert plain["id"] == calendar["id"]
        assert {record["kind"] for record in engine.records()} == {
            "reminder",
            "calendar_reminder",
        }
        assert len(engine.records(limit=1)) == 1
        saved_calendar = store.get("calendar_reminder", calendar["id"])
        engine.update(plain["id"], {"title": "只改普通提醒"})
        assert store.get("calendar_reminder", calendar["id"]) == saved_calendar
        saved_plain = store.get("reminder", plain["id"])
        engine.update(
            calendar["id"],
            {"title": "只改日历提醒", "due_at": utc(1, 12)},
            kind="calendar_reminder",
        )
        assert store.get("reminder", plain["id"]) == saved_plain
        changed = store.get("calendar_reminder", calendar["id"])
        assert changed is not None and saved_calendar is not None
        assert changed["calendar"] == saved_calendar["calendar"]
        assert engine.delete(plain["id"])
        assert not engine.delete(plain["id"])
        with pytest.raises(KeyError):
            engine.update(plain["id"], {"title": "禁止跨类查找"})
        assert store.get("calendar_reminder", calendar["id"]) == changed
        assert engine.delete(calendar["id"], kind="calendar_reminder")
        assert engine.records() == []
    finally:
        store.close()


async def test_record_namespaces_come_from_sql_and_same_ids_fire_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    monkeypatch.setattr(reminders_module, "new_id", lambda: "same-id")
    store = Store(tmp_path / "calendar.sqlite")
    engine = Reminders(store, EventBus(store))
    try:
        calendar = engine.create_calendar("日历记录", "Asia/Shanghai", "07:30", list(range(1, 8)))
        plain = engine.create("普通记录", calendar["due_at"])
        store.put("reminder", {**plain, "kind": "calendar_reminder"})
        store.put("calendar_reminder", {**calendar, "kind": "reminder"})
        expected = {"普通记录": "reminder", "日历记录": "calendar_reminder"}
        assert {record["title"]: record["kind"] for record in engine.records()} == expected
        active = engine._active_reminders()
        assert {record["title"]: record["kind"] for record in active} == expected
        timestamp = calendar["due_at"]
        for record in active:
            await engine._fire(record, timestamp)
        notices = store.list("notification")
        assert {notice["id"] for notice in notices} == {
            f"reminder:same-id:{timestamp}",
            f"calendar_reminder:same-id:{timestamp}",
        }
        ordinary_notice = next(notice for notice in notices if notice["title"] == "普通记录")
        assert "reminder_kind" not in ordinary_notice
        updates = [event["data"] for event in store.events() if event["type"] == "reminder.updated"]
        assert {event["title"]: event["kind"] for event in updates} == expected
        saved_plain = store.get("reminder", "same-id")
        saved_calendar = store.get("calendar_reminder", "same-id")
        assert saved_plain is not None and saved_plain["fired_count"] == 1
        assert not saved_plain["enabled"]
        assert saved_calendar is not None and saved_calendar["fired_count"] == 1
        assert saved_calendar["enabled"] and saved_calendar["due_at"] > timestamp
    finally:
        store.close()


@pytest.mark.parametrize("kind", [None, True, "notification", "unknown"])
def test_unknown_management_kind_cannot_modify_either_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Any
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    monkeypatch.setattr(reminders_module, "new_id", lambda: "same-id")
    store = Store(tmp_path / "calendar.sqlite")
    engine = Reminders(store, EventBus(store))
    try:
        engine.create("普通提醒", timestamp + 60)
        engine.create_calendar("日历提醒", "Asia/Shanghai", "07:30", [1])
        before = engine.records()
        with pytest.raises(ValueError):
            engine.update("same-id", {"title": "禁止修改"}, kind=kind)
        with pytest.raises(ValueError):
            engine.delete("same-id", kind=kind)
        assert engine.records() == before
    finally:
        store.close()


@pytest.mark.parametrize(
    "field", ["commitment_id", "followup", "followup_revision", "followup_occurrence"]
)
async def test_null_task_binding_fields_are_rejected_before_calendar_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    store = Store(tmp_path / "calendar.sqlite")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    try:
        item = engine.create_calendar("个人提醒", "Asia/Shanghai", "07:30", list(range(1, 8)))
        before = store.get("calendar_reminder", item["id"])
        with pytest.raises(ValueError):
            engine.update(item["id"], {field: None}, kind="calendar_reminder")
        assert store.get("calendar_reminder", item["id"]) == before
        assert before is not None
        damaged = {**before, field: None}
        store.put("calendar_reminder", damaged)
        timestamp = item["due_at"]
        with pytest.raises(ValueError):
            await engine._fire(item, timestamp)
        assert store.get("calendar_reminder", item["id"]) == damaged
        assert not store.list("notification") and not store.events()
        assert bus.revision == 0
    finally:
        store.close()


async def test_rescheduling_changes_only_the_next_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    store = Store(tmp_path / "calendar.sqlite")
    engine = Reminders(store, EventBus(store))
    try:
        item = engine.create_calendar("只改下一次", "Asia/Shanghai", "07:30", list(range(1, 8)))
        selected = engine.update(item["id"], {"due_at": utc(1, 12)}, kind="calendar_reminder")
        timestamp = utc(1, 12)
        await engine._fire(selected, timestamp)
        saved = store.get("calendar_reminder", item["id"])
        assert saved is not None and saved["calendar"] == item["calendar"]
        assert saved["due_at"] == utc(1, 23, 30) and saved["fired_count"] == 1
        await engine._fire(selected, timestamp)
        assert len(store.list("notification")) == 1
    finally:
        store.close()


async def test_real_transaction_failure_does_not_partly_advance_calendar_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    store = Store(tmp_path / "calendar.sqlite")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    try:
        item = engine.create_calendar("事务一起提交", "Asia/Shanghai", "07:30", list(range(1, 8)))
        before = store.get("calendar_reminder", item["id"])
        with store.db:
            store.db.execute(
                "CREATE TRIGGER reject_calendar_advance AFTER UPDATE ON objects "
                "WHEN NEW.kind='calendar_reminder' "
                "BEGIN SELECT RAISE(ABORT,'calendar_advance_rolled_back'); END"
            )
        timestamp = item["due_at"]
        with pytest.raises(sqlite3.IntegrityError, match="calendar_advance_rolled_back"):
            await engine._fire(item, timestamp)
        assert not store.db.in_transaction
        assert store.get("calendar_reminder", item["id"]) == before
        assert not store.list("notification") and not store.events()
        assert bus.revision == 0
        with store.db:
            store.db.execute("DROP TRIGGER reject_calendar_advance")
        await engine._fire(item, timestamp)
        saved = store.get("calendar_reminder", item["id"])
        assert saved is not None and saved["fired_count"] == 1
        assert saved["due_at"] > timestamp
        assert len(store.list("notification")) == 1 and bus.revision == 1
    finally:
        store.close()


async def test_admission_wait_refreshes_current_content_and_coalesces_against_new_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    admission = Admission(allowed=False)
    released = ObservedEvent()
    monkeypatch.setattr(admission, "_released", released)
    store = Store(tmp_path / "calendar.sqlite")
    engine = Reminders(store, EventBus(store), admission=admission)
    item = engine.create_calendar(
        "等待前正文", "Asia/Shanghai", "07:30", list(range(1, 8)), "等待前内容"
    )
    timestamp = item["due_at"]
    pending = asyncio.create_task(engine._fire(item, timestamp))
    try:
        async with asyncio.timeout(3):
            await released.waits.get()
        assert not pending.done() and admission.idle
        current = store.get("calendar_reminder", item["id"])
        assert current is not None
        store.put("calendar_reminder", {**current, "title": "等待后正文", "content": "当前内容"})
        timestamp = utc(5)
        admission.release()
        async with asyncio.timeout(3):
            await pending
        notices = store.list("notification")
        assert len(notices) == 1
        assert notices[0]["title"] == "等待后正文" and notices[0]["content"] == "当前内容"
        assert notices[0]["created_at"] == timestamp
        assert notices[0]["id"] == f"calendar_reminder:{item['id']}:{item['due_at']}"
        saved = store.get("calendar_reminder", item["id"])
        assert saved is not None and saved["fired_count"] == 1
        assert saved["last_fired_at"] == timestamp
        assert saved["due_at"] == utc(5, 23, 30) > timestamp
        await engine._fire(item, timestamp)
        assert store.list("notification") == notices
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        store.close()


@pytest.mark.parametrize("change", ["rollback", "disable", "delete", "reschedule"])
async def test_admission_wait_rechecks_clock_and_current_occurrence_before_emitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    admission = Admission(allowed=False)
    released = ObservedEvent()
    monkeypatch.setattr(admission, "_released", released)
    store = Store(tmp_path / "calendar.sqlite")
    bus = EventBus(store)
    engine = Reminders(store, bus, admission=admission)
    item = engine.create_calendar("复核发生身份", "Asia/Shanghai", "07:30", list(range(1, 8)))
    timestamp = item["due_at"]
    pending = asyncio.create_task(engine._fire(item, timestamp))
    try:
        async with asyncio.timeout(3):
            await released.waits.get()
        if change == "rollback":
            timestamp -= 60
        elif change == "disable":
            engine.update(item["id"], {"enabled": False}, kind="calendar_reminder")
        elif change == "delete":
            engine.delete(item["id"], kind="calendar_reminder")
        else:
            engine.update(item["id"], {"due_at": timestamp + 3600}, kind="calendar_reminder")
        before_release = store.get("calendar_reminder", item["id"])
        admission.release()
        async with asyncio.timeout(3):
            await pending
        assert store.get("calendar_reminder", item["id"]) == before_release
        assert not store.list("notification") and not store.events()
        assert bus.revision == 0 and admission.idle
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        store.close()


async def test_failed_calendar_does_not_park_same_id_ordinary_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = utc(1)
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    monkeypatch.setattr(reminders_module, "new_id", lambda: "same-id")
    store = Store(tmp_path / "calendar.sqlite")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    changed = ObservedEvent()
    engine.changed = changed
    try:
        calendar = engine.create_calendar(
            "坏日历独立隔离", "Asia/Shanghai", "07:30", list(range(1, 8))
        )
        ordinary = engine.create("同 ID 普通提醒继续", calendar["due_at"] + 0.02)
        saved = store.get("calendar_reminder", calendar["id"])
        assert saved is not None
        damaged = {**saved, "followup": None}
        store.put("calendar_reminder", damaged)
        timestamp = calendar["due_at"]
        await engine.start()
        async with asyncio.timeout(3):
            await changed.waits.get()
        # The healthy item's own finite timeout must wake the existing clock;
        # a changed signal here would also clear the failed namespace's state.
        timestamp = ordinary["due_at"]
        async with asyncio.timeout(3), bus.changed:
            await bus.changed.wait_for(lambda: bool(store.list("notification")))
        notices = store.list("notification")
        assert len(notices) == 1 and notices[0]["title"] == "同 ID 普通提醒继续"
        assert notices[0]["id"].startswith("reminder:same-id:")
        assert store.get("calendar_reminder", "same-id") == damaged
        assert engine.task is not None and not engine.task.done()
    finally:
        await engine.close()
        store.close()
