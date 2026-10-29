from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from muli.shell_sessions import ShellFinished, ShellSessions


@pytest.mark.skipif(os.name != "posix", reason="真实临时 POSIX shell")
@pytest.mark.parametrize("notify", [None, False], ids=["default", "false"])
async def test_plain_completed_receipt_allows_next_command_at_single_record_capacity(
    tmp_path: Path,
    notify: bool | None,
) -> None:
    manager = ShellSessions(
        default_cwd=tmp_path,
        max_sessions=lambda: 1,
        max_output_bytes=lambda: 6000,
        max_command_bytes=lambda: 6000,
        max_run_seconds=lambda: 4,
        max_receipt_chars=lambda: 6000,
    )
    options: dict[str, Any] = {} if notify is None else {"notify_on_finish": notify}
    try:
        first = await manager.execute(
            "owner", "echo FIRST", wait_seconds=0, admission_guard=lambda: None, **options
        )
        async with asyncio.timeout(3):
            while manager.status("owner")["state"] == "running":
                await asyncio.sleep(0)
        observed = await manager.read("owner", first["command_id"])
        assert observed["state"] == "completed" and observed["exit_code"] == 0
        second = await manager.execute(
            "owner", "echo SECOND", wait_seconds=1, admission_guard=lambda: None, **options
        )
        assert second["state"] == "completed" and second["exit_code"] == 0
        assert "SECOND" in second["output"] and second["os_epoch"] == first["os_epoch"]
    finally:
        await manager.aclose()


@pytest.mark.skipif(os.name != "posix", reason="真实临时 POSIX shell")
async def test_close_collects_the_terminal_cleanup_callback(tmp_path: Path) -> None:
    published = asyncio.Event()
    release = asyncio.Event()

    async def on_finish(result: ShellFinished) -> None:
        assert result.state == "stopped"
        published.set()
        await release.wait()

    manager = ShellSessions(
        default_cwd=tmp_path,
        max_sessions=lambda: 1,
        max_output_bytes=lambda: 6000,
        max_command_bytes=lambda: 6000,
        max_run_seconds=lambda: 4,
        on_finish=on_finish,
    )
    stopping: asyncio.Task[dict[str, Any]] | None = None
    closing: asyncio.Task[None] | None = None
    try:
        receipt = await manager.execute(
            "owner", "sleep 10", wait_seconds=0, notify_on_finish=True, admission_guard=lambda: None
        )
        assert receipt["state"] == "running"
        stopping = asyncio.create_task(manager.stop("owner"))
        async with asyncio.timeout(3):
            await published.wait()
        closing = asyncio.create_task(manager.aclose())
        completed, _ = await asyncio.wait({closing}, timeout=0.05)
        assert not completed, "关闭不能早于原 cleanup 所拥有的完成回调结束"
        assert not stopping.done()
        release.set()
        async with asyncio.timeout(3):
            await closing
            await stopping
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (closing, stopping) if task is not None), return_exceptions=True
        )
        await manager.aclose()
