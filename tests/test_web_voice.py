from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from muli.attachments import detected_media
from muli.auth import hash_password
from muli.config import ConfigFile, Settings
from muli.local_services import LocalServices
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.runtime import Runtime
from muli.server import create_app
from muli.web_voice import WebVoiceError, WebVoiceInput

WEBM = b"\x1a\x45\xdf\xa3offline browser recording"


@dataclass
class VoiceHarness:
    app: FastAPI
    runtime: Runtime
    local: LocalServices
    voice: WebVoiceInput

    async def audio(self, data: bytes = WEBM) -> dict[str, Any]:
        async def chunks() -> AsyncIterator[bytes]:
            yield data

        return await self.local.attachments.save(
            chunks(), "语音草稿.webm", "audio/webm;codecs=opus"
        )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[VoiceHarness]:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(tmp_path)],
            "updates": {"check_on_start": False},
            "web_voice": {"enabled": True},
            "media": {"asr": {"model": "fixture-asr", "base_url": "https://asr.invalid/v1"}},
        }
    )
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    local, voice = app.state.local_services, app.state.web_voice
    try:
        yield VoiceHarness(app, runtime, local, voice)
    finally:
        await voice.aclose()
        await runtime.close()


def mock_media(
    monkeypatch: pytest.MonkeyPatch, local: LocalServices, client: httpx.AsyncClient
) -> None:
    def factory() -> MediaService:
        return MediaService(
            MediaConfig(asr=ServiceConfig(model="fixture-asr", base_url="https://asr.invalid/v1")),
            client=client,
        )

    monkeypatch.setattr(local, "media", factory)


def defer_transcription_timeout(
    monkeypatch: pytest.MonkeyPatch, expected_delay: float
) -> asyncio.Timeout:
    original_timeout = asyncio.timeout
    timer = original_timeout(None)

    def capture(delay: float | None) -> asyncio.Timeout:
        # Defer only the next production timeout; media timeouts stay unchanged.
        monkeypatch.setattr(asyncio, "timeout", original_timeout)
        assert delay == expected_delay
        return timer

    monkeypatch.setattr(asyncio, "timeout", capture)
    return timer


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "true"},
        {"max_seconds": 0},
        {"max_seconds": 301},
        {"max_seconds": True},
        {"max_file_bytes": 1023},
        {"max_file_bytes": 33 * 1024 * 1024},
        {"max_concurrent": 5},
        {"max_concurrent": "1"},
        {"timeout": float("inf")},
        {"timeout": 0},
        {"max_text_chars": 64001},
        {"unknown": True},
    ],
)
def test_web_voice_policy_is_strict_and_bounded(patch: dict[str, Any]) -> None:
    assert not Settings().web_voice.enabled
    with pytest.raises(ValidationError):
        Settings.model_validate({"web_voice": patch})


@pytest.mark.parametrize(
    ("header", "claimed", "expected"),
    [
        (WEBM, "audio/webm;codecs=opus", "audio/webm"),
        (WEBM, "video/webm;codecs=vp9", "video/webm"),
        (b"\x00\x00\x00\x18ftypM4A ", "audio/mp4;codecs=mp4a.40.2", "audio/mp4"),
        (b"OggSfixture", "audio/ogg;codecs=opus", "audio/ogg"),
        (b"<script>fixture</script>", "audio/webm", "application/octet-stream"),
        (WEBM, "text/html", "application/octet-stream"),
    ],
)
def test_browser_audio_detection_preserves_passive_media(
    header: bytes, claimed: str, expected: str
) -> None:
    assert detected_media(header, claimed) == expected


