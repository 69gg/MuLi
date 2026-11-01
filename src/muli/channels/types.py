"""渠道配置与协议数据；内容和凭据不混入路由或错误文本。"""

from __future__ import annotations

import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from muli.reply_origin import OWNER_CHANNELS, validate_channel_owners

ChannelName = Literal["onebot", "telegram", "qq", "weixin"]
MAX_OUTGOING_TEXT_CHARS = 100_000
NAMES: tuple[ChannelName, ...] = ("onebot", "telegram", "qq", "weixin")
Submit = Callable[..., Awaitable[dict[str, Any]]]


def is_mobile_control_text(text: str) -> bool:
    """Reserve malformed commands too; this never parses or authorizes one."""
    return text.lstrip().startswith(("/任务", "/暂停", "/调整", "/审批", "/批准", "/拒绝"))


def normalize_channel_reference(value: object) -> dict[str, Any] | None:
    """Validate the bounded host marker without accepting quoted message content."""
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ValueError("渠道引用标记无效")
    if value["version"] != 1:
        raise ValueError("渠道引用标记版本无效")
    if value.get("status") == "unavailable" and set(value) == {"version", "status"}:
        return {"version": 1, "status": "unavailable"}
    message_id = value.get("message_id")
    if (
        value.get("status") != "identified"
        or set(value) != {"version", "status", "message_id"}
        or not isinstance(message_id, str)
        or not message_id
        or message_id[0] == "0"
        or not message_id.isascii()
        or not message_id.isdecimal()
    ):
        raise ValueError("渠道引用消息 ID 无效")
    return {"version": 1, "status": "identified", "message_id": message_id}


class ChannelError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        uncertain: bool = False,
        status_code: int = 400,
        retry_after: float = 0,
    ) -> None:
        super().__init__(message)
        self.retryable, self.uncertain, self.status_code = retryable, uncertain, status_code
        self.retry_after = max(0.0, min(retry_after, 3600.0))


def valid_url(value: str, *, allow_query: bool = False) -> None:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ChannelError("渠道 URL 端口无效") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
        or any(character.isspace() for character in value)
        or port == 0
    ):
        raise ChannelError("渠道 URL 必须是无账号和片段的 HTTP(S) 地址")


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    enabled: bool = False
    base_url: str = ""
    token: str = field(default="", repr=False)
    token_env: str = ""
    webhook_secret: str = field(default="", repr=False)
    webhook_secret_env: str = ""
    owner_ids: list[str] = field(default_factory=list)
    allowed_groups: list[str] = field(default_factory=list)
    receive_files: bool = False
    max_file_bytes: int = 8 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    max_attachments: int = 8
    timeout: float = 45.0
    poll_timeout: int = 30
    max_message_chars: int = 4000
    app_id: str = ""
    client_secret: str = field(default="", repr=False)
    client_secret_env: str = ""
    token_url: str = "https://api.bot.qq.com/app/getAppAccessToken"
    account_id: str = ""
    user_id: str = ""
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.receive_files) is not bool:
            raise ChannelError("渠道开关必须是布尔值")
        for value in (self.base_url, self.token_url, self.cdn_base_url):
            if value:
                valid_url(value)
        for values in (self.owner_ids, self.allowed_groups):
            if len(values) > 256 or any(
                not isinstance(value, str) or not value or len(value) > 200 for value in values
            ):
                raise ChannelError("渠道 owner_ids/allowed_groups 需要有界字符串列表")
        if any(
            type(value) is not int or value < 1
            for value in (
                self.max_file_bytes,
                self.max_response_bytes,
                self.max_attachments,
                self.max_message_chars,
            )
        ):
            raise ChannelError("渠道大小和数量限制必须是正整数")
        if type(self.poll_timeout) is not int or not 1 <= self.poll_timeout <= 50:
            raise ChannelError("渠道长轮询等待必须在 1–50 秒")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ChannelError("渠道 timeout 必须是有限正数")
        for value in (self.app_id, self.account_id, self.user_id):
            if not isinstance(value, str) or len(value) > 64:
                raise ChannelError("渠道账号标识不能超过 64 字符")

    def secret(self, name: str) -> str:
        environment = str(getattr(self, name + "_env"))
        return os.environ.get(environment, "") if environment else str(getattr(self, name))

    def public(self) -> dict[str, Any]:
        result = asdict(self)
        for name in ("token", "webhook_secret", "client_secret"):
            result[name] = "••••" if result[name] else ""
        return result


@dataclass(frozen=True, slots=True)
class ChannelsConfig:
    onebot: ChannelProfile = field(default_factory=ChannelProfile)
    telegram: ChannelProfile = field(
        default_factory=lambda: ChannelProfile(base_url="https://api.telegram.org")
    )
    qq: ChannelProfile = field(
        default_factory=lambda: ChannelProfile(base_url="https://api.bot.qq.com")
    )
    weixin: ChannelProfile = field(
        default_factory=lambda: ChannelProfile(base_url="https://ilinkai.weixin.qq.com")
    )
    max_pending_deliveries: int = 64
    max_attempts: int = 3
    retry_backoff_seconds: float = 5.0

    def __post_init__(self) -> None:
        try:
            for name in OWNER_CHANNELS:
                profile = self.profile(name)
                validate_channel_owners(name, profile.owner_ids, enabled=profile.enabled)
        except ValueError as error:
            raise ChannelError(str(error)) from None
        if any(
            type(value) is not int or value < 1
            for value in (self.max_pending_deliveries, self.max_attempts)
        ):
            raise ChannelError("渠道投递队列和重试次数必须是正整数")
        if not math.isfinite(self.retry_backoff_seconds) or self.retry_backoff_seconds < 0:
            raise ChannelError("渠道重试等待必须是有限非负数")

    def profile(self, name: str) -> ChannelProfile:
        if name not in NAMES:
            raise ChannelError("未知渠道")
        result: ChannelProfile = getattr(self, name)
        return result


@dataclass(frozen=True, slots=True)
class RemoteFile:
    name: str
    mime_type: str = "application/octet-stream"
    url: str = field(default="", repr=False)
    file_id: str = ""
    size: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    event_id: str
    sender_id: str
    target: str
    text: str
    kind: Literal["private", "group"] = "private"
    reply_to: str = ""
    files: tuple[RemoteFile, ...] = ()
    context_token: str = field(default="", repr=False)
    control_text: str = ""
    control_account_id: str = ""
    channel_reference: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PollBatch:
    messages: tuple[IncomingMessage, ...]
    cursor: str


@dataclass(frozen=True, slots=True)
class FileContent:
    name: str
    mime_type: str
    data: bytes = field(repr=False)
    media_mode: str = "file"
