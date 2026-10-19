from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.providers import Message, ModelTurn, StreamEvent
from muli.store import Store


async def test_runtime_close_settles_committed_team_finalizer_before_database_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="完整结果")])
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_notify = runtime.bus.notify

    async def gated_notify() -> None:
        if runtime.store.events(limit=100)[-1]["type"] == "team.finished":
            entered.set()
            try:
                await release.wait()
            finally:
                exited.set()
        await original_notify()

    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    parent = runtime.new_session(mode="yolo")
    child = await runtime.team.spawn("关闭边界", "工作", parent_id=parent["id"])
    task = runtime.team._active[child["id"]]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not runtime.team._active and not runtime.tasks
        await runtime.close()
        assert task.done(), "团队收尾协程不能在 SQLite 关闭后继续运行"
        assert task.cancelled() and exited.is_set()
        assert not runtime.team._tasks
        reopened = Store(tmp_path / "data" / "muli.sqlite3")
        try:
            stored = reopened.get("agent", child["id"])
            assert stored is not None and stored["status"] == "completed"
            assert stored["completion_revision"] == 1 and stored["result"] == "完整结果"
            assert len(reopened.team_completions(child["id"], 0)) == 1
            assert reopened.list("team_report")[0]["status"] == "pending"
        finally:
            reopened.close()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_concurrent_close_and_repeated_outer_cancel_do_not_recancel_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="已完成")])
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_entered = asyncio.Event()
    original_notify = runtime.bus.notify

    async def gated_notify() -> None:
        if runtime.store.events(limit=100)[-1]["type"] == "team.finished":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        await original_notify()

    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    parent = runtime.new_session(mode="yolo")
    child = await runtime.team.spawn("多次关闭", "工作", parent_id=parent["id"])
    task = runtime.team._active[child["id"]]
    closers: list[asyncio.Task[None]] = []

    async def second_close() -> None:
        second_entered.set()
        await runtime.team.close()

    try:
        await asyncio.wait_for(entered.wait(), 2)
        first = asyncio.create_task(runtime.team.close())
        closers.append(first)
        await asyncio.wait_for(cleaning.wait(), 2)
        second = asyncio.create_task(second_close())
        closers.append(second)
        await asyncio.wait_for(second_entered.wait(), 2)
        assert task.cancelling() == 1
        first.cancel()
        first.cancel()
        assert task.cancelling() == 1
        assert not task.done() and not second.done()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*closers, return_exceptions=True), 2)
        assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
        assert task.done() and task.cancelled() and task.cancelling() == 1
        assert not runtime.team._tasks and not runtime.team._active
        await runtime.team.close()
    finally:
        release.set()
        await asyncio.gather(*closers, return_exceptions=True)
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_repeated_stop_does_not_interrupt_inflight_cancellation_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    started, cancelling, release, second_entered = (asyncio.Event() for _ in range(4))
    original_emit = runtime.bus.emit

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent("done", turn=ModelTurn(content="不会到达"))

    async def gated_emit(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        if event_type == "run.cancelled":
            cancelling.set()
            await release.wait()
        return await original_emit(event_type, data, session_id)

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "emit", gated_emit)
    parent = runtime.new_session(mode="yolo")
    child = await runtime.team.spawn("反复停止", "工作", parent_id=parent["id"])
    task = runtime.team._active[child["id"]]
    stoppers: list[asyncio.Task[dict[str, Any]]] = []

    async def second_stop() -> dict[str, Any]:
        second_entered.set()
        return await runtime.team.stop(child["id"])

    try:
        await asyncio.wait_for(started.wait(), 2)
        stoppers.append(asyncio.create_task(runtime.team.stop(child["id"])))
        await asyncio.wait_for(cancelling.wait(), 2)
        stoppers.append(asyncio.create_task(second_stop()))
        await asyncio.wait_for(second_entered.wait(), 2)
        assert task.cancelling() == 1
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*stoppers), 2)
        assert all(item["status"] == "cancelled" for item in results)
        assert runtime.team.get(child["id"])["completion_revision"] == 1
        assert not runtime.tasks and not runtime.team._tasks
    finally:
        release.set()
        await asyncio.gather(*stoppers, return_exceptions=True)
        await runtime.close()


