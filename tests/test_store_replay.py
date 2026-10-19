from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.compaction import Compactor
from muli.config import CompactionSettings
from muli.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "journal.sqlite3")
    try:
        yield journal
    finally:
        journal.close()


def complete_turn(store: Store, label: str) -> list[dict[str, Any]]:
    store.add_message("session", "user", label)
    store.add_message(
        "session",
        "assistant",
        "执行计划",
        tool_calls=[
            {"id": f"{label}-a", "name": "read", "arguments": "{}"},
            {"id": f"{label}-b", "name": "check", "arguments": "{}"},
        ],
        provider_data={
            "content_blocks": [
                {"type": "thinking", "thinking": "原生思考", "signature": f"signed-{label}"}
            ]
        },
    )
    store.add_message("session", "tool", "读取结果", tool_call_id=f"{label}-a")
    store.add_message("session", "tool", "检查结果", tool_call_id=f"{label}-b")
    store.add_message("session", "assistant", "已完成")
    return store.messages("session", limit=5)


def test_full_native_turn_is_replayed_without_changing_the_archive(store: Store) -> None:
    original = complete_turn(store, "当前任务")
    projected = store.replay_messages("session", limit=5)
    assert projected == original
    projected[1]["provider_data"]["content_blocks"][0]["signature"] = "modified-copy"
    assert store.messages("session") == original


@pytest.mark.parametrize("limit", [5, 6, 7, 8, 9])
def test_tail_window_skips_the_whole_incomplete_old_turn(store: Store, limit: int) -> None:
    old = complete_turn(store, "旧任务")
    current = complete_turn(store, "当前任务")
    assert store.replay_messages("session", limit=limit) == current
    assert len(store.messages("session", limit=limit)) == limit
    assert store.message_prefix("session") == old + current


def test_compacted_user_anchor_can_precede_a_full_native_suffix(store: Store) -> None:
    original = complete_turn(store, "已保存在摘要锚点的任务")
    assert store.replay_messages("session", after=original[0]["seq"], limit=4) == original[1:]


def test_current_turn_over_row_budget_fails_explicitly_and_keeps_originals(store: Store) -> None:
    original = complete_turn(store, "不能静默遗漏的当前任务")
    with pytest.raises(ValueError, match="当前完整用户轮次.*未发送残缺上下文"):
        store.replay_messages("session", limit=4)
    assert store.message_prefix("session") == original
    assert store.messages("session", limit=4) == original[1:]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_replay_limit_must_be_a_positive_integer(store: Store, limit: int | float) -> None:
    with pytest.raises(ValueError, match="正整数"):
        store.replay_messages("session", limit=limit)  # type: ignore[arg-type]


def test_compaction_projection_marks_every_record_skipped_to_a_complete_turn(store: Store) -> None:
    old = complete_turn(store, "旧任务")
    store.put_many(
        [
            (
                "message",
                {
                    "id": f"old-output-{index}",
                    "session_id": "session",
                    "role": "assistant",
                    "content": "旧轮次的归档输出",
                },
            )
            for index in range(995)
        ]
    )
    current = complete_turn(store, "当前任务")

    async def forbidden(source: str, max_tokens: int) -> str:
        pytest.fail("历史投影不应请求模型")

    compactor = Compactor(
        store,
        forbidden,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "config",
    )
    projected = compactor.project("session")
    assert projected[0]["compaction_summary"]
    assert f"seq={old[0]['seq']} 至 seq<{current[0]['seq']}" in projected[0]["content"]
    assert projected[1:] == current
    assert store.message_prefix("session", limit=5) == old
    assert store.messages("session", limit=5) == current