async def test_transcription_only_returns_draft_and_keeps_uploaded_audio(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        assert WEBM in body and b"fixture-asr" in body
        assert b"audio/webm" in body
        return httpx.Response(200, json={"text": "这是一份待确认的草稿。"})

    item = await harness.audio()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        mock_media(monkeypatch, harness.local, client)
        result = await harness.voice.transcribe(item["id"])
    assert result == {"text": "这是一份待确认的草稿。", "attachment_id": item["id"], "draft": True}
    assert str(requests[0].url) == "https://asr.invalid/v1/audio/transcriptions"
    assert harness.runtime.store.list("message") == []
    assert harness.runtime.store.db.execute("SELECT COUNT(*) FROM ingress").fetchone()[0] == 0
    assert harness.local.attachments.path(item["id"]).read_bytes() == WEBM
    assert item["mime_type"] == "audio/webm" and item["preview_url"]
    assert harness.voice.status()["active"] == 0


async def test_admission_rejects_disabled_unconfigured_non_audio_and_oversize(
    harness: VoiceHarness,
) -> None:
    item = await harness.audio()
    config = harness.runtime.config
    policies: list[tuple[dict[str, Any], int]] = [
        ({"web_voice": {"enabled": False}}, 403),
        ({"web_voice": {"enabled": True}, "media": {"asr": {"model": ""}}}, 409),
    ]
    for patch, code in policies:
        config.update(patch)
        with pytest.raises(WebVoiceError) as caught:
            await harness.voice.transcribe(item["id"])
        assert caught.value.status_code == code
    config.update({"media": {"asr": {"model": "fixture-asr"}}})
    raw = harness.runtime.store.get("attachment", item["id"])
    assert raw is not None
    for values, code in [
        ({"mime_type": "video/webm", "size": len(WEBM)}, 415),
        ({"mime_type": "audio/webm", "size": 0}, 413),
        ({"mime_type": "audio/webm", "size": 9000000}, 413),
    ]:
        harness.runtime.store.put("attachment", {**raw, **values})
        with pytest.raises(WebVoiceError) as caught:
            await harness.voice.transcribe(item["id"])
        assert caught.value.status_code == code
    assert harness.voice.status()["active"] == 0


@pytest.mark.parametrize(("text", "code"), [("  \n", 422), ("一二三四", 413)])
async def test_empty_or_overlong_text_never_becomes_a_partial_draft(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch, text: str, code: int
) -> None:
    item = await harness.audio()
    harness.runtime.config.update({"web_voice": {"max_text_chars": 3}})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"text": text}))
    ) as client:
        mock_media(monkeypatch, harness.local, client)
        with pytest.raises(WebVoiceError) as caught:
            await harness.voice.transcribe(item["id"])
    assert caught.value.status_code == code
    assert harness.runtime.store.list("message") == []


async def test_concurrency_is_immediate_and_cancellation_frees_capacity(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    item = await harness.audio()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        mock_media(monkeypatch, harness.local, client)
        task = asyncio.create_task(harness.voice.transcribe(item["id"]))
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(WebVoiceError) as caught:
            await asyncio.wait_for(harness.voice.transcribe(item["id"]), 2)
        assert caught.value.status_code == 429
        # A blocked remote ASR does not own the configuration lock or event loop.
        async with asyncio.timeout(2):
            async with harness.runtime.configuration_lock:
                assert harness.voice.status()["active"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert harness.voice.status()["active"] == 0


async def test_timeout_cancels_remote_operation(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()
    requests = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests > 1:
            return httpx.Response(200, json={"text": "超时后可重新转写"})
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    item = await harness.audio()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        mock_media(monkeypatch, harness.local, client)
        timer = defer_transcription_timeout(
            monkeypatch, harness.runtime.config.settings.web_voice.timeout
        )
        task = asyncio.create_task(harness.voice.transcribe(item["id"]))
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert not task.done() and not timer.expired()
            timer.reschedule(asyncio.get_running_loop().time())
            # The watchdog must not supply the TimeoutError asserted below.
            done, _ = await asyncio.wait({task}, timeout=2)
            assert task in done, "转写未在超时后结束"
            with pytest.raises(TimeoutError):
                task.result()
            assert timer.expired() and cancelled.is_set() and requests == 1
            assert harness.voice.status()["active"] == 0
            result = await asyncio.wait_for(harness.voice.transcribe(item["id"]), 2)
            assert result["text"] == "超时后可重新转写" and requests == 2
            assert harness.voice.status()["active"] == 0
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_timeout_during_audio_read_never_dispatches_remote_operation(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    reading, cancelled = asyncio.Event(), asyncio.Event()
    requests: list[httpx.Request] = []
    original_read = harness.local.read_bytes

    async def read(identifier: str, max_bytes: int) -> tuple[dict[str, Any], bytes]:
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "读取超时后可重新转写"})

    item = await harness.audio()
    monkeypatch.setattr(harness.local, "read_bytes", read)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        mock_media(monkeypatch, harness.local, client)
        timer = defer_transcription_timeout(
            monkeypatch, harness.runtime.config.settings.web_voice.timeout
        )
        task = asyncio.create_task(harness.voice.transcribe(item["id"]))
        try:
            await asyncio.wait_for(reading.wait(), 2)
            assert not task.done() and not timer.expired()
            timer.reschedule(asyncio.get_running_loop().time())
            done, _ = await asyncio.wait({task}, timeout=2)
            assert task in done, "音频读取未在超时后结束"
            with pytest.raises(TimeoutError):
                task.result()
            assert timer.expired() and cancelled.is_set() and not requests
            assert harness.voice.status()["active"] == 0
            monkeypatch.setattr(harness.local, "read_bytes", original_read)
            result = await asyncio.wait_for(harness.voice.transcribe(item["id"]), 2)
            assert result["text"] == "读取超时后可重新转写" and len(requests) == 1
            assert harness.voice.status()["active"] == 0
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_reconfigure_cancels_then_allows_new_operation(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
        raise AssertionError("unreachable")

    item = await harness.audio()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        mock_media(monkeypatch, harness.local, client)
        task = asyncio.create_task(harness.voice.transcribe(item["id"]))
        await asyncio.wait_for(started.wait(), 2)
        closing = asyncio.create_task(harness.voice.aclose())
        await asyncio.wait_for(cleanup_started.wait(), 2)
        closing.cancel()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(WebVoiceError) as caught:
            await task
        assert caught.value.status_code == 409
        assert harness.voice.status()["active"] == 0
        assert harness.voice.status()["available"] is False
        await harness.voice.reconfigure()
        assert harness.voice.status()["available"] is True


async def test_api_authentication_draft_contract_and_schema(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = harness.app
    item = await harness.audio()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"text": "待发送"}))
    ) as asr:
        mock_media(monkeypatch, app.state.local_services, asr)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            path, body = "/api/media/transcribe", {"attachment_id": item["id"]}
            assert (
                await client.post(path, json=body, headers={"Origin": "https://other.invalid"})
            ).status_code == 403
            assert (await client.post(path, json={"attachment_id": "../secret"})).status_code == 422
            result = await asyncio.wait_for(client.post(path, json=body), 2)
            assert result.status_code == 200 and result.json()["draft"] is True
            assert harness.runtime.store.list("message") == []
            assert app.state.web_voice.status()["active"] == 0
            assert (
                "/api/media/transcribe" in (await client.get("/api/openapi.json")).json()["paths"]
            )
            harness.runtime.config.update(
                {"server": {"password_hash": hash_password("fixture-password")}}
            )
            assert (await client.post(path, json=body)).status_code == 401
            assert (
                await client.post("/api/auth/login", json={"password": "fixture-password"})
            ).status_code == 200
            assert (await client.post(path, json=body)).status_code == 200
    await app.state.web_voice.aclose()


