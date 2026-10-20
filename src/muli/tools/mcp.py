"""显式启用的 MCP 服务器连接，工具仍通过运行时统一权限门。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class ToolPage:
    tools: list[Any]
    next_cursor: str | None = None


class MCPPrepareError(ValueError):
    def __init__(self, code: str, source: str = "") -> None:
        self.code, self.source = code, source
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MCPLimits:
    max_tools: int = 100
    max_pages: int = 8
    max_catalog_bytes: int = 262144
    max_output_chars: int = 24000


@dataclass(frozen=True, slots=True)
class _RemoteTool:
    name: str
    description: str
    schema: JsonObject
    size: int


@dataclass(eq=False, slots=True)
class _Connection:
    name: str
    raw: JsonObject
    server: JsonObject
    timeout: float
    client: MCPClient
    stack: AsyncExitStack
    catalog: tuple[_RemoteTool, ...] = ()
    usable: bool = True
    close_failed: bool = False
    closed: bool = False
    close_attempted: bool = False
    cleanup_task: asyncio.Task[bool] | None = None


@dataclass(frozen=True, slots=True)
class MCPCandidate:
    config: JsonObject
    connections: dict[str, _Connection]
    catalogs: dict[str, tuple[_RemoteTool, ...]]
    tools: tuple[Tool, ...]
    statuses: tuple[JsonObject, ...]
    created: tuple[_Connection, ...]
    limits: MCPLimits
    secrets: frozenset[str] = field(repr=False)


class _FastMCPClient:
    """Adapt the locked FastMCP release, whose public list_tools reads one page."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def __aenter__(self) -> Self:
        await self.client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return cast(bool | None, await self.client.__aexit__(exc_type, exc_value, traceback))

    async def close(self) -> None:
        await self.client.close()

    async def list_tools(self) -> list[Any]:
        return (await self.list_tools_page(None)).tools

    async def list_tools_page(self, cursor: str | None) -> ToolPage:
        from mcp.types import PaginatedRequestParams

        result = await self.client.session.list_tools(params=PaginatedRequestParams(cursor=cursor))
        return ToolPage(list(result.tools), result.nextCursor)

    async def call_tool(
        self, name: str, arguments: JsonObject, *, timeout: float, raise_on_error: bool
    ) -> Any:
        return await self.client.call_tool(
            name, arguments, timeout=timeout, raise_on_error=raise_on_error
        )


async def _ignore_server_log(message: Any) -> None:
    """不把远端日志中的请求头或环境信息回显到应用日志。"""


