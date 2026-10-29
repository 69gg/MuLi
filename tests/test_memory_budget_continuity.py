from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_attachment_memory_policy import offline as offline
from test_compaction import history
from test_document_memory import tool
from test_memory_context import fact
from test_memory_context_budget import ESCAPED_TEXT, MemoryRuntime, one_unit_reserve, pad_required
from test_memory_runtime_quality import (
    MEMORY_HEADING,
    memory_payload,
    memory_records,
)
from test_memory_runtime_quality import (
    memory_runtime as memory_runtime,
)
from test_runtime import FakeClient

from muli.compaction import digest
from muli.config import MemorySettings, Persona
from muli.context import context_size
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.tools import Tool, parameters


async def test_new_owner_input_during_tool_wait_keeps_memory_and_complete_accepted_evidence(
    memory_runtime: MemoryRuntime,
) -> None:
    runtime, application, seen = memory_runtime
    query = "PENDINGBUDGET"
    procedure = fact(
        runtime.memory, "pending-procedure", query + " " + ESCAPED_TEXT * 900, "procedural"
    )
    maximum = runtime.config.settings.limits.max_context_chars
    reserve = one_unit_reserve(
        application.pipeline.cached_context(query, max_chars=maximum // 4 - len(MEMORY_HEADING))
    )
    evidence = {"content": ESCAPED_TEXT * 70, "revision": "pending-evidence-v1"}
    encoded = json.dumps(evidence, ensure_ascii=False)
    call = ToolCall("pending-budget-read", "budget_evidence", "{}")
    native = ModelTurn(
        tool_calls=[call],
        reasoning="保留完整推理🙂\\",
        provider_data={"opaque": ["原生签名", "\n\u0001"]},
    )
    turns = [native, ModelTurn(content="已核对补充与完整证据")]
    runtime.client_factory = lambda _: FakeClient(turns, seen)  # type: ignore[assignment]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    skills_calls = 0

    async def read(args: dict[str, Any]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return evidence

    async def skills(session_id: str, messages: list[Message]) -> list[Message]:
        nonlocal skills_calls
        skills_calls += 1
        return pad_required(messages, maximum, reserve)

    runtime.add_tool_provider(
        lambda _: [
            Tool("budget_evidence", "返回完整本地证据", parameters({}), read, preserve_output=True)
        ]
    )
    runtime.skills_context = skills
    session_id = runtime.main_session()["id"]
    receipt = await runtime.start(session_id, query, mode="yolo")
    running = runtime.tasks[session_id]
    supplement = query + " 主人补充：先核对本次完整回执，保留原文，不重复读取。"
    try:
        await asyncio.wait_for(entered.wait(), 5)
        queued = await runtime.submit(supplement, mode="yolo")
        assert queued["session_id"] == session_id and queued["queued"] is True
        assert not running.done() and calls == 1
        release.set()
        assert await asyncio.wait_for(asyncio.shield(running), 5) == "已核对补充与完整证据"
    finally:
        release.set()
    run = runtime.store.get("run", receipt["run_id"])
    assert run is not None and run["status"] == "completed"
    assert len(seen) == skills_calls == 2 and calls == 1
    for request in seen:
        assert context_size(request) <= maximum
        assert [item["id"] for item in memory_records(request)] == [procedure["id"]]
    request = seen[-1]
    assert any(message.role == "user" and message.content == supplement for message in request)
    returned = next(message for message in request if message.tool_call_id == call.id)
    assert returned.content == encoded and json.loads(returned.content) == evidence
    assert returned.preserve_content is True
    sent_native = next(message for message in request if message.tool_calls)
    assert sent_native.tool_calls == [call]
    assert sent_native.reasoning == native.reasoning
    assert sent_native.provider_data == native.provider_data
    saved = next(
        item for item in runtime.store.messages(session_id) if item.get("tool_call_id") == call.id
    )
    assert saved["content"] == encoded and saved["content_provenance"] == "accepted"
    assert saved["preserve_content"] is True


def assert_accepted_delivery(
    rig: MemoryRig, result: dict[str, Any], request: list[Message]
) -> dict[str, Any]:
    record = next(
        item
        for item in reversed(rig.runtime.store.messages(rig.runtime.main_session()["id"]))
        if item["role"] == "tool"
    )
    assert record["content_provenance"] == "accepted" and record["preserve_content"] is True
    assert json.loads(record["content"]) == result
    delivered = [item for item in request if item.tool_call_id == record["tool_call_id"]]
    assert len(delivered) == 1 and delivered[0].content == record["content"]
    assert delivered[0].preserve_content is True
    return record


async def test_default_auto_memory_survives_real_compaction_normal_close_and_source_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic = "COMPACTORCHID"
    reason = topic + " 核验必须先恢复归档清单，再检查唯一镜片记录。"
    text = "外部原文，仅供参考。\n" + "普通背景资料。🙂\n" * 40 + reason
    seen: list[list[Message]] = []
    original_stream = FakeClient.stream

    async def capture(
        client: FakeClient, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        seen.append(deepcopy(messages))
        async for event in original_stream(client, messages, tools):
            yield event

    monkeypatch.setattr(FakeClient, "stream", capture)
    async with actual_app(tmp_path) as rig:
        settings = rig.runtime.config.settings
        assert settings.persona == Persona()
        assert settings.memory.enabled is True
        assert settings.memory.context_max_chars == MemorySettings().context_max_chars > 0
        assert settings.limits.max_context_chars == 16000
        assert settings.limits.max_output_chars == 6000
        profile = fact(
            rig.runtime.memory, "continuity-profile", "我偏好先核查完整原文。", "preference"
        )
        item = await rig.upload(text.encode("utf-8"))
        imported = await tool(rig, "memory_import_attachment", id=item["id"])
        assert "error" not in imported, imported
        accepted = assert_accepted_delivery(rig, imported, seen[-1])
        source_id = imported["source_id"]
        job = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), 4)
        assert job is not None and job["state"] == "done"
        source = rig.runtime.memory.source(source_id)
        assert source is not None and source["content"] == text
        assert source["trusted_owner"] is False and source["role"] == "tool"
        frozen_source, frozen_job = deepcopy(source), deepcopy(job)
        main_id = rig.runtime.main_session()["id"]
        original = history(rig.runtime.store, main_id)
        rig.runtime.config.update(
            {
                "compaction": {
                    "enabled": True,
                    "mode": "extractive",
                    "trigger_ratio": 0.25,
                    "target_ratio": 0.2,
                }
            }
        )
        rig.runtime.compaction.observe(main_id)
        await asyncio.wait_for(rig.runtime.compaction.finish(main_id), 4)
        snapshot = rig.runtime.store.get("compaction", main_id)
        assert snapshot is not None and snapshot["kind"] == "extractive"
        assert snapshot["covered_seq"] >= accepted["seq"]
        prefix = [item for item in original if item["seq"] <= snapshot["covered_seq"]]
        assert snapshot["prefix_digest"] == digest(prefix)
        persisted = rig.runtime.store.get("compaction_snapshot", snapshot["snapshot_id"])
        assert persisted is not None and persisted["prefix_digest"] == digest(prefix)
        assert rig.runtime.store.messages(main_id) == original
        projected = rig.runtime.compaction.project(main_id)
        assert any(item.get("compaction_summary") for item in projected)
        assert accepted["id"] not in {item["id"] for item in projected}
        status = rig.runtime.compaction.status(main_id)
        assert status["running"] is False and status["ready"] is False
        replay = await rig.runtime._next_context(main_id)
        injected_profile = next(
            item for item in memory_records(replay) if item.get("id") == profile["id"]
        )
        for key in ("content", "layer", "source_id", "trusted_owner"):
            assert injected_profile[key] == profile[key]
        assert context_size(replay) <= settings.limits.max_context_chars
        frozen_snapshot = deepcopy(snapshot)
    assert rig.runtime.compaction.idle and not rig.runtime.tasks

    async with actual_app(tmp_path) as reopened:
        settings = reopened.runtime.config.settings
        assert settings.persona == Persona()
        assert settings.memory.enabled is True
        assert settings.memory.context_max_chars == MemorySettings().context_max_chars > 0
        assert settings.limits.max_context_chars == 16000
        assert settings.limits.max_output_chars == 6000
        assert reopened.runtime.store.get("compaction", main_id) == frozen_snapshot
        assert reopened.runtime.store.messages(main_id) == original
        assert reopened.runtime.memory.source(source_id) == frozen_source
        assert reopened.memory.pipeline.job(source_id) == frozen_job
        found = await tool(
            reopened, "memory_query", query=topic, hops=0, limit=1, source_ids=[source_id]
        )
        assert "error" not in found, found
        assert_accepted_delivery(reopened, found, seen[-1])
        assert any(
            item["source_id"] == source_id and reason in item["content"]
            for item in found["memories"]
        )
        reference = next(item for item in found["sources"] if item["id"] == source_id)
        offset = 0
        pieces: list[str] = []
        for _ in range(16):
            page = await tool(
                reopened,
                "memory_source",
                id=source_id,
                revision=reference["digest"],
                offset_bytes=offset,
                length_bytes=256,
            )
            assert "error" not in page, page
            assert_accepted_delivery(reopened, page, seen[-1])
            assert page["revision"] == reference["digest"] and page["offset_bytes"] == offset
            assert page["trusted_owner"] is False
            pieces.append(page["content"])
            assert page["next_offset_bytes"] > offset
            offset = page["next_offset_bytes"]
            if not page["has_more"]:
                break
        else:
            pytest.fail("原文在有界分页内未完整返回")
        assert len(pieces) > 1
        assert "".join(pieces) == text and offset == len(text.encode("utf-8"))
        assert reopened.runtime.memory.source(source_id) == frozen_source
        assert reopened.memory.pipeline.job(source_id) == frozen_job
        assert all(context_size(request) <= 16000 for request in seen)
        assert any(
            MEMORY_HEADING in message.content and memory_payload(request)
            for request in seen
            for message in request
        )
    assert reopened.runtime.compaction.idle and not reopened.runtime.tasks
