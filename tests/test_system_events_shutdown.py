from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from test_runtime import make_runtime

from muli.store import Store


def store_open(store: Store) -> bool:
    try:
        store.db.execute("SELECT 1").fetchone()
    except sqlite3.ProgrammingError:
        return False
    return True


async def test_mcp_shutdown_failure_stops_collector_and_preserves_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}})
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = runtime.mcp.close
    failure = RuntimeError("injected MCP shutdown failure")

    async def fail_close() -> None:
        entered.set()
        await release.wait()
        raise failure

    operation: asyncio.Task[None] | None = None
    try:
        await runtime.startup()
        assert runtime.system_logger is not None
        logger = runtime.system_logger.logger
        handler = runtime.system_events._handler
        assert handler is not None and handler in logger.handlers
        monkeypatch.setattr(runtime.mcp, "close", fail_close)
        operation = asyncio.create_task(runtime.close())
        await asyncio.wait_for(entered.wait(), 1)
        assert runtime.system_events.status()["running"]
        release.set()
        with pytest.raises(RuntimeError, match="injected MCP shutdown failure") as caught:
            await operation
        assert caught.value is failure
        # This test establishes collector ownership only. Database closure needs
        # a separate proof that every driver/finalizer can no longer write to it.
        record_property("store_open_after_shutdown_failure", store_open(runtime.store))
        assert not runtime.system_events.status()["running"]
        assert runtime.system_events.status()["queued"] == 0
        assert handler not in logger.handlers
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        monkeypatch.setattr(runtime.mcp, "close", original_close)
        await runtime.system_events.close()
        await original_close()
        await runtime.notifications.close()
        await runtime.computer.aclose()
        if store_open(runtime.store):
            runtime.store.close()
