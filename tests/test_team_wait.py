from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.store import new_id, now


class ObservedCondition(asyncio.Condition):
    def __init__(self) -> None:
        super().__init__()
        self.waits: asyncio.Queue[None] = asyncio.Queue()
        self.before_wait: Callable[[], None] | None = None

    async def wait_for(self, predicate: Callable[[], bool]) -> bool:
        self.waits.put_nowait(None)
        if self.before_wait:
            callback, self.before_wait = self.before_wait, None
            callback()
        return await super().wait_for(predicate)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.bus.changed = ObservedCondition()
    try:
        yield runtime
    finally:
        await runtime.close()


def agent(runtime: Runtime, name: str, *, status: str = "idle") -> dict[str, Any]:
    session = runtime.new_session(name, "yolo")
    record = {
        "id": session["id"],
        "session_id": session["id"],
        "parent_id": runtime.main_session()["id"],
        "name": name,
        "role": "worker",
        "mode": "yolo",
        "status": status,
        "generation": 0,
        "created_at": now(),
    }
    return runtime.store.put("agent", record)


async def cursor(runtime: Runtime, *agents: dict[str, Any], caller: str | None = None) -> str:
    return str((await runtime.team.wait([item["id"] for item in agents], caller=caller))["cursor"])


async def finished(runtime: Runtime, target: dict[str, Any], text: str) -> None:
    runtime.team._finish(runtime.team.get(target["id"]), "completed", text, new_id())
    await runtime.bus.notify()


async def waiting(runtime: Runtime) -> None:
    condition = runtime.bus.changed
    assert isinstance(condition, ObservedCondition)
    await asyncio.wait_for(condition.waits.get(), 2)


def repack(token: str, change: Callable[[dict[str, Any]], None]) -> str:
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    change(payload)
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


async def test_legacy_snapshot_stays_complete_but_cursor_waits_for_unseen_target(
    runtime: Runtime,
) -> None:
    old = agent(runtime, "已完成")
    await finished(runtime, old, "旧结果完整保留" * 100)
    active = agent(runtime, "仍执行", status="running")
    snapshot = await runtime.team.wait([old["id"], active["id"]])
    assert (
        snapshot["agents"][0 if old["id"] < active["id"] else 1]["result"] == "旧结果完整保留" * 100
    )
    assert not snapshot["timed_out"] and "completed" not in snapshot
    task = asyncio.create_task(
        runtime.team.wait([old["id"], active["id"]], cursor=snapshot["cursor"], timeout=2)
    )
    await waiting(runtime)
    assert not task.done()
    await finished(runtime, active, "新的结果")
    response = await asyncio.wait_for(task, 2)
    assert [item["agent_id"] for item in response["completed"]] == [active["id"]]
    assert response["completed"][0]["result_preview"] == "新的结果"
    assert response["reason"] == "completed" and not response["has_more"]
    assert all("result" not in item for item in response["agents"])


async def test_two_observers_and_retry_receive_the_same_completion(runtime: Runtime) -> None:
    target = agent(runtime, "共同目标")
    initial = await cursor(runtime, target)
    tasks = [
        asyncio.create_task(runtime.team.wait([target["id"]], cursor=initial, timeout=2))
        for _ in range(2)
    ]
    await waiting(runtime)
    await waiting(runtime)
    await finished(runtime, target, "两位观察者都收到")
    first, second = await asyncio.gather(*tasks)
    assert first == second
    assert (await runtime.team.wait([target["id"]], cursor=initial)) == first
    unchanged = await runtime.team.wait([target["id"]], cursor=first["cursor"], timeout=0.001)
    assert unchanged["completed"] == [] and unchanged["timed_out"]
    assert unchanged["cursor"] == first["cursor"] and unchanged["reason"] == "timeout"


