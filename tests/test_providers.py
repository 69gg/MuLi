from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from typing import Any

import httpx
import pytest

from muli.providers import Message, ModelClient, ModelConfig, ProviderError, ToolCall
from muli.providers.codecs import build_request
from muli.providers.streaming import sse_events
from muli.providers.types import JsonObject, ProtocolName

TOOLS: list[JsonObject] = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "查询",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }
]


def config(protocol: ProtocolName = "openai_chat", **options: Any) -> ModelConfig:
    return ModelConfig(
        protocol=protocol,
        model="test-model",
        base_url="https://example.test/v1/",
        api_key="secret-key",
        retry_backoff=0,
        **options,
    )


def payload(protocol: str) -> JsonObject:
    if protocol == "openai_chat":
        return {
            "id": "chat_1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "你好",
                        "reasoning_content": "说明",
                        "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}],
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{ "q": "词" }',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "completion_tokens_details": {"reasoning_tokens": 2},
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        }
    if protocol == "openai_responses":
        return {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "说明"}],
                    "encrypted_content": "opaque",
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "你好", "annotations": []}],
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": '{ "q": "词" }',
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "output_tokens_details": {"reasoning_tokens": 2},
                "input_tokens_details": {"cached_tokens": 3},
            },
        }
    return {
        "id": "msg_1",
        "role": "assistant",
        "type": "message",
        "content": [
            {"type": "thinking", "thinking": "说明", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": "你好"},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "lookup",
                "input": {"q": "词"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 7, "cache_read_input_tokens": 3, "output_tokens": 4},
    }


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, failure: bool = False, chunk_size: int = 19) -> None:
        self.data = data
        self.failure = failure
        self.chunk_size = chunk_size
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.data), self.chunk_size):
            yield self.data[offset : offset + self.chunk_size]
        if self.failure:
            raise httpx.ReadError("secret upstream error text")

    async def aclose(self) -> None:
        self.closed = True


def sse(events: list[JsonObject | str]) -> bytes:
    return "".join(
        "data: "
        + (event if isinstance(event, str) else json.dumps(event, ensure_ascii=False))
        + "\r\n\r\n"
        for event in events
    ).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_native_protocol_tool_roundtrip_and_usage(protocol: ProtocolName) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload(protocol))

    original_messages = [Message("system", "规则"), Message("user", "查询")]
    untouched = deepcopy(original_messages)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ModelClient(config(protocol, stream=False), http) as client:
            turn = await client.complete(original_messages, TOOLS)
            await client.complete(
                [
                    *original_messages,
                    turn.to_message(),
                    Message("tool", "结果", tool_call_id="call_1"),
                    Message("tool", "第二个结果", tool_call_id="call_2"),
                ],
                TOOLS,
            )
        assert not http.is_closed
    assert original_messages == untouched
    assert turn.content == "你好" and turn.reasoning == "说明"
    assert turn.tool_calls[0].id == "call_1"
    assert json.loads(turn.tool_calls[0].arguments) == {"q": "词"}
    assert turn.usage.input_tokens == 10
    assert turn.usage.output_tokens == 4 and turn.usage.cached_tokens == 3
    wire = json.loads(requests[1].content)
    if protocol == "openai_chat":
        assert requests[0].url.path == "/v1/chat/completions"
        assert requests[0].headers["authorization"] == "Bearer secret-key"
        assert wire["messages"][2] == payload(protocol)["choices"][0]["message"]
        assert wire["max_completion_tokens"] == 4096
        assert turn.usage.reasoning_tokens == 2
    elif protocol == "openai_responses":
        assert requests[0].url.path == "/v1/responses"
        assert wire["input"][2:5] == payload(protocol)["output"]
        assert wire["input"][5] == {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "结果",
        }
        assert wire["store"] is False
        assert wire["tools"][0]["name"] == "lookup"
        assert wire["max_output_tokens"] == 4096
    else:
        assert requests[0].url.path == "/v1/messages"
        assert requests[0].headers["x-api-key"] == "secret-key"
        assert requests[0].headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in requests[0].headers
        assert wire["system"] == "规则"
        assert wire["messages"][1]["content"] == payload(protocol)["content"]
        assert [block["tool_use_id"] for block in wire["messages"][2]["content"]] == [
            "call_1",
            "call_2",
        ]
        assert wire["tools"][0]["input_schema"] == TOOLS[0]["function"]["parameters"]
        assert wire["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_chat_stream_reassembles_tool_arguments_metadata_usage() -> None:
    stream = BytesStream(
        sse(
            [
                {
                    "id": "chat_1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "reasoning_content": "想",
                                "reasoning_details": [
                                    {
                                        "index": 0,
                                        "type": "reasoning.encrypted",
                                        "data": "abc",
                                    }
                                ],
                            },
                        }
                    ],
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "你",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"q":',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "好",
                                "reasoning_details": [
                                    {
                                        "index": 0,
                                        "type": "reasoning.encrypted",
                                        "data": "def",
                                    }
                                ],
                                "tool_calls": [{"index": 0, "function": {"arguments": '"词"}'}}],
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}},
                "[DONE]",
            ]
        ),
        chunk_size=1,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    ) as http:
        events = [
            event
            async for event in ModelClient(config(), http).stream([Message("user", "你好")], TOOLS)
        ]
    assert "".join(event.text for event in events if event.kind == "text_delta") == "你好"
    assert events[-1].kind == "done"
    turn = events[-1].turn
    assert turn is not None
    assert turn.tool_calls == [ToolCall("call_1", "lookup", '{"q":"词"}')]
    assert turn.usage.input_tokens == 9 and turn.reasoning == "想"
    assert turn.provider_data["message"]["reasoning_details"][0]["data"] == "abcdef"
    assert stream.closed


