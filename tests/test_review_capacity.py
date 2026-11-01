from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from test_runtime import make_runtime

from muli.providers import Message, ModelConfig, ModelTurn


class SlowReviewClient:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self) -> SlowReviewClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def complete(
        self, messages: list[Message], tools: Any = (), *, attempt_guard: Any = None
    ) -> ModelTurn:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= 2:
            self.entered.set()
        await self.release.wait()
        self.active -= 1
        return ModelTurn(content='{"allow":true,"reason":"有界只读"}')


async def test_review_has_its_own_capacity_and_reports_main_source(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    client = SlowReviewClient()
    runtime.client_factory = lambda config: client  # type: ignore[assignment]
    runtime.config.update({"limits": {"max_parallel_agents": 1, "review_parallel": 2}})
    runtime.config.update({"models": {"reviewer": {"model": "", "base_url": ""}}})
    runtime.configure()
    try:
        first = asyncio.create_task(runtime.review_with_source("fs_read", {}, "read"))
        second = asyncio.create_task(runtime.review_with_source("fs_read", {}, "read"))
        await asyncio.wait_for(client.entered.wait(), 2)
        client.release.set()
        results = await asyncio.gather(first, second)
        assert client.max_active == 2
        assert all(result == (True, "有界只读", "main") for result in results)
    finally:
        await runtime.close()


async def test_configured_reviewer_is_reported_separately(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        _, source = runtime.reviewer_config()
        assert source == "configured"
    finally:
        await runtime.close()
