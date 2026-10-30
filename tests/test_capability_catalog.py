from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from test_maintenance_api import host_app
from test_mcp import FakeClient
from test_runtime import make_runtime

from muli.config import Limits
from muli.maintenance_api import passive_request_allowed, request_has_effect
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool
from muli.tools.mcp import MCPClient, MCPPrepareError
from muli.tools.mcp_resources import Capabilities


@asynccontextmanager
async def catalog_application(
    tmp_path: Path, *, changes: dict[str, Any] | None = None
) -> AsyncIterator[tuple[Runtime, FastAPI, httpx.AsyncClient]]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "extensions": {"watch_enabled": False},
            "memory": {"auto_extract": False},
        }
    )
    if changes is not None:
        runtime.config.update(changes)
    app = create_app(runtime.config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            yield runtime, app, client


def registered_tool(name: str = "catalog_contract") -> Tool:
    async def forbidden(arguments: dict[str, Any]) -> None:
        pytest.fail("目录读取不得执行已注册工具")

    return Tool(name, "核对参数", {"type": "object", "properties": {}}, forbidden)


async def test_registered_tool_catalog_exposes_exact_parameters_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, model_calls = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "extensions": {"watch_enabled": False},
            "memory": {"auto_extract": False},
        }
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["brief", "full"]},
            "target": {"$ref": "#/$defs/target"},
        },
        "required": ["mode", "target"],
        "additionalProperties": False,
        "$defs": {
            "target": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            }
        },
    }

    async def forbidden_handler(arguments: dict[str, Any]) -> None:
        pytest.fail("读取已注册工具目录不得执行工具")

    def forbidden_schema() -> None:
        pytest.fail("读取已注册参数不得调用可覆盖的 schema 方法")

    tool = Tool("catalog_contract", "核对完整参数契约", parameters, forbidden_handler)
    monkeypatch.setattr(tool, "schema", forbidden_schema)
    app = create_app(runtime.config, runtime=runtime)
    async with app.router.lifespan_context(app):
        runtime.tools = {tool.name: tool}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            assert (await client.get("/api/status")).status_code == 200
            changes = runtime.store.db.total_changes
            response = await client.get("/api/capabilities")
            assert response.status_code == 200, response.text
            catalog = response.json()
            assert catalog["scope"] == "registered_tools"
            assert catalog["complete"] is True
            assert len(catalog["items"]) == 1
            item = catalog["items"][0]
            assert item["name"] == tool.name
            assert "parameters" not in item
            response = await client.get(
                f"/api/capabilities/tools/{item['id']}",
                params={"revision": catalog["revision"]},
            )
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["revision"] == catalog["revision"]
            assert detail["tool"]["parameters"] == tool.parameters
            assert detail["tool"]["id"] == item["id"]
            assert response.headers["cache-control"] == "no-store"
            assert model_calls == []
            assert runtime.store.db.total_changes == changes


