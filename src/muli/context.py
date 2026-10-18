"""按字符预算打包上下文；持久记录和原生协议数据保持不变。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict

from muli.providers import Message

EXCERPT_MARKER = "[结果已节选；原始记录保存在会话]"
_PINNED_ROLES = frozenset({"system", "developer"})


class ContextLimitError(ValueError):
    """保留必要消息和完整协议数据后仍无法满足预算。"""


def _size(messages: Sequence[Message]) -> int:
    return len(json.dumps([asdict(message) for message in messages], ensure_ascii=False))


def _validate_pairs(messages: Sequence[Message]) -> None:
    pending: set[str] = set()
    for message in messages:
        if message.role == "tool":
            if (
                message.tool_calls
                or not message.tool_call_id
                or message.tool_call_id not in pending
            ):
                raise ValueError("上下文包含孤立或重复的工具结果")
            pending.remove(message.tool_call_id)
            continue
        if pending:
            raise ValueError("上下文中的工具调用缺少完整结果，不能安全发送")
        if message.tool_calls:
            identifiers = [call.id for call in message.tool_calls]
            if (
                message.role != "assistant"
                or any(not identifier for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)
            ):
                raise ValueError("上下文中的工具调用角色或标识无效")
            pending.update(identifiers)
    if pending:
        raise ValueError("上下文中的工具调用缺少完整结果，不能安全发送")


def _excerpt(content: str, keep: int) -> str:
    head = (keep + 1) // 2
    tail = keep // 2
    return content[:head] + "\n" + EXCERPT_MARKER + "\n" + content[-tail:]


def _compress(message: Message, current_size: int, max_chars: int) -> int:
    """压到可容纳的最长头尾摘录；不足时留最短摘录供后续候选继续压缩。"""
    original = message.content
    if len(original) < 3:
        return current_size
    minimal = _excerpt(original, 2)
    original_size = len(json.dumps(original, ensure_ascii=False))
    minimal_size = len(json.dumps(minimal, ensure_ascii=False))
    if minimal_size >= original_size:
        return current_size
    # 只改变一个 JSON 字符串，增减其转义后的长度即可；最终仍完整序列化复核。
    fixed_size = current_size - original_size
    message.content = minimal
    if fixed_size + minimal_size > max_chars:
        return fixed_size + minimal_size
    low, high = 2, len(original) - 1
    while low < high:
        middle = (low + high + 1) // 2
        message.content = _excerpt(original, middle)
        if fixed_size + len(json.dumps(message.content, ensure_ascii=False)) <= max_chars:
            low = middle
        else:
            high = middle - 1
    message.content = _excerpt(original, low)
    return fixed_size + len(json.dumps(message.content, ensure_ascii=False))


def _older_turns(messages: Sequence[Message]) -> list[list[Message]]:
    """系统消息不归入可移除轮次，最后一组始终保留。"""
    turns: list[list[Message]] = []
    for message in messages:
        if message.role in _PINNED_ROLES:
            continue
        if message.role == "user" or not turns:
            turns.append([])
        turns[-1].append(message)
    return turns[:-1]


def pack_context(messages: Sequence[Message], max_chars: int) -> list[Message]:
    """先节选文本，再整体移出旧轮次；返回独立副本，严格遵守 JSON 字符预算。

    不补齐、不执行工具；调用方须先为中断且结果未知的调用补明确的未知结果。
    最新用户输入、系统提示、工具参数、思考和 provider_data 均不截断。
    """
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("上下文字符预算必须是正整数")
    packed = deepcopy(list(messages))
    _validate_pairs(packed)
    current_size = _size(packed)
    if current_size <= max_chars:
        return packed

    last_user = next(
        (index for index in range(len(packed) - 1, -1, -1) if packed[index].role == "user"),
        -1,
    )
    for index, message in enumerate(packed):
        if message.role == "tool" or (
            index < last_user
            and message.role == "assistant"
            and not message.tool_calls
            and not message.provider_data
        ):
            current_size = _compress(message, current_size, max_chars)
            if current_size <= max_chars:
                break
    if current_size > max_chars:
        for turn in _older_turns(packed):
            removed = {id(message) for message in turn}
            packed = [message for message in packed if id(message) not in removed]
            # 列表元素之间的默认 JSON 分隔符是两个字符；最后一轮始终保留。
            current_size -= sum(
                len(json.dumps(asdict(message), ensure_ascii=False)) + 2 for message in turn
            )
            if current_size <= max_chars:
                break

    _validate_pairs(packed)
    if _size(packed) > max_chars:
        raise ContextLimitError(
            "最新用户输入、系统提示或当前轮原生协议数据超过上下文字符预算；"
            "已完成操作仍保留在会话中，请拆分任务或提高上限"
        )
    return packed
