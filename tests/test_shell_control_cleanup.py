"""Retiring a shell must close its actual persistent control connection."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
from pathlib import Path

from muli.shell_protocol import ShellSpec
from muli.shell_sessions import ShellSessions, _Shell


async def test_retire_closes_a_live_powershell_control_connection_before_waiting_for_server(
    tmp_path: Path,
) -> None:
    manager = ShellSessions(
        default_cwd=tmp_path,
        max_sessions=lambda: 2,
        max_output_bytes=lambda: 4096,
        max_command_bytes=lambda: 4096,
        max_run_seconds=lambda: 3,
    )
    temporary = tempfile.TemporaryDirectory(prefix="muli-control-test-", dir=tmp_path)
    shell = _Shell(
        "control-owner", ShellSpec(sys.executable, "pwsh"), temporary, str(tmp_path), state="idle"
    )
    manager._sessions[shell.session_id] = shell
    accepted_controls: list[asyncio.Task[None]] = []

    def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(manager._control(shell, reader, writer))
        accepted_controls.append(task)
        shell.controls.add(task)
        task.add_done_callback(shell.controls.discard)

    server = await asyncio.start_server(accepted, "127.0.0.1", 0)
    shell.server = server
    sockets = server.sockets
    assert sockets
    client_reader, client_writer = await asyncio.open_connection(
        "127.0.0.1", sockets[0].getsockname()[1]
    )
    try:
        client_writer.write(
            (
                json.dumps({"op": "ready", "secret": shell.secret, "epoch": shell.epoch}) + "\n"
            ).encode()
        )
        async with asyncio.timeout(1):
            await client_writer.drain()
            await shell.ready.wait()
        assert shell.control_writer is not None and not shell.control_writer.is_closing()
        assert len(accepted_controls) == 1 and not accepted_controls[0].done()

        # The client remains connected until the host retires it. A server's
        # wait_closed cannot release this connection on the host's behalf.
        cleanup = manager._retire(shell, "stopped")
        async with asyncio.timeout(1):
            await asyncio.shield(cleanup)
            assert await client_reader.read(1) == b""
        assert not shell.cleanup_failed and shell.state == "closed"
        assert not shell.controls and not shell.writers
        assert all(task.done() for task in accepted_controls)
        assert manager.live_sessions() == ()
        assert not server.is_serving()
    finally:
        # Release the peer even when the regression is red, so its failure
        # cannot leave the real listener or cleanup task alive in pytest.
        client_writer.close()
        with contextlib.suppress(ConnectionError):
            await asyncio.wait_for(client_writer.wait_closed(), 1)
        cleanup = manager._retire(shell, "stopped")
        await asyncio.wait_for(asyncio.shield(cleanup), 1)
        await asyncio.wait_for(server.wait_closed(), 1)
