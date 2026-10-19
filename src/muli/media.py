"""显式视觉、语音与检索模型服务；只调用已配置的 HTTP 接口。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from .providers import ImageInput, JsonObject, Message, ModelClient, ModelConfig, ProviderError
from .providers.http import read_response

_PROTECTED = frozenset(
    {
        "model",
        "api_key",
        "api_key_env",
        "auth",
        "headers",
        "extra_headers",
        "base_url",
        "timeout",
        "stream",
        "messages",
        "tools",
        "system",
        "previous_response_id",
        "conversation",
    }
)
_SPEECH_FORMATS = {
    "mp3": ("audio/mpeg", ".mp3"),
    "opus": ("audio/ogg", ".opus"),
    "aac": ("audio/aac", ".aac"),
    "flac": ("audio/flac", ".flac"),
    "wav": ("audio/wav", ".wav"),
    "pcm": ("audio/pcm", ".pcm"),
}


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """OpenAI/Cohere 兼容 HTTP 服务；密钥环境变量由 Settings 解析。"""

    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    timeout: float = 60.0
    max_input_bytes: int = 20_000_000
    max_response_bytes: int = 8 * 1024 * 1024
    extra_body: JsonObject = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)
    dimensions: int | None = None
    query_instruction: str = ""
    language: str = ""
    voice: str = ""
    response_format: str = "mp3"
    speed: float | None = None
    instructions: str = ""

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if not self.model.strip():
            raise ValueError("媒体服务 model 不能为空")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in self.base_url)
        ):
            raise ValueError("媒体服务 base_url 必须是无账号、查询参数或片段的 HTTP(S) 基础地址")
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("媒体服务 base_url 端口无效") from None
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("媒体服务 timeout 必须是有限正数")
        if any(
            type(limit) is not int or limit <= 0
            for limit in (self.max_input_bytes, self.max_response_bytes)
        ):
            raise ValueError("媒体输入和响应限制必须是正整数")
        if self.dimensions is not None and (
            type(self.dimensions) is not int or self.dimensions <= 0
        ):
            raise ValueError("embedding dimensions 必须是正整数或 null")
        if self.speed is not None and (
            not math.isfinite(self.speed) or not 0.25 <= self.speed <= 4
        ):
            raise ValueError("语速必须在 0.25 到 4 之间或使用 null 省略")

    def to_dict(self, *, include_secrets: bool = False) -> JsonObject:
        result: JsonObject = asdict(self)
        if not include_secrets:
            result["api_key"] = ""
            result["extra_headers"] = {name: "***" for name in self.extra_headers}
        return result


@dataclass(frozen=True, slots=True)
class MediaConfig:
    vision: ModelConfig | None = None
    embedding: ServiceConfig | None = None
    reranker: ServiceConfig | None = None
    asr: ServiceConfig | None = None
    tts: ServiceConfig | None = None


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    relevance_score: float
    document: str


@dataclass(frozen=True, slots=True)
class SpeechResult:
    data: bytes = field(repr=False)
    mime_type: str
    extension: str


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ProviderError("媒体服务参数必须是可序列化的有限 JSON 值") from None


def _json_value(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise ProviderError("媒体模型返回无效 JSON") from None


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ProviderError("媒体模型返回无效数值")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ProviderError("媒体模型返回无效数值") from None
    if not math.isfinite(number):
        raise ProviderError("媒体模型返回非有限数值")
    return number


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _form_fields(body: JsonObject) -> dict[str, str | list[str]]:
    result: dict[str, str | list[str]] = {}
    for key, value in body.items():
        if value is None:
            continue
        if isinstance(value, list):
            name = key if key.endswith("[]") else key + "[]"
            result[name] = [
                item if isinstance(item, str) else _json_bytes(item).decode("utf-8")
                for item in value
            ]
        else:
            result[key] = value if isinstance(value, str) else _json_bytes(value).decode("utf-8")
    return result


class MediaService:
    """不会读取文件、下载 URL、装载模型、自动播放或自动将上传图片注入会话。"""

    def __init__(
        self,
        config: MediaConfig,
        main_model: ModelConfig | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        max_image_bytes: int = 10_000_000,
    ) -> None:
        if type(max_image_bytes) is not int or max_image_bytes <= 0:
            raise ValueError("max_image_bytes 必须是正整数")
        self.config = config
        self.main_model = main_model
        self.max_image_bytes = max_image_bytes
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        self._closed = True

    def status(self) -> JsonObject:
        source = (
            "main"
            if self.main_model is not None and self.main_model.vision
            else "vision"
            if self.config.vision is not None
            else None
        )
        return {
            "vision": source is not None,
            "vision_source": source,
            **{
                name: getattr(self.config, name) is not None
                for name in ("embedding", "reranker", "asr", "tts")
            },
        }

    def _service(self, name: str) -> ServiceConfig:
        if self._closed:
            raise RuntimeError("媒体服务已经关闭")
        config = getattr(self.config, name)
        if not isinstance(config, ServiceConfig):
            raise ProviderError(f"尚未配置 {name} 模型服务")
        return config

    @staticmethod
    def _body(config: ServiceConfig, protected: set[str]) -> JsonObject:
        if _PROTECTED.union(protected).intersection(config.extra_body):
            raise ProviderError("媒体 extra_body 不允许覆盖协议核心字段")
        return {**deepcopy(config.extra_body), "model": config.model}

    @staticmethod
    def _headers(config: ServiceConfig, *, audio: bool) -> httpx.Headers:
        try:
            if any(
                name.lower() in {"content-type", "content-length", "transfer-encoding", "host"}
                or any(ord(char) < 32 or ord(char) == 127 for char in name + value)
                for name, value in config.extra_headers.items()
            ):
                raise ValueError
            headers = httpx.Headers({"Accept": "audio/*" if audio else "application/json"})
            if config.api_key:
                headers["Authorization"] = f"Bearer {config.api_key}"
            headers.update(config.extra_headers)
            return headers
        except (ValueError, UnicodeError):
            raise ProviderError("媒体请求头配置无效") from None

    async def _send(
        self,
        config: ServiceConfig,
        endpoint: str,
        body: JsonObject,
        *,
        audio_output: str | None = None,
        upload: tuple[str, bytes, str] | None = None,
    ) -> bytes:
        size = len(_json_bytes(body)) + (len(upload[1]) if upload else 0)
        if size > config.max_input_bytes:
            raise ProviderError("媒体请求超过 max_input_bytes 限制")
        headers = self._headers(config, audio=audio_output is not None)
        options: JsonObject = (
            {"files": {"file": upload}, "data": _form_fields(body)}
            if upload is not None
            else {"json": body}
        )
        try:
            async with asyncio.timeout(config.timeout):
                async with self._client.stream(
                    "POST",
                    config.base_url.rstrip("/") + endpoint,
                    headers=headers,
                    timeout=config.timeout,
                    follow_redirects=False,
                    **options,
                ) as response:
                    if response.status_code >= 300:
                        raise ProviderError(
                            f"媒体模型请求失败（HTTP {response.status_code}）",
                            status_code=response.status_code,
                        )
                    if audio_output:
                        content_type = (
                            response.headers.get("content-type", "").lower().split(";")[0]
                        )
                        if content_type and not (
                            content_type.startswith("audio/")
                            or content_type in {"application/octet-stream", "binary/octet-stream"}
                        ):
                            raise ProviderError("TTS 接口没有返回音频文件")
                        aliases = {
                            "mp3": {"audio/mpeg", "audio/mp3"},
                            "opus": {"audio/ogg", "audio/opus"},
                            "aac": {"audio/aac"},
                            "flac": {"audio/flac", "audio/x-flac"},
                            "wav": {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"},
                            "pcm": {"audio/pcm", "audio/l16"},
                        }
                        if (
                            content_type.startswith("audio/")
                            and content_type not in aliases[audio_output]
                        ):
                            raise ProviderError("TTS 返回的音频类型与 response_format 不匹配")
                    return await read_response(response, config.max_response_bytes)
        except TimeoutError:
            raise ProviderError("媒体模型请求超过总超时限制") from None
        except httpx.HTTPError:
            raise ProviderError("媒体模型连接或传输失败") from None

    async def describe_image(self, image: bytes, mime_type: str, prompt: str) -> str:
        if self._closed:
            raise RuntimeError("媒体服务已经关闭")
        if not prompt.strip():
            raise ProviderError("看图问题不能为空")
        if _image_type(image) != mime_type:
            raise ProviderError("图片格式不支持或与声明的 MIME 类型不匹配")
        model = (
            self.main_model
            if self.main_model is not None and self.main_model.vision
            else self.config.vision
        )
        if model is None:
            raise ProviderError("主模型没有 vision 能力，且尚未配置外挂视觉模型")
        try:
            part = ImageInput.from_bytes(image, mime_type, max_bytes=self.max_image_bytes)
        except ValueError:
            raise ProviderError("图片为空或超过 max_image_bytes 限制") from None
        try:
            async with asyncio.timeout(model.timeout):
                async with ModelClient(replace(model, vision=True), self._client) as vision:
                    turn = await vision.complete([Message("user", prompt, images=[part])])
        except TimeoutError:
            raise ProviderError("视觉模型请求超过总超时限制") from None
        if turn.tool_calls:
            raise ProviderError("视觉模型返回了未声明的工具调用")
        if not turn.content:
            raise ProviderError("视觉模型没有返回可读结果")
        return turn.content

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        config = self._service("embedding")
        if isinstance(texts, (str, bytes)):
            raise ProviderError("embedding 输入必须是文本数组")
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ProviderError("embedding 输入必须是非空文本")
        body = self._body(config, {"input", "dimensions", "encoding_format"})
        body.update(input=list(texts), encoding_format="float")
        if config.dimensions is not None:
            body["dimensions"] = config.dimensions
        value = _json_value(await self._send(config, "/embeddings", body))
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise ProviderError("embedding 返回数量与输入不匹配")
        vectors: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise ProviderError("embedding 返回项无效")
            index, vector = item.get("index"), item.get("embedding")
            if (
                type(index) is not int
                or not 0 <= index < len(texts)
                or index in vectors
                or not isinstance(vector, list)
                or not vector
            ):
                raise ProviderError("embedding 返回索引或向量无效")
            if any(type(number) not in {int, float} for number in vector):
                raise ProviderError("embedding 返回的向量必须是浮点数组")
            vectors[index] = [_number(number) for number in vector]
        dimensions = config.dimensions if config.dimensions is not None else len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors.values()):
            raise ProviderError("embedding 返回维度不匹配")
        return [vectors[index] for index in range(len(texts))]

    async def rerank(
        self, query: str, documents: Sequence[str], top_n: int | None = None
    ) -> list[RerankResult]:
        config = self._service("reranker")
        if isinstance(documents, (str, bytes)):
            raise ProviderError("reranker documents 必须是文本数组")
        if not query.strip():
            raise ProviderError("reranker 查询不能为空")
        if top_n is not None and (type(top_n) is not int or top_n <= 0):
            raise ProviderError("reranker top_n 必须是正整数或 null")
        if not documents:
            return []
        if any(not isinstance(document, str) for document in documents):
            raise ProviderError("reranker documents 必须是文本数组")
        body = self._body(config, {"query", "documents", "top_n", "return_documents"})
        body.update(
            query=config.query_instruction + query,
            documents=list(documents),
            return_documents=False,
        )
        if top_n is not None:
            body["top_n"] = min(top_n, len(documents))
        value = _json_value(await self._send(config, "/rerank", body))
        if isinstance(value, dict):
            value = value.get("results", value.get("data"))
        if not isinstance(value, list) or not value:
            raise ProviderError("reranker 没有返回有效排序结果")
        results: list[RerankResult] = []
        indices: set[int] = set()
        for position, item in enumerate(value):
            if not isinstance(item, dict):
                raise ProviderError("reranker 返回项无效")
            index = item.get("index", position)
            if type(index) is not int or not 0 <= index < len(documents) or index in indices:
                raise ProviderError("reranker 返回索引无效或重复")
            score = item.get("relevance_score", item.get("score", item.get("similarity")))
            results.append(RerankResult(index, _number(score), documents[index]))
            indices.add(index)
        return results[:top_n] if top_n is not None else results

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        mime_type: str,
        language: str | None = None,
        prompt: str = "",
    ) -> str:
        config = self._service("asr")
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not audio or not name or len(name) > 255 or any(ord(char) < 32 for char in name):
            raise ProviderError("转写需要非空音频及有效文件名")
        if not mime_type.startswith(("audio/", "video/")) or any(
            ord(char) < 32 or ord(char) == 127 for char in mime_type
        ):
            raise ProviderError("转写文件需要有效的音视频 MIME 类型")
        body = self._body(config, {"file"})
        body.setdefault("response_format", "json")
        response_format = body["response_format"]
        if not isinstance(response_format, str) or response_format not in {
            "json",
            "verbose_json",
            "diarized_json",
            "text",
            "srt",
            "vtt",
        }:
            raise ProviderError("ASR response_format 不支持")
        selected_language = config.language if language is None else language
        if selected_language:
            body["language"] = selected_language
        if prompt:
            body["prompt"] = prompt
        data = await self._send(
            config, "/audio/transcriptions", body, upload=(name, audio, mime_type)
        )
        if response_format in {"text", "srt", "vtt"}:
            try:
                return data.decode("utf-8")
            except UnicodeError:
                raise ProviderError("ASR 返回无效 UTF-8 文本") from None
        value = _json_value(data)
        text = value.get("text") if isinstance(value, dict) else None
        if not isinstance(text, str):
            raise ProviderError("ASR 返回结果缺少 text")
        return text

    async def synthesize(self, text: str, voice: str | None = None) -> SpeechResult:
        config = self._service("tts")
        selected_voice = config.voice if voice is None else voice
        if not text.strip() or not selected_voice.strip():
            raise ProviderError("TTS 需要非空文本和显式配置的音色")
        if config.response_format not in _SPEECH_FORMATS:
            raise ProviderError("TTS response_format 不支持")
        body = self._body(config, {"input", "voice", "response_format", "stream_format"})
        body.update(input=text, voice=selected_voice, response_format=config.response_format)
        if config.speed is not None:
            body["speed"] = config.speed
        if config.instructions:
            body["instructions"] = config.instructions
        data = await self._send(config, "/audio/speech", body, audio_output=config.response_format)
        if not data:
            raise ProviderError("TTS 返回空音频")
        mime_type, extension = _SPEECH_FORMATS[config.response_format]
        return SpeechResult(data, mime_type, extension)


__all__ = [
    "MediaConfig",
    "MediaService",
    "RerankResult",
    "ServiceConfig",
    "SpeechResult",
]
