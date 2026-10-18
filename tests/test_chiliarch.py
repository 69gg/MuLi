from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.integrations.chiliarch import (
    ChiliarchBridge,
    ChiliarchConfig,
    ChiliarchProtocolError,
    Envelope,
    _SSEDecoder,
)


class EventStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, remain_open: bool = True) -> None:
        self.chunks = chunks
        self.remain_open = remain_open
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.remain_open:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


def config(**changes: Any) -> ChiliarchConfig:
    return replace(
        ChiliarchConfig(
            base_url="https://chiliarch.example/platform",
            credential="test-credential",
            machine="test-machine",
            reconnect_initial_seconds=0.001,
            reconnect_max_seconds=0.002,
        ),
        **changes,
    )


def event_bytes(envelope: Envelope) -> bytes:
    return (
        "event: message\nid: " + envelope.id + "\ndata: " + json.dumps(envelope.to_dict()) + "\n\n"
    ).encode()


def receipt(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    ack = None
    if body["type"] != "ack":
        ack = Envelope(type="ack", payload={"message_id": body["id"]}).to_dict()
    return httpx.Response(200, json={"ack": ack, "duplicate": False})


async def unused_callback(_: Envelope) -> None:
    raise AssertionError("不应接收到业务消息")


@pytest.mark.parametrize("pairing", [True, False])
async def test_control_response_size_is_bounded_and_stream_is_closed(pairing: bool) -> None:
    stream = EventStream([b" " * 1024, b" "], remain_open=False)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bridge = ChiliarchBridge(config(max_message_bytes=1024), unused_callback, client=client)
        with pytest.raises(ChiliarchProtocolError, match="响应超过"):
            if pairing:
                await bridge.pair("once")
            else:
                await bridge.send(Envelope(type="heartbeat", payload={"status": "ready"}))
    assert stream.closed


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/test",
        "https://user:password@example.com",
        "https://example.com/?token=x",
        "https://example.com/#x",
        "https://example.com:bad",
        "https://exa mple.com",
    ],
)
def test_rejects_ambiguous_or_credentialled_base_urls(url: str) -> None:
    with pytest.raises(ValueError):
        config(base_url=url)


def test_pairing_preserves_prefix_and_requires_explicit_credential_storage() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "agent_id": "agent-1",
                    "credential": "fresh-credential",
                    "protocol_version": "1.0",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            bridge = ChiliarchBridge(config(credential=""), unused_callback, client=client)
            paired = await bridge.pair("one-time-code")
            assert paired.agent_id == "agent-1"
            assert paired.credential == bridge.config.credential == "fresh-credential"
            assert "fresh-credential" not in repr(paired)
            assert "fresh-credential" not in repr(bridge.config)
            assert not client.is_closed
        assert requests[0].url.path == "/platform/api/agent/pair"
        assert "Authorization" not in requests[0].headers
        assert json.loads(requests[0].content) == {
            "code": "one-time-code",
            "name": "MuLi",
            "machine": "test-machine",
            "capabilities": [],
        }

    asyncio.run(scenario())