async def test_cancelling_stopper_twice_settles_child_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    started, cancelling, release = (asyncio.Event() for _ in range(3))
    original_emit = runtime.bus.emit

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent("done", turn=ModelTurn(content="不会到达"))

    async def gated_emit(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        if event_type == "run.cancelled":
            cancelling.set()
            await release.wait()
        return await original_emit(event_type, data, session_id)

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "emit", gated_emit)
    parent = runtime.new_session(mode="yolo")
    child = await runtime.team.spawn("停止入口取消", "工作", parent_id=parent["id"])
    task = runtime.team._active[child["id"]]
    stopper: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        stopper = asyncio.create_task(runtime.team.stop(child["id"]))
        await asyncio.wait_for(cancelling.wait(), 2)
        assert task.cancelling() == 1
        stopper.cancel()
        stopper.cancel()
        assert task.cancelling() == 1, "调用者取消不能转发到已在清理的子任务"
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopper, 2)
        assert task.done() and task.cancelled() and task.cancelling() == 1
        stored = runtime.team.get(child["id"])
        assert stored["status"] == "cancelled" and stored["completion_revision"] == 1
        assert not runtime.tasks and not runtime.team._tasks and not runtime.team._active
        assert (await runtime.team.stop(child["id"]))["completion_revision"] == 1
    finally:
        release.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()


async def test_new_generation_starts_while_previous_finalizer_is_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    entered, release_finalizer, second_started, release_second = (asyncio.Event() for _ in range(4))
    original_notify = runtime.bus.notify
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            calls += 1
            index = calls
            if index == 2:
                second_started.set()
                await release_second.wait()
            yield StreamEvent("done", turn=ModelTurn(content=f"第{index}代"))

    async def gated_notify() -> None:
        events = runtime.store.events(limit=100)
        if events[-1]["type"] == "team.finished" and events[-1]["data"]["generation"] == 1:
            entered.set()
            await release_finalizer.wait()
        await original_notify()

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    parent = runtime.new_session(mode="yolo")
    child = await runtime.team.spawn("连续代", "工作", parent_id=parent["id"])
    first = runtime.team._active[child["id"]]
    tasks = [first]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert first in runtime.team._tasks and not runtime.team._active
        await runtime.team.send(child["id"], "继续下一代")
        await asyncio.wait_for(second_started.wait(), 2)
        second = runtime.team._active[child["id"]]
        tasks.append(second)
        assert second is not first and {first, second} <= runtime.team._tasks
        release_finalizer.set()
        await asyncio.wait_for(first, 2)
        assert runtime.team._active[child["id"]] is second
        assert first not in runtime.team._tasks and second in runtime.team._tasks
        release_second.set()
        await asyncio.wait_for(second, 2)
        assert [
            item["data"]["generation"]
            for item in runtime.store.team_completions(child["id"], 0, limit=10)
        ] == [1, 2]
        assert not runtime.team._tasks and not runtime.team._active
    finally:
        release_finalizer.set()
        release_second.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()


async def test_unexpected_task_failure_is_observed_and_removed_from_lifecycle_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    done = asyncio.Event()
    errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()

    async def fail(identifier: str) -> None:
        raise RuntimeError(f"故意测试未预期失败：{identifier}")

    monkeypatch.setattr(runtime.team, "_run", fail)
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        parent = runtime.new_session(mode="yolo")
        child = await runtime.team.spawn("失败观察", "工作", parent_id=parent["id"])
        runtime.team._active[child["id"]].add_done_callback(lambda _: done.set())
        await asyncio.wait_for(done.wait(), 2)
        assert not runtime.team._tasks
        await runtime.team.close()
        gc.collect()
        assert errors == []
    finally:
        loop.set_exception_handler(original_handler)
        await runtime.close()


async def test_stop_keeps_unexpected_child_cleanup_error_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    entered = asyncio.Event()

    async def fail(identifier: str) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError(f"清理失败：{identifier}")

    monkeypatch.setattr(runtime.team, "_run", fail)
    try:
        parent = runtime.new_session(mode="yolo")
        child = await runtime.team.spawn("异常可见", "工作", parent_id=parent["id"])
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(RuntimeError, match="清理失败"):
            await asyncio.wait_for(runtime.team.stop(child["id"]), 2)
        assert not runtime.team._tasks
    finally:
        await runtime.close()
