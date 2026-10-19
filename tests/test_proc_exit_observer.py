"""The cleanup test must accept /proc removal without masking a live process."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import test_safety as safety


@pytest.mark.parametrize("state", ["gone", "Z", "S", "permission"])
def test_descendant_exit_observer_handles_removal_and_preserves_other_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    # Only the post-timeout observer is controlled here. The original test still
    # runs a real parent/descendant and keeps its timeout and liveness assertions.
    stat = Path("/proc/424242/stat")
    real_exists, real_read = Path.exists, Path.read_text
    reads = 0

    async def timed_out(command: list[str], *, timeout: float) -> None:
        assert timeout == 0.15
        Path(command[3]).write_text("424242", encoding="utf-8")
        raise TimeoutError("process cleanup already completed")

    def exists(path: Path) -> bool:
        return True if path == stat else real_exists(path)

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal reads
        if path != stat:
            return real_read(path, *args, **kwargs)
        reads += 1
        if state == "gone":
            # The old exists() observation succeeded, but reaping removed the
            # entry before open(), exactly as in the release CI traceback.
            raise FileNotFoundError(str(stat))
        if state == "permission":
            raise PermissionError(str(stat))
        return f"424242 (python) {state}"

    async def no_delay(delay: float) -> None:
        assert delay == 0.005

    monkeypatch.setattr(safety, "run_process", timed_out)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(safety.asyncio, "sleep", no_delay)

    if state == "permission":
        with pytest.raises(PermissionError):
            safety.test_process_timeout_kills_descendant_after_parent_exits(tmp_path)
    elif state == "S":
        with pytest.raises(pytest.fail.Exception, match="子进程在超时后仍在运行"):
            safety.test_process_timeout_kills_descendant_after_parent_exits(tmp_path)
        assert reads == 100
    else:
        safety.test_process_timeout_kills_descendant_after_parent_exits(tmp_path)
        assert reads == 1