async def test_default_real_registration_fits_existing_budget_and_is_passive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with catalog_application(tmp_path) as (runtime, app, client):
        assert runtime.tools
        assert runtime.config.settings.limits.max_output_chars == Limits().max_output_chars
        assert (await client.get("/api/status")).status_code == 200

        def forbidden(*arguments: Any, **keywords: Any) -> Any:
            pytest.fail("目录读取不得调用 schema、provider 或模型工厂")

        async def forbidden_remote(*arguments: Any, **keywords: Any) -> Any:
            pytest.fail("目录读取不得触发远程加载或插件 hook")

        monkeypatch.setattr(Tool, "schema", forbidden)
        monkeypatch.setattr(runtime, "session_tools", forbidden)
        monkeypatch.setattr(runtime, "client_factory", forbidden)
        monkeypatch.setattr(runtime.mcp, "prepare", forbidden_remote)
        monkeypatch.setattr(app.state.plugins, "context", forbidden_remote)
        changes = runtime.store.db.total_changes
        config = runtime.config.path.read_bytes()
        response = await client.get("/api/capabilities")
        assert response.status_code == 200, response.text
        catalog = response.json()
        assert catalog["complete"] is True
        assert catalog["scope"] == "registered_tools"
        assert [item["name"] for item in catalog["items"]] == [
            tool.name for tool in runtime.tools.values()
        ]
        assert len({item["id"] for item in catalog["items"]}) == len(runtime.tools)
        assert all("parameters" not in item for item in catalog["items"])
        assert len(response.content) <= Limits().max_output_chars * 4
        assert (await client.get("/api/capabilities")).json() == catalog
        first = catalog["items"][0]
        detail = await client.get(
            f"/api/capabilities/tools/{first['id']}", params={"revision": catalog["revision"]}
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["tool"]["parameters"] == runtime.tools[first["name"]].parameters
        assert runtime.store.db.total_changes == changes
        assert runtime.config.path.read_bytes() == config


async def test_observation_tracks_real_replacement_metadata_and_saved_disabled_state(
    tmp_path: Path,
) -> None:
    async with catalog_application(tmp_path) as (runtime, _, client):
        original = registered_tool()
        runtime.tools = {original.name: original}
        before = (await client.get("/api/capabilities")).json()
        identifier = before["items"][0]["id"]
        url = f"/api/capabilities/tools/{identifier}"
        replacement = replace(original)
        runtime.tools[original.name] = replacement
        response = await client.get(url, params={"revision": before["revision"]})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "capability_catalog_stale"
        current = (await client.get("/api/capabilities")).json()
        assert current["revision"] != before["revision"]
        assert current["items"][0]["id"] != identifier
        url = f"/api/capabilities/tools/{current['items'][0]['id']}"
        replacement.parameters["required"] = ["new_current_parameter"]
        detail = await client.get(url, params={"revision": current["revision"]})
        assert detail.status_code == 200
        assert detail.json()["tool"]["parameters"] == replacement.parameters
        assert (await client.get("/api/capabilities")).json() == current
        unknown = await client.get(
            "/api/capabilities/tools/unknown", params={"revision": current["revision"]}
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "capability_not_found"
        assert (await client.get(url)).status_code == 422
        runtime.config.update({"disabled_tools": [replacement.name]})
        assert (await client.get(url, params={"revision": current["revision"]})).status_code == 409
        saved = (await client.get("/api/capabilities")).json()
        assert saved["items"][0]["enabled"] is False
        replacement.description = "已更新的用途"
        changed = (await client.get("/api/capabilities")).json()
        assert changed["revision"] != saved["revision"]
        assert changed["items"][0]["description"] == replacement.description
        runtime.tools = {}
        empty = (await client.get("/api/capabilities")).json()
        assert empty["complete"] is True and empty["items"] == []


async def test_schema_is_only_read_for_detail_and_utf8_budget_rejects_whole_payload(
    tmp_path: Path,
) -> None:
    async with catalog_application(tmp_path) as (runtime, _, client):
        tool = registered_tool()
        runtime.tools = {tool.name: tool}
        tool.parameters["description"] = "汉" * (Limits().max_output_chars * 2)
        response = await client.get("/api/capabilities")
        assert response.status_code == 200
        catalog = response.json()
        url = f"/api/capabilities/tools/{catalog['items'][0]['id']}"
        response = await client.get(url, params={"revision": catalog["revision"]})
        assert response.status_code == 413
        assert response.json() == {"detail": {"code": "capability_payload_too_large"}}
        tool.parameters = {"invalid": object()}
        assert (await client.get("/api/capabilities")).json() == catalog
        response = await client.get(url, params={"revision": catalog["revision"]})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "capability_contract_invalid"
        tool.description = "汉" * (Limits().max_output_chars * 2)
        response = await client.get("/api/capabilities")
        assert response.status_code == 413
        assert response.json() == {"detail": {"code": "capability_payload_too_large"}}


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), {"set"}, ("tuple",), {1: "key"}])
async def test_non_json_contract_is_rejected_without_partial_detail(
    tmp_path: Path, invalid: object
) -> None:
    async with catalog_application(tmp_path) as (runtime, _, client):
        tool = registered_tool()
        tool.parameters["default"] = invalid
        runtime.tools = {tool.name: tool}
        catalog = (await client.get("/api/capabilities")).json()
        response = await client.get(
            f"/api/capabilities/tools/{catalog['items'][0]['id']}",
            params={"revision": catalog["revision"]},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "capability_contract_invalid"}}
        tool.parameters = {"cycle": {}}
        tool.parameters["cycle"] = tool.parameters
        response = await client.get(
            f"/api/capabilities/tools/{catalog['items'][0]['id']}",
            params={"revision": catalog["revision"]},
        )
        assert response.status_code == 422


