"""A scoped worker consults with its frozen brief, plan and execution epoch."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from test_runtime import FakeClient
from test_task_briefs import attach_services
from test_team_consult_regular import configured_runtime, current_session, until
from test_team_consult_regular import offline as offline
from test_teammate_attachment_runtime import call, tool_result

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.reply_origin import ReplyOrigin
from muli.task_control import ControlResult, OwnerControlRequest, TaskSnapshot


@pytest.mark.parametrize("change", ["none", "progress", "pause", "reapprove", "brief"])
async def test_bound_worker_consults_with_original_scope_and_revokes_stale_advice(
    tmp_path: Path, change: str
) -> None:
    runtime = await configured_runtime(tmp_path)
    services = attach_services(runtime, tmp_path)
    main_id = runtime.main_session()["id"]
    title = "真实绑定的资料核查"
    goal = "核对原始资料并保留准确来源，不将模型建议当成新的主人授权。"
    constraints = ["只读核查，不部署。", "保留中文与 emoji🙂 限制原文。"]
    item = await services.commitments.save(title, brief={"goal": goal, "constraints": constraints})
    identifier = item["id"]
    plan = "先读完整原文，再核对任务限制；仅提供有依据的建议。"

    def owner_control(
        action: Literal["pause", "request_replan"],
        epoch: int,
        request_id: str,
        task_id: str = identifier,
    ) -> ControlResult:
        return services.task_control.owner_control(
            task_id,
            OwnerControlRequest(action, epoch, request_id),
            main_session_id=main_id,
            mode="yolo",
            model_configured=True,
            max_pending=runtime.config.settings.limits.max_pending_runs,
            active_workflow_ids=runtime.workflows.active_run_ids,
        )

    # Establish a nonempty plan through the real owner and approval APIs. The
    # setup's sole queued replan request is cancelled before the measured run;
    # its authentic owner/approval receipts and plan remain in the temporary DB.
    initial_epoch = 0
    initial_plan: dict[str, Any] | None = None
    if change != "brief":
        owner_control("pause", 0, "initial-pause")
        initial_replan = owner_control("request_replan", 1, "initial-replan")
        initial_snapshot = services.task_control.replan_snapshot(main_id, identifier)
        services.task_control.approve_plan(
            initial_snapshot,
            initial_snapshot.epoch,
            plan,
            [],
            caller_session_id=main_id,
            max_targets=runtime.config.settings.limits.max_pending_runs,
        )
        assert initial_replan.ingress is not None and runtime.store.pending_inputs(main_id) == 1
        runtime.store.cancel_inputs(main_id)
        initial_epoch = initial_snapshot.epoch
        initial_plan = {"epoch": initial_epoch, "text": plan}
    evidence = tmp_path / "scoped-original.txt"
    evidence_text = "原始资料：需要逐项核对限制，记住结尾🙂。"
    evidence.write_text(evidence_text, encoding="utf-8")
    question = f"请核对 {evidence} 和任务 {identifier}，按原方案给我只读建议。"
    answer = "原资料和全部限制已核对；按原方案继续，建议不增加操作授权。"
    assigned = "绑定队友已派出，主派工轮已结束。"
    worker_final = "本次咨询回执已处理，原队友没有执行额外操作。"
    entered, release_worker = asyncio.Event(), asyncio.Event()
    model_waiting, release_model = asyncio.Event(), asyncio.Event()
    child_ids: list[str] = []
    child_runs: list[str] = []
    frozen: list[TaskSnapshot] = []
    offered: list[bool] = []
    control_runs: list[str] = []
    read_receipts: list[dict[str, Any]] = []
    rounds = {"owner": 0, "worker": 0, "control": 0}
    initial_run = ""
    selected_id: str | None = None

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            names = {tool["function"]["name"] for tool in tools}
            if self.role == "worker":
                rounds["worker"] += 1
                child_id = current_session(runtime)
                child = runtime.team.get(child_id)
                assert child["parent_id"] == main_id and child["commitment_id"] == identifier
                assert child["generation"] == 1
                if rounds["worker"] == 1:
                    scope = services.task_control.execution_snapshot(child_id)
                    assert scope is not None and scope.epoch == initial_epoch
                    assert scope.approved_epoch == scope.epoch
                    assert json.loads(scope.plan_json) == initial_plan
                    child_ids.append(child_id)
                    child_runs.append(runtime.run_ids[child_id])
                    frozen.append(scope)
                    offered.append("team_consult" in names)
                    entered.set()
                    await asyncio.wait_for(release_worker.wait(), timeout=2)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "scoped-consult", "team_consult", question=question, wait_seconds=2
                            )
                        ]
                    )
                else:
                    assert rounds["worker"] == 2
                    assert runtime.run_ids[child_id] == child_runs[0]
                    receipt = tool_result(messages, "scoped-consult")
                    if change in {"none", "progress"}:
                        assert receipt["answer"] == answer
                    else:
                        assert "error" in receipt and "answer" not in receipt
                    turn = ModelTurn(content=worker_final)
            elif not initial_run or runtime.run_ids[main_id] == initial_run:
                rounds["owner"] += 1
                if rounds["owner"] == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-bound-worker",
                                "spawn_agent",
                                task_name="绑定直属队友",
                                prompt="按完整任务说明咨询主助手，然后沿用原运行完成。",
                                commitment_id=identifier,
                            )
                        ]
                    )
                else:
                    assert rounds["owner"] == 2
                    assert (
                        tool_result(messages, "spawn-bound-worker")["commitment_id"] == identifier
                    )
                    turn = ModelTurn(content=assigned)
            elif any(
                message.get("source") == f"team-consult:{child_ids[0]}"
                and message.get("run_id") == runtime.run_ids[main_id]
                for message in runtime.store.messages(main_id)
            ):
                rounds["control"] += 1
                scope = frozen[0]
                assert services.task_control.resolve_scope(main_id) is None
                assert services.task_control.execution_snapshot(main_id) is None
                assert runtime._selected_replans.get(main_id) == selected_id
                assert any(
                    message.role == "system" and scope.context() in message.content
                    for message in messages
                )
                known = runtime.session_tools(main_id)
                assert names <= {name for name, tool in known.items() if tool.risk == "read"}
                assert {"fs_read", "commitment_read"} <= names
                assert {"commitment_save", "task_approve_plan", "spawn_agent"}.isdisjoint(names)
                current_run = runtime.run_ids[main_id]
                if not control_runs:
                    control_runs.append(current_run)
                assert control_runs == [current_run]
                assert runtime.store.get("run", initial_run)["status"] == "completed"
                if rounds["control"] == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call("read-original", "fs_read", path=str(evidence)),
                            call("read-bound-brief", "commitment_read", identifier=identifier),
                        ]
                    )
                else:
                    assert rounds["control"] == 2
                    read = tool_result(messages, "read-original")
                    assert read == {"path": str(evidence.resolve()), "content": evidence_text}
                    brief = tool_result(messages, "read-bound-brief")
                    assert brief["id"] == identifier and brief["has_more"] is False
                    assert json.loads(brief["text"]) == json.loads(scope.brief_json)
                    read_receipts.append(brief)
                    # Return a genuine late provider answer even if invalidation
                    # cancels its request while it is suspended here.
                    model_waiting.set()
                    while not release_model.is_set():
                        try:
                            await release_model.wait()
                        except asyncio.CancelledError:
                            continue
                    turn = ModelTurn(content=answer)
            else:
                turn = ModelTurn(content="已收到任务控制或队友报告，未执行额外操作。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> Client:
        return Client(config.model)

    runtime.client_factory = factory
    try:
        started = await runtime.start(main_id, "派一名绑定任务的直属执行队友核对原资料。", "yolo")
        initial_run = started["run_id"]
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert offered == [True], "真实绑定 worker 的 provider 缺少咨询入口"
        assert await asyncio.wait_for(runtime.wait(initial_run), timeout=2) == assigned
        if change == "none":
            other = await services.commitments.save(
                "主助手原先选中的独立重规划",
                brief={"goal": "另一项尚待批准的任务", "constraints": ["保持暂停"]},
            )
            owner_control("pause", 0, "other-pause", other["id"])
            pending = owner_control("request_replan", 1, "other-replan", other["id"])
            assert pending.ingress is not None and runtime.store.pending_inputs(main_id) == 1
            runtime.store.cancel_inputs(main_id)
            runtime.select_replan(main_id, other["id"])
            selected_id = other["id"]
            assert runtime._selected_replans.get(main_id) == selected_id
        release_worker.set()
        await asyncio.wait_for(model_waiting.wait(), timeout=2)
        scope = frozen[0]
        input_seq = runtime.store.db.execute("SELECT COALESCE(MAX(seq),0) FROM ingress").fetchone()[
            0
        ]
        if change == "progress":
            updated = await services.commitments.save(
                title, identifier=identifier, next_step="资料正在核对，保持原任务和方案。"
            )
            assert updated["revision"] > scope.revision
            current = services.task_control.execution_snapshot(child_ids[0])
            assert current is not None and replace(current, revision=scope.revision) == scope
        elif change in {"pause", "reapprove"}:
            if change == "reapprove":
                original_consult = runtime.team.consult._requests[child_runs[0]]
                owner_control("pause", scope.epoch, "pause-during-consult")
                owner_control("request_replan", scope.epoch + 1, "replan-during-consult")
                replan = services.task_control.replan_snapshot(main_id, identifier)
                services.task_control.approve_plan(
                    replan,
                    replan.epoch,
                    "新方案：重新核查当前事实，不消费旧咨询的答案。",
                    [],
                    caller_session_id=main_id,
                    max_targets=runtime.config.settings.limits.max_pending_runs,
                )
                current = services.task_control.execution_snapshot(child_ids[0])
                assert current is not None and current.epoch == current.approved_epoch > scope.epoch
                # Isolate the scope fence on a value copy before yielding. The
                # real request still retains its earlier owner-input boundary.
                latest_input = runtime.store.db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM ingress"
                ).fetchone()[0]
                probe = replace(original_consult, input_seq=latest_input)
                with pytest.raises(PermissionError, match="执行版本已改变"):
                    runtime.team.consult.guard_child(probe)
                await runtime.bus.notify()
            else:
                await runtime.submit_task_control(
                    identifier,
                    OwnerControlRequest("pause", scope.epoch, "pause-during-consult"),
                )
        elif change == "brief":
            updated = await services.commitments.save(
                title,
                identifier=identifier,
                brief={
                    "goal": goal,
                    "constraints": [*constraints, "新增完整限制：本轮建议不能复用。"],
                },
            )
            assert "control" not in updated and scope.epoch == 0
            current = services.task_control.execution_snapshot(child_ids[0])
            assert current is not None and current.brief_json != scope.brief_json
        if change in {"progress", "pause", "brief"}:
            assert (
                runtime.store.db.execute(
                    "SELECT 1 FROM ingress WHERE seq>? AND json_extract(payload,'$.principal')='owner'",
                    (input_seq,),
                ).fetchone()
                is None
            )
        release_model.set()
        await until(
            runtime,
            lambda: runtime.team.get(child_ids[0])["status"] in {"completed", "paused", "failed"},
        )
        await until(
            runtime,
            lambda: (
                (runtime.store.get("run", control_runs[0]) or {}).get("status")
                in {"completed", "failed", "cancelled", "paused"}
            ),
        )
        child = runtime.team.get(child_ids[0])
        assert child["run_id"] == child_runs[0] and child["generation"] == 1
        receipts = [
            message
            for message in runtime.store.messages(child_ids[0])
            if message.get("tool_call_id") == "scoped-consult"
        ]
        assert len(receipts) == 1
        receipt = json.loads(receipts[0]["content"])
        control = runtime.store.get("run", control_runs[0])
        assert control is not None and control["status"] != "paused"
        assert len(read_receipts) == 1 and rounds["control"] == 2
        assert services.task_control.resolve_scope(main_id) is None
        assert runtime._selected_replans.get(main_id) == selected_id
        if change in {"none", "progress"}:
            assert receipt["answer"] == answer and receipt["child_run_id"] == child_runs[0]
            assert receipt["main_run_id"] == control_runs[0]
            assert child["status"] == "completed" and child["result"] == worker_final
            assert control["status"] == "completed"
            assert ReplyOrigin.load(control.get("reply_origin")) == ReplyOrigin()
        else:
            assert "error" in receipt and "answer" not in receipt
            assert control.get("result") != answer
            assert not any(
                message["role"] == "assistant" and message["content"] == answer
                for message in runtime.store.messages(child_ids[0])
            )
    finally:
        release_worker.set()
        release_model.set()
        await runtime.close()
