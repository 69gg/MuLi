"""不依赖厂商 SDK 的模型协议数据结构。"""

from __future__ import annotations

import base64
import binascii
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

JsonObject = dict[str, Any]
ProtocolName = Literal["openai_chat", "openai_responses", "anthropic"]
MessageRole = Literal["system", "developer", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """base_url 是带 API 版本前缀的基础地址，例如 https://host/v1。"""

    protocol: ProtocolName
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    max_output_tokens: int = 4096
    timeout: float = 60.0
    max_retries: int = 2
    retry_backoff: float = 0.5
    extra_body: JsonObject = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)
    reasoning: JsonObject = field(default_factory=dict)
    stream: bool = True
    max_response_bytes: int = 8 * 1024 * 1024
    thinking: JsonObject = field(default_factory=dict)
    reasoning_effort: str | None = None
    vision: bool = False

    def __post_init__(self) -> None:
        if self.protocol not in {"openai_chat", "openai_responses", "anthropic"}:
            raise ValueError("不支持的模型协议")
        if not self.model.strip():
            raise ValueError("model 不能为空")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url 必须是 HTTP(S) 基础地址")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in self.base_url)
        ):
            raise ValueError("base_url 不允许包含账号、查询参数或片段")
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("base_url 端口无效") from None
        if self.max_output_tokens <= 0 or self.max_response_bytes <= 0:
            raise ValueError("输出限制必须大于零")
        if (
            not math.isfinite(self.timeout)
            or not math.isfinite(self.retry_backoff)
            or self.timeout <= 0
            or self.max_retries < 0
            or self.retry_backoff < 0
        ):
            raise ValueError("timeout 必须大于零，重试次数和间隔不能为负")
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort 为空时请使用 null 省略参数")
        if not isinstance(self.thinking, dict) or not isinstance(self.reasoning, dict):
            raise ValueError("thinking 和 reasoning 必须是对象")

    def to_dict(self, *, include_secrets: bool = False) -> JsonObject:
        """默认可供界面展示；持久化密钥需要显式 include_secrets=True。"""
        result: JsonObject = asdict(self)
        if not include_secrets:
            result["api_key"] = ""
            result["extra_headers"] = {key: "***" for key in self.extra_headers}
        return result


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ImageInput:
    """显式图片输入；仅保存可序列化的内联数据，不下载 URL。"""

    data: str = field(repr=False)
    mime_type: str
    detail: Literal["auto", "low", "high", "original"] = "auto"

    def __post_init__(self) -> None:
        if self.mime_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise ValueError("图片仅支持 PNG、JPEG、GIF 或 WebP")
        if self.detail not in {"auto", "low", "high", "original"}:
            raise ValueError("图片 detail 无效")
        try:
            decoded = base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("图片必须是有效的非空 base64 数据") from exc
        if not decoded:
            raise ValueError("图片不能为空")

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        mime_type: str,
        *,
        max_bytes: int = 10_000_000,
        detail: Literal["auto", "low", "high", "original"] = "auto",
    ) -> ImageInput:
        if max_bytes <= 0 or not data or len(data) > max_bytes:
            raise ValueError("图片为空或超过 max_bytes 限制")
        return cls(base64.b64encode(data).decode("ascii"), mime_type, detail)

    @property
    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{self.data}"


@dataclass(slots=True)
class Message:
    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    reasoning: str = ""
    provider_data: JsonObject = field(default_factory=dict)
    images: list[ImageInput] = field(default_factory=list)
    # Host metadata only. Provider codecs deliberately serialize explicit fields.
    preserve_content: bool = False
    source_message_id: str | None = None


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    raw: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class ModelTurn:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    response_id: str | None = None
    provider_data: JsonObject = field(default_factory=dict)

    def to_message(self) -> Message:
        """保留思维签名和原生输出，调用者不要重新拼接 assistant 消息。"""
        return Message(
            role="assistant",
            content=self.content,
            reasoning=self.reasoning,
            tool_calls=list(self.tool_calls),
            provider_data=deepcopy(self.provider_data),
        )


@dataclass(frozen=True, slots=True)
class StreamEvent:
    kind: Literal["text_delta", "reasoning_delta", "tool_call_delta", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    turn: ModelTurn | None = None
    index: int | None = None


class ProviderError(RuntimeError):
    """错误文本不包含请求头、密钥、请求体或上游错误正文。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        partial: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.partial = partial
        self.retry_after = retry_after
