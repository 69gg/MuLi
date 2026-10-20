from __future__ import annotations

import base64
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from muli.evidence import EvidenceError, json_size
from muli.session_records import SessionRecords
from muli.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "records.sqlite3")
    try:
        yield journal
    finally:
        journal.close()


def save(store: Store, identifier: str, created: Any = 1, **fields: Any) -> dict[str, Any]:
    return store.put(
        "session",
        {
            "id": identifier,
            "title": identifier,
            "status": "idle",
            "created_at": created,
            "updated_at": created,
            **fields,
        },
    )


def all_records(
    reader: SessionRecords, *, budget: int = 32000, query: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    cursor: str | None = None
    result: list[dict[str, Any]] = []
    for _ in range(300):
        page = reader.read(query=query, cursor=cursor, limit=limit, max_output_chars=budget)
        assert set(page) == {"items", "next_cursor", "has_more"}
        assert json_size(page) <= budget
        assert len(page["items"]) <= limit
        result.extend(page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            return result
        assert page["items"] and page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    pytest.fail("记录目录没有通过有限的非空连续页到达末尾")


def test_directory_finds_old_titles_before_pagination_and_keeps_the_legacy_list(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = 1.0

    def clock() -> float:
        return timestamp

    # The legacy list orders by objects.updated, independent of JSON dates.
    monkeypatch.setattr("muli.store.now", clock)
    save(store, "old-record", timestamp, title="唯一的旧任务")
    expected_times = {"old-record": timestamp}
    for index in range(201):
        timestamp = float(index + 2)
        identifier = f"new-{index:03d}"
        save(store, identifier, timestamp)
        expected_times[identifier] = timestamp
    saved_times = {
        row["id"]: (row["created_at"], row["updated_at"], row["updated"])
        for row in store.db.execute(
            "SELECT id,updated,json_extract(data,'$.created_at') AS created_at,"
            "json_extract(data,'$.updated_at') AS updated_at FROM objects WHERE kind='session'"
        )
    }
    assert saved_times == {
        identifier: (value, value, value) for identifier, value in expected_times.items()
    }
    assert "old-record" not in {item["id"] for item in store.list("session")}
    changes = store.db.total_changes
    reader = SessionRecords(store)
    first = reader.read(max_output_chars=32000)
    assert len(first["items"]) == 50
    assert first["items"][0]["id"] == "new-200"
    assert [item["id"] for item in all_records(reader, query="旧任务")] == ["old-record"]
    complete = all_records(reader)
    assert len(complete) == 202 and complete[-1]["id"] == "old-record"
    assert len({item["id"] for item in complete}) == 202
    assert store.db.total_changes == changes


@pytest.mark.parametrize("budget", [1000, 1600, 32000])
def test_budget_preserves_contiguous_complete_rows_and_actual_last_key(
    store: Store, budget: int
) -> None:
    for index in range(73):
        save(
            store,
            f"session-{index:03d}",
            index // 3 + 1,
            title=('中文🙂\0"\\\n' * 8) + str(index),
            updated_at=1000 - index,
        )
    result = all_records(SessionRecords(store), budget=budget, limit=17)
    assert [item["id"] for item in result] == [f"session-{i:03d}" for i in reversed(range(73))]
    assert all(
        set(item) == {"id", "title", "status", "created_at", "updated_at"} for item in result
    )
    assert result[0]["title"] == ('中文🙂\0"\\\n' * 8) + "72"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("%", ["percent"]),
        ("_", ["underscore"]),
        (" a ", ["spaces"]),
        ("  ", ["spaces"]),
        ("\0bC", ["nul"]),
        ("BC", ["nul"]),
        ("Ä", ["case"]),
        ("ä", []),
        ("🙂" * 120, ["emoji"]),
    ],
)
def test_title_query_is_literal_ascii_case_insensitive_and_not_trimmed(
    store: Store, query: str, expected: list[str]
) -> None:
    for identifier, title in {
        "percent": "100%",
        "underscore": "a_b",
        "spaces": "  a  ",
        "nul": "A\0Bc",
        "case": "Ä",
        "emoji": "🙂" * 120,
    }.items():
        save(store, identifier, title=title)
    assert [item["id"] for item in all_records(SessionRecords(store), query=query)] == expected


@pytest.mark.parametrize("value", [None, 0, -1, True, False, "123", [], {}])
def test_nonpositive_or_non_number_times_are_unknown_not_coerced(store: Store, value: Any) -> None:
    save(store, "z-unknown", value)
    save(store, "a-valid", 0.125)
    items = all_records(SessionRecords(store), limit=1)
    assert [item["id"] for item in items] == ["a-valid", "z-unknown"]
    assert items[0]["created_at"] == items[0]["updated_at"] == 0.125
    assert items[1]["created_at"] is items[1]["updated_at"] is None


def test_infinite_json_numbers_and_missing_times_are_unknown(store: Store) -> None:
    for identifier, exponent in (("positive", "1e999"), ("negative", "-1e999")):
        raw = json.dumps(
            {"id": identifier, "title": identifier, "created_at": "NUMBER", "updated_at": "NUMBER"}
        ).replace('"NUMBER"', exponent)
        store.db.execute(
            "INSERT INTO objects(kind,id,data,updated) VALUES('session',?,?,1)", (identifier, raw)
        )
    store.put("session", {"id": "missing"})
    items = all_records(SessionRecords(store), limit=1)
    assert [item["id"] for item in items] == ["positive", "negative", "missing"]
    assert all(item["created_at"] is item["updated_at"] is None for item in items)
    assert items[-1]["title"] is items[-1]["status"] is None


@pytest.mark.parametrize("field", ["title", "status"])
def test_empty_text_metadata_is_preserved_on_first_and_continuation_pages(
    store: Store, field: str
) -> None:
    save(store, "empty-first", 3, **{field: ""})
    save(store, "empty-next", 2, **{field: ""})
    save(store, "nullable", 1, **{field: None})
    store.put("session", {"id": "missing"})

    items = all_records(SessionRecords(store), limit=1)

    assert [item["id"] for item in items] == [
        "empty-first",
        "empty-next",
        "nullable",
        "missing",
    ]
    assert [item[field] for item in items[:2]] == ["", ""]
    assert items[2][field] is None
    assert items[3]["title"] is items[3]["status"] is None


def test_integer_keys_keep_precision_and_a_first_page_has_no_artificial_upper_anchor(
    store: Store,
) -> None:
    for identifier, created in (
        ("z-low", 1 << 53),
        ("a-next", (1 << 53) + 1),
        ("m-high", (1 << 63) - 1),
        ("real-max", sys.float_info.max),
    ):
        save(store, identifier, created)
    items = all_records(SessionRecords(store), limit=1)
    assert [item["id"] for item in items] == ["real-max", "m-high", "a-next", "z-low"]
    assert type(items[1]["created_at"]) is int
    assert items[1]["created_at"] == (1 << 63) - 1
    assert items[2]["created_at"] == (1 << 53) + 1


def test_keyset_survives_deleted_anchor_and_updated_changes_without_loading_newer_rows(
    store: Store,
) -> None:
    for identifier in "abcdef":
        save(store, identifier, 12)
    reader = SessionRecords(store)
    page = reader.read(limit=2, max_output_chars=2000)
    assert [item["id"] for item in page["items"]] == ["f", "e"]
    store.delete("session", "e")
    record = store.get("session", "b")
    assert record is not None
    store.put("session", {**record, "updated_at": 99999, "title": "已更正"})
    save(store, "later", 13)
    page = reader.read(cursor=page["next_cursor"], max_output_chars=2000)
    assert [item["id"] for item in page["items"]] == ["d", "c", "b", "a"]
    assert page["items"][2]["title"] == "已更正"
    assert page["has_more"] is False


def test_cursor_binds_the_database_and_exact_query_but_survives_reopen(
    store: Store, tmp_path: Path
) -> None:
    save(store, "b", 2, title=" literal ")
    save(store, "a", 1, title=" literal ")
    cursor = SessionRecords(store).read(query=" literal ", limit=1, max_output_chars=2000)[
        "next_cursor"
    ]
    for query in ("literal", " LITERAL ", ""):
        with pytest.raises(EvidenceError) as caught:
            SessionRecords(store).read(query=query, cursor=cursor, max_output_chars=2000)
        assert caught.value.code == "record_cursor_invalid"
    with closing(Store(tmp_path / "other.sqlite3")) as other:
        with pytest.raises(EvidenceError) as caught:
            SessionRecords(other).read(query=" literal ", cursor=cursor, max_output_chars=2000)
        assert caught.value.code == "record_cursor_invalid"
    with closing(Store(tmp_path / "records.sqlite3")) as reopened:
        page = SessionRecords(reopened).read(
            query=" literal ", cursor=cursor, max_output_chars=2000
        )
        assert [item["id"] for item in page["items"]] == ["a"]


@pytest.mark.parametrize("query", [True, None, 1, "x" * 121, "\ud800"])
def test_invalid_query_is_a_domain_error(store: Store, query: Any) -> None:
    with pytest.raises(EvidenceError) as caught:
        SessionRecords(store).read(query=query, max_output_chars=1000)
    assert caught.value.code == "record_query_invalid"


@pytest.mark.parametrize("limit", [True, 0, -1, 101, 1.0, "1"])
def test_invalid_limit_is_a_domain_error(store: Store, limit: Any) -> None:
    with pytest.raises(EvidenceError) as caught:
        SessionRecords(store).read(limit=limit, max_output_chars=1000)
    assert caught.value.code == "record_limit_invalid"


@pytest.mark.parametrize("budget", [True, 0, -1, 1.0, 1 << 80])
def test_invalid_budget_does_not_overflow_sqlite(store: Store, budget: Any) -> None:
    with pytest.raises(EvidenceError) as caught:
        SessionRecords(store).read(max_output_chars=budget)
    assert caught.value.code == "invalid_arguments"


@pytest.mark.parametrize("cursor", ["", "bad-token", True, 1, "非 ASCII"])
def test_invalid_cursor_encoding_is_a_domain_error(store: Store, cursor: Any) -> None:
    with pytest.raises(EvidenceError) as caught:
        SessionRecords(store).read(cursor=cursor, max_output_chars=1000)
    assert caught.value.code == "record_cursor_invalid"


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"created": True},
        {"created": -1},
        {"created": 1 << 80},
        {"created": float("inf")},
        {"identifier": ""},
        {"identifier": "\ud800"},
    ],
)
def test_cursor_payload_rejects_ambiguous_or_out_of_range_keys(
    store: Store, change: dict[str, Any]
) -> None:
    values = {"version": 1, "created": 1, "identifier": "record", **change}
    raw = json.dumps(
        [values["version"], store.instance_id, "", values["created"], values["identifier"]]
    ).encode()
    token = base64.urlsafe_b64encode(raw).decode()
    with pytest.raises(EvidenceError) as caught:
        SessionRecords(store).read(cursor=token, max_output_chars=1000)
    assert caught.value.code == "record_cursor_invalid"


