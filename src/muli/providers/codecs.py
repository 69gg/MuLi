"""三种 HTTP 协议的纯 JSON 编解码。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from .types import (
    JsonObject,
    Message,
    ModelConfig,
    ModelTurn,
    ProviderError,
    ToolCall,
    Usage,
)

_PROTECTED_FIELDS = frozenset(
    {
        "model",
        "messages",
        "input",
        "tools",
        "stream",
        "system",
        "instructions",
        "api_key",
        "auth",
        "headers",
        "extra_headers",
        "base_url",
        "timeout",
        "previous_response_id",
        "conversation",
        "n",
    }
)


def object_value(value: Any) -> JsonObject:
    return value if isinstance(value, dict) else {}


def object_list(value: Any) -> list[JsonObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or item.get("refusal") or "")
            for item in value
            if isinstance(item, dict)
            and item.get("type")
            in {"text", "output_text", "summary_text", "reasoning_text", "refusal"}
        )
    return ""


def _function_call(call: ToolCall) -> JsonObject:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


def _tool_id(message: Message) -> str:
    if not message.tool_call_id:
        raise ValueError("工具结果缺少 tool_call_id")
    return message.tool_call_id


def _chat_messages(messages: Sequence[Message]) -> list[JsonObject]:
    result: list[JsonObject] = []
    for message in messages:
        if message.role == "assistant" and message.provider_data.get("protocol") == "openai_chat":
            native = message.provider_data.get("message")
            if isinstance(native, dict):
                result.append(deepcopy(native))
                continue
        item: JsonObject = {"role": message.role, "content": message.content}
        if message.role == "tool":
            item["tool_call_id"] = _tool_id(message)
        if message.tool_calls:
            item["tool_calls"] = [_function_call(call) for call in message.tool_calls]
            if not message.content:
                item["content"] = None
        if message.reasoning:
            item["reasoning_content"] = message.reasoning
        result.append(item)
    return result


def _responses_input(messages: Sequence[Message]) -> list[JsonObject]:
    result: list[JsonObject] = []
    for message in messages:
        if message.role == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": _tool_id(message),
                    "output": message.content,
                }
            )
            continue
        if (
            message.role == "assistant"
            and message.provider_data.get("protocol") == "openai_responses"
        ):
            native = message.provider_data.get("output_items")
            if isinstance(native, list):
                result.extend(deepcopy(native))
                continue
        if message.content or message.role != "assistant":
            result.append({"role": message.role, "content": message.content})
        result.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in message.tool_calls
        )
    return result


def _anthropic_messages(messages: Sequence[Message]) -> tuple[list[JsonObject], str]:
    result: list[JsonObject] = []
    system: list[str] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            system.append(message.content)
            continue
        role = "assistant" if message.role == "assistant" else "user"
        blocks: list[JsonObject] = []
        if message.role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": _tool_id(message),
                    "content": message.content,
                }
            ]
        elif message.role == "assistant" and message.provider_data.get("protocol") == "anthropic":
            native = message.provider_data.get("content_blocks")
            if isinstance(native, list):
                blocks = deepcopy(native)
        if not blocks:
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                try:
                    arguments: Any = json.loads(call.arguments)
                except ValueError as exc:
                    raise ValueError("Anthropic 工具参数必须是完整 JSON 对象") from exc
                if not isinstance(arguments, dict):
                    raise ValueError("Anthropic 工具参数必须是 JSON 对象")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": arguments,
                    }
                )
        if not blocks:
            raise ValueError("Anthropic 消息不能为空")
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    return result, "\n\n".join(system)


def _tools(tools: Sequence[JsonObject], protocol: str) -> list[JsonObject]:
    result: list[JsonObject] = []
    for tool in tools:
        function = object_value(tool.get("function"))
        if tool.get("type") != "function" or not function.get("name"):
            raise ValueError("工具定义必须使用 OpenAI function schema")
        if protocol == "openai_chat":
            result.append(deepcopy(tool))
        elif protocol == "openai_responses":
            result.append({"type": "function", **deepcopy(function)})
        else:
            converted: JsonObject = {
                "name": function["name"],
                "input_schema": deepcopy(
                    function.get("parameters", {"type": "object", "properties": {}})
                ),
            }
            if "description" in function:
                converted["description"] = function["description"]
            result.append(converted)
    return result


def _tool_choice(choice: Any, protocol: str) -> Any:
    if protocol == "openai_chat":
        return choice
    if isinstance(choice, dict):
        name = object_value(choice.get("function")).get("name") or choice.get("name")
        if name:
            return {
                "type": "tool" if protocol == "anthropic" else "function",
                "name": name,
            }
    if protocol == "anthropic" and isinstance(choice, str):
        return {"type": {"required": "any"}.get(choice, choice)}
    return choice


def build_request(
    config: ModelConfig,
    messages: Sequence[Message],
    tools: Sequence[JsonObject],
    *,
    stream: bool,
) -> JsonObject:
    conflicts = _PROTECTED_FIELDS.intersection(config.extra_body)
    if conflicts:
        raise ValueError("extra_body 不允许覆盖协议核心字段：" + ", ".join(sorted(conflicts)))
    body: JsonObject = deepcopy(config.extra_body)
    body.update(model=config.model, stream=stream)
    if "tool_choice" in body:
        body["tool_choice"] = _tool_choice(body["tool_choice"], config.protocol)
    if config.protocol == "openai_chat":
        body["messages"] = _chat_messages(messages)
        token_field = (
            "max_tokens"
            if "max_tokens" in body and "max_completion_tokens" not in body
            else "max_completion_tokens"
        )
        other = "max_completion_tokens" if token_field == "max_tokens" else "max_tokens"
        if other in body:
            raise ValueError("Chat 输出限制只能选择 max_tokens 或 max_completion_tokens 之一")
        if config.reasoning:
            if set(config.reasoning) != {"effort"}:
                raise ValueError("Chat reasoning 仅接受 effort；厂商参数请放入 extra_body")
            body.setdefault("reasoning_effort", config.reasoning["effort"])
        if stream:
            options = object_value(body.get("stream_options"))
            body["stream_options"] = {"include_usage": True, **options}
    elif config.protocol == "openai_responses":
        body["input"] = _responses_input(messages)
        body.setdefault("store", False)
        token_field = "max_output_tokens"
        if config.reasoning:
            body.setdefault("reasoning", deepcopy(config.reasoning))
        if "include" not in body:
            body["include"] = ["reasoning.encrypted_content"]
    else:
        body["messages"], system = _anthropic_messages(messages)
        if system:
            body["system"] = system
        token_field = "max_tokens"
        if config.reasoning:
            body.setdefault("thinking", deepcopy(config.reasoning))
        if "stop" in body:
            body.setdefault("stop_sequences", body.pop("stop"))
        if "parallel_tool_calls" in body:
            choice = object_value(body.get("tool_choice")) or {"type": "auto"}
            choice["disable_parallel_tool_use"] = not body.pop("parallel_tool_calls")
            body["tool_choice"] = choice
    requested_limit = body.get(token_field, config.max_output_tokens)
    if (
        not isinstance(requested_limit, int)
        or isinstance(requested_limit, bool)
        or requested_limit <= 0
    ):
        raise ValueError("模型输出 token 限制必须是正整数")
    body[token_field] = min(requested_limit, config.max_output_tokens)
    if tools:
        body["tools"] = _tools(tools, config.protocol)
    return body


def parse_usage(raw: JsonObject, protocol: str) -> Usage:
    def count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    if protocol == "openai_chat":
        details = object_value(raw.get("completion_tokens_details"))
        cached = object_value(raw.get("prompt_tokens_details")).get("cached_tokens")
        input_tokens, output_tokens = (
            raw.get("prompt_tokens"),
            raw.get("completion_tokens"),
        )
    else:
        details = object_value(raw.get("output_tokens_details"))
        cached = (
            raw.get("cache_read_input_tokens")
            if protocol == "anthropic"
            else object_value(raw.get("input_tokens_details")).get("cached_tokens")
        )
        input_tokens, output_tokens = raw.get("input_tokens"), raw.get("output_tokens")
        if protocol == "anthropic":
            input_tokens = (
                count(input_tokens)
                + count(raw.get("cache_read_input_tokens"))
                + count(raw.get("cache_creation_input_tokens"))
            )
    return Usage(
        count(input_tokens),
        count(output_tokens),
        count(details.get("reasoning_tokens")),
        count(cached),
        deepcopy(raw),
    )


def parse_turn(payload: JsonObject, protocol: str) -> ModelTurn:
    if payload.get("error") or payload.get("status") in {"failed", "cancelled"}:
        raise ProviderError("上游模型返回失败状态")
    turn = ModelTurn(
        response_id=payload.get("id"),
        usage=parse_usage(object_value(payload.get("usage")), protocol),
    )
    if protocol == "openai_chat":
        choices = object_list(payload.get("choices"))
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise ProviderError("Chat 响应缺少 assistant 消息")
        choice, message = choices[0], choices[0]["message"]
        turn.content = text_value(message.get("content")) or text_value(message.get("refusal"))
        turn.reasoning = text_value(message.get("reasoning_content")) or text_value(
            message.get("reasoning")
        )
        turn.tool_calls = [
            ToolCall(
                str(call.get("id") or ""),
                str(object_value(call.get("function")).get("name") or ""),
                str(object_value(call.get("function")).get("arguments") or ""),
            )
            for call in object_list(message.get("tool_calls"))
        ]
        turn.finish_reason = choice.get("finish_reason")
        turn.provider_data = {"protocol": protocol, "message": deepcopy(message)}
    elif protocol == "openai_responses":
        output = object_list(payload.get("output"))
        if not isinstance(payload.get("output"), list):
            raise ProviderError("Responses 响应缺少 output")
        for item in output:
            if item.get("type") == "message":
                turn.content += text_value(item.get("content"))
            elif item.get("type") == "reasoning":
                turn.reasoning += text_value(item.get("summary")) or text_value(item.get("content"))
            elif item.get("type") == "function_call":
                turn.tool_calls.append(
                    ToolCall(
                        str(item.get("call_id") or ""),
                        str(item.get("name") or ""),
                        str(item.get("arguments") or ""),
                    )
                )
        turn.finish_reason = str(
            object_value(payload.get("incomplete_details")).get("reason")
            or payload.get("status")
            or "completed"
        )
        turn.provider_data = {"protocol": protocol, "output_items": deepcopy(output)}
    else:
        blocks = object_list(payload.get("content"))
        if not isinstance(payload.get("content"), list):
            raise ProviderError("Anthropic 响应缺少 content")
        for block in blocks:
            if block.get("type") == "text":
                turn.content += text_value(block.get("text"))
            elif block.get("type") == "thinking":
                turn.reasoning += text_value(block.get("thinking"))
            elif block.get("type") == "tool_use":
                turn.tool_calls.append(
                    ToolCall(
                        str(block.get("id") or ""),
                        str(block.get("name") or ""),
                        json.dumps(
                            block.get("input", {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
        turn.finish_reason = payload.get("stop_reason")
        turn.provider_data = {"protocol": protocol, "content_blocks": deepcopy(blocks)}
    if any(not call.id or not call.name for call in turn.tool_calls):
        raise ProviderError("上游返回的工具调用缺少 id 或 name")
    return turn
