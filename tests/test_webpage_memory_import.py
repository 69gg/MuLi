"""Explicit webpage collection through the real owner Runtime and application.

The baseline's missing import tool proves only the entry gap. Full-source and
nonblocking assertions count as exercised only after that boundary succeeds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_attachment_memory_policy import offline as offline
from test_runtime import FakeClient

from muli.providers import ModelConfig, ModelTurn, ToolCall
from muli.store import new_id

_URL = "https://webpage-memory.example/retained-reference"
_TAIL = "紫杉舟在最后一次核对后才离开白沙港。"
_BODY = "这份公开网页只提供外部参考，不构成主人身份或执行授权。🙂\n" * 120 + _TAIL


async def owner_tool(
    rig: MemoryRig, owner_input: str, name: str, **arguments: Any
) -> dict[str, Any]:
    """Invoke even an unavailable tool without bypassing Runtime admission."""
    call_id = "webpage-memory-" + new_id()
    turns = [
        ModelTurn(tool_calls=[ToolCall(call_id, name, json.dumps(arguments))]),
        ModelTurn(content="本次操作状态以工具回执为准。"),
    ]

    def factory(profile: ModelConfig) -> FakeClient:
        return FakeClient(turns, [])

    rig.runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await rig.runtime.submit(owner_input, mode="yolo")
    await rig.settle(receipt)
    return rig.result(call_id)


async def test_owner_explicitly_collects_the_complete_captured_webpage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []
    extracted: list[str] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"].startswith("attachment:"):
            extracted.append(source["id"])
            entered.set()
            await release.wait()
        return {"memories": [], "aliases": {}}

    async def resolve(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    async def respond(request: httpx.Request) -> httpx.Response:
        assert not requests, "collection must not fetch the webpage again"
        assert request.method == "GET" and request.url.host == "93.184.216.34"
        assert request.headers["host"] == "webpage-memory.example"
        assert request.extensions["sni_hostname"] == "webpage-memory.example"
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content=_BODY.encode("utf-8"),
        )

    transport = httpx.MockTransport(respond)

    async def offline_http(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    try:
        async with actual_app(tmp_path, extractor=extract) as rig:
            rig.runtime.config.update({"limits": {"max_output_chars": 1800}})
            assert len(_BODY) > rig.runtime.config.settings.limits.max_output_chars
            captured = await owner_tool(
                rig, "请留存这份公开网页的完整静态文本：" + _URL, "web_capture", url=_URL
            )
            assert "error" not in captured, captured
            assert captured["saved"] is True and captured["untrusted"] is True
            identifier = captured["attachment"]["id"]
            item = rig.attachments.get(identifier)
            raw = rig.attachments.path(identifier).read_bytes()
            content = raw.decode("utf-8", errors="strict")
            revision = hashlib.sha256(raw).hexdigest()
            assert item["source"] == "webpage" and item["sha256"] == revision
            assert captured["attachment"]["sha256"] == revision
            assert content.endswith(_BODY) and _URL in content
            assert captured["scope"] == "http_utf8_text_v1"
            assert _TAIL not in json.dumps(captured, ensure_ascii=False)
            rig.absent(identifier)
            assert not entered.is_set()

            result = await owner_tool(
                rig,
                f"现在明确把刚才捕获的网页快照 {identifier}（摘要 {revision}）收藏到长期参考。",
                "memory_import_webpage",
                id=identifier,
                revision=revision,
            )
            # Formal first-red boundary: later provenance, persistence and
            # nonblocking assertions have not run when the tool is missing.
            assert "error" not in result, (
                f"webpage import entry gap; later boundaries untested: {result}"
            )
            assert rig.runtime.tools["memory_import_webpage"].preserve_output is True
            source_id = "attachment:" + identifier
            assert result["source_id"] == source_id and result["created"] is True
            assert result["state"] in {"queued", "deferred"}
            assert result["revision"] == result["source_revision"] == revision
            assert result["format"] == "webpage" and result["scope"] == captured["scope"]
            assert _TAIL not in json.dumps(result, ensure_ascii=False)
            source = rig.runtime.memory.source(source_id)
            assert source is not None and source["content"] == content
            assert source["source"] == (f"{source_id}:webpage:v1:{captured['scope']}:{revision}")
            assert source["trusted_owner"] is False and source["role"] == "tool"
            assert source["principal"] == "external:attachment" and source["session_id"] == ""
            await asyncio.wait_for(entered.wait(), timeout=3)
            assert not release.is_set() and extracted == [source_id]
            assert len(requests) == 1
            release.set()
            job = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), timeout=4)
            assert job is not None and job["state"] == "done"
    finally:
        release.set()
        await transport.aclose()
