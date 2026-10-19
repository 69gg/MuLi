from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime
from test_task_briefs import attach_services, bind

from muli.attention import AttentionInbox
from muli.context import ContextLimitError
from muli.local_services import LocalServices
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.task_control import ControlConflict, OwnerControlRequest, TaskPaused
from muli.tools import Tool, parameters


async def rig(
    tmp_path: Path, turns: list[ModelTurn]
) -> tuple[Runtime, LocalServices, dict[str, Any], dict[str, Any], list[list[Message]]]:
    runtime, seen = make_runtime(tmp_path, turns)
    services = attach_services(runtime, tmp_path)
    item = await services.commitments.save(
        "受控任务", brief={"goal": "检查项目", "constraints": ["不可部署", "必须保留证据"]}
    )
    worker = bind(runtime, item["id"])
    return runtime, services, item, worker, seen


async def wait_agent(runtime: Runtime, identifier: str, status: str) -> dict[str, Any]:
    async def wait() -> dict[str, Any]:
        async with runtime.bus.changed:
            await runtime.bus.changed.wait_for(
                lambda: runtime.team.get(identifier)["status"] == status
            )
            return runtime.team.get(identifier)

    return await asyncio.wait_for(wait(), 3)


def command(
    runtime: Runtime,
    services: LocalServices,
    identifier: str,
    action: str,
    epoch: int,
    request_id: str,
) -> None:
    services.task_control.owner_control(
        identifier,
        OwnerControlRequest(action, epoch, request_id),  # type: ignore[arg-type]
        main_session_id=runtime.main_session()["id"],
        mode="yolo",
        model_configured=True,
        max_pending=runtime.config.settings.limits.max_pending_runs,
        active_workflow_ids=runtime.workflows.active_run_ids,
    )


@pytest.mark.parametrize("gate", ["approval.requested", "tool.started"])
async def test_pause_wins_after_last_await_before_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    runtime, services, item, worker, seen = await rig(
        tmp_path, [ModelTurn(tool_calls=[ToolCall("write", "probe", "{}")])]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.bus.emit
    effects: list[str] = []

    async def handler(args: dict[str, Any]) -> Any:
        effects.append("entered")
        return {"ok": True}

    async def emit(kind: str, data: dict[str, Any], session_id: str | None = None) -> Any:
        result = await original(kind, data, session_id)
        if kind == gate:
            entered.set()
            if gate == "tool.started":
                await release.wait()
        return result

    runtime.tools["probe"] = Tool("probe", "test", parameters({}), handler, "write")
    monkeypatch.setattr(runtime.bus, "emit", emit)
    try:
        started = await runtime.start(
            worker["id"], "检查", mode="ask" if gate.startswith("approval") else "yolo"
        )
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        if gate.startswith("approval"):
            await runtime.permissions.resolve(next(iter(runtime.permissions.pending)), "approve")
        release.set()
        with pytest.raises(TaskPaused):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 2)
        assert effects == [] and len(seen) == 1
        messages = runtime.store.messages(worker["id"])
        result = json.loads(messages[-1]["content"])
        assert result["status"] == "not_executed" and result["code"] == "task_paused"
        completed = [
            event for event in runtime.store.events(limit=100) if event["type"] == "tool.completed"
        ]
        assert len(completed) == 1 and completed[0]["data"]["call_id"] == "write"
        assert json.loads(completed[0]["data"]["result"]) == result
        run = runtime.store.get("run", started["run_id"])
        assert run is not None and run["status"] == "paused" and run["observed_epoch"] == 0
        assert runtime.store.get("session", worker["id"])["status"] == "paused"  # type: ignore[index]
        assert not runtime.tasks and not runtime.permissions.pending
    finally:
        release.set()
        await runtime.close()


async def test_entered_handler_finishes_but_remaining_native_batch_is_paired(
    tmp_path: Path,
) -> None:
    native = {"type": "signed_native", "signature": "preserve\u0001", "arguments": "verbatim"}
    runtime, services, item, worker, seen = await rig(
        tmp_path,
        [
            ModelTurn(
                tool_calls=[
                    ToolCall("a", "probe", "{}"),
                    ToolCall("b", "probe", "{}"),
                    ToolCall("c", "probe", "{}"),
                ],
                provider_data=native,
            )
        ],
    )
    calls: list[str] = []

    async def handler(args: dict[str, Any]) -> Any:
        calls.append("started")
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        calls.append("finished")
        return {"actually_executed": True}

    runtime.tools["probe"] = Tool("probe", "test", parameters({}), handler, "write")
    try:
        started = await runtime.start(worker["id"], "执行", mode="yolo")
        with pytest.raises(TaskPaused):
            await runtime.wait(started["run_id"])
        assert calls == ["started", "finished"] and len(seen) == 1
        messages = runtime.store.messages(worker["id"])
        assistant = messages[1]
        assert assistant["provider_data"] == native
        assert [call["id"] for call in assistant["tool_calls"]] == ["a", "b", "c"]
        results = [m for m in messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in results] == ["a", "b", "c"]
        assert json.loads(results[0]["content"])["actually_executed"]
        assert all(json.loads(m["content"])["code"] == "task_paused" for m in results[1:])
        completed = [
            event for event in runtime.store.events(limit=100) if event["type"] == "tool.completed"
        ]
        assert [event["data"]["call_id"] for event in completed] == ["a", "b", "c"]
        replay = await runtime._next_context(worker["id"])
        assert replay[-4].provider_data == native
    finally:
        await runtime.close()


