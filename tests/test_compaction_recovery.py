from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_runtime import make_runtime

from muli.compaction import closed_groups
from muli.providers import ModelTurn


@pytest.mark.parametrize("kind", ["main", "independent", "workflow_step"])
async def test_reopened_interrupted_batch_closes_before_new_input_and_compacts(
    tmp_path: Path, kind: str
) -> None:
    first, _ = make_runtime(tmp_path, [])
    first.config.update(
        {"compaction": {"mode": "extractive"}, "limits": {"max_context_chars": 16000}}
    )
    session = first.main_session() if kind == "main" else first.new_session(mode="yolo")
    session_id = str(session["id"])
    first.store.add_message(session_id, "user", "旧的明确请求：" + "继续检查证据。" * 100)
    first.store.add_message(
        session_id,
        "assistant",
        "已开始只读工具调用",
        tool_calls=[
            {"id": "finished-call", "name": "fs_list", "arguments": "{}"},
            {"id": "crash-call", "name": "fs_list", "arguments": "{}"},
        ],
    )
    success = first.store.add_message(
        session_id, "tool", "已保存的真实结果", tool_call_id="finished-call"
    )
    first.store.put("session", {**session, "status": "running"})
    first.store.put("run", {"id": "crashed-run", "session_id": session_id, "status": "running"})
    # No live task exists; persisted rows reproduce the exact crash boundary.
    await first.close()

    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content="本轮已完成。" * 500) for _ in range(3)]
    )
    try:
        for index in range(3):
            text = f"第 {index} 次新请求：" + "请保留已确认的限制。" * 350
            if kind == "workflow_step":
                await runtime.run_step(session_id, text, "worker", "yolo")
            else:
                receipt = (
                    await runtime.submit(text, mode="yolo")
                    if kind == "main"
                    else await runtime.start(session_id, text, mode="yolo")
                )
                assert receipt.get("run_id") is not None
                await runtime.wait(str(receipt["run_id"]))
            runtime.compaction.observe(session_id)
            await runtime.compaction.finish(session_id)

        assert len(seen) == 3
        records = runtime.store.messages(session_id)
        unknown = [record for record in records if record.get("tool_call_id") == "crash-call"]
        assert len(unknown) == 1
        assert unknown[0]["interrupted"] is True
        assert json.loads(unknown[0]["content"])["status"] == "unknown"
        assert [record for record in records if record.get("tool_call_id") == "finished-call"] == [
            success
        ]
        resumed = next(
            record for record in records if record["content"].startswith("第 0 次新请求：")
        )
        assert unknown[0]["seq"] < resumed["seq"]
        assert sum(len(group) for group in closed_groups(records)) == len(records)
        assert runtime.compaction.status(session_id)["covered_seq"] > resumed["seq"]
        # Repeated recovery cannot synthesize a second result or execute a tool.
        runtime._repair_tail(session_id)
        assert runtime.store.messages(session_id) == records
        assert not any(event["type"] == "tool.started" for event in runtime.store.events())
    finally:
        await runtime.close()
