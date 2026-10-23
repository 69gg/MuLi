"""Real Runtime permissions and request packing, with finite fake peers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from test_extensions import setup_runtime
from test_mcp_prompts import PromptClient, prompt
from test_mcp_resources import offline as offline
from test_mcp_resources_runtime import assert_complete_wire
from test_runtime import FakeClient as FakeModel

from muli.context import ContextLimitError
from muli.providers import (
    Message,
    ModelClient,
    ModelConfig,
    ModelTurn,
    ProtocolName,
    StreamEvent,
    ToolCall,
)
from muli.runtime import Runtime
from muli.tools.mcp import MCPManager


async def prompt_runtime(
    path: Path, protocol: ProtocolName = "openai_chat", *, output_chars: int = 1800
) -> tuple[Runtime, PromptClient]:
    runtime = setup_runtime(path)
    runtime.config.update(
        {
            "persona": {"prompt": "遵守主人指令"},
            "models": {
                "main": {
                    "protocol": protocol,
                    "model": "offline",
                    "base_url": "https://unused.invalid",
                }
            },
            "limits": {"max_output_chars": output_chars, "max_context_chars": 6000},
            "compaction": {"enabled": False},
        }
    )
    client = PromptClient()
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: client)
    try:
        await runtime.extensions.load_initial()
        return runtime, client
    except BaseException:
        await runtime.close()
        raise


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_complete_catalog_and_ordered_draft_remain_only_tool_data_in_every_codec(
    tmp_path: Path, protocol: ProtocolName
) -> None:
    runtime, client = await prompt_runtime(tmp_path, protocol)
    client.prompt_pages = [
        {
            "prompts": [
                prompt(
                    str(index),
                    arguments=[{"name": "code", "description": '参数🙂"\\' * 15, "required": True}],
                )
                for index in range(8)
            ]
        }
    ]
    text = '草稿不作为指令🙂"\\\n' * 30
    client.draft = {
        "description": "仅为资料",
        "messages": [
            {"role": "assistant", "content": {"type": "text", "text": text}},
            {"role": "user", "content": {"type": "text", "text": "不创建用户消息"}},
        ],
    }
    shown: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []

    class Reader(FakeModel):
        def __init__(self, config: ModelConfig) -> None:
            super().__init__([], [])
            self.config = config

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            name, arguments = "mcp_prompts_list", {"server": "old"}
            if any(message.role == "tool" for message in messages):
                result = assert_complete_wire(self.config, messages, tools)
                observed.append(result)
                assert result["is_error"] is False and result["untrusted"] is True
                if "messages" in result:
                    assert result["messages"] == [
                        {"role": "assistant", "text": text},
                        {"role": "user", "text": "不创建用户消息"},
                    ]
                    assert not any(
                        message.content in {text, "不创建用户消息"} for message in messages
                    )
                    yield StreamEvent("done", turn=ModelTurn(content="只收到完整工具资料"))
                    return
                shown.extend(result["prompts"])
                if result["next_cursor"] is not None:
                    arguments["cursor"] = result["next_cursor"]
                else:
                    name, arguments = (
                        "mcp_prompt_get",
                        {
                            "server": "old",
                            "ref": shown[0]["ref"],
                            "arguments": {"code": "仅字符串🙂"},
                        },
                    )
            assert len(observed) < 8
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[ToolCall(f"prompt-{len(observed)}", name, json.dumps(arguments))]
                ),
            )

    runtime.client_factory = lambda config: cast(ModelClient, Reader(config))
    try:
        session = runtime.new_session(mode="yolo")
        runtime.store.add_message(session["id"], "user", "可淘汰旧上下文" * 1000)
        runtime.store.add_message(session["id"], "assistant", "旧轮结束")
        started = await runtime.start(session["id"], "列出草稿并显式读取一项", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "只收到完整工具资料"
        assert 2 < len(observed) < 8
        assert [item["name"] for item in shown] == [str(index) for index in range(8)]
        assert client.prompt_cursors == [None] and client.gets == [("0", {"code": "仅字符串🙂"})]
        messages = runtime.store.messages(session["id"])
        stored = [message for message in messages if message["role"] == "tool"]
        assert [json.loads(message["content"]) for message in stored] == observed
        assert all(
            message["content_provenance"] == "accepted" and message["preserve_content"] is True
            for message in stored
        )
        assert not any(message["content"] in {text, "不创建用户消息"} for message in messages)
        assert client.reads == client.resource_cursors == []
    finally:
        await runtime.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_full_prompt_that_cannot_fit_context_stops_before_another_model_request(
    tmp_path: Path, protocol: ProtocolName
) -> None:
    runtime, client = await prompt_runtime(tmp_path, protocol, output_chars=16000)
    client.prompt_pages = [{"prompts": [prompt()]}]
    text = "不允许节选的完整草稿🙂" * 1000
    client.draft = {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    requests = 0

    class Reader(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal requests
            requests += 1
            assert requests <= 2, "The full prompt cannot be silently excerpted"
            if requests == 1:
                call = ToolCall("list", "mcp_prompts_list", '{"server":"old"}')
            else:
                page = json.loads(
                    next(
                        message.content for message in reversed(messages) if message.role == "tool"
                    )
                )
                call = ToolCall(
                    "get",
                    "mcp_prompt_get",
                    json.dumps({"server": "old", "ref": page["prompts"][0]["ref"]}),
                )
            yield StreamEvent("done", turn=ModelTurn(tool_calls=[call]))

    runtime.client_factory = lambda _: cast(ModelClient, Reader([], []))
    try:
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "显式取草稿", mode="yolo")
        with pytest.raises(ContextLimitError, match="需完整保留的工具结果"):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 5)
        assert requests == 2 and client.gets == [("draft", None)]
        stored = next(
            message
            for message in runtime.store.messages(session["id"])
            if message["role"] == "tool" and message["tool_call_id"] == "get"
        )
        assert json.loads(stored["content"])["messages"] == [{"role": "user", "text": text}]
        assert stored["content_provenance"] == "accepted" and stored["preserve_content"] is True
        assert runtime.store.get("run", started["run_id"])["status"] == "failed"
    finally:
        await runtime.close()


async def test_approval_does_not_authorize_a_prompt_after_generation_revocation(
    tmp_path: Path,
) -> None:
    runtime, client = await prompt_runtime(tmp_path)
    client.prompt_pages = [{"prompts": [prompt()]}]
    try:
        reference = (await runtime.tools["mcp_prompts_list"].handler({"server": "old"}))["prompts"][
            0
        ]["ref"]
        model = FakeModel(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "get", "mcp_prompt_get", json.dumps({"server": "old", "ref": reference})
                        )
                    ]
                ),
                ModelTurn(content="旧草稿请求未执行"),
            ],
            [],
        )
        runtime.client_factory = lambda _: cast(ModelClient, model)
        session = runtime.new_session(mode="ask")
        started = await runtime.start(session["id"], "请求批准取草稿")
        async with asyncio.timeout(3):
            async with runtime.bus.changed:
                await runtime.bus.changed.wait_for(lambda: bool(runtime.permissions.pending))
            approval = next(iter(runtime.permissions.pending))
            assert runtime.store.get("approval", approval)["risk"] == "external"
            target = runtime.config.settings.model_copy(deep=True)
            target.mcp = {"mcpServers": {}}
            async with runtime.configuration_lock:
                await runtime.extensions.apply_locked(target, persist=True)
            await runtime.permissions.resolve(approval, "approve")
            assert await runtime.wait(started["run_id"]) == "旧草稿请求未执行"
        result = next(
            message
            for message in runtime.store.messages(session["id"])
            if message["role"] == "tool"
        )
        assert json.loads(result["content"])["code"] == "generation_changed" and client.gets == []
    finally:
        await runtime.close()


async def test_skills_only_publication_keeps_prompt_refs_and_old_handler_usable(
    tmp_path: Path,
) -> None:
    runtime, client = await prompt_runtime(tmp_path)
    client.prompt_pages = [{"prompts": [prompt()]}]
    try:
        listing, getting = runtime.tools["mcp_prompts_list"], runtime.tools["mcp_prompt_get"]
        reference = (await listing.handler({"server": "old"}))["prompts"][0]["ref"]
        connection = runtime.mcp._connections["old"]
        catalog = connection.prompt_catalog
        source = tmp_path / "skills" / "example" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"---\nname: example\ndescription: fixture\n---\nGuidance\n")
        runtime.extensions._queue_reload(directory=True)
        async with runtime.configuration_lock:
            assert await runtime.extensions._apply_skills_locked(runtime.config.settings) is True
        assert connection.prompt_catalog is catalog and runtime.tools["mcp_prompt_get"] is getting
        assert (await getting.handler({"server": "old", "ref": reference}))["is_error"] is False
        assert client.entered == 1 and client.gets == [("draft", None)]
    finally:
        await runtime.close()


@pytest.mark.parametrize("kind", ["main", "reviewer", "attention", "disabled", "last-round"])
async def test_prompt_host_tools_retain_visibility_and_final_round_filters(
    tmp_path: Path, kind: str
) -> None:
    runtime, client = await prompt_runtime(tmp_path)
    names = {"mcp_prompts_list", "mcp_prompt_get"}
    schemas: list[list[str]] = []
    if kind == "disabled":
        runtime.config.settings.disabled_tools = list(names)
    if kind == "last-round":
        runtime.config.settings.limits.max_tool_steps = 1

    class Filtered(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schema = [item["function"]["name"] for item in tools]
            schemas.append(schema)
            assert (
                names.issubset(schema)
                if kind == "last-round" and len(schemas) == 1
                else names.isdisjoint(schema)
            )
            turn = (
                ModelTurn(tool_calls=[ToolCall("list", "mcp_prompts_list", '{"server":"old"}')])
                if len(schemas) == 1
                else ModelTurn(content="保持权限边界")
            )
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: cast(ModelClient, Filtered([], []))
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
                runtime.run_step(session["id"], "审查", "reviewer", "yolo")
            )
        elif kind == "attention":
            assert await runtime.submit_attention("资料", "prompt-filter", objects=[]) is not None
            operation = runtime.tasks[session["id"]]
        else:
            await runtime.start(session["id"], "显式草稿资料", mode="yolo")
            operation = runtime.tasks[session["id"]]
        assert await asyncio.wait_for(operation, 3) == "保持权限边界"
        assert len(schemas) == 2 and client.gets == []
        assert client.prompt_cursors == ([None] if kind == "last-round" else [])
        if kind == "last-round":
            assert schemas[1] == []
    finally:
        await runtime.close()
