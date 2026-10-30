"""Real HTTP await budgets with one in-memory stream and no native effects."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import managed_native_worker as worker
from test_managed_native_ci import forbid_real_effects as forbid_real_effects

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux native CI fixture")

# Freeze a response beyond the former two-second HTTP limit, not the new budget.
DELAYED_REPLY_SECONDS: float = 2.25
REQUEST_PATH: str = "/api/management/transactions"
REQUEST_BODY: dict[str, Any] = {"action": "switch"}


@dataclass
class MemoryResponse:
    headers: httpx.Headers
    status_code: int = 200

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        yield b'{"id":"offline-request"}'


@dataclass
class DelayedClient:
    calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: list[asyncio.CancelledError] = field(default_factory=list)
    exits: int = 0

    @asynccontextmanager
    async def stream(
        self, method: str, path: str, *, json: dict[str, Any] | None
    ) -> AsyncIterator[MemoryResponse]:
        self.calls.append((method, path, json))
        self.entered.set()
        try:
            # This runs inside the actual stream __aenter__ and asyncio timeout.
            await asyncio.sleep(DELAYED_REPLY_SECONDS)
            yield MemoryResponse(httpx.Headers({"content-encoding": "identity"}))
        except asyncio.CancelledError as error:
            self.cancelled.append(error)
            raise
        finally:
            self.exits += 1


async def test_http_accepts_one_delayed_reply_within_case_budget(tmp_path: Path) -> None:
    case: worker.Case = worker.Case(tmp_path)
    client: DelayedClient = DelayedClient()
    assert case.http.__func__ is worker.Case.http
    try:
        reply: tuple[int, dict[str, Any]] = await case.http(
            client, "POST", REQUEST_PATH, REQUEST_BODY
        )
    finally:
        assert client.entered.is_set()
        assert client.calls == [("POST", REQUEST_PATH, REQUEST_BODY)]
        assert client.exits == 1
    assert reply == (200, {"id": "offline-request"})
    assert client.cancelled == []


async def test_http_preserves_the_shorter_original_case_deadline(tmp_path: Path) -> None:
    case: worker.Case = worker.Case(tmp_path, seconds=worker.POLL_SECONDS)
    deadline: float = case.deadline
    client: DelayedClient = DelayedClient()
    with pytest.raises(TimeoutError):
        await case.http(client, "POST", REQUEST_PATH, REQUEST_BODY)
    assert case.deadline == deadline
    assert client.entered.is_set()
    assert client.calls == [("POST", REQUEST_PATH, REQUEST_BODY)]
    assert client.exits == 1 and len(client.cancelled) == 1
    assert "worker_await" not in case.result["resources"]


async def test_http_propagates_external_cancellation_without_retry(tmp_path: Path) -> None:
    case: worker.Case = worker.Case(tmp_path)
    client: DelayedClient = DelayedClient()
    task: asyncio.Task[tuple[int, dict[str, Any]]] = asyncio.create_task(
        case.http(client, "POST", REQUEST_PATH, REQUEST_BODY)
    )
    try:
        async with asyncio.timeout(worker.PROBE_SECONDS):
            await client.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
        assert len(client.cancelled) == 1 and caught.value is client.cancelled[0]
        assert client.calls == [("POST", REQUEST_PATH, REQUEST_BODY)]
        assert client.exits == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