def _default_factory(
    server: JsonObject, timeout: float, changed: Callable[[], None] | None = None
) -> MCPClient:
    # MCP 是可选依赖；未配置或未启用服务器时不加载。
    from fastmcp import Client
    from fastmcp.client.messages import MessageHandler
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

    class CatalogNotifications(MessageHandler):
        async def on_tool_list_changed(self, message: Any) -> None:
            if changed is not None:
                changed()

    return _FastMCPClient(
        Client(
            transport,
            timeout=timeout,
            init_timeout=timeout,
            log_handler=_ignore_server_log,
            message_handler=CatalogNotifications(),
        )
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
        max_pages: int = 8,
        max_catalog_bytes: int = 262144,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if (
            timeout <= 0
            or max_output_chars < 128
            or min(max_tools, max_pages, max_catalog_bytes) < 1
        ):
            raise ValueError("MCP 超时和工具数必须为正，输出上限至少为 128 字符")
        self.config = deepcopy(config)
        self.timeout, self.max_output_chars, self.max_tools = timeout, max_output_chars, max_tools
        self._limits = MCPLimits(max_tools, max_pages, max_catalog_bytes, max_output_chars)
        self._factory = client_factory
        self._connections: dict[str, _Connection] = {}
        self._retiring: set[_Connection] = set()
        self._status: dict[str, JsonObject] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._changed: Callable[[str], None] | None = None
        self.retirement_errors: list[JsonObject] = []
        self.tools: list[Tool] = []
        self._secrets = self._config_secrets(config)

    @staticmethod
    def _config_secrets(config: JsonObject) -> set[str]:
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers 必须是按服务器名称索引的对象")
        secrets: set[str] = set()
        for raw in servers.values():
            if isinstance(raw, dict):
                for field in ("headers", "env"):
                    values = raw.get(field, {})
                    if isinstance(values, dict):
                        secrets.update(
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
                                        secrets.add(token)
        return secrets

    def set_changed_callback(self, callback: Callable[[str], None] | None) -> None:
        self._changed = callback

    def _notify(self, connection: _Connection) -> None:
        if (
            not self._closed
            and self._connections.get(connection.name) is connection
            and connection.usable
            and self._changed is not None
        ):
            self._changed(connection.name)

    def status(self) -> list[JsonObject]:
        return deepcopy(list(self._status.values()))

    @property
    def pending_cleanup(self) -> int:
        return len(self._retiring)

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
            candidate = await self.prepare(self.config, strict=False)
            self.publish(candidate)
            return list(self.tools)

    async def _catalog(self, client: MCPClient, limits: MCPLimits) -> tuple[_RemoteTool, ...]:
        cursor: str | None = None
        cursors: set[str] = set()
        names: set[str] = set()
        catalog: list[_RemoteTool] = []
        total_bytes = 0
        page_method = getattr(client, "list_tools_page", None)
        for _ in range(limits.max_pages):
            if page_method is None:
                page = ToolPage(await client.list_tools())
            else:
                page = await page_method(cursor)
            if not isinstance(page, ToolPage) or not isinstance(page.tools, list):
                raise MCPPrepareError("catalog_invalid")
            for remote in page.tools:
                name = getattr(remote, "name", None)
                description = getattr(remote, "description", None)
                schema = getattr(remote, "inputSchema", None)
                if (
                    not isinstance(name, str)
                    or not name
                    or name in names
                    or not isinstance(schema, dict)
                    or (description is not None and not isinstance(description, str))
                ):
                    raise MCPPrepareError("catalog_invalid")
                names.add(name)
                try:
                    size = len(
                        json.dumps(
                            [name, description or name, schema],
                            ensure_ascii=False,
                            allow_nan=False,
                        ).encode()
                    )
                    Draft202012Validator.check_schema(schema)
                except Exception as exc:
                    raise MCPPrepareError("schema_invalid") from exc
                total_bytes += size
                if len(catalog) >= limits.max_tools or total_bytes > limits.max_catalog_bytes:
                    raise MCPPrepareError("catalog_budget_exceeded")
                catalog.append(_RemoteTool(name, description or name, deepcopy(schema), size))
            next_cursor = page.next_cursor
            if next_cursor is None:
                return tuple(catalog)
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
                raise MCPPrepareError("pagination_stalled")
            cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPPrepareError("page_budget_exceeded")

    def _tools_for(
        self, connection: _Connection, catalog: tuple[_RemoteTool, ...], secrets: set[str]
    ) -> list[Tool]:
        tools: list[Tool] = []
        for remote in catalog:

            async def handler(
                arguments: JsonObject,
                *,
                _connection: _Connection = connection,
                _remote: str = remote.name,
            ) -> Any:
                return await self._call(_connection, _remote, arguments)

            description = remote.description
            for secret in sorted(secrets, key=len, reverse=True):
                description = description.replace(secret, "••••")
            tools.append(
                Tool(
                    _name(connection.name, remote.name),
                    description,
                    deepcopy(remote.schema),
                    handler,
                    risk="external",
                )
            )
        return tools

    async def prepare(
        self,
        config: JsonObject,
        *,
        strict: bool = True,
        refresh: set[str] | None = None,
        limits: MCPLimits | None = None,
    ) -> MCPCandidate:
        """Prepare a complete catalog; reused connections remain owned by the active generation."""
        if self._closed:
            raise MCPPrepareError("manager_closed")
        if self._retiring:
            raise MCPPrepareError("restart_required", "connection_cleanup_pending")
        limits = limits or self._limits
        config = deepcopy(config)
        secrets = self._config_secrets(config)
        connections: dict[str, _Connection] = {}
        catalogs: dict[str, tuple[_RemoteTool, ...]] = {}
        created: list[_Connection] = []
        statuses: list[JsonObject] = []
        tools: list[Tool] = []
        total_bytes = 0
        try:
            for name, raw in config.get("mcpServers", {}).items():
                status: JsonObject = {"name": name, "status": "disabled", "tool_count": 0}
                statuses.append(status)
                if (
                    not isinstance(raw, dict)
                    or raw.get("enabled") is not True
                    or raw.get("disabled") is True
                ):
                    continue
                connection: _Connection | None = None
                new = False
                try:
                    server, timeout = self._server(raw)
                    status["transport"] = server["transport"]
                    previous = self._connections.get(name)
                    if previous is not None and previous.raw == raw and previous.usable:
                        connection = previous
                        catalog = previous.catalog
                        if refresh and name in refresh:
                            async with asyncio.timeout(timeout):
                                catalog = await self._catalog(connection.client, limits)
                    else:
                        if previous is not None and previous.server["transport"] == "stdio":
                            raise MCPPrepareError("restart_required", name)
                        stack = AsyncExitStack()
                        holder: list[_Connection] = []

                        def changed(_holder: list[_Connection] = holder) -> None:
                            if _holder:
                                self._notify(_holder[0])

                        client = (
                            self._factory(server, timeout)
                            if self._factory is not None
                            else _default_factory(server, timeout, changed)
                        )
                        stack.push_async_callback(client.close)
                        connection = _Connection(name, raw, server, timeout, client, stack)
                        holder.append(connection)
                        created.append(connection)
                        new = True
                        async with asyncio.timeout(timeout):
                            await stack.enter_async_context(client)
                            catalog = await self._catalog(client, limits)
                    size = sum(item.size for item in catalog)
                    if (
                        len(tools) + len(catalog) > limits.max_tools
                        or total_bytes + size > limits.max_catalog_bytes
                    ):
                        raise MCPPrepareError("catalog_budget_exceeded", name)
                    total_bytes += size
                    connections[name] = connection
                    catalogs[name] = catalog
                    tools.extend(self._tools_for(connection, catalog, secrets))
                    status.update(status="connected", tool_count=len(catalog))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    code = (
                        exc.code
                        if isinstance(exc, MCPPrepareError)
                        else "dependency_missing"
                        if isinstance(exc, ImportError)
                        else "candidate_timeout"
                        if isinstance(exc, TimeoutError)
                        else "server_unavailable"
                    )
                    status.update(status="error", error_code=code)
                    status["error"] = (
                        "缺少可选 MCP 依赖；安装 MuLi 的 mcp 可选组件后重试"
                        if code == "dependency_missing"
                        else code
                    )
                    if strict:
                        raise MCPPrepareError(code, name) from exc
                    if new and connection is not None:
                        await self._retire([connection], stage="candidate_cleanup")
            return MCPCandidate(
                config,
                connections,
                catalogs,
                tuple(tools),
                tuple(statuses),
                tuple(created),
                limits,
                frozenset(secrets),
            )
        except BaseException:
            await self._retire(created, stage="candidate_cleanup")
            raise

    def publish(self, candidate: MCPCandidate) -> tuple[_Connection, ...]:
        """Only reference swaps; the caller validates registry conflicts before persisting config."""
        retired = tuple(
            connection
            for name, connection in self._connections.items()
            if candidate.connections.get(name) is not connection
        )
        for name, connection in candidate.connections.items():
            connection.catalog = candidate.catalogs[name]
        self.config, self._connections = candidate.config, candidate.connections
        self._status = {item["name"]: item for item in candidate.statuses}
        self.tools = list(candidate.tools)
        self._limits = candidate.limits
        self.max_tools = candidate.limits.max_tools
        self.max_output_chars = candidate.limits.max_output_chars
        self._secrets = set(candidate.secrets)
        self._started = True
        return retired

    async def discard(self, candidate: MCPCandidate) -> None:
        await self._retire(candidate.created, stage="candidate_cleanup")

    async def _cleanup(self, connection: _Connection, stage: str) -> bool:
        connection.close_failed = True
        try:
            async with asyncio.timeout(self.timeout):
                if connection.close_attempted:
                    # AsyncExitStack has consumed its callbacks after a failed/expired close.
                    # FastMCP.close uses force-disconnect and the transport's idempotent close.
                    await connection.client.close()
                else:
                    connection.close_attempted = True
                    await connection.stack.aclose()
        except (Exception, asyncio.CancelledError):
            self.retirement_errors = [
                *self.retirement_errors[-31:],
                {"name": connection.name, "error_code": "connection_close_failed", "stage": stage},
            ]
            return False
        connection.closed = True
        connection.close_failed = False
        self._retiring.discard(connection)
        return True

    async def _retire(
        self,
        connections: Iterable[_Connection],
        *,
        stage: str = "retirement",
        retry_failed: bool = False,
    ) -> bool:
        complete = True
        cancelled = False
        pending: list[asyncio.Task[bool]] = []
        for connection in reversed(list(connections)):
            if connection.closed:
                continue
            connection.usable = False
            self._retiring.add(connection)
            task = connection.cleanup_task
            if task is None or (task.done() and retry_failed):
                task = asyncio.create_task(self._cleanup(connection, stage), name="muli-mcp-close")
                connection.cleanup_task = task
            pending.append(task)
        for task in pending:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Repeated cancellation of a caller must not lose the stack callback.
                    cancelled = True
            complete = task.result() and complete
        if cancelled:
            raise asyncio.CancelledError
        return complete

    async def retire(self, connections: tuple[_Connection, ...]) -> bool:
        return await self._retire(connections)

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
        self, connection: _Connection, remote: str, arguments: JsonObject
    ) -> JsonObject:
        if self._closed or not connection.usable:
            return {"is_error": True, "error": "MCP 服务器未连接"}
        try:
            async with asyncio.timeout(connection.timeout):
                result = await connection.client.call_tool(
                    remote, arguments, timeout=connection.timeout, raise_on_error=False
                )
                return self._result(result)
        except TimeoutError:
            return {"is_error": True, "error": "MCP 工具调用达到时间上限"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"is_error": True, "error": f"MCP 工具调用失败：{type(exc).__name__}"}

    async def _close_connections(self) -> None:
        await self._retire(
            set(self._connections.values()) | self._retiring,
            stage="shutdown",
            retry_failed=True,
        )
        for name, connection in self._connections.items():
            self._status[name]["status"] = "close_failed" if connection.close_failed else "closed"
        self._connections.clear()
        self.tools.clear()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._close_connections()
