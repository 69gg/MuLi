"""Finite offline capture policies through the real owner tool and storage path."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from test_reading_bookmark_runtime import bookmark_arguments, source_page
from test_reading_excerpt_runtime import page_reference
from test_vision_archives_policy import Host
from test_web_capture_runtime import _TAIL, _TEXT, _URL, entry
from test_web_capture_runtime import web_host as web_host

from muli import attachments as attachment_module
from muli.providers import ModelTurn, ToolCall


def no_snapshot(host: Host) -> None:
    assert host.runtime.store.list("attachment") == []
    assert list(host.attachments.directory.iterdir()) == []
    assert host.attachments.reserved == 0


async def response_body(
    host: Host, monkeypatch: pytest.MonkeyPatch, raw: bytes, mime: str = "text/plain"
) -> None:
    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        host.requests.append(request)
        return httpx.Response(200, headers={"Content-Type": mime}, content=raw)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)


async def test_html_capture_records_static_scope_and_excludes_script_and_style(
    web_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    await response_body(
        web_host,
        monkeypatch,
        b"<html><style>STYLE_SECRET</style><script>SCRIPT_SECRET</script>"
        b"<nav>navigation</nav><p>body one</p><p>body two</p></html>",
        "text/html; charset=utf-8",
    )
    captured = await entry(web_host, "web_capture", url=_URL)
    assert captured["saved"] is True and captured["scope"] == "html_static_text_v1"
    raw = web_host.attachments.path(captured["attachment"]["id"]).read_bytes()
    assert raw.decode().endswith("navigation\nbody one\nbody two")
    assert b"STYLE_SECRET" not in raw and b"SCRIPT_SECRET" not in raw
    assert b"html_static_text_v1" in raw and len(web_host.requests) == 1


@pytest.mark.parametrize(
    ("raw", "mime"),
    [
        (b"invalid UTF8 \xff", "text/plain"),
        (b"binary\x00text", "text/plain"),
        (b" \r\n\t", "text/plain"),
        (b"<style>hidden</style><script>hidden</script>", "text/html"),
        (b"binary", "image/png"),
    ],
)
async def test_invalid_or_empty_response_never_leaves_an_attachment(
    web_host: Host, monkeypatch: pytest.MonkeyPatch, raw: bytes, mime: str
) -> None:
    await response_body(web_host, monkeypatch, raw, mime)
    result = await entry(web_host, "web_capture", url=_URL)
    assert result.get("error") and len(web_host.requests) == 1
    no_snapshot(web_host)


@pytest.mark.parametrize("boundary", ["download", "artifact", "quota", "receipt"])
async def test_each_complete_budget_rejects_without_saving_a_truncated_file(
    web_host: Host, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    url = _URL
    if boundary == "download":
        web_host.runtime.config.update({"limits": {"max_file_bytes": 1024}})
        await response_body(web_host, monkeypatch, b"x" * 1025)
    elif boundary == "artifact":
        web_host.runtime.config.update({"uploads": {"max_file_bytes": 1024}})
        await response_body(web_host, monkeypatch, b"x" * 900)
    elif boundary == "quota":
        web_host.runtime.config.update({"uploads": {"max_total_bytes": 1024}})
        await response_body(web_host, monkeypatch, b"x" * 1000)
    else:
        web_host.runtime.config.update({"limits": {"max_output_chars": 1000}})
        url += "/" + "x" * 800
    result = await entry(web_host, "web_capture", url=url)
    assert result.get("error"), result
    assert len(web_host.requests) == (0 if boundary == "receipt" else 1)
    no_snapshot(web_host)


async def test_credentials_and_private_redirect_are_refused_before_private_http(
    web_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = await entry(web_host, "web_capture", url="https://u:p@reading.example/article")
    assert credential.get("error") and not web_host.requests
    no_snapshot(web_host)

    async def resolve(host: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        address = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    async def redirect(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        web_host.requests.append(request)
        return httpx.Response(302, headers={"Location": "http://localhost/private"})

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", redirect)
    rejected = await entry(web_host, "web_capture", url=_URL)
    assert rejected.get("error") and len(web_host.requests) == 1
    assert web_host.requests[0].url.host == "93.184.216.34"
    no_snapshot(web_host)


@pytest.mark.parametrize(
    ("waiting", "change"),
    [
        ("dns", "new_input"),
        ("http", "new_input"),
        ("write", "new_input"),
        ("fsync", "disabled"),
        ("fsync", "file_limit"),
        ("fsync", "quota"),
        ("fsync", "output"),
        ("fsync", "mode"),
        ("capacity", "new_input"),
    ],
)
async def test_waiting_candidates_recheck_owner_configuration_and_commit_quota(
    web_host: Host, monkeypatch: pytest.MonkeyPatch, waiting: str, change: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original_io = attachment_module.file_io
    writes = 0

    async def held_io(operation: Callable[[], Any]) -> Any:
        nonlocal writes
        writes += 1
        if (waiting == "write" and writes == 1) or (waiting == "fsync" and writes == 2):
            entered.set()
            await release.wait()
        return await original_io(operation)

    async def resolve(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        if waiting == "dns":
            entered.set()
            await release.wait()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        web_host.requests.append(request)
        if waiting == "http":
            entered.set()
            await release.wait()
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=_TEXT.encode())

    monkeypatch.setattr(attachment_module, "file_io", held_io)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    if waiting == "capacity":
        await web_host.attachments.capacity.acquire()
    capturing = asyncio.create_task(entry(web_host, "web_capture", url=_URL))
    try:
        if waiting == "capacity":
            async with asyncio.timeout(3):
                while not web_host.requests:
                    await asyncio.sleep(0)
            entered.set()
        await asyncio.wait_for(entered.wait(), 3)
        if change == "new_input":
            queued = await web_host.runtime.submit("新要求：先别留存这篇网页", mode="yolo")
            assert queued["queued"]
        elif change == "disabled":
            web_host.runtime.config.update({"disabled_tools": ["web_capture"]})
        elif change == "file_limit":
            web_host.runtime.config.update({"uploads": {"max_file_bytes": 1024}})
        elif change == "quota":
            web_host.runtime.config.update({"uploads": {"max_total_bytes": 1024}})
        elif change == "output":
            web_host.runtime.config.update({"limits": {"max_output_chars": 1000}})
        else:
            session = web_host.runtime.main_session()
            web_host.runtime.store.put("session", {**session, "mode": "ask"})
        release.set()
        if waiting == "capacity":
            web_host.attachments.capacity.release()
        result = await asyncio.wait_for(capturing, 3)
    finally:
        release.set()
        if waiting == "capacity" and web_host.attachments.capacity.locked():
            web_host.attachments.capacity.release()
        if not capturing.done():
            await web_host.runtime.stop(web_host.runtime.main_session()["id"])
            await asyncio.gather(capturing, return_exceptions=True)
    assert result.get("error"), result
    assert len(web_host.requests) == (0 if waiting == "dns" else 1)
    no_snapshot(web_host)


async def test_file_failure_does_not_repeat_the_completed_download(
    web_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = attachment_module.file_io
    calls = 0

    async def broken_fsync(operation: Callable[[], Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("finite fsync failure")
        return await original(operation)

    monkeypatch.setattr(attachment_module, "file_io", broken_fsync)
    result = await entry(web_host, "web_capture", url=_URL)
    assert result.get("error") and calls == 2 and len(web_host.requests) == 1
    no_snapshot(web_host)


async def test_recapture_is_a_separate_snapshot_and_old_revision_cannot_drift(
    web_host: Host,
) -> None:
    first = await entry(web_host, "web_capture", url=_URL)
    identifier = first["attachment"]["id"]
    item = web_host.attachments.get(identifier)
    raw = web_host.attachments.path(identifier).read_bytes()
    page = await web_host.drive("attachment_read", id=identifier, offset_bytes=0)
    proof = source_page(web_host)
    saved = await web_host.drive("reading_bookmark_set", **bookmark_arguments(item, proof))
    assert saved["state"] == "active"
    second = await entry(web_host, "web_capture", url=_URL)
    assert second["attachment"]["id"] != identifier
    assert web_host.attachments.path(identifier).read_bytes() == raw
    assert (await web_host.drive("reading_bookmark_read", id=identifier)) == saved
    web_host.attachments.path(identifier).write_bytes(b"x" * len(raw))
    rejected = await web_host.drive(
        "attachment_read",
        id=identifier,
        revision=page["revision"],
        offset_bytes=page["next_offset_bytes"],
    )
    assert rejected.get("error")
    assert (await web_host.drive("reading_bookmark_read", id=identifier)) == saved
    assert len(web_host.requests) == 2


async def test_direct_handler_has_no_owner_authority(web_host: Host) -> None:
    with pytest.raises(PermissionError):
        await web_host.runtime.tools["web_capture"].handler({"url": _URL})
    assert not web_host.requests
    no_snapshot(web_host)


async def test_public_redirect_records_final_url_without_an_extra_resolution_at_connect(
    web_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolutions: list[str] = []

    async def resolve(host: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        resolutions.append(host)
        assert len(resolutions) <= 2, "连接必须使用已验证的固定地址"
        address = "93.184.216.34" if len(resolutions) == 1 else "1.1.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        web_host.requests.append(request)
        if len(web_host.requests) == 1:
            assert request.url.host == "93.184.216.34"
            return httpx.Response(302, headers={"Location": "https://final.example/material"})
        assert request.url.host == "1.1.1.1" and request.headers["host"] == "final.example"
        assert request.extensions["sni_hostname"] == "final.example"
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="final text")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    captured = await entry(web_host, "web_capture", url=_URL)
    assert captured["requested_url"] == _URL
    assert captured["final_url"] == "https://final.example/material"
    raw = web_host.attachments.path(captured["attachment"]["id"]).read_bytes()
    assert _URL.encode() in raw and captured["final_url"].encode() in raw
    assert raw.endswith(b"final text") and resolutions == ["reading.example", "final.example"]


async def test_capture_does_not_forward_set_cookie_across_a_same_host_redirect(
    web_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        web_host.requests.append(request)
        assert request.headers.get("cookie") is None
        if len(web_host.requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "Location": "/cookie-free-final",
                    "Set-Cookie": "reader_token=must-not-forward; Path=/; Secure",
                },
            )
        assert len(web_host.requests) == 2 and request.url.path == "/cookie-free-final"
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="public text")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    captured = await entry(web_host, "web_capture", url=_URL)
    assert captured["saved"] is True
    assert captured["final_url"] == "https://reading.example/cookie-free-final"
    assert len(web_host.requests) == 2


@pytest.mark.parametrize("userinfo", ["@", ":p@", "u:@", ":@"])
async def test_capture_rejects_even_empty_url_credentials_before_http(
    web_host: Host, userinfo: str
) -> None:
    result = await entry(web_host, "web_capture", url=f"https://{userinfo}reading.example/article")
    assert result.get("error") and not web_host.requests
    no_snapshot(web_host)


async def test_capture_revalidates_empty_credentials_on_redirect(
    web_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def redirect(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        web_host.requests.append(request)
        assert len(web_host.requests) == 1
        return httpx.Response(
            302, headers={"Location": "https://:@reading.example/credential-final"}
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", redirect)
    result = await entry(web_host, "web_capture", url=_URL)
    assert result.get("error") and len(web_host.requests) == 1
    no_snapshot(web_host)


@pytest.mark.parametrize("waiting", ["http", "write"])
async def test_stop_cancels_pending_capture_and_the_main_session_can_continue(
    web_host: Host, monkeypatch: pytest.MonkeyPatch, waiting: str
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original_io = attachment_module.file_io

    async def hold() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def held_io(operation: Callable[[], Any]) -> Any:
        if waiting == "write":
            await hold()
        return await original_io(operation)

    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        web_host.requests.append(request)
        if waiting == "http":
            await hold()
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="pending text")

    monkeypatch.setattr(attachment_module, "file_io", held_io)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    web_host.turns.append(
        ModelTurn(
            tool_calls=[ToolCall("stopped-capture", "web_capture", json.dumps({"url": _URL}))]
        )
    )
    receipt = await web_host.runtime.submit("明确留存网页，等待期间停止", mode="yolo")
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(web_host.runtime.stop(receipt["session_id"]), 3)
    assert cancelled.is_set()
    assert web_host.runtime.store.get("run", receipt["run_id"])["status"] == "cancelled"
    no_snapshot(web_host)
    web_host.turns.append(ModelTurn(content="停止后的主会话仍可正常继续。"))
    following = await web_host.runtime.submit("继续正常对话", mode="yolo")
    assert await asyncio.wait_for(web_host.runtime.wait(following["run_id"]), 3) == (
        "停止后的主会话仍可正常继续。"
    )


async def test_independent_session_cannot_capture_for_the_owner(web_host: Host) -> None:
    runtime = web_host.runtime
    independent = runtime.new_session(mode="yolo")
    web_host.turns.extend(
        [
            ModelTurn(
                tool_calls=[ToolCall("other-capture", "web_capture", json.dumps({"url": _URL}))]
            ),
            ModelTurn(content="独立会话没有主人的网页留存入口。"),
        ]
    )
    receipt = await runtime.start(independent["id"], "试图留存网页", mode="yolo")
    await asyncio.wait_for(runtime.wait(receipt["run_id"]), 3)
    stored = next(
        item
        for item in runtime.store.messages(independent["id"])
        if item.get("tool_call_id") == "other-capture" and item["role"] == "tool"
    )
    assert json.loads(stored["content"]).get("error")
    assert not web_host.requests
    no_snapshot(web_host)


async def test_only_actual_captured_page_text_can_be_saved_as_an_excerpt(web_host: Host) -> None:
    captured = await entry(web_host, "web_capture", url=_URL)
    identifier = captured["attachment"]["id"]
    first = await web_host.drive("attachment_read", id=identifier, offset_bytes=0)
    proof = source_page(web_host)
    reference = await page_reference(web_host, proof)
    assert _TAIL not in first["content"]
    rejected = await web_host.drive("reading_excerpt_save", **reference, quote=_TAIL)
    assert rejected.get("error") and rejected["code"] == "excerpt_quote_unavailable"
    quote = _TEXT[:12]
    assert quote in first["content"]
    saved = await web_host.drive(
        "reading_excerpt_save", **reference, quote=quote, note="本地网页资料"
    )
    assert saved["quote"] == quote and saved["source_page"]["message_id"] == proof["id"]
    assert (await web_host.drive("reading_bookmark_read", id=identifier))["state"] == "empty"
    assert len(web_host.requests) == 1
