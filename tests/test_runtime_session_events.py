from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.runtime import Runtime, _ExecutionResult
from muli.task_control import TaskPaused


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("Session finalization must not call a model")


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "attention": {"enabled": False},
        }
    )
    value = Runtime(config, tmp_path / "data", client_factory=no_model)
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.parametrize("outcome", ["completed", "failed", "paused", "cancelled"])
async def test_final_session_fact_has_followup_event_after_terminal_cleanup_gap(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()

    async def execute(*args: Any, **kwargs: Any) -> _ExecutionResult:
        if outcome == "failed":
            raise ValueError("controlled failure")
        if outcome == "paused":
            raise TaskPaused("task_paused", "controlled pause", observed_epoch=3)
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return _ExecutionResult("controlled result")

    async def release(session_id: str, run_id: str) -> None:
        entered.set()
        await finish.wait()

    monkeypatch.setattr(runtime, "_execute", execute)
    monkeypatch.setattr(runtime.computer, "release_execution", release)
    session = runtime.new_session("final session event")
    sid, run_id = session["id"], "controlled-run"
    runtime.store.put("session", {**session, "status": "running"})
    runtime.run_ids[sid] = run_id
    task = asyncio.create_task(runtime._drive(sid, run_id, "main", "auto"))
    runtime.tasks[sid] = task
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        prior = runtime.store.events(session_id=sid)
        assert prior[-1]["type"] == f"run.{outcome}"
        position = prior[-1]["id"]
        assert runtime.store.get("session", sid)["status"] == "running"
        finish.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if outcome == "completed":
            assert result == "controlled result"
        else:
            assert isinstance(
                result,
                {"failed": ValueError, "paused": TaskPaused, "cancelled": asyncio.CancelledError}[
                    outcome
                ],
            )
        status = "paused" if outcome == "paused" else "idle"
        assert runtime.store.get("session", sid)["status"] == status
        assert runtime.store.get("run", run_id)["status"] == outcome
        events = runtime.store.events(after=position, session_id=sid)
        assert len(events) == 1 and events[0]["type"] == "session.updated"
        assert events[0]["data"] == {"session_id": sid, "status": status}
        assert sid not in runtime.tasks and sid not in runtime.run_ids
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_old_run_does_not_publish_or_overwrite_replacement_session(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()

    async def execute(*args: Any, **kwargs: Any) -> _ExecutionResult:
        return _ExecutionResult("old result")

    async def release(session_id: str, run_id: str) -> None:
        entered.set()
        await finish.wait()

    monkeypatch.setattr(runtime, "_execute", execute)
    monkeypatch.setattr(runtime.computer, "release_execution", release)
    session = runtime.new_session("replacement identity")
    sid = session["id"]
    runtime.store.put("session", {**session, "status": "running"})
    runtime.run_ids[sid] = "old-run"
    task = asyncio.create_task(runtime._drive(sid, "old-run", "main", "auto"))
    runtime.tasks[sid] = task
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        position = runtime.store.events(session_id=sid)[-1]["id"]
        runtime.run_ids[sid] = "replacement-run"
        runtime.tasks.pop(sid)
        finish.set()
        assert await task == "old result"
        assert runtime.store.get("session", sid)["status"] == "running"
        assert runtime.run_ids[sid] == "replacement-run"
        assert runtime.store.events(after=position, session_id=sid) == []
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        runtime.run_ids.pop(sid, None)


@pytest.mark.parametrize("outcome", ["cancel", "error"])
async def test_final_session_notification_preserves_facts_and_settles_after_handle_release(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    original_notify = runtime.bus.notify

    async def execute(*args: Any, **kwargs: Any) -> _ExecutionResult:
        return _ExecutionResult("known result")

    async def notify() -> None:
        if runtime.store.events()[-1]["type"] == "session.updated":
            entered.set()
            await finish.wait()
            if outcome == "error":
                raise RuntimeError("controlled notification failure")
        await original_notify()

    monkeypatch.setattr(runtime, "_execute", execute)
    monkeypatch.setattr(runtime.bus, "notify", notify)
    session = runtime.new_session("notification cleanup")
    sid, run_id = session["id"], "known-run"
    runtime.store.put("session", {**session, "status": "running"})
    runtime.run_ids[sid] = run_id
    task = asyncio.create_task(runtime._drive(sid, run_id, "main", "auto"))
    runtime.tasks[sid] = task
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert sid not in runtime.tasks and sid not in runtime.run_ids
        assert runtime.store.get("session", sid)["status"] == "idle"
        assert runtime.store.get("run", run_id)["status"] == "completed"
        assert runtime.store.events()[-1]["type"] == "session.updated"
        if outcome == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finish.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        stored = runtime.store.get("run", run_id)
        assert stored["status"] == "completed" and stored["result"] == "known result"
        if outcome == "cancel":
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result == "known result"
            assert stored["notification_error"] == "会话更新通知失败：RuntimeError"
        assert (
            len([event for event in runtime.store.events() if event["type"] == "session.updated"])
            == 1
        )
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("notification_error", [False, True])
async def test_close_waits_for_released_session_finalizer_before_closing_store(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, notification_error: bool
) -> None:
    entered, finish, closing_started = (asyncio.Event() for _ in range(3))
    original_notify = runtime.bus.notify
    original_compaction_close = runtime.compaction.close

    async def execute(*args: Any, **kwargs: Any) -> _ExecutionResult:
        return _ExecutionResult("known result")

    async def notify() -> None:
        if runtime.store.events()[-1]["type"] == "session.updated":
            entered.set()
            await finish.wait()
            if notification_error:
                raise RuntimeError("controlled notification failure")
        await original_notify()

    async def compaction_close() -> None:
        closing_started.set()
        await original_compaction_close()

    monkeypatch.setattr(runtime, "_execute", execute)
    monkeypatch.setattr(runtime.bus, "notify", notify)
    monkeypatch.setattr(runtime.compaction, "close", compaction_close)
    session = runtime.new_session("close finalizer")
    run_id = runtime._launch(session, "main", "auto")
    driver = runtime.tasks[session["id"]]
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert session["id"] not in runtime.tasks
        closing = asyncio.create_task(runtime.close())
        await asyncio.wait_for(closing_started.wait(), timeout=2)
        await asyncio.sleep(0)
        assert not closing.done()
        assert runtime.store.get("run", run_id)["result"] == "known result"
        finish.set()
        await closing
        assert await driver == "known result"
        assert not runtime._session_finalizers
    finally:
        finish.set()
        await asyncio.gather(driver, *([closing] if closing else []), return_exceptions=True)


@pytest.mark.parametrize("notification_error", [False, True])
async def test_old_notification_cannot_modify_a_real_replacement_run(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, notification_error: bool
) -> None:
    entered, finish, replacement_entered, replacement_finish = (asyncio.Event() for _ in range(4))
    original_notify = runtime.bus.notify
    executions = 0

    async def execute(*args: Any, **kwargs: Any) -> _ExecutionResult:
        nonlocal executions
        executions += 1
        if executions == 2:
            replacement_entered.set()
            await replacement_finish.wait()
        return _ExecutionResult(f"result {executions}")

    async def notify() -> None:
        if not entered.is_set() and runtime.store.events()[-1]["type"] == "session.updated":
            entered.set()
            await finish.wait()
            if notification_error:
                raise RuntimeError("old notification failed")
        await original_notify()

    monkeypatch.setattr(runtime, "_execute", execute)
    monkeypatch.setattr(runtime.bus, "notify", notify)
    session = runtime.new_session("real replacement")
    old_id = runtime._launch(session, "main", "auto")
    old = runtime.tasks[session["id"]]
    replacement: asyncio.Task[str] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        new_id = runtime._launch(runtime.store.get("session", session["id"]), "main", "auto")
        replacement = runtime.tasks[session["id"]]
        await asyncio.wait_for(replacement_entered.wait(), timeout=2)
        finish.set()
        assert await old == "result 1"
        assert runtime.tasks[session["id"]] is replacement
        assert runtime.run_ids[session["id"]] == new_id
        assert runtime.store.get("session", session["id"])["status"] == "running"
        assert runtime.store.get("run", old_id)["result"] == "result 1"
        assert ("notification_error" in runtime.store.get("run", old_id)) == notification_error
        replacement_finish.set()
        assert await replacement == "result 2"
    finally:
        finish.set()
        replacement_finish.set()
        await asyncio.gather(old, *([replacement] if replacement else []), return_exceptions=True)


async def test_cancelled_close_observer_still_settles_old_and_replacement_drivers(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_waiting, old_finish, new_waiting, cleanup_waiting, cleanup_finish = (
        asyncio.Event() for _ in range(5)
    )
    original_notify = runtime.bus.notify
    original_close = runtime.store.close
    executions, releases, store_closes = 0, 0, 0

    async def execute(*args: Any, **kwargs: Any) -> _ExecutionResult:
        nonlocal executions
        executions += 1
        if executions == 2:
            new_waiting.set()
            await asyncio.Future[None]()
        return _ExecutionResult("old result")

    async def notify() -> None:
        if not old_waiting.is_set() and runtime.store.events()[-1]["type"] == "session.updated":
            old_waiting.set()
            await old_finish.wait()
            raise RuntimeError("old diagnostic still needs an open Store")
        await original_notify()

    async def release(*args: Any) -> None:
        nonlocal releases
        releases += 1
        if releases == 2:
            cleanup_waiting.set()
            await cleanup_finish.wait()

    def close_store() -> None:
        nonlocal store_closes
        store_closes += 1
        original_close()

    monkeypatch.setattr(runtime, "_execute", execute)
    monkeypatch.setattr(runtime.bus, "notify", notify)
    monkeypatch.setattr(runtime.computer, "release_execution", release)
    monkeypatch.setattr(runtime.store, "close", close_store)
    session = runtime.new_session("cancel close observer")
    runtime._launch(session, "main", "auto")
    old = runtime.tasks[session["id"]]
    tasks: list[asyncio.Task[Any]] = [old]
    try:
        await asyncio.wait_for(old_waiting.wait(), timeout=2)
        new_id = runtime._launch(runtime.store.get("session", session["id"]), "main", "auto")
        replacement = runtime.tasks[session["id"]]
        tasks.append(replacement)
        await asyncio.wait_for(new_waiting.wait(), timeout=2)
        closing = asyncio.create_task(runtime.close())
        tasks.append(closing)
        await asyncio.wait_for(cleanup_waiting.wait(), timeout=2)
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
        another = asyncio.create_task(runtime.close())
        tasks.append(another)
        await asyncio.sleep(0)
        assert not closing.done() and not another.done()
        assert store_closes == 0
        cleanup_finish.set()
        await asyncio.gather(replacement, return_exceptions=True)
        assert runtime.store.get("run", new_id)["status"] == "cancelled"
        assert not closing.done() and store_closes == 0
        old_finish.set()
        results = await asyncio.gather(closing, another, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert await old == "old result"
        assert store_closes == 1
        assert not runtime._session_finalizers
        await runtime.close()
        assert store_closes == 1
    finally:
        old_finish.set()
        cleanup_finish.set()
        await asyncio.gather(*tasks, return_exceptions=True)
