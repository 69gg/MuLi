"""Validated configuration with atomic writes and secret-free views."""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from muli.reply_origin import authorize_route, channel_account
from muli.system_events import SystemEventsSettings


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


SECRET_PLACEHOLDER = "••••"


def _mcp_url(value: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not any(character.isspace() for character in value)
        )
    except ValueError as exc:
        raise ValueError("MCP url 格式无效") from exc
    if not valid:
        raise ValueError("MCP url 必须是无内嵌账号、片段或空白的 HTTP(S) 地址")
    return parsed


def _validate_mcp(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) - {"mcpServers"}:
        raise ValueError("MCP 根配置只能包含 mcpServers")
    servers = value.get("mcpServers", {})
    if not isinstance(servers, dict) or len(servers) > 32:
        raise ValueError("mcpServers 必须是最多 32 项的对象")
    allowed = {
        "enabled",
        "disabled",
        "timeout",
        "transport",
        "command",
        "args",
        "env",
        "cwd",
        "url",
        "headers",
    }
    for name, server in servers.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("MCP 服务器名称无效")
        if not isinstance(server, dict) or set(server) - allowed:
            raise ValueError("MCP 服务器配置字段无效")
        for field in ("enabled", "disabled"):
            if field in server and type(server[field]) is not bool:
                raise ValueError(f"MCP {field} 必须是布尔值")
        if "timeout" in server and (
            type(server["timeout"]) not in {int, float}
            or not 0 < server["timeout"] <= 86400
            or not math.isfinite(server["timeout"])
        ):
            raise ValueError("MCP timeout 必须是一天以内的有限正数")
        if "transport" in server and (
            not isinstance(server["transport"], str)
            or server["transport"] not in {"stdio", "http", "streamable-http", "sse"}
        ):
            raise ValueError("MCP transport 无效")
        for field in ("command", "cwd", "url"):
            if field in server and (not isinstance(server[field], str) or "\x00" in server[field]):
                raise ValueError(f"MCP {field} 必须是字符串")
        if "args" in server and (
            not isinstance(server["args"], list)
            or len(server["args"]) > 128
            or any(not isinstance(arg, str) or "\x00" in arg for arg in server["args"])
        ):
            raise ValueError("MCP args 必须是最多 128 项的字符串数组")
        for field in ("env", "headers"):
            values = server.get(field, {})
            if not isinstance(values, dict) or any(
                not isinstance(key, str) or not isinstance(secret, str)
                for key, secret in values.items()
            ):
                raise ValueError(f"MCP {field} 必须是字符串映射")
            if SECRET_PLACEHOLDER in values.values():
                raise ValueError("MCP 密钥占位符没有对应的原值，请填写真实值或删除字段")
        if "url" in server:
            parsed = _mcp_url(server["url"])
            if unquote(parsed.query) == SECRET_PLACEHOLDER:
                raise ValueError("MCP URL 查询占位符没有对应的原值")
    return value


def _public_mcp(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    for server in result.get("mcpServers", {}).values():
        for field in ("env", "headers"):
            if field in server:
                server[field] = {key: SECRET_PLACEHOLDER for key in server[field]}
        if "url" in server:
            parsed = _mcp_url(server["url"])
            if parsed.query:
                server["url"] = urlunsplit(parsed._replace(query=SECRET_PLACEHOLDER))
    return result


def _replace_mcp(current: dict[str, Any], replacement: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(replacement)
    previous = current.get("mcpServers", {})
    servers = result.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 必须是对象")
    for name, server in servers.items():
        if not isinstance(server, dict):
            raise ValueError("MCP 服务器配置必须是对象")
        old = previous.get(name, {})
        for field in ("env", "headers"):
            values = server.get(field, {})
            if not isinstance(values, dict):
                raise ValueError(f"MCP {field} 必须是字符串映射")
            for key, secret in values.items():
                if secret == SECRET_PLACEHOLDER:
                    if key not in old.get(field, {}):
                        raise ValueError("新增 MCP 密钥不能使用占位符")
                    values[key] = old[field][key]
        if isinstance(server.get("url"), str):
            parsed = _mcp_url(server["url"])
            if unquote(parsed.query) == SECRET_PLACEHOLDER:
                original = _mcp_url(old.get("url", ""))
                if not original.query or parsed._replace(query="") != original._replace(query=""):
                    raise ValueError("MCP URL 改变后必须重新填写或删除查询参数")
                server["url"] = old["url"]
    return _validate_mcp(result)


class ModelProfile(Section):
    protocol: Literal["openai_chat", "openai_responses", "anthropic"] = "openai_chat"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    max_output_tokens: int = Field(default=4096, ge=128, le=131072)
    timeout: float = Field(default=60, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    stream: bool = True
    max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    retry_backoff: float = Field(default=0.5, ge=0, le=30)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    reasoning: dict[str, Any] = Field(default_factory=dict)
    thinking: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort: str | None = None
    vision: bool = False
    extra_body: dict[str, Any] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)

    def key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else self.api_key

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url)


class Models(Section):
    main: ModelProfile = Field(default_factory=ModelProfile)
    reviewer: ModelProfile = Field(default_factory=ModelProfile)
    worker: ModelProfile = Field(default_factory=ModelProfile)
    vision: ModelProfile = Field(default_factory=ModelProfile)
    compactor: ModelProfile = Field(default_factory=ModelProfile)
    memory_extractor: ModelProfile = Field(default_factory=ModelProfile)


class HTTPServiceProfile(Section):
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    timeout: float = Field(default=60, gt=0, le=600)
    max_input_bytes: int = Field(default=20_000_000, ge=1024)
    max_response_bytes: int = Field(default=8_388_608, ge=1024)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    dimensions: int | None = Field(default=None, ge=1)
    query_instruction: str = ""
    language: str = ""
    voice: str = ""
    response_format: str = "mp3"
    speed: float | None = Field(default=None, ge=0.25, le=4)
    instructions: str = ""

    def key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else self.api_key

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url)


