"""Saved complete answers reach configured TTS only after an authenticated HTTP action."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call
from test_voice import FakeDetector, FakePlayer, FakeRecorder

import muli.attachments as attachments_module
import muli.voice as voice_module
from muli.auth import COOKIE_NAME, hash_password
from muli.config import VoiceSettings
from muli.media import SpeechResult
from muli.platforms.files import LocalFiles
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent


async def test_saved_complete_main_answer_synthesizes_full_audio_after_owner_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = "先核查提醒，再给我一份完整答复，我稍后自己播放。"
    intermediate = "INTERMEDIATE-ONLY：我先核查提醒。"
    reasoning = "REASONING-ONLY：核对工具结果后再给出完整回答。"
    final_reply = (
        "当前没有提醒。\n\n"
        + "这段完整答复需要保留原样，不能只生成网页显示的某个片段。\n" * 36
        + "FINAL-TAIL-ONLY：以上就是本次完整答复。"
    )
    reply_audio = b"ID3-OFFLINE-SAVED-ANSWER\x00" + bytes(range(256)) * 8 + b"AUDIO-TAIL"
    password, media_key = "fixture-owner-password", "fixture-saved-answer-key"
    recorder, wake, speaker, player = (
        FakeRecorder(),
        FakeDetector(),
        FakeDetector(),
        FakePlayer(True),
    )
    tts_inputs: list[str] = []
    seen: list[list[Message]] = []

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

    async def media_http(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://tts.invalid/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer " + media_key
        body = json.loads(request.content)
        assert body["model"] == "synthetic-saved-answer-tts"
        assert body["voice"] == "offline-test-voice"
        assert body["response_format"] == "mp3"
        tts_inputs.append(body["input"])
        assert body["input"] == final_reply
        return httpx.Response(200, content=reply_audio, headers={"Content-Type": "audio/mpeg"})

    transport = httpx.MockTransport(media_http)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    # Keep the real LocalServices.media factory, profile/key resolution and
    # MediaService request/response handling; only its HTTP transport is offline.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            if len(seen) == 1:
                assert messages[-1].role == "user" and messages[-1].content == question
                assert "reminder_list" in {item["function"]["name"] for item in tools}
                turn = ModelTurn(
                    content=intermediate,
                    reasoning=reasoning,
                    tool_calls=[call("saved-answer-reminders", "reminder_list")],
                )
            else:
                assert len(seen) == 2
                tool_result = next(
                    item for item in messages if item.tool_call_id == "saved-answer-reminders"
                )
                assert tool_result.role == "tool" and json.loads(tool_result.content) == []
                turn = ModelTurn(content=final_reply, reasoning=reasoning)
            yield StreamEvent("reasoning_delta", text=reasoning)
            midpoint = len(turn.content) // 2
            yield StreamEvent("text_delta", text=turn.content[:midpoint])
            yield StreamEvent("text_delta", text=turn.content[midpoint:])
            yield StreamEvent("done", turn=turn)

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model == "main"
        return Main([], [])

    async with actual_app(tmp_path) as rig:
        runtime = rig.runtime
        runtime.client_factory = factory  # type: ignore[assignment]
        runtime.config.update({"server": {"password_hash": hash_password(password)}})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            assert (await web.get("/api/settings")).status_code == 401
            login = await web.post("/api/auth/login", json={"password": password})
            assert login.status_code == 200 and COOKIE_NAME in web.cookies
            configured = await web.patch(
                "/api/settings",
                json={
                    "memory": {"auto_extract": False},
                    "voice": {"enabled": False, "tts_enabled": False, "reply_enabled": False},
                    "web_voice": {"enabled": False},
                    "media": {
                        "tts": {
                            "model": "synthetic-saved-answer-tts",
                            "base_url": "https://tts.invalid/v1",
                            "api_key": media_key,
                            "voice": "offline-test-voice",
                            "response_format": "mp3",
                        }
                    },
                },
            )
            assert configured.status_code == 200
            voice_before = runtime.config.settings.voice.model_dump()
            web_voice_before = runtime.config.settings.web_voice.model_dump()
            assert not any(voice_before[key] for key in ("enabled", "tts_enabled", "reply_enabled"))
            assert web_voice_before["enabled"] is False
            session_id = runtime.main_session()["id"]
            submitted = await web.post(
                f"/api/sessions/{session_id}/messages", json={"content": question, "mode": "yolo"}
            )
            assert submitted.status_code == 200
            run_id = submitted.json()["run_id"]
            assert isinstance(run_id, str) and run_id
            assert await asyncio.wait_for(runtime.wait(run_id), 4) == final_reply
            run_before = runtime.store.get("run", run_id)
            assert run_before is not None and run_before["status"] == "completed"
            assert run_before["result"] == final_reply
            message_id = run_before["result_message_id"]
            message_before = runtime.store.get("message", message_id)
            assert message_before is not None and message_before["role"] == "assistant"
            assert message_before["content"] == final_reply
            assert message_before["reasoning"] == reasoning
            assert not message_before.get("tool_calls") and not message_before.get("interrupted")
            assert message_before["reply_origin"] == run_before["reply_origin"]
            assert message_before["reply_origin"]["input_id"] == submitted.json()["message"]["id"]
            # Ordinary Runtime finals have no accepted/tool-output provenance.
            assert "content_provenance" not in message_before
            revision = message_before["evidence_revision"]
            assert isinstance(revision, str) and 0 < len(revision) <= 200
            history = await web.get(f"/api/sessions/{session_id}")
            assert history.status_code == 200
            saved = next(item for item in history.json()["messages"] if item["id"] == message_id)
            assert saved["content"] == final_reply and saved["evidence_revision"] == revision
            messages_before = runtime.store.messages(session_id)
            assert len(seen) == 2
            assert any(
                item.get("content") == intermediate and item.get("tool_calls")
                for item in messages_before
            )
            assert any(
                item.get("tool_call_id") == "saved-answer-reminders" for item in messages_before
            )
            assert tts_inputs == [] and recorder.calls == [] and player.played == []
            assert rig.attachments.list() == []

            # All existing real-runtime/authentication prerequisites are complete.
            # The frozen baseline must fail here at the missing product HTTP entry.
            result = await web.post(
                f"/api/sessions/{session_id}/messages/{message_id}/speech",
                json={"run_id": run_id, "revision": revision, "confirmed": True},
            )
            assert result.status_code == 200, (
                "已持久完成的真实主答复缺少确认后生成语音的 HTTP 入口："
                f"HTTP {result.status_code} {result.text}"
            )
            receipt = result.json()
            assert set(receipt) == {"session_id", "run_id", "message_id", "revision", "attachment"}
            identity = {
                "session_id": session_id,
                "run_id": run_id,
                "message_id": message_id,
                "revision": revision,
            }
            assert {key: receipt[key] for key in identity} == identity
            attachment = receipt["attachment"]
            assert attachment["source"] == "tts" and attachment["mime_type"] == "audio/mpeg"
            assert attachment["kind"] == "audio" and attachment["size"] == len(reply_audio)
            assert attachment["preview_url"] and attachment["url"]
            downloaded = await web.get(attachment["url"])
            assert downloaded.status_code == 200 and downloaded.content == reply_audio
            assert tts_inputs == [final_reply]
            assert all(
                marker not in tts_inputs[0]
                for marker in ("REASONING-ONLY", "INTERMEDIATE-ONLY", "saved-answer-reminders")
            )
            assert media_key not in result.text and final_reply not in result.text
            assert "path" not in attachment
            assert runtime.store.get("run", run_id) == run_before
            assert runtime.store.get("message", message_id) == message_before
            assert runtime.store.messages(session_id) == messages_before
            assert runtime.config.settings.voice.model_dump() == voice_before
            assert runtime.config.settings.web_voice.model_dump() == web_voice_before
            assert recorder.calls == [] and player.played == [] and len(seen) == 2
            assert len(rig.attachments.list()) == 1


def offline_tts(
    monkeypatch: pytest.MonkeyPatch,
    handle: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> None:
    transport = httpx.MockTransport(handle)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        assert str(request.url) == "https://tts.invalid/v1/audio/speech"
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)


async def configure_speech(rig: MemoryRig, web: httpx.AsyncClient) -> None:
    password = "fixture-saved-answer-owner"
    rig.runtime.config.update({"server": {"password_hash": hash_password(password)}})
    assert (await web.post("/api/auth/login", json={"password": password})).status_code == 200
    response = await web.patch(
        "/api/settings",
        json={
            "memory": {"auto_extract": False},
            "voice": {"enabled": False, "tts_enabled": False, "reply_enabled": False},
            "web_voice": {"enabled": False},
            "media": {
                "tts": {
                    "model": "synthetic-saved-answer-tts",
                    "base_url": "https://tts.invalid/v1",
                    "voice": "offline-test-voice",
                }
            },
        },
    )
    assert response.status_code == 200


async def complete_answer(
    rig: MemoryRig, web: httpx.AsyncClient, answer: str = "这是一条完整的已保存最终答复。"
) -> tuple[dict[str, Any], dict[str, Any]]:
    turns = [
        ModelTurn(
            content="先检查提醒，再回答。",
            reasoning="只在核对之后回答。",
            tool_calls=[call("saved-answer-policy-reminders", "reminder_list")],
        ),
        ModelTurn(content=answer),
    ]
    seen: list[list[Message]] = []

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model == "main"
        return FakeClient(turns, seen)

    rig.runtime.client_factory = factory  # type: ignore[assignment]
    session_id = rig.runtime.main_session()["id"]
    response = await web.post(
        f"/api/sessions/{session_id}/messages", json={"content": "核对提醒后回答", "mode": "yolo"}
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert await asyncio.wait_for(rig.runtime.wait(run_id), 4) == answer
    run = rig.runtime.store.get("run", run_id)
    assert run is not None and run["status"] == "completed" and len(seen) == 2
    message = rig.runtime.store.get("message", run["result_message_id"])
    assert message is not None and message["content"] == answer
    return run, message


def speech_request(run: dict[str, Any], message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        f"/api/sessions/{run['session_id']}/messages/{message['id']}/speech",
        {"run_id": run["id"], "revision": message["evidence_revision"], "confirmed": True},
    )


async def test_saved_speech_rejects_inexact_sources_and_strict_unconfirmed_input_before_tts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=b"ID3-UNEXPECTED", headers={"Content-Type": "audio/mpeg"}
        )

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            run, final = await complete_answer(rig, web)
            path, body = speech_request(run, final)
            for changes, status in (
                ({"confirmed": False}, 400),
                ({"confirmed": 1}, 422),
                ({"confirmed": "true"}, 422),
                ({"text": "浏览器传来的节选不能作为来源"}, 422),
                ({"base_url": "https://other.invalid"}, 422),
                ({"run_id": 1}, 422),
                ({"run_id": "missing-run"}, 409),
                ({"revision": "previous-version"}, 409),
            ):
                response = await web.post(path, json={**body, **changes})
                assert response.status_code == status, response.text
                assert requests == []
            intermediate = next(
                item
                for item in rig.runtime.store.messages(run["session_id"])
                if item["role"] == "assistant" and item.get("tool_calls")
            )
            intermediate_path, intermediate_body = speech_request(run, intermediate)
            assert (await web.post(intermediate_path, json=intermediate_body)).status_code == 409
            other = rig.runtime.new_session()
            assert (
                await web.post(path.replace(run["session_id"], other["id"]), json=body)
            ).status_code == 409

            for damage in ("excerpt", "blank", "content_limit", "proof_budget"):
                changed_run, changed = dict(run), dict(final)
                if damage == "excerpt":
                    changed["content_provenance"] = "runtime_excerpt"
                elif damage == "blank":
                    changed["content"] = changed_run["result"] = " \n\t"
                elif damage == "content_limit":
                    changed["content"] = changed_run["result"] = "字" * (
                        rig.runtime.config.settings.limits.max_output_chars + 1
                    )
                else:
                    changed["reasoning"] = "X" * (
                        rig.runtime.config.settings.limits.max_context_chars * 4
                    )
                rig.runtime.store.put("run", changed_run)
                rig.runtime.store.put("message", changed)
                damaged_path, damaged_body = speech_request(changed_run, changed)
                response = await web.post(damaged_path, json=damaged_body)
                expected = 413 if damage in {"content_limit", "proof_budget"} else 409
                assert response.status_code == expected
                assert requests == []
            rig.runtime.store.put("run", run)
            rig.runtime.store.put("message", final)
            path, body = speech_request(run, final)
            disabled = await web.patch(
                "/api/settings", json={"disabled_tools": ["speech_synthesize"]}
            )
            assert disabled.status_code == 200
            assert (await web.post(path, json=body)).status_code == 403
            enabled = await web.patch(
                "/api/settings", json={"disabled_tools": [], "media": {"tts": {"voice": ""}}}
            )
            assert enabled.status_code == 200
            assert (await web.post(path, json=body)).status_code == 409
            assert requests == [] and rig.attachments.list() == []


async def test_same_version_is_single_flight_and_total_admission_has_no_waiting_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    inputs: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        inputs.append(json.loads(request.content)["input"])
        entered.set()
        await release.wait()
        return httpx.Response(200, content=b"ID3-SINGLE", headers={"Content-Type": "audio/mpeg"})

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            first_run, first = await complete_answer(rig, web, "第一条完整答复。")
            second_run, second = await complete_answer(rig, web, "第二条完整答复。")
            assert (
                await web.patch("/api/settings", json={"uploads": {"max_concurrent": 1}})
            ).status_code == 200
            path, body = speech_request(first_run, first)
            pending = asyncio.create_task(web.post(path, json=body))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                duplicate = await asyncio.wait_for(web.post(path, json=body), 3)
                assert duplicate.status_code == 409 and "正在生成" in duplicate.text
                other_path, other_body = speech_request(second_run, second)
                full = await asyncio.wait_for(web.post(other_path, json=other_body), 3)
                assert full.status_code == 429
                assert not pending.done() and inputs == [first["content"]]
                release.set()
                assert (await asyncio.wait_for(pending, 3)).status_code == 200
                assert rig.app.state.saved_answer_voice.idle
                assert inputs == [first["content"]] and len(rig.attachments.list()) == 1
                assert rig.runtime.store.get("run", first_run["id"]) == first_run
                assert rig.runtime.store.get("message", first["id"]) == first
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_same_settings_instance_voice_change_rejects_old_tts_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[dict[str, Any]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["voice"] == "offline-test-voice"
        entered.set()
        await release.wait()
        return httpx.Response(
            200, content=b"ID3-OLD-POLICY-AUDIO", headers={"Content-Type": "audio/mpeg"}
        )

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            run, message = await complete_answer(rig, web)
            settings = rig.runtime.config.settings
            component = rig.app.state.saved_answer_voice
            epoch = component._epoch
            path, body = speech_request(run, message)
            pending = asyncio.create_task(web.post(path, json=body))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                # Both values are valid policy values. Change only the live profile;
                # no Settings replacement, reconfigure callback or epoch change occurs.
                settings.media.tts.voice = "another-valid-voice"
                assert rig.runtime.config.settings is settings and component._epoch == epoch
                release.set()
                response = await asyncio.wait_for(pending, 3)
                assert response.status_code == 409, (
                    "同一 Settings 实例的真实音色策略已变化，旧 TTS 结果仍被保存或发布："
                    f"HTTP {response.status_code} {response.text}"
                )
                assert "可能已计费" in response.text
                assert len(requests) == 1 and requests[0]["input"] == message["content"]
                assert rig.attachments.list() == [] and component.idle
                assert rig.runtime.store.get("run", run["id"]) == run
                assert rig.runtime.store.get("message", message["id"]) == message
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_legacy_saved_answer_uses_http_evidence_revision_instead_of_payload_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        inputs.append(json.loads(request.content)["input"])
        return httpx.Response(200, content=b"ID3-LEGACY", headers={"Content-Type": "audio/mpeg"})

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            run, message = await complete_answer(rig, web)
            stored_seq = message["seq"]
            forged_seq = stored_seq + 1000
            # Reproduce a historical row without a revision, with an unreliable
            # payload seq. HistoryEvidence's message_order mapping remains real.
            with rig.runtime.store.db:
                rig.runtime.store.db.execute(
                    "UPDATE objects SET data=json_remove(json_set(data,'$.seq',?),"
                    "'$.evidence_revision') WHERE kind='message' AND id=?",
                    (forged_seq, message["id"]),
                )
            legacy = rig.runtime.store.get("message", message["id"])
            assert legacy is not None and "evidence_revision" not in legacy
            assert legacy["seq"] == forged_seq
            content = await web.get(
                f"/api/sessions/{run['session_id']}/messages/{message['id']}/content"
            )
            assert content.status_code == 200 and content.json()["content"] == message["content"]
            revision = content.json()["revision"]
            assert revision == f"legacy:{rig.runtime.store.instance_id}:{stored_seq}"
            assert content.json()["seq"] == stored_seq
            path, body = speech_request(run, message)
            wrong = await web.post(
                path,
                json={**body, "revision": f"legacy:{rig.runtime.store.instance_id}:{forged_seq}"},
            )
            assert wrong.status_code == 409 and inputs == []
            result = await web.post(path, json={**body, "revision": revision})
            assert result.status_code == 200 and result.json()["revision"] == revision
            audio = await web.get(result.json()["attachment"]["url"])
            assert audio.status_code == 200 and audio.content == b"ID3-LEGACY"
            assert inputs == [message["content"]]
            assert rig.runtime.store.get("message", message["id"]) == legacy
            assert rig.runtime.store.get("run", run["id"]) == run


@pytest.mark.parametrize("action", ["settings_aba", "application_close", "caller_cancel"])
async def test_inflight_saved_speech_lifecycle_settles_its_owner_without_replaying_tts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    release, cleanup_release = asyncio.Event(), asyncio.Event()
    inputs: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        inputs.append(json.loads(request.content)["input"])
        if len(inputs) == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                if action == "settings_aba":
                    await cleanup_release.wait()
                raise
        return httpx.Response(
            200, content=b"ID3-NEW-INTENT", headers={"Content-Type": "audio/mpeg"}
        )

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            run, message = await complete_answer(rig, web)
            path, body = speech_request(run, message)
            pending = asyncio.create_task(web.post(path, json=body))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                if action == "settings_aba":
                    changed = await asyncio.wait_for(
                        web.patch(
                            "/api/settings", json={"media": {"tts": {"voice": "other-voice"}}}
                        ),
                        3,
                    )
                    assert changed.status_code == 200
                    await asyncio.wait_for(cancelled.wait(), 3)
                    restored = await asyncio.wait_for(
                        web.patch(
                            "/api/settings",
                            json={"media": {"tts": {"voice": "offline-test-voice"}}},
                        ),
                        3,
                    )
                    assert restored.status_code == 200
                    # Reconfiguration did not join under its lock, and old cleanup
                    # still owns its slot even after settings returned A -> B -> A.
                    assert not pending.done() and not rig.app.state.saved_answer_voice.idle
                    duplicate = await web.post(path, json=body)
                    assert duplicate.status_code == 409 and inputs == [message["content"]]
                    cleanup_release.set()
                    response = await asyncio.wait_for(pending, 3)
                    assert response.status_code == 409 and "可能已计费" in response.text
                elif action == "application_close":
                    await asyncio.wait_for(rig.app.state.close(), 3)
                    assert (await asyncio.wait_for(pending, 3)).status_code == 409
                else:
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                await asyncio.wait_for(cancelled.wait(), 3)
                assert rig.app.state.saved_answer_voice.idle
                assert inputs == [message["content"]]
                assert list(rig.attachments.directory.iterdir()) == []
                if action != "application_close":
                    assert rig.runtime.store.get("run", run["id"]) == run
                    assert rig.runtime.store.get("message", message["id"]) == message
                if action == "settings_aba":
                    # A separate authenticated and confirmed user action may retry;
                    # no reconnect or lifecycle callback submitted this second call.
                    retry = await web.post(path, json=body)
                    assert retry.status_code == 200
                    assert inputs == [message["content"], message["content"]]
            finally:
                release.set()
                cleanup_release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_asgi_disconnect_cancels_saved_speech_and_collects_storage_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            run, message = await complete_answer(rig, web)
            path, payload = speech_request(run, message)
            body = json.dumps(payload).encode()
            incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})
            output: list[dict[str, Any]] = []

            async def send(value: MutableMapping[str, Any]) -> None:
                output.append(dict(value))

            scope: dict[str, Any] = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "client": ("127.0.0.1", 3210),
                "server": ("127.0.0.1", 8765),
                "headers": [
                    (b"host", b"127.0.0.1:8765"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cookie", f"{COOKIE_NAME}={web.cookies[COOKIE_NAME]}".encode()),
                ],
            }
            pending = asyncio.create_task(rig.app(scope, incoming.get, send))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                incoming.put_nowait({"type": "http.disconnect"})
                await asyncio.wait_for(pending, 3)
                assert cancelled.is_set() and calls == 1
                assert rig.app.state.saved_answer_voice.idle and rig.attachments.list() == []
                assert rig.runtime.store.get("run", run["id"]) == run
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("boundary", ["capacity", "write", "quota"])
async def test_source_revision_change_during_attachment_wait_cannot_commit_old_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    inputs: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        inputs.append(json.loads(request.content)["input"])
        return httpx.Response(
            200, content=b"ID3-OLD-ANSWER" + b"x" * 2048, headers={"Content-Type": "audio/mpeg"}
        )

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            run, message = await complete_answer(rig, web)
            path, body = speech_request(run, message)
            save = rig.app.state.local_services.save_speech

            async def waiting_save(
                result: SpeechResult, *, commit_guard: Callable[[], None] | None = None
            ) -> dict[str, Any]:
                if boundary == "capacity":
                    entered.set()
                return await save(result, commit_guard=commit_guard)

            monkeypatch.setattr(rig.app.state.local_services, "save_speech", waiting_save)
            held = 0
            if boundary == "capacity":
                for _ in range(rig.runtime.config.settings.uploads.max_concurrent):
                    await rig.attachments.capacity.acquire()
                    held += 1
            else:
                file_io = attachments_module.file_io

                async def waiting_file_io[T](operation: Callable[[], T]) -> T:
                    if not entered.is_set():
                        entered.set()
                        await release.wait()
                    return await file_io(operation)

                monkeypatch.setattr(attachments_module, "file_io", waiting_file_io)
            pending = asyncio.create_task(web.post(path, json=body))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert inputs == [message["content"]] and not pending.done()
                expected_message = message
                if boundary == "quota":
                    settings = rig.runtime.config.settings
                    epoch = rig.app.state.saved_answer_voice._epoch
                    # The old size check has passed before the real write await.
                    settings.uploads.max_total_bytes = 1024
                    assert rig.runtime.config.settings is settings
                    assert rig.app.state.saved_answer_voice._epoch == epoch
                else:
                    expected_message = {**message, "content": "主人已经改成另一个版本的答复。"}
                    rig.runtime.store.put("message", expected_message)
                while held:
                    rig.attachments.capacity.release()
                    held -= 1
                release.set()
                response = await asyncio.wait_for(pending, 3)
                assert response.status_code == 409 and "可能已计费" in response.text
                assert rig.app.state.saved_answer_voice.idle and rig.attachments.list() == []
                assert list(rig.attachments.directory.iterdir()) == []
                assert rig.attachments.reserved == 0 and inputs == [message["content"]]
                assert rig.runtime.store.get("run", run["id"]) == run
                assert rig.runtime.store.get("message", message["id"]) == expected_message
            finally:
                release.set()
                while held:
                    rig.attachments.capacity.release()
                    held -= 1
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_completed_tts_with_local_quota_failure_reports_possible_charge_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        inputs.append(json.loads(request.content)["input"])
        return httpx.Response(
            200, content=b"ID3" + b"x" * 2048, headers={"Content-Type": "audio/mpeg"}
        )

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            run, message = await complete_answer(rig, web)
            configured = await web.patch(
                "/api/settings", json={"uploads": {"max_total_bytes": 1024}}
            )
            assert configured.status_code == 200
            path, body = speech_request(run, message)
            response = await web.post(path, json=body)
            assert response.status_code == 409
            assert "TTS 已完成" in response.text and "可能已计费" in response.text
            assert inputs == [message["content"]] and rig.attachments.list() == []
            assert rig.app.state.saved_answer_voice.idle
            assert rig.runtime.store.get("run", run["id"]) == run


async def test_terminal_event_without_persisted_completed_run_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    entered, release = asyncio.Event(), asyncio.Event()
    candidate: dict[str, Any] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=b"ID3-UNEXPECTED", headers={"Content-Type": "audio/mpeg"}
        )

    offline_tts(monkeypatch, handle)
    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)
            emit = rig.runtime.bus.emit

            async def delayed(
                event_type: str, data: dict[str, Any], session_id: str | None = None
            ) -> dict[str, Any]:
                event = await emit(event_type, data, session_id)
                if event_type == "run.completed":
                    candidate.update(data)
                    entered.set()
                    await release.wait()
                return event

            monkeypatch.setattr(rig.runtime.bus, "emit", delayed)
            answer = "终态事件已发出，但运行仍在清理。"
            turns = [ModelTurn(content=answer)]

            def factory(profile: ModelConfig) -> FakeClient:
                return FakeClient(turns, [])

            rig.runtime.client_factory = factory  # type: ignore[assignment]
            session_id = rig.runtime.main_session()["id"]
            submitted = await web.post(
                f"/api/sessions/{session_id}/messages", json={"content": "请回答", "mode": "yolo"}
            )
            assert submitted.status_code == 200
            try:
                await asyncio.wait_for(entered.wait(), 3)
                run = rig.runtime.store.get("run", candidate["run_id"])
                message = rig.runtime.store.get("message", candidate["result_message_id"])
                assert run is not None and run["status"] == "running" and message is not None
                path, body = speech_request(run, message)
                assert (await web.post(path, json=body)).status_code == 409
                assert requests == [] and rig.attachments.list() == []
            finally:
                release.set()
                assert (
                    await asyncio.wait_for(rig.runtime.wait(submitted.json()["run_id"]), 3)
                    == answer
                )


async def test_cancelled_partial_main_answer_is_never_synthesized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=b"ID3-UNEXPECTED", headers={"Content-Type": "audio/mpeg"}
        )

    offline_tts(monkeypatch, handle)

    class Partial(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            yield StreamEvent("text_delta", text="这只是一段被中断的流式草稿。")
            entered.set()
            await asyncio.Event().wait()

    async with actual_app(tmp_path) as rig:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://127.0.0.1:8765"
        ) as web:
            await configure_speech(rig, web)

            def factory(profile: ModelConfig) -> FakeClient:
                return Partial([], [])

            rig.runtime.client_factory = factory  # type: ignore[assignment]
            session_id = rig.runtime.main_session()["id"]
            submitted = await web.post(
                f"/api/sessions/{session_id}/messages", json={"content": "请回答", "mode": "yolo"}
            )
            await asyncio.wait_for(entered.wait(), 3)
            assert (await web.post(f"/api/sessions/{session_id}/stop", json={})).status_code == 200
            run = rig.runtime.store.get("run", submitted.json()["run_id"])
            partial = next(
                item for item in rig.runtime.store.messages(session_id) if item.get("interrupted")
            )
            assert run is not None and run["status"] == "cancelled"
            path, body = speech_request(run, partial)
            assert (await web.post(path, json=body)).status_code == 409
            assert requests == [] and rig.attachments.list() == []
