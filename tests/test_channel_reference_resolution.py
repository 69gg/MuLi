"""Persisted input/receipt proof fixtures; these do not claim a platform send."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from typing import Any

import pytest
from test_channel_history_policy import delivery
from test_history_evidence import assert_code
from test_history_evidence import store as store

from muli.channel_references import ChannelReferences
from muli.conversation import Conversation
from muli.evidence import json_size
from muli.reply_origin import _ROUTE_FIELD_CHARS
from muli.store import Store

_LIMITS: dict[str, Any] = {
    "max_output_chars": 4000,
    "max_context_chars": 16000,
    "max_search_seconds": 10,
}


@pytest.fixture(autouse=True)
def channel_tables(store: Store) -> None:
    store.prepare_channels()


def original(
    journal: Store,
    platform_id: str | None = "6001",
    *,
    source: str = "channel:telegram:account",
    session_id: str = "session",
    state: str = "delivered",
    marker: object = None,
) -> dict[str, Any]:
    payload = Conversation.payload(
        session_id,
        "把这个改为一周，只给草稿。",
        source=source,
        principal="owner",
        mode="yolo",
        route={
            "name": "telegram",
            "account_id": "account",
            "sender_id": "7",
            "target": "7",
            "kind": "private",
            "reply_to": "7002",
        }
        if source.startswith("channel:")
        else None,
    )
    if marker is not None:
        payload["channel_reference"] = marker
    elif platform_id is not None and source.startswith("channel:"):
        payload["channel_reference"] = {
            "version": 1,
            "status": "identified",
            "message_id": platform_id,
        }
    result, inserted = journal.enqueue_input(payload, max_pending=100)
    assert inserted
    if state == "delivered":
        return journal.claim_inputs(session_id, "owner-round", limit=1)[0]
    if state == "cancelled":
        with journal.db:
            journal.db.execute("UPDATE ingress SET state='cancelled' WHERE id=?", (result["id"],))
    return result


def confirmed(
    journal: Store,
    *,
    message_ids: list[str] | None = None,
    parts: list[dict[str, Any]] | None = None,
    state: str = "sent",
    channel: str = "telegram",
    account: str = "account",
    owner: str = "7",
) -> str:
    receipts = message_ids if message_ids is not None else ["6001"]
    identifier = delivery(
        journal,
        channel=channel,
        account=account,
        owner=owner,
        state=state,
        confirmed=len(receipts),
        parts=parts or [{"text": "旧方案 A：蓝笔，每日二十分钟。"}],
    )
    with journal.db:
        journal.db.execute(
            "UPDATE channel_outbox SET receipts=? WHERE id=?",
            (json.dumps(receipts), identifier),
        )
    return identifier


def current_binding(name: str, account: str, owner: str) -> None:
    if (name, account, owner) != ("telegram", "account", "7"):
        raise PermissionError("当前账号或主人绑定已改变")


def read(
    journal: Store,
    target: dict[str, Any],
    *,
    current: dict[str, Any] | None = None,
    authorize: Callable[[str, str, str], None] = current_binding,
    **arguments: Any,
) -> dict[str, Any]:
    return ChannelReferences(journal).read(
        "session",
        input_id=target["id"],
        current_owner_input=lambda: current if current is not None else target,
        authorize_scope=authorize,
        **(_LIMITS | arguments),
    )


@pytest.mark.parametrize("selected", ["6001", "6002"])
def test_exact_native_reference_selects_a_or_b_with_identical_owner_text(
    store: Store,
    selected: str,
) -> None:
    first = confirmed(store)
    second = confirmed(
        store,
        message_ids=["6002"],
        parts=[{"text": "旧方案 B：红笔，每日四十分钟。"}],
    )
    target = original(store, selected)
    changes = store.db.total_changes
    page = read(store, target)
    assert page["delivery_id"] == (first if selected == "6001" else second)
    assert page["content"] == (
        "旧方案 A：蓝笔，每日二十分钟。" if selected == "6001" else "旧方案 B：红笔，每日四十分钟。"
    )
    assert page["section"] == "reference" and page["part"] == 0
    assert page["input_id"] == target["id"] and page["platform_message_id"] == selected
    assert page["evidence"] == "confirmed_text_part" and page["scope"] == "single_delivery_part"
    assert page["has_more"] is False and page["attachment_ids"] == []
    assert store.db.total_changes == changes and not store.db.in_transaction
    assert "7002" not in json.dumps(page)
    assert store.get("message", target["id"])["route"]["reply_to"] == "7002"


@pytest.mark.parametrize(("field", "value"), [("name", "onebot"), ("kind", "invalid")])
def test_invalid_route_cannot_exclude_a_second_receipt_from_scope(
    store: Store,
    field: str,
    value: str,
) -> None:
    confirmed(store)
    damaged = confirmed(store)
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET route=json_set(route,?,?) WHERE id=?",
            (f"$.{field}", value, damaged),
        )
    row = store.db.execute(
        "SELECT name,json_extract(route,'$.account_id'),"
        "json_extract(route,'$.sender_id'),json_extract(route,'$.target'),receipts "
        "FROM channel_outbox WHERE id=?",
        (damaged,),
    ).fetchone()
    assert tuple(row) == ("telegram", "account", "7", "7", '["6001"]')
    target = original(store)
    assert_code("channel_reference_search_incomplete", lambda: read(store, target))


@pytest.mark.parametrize("variant", ["sender_id", "target", "length"])
def test_malformed_private_route_cannot_exclude_a_second_receipt(
    store: Store,
    variant: str,
) -> None:
    confirmed(store)
    damaged = confirmed(store)
    sender = "8" if variant == "sender_id" else "7"
    target = "8" if variant == "target" else "7"
    if variant == "length":
        sender = target = "8" * (_ROUTE_FIELD_CHARS + 1)
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET route=json_set(route,'$.sender_id',?,'$.target',?) WHERE id=?",
            (sender, target, damaged),
        )
    row = store.db.execute(
        "SELECT name,json_extract(route,'$.account_id'),"
        "json_extract(route,'$.sender_id'),json_extract(route,'$.target'),"
        "json_extract(route,'$.kind'),receipts FROM channel_outbox WHERE id=?",
        (damaged,),
    ).fetchone()
    assert tuple(row) == ("telegram", "account", sender, target, "private", '["6001"]')
    if variant == "length":
        assert sender == target and len(sender) == _ROUTE_FIELD_CHARS + 1
    else:
        assert sender != target
    target_input = original(store)
    assert_code("channel_reference_search_incomplete", lambda: read(store, target_input))


def test_same_platform_id_in_other_accounts_owners_channels_or_groups_is_not_a_match(
    store: Store,
) -> None:
    expected = confirmed(store)
    confirmed(store, account="another-account")
    confirmed(store, owner="8")
    confirmed(store, channel="onebot")
    group = confirmed(store)
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET route=json_set(route,'$.kind','group') WHERE id=?",
            (group,),
        )
    target = original(store)
    assert read(store, target)["delivery_id"] == expected


def test_confirmed_earlier_part_remains_readable_after_later_part_becomes_unknown(
    store: Store,
) -> None:
    expected = confirmed(
        store,
        parts=[{"text": "确已确认的 A"}, {"text": "发送结果未知的 B"}],
        state="unknown",
    )
    page = read(store, original(store))
    assert page["delivery_id"] == expected and page["state"] == "unknown"
    assert page["confirmed_parts"] == 1 and page["total_parts"] == 2
    assert page["content"] == "确已确认的 A"
    unknown = original(store, "6002")
    assert_code("channel_reference_not_found", lambda: read(store, unknown))


@pytest.mark.parametrize("same_delivery", [False, True])
def test_duplicate_platform_receipts_never_choose_a_first_or_latest_hit(
    store: Store,
    same_delivery: bool,
) -> None:
    if same_delivery:
        confirmed(
            store,
            message_ids=["6001", "6001"],
            parts=[{"text": "第一段"}, {"text": "第二段"}],
        )
    else:
        confirmed(store)
        # The older duplicate is beyond a customary 20-row directory window.
        for index in range(25):
            confirmed(store, message_ids=[str(8000 + index)])
        confirmed(store)
    target = original(store)
    assert_code("channel_reference_ambiguous", lambda: read(store, target))


@pytest.mark.parametrize(
    "raw",
    [
        "[",
        "{}",
        "null",
        "[]",
        "[6001]",
        '["06001"]',
        '["6001",null]',
        '["6001",""]',
        '["6001","1e2"]',
        '["6001","\\u0000"]',
        '["9001\\u0000hidden"]',
    ],
)
def test_complete_valid_receipt_array_is_required_even_when_one_good_hit_exists(
    store: Store,
    raw: str,
) -> None:
    confirmed(store)
    damaged = confirmed(store, message_ids=["9001"])
    with store.db:
        store.db.execute("UPDATE channel_outbox SET receipts=? WHERE id=?", (raw, damaged))
    target = original(store)
    assert_code("channel_reference_search_incomplete", lambda: read(store, target))


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '{"name":"telegram"}',
        '{"name":"telegram","name":"other","account_id":"account",'
        '"sender_id":"7","target":"7","kind":"private"}',
    ],
)
def test_invalid_route_cannot_be_ignored_as_if_it_proved_another_scope(
    store: Store,
    raw: str,
) -> None:
    confirmed(store)
    damaged = confirmed(store, account="another-account")
    with store.db:
        store.db.execute("UPDATE channel_outbox SET route=? WHERE id=?", (raw, damaged))
    target = original(store)
    assert_code("channel_reference_search_incomplete", lambda: read(store, target))


@pytest.mark.parametrize("column", ["route", "receipts"])
def test_oversized_complete_metadata_stays_inside_sqlite_and_is_explicitly_incomplete(
    store: Store,
    column: str,
) -> None:
    identifier = confirmed(store)
    raw = (
        json.dumps({"padding": "x" * 100_000}) if column == "route" else json.dumps(["9" * 100_000])
    )
    with store.db:
        store.db.execute(f"UPDATE channel_outbox SET {column}=? WHERE id=?", (raw, identifier))
    target = original(store)
    assert_code("channel_reference_search_incomplete", lambda: read(store, target))


@pytest.mark.parametrize(
    ("parts", "code"),
    [
        ([{"attachment_id": "saved-file"}], "channel_reference_not_text"),
        ([{"text": "正文", "attachment_id": "saved-file"}], "channel_reference_not_text"),
        ([{"text": "正文", "unexpected": "extra"}], "channel_reference_not_text"),
        ([], "channel_reference_search_incomplete"),
    ],
)
def test_only_one_confirmed_pure_text_part_is_supported(
    store: Store,
    parts: list[dict[str, Any]],
    code: str,
) -> None:
    identifier = confirmed(store)
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET payload=? WHERE id=?",
            (json.dumps({"parts": parts}), identifier),
        )
    target = original(store)
    assert_code(code, lambda: read(store, target))


@pytest.mark.parametrize("raw", ["{", '{"parts":[],"parts":[{"text":"伪造"}]}'])
def test_malformed_payload_or_duplicate_parts_key_cannot_authenticate_a_text_part(
    store: Store,
    raw: str,
) -> None:
    identifier = confirmed(store)
    with store.db:
        store.db.execute("UPDATE channel_outbox SET payload=? WHERE id=?", (raw, identifier))
    target = original(store)
    assert_code("channel_reference_search_incomplete", lambda: read(store, target))


def test_unconfirmed_array_position_and_contradictory_sent_state_are_rejected(store: Store) -> None:
    identifier = confirmed(store)
    with store.db:
        store.db.execute("UPDATE channel_outbox SET next_part=0 WHERE id=?", (identifier,))
    target = original(store)
    assert_code("channel_reference_search_incomplete", lambda: read(store, target))
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET next_part=1,payload=? WHERE id=?",
            (json.dumps({"parts": [{"text": "已确认"}, {"text": "尚未确认"}]}), identifier),
        )
    assert_code("channel_reference_unavailable", lambda: read(store, target))


def test_long_body_pages_recheck_receipt_binding_input_and_body_revision(store: Store) -> None:
    text = "蓝笔🙂保留这份准确原文。\n" * 180
    identifier = confirmed(store, parts=[{"text": text}])
    target = original(store)
    page = read(store, target, max_output_chars=1100)
    assert page["has_more"] is True and json_size(page) <= 1100
    pages = [page]
    while pages[-1]["has_more"]:
        assert len(pages) < 20
        pages.append(
            read(
                store,
                target,
                offset_bytes=pages[-1]["next_offset_bytes"],
                revision=page["revision"],
                max_output_chars=1100,
            )
        )
    assert "".join(item["content"] for item in pages) == text
    assert len({item["revision"] for item in pages}) == 1
    assert all(json_size(item) <= 1100 for item in pages)

    def changed_binding(name: str, account: str, owner: str) -> None:
        assert (name, account, owner) == ("telegram", "account", "7")
        raise PermissionError("主人已解绑或账号已改变")

    continuation = {"offset_bytes": page["next_offset_bytes"], "revision": page["revision"]}
    with pytest.raises(PermissionError):
        read(store, target, authorize=changed_binding, **continuation)
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET receipts='[]',next_part=0,state='unknown' WHERE id=?",
            (identifier,),
        )
    assert_code("channel_reference_not_found", lambda: read(store, target, **continuation))
    with store.db:
        store.db.execute(
            "UPDATE channel_outbox SET receipts='[\"6001\"]',next_part=1,state='sent',payload=? WHERE id=?",
            (json.dumps({"parts": [{"text": text.replace("蓝笔", "红笔")}]}), identifier),
        )
    assert_code("evidence_changed", lambda: read(store, target, **continuation))


@pytest.mark.parametrize("changed", ["canonical", "archive", "archive_only"])
def test_canonical_and_archive_reference_presence_and_value_must_match(
    store: Store,
    changed: str,
) -> None:
    confirmed(store)
    target = original(store, None if changed == "archive_only" else "6001")
    marker = {"version": 1, "status": "identified", "message_id": "6002"}
    if changed == "canonical":
        with store.db:
            store.db.execute(
                "UPDATE ingress SET payload=json_set(payload,'$.channel_reference',json(?)) WHERE id=?",
                (json.dumps(marker), target["id"]),
            )
    else:
        store.put("message", {**target, "channel_reference": marker})
    assert_code(
        "channel_reference_unavailable" if changed == "archive_only" else "content_unavailable",
        lambda: read(store, target),
    )


@pytest.mark.parametrize(
    ("marker", "code"),
    [
        (None, "channel_reference_missing"),
        ({"version": 1, "status": "unavailable"}, "channel_reference_unavailable"),
        (
            {"version": True, "status": "identified", "message_id": "6001"},
            "channel_reference_unavailable",
        ),
    ],
)
def test_missing_unavailable_or_corrupt_marker_never_falls_back_to_latest(
    store: Store,
    marker: object,
    code: str,
) -> None:
    confirmed(store)
    target = original(store, None, marker=marker)
    assert_code(code, lambda: read(store, target))


def test_channel_round_can_only_read_its_own_input_but_actual_web_round_can_select_saved_input(
    store: Store,
) -> None:
    identifier = confirmed(store)
    earlier = original(store)
    current = original(store, "6002")
    with pytest.raises(PermissionError):
        read(store, earlier, current=current)
    web = original(store, None, source="web")
    assert read(store, earlier, current=web)["delivery_id"] == identifier
    for state in ("pending", "cancelled"):
        unpublished = original(store, state=state)
        assert_code("content_unavailable", partial(read, store, unpublished, current=web))


def test_other_session_or_delegated_input_never_acquires_main_reference_access(
    store: Store,
) -> None:
    confirmed(store)
    other = original(store, session_id="other")
    web = original(store, None, source="web")
    assert_code("content_unavailable", lambda: read(store, other, current=web))
    target = original(store)
    with store.db:
        store.db.execute(
            "UPDATE ingress SET payload=json_set(payload,'$.channel_ownership',json(?)) WHERE id=?",
            (
                json.dumps({"id": "lease", "epoch": 1, "main_id": "session", "agent_id": "child"}),
                target["id"],
            ),
        )
    assert_code("content_unavailable", lambda: read(store, target))


def test_sqlite_vm_budget_never_turns_a_partial_scan_into_unique_success(store: Store) -> None:
    identifier = confirmed(store)
    target = original(store)
    row = store.db.execute(
        "SELECT name,route,payload,state,next_part,created_at,updated_at FROM channel_outbox WHERE id=?",
        (identifier,),
    ).fetchone()
    with store.db:
        store.db.executemany(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_part,attempts,next_attempt,"
            "last_error,receipts,created_at,updated_at) VALUES(?,?,?,?,?,?,0,0,'',?,?,?)",
            [
                (f"bounded-scan-{index}", *tuple(row[:5]), '["9001"]', *tuple(row[5:]))
                for index in range(2000)
            ],
        )
    changes = store.db.total_changes
    assert_code("channel_reference_search_incomplete", lambda: read(store, target))
    assert store.db.total_changes == changes and not store.db.in_transaction
    assert store.db.execute("SELECT 1").fetchone()[0] == 1
    assert store._channel_reference_read_active is False


def test_time_budget_restores_busy_timeout_and_an_outer_transaction_is_never_taken_over(
    store: Store,
) -> None:
    confirmed(store)
    target = original(store)
    store.db.execute("PRAGMA busy_timeout=2345")
    assert_code(
        "channel_reference_search_incomplete", lambda: read(store, target, max_search_seconds=1e-9)
    )
    assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 2345
    assert not store.db.in_transaction and store._channel_reference_read_active is False
    store.db.execute("BEGIN")
    try:
        assert_code("channel_reference_transaction_active", lambda: read(store, target))
        assert store.db.in_transaction
    finally:
        store.db.rollback()
