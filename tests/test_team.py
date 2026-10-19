from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.providers import Message, ModelTurn, StreamEvent, ToolCall


async def test_single_model_slot_can_spawn_wait_and_collect_child(tmp_path: Path) -> None:
    turns = [
        ModelTurn(
            tool_calls=[
                ToolCall(
                    "spawn", "spawn_agent", json.dumps({"task_name": "child", "prompt": "执行"})
                ),
                ToolCall("wait", "wait_agents", json.dumps({"targets": ["child"], "timeout": 2})),
            ]
        ),
        ModelTurn(content="综合结果"),
    ]
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {"max_parallel_agents": 1},
        }
    )
    await runtime.reconfigure()
    active, high_water = 0, 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal active, high_water
            active += 1
            high_water = max(high_water, active)
            try:
                async for event in super().stream(messages, tools):
                    yield event
            finally:
                active -= 1

    runtime.client_factory = lambda config: Client(
        [ModelTurn(content="队友证据")] if config.model == "worker" else turns, seen
    )  # type: ignore[assignment,return-value]
    try:
        started = await runtime.submit("协作", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "综合结果"
        agent = runtime.team.get("child")
        assert agent["status"] == "completed" and high_water == 1
        results = [
            item for item in runtime.store.messages(started["session_id"]) if item["role"] == "tool"
        ]
        assert json.loads(results[1]["content"])["agents"][0]["status"] == "completed"
        assert not runtime.team._waiting
    finally:
        await runtime.close()


async def test_mailbox_only_send_and_resume_reuse_identity(tmp_path: Path) -> None:
    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content="第一轮"), ModelTurn(content="第二轮")]
    )
    parent = runtime.new_session(mode="yolo")
    try:
        agent = await runtime.team.spawn("worker", "工作", parent_id=parent["id"])
        await runtime.team.wait([agent["id"]], timeout=2)
        sent = await runtime.team.send(agent["id"], "补充", wake=False)
        assert sent["accepted"] and runtime.team.get(agent["id"])["status"] == "completed"
        assert runtime.store.pending_inputs(agent["id"]) == 1 and len(seen) == 1
        await runtime.team.resume(agent["id"])
        await runtime.team.wait([agent["id"]], timeout=2)
        assert runtime.team.get(agent["id"])["generation"] == 2
        assert len(runtime.team.list()) == 1
        assert "补充" in seen[1][-1].content
    finally:
        await runtime.close()


async def test_history_count_does_not_consume_pending_budget(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="完成") for _ in range(5)])
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    parent = runtime.new_session(mode="yolo")
    try:
        for index in range(5):
            agent = await runtime.team.spawn(f"worker{index}", "工作", parent_id=parent["id"])
            await runtime.team.wait([agent["id"]], timeout=2)
        assert len(runtime.team.list()) == 5
        assert not runtime.team._active
    finally:
        await runtime.close()


async def test_parent_stop_cancels_descendants_without_main_wakeup(tmp_path: Path) -> None:
    started = asyncio.Event()
    runtime, seen = make_runtime(tmp_path, [])

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent("done", turn=ModelTurn(content="unreachable"))

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        parent = runtime.main_session()
        agent = await runtime.team.spawn("parent", "工作", parent_id=parent["id"])
        child = await runtime.team.spawn("child", "子任务", parent_id=agent["id"])
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(runtime.stop(parent["id"]), 5)
        assert not runtime.tasks and not runtime.team._active
        assert runtime.team.get(agent["id"])["status"] == "cancelled"
        assert runtime.team.get(child["id"])["status"] == "cancelled"
        assert runtime.store.pending_inputs(parent["id"]) == 0
    finally:
        await runtime.close()


async def test_invalid_spawn_leaves_no_empty_agent_or_session(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.new_session(mode="yolo")
    try:
        before = runtime.store.list("session")
        with pytest.raises(ValueError, match="64000"):
            await runtime.team.spawn("invalid", '"' * 40000, parent_id=parent["id"])
        assert runtime.store.list("session") == before and not runtime.team.list()
    finally:
        await runtime.close()


async def test_stop_before_first_schedule_cleans_registered_identity(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    parent = runtime.new_session(mode="yolo")
    try:
        agent = await runtime.team.spawn("queued", "工作", parent_id=parent["id"])
        result = await runtime.team.stop(agent["id"])
        assert result["status"] == "cancelled"
        assert not runtime.team._active and not runtime.tasks and not seen
        assert runtime.store.pending_inputs(agent["id"]) == 0
    finally:
        await runtime.close()


async def test_cancel_traverses_completed_intermediate_parent(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="父任务已完成")])
    root = runtime.new_session(mode="yolo")
    started = asyncio.Event()

    class Waiting(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent("done", turn=ModelTurn(content="unreachable"))

    try:
        parent = await runtime.team.spawn("done-parent", "工作", parent_id=root["id"])
        await runtime.team.wait([parent["id"]], timeout=2)
        runtime.client_factory = lambda _: Waiting([], seen)  # type: ignore[assignment,return-value]
        child = await runtime.team.spawn("active-child", "继续", parent_id=parent["id"])
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(runtime.stop(root["id"]), 5)
        assert runtime.team.get(child["id"])["status"] == "cancelled"
        assert runtime.team.get(parent["id"])["status"] == "completed"
        assert not runtime.tasks
    finally:
        await runtime.close()


async def test_completed_report_waits_durably_for_parent_mailbox_space(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="队友结论")])
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    parent = runtime.main_session()
    runtime.config.settings.models.main.model = ""
    runtime.config.settings.models.worker.model = "worker"
    runtime.config.settings.models.worker.base_url = "https://unused.invalid/v1"
    runtime.conversation.enqueue(
        parent["id"], "占满邮箱", source="web", principal="owner", mode="yolo", max_pending=1
    )
    try:
        agent = await runtime.team.spawn("worker", "工作", parent_id=parent["id"])
        await runtime.team.wait([agent["id"]], timeout=2)
        reports = runtime.store.list("team_report")
        assert len(reports) == 1 and reports[0]["status"] == "pending"
        runtime.store.claim_inputs(parent["id"], "manual-test", limit=1)
        await runtime.team.flush_reports(parent["id"])
        assert runtime.store.list("team_report")[0]["status"] == "delivered"
        assert runtime.store.pending_inputs(parent["id"]) == 1
        await runtime.team.flush_reports(parent["id"])
        assert runtime.store.pending_inputs(parent["id"]) == 1
    finally:
        await runtime.close()


async def test_wait_cycle_and_self_or_ancestor_stop_are_rejected(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.new_session(mode="yolo")
    runtime.team._closed = True
    # Persist dormant identities to exercise graph checks without starting any model.
    for identifier, ancestor in (("a", parent["id"]), ("b", "a")):
        runtime.store.put(
            "agent",
            {
                "id": identifier,
                "name": identifier,
                "parent_id": ancestor,
                "session_id": identifier,
                "status": "idle",
                "mode": "yolo",
            },
        )
    try:
        with pytest.raises(ValueError, match="循环"):
            await runtime.team.wait(["a"], caller="a")
        runtime.team._waiting["a"] = {"b"}
        with pytest.raises(ValueError, match="循环"):
            await runtime.team.wait(["a"], caller="b")
        stop = next(tool for tool in runtime.team.tools("b", "yolo") if tool.name == "stop_agent")
        with pytest.raises(ValueError, match="祖先"):
            await stop.handler({"target": "a"})
    finally:
        await runtime.close()
