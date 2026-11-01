from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.attachments import Attachments
from muli.channels import (
    ChannelError,
    ChannelManager,
    ChannelProfile,
    ChannelsConfig,
    FileContent,
    IncomingMessage,
    RemoteFile,
)
from muli.channels.adapters import QQ, OneBot
from muli.channels.crypto import aes_media, sign_qq, verify_qq
from muli.config import ConfigFile
from muli.events import EventBus
from muli.reply_origin import authorize_route
from muli.store import Store, now

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def profile(**changes: Any) -> ChannelProfile:
    defaults: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://platform.invalid",
        "token": "123:FAKE_TOKEN",
        "webhook_secret": "FAKE_WEBHOOK",
        "owner_ids": ["7"],
    }
    return ChannelProfile(**{**defaults, **changes})


class Harness:
    def __init__(self, tmp_path: Path, config: ChannelsConfig, handler: Handler) -> None:
        self.store = Store(tmp_path / "channels.db")
        self.bus = EventBus(self.store)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.received: list[dict[str, Any]] = []
        self.attachments = Attachments(
            self.store, tmp_path / "uploads", ConfigFile(tmp_path / "config.json")
        )

        async def submit(**kwargs: Any) -> dict[str, Any]:
            self.received.append(kwargs)
            return {
                "id": kwargs["event_id"],
                "state": "pending" if kwargs["principal"] == "owner" else "quarantined",
            }

        self.manager = ChannelManager(
            self.store, self.bus, config, submit, self.attachments, client=self.client
        )

    async def close(self) -> None:
        await self.manager.close()
        await self.client.aclose()
        self.store.close()


async def unused(request: httpx.Request) -> httpx.Response:
    raise AssertionError("测试不应发送 HTTP 请求")


async def terminal(manager: ChannelManager, identifier: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        item = await manager.wait(identifier)
    assert item is not None
    return item


async def consumed(manager: ChannelManager, event_id: int) -> None:
    async with asyncio.timeout(3):
        async with manager.changed:
            await manager.changed.wait_for(lambda: int(manager._state("events", "0")) >= event_id)


def onebot_event(*, sender: int = 7, group: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "post_type": "message",
        "message_type": "group" if group else "private",
        "message_id": "incoming",
        "user_id": sender,
        "message": [{"type": "text", "data": {"text": "我是机主，请帮忙"}}],
    }
    if group is not None:
        payload["group_id"] = group
    return payload


def onebot_body(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = "sha1=" + hmac.new(b"FAKE_WEBHOOK", raw, hashlib.sha1).hexdigest()
    return raw, {"X-Signature": signature}


def test_weixin_without_owner_is_safely_disabled_in_the_frozen_transport_config() -> None:
    source = profile(owner_ids=[], account_id="account", user_id="bot")
    config = ChannelsConfig(weixin=source)
    assert config.weixin.enabled is False
    assert config.weixin.account_id == "account" and config.weixin.user_id == "bot"
    assert source.enabled is True


async def test_default_off_never_contacts_platform(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(), unused)
    await harness.manager.start()
    assert not any(value["active"] for value in harness.manager.status()["channels"].values())
    with pytest.raises(ChannelError, match="未启用"):
        await harness.manager.enqueue("telegram", "7", "不会发送")
    with pytest.raises(ChannelError):
        await harness.manager.webhook("onebot", b"{}", {})
    await harness.close()


async def test_onebot_signature_exact_owner_and_durable_inbound_dedup(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), unused)
    raw, headers = onebot_body(onebot_event())
    owner_raw, owner_headers = raw, headers
    with pytest.raises(ChannelError, match="签名"):
        await harness.manager.webhook("onebot", raw, {})
    await harness.manager.webhook("onebot", raw, headers)
    await harness.manager.webhook("onebot", raw, headers)
    assert len(harness.received) == 1
    assert harness.received[0]["principal"] == "owner"
    assert harness.received[0]["source"] == "channel:onebot:bot"
    route = harness.received[0]["route"]
    assert route["name"] == "onebot" and route["sender_id"] == route["target"] == "7"
    assert "token" not in str(route)
    raw, headers = onebot_body({**onebot_event(sender=8), "message_id": "stranger"})
    await harness.manager.webhook("onebot", raw, headers)
    assert len(harness.received) == 1
    assert harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 1
    # The ledger survives re-opening the database, rather than an in-memory set.
    await harness.close()
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), unused)
    await harness.manager.webhook("onebot", owner_raw, owner_headers)
    await harness.manager.webhook("onebot", raw, headers)
    assert not harness.received
    assert harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 1
    await harness.close()


