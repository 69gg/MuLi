"""Durable events with wakeups rather than model polling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from muli.store import Store


class EventBus:
    def __init__(self, store: Store, heartbeat_seconds: float = 20) -> None:
        self.store = store
        self.heartbeat_seconds = heartbeat_seconds
        self.changed = asyncio.Condition()
        self.revision = 0

    async def emit(
        self, event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        event = self.store.event(event_type, data, session_id)
        await self.notify()
        return event

    async def notify(self) -> None:
        """Wake observers after an event was committed with its related objects."""
        async with self.changed:
            self.revision += 1
            self.changed.notify_all()

    async def stream(self, after: int = 0, session_id: str | None = None) -> AsyncIterator[str]:
        while True:
            revision = self.revision
            events = self.store.events(after, session_id)
            for event in events:
                after = event["id"]
                yield f"id: {after}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if events:
                await asyncio.sleep(0)
                continue
            heartbeat = False

            def changed(expected: int = revision) -> bool:
                return self.revision != expected

            async with self.changed:
                try:
                    await asyncio.wait_for(
                        self.changed.wait_for(changed),
                        timeout=self.heartbeat_seconds,
                    )
                except TimeoutError:
                    heartbeat = True
            if heartbeat:
                yield ": keepalive\n\n"
