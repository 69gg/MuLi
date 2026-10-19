from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path
from typing import Any

import pytest

import muli.voice as voice_module
from muli.config import VoiceSettings
from muli.media import SpeechResult
from muli.platforms.files import LocalFiles
from muli.voice import CommandDetector, SystemPlayer, SystemRecorder, VoiceWorker, devices


def write_wav(path: Path, seconds: float, sample: int = 1, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(sample.to_bytes(2, "little") * int(seconds * rate))


class FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def record(self, path: Path, seconds: float) -> None:
        self.calls.append(seconds)
        write_wav(path, seconds, len(self.calls))


class FakeDetector:
    def __init__(self, result: bool = True, *, configured: bool = True) -> None:
        self.result, self.configured = result, configured
        self.references: list[Path | None] = []

    def status(self) -> dict[str, Any]:
        return {"available": self.configured, "configured": self.configured}

    async def detect(self, path: Path, *, reference: Path | None = None) -> bool:
        self.references.append(reference)
        return self.result


class FakeMedia:
    def __init__(self) -> None:
        self.audio: list[bytes] = []

    def status(self) -> dict[str, Any]:
        return {"asr": True, "tts": True}

    async def transcribe(self, audio: bytes, filename: str, mime_type: str, **kwargs: Any) -> str:
        self.audio.append(audio)
        return "沐璃，整理一下笔记"

    async def synthesize(self, text: str) -> SpeechResult:
        return SpeechResult(b"local fake audio", "audio/mpeg", ".mp3")


class FakePlayer:
    def __init__(self, available: bool = False) -> None:
        self.available = available
        self.played: list[tuple[bytes, str]] = []

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "reason": "测试无声输出"}

    async def play(self, path: Path, mime_type: str) -> None:
        if not self.available:
            raise ValueError("测试无声输出")
        self.played.append((path.read_bytes(), mime_type))


@pytest.fixture(autouse=True)
def no_real_audio_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_module, "SystemPlayer", FakePlayer)


async def ignore_event(event: dict[str, Any]) -> None:
    return None


async def test_disabled_voice_never_records_or_starts(tmp_path: Path) -> None:
    recorder = FakeRecorder()
    worker = VoiceWorker(VoiceSettings(), tmp_path, FakeMedia(), ignore_event, recorder=recorder)
    await worker.start()
    assert worker.status()["running"] is False
    with pytest.raises(ValueError, match="尚未启用"):
        await worker.listen_once()
    with pytest.raises(ValueError, match="尚未启用"):
        await worker.enroll()
    assert recorder.calls == []
    assert not (tmp_path / "voice").exists()


async def test_missing_acoustic_backend_prevents_background_recording(tmp_path: Path) -> None:
    recorder = FakeRecorder()
    worker = VoiceWorker(
        VoiceSettings(enabled=True),
        tmp_path,
        FakeMedia(),
        ignore_event,
        recorder=recorder,
        wake=FakeDetector(configured=False),
    )
    await worker.start()
    assert worker.status()["running"] is False
    assert worker.last_error
    assert recorder.calls == []


async def test_wake_keeps_preroll_and_unverified_identity_explicit(tmp_path: Path) -> None:
    recorder, media = FakeRecorder(), FakeMedia()
    events: list[dict[str, Any]] = []

    async def receive(event: dict[str, Any]) -> None:
        events.append(event)

    worker = VoiceWorker(
        VoiceSettings(enabled=True, max_utterance_seconds=4, cooldown_seconds=0),
        tmp_path,
        media,
        receive,
        recorder=recorder,
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )
    result = await worker.listen_once()
    assert result == events[0]
    assert events[0]["speaker_verified"] is False
    assert recorder.calls == [2, 2]
    with wave.open(io.BytesIO(media.audio[0]), "rb") as source:
        frames = source.readframes(source.getnframes())
    assert frames.startswith(b"\x01\x00") and frames.endswith(b"\x02\x00")
    assert len(frames) == 4 * 16000 * 2
    assert list(worker.files.root.glob("*.wav")) == []