class MediaSettings(Section):
    embedding: HTTPServiceProfile = Field(default_factory=HTTPServiceProfile)
    reranker: HTTPServiceProfile = Field(default_factory=HTTPServiceProfile)
    asr: HTTPServiceProfile = Field(default_factory=HTTPServiceProfile)
    tts: HTTPServiceProfile = Field(default_factory=HTTPServiceProfile)


class Persona(Section):
    user_name: str = Field(default="你", min_length=1, max_length=120)
    name: str = Field(default="沐璃", min_length=1, max_length=120)
    introduction: str = Field(default="", max_length=4000)
    prompt: str = Field(default="", max_length=64000)
    append: str = Field(default="", max_length=32000)


class CompactionSettings(Section):
    enabled: bool = True
    mode: Literal["auto", "extractive"] = "auto"
    trigger_ratio: float = Field(default=0.75, gt=0.2, lt=1)
    target_ratio: float = Field(default=0.4, gt=0.1, lt=0.8)
    max_summary_tokens: int = Field(default=1600, ge=128)

    @model_validator(mode="after")
    def valid_ratios(self) -> CompactionSettings:
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("压缩目标比例必须小于触发比例")
        return self


class UploadSettings(Section):
    max_file_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_concurrent: int = Field(default=2, ge=1)


class MemorySettings(Section):
    enabled: bool = True
    auto_extract: bool = True
    max_pending_jobs: int = Field(default=128, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: float = Field(default=5, ge=0)
    job_timeout: float = Field(default=60, gt=0)
    max_source_chars: int = Field(default=64000, ge=1000)
    max_facts_per_source: int = Field(default=16, ge=1)
    graph_hops: int = Field(default=2, ge=0, le=8)
    max_graph_edges: int = Field(default=256, ge=1)
    max_candidates: int = Field(default=256, ge=1)
    max_vector_scan: int = Field(default=512, ge=1)
    max_community_members: int = Field(default=32, ge=2)
    context_max_chars: int = Field(default=6000, ge=0)
    context_limit: int = Field(default=6, ge=1)
    query_timeout: float = Field(default=5, gt=0)
    cache_entries: int = Field(default=64, ge=1)


class PluginSettings(Section):
    enabled: bool = True
    disabled: list[str] = Field(default_factory=list)
    max_source_bytes: int = Field(default=256 * 1024, ge=1024)
    hook_timeout: float = Field(default=10, gt=0, le=120)


class UpdateSettings(Section):
    check_on_start: bool = True
    metadata_url: str = "https://pypi.org/pypi/muli-agent/json"
    repository: str = "https://github.com/69gg/MuLi.git"
    timeout: float = Field(default=8, gt=0, le=30)
    max_metadata_bytes: int = Field(default=256 * 1024, ge=1024)


class Limits(Section):
    max_parallel_agents: int = Field(default=2, ge=1)
    max_pending_runs: int = Field(default=8, ge=1)
    max_tool_steps: int = Field(default=16, ge=1, le=100)
    max_run_seconds: int = Field(default=600, ge=10, le=7200)
    max_file_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    max_output_chars: int = Field(default=24000, ge=1000, le=100000)
    max_context_chars: int = Field(default=96000, ge=4000, le=1000000)
    max_workflow_steps: int = Field(default=12, ge=1)
    approval_timeout: int = Field(default=600, ge=10, le=86400)
    max_search_entries: int = Field(default=2000, ge=1, le=20000)
    max_search_results: int = Field(default=100, ge=1, le=1000)
    max_search_depth: int = Field(default=16, ge=1, le=64)
    max_search_seconds: float = Field(default=10, gt=0, le=60)


class Search(Section):
    provider: Literal["searxng", "brave", "tavily"] = "searxng"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    max_results: int = Field(default=5, ge=1, le=20)


class CaptureSettings(Section):
    enabled: bool = False
    backend: str = "auto"
    device: str = ""
    max_seconds: int = Field(default=30, ge=1, le=300)
    portal_timeout_seconds: int = Field(default=60, ge=1, le=300)
    fps: int = Field(default=8, ge=1, le=30)
    max_file_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    max_total_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)


