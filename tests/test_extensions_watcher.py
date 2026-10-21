"""Offline lifecycle regressions for extension observation and watcher ownership."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import httpx
import pytest
from test_extensions import setup_runtime
from test_mcp import FakeClient

import muli.extensions as module
from muli.config import Settings
from muli.extensions import ExtensionError
from muli.runtime import Runtime
from muli.tools.mcp import MCPManager


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Watcher tests must not use external networks or subprocesses")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    instance = setup_runtime(tmp_path)
    instance.config.settings.extensions.scan_interval_seconds = 300
    instance.mcp = MCPManager(instance.config.settings.mcp, client_factory=lambda *_: FakeClient())
    try:
        await instance.extensions.load_initial()
        yield instance
    finally:
        worker = instance.extensions._worker
        if worker is not None and worker.done() and not worker.cancelled():
            if worker.exception() is not None:
                # Let the old implementation's failing task remain observable in
                # assertions without preventing cleanup of the remaining Runtime.
                instance.extensions._worker = None
        await instance.close()


def observe_waits(monkeypatch: pytest.MonkeyPatch) -> asyncio.Queue[float | None]:
    waits: asyncio.Queue[float | None] = asyncio.Queue()

    async def wait_for[T](operation: Awaitable[T], timeout: float | None) -> T:
        waits.put_nowait(timeout)
        return await asyncio.wait_for(operation, timeout)

    monkeypatch.setattr(
        module, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": wait_for})
    )
    return waits


@pytest.mark.parametrize("point", ["before_lock", "inside_lock", "waiting"])
async def test_observation_error_keeps_owner_alive_and_later_reloads(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    ext = runtime.extensions
    generation = ext.generation
    old_tools = dict(runtime.tools)
    observed, published = asyncio.Event(), asyncio.Event()
    reads = 0
    failed_read = 2 if point == "inside_lock" else 1
    notices: list[None] = []

    def configuration_blocked(instance: Any) -> bool:
        nonlocal reads
        reads += 1
        observed.set()
        if reads == failed_read:
            raise OSError("PRIVATE_JOURNAL_PATH_AND_CREDENTIAL")
        return False

    original_apply = ext.apply_locked

    async def apply(target: Settings, *, persist: bool) -> None:
        await original_apply(target, persist=persist)
        published.set()

    monkeypatch.setattr(
        type(runtime.maintenance), "configuration_blocked", property(configuration_blocked)
    )
    monkeypatch.setattr(ext, "apply_locked", apply)
    monkeypatch.setattr(
        runtime.system_events, "extension_watch_failed", lambda: notices.append(None)
    )
    waits = observe_waits(monkeypatch)
    assert ext.status()["running"] is False
    ext.request_reload(refresh_catalogs=False)
    if point == "waiting":
        ext._ready_at = asyncio.get_running_loop().time() + 60
    ext.start()
    await asyncio.wait_for(observed.wait(), 2)
    worker = ext._worker
    assert worker is not None
    failure = worker.exception() if worker.done() and not worker.cancelled() else None
    ext.start()
    assert ext._worker is not None and not ext._worker.done(), type(failure).__name__
    assert ext._worker is worker
    assert await asyncio.wait_for(waits.get(), 2) == 300
    assert reads == failed_read and ext._dirty and notices == [None]
    assert ext.status()["running"] is True
    assert ext.status()["error"] == {"code": "watcher_failed", "source": "extensions"}
    assert "PRIVATE_" not in json.dumps(ext.status())
    assert ext.generation == generation
    assert all(runtime.tools[name] is tool for name, tool in old_tools.items())
    ext._ready_at = 0
    ext.wake()
    await asyncio.wait_for(published.wait(), 2)
    assert ext.generation == generation + 1 and ext.phase == "loaded" and not ext._dirty
    assert (await runtime.mcp.tools[0].handler({"q": "retained"}))["is_error"] is False


async def test_repeated_observation_failure_deduplicates_until_real_publication(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = runtime.extensions
    generation = ext.generation
    notices: list[None] = []

    def failed(instance: Any) -> bool:
        raise OSError("PRIVATE_JOURNAL")

    maintenance = type(runtime.maintenance)
    original = maintenance.configuration_blocked
    monkeypatch.setattr(maintenance, "configuration_blocked", property(failed))
    monkeypatch.setattr(
        runtime.system_events, "extension_watch_failed", lambda: notices.append(None)
    )
    waits = observe_waits(monkeypatch)
    ext.request_reload(refresh_catalogs=False)
    ext.start()
    for _ in range(3):
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert ext.status()["running"] is True and ext._dirty
        assert ext.generation == generation and notices == [None]
        ext.wake()
    monkeypatch.setattr(maintenance, "configuration_blocked", original)
    assert await asyncio.wait_for(waits.get(), 2) == 300
    assert ext.phase == "loaded" and ext.generation == generation + 1
    monkeypatch.setattr(maintenance, "configuration_blocked", property(failed))
    ext.request_reload(refresh_catalogs=False)
    assert await asyncio.wait_for(waits.get(), 2) == 300
    assert notices == [None, None]


@pytest.mark.parametrize("unexpected", [False, True], ids=["candidate", "unexpected"])
async def test_consumed_candidate_is_not_retried_or_reclassified_as_directory_failure(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, unexpected: bool
) -> None:
    ext = runtime.extensions
    generation = ext.generation
    old_tools = dict(runtime.tools)
    calls = 0
    notices: list[None] = []

    async def failed(target: Settings, *, persist: bool) -> None:
        nonlocal calls
        calls += 1
        if unexpected:
            raise OSError("PRIVATE_CANDIDATE_STATE")
        raise ext._failure(ExtensionError("candidate_failed", "mcp.d"))

    monkeypatch.setattr(ext, "apply_locked", failed)
    monkeypatch.setattr(
        runtime.system_events, "extension_watch_failed", lambda: notices.append(None)
    )
    waits = observe_waits(monkeypatch)
    ext.request_reload(refresh_catalogs=False)
    ext.start()
    for _ in range(2):
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert ext.status()["running"] is True and not ext._dirty
        assert calls == 1 and ext.generation == generation
        assert all(runtime.tools[name] is tool for name, tool in old_tools.items())
        ext.wake()
    assert notices == ([None] if unexpected else [])
    assert ext.status()["error"] == {
        "code": "watcher_failed" if unexpected else "candidate_failed",
        "source": "extensions" if unexpected else "mcp.d",
    }
    assert "PRIVATE_" not in json.dumps(ext.status())


@pytest.mark.parametrize("origin", ["observation", "directory"])
async def test_notification_failure_does_not_lose_watcher_or_repeat_unknown_delivery(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    ext = runtime.extensions
    notices = 0

    def blocked(instance: Any) -> bool:
        raise OSError("PRIVATE_JOURNAL")

    async def fingerprint(target: Settings) -> str:
        raise ExtensionError("source_changed", "mcp.d")

    def notify() -> None:
        nonlocal notices
        notices += 1
        raise OSError("PRIVATE_NOTIFICATION_CREDENTIAL")

    if origin == "observation":
        monkeypatch.setattr(type(runtime.maintenance), "configuration_blocked", property(blocked))
        ext.request_reload(refresh_catalogs=False)
    else:
        runtime.config.settings.extensions.watch_enabled = True
        monkeypatch.setattr(ext, "fingerprint", fingerprint)
    monkeypatch.setattr(runtime.system_events, "extension_watch_failed", notify)
    waits = observe_waits(monkeypatch)
    ext.start()
    for index in range(2):
        assert await asyncio.wait_for(waits.get(), 2) == 300
        assert ext.status()["running"] is True and notices == 1
        assert "PRIVATE_" not in json.dumps(ext.status())
        if index == 0:
            assert ext.status()["error"] == {"code": "watcher_failed", "source": "extensions"}
        ext.wake()


@pytest.mark.parametrize("cancelled", [False, True], ids=["failed", "cancelled"])
async def test_explicit_start_collects_done_owner_and_restarts_once(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    reads = 0

    class ObservedTask(asyncio.Task[None]):
        def exception(self) -> BaseException | None:
            nonlocal reads
            reads += 1
            return super().exception()

    async def finished() -> None:
        if cancelled:
            await asyncio.Event().wait()
        raise OSError("PRIVATE_OLD_TASK")

    old = ObservedTask(finished())
    if cancelled:
        old.cancel()
    await asyncio.wait({old})
    runtime.extensions._worker = old
    assert runtime.extensions.status()["running"] is False
    waits = observe_waits(monkeypatch)
    runtime.extensions.start()
    new = runtime.extensions._worker
    assert new is not None and new is not old
    assert reads == (0 if cancelled else 1)
    assert await asyncio.wait_for(waits.get(), 2) == 300
    runtime.extensions.start()
    assert runtime.extensions._worker is new and runtime.extensions.status()["running"] is True


async def test_close_cancels_observation_and_cannot_be_reopened_by_start_or_reload(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ext = runtime.extensions
    entered, exited = asyncio.Event(), asyncio.Event()
    notices: list[None] = []

    async def fingerprint(target: Settings) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
        raise AssertionError("unreachable")

    runtime.config.settings.extensions.watch_enabled = True
    monkeypatch.setattr(ext, "fingerprint", fingerprint)
    monkeypatch.setattr(
        runtime.system_events, "extension_watch_failed", lambda: notices.append(None)
    )
    ext.start()
    await asyncio.wait_for(entered.wait(), 2)
    owner = ext._worker
    assert owner is not None
    await asyncio.wait_for(ext.close(), 2)
    assert exited.is_set() and owner.cancelled()
    assert ext.status()["running"] is False and notices == []
    requested = ext._requested
    ext.start()
    ext.request_reload()
    ext.wake()
    assert ext._worker is None and ext._requested == requested


async def test_runtime_closing_does_not_start_worker(runtime: Runtime) -> None:
    runtime._closing = True
    try:
        runtime.extensions.start()
        assert runtime.extensions._worker is None
        assert runtime.extensions.status()["running"] is False
    finally:
        runtime._closing = False


@pytest.mark.parametrize("during_notification", [False, True], ids=["observation", "notification"])
async def test_cancelled_error_propagates_from_round_or_notification(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, during_notification: bool
) -> None:
    ext = runtime.extensions
    notices: list[None] = []

    async def fingerprint(target: Settings) -> str:
        if not during_notification:
            raise asyncio.CancelledError
        raise ExtensionError("source_changed", "mcp.d")

    def notify() -> NoReturn:
        notices.append(None)
        raise asyncio.CancelledError

    runtime.config.settings.extensions.watch_enabled = True
    monkeypatch.setattr(ext, "fingerprint", fingerprint)
    monkeypatch.setattr(runtime.system_events, "extension_watch_failed", notify)
    ext.start()
    worker = ext._worker
    assert worker is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2)
    assert worker.cancelled() and ext.status()["running"] is False
    assert notices == ([None] if during_notification else [])
