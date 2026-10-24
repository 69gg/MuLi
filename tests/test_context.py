from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from muli.config import Limits
from muli.context import EXCERPT_MARKER, ContextLimitError, pack_context
from muli.providers import ImageInput, Message, ModelConfig, ToolCall
from muli.providers.codecs import build_request
from muli.providers.types import MessageRole, ProtocolName


def size(messages: list[Message]) -> int:
    data = [asdict(message) for message in messages]
    for record in data:
        record.pop("source_message_id")
    return len(json.dumps(data, ensure_ascii=False))


def tool_turn(content: str, identifier: str = "call_a") -> list[Message]:
    return [
        Message(role="assistant", tool_calls=[ToolCall(identifier, "inspect", '{"path":"/资料"}')]),
        Message(role="tool", tool_call_id=identifier, content=content),
    ]


def test_fit_is_unchanged_independent_copy_with_native_protocol_data() -> None:
    messages = [
        Message(role="system", content="系统提示"),
        Message(role="user", content="最新输入", source_message_id="actual-local-message"),
        Message(
            role="assistant",
            content="答复",
            reasoning="不裁剪思考",
            provider_data={
                "protocol": "anthropic",
                "content_blocks": [
                    {"type": "thinking", "thinking": "思考", "signature": "signature"}
                ],
            },
        ),
    ]
    snapshot = deepcopy(messages)
    packed = pack_context(messages, size(messages))
    assert packed == snapshot and packed is not messages
    assert packed[1] is not messages[1]
    assert packed[1].source_message_id == "actual-local-message"
    packed[-1].provider_data["content_blocks"][0]["signature"] = "different"
    packed[0].content = "different"
    assert messages == snapshot


def test_tool_result_excerpt_preserves_both_ends_and_call_arguments() -> None:
    content = "BEGIN\n" + "中间数据" * 1500 + "\nEND"
    messages = [
        Message(role="system", content="系统"),
        Message(role="user", content="检查"),
    ] + tool_turn(content)
    snapshot = deepcopy(messages)
    packed = pack_context(messages, 1600)
    assert size(packed) <= 1600
    assert packed[-1].content.startswith("BEGIN\n") and packed[-1].content.endswith("\nEND")
    assert EXCERPT_MARKER in packed[-1].content
    assert packed[-2].tool_calls == messages[-2].tool_calls
    assert messages == snapshot
    assert pack_context(packed, 1600) == packed


def test_json_escaping_is_included_in_the_strict_budget() -> None:
    messages = [Message(role="user", content="读取")] + tool_turn('"\\\n\t' * 2000)
    packed = pack_context(messages, 1200)
    assert EXCERPT_MARKER in packed[-1].content
    assert size(packed) <= 1200
    assert size(packed) > 1100


def test_only_old_plain_assistant_text_can_be_excerpted() -> None:
    messages = [
        Message(role="system", content="系统"),
        Message(role="user", content="之前的问题"),
        Message(role="assistant", content="START" + "旧答复" * 1000 + "END", reasoning="完整思考"),
        Message(role="user", content="继续"),
    ]
    packed = pack_context(messages, 1000)
    assert len(packed) == 4 and size(packed) <= 1000
    assert EXCERPT_MARKER in packed[2].content
    assert packed[2].reasoning == "完整思考" and packed[-1].content == "继续"


def test_old_turn_is_removed_as_whole_with_its_calls_and_results() -> None:
    old = [Message(role="user", content="旧问题" * 1000)] + tool_turn("旧结果")
    messages = [
        Message(role="system", content="系统"),
        *old,
        Message(role="user", content="最新问题"),
    ]
    packed = pack_context(messages, 600)
    assert [message.role for message in packed] == ["system", "user"]
    assert packed[-1].content == "最新问题"
    assert len(messages) == 5


