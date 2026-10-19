from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime


class FakeClient:
    def __init__(self, turns: list[ModelTurn], seen: list[list[Message]]) -> None:
        self.turns, self.seen = turns, seen

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        self.seen.append(list(messages))
        turn = self.turns.pop(0)
        if turn.content:
            yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("done", turn=turn)

    async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
        return ModelTurn(content='{"allow":true,"reason":"有界操作"}')


def make_runtime(tmp_path: Path, turns: list[ModelTurn]) -> tuple[Runtime, list[list[Message]]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(tmp_path)],
            "models": {
                "main": {"model": "main", "base_url": "https://unused.invalid/v1"},
                "reviewer": {"model": "reviewer", "base_url": "https://unused.invalid/v1"},
            },
        }
    )
    seen: list[list[Message]] = []

    def factory(config: ModelConfig) -> FakeClient:
        return FakeClient(turns, seen)

    return Runtime(config, tmp_path / "data", client_factory=factory), seen  # type: ignore[arg-type]


async def test_chat_persists_replayable_messages(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="你好")])
    try:
        session = runtime.new_session()
        result = await runtime.start(session["id"], "您好")
        assert await runtime.tasks[session["id"]] == "你好"
        assert result["message"]["role"] == "user"
        assert [m["role"] for m in runtime.store.messages(session["id"])] == ["user", "assistant"]
        assert seen[0][-1].content == "您好"
        assert runtime.store.get("run", result["run_id"])["status"] == "completed"
    finally:
        await runtime.close()


async def test_yolo_tool_result_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "result.txt"
    turns = [
        ModelTurn(
            tool_calls=[
                ToolCall("c1", "fs_write", json.dumps({"path": str(path), "content": "evidence"}))
            ],
            provider_data={"original": "kept"},
        ),
        ModelTurn(content="已写入"),
    ]
    runtime, seen = make_runtime(tmp_path, turns)
    try:
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "写入文件")
        await runtime.tasks[session["id"]]
        assert path.read_text() == "evidence"
        assert seen[1][-2].provider_data == {"original": "kept"}
        assert seen[1][-1].role == "tool"
        assert seen[1][-1].tool_call_id == "c1"
    finally:
        await runtime.close()


async def test_ask_pauses_before_file_read_and_rejects(tmp_path: Path) -> None:
    path = tmp_path / "private.txt"
    path.write_text("not disclosed")
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("c1", "fs_read", json.dumps({"path": str(path)}))]),
            ModelTurn(content="已取消"),
        ],
    )
    try:
        session = runtime.new_session(mode="ask")
        await runtime.start(session["id"], "读文件")
        for _ in range(30):
            if runtime.permissions.pending:
                break
            await asyncio.sleep(0)
        approval_id = next(iter(runtime.permissions.pending))
        assert not any(e["type"] == "tool.started" for e in runtime.store.events())
        await runtime.permissions.resolve(approval_id, "reject")
        await runtime.tasks[session["id"]]
        assert "not disclosed" not in seen[1][-1].content
        assert "拒绝" in seen[1][-1].content
    finally:
        await runtime.close()


async def test_auto_independent_review_allows_read(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("ok")
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("c1", "fs_read", json.dumps({"path": str(path)}))]),
            ModelTurn(content="完成"),
        ],
    )
    try:
        session = runtime.new_session(mode="auto")
        await runtime.start(session["id"], "读取文件")
        await runtime.tasks[session["id"]]
        assert any(e["type"] == "tool.reviewed" for e in runtime.store.events())
        assert "ok" in seen[1][-1].content
    finally:
        await runtime.close()


async def test_invalid_tool_arguments_never_run(tmp_path: Path) -> None:
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("c1", "fs_write", '{"path":"x"}')]),
            ModelTurn(content="参数无效"),
        ],
    )
    try:
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "写文件")
        await runtime.tasks[session["id"]]
        assert "error" in json.loads(seen[1][-1].content)
        assert not any(e["type"] == "tool.started" for e in runtime.store.events())
    finally:
        await runtime.close()