class WebVoiceSettings(Section):
    model_config = ConfigDict(strict=True)

    enabled: bool = False
    max_seconds: int = Field(default=60, ge=1, le=300)
    max_file_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    max_concurrent: int = Field(default=1, ge=1, le=4)
    timeout: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)
    max_text_chars: int = Field(default=64000, ge=1, le=64000)


class VoiceSettings(Section):
    enabled: bool = False
    wake_word: str = "沐璃"
    device: Literal["auto", "cpu", "rocm", "cuda"] = "auto"
    input_device: str = ""
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    chunk_seconds: float = Field(default=2, gt=0, le=10)
    max_utterance_seconds: int = Field(default=15, ge=1, le=120)
    cooldown_seconds: float = Field(default=3, ge=0)
    max_file_bytes: int = Field(default=2_000_000, ge=1024)
    max_total_bytes: int = Field(default=20_000_000, ge=1024)
    wake_command: list[str] = Field(default_factory=list)
    wake_threshold: float = Field(default=0.6, ge=0, le=1)
    speaker_command: list[str] = Field(default_factory=list)
    speaker_threshold: float = Field(default=0.75, ge=0, le=1)
    tts_enabled: bool = False


class ChannelProfile(Section):
    enabled: bool = False
    base_url: str = ""
    token: str = ""
    token_env: str = ""
    webhook_secret: str = ""
    webhook_secret_env: str = ""
    owner_ids: list[str] = Field(default_factory=list)
    allowed_groups: list[str] = Field(default_factory=list)
    receive_files: bool = False
    max_file_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_attachments: int = Field(default=8, ge=1)
    timeout: float = Field(default=45, gt=0, le=180)
    poll_timeout: int = Field(default=30, ge=1, le=50)
    max_message_chars: int = Field(default=4000, ge=1, le=64000)
    app_id: str = ""
    client_secret: str = ""
    client_secret_env: str = ""
    token_url: str = ""
    account_id: str = ""
    user_id: str = ""
    cdn_base_url: str = ""

    def secret(self, name: str) -> str:
        if name not in {"token", "webhook_secret", "client_secret"}:
            raise ValueError("未知渠道密钥字段")
        env = str(getattr(self, f"{name}_env"))
        return os.environ.get(env, "") if env else str(getattr(self, name))


RouteText = Annotated[str, Field(min_length=1, max_length=200, strict=True)]


