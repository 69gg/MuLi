from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from muli.events import EventBus
from muli.store import Store, now
from muli.workflows import JsonObject, RunStep, WorkflowEngine


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "workflow.sqlite3")
    yield value
    value.close()


@pytest_asyncio.fixture
async def factory(store: Store) -> AsyncIterator[Callable[..., WorkflowEngine]]:
    engines: list[WorkflowEngine] = []

    def create(callback: RunStep, **options: Any) -> WorkflowEngine:
        engine = WorkflowEngine(store, EventBus(store), callback, **options)
        engines.append(engine)
        return engine

    yield create
    for engine in engines:
        await engine.close()


def step(
    identifier: str, *, depends_on: list[str] | None = None, role: str = "worker"
) -> JsonObject:
    return {
        "id": identifier,
        "title": identifier,
        "prompt": f"执行 {identifier}",
        "role": role,
        "depends_on": depends_on or [],
    }


async def completed(engine: WorkflowEngine, run_id: str) -> JsonObject:
    async with asyncio.timeout(2):
        while True:
            run = engine.store.get("workflow_run", run_id)
            assert run is not None
            if run["status"] not in {"queued", "running"}:
                return run
            revision = engine.bus.revision
            async with engine.bus.changed:
                await engine.bus.changed.wait_for(
                    lambda revision=revision: engine.bus.revision != revision
                )