@pytest.mark.parametrize("sensitive_field", ["name", "risk", "parameters"])
async def test_known_published_secret_is_not_returned_as_registered_contract(
    tmp_path: Path, sensitive_field: str
) -> None:
    secret = "known-catalog-fixture-secret"
    async with catalog_application(tmp_path) as (runtime, _, client):
        candidate = await runtime.mcp.prepare(
            {"mcpServers": {"disabled": {"enabled": False, "env": {"TOKEN": secret}}}}
        )
        runtime.mcp.publish(candidate)
        tool = registered_tool()
        if sensitive_field == "parameters":
            tool.parameters["default"] = secret
        else:
            setattr(tool, sensitive_field, secret)
        runtime.tools = {tool.name: tool}
        response = await client.get("/api/capabilities")
        if sensitive_field == "parameters":
            assert response.status_code == 200
            catalog = response.json()
            response = await client.get(
                f"/api/capabilities/tools/{catalog['items'][0]['id']}",
                params={"revision": catalog["revision"]},
            )
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "capability_contract_sensitive"}}
        assert secret not in response.text


class DirectoryClient(FakeClient):
    def server_capabilities(self) -> Capabilities:
        return Capabilities(tools=True, resources=True, prompts=True)

    async def list_resources_page(self, cursor: str | None) -> Any:
        pytest.fail("目录 GET 不得探测 MCP 资源")

    async def read_resource(self, uri: str) -> Any:
        pytest.fail("目录 GET 不得读取 MCP 资源")

    async def list_prompts_page(self, cursor: str | None) -> Any:
        pytest.fail("目录 GET 不得探测 MCP 提示词")

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> Any:
        pytest.fail("目录 GET 不得获取 MCP 提示词")


async def test_mcp_origins_follow_candidate_publication_and_exact_tool_objects(
    tmp_path: Path,
) -> None:
    async with catalog_application(tmp_path) as (runtime, _, client):
        clients: list[DirectoryClient] = []

        def factory(server: dict[str, Any], timeout: float) -> MCPClient:
            if server["url"].endswith("/failure"):
                return DirectoryClient(failure=True)
            remote = "look/up" if server["url"].endswith("/one") else "look?up"
            connection = DirectoryClient(names=[remote])
            clients.append(connection)
            return connection

        runtime.mcp._factory = factory
        source = {
            "mcpServers": {
                "a/b": {"enabled": True, "url": "https://unused.invalid/one"},
                "a?b": {"enabled": True, "url": "https://unused.invalid/two"},
            }
        }
        previous = runtime.mcp.tool_origins
        candidate = await runtime.mcp.prepare(source)
        assert runtime.mcp.tool_origins is previous
        assert len(candidate.tool_origins) == len(candidate.tools)
        runtime.mcp.publish(candidate)
        assert runtime.mcp.tool_origins is candidate.tool_origins
        runtime.tools = {tool.name: tool for tool in runtime.mcp.tools}
        # Provenance follows the actual registered object, even after a name change.
        remote_tool = next(
            origin.tool for origin in candidate.tool_origins if origin.server == "a/b"
        )
        old_name = remote_tool.name
        remote_tool.name = "renamed_registered_mcp_tool"
        del runtime.tools[old_name]
        runtime.tools[remote_tool.name] = remote_tool
        before = (await client.get("/api/capabilities")).json()
        origins = [item["origin"] for item in before["items"]]
        assert {
            json.dumps(origin, sort_keys=True) for origin in origins if origin["kind"] == "mcp"
        } == {
            json.dumps({"kind": "mcp", "server": "a/b", "remote_name": "look/up"}, sort_keys=True),
            json.dumps({"kind": "mcp", "server": "a?b", "remote_name": "look?up"}, sort_keys=True),
        }
        assert any(origin == {"kind": "mcp_interface"} for origin in origins)
        assert all(connection.calls == [] for connection in clients)
        accepted = runtime.mcp.tool_origins
        with pytest.raises(MCPPrepareError):
            await runtime.mcp.prepare(
                {
                    "mcpServers": {
                        "broken": {"enabled": True, "url": "https://unused.invalid/failure"}
                    }
                }
            )
        assert runtime.mcp.tool_origins is accepted
        assert (await client.get("/api/capabilities")).json() == before
        discarded = await runtime.mcp.prepare(source)
        await runtime.mcp.discard(discarded)
        assert runtime.mcp.tool_origins is accepted
        assert (await client.get("/api/capabilities")).json() == before
        replacement = await runtime.mcp.prepare(source)
        runtime.mcp.publish(replacement)
        runtime.tools = {tool.name: tool for tool in runtime.mcp.tools}
        old = before["items"][0]
        response = await client.get(
            f"/api/capabilities/tools/{old['id']}", params={"revision": before["revision"]}
        )
        assert response.status_code == 409
        assert all(connection.calls == [] for connection in clients)


