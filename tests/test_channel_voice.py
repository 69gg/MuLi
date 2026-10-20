from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import Harness, profile, terminal, unused
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelError, ChannelsConfig
from muli.channels import manager as channel_manager
from muli.channels.adapters import QQ
from muli.channels.types import FileContent

MP3 = b"ID3\x04\x00\x00FAKE_AUDIO"
OPUS = b"OggS" + b"\x00" * 24 + b"OpusHead" + b"\x00" * 12


async def audio(
    attachments: Attachments, data: bytes = MP3, name: str = "speech.mp3", mime: str = "audio/mpeg"
) -> str:
    async def chunks() -> AsyncIterator[bytes]:
        yield data

    return str((await attachments.save(chunks(), name, mime, source="tts"))["id"])


@pytest.mark.parametrize("voice_first", [True, False])
async def test_telegram_voice_is_explicit_and_default_audio_remains_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, voice_first: bool
) -> None:
    sends: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        sends.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sends)}})

    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), handler)
    try:
        attachment_id = await audio(harness.attachments)
        timestamp = channel_manager.now()
        identifiers = iter(("a" * 32, "b" * 32) if voice_first else ("b" * 32, "a" * 32))

        def fixed_time() -> float:
            return timestamp

        def next_identifier() -> str:
            return next(identifiers)

        # Coarse clocks can tie; the outbox breaks ties by ID, not enqueue order.
        with monkeypatch.context() as patch:
            patch.setattr(channel_manager, "now", fixed_time)
            patch.setattr(channel_manager, "new_id", next_identifier)
            voice = await harness.manager.enqueue(
                "telegram", "7", "说明", [attachment_id], media_mode="voice"
            )
            file = await harness.manager.enqueue("telegram", "7", attachment_ids=[attachment_id])
        assert voice["payload"]["parts"][-1]["media_mode"] == "voice"
        assert "media_mode" not in file["payload"]["parts"][0]
        await harness.manager.start()
        assert (await terminal(harness.manager, file["id"]))["state"] == "sent"
        assert (await terminal(harness.manager, voice["id"]))["state"] == "sent"
        methods = [request.url.path.rsplit("/", 1)[-1] for request in sends]
        assert sorted(methods) == [
            "sendDocument",
            "sendMessage",
            "sendVoice",
        ]
        assert methods.index("sendMessage") < methods.index("sendVoice")
        voice_request = sends[methods.index("sendVoice")]
        document_request = sends[methods.index("sendDocument")]
        assert b'name="voice"' in voice_request.content and MP3 in voice_request.content
        assert b'name="document"' in document_request.content
        assert b"parse_mode" not in voice_request.content
        capabilities = harness.manager.status()["channels"]
        assert capabilities["telegram"]["voice_supported"] is True
        assert capabilities["qq"]["voice_supported"] is False
    finally:
        await harness.close()


async def test_opus_tts_attachment_uses_ogg_upload_name_without_changing_saved_file(
    tmp_path: Path,
) -> None:
    sends: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        sends.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), handler)
    try:
        identifier = await audio(harness.attachments, OPUS, "tts.opus", "audio/ogg")
        item = await harness.manager.enqueue(
            "telegram", "7", attachment_ids=[identifier], media_mode="voice"
        )
        await harness.manager.start()
        assert (await terminal(harness.manager, item["id"]))["state"] == "sent"
        assert b'filename="tts.ogg"' in sends[0].content
        assert harness.attachments.get(identifier)["name"] == "tts.opus"
        assert harness.attachments.path(identifier).read_bytes() == OPUS
    finally:
        await harness.close()


@pytest.mark.parametrize(
    ("data", "filename", "mime"),
    [
        (b"RIFF0000WAVE0000", "speech.wav", "audio/wav"),
        (b"OggS0000vorbis", "speech.ogg", "audio/ogg"),
        (b"not audio", "speech.mp3", "audio/mpeg"),
    ],
)
async def test_unsupported_telegram_voice_is_rejected_before_any_part_is_queued(
    tmp_path: Path, data: bytes, filename: str, mime: str
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), unused)
    try:
        identifier = await audio(harness.attachments, data, filename, mime)
        with pytest.raises(ChannelError, match="格式|Opus"):
            await harness.manager.enqueue(
                "telegram", "7", "文字也不先发送", [identifier], media_mode="voice"
            )
        assert harness.manager.deliveries() == []
    finally:
        await harness.close()


@pytest.mark.parametrize("channel", ["qq", "weixin"])
async def test_voice_unsupported_is_explicit_and_never_falls_back(
    tmp_path: Path, channel: str
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(**{channel: profile()}), unused)
    try:
        identifier = await audio(harness.attachments)
        with pytest.raises(ChannelError, match="不支持原生语音"):
            await harness.manager.enqueue(
                channel, "7", attachment_ids=[identifier], media_mode="voice"
            )
        assert harness.manager.deliveries() == []
    finally:
        await harness.close()


