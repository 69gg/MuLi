from __future__ import annotations

import json
from copy import deepcopy

import pytest

from muli.context import ContextLimitError, context_size, pack_context
from muli.providers import Message, ToolCall


def pending_result(content: str) -> list[Message]:
    return [
        Message(role="user", content="核对这一步"),
        Message(role="assistant", tool_calls=[ToolCall("read", "conversation_read", "{}")]),
        Message(role="tool", tool_call_id="read", content=content, preserve_content=True),
    ]


@pytest.mark.parametrize(
    "suffix",
    [
        [Message(role="user", content="补充：先保留草稿")],
        [Message(role="user", content="队友只交资料"), Message(role="user", content="系统事项")],
        [
            Message(role="system", content="指导"),
            Message(role="user", content="主人补充"),
            Message(role="developer", content="宿主约束"),
        ],
    ],
)
def test_new_inputs_do_not_release_unconsumed_complete_result(suffix: list[Message]) -> None:
    messages = (
        pending_result(json.dumps({"evidence": "原始正文" * 1500}, ensure_ascii=False)) + suffix
    )
    original = deepcopy(messages)
    with pytest.raises(ContextLimitError):
        pack_context(messages, 2500)
    assert messages == original


def test_only_finished_older_turns_make_room_for_pending_batch_and_new_input() -> None:
    content = json.dumps({"evidence": "核查" * 500}, ensure_ascii=False)
    messages = [
        Message(role="system", content="宿主"),
        Message(role="user", content="已结束的旧任务" * 1800),
        Message(role="assistant", content="旧任务完成"),
        *pending_result(content),
        Message(role="user", content="当前补充"),
    ]
    packed = pack_context(messages, 3500)
    result = next(message for message in packed if message.tool_call_id == "read")
    assert result.content == content
    assert json.loads(result.content) == {"evidence": "核查" * 500}
    assert packed[-1].content == "当前补充" and context_size(packed) <= 3500
    assert all("已结束的旧任务" not in message.content for message in packed)


@pytest.mark.parametrize(
    "response",
    [
        Message(role="assistant", content="已核查，按新要求继续"),
        Message(role="assistant", content="", reasoning="模型已处理回执"),
    ],
)
def test_model_response_releases_previous_result_for_ordinary_history_packing(
    response: Message,
) -> None:
    messages = [
        *pending_result("保存的原始回执" * 1500),
        Message(role="user", content="补充"),
        response,
        Message(role="user", content="下一任务"),
    ]
    packed = pack_context(messages, 2500)
    assert packed[-1].content == "下一任务" and context_size(packed) <= 2500
    assert all(len(message.content) < 1500 for message in packed)


def test_another_tool_batch_releases_the_previous_batch_only() -> None:
    latest = json.dumps({"current": "必须完整" * 250}, ensure_ascii=False)
    messages = [
        *pending_result("旧回执" * 3000),
        Message(role="user", content="处理中追加"),
        Message(role="assistant", tool_calls=[ToolCall("next", "conversation_read", "{}")]),
        Message(role="tool", tool_call_id="next", content=latest, preserve_content=True),
        Message(role="user", content="最后补充"),
    ]
    packed = pack_context(messages, 3500)
    assert next(message for message in packed if message.tool_call_id == "next").content == latest
    assert context_size(packed) <= 3500
