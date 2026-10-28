from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import muli.reminders as reminders
from muli.events import EventBus
from muli.reminders import Reminders
from muli.store import Store, now


async def fire_due(engine: Reminders, item: dict[str, Any]) -> None:
    """Advance the actual clock for tests that previously passed a future snapshot."""
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(reminders, "now", lambda: item["due_at"])
        await engine._fire(item, item["due_at"])


class ObservedEvent(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waits: asyncio.Queue[None] = asyncio.Queue()

    async def wait(self) -> bool:
        self.waits.put_nowait(None)
        return await super().wait()


async def test_reminder_fires_once_and_survives_restart(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    item = engine.create("稍后提醒", now() + 0.02)
    await engine.start()
    try:
        # Short sleeps may wake early on coarse clocks. Wait for the observable result;
        # the timeout only detects a stalled scheduler, not how many sleeps have elapsed.
        async with asyncio.timeout(2), bus.changed:
            await bus.changed.wait_for(lambda: bool(store.list("notification")))
    finally:
        await engine.close()
    assert len(store.list("notification")) == 1
    assert store.list("notification")[0]["created_at"] >= item["due_at"]
    assert not store.get("reminder", item["id"])["enabled"]
    assert store.get("reminder", item["id"])["fired_count"] == 1
    store.close()

    store = Store(tmp_path / "state.db")
    second = Reminders(store, EventBus(store))
    idle = ObservedEvent()
    second.changed = idle
    await second.start()
    try:
        async with asyncio.timeout(2):
            await idle.waits.get()
        assert len(store.list("notification")) == 1
        assert store.get("reminder", item["id"])["fired_count"] == 1
    finally:
        await second.close()
        store.close()


async def test_early_wakeups_and_clock_rollback_do_not_fire_before_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = now()
    monkeypatch.setattr(reminders, "now", lambda: timestamp)
    store = Store(tmp_path / "state.db")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    changed = ObservedEvent()
    engine.changed = changed
    item = engine.create("按到期时间提醒", timestamp + 60)
    await engine.start()
    try:
        async with asyncio.timeout(2):
            await changed.waits.get()
            for offset in (-30, -90, -1):
                timestamp = item["due_at"] + offset
                engine.changed.set()
                await changed.waits.get()
                assert not store.list("notification")
            timestamp = item["due_at"]
            engine.changed.set()
            async with bus.changed:
                await bus.changed.wait_for(lambda: bool(store.list("notification")))
        notices = store.list("notification")
        assert len(notices) == 1
        assert notices[0]["created_at"] == item["due_at"]
    finally:
        await engine.close()
        store.close()


async def test_missed_recurrences_coalesce_without_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "state.db")
    engine = Reminders(store, EventBus(store))
    item = engine.create("每分钟", now() + 5, interval_seconds=60)
    timestamp = item["due_at"] + 180
    monkeypatch.setattr(reminders, "now", lambda: timestamp)
    await engine._fire(item, timestamp)
    await engine._fire(item, timestamp)
    assert len(store.list("notification")) == 1
    updated = store.get("reminder", item["id"])
    assert updated["fired_count"] == 1
    assert updated["due_at"] > timestamp
    store.close()


async def test_disabled_or_deleted_reminders_do_not_fire(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    engine = Reminders(store, EventBus(store))
    item = engine.create("不提醒", now() + 100)
    engine.update(item["id"], {"enabled": False})
    await engine._fire(item, now() + 100)
    assert not store.list("notification")
    engine.delete(item["id"])
    await engine._fire(item, now() + 100)
    assert not store.list("notification")
    with pytest.raises(ValueError):
        engine.create("过于频繁", now() + 5, interval_seconds=1)
    store.close()
