from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_shell_continue_runtime import registered_shell_wait
from test_task_control import durable_state
from test_task_current_wait import member, read
from test_task_current_wait import wait_case as wait_case
from test_teammate_attachment_runtime import eventually

from muli.local_services import LocalServices
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import new_id, now


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("当前等待旅程只允许 ASGITransport 和本机受控 shell")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


@asynccontextmanager
async def live_facts(
    runtime: Runtime, task_id: str
) -> AsyncIterator[tuple[dict[str, Any], asyncio.Task[str], str, asyncio.Future[bool]]]:
    agent = member(runtime, task_id, "worker", status="running")
    run_id = new_id()
    runtime.store.put("agent", {**agent, "run_id": run_id})
    runtime.store.put("run", {"id": run_id, "session_id": agent["id"], "status": "running"})
    release = asyncio.Event()

    async def hold() -> str:
        await release.wait()
        return "test driver settled"

    driver = asyncio.create_task(hold())
    runtime.tasks[agent["id"]] = driver
    runtime.run_ids[agent["id"]] = run_id
    approval_id = new_id()
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    runtime.permissions.pending[approval_id] = future
    runtime.permissions._waiting[(agent["id"], run_id)] = {approval_id}
    runtime.store.put(
        "approval",
        {
            "id": approval_id,
            "session_id": agent["id"],
            "run_id": run_id,
            "tool": "notify",
            "arguments": {"title": "private-argument"},
            "reason": "private-reason",
            "risk": "write",
            "status": "pending",
            "created_at": now(),
        },
    )
    try:
        yield agent, driver, approval_id, future
    finally:
        runtime.tasks.pop(agent["id"], None)
        runtime.run_ids.pop(agent["id"], None)
        runtime.permissions._waiting.pop((agent["id"], run_id), None)
        runtime.permissions.pending.pop(approval_id, None)
        if not future.done():
            future.cancel()
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "done-future",
        "wrong-run",
        "wrong-current-run",
        "missing-record",
        "oversized-record",
        "cancel-driver",
        "done-driver",
        "queued-old-run",
        "waiter-budget",
        "missing-future",
        "stale-stored-only",
    ],
)
async def test_waiter_requires_the_actual_live_execution_and_exact_owner(
    wait_case: tuple[Runtime, LocalServices, str], change: str
) -> None:
    runtime, services, task_id = wait_case
    async with live_facts(runtime, task_id) as (agent, driver, approval_id, future):
        original = runtime.store.get("approval", approval_id)
        assert original is not None
        if change == "done-future":
            future.set_result(True)
        elif change == "wrong-run":
            runtime.store.put("approval", {**original, "run_id": "different-run"})
        elif change == "wrong-current-run":
            runtime.run_ids[agent["id"]] = "different-run"
        elif change == "missing-record":
            runtime.store.delete("approval", approval_id)
        elif change == "oversized-record":
            runtime.store.put("approval", {**original, "arguments": {"secret": "x" * 200000}})
        elif change == "cancel-driver":
            driver.cancel()
        elif change == "done-driver":
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        elif change == "queued-old-run":
            current = runtime.store.get("agent", agent["id"])
            assert current is not None
            runtime.store.put("agent", {**current, "status": "queued"})
            runtime.tasks.pop(agent["id"])
            runtime.run_ids.pop(agent["id"])
        elif change == "waiter-budget":
            owner = (agent["id"], original["run_id"])
            runtime.permissions._waiting[owner].add("missing-second-waiter")
            runtime.config.update({"limits": {"max_search_results": 2}})
            # One hop for the agent and one for main; the waiter cap is also two.
            runtime.permissions._waiting[owner].add("missing-third-waiter")
        elif change == "missing-future":
            runtime.permissions.pending.pop(approval_id)
        elif change == "stale-stored-only":
            runtime.permissions._waiting[(agent["id"], original["run_id"])].clear()
            runtime.permissions.pending.pop(approval_id)
        before, changes = durable_state(runtime.store), runtime.store.db.total_changes
        page = read(services, task_id)
        item = page["items"][0]
        execution = item["execution"]
        if change == "none":
            assert execution["state"] == "approval"
            assert execution["approvals"] == [{"id": approval_id, "tool": "notify"}]
        elif change in {"done-future", "stale-stored-only"}:
            assert execution["state"] == "running" and execution["approvals"] == []
        elif change == "queued-old-run":
            assert item["stored_status"] == "queued"
            assert execution["state"] == "none" and "run_id" not in execution
        else:
            assert execution == {"availability": "unavailable", "state": "unknown"}
            assert page["unavailable"] == 1
        assert "private-argument" not in json.dumps(page)
        assert "private-reason" not in json.dumps(page)
        assert durable_state(runtime.store) == before and runtime.store.db.total_changes == changes
        # Legacy callers retain the original enumeration contract.
        if change == "waiter-budget":
            assert runtime.permissions.pending_for(agent["id"], original["run_id"]) == (
                approval_id,
            )