async def started(engine: WorkflowEngine, run_id: str, event: asyncio.Event) -> None:
    ready = asyncio.create_task(event.wait())
    finished = asyncio.create_task(engine.wait(run_id))
    try:
        # Keep the watchdog, but inspect the real condition after waking. A timer
        # can run after Event.set() and before its waiter resumes in the same turn.
        await asyncio.wait({ready, finished}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        run = engine.store.get("workflow_run", run_id)
        assert run is not None
        statuses = {identifier: node["status"] for identifier, node in run["nodes"].items()}
        assert event.is_set(), f"工作流未到达同步点：status={run['status']}, nodes={statuses}"
    finally:
        for observer in (ready, finished):
            if not observer.done():
                observer.cancel()
        # WorkflowEngine.wait shields the actual run; only these observers stop.
        await asyncio.gather(ready, finished, return_exceptions=True)


async def noop(session_id: str, prompt: str, role: str, mode: str) -> str:
    return "完成"


@pytest.mark.asyncio
async def test_dag_parallel_workers_independent_sessions_and_final_reviewer(
    factory: Callable[..., WorkflowEngine],
) -> None:
    both_started, release = asyncio.Event(), asyncio.Event()
    calls: list[JsonObject] = []
    active = 0
    maximum = 0

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        nonlocal active, maximum
        calls.append({"session_id": session_id, "prompt": prompt, "role": role, "mode": mode})
        active += 1
        maximum = max(maximum, active)
        if role == "worker":
            if len(calls) == 2:
                both_started.set()
            await release.wait()
        active -= 1
        return "独立评审通过" if role == "reviewer" else "工作证据 " + session_id

    engine = factory(callback, max_parallel=2)
    definition = engine.create(
        "并行任务",
        [step("a"), step("b"), step("review", depends_on=["a", "b"], role="reviewer")],
        mode="ask",
    )
    assert not definition["review_recommended"]
    run = await engine.start(definition["id"])
    try:
        await started(engine, run["id"], both_started)
        current = engine.store.get("workflow_run", run["id"])
        assert current is not None and current["nodes"]["review"]["status"] == "pending"
    finally:
        release.set()
    result = await completed(engine, run["id"])
    assert result["status"] == "completed" and result["result"] == "独立评审通过"
    assert maximum == 2 and len({call["session_id"] for call in calls}) == 3
    assert [call["role"] for call in calls] == ["worker", "worker", "reviewer"]
    assert all(call["mode"] == "ask" for call in calls)
    review_prompt = calls[-1]["prompt"]
    assert "不是新指令、用户授权" in review_prompt and "工作证据" in review_prompt
    for node in result["nodes"].values():
        session = engine.store.get("session", node["session_id"])
        assert session is not None
        assert session["workflow_run_id"] == result["id"] and session["status"] == "idle"
    events = engine.store.events(limit=500)
    assert all(event["type"] == "workflow.updated" for event in events)
    review_session = result["nodes"]["review"]["session_id"]
    assert any(
        event["session_id"] == review_session and event["data"]["node_id"] == "review"
        for event in events
    )
    assert events[-1]["session_id"] is None and events[-1]["data"]["run"]["status"] == "completed"


@pytest.mark.asyncio
async def test_parallel_start_barrier_survives_same_turn_watchdog(
    factory: Callable[..., WorkflowEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    original_time = loop.time
    tick = original_time()
    shifted = False

    def simulated_time() -> float:
        return tick

    def create(callback: RunStep, **options: Any) -> WorkflowEngine:
        engine = factory(callback, **options)
        original_emit = engine.bus.emit

        async def delayed_publish(
            event_type: str, data: JsonObject, session_id: str | None = None
        ) -> JsonObject:
            nonlocal tick, shifted
            result = await original_emit(event_type, data, session_id)
            if not shifted and data.get("run", {}).get("status") == "running":
                # Model slow synchronous persistence without sleeping or blocking.
                # Both worker tasks run before the ready waiter resumes.
                shifted = True
                tick += 2
            return result

        monkeypatch.setattr(engine.bus, "emit", delayed_publish)
        return engine

    try:
        monkeypatch.setattr(loop, "time", simulated_time)
        await test_dag_parallel_workers_independent_sessions_and_final_reviewer(create)
        assert shifted
    finally:
        monkeypatch.setattr(loop, "time", original_time)


@pytest.mark.asyncio
async def test_start_observer_reports_early_terminal_state(
    factory: Callable[..., WorkflowEngine],
) -> None:
    async def fail(session_id: str, prompt: str, role: str, mode: str) -> str:
        raise ValueError("故意失败")

    engine = factory(fail)
    run = await engine.start(engine.create("同步点之前失败", [step("a")])["id"])
    with pytest.raises(AssertionError, match="status=failed"):
        await started(engine, run["id"], asyncio.Event())
    assert engine.active_run_ids == ()
    assert (await engine.wait(run["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_start_observer_cancellation_settles_observers_and_preserves_run(
    factory: Callable[..., WorkflowEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    running, release = asyncio.Event(), asyncio.Event()
    wait_started, wait_settled, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ready_settled = asyncio.Event()

    class ObservedEvent(asyncio.Event):
        async def wait(self) -> bool:
            try:
                return await super().wait()
            finally:
                ready_settled.set()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        running.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "工作仍可完成"

    engine = factory(callback)
    run = await engine.start(engine.create("只取消观察者", [step("a")])["id"])
    original_wait = engine.wait

    async def observed_wait(run_id: str) -> JsonObject:
        wait_started.set()
        try:
            return await original_wait(run_id)
        finally:
            wait_settled.set()

    monkeypatch.setattr(engine, "wait", observed_wait)
    observer = asyncio.create_task(started(engine, run["id"], ObservedEvent()))
    try:
        await wait_started.wait()
        await running.wait()
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert ready_settled.is_set() and wait_settled.is_set() and not cancelled.is_set()
        current = engine.store.get("workflow_run", run["id"])
        assert current is not None and current["status"] == "running"
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        release.set()
    assert (await engine.wait(run["id"]))["result"] == "工作仍可完成"


@pytest.mark.asyncio
async def test_failure_skips_only_descendants_without_retry(
    factory: Callable[..., WorkflowEngine],
) -> None:
    calls: list[str] = []

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        title = prompt.split("节点：", 1)[1].split("\n", 1)[0]
        calls.append(title)
        if title == "fail":
            raise ValueError("secret response headers")
        return "独立分支完成"

    engine = factory(callback)
    definition = engine.create(
        "部分失败",
        [
            step("grandchild", depends_on=["child"]),
            step("child", depends_on=["fail"]),
            step("fail"),
            step("independent"),
        ],
    )
    run = await engine.start(definition["id"])
    result = await completed(engine, run["id"])
    assert result["status"] == "failed"
    assert result["nodes"]["fail"]["status"] == "failed"
    assert (
        result["nodes"]["child"]["status"] == result["nodes"]["grandchild"]["status"] == "skipped"
    )
    assert result["nodes"]["independent"]["status"] == "completed"
    assert calls.count("fail") == 1 and set(calls) == {"fail", "independent"}
    assert "secret response headers" not in json.dumps(result)
    assert result["results"] == {"independent": "独立分支完成"}


@pytest.mark.asyncio
async def test_cancel_propagates_to_active_nodes_and_pending_descendants(
    factory: Callable[..., WorkflowEngine],
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "不会到达"

    engine = factory(callback, max_parallel=1)
    definition = engine.create("取消", [step("a"), step("b", depends_on=["a"])])
    run = await engine.start(definition["id"])
    await asyncio.wait_for(started.wait(), 1)
    result = await engine.cancel(run["id"])
    assert cancelled.is_set() and result["status"] == "cancelled"
    assert all(node["status"] == "cancelled" for node in result["nodes"].values())
    assert (await engine.cancel(run["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_before_first_task_execution_is_persisted(
    factory: Callable[..., WorkflowEngine],
) -> None:
    calls = 0

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        nonlocal calls
        calls += 1
        return "完成"

    engine = factory(callback)
    run = await engine.start(engine.create("立即取消", [step("a")])["id"])
    result = await engine.cancel(run["id"])
    assert result["status"] == result["nodes"]["a"]["status"] == "cancelled"
    assert calls == 0


@pytest.mark.asyncio
async def test_total_timeout_cancels_children(factory: Callable[..., WorkflowEngine]) -> None:
    cancelled = asyncio.Event()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "不会到达"

    engine = factory(callback, timeout=0.01)
    run = await engine.start(engine.create("时限", [step("a"), step("b", depends_on=["a"])])["id"])
    result = await completed(engine, run["id"])
    assert result["status"] == "timed_out" and cancelled.is_set()
    assert all(node["status"] == "cancelled" for node in result["nodes"].values())


@pytest.mark.asyncio
async def test_parallel_limit_is_shared_across_workflow_runs(
    factory: Callable[..., WorkflowEngine],
) -> None:
    first_started, release = asyncio.Event(), asyncio.Event()
    active, maximum, calls = 0, 0, 0

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        nonlocal active, maximum, calls
        active += 1
        calls += 1
        maximum = max(maximum, active)
        first_started.set()
        await release.wait()
        active -= 1
        return "完成"

    engine = factory(callback, max_parallel=1)
    definition = engine.create("并发共享", [step("a")])
    one = await engine.start(definition["id"])
    await asyncio.wait_for(first_started.wait(), 1)
    two = await engine.start(definition["id"])
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert (await completed(engine, one["id"]))["status"] == "completed"
    assert (await completed(engine, two["id"]))["status"] == "completed"
    assert maximum == 1 and calls == 2


@pytest.mark.asyncio
async def test_pending_run_limit_rejects_before_persistence_and_releases_after_cancel(
    factory: Callable[..., WorkflowEngine],
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        started.set()
        await release.wait()
        return "完成"

    engine = factory(callback, max_parallel=1, max_pending_runs=2)
    definition = engine.create("运行总量", [step("a")])
    first = await engine.start(definition["id"])
    await asyncio.wait_for(started.wait(), 1)
    second = await engine.start(definition["id"])
    with pytest.raises(ValueError, match="运行数量已达上限"):
        await engine.start(definition["id"])
    assert len(engine.store.list("workflow_run")) == len(engine.store.list("session")) == 2
    await engine.cancel(second["id"])
    third = await engine.start(definition["id"])
    release.set()
    assert (await engine.wait(first["id"]))["status"] == "completed"
    assert (await engine.wait(third["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_pending_run_limit_is_atomic_before_first_await(
    factory: Callable[..., WorkflowEngine],
) -> None:
    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        await asyncio.Event().wait()
        return "不会到达"

    engine = factory(callback, max_pending_runs=1)
    definition = engine.create("原子数量限制", [step("a")])
    outcomes = await asyncio.gather(
        engine.start(definition["id"]), engine.start(definition["id"]), return_exceptions=True
    )
    assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 1
    assert len(engine.store.list("workflow_run")) == len(engine.store.list("session")) == 1


@pytest.mark.asyncio
async def test_pending_run_limit_must_be_positive(factory: Callable[..., WorkflowEngine]) -> None:
    with pytest.raises(ValueError, match="运行数"):
        factory(noop, max_pending_runs=0)


@pytest.mark.asyncio
async def test_close_cancels_all_runs_and_prevents_new_work(
    factory: Callable[..., WorkflowEngine],
) -> None:
    started = asyncio.Event()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        started.set()
        await asyncio.Event().wait()
        return "不会到达"

    engine = factory(callback)
    definition = engine.create("关停", [step("a")])
    one = await engine.start(definition["id"])
    two = await engine.start(definition["id"])
    await asyncio.wait_for(started.wait(), 1)
    await engine.close()
    assert (await completed(engine, one["id"]))["status"] == "cancelled"
    assert (await completed(engine, two["id"]))["status"] == "cancelled"
    with pytest.raises(RuntimeError, match="关闭"):
        await engine.start(definition["id"])


@pytest.mark.asyncio
async def test_restart_marks_unfinished_nodes_interrupted_without_rerun(
    factory: Callable[..., WorkflowEngine], store: Store
) -> None:
    store.put(
        "session",
        {
            "id": "s",
            "title": "恢复",
            "mode": "auto",
            "status": "running",
            "created_at": now(),
            "updated_at": now(),
        },
    )
    store.put(
        "workflow_run",
        {
            "id": "r",
            "workflow_id": "w",
            "status": "running",
            "nodes": {
                "a": {"session_id": "s", "status": "running"},
                "b": {"status": "completed", "result": "保留"},
            },
            "created_at": now(),
        },
    )
    engine = factory(noop)
    recovered = store.get("workflow_run", "r")
    assert recovered is not None and recovered["status"] == "interrupted"
    assert recovered["nodes"]["a"]["status"] == "interrupted"
    assert recovered["nodes"]["b"] == {"status": "completed", "result": "保留"}
    session = store.get("session", "s")
    assert session is not None and session["status"] == "idle"
    assert not engine._tasks


@pytest.mark.asyncio
async def test_result_and_dependency_budget_are_explicit(
    factory: Callable[..., WorkflowEngine],
) -> None:
    prompts: list[str] = []

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        prompts.append(prompt)
        return "x" * 100 if role == "worker" else "评审"

    engine = factory(callback, max_output_chars=20)
    definition = engine.create(
        "输出限制", [step("a"), step("b"), step("r", depends_on=["a", "b"], role="reviewer")]
    )
    result = await completed(engine, (await engine.start(definition["id"]))["id"])
    assert len(result["nodes"]["a"]["result"]) == 20 and result["nodes"]["a"]["result_truncated"]
    sources = json.loads(prompts[-1].rsplit("\n", 1)[-1])
    assert sum(len(source["output"]) for source in sources) == 20
    assert all(source["truncated"] for source in sources)


@pytest.mark.asyncio
async def test_review_recommendation_and_template_never_rewrite_dag(
    factory: Callable[..., WorkflowEngine],
) -> None:
    engine = factory(noop)
    nodes = [step("a"), step("b"), step("review", depends_on=["a"], role="reviewer")]
    definition = engine.create("缺失分支评审", nodes)
    assert definition["review_recommended"] and definition["warnings"]
    assert definition["steps"] == nodes
    template = engine.review_template("一个实际任务")
    reviewed = engine.create("模板", template)
    assert not reviewed["review_recommended"]
    assert template[0]["prompt"] == "一个实际任务"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nodes",
    [
        [],
        [step("a"), step("a")],
        [step("a", depends_on=["missing"])],
        [step("a", depends_on=["a"])],
        [step("a", depends_on=["b"]), step("b", depends_on=["a"])],
        [{**step("a"), "prompt": " "}],
        [step("a", role="owner")],
        [step("a", depends_on=["b", "b"]), step("b")],
        [{**step("a"), "mode": "yolo"}],
        [{**step("a"), "id": " "}],
        [{**step("a"), "depends_on": "a"}],
    ],
)
async def test_invalid_dags_are_rejected_before_persistence(
    factory: Callable[..., WorkflowEngine], nodes: list[JsonObject]
) -> None:
    engine = factory(noop)
    with pytest.raises(ValueError):
        engine.create("无效", nodes)
    assert not engine.store.list("workflow") and not engine.store.list("session")


@pytest.mark.asyncio
async def test_workflow_limits_names_modes_and_missing_ids(
    factory: Callable[..., WorkflowEngine],
) -> None:
    engine = factory(noop, max_steps=1)
    for name, nodes, mode in [
        ("", [step("a")], "auto"),
        ("name", [step("a")], "root"),
        ("name", [step("a"), step("b")], "auto"),
    ]:
        with pytest.raises(ValueError):
            engine.create(name, nodes, mode=mode)
    with pytest.raises(KeyError):
        await engine.start("missing")
    with pytest.raises(KeyError):
        await engine.cancel("missing")


@pytest.mark.asyncio
async def test_cancelling_finished_run_preserves_completed_output(
    factory: Callable[..., WorkflowEngine],
) -> None:
    engine = factory(noop)
    run = await engine.start(engine.create("完成", [step("a")])["id"])
    result = await completed(engine, run["id"])
    assert (await engine.cancel(run["id"])) == result


@pytest.mark.asyncio
async def test_parent_metadata_and_wait_return_durable_result(
    factory: Callable[..., WorkflowEngine],
) -> None:
    engine = factory(noop)
    run = await engine.start(
        engine.create("父任务", [step("a")])["id"],
        parent_run_id="parent-run",
        parent_session_id="parent-session",
    )
    result = await engine.wait(run["id"])
    assert result["parent_run_id"] == "parent-run"
    assert result["parent_session_id"] == "parent-session"
    assert result["status"] == "completed" and result["result"] == "完成"
    assert (await engine.wait(run["id"])) == result


@pytest.mark.asyncio
async def test_cancelling_waiter_does_not_cancel_child_run(
    factory: Callable[..., WorkflowEngine],
) -> None:
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "保留结果"

    engine = factory(callback)
    run = await engine.start(engine.create("取消等待者", [step("a")])["id"])
    await asyncio.wait_for(started.wait(), 1)
    waiter = asyncio.create_task(engine.wait(run["id"]))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not cancelled.is_set()
    current = engine.store.get("workflow_run", run["id"])
    assert current is not None and current["status"] == "running"
    release.set()
    assert (await engine.wait(run["id"]))["result"] == "保留结果"


@pytest.mark.asyncio
async def test_wait_for_cancelled_child_returns_cancelled_snapshot(
    factory: Callable[..., WorkflowEngine],
) -> None:
    started = asyncio.Event()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        started.set()
        await asyncio.Event().wait()
        return "不会到达"

    engine = factory(callback)
    run = await engine.start(engine.create("取消子任务", [step("a")])["id"])
    await asyncio.wait_for(started.wait(), 1)
    waiter = asyncio.create_task(engine.wait(run["id"]))
    await engine.cancel(run["id"])
    assert (await waiter)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_during_start_publish_does_not_orphan_queued_run(
    factory: Callable[..., WorkflowEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    publishing = asyncio.Event()

    async def callback(session_id: str, prompt: str, role: str, mode: str) -> str:
        await asyncio.Event().wait()
        return "不会到达"

    engine = factory(callback)
    original = engine.bus.emit

    async def delayed_emit(
        event_type: str, data: JsonObject, session_id: str | None = None
    ) -> JsonObject:
        if data.get("run", {}).get("status") == "queued":
            publishing.set()
            await asyncio.Event().wait()
        return await original(event_type, data, session_id)

    monkeypatch.setattr(engine.bus, "emit", delayed_emit)
    launch = asyncio.create_task(
        engine.start(engine.create("启动取消", [step("a")])["id"], parent_run_id="parent")
    )
    await asyncio.wait_for(publishing.wait(), 1)
    launch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launch
    runs = engine.store.list("workflow_run")
    assert len(runs) == 1 and runs[0]["status"] == "cancelled"
    assert runs[0]["parent_run_id"] == "parent"
    assert all(node["status"] == "cancelled" for node in runs[0]["nodes"].values())
