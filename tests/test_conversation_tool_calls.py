"""Read saved call parameters as bounded evidence, never as executable calls."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from muli.evidence import EvidenceError
from muli.history_evidence import HistoryEvidence
from muli.store import Store
from muli.store_schema import ExistingStoreLimits


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "calls.sqlite3")
    journal.put("session", {"id": "session"})
    journal.put("session", {"id": "other"})
    try:
        yield journal
    finally:
        journal.close()


@pytest.mark.parametrize("locator", ["message_id", "seq"])
def test_pages_reassemble_the_outer_array_without_parsing_argument_strings(
    store: Store, locator: str
) -> None:
    calls = [
        {
            "id": "original-id",
            "name": "inspect",
            "arguments": ' { "z": 1,\n "a": "中文🙂\\u0000" } \n' * 8,
        },
        {"id": "opaque-id", "name": "legacy", "arguments": "not JSON\n\0保持未知"},
    ]
    record = store.add_message(
        "session",
        "assistant",
        "正文已接受不代表调用已执行",
        tool_calls=calls,
        source="system:attention",
        principal="system:attention",
        content_provenance="accepted",
    )
    expected = store.db.execute(
        "SELECT json_extract(data,'$.tool_calls') FROM objects WHERE kind='message' AND id=?",
        (record["id"],),
    ).fetchone()[0]
    reader = HistoryEvidence(store)
    offset = 0
    pieces: list[str] = []
    for _ in range(30):
        page = reader.read_message(
            "session",
            **{locator: record["id"] if locator == "message_id" else record["seq"]},
            field="tool_calls",
            offset_bytes=offset,
            length_bytes=137,
            revision=record["evidence_revision"],
            max_output_chars=700,
        )
        assert page["field"] == "tool_calls" and page["message_id"] == record["id"]
        assert page["seq"] == record["seq"] and page["role"] == "assistant"
        assert page["source"] == page["principal"] == "system:attention"
        assert "content_provenance" not in page and page["side_effect_status"] == "unknown"
        assert len(json.dumps(page, ensure_ascii=False)) <= 700
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        assert page["next_offset_bytes"] == offset + page["length_bytes"]
        pieces.append(page["content"])
        offset = page["next_offset_bytes"]
        if not page["has_more"]:
            break
        assert page["length_bytes"] > 0
    else:
        pytest.fail("工具调用字节分页未取得进展")
    assert "".join(pieces) == expected
    assert json.loads("".join(pieces)) == calls
    assert offset == len(expected.encode("utf-8"))


def test_only_the_requested_sql_byte_window_is_materialized_in_python(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = store.add_message(
        "session",
        "assistant",
        "正文" * 50_000,
        tool_calls=[{"id": "large", "name": "inspect", "arguments": "参数" * 50_000}],
        provider_data={"opaque": "native" * 50_000},
        images=[{"data": "Y" * 100_000}],
    )
    projected: list[dict[str, Any]] = []
    statements: list[str] = []
    connections: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    class ObservedCursor(sqlite3.Cursor):
        def fetchone(self) -> Any:
            row = super().fetchone()
            if isinstance(row, sqlite3.Row) and "content" in row.keys():
                projected.append(dict(row))
            return row

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            return self.cursor(factory=ObservedCursor).execute(sql, parameters)

    def observed_connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        assert database.endswith("?mode=ro&cache=private") and kwargs["timeout"] == 0
        connection = connect(database, factory=ObservedConnection, **kwargs)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    def no_decode(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("证据分页不能解码整条消息或完整工具调用数组")

    with monkeypatch.context() as scoped:
        scoped.setattr(sqlite3, "connect", observed_connect)
        scoped.setattr(json, "loads", no_decode)
        scoped.setattr(store, "get", no_decode)
        page = HistoryEvidence(store).read_message(
            "session",
            message_id=record["id"],
            field="tool_calls",
            length_bytes=11,
            max_output_chars=1000,
        )
    assert page["has_more"] and 0 < page["length_bytes"] <= 11
    assert len(projected) == 1 and len(projected[0]["content"]) == 11
    assert projected[0]["content_bytes"] > 100_000
    assert not {"data", "images", "provider_data", "tool_calls"}.intersection(projected[0])
    assert all("SELECT o.data" not in sql and "SELECT data" not in sql for sql in statements)
    assert all("$.provider_data" not in sql and "$.images" not in sql for sql in statements)
    assert any("json_type(o.data,'$.tool_calls')" in sql for sql in statements)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


def test_missing_and_explicit_empty_calls_are_distinct_and_identity_is_not_guessed(
    store: Store,
) -> None:
    missing = store.add_message("session", "assistant", "旧记录没有调用字段")
    empty = store.add_message("session", "assistant", "显式没有调用", tool_calls=[])
    evidence = HistoryEvidence(store)
    with pytest.raises(EvidenceError) as captured:
        evidence.read_message(
            "session", message_id=missing["id"], field="tool_calls", max_output_chars=1000
        )
    assert captured.value.code == "field_missing"
    page = evidence.read_message(
        "session", seq=empty["seq"], field="tool_calls", max_output_chars=1000
    )
    assert page["content"] == "[]" and page["total_bytes"] == 2 and not page["has_more"]
    assert "source" not in page and "principal" not in page


@pytest.mark.parametrize("calls", [None, False, 3, "[]", {"arguments": "{}"}])
def test_non_array_call_fields_are_rejected_instead_of_becoming_empty(
    store: Store, calls: Any
) -> None:
    record = store.add_message("session", "assistant", "已保存", tool_calls=calls)
    with pytest.raises(EvidenceError) as captured:
        HistoryEvidence(store).read_message(
            "session", seq=record["seq"], field="tool_calls", max_output_chars=1000
        )
    assert captured.value.code == "content_unavailable"


def test_utf8_offsets_eof_and_json_budget_keep_existing_progress_rules(store: Store) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[{"arguments": "A🙂中\0Z"}])
    raw = store.db.execute(
        "SELECT CAST(json_extract(data,'$.tool_calls') AS BLOB) FROM objects "
        "WHERE kind='message' AND id=?",
        (record["id"],),
    ).fetchone()[0]
    start = raw.index("🙂".encode())
    reader = HistoryEvidence(store)
    common = {
        "message_id": record["id"],
        "field": "tool_calls",
        "revision": record["evidence_revision"],
    }
    page = reader.read_message(
        "session", **common, offset_bytes=start - 1, length_bytes=3, max_output_chars=1000
    )
    assert page["content"] == "A" and page["next_offset_bytes"] == start
    page = reader.read_message(
        "session", **common, offset_bytes=start, length_bytes=4, max_output_chars=1000
    )
    assert page["content"] == "🙂" and page["length_bytes"] == 4
    for offset, length, budget, code in (
        (start + 1, 4, 1000, "invalid_offset"),
        (start, 3, 1000, "output_budget"),
        (len(raw) + 1, 4, 1000, "invalid_offset"),
        (0, 4, 100, "output_budget"),
    ):
        with pytest.raises(EvidenceError) as captured:
            reader.read_message(
                "session",
                **common,
                offset_bytes=offset,
                length_bytes=length,
                max_output_chars=budget,
            )
        assert captured.value.code == code
    eof = reader.read_message("session", **common, offset_bytes=len(raw), max_output_chars=1000)
    assert eof["content"] == "" and eof["length_bytes"] == 0 and not eof["has_more"]


@pytest.mark.parametrize("changed", ["content", "source", "tool_calls", "provider_data"])
def test_any_message_edit_invalidates_a_call_page_revision(store: Store, changed: str) -> None:
    record = store.add_message("session", "assistant", "原文", tool_calls=[{"arguments": "{}"}])
    page = HistoryEvidence(store).read_message(
        "session", seq=record["seq"], field="tool_calls", length_bytes=1, max_output_chars=1000
    )
    edits: dict[str, Any] = {
        "content": "改文",
        "source": "system:attention",
        "tool_calls": [{"arguments": "[]"}],
        "provider_data": {"opaque": "changed"},
    }
    store.put("message", {**record, changed: edits[changed]})
    with pytest.raises(EvidenceError) as captured:
        HistoryEvidence(store).read_message(
            "session",
            seq=record["seq"],
            field="tool_calls",
            offset_bytes=page["next_offset_bytes"],
            revision=page["revision"],
            max_output_chars=1000,
        )
    assert captured.value.code == "evidence_changed"


def test_legacy_revisions_are_read_only_and_cannot_cross_sessions(store: Store) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[])
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_remove(data,'$.evidence_revision') "
            "WHERE kind='message' AND id=?",
            (record["id"],),
        )
    reader = HistoryEvidence(store)
    changes = store.db.total_changes
    page = reader.read_message(
        "session", message_id=record["id"], field="tool_calls", max_output_chars=1000
    )
    assert page["revision"] == f"legacy:{store.instance_id}:{record['seq']}"
    assert store.db.total_changes == changes
    with pytest.raises(EvidenceError) as captured:
        reader.read_message(
            "other", message_id=record["id"], field="tool_calls", max_output_chars=1000
        )
    assert captured.value.code == "evidence_not_found"
    current = store.get("message", record["id"])
    assert current is not None
    store.put("message", current)
    with pytest.raises(EvidenceError) as captured:
        reader.read_message(
            "session",
            message_id=record["id"],
            field="tool_calls",
            revision=page["revision"],
            max_output_chars=1000,
        )
    assert captured.value.code == "evidence_changed"


def test_new_read_does_not_replace_shared_progress_or_busy_timeout(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[])
    callbacks = 0

    def outer_progress() -> int:
        nonlocal callbacks
        callbacks += 1
        return 0

    store.db.set_progress_handler(outer_progress, 1)
    store.db.execute("PRAGMA busy_timeout=2345")
    changes = store.db.total_changes
    page = HistoryEvidence(store).read_message(
        "session", seq=record["seq"], field="tool_calls", max_output_chars=1000
    )
    assert page["content"] == "[]"
    previous = callbacks
    store.db.execute("SELECT 1").fetchone()
    assert callbacks > previous
    monkeypatch.setattr("muli.store.ExistingStoreLimits", lambda: ExistingStoreLimits(vm_steps=1))
    with pytest.raises(EvidenceError) as captured:
        HistoryEvidence(store).read_message(
            "session", seq=record["seq"], field="tool_calls", max_output_chars=1000
        )
    assert captured.value.code == "evidence_read_budget"
    previous = callbacks
    store.db.execute("SELECT 1").fetchone()
    assert callbacks > previous and not store.db.in_transaction
    assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 2345
    assert store.db.total_changes == changes


def test_new_read_rejects_expired_deadlines_outer_transactions_and_read_owners(
    store: Store,
) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[])
    reader = HistoryEvidence(store)
    with pytest.raises(EvidenceError) as captured:
        reader.read_message(
            "session",
            seq=record["seq"],
            field="tool_calls",
            deadline=monotonic() - 1,
            max_output_chars=1000,
        )
    assert captured.value.code == "evidence_read_budget"
    store.db.execute("BEGIN")
    try:
        with pytest.raises(EvidenceError) as captured:
            reader.read_message(
                "session", seq=record["seq"], field="tool_calls", max_output_chars=1000
            )
        assert captured.value.code == "evidence_transaction_active" and store.db.in_transaction
    finally:
        store.db.rollback()
    store._channel_reference_read_active = True
    try:
        with pytest.raises(EvidenceError) as captured:
            reader.read_message(
                "session", seq=record["seq"], field="tool_calls", max_output_chars=1000
            )
        assert captured.value.code == "evidence_transaction_active"
    finally:
        store._channel_reference_read_active = False


@pytest.mark.parametrize("deadline", [True, float("inf"), float("nan"), 1 << 2000])
def test_new_read_requires_a_finite_host_deadline(store: Store, deadline: Any) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[])
    with pytest.raises(EvidenceError) as captured:
        HistoryEvidence(store).read_message(
            "session",
            seq=record["seq"],
            field="tool_calls",
            deadline=deadline,
            max_output_chars=1000,
        )
    assert captured.value.code == "invalid_arguments"


@pytest.mark.parametrize("field", ["$.provider_data", "attachment_ids", "", None, ["tool_calls"]])
def test_field_paths_are_a_host_whitelist(store: Store, field: Any) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[])
    with pytest.raises(EvidenceError) as captured:
        HistoryEvidence(store).read_message(
            "session", seq=record["seq"], field=field, max_output_chars=1000
        )
    assert captured.value.code == "invalid_arguments"
    with pytest.raises(EvidenceError) as captured:
        store.message_evidence_slice(
            "session",
            seq=record["seq"],
            message_id=None,
            field=field,
            offset_bytes=0,
            length_bytes=10,
            metadata_bytes=1000,
        )
    assert captured.value.code == "invalid_arguments"


def test_large_valid_output_budget_still_uses_sqlite_sized_byte_bounds(store: Store) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[{"arguments": "中文🙂"}])
    page = HistoryEvidence(store).read_message(
        "session", seq=record["seq"], field="tool_calls", max_output_chars=sys.maxsize - 1
    )
    assert json.loads(page["content"]) == [{"arguments": "中文🙂"}]
    assert not page["has_more"] and page["length_bytes"] == page["total_bytes"]


def test_readonly_connection_must_prove_the_same_store_instance(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = store.add_message("session", "assistant", "", tool_calls=[])
    replacement_path = tmp_path / "replacement.sqlite3"
    replacement = Store(replacement_path)
    connect = sqlite3.connect

    def replaced_connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        return connect(replacement_path.as_uri() + "?mode=ro&cache=private", **kwargs)

    try:
        assert replacement.instance_id != store.instance_id
        monkeypatch.setattr(sqlite3, "connect", replaced_connect)
        with pytest.raises(EvidenceError) as captured:
            HistoryEvidence(store).read_message(
                "session", seq=record["seq"], field="tool_calls", max_output_chars=1000
            )
        assert captured.value.code == "content_unavailable"
    finally:
        replacement.close()


def test_memory_database_cannot_silently_become_another_file_read(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(store, "db", memory)
            with pytest.raises(EvidenceError) as captured:
                store.message_evidence_slice(
                    "session",
                    message_id="known",
                    seq=None,
                    field="tool_calls",
                    offset_bytes=0,
                    length_bytes=10,
                    metadata_bytes=1000,
                )
            assert captured.value.code == "content_unavailable"
    finally:
        memory.close()


def test_content_api_and_default_projection_preserve_the_original_shape_and_errors(
    store: Store,
) -> None:
    record = store.add_message(
        "session", "tool", "原始正文🙂\0", content_provenance="accepted", tool_calls=[]
    )
    reader = HistoryEvidence(store)
    previous = reader.read_content("session", seq=record["seq"], max_output_chars=1000)
    assert previous == reader.read_message("session", seq=record["seq"], max_output_chars=1000)
    assert previous == reader.read_message(
        "session", seq=record["seq"], field="content", deadline=-1, max_output_chars=1000
    )
    assert previous["field"] == "content" and previous["content_provenance"] == "accepted"
    assert "side_effect_status" not in previous
    errors: list[tuple[str, str]] = []
    for read in (reader.read_content, reader.read_message):
        with pytest.raises(EvidenceError) as captured:
            read("session", seq=record["seq"], offset_bytes=1, max_output_chars=1000)
        errors.append((captured.value.code, captured.value.message))
    assert errors[0] == errors[1]
    bounds = {
        "message_id": record["id"],
        "seq": None,
        "offset_bytes": 0,
        "length_bytes": 13,
        "metadata_bytes": 1000,
    }
    assert store.message_evidence_slice("session", **bounds) == store.message_evidence_slice(
        "session", **bounds, field="content"
    )
