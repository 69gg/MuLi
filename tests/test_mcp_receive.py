from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, NoReturn

import httpx
import pytest

from muli.mcp_policy import MIN_MESSAGE_BYTES, ReceivePolicy
from muli.tools.mcp_receive import ReceiveError, ReceiveState, install_receive_guard

LIMIT = MIN_MESSAGE_BYTES


def event_source(response: httpx.Response) -> Any:
    # JSON/Runtime fake tests stay collectible without the optional MCP extra.
    return pytest.importorskip("httpx_sse").EventSource(response)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("receive tests must only use MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.reads = 0
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for value in self.chunks:
            self.reads += 1
            yield value

    async def aclose(self) -> None:
        self.closed += 1


def guarded(
    stream: httpx.AsyncByteStream,
    *,
    streamable: bool = True,
    headers: dict[str, str] | None = None,
    status: int = 200,
) -> tuple[httpx.AsyncClient, ReceiveState]:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(status, headers=headers, stream=stream)

    state = ReceiveState(ReceivePolicy(max_message_bytes=LIMIT))
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    install_receive_guard(client, state, streamable=streamable)
    return client, state


@pytest.mark.parametrize("length", [LIMIT, LIMIT + 1], ids=["exact", "over"])
async def test_json_bytes_are_limited_before_aread_without_content_length(length: int) -> None:
    stream = Chunks(b"x" * (LIMIT // 2), b"x" * (length - LIMIT // 2))
    client, state = guarded(stream, headers={"content-type": "application/json"})
    async with client:
        if length <= LIMIT:
            response = await client.post("https://fixture.invalid/mcp")
            assert len(response.content) == LIMIT and state.code is None
        else:
            with pytest.raises(ReceiveError, match="^mcp_receive_budget_exceeded$"):
                await client.post("https://fixture.invalid/mcp")
            assert state.code == "mcp_receive_budget_exceeded"
    assert stream.closed == 1


@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"], ids=["lf", "cr", "crlf"])
async def test_sse_resets_only_at_events_across_every_delimiter_split(newline: bytes) -> None:
    # Each event is valid; their combined body exceeds the message budget.
    prefix = b"data: " + b"x" * (LIMIT // 2)
    # Put every delimiter byte in its own transport chunk, including CRLF.
    chunks = tuple(part for _ in range(3) for part in (prefix, *(bytes([c]) for c in newline * 2)))
    stream = Chunks(*chunks)
    client, state = guarded(
        stream, headers={"content-type": "text/event-stream", "content-length": str(LIMIT * 3)}
    )
    async with client, client.stream("GET", "https://fixture.invalid/mcp") as response:
        events = [item async for item in event_source(response).aiter_sse()]
    assert [item.data for item in events] == ["x" * (LIMIT // 2)] * 3
    assert state.code is None and stream.closed == 1


@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"], ids=["lf", "cr", "crlf"])
@pytest.mark.parametrize("extra", [0, 1], ids=["exact", "over"])
async def test_sse_budget_includes_the_entire_event_separator(newline: bytes, extra: int) -> None:
    prefix = b"data:" + b"x" * (LIMIT + extra - 5 - len(newline) * 2)
    stream = Chunks(prefix, *(bytes([c]) for c in newline * 2))
    client, state = guarded(stream, headers={"content-type": "text/event-stream"})
    async with client:
        if extra:
            with pytest.raises(ReceiveError, match="^mcp_receive_budget_exceeded$"):
                async with client.stream("GET", "https://fixture.invalid/mcp") as response:
                    _ = [data async for data in response.aiter_raw()]
            assert state.code == "mcp_receive_budget_exceeded"
        else:
            async with client.stream("GET", "https://fixture.invalid/mcp") as response:
                data = b"".join([data async for data in response.aiter_raw()])
            assert len(data) == LIMIT and data == prefix + newline * 2
            assert state.code is None
    assert stream.closed == 1


@pytest.mark.parametrize("delimiter", [b"\n", b"\r\n"], ids=["lf", "crlf"])
async def test_multiline_sse_is_one_budget_not_one_per_line(delimiter: bytes) -> None:
    stream = Chunks(
        b"data: " + b"x" * (LIMIT // 2) + delimiter, b"data: " + b"y" * (LIMIT // 2) + delimiter * 2
    )
    client, state = guarded(stream, headers={"content-type": "text/event-stream"})
    async with client:
        with pytest.raises(ReceiveError, match="^mcp_receive_budget_exceeded$"):
            async with client.stream("GET", "https://fixture.invalid/mcp") as response:
                _ = [item async for item in event_source(response).aiter_sse()]
    assert state.code == "mcp_receive_budget_exceeded"


async def test_unterminated_sse_line_is_rejected_before_text_decoder_receives_overflow() -> None:
    stream = Chunks(b"data:" + b"x" * (LIMIT - 5), b"x", b"must not read")
    client, state = guarded(stream, headers={"content-type": "text/event-stream"})
    async with client:
        with pytest.raises(ReceiveError, match="^mcp_receive_budget_exceeded$"):
            async with client.stream("GET", "https://fixture.invalid/mcp") as response:
                iterator = response.aiter_raw()
                assert await anext(iterator) == b"data:" + b"x" * (LIMIT - 5)
                # The SDK may retain the first line; the overflowing byte is never yielded.
                await anext(iterator)
    assert stream.reads == 2 and state.code == "mcp_receive_budget_exceeded"


async def test_crlf_is_not_misread_as_two_empty_lines() -> None:
    # Treating LF after CR as another blank line would incorrectly reset here.
    stream = Chunks(b"data:" + b"x" * (LIMIT - 8) + b"\r", b"\n", b"y" * 8)
    client, state = guarded(stream, headers={"content-type": "text/event-stream"})
    async with client:
        with pytest.raises(ReceiveError, match="^mcp_receive_budget_exceeded$"):
            async with client.stream("GET", "https://fixture.invalid/mcp") as response:
                _ = [item async for item in event_source(response).aiter_sse()]
    assert state.code is not None


@pytest.mark.parametrize("prefix", [b":", b"future:"], ids=["comment", "unknown-field"])
async def test_ignored_sse_fields_still_count_against_the_event(prefix: bytes) -> None:
    stream = Chunks(prefix + b"x" * LIMIT)
    client, _ = guarded(stream, headers={"content-type": "text/event-stream"})
    async with client:
        with pytest.raises(ReceiveError, match="^mcp_receive_budget_exceeded$"):
            async with client.stream("GET", "https://fixture.invalid/mcp") as response:
                _ = [item async for item in event_source(response).aiter_sse()]


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip"])
async def test_compressed_response_is_refused_before_any_raw_byte_is_read(encoding: str) -> None:
    stream = Chunks(b"untrusted compressed data")
    client, state = guarded(stream, headers={"content-encoding": encoding})
    async with client:
        with pytest.raises(ReceiveError, match="^mcp_receive_encoding_unsupported$"):
            await client.get("https://fixture.invalid/mcp")
    assert stream.reads == 0 and stream.closed == 1
    assert state.code == "mcp_receive_encoding_unsupported"


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"content-length": str(LIMIT + 1)}, "mcp_receive_budget_exceeded"),
        ({"content-length": "-1"}, "mcp_receive_headers_invalid"),
        ({"content-type": "text/event-stream; charset=utf-16"}, "mcp_receive_charset_unsupported"),
    ],
)
async def test_header_prechecks_do_not_read_body(headers: dict[str, str], code: str) -> None:
    stream = Chunks(b"not read")
    client, state = guarded(stream, headers=headers)
    async with client:
        with pytest.raises(ReceiveError, match=f"^{code}$"):
            await client.get("https://fixture.invalid/mcp")
    assert state.code == code and stream.reads == 0 and stream.closed == 1


@pytest.mark.parametrize(
    "method,streamable,status",
    [
        ("POST", False, 200),
        ("DELETE", True, 200),
        ("GET", True, 400),
    ],
    ids=["legacy-sse-post", "delete", "error-body"],
)
async def test_nonstreaming_sse_bodies_cannot_reset_the_total_budget(
    method: str, streamable: bool, status: int
) -> None:
    stream = Chunks(*([b"data: " + b"x" * (LIMIT // 2) + b"\n\n"] * 3))
    client, state = guarded(
        stream, streamable=streamable, status=status, headers={"content-type": "text/event-stream"}
    )
    async with client:
        with pytest.raises(ReceiveError, match="^mcp_receive_budget_exceeded$"):
            await client.request(method, "https://fixture.invalid/mcp")
    assert state.code == "mcp_receive_budget_exceeded" and stream.reads == 2


async def test_cancelled_stream_does_not_latch_a_budget_failure() -> None:
    entered = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self) -> None:
            self.closed = True

    stream = Waiting()
    client, state = guarded(stream)
    async with client:
        task = asyncio.create_task(client.get("https://fixture.invalid/mcp"))
        try:
            async with asyncio.timeout(2):
                await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert state.code is None and stream.closed


async def test_locked_sdk_background_get_swallows_error_but_not_the_failure_latch() -> None:
    import anyio

    pytest.importorskip("mcp")
    from mcp.client.streamable_http import StreamableHTTPTransport

    stream = Chunks(b":" + b"x" * LIMIT)
    client, state = guarded(stream, headers={"content-type": "text/event-stream"})
    sdk = StreamableHTTPTransport("https://fixture.invalid/mcp")
    sdk.session_id = "fixture-session"
    writer, reader = anyio.create_memory_object_stream[Any](1)
    async with client, writer, reader:
        # The pinned SDK returns normally from this failure: callers need the latch.
        await sdk.handle_get_stream(client, writer)
    assert state.code == "mcp_receive_budget_exceeded" and stream.closed == 1


async def test_locked_sdk_json_handler_receives_fixed_failure_before_json_parse() -> None:
    import anyio

    pytest.importorskip("mcp")
    from mcp.client.streamable_http import StreamableHTTPTransport

    stream = Chunks(b"x" * LIMIT, b"x")
    client, state = guarded(stream, headers={"content-type": "application/json"})
    sdk = StreamableHTTPTransport("https://fixture.invalid/mcp")
    writer, reader = anyio.create_memory_object_stream[Any](1)
    async with client, writer, reader, client.stream("POST", sdk.url) as response:
        await sdk._handle_json_response(response, writer)
        error = await reader.receive()
    assert isinstance(error, ReceiveError) and error.code == "mcp_receive_budget_exceeded"
    assert state.code == error.code


async def test_locked_sdk_streamable_post_uses_per_event_budget_not_total_body() -> None:
    import anyio

    pytest.importorskip("mcp")
    from mcp.client.streamable_http import RequestContext, StreamableHTTPTransport
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    comments = (b":" + b"x" * (LIMIT // 2) + b"\n\n",) * 3
    reply = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    stream = Chunks(*comments, reply)
    client, state = guarded(stream, headers={"content-type": "text/event-stream"})
    sdk = StreamableHTTPTransport("https://fixture.invalid/mcp")
    writer, reader = anyio.create_memory_object_stream[Any](1)
    request = JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list")
    async with client, writer, reader:
        await sdk._handle_post_request(
            RequestContext(
                client=client,
                headers={},
                session_id="fixture",
                session_message=SessionMessage(JSONRPCMessage(root=request)),
                metadata=None,
                read_stream_writer=writer,
                sse_read_timeout=2,
            )
        )
        received = await reader.receive()
    assert isinstance(received, SessionMessage) and received.message.root.id == 1
    assert state.code is None and stream.closed == 1
