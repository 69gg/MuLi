"""Saved audio can be retried as a draft before a separate, explicit main-chat send."""

from __future__ import annotations

import asyncio
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest
from test_runtime import FakeClient
from test_voice import write_wav
from test_web_voice import VoiceHarness, mock_media
from test_web_voice import harness as harness

from muli.providers import Message, ModelConfig, ModelTurn


async def test_saved_audio_retry_stays_a_draft_until_explicit_main_send(
    harness: VoiceHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = tmp_path / "saved-recording.wav"
    write_wav(recording, seconds=0.02)
    audio = recording.read_bytes()
    draft, edited, answer = "下周三再核对录音。", "下周四再核对录音。", "已看到编辑后的文字。"
    runtime = harness.runtime
    runtime.config.update(
        {
            "models": {"main": {"model": "main", "base_url": "https://unused.invalid/v1"}},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
        }
    )
    seen: list[list[Message]] = []
    profiles: list[str] = []
    turns = [ModelTurn(content=answer)]

    def factory(profile: ModelConfig) -> FakeClient:
        profiles.append(profile.model)
        assert profile.model == "main"
        return FakeClient(turns, seen)

    async def reject_network(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        raise AssertionError("saved-audio draft journey must remain offline")

    runtime.client_factory = factory  # type: ignore[assignment]
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)
    asr_requests: list[httpx.Request] = []

    async def transcribe(request: httpx.Request) -> httpx.Response:
        asr_requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://asr.invalid/v1/audio/transcriptions"
        multipart = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + await request.aread()
        )
        parts = {
            part.get_param("name", header="content-disposition"): part
            for part in multipart.iter_parts()
        }
        assert parts["model"].get_payload(decode=True) == b"fixture-asr"
        assert parts["file"].get_filename() == recording.name
        assert parts["file"].get_content_type() == "audio/wav"
        assert parts["file"].get_payload(decode=True) == audio
        if len(asr_requests) == 1:
            return httpx.Response(503, json={"error": "offline fixture unavailable"})
        assert len(asr_requests) == 2
        return httpx.Response(200, json={"text": draft})

    async with (
        harness.app.router.lifespan_context(harness.app),
        httpx.AsyncClient(transport=httpx.MockTransport(transcribe)) as asr,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(harness.app), base_url="http://127.0.0.1:8765"
        ) as web,
    ):
        mock_media(monkeypatch, harness.local, asr)
        session_id = runtime.main_session()["id"]
        uploaded = await web.post(
            "/api/uploads",
            content=audio,
            headers={"x-file-name": recording.name, "content-type": "audio/wav"},
        )
        assert uploaded.status_code == 200, uploaded.text
        original = uploaded.json()
        attachment_id = original["id"]
        assert original["kind"] == "audio" and original["size"] == len(audio)
        body = {"attachment_id": attachment_id}

        def assert_draft_only() -> None:
            assert not seen and not profiles
            assert runtime.store.list("message") == []
            assert runtime.store.db.execute("SELECT COUNT(*) FROM ingress").fetchone()[0] == 0
            assert harness.local.attachments.list() == [original]
            assert harness.local.attachments.path(attachment_id).read_bytes() == audio
            assert harness.voice.status()["active"] == 0
            assert runtime.main_session()["id"] == session_id

        failed = await web.post("/api/media/transcribe", json=body)
        assert failed.status_code == 502, failed.text
        assert "503" in failed.json()["detail"] and len(asr_requests) == 1
        assert_draft_only()

        # This second request is the user's explicit retry of the saved attachment.
        retried = await web.post("/api/media/transcribe", json=body)
        assert retried.status_code == 200, retried.text
        assert retried.json() == {"text": draft, "attachment_id": attachment_id, "draft": True}
        assert len(asr_requests) == 2
        assert_draft_only()

        sent = await web.post(f"/api/sessions/{session_id}/messages", json={"content": edited})
        assert sent.status_code == 200, sent.text
        receipt = sent.json()
        assert receipt["session_id"] == session_id and receipt["run_id"] is not None
        assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), 2) == answer
        ingress = runtime.store.db.execute("SELECT * FROM ingress").fetchall()
        assert len(ingress) == 1
        entry = ingress[0]
        assert entry["id"] == receipt["message"]["id"]
        assert entry["session_id"] == session_id and entry["run_id"] == receipt["run_id"]
        payload = json.loads(entry["payload"])
        assert payload["content"] == edited and payload["attachment_ids"] == []
        assert payload["source"] == "web" and payload["principal"] == "owner"
        messages = runtime.store.messages(session_id)
        assert [(item["role"], item["content"]) for item in messages] == [
            ("user", edited),
            ("assistant", answer),
        ]
        assert profiles == ["main"] and len(seen) == 1
        assert seen[0][-1].role == "user" and seen[0][-1].content == edited
        assert len(asr_requests) == 2
        uploads = (await web.get("/api/uploads")).json()
        assert uploads["items"] == [original] and uploads["used_bytes"] == len(audio)
        downloaded = await web.get(original["url"])
        assert downloaded.status_code == 200 and downloaded.content == audio
        assert runtime.main_session()["id"] == session_id
