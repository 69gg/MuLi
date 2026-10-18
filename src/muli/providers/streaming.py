"""SSE 解帧及各协议的增量累计，不执行工具。"""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import httpx

from .codecs import object_list, object_value, parse_turn, text_value
from .types import JsonObject, ModelTurn, ProviderError, StreamEvent, ToolCall


async def sse_events(response: httpx.Response, limit: int) -> AsyncIterator[JsonObject]:
    """按字节限制整个响应；支持 CRLF、多行 data 和没有末尾空行的帧。"""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    buffer = ""
    data: list[str] = []
    event = ""
    received = 0

    def consume(line: str) -> JsonObject | None:
        nonlocal event
        if not line:
            if not data:
                event = ""
                return None
            raw = "\n".join(data)
            data.clear()
            if raw.strip() == "[DONE]":
                result: JsonObject = {"_done": True}
            else:
                try:
                    value: Any = json.loads(raw)
                except ValueError as exc:
                    raise ProviderError("上游 SSE 返回无效 JSON") from exc
                if not isinstance(value, dict):
                    raise ProviderError("上游 SSE 帧必须是 JSON 对象")
                result = value
                if event and "type" not in result:
                    result["type"] = event
            event = ""
            return result
        if line.startswith(":"):
            return None
        name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value
        return None

    try:
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > limit:
                raise ProviderError("模型响应超过 max_response_bytes 限制")
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                item = consume(line.removesuffix("\r"))
                if item is not None:
                    yield item
        buffer += decoder.decode(b"", final=True)
    except UnicodeError as exc:
        raise ProviderError("上游 SSE 返回无效 UTF-8") from exc
    if buffer:
        item = consume(buffer.removesuffix("\r"))
        if item is not None:
            yield item
    item = consume("")
    if item is not None:
        yield item


def _merge_delta(target: JsonObject, delta: JsonObject) -> None:
    """累加文本碎片，保留兼容提供方附加的思维签名等 JSON 元数据。"""
    for key, value in delta.items():
        if isinstance(value, dict):
            current = target.setdefault(key, {})
            if isinstance(current, dict):
                _merge_delta(current, value)
            else:
                target[key] = deepcopy(value)
        elif isinstance(value, list):
            current_list = target.setdefault(key, [])
            if not isinstance(current_list, list):
                target[key] = deepcopy(value)
                continue
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("index"), int):
                    matching = next(
                        (
                            entry
                            for entry in current_list
                            if isinstance(entry, dict) and entry.get("index") == item["index"]
                        ),
                        None,
                    )
                    if matching is not None:
                        _merge_delta(matching, item)
                    else:
                        current_list.append(deepcopy(item))
                else:
                    current_list.append(deepcopy(item))
        elif (
            isinstance(value, str)
            and isinstance(target.get(key), str)
            and key not in {"id", "call_id", "type", "role", "status"}
        ):
            target[key] += value
        elif value is not None:
            target[key] = value


def _call_delta(call: JsonObject, arguments: str = "", *, index: int) -> StreamEvent:
    function = object_value(call.get("function"))
    return StreamEvent(
        "tool_call_delta",
        tool_call=ToolCall(
            str(call.get("call_id") or call.get("id") or ""),
            str(function.get("name") or call.get("name") or ""),
            arguments,
        ),
        index=index,
    )


