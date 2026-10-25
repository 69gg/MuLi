"""Resource pages through the real execution loop; no network or model."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from test_evidence_runtime_review import wire_output
from test_runtime import FakeClient
from test_skills import Host, real_runtime
from test_skills import host as host

from muli.context import EXCERPT_MARKER, ContextLimitError
from muli.providers import (
    Message,
    ModelClient,
    ModelConfig,
    ModelTurn,
    ProtocolName,
    StreamEvent,
    ToolCall,
)
from muli.providers.codecs import build_request
from muli.runtime import Runtime
from muli.skills import Skill


async def prepare_runtime(
    host: Host, protocol: ProtocolName, *, output_chars: int = 1600
) -> tuple[Runtime, Skill, list[list[Message]]]:
    host.source()
    runtime, skills, _, seen = real_runtime(host)
    host.config.settings.models.main.protocol = protocol
    host.config.settings.models.main.max_output_tokens = 4096 if protocol == "anthropic" else None
    host.config.settings.limits.max_output_chars = output_chars
    host.config.settings.limits.max_context_chars = 6000
    try:
        snapshot = await skills.prepare(host.config.settings.skills)
        skills.publish(snapshot)
    except BaseException:
        await runtime.close()
        raise
    return runtime, snapshot.entries[0], seen


def check_wire(
    config: ModelConfig,
    messages: list[Message],
    tools: list[dict[str, Any]],
    result: Message,
) -> dict[str, Any]:
    assert EXCERPT_MARKER not in result.content
    page = cast(dict[str, Any], json.loads(result.content))
    assert result.preserve_content is True
    assert result.tool_call_id is not None
    specification = next(tool for tool in tools if tool["function"]["name"] == "skill_resource")
    body = build_request(config, messages, tools, stream=False)
    assert wire_output(config.protocol, body, result.tool_call_id) == result.content
    for encoded in (json.dumps(specification), json.dumps(body)):
        assert "preserve_output" not in encoded and "preserve_content" not in encoded
    return page


def add_old_turn(runtime: Runtime) -> str:
    session_id = runtime.main_session()["id"]
    # It is removable, but packing first excerpts unprotected tool results.
    runtime.store.add_message(session_id, "user", "可移除的旧轮次" * 1000)
    runtime.store.add_message(session_id, "assistant", "旧轮次已完成")
    return cast(str, session_id)


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_resource_pages_survive_pressure_through_each_codec(
    host: Host, protocol: ProtocolName
) -> None:
    runtime, entry, seen = await prepare_runtime(host, protocol)
    text = '参考🙂"\\\r\n' * 180
    (entry.location.parent / "reference.txt").write_bytes(text.encode("utf-8"))
    revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
    pages: list[dict[str, Any]] = []

    class Reader(FakeClient):
        def __init__(self, config: ModelConfig) -> None:
            super().__init__([], seen)
            self.config = config

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            arguments: dict[str, Any] = {
                "name": entry.name,
                "revision": entry.revision,
                "path": "reference.txt",
            }
            results = [message for message in messages if message.role == "tool"]
            if results:
                page = check_wire(self.config, messages, tools, results[-1])
                offset = sum(len(previous["content"]) for previous in pages)
                assert page == {
                    "name": entry.name,
                    "skill_revision": entry.revision,
                    "revision": revision,
                    "path": "reference.txt",
                    "offset": offset,
                    "content": text[offset : page["next_offset"]],
                    "next_offset": offset + len(page["content"]),
                    "truncated": page["next_offset"] < len(text),
                    "trust": "skill_resource_not_authorization",
                }
                assert 0 < len(page["content"]) and len(results[-1].content) <= 1600
                pages.append(page)
                if not page["truncated"]:
                    yield StreamEvent("done", turn=ModelTurn(content="完整读取资源"))
                    return
                arguments.update(offset=page["next_offset"], resource_revision=page["revision"])
            assert len(pages) < 8
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[
                        ToolCall(f"resource-{len(pages)}", "skill_resource", json.dumps(arguments))
                    ]
                ),
            )

    runtime.client_factory = lambda config: cast(ModelClient, Reader(config))
    try:
        session_id = add_old_turn(runtime)
        started = await runtime.start(session_id, "逐页读取资源", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "完整读取资源"
        assert 1 < len(pages) < 8
        assert "".join(page["content"] for page in pages) == text
        stored = [
            message for message in runtime.store.messages(session_id) if message["role"] == "tool"
        ]
        assert [json.loads(message["content"]) for message in stored] == pages
        assert all(message["preserve_content"] is True for message in stored)
        assert all(message["content_provenance"] == "accepted" for message in stored)
    finally:
        await runtime.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_resource_page_that_cannot_fit_fails_before_next_request(
    host: Host, protocol: ProtocolName
) -> None:
    runtime, entry, seen = await prepare_runtime(host, protocol, output_chars=16000)
    text = "不能节选的完整资源🙂" * 700
    (entry.location.parent / "reference.txt").write_bytes(text.encode("utf-8"))

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            assert len(seen) == 1, "A required page that cannot fit must not reach the provider"
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "resource",
                            "skill_resource",
                            json.dumps(
                                {
                                    "name": entry.name,
                                    "revision": entry.revision,
                                    "path": "reference.txt",
                                }
                            ),
                        )
                    ]
                ),
            )

    runtime.client_factory = lambda _: cast(ModelClient, Reader([], seen))
    try:
        session_id = add_old_turn(runtime)
        started = await runtime.start(session_id, "完整读取资源", mode="yolo")
        with pytest.raises(ContextLimitError, match="需完整保留的工具结果"):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 5)
        assert len(seen) == 1
        stored = next(
            message for message in runtime.store.messages(session_id) if message["role"] == "tool"
        )
        page = json.loads(stored["content"])
        assert page["content"] == text and page["truncated"] is False
        assert page["offset"] == 0 and page["next_offset"] == len(text)
        assert stored["preserve_content"] is True
        assert stored["content_provenance"] == "accepted"
        assert runtime.store.get("run", started["run_id"])["status"] == "failed"
    finally:
        await runtime.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_resource_revision_change_between_rounds_is_explicit(
    host: Host, protocol: ProtocolName
) -> None:
    runtime, entry, seen = await prepare_runtime(host, protocol)
    path = entry.location.parent / "reference.txt"
    original = "续读前的资源🙂" * 400
    path.write_bytes(original.encode("utf-8"))
    pages: list[dict[str, Any]] = []

    class Reader(FakeClient):
        def __init__(self, config: ModelConfig) -> None:
            super().__init__([], seen)
            self.config = config

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            arguments: dict[str, Any] = {
                "name": entry.name,
                "revision": entry.revision,
                "path": "reference.txt",
            }
            results = [message for message in messages if message.role == "tool"]
            if results:
                page = check_wire(self.config, messages, tools, results[-1])
                pages.append(page)
                if len(pages) == 2:
                    assert page == {"status": "unavailable", "code": "resource_changed"}
                    yield StreamEvent("done", turn=ModelTurn(content="资源已变更，停止续读"))
                    return
                assert page["truncated"] is True and page["offset"] == 0
                assert page["content"] == original[: page["next_offset"]]
                assert page["revision"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
                path.write_bytes("已变更，不能拼接".encode())
                arguments.update(offset=page["next_offset"], resource_revision=page["revision"])
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[
                        ToolCall(f"resource-{len(pages)}", "skill_resource", json.dumps(arguments))
                    ]
                ),
            )

    runtime.client_factory = lambda config: cast(ModelClient, Reader(config))
    try:
        session_id = add_old_turn(runtime)
        started = await runtime.start(session_id, "读取资源并核验续读版本", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "资源已变更，停止续读"
        assert len(pages) == 2 and len(seen) == 3
        calls = [
            json.loads(call.arguments)
            for messages in seen[1:]
            for message in messages
            for call in message.tool_calls
            if call.id == "resource-1"
        ]
        assert calls == [
            {
                "name": entry.name,
                "revision": entry.revision,
                "path": "reference.txt",
                "offset": pages[0]["next_offset"],
                "resource_revision": pages[0]["revision"],
            }
        ]
    finally:
        await runtime.close()