async def test_same_caller_cancellation_only_removes_its_own_graph_registration(
    runtime: Runtime,
) -> None:
    parent, left, right = (agent(runtime, name) for name in ("parent", "left", "right"))
    left_cursor = await cursor(runtime, left, caller=parent["id"])
    right_cursor = await cursor(runtime, right, caller=parent["id"])
    first = asyncio.create_task(
        runtime.team.wait([left["id"]], cursor=left_cursor, caller=parent["id"], timeout=2)
    )
    second = asyncio.create_task(
        runtime.team.wait([right["id"]], cursor=right_cursor, caller=parent["id"], timeout=2)
    )
    try:
        await waiting(runtime)
        await waiting(runtime)
        assert runtime.team._waiting[parent["id"]] == {left["id"], right["id"]}
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert runtime.team._waiting[parent["id"]] == {right["id"]}
        with pytest.raises(ValueError, match="循环"):
            await runtime.team.wait([parent["id"]], caller=right["id"])
        assert not second.done()
        await finished(runtime, right, "完成")
        assert (await second)["completed"][0]["agent_id"] == right["id"]
        assert not runtime.team._waiting
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_owner_input_precedes_completion_without_consuming_or_advancing(
    runtime: Runtime,
) -> None:
    target = agent(runtime, "child")
    caller = runtime.new_session(mode="yolo")["id"]
    initial = await cursor(runtime, target, caller=caller)
    await finished(runtime, target, "稍后再读")
    receipt, _ = runtime.conversation.enqueue(
        caller, "先处理用户补充", source="web", principal="owner", mode="yolo", max_pending=8
    )
    response = await runtime.team.wait([target["id"]], cursor=initial, caller=caller)
    assert response["reason"] == "new_input" and response["new_input"]
    assert response["completed"] == [] and response["cursor"] == initial
    assert runtime.store.pending_inputs(caller) == 1
    assert runtime.store.messages(caller) == []
    runtime.store.claim_inputs(caller, "test-run", limit=1)
    result = await runtime.team.wait([target["id"]], cursor=response["cursor"], caller=caller)
    assert result["completed"][0]["result_preview"] == "稍后再读"
    assert runtime.store.messages(caller)[0]["id"] == receipt["id"]


@pytest.mark.parametrize(
    ("principal", "internal", "reason"),
    [
        ("system:attention", True, "mailbox"),
        ("agent:peer", True, "mailbox"),
        ("external:test", False, "timeout"),
    ],
)
async def test_system_and_quarantined_input_are_never_owner_wakeups(
    runtime: Runtime, principal: str, internal: bool, reason: str
) -> None:
    target = agent(runtime, "child")
    caller = runtime.new_session(mode="yolo")["id"]
    initial = await cursor(runtime, target, caller=caller)
    runtime.conversation.enqueue(
        caller,
        "资料",
        source="test",
        principal=principal,
        mode="yolo",
        max_pending=8,
        internal=internal,
    )
    response = await runtime.team.wait([target["id"]], cursor=initial, caller=caller, timeout=0.001)
    assert response["reason"] == reason and response["cursor"] == initial
    assert response["completed"] == [] and runtime.store.messages(caller) == []


async def test_input_between_state_check_and_condition_wait_is_observed(runtime: Runtime) -> None:
    target = agent(runtime, "child")
    caller = runtime.new_session(mode="yolo")["id"]
    initial = await cursor(runtime, target, caller=caller)
    condition = runtime.bus.changed
    assert isinstance(condition, ObservedCondition)

    def arrive() -> None:
        runtime.conversation.enqueue(
            caller, "检查后到达", source="web", principal="owner", mode="yolo", max_pending=8
        )
        runtime.bus.revision += 1

    condition.before_wait = arrive
    response = await asyncio.wait_for(
        runtime.team.wait([target["id"]], cursor=initial, caller=caller, timeout=2), 2
    )
    assert response["reason"] == "new_input" and response["cursor"] == initial