@pytest.mark.parametrize("count", [0, 2])
async def test_voice_requires_exactly_one_attachment(tmp_path: Path, count: int) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), unused)
    try:
        identifier = await audio(harness.attachments)
        with pytest.raises(ChannelError, match="恰好一个"):
            await harness.manager.enqueue(
                "onebot", "7", "说明", [identifier] * count, media_mode="voice"
            )
        assert harness.manager.deliveries() == []
    finally:
        await harness.close()


@pytest.mark.parametrize("group", [False, True])
async def test_onebot_voice_queries_capability_and_sends_base64_record(
    tmp_path: Path, group: bool
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        result = {"yes": True} if request.url.path == "/can_send_record" else {"message_id": 8}
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": result})

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(allowed_groups=["99"])), handler)
    try:
        identifier = await audio(harness.attachments)
        target = "99" if group else "7"
        route = {"sender_id": "7", "target": target, "kind": "group" if group else "private"}
        item = await harness.manager.enqueue(
            "onebot", target, attachment_ids=[identifier], route=route, media_mode="voice"
        )
        await harness.manager.start()
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "sent" and result["receipts"] == ["8"]
        assert [path for path, _ in calls] == [
            "/can_send_record",
            "/send_group_msg" if group else "/send_private_msg",
        ]
        body = calls[1][1]
        assert body["group_id" if group else "user_id"] == target
        segment = body["message"][0]
        assert segment["type"] == "record"
        assert base64.b64decode(segment["data"]["file"].removeprefix("base64://")) == MP3
    finally:
        await harness.close()


async def test_onebot_without_record_capability_fails_without_sending(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"yes": False}})

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), handler)
    try:
        identifier = await audio(harness.attachments)
        item = await harness.manager.enqueue(
            "onebot", "7", attachment_ids=[identifier], media_mode="voice"
        )
        await harness.manager.start()
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "failed" and "未确认可发送语音" in result["last_error"]
        assert calls == ["/can_send_record"]
    finally:
        await harness.close()


async def test_voice_idempotency_persists_mode_and_conflicts_with_changed_payload(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), unused)
    try:
        identifier = await audio(harness.attachments)
        arguments: dict[str, Any] = {
            "attachment_ids": [identifier],
            "media_mode": "voice",
            "event_id": "logical-send",
        }
        original = await harness.manager.enqueue("telegram", "7", **arguments)
        assert (await harness.manager.enqueue("telegram", "7", **arguments))["id"] == original["id"]
        for changes in [{"media_mode": "file"}, {"text": "changed"}]:
            with pytest.raises(ChannelError) as caught:
                await harness.manager.enqueue("telegram", "7", **{**arguments, **changes})
            assert caught.value.status_code == 409
        assert len(harness.manager.deliveries()) == 1
    finally:
        await harness.close()


async def test_pending_voice_and_legacy_file_resume_with_their_persisted_modes(
    tmp_path: Path,
) -> None:
    config = ChannelsConfig(telegram=profile())
    harness = Harness(tmp_path, config, unused)
    identifier = await audio(harness.attachments)
    voice = await harness.manager.enqueue(
        "telegram", "7", attachment_ids=[identifier], media_mode="voice", event_id="voice"
    )
    file = await harness.manager.enqueue(
        "telegram", "7", attachment_ids=[identifier], event_id="legacy"
    )
    await harness.close()
    sends: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        sends.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sends)}})

    reopened = Harness(tmp_path, config, handler)
    try:
        await reopened.manager.start()
        assert (await terminal(reopened.manager, file["id"]))["state"] == "sent"
        assert (await terminal(reopened.manager, voice["id"]))["state"] == "sent"
        assert sends == ["sendVoice", "sendDocument"]
        same = await reopened.manager.enqueue(
            "telegram", "7", attachment_ids=[identifier], media_mode="voice", event_id="voice"
        )
        assert same["id"] == voice["id"] and same["state"] == "sent"
        assert len(reopened.manager.deliveries()) == 2
    finally:
        await reopened.close()


async def test_restart_never_repeats_a_voice_left_sending(tmp_path: Path) -> None:
    config = ChannelsConfig(telegram=profile())
    harness = Harness(tmp_path, config, unused)
    identifier = await audio(harness.attachments)
    item = await harness.manager.enqueue(
        "telegram", "7", attachment_ids=[identifier], media_mode="voice", event_id="uncertain"
    )
    with harness.store.db:
        harness.store.db.execute(
            "UPDATE channel_outbox SET state='sending' WHERE id=?", (item["id"],)
        )
    await harness.close()
    sends: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        sends.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    reopened = Harness(tmp_path, config, handler)
    try:
        await reopened.manager.start()
        same = await reopened.manager.enqueue(
            "telegram", "7", attachment_ids=[identifier], media_mode="voice", event_id="uncertain"
        )
        assert same["id"] == item["id"] and same["state"] == "unknown"
        fence = await reopened.manager.enqueue("telegram", "7", "继续后续消息")
        assert (await terminal(reopened.manager, fence["id"]))["state"] == "sent"
        assert sends == ["sendMessage"]
    finally:
        await reopened.close()