async def test_pause_does_not_cancel_stream_and_read_result_can_finish(tmp_path: Path) -> None:
    runtime, services, item, worker, seen = await rig(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()
    read: list[str] = []

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(messages)
            entered.set()
            await release.wait()
            yield StreamEvent("done", turn=ModelTurn(tool_calls=[ToolCall("read", "probe", "{}")]))

    async def handler(args: dict[str, Any]) -> Any:
        read.append("read")
        return "evidence"

    runtime.client_factory = lambda config: Client([], seen)  # type: ignore[assignment,return-value]
    runtime.tools["probe"] = Tool("probe", "read", parameters({}), handler)
    try:
        started = await runtime.start(worker["id"], "只读取", mode="yolo")
        task = runtime.tasks[worker["id"]]
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        assert not task.done() and task.cancelling() == 0
        release.set()
        with pytest.raises(TaskPaused):
            await runtime.wait(started["run_id"])
        assert read == ["read"] and len(seen) == 1
        assert json.loads(runtime.store.messages(worker["id"])[-1]["content"]) == "evidence"
    finally:
        release.set()
        await runtime.close()


async def test_context_hook_keeps_original_brief_epoch_and_capacity_rechecks(
    tmp_path: Path,
) -> None:
    runtime, services, item, worker, seen = await rig(tmp_path, [ModelTurn(content="不应调用")])
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(session_id: str, messages: list[Message]) -> list[Message]:
        entered.set()
        await release.wait()
        return messages

    runtime.context_hooks.append(hook)
    try:
        preparation = asyncio.create_task(runtime._prepare_request(worker["id"]))
        await asyncio.wait_for(entered.wait(), 2)
        command(runtime, services, item["id"], "pause", 0, "pause")
        command(runtime, services, item["id"], "request_replan", 1, "replan")
        release.set()
        prepared = await preparation
        assert prepared.execution_scope is not None and prepared.execution_scope.epoch == 0
        assert any('"epoch":0' in message.content for message in prepared.messages)
        with pytest.raises(TaskPaused):
            runtime._guard_request(prepared)
        assert not seen
    finally:
        release.set()
        await runtime.close()


async def test_pause_while_waiting_for_model_capacity_never_calls_model(tmp_path: Path) -> None:
    runtime, services, item, worker, seen = await rig(tmp_path, [ModelTurn(content="不应调用")])
    runtime.capacity = asyncio.Semaphore(0)
    prepared = asyncio.Event()

    async def hook(session_id: str, messages: list[Message]) -> list[Message]:
        prepared.set()
        return messages

    runtime.context_hooks.append(hook)
    try:
        started = await runtime.start(worker["id"], "等槽", mode="yolo")
        await asyncio.wait_for(prepared.wait(), 2)
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        runtime.capacity.release()
        with pytest.raises(TaskPaused):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 2)
        assert seen == []
    finally:
        await runtime.close()


async def test_main_read_selects_only_next_request_and_cannot_guess_epoch(tmp_path: Path) -> None:
    runtime, services, item, worker, seen = await rig(tmp_path, [])
    main = runtime.main_session()
    command(runtime, services, item["id"], "pause", 0, "pause")
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    args = {"observed_epoch": 2, "plan": "只核查不部署", "targets": []}
    turns = [
        ModelTurn(
            tool_calls=[
                ToolCall("read", "commitment_read", json.dumps({"identifier": item["id"]})),
                ToolCall("too-soon", "task_approve_plan", json.dumps(args)),
            ]
        ),
        ModelTurn(tool_calls=[ToolCall("approve", "task_approve_plan", json.dumps(args))]),
        ModelTurn(content="方案已确认；没有派遣目标"),
    ]
    runtime.client_factory = lambda config: FakeClient(turns, seen)  # type: ignore[assignment,return-value]
    try:
        await runtime.wake_pending_inputs()
        run_id = runtime.run_ids[main["id"]]
        await asyncio.wait_for(runtime.wait(run_id), 2)
        results = {
            m.get("tool_call_id"): json.loads(m["content"])
            for m in runtime.store.messages(main["id"])
            if m["role"] == "tool"
        }
        assert "下一轮" in results["too-soon"]["error"]
        assert results["approve"]["current"]["control"]["approved_epoch"] == 2
        assert runtime.task_control_current(item["id"])["control"]["paused"] is False
        assert any(
            "完整受控任务说明" in message.content and '"epoch":2' in message.content
            for message in seen[1]
        )
        assert not runtime._selected_replans and len(seen) == 3
        assert runtime.task_control.execution_snapshot(main["id"]) is None  # type: ignore[union-attr]
    finally:
        await runtime.close()


