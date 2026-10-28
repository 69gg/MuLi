from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, NoReturn

import pytest

from muli.evidence import EvidenceError, json_size
from muli.reading_bookmarks import ReadingBookmarks
from muli.store import Store

Archive = tuple[Store, ReadingBookmarks, str]
_BUDGET = 8000


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    store = Store(tmp_path / "bookmarks.sqlite3")
    main = str(store.main_session()["id"])
    try:
        yield store, ReadingBookmarks(store), main
    finally:
        store.close()


def attachment(store: Store, number: int, **changes: Any) -> str:
    identifier = f"{number:032x}"
    store.put(
        "attachment",
        {
            "id": identifier,
            "name": f"资料-{number}.txt",
            "source": "web",
            "mime_type": "text/plain",
            "size": 12,
            "sha256": "a" * 64,
            "created_at": 1.0,
            **changes,
        },
    )
    return identifier


def page(store: Store, main: str, identifier: str, **changes: object) -> dict[str, object]:
    return {
        "store_instance": store.instance_id,
        "main_id": main,
        "attachment_id": identifier,
        "reader": "attachment_read",
        "source_revision": "a" * 64,
        "revision": "a" * 64,
        "offset_bytes": 0,
        "length_bytes": 7,
        "next_offset_bytes": 7,
        "total_bytes": 12,
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
    identifier: str,
    *,
    generation: int = 0,
    position: Literal["start", "next"] = "next",
    **changes: object,
) -> dict[str, Any]:
    store, service, main = archive
    return service.set(
        main,
        identifier,
        page=page(store, main, identifier, **changes),
        position=position,
        expected_generation=generation,
        max_output_chars=_BUDGET,
    )


def order_rows(store: Store) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in store.db.execute(
            "SELECT data FROM objects WHERE kind='reading_bookmark_order' ORDER BY id"
        )
    ]


