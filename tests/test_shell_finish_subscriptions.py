"""Exact finish observers share the existing pipe monitor and cleanup owner."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from test_persistent_shell_output import published_child

from muli.shell_protocol import marker
from muli.shell_sessions import (
    FinishCallback,
    ShellFinished,
    ShellFinishObserver,
    ShellSessionError,
    ShellSessions,
    _Command,
    _Output,
)

_CONTROLLED_SOURCE = (
    "import json, os, socket, sys\n"
    "\n"
    "def main() -> None:\n"
    "    config: dict[str, object] = json.loads(sys.stdin.buffer.readline())\n"
    "    fence: bytes = bytes.fromhex(str(config['fence']))\n"
    "    os.write(1, b'R')\n"
    "    while action := sys.stdin.buffer.read(1):\n"
    "        if action == b'p':\n"
    "            proof: dict[str, object] = {\n"
    "                'op': 'complete', 'secret': config['secret'],\n"
    "                'epoch': config['epoch'], 'command_id': config['command_id'],\n"
    "                'exit_code': 7, 'cwd': config['cwd'],\n"
    "            }\n"
    "            with socket.create_connection(('127.0.0.1', int(str(config['port']))),\n"
    "                                          timeout=1) as connection:\n"
    "                connection.sendall((json.dumps(proof) + '\\n').encode())\n"
    "                if connection.recv(1) != b'1':\n"
    "                    raise RuntimeError('completion identity rejected')\n"
    "        elif action == b'o':\n"
    "            os.write(1, b'PRIVATE_OUTPUT' + fence)\n"
    "        elif action == b'e':\n"
    "            os.write(2, b'PRIVATE_STDERR' + fence)\n"
    "\n"
    "main()\n"
)


@asynccontextmanager
async def controlled_command(
    tmp_path: Path,
    *,
    on_finish: FinishCallback | None = None,
    notify: bool = False,
    timeout: float = 2,
) -> AsyncIterator[tuple[ShellSessions, _Command, asyncio.subprocess.Process]]:
    # Reuse the existing bounded real child fixture. Only the producer is
    # controlled here: real authenticated control bytes and both actual pipe
    # fences still pass through ShellSessions' own readers and monitor.
    async with published_child(_CONTROLLED_SOURCE, tmp_path, [4096]) as (
        manager,
        item,
        process,
    ):
        shell = item.shell
        del manager._commands[item.id]
        item.id = secrets.token_hex(16)
        manager._retain_command(item)
        manager._sessions[shell.session_id] = shell
        manager._on_finish = on_finish
        item.notify_on_finish = notify
        item.dispatched = True

        def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.create_task(manager._control(shell, reader, writer))
            shell.controls.add(task)
            task.add_done_callback(shell.controls.discard)

        try:
            shell.server = await asyncio.start_server(accepted, "127.0.0.1", 0)
            sockets = shell.server.sockets
            assert sockets and process.stdin is not None
            assert process.stdout is not None and process.stderr is not None
            config: dict[str, object] = {
                "port": sockets[0].getsockname()[1],
                "secret": shell.secret,
                "epoch": shell.epoch,
                "command_id": item.id,
                "cwd": shell.cwd,
                "fence": marker(shell.secret, item.id).hex(),
            }
            process.stdin.write((json.dumps(config) + "\n").encode())
            async with asyncio.timeout(1):
                await process.stdin.drain()
                assert await process.stdout.readexactly(1) == b"R"
            shell.readers = [
                asyncio.create_task(manager._pipe(shell, process.stdout, "output")),
                asyncio.create_task(manager._pipe(shell, process.stderr, "stderr")),
            ]
            item.deadline = asyncio.get_running_loop().time() + timeout
            item.monitor = asyncio.create_task(manager._monitor(item))
            yield manager, item, process
        finally:
            await manager.aclose()


async def signal(process: asyncio.subprocess.Process, actions: bytes) -> None:
    assert process.stdin is not None
    process.stdin.write(actions)
    await process.stdin.drain()


async def complete(item: _Command, process: asyncio.subprocess.Process) -> None:
    assert item.monitor is not None
    await signal(process, b"poe")
    async with asyncio.timeout(1):
        await asyncio.shield(item.monitor)
    assert item.done.is_set() and item.state == "completed" and item.exit_code == 7


def forbidden_work(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("订阅或已完成事实查询不能启动任务、读取或修剪输出")


@pytest.mark.parametrize("last", [b"p", b"o", b"e"], ids=["proof", "stdout", "stderr"])
async def test_running_watch_waits_for_identity_and_both_real_pipe_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, last: bytes
) -> None:
    observed: list[ShellFinished] = []
    async with controlled_command(tmp_path) as (manager, item, process):

        def on_finish(result: ShellFinished) -> None:
            assert item.finish_observer is None
            assert item.proof.is_set() and item.stdout_fence.is_set()
            assert item.stderr_fence.is_set() and item.done.is_set()
            assert item.finished is result
            observed.append(result)

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", forbidden_work)
            patch.setattr(_Output, "read_stream", forbidden_work)
            assert manager.watch_finish(item.shell.session_id, item.id, on_finish) is None
            assert manager.watch_finish(item.shell.session_id, item.id, on_finish) is None
        events = {b"p": item.proof, b"o": item.stdout_fence, b"e": item.stderr_fence}
        async with asyncio.timeout(1):
            for action, event in events.items():
                if action != last:
                    await signal(process, action)
                    await event.wait()
        assert item.monitor is not None
        ready, _ = await asyncio.wait({item.monitor}, timeout=0.01)
        assert not ready and not item.done.is_set() and observed == []
        await signal(process, last)
        async with asyncio.timeout(1):
            await asyncio.shield(item.monitor)
        assert observed == [
            ShellFinished(item.shell.session_id, item.id, item.shell.epoch, "completed", 7)
        ]
        assert not item.finish_attempted and process.returncode is None
        await manager.stop(item.shell.session_id)
        assert len(observed) == 1


async def test_completed_watch_returns_the_same_fact_without_callback_or_output_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[ShellFinished] = []
    async with controlled_command(tmp_path) as (manager, item, process):
        await complete(item, process)
        before = (tuple(item.output.chunks), item.output.observed, item.output.discarded)
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", forbidden_work)
            patch.setattr(_Output, "read_stream", forbidden_work)
            patch.setattr(_Output, "trim", forbidden_work)
            first = manager.watch_finish(item.shell.session_id, item.id, observed.append)
            second = manager.watch_finish(item.shell.session_id, item.id, forbidden_work)
        assert first is not None and first is second and first is item.finished
        assert first == ShellFinished(
            item.shell.session_id, item.id, item.shell.epoch, "completed", 7
        )
        assert item.finish_observer is None and observed == [] and not item.finish_attempted
        assert (tuple(item.output.chunks), item.output.observed, item.output.discarded) == before


async def test_watch_rejects_foreign_unknown_and_undispatched_commands(tmp_path: Path) -> None:
    observed: list[ShellFinished] = []
    async with controlled_command(tmp_path) as (manager, item, process):
        for session_id, command_id in (
            ("other-session", item.id),
            (item.shell.session_id, "missing-command"),
        ):
            with pytest.raises(ShellSessionError) as rejected:
                manager.watch_finish(session_id, command_id, observed.append)
            assert rejected.value.code == "not_found"
        item.dispatched = False
        try:
            with pytest.raises(ShellSessionError) as rejected:
                manager.watch_finish(item.shell.session_id, item.id, observed.append)
            assert rejected.value.code == "not_found"
        finally:
            item.dispatched = True
        with pytest.raises(ShellSessionError) as rejected:
            manager.watch_finish(item.shell.session_id, item.id, cast(ShellFinishObserver, None))
        assert rejected.value.code == "invalid_argument"
        assert item.finish_observer is None and observed == []
        assert process.returncode is None and not item.done.is_set()


async def test_conflicts_and_unwatch_use_the_exact_callback_identity(tmp_path: Path) -> None:
    first: list[ShellFinished] = []
    second: list[ShellFinished] = []
    callback = first.append
    different_identity = first.append
    replacement = second.append
    assert callback is not different_identity
    async with controlled_command(tmp_path) as (manager, item, process):
        session_id = item.shell.session_id
        assert manager.watch_finish(session_id, item.id, callback) is None
        assert manager.watch_finish(session_id, item.id, callback) is None
        with pytest.raises(ShellSessionError) as rejected:
            manager.watch_finish(session_id, item.id, different_identity)
        assert rejected.value.code == "conflict"
        manager.unwatch_finish("other-session", item.id, callback)
        manager.unwatch_finish(session_id, "missing-command", callback)
        manager.unwatch_finish(session_id, item.id, different_identity)
        assert item.finish_observer is callback
        manager.unwatch_finish(session_id, item.id, callback)
        manager.unwatch_finish(session_id, item.id, callback)
        assert item.finish_observer is None
        assert process.returncode is None and not item.done.is_set()
        assert manager.watch_finish(session_id, item.id, replacement) is None
        await complete(item, process)
        assert first == [] and len(second) == 1


@pytest.mark.parametrize("ending", ["stop", "timeout", "close", "lost"])
async def test_cleanup_publishes_the_actual_terminal_state_once(
    tmp_path: Path, ending: str
) -> None:
    observed: list[ShellFinished] = []
    async with controlled_command(tmp_path, timeout=0.1 if ending == "timeout" else 2) as (
        manager,
        item,
        process,
    ):
        callback = observed.append
        assert manager.watch_finish(item.shell.session_id, item.id, callback) is None
        if ending == "stop":
            await manager.stop(item.shell.session_id)
        elif ending == "close":
            await manager.aclose()
        elif ending == "lost":
            process.kill()
        async with asyncio.timeout(1):
            await item.done.wait()
            assert item.shell.cleanup is not None
            await asyncio.shield(item.shell.cleanup)
        expected = {
            "stop": "stopped",
            "timeout": "timed_out",
            "close": "stopped",
            "lost": "unknown",
        }
        assert observed == [
            ShellFinished(item.shell.session_id, item.id, item.shell.epoch, expected[ending], None)
        ]
        assert item.finish_observer is None and not item.finish_attempted
        assert item.monitor is not None and item.monitor.done()
        assert process.returncode is not None and manager.live_sessions() == ()
        assert item.shell.state == "closed" and not item.shell.cleanup_failed
        await manager.stop(item.shell.session_id)
        assert len(observed) == 1
        if ending == "close":
            with pytest.raises(ShellSessionError) as rejected:
                manager.watch_finish(item.shell.session_id, item.id, callback)
            assert rejected.value.code == "closed"
            manager.unwatch_finish(item.shell.session_id, item.id, callback)


@pytest.mark.parametrize("cancelled", [False, True], ids=["exception", "cancelled-error"])
async def test_observer_failure_does_not_change_the_main_notice_or_command_result(
    tmp_path: Path, cancelled: bool
) -> None:
    observed: list[ShellFinished] = []
    notices: list[ShellFinished] = []

    def observer(result: ShellFinished) -> None:
        observed.append(result)
        if cancelled:
            raise asyncio.CancelledError
        raise OSError("local observer fault")

    async def on_finish(result: ShellFinished) -> None:
        assert observed == [result]
        notices.append(result)

    async with controlled_command(tmp_path, on_finish=on_finish, notify=True) as (
        manager,
        item,
        process,
    ):
        assert manager.watch_finish(item.shell.session_id, item.id, observer) is None
        await complete(item, process)
        assert len(observed) == len(notices) == 1 and observed[0] is notices[0]
        assert item.finish_attempted and item.finish_observer is None
        await manager.stop(item.shell.session_id)
        assert len(observed) == len(notices) == 1
        assert item.shell.state == "closed" and not item.shell.cleanup_failed


async def test_main_notice_failure_keeps_the_already_delivered_observer_fact(
    tmp_path: Path,
) -> None:
    observed: list[ShellFinished] = []
    notices: list[ShellFinished] = []

    async def on_finish(result: ShellFinished) -> None:
        assert observed == [result]
        notices.append(result)
        raise OSError("local notice fault")

    async with controlled_command(tmp_path, on_finish=on_finish, notify=True) as (
        manager,
        item,
        process,
    ):
        assert manager.watch_finish(item.shell.session_id, item.id, observed.append) is None
        await complete(item, process)
        assert len(observed) == len(notices) == 1 and observed[0] is notices[0]
        assert item.finish_attempted and item.finish_observer is None
        await manager.stop(item.shell.session_id)
        assert len(observed) == len(notices) == 1


async def test_retire_collects_a_cancelled_main_notice_after_observer_delivery(
    tmp_path: Path,
) -> None:
    observed: list[ShellFinished] = []
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def on_finish(result: ShellFinished) -> None:
        assert observed == [result]
        entered.set()
        try:
            await release.wait()
        finally:
            cancelled.set()

    async with controlled_command(tmp_path, on_finish=on_finish, notify=True) as (
        manager,
        item,
        process,
    ):
        try:
            assert manager.watch_finish(item.shell.session_id, item.id, observed.append) is None
            await signal(process, b"poe")
            async with asyncio.timeout(1):
                await entered.wait()
            assert len(observed) == 1 and item.state == "completed" and item.done.is_set()
            assert item.monitor is not None and not item.monitor.done()
            await manager.stop(item.shell.session_id)
            assert cancelled.is_set() and item.monitor.done()
            assert item.finish_attempted and item.finish_observer is None
            assert len(observed) == 1 and observed[0].state == "completed"
            assert item.shell.state == "closed" and not item.shell.cleanup_failed
        finally:
            release.set()


async def test_retiring_observer_keeps_its_record_until_it_can_be_withdrawn(tmp_path: Path) -> None:
    observed: list[ShellFinished] = []
    async with controlled_command(tmp_path) as (manager, item, _process):
        callback = observed.append
        assert manager.watch_finish(item.shell.session_id, item.id, callback) is None
        manager._max_sessions = lambda: 1
        cleanup = manager._retire(item.shell, "stopped")
        later = _Command("later-command", item.shell, item.deadline)
        with pytest.raises(ShellSessionError) as rejected:
            manager._retain_command(later)
        assert rejected.value.code == "capacity"
        assert manager._commands.get(item.id) is item
        manager.unwatch_finish(item.shell.session_id, item.id, callback)
        async with asyncio.timeout(1):
            await asyncio.shield(cleanup)
        assert observed == [] and item.done.is_set() and item.state == "stopped"
