"""Cancelable subprocesses with finite time and output budgets."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
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
    env: dict[str, str] | None = None,
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
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        env=env,
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

                async def discard(stream: asyncio.StreamReader) -> None:
                    while await stream.read(4096):
                        pass

                # A paused StreamReader can prevent Process.wait() even after SIGKILL.
                # Discard buffered output, then close inherited pipes on a finite deadline.
                streams = [stream for stream in (process.stdout, process.stderr) if stream]
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(discard(stream) for stream in streams)), 1
                    )
                except Exception:
                    pass
                finally:
                    for stream in streams:
                        # StreamReader has no public close; close its pipe transport.
                        transport = getattr(stream, "_transport", None)
                        if transport is not None:
                            transport.close()
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
