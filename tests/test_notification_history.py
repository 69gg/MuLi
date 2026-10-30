from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from muli.evidence import EvidenceError, json_size
from muli.notification_history import NotificationHistory
from muli.store import Store
from muli.store_schema import ExistingStoreLimits


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Store]:
    store = Store.open_runtime(tmp_path / "notifications.sqlite3", default_mode="ask")
    try:
        yield store
    finally:
        store.close()


def save(journal: Store, identifier: str, created: Any = 1, **fields: Any) -> dict[str, Any]:
    return journal.put(
        "notification",
        {
            "id": identifier,
            "title": identifier,
            "content": "原文",
            "source": "assistant",
            "read": False,
            "created_at": created,
            **fields,
        },
    )


def directory(reader: NotificationHistory, **arguments: Any) -> dict[str, Any]:
    return reader.directory(
        **{
            "max_output_chars": 32000,
            "max_results": 100,
            "max_context_chars": 96000,
            "deadline": monotonic() + 5,
            **arguments,
        }
    )


def detail(reader: NotificationHistory, identifier: str, **arguments: Any) -> dict[str, Any]:
    return reader.read(
        identifier,
        **{
            "max_output_chars": 32000,
            "max_context_chars": 96000,
            "deadline": monotonic() + 5,
            **arguments,
        },
    )


def test_exact_id_is_readable_without_index_and_directory_does_not_prepare_it(
    journal: Store,
) -> None:
    saved = save(journal, "saved")
    reader = NotificationHistory(journal)
    changes = journal.db.total_changes
    assert json.loads(detail(reader, "saved")["content"]) == saved
    with pytest.raises(EvidenceError) as caught:
        directory(reader)
    assert caught.value.code == "notification_directory_unavailable"
    assert not journal.notification_history_index()
    assert journal.db.total_changes == changes and not journal.db.in_transaction


def test_preparation_budget_rolls_back_and_restores_connection_for_id_reads(journal: Store) -> None:
    save(journal, "saved")
    timeout = journal.db.execute("PRAGMA busy_timeout").fetchone()[0]
    with pytest.raises(EvidenceError) as caught:
        journal.prepare_notification_history(limits=ExistingStoreLimits(vm_steps=1))
    assert caught.value.code == "notification_directory_unavailable"
    assert not journal.notification_history_index()
    assert journal.db.execute("PRAGMA busy_timeout").fetchone()[0] == timeout
    assert journal.db.execute("SELECT 1").fetchone()[0] == 1
    assert not journal.db.in_transaction
    assert detail(NotificationHistory(journal), "saved")["has_more"] is False


def test_same_name_wrong_ddl_is_not_repaired(journal: Store) -> None:
    journal.db.execute("CREATE INDEX notifications_created_directory ON objects(kind,id)")
    original = journal.db.execute(
        "SELECT sql FROM sqlite_schema WHERE name='notifications_created_directory'"
    ).fetchone()[0]
    with pytest.raises(EvidenceError) as caught:
        journal.prepare_notification_history()
    assert caught.value.code == "notification_directory_unavailable"
    assert (
        journal.db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='notifications_created_directory'"
        ).fetchone()[0]
        == original
    )
    assert not journal.notification_history_index() and not journal.db.in_transaction


