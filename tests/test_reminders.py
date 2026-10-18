from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from muli.events import EventBus
from muli.reminders import Reminders
from muli.store import Store, now


async def test_reminder_fires_once_and_survives_restart(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    engine = Reminders(store, EventBus(store))
    item = engine.create("稍后提醒", now() + 0.02)
    await engine.start()
    for _ in range(50):
        if store.list("notification"):
            break
        await asyncio.sleep(0.005)
    await engine.close()
    assert len(store.list("notification")) == 1
    assert not store.get("reminder", item["id"])["enabled"]
    second = Reminders(store, EventBus(store))
    await second.start()
    await asyncio.sleep(0)
    await second.close()
    assert len(store.list("notification")) == 1
    store.close()


async def test_missed_recurrences_coalesce_without_storm(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    engine = Reminders(store, EventBus(store))
    item = engine.create("每分钟", now() + 5, interval_seconds=60)
    timestamp = item["due_at"] + 180
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
