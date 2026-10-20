from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace, TracebackType
from typing import Any, Self, cast

import pytest

from muli.tools.mcp import (
    MCPClient,
    MCPLimits,
    MCPManager,
    MCPPrepareError,
    ToolPage,
    _FastMCPClient,
)

JsonObject = dict[str, Any]


class FakeClient:
    def __init__(
        self,
        *,
        names: list[str] | None = None,
        result: Any = None,
        failure: bool = False,
        slow: bool = False,
    ) -> None:
        self.entered = 0
        self.exited = 0
        self.closed = 0
        self.failure = failure
        self.slow = slow
        self.calls: list[JsonObject] = []
        self.names = ["lookup"] if names is None else names
        self.result = (
            result
            if result is not None
            else SimpleNamespace(
                is_error=False,
                content=[SimpleNamespace(type="text", text="结果")],
                structured_content=None,
            )
        )

    async def __aenter__(self) -> Self:
        self.entered += 1
        if self.failure:
            raise ValueError("env-secret bare-header-secret")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exited += 1

    async def close(self) -> None:
        self.closed += 1

    async def list_tools(self) -> list[Any]:
        return [
            SimpleNamespace(
                name=name,
                description="查询",
                inputSchema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            )
            for name in self.names
        ]

    async def call_tool(
        self, name: str, arguments: JsonObject, *, timeout: float, raise_on_error: bool
    ) -> Any:
        self.calls.append(
            {
                "name": name,
                "arguments": arguments,
                "timeout": timeout,
                "raise_on_error": raise_on_error,
            }
        )
        if self.slow:
            await asyncio.Event().wait()
        return self.result


def settings(**server: Any) -> JsonObject:
    return {"mcpServers": {"test": {"enabled": True, "url": "https://example.test/mcp", **server}}}


@pytest.mark.asyncio
async def test_no_server_starts_without_explicit_boolean_enable() -> None:
    calls: list[JsonObject] = []

    def factory(server: JsonObject, timeout: float) -> MCPClient:
        calls.append(server)
        return FakeClient()

    manager = MCPManager(
        {
            "mcpServers": {
                "missing": {"command": "npx", "args": ["-y", "large-package"]},
                "false": {"enabled": False, "command": "python"},
                "string": {"enabled": "true", "url": "https://example.test/mcp"},
            }
        },
        client_factory=factory,
    )
    assert await manager.start() == []
    assert calls == []
    assert {item["status"] for item in manager.status()} == {"disabled"}
    await manager.close()


@pytest.mark.asyncio
async def test_persistent_connection_namespacing_closure_binding_and_close() -> None:
    client = FakeClient(names=["lookup", "a.b", "a_b"])
    manager = MCPManager(settings(), client_factory=lambda server, timeout: client)
    tools = await manager.start()
    assert len(tools) == 3 and len({tool.name for tool in tools}) == 3
    assert all(tool.risk == "external" and len(tool.name) <= 64 for tool in tools)
    assert client.entered == 1 and client.exited == 0
    for tool in tools:
        assert (await tool.handler({"q": "词"}))["content"][0]["text"] == "结果"
    assert [call["name"] for call in client.calls] == ["lookup", "a.b", "a_b"]
    assert (await manager.start()) == tools and client.entered == 1
    await manager.close()
    assert client.exited == 1 and client.closed == 1
    assert manager.status()[0]["status"] == "closed"
    assert (await tools[0].handler({"q": "词"}))["is_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,args,allowed",
    [
        ("npx", ["-y", "package"], False),
        ("uvx", ["package"], False),
        ("npx", ["--no-install", "package"], True),
        ("uvx", ["--offline", "package"], True),
    ],
)
async def test_package_runners_cannot_implicitly_download(
    command: str, args: list[str], allowed: bool
) -> None:
    calls: list[JsonObject] = []

    def factory(server: JsonObject, timeout: float) -> MCPClient:
        calls.append(server)
        return FakeClient()

    manager = MCPManager(
        {"mcpServers": {"local": {"enabled": True, "command": command, "args": args}}},
        client_factory=factory,
    )
    tools = await manager.start()
    assert bool(tools) == allowed and bool(calls) == allowed
    if not allowed:
        assert manager.status()[0]["status"] == "error"
    await manager.close()