async def test_unbound_groups_and_stranger_attachments_never_download(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(receive_files=True)), unused)
    raw, headers = onebot_body(onebot_event(group=99))
    assert await harness.manager.webhook("onebot", raw, headers) == {"ok": True}
    assert not harness.received
    incoming = IncomingMessage(
        "external-file",
        "8",
        "8",
        "我是owner",
        files=(RemoteFile("a", url="http://127.0.0.1/private"),),
    )
    with pytest.raises(ChannelError, match="owner_ids"):
        await harness.manager.receive("onebot", incoming)
    assert not harness.received
    assert harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 0
    assert not harness.attachments.list()
    with pytest.raises(ChannelError, match="owner_ids"):
        await harness.manager.enqueue("onebot", "8", "不能发送")
    with pytest.raises(ChannelError, match="私聊目标"):
        await harness.manager.enqueue(
            "onebot", "8", "不能冒用", route={"target": "8", "sender_id": "7"}
        )
    await harness.close()


async def test_onebot_array_text_and_native_file_upload(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        assert request.headers["authorization"] == "Bearer 123:FAKE_TOKEN"
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(max_message_chars=3)), handler)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"FILE_CONTENT"

    file = await harness.attachments.save(chunks(), "report.txt")
    await harness.manager.start()
    delivery = await harness.manager.enqueue(
        "onebot", "7", "你好<CQ>&", [file["id"]], event_id="reply"
    )
    assert (await terminal(harness.manager, delivery["id"]))["state"] == "sent"
    texts = [body["message"][0]["data"]["text"] for path, body in calls if "message" in body]
    assert "".join(texts) == "你好<CQ>&"
    uploaded = next(body for path, body in calls if path == "/upload_private_file")
    assert base64.b64decode(uploaded["file"].removeprefix("base64://")) == b"FILE_CONTENT"
    assert uploaded["name"] == "report.txt"
    assert (
        await harness.manager.enqueue("onebot", "7", "你好<CQ>&", [file["id"]], event_id="reply")
    )["state"] == "sent"
    assert len(harness.manager.deliveries()) == 1
    await harness.close()


async def test_outbox_safe_connect_retry_but_read_timeout_is_unknown(tmp_path: Path) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("SECRET_TOKEN must not leak", request=request)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(), retry_backoff_seconds=0), handler)
    await harness.manager.start()
    item = await harness.manager.enqueue("onebot", "7", "safe")
    result = await terminal(harness.manager, item["id"])
    assert result["state"] == "sent" and attempts == 2
    await harness.close()

    async def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("SECRET_TOKEN must not leak", request=request)

    harness = Harness(
        tmp_path, ChannelsConfig(onebot=profile(), retry_backoff_seconds=0), timed_out
    )
    await harness.manager.start()
    item = await harness.manager.enqueue("onebot", "7", "uncertain")
    result = await terminal(harness.manager, item["id"])
    assert result["state"] == "unknown" and result["attempts"] == 1
    assert "SECRET" not in str(result)
    with pytest.raises(ChannelError, match="明确允许"):
        await harness.manager.retry(item["id"])
    await harness.close()


