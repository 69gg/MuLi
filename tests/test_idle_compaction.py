from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.config import CompactionSettings
from muli.permissions import Mode
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.tools import Tool, parameters


def seed(runtime: Runtime, session_id: str, size: int = 600) -> list[dict[str, Any]]:
    runtime.store.add_message(session_id, "user", "甲" * size)
    runtime.store.add_message(session_id, "assistant", "乙" * size)
    runtime.store.add_message(session_id, "user", "后续问题")
    runtime.store.add_message(session_id, "assistant", "最后回复")
    return runtime.store.messages(session_id)


async def test_idle_scan_requires_both_thresholds_and_skips_recent_or_queued_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    started = 100_000.0
    clock = started
    monkeypatch.setattr("muli.runtime.now", lambda: clock)
    monkeypatch.setattr("muli.store.now", lambda: clock)
    runtime.config.update(
        {"compaction": {"mode": "extractive"}, "limits": {"max_context_chars": 16_000}}
    )
    try:
        paused = runtime.new_session()
        eligible = paused["id"]
        paused.pop("updated_at")
        runtime.store.put("session", {**paused, "status": "paused"})
        original = seed(runtime, eligible)
        short = runtime.new_session()["id"]
        seed(runtime, short, size=1)
        queued = runtime.new_session()["id"]
        seed(runtime, queued)
        runtime.store.enqueue_input({"id": "pending", "session_id": queued}, max_pending=1)
        recent_record = runtime.new_session()["id"]
        seed(runtime, recent_record)
        running = runtime.new_session()
        seed(runtime, running["id"])
        runtime.store.put("session", {**running, "status": "running"})
        clock = started + 3600 - 1
        await runtime._compact_idle_sessions()
        assert runtime.store.list("compaction") == []
        runtime.store.add_message(recent_record, "user", "刚收到")
        clock += 1
        runtime.maintenance.admission.pause()
        await runtime._compact_idle_sessions()
        assert runtime.store.list("compaction") == []
        runtime.maintenance.admission.release()
        await runtime._compact_idle_sessions()
        assert [item["id"] for item in runtime.store.list("compaction")] == [eligible]
        assert runtime.store.messages(eligible) == original
        projected = runtime.compaction.project(eligible)
        assert projected[0]["compaction_summary"]
        assert projected[-1]["id"] == original[-1]["id"]
        await runtime._compact_idle_sessions()
        assert runtime.compaction.status(eligible)["generation"] == 1
        assert seen == []
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"mode": "extractive"}, True),
        ({"mode": "extractive", "idle_trigger_percent": 20}, False),
        ({"mode": "extractive", "idle_after_hours": 0}, False),
        ({"enabled": False}, False),
    ],
)
async def test_idle_scan_uses_model_context_window_and_current_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any],
    expected: bool,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"models": {"main": {"context_window": 8192}}, "compaction": settings})
    try:
        session = runtime.new_session()
        identifier = session["id"]
        # Including message metadata, this is between 10% and 20% of the model window.
        seed(runtime, identifier, size=650)
        future = session["updated_at"] + 3 * 3600
        monkeypatch.setattr("muli.runtime.now", lambda: future)
        await runtime._compact_idle_sessions()
        assert (runtime.store.get("compaction", identifier) is not None) == expected
    finally:
        await runtime.close()


async def test_idle_summary_finishes_while_a_new_turn_is_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "limits": {"max_context_chars": 8000},
            "memory": {"auto_extract": False},
            "extensions": {"watch_enabled": False},
        }
    )
    summarizing, complete_summary = asyncio.Event(), asyncio.Event()
    streaming, complete_turn = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            summarizing.set()
            await complete_summary.wait()
            return ModelTurn(content="空闲时生成的摘要")

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            streaming.set()
            await complete_turn.wait()
            yield StreamEvent("done", turn=ModelTurn(content="继续后的答复"))

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    scan: asyncio.Task[None] | None = None
    try:
        session = runtime.main_session()
        identifier = session["id"]
        original = seed(runtime, identifier)
        future = session["updated_at"] + 3 * 3600
        monkeypatch.setattr("muli.runtime.now", lambda: future)
        await runtime.startup()
        scan = runtime._idle_compaction_task
        assert scan is not None
        await asyncio.wait_for(summarizing.wait(), 5)
        assert runtime.store.get("compaction", identifier) is None

        started = await runtime.submit("恢复对话", mode="yolo")
        await asyncio.wait_for(streaming.wait(), 5)
        summary = runtime.compaction._tasks[identifier]
        complete_summary.set()
        await asyncio.wait_for(asyncio.shield(summary), 5)
        assert runtime.compaction.status(identifier)["ready"]
        assert runtime.store.get("compaction", identifier) is None
        assert seen[0][-1].content == "恢复对话"

        complete_turn.set()
        assert await runtime.wait(started["run_id"]) == "继续后的答复"
        projected = runtime.compaction.project(identifier)
        assert "空闲时生成的摘要" in projected[0]["content"]
        assert [item["content"] for item in projected[-2:]] == ["恢复对话", "继续后的答复"]
        assert runtime.store.messages(identifier)[: len(original)] == original
    finally:
        complete_summary.set()
        complete_turn.set()
        await runtime.close()
    assert scan is not None and scan.cancelled()


