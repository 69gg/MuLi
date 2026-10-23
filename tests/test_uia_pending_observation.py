"""Observe only fake owned helper handles; no PowerShell or desktop is started."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from helpers import uia_probe_diagnostics as diagnostic

from muli.tools import ax_lease
from muli.tools.ax_lease import MAX_STDERR_BYTES, AXLeaseError

PREFIX = b"muli-uia-pending=1:"
NOTE_PREFIX = "uia_pending_diagnostic="


def markers(*stages: str) -> bytes:
    return b"".join(PREFIX + stage.encode("ascii") + b"\r\n" for stage in stages)


def note(error: BaseException) -> dict[str, Any]:
    notes = [value for value in getattr(error, "__notes__", ()) if value.startswith(NOTE_PREFIX)]
    assert len(notes) == 1 and len(notes[0].encode("ascii")) <= 4096
    return cast(dict[str, Any], json.loads(notes[0][len(NOTE_PREFIX) :]))


@pytest.fixture(autouse=True)
def forbid_processes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Pending observation must not start a real process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    yield


class _Input:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.drains = self.closes = self.waits = 0
        self.drain_entered = asyncio.Event()
        self.drain_release = asyncio.Event()
        self.drain_release.set()

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        self.drains += 1
        self.drain_entered.set()
        await self.drain_release.wait()

    def close(self) -> None:
        self.closes += 1

    async def wait_closed(self) -> None:
        self.waits += 1


class _Process:
    def __init__(self, writer: _Input) -> None:
        self.stdin = cast(asyncio.StreamWriter, writer)
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode: int | None = None


class _Harness:
    def __init__(self) -> None:
        self.writer = _Input()
        self.process = _Process(self.writer)
        self.spawn_entered = asyncio.Event()
        self.spawn_release = asyncio.Event()
        self.spawn_release.set()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.spawns = self.cleanups = 0
        self.cleanup_failure: BaseException | None = None
        self.lease = diagnostic.PendingHelperLease(
            ["test-owned-host", "-NoProfile", "-NonInteractive", "-Mta", "-File", "test.ps1"],
            timeout=12,
            ttl=60,
            max_nodes=300,
        )

    async def spawn(self, *args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        self.spawns += 1
        self.spawn_entered.set()
        await self.spawn_release.wait()
        return cast(asyncio.subprocess.Process, self.process)

    async def settle(
        self,
        process: asyncio.subprocess.Process,
        readers: list[asyncio.Task[None]],
        **kwargs: Any,
    ) -> None:
        self.cleanups += 1
        self.cleanup_entered.set()
        await self.cleanup_release.wait()
        if self.cleanup_failure is not None:
            raise self.cleanup_failure
        assert process is self.process
        assert process.stdin is not None
        process.stdin.close()
        await process.stdin.wait_closed()
        self.process.returncode = 7
        self.process.stdout.feed_eof()
        self.process.stderr.feed_eof()
        await asyncio.gather(*readers)


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Harness]:
    case = _Harness()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", case.spawn)
    monkeypatch.setattr(ax_lease, "_settle_process", case.settle)
    try:
        yield case
    finally:
        case.spawn_release.set()
        case.cleanup_release.set()
        case.writer.drain_release.set()
        try:
            try:
                await case.lease.aclose()
            except AXLeaseError:
                if case.cleanup_failure is None:
                    raise
        finally:
            for reader in case.lease._readers:
                reader.cancel()
            await asyncio.gather(*case.lease._readers, return_exceptions=True)


@pytest.mark.parametrize("phase", ["spawn_pending", "drain_pending", "await_response"])
async def test_same_process_observation_separates_before_cleanup_from_forced_exit(
    harness: _Harness,
    phase: str,
) -> None:
    lease = harness.lease
    failure = TimeoutError("original private timeout")
    fail_now = asyncio.Event()
    harness.cleanup_release.clear()
    if phase == "spawn_pending":
        harness.spawn_release.clear()
    if phase == "drain_pending":
        harness.writer.drain_release.clear()

    async def operation() -> None:
        async with diagnostic.observe_pending(lease):
            await fail_now.wait()
            raise failure

    observing = asyncio.create_task(operation())
    try:
        async with asyncio.timeout(2):
            await harness.spawn_entered.wait()
            if phase != "spawn_pending":
                await harness.writer.drain_entered.wait()
            fail_now.set()
            while lease.state != "closing":
                await asyncio.sleep(0)
        before = lease.before_cleanup
        assert before is not None
        assert before["spawn"] == ("pending" if phase == "spawn_pending" else "returned")
        assert before["exit"] == ("unavailable" if phase == "spawn_pending" else "running")
        assert before["sent"] == (0 if phase == "spawn_pending" else 1)
        assert before["write_returned"] is (phase != "spawn_pending")
        assert before["drain_started"] is (phase != "spawn_pending")
        assert before["drain_returned"] is (phase == "await_response")
        assert not observing.done() and not getattr(failure, "__notes__", ())
        harness.spawn_release.set()
        harness.cleanup_release.set()
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(observing, 2)
        assert caught.value is failure
        facts = note(failure)
        assert facts["before_cleanup"] == before
        assert facts["after_cleanup"]["spawn"] == "returned"
        assert facts["after_cleanup"]["exit"] == "nonzero"
        assert facts["after_cleanup"]["readers_settled"] is True
        assert facts["cleanup"] == "confirmed"
        assert "private" not in json.dumps(facts)
        assert harness.spawns == harness.cleanups == 1
        expected_writes = 0 if phase == "spawn_pending" else 1
        assert len(harness.writer.frames) == harness.writer.drains == expected_writes
        assert harness.writer.closes == harness.writer.waits == 1
        if harness.writer.frames:
            assert json.loads(harness.writer.frames[0])["command"] == "probe"
    finally:
        harness.spawn_release.set()
        harness.cleanup_release.set()
        observing.cancel()
        await asyncio.gather(observing, return_exceptions=True)


@pytest.mark.parametrize(
    ("data", "stages", "sequence", "received"),
    [
        (b"private-host-error\n", [], "unobserved", False),
        (PREFIX + b"input_received", [], "unobserved", False),
        (markers("script_entered"), ["script_entered"], "valid", False),
        (markers("input_received"), ["input_received"], "valid", True),
        (markers(*diagnostic.PENDING_STAGES), list(diagnostic.PENDING_STAGES), "valid", True),
        (markers("script_entered", "script_entered"), ["script_entered"], "invalid", False),
        (
            markers("input_received", "script_entered"),
            ["input_received", "script_entered"],
            "invalid",
            True,
        ),
        (b"x" * MAX_STDERR_BYTES + b"\n" + markers("input_received"), [], "unobserved", False),
    ],
    ids=["unknown", "partial", "entered", "read-only", "both", "duplicate", "order", "cap"],
)
async def test_pending_projection_exports_only_complete_fixed_lines_and_bounded_counts(
    harness: _Harness,
    data: bytes,
    stages: list[str],
    sequence: str,
    received: bool,
) -> None:
    lease = harness.lease
    lease._stderr.extend(data)
    lease._count = 99
    facts = diagnostic.pending_snapshot(lease)
    assert facts["script_stages"] == stages and facts["stage_sequence"] == sequence
    assert facts["stderr_bytes"] == min(len(data), MAX_STDERR_BYTES)
    assert facts["stderr_prefix_capped"] is (len(data) >= MAX_STDERR_BYTES)
    assert facts["sent"] == "unavailable"
    assert diagnostic.pending_received(lease) is received
    assert "private" not in json.dumps(facts)


async def test_external_cancellation_is_same_exception_after_owned_cleanup(
    harness: _Harness,
) -> None:
    original: list[asyncio.CancelledError] = []
    harness.cleanup_release.clear()

    async def operation() -> None:
        async with diagnostic.observe_pending(harness.lease):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                original.append(error)
                raise

    observing = asyncio.create_task(operation())
    try:
        async with asyncio.timeout(2):
            await harness.writer.drain_entered.wait()
            observing.cancel("private cancellation")
            await harness.cleanup_entered.wait()
        assert not observing.done() and not getattr(original[0], "__notes__", ())
        harness.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(observing, 2)
        assert caught.value is original[0]
        assert note(caught.value)["cleanup"] == "confirmed"
        assert harness.lease.closed and harness.spawns == harness.cleanups == 1
    finally:
        harness.cleanup_release.set()
        observing.cancel()
        await asyncio.gather(observing, return_exceptions=True)


async def test_intended_cancel_after_read_marker_remains_the_original_success_path(
    harness: _Harness,
) -> None:
    harness.process.stderr.feed_data(markers("input_received"))
    async with diagnostic.observe_pending(harness.lease) as reading:
        async with asyncio.timeout(2):
            while not diagnostic.pending_received(harness.lease):
                await asyncio.sleep(0)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert harness.lease.closed
    assert harness.spawns == harness.cleanups == 1
    assert len(harness.writer.frames) == harness.writer.drains == 1


@pytest.mark.parametrize("failed_component", ["projection", "retire_projection", "cleanup"])
async def test_diagnostic_and_cleanup_faults_do_not_replace_original_error(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    failed_component: str,
) -> None:
    failure = AXLeaseError("read_timeout", "private original")
    if failed_component in {"projection", "retire_projection"}:

        def unavailable(*args: object, **kwargs: object) -> dict[str, Any]:
            raise RuntimeError("private projection")

        monkeypatch.setattr(
            diagnostic,
            "pending_facts" if failed_component == "projection" else "pending_snapshot",
            unavailable,
        )
    else:
        harness.cleanup_failure = RuntimeError("private cleanup")
    with pytest.raises(AXLeaseError) as caught:
        async with diagnostic.observe_pending(harness.lease):
            async with asyncio.timeout(2):
                await harness.writer.drain_entered.wait()
            raise failure
    assert caught.value is failure and caught.value.outcome == "not_started"
    assert harness.spawns == harness.cleanups == 1
    if failed_component != "cleanup":
        assert not getattr(failure, "__notes__", ()) and harness.lease.closed
    else:
        assert note(failure)["cleanup"] == "unconfirmed" and not harness.lease.closed


async def test_success_does_not_swallow_a_cleanup_error(harness: _Harness) -> None:
    harness.cleanup_failure = RuntimeError("private cleanup")
    with pytest.raises(AXLeaseError, match="清理未能完成"):
        async with diagnostic.observe_pending(harness.lease):
            async with asyncio.timeout(2):
                await harness.writer.drain_entered.wait()
    assert harness.spawns == harness.cleanups == 1 and not harness.lease.closed


def test_fixed_pending_script_marks_entry_before_read_without_changing_sleep() -> None:
    script = diagnostic.PENDING_SCRIPT
    entered = "'muli-uia-pending=1:script_entered'"
    received = "'muli-uia-pending=1:input_received'"
    assert script.count(entered) == script.count(received) == 1
    assert script.index(entered) < script.index("[Console]::ReadLine()") < script.index(received)
    assert script.count("[Console]::Error.Flush()") == 2
    assert script.count("[Threading.Thread]::Sleep(30000)") == 1
    assert len(markers(*diagnostic.PENDING_STAGES)) < 128
    assert "ExecutionPolicy" not in script and "Start-Process" not in script


async def test_native_fourth_case_uses_one_observed_lease_and_keeps_original_cancel_assertions(
    harness: _Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_uia_native as native

    monkeypatch.setattr(native.module, "_powershell", lambda: tmp_path / "owned-host.exe")
    harness.process.stderr.feed_data(markers("input_received"))
    async with asyncio.timeout(2):
        await native.test_real_windows_pending_powershell_cancellation_reaps_only_owned_helper(
            tmp_path
        )
    assert harness.spawns == harness.cleanups == 1
    assert len(harness.writer.frames) == harness.writer.drains == 1
    observed = cast(diagnostic._PendingInput, harness.process.stdin).lease
    assert observed.closed and observed.before_cleanup is not None
    assert observed.before_cleanup["drain_returned"] is True
    assert harness.process.returncode is not None
