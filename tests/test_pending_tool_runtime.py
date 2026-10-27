from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.context import ContextLimitError, context_size
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.tools import Tool, parameters


@pytest.mark.parametrize("compaction_enabled", [False, True], ids=["no-compaction", "compaction"])
@pytest.mark.parametrize(
    "over_budget", [False, True], ids=["fits-after-old-turn", "required-overflow"]
)
async def test_owner_input_during_tool_wait_keeps_unconsumed_json_complete(
    tmp_path: Path, compaction_enabled: bool, over_budget: bool
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    budget = 8000
    old_marker = "OLD-REMOVABLE-TURN:"
    old_reply = "上一轮已经结束。"
    supplement = "主人补充：先核对刚返回的完整证据，不要重复读取。"
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    body_chars = budget if over_budget else budget // 2
    unit = "证据中文🙂"
    result = {
        "content": "BEGIN|" + (unit * (body_chars // len(unit) + 1))[:body_chars] + "|END",
        "revision": "stable-test-revision",
    }
    encoded = json.dumps(result, ensure_ascii=False)
    call = ToolCall("pending-result", "bounded_evidence", "{}")

    async def evidence(args: dict[str, Any]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return result

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            if len(seen) == 1:
                assert "bounded_evidence" in [tool["function"]["name"] for tool in tools]
                turn = ModelTurn(tool_calls=[call])
            else:
                turn = ModelTurn(content="补充与证据已核对")
            yield StreamEvent("done", turn=turn)

    try:
        runtime.config.update(
            {
                "persona": {"prompt": "你是简短的测试助手。"},
                "memory": {"enabled": False},
                "updates": {"check_on_start": False},
                "limits": {"max_context_chars": budget, "max_output_chars": budget * 2},
                "compaction": {
                    "enabled": compaction_enabled,
                    "mode": "extractive",
                    "trigger_ratio": 0.25,
                    "target_ratio": 0.2,
                },
            }
        )
        await runtime.reconfigure()
        runtime.add_tool_provider(
            lambda _: [
                Tool(
                    "bounded_evidence",
                    "只读返回有限证据，不产生外部副作用",
                    parameters({}),
                    evidence,
                    preserve_output=True,
                )
            ]
        )
        runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment]
        session_id = runtime.main_session()["id"]
        old = runtime.store.add_message(
            session_id, "user", old_marker + "旧" * (budget // 2), source="web", principal="owner"
        )
        runtime.store.add_message(session_id, "assistant", old_reply)
        started = await runtime.submit("读取有限证据，然后等我补充。", mode="yolo")
        await asyncio.wait_for(entered.wait(), 2)
        running = runtime.tasks[session_id]
        assert any(message.source_message_id == old["id"] for message in seen[0])

        # Establish real request pressure without altering limits or simulating
        # the input-claiming path. Only the next submit below introduces input.
        pressure = [
            *seen[0],
            Message(role="assistant", tool_calls=[call]),
            Message(role="tool", content=encoded, tool_call_id=call.id, preserve_content=True),
            Message(role="user", content=supplement),
        ]
        without_old = [
            message
            for message in pressure
            if message.source_message_id != old["id"]
            and not (message.role == "assistant" and message.content == old_reply)
        ]
        assert context_size(pressure) > budget
        assert (context_size(without_old) > budget) is over_budget

        queued = await runtime.submit(supplement, mode="yolo")
        assert queued["session_id"] == session_id and queued["queued"] is True
        assert not running.done() and calls == 1
        release.set()
        if over_budget:
            with pytest.raises(ContextLimitError):
                await asyncio.wait_for(asyncio.shield(running), 2)
            assert len(seen) == 1, (
                "required JSON must not be damaged to issue another model request"
            )
            run = runtime.store.get("run", started["run_id"])
            assert run is not None and run["status"] == "failed"
        else:
            assert await asyncio.wait_for(asyncio.shield(running), 2) == "补充与证据已核对"
            assert len(seen) == 2
            delivered = seen[1]
            assert any(
                message.role == "user" and message.content == supplement for message in delivered
            )
            reply = next(message for message in delivered if message.tool_call_id == call.id)
            assert reply.content == encoded and json.loads(reply.content) == result
            assert reply.preserve_content is True and context_size(delivered) <= budget
            assert not any(message.source_message_id == old["id"] for message in delivered)

        stored = next(
            message
            for message in runtime.store.messages(session_id)
            if message.get("tool_call_id") == call.id
        )
        assert stored["preserve_content"] is True and stored["content_provenance"] == "accepted"
        assert json.loads(stored["content"]) == result and calls == 1
        assert any(message["id"] == old["id"] for message in runtime.store.messages(session_id))
    finally:
        release.set()
        await runtime.close()
