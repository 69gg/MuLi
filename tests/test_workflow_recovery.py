"""Workflow recovery keeps unstarted nodes and flags started work for the owner."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.recovery import RecoveryPlanner
from muli.runtime import Runtime
from muli.store import Store
from muli.tool_attempt import ToolAttempts


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("工作流恢复不能调用模型")


async def test_workflow_recovery_preserves_pending_and_interrupts_started_nodes(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    data = tmp_path / "data"
    store = Store.open_runtime(data / "muli.sqlite3", default_mode="ask")
    sessions = {
        "pending-node": {
            "id": "pending-node",
            "title": "待开始",
            "kind": "workflow",
            "status": "idle",
        },
        "running-node": {
            "id": "running-node",
            "title": "已开始",
            "kind": "workflow",
            "status": "running",
        },
        "parked-node": {
            "id": "parked-node",
            "title": "已暂停",
            "kind": "workflow",
            "status": "paused",
        },
    }
    for session in sessions.values():
        store.put("session", session)
    store.put(
        "workflow_run",
        {
            "id": "wf-running",
            "workflow_id": "definition",
            "status": "running",
            "nodes": {
                "pending": {"session_id": "pending-node", "status": "pending"},
                "running": {
                    "session_id": "running-node",
                    "status": "running",
                    "started_at": 1.0,
                },
                "parked": {"session_id": "parked-node", "status": "paused"},
            },
        },
    )
    store.put(
        "workflow_run",
        {
            "id": "wf-pending",
            "workflow_id": "definition",
            "status": "running",
            "nodes": {
                "pending": {"session_id": "pending-node", "status": "queued"},
            },
        },
    )
    store.close()

    runtime = Runtime(config, data, client_factory=no_model)
    try:
        started = runtime.store.get("workflow_run", "wf-running")
        assert started is not None
        assert started["status"] == "interrupted"
        assert started["recovery_decision"] == "needs_owner"
        assert started["nodes"]["running"]["status"] == "interrupted"
        assert "核查实际状态" in started["nodes"]["running"]["error"]
        assert started["nodes"]["pending"]["status"] == "pending"
        assert started["nodes"]["parked"]["status"] == "paused"

        unstarted = runtime.store.get("workflow_run", "wf-pending")
        assert unstarted is not None
        assert unstarted["status"] == "interrupted"
        assert unstarted["recovery_decision"] == "resume_safe"
        assert unstarted["nodes"]["pending"]["status"] == "pending"
        assert "保留 pending" in unstarted["error"]

        for identifier in ("pending-node", "running-node"):
            session = runtime.store.get("session", identifier)
            assert session is not None and session["status"] == "idle"
        parked = runtime.store.get("session", "parked-node")
        assert parked is not None and parked["status"] == "paused"
        assert runtime.store.unfinished_object_ids("workflow_run") == []
        assert not runtime.workflows.active_run_ids
    finally:
        await runtime.close()


def test_planner_marks_workflow_with_started_attempt_for_the_owner(tmp_path: Path) -> None:
    store = Store(tmp_path / "planner.sqlite3")
    try:
        store.put("session", {"id": "node-session", "status": "running"})
        store.put("run", {"id": "node-run", "session_id": "node-session", "status": "running"})
        store.put(
            "workflow_run",
            {
                "id": "wf",
                "status": "running",
                "nodes": {"work": {"session_id": "node-session", "status": "running"}},
            },
        )
        attempts = ToolAttempts(store)
        attempt = attempts.begin(
            run_id="node-run",
            call_id="call",
            session_id="node-session",
            generation=0,
            tool="shell_run",
            risk="external",
            recovery="reconcile",
            arguments={"command": "deploy"},
        )
        attempts.mark_started(attempt, command_id="cmd-1")
        planner = RecoveryPlanner(store)
        decision = planner.plan_workflow("wf")
        assert decision.status == "needs_owner"
        assert decision.attempt_ids == (attempt.attempt_id,)
        assert decision.details["attempts"][0]["command_id"] == "cmd-1"
        assert planner.plan_session("node-session").status == "needs_owner"
    finally:
        store.close()