@pytest.mark.parametrize("field", ["id", "title", "status"])
def test_first_oversized_metadata_is_neither_truncated_nor_skipped(
    store: Store, field: str
) -> None:
    large = "中文🙂" * 2000
    save(
        store, large if field == "id" else "newest", 2, **({field: large} if field != "id" else {})
    )
    save(store, "older", 1, title="可读取")
    reader = SessionRecords(store)
    with pytest.raises(EvidenceError) as caught:
        reader.read(max_output_chars=1000)
    assert caught.value.code == "output_budget"
    assert [item["id"] for item in all_records(reader, query="可读取", budget=1000)] == ["older"]


def test_oversized_later_record_returns_a_real_prefix_then_fails_explicitly(store: Store) -> None:
    save(store, "first", 3)
    save(store, "large", 2, title="大" * 10000)
    save(store, "last", 1)
    reader = SessionRecords(store)
    page = reader.read(max_output_chars=1000)
    assert [item["id"] for item in page["items"]] == ["first"]
    assert page["has_more"] and page["next_cursor"]
    with pytest.raises(EvidenceError) as caught:
        reader.read(cursor=page["next_cursor"], max_output_chars=1000)
    assert caught.value.code == "output_budget"


@pytest.mark.parametrize("field", ["title", "status"])
def test_non_text_visible_metadata_is_explicitly_invalid(store: Store, field: str) -> None:
    save(store, "malformed", **{field: {"unexpected": "value"}})
    with pytest.raises(EvidenceError) as caught:
        SessionRecords(store).read(max_output_chars=2000)
    assert caught.value.code == "record_metadata_invalid"