async def test_real_workers_and_web_approval_read_again_without_replaying_work(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    app = create_app(runtime.config, runtime=runtime)
    services: LocalServices = app.state.local_services
    main = runtime.main_session()
    runtime.store.put("session", {**main, "mode": "ask"})
    task = await services.commitments.save(
        "两位队友", brief={"goal": "核查实际等待，不擅自决定", "constraints": []}
    )
    release = asyncio.Event()
    running = asyncio.Event()
    model_calls: list[str] = []

    class Client(FakeClient):
        def __init__(self, worker: bool) -> None:
            super().__init__([], [])
            self.worker = worker

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if not self.worker:
                turn = ModelTurn(content="已收到队友报告，任务尚未验收。")
            elif any("审批侧" in message.content for message in messages if message.role == "user"):
                model_calls.append("approval")
                turn = (
                    ModelTurn(content="工具已执行，任务尚未验收。")
                    if any(message.tool_call_id == "needs-owner" for message in messages)
                    else ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "needs-owner",
                                "notify",
                                json.dumps({"title": "真实一次", "content": "private-tool-body"}),
                            )
                        ]
                    )
                )
            else:
                model_calls.append("running")
                running.set()
                await release.wait()
                turn = ModelTurn(content="等待主人补充只是普通文字，任务尚未验收。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda profile: Client(profile.model == "worker")
    try:
        approval_agent = await runtime.team.spawn(
            "审批者", "审批侧：留通知前等主人决定", parent_id=main["id"], commitment_id=task["id"]
        )
        running_agent = await runtime.team.spawn(
            "执行者", "执行侧：保持执行", parent_id=main["id"], commitment_id=task["id"]
        )
        async with asyncio.timeout(5):
            await running.wait()
            await eventually(
                runtime,
                lambda: bool(
                    runtime.permissions.pending_for(
                        approval_agent["id"], runtime.run_ids.get(approval_agent["id"], "")
                    )
                ),
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            before, calls = durable_state(runtime.store), list(model_calls)
            url = f"/api/commitments/{task['id']}/inspection"
            response = await client.get(url, params={"section": "current_wait"})
            assert response.status_code == 200, response.text
            page = response.json()
            rows = {item["agent_id"]: item for item in page["items"]}
            assert rows[approval_agent["id"]]["execution"]["state"] == "approval"
            assert rows[running_agent["id"]]["execution"]["state"] == "running"
            assert "private-tool-body" not in response.text
            assert durable_state(runtime.store) == before and model_calls == calls
            approval_id = rows[approval_agent["id"]]["execution"]["approvals"][0]["id"]
            decision = await client.post(
                f"/api/approvals/{approval_id}", json={"decision": "approve"}
            )
            assert decision.status_code == 200 and decision.json() == {"resolved": True}
            await eventually(
                runtime,
                lambda: (
                    runtime.team.get(approval_agent["id"])["status"] == "completed"
                    and approval_agent["id"] not in runtime.tasks
                ),
            )
            confirmed = await client.get(url, params={"section": "current_wait"})
            assert confirmed.status_code == 200
            rows = {item["agent_id"]: item for item in confirmed.json()["items"]}
            assert rows[approval_agent["id"]]["execution"]["state"] == "none"
            assert rows[running_agent["id"]]["execution"]["state"] == "running"
            assert services.commitments.get(task["id"])["status"] != "done"
            notes = runtime.store.db.execute(
                "SELECT count(*) FROM objects WHERE kind='notification' "
                "AND json_extract(data,'$.title')='真实一次'"
            ).fetchone()[0]
            assert notes == 1
    finally:
        release.set()
        await runtime.close()


@pytest.mark.skipif(os.name != "posix", reason="复用真实 POSIX shell 续办旅程")
async def test_completed_worker_still_waits_for_real_shell_and_query_never_schedules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with registered_shell_wait(tmp_path) as state:
        runtime = state.runtime
        inspection = next(
            provider.__self__.task_inspection
            for provider, _, _, _ in runtime._tool_providers
            if isinstance(getattr(provider, "__self__", None), LocalServices)
        )
        record = runtime.team._shell_continuations[state.target]
        before, changes = durable_state(runtime.store), runtime.store.db.total_changes

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "wait inspection must not enter scheduling or unbounded execution gates"
            )

        with monkeypatch.context() as patch:
            patch.setattr(runtime.team, "guard_shell_continuation", forbidden)
            patch.setattr(runtime.team, "cancel_shell_continuation", forbidden)
            patch.setattr(runtime.team, "_schedule_shell_continuations", forbidden)
            patch.setattr(runtime.team, "start", forbidden)
            patch.setattr(runtime.store, "pending_inputs", forbidden)
            patch.setattr(runtime.task_control, "resolve_scope", forbidden)
            patch.setattr(runtime.task_control, "guard_execution", forbidden)
            page = inspection.read(state.task_id, section="current_wait")
            item = next(item for item in page["items"] if item["agent_id"] == state.target)
            assert item["stored_status"] == "completed"
            assert item["execution"]["state"] == "none"
            assert item["shell_continuation"]["state"] == "waiting_command"
            assert item["shell_continuation"]["command_id"] == state.command["command_id"]
            assert item["shell_continuation"]["automatic_resume_after_restart"] is False
            record.paired = False
            unpaired = inspection.read(state.task_id, section="current_wait")["items"][0]
            assert unpaired["shell_continuation"]["availability"] == "unavailable"
            record.paired = True
            record.revoked_reason = "test revoked"
            revoked = inspection.read(state.task_id, section="current_wait")["items"][0]
            assert revoked["shell_continuation"]["availability"] == "unavailable"
            record.revoked_reason = ""
            runtime._shell_generation += 1
            stale = inspection.read(state.task_id, section="current_wait")["items"][0]
            assert stale["shell_continuation"]["availability"] == "unavailable"
            runtime._shell_generation -= 1
        assert durable_state(runtime.store) == before and runtime.store.db.total_changes == changes
        assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
        assert runtime.team.get(state.target)["generation"] == 1
        assert record.finished is None and not record.reserved
        # A real independent input revokes the old registration through the existing path.
        await runtime.team.send(state.target, "主人补充资料，先核查旧操作", wake=False)
        pending = inspection.read(state.task_id, section="current_wait")["items"][0]
        assert pending["shell_continuation"]["state"] == "none"


@pytest.mark.skipif(os.name != "posix", reason="复用真实 POSIX shell 续办旅程")
async def test_real_finish_ready_and_bound_generation_are_distinct_current_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, settle, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def resumed(state: Any, messages: list[Message]) -> ModelTurn:
        started.set()
        await settle.wait()
        return ModelTurn(content="只核查原命令，尚未验收任务。")

    async with registered_shell_wait(tmp_path, resumed=resumed) as state:
        runtime = state.runtime
        inspection = next(
            provider.__self__.task_inspection
            for provider, _, _, _ in runtime._tool_providers
            if isinstance(getattr(provider, "__self__", None), LocalServices)
        )
        record = runtime.team._shell_continuations[state.target]

        def defer() -> None:
            finished.set()

        try:
            with monkeypatch.context() as patch:
                patch.setattr(runtime.team, "_schedule_shell_continuations", defer)
                state.release.write_text("real finish", encoding="utf-8")
                async with asyncio.timeout(5):
                    await finished.wait()
                fact = record.finished
                assert fact is not None and fact.state == "completed" and fact.exit_code == 7
                ready = inspection.read(state.task_id, section="current_wait")["items"][0]
                assert ready["shell_continuation"]["state"] == "ready"
                for invalid_state in ("unknown", "stopped", "timed_out"):
                    record.finished = replace(fact, state=invalid_state, exit_code=None)
                    unavailable = inspection.read(state.task_id, section="current_wait")["items"][0]
                    assert unavailable["shell_continuation"]["availability"] == "unavailable"
                record.finished = fact
            runtime.team._schedule_shell_continuations()
            async with asyncio.timeout(5):
                await started.wait()
            continuing = inspection.read(state.task_id, section="current_wait")["items"][0]
            assert continuing["execution"]["state"] == "running"
            assert continuing["shell_continuation"]["state"] == "continuing"
            assert runtime.team.get(state.target)["generation"] == 2
            assert state.counter.read_text(encoding="utf-8").splitlines() == ["once"]
        finally:
            settle.set()
