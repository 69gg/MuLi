from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from muli.process import _settle_process, run_process


@pytest.mark.parametrize("descriptor", [1, 2])
def test_output_flood_cleanup_finishes_with_external_watchdog(descriptor: int) -> None:
    """A runner regression must fail in finite time, even if its cleanup ignores cancel."""
    source = Path(__file__).resolve().parents[1] / "src"
    child = f"import os,time; os.write({descriptor}, b'x'*1000000); time.sleep(10)"
    worker = """
import asyncio
import sys
from muli.process import run_process

async def main() -> None:
    try:
        await run_process([sys.executable, '-c', sys.argv[1]], limit=32,
                          timeout=1, merge_stderr=False)
    except ValueError as error:
        assert '输出超过' in str(error)
        print('reaped')
    else:
        raise AssertionError('flood should exceed output limit')

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", worker, child],
        env=dict(os.environ, PYTHONPATH=str(source)),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "reaped"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cleanup")
@pytest.mark.parametrize("terminate_grace", [0, 0.01])
async def test_exited_group_permission_race_preserves_output_error(
    monkeypatch: pytest.MonkeyPatch, terminate_grace: float
) -> None:
    original = os.killpg
    signals: list[int] = []

    def deny_signal_until_reaped(pid: int, sig: int) -> None:
        signals.append(sig)
        if sig != 0:
            raise PermissionError("simulated Darwin zombie group")
        # Real kernel proof after the owned child is reaped, not a fake ESRCH.
        original(pid, sig)

    monkeypatch.setattr(os, "killpg", deny_signal_until_reaped)
    with pytest.raises(ValueError, match="输出超过"):
        await run_process(
            [sys.executable, "-c", "print('x' * 100)"],
            limit=32,
            terminate_grace=terminate_grace,
        )
    assert signal.SIGKILL in signals
    assert signals[-1] == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cleanup")
@pytest.mark.parametrize("probe", ["present", "denied", "gone", "unreapable"])
async def test_group_permission_failure_finishes_cleanup_before_reporting(
    monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    class Pipe:
        closed = False

        def close(self) -> None:
            self.closed = True

    pipe = Pipe()
    reader = asyncio.StreamReader()
    reader.set_transport(cast(asyncio.ReadTransport, pipe))
    reader.feed_data(b"remaining buffered output")
    reader.feed_eof()

    class Child:
        pid = 123456789
        returncode: int | None = None
        stdin = None
        stdout = reader
        stderr = None
        killed = False
        reaped = False

        def kill(self) -> None:
            self.killed = True
            if probe != "unreapable":
                self.returncode = -signal.SIGKILL

        async def wait(self) -> int:
            if probe == "unreapable":
                await asyncio.Event().wait()
            assert self.returncode is not None
            self.reaped = True
            return self.returncode

    child = Child()
    denied = PermissionError("owned group signal denied")
    signals: list[int] = []

    def kill_group(pid: int, sig: int) -> None:
        assert pid == child.pid
        signals.append(sig)
        if sig != 0:
            raise denied
        assert child.reaped and pipe.closed
        if probe == "gone":
            raise ProcessLookupError
        if probe == "denied":
            raise PermissionError("probe also denied")

    monkeypatch.setattr(os, "killpg", kill_group)
    process = cast(asyncio.subprocess.Process, child)
    if probe == "gone":
        await _settle_process(process, ())
    else:
        with pytest.raises(PermissionError) as failure:
            await _settle_process(process, ())
        assert failure.value is denied
    assert child.killed and pipe.closed and reader.at_eof()
    assert child.reaped is (probe != "unreapable")
    assert signals == ([signal.SIGKILL] if probe == "unreapable" else [signal.SIGKILL, 0])
