from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_voice import FakeDetector, FakeMedia, FakePlayer, FakeRecorder

import muli.voice as voice_module
from muli.config import VoiceSettings
from muli.voice import VoiceWorker
from muli.voice_health import VOICE_LISTENER_FAILED, VOICE_UNAVAILABLE


class ControlledLoop:
    """Replace only voice.py's module reference, never global asyncio.sleep."""

    def __init__(self) -> None:
        self.sleeps: asyncio.Queue[tuple[float, asyncio.Event]] = asyncio.Queue()

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, seconds: float) -> None:
        release = asyncio.Event()
        self.sleeps.put_nowait((seconds, release))
        await release.wait()

    async def checkpoint(self, seconds: float) -> asyncio.Event:
        delay, release = await asyncio.wait_for(self.sleeps.get(), 2)
        assert delay == seconds
        return release


class SwitchingDetector(FakeDetector):
    def __init__(self, *, failing: bool = False) -> None:
        super().__init__(False)
        self.failing = failing

    async def detect(self, path: Path, *, reference: Path | None = None) -> bool:
        if self.failing:
            raise ValueError("PRIVATE listener failure at " + str(path))
        return await super().detect(path, reference=reference)


async def no_text(event: dict[str, Any]) -> None:
    raise AssertionError("未命中唤醒不能提交消息")


def make_worker(
    path: Path,
    *,
    wake: FakeDetector | None = None,
    recorder: FakeRecorder | None = None,
    enabled: bool = True,
    on_fault: Callable[[str, str], None] | None = None,
) -> VoiceWorker:
    return VoiceWorker(
        VoiceSettings(enabled=enabled, chunk_seconds=1, cooldown_seconds=0),
        path,
        FakeMedia(),
        no_text,
        wake=wake or SwitchingDetector(),
        speaker=FakeDetector(configured=False),
        recorder=recorder or FakeRecorder(),
        player=FakePlayer(),
        on_fault=on_fault,
    )


def fault_sink(events: list[tuple[str, str]]) -> Callable[[str, str], None]:
    def receive(code: str, occurrence: str) -> None:
        events.append((code, occurrence))

    return receive


async def test_listener_failure_is_one_period_until_a_real_no_wake_cycle_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ControlledLoop()
    monkeypatch.setattr(voice_module, "asyncio", clock)
    events: list[tuple[str, str]] = []
    wake = SwitchingDetector(failing=True)
    worker = make_worker(tmp_path, wake=wake, on_fault=fault_sink(events))
    try:
        await worker.start()
        assert worker.status()["health"] == {
            "state": "idle",
            "code": None,
            "occurrence_id": None,
        }
        first = await clock.checkpoint(2)
        failed = worker.status()["health"]
        assert failed["state"] == "failed" and failed["code"] == VOICE_LISTENER_FAILED
        assert events == [(VOICE_LISTENER_FAILED, failed["occurrence_id"])]
        assert len(failed["occurrence_id"]) == 32
        assert set(failed["occurrence_id"]) <= set("0123456789abcdef")
        assert "PRIVATE" not in json.dumps(failed) and str(tmp_path) not in json.dumps(failed)
        failed["code"] = "changed-by-caller"

        first.set()
        repeated = await clock.checkpoint(2)
        assert worker.status()["health"]["code"] == VOICE_LISTENER_FAILED
        assert len(events) == 1
        wake.failing = False
        repeated.set()
        recovered = await clock.checkpoint(0)
        assert worker.status()["health"] == {
            "state": "ready",
            "code": None,
            "occurrence_id": None,
        }
        assert worker.status()["last_error"] == ""
        assert worker.media.audio == []
        wake.failing = True
        recovered.set()
        await clock.checkpoint(2)
        assert [code for code, _ in events] == [VOICE_LISTENER_FAILED, VOICE_LISTENER_FAILED]
        assert events[0][1] != events[1][1]
        assert worker.status()["health"]["occurrence_id"] == events[1][1]
    finally:
        await worker.aclose()
    assert list(worker.files.root.glob("*.wav")) == []


@pytest.mark.parametrize("missing", ["recorder", "wake", "asr", "reference"])
async def test_unavailable_start_reports_once_and_never_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    events: list[tuple[str, str]] = []
    recorder = FakeRecorder()
    worker = make_worker(tmp_path, recorder=recorder, on_fault=fault_sink(events))
    if missing == "recorder":
        monkeypatch.setattr(recorder, "status", lambda: {"available": False})
    elif missing == "wake":
        worker.wake = FakeDetector(configured=False)
    elif missing == "asr":
        monkeypatch.setattr(worker.media, "status", lambda: {"asr": False, "tts": False})
    else:
        worker.speaker = FakeDetector()
    try:
        for _ in range(3):
            await worker.start()
        assert recorder.calls == [] and worker.status()["running"] is False
        assert len(events) == 1 and events[0][0] == VOICE_UNAVAILABLE
        assert worker.status()["health"] == {
            "state": "failed",
            "code": VOICE_UNAVAILABLE,
            "occurrence_id": events[0][1],
        }
    finally:
        await worker.aclose()
    assert len(events) == 1


