"""Small evidence/admission boundaries; the complete Runtime journey is separate."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from collections.abc import AsyncIterator, Coroutine
from contextvars import Context
from copy import deepcopy
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn, cast

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_workflows import step

from muli.evidence import EvidenceError
from muli.providers import Message, ModelClient, ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.workflow_sources import SOURCE_NOTICE, WorkflowSources


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("工作流来源单元验证不得访问网络或启动进程")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", reject)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    value, _ = make_runtime(tmp_path, [])
    value.config.update(
        {
            "default_mode": "yolo",
            "persona": {"prompt": "只执行主人本轮明确的新任务。"},
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
            "compaction": {"enabled": False},
            "memory": {"auto_extract": False, "context_max_chars": 0},
        }
    )
    await value.reconfigure()
    owner = value.main_session()
    owner["mode"] = "yolo"
    value.store.put("session", owner)
    try:
        yield value
    finally:
        await value.close()


def saved_source(runtime: Runtime) -> dict[str, Any]:
    """Unit fixture with real delivered owner archive and explicit old run records."""
    owner = runtime.main_session()["id"]
    runtime.conversation.enqueue(
        owner,
        "请读取旧资料并完成原任务。",
        source="web",
        principal="owner",
        mode="yolo",
        max_pending=runtime.config.settings.limits.max_pending_runs,
    )
    original = runtime.store.claim_inputs(owner, "source-parent", limit=1)[0]
    result = "旧资料🙂：第一段完整原文。\n第二段包含唯一记号 SOURCE-ORIGINAL。"
    node = {
        **step("read"),
        "session_id": "source-node",
        "status": "completed",
        "started_at": 2.0,
        "finished_at": 4.0,
        "result": result,
        "result_truncated": False,
    }
    workflow = {
        "id": "source-workflow-run",
        "run_id": "source-workflow-run",
        "workflow_id": "source-definition",
        "name": "原资料工作流",
        "parent_session_id": owner,
        "parent_run_id": "source-parent",
        "reply_origin": original["reply_origin"],
        "mode": "yolo",
        "status": "failed",
        "nodes": {"read": node},
        "created_at": 1.0,
        "finished_at": 5.0,
        "updated_at": 5.0,
    }
    producing = {
        "id": "source-execution",
        "session_id": node["session_id"],
        "role": "worker",
        "mode": "yolo",
        "status": "completed",
        "result": result,
        "created_at": 2.5,
        "finished_at": 3.5,
    }
    runtime.store.put_many(
        [
            ("workflow_run", workflow),
            (
                "session",
                {
                    "id": node["session_id"],
                    "workflow_id": workflow["workflow_id"],
                    "workflow_run_id": workflow["id"],
                    "workflow_node_id": "read",
                    "role": "worker",
                    "mode": "yolo",
                    "status": "idle",
                },
            ),
            ("run", producing),
            (
                "run",
                {
                    "id": "source-parent",
                    "session_id": owner,
                    "role": "main",
                    "mode": "yolo",
                    "status": "failed",
                    "created_at": 1.0,
                    "finished_at": 5.0,
                },
            ),
        ]
    )
    runtime.store.event("run.started", {**producing, "status": "running"}, node["session_id"])
    return workflow


def reference(runtime: Runtime, run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "revision": WorkflowSources(runtime).inspect(run["id"])["revision"],
        "node_ids": ["read"],
        "targets": ["draft"],
    }


def object_counts(runtime: Runtime) -> dict[str, int]:
    return {
        kind: runtime.store.db.execute(
            "SELECT count(*) FROM objects WHERE kind=?", (kind,)
        ).fetchone()[0]
        for kind in ("workflow", "workflow_run", "session")
    }


async def invoke(
    runtime: Runtime,
    args: dict[str, Any],
    *,
    seen: list[list[Message]] | None = None,
    allow_queued_child_failure: bool = False,
) -> dict[str, Any]:
    call_id = "source-create"
    client = FakeClient(
        [
            ModelTurn(tool_calls=[ToolCall(call_id, "workflow_create", json.dumps(args))]),
            ModelTurn(content="本轮以工具回执为准。"),
        ],
        seen if seen is not None else [],
    )
    runtime.client_factory = lambda _: cast(ModelClient, client)
    receipt = await runtime.submit("沿用准确旧资料，执行显式的新任务。", mode="yolo")
    try:
        outcome = await asyncio.wait_for(runtime.wait(receipt["run_id"]), 3)
    except ValueError as error:
        if not (
            allow_queued_child_failure
            and str(error).startswith("子工作流未成功完成：")
            and str(error).endswith("（queued）")
        ):
            raise
    else:
        assert outcome == "本轮以工具回执为准。"
    messages = runtime.store.messages(runtime.main_session()["id"])
    result = next(message for message in messages if message.get("tool_call_id") == call_id)
    assert result["content_provenance"] == "accepted"
    return cast(dict[str, Any], json.loads(result["content"]))


async def test_exact_utf8_pages_have_one_revision_and_complete_old_body(runtime: Runtime) -> None:
    run = saved_source(runtime)
    sources = WorkflowSources(runtime)
    directory = sources.inspect(run["id"])
    assert directory["authority"] == "assistant_data"
    assert directory["source_completeness"] == "stored_record_only"
    assert directory["nodes"][0]["producing_run_id"] == "source-execution"
    content, offset = "", 0
    while True:
        page = sources.inspect(
            run["id"],
            node_id="read",
            revision=directory["revision"],
            offset_bytes=offset,
            length_bytes=7,
        )
        assert page["revision"] == directory["revision"]
        assert page["result_truncated"] is False
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        content += page["content"]
        offset = page["next_offset_bytes"]
        if not page["has_more"]:
            break
    assert content == run["nodes"]["read"]["result"]
    with pytest.raises(EvidenceError, match="UTF-8"):
        sources.inspect(run["id"], node_id="read", revision=directory["revision"], offset_bytes=1)
    with pytest.raises(EvidenceError, match="revision"):
        sources.inspect(run["id"], node_id="read", offset_bytes=3)


async def test_revision_binds_full_original_run_even_without_updated_at_change(
    runtime: Runtime,
) -> None:
    run = saved_source(runtime)
    source = reference(runtime, run)
    old = runtime.store.get("run", "source-execution")
    assert old is not None
    runtime.store.put("run", {**old, "result": old["result"] + "后来改写"})
    with pytest.raises(EvidenceError) as error:
        WorkflowSources(runtime).inspect(run["id"], node_id="read", revision=source["revision"])
    assert error.value.code == "evidence_changed"


async def test_later_session_run_never_replaces_original_producing_run(runtime: Runtime) -> None:
    run = saved_source(runtime)
    source = reference(runtime, run)
    later = {
        "id": "later-execution",
        "session_id": "source-node",
        "role": "worker",
        "mode": "yolo",
        "status": "completed",
        "result": "后来内容",
        "created_at": 6.0,
        "finished_at": 7.0,
    }
    runtime.store.put("run", later)
    runtime.store.event("run.started", later, "source-node")
    page = WorkflowSources(runtime).inspect(run["id"], node_id="read", revision=source["revision"])
    assert page["producing_run_id"] == "source-execution"
    assert page["content"] == run["nodes"]["read"]["result"]


async def test_only_explicit_targets_get_complete_low_trust_material(runtime: Runtime) -> None:
    run = saved_source(runtime)
    before = deepcopy(run)
    source = reference(runtime, run)
    steps = [step("draft"), step("review", role="reviewer", depends_on=["draft"])]
    material, mode = WorkflowSources(runtime).materialize(
        source, steps, current_run_id="new-parent", current_mode="yolo"
    )
    evidence = json.loads(material[0]["prompt"].split(SOURCE_NOTICE, 1)[1])
    assert evidence[0]["output"] == run["nodes"]["read"]["result"]
    assert evidence[0]["producing_run_id"] == "source-execution"
    assert evidence[0]["revision"] == source["revision"]
    assert evidence[0]["authority"] == "assistant_data" and evidence[0]["truncated"] is False
    assert "reply_origin" not in evidence[0]
    assert material[1] == steps[1] and SOURCE_NOTICE not in steps[0]["prompt"]
    assert mode == "yolo" and runtime.store.get("workflow_run", run["id"]) == before
    owner = runtime.main_session()
    runtime.store.put("session", {**owner, "mode": "ask"})
    _, mode = WorkflowSources(runtime).materialize(
        source, steps, current_run_id="new-parent", current_mode="yolo"
    )
    assert mode == "ask"


async def test_completed_material_survives_whole_workflow_timeout(runtime: Runtime) -> None:
    run = saved_source(runtime)
    runtime.store.put("workflow_run", {**run, "status": "timed_out"})
    source = reference(runtime, run)
    steps, _ = WorkflowSources(runtime).materialize(
        source, [step("draft")], current_run_id="new-parent", current_mode="yolo"
    )
    assert (
        run["nodes"]["read"]["result"]
        == json.loads(steps[0]["prompt"].split(SOURCE_NOTICE, 1)[1])[0]["output"]
    )


@pytest.mark.parametrize(
    ("kind", "identifier", "patch", "code"),
    [
        ("node", "read", {"result_truncated": True}, "source_incomplete"),
        ("node", "read", {"result_truncated": None}, "source_incomplete"),
        ("node", "read", {"status": "failed"}, "source_incomplete"),
        ("node", "read", {"status": "skipped"}, "source_incomplete"),
        ("run", "source-execution", {"result": "伪造正文"}, "source_incomplete"),
        ("run", "source-parent", {"status": "running"}, "source_ineligible"),
        ("session", "source-node", {"workflow_node_id": "wrong"}, "source_incomplete"),
        ("workflow_run", "source-workflow-run", {"commitment_id": "task"}, "source_ineligible"),
        ("workflow_run", "source-workflow-run", {"schedule_id": "schedule"}, "source_ineligible"),
    ],
)
async def test_reject_incomplete_and_bound_sources_without_writes(
    runtime: Runtime, kind: str, identifier: str, patch: dict[str, Any], code: str
) -> None:
    run = saved_source(runtime)
    if kind == "node":
        run["nodes"][identifier].update(patch)
        runtime.store.put("workflow_run", run)
    else:
        record = runtime.store.get(kind, identifier)
        assert record is not None
        runtime.store.put(kind, {**record, **patch})
    source, counts = reference(runtime, run), object_counts(runtime)
    with pytest.raises(EvidenceError) as error:
        WorkflowSources(runtime).materialize(
            source, [step("draft")], current_run_id="new-parent", current_mode="yolo"
        )
    assert error.value.code == code and object_counts(runtime) == counts


async def test_raw_byte_gate_and_strict_json_reject_before_reading_content(
    runtime: Runtime,
) -> None:
    run = saved_source(runtime)
    runtime.config.settings.limits.max_context_chars = 4000
    maximum = runtime.config.settings.limits.max_context_chars * 4
    runtime.store.put("workflow_run", {**run, "hidden_padding": "x" * (maximum + 1)})
    with pytest.raises(EvidenceError) as error:
        WorkflowSources(runtime).inspect(run["id"])
    assert error.value.code == "output_budget"
    with runtime.store.db:
        runtime.store.db.execute(
            "UPDATE objects SET data=? WHERE kind='workflow_run' AND id=?",
            ('{"id":"source-workflow-run","id":"source-workflow-run"}', run["id"]),
        )
    with pytest.raises(EvidenceError) as error:
        WorkflowSources(runtime).inspect(run["id"])
    assert error.value.code == "content_unavailable"


async def test_forged_direct_handler_and_worker_schema_have_no_source_authority(
    runtime: Runtime,
) -> None:
    run = saved_source(runtime)
    source, counts = reference(runtime, run), object_counts(runtime)
    tool = runtime._workflow_tool("yolo", runtime.main_session()["id"], "forged", allow_source=True)
    with pytest.raises(PermissionError):
        await tool.handler({"name": "新任务", "steps": [step("draft")], "source": source})
    worker = runtime._workflow_tool("yolo", "worker", "worker-run")
    assert "source" not in worker.parameters["properties"]
    assert object_counts(runtime) == counts


@pytest.mark.parametrize("live", ["source_driver", "parent_finalizer", "same_parent"])
async def test_terminal_record_does_not_override_live_driver_boundary(
    runtime: Runtime, live: str
) -> None:
    run = saved_source(runtime)
    source, counts = reference(runtime, run), object_counts(runtime)
    driver: asyncio.Task[bool] | None = None
    if live != "same_parent":
        driver = asyncio.create_task(asyncio.Event().wait(), name="muli-source-parent")
        if live == "source_driver":
            runtime.workflows._tasks[run["id"]] = driver  # type: ignore[assignment]
        else:
            runtime._session_finalizers.add(driver)
    try:
        with pytest.raises(EvidenceError) as error:
            WorkflowSources(runtime).materialize(
                source,
                [step("draft")],
                current_run_id="source-parent" if live == "same_parent" else "new-parent",
                current_mode="yolo",
            )
        assert error.value.code == "source_ineligible"
        assert object_counts(runtime) == counts
    finally:
        if driver is not None:
            runtime.workflows._tasks.pop(run["id"], None)
            runtime._session_finalizers.discard(driver)
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)


@pytest.mark.parametrize("failure", ["stale_revision", "context", "receipt", "admission"])
async def test_real_owner_refusal_leaves_no_definition_run_or_node_session(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run = saved_source(runtime)
    if failure == "context":
        runtime.config.settings.limits.max_context_chars = 4000
        content = "SOURCE\n" * 400
        run["nodes"]["read"]["result"] = content
        producing = runtime.store.get("run", "source-execution")
        assert producing is not None
        runtime.store.put_many([("workflow_run", run), ("run", {**producing, "result": content})])
    source = reference(runtime, run)
    args = {"name": "新任务", "steps": [step("draft")], "source": source}
    if failure == "stale_revision":
        source["revision"] = "incorrect"
    elif failure == "context":
        args["steps"][0]["prompt"] = "x" * 1000
    elif failure == "receipt":
        args["name"] = "x" * runtime.config.settings.limits.max_output_chars
    else:

        def reject(parent_session_id: str | None, snapshot: object) -> None:
            raise EvidenceError("source_ineligible", "最后同步接纳边界已撤销")

        monkeypatch.setattr(runtime.workflows, "scope_admission", reject)
    counts = object_counts(runtime)
    result = await invoke(runtime, args)
    assert result.get("code") in {"evidence_changed", "output_budget", "source_ineligible"}
    assert object_counts(runtime) == counts
    assert runtime.workflows.active_run_ids == ()


async def test_source_create_task_failure_rolls_back_definition_and_associations(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
) -> None:
    run = saved_source(runtime)
    source, counts = reference(runtime, run), object_counts(runtime)
    original_create = asyncio.create_task
    unstarted: list[Coroutine[Any, Any, Any]] = []
    seen: list[list[Message]] = []

    def fail_workflow_task(
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
        context: Context | None = None,
    ) -> asyncio.Task[Any]:
        if name is not None and name.startswith("muli-workflow-") and not unstarted:
            unstarted.append(coroutine)
            raise RuntimeError("工作流 driver 创建失败")
        return original_create(coroutine, name=name, context=context)

    monkeypatch.setattr(asyncio, "create_task", fail_workflow_task)
    try:
        result = await invoke(
            runtime,
            {"name": "新任务", "steps": [step("draft")], "source": source},
            seen=seen,
            allow_queued_child_failure=True,
        )
        await asyncio.sleep(0)
        assert len(unstarted) == 1 and result.get("error")
        assert object_counts(runtime) == counts
        assert runtime.workflows.active_run_ids == ()
        assert inspect.getcoroutinestate(unstarted[0]) == inspect.CORO_CLOSED
        assert len(seen) == 2
        assert [
            event["data"]["name"]
            for event in runtime.store.events()
            if event["type"] == "tool.started"
        ] == ["workflow_create"]
        assert not any("was never awaited" in str(warning.message) for warning in recwarn)
    finally:
        for coroutine in unstarted:
            coroutine.close()


async def test_source_commit_failure_cancels_unregistered_driver_without_effects(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
) -> None:
    run = saved_source(runtime)
    source, counts = reference(runtime, run), object_counts(runtime)
    connection = runtime.store.db
    original_create = asyncio.create_task
    drivers: list[asyncio.Task[Any]] = []
    boundary_counts: list[dict[str, int]] = []
    seen: list[list[Message]] = []
    idle_calls: list[bool] = []

    def record_workflow_task(
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
        context: Context | None = None,
    ) -> asyncio.Task[Any]:
        task = original_create(coroutine, name=name, context=context)
        if name is not None and name.startswith("muli-workflow-"):
            drivers.append(task)
        return task

    class FailAdmissionCommit:
        def __getattr__(self, name: str) -> Any:
            return getattr(connection, name)

        def __enter__(self) -> FailAdmissionCommit:
            connection.__enter__()
            return self

        def __exit__(
            self,
            error_type: type[BaseException] | None,
            error: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            if error_type is None and drivers and not boundary_counts:
                assert connection.in_transaction
                boundary_counts.append(object_counts(runtime))
                assert runtime.workflows.active_run_ids == ()
                failure = sqlite3.OperationalError("工作流关联事务提交失败")
                connection.__exit__(type(failure), failure, failure.__traceback__)
                raise failure
            return connection.__exit__(error_type, error, traceback)

    monkeypatch.setattr(asyncio, "create_task", record_workflow_task)
    monkeypatch.setattr(runtime.store, "db", FailAdmissionCommit())
    monkeypatch.setattr(runtime.workflows, "on_idle", lambda: idle_calls.append(True))
    result = await invoke(
        runtime, {"name": "新任务", "steps": [step("draft")], "source": source}, seen=seen
    )
    await asyncio.sleep(0)
    assert result.get("error") and len(drivers) == 1
    assert boundary_counts == [{kind: count + 1 for kind, count in counts.items()}]
    assert object_counts(runtime) == counts
    assert runtime.workflows.active_run_ids == () and not idle_calls
    assert drivers[0].cancelled()
    assert inspect.getcoroutinestate(drivers[0].get_coro()) == inspect.CORO_CLOSED
    assert len(seen) == 2
    assert [
        event["data"]["name"] for event in runtime.store.events() if event["type"] == "tool.started"
    ] == ["workflow_create"]
    assert not any("was never awaited" in str(warning.message) for warning in recwarn)
