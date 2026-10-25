"""Durable input admission and main-approval supersession have one commit."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_owner_approval_steering_runtime import pending_for, policy_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.providers import ModelTurn


@pytest.mark.parametrize("failure", ["transaction", "notification"])
async def test_failed_owner_admission_or_wakeup_has_one_durable_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    effects: list[str] = []

    async def effect(arguments: dict[str, Any]) -> str:
        effects.append(arguments["label"])
        return "unexpected"

    runtime, seen = policy_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[call("waiting", "steering_effect", label="old")]),
            ModelTurn(content="按新输入续办"),
        ],
        effect,
    )
    try:
        first = await runtime.submit("原主人任务", mode="ask", event_id="original")
        approval = await pending_for(runtime, first["session_id"])
        future = runtime.permissions.pending[approval["id"]]
        count = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
        position = runtime.store.event_position()
        original_event, original_notify = runtime.store._event, runtime.bus.notify
        observed: list[bool] = []

        def failed_event(
            event_type: str,
            data: dict[str, Any],
            session_id: str | None = None,
            *,
            objects: list[tuple[str, dict[str, Any]]] | None = None,
        ) -> dict[str, Any]:
            event = original_event(event_type, data, session_id, objects=objects)
            if event_type == "approval.resolved" and data.get("input_id"):
                assert runtime.store.db.in_transaction
                assert runtime.store.get("approval", approval["id"])["status"] == "cancelled"
                assert not future.done()
                observed.append(True)
                raise RuntimeError("synthetic transaction failure")
            return event

        async def failed_notify() -> None:
            if not observed:
                assert not runtime.store.db.in_transaction
                assert runtime.store.get("approval", approval["id"])["status"] == "cancelled"
                assert future.done() and not future.cancelled()
                observed.append(True)
                raise RuntimeError("synthetic notification failure")
            await original_notify()

        if failure == "transaction":
            monkeypatch.setattr(runtime.store, "_event", failed_event)
        else:
            monkeypatch.setattr(runtime.bus, "notify", failed_notify)
        with pytest.raises(RuntimeError, match=f"synthetic {failure} failure"):
            await runtime.submit("新的主人指令", event_id="followup")
        assert observed == [True]
        if failure == "transaction":
            assert runtime.store.get("approval", approval["id"]) == approval
            assert not future.done()
            assert runtime.store.event_position() == position
            assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count
            assert runtime.store.pending_inputs(first["session_id"]) == 0
        else:
            assert runtime.store.get("approval", approval["id"])["status"] == "cancelled"
            assert future.done()
            assert (
                runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count + 1
            )
        monkeypatch.setattr(runtime.store, "_event", original_event)
        monkeypatch.setattr(runtime.bus, "notify", original_notify)
        retry = await runtime.submit("新的主人指令", event_id="followup")
        assert retry["duplicate"] is (failure == "notification")
        async with asyncio.timeout(3):
            assert await runtime.wait(first["run_id"]) == "按新输入续办"
        decisions = [
            event
            for event in runtime.store.events(session_id=first["session_id"])
            if event["type"] == "approval.resolved" and event["data"].get("id") == approval["id"]
        ]
        assert len(decisions) == 1
        assert decisions[0]["data"]["decision"] == "cancelled"
        assert decisions[0]["data"]["input_id"] == retry["message"]["id"]
        assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count + 1
        assert len(seen) == 2 and tool_result(seen[1], "waiting")["status"] == "not_executed"
        assert runtime.live_approvals(first["session_id"]) == [] and effects == []
    finally:
        await runtime.close()
