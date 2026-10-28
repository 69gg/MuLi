"""Host-internal excerpt storage; real page authorization is tested by its caller."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from muli.evidence import EvidenceError, json_size
from muli.reading_excerpts import ReadingExcerpts
from muli.store import Store

Archive = tuple[Store, ReadingExcerpts, str]
_BUDGET = 8000


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    store = Store(tmp_path / "excerpts.sqlite3")
    main = str(store.main_session()["id"])
    try:
        yield store, ReadingExcerpts(store), main
    finally:
        store.close()


def page(
    store: Store,
    main: str,
    content: str = "前🙂重复，重复。",
    *,
    number: int = 1,
    offset: int = 7,
    **changes: object,
) -> dict[str, object]:
    identifier = f"{number:032x}"
    length = len(content.encode())
    total = offset + length + 3
    store.put(
        "attachment",
        {
            "id": identifier,
            "name": f"资料-{number}.txt",
            "source": "web",
            "mime_type": "text/plain",
            "size": total,
            "sha256": "a" * 64,
        },
    )
    return {
        "store_instance": store.instance_id,
        "main_id": main,
        "attachment_id": identifier,
        "reader": "attachment_read",
        "source_revision": "a" * 64,
        "revision": "a" * 64,
        "offset_bytes": offset,
        "length_bytes": length,
        "next_offset_bytes": offset + length,
        "total_bytes": total,
        "has_more": True,
        "source_session_id": main,
        "source_agent_id": None,
        "source_run_id": "r" * 32,
        "source_generation": None,
        "source_message_id": "m" * 32,
        "source_message_revision": "e" * 32,
        **changes,
    }


def save(
    archive: Archive,
    *,
    content: str = "前🙂重复，重复。",
    quote: str = "重复",
    note: str = "核对条件",
    occurrence: int = 1,
    **changes: object,
) -> dict[str, Any]:
    store, service, main = archive
    return service.save(
        main,
        page=page(store, main, content, **changes),
        content=content,
        quote=quote,
        occurrence=occurrence,
        note=note,
        max_output_chars=_BUDGET,
    )


def test_multiple_excerpts_keep_exact_utf8_positions_and_separate_annotation(
    archive: Archive,
) -> None:
    store, service, main = archive
    first = save(archive)
    second = save(archive, occurrence=2, note="这是批注，不是原文")
    assert first["id"] != second["id"]
    assert first["quote"] == second["quote"] == "重复"
    assert first["quote_kind"] == "source_excerpt"
    assert second["note_kind"] == "annotation" and second["note"] == "这是批注，不是原文"
    assert first["read_arguments"]["offset_bytes"] == 7 + len("前🙂".encode())
    assert second["read_arguments"]["offset_bytes"] == 7 + len("前🙂重复，".encode())
    assert second["length_bytes"] == len("重复".encode())
    assert first["verification"] == second["verification"] == "not_checked"
    assert first["generation"] == 1 and first["state"] == "active"
    assert first["created_event_id"] < second["created_event_id"]
    assert service.read(main, second["id"], max_output_chars=_BUDGET) == second
    result = service.list(main, max_output_chars=_BUDGET)
    assert [item["id"] for item in result["items"]] == [second["id"], first["id"]]
    assert result["items"][0]["quote_preview"] == "重复"
    assert result["items"][0]["note_preview"] == second["note"]
    assert all(item["preview"] is True for item in result["items"])
    assert not store.db.execute(
        "SELECT 1 FROM objects WHERE kind IN ('reading_bookmark','memory_job')"
    ).fetchone()


@pytest.mark.parametrize(
    ("content", "quote", "occurrence", "prefix"),
    [
        ("aaaa", "aa", 3, "aa"),
        ("🙂🙂🙂", "🙂🙂", 2, "🙂"),
        ("é e\u0301 e\u0301", "e\u0301", 2, "é e\u0301 "),
        ("Aa aa", "aa", 1, "Aa "),
    ],
)
def test_literal_overlapping_occurrences_are_computed_by_the_service(
    archive: Archive, content: str, quote: str, occurrence: int, prefix: str
) -> None:
    result = save(archive, content=content, quote=quote, occurrence=occurrence)
    assert result["offset_bytes"] == 7 + len(prefix.encode())
    assert result["read_arguments"]["offset_bytes"] == result["offset_bytes"]


def test_occurrence_uses_the_existing_page_item_cap_instead_of_scanning_unlimited_overlaps(
    archive: Archive,
) -> None:
    store, service, main = archive
    prepared = page(store, main, "a" * 500)
    before = store.event_position()
    with pytest.raises(EvidenceError):
        service.save(
            main,
            page=prepared,
            content="a" * 500,
            quote="a",
            occurrence=101,
            max_output_chars=_BUDGET,
        )
    assert store.event_position() == before


def test_real_event_order_survives_frozen_time_clock_rollback_and_reopen(
    archive: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, main = archive
    monkeypatch.setattr("time.time", lambda: 100.0)
    a = save(archive, note="A")
    b = save(archive, note="B")
    monkeypatch.setattr("time.time", lambda: 50.0)
    c = save(archive, note="C")
    assert [a["created_event_id"], b["created_event_id"], c["created_event_id"]] == sorted(
        {a["created_event_id"], b["created_event_id"], c["created_event_id"]}
    )
    assert c["created_at"] == 50.0
    first = service.list(main, limit=1, max_output_chars=_BUDGET)
    assert first["items"][0]["id"] == c["id"] and first["has_more"]
    store.close()
    reopened = Store(tmp_path / "excerpts.sqlite3")
    try:
        restored = ReadingExcerpts(reopened)
        assert restored.read(main, c["id"], max_output_chars=_BUDGET) == c
        second = restored.list(main, cursor=first["next_cursor"], max_output_chars=_BUDGET)
        assert [item["id"] for item in second["items"]] == [b["id"], a["id"]]
    finally:
        reopened.close()


def test_document_teammate_actor_is_retained_without_becoming_the_note_author(
    archive: Archive,
) -> None:
    _, service, main = archive
    result = save(
        archive,
        reader="document_read",
        revision="b" * 64,
        source_session_id="teammate",
        source_agent_id="teammate",
        source_generation=7,
    )
    assert result["reader"] == "document_read" and result["revision"] == "b" * 64
    assert result["source_page"] == {
        "session_id": "teammate",
        "agent_id": "teammate",
        "run_id": "r" * 32,
        "generation": 7,
        "message_id": "m" * 32,
        "message_revision": "e" * 32,
    }
    assert result["note_kind"] == "annotation"
    assert service.read(main, result["id"], max_output_chars=_BUDGET) == result


def test_delete_cas_keeps_a_tombstone_and_never_removes_independent_copies(
    archive: Archive,
) -> None:
    store, service, main = archive
    first, second = save(archive), save(archive)
    store.put("reading_bookmark", {"id": "untouched", "position": "next"})
    result = service.delete(main, first["id"], expected_generation=1, max_output_chars=_BUDGET)
    assert result == {
        "id": first["id"],
        "state": "deleted",
        "generation": 2,
        "verification": "not_checked",
    }
    position = store.event_position()
    with pytest.raises(EvidenceError) as caught:
        service.delete(main, first["id"], expected_generation=1, max_output_chars=_BUDGET)
    assert caught.value.code == "excerpt_generation_changed"
    assert store.event_position() == position
    assert service.read(main, first["id"], max_output_chars=_BUDGET) == result
    assert (
        service.delete(main, first["id"], expected_generation=2, max_output_chars=_BUDGET) == result
    )
    assert store.event_position() == position
    assert service.read(main, second["id"], max_output_chars=_BUDGET) == second
    assert store.get("reading_bookmark", "untouched") == {"id": "untouched", "position": "next"}
    assert store.get("attachment", second["attachment_id"]) is not None
    raw = store.db.execute(
        "SELECT data FROM objects WHERE kind='reading_excerpt' AND id=?", (first["id"],)
    ).fetchone()[0]
    assert "quote" not in json.loads(raw) and "note" not in json.loads(raw)


def test_saved_history_remains_readable_without_current_attachment_metadata(
    archive: Archive,
) -> None:
    store, service, main = archive
    saved = save(archive)
    store.delete("attachment", saved["attachment_id"])
    assert service.read(main, saved["id"], max_output_chars=_BUDGET) == saved
    assert service.list(main, max_output_chars=_BUDGET)["items"][0]["id"] == saved["id"]
    assert (
        service.delete(main, saved["id"], expected_generation=1, max_output_chars=_BUDGET)["state"]
        == "deleted"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "未完整核对的页"},
        {"quote": "不存在"},
        {"quote": ""},
        {"quote": "\ud800"},
        {"quote": True},
        {"occurrence": True},
        {"occurrence": 0},
        {"occurrence": 3},
        {"occurrence": 1 << 63},
        {"note": "\ud800"},
        {"note": True},
        {"max_output_chars": True},
    ],
)
def test_invalid_text_or_selection_has_no_persistent_effect(
    archive: Archive, changes: dict[str, Any]
) -> None:
    store, service, main = archive
    parameters: dict[str, Any] = {
        "page": page(store, main),
        "content": "前🙂重复，重复。",
        "quote": "重复",
        "occurrence": 1,
        "note": "",
        "max_output_chars": _BUDGET,
        **changes,
    }
    before = store.event_position()
    with pytest.raises(EvidenceError):
        service.save(main, **parameters)
    assert store.event_position() == before
    assert service.list(main, max_output_chars=_BUDGET)["items"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("store_instance", "other"),
        ("main_id", "other"),
        ("attachment_id", "f" * 32),
        ("reader", "image_describe"),
        ("reader", []),
        ("source_revision", "b" * 64),
        ("revision", "b" * 64),
        ("offset_bytes", True),
        ("length_bytes", 1),
        ("next_offset_bytes", 1),
        ("total_bytes", 1 << 63),
        ("has_more", 1),
        ("source_session_id", "other"),
        ("source_agent_id", "other"),
        ("source_generation", True),
        ("source_message_revision", ""),
        ("unexpected", "value"),
    ],
)
def test_page_metadata_is_strict_even_for_the_host_internal_save(
    archive: Archive, field: str, value: object
) -> None:
    store, service, main = archive
    prepared = page(store, main, **{field: value})
    before = store.event_position()
    with pytest.raises(EvidenceError):
        service.save(
            main,
            page=prepared,
            content="前🙂重复，重复。",
            quote="重复",
            max_output_chars=_BUDGET,
        )
    assert store.event_position() == before


def test_foreign_root_cursor_card_and_missing_main_never_create_identity(
    archive: Archive, tmp_path: Path
) -> None:
    store, service, main = archive
    a, b = save(archive), save(archive)
    cursor = service.list(main, limit=1, max_output_chars=_BUDGET)["next_cursor"]
    other = Store(tmp_path / "other.sqlite3")
    try:
        other_main = str(other.main_session()["id"])
        foreign = ReadingExcerpts(other)
        for operation in (
            lambda: foreign.read(other_main, a["id"], max_output_chars=_BUDGET),
            lambda: foreign.delete(
                other_main, b["id"], expected_generation=1, max_output_chars=_BUDGET
            ),
            lambda: foreign.list(other_main, cursor=cursor, max_output_chars=_BUDGET),
        ):
            with pytest.raises(EvidenceError):
                operation()
    finally:
        other.close()
    before = store.db.total_changes
    with pytest.raises(EvidenceError):
        service.list("missing-main", max_output_chars=_BUDGET)
    assert store.db.total_changes == before
    current = store.get("session", main)
    assert current is not None
    store.put("session", {**current, "principal": "external"})
    before = store.db.total_changes
    with pytest.raises(EvidenceError):
        service.read(main, a["id"], max_output_chars=_BUDGET)
    assert store.db.total_changes == before


def test_directory_range_never_visits_another_roots_records_in_the_same_store(
    archive: Archive,
) -> None:
    store, service, main = archive
    saved = save(archive)
    other = ("f" if saved["id"][0] != "f" else "a") + saved["id"][1:]
    store.put("reading_excerpt", {"id": other, "unrelated": "x" * 100_000})
    result = service.list(main, max_output_chars=_BUDGET)
    assert [item["id"] for item in result["items"]] == [saved["id"]]
    assert result["scanned"] == 1 and result["omitted"] == 0 and not result["has_more"]


@pytest.mark.parametrize(
    "changes", [{"source": "asr"}, {"source": "vision"}, {"sha256": "b" * 64}, {"size": True}]
)
def test_save_rechecks_current_attachment_metadata_inside_its_transaction(
    archive: Archive, changes: dict[str, object]
) -> None:
    store, service, main = archive
    prepared = page(store, main)
    identifier = str(prepared["attachment_id"])
    current = store.get("attachment", identifier)
    assert current is not None
    store.put("attachment", {**current, **changes})
    before = store.event_position()
    with pytest.raises(EvidenceError) as caught:
        service.save(
            main, page=prepared, content="前🙂重复，重复。", quote="重复", max_output_chars=_BUDGET
        )
    assert caught.value.code == "excerpt_source_changed"
    assert store.event_position() == before


def test_directory_is_finite_indexed_and_can_continue_past_corrupt_candidates(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, main = archive
    wanted = save(archive, note='唯一批注🙂"%_')
    others = [save(archive, note="普通批注") for _ in range(203)]
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_excerpt' AND id=?",
            ("x" * 100_000, others[-1]["id"]),
        )
        raw = store.db.execute(
            "SELECT data FROM objects WHERE kind='reading_excerpt' AND id=?", (others[-2]["id"],)
        ).fetchone()[0]
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_excerpt' AND id=?",
            (json.dumps({**json.loads(raw), "id": wanted["id"]}), others[-2]["id"]),
        )

    def forbidden(*_: object, **__: object) -> NoReturn:
        raise AssertionError("excerpt directory must not use unbounded legacy object reads")

    monkeypatch.setattr(store, "get", forbidden)
    monkeypatch.setattr(store, "list", forbidden)
    traced: list[str] = []
    store.db.set_trace_callback(traced.append)
    try:
        first = service.list(main, query='唯一批注🙂"%_', max_output_chars=_BUDGET)
    finally:
        store.db.set_trace_callback(None)
    assert first["items"] == [] and first["scanned"] == 200 and first["omitted"] == 2
    assert first["has_more"] and first["next_cursor"]
    second = service.list(
        main, query='唯一批注🙂"%_', cursor=first["next_cursor"], max_output_chars=_BUDGET
    )
    assert [item["id"] for item in second["items"]] == [wanted["id"]]
    assert second["scanned"] == 4 and not second["has_more"]
    sql = next(sql for sql in traced if "ORDER BY id DESC" in sql and "reading_excerpt" in sql)
    plan = [str(row[3]) for row in store.db.execute("EXPLAIN QUERY PLAN " + sql)]
    assert any("SEARCH" in detail and "kind=?" in detail and "id>" in detail for detail in plan)
    assert not any("TEMP B-TREE" in detail or "SCAN objects" in detail for detail in plan)


def test_cursor_binds_query_and_deleted_anchor_does_not_skip_older_cards(archive: Archive) -> None:
    _, service, main = archive
    a, b = save(archive), save(archive)
    first = service.list(main, limit=1, max_output_chars=_BUDGET)
    cursor = first["next_cursor"]
    for arguments in (
        {"query": "changed", "cursor": cursor},
        {"cursor": "x" * 513},
        {"cursor": True},
    ):
        with pytest.raises(EvidenceError):
            service.list(main, max_output_chars=_BUDGET, **arguments)
    service.delete(main, b["id"], expected_generation=1, max_output_chars=_BUDGET)
    assert service.list(main, cursor=cursor, max_output_chars=_BUDGET)["items"][0]["id"] == a["id"]
    assert len(service.list(main, limit=100, max_results=1, max_output_chars=_BUDGET)["items"]) == 1


def test_complete_json_budget_prevents_writes_and_queries_never_write(archive: Archive) -> None:
    store, service, main = archive
    prepared = page(store, main)
    before = store.event_position()
    with pytest.raises(EvidenceError) as caught:
        service.save(
            main, page=prepared, content="前🙂重复，重复。", quote="重复", max_output_chars=1
        )
    assert caught.value.code == "output_budget" and store.event_position() == before
    saved = save(archive)
    before = store.event_position()
    with pytest.raises(EvidenceError) as caught:
        service.delete(main, saved["id"], expected_generation=1, max_output_chars=1)
    assert caught.value.code == "output_budget" and store.event_position() == before
    assert service.read(main, saved["id"], max_output_chars=_BUDGET) == saved
    changes = store.db.total_changes
    assert json_size(service.list(main, max_output_chars=1500)) <= 1500
    with pytest.raises(EvidenceError):
        service.read(main, saved["id"], max_output_chars=1)
    assert store.db.total_changes == changes


def test_complete_read_keeps_the_quote_and_note_tail_that_directory_previews_omit(
    archive: Archive,
) -> None:
    _, service, main = archive
    quote = "🙂资料" * 70 + "原文末尾不是节选"
    note = "注释" * 80 + "批注末尾不是原文"
    saved = save(archive, content=quote, quote=quote, note=note)
    listed = service.list(main, max_output_chars=_BUDGET)["items"][0]
    assert listed["preview"] is True
    assert "原文末尾不是节选" not in listed["quote_preview"]
    assert "批注末尾不是原文" not in listed["note_preview"]
    complete = service.read(main, saved["id"], max_output_chars=_BUDGET)
    assert complete["quote"] == quote and complete["note"] == note
    assert "preview" not in complete and "quote_preview" not in complete
    assert complete["quote_kind"] == "source_excerpt" and complete["note_kind"] == "annotation"


def test_existing_large_valid_card_is_a_budget_error_and_never_missing_or_overwritten(
    archive: Archive,
) -> None:
    store, service, main = archive
    content = "a" * 10000
    prepared = page(store, main, content)
    saved = service.save(
        main,
        page=prepared,
        content=content,
        quote=content,
        note="明确保存的长摘录",
        max_output_chars=16000,
    )
    before = store.event_position()
    for operation in (
        lambda: service.read(main, saved["id"], max_output_chars=1000),
        lambda: service.delete(main, saved["id"], expected_generation=1, max_output_chars=1000),
    ):
        with pytest.raises(EvidenceError) as caught:
            operation()
        assert caught.value.code == "output_budget"
        assert store.event_position() == before
    listing = service.list(main, max_output_chars=1000)
    assert listing["items"] == [] and listing["omitted"] == 1 and listing["scanned"] == 1
    assert service.read(main, saved["id"], max_output_chars=16000) == saved


def test_save_with_missing_attachment_reports_excerpt_source_failure(archive: Archive) -> None:
    store, service, main = archive
    prepared = page(store, main)
    store.delete("attachment", str(prepared["attachment_id"]))
    before = store.event_position()
    with pytest.raises(EvidenceError) as caught:
        service.save(
            main, page=prepared, content="前🙂重复，重复。", quote="重复", max_output_chars=_BUDGET
        )
    assert caught.value.code == "excerpt_source_changed" and "书签" not in str(caught.value)
    assert store.event_position() == before


def test_escaped_previews_page_with_the_full_json_budget_without_losing_a_card(
    archive: Archive,
) -> None:
    _, service, main = archive
    quote = '"' * 40
    saved = [save(archive, content=quote, quote=quote, note="🙂" * 40) for _ in range(8)]
    cursor: str | None = None
    found: list[str] = []
    for _ in range(8):
        result = service.list(main, cursor=cursor, limit=100, max_output_chars=1000)
        assert json_size(result) <= 1000
        found.extend(item["id"] for item in result["items"])
        if not result["has_more"]:
            break
        assert result["items"] and result["next_cursor"] != cursor
        cursor = result["next_cursor"]
    assert found == [item["id"] for item in reversed(saved)]


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "x" * 121},
        {"query": "\ud800"},
        {"query": True},
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"max_results": True},
        {"max_results": 0},
        {"max_output_chars": True},
    ],
)
def test_invalid_directory_arguments_fail_without_writing(
    archive: Archive, arguments: dict[str, Any]
) -> None:
    store, service, main = archive
    save(archive)
    before = store.db.total_changes
    with pytest.raises(EvidenceError):
        service.list(main, **{"max_output_chars": _BUDGET, **arguments})
    assert store.db.total_changes == before


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_event_and_record_rollback_together_on_real_transaction_failure(
    archive: Archive, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store, service, main = archive
    saved = save(archive)
    prepared = page(store, main)
    before = store.event_position()
    original = store._put

    def failing(kind: str, item: dict[str, Any]) -> None:
        original(kind, item)
        if kind == "reading_excerpt":
            raise sqlite3.DatabaseError("synthetic record write failure")

    monkeypatch.setattr(store, "_put", failing)
    with pytest.raises(EvidenceError):
        if operation == "save":
            service.save(
                main,
                page=prepared,
                content="前🙂重复，重复。",
                quote="重复",
                max_output_chars=_BUDGET,
            )
        else:
            service.delete(main, saved["id"], expected_generation=1, max_output_chars=_BUDGET)
    assert not store.db.in_transaction and store.event_position() == before
    assert service.read(main, saved["id"], max_output_chars=_BUDGET) == saved
    assert len(service.list(main, max_output_chars=_BUDGET)["items"]) == 1


def test_unknown_or_corrupt_record_cannot_become_a_deleted_or_empty_card(archive: Archive) -> None:
    store, service, main = archive
    saved = save(archive)
    unknown = saved["id"][:-19] + f"{99999:019d}"
    assert service.read(main, unknown, max_output_chars=_BUDGET)["state"] == "empty"
    with pytest.raises(EvidenceError):
        service.delete(main, unknown, expected_generation=0, max_output_chars=_BUDGET)
    raw = store.db.execute(
        "SELECT data FROM objects WHERE kind='reading_excerpt' AND id=?", (saved["id"],)
    ).fetchone()[0]
    for damaged in (json.dumps({**json.loads(raw), "schema_version": 999}), "x" * 100_000):
        with store.db:
            store.db.execute(
                "UPDATE objects SET data=? WHERE kind='reading_excerpt' AND id=?",
                (damaged, saved["id"]),
            )
        with pytest.raises(EvidenceError):
            service.read(main, saved["id"], max_output_chars=_BUDGET)
        with pytest.raises(EvidenceError):
            service.delete(main, saved["id"], expected_generation=1, max_output_chars=_BUDGET)


def test_save_and_delete_refuse_the_callers_transaction(archive: Archive) -> None:
    store, service, main = archive
    saved = save(archive)
    prepared = page(store, main)
    store.db.execute("INSERT INTO runtime_state(name,value) VALUES('caller-owned','pending')")
    for operation in (
        lambda: service.save(
            main, page=prepared, content="前🙂重复，重复。", quote="重复", max_output_chars=_BUDGET
        ),
        lambda: service.delete(main, saved["id"], expected_generation=1, max_output_chars=_BUDGET),
    ):
        with pytest.raises(EvidenceError) as caught:
            operation()
        assert caught.value.code == "excerpt_transaction_active" and store.db.in_transaction
    store.db.rollback()
    assert (
        store.db.execute("SELECT 1 FROM runtime_state WHERE name='caller-owned'").fetchone() is None
    )


def test_deleted_record_still_requires_exact_card_identity(archive: Archive) -> None:
    store, service, main = archive
    a, b = save(archive), save(archive)
    service.delete(main, a["id"], expected_generation=1, max_output_chars=_BUDGET)
    row = store.db.execute(
        "SELECT data FROM objects WHERE kind='reading_excerpt' AND id=?", (a["id"],)
    ).fetchone()
    assert row is not None
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_excerpt' AND id=?",
            (json.dumps({**json.loads(row[0]), "id": b["id"]}), a["id"]),
        )
    with pytest.raises(EvidenceError):
        service.read(main, a["id"], max_output_chars=_BUDGET)
    with pytest.raises(EvidenceError):
        service.delete(main, a["id"], expected_generation=2, max_output_chars=_BUDGET)
