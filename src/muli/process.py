"""Cancelable subprocesses with finite time and output budgets."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any


async def run_process(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 15,
    limit: int = 24000,
    input_text: str | None = None,
    merge_stderr: bool = True,
) -> dict[str, Any]:
    if not argv or any("\x00" in arg for arg in argv):
        raise ValueError("命令参数无效")
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    output = bytearray()
    error = bytearray()
    truncated = False
    completed = False

    async def read_pipe(stream: asyncio.StreamReader, destination: bytearray) -> None:
        nonlocal truncated
        while chunk := await stream.read(4096):
            remaining = limit - len(output) - len(error)
            destination.extend(chunk[: max(0, remaining)])
            if len(chunk) > remaining:
                truncated = True
                raise ValueError("命令输出超过限制，已停止")

    async def write_input() -> None:
        if input_text is not None and process.stdin:
            process.stdin.write(input_text.encode())
            await process.stdin.drain()
            process.stdin.close()

    assert process.stdout is not None
    readers = [asyncio.create_task(read_pipe(process.stdout, output))]
    if process.stderr is not None:
        readers.append(asyncio.create_task(read_pipe(process.stderr, error)))
    readers.append(asyncio.create_task(write_input()))

    async def communicate() -> None:
        await asyncio.gather(*readers)
        await process.wait()

    try:
        await asyncio.wait_for(communicate(), timeout)
        completed = True
    finally:
        if not completed:
            try:
                if os.name == "posix":
                    # The parent can exit while descendants still retain its output pipe.
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
            except ProcessLookupError:
                pass
            for reader in readers:
                reader.cancel()

            async def settle() -> None:
                await asyncio.gather(*readers, return_exceptions=True)
                await process.wait()

            cleanup = asyncio.create_task(settle())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
    return {
        "exit_code": process.returncode,
        "output": output.decode(errors="replace"),
        "stderr": error.decode(errors="replace"),
        "truncated": truncated,
    }