async def test_stop_cancels_pending_approval(tmp_path: Path) -> None:
    runtime, _ = make_runtime(
        tmp_path,
        [ModelTurn(tool_calls=[ToolCall("c1", "fs_list", json.dumps({"path": str(tmp_path)}))])],
    )
    try:
        session = runtime.new_session(mode="ask")
        result = await runtime.start(session["id"], "列目录")
        for _ in range(30):
            if runtime.permissions.pending:
                break
            await asyncio.sleep(0)
        await runtime.stop(session["id"])
        assert not runtime.permissions.pending
        assert runtime.store.get("run", result["run_id"])["status"] == "cancelled"
        assert runtime.store.get("session", session["id"])["status"] == "idle"
    finally:
        await runtime.close()


async def test_missing_model_fails_before_message_is_added(tmp_path: Path) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data")
    try:
        session = runtime.new_session()
        with pytest.raises(ValueError, match="配置"):
            await runtime.start(session["id"], "你好")
        assert runtime.store.messages(session["id"]) == []
    finally:
        await runtime.close()


async def test_immediate_stop_cleans_up_before_first_scheduled_turn(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="不会运行")])
    try:
        session = runtime.new_session()
        result = await runtime.start(session["id"], "开始")
        await runtime.stop(session["id"])
        assert not seen
        assert not runtime.tasks
        assert runtime.store.get("run", result["run_id"])["status"] == "cancelled"
        assert runtime.store.get("session", session["id"])["status"] == "idle"
    finally:
        await runtime.close()


async def test_concurrent_start_reserves_session_before_event_wakeup(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="唯一运行")])
    try:
        session = runtime.new_session()
        async with runtime.bus.changed:
            first = asyncio.create_task(runtime.start(session["id"], "第一个"))
            await asyncio.sleep(0)
            second = asyncio.create_task(runtime.start(session["id"], "第二个"))
            await asyncio.sleep(0)
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        for task in list(runtime.tasks.values()):
            await task
        assert len(seen) == 1
        assert len([m for m in runtime.store.messages(session["id"]) if m["role"] == "user"]) == 1
    finally:
        await runtime.close()


async def test_interrupted_tool_pair_is_repaired_without_replaying_side_effect(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        session = runtime.new_session()
        runtime.store.add_message(session["id"], "user", "修改")
        runtime.store.add_message(
            session["id"],
            "assistant",
            "",
            tool_calls=[
                {"id": "a", "name": "fs_write", "arguments": "{}"},
                {"id": "b", "name": "fs_write", "arguments": "{}"},
            ],
        )
        runtime.store.add_message(session["id"], "tool", "第一步已写入", tool_call_id="a")
        runtime.store.add_message(session["id"], "user", "现在怎么样")
        messages = runtime._history(session["id"])
        results = {
            message.tool_call_id: message.content for message in messages if message.role == "tool"
        }
        assert results["a"] == "第一步已写入"
        assert "未知" in results["b"]
        assert "不要自动重做" in results["b"]
    finally:
        await runtime.close()


async def test_latest_turn_cannot_bypass_context_budget(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="不会调用")])
    runtime.config.update({"limits": {"max_context_chars": 4000}})
    try:
        session = runtime.new_session()
        await runtime.start(session["id"], "x" * 5000)
        with pytest.raises(ValueError, match="上下文.*预算"):
            await runtime.tasks[session["id"]]
        assert not seen
    finally:
        await runtime.close()


@pytest.mark.parametrize("batch_size", [17, 1000])
async def test_oversized_native_tool_batch_fails_before_any_call_or_replay_is_stored(
    tmp_path: Path, batch_size: int
) -> None:
    path = tmp_path / "must-not-be-written.txt"
    calls = [
        ToolCall(f"c{index}", "fs_write", json.dumps({"path": str(path), "content": "bad"}))
        for index in range(batch_size)
    ]
    turn = ModelTurn(
        tool_calls=calls,
        provider_data={
            "protocol": "anthropic",
            "content_blocks": [
                {"type": "thinking", "thinking": "原生思考", "signature": "signed-original"},
                *[
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": json.loads(call.arguments),
                    }
                    for call in calls
                ],
            ],
        },
    )
    runtime, seen = make_runtime(tmp_path, [turn])
    try:
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "必须保留的当前任务")
        with pytest.raises(ValueError, match="批次超过剩余预算.*未执行"):
            await runtime.wait(started["run_id"])
        assert not path.exists()
        assert [item["role"] for item in runtime.store.messages(session["id"])] == ["user"]
        assert len(seen) == 1 and seen[0][-1].content == "必须保留的当前任务"
        assert len(turn.tool_calls) == batch_size
        assert len(turn.provider_data["content_blocks"]) == batch_size + 1
        assert runtime.store.get("run", started["run_id"])["status"] == "failed"
        assert not any(event["type"] == "tool.started" for event in runtime.store.events())
    finally:
        await runtime.close()


