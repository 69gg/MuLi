"""Cancelable subprocesses with finite time and output budgets."""

from __future__ import annotations

import asyncio
import math
import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class _OutputBudget:
    limit: int
    used: int = 0

    def charge(self, data: bytes) -> None:
        # Both pipes share one counter; charging contains no scheduling point.
        self.used += len(data)
        if self.used > self.limit:
            raise ValueError("命令输出超过限制，已停止")


async def _wait_cleanup(task: asyncio.Task[None]) -> bool:
    """Settle one strongly owned cleanup even through repeated observer cancel."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()  # Never leave an unobserved cleanup exception.
    return cancelled


async def _settle_process(
    process: asyncio.subprocess.Process,
    tasks: Sequence[asyncio.Task[Any]],
    *,
    terminate_grace: float = 0,
    eof_grace: float = 0,
) -> None:
    """Stop owned readers, drain both pipes and reap this process/group."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if process.stdin is not None:
        process.stdin.close()

    async def discard(stream: asyncio.StreamReader) -> None:
        while await stream.read(4096):
            pass

    streams = [stream for stream in (process.stdout, process.stderr) if stream]
    draining = asyncio.gather(*(discard(stream) for stream in streams))
    if eof_grace > 0 and process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), eof_grace)
        except TimeoutError:
            pass
    group_error: PermissionError | None = None
    if terminate_grace > 0:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            elif process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), terminate_grace)
        except (ProcessLookupError, TimeoutError):
            pass
        except PermissionError:
            # Still attempt hard cleanup and settle pipes before reporting failure.
            pass
    try:
        if os.name == "posix":
            # A departed parent may leave descendants retaining its output pipe.
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    except PermissionError as error:
        group_error = error
        if process.returncode is None:
            try:
                process.kill()
            except (ProcessLookupError, PermissionError):
                pass
    # A paused StreamReader can prevent Process.wait() even after SIGKILL.
    # Drain buffers, then close inherited pipes on a finite deadline.
    try:
        await asyncio.wait_for(draining, 1)
    except Exception:
        pass
    finally:
        for stream in streams:
            # StreamReader has no public close; keep this workaround in one place.
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                transport.close()
    if process.stdin is not None:
        try:
            await asyncio.wait_for(process.stdin.wait_closed(), 1)
        except (ConnectionError, TimeoutError):
            pass
    if group_error is None:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), 1)
    except TimeoutError as error:
        raise group_error from error
    if os.name == "posix":
        try:
            # Darwin can return EPERM for a group containing only zombies.
            # Reaping the child is not proof that all descendants are gone.
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            pass
    raise group_error


async def run_process(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 15,
    limit: int = 24000,
    input_text: str | None = None,
    merge_stderr: bool = True,
    env: dict[str, str] | None = None,
    terminate_grace: float = 0,
) -> dict[str, Any]:
    if not argv or any("\x00" in arg for arg in argv):
        raise ValueError("命令参数无效")
    if not math.isfinite(terminate_grace) or terminate_grace < 0:
        raise ValueError("进程清理宽限须为有限非负数")
    # Own creation before the first await. A late-returning handle after cancel
    # still belongs to this invocation and must be adopted and reaped.
    starting = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
            env=env,
        )
    )
    output, error = bytearray(), bytearray()
    budget = _OutputBudget(limit)
    completed = False
    readers: list[asyncio.Task[None]] = []

    async def read_pipe(stream: asyncio.StreamReader, destination: bytearray) -> None:
        while chunk := await stream.read(4096):
            budget.charge(chunk)
            destination.extend(chunk)

    try:
        process = await asyncio.shield(starting)

        async def write_input() -> None:
            if input_text is not None and process.stdin:
                process.stdin.write(input_text.encode())
                await process.stdin.drain()
                process.stdin.close()

        assert process.stdout is not None
        readers.append(asyncio.create_task(read_pipe(process.stdout, output)))
        if process.stderr is not None:
            readers.append(asyncio.create_task(read_pipe(process.stderr, error)))
        readers.append(asyncio.create_task(write_input()))

        async def communicate() -> None:
            await asyncio.gather(*readers)
            await process.wait()

        await asyncio.wait_for(communicate(), timeout)
        completed = True
    finally:
        if not completed:

            async def settle() -> None:
                try:
                    adopted = await asyncio.shield(starting)
                except Exception:
                    return  # Creation failed; the original exception remains active.
                await _settle_process(adopted, readers, terminate_grace=terminate_grace)

            cleanup = asyncio.create_task(settle())
            if await _wait_cleanup(cleanup):
                raise asyncio.CancelledError
    return {
        "exit_code": process.returncode,
        "output": output.decode(errors="replace"),
        "stderr": error.decode(errors="replace"),
        "truncated": False,
    }
