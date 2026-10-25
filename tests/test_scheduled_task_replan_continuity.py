"""A completed scheduled generation cannot become a permanent team restriction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient
from test_scheduled_task_replan_guards import paused_schedule
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import tool_result

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.task_control import OwnerControlRequest, TaskPaused


async def test_missing_scheduled_marker_cannot_downgrade_a_real_admitted_owner_receipt(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        await rig.admit()
        control = rig.local.task_control
        snapshot = control.replan_snapshot(rig.main_id, rig.task_id)
        assert control.is_current_replan(snapshot)
        assert snapshot.replan_request_id is not None
        receipt = rig.runtime.store.get("task_control_receipt", snapshot.replan_request_id)
        assert receipt is not None
        occurrence = rig.runtime.store.list("task_replan_occurrence")[0]
        assert occurrence["owner_receipt_id"] == receipt["id"]
        assert receipt["scheduled_replan"]["occurrence_id"] == occurrence["id"]
        assert receipt["request_id"] == "scheduled-replan:" + occurrence["id"]

        # Explicit corruption negative control: retain the real host request and
        # occurrence, deleting only the proof from its actual persisted receipt.
        damaged = {key: value for key, value in receipt.items() if key != "scheduled_replan"}
        rig.runtime.store.put("task_control_receipt", damaged)
        before_task = rig.local.commitments.get(rig.task_id)
        assert not control.is_current_replan(snapshot)
        with pytest.raises((ValueError, PermissionError)):
            control.replan_snapshot(rig.main_id, rig.task_id)
        with pytest.raises((ValueError, PermissionError)):
            control.approve_plan(
                snapshot,
                snapshot.epoch,
                "继续核查本地资料，不部署",
                rig.target_ids,
                caller_session_id=rig.main_id,
                max_targets=rig.runtime.config.settings.limits.max_pending_runs,
            )
        assert rig.runtime.store.get("task_control_receipt", receipt["id"]) == damaged
        assert rig.runtime.store.list("task_replan_occurrence") == [occurrence]
        assert rig.local.commitments.get(rig.task_id) == before_task
        assert rig.runtime.store.list("task_plan_delivery") == []
        target = rig.runtime.team.get(rig.target_ids[0])
        assert target["status"] == "paused" and target["generation"] == 1
        assert not rig.forbidden.exists()


async def test_real_owner_pause_clears_scheduled_domain_without_reviving_the_old_snapshot(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        await rig.admit()
        control = rig.local.task_control
        old_snapshot = control.replan_snapshot(rig.main_id, rig.task_id)
        old_run = rig.runtime.run_ids[rig.main_id]
        old_fields = {"epoch", "approved_epoch", "replan_request_id", "plan"}
        scheduled_state = rig.local.commitments.get(rig.task_id)["control"]
        assert set(scheduled_state) == old_fields | {"scheduled_replan"}
        old_receipt = rig.runtime.store.get("task_control_receipt", old_snapshot.replan_request_id)
        assert old_receipt is not None
        assert scheduled_state["scheduled_replan"] == old_receipt["scheduled_replan"]

        paused = await rig.runtime.submit_task_control(
            rig.task_id,
            OwnerControlRequest("pause", old_snapshot.epoch, "ordinary-owner-pauses-schedule"),
        )
        paused_state = rig.local.commitments.get(rig.task_id)["control"]
        assert set(paused_state) == old_fields
        assert paused_state["epoch"] == old_snapshot.epoch + 1
        assert paused.receipt["epoch"] == paused_state["epoch"]
        assert paused_state["replan_request_id"] is None
        assert not control.is_current_replan(old_snapshot)
        with pytest.raises((ValueError, PermissionError)):
            control.approve_plan(
                old_snapshot,
                old_snapshot.epoch,
                "旧约定不能恢复执行",
                rig.target_ids,
                caller_session_id=rig.main_id,
                max_targets=rig.runtime.config.settings.limits.max_pending_runs,
            )

        # Let the actual stale model response return. The original run must
        # journal its unexecuted call and park, rather than cancelling the child.
        rig.release_due.set()
        with pytest.raises(TaskPaused):
            await asyncio.wait_for(rig.runtime.wait(old_run), 3)
        old_run_record = rig.runtime.store.get("run", old_run)
        assert old_run_record is not None and old_run_record["status"] == "paused"
        old_calls = [
            message
            for message in rig.runtime.store.messages(rig.main_id)
            if message.get("tool_call_id") == "approve"
        ]
        assert len(old_calls) == 1
        assert json.loads(old_calls[0]["content"])["status"] == "not_executed"
        target = rig.runtime.team.get(rig.target_ids[0])
        assert target["status"] == "paused" and target["generation"] == 1

        # A separate real owner request advances the epoch again. Only inspect
        # the ordinary domain snapshot here; old ordinary execution has its own
        # full Runtime regressions and does not need another model fixture.
        rig.phase = "quiet"
        ordinary = await rig.runtime.submit_task_control(
            rig.task_id,
            OwnerControlRequest(
                "request_replan", paused_state["epoch"], "ordinary-owner-replans-after-pause"
            ),
        )
        assert ordinary.created and "scheduled_replan" not in ordinary.receipt
        current = control.replan_snapshot(rig.main_id, rig.task_id)
        assert current.epoch == paused_state["epoch"] + 1
        assert current.replan_request_id == ordinary.receipt["id"]
        assert control.is_current_replan(current)
        assert not control.is_current_replan(old_snapshot)
        assert set(rig.local.commitments.get(rig.task_id)["control"]) == old_fields
        if main_run := rig.runtime.run_ids.get(rig.main_id):
            assert await asyncio.wait_for(rig.runtime.wait(main_run), 3) == "已收到"
        assert rig.runtime.store.list("task_plan_delivery") == []
        target = rig.runtime.team.get(rig.target_ids[0])
        assert target["status"] == "paused" and target["generation"] == 1
        assert not rig.forbidden.exists()


async def test_completed_scheduled_generation_allows_a_real_owner_followup_to_same_child(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        await rig.admit()
        main_run = rig.runtime.run_ids[rig.main_id]
        rig.release_due.set()
        assert await asyncio.wait_for(rig.runtime.wait(main_run), 3) == "已收到"
        scheduled = await wait_agent(rig.runtime, rig.target_ids[0], "completed")
        assert "error" not in rig.approval_result
        assert scheduled["generation"] == 2 and scheduled["completion_revision"] == 2
        scheduled_run = scheduled["run_id"]
        assert rig.runtime.store.get("run", scheduled_run)["status"] == "completed"  # type: ignore[index]
        assert rig.forbidden.read_text(encoding="utf-8") == "不应执行"
        # Drain the real report/finalizer before beginning a separate owner turn.
        await asyncio.wait_for(asyncio.gather(*tuple(rig.runtime.team._tasks)), 3)
        if report_run := rig.runtime.run_ids.get(rig.main_id):
            await asyncio.wait_for(rig.runtime.wait(report_run), 3)
        before_control = rig.runtime.task_control_current(rig.task_id)["control"]
        assert before_control["epoch"] == before_control["approved_epoch"] == 2

        owner_input = "原核查已完成，请让同一个队友读取新补充资料并另写草稿。"
        followup_prompt = "这是主人新的补充核查，读取补充资料并另写本地草稿。"
        evidence = tmp_path / "followup-evidence.txt"
        evidence.write_bytes("新主人补充的核查资料".encode())
        draft = tmp_path / "followup-draft.txt"
        worker_rounds = 0

        class Main(FakeClient):
            def __init__(self) -> None:
                super().__init__([], [])
                self.round = 0

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                self.round += 1
                latest = next(message for message in reversed(messages) if message.role == "user")
                if latest.content != owner_input:
                    turn = ModelTurn(content="已收到队友补充结果")
                elif self.round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "send-followup",
                                "send_input",
                                json.dumps(
                                    {"target": rig.target_ids[0], "message": followup_prompt}
                                ),
                            )
                        ]
                    )
                else:
                    assert self.round == 2
                    assert tool_result(messages, "send-followup")["accepted"] is True
                    turn = ModelTurn(content="已向原队友交办补充核查")
                yield StreamEvent("done", turn=turn)

        class Worker(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal worker_rounds
                worker_rounds += 1
                current = rig.runtime.team.get(rig.target_ids[0])
                assert current["generation"] == 3 and current["completion_revision"] == 2
                if worker_rounds == 1:
                    assert any(followup_prompt in message.content for message in messages)
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "read-followup", "fs_read", json.dumps({"path": str(evidence)})
                            )
                        ]
                    )
                elif worker_rounds == 2:
                    assert tool_result(messages, "read-followup")["content"] == evidence.read_text(
                        encoding="utf-8"
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "write-followup",
                                "fs_write",
                                json.dumps({"path": str(draft), "content": "真实第三代补充草稿"}),
                            )
                        ]
                    )
                else:
                    assert worker_rounds == 3
                    assert tool_result(messages, "write-followup")["path"] == str(draft)
                    turn = ModelTurn(content="补充核查与本地草稿已完成")
                yield StreamEvent("done", turn=turn)

        def factory(config: ModelConfig) -> FakeClient:
            return Worker([], []) if config.model == "worker" else Main()

        rig.runtime.client_factory = factory  # type: ignore[assignment]
        admitted = await rig.runtime.submit(
            owner_input, event_id="owner-after-scheduled-completion"
        )
        assert admitted["duplicate"] is False
        await asyncio.wait_for(rig.runtime.wait(admitted["run_id"]), 3)
        waited = await rig.runtime.team.wait(rig.target_ids, timeout=2)
        assert waited["timed_out"] is False
        current = rig.runtime.team.get(rig.target_ids[0])
        assert current["status"] == "completed", {
            "status": current["status"],
            "generation": current["generation"],
            "completion_revision": current["completion_revision"],
            "worker_rounds": worker_rounds,
        }
        assert current["generation"] == current["completion_revision"] == 3
        assert current["run_id"] != scheduled_run and worker_rounds == 3
        assert draft.read_text(encoding="utf-8") == "真实第三代补充草稿"
        assert rig.runtime.task_control_current(rig.task_id)["control"] == before_control
        assert len(rig.runtime.store.list("task_replan_occurrence")) == 1
        assert (
            len(
                [
                    item
                    for item in rig.runtime.store.list("task_control_receipt")
                    if item["action"] == "request_replan"
                ]
            )
            == 1
        )