@pytest.mark.asyncio
async def test_stdio_env_cwd_and_http_headers_pass_only_to_client() -> None:
    calls: list[JsonObject] = []

    def factory(server: JsonObject, timeout: float) -> MCPClient:
        calls.append(server)
        return FakeClient()

    config = {
        "mcpServers": {
            "local": {
                "enabled": True,
                "command": "python",
                "args": ["server.py"],
                "env": {"TOKEN": "env-secret"},
                "cwd": "/tmp",
            },
            "remote": {
                "enabled": True,
                "transport": "streamable-http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer bare-header-secret"},
            },
        }
    }
    manager = MCPManager(config, client_factory=factory)
    await manager.start()
    assert calls[0]["env"] == {"TOKEN": "env-secret"} and calls[0]["cwd"] == "/tmp"
    assert (
        calls[1]["headers"] == {"Authorization": "Bearer bare-header-secret"}
        and calls[1]["transport"] == "http"
    )
    assert "secret" not in json.dumps(manager.status())
    await manager.close()


@pytest.mark.asyncio
async def test_connection_error_masks_exception_text_and_cleans_up() -> None:
    client = FakeClient(failure=True)
    manager = MCPManager(
        settings(headers={"Authorization": "Bearer bare-header-secret"}),
        client_factory=lambda server, timeout: client,
    )
    assert await manager.start() == []
    assert manager.status()[0]["status"] == "error"
    assert "secret" not in json.dumps(manager.status())
    assert client.closed == 1
    await manager.close()


@pytest.mark.asyncio
async def test_missing_optional_dependency_becomes_status_error() -> None:
    def unavailable(server: JsonObject, timeout: float) -> MCPClient:
        raise ImportError("do not show local paths or secrets")

    manager = MCPManager(settings(), client_factory=unavailable)
    assert await manager.start() == []
    assert "可选" in manager.status()[0]["error"]
    assert "secrets" not in manager.status()[0]["error"]
    await manager.close()


@pytest.mark.asyncio
async def test_call_timeout_is_bounded_and_never_retries() -> None:
    client = FakeClient(slow=True)
    manager = MCPManager(
        settings(timeout=100), timeout=0.01, client_factory=lambda server, timeout: client
    )
    tools = await manager.start()
    result = await tools[0].handler({"q": "词"})
    assert result["is_error"] and "时间上限" in result["error"]
    assert len(client.calls) == 1 and client.calls[0]["timeout"] == 0.01
    await manager.close()


