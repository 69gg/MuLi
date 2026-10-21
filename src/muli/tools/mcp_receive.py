"""Raw HTTP body fences before HTTPX decoding and the locked MCP parsers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx

from muli.mcp_policy import ReceivePolicy
from muli.mcp_receive_errors import ReceiveFailureCode


class ReceiveError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class ReceiveState:
    policy: ReceivePolicy
    code: ReceiveFailureCode | None = None
    failed: Callable[[], None] | None = field(default=None, repr=False)

    def fail(self, code: ReceiveFailureCode) -> ReceiveError:
        if self.code is None:
            self.code = code
            if self.failed is not None:
                try:
                    self.failed()
                except Exception:
                    # The latch must survive a failed status/retirement callback.
                    pass
        return ReceiveError(self.code)


class _BodyBudget:
    def __init__(self, state: ReceiveState, *, sse: bool) -> None:
        self.state, self.sse = state, sse
        self.size = 0
        self.line = False
        self.cr = False

    def _add(self, size: int) -> None:
        self.size += size
        if self.size > self.state.policy.max_message_bytes:
            raise self.state.fail("mcp_receive_budget_exceeded")

    def _newline(self) -> None:
        if not self.line:
            self.size = 0
        self.line = False

    def feed(self, data: bytes) -> None:
        if not self.sse:
            self._add(len(data))
            return
        for value in data:
            if self.cr:
                self.cr = False
                if value == 10:
                    # Count both bytes of CRLF in its original event, exactly once.
                    self._add(1)
                    self._newline()
                    continue
                self._newline()
            self._add(1)
            if value == 13:
                self.cr = True
            elif value == 10:
                self._newline()
            else:
                self.line = True


class _ReceiveStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, state: ReceiveState, *, sse: bool) -> None:
        self.stream, self.state = stream, state
        self.budget = _BodyBudget(state, sse=sse)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for data in self.stream:
                self.budget.feed(data)
                yield data
        except ReceiveError:
            raise
        except Exception:
            raise self.state.fail("mcp_receive_failed") from None

    async def aclose(self) -> None:
        await self.stream.aclose()


def install_receive_guard(
    client: httpx.AsyncClient, state: ReceiveState, *, streamable: bool
) -> None:
    """Add public HTTPX hooks; leave auth, proxy, redirects, timeouts and close owned by SDK."""

    async def request_hook(request: httpx.Request) -> None:
        # Per-request MCP headers may override client defaults, so fence at the hook.
        request.headers["Accept-Encoding"] = "identity"

    async def response_hook(response: httpx.Response) -> None:
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in {"", "identity"}:
            raise state.fail("mcp_receive_encoding_unsupported")
        media = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        method = response.request.method
        sse = (
            response.is_success
            and media == "text/event-stream"
            and (method == "GET" or (method == "POST" and streamable))
        )
        if sse:
            # httpx-sse 0.4.3 otherwise inherits an arbitrary advertised charset.
            charset = response.encoding
            if charset is None or charset.lower().replace("_", "-") not in {"utf-8", "utf8"}:
                raise state.fail("mcp_receive_charset_unsupported")
            response.encoding = "utf-8"
        else:
            size = response.headers.get("content-length")
            if size is not None:
                if len(size) > 20 or not size.isascii() or not size.isdecimal():
                    raise state.fail("mcp_receive_headers_invalid")
                if int(size) > state.policy.max_message_bytes:
                    raise state.fail("mcp_receive_budget_exceeded")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            raise state.fail("mcp_receive_stream_unsupported")
        response.stream = _ReceiveStream(response.stream, state, sse=sse)

    client.event_hooks["request"].append(request_hook)
    client.event_hooks["response"].append(response_hook)