async def test_worker_inheriting_main_context_cannot_approve_or_see_schema(tmp_path: Path) -> None:
    runtime, services, item, worker, seen = await rig(tmp_path, [])
    main = runtime.main_session()
    command(runtime, services, item["id"], "pause", 0, "pause")
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    runtime.select_replan(main["id"], item["id"])
    runtime.store.claim_inputs(main["id"], "test", limit=8)
    agent = runtime.team.get(worker["id"])
    agent.pop("commitment_id")
    runtime.store.put("agent", agent)
    prepared = await runtime._prepare_request(main["id"])
    main_tools = runtime.session_tools(main["id"])
    # A real main handler creates a child Task, which copies both ContextVars.
    task: asyncio.Task[Any] | None = None

    async def child() -> Any:
        assert "task_approve_plan" not in runtime.session_tools(worker["id"])
        return await runtime._tool(
            ToolCall(
                "forged",
                "task_approve_plan",
                json.dumps({"observed_epoch": 2, "plan": "fake", "targets": []}),
            ),
            main_tools,
            "yolo",
            worker["id"],
            "child",
        )

    async def handler(args: dict[str, Any]) -> Any:
        nonlocal task
        task = asyncio.create_task(child())
        return await task

    try:
        result = await runtime._tool(
            ToolCall("spawn", "spawn", "{}"),
            {"spawn": Tool("spawn", "test", parameters({}), handler)},
            "yolo",
            main["id"],
            "main",
            prepared=prepared,
        )
        assert "只有真实主会话" in result["error"]
        assert runtime.task_control_current(item["id"])["control"]["paused"]
        assert runtime.tool_session_id is None and runtime.request_context(main["id"]) is None
    finally:
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_full_brief_remains_pinned_or_budget_fails_explicitly(tmp_path: Path) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [])
    marker = "中间不能丢失的条件" * 50
    item = await services.commitments.save(
        item["title"],
        identifier=item["id"],
        brief={"goal": "g", "constraints": ["头" * 3000 + marker + "尾" * 3000]},
    )
    runtime.store.add_message(worker["id"], "user", "旧用户" * 4000)
    runtime.store.add_message(worker["id"], "assistant", "旧答复" * 4000)
    runtime.store.add_message(worker["id"], "user", "继续")
    try:
        messages = await runtime._next_context(worker["id"])
        assert marker in next(m.content for m in messages if "完整受控任务说明" in m.content)
        runtime.config.update({"limits": {"max_context_chars": 4000}})
        with pytest.raises(ContextLimitError):
            await runtime._next_context(worker["id"])
    finally:
        await runtime.close()


async def test_main_spawn_does_not_pass_replan_capability_to_worker(tmp_path: Path) -> None:
    runtime, services, item, _, seen = await rig(tmp_path, [])
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    await runtime.reconfigure()
    main = runtime.main_session()
    runtime.store.put("session", {**main, "mode": "yolo"})
    command(runtime, services, item["id"], "pause", 0, "pause")
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    runtime.select_replan(main["id"], item["id"])
    main_turns = [
        ModelTurn(
            tool_calls=[
                ToolCall(
                    "spawn", "spawn_agent", json.dumps({"task_name": "unbound", "prompt": "仅核查"})
                ),
                ToolCall("wait", "wait_agents", json.dumps({"targets": ["unbound"], "timeout": 2})),
            ]
        ),
        ModelTurn(content="待主人确认"),
    ]
    worker_turns = [
        ModelTurn(
            tool_calls=[
                ToolCall(
                    "fake",
                    "task_approve_plan",
                    json.dumps({"observed_epoch": 2, "plan": "fake", "targets": []}),
                )
            ]
        ),
        ModelTurn(content="无确认权限"),
    ]
    worker_schemas: list[list[str]] = []

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            worker_schemas.append([tool["function"]["name"] for tool in tools])
            async for event in super().stream(messages, tools):
                yield event

    def factory(config: ModelConfig) -> FakeClient:
        return (
            Client(worker_turns, seen) if config.model == "worker" else FakeClient(main_turns, seen)
        )

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        await runtime.wake_pending_inputs()
        await asyncio.wait_for(runtime.wait(runtime.run_ids[main["id"]]), 3)
        child = runtime.team.get("unbound")
        assert child["status"] == "completed"
        assert all("task_approve_plan" not in schemas for schemas in worker_schemas)
        result = next(m for m in runtime.store.messages(child["id"]) if m["role"] == "tool")
        assert "未启用" in json.loads(result["content"])["error"]
        assert runtime.task_control_current(item["id"])["control"]["paused"]
        assert runtime.tool_session_id is None
    finally:
        await runtime.close()