def test_eof_and_empty_page_use_the_exact_final_json_budget(store: Store) -> None:
    reader = SessionRecords(store)
    empty = reader.read(max_output_chars=1000)
    assert empty == {"items": [], "next_cursor": None, "has_more": False}
    assert reader.read(max_output_chars=json_size(empty)) == empty
    with pytest.raises(EvidenceError) as caught:
        reader.read(max_output_chars=json_size(empty) - 1)
    assert caught.value.code == "output_budget"
    save(store, "only", title='中文🙂\0"\\\n' * 20)
    page = reader.read(max_output_chars=sys.maxsize - 1)
    assert reader.read(max_output_chars=json_size(page)) == page
    with pytest.raises(EvidenceError) as caught:
        reader.read(max_output_chars=json_size(page) - 1)
    assert caught.value.code == "output_budget"


def test_sql_materializes_only_bounded_metadata_not_other_session_fields(store: Store) -> None:
    save(store, "first", 2, unrelated={"provider_data": "body" * 50000})
    save(store, "large", 1, title="长" * 10000)
    received: list[tuple[str, int]] = []

    def observe(cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> sqlite3.Row:
        received.extend(
            (description[0], len(value))
            for description, value in zip(cursor.description, values, strict=True)
            if isinstance(value, (str, bytes))
        )
        return sqlite3.Row(cursor, values)

    store.db.row_factory = observe
    try:
        page = SessionRecords(store).read(max_output_chars=1000)
        assert [item["id"] for item in page["items"]] == ["first"]
        assert received and max(length for _, length in received) <= 4000
        assert "data" not in {field for field, _ in received}
    finally:
        store.db.row_factory = sqlite3.Row


def test_creation_index_seeks_anchor_without_sorting_the_bounded_id_projection(
    store: Store,
) -> None:
    for index in range(300):
        save(store, f"record-{index:03d}", index // 5 + 1)
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    try:
        reader = SessionRecords(store)
        first = reader.read(limit=25, max_output_chars=32000)
        second = reader.read(cursor=first["next_cursor"], limit=25, max_output_chars=32000)
    finally:
        store.db.set_trace_callback(None)
    assert [item["id"] for item in second["items"]] == [
        f"record-{index:03d}" for index in reversed(range(250, 275))
    ]
    queries = [
        statement for statement in statements if "FROM objects WHERE kind='session'" in statement
    ]
    assert len(queries) == 2
    for statement in queries:
        plan = " ".join(row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + statement))
        assert "sessions_created_directory" in plan
        assert "TEMP B-TREE" not in plan
    second_plan = " ".join(row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + queries[1]))
    assert "<expr><?" in second_plan
