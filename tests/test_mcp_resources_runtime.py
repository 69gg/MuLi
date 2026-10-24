"""Actual Runtime permission, packing and generation gates, with fake peers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from test_evidence_runtime_review import wire_output
from test_extensions import setup_runtime
from test_mcp_resources import ResourceClient, resource
from test_mcp_resources import offline as offline
from test_runtime import FakeClient as FakeModel

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


async def resource_runtime(
    path: Path, protocol: ProtocolName = "openai_chat", *, output_chars: int = 1600
) -> tuple[Runtime, ResourceClient]:
    from muli.tools.mcp import MCPManager

    runtime = setup_runtime(path)
    runtime.config.update(
        {
            "persona": {"prompt": "遵守主人指令"},
            "models": {
                "main": {
                    "protocol": protocol,
                    **({"max_output_tokens": 4096} if protocol == "anthropic" else {}),
                    "model": "offline",
                    "base_url": "https://unused.invalid",
                }
            },
            "limits": {"max_output_chars": output_chars, "max_context_chars": 6000},
            "compaction": {"enabled": False},
        }
    )
    client = ResourceClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    try:
        await runtime.extensions.load_initial()
        return runtime, client
    except BaseException:
        await runtime.close()
        raise


def assert_complete_wire(
    config: ModelConfig, messages: list[Message], tools: list[dict[str, Any]]
) -> dict[str, Any]:
    result = next(message for message in reversed(messages) if message.role == "tool")
    assert result.preserve_content is True and EXCERPT_MARKER not in result.content
    assert result.tool_call_id is not None
    body = build_request(config, messages, tools, stream=False)
    assert wire_output(config.protocol, body, result.tool_call_id) == result.content
    assert "preserve_output" not in json.dumps(tools)
    assert "preserve_content" not in json.dumps(body)
    return cast(dict[str, Any], json.loads(result.content))


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_complete_catalog_and_text_reach_real_model_request_in_every_codec(
    tmp_path: Path, protocol: ProtocolName
) -> None:
    runtime, client = await resource_runtime(tmp_path, protocol)
    client.pages = [{"resources": [resource(i, description='元数据🙂"\\' * 20) for i in range(10)]}]
    contents = [
        {"uri": "test:///正文", "text": '不可信的全文🙂"\\\n' * 50, "mimeType": "text/plain"}
    ]
    client.content = {"contents": contents}
    shown: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []

    class Reader(FakeModel):
        def __init__(self, config: ModelConfig) -> None:
            super().__init__([], [])
            self.config = config

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            name, arguments = "mcp_resources_list", {"server": "old"}
            if any(message.role == "tool" for message in messages):
                result = assert_complete_wire(self.config, messages, tools)
                observed.append(result)
                assert result["is_error"] is False and result["untrusted"] is True
                if "contents" in result:
                    assert result["contents"][0]["text"] == contents[0]["text"]
                    yield StreamEvent("done", turn=ModelTurn(content="完整读取"))
                    return
                shown.extend(result["resources"])
                if result["next_cursor"] is not None:
                    arguments["cursor"] = result["next_cursor"]
                else:
                    name, arguments = "mcp_resource_read", {"server": "old", "ref": shown[0]["ref"]}
            assert len(observed) < 8
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[ToolCall(f"resource-{len(observed)}", name, json.dumps(arguments))]
                ),
            )

    runtime.client_factory = lambda config: cast(ModelClient, Reader(config))
    try:
        session = runtime.new_session(mode="yolo")
        runtime.store.add_message(session["id"], "user", "可淘汰旧上下文" * 1000)
        runtime.store.add_message(session["id"], "assistant", "旧轮结束")
        started = await runtime.start(session["id"], "列全目录并读首项", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "完整读取"
        assert 2 < len(observed) < 8
        assert [item["uri"] for item in shown] == [resource(i)["uri"] for i in range(10)]
        assert client.resource_cursors == [None] and client.reads == [resource()["uri"]]
        stored = [
            message
            for message in runtime.store.messages(session["id"])
            if message["role"] == "tool"
        ]
        assert [json.loads(message["content"]) for message in stored] == observed
        assert all(
            message["content_provenance"] == "accepted" and message["preserve_content"] is True
            for message in stored
        )
    finally:
        await runtime.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_full_resource_that_cannot_fit_context_stops_without_another_request(
    tmp_path: Path, protocol: ProtocolName
) -> None:
    runtime, client = await resource_runtime(tmp_path, protocol, output_chars=16000)
    client.pages = [{"resources": [resource()]}]
    text = "不可节选的完整文本🙂" * 1000
    client.content = {"contents": [{"uri": resource()["uri"], "text": text}]}
    requests = 0

    class Reader(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal requests
            requests += 1
            assert requests <= 2, "A required full resource cannot be silently excerpted"
            if requests == 1:
                call = ToolCall("list", "mcp_resources_list", '{"server":"old"}')
            else:
                result = json.loads(
                    next(
                        message.content for message in reversed(messages) if message.role == "tool"
                    )
                )
                call = ToolCall(
                    "read",
                    "mcp_resource_read",
                    json.dumps({"server": "old", "ref": result["resources"][0]["ref"]}),
                )
            yield StreamEvent("done", turn=ModelTurn(tool_calls=[call]))

    runtime.client_factory = lambda _: cast(ModelClient, Reader([], []))
    try:
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "读取整份文本", mode="yolo")
        with pytest.raises(ContextLimitError, match="需完整保留的工具结果"):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 5)
        assert requests == 2 and client.reads == [resource()["uri"]]
        stored = next(
            message
            for message in runtime.store.messages(session["id"])
            if message["role"] == "tool" and message["tool_call_id"] == "read"
        )
        assert json.loads(stored["content"])["contents"][0]["text"] == text
        assert stored["content_provenance"] == "accepted" and stored["preserve_content"] is True
        assert runtime.store.get("run", started["run_id"])["status"] == "failed"
    finally:
        await runtime.close()


async def test_real_approval_cannot_authorize_resource_after_its_generation_was_revoked(
    tmp_path: Path,
) -> None:
    runtime, client = await resource_runtime(tmp_path)
    client.pages = [{"resources": [resource()]}]
    try:
        listing = runtime.tools["mcp_resources_list"]
        reference = (await listing.handler({"server": "old"}))["resources"][0]["ref"]
        model = FakeModel(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "read",
                            "mcp_resource_read",
                            json.dumps({"server": "old", "ref": reference}),
                        )
                    ]
                ),
                ModelTurn(content="原调用未执行"),
            ],
            [],
        )
        runtime.client_factory = lambda _: cast(ModelClient, model)
        session = runtime.new_session(mode="ask")
        started = await runtime.start(session["id"], "请求批准读取")
        async with asyncio.timeout(3):
            async with runtime.bus.changed:
                await runtime.bus.changed.wait_for(lambda: bool(runtime.permissions.pending))
            approval = next(iter(runtime.permissions.pending))
            stored = runtime.store.get("approval", approval)
            assert stored["risk"] == "external" and stored["tool"] == "mcp_resource_read"
            target = runtime.config.settings.model_copy(deep=True)
            target.mcp = {"mcpServers": {}}
            async with runtime.configuration_lock:
                await runtime.extensions.apply_locked(target, persist=True)
            await runtime.permissions.resolve(approval, "approve")
            assert await runtime.wait(started["run_id"]) == "原调用未执行"
        result = next(
            message
            for message in runtime.store.messages(session["id"])
            if message["role"] == "tool"
        )
        assert json.loads(result["content"])["code"] == "generation_changed" and client.reads == []
    finally:
        await runtime.close()


async def test_skills_only_publication_keeps_resource_catalog_and_old_handler_usable(
    tmp_path: Path,
) -> None:
    runtime, client = await resource_runtime(tmp_path)
    client.pages = [{"resources": [resource()]}]
    try:
        listing = runtime.tools["mcp_resources_list"]
        reading = runtime.tools["mcp_resource_read"]
        reference = (await listing.handler({"server": "old"}))["resources"][0]["ref"]
        connection = runtime.mcp._connections["old"]
        catalog = connection.resource_catalog
        source = tmp_path / "skills" / "example" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"---\nname: example\ndescription: local fixture\n---\nGuidance\n")
        runtime.extensions._queue_reload(directory=True)
        async with runtime.configuration_lock:
            assert await runtime.extensions._apply_skills_locked(runtime.config.settings) is True
        assert runtime.extensions.status()["last_apply"]["scope"] == "skills"
        assert (
            connection.resource_catalog is catalog and runtime.tools["mcp_resource_read"] is reading
        )
        assert (await reading.handler({"server": "old", "ref": reference}))["is_error"] is False
        assert client.entered == 1 and client.reads == [resource()["uri"]]
    finally:
        await runtime.close()


async def test_last_model_round_does_not_regain_resource_tools(tmp_path: Path) -> None:
    runtime, client = await resource_runtime(tmp_path)
    runtime.config.settings.limits.max_tool_steps = 1
    schemas: list[list[str]] = []

    class Limited(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schemas.append([item["function"]["name"] for item in tools])
            turn = (
                ModelTurn(tool_calls=[ToolCall("list", "mcp_resources_list", '{"server":"old"}')])
                if len(schemas) == 1
                else ModelTurn(content="最后解释轮")
            )
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: cast(ModelClient, Limited([], []))
    try:
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "一次目录观察", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 3) == "最后解释轮"
        assert "mcp_resources_list" in schemas[0] and schemas[1] == []
        assert client.resource_cursors == [None] and client.reads == []
    finally:
        await runtime.close()


@pytest.mark.parametrize("kind", ["main", "reviewer", "attention", "disabled"])
async def test_resource_host_tools_keep_existing_runtime_visibility_and_execution_filters(
    tmp_path: Path, kind: str
) -> None:
    runtime, client = await resource_runtime(tmp_path)
    schemas: list[list[str]] = []
    if kind == "disabled":
        runtime.config.settings.disabled_tools = ["mcp_resources_list", "mcp_resource_read"]

    class Hidden(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schemas.append([item["function"]["name"] for item in tools])
            assert (
                "mcp_resources_list" not in schemas[-1] and "mcp_resource_read" not in schemas[-1]
            )
            turn = (
                ModelTurn(tool_calls=[ToolCall("hidden", "mcp_resources_list", '{"server":"old"}')])
                if len(schemas) == 1
                else ModelTurn(content="保持过滤")
            )
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: cast(ModelClient, Hidden([], []))
    session = (
        runtime.main_session()
        if kind in {"main", "attention"}
        else runtime.new_session(mode="yolo")
    )
    try:
        if kind == "reviewer":
            runtime.config.settings.models.reviewer = (
                runtime.config.settings.models.main.model_copy()
            )
            operation = asyncio.create_task(
                runtime.run_step(session["id"], "只读审查", "reviewer", "yolo")
            )
        elif kind == "attention":
            assert (
                await runtime.submit_attention("参考资料", "resource-filter", objects=[])
                is not None
            )
            operation = runtime.tasks[session["id"]]
        else:
            await runtime.start(session["id"], "过滤外部资源", mode="yolo")
            operation = runtime.tasks[session["id"]]
        assert await asyncio.wait_for(operation, 3) == "保持过滤"
        assert len(schemas) == 2 and client.reads == client.resource_cursors == []
    finally:
        await runtime.close()