async def test_input_committed_at_deadline_precedes_timeout(runtime: Runtime) -> None:
    target = agent(runtime, "deadline")
    caller = runtime.new_session(mode="yolo")["id"]
    initial = await cursor(runtime, target, caller=caller)
    condition = runtime.bus.changed
    assert isinstance(condition, ObservedCondition)

    def deadline() -> None:
        runtime.conversation.enqueue(
            caller, "期限同时到达", source="web", principal="owner", mode="yolo", max_pending=8
        )
        raise TimeoutError

    condition.before_wait = deadline
    response = await runtime.team.wait([target["id"]], cursor=initial, caller=caller)
    assert response["reason"] == "new_input" and not response["timed_out"]
    assert response["cursor"] == initial


async def test_target_scope_change_is_an_explicit_current_baseline(runtime: Runtime) -> None:
    first, added = agent(runtime, "first"), agent(runtime, "added")
    initial = await cursor(runtime, first)
    await finished(runtime, added, "加入前的历史")
    response = await runtime.team.wait([first["id"], added["id"]], cursor=initial)
    assert response["reason"] == "scope_changed" and response["completed"] == []
    assert response["baseline"] == [{"agent_id": added["id"], "completion_revision": 1}]
    removed = await runtime.team.wait([first["id"]], cursor=response["cursor"])
    assert removed["reason"] == "scope_changed" and "baseline" not in removed
    await finished(runtime, added, "移出后的历史")
    readded = await runtime.team.wait([added["id"]], cursor=removed["cursor"])
    assert readded["completed"] == []
    assert readded["baseline"][0]["completion_revision"] == 2


async def test_many_generations_page_without_skipping_or_invalid_json(runtime: Runtime) -> None:
    target = agent(runtime, "连续完成")
    initial = await cursor(runtime, target)
    for index in range(7):
        current = runtime.team.get(target["id"])
        current["generation"] = index + 1
        runtime.store.put("agent", current)
        await finished(runtime, target, f"第{index}代：" + '\n"\\沐璃' * 1000)
    runtime.config.update({"limits": {"max_output_chars": 1400}})
    seen = []
    token = initial
    while True:
        response = await runtime.team.wait([target["id"]], cursor=token)
        assert len(json.dumps(response, ensure_ascii=False)) <= 1400
        assert json.loads(json.dumps(response))["cursor"] == response["cursor"]
        seen.extend(response["completed"])
        token = response["cursor"]
        if not response["has_more"]:
            break
    assert [item["completion_revision"] for item in seen] == list(range(1, 8))
    assert [item["generation"] for item in seen] == list(range(1, 8))
    assert all(item["coalesced"] is False and item["count"] == 1 for item in seen)
    assert all(item["truncated"] for item in seen)
    assert len({item["history"]["event_id"] for item in seen}) == 7
    assert (await runtime.team.wait([target["id"]], cursor=initial))["completed"][0] == seen[0]


