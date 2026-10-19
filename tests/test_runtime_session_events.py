from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.runtime import Runtime
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

    async def execute(*args: Any, **kwargs: Any) -> str:
        if outcome == "failed":
            raise ValueError("controlled failure")
        if outcome == "paused":
            raise TaskPaused("task_paused", "controlled pause", observed_epoch=3)
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return "controlled result"

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

    async def execute(*args: Any, **kwargs: Any) -> str:
        return "old result"

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

    async def execute(*args: Any, **kwargs: Any) -> str:
        return "known result"

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
