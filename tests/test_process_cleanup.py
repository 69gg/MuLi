from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


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