async def test_asgi_disconnect_cancels_asr_without_submitting_owner_input(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = harness.app
    item = await harness.audio()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    body = json.dumps({"attachment_id": item["id"]}).encode()
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})
    output: list[dict[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        output.append(dict(message))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/media/transcribe",
        "raw_path": b"/api/media/transcribe",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 3210),
        "server": ("127.0.0.1", 8765),
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        mock_media(monkeypatch, app.state.local_services, client)
        request = asyncio.create_task(app(scope, incoming.get, send))
        await asyncio.wait_for(started.wait(), 2)
        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(request, 2)
    assert cancelled.is_set()
    assert app.state.web_voice.status()["active"] == 0
    assert harness.runtime.store.list("message") == []
    await app.state.web_voice.aclose()


async def test_live_settings_disable_cancels_inflight_asr_and_releases_slot(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = await harness.audio()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    app = harness.app
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as asr,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765"
        ) as client,
    ):
        mock_media(monkeypatch, harness.local, asr)
        task = asyncio.create_task(
            client.post("/api/media/transcribe", json={"attachment_id": item["id"]})
        )
        await asyncio.wait_for(started.wait(), 2)
        response = await asyncio.wait_for(
            client.patch("/api/settings", json={"web_voice": {"enabled": False}}), 2
        )
        assert response.status_code == 200
        assert (await task).status_code == 409
        assert cancelled.is_set()
        status = (await client.get("/api/status")).json()["web_voice"]
        assert not status["available"] and status["active"] == 0
        assert harness.runtime.store.list("message") == []
        assert (await client.get("/api/settings/schema")).json()["$defs"]["WebVoiceSettings"][
            "properties"
        ]["enabled"]["default"] is False


async def test_only_separate_explicit_send_submits_the_draft(
    harness: VoiceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []

    async def submit(content: str, mode: str | None = None, **kwargs: Any) -> dict[str, Any]:
        submitted.append(content)
        assert kwargs["attachment_ids"] == []
        return {"queued": True}

    monkeypatch.setattr(harness.runtime, "submit", submit)
    item = await harness.audio()
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"text": "草稿"})
            )
        ) as asr,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(harness.app), base_url="http://127.0.0.1:8765"
        ) as client,
    ):
        mock_media(monkeypatch, harness.local, asr)
        response = await client.post("/api/media/transcribe", json={"attachment_id": item["id"]})
        assert response.status_code == 200 and not submitted
        session_id = harness.runtime.main_session()["id"]
        sent = await client.post(
            f"/api/sessions/{session_id}/messages", json={"content": "用户编辑过的草稿"}
        )
        assert sent.status_code == 200
        assert submitted == ["用户编辑过的草稿"]
