from __future__ import annotations

from pathlib import Path

import pytest

from muli.compaction import Compactor
from muli.config import CompactionSettings
from muli.store import Store


@pytest.mark.parametrize("native", [False, True], ids=["text", "consumed-native-batch"])
async def test_long_first_input_includes_completed_reply_before_stopping(
    tmp_path: Path, native: bool
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    session_id = "long-first-input"
    source = store.add_message(session_id, "user", "保留原始要求：" + "资料" * 2100)
    reply = store.add_message(
        session_id,
        "assistant",
        "已经核查资料",
        **(
            {
                "tool_calls": [
                    {"id": "a", "name": "read", "arguments": "{}"},
                    {"id": "b", "name": "check", "arguments": "{}"},
                ],
                "provider_data": {
                    "content_blocks": [
                        {"type": "thinking", "signature": "original-signature", "thinking": "核查"}
                    ]
                },
            }
            if native
            else {}
        ),
    )
    cutoff = reply
    if native:
        store.add_message(
            session_id, "tool", "首个完整结果", tool_call_id="a", preserve_content=True
        )
        cutoff = store.add_message(
            session_id, "tool", "另一个完整结果", tool_call_id="b", preserve_content=True
        )
        store.add_message(session_id, "assistant", "完整工具结果已核对")
    latest = store.add_message(session_id, "user", "下一步继续核查，不能部署。")
    original = store.messages(session_id)
    seen: list[str] = []

    async def summarize(text: str, max_tokens: int) -> str:
        seen.append(text)
        return "先前资料已核对，继续核查，不能部署。"

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "unchanged",
    )
    try:
        compactor.observe(session_id)
        assert compactor.status(session_id)["running"] is True
        await compactor.finish(session_id)
        active = store.get("compaction", session_id)
        assert active is not None and active["covered_seq"] == cutoff["seq"]
        assert len(seen) == 1
        assert f"seq={source['seq']} user" in seen[0]
        assert f"seq={reply['seq']} assistant" in seen[0]
        assert f"seq={latest['seq']} user" not in seen[0]
        if native:
            assert "call=a:read,b:check" in seen[0]
            assert "首个完整结果" in seen[0] and "另一个完整结果" in seen[0]
        projected = compactor.project(session_id)
        assert projected[0]["compaction_summary"] is True
        assert projected[-1] == latest
        assert store.messages(session_id) == original
    finally:
        await compactor.close()
        store.close()


@pytest.mark.parametrize("pending", ["owner-input", "complete-tool", "partial-tool"])
async def test_long_input_does_not_cross_unanswered_or_unconsumed_boundary(
    tmp_path: Path, pending: str
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    session_id = "pending-input"
    store.add_message(session_id, "user", "未完成要求" + "资料" * 2100)
    if pending == "owner-input":
        store.add_message(session_id, "user", "还有这个要求")
    else:
        store.add_message(
            session_id,
            "assistant",
            "",
            tool_calls=[
                {"id": "a", "name": "read", "arguments": "{}"},
                {"id": "b", "name": "check", "arguments": "{}"},
            ],
        )
        store.add_message(session_id, "tool", "完整结果A", tool_call_id="a", preserve_content=True)
        if pending == "complete-tool":
            store.add_message(
                session_id, "tool", "完整结果B", tool_call_id="b", preserve_content=True
            )
    store.add_message(session_id, "user", "刚补充的要求必须保留")
    original = store.messages(session_id)

    async def forbidden(text: str, max_tokens: int) -> str:
        pytest.fail("不能为越过未消费边界而调用摘要模型")

    compactor = Compactor(
        store,
        forbidden,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "unchanged",
    )
    try:
        compactor.observe(session_id)
        assert compactor.status(session_id)["running"] is False
        assert await compactor.finish(session_id) is False
        assert store.get("compaction", session_id) is None
        assert store.list("compaction_snapshot") == []
        assert compactor.project(session_id) == original
        assert store.messages(session_id) == original
    finally:
        await compactor.close()
        store.close()
