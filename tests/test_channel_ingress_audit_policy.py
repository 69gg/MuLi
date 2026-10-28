"""Ingress proofs, complete working-set budgets and live continuation stay read-only."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_history_evidence import assert_code
from test_history_evidence import store as store

from muli.channel_ingress_audit import ChannelIngressAudit
from muli.conversation import Conversation
from muli.evidence import json_size
from muli.store import Store

_SCOPE = ("onebot", "account", "7", "session")
_LIMITS: dict[str, Any] = {
    "max_output_chars": 4000,
    "max_context_chars": 16000,
    "max_search_results": 100,
    "max_search_seconds": 10,
}


def read(journal: Store, **arguments: Any) -> dict[str, Any]:
    return ChannelIngressAudit(journal).read(*_SCOPE, **(_LIMITS | arguments))


def body(journal: Store, input_id: str, **arguments: Any) -> dict[str, Any]:
    return ChannelIngressAudit(journal).read_input(
        *_SCOPE, input_id=input_id, **(_LIMITS | arguments)
    )


def ingress(
    journal: Store,
    *,
    session: str = "session",
    owner: str = "7",
    state: str = "pending",
    content: str = "这份中文原件保留在接收入站中。",
    attachment_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload = Conversation.payload(
        session,
        content,
        source="channel:onebot:account",
        principal="owner",
        mode="yolo",
        attachment_ids=attachment_ids,
        route={
            "name": "onebot",
            "account_id": "account",
            "sender_id": owner,
            "target": owner,
            "kind": "private",
            "reply_to": "original-platform-event",
            "context_token": "PRIVATE-CONTEXT",
        },
    )
    if session != "session":
        journal.put("agent", {"id": session, "session_id": session, "parent_id": "session"})
        payload["channel_ownership"] = {
            "id": "historical-lease",
            "epoch": 1,
            "main_id": "session",
            "agent_id": session,
        }
    result, inserted = journal.enqueue_input(payload, max_pending=1000)
    assert inserted
    if state == "delivered":
        assert journal.claim_inputs(session, "actual-claim-run", limit=1)[0]["id"] == result["id"]
    elif state != "pending":
        with journal.db:
            journal.db.execute("UPDATE ingress SET state=? WHERE id=?", (state, result["id"]))
    return result


def payload_bytes(journal: Store, identifier: str) -> bytes:
    return bytes(
        journal.db.execute(
            "SELECT CAST(payload AS BLOB) FROM ingress WHERE id=?", (identifier,)
        ).fetchone()[0]
    )


def write_payload(journal: Store, identifier: str, raw: bytes) -> None:
    with journal.db:
        journal.db.execute(
            "UPDATE ingress SET payload=CAST(? AS TEXT) WHERE id=?", (raw, identifier)
        )


def snapshot(journal: Store) -> dict[str, Any]:
    return {
        table: [tuple(row) for row in journal.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("objects", "ingress", "message_order", "events", "runtime_state")
    }


def test_directory_and_utf8_body_preserve_ingress_independently_of_message_archive(
    store: Store,
) -> None:
    for _ in range(3):
        store.add_message("session", "assistant", "让两种序号明确不同。")
    delivered = ingress(store, session="child", state="delivered")
    references = ["plugin:旧资料", "UPPERCASE", "plugin:旧资料"]
    pending = ingress(store, attachment_ids=references, content="中文与表情🙂\n" * 300)
    cancelled = ingress(store, state="cancelled")
    before, changes = snapshot(store), store.db.total_changes
    page = read(store)
    assert [item["input_id"] for item in page["items"]] == [
        cancelled["id"],
        pending["id"],
        delivered["id"],
    ]
    assert page["stopped"] == "end" and page["has_more"] is False
    assert page["omitted"] == {"invalid": 0, "oversized": 0, "missing": 0}
    claimed = page["items"][-1]
    assert claimed["ingress_seq"] != claimed["delivered_message_seq"]
    assert claimed["state"] == "delivered" and claimed["session_id"] == "child"
    pages = [body(store, pending["id"], max_output_chars=1000)]
    while pages[-1]["has_more"]:
        assert len(pages) < 10
        pages.append(
            body(
                store,
                pending["id"],
                offset_bytes=pages[-1]["next_offset_bytes"],
                revision=pages[-1]["revision"],
                max_output_chars=1000,
            )
        )
    assert len(pages) > 1
    assert "".join(item["content"] for item in pages) == "中文与表情🙂\n" * 300
    assert all(item["attachment_ids"] == references for item in pages)
    assert len({item["revision"] for item in pages}) == 1
    assert all(item["length_bytes"] == len(item["content"].encode("utf-8")) for item in pages)
    assert all(json_size(item) <= 1000 for item in pages)
    assert store.get("message", pending["id"]) is None
    assert store.get("message", cancelled["id"]) is None
    assert snapshot(store) == before and store.db.total_changes == changes
    encoded = json.dumps([page, *pages], ensure_ascii=False)
    assert all(secret not in encoded for secret in ("PRIVATE-CONTEXT", '"route"', '"run_id"'))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("id",), "copied-input"),
        (("session_id",), "copied-session"),
        (("principal",), "external:onebot:7"),
        (("source",), "channel:onebot:another-account"),
        (("route", "name"), "telegram"),
        (("route", "account_id"), "another-account"),
        (("route", "kind"), "group"),
        (("route", "sender_id"), "8"),
        (("route", "target"), "8"),
        (("route", "reply_to"), ""),
        (("channel_ownership", "main_id"), "another-main"),
        (("channel_ownership", "agent_id"), "another-child"),
        (("channel_ownership", "epoch"), True),
        (("channel_ownership_control",), True),
        (("attachment_ids",), [""]),
    ],
)
def test_unprovable_original_scope_never_discloses_an_exact_input_id(
    store: Store, path: tuple[str, ...], value: Any
) -> None:
    item = ingress(store, session="child", state="cancelled")
    payload = json.loads(payload_bytes(store, item["id"]))
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    write_payload(store, item["id"], json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert read(store)["items"] == []
    assert_code("evidence_not_found", lambda: body(store, item["id"]))
    assert_code("evidence_not_found", lambda: body(store, "not-an-input"))


@pytest.mark.parametrize("damage", ["missing", "wrong_parent", "wrong_id", "quarantined"])
def test_historical_child_proof_needs_its_complete_direct_parent(store: Store, damage: str) -> None:
    item = ingress(store, session="child", state="cancelled")
    if damage == "missing":
        store.delete("agent", "child")
    elif damage == "quarantined":
        with store.db:
            store.db.execute("UPDATE ingress SET state='quarantined' WHERE id=?", (item["id"],))
    else:
        with store.db:
            store.db.execute(
                "UPDATE objects SET data=? WHERE kind='agent' AND id='child'",
                (
                    json.dumps(
                        {
                            "id": "other" if damage == "wrong_id" else "child",
                            "parent_id": "other" if damage == "wrong_parent" else "session",
                        }
                    ),
                ),
            )
    assert read(store)["items"] == []
    assert_code("evidence_not_found", lambda: body(store, item["id"]))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("field", ["principal", "route", "proof", "parent_id", "parent_identity"])
def test_duplicate_source_and_parent_keys_are_rejected_in_both_orders(
    store: Store, reverse: bool, field: str
) -> None:
    item = ingress(store, session="child", state="cancelled")
    if field in {"parent_id", "parent_identity"}:
        key, legitimate = ("parent_id", "session") if field == "parent_id" else ("id", "child")
        values = ["foreign", legitimate]
        if reverse:
            values.reverse()
        duplicated = ",".join(json.dumps(key) + ":" + json.dumps(value) for value in values)
        other = '"id":"child"' if key == "parent_id" else '"parent_id":"session"'
        with store.db:
            store.db.execute(
                "UPDATE objects SET data=? WHERE kind='agent' AND id='child'",
                ("{" + other + "," + duplicated + "}",),
            )
    else:
        raw = payload_bytes(store, item["id"]).decode("utf-8")
        key, legitimate = {
            "principal": ("principal", "owner"),
            "route": ("target", "7"),
            "proof": ("main_id", "session"),
        }[field]
        values = ["foreign", legitimate]
        if reverse:
            values.reverse()
        original = json.dumps(key) + ": " + json.dumps(legitimate)
        duplicated = ",".join(json.dumps(key) + ":" + json.dumps(value) for value in values)
        assert original in raw
        write_payload(store, item["id"], raw.replace(original, duplicated).encode("utf-8"))
    assert read(store)["items"] == []
    assert_code("evidence_not_found", lambda: body(store, item["id"]))


@pytest.mark.parametrize(
    "damage", ["empty", "syntax", "utf8", "surrogate", "nan", "overflow", "nested_duplicate"]
)
def test_invalid_complete_payload_json_is_never_read_as_a_prefix_or_last_wins_object(
    store: Store, damage: str
) -> None:
    item = ingress(store)
    raw = payload_bytes(store, item["id"])
    invalid = {
        "empty": b"",
        "syntax": raw[:-1],
        "utf8": raw[:-1] + b',"extra":"\xff"}',
        "surrogate": raw[:-1] + b',"extra":"\\ud800"}',
        "nan": raw[:-1] + b',"extra":NaN}',
        "overflow": raw[:-1] + b',"extra":1e999}',
        "nested_duplicate": raw[:-1] + b',"extra":{"same":1,"same":2}}',
    }[damage]
    write_payload(store, item["id"], invalid)
    page = read(store)
    assert page["items"] == [] and page["omitted"]["invalid"] == 1
    assert_code("evidence_not_found", lambda: body(store, item["id"]))


@pytest.mark.parametrize(
    "extra", [b'"escaped":"\\ud800"', b'"number":1e999', b'"nested":{"same":1,"same":2}']
)
def test_complete_parent_json_uses_the_same_strict_decoder(store: Store, extra: bytes) -> None:
    item = ingress(store, session="child")
    raw = b'{"id":"child","parent_id":"session",' + extra + b"}"
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=CAST(? AS TEXT) WHERE kind='agent' AND id='child'", (raw,)
        )
    assert read(store)["items"] == []
    assert_code("evidence_not_found", lambda: body(store, item["id"]))


def test_zero_payload_never_calls_a_positive_budget_reader_and_missing_payload_is_counted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = ingress(store)
    original = payload_bytes(store, item["id"])
    write_payload(store, item["id"], b"")
    calls: list[int] = []

    def missing(seq: int, *, parent: bool = False, max_bytes: int) -> None:
        assert max_bytes > 0
        calls.append(seq)
        return None

    monkeypatch.setattr(store, "ingress_audit_bytes", missing)
    assert read(store)["omitted"]["invalid"] == 1 and calls == []
    write_payload(store, item["id"], original)
    page = read(store)
    assert page["items"] == [] and page["omitted"]["missing"] == 1
    assert calls == [item["seq"]]
    assert_code("evidence_not_found", lambda: body(store, item["id"]))


def test_working_set_over_page_cap_advances_without_loading_payload_or_parent(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = ingress(store)
    oversized = ingress(store, session="child")
    store.put("agent", {"id": "child", "parent_id": "session", "padding": "x" * 700})
    probe = store.ingress_audit_probe("session", seq=oversized["seq"], max_bytes=64000)
    assert probe is not None
    individual = max(probe["payload_bytes"], probe["parent_bytes"])
    context = (individual + 3) // 4
    cap = context * 4
    assert individual <= cap < probe["required_bytes"]
    gated = store.ingress_audit_probe("session", seq=oversized["seq"], max_bytes=cap)
    assert gated is not None and gated["id"] is None and gated["session_id"] is None
    original = store.ingress_audit_bytes
    loads: list[tuple[int, bool]] = []

    def loaded(seq: int, *, parent: bool = False, max_bytes: int) -> bytes | None:
        loads.append((seq, parent))
        return original(seq, parent=parent, max_bytes=max_bytes)

    monkeypatch.setattr(store, "ingress_audit_bytes", loaded)
    first = read(store, max_context_chars=context, max_search_results=1)
    assert first["items"] == [] and first["omitted"]["oversized"] == 1
    assert first["has_more"] is True and first["stopped"] == "candidate_budget"
    assert loads == [] and first["json_bytes"] == 0
    assert_code(
        "evidence_not_found", lambda: body(store, oversized["id"], max_context_chars=context)
    )
    assert loads == []
    second = read(store, cursor=first["next_cursor"], max_context_chars=context)
    assert [item["input_id"] for item in second["items"]] == [older["id"]]
    assert second["omitted_total"] == 1 and second["has_more"] is False


def test_remaining_raw_budget_and_parent_cache_are_distinct_from_an_oversized_unit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = ingress(store, session="child")
    newest = ingress(store, session="child")
    store.put("agent", {"id": "child", "parent_id": "session", "padding": "x" * 300})
    probe = store.ingress_audit_probe("session", seq=newest["seq"], max_bytes=64000)
    assert probe is not None
    unit, parent_size = probe["required_bytes"], probe["parent_bytes"]
    original = store.ingress_audit_bytes
    parent_loads: list[int] = []

    def loaded(seq: int, *, parent: bool = False, max_bytes: int) -> bytes | None:
        if parent:
            parent_loads.append(seq)
        return original(seq, parent=parent, max_bytes=max_bytes)

    monkeypatch.setattr(store, "ingress_audit_bytes", loaded)
    combined_context = (unit * 2 - parent_size + 3) // 4
    cached = read(store, max_context_chars=combined_context)
    assert len(cached["items"]) == 2 and cached["json_bytes"] == unit * 2 - parent_size
    assert parent_loads == [newest["seq"]]
    parent_loads.clear()
    one_context = (unit + 3) // 4
    first = read(store, max_context_chars=one_context)
    assert [item["input_id"] for item in first["items"]] == [newest["id"]]
    assert first["stopped"] == "raw_budget" and first["omitted_total"] == 0
    second = read(store, cursor=first["next_cursor"], max_context_chars=one_context)
    assert [item["input_id"] for item in second["items"]] == [older["id"]]
    assert second["json_bytes"] == unit and second["has_more"] is False
    assert parent_loads == [newest["seq"], older["seq"]]


def test_giant_relational_identity_is_sql_gated_and_skipped_before_json_reads(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = ingress(store)
    with store.db:
        store.db.execute("UPDATE ingress SET session_id=? WHERE id=?", ("x" * 10000, item["id"]))
    probe = store.ingress_audit_probe("session", seq=item["seq"], max_bytes=4000)
    assert probe is not None and probe["id"] is None and probe["session_id"] is None

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("超大工作集不能读取完整 JSON")

    monkeypatch.setattr(store, "ingress_audit_bytes", forbidden)
    page = read(store, max_context_chars=1000)
    assert page["omitted"]["oversized"] == 1 and page["stopped"] == "end"


@pytest.mark.parametrize(
    "damage", ["large_payload", "large_parent", "foreign_owner", "foreign_account"]
)
def test_exact_id_without_a_complete_scope_proof_has_the_same_error_as_absence(
    store: Store, damage: str
) -> None:
    item = ingress(store, session="child", owner="8" if damage == "foreign_owner" else "7")
    if damage == "large_parent":
        store.put("agent", {"id": "child", "parent_id": "session", "padding": "x" * 10000})
    elif damage in {"large_payload", "foreign_account"}:
        payload = json.loads(payload_bytes(store, item["id"]))
        if damage == "large_payload":
            payload["content"] = "x" * 10000
        else:
            payload["source"] = "channel:onebot:another-account"
            payload["route"]["account_id"] = "another-account"
        write_payload(store, item["id"], json.dumps(payload).encode())
    for identifier in (item["id"], "not-an-input"):
        assert_code(
            "evidence_not_found",
            lambda identifier=identifier: body(store, identifier, max_context_chars=1000),
        )


def test_empty_foreign_window_deleted_anchor_and_new_arrival_keep_seek_progress(
    store: Store,
) -> None:
    older = ingress(store)
    foreign = ingress(store, owner="8")
    first = read(store, max_search_results=1)
    assert first["items"] == [] and first["scanned"] == 1 and first["has_more"] is True
    with store.db:
        store.db.execute("DELETE FROM ingress WHERE id=?", (foreign["id"],))
    newer = ingress(store)
    next_page = read(store, cursor=first["next_cursor"])
    assert [item["input_id"] for item in next_page["items"]] == [older["id"]]
    assert next_page["has_more"] is False
    assert read(store)["items"][0]["input_id"] == newer["id"]


@pytest.mark.parametrize(
    ("position", "value"),
    [
        (0, True),
        (1, "channel_history"),
        (2, "another-store"),
        (3, "other-main"),
        (4, "telegram"),
        (5, "other-account"),
        (6, "8"),
        (7, "cancelled"),
        (8, True),
        (9, -1),
    ],
)
def test_cursor_is_strict_and_bound_to_the_entire_scope(
    store: Store, position: int, value: Any
) -> None:
    ingress(store)
    ingress(store)
    token = read(store, limit=1)["next_cursor"]
    fields = json.loads(base64.urlsafe_b64decode(token))
    fields[position] = value
    changed = base64.urlsafe_b64encode(json.dumps(fields).encode()).decode()
    assert_code("channel_ingress_cursor_invalid", lambda: read(store, cursor=changed))


def test_cross_database_cursor_and_other_reader_cursor_are_not_accepted(
    store: Store, tmp_path: Path
) -> None:
    ingress(store)
    ingress(store)
    token = read(store, limit=1)["next_cursor"]
    other = Store(tmp_path / "other.sqlite3")
    try:
        assert_code("channel_ingress_cursor_invalid", lambda: read(other, cursor=token))
    finally:
        other.close()
    malformed = base64.urlsafe_b64encode(b'[1,"channel_ingress_audit",NaN]').decode()
    assert_code("channel_ingress_cursor_invalid", lambda: read(store, cursor=malformed))


def test_status_changes_keep_revision_but_payload_changes_invalidate_continuation(
    store: Store,
) -> None:
    item = ingress(store, content="正文中有中文🙂" * 300)
    first = body(store, item["id"], max_output_chars=1000)
    assert first["has_more"] is True
    with store.db:
        store.db.execute("UPDATE ingress SET state='cancelled' WHERE id=?", (item["id"],))
    second = body(
        store, item["id"], offset_bytes=first["next_offset_bytes"], revision=first["revision"]
    )
    assert second["state"] == "cancelled" and second["revision"] == first["revision"]
    assert_code("invalid_arguments", lambda: body(store, item["id"], offset_bytes=3))
    assert_code(
        "invalid_offset",
        lambda: body(store, item["id"], offset_bytes=1, revision=first["revision"]),
    )
    raw = payload_bytes(store, item["id"])
    payload = json.loads(raw)
    payload["content"] += "原始记录已变更"
    write_payload(store, item["id"], json.dumps(payload, ensure_ascii=False).encode())
    assert_code("evidence_changed", lambda: body(store, item["id"], revision=first["revision"]))


def test_missing_archive_does_not_reclassify_or_recreate_delivered_ingress(store: Store) -> None:
    item = ingress(store, state="delivered")
    linked = read(store)["items"][0]["delivered_message_seq"]
    store.delete("message", item["id"])
    with store.db:
        store.db.execute("DELETE FROM message_order WHERE message_id=?", (item["id"],))
    before, changes = snapshot(store), store.db.total_changes
    page = body(store, item["id"])
    assert page["state"] == "delivered" and page["delivered_message_seq"] == linked
    assert read(store)["items"][0]["state"] == "delivered"
    assert store.get("message", item["id"]) is None
    assert snapshot(store) == before and store.db.total_changes == changes


def test_legal_item_output_failure_keeps_position_and_is_never_an_invalid_omission(
    store: Store,
) -> None:
    large = ingress(store, attachment_ids=[f"{number}:" + "x" * 200 for number in range(32)])
    newest = ingress(store)
    page = read(store, max_output_chars=2000)
    assert [item["input_id"] for item in page["items"]] == [newest["id"]]
    assert page["stopped"] == "output_budget" and page["has_more"] is True
    assert page["omitted_total"] == 0
    assert_code(
        "output_budget", lambda: read(store, cursor=page["next_cursor"], max_output_chars=2000)
    )
    assert_code("output_budget", lambda: body(store, large["id"], max_output_chars=2000))
    assert_code("output_budget", lambda: read(store, max_output_chars=10))


def test_deadline_returns_only_committed_progress_and_closes_its_read_transaction(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingress(store)
    newest = ingress(store)
    clock = [0.0]
    monkeypatch.setattr("muli.channel_ingress_audit.time.monotonic", lambda: clock[0])
    original = store.ingress_audit_bytes
    calls = 0

    def loaded(seq: int, *, parent: bool = False, max_bytes: int) -> bytes | None:
        nonlocal calls
        result = original(seq, parent=parent, max_bytes=max_bytes)
        calls += 1
        if calls == 2:
            clock[0] = 20.0
        return result

    monkeypatch.setattr(store, "ingress_audit_bytes", loaded)
    page = read(store)
    assert [item["input_id"] for item in page["items"]] == [newest["id"]]
    assert page["stopped"] == "deadline" and page["has_more"] is True
    assert not store.db.in_transaction
    clock[0], calls = 0.0, 1
    assert_code("channel_ingress_timeout", lambda: read(store))
    assert not store.db.in_transaction


def test_existing_transaction_is_not_rolled_back_and_sql_errors_do_not_leak(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = ingress(store)
    store.db.execute("BEGIN")
    store.db.execute("UPDATE ingress SET state='cancelled' WHERE id=?", (item["id"],))
    assert_code("channel_ingress_transaction_active", lambda: read(store))
    assert store.db.in_transaction
    assert (
        store.db.execute("SELECT state FROM ingress WHERE id=?", (item["id"],)).fetchone()[0]
        == "cancelled"
    )
    store.db.rollback()
    assert (
        store.db.execute("SELECT state FROM ingress WHERE id=?", (item["id"],)).fetchone()[0]
        == "pending"
    )

    def broken(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("PRIVATE-SQL-DIAGNOSTIC")

    monkeypatch.setattr(store, "ingress_audit_probe", broken)
    for action in (lambda: read(store), lambda: body(store, item["id"])):
        assert_code("channel_ingress_unavailable", action)
    assert not store.db.in_transaction
