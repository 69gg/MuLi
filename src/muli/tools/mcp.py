"""显式启用的 MCP 服务器连接，工具仍通过运行时统一权限门。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import AsyncExitStack
from copy import deepcopy
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, cast
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from muli.tools import Tool

JsonObject = dict[str, Any]


class MCPClient(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    async def close(self) -> None: ...

    async def list_tools(self) -> list[Any]: ...

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        timeout: float,
        raise_on_error: bool,
    ) -> Any: ...


ClientFactory = Callable[[JsonObject, float], MCPClient]


async def _ignore_server_log(message: Any) -> None:
    """不把远端日志中的请求头或环境信息回显到应用日志。"""


def _default_factory(server: JsonObject, timeout: float) -> MCPClient:
    # MCP 是可选依赖；未配置或未启用服务器时不加载。
    from fastmcp import Client
    from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

    transport: Any
    if server["transport"] == "stdio":
        transport = StdioTransport(
            command=server["command"],
            args=server["args"],
            env=server.get("env"),
            cwd=server.get("cwd"),
            keep_alive=False,
            log_file=Path(os.devnull),
        )
    else:
        transport_type = SSETransport if server["transport"] == "sse" else StreamableHttpTransport
        transport = transport_type(url=server["url"], headers=server.get("headers"))
    return cast(
        MCPClient,
        Client(transport, timeout=timeout, init_timeout=timeout, log_handler=_ignore_server_log),
    )


def _name(server: str, remote_tool: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", server)[:16]
    suffix = re.sub(r"[^A-Za-z0-9_-]", "_", remote_tool)[:28]
    digest = hashlib.sha256(f"{server}\0{remote_tool}".encode()).hexdigest()[:10]
    return f"mcp_{prefix}_{suffix}_{digest}"


class MCPManager:
    def __init__(
        self,
        config: JsonObject,
        *,
        timeout: float = 30,
        max_output_chars: int = 24000,
        max_tools: int = 100,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if timeout <= 0 or max_output_chars < 128 or max_tools < 1:
            raise ValueError("MCP 超时和工具数必须为正，输出上限至少为 128 字符")
        self.config = deepcopy(config)
        self.timeout, self.max_output_chars, self.max_tools = timeout, max_output_chars, max_tools
        self._factory = client_factory or _default_factory
        self._clients: dict[str, MCPClient] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._status: dict[str, JsonObject] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self.tools: list[Tool] = []
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers 必须是按服务器名称索引的对象")
        self._secrets: set[str] = set()
        for raw in servers.values():
            if isinstance(raw, dict):
                for field in ("headers", "env"):
                    values = raw.get(field, {})
                    if isinstance(values, dict):
                        self._secrets.update(
                            value for value in values.values() if isinstance(value, str) and value
                        )
                        if field == "headers":
                            for key, value in values.items():
                                if str(key).lower() in {
                                    "authorization",
                                    "proxy-authorization",
                                } and isinstance(value, str):
                                    _, separator, token = value.partition(" ")
                                    if separator and token:
                                        self._secrets.add(token)

    def status(self) -> list[JsonObject]:
        return deepcopy(list(self._status.values()))

    def _redact(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, "••••")
        return value

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {str(key): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._redact(str(value))

    def _server(self, raw: JsonObject) -> tuple[JsonObject, float]:
        server = {
            key: deepcopy(value)
            for key, value in raw.items()
            if key not in {"enabled", "disabled", "timeout"}
        }
        transport = server.get("transport") or ("stdio" if "command" in server else "http")
        if transport == "streamable-http":
            transport = "http"
        if transport not in {"stdio", "http", "sse"}:
            raise ValueError("MCP transport 必须为 stdio、http 或 sse")
        server["transport"] = transport
        limit = raw.get("timeout", self.timeout)
        if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("MCP timeout 必须大于零")
        if transport == "stdio":
            if set(server) - {"transport", "command", "args", "env", "cwd"}:
                raise ValueError("不支持的 MCP stdio 配置字段")
            command, args = server.get("command"), server.get("args", [])
            if not isinstance(command, str) or not command.strip() or "\x00" in command:
                raise ValueError("MCP command 必须是已安装的可执行程序")
            if not isinstance(args, list) or any(
                not isinstance(arg, str) or "\x00" in arg for arg in args
            ):
                raise ValueError("MCP args 必须是字符串数组")
            executable = Path(command).name.lower()
            if executable in {"npx", "npx.cmd"} and not {"--no-install", "--offline"}.intersection(
                args
            ):
                raise ValueError("npx 必须显式使用 --no-install 或 --offline，禁止自动下载")
            if executable in {"uvx", "uvx.exe"} and "--offline" not in args:
                raise ValueError("uvx 必须显式使用 --offline，禁止自动下载")
            server["args"] = args
            if "env" in server and (
                not isinstance(server["env"], dict)
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in server["env"].items()
                )
            ):
                raise ValueError("MCP env 必须是字符串映射")
            if "cwd" in server and not isinstance(server["cwd"], str):
                raise ValueError("MCP cwd 必须是路径字符串")
        else:
            if set(server) - {"transport", "url", "headers"}:
                raise ValueError("不支持的 MCP HTTP 配置字段")
            url = server.get("url")
            if not isinstance(url, str):
                raise ValueError("MCP HTTP 服务器缺少 url")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ValueError("MCP url 必须是无内嵌账号的 HTTP(S) 地址")
            if "headers" in server and (
                not isinstance(server["headers"], dict)
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in server["headers"].items()
                )
            ):
                raise ValueError("MCP headers 必须是字符串映射")
        return server, min(float(limit), self.timeout)

    async def start(self) -> list[Tool]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP 管理器已经关闭")
            if self._started:
                return list(self.tools)
            self._started = True
            for name, raw in self.config.get("mcpServers", {}).items():
                status: JsonObject = {"name": str(name), "status": "disabled", "tool_count": 0}
                self._status[str(name)] = status
                if (
                    not isinstance(raw, dict)
                    or raw.get("enabled") is not True
                    or raw.get("disabled") is True
                ):
                    continue
                stack = AsyncExitStack()
                validated = False
                try:
                    server, timeout = self._server(raw)
                    validated = True
                    status.update(status="connecting", transport=server["transport"])
                    client = self._factory(server, timeout)
                    stack.push_async_callback(client.close)
                    async with asyncio.timeout(timeout):
                        await stack.enter_async_context(client)
                        remote_tools = await client.list_tools()
                    added: list[Tool] = []
                    seen: set[str] = set()
                    for remote in remote_tools:
                        remote_name = getattr(remote, "name", None)
                        schema = getattr(remote, "inputSchema", None)
                        if (
                            not isinstance(remote_name, str)
                            or not remote_name
                            or remote_name in seen
                            or not isinstance(schema, dict)
                        ):
                            raise ValueError("MCP 工具目录无效或存在重名")
                        seen.add(remote_name)
                        Draft202012Validator.check_schema(schema)
                        if len(self.tools) + len(added) >= self.max_tools:
                            status["tool_limit_reached"] = True
                            break

                        async def handler(
                            arguments: JsonObject,
                            *,
                            _name: str = str(name),
                            _remote: str = str(remote_name),
                            _timeout: float = timeout,
                        ) -> Any:
                            return await self._call(_name, _remote, arguments, _timeout)

                        added.append(
                            Tool(
                                _name(str(name), remote_name),
                                self._redact(
                                    str(getattr(remote, "description", None) or remote_name)
                                ),
                                deepcopy(schema),
                                handler,
                                risk="external",
                            )
                        )
                    self._clients[str(name)] = client
                    self._stacks[str(name)] = stack
                    self.tools.extend(added)
                    status.update(status="connected", tool_count=len(added))
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception):
                        await stack.aclose()
                    await self._close_connections()
                    self._started = False
                    raise
                except ImportError:
                    status.update(
                        status="error", error="缺少可选 MCP 依赖；安装 MuLi 的 mcp 可选组件后重试"
                    )
                    with contextlib.suppress(Exception):
                        await stack.aclose()
                except Exception as exc:
                    reason = (
                        str(exc)
                        if isinstance(exc, ValueError) and not validated
                        else f"MCP 连接失败：{type(exc).__name__}"
                    )
                    status.update(status="error", error=self._redact(reason)[:500])
                    with contextlib.suppress(Exception):
                        await stack.aclose()
            return list(self.tools)

    def _result(self, result: Any) -> JsonObject:
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            return {"is_error": True, "error": "MCP 服务器报告工具执行失败"}
        content: list[JsonObject] = []
        for block in getattr(result, "content", []):
            kind = str(getattr(block, "type", "unknown"))
            if kind == "text":
                content.append(
                    {
                        "type": "text",
                        "text": self._redact(str(getattr(block, "text", "")))[
                            : self.max_output_chars
                        ],
                    }
                )
            elif kind == "resource" and hasattr(getattr(block, "resource", None), "text"):
                content.append(
                    {
                        "type": "text",
                        "text": self._redact(str(block.resource.text))[: self.max_output_chars],
                    }
                )
            else:
                content.append(
                    {
                        "type": kind,
                        "mime_type": str(getattr(block, "mimeType", "")),
                        "omitted": True,
                    }
                )
        payload: JsonObject = {"is_error": False, "content": content}
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            payload["structured_content"] = structured
        encoded = json.dumps(self._sanitize(payload), ensure_ascii=False)
        if len(encoded) <= self.max_output_chars:
            return cast(JsonObject, json.loads(encoded))
        wrapper: JsonObject = {"is_error": False, "truncated": True, "preview": ""}
        lower, upper = 0, min(len(encoded), self.max_output_chars)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            wrapper["preview"] = encoded[:middle]
            if len(json.dumps(wrapper, ensure_ascii=False)) <= self.max_output_chars:
                lower = middle
            else:
                upper = middle - 1
        wrapper["preview"] = encoded[:lower]
        return wrapper

    async def _call(
        self, server: str, remote: str, arguments: JsonObject, timeout: float
    ) -> JsonObject:
        client = self._clients.get(server)
        if self._closed or client is None:
            return {"is_error": True, "error": "MCP 服务器未连接"}
        try:
            async with asyncio.timeout(timeout):
                result = await client.call_tool(
                    remote, arguments, timeout=timeout, raise_on_error=False
                )
                return self._result(result)
        except TimeoutError:
            return {"is_error": True, "error": "MCP 工具调用达到时间上限"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"is_error": True, "error": f"MCP 工具调用失败：{type(exc).__name__}"}

    async def _close_connections(self) -> None:
        for name, stack in reversed(list(self._stacks.items())):
            with contextlib.suppress(Exception):
                await stack.aclose()
            self._status[name]["status"] = "closed"
        self._stacks.clear()
        self._clients.clear()
        self.tools.clear()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._close_connections()