def test_position_round_trip_clear_barrier_and_missing_attachment(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    assert service.read(main, identifier, max_output_chars=_BUDGET) == {
        "id": identifier,
        "state": "empty",
        "generation": 0,
        "verification": "not_checked",
    }
    first = save(archive, identifier, position="start")
    assert first["offset_bytes"] == 0 and first["generation"] == 1
    second = save(archive, identifier, generation=1)
    assert second["read_arguments"] == {
        "id": identifier,
        "revision": "a" * 64,
        "offset_bytes": len("中🙂".encode()),
    }
    assert second["verification"] == "not_checked" and not second["eof"]
    assert service.read(main, identifier, max_output_chars=_BUDGET) == second
    assert len(order_rows(store)) == 1
    store.delete("attachment", identifier)
    cleared = service.delete(main, identifier, expected_generation=2, max_output_chars=_BUDGET)
    assert cleared == {
        "id": identifier,
        "state": "deleted",
        "generation": 3,
        "verification": "not_checked",
    }
    assert order_rows(store) == []
    assert service.read(main, identifier, max_output_chars=_BUDGET) == cleared
    assert service.list(main, max_output_chars=_BUDGET)["items"] == []


def test_real_event_order_survives_frozen_time_clock_rollback_and_reopen(
    archive: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, main = archive
    a, b = attachment(store, 1), attachment(store, 2)
    monkeypatch.setattr("time.time", lambda: 100.0)
    generations = {a: 0, b: 0}
    events: list[int] = []
    for identifier in (a, b, a, b):
        current = save(archive, identifier, generation=generations[identifier])
        generations[identifier] += 1
        events.append(current["last_set_event_id"])
        assert service.list(main, limit=1, max_output_chars=_BUDGET)["items"][0]["id"] == identifier
    monkeypatch.setattr("time.time", lambda: 50.0)
    current = save(archive, a, generation=2)
    events.append(current["last_set_event_id"])
    assert events == sorted(set(events))
    assert len(order_rows(store)) == 2
    assert current["updated_at"] == 50.0
    assert [item["id"] for item in service.list(main, max_output_chars=_BUDGET)["items"]] == [a, b]
    first = service.list(main, limit=1, max_output_chars=_BUDGET)
    assert first["has_more"]
    assert (
        service.list(main, cursor=first["next_cursor"], max_output_chars=_BUDGET)["items"][0]["id"]
        == b
    )
    store.close()
    reopened = Store(tmp_path / "bookmarks.sqlite3")
    try:
        reopened_service = ReadingBookmarks(reopened)
        assert reopened_service.list(main, limit=1, max_output_chars=_BUDGET)["items"][0]["id"] == a
        assert reopened_service.read(main, a, max_output_chars=_BUDGET) == current
    finally:
        reopened.close()


def test_teammate_document_metadata_remains_its_real_source(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    result = save(
        archive,
        identifier,
        reader="document_read",
        revision="b" * 64,
        source_session_id="teammate-session",
        source_agent_id="teammate-agent",
        source_generation=7,
        total_bytes=7,
        has_more=False,
    )
    assert result["reader"] == "document_read" and result["eof"]
    assert result["source_page"] == {
        "session_id": "teammate-session",
        "agent_id": "teammate-agent",
        "run_id": "r" * 32,
        "generation": 7,
        "message_id": "m" * 32,
        "message_revision": "e" * 32,
    }
    assert service.read(main, identifier, max_output_chars=_BUDGET)["revision"] == "b" * 64


def test_old_cas_cannot_restore_deleted_position_or_create_arbitrary_tombstones(
    archive: Archive,
) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    save(archive, identifier)
    service.delete(main, identifier, expected_generation=1, max_output_chars=_BUDGET)
    before = store.db.total_changes
    with pytest.raises(EvidenceError) as caught:
        save(archive, identifier, generation=1)
    assert caught.value.code == "bookmark_generation_changed"
    assert store.db.total_changes == before
    assert save(archive, identifier, generation=2)["generation"] == 3
    missing = f"{999:032x}"
    with pytest.raises(EvidenceError):
        service.delete(main, missing, expected_generation=0, max_output_chars=_BUDGET)
    assert service.read(main, missing, max_output_chars=_BUDGET)["state"] == "empty"
    fresh = attachment(store, 2)
    assert (
        service.delete(main, fresh, expected_generation=0, max_output_chars=_BUDGET)["generation"]
        == 1
    )
    with pytest.raises(EvidenceError):
        save(archive, fresh)


def test_output_budget_failure_is_atomic_and_reads_do_not_write(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    before_event = store.event_position()
    with pytest.raises(EvidenceError) as caught:
        service.set(
            main,
            identifier,
            page=page(store, main, identifier),
            position="next",
            expected_generation=0,
            max_output_chars=1,
        )
    assert caught.value.code == "output_budget"
    assert store.event_position() == before_event and order_rows(store) == []
    assert service.read(main, identifier, max_output_chars=_BUDGET)["state"] == "empty"
    saved = save(archive, identifier)
    with pytest.raises(EvidenceError):
        service.delete(main, identifier, expected_generation=1, max_output_chars=1)
    assert service.read(main, identifier, max_output_chars=_BUDGET) == saved
    before = store.db.total_changes
    assert json_size(service.list(main, max_output_chars=2000)) <= 2000
    service.read(main, identifier, max_output_chars=_BUDGET)
    assert store.db.total_changes == before


@pytest.mark.parametrize("failure", ["new_order", "old_order_delete"])
def test_event_bookmark_and_order_roll_back_as_one_transaction(
    archive: Archive, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    original = save(archive, identifier)
    old_order = order_rows(store)
    event_position = store.event_position()
    if failure == "new_order":
        original_put = store._put

        def put(kind: str, item: dict[str, Any]) -> None:
            original_put(kind, item)
            if kind == "reading_bookmark_order":
                raise sqlite3.DatabaseError("synthetic order persistence failure")

        monkeypatch.setattr(store, "_put", put)
    else:

        def deny_delete(action: int, first: str | None, *_: object) -> int:
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_DELETE and first == "objects"
                else sqlite3.SQLITE_OK
            )

        store.db.set_authorizer(deny_delete)
    try:
        with pytest.raises(EvidenceError):
            save(archive, identifier, generation=1)
    finally:
        store.db.set_authorizer(None)
    assert store.event_position() == event_position
    assert service.read(main, identifier, max_output_chars=_BUDGET) == original
    assert order_rows(store) == old_order
    assert not store.db.in_transaction


def test_delete_does_not_commit_before_a_later_transaction_failure(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    original = save(archive, identifier)
    old_order = order_rows(store)
    event_position = store.event_position()

    def deny_delete(action: int, first: str | None, *_: object) -> int:
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_DELETE and first == "objects"
            else sqlite3.SQLITE_OK
        )

    store.db.set_authorizer(deny_delete)
    try:
        with pytest.raises(EvidenceError):
            service.delete(main, identifier, expected_generation=1, max_output_chars=_BUDGET)
    finally:
        store.db.set_authorizer(None)
    assert service.read(main, identifier, max_output_chars=_BUDGET) == original
    assert order_rows(store) == old_order and store.event_position() == event_position


def test_foreign_or_corrupt_main_identity_is_never_created(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    before = store.db.total_changes
    for root in ("other-main", "", True):
        with pytest.raises(EvidenceError):
            service.read(root, identifier, max_output_chars=_BUDGET)  # type: ignore[arg-type]
    assert store.db.total_changes == before
    session = store.get("session", main)
    assert session is not None
    store.put("session", {**session, "principal": "external"})
    before = store.db.total_changes
    with pytest.raises(EvidenceError):
        save(archive, identifier)
    assert store.db.total_changes == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("store_instance", "other-store"),
        ("main_id", "other-main"),
        ("attachment_id", "f" * 32),
        ("reader", "audio_transcribe"),
        ("reader", []),
        ("source_revision", "x" * 64),
        ("revision", "b" * 64),
        ("offset_bytes", True),
        ("length_bytes", -1),
        ("next_offset_bytes", 8),
        ("total_bytes", 13),
        ("total_bytes", 1 << 63),
        ("has_more", 1),
        ("source_session_id", "other-session"),
        ("source_agent_id", "other-agent"),
        ("source_generation", 1),
        ("source_run_id", "r" * 97),
        ("source_message_id", ""),
        ("source_message_revision", "\ud800"),
        ("unexpected", "not accepted"),
    ],
)
def test_untrusted_page_fields_fail_without_persisting(
    archive: Archive, field: str, value: object
) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    before = store.db.total_changes
    with pytest.raises(EvidenceError):
        save(archive, identifier, **{field: value})
    assert store.db.total_changes == before and order_rows(store) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"page": None},
        {"position": []},
        {"position": "offset"},
        {"expected_generation": True},
        {"max_output_chars": True},
    ],
)
def test_invalid_set_arguments_cannot_turn_into_delete(
    archive: Archive, arguments: dict[str, Any]
) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    saved = save(archive, identifier)
    parameters: dict[str, Any] = {
        "page": page(store, main, identifier),
        "position": "next",
        "expected_generation": 1,
        "max_output_chars": _BUDGET,
        **arguments,
    }
    before = store.db.total_changes
    with pytest.raises(EvidenceError):
        service.set(main, identifier, **parameters)
    assert store.db.total_changes == before
    assert service.read(main, identifier, max_output_chars=_BUDGET) == saved