async def test_crash_recovery_never_blindly_retries_sending(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    directory = tmp_path_factory.mktemp("channel-recovery")
    checks: dict[str, list[str]] = {}
    diagnostics: dict[str, Any] = {
        "python_version": sys.version,
        "sqlite_version": sqlite3.sqlite_version,
        "integrity_checks": checks,
        "stage": "open",
        "connections_closed": False,
    }
    harness: Harness | None = None
    primary_error: BaseException | None = None

    def check_integrity(stage: str) -> None:
        assert harness is not None
        diagnostics["stage"] = stage
        checks[stage] = [
            str(row[0]) for row in harness.store.db.execute("PRAGMA integrity_check(10)")
        ]
        assert checks[stage] == ["ok"]

    async def close_current() -> None:
        nonlocal harness
        current, harness = harness, None
        if current is None:
            return
        try:
            await current.close()
        except BaseException as error:
            diagnostics["close_error"] = f"{type(error).__name__}: {error}"
            diagnostics["connections_closed"] = False
            raise
        diagnostics["connections_closed"] = True

    try:
        harness = Harness(directory, ChannelsConfig(onebot=profile()), unused)
        diagnostics["sqlite_source_id"] = harness.store.db.execute(
            "SELECT sqlite_source_id()"
        ).fetchone()[0]
        item = await harness.manager.enqueue("onebot", "7", "possible accepted")
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET state='sending' WHERE id=?", (item["id"],)
            )
        check_integrity("before_close")
        diagnostics["stage"] = "close"
        await close_current()
        diagnostics["stage"] = "reopen"
        # A constructor may open SQLite before failing to return its Harness.
        diagnostics["connections_closed"] = False
        harness = Harness(directory, ChannelsConfig(onebot=profile()), unused)
        check_integrity("after_reopen")
        diagnostics["stage"] = "recover_sending"
        await harness.manager.start()
        assert harness.manager.delivery(item["id"])["state"] == "unknown"
        diagnostics["stage"] = "complete"
    except BaseException as error:
        primary_error = error
        if isinstance(error, sqlite3.Error):
            diagnostics["sqlite_errorcode"] = getattr(error, "sqlite_errorcode", None)
            diagnostics["sqlite_errorname"] = getattr(error, "sqlite_errorname", None)
        error.add_note(json.dumps(diagnostics, ensure_ascii=False, sort_keys=True))
        raise
    finally:
        close_error: BaseException | None = None
        try:
            await close_current()
        except BaseException as error:
            close_error = error
            if primary_error is not None:
                primary_error.add_note("Synthetic database close failed: " + str(error))
        diagnostics["file_evidence_consistency"] = (
            "closed_test_connections" if diagnostics["connections_closed"] else "unconfirmed"
        )
        # Hash only after every test connection was confirmed closed. On close
        # or constructor failure, retained CI files are unconfirmed evidence.
        diagnostics["files"] = (
            {
                name: {
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for name in ("channels.db", "channels.db-wal", "channels.db-shm")
                if (path := directory / name).is_file()
            }
            if diagnostics["connections_closed"]
            else {}
        )
        (directory / "sqlite-diagnostics.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if close_error is not None and primary_error is None:
            close_error.add_note(json.dumps(diagnostics, ensure_ascii=False, sort_keys=True))
            raise close_error


async def test_partial_checkpoint_preserves_successful_parts_on_retry(tmp_path: Path) -> None:
    attempts: list[str] = []
    fail = True

    async def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["message"][0]["data"]["text"]
        attempts.append(text)
        if text == "cd" and fail:
            return httpx.Response(200, json={"status": "failed", "retcode": 10})
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": text}}
        )

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(max_message_chars=2)), handler)
    await harness.manager.start()
    item = await harness.manager.enqueue("onebot", "7", "abcdef")
    result = await terminal(harness.manager, item["id"])
    assert result["state"] == "failed" and result["next_part"] == 1
    fail = False
    await harness.manager.retry(item["id"])
    assert (await terminal(harness.manager, item["id"]))["state"] == "sent"
    assert attempts == ["ab", "cd", "cd", "ef"]
    await harness.close()


async def test_assistant_events_stay_local_and_advance_the_persistent_cursor(
    tmp_path: Path,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    config = ChannelsConfig(onebot=profile())
    harness = Harness(tmp_path, config, handler)
    route = {"name": "onebot", "target": "7", "sender_id": "7", "kind": "private"}
    await harness.bus.emit(
        "message.completed",
        {
            "message": {
                "id": "before-enable",
                "role": "assistant",
                "content": "old",
                "delivery_routes": [route],
            }
        },
    )
    await harness.manager.start()
    for identifier, role, interrupted in (
        ("user", "user", False),
        ("partial", "assistant", True),
        ("stable", "assistant", False),
    ):
        await harness.bus.emit(
            "message.completed",
            {
                "message": {
                    "id": identifier,
                    "role": role,
                    "content": identifier,
                    "interrupted": interrupted,
                    "delivery_routes": [route],
                }
            },
        )
    last = harness.store.db.execute("SELECT max(id) FROM events").fetchone()[0]
    await consumed(harness.manager, last)
    assert calls == 0 and harness.manager.deliveries() == []
    await harness.close()
    harness = Harness(tmp_path, config, handler)
    await harness.manager.start()
    await harness.bus.emit(
        "message.completed",
        {
            "message": {
                "id": "stable",
                "role": "assistant",
                "content": "stable",
                "delivery_routes": [route],
            }
        },
    )
    # Wait until the event cursor crosses the repeated message's durable event.
    last = harness.store.db.execute("SELECT max(id) FROM events").fetchone()[0]
    await consumed(harness.manager, last)
    assert calls == 0 and harness.manager.deliveries() == []
    await harness.close()


async def test_queue_limit_and_current_owner_binding(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(), max_pending_deliveries=1), unused)
    first = await harness.manager.enqueue("onebot", "7", "first")
    with pytest.raises(ChannelError, match="队列已满"):
        await harness.manager.enqueue("onebot", "7", "second")
    # A pending item must be checked against the current binding before it is sent.
    harness.manager.config = replace(
        harness.manager.config, onebot=replace(profile(), owner_ids=["8"])
    )
    await harness.manager.start()
    result = await terminal(harness.manager, first["id"])
    assert result["state"] == "blocked" and "owner_ids" in result["last_error"]
    await harness.close()


