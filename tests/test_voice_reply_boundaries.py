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
from muli.config import VoiceSettings
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


async def test_verified_voice_input_never_auto_plays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # reply_enabled only permits the voice_speak tool to use the speaker; a
    # completed voice conversation must not synthesize or play on its own.
    async with voice_case(tmp_path, monkeypatch, reply_enabled=True) as case:
        await case.listen()
        await case.completed_voice()
        await settled(lambda: not case.runtime.tasks)
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
        case.prompt_only()
