"""Bound private owners are the only peers of the three bot channels."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from test_channels import Handler, Harness, onebot_body, onebot_event, profile

from muli.channels import ChannelError, ChannelProfile, ChannelsConfig, IncomingMessage, RemoteFile
from muli.channels.crypto import sign_qq
from muli.channels.manager import encoded
from muli.reply_origin import RouteError, authorize_route, channel_account
from muli.store import now

BotChannel = Literal["onebot", "telegram", "qq"]
CHANNELS: tuple[BotChannel, ...] = ("onebot", "telegram", "qq")


def owner_id(channel: BotChannel) -> str:
    return "owner-openid" if channel == "qq" else "7"


def group_id(channel: BotChannel) -> str:
    return "group-openid" if channel == "qq" else "99"


def binding(channel: BotChannel) -> ChannelProfile:
    return profile(
        owner_ids=[owner_id(channel)],
        allowed_groups=[group_id(channel)],
        receive_files=True,
        **({"app_id": "app", "client_secret": "FAKE_QQ_SECRET"} if channel == "qq" else {}),
    )


def webhook(
    channel: BotChannel,
    *,
    event: int,
    sender: str,
    group: bool = False,
    attachment: bool = False,
) -> tuple[bytes, dict[str, str]]:
    if channel == "onebot":
        payload = {
            **onebot_event(sender=int(sender), group=99 if group else None),
            "message_id": event,
        }
        if attachment:
            payload["message"].append(
                {"type": "file", "data": {"name": "private.txt", "url": "https://8.8.8.8/file"}}
            )
        return onebot_body(payload)
    if channel == "telegram":
        message: dict[str, Any] = {
            "message_id": event,
            "from": {"id": int(sender), "is_bot": False},
            "chat": {"id": 99 if group else int(sender), "type": "group" if group else "private"},
            "text": "真实渠道消息",
        }
        if attachment:
            message["document"] = {"file_id": "remote-file", "file_name": "private.txt"}
        return json.dumps({"update_id": event, "message": message}).encode(), {
            "X-Telegram-Bot-Api-Secret-Token": "FAKE_WEBHOOK"
        }
    message = {"id": str(event), "author": {"user_openid": sender}, "content": "真实渠道消息"}
    if group:
        message["group_openid"] = group_id(channel)
        message["author"] = {"member_openid": sender}
    if attachment:
        message["attachments"] = [
            {"url": "https://8.8.8.8/file", "filename": "private.txt", "content_type": "text/plain"}
        ]
    raw = json.dumps(
        {
            "id": str(event),
            "op": 0,
            "t": "GROUP_AT_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE",
            "d": message,
        }
    ).encode()
    timestamp = str(int(now()))
    return raw, {
        "X-Bot-Appid": "app",
        "X-Signature-Timestamp": timestamp,
        "X-Signature-Ed25519": sign_qq("FAKE_QQ_SECRET", timestamp.encode() + raw),
    }


def recording_http(calls: list[tuple[str, str]]) -> Handler:
    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "private.txt"}})
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "FAKE_TOKEN", "expires_in": "7200"})
        if request.method == "GET":
            return httpx.Response(200, content=b"private attachment")
        if request.url.path.endswith("sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"id": "sent"})
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    return handle


def inbound_snapshot(harness: Harness) -> tuple[tuple[Any, ...], ...]:
    # No message bodies need to be read to prove that rejection precedes staging.
    return tuple(
        tuple(harness.store.db.execute(sql).fetchone())
        for sql in (
            "SELECT count(*) FROM channel_inbound",
            "SELECT count(*) FROM ingress",
            "SELECT count(*) FROM objects",
            "SELECT count(*) FROM channel_state",
        )
    )


@pytest.mark.parametrize("channel", CHANNELS)
async def test_signed_webhooks_ignore_every_peer_except_the_bound_private_owner(
    tmp_path: Path, channel: BotChannel
) -> None:
    calls: list[tuple[str, str]] = []
    harness = Harness(
        tmp_path, ChannelsConfig(**{channel: binding(channel)}), recording_http(calls)
    )
    try:
        raw, headers = webhook(channel, event=100, sender=owner_id(channel))
        with pytest.raises(ChannelError) as rejected_auth:
            await harness.manager.webhook(channel, raw, {})
        assert rejected_auth.value.status_code == 401
        assert not harness.received
        await harness.manager.webhook(channel, raw, headers)
        assert len(harness.received) == 1
        accepted = harness.received[0]
        assert accepted["principal"] == "owner"
        assert accepted["route"]["kind"] == "private"
        assert accepted["route"]["sender_id"] == accepted["route"]["target"] == owner_id(channel)
        before = inbound_snapshot(harness)
        assert before[0] == (1,)
        assert calls == [] and not harness.attachments.list()
        for event, sender, group in (
            (101, owner_id(channel), True),
            (102, "8" if channel != "qq" else "stranger-openid", False),
            (103, "8" if channel != "qq" else "stranger-openid", True),
        ):
            raw, headers = webhook(
                channel, event=event, sender=sender, group=group, attachment=True
            )
            await harness.manager.webhook(channel, raw, headers)
            assert len(harness.received) == 1, "群消息和陌生私聊均不得提交或隔离"
            assert inbound_snapshot(harness) == before
            assert calls == [], "拒绝必须早于任何附件下载或平台请求"
            assert not harness.attachments.list()
    finally:
        await harness.close()


@pytest.mark.parametrize("channel", CHANNELS)
async def test_shared_route_gate_rejects_untrusted_peers_before_any_direct_receive_effect(
    tmp_path: Path, channel: BotChannel
) -> None:
    calls: list[tuple[str, str]] = []
    selected = binding(channel)
    harness = Harness(tmp_path, ChannelsConfig(**{channel: selected}), recording_http(calls))
    account = channel_account(channel, selected)
    valid = {"sender_id": owner_id(channel), "target": owner_id(channel), "kind": "private"}
    forbidden = (
        {"sender_id": owner_id(channel), "target": group_id(channel), "kind": "group"},
        {"sender_id": "8", "target": "8", "kind": "private"},
        {"sender_id": "8", "target": group_id(channel), "kind": "group"},
    )
    try:
        for outbound in (False, True):
            assert authorize_route(
                channel,
                valid,
                account_id=account,
                owner_ids=selected.owner_ids,
                allowed_groups=selected.allowed_groups,
                outbound=outbound,
            )["target"] == owner_id(channel)
            for route in forbidden:
                with pytest.raises(RouteError) as rejected:
                    authorize_route(
                        channel,
                        route,
                        account_id=account,
                        owner_ids=selected.owner_ids,
                        allowed_groups=selected.allowed_groups,
                        outbound=outbound,
                    )
                assert rejected.value.status_code == 403
        before = inbound_snapshot(harness)
        for index, route in enumerate(forbidden):
            message = IncomingMessage(
                f"direct-{index}",
                route["sender_id"],
                route["target"],
                "不能处理",
                kind="group" if route["kind"] == "group" else "private",
                files=(RemoteFile("private.txt", url="https://8.8.8.8/file"),),
                context_token="must-not-be-persisted",
            )
            with pytest.raises(ChannelError) as rejected_receive:
                await harness.manager.receive(channel, message)
            assert rejected_receive.value.status_code == 403
            assert not harness.received
            assert inbound_snapshot(harness) == before
            assert calls == [] and not harness.attachments.list()
    finally:
        await harness.close()


@pytest.mark.parametrize("channel", CHANNELS)
async def test_preexisting_group_and_stranger_outbox_cannot_bypass_the_private_owner_gate(
    tmp_path: Path, channel: BotChannel
) -> None:
    calls: list[tuple[str, str]] = []
    harness = Harness(
        tmp_path, ChannelsConfig(**{channel: binding(channel)}), recording_http(calls)
    )
    try:
        for index, route in enumerate(
            (
                {"sender_id": owner_id(channel), "target": group_id(channel), "kind": "group"},
                {"sender_id": "8", "target": "8", "kind": "private"},
            )
        ):
            queued = await harness.manager.enqueue(
                channel, owner_id(channel), "历史投递不能越过现授权", event_id=f"old-{index}"
            )
            # Keep a real persisted envelope, changing only the historical route.
            with harness.store.db:
                harness.store.db.execute(
                    "UPDATE channel_outbox SET route=? WHERE id=?",
                    (encoded({**queued["route"], **route}), queued["id"]),
                )
            old = harness.manager.delivery(queued["id"])
            assert old is not None and old["state"] == "pending"
            await harness.manager._send_item(old)
            blocked = harness.manager.delivery(queued["id"])
            assert blocked is not None and blocked["state"] == "blocked"
            assert blocked["next_part"] == 0
            assert blocked["receipts"] == [] and calls == []
    finally:
        await harness.close()