@pytest.mark.parametrize("changes", [{"source": "asr"}, {"sha256": "b" * 64}, {"size": True}])
def test_current_attachment_metadata_must_match_verified_page(
    archive: Archive, changes: dict[str, object]
) -> None:
    store, _, _ = archive
    identifier = attachment(store, 1, **changes)
    with pytest.raises(EvidenceError):
        save(archive, identifier)
    assert order_rows(store) == []


def test_bounded_directory_skips_stale_and_oversized_candidates_without_legacy_scans(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service, main = archive
    wanted = attachment(store, 1, name='报告-中文🙂".txt')
    save(archive, wanted)
    for number in range(2, 205):
        save(archive, attachment(store, number))
    rows = order_rows(store)
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_bookmark_order' AND id=?",
            (json.dumps({**rows[-1], "generation": 999}), rows[-1]["id"]),
        )
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_bookmark_order' AND id=?",
            ("x" * 100_000, rows[-2]["id"]),
        )

    def forbidden(*_: object, **__: object) -> NoReturn:
        raise AssertionError("bookmark directory must use finite indexed point/range reads")

    monkeypatch.setattr(store, "get", forbidden)
    monkeypatch.setattr(store, "list", forbidden)
    traced: list[str] = []
    store.db.set_trace_callback(traced.append)
    try:
        first = service.list(main, name_query='中文🙂"', max_output_chars=_BUDGET)
    finally:
        store.db.set_trace_callback(None)
    assert first["items"] == [] and first["scanned"] == 200 and first["omitted"] == 2
    assert first["has_more"] and first["next_cursor"]
    second = service.list(
        main, name_query='中文🙂"', cursor=first["next_cursor"], max_output_chars=_BUDGET
    )
    assert [item["id"] for item in second["items"]] == [wanted]
    assert second["scanned"] == 4 and not second["has_more"]
    directory_sql = next(
        sql for sql in traced if "ORDER BY id DESC" in sql and "reading_bookmark_order" in sql
    )
    plan = [str(row[3]) for row in store.db.execute("EXPLAIN QUERY PLAN " + directory_sql)]
    assert any(
        "SEARCH" in detail and "kind=?" in detail and "id>" in detail and "id<" in detail
        for detail in plan
    )
    assert not any("TEMP B-TREE" in detail or "SCAN objects" in detail for detail in plan)


def test_cursor_binds_store_root_query_and_deleted_anchor_is_safe(
    archive: Archive, tmp_path: Path
) -> None:
    store, service, main = archive
    a, b = attachment(store, 1), attachment(store, 2)
    save(archive, a)
    save(archive, b)
    first = service.list(main, limit=1, max_output_chars=_BUDGET)
    cursor = first["next_cursor"]
    with pytest.raises(EvidenceError):
        service.list(main, cursor=cursor, name_query="different", max_output_chars=_BUDGET)
    for invalid in ("!invalid", "x" * 513, True):
        with pytest.raises(EvidenceError):
            service.list(main, cursor=invalid, max_output_chars=_BUDGET)  # type: ignore[arg-type]
    other = Store(tmp_path / "other.sqlite3")
    try:
        other_main = str(other.main_session()["id"])
        with pytest.raises(EvidenceError):
            ReadingBookmarks(other).list(other_main, cursor=cursor, max_output_chars=_BUDGET)
    finally:
        other.close()
    service.delete(main, b, expected_generation=1, max_output_chars=_BUDGET)
    assert service.list(main, cursor=cursor, max_output_chars=_BUDGET)["items"][0]["id"] == a
    assert (
        service.list(main, limit=100, max_results=1, max_output_chars=_BUDGET)["items"][0]["id"]
        == a
    )


