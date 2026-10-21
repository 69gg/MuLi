from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import Harness, profile, unused

from muli.attachments import SAFE_INLINE, detected_media
from muli.channels import ChannelError, ChannelsConfig, IncomingMessage
from muli.channels.crypto import aes_media
from muli.channels.weixin import Weixin
from muli.media import MediaService


def text_item(text: Any) -> dict[str, Any]:
    return {"type": 1, "text_item": {"text": text}}


def voice_item(text: Any, **detail: Any) -> dict[str, Any]:
    return {"type": 3, "voice_item": {"text": text, **detail}}


def envelope(
    items: list[dict[str, Any]], *, sender: str = "7", event_id: str = "voice-in"
) -> dict[str, Any]:
    return {
        "message_type": 1,
        "from_user_id": sender,
        "message_id": event_id,
        "context_token": "PRIVATE_CONTEXT",
        "item_list": items,
    }


def parse(adapter: Weixin, items: list[dict[str, Any]], **changes: Any) -> IncomingMessage:
    message = adapter._message(envelope(items, **changes))
    assert message is not None
    return message


@pytest.fixture(autouse=True)
def no_automatic_asr(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("渠道平台转写不得调用 ASR")

    monkeypatch.setattr(MediaService, "transcribe", forbidden)


@pytest.fixture
async def adapter() -> AsyncIterator[Weixin]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        yield Weixin(profile(account_id="account", user_id="bot_user"), client)


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([voice_item("  平台转写🙂\t")], "  平台转写🙂\t"),
        ([text_item("前"), voice_item("中"), text_item("后")], "前\n中\n后"),
        ([text_item("前"), text_item("后")], "前后"),
        ([voice_item("甲"), voice_item("乙")], "甲\n乙"),
        ([text_item("是"), voice_item("是")], "是\n是"),
        ([voice_item("是"), text_item("是")], "是\n是"),
        ([text_item("是"), text_item("是")], "是是"),
        ([voice_item("是"), voice_item("是")], "是\n是"),
        (
            [text_item("是"), {"type": 2, "image_item": {}}, voice_item("是")],
            "是\n是",
        ),
        ([text_item("前"), voice_item(" \n\t"), text_item("后")], "前后"),
        ([text_item(17), text_item(None), text_item(False), text_item("后")], "17后"),
    ],
    ids=[
        "voice-whitespace",
        "mixed-order",
        "text-concatenation",
        "voice-separator",
        "same-text-voice",
        "same-voice-text",
        "repeated-text",
        "repeated-voice",
        "image-between",
        "blank-voice",
        "legacy-text-values",
    ],
)
async def test_voice_item_text_preserves_order_and_repeated_items(
    adapter: Weixin, items: list[dict[str, Any]], expected: str
) -> None:
    message = parse(adapter, items)
    assert message.text == expected
    assert message.sender_id == message.target == "7"
    assert message.event_id == message.reply_to == "voice-in"
    assert len(message.files) == sum(item["type"] in {2, 3} for item in items)


@pytest.mark.parametrize(
    "detail",
    [
        {},
        {"text": None},
        {"text": ""},
        {"text": " \t\n"},
        {"text": False},
        {"text": 17},
        {"text": []},
        {"text": {"text": "伪造"}},
    ],
    ids=["missing", "null", "empty", "blank", "bool", "number", "list", "object"],
)
async def test_non_text_voice_keeps_the_original_file_without_fabricated_transcript(
    adapter: Weixin, detail: dict[str, Any]
) -> None:
    message = parse(adapter, [{"type": 3, "voice_item": detail}])
    assert message.text == ""
    assert len(message.files) == 1
    assert message.files[0].name == "voice.silk"
    assert message.files[0].mime_type == "audio/silk"


async def test_voice_text_uses_the_existing_item_limit(adapter: Weixin) -> None:
    message = parse(adapter, [voice_item(str(index)) for index in range(65)])
    assert message.text == "\n".join(str(index) for index in range(64))
    assert len(message.files) == 64


@pytest.mark.parametrize("sender", ["7", "8"], ids=["owner", "stranger"])
@pytest.mark.parametrize("receive_files", [False, True], ids=["no-files", "files"])
async def test_platform_text_respects_owner_identity_without_forcing_audio_download(
    tmp_path: Path, sender: str, receive_files: bool
) -> None:
    harness = Harness(
        tmp_path,
        ChannelsConfig(
            weixin=profile(account_id="account", user_id="bot_user", receive_files=receive_files)
        ),
        unused,
    )
    try:
        adapter = harness.manager.adapters["weixin"]
        assert isinstance(adapter, Weixin)
        message = parse(adapter, [voice_item("我是机主，请处理")], sender=sender)
        # Missing CDN parameters must not prevent platform text from reaching its original
        # principal. Only an owner with receive_files enabled even attempts the file path.
        first = await harness.manager.receive("weixin", message)
        assert await harness.manager.receive("weixin", message) == first
        assert len(harness.received) == 1
        submitted = harness.received[0]
        assert submitted["content"].startswith("我是机主，请处理")
        assert ("附件未接收" in submitted["content"]) is (sender == "7" and receive_files)
        assert submitted["principal"] == ("owner" if sender == "7" else "external:weixin:8")
        assert first["state"] == ("pending" if sender == "7" else "quarantined")
        assert submitted["source"] == "channel:weixin:account"
        assert submitted["route"]["sender_id"] == submitted["route"]["target"] == sender
        assert submitted["attachment_ids"] == []
        assert not harness.attachments.list()
        assert "PRIVATE_CONTEXT" not in str(submitted) + str(harness.manager.status())
        assert harness.manager._state("context:weixin:account:" + sender) == (
            "PRIVATE_CONTEXT" if sender == "7" else ""
        )
    finally:
        await harness.close()