def test_persist_before_ack_and_deduplicate_redelivery(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = sqlite3.connect(tmp_path / "inbox.db")
        database.execute("CREATE TABLE inbox(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        event = Envelope(type="agent.message", payload={"message": "任务完成", "metadata": {}})
        stream = EventStream([event_bytes(event), event_bytes(event)])
        acknowledgements: list[str] = []
        callbacks: list[str] = []
        bridge: ChiliarchBridge

        async def persist(envelope: Envelope) -> None:
            with database:
                database.execute(
                    "INSERT OR IGNORE INTO inbox VALUES (?, ?)",
                    (envelope.id, json.dumps(envelope.to_dict())),
                )
            callbacks.append(envelope.id)

        def handle(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer test-credential"
            if request.method == "GET":
                assert request.url.path == "/platform/api/agent/events"
                assert "last-event-id" not in request.headers
                return httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, stream=stream
                )
            body = json.loads(request.content)
            if body["type"] == "ack":
                message_id = body["payload"]["message_id"]
                assert (
                    database.execute(
                        "SELECT count(*) FROM inbox WHERE id = ?", (message_id,)
                    ).fetchone()[0]
                    == 1
                )
                acknowledgements.append(message_id)
                if len(acknowledgements) == 2:
                    bridge.stop()
            return receipt(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            bridge = ChiliarchBridge(config(), persist, client=client)
            await asyncio.wait_for(bridge.run(), 1)
            assert not client.is_closed
        assert acknowledgements == callbacks == [event.id, event.id]
        assert database.execute("SELECT count(*) FROM inbox").fetchone()[0] == 1
        assert stream.closed
        assert bridge.status == "stopped"
        database.close()

    asyncio.run(scenario())


def test_failed_persistence_is_not_acked_and_is_retried() -> None:
    async def scenario() -> None:
        event = Envelope(type="agent.message", payload={"message": "保存后确认"})
        attempts = 0
        connections = 0
        ack_count = 0
        bridge: ChiliarchBridge

        async def persist(_: Envelope) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("disk temporarily unavailable")

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal connections, ack_count
            if request.method == "GET":
                connections += 1
                assert ack_count == 0
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    stream=EventStream([event_bytes(event)]),
                )
            if json.loads(request.content)["type"] == "ack":
                assert attempts == 2
                ack_count += 1
                bridge.stop()
            return receipt(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            bridge = ChiliarchBridge(config(), persist, client=client)
            await asyncio.wait_for(bridge.run(), 1)
        assert attempts == connections == 2
        assert ack_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 404, 409, 413, 422, 302])
def test_permanent_http_errors_do_not_retry_or_forward_credentials(status: int) -> None:
    async def scenario() -> None:
        streams = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal streams
            assert request.url.host == "chiliarch.example"
            if request.method == "GET":
                streams += 1
                return httpx.Response(status, headers={"Location": "https://other.example/"})
            return receipt(request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as client:
            bridge = ChiliarchBridge(config(), unused_callback, client=client)
            with pytest.raises(ChiliarchProtocolError):
                await asyncio.wait_for(bridge.run(), 1)
        assert streams == 1
        assert bridge.status == "error"
        assert "test-credential" not in (bridge.last_error or "")

    asyncio.run(scenario())


def test_revocation_without_data_stops_the_stream() -> None:
    async def scenario() -> None:
        stream = EventStream([b"event: revoked\n\n"])

        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, stream=stream
                )
            return receipt(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            bridge = ChiliarchBridge(config(), unused_callback, client=client)
            with pytest.raises(ChiliarchProtocolError, match="吊销"):
                await asyncio.wait_for(bridge.run(), 1)
        assert stream.closed

    asyncio.run(scenario())


def test_stop_cancels_idle_connection_promptly() -> None:
    async def scenario() -> None:
        connected = asyncio.Event()
        stream = EventStream([b": connected\n\n"])

        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                connected.set()
                return httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, stream=stream
                )
            return receipt(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            bridge = ChiliarchBridge(config(), unused_callback, client=client)
            running = asyncio.create_task(bridge.run())
            await asyncio.wait_for(connected.wait(), 1)
            bridge.stop()
            await asyncio.wait_for(running, 0.5)
        assert stream.closed
        assert bridge.status == "stopped"

    asyncio.run(scenario())


def test_sse_decoder_handles_utf8_boundaries_multiline_and_all_newlines() -> None:
    payload = "\ufeff:连接\r\nevent: message\rdata: 第一行\r\ndata: 第二行\n\n".encode()
    decoder = _SSEDecoder(1024)
    frames = []
    for byte in payload:
        frames.extend(decoder.feed(bytes([byte])))
    assert [(frame.event, frame.data) for frame in frames] == [("message", "第一行\n第二行")]


def test_sse_decoder_limits_unterminated_lines() -> None:
    decoder = _SSEDecoder(1024)
    decoder.feed(b"data: " + b"x" * 1000)
    with pytest.raises(ChiliarchProtocolError, match="max_message_bytes"):
        decoder.feed(b"x" * 100)


def test_send_preserves_identity_and_rejects_unmatched_ack() -> None:
    async def scenario() -> None:
        sent: list[dict[str, Any]] = []
        envelope = Envelope(
            type="agent.request",
            payload={
                "request_id": "request-1",
                "operation": "list_agents",
                "arguments": {},
            },
        )

        def handle(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "ack": Envelope(type="ack", payload={"message_id": "other"}).to_dict(),
                    "duplicate": False,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            bridge = ChiliarchBridge(config(), unused_callback, client=client)
            with pytest.raises(ChiliarchProtocolError, match="不匹配"):
                await bridge.send(envelope)
            assert sent == [envelope.to_dict()]

    asyncio.run(scenario())


def test_envelope_rejects_unknown_protocol_and_naive_time() -> None:
    with pytest.raises(ChiliarchProtocolError, match="1.0"):
        Envelope(type="agent.message", payload={}, protocol_version="2.0")
    with pytest.raises(ChiliarchProtocolError, match="时区"):
        Envelope(type="agent.message", payload={}, timestamp="2026-01-01T00:00:00")
