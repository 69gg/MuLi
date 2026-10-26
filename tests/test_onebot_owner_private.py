"""OneBot accepts only its bound owner's point-to-point private inputs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import Harness, onebot_body, onebot_event, profile, unused

from muli.channels import ChannelError, ChannelsConfig, FileContent, IncomingMessage, RemoteFile
from muli.channels.manager import encoded


async def test_bound_owner_group_is_ignored_even_when_the_group_is_configured(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path,
        ChannelsConfig(onebot=profile(allowed_groups=["99"], receive_files=True)),
        unused,
    )
    try:
        private, private_headers = onebot_body(onebot_event())
        assert await harness.manager.webhook("onebot", private, private_headers) == {"ok": True}
        assert len(harness.received) == 1
        assert harness.received[0]["principal"] == "owner"
        assert harness.received[0]["route"]["kind"] == "private"
        before = harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0]
        assert before == 1

        group, group_headers = onebot_body(
            {**onebot_event(group=99), "message_id": "owner-group-must-not-be-admitted"}
        )
        assert await harness.manager.webhook("onebot", group, group_headers) == {"ok": True}
        assert len(harness.received) == 1, "主人群消息也不能进入模型或隔离队列"
        assert (
            harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == before
        )
        assert not harness.attachments.list()
    finally:
        await harness.close()


async def test_non_private_or_non_owner_events_never_stage_an_inbound_or_attachment(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path,
        ChannelsConfig(onebot=profile(allowed_groups=["99"], receive_files=True)),
        unused,
    )
    media = [{"type": "image", "data": {"file": "remote-image"}}]
    cases: list[dict[str, Any]] = [
        {**onebot_event(sender=8), "message": media},
        {**onebot_event(group=99), "message": media},
        {**onebot_event(), "message_type": "group", "message": media},
        {**onebot_event(), "message_type": "private", "group_id": 99},
        {**onebot_event(), "message_type": "private", "group_id": 0},
        {**onebot_event(), "post_type": "request", "request_type": "friend"},
        {**onebot_event(), "post_type": "notice", "notice_type": "friend_add"},
        {**onebot_event(), "post_type": "meta_event"},
        {
            **onebot_event(),
            "post_type": "notice",
            "notice_type": "group_upload",
            "file": {"id": "upload", "name": "private.txt", "size": 3},
        },
        {
            **onebot_event(sender=8),
            "post_type": "notice",
            "notice_type": "offline_file",
            "file": {"id": "stranger-file", "name": "private.txt", "size": 3},
        },
    ]
    try:
        for index, payload in enumerate(cases):
            raw, headers = onebot_body({**payload, "message_id": f"ignored-{index}"})
            assert await harness.manager.webhook("onebot", raw, headers) == {"ok": True}
            assert not harness.received, index
            assert (
                harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 0
            )
            assert not harness.attachments.list()
    finally:
        await harness.close()


@pytest.mark.parametrize("account_id", ["", "123"])
async def test_explicit_account_checks_self_id_without_requiring_a_new_ordinary_setting(
    tmp_path: Path, account_id: str
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id=account_id)), unused)
    try:
        accepted = 0
        for index, self_id in enumerate((None, 124, True, "123", 123)):
            payload = {**onebot_event(), "message_id": f"account-{index}"}
            if self_id is not None:
                payload["self_id"] = self_id
            raw, headers = onebot_body(payload)
            assert await harness.manager.webhook("onebot", raw, headers) == {"ok": True}
            accepted += int(not account_id or type(self_id) is int and self_id == 123)
            assert len(harness.received) == accepted
            assert (
                harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0]
                == accepted
            )
    finally:
        await harness.close()


async def test_owner_offline_file_is_saved_once_through_the_real_private_file_path(
    tmp_path: Path,
) -> None:
    body = b"owned-private-file\n"
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.url.path == "/get_private_file_url"
            assert json.loads(request.content) == {"file_id": "owned-file"}
            return httpx.Response(
                200,
                json={"status": "ok", "retcode": 0, "data": {"url": "https://8.8.8.8/owned"}},
            )
        assert request.method == "GET" and request.url.path == "/owned"
        return httpx.Response(200, content=body)

    harness = Harness(
        tmp_path,
        ChannelsConfig(onebot=profile(account_id="123", receive_files=True)),
        handler,
    )
    raw, headers = onebot_body(
        {
            "post_type": "notice",
            "notice_type": "offline_file",
            "self_id": 123,
            "user_id": 7,
            "time": 1234,
            "file": {"id": "owned-file", "name": "owned.txt", "size": len(body)},
        }
    )
    try:
        await harness.manager.webhook("onebot", raw, headers)
        await harness.manager.webhook("onebot", raw, headers)
        assert len(harness.received) == 1
        received = harness.received[0]
        assert received["principal"] == "owner"
        assert received["route"]["kind"] == "private"
        assert received["route"]["target"] == received["route"]["sender_id"] == "7"
        assert len(received["attachment_ids"]) == 1
        assert harness.attachments.path(received["attachment_ids"][0]).read_bytes() == body
        assert calls == [("POST", "/get_private_file_url"), ("GET", "/owned")]
    finally:
        await harness.close()


async def test_shared_onebot_route_gate_rejects_direct_input_and_old_group_outbox(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path, ChannelsConfig(onebot=profile(allowed_groups=["99"], receive_files=True)), unused
    )
    group_route = {"sender_id": "7", "target": "99", "kind": "group"}
    try:
        for message in (
            IncomingMessage("direct-stranger", "8", "8", "不接收", files=(RemoteFile("x"),)),
            IncomingMessage("direct-group", "7", "99", "不接收", kind="group"),
        ):
            with pytest.raises(ChannelError, match="OneBot"):
                await harness.manager.receive("onebot", message)
        assert not harness.received
        assert harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 0
        assert not harness.attachments.list()
        with pytest.raises(ChannelError, match="OneBot"):
            await harness.manager.enqueue("onebot", "99", "不发送", route=group_route)
        assert harness.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0

        queued = await harness.manager.enqueue("onebot", "7", "旧版群投递")
        # Preserve an actual outbox envelope, replacing only its route to model
        # a group delivery persisted by a previous version of the product.
        old_route = {**queued["route"], **group_route}
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET route=? WHERE id=?", (encoded(old_route), queued["id"])
            )
        old = harness.manager.delivery(queued["id"])
        assert old is not None and old["state"] == "pending"
        await harness.manager._send_item(old)
        blocked = harness.manager.delivery(queued["id"])
        assert blocked is not None and blocked["state"] == "blocked"
        assert blocked["next_part"] == 0
    finally:
        await harness.close()


async def test_telegram_also_rejects_groups_and_strangers_before_admission(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile(allowed_groups=["99"])), unused)
    try:
        for message in (
            IncomingMessage("group-owner", "7", "99", "原群消息", kind="group"),
            IncomingMessage("private-stranger", "8", "8", "原隔离输入"),
        ):
            with pytest.raises(ChannelError) as refused:
                await harness.manager.receive("telegram", message)
            assert refused.value.status_code == 403
        assert not harness.received
        assert harness.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == 0
        assert not harness.attachments.list()
        with pytest.raises(ChannelError) as refused:
            await harness.manager.enqueue(
                "telegram",
                "99",
                "原群回复",
                route={"sender_id": "7", "target": "99", "kind": "group"},
            )
        assert refused.value.status_code == 403
        assert harness.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
    finally:
        await harness.close()


async def test_owner_revoked_during_actual_attachment_read_blocks_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), unused)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"owned-file"

    original = harness.manager._file

    async def read_then_revoke(
        identifier: str, name: str, *, media_mode: str = "file"
    ) -> FileContent:
        file = await original(identifier, name, media_mode=media_mode)
        harness.manager.config = replace(
            harness.manager.config, onebot=replace(profile(), owner_ids=["8"])
        )
        return file

    try:
        attachment = await harness.attachments.save(chunks(), "owned.txt")
        queued = await harness.manager.enqueue("onebot", "7", "", [attachment["id"]])
        monkeypatch.setattr(harness.manager, "_file", read_then_revoke)
        await harness.manager._send_item(queued)
        blocked = harness.manager.delivery(queued["id"])
        assert blocked is not None and blocked["state"] == "blocked"
        assert blocked["next_part"] == 0 and not blocked["receipts"]
        assert harness.attachments.path(attachment["id"]).read_bytes() == b"owned-file"
    finally:
        await harness.close()
