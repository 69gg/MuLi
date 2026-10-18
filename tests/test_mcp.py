from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace, TracebackType
from typing import Any, Self, cast

import pytest

from muli.tools.mcp import MCPClient, MCPManager

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
