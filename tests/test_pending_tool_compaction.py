from __future__ import annotations

import json
from pathlib import Path

import pytest

from muli.compaction import Compactor
from muli.config import CompactionSettings
from muli.store import Store


async def summary(source: str, max_tokens: int) -> str:
    raise AssertionError("本地摘录不调用模型")


@pytest.mark.parametrize("preserve", [True, False])
async def test_background_prefix_keeps_unconsumed_results_until_assistant_response(
    tmp_path: Path,
    preserve: bool,
) -> None:
    store = Store(tmp_path / "messages.sqlite3")
    session = store.main_session()
    session_id = session["id"]
    store.put("session", {**session, "status": "running"})
    store.add_message(session_id, "user", "旧任务" * 100)
    store.add_message(session_id, "assistant", "已完成" * 100)
    store.add_message(session_id, "user", "核对证据")
    store.add_message(
        session_id,
        "assistant",
        "",
        tool_calls=[
            {"id": "read", "name": "conversation_read", "arguments": "{}"},
        ],
    )
    result = store.add_message(
        session_id,
        "tool",
        json.dumps({"data": "完整" * 2500}, ensure_ascii=False),
        tool_call_id="read",
        preserve_content=preserve,
    )
    store.add_message(session_id, "user", "主人在工具返回时补充")
    records = store.messages(session_id)
    result_seq = next(item["seq"] for item in records if item["id"] == result["id"])
    compactor = Compactor(
        store,
        summary,
        lambda: CompactionSettings(mode="extractive"),
        max_chars=lambda: 4000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "same",
    )
    try:
        compactor.observe(session_id)
        assert await compactor.finish(session_id)
        active = store.get("compaction", session_id)
        assert active is not None
        assert active["covered_seq"] < result_seq
        projected = compactor.project(session_id)
        assert (
            next(item for item in projected if item.get("id") == result["id"])["content"]
            == result["content"]
        )
        store.add_message(session_id, "assistant", "模型已读过完整回执并回应")
        store.add_message(session_id, "user", "新任务")
        compactor.observe(session_id)
        assert await compactor.finish(session_id)
        assert store.get("compaction", session_id)["covered_seq"] >= result_seq
        assert store.messages(session_id)[: len(records)] == records
    finally:
        await compactor.close()
        store.close()


async def test_consuming_assistant_outside_prefix_window_does_not_block_old_result(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "messages.sqlite3")
    session = store.main_session()
    session_id = session["id"]
    store.put("session", {**session, "status": "running"})
    store.add_message(session_id, "user", "旧任务")
    store.add_message(session_id, "assistant", "旧答复")
    store.add_message(session_id, "user", "读取")
    store.add_message(
        session_id,
        "assistant",
        "",
        tool_calls=[
            {"id": "old", "name": "conversation_read", "arguments": "{}"},
        ],
    )
    result = store.add_message(
        session_id, "tool", "旧完整回执" * 1800, tool_call_id="old", preserve_content=True
    )
    store.add_message(session_id, "user", "前窗内补充")
    store.add_message(session_id, "user", "前窗外大段补充" * 1800)
    reply = store.add_message(session_id, "assistant", "实际早已读过完整回执")
    store.add_message(session_id, "user", "继续")
    records = store.messages(session_id)
    result_seq = next(item["seq"] for item in records if item["id"] == result["id"])
    prefix = store.message_prefix(session_id, max_chars=16000)
    assert result["id"] in {item["id"] for item in prefix}
    assert reply["id"] not in {item["id"] for item in prefix}
    compactor = Compactor(
        store,
        summary,
        lambda: CompactionSettings(mode="extractive"),
        max_chars=lambda: 4000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "same",
    )
    try:
        compactor.observe(session_id)
        assert await compactor.finish(session_id)
        assert store.get("compaction", session_id)["covered_seq"] >= result_seq
    finally:
        await compactor.close()
        store.close()