def test_system_and_developer_messages_are_kept_when_old_turn_is_removed() -> None:
    messages = [
        Message(role="system", content="必须保留系统"),
        Message(role="user", content="旧问题" * 1000),
        Message(role="assistant", content="旧答复"),
        Message(role="developer", content="必须保留开发指令"),
        Message(role="user", content="最新问题"),
    ]
    packed = pack_context(messages, 700)
    assert [message.role for message in packed] == ["system", "developer", "user"]
    assert packed[0].content == messages[0].content and packed[1].content == messages[3].content


def test_current_native_replay_cannot_be_clipped_even_with_small_visible_text() -> None:
    native = {
        "protocol": "openai_responses",
        "output_items": [{"type": "reasoning", "encrypted_content": "signed" * 1000}],
    }
    messages = [
        Message(role="system", content="系统"),
        Message(role="user", content="最新问题"),
        Message(role="assistant", content="短回答", provider_data=native),
    ]
    snapshot = deepcopy(messages)
    with pytest.raises(ContextLimitError, match="原生协议数据"):
        pack_context(messages, 1200)
    assert messages == snapshot


def test_old_native_turn_can_be_removed_without_mutating_its_signature() -> None:
    native = {
        "protocol": "anthropic",
        "content_blocks": [{"type": "thinking", "signature": "signature" * 500}],
    }
    messages = [
        Message(role="user", content="旧问题"),
        Message(role="assistant", content="旧答复", provider_data=native),
        Message(role="user", content="新问题"),
    ]
    packed = pack_context(messages, 500)
    assert packed == [messages[-1]]
    assert messages[1].provider_data == native


@pytest.mark.parametrize("role", ["user", "system", "developer"])
def test_necessary_input_over_limit_has_clear_error_without_content(role: MessageRole) -> None:
    message = Message(role=role, content="sensitive-value" * 1000)
    with pytest.raises(ContextLimitError, match="上下文字符预算") as error:
        pack_context([message], 500)
    assert "sensitive-value" not in str(error.value)
    assert message.content == "sensitive-value" * 1000


def test_tool_parameters_are_never_clipped_to_make_room() -> None:
    messages = [
        Message(role="user", content="执行"),
        Message(
            role="assistant", tool_calls=[ToolCall("a", "inspect", json.dumps({"x": "v" * 2000}))]
        ),
        Message(role="tool", tool_call_id="a", content="成功"),
    ]
    with pytest.raises(ContextLimitError):
        pack_context(messages, 1000)


@pytest.mark.parametrize(
    "messages",
    [
        [Message(role="tool", tool_call_id="missing", content="孤立")],
        [Message(role="assistant", tool_calls=[ToolCall("a", "inspect", "{}")])],
        [
            Message(role="assistant", tool_calls=[ToolCall("a", "inspect", "{}")]),
            Message(role="user", content="结果缺失"),
        ],
        [
            Message(role="assistant", tool_calls=[ToolCall("a", "inspect", "{}")]),
            Message(role="tool", tool_call_id="a", content="第一次"),
            Message(role="tool", tool_call_id="a", content="重复"),
        ],
        [
            Message(
                role="assistant",
                tool_calls=[ToolCall("a", "inspect", "{}"), ToolCall("a", "inspect", "{}")],
            ),
            Message(role="tool", tool_call_id="a", content="结果"),
        ],
        [Message(role="user", tool_calls=[ToolCall("a", "inspect", "{}")])],
    ],
)
def test_unpaired_calls_and_results_are_rejected_even_when_they_fit(
    messages: list[Message],
) -> None:
    with pytest.raises(ValueError, match="工具"):
        pack_context(messages, 10000)