async def test_queued_pause_is_durable_waitable_and_does_not_wake_main(tmp_path: Path) -> None:
    runtime, services, item, worker, seen = await rig(tmp_path, [])
    agent = runtime.team.get(worker["id"])
    runtime.store.put("agent", {**agent, "status": "queued", "completion_revision": 0})
    await runtime.team.send(worker["id"], "工作", wake=False)
    try:
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        paused = runtime.team.get(worker["id"])
        assert paused["status"] == "paused" and paused["completion_revision"] == 1
        assert runtime.team.page(active_only=True)["members"][0]["id"] == worker["id"]
        snapshot = await runtime.team.wait([worker["id"]], timeout=0.1)
        assert snapshot["agents"][0]["status"] == "paused"
        assert not runtime.tasks and not runtime.team._active and seen == []
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0
        assert runtime.store.list("team_report")[0]["status"] == "pending"
        await runtime.team.send(worker["id"], "普通消息不能恢复")
        assert (await runtime.team.resume(worker["id"]))["status"] == "paused"
        assert not runtime.tasks and not runtime.team._active
        with pytest.raises(ControlConflict, match="task_paused"):
            await runtime.team.spawn("新树", "工作", commitment_id=item["id"])
        runtime.config.update({"limits": {"max_pending_runs": 1}})
        with pytest.raises(ValueError, match="上限"):
            await runtime.team.spawn("别的树", "工作")
        await runtime.team.stop(worker["id"])
        assert runtime.team.get(worker["id"])["status"] == "cancelled"
    finally:
        await runtime.close()
    reopened = Runtime(runtime.config, runtime.data_dir)
    attach_services(reopened, tmp_path)
    try:
        assert reopened.team.get(worker["id"])["status"] == "cancelled"
        assert reopened.task_control_current(item["id"])["control"]["paused"]
    finally:
        await reopened.close()


async def test_running_pause_releases_slot_and_records_paused_generation(tmp_path: Path) -> None:
    runtime, services, item, _, seen = await rig(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(messages)
            entered.set()
            await release.wait()
            yield StreamEvent("done", turn=ModelTurn(content="迟到输出"))

    runtime.client_factory = lambda config: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        child = await runtime.team.spawn("正在执行", "检查", commitment_id=item["id"])
        await asyncio.wait_for(entered.wait(), 2)
        task = runtime.team._active[child["id"]]
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        assert task.cancelling() == 0
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 2)
        paused = runtime.team.get(child["id"])
        assert paused["status"] == "paused" and paused["generation"] == 1
        assert paused["completion_revision"] == 1 and paused["observed_epoch"] == 0
        assert runtime.store.get("run", paused["run_id"])["status"] == "paused"  # type: ignore[index]
        assert not runtime.tasks and not runtime.team._active and len(seen) == 1
        await asyncio.wait_for(runtime.capacity.acquire(), 0.1)
        runtime.capacity.release()
        assert runtime.store.list("team_report")[0]["status"] == "pending"
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0
    finally:
        release.set()
        await runtime.close()


async def test_workflow_scope_and_pause_admission_are_ordered(tmp_path: Path) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()

    async def step(session_id: str, prompt: str, role: str, mode: str) -> str:
        assert services.task_control.resolve_scope(session_id) == item["id"]
        entered.set()
        await release.wait()
        return "证据"

    runtime.workflows.run_step = step
    workflow = runtime.workflows.create(
        "DAG", [{"id": "a", "title": "节点", "prompt": "检查", "role": "worker", "depends_on": []}]
    )
    try:
        started = await runtime.workflows.start(workflow["id"], parent_session_id=worker["id"])
        await asyncio.wait_for(entered.wait(), 2)
        assert started["commitment_id"] == item["id"]
        assert runtime.task_control_current(item["id"])["scope"]["active_workflow_count"] == 1
        with pytest.raises(ControlConflict, match="workflow_active"):
            await runtime.submit_task_control(
                item["id"], OwnerControlRequest("pause", 0, "blocked")
            )
        assert runtime.task_control_current(item["id"])["control"]["epoch"] == 0
        release.set()
        assert (await runtime.workflows.wait(started["id"]))["status"] == "completed"
        # A done callback can lag task completion; neither is an active finalizer.
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        previous = len(runtime.store.list("workflow_run"))
        with pytest.raises(TaskPaused):
            await runtime.workflows.start(workflow["id"], parent_session_id=worker["id"])
        assert len(runtime.store.list("workflow_run")) == previous
        node = next(iter(started["nodes"].values()))["session_id"]
        node_run = await runtime.start(node, "绕普通入口", mode="yolo")
        with pytest.raises(TaskPaused):
            await runtime.wait(node_run["run_id"])
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_running_plan_delivery_then_old_request_parks_without_losing_resume(
    tmp_path: Path, cancel: bool
) -> None:
    runtime, services, item, _, seen = await rig(tmp_path, [])
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    await runtime.reconfigure()
    entered, release = asyncio.Event(), asyncio.Event()
    worker_requests: list[list[Message]] = []
    effects: list[str] = []

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            worker_requests.append(messages)
            if len(worker_requests) == 1:
                entered.set()
                await release.wait()
                yield StreamEvent(
                    "done", turn=ModelTurn(tool_calls=[ToolCall("old", "probe", "{}")])
                )
            else:
                yield StreamEvent("done", turn=ModelTurn(content="依据新计划继续完毕"))

    def factory(config: ModelConfig) -> FakeClient:
        return (
            Client([], seen)
            if config.model == "worker"
            else FakeClient([ModelTurn(content="资料已收") for _ in range(8)], seen)
        )

    async def handler(args: dict[str, Any]) -> Any:
        effects.append("unexpected")
        return True

    runtime.client_factory = factory  # type: ignore[assignment]
    runtime.tools["probe"] = Tool("probe", "test", parameters({}), handler, "write")
    try:
        child = await runtime.team.spawn("实际队友", "检查", commitment_id=item["id"])
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        command(runtime, services, item["id"], "request_replan", 1, "replan")
        snapshot = services.task_control.replan_snapshot(runtime.main_session()["id"], item["id"])
        result = services.task_control.approve_plan(
            snapshot,
            2,
            "只核查，不部署",
            [child["id"]],
            caller_session_id=runtime.main_session()["id"],
            max_targets=8,
        )
        await runtime.task_control_committed(result)
        delivery = runtime.store.list("task_plan_delivery")[0]
        assert delivery["state"] == "delivered" and delivery["path"] == "ingress"
        assert runtime.store.pending_inputs(child["id"]) == 1
        if cancel:
            await runtime.team.stop(child["id"])
            release.set()
            await runtime.flush_task_plan_deliveries()
            assert runtime.team.get(child["id"])["status"] == "cancelled"
            assert len(worker_requests) == 1
        else:
            release.set()
            completed = await wait_agent(runtime, child["id"], "completed")
            assert completed["generation"] == 2 and completed["completion_revision"] == 2
            assert len(worker_requests) == 2
            completions = runtime.store.team_completions(child["id"], 0, limit=10)
            assert [event["data"]["status"] for event in completions] == ["paused", "completed"]
            assert any(
                '"epoch":2' in message.content and "只核查，不部署" in message.content
                for message in worker_requests[1]
            )
            old_result = next(
                m for m in runtime.store.messages(child["id"]) if m.get("tool_call_id") == "old"
            )
            assert json.loads(old_result["content"])["code"] == "task_paused"
        assert effects == []
    finally:
        release.set()
        await runtime.close()