async def test_default_qq_audio_keeps_its_existing_rich_media_behavior() -> None:
    uploads: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 60})
        if request.url.path.endswith("/files"):
            uploads.append(body)
            return httpx.Response(200, json={"file_info": "opaque"})
        assert body == {"msg_type": 7, "media": {"file_info": "opaque"}}
        return httpx.Response(200, json={"id": "confirmed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQ(profile(app_id="fake", client_secret="fake"), client)
        receipt = await adapter.send(
            {"target": "7", "kind": "private"},
            "",
            FileContent("speech.mp3", "audio/mpeg", MP3),
            key="existing",
            sequence=0,
        )
    assert receipt == "confirmed" and uploads[0]["file_type"] == 3


async def test_queued_voice_revalidates_attachment_before_sending(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        raise AssertionError("改变后的音频不得发送")

    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), handler)
    try:
        identifier = await audio(harness.attachments)
        item = await harness.manager.enqueue(
            "telegram", "7", attachment_ids=[identifier], media_mode="voice"
        )
        harness.attachments.path(identifier).write_bytes(b"ID3CHANGED")
        await harness.manager.start()
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "failed" and "内容已变化" in result["last_error"]
    finally:
        await harness.close()


@pytest.mark.parametrize("failure", ["read_timeout", "missing_receipt"])
async def test_unknown_voice_is_not_retried_or_resent_for_the_same_key(
    tmp_path: Path, failure: str
) -> None:
    voice_sends = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal voice_sends
        if request.url.path.endswith("getUpdates"):
            await asyncio.Event().wait()
        if request.url.path.endswith("sendVoice"):
            voice_sends += 1
            if failure == "read_timeout":
                raise httpx.ReadTimeout("private details", request=request)
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    harness = Harness(
        tmp_path, ChannelsConfig(telegram=profile(), retry_backoff_seconds=0), handler
    )
    try:
        identifier = await audio(harness.attachments)
        arguments: dict[str, Any] = {
            "attachment_ids": [identifier],
            "media_mode": "voice",
            "event_id": "once",
        }
        item = await harness.manager.enqueue("telegram", "7", **arguments)
        await harness.manager.start()
        result = await terminal(harness.manager, item["id"])
        assert result["state"] == "unknown" and "private details" not in result["last_error"]
        with pytest.raises(ChannelError, match="可能重复"):
            await harness.manager.retry(item["id"])
        assert (await harness.manager.enqueue("telegram", "7", **arguments))["state"] == "unknown"
        fence = await harness.manager.enqueue("telegram", "7", "后续投递可继续")
        assert (await terminal(harness.manager, fence["id"]))["state"] == "sent"
        assert voice_sends == 1
    finally:
        await harness.close()


@pytest.mark.parametrize("phase", ["capability", "send"])
async def test_shutdown_distinguishes_voice_capability_query_from_started_send(
    tmp_path: Path, phase: str
) -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if phase == "capability" or request.url.path != "/can_send_record":
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"yes": True}})

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), handler)
    try:
        identifier = await audio(harness.attachments)
        item = await harness.manager.enqueue(
            "onebot", "7", attachment_ids=[identifier], media_mode="voice"
        )
        await harness.manager.start()
        await asyncio.wait_for(entered.wait(), 2)
        await harness.manager.close()
        result = harness.manager.delivery(item["id"])
        assert result is not None
        assert result["state"] == ("pending" if phase == "capability" else "unknown")
    finally:
        await harness.close()


async def test_channel_send_exposes_voice_and_stable_tool_idempotency(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {"channels": {"telegram": {"enabled": True, "owner_ids": ["7"], "token": "123:FAKE"}}}
    )
    attachments = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    application = ChannelApplication(runtime, attachments)
    try:
        identifier = await audio(attachments)
        tool = next(tool for tool in application.tools(runtime) if tool.name == "channel_send")
        assert tool.risk == "external"
        arguments = {
            "channel": "telegram",
            "target": "7",
            "attachment_ids": [identifier],
            "media_mode": "voice",
            "idempotency_key": "stable-operation",
        }
        original = await tool.handler(arguments)
        assert (await tool.handler(arguments))["id"] == original["id"]
        with pytest.raises(ChannelError, match="模式不一致"):
            await tool.handler({**arguments, "media_mode": "file"})
        for key in ["", "a" * 201, "bad\nkey", 3]:
            with pytest.raises(ValueError, match="idempotency_key"):
                await tool.handler({**arguments, "idempotency_key": key})
        assert len(application.manager.deliveries()) == 1
    finally:
        await application.close()
        await runtime.close()
