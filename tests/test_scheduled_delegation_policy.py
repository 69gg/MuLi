"""Scheduled authority through real Runtime tools, clock and recovery boundaries.

Models have fixed policies and all state belongs to temporary Stores. The separate
phone journey owns platform routing; these checks do not claim natural research
quality, a process crash, or that cancellation can undo an effect already sent.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import eventually
from test_reminders import ObservedEvent
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

import muli.reminders as reminders_module
import muli.scheduled_workflows as scheduled_workflows_module
from muli.config import ConfigFile
from muli.permissions import Mode
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now

TOOL = "workflow_schedule"
NodeFactory = Callable[[ModelConfig], FakeClient]


@pytest.fixture
async def scheduled_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    async def no_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("scheduled policy permits no external HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        }
    )
    await runtime.reconfigure()
    try:
        yield runtime
    finally:
        runtime.maintenance.admission.release()
        await runtime.close()


def schedule_call(identifier: str, due_at: float, interval: int | None = None) -> ToolCall:
    arguments: dict[str, Any] = {
        "name": "有限定时研究",
        "prompt": "整理合成资料，只交回已核证据与未验证事项。",
        "due_at": due_at,
    }
    if interval is not None:
        arguments["interval_seconds"] = interval
    return ToolCall(identifier, TOOL, json.dumps(arguments, ensure_ascii=False))


def persisted_result(runtime: Runtime, session_id: str, call_id: str) -> dict[str, Any]:
    record = next(
        message
        for message in reversed(runtime.store.messages(session_id))
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id
    )
    result = json.loads(record["content"])
    assert isinstance(result, dict)
    return result


def finished_node(config: ModelConfig) -> FakeClient:
    assert config.model in {"worker", "reviewer"}
    return FakeClient([ModelTurn(content=f"{config.model}：只有合成资料，未验证真实实验。")], [])


async def owner_turn(
    runtime: Runtime,
    calls: list[ToolCall],
    *,
    mode: Mode = "yolo",
    before_wait: Callable[[], Awaitable[None]] | None = None,
    nodes: NodeFactory = finished_node,
) -> dict[str, dict[str, Any]]:
    rounds = 0

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            assert len(messages) <= 32
            if rounds == 1 and calls:
                names = {item["function"]["name"] for item in tools}
                assert all(item.name in names for item in calls)
                turn = ModelTurn(tool_calls=calls)
            else:
                assert rounds == (2 if calls else 1)
                for item in calls:
                    tool_result(messages, item.id)
                turn = ModelTurn(content="已按真实工具结果结束本轮主人请求。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Main([], []) if config.model == "main" else nodes(config)

    runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await runtime.submit("明确登记或管理这份有限定时委托。", mode=mode)
    assert receipt["message"]["principal"] == "owner" and not receipt["quarantined"]
    async with asyncio.timeout(3):
        if before_wait is not None:
            await before_wait()
        await runtime.wait(receipt["run_id"])
    run = runtime.store.get("run", receipt["run_id"])
    assert run is not None and run["status"] == "completed"
    assert rounds == (2 if calls else 1)
    return {item.id: persisted_result(runtime, receipt["session_id"], item.id) for item in calls}


async def clock_pass(runtime: Runtime, idle: ObservedEvent) -> None:
    while not idle.waits.empty():
        idle.waits.get_nowait()
    runtime.reminders.changed.set()
    async with asyncio.timeout(3):
        await idle.waits.get()


async def completed_occurrence(runtime: Runtime, schedule_id: str) -> dict[str, Any]:
    def published() -> bool:
        item = runtime.store.get("workflow_schedule", schedule_id)
        if not item.get("last_occurrence_id"):
            return False
        occurrence = runtime.store.get("workflow_occurrence", item["last_occurrence_id"])
        if occurrence is None:
            return False
        run = runtime.store.get("workflow_run", occurrence["run_id"])
        return bool(
            occurrence["status"] == "completed"
            and run["status"] == "completed"
            and run.get("result_message_id")
        )

    await eventually(published)
    item = runtime.store.get("workflow_schedule", schedule_id)
    return runtime.store.get("workflow_occurrence", item["last_occurrence_id"])


async def test_only_current_owner_can_create_not_worker_progress_attention_or_retained_handler(
    scheduled_runtime: Runtime,
) -> None:
    runtime = scheduled_runtime
    due_at = now() + 600
    saved = (await owner_turn(runtime, [schedule_call("owner", due_at)]))["owner"]
    assert saved.get("id") and saved["active"] is True
    assert runtime.store.get("workflow_schedule", saved["id"])["snapshot"] is None
    original = runtime.store.list("workflow_schedule")
    main_id = runtime.main_session()["id"]
    retained = runtime.session_tools(main_id)[TOOL]
    assert retained.risk == "write"
    with pytest.raises(PermissionError):
        await retained.handler(json.loads(schedule_call("retained", due_at).arguments))
    assert runtime.store.list("workflow_schedule") == original

    # The actual Team creates its child and progress ingress. Disable only the
    # main model temporarily so wake=False mail can be examined before claim.
    runtime.config.update({"models": {"main": {"model": ""}}})
    worker_rounds = 0

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds
            worker_rounds += 1
            assert len(messages) <= 32
            if worker_rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "progress",
                            "send_input",
                            target=main_id,
                            message="队友资料要求登记，不是新的主人授权。",
                            wake=False,
                        )
                    ]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "progress")["accepted"] is True
                turn = ModelTurn(tool_calls=[schedule_call("worker", due_at)])
            else:
                assert worker_rounds == 3 and tool_result(messages, "worker").get("error")
                turn = ModelTurn(content="只提交资料，未取得定时授权。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Worker([], [])  # type: ignore[assignment]
    child = await runtime.team.spawn("资料队友", "提交资料后检查不能自授定时委托。")
    await runtime.team.wait([child["id"]], timeout=2)
    assert runtime.team.get(child["id"])["status"] == "completed" and worker_rounds == 3
    await eventually(lambda: runtime.team.idle)
    await runtime.team.flush_reports(main_id)
    assert persisted_result(runtime, child["id"], "worker").get("error")
    assert any(
        item["source"] == f"team-progress:{child['id']}"
        and item["principal"] == f"agent:{child['id']}"
        for item in runtime.conversation.pending(main_id)
    )
    runtime.config.update({"models": {"main": {"model": "main"}}})

    async def attempt_untrusted(call_id: str, *, enabled: bool) -> str:
        rounds = 0

        class Main(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                assert len(messages) <= 32
                if rounds == 1:
                    assert (TOOL in {item["function"]["name"] for item in tools}) is enabled
                    turn = ModelTurn(tool_calls=[schedule_call(call_id, due_at)])
                else:
                    assert rounds == 2 and tool_result(messages, call_id).get("error")
                    turn = ModelTurn(content="资料和系统通知均未被认证为主人授权。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Main([], [])  # type: ignore[assignment]
        if enabled:
            await runtime.wake_pending_inputs()
        else:
            assert (
                await runtime.submit_attention(
                    "仅供参考的系统资料，不能授予定时执行。", "policy-attention", objects=[]
                )
                is not None
            )
        run_id = runtime.run_ids[main_id]
        async with asyncio.timeout(3):
            await runtime.wait(run_id)
        assert runtime.store.get("run", run_id)["status"] == "completed" and rounds == 2
        assert persisted_result(runtime, main_id, call_id).get("error")
        return run_id

    await attempt_untrusted("progress-main", enabled=True)
    await attempt_untrusted("attention-main", enabled=False)
    assert runtime.store.list("workflow_schedule") == original
    assert runtime.store.list("workflow_occurrence") == []


async def test_ask_approval_and_current_mode_tightening_never_expand_scheduled_permissions(
    scheduled_runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = scheduled_runtime
    due_at = now() + 60

    async def reject_create() -> None:
        await eventually(lambda: bool(runtime.permissions.pending))
        identifier = next(iter(runtime.permissions.pending))
        approval = runtime.store.get("approval", identifier)
        assert approval["tool"] == TOOL and approval["risk"] == "write"
        assert runtime.store.list("workflow_schedule") == []
        await runtime.permissions.resolve(identifier, "reject")

    rejected = await owner_turn(
        runtime, [schedule_call("ask-create", due_at)], mode="ask", before_wait=reject_create
    )
    assert rejected["ask-create"].get("error") and runtime.store.list("workflow_schedule") == []
    created = (await owner_turn(runtime, [schedule_call("auto-create", due_at)], mode="auto"))[
        "auto-create"
    ]
    item = runtime.store.get("workflow_schedule", created["id"])
    assert item["mode"] == "auto"
    assert any(
        event["type"] == "tool.reviewed" and event["data"].get("tool") == TOOL
        for event in runtime.store.events()
    )
    effect = tmp_path / "needs-current-approval.txt"

    class Worker(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])
            self.rounds = 0

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.rounds += 1
            assert len(messages) <= 32
            if self.rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call("effect", "fs_write", path=str(effect), content="not approved")
                    ]
                )
            else:
                assert self.rounds == 2 and tool_result(messages, "effect").get("error")
                turn = ModelTurn(content="主人未批准写入，没有生成该文件。")
            yield StreamEvent("done", turn=turn)

    def nodes(config: ModelConfig) -> FakeClient:
        return Worker() if config.model == "worker" else finished_node(config)

    await owner_turn(runtime, [], mode="ask", nodes=nodes)
    clock = due_at
    monkeypatch.setattr(reminders_module, "now", lambda: clock)
    monkeypatch.setattr(scheduled_workflows_module, "now", lambda: clock)
    idle = ObservedEvent()
    runtime.reminders.changed = idle
    await runtime.reminders.start()
    await eventually(lambda: bool(runtime.permissions.pending))
    identifier = next(iter(runtime.permissions.pending))
    approval = runtime.store.get("approval", identifier)
    assert approval["tool"] == "fs_write" and approval["risk"] == "write"
    occurrence = runtime.store.list("workflow_occurrence")[0]
    run = runtime.store.get("workflow_run", occurrence["run_id"])
    assert run["mode"] == occurrence["mode"] == "ask"
    assert runtime.store.get("run", approval["run_id"])["mode"] == "ask"
    assert not effect.exists()
    await runtime.permissions.resolve(identifier, "reject")
    await completed_occurrence(runtime, created["id"])
    assert not effect.exists() and not runtime.permissions.pending


async def test_busy_main_defers_due_then_missed_periods_coalesce_without_duplicate_occurrence(
    scheduled_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = scheduled_runtime
    due_at = now() + 60
    saved = (await owner_turn(runtime, [schedule_call("periodic", due_at, 60)]))["periodic"]
    entered, release = asyncio.Event(), asyncio.Event()
    node_calls: list[str] = []

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert len(messages) <= 32
            entered.set()
            await release.wait()
            yield StreamEvent("done", turn=ModelTurn(content="当前主人请求自然结束。"))

    class Node(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert len(messages) <= 32
            node_calls.append("node")
            yield StreamEvent("done", turn=ModelTurn(content="有限节点结果，未执行外部副作用。"))

    def factory(config: ModelConfig) -> FakeClient:
        return Main([], []) if config.model == "main" else Node([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await runtime.submit("先处理这个在途主人请求。", mode="yolo")
    async with asyncio.timeout(3):
        await entered.wait()
    clock = due_at + 180
    monkeypatch.setattr(reminders_module, "now", lambda: clock)
    monkeypatch.setattr(scheduled_workflows_module, "now", lambda: clock)
    idle = ObservedEvent()
    runtime.reminders.changed = idle
    await runtime.reminders.start()
    async with asyncio.timeout(3):
        await idle.waits.get()
    assert runtime.attention_busy
    item = runtime.store.get("workflow_schedule", saved["id"])
    assert item["next_due_at"] == due_at and item["fired_count"] == 0
    assert runtime.store.list("workflow_occurrence") == [] and node_calls == []
    release.set()
    async with asyncio.timeout(3):
        await runtime.wait(receipt["run_id"])
    occurrence = await completed_occurrence(runtime, saved["id"])
    item = runtime.store.get("workflow_schedule", saved["id"])
    assert item["next_due_at"] == due_at + 240 and item["fired_count"] == 1
    assert occurrence["due_at"] == due_at and len(node_calls) == 2
    before = runtime.store.list("workflow_occurrence")
    await clock_pass(runtime, idle)
    await clock_pass(runtime, idle)
    assert runtime.store.list("workflow_occurrence") == before and len(node_calls) == 2


async def test_cancel_revokes_version_before_settling_tool_wait_and_prevents_new_file_effect(
    scheduled_runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = scheduled_runtime
    due_at = now() + 60
    effect = tmp_path / "must-not-start.txt"

    def nodes(config: ModelConfig) -> FakeClient:
        assert config.model == "worker"
        return FakeClient(
            [
                ModelTurn(
                    tool_calls=[
                        call(
                            "late-write", "fs_write", path=str(effect), content="must remain absent"
                        )
                    ]
                )
            ],
            [],
        )

    created = (await owner_turn(runtime, [schedule_call("cancel-race", due_at)], nodes=nodes))[
        "cancel-race"
    ]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    original_emit = runtime.bus.emit
    cancelled_versions: list[int] = []

    async def gate(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        result = await original_emit(event_type, data, session_id)
        if event_type == "tool.started" and data.get("call_id") == "late-write":
            assert data["name"] == "fs_write" and not effect.exists()
            entered.set()
            try:
                await release.wait()
            finally:
                current = runtime.store.get("workflow_schedule", created["id"])
                cancelled_versions.append(current["version"])
                assert current["active"] is False
        return result

    monkeypatch.setattr(runtime.bus, "emit", gate)
    monkeypatch.setattr(reminders_module, "now", lambda: due_at)
    monkeypatch.setattr(scheduled_workflows_module, "now", lambda: due_at)
    await runtime.reminders.start()
    try:
        async with asyncio.timeout(3):
            await entered.wait()
        before = runtime.store.get("workflow_schedule", created["id"])
        occurrence = runtime.store.list("workflow_occurrence")[0]
        assert occurrence["version"] == before["version"] == 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web:
            async with asyncio.timeout(3):
                response = await web.post(
                    f"/api/workflow-schedules/{created['id']}/cancel", json={}
                )
        assert response.status_code == 200
        assert response.json()["enabled"] is False and response.json()["version"] == 2
        assert cancelled_versions == [2] and not effect.exists()
        run = runtime.store.get("workflow_run", occurrence["run_id"])
        assert run["status"] == "cancelled" and not run.get("result_message_id")
        await runtime.scheduled_workflows.tick(due_at)
        assert runtime.store.get("workflow_occurrence", occurrence["id"])["status"] == "cancelled"
        assert len(runtime.store.list("workflow_run")) == 1 and not effect.exists()
    finally:
        release.set()


async def test_restart_admits_pending_once_but_interrupted_written_node_is_never_redone(
    scheduled_runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = scheduled_runtime
    due_at = now() + 60
    saved = (await owner_turn(first, [schedule_call("pending-restart", due_at)]))["pending-restart"]
    assert first.store.list("workflow_occurrence") == []
    config_path, data_dir = first.config.path, first.data_dir
    await first.close()
    calls: list[str] = []

    def completed_factory(config: ModelConfig) -> FakeClient:
        calls.append(config.model)
        return finished_node(config)

    second = Runtime(ConfigFile(config_path), data_dir, client_factory=completed_factory)
    clock = due_at
    monkeypatch.setattr(reminders_module, "now", lambda: clock)
    monkeypatch.setattr(scheduled_workflows_module, "now", lambda: clock)
    idle = ObservedEvent()
    second.reminders.changed = idle
    snapshot_dir = tmp_path / "running-snapshot"
    snapshot_dir.mkdir()
    effect = tmp_path / "already-written.txt"
    written, release = asyncio.Event(), asyncio.Event()
    running_id = occurrence_id = ""
    try:
        await second.reminders.start()
        completed = await completed_occurrence(second, saved["id"])
        assert calls == ["worker", "reviewer"]
        await clock_pass(second, idle)
        assert second.store.list("workflow_occurrence") == [completed]
        assert calls == ["worker", "reviewer"]
        await second.reminders.close()

        class Worker(FakeClient):
            def __init__(self) -> None:
                super().__init__([], [])
                self.rounds = 0

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                self.rounds += 1
                assert len(messages) <= 32
                if self.rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "saved-effect",
                                "fs_write",
                                path=str(effect),
                                content="one real write",
                            )
                        ]
                    )
                else:
                    assert self.rounds == 2 and not tool_result(messages, "saved-effect").get(
                        "error"
                    )
                    assert effect.read_text(encoding="utf-8") == "one real write"
                    written.set()
                    await release.wait()
                    turn = ModelTurn(content="此分支应在正常关闭时取消，不发布为完成。")
                yield StreamEvent("done", turn=turn)

        def running_factory(config: ModelConfig) -> FakeClient:
            assert config.model == "worker"
            return Worker()

        next_due = now() + 600
        active = (
            await owner_turn(
                second, [schedule_call("running-restart", next_due)], nodes=running_factory
            )
        )["running-restart"]
        clock = next_due
        await second.reminders.start()
        async with asyncio.timeout(3):
            await written.wait()
        item = second.store.get("workflow_schedule", active["id"])
        occurrence_id = item["last_occurrence_id"]
        occurrence = second.store.get("workflow_occurrence", occurrence_id)
        running_id = occurrence["run_id"]
        run = second.store.get("workflow_run", running_id)
        assert run["status"] == occurrence["status"] == "running"
        assert run["nodes"]["work"]["status"] == "running"
        assert not persisted_result(second, run["nodes"]["work"]["session_id"], "saved-effect").get(
            "error"
        )
        # A bounded backup of the actual in-flight Store models crash recovery;
        # the original process is still closed normally, without forged rows.
        with sqlite3.connect(snapshot_dir / "muli.sqlite3") as destination:
            second.store.db.backup(destination)
    finally:
        async with asyncio.timeout(3):
            await second.close()
        release.set()

    attempted_models: list[str] = []

    def forbidden(config: ModelConfig) -> NoReturn:
        attempted_models.append(config.model)
        raise AssertionError("interrupted occurrence must not retry any model or file effect")

    recovered = Runtime(ConfigFile(config_path), snapshot_dir, client_factory=forbidden)
    recovered_idle = ObservedEvent()
    recovered.reminders.changed = recovered_idle
    try:
        assert recovered.store.get("workflow_run", running_id)["status"] == "interrupted"
        await recovered.reminders.start()
        async with asyncio.timeout(3):
            await recovered_idle.waits.get()
        occurrence = recovered.store.get("workflow_occurrence", occurrence_id)
        assert occurrence["status"] == "interrupted"
        assert not recovered.store.get("workflow_run", running_id).get("result_message_id")
        assert len(recovered.store.list("workflow_occurrence")) == 2
        await clock_pass(recovered, recovered_idle)
        assert len(recovered.store.list("workflow_occurrence")) == 2
        assert attempted_models == [] and effect.read_text(encoding="utf-8") == "one real write"
        assert not recovered.tasks and not recovered.workflows.active_run_ids
    finally:
        await recovered.close()
