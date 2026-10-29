from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

from test_memory_context import fact
from test_memory_context_budget import ESCAPED_TEXT, MemoryRuntime, one_unit_reserve, pad_required
from test_memory_runtime_quality import MEMORY_HEADING, memory_records
from test_memory_runtime_quality import memory_runtime as memory_runtime
from test_runtime import FakeClient
from test_task_briefs import attach_services

from muli.context import context_size
from muli.providers import Message, ModelTurn, ToolCall


async def test_paged_commitment_read_reaches_next_request_complete_under_memory_pressure(
    memory_runtime: MemoryRuntime, tmp_path: Path
) -> None:
    runtime, application, seen = memory_runtime
    services = attach_services(runtime, tmp_path)
    query = "BRIEFREADBUDGET"
    procedure = fact(
        runtime.memory, "brief-procedure", query + " " + ESCAPED_TEXT * 900, "procedural"
    )
    maximum = runtime.config.settings.limits.max_context_chars
    reserve = one_unit_reserve(
        application.pipeline.cached_context(query, max_chars=maximum // 4 - len(MEMORY_HEADING))
    )
    item = await services.commitments.save(
        "核查任务说明",
        brief={"goal": query + " " + ESCAPED_TEXT * 6, "constraints": ["保留完整任务限制"]},
    )
    original = deepcopy(item)
    brief_text = json.dumps(item["brief"], ensure_ascii=False, sort_keys=True)
    page_limit = len(brief_text)
    expected = services.commitments.read(item["id"], limit=page_limit, revision=item["revision"])
    arguments = json.dumps(
        {"identifier": item["id"], "limit": page_limit, "revision": item["revision"]},
        ensure_ascii=False,
    )
    call = ToolCall("brief-budget-read", "commitment_read", arguments)
    native = ModelTurn(
        tool_calls=[call],
        reasoning="保留原生推理🙂\\\n",
        provider_data={"opaque": ["原生签名", "\u0001"]},
    )
    turns = [native, ModelTurn(content="已核对完整任务页")]
    runtime.client_factory = lambda _: FakeClient(turns, seen)  # type: ignore[assignment]
    skills_calls = 0

    async def skills(session_id: str, messages: list[Message]) -> list[Message]:
        nonlocal skills_calls
        skills_calls += 1
        return pad_required(messages, maximum, reserve)

    runtime.skills_context = skills
    session_id = runtime.main_session()["id"]
    receipt = await runtime.submit(query, mode="yolo")
    assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5) == "已核对完整任务页"
    assert len(seen) == skills_calls == 2
    saved = next(
        message
        for message in runtime.store.messages(session_id)
        if message.get("tool_call_id") == call.id
    )
    returned = next(message for message in seen[-1] if message.tool_call_id == call.id)
    assert saved["content_provenance"] == "accepted"
    assert json.loads(saved["content"]) == expected
    assert returned.content == saved["content"]
    assert json.loads(returned.content) == expected
    assert expected["text"] == brief_text and expected["has_more"] is False
    assert saved["preserve_content"] is True and returned.preserve_content is True
    assert returned.role == "tool"
    assert len(returned.content) <= runtime.config.settings.limits.max_output_chars
    sent_native = next(message for message in seen[-1] if message.tool_calls)
    assert sent_native.tool_calls == [call] and sent_native.tool_calls[0].arguments == arguments
    assert sent_native.reasoning == native.reasoning
    assert sent_native.provider_data == native.provider_data
    for request in seen:
        assert context_size(request) <= maximum
        assert [page["id"] for page in memory_records(request)] == [procedure["id"]]
    assert services.commitments.get(item["id"]) == original
    assert application.pipeline._worker is None