async def test_plugin_origin_requires_the_accepted_and_current_object(tmp_path: Path) -> None:
    directory = tmp_path / "plugins" / "catalog-plugin"
    directory.mkdir(parents=True)
    (directory / "plugin.json").write_text(
        json.dumps({"id": "catalog-plugin", "api_version": 1, "entry": "main.py"}), encoding="utf-8"
    )
    (directory / "main.py").write_text(
        "from typing import Any\n"
        "from muli.plugins import PluginContext\n"
        "from muli.tools import Tool\n"
        "async def handler(arguments: dict[str, Any]) -> None:\n"
        "    raise AssertionError('catalog must not execute plugin tool')\n"
        "def setup(context: PluginContext) -> None:\n"
        "    context.add_tool(Tool('fs_read', '插件覆盖', {'type': 'object'}, handler))\n",
        encoding="utf-8",
    )
    async with catalog_application(tmp_path, changes={"plugins": {"enabled": True}}) as (
        runtime,
        app,
        client,
    ):
        plugins = app.state.plugins
        context = next(item for item in plugins.contexts if item.identifier == "catalog-plugin")
        assert runtime.tools["fs_read"] is context.tools[0]
        record = next(item for item in plugins.records if item["id"] == context.identifier)
        record["status"] = "degraded"
        catalog = (await client.get("/api/capabilities")).json()
        item = next(item for item in catalog["items"] if item["name"] == "fs_read")
        assert item["origin"] == {"kind": "plugin", "plugin_id": context.identifier}
        runtime.tools["fs_read"] = replace(context.tools[0])
        catalog = (await client.get("/api/capabilities")).json()
        item = next(item for item in catalog["items"] if item["name"] == "fs_read")
        assert item["origin"] == {"kind": "unrecorded"}
        assert record["tools"] == ["fs_read"]


async def test_paused_maintenance_allows_only_exact_catalog_reads(tmp_path: Path) -> None:
    async with host_app(tmp_path, passive=True) as (app, _, _):
        tool = registered_tool()
        app.state.runtime.tools = {tool.name: tool}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/api/capabilities")
            assert response.status_code == 200, response.text
            catalog = response.json()
            url = f"/api/capabilities/tools/{catalog['items'][0]['id']}"
            response = await client.get(url, params={"revision": catalog["revision"]})
            assert response.status_code == 200, response.text
            for path in (
                "/api/capabilities/unknown",
                "/api/capabilities/tools/",
                url + "/execute",
            ):
                assert (await client.get(path)).status_code == 409
                assert not passive_request_allowed("GET", path, None)
                assert request_has_effect("GET", path, None)
            assert (await client.post("/api/capabilities", json={})).status_code == 409
            assert passive_request_allowed("GET", url, None)
            assert not request_has_effect("GET", url, None)