async def test_no_wake_does_not_send_audio_to_asr(tmp_path: Path) -> None:
    recorder, media = FakeRecorder(), FakeMedia()
    worker = VoiceWorker(
        VoiceSettings(enabled=True),
        tmp_path,
        media,
        ignore_event,
        recorder=recorder,
        wake=FakeDetector(False),
        speaker=FakeDetector(configured=False),
    )
    assert await worker.listen_once() is None
    assert recorder.calls == [2]
    assert media.audio == []
    assert list(worker.files.root.iterdir()) == []


async def test_speaker_enrollment_is_explicit_and_rejection_skips_asr(tmp_path: Path) -> None:
    recorder, media, speaker = FakeRecorder(), FakeMedia(), FakeDetector(False)
    config = VoiceSettings(enabled=True, max_utterance_seconds=4, cooldown_seconds=0)
    worker = VoiceWorker(
        config,
        tmp_path,
        media,
        ignore_event,
        recorder=recorder,
        wake=FakeDetector(),
        speaker=speaker,
    )
    assert worker.status()["available"] is False
    enrollment = await worker.enroll(3)
    assert Path(enrollment["path"]).exists()
    assert media.audio == []
    assert worker.status()["speaker"]["enrolled"] is True
    assert await worker.listen_once() is None
    assert speaker.references == [Path(enrollment["path"])]
    assert media.audio == []
    assert len(list(worker.files.root.glob("*.wav"))) == 1
    restored = VoiceWorker(
        config,
        tmp_path,
        media,
        ignore_event,
        recorder=recorder,
        wake=FakeDetector(),
        speaker=speaker,
    )
    assert restored.status()["speaker"]["enrolled"] is True
    speaker.result = True
    result = await restored.listen_once()
    assert result is not None and result["speaker_verified"] is True
    assert len(media.audio) == 1


@pytest.mark.parametrize("seconds", [0, -1, 16, float("nan"), True])
async def test_enrollment_duration_is_bounded(tmp_path: Path, seconds: float) -> None:
    worker = VoiceWorker(VoiceSettings(enabled=True), tmp_path, FakeMedia(), ignore_event)
    with pytest.raises(ValueError, match="时长"):
        await worker.enroll(seconds)


async def test_tts_saves_bounded_file_without_playback(tmp_path: Path) -> None:
    worker = VoiceWorker(
        VoiceSettings(enabled=True, tts_enabled=True), tmp_path, FakeMedia(), ignore_event
    )
    result = await worker.synthesize("你好")
    assert result["mime_type"] == "audio/mpeg"
    assert Path(result["path"]).read_bytes() == b"local fake audio"
    worker.config.tts_enabled = False
    with pytest.raises(ValueError, match="尚未启用"):
        await worker.synthesize("你好")


async def test_acoustic_bridge_forwards_structured_input_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceSettings(
        wake_command=["acoustic-model", "argument with spaces"],
        device="cpu",
        input_device="Microphone $name",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append((arguments, kwargs))
        return {"exit_code": 0, "output": '{"detected":true,"score":0.8}'}

    monkeypatch.setattr(voice_module, "run_process", run)
    monkeypatch.setattr(voice_module.shutil, "which", lambda name: "/bin/" + name)
    assert await CommandDetector(config, "wake").detect(tmp_path / "audio.wav") is True
    assert calls[0][0] == config.wake_command
    request = json.loads(calls[0][1]["input_text"])
    assert request["device"] == "cpu"
    assert request["input_device"] == "Microphone $name"
    assert request["wake_word"] == "沐璃"
    assert calls[0][1]["merge_stderr"] is False


@pytest.mark.parametrize(
    "payload",
    [
        '{"text":"沐璃","score":1}',
        '{"detected":"true","score":1}',
        '{"detected":true,"score":NaN}',
        '{"detected":true,"score":true}',
        '{"detected":true,"score":1.1}',
        '{"detected":true}',
    ],
)
async def test_bridge_never_treats_text_or_invalid_confidence_as_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 0, "output": payload}

    monkeypatch.setattr(voice_module, "run_process", run)
    monkeypatch.setattr(voice_module.shutil, "which", lambda name: "/bin/" + name)
    with pytest.raises(ValueError, match="桥接"):
        await CommandDetector(VoiceSettings(wake_command=["model"]), "wake").detect(
            tmp_path / "x.wav"
        )


