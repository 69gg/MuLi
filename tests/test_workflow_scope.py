"""Workflow lifecycle queries stay independent of management history windows."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_reminders import fire_due
from test_runtime import FakeClient, make_runtime, workflow_call

from muli.attention import AttentionInbox
from muli.events import EventBus
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store import LifecycleKind, Store, now
from muli.workflows import WorkflowEngine


def churn_completed_history(runtime: Runtime, count: int) -> None:
    """No tasks: small, newer terminal rows in one temporary transaction."""
    runtime.store.put_many(
        [
            (
                "workflow_run",
                {
                    "id": f"unrelated-history-{index}",
                    "status": "completed",
                    "parent_run_id": "other-parent",
                    "name": "history",
                    "nodes": {},
                },
            )
            for index in range(count)
        ]
    )


def assert_history_preserved(runtime: Runtime, count: int) -> None:
    row = runtime.store.db.execute(
        "SELECT count(*) AS total,sum(CASE WHEN json_extract(data,'$.status')='completed' "
        "AND json_extract(data,'$.parent_run_id')='other-parent' "
        "AND json_extract(data,'$.name')='history' AND json_extract(data,'$.nodes')='{}' "
        "THEN 1 ELSE 0 END) AS unchanged FROM objects "
        "WHERE kind='workflow_run' AND id LIKE 'unrelated-history-%'"
    ).fetchone()
    assert row["total"] == count and (row["unchanged"] or 0) == count


@dataclass
class Scenario:
    runtime: Runtime
    parent_id: str
    parent_run_id: str
    parent_task: asyncio.Task[str]
    child_id: str
    release_worker: asyncio.Event
    release_parent_final: asyncio.Event

    async def close(self) -> None:
        self.release_worker.set()
        self.release_parent_final.set()
        await self.runtime.close()


async def start_blocked_workflow(tmp_path: Path) -> Scenario:
    runtime, _ = make_runtime(tmp_path, [])
    entered_worker = asyncio.Event()
    entered_parent_final = asyncio.Event()
    release_worker = asyncio.Event()
    release_parent_final = asyncio.Event()
    turns = [workflow_call(), ModelTurn(content="parent arranged work")]

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if any(
                message.content.startswith("工作流：")
                for message in messages
                if message.role == "user"
            ):
                entered_worker.set()
                await release_worker.wait()
                yield StreamEvent("done", turn=ModelTurn(content="child evidence"))
                return
            if len(turns) == 1:
                entered_parent_final.set()
                await release_parent_final.wait()
            async for event in super().stream(messages, tools):
                yield event

    runtime.client_factory = lambda _: Client(turns, [])  # type: ignore[assignment,return-value]
    try:
        parent = runtime.main_session()
        started = await runtime.start(parent["id"], "delegate", "yolo")
        parent_task = runtime.tasks[parent["id"]]
        await asyncio.wait_for(entered_parent_final.wait(), 2)
        await asyncio.wait_for(entered_worker.wait(), 2)
        child_id = runtime.workflows.active_run_ids[0]
        child = runtime.store.get("workflow_run", child_id)
        assert child is not None and child["parent_run_id"] == started["run_id"]
        assert child["status"] == "running"
        return Scenario(
            runtime,
            parent["id"],
            started["run_id"],
            parent_task,
            child_id,
            release_worker,
            release_parent_final,
        )
    except BaseException:
        release_worker.set()
        release_parent_final.set()
        await runtime.close()
        raise


@pytest.mark.parametrize("history_count", [0, 10_000])
async def test_restart_recovers_live_workflow_outside_recent_history(
    tmp_path: Path, history_count: int
) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    runtime = scenario.runtime
    snapshot_dir = tmp_path / "crash-snapshot"
    snapshot_dir.mkdir()
    try:
        churn_completed_history(runtime, history_count)
        # Snapshot an actual in-flight Runtime/WorkflowEngine/worker, then clean
        # up the original normally. No process crash or user database is needed.
        with sqlite3.connect(snapshot_dir / "muli.sqlite3") as destination:
            runtime.store.db.backup(destination)
    finally:
        await scenario.close()

    second = Runtime(runtime.config, snapshot_dir, client_factory=runtime.client_factory)
    try:
        assert not second.tasks and not second.workflows.active_run_ids
        restored = second.store.get("workflow_run", scenario.child_id)
        assert restored is not None
        assert restored["status"] == "interrupted", {
            "workflow_status": restored["status"],
            "busy": second.busy,
            "attention_busy": second.attention_busy,
            "live_workflow_tasks": second.workflows.active_run_ids,
        }
        assert not second.busy and not second.attention_busy
        assert_history_preserved(second, history_count)
    finally:
        await second.close()


@pytest.mark.parametrize("history_count", [0, 10_000])
@pytest.mark.parametrize("action", ["complete", "cancel"])
async def test_parent_scope_survives_unrelated_history_churn(
    tmp_path: Path, action: str, history_count: int
) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    runtime = scenario.runtime
    observer: asyncio.Task[bool] | None = None
    try:
        churn_completed_history(runtime, history_count)
        if history_count:
            assert all(
                row["id"] != scenario.child_id for row in runtime.store.list("workflow_run", 10_000)
            )

        if action == "cancel":
            await asyncio.wait_for(runtime.stop(scenario.parent_id), 2)
            child = runtime.store.get("workflow_run", scenario.child_id)
            assert child is not None
            assert child["status"] == "cancelled", {
                "child_status": child["status"],
                "still_live": scenario.child_id in runtime.workflows.active_run_ids,
                "parent_run": runtime.store.get("run", scenario.parent_run_id),
            }
        else:
            entered_child_wait = asyncio.Event()
            real_wait = runtime.workflows.wait

            async def observed_wait(run_id: str) -> dict[str, Any]:
                assert run_id == scenario.child_id
                entered_child_wait.set()
                return await real_wait(run_id)

            runtime.workflows.wait = observed_wait  # type: ignore[method-assign]
            observer = asyncio.create_task(entered_child_wait.wait())
            scenario.release_parent_final.set()
            done, _ = await asyncio.wait(
                [observer, scenario.parent_task],
                timeout=2,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert observer in done and not scenario.parent_task.done(), {
                "parent_done": scenario.parent_task.done(),
                "parent_run": runtime.store.get("run", scenario.parent_run_id),
                "child": runtime.store.get("workflow_run", scenario.child_id),
            }
            scenario.release_worker.set()
            assert "child evidence" in await asyncio.wait_for(scenario.parent_task, 2)
        assert_history_preserved(runtime, history_count)
    finally:
        if observer is not None and not observer.done():
            observer.cancel()
        if observer is not None:
            await asyncio.gather(observer, return_exceptions=True)
        await scenario.close()


@pytest.mark.parametrize("status", ["pending", "queued", "running"])
async def test_recovery_pages_all_four_kinds_and_preserves_other_states(
    tmp_path: Path, status: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    kinds: tuple[LifecycleKind, ...] = ("session", "run", "approval", "workflow_run")
    expected: dict[LifecycleKind, list[str]] = {}
    untouched: list[tuple[str, dict[str, Any]]] = []
    records: list[tuple[str, dict[str, Any]]] = []
    for kind in kinds:
        expected[kind] = [f"{kind}-unfinished-{index:03}" for index in range(105)]
        for identifier in expected[kind]:
            item: dict[str, Any] = {
                "id": identifier,
                "status": status,
                "unknown_effect": "do not repeat",
            }
            if kind == "workflow_run":
                session_id = "session-for-" + identifier
                records.append(("session", {"id": session_id, "status": status}))
                item["nodes"] = {
                    "work": {"session_id": session_id, "status": status},
                    "parked": {"session_id": "absent-paused", "status": "paused"},
                }
            records.append((kind, item))
        for preserved in ("paused", "completed", "failed", "cancelled", "interrupted", "idle"):
            item = {"id": f"{kind}-kept-{preserved}", "status": preserved, "evidence": "原样保留"}
            records.append((kind, item))
            untouched.append((kind, dict(item)))
    runtime.store.put_many(records)
    config, data_dir = runtime.config, runtime.data_dir
    await runtime.close()
    calls = 0

    def forbidden_client(config: Any) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("恢复不能调用模型或重新执行节点")

    recovered = Runtime(config, data_dir, client_factory=forbidden_client)
    try:
        for kind, identifiers in expected.items():
            for identifier in identifiers:
                item = recovered.store.get(kind, identifier)
                assert item is not None
                expected_status = (
                    "idle"
                    if kind == "session"
                    else "expired"
                    if kind == "approval" and status == "pending"
                    else "interrupted"
                )
                assert item["status"] == expected_status
                assert item["unknown_effect"] == "do not repeat"
                if kind == "workflow_run":
                    node = item["nodes"]["work"]
                    assert node["status"] == (
                        "pending" if status in {"pending", "queued"} else "interrupted"
                    )
                    assert recovered.store.get("session", node["session_id"])["status"] == "idle"  # type: ignore[index]
                    assert item["nodes"]["parked"]["status"] == "paused"
            assert recovered.store.unfinished_object_ids(kind) == []
        assert all(recovered.store.get(kind, item["id"]) == item for kind, item in untouched)
        assert not recovered.busy and not recovered.attention_busy and calls == 0
    finally:
        await recovered.close()


@pytest.mark.parametrize("kind", ["session", "run", "approval", "workflow_run"])
def test_unfinished_pages_are_stable_while_each_page_changes_status(
    tmp_path: Path, kind: LifecycleKind
) -> None:
    store = Store(tmp_path / "pages.sqlite3")
    try:
        identifiers = [f"unfinished-{index:03}" for index in range(205)]
        store.put_many(
            [
                (kind, {"id": identifier, "status": ("pending", "queued", "running")[index % 3]})
                for index, identifier in enumerate(identifiers)
            ]
            + [(kind, {"id": f"new-history-{index}", "status": "completed"}) for index in range(12)]
        )
        seen: list[str] = []
        after_id = ""
        while page := store.unfinished_object_ids(kind, after_id=after_id):
            assert len(page) <= 100
            after_id = page[-1]
            seen.extend(page)
            with store.db:
                for identifier in page:
                    store._put(kind, {"id": identifier, "status": "interrupted"})
        assert seen == identifiers
        assert not store.unfinished_object_ids(kind)
    finally:
        store.close()


def test_lifecycle_queries_only_project_ids_and_use_scoped_indexes(tmp_path: Path) -> None:
    store = Store(tmp_path / "projection.sqlite3")
    try:
        for identifier, status, parent in (
            ("a", "pending", "parent"),
            ("b", "queued", "parent"),
            ("c", "running", "other-parent"),
            ("d", "completed", "parent"),
            ("e", "paused", "parent"),
        ):
            store.put(
                "workflow_run",
                {
                    "id": identifier,
                    "status": status,
                    "workflow_id": "definition",
                    "parent_run_id": parent,
                    "name": "工作流",
                    "nodes": {"large": "原文" * 50_000},
                },
            )
        statements: list[str] = []
        store.db.set_trace_callback(statements.append)

        def reject_large_text(value: bytes) -> str:
            assert len(value) <= 512, "生命周期投影不得把完整节点/正文加载到 Python"
            return value.decode("utf-8")

        store.db.text_factory = reject_large_text
        assert store.unfinished_object_ids("workflow_run", workflow_id="definition", limit=2) == [
            "a",
            "b",
        ]
        unfinished_query = statements[-1]
        assert store.workflow_child_ids("parent", limit=100) == ["a", "b", "d", "e"]
        children_query = statements[-1]
        assert store.has_unfinished_workflow("definition")
        assert not store.has_unfinished_workflow("missing")
        assert store.workflow_run_summary("a") == {
            "id": "a",
            "workflow_id": "definition",
            "name": "工作流",
            "status": "pending",
            "created_at": None,
            "updated_at": None,
        }
        assert store.workflow_run_summary("missing") is None
        store.db.set_trace_callback(None)
        assert any(
            "objects_unfinished_lifecycle" in row["detail"]
            for row in store.db.execute("EXPLAIN QUERY PLAN " + unfinished_query)
        )
        assert any(
            "workflow_runs_by_parent" in row["detail"]
            for row in store.db.execute("EXPLAIN QUERY PLAN " + children_query)
        )
    finally:
        store.close()


@pytest.mark.parametrize("limit", [0, -1, 101, True])
def test_lifecycle_pages_reject_unbounded_or_boolean_limits(tmp_path: Path, limit: int) -> None:
    store = Store(tmp_path / "invalid.sqlite3")
    try:
        with pytest.raises(ValueError):
            store.unfinished_object_ids("workflow_run", limit=limit)
        with pytest.raises(ValueError):
            store.workflow_child_ids("parent", limit=limit)
        with pytest.raises(ValueError):
            store.unfinished_object_ids("run", workflow_id="definition")
    finally:
        store.close()


def test_old_database_adds_indexes_idempotently_and_rollback_preserves_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration.sqlite3"
    original = Store(path)
    original.put("workflow_run", {"id": "legacy", "status": "running", "parent_run_id": "parent"})
    with original.db:
        original.db.execute("DROP INDEX objects_unfinished_lifecycle")
        original.db.execute("DROP INDEX workflow_runs_by_parent")
    original.close()
    for _ in range(2):
        store = Store(path)
        try:
            assert store.unfinished_object_ids("workflow_run") == ["legacy"]
            assert store.workflow_child_ids("parent") == ["legacy"]
            with pytest.raises(RuntimeError, match="rollback"):
                with store.db:
                    store._put(
                        "workflow_run",
                        {"id": "legacy", "status": "completed", "parent_run_id": "new"},
                    )
                    raise RuntimeError("rollback")
            assert store.unfinished_object_ids("workflow_run") == ["legacy"]
            assert store.workflow_child_ids("parent") == ["legacy"]
        finally:
            store.close()


async def test_parent_collects_terminal_child_without_live_handle(tmp_path: Path) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    try:
        scenario.release_worker.set()
        child = await asyncio.wait_for(scenario.runtime.workflows.wait(scenario.child_id), 2)
        assert child["status"] == "completed"
        assert scenario.child_id not in scenario.runtime.workflows.active_run_ids
        churn_completed_history(scenario.runtime, 10_000)
        scenario.release_parent_final.set()
        assert "child evidence" in await asyncio.wait_for(scenario.parent_task, 2)
    finally:
        await scenario.close()


@pytest.mark.parametrize("last_status", ["completed", "failed"])
async def test_child_results_stay_bounded_but_all_pages_are_checked(
    tmp_path: Path, last_status: str
) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    runtime = scenario.runtime
    try:
        scenario.release_worker.set()
        original_child = await asyncio.wait_for(runtime.workflows.wait(scenario.child_id), 2)
        runtime.config.update({"limits": {"max_output_chars": 1000}})
        children = [
            {
                "id": f"stored-child-{index:03}",
                "name": f"历史子项 {index}\x00",
                "result": '引文\\"🙂' * 40,
                "status": last_status if index == 204 else "completed",
                "nodes": {},
                "parent_run_id": scenario.parent_run_id,
                "parent_session_id": scenario.parent_id,
            }
            for index in range(205)
        ]
        runtime.store.put_many([("workflow_run", child) for child in children])
        waited: list[str] = []
        real_wait = runtime.workflows.wait

        async def observe_wait(identifier: str) -> dict[str, Any]:
            waited.append(identifier)
            return await real_wait(identifier)

        runtime.workflows.wait = observe_wait  # type: ignore[method-assign]
        scenario.release_parent_final.set()
        if last_status == "failed":
            with pytest.raises(ValueError, match="子工作流未成功完成"):
                await asyncio.wait_for(scenario.parent_task, 2)
            parent = runtime.store.get("run", scenario.parent_run_id)
            assert parent is not None and parent["status"] == "failed"
        else:
            result = await asyncio.wait_for(scenario.parent_task, 2)
            expected = "\n\n".join(
                f"{child['name']}\n{child.get('result', '')}"
                for child in sorted([original_child, *children], key=lambda child: child["id"])
            )[:1000]
            assert result == expected and len(result) == 1000
        assert waited == sorted([scenario.child_id, *(child["id"] for child in children)])
        assert waited[-1] == children[-1]["id"]
    finally:
        await scenario.close()


async def test_child_pages_keep_exact_execution_scope_and_all_terminal_children(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        parent_id = runtime.main_session()["id"]
        old_ids = [f"old-{index:03}" for index in range(205)]
        runtime.store.put_many(
            [
                (
                    "workflow_run",
                    {
                        "id": identifier,
                        "workflow_id": "definition",
                        "parent_run_id": "old-run",
                        "parent_session_id": parent_id,
                        "status": "completed",
                    },
                )
                for identifier in old_ids
            ]
            + [
                (
                    "workflow_run",
                    {
                        "id": "new-child",
                        "workflow_id": "definition",
                        "parent_run_id": "new-run",
                        "parent_session_id": parent_id,
                        "status": "pending",
                    },
                )
            ]
        )
        actual: list[str] = []
        after_id = ""
        while page := runtime.workflows.child_run_ids("old-run", after_id=after_id):
            after_id = page[-1]
            actual.extend(page)
        assert actual == old_ids
        assert runtime.workflows.unfinished_run_ids(workflow_id="definition") == ["new-child"]
        assert runtime.workflows.has_unfinished("definition")
        assert runtime.busy  # Legacy pending is part of the same unfinished set.
    finally:
        await runtime.close()


async def test_old_parent_stop_does_not_cancel_later_execution_of_same_session(
    tmp_path: Path,
) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    runtime = scenario.runtime
    old_stop_releasing, release_old_stop = asyncio.Event(), asyncio.Event()
    new_worker_started, release_new_worker = asyncio.Event(), asyncio.Event()
    new_parent_final, release_new_parent = asyncio.Event(), asyncio.Event()
    original_release = runtime.computer.release_execution
    releases = 0
    stopper: asyncio.Task[None] | None = None
    turns = [workflow_call(), ModelTurn(content="new parent arranged work")]

    async def release_execution(session_id: str, run_id: str) -> None:
        nonlocal releases
        await original_release(session_id, run_id)
        if session_id == scenario.parent_id and run_id == scenario.parent_run_id:
            releases += 1
            if releases == 2:
                # The driver has settled. Its observer still owns an old stop
                # while the session can legitimately begin a later execution.
                old_stop_releasing.set()
                await release_old_stop.wait()

    class NewClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if any(
                message.content.startswith("工作流：")
                for message in messages
                if message.role == "user"
            ):
                new_worker_started.set()
                await release_new_worker.wait()
                yield StreamEvent("done", turn=ModelTurn(content="new child evidence"))
                return
            if len(turns) == 1:
                new_parent_final.set()
                await release_new_parent.wait()
            async for event in super().stream(messages, tools):
                yield event

    runtime.computer.release_execution = release_execution  # type: ignore[method-assign]
    try:
        stopper = asyncio.create_task(runtime.stop(scenario.parent_id))
        await asyncio.wait_for(old_stop_releasing.wait(), 2)
        assert scenario.parent_task.done() and scenario.parent_id not in runtime.tasks
        runtime.client_factory = lambda _: NewClient(turns, [])  # type: ignore[assignment,return-value]
        started = await runtime.start(scenario.parent_id, "later owner task", "yolo")
        await asyncio.wait_for(new_parent_final.wait(), 2)
        await asyncio.wait_for(new_worker_started.wait(), 2)
        new_child_id = runtime.workflows.active_run_ids[0]
        new_task = runtime.tasks[scenario.parent_id]
        child = runtime.store.get("workflow_run", new_child_id)
        assert child is not None and child["parent_run_id"] == started["run_id"]
        assert child["parent_session_id"] == scenario.parent_id
        assert started["run_id"] != scenario.parent_run_id
        release_old_stop.set()
        await asyncio.wait_for(stopper, 2)
        assert not new_task.done() and not new_task.cancelling()
        assert runtime.store.get("workflow_run", new_child_id)["status"] == "running"  # type: ignore[index]
        release_new_worker.set()
        release_new_parent.set()
        assert "new child evidence" in await asyncio.wait_for(new_task, 2)
    finally:
        release_old_stop.set()
        release_new_worker.set()
        release_new_parent.set()
        if stopper is not None:
            await asyncio.gather(stopper, return_exceptions=True)
        await scenario.close()


async def test_real_attention_resumes_after_recovering_old_live_workflow(tmp_path: Path) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    runtime = scenario.runtime
    snapshot_dir = tmp_path / "attention-snapshot"
    snapshot_dir.mkdir()
    try:
        churn_completed_history(runtime, 10_000)
        with sqlite3.connect(snapshot_dir / "muli.sqlite3") as destination:
            runtime.store.db.backup(destination)
    finally:
        await scenario.close()
    delivered = asyncio.Event()
    seen: list[list[Message]] = []

    class NoticeClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert any("【系统注意事项" in message.content for message in messages)
            async for event in super().stream(messages, tools):
                yield event
            delivered.set()

    runtime.config.update({"attention": {"coalesce_seconds": 0}})
    recovered = Runtime(
        runtime.config,
        snapshot_dir,
        client_factory=lambda _: NoticeClient([ModelTurn(content="恢复后提醒")], seen),
    )  # type: ignore[arg-type,return-value]
    inbox = AttentionInbox(recovered)
    try:
        await inbox.start()
        item = recovered.reminders.create("恢复后资料", now() + 60, "不要求重做旧副作用")
        await fire_due(recovered.reminders, item)
        await asyncio.wait_for(delivered.wait(), 2)
        assert len(seen) == 1
        assert recovered.store.get("workflow_run", scenario.child_id)["status"] == "interrupted"  # type: ignore[index]
        assert not recovered.workflows.active_run_ids
    finally:
        await inbox.close()
        await recovered.close()


async def test_persisted_terminal_status_does_not_drop_live_finalizer(tmp_path: Path) -> None:
    store = Store(tmp_path / "finalizer.sqlite3")
    bus = EventBus(store)
    finishing, release = asyncio.Event(), asyncio.Event()
    real_emit = bus.emit

    async def gated_emit(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        if (
            event_type == "workflow.updated"
            and data["run"]["status"] == "completed"
            and data["node_id"] is None
        ):
            finishing.set()
            await release.wait()
        return await real_emit(event_type, data, session_id)

    async def complete(session_id: str, prompt: str, role: str, mode: str) -> str:
        return "result"

    bus.emit = gated_emit  # type: ignore[method-assign]
    engine = WorkflowEngine(store, bus, complete)
    try:
        workflow = engine.create("finalizer", [{"id": "work", "prompt": "work"}])
        run = await engine.start(workflow["id"])
        await asyncio.wait_for(finishing.wait(), 2)
        assert engine.active_run_ids == (run["id"],)
        assert engine.unfinished_run_ids() == []
        assert not engine.has_unfinished(workflow["id"])
        release.set()
        assert (await engine.wait(run["id"]))["status"] == "completed"
        assert not engine.active_run_ids
    finally:
        release.set()
        await engine.close()
        store.close()
