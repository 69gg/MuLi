"""Host lifecycle gates use only Python JSONL children, never native AX."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_ax_computer import action
from test_ax_integration import install_child

import muli.tools.ax_computer as host_module
import muli.tools.ax_lease as transport_module
from muli.config import Settings
from muli.process import _settle_process
from muli.tools.ax_lease import AXHelperLease
from muli.tools.computer import Computer, ComputerError


def host(monkeypatch: pytest.MonkeyPatch, owner: list[tuple[str, str]]) -> Computer:
    monkeypatch.setattr(host_module.sys, "platform", "darwin")
    return Computer(
        Settings.model_validate({"computer": {"backend": "ax", "enabled": True}}),
        owner=lambda: owner[0],
    )


async def cleanup(
    computer: Computer,
    *,
    tasks: Sequence[asyncio.Task[Any]] = (),
    leases: Sequence[AXHelperLease] = (),
    processes: Sequence[asyncio.subprocess.Process] = (),
) -> None:
    """Call after releasing gates, including when the tested assertion fails."""
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await computer.aclose()
    finally:
        await asyncio.gather(*(item.aclose() for item in leases), return_exceptions=True)
        # A gated launch may publish its handle while observers are settling.
        # Fall back to the shared reaper if the product cleanup itself failed.
        handles = {process.pid: process for process in processes}
        for item in leases:
            if item._process is not None:
                handles[item._process.pid] = item._process
        await asyncio.gather(
            *(
                _settle_process(process, ())
                for process in handles.values()
                if process.returncode is None
            )
        )


async def test_probe_launch_close_reaps_and_repeated_observer_cancel_stays_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer = host(monkeypatch, [("main", "one")])
    lease = AXHelperLease(
        [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
        timeout=2,
        ttl=5,
        max_nodes=10,
        once=True,
    )
    monkeypatch.setattr(computer._ax, "_new_lease", lambda **kwargs: lease)
    launched, publish = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec
    created: list[asyncio.subprocess.Process] = []
    tasks: list[asyncio.Task[Any]] = []

    async def start(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        created.append(process)
        launched.set()
        await publish.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    try:
        observer = asyncio.create_task(computer.status())
        tasks.append(observer)
        await asyncio.wait_for(launched.wait(), 2)
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert computer._ax._probe is not None and not computer._ax._probe.done()
        closer = asyncio.create_task(computer.aclose())
        tasks.append(closer)
        await asyncio.sleep(0)
        closer.cancel()
        await asyncio.sleep(0)
        closer.cancel()
        assert not lease.closed and not closer.done()
        publish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, 2)
        assert lease.closed and computer._ax._closed
        assert all(process.returncode is not None for process in created)
        assert computer._ax._probe_lease is None
        assert (await computer.status())["code"] == "backend_closed"
    finally:
        publish.set()
        await cleanup(computer, tasks=tasks, leases=[lease], processes=created)


async def test_expired_closing_lease_blocks_new_owner_until_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = [("main", "one")]
    computer = host(monkeypatch, owner)
    leases, _ = install_child(tmp_path, monkeypatch, computer)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = transport_module._settle_process

    async def settle(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await finish.wait()
        await original(*args, **kwargs)

    try:
        snapshot = await computer.snapshot({})
        first = leases[0]
        monkeypatch.setattr(transport_module, "_settle_process", settle)
        first.expires_at = asyncio.get_running_loop().time()
        first._expire()
        await asyncio.wait_for(entered.wait(), 2)
        assert first.state == "closing" and not first.closed
        owner[0] = ("worker", "two")
        with pytest.raises(ComputerError) as caught:
            await computer.snapshot({})
        assert caught.value.code == "desktop_busy" and len(leases) == 1
        finish.set()
        await first.aclose()
        second = await computer.snapshot({})
        assert second["snapshot_id"] != snapshot["snapshot_id"] and len(leases) == 2
        await computer.release_execution("main", "one")
        assert not leases[1].closed and computer._ax._snapshot_id == second["snapshot_id"]
        await computer.aclose()
        assert all(item.closed for item in leases)
    finally:
        finish.set()
        await cleanup(computer, leases=leases)


async def test_global_close_during_native_response_never_publishes_late_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer = host(monkeypatch, [("main", "one")])
    leases, _ = install_child(tmp_path, monkeypatch, computer)
    made, ready = asyncio.Event(), asyncio.Event()
    original_factory = computer._ax._new_lease
    tasks: list[asyncio.Task[Any]] = []

    def factory(**kwargs: Any) -> AXHelperLease:
        item = original_factory(**kwargs)
        original = item._start

        async def start() -> None:
            await original()
            made.set()
            await ready.wait()

        monkeypatch.setattr(item, "_start", start)
        return item

    monkeypatch.setattr(computer._ax, "_new_lease", factory)
    try:
        observing = asyncio.create_task(computer.snapshot({}))
        tasks.append(observing)
        await asyncio.wait_for(made.wait(), 2)
        closing = asyncio.create_task(computer.aclose())
        tasks.append(closing)
        await asyncio.wait_for(closing, 2)
        assert leases[0].closed
        ready.set()
        with pytest.raises(ComputerError):
            await asyncio.wait_for(observing, 2)
        assert not computer._ax._snapshot_id and not computer._ax._nodes
    finally:
        ready.set()
        await cleanup(computer, tasks=tasks, leases=leases)


async def test_busy_foreign_action_cannot_consume_current_owner_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = [("main", "one")]
    computer = host(monkeypatch, owner)
    leases, _ = install_child(tmp_path, monkeypatch, computer)
    try:
        snapshot = await computer.snapshot({})
        owner[0] = ("worker", "two")
        with pytest.raises(ComputerError) as caught:
            await computer.action(action(snapshot))
        assert caught.value.code == "desktop_busy"
        assert computer._ax._snapshot_id == snapshot["snapshot_id"] and not leases[0].closed
        owner[0] = ("main", "one")
        assert (await computer.action(action(snapshot)))["outcome"] == "accepted"
    finally:
        await cleanup(computer, leases=leases)
