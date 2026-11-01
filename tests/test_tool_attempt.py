from __future__ import annotations

import json
from pathlib import Path

from test_runtime import make_runtime

from muli.store import new_id
from muli.tool_attempt import ToolAttempts, arguments_digest, attempt_id


def test_attempt_ids_include_the_run_and_digest_arguments() -> None:
    assert attempt_id("run-a", "call-1") == "run-a:call-1"
    assert attempt_id("run-a", "call-1") != attempt_id("run-b", "call-1")
    assert arguments_digest({"b": 2, "a": 1}) == arguments_digest({"a": 1, "b": 2})


async def test_attempt_phases_are_additive_objects(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        run_id = new_id()
        attempts = ToolAttempts(runtime.store)
        attempt = attempts.begin(
            run_id=run_id,
            call_id="call-1",
            session_id="session-1",
            generation=0,
            tool="fs_read",
            risk="read",
            recovery="replayable",
            arguments={"path": "/tmp/a"},
        )
        assert attempt.phase == "planned"
        assert runtime.store.get("tool_attempt", attempt.attempt_id)["phase"] == "planned"
        attempts.mark_started(attempt)
        assert runtime.store.get("tool_attempt", attempt.attempt_id)["phase"] == "started"
        attempts.mark_committed(
            attempt, result_status="completed", result_message_id="message-1"
        )
        runtime.store.put("tool_attempt", attempt.dump())
        stored = runtime.store.get("tool_attempt", attempt.attempt_id)
        assert stored["phase"] == "committed"
        assert stored["result_message_id"] == "message-1"
        assert attempts.safe_phase("committed")
        assert not attempts.safe_phase("started")
    finally:
        await runtime.close()


async def test_started_at_is_preserved_when_command_identity_arrives(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        attempts = ToolAttempts(runtime.store)
        attempt = attempts.begin(
            run_id=new_id(),
            call_id="shell-call",
            session_id="session-1",
            generation=0,
            tool="shell_run",
            risk="external",
            recovery="reconcile",
            arguments={"command": "make"},
        )
        attempts.mark_started(attempt)
        first_started = attempt.started_at
        assert first_started is not None
        attempts.mark_started(attempt, command_id="cmd-1")
        assert attempt.started_at == first_started
        assert attempt.command_id == "cmd-1"
        stored = runtime.store.get("tool_attempt", attempt.attempt_id)
        assert stored["phase"] == "started"
        assert stored["command_id"] == "cmd-1"
    finally:
        await runtime.close()


async def test_repair_tail_uses_attempt_phases_without_replaying(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        session = runtime.new_session()
        sid = str(session["id"])
        run_id = new_id()
        calls = [
            {"id": "planned", "name": "fs_write", "arguments": "{}"},
            {"id": "started", "name": "shell_run", "arguments": "{}"},
            {"id": "committed", "name": "fs_read", "arguments": "{}"},
            {"id": "legacy", "name": "fs_read", "arguments": "{}"},
        ]
        runtime.store.add_message(
            sid,
            "assistant",
            "",
            tool_calls=calls,
            run_id=run_id,
        )
        attempts = ToolAttempts(runtime.store)
        planned = attempts.begin(
            run_id=run_id,
            call_id="planned",
            session_id=sid,
            generation=0,
            tool="fs_write",
            risk="write",
            recovery="never",
            arguments={},
        )
        started = attempts.begin(
            run_id=run_id,
            call_id="started",
            session_id=sid,
            generation=0,
            tool="shell_run",
            risk="write",
            recovery="never",
            arguments={},
        )
        attempts.mark_started(started)
        committed = attempts.begin(
            run_id=run_id,
            call_id="committed",
            session_id=sid,
            generation=0,
            tool="fs_read",
            risk="read",
            recovery="replayable",
            arguments={},
        )
        attempts.mark_started(committed)
        result = runtime.store._message(
            sid, "tool", json.dumps({"ok": True}), tool_call_id="committed"
        )
        runtime.store.put("message", result)
        runtime.store.db.execute(
            "DELETE FROM message_order WHERE message_id=?", (result["id"],)
        )
        runtime.store.db.commit()
        attempts.mark_committed(
            committed, result_status="completed", result_message_id=result["id"]
        )
        runtime.store.put("tool_attempt", committed.dump())

        runtime._repair_tail(sid)

        repaired = {
            item["tool_call_id"]: item
            for item in runtime.store.messages(sid)
            if item["role"] == "tool"
        }
        assert set(repaired) == {"planned", "started", "committed", "legacy"}
        assert json.loads(repaired["planned"]["content"])["status"] == "not_executed"
        assert json.loads(repaired["started"]["content"])["status"] == "unknown"
        assert repaired["committed"]["content"] == json.dumps({"ok": True})
        assert json.loads(repaired["legacy"]["content"])["status"] == "unknown"
        assert runtime.store.get("tool_attempt", planned.attempt_id)["phase"] == "not_executed"
        assert runtime.store.get("tool_attempt", started.attempt_id)["phase"] == "unknown"
        assert runtime.store.get("tool_attempt", committed.attempt_id)["phase"] == "committed"

        # A second repair must not synthesize another result.
        runtime._repair_tail(sid)
        assert len([m for m in runtime.store.messages(sid) if m["role"] == "tool"]) == 4
    finally:
        await runtime.close()
