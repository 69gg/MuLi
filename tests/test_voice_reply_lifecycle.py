"""Focused real-worker lifecycle boundaries; the separate formal uses ASR/TTS HTTP."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_runtime import make_runtime
from test_voice import FakeDetector, FakeMedia, FakePlayer, FakeRecorder

import muli.voice as voice_module
from muli.attachments import Attachments
from muli.config import VoiceSettings
from muli.local_services import LocalServices
from muli.media import SpeechResult
from muli.personal_io import PersonalIO
from muli.platforms.files import LocalFiles
from muli.providers import ModelTurn
from muli.voice import VoiceWorker


class WaitingMedia(FakeMedia):
    def __init__(self, answer: str) -> None:
        super().__init__()
        self.answer = answer
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.inputs: list[str] = []

    async def synthesize(self, text: str) -> SpeechResult:
        self.inputs.append(text)
        if text == self.answer:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return await super().synthesize(text)

    async def aclose(self) -> None:
        return None


class WaitingPlayer(FakePlayer):
    def __init__(self) -> None:
        super().__init__(True)
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def play(self, path: Path, mime_type: str) -> None:
        if self.played:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        await super().play(path, mime_type)


@pytest.mark.parametrize(
    "action", ["new_input", "runtime_stop", "voice_stop", "runtime_stop_playing"]
)
async def test_waiting_reply_is_revoked_without_cancelling_its_completed_main_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    answer = "本轮已经完成。"
    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content=answer), ModelTurn(content="已改用网页。")]
    )
    runtime.config.update(
        {
            "voice": {
                "enabled": True,
                "tts_enabled": True,
                "reply_enabled": True,
                "max_utterance_seconds": 4,
            },
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
        }
    )
    media = WaitingMedia(answer)
    player = WaitingPlayer() if action == "runtime_stop_playing" else FakePlayer(True)
    if action == "runtime_stop_playing":
        media.release.set()
    recorder, wake, speaker = FakeRecorder(), FakeDetector(), FakeDetector()

    def make_recorder(config: VoiceSettings, files: LocalFiles) -> FakeRecorder:
        return recorder

    def make_detector(config: VoiceSettings, operation: str) -> FakeDetector:
        return wake if operation == "wake" else speaker

    monkeypatch.setattr(voice_module, "SystemRecorder", make_recorder)
    monkeypatch.setattr(voice_module, "CommandDetector", make_detector)
    monkeypatch.setattr(voice_module, "SystemPlayer", lambda: player)
    attachments = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    local = LocalServices(runtime, attachments)
    monkeypatch.setattr(local, "media", lambda: media)
    io = PersonalIO(runtime, local, attachments)
    try:
        await io.voice.enroll(1)
        event = await io.voice.listen_once()
        assert event is not None and event["speaker_verified"] is True
        await asyncio.wait_for(media.entered.wait(), 3)
        if isinstance(player, WaitingPlayer):
            await asyncio.wait_for(player.entered.wait(), 3)
        reply = io._reply_task
        assert reply is not None and not reply.done()
        run_id = io.voice.last_reply["run_id"]
        assert runtime.store.get("run", run_id)["status"] == "completed"
        assert await runtime.wait(run_id) == answer
        epoch = runtime.voice_reply_epoch
        if action == "new_input":
            submitted = await runtime.submit("改用网页继续")
            assert runtime.voice_reply_epoch > epoch
            assert await runtime.wait(submitted["run_id"]) == "已改用网页。"
        elif action in {"runtime_stop", "runtime_stop_playing"}:
            await runtime.stop(runtime.main_session()["id"])
            assert runtime.voice_reply_epoch > epoch
        else:
            await io.voice.stop()
            assert runtime.voice_reply_epoch == epoch
        cancelled = player.cancelled if isinstance(player, WaitingPlayer) else media.cancelled
        await asyncio.wait_for(cancelled.wait(), 3)
        await asyncio.wait_for(asyncio.shield(reply), 3)
        assert io.voice.last_reply["state"] == (
            "unknown" if action == "runtime_stop_playing" else "cancelled"
        )
        assert runtime.store.get("run", run_id)["status"] == "completed"
        assert media.inputs == ["请说", answer]
        assert len(player.played) == 1  # Only the real wake prompt was played.
        assert len(seen) == (2 if action == "new_input" else 1)
    finally:
        await io.close()
        await runtime.close()


async def test_reply_rechecks_after_tts_and_retains_audio_on_rejection(tmp_path: Path) -> None:
    player = FakePlayer(True)
    worker = VoiceWorker(
        VoiceSettings(enabled=True, tts_enabled=True, reply_enabled=True),
        tmp_path,
        FakeMedia(),
        ignore_voice,
        recorder=FakeRecorder(),
        wake=FakeDetector(),
        speaker=FakeDetector(),
        player=player,
    )
    checks = 0

    def guard() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise PermissionError("已撤销本次播放")

    try:
        with pytest.raises(PermissionError, match="已撤销"):
            await worker.reply("一次答复", admission_guard=guard)
        assert checks == 2 and player.played == []
        audio = worker.last_reply["audio"]
        assert Path(audio["path"]).read_bytes() == b"local fake audio"
        assert audio["size"] <= worker.config.max_file_bytes
    finally:
        await worker.aclose()


async def ignore_voice(event: dict[str, Any]) -> None:
    return None


async def test_reconfigured_worker_callback_cannot_submit_after_waiting_for_configuration_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    files = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    io = PersonalIO(runtime, LocalServices(runtime, files), files)
    original = runtime.submit
    entered = asyncio.Event()

    async def waiting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, "submit", waiting)
    old = io.voice
    try:
        async with runtime.configuration_lock:
            pending = asyncio.create_task(
                old.on_text({"text": "旧实例的迟到语音", "speaker_verified": True})
            )
            await asyncio.wait_for(entered.wait(), 3)
            await io.reconfigure()  # Voice remains disabled: no device or listener starts.
            assert io.voice is not old
        with pytest.raises(Exception) as error:
            await pending
        assert type(error.value).__name__ == "_ReplyRevoked"
        assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0
        assert seen == [] and runtime.tasks == {}
    finally:
        await io.close()
        await runtime.close()


async def test_duplicate_owner_receipt_and_quarantine_do_not_revoke_current_epoch(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="第一次回应")])
    try:
        first = await runtime.submit("固定信封", event_id="same-owner-input")
        epoch = first["voice_reply_epoch"]
        assert await runtime.wait(first["run_id"]) == "第一次回应"
        duplicate = await runtime.submit("固定信封", event_id="same-owner-input")
        assert duplicate["duplicate"] is True
        assert duplicate["voice_reply_epoch"] == epoch == runtime.voice_reply_epoch
        untrusted = await runtime.submit("外来文本", principal="external:voice:unverified")
        assert untrusted["quarantined"] is True
        assert untrusted["voice_reply_epoch"] == epoch == runtime.voice_reply_epoch
    finally:
        await runtime.close()
