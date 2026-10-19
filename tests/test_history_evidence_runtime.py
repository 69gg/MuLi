"""Archive evidence reaches real model requests without replaying old effects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.context import ContextLimitError
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.tools import Tool, parameters


async def completed(runtime: Runtime, session_id: str, prompt: str) -> str:
    started = await runtime.start(session_id, prompt, mode="yolo")
    return await asyncio.wait_for(runtime.wait(started["run_id"]), 5)


@pytest.mark.parametrize("caller", ["main", "independent", "team", "workflow"])
async def test_compacted_reopened_archive_can_be_read_from_every_runtime_path(
    tmp_path: Path, caller: str
) -> None:
    first, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("original", "capture_once", "{}")]),
            ModelTurn(content="归档完成"),
        ],
    )
    calls = 0
    marker = "中段的唯一证据🙂\u0000不可重跑原工具"
    stored_marker = json.dumps(marker, ensure_ascii=False)[1:-1]
    original = {"text": "前" * 4200 + marker + "后" * 4200}

    async def capture(args: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return original

    try:
        first.config.update(
            {
                "limits": {"max_context_chars": 16000, "max_output_chars": 16000},
                "compaction": {"mode": "extractive", "trigger_ratio": 0.5},
            }
        )
        await first.reconfigure()
        first.add_tool_provider(lambda _: [Tool("capture_once", "假工具", parameters({}), capture)])
        source_id = first.new_session(mode="yolo")["id"]
        assert await completed(first, source_id, "保存这份结果") == "归档完成"
        record = next(item for item in first.store.messages(source_id) if item["role"] == "tool")
        first.compaction.observe(source_id)
        await first.compaction.finish(source_id)
        assert first.compaction.status(source_id)["covered_seq"] >= record["seq"]
    finally:
        await first.close()

    runtime, seen = make_runtime(tmp_path, [])
    pages: list[dict[str, Any]] = []
    try:
        runtime.config.update(
            {"limits": {"max_output_chars": 1000}, "compaction": {"enabled": False}}
        )
        await runtime.reconfigure()
        runtime.add_tool_provider(
            lambda _: [Tool("capture_once", "不得重跑", parameters({}), capture)]
        )

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                seen.append(messages)
                replies = [message for message in messages if message.role == "tool"]
                if not replies:
                    args: dict[str, Any] = {
                        "session_id": source_id,
                        "after": record["seq"] - 1,
                        "limit": 1,
                    }
                else:
                    page = json.loads(replies[-1].content)
                    pages.append(page)
                    if "records" in page:
                        entry = page["records"][0]
                        assert entry["id"] == record["id"] and entry["content_truncated"]
                        args = {
                            "session_id": source_id,
                            "message_id": entry["id"],
                            "revision": entry["revision"],
                            "offset_bytes": len(b'{"text": "') + 4200 * len("前".encode()),
                            "length_bytes": 160,
                        }
                    else:
                        assert stored_marker in page["content"]
                        assert page["content_provenance"] == "accepted"
                        yield StreamEvent("done", turn=ModelTurn(content="已经核对归档中段"))
                        return
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            ToolCall(f"read-{len(pages)}", "conversation_read", json.dumps(args))
                        ]
                    ),
                )

        runtime.client_factory = lambda _: Reader([], seen)  # type: ignore[assignment,return-value]
        if caller == "main":
            receipt = await runtime.submit("核查已有证据", mode="yolo")
            answer = await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5)
        elif caller == "team":
            parent = runtime.new_session(mode="yolo")["id"]
            agent = await runtime.team.spawn("evidence", "核查已有证据", parent_id=parent)
            await runtime.team.wait([agent["id"]], timeout=3)
            answer = runtime.team.get(agent["id"])["result"]
        else:
            reader_id = runtime.new_session(mode="yolo")["id"]
            answer = (
                await runtime.run_step(reader_id, "核查已有证据", "worker", "yolo")
                if caller == "workflow"
                else await completed(runtime, reader_id, "核查已有证据")
            )
        assert answer == "已经核对归档中段" and calls == 1
        assert len(pages) == 2
        assert all(len(json.dumps(page, ensure_ascii=False)) <= 1000 for page in pages)
        assert runtime.store.get("message", record["id"])["content"] == record["content"]
        matches = [
            message for messages in seen for message in messages if message.tool_call_id == "read-1"
        ]
        assert matches
        assert all(
            message.role == "tool" and stored_marker in json.loads(message.content)["content"]
            for message in matches
        )
    finally:
        await runtime.close()


async def test_runtime_records_excerpt_provenance_without_claiming_lost_tail(
    tmp_path: Path,
) -> None:
    turns = [
        ModelTurn(tool_calls=[ToolCall("large", "large_read", "{}")]),
        ModelTurn(content="完成"),
    ]
    runtime, _ = make_runtime(tmp_path, turns)

    async def large(args: dict[str, Any]) -> Any:
        return {"text": "x" * 2000 + "UNSAVED_TAIL"}

    try:
        runtime.config.update(
            {"limits": {"max_output_chars": 1000}, "compaction": {"enabled": False}}
        )
        await runtime.reconfigure()
        runtime.add_tool_provider(lambda _: [Tool("large_read", "假工具", parameters({}), large)])
        session_id = runtime.new_session(mode="yolo")["id"]
        await completed(runtime, session_id, "读取")
        record = next(item for item in runtime.store.messages(session_id) if item["role"] == "tool")
        assert record["content_provenance"] == "runtime_excerpt"
        assert "UNSAVED_TAIL" not in record["content"]
        page = await runtime.tools["conversation_read"].handler(
            {"session_id": session_id, "message_id": record["id"]}
        )
        assert page["content_provenance"] == "runtime_excerpt"
        assert "UNSAVED_TAIL" not in json.dumps(page)
    finally:
        await runtime.close()


async def test_untrusted_result_shape_cannot_opt_into_context_preservation(tmp_path: Path) -> None:
    runtime, seen = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("fake", "conversation_read", "{}")]),
            ModelTurn(content="完成"),
        ],
    )

    async def fake(args: dict[str, Any]) -> Any:
        return {"preserve_content": True, "preserve_output": True, "content": "x" * 2000}

    try:
        runtime.config.update({"persona": {"prompt": "test"}, "compaction": {"enabled": False}})
        await runtime.reconfigure()
        runtime.add_tool_provider(
            lambda _: [Tool("conversation_read", "替换工具", parameters({}), fake)], replace=True
        )
        session_id = runtime.new_session(mode="yolo")["id"]
        await completed(runtime, session_id, "读取")
        message = next(item for item in seen[-1] if item.role == "tool")
        assert message.preserve_content is False
        saved = next(item for item in runtime.store.messages(session_id) if item["role"] == "tool")
        assert saved["preserve_content"] is False
    finally:
        await runtime.close()


async def test_latest_evidence_json_survives_real_context_pressure_and_restore(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "test"},
                "limits": {"max_context_chars": 6000, "max_output_chars": 2000},
                "compaction": {"enabled": False},
            }
        )
        await runtime.reconfigure()
        source_id = runtime.new_session(mode="yolo")["id"]
        source = runtime.store.add_message(source_id, "user", "long evidence " * 400)
        reader_id = runtime.new_session(mode="yolo")["id"]
        runtime.store.add_message(reader_id, "user", "阅读证据")
        runtime.store.add_message(
            reader_id,
            "assistant",
            "",
            tool_calls=[{"id": "old", "name": "inspect", "arguments": "{}"}],
        )
        runtime.store.add_message(reader_id, "tool", "old text " * 500, tool_call_id="old")
        runtime.store.add_message(
            reader_id,
            "assistant",
            "",
            tool_calls=[{"id": "read", "name": "conversation_read", "arguments": "{}"}],
        )
        page = await runtime.tools["conversation_read"].handler(
            {"session_id": source_id, "message_id": source["id"]}
        )
        content = json.dumps(page, ensure_ascii=False)
        runtime.store.add_message(
            reader_id, "tool", content, tool_call_id="read", preserve_content=True
        )
        messages = await runtime._next_context(reader_id)
        assert messages[-1].content == content
        assert json.loads(messages[-1].content)["next_offset_bytes"] == page["next_offset_bytes"]
        assert messages[-3].content != "old text " * 500
        runtime.config.update({"persona": {"prompt": "p" * 5900}})
        with pytest.raises(ContextLimitError):
            await runtime._next_context(reader_id)
    finally:
        await runtime.close()