class ProactiveRoute(Section):
    name: Literal["onebot", "telegram", "qq", "weixin"]
    account_id: RouteText
    kind: Literal["private", "group"]
    target: RouteText
    sender_id: RouteText

    @field_validator("account_id", "target", "sender_id")
    @classmethod
    def finite_identifier(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("渠道目标不能包含控制字符")
        return value


class WebDestination(Section):
    kind: Literal["web"] = "web"


class ChannelDestination(Section):
    kind: Literal["channel"]
    route: ProactiveRoute


class NotificationPreference(Section):
    memory_id: RouteText
    version: int = Field(ge=1, strict=True)
    destination: WebDestination | ChannelDestination = Field(discriminator="kind")

    @field_validator("memory_id")
    @classmethod
    def finite_identifier(cls, value: str) -> str:
        return ProactiveRoute.finite_identifier(value)


class ProactiveSettings(Section):
    external_enabled: bool = Field(default=False, strict=True)
    default_destination: WebDestination | ChannelDestination = Field(
        default_factory=WebDestination, discriminator="kind"
    )
    memory_preference: NotificationPreference | None = None


class ChannelSettings(Section):
    onebot: ChannelProfile = Field(default_factory=ChannelProfile)
    telegram: ChannelProfile = Field(
        default_factory=lambda: ChannelProfile(base_url="https://api.telegram.org")
    )
    qq: ChannelProfile = Field(
        default_factory=lambda: ChannelProfile(
            base_url="https://api.bot.qq.com",
            token_url="https://api.bot.qq.com/app/getAppAccessToken",
        )
    )
    weixin: ChannelProfile = Field(
        default_factory=lambda: ChannelProfile(
            base_url="https://ilinkai.weixin.qq.com",
            cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
        )
    )
    max_pending_deliveries: int = Field(default=64, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: float = Field(default=5, ge=0)
    proactive: ProactiveSettings = Field(default_factory=ProactiveSettings)

    def validate_proactive_destination(
        self, destination: WebDestination | ChannelDestination | None = None
    ) -> None:
        destination = destination or self.proactive.default_destination
        if isinstance(destination, ChannelDestination):
            route = destination.route
            profile: ChannelProfile = getattr(self, route.name)
            authorize_route(
                route.name,
                route.model_dump(),
                account_id=channel_account(route.name, profile),
                owner_ids=profile.owner_ids,
                allowed_groups=profile.allowed_groups,
                outbound=True,
            )


class Computer(Section):
    enabled: bool = False
    backend: Literal["auto", "atspi", "ax"] = "auto"
    python_path: str = "/usr/bin/python3"
    max_nodes: int = Field(default=300, ge=10, le=1000)
    operation_timeout_seconds: float = Field(
        default=12, gt=0, le=120, strict=True, allow_inf_nan=False
    )
    snapshot_ttl_seconds: float = Field(default=60, gt=0, le=300, strict=True, allow_inf_nan=False)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)


class Chiliarch(Section):
    enabled: bool = False
    base_url: str = ""
    credential: str = ""
    agent_id: str = ""
    name: str = "MuLi"
    machine: str = ""
    max_queued_tasks: int = Field(default=100, ge=1, le=1000)


class DesktopNotifications(Section):
    enabled: bool = Field(default=False, strict=True)
    timeout: float = Field(default=5, gt=0, le=30)
    max_queue: int = Field(default=32, ge=1, le=128)
    expire_ms: int = Field(default=8000, ge=1000, le=60000)


AttentionEvent = Literal["reminder", "management", "update"]
ATTENTION_EVENTS: tuple[AttentionEvent, ...] = ("reminder", "management", "update")


class AttentionSettings(Section):
    enabled: bool = Field(default=True, strict=True)
    events: list[AttentionEvent] = Field(default_factory=lambda: list(ATTENTION_EVENTS))
    batch_size: int = Field(default=8, ge=1, le=128, strict=True)
    max_chars: int = Field(default=4000, ge=512, le=32000, strict=True)
    coalesce_seconds: float = Field(default=0.25, ge=0, le=60, allow_inf_nan=False)

    @field_validator("events")
    @classmethod
    def unique_events(cls, value: list[AttentionEvent]) -> list[AttentionEvent]:
        if len(value) != len(set(value)):
            raise ValueError("注意事项事件不能重复")
        return value


class Server(Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    password_hash: str = ""
    allowed_hosts: list[str] = Field(default_factory=list)
    secure_cookie: bool = False
    session_ttl_seconds: int = Field(default=86400, ge=60, le=31_536_000)

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if value != "localhost":
            ipaddress.ip_address(value)
        return value

    @model_validator(mode="after")
    def require_authentication(self) -> Server:
        if self.host != "localhost" and not ipaddress.ip_address(self.host).is_loopback:
            if not self.password_hash:
                raise ValueError("非回环监听地址必须配置访问密码")
        return self


class SkillsSettings(Section):
    enabled: bool = Field(default=True, strict=True)
    directory: str = ""
    max_entries: int = Field(default=256, ge=1, le=4096, strict=True)
    max_skills: int = Field(default=64, ge=1, le=1024, strict=True)
    max_source_bytes: int = Field(default=262144, ge=1, le=2_097_152, strict=True)
    max_total_source_bytes: int = Field(default=2_097_152, ge=1, le=8_388_608, strict=True)
    max_frontmatter_bytes: int = Field(default=16384, ge=1, le=262144, strict=True)
    max_catalog_chars: int = Field(default=12000, ge=1, le=64000, strict=True)
    max_active_skills: int = Field(default=4, ge=1, le=32, strict=True)
    max_active_chars: int = Field(default=24000, ge=1, le=256000, strict=True)
    max_yaml_depth: int = Field(default=8, ge=1, le=32, strict=True)
    max_yaml_nodes: int = Field(default=256, ge=1, le=4096, strict=True)


class ExtensionSettings(Section):
    mcp_directory: str = ""
    watch_enabled: bool = Field(default=True, strict=True)
    scan_interval_seconds: float = Field(default=2, ge=0.25, le=300, allow_inf_nan=False)
    debounce_seconds: float = Field(default=0.5, ge=0, le=60, allow_inf_nan=False)
    max_entries: int = Field(default=256, ge=1, le=4096, strict=True)
    max_mcp_fragments: int = Field(default=32, ge=1, le=256, strict=True)
    max_mcp_fragment_bytes: int = Field(default=65536, ge=1, le=1_048_576, strict=True)
    max_mcp_source_bytes: int = Field(default=262144, ge=1, le=4_194_304, strict=True)
    max_mcp_pages: int = Field(default=8, ge=1, le=64, strict=True)
    max_mcp_tools: int = Field(default=100, ge=1, le=1000, strict=True)
    max_mcp_catalog_bytes: int = Field(default=262144, ge=1, le=4_194_304, strict=True)
    candidate_timeout_seconds: float = Field(default=30, gt=0, le=180, allow_inf_nan=False)


class Settings(Section):
    models: Models = Field(default_factory=Models)
    media: MediaSettings = Field(default_factory=MediaSettings)
    persona: Persona = Field(default_factory=Persona)
    compaction: CompactionSettings = Field(default_factory=CompactionSettings)
    uploads: UploadSettings = Field(default_factory=UploadSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    updates: UpdateSettings = Field(default_factory=UpdateSettings)
    skills: SkillsSettings = Field(default_factory=SkillsSettings)
    extensions: ExtensionSettings = Field(default_factory=ExtensionSettings)
    onboarding_pending: bool = False
    default_mode: Literal["ask", "auto", "yolo"] = "auto"
    workspace_roots: list[str] = Field(default_factory=lambda: [str(Path.home())])
    search: Search = Field(default_factory=Search)
    computer: Computer = Field(default_factory=Computer)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    web_voice: WebVoiceSettings = Field(default_factory=WebVoiceSettings)
    channels: ChannelSettings = Field(default_factory=ChannelSettings)
    chiliarch: Chiliarch = Field(default_factory=Chiliarch)
    notifications: DesktopNotifications = Field(default_factory=DesktopNotifications)
    attention: AttentionSettings = Field(default_factory=AttentionSettings)
    system_events: SystemEventsSettings = Field(default_factory=SystemEventsSettings)
    limits: Limits = Field(default_factory=Limits)
    server: Server = Field(default_factory=Server)
    disabled_tools: list[str] = Field(default_factory=list)
    mcp: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mcp")
    @classmethod
    def valid_mcp(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_mcp(value)

    @field_validator("workspace_roots")
    @classmethod
    def valid_roots(cls, roots: list[str]) -> list[str]:
        if not roots:
            raise ValueError("至少配置一个文件访问根目录")
        return [str(Path(root).expanduser().resolve()) for root in roots]


def default_data_dir() -> Path:
    return Path(os.environ.get("MULI_DATA_DIR", default_home() / "data")).expanduser()


def default_home() -> Path:
    return Path(os.environ.get("MULI_HOME", Path.home() / "MuLi")).expanduser()


def default_config_path() -> Path:
    return Path(os.environ.get("MULI_CONFIG", default_home() / "config.json")).expanduser()


class ConfigFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.settings = (
            Settings.model_validate_json(path.read_text(encoding="utf-8"))
            if path.exists()
            else Settings()
        )

    @classmethod
    def from_bytes(cls, path: Path, data: bytes) -> ConfigFile:
        """Use host-verified bytes without rereading a path or defaulting a missing file."""
        result = cls.__new__(cls)
        result.path = path
        result.settings = Settings.model_validate_json(data)
        return result

    def public(self) -> dict[str, Any]:
        data = self.settings.model_dump()
        for section in [*data["models"].values(), *data["media"].values(), data["search"]]:
            section["api_key_set"] = bool(
                section.pop("api_key", "") or os.environ.get(section.get("api_key_env", ""), "")
            )
            if "extra_headers" in section:
                section["extra_headers"] = {key: "••••" for key in section["extra_headers"]}
        data["chiliarch"]["credential_set"] = bool(data["chiliarch"].pop("credential"))
        data["server"]["password_set"] = bool(data["server"].pop("password_hash"))
        data["mcp"] = _public_mcp(data["mcp"])
        for name in ("onebot", "telegram", "qq", "weixin"):
            profile = data["channels"][name]
            for secret in ("token", "webhook_secret", "client_secret"):
                profile[f"{secret}_set"] = bool(
                    profile.pop(secret, "") or os.environ.get(profile.get(f"{secret}_env", ""), "")
                )
        return data

    def validate_changes(self, changes: dict[str, Any]) -> Settings:
        changes = copy.deepcopy(changes)
        channels = changes.get("channels")
        proactive = channels.get("proactive") if isinstance(channels, dict) else None
        destination = proactive.get("default_destination") if isinstance(proactive, dict) else None
        if (
            isinstance(proactive, dict)
            and isinstance(destination, dict)
            and destination.get("kind") == "channel"
        ):
            # Choosing a complete external destination enables it in one action.
            # An explicit pause in that same patch remains intentional.
            proactive.setdefault("external_enabled", True)
        preference = proactive.get("memory_preference") if isinstance(proactive, dict) else None
        if (
            isinstance(proactive, dict)
            and isinstance(preference, dict)
            and isinstance(preference.get("destination"), dict)
            and preference["destination"].get("kind") == "channel"
        ):
            proactive.setdefault("external_enabled", True)
        server = changes.get("server")
        if isinstance(server, dict):
            from muli.auth import hash_password

            password = server.pop("password", None)
            if server.pop("clear_password", False):
                server["password_hash"] = ""
            elif password:
                if not isinstance(password, str):
                    raise ValueError("密码必须是字符串")
                server["password_hash"] = hash_password(password)
            server.pop("password_set", None)
        data = self.settings.model_dump()
        self._merge(data, changes)
        target = Settings.model_validate(data)
        channel_changes = changes.get("channels")
        proactive = channel_changes.get("proactive") if isinstance(channel_changes, dict) else None
        if isinstance(proactive, dict):
            # Revoking a binding must remain possible. Validate a newly chosen
            # destination, not every saved historical destination on every load.
            enabling = (
                proactive.get("external_enabled") is True
                and not self.settings.channels.proactive.external_enabled
            )
            if "default_destination" in proactive or (
                enabling and target.channels.proactive.memory_preference is None
            ):
                target.channels.validate_proactive_destination()
            preference = target.channels.proactive.memory_preference
            if preference is not None and ("memory_preference" in proactive or enabling):
                target.channels.validate_proactive_destination(preference.destination)
        return target

    def update(self, changes: dict[str, Any]) -> Settings:
        settings = self.validate_changes(changes)
        return self.replace(settings)

    def replace(self, settings: Settings) -> Settings:
        """Atomically publish a fully validated snapshot, including intentional empty values."""
        settings = Settings.model_validate(settings.model_dump())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".muli-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(settings.model_dump(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
        self.settings = settings
        return settings

    @classmethod
    def _merge(
        cls, target: dict[str, Any], changes: dict[str, Any], path: tuple[str, ...] = ()
    ) -> None:
        clear_key = changes.pop("clear_api_key", False)
        if clear_key:
            target["api_key"] = ""
        for secret in ("token", "webhook_secret", "client_secret"):
            if changes.pop(f"clear_{secret}", False):
                target[secret] = ""
        for key, value in changes.items():
            if key in {
                "api_key_set",
                "credential_set",
                "token_set",
                "webhook_secret_set",
                "client_secret_set",
            }:
                continue
            if (
                key in {"api_key", "credential", "token", "webhook_secret", "client_secret"}
                and value == ""
            ):
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                if (*path, key) in {
                    ("channels", "proactive", "default_destination"),
                    ("channels", "proactive", "memory_preference"),
                }:
                    target[key] = value
                elif key == "mcp":
                    target[key] = _replace_mcp(target[key], value)
                elif key == "extra_headers":
                    target[key] = {
                        k: target[key].get(k, "") if v == "••••" else v for k, v in value.items()
                    }
                elif key in {"extra_body", "reasoning", "thinking"}:
                    target[key] = value
                else:
                    cls._merge(target[key], value, (*path, key))
            else:
                target[key] = value
