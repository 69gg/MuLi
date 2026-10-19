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
        assert runtime.team._stopping_tasks[task] == 2
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*stoppers), 2)
        assert all(item["status"] == "cancelled" for item in results)
        assert runtime.team.get(child["id"])["completion_revision"] == 1
        assert not runtime.tasks and not runtime.team._tasks
        assert not runtime.team._stopping_tasks
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


@pytest.mark.parametrize("later_input", [False, True])
async def test_old_stop_preserves_resumed_generation_and_its_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later_input: bool
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    started, finished, release_first, second_started, release_second = (
        asyncio.Event() for _ in range(5)
    )
    original_notify = runtime.bus.notify
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await asyncio.Event().wait()
            else:
                second_started.set()
                await release_second.wait()
            yield StreamEvent("done", turn=ModelTurn(content="新代结果"))

    async def gated_notify() -> None:
        event = runtime.store.events(limit=100)[-1]
        if event["type"] == "team.finished" and event["data"]["generation"] == 1:
            finished.set()
            await release_first.wait()
        await original_notify()

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    parent = runtime.new_session(mode="yolo")
    child = await runtime.team.spawn("跨代停止", "第一代", parent_id=parent["id"])
    stopper: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        stopper = asyncio.create_task(runtime.team.stop(child["id"]))
        await asyncio.wait_for(finished.wait(), 2)
        await runtime.team.resume(child["id"])
        await asyncio.wait_for(second_started.wait(), 2)
        second = runtime.team._active[child["id"]]
        if later_input:
            await runtime.team.send(child["id"], "新代专用补充", wake=False)
        release_first.set()
        await asyncio.wait_for(stopper, 2)
        current = runtime.team.get(child["id"])
        assert not second.done() and second.cancelling() == 0
        assert current["status"] == "running" and current["generation"] == 2
        assert current["completion_revision"] == 1
        assert runtime.store.pending_inputs(child["session_id"]) == int(later_input)
        assert not runtime.team._stopping_tasks
    finally:
        release_first.set()
        release_second.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()


async def test_stop_only_cancels_inputs_pending_when_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    original_emit = runtime.bus.emit

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent("done", turn=ModelTurn(content="不会执行"))

    async def gated_emit(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        if event_type == "run.cancelled":
            cleaning.set()
            await release.wait()
        return await original_emit(event_type, data, session_id)

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "emit", gated_emit)
    parent = runtime.new_session(mode="yolo")
    child = await runtime.team.spawn("收件归属", "工作", parent_id=parent["id"])
    stopper: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        await runtime.team.send(child["id"], "停止前的旧输入", wake=False)
        stopper = asyncio.create_task(runtime.team.stop(child["id"]))
        await asyncio.wait_for(cleaning.wait(), 2)
        assert runtime.store.pending_inputs(child["session_id"]) == 0
        await runtime.team.send(child["id"], "停止之后的新意图", wake=False)
        release.set()
        await asyncio.wait_for(stopper, 2)
        assert runtime.store.pending_inputs(child["session_id"]) == 1
        assert runtime.team.get(child["id"])["completion_revision"] == 1
        assert not runtime.team._stopping_tasks
    finally:
        release.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()