@pytest.mark.asyncio
async def test_responses_stream_preserves_completed_reasoning_and_function_items() -> None:
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "想"}],
        "encrypted_content": "complete-signature",
    }
    call = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{ "q": "词" }',
    }
    native = payload("openai_responses")
    native["output"][0] = {**reasoning, "encrypted_content": "incomplete-signature"}
    native["output"][2] = call
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp_1", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "incomplete-signature",
            },
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "summary_index": 0,
            "delta": "想",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {
            "type": "response.output_text.delta",
            "output_index": 1,
            "content_index": 0,
            "delta": "你好",
        },
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {**call, "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 2,
            "delta": '{ "q": "词" }',
        },
        {"type": "response.output_item.done", "output_index": 2, "item": call},
        {"type": "response.completed", "response": native},
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(events)
            )
        )
    ) as http:
        turn = await ModelClient(config("openai_responses"), http).complete(
            [Message("user", "查词")], TOOLS
        )
    assert turn.content == "你好" and turn.reasoning == "想"
    assert turn.provider_data["output_items"][0] == reasoning
    assert turn.provider_data["output_items"][2] == call
    wire = build_request(
        config("openai_responses"),
        [turn.to_message(), Message("tool", "结果", tool_call_id="call_1")],
        TOOLS,
        stream=True,
    )
    assert wire["input"][0] == reasoning
    assert wire["input"][-1]["call_id"] == "call_1"