async def test_available_preflight_does_not_clear_failure_before_recording_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Recorder(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            entered.set()
            await release.wait()
            await super().record(path, seconds)

    clock = ControlledLoop()
    monkeypatch.setattr(voice_module, "asyncio", clock)
    events: list[tuple[str, str]] = []
    wake = SwitchingDetector()
    wake.configured = False
    worker = make_worker(tmp_path, recorder=Recorder(), wake=wake, on_fault=fault_sink(events))
    try:
        await worker.start()
        failed = worker.status()["health"]
        wake.configured = True
        await worker.start()
        await asyncio.wait_for(entered.wait(), 2)
        assert worker.status()["health"] == failed
        release.set()
        await clock.checkpoint(0)
        assert worker.status()["health"]["state"] == "ready"
        assert len(events) == 1
    finally:
        await worker.aclose()


@pytest.mark.parametrize("ending", ["stop", "close"])
async def test_normal_background_cancellation_has_no_fault(tmp_path: Path, ending: str) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Recorder(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            await super().record(path, seconds)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    events: list[tuple[str, str]] = []
    worker = make_worker(tmp_path, recorder=Recorder(), on_fault=fault_sink(events))
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 2)
        await (worker.stop() if ending == "stop" else worker.aclose())
        assert cancelled.is_set() and events == []
        assert worker.status()["health"]["state"] == "idle"
        assert worker._operations == set() and not worker.status()["running"]
        assert list(worker.files.root.glob("*.wav")) == []
    finally:
        await worker.aclose()


async def test_fault_callback_exception_preserves_retry_delay_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ControlledLoop()
    monkeypatch.setattr(voice_module, "asyncio", clock)
    events: list[tuple[str, str]] = []

    def broken_callback(code: str, occurrence: str) -> None:
        events.append((code, occurrence))
        raise RuntimeError("PRIVATE notification failure")

    wake = SwitchingDetector(failing=True)
    worker = make_worker(tmp_path, wake=wake, on_fault=broken_callback)
    try:
        wake.configured = False
        await worker.start()
        assert len(events) == 1 and events[0][0] == VOICE_UNAVAILABLE
        wake.configured = True
        await worker.start()
        first = await clock.checkpoint(2)
        assert len(events) == 1 and worker._task is not None and not worker._task.done()
        first.set()
        repeated = await clock.checkpoint(2)
        assert len(events) == 1
        assert "listener failure" in worker.last_error
        assert "notification failure" not in worker.last_error
        wake.failing = False
        repeated.set()
        recovered = await clock.checkpoint(0)
        assert worker.status()["health"]["state"] == "ready"
        wake.failing = True
        recovered.set()
        await clock.checkpoint(2)
        assert len(events) == 2 and worker._task is not None and not worker._task.done()
        assert events[1][0] == VOICE_LISTENER_FAILED
    finally:
        await worker.aclose()


async def test_disabling_background_before_a_late_failure_suppresses_the_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class LateFailure(FakeDetector):
        async def detect(self, path: Path, *, reference: Path | None = None) -> bool:
            entered.set()
            await release.wait()
            raise ValueError("late device failure")

    clock = ControlledLoop()
    monkeypatch.setattr(voice_module, "asyncio", clock)
    events: list[tuple[str, str]] = []
    worker = make_worker(tmp_path, wake=LateFailure(), on_fault=fault_sink(events))
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 2)
        worker.config.enabled = False
        release.set()
        await clock.checkpoint(2)
        assert events == [] and worker.status()["health"]["state"] == "idle"
    finally:
        await worker.aclose()


async def test_disabled_manual_and_setup_operations_do_not_produce_background_health(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, str]] = []
    wake = SwitchingDetector()
    worker = make_worker(tmp_path, enabled=False, wake=wake, on_fault=fault_sink(events))
    worker.speaker = FakeDetector()
    try:
        await worker.start()
        with pytest.raises(ValueError):
            await worker.listen_once()
        await worker.setup_enroll(1)
        result = await worker.setup_verify(1)
        assert result["state"] == "completed" and result["wake_detected"] is False
        wake.failing = True
        with pytest.raises(ValueError, match="PRIVATE"):
            await worker.setup_verify(1)
        worker.config.enabled = True
        with pytest.raises(ValueError, match="PRIVATE"):
            await worker.listen_once()
        assert events == [] and worker.status()["health"] == {
            "state": "idle",
            "code": None,
            "occurrence_id": None,
        }
        assert worker.status()["running"] is False
    finally:
        await worker.aclose()
