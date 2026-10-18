"""Validated configuration with atomic writes and secret-free views."""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class Limits(Section):
    max_parallel_agents: int = Field(default=2, ge=1, le=4)
    max_pending_runs: int = Field(default=8, ge=1, le=128)
    max_tool_steps: int = Field(default=16, ge=1, le=100)
    max_run_seconds: int = Field(default=600, ge=10, le=7200)
    max_file_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    max_output_chars: int = Field(default=24000, ge=1000, le=100000)
    max_context_chars: int = Field(default=96000, ge=4000, le=1000000)
    max_workflow_steps: int = Field(default=12, ge=1, le=32)
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


class Computer(Section):
    enabled: bool = False
    backend: Literal["atspi"] = "atspi"
    python_path: str = "/usr/bin/python3"
    max_nodes: int = Field(default=300, ge=10, le=1000)


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


class Server(Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def local_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("管理服务仅绑定本机回环地址")
        return value


class Settings(Section):
    models: Models = Field(default_factory=Models)
    default_mode: Literal["ask", "auto", "yolo"] = "auto"
    workspace_roots: list[str] = Field(default_factory=lambda: [str(Path.home())])
    search: Search = Field(default_factory=Search)
    computer: Computer = Field(default_factory=Computer)
    chiliarch: Chiliarch = Field(default_factory=Chiliarch)
    notifications: DesktopNotifications = Field(default_factory=DesktopNotifications)
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
    return Path(os.environ.get("MULI_DATA_DIR", Path.home() / ".local/share/muli")).expanduser()


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(os.environ.get("MULI_CONFIG", base / "muli/config.json")).expanduser()


class ConfigFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.settings = (
            Settings.model_validate_json(path.read_text(encoding="utf-8"))
            if path.exists()
            else Settings()
        )

    def public(self) -> dict[str, Any]:
        data = self.settings.model_dump()
        for section in [*data["models"].values(), data["search"]]:
            section["api_key_set"] = bool(
                section.pop("api_key", "") or os.environ.get(section.get("api_key_env", ""), "")
            )
            if "extra_headers" in section:
                section["extra_headers"] = {key: "••••" for key in section["extra_headers"]}
        data["chiliarch"]["credential_set"] = bool(data["chiliarch"].pop("credential"))
        data["mcp"] = _public_mcp(data["mcp"])
        return data

    def update(self, changes: dict[str, Any]) -> Settings:
        data = self.settings.model_dump()
        self._merge(data, copy.deepcopy(changes))
        settings = Settings.model_validate(data)
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
    def _merge(cls, target: dict[str, Any], changes: dict[str, Any]) -> None:
        clear_key = changes.pop("clear_api_key", False)
        if clear_key:
            target["api_key"] = ""
        for key, value in changes.items():
            if key in {"api_key_set", "credential_set"}:
                continue
            if key in {"api_key", "credential"} and value == "":
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                if key == "mcp":
                    target[key] = _replace_mcp(target[key], value)
                elif key == "extra_headers":
                    target[key] = {
                        k: target[key].get(k, "") if v == "••••" else v for k, v in value.items()
                    }
                elif key in {"extra_body", "reasoning"}:
                    target[key] = value
                else:
                    cls._merge(target[key], value)
            else:
                target[key] = value