async def test_telegram_poll_file_receive_and_native_document_send(tmp_path: Path) -> None:
    second_poll = asyncio.Event()
    polls: list[dict[str, Any]] = []
    sends: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            polls.append(json.loads(request.content))
            if len(polls) == 1:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {
                                "update_id": 20,
                                "message": {
                                    "message_id": 4,
                                    "from": {"id": 7},
                                    "chat": {"id": 7, "type": "private"},
                                    "document": {
                                        "file_id": "file",
                                        "file_name": "hello.txt",
                                        "file_size": 5,
                                    },
                                },
                            }
                        ],
                    },
                )
            second_poll.set()
            await asyncio.Event().wait()
        if request.url.path.endswith("getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "documents/hello.txt"}}
            )
        if "/file/" in request.url.path:
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"HELLO")
        sends.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    harness = Harness(tmp_path, ChannelsConfig(telegram=profile(receive_files=True)), handler)
    await harness.manager.start()
    async with asyncio.timeout(3):
        await second_poll.wait()
    assert len(harness.received) == 1 and polls[1]["offset"] == 21
    received = harness.received[0]
    assert received["principal"] == "owner" and len(received["attachment_ids"]) == 1
    file = harness.attachments.path(received["attachment_ids"][0]).read_bytes()
    assert file == b"HELLO"
    item = await harness.manager.enqueue("telegram", "7", "中文<&>", received["attachment_ids"])
    assert (await terminal(harness.manager, item["id"]))["state"] == "sent"
    assert sends[0].url.path.endswith("sendMessage") and "parse_mode" not in json.loads(
        sends[0].content
    )
    assert sends[1].url.path.endswith("sendDocument") and b"HELLO" in sends[1].content
    assert "123:FAKE_TOKEN" not in str(harness.manager.status())
    await harness.close()


async def test_bad_or_oversized_file_does_not_block_owner_text(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, ChannelsConfig(onebot=profile(receive_files=True, max_file_bytes=4)), unused
    )
    incoming = IncomingMessage(
        "large",
        "7",
        "7",
        "仍需处理的文字",
        files=(RemoteFile("too-big", url="https://cdn.invalid/file", size=10),),
    )
    await harness.manager.receive("onebot", incoming)
    assert harness.received[0]["content"].startswith("仍需处理的文字")
    assert "附件未接收" in harness.received[0]["content"]
    assert not harness.received[0]["attachment_ids"]
    await harness.close()


