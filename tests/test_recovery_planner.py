from __future__ import annotations

from pathlib import Path

from test_runtime import make_runtime

from muli.recovery import RecoveryPlanner
from muli.store import new_id
from muli.tool_attempt import ToolAttempts


async def test_resume_safe_only_when_no_started_attempt(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        session = runtime.new_session()
        sid = str(session["id"])
        run_id = new_id()
        runtime.store.put("run", {"id": run_id, "session_id": sid, "status": "interrupted"})
        attempts = ToolAttempts(runtime.store)
        committed = attempts.begin(
            run_id=run_id,
            call_id="read",
            session_id=sid,
            generation=0,
            tool="fs_read",
            risk="read",
            recovery="replayable",
            arguments={},
        )
        attempts.mark_committed(committed, result_status="completed", result_message_id="m1")
        runtime.store.put("tool_attempt", committed.dump())
        planner = RecoveryPlanner(runtime.store)
        assert planner.plan_run(run_id).status == "resume_safe"

        started = attempts.begin(
            run_id=run_id,
            call_id="write",
            session_id=sid,
            generation=0,
            tool="fs_write",
            risk="write",
            recovery="never",
            arguments={},
        )
        attempts.mark_started(started)
        decision = planner.plan_run(run_id)
        assert decision.status == "needs_owner"
        assert decision.attempt_ids == (started.attempt_id,)
        assert decision.details["attempts"][0]["risk"] == "write"
    finally:
        await runtime.close()


async def test_outbox_and_pending_approval_require_the_owner(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        session = runtime.new_session()
        sid = str(session["id"])
        run_id = new_id()
        runtime.store.put("run", {"id": run_id, "session_id": sid, "status": "running"})
        planner = RecoveryPlanner(runtime.store)
        runtime.store.put(
            "approval",
            {"id": "old", "session_id": sid, "run_id": run_id, "status": "pending"},
        )
        decision = planner.plan_run(run_id)
        assert decision.status == "needs_owner"
        assert "审批" in decision.reason

        runtime.store.put("approval", {"id": "old", "session_id": sid, "status": "expired"})
        runtime.store.db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES('o1','telegram','{}','{}','sending',0,0,0)"
        )
        runtime.store.db.commit()
        decision = planner.plan_run(run_id)
        assert decision.status == "needs_owner"
        assert "投递" in decision.reason
    finally:
        await runtime.close()


async def test_cancelled_scope_is_blocked(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        session = runtime.new_session()
        sid = str(session["id"])
        runtime.store.put("session", {**session, "status": "cancelled"})
        planner = RecoveryPlanner(runtime.store)
        assert planner.plan_session(sid).status == "blocked"
    finally:
        await runtime.close()