@pytest.mark.asyncio
async def test_anthropic_stream_keeps_signature_redacted_blocks_and_tool_input() -> None:
    events: list[JsonObject | str] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "content": [],
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 2,
                    "output_tokens": 0,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "想"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "opaque"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "call_1",
                "name": "lookup",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"词"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "content_block_start",
            "index": 3,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "text_delta", "text": "你好"},
        },
        {"type": "content_block_stop", "index": 3},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(events)
            )
        )
    ) as http:
        turn = await ModelClient(config("anthropic"), http).complete(
            [Message("user", "查词")], TOOLS
        )
    assert turn.content == "你好" and turn.reasoning == "想"
    assert turn.usage.input_tokens == 7 and turn.usage.output_tokens == 4
    assert turn.tool_calls == [ToolCall("call_1", "lookup", '{"q":"词"}')]
    blocks = turn.provider_data["content_blocks"]
    assert blocks[:2] == [
        {"type": "thinking", "thinking": "想", "signature": "sig"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [429, 503])
async def test_transient_failure_retries_before_output(initial_status: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(initial_status, headers={"retry-after": "0"})
            if calls == 1
            else httpx.Response(200, json=payload("openai_chat"))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        turn = await ModelClient(config(stream=False), http).complete([Message("user", "hi")])
    assert calls == 2 and turn.content == "你好"


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [False, True])
async def test_stream_disconnect_retries_only_before_output(visible: bool) -> None:
    calls = 0
    received: list[str] = []
    failed = BytesStream(
        sse([{"choices": [{"delta": {"content": "已输出"}}]}]) if visible else b"",
        failure=True,
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=failed)
        return httpx.Response(200, json=payload("openai_chat"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelClient(config(), http)
        if visible:
            with pytest.raises(ProviderError) as error:
                async for event in client.stream([Message("user", "hi")]):
                    received.append(event.text)
            assert error.value.partial
            assert "secret" not in str(error.value)
            assert calls == 1 and received == ["已输出"]
        else:
            turn = await client.complete([Message("user", "hi")])
            assert turn.content == "你好" and calls == 2
    assert failed.closed


@pytest.mark.asyncio
async def test_auth_error_has_no_retry_or_secret_disclosure() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="Authorization: secret-key")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProviderError, match="HTTP 401") as error:
            await ModelClient(config(), http).complete([Message("user", "hi")])
    assert calls == 1 and "secret-key" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_response_byte_limit(stream: bool) -> None:
    data = (
        sse([{"choices": [{"delta": {"content": "x" * 100}}]}])
        if stream
        else json.dumps(payload("openai_chat")).encode()
    )
    headers = {"content-type": "text/event-stream" if stream else "application/json"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, content=data))
    ) as http:
        with pytest.raises(ProviderError, match="max_response_bytes"):
            await ModelClient(config(stream=stream, max_response_bytes=50), http).complete(
                [Message("user", "hi")]
            )


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "messages",
        "input",
        "tools",
        "auth",
        "api_key",
        "headers",
        "stream",
        "system",
        "instructions",
        "previous_response_id",
    ],
)
def test_core_fields_cannot_be_overridden(field: str) -> None:
    with pytest.raises(ValueError, match="核心字段"):
        build_request(config(extra_body={field: "bad"}), [Message("user", "hi")], [], stream=False)


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
def test_sampling_tool_choice_reasoning_and_token_limits(
    protocol: ProtocolName,
) -> None:
    reasoning = (
        {"type": "enabled", "budget_tokens": 128} if protocol == "anthropic" else {"effort": "high"}
    )
    extra: JsonObject = {
        "temperature": 0.2,
        "top_p": 0.9,
        "seed": 7,
        "stop": ["END"],
        "parallel_tool_calls": False,
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    }
    limit_field = {
        "openai_chat": "max_tokens",
        "openai_responses": "max_output_tokens",
        "anthropic": "max_tokens",
    }[protocol]
    extra[limit_field] = 500
    body = build_request(
        config(protocol, max_output_tokens=100, reasoning=reasoning, extra_body=extra),
        [Message("user", "hi")],
        TOOLS,
        stream=True,
    )
    assert body[limit_field] == 100
    assert body["temperature"] == 0.2 and body["top_p"] == 0.9 and body["seed"] == 7
    if protocol == "openai_chat":
        assert body["reasoning_effort"] == "high"
        assert body["tool_choice"] == extra["tool_choice"]
        assert body["stream_options"]["include_usage"] is True
        assert "max_completion_tokens" not in body
    elif protocol == "openai_responses":
        assert body["reasoning"] == reasoning
        assert body["tool_choice"] == {"type": "function", "name": "lookup"}
    else:
        assert body["thinking"] == reasoning
        assert body["tool_choice"] == {
            "type": "tool",
            "name": "lookup",
            "disable_parallel_tool_use": True,
        }
        assert body["stop_sequences"] == ["END"]


def test_config_public_serialization_and_repr_hide_all_header_values() -> None:
    settings = config(
        extra_headers={
            "X-Custom-Secret": "header-secret",
            "Anthropic-Version": "version-value",
        }
    )
    public = settings.to_dict()
    assert public["api_key"] == ""
    assert set(public["extra_headers"].values()) == {"***"}
    assert "secret-key" not in repr(settings) and "header-secret" not in repr(settings)
    assert settings.to_dict(include_secrets=True)["api_key"] == "secret-key"


def test_replay_metadata_is_deep_copied() -> None:
    from muli.providers.codecs import parse_turn

    turn = parse_turn(payload("anthropic"), "anthropic")
    message = turn.to_message()
    message.provider_data["content_blocks"][0]["signature"] = "modified"
    assert turn.provider_data["content_blocks"][0]["signature"] == "sig"


@pytest.mark.asyncio
async def test_sse_multiline_bom_comment_and_final_frame() -> None:
    data = '\ufeff: keepalive\r\nevent: ping\r\ndata: {"ok":\r\ndata: true}\r\n\r\ndata: {"last": true}'.encode()
    response = httpx.Response(200, stream=BytesStream(data, chunk_size=1))
    events = [item async for item in sse_events(response, 1024)]
    assert events == [{"ok": True, "type": "ping"}, {"last": True}]


@pytest.mark.asyncio
async def test_clean_eof_without_completion_is_not_success() -> None:
    data = sse([{"choices": [{"delta": {"content": "半截"}}]}])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=data
            )
        )
    ) as http:
        with pytest.raises(ProviderError, match="完成标记") as error:
            await ModelClient(config(), http).complete([Message("user", "hi")])
    assert error.value.partial


@pytest.mark.parametrize(
    "options",
    [
        {"protocol": "unknown"},
        {"max_output_tokens": 0},
        {"max_response_bytes": 0},
        {"timeout": 0},
        {"max_retries": -1},
        {"base_url": "https://secret@example.test/v1"},
        {"base_url": "https://example.test/v1?key=secret"},
    ],
)
def test_invalid_config(options: JsonObject) -> None:
    with pytest.raises(ValueError):
        replace(config(), **options)