async def test_qq_signature_handshake_media_and_global_reply_sequence(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    requests: list[tuple[str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "FAKE_QQ_TOKEN", "expires_in": "7200"})
        assert request.headers["authorization"] == "QQBot FAKE_QQ_TOKEN"
        if request.url.path.endswith("/files"):
            assert base64.b64decode(body["file_data"]) == b"FILE"
            return httpx.Response(200, json={"file_info": "opaque_file_info"})
        return httpx.Response(200, json={"id": "sent"})

    qq = profile(app_id="app", client_secret="FAKE_QQ_SECRET")
    harness = Harness(tmp_path, ChannelsConfig(qq=qq), handler)
    challenge = {"op": 13, "d": {"plain_token": "challenge", "event_ts": str(int(now()))}}
    response = await harness.manager.webhook(
        "qq", json.dumps(challenge).encode(), {"X-Bot-Appid": "app"}
    )
    verify_qq(
        "FAKE_QQ_SECRET", (challenge["d"]["event_ts"] + "challenge").encode(), response["signature"]
    )
    with pytest.raises(ChannelError, match="验证请求"):
        await harness.manager.webhook(
            "qq", b'{"op":13,"d":{"plain_token":"{}","event_ts":"123"}}', {"X-Bot-Appid": "app"}
        )
    payload = {
        "id": "event",
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {"id": "msg", "author": {"user_openid": "7"}, "content": "你好"},
    }
    raw, timestamp = json.dumps(payload).encode(), str(int(now()))
    with pytest.raises(ChannelError, match="签名"):
        await harness.manager.webhook(
            "qq", raw, {"X-Bot-Appid": "app", "X-Signature-Timestamp": timestamp}
        )
    await harness.manager.webhook(
        "qq",
        raw,
        {
            "X-Bot-Appid": "app",
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": sign_qq("FAKE_QQ_SECRET", timestamp.encode() + raw),
        },
    )
    route = harness.received[0]["route"]

    async def chunks() -> AsyncIterator[bytes]:
        yield b"FILE"

    file = await harness.attachments.save(chunks(), "data.txt")
    await harness.manager.start()
    first = await harness.manager.enqueue(
        "qq", "7", "answer", [file["id"]], route=route, event_id="answer1"
    )
    assert (await terminal(harness.manager, first["id"]))["state"] == "sent"
    assert (
        await harness.manager.enqueue(
            "qq", "7", "answer", [file["id"]], route=route, event_id="answer1"
        )
    )["state"] == "sent"
    second = await harness.manager.enqueue("qq", "7", "follow up", route=route, event_id="answer2")
    await terminal(harness.manager, second["id"])
    outgoing = [body for path, body in requests if path.endswith("/messages")]
    assert [body["msg_seq"] for body in outgoing] == [1, 2, 3]
    assert outgoing[1]["media"] == {"file_info": "opaque_file_info"}
    assert len([path for path, body in requests if path.endswith("getAppAccessToken")]) == 1
    await harness.close()


async def test_weixin_context_private_media_roundtrip_and_wire_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("cryptography")
    key = b"1234567890123456"
    incoming_cipher = aes_media(b"INBOUND", key, encrypt=True)
    uploaded: list[bytes] = []
    sent: list[dict[str, Any]] = []

    async def public(url: str) -> tuple[httpx.URL, str, str]:
        return httpx.URL(url), "cdn.invalid", "cdn.invalid"

    monkeypatch.setattr("muli.channels.http.public_url", public)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.invalid":
            assert "authorization" not in request.headers
            if request.method == "GET":
                return httpx.Response(200, content=incoming_cipher)
            uploaded.append(request.content)
            return httpx.Response(200, headers={"x-encrypted-param": "DOWNLOAD"})
        body = json.loads(request.content)
        assert request.headers["authorizationtype"] == "ilink_bot_token"
        assert body["base_info"]["channel_version"] == "2.4.6"
        if request.url.path.endswith("getuploadurl"):
            assert body["media_type"] == 3 and body["rawsize"] == 7
            return httpx.Response(
                200, json={"ret": 0, "upload_full_url": "https://cdn.invalid/upload"}
            )
        if request.url.path.endswith("sendmessage"):
            sent.append(body["msg"])
            return httpx.Response(200, json={"ret": 0})
        await asyncio.Event().wait()
        return httpx.Response(200, json={"msgs": []})

    weixin = profile(account_id="account", user_id="bot_user", receive_files=True)
    harness = Harness(tmp_path, ChannelsConfig(weixin=weixin), handler)
    remote = RemoteFile(
        "data.txt",
        metadata={
            "media": {
                "full_url": "https://cdn.invalid/incoming",
                "aes_key": base64.b64encode(key).decode(),
            }
        },
    )
    await harness.manager.receive(
        "weixin",
        IncomingMessage("in", "7", "7", "", files=(remote,), context_token="PRIVATE_CONTEXT"),
    )
    file_id = harness.received[0]["attachment_ids"][0]
    assert harness.attachments.path(file_id).read_bytes() == b"INBOUND"
    assert "PRIVATE_CONTEXT" not in str(harness.received[0]["route"])
    await harness.manager.start()
    item = await harness.manager.enqueue("weixin", "7", "回复", [file_id])
    assert (await terminal(harness.manager, item["id"]))["state"] == "sent"
    assert sent[0]["context_token"] == "PRIVATE_CONTEXT"
    media = sent[1]["item_list"][0]["file_item"]["media"]
    upload_key = bytes.fromhex(base64.b64decode(media["aes_key"]).decode())
    assert aes_media(uploaded[0], upload_key, encrypt=False) == b"INBOUND"
    assert "PRIVATE_CONTEXT" not in str(harness.manager.deliveries()) + str(
        harness.manager.status()
    )
    await harness.close()


async def test_decoded_response_limit_and_safe_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OneBot(profile(max_response_bytes=16), client)
        with pytest.raises(ChannelError) as caught:
            await adapter.send(
                {"target": "7", "kind": "private"}, "text", None, key="k", sequence=0
            )
        assert caught.value.uncertain
        assert "FAKE_TOKEN" not in str(caught.value)


def test_profiles_validate_and_public_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULI_TEST_CHANNEL", "SECRET")
    value = replace(profile(), token_env="MULI_TEST_CHANNEL")
    assert value.secret("token") == "SECRET"
    assert "FAKE_TOKEN" not in str(value.public())
    assert "FAKE_WEBHOOK" not in repr(value)
    with pytest.raises(ChannelError):
        profile(base_url="https://name:secret@example.com")
    with pytest.raises(ChannelError):
        profile(timeout=float("inf"))


async def test_owner_attachment_local_address_is_rejected_without_http(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(receive_files=True)), unused)
    await harness.manager.receive(
        "onebot",
        IncomingMessage(
            "private-address",
            "7",
            "7",
            "继续处理文字",
            files=(RemoteFile("secret.txt", url="http://127.0.0.1/private"),),
        ),
    )
    assert harness.received[0]["content"].startswith("继续处理文字")
    assert "附件未接收" in harness.received[0]["content"]
    assert not harness.attachments.list()
    await harness.close()