def test_extra_index_keeps_objects4_read_only_existing_admission(
    journal: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal.prepare_notification_history()
    path = Path(journal.db.execute("PRAGMA database_list").fetchone()[2])
    mutations: list[int] = []
    original = sqlite3.connect
    forbidden = {
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_ALTER_TABLE,
    }

    def authorize(action: int, *arguments: Any) -> int:
        if action in forbidden:
            mutations.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect(*arguments: Any, **keywords: Any) -> sqlite3.Connection:
        connection = original(*arguments, **keywords)
        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr("muli.store.sqlite3.connect", connect)
    existing = Store.open_existing(path)
    try:
        assert existing.format_state == journal.format_state
        assert existing.notification_history_index()
        assert directory(NotificationHistory(existing))["has_more"] is False
        with pytest.raises(EvidenceError):
            existing.prepare_notification_history()
        assert mutations == []
    finally:
        existing.close()


def test_cumulative_raw_budget_keeps_the_unconsumed_record_for_next_page(journal: Store) -> None:
    journal.prepare_notification_history()
    for identifier, created in (("first", 3), ("middle", 2), ("last", 1)):
        save(journal, identifier, created, content="a" * 7000)
    reader = NotificationHistory(journal)
    first = directory(reader, max_context_chars=4000)
    assert [item["id"] for item in first["items"]] == ["first", "middle"]
    assert first["stopped"] == "raw_budget" and first["has_more"]
    assert first["omitted"] == {"invalid": 0, "oversized": 0, "status_unknown": 0}
    second = directory(reader, max_context_chars=4000, cursor=first["next_cursor"])
    assert [item["id"] for item in second["items"]] == ["last"]
    assert second["has_more"] is False


def test_oversized_summary_is_explicitly_skipped_without_truncating_identity(
    journal: Store,
) -> None:
    journal.prepare_notification_history()
    save(journal, "huge-title", 2, title="长" * 3000)
    save(journal, "small", 1)
    page = directory(NotificationHistory(journal), max_output_chars=1000)
    assert [item["id"] for item in page["items"]] == ["small"]
    assert page["omitted"]["oversized"] == 1
    assert page["has_more"] is False and json_size(page) <= 1000
    with pytest.raises(EvidenceError) as caught:
        directory(NotificationHistory(journal), max_output_chars=50)
    assert caught.value.code == "output_budget"


@pytest.mark.parametrize("created", [None, 0, -1, True, False, "123", [], {}])
def test_bad_times_have_stable_unknown_time_fallback(journal: Store, created: Any) -> None:
    journal.prepare_notification_history()
    save(journal, "z-unknown", created)
    save(journal, "a-known", 0.5)
    reader = NotificationHistory(journal)
    first = directory(reader, limit=1)
    second = directory(reader, limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["id"] == "a-known"
    assert second["items"][0]["id"] == "z-unknown"
    assert second["items"][0]["created_at"] is None
    assert second["has_more"] is False


def test_large_json_integer_and_finite_overflow_keep_the_sqlite_sort_key(journal: Store) -> None:
    journal.prepare_notification_history()
    save(journal, "huge-integer", 10**30)
    save(journal, "ordinary", 1)
    with journal.db:
        journal.db.execute(
            "INSERT INTO objects VALUES('notification',?,?,?)",
            ("overflow-time", '{"id":"overflow-time","created_at":1e999,"read":false}', 1),
        )
    reader = NotificationHistory(journal)
    identifiers: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        page = directory(reader, cursor=cursor, limit=1)
        identifiers.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert identifiers == ["huge-integer", "ordinary", "overflow-time"]
    assert page["items"][0]["created_at"] is None and cursor is None


def test_keyset_query_seeks_the_derived_index_and_never_sorts_whole_json(journal: Store) -> None:
    journal.prepare_notification_history()
    journal.put_many(
        [
            (
                "notification",
                {"id": f"notice-{index:04d}", "created_at": index // 3 + 1, "read": True},
            )
            for index in range(1200)
        ]
    )
    statements: list[str] = []
    journal.db.set_trace_callback(statements.append)
    try:
        reader = NotificationHistory(journal)
        first = directory(reader, status="unread")
        second = directory(reader, status="unread", cursor=first["next_cursor"])
    finally:
        journal.db.set_trace_callback(None)
    assert first["items"] == second["items"] == []
    assert first["scanned"] == second["scanned"] == 100
    queries = [sql for sql in statements if "INDEXED BY notifications_created_directory" in sql]
    assert 2 <= len(queries) <= 3
    for query in queries:
        plan = " ".join(row[3] for row in journal.db.execute("EXPLAIN QUERY PLAN " + query))
        assert "notifications_created_directory" in plan and "TEMP B-TREE" not in plan
        assert "read" not in query and "LIMIT " in query
    tie_plan = " ".join(row[3] for row in journal.db.execute("EXPLAIN QUERY PLAN " + queries[1]))
    assert "<expr>=?" in tie_plan and "id<?" in tie_plan


def test_tie_seek_work_is_bounded_even_at_the_tail_of_a_large_unknown_time_group(
    journal: Store,
) -> None:
    journal.prepare_notification_history()
    samples: list[int] = []
    for size in (30, 1200):
        journal.put_many(
            [
                ("notification", {"id": f"unknown-{index:05d}", "read": False})
                for index in range(size)
            ]
        )
        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += 1
            return 0

        journal.db.set_progress_handler(progress, 1)
        try:
            rows = list(
                journal.notification_directory_rows(
                    after=(0, "unknown-00003"), limit=2, key_bytes=100
                )
            )
        finally:
            journal.db.set_progress_handler(None, 0)
        samples.append(steps)
        assert [row["id"] for row in rows] == [b"unknown-00002", b"unknown-00001"]
    assert max(samples) < 250
    assert samples[1] <= samples[0] + 30


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset_bytes": 1},
        {"offset_bytes": True},
        {"offset_bytes": -1},
        {"length_bytes": 0},
        {"revision": ""},
    ],
)
def test_raw_reader_rejects_unsafe_coordinates(journal: Store, arguments: dict[str, Any]) -> None:
    save(journal, "notice", content="中文")
    with pytest.raises(EvidenceError) as caught:
        detail(NotificationHistory(journal), "notice", **arguments)
    assert caught.value.code == "invalid_arguments"


def test_reader_budget_failure_leaves_no_transaction_or_active_progress_callback(
    journal: Store,
) -> None:
    save(journal, "notice")
    reader = NotificationHistory(journal)
    with pytest.raises(EvidenceError) as caught:
        detail(reader, "notice", deadline=monotonic() - 1)
    assert caught.value.code == "notification_read_budget"
    assert not journal.db.in_transaction
    assert detail(reader, "notice")["has_more"] is False