async def test_paused_full_mailbox_uses_archive_delivery_after_real_restart(tmp_path: Path) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [])
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    agent = runtime.team.get(worker["id"])
    runtime.store.put("agent", {**agent, "status": "queued", "completion_revision": 0})
    await runtime.team.send(worker["id"], "保留的原始待处理任务", wake=False)
    await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    snapshot = services.task_control.replan_snapshot(runtime.main_session()["id"], item["id"])
    services.task_control.approve_plan(
        snapshot,
        2,
        "检查现状再继续",
        [worker["id"]],
        caller_session_id=runtime.main_session()["id"],
        max_targets=1,
    )
    assert runtime.store.pending_inputs(worker["id"]) == 1
    await runtime.close()
    seen: list[list[Message]] = []

    def factory(config: ModelConfig) -> FakeClient:
        return FakeClient([ModelTurn(content="新计划证据") for _ in range(8)], seen)

    reopened = Runtime(runtime.config, runtime.data_dir, client_factory=factory)  # type: ignore[arg-type]
    attach_services(reopened, tmp_path)
    try:
        await reopened.startup()
        completed = await wait_agent(reopened, worker["id"], "completed")
        assert completed["generation"] == 1 and completed["completion_revision"] == 2
        delivery = reopened.store.list("task_plan_delivery")[0]
        assert delivery["state"] == "delivered" and delivery["path"] == "archive"
        messages = reopened.store.messages(worker["id"])
        notices = [
            message for message in messages if message.get("principal") == "system:task_control"
        ]
        assert len(notices) == 1 and notices[0]["task_control"]["epoch"] == 2
        assert any("保留的原始待处理任务" in message["content"] for message in messages)
        await reopened.flush_task_plan_deliveries()
        assert (
            len(
                [
                    m
                    for m in reopened.store.messages(worker["id"])
                    if m.get("principal") == "system:task_control"
                ]
            )
            == 1
        )
        assert any(
            "检查现状再继续" in message.content and "完整受控任务说明" in message.content
            for request in seen
            for message in request
        )
    finally:
        await reopened.close()


async def test_delivery_keyset_pass_reaches_paused_target_after_full_running_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [])
    other = bind(runtime, item["id"])
    for session in (worker, other):
        agent = runtime.team.get(session["id"])
        runtime.store.put(
            "agent", {**agent, "status": "running", "generation": 1, "completion_revision": 0}
        )
    command(runtime, services, item["id"], "pause", 0, "pause")
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    snapshot = services.task_control.replan_snapshot(runtime.main_session()["id"], item["id"])
    services.task_control.approve_plan(
        snapshot,
        2,
        "继续",
        [worker["id"], other["id"]],
        caller_session_id=runtime.main_session()["id"],
        max_targets=8,
    )
    deliveries = services.task_control.pending_deliveries(8)
    first, second = deliveries
    await runtime.team.send(first["target_session_id"], "占满邮箱", wake=False)
    parked = runtime.team.get(second["target_session_id"])
    runtime.team._finish(parked, "paused", "旧请求已停靠", None)
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    monkeypatch.setattr(runtime.team, "start", lambda: None)
    try:
        await runtime.flush_task_plan_deliveries()
        assert runtime.store.get("task_plan_delivery", first["id"])["state"] == "pending"  # type: ignore[index]
        delivered = runtime.store.get("task_plan_delivery", second["id"])
        assert (
            delivered is not None
            and delivered["state"] == "delivered"
            and delivered["path"] == "archive"
        )
        assert runtime.team.get(second["target_session_id"])["status"] == "queued"
    finally:
        await runtime.close()


