"""Parent exit is independent of a live descendant retaining both shell pipes."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from test_persistent_shell_output import published_child

import muli.shell_sessions as shell_module
from muli.shell_protocol import quote, select_shell
from muli.shell_sessions import ShellFinished, ShellSessions

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell process-group ownership")


@pytest.mark.parametrize("ending", ["lost", "close"])
async def test_parent_retirement_closes_inherited_pipes_without_releasing_the_child(
    tmp_path: Path, ending: str
) -> None:
    loop = asyncio.get_running_loop()
    accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        loop.create_future()
    )
    peers: list[asyncio.StreamWriter] = []

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peers.append(writer)
        if not accepted.done():
            accepted.set_result((reader, writer))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    sockets = server.sockets
    assert sockets
    effect = tmp_path / "must-not-run-after-gate.txt"
    script = tmp_path / "gated-child.py"
    script.write_text(
        "import os, socket\n"
        "from pathlib import Path\n"
        "\n"
        "def main() -> None:\n"
        f"    address: tuple[str, int] = ('127.0.0.1', {sockets[0].getsockname()[1]})\n"
        "    with socket.create_connection(address, timeout=3) as connection:\n"
        "        connection.settimeout(None)\n"
        "        os.write(1, b'CHILD_OUTPUT\\n')\n"
        "        os.write(2, b'CHILD_STDERR\\n')\n"
        "        connection.sendall(b'ready\\n')\n"
        "        if connection.recv(1) == b'g':\n"
        f"            Path({str(effect)!r}).write_text('released', encoding='utf-8')\n"
        "\n"
        "main()\n",
        encoding="utf-8",
    )
    spec = select_shell()
    manager = ShellSessions(
        default_cwd=tmp_path,
        executable=spec.executable,
        max_sessions=lambda: 2,
        max_output_bytes=lambda: 4096,
        max_command_bytes=lambda: 4096,
        max_run_seconds=lambda: 10,
    )
    observed: list[ShellFinished] = []
    try:
        command = f"{quote(sys.executable, spec.flavor)} -I {quote(str(script), spec.flavor)}"
        receipt = await manager.execute(
            "parent-owner", command, wait_seconds=0, admission_guard=lambda: None
        )
        item = manager._commands[receipt["command_id"]]
        shell = item.shell
        process = shell.process
        assert process is not None and shell.watcher is not None

        def finished(result: ShellFinished) -> None:
            assert shell.state == "closed" and item.done.is_set()
            assert shell.watcher is not None and shell.watcher.done()
            assert all(reader.done() for reader in shell.readers)
            assert process.returncode is not None
            observed.append(result)

        assert manager.watch_finish(shell.session_id, item.id, finished) is None
        async with asyncio.timeout(4):
            reader, _writer = await accepted
            assert await reader.readline() == b"ready\n"
            while item.output.observed_by_stream["output"] < len(
                b"CHILD_OUTPUT\n"
            ) or item.output.observed_by_stream["stderr"] < len(b"CHILD_STDERR\n"):
                item.changed.clear()
                if item.output.observed_by_stream["output"] >= len(
                    b"CHILD_OUTPUT\n"
                ) and item.output.observed_by_stream["stderr"] >= len(b"CHILD_STDERR\n"):
                    break
                await item.changed.wait()
            assert not item.done.is_set() and process.returncode is None
            if ending == "lost":
                process.kill()
            else:
                await manager.aclose()
            await item.done.wait()
            assert shell.cleanup is not None
            await asyncio.shield(shell.cleanup)
            # Nothing opens the gate: EOF proves the existing group cleanup
            # released the actual child that was retaining stdout and stderr.
            assert await reader.read(1) == b""
        expected = "unknown" if ending == "lost" else "stopped"
        assert item.state == expected and item.exit_code is None
        assert observed == [ShellFinished(shell.session_id, item.id, shell.epoch, expected, None)]
        assert not item.proof.is_set() and not effect.exists()
        assert not shell.cleanup_failed and manager.live_sessions() == ()
        result = await manager.read(shell.session_id, item.id)
        assert result["state"] == expected and result["exit_code"] is None
        assert result["output"] == "CHILD_OUTPUT\n" and result["stderr"] == "CHILD_STDERR\n"
    finally:
        await manager.aclose()
        for writer in peers:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


async def test_completed_commands_keep_the_same_shell_dispatch_pipe_until_close(
    tmp_path: Path,
) -> None:
    manager = ShellSessions(
        default_cwd=tmp_path,
        executable=select_shell().executable,
        max_sessions=lambda: 2,
        max_output_bytes=lambda: 4096,
        max_command_bytes=lambda: 4096,
        max_run_seconds=lambda: 10,
    )
    try:
        identity: tuple[str, int] | None = None
        async with asyncio.timeout(4):
            for text in ("FIRST_COMMAND", "SECOND_COMMAND"):
                receipt = await manager.execute(
                    "persistent-owner", f"echo {text}", wait_seconds=0, admission_guard=lambda: None
                )
                item = manager._commands[receipt["command_id"]]
                assert item.monitor is not None
                await asyncio.shield(item.monitor)
                shell = item.shell
                process = shell.process
                assert process is not None and process.stdin is not None
                assert item.state == "completed" and item.exit_code == 0
                assert shell.state == "idle" and not shell.lost.is_set()
                assert shell.watcher is not None and not shell.watcher.done()
                assert process.returncode is None and not process.stdin.is_closing()
                if identity is None:
                    identity = (shell.epoch, process.pid)
                assert identity == (shell.epoch, process.pid)
                assert (await manager.read(shell.session_id, item.id))["output"].strip() == text
            await manager.aclose()
            assert shell.watcher is not None and shell.watcher.done()
            await process.stdin.wait_closed()
            assert not shell.cleanup_failed and manager.live_sessions() == ()
    finally:
        await manager.aclose()


async def test_cleanup_aborts_a_real_flushing_input_before_the_shared_close_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (
        "import os\n"
        "\n"
        "def main() -> None:\n"
        "    read_fd: int\n"
        "    write_fd: int\n"
        "    read_fd, write_fd = os.pipe()\n"
        "    os.write(1, b'R')\n"
        "    os.read(read_fd, 1)\n"
        "\n"
        "main()\n"
    )
    async with published_child(source, tmp_path, [4096]) as (manager, item, process):
        assert process.stdout is not None and process.stdin is not None
        assert await process.stdout.readexactly(1) == b"R"
        shell = item.shell
        manager._sessions[shell.session_id] = shell
        shell.watcher = asyncio.create_task(manager._watch(shell))
        writer = process.stdin
        writer.write(b"x" * (1024 * 1024))
        assert writer.transport.get_write_buffer_size() > 0
        writer.close()
        closed = asyncio.create_task(writer.wait_closed())
        settlement_started = asyncio.Event()
        original_settlement = shell_module._settle_process

        async def settle_after_input_closes(
            owned: asyncio.subprocess.Process, tasks: list[asyncio.Task[None]]
        ) -> None:
            # The real flushing close below cannot complete while this child
            # ignores stdin. Early retirement must release it before the same
            # finite close deadline used by the existing process settlement.
            await asyncio.wait_for(asyncio.shield(closed), 1)
            settlement_started.set()
            await original_settlement(owned, tasks)

        try:
            ready, _pending = await asyncio.wait({closed, shell.watcher}, timeout=0.01)
            assert not ready and process.returncode is None
            assert writer.is_closing() and writer.transport.get_write_buffer_size() > 0
            with monkeypatch.context() as patch:
                patch.setattr(shell_module, "_settle_process", settle_after_input_closes)
                async with asyncio.timeout(2):
                    await asyncio.shield(manager._retire(shell, "stopped"))
                    await writer.wait_closed()
            assert settlement_started.is_set() and closed.done() and not closed.cancelled()
            assert shell.watcher.done() and not shell.watcher.cancelled()
            assert not shell.cleanup_failed and shell.state == "closed"
            assert item.state == "stopped" and item.done.is_set()
            assert process.returncode is not None and manager.live_sessions() == ()
        finally:
            manager._abort_input(process)
            await asyncio.gather(closed, return_exceptions=True)


@pytest.mark.parametrize("failure", [OSError, TimeoutError])
async def test_failed_settlement_collects_the_input_watcher_without_cancelling_the_close_future(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    source = "import os, sys\nos.write(1, b'R')\nsys.stdin.buffer.read(1)\n"
    async with published_child(source, tmp_path, [4096]) as (manager, item, process):
        assert process.stdout is not None and process.stdin is not None
        assert await process.stdout.readexactly(1) == b"R"
        shell = item.shell
        manager._sessions[shell.session_id] = shell
        waiting = asyncio.Event()
        original_wait_closed = process.stdin.wait_closed

        async def wait_closed() -> None:
            waiting.set()
            await original_wait_closed()

        async def fail_settlement(
            _process: asyncio.subprocess.Process, _tasks: list[asyncio.Task[None]]
        ) -> None:
            raise failure("owned process settlement failed before closing stdin")

        with monkeypatch.context() as patch:
            patch.setattr(process.stdin, "wait_closed", wait_closed)
            patch.setattr(shell_module, "_settle_process", fail_settlement)
            shell.watcher = asyncio.create_task(manager._watch(shell))
            async with asyncio.timeout(1):
                await waiting.wait()
                assert not shell.watcher.done() and process.returncode is None
                await asyncio.shield(manager._retire(shell, "unknown"))
                assert shell.cleanup_failed and shell.state == "blocked"
                assert shell.watcher.done() and not shell.watcher.cancelled()
                # The public waiter remains reusable: no cancelled shared
                # future and no invalid-state callback during pipe closure.
                await original_wait_closed()
            assert item.state == "unknown" and item.done.is_set()
