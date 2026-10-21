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

from muli.mcp_policy import ReceivePolicy, checked_policy
from muli.store import new_id
from muli.tools import Tool
from muli.tools.mcp_receive import ReceiveState, install_receive_guard

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


@dataclass(frozen=True, eq=False, slots=True)
class _Generation:
    secrets: frozenset[str] = field(repr=False)
    max_output_chars: int


@dataclass(eq=False, slots=True)
class _Connection:
    name: str
    raw: JsonObject
    server: JsonObject
    timeout: float
    client: MCPClient
    stack: AsyncExitStack
    receive: ReceiveState
    receive_occurrence_id: str = field(default_factory=new_id, repr=False)
    catalog: tuple[_RemoteTool, ...] = ()
    usable: bool = True
    close_failed: bool = False
    closed: bool = False
    close_attempted: bool = False
    cleanup_task: asyncio.Task[bool] | None = None
    active_calls: int = 0
    idle: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self) -> None:
        self.idle.set()


@dataclass(frozen=True, slots=True)
class MCPCandidate:
    config: JsonObject
    connections: dict[str, _Connection]
    catalogs: dict[str, tuple[_RemoteTool, ...]]
    tools: tuple[Tool, ...]
    statuses: tuple[JsonObject, ...]
    created: tuple[_Connection, ...]
    limits: MCPLimits
    generation: _Generation = field(repr=False)
    receive_policy: ReceivePolicy


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
    server: JsonObject,
    timeout: float,
    changed: Callable[[], None] | None = None,
    receive: ReceiveState | None = None,
) -> MCPClient:
    # MCP 是可选依赖；未配置或未启用服务器时不加载。
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.types import ServerNotification, ToolListChangedNotification

    receive = receive or ReceiveState(ReceivePolicy())

    def http_client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        install_receive_guard(client, receive, streamable=server["transport"] == "http")
        return client

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
        transport = transport_type(
            url=server["url"],
            headers=server.get("headers"),
            httpx_client_factory=http_client_factory,
        )

    async def catalog_notifications(message: Any) -> None:
        if (
            changed is not None
            and isinstance(message, ServerNotification)
            and isinstance(message.root, ToolListChangedNotification)
        ):
            changed()

    return _FastMCPClient(
        Client(
            transport,
            timeout=timeout,
            init_timeout=timeout,
            log_handler=_ignore_server_log,
            message_handler=catalog_notifications,
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
        receive_policy: ReceivePolicy | None = None,
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
        self.receive_policy = checked_policy(receive_policy or ReceivePolicy())
        self._connections: dict[str, _Connection] = {}
        self._retiring: set[_Connection] = set()
        self._status: dict[str, JsonObject] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._generation: _Generation | None = None
        self._candidate: _Generation | None = None
        self._changed: Callable[[str], None] | None = None
        self._receive_failed_callback: Callable[[str, str], None] | None = None
        self.retirement_errors: list[JsonObject] = []
        self.tools: list[Tool] = []

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

    def set_receive_failed_callback(self, callback: Callable[[str, str], None] | None) -> None:
        self._receive_failed_callback = callback

    def _notify(self, connection: _Connection) -> None:
        if (
            not self._closed
            and self._connections.get(connection.name) is connection
            and connection.usable
            and self._changed is not None
        ):
            self._changed(connection.name)

    def _receive_failed(self, connection: _Connection) -> None:
        connection.usable = False
        if self._connections.get(connection.name) is connection:
            self._status[connection.name].update(
                status="error", error_code=connection.receive.code, error=connection.receive.code
            )
            self._schedule_retirement([connection], stage="receive_failure")
            if (
                not self._closed
                and connection.server["transport"] in {"http", "sse"}
                and connection.receive.code is not None
                and self._receive_failed_callback is not None
            ):
                try:
                    self._receive_failed_callback(
                        connection.receive.code, connection.receive_occurrence_id
                    )
                except Exception:
                    # Evidence failure must never undo the latch or lose its cleanup owner.
                    pass

    def status(self) -> list[JsonObject]:
        return deepcopy(list(self._status.values()))

    @property
    def effective_receive_policy(self) -> ReceivePolicy | None:
        return self.receive_policy if self._started else None

    @property
    def pending_cleanup(self) -> int:
        return len(self._retiring)

    @property
    def draining(self) -> int:
        return sum(connection.active_calls > 0 for connection in self._retiring)

    @property
    def cleanup_failed(self) -> bool:
        return any(
            connection.close_failed
            and connection.cleanup_task is not None
            and connection.cleanup_task.done()
            for connection in self._retiring
        )

    @staticmethod
    def _redact(value: str, generation: _Generation) -> str:
        for secret in sorted(generation.secrets, key=len, reverse=True):
            value = value.replace(secret, "••••")
        return value

    def _sanitize(self, value: Any, generation: _Generation) -> Any:
        if isinstance(value, str):
            return self._redact(value, generation)
        if isinstance(value, dict):
            return {
                self._redact(str(key), generation): self._sanitize(item, generation)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item, generation) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._redact(str(value), generation)

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
        self, connection: _Connection, catalog: tuple[_RemoteTool, ...], generation: _Generation
    ) -> list[Tool]:
        tools: list[Tool] = []
        for remote in catalog:

            async def handler(
                arguments: JsonObject,
                *,
                _connection: _Connection = connection,
                _remote: str = remote.name,
            ) -> Any:
                return await self._call(_connection, _remote, arguments, generation)

            description = self._redact(remote.description, generation)
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
        receive_policy: ReceivePolicy | None = None,
    ) -> MCPCandidate:
        """Prepare a complete catalog; reused connections remain owned by the active generation."""
        if self._closed:
            raise MCPPrepareError("manager_closed")
        if self._retiring:
            if self.cleanup_failed:
                raise MCPPrepareError("restart_required", "connection_cleanup_pending")
            raise MCPPrepareError("generation_draining")
        if self._candidate is not None:
            raise MCPPrepareError("candidate_pending")
        limits = limits or self._limits
        receive_policy = checked_policy(receive_policy or self.receive_policy)
        config = deepcopy(config)
        secrets = self._config_secrets(config)
        generation = _Generation(frozenset(secrets), limits.max_output_chars)
        self._candidate = generation
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
                    if (
                        previous is not None
                        and previous.raw == raw
                        and previous.usable
                        and previous.receive.code is None
                        and (
                            server["transport"] == "stdio"
                            or previous.receive.policy == receive_policy
                        )
                    ):
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
                        receive = ReceiveState(receive_policy)

                        def changed(_holder: list[_Connection] = holder) -> None:
                            if _holder:
                                self._notify(_holder[0])

                        client = (
                            self._factory(server, timeout)
                            if self._factory is not None
                            else _default_factory(server, timeout, changed, receive)
                        )
                        stack.push_async_callback(client.close)
                        connection = _Connection(name, raw, server, timeout, client, stack, receive)
                        receive.failed = lambda item=connection: self._receive_failed(item)
                        holder.append(connection)
                        created.append(connection)
                        new = True
                        async with asyncio.timeout(timeout):
                            await stack.enter_async_context(client)
                            catalog = await self._catalog(client, limits)
                    if connection.receive.code is not None:
                        raise MCPPrepareError(connection.receive.code, name)
                    size = sum(item.size for item in catalog)
                    if (
                        len(tools) + len(catalog) > limits.max_tools
                        or total_bytes + size > limits.max_catalog_bytes
                    ):
                        raise MCPPrepareError("catalog_budget_exceeded", name)
                    total_bytes += size
                    connections[name] = connection
                    catalogs[name] = catalog
                    tools.extend(self._tools_for(connection, catalog, generation))
                    status.update(status="connected", tool_count=len(catalog))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    code = (
                        connection.receive.code
                        if connection is not None and connection.receive.code is not None
                        else exc.code
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
            if self._closed:
                raise MCPPrepareError("manager_closed")
            return MCPCandidate(
                config,
                connections,
                catalogs,
                tuple(tools),
                tuple(statuses),
                tuple(created),
                limits,
                generation,
                receive_policy,
            )
        except BaseException:
            try:
                await self._retire(created, stage="candidate_cleanup")
            finally:
                if self._candidate is generation:
                    self._candidate = None
            raise

    def validate_candidate(self, candidate: MCPCandidate) -> None:
        """Synchronous final fence, also usable before a caller persists configuration."""
        if self._closed:
            raise MCPPrepareError("manager_closed")
        if self._candidate is not candidate.generation:
            raise MCPPrepareError("candidate_changed")
        for connection in candidate.connections.values():
            if connection.receive.code is not None or not connection.usable:
                raise MCPPrepareError(
                    connection.receive.code or "connection_changed", connection.name
                )

    def publish(self, candidate: MCPCandidate) -> tuple[_Connection, ...]:
        """Only reference swaps; the caller validates registry conflicts before persisting config."""
        self.validate_candidate(candidate)
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
        self.receive_policy = candidate.receive_policy
        self.max_tools = candidate.limits.max_tools
        self.max_output_chars = candidate.limits.max_output_chars
        self._generation = candidate.generation
        self._candidate = None
        self._started = True
        self._schedule_retirement(retired)
        return retired

    async def discard(self, candidate: MCPCandidate) -> None:
        if candidate.generation is self._generation:
            raise MCPPrepareError("candidate_already_published")
        try:
            await self._retire(candidate.created, stage="candidate_cleanup")
        finally:
            if self._candidate is candidate.generation:
                self._candidate = None

    async def _cleanup(self, connection: _Connection, stage: str) -> bool:
        # A published token has already revoked new admissions. Existing calls
        # retain their exact connection until the SDK stack has actually settled.
        await connection.idle.wait()
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

    def _schedule_retirement(
        self,
        connections: Iterable[_Connection],
        *,
        stage: str = "retirement",
        retry_failed: bool = False,
    ) -> list[asyncio.Task[bool]]:
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
        return pending

    async def _retire(
        self,
        connections: Iterable[_Connection],
        *,
        stage: str = "retirement",
        retry_failed: bool = False,
    ) -> bool:
        complete = True
        cancelled = False
        pending = self._schedule_retirement(connections, stage=stage, retry_failed=retry_failed)
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

    def _result(self, result: Any, generation: _Generation) -> JsonObject:
        maximum = generation.max_output_chars
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            return {"is_error": True, "error": "MCP 服务器报告工具执行失败"}
        content: list[JsonObject] = []
        for block in getattr(result, "content", []):
            kind = str(getattr(block, "type", "unknown"))
            if kind == "text":
                content.append(
                    {
                        "type": "text",
                        "text": self._redact(str(getattr(block, "text", "")), generation)[:maximum],
                    }
                )
            elif kind == "resource" and hasattr(getattr(block, "resource", None), "text"):
                content.append(
                    {
                        "type": "text",
                        "text": self._redact(str(block.resource.text), generation)[:maximum],
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
        encoded = json.dumps(self._sanitize(payload, generation), ensure_ascii=False)
        if len(encoded) <= maximum:
            return cast(JsonObject, json.loads(encoded))
        wrapper: JsonObject = {"is_error": False, "truncated": True, "preview": ""}
        lower, upper = 0, min(len(encoded), maximum)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            wrapper["preview"] = encoded[:middle]
            if len(json.dumps(wrapper, ensure_ascii=False)) <= maximum:
                lower = middle
            else:
                upper = middle - 1
        wrapper["preview"] = encoded[:lower]
        return wrapper

    async def _call(
        self, connection: _Connection, remote: str, arguments: JsonObject, generation: _Generation
    ) -> JsonObject:
        # This synchronous gate follows Runtime approval/task guards. A schema
        # snapshot alone never authorizes entry to a revoked endpoint.
        if (
            self._closed
            or generation is not self._generation
            or not connection.usable
            or connection.receive.code is not None
            or self._connections.get(connection.name) is not connection
        ):
            return {
                "is_error": True,
                "status": "not_executed",
                "code": (
                    "manager_closed"
                    if self._closed
                    else connection.receive.code or "generation_changed"
                ),
            }
        connection.active_calls += 1
        connection.idle.clear()
        try:
            async with asyncio.timeout(connection.timeout):
                result = await connection.client.call_tool(
                    remote, arguments, timeout=connection.timeout, raise_on_error=False
                )
                return self._result(result, generation)
        except TimeoutError:
            return {"is_error": True, "error": "MCP 工具调用达到时间上限"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"is_error": True, "error": f"MCP 工具调用失败：{type(exc).__name__}"}
        finally:
            connection.active_calls -= 1
            if not connection.active_calls:
                connection.idle.set()

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
        if self._retiring:
            # A whole Runtime reconfiguration must retain this owner until every
            # connection is reclaimed, just as candidate publication already does.
            raise MCPPrepareError("connection_cleanup_pending")

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._close_connections()