async def test_delivery_remembers_capacity_event_after_cursor_already_passed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [])
    agent = runtime.team.get(worker["id"])
    runtime.store.put(
        "agent", {**agent, "status": "running", "generation": 1, "completion_revision": 0}
    )
    command(runtime, services, item["id"], "pause", 0, "pause")
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    snapshot = services.task_control.replan_snapshot(runtime.main_session()["id"], item["id"])
    services.task_control.approve_plan(
        snapshot,
        2,
        "新计划",
        [worker["id"]],
        caller_session_id=runtime.main_session()["id"],
        max_targets=8,
    )
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    await runtime.team.send(worker["id"], "占满邮箱", wake=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep
    flusher: asyncio.Task[None] | None = None
    pages: list[str | None] = []
    original_pending = services.task_control.pending_deliveries

    async def sleep(delay: float) -> None:
        if asyncio.current_task() is flusher and not entered.is_set():
            entered.set()
            await release.wait()
        await original_sleep(delay)

    def pending(limit: int, *, after_id: str | None = None) -> list[dict[str, Any]]:
        pages.append(after_id)
        return original_pending(limit, after_id=after_id)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(services.task_control, "pending_deliveries", pending)
    monkeypatch.setattr(runtime.team, "start", lambda: None)
    try:
        flusher = asyncio.create_task(runtime.flush_task_plan_deliveries())
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime.store.list("task_plan_delivery")[0]["state"] == "pending"
        await runtime._claim_inputs(worker["id"], "capacity-release")
        assert runtime._plans_dirty
        release.set()
        await asyncio.wait_for(flusher, 2)
        assert runtime.store.list("task_plan_delivery")[0]["state"] == "delivered"
        assert pages.count(None) == 2
        frozen_count = len(pages)
        for _ in range(100):
            await original_sleep(0)
        assert len(pages) == frozen_count and not runtime._flushing_plans
    finally:
        release.set()
        if flusher:
            await asyncio.gather(flusher, return_exceptions=True)
        await runtime.close()


async def test_pause_scope_covers_inherited_children_and_more_than_management_page(
    tmp_path: Path,
) -> None:
    runtime, services, item, first, _ = await rig(tmp_path, [])
    members = [first, *(bind(runtime, item["id"]) for _ in range(100))]
    for index, member in enumerate(members):
        agent = runtime.team.get(member["id"])
        if index == 1:
            agent.pop("commitment_id")
            agent["parent_id"] = first["id"]
        elif index == 2:
            agent["parent_id"] = members[1]["id"]
        runtime.store.put("agent", {**agent, "status": "queued", "completion_revision": 0})
    unrelated = await services.commitments.save(
        "其它任务", brief={"goal": "独立检查", "constraints": []}
    )
    other = bind(runtime, unrelated["id"])
    frozen = await runtime._prepare_request(first["id"])
    try:
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        assert runtime.task_control_current(item["id"])["scope"]["member_count"] == 101
        assert len(runtime.team.list()) == 100
        for member in members:
            assert runtime.team.get(member["id"])["status"] == "paused"
            prepared = await runtime._prepare_request(member["id"])
            with pytest.raises(TaskPaused):
                runtime._guard_request(prepared)
        with pytest.raises(TaskPaused):
            runtime._guard_request(frozen)
        runtime._guard_request(await runtime._prepare_request(other["id"]))
        assert not runtime.tasks and not runtime.team._active
    finally:
        await runtime.close()


async def test_workflow_terminal_publish_still_blocks_pause_until_actual_task_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.bus.emit

    async def step(session_id: str, prompt: str, role: str, mode: str) -> str:
        return "证据"

    async def emit(kind: str, data: dict[str, Any], session_id: str | None = None) -> Any:
        if kind == "workflow.updated" and data.get("run", {}).get("status") == "completed":
            entered.set()
            await release.wait()
        return await original(kind, data, session_id)

    runtime.workflows.run_step = step
    monkeypatch.setattr(runtime.bus, "emit", emit)
    workflow = runtime.workflows.create(
        "完成边界",
        [{"id": "a", "title": "a", "prompt": "检查", "role": "worker", "depends_on": []}],
    )
    try:
        started = await runtime.workflows.start(workflow["id"], parent_session_id=worker["id"])
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime.store.get("workflow_run", started["id"])["status"] == "completed"  # type: ignore[index]
        assert runtime.task_control_current(item["id"])["scope"]["active_workflow_count"] == 1
        with pytest.raises(ControlConflict, match="workflow_active"):
            await runtime.submit_task_control(
                item["id"], OwnerControlRequest("pause", 0, "blocked")
            )
        release.set()
        await runtime.workflows.wait(started["id"])
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
    finally:
        release.set()
        await runtime.close()


async def test_paused_report_does_not_starve_unrelated_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [ModelTurn(content="独立完成")])
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    monkeypatch.setattr(runtime, "_wake_main", lambda: None)
    agent = runtime.team.get(worker["id"])
    runtime.store.put("agent", {**agent, "status": "queued", "completion_revision": 0})
    try:
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        await runtime.team.stop(worker["id"])
        other = await runtime.team.spawn("独立任务", "完成并报告")
        tasks = list(runtime.team._tasks)
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert runtime.team.get(other["id"])["status"] == "completed"
        reports = [
            row for row in runtime.store.list("team_report") if row["agent_id"] == other["id"]
        ]
        assert len(reports) == 1
        assert reports[0]["status"] == "delivered"
    finally:
        await runtime.close()