def _index(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderError("上游流式索引必须是非负整数")
    return value


class StreamAccumulator:
    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        self.payload: JsonObject = {}
        self.items: dict[int, JsonObject] = {}
        self.completed_items: set[int] = set()
        self.arguments: dict[int, str] = {}
        self.message: JsonObject = {"role": "assistant", "content": ""}
        self.chat_tools: dict[int, JsonObject] = {}
        self.finished = False
        self.seen = False

    def feed(self, event: JsonObject) -> list[StreamEvent]:
        event_type = event.get("type", "")
        if event_type == "error" or event.get("error"):
            error = object_value(event.get("error"))
            kind = str(error.get("type") or error.get("code") or event.get("code") or "")
            retryable = kind in {
                "rate_limit_error",
                "rate_limit_exceeded",
                "overloaded_error",
                "server_error",
                "api_error",
            }
            raise ProviderError("上游模型在流式响应中返回错误", retryable=retryable)
        if self.protocol == "openai_chat":
            return self._chat(event)
        if self.protocol == "openai_responses":
            return self._responses(event)
        return self._anthropic(event)

    def _chat(self, event: JsonObject) -> list[StreamEvent]:
        result: list[StreamEvent] = []
        if event.get("_done"):
            self.finished = True
            return result
        if event.get("id"):
            self.payload["id"] = event["id"]
        if isinstance(event.get("usage"), dict):
            self.payload["usage"] = event["usage"]
        choices = object_list(event.get("choices"))
        if not choices:
            return result
        choice = next((item for item in choices if item.get("index", 0) == 0), None)
        if choice is None:
            return result
        self.seen = True
        delta = deepcopy(object_value(choice.get("delta")))
        for call in object_list(delta.pop("tool_calls", [])):
            index = _index(call.pop("index", 0))
            target = self.chat_tools.setdefault(index, {})
            arguments = str(object_value(call.get("function")).get("arguments") or "")
            _merge_delta(target, call)
            result.append(_call_delta(target, arguments, index=index))
        for key in ("content", "reasoning_content", "reasoning"):
            text = text_value(delta.get(key))
            if text:
                result.append(
                    StreamEvent(
                        "text_delta" if key == "content" else "reasoning_delta",
                        text=text,
                    )
                )
        _merge_delta(self.message, delta)
        if choice.get("finish_reason") is not None:
            self.payload["finish_reason"] = choice["finish_reason"]
        return result

    def _responses(self, event: JsonObject) -> list[StreamEvent]:
        kind = str(event.get("type") or "")
        index = _index(event.get("output_index", 0))
        if kind in {"response.created", "response.in_progress"}:
            self.payload.update(deepcopy(object_value(event.get("response"))))
            self.seen = True
        elif kind in {"response.output_item.added", "response.output_item.done"}:
            item = deepcopy(object_value(event.get("item")))
            self.items[index] = item
            self.seen = True
            if kind.endswith(".done"):
                self.completed_items.add(index)
            if item.get("type") == "function_call" and kind.endswith(".added"):
                return [_call_delta(item, str(item.get("arguments") or ""), index=index)]
        elif kind == "response.function_call_arguments.delta":
            item = self.items.setdefault(index, {"type": "function_call"})
            delta = str(event.get("delta") or "")
            item["arguments"] = str(item.get("arguments") or "") + delta
            return [_call_delta(item, delta, index=index)]
        elif kind in {
            "response.output_text.delta",
            "response.refusal.delta",
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            delta = str(event.get("delta") or "")
            reasoning = "reasoning" in kind
            item = self.items.setdefault(
                index,
                {"type": "reasoning" if reasoning else "message", "role": "assistant"},
            )
            field = "summary" if "summary" in kind else "content"
            part_index = _index(event.get("summary_index", event.get("content_index", 0)))
            parts = item.setdefault(field, [])
            part_type = (
                "summary_text"
                if field == "summary"
                else "reasoning_text"
                if reasoning
                else "refusal"
                if "refusal" in kind
                else "output_text"
            )
            if part_index > len(parts):
                raise ProviderError("Responses 流式内容块顺序无效")
            while len(parts) <= part_index:
                parts.append({"type": part_type, "text": ""})
            text_key = "refusal" if part_type == "refusal" else "text"
            parts[part_index][text_key] = str(parts[part_index].get(text_key) or "") + delta
            return (
                [StreamEvent("reasoning_delta" if reasoning else "text_delta", text=delta)]
                if delta
                else []
            )
        elif kind in {"response.completed", "response.incomplete"}:
            self.payload.update(deepcopy(object_value(event.get("response"))))
            self.payload.setdefault(
                "status", "completed" if kind.endswith("completed") else "incomplete"
            )
            output = object_list(self.payload.get("output"))
            if output:
                for output_index, item in enumerate(output):
                    if output_index not in self.completed_items:
                        self.items[output_index] = item
            self.payload["output"] = [self.items[key] for key in sorted(self.items)]
            self.finished = True
            self.seen = True
        elif kind in {"response.failed", "response.cancelled"}:
            raise ProviderError("上游 Responses 请求失败")
        return []

    def _anthropic(self, event: JsonObject) -> list[StreamEvent]:
        kind = str(event.get("type") or "")
        index = _index(event.get("index", 0))
        if kind == "message_start":
            self.payload = deepcopy(object_value(event.get("message")))
            self.seen = True
        elif kind == "content_block_start":
            block = deepcopy(object_value(event.get("content_block")))
            self.items[index] = block
            if block.get("type") == "tool_use":
                return [_call_delta(block, index=index)]
            text = text_value(block.get("text") or block.get("thinking"))
            if text:
                return [
                    StreamEvent(
                        "reasoning_delta" if block.get("type") == "thinking" else "text_delta",
                        text=text,
                    )
                ]
        elif kind == "content_block_delta":
            if index not in self.items:
                raise ProviderError("Anthropic 流式内容块顺序无效")
            block = self.items[index]
            delta = object_value(event.get("delta"))
            delta_type = delta.get("type")
            if delta_type == "input_json_delta":
                fragment = str(delta.get("partial_json") or "")
                self.arguments[index] = self.arguments.get(index, "") + fragment
                return [_call_delta(block, fragment, index=index)]
            if delta_type in {"text_delta", "thinking_delta", "signature_delta"}:
                field = {
                    "text_delta": "text",
                    "thinking_delta": "thinking",
                    "signature_delta": "signature",
                }[delta_type]
                fragment = str(delta.get(field) or "")
                block[field] = str(block.get(field) or "") + fragment
                if delta_type != "signature_delta" and fragment:
                    return [
                        StreamEvent(
                            "text_delta" if delta_type == "text_delta" else "reasoning_delta",
                            text=fragment,
                        )
                    ]
        elif kind == "content_block_stop":
            if index in self.arguments:
                try:
                    arguments: Any = json.loads(self.arguments[index])
                except ValueError as exc:
                    raise ProviderError("Anthropic 返回了不完整的工具参数 JSON") from exc
                if not isinstance(arguments, dict):
                    raise ProviderError("Anthropic 工具参数必须是 JSON 对象")
                self.items[index]["input"] = arguments
        elif kind == "message_delta":
            self.payload.update(deepcopy(object_value(event.get("delta"))))
            self.payload.setdefault("usage", {}).update(object_value(event.get("usage")))
        elif kind == "message_stop":
            self.payload["content"] = [self.items[key] for key in sorted(self.items)]
            self.finished = True
        return []

    def finish(self) -> ModelTurn:
        if self.protocol == "openai_chat":
            if self.chat_tools:
                self.message["tool_calls"] = [
                    self.chat_tools[key] for key in sorted(self.chat_tools)
                ]
                if not self.message.get("content"):
                    self.message["content"] = None
            self.finished = self.finished or self.payload.get("finish_reason") is not None
            self.payload["choices"] = [
                {
                    "message": self.message,
                    "finish_reason": self.payload.pop("finish_reason", None),
                }
            ]
        if not self.seen or not self.finished:
            raise ProviderError("上游流式响应在完成标记前中断", retryable=True)
        return parse_turn(self.payload, self.protocol)
