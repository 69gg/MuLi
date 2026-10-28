"""Own and settle a finite operation when its HTTP caller disconnects."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from fastapi import HTTPException, Request


async def request_operation[Result](
    request: Request,
    pending: Coroutine[Any, Any, Result],
    *,
    cancelled_message: str = "操作已取消",
) -> Result:
    async def disconnected() -> None:
        # JSON body has already been consumed; wait on ASGI, not periodic polling.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    operation = asyncio.create_task(pending)
    disconnect = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((operation, disconnect), return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            raise HTTPException(499, cancelled_message)
        return await operation
    finally:
        operation.cancel()
        disconnect.cancel()
        cleanup = asyncio.gather(operation, disconnect, return_exceptions=True)
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
