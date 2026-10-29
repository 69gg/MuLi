"""Cancellation crosses a same-round join only after both branches settle."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from test_managed_probe_interface_join import Interface, Kind, case, entered, settle
from test_managed_probe_interface_join import no_subprocess as no_subprocess

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed probe boundaries")


@pytest.mark.parametrize("kind", ["service", "helper"])
async def test_outer_timeout_collects_both_entered_interfaces_before_escaping(
    tmp_path: Path, kind: Kind, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reschedule the probe's actual timeout after both handshakes. Elapsed time
    # is not an acceptance assertion, and no replacement runner timeout is used.
    original_timeout = asyncio.timeout
    contexts: list[asyncio.Timeout] = []

    def capture_timeout(delay: float | None) -> asyncio.Timeout:
        context = original_timeout(delay)
        contexts.append(context)
        return context

    monkeypatch.setattr(asyncio, "timeout", capture_timeout)
    selected = case(tmp_path, kind)
    runner = selected.runner
    runner.cleanup_block.add((1, "Service"))
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await entered(runner, 1, "Service")
        assert len(contexts) == 1
        contexts[0].reschedule(asyncio.get_running_loop().time())
        await asyncio.wait_for(runner.cleanup_entered[(1, "Service")].wait(), 1)
        assert not task.done(), "The timeout must wait for its sibling's cleanup"
        runner.cleanup_release[(1, "Service")].set()
        with pytest.raises(TimeoutError):
            await task
        assert contexts[0].expired()
        assert runner.cancelled == runner.settled == {(1, "Unit"), (1, "Service")}
        assert runner.active == 0
        assert not any(runner.entered[(2, interface)].is_set() for interface in ("Unit", "Service"))
    finally:
        await settle(task, runner)


@pytest.mark.parametrize("kind", ["service", "helper"])
@pytest.mark.parametrize("unit_pending", [True, False])
async def test_repeated_cancel_collects_both_interfaces_before_return(
    tmp_path: Path, kind: Kind, unit_pending: bool
) -> None:
    selected = case(tmp_path, kind)
    runner = selected.runner
    runner.cleanup_block.add((1, "Service"))
    unit_returned = asyncio.Event()
    original = runner.original

    async def read(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        result = await original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)
        if "org.freedesktop.systemd1.Unit" in argv:
            unit_returned.set()
        return result

    runner.original = read
    if unit_pending:
        runner.cleanup_block.add((1, "Unit"))
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await entered(runner, 1, "Service")
        if not unit_pending:
            runner.release[(1, "Unit")].set()
            await asyncio.wait_for(unit_returned.wait(), 1)
            assert (1, "Unit") in runner.settled
        task.cancel()
        if unit_pending:
            await asyncio.wait_for(runner.cleanup_entered[(1, "Unit")].wait(), 1)
            task.cancel()
        await asyncio.wait_for(runner.cleanup_entered[(1, "Service")].wait(), 1)
        if not unit_pending:
            task.cancel()
        # This callback runs after the repeated cancel was queued. It gives
        # the parent a scheduling turn while the sibling cleanup stays gated.
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 1)
        assert not task.done(), "Repeated cancellation must not abandon the sibling"
        assert (1, "Service") not in runner.settled
        runner.cleanup_release[(1, "Service")].set()
        with pytest.raises(asyncio.CancelledError):
            await task
        expected = {(1, "Unit"), (1, "Service")} if unit_pending else {(1, "Service")}
        assert runner.cancelled == expected
        assert runner.settled == {(1, "Unit"), (1, "Service")}
        assert runner.active == 0
        assert not any(runner.entered[(2, interface)].is_set() for interface in ("Unit", "Service"))
    finally:
        await settle(task, runner)


@pytest.mark.parametrize("kind", ["service", "helper"])
@pytest.mark.parametrize("interface", ["Unit", "Service"])
async def test_child_cancelled_error_propagates_after_its_sibling_settles(
    tmp_path: Path, kind: Kind, interface: Interface
) -> None:
    selected = case(tmp_path, kind)
    runner = selected.runner
    original = runner.original
    child_cancelled = asyncio.Event()

    async def cancel_child(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        if "org.freedesktop.systemd1." + interface in argv:
            child_cancelled.set()
            raise asyncio.CancelledError("independent child cancellation")
        return await original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    runner.original = cancel_child
    if interface == "Unit":
        runner.cleanup_block.add((1, "Service"))
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await entered(runner, 1, "Service")
        runner.release[(1, interface)].set()
        await asyncio.wait_for(child_cancelled.wait(), 1)
        if interface == "Unit":
            await asyncio.wait_for(runner.cleanup_entered[(1, "Service")].wait(), 1)
            assert not task.done(), "A cancelled Unit must still collect its sibling"
            runner.cleanup_release[(1, "Service")].set()
        else:
            assert not task.done() and (1, "Unit") not in runner.cancelled
            runner.release[(1, "Unit")].set()
        with pytest.raises(asyncio.CancelledError):
            await task
        expected = {(1, "Unit"), (1, "Service")} if interface == "Unit" else {(1, "Service")}
        assert runner.cancelled == expected
        assert runner.settled == {(1, "Unit"), (1, "Service")}
        assert runner.active == 0
        assert not any(runner.entered[(2, other)].is_set() for other in ("Unit", "Service"))
    finally:
        await settle(task, runner)
