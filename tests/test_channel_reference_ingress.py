"""Native reference facts survive real channel admission without adding authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import pytest
from test_channels import Harness, profile, unused
from test_mobile_channels import telegram_payload
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelError, ChannelsConfig, IncomingMessage
from muli.channels.adapters import Telegram
from muli.channels.manager import encoded
from muli.channels.types import normalize_channel_reference
from muli.permissions import Mode
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime


def identified(message_id: str = "6001") -> dict[str, Any]:
    return {"version": 1, "status": "identified", "message_id": message_id}


def native_payload(
    *, update_id: int = 10, text: str = "把这个改为一周，只给草稿"
) -> dict[str, Any]:
    payload = telegram_payload(text)
    payload["update_id"] = update_id
    payload["message"]["reply_to_message"] = {
        "message_id": 6001,
        "chat": {"id": 7, "type": "private"},
        "text": "QUOTED_BODY_MUST_NEVER_BE_PERSISTED",
    }
    return payload


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("channel reference ingress verification must stay offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def pending_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Runtime, ChannelApplication]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "limits": {"max_pending_runs": 16},
            "channels": {
                "telegram": {
                    "enabled": True,
                    "base_url": "https://platform.invalid",
                    "token": "123:FAKE_TOKEN",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7"],
                }
            },
        }
    )

    def keep_pending(session: dict[str, Any], role: str, mode: Mode) -> None:
        # These tests exercise durable host admission, without a model turn.
        return None

    monkeypatch.setattr(runtime, "_launch", keep_pending)
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    return runtime, application


@pytest.fixture
async def host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, ChannelApplication]]:
    runtime, application = pending_host(tmp_path, monkeypatch)
    try:
        yield runtime, application
    finally:
        await application.close()
        await runtime.close()


def persisted(runtime: Runtime, input_id: str) -> dict[str, Any]:
    row = runtime.store.db.execute("SELECT payload FROM ingress WHERE id=?", (input_id,)).fetchone()
    assert row is not None
    return cast(dict[str, Any], json.loads(row["payload"]))


@pytest.mark.parametrize("marker", [None, identified(), {"version": 1, "status": "unavailable"}])
def test_reference_marker_accepts_only_the_finite_host_contract(marker: object) -> None:
    normalized = normalize_channel_reference(marker)
    assert normalized == marker
    if marker is not None:
        assert normalized is not marker


@pytest.mark.parametrize(
    "marker",
    [
        {},
        [],
        "6001",
        {"version": True, "status": "unavailable"},
        {"version": 1.0, "status": "unavailable"},
        {"version": 2, "status": "unavailable"},
        {"version": 1, "status": "unknown"},
        {"version": 1, "status": "unavailable", "message_id": "6001"},
        {**identified(), "text": "not a finite marker"},
        {"version": 1, "status": "identified"},
        *[identified(value) for value in ("", "0", "01", "-1", "+1", " 1", "1 ", "１")],
        *[{**identified(), "message_id": value} for value in (True, 1, 1.0, None)],
    ],
)
def test_reference_marker_rejects_extra_fields_and_noncanonical_ids(marker: object) -> None:
    with pytest.raises(ValueError, match="引用"):
        normalize_channel_reference(marker)


@pytest.mark.parametrize("thread_id", [None, 17])
async def test_telegram_extracts_only_same_chat_thread_id_without_quoted_body(
    thread_id: int | None,
) -> None:
    payload = native_payload()
    if thread_id is not None:
        payload["message"]["message_thread_id"] = thread_id
        payload["message"]["reply_to_message"]["message_thread_id"] = thread_id
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        incoming = Telegram(profile(), client).webhook(payload)[0]
    assert incoming.channel_reference == identified()
    assert incoming.reply_to == "11" and incoming.text == payload["message"]["text"]
    assert "QUOTED_BODY" not in encoded(incoming.channel_reference)
    assert incoming.control_text == incoming.control_account_id == ""


@pytest.mark.parametrize(
    "quoted",
    [
        None,
        {},
        [],
        {"message_id": "6001", "chat": {"id": 7, "type": "private"}},
        {"message_id": True, "chat": {"id": 7, "type": "private"}},
        {"message_id": 0, "chat": {"id": 7, "type": "private"}},
        {"message_id": -1, "chat": {"id": 7, "type": "private"}},
        {"message_id": 6001},
        {"message_id": 6001, "chat": {"id": 8, "type": "private"}},
        {"message_id": 6001, "chat": {"id": 7, "type": "group"}},
        {"message_id": 6001, "chat": {"id": "7", "type": "private"}},
        {"message_id": 6001, "chat": {"id": 7, "type": "private"}, "message_thread_id": 17},
        {"message_id": 6001, "chat": {"id": 7, "type": "private"}, "message_thread_id": None},
        {"message_id": 6001, "chat": {"id": 7, "type": "private"}, "message_thread_id": True},
    ],
)
async def test_telegram_keeps_malformed_or_cross_domain_reference_unavailable(
    quoted: object,
) -> None:
    payload = native_payload()
    payload["message"]["reply_to_message"] = deepcopy(quoted)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        incoming = Telegram(profile(), client).webhook(payload)[0]
    assert incoming.channel_reference == {"version": 1, "status": "unavailable"}


@pytest.mark.parametrize("field", ["external_reply", "quote", "reply_to_story"])
async def test_telegram_preserves_unsupported_reference_presence(field: str) -> None:
    payload = telegram_payload("同一正文")
    payload["message"][field] = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        adapter = Telegram(profile(), client)
        assert adapter.webhook(telegram_payload("同一正文"))[0].channel_reference is None
        incoming = adapter.webhook(payload)[0]
    assert incoming.channel_reference == {"version": 1, "status": "unavailable"}


async def test_ordinary_fingerprint_is_the_exact_pre_reference_envelope(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), unused)
    try:
        message = IncomingMessage("ordinary", "7", "7", "普通主人正文", reply_to="11")
        await harness.manager.receive("telegram", message)
        old_envelope = {
            "event_id": "ordinary",
            "sender_id": "7",
            "target": "7",
            "text": "普通主人正文",
            "kind": "private",
            "reply_to": "11",
            "files": (),
            "context_token": "",
        }
        expected = hashlib.sha256(encoded(old_envelope).encode("utf-8")).hexdigest()
        assert harness.store.db.execute("SELECT fingerprint FROM channel_inbound").fetchone()[
            0
        ] == (expected)
    finally:
        await harness.close()


@pytest.mark.parametrize("completed", [False, True])
async def test_same_event_cannot_change_identified_reference_before_or_after_receipt(
    tmp_path: Path, completed: bool
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), unused)
    message = IncomingMessage("same-event", "7", "7", "只改这个", channel_reference=identified())
    try:
        await harness.manager.receive("telegram", message)
        if not completed:
            with harness.store.db:
                harness.store.db.execute("UPDATE channel_inbound SET receipt=NULL")
        before = dict(harness.store.db.execute("SELECT * FROM channel_inbound").fetchone())
        with pytest.raises(ChannelError, match="信封内容不一致") as caught:
            await harness.manager.receive(
                "telegram", replace(message, channel_reference=identified("6002"))
            )
        assert caught.value.status_code == 409
        assert dict(harness.store.db.execute("SELECT * FROM channel_inbound").fetchone()) == before
        assert len(harness.received) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("completed", [False, True])
async def test_old_reference_free_receipts_are_read_only_and_unfinished_rows_never_upgrade(
    tmp_path: Path, verified: bool, completed: bool
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), unused)
    original = IncomingMessage("legacy-event", "7", "7", "旧正文")
    try:
        receipt = await harness.manager.receive("telegram", original)
        with harness.store.db:
            if not verified:
                harness.store.db.execute("UPDATE channel_inbound SET fingerprint=NULL")
            if not completed:
                harness.store.db.execute("UPDATE channel_inbound SET receipt=NULL")
        before = dict(harness.store.db.execute("SELECT * FROM channel_inbound").fetchone())
        quoted = replace(original, channel_reference=identified())
        if completed:
            replay = await harness.manager.receive("telegram", quoted)
            assert replay == (receipt if verified else {**receipt, "fingerprint_verified": False})
            assert "channel_reference" not in replay
        else:
            with pytest.raises(ChannelError) as caught:
                await harness.manager.receive("telegram", quoted)
            assert caught.value.status_code == 409
        assert dict(harness.store.db.execute("SELECT * FROM channel_inbound").fetchone()) == before
        assert len(harness.received) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize("marker", [identified(), {"version": 1, "status": "unavailable"}])
async def test_real_webhook_preserves_canonical_archive_and_source_hint(
    host: tuple[Runtime, ChannelApplication], marker: dict[str, Any]
) -> None:
    runtime, application = host
    payload = native_payload()
    if marker["status"] == "unavailable":
        payload["message"]["external_reply"] = {}
    assert await application.manager.webhook(
        "telegram",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"X-Telegram-Bot-Api-Secret-Token": "FAKE_WEBHOOK"},
    ) == {"ok": True}
    row = runtime.store.db.execute("SELECT id,payload FROM ingress").fetchone()
    assert row is not None
    canonical = json.loads(row["payload"])
    main_id = runtime.main_session()["id"]
    assert canonical["session_id"] == main_id and canonical["principal"] == "owner"
    assert canonical["channel_reference"] == marker
    assert canonical["content"] == payload["message"]["text"]
    assert canonical["owner_text"] == {
        "version": 1,
        "kind": "channel",
        "chars": len(canonical["content"]),
    }
    assert canonical["route"]["reply_to"] == "11"
    assert "channel_reference" not in canonical["route"]
    assert "channel_reference" not in ReplyOrigin.from_input(canonical).dump()
    archived = runtime.store.claim_inputs(main_id, "actual-claim-run", limit=1)[0]
    assert archived["channel_reference"] == canonical["channel_reference"]
    assert archived["content"] == canonical["content"]
    supplied = next(
        message
        for message in runtime._history(main_id, pack=False)
        if message.source_message_id == row["id"]
    )
    header, _, body = supplied.content.partition("\n")
    prefix = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
    source = json.loads(header.removeprefix(prefix))
    assert source["channel_reference"] == marker and source["input_id"] == row["id"]
    assert "channel_history" in body and "section=reference" in body
    assert body.endswith(canonical["content"])
    assert "QUOTED_BODY" not in row["payload"] and "QUOTED_BODY" not in supplied.content
    assert not application.manager.deliveries()


@pytest.mark.parametrize("shape", ["legacy", "stable"])
@pytest.mark.parametrize("same_reference", [False, True])
async def test_canonical_recovery_compares_reference_without_backfill_or_second_input(
    host: tuple[Runtime, ChannelApplication], shape: str, same_reference: bool
) -> None:
    runtime, application = host
    incoming = application.manager.adapters["telegram"].webhook(native_payload())[0]
    first = await application.manager.receive("telegram", incoming)
    payload = persisted(runtime, first["message"]["id"])
    original_id = payload["id"]
    inbound_id = runtime.store.db.execute("SELECT id FROM channel_inbound").fetchone()["id"]
    if not same_reference:
        payload.pop("channel_reference")
    if shape == "legacy":
        payload["id"] = hashlib.sha256(
            f"{payload['session_id']}\0{payload['source']}\0{inbound_id}".encode()
        ).hexdigest()
    raw_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with runtime.store.db:
        runtime.store.db.execute(
            "UPDATE ingress SET id=?,payload=? WHERE id=?",
            (payload["id"], raw_payload, original_id),
        )
        runtime.store.db.execute(
            "UPDATE channel_inbound SET receipt=NULL WHERE id=?", (inbound_id,)
        )
    before = dict(runtime.store.db.execute("SELECT * FROM channel_inbound").fetchone())
    if same_reference:
        replay = await application.manager.receive("telegram", incoming)
        assert replay["duplicate"] is True
        assert replay["message"]["id"] == payload["id"]
        assert replay["message"]["seq"] == first["message"]["seq"]
        assert replay["message"]["channel_reference"] == identified()
    else:
        with pytest.raises(ValueError, match="不一致"):
            await application.manager.receive("telegram", incoming)
        assert dict(runtime.store.db.execute("SELECT * FROM channel_inbound").fetchone()) == before
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 1
    assert runtime.store.db.execute("SELECT payload FROM ingress").fetchone()[0] == raw_payload
    assert not application.manager.deliveries()


@pytest.mark.parametrize("entry", ["web", "scripted_source", "missing_event", "missing_guard"])
async def test_private_marker_cannot_bypass_real_verified_channel_admission(
    host: tuple[Runtime, ChannelApplication], entry: str
) -> None:
    runtime, application = host
    extra: dict[str, Any] = {"_channel_reference": identified()}
    if entry != "web":
        extra.update(
            {
                "source": "channel:telegram:123",
                "route": {
                    "name": "telegram",
                    "account_id": "123",
                    "sender_id": "7",
                    "target": "7",
                    "kind": "private",
                    "reply_to": "11",
                },
            }
        )
    if entry == "missing_event":
        extra.update({"event_id": "not-received", "_channel_event": "not-received"})
    if entry == "missing_guard":
        incoming = application.manager.adapters["telegram"].webhook(native_payload())[0]
        await application.manager.receive("telegram", incoming)
        inbound_id = runtime.store.db.execute("SELECT id FROM channel_inbound").fetchone()[0]
        with runtime.store.db:
            runtime.store.db.execute("UPDATE channel_inbound SET receipt=NULL")
        extra.update({"event_id": inbound_id, "_channel_event": inbound_id})
    count = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
    with pytest.raises(ValueError, match="渠道"):
        await runtime.submit("把这个改为一周，只给草稿", **extra)
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == count


async def test_shared_control_guard_rejects_even_direct_reference_with_valid_control_evidence(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), unused)
    try:
        message = IncomingMessage(
            "quoted-control",
            "7",
            "7",
            "/批准 invalid-ticket",
            control_text="/批准 invalid-ticket",
            control_account_id="123",
            channel_reference=identified(),
        )
        with pytest.raises(ChannelError, match="原始私聊纯文本") as caught:
            await harness.manager.receive("telegram", message)
        assert caught.value.status_code == 403
        assert harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 0
        assert not harness.received and not harness.manager.deliveries()
    finally:
        await harness.close()


async def test_stale_channel_binding_cannot_admit_new_reference(
    host: tuple[Runtime, ChannelApplication],
) -> None:
    runtime, application = host
    incoming = application.manager.adapters["telegram"].webhook(native_payload())[0]
    runtime.config.update({"channels": {"telegram": {"owner_ids": ["8"]}}})
    with pytest.raises(ValueError, match="配置或授权已改变"):
        await application.manager.receive("telegram", incoming)
    assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0


async def test_delegated_input_preserves_reference_but_hint_does_not_offer_resolution(
    host: tuple[Runtime, ChannelApplication],
) -> None:
    runtime, application = host
    await runtime.team.close()
    main_id = runtime.main_session()["id"]
    child = runtime.new_session("接管 Telegram 的队友")
    runtime.store.put("session", {**child, "kind": "teammate", "principal": "owner"})
    runtime.store.put(
        "agent",
        {
            "id": child["id"],
            "session_id": child["id"],
            "parent_id": main_id,
            "role": "worker",
            "status": "completed",
            "mode": child["mode"],
            "generation": 1,
        },
    )
    runtime.channel_ownership.control(
        main_id,
        {"action": "assign", "channel": "telegram", "scope": "account", "agent_id": child["id"]},
    )
    incoming = application.manager.adapters["telegram"].webhook(native_payload())[0]
    receipt = await application.manager.receive("telegram", incoming)
    canonical = persisted(runtime, receipt["message"]["id"])
    assert canonical["session_id"] == child["id"] and canonical["channel_reference"] == identified()
    assert "owner_text" not in canonical
    archive = runtime.store.claim_inputs(child["id"], "delegated-claim", limit=1)[0]
    assert archive["channel_reference"] == identified()
    supplied = next(
        message
        for message in runtime._history(child["id"], pack=False)
        if message.source_message_id == canonical["id"]
    )
    assert "当前委派会话暂不支持按引用定位" in supplied.content
    assert "section=reference" not in supplied.content
    assert not application.manager.deliveries()


async def test_reference_and_host_source_hint_survive_normal_close_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, application = pending_host(tmp_path, monkeypatch)
    try:
        incoming = application.manager.adapters["telegram"].webhook(native_payload())[0]
        receipt = await application.manager.receive("telegram", incoming)
        input_id = receipt["message"]["id"]
        main_id = runtime.main_session()["id"]
        runtime.store.claim_inputs(main_id, "first-claim", limit=1)
    finally:
        await application.close()
        await runtime.close()
    reopened, reopened_application = pending_host(tmp_path, monkeypatch)
    try:
        assert reopened.main_session()["id"] == main_id
        assert persisted(reopened, input_id)["channel_reference"] == identified()
        archive = reopened.store.get("message", input_id)
        assert archive is not None and archive["channel_reference"] == identified()
        supplied = next(
            message
            for message in reopened._history(main_id, pack=False)
            if message.source_message_id == input_id
        )
        assert "section=reference" in supplied.content and input_id in supplied.content
        assert "QUOTED_BODY" not in supplied.content
        assert not reopened_application.manager.deliveries()
    finally:
        await reopened_application.close()
        await reopened.close()
