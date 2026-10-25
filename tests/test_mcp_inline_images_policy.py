"""Finite manager-policy checks, separate from the real SDK/vision journey.

The clients return real SDK ImageContent values through the existing offline
client seam. Save callbacks only observe admission and entry; they do not claim
Runtime receipts, real attachment persistence or image pixel validity.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from test_mcp import settings
from test_mcp_resources import ResourceClient, listing, tool
from test_mcp_resources import offline as offline
from test_media import PNG

from muli.tools import Tool
from muli.tools.mcp import MCPLimits, MCPManager
from muli.tools.mcp_resources import MAX_RESOURCE_CONTENT_BLOCKS, ResourceSaveTarget

JsonObject = dict[str, Any]


def image(data: bytes = PNG, *, mime: str = "image/png", **fields: Any) -> Any:
    types = pytest.importorskip("mcp.types")
    return types.ImageContent(
        type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime, **fields
    )


def result(
    *blocks: Any, structured: JsonObject | None = None, failed: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(is_error=failed, content=list(blocks), structured_content=structured)


class ImageClient(ResourceClient):
    def __init__(self, *, resources: bool = False) -> None:
        super().__init__(tools=True, resources=resources)
        self.result = result(image())
        self.call_gate: Callable[[JsonObject], Awaitable[None]] | None = None

    async def call_tool(
        self, name: str, arguments: JsonObject, *, timeout: float, raise_on_error: bool
    ) -> Any:
        response = await super().call_tool(
            name, arguments, timeout=timeout, raise_on_error=raise_on_error
        )
        if self.call_gate is not None:
            await self.call_gate(arguments)
        return response


class Saves:
    def __init__(self, *, fail_after_entry: bool = False) -> None:
        self.preflights: list[str] = []
        self.prepared: list[tuple[bytes, str, int]] = []
        self.entered: list[bytes] = []
        self.fail_after_entry = fail_after_entry

    def target(self, name: str) -> ResourceSaveTarget:
        self.preflights.append(name)

        def prepare(data: bytes, mime: str, budget: int) -> Callable[[], Awaitable[JsonObject]]:
            self.prepared.append((data, mime, budget))

            async def save() -> JsonObject:
                self.entered.append(data)
                if self.fail_after_entry:
                    raise ValueError("private fixture save failure")
                return {"is_error": False, "saved": True}

            return save

        return ResourceSaveTarget(max_file_bytes=len(PNG) + 4096, prepare=prepare)


def remote_tool(manager: MCPManager, server: str = "test") -> Tool:
    return next(item for item in manager.tools if item.name.startswith(f"mcp_{server}_lookup_"))


def image_blocks(response: JsonObject) -> list[JsonObject]:
    return [
        block
        for block in response.get("content", [])
        if isinstance(block, dict) and block.get("type") == "image"
    ]


def assert_no_image_references(response: JsonObject) -> None:
    assert not any("ref" in block for block in image_blocks(response)), response


async def current_reference(manager: MCPManager, data: bytes = PNG) -> str:
    response = await remote_tool(manager).handler({"q": "建立本轮图片引用"})
    assert response.get("is_error") is False and not response.get("truncated", False)
    blocks = image_blocks(response)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["mime_type"] == "image/png" and block["size"] == len(data)
    assert block["untrusted"] is True and isinstance(block["ref"], str)
    assert block["ref"] and "data" not in block
    assert base64.b64encode(data).decode("ascii") not in json.dumps(response)
    return str(block["ref"])


async def saving(handler: Tool, reference: str, *, server: str = "test") -> JsonObject:
    return await handler.handler({"server": server, "ref": reference, "name": "selected.png"})


async def test_typed_image_group_requires_complete_valid_bounded_original_blocks() -> None:
    limits = MCPLimits(max_catalog_bytes=2048)
    client, saves = ImageClient(resources=True), Saves()
    manager = MCPManager(
        settings(), max_catalog_bytes=limits.max_catalog_bytes, client_factory=lambda *_: client
    )
    manager.set_resource_save_factory(saves.target)
    try:
        await manager.start()
        client.result = result(image(), image(PNG + b"second image"))
        response = await remote_tool(manager).handler({"q": "同次调用的两张真实typed图片"})
        blocks = image_blocks(response)
        assert response["is_error"] is False and len(blocks) == 2
        references = [block["ref"] for block in blocks]
        assert len(set(references)) == 2 and all(block["untrusted"] is True for block in blocks)
        assert [block["size"] for block in blocks] == [len(PNG), len(PNG + b"second image")]
        assert all(block["mime_type"] == "image/png" and "data" not in block for block in blocks)
        assert saves.preflights == [] and client.resource_cursors == [] and client.reads == []

        bad_base64 = image().model_copy(update={"data": "%%%"})
        non_ascii_base64 = image().model_copy(update={"data": "中文"})
        large = image(PNG + b"x" * 700)
        assert (
            len(json.dumps(large.model_dump(mode="json", by_alias=True)).encode())
            < limits.max_catalog_bytes
        )
        assert (
            len(json.dumps([large.model_dump(mode="json", by_alias=True)] * 2).encode())
            > limits.max_catalog_bytes
        )
        invalid_groups = [
            result(image(), bad_base64),
            result(image(), non_ascii_base64),
            result(image(), image(b"not a PNG signature")),
            result(image(), image(mime="image/jpeg")),
            result(image(), image(mime="image/svg+xml")),
            result(image(), image(extra="x" * (limits.max_catalog_bytes + 1))),
            result(large, large),
            result(*(image() for _ in range(MAX_RESOURCE_CONTENT_BLOCKS + 1))),
            result(image(), failed=True),
            result(image(), SimpleNamespace(type="text", text="x" * (limits.max_output_chars * 2))),
            result(
                SimpleNamespace(type="text", text=json.dumps(image().model_dump(mode="json"))),
                structured={"content": [image().model_dump(mode="json")]},
            ),
        ]
        save = tool(manager, "mcp_resource_save")
        for invalid in invalid_groups:
            client.result = invalid
            rejected = await remote_tool(manager).handler({"q": "整组拒绝，不能只授予第一图"})
            assert_no_image_references(rejected)
            assert (await saving(save, references[0]))["is_error"] is True
            assert saves.prepared == [] and saves.entered == []

        types = pytest.importorskip("mcp.types")
        combined = [
            large,
            types.ResourceLink(
                type="resource_link",
                uri="test:///combined",
                name="完整原文",
                description="x" * 1000,
            ),
        ]
        original = [block.model_dump(mode="json", by_alias=True) for block in combined]
        assert all(
            len(json.dumps(block, ensure_ascii=False).encode("utf-8")) < limits.max_catalog_bytes
            for block in original
        )
        assert (
            len(json.dumps(original, ensure_ascii=False).encode("utf-8")) > limits.max_catalog_bytes
        )
        client.result = result(*combined)
        rejected = await remote_tool(manager).handler({"q": "图片与原文链接共用同一整组预算"})
        assert_no_image_references(rejected)
        assert not rejected.get("resource_links_available", False)
        page = await listing(manager, source="tool_result")
        assert not any("ref" in item for item in page.get("resources", [])), page
        assert saves.prepared == [] and saves.entered == []
        assert client.resource_cursors == [] and client.reads == []
    finally:
        await manager.close()


async def test_tools_only_image_save_does_not_open_stdio_or_text_result_binary_save() -> None:
    client, local, saves = ImageClient(), ImageClient(resources=True), Saves()
    config = settings()
    config["mcpServers"]["local"] = {
        "enabled": True,
        "transport": "stdio",
        "command": "python",
        "args": [],
    }
    manager = MCPManager(
        config,
        client_factory=lambda server, _: local if server["transport"] == "stdio" else client,
    )
    manager.set_resource_save_factory(saves.target)
    reference = ""
    try:
        await manager.start()
        assert not {"mcp_resources_list", "mcp_resource_read"} & {
            item.name for item in manager.tools
        }
        reference = await current_reference(manager)
        save = tool(manager, "mcp_resource_save")
        assert save.risk == "external" and save.preserve_output is True
        assert_no_image_references(await remote_tool(manager, "local").handler({"q": "stdio图片"}))
        assert (await saving(save, reference, server="local"))["is_error"] is True
        assert saves.preflights == [] and saves.prepared == []
        assert (await saving(save, reference)) == {"is_error": False, "saved": True}
        assert saves.entered == [PNG] and saves.prepared[0][:2] == (PNG, "image/png")
        assert client.resource_cursors == local.resource_cursors == []
        assert client.reads == local.reads == []
    finally:
        await manager.close()

    # A fresh manager has no persisted inline-image cache to adopt.
    fresh, untouched = ImageClient(), Saves()
    reopened = MCPManager(settings(), client_factory=lambda *_: fresh)
    reopened.set_resource_save_factory(untouched.target)
    try:
        await reopened.start()
        assert (await saving(tool(reopened, "mcp_resource_save"), reference))["is_error"] is True
        assert untouched.prepared == [] and untouched.entered == [] and fresh.calls == []
        assert fresh.resource_cursors == [] and fresh.reads == []
    finally:
        await reopened.close()

    types = pytest.importorskip("mcp.types")
    text_client, text_saves = ImageClient(resources=True), Saves()
    text_client.result = result(
        types.ResourceLink(type="resource_link", uri="test:///only-text", name="仅原文")
    )
    text_manager = MCPManager(settings(), client_factory=lambda *_: text_client)
    text_manager.set_resource_save_factory(text_saves.target)
    try:
        await text_manager.start()
        assert (await remote_tool(text_manager).handler({"q": "真实typed文字链接"}))[
            "is_error"
        ] is False
        page = await listing(text_manager, source="tool_result")
        assert page["is_error"] is False and len(page["resources"]) == 1
        text_ref = page["resources"][0]["ref"]
        assert (await saving(tool(text_manager, "mcp_resource_save"), text_ref))["is_error"] is True
        assert (
            text_saves.preflights == [] and text_saves.prepared == [] and text_saves.entered == []
        )
        assert text_client.resource_cursors == [] and text_client.reads == []
    finally:
        await text_manager.close()


async def test_late_images_cannot_survive_new_failure_cancel_generation_receive_or_close() -> None:
    async def check_boundary(boundary: str) -> None:
        client, saves = ImageClient(), Saves()
        manager = MCPManager(settings(timeout=2), client_factory=lambda *_: client)
        manager.set_resource_save_factory(saves.target)
        first_entered, second_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        first: asyncio.Task[JsonObject] | None = None
        second: asyncio.Task[JsonObject] | None = None
        closing: asyncio.Task[None] | None = None

        async def hold(arguments: JsonObject) -> None:
            if arguments["q"] == "A":
                first_entered.set()
            else:
                second_entered.set()
                if boundary == "failure":
                    raise ValueError("newer RPC failed")
            await release.wait()

        try:
            await manager.start()
            reference = await current_reference(manager)
            remote, save = remote_tool(manager), tool(manager, "mcp_resource_save")
            original = manager._connections["test"]
            client.call_gate = hold
            first = asyncio.create_task(remote.handler({"q": "A"}))
            async with asyncio.timeout(2):
                await first_entered.wait()
            if boundary in {"failure", "cancel"}:
                second = asyncio.create_task(remote.handler({"q": "B"}))
                async with asyncio.timeout(2):
                    await second_entered.wait()
                if boundary == "cancel":
                    second.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await second
                else:
                    assert (await second)["is_error"] is True
            elif boundary == "generation":
                candidate = await manager.prepare(settings(timeout=2))
                assert manager.publish(candidate) == ()
                assert bool(manager._connections["test"] is original) and client.entered == 1
            elif boundary == "receive":
                # The existing receive latch is a controlled host fault here;
                # raw transport/parser rejection is covered by its own suite.
                original.receive.fail("mcp_receive_budget_exceeded")
            else:
                closing = asyncio.create_task(manager.close())
                async with asyncio.timeout(2):
                    while not manager._closed:
                        await asyncio.sleep(0)
                assert client.closed == 0 and not closing.done()
            assert (await saving(save, reference))["is_error"] is True
            assert saves.prepared == [] and saves.entered == []
            release.set()
            assert_no_image_references(await first)
            if closing is not None:
                await asyncio.wait_for(closing, 2)
            assert (await saving(save, reference))["is_error"] is True
            assert saves.prepared == [] and saves.entered == []
            assert client.resource_cursors == [] and client.reads == []
        finally:
            release.set()
            await asyncio.gather(
                *(operation for operation in (first, second, closing) if operation is not None),
                return_exceptions=True,
            )
            await manager.close()

    for boundary in ("failure", "cancel", "generation", "receive", "close"):
        await check_boundary(boundary)


async def test_host_image_ref_is_added_after_secret_redaction_and_still_selects_original() -> None:
    # Only the documented source tag is chosen; the digest remains opaque.
    secret = "image_result:"
    client, saves = ImageClient(), Saves()
    client.result = result(SimpleNamespace(type="text", text=f"private {secret} value"), image())
    manager = MCPManager(
        settings(headers={"X-Fixture-Secret": secret}), client_factory=lambda *_: client
    )
    manager.set_resource_save_factory(saves.target)
    try:
        await manager.start()
        response = await remote_tool(manager).handler({"q": "输出消毒与宿主引用碰撞"})
        assert response["is_error"] is False
        text = next(block["text"] for block in response["content"] if block["type"] == "text")
        assert secret not in text and "••••" in text
        blocks = image_blocks(response)
        assert len(blocks) == 1 and secret in blocks[0]["ref"]
        assert blocks[0]["untrusted"] is True and blocks[0]["size"] == len(PNG)
        assert base64.b64encode(PNG).decode("ascii") not in json.dumps(response)
        assert (await saving(tool(manager, "mcp_resource_save"), blocks[0]["ref"]))["saved"] is True
        assert saves.entered == [PNG] and saves.prepared[0][:2] == (PNG, "image/png")
        assert client.resource_cursors == [] and client.reads == []
    finally:
        await manager.close()


async def test_save_entry_failure_is_unknown_and_never_automatically_reentered() -> None:
    client, saves = ImageClient(), Saves(fail_after_entry=True)
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    manager.set_resource_save_factory(saves.target)
    try:
        await manager.start()
        reference = await current_reference(manager)
        response = await saving(tool(manager, "mcp_resource_save"), reference)
        assert response == {"is_error": True, "status": "unknown", "code": "resource_save_unknown"}
        await asyncio.sleep(0)
        assert len(saves.preflights) == len(saves.prepared) == len(saves.entered) == 1
        assert saves.entered == [PNG] and len(client.calls) == 1
        assert client.resource_cursors == [] and client.reads == []
    finally:
        await manager.close()
    assert len(saves.preflights) == len(saves.prepared) == len(saves.entered) == 1