@pytest.mark.parametrize("timing", ["BEFORE", "AFTER"])
async def test_completion_result_report_and_revision_rollback_together(
    runtime: Runtime, timing: str
) -> None:
    target = agent(runtime, "原子提交")
    before = runtime.team.get(target["id"])
    revision = runtime.bus.revision
    runtime.store.db.execute(
        f"CREATE TEMP TRIGGER reject_completion {timing} INSERT ON events "
        "WHEN new.type='team.finished' BEGIN SELECT RAISE(ABORT,'reject'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="reject"):
        runtime.team._finish(
            target, "completed", "结果", "run-id", objects=[("team_report", {"id": "run-id"})]
        )
    assert runtime.team.get(target["id"]) == before
    assert runtime.store.list("team_report") == []
    assert runtime.store.team_completions(target["id"], 0) == []
    assert runtime.bus.revision == revision


async def test_preschedule_cancel_commits_once_and_does_not_reuse_previous_result(
    runtime: Runtime,
) -> None:
    target = agent(runtime, "没有执行")
    await finished(runtime, target, "上一代内容")
    initial = await cursor(runtime, target)
    target = runtime.team.get(target["id"])
    target["status"] = "queued"
    runtime.store.put("agent", target)
    await runtime.team.stop(target["id"])
    await runtime.team.stop(target["id"])
    response = await runtime.team.wait([target["id"]], cursor=initial)
    assert len(response["completed"]) == 1
    completion = response["completed"][0]
    assert completion["status"] == "cancelled" and completion["completion_revision"] == 2
    assert completion["generation"] == 0 and completion["run_id"] is None
    assert completion["result_preview"] == ""
    assert runtime.team.get(target["id"])["completion_revision"] == 2


async def test_real_spawn_cancel_before_first_schedule_persists_terminal(runtime: Runtime) -> None:
    parent = runtime.new_session(mode="yolo")
    target = await runtime.team.spawn("尚未调度", "工作", parent_id=parent["id"])
    result = await runtime.team.stop(target["id"])
    assert result["status"] == "cancelled" and result["completion_revision"] == 1
    events = runtime.store.team_completions(target["id"], 0)
    assert len(events) == 1 and events[0]["data"]["generation"] == 0
    assert events[0]["data"]["run_id"] is None and not runtime.team._active


@pytest.mark.parametrize("old_run_status", ["running", "completed"])
async def test_reopen_keeps_cursor_and_publishes_crash_terminal_without_old_evidence(
    tmp_path: Path, old_run_status: str
) -> None:
    first, _ = make_runtime(tmp_path, [])
    target = agent(first, "持久队友")
    initial = await cursor(first, target)
    run_id = new_id()
    first.store.put("run", {"id": run_id, "session_id": target["id"], "status": old_run_status})
    first.store.put(
        "agent",
        {**target, "generation": 1, "status": "running", "run_id": run_id, "result": "旧内容"},
    )
    instance = first.store.instance_id
    await first.close()
    second, seen = make_runtime(tmp_path, [])
    try:
        assert second.store.instance_id == instance
        response = await second.team.wait([target["id"]], cursor=initial)
        completion = response["completed"][0]
        assert completion["status"] == "interrupted" and completion["completion_revision"] == 1
        assert completion["run_id"] == (run_id if old_run_status == "running" else None)
        assert completion["result_preview"] == "" and not seen
        assert (
            second.store.team_completions(target["id"], 0)[0]["id"]
            == completion["history"]["event_id"]
        )
    finally:
        await second.close()


@pytest.mark.parametrize(
    "fault", ["version", "store", "caller", "future", "negative", "bool", "unknown", "malformed"]
)
async def test_untrusted_cursor_fields_fail_explicitly(runtime: Runtime, fault: str) -> None:
    target = agent(runtime, "校验")
    initial = await cursor(runtime, target)
    changes: dict[str, Callable[[dict[str, Any]], None]] = {
        "version": lambda item: item.update(v=99),
        "store": lambda item: item.update(store="another-store"),
        "caller": lambda item: item.update(caller="another-caller"),
        "future": lambda item: item["seen"].update({target["id"]: 1}),
        "negative": lambda item: item["seen"].update({target["id"]: -1}),
        "bool": lambda item: item["seen"].update({target["id"]: False}),
        "unknown": lambda item: item["seen"].update({"missing": 0}),
    }
    broken = "not!base64" if fault == "malformed" else repack(initial, changes[fault])
    with pytest.raises(ValueError, match="wait_cursor_(invalid|stale)"):
        await runtime.team.wait([target["id"]], cursor=broken)
    assert not runtime.team._waiting


async def test_missing_completion_prefix_is_not_silently_acknowledged(runtime: Runtime) -> None:
    target = agent(runtime, "缺失记录")
    initial = await cursor(runtime, target)
    await finished(runtime, target, "第一代")
    await finished(runtime, target, "第二代")
    with runtime.store.db:
        runtime.store.db.execute(
            "DELETE FROM events WHERE type='team.finished' AND json_extract(data,'$.completion_revision')=1"
        )
    with pytest.raises(ValueError, match="wait_cursor_stale.*缺口"):
        await runtime.team.wait([target["id"]], cursor=initial)


async def test_metadata_over_budget_fails_without_advancing(runtime: Runtime) -> None:
    targets = [agent(runtime, f"目标{index}" + "长" * 110) for index in range(8)]
    initial = await cursor(runtime, *targets)
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    with pytest.raises(ValueError, match="wait_budget_exceeded"):
        await runtime.team.wait([target["id"] for target in targets], cursor=initial)
    runtime.config.update({"limits": {"max_output_chars": 24000}})
    await finished(runtime, targets[0], "预算恢复后仍可读取")
    assert (await runtime.team.wait([target["id"] for target in targets], cursor=initial))[
        "completed"
    ][0]["agent_id"] == targets[0]["id"]


async def test_live_completion_is_visible_even_if_next_generation_already_running(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.bus.changed = ObservedCondition()
    parent = runtime.new_session(mode="yolo")
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            yield StreamEvent("done", turn=ModelTurn(content=f"generation-{index + 1}"))

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    original_wait = runtime.wait
    injected = False

    async def wait_and_inject(run_id: str) -> str:
        nonlocal injected
        result = await original_wait(run_id)
        if not injected:
            injected = True
            await runtime.team.send(target["id"], "下一代工作", wake=False)
        return result

    runtime.wait = wait_and_inject  # type: ignore[method-assign]
    try:
        target = await runtime.team.spawn("连续执行", "工作", parent_id=parent["id"])
        await asyncio.wait_for(started[0].wait(), 2)
        assert runtime.team.get(target["id"])["run_id"] == runtime.run_ids[target["id"]]
        baseline = (await runtime.team.wait([target["id"]], timeout=0.001))["cursor"]
        await waiting(runtime)  # Discard the completed baseline timeout's observation.
        observer = asyncio.create_task(
            runtime.team.wait([target["id"]], cursor=baseline, timeout=2)
        )
        await waiting(runtime)
        release[0].set()
        await asyncio.wait_for(started[1].wait(), 2)
        first = await observer
        assert first["completed"][0]["generation"] == 1
        assert first["completed"][0]["status"] == "completed"
        assert runtime.team.get(target["id"])["status"] == "running"
        release[1].set()
        second = await runtime.team.wait([target["id"]], cursor=first["cursor"], timeout=2)
        assert second["completed"][0]["generation"] == 2
        assert second["completed"][0]["result_preview"] == "generation-2"
        assert len(runtime.store.list("team_report")) == 2
    finally:
        await runtime.close()


async def test_completion_query_uses_revision_index_and_never_reads_message_history(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = agent(runtime, "索引")
    initial = await cursor(runtime, target)
    await finished(runtime, target, "有界读取")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("wait must not load message or provider history")

    monkeypatch.setattr(runtime.store, "messages", forbidden)
    response = await runtime.team.wait([target["id"]], cursor=initial)
    assert response["completed"][0]["result_preview"] == "有界读取"
    plan = runtime.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE type='team.finished' AND session_id=? "
        "AND json_extract(data,'$.completion_revision')>? ORDER BY json_extract(data,'$.completion_revision') LIMIT 1",
        (target["id"], 0),
    ).fetchall()
    assert "team_completion_revision" in str([tuple(row) for row in plan])


async def test_model_tool_exposes_and_forwards_optional_cursor(runtime: Runtime) -> None:
    target = agent(runtime, "模型工具")
    caller = runtime.new_session(mode="yolo")["id"]
    tool = next(item for item in runtime.team.tools(caller, "yolo") if item.name == "wait_agents")
    assert tool.parameters["properties"]["cursor"] == {"type": "string", "minLength": 1}
    first = await tool.handler({"targets": [target["id"]]})
    await finished(runtime, target, "工具收到的新结果")
    next_result = await tool.handler({"targets": [target["id"]], "cursor": first["cursor"]})
    assert next_result["completed"][0]["result_preview"] == "工具收到的新结果"


async def test_preview_retains_truncation_when_later_output_budget_grows(runtime: Runtime) -> None:
    target = agent(runtime, "完整性标志")
    initial = await cursor(runtime, target)
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    await finished(runtime, target, "超出当时预算" * 1000)
    runtime.config.update({"limits": {"max_output_chars": 24000}})
    response = await runtime.team.wait([target["id"]], cursor=initial)
    assert len(response["completed"][0]["result_preview"]) == 1000
    assert response["completed"][0]["truncated"]


async def test_multiple_targets_merge_completed_generations_by_event_order(
    runtime: Runtime,
) -> None:
    first, second = agent(runtime, "first"), agent(runtime, "second")
    initial = await cursor(runtime, first, second)
    for target, text in ((second, "b1"), (first, "a1"), (second, "b2"), (first, "a2")):
        await finished(runtime, target, text)
    response = await runtime.team.wait([first["id"], second["id"]], cursor=initial)
    assert [item["result_preview"] for item in response["completed"]] == ["b1", "a1", "b2", "a2"]
    identifiers = [item["history"]["event_id"] for item in response["completed"]]
    assert identifiers == sorted(identifiers)
    assert not response["has_more"]


async def test_runtime_large_initial_tool_snapshot_keeps_a_usable_cursor_and_pairs(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    target = agent(runtime, "长结果")
    original = '大"\\\n' * 1000
    await finished(runtime, target, original)
    runtime.config.update({"limits": {"max_output_chars": 1000, "max_parallel_agents": 1}})
    await runtime.reconfigure()
    # Python callers retain the complete old snapshot despite a smaller tool budget.
    snapshot = await runtime.team.wait([target["id"]])
    assert snapshot["agents"][0]["result"] == original

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            index = len(seen)
            if index == 1:
                call = ToolCall("initial", "wait_agents", json.dumps({"targets": [target["id"]]}))
            elif index == 2:
                text = messages[-1].content
                first = json.loads(text)
                assert len(text) <= 1000 and first["legacy_snapshot"] and first["truncated"]
                assert first["agents"][0]["truncated"] and "result_preview" in first["agents"][0]
                assert first["baseline"] == [{"agent_id": target["id"], "completion_revision": 1}]
                assert messages[-1].tool_call_id == "initial"
                assert messages[-2].tool_calls[0].id == "initial"
                assert messages[-2].provider_data == {"native": "initial"}
                await finished(runtime, target, "新的完整完成记录")
                call = ToolCall(
                    "incremental",
                    "wait_agents",
                    json.dumps({"targets": [target["id"]], "cursor": first["cursor"]}),
                )
            else:
                text = messages[-1].content
                result = json.loads(text)
                assert len(text) <= 1000 and result["completed"][0]["completion_revision"] == 2
                assert result["completed"][0]["result_preview"] == "新的完整完成记录"
                assert messages[-1].tool_call_id == "incremental"
                yield StreamEvent("done", turn=ModelTurn(content="验收完成"))
                return
            yield StreamEvent(
                "done", turn=ModelTurn(tool_calls=[call], provider_data={"native": call.id})
            )

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        parent = runtime.new_session(mode="yolo")
        started = await runtime.start(parent["id"], "等待新结果")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 3) == "验收完成"
        results = [item for item in runtime.store.messages(parent["id"]) if item["role"] == "tool"]
        assert len(results) == 2
        assert all("cursor" in json.loads(item["content"]) for item in results)
        assert not runtime.team._waiting
    finally:
        await runtime.close()
