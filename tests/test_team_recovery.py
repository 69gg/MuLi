"""Restarted teammates keep their identity but never replay unknown effects."""

from __future__ import annotations

from pathlib import Path

from test_runtime import FakeClient, make_runtime

from muli.providers import ModelTurn
from muli.runtime import Runtime
from muli.tool_attempt import ToolAttempts


def agent_record(
    identifier: str, parent_id: str, *, run_id: str, status: str = "running"
) -> tuple[dict[str, object], dict[str, object]]:
    session = {
        "id": identifier,
        "title": identifier,
        "kind": "team",
        "role": "worker",
        "mode": "yolo",
        "principal": "owner",
        "status": "running",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    agent = {
        "id": identifier,
        "session_id": identifier,
        "name": identifier,
        "role": "worker",
        "parent_id": parent_id,
        "mode": "yolo",
        "status": status,
        "created_at": 1.0,
        "updated_at": 1.0,
        "generation": 1,
        "completion_revision": 0,
        "run_id": run_id,
        "reply_origin": {"kind": "local", "session_id": parent_id},
    }
    return session, agent


async def test_restart_marks_started_teammate_recovering_and_resume_replans(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()
    recovering_session, recovering = agent_record(
        "recovering-agent", parent["id"], run_id="agent-run"
    )
    blocked_session, blocked = agent_record("blocked-agent", parent["id"], run_id="blocked-run")
    runtime.store.put_many(
        [
            ("session", recovering_session),
            ("agent", recovering),
            ("run", {"id": "agent-run", "session_id": "recovering-agent", "status": "running"}),
            ("session", blocked_session),
            ("agent", blocked),
            ("run", {"id": "blocked-run", "session_id": "blocked-agent", "status": "cancelled"}),
        ]
    )
    attempts = ToolAttempts(runtime.store)
    attempt = attempts.begin(
        run_id="agent-run",
        call_id="call",
        session_id="recovering-agent",
        generation=1,
        tool="fs_write",
        risk="write",
        recovery="never",
        arguments={"path": "result.txt"},
    )
    attempts.mark_started(attempt)
    config, data_dir = runtime.config, runtime.data_dir
    await runtime.close()

    recovered = Runtime(
        config,
        data_dir,
        client_factory=lambda _: FakeClient([ModelTurn(content="恢复后重新规划完成")], []),
    )
    try:
        agent = recovered.team.get("recovering-agent")
        assert agent["status"] == "recovering"
        assert agent["recovery_decision"] == "needs_owner"
        assert agent["recovery_attempt_ids"] == [attempt.attempt_id]
        assert "结果未确认" in agent["recovery_reason"]
        blocked_agent = recovered.team.get("blocked-agent")
        assert blocked_agent["status"] == "interrupted"
        assert blocked_agent["recovery_decision"] == "blocked"
        assert blocked_agent["recovery_reason"] == "运行已进入终态"

        await recovered.team.resume("recovering-agent")
        finished = await recovered.team.wait(["recovering-agent"], timeout=5)
        agent = finished["agents"][0]
        assert agent["status"] == "completed"
        assert agent["generation"] == 2
        assert agent["result"] == "恢复后重新规划完成"
        assert recovered.store.get("run", "agent-run")["status"] == "interrupted"  # type: ignore[index]
    finally:
        await recovered.close()
