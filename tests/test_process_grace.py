from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal cleanup")
def test_grace_drains_flood_while_term_handler_finishes(tmp_path: Path) -> None:
    closed = tmp_path / "closed"
    child = tmp_path / "child.py"
    child.write_text("""import os,signal,sys,time
from pathlib import Path
def close(sig: int, frame: object) -> None:
    os.write(1,b'y'*1000000)
    Path(sys.argv[1]).write_text('closed')
    raise SystemExit(0)
signal.signal(signal.SIGTERM,close)
os.write(1,b'x'*1000000)
time.sleep(10)
""")
    worker = """import asyncio,sys
from muli.process import run_process
async def main() -> None:
    try:
        await run_process([sys.executable,sys.argv[1],sys.argv[2]],limit=32,
                          timeout=2,terminate_grace=1)
    except ValueError:
        print('reaped')
    else:
        raise AssertionError('expected output limit')
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", worker, str(child), str(closed)],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "reaped"
    assert closed.read_text() == "closed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal cleanup")
def test_grace_hard_kills_uncooperative_process(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    worker = """import asyncio,sys
from muli.process import run_process
async def main() -> None:
    try:
        await run_process([sys.executable,'-c',
          "import os,signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(10)",
          sys.argv[1]],timeout=.3,terminate_grace=.2)
    except TimeoutError:
        print('reaped')
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", worker, str(pid)],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "reaped"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)