def test_device_visibility_fallback_does_not_claim_model_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_module.Path, "exists", lambda path: False)
    monkeypatch.setattr(voice_module.shutil, "which", lambda name: None)
    status = devices("cuda")
    assert status["selected"] == "cpu"
    assert status["fallback"] is True
    assert status["runtime_verified"] is False
    assert devices("auto")["selected"] == "cpu"


async def test_system_recorder_argv_and_cross_platform_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceSettings(enabled=True, input_device="hw:1,0 $name")
    files = LocalFiles(tmp_path, config.max_file_bytes, config.max_total_bytes)
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        write_wav(Path(arguments[-1]), 1)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(voice_module, "run_process", run)
    monkeypatch.setattr(voice_module.shutil, "which", lambda name: "/bin/" + name)
    await SystemRecorder(config, files, platform="linux").record(files.allocate(".wav"), 1)
    assert calls[0][calls[0].index("-D") + 1] == config.input_device
    assert "-d" in calls[0]
    empty = VoiceSettings()
    assert SystemRecorder(empty, files, platform="darwin").status()["available"] is False
    assert SystemRecorder(empty, files, platform="win32").status()["available"] is False


async def test_wake_prompt_plays_tts_before_accepting_text(tmp_path: Path) -> None:
    order: list[str] = []
    player = FakePlayer(True)

    async def receive(event: dict[str, Any]) -> None:
        order.append("text")

    async def status(event: dict[str, Any]) -> None:
        order.append(event["type"])
        assert event["played"] is True
        assert event["prompt"] == "请说"

    worker = VoiceWorker(
        VoiceSettings(enabled=True, tts_enabled=True, max_utterance_seconds=4),
        tmp_path,
        FakeMedia(),
        receive,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
        player=player,
        on_status=status,
    )
    await worker.listen_once()
    assert player.played == [(b"local fake audio", "audio/mpeg")]
    assert worker.status()["last_prompt"]["kind"] == "speech"
    assert order == ["voice.prompt", "text"]
    assert list(worker.files.root.iterdir()) == []


async def test_wake_without_tts_uses_real_player_contract_for_short_tone(tmp_path: Path) -> None:
    player = FakePlayer(True)
    worker = VoiceWorker(
        VoiceSettings(enabled=True, max_utterance_seconds=4),
        tmp_path,
        FakeMedia(),
        ignore_event,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
        player=player,
    )
    await worker.listen_once()
    assert len(player.played) == 1
    assert player.played[0][1] == "audio/wav"
    with wave.open(io.BytesIO(player.played[0][0]), "rb") as source:
        assert 0 < source.getnframes() / source.getframerate() <= 0.12
    assert worker.last_prompt["played"] is True
    assert worker.last_prompt["kind"] == "tone"


