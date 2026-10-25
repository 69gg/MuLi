"""A verified local voice input reaches the shared assistant and its real audio bridge."""

from __future__ import annotations

import hashlib
import io
import json
import wave
from collections.abc import AsyncIterator
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attention import eventually as settled
from test_mobile_task_control_followup import phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually
from test_voice import FakeDetector, FakePlayer, FakeRecorder

import muli.voice as voice_module
from muli.config import VoiceSettings
from muli.context import context_size
from muli.platforms.files import LocalFiles
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.server import create_app


async def test_verified_voice_final_reply_is_spoken_once_and_mobile_stays_in_the_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice_input = "沐璃，请核对当前提醒后回答，保持这个会话。"
    intermediate = "我先核对当前提醒。"
    final_reply = "当前没有提醒。我们继续在同一个会话里交流。"
    phone_input = "我改用手机继续，请确认刚才的语音和提醒核查。"
    phone_reply = "已接上刚才的语音会话，提醒核查结果仍在。"
    prompt_audio = b"ID3-SYNTHETIC-VOICE-PROMPT"
    reply_audio = b"ID3-SYNTHETIC-VOICE-FINAL"
    recorder, wake, speaker, player = (
        FakeRecorder(),
        FakeDetector(),
        FakeDetector(),
        FakePlayer(True),
    )
    asr_audio: list[bytes] = []
    tts_inputs: list[str] = []
    phone_requests: list[dict[str, Any]] = []

    def make_recorder(config: VoiceSettings, files: LocalFiles) -> FakeRecorder:
        return recorder

    def make_detector(config: VoiceSettings, operation: str) -> FakeDetector:
        assert operation in {"wake", "verify"}
        return wake if operation == "wake" else speaker

    def make_player() -> FakePlayer:
        return player

    monkeypatch.setattr(voice_module, "SystemRecorder", make_recorder)
    monkeypatch.setattr(voice_module, "CommandDetector", make_detector)
    monkeypatch.setattr(voice_module, "SystemPlayer", make_player)

    async def media_and_platform(request: httpx.Request) -> httpx.Response:
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
            assert parts["language"].get_payload(decode=True) == b"zh"
            assert parts["file"].get_content_type() == "audio/wav"
            audio = parts["file"].get_payload(decode=True)
            assert isinstance(audio, bytes)
            asr_audio.append(audio)
            return httpx.Response(200, json={"text": voice_input})
        if request.url.host == "tts.invalid":
            assert request.url.path == "/v1/audio/speech"
            body = json.loads(request.content)
            assert body["model"] == "synthetic-tts"
            assert body["voice"] == "offline-test-voice" and body["response_format"] == "mp3"
            text = body["input"]
            tts_inputs.append(text)
            assert text in {"请说", final_reply}
            return httpx.Response(
                200,
                content=prompt_audio if text == "请说" else reply_audio,
                headers={"Content-Type": "audio/mpeg"},
            )
        assert request.url.host == "platform.invalid"
        assert request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert str(body["user_id"]) == "7"
        assert all(part["type"] == "text" for part in body["message"])
        phone_requests.append(body)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 7001}}
        )

    transport = httpx.MockTransport(media_and_platform)

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
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7"],
                }
            },
        }
    )
    await runtime.reconfigure()
    rounds = 0
    seen: list[list[Message]] = []

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            seen.append(list(messages))
            assert len(messages) <= 32 and context_size(messages) <= 16000
            if rounds == 1:
                assert messages[-1].role == "user" and messages[-1].content == voice_input
                assert "reminder_list" in {tool["function"]["name"] for tool in tools}
                turn = ModelTurn(
                    content=intermediate,
                    tool_calls=[call("voice-check-reminders", "reminder_list")],
                )
            elif rounds == 2:
                result = next(
                    message
                    for message in messages
                    if message.tool_call_id == "voice-check-reminders"
                )
                assert json.loads(result.content) == []
                turn = ModelTurn(content=final_reply)
            else:
                assert rounds == 3 and messages[-1].role == "user"
                identifier = messages[-1].source_message_id
                assert isinstance(identifier, str)
                stored_phone = runtime.store.get("message", identifier)
                assert stored_phone is not None and stored_phone["content"] == phone_input
                assert stored_phone["principal"] == "owner"
                assert stored_phone["source"] == "channel:onebot:123"
                prefix = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                header, separator, original = messages[-1].content.partition("\n")
                assert header.startswith(prefix) and separator and original == phone_input
                route = stored_phone["route"]
                assert json.loads(header[len(prefix) :]) == {
                    "channel": "onebot",
                    "account_id": route["account_id"],
                    "sender_id": "7",
                    "target": "7",
                    "kind": "private",
                    "input_id": identifier,
                }
                assert any(message.content == voice_input for message in messages)
                assert any(message.content == final_reply for message in messages)
                turn = ModelTurn(content=phone_reply)
            if turn.content:
                midpoint = len(turn.content) // 2
                yield StreamEvent("text_delta", text=turn.content[:midpoint])
                yield StreamEvent("text_delta", text=turn.content[midpoint:])
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model == "main"
        return Main([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    main_id = runtime.main_session()["id"]
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            configured = await web.patch(
                "/api/settings",
                json={
                    "voice": {
                        "enabled": True,
                        "tts_enabled": True,
                        "reply_enabled": True,
                        "device": "cpu",
                        "max_utterance_seconds": 4,
                        "cooldown_seconds": 0,
                    },
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
            assert configured.json()["voice"]["reply_enabled"] is True
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
            worker = app.state.personal_io.voice
            assert worker.status()["running"] is False
            assert worker.status()["speaker"]["enrolled"] is False and recorder.calls == []
            enrollment = await worker.enroll(1)
            reference = Path(enrollment["path"])
            assert reference.is_file()
            assert (reference.parent / "enrollment.txt").read_text(
                encoding="utf-8"
            ) == reference.stem
            assert worker.status()["available"] is True
            event = await worker.listen_once()
            assert event is not None
            assert event["speaker_verified"] is True and event["text"] == voice_input
            assert recorder.calls == [1, 2, 2]
            assert len(wake.references) == 1 and speaker.references == [reference]
            assert len(asr_audio) == 1
            with wave.open(io.BytesIO(asr_audio[0]), "rb") as audio:
                assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                    1,
                    2,
                    16000,
                )
                frames = audio.readframes(audio.getnframes())
            assert len(frames) == 4 * 16000 * 2
            assert frames.startswith(b"\x02\x00") and frames.endswith(b"\x03\x00")

            await eventually(runtime, lambda: bool(completed(runtime, final_reply)))
            ingress = runtime.store.db.execute(
                "SELECT id,state,run_id,payload FROM ingress WHERE session_id=? "
                "AND json_extract(payload,'$.source')='voice'",
                (main_id,),
            ).fetchall()
            assert len(ingress) == 1 and ingress[0]["state"] == "delivered"
            payload = json.loads(ingress[0]["payload"])
            assert payload["principal"] == "owner" and payload["content"] == voice_input
            run_id = ingress[0]["run_id"]
            assert await runtime.wait(run_id) == final_reply
            assert runtime.store.get("run", run_id)["status"] == "completed"
            tool = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "voice-check-reminders"
            )
            assert tool["role"] == "tool" and tool["content_provenance"] == "accepted"
            assert json.loads(tool["content"]) == []
            assert len(completed(runtime, final_reply)) == 1 and rounds == 2
            history = await web.get(f"/api/sessions/{main_id}")
            assert history.status_code == 200
            assert any(item["content"] == final_reply for item in history.json()["messages"])
            assert worker.last_prompt["played"] is True

            # All real PCM, identity, ASR, ingress, tool and final-run prerequisites passed.
            # Prompt synthesis is independent of the missing final-answer bridge.
            try:
                await settled(
                    lambda: (
                        final_reply in tts_inputs and (reply_audio, "audio/mpeg") in player.played
                    )
                )
            except TimeoutError:
                assert [text for text in tts_inputs if text != "请说"] == [final_reply], (
                    "已验证语音的真实主运行已完成，但最终答复没有经 TTS HTTP 合成；"
                    f"仅观测到 {len(tts_inputs)} 次提示/答复合成和 {len(player.played)} 次播放"
                )
                assert (reply_audio, "audio/mpeg") in player.played
            assert tts_inputs == ["请说", final_reply]
            assert player.played == [(prompt_audio, "audio/mpeg"), (reply_audio, "audio/mpeg")]
            assert (
                hashlib.sha256(player.played[1][0]).digest() == hashlib.sha256(reply_audio).digest()
            )
            assert intermediate not in tts_inputs and phone_requests == []

            assert (await phone(web, phone_input, 2601)).status_code == 200
            await eventually(runtime, lambda: bool(completed(runtime, phone_reply)))
            manager = app.state.channel_application.manager
            phone_message = completed(runtime, phone_reply)[0]
            await processed_reply(runtime, manager, phone_message["id"])
            deliveries = manager.deliveries()
            assert deliveries == [] and phone_requests == []
            assert runtime.main_session()["id"] == main_id and rounds == len(seen) == 3
            history = await web.get(f"/api/sessions/{main_id}")
            assert history.status_code == 200
            assert all(
                any(item["content"] == content for item in history.json()["messages"])
                for content in (voice_input, final_reply, phone_input, phone_reply)
            )
            assert tts_inputs == ["请说", final_reply] and len(player.played) == 2
    finally:
        await app.state.close()
