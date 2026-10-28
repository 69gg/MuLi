"""An accepted same-due edit after a clock rollback must be delivered.

The public update contract rejects a same-due edit once the reminder is due.
This case therefore models the reachable rollback boundary explicitly instead
of calling _fire with a future timestamp while pretending the clock is due.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

import muli.reminders as reminders
from muli.events import EventBus
from muli.maintenance import Admission
from muli.reminders import Reminders
from muli.store import Store, now


class ObservedAdmission(Admission):
    """Observe the real closed gate; retain Admission's wait/operation behavior."""

    def __init__(self) -> None:
        super().__init__(allowed=False)
        self.waiting = asyncio.Event()

    async def wait(self) -> None:
        if not self.allowed:
            self.waiting.set()
        await super().wait()


async def test_ordinary_reminder_uses_accepted_edit_after_clock_rollback_during_admission_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp: float = now()
    monkeypatch.setattr(reminders, "now", lambda: timestamp)
    store = Store(tmp_path / "state.db")
    admission = ObservedAdmission()
    engine = Reminders(store, EventBus(store), admission=admission)
    item = engine.create("原提醒标题", timestamp + 60, "原提醒正文")
    timestamp = item["due_at"]
    firing = asyncio.create_task(engine._fire(item, timestamp))
    try:
        async with asyncio.timeout(2):
            await admission.waiting.wait()
            assert not firing.done()
            assert not store.list("notification")

            # Normal due-time edits are rejected by the public contract.
            changes: dict[str, str] = {"title": "已修改标题", "content": "已修改正文"}
            with pytest.raises(ValueError, match="启用提醒需要一个未来时间"):
                engine.update(item["id"], changes)

            # A real clock rollback during the blocked admission makes the
            # unchanged due_at future again, so this public update is accepted.
            timestamp = item["due_at"] - 1
            updated = engine.update(item["id"], changes)
            assert updated["enabled"] is True
            assert updated["due_at"] == item["due_at"]
            assert updated["title"] == changes["title"]
            assert updated["content"] == changes["content"]
            assert "followup_revision" not in updated
            assert not firing.done()
            assert not store.list("notification")

            # Reaching the deadline again isolates stale content from the
            # separate early-fire regression below.
            timestamp = item["due_at"]
            assert admission.release() is True
            await firing

        saved = store.get("reminder", item["id"])
        assert saved is not None
        assert saved["title"] == changes["title"]
        assert saved["content"] == changes["content"]
        assert saved["fired_count"] == 1
        assert saved["enabled"] is False
        notices = store.list("notification")
        assert len(notices) == 1
        assert notices[0]["reminder_id"] == item["id"]
        assert notices[0]["title"] == changes["title"]
        assert notices[0]["content"] == changes["content"]
    finally:
        if not firing.done():
            firing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await firing
        store.close()


async def test_ordinary_reminder_does_not_fire_if_clock_rolled_back_during_admission_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp: float = now()
    monkeypatch.setattr(reminders, "now", lambda: timestamp)
    store = Store(tmp_path / "state.db")
    admission = ObservedAdmission()
    engine = Reminders(store, EventBus(store), admission=admission)
    item = engine.create("等待真正到期", timestamp + 60, "不能提前交付")
    timestamp = item["due_at"]
    firing = asyncio.create_task(engine._fire(item, timestamp))
    try:
        async with asyncio.timeout(2):
            await admission.waiting.wait()
            assert not firing.done()
            timestamp = item["due_at"] - 1
            assert admission.release() is True
            await firing

        assert not store.list("notification")
        saved = store.get("reminder", item["id"])
        assert saved is not None
        assert saved["enabled"] is True
        assert saved["fired_count"] == 0
        assert saved["last_fired_at"] is None
        assert saved["due_at"] == item["due_at"]
    finally:
        if not firing.done():
            firing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await firing
        store.close()