async def test_unavailable_playback_emits_truthful_status_and_continues(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []

    async def status(event: dict[str, Any]) -> None:
        events.append(event)

    worker = VoiceWorker(
        VoiceSettings(enabled=True, max_utterance_seconds=4),
        tmp_path,
        FakeMedia(),
        ignore_event,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
        on_status=status,
    )
    assert await worker.listen_once() is not None
    assert events[0]["played"] is False
    assert events[0]["kind"] == "status"
    assert events[0]["reason"]


async def test_windows_playback_uses_literal_environment_path_with_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append((arguments, kwargs))
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(voice_module, "run_process", run)
    monkeypatch.setattr(
        voice_module.shutil, "which", lambda name: name if name == "powershell" else None
    )
    path = tmp_path / "语音 $(untrusted).wav"
    await SystemPlayer(platform="win32").play(path, "audio/wav")
    assert str(path) not in calls[0][0][-1]
    assert calls[0][1]["env"]["MULI_VOICE_PLAYBACK_FILE"] == str(path)
    assert calls[0][1]["timeout"] == 20


async def test_playback_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 1, "output": "no output device"}

    monkeypatch.setattr(voice_module, "run_process", run)
    monkeypatch.setattr(voice_module.shutil, "which", lambda name: "/bin/" + name)
    with pytest.raises(ValueError, match="播放失败"):
        await SystemPlayer(platform="linux").play(tmp_path / "tone.wav", "audio/wav")


