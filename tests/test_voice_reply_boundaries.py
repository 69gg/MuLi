"""Real voice ingress and HTTP boundaries; acoustic hardware remains offline."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from test_attention import eventually as settled
from test_runtime import FakeClient, make_runtime
from test_voice import FakeDetector, FakePlayer, FakeRecorder

import muli.voice as voice_module
from muli.config import ConfigFile, VoiceSettings
from muli.context import context_size
from muli.platforms.files import LocalFiles
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.voice import VoiceWorker

VOICE_INPUT = "沐璃，请给这次本地语音一个简短回答。"
VOICE_ANSWER = "这次语音的回答已经完成。"
WEB_INPUT = "我现在改用网页继续。"
WEB_ANSWER = "已切换到网页继续。"
PROMPT_AUDIO = b"ID3-OFFLINE-BOUNDARY-PROMPT"
ANSWER_AUDIO = b"ID3-OFFLINE-BOUNDARY-ANSWER"


class UncertainPlayer(FakePlayer):
    """The output device starts accepting bytes, then loses its result channel."""

    def __init__(self) -> None:
        super().__init__(True)
        self.started: list[tuple[bytes, str]] = []

    async def play(self, path: Path, mime_type: str) -> None:
        audio = path.read_bytes()
        if audio == ANSWER_AUDIO:
            self.started.append((audio, mime_type))
            raise OSError("synthetic playback result channel lost after start")
        await super().play(path, mime_type)


@dataclass
class VoiceCase:
    runtime: Runtime
    app: FastAPI
    web: httpx.AsyncClient
    worker: VoiceWorker
    recorder: FakeRecorder
    wake: FakeDetector
    speaker: FakeDetector
    player: FakePlayer
    tts_inputs: list[str]
    seen: list[list[Message]]
    tts_entered: asyncio.Event
    tts_cancelled: asyncio.Event
    tts_release: asyncio.Event
    main_entered: asyncio.Event
    main_cancelled: asyncio.Event
    main_release: asyncio.Event

    async def listen(self, *, verified: bool = True) -> dict[str, Any]:
        # Startup saw a configured but unenrolled verifier, so no background capture
        # raced this explicit listen_once. Enrollment and PCM processing stay real.
        assert self.worker.status()["running"] is False and self.recorder.calls == []
        enrollment = await self.worker.enroll(1)
        reference = Path(enrollment["path"])
        assert reference.is_file() and self.worker.status()["speaker"]["enrolled"] is True
        if not verified:
            self.speaker.configured = False
        event = await self.worker.listen_once()
        assert event is not None and event["speaker_verified"] is verified
        assert event["text"] == VOICE_INPUT and self.recorder.calls == [1, 2, 2]
        assert self.speaker.references == ([reference] if verified else [])
        return event

    def ingress(self) -> dict[str, Any]:
        rows = self.runtime.store.db.execute(
            "SELECT id,state,run_id,payload FROM ingress WHERE session_id=? "
            "AND json_extract(payload,'$.source')='voice'",
            (self.runtime.main_session()["id"],),
        ).fetchall()
        assert len(rows) == 1
        return dict(rows[0])

    async def completed_voice(self) -> str:
        await settled(lambda: self.ingress()["state"] == "delivered")
        ingress = self.ingress()
        assert json.loads(ingress["payload"])["principal"] == "owner"
        run_id = ingress["run_id"]
        assert isinstance(run_id, str)
        assert await asyncio.wait_for(self.runtime.wait(run_id), 3) == VOICE_ANSWER
        assert self.runtime.store.get("run", run_id)["status"] == "completed"
        return run_id

    def prompt_only(self) -> None:
        assert self.tts_inputs == ["请说"]
        assert self.player.played == [(PROMPT_AUDIO, "audio/mpeg")]


@asynccontextmanager
async def voice_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply_enabled: bool | None = True,
    block_tts: bool = False,
    block_main: bool = False,
    player: FakePlayer | None = None,
) -> AsyncIterator[VoiceCase]:
    recorder, wake, speaker = FakeRecorder(), FakeDetector(), FakeDetector()
    selected_player = player if player is not None else FakePlayer(True)
    tts_inputs: list[str] = []
    seen: list[list[Message]] = []
    tts_entered, tts_cancelled, tts_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    main_entered, main_cancelled, main_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def make_recorder(config: VoiceSettings, files: LocalFiles) -> FakeRecorder:
        return recorder

    def make_detector(config: VoiceSettings, operation: str) -> FakeDetector:
        assert operation in {"wake", "verify"}
        return wake if operation == "wake" else speaker

    def make_player() -> FakePlayer:
        return selected_player

    monkeypatch.setattr(voice_module, "SystemRecorder", make_recorder)
    monkeypatch.setattr(voice_module, "CommandDetector", make_detector)
    monkeypatch.setattr(voice_module, "SystemPlayer", make_player)

    async def media_http(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        if request.url.host == "asr.invalid":
            assert request.url.path == "/v1/audio/transcriptions"
            multipart = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            parts = {
                part.get_param("name", header="content-disposition"): part
                for part in multipart.iter_parts()
            }
            assert parts["model"].get_payload(decode=True) == b"synthetic-asr"
            audio = parts["file"].get_payload(decode=True)
            assert isinstance(audio, bytes)
            with wave.open(io.BytesIO(audio), "rb") as source:
                assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (
                    1,
                    2,
                    16000,
                )
                assert source.getnframes() == 4 * 16000
            return httpx.Response(200, json={"text": VOICE_INPUT})
        assert request.url.host == "tts.invalid" and request.url.path == "/v1/audio/speech"
        body = json.loads(request.content)
        assert body["model"] == "synthetic-tts" and body["voice"] == "offline-test-voice"
        assert body["response_format"] == "mp3"
        text = body["input"]
        assert text in {"请说", VOICE_ANSWER}
        tts_inputs.append(text)
        if text == VOICE_ANSWER:
            tts_entered.set()
            if block_tts:
                try:
                    await tts_release.wait()
                except asyncio.CancelledError:
                    tts_cancelled.set()
                    raise
        return httpx.Response(
            200,
            content=PROMPT_AUDIO if text == "请说" else ANSWER_AUDIO,
            headers={"Content-Type": "audio/mpeg"},
        )

    transport = httpx.MockTransport(media_http)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        }
    )
    await runtime.reconfigure()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            assert len(seen) <= 2 and len(messages) <= 32 and context_size(messages) <= 16000
            if len(seen) == 1:
                assert messages[-1].content == VOICE_INPUT
                main_entered.set()
                if block_main:
                    try:
                        await main_release.wait()
                    except asyncio.CancelledError:
                        main_cancelled.set()
                        raise
                answer = VOICE_ANSWER
            else:
                assert messages[-1].content == WEB_INPUT
                answer = WEB_ANSWER
            yield StreamEvent("text_delta", text=answer)
            yield StreamEvent("done", turn=ModelTurn(content=answer))

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model == "main"
        return Main([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            voice: dict[str, Any] = {
                "enabled": True,
                "tts_enabled": True,
                "device": "cpu",
                "max_utterance_seconds": 4,
                "cooldown_seconds": 0,
            }
            if reply_enabled is not None:
                voice["reply_enabled"] = reply_enabled
            configured = await web.patch(
                "/api/settings",
                json={
                    "voice": voice,
                    "media": {
                        "asr": {"model": "synthetic-asr", "base_url": "https://asr.invalid/v1"},
                        "tts": {
                            "model": "synthetic-tts",
                            "base_url": "https://tts.invalid/v1",
                            "voice": "offline-test-voice",
                        },
                    },
                },
            )
            assert configured.status_code == 200
            assert configured.json()["voice"]["reply_enabled"] is (reply_enabled is True)
            main_id = runtime.main_session()["id"]
            assert (
                await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            ).status_code == 200
            yield VoiceCase(
                runtime,
                app,
                web,
                app.state.personal_io.voice,
                recorder,
                wake,
                speaker,
                selected_player,
                tts_inputs,
                seen,
                tts_entered,
                tts_cancelled,
                tts_release,
                main_entered,
                main_cancelled,
                main_release,
            )
    finally:
        tts_release.set()
        main_release.set()
        await app.state.close()


async def test_default_reply_disabled_keeps_verified_voice_text_without_final_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with voice_case(tmp_path, monkeypatch, reply_enabled=None) as case:
        await case.listen()
        await case.completed_voice()
        await settled(lambda: not case.runtime.tasks)
        assert case.app.state.personal_io._reply_task is None
        assert case.worker.last_reply == {} and len(case.seen) == 1
        case.prompt_only()


async def test_unverified_voice_is_quarantined_without_main_run_or_final_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with voice_case(tmp_path, monkeypatch) as case:
        await case.listen(verified=False)
        ingress = case.ingress()
        assert ingress["state"] == "quarantined" and ingress["run_id"] is None
        assert json.loads(ingress["payload"])["principal"] == "external:voice:unverified"
        assert case.runtime.tasks == {} and case.seen == []
        assert (
            case.runtime.store.db.execute(
                "SELECT count(*) FROM objects WHERE kind='run'"
            ).fetchone()[0]
            == 0
        )
        assert case.app.state.personal_io._reply_task is None
        case.prompt_only()


@pytest.mark.parametrize("action", ["new_owner_input", "stop_completed_run"])
async def test_tts_http_is_revoked_by_real_owner_control_after_main_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    async with voice_case(tmp_path, monkeypatch, block_tts=True) as case:
        await case.listen()
        await asyncio.wait_for(case.tts_entered.wait(), 3)
        run_id = await case.completed_voice()
        main_id = case.runtime.main_session()["id"]
        if action == "new_owner_input":
            response = await case.web.post(
                f"/api/sessions/{main_id}/messages", json={"content": WEB_INPUT}
            )
            assert response.status_code == 200
            assert (
                await asyncio.wait_for(case.runtime.wait(response.json()["run_id"]), 3)
                == WEB_ANSWER
            )
        else:
            response = await case.web.post(f"/api/sessions/{main_id}/stop", json={})
            assert response.status_code == 200 and response.json() == {"stopped": True}
        await asyncio.wait_for(case.tts_cancelled.wait(), 3)
        await settled(lambda: case.app.state.personal_io._reply_task is None)
        assert case.worker.last_reply["state"] == "cancelled"
        assert case.runtime.store.get("run", run_id)["status"] == "completed"
        assert case.tts_inputs == ["请说", VOICE_ANSWER]
        assert case.player.played == [(PROMPT_AUDIO, "audio/mpeg")]
        assert len(case.seen) == (2 if action == "new_owner_input" else 1)


async def test_settings_disable_replaces_worker_and_revokes_its_tts_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with voice_case(tmp_path, monkeypatch, block_tts=True) as case:
        await case.listen()
        await asyncio.wait_for(case.tts_entered.wait(), 3)
        run_id = await case.completed_voice()
        await settled(lambda: not case.runtime.busy)
        response = await case.web.patch("/api/settings", json={"voice": {"enabled": False}})
        assert response.status_code == 200 and response.json()["voice"]["enabled"] is False
        await asyncio.wait_for(case.tts_cancelled.wait(), 3)
        current = case.app.state.personal_io.voice
        assert current is not case.worker and current.status()["running"] is False
        assert case.worker.status()["closed"] is True
        with pytest.raises(ValueError):
            await case.worker.listen_once()
        assert case.worker.last_reply["state"] == "cancelled"
        assert case.runtime.store.get("run", run_id)["status"] == "completed"
        assert case.runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 1
        assert case.tts_inputs == ["请说", VOICE_ANSWER]
        assert case.player.played == [(PROMPT_AUDIO, "audio/mpeg")]


async def test_personal_io_close_does_not_cancel_the_shielded_main_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with voice_case(tmp_path, monkeypatch, block_main=True) as case:
        await case.listen()
        await asyncio.wait_for(case.main_entered.wait(), 3)
        await settled(lambda: case.worker.last_reply.get("run_id") == case.ingress()["run_id"])
        run_id = case.ingress()["run_id"]
        assert isinstance(run_id, str)
        task = case.runtime.tasks[case.runtime.main_session()["id"]]
        assert not task.done() and case.worker.last_reply["state"] == "waiting"
        await asyncio.wait_for(case.app.state.personal_io.close(), 3)
        assert not task.done() and not task.cancelled() and not case.main_cancelled.is_set()
        assert case.runtime.store.get("run", run_id)["status"] == "running"
        assert case.worker.last_reply["state"] == "cancelled"
        case.main_release.set()
        assert await asyncio.wait_for(case.runtime.wait(run_id), 3) == VOICE_ANSWER
        assert case.runtime.store.get("run", run_id)["status"] == "completed"
        case.prompt_only()


async def test_uncertain_player_result_keeps_audio_and_normal_reopen_does_not_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    player = UncertainPlayer()
    async with voice_case(tmp_path, monkeypatch, player=player) as case:
        await case.listen()
        run_id = await case.completed_voice()
        await settled(lambda: case.worker.last_reply.get("state") == "unknown")
        await settled(lambda: case.app.state.personal_io._reply_task is None)
        assert player.started == [(ANSWER_AUDIO, "audio/mpeg")]
        assert player.played == [(PROMPT_AUDIO, "audio/mpeg")]
        artifact_path = Path(case.worker.last_reply["audio"]["path"])
        assert artifact_path.read_bytes() == ANSWER_AUDIO
        assert case.tts_inputs == ["请说", VOICE_ANSWER] and len(case.seen) == 1
        config_path, data_dir = case.runtime.config.path, case.runtime.data_dir

    # Normal application shutdown finished above. With the acoustic wake bridge
    # unavailable on reopen, no new recording can be mistaken for a resumed ticket.
    case.wake.configured = False

    def no_model(config: ModelConfig) -> FakeClient:
        raise AssertionError("normal reopen must not regenerate the completed voice answer")

    reopened = Runtime(ConfigFile(config_path), data_dir, client_factory=no_model)  # type: ignore[arg-type]
    app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert app.state.personal_io.voice.status()["running"] is False
            assert app.state.personal_io.voice.last_reply == {}
            assert app.state.personal_io._reply_task is None
            assert reopened.store.get("run", run_id)["status"] == "completed"
            history = await web.get(f"/api/sessions/{reopened.main_session()['id']}")
            assert history.status_code == 200
            assert sum(item["content"] == VOICE_ANSWER for item in history.json()["messages"]) == 1
            assert artifact_path.read_bytes() == ANSWER_AUDIO
            assert case.tts_inputs == ["请说", VOICE_ANSWER]
            assert player.started == [(ANSWER_AUDIO, "audio/mpeg")]
            assert reopened.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 1
    finally:
        await app.state.close()
