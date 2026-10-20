"""Durable deadline scheduling; idle reminders never call a model."""

from __future__ import annotations

import asyncio
import contextlib
import math
from typing import Any

from muli.events import EventBus
from muli.maintenance import Admission
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store import Store, new_id, now


class Reminders:
    def __init__(self, store: Store, bus: EventBus, *, admission: Admission | None = None) -> None:
        self.store, self.bus = store, bus
        self.admission = admission or Admission()
        self.changed = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def create(
        self,
        title: str,
        due_at: float,
        content: str = "",
        interval_seconds: int | None = None,
        *,
        reply_origin: ReplyOrigin | None = None,
    ) -> dict[str, Any]:
        if not title.strip() or len(title) > 120 or len(content) > 4000:
            raise ValueError("提醒标题或内容长度无效")
        if not math.isfinite(due_at) or due_at <= now():
            raise ValueError("提醒时间必须在未来")
        if interval_seconds is not None and not 60 <= interval_seconds <= 31_536_000:
            raise ValueError("重复间隔需为60秒至一年")
        item = {
            "id": new_id(),
            "title": title.strip(),
            "content": content,
            "due_at": due_at,
            "interval_seconds": interval_seconds,
            "enabled": True,
            "created_at": now(),
            "last_fired_at": None,
            "fired_count": 0,
            "routing": FrozenRouting.bound(reply_origin or ReplyOrigin()).dump(),
        }
        self.store.put("reminder", item)
        self.changed.set()
        return item

    def update(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        item = self.store.get("reminder", identifier)
        if item is None:
            raise KeyError(identifier)
        if set(changes) - {"title", "content", "due_at", "interval_seconds", "enabled"}:
            raise ValueError("未知提醒字段")
        candidate = {**item, **changes}
        if not isinstance(candidate["enabled"], bool):
            raise ValueError("enabled 必须是布尔值")
        if (
            not isinstance(candidate["title"], str)
            or not candidate["title"].strip()
            or len(candidate["title"]) > 120
            or not isinstance(candidate["content"], str)
            or len(candidate["content"]) > 4000
        ):
            raise ValueError("提醒标题或内容无效")
        due = candidate["due_at"]
        if not isinstance(due, (float, int)) or not math.isfinite(due):
            raise ValueError("提醒时间无效")
        if candidate["enabled"] and due <= now():
            raise ValueError("启用提醒需要一个未来时间")
        interval = candidate["interval_seconds"]
        if interval is not None and (type(interval) is not int or not 60 <= interval <= 31_536_000):
            raise ValueError("重复间隔无效")
        self.store.put("reminder", candidate)
        self.changed.set()
        return candidate

    def delete(self, identifier: str) -> bool:
        deleted = self.store.delete("reminder", identifier)
        self.changed.set()
        return deleted

    async def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="muli-reminders")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _fire(self, item: dict[str, Any], timestamp: float) -> None:
        await self.admission.wait()
        with self.admission.operation():
            await self._fire_admitted(item, timestamp)

    async def _fire_admitted(self, item: dict[str, Any], timestamp: float) -> None:
        current = self.store.get("reminder", item["id"])
        if not current or not current["enabled"] or current["due_at"] != item["due_at"]:
            return
        notice = {
            "id": f"reminder:{item['id']}:{item['due_at']}",
            "title": item["title"],
            "content": item["content"],
            "source": "reminder",
            "read": False,
            "created_at": timestamp,
            "reminder_id": item["id"],
            "routing": FrozenRouting.load(current.get("routing")).dump(),
        }
        interval = item["interval_seconds"]
        current["last_fired_at"] = timestamp
        current["fired_count"] += 1
        if interval:
            # Coalesce missed occurrences after downtime; never emit a catch-up storm.
            missed = math.floor(max(0, timestamp - item["due_at"]) / interval) + 1
            current["due_at"] = item["due_at"] + missed * interval
        else:
            current["enabled"] = False
        with self.store.db:
            self.store._event(
                "notification.created",
                notice,
                objects=[("notification", notice), ("reminder", current)],
            )
            self.store._event("reminder.updated", current)
        await self.bus.notify()

    async def _run(self) -> None:
        while True:
            await self.admission.wait()
            self.changed.clear()
            active = [item for item in self.store.list("reminder", 10000) if item["enabled"]]
            timestamp = now()
            for item in active:
                if item["due_at"] <= timestamp:
                    await self._fire(item, timestamp)
            active = [item for item in self.store.list("reminder", 10000) if item["enabled"]]
            delay = max(0.01, min(item["due_at"] for item in active) - now()) if active else None
            try:
                if delay is None:
                    await self.changed.wait()
                else:
                    await asyncio.wait_for(self.changed.wait(), delay)
            except TimeoutError:
                pass
