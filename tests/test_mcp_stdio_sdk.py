"""Linux owned-process journey through the optional locked SDK/default factory.

No third-party server, socket, package runner or user configuration is used. The
peer has a 15-second watchdog, 12 requests and at most 12 KiB of stdout per process;
the journey has a 20-second deadline and the existing manager has a 4-second limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, NoReturn, TextIO, cast

import httpx
import pytest
from test_mcp import stdio_settings

from muli.tools import Tool
from muli.tools.mcp import MCPCandidate, MCPManager, MCPPrepareError


@pytest.mark.skipif(sys.platform != "linux", reason="owned stdio journey runs in Linux MCP CI")
async def test_locked_default_stdio_swap_drains_owned_process_and_failed_candidate_keeps_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastmcp", reason="requires the optional mcp extra")
    from anyio.abc import Process
    from mcp.client import stdio

    script = tmp_path / "owned_stdio_peer.py"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "mcp_stdio_peer.py", script)
    processes: list[Process] = []
    create_process = stdio._create_platform_compatible_process

    async def owned_process(
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        errlog: TextIO = sys.stderr,
        cwd: str | Path | None = None,
    ) -> Process:
        # Observe and retain real SDK handles; neither the client nor transport is faked.
        assert command == sys.executable and Path(cast(str, cwd)) == tmp_path
        assert args[:-1] == ["-I", "-S", "-u", str(script)]
        assert args[-1] in {"before", "after", "rejected"} and len(processes) < 3
        process = await create_process(command, args, env, errlog, cwd)
        processes.append(process)
        return cast(Process, process)

    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("owned stdio journey cannot use network or shell processes")

    monkeypatch.setattr(stdio, "_create_platform_compatible_process", owned_process)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)

    def config(argument: str, environment: str) -> dict[str, Any]:
        return stdio_settings(
            command=sys.executable,
            args=["-I", "-S", "-u", str(script), argument],
            env={"MULI_TEST_STDIO_GENERATION": environment},
            cwd=str(tmp_path),
        )

    async def identity(tool: Tool, *, wait: bool = False) -> dict[str, Any]:
        result = await tool.handler({"wait": wait})
        assert result["is_error"] is False, result
        return cast(dict[str, Any], json.loads(result["content"][0]["text"]))

    before_env, after_env = "owned-before-environment", "owned-after-environment"
    before_config = config("before", before_env)
    after_config = config("after", after_env)
    rejected_config = config("rejected", "owned-rejected-environment")
    manager = MCPManager(
        before_config,
        timeout=4,
        max_tools=1,
        max_pages=1,
        max_catalog_bytes=4096,
        max_output_chars=1024,
    )
    pending: asyncio.Task[dict[str, Any]] | None = None
    candidate: MCPCandidate | None = None
    release = tmp_path / "before.release"
    try:
        async with asyncio.timeout(20):
            old = (await manager.start())[0]
            before = await identity(old)
            assert before == {
                "pid": processes[0].pid,
                "argument": "before",
                "environment_sha256": hashlib.sha256(before_env.encode()).hexdigest(),
                "call": 1,
            }
            pending = asyncio.create_task(identity(old, wait=True))
            async with asyncio.timeout(2):
                while not (tmp_path / "before.entered").exists():
                    await asyncio.sleep(0.01)
            candidate = await manager.prepare(after_config)
            assert len(processes) == 2 and processes[0].pid != processes[1].pid
            assert (await candidate.tools[0].handler({}))["code"] == "generation_changed"
            assert not (tmp_path / "after.calls").exists()
            retired = manager.publish(candidate)
            candidate = None
            current = manager.tools[0]
            assert current.name == old.name and current is not old
            assert manager.draining == manager.pending_cleanup == 1
            assert processes[0].returncode is None and not pending.done()
            assert (await old.handler({}))["code"] == "generation_changed"
            after = await identity(current)
            assert after == {
                "pid": processes[1].pid,
                "argument": "after",
                "environment_sha256": hashlib.sha256(after_env.encode()).hexdigest(),
                "call": 1,
            }
            with pytest.raises(MCPPrepareError, match="^generation_draining$"):
                await manager.prepare(rejected_config)
            assert len(processes) == 2
            release.touch()
            assert await pending == {**before, "call": 2}
            assert await manager.retire(retired)
            assert processes[0].returncode == 0 and manager.pending_cleanup == 0
            with pytest.raises(MCPPrepareError, match="^server_unavailable$"):
                await manager.prepare(rejected_config)
            assert len(processes) == 3 and processes[2].returncode == 0
            assert manager.config == after_config and manager.tools[0] is current
            assert manager.status()[0]["status"] == "connected"
            assert (await identity(current)) == {**after, "call": 2}
            assert (tmp_path / "before.calls").read_text(encoding="ascii") == "2"
            assert not (tmp_path / "rejected.calls").exists()
            assert manager.pending_cleanup == 0 and manager.retirement_errors == []
    finally:
        release.touch(exist_ok=True)
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            try:
                if candidate is not None:
                    await manager.discard(candidate)
            finally:
                await manager.close()
        finally:
            leaked = [process for process in processes if process.returncode is None]
            for process in leaked:
                # Failure-only cleanup is confined to handles returned for this fixture.
                await stdio._terminate_process_tree(process, timeout_seconds=0.1)
                await asyncio.wait_for(process.wait(), 2)
            assert not leaked, "MCPManager failed to reap an owned stdio fixture"
    assert len(processes) == 3 and all(process.returncode == 0 for process in processes)
