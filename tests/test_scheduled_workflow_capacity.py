"""A persisted terminal run still owns admission capacity until its finalizer exits."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import make_runtime
from test_workflows import step

import muli.scheduled_workflows as scheduled_workflows_module
from muli.providers import ModelTurn, ToolCall
from muli.server import create_app
from muli.store import now


async def test_due_delegation_waits_for_real_terminal_finalizer_without_advancing_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    due_at = now() + 60
    runtime, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "schedule",
                        "workflow_schedule",
                        json.dumps(
                            {
                                "name": "等待容量",
                                "prompt": "执行并独立评审合成资料",
                                "due_at": due_at,
                            }
                        ),
                    )
                ]
            ),
            ModelTurn(content="已登记"),
        ],
    )
    runtime.config.update({"limits": {"max_pending_runs": 1}, "attention": {"enabled": False}})
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    app = create_app(runtime.config, runtime=runtime)
    finalizing, release = asyncio.Event(), asyncio.Event()
    original_emit = runtime.bus.emit
    blocker_id: str | None = None
    blocker_workflow_id: str | None = None

    async def finish_node(session_id: str, prompt: str, role: str, mode: str) -> str:
        return "合成完成证据"

    async def gate_terminal(
        kind: str,
        data: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            kind == "workflow.updated"
            and data["run"]["workflow_id"] == blocker_workflow_id
            and data["run"]["status"] == "completed"
            and data["node_id"] is None
        ):
            finalizing.set()
            await release.wait()
        return await original_emit(kind, data, session_id)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost:8765",
        ) as client:
            response = await client.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert response.status_code == 200
            submitted = await client.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "一分钟后安排合成委托"},
            )
            assert submitted.status_code == 200
            started = submitted.json()
            assert await runtime.wait(started["run_id"]) == "已登记"
        accepted = [
            message
            for message in runtime.store.messages(main_id)
            if message.get("tool_call_id") == "schedule"
        ]
        assert len(accepted) == 1 and accepted[0]["content_provenance"] == "accepted"
        receipt = json.loads(accepted[0]["content"])
        assert isinstance(receipt, dict) and "error" not in receipt
        assert isinstance(receipt.get("id"), str)
        schedules = runtime.store.list("workflow_schedule")
        assert len(schedules) == 1
        scheduled = schedules[0]
        assert scheduled["id"] == receipt["id"]
        assert scheduled["reply_origin"]["kind"] == "web"
        owners = [
            message
            for message in runtime.store.messages(main_id)
            if message.get("principal") == "owner" and message["role"] == "user"
        ]
        assert len(owners) == 1
        assert scheduled["reply_origin"]["input_id"] == owners[0]["id"]
        assert scheduled["next_due_at"] == due_at and scheduled["fired_count"] == 0
        runtime.workflows.run_step = finish_node
        definition = runtime.workflows.create("终态屏障", [step("one")])
        blocker_workflow_id = definition["id"]
        runtime.bus.emit = gate_terminal  # type: ignore[method-assign]
        blocker = await runtime.workflows.start(definition["id"])
        blocker_id = blocker["id"]
        await asyncio.wait_for(finalizing.wait(), 2)
        assert runtime.store.get("workflow_run", blocker_id)["status"] == "completed"
        assert not runtime.attention_busy
        assert not runtime.workflows._tasks[blocker_id].done()
        runtime.reminders.changed.clear()
        monkeypatch.setattr(scheduled_workflows_module, "now", lambda: due_at)

        # This is the real clock callback. Capacity exhaustion must return,
        # not throw into Reminders._run or write an occurrence before admission.
        await runtime.scheduled_workflows.tick(due_at)
        assert runtime.store.get("workflow_schedule", scheduled["id"]) == scheduled
        assert runtime.store.list("workflow_occurrence") == []
        assert runtime.scheduled_workflows.next_due(due_at) is None
        assert not runtime.reminders.changed.is_set()

        release.set()
        await asyncio.wait_for(runtime.workflows.wait(blocker_id), 2)
        await asyncio.wait_for(runtime.reminders.changed.wait(), 2)
        assert runtime.workflows.accepting_runs
        await runtime.scheduled_workflows.tick(due_at)
        occurrences = runtime.store.list("workflow_occurrence")
        assert len(occurrences) == 1
        occurrence = occurrences[0]
        assert occurrence["schedule_id"] == scheduled["id"]
        assert occurrence["run_id"] != blocker_id
        await asyncio.wait_for(runtime.workflows.wait(occurrence["run_id"]), 2)
        assert runtime.store.get("workflow_schedule", scheduled["id"])["fired_count"] == 1
    finally:
        release.set()
        await runtime.close()
