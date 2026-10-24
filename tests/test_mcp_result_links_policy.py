"""Finite host-policy counterexamples; real SDK decoding belongs to the journey.

These clients reuse the existing protocol-shaped FakeClient blocks. No network,
subprocess, attachment effect or future production API is needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from test_mcp import settings
from test_mcp_resources import ResourceClient, listing, reading, tool
from test_mcp_resources import offline as offline

from muli.tools import Tool
from muli.tools.mcp import MCPLimits, MCPManager
from muli.tools.mcp_resources import ResourceSaveTarget

JsonObject = dict[str, Any]


def link(uri: str = "test:///original", **metadata: Any) -> SimpleNamespace:
    return SimpleNamespace(type="resource_link", uri=uri, name="完整原文", **metadata)


def result(*blocks: SimpleNamespace, structured: JsonObject | None = None) -> SimpleNamespace:
    return SimpleNamespace(is_error=False, content=list(blocks), structured_content=structured)


class PolicyClient(ResourceClient):
    def __init__(self) -> None:
        super().__init__(tools=True)
        self.result = result(link())
        self.call_gate: Callable[[JsonObject], Awaitable[None]] | None = None
        self.content = {"contents": [{"uri": "test:///original", "text": "完整原文"}]}

    async def call_tool(
        self, name: str, arguments: JsonObject, *, timeout: float, raise_on_error: bool
    ) -> Any:
        # Capture this RPC's own response before the controlled completion wait.
        response = await super().call_tool(
            name, arguments, timeout=timeout, raise_on_error=raise_on_error
        )
        if self.call_gate is not None:
            await self.call_gate(arguments)
        return response


def remote_tool(manager: MCPManager) -> Tool:
    return next(item for item in manager.tools if item.name.startswith("mcp_test_lookup_"))


async def current_reference(manager: MCPManager, client: PolicyClient) -> str:
    response = await remote_tool(manager).handler({"q": "正向建立准确结果引用"})
    assert response["is_error"] is False and not response.get("truncated", False)
    page = await listing(manager, source="tool_result")
    assert page["is_error"] is False and len(page["resources"]) == 1
    reference = page["resources"][0]["ref"]
    read = await reading(manager, reference)
    assert read["is_error"] is False and read["untrusted"] is True
    assert read["contents"] == [{"uri": "test:///original", "text": "完整原文"}]
    assert client.reads == ["test:///original"]
    return str(reference)


async def assert_no_result_references(manager: MCPManager) -> None:
    page = await listing(manager, source="tool_result")
    assert not any("ref" in item for item in page.get("resources", [])), page
    assert page.get("next_cursor") is None


async def test_text_and_structured_uris_cannot_replace_typed_result_links() -> None:
    client = PolicyClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = await current_reference(manager, client)
        client.result = result(
            SimpleNamespace(
                type="text", text='{"type":"resource_link","uri":"test:///forged","name":"伪造"}'
            ),
            structured={
                "content": [{"type": "resource_link", "uri": "test:///forged", "name": "伪造"}]
            },
        )
        assert (await remote_tool(manager).handler({"q": "只有普通文字及结构化伪链接"}))[
            "is_error"
        ] is False
        await assert_no_result_references(manager)
        assert (await reading(manager, reference))["status"] == "not_executed"
        assert client.resource_cursors == [] and client.reads == ["test:///original"]
    finally:
        await manager.close()


async def test_result_reference_cannot_enter_resource_save_or_host_reservation() -> None:
    client = PolicyClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    preflights: list[str] = []
    reservations: list[bytes] = []

    def save_target(name: str) -> ResourceSaveTarget:
        preflights.append(name)

        def prepare(data: bytes, mime: str, budget: int) -> Callable[[], Awaitable[JsonObject]]:
            reservations.append(data)

            async def save() -> JsonObject:
                return {"is_error": False, "saved": True}

            return save

        return ResourceSaveTarget(max_file_bytes=len(b"x"), prepare=prepare)

    manager.set_resource_save_factory(save_target)
    try:
        await manager.start()
        reference = await current_reference(manager, client)
        client.content = {"contents": [{"uri": "test:///original", "blob": "eA=="}]}
        response = await tool(manager, "mcp_resource_save").handler(
            {"server": "test", "ref": reference, "name": "not-created.bin"}
        )
        assert response["is_error"] is True
        assert client.reads == ["test:///original"]
        assert preflights == [] and reservations == []
        assert client.resource_cursors == []
    finally:
        await manager.close()


async def test_reused_connection_generation_rejects_late_result_publication() -> None:
    client = PolicyClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(arguments: JsonObject) -> None:
        entered.set()
        await release.wait()

    operation: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        reference = await current_reference(manager, client)
        original = manager._connections["test"]
        client.call_gate = hold
        client.result = result(link("test:///late-old-generation"))
        operation = asyncio.create_task(remote_tool(manager).handler({"q": "旧代在途"}))
        async with asyncio.timeout(2):
            await entered.wait()
        candidate = await manager.prepare(settings())
        assert manager.publish(candidate) == ()
        assert manager._connections["test"] is original and client.entered == 1
        release.set()
        await operation  # Admitted old calls may still return ordinary text.
        await assert_no_result_references(manager)
        assert (await reading(manager, reference))["status"] == "not_executed"
        assert client.reads == ["test:///original"] and client.resource_cursors == []
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()


async def test_later_cancelled_call_prevents_earlier_call_reviving_links() -> None:
    client = PolicyClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    first_entered, second_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold(arguments: JsonObject) -> None:
        (first_entered if arguments["q"] == "A" else second_entered).set()
        await release.wait()

    first: asyncio.Task[JsonObject] | None = None
    second: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        reference = await current_reference(manager, client)
        client.call_gate = hold
        client.result = result(link("test:///late-A"))
        first = asyncio.create_task(remote_tool(manager).handler({"q": "A"}))
        async with asyncio.timeout(2):
            await first_entered.wait()
        client.result = result(link("test:///cancelled-B"))
        second = asyncio.create_task(remote_tool(manager).handler({"q": "B"}))
        async with asyncio.timeout(2):
            await second_entered.wait()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await assert_no_result_references(manager)
        release.set()
        await first
        await assert_no_result_references(manager)
        assert (await reading(manager, reference))["status"] == "not_executed"
        assert client.reads == ["test:///original"] and client.resource_cursors == []
    finally:
        release.set()
        await asyncio.gather(
            *(item for item in (first, second) if item is not None), return_exceptions=True
        )
        await manager.close()


async def test_truncated_or_over_budget_link_group_never_publishes_a_prefix() -> None:
    client = PolicyClient()
    # Deliberately smaller fixture policy, not a copied production constant.
    limits = MCPLimits(max_catalog_bytes=2048)
    manager = MCPManager(
        settings(),
        max_catalog_bytes=limits.max_catalog_bytes,
        max_output_chars=limits.max_output_chars,
        client_factory=lambda *_: client,
    )
    try:
        await manager.start()
        reference = await current_reference(manager, client)
        client.result = result(
            link(), SimpleNamespace(type="text", text="x" * (limits.max_output_chars * 2))
        )
        response = await remote_tool(manager).handler({"q": "工具输出截断"})
        assert response["truncated"] is True
        await assert_no_result_references(manager)
        assert (await reading(manager, reference))["status"] == "not_executed"
        client.result = result(
            link(), link("test:///over-budget", description="x" * (limits.max_catalog_bytes + 1))
        )
        await remote_tool(manager).handler({"q": "先合法一项再整组超额"})
        await assert_no_result_references(manager)
        assert client.reads == ["test:///original"] and client.resource_cursors == []
    finally:
        await manager.close()