def test_multiple_results_stay_paired_and_packing_can_continue() -> None:
    messages = [
        Message(role="user", content="检查两处"),
        Message(
            role="assistant",
            tool_calls=[ToolCall("a", "inspect", "{}"), ToolCall("b", "inspect", "{}")],
        ),
        Message(role="tool", tool_call_id="b", content="b-head" + "b" * 6000 + "b-tail"),
        Message(role="tool", tool_call_id="a", content="a-head" + "a" * 6000 + "a-tail"),
    ]
    packed = pack_context(messages, 1500)
    assert {message.tool_call_id for message in packed if message.role == "tool"} == {"a", "b"}
    assert size(packed) <= 1500
    continued = pack_context(packed + tool_turn("继续检查证据" * 2000, "c"), 2200)
    assert size(continued) <= 2200 and continued[-1].tool_call_id == "c"
    assert EXCERPT_MARKER in continued[-1].content


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
def test_packed_turn_still_encodes_with_native_tool_replay(protocol: ProtocolName) -> None:
    call = ToolCall("native-call", "inspect", '{"path":"/x"}')
    native_by_protocol = {
        "openai_chat": {
            "protocol": "openai_chat",
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning_content": "完整推理",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                ],
            },
        },
        "openai_responses": {
            "protocol": "openai_responses",
            "output_items": [
                {"id": "reason", "type": "reasoning", "encrypted_content": "opaque-signature"},
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                },
            ],
        },
        "anthropic": {
            "protocol": "anthropic",
            "content_blocks": [
                {"type": "thinking", "thinking": "完整推理", "signature": "opaque-signature"},
                {"type": "tool_use", "id": call.id, "name": call.name, "input": {"path": "/x"}},
            ],
        },
    }
    native = native_by_protocol[protocol]
    messages = [
        Message(role="user", content="检查"),
        Message(role="assistant", tool_calls=[call], provider_data=native),
        Message(role="tool", tool_call_id=call.id, content="start" + "详细资料" * 2000 + "end"),
    ]
    packed = pack_context(messages, 2200)
    assert packed[1].provider_data == native and packed[1].tool_calls == [call]
    config = ModelConfig(protocol=protocol, model="test", base_url="https://example.invalid/v1")
    body = build_request(config, packed, [], stream=False)
    encoded = json.dumps(body, ensure_ascii=False)
    assert call.id in encoded and EXCERPT_MARKER in encoded
    assert "完整推理" in encoded or "opaque-signature" in encoded


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_budget_is_rejected(budget: int) -> None:
    with pytest.raises(ValueError, match="正整数"):
        pack_context([], budget)


def test_pending_run_config_limit_defaults_and_bounds() -> None:
    assert Limits().max_pending_runs == 8
    assert Limits(max_pending_runs=1024).max_pending_runs == 1024
    assert Limits(max_parallel_agents=129).max_parallel_agents == 129
    assert Limits(max_workflow_steps=256).max_workflow_steps == 256
    for value in (0, -1):
        with pytest.raises(ValueError):
            Limits(max_pending_runs=value)


def test_image_bytes_use_media_budget_and_native_image_stays_intact() -> None:
    image = ImageInput.from_bytes(b"x" * 128000, "image/png")
    message = Message(role="user", content="看图", images=[image])
    packed = pack_context([message], 1000)
    assert packed == [message] and packed[0].images == [image]
    assert size(packed) > 1000


@pytest.mark.parametrize("budget", [750, 1200, 2100, 4000])
def test_mixed_old_turns_and_tool_text_are_deterministic_at_different_budgets(budget: int) -> None:
    messages = [
        Message(role="system", content="系统"),
        Message(role="user", content='旧输入"\\' * 100),
        *tool_turn("起" + '"\\\n' * 2000 + "末", "old"),
        Message(role="assistant", content="旧结论" * 100),
        Message(role="user", content="当前输入"),
        *tool_turn("头" + "当前资料" * 1000 + "尾", "current"),
    ]
    snapshot = deepcopy(messages)
    packed = pack_context(messages, budget)
    assert size(packed) <= budget
    assert packed == pack_context(messages, budget)
    assert messages == snapshot
    assert packed[0] == messages[0]
    assert [message.content for message in packed if message.role == "user"][-1] == "当前输入"
    assert packed[-1].tool_call_id == "current"
