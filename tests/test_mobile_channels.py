"""Raw channel evidence and durable acknowledgements; never use real accounts."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import Harness, profile, terminal, unused
from test_runtime import make_runtime
from test_task_briefs import attach_services

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelError, ChannelsConfig, IncomingMessage, RemoteFile
from muli.channels.adapters import OneBot, Telegram
from muli.channels.manager import encoded
from muli.channels.types import is_mobile_control_text
from muli.local_services import LocalServices
from muli.mobile_task_control import MobileControlSource, MobileTaskControl
from muli.runtime import Runtime
from muli.task_control import ControlConflict


def telegram_payload(text: str = "/任务") -> dict[str, Any]:
    return {
        "update_id": 10,
        "message": {
            "message_id": 11,
            "date": 1,
            "from": {"id": 7, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 7, "type": "private"},
            "text": text,
        },
    }


def onebot_payload(text: str = "/任务") -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": 123,
        "message_id": 10,
        "user_id": 7,
        "message": [{"type": "text", "data": {"text": text}}],
    }


@pytest.mark.parametrize("text", ["/任务", "/任务 bad", " /暂停 bad", "/调整"])
async def test_raw_private_control_candidates_keep_exact_text_and_real_account(text: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        for adapter, payload in (
            (Telegram(profile(), client), telegram_payload(text)),
            (OneBot(profile(account_id="123"), client), onebot_payload(text)),
        ):
            message = adapter.webhook(payload)[0]
            assert message.control_text == message.text == text
            assert message.control_account_id == "123"
            assert is_mobile_control_text(message.text)


@pytest.mark.parametrize(
    "extra",
    [
        {"caption": "/任务"},
        {"document": {"file_id": "file"}},
        {"voice": {}},
        {"photo": []},
        {"reply_to_message": {}},
        {"external_reply": {}},
        {"quote": {}},
        {"forward_origin": {}},
        {"forward_from": {}},
        {"via_bot": {}},
        {"business_connection_id": "connection"},
        {"sender_business_bot": {}},
        {"future_content": {}},
    ],
)
async def test_telegram_never_proves_quoted_forwarded_business_or_media_commands(
    extra: dict[str, Any],
) -> None:
    payload = telegram_payload()
    payload["message"].update(extra)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        message = Telegram(profile(), client).webhook(payload)[0]
        assert message.control_text == message.control_account_id == ""


@pytest.mark.parametrize("change", ["caption", "group", "bot_unknown", "account", "token"])
async def test_telegram_requires_original_user_private_text_and_token_account(change: str) -> None:
    payload = telegram_payload()
    configuration = profile()
    if change == "caption":
        payload["message"]["caption"] = payload["message"].pop("text")
    elif change == "group":
        payload["message"]["chat"] = {"id": 99, "type": "group"}
    elif change == "bot_unknown":
        del payload["message"]["from"]["is_bot"]
    elif change == "account":
        configuration = profile(account_id="different")
    else:
        configuration = profile(token="opaque-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        message = Telegram(configuration, client).webhook(payload)[0]
        assert message.control_text == message.control_account_id == ""


@pytest.mark.parametrize("kind", ["blockquote", "expandable_blockquote", "code", "pre"])
async def test_telegram_quote_or_code_display_cannot_supply_the_command_head(kind: str) -> None:
    payload = telegram_payload()
    payload["message"]["entities"] = [{"type": kind, "offset": 0, "length": 3}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        message = Telegram(profile(), client).webhook(payload)[0]
        assert message.control_text == message.control_account_id == ""


@pytest.mark.parametrize(
    "entity",
    [
        None,
        {},
        {"type": "bold", "offset": True, "length": 1},
        {"type": "code", "offset": -1, "length": 1},
        {"type": "code", "offset": 0, "length": False},
        {"type": "code", "offset": 0, "length": 100},
    ],
)
async def test_telegram_malformed_entities_do_not_prove_original_control(entity: object) -> None:
    payload = telegram_payload()
    payload["message"]["entities"] = [entity]
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        message = Telegram(profile(), client).webhook(payload)[0]
        assert message.control_text == message.control_account_id == ""


async def test_telegram_keeps_bold_urls_and_utf16_code_in_additional_requirements() -> None:
    text = "/调整 abc 😀只交草稿 https://example.invalid"
    prefix = "/调整 abc 😀"
    payload = telegram_payload(text)
    payload["message"]["entities"] = [
        {"type": "bold", "offset": 0, "length": 3},
        {"type": "code", "offset": len(prefix.encode("utf-16-le")) // 2, "length": 4},
        {
            "type": "url",
            "offset": len(text[: text.index("https")].encode("utf-16-le")) // 2,
            "length": len("https://example.invalid"),
        },
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        message = Telegram(profile(), client).webhook(payload)[0]
        assert message.control_text == text and message.control_account_id == "123"


@pytest.mark.parametrize(
    "change",
    [
        "fallback",
        "self_id",
        "self_id_type",
        "string",
        "reply",
        "record",
        "forward",
        "bad_text",
        "tail",
        "notice",
        "group",
    ],
)
async def test_onebot_requires_matching_explicit_account_and_complete_raw_text_array(
    change: str,
) -> None:
    payload = onebot_payload()
    configuration = profile(account_id="123")
    if change == "fallback":
        configuration = profile()
    elif change == "self_id":
        payload["self_id"] = 456
    elif change == "self_id_type":
        payload["self_id"] = "123"
    elif change == "string":
        payload["message"] = "/任务[CQ:record,file=private]"
    elif change in {"reply", "record", "forward"}:
        payload["message"].append({"type": change, "data": {}})
    elif change == "bad_text":
        payload["message"][0]["data"]["text"] = 123
    elif change == "tail":
        payload["message"] = payload["message"] * 64 + [{"type": "reply", "data": {}}]
    elif change == "notice":
        payload["post_type"] = "notice"
    else:
        payload.update(message_type="group", group_id=99)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unused)) as client:
        message = OneBot(configuration, client).webhook(payload)[0]
        assert message.control_text == message.control_account_id == ""


@pytest.mark.parametrize("name", ["telegram", "onebot"])
async def test_ordinary_adapter_messages_replay_old_pre_control_fingerprints(
    tmp_path: Path, name: str
) -> None:
    configuration = profile(account_id="123")
    harness = Harness(tmp_path, ChannelsConfig(**{name: configuration}), unused)
    try:
        payload = telegram_payload("普通聊天") if name == "telegram" else onebot_payload("普通聊天")
        message = harness.manager.adapters[name].webhook(payload)[0]
        assert message.control_text == message.control_account_id == ""
        legacy = asdict(message)
        legacy.pop("control_text")
        legacy.pop("control_account_id")
        key = hashlib.sha256(encoded([name, "123", message.event_id]).encode()).hexdigest()
        receipt = {"state": "pending", "id": "original"}
        with harness.store.db:
            harness.store.db.execute(
                "INSERT INTO channel_inbound(id,name,created_at,fingerprint,receipt) VALUES(?,?,1,?,?)",
                (key, name, hashlib.sha256(encoded(legacy).encode()).hexdigest(), encoded(receipt)),
            )
        assert await harness.manager.receive(name, message) == receipt
        assert not harness.received
    finally:
        await harness.close()


def control_message() -> IncomingMessage:
    return IncomingMessage(
        "control", "7", "7", "/任务", control_text="/任务", control_account_id="123"
    )


async def test_control_receipt_rechecks_authority_and_repairs_local_enqueue_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    calls: list[dict[str, Any]] = []
    allowed = True
    receipt = {"state": "ok", "reply_text": "任务列表", "mobile_task_control": {"version": 1}}

    async def control(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if not allowed:
            raise ChannelError("主人绑定已撤销", status_code=403)
        # Mirror only the core service's ownership of channel_inbound.receipt.
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_inbound SET receipt=? WHERE id=?",
                (encoded(receipt), kwargs["event_id"]),
            )
        return receipt

    harness.manager.control_submit = control
    enqueue = harness.manager.enqueue
    first = True

    async def full(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal first
        if first:
            first = False
            raise ChannelError("投递队列已满")
        return await enqueue(*args, **kwargs)

    monkeypatch.setattr(harness.manager, "enqueue", full)
    try:
        with pytest.raises(ChannelError, match="投递队列已满"):
            await harness.manager.receive("onebot", control_message())
        assert await harness.manager.receive("onebot", control_message()) == receipt
        assert calls[0]["receipt"] is None and calls[1]["receipt"] is None
        assert len(harness.manager.deliveries()) == 1
        assert not harness.received
        allowed = False
        with pytest.raises(ChannelError, match="撤销"):
            await harness.manager.receive("onebot", control_message())
        assert len(calls) == 3 and len(harness.manager.deliveries()) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize("change", ["unchanged", "body", "unknown", "corrupt", "mobile_marker"])
async def test_historical_command_receipt_cannot_be_upgraded_into_a_control_ticket(
    tmp_path: Path, change: str
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    message = control_message()
    key = hashlib.sha256(encoded(["onebot", "123", message.event_id]).encode()).hexdigest()
    envelope = asdict(message)
    envelope.pop("control_text")
    envelope.pop("control_account_id")
    receipt = encoded({"id": "old-owner-input", "state": "pending"})
    if change == "unknown":
        receipt = encoded({"unexpected": "shape"})
    elif change == "corrupt":
        receipt = "{"
    elif change == "mobile_marker":
        receipt = encoded(
            {"id": "old-owner-input", "state": "pending", "mobile_task_control": None}
        )
    with harness.store.db:
        harness.store.db.execute(
            "INSERT INTO channel_inbound(id,name,created_at,fingerprint,receipt) VALUES(?,?,1,?,?)",
            (key, "onebot", hashlib.sha256(encoded(envelope).encode()).hexdigest(), receipt),
        )
    original = dict(
        harness.store.db.execute("SELECT * FROM channel_inbound WHERE id=?", (key,)).fetchone()
    )
    if change == "body":
        message = replace(message, text="/任务 changed", control_text="/任务 changed")

    async def forbidden(**kwargs: Any) -> dict[str, Any]:
        pytest.fail("A historical chat receipt must never be upgraded into control")

    harness.manager.control_submit = forbidden
    try:
        with pytest.raises(ChannelError) as caught:
            await harness.manager.receive("onebot", message)
        assert caught.value.status_code == (409 if change == "body" else 403)
        current = dict(
            harness.store.db.execute("SELECT * FROM channel_inbound WHERE id=?", (key,)).fetchone()
        )
        assert current == original
        assert not harness.received and not harness.manager.deliveries()
    finally:
        await harness.close()


async def test_missing_control_service_never_falls_back_to_model(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    try:
        with pytest.raises(ChannelError, match="控制服务未启用"):
            await harness.manager.receive("onebot", control_message())
        assert not harness.received
        await harness.manager.receive("onebot", IncomingMessage("chat", "7", "7", "普通聊天"))
        assert len(harness.received) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "change", ["unproven", "account", "file", "owner", "group", "changed_proof"]
)
async def test_unproven_commands_never_download_or_reach_model_or_control(
    tmp_path: Path, change: str
) -> None:
    harness = Harness(
        tmp_path,
        ChannelsConfig(onebot=profile(account_id="123", receive_files=True, allowed_groups=["99"])),
        unused,
    )
    message = control_message()
    if change == "unproven":
        message = replace(message, control_text="", control_account_id="")
    elif change == "account":
        message = replace(message, control_account_id="456")
    elif change == "file":
        message = replace(message, files=(RemoteFile("private", url="https://cdn.invalid"),))
    elif change == "owner":
        message = replace(message, sender_id="8", target="8")
    elif change == "group":
        message = replace(message, kind="group", target="99")
    else:
        message = replace(message, control_text="/暂停 other")

    async def forbidden(**kwargs: Any) -> dict[str, Any]:
        pytest.fail("Unproven input must not reach control")

    harness.manager.control_submit = forbidden
    try:
        with pytest.raises(ChannelError):
            await harness.manager.receive("onebot", message)
        assert not harness.received and not harness.attachments.list()
    finally:
        await harness.close()


async def test_control_ack_unknown_is_not_automatically_retried(tmp_path: Path) -> None:
    attempts = 0

    async def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("unconfirmed", request=request)

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), timeout)
    receipt = {"reply_text": "已登记暂停", "mobile_task_control": {"version": 1}}

    async def control(**kwargs: Any) -> dict[str, Any]:
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_inbound SET receipt=? WHERE id=?",
                (encoded(receipt), kwargs["event_id"]),
            )
        return receipt

    harness.manager.control_submit = control
    try:
        await harness.manager.start()
        await harness.manager.receive("onebot", control_message())
        identifier = harness.manager.deliveries()[0]["id"]
        assert (await terminal(harness.manager, identifier))["state"] == "unknown"
        await harness.manager.receive("onebot", control_message())
        delivery = harness.manager.delivery(identifier)
        assert delivery is not None and delivery["state"] == "unknown"
        assert attempts == 1
    finally:
        await harness.close()


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("change", ["configuration", "generation", "adapter"])
async def test_application_final_guard_rejects_waiting_control_and_completed_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, replay: bool
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "owner_ids": ["7"],
                    "base_url": "https://platform.invalid",
                }
            }
        }
    )
    attachments = Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    application = ChannelApplication(runtime, attachments)
    entered = asyncio.Event()
    committed: list[str] = []
    receipt = {"reply_text": "任务列表", "mobile_task_control": {"version": 1}}

    async def submit(
        *,
        inbound_id: str,
        source: MobileControlSource,
        text: str,
        admission_guard: Callable[[], None],
    ) -> dict[str, Any]:
        assert source == MobileControlSource("onebot", "123", "7", "7")
        assert text == "/任务"
        entered.set()
        async with runtime.configuration_lock:
            admission_guard()
            committed.append(inbound_id)
            with runtime.store.db:
                runtime.store.db.execute(
                    "UPDATE channel_inbound SET receipt=? WHERE id=?",
                    (encoded(receipt), inbound_id),
                )
        return receipt

    monkeypatch.setattr(runtime, "submit_mobile_task_control", submit)
    operation: asyncio.Task[dict[str, Any]] | None = None
    try:
        if replay:
            await application.manager.receive("onebot", control_message())
            entered.clear()
        async with runtime.configuration_lock:
            operation = asyncio.create_task(
                application.manager.receive("onebot", control_message())
            )
            async with asyncio.timeout(2):
                await entered.wait()
            if change == "configuration":
                runtime.config.update({"channels": {"onebot": {"owner_ids": []}}})
            elif change == "generation":
                application._generation += 1
            else:
                manager = application.manager
                manager.adapters["onebot"] = OneBot(manager.config.onebot, manager.client)
        with pytest.raises(ValueError, match="配置或授权已改变"):
            await operation
        assert len(committed) == (1 if replay else 0)
        assert len(application.manager.deliveries()) == (1 if replay else 0)
        assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0
    finally:
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await application.close()
        await runtime.close()


async def test_closing_application_cancels_waiting_control_without_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "owner_ids": ["7"],
                    "base_url": "https://platform.invalid",
                }
            }
        }
    )
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    entered = asyncio.Event()

    async def submit(**kwargs: Any) -> dict[str, Any]:
        entered.set()
        async with runtime.configuration_lock:
            pytest.fail("A closed application's waiting control must not commit")

    monkeypatch.setattr(runtime, "submit_mobile_task_control", submit)
    operation: asyncio.Task[dict[str, Any]] | None = None
    try:
        async with runtime.configuration_lock:
            operation = asyncio.create_task(
                application.manager.receive("onebot", control_message())
            )
            async with asyncio.timeout(2):
                await entered.wait()
                await application.close()
            with pytest.raises(asyncio.CancelledError):
                await operation
        assert not application.manager.deliveries()
    finally:
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await application.close()
        await runtime.close()


def composed_control_application(
    tmp_path: Path,
) -> tuple[Runtime, LocalServices, ChannelApplication]:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "owner_ids": ["7"],
                    "base_url": "https://platform.invalid",
                }
            }
        }
    )
    services = attach_services(runtime, tmp_path)
    return runtime, services, ChannelApplication(runtime, services.attachments)


@pytest.mark.parametrize("case", ["bad-json", "non-object", "oversized", "invalid-utf8"])
async def test_application_real_runtime_rejects_corrupt_cached_control_with_stable_403(
    tmp_path: Path, case: str
) -> None:
    runtime, _, application = composed_control_application(tmp_path)
    manager = application.manager
    message = control_message()
    try:
        await manager.receive("onebot", message)
        row = runtime.store.db.execute("SELECT id FROM channel_inbound").fetchone()
        assert row is not None
        key = row["id"]
        raw = {
            "bad-json": b"{",
            "non-object": b"[]",
            "oversized": b" " * (runtime.config.settings.limits.max_output_chars * 4 + 1),
            "invalid-utf8": b"\x80",
        }[case]
        with runtime.store.db:
            runtime.store.db.execute(
                "UPDATE channel_inbound SET receipt=CAST(? AS TEXT) WHERE id=?", (raw, key)
            )
        for _ in range(2):
            with pytest.raises(ChannelError) as rejection:
                await manager.receive("onebot", message)
            assert rejection.value.status_code == 403
        assert (
            runtime.store.db.execute(
                "SELECT CAST(receipt AS BLOB) FROM channel_inbound WHERE id=?", (key,)
            ).fetchone()[0]
            == raw
        )
        assert len(manager.deliveries()) == 1
        assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0
        assert not runtime.tasks
    finally:
        await application.close()
        await runtime.close()


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("change", ["configuration", "generation", "adapter"])
async def test_application_real_runtime_final_revocation_is_403(
    tmp_path: Path, change: str, replay: bool
) -> None:
    runtime, _, application = composed_control_application(tmp_path)
    manager = application.manager
    message = control_message()
    waiting: asyncio.Task[dict[str, Any]] | None = None
    try:
        if replay:
            await manager.receive("onebot", message)
        async with runtime.configuration_lock:
            waiting = asyncio.create_task(manager.receive("onebot", message))
            await asyncio.sleep(0)
            assert not waiting.done()
            assert (
                runtime.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 1
            )
            if change == "configuration":
                runtime.config.update({"channels": {"onebot": {"owner_ids": []}}})
            elif change == "generation":
                application._generation += 1
            else:
                manager.adapters["onebot"] = OneBot(manager.config.onebot, manager.client)
        with pytest.raises(ChannelError) as rejection:
            await waiting
        assert rejection.value.status_code == 403
        assert len(manager.deliveries()) == (1 if replay else 0)
        assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0
        assert not runtime.tasks
    finally:
        if waiting is not None:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await application.close()
        await runtime.close()


@pytest.mark.parametrize("stage", ["storage", "notification"])
@pytest.mark.parametrize("error_kind", ["value", "conflict", "sqlite"])
async def test_application_real_runtime_does_not_hide_retryable_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, error_kind: str
) -> None:
    runtime, services, application = composed_control_application(tmp_path)
    item = await services.commitments.save(
        "保持同一暂停", brief={"goal": "核查", "constraints": []}
    )
    manager = application.manager
    status_message = control_message()
    failure = {
        "value": ValueError("ordinary infrastructure failure"),
        "conflict": ControlConflict("request_id_conflict", "ordinary infrastructure failure"),
        "sqlite": sqlite3.OperationalError("ordinary infrastructure failure"),
    }[error_kind]
    original_save = MobileTaskControl._save
    saves = notifications = 0

    def save(service: MobileTaskControl, inbound_id: str, receipt: dict[str, Any]) -> None:
        nonlocal saves
        saves += 1
        if stage == "storage" and saves == 1:
            raise failure
        original_save(service, inbound_id, receipt)

    async def notify() -> None:
        nonlocal notifications
        notifications += 1
        if stage == "notification" and notifications == 1:
            raise failure

    try:
        status = await manager.receive("onebot", status_message)
        assert status["mobile_task_control"]["tickets"][0]["commitment_id"] == item["id"]
        status_key = hashlib.sha256(
            encoded(["onebot", "123", status_message.event_id]).encode()
        ).hexdigest()
        text = f"/暂停 {status_key[:12]}-1"
        pause_message = replace(status_message, event_id="pause-once", text=text, control_text=text)
        monkeypatch.setattr(MobileTaskControl, "_save", save)
        monkeypatch.setattr(runtime, "_notify_task_control", notify)
        with pytest.raises(type(failure)) as caught:
            await manager.receive("onebot", pause_message)
        assert caught.value is failure
        assert services.task_control.current(item["id"])["control"]["epoch"] == 1
        assert len(manager.deliveries()) == 1
        receipt = await manager.receive("onebot", pause_message)
        assert receipt["mobile_task_control"]["kind"] == "pause"
        assert services.task_control.current(item["id"])["control"]["epoch"] == 1
        assert len(manager.deliveries()) == 2
        assert notifications == (1 if stage == "storage" else 2)
        assert runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == 0
        assert not runtime.tasks
    finally:
        await application.close()
        await runtime.close()
