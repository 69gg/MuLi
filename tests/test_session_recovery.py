from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from muli.config import ConfigFile
from muli.providers import ModelConfig, ToolCall
from muli.runtime import Runtime, _ExecutionResult
from muli.server import create_app
from muli.store import Store
from muli.task_control import TaskPaused


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("会话恢复不能调用模型")


@pytest.fixture
async def recovery_case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data", client_factory=no_model)
    app = create_app(config, runtime=runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            yield runtime, client
    finally:
        await runtime.close()


async def test_restart_publishes_recovery_for_an_existing_subscriber_without_execution(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    data = tmp_path / "data"
    store = Store(data / "muli.sqlite3")
    session = store.main_session("ask")
    sid = str(session["id"])
    store.put("session", {**session, "status": "running"})
    store.put("run", {"id": "crashed-run", "session_id": sid, "status": "running"})
    approval = {
        "id": "old-approval",
        "session_id": sid,
        "run_id": "crashed-run",
        "status": "pending",
    }
    store.put("approval", approval)
    store.event("run.started", {"id": "crashed-run"}, sid)
    store.event("tool.started", {"call_id": "old-tool", "name": "unused"}, sid)
    store.event("approval.requested", approval, sid)
    before = store.event_position()
    store.close()

    runtime = Runtime(config, data, client_factory=no_model)
    app = create_app(config, runtime=runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            detail = (await client.get("/api/conversation")).json()
            assert detail["session"]["id"] == sid
            assert detail["session"]["status"] == "idle"
            assert detail["live"]["status"] == "interrupted"
            assert detail["live"]["run"] == {"id": "crashed-run", "status": "interrupted"}
            assert detail["live"]["run_id"] is None
            assert detail["live"]["pending_approvals"] == []
            assert all(isinstance(event["id"], str) for event in detail["events"])
            assert detail["after"] == str(runtime.store.event_position())
            assert (await client.get("/api/approvals")).json() == {"items": []}
            assert (
                await client.post("/api/approvals/old-approval", json={"decision": "approve"})
            ).status_code == 400

        recovered = runtime.store.events(after=before, session_id=sid)
        assert len(recovered) == 3
        assert {event["data"]["kind"] for event in recovered} == {"session", "run", "approval"}
        assert all(event["type"] == "session.recovered" for event in recovered)
        # An old page resumes its original cursor, without requesting a new snapshot.
        async with aclosing(runtime.bus.stream(before, sid)) as stream:
            wire = await anext(stream)
        assert json.loads(wire.split("data: ", 1)[1])["type"] == "session.recovered"
        assert runtime.store.get("approval", "old-approval")["status"] == "interrupted"
        assert not runtime.tasks and not runtime.permissions.pending
        assert runtime.store.messages(sid) == []
        runtime._recover()
        assert runtime.store.events(after=before, session_id=sid) == recovered
    finally:
        await runtime.close()


async def test_recovery_event_and_object_change_roll_back_together(
    recovery_case: tuple[Runtime, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = recovery_case
    session = runtime.main_session()
    run = {"id": "atomic-run", "session_id": session["id"], "status": "running"}
    runtime.store.put("run", run)
    before = runtime.store.event_position()
    original = runtime.store._event

    def fail_after_insert(
        event_type: str,
        data: dict[str, Any],
        session_id: str | None = None,
        *,
        objects: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        original(event_type, data, session_id, objects=objects)
        raise RuntimeError("controlled rollback")

    monkeypatch.setattr(runtime.store, "_event", fail_after_insert)
    with pytest.raises(RuntimeError, match="controlled rollback"):
        runtime._recover()
    assert runtime.store.get("run", "atomic-run") == run
    assert runtime.store.event_position() == before
    monkeypatch.setattr(runtime.store, "_event", original)
    runtime._recover()
    assert runtime.store.get("run", "atomic-run")["status"] == "interrupted"
    assert runtime.store.events(after=before)[0]["type"] == "session.recovered"


async def test_live_approval_survives_recent_history_and_requires_a_pending_future(
    recovery_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = recovery_case
    session = runtime.main_session()
    sid = str(session["id"])
    runtime.store.put("session", {**session, "status": "running"})
    runtime.store.put("run", {"id": "live-run", "session_id": sid, "status": "running"})
    approval = {"id": "old-live", "session_id": sid, "run_id": "live-run", "status": "pending"}
    runtime.store.put("approval", approval)
    for index in range(201):
        runtime.store.put("approval", {**approval, "id": f"new-{index}", "status": "approved"})
    loop = asyncio.get_running_loop()
    runtime.permissions.pending["old-live"] = loop.create_future()
    for identifier, status in (
        ("done", "pending"),
        ("cancelled", "pending"),
        ("resolved", "approved"),
    ):
        runtime.store.put("approval", {**approval, "id": identifier, "status": status})
        future: asyncio.Future[bool] = loop.create_future()
        if identifier == "done":
            future.set_result(True)
        elif identifier == "cancelled":
            future.cancel()
        runtime.permissions.pending[identifier] = future
    runtime.store.put("approval", {**approval, "id": "no-future"})
    finish = asyncio.Event()

    async def hold() -> str:
        await finish.wait()
        return "controlled"

    task = asyncio.create_task(hold())
    runtime.tasks[sid] = task
    runtime.run_ids[sid] = "live-run"
    try:
        detail = (await client.get("/api/conversation")).json()
        assert detail["live"]["run_id"] == "live-run"
        assert detail["live"]["status"] == "approval"
        assert detail["live"]["pending_approvals"] == [approval]
        assert (await client.get("/api/approvals")).json() == {"items": [approval]}
        runtime.permissions.pending["old-live"].cancel()
        assert (await client.get("/api/approvals")).json() == {"items": []}
        assert (
            await client.post("/api/approvals/old-live", json={"decision": "approve"})
        ).status_code == 400
    finally:
        finish.set()
        await task
        runtime.tasks.pop(sid)
        runtime.run_ids.pop(sid)
        for future in runtime.permissions.pending.values():
            future.cancel()
        runtime.permissions.pending.clear()


async def test_snapshot_position_is_exact_and_later_session_events_are_not_lost(
    recovery_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = recovery_case
    sid = str(runtime.main_session()["id"])
    original = runtime.store.event("main-history", {}, sid)
    exact = (1 << 53) + 1
    runtime.store.db.execute(
        "INSERT INTO events(id,type,session_id,data,created_at) VALUES(?,?,?,?,1)",
        (exact, "position-only", "another-session", "{}"),
    )
    runtime.store.db.commit()
    detail = (await client.get("/api/conversation")).json()
    assert detail["after"] == str(exact)
    assert detail["events"][0]["id"] == str(original["id"])
    event = await runtime.bus.emit("run.started", {"id": "new-run"}, sid)
    async with aclosing(runtime.bus.stream(int(detail["after"]), sid)) as stream:
        wire = await anext(stream)
    assert f"id: {event['id']}\n" in wire
    assert json.loads(wire.split("data: ", 1)[1])["data"]["id"] == "new-run"


@pytest.mark.parametrize("outcome", ["completed", "failed", "paused", "cancelled"])
async def test_snapshot_retains_same_run_terminal_evidence_before_cleanup_finishes(
    recovery_case: tuple[Runtime, httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    runtime, client = recovery_case
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
    session = runtime.main_session()
    sid = str(session["id"])
    runtime.store.put("session", {**session, "status": "running"})
    runtime.run_ids[sid] = "finishing-run"
    task = asyncio.create_task(runtime._drive(sid, "finishing-run", "main", "auto"))
    runtime.tasks[sid] = task
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        detail = (await client.get("/api/conversation")).json()
        assert detail["session"]["status"] == "running"
        terminal = detail["events"][-1]
        assert terminal["type"] == f"run.{outcome}"
        assert terminal["data"]["run_id"] == "finishing-run"
        assert detail["live"]["run"]["id"] == "finishing-run"
        assert detail["live"]["run"]["status"] == ("paused" if outcome == "paused" else "running")
        assert detail["after"] == terminal["id"]
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        subsequent = runtime.store.events(after=int(detail["after"]), session_id=sid)
        assert subsequent[-1]["type"] == "session.updated"
        assert runtime.store.get("run", "finishing-run")["status"] == outcome
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ({"status": "unknown", "error": "结果未知"}, "unknown"),
        ({"outcome": "unknown", "error": "未确认"}, "unknown"),
        ({"status": "not_executed", "code": "task_paused"}, "not_executed"),
        ({"error": "confirmed failure"}, "failed"),
        ({"status": "arbitrary-handler-state"}, "completed"),
        ({"outcome": "unknown", "error": "large result" * 1000}, "unknown"),
    ],
)
async def test_tool_terminal_metadata_survives_result_truncation(
    recovery_case: tuple[Runtime, httpx.AsyncClient], result: dict[str, Any], status: str
) -> None:
    runtime, _ = recovery_case
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    sid = str(runtime.main_session()["id"])
    await runtime._tool_completed(ToolCall("tool-call", "unused", "{}"), result, sid)
    event = runtime.store.events(session_id=sid)[-1]
    assert event["type"] == "tool.completed"
    assert event["data"]["status"] == status
    assert len(event["data"]["result"]) <= 1000
    if len(json.dumps(result, ensure_ascii=False)) > 1000:
        with pytest.raises(json.JSONDecodeError):
            json.loads(event["data"]["result"])
