"""Pure streams and owned tasks; this module must never start PowerShell."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast

import pytest
from helpers import uia_native_startup as module

from muli.config import Settings
from muli.process import _wait_cleanup


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 731
        self.stdout = asyncio.StreamReader(limit=module.OUTPUT_BYTES)
        self.stderr = asyncio.StreamReader(limit=module.OUTPUT_BYTES)
        self.returncode: int | None = None

    def ready(self) -> None:
        self.stdout.feed_data(json.dumps({"pid": self.pid, "title": "owned"}).encode() + b"\n")


@pytest.fixture(autouse=True)
def no_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        raise AssertionError("This fixture test must not create a real subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


@pytest.fixture
def reaped(monkeypatch: pytest.MonkeyPatch) -> list[FakeProcess]:
    calls: list[FakeProcess] = []

    async def settle(
        process: asyncio.subprocess.Process, tasks: Sequence[asyncio.Task[None]]
    ) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        fake = cast(FakeProcess, process)
        calls.append(fake)
        fake.returncode = -1

    monkeypatch.setattr(module, "_settle_process", settle)
    return calls


def start(process: FakeProcess) -> asyncio.Task[asyncio.subprocess.Process]:
    async def create() -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    return asyncio.create_task(create())


def diagnostic(error: BaseException) -> dict[str, Any]:
    notes = [note for note in error.__notes__ if note.startswith("uia_native_startup_diagnostic=")]
    assert len(notes) == 1 and len(notes[0].encode("utf-8")) <= module.OUTPUT_BYTES
    assert "private-body" not in notes[0] and "private-cleanup" not in notes[0]
    return cast(dict[str, Any], json.loads(notes[0].split("=", 1)[1]))


async def test_ready_fixture_keeps_target_alive_until_owned_cleanup(
    reaped: list[FakeProcess],
) -> None:
    process = FakeProcess()
    process.ready()
    async with module.observe_native_form(start(process), "owned", 12) as observed:
        assert observed is cast(asyncio.subprocess.Process, process) and process.returncode is None
    assert reaped == [process] and process.returncode == -1


async def test_failure_note_follows_cleanup_and_only_contains_fixed_stage_names(
    reaped: list[FakeProcess],
) -> None:
    process = FakeProcess()
    process.ready()
    process.stderr.feed_data(
        b"muli-native-stage=script_entered\r\nprivate-body\n"
        b"muli-native-stage=../private-body\n"
        b"muli-native-stage=script_entered\n"
        b"muli-native-stage=shown\n"
    )
    original = RuntimeError("body failed")
    with pytest.raises(RuntimeError) as caught:
        async with module.observe_native_form(start(process), "owned", 12):
            raise original
    assert caught.value is original and reaped == [process]
    details = diagnostic(original)
    stages = [item["stage"] for item in details["stages"]]
    assert stages.count("script_entered") == 1 and "shown" in stages
    assert all(
        stage in (*module.SCRIPT_STAGES, "spawn_returned", "readiness_received") for stage in stages
    )
    assert details["exit_before_cleanup"] is None and details["exit_after_cleanup"] == -1
    assert details["cleanup"] == "reaped" and details["readers_settled"] is True


async def test_both_pipes_share_one_total_byte_budget(reaped: list[FakeProcess]) -> None:
    process = FakeProcess()
    process.stderr.feed_data(b"x" * (module.OUTPUT_BYTES - 100))
    process.stdout.feed_data(
        json.dumps({"pid": process.pid, "title": "owned"}).encode() + b" " * 200 + b"\n"
    )
    with pytest.raises(AssertionError, match="output budget") as caught:
        async with module.observe_native_form(start(process), "owned", 12):
            pytest.fail("An over-budget fixture must not become ready")
    details = diagnostic(caught.value)
    assert details["stdout_bytes"] + details["stderr_bytes"] > module.OUTPUT_BYTES
    assert details["output_budget"] is True and details["cleanup"] == "reaped"
    assert reaped == [process]


async def test_stderr_failure_after_readiness_still_fails_and_reaps(
    monkeypatch: pytest.MonkeyPatch, reaped: list[FakeProcess]
) -> None:
    process = FakeProcess()
    process.ready()
    failed = asyncio.Event()
    original_read = module._read_stderr

    async def read_stderr(stream: asyncio.StreamReader, trace: module._Trace) -> None:
        try:
            await original_read(stream, trace)
        except AssertionError:
            failed.set()
            raise

    monkeypatch.setattr(module, "_read_stderr", read_stderr)
    with pytest.raises(AssertionError, match="did not settle successfully") as caught:
        async with module.observe_native_form(start(process), "owned", 12):
            process.stderr.feed_data(b"x" * module.OUTPUT_BYTES)
            await failed.wait()
    details = diagnostic(caught.value)
    assert details["reader_failed"] is True and details["output_budget"] is True
    assert details["cleanup"] == "reaped" and reaped == [process]


async def test_stages_do_not_renew_the_single_original_deadline(
    monkeypatch: pytest.MonkeyPatch, reaped: list[FakeProcess]
) -> None:
    process = FakeProcess()
    clock = 100.0
    timers: list[asyncio.Timeout] = []
    observed_stage = asyncio.Event()
    original_timeout = asyncio.timeout_at
    original_mark = module._Trace.mark

    def timeout_at(when: float) -> asyncio.Timeout:
        timer = original_timeout(when)
        timers.append(timer)
        return timer

    def mark(trace: module._Trace, stage: str) -> None:
        original_mark(trace, stage)
        if stage == "loaded":
            observed_stage.set()

    monkeypatch.setattr(module, "timeout_at", timeout_at)
    monkeypatch.setattr(module._Trace, "mark", mark)

    async def observe() -> None:
        async with module.observe_native_form(
            start(process),
            "owned",
            Settings().computer.operation_timeout_seconds,
            clock=lambda: clock,
        ):
            pytest.fail("No readiness was supplied")

    watching = asyncio.create_task(observe())
    while not timers:
        await asyncio.sleep(0)
    deadline = timers[0].when()
    for stage in ("script_entered", "system_module_loaded", "assemblies_loaded", "loaded"):
        clock += 2
        process.stderr.feed_data(module.STAGE_PREFIX + stage.encode() + b"\n")
        await asyncio.sleep(0)
    await observed_stage.wait()
    assert len(timers) == 1 and timers[0].when() == deadline
    # Expire the real timeout callback without sleeping or changing global time.
    timers[0].reschedule(asyncio.get_running_loop().time() - 1)
    with pytest.raises(TimeoutError) as caught:
        await watching
    details = diagnostic(caught.value)
    assert [stage["stage"] for stage in details["stages"]][-1] == "loaded"
    assert details["cleanup"] == "reaped" and reaped == [process]


async def test_eof_and_private_stderr_do_not_leak_body(reaped: list[FakeProcess]) -> None:
    process = FakeProcess()
    process.returncode = 1
    process.stdout.feed_eof()
    process.stderr.feed_data(b"private-body\n")
    process.stderr.feed_eof()
    with pytest.raises(AssertionError, match="did not publish readiness") as caught:
        async with module.observe_native_form(start(process), "owned", 12):
            pytest.fail("An exited fixture cannot become ready")
    details = diagnostic(caught.value)
    assert details["stdout_eof"] is True and details["stderr_eof"] is True
    assert details["exit_before_cleanup"] == 1 and details["cleanup"] == "reaped"
    assert reaped == [process]


async def test_repeated_cancel_adopts_and_reaps_a_late_spawn(
    monkeypatch: pytest.MonkeyPatch, reaped: list[FakeProcess]
) -> None:
    process = FakeProcess()
    spawn_entered = asyncio.Event()
    spawn_release = asyncio.Event()
    cleanup_entered = asyncio.Event()
    original_cleanup = _wait_cleanup

    async def create() -> asyncio.subprocess.Process:
        spawn_entered.set()
        await spawn_release.wait()
        return cast(asyncio.subprocess.Process, process)

    async def cleanup(task: asyncio.Task[None]) -> bool:
        cleanup_entered.set()
        return await original_cleanup(task)

    monkeypatch.setattr(module, "_wait_cleanup", cleanup)
    starting = asyncio.create_task(create())

    async def observe() -> None:
        async with module.observe_native_form(starting, "owned", 12):
            pytest.fail("Cancelled creation must not yield")

    watching = asyncio.create_task(observe())
    await spawn_entered.wait()
    watching.cancel()
    await cleanup_entered.wait()
    watching.cancel()
    assert not watching.done() and not starting.cancelled()
    spawn_release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await watching
    details = diagnostic(caught.value)
    assert details["spawn"] == "returned" and details["cleanup"] == "reaped"
    assert reaped == [process] and starting.done()


async def test_repeated_cancel_during_cleanup_still_waits_for_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    process.ready()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()

    async def settle(
        adopted: asyncio.subprocess.Process, readers: Sequence[asyncio.Task[None]]
    ) -> None:
        cleanup_entered.set()
        await cleanup_release.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        assert adopted is cast(asyncio.subprocess.Process, process)
        process.returncode = -1

    monkeypatch.setattr(module, "_settle_process", settle)

    async def observe() -> None:
        async with module.observe_native_form(start(process), "owned", 12):
            pass

    watching = asyncio.create_task(observe())
    await cleanup_entered.wait()
    watching.cancel()
    await asyncio.sleep(0)
    watching.cancel()
    assert not watching.done()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await watching
    assert diagnostic(caught.value)["cleanup"] == "reaped" and process.returncode == -1


async def test_cleanup_failure_does_not_replace_the_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    process.ready()
    original = RuntimeError("body failed")

    async def settle(
        adopted: asyncio.subprocess.Process, readers: Sequence[asyncio.Task[None]]
    ) -> None:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        raise OSError("private-cleanup")

    monkeypatch.setattr(module, "_settle_process", settle)
    with pytest.raises(RuntimeError) as caught:
        async with module.observe_native_form(start(process), "owned", 12):
            raise original
    assert caught.value is original
    assert diagnostic(caught.value)["cleanup"] == "failed"
