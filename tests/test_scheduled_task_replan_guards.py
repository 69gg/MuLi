"""Revocation uses real paused teammates, owner tools and the original transaction."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import tool_result

from muli.local_services import LocalServices
from muli.permissions import Mode
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now
from muli.task_control import OwnerControlRequest


@dataclass
class GuardRig:
    runtime: Runtime
    local: LocalServices
    web: httpx.AsyncClient
    main_id: str
    task_id: str
    target_ids: list[str]
    due_at: float
    forbidden: Path
    phase: str = "quiet"
    schedule: dict[str, Any] = field(default_factory=dict)
    due_ready: asyncio.Event = field(default_factory=asyncio.Event)
    release_due: asyncio.Event = field(default_factory=asyncio.Event)
    approve_targets: list[str] = field(default_factory=list)
    approval_result: dict[str, Any] = field(default_factory=dict)

    def current(self) -> dict[str, Any]:
        item = self.runtime.store.get("task_replan_schedule", self.schedule["id"])
        assert item is not None
        return item

    async def edit_mode(self, entry: str, mode: Mode) -> None:
        if entry == "patch":
            response = await self.web.patch(f"/api/sessions/{self.main_id}", json={"mode": mode})
            assert response.status_code == 200 and response.json()["mode"] == mode
        else:
            started = (
                await self.runtime.submit("仅切换本次权限，不继续原任务", mode=mode)
                if entry == "submit"
                else await self.runtime.start(
                    self.main_id, "仅切换本次权限，不继续原任务", mode=mode
                )
            )
            assert await asyncio.wait_for(self.runtime.wait(started["run_id"]), 3) == "已收到"
        assert self.runtime.store.get("session", self.main_id)["mode"] == mode  # type: ignore[index]

    async def admit(self) -> None:
        self.phase = "due"
        await self.runtime.scheduled_task_replans.tick(self.due_at)
        await asyncio.wait_for(self.due_ready.wait(), 3)
        occurrences = self.runtime.store.list("task_replan_occurrence")
        assert len(occurrences) == 1
        assert occurrences[0]["schedule_id"] == self.schedule["id"]
        assert occurrences[0]["owner_receipt_id"]
        assert self.runtime.team.get(self.target_ids[0])["status"] == "paused"


@asynccontextmanager
async def paused_schedule(tmp_path: Path, *, targets: int = 1) -> AsyncIterator[GuardRig]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    local: LocalServices = app.state.local_services
    main_id = runtime.main_session()["id"]
    evidence = tmp_path / "guard-evidence.txt"
    evidence.write_bytes("必须保留的真实本地资料".encode())
    forbidden = tmp_path / "must-not-execute.txt"
    read_ready = [asyncio.Event() for _ in range(targets)]
    release_initial = asyncio.Event()
    worker_instances = 0
    rig: GuardRig

    class Main(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])
            self.round = 0
            self.phase = rig.phase

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.round += 1
            if self.phase not in {"schedule", "due"} or (
                self.phase == "due"
                and any(
                    message.role == "user" and message.content == "先别继续原任务，重新说明情况"
                    for message in messages
                )
            ):
                turn = ModelTurn(content="已收到")
            elif self.round == 1:
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "read-task", "commitment_read", json.dumps({"identifier": rig.task_id})
                        )
                    ]
                )
            elif self.round == 2:
                page = tool_result(messages, "read-task")
                assert page["has_more"] is False
                assert json.loads(page["text"]) == local.commitments.get(rig.task_id)["brief"]
                if self.phase == "schedule":
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "schedule",
                                "task_schedule_replan",
                                json.dumps(
                                    {
                                        "task_id": rig.task_id,
                                        "target_id": rig.target_ids[0],
                                        "due_at": rig.due_at,
                                        "expires_at": rig.due_at + 60,
                                        "key": "guard-original-target",
                                    }
                                ),
                            )
                        ]
                    )
                else:
                    # A complete real read has selected the owner receipt for
                    # the next request. Keep that actual request in flight.
                    rig.due_ready.set()
                    await rig.release_due.wait()
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "approve",
                                "task_approve_plan",
                                json.dumps(
                                    {
                                        "observed_epoch": 2,
                                        "plan": "继续核查本地资料，不部署",
                                        "targets": rig.approve_targets,
                                    }
                                ),
                            )
                        ]
                    )
            else:
                assert self.round == 3
                result = tool_result(
                    messages, "schedule" if self.phase == "schedule" else "approve"
                )
                if self.phase == "schedule":
                    assert "error" not in result, result
                    rig.schedule = result
                else:
                    rig.approval_result = result
                    rig.phase = "quiet"
                turn = ModelTurn(content="已收到")
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        def __init__(self, index: int) -> None:
            super().__init__([], [])
            self.index, self.round = index, 0

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.round += 1
            if self.index >= targets:
                turn = (
                    ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "resume-write",
                                "fs_write",
                                json.dumps({"path": str(forbidden), "content": "不应执行"}),
                            )
                        ]
                    )
                    if self.round == 1
                    else ModelTurn(content="已核查工具回执")
                )
            elif self.round == 1:
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall("read-evidence", "fs_read", json.dumps({"path": str(evidence)}))
                    ]
                )
            else:
                assert self.round == 2
                assert tool_result(messages, "read-evidence")["content"] == evidence.read_text(
                    encoding="utf-8"
                )
                read_ready[self.index].set()
                await release_initial.wait()
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "paused-write",
                            "fs_write",
                            json.dumps({"path": str(forbidden), "content": "不应执行"}),
                        )
                    ]
                )
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        nonlocal worker_instances
        if config.model == "worker":
            index = worker_instances
            worker_instances += 1
            return Worker(index)
        return Main()

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web:
            response = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert response.status_code == 200
            item = await local.commitments.save(
                "原核查任务",
                brief={"goal": "核查本地项目", "constraints": ["不可部署", "保留证据"]},
            )
            rig = GuardRig(runtime, local, web, main_id, item["id"], [], now() + 60, forbidden)
            for index in range(targets):
                agent = await runtime.team.spawn(
                    f"原核查队友{index}",
                    f"读取 {evidence} 并核查项目",
                    parent_id=main_id,
                    commitment_id=item["id"],
                )
                rig.target_ids.append(agent["id"])
            await asyncio.wait_for(asyncio.gather(*(ready.wait() for ready in read_ready)), 3)
            await runtime.submit_task_control(
                item["id"], OwnerControlRequest("pause", 0, "park-originals")
            )
            release_initial.set()
            for identifier in rig.target_ids:
                agent = await wait_agent(runtime, identifier, "paused")
                assert agent["generation"] == agent["completion_revision"] == 1
                assert agent["parent_id"] == main_id and agent["commitment_id"] == item["id"]
            assert not forbidden.exists()
            rig.phase = "schedule"
            started = await runtime.submit(
                "约定一分钟后至两分钟内重新规划给原队友", event_id="guard-owner-schedule"
            )
            assert await asyncio.wait_for(runtime.wait(started["run_id"]), 3) == "已收到"
            assert rig.schedule["id"] and rig.current()["state"] == "waiting"
            assert rig.current()["target_id"] == rig.target_ids[0]
            rig.phase = "quiet"
            rig.approve_targets = rig.target_ids[:1]
            yield rig
    finally:
        release_initial.set()
        if "rig" in locals():
            rig.release_due.set()
        await runtime.close()


@pytest.mark.parametrize("entry", ["patch", "submit", "start"])
async def test_real_mode_entry_aba_permanently_revokes_waiting_schedule(
    tmp_path: Path, entry: str
) -> None:
    async with paused_schedule(tmp_path) as rig:
        await rig.edit_mode(entry, "ask")
        assert rig.current()["authorization_revoked"] is True
        await rig.edit_mode(entry, "yolo")
        assert rig.current()["authorization_revoked"] is True
        await rig.runtime.scheduled_task_replans.tick(rig.due_at)
        assert rig.runtime.store.list("task_replan_occurrence") == []
        assert not any(
            item["action"] == "request_replan"
            for item in rig.runtime.store.list("task_control_receipt")
        )
        assert rig.runtime.team.get(rig.target_ids[0])["generation"] == 1
        assert not rig.forbidden.exists()


async def test_title_and_same_mode_edits_preserve_the_actual_waiting_authorization(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        before = rig.current()
        response = await rig.web.patch(
            f"/api/sessions/{rig.main_id}", json={"title": "新的会话标题", "mode": "yolo"}
        )
        assert response.status_code == 200 and response.json()["title"] == "新的会话标题"
        await rig.edit_mode("submit", "yolo")
        await rig.edit_mode("start", "yolo")
        assert rig.current() == before
        await rig.admit()
        assert len(rig.runtime.store.list("task_replan_occurrence")) == 1


@pytest.mark.parametrize("entry", ["patch", "submit", "start"])
async def test_mode_edit_and_schedule_revocation_rollback_together(
    tmp_path: Path, entry: str
) -> None:
    async with paused_schedule(tmp_path) as rig:
        before_session = rig.runtime.store.get("session", rig.main_id)
        before_schedule = rig.current()
        before_ingress = rig.runtime.store.db.execute("SELECT COUNT(*) FROM ingress").fetchone()[0]
        # This aborts the real invalidation write, not a mock of the helper.
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "CREATE TRIGGER fail_replan_revocation BEFORE UPDATE ON objects WHEN OLD.kind='task_replan_schedule' AND OLD.id='"
                + rig.schedule["id"]
                + "' BEGIN SELECT RAISE(ABORT,'revocation_failed'); END"
            )
        try:
            with pytest.raises(sqlite3.IntegrityError, match="revocation_failed"):
                await rig.edit_mode(entry, "ask")
        finally:
            with rig.runtime.store.db:
                rig.runtime.store.db.execute("DROP TRIGGER fail_replan_revocation")
        assert rig.runtime.store.get("session", rig.main_id) == before_session
        assert rig.current() == before_schedule
        assert (
            rig.runtime.store.db.execute("SELECT COUNT(*) FROM ingress").fetchone()[0]
            == before_ingress
        )
        assert rig.main_id not in rig.runtime.tasks


async def test_new_owner_input_revokes_admitted_receipt_and_old_selection_cannot_recover(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        await rig.admit()
        snapshot = rig.local.task_control.replan_snapshot(rig.main_id, rig.task_id)
        run_id = rig.runtime.run_ids[rig.main_id]
        assert rig.local.task_control.is_current_replan(snapshot)
        admitted = await rig.runtime.submit(
            "先别继续原任务，重新说明情况", event_id="guard-new-owner"
        )
        assert admitted["duplicate"] is False and admitted["message"]["state"] == "pending"
        assert rig.current()["authorization_revoked"] is True
        assert not rig.local.task_control.is_current_replan(snapshot)
        with pytest.raises((ValueError, PermissionError)):
            rig.local.task_control.replan_snapshot(rig.main_id, rig.task_id)
        rig.release_due.set()
        await asyncio.wait_for(rig.runtime.wait(run_id), 3)
        assert rig.runtime.store.list("task_plan_delivery") == []
        assert rig.runtime.team.get(rig.target_ids[0])["generation"] == 1
        assert not rig.forbidden.exists()


async def test_scheduled_approval_cannot_substitute_another_real_paused_target(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path, targets=2) as rig:
        rig.approve_targets = rig.target_ids[1:]
        await rig.admit()
        run_id = rig.runtime.run_ids[rig.main_id]
        before = rig.local.commitments.get(rig.task_id)
        rig.release_due.set()
        await asyncio.wait_for(rig.runtime.wait(run_id), 3)
        assert "error" in rig.approval_result
        assert rig.local.commitments.get(rig.task_id) == before
        assert rig.runtime.store.list("task_plan_delivery") == []
        assert all(
            rig.runtime.team.get(identifier)["generation"] == 1 for identifier in rig.target_ids
        )
        assert not rig.forbidden.exists()


async def test_mode_aba_after_approval_blocks_original_target_at_last_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with paused_schedule(tmp_path) as rig:
        entered, release = asyncio.Event(), asyncio.Event()
        original_emit = rig.runtime.bus.emit

        async def emit(kind: str, data: dict[str, Any], session_id: str | None = None) -> Any:
            result = await original_emit(kind, data, session_id)
            if kind == "tool.started" and data.get("call_id") == "resume-write":
                assert session_id == rig.target_ids[0]
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(rig.runtime.bus, "emit", emit)
        try:
            await rig.admit()
            run_id = rig.runtime.run_ids[rig.main_id]
            rig.release_due.set()
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(rig.runtime.wait(run_id), 3)
            assert "error" not in rig.approval_result
            target = rig.runtime.team.get(rig.target_ids[0])
            assert target["generation"] == 2 and target["completion_revision"] == 1
            assert not rig.forbidden.exists()
            rig.phase = "quiet"
            await rig.edit_mode("patch", "ask")
            await rig.edit_mode("patch", "yolo")
            assert rig.current()["authorization_revoked"] is True
            release.set()
            waited = await rig.runtime.team.wait(rig.target_ids, timeout=2)
            assert waited["timed_out"] is False
            assert not rig.forbidden.exists()
            result = next(
                message
                for message in rig.runtime.store.messages(target["session_id"])
                if message.get("tool_call_id") == "resume-write"
            )
            value = json.loads(result["content"])
            assert "error" in value or value.get("status") == "not_executed"
        finally:
            release.set()
