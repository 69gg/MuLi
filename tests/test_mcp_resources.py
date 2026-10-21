"""Resource discovery uses only finite fake clients; no network or process."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import Mock

import httpx
import pytest
from test_mcp import FakeClient, settings

import muli.tools.mcp as module
from muli.tools import Tool
from muli.tools.mcp import MCPLimits, MCPManager
from muli.tools.mcp_resources import (
    MAX_RESOURCE_CONTENT_BLOCKS,
    MAX_RESOURCE_ITEMS,
    MAX_RESOURCE_URI_BYTES,
    Capabilities,
)

JsonObject = dict[str, Any]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("resource tests cannot use network or subprocesses")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


class ResourceClient(FakeClient):
    def __init__(self, *, tools: bool = False, resources: bool = True) -> None:
        super().__init__()
        self.capabilities = Capabilities(tools, resources)
        self.pages: list[JsonObject] = [{"resources": []}]
        self.resource_cursors: list[str | None] = []
        self.page_index = 0
        self.page_gate: Callable[[], Awaitable[None]] | None = None
        self.reads: list[str] = []
        self.content: JsonObject = {"contents": []}
        self.read_gate: Callable[[], Awaitable[None]] | None = None

    def server_capabilities(self) -> Capabilities:
        return self.capabilities

    async def list_tools(self) -> list[Any]:
        assert self.capabilities.tools, "a resources-only server must not receive tools/list"
        return await super().list_tools()

    async def list_resources_page(self, cursor: str | None) -> JsonObject:
        if cursor is None:
            self.page_index = 0
        index = self.page_index
        self.page_index += 1
        self.resource_cursors.append(cursor)
        if self.page_gate is not None:
            await self.page_gate()
        return self.pages[index]

    async def read_resource(self, uri: Any) -> JsonObject:
        self.reads.append(str(uri))
        if self.read_gate is not None:
            await self.read_gate()
        return self.content


async def test_resources_only_server_publishes_two_host_tools_without_prefetch() -> None:
    client = ResourceClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        tools = await manager.start()
        assert {tool.name for tool in tools} == {"mcp_resources_list", "mcp_resource_read"}
        assert all(tool.risk == "external" and tool.preserve_output for tool in tools)
        assert client.resource_cursors == [] and client.reads == []
        assert manager.status()[0]["status"] == "connected"
    finally:
        await manager.close()
    assert client.closed == 1


def resource(index: int = 0, **fields: Any) -> JsonObject:
    return {"uri": f"test:///resource-{index}", "name": f"资源-{index}", **fields}


def tool(manager: MCPManager, name: str) -> Tool:
    return next(item for item in manager.tools if item.name == name)


async def listing(manager: MCPManager, **arguments: Any) -> JsonObject:
    return await tool(manager, "mcp_resources_list").handler({"server": "test", **arguments})


async def reading(manager: MCPManager, reference: str) -> JsonObject:
    return await tool(manager, "mcp_resource_read").handler({"server": "test", "ref": reference})


@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_reused_resource_only_server_skips_tool_refresh_and_accepts_empty_capability(
    transport: str,
) -> None:
    client = ResourceClient()
    manager = MCPManager(settings(transport=transport), client_factory=lambda *_: client)
    try:
        await manager.start()
        candidate = await manager.prepare(settings(transport=transport), refresh={"test"})
        assert manager.publish(candidate) == () and client.entered == 1
        result = await listing(manager)
        assert result["catalog_complete"] and result["resources"] == []
        assert result["next_cursor"] is None and result["is_error"] is False
        assert client.resource_cursors == [None] and client.reads == []
    finally:
        await manager.close()


@pytest.mark.parametrize("kind", ["legacy", "tools-only", "stdio", "disabled"])
async def test_resource_tools_require_declared_guarded_enabled_connection(kind: str) -> None:
    client = (
        FakeClient()
        if kind == "legacy"
        else ResourceClient(tools=True, resources=kind != "tools-only")
    )
    config = settings()
    if kind == "stdio":
        config = settings(transport="stdio", command="python", args=[])
        config["mcpServers"]["test"].pop("url")
    elif kind == "disabled":
        config["mcpServers"]["test"]["enabled"] = False
    manager = MCPManager(config, client_factory=lambda *_: client)
    try:
        await manager.start()
        assert "mcp_resources_list" not in {item.name for item in manager.tools}
        assert len(manager.tools) == (0 if kind == "disabled" else 1)
    finally:
        await manager.close()


async def test_host_tools_do_not_consume_remote_catalog_limit_or_leak_markers() -> None:
    client = ResourceClient(tools=True)
    manager = MCPManager(settings(), max_tools=1, client_factory=lambda *_: client)
    try:
        await manager.start()
        assert len(manager.tools) == 3 and manager.status()[0]["tool_count"] == 1
        for item in manager.tools:
            assert "preserve_output" not in json.dumps(item.schema())
        assert tool(manager, "mcp_resources_list").parameters["properties"]["server"]["enum"] == [
            "test"
        ]
    finally:
        await manager.close()


async def test_complete_catalog_uses_opaque_empty_cursor_and_projects_unknown_fields() -> None:
    client = ResourceClient()
    client.pages = [
        {"resources": [], "nextCursor": "", "_meta": {"unknown": "charged"}},
        {"resources": [resource(title="标题", mimeType="text/plain", size=1, evil="ignored")]},
    ]
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        result = await listing(manager)
        assert result["catalog_complete"] and client.resource_cursors == [None, ""]
        item = result["resources"][0]
        assert item.keys() == {"uri", "name", "title", "mime_type", "size", "ref"}
        assert "unknown" not in json.dumps(result) and "evil" not in json.dumps(result)
        assert client.reads == []
    finally:
        await manager.close()


@pytest.mark.parametrize(
    ("pages", "options", "code"),
    [
        ([{"resources": [resource(), resource()]}], {}, "resource_duplicate_uri"),
        ([{"resources": [], "nextCursor": ""}] * 2, {}, "resource_pagination_stalled"),
        (
            [{"resources": [], "nextCursor": "next"}],
            {"max_pages": 1},
            "resource_page_budget_exceeded",
        ),
        (
            [{"resources": [resource(i) for i in range(MAX_RESOURCE_ITEMS + 1)]}],
            {},
            "resource_catalog_budget_exceeded",
        ),
        (
            [{"resources": [], "_meta": "large" * 100}],
            {"max_catalog_bytes": 128},
            "resource_catalog_budget_exceeded",
        ),
        (
            [{"resources": [resource(uri="x:" + "a" * MAX_RESOURCE_URI_BYTES)]}],
            {},
            "resource_invalid",
        ),
        ([{"resources": [resource(size=True)]}], {}, "resource_invalid"),
    ],
    ids=[
        "duplicate-uri",
        "repeat-empty",
        "page-budget",
        "item-budget",
        "extra-budget",
        "uri-budget",
        "invalid-size",
    ],
)
async def test_catalog_failure_never_publishes_a_prefix_or_breaks_tools(
    pages: list[JsonObject], options: JsonObject, code: str
) -> None:
    client = ResourceClient(tools=True)
    manager = MCPManager(settings(), client_factory=lambda *_: client, **options)
    try:
        await manager.start()
        before = await listing(manager)
        previous = manager._connections["test"].resource_catalog
        client.pages = pages
        result = await listing(manager)
        assert result == {"is_error": True, "code": code}
        assert manager._connections["test"].resource_catalog is previous
        assert "resources" not in result and "next_cursor" not in result
        assert before["catalog_complete"]
        remote = next(item for item in manager.tools if item.name.startswith("mcp_test_lookup_"))
        assert (await remote.handler({"q": "still usable"}))["is_error"] is False
    finally:
        await manager.close()


async def test_page_budget_counts_all_json_bytes_exactly() -> None:
    client = ResourceClient()
    page: JsonObject = {"resources": [resource()], "_meta": "说明🙂"}
    size = len(json.dumps(page, ensure_ascii=False).encode())
    manager = MCPManager(settings(), max_catalog_bytes=size, client_factory=lambda *_: client)
    try:
        await manager.start()
        client.pages = [page]
        assert (await listing(manager))["is_error"] is False
        previous = manager._connections["test"].resource_catalog
        page["_meta"] += "x"
        assert (await listing(manager))["code"] == "resource_catalog_budget_exceeded"
        assert manager._connections["test"].resource_catalog is previous
    finally:
        await manager.close()


async def test_host_wrapped_json_exact_output_boundary_is_checked_after_redaction() -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource(description='密钥🙂"\\' * 30)]}]
    config = settings(headers={"X-Key": "密钥"})
    manager = MCPManager(config, client_factory=lambda *_: client)
    try:
        await manager.start()
        first = await listing(manager)
        maximum = len(json.dumps(first, ensure_ascii=False))
        assert "密钥" not in json.dumps(first, ensure_ascii=False)
        candidate = await manager.prepare(config, limits=MCPLimits(max_output_chars=maximum))
        manager.publish(candidate)
        exact = await listing(manager)
        assert exact["is_error"] is False and len(json.dumps(exact, ensure_ascii=False)) == maximum
        candidate = await manager.prepare(config, limits=MCPLimits(max_output_chars=maximum - 1))
        manager.publish(candidate)
        assert (await listing(manager))["code"] == "resource_output_budget_exceeded"
        assert manager._connections["test"].resource_catalog is None
    finally:
        await manager.close()


@pytest.mark.parametrize("secret", ["text", "name", "uri"])
async def test_resource_redaction_preserves_host_fields_and_original_read_uri(secret: str) -> None:
    client = ResourceClient()
    uri = "test:///name-text-uri"
    text = "name uri text"
    client.pages = [{"resources": [resource(uri=uri, name=text, description=text)]}]
    client.content = {"contents": [{"uri": uri, "text": text, "mimeType": "text/plain"}]}
    manager = MCPManager(settings(headers={"X-Key": secret}), client_factory=lambda *_: client)
    try:
        await manager.start()
        page = await listing(manager)
        item = page["resources"][0]
        assert item.keys() == {"uri", "name", "description", "ref"}
        assert item["uri"] == uri.replace(secret, "••••")
        assert item["name"] == item["description"] == text.replace(secret, "••••")
        result = await reading(manager, item["ref"])
        assert result["ref"] == item["ref"] and result["is_error"] is False
        block = result["contents"][0]
        assert block.keys() == {"uri", "text", "mime_type"}
        assert block["uri"] == uri.replace(secret, "••••")
        assert block["text"] == text.replace(secret, "••••")
        assert block["mime_type"] == "text/plain".replace(secret, "••••")
        assert client.reads == [uri]
    finally:
        await manager.close()


async def test_entered_read_failure_is_not_reported_as_unexecuted_or_implicitly_retried() -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource()]}]

    async def failing() -> None:
        raise RuntimeError("remote secret detail")

    client.read_gate = failing
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = (await listing(manager))["resources"][0]["ref"]
        result = await reading(manager, reference)
        assert result == {"is_error": True, "code": "resource_unavailable"}
        assert len(client.reads) == 1 and manager._connections["test"].active_calls == 0
    finally:
        await manager.close()


async def test_cancelled_read_settles_original_lease_without_an_automatic_second_request() -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource()]}]
    entered = asyncio.Event()

    async def hold() -> None:
        entered.set()
        await asyncio.Event().wait()

    client.read_gate = hold
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    operation: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        reference = (await listing(manager))["resources"][0]["ref"]
        operation = asyncio.create_task(reading(manager, reference))
        async with asyncio.timeout(2):
            await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        connection = manager._connections["test"]
        assert connection.active_calls == 0 and connection.idle.is_set()
        assert len(client.reads) == 1
    finally:
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()
    assert client.closed == 1


async def test_local_pages_only_authorize_shown_refs_and_have_bounded_stable_cursors() -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource(i, description='引号"🙂\\' * 10) for i in range(7)]}]
    manager = MCPManager(settings(), max_output_chars=550, client_factory=lambda *_: client)
    try:
        await manager.start()
        first = result = await listing(manager)
        catalog = manager._connections["test"].resource_catalog
        assert catalog is not None and first["next_cursor"] is not None
        assert len(catalog.references) == len(first["resources"]) < len(catalog.items)
        hidden = catalog.token("ref", len(catalog.items) - 1)
        assert (await reading(manager, hidden))["status"] == "not_executed" and client.reads == []
        complete: list[JsonObject] = []
        while True:
            assert len(json.dumps(result, ensure_ascii=False)) <= 550
            complete.extend(result["resources"])
            if result["next_cursor"] is None:
                break
            cursor = result["next_cursor"]
            result = await listing(manager, cursor=cursor)
            repeated = await listing(manager, cursor=cursor)
            assert result == repeated
        assert [item["uri"] for item in complete] == [resource(i)["uri"] for i in range(7)]
        assert len({item["ref"] for item in complete}) == len(catalog.references) == 7
        assert len(catalog.cursors) < 7 and client.resource_cursors == [None]
        assert (await listing(manager, cursor="remote-or-forged"))["status"] == "not_executed"
        assert client.resource_cursors == [None]
    finally:
        await manager.close()


@pytest.mark.parametrize("contents", [[], [{"uri": "file:///unused", "text": ""}]])
async def test_read_returns_complete_empty_text_without_opening_uri(
    contents: list[JsonObject],
) -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource(uri="file:///does-not-exist/secret")]}]
    client.content = {"contents": contents, "_meta": {"instructions": "ignored"}}
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = (await listing(manager))["resources"][0]["ref"]
        result = await reading(manager, reference)
        assert result["is_error"] is False and result["untrusted"] is True
        assert result["contents"] == contents and "_meta" not in result
        assert client.reads == ["file:///does-not-exist/secret"]
    finally:
        await manager.close()


@pytest.mark.parametrize(
    ("contents", "code"),
    [
        ([{"uri": "test:///blob", "blob": "AA=="}], "resource_content_unsupported"),
        (
            [{"uri": "test:///mixed", "text": "partial", "blob": "AA=="}],
            "resource_content_unsupported",
        ),
        ([{"uri": "test:///invalid", "text": 1}], "resource_invalid"),
        (
            [{"uri": "test:///many", "text": "text"}] * (MAX_RESOURCE_CONTENT_BLOCKS + 1),
            "resource_content_budget_exceeded",
        ),
        ([{"uri": "test:///large", "text": '🙂"\\' * 1000}], "resource_output_budget_exceeded"),
    ],
    ids=["blob", "mixed", "type", "blocks", "output"],
)
async def test_read_never_returns_a_text_subset_or_truncated_json(
    contents: list[JsonObject], code: str
) -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource()]}]
    client.content = {"contents": contents}
    manager = MCPManager(settings(), max_output_chars=550, client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = (await listing(manager))["resources"][0]["ref"]
        result = await reading(manager, reference)
        assert result == {"is_error": True, "code": code} and len(client.reads) == 1
        assert "partial" not in json.dumps(result) and "preview" not in result
        assert manager.status()[0]["status"] == "connected"
    finally:
        await manager.close()


async def test_failed_or_cancelled_refresh_preserves_catalog_and_allows_explicit_retry() -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource()]}]
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    operation: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        first = await listing(manager)
        previous = manager._connections["test"].resource_catalog
        client.page_gate = hold
        operation = asyncio.create_task(listing(manager))
        async with asyncio.timeout(2):
            await entered.wait()
        assert (await listing(manager))["code"] == "resource_catalog_busy"
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert manager._connections["test"].resource_catalog is previous
        assert (await reading(manager, first["resources"][0]["ref"]))["is_error"] is False
        client.page_gate = None
        refreshed = await listing(manager)
        assert refreshed["resources"][0]["ref"] != first["resources"][0]["ref"]
        assert (await reading(manager, first["resources"][0]["ref"]))["status"] == "not_executed"
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()


async def test_all_remote_pages_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ResourceClient()
    client.pages = [
        {"resources": [], "nextCursor": "a"},
        {"resources": [], "nextCursor": "b"},
        {"resources": []},
    ]
    manager = MCPManager(settings(), timeout=1, client_factory=lambda *_: client)
    now = asyncio.get_running_loop().time()
    clock = SimpleNamespace(time=lambda: now)
    proxy = Mock(wraps=asyncio)
    proxy.get_running_loop.return_value = clock

    async def advance() -> None:
        nonlocal now
        now += 0.6

    try:
        await manager.start()
        client.page_gate = advance
        monkeypatch.setattr(module, "asyncio", proxy)
        assert (await listing(manager))["code"] == "resource_timeout"
        assert client.resource_cursors == [None, "a"]
        assert manager._connections["test"].resource_catalog is None
    finally:
        await manager.close()


async def test_publish_reuse_invalidates_refs_and_late_catalog_cannot_publish() -> None:
    client = ResourceClient()
    client.pages = [{"resources": [resource()]}]
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    operation: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        first = await listing(manager)
        old_handler = tool(manager, "mcp_resource_read")
        client.page_gate = hold
        operation = asyncio.create_task(listing(manager))
        async with asyncio.timeout(2):
            await entered.wait()
        candidate = await manager.prepare(settings())
        assert manager.publish(candidate) == () and client.entered == 1
        release.set()
        assert await operation == {"is_error": True, "code": "resource_stale"}
        assert manager._connections["test"].resource_catalog is None
        assert (await old_handler.handler({"server": "test", "ref": first["resources"][0]["ref"]}))[
            "code"
        ] == "generation_changed"
        assert (await reading(manager, first["resources"][0]["ref"]))["status"] == "not_executed"
        assert client.reads == []
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()


async def test_admitted_read_keeps_old_secrets_limits_and_connection_until_settled() -> None:
    old, new = ResourceClient(), ResourceClient()
    old.pages = [{"resources": [resource()]}]
    old.content = {"contents": [{"uri": "test:///subresource", "text": "old-secret" + "文" * 500}]}
    clients = iter([old, new])
    manager = MCPManager(
        settings(headers={"X-Key": "old-secret"}),
        max_output_chars=2000,
        client_factory=lambda *_: next(clients),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    pending: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        reference = (await listing(manager))["resources"][0]["ref"]
        old.read_gate = hold
        pending = asyncio.create_task(reading(manager, reference))
        async with asyncio.timeout(2):
            await entered.wait()
        candidate = await manager.prepare(
            settings(url="https://example.test/new"), limits=MCPLimits(max_output_chars=128)
        )
        retired = manager.publish(candidate)
        assert manager.draining == 1 and old.closed == 0
        release.set()
        result = await pending
        assert result["contents"][0]["text"] == "••••" + "文" * 500
        assert "old-secret" not in json.dumps(result) and len(old.reads) == 1
        assert await manager.retire(retired) and old.closed == 1 and not new.closed
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await manager.close()


async def test_receive_latch_blocks_both_tools_and_resources_without_retry() -> None:
    client = ResourceClient(tools=True)
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        handlers = list(manager.tools)
        connection = manager._connections["test"]
        connection.receive.fail("mcp_receive_budget_exceeded")
        for item in handlers:
            arguments = (
                {"q": "test"}
                if "lookup" in item.name
                else {
                    "server": "test",
                    **({"ref": "not-read"} if item.name.endswith("read") else {}),
                }
            )
            result = await item.handler(arguments)
            assert (
                result["status"] == "not_executed"
                and result["code"] == "mcp_receive_budget_exceeded"
            )
        assert client.calls == client.reads == client.resource_cursors == []
        await manager.retire((connection,))
    finally:
        await manager.close()
