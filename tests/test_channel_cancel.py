"""Exact outbox cancellation, dispatch arbitration and persistent terminal state."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_channels import Harness, profile, unused

from muli.channels import ChannelError, ChannelsConfig, FileContent


async def test_cancel_wins_against_old_dispatch_snapshot_and_survives_restart(
    tmp_path: Path,
) -> None:
    config = ChannelsConfig(onebot=profile(account_id="123"))
    harness = Harness(tmp_path, config, unused)
    identifier = ""
    try:
        item = await harness.manager.enqueue("onebot", "7", "待取消正文", event_id="stable")
        identifier = item["id"]
        raw_before = tuple(
            harness.store.db.execute(
                "SELECT route,payload,receipts,next_part FROM channel_outbox WHERE id=?",
                (identifier,),
            ).fetchone()
        )
        result = await harness.manager.cancel(
            identifier, name="onebot", owner_id="7", max_bytes=4096
        )
        assert result["id"] == identifier and result["state"] == "cancelled"
        assert result["cancelled"] is True
        assert result["confirmed_parts"] == 0 and result["total_parts"] == 1
        assert (
            await harness.manager.cancel(identifier, name="onebot", owner_id="7", max_bytes=4096)
            == result
        )
        assert (
            tuple(
                harness.store.db.execute(
                    "SELECT route,payload,receipts,next_part FROM channel_outbox WHERE id=?",
                    (identifier,),
                ).fetchone()
            )
            == raw_before
        )
        await harness.manager._send_item(item)
        same_key = await harness.manager.enqueue("onebot", "7", "待取消正文", event_id="stable")
        assert same_key["id"] == identifier and same_key["state"] == "cancelled"
        assert (await harness.manager.retry(identifier))["state"] == "cancelled"
        assert (await harness.manager.retry(identifier, allow_unknown=True))["state"] == "cancelled"
        async with asyncio.timeout(2):
            terminal = await harness.manager.wait(identifier)
        assert terminal is not None and terminal["state"] == "cancelled"
    finally:
        await harness.close()

    reopened = Harness(tmp_path, config, unused)
    try:
        await reopened.manager.start()
        async with asyncio.timeout(2):
            terminal = await reopened.manager.wait(identifier)
        assert terminal is not None and terminal["state"] == "cancelled"
        assert reopened.manager.delivery(identifier)["state"] == "cancelled"  # type: ignore[index]
        assert reopened.manager.status()["outbox"]["cancelled"] == 1
    finally:
        await reopened.close()


async def test_dispatch_claim_wins_and_cancel_does_not_rewrite_sending_or_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    entered, release = asyncio.Event(), asyncio.Event()
    sends: list[str] = []
    sending: asyncio.Task[None] | None = None

    async def send(
        route: dict[str, Any],
        text: str,
        file: FileContent | None,
        *,
        key: str,
        sequence: int,
    ) -> str:
        sends.append(key)
        entered.set()
        async with asyncio.timeout(2):
            await release.wait()
        return "confirmed-platform-receipt"

    monkeypatch.setattr(harness.manager.adapters["onebot"], "send", send)
    try:
        item = await harness.manager.enqueue("onebot", "7", "已领取正文")
        sending = asyncio.create_task(harness.manager._send_item(item))
        async with asyncio.timeout(2):
            await entered.wait()
        raw = tuple(
            harness.store.db.execute(
                "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
            ).fetchone()
        )
        result = await harness.manager.cancel(
            item["id"], name="onebot", owner_id="7", max_bytes=4096
        )
        assert result["cancelled"] is False and result["state"] == "sending"
        assert (
            tuple(
                harness.store.db.execute(
                    "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
                ).fetchone()
            )
            == raw
        )
        release.set()
        await sending
        saved = harness.manager.delivery(item["id"])
        assert saved is not None and saved["state"] == "sent"
        assert saved["receipts"] == ["confirmed-platform-receipt"] and len(sends) == 1
    finally:
        release.set()
        if sending is not None:
            await sending
        await harness.close()


@pytest.mark.parametrize(
    ("state", "next_part", "receipts"),
    [
        ("unknown", 0, []),
        ("sent", 2, ["a", "b"]),
        ("pending", 1, ["a"]),
        ("retry", 1, ["a"]),
        ("failed", 0, []),
        ("blocked", 0, []),
    ],
)
async def test_unsafe_or_nonqueued_state_is_reported_without_any_row_change(
    tmp_path: Path, state: str, next_part: int, receipts: list[str]
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    try:
        item = await harness.manager.enqueue("onebot", "7", "a" * 4001)
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET state=?,next_part=?,receipts=? WHERE id=?",
                (state, next_part, json.dumps(receipts), item["id"]),
            )
        before = tuple(
            harness.store.db.execute(
                "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
            ).fetchone()
        )
        result = await harness.manager.cancel(
            item["id"], name="onebot", owner_id="7", max_bytes=16384
        )
        assert result["cancelled"] is False and result["state"] == state
        assert result["confirmed_parts"] == next_part and result["total_parts"] == 2
        assert (
            tuple(
                harness.store.db.execute(
                    "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
                ).fetchone()
            )
            == before
        )
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "receipts", ["[", "null", "{}", '["confirmed"]', "[] trailing", "[NaN]", '"[]"']
)
async def test_bad_or_nonempty_zero_part_receipts_cannot_be_cancelled(
    tmp_path: Path, receipts: str
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    try:
        item = await harness.manager.enqueue("onebot", "7", "坏证据不能取消")
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET receipts=? WHERE id=?", (receipts, item["id"])
            )
        before = tuple(
            harness.store.db.execute(
                "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
            ).fetchone()
        )
        with pytest.raises(ChannelError):
            await harness.manager.cancel(item["id"], name="onebot", owner_id="7", max_bytes=4096)
        assert (
            tuple(
                harness.store.db.execute(
                    "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
                ).fetchone()
            )
            == before
        )
    finally:
        await harness.close()


async def test_unknown_manual_retry_can_be_cancelled_without_claiming_past_was_unsent(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    try:
        item = await harness.manager.enqueue("onebot", "7", "历史平台结果可能未知")
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET state='unknown',last_error='平台可能已接收' WHERE id=?",
                (item["id"],),
            )
        retried = await harness.manager.retry(item["id"], allow_unknown=True)
        assert retried["state"] == "pending" and retried["receipts"] == []
        result = await harness.manager.cancel(
            item["id"], name="onebot", owner_id="7", max_bytes=4096
        )
        assert result["state"] == "cancelled" and result["cancelled"] is True
        assert result["confirmed_parts"] == 0
        assert not any(
            word in json.dumps(result, ensure_ascii=False) for word in ("从未发送", "已撤回")
        )
    finally:
        await harness.close()


async def test_cancel_preserves_qq_reserved_sequences_and_uses_bounded_exact_point_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(qq=profile(account_id="123", app_id="123")), unused)
    try:
        item = await harness.manager.enqueue(
            "qq",
            "7",
            "原文",
            route={"target": "7", "sender_id": "7", "kind": "private", "reply_to": "origin"},
        )
        reservation = tuple(harness.store.db.execute("SELECT * FROM channel_state").fetchall())

        def unbounded_read(identifier: str) -> None:
            raise AssertionError("取消不能调用无字节边界的 delivery()")

        monkeypatch.setattr(harness.manager, "delivery", unbounded_read)
        result = await harness.manager.cancel(item["id"], name="qq", owner_id="7", max_bytes=4096)
        assert result["state"] == "cancelled"
        assert (
            tuple(harness.store.db.execute("SELECT * FROM channel_state").fetchall()) == reservation
        )
        row = harness.store.db.execute(
            "SELECT payload FROM channel_outbox WHERE id=?", (item["id"],)
        ).fetchone()
        assert row is not None and json.loads(row[0])["parts"][0]["sequence"] == 0
    finally:
        await harness.close()


async def test_publish_failure_does_not_undo_cancellation_or_hide_its_durable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(account_id="123")), unused)
    try:
        item = await harness.manager.enqueue("onebot", "7", "取消独立于事件通知")
        waiter = asyncio.create_task(harness.manager.wait(item["id"]))
        await asyncio.sleep(0)

        async def unavailable() -> None:
            raise RuntimeError("合成通知失败")

        monkeypatch.setattr(harness.manager, "_publish", unavailable)
        result = await harness.manager.cancel(
            item["id"], name="onebot", owner_id="7", max_bytes=4096
        )
        assert result["state"] == "cancelled" and result["cancelled"] is True
        row = harness.store.db.execute(
            "SELECT state FROM channel_outbox WHERE id=?", (item["id"],)
        ).fetchone()
        assert row is not None and row[0] == "cancelled"
        async with asyncio.timeout(2):
            waited = await waiter
        assert waited is not None and waited["state"] == "cancelled"
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "account",
        "group",
        "missing_account",
        "duplicate_route",
        "fractional_progress",
        "payload",
        "huge_receipts",
    ],
)
async def test_cancel_refuses_wrong_scope_and_bad_or_oversized_full_evidence(
    tmp_path: Path, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path, ChannelsConfig(onebot=profile(account_id="123", owner_ids=["7", "8"])), unused
    )
    try:
        item = await harness.manager.enqueue("onebot", "7", "核查全部证据")
        route = dict(item["route"])
        if change == "owner":
            route.update(sender_id="8", target="8")
        elif change == "account":
            route["account_id"] = "other-account"
        elif change == "group":
            route["kind"] = "group"
        elif change == "missing_account":
            route.pop("account_id")
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET route=? WHERE id=?", (json.dumps(route), item["id"])
            )
            if change == "duplicate_route":
                duplicate = json.dumps(route)[:-1] + ',"account_id":"123"}'
                harness.store.db.execute(
                    "UPDATE channel_outbox SET route=? WHERE id=?", (duplicate, item["id"])
                )
            elif change == "fractional_progress":
                harness.store.db.execute(
                    "UPDATE channel_outbox SET next_part=0.5 WHERE id=?", (item["id"],)
                )
            elif change == "payload":
                harness.store.db.execute(
                    "UPDATE channel_outbox SET payload=? WHERE id=?",
                    ('{"parts":[{"text":"x","attachment_id":"ambiguous"}]}', item["id"]),
                )
            elif change == "huge_receipts":
                harness.store.db.execute(
                    "UPDATE channel_outbox SET receipts=? WHERE id=?",
                    ('["' + "x" * 12000 + '"]', item["id"]),
                )
        before = tuple(
            harness.store.db.execute(
                "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
            ).fetchone()
        )

        def reject_full_decode(raw: bytes) -> dict[str, Any]:
            raise AssertionError("超过SQL字节预算的记录不能进入JSON解析")

        if change == "huge_receipts":
            monkeypatch.setattr("muli.channels.manager._object", reject_full_decode)
        with pytest.raises(ChannelError):
            await harness.manager.cancel(item["id"], name="onebot", owner_id="7", max_bytes=4096)
        assert (
            tuple(
                harness.store.db.execute(
                    "SELECT * FROM channel_outbox WHERE id=?", (item["id"],)
                ).fetchone()
            )
            == before
        )
    finally:
        await harness.close()
