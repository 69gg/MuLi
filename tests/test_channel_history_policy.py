"""Bounded local history readers; these fixtures do not claim platform history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_history_evidence import assert_code
from test_history_evidence import store as store

from muli.channel_history import ChannelHistory
from muli.channels.types import MAX_OUTGOING_TEXT_CHARS
from muli.evidence import json_size
from muli.history_evidence import HistoryEvidence
from muli.reply_origin import ReplyOrigin
from muli.store import Store, new_id, now


@pytest.fixture(autouse=True)
def channel_tables(store: Store) -> None:
    store.prepare_channels()


def incoming(
    journal: Store,
    *,
    channel: str = "onebot",
    account: str = "account",
    owner: str = "7",
    session: str = "session",
    **extra: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "source": f"channel:{channel}:{account}",
        "principal": "owner",
        "route": {
            "name": channel,
            "account_id": account,
            "sender_id": owner,
            "target": owner,
            "kind": "private",
            "reply_to": "actual-platform-input",
            "context_token": "PRIVATE-REPLY-CONTEXT",
        },
        **extra,
    }
    return journal.add_message(session, "user", "PRIVATE-OWNER-CONTENT", **fields)


def answer(journal: Store, original: dict[str, Any], **extra: Any) -> dict[str, Any]:
    origin = ReplyOrigin.from_input(original)
    assert origin.kind == "channel"
    return journal.complete_message(
        "session", "PRIVATE-ASSISTANT-CONTENT", **{"reply_origin": origin.dump(), **extra}
    )


def delivery(
    journal: Store,
    *,
    channel: str = "onebot",
    account: str = "account",
    owner: str = "7",
    state: str = "pending",
    confirmed: int = 0,
    parts: list[dict[str, Any]] | None = None,
    **payload_extra: Any,
) -> str:
    """Persist outbox fixtures for the reader; no platform-send claim is made."""
    identifier = new_id()
    route = {
        "name": channel,
        "account_id": account,
        "sender_id": owner,
        "target": owner,
        "kind": "private",
        "reply_to": "incoming-id-is-not-an-outgoing-platform-id",
        "context_token": "PRIVATE-OUTGOING-CONTEXT",
    }
    payload = {
        "parts": parts if parts is not None else [{"text": "PRIVATE-OUTGOING-TEXT"}],
        **payload_extra,
    }
    timestamp = now()
    with journal.db:
        journal.db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_part,attempts,"
            "next_attempt,last_error,receipts,created_at,updated_at) VALUES(?,?,?,?,?,?,0,0,'',?,?,?)",
            (
                identifier,
                channel,
                json.dumps(route),
                json.dumps(payload),
                state,
                confirmed,
                json.dumps(["PRIVATE-ADAPTER-RECEIPT"]),
                timestamp,
                timestamp,
            ),
        )
    return identifier


@pytest.mark.parametrize("channel", ["onebot", "telegram", "qq", "weixin"])
def test_only_exact_owner_inputs_and_actual_outbox_deliveries_are_disclosed(
    store: Store, channel: str
) -> None:
    original = incoming(store, channel=channel, attachment_ids=["local:opaque-reference"])
    reply = answer(store, original)
    other_owner = incoming(store, channel=channel, owner="8")
    incoming(store, channel=channel, account="another-account")
    outside = incoming(store, channel=channel, session="other")
    # A shared source or a copied route cannot authenticate a different input.
    for referenced in (other_owner, outside):
        answer(
            store,
            original,
            reply_origin={**reply["reply_origin"], "input_id": referenced["id"]},
        )
    store.add_message("session", "assistant", "NO-ORIGIN", source=f"channel:{channel}:account")
    incoming(store, channel=channel, principal="external:unverified")
    sent = delivery(store, channel=channel, state="sent", confirmed=1)
    unknown = delivery(
        store,
        channel=channel,
        state="unknown",
        confirmed=1,
        parts=[{"text": "PRIVATE-OUTGOING-TEXT"}, {"attachment_id": "opaque-file"}],
    )
    blocked = delivery(store, channel=channel, state="blocked", automatic=True)
    delivery(store, channel=channel, owner="8")
    delivery(store, channel=channel, account="another-account")

    page = ChannelHistory(store).read(channel, "account", "7", "session", max_output_chars=6000)
    assert page["coverage"] == "saved_by_muli" and page["consistency"] == "live"
    inputs, outputs = page["incoming"], page["outgoing"]
    assert [item["id"] for item in inputs["items"]] == [original["id"]]
    assert [item["seq"] for item in inputs["items"]] == [original["seq"]]
    assert inputs["omitted"] == 7 and inputs["has_more"] is False
    first = inputs["items"][0]
    assert first["source"] == f"channel:{channel}:account" and first["principal"] == "owner"
    assert first["revision"] == original["evidence_revision"]
    assert first["attachment_ids"] == ["local:opaque-reference"]
    assert first["platform_message_id"] == "actual-platform-input"
    assert [item["id"] for item in outputs["items"]] == [sent, unknown, blocked]
    assert [item["state"] for item in outputs["items"]] == ["sent", "unknown", "blocked"]
    assert [item["confirmed_parts"] for item in outputs["items"]] == [1, 1, 0]
    assert [item["total_parts"] for item in outputs["items"]] == [1, 2, 1]
    assert outputs["items"][1]["attachment_ids"] == ["opaque-file"]
    assert outputs["omitted"] == 2 and outputs["has_more"] is False
    assert all(item["kind"] == "delivery" for item in outputs["items"])
    assert all("platform_message_id" not in item for item in outputs["items"])
    assert all(item["created_at"] > 0 for item in [first, *outputs["items"]])
    encoded = json.dumps(page, ensure_ascii=False)
    assert "PRIVATE-" not in encoded and "context_token" not in encoded
    assert all("content" not in item and "route" not in item for item in [first, *outputs["items"]])
    body = HistoryEvidence(store).read_content(
        "session", message_id=first["id"], revision=first["revision"], max_output_chars=2000
    )
    assert body["content"] == original["content"]
    outgoing_body = ChannelHistory(store).read_delivery(
        channel, "account", "7", "session", delivery_id=sent, max_output_chars=2000
    )
    assert outgoing_body["content"] == "PRIVATE-OUTGOING-TEXT"
    assert outgoing_body["state"] == "sent" and outgoing_body["confirmed_parts"] == 1


def test_bad_candidates_consume_an_empty_window_and_deleted_anchor_still_advances(
    store: Store,
) -> None:
    older = incoming(store)
    orphan = store.add_message("session", "user", "orphan")
    assert store.delete("message", orphan["id"])
    store.add_message("session", "tool", "TOOL-RESULT-MUST-NOT-LEAK")
    reader = ChannelHistory(store)
    first = reader.read(
        "onebot",
        "account",
        "7",
        "session",
        direction="incoming",
        limit=2,
        max_output_chars=2000,
    )["incoming"]
    assert first["items"] == [] and first["omitted"] == 2
    assert first["has_more"] is True and isinstance(first["next_cursor"], str)
    with store.db:
        store.db.execute("DELETE FROM message_order WHERE message_id=?", (orphan["id"],))
    tail = reader.read(
        "onebot",
        "account",
        "7",
        "session",
        direction="incoming",
        cursor=first["next_cursor"],
        limit=2,
        max_output_chars=2000,
    )["incoming"]
    assert [item["id"] for item in tail["items"]] == [older["id"]]
    assert tail["omitted"] == 0 and tail["has_more"] is False
    assert tail["next_cursor"] is None


def test_cursor_is_bound_to_database_main_channel_account_and_owner(
    store: Store, tmp_path: Path
) -> None:
    incoming(store)
    incoming(store)
    reader = ChannelHistory(store)
    page = reader.read(
        "onebot",
        "account",
        "7",
        "session",
        direction="incoming",
        limit=1,
        max_output_chars=2000,
    )["incoming"]
    cursor = page["next_cursor"]
    assert isinstance(cursor, str) and page["has_more"]
    for scope in (
        ("weixin", "account", "7", "session"),
        ("onebot", "another-account", "7", "session"),
        ("onebot", "account", "8", "session"),
        ("onebot", "account", "7", "other"),
    ):
        assert_code(
            "record_cursor_invalid",
            lambda scope=scope: reader.read(
                *scope,
                direction="incoming",
                cursor=cursor,
                max_output_chars=2000,
            ),
        )
    assert_code(
        "record_cursor_invalid",
        lambda: reader.read(
            "onebot",
            "account",
            "7",
            "session",
            direction="outgoing",
            cursor=cursor,
            max_output_chars=2000,
        ),
    )
    assert_code(
        "invalid_arguments",
        lambda: reader.read(
            "onebot", "account", "7", "session", cursor=cursor, max_output_chars=2000
        ),
    )
    other = Store(tmp_path / "other-history.sqlite3")
    try:
        assert_code(
            "record_cursor_invalid",
            lambda: ChannelHistory(other).read(
                "onebot",
                "account",
                "7",
                "session",
                direction="incoming",
                cursor=cursor,
                max_output_chars=2000,
            ),
        )
    finally:
        other.close()


def test_full_page_and_cursor_budget_never_drops_or_truncates_a_valid_item(
    store: Store,
) -> None:
    account = '账号"\\\n🙂' * 4
    incoming(store, account=account)
    latest = incoming(store, account=account, attachment_ids=['opaque:"\\\n🙂'])
    delivery(store, account=account)
    newest_delivery = delivery(store, account=account)
    reader = ChannelHistory(store)
    page = reader.read("onebot", account, "7", "session", limit=1, max_output_chars=4000)
    assert [item["id"] for item in page["incoming"]["items"]] == [latest["id"]]
    assert [item["id"] for item in page["outgoing"]["items"]] == [newest_delivery]
    assert all(
        page[stream]["has_more"] and page[stream]["next_cursor"]
        for stream in ("incoming", "outgoing")
    )
    exact = json_size(page)
    assert reader.read("onebot", account, "7", "session", limit=1, max_output_chars=exact) == page
    assert_code(
        "output_budget",
        lambda: reader.read("onebot", account, "7", "session", limit=1, max_output_chars=exact - 1),
    )


def test_sql_projection_has_no_body_provider_data_or_context_and_never_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = incoming(
        store,
        images=[{"data": "I" * 100_000}],
        provider_data={"secret": "P" * 100_000},
        tool_calls=[{"arguments": "PRIVATE-TOOL-ARGUMENTS"}],
    )
    answer(store, original, provider_data={"secret": "A" * 100_000})
    rows = list(
        store.channel_history_rows(
            "session",
            name="onebot",
            account_id="account",
            owner_id="7",
            before_seq=None,
            limit=3,
            metadata_bytes=64,
        )
    )
    assert len(rows) == 2 and all(row["content"] == b"" for row in rows)
    assert all("data" not in row and "owner_data" not in row for row in rows)
    assert all(
        len(value) <= 64 for row in rows for value in row.values() if isinstance(value, bytes)
    )
    changes = store.db.total_changes

    def no_json_loads(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("渠道元目录不得在 Python 解码整条消息或私有路由")

    with monkeypatch.context() as scoped:
        scoped.setattr(json, "loads", no_json_loads)
        page = ChannelHistory(store).read(
            "onebot", "account", "7", "session", max_output_chars=2000
        )
    assert [item["id"] for item in page["incoming"]["items"]] == [original["id"]]
    assert page["outgoing"]["items"] == []
    assert store.db.total_changes == changes
    encoded = json.dumps(page, ensure_ascii=False)
    assert all(secret not in encoded for secret in ("PRIVATE-", "provider_data", "tool_calls"))
    body = HistoryEvidence(store).read_content(
        "session",
        message_id=original["id"],
        revision=page["incoming"]["items"][0]["revision"],
        max_output_chars=1000,
    )
    assert body["content"] == original["content"]
    assert store.db.total_changes == changes


def test_outgoing_empty_window_consumes_foreign_rows_and_survives_deleted_anchor(
    store: Store,
) -> None:
    oldest = delivery(store)
    foreign = delivery(store, owner="8")
    delivery(store, state="invalid-state")
    reader = ChannelHistory(store)
    page = reader.read(
        "onebot",
        "account",
        "7",
        "session",
        direction="outgoing",
        limit=2,
        max_output_chars=2000,
    )
    assert page["incoming"] is None
    first = page["outgoing"]
    assert first["items"] == [] and first["omitted"] == 2
    assert first["has_more"] and isinstance(first["next_cursor"], str)
    with store.db:
        store.db.execute("DELETE FROM channel_outbox WHERE id=?", (foreign,))
    tail = reader.read(
        "onebot",
        "account",
        "7",
        "session",
        direction="outgoing",
        cursor=first["next_cursor"],
        limit=2,
        max_output_chars=2000,
    )["outgoing"]
    assert [item["id"] for item in tail["items"]] == [oldest]
    assert tail["omitted"] == 0 and tail["next_cursor"] is None and not tail["has_more"]


def test_cancelled_delivery_remains_in_exact_owner_history_with_zero_confirmations(
    store: Store,
) -> None:
    identifier = delivery(
        store,
        state="cancelled",
        parts=[{"text": "保存的取消前正文"}, {"attachment_id": "cancelled-file"}],
    )
    with store.db:
        store.db.execute("UPDATE channel_outbox SET receipts='[]' WHERE id=?", (identifier,))
    delivery(store, owner="8", state="cancelled")
    delivery(store, state="future-state")
    changes = store.db.total_changes
    page = ChannelHistory(store).read(
        "onebot",
        "account",
        "7",
        "session",
        direction="outgoing",
        max_output_chars=2000,
    )["outgoing"]
    assert [item["id"] for item in page["items"]] == [identifier]
    item = page["items"][0]
    assert item["state"] == "cancelled"
    assert item["confirmed_parts"] == 0 and item["total_parts"] == 2
    assert item["attachment_ids"] == ["cancelled-file"]
    assert page["omitted"] == 2 and not page["has_more"] and page["next_cursor"] is None
    assert "content" not in item and "receipts" not in item
    assert store.db.total_changes == changes


def test_cancelled_body_keeps_exact_utf8_revision_and_saved_attachment_without_writes(
    store: Store,
) -> None:
    text = "取消后仍可核查😀" * 200
    identifier = delivery(
        store,
        parts=[{"text": text}, {"attachment_id": "cancelled-file"}],
    )
    reader = ChannelHistory(store)
    first = reader.read_delivery(
        "onebot", "account", "7", "session", delivery_id=identifier, max_output_chars=1000
    )
    assert first["state"] == "pending" and first["has_more"]
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET state='cancelled',receipts='[]' WHERE id=?",
            (identifier,),
        )
    changes = store.db.total_changes
    observed = first["content"]
    page = first
    for _ in range(20):
        if not page["has_more"]:
            break
        page = reader.read_delivery(
            "onebot",
            "account",
            "7",
            "session",
            delivery_id=identifier,
            offset_bytes=page["next_offset_bytes"],
            revision=first["revision"],
            max_output_chars=1000,
        )
        assert page["state"] == "cancelled" and page["confirmed_parts"] == 0
        assert page["total_parts"] == 2 and page["revision"] == first["revision"]
        observed += page["content"]
    assert not page["has_more"] and observed == text
    attachment = reader.read_delivery(
        "onebot",
        "account",
        "7",
        "session",
        delivery_id=identifier,
        part=1,
        max_output_chars=1000,
    )
    assert attachment["state"] == "cancelled" and attachment["confirmed_parts"] == 0
    assert attachment["total_parts"] == 2 and attachment["attachment_ids"] == ["cancelled-file"]
    assert attachment["content"] == "" and attachment["total_bytes"] == 0
    assert not attachment["has_more"] and store.db.total_changes == changes


def test_unrecognised_delivery_state_still_cannot_be_read_as_history_or_body(store: Store) -> None:
    identifier = delivery(store, state="future-state")
    reader = ChannelHistory(store)
    page = reader.read(
        "onebot", "account", "7", "session", direction="outgoing", max_output_chars=2000
    )["outgoing"]
    assert page["items"] == [] and page["omitted"] == 1
    assert_code(
        "content_unavailable",
        lambda: reader.read_delivery(
            "onebot", "account", "7", "session", delivery_id=identifier, max_output_chars=1000
        ),
    )


def test_delivery_body_keeps_utf8_and_content_revision_across_send_state_changes(
    store: Store,
) -> None:
    text = "甲🙂" * 10_000  # UTF-8 deliberately crosses the fixed 64KiB SQL boundary.
    identifier = delivery(
        store,
        state="unknown",
        confirmed=1,
        parts=[{"text": text}, {"attachment_id": "opaque-file"}, {"text": "other part"}],
    )
    reader = ChannelHistory(store)
    first = reader.read_delivery(
        "onebot",
        "account",
        "7",
        "session",
        delivery_id=identifier,
        max_output_chars=800,
    )
    assert first["state"] == "unknown" and first["confirmed_parts"] == 1
    assert first["has_more"] and first["next_offset_bytes"] > 0
    assert first["content"].encode("utf-8") == text.encode("utf-8")[: first["next_offset_bytes"]]
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET state='sent',next_part=3,updated_at=?,"
            "payload=json_set(payload,'$.parts[2].text',?) WHERE id=?",
            (now(), "another unchanged-width part", identifier),
        )
    following = reader.read_delivery(
        "onebot",
        "account",
        "7",
        "session",
        delivery_id=identifier,
        offset_bytes=first["next_offset_bytes"],
        revision=first["revision"],
        max_output_chars=800,
    )
    assert following["state"] == "sent" and following["confirmed_parts"] == 3
    assert following["revision"] == first["revision"]
    assert (
        following["content"].encode("utf-8")
        == text.encode("utf-8")[first["next_offset_bytes"] : following["next_offset_bytes"]]
    )
    assert_code(
        "invalid_offset",
        lambda: reader.read_delivery(
            "onebot",
            "account",
            "7",
            "session",
            delivery_id=identifier,
            offset_bytes=1,
            revision=first["revision"],
            max_output_chars=800,
        ),
    )
    assert_code(
        "invalid_arguments",
        lambda: reader.read_delivery(
            "onebot",
            "account",
            "7",
            "session",
            delivery_id=identifier,
            offset_bytes=first["next_offset_bytes"],
            max_output_chars=800,
        ),
    )
    attachment = reader.read_delivery(
        "onebot",
        "account",
        "7",
        "session",
        delivery_id=identifier,
        part=1,
        max_output_chars=1000,
    )
    assert attachment["content"] == "" and attachment["total_bytes"] == 0
    assert attachment["attachment_ids"] == ["opaque-file"] and not attachment["has_more"]
    exact = json_size(attachment)
    assert (
        reader.read_delivery(
            "onebot",
            "account",
            "7",
            "session",
            delivery_id=identifier,
            part=1,
            max_output_chars=exact,
        )
        == attachment
    )
    assert_code(
        "output_budget",
        lambda: reader.read_delivery(
            "onebot",
            "account",
            "7",
            "session",
            delivery_id=identifier,
            part=1,
            max_output_chars=exact - 1,
        ),
    )
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET payload=json_set(payload,'$.parts[0].text',?) WHERE id=?",
            (text.replace("甲", "乙", 1), identifier),
        )
    assert_code(
        "evidence_changed",
        lambda: reader.read_delivery(
            "onebot",
            "account",
            "7",
            "session",
            delivery_id=identifier,
            offset_bytes=first["next_offset_bytes"],
            revision=first["revision"],
            max_output_chars=800,
        ),
    )


def test_delivery_sql_projection_is_bounded_scoped_read_only_and_never_exports_payload(
    store: Store,
) -> None:
    text = "甲🙂" * 10_000
    identifier = delivery(
        store,
        parts=[{"text": text}, {"text": "PRIVATE-OTHER-PART" * 20_000}],
        private_data="PRIVATE-PAYLOAD-EXTRA" * 10_000,
    )
    changes = store.db.total_changes
    rows = list(
        store.channel_delivery_slices(
            name="onebot",
            account_id="account",
            owner_id="7",
            delivery_id=identifier,
            part=0,
            chunk_bytes=64 * 1024,
            source_bytes=MAX_OUTGOING_TEXT_CHARS * 4,
            metadata_bytes=64,
        )
    )
    assert len(rows) == 2
    assert b"".join(row["content"] for row in rows) == text.encode("utf-8")
    assert all(
        len(value) <= 64 * 1024
        for row in rows
        for value in row.values()
        if isinstance(value, bytes)
    )
    assert all(
        field not in row
        for row in rows
        for field in ("payload", "part_data", "route", "route_data", "receipts", "context_token")
    )
    directory_rows = list(
        store.channel_delivery_rows(
            name="onebot",
            account_id="account",
            owner_id="7",
            before_rowid=None,
            limit=2,
            metadata_bytes=64,
        )
    )
    assert all(
        len(value) <= 64
        for row in directory_rows
        for value in row.values()
        if isinstance(value, bytes)
    )
    assert "content" not in directory_rows[0] and "payload" not in directory_rows[0]
    reader = ChannelHistory(store)
    body = reader.read_delivery(
        "onebot",
        "account",
        "7",
        "session",
        delivery_id=identifier,
        max_output_chars=1000,
    )
    assert text.startswith(body["content"]) and body["has_more"]
    encoded = json.dumps(body, ensure_ascii=False)
    assert "PRIVATE-" not in encoded and "context_token" not in encoded
    for channel, account, owner in (
        ("weixin", "account", "7"),
        ("onebot", "other-account", "7"),
        ("onebot", "account", "8"),
    ):
        assert_code(
            "evidence_not_found",
            lambda channel=channel, account=account, owner=owner: reader.read_delivery(
                channel,
                account,
                owner,
                "session",
                delivery_id=identifier,
                max_output_chars=1000,
            ),
        )
    assert store.db.total_changes == changes


def test_outbox_attachments_use_complete_page_budget_and_body_enforces_original_source_cap(
    store: Store,
) -> None:
    identifiers = [f"opaque-file-{index}" for index in range(33)]
    identifier = delivery(
        store,
        state="sent",
        confirmed=len(identifiers),
        parts=[{"attachment_id": item} for item in identifiers],
    )
    reader = ChannelHistory(store)
    page = reader.read(
        "onebot",
        "account",
        "7",
        "session",
        direction="outgoing",
        max_output_chars=4000,
    )
    assert page["outgoing"]["items"][0]["attachment_ids"] == identifiers
    attachment = reader.read_delivery(
        "onebot",
        "account",
        "7",
        "session",
        delivery_id=identifier,
        part=32,
        max_output_chars=1000,
    )
    assert attachment["attachment_ids"] == [identifiers[-1]] and attachment["content"] == ""
    oversized = delivery(store, parts=[{"text": "🙂" * (MAX_OUTGOING_TEXT_CHARS + 1)}])
    assert_code(
        "content_unavailable",
        lambda: reader.read_delivery(
            "onebot",
            "account",
            "7",
            "session",
            delivery_id=oversized,
            max_output_chars=1000,
        ),
    )
