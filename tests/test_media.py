from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from typing import Any

import httpx
import pytest

from muli.media import MediaConfig, MediaService, RerankResult, ServiceConfig
from muli.providers import ModelConfig, ProviderError
from muli.providers.types import JsonObject, ProtocolName

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt "


def service_config(**options: Any) -> ServiceConfig:
    return ServiceConfig(
        model="service-model", base_url="https://example.test/v1", api_key="secret-key", **options
    )


def model_config(protocol: ProtocolName = "openai_chat", **options: Any) -> ModelConfig:
    if protocol == "anthropic":
        options.setdefault("max_output_tokens", 4096)
    return ModelConfig(
        protocol=protocol,
        model="vision-model",
        base_url="https://vision.test/v1",
        stream=False,
        **options,
    )


def vision_payload(protocol: ProtocolName) -> JsonObject:
    if protocol == "openai_chat":
        return {"choices": [{"message": {"role": "assistant", "content": "一只猫"}}]}
    if protocol == "openai_responses":
        return {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "一只猫"}],
                }
            ]
        }
    return {"content": [{"type": "text", "text": "一只猫"}], "stop_reason": "end_turn"}


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.block = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        await self.block.wait()
        yield AUDIO

    async def aclose(self) -> None:
        self.closed = True


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.data), 7):
            yield self.data[offset : offset + 7]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
@pytest.mark.parametrize("main_vision", [True, False])
async def test_explicit_vision_routes_to_main_or_fallback(
    protocol: ProtocolName, main_vision: bool
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=vision_payload(protocol))

    primary = replace(model_config(protocol, vision=main_vision), base_url="https://main.test/v1")
    config = MediaConfig(vision=model_config(protocol))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with MediaService(config, primary, http) as media:
            assert media.status()["vision_source"] == ("main" if main_vision else "vision")
            assert not requests
            assert await media.describe_image(PNG, "image/png", "图片里有什么？") == "一只猫"
        assert not http.is_closed
    assert len(requests) == 1
    assert requests[0].url.host == ("main.test" if main_vision else "vision.test")
    body = json.loads(requests[0].content)
    messages = body["input"] if protocol == "openai_responses" else body["messages"]
    parts = messages[0]["content"]
    assert parts[0]["text"] == "图片里有什么？"
    assert "base64" in json.dumps(parts) and "image/png" in json.dumps(parts)
    assert primary.vision == main_vision and config.vision is not None and not config.vision.vision


@pytest.mark.asyncio
async def test_vision_requires_config_and_valid_bounded_image_without_network() -> None:
    async with MediaService(MediaConfig(), model_config()) as media:
        assert media.status()["vision"] is False
        with pytest.raises(ProviderError, match="外挂视觉"):
            await media.describe_image(PNG, "image/png", "看图")
    async with MediaService(MediaConfig(vision=model_config()), max_image_bytes=8) as media:
        with pytest.raises(ProviderError, match="max_image_bytes"):
            await media.describe_image(PNG, "image/png", "看图")
        with pytest.raises(ProviderError, match="MIME"):
            await media.describe_image(PNG, "image/jpeg", "看图")
        with pytest.raises(ProviderError, match="MIME"):
            await media.describe_image(b"<svg></svg>", "image/svg+xml", "看图")
        with pytest.raises(ProviderError, match="问题不能为空"):
            await media.describe_image(PNG, "image/png", " ")


