"""Literal archive locations reach real Runtime readers without replaying effects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import pytest
from test_history_evidence_runtime import completed
from test_runtime import FakeClient, make_runtime

from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.tools import Tool, parameters


def find_schema(tools: list[dict[str, Any]]) -> None:
    functions = {tool["function"]["name"]: tool["function"] for tool in tools}
    assert {"conversation_find", "conversation_read"} <= functions.keys()
    schema = functions["conversation_find"]["parameters"]
    assert set(schema["required"]) == {"session_id", "query"}
    assert set(schema["properties"]) == {"session_id", "query", "cursor", "limit"}


def preserved_reply(messages: list[Message], call_id: str) -> dict[str, Any]:
    matches = [
        message
        for message in messages
        if message.role == "tool" and message.tool_call_id == call_id
    ]
    assert len(matches) == 1
    assert matches[0].preserve_content is True
    result = json.loads(matches[0].content)
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("caller", ["main", "independent", "team", "workflow"])
async def test_find_reads_compacted_reopened_original_tool_from_every_runtime_path(
    tmp_path: Path, caller: Literal["main", "independent", "team", "workflow"]
) -> None:
    first, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("original", "capture_once", "{}")]),
            ModelTurn(content="原始结果已归档"),
        ],
    )
    calls = 0
    query = "archive-v4.tar.gz🙂"
    marker = query + "｜只回读已保存的结果，不重跑工具"
    original = {"text": "前" * 4200 + marker + "后" * 4200}

    async def capture(args: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return original

    try:
        first.config.update(
            {
                "persona": {"prompt": "test"},
                "limits": {"max_context_chars": 16000, "max_output_chars": 16000},
                "compaction": {"mode": "extractive", "trigger_ratio": 0.5},
            }
        )
        await first.reconfigure()
        first.add_tool_provider(
            lambda _: [Tool("capture_once", "受控原工具", parameters({}), capture)]
        )
        source_id = first.new_session(mode="yolo")["id"]
        assert await completed(first, source_id, "保存本次原始结果") == "原始结果已归档"
        record = next(item for item in first.store.messages(source_id) if item["role"] == "tool")
        assert record["content_provenance"] == "accepted"
        assert json.loads(record["content"]) == original
        first.compaction.observe(source_id)
        await first.compaction.finish(source_id)
        assert first.compaction.status(source_id)["covered_seq"] >= record["seq"]
    finally:
        await first.close()

    runtime, seen = make_runtime(tmp_path, [])
    responses: list[dict[str, Any]] = []
    rounds = 0
    try:
        runtime.config.update(
            {"limits": {"max_output_chars": 2400}, "compaction": {"enabled": False}}
        )
        await runtime.reconfigure()
        runtime.add_tool_provider(
            lambda _: [Tool("capture_once", "不得重跑", parameters({}), capture)]
        )
        assert runtime.tools["conversation_find"].preserve_output is True

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                seen.append(list(messages))
                find_schema(tools)
                if rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "locate-original",
                                "conversation_find",
                                json.dumps({"session_id": source_id, "query": query}),
                            )
                        ]
                    )
                elif rounds == 2:
                    found = preserved_reply(messages, "locate-original")
                    responses.append(found)
                    assert found["consistency"] == "bounded_live"
                    assert len(found["items"]) == 1 and not found["has_more"]
                    hit = found["items"][0]
                    assert hit["message_id"] == record["id"] and hit["seq"] == record["seq"]
                    assert hit["revision"] == record["evidence_revision"]
                    assert hit["role"] == "tool" and hit["content_provenance"] == "accepted"
                    assert hit["revision_is_legacy"] is False
                    raw = record["content"].encode("utf-8")
                    start, length = hit["offset_bytes"], hit["length_bytes"]
                    assert raw[start : start + length] == query.encode("utf-8")
                    assert start == len(b'{"text": "') + 4200 * len("前".encode())
                    assert hit["read_arguments"]["session_id"] == source_id
                    assert "content" not in hit and "reading_page" not in found
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "read-original",
                                "conversation_read",
                                json.dumps(hit["read_arguments"]),
                            )
                        ]
                    )
                else:
                    assert rounds == 3
                    page = preserved_reply(messages, "read-original")
                    responses.append(page)
                    assert page["content"].startswith(marker)
                    assert page["revision"] == record["evidence_revision"]
                    assert page["message_id"] == record["id"]
                    assert page["content_provenance"] == "accepted"
                    turn = ModelTurn(content="已从旧归档定位并核对原始结果")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Reader([], seen)  # type: ignore[assignment,return-value]
        if caller == "main":
            receipt = await runtime.submit("找回已知会话里的旧回执，不重做", mode="yolo")
            reader_id = receipt["session_id"]
            answer = await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5)
        elif caller == "team":
            parent_id = runtime.new_session(mode="yolo")["id"]
            agent = await runtime.team.spawn(
                "archive-reader", "只回读已存历史", parent_id=parent_id
            )
            await runtime.team.wait([agent["id"]], timeout=3)
            current = runtime.team.get(agent["id"])
            assert current["status"] == "completed", json.dumps(
                {"status": current["status"], "error": str(current.get("error", ""))[:200]},
                ensure_ascii=False,
            )
            assert current["parent_id"] == parent_id and current["mode"] == "yolo"
            reader_id, answer = agent["id"], current["result"]
        else:
            reader_id = runtime.new_session(mode="yolo")["id"]
            answer = (
                await runtime.run_step(reader_id, "只回读已存历史", "worker", "yolo")
                if caller == "workflow"
                else await completed(runtime, reader_id, "只回读已存历史")
            )
        assert answer == "已从旧归档定位并核对原始结果" and calls == 1
        assert rounds == 3 and len(responses) == 2
        assert all(len(json.dumps(response, ensure_ascii=False)) <= 2400 for response in responses)
        unchanged = runtime.store.get("message", record["id"])
        assert unchanged is not None and unchanged["content"] == record["content"]
        assert unchanged["evidence_revision"] == record["evidence_revision"]
        archived = [item for item in runtime.store.messages(reader_id) if item["role"] == "tool"]
        assert {item["tool_call_id"] for item in archived} == {"locate-original", "read-original"}
        assert all(item["content_provenance"] == "accepted" for item in archived)
        assert all(
            item["preserve_content"] is True and "reading_page" not in item for item in archived
        )
    finally:
        await runtime.close()


async def test_two_find_results_in_one_runtime_batch_keep_their_complete_json(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    results: dict[str, dict[str, Any]] = {}
    rounds = 0
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "test"},
                "limits": {"max_context_chars": 16000, "max_output_chars": 2400},
                "compaction": {"enabled": False},
            }
        )
        await runtime.reconfigure()
        source_id = runtime.new_session(mode="yolo")["id"]
        original = runtime.store.add_message(
            source_id, "user", "🙂重叠🙂重叠🙂结束 archive-v4.tar.gz"
        )
        requests = {"batch-unicode": "🙂重叠🙂", "batch-archive": "archive-v4.tar.gz"}

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                seen.append(list(messages))
                find_schema(tools)
                if rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                call_id,
                                "conversation_find",
                                json.dumps({"session_id": source_id, "query": query}),
                            )
                            for call_id, query in requests.items()
                        ]
                    )
                else:
                    assert rounds == 2
                    for call_id, query in requests.items():
                        result = preserved_reply(messages, call_id)
                        results[call_id] = result
                        assert not result["has_more"] and result["omitted_total"] == 0
                        assert all(item["message_id"] == original["id"] for item in result["items"])
                        for item in result["items"]:
                            start, length = item["offset_bytes"], item["length_bytes"]
                            assert (
                                original["content"].encode()[start : start + length]
                                == query.encode()
                            )
                    assert len(results["batch-unicode"]["items"]) == 2
                    assert len(results["batch-archive"]["items"]) == 1
                    turn = ModelTurn(content="同批的两个完整位置回包均已核对")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Reader([], seen)  # type: ignore[assignment,return-value]
        reader_id = runtime.new_session(mode="yolo")["id"]
        assert (
            await completed(runtime, reader_id, "同时查两个字面片段")
            == "同批的两个完整位置回包均已核对"
        )
        archived = [item for item in runtime.store.messages(reader_id) if item["role"] == "tool"]
        assert len(archived) == 2 and rounds == 2
        for item in archived:
            assert item["content_provenance"] == "accepted" and item["preserve_content"] is True
            assert "reading_page" not in item
            assert json.loads(item["content"]) == results[item["tool_call_id"]]
    finally:
        await runtime.close()


async def test_find_preserves_sources_from_trusted_web_and_channel_submit_without_sending(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content="网页输入已归档"), ModelTurn(content="渠道输入已归档")]
    )
    query = "跨来源旧记录🙂"
    web_content, channel_content = query + "网页原文", query + "渠道原文"
    rounds = 0
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "test"},
                "limits": {"max_context_chars": 16000, "max_output_chars": 4000},
                "compaction": {"enabled": False},
            }
        )
        await runtime.reconfigure()
        web = await runtime.submit(web_content, mode="yolo")
        assert await asyncio.wait_for(runtime.wait(web["run_id"]), 5) == "网页输入已归档"
        # This is the trusted host submit boundary. No adapter, polling or
        # external channel transport is installed or exercised by this fixture.
        channel = await runtime.submit(
            channel_content,
            mode="yolo",
            source="channel:telegram:bot",
            route={"name": "telegram", "target": "100", "kind": "private", "sender_id": "100"},
        )
        assert await asyncio.wait_for(runtime.wait(channel["run_id"]), 5) == "渠道输入已归档"
        main_id = web["session_id"]
        assert channel["session_id"] == main_id
        expected = {
            web["message"]["id"]: ("web", web_content),
            channel["message"]["id"]: ("channel:telegram:bot", channel_content),
        }

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                seen.append(list(messages))
                find_schema(tools)
                if rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "find-sources",
                                "conversation_find",
                                json.dumps({"session_id": main_id, "query": query}),
                            )
                        ]
                    )
                elif rounds == 2:
                    found = preserved_reply(messages, "find-sources")
                    assert {item["message_id"] for item in found["items"]} == set(expected)
                    assert not found["has_more"] and found["omitted_total"] == 0
                    calls: list[ToolCall] = []
                    for item in found["items"]:
                        source, _ = expected[item["message_id"]]
                        assert item["role"] == "user" and item["principal"] == "owner"
                        assert item["source"] == source and item["offset_bytes"] == 0
                        calls.append(
                            ToolCall(
                                "read-" + item["message_id"],
                                "conversation_read",
                                json.dumps(item["read_arguments"]),
                            )
                        )
                    turn = ModelTurn(tool_calls=calls)
                else:
                    assert rounds == 3
                    for identifier, (source, content) in expected.items():
                        page = preserved_reply(messages, "read-" + identifier)
                        assert page["message_id"] == identifier and page["content"] == content
                        assert page["source"] == source and page["principal"] == "owner"
                    turn = ModelTurn(content="已按原来源核对两条历史，未发送渠道消息")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Reader([], seen)  # type: ignore[assignment,return-value]
        receipt = await runtime.submit("只回读两条既有输入，不发送消息", mode="yolo")
        assert (
            await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5)
            == "已按原来源核对两条历史，未发送渠道消息"
        )
        started = [
            event["data"]["name"]
            for event in runtime.store.events(session_id=main_id)
            if event["type"] == "tool.started"
        ]
        assert started == ["conversation_find", "conversation_read", "conversation_read"]
        assert rounds == 3
    finally:
        await runtime.close()