@pytest.mark.asyncio
async def test_call_cancellation_propagates() -> None:
    client = FakeClient(slow=True)
    manager = MCPManager(settings(), client_factory=lambda server, timeout: client)
    tools = await manager.start()
    task = asyncio.create_task(tools[0].handler({"q": "词"}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.calls) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_oversized_results_are_marked_and_media_payload_is_omitted() -> None:
    result = SimpleNamespace(
        is_error=False,
        content=[
            SimpleNamespace(type="text", text="x" * 2000),
            SimpleNamespace(type="image", mimeType="image/png", data="large-binary"),
        ],
        structured_content={"long": "x" * 2000},
    )
    manager = MCPManager(
        settings(),
        max_output_chars=256,
        client_factory=lambda server, timeout: FakeClient(result=result),
    )
    tools = await manager.start()
    output = await tools[0].handler({"q": "词"})
    assert output["truncated"]
    assert len(json.dumps(output, ensure_ascii=False)) <= 256
    assert "large-binary" not in json.dumps(output)
    await manager.close()


@pytest.mark.asyncio
async def test_remote_error_body_never_exposes_server_secrets() -> None:
    result = SimpleNamespace(
        is_error=True, content=[SimpleNamespace(type="text", text="bare-header-secret")]
    )
    manager = MCPManager(
        settings(), client_factory=lambda server, timeout: FakeClient(result=result)
    )
    output = await (await manager.start())[0].handler({"q": "词"})
    assert output["is_error"] and "secret" not in json.dumps(output)
    await manager.close()


@pytest.mark.asyncio
async def test_server_prefix_collisions_are_still_unique() -> None:
    config = {
        "mcpServers": {
            "a.b": {"enabled": True, "url": "https://example.test/mcp"},
            "a_b": {"enabled": True, "url": "https://example.test/mcp"},
        }
    }
    manager = MCPManager(config, client_factory=lambda server, timeout: FakeClient())
    tools = await manager.start()
    assert len(tools) == 2 and tools[0].name != tools[1].name
    await manager.close()


@pytest.mark.asyncio
async def test_real_fastmcp_in_process_client_lifecycle() -> None:
    fastmcp = pytest.importorskip("fastmcp")
    server = fastmcp.FastMCP("test")

    @server.tool
    def echo(value: str) -> str:
        return value

    client = fastmcp.Client(server)
    manager = MCPManager(settings(), client_factory=lambda spec, timeout: cast(MCPClient, client))
    tools = await asyncio.create_task(manager.start())
    assert len(tools) == 1 and client.is_connected()
    output = await tools[0].handler({"value": "真实本机协议"})
    assert not output["is_error"] and "真实本机协议" in json.dumps(output, ensure_ascii=False)
    await manager.close()
    assert not client.is_connected()


class PagedClient(FakeClient):
    def __init__(self, pages: list[tuple[list[str], str | None]]) -> None:
        super().__init__()
        self.pages = pages
        self.cursors: list[str | None] = []

    async def list_tools_page(self, cursor: str | None) -> ToolPage:
        index = len(self.cursors)
        self.cursors.append(cursor)
        self.names, next_cursor = self.pages[index]
        return ToolPage(await self.list_tools(), next_cursor)


async def test_complete_paginated_catalog_and_zero_tool_server() -> None:
    client = PagedClient([(["first"], "page-two"), (["last"], None)])
    manager = MCPManager(settings(), client_factory=lambda server, timeout: client)
    try:
        assert len(await manager.start()) == 2
        assert client.cursors == [None, "page-two"]
        candidate = await manager.prepare(settings())
        assert client.cursors == [None, "page-two"]
        assert len(candidate.tools) == 2
    finally:
        await manager.close()
    empty = MCPManager(settings(), client_factory=lambda server, timeout: FakeClient(names=[]))
    try:
        assert await empty.start() == []
        assert empty.status()[0]["status"] == "connected"
    finally:
        await empty.close()


@pytest.mark.parametrize(
    "pages,limits,code",
    [
        ([(["one"], "same"), (["two"], "same")], MCPLimits(), "pagination_stalled"),
        ([(["one"], "two")], MCPLimits(max_pages=1), "page_budget_exceeded"),
        ([(["one", "two"], None)], MCPLimits(max_tools=1), "catalog_budget_exceeded"),
        ([(["one"], None)], MCPLimits(max_catalog_bytes=1), "catalog_budget_exceeded"),
        ([(["one"], "two"), (["one"], None)], MCPLimits(), "catalog_invalid"),
    ],
)
async def test_incomplete_candidate_never_replaces_current_catalog(
    pages: list[tuple[list[str], str | None]], limits: MCPLimits, code: str
) -> None:
    original = FakeClient()
    replacement = PagedClient(pages)
    manager = MCPManager(
        settings(),
        client_factory=lambda server, timeout: (
            original if server["url"].endswith("/mcp") else replacement
        ),
    )
    try:
        old_tools = await manager.start()
        with pytest.raises(MCPPrepareError) as failed:
            await manager.prepare(settings(url="https://example.test/new"), limits=limits)
        assert failed.value.code == code
        assert manager.tools == old_tools
        assert original.closed == 0 and replacement.closed == 1
        assert not (await old_tools[0].handler({"q": "still available"}))["is_error"]
    finally:
        await manager.close()


async def test_failed_candidate_closes_only_new_connections_not_reused() -> None:
    old = FakeClient()
    new = FakeClient()
    failed = FakeClient(failure=True)
    clients = {"old": old, "new": new, "failed": failed}
    base = {"mcpServers": {"old": {"enabled": True, "url": "https://example.test/old"}}}
    manager = MCPManager(
        base, client_factory=lambda server, timeout: clients[server["url"].rsplit("/", 1)[-1]]
    )
    try:
        await manager.start()
        candidate_config = {
            "mcpServers": {
                name: {"enabled": True, "url": f"https://example.test/{name}"} for name in clients
            }
        }
        with pytest.raises(MCPPrepareError):
            await manager.prepare(candidate_config)
        assert old.entered == 1 and old.closed == 0
        assert new.closed == failed.closed == 1
        assert [item["name"] for item in manager.status()] == ["old"]
    finally:
        await manager.close()


async def test_changed_stdio_requires_restart_without_closing_active_process() -> None:
    client = FakeClient()
    config = settings(command="python", args=["installed.py"])
    config["mcpServers"]["test"].pop("url")
    manager = MCPManager(config, client_factory=lambda server, timeout: client)
    try:
        await manager.start()
        target = json.loads(json.dumps(config))
        target["mcpServers"]["test"]["args"] = ["changed.py"]
        with pytest.raises(MCPPrepareError, match="restart_required"):
            await manager.prepare(target)
        assert client.entered == 1 and client.closed == 0
        assert not (await manager.tools[0].handler({"q": "old"}))["is_error"]
    finally:
        await manager.close()


async def test_handlers_bind_the_validated_connection_until_retirement() -> None:
    old, new = FakeClient(names=["old"]), FakeClient(names=["new"])
    manager = MCPManager(
        settings(),
        client_factory=lambda server, timeout: old if server["url"].endswith("/mcp") else new,
    )
    try:
        old_tool = (await manager.start())[0]
        candidate = await manager.prepare(settings(url="https://example.test/new"))
        retired = manager.publish(candidate)
        assert not (await old_tool.handler({"q": "old generation"}))["is_error"]
        assert not (await manager.tools[0].handler({"q": "new generation"}))["is_error"]
        assert old.calls[0]["name"] == "old" and new.calls[0]["name"] == "new"
        await manager.retire(retired)
        assert (await old_tool.handler({"q": "retired"}))["is_error"]
        assert old.closed == 1 and new.closed == 0
    finally:
        await manager.close()


async def test_retirement_failure_is_reported_after_new_catalog_is_published() -> None:
    class FailingClose(FakeClient):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("private-fixture-value")

    old, new = FailingClose(), FakeClient(names=["new"])
    manager = MCPManager(
        settings(),
        client_factory=lambda server, timeout: old if server["url"].endswith("/mcp") else new,
    )
    try:
        await manager.start()
        candidate = await manager.prepare(settings(url="https://example.test/new"))
        await manager.retire(manager.publish(candidate))
        assert not (await manager.tools[0].handler({"q": "new"}))["is_error"]
        assert manager.retirement_errors == [
            {"name": "test", "error_code": "connection_close_failed", "stage": "retirement"}
        ]
        assert "private-fixture-value" not in json.dumps(manager.status())
    finally:
        await manager.close()


async def test_repeated_caller_cancellation_does_not_cancel_owned_cleanup() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedClose(FakeClient):
        async def close(self) -> None:
            entered.set()
            await release.wait()
            await super().close()

    old, new = GatedClose(), FakeClient(names=["new"])
    manager = MCPManager(
        settings(),
        client_factory=lambda server, timeout: old if server["url"].endswith("/mcp") else new,
    )
    retiring: asyncio.Task[bool] | None = None
    try:
        await manager.start()
        candidate = await manager.prepare(settings(url="https://example.test/new"))
        retiring = asyncio.create_task(manager.retire(manager.publish(candidate)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        retiring.cancel()
        await asyncio.sleep(0)
        retiring.cancel()
        await asyncio.sleep(0)
        assert not retiring.done() and len(manager._retiring) == 1
        connection = next(iter(manager._retiring))
        assert connection.cleanup_task is not None and not connection.cleanup_task.cancelled()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await retiring
        assert old.closed == old.exited == 1
        assert not manager._retiring
        assert not (await manager.tools[0].handler({"q": "new"}))["is_error"]
    finally:
        release.set()
        if retiring is not None:
            await asyncio.gather(retiring, return_exceptions=True)
        await manager.close()


async def test_failed_retirement_stays_owned_and_shutdown_reclaims_it() -> None:
    class FailOnce(FakeClient):
        async def close(self) -> None:
            await super().close()
            if self.closed == 1:
                raise RuntimeError("synthetic close failure")

    old, new = FailOnce(), FakeClient(names=["new"])
    manager = MCPManager(
        settings(),
        client_factory=lambda server, timeout: old if server["url"].endswith("/mcp") else new,
    )
    try:
        await manager.start()
        candidate = await manager.prepare(settings(url="https://example.test/new"))
        assert not await manager.retire(manager.publish(candidate))
        assert len(manager._retiring) == 1 and old.closed == 1
        with pytest.raises(MCPPrepareError, match="restart_required"):
            await manager.prepare(manager.config)
        assert not (await manager.tools[0].handler({"q": "new"}))["is_error"]
        await manager.close()
        assert old.closed == 2 and old.exited == 1 and new.closed == 1
        assert not manager._retiring
    finally:
        await manager.close()


async def test_locked_fastmcp_adapter_passes_sdk_cursor_without_single_page_helper() -> None:
    pytest.importorskip("mcp")
    params: list[Any] = []

    class Session:
        async def list_tools(self, **arguments: Any) -> Any:
            params.append(arguments["params"])
            return SimpleNamespace(tools=[], nextCursor=None)

    adapter = _FastMCPClient(SimpleNamespace(session=Session()))
    assert await adapter.list_tools_page("second-page") == ToolPage([])
    assert params[0].cursor == "second-page"


async def test_late_notifications_cannot_resurrect_retired_or_closed_connections() -> None:
    old, new = FakeClient(), FakeClient()
    notices: list[str] = []
    manager = MCPManager(
        settings(),
        client_factory=lambda server, timeout: old if server["url"].endswith("/mcp") else new,
    )
    manager.set_changed_callback(notices.append)
    try:
        await manager.start()
        previous = manager._connections["test"]
        manager._notify(previous)
        candidate = await manager.prepare(settings(url="https://example.test/new"))
        retired = manager.publish(candidate)
        manager._notify(previous)
        manager._notify(manager._connections["test"])
        assert notices == ["test", "test"]
        current = manager._connections["test"]
        await manager.retire(retired)
        await manager.close()
        manager._notify(current)
        assert notices == ["test", "test"]
    finally:
        await manager.close()