async def test_stop_captures_parent_and_all_siblings_before_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update({"limits": {"max_parallel_agents": 4}})
    await runtime.reconfigure()
    started = [asyncio.Event() for _ in range(5)]
    finished = [asyncio.Event() for _ in range(3)]
    release_first, release_next = asyncio.Event(), asyncio.Event()
    original_notify = runtime.bus.notify
    identifiers: list[str] = []
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            if index < 3:
                await asyncio.Event().wait()
            else:
                await release_next.wait()
            yield StreamEvent("done", turn=ModelTurn(content="下一代"))

    async def gated_notify() -> None:
        event = runtime.store.events(limit=100)[-1]
        if (
            event["type"] == "team.finished"
            and event["data"]["generation"] == 1
            and event["data"]["agent_id"] in identifiers
        ):
            index = identifiers.index(event["data"]["agent_id"])
            finished[index].set()
            if index == 1:
                await release_first.wait()
        await original_notify()

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    legacy = runtime.new_session(mode="yolo")
    parent = await runtime.team.spawn("固定父代", "工作", parent_id=legacy["id"])
    identifiers.append(parent["id"])
    stopper: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(started[0].wait(), 2)
        first = await runtime.team.spawn("先清理的子任务", "工作", parent_id=parent["id"])
        identifiers.append(first["id"])
        await asyncio.wait_for(started[1].wait(), 2)
        second = await runtime.team.spawn("后清理的兄弟", "工作", parent_id=parent["id"])
        identifiers.append(second["id"])
        await asyncio.wait_for(started[2].wait(), 2)
        stopper = asyncio.create_task(runtime.team.stop(parent["id"]))
        for event in finished:
            await asyncio.wait_for(event.wait(), 2)
        await runtime.team.resume(parent["id"])
        await asyncio.wait_for(started[3].wait(), 2)
        await runtime.team.resume(second["id"])
        await asyncio.wait_for(started[4].wait(), 2)
        successors = [runtime.team._active[item] for item in (parent["id"], second["id"])]
        for identifier in (parent["id"], second["id"]):
            await runtime.team.send(identifier, "后来明确恢复的新收件", wake=False)
        release_first.set()
        await asyncio.wait_for(stopper, 2)
        for identifier, task in zip((parent["id"], second["id"]), successors, strict=True):
            current = runtime.team.get(identifier)
            assert not task.done() and task.cancelling() == 0
            assert current["status"] == "running" and current["generation"] == 2
            assert current["completion_revision"] == 1
            assert runtime.store.pending_inputs(current["session_id"]) == 1
        assert not runtime.team._stopping_tasks
    finally:
        release_first.set()
        release_next.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()


async def test_parent_runtime_cleanup_cannot_rediscover_resumed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    parent_started, child_started, parent_cleaning, child_finished = (
        asyncio.Event() for _ in range(4)
    )
    release_parent, next_started, release_next = (asyncio.Event() for _ in range(3))
    original_notify = runtime.bus.notify
    child_id = ""
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            calls += 1
            if calls == 1:
                parent_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    parent_cleaning.set()
                    await release_parent.wait()
            elif calls == 2:
                child_started.set()
                await asyncio.Event().wait()
            else:
                next_started.set()
                await release_next.wait()
            yield StreamEvent("done", turn=ModelTurn(content="新子代"))

    async def observing_notify() -> None:
        event = runtime.store.events(limit=100)[-1]
        if event["type"] == "team.finished" and event["data"]["agent_id"] == child_id:
            child_finished.set()
        await original_notify()

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "notify", observing_notify)
    legacy = runtime.new_session(mode="yolo")
    parent = await runtime.team.spawn("父清理延迟", "工作", parent_id=legacy["id"])
    stopper: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(parent_started.wait(), 2)
        child = await runtime.team.spawn("父清理期间恢复", "工作", parent_id=parent["id"])
        child_id = child["id"]
        await asyncio.wait_for(child_started.wait(), 2)
        stopper = asyncio.create_task(runtime.team.stop(parent["id"]))
        await asyncio.wait_for(parent_cleaning.wait(), 2)
        await asyncio.wait_for(child_finished.wait(), 2)
        await runtime.team.resume(child_id)
        await asyncio.wait_for(next_started.wait(), 2)
        successor = runtime.team._active[child_id]
        await runtime.team.send(child_id, "只交新子代的补充", wake=False)
        release_parent.set()
        await asyncio.wait_for(stopper, 2)
        current = runtime.team.get(child_id)
        assert not successor.done() and successor.cancelling() == 0
        assert current["status"] == "running" and current["generation"] == 2
        assert current["completion_revision"] == 1
        assert runtime.store.pending_inputs(current["session_id"]) == 1
        assert not runtime.team._stopping_tasks
    finally:
        release_parent.set()
        release_next.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_main_runtime_stop_keeps_one_batch_across_driver_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_waiter: bool
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    main_started, child_started, child_finished, release_child = (asyncio.Event() for _ in range(4))
    next_started, release_next = asyncio.Event(), asyncio.Event()
    original_notify = runtime.bus.notify
    child_id = ""
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            calls += 1
            if calls == 1:
                main_started.set()
                await asyncio.Event().wait()
            elif calls == 2:
                child_started.set()
                await asyncio.Event().wait()
            else:
                next_started.set()
                await release_next.wait()
            yield StreamEvent("done", turn=ModelTurn(content="新子代"))

    async def gated_notify() -> None:
        event = runtime.store.events(limit=100)[-1]
        if (
            event["type"] == "team.finished"
            and event["data"]["agent_id"] == child_id
            and event["data"]["generation"] == 1
        ):
            child_finished.set()
            await release_child.wait()
        await original_notify()

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    main = runtime.main_session()
    stopper: asyncio.Task[None] | None = None
    try:
        await runtime.start(main["id"], "主任务", mode="yolo")
        await asyncio.wait_for(main_started.wait(), 2)
        child = await runtime.team.spawn("主停止中的子新代", "工作", parent_id=main["id"])
        child_id = child["id"]
        await asyncio.wait_for(child_started.wait(), 2)
        original = runtime.team._active[child_id]
        stopper = asyncio.create_task(runtime.stop(main["id"]))
        await asyncio.wait_for(child_finished.wait(), 2)
        await runtime.team.resume(child_id)
        await asyncio.wait_for(next_started.wait(), 2)
        successor = runtime.team._active[child_id]
        await runtime.team.send(child_id, "新代专用资料", wake=False)
        if cancel_waiter:
            stopper.cancel()
            stopper.cancel()
            assert original.cancelling() == 1 and not original.done()
        release_child.set()
        result = await asyncio.wait_for(asyncio.gather(stopper, return_exceptions=True), 2)
        if cancel_waiter:
            assert isinstance(result[0], asyncio.CancelledError)
        else:
            assert result[0] is None
        current = runtime.team.get(child_id)
        assert not successor.done() and successor.cancelling() == 0
        assert current["status"] == "running" and current["generation"] == 2
        assert current["completion_revision"] == 1
        assert runtime.store.pending_inputs(current["session_id"]) == 1
        assert not runtime.team._stopping_tasks
    finally:
        release_child.set()
        release_next.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()