async def test_paused_report_does_not_mark_idle_runtime_attention_busy(tmp_path: Path) -> None:
    runtime, services, item, worker, _ = await rig(tmp_path, [])
    agent = runtime.team.get(worker["id"])
    runtime.store.put("agent", {**agent, "status": "queued", "completion_revision": 0})
    try:
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        assert not runtime.busy and not runtime.tasks
        assert not runtime.attention_busy
    finally:
        await runtime.close()


async def test_pause_after_driver_final_guard_defers_completed_team_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, _, seen = await rig(
        tmp_path, [ModelTurn(content="队友已完成"), ModelTurn(content="主助手被意外唤醒")]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.bus.emit
    main_id = runtime.main_session()["id"]

    async def emit(kind: str, data: dict[str, Any], session_id: str | None = None) -> Any:
        if kind == "run.completed" and session_id != main_id:
            entered.set()
            await release.wait()
        return await original(kind, data, session_id)

    monkeypatch.setattr(runtime.bus, "emit", emit)
    try:
        worker = await runtime.team.spawn("最后完成边界", "检查", commitment_id=item["id"])
        await asyncio.wait_for(entered.wait(), 2)
        task = runtime.team._active[worker["id"]]
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 2)
        main_task = runtime.tasks.get(main_id)
        if main_task is not None:
            await asyncio.wait_for(asyncio.shield(main_task), 2)
        report = next(
            record
            for record in runtime.store.list("team_report")
            if record["agent_id"] == worker["id"]
        )
        assert report["status"] == "pending" and len(seen) == 1
    finally:
        release.set()
        await runtime.close()


async def test_pause_before_queued_worker_launch_does_not_need_full_context_budget(
    tmp_path: Path,
) -> None:
    runtime, services, item, _, seen = await rig(tmp_path, [])
    await services.commitments.save(
        "较长的合法要求",
        identifier=item["id"],
        brief={"goal": "完整目标", "constraints": ["原始限制" * 2000]},
    )
    runtime.config.update({"limits": {"max_context_chars": 4000}})
    await runtime.configuration_lock.acquire()
    pause: asyncio.Task[Any] | None = None
    try:
        pause = asyncio.create_task(
            runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
        )
        await asyncio.sleep(0)  # Establish the owner waiter before the worker's waiter.
        worker = await runtime.team.spawn("等待准入", "检查", commitment_id=item["id"])
        task = runtime.team._active[worker["id"]]
        runtime.configuration_lock.release()
        await asyncio.wait_for(pause, 2)
        await asyncio.wait_for(asyncio.shield(task), 2)
        assert seen == []
        assert runtime.team.get(worker["id"])["status"] == "paused"
    finally:
        if runtime.configuration_lock.locked():
            runtime.configuration_lock.release()
        if pause is not None:
            await asyncio.gather(pause, return_exceptions=True)
        await runtime.close()


async def test_real_attention_consumer_delivers_notice_while_task_remains_paused(
    tmp_path: Path,
) -> None:
    runtime, services, item, worker, seen = await rig(tmp_path, [ModelTurn(content="只读告知进展")])
    runtime.config.update({"attention": {"coalesce_seconds": 0}})
    agent = runtime.team.get(worker["id"])
    runtime.store.put("agent", {**agent, "status": "queued", "completion_revision": 0})
    await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 0, "pause"))
    inbox = AttentionInbox(runtime)
    try:
        plan = runtime.store.put(
            "management", {"id": "review-plan", "action": "configure", "status": "completed"}
        )
        await runtime.bus.emit("management.completed", plan)
        await inbox.start()

        async def delivered() -> None:
            async with runtime.bus.changed:
                await runtime.bus.changed.wait_for(lambda: len(seen) == 1)

        await asyncio.wait_for(delivered(), 1)
        assert runtime.team.get(worker["id"])["status"] == "paused"
        main_id = runtime.main_session()["id"]
        messages = runtime.store.messages(main_id)
        notice = next(
            message for message in messages if message.get("source") == "system:attention"
        )
        assert notice["principal"] == "system:attention" and notice.get("route") is None
    finally:
        await inbox.close()
        await runtime.close()


