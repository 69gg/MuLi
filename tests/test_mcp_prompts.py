"""Finite prompt peers; no server, subprocess or network access."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from test_mcp import settings
from test_mcp_resources import ResourceClient, resource, tool
from test_mcp_resources import offline as offline

from muli.tools.mcp import MCPManager, _name
from muli.tools.mcp_resources import Capabilities

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class PromptCapabilities(Capabilities):
    prompts: bool = True


class PromptClient(ResourceClient):
    def __init__(self, *, tools: bool = False, resources: bool = False) -> None:
        super().__init__(tools=tools, resources=resources)
        self.capabilities = PromptCapabilities(tools, resources)
        self.prompt_pages: list[JsonObject] = [{"prompts": []}]
        self.prompt_cursors: list[str | None] = []
        self.prompt_index = 0
        self.prompt_gate: Callable[[], Awaitable[None]] | None = None
        self.gets: list[tuple[str, dict[str, str] | None]] = []
        self.draft: JsonObject = {"messages": []}
        self.get_gate: Callable[[], Awaitable[None]] | None = None

    async def list_prompts_page(self, cursor: str | None) -> JsonObject:
        if cursor is None:
            self.prompt_index = 0
        index = self.prompt_index
        self.prompt_index += 1
        self.prompt_cursors.append(cursor)
        if self.prompt_gate is not None:
            await self.prompt_gate()
        return self.prompt_pages[index]

    async def get_prompt(self, name: str, arguments: dict[str, str] | None) -> JsonObject:
        self.gets.append((name, arguments))
        if self.get_gate is not None:
            await self.get_gate()
        return self.draft


async def test_prompt_only_server_publishes_explicit_tools_without_prefetch() -> None:
    client = PromptClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        tools = await manager.start()
        assert {item.name for item in tools} == {"mcp_prompts_list", "mcp_prompt_get"}
        assert all(item.risk == "external" and item.preserve_output for item in tools)
        assert client.prompt_cursors == client.gets == client.resource_cursors == client.reads == []
        assert manager.status()[0]["status"] == "connected"
    finally:
        await manager.close()
    assert client.closed == 1


def prompt(name: str = "draft", **fields: Any) -> JsonObject:
    return {"name": name, "description": "非可信草稿", **fields}


async def listing(manager: MCPManager, **arguments: Any) -> JsonObject:
    return await tool(manager, "mcp_prompts_list").handler({"server": "test", **arguments})


async def getting(manager: MCPManager, reference: str, **arguments: Any) -> JsonObject:
    return await tool(manager, "mcp_prompt_get").handler(
        {"server": "test", "ref": reference, **arguments}
    )


async def test_empty_cursor_unknown_fields_empty_names_and_argument_contract_roundtrip() -> None:
    client = PromptClient()
    client.prompt_pages = [
        {"prompts": [], "nextCursor": "", "_meta": {"ignored": True}},
        {"prompts": [prompt("", arguments=[{"name": "", "required": True, "unknown": "drop"}])]},
    ]
    client.draft = {
        "description": "资料",
        "messages": [
            {"role": "assistant", "content": {"type": "text", "text": "不执行", "_meta": "drop"}},
            {"role": "user", "content": {"type": "text", "text": ""}},
        ],
        "unknown": "drop",
    }
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        page = await listing(manager)
        assert client.prompt_cursors == [None, ""] and page["catalog_complete"]
        item = page["prompts"][0]
        assert item["name"] == "" and item["arguments"] == [{"name": "", "required": True}]
        assert (await getting(manager, item["ref"]))["code"] == "prompt_arguments_invalid"
        result = await getting(manager, item["ref"], arguments={"": ""})
        assert result == {
            "is_error": False,
            "server": "test",
            "ref": item["ref"],
            "untrusted": True,
            "description": "资料",
            "messages": [{"role": "assistant", "text": "不执行"}, {"role": "user", "text": ""}],
        }
        assert client.gets == [("", {"": ""})]
        assert "unknown" not in json.dumps(page) and "drop" not in json.dumps(result)
    finally:
        await manager.close()


@pytest.mark.parametrize("kind", ["tools", "resources", "legacy", "stdio", "disabled"])
async def test_missing_or_unguarded_prompt_capability_never_publishes_prompt_tools(
    kind: str,
) -> None:
    from test_mcp import FakeClient

    client = (
        FakeClient()
        if kind == "legacy"
        else ResourceClient(tools=kind == "tools")
        if kind in {"tools", "resources"}
        else PromptClient()
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
        assert not any(item.name.startswith("mcp_prompt") for item in manager.tools)
    finally:
        await manager.close()


async def test_claimed_prompt_capability_requires_its_optional_adapter_methods() -> None:
    client = ResourceClient(tools=False, resources=False)
    client.capabilities = Capabilities(False, False, True)
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        assert await manager.start() == []
        assert manager.status()[0]["error_code"] == "capabilities_invalid"
        assert client.resource_cursors == [] and client.reads == []
        assert client.closed == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("remote_tools", [False, True])
async def test_reuse_respects_tools_capability_and_all_capabilities_can_coexist(
    remote_tools: bool,
) -> None:
    client = PromptClient(tools=remote_tools, resources=True)
    client.pages = [{"resources": [resource()]}]
    client.prompt_pages = [{"prompts": [prompt()]}]
    manager = MCPManager(settings(), max_tools=1, client_factory=lambda *_: client)
    try:
        await manager.start()
        candidate = await manager.prepare(settings(), refresh={"test"})
        assert manager.publish(candidate) == () and client.entered == 1
        assert manager.status()[0]["prompts_supported"] is True
        names = {item.name for item in manager.tools}
        assert names == {
            "mcp_resources_list",
            "mcp_resource_read",
            "mcp_prompts_list",
            "mcp_prompt_get",
        } | ({_name("test", "lookup")} if remote_tools else set())
        resource_page = await tool(manager, "mcp_resources_list").handler({"server": "test"})
        page = await listing(manager)
        assert (await getting(manager, page["prompts"][0]["ref"]))["is_error"] is False
        assert (
            await tool(manager, "mcp_resource_read").handler(
                {"server": "test", "ref": resource_page["resources"][0]["ref"]}
            )
        )["is_error"] is False
        assert (await getting(manager, resource_page["resources"][0]["ref"]))[
            "status"
        ] == "not_executed"
        assert all("preserve_output" not in json.dumps(item.schema()) for item in manager.tools)
    finally:
        await manager.close()


@pytest.mark.parametrize(
    ("pages", "options", "code"),
    [
        ([{"prompts": [prompt(), prompt()]}], {}, "prompt_duplicate_name"),
        ([{"prompts": [], "nextCursor": ""}] * 2, {}, "prompt_pagination_stalled"),
        ([{"prompts": [], "nextCursor": "next"}], {"max_pages": 1}, "prompt_page_budget_exceeded"),
        ([{"prompts": [prompt(str(i)) for i in range(257)]}], {}, "prompt_catalog_budget_exceeded"),
        (
            [{"prompts": [], "unknown": "x" * 300}],
            {"max_catalog_bytes": 128},
            "prompt_catalog_budget_exceeded",
        ),
        (
            [{"prompts": [prompt(arguments=[{"name": str(i)} for i in range(33)])]}],
            {},
            "prompt_catalog_budget_exceeded",
        ),
        ([{"prompts": [prompt(arguments=[{"name": "a"}, {"name": "a"}])]}], {}, "prompt_invalid"),
        (
            [{"prompts": [prompt(arguments=[{"name": "a", "required": "yes"}])]}],
            {},
            "prompt_invalid",
        ),
    ],
    ids=[
        "duplicate",
        "repeat-empty",
        "pages",
        "items",
        "extra-bytes",
        "arguments",
        "duplicate-argument",
        "invalid-required",
    ],
)
async def test_bad_directory_keeps_previous_complete_prompt_and_resource_cache(
    pages: list[JsonObject], options: JsonObject, code: str
) -> None:
    client = PromptClient(resources=True)
    client.prompt_pages = [{"prompts": [prompt()]}]
    manager = MCPManager(settings(), client_factory=lambda *_: client, **options)
    try:
        await manager.start()
        before = await listing(manager)
        previous = manager._connections["test"].prompt_catalog
        client.prompt_pages = pages
        assert await listing(manager) == {"is_error": True, "code": code}
        assert manager._connections["test"].prompt_catalog is previous
        assert (await getting(manager, before["prompts"][0]["ref"]))["is_error"] is False
        assert manager.status()[0]["status"] == "connected"
    finally:
        await manager.close()


async def test_local_pages_keep_complete_argument_contracts_and_only_issued_refs() -> None:
    client = PromptClient()
    client.prompt_pages = [
        {
            "prompts": [
                prompt(str(i), arguments=[{"name": "code", "description": '说明🙂"\\' * 12}])
                for i in range(7)
            ]
        }
    ]
    manager = MCPManager(settings(), max_output_chars=650, client_factory=lambda *_: client)
    try:
        await manager.start()
        result = await listing(manager)
        catalog = manager._connections["test"].prompt_catalog
        assert catalog is not None and len(result["prompts"]) < 7
        assert (await getting(manager, catalog.token("ref", 6)))["status"] == "not_executed"
        shown = list(result["prompts"])
        while result["next_cursor"] is not None:
            cursor = result["next_cursor"]
            result = await listing(manager, cursor=cursor)
            assert await listing(manager, cursor=cursor) == result
            shown.extend(result["prompts"])
        assert [item["name"] for item in shown] == [str(i) for i in range(7)]
        assert all(item["arguments"][0]["description"] == '说明🙂"\\' * 12 for item in shown)
        assert len(catalog.references) == 7 and len(catalog.cursors) < 7
        assert client.prompt_cursors == [None]
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "arguments",
    [{"extra": "x"}, {"code": 1}, {"code": None}, None, {"code": "\ud800"}, {"code": "x" * 16384}],
    ids=["extra", "non-string", "null-value", "null-object", "unicode", "bytes"],
)
async def test_invalid_arguments_never_enter_the_sdk(arguments: object) -> None:
    client = PromptClient()
    client.prompt_pages = [{"prompts": [prompt(arguments=[{"name": "code", "required": True}])]}]
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = (await listing(manager))["prompts"][0]["ref"]
        result = await getting(manager, reference, arguments=arguments)
        assert result["is_error"] and result["status"] == "not_executed"
        assert result["code"] in {"prompt_arguments_invalid", "prompt_arguments_budget_exceeded"}
        assert client.gets == []
    finally:
        await manager.close()


async def test_missing_and_empty_arguments_are_distinct_and_do_not_invent_defaults() -> None:
    client = PromptClient()
    client.prompt_pages = [
        {
            "prompts": [
                prompt(arguments=[{"name": "code", "required": False, "default": "not applied"}])
            ]
        }
    ]
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = (await listing(manager))["prompts"][0]["ref"]
        for arguments in ({}, {"arguments": {}}, {"arguments": {"code": ""}}):
            assert (await getting(manager, reference, **arguments))["messages"] == []
        assert client.gets == [("draft", None), ("draft", {}), ("draft", {"code": ""})]
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "draft",
    [
        {"messages": [{"role": "user", "content": {"type": "image", "data": "blob"}}]},
        {
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "subset"}},
                {
                    "role": "assistant",
                    "content": {
                        "type": "resource",
                        "resource": {"uri": "file:///never-read", "text": "x"},
                    },
                },
            ]
        },
        {"messages": [{"role": "system", "content": {"type": "text", "text": "no"}}]},
        {"messages": [{"role": "user", "content": {"type": "text", "text": "x"}}] * 17},
    ],
    ids=["image", "mixed", "role", "messages"],
)
async def test_non_text_or_invalid_draft_is_never_a_successful_text_subset(
    draft: JsonObject,
) -> None:
    client = PromptClient()
    client.prompt_pages = [{"prompts": [prompt()]}]
    client.draft = draft
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = (await listing(manager))["prompts"][0]["ref"]
        result = await getting(manager, reference)
        assert result["is_error"] and result.get("status") != "not_executed"
        assert "messages" not in result and "preview" not in result and len(client.gets) == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("secret", ["text", "user"])
async def test_short_secret_keeps_protocol_fields_and_rejects_changed_parameter_names(
    secret: str,
) -> None:
    client = PromptClient()
    client.prompt_pages = [{"prompts": [prompt(secret, arguments=[])]}]
    client.draft = {
        "description": secret,
        "messages": [{"role": "user", "content": {"type": "text", "text": secret}}],
    }
    manager = MCPManager(settings(headers={"X-Key": secret}), client_factory=lambda *_: client)
    try:
        await manager.start()
        item = (await listing(manager))["prompts"][0]
        assert item["name"] == "••••"
        result = await getting(manager, item["ref"])
        assert result["messages"] == [{"role": "user", "text": "••••"}]
        assert result["description"] == "••••" and client.gets == [(secret, None)]
        client.prompt_pages = [{"prompts": [prompt(arguments=[{"name": secret}])]}]
        assert (await listing(manager))["code"] == "prompt_parameters_unavailable"
    finally:
        await manager.close()


async def test_cancelled_refresh_keeps_old_refs_and_late_generation_cannot_publish() -> None:
    client = PromptClient()
    client.prompt_pages = [{"prompts": [prompt()]}]
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    manager = MCPManager(settings(), client_factory=lambda *_: client)
    operation: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        old = (await listing(manager))["prompts"][0]["ref"]
        client.prompt_gate = hold
        operation = asyncio.create_task(listing(manager))
        async with asyncio.timeout(2):
            await entered.wait()
        assert (await listing(manager))["code"] == "prompt_catalog_busy"
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert (await getting(manager, old))["is_error"] is False
        entered.clear()
        operation = asyncio.create_task(listing(manager))
        async with asyncio.timeout(2):
            await entered.wait()
        candidate = await manager.prepare(settings())
        manager.publish(candidate)
        release.set()
        assert (await operation)["code"] == "prompt_stale"
        assert manager._connections["test"].prompt_catalog is None
        assert (await getting(manager, old))["status"] == "not_executed"
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()


async def test_admitted_get_uses_old_connection_secret_and_limit_then_retires() -> None:
    old, new = PromptClient(), PromptClient()
    old.prompt_pages = [{"prompts": [prompt()]}]
    old.draft = {
        "messages": [
            {"role": "assistant", "content": {"type": "text", "text": "old-secret" + "文" * 500}}
        ]
    }
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    clients = iter([old, new])
    manager = MCPManager(
        settings(headers={"X-Key": "old-secret"}),
        max_output_chars=2000,
        client_factory=lambda *_: next(clients),
    )
    operation: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        reference = (await listing(manager))["prompts"][0]["ref"]
        old.get_gate = hold
        operation = asyncio.create_task(getting(manager, reference))
        async with asyncio.timeout(2):
            await entered.wait()
        from muli.tools.mcp import MCPLimits

        retired = manager.publish(
            await manager.prepare(
                settings(url="https://unused.invalid/new"), limits=MCPLimits(max_output_chars=128)
            )
        )
        assert not old.closed and manager.draining == 1
        release.set()
        assert (await operation)["messages"] == [{"role": "assistant", "text": "••••" + "文" * 500}]
        assert await manager.retire(retired) and old.closed == 1
        assert old.gets == [("draft", None)]
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()


async def test_empty_catalog_and_unissued_reference_are_not_guessed_or_fetched() -> None:
    client = PromptClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        assert (await getting(manager, "not-shown"))["status"] == "not_executed"
        assert client.prompt_cursors == client.gets == []
        result = await listing(manager)
        assert result == {
            "is_error": False,
            "server": "test",
            "untrusted": True,
            "catalog_complete": True,
            "prompts": [],
            "next_cursor": None,
        }
        assert (await listing(manager, cursor="not-shown"))["code"] == "prompt_cursor_invalid"
        assert client.prompt_cursors == [None] and client.gets == []
    finally:
        await manager.close()


async def test_raw_directory_bytes_and_wrapped_draft_budget_have_exact_boundaries() -> None:
    from muli.tools.mcp import MCPLimits

    client = PromptClient()
    page = {"prompts": [prompt()], "_meta": '未知🙂"\\'}
    client.prompt_pages = [page]
    client.draft = {
        "description": "secret",
        "messages": [{"role": "user", "content": {"type": "text", "text": 'secret🙂"\\' * 30}}],
    }
    raw_limit = len(json.dumps(page, ensure_ascii=False).encode())
    config = settings(headers={"X-Key": "secret"})
    manager = MCPManager(config, max_catalog_bytes=raw_limit, client_factory=lambda *_: client)
    try:
        await manager.start()
        item = (await listing(manager))["prompts"][0]
        reference = item["ref"]
        previous = manager._connections["test"].prompt_catalog
        page["_meta"] += "x"
        assert (await listing(manager))["code"] == "prompt_catalog_budget_exceeded"
        assert manager._connections["test"].prompt_catalog is previous
        draft = await getting(manager, reference)
        assert "secret" not in json.dumps(draft, ensure_ascii=False)
        maximum = len(json.dumps(draft, ensure_ascii=False))
        limits = MCPLimits(max_catalog_bytes=raw_limit + 1, max_output_chars=maximum)
        manager.publish(await manager.prepare(config, limits=limits))
        reference = (await listing(manager))["prompts"][0]["ref"]
        assert len(json.dumps(await getting(manager, reference), ensure_ascii=False)) == maximum
        limits = MCPLimits(max_catalog_bytes=raw_limit + 1, max_output_chars=maximum - 1)
        manager.publish(await manager.prepare(config, limits=limits))
        reference = (await listing(manager))["prompts"][0]["ref"]
        assert await getting(manager, reference) == {
            "is_error": True,
            "code": "prompt_output_budget_exceeded",
        }
        assert manager.status()[0]["prompt_error_code"] == "prompt_output_budget_exceeded"
    finally:
        await manager.close()


@pytest.mark.parametrize("kind", ["error", "timeout", "cancel"])
async def test_entered_get_is_not_reported_as_unsent_or_automatically_retried(kind: str) -> None:
    client = PromptClient()
    client.prompt_pages = [{"prompts": [prompt()]}]
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail() -> None:
        entered.set()
        if kind == "cancel":
            await release.wait()
        elif kind == "timeout":
            raise TimeoutError("remote secrets must not appear")
        else:
            raise RuntimeError("remote secrets must not appear")

    client.get_gate = fail
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    operation: asyncio.Task[JsonObject] | None = None
    try:
        await manager.start()
        reference = (await listing(manager))["prompts"][0]["ref"]
        operation = asyncio.create_task(getting(manager, reference))
        async with asyncio.timeout(2):
            await entered.wait()
            if kind == "cancel":
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation
            else:
                result = await operation
                assert result == {
                    "is_error": True,
                    "code": "prompt_timeout" if kind == "timeout" else "prompt_unavailable",
                }
        assert client.gets == [("draft", None)]
        assert manager._connections["test"].active_calls == 0
        client.get_gate = None
        assert (await getting(manager, reference))["messages"] == []
        assert len(client.gets) == 2  # Only this explicit new call is a second request.
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.close()


async def test_parameter_byte_budget_includes_json_and_exact_required_contract() -> None:
    from muli.tools.mcp_prompts import MAX_PROMPT_ARGUMENT_BYTES

    client = PromptClient()
    client.prompt_pages = [{"prompts": [prompt(arguments=[{"name": "code", "required": True}])]}]
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        await manager.start()
        reference = (await listing(manager))["prompts"][0]["ref"]
        value = "x" * (MAX_PROMPT_ARGUMENT_BYTES - len(json.dumps({"code": ""}).encode()))
        assert (await getting(manager, reference, arguments={"code": value}))["messages"] == []
        assert client.gets == [("draft", {"code": value})]
        assert (await getting(manager, reference, arguments={"code": value + "x"}))[
            "code"
        ] == "prompt_arguments_budget_exceeded"
        assert len(client.gets) == 1
    finally:
        await manager.close()
