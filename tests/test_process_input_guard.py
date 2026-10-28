"""The optional input fence runs with an owned process and no written bytes."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from muli.process import _settle_process, run_process


@pytest.mark.parametrize("binary", [False, True])
async def test_input_guard_runs_once_and_preserves_actual_stdin(binary: bool) -> None:
    guarded: list[bool] = []
    payload = b"\x00\xffrequest" if binary else "请求".encode()
    result = await run_process(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"],
        **({"input_bytes": payload} if binary else {"input_text": payload.decode()}),
        input_guard=lambda: guarded.append(True),
        timeout=2,
        limit=100,
    )
    assert guarded == [True]
    assert result["exit_code"] == 0 and result["output"].strip() == payload.hex()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal cleanup and owned PID check")
@pytest.mark.parametrize("cancel", [False, True])
async def test_rejected_input_guard_delivers_zero_bytes_and_reaps_real_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel: bool
) -> None:
    receipt = tmp_path / "received.bin"
    source = """import signal,sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('ready', flush=True)
Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())
"""
    original = asyncio.create_subprocess_exec
    created: list[asyncio.subprocess.Process] = []
    guards: list[int] = []
    failure = asyncio.CancelledError() if cancel else ValueError("input denied")

    async def start(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        created.append(process)
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 1) == b"ready\n"
        return process

    def guard() -> None:
        process = created[0]
        assert process.returncode is None and process.stdin is not None
        guards.append(process.pid)
        raise failure

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    try:
        with pytest.raises(type(failure)) as rejected:
            await run_process(
                [sys.executable, "-c", source, str(receipt)],
                input_text="must never reach the child",
                input_guard=guard,
                timeout=2,
                limit=100,
                terminate_grace=0.5,
            )
        assert rejected.value is failure
        assert len(created) == 1 and guards == [created[0].pid]
        assert receipt.read_bytes() == b""
        assert created[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(created[0].pid, 0)
    finally:
        for process in created:
            if process.returncode is None:
                await _settle_process(process, (), terminate_grace=0.5)