async def test_transient_attachment_failure_is_bounded_and_text_eventually_progresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(receive_files=True)), unused)
    attempts = 0

    async def download(file: RemoteFile, route: dict[str, Any]) -> FileContent:
        nonlocal attempts
        attempts += 1
        raise ChannelError("渠道连接未建立", retryable=True)

    monkeypatch.setattr(harness.manager.adapters["onebot"], "download", download)
    incoming = IncomingMessage("retry-file", "7", "7", "附件失败也要处理", files=(RemoteFile("f"),))
    for _ in range(2):
        with pytest.raises(ChannelError):
            await harness.manager.receive("onebot", incoming)
    await harness.manager.receive("onebot", incoming)
    await harness.manager.receive("onebot", incoming)
    assert attempts == 3 and len(harness.received) == 1
    assert "附件未接收" in harness.received[0]["content"]
    await harness.close()


async def test_saved_attachment_is_not_imported_twice_when_submit_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(receive_files=True)), unused)
    downloads, submits = 0, 0

    async def download(file: RemoteFile, route: dict[str, Any]) -> FileContent:
        nonlocal downloads
        downloads += 1
        return FileContent("data.txt", "text/plain", b"DATA")

    async def submit(**kwargs: Any) -> dict[str, Any]:
        nonlocal submits
        submits += 1
        if submits == 1:
            raise RuntimeError("temporary admission failure")
        harness.received.append(kwargs)
        return {"state": "pending"}

    monkeypatch.setattr(harness.manager.adapters["onebot"], "download", download)
    harness.manager.submit = submit
    incoming = IncomingMessage("saved-file", "7", "7", "文字", files=(RemoteFile("data.txt"),))
    with pytest.raises(RuntimeError):
        await harness.manager.receive("onebot", incoming)
    assert len(harness.attachments.list()) == 1
    await harness.manager.receive("onebot", incoming)
    assert downloads == 1 and len(harness.attachments.list()) == 1
    assert harness.received[0]["attachment_ids"] == [harness.attachments.list()[0]["id"]]
    await harness.close()


async def test_close_during_final_send_records_unknown_and_releases_waiter(tmp_path: Path) -> None:
    sending = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        sending.set()
        await asyncio.Event().wait()
        raise AssertionError("should be cancelled")

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), handler)
    await harness.manager.start()
    item = await harness.manager.enqueue("onebot", "7", "可能已经发送")
    async with asyncio.timeout(3):
        await sending.wait()
        waiter = asyncio.create_task(harness.manager.wait(item["id"]))
        await harness.manager.close()
        result = await waiter
    assert result is not None and result["state"] == "unknown"
    assert not harness.manager.status()["running"]
    await harness.close()


