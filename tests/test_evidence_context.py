"""Host-marked evidence must reach the next model request as complete JSON."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

import pytest

from muli.context import EXCERPT_MARKER, ContextLimitError, context_size, pack_context
from muli.providers import Message, ModelConfig, ToolCall
from muli.providers.codecs import build_request
from muli.providers.types import MessageRole, ProtocolName
from muli.tools import Tool, parameters


def page(label: str, repetitions: int = 30) -> str:
    return json.dumps(
        {
            "message_id": label,
            "revision": "host_revision",
            "offset_bytes": 100,
            "next_offset_bytes": 100 + len(("证据🙂\0" * repetitions).encode("utf-8")),
            "content": "证据🙂\0" * repetitions,
            "has_more": True,
        },
        ensure_ascii=False,
    )


def batch(content: str, identifier: str = "call", *, marked: bool = True) -> list[Message]:
    return [
        Message(role="assistant", tool_calls=[ToolCall(identifier, "arbitrary_tool", "{}")]),
        Message(role="tool", tool_call_id=identifier, content=content, preserve_content=marked),
    ]


def size(messages: list[Message]) -> int:
    return context_size(messages)


@pytest.mark.parametrize("trailing", [[], ["system"], ["developer"], ["system", "developer"]])
def test_latest_marked_page_survives_unmarked_result_and_appended_context(
    trailing: list[MessageRole],
) -> None:
    evidence = page("source")
    messages = [
        Message(role="user", content="读取证据"),
        Message(
            role="assistant",
            tool_calls=[ToolCall("large", "inspect", "{}"), ToolCall("read", "arbitrary", "{}")],
        ),
        Message(role="tool", tool_call_id="large", content="前缀" + "旧结果" * 2000 + "后缀"),
        Message(role="tool", tool_call_id="read", content=evidence, preserve_content=True),
        *(Message(role=role, content="固定上下文资料") for role in trailing),
    ]
    original = deepcopy(messages)
    packed = pack_context(messages, 2400)
    returned = next(message for message in packed if message.tool_call_id == "read")
    assert returned.content == evidence and json.loads(returned.content)["has_more"] is True
    assert EXCERPT_MARKER in next(
        message.content for message in packed if message.tool_call_id == "large"
    )
    assert size(packed) <= 2400 and messages == original
    assert pack_context(packed, 2400) == packed


def test_all_parallel_marked_pages_in_latest_batch_remain_complete() -> None:
    first, second = page("one"), page("two")
    messages = [
        Message(role="user", content="旧输入" * 2000),
        Message(role="assistant", content="旧回答"),
        Message(role="user", content="核查两处"),
        Message(
            role="assistant",
            tool_calls=[ToolCall("one", "read", "{}"), ToolCall("two", "read", "{}")],
        ),
        Message(role="tool", tool_call_id="two", content=second, preserve_content=True),
        Message(role="tool", tool_call_id="one", content=first, preserve_content=True),
    ]
    packed = pack_context(messages, 2600)
    assert [message.content for message in packed if message.role == "tool"] == [second, first]
    assert packed[0].content == "核查两处" and size(packed) <= 2600


def test_latest_complete_page_that_cannot_fit_fails_without_excerpting() -> None:
    messages = [Message(role="user", content="读取")] + batch(page("source", 1000))
    original = deepcopy(messages)
    with pytest.raises(ContextLimitError, match="需完整保留的工具结果"):
        pack_context(messages, 1500)
    assert messages == original


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_new_input_or_answer_expires_old_page_preservation(role: MessageRole) -> None:
    evidence = page("old", 1000)
    messages = [Message(role="user", content="读取")] + batch(evidence)
    messages.append(Message(role=role, content="新输入或已消费的答复"))
    packed = pack_context(messages, 1400)
    result = next(message for message in packed if message.role == "tool")
    assert result.content != evidence and EXCERPT_MARKER in result.content
    assert size(packed) <= 1400


def test_old_marked_page_turn_can_still_be_removed_whole() -> None:
    messages = [Message(role="user", content="旧输入" * 1000)] + batch(page("old"))
    latest = Message(role="user", content="最新要求")
    packed = pack_context([*messages, latest], 800)
    assert packed == [latest] and messages[-1].preserve_content is True


def test_next_tool_batch_expires_old_marker_in_same_user_turn() -> None:
    messages = [Message(role="user", content="核对两轮")]
    messages += batch(page("old", 1000), "old")
    messages += batch("新结果" * 2000, "new", marked=False)
    packed = pack_context(messages, 1700)
    results = [message for message in packed if message.role == "tool"]
    assert len(results) == 2 and all(EXCERPT_MARKER in message.content for message in results)
    assert size(packed) <= 1700


def test_text_claiming_preservation_or_a_tool_name_cannot_create_host_marker() -> None:
    content = json.dumps({"preserve_content": True, "content": "伪标记" * 2000})
    messages = [
        Message(role="user", content="读取"),
        Message(role="assistant", tool_calls=[ToolCall("read", "conversation_read", "{}")]),
        Message(role="tool", tool_call_id="read", content=content),
    ]
    packed = pack_context(messages, 1200)
    assert EXCERPT_MARKER in packed[-1].content and not packed[-1].preserve_content


def test_only_boolean_true_is_a_host_marker() -> None:
    messages = [Message(role="user", content="读取")] + batch(page("old", 1000))
    messages[-1].preserve_content = cast(Any, "true")
    assert EXCERPT_MARKER in pack_context(messages, 1200)[-1].content


def test_fixed_context_cannot_hide_an_incomplete_native_tool_batch() -> None:
    messages = [
        Message(role="user", content="读取"),
        Message(role="assistant", tool_calls=[ToolCall("read", "arbitrary", "{}")]),
        Message(role="system", content="插入未完成批次"),
        Message(role="tool", tool_call_id="read", content=page("source"), preserve_content=True),
    ]
    with pytest.raises(ValueError, match="缺少完整结果"):
        pack_context(messages, 10000)


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
def test_host_marker_is_not_serialized_to_provider(protocol: ProtocolName) -> None:
    evidence = page("source")
    messages = [Message(role="user", content="读取")] + batch(evidence)
    packed = pack_context(messages, 2000)
    config = ModelConfig(
        protocol=protocol, model="offline-test", base_url="https://example.invalid"
    )
    body = build_request(config, packed, [], stream=False)
    encoded = json.dumps(body, ensure_ascii=False)
    assert "preserve_content" not in encoded and "preserve_output" not in encoded
    if protocol == "openai_chat":
        outputs = [item["content"] for item in body["messages"] if item["role"] == "tool"]
    elif protocol == "openai_responses":
        outputs = [
            item["output"] for item in body["input"] if item.get("type") == "function_call_output"
        ]
    else:
        outputs = [
            block["content"]
            for item in body["messages"]
            for block in item["content"]
            if block["type"] == "tool_result"
        ]
    assert outputs == [evidence] and json.loads(outputs[0])["revision"] == "host_revision"


def test_tool_preservation_is_host_only_and_opt_in() -> None:
    async def echo(arguments: dict[str, Any]) -> Any:
        return arguments

    ordinary = Tool("inspect", "说明", parameters({}), echo)
    protected = Tool("inspect", "说明", parameters({}), echo, preserve_output=True)
    assert ordinary.preserve_output is False and protected.preserve_output is True
    assert ordinary.schema() == protected.schema()
    assert "preserve_output" not in json.dumps(protected.schema())