async def test_nested_failure_and_parent_stop_keep_independent_batch_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update({"limits": {"max_parallel_agents": 4}})
    await runtime.reconfigure()
    started = [asyncio.Event() for _ in range(3)]
    fail_child, grandchild_cleaning, release_grandchild = (asyncio.Event() for _ in range(3))
    parent_cancelled, child_finished, release_child = (asyncio.Event() for _ in range(3))
    original_notify = runtime.bus.notify
    child_id = ""
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            if index == 0:
                try:
                    await asyncio.Event().wait()
                finally:
                    parent_cancelled.set()
            elif index == 1:
                await fail_child.wait()
                raise ValueError("自然失败触发后代清理")
            else:
                try:
                    await asyncio.Event().wait()
                finally:
                    grandchild_cleaning.set()
                    await release_grandchild.wait()
            yield StreamEvent("done", turn=ModelTurn(content="不会到达"))

    async def gated_notify() -> None:
        event = runtime.store.events(limit=100)[-1]
        if event["type"] == "team.finished" and event["data"]["agent_id"] == child_id:
            child_finished.set()
            await release_child.wait()
        await original_notify()

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    legacy = runtime.new_session(mode="yolo")
    parent = await runtime.team.spawn("重叠批次父", "工作", parent_id=legacy["id"])
    stopper: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(started[0].wait(), 2)
        child = await runtime.team.spawn("先失败的子", "工作", parent_id=parent["id"])
        child_id = child["id"]
        await asyncio.wait_for(started[1].wait(), 2)
        grandchild = await runtime.team.spawn("等待清理的孙", "工作", parent_id=child_id)
        await asyncio.wait_for(started[2].wait(), 2)
        original = runtime.team._active[grandchild["id"]]
        fail_child.set()
        await asyncio.wait_for(grandchild_cleaning.wait(), 2)
        assert runtime.team._stopping_tasks[original] == 1
        stopper = asyncio.create_task(runtime.team.stop(parent["id"]))
        await asyncio.wait_for(parent_cancelled.wait(), 2)
        assert runtime.team._stopping_tasks[original] == 2
        assert original.cancelling() == 1
        release_grandchild.set()
        await asyncio.wait_for(child_finished.wait(), 2)
        assert runtime.team._stopping_tasks[original] == 1
        release_child.set()
        await asyncio.wait_for(stopper, 2)
        assert not runtime.team._stopping_tasks and not runtime.team._tasks
        for agent in (parent, child, grandchild):
            assert runtime.team.get(agent["id"])["completion_revision"] == 1
    finally:
        fail_child.set()
        release_grandchild.set()
        release_child.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()