async def test_deleted_attachment_in_local_event_does_not_block_explicit_send(
    tmp_path: Path,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), handler)
    await harness.manager.start()
    route = {"name": "onebot", "target": "7", "sender_id": "7", "kind": "private"}
    for identifier, attachments in (("deleted", ["missing"]), ("valid", [])):
        await harness.bus.emit(
            "message.completed",
            {
                "message": {
                    "id": identifier,
                    "role": "assistant",
                    "content": "正文",
                    "attachment_ids": attachments,
                    "delivery_routes": [route],
                }
            },
        )
    last = harness.store.db.execute("SELECT max(id) FROM events").fetchone()[0]
    await consumed(harness.manager, last)
    assert calls == 0 and harness.manager.deliveries() == []
    delivery = await harness.manager.enqueue("onebot", "7", "显式回复")
    assert (await terminal(harness.manager, delivery["id"]))["state"] == "sent"
    assert calls == 1
    assert not harness.manager.status()["channels"]["onebot"]["last_error"]
    await harness.close()


async def test_http_429_retries_are_finite(tmp_path: Path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="FAKE_TOKEN should not leak")

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(), retry_backoff_seconds=0), handler)
    await harness.manager.start()
    item = await harness.manager.enqueue("onebot", "7", "limited")
    result = await terminal(harness.manager, item["id"])
    assert result["state"] == "failed" and result["attempts"] == calls == 3
    assert result["last_error"] == "渠道 HTTP 429"
    await harness.close()


async def test_qq_reply_budget_rejects_before_token_or_file_upload(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        ChannelsConfig(
            qq=profile(app_id="app", client_secret="FAKE_QQ_SECRET", max_message_chars=1)
        ),
        unused,
    )
    route = {"target": "7", "sender_id": "7", "kind": "private", "reply_to": "incoming"}
    first = await harness.manager.enqueue("qq", "7", "12345", route=route)
    with pytest.raises(ChannelError, match="预算不足"):
        await harness.manager.enqueue("qq", "7", "6", route=route)
    assert len(harness.manager.deliveries()) == 1
    assert len(first["payload"]["parts"]) == 5
    await harness.close()


async def test_qq_invalid_token_lifetime_is_rejected_without_delivery() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "FAKE_QQ_TOKEN", "expires_in": "nan"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQ(profile(app_id="app", client_secret="FAKE_QQ_SECRET"), client)
        with pytest.raises(ChannelError, match="响应无效"):
            await adapter.send(
                {"target": "7", "kind": "private"}, "text", None, key="k", sequence=0
            )
    assert calls == 1


@pytest.mark.parametrize("change", ["text", "sender", "attachment"])
async def test_incomplete_inbound_rejects_changed_envelope_before_checkpoint_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    harness = Harness(
        tmp_path, ChannelsConfig(onebot=profile(receive_files=True, owner_ids=["7", "8"])), unused
    )
    downloads: list[str] = []
    allowed = False

    async def download(file: RemoteFile, route: dict[str, Any]) -> FileContent:
        downloads.append(file.name)
        return FileContent(file.name, "text/plain", file.name.encode("utf-8"))

    async def submit(**kwargs: Any) -> dict[str, Any]:
        if not allowed:
            raise ValueError("队列暂满")
        harness.received.append(kwargs)
        return {"state": "pending"}

    monkeypatch.setattr(harness.manager.adapters["onebot"], "download", download)
    harness.manager.submit = submit
    original = IncomingMessage(
        "immutable",
        "7",
        "7",
        "原始正文",
        files=(RemoteFile("old.txt", url="https://cdn.invalid/file?token=PRIVATE_CDN_TOKEN"),),
    )
    with pytest.raises(ValueError, match="队列暂满"):
        await harness.manager.receive("onebot", original)
    changed = (
        replace(original, text="变更正文")
        if change == "text"
        else replace(
            original,
            sender_id="8",
            target="8",
        )
        if change == "sender"
        else replace(original, files=(RemoteFile("new.txt"),))
    )
    with pytest.raises(ChannelError, match="信封内容不一致") as caught:
        await harness.manager.receive("onebot", changed)
    assert caught.value.status_code == 409
    assert downloads == ["old.txt"] and not harness.received
    assert len(harness.attachments.list()) == 1
    allowed = True
    await harness.manager.receive("onebot", original)
    assert downloads == ["old.txt"]
    assert harness.received[0]["content"] == "原始正文"
    assert harness.attachments.get(harness.received[0]["attachment_ids"][0])["name"] == "old.txt"
    row = dict(harness.store.db.execute("SELECT * FROM channel_inbound").fetchone())
    assert len(row["fingerprint"]) == 64 and "PRIVATE_CDN_TOKEN" not in str(row)
    await harness.close()