async def test_shutdown_settles_an_inflight_idle_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "limits": {"max_context_chars": 8000},
            "extensions": {"watch_enabled": False},
        }
    )
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closing: asyncio.Task[None] | None = None

    class Client(FakeClient):
        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return ModelTurn(content="迟到摘要")
            raise AssertionError("summary should wait for cancellation")

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        session = runtime.main_session()
        identifier = session["id"]
        original = seed(runtime, identifier)
        future = session["updated_at"] + 3 * 3600
        monkeypatch.setattr("muli.runtime.now", lambda: future)
        await runtime.startup()
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(runtime.close())
        await asyncio.wait_for(cancelled.wait(), 5)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 5)
        assert runtime.compaction.idle
    finally:
        release.set()
        await runtime.close()

    reopened, _ = make_runtime(tmp_path, [])
    try:
        assert reopened.store.get("compaction", identifier) is None
        assert reopened.store.messages(identifier) == original
    finally:
        await reopened.close()


@pytest.mark.parametrize("mode", ["ask", "auto", "yolo"])
async def test_model_can_schedule_compaction_without_approval_below_threshold(
    tmp_path: Path,
    mode: Mode,
) -> None:
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("compact-call", "compact", "{}")]),
            ModelTurn(content="已安排压缩"),
        ],
    )
    runtime.config.update({"compaction": {"mode": "extractive", "idle_after_hours": 0}})
    try:
        identifier = runtime.main_session()["id"]
        original = seed(runtime, identifier, size=30)
        other = runtime.new_session()["id"]
        seed(runtime, other)
        started = await runtime.submit("请整理早些时候的上下文", mode=mode)
        async with asyncio.timeout(5):
            assert await runtime.wait(started["run_id"]) == "已安排压缩"
        assert runtime.store.list("approval") == []
        assert not any(event["type"] == "tool.reviewed" for event in runtime.store.events())
        assert seen[1][-1].role == "tool"
        assert seen[1][-1].tool_call_id == "compact-call"
        assert json.loads(seen[1][-1].content) == {"status": "scheduled"}
        saved = runtime.store.messages(identifier)
        assert saved[: len(original)] == original
        assert runtime.compaction.status(identifier)["covered_seq"] < saved[-3]["seq"]
        assert runtime.compaction.status(identifier)["generation"] == 1
        assert runtime.store.get("compaction", other) is None
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    ("enabled", "receipt"),
    [(True, {"status": "nothing_to_compact"}), (False, {"error": "工具未启用"})],
)
async def test_compact_with_no_closed_history(
    tmp_path: Path, enabled: bool, receipt: dict[str, str]
) -> None:
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("compact-call", "compact", "{}")]),
            ModelTurn(content="收到"),
        ],
    )
    runtime.config.update({"compaction": {"enabled": enabled}})
    try:
        started = await runtime.submit("整理历史", mode="yolo")
        assert await runtime.wait(started["run_id"]) == "收到"
        assert json.loads(seen[1][-1].content) == receipt
        assert runtime.compaction.idle
        assert runtime.store.list("compaction") == []
    finally:
        await runtime.close()


@pytest.mark.parametrize("enabled", [True, False])
async def test_replacing_compact_does_not_inherit_approval_exemption(
    tmp_path: Path, enabled: bool
) -> None:
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("replacement", "compact", "{}")]),
            ModelTurn(content="已收到拒绝回执"),
        ],
    )

    async def replacement(arguments: dict[str, Any]) -> None:
        pytest.fail("未获批准的同名工具不得执行")

    try:
        runtime.config.update({"compaction": {"enabled": enabled}})
        runtime.add_tool_provider(
            lambda _: [Tool("compact", "替换工具", parameters({}), replacement, "write")],
            replace=True,
        )
        started = await runtime.submit("整理会话", mode="ask")
        async with asyncio.timeout(5):
            while not runtime.permissions.pending:
                await asyncio.sleep(0)
            (approval,) = runtime.permissions.pending
            assert runtime.store.get("approval", approval)["tool"] == "compact"
            await runtime.permissions.resolve(approval, "reject")
            assert await runtime.wait(started["run_id"]) == "已收到拒绝回执"
        assert json.loads(seen[1][-1].content)["code"] == "permission_denied"
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "fields", [{"idle_after_hours": -1}, {"idle_trigger_percent": 0}, {"idle_trigger_percent": 101}]
)
def test_idle_compaction_rejects_out_of_range_settings(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CompactionSettings.model_validate(fields)