async def test_stop_voice_cancels_inflight_recording_and_deletes_partial(tmp_path: Path) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class WaitingRecorder(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            write_wav(path, 0.1)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker = VoiceWorker(
        VoiceSettings(enabled=True),
        tmp_path,
        FakeMedia(),
        ignore_event,
        recorder=WaitingRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )
    await worker.start()
    await started.wait()
    await worker.stop()
    assert cancelled.is_set()
    assert worker.status()["running"] is False
    assert list(worker.files.root.iterdir()) == []


async def test_asr_failure_removes_current_audio_preserves_reference(tmp_path: Path) -> None:
    class FailedMedia(FakeMedia):
        async def transcribe(
            self, audio: bytes, filename: str, mime_type: str, **kwargs: Any
        ) -> str:
            raise ValueError("模型临时离线")

    worker = VoiceWorker(
        VoiceSettings(enabled=True, max_utterance_seconds=4),
        tmp_path,
        FailedMedia(),
        ignore_event,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(),
    )
    reference = await worker.enroll(1)
    with pytest.raises(ValueError, match="临时离线"):
        await worker.listen_once()
    assert list(worker.files.root.glob("*.wav")) == [Path(reference["path"])]


@pytest.mark.parametrize("operation", ["listen", "enroll"])
async def test_close_cancels_direct_recording_and_rejects_new_operations(
    tmp_path: Path,
    operation: str,
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class WaitingRecorder(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            write_wav(path, 0.1)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker = VoiceWorker(
        VoiceSettings(enabled=True),
        tmp_path,
        FakeMedia(),
        ignore_event,
        recorder=WaitingRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )
    task = asyncio.create_task(worker.listen_once() if operation == "listen" else worker.enroll())
    async with asyncio.timeout(3):
        await started.wait()
        await worker.aclose()
    assert task.cancelled() and cancelled.is_set()
    assert list(worker.files.root.iterdir()) == []
    assert worker.status()["closed"] is True and worker.status()["available"] is False
    for callback in (worker.start, worker.listen_once, worker.enroll):
        with pytest.raises(ValueError, match="已关闭"):
            await callback()


async def test_close_cancels_queued_enrollment_before_it_opens_recorder(tmp_path: Path) -> None:
    started, queued = asyncio.Event(), asyncio.Event()
    calls = 0

    class WaitingRecorder(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            nonlocal calls
            calls += 1
            write_wav(path, 0.1)
            started.set()
            await asyncio.Event().wait()

    worker = VoiceWorker(
        VoiceSettings(enabled=True),
        tmp_path,
        FakeMedia(),
        ignore_event,
        recorder=WaitingRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )

    async def enrollment() -> dict[str, Any]:
        queued.set()
        return await worker.enroll()

    listening = asyncio.create_task(worker.listen_once())
    async with asyncio.timeout(3):
        await started.wait()
        enrolling = asyncio.create_task(enrollment())
        await queued.wait()
        await worker.aclose()
    assert listening.cancelled() and enrolling.cancelled()
    assert calls == 1 and list(worker.files.root.iterdir()) == []


async def test_close_cancels_inflight_tts_without_saving_or_playing(tmp_path: Path) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class WaitingMedia(FakeMedia):
        async def synthesize(self, text: str) -> SpeechResult:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("TTS should be cancelled")

    worker = VoiceWorker(
        VoiceSettings(enabled=True, tts_enabled=True),
        tmp_path,
        WaitingMedia(),
        ignore_event,
    )
    task = asyncio.create_task(worker.synthesize("未完成的语音"))
    async with asyncio.timeout(3):
        await started.wait()
        await worker.aclose()
    assert task.cancelled() and cancelled.is_set()
    assert not worker.files.root.exists()
    with pytest.raises(ValueError, match="已关闭"):
        await worker.synthesize("不能在关闭后发送TTS请求")


async def test_cancelled_close_still_waits_for_recording_cleanup(tmp_path: Path) -> None:
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowCleanupRecorder(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            write_wav(path, 0.1)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

    worker = VoiceWorker(
        VoiceSettings(enabled=True),
        tmp_path,
        FakeMedia(),
        ignore_event,
        recorder=SlowCleanupRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )
    recording = asyncio.create_task(worker.listen_once())
    async with asyncio.timeout(3):
        await started.wait()
        closing = asyncio.create_task(worker.aclose())
        await cleaning.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
    assert recording.cancelled()
    assert list(worker.files.root.iterdir()) == []
    assert not worker._operations


async def test_stop_allows_a_later_start_and_callback_can_stop_itself(tmp_path: Path) -> None:
    stopped = asyncio.Event()
    worker: VoiceWorker

    async def on_text(event: dict[str, Any]) -> None:
        await worker.stop()
        stopped.set()

    worker = VoiceWorker(
        VoiceSettings(enabled=True, max_utterance_seconds=4, cooldown_seconds=0),
        tmp_path,
        FakeMedia(),
        on_text,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )
    async with asyncio.timeout(3):
        await worker.start()
        await stopped.wait()
        await worker.stop()
        stopped.clear()
        await worker.start()
        await stopped.wait()
        await worker.aclose()
    assert not worker.status()["running"]
    assert list(worker.files.root.iterdir()) == []


async def test_concurrent_stop_and_callback_stop_do_not_deadlock(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    worker: VoiceWorker

    async def on_text(event: dict[str, Any]) -> None:
        entered.set()
        await release.wait()
        await worker.stop()

    worker = VoiceWorker(
        VoiceSettings(enabled=True, max_utterance_seconds=4, cooldown_seconds=0),
        tmp_path,
        FakeMedia(),
        on_text,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )
    async with asyncio.timeout(3):
        await worker.start()
        background = worker._task
        assert background is not None
        await entered.wait()
        stopping = asyncio.create_task(worker.stop())
        release.set()
        await stopping
        await asyncio.gather(background, return_exceptions=True)
        await worker.aclose()
    assert not worker._operations and worker._stops == 0
    assert list(worker.files.root.iterdir()) == []


async def test_voice_reply_can_synthesize_and_close_still_tracks_the_outer_operation(
    tmp_path: Path,
) -> None:
    replied = asyncio.Event()
    worker: VoiceWorker

    async def on_text(event: dict[str, Any]) -> None:
        speech = await worker.synthesize("我听到了")
        assert Path(speech["path"]).exists()
        replied.set()
        await asyncio.Event().wait()

    worker = VoiceWorker(
        VoiceSettings(enabled=True, tts_enabled=True, max_utterance_seconds=4, cooldown_seconds=0),
        tmp_path,
        FakeMedia(),
        on_text,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
    )
    listening = asyncio.create_task(worker.listen_once())
    try:
        async with asyncio.timeout(3):
            await replied.wait()
            await worker.aclose()
        assert listening.cancelled()
        assert not worker._operations
        assert [path.suffix for path in worker.files.root.iterdir()] == [".mp3"]
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)
        await worker.aclose()
