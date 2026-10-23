"""Locked optional SDKs through real streaming HTTPX, without sockets."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import AnyUrl
from test_extensions import setup_runtime
from test_mcp import settings
from test_mcp_receive import Chunks
from test_mcp_resources import listing, reading
from test_mcp_resources import offline as offline

from muli.attachments import Attachments
from muli.local_services import LocalServices
from muli.mcp_policy import MIN_MESSAGE_BYTES, ReceivePolicy
from muli.providers import ToolCall
from muli.tools.mcp import MCPManager, _FastMCPClient


async def test_locked_streaming_sdk_blob_saves_original_bytes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils

    data = b"\x89PNG\r\n\x1a\n" + b"original binary\x00\xff"
    methods: list[str] = []
    streams: list[Chunks] = []

    def response(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405, stream=Chunks())
        message = json.loads(request.content)
        method = message["method"]
        methods.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202, stream=Chunks())
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"resources": {}},
                "serverInfo": {"name": "blob-fixture", "version": "1"},
            }
        elif method == "resources/list":
            result = {"resources": [{"uri": "file:///not-opened", "name": "blob"}]}
        elif method == "resources/read":
            assert message["params"]["uri"] == "file:///not-opened"
            result = {
                "contents": [
                    {
                        "uri": "custom:///returned",
                        "mimeType": "text/plain",
                        "blob": base64.b64encode(data).decode("ascii"),
                    }
                ],
                "_meta": {"untrusted": "must not propagate"},
            }
        else:
            raise AssertionError(method)
        payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        body = Chunks(payload[:3], payload[3:21], payload[21:])
        streams.append(body)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=body)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False, **kwargs)

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", factory)
    runtime = setup_runtime(tmp_path)
    runtime.config.settings.mcp = settings()
    runtime.mcp = MCPManager(settings(), timeout=2)
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", runtime.config)
    LocalServices(runtime, attachments)
    try:
        await runtime.extensions.load_initial()
        reference = (await listing(runtime.mcp))["resources"][0]["ref"]
        session = runtime.new_session(mode="yolo")
        runtime.run_ids[session["id"]] = "offline-owner"
        receipt = await runtime._tool(
            ToolCall(
                "save",
                "mcp_resource_save",
                json.dumps({"server": "test", "ref": reference, "name": "local.png"}),
            ),
            runtime.session_tools(session["id"]),
            "yolo",
            session["id"],
            "offline-owner",
        )
        assert receipt["saved"] is True, receipt
        item = receipt["attachment"]
        assert item["mime_type"] == "image/png" and item["size"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert attachments.path(item["id"]).read_bytes() == data
        assert methods.count("resources/read") == methods.count("resources/list") == 1
        assert "tools/list" not in methods
        assert runtime.store.list("channel_outbox") == []
    finally:
        await runtime.close()
    assert all(stream.closed == 1 for stream in streams)


@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_locked_factory_resource_only_server_uses_real_session_params_and_text(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils

    requests: list[dict[str, Any]] = []
    streams: list[Chunks] = []
    events: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
    uri = "memory:///notes/完整🙂"

    class Events(Chunks):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"event: endpoint\ndata: /messages\n\n"
            while True:
                yield await events.get()

    def response(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "GET":
            body = Events() if transport == "sse" else Chunks()
            streams.append(body)
            return httpx.Response(
                200 if transport == "sse" else 405,
                headers={"content-type": "text/event-stream"},
                stream=body,
            )
        if request.method == "DELETE":
            return httpx.Response(204, stream=Chunks())
        message = json.loads(request.content)
        requests.append(message)
        method = message["method"]
        if method == "notifications/initialized":
            return httpx.Response(202, stream=Chunks())
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"resources": {}},
                "serverInfo": {"name": "synthetic-resource-server", "version": "1"},
            }
        elif method == "resources/list":
            cursor = message["params"].get("cursor")
            assert cursor in {None, ""}
            result = (
                {"resources": [], "nextCursor": "", "_meta": {"untrusted": "ignored"}}
                if cursor is None
                else {"resources": [{"name": "说明", "uri": uri, "extension": "ignored"}]}
            )
        elif method == "resources/read":
            assert message["params"]["uri"] == str(AnyUrl(uri))
            result = {
                "contents": [{"uri": uri, "text": '不可信内容🙂\n"\\', "mimeType": "text/plain"}]
            }
        else:
            raise AssertionError(f"unexpected SDK method: {method}")
        payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        if transport == "sse":
            events.put_nowait(b"event: message\ndata: " + payload + b"\n\n")
            return httpx.Response(202, stream=Chunks())
        body = Chunks(payload[:7], payload[7:])
        streams.append(body)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=body,
        )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(response),
            follow_redirects=True,
            trust_env=False,
            **kwargs,
        )

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", factory)
    manager = MCPManager(settings(transport=transport), timeout=2)
    try:
        assert {item.name for item in await manager.start()} == {
            "mcp_resources_list",
            "mcp_resource_read",
        }
        assert [message["method"] for message in requests] == [
            "initialize",
            "notifications/initialized",
        ]
        result = await listing(manager)
        assert result["is_error"] is False and len(result["resources"]) == 1
        text = await reading(manager, result["resources"][0]["ref"])
        assert text["contents"][0]["text"] == '不可信内容🙂\n"\\'
        assert text["contents"][0]["mime_type"] == "text/plain" and text["untrusted"] is True
        assert [m["params"].get("cursor") for m in requests if m["method"] == "resources/list"] == [
            None,
            "",
        ]
        assert all(m["method"] != "tools/list" for m in requests)
    finally:
        await manager.close()
    assert all(stream.closed == 1 for stream in streams)


async def test_locked_adapter_does_not_treat_missing_initialization_as_empty_resources() -> None:
    pytest.importorskip("mcp")
    from types import SimpleNamespace

    from muli.tools.mcp import MCPPrepareError

    adapter = _FastMCPClient(
        SimpleNamespace(session=SimpleNamespace(get_server_capabilities=lambda: None))
    )
    with pytest.raises(MCPPrepareError, match="^capabilities_unavailable$"):
        adapter.server_capabilities()


async def test_real_sdk_resource_read_guard_latches_and_does_not_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils

    reads: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405, stream=Chunks())
        message = json.loads(request.content)
        method = message["method"]
        if method == "notifications/initialized":
            return httpx.Response(202, stream=Chunks())
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"resources": {}},
                "serverInfo": {"name": "receive-fixture", "version": "1"},
            }
        elif method == "resources/list":
            result = {"resources": [{"uri": "test:///note", "name": "note"}]}
        elif method == "resources/read":
            reads.append(message["params"]["uri"])
            result = {"contents": [{"uri": "test:///note", "text": "x" * MIN_MESSAGE_BYTES}]}
        else:
            raise AssertionError(method)
        payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=Chunks(payload[:10], payload[10:]),
        )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False, **kwargs)

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", factory)
    manager = MCPManager(
        settings(), timeout=1, receive_policy=ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES)
    )
    try:
        await manager.start()
        reference = (await listing(manager))["resources"][0]["ref"]
        result = await reading(manager, reference)
        assert result["is_error"] and result.get("status") != "not_executed"
        assert manager.status()[0]["error_code"] == "mcp_receive_budget_exceeded"
        assert (await reading(manager, reference))["status"] == "not_executed"
        assert reads == ["test:///note"]
    finally:
        await manager.close()