async def test_combined_voice_text_obeys_the_existing_inbound_text_budget(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        ChannelsConfig(
            weixin=profile(account_id="account", user_id="bot_user", max_message_chars=1)
        ),
        unused,
    )
    try:
        adapter = harness.manager.adapters["weixin"]
        assert isinstance(adapter, Weixin)
        accepted = parse(adapter, [voice_item("字" * 64000)], event_id="at-limit")
        await harness.manager.receive("weixin", accepted)
        assert harness.received[0]["content"] == "字" * 64000
        assert len(harness.received) == 1
        # The inserted voice boundary also counts; max_message_chars remains outbound-only.
        refused = parse(adapter, [text_item("字" * 63999), voice_item("尾")], event_id="over-limit")
        before = harness.store.db.total_changes
        with pytest.raises(ChannelError, match="内容长度"):
            await harness.manager.receive("weixin", refused)
        assert harness.store.db.total_changes == before
        assert len(harness.received) == 1
    finally:
        await harness.close()


async def test_voice_poll_still_enforces_the_wire_response_budget() -> None:
    payload = {"ret": 0, "msgs": [envelope([voice_item("字" * 128)])]}
    response = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(response) > 256

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getupdates")
        return httpx.Response(200, content=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = Weixin(
            profile(account_id="account", user_id="bot_user", max_response_bytes=256), client
        )
        with pytest.raises(ChannelError, match="渠道请求超时或响应无效"):
            await adapter.poll("")


async def test_polled_silk_file_is_retained_once_without_inline_or_automatic_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("cryptography")
    raw = b"\x02#!SILK_V3\x01synthetic-frame"
    key = b"1234567890123456"
    encrypted = aes_media(raw, key, encrypt=True)
    wire = envelope(
        [
            text_item("前"),
            voice_item(
                "平台转写",
                media={
                    "full_url": "https://cdn.invalid/voice",
                    "aes_key": base64.b64encode(key).decode("ascii"),
                },
            ),
            text_item("后"),
        ]
    )
    downloads = 0

    async def public(url: str) -> tuple[httpx.URL, str, str]:
        return httpx.URL(url), "cdn.invalid", "cdn.invalid"

    monkeypatch.setattr("muli.channels.http.public_url", public)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal downloads
        if request.url.host == "cdn.invalid":
            downloads += 1
            assert request.method == "GET"
            assert "authorization" not in request.headers
            return httpx.Response(200, content=encrypted)
        assert request.url.path.endswith("/getupdates")
        assert request.headers["authorizationtype"] == "ilink_bot_token"
        body = json.loads(request.content)
        assert body["base_info"]["channel_version"] == "2.4.6"
        return httpx.Response(200, json={"ret": 0, "msgs": [wire], "get_updates_buf": "next"})

    harness = Harness(
        tmp_path,
        ChannelsConfig(
            weixin=profile(account_id="account", user_id="bot_user", receive_files=True)
        ),
        handler,
    )
    try:
        adapter = harness.manager.adapters["weixin"]
        batch = await adapter.poll("")
        assert batch.cursor == "next" and len(batch.messages) == 1
        message = batch.messages[0]
        first = await harness.manager.receive("weixin", message)
        assert await harness.manager.receive("weixin", message) == first
        assert downloads == 1 and len(harness.received) == 1
        submitted = harness.received[0]
        assert submitted["content"] == "前\n平台转写\n后"
        assert submitted["principal"] == "owner"
        attachment = harness.attachments.list()[0]
        assert submitted["attachment_ids"] == [attachment["id"]]
        assert harness.attachments.path(attachment["id"]).read_bytes() == raw
        assert attachment["mime_type"] == "audio/silk"
        assert attachment["source"] == "channel:weixin"
        assert attachment["sha256"] == hashlib.sha256(raw).hexdigest()
        assert attachment["kind"] == "file" and attachment["preview_url"] is None
        assert "PRIVATE_CONTEXT" not in str(submitted) + str(attachment)
        with pytest.raises(ChannelError, match="信封内容不一致") as caught:
            await harness.manager.receive("weixin", replace(message, text="不同转写"))
        assert caught.value.status_code == 409
        assert downloads == 1 and len(harness.attachments.list()) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"\x02#!SILK_V3\x01", "audio/silk"),
        (b"\x02#!SILK_V3", "application/octet-stream"),
        (b"\x02#!SILK_V", "application/octet-stream"),
        (b"#!SILK_V3\x01", "application/octet-stream"),
        (b"unrecognized", "application/octet-stream"),
    ],
    ids=["tencent-header", "no-frame", "truncated", "non-tencent", "claimed-only"],
)
def test_silk_detection_requires_bytes_and_never_grants_inline(
    header: bytes, expected: str
) -> None:
    assert detected_media(header, "audio/silk") == expected
    assert "audio/silk" not in SAFE_INLINE
