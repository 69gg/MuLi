"""Locked SDK prompt sessions over streaming MockTransport, never sockets."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from test_mcp import settings
from test_mcp_prompts import getting, listing
from test_mcp_receive import Chunks
from test_mcp_resources import offline as offline

from muli.mcp_policy import MIN_MESSAGE_BYTES, ReceivePolicy
from muli.tools.mcp import MCPManager


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("mixed", [False, True], ids=["text", "mixed-image"])
async def test_real_prompt_only_session_preserves_cursor_arguments_and_text_or_rejects_whole_media(
    monkeypatch: pytest.MonkeyPatch, transport: str, mixed: bool
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils

    requests: list[dict[str, Any]] = []
    streams: list[Chunks] = []
    events: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)

    class Events(Chunks):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"event: endpoint\ndata: /messages\n\n"
            while True:
                yield await events.get()

    def response(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "GET":
            stream = Events() if transport == "sse" else Chunks()
            streams.append(stream)
            return httpx.Response(
                200 if transport == "sse" else 405,
                headers={"content-type": "text/event-stream"},
                stream=stream,
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
                "capabilities": {"prompts": {}},
                "serverInfo": {"name": "finite-prompt-server", "version": "1"},
            }
        elif method == "prompts/list":
            cursor = message["params"].get("cursor")
            assert cursor in {None, ""}
            result = (
                {"prompts": [], "nextCursor": ""}
                if cursor is None
                else {
                    "prompts": [
                        {
                            "name": "草稿🙂",
                            "arguments": [{"name": "code", "required": False}],
                            "extra": "ignored",
                        }
                    ]
                }
            )
        elif method == "prompts/get":
            assert message["params"]["name"] == "草稿🙂"
            result = {
                "description": "仅为工具资料",
                "messages": [
                    {"role": "assistant", "content": {"type": "text", "text": '完整🙂\n"\\'}},
                    {
                        "role": "user",
                        "content": {"type": "image", "data": "YQ==", "mimeType": "image/png"}
                        if mixed
                        else {"type": "text", "text": ""},
                    },
                ],
            }
        else:
            raise AssertionError(f"unexpected SDK method: {method}")
        payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        if transport == "sse":
            events.put_nowait(b"event: message\ndata: " + payload + b"\n\n")
            return httpx.Response(202, stream=Chunks())
        stream = Chunks(payload[:7], payload[7:])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

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
            "mcp_prompts_list",
            "mcp_prompt_get",
        }
        assert [message["method"] for message in requests] == [
            "initialize",
            "notifications/initialized",
        ]
        page = await listing(manager)
        assert page["catalog_complete"] and len(page["prompts"]) == 1
        item = page["prompts"][0]
        assert item["name"] == "草稿🙂" and item["arguments"] == [
            {"name": "code", "required": False}
        ]
        for arguments in ({}, {"arguments": {}}, {"arguments": {"code": "值🙂"}}):
            draft = await getting(manager, item["ref"], **arguments)
            if mixed:
                assert draft == {"is_error": True, "code": "prompt_content_unsupported"}
            else:
                assert draft["messages"] == [
                    {"role": "assistant", "text": '完整🙂\n"\\'},
                    {"role": "user", "text": ""},
                ]
                assert draft["untrusted"] and draft["description"] == "仅为工具资料"
        gets = [message["params"] for message in requests if message["method"] == "prompts/get"]
        assert gets == [
            {"name": "草稿🙂"},
            {"name": "草稿🙂", "arguments": {}},
            {"name": "草稿🙂", "arguments": {"code": "值🙂"}},
        ]
        assert [
            message["params"].get("cursor")
            for message in requests
            if message["method"] == "prompts/list"
        ] == [None, ""]
        assert not any(
            message["method"].startswith(("tools/", "resources/")) for message in requests
        )
    finally:
        await manager.close()
    assert all(stream.closed == 1 for stream in streams)


async def test_real_prompt_get_receive_latch_rejects_new_dispatch_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils

    gets: list[str] = []

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
                "capabilities": {"prompts": {}},
                "serverInfo": {"name": "receive-fixture", "version": "1"},
            }
        elif method == "prompts/list":
            result = {"prompts": [{"name": "draft"}]}
        elif method == "prompts/get":
            gets.append(message["params"]["name"])
            result = {
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": "x" * MIN_MESSAGE_BYTES}}
                ]
            }
        else:
            raise AssertionError(method)
        data = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=Chunks(data[:10], data[10:])
        )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False, **kwargs)

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", factory)
    manager = MCPManager(
        settings(), timeout=1, receive_policy=ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES)
    )
    try:
        await manager.start()
        reference = (await listing(manager))["prompts"][0]["ref"]
        result = await getting(manager, reference)
        assert result["is_error"] and result.get("status") != "not_executed"
        assert manager.status()[0]["error_code"] == "mcp_receive_budget_exceeded"
        assert (await getting(manager, reference))["status"] == "not_executed"
        assert gets == ["draft"]
    finally:
        await manager.close()