def test_missing_attachment_metadata_is_a_marker_not_a_hidden_bookmark(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    save(archive, identifier)
    store.delete("attachment", identifier)
    item = service.list(main, max_output_chars=_BUDGET)["items"][0]
    assert item["id"] == identifier and item["name"] is None
    assert item["metadata_available"] is False and item["verification"] == "not_checked"


def test_unknown_or_oversized_bookmark_never_becomes_an_empty_record(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    save(archive, identifier)
    row = store.db.execute("SELECT id,data FROM objects WHERE kind='reading_bookmark'").fetchone()
    assert row is not None
    original = json.loads(row["data"])
    for raw in (json.dumps({**original, "schema_version": 999}), "x" * 100_000):
        with store.db:
            store.db.execute(
                "UPDATE objects SET data=? WHERE kind='reading_bookmark' AND id=?", (raw, row["id"])
            )
        for operation in (
            lambda: service.read(main, identifier, max_output_chars=_BUDGET),
            lambda: save(archive, identifier, generation=1),
            lambda: service.delete(
                main, identifier, expected_generation=1, max_output_chars=_BUDGET
            ),
        ):
            with pytest.raises(EvidenceError):
                operation()


def test_write_refuses_to_commit_an_existing_callers_transaction(archive: Archive) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    store.db.execute("INSERT INTO runtime_state(name,value) VALUES('caller-owned','pending')")
    assert store.db.in_transaction
    with pytest.raises(EvidenceError):
        save(archive, identifier)
    assert store.db.in_transaction
    store.db.rollback()
    assert (
        store.db.execute("SELECT 1 FROM runtime_state WHERE name='caller-owned'").fetchone() is None
    )
    assert service.read(main, identifier, max_output_chars=_BUDGET)["state"] == "empty"


def test_generation_and_event_integer_exhaustion_do_not_leave_half_a_write(
    archive: Archive,
) -> None:
    store, service, main = archive
    identifier = attachment(store, 1)
    saved = save(archive, identifier)
    record = store.db.execute(
        "SELECT id,data FROM objects WHERE kind='reading_bookmark'"
    ).fetchone()
    order = order_rows(store)[0]
    assert record is not None
    generation = (1 << 63) - 2
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_bookmark' AND id=?",
            (json.dumps({**json.loads(record["data"]), "generation": generation}), record["id"]),
        )
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_bookmark_order' AND id=?",
            (json.dumps({**order, "generation": generation}), order["id"]),
        )
    before_event = store.event_position()
    with pytest.raises(EvidenceError) as caught:
        save(archive, identifier, generation=generation)
    assert caught.value.code == "bookmark_generation_exhausted"
    assert store.event_position() == before_event
    assert service.read(main, identifier, max_output_chars=_BUDGET)["generation"] == generation
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_bookmark' AND id=?",
            (record["data"], record["id"]),
        )
        store.db.execute(
            "UPDATE objects SET data=? WHERE kind='reading_bookmark_order' AND id=?",
            (json.dumps(order), order["id"]),
        )
        store.db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='events'", ((1 << 63) - 1,))
    with pytest.raises(EvidenceError):
        save(archive, identifier, generation=1)
    assert service.read(main, identifier, max_output_chars=_BUDGET) == saved
    assert order_rows(store) == [order]


def test_current_format_reopen_requires_no_new_schema_or_startup_backfill(tmp_path: Path) -> None:
    path = tmp_path / "current.sqlite3"
    original = Store.open_runtime(path, default_mode="ask")
    main = original.existing_main_session_id()
    assert main is not None
    schema = [
        (row[0], row[1])
        for row in original.db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name")
    ]
    original.close()
    store = Store.open_runtime(path, default_mode="ask")
    try:
        assert not store.migration_allowed
        before = store.db.total_changes
        service = ReadingBookmarks(store)
        assert service.read(main, f"{1:032x}", max_output_chars=_BUDGET)["state"] == "empty"
        assert service.list(main, max_output_chars=_BUDGET)["items"] == []
        assert store.db.total_changes == before
        identifier = attachment(store, 1)
        save((store, service, main), identifier)
        assert [
            (row[0], row[1])
            for row in store.db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name")
        ] == schema
    finally:
        store.close()
