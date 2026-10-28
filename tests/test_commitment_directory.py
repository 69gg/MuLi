"""Real SQLite directory windows, complete JSON gates and resumable metadata pages."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from collections.abc import Generator, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from muli import commitment_directory
from muli import store as store_module
from muli.commitment_directory import CommitmentDirectory
from muli.commitments import STATES, Commitments
from muli.config import Limits
from muli.events import EventBus
from muli.evidence import EvidenceError, json_size
from muli.store import Store


@dataclass
class Ledger:
    store: Store
    commitments: Commitments

    def put(self, identifier: str, **fields: Any) -> dict[str, Any]:
        return self.store.put(
            "commitment", {"id": identifier, "title": identifier, "status": "open", **fields}
        )

    def directory(self, **arguments: Any) -> dict[str, Any]:
        limits = Limits()
        options = {
            "max_output_chars": limits.max_output_chars,
            "max_results": limits.max_search_results,
            "max_context_chars": limits.max_context_chars,
            "deadline": time.monotonic() + limits.max_search_seconds,
            **arguments,
        }
        return CommitmentDirectory(
            self.store,
            lambda item, maximum: self.commitments.metadata(item, max_output_chars=maximum),
            STATES,
        ).directory(**options)

    def size(self, identifier: str) -> int:
        row = self.store.db.execute(
            "SELECT length(CAST(data AS BLOB)) FROM objects WHERE kind='commitment' AND id=?",
            (identifier,),
        ).fetchone()
        return int(row[0])


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    store = Store(tmp_path / "ledger.sqlite3")
    try:
        yield Ledger(store, Commitments(store, EventBus(store)))
    finally:
        store.close()


def pages(ledger: Ledger, **arguments: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(40):
        result = ledger.directory(cursor=cursor, **arguments)
        results.append(result)
        if not result["has_more"]:
            assert result["next_cursor"] is None and result["stopped"] == "end"
            return results
        assert result["next_cursor"] and result["next_cursor"] != cursor
        cursor = result["next_cursor"]
    pytest.fail("有限测试目录未在预期页数内到尾")


def test_empty_middle_pages_charge_candidates_bad_json_and_mismatched_status_before_filtering(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = ledger.put("a", title="目标")
    done = ledger.put("v", status="done", evidence="有验证证据")
    invalid = ledger.put("w", title=["不是元数据文字"])
    broken = ledger.put("x")
    huge_plan = ledger.put(
        "y", control={"epoch": 0, "approved_epoch": 0, "plan": {"text": "plan-private" * 2000}}
    )
    huge_brief = ledger.put("z", brief={"goal": "brief-private" * 2000})
    with ledger.store.db:
        ledger.store.db.execute(
            "UPDATE objects SET data='broken json' WHERE kind='commitment' AND id=?",
            (broken["id"],),
        )
    bounded = ledger.store.bounded_object
    reads: list[str] = []

    def observed(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
        reads.append(identifier)
        return bounded(kind, identifier, max_bytes=max_bytes)

    monkeypatch.setattr(ledger.store, "bounded_object", observed)
    statements: list[str] = []
    ledger.store.db.set_trace_callback(statements.append)
    changes = ledger.store.db.total_changes
    try:
        results = pages(ledger, max_results=2, max_context_chars=512)
    finally:
        ledger.store.db.set_trace_callback(None)
    assert len(results) == 3
    assert results[0]["items"] == results[1]["items"] == []
    assert results[0]["omitted"] == {"oversized": 2, "invalid": 0, "missing": 0}
    assert results[1]["omitted"]["invalid"] == 2
    assert [item["id"] for item in results[2]["items"]] == [older["id"]]
    assert results[2]["omitted_total"] == 4
    assert all(result["scanned"] == 2 for result in results)
    assert all(result["stopped"] == "candidate_budget" for result in results[:2])
    assert reads == [broken["id"], invalid["id"], done["id"], older["id"]]
    assert huge_plan["id"] not in reads and huge_brief["id"] not in reads
    assert sum(result["json_bytes"] for result in results) == sum(
        ledger.size(item["id"]) for item in (broken, invalid, done, older)
    )
    windows = [
        statement for statement in statements if statement.startswith("SELECT CASE WHEN typeof(id)")
    ]
    assert len(windows) == 3
    assert all("LIMIT 3" in statement and "json_" not in statement for statement in windows)
    assert all("JOIN" not in statement for statement in windows)
    assert ledger.store.db.total_changes == changes and not ledger.store.db.in_transaction


def test_bounded_object_sql_gate_never_decodes_an_oversized_complete_json(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = ledger.put("huge", brief={"goal": "private" * 5000})
    loads = store_module.json.loads
    decoded: list[str | bytes | bytearray] = []

    def observed(raw: str | bytes | bytearray, **options: Any) -> Any:
        decoded.append(raw)
        return loads(raw, **options)

    monkeypatch.setattr(store_module.json, "loads", observed)
    with pytest.raises(EvidenceError) as caught:
        ledger.store.bounded_object("commitment", huge["id"], max_bytes=128)
    assert caught.value.code == "output_budget" and decoded == []


def test_existing_search_candidate_limit_is_independent_of_the_100_item_page_cap(
    ledger: Ledger,
) -> None:
    ledger.put("a")
    ledger.store.put_many(
        [
            ("commitment", {"id": f"task-{index:03d}", "title": "已完成", "status": "done"})
            for index in range(130)
        ]
    )
    first = ledger.directory(max_results=130)
    assert first["items"] == [] and first["scanned"] == 130
    assert first["has_more"] and first["stopped"] == "candidate_budget"
    assert first["omitted_total"] == 0
    final = ledger.directory(cursor=first["next_cursor"], max_results=130)
    assert [item["id"] for item in final["items"]] == ["a"] and not final["has_more"]


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "unknown"},
        {"revision": True},
        {"revision": 0},
        {"created_at": float("inf")},
        {"title": {"nested": "private"}},
        {"control": {"epoch": "1", "approved_epoch": 0}},
        {"has_brief": "yes"},
    ],
)
def test_invalid_public_metadata_is_counted_without_becoming_a_projection(
    ledger: Ledger, fields: dict[str, Any]
) -> None:
    item = ledger.put("task", **fields)
    result = ledger.directory(status="all")
    assert result["items"] == [] and result["scanned"] == 1
    assert result["json_bytes"] == ledger.size(item["id"])
    assert result["omitted"]["invalid"] == result["omitted_total"] == 1
    assert result["has_more"] is False


def test_sql_gates_complete_keys_before_python_and_refuses_an_unseekable_giant_key(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.put("a")
    giant = "z" * 20000
    ledger.put(giant)
    window = list(ledger.store.commitment_directory_window(before_id=None, limit=2, key_bytes=64))
    assert window == [{"id": None}, {"id": b"a"}]
    reads: list[str] = []
    bounded = ledger.store.bounded_object

    def observed(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
        reads.append(identifier)
        return bounded(kind, identifier, max_bytes=max_bytes)

    monkeypatch.setattr(ledger.store, "bounded_object", observed)
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(max_context_chars=16, max_results=1)
    assert caught.value.code == "key_budget" and "未声称查全" in str(caught.value)
    assert reads == [] and not ledger.store.db.in_transaction


def test_lookahead_does_not_consume_or_decode_an_oversized_next_key(ledger: Ledger) -> None:
    newer = ledger.put("z")
    ledger.put("a" * 20000)
    first = ledger.directory(limit=1, max_results=1, max_context_chars=64)
    assert [item["id"] for item in first["items"]] == [newer["id"]]
    assert first["scanned"] == 1 and first["has_more"]
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(cursor=first["next_cursor"], max_context_chars=64)
    assert caught.value.code == "key_budget"


def test_limit_cursor_cannot_seek_past_a_giant_raw_key_between_valid_records(
    ledger: Ledger,
) -> None:
    ledger.put("a")
    ledger.put("m" * 20000)
    newer = ledger.put("z")
    first = ledger.directory(limit=1, max_results=1, max_context_chars=64)
    assert [item["id"] for item in first["items"]] == [newer["id"]]
    assert first["stopped"] == "limit" and first["has_more"]
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(cursor=first["next_cursor"], limit=1, max_results=1, max_context_chars=64)
    assert caught.value.code == "key_budget" and "未声称查全" in str(caught.value)


def test_invalid_utf8_key_stops_without_inventing_a_replacement_seek(ledger: Ledger) -> None:
    ledger.put("a")
    with ledger.store.db:
        ledger.store.db.execute(
            "INSERT INTO objects(kind,id,data,updated) "
            "VALUES('commitment',CAST(x'80' AS TEXT),'{}',0)"
        )
    with pytest.raises(EvidenceError) as caught:
        ledger.directory()
    assert caught.value.code == "commitment_directory_key_invalid"


def test_raw_budget_retries_the_first_unloaded_row_on_the_next_page(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    older, newer = [ledger.put(identifier, evidence="e" * 400) for identifier in ("a", "b")]
    sizes = [ledger.size(item["id"]) for item in (newer, older)]
    context = (max(sizes) + min(sizes) // 2 + 3) // 4
    assert max(sizes) <= context * 4 < sum(sizes)
    bounded = ledger.store.bounded_object
    reads: list[tuple[str, int]] = []

    def observed(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
        reads.append((identifier, max_bytes))
        return bounded(kind, identifier, max_bytes=max_bytes)

    monkeypatch.setattr(ledger.store, "bounded_object", observed)
    first = ledger.directory(max_context_chars=context, max_results=2)
    assert [item["id"] for item in first["items"]] == [newer["id"]]
    assert first["stopped"] == "raw_budget" and first["scanned"] == 2
    assert first["json_bytes"] == sizes[0] and reads == [(newer["id"], context * 4)]
    second = ledger.directory(cursor=first["next_cursor"], max_context_chars=context, max_results=2)
    assert [item["id"] for item in second["items"]] == [older["id"]]
    assert second["json_bytes"] == sizes[1] and second["has_more"] is False
    assert reads[-1] == (older["id"], context * 4)


def test_complete_output_shell_reserves_cursor_counts_and_escaped_metadata(ledger: Ledger) -> None:
    records = [ledger.put(f"task-{index}", title="\x01" * 239) for index in range(5)]
    results = pages(ledger, max_output_chars=900, max_results=5)
    found = [item for result in results for item in result["items"]]
    assert [item["id"] for item in found] == [item["id"] for item in reversed(records)]
    assert all(json_size(result) <= 900 for result in results)
    assert any(result["stopped"] == "output_budget" for result in results)
    assert all(item["details_omitted"] for item in found)


@pytest.mark.parametrize("output", [320, 750])
def test_tiny_output_or_unfittable_first_metadata_fails_explicitly(
    ledger: Ledger, output: int
) -> None:
    ledger.put("z" * 200)
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(max_output_chars=output)
    assert caught.value.code == "output_budget" and not ledger.store.db.in_transaction


def test_live_seek_survives_edit_and_anchor_deletion_without_repeating_consumed_ids(
    ledger: Ledger,
) -> None:
    for identifier in ("v", "w", "x", "y", "z"):
        ledger.put(identifier)
    first = ledger.directory(limit=2)
    assert [item["id"] for item in first["items"]] == ["z", "y"]
    ledger.put("z", title="已走过的编辑项")
    ledger.put("zz", title="插入在已走过位置")
    ledger.put("q", title="插入在未走到的位置")
    with ledger.store.db:
        ledger.store.db.execute("DELETE FROM objects WHERE kind='commitment' AND id IN ('x','y')")
    remaining = ledger.directory(cursor=first["next_cursor"], limit=100)
    assert [item["id"] for item in remaining["items"]] == ["w", "v", "q"]
    assert remaining["consistency"] == "bounded_live" and remaining["has_more"] is False
    assert [item["id"] for item in ledger.directory(limit=100)["items"]] == [
        "zz",
        "z",
        "w",
        "v",
        "q",
    ]


def test_same_database_reopen_preserves_cursor_but_other_database_and_status_refuse_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reopen.sqlite3"
    store = Store(path)
    ledger = Ledger(store, Commitments(store, EventBus(store)))
    ledger.put("a")
    ledger.put("z")
    cursor = ledger.directory(limit=1)["next_cursor"]
    store.close()
    reopened, other = Store(path), Store(tmp_path / "other.sqlite3")
    try:
        same = Ledger(reopened, Commitments(reopened, EventBus(reopened)))
        assert [item["id"] for item in same.directory(cursor=cursor)["items"]] == ["a"]
        for target, arguments in (
            (same, {"status": "all"}),
            (Ledger(other, Commitments(other, EventBus(other))), {}),
        ):
            with pytest.raises(EvidenceError) as caught:
                target.directory(cursor=cursor, **arguments)
            assert caught.value.code == "commitment_directory_cursor_invalid"
    finally:
        reopened.close()
        other.close()


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (0, True),
        (0, 2),
        (1, "history_content_find"),
        (2, "other"),
        (3, "done"),
        (4, ""),
        (4, 1),
        (5, -1),
        (5, True),
    ],
)
def test_cursor_rejects_changed_scope_version_or_invalid_position(
    ledger: Ledger, index: int, value: Any
) -> None:
    ledger.put("a")
    ledger.put("z")
    original = ledger.directory(limit=1)["next_cursor"]
    decoded = json.loads(base64.urlsafe_b64decode(original))
    decoded[index] = value
    cursor = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode()
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(cursor=cursor)
    assert caught.value.code == "commitment_directory_cursor_invalid"


@pytest.mark.parametrize("cursor", ["", "not base64", "W10=", 1, True, [], "x" * 10000])
def test_malformed_and_over_budget_cursor_is_refused(ledger: Ledger, cursor: Any) -> None:
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(cursor=cursor, max_output_chars=1000, max_context_chars=512)
    assert caught.value.code in {"commitment_directory_cursor_invalid", "output_budget"}


def test_existing_write_transaction_is_neither_committed_nor_rolled_back(ledger: Ledger) -> None:
    ledger.store.db.execute("BEGIN")
    ledger.store.db.execute(
        "INSERT INTO objects(kind,id,data,updated) VALUES('commitment','pending','{}',0)"
    )
    try:
        with pytest.raises(EvidenceError) as caught:
            ledger.directory()
        assert caught.value.code == "commitment_directory_transaction_active"
        assert ledger.store.db.in_transaction
        assert (
            ledger.store.db.execute("SELECT 1 FROM objects WHERE id='pending'").fetchone()
            is not None
        )
    finally:
        ledger.store.db.rollback()
    assert ledger.store.get("commitment", "pending") is None


def test_deadline_keeps_original_absolute_cutoff_and_delivers_only_finished_progress(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.put("a")
    ledger.put("z")
    expired = False
    window = ledger.store.commitment_directory_window

    def observed(
        *, before_id: str | None, limit: int, key_bytes: int
    ) -> Generator[dict[str, Any], None, None]:
        nonlocal expired
        with closing(window(before_id=before_id, limit=limit, key_bytes=key_bytes)) as rows:
            for index, row in enumerate(rows):
                if index:
                    expired = True
                yield row

    monkeypatch.setattr(ledger.store, "commitment_directory_window", observed)
    monkeypatch.setattr(commitment_directory.time, "monotonic", lambda: 2.0 if expired else 0.0)
    first = ledger.directory(deadline=1.0)
    assert [item["id"] for item in first["items"]] == ["z"]
    assert first["stopped"] == "deadline" and first["has_more"]
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(cursor=first["next_cursor"], deadline=1.0)
    assert caught.value.code == "commitment_directory_timeout"
    expired = False
    last = ledger.directory(cursor=first["next_cursor"], deadline=1.0)
    assert [item["id"] for item in last["items"]] == ["a"] and not last["has_more"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": True},
        {"limit": 101},
        {"limit": 0},
        {"max_results": False},
        {"status": []},
        {"deadline": float("nan")},
    ],
)
def test_argument_types_and_existing_page_limit_are_enforced(
    ledger: Ledger, arguments: dict[str, Any]
) -> None:
    with pytest.raises(EvidenceError) as caught:
        ledger.directory(**arguments)
    assert caught.value.code == "invalid_arguments"


async def test_explicit_directory_does_not_change_legacy_array_tools_or_metadata_default(
    ledger: Ledger,
) -> None:
    older, newer = ledger.put("a"), ledger.put("z")
    with ledger.store.db:
        ledger.store.db.executemany(
            "UPDATE objects SET updated=? WHERE kind='commitment' AND id=?", [(0, "a"), (1, "z")]
        )
    tools = {tool.name: tool for tool in ledger.commitments.tools()}
    legacy = await tools["commitment_list"].handler({})
    assert isinstance(legacy, list) and legacy == ledger.commitments.list()
    assert [item["id"] for item in legacy] == [newer["id"], older["id"]]
    assert set(tools["commitment_list"].parameters["properties"]) == {"status"}
    assert tools["commitment_directory"].preserve_output is True
    assert tools["commitment_directory"].risk == "read"
    page = await tools["commitment_directory"].handler({"limit": 1})
    assert page["has_more"] and [item["id"] for item in page["items"]] == [newer["id"]]
    assert ledger.commitments.metadata(newer) == legacy[0]
    with pytest.raises(ValueError, match="参数"):
        await tools["commitment_directory"].handler({"identifier": "a"})


@pytest.mark.parametrize("stop", ["limit", "output_budget", "exception"])
def test_early_return_and_retained_exception_close_the_real_sql_key_cursor(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    for identifier in ("a", "b", "c"):
        ledger.put(identifier, title="\x01" * 239)
    connection = ledger.store.db
    cursors: list[TrackedCursor] = []

    class TrackedCursor:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self.cursor, self.closed = cursor, False

        def __iter__(self) -> TrackedCursor:
            return self

        def __next__(self) -> sqlite3.Row:
            return next(self.cursor)

        def close(self) -> None:
            self.closed = True
            self.cursor.close()

    class ObservedConnection:
        @property
        def in_transaction(self) -> bool:
            return connection.in_transaction

        def execute(
            self, statement: str, values: tuple[Any, ...] = ()
        ) -> sqlite3.Cursor | TrackedCursor:
            cursor = connection.execute(statement, values)
            if statement.startswith("SELECT CASE WHEN typeof(id)"):
                tracked = TrackedCursor(cursor)
                cursors.append(tracked)
                return tracked
            return cursor

        def rollback(self) -> None:
            connection.rollback()

        def close(self) -> None:
            connection.close()

    monkeypatch.setattr(ledger.store, "db", ObservedConnection())
    if stop == "exception":

        def reject(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
            raise RuntimeError("保留异常及回溯以核查显式关闭")

        monkeypatch.setattr(ledger.store, "bounded_object", reject)
        with pytest.raises(RuntimeError) as caught:
            ledger.directory()
        assert caught.value.__traceback__ is not None
    else:
        arguments = {"limit": 1} if stop == "limit" else {"max_output_chars": 900}
        result = ledger.directory(**arguments)
        assert result["stopped"] == stop and result["has_more"]
    assert len(cursors) == 1 and cursors[0].closed
    assert not connection.in_transaction