@pytest.mark.asyncio
async def test_embedding_request_dimensions_and_response_indices() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        media = MediaService(
            MediaConfig(embedding=service_config(dimensions=2, extra_body={"user": "local-user"})),
            client=http,
        )
        assert await media.embed([]) == []
        assert not requests
        assert await media.embed(["甲", "乙"]) == [[1.0, 2.0], [3.0, 4.0]]
    request = requests[0]
    assert (
        request.url.path == "/v1/embeddings"
        and request.headers["authorization"] == "Bearer secret-key"
    )
    assert json.loads(request.content) == {
        "model": "service-model",
        "input": ["甲", "乙"],
        "encoding_format": "float",
        "dimensions": 2,
        "user": "local-user",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 1, "embedding": [1, 2]}],
        [{"index": True, "embedding": [1, 2]}],
        [{"index": 0, "embedding": []}],
        [{"index": 0, "embedding": [True, 2]}],
        [{"index": 0, "embedding": ["1", 2]}],
        [{"index": 0, "embedding": [1, 2, 3]}],
        [{"index": 0, "embedding": "base64-unsupported"}],
    ],
)
async def test_embedding_rejects_invalid_vectors(data: list[Any]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data}))
    ) as http:
        media = MediaService(MediaConfig(embedding=service_config(dimensions=2)), client=http)
        with pytest.raises(ProviderError, match="embedding"):
            await media.embed(["文本"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,texts",
    [
        (b'{"data":[{"index":0,"embedding":[NaN]}]}', ["甲"]),
        (b'{"data":[{"index":0,"embedding":[Infinity]}]}', ["甲"]),
        (b'{"data":[{"index":0,"embedding":[1]},{"index":0,"embedding":[2]}]}', ["甲", "乙"]),
    ],
)
async def test_embedding_rejects_nonfinite_and_duplicate_indices(
    payload: bytes, texts: list[str]
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as http:
        media = MediaService(MediaConfig(embedding=service_config()), client=http)
        with pytest.raises(ProviderError):
            await media.embed(texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["results", "data", "list"])
async def test_rerank_undefined_compatible_shapes_preserve_input_document(wrapper: str) -> None:
    requests: list[httpx.Request] = []
    results = [
        {"index": 2, "relevance_score": "0.9", "document": "上游改过的文本"},
        {"index": 0, "score": 0.2},
        {"index": 1, "similarity": -0.5},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=results if wrapper == "list" else {wrapper: results})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        media = MediaService(
            MediaConfig(reranker=service_config(query_instruction="查询：")), client=http
        )
        assert await media.rerank("问题", []) == []
        assert await media.rerank("问题", ["甲", "乙", "丙"], 2) == [
            RerankResult(2, 0.9, "丙"),
            RerankResult(0, 0.2, "甲"),
        ]
    assert requests[0].url.path == "/v1/rerank"
    assert json.loads(requests[0].content) == {
        "model": "service-model",
        "query": "查询：问题",
        "documents": ["甲", "乙", "丙"],
        "top_n": 2,
        "return_documents": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": -1, "score": 1}],
        [{"index": 3, "score": 1}],
        [{"index": 0, "score": "NaN"}],
        [{"index": 0}],
        [{"index": 0, "score": 0.8}, {"index": 0, "score": 0.2}],
    ],
)
async def test_rerank_invalid_results_do_not_invent_rankings(results: list[Any]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": results}))
    ) as http:
        media = MediaService(MediaConfig(reranker=service_config()), client=http)
        with pytest.raises(ProviderError):
            await media.rerank("query", ["甲", "乙"])


@pytest.mark.asyncio
async def test_asr_multipart_preserves_audio_and_optional_parameters() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "你好世界"})

    config = service_config(
        language="zh",
        extra_body={
            "include": ["logprobs"],
            "chunking_strategy": {"type": "server_vad", "threshold": 0.5},
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        media = MediaService(MediaConfig(asr=config), client=http)
        assert (
            await media.transcribe(AUDIO, "/private/录音.wav", "audio/wav", prompt="专有名词")
            == "你好世界"
        )
    request = requests[0]
    assert request.url.path == "/v1/audio/transcriptions"
    parsed = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    parts = {
        part.get_param("name", header="content-disposition"): part for part in parsed.iter_parts()
    }
    assert parts["file"].get_filename() == "录音.wav"
    assert parts["file"].get_payload(decode=True) == AUDIO
    assert parts["file"].get_content_type() == "audio/wav"
    assert parts["model"].get_payload(decode=True) == b"service-model"
    assert parts["language"].get_payload(decode=True) == b"zh"
    assert parts["prompt"].get_payload(decode=True) == "专有名词".encode()
    assert parts["include[]"].get_payload(decode=True) == b"logprobs"
    assert json.loads(parts["chunking_strategy"].get_payload(decode=True)) == {
        "type": "server_vad",
        "threshold": 0.5,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", ["text", "srt", "vtt", "verbose_json", "diarized_json"])
async def test_asr_configured_response_formats(response_format: str) -> None:
    text = "转写结果"
    data = (
        text.encode()
        if response_format in {"text", "srt", "vtt"}
        else json.dumps({"text": text}).encode()
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))
    ) as http:
        media = MediaService(
            MediaConfig(asr=service_config(extra_body={"response_format": response_format})),
            client=http,
        )
        assert await media.transcribe(AUDIO, "voice.wav", "audio/wav") == text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not-json secret-key", b'{"error":"secret-key"}', b"[]"])
async def test_asr_invalid_payload_is_safe(payload: bytes) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as http:
        media = MediaService(MediaConfig(asr=service_config()), client=http)
        with pytest.raises(ProviderError) as error:
            await media.transcribe(AUDIO, "voice.wav", "audio/wav")
    assert "secret-key" not in str(error.value)


@pytest.mark.asyncio
async def test_asr_binary_input_uses_byte_limit_before_sending() -> None:
    config = service_config(max_input_bytes=128)
    async with MediaService(MediaConfig(asr=config)) as media:
        with pytest.raises(ProviderError, match="max_input_bytes"):
            await media.transcribe(AUDIO * 16, "voice.wav", "audio/wav")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_format,mime,extension",
    [
        ("mp3", "audio/mpeg", ".mp3"),
        ("opus", "audio/ogg", ".opus"),
        ("aac", "audio/aac", ".aac"),
        ("flac", "audio/flac", ".flac"),
        ("wav", "audio/x-wav", ".wav"),
        ("pcm", "application/octet-stream", ".pcm"),
    ],
)
async def test_tts_voice_format_speed_and_instructions(
    response_format: str, mime: str, extension: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": mime}, content=AUDIO)

    config = service_config(
        voice="configured-voice",
        response_format=response_format,
        speed=1.2,
        instructions="平静一些",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        media = MediaService(MediaConfig(tts=config), client=http)
        speech = await media.synthesize("你好", voice="selected-voice")
    assert speech.data == AUDIO and speech.extension == extension
    assert speech.mime_type.startswith("audio/") and "RIFF" not in repr(speech)
    assert requests[0].url.path == "/v1/audio/speech"
    assert json.loads(requests[0].content) == {
        "model": "service-model",
        "input": "你好",
        "voice": "selected-voice",
        "response_format": response_format,
        "speed": 1.2,
        "instructions": "平静一些",
    }


@pytest.mark.asyncio
async def test_tts_omits_unset_parameters_and_requires_voice() -> None:
    requests: list[JsonObject] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=AUDIO)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        media = MediaService(MediaConfig(tts=service_config()), client=http)
        with pytest.raises(ProviderError, match="音色"):
            await media.synthesize("你好")
        assert not requests
        await media.synthesize("你好", "custom-voice")
        assert "speed" not in requests[0] and "instructions" not in requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,data,error",
    [
        ("application/json", b'{"error":"secret-key"}', "音频"),
        ("text/html", b"secret-key", "音频"),
        ("audio/wav", AUDIO, "不匹配"),
        ("audio/mpeg", b"", "空音频"),
    ],
)
async def test_tts_rejects_non_audio_mismatched_or_empty_response(
    content_type: str, data: bytes, error: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": content_type}, content=data)
        )
    ) as http:
        media = MediaService(MediaConfig(tts=service_config(voice="voice")), client=http)
        with pytest.raises(ProviderError, match=error) as exc:
            await media.synthesize("你好")
    assert "secret-key" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_media_http_error_safe_no_redirects_or_automatic_retries(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://other.test/leak"},
            text="secret-key upstream-private-body",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        media = MediaService(MediaConfig(embedding=service_config()), client=http)
        with pytest.raises(ProviderError) as exc:
            await media.embed(["文本"])
    assert len(requests) == 1
    assert exc.value.status_code == status and not exc.value.retryable
    assert "secret-key" not in str(exc.value) and "upstream-private" not in str(exc.value)


@pytest.mark.asyncio
async def test_media_input_and_response_limits_close_stream() -> None:
    requests: list[httpx.Request] = []
    stream = ChunkStream(b"a" * 200)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "audio/mpeg", "content-length": "1"}, stream=stream
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        media = MediaService(
            MediaConfig(
                tts=service_config(voice="voice", max_input_bytes=150, max_response_bytes=100)
            ),
            client=http,
        )
        with pytest.raises(ProviderError, match="max_input_bytes"):
            await media.synthesize("很长的文本" * 100)
        assert not requests
        with pytest.raises(ProviderError, match="max_response_bytes"):
            await media.synthesize("你好")
    assert stream.closed and len(requests) == 1


