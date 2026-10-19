"""Bound request bodies before JSON parsing, including chunked transfers."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = 1_048_576) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # The attachment handler applies its own streaming and total-storage limits.
        # Buffering uploads here would multiply memory use and cap all files at 1 MiB.
        if scope.get("path") == "/api/uploads" and scope.get("method") == "POST":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        data = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            data.extend(event.get("body", b""))
            if len(data) > self.max_bytes:
                await JSONResponse({"detail": "请求体超过限制"}, status_code=413)(
                    scope, receive, send
                )
                return
            if not event.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
