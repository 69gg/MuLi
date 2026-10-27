from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from test_voice import FakeDetector, FakePlayer, FakeRecorder, write_wav

from muli.config import VoiceSettings
from muli.media import SpeechResult
from muli.voice import VoiceWorker


class NoMedia:
    def status(self) -> dict[str, Any]:
        return {"asr": False, "tts": False}

    async def transcribe(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("设置验证不能调用 ASR")

    async def synthesize(self, text: str) -> SpeechResult:
        raise AssertionError("设置验证不能调用 TTS")


async def no_submission(event: dict[str, Any]) -> None:
    raise AssertionError("设置验证不能提交消息或主人授权")


def make_worker(
    directory: Path,
    *,
    recorder: FakeRecorder | None = None,
    wake: FakeDetector | None = None,
    speaker: FakeDetector | None = None,
    enabled: bool = False,
) -> VoiceWorker:
    return VoiceWorker(
        VoiceSettings(enabled=enabled),
        directory,
        NoMedia(),
        no_submission,
        recorder=recorder or FakeRecorder(),
        wake=wake or FakeDetector(),
        speaker=speaker or FakeDetector(),
        player=FakePlayer(),
    )


async def reached(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 2)


async def test_setup_enrollment_preserves_disabled_voice_and_private_reference(
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder()
    worker = make_worker(tmp_path, recorder=recorder, wake=FakeDetector(configured=False))
    assert worker.status()["setup"] == {"state": "idle"}
    await worker.start()
    for operation in (worker.enroll, worker.listen_once):
        with pytest.raises(ValueError, match="尚未启用"):
            await operation()
    with pytest.raises(ValueError, match="尚未启用"):
        await worker._record(1, _setup=True)
    assert recorder.calls == []

    receipt = await worker.setup_enroll(1)
    assert receipt == {
        "id": receipt["id"],
        "operation": "enroll",
        "state": "completed",
        "requested_seconds": 1,
        "enrolled": True,
    }
    pointer = worker.files.root / "enrollment.txt"
    reference = worker.files.root / (pointer.read_text(encoding="utf-8") + ".wav")
    assert reference.is_file() and reference.stat().st_mode & 0o777 == 0o600
    assert recorder.calls == [1]
    assert worker.config.enabled is False and worker.status()["running"] is False
    assert str(tmp_path) not in json.dumps(receipt)
    assert worker._operations == set()
    receipt["state"] = "changed-by-caller"
    assert worker.status()["setup"]["state"] == "completed"
    restored = make_worker(tmp_path)
    assert restored.status()["speaker"]["enrolled"] is True
    assert await worker.cancel_setup() == worker.status()["setup"]
    await worker.aclose()
    await restored.aclose()


@pytest.mark.parametrize(
    ("wake_result", "speaker_result", "configured"),
    [(False, True, True), (True, False, True), (True, True, True), (True, False, False)],
)
async def test_setup_verification_reports_only_acoustic_decisions_without_asr_or_messages(
    tmp_path: Path, wake_result: bool, speaker_result: bool, configured: bool
) -> None:
    wake = FakeDetector(wake_result)
    speaker = FakeDetector(speaker_result, configured=configured)
    worker = make_worker(tmp_path, wake=wake, speaker=speaker)
    if configured:
        await worker.setup_enroll(1)
    identifier = str(uuid4())
    receipt = await worker.setup_verify(1, identifier=identifier)
    assert receipt == {
        "id": identifier,
        "operation": "verify",
        "state": "completed",
        "requested_seconds": 1,
        "wake_detected": wake_result,
        "speaker_configured": configured,
        "speaker_verified": configured and speaker_result,
    }
    assert wake.references == [None]
    assert len(speaker.references) == int(configured)
    if speaker.references:
        assert speaker.references[0] == worker._reference
    assert len(list(worker.files.root.glob("*.wav"))) == int(configured)
    assert worker.config.enabled is False and worker.status()["running"] is False
    assert "owner" not in receipt and "text" not in receipt and "path" not in receipt
    assert worker.last_prompt == worker.last_reply == {}
    await worker.aclose()


async def test_setup_gates_and_duration_reject_before_any_recording(tmp_path: Path) -> None:
    invalid: tuple[Any, ...] = (True, False, 0, -1, 16, float("nan"), float("inf"), "5")
    worker = make_worker(tmp_path / "durations")
    for operation in (worker.setup_enroll, worker.setup_verify):
        for seconds in invalid:
            with pytest.raises(ValueError, match="时长"):
                await operation(seconds)
    worker.config.max_utterance_seconds = 2
    with pytest.raises(ValueError, match="时长"):
        await worker.setup_enroll(3)
    assert not worker.files.root.exists()

    for gate in ("recorder", "wake", "speaker", "reference", "enabled", "closed"):
        recorder = FakeRecorder()
        candidate = make_worker(tmp_path / gate, recorder=recorder, enabled=gate == "enabled")
        if gate == "recorder":
            recorder.status = lambda: {"available": False}
        if gate == "wake":
            candidate.wake = FakeDetector(configured=False)
        if gate == "speaker":
            candidate.speaker.status = lambda: {"configured": True, "available": False}
        if gate == "closed":
            await candidate.aclose()
        operation = (
            candidate.setup_enroll if gate in {"recorder", "speaker"} else candidate.setup_verify
        )
        with pytest.raises(ValueError):
            await operation(1)
        assert recorder.calls == [] and not candidate.files.root.exists()
        assert candidate.status()["setup"]["state"] == "error"
        await candidate.aclose()
    await worker.aclose()


async def test_setup_rechecks_configuration_after_waiting_for_capture_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = FakeRecorder()
    worker = make_worker(tmp_path, recorder=recorder, speaker=FakeDetector(configured=False))
    entered = asyncio.Event()
    original = worker._operation

    @asynccontextmanager
    async def operation() -> AsyncIterator[None]:
        async with original():
            entered.set()
            yield

    monkeypatch.setattr(worker, "_operation", operation)
    await worker._capture_lock.acquire()
    pending = asyncio.create_task(worker.setup_verify(1))
    try:
        await reached(entered)
        worker.config.enabled = True
    finally:
        worker._capture_lock.release()
    with pytest.raises(ValueError, match="关闭后台"):
        await pending
    assert recorder.calls == [] and not worker.files.root.exists()
    assert worker.status()["setup"]["state"] == "error"
    await worker.aclose()


class HeldRecorder(FakeRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def record(self, path: Path, seconds: float) -> None:
        self.calls.append(seconds)
        write_wav(path, seconds)
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleaning.set()
            await self.release.wait()
            raise
        finally:
            self.finished.set()


async def test_cancel_setup_settles_recording_and_leaves_unrelated_operation_running(
    tmp_path: Path,
) -> None:
    recorder = HeldRecorder()
    worker = make_worker(tmp_path, recorder=recorder)
    unrelated_entered, unrelated_release = asyncio.Event(), asyncio.Event()

    async def unrelated() -> None:
        async with worker._operation():
            unrelated_entered.set()
            await unrelated_release.wait()

    other = asyncio.create_task(unrelated())
    pending = asyncio.create_task(worker.setup_enroll(1))
    await reached(unrelated_entered)
    await reached(recorder.entered)
    assert worker.status()["setup"]["state"] == "recording"
    for operation in (worker.setup_enroll, worker.setup_verify):
        with pytest.raises(ValueError, match="正在进行"):
            await operation(1)
    cancellation = asyncio.create_task(worker.cancel_setup())
    await reached(recorder.cleaning)
    assert not cancellation.done() and not recorder.finished.is_set()
    recorder.release.set()
    receipt = await cancellation
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert receipt["state"] == "cancelled" and recorder.finished.is_set()
    assert not other.done() and worker._reference is None
    assert list(worker.files.root.iterdir()) == []
    unrelated_release.set()
    await other
    assert worker._operations == set() and worker._setup_task is None
    await worker.aclose()


class HeldDetector(FakeDetector):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.path: Path | None = None

    async def detect(self, path: Path, *, reference: Path | None = None) -> bool:
        self.path = path
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleaning.set()
            await self.release.wait()
            raise
        return True


@pytest.mark.parametrize("ending", ["stop", "close", "caller_cancel"])
async def test_setup_comparison_is_owned_until_cancellation_cleanup_finishes(
    tmp_path: Path, ending: str
) -> None:
    wake = HeldDetector()
    worker = make_worker(tmp_path, wake=wake, speaker=FakeDetector(configured=False))
    pending = asyncio.create_task(worker.setup_verify(1))
    await reached(wake.entered)
    assert worker.status()["setup"]["state"] == "comparing"
    if ending == "caller_cancel":
        pending.cancel()
        cleanup = pending
    else:
        cleanup = asyncio.create_task(worker.stop() if ending == "stop" else worker.aclose())
    await reached(wake.cleaning)
    assert not cleanup.done() and wake.path is not None and wake.path.is_file()
    if ending == "caller_cancel":
        pending.cancel()
    wake.release.set()
    if ending == "caller_cancel":
        with pytest.raises(asyncio.CancelledError):
            await cleanup
    else:
        await cleanup
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert wake.path is not None and not wake.path.exists()
    assert worker.status()["setup"]["state"] == "cancelled"
    assert worker._operations == set() and worker._setup_task is None
    assert worker.status()["running"] is False
    await worker.aclose()


async def test_setup_model_failure_cleans_audio_and_keeps_receipt_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = make_worker(tmp_path, speaker=FakeDetector(configured=False))

    async def failed(path: Path, *, reference: Path | None = None) -> bool:
        raise ValueError("PRIVATE model failure: " + str(path))

    monkeypatch.setattr(worker.wake, "detect", failed)
    with pytest.raises(ValueError, match="PRIVATE"):
        await worker.setup_verify(1)
    receipt = worker.status()["setup"]
    assert UUID(receipt["id"]).version == 4
    assert receipt == {
        "id": receipt["id"],
        "operation": "verify",
        "state": "error",
        "requested_seconds": 1,
    }
    assert "PRIVATE" not in json.dumps(receipt) and str(tmp_path) not in json.dumps(receipt)
    assert list(worker.files.root.iterdir()) == []
    assert worker._operations == set() and worker._setup_task is None
    await worker.aclose()