@pytest.mark.asyncio
async def test_media_total_deadline_closes_blocked_stream() -> None:
    stream = BlockingStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as http:
        media = MediaService(
            MediaConfig(tts=service_config(voice="voice", timeout=0.02)), client=http
        )
        with pytest.raises(ProviderError, match="总超时"):
            await asyncio.wait_for(media.synthesize("你好"), 2)
    assert stream.entered.is_set() and stream.closed


@pytest.mark.asyncio
async def test_media_cancellation_propagates_and_closes_response() -> None:
    stream = BlockingStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as http:
        media = MediaService(MediaConfig(tts=service_config(voice="voice")), client=http)
        task = asyncio.create_task(media.synthesize("你好"))
        await asyncio.wait_for(stream.entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,extra",
    [
        ("embedding", {"input": ["overridden"]}),
        ("embedding", {"dimensions": 10}),
        ("reranker", {"documents": ["overridden"]}),
        ("reranker", {"query": "overridden"}),
        ("asr", {"file": "file:///private.wav"}),
        ("tts", {"voice": "overridden"}),
        ("tts", {"headers": {"Authorization": "secret-key"}}),
    ],
)
async def test_extra_body_cannot_override_protocol_core(kind: str, extra: JsonObject) -> None:
    settings = MediaConfig(**{kind: service_config(voice="voice", extra_body=extra)})
    async with MediaService(settings) as media:
        with pytest.raises(ProviderError, match="核心字段"):
            if kind == "embedding":
                await media.embed(["文本"])
            elif kind == "reranker":
                await media.rerank("查询", ["文本"])
            elif kind == "asr":
                await media.transcribe(AUDIO, "voice.wav", "audio/wav")
            else:
                await media.synthesize("你好")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Secret": "私密"},
        {"X-Secret": "secret\r\nheader"},
        {"Content-Type": "application/json"},
        {"Host": "other.test"},
    ],
)
async def test_media_invalid_header_does_not_leak_or_send(headers: dict[str, str]) -> None:
    async with MediaService(MediaConfig(embedding=service_config(extra_headers=headers))) as media:
        with pytest.raises(ProviderError, match="请求头配置") as error:
            await media.embed(["文本"])
    assert "私密" not in str(error.value) and "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_media_validates_empty_input_and_asr_metadata_without_io() -> None:
    async with MediaService(
        MediaConfig(
            embedding=service_config(),
            reranker=service_config(),
            asr=service_config(),
            tts=service_config(voice="voice"),
        )
    ) as media:
        with pytest.raises(ProviderError):
            await media.embed([""])
        with pytest.raises(ProviderError):
            await media.embed("not-an-array")
        with pytest.raises(ProviderError):
            await media.rerank(" ", ["text"])
        with pytest.raises(ProviderError):
            await media.rerank("query", ["text"], top_n=0)
        with pytest.raises(ProviderError):
            await media.transcribe(b"", "voice.wav", "audio/wav")
        with pytest.raises(ProviderError):
            await media.transcribe(AUDIO, "private\r\nheader.wav", "audio/wav")
        with pytest.raises(ProviderError):
            await media.transcribe(AUDIO, "voice.wav", "audio/wav\r\nsecret")
        with pytest.raises(ProviderError):
            await media.synthesize(" ")