async def test_tool_batch_budget_applies_to_remaining_calls_after_prior_effects(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    runtime, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(
                tool_calls=[
                    ToolCall("first", "fs_write", json.dumps({"path": str(first), "content": "ok"}))
                ]
            ),
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        f"extra{index}",
                        "fs_write",
                        json.dumps({"path": str(second), "content": "bad"}),
                    )
                    for index in range(2)
                ]
            ),
        ],
    )
    runtime.config.update({"limits": {"max_tool_steps": 2}})
    try:
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "有界操作")
        with pytest.raises(ValueError, match="剩余预算（1）"):
            await runtime.wait(started["run_id"])
        assert first.read_text() == "ok" and not second.exists()
        records = runtime.store.messages(session["id"])
        assert [record["role"] for record in records] == ["user", "assistant", "tool"]
        assert records[-1]["tool_call_id"] == "first"
        assert [message.role for message in runtime._history(session["id"])] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
    finally:
        await runtime.close()


def workflow_call() -> ModelTurn:
    return ModelTurn(
        tool_calls=[
            ToolCall(
                "delegate",
                "workflow_create",
                json.dumps(
                    {
                        "name": "协作任务",
                        "steps": [
                            {
                                "id": "work",
                                "title": "执行",
                                "prompt": "完成任务",
                                "role": "worker",
                                "depends_on": [],
                            }
                        ],
                    }
                ),
            )
        ]
    )


async def test_parent_waits_for_children_without_single_slot_deadlock(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"limits": {"max_parallel_agents": 1}})
    await runtime.reconfigure()
    parent_turns = [workflow_call(), ModelTurn(content="已安排")]
    child_context_ready, parent_waiting, child_streaming = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    allow_child_model, allow_child_result = asyncio.Event(), asyncio.Event()
    child_ids: list[str] = []
    real_wait = runtime.workflows.wait

    async def gate_child_context(session_id: str, messages: list[Message]) -> list[Message]:
        session = runtime.store.get("session", session_id)
        if session is not None and session.get("workflow_run_id"):
            child_context_ready.set()
            await allow_child_model.wait()
        return messages

    async def observe_child_wait(run_id: str) -> dict[str, Any]:
        child_ids.append(run_id)
        parent_waiting.set()
        return await real_wait(run_id)

    class ChildClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if any(
                message.role == "user" and message.content.startswith("工作流：")
                for message in messages
            ):
                assert runtime.capacity.locked()
                child_streaming.set()
                await allow_child_result.wait()
                yield StreamEvent("done", turn=ModelTurn(content="真实子任务结果"))
            else:
                async for event in super().stream(messages, tools):
                    yield event

    def factory(config: ModelConfig) -> ChildClient:
        return ChildClient(parent_turns, [])

    runtime.client_factory = factory  # type: ignore[assignment]
    runtime.context_hooks.append(gate_child_context)
    runtime.workflows.wait = observe_child_wait  # type: ignore[method-assign]
    try:
        session = runtime.new_session(mode="yolo")
        result = await runtime.start(session["id"], "拆分工作")
        parent = runtime.tasks[session["id"]]

        async def stage(event: asyncio.Event) -> None:
            observer = asyncio.create_task(event.wait())
            try:
                # Only a deadlock watchdog: progress is proved by the gates,
                # and its expiry cannot cancel the execution being inspected.
                await asyncio.wait(
                    [observer, parent], timeout=2, return_when=asyncio.FIRST_COMPLETED
                )
                if parent.done():
                    parent.result()
                assert event.is_set() and not parent.done(), {
                    "parent": runtime.store.get("run", result["run_id"]),
                    "children": [runtime.store.get("workflow_run", key) for key in child_ids],
                }
                if observer.done():
                    observer.result()
            finally:
                if not observer.done():
                    observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)

        await stage(child_context_ready)
        await stage(parent_waiting)
        assert len(child_ids) == 1
        assert not runtime.capacity.locked(), {
            "parent": runtime.store.get("run", result["run_id"]),
            "child": runtime.store.get("workflow_run", child_ids[0]),
        }
        allow_child_model.set()
        await stage(child_streaming)
        done, pending = await asyncio.wait([parent], timeout=0)
        assert not done and parent in pending and not parent.cancelling()
        assert runtime.store.get("workflow_run", child_ids[0])["status"] == "running"
        observing_child = asyncio.Event()

        async def observe_child() -> dict[str, Any]:
            observing_child.set()
            return await real_wait(child_ids[0])

        observer = asyncio.create_task(observe_child())
        try:
            await stage(observing_child)
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer
            assert not parent.cancelling()
            assert not runtime.workflows._tasks[child_ids[0]].cancelling()
        finally:
            if not observer.done():
                observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        allow_child_result.set()
        await asyncio.wait([parent], timeout=2)
        assert parent.done(), {
            "parent": runtime.store.get("run", result["run_id"]),
            "child": runtime.store.get("workflow_run", child_ids[0]),
        }
        output = parent.result()
        child = runtime.store.get("workflow_run", child_ids[0])
        assert child is not None
        assert child["parent_run_id"] == result["run_id"]
        assert child["status"] == "completed"
        assert "真实子任务结果" in output
        assert not parent_turns
        assert runtime.store.get("run", result["run_id"])["status"] == "completed"
    finally:
        allow_child_model.set()
        allow_child_result.set()
        await runtime.close()