async def test_report_capacity_release_during_keyset_pass_requests_one_event_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _, worker, _ = await rig(tmp_path, [])
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    main_id = runtime.main_session()["id"]
    runtime.conversation.enqueue(
        main_id, "占满父邮箱", source="web", principal="owner", mode="yolo", max_pending=1
    )
    runtime.store.put(
        "team_report",
        {
            "id": "pending-completion",
            "agent_id": worker["id"],
            "parent_id": main_id,
            "status": "pending",
            "content": "已完成，待核查",
        },
    )
    runtime.store.put(
        "team_report",
        {
            "id": "later-missing-parent",
            "agent_id": worker["id"],
            "parent_id": "deleted-parent",
            "status": "pending",
            "content": "无法交付的旧报告",
        },
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original_sleep, original_deliver = asyncio.sleep, runtime.team._deliver_report
    flusher: asyncio.Task[None] | None = None
    attempts: list[str] = []

    async def sleep(delay: float) -> None:
        if asyncio.current_task() is flusher and not entered.is_set():
            entered.set()
            await release.wait()
        await original_sleep(delay)

    async def deliver(report: dict[str, Any]) -> None:
        attempts.append(report["id"])
        await original_deliver(report)

    monkeypatch.setattr(runtime, "_wake_main", lambda: None)
    monkeypatch.setattr(runtime.team, "start", lambda: None)
    monkeypatch.setattr(runtime.team, "_deliver_report", deliver)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    try:
        flusher = asyncio.create_task(runtime.team.flush_reports())
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime.store.get("team_report", "pending-completion")["status"] == "pending"  # type: ignore[index]
        await runtime._claim_inputs(main_id, "release-report-capacity")
        assert runtime.team._reports_dirty
        release.set()
        await asyncio.wait_for(flusher, 2)
        assert runtime.store.get("team_report", "pending-completion")["status"] == "delivered"  # type: ignore[index]
        assert runtime.store.pending_inputs(main_id) == 1
        assert attempts == ["pending-completion", "later-missing-parent", "pending-completion"]
        for _ in range(100):
            await original_sleep(0)
        assert len(attempts) == 3 and not runtime.team._flushing
    finally:
        release.set()
        if flusher:
            await asyncio.gather(flusher, return_exceptions=True)
        await runtime.close()


async def test_report_page_does_not_resurrect_report_suppressed_by_completed_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, _, _ = await rig(
        tmp_path, [ModelTurn(content="第一份结果"), ModelTurn(content="第二份结果")]
    )
    original_flush, original_emit = runtime.team.flush_reports, runtime.bus.emit
    entered, release = asyncio.Event(), asyncio.Event()
    flusher: asyncio.Task[None] | None = None

    async def hold_reports(parent_id: str | None = None) -> None:
        return None

    async def emit(kind: str, data: dict[str, Any], session_id: str | None = None) -> Any:
        result = await original_emit(kind, data, session_id)
        if kind == "team.result" and not entered.is_set():
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(runtime.team, "flush_reports", hold_reports)
    monkeypatch.setattr(runtime, "_wake_main", lambda: None)
    try:
        await runtime.team.spawn("第一位", "完成")
        await runtime.team.spawn("第二位", "完成")
        await asyncio.wait_for(asyncio.gather(*list(runtime.team._tasks)), 2)
        assert len(runtime.store.list("team_report")) == 2
        monkeypatch.setattr(runtime.team, "flush_reports", original_flush)
        monkeypatch.setattr(runtime.bus, "emit", emit)
        flusher = asyncio.create_task(runtime.team.flush_reports())
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.stop(runtime.main_session()["id"])
        suppressed = [
            report["id"]
            for report in runtime.store.list("team_report")
            if report["status"] == "suppressed"
        ]
        assert len(suppressed) == 1
        release.set()
        await asyncio.wait_for(flusher, 2)
        report = runtime.store.get("team_report", suppressed[0])
        assert report is not None and report["status"] == "suppressed"
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0
    finally:
        release.set()
        if flusher is not None:
            await asyncio.gather(flusher, return_exceptions=True)
        await runtime.close()


async def test_new_owner_pause_clears_selected_replan_before_invalid_old_evidence_blocks_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, _, seen = await rig(tmp_path, [ModelTurn(content="无关主聊天正常")])
    reference_session = runtime.new_session("可删除的独立历史")
    evidence = runtime.store.add_message(reference_session["id"], "user", "旧证据")
    await services.commitments.save(
        "原任务",
        identifier=item["id"],
        brief={
            "goal": "完整目标",
            "constraints": [],
            "evidence_refs": [{"kind": "message", "id": evidence["id"]}],
        },
    )
    command(runtime, services, item["id"], "pause", 0, "pause")
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    main_id = runtime.main_session()["id"]
    runtime.select_replan(main_id, item["id"])
    original_wake = runtime._wake_main
    monkeypatch.setattr(runtime, "_wake_main", lambda: None)
    try:
        runtime.store.delete("message", evidence["id"])
        await runtime.submit_task_control(item["id"], OwnerControlRequest("pause", 2, "new-pause"))
        await runtime.stop(main_id)
        monkeypatch.setattr(runtime, "_wake_main", original_wake)
        started = await runtime.submit("先聊别的事情")
        assert await runtime.wait(started["run_id"]) == "无关主聊天正常"
        assert len(seen) == 1 and not runtime._selected_replans
    finally:
        await runtime.close()


async def test_duplicate_owner_receipt_preserves_newer_replan_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services, item, _, _ = await rig(tmp_path, [])
    command(runtime, services, item["id"], "pause", 0, "pause")
    command(runtime, services, item["id"], "request_replan", 1, "replan")
    main_id = runtime.main_session()["id"]
    runtime.select_replan(main_id, item["id"])
    monkeypatch.setattr(runtime, "_wake_main", lambda: None)
    try:
        result = await runtime.submit_task_control(
            item["id"], OwnerControlRequest("pause", 0, "pause")
        )
        assert not result.created
        assert runtime._selected_replans == {main_id: item["id"]}
        result = await runtime.submit_task_control(
            item["id"], OwnerControlRequest("pause", 2, "new-pause")
        )
        assert result.created and not runtime._selected_replans
    finally:
        await runtime.close()