def test_service_config_hides_credentials_and_rejects_unbounded_settings() -> None:
    config = service_config(extra_headers={"X-Credential": "secret-header"})
    assert "secret-key" not in repr(config) and "secret-header" not in repr(config)
    assert config.to_dict()["extra_headers"] == {"X-Credential": "***"}
    assert config.to_dict()["api_key"] == ""
    assert config.to_dict(include_secrets=True)["api_key"] == "secret-key"
    for changes in [
        {"timeout": float("inf")},
        {"timeout": 0},
        {"dimensions": 0},
        {"max_input_bytes": 0},
        {"max_response_bytes": 0},
        {"speed": float("nan")},
        {"speed": 5},
        {"base_url": "https://secret@example.test/v1"},
        {"base_url": "https://example.test/v1?key=secret"},
    ]:
        with pytest.raises(ValueError):
            replace(config, **changes)


@pytest.mark.asyncio
async def test_unconfigured_services_and_closed_lifecycle() -> None:
    async with MediaService(MediaConfig()) as media:
        assert media.status() == {
            "vision": False,
            "vision_source": None,
            "embedding": False,
            "reranker": False,
            "asr": False,
            "tts": False,
        }
        with pytest.raises(ProviderError, match="embedding"):
            await media.embed(["文本"])
        with pytest.raises(ProviderError, match="reranker"):
            await media.rerank("query", ["text"])
        with pytest.raises(ProviderError, match="asr"):
            await media.transcribe(AUDIO, "voice.wav", "audio/wav")
        with pytest.raises(ProviderError, match="tts"):
            await media.synthesize("text", "voice")
    with pytest.raises(RuntimeError, match="关闭"):
        await media.embed(["text"])