async def test_overlapping_main_stops_keep_the_remaining_scope_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    started = [asyncio.Event() for _ in range(2)]
    entered, release, main_finished = (asyncio.Event() for _ in range(3))
    second_cleaning, release_second, second_entered = (asyncio.Event() for _ in range(3))
    original_notify, original_emit = runtime.bus.notify, runtime.bus.emit
    child_id = ""
    main = runtime.main_session()
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            index = calls
            calls += 1
            seen.append(list(messages))
            if index < 2:
                started[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    if index == 1:
                        second_cleaning.set()
                        await release_second.wait()
            yield StreamEvent("done", turn=ModelTurn(content="处理后来输入"))

    async def gated_notify() -> None:
        event = runtime.store.events(limit=100)[-1]
        if event["type"] == "team.finished" and event["data"]["agent_id"] == child_id:
            entered.set()
            await release.wait()
        await original_notify()

    async def observing_emit(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        event = await original_emit(event_type, data, session_id)
        if event_type == "run.completed" and session_id == main["id"]:
            main_finished.set()
        return event

    monkeypatch.setattr(runtime.bus, "notify", gated_notify)
    monkeypatch.setattr(runtime.bus, "emit", observing_emit)
    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    child = await runtime.team.spawn("先提交终态", "工作", parent_id=main["id"])
    child_id = child["id"]
    original = runtime.team._active[child_id]
    stoppers: list[asyncio.Task[None]] = []

    async def second_stop() -> None:
        second_entered.set()
        await runtime.stop(main["id"])

    try:
        await asyncio.wait_for(started[0].wait(), 2)
        second = await runtime.team.spawn("后提交终态", "工作", parent_id=main["id"])
        await asyncio.wait_for(started[1].wait(), 2)
        second_task = runtime.team._active[second["id"]]
        stoppers.append(asyncio.create_task(runtime.stop(main["id"])))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(second_cleaning.wait(), 2)
        stoppers.append(asyncio.create_task(second_stop()))
        await asyncio.wait_for(second_entered.wait(), 2)
        assert runtime.team._stopping_tasks[second_task] == 2
        release_second.set()
        await asyncio.wait_for(stoppers[1], 2)
        assert not stoppers[0].done()
        assert runtime._stopping[main["id"]] == 1
        assert runtime.team._stopping_tasks[original] == 1
        assert runtime.team._stopping_tasks[second_task] == 1
        assert runtime.team._stopping_generations[(child_id, 1)] == 1
        runtime.conversation.enqueue(
            main["id"],
            "后来排队的主人输入",
            source="web",
            principal="owner",
            mode="yolo",
            max_pending=runtime.config.settings.limits.max_pending_runs,
        )
        await runtime.wake_pending_inputs()
        assert main["id"] not in runtime.tasks
        release.set()
        await asyncio.wait_for(asyncio.gather(*stoppers), 2)
        await asyncio.wait_for(main_finished.wait(), 2)
        assert runtime.store.pending_inputs(main["id"]) == 0
        assert runtime.team.get(child_id)["completion_revision"] == 1
        assert not runtime._stopping and not runtime.team._stopping_tasks
        assert not runtime.team._stopping_generations
    finally:
        release.set()
        release_second.set()
        await asyncio.gather(*stoppers, return_exceptions=True)
        await runtime.close()


async def test_main_stop_consumes_late_owner_input_once_after_cleanup(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    started, cleaning, release, next_started = (asyncio.Event() for _ in range(4))
    successor: asyncio.Task[Any] | None = None
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls, successor
            calls += 1
            seen.append(list(messages))
            if calls == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            else:
                successor = asyncio.current_task()
                next_started.set()
            yield StreamEvent("done", turn=ModelTurn(content="已处理后来输入"))

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    main = runtime.main_session()
    stopper: asyncio.Task[None] | None = None
    try:
        await runtime.submit("旧任务", mode="yolo")
        await asyncio.wait_for(started.wait(), 2)
        stopper = asyncio.create_task(runtime.stop(main["id"]))
        await asyncio.wait_for(cleaning.wait(), 2)
        receipt = await runtime.submit(
            "后来的主人输入", source="channel:fake", principal="owner", event_id="later"
        )
        assert receipt["queued"] and runtime.store.pending_inputs(main["id"]) == 1
        release.set()
        await asyncio.wait_for(stopper, 2)
        await asyncio.wait_for(next_started.wait(), 2)
        assert successor is not None
        await asyncio.wait_for(successor, 2)
        assert calls == 2 and runtime.store.pending_inputs(main["id"]) == 0
        assert any(message.content == "后来的主人输入" for message in seen[-1])
        assert (
            sum(
                item["id"] == receipt["message"]["id"]
                for item in runtime.store.messages(main["id"])
            )
            == 1
        )
        assert not runtime._stopping and not runtime.team._stopping_tasks
        assert not runtime.team._stopping_generations
    finally:
        release.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await runtime.close()