@pytest.mark.parametrize("change", ["text", "sender", "attachment"])
async def test_completed_inbound_checks_fingerprint_before_returning_receipt(
    tmp_path: Path,
    change: str,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(owner_ids=["7", "8"])), unused)
    original = IncomingMessage("completed", "7", "7", "原始正文", files=(RemoteFile("old.txt"),))
    receipt = await harness.manager.receive("onebot", original)
    assert await harness.manager.receive("onebot", original) == receipt
    changed = (
        replace(original, text="变更正文")
        if change == "text"
        else replace(
            original,
            sender_id="8",
            target="8",
        )
        if change == "sender"
        else replace(original, files=(RemoteFile("new.txt"),))
    )
    with pytest.raises(ChannelError) as caught:
        await harness.manager.receive("onebot", changed)
    assert caught.value.status_code == 409
    assert len(harness.received) == 1
    await harness.close()


async def test_legacy_inbound_schema_migrates_without_claiming_unknown_fingerprints(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(receive_files=True)), unused)
    submit = harness.manager.submit
    await harness.manager.close()
    with harness.store.db:
        harness.store.db.execute("DROP TABLE channel_inbound")
        harness.store.db.execute(
            "CREATE TABLE channel_inbound(id TEXT PRIMARY KEY,name TEXT NOT NULL,"
            "attachment_ids TEXT NOT NULL DEFAULT '[]',receipt TEXT,created_at REAL NOT NULL)"
        )
        for event_id, receipt in (
            ("legacy-completed", '{"state":"pending","id":"original"}'),
            ("legacy-incomplete", None),
        ):
            key = hashlib.sha256(
                json.dumps(["onebot", "bot", event_id], ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            harness.store.db.execute(
                "INSERT INTO channel_inbound VALUES(?,?,?,?,?)",
                (
                    key,
                    "onebot",
                    '["old-attachment"]',
                    receipt,
                    now(),
                ),
            )
    harness.manager = ChannelManager(
        harness.store,
        harness.bus,
        harness.manager.config,
        submit,
        harness.attachments,
        client=harness.client,
    )
    assert "fingerprint" in {
        row[1] for row in harness.store.db.execute("PRAGMA table_info(channel_inbound)")
    }
    result = await harness.manager.receive(
        "onebot", IncomingMessage("legacy-completed", "7", "7", "旧内容未知")
    )
    assert result == {"state": "pending", "id": "original", "fingerprint_verified": False}
    with pytest.raises(ChannelError, match="人工核对") as caught:
        await harness.manager.receive(
            "onebot",
            IncomingMessage(
                "legacy-incomplete",
                "7",
                "7",
                "不能搭配旧附件",
                files=(RemoteFile("new.txt"),),
            ),
        )
    assert caught.value.status_code == 409 and not harness.received
    assert harness.manager.status()["inbound"] == {"legacy_unverified": 2, "legacy_incomplete": 1}
    assert all(
        row[0] is None
        for row in harness.store.db.execute("SELECT fingerprint FROM channel_inbound")
    )
    await harness.close()


@pytest.mark.parametrize("name", ["onebot", "telegram", "qq"])
def test_enabled_channel_requires_a_bound_owner(name: str) -> None:
    with pytest.raises(ChannelError, match="必须填写"):
        ChannelsConfig(**{name: profile(owner_ids=[])})


def test_weixin_owner_ids_are_bounded_and_groups_remain_opt_in() -> None:
    with pytest.raises(ChannelError, match="owner_ids"):
        ChannelsConfig(weixin=profile(owner_ids=[""]))
    route = authorize_route(
        "weixin",
        {"sender_id": "7", "target": "99", "kind": "group"},
        account_id="bot",
        owner_ids=["7"],
        allowed_groups=["99"],
        outbound=True,
    )
    assert route == {
        "name": "weixin",
        "account_id": "bot",
        "target": "99",
        "sender_id": "7",
        "kind": "group",
    }
