"""Terminal commits and cleanup survive late or repeated cancellation observers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from test_runtime import make_runtime
from test_workflow_scope import start_blocked_workflow
from test_workflows import step

from muli.events import EventBus
from muli.server import create_app
from muli.store import Store
from muli.workflows import JsonObject, RunStep, WorkflowEngine


@pytest_asyncio.fixture
async def factory(tmp_path: Path) -> AsyncIterator[Callable[[RunStep], WorkflowEngine]]:
    store = Store(tmp_path / "workflow-terminal.sqlite3")
    engines: list[WorkflowEngine] = []

    def create(callback: RunStep) -> WorkflowEngine:
        engine = WorkflowEngine(store, EventBus(store), callback)
        engines.append(engine)
        return engine

    try:
        yield create
    finally:
        for engine in engines:
            await engine.close()
        store.close()


@pytest.mark.parametrize("cancel_after_commit", [False, True])
async def test_late_cancel_api_preserves_successful_child_and_real_parent(
    tmp_path: Path, cancel_after_commit: bool
) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    runtime = scenario.runtime
    runtime.config.update({"attention": {"enabled": False}})
    finalizing, release = asyncio.Event(), asyncio.Event()
    cancel_entered = asyncio.Event()
    real_emit, real_cancel = runtime.bus.emit, runtime.workflows.cancel
    request: asyncio.Task[httpx.Response] | None = None

    async def gate_terminal_emit(
        kind: str, data: JsonObject, session_id: str | None = None
    ) -> JsonObject:
        if (
            kind == "workflow.updated"
            and data["run"]["id"] == scenario.child_id
            and data["run"]["status"] == "completed"
            and data["node_id"] is None
        ):
            finalizing.set()
            await release.wait()
        return await real_emit(kind, data, session_id)

    async def observe_cancel(run_id: str) -> JsonObject:
        cancel_entered.set()
        return await real_cancel(run_id)

    runtime.bus.emit = gate_terminal_emit  # type: ignore[method-assign]
    runtime.workflows.cancel = observe_cancel  # type: ignore[method-assign]
    app = create_app(runtime.config, runtime=runtime)
    try:
        scenario.release_parent_final.set()
        scenario.release_worker.set()
        await asyncio.wait_for(finalizing.wait(), 2)
        committed = runtime.store.get("workflow_run", scenario.child_id)
        assert committed is not None and committed["status"] == "completed"
        assert all(node["status"] == "completed" for node in committed["nodes"].values())
        driver = runtime.workflows._tasks[scenario.child_id]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765"
        ) as client:
            assert (await client.get("/api/workflow-runs/active")).json()["items"] == []
            if cancel_after_commit:
                request = asyncio.create_task(
                    client.post(f"/api/workflow-runs/{scenario.child_id}/cancel", json={})
                )
                await asyncio.wait_for(cancel_entered.wait(), 2)
                assert not driver.cancelling()
                assert not request.done()
                assert runtime.store.get("workflow_run", scenario.child_id) == committed
            release.set()
            if request is not None:
                response = await asyncio.wait_for(request, 2)
                assert response.status_code == 200
                assert response.json()["status"] == "completed"
            assert "child evidence" in await asyncio.wait_for(scenario.parent_task, 2)
            assert runtime.store.get("workflow_run", scenario.child_id) == committed
            parent = runtime.store.get("run", scenario.parent_run_id)
            assert parent is not None and parent["status"] == "completed"
            assert scenario.child_id not in runtime.workflows.active_run_ids
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await scenario.close()


async def test_repeated_cancel_does_not_interrupt_node_cleanup(
    factory: Callable[[RunStep], WorkflowEngine],
) -> None:
    entered, cleanup, release, settled = (asyncio.Event() for _ in range(4))
    calls = 0

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
            settled.set()
        return "unreachable"

    engine = factory(callback)
    run = await engine.start(engine.create("cleanup", [step("work")])["id"])
    await asyncio.wait_for(entered.wait(), 2)
    driver = engine._tasks[run["id"]]
    observers: list[asyncio.Task[JsonObject]] = []

    async def cancel() -> JsonObject:
        nonlocal calls
        calls += 1
        return await engine.cancel(run["id"])

    try:
        observers.append(asyncio.create_task(cancel()))
        await asyncio.wait_for(cleanup.wait(), 2)
        observers.append(asyncio.create_task(cancel()))
        # The second observer runs synchronously to its first cleanup await.
        await asyncio.sleep(0)
        assert calls == 2 and driver.cancelling() == 1
        assert not settled.is_set() and not any(task.done() for task in observers)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*observers), 2)
        assert settled.is_set()
        assert all(result["status"] == "cancelled" for result in results)
        assert not engine.active_run_ids
    finally:
        release.set()
        await asyncio.gather(*observers, return_exceptions=True)


async def test_cancelled_http_observer_settles_cleanup_then_propagates_cancel(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"attention": {"enabled": False}})
    entered, cleanup, release, settled = (asyncio.Event() for _ in range(4))
    request: asyncio.Task[httpx.Response] | None = None

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
            settled.set()
        return "unreachable"

    runtime.workflows.run_step = callback
    engine = runtime.workflows
    app = create_app(runtime.config, runtime=runtime)
    try:
        run = await engine.start(engine.create("HTTP cleanup", [step("work")])["id"])
        await asyncio.wait_for(entered.wait(), 2)
        driver = engine._tasks[run["id"]]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765"
        ) as client:
            request = asyncio.create_task(
                client.post(f"/api/workflow-runs/{run['id']}/cancel", json={})
            )
            await asyncio.wait_for(cleanup.wait(), 2)
            request.cancel()
            request.cancel()
            assert driver.cancelling() == 1
            await asyncio.sleep(0)
            assert not request.done() and not settled.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 2)
            assert settled.is_set()
            result = await engine.wait(run["id"])
            assert result["status"] == "cancelled" and not engine.active_run_ids
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await runtime.close()


async def test_prestart_fallback_is_shared_and_close_waits_for_its_publish(
    factory: Callable[[RunStep], WorkflowEngine],
) -> None:
    publishing, release = asyncio.Event(), asyncio.Event()
    publish_calls, second_calls = 0, 0

    async def unreachable(session_id: str, prompt: str, role: str, mode: str) -> str:
        pytest.fail("driver was cancelled before its first schedule")

    engine = factory(unreachable)
    real_emit = engine.bus.emit

    async def gate_cancel_publish(
        kind: str, data: JsonObject, session_id: str | None = None
    ) -> JsonObject:
        nonlocal publish_calls
        if data.get("node_id") == "work" and data["run"]["nodes"]["work"]["status"] == "cancelled":
            publish_calls += 1
            publishing.set()
            await release.wait()
        return await real_emit(kind, data, session_id)

    engine.bus.emit = gate_cancel_publish  # type: ignore[method-assign]
    run = await engine.start(engine.create("prestart", [step("work")])["id"])
    engine._tasks[run["id"]].cancel()
    first = asyncio.create_task(engine.cancel(run["id"]))
    second: asyncio.Task[JsonObject] | None = None
    closer: asyncio.Task[None] | None = None

    async def cancel_again() -> JsonObject:
        nonlocal second_calls
        second_calls += 1
        return await engine.cancel(run["id"])

    try:
        await asyncio.wait_for(publishing.wait(), 2)
        assert not engine.active_run_ids
        second = asyncio.create_task(cancel_again())
        await asyncio.sleep(0)
        assert second_calls == 1 and publish_calls == 1 and not second.done()
        closer = asyncio.create_task(engine.close())
        await asyncio.sleep(0)
        assert engine._closed and not closer.done()
        closer.cancel()
        await asyncio.sleep(0)
        assert not closer.done()
        release.set()
        assert (await asyncio.wait_for(first, 2))["status"] == "cancelled"
        assert (await asyncio.wait_for(second, 2))["status"] == "cancelled"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, 2)
        assert publish_calls == 1
    finally:
        release.set()
        await asyncio.gather(
            *[task for task in (first, second, closer) if task is not None],
            return_exceptions=True,
        )


async def test_cancelled_close_observer_preserves_all_terminal_finalizers(
    factory: Callable[[RunStep], WorkflowEngine],
) -> None:
    finalizing, release = asyncio.Event(), asyncio.Event()
    publishing: set[str] = set()

    async def complete(session_id: str, prompt: str, role: str, mode: str) -> str:
        return "accepted result"

    engine = factory(complete)
    real_emit = engine.bus.emit

    async def gate_terminal_emit(
        kind: str, data: dict[str, Any], session_id: str | None = None
    ) -> JsonObject:
        if data["run"]["status"] == "completed" and data["node_id"] is None:
            publishing.add(data["run"]["id"])
            if len(publishing) == 2:
                finalizing.set()
            await release.wait()
        return await real_emit(kind, data, session_id)

    engine.bus.emit = gate_terminal_emit  # type: ignore[method-assign]
    workflow = engine.create("terminal finalizers", [step("work")])
    runs = [await engine.start(workflow["id"]) for _ in range(2)]
    closer: asyncio.Task[None] | None = None
    waiter: asyncio.Task[JsonObject] | None = None
    try:
        await asyncio.wait_for(finalizing.wait(), 2)
        drivers = list(engine._tasks.values())
        waiter = asyncio.create_task(engine.wait(runs[0]["id"]))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        closer = asyncio.create_task(engine.close())
        await asyncio.sleep(0)
        assert engine._closed and not closer.done()
        closer.cancel()
        closer.cancel()
        assert all(not driver.cancelling() for driver in drivers)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, 2)
        for run in runs:
            result = engine.store.get("workflow_run", run["id"])
            assert result is not None and result["status"] == "completed"
            assert result["result"] == "accepted result"
        assert all(driver.done() for driver in drivers) and not engine.active_run_ids
    finally:
        release.set()
        await asyncio.gather(
            *[task for task in (closer, waiter) if task is not None], return_exceptions=True
        )