async def test_parent_cancellation_propagates_to_spawned_workflow(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    entered = asyncio.Event()
    released = asyncio.Event()
    main_turns = [workflow_call(), ModelTurn(content="已安排")]

    class BlockingWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if any(
                message.role == "user" and message.content.startswith("工作流：")
                for message in messages
            ):
                entered.set()
                await released.wait()
                yield StreamEvent("done", turn=ModelTurn(content="不会抵达"))
            else:
                async for event in super().stream(messages, tools):
                    yield event

    def factory(config: ModelConfig) -> BlockingWorker:
        return BlockingWorker(main_turns, [])

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        session = runtime.new_session(mode="yolo")
        await runtime.start(session["id"], "协作")
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.stop(session["id"])
        assert runtime.store.list("workflow_run")[0]["status"] == "cancelled"
        assert not runtime.tasks
    finally:
        released.set()
        await runtime.close()


async def test_wait_reads_result_after_completed_task_is_unregistered(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="持久结果")])
    try:
        session = runtime.new_session()
        response = await runtime.start(session["id"], "开始")
        await asyncio.gather(*list(runtime.tasks.values()))
        assert not runtime.tasks
        assert await runtime.wait(response["run_id"]) == "持久结果"
    finally:
        await runtime.close()


async def test_queue_limit_rejects_before_storing_user_input(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="完成")])
    runtime.config.update({"limits": {"max_pending_runs": 1, "max_parallel_agents": 1}})
    await runtime.reconfigure()
    await runtime.capacity.acquire()
    try:
        first, second = runtime.new_session(), runtime.new_session()
        started = await runtime.start(first["id"], "已排队")
        with pytest.raises(ValueError, match="达到上限"):
            await runtime.start(second["id"], "不应保存")
        assert runtime.store.messages(second["id"]) == []
        assert runtime.store.get("session", second["id"])["status"] == "idle"
        runtime.capacity.release()
        assert await runtime.wait(started["run_id"]) == "完成"
    finally:
        await runtime.close()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_large_tool_result_is_packed_without_changing_the_journal(
    tmp_path: Path, newline: str
) -> None:
    path = tmp_path / "long.txt"
    original = "first-evidence" + newline + "中" * 6000 + newline + "last-evidence"
    path.write_bytes(original.encode("utf-8"))
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("c1", "fs_read", json.dumps({"path": str(path)}))]),
            ModelTurn(content="已参考头尾证据"),
        ],
    )
    runtime.config.update({"limits": {"max_context_chars": 4000}})
    try:
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "概括文件")
        assert await runtime.wait(started["run_id"]) == "已参考头尾证据"
        assert len(json.dumps([asdict(message) for message in seen[1]], ensure_ascii=False)) <= 4000
        packed = seen[1][-1].content
        assert "已节选" in packed and "first-evidence" in packed and "last-evidence" in packed
        saved = next(m for m in runtime.store.messages(session["id"]) if m["role"] == "tool")
        assert "已节选" not in saved["content"]
        assert original in json.loads(saved["content"])["content"]
    finally:
        await runtime.close()
