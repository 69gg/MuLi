"""Host admission of parent progress through real Runtime worker executions.

The probe is a real write tool: it retains the actual prepared request, driver
Task and execution ContextVars while invoking the current Team tool table. It
does not manufacture agents, runs, input receipts or execution snapshots. The
separate channel journey covers SDK input and the frozen phone destination.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import command
from test_teammate_attachment_runtime import tool_result

from muli.local_services import LocalServices
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool, parameters

Probe = Callable[[str, dict[str, Tool]], Awaitable[None]]


@pytest.fixture
async def progress_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, LocalServices]]:
    async def no_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("team progress policy must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"enabled": False, "auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {
                "max_output_chars": 6000,
                "max_context_chars": 16000,
                "max_pending_runs": 3,
            },
            # Keep parent input pending without replacing the real wake method.
            "models": {
                "main": {"model": ""},
                "worker": {"model": "worker", "base_url": "https://unused.invalid/v1"},
            },
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    services: LocalServices = app.state.local_services
    main = runtime.main_session()
    try:
        # Store construction already created this persistent main in auto mode;
        # changing default_mode only affects future sessions. Use the real
        # management endpoint before spawning a child, without starting main.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as web:
            response = await web.patch(f"/api/sessions/{main['id']}", json={"mode": "yolo"})
            assert response.status_code == 200
            assert response.json()["mode"] == "yolo"
        assert runtime.main_session()["mode"] == "yolo"
        yield runtime, services
    finally:
        await runtime.close()


def install_probe(runtime: Runtime, probe: Probe) -> list[Exception]:
    errors: list[Exception] = []

    async def handler(args: dict[str, Any]) -> dict[str, bool]:
        caller = runtime.tool_session_id
        assert caller is not None
        assert runtime._tool_execution.get() == (caller, runtime.run_ids[caller])
        assert runtime.tasks[caller] is asyncio.current_task()
        assert runtime.request_context(caller) is not None
        assert runtime.team.get(caller)["mode"] == runtime.main_session()["mode"] == "yolo"
        table = {tool.name: tool for tool in runtime.team.tools(caller, "yolo")}
        try:
            await probe(caller, table)
        except Exception as exc:
            errors.append(exc)
            raise
        return {"checked": True}

    runtime.tools["progress_policy_probe"] = Tool(
        "progress_policy_probe", "真实执行上下文内的阶段消息边界", parameters({}), handler, "write"
    )

    class Worker(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])
            self.rounds = 0

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.rounds += 1
            if self.rounds == 1:
                assert any(tool["function"]["name"] == "send_input" for tool in tools)
                turn = ModelTurn(tool_calls=[ToolCall("policy", "progress_policy_probe", "{}")])
            else:
                assert self.rounds == 2
                assert tool_result(messages, "policy") == {"checked": True}
                turn = ModelTurn(content="独立阶段消息边界已核查。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model == "worker"
        return Worker()

    runtime.client_factory = factory
    return errors


async def finish(runtime: Runtime, identifier: str, errors: list[Exception]) -> dict[str, Any]:
    await runtime.team.wait([identifier], timeout=2)
    if errors:
        raise errors[0]
    agent = runtime.team.get(identifier)
    if agent["status"] not in {"completed", "paused"}:
        run = runtime.store.get("run", agent.get("run_id", "")) or {}
        statuses = {
            "queued",
            "running",
            "completed",
            "failed",
            "paused",
            "cancelled",
            "interrupted",
        }
        last_tool = "none"
        for message in runtime.store.messages(identifier, limit=4):
            for call in message.get("tool_calls", []):
                last_tool = (
                    "progress_policy_probe"
                    if call.get("name") == "progress_policy_probe"
                    else "other"
                )
        diagnostic = json.dumps(
            {
                "agent_status": agent["status"] if agent["status"] in statuses else "other",
                "run_status": run.get("status") if run.get("status") in statuses else "other",
                "pending_approvals": len(runtime.permissions.pending),
                "last_tool": last_tool,
            },
            separators=(",", ":"),
        )
        assert len(diagnostic.encode("utf-8")) <= 1024
        raise AssertionError(diagnostic)
    return agent


def state(runtime: Runtime) -> tuple[object, ...]:
    """Small fixture-wide oracle, including object writes and message events."""
    return (
        [tuple(row) for row in runtime.store.db.execute("SELECT * FROM ingress ORDER BY seq")],
        [
            tuple(row)
            for row in runtime.store.db.execute(
                "SELECT kind,id,data FROM objects WHERE kind IN ('agent','team_report') "
                "ORDER BY kind,id"
            )
        ],
        runtime.store.events(limit=1000),
    )


async def rejected(runtime: Runtime, tool: Tool, arguments: dict[str, Any]) -> None:
    before = state(runtime)
    with pytest.raises((PermissionError, ValueError)):
        await tool.handler(arguments)
    assert state(runtime) == before


def arguments(runtime: Runtime, message: str = "仍需主人确认，不是新授权。") -> dict[str, Any]:
    return {"target": runtime.main_session()["id"], "message": message, "wake": False}


async def positive(runtime: Runtime, caller: str, table: dict[str, Tool]) -> dict[str, Any]:
    agent = runtime.team.get(caller)
    assert agent["status"] == "running"
    assert not any(
        report["agent_id"] == caller and report["id"] == agent["run_id"]
        for report in runtime.store.list("team_report")
    )
    previous = runtime.store.pending_inputs(runtime.main_session()["id"])
    result = await table["send_input"].handler(arguments(runtime))
    assert result["accepted"] is True and result["duplicate"] is False
    pending = runtime.conversation.pending(runtime.main_session()["id"])
    item = next(item for item in pending if item["id"] == result["receipt_id"])
    assert len(pending) == previous + 1
    assert item["source"] == f"team-progress:{caller}"
    assert item["principal"] == f"agent:{caller}"
    assert "不是新的用户授权" in item["content"]
    assert runtime.team.get(caller) == agent
    return result


async def test_current_progress_is_mailbox_only_and_retained_handler_has_no_authority(
    progress_runtime: tuple[Runtime, LocalServices], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = progress_runtime
    retained: list[Tool] = []
    wakes: list[None] = []
    original = runtime._wake_main

    def wake() -> None:
        wakes.append(None)
        original()

    async def probe(caller: str, table: dict[str, Tool]) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "_wake_main", wake)
            await positive(runtime, caller, table)
            assert wakes == []
        # ContextVars copy into a child Task, but the actual Runtime driver
        # identity does not. A retained closure cannot delegate that authority.
        await asyncio.create_task(rejected(runtime, table["send_input"], arguments(runtime)))
        await rejected(runtime, table["send_message"], arguments(runtime))
        before = state(runtime)
        with pytest.raises(ValueError):
            await runtime.team.send(
                runtime.main_session()["id"], "不能借公开send越界", sender=caller
            )
        assert state(runtime) == before
        retained.append(table["send_input"])

    errors = install_probe(runtime, probe)
    agent = await runtime.team.spawn("直接队友", "核查阶段消息边界")
    await finish(runtime, agent["id"], errors)
    assert len(retained) == 1
    assert runtime.tool_session_id is None and runtime._tool_execution.get() is None
    await rejected(runtime, retained[0], arguments(runtime))


async def test_old_generation_handler_cannot_send_inside_the_new_real_driver(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, _ = progress_runtime
    retained: list[Tool] = []
    executions: list[tuple[str, int]] = []

    async def probe(caller: str, table: dict[str, Tool]) -> None:
        agent = runtime.team.get(caller)
        executions.append((runtime.run_ids[caller], agent["generation"]))
        await positive(runtime, caller, table)
        if retained:
            await rejected(runtime, retained[0], arguments(runtime, "旧代handler不得再次发言"))
        else:
            retained.append(table["send_input"])

    errors = install_probe(runtime, probe)
    agent = await runtime.team.spawn("跨代队友", "第一代核查")
    await finish(runtime, agent["id"], errors)
    await runtime.team.resume(agent["id"])
    await finish(runtime, agent["id"], errors)
    assert len(executions) == 2
    assert executions[0][0] != executions[1][0]
    assert [generation for _, generation in executions] == [1, 2]


async def test_grandchild_cannot_address_main_or_borrow_its_parents_tool_caller(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, _ = progress_runtime
    parent_id = ""
    checked: list[str] = []

    async def probe(caller: str, table: dict[str, Tool]) -> None:
        if not parent_id:
            await positive(runtime, caller, table)
        else:
            assert runtime.team.get(caller)["parent_id"] == parent_id
            # Legacy child-to-direct-parent mail remains available.
            result = await table["send_input"].handler(
                {"target": parent_id, "message": "给真实直接父队友的资料", "wake": False}
            )
            assert result["accepted"] is True
            await rejected(runtime, table["send_input"], arguments(runtime))
            parent_table = {tool.name: tool for tool in runtime.team.tools(parent_id, "yolo")}
            await rejected(runtime, parent_table["send_input"], arguments(runtime))
            legacy = runtime.new_session("旧本地会话", mode="yolo")
            legacy_table = {tool.name: tool for tool in runtime.team.tools(legacy["id"], "yolo")}
            await rejected(runtime, legacy_table["send_input"], arguments(runtime))
        checked.append(caller)

    errors = install_probe(runtime, probe)
    parent = await runtime.team.spawn("直接父队友", "先核准直接子队友正控")
    await finish(runtime, parent["id"], errors)
    parent_id = parent["id"]
    child = await runtime.team.spawn("真实孙代", "仅向直接父队友报告", parent_id=parent_id)
    await finish(runtime, child["id"], errors)
    assert checked == [parent_id, child["id"]]


async def test_current_worker_cannot_cross_stop_close_maintenance_or_owner_pause(
    progress_runtime: tuple[Runtime, LocalServices], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, services = progress_runtime
    task = await services.commitments.save(
        "受控阶段任务", brief={"goal": "核查资料", "constraints": ["先询问，不发布"]}
    )
    checked: list[str] = []

    async def probe(caller: str, table: dict[str, Tool]) -> None:
        await positive(runtime, caller, table)
        tool = table["send_input"]
        runtime.maintenance.admission.pause()
        try:
            await rejected(runtime, tool, arguments(runtime))
        finally:
            runtime.maintenance.admission.release()
        with runtime._stopping_session(caller):
            await rejected(runtime, tool, arguments(runtime))
        for owner, field in ((runtime, "_closing"), (runtime.team, "_closed")):
            with monkeypatch.context() as patch:
                patch.setattr(owner, field, True)
                await rejected(runtime, tool, arguments(runtime))
        # The authentic owner-control path changes the real task epoch; no
        # injected cache/snapshot is used to simulate a paused generation.
        command(runtime, services, task["id"], "pause", 0, "policy-pause")
        await rejected(runtime, tool, arguments(runtime))
        checked.append(caller)

    errors = install_probe(runtime, probe)
    agent = await runtime.team.spawn("受控队友", "检查暂停门", commitment_id=task["id"])
    terminal = await finish(runtime, agent["id"], errors)
    assert checked == [agent["id"]]
    assert terminal["status"] == "paused"


async def test_bad_attachment_and_full_parent_inbox_leave_no_partial_progress(
    progress_runtime: tuple[Runtime, LocalServices],
) -> None:
    runtime, _ = progress_runtime
    checked: list[str] = []

    async def probe(caller: str, table: dict[str, Tool]) -> None:
        await positive(runtime, caller, table)
        tool = table["send_input"]
        for identifiers in (["not-an-id"], ["f" * 32], ["f" * 32, "f" * 32]):
            await rejected(runtime, tool, {**arguments(runtime), "attachment_ids": identifiers})
        main = runtime.main_session()
        limit = runtime.config.settings.limits.max_pending_runs
        # Fill the real mailbox via Conversation; never manufacture receipts.
        for index in range(limit - runtime.store.pending_inputs(main["id"])):
            runtime.conversation.enqueue(
                main["id"],
                f"真实入箱占位{index}",
                source="web",
                principal="owner",
                mode="yolo",
                max_pending=limit,
            )
        assert runtime.store.pending_inputs(main["id"]) == limit
        await rejected(runtime, tool, arguments(runtime, "满邮箱不应留下半条阶段消息"))
        checked.append(caller)

    errors = install_probe(runtime, probe)
    agent = await runtime.team.spawn("容量队友", "检查阶段消息原子拒绝")
    await finish(runtime, agent["id"], errors)
    assert checked == [agent["id"]]
