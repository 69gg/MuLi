"""Own one disposable GTK fixture and observe its bounded independent events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from helpers import atspi_native_window as fixture

from muli.process import _OutputBudget, _settle_process, _wait_cleanup


def parse_state(data: bytes, pid: int, title: str) -> dict[str, Any]:
    if not data or len(data) > fixture.EVENT_BYTES or not data.endswith(b"\n"):
        raise AssertionError("Owned AT-SPI event was missing or exceeded its budget")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise AssertionError("Owned AT-SPI event was invalid") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "pid", "title", "clicks", "changes", "text", "password_unchanged"}
        or not isinstance(value["kind"], str)
        or value["kind"] not in {"ready", "button", "text"}
        or type(value["pid"]) is not int
        or value["pid"] != pid
        or value["title"] != title
        or any(
            type(value[key]) is not int or not 0 <= value[key] <= 4 for key in ("clicks", "changes")
        )
        or not isinstance(value["text"], str)
        or type(value["password_unchanged"]) is not bool
    ):
        raise AssertionError("Owned AT-SPI event did not match its exact process")
    return value


@dataclass
class NativeWindow:
    process: asyncio.subprocess.Process
    title: str
    budget: _OutputBudget
    stderr: asyncio.Task[None]

    async def state(self) -> dict[str, Any]:
        assert self.process.stdout is not None

        async def read() -> bytes:
            assert self.process.stdout is not None
            data = await self.process.stdout.readline()
            self.budget.charge(data)
            return data

        reading = asyncio.create_task(read())
        try:
            await asyncio.wait((reading, self.stderr), return_when=asyncio.FIRST_COMPLETED)
            if self.stderr.done():
                self.stderr.result()
            return parse_state(await reading, self.process.pid, self.title)
        finally:
            if not reading.done():
                reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)


@asynccontextmanager
async def owned_window(python: str, title: str) -> AsyncIterator[NativeWindow]:
    """Creation is owned before its first await, including timeout/cancel cleanup."""
    starting = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *fixture.window_arguments(python, title),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=fixture.EVENT_BYTES,
        )
    )
    readers: list[asyncio.Task[None]] = []
    try:
        async with asyncio.timeout(fixture.WINDOW_SECONDS):
            process = await asyncio.shield(starting)
            budget = _OutputBudget(fixture.EVENT_BYTES * 4)

            async def discard_stderr() -> None:
                assert process.stderr is not None
                while data := await process.stderr.read(fixture.EVENT_BYTES):
                    budget.charge(data)

            stderr = asyncio.create_task(discard_stderr())
            readers.append(stderr)
            window = NativeWindow(process, title, budget, stderr)
            ready = await window.state()
            assert ready == {
                "kind": "ready",
                "pid": process.pid,
                "title": title,
                "clicks": 0,
                "changes": 0,
                "text": "",
                "password_unchanged": True,
            }, "GTK must publish its own initial state before any computer request"
            yield window
            if stderr.done():
                stderr.result()
    finally:

        async def settle() -> None:
            try:
                adopted = await asyncio.shield(starting)
            except Exception:
                return
            await _settle_process(adopted, readers)
            assert adopted.returncode is not None, "Owned GTK process was not reaped"

        if await _wait_cleanup(asyncio.create_task(settle())):
            raise asyncio.CancelledError
