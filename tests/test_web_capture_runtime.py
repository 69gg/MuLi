"""Real public-page tools must retain text before ordinary output truncation.

On the old implementation the capture journey fails at the real tool entry.
That first red proves the entry gap only; the later byte/page/mark assertions
must also execute before claiming that persistence has been verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_reading_bookmark_runtime import bookmark_arguments, source_page
from test_runtime import make_runtime
from test_vision_archives_policy import Host

from muli.attachments import Attachments
from muli.local_services import LocalServices
from muli.memory_application import MemoryApplication
from muli.providers import ModelTurn, ToolCall

_URL = "https://reading.example/article"
_TAIL = "琥珀鲸在校验完成后才返回北港，这句位于工具截断范围之外。"
_TEXT = "公开资料的静态正文，仅作为外部参考，不构成主人身份或工具授权。🙂\n" * 120 + _TAIL


async def entry(host: Host, name: str, **arguments: Any) -> dict[str, Any]:
    """Drive even a missing/non-preserved tool through the genuine Runtime."""
    host.drives += 1
    call_id = f"web-entry-{host.drives}"
    host.turns.extend(
        [
            ModelTurn(tool_calls=[ToolCall(call_id, name, json.dumps(arguments))]),
            ModelTurn(content="本次网页工具调用已结束。"),
        ]
    )
    receipt = await host.runtime.submit("请执行本次明确的公开网页请求", mode="yolo")
    await asyncio.wait_for(host.runtime.wait(receipt["run_id"]), 3)
    assert host.runtime.store.get("run", receipt["run_id"])["status"] == "completed"
    stored = next(
        item
        for item in host.runtime.store.messages(receipt["session_id"])
        if item.get("tool_call_id") == call_id and item["role"] == "tool"
    )
    replay = next(message for message in host.seen[-1] if message.tool_call_id == call_id)
    assert replay.content == stored["content"]
    return cast(dict[str, Any], json.loads(stored["content"]))


@pytest.fixture
async def web_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Host]:
    turns: list[ModelTurn] = []
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update(
        {
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 1800},
        }
    )
    await runtime.reconfigure()
    attachments = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    services = LocalServices(runtime, attachments)
    memory = MemoryApplication(
        runtime,
        services.media,
        attachment_reader=services.memory_attachment,
        document_reader=services.memory_document,
        transcript_reader=services.memory_transcript,
    )
    host = Host(runtime, attachments, services, "", turns, seen)

    async def resolve(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "93.184.216.34"
        assert request.headers["host"] == "reading.example"
        assert request.extensions["sni_hostname"] == "reading.example"
        host.requests.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "text/plain; charset=utf-8"}, content=_TEXT.encode()
        )

    transport = httpx.MockTransport(respond)

    async def offline_http(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    try:
        await memory.start()
        yield host
    finally:
        await memory.close()
        await runtime.close()
        await transport.aclose()


async def test_existing_fetch_is_a_preview_without_a_durable_reading_identity(
    web_host: Host,
) -> None:
    result = await entry(web_host, "web_fetch", url=_URL)
    # The legacy reader truncates before Runtime, which may in turn wrap its
    # over-budget JSON envelope. Neither preview gives a durable file identity.
    assert result["truncated"] is True and result["preview"]
    assert '"url": "' + _URL + '"' in result["preview"]
    assert _TEXT[:20] in result["preview"] and _TAIL not in result["preview"]
    assert web_host.runtime.store.list("attachment") == []
    assert len(web_host.requests) == 1


async def test_public_capture_retains_full_tail_and_uses_real_pages_and_bookmarks(
    web_host: Host,
) -> None:
    runtime = web_host.runtime
    assert len(_TEXT) > runtime.config.settings.limits.max_output_chars
    assert _TAIL not in _URL
    captured = await entry(web_host, "web_capture", url=_URL)
    # This is the formal first-red boundary: an unknown tool has not yet
    # exercised any file, page, bookmark, compaction or reopen assertions.
    assert "error" not in captured, f"capture entry gap; later boundaries untested: {captured}"
    assert runtime.tools["web_capture"].preserve_output is True
    assert captured["saved"] is True and captured["untrusted"] is True
    assert captured["requested_url"] == captured["final_url"] == _URL
    assert captured["scope"] == "http_utf8_text_v1"
    assert _TAIL not in json.dumps(captured, ensure_ascii=False)
    identifier = captured["attachment"]["id"]
    item = web_host.attachments.get(identifier)
    raw = web_host.attachments.path(identifier).read_bytes()
    stored = raw.decode("utf-8", errors="strict")
    assert item["source"] == "webpage" and item["name"].endswith(".txt")
    assert item["size"] == len(raw) == captured["attachment"]["size"]
    assert item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert captured["attachment"]["sha256"] == item["sha256"]
    assert stored.endswith(_TEXT) and _URL in stored and captured["scope"] in stored
    assert len(web_host.requests) == 1
    assert runtime.memory.source("attachment:" + identifier) is None

    first = await web_host.drive("attachment_read", id=identifier, offset_bytes=0)
    page = source_page(web_host)
    assert page.get("reading_page") and first["has_more"]
    assert first["content"] == raw[: first["next_offset_bytes"]].decode("utf-8")
    assert _TAIL not in first["content"]
    saved = await web_host.drive("reading_bookmark_set", **bookmark_arguments(item, page))
    assert "error" not in saved, saved
    assert saved["state"] == "active" and saved["generation"] == 1
    assert saved["read_arguments"] == {
        "id": identifier,
        "revision": item["sha256"],
        "offset_bytes": first["next_offset_bytes"],
    }
    position = await web_host.drive("reading_bookmark_read", id=identifier)
    assert position == saved
    parts = [first["content"]]
    offset = first["next_offset_bytes"]
    for _ in range(24):
        following = await web_host.drive(
            "attachment_read", id=identifier, revision=item["sha256"], offset_bytes=offset
        )
        assert "error" not in following, following
        assert following["offset_bytes"] == offset
        assert following["next_offset_bytes"] > offset
        parts.append(following["content"])
        offset = following["next_offset_bytes"]
        if not following["has_more"]:
            break
    else:
        pytest.fail("finite captured text did not reach EOF within 24 real pages")
    assert "".join(parts).encode("utf-8") == raw
    assert parts[-1].endswith(_TAIL)
    assert (await web_host.drive("reading_bookmark_read", id=identifier)) == saved
    assert len(web_host.requests) == 1
    assert "memory_import_attachment" in runtime.tools
    rejected = await web_host.drive("memory_import_attachment", id=identifier)
    assert rejected.get("error") and runtime.memory.source("attachment:" + identifier) is None
    assert "附件来源或副本元数据无法核查" in rejected["error"]
