"""Real Store/SQLite history lookup, without reader or guard substitutes."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from muli import history_find
from muli.config import ConfigFile
from muli.evidence import EvidenceError, json_size
from muli.history_evidence import HistoryEvidence
from muli.history_find import HistoryFind
from muli.store import Store, new_id


@dataclass
class Archive:
    store: Store
    session: str
    config: ConfigFile

    def message(self, content: str, **extra: Any) -> dict[str, Any]:
        return self.store.put(
            "message",
            {
                "id": new_id(),
                "session_id": self.session,
                "role": "tool",
                "content": content,
                "source": "web",
                "principal": "owner",
                "content_provenance": "accepted",
                **extra,
            },
        )

    def find(self, query: str, **arguments: Any) -> dict[str, Any]:
        limits = self.config.settings.limits
        options = {
            "max_output_chars": limits.max_output_chars,
            "max_context_chars": limits.max_context_chars,
            "max_results": limits.max_search_results,
            "deadline": time.monotonic() + limits.max_search_seconds,
            **arguments,
        }
        return HistoryFind(self.store).find(self.session, query, **options)

    def size(self, identifier: str) -> int:
        row = self.store.db.execute(
            "SELECT length(CAST(data AS BLOB)) FROM objects WHERE kind='message' AND id=?",
            (identifier,),
        ).fetchone()
        return int(row[0])


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    store = Store(tmp_path / "history.sqlite3")
    session = new_id()
    store.put("session", {"id": session, "title": "明确选择的历史", "mode": "yolo"})
    try:
        yield Archive(store, session, ConfigFile(tmp_path / "config.json"))
    finally:
        store.close()


def pages(archive: Archive, query: str, **arguments: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(40):
        result = archive.find(query, cursor=cursor, **arguments)
        results.append(result)
        if not result["has_more"]:
            return results
        assert result["next_cursor"] and result["next_cursor"] != cursor
        cursor = result["next_cursor"]
    pytest.fail("bounded fixture did not reach EOF within its finite expected pages")


def test_exact_utf8_overlaps_are_coordinates_for_the_real_reader_and_never_cross_messages(
    archive: Archive,
) -> None:
    original = '前🙂a🙂a🙂a后\n"\\'
    message = archive.message(original)
    archive.message("ab")
    archive.message("cd")
    result = archive.find("🙂a🙂")
    assert [(item["message_id"], item["offset_bytes"]) for item in result["items"]] == [
        (message["id"], len("前".encode())),
        (message["id"], len("前🙂a".encode())),
    ]
    reader = HistoryEvidence(archive.store)
    for item in result["items"]:
        assert item["length_bytes"] == len("🙂a🙂".encode())
        assert reader.read_content(**item["read_arguments"], max_output_chars=2400)[
            "content"
        ].startswith("🙂a🙂")
        assert item["role"] == "tool" and item["principal"] == "owner"
        assert item["content_provenance"] == "accepted" and not item["revision_is_legacy"]
    assert archive.find("bc")["items"] == []
    escaped = archive.find('\n"\\')["items"][0]
    assert escaped["offset_bytes"] == len(original.removesuffix('\n"\\').encode())


def test_full_json_fit_replays_the_first_undelivered_match_without_loss(archive: Archive) -> None:
    messages = [archive.message("aaaa", source="source-" + "s" * 120) for _ in range(3)]
    result_pages = pages(archive, "aa", max_output_chars=1800, limit=100)
    found = [item for result in result_pages for item in result["items"]]
    assert [(item["message_id"], item["offset_bytes"]) for item in found] == [
        (message["id"], offset) for message in reversed(messages) for offset in (0, 1, 2)
    ]
    assert any(result["stopped"] == "output_budget" for result in result_pages)
    assert all(json_size(result) <= 1800 for result in result_pages)
    assert result_pages[-1]["has_more"] is False


def test_limit_cursor_reloads_a_long_but_capped_message_without_skipping_overlaps(
    archive: Archive,
) -> None:
    message = archive.message("前🙂" + "aaaaaa" + "后🙂" * 100)
    result_pages = pages(archive, "aa", limit=1)
    items = [item for result in result_pages for item in result["items"]]
    beginning = len("前🙂".encode())
    assert [item["offset_bytes"] for item in items] == list(range(beginning, beginning + 5))
    assert all(item["message_id"] == message["id"] for item in items)
    assert all(result["json_bytes"] == archive.size(message["id"]) for result in result_pages[:-1])


def test_raw_budget_uses_actual_json_size_and_remaining_budget_and_can_resume(
    archive: Archive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = archive.message("older " * 40)
    newer = archive.message("newer " * 40)
    sizes = [archive.size(message["id"]) for message in (newer, older)]
    context = (max(sizes) + min(sizes) // 2 + 3) // 4
    raw_budget = context * 4
    assert max(sizes) <= raw_budget < sum(sizes)
    reads: list[tuple[str, int]] = []
    bounded = archive.store.bounded_object

    def observed(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
        reads.append((identifier, max_bytes))
        return bounded(kind, identifier, max_bytes=max_bytes)

    monkeypatch.setattr(archive.store, "bounded_object", observed)
    first = archive.find("absent", max_context_chars=context, max_results=2)
    assert first["items"] == [] and first["stopped"] == "raw_budget" and first["has_more"]
    assert first["json_bytes"] == sizes[0] and first["scanned"] == 2
    assert reads == [(newer["id"], raw_budget)]
    second = archive.find(
        "absent", cursor=first["next_cursor"], max_context_chars=context, max_results=2
    )
    assert second["has_more"] is False and second["json_bytes"] == sizes[1]
    assert reads[-1] == (older["id"], raw_budget)
    reads.clear()
    full_context = (sum(sizes) + 3) // 4
    whole = archive.find("absent", max_context_chars=full_context, max_results=2)
    assert whole["json_bytes"] == sum(sizes) <= full_context * 4
    assert reads == [
        (newer["id"], full_context * 4),
        (older["id"], full_context * 4 - sizes[0]),
    ]


def test_provider_payload_not_content_length_controls_oversized_omission(
    archive: Archive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = archive.message("needle")
    huge = archive.message("needle", provider_data={"opaque": "x" * 20000})
    reads: list[str] = []
    bounded = archive.store.bounded_object

    def observed(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
        reads.append(identifier)
        return bounded(kind, identifier, max_bytes=max_bytes)

    monkeypatch.setattr(archive.store, "bounded_object", observed)
    result = archive.find("needle", max_context_chars=1024, max_results=2)
    assert result["omitted"]["oversized"] == result["omitted_total"] == 1
    assert [item["message_id"] for item in result["items"]] == [older["id"]]
    assert result["json_bytes"] == archive.size(older["id"])
    assert huge["id"] not in reads and result["has_more"] is False


def test_empty_candidate_pages_count_broken_mappings_before_json_filtering(
    archive: Archive,
) -> None:
    older = archive.message("needle")
    broken = [archive.message("unused") for _ in range(3)]
    with archive.store.db:
        for message in broken:
            archive.store.db.execute(
                "UPDATE objects SET data='not valid json' WHERE kind='message' AND id=?",
                (message["id"],),
            )
    statements: list[str] = []
    archive.store.db.set_trace_callback(statements.append)
    try:
        first = archive.find("needle", max_results=2)
    finally:
        archive.store.db.set_trace_callback(None)
    mappings = [statement for statement in statements if statement.startswith("SELECT seq,")]
    assert len(mappings) == 1 and "LIMIT 3" in mappings[0]
    assert "json_" not in mappings[0] and "JOIN" not in mappings[0]
    assert first["items"] == [] and first["scanned"] == 2 and first["has_more"]
    assert first["omitted"]["invalid_message"] == 2
    second = archive.find("needle", cursor=first["next_cursor"], max_results=2)
    assert [item["message_id"] for item in second["items"]] == [older["id"]]
    assert second["omitted_total"] == 3 and not second["has_more"]


@pytest.mark.parametrize(
    "damage", ["mapping_id", "long_mapping_id", "missing", "wrong_session", "duplicate_content"]
)
def test_bad_mapping_or_record_is_reported_and_never_turns_into_a_match(
    archive: Archive,
    damage: str,
) -> None:
    good = archive.message("needle")
    broken = archive.message("needle")
    with archive.store.db:
        if damage in {"mapping_id", "long_mapping_id"}:
            excessive = "x" * (archive.config.settings.limits.max_output_chars * 4 + 1)
            archive.store.db.execute(
                "UPDATE message_order SET message_id=? WHERE seq=?",
                (b"invalid-utf8-\xff" if damage == "mapping_id" else excessive, broken["seq"]),
            )
        elif damage == "missing":
            archive.store.db.execute(
                "DELETE FROM objects WHERE kind='message' AND id=?", (broken["id"],)
            )
        elif damage == "wrong_session":
            archive.store.db.execute(
                "UPDATE objects SET data=json_set(data,'$.session_id','unrelated') WHERE kind='message' AND id=?",
                (broken["id"],),
            )
        else:
            raw = json.dumps(broken, ensure_ascii=False)
            archive.store.db.execute(
                "UPDATE objects SET data=? WHERE kind='message' AND id=?",
                ('{"content":"no match",' + raw[1:], broken["id"]),
            )
    result = archive.find("needle", max_results=2)
    assert [item["message_id"] for item in result["items"]] == [good["id"]]
    assert result["scanned"] == 2 and result["omitted_total"] == 1
    assert sum(result["omitted"].values()) == 1
    assert result["has_more"] is False


def test_through_seq_freezes_the_upper_mapping_boundary_and_finished_anchor_need_not_exist(
    archive: Archive,
) -> None:
    target = archive.message("needle")
    newest = archive.message("not matching")
    first = archive.find("needle", max_results=1)
    assert first["items"] == [] and first["through_seq"] == newest["seq"]
    inserted = archive.message("needle")
    with archive.store.db:
        archive.store.db.execute("DELETE FROM message_order WHERE seq=?", (newest["seq"],))
    second = archive.find("needle", cursor=first["next_cursor"], max_results=1)
    assert [item["message_id"] for item in second["items"]] == [target["id"]]
    fresh = archive.find("needle")
    assert [item["message_id"] for item in fresh["items"]] == [inserted["id"], target["id"]]


@pytest.mark.parametrize("mutation", ["normal_put", "same_revision", "legacy", "delete"])
def test_current_message_mutation_or_deletion_invalidates_undelivered_matches(
    archive: Archive,
    mutation: str,
) -> None:
    message = archive.message("aaaa")
    if mutation == "legacy":
        with archive.store.db:
            archive.store.db.execute(
                "UPDATE objects SET data=json_remove(data,'$.evidence_revision') WHERE kind='message' AND id=?",
                (message["id"],),
            )
    first = archive.find("aa", limit=1)
    if mutation == "normal_put":
        archive.store.put("message", {**message, "content": "changed aaaa"})
    else:
        with archive.store.db:
            if mutation == "delete":
                archive.store.db.execute(
                    "DELETE FROM message_order WHERE message_id=?", (message["id"],)
                )
            else:
                archive.store.db.execute(
                    "UPDATE objects SET data=json_set(data,'$.content','changed aaaa') WHERE kind='message' AND id=?",
                    (message["id"],),
                )
    if mutation == "legacy":
        hit = first["items"][0]
        assert hit["revision_is_legacy"]
        # The existing reader's old legacy token cannot promise this detection.
        read = HistoryEvidence(archive.store).read_content(
            **hit["read_arguments"], max_output_chars=2400
        )
        assert read["content"].startswith("changed")
    with pytest.raises(EvidenceError) as rejected:
        archive.find("aa", cursor=first["next_cursor"], limit=1)
    assert rejected.value.code == "history_find_changed"


def test_cursor_binds_query_session_and_instance_without_discovering_sessions(
    archive: Archive,
    tmp_path: Path,
) -> None:
    archive.message("aaaa")
    first = archive.find("aa", limit=1)
    with pytest.raises(EvidenceError) as wrong_query:
        archive.find("aA", cursor=first["next_cursor"])
    assert wrong_query.value.code == "history_find_cursor_invalid"
    other_id = new_id()
    archive.store.put("session", {"id": other_id})
    other_service = HistoryFind(archive.store)
    limits = archive.config.settings.limits
    arguments = {
        "cursor": first["next_cursor"],
        "max_output_chars": limits.max_output_chars,
        "max_context_chars": limits.max_context_chars,
        "max_results": limits.max_search_results,
        "deadline": time.monotonic() + limits.max_search_seconds,
    }
    with pytest.raises(EvidenceError) as wrong_session:
        other_service.find(other_id, "aa", **arguments)
    assert wrong_session.value.code == "history_find_cursor_invalid"
    other = Store(tmp_path / "other.sqlite3")
    try:
        other.put("session", {"id": archive.session})
        with pytest.raises(EvidenceError) as wrong_instance:
            HistoryFind(other).find(archive.session, "aa", **arguments)
        assert wrong_instance.value.code == "history_find_cursor_invalid"
    finally:
        other.close()


def test_small_output_fails_instead_of_returning_a_nonadvancing_empty_cursor(
    archive: Archive,
) -> None:
    archive.message("needle")
    with pytest.raises(EvidenceError) as rejected:
        archive.find("needle", max_output_chars=80)
    assert rejected.value.code == "output_budget"


def test_deadline_before_work_or_after_real_object_loading_never_claims_eof(
    archive: Archive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive.message("needle")
    now = [100.0]
    monkeypatch.setattr(history_find.time, "monotonic", lambda: now[0])
    with pytest.raises(EvidenceError) as before:
        archive.find("needle", deadline=99.0)
    assert before.value.code == "history_find_timeout"
    bounded = archive.store.bounded_object

    def observed(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
        result = bounded(kind, identifier, max_bytes=max_bytes)
        now[0] = 101.0
        return result

    monkeypatch.setattr(archive.store, "bounded_object", observed)
    with pytest.raises(EvidenceError) as after:
        archive.find("needle", deadline=100.5)
    assert after.value.code == "history_find_timeout" and not archive.store.db.in_transaction


def test_deadline_after_committed_progress_returns_a_live_continuation(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = archive.message("needle")
    newer = archive.message("quiet")
    now = [100.0]
    monkeypatch.setattr(history_find.time, "monotonic", lambda: now[0])

    def observed(statement: str) -> None:
        if statement.startswith("SELECT typeof(data)") and older["id"] in statement:
            now[0] = 101.0

    archive.store.db.set_trace_callback(observed)
    try:
        first = archive.find("needle", deadline=100.5)
    finally:
        archive.store.db.set_trace_callback(None)
    assert first["items"] == [] and first["stopped"] == "deadline" and first["has_more"]
    assert first["json_bytes"] == archive.size(newer["id"])
    now[0] = 100.0
    second = archive.find("needle", cursor=first["next_cursor"], deadline=100.5)
    assert [item["message_id"] for item in second["items"]] == [older["id"]]


def test_queries_never_write_create_identity_or_take_over_the_callers_transaction(
    archive: Archive,
) -> None:
    archive.message("needle")
    changes = archive.store.db.total_changes
    archive.find("needle")
    assert archive.store.db.total_changes == changes and not archive.store.db.in_transaction
    archive.store.db.execute("BEGIN")
    archive.store.db.execute(
        "UPDATE objects SET data=json_set(data,'$.title','uncommitted') WHERE kind='session' AND id=?",
        (archive.session,),
    )
    try:
        with pytest.raises(EvidenceError) as rejected:
            archive.find("needle")
        assert rejected.value.code == "history_find_transaction_active"
        assert archive.store.db.in_transaction
    finally:
        archive.store.db.rollback()
    with archive.store.db:
        archive.store.db.execute("DELETE FROM runtime_state WHERE name='store_instance_v1'")
    changes = archive.store.db.total_changes
    with pytest.raises(EvidenceError) as missing_identity:
        archive.find("needle")
    assert missing_identity.value.code == "history_find_identity_unavailable"
    assert archive.store.db.total_changes == changes
    assert (
        archive.store.db.execute(
            "SELECT 1 FROM runtime_state WHERE name='store_instance_v1'"
        ).fetchone()
        is None
    )


def test_large_inputs_and_bad_types_fail_before_raw_object_loading(
    archive: Archive,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str] = []
    bounded = archive.store.bounded_object

    def observed(kind: str, identifier: str, *, max_bytes: int) -> dict[str, Any] | None:
        reads.append(identifier)
        return bounded(kind, identifier, max_bytes=max_bytes)

    monkeypatch.setattr(archive.store, "bounded_object", observed)
    for query, arguments in (
        ("x" * 121, {}),
        ("needle", {"cursor": "a" * 10001, "max_output_chars": 1000}),
        ("needle", {"limit": True}),
        ("needle", {"deadline": float("nan")}),
    ):
        with pytest.raises(EvidenceError):
            archive.find(query, **arguments)
    limits = archive.config.settings.limits
    with pytest.raises(EvidenceError) as large_session:
        HistoryFind(archive.store).find(
            "s" * 4001,
            "needle",
            max_output_chars=1000,
            max_context_chars=limits.max_context_chars,
            max_results=limits.max_search_results,
            deadline=time.monotonic() + limits.max_search_seconds,
        )
    assert large_session.value.code == "output_budget"
    assert reads == [] and not archive.store.db.in_transaction


@pytest.mark.parametrize("budget", ["raw", "output"])
def test_current_cursor_budget_reduction_fails_closed_instead_of_omitting_or_stalling(
    archive: Archive,
    budget: str,
) -> None:
    message = archive.message("aaaa" + "普通长原文🙂" * 300)
    first = archive.find("aa", limit=1, max_results=2, max_output_chars=2400)
    assert first["has_more"] and first["items"][0]["message_id"] == message["id"]
    arguments: dict[str, Any] = {"max_results": 2, "max_output_chars": 2400}
    if budget == "raw":
        arguments["max_context_chars"] = max(1, (archive.size(message["id"]) - 1) // 4)
    else:
        # Even the opaque cursor no longer fits: reject before decoding it,
        # preserving the difference between low budget and a wrong identity.
        arguments["max_output_chars"] = 80
    with pytest.raises(EvidenceError) as rejected:
        archive.find("aa", cursor=first["next_cursor"], limit=1, **arguments)
    assert rejected.value.code == "output_budget"


def test_escaped_top_level_content_alias_is_rejected_instead_of_disagreeing_with_reader(
    archive: Archive,
) -> None:
    good = archive.message("needle")
    broken = archive.message("needle")
    raw = json.dumps(broken, ensure_ascii=False)
    duplicated = r'{"\u0063ontent":"not the selected text",' + raw[1:]
    with archive.store.db:
        archive.store.db.execute(
            "UPDATE objects SET data=? WHERE kind='message' AND id=?",
            (duplicated, broken["id"]),
        )
    result = archive.find("needle", max_results=2)
    assert [item["message_id"] for item in result["items"]] == [good["id"]]
    assert result["omitted"]["invalid_message"] == result["omitted_total"] == 1
    assert result["scanned"] == 2 and not result["has_more"]


@pytest.mark.parametrize("damage", ["surrogate", "nan", "infinity", "deep"])
def test_store_rejects_raw_json_damage_as_an_omission_and_fences_current_cursor(
    archive: Archive,
    damage: str,
) -> None:
    good = archive.message("needle")
    broken = archive.message("aaaa")
    current = archive.find("aa", limit=1)
    raw = json.dumps(broken, ensure_ascii=False)
    if damage == "surrogate":
        damaged = raw[:-1] + r',"extra":"\ud800"}'
    elif damage == "nan":
        damaged = raw[:-1] + ',"extra":NaN}'
    elif damage == "infinity":
        damaged = raw[:-1] + ',"extra":Infinity}'
    else:
        damaged = raw[:-1] + ',"extra":' + "[" * 1100 + "0" + "]" * 1100 + "}"
    with archive.store.db:
        archive.store.db.execute(
            "UPDATE objects SET data=? WHERE kind='message' AND id=?",
            (damaged, broken["id"]),
        )
    result = archive.find("needle")
    assert [item["message_id"] for item in result["items"]] == [good["id"]]
    assert result["omitted"]["invalid_message"] == 1 and result["omitted_total"] == 1
    assert result["json_bytes"] == len(damaged.encode()) + archive.size(good["id"])
    with pytest.raises(EvidenceError) as rejected:
        archive.find("aa", cursor=current["next_cursor"], limit=1)
    assert rejected.value.code == "history_find_changed"


def test_mapping_window_optional_bounds_preserve_old_output_and_keep_bad_rows(
    archive: Archive,
) -> None:
    oldest = archive.message("old")
    middle = archive.message("middle")
    newest = archive.message("new")
    with archive.store.db:
        archive.store.db.execute(
            "UPDATE message_order SET message_id=? WHERE seq=?",
            (b"invalid-utf8-\xff", middle["seq"]),
        )
    old_contract = archive.store.message_evidence_window(
        archive.session, limit=3, metadata_bytes=64
    )
    assert old_contract == [
        {"seq": newest["seq"], "message_id": newest["id"]},
        {"seq": middle["seq"], "message_id": None},
        {"seq": oldest["seq"], "message_id": oldest["id"]},
    ]
    assert (
        archive.store.message_evidence_window(
            archive.session,
            limit=3,
            metadata_bytes=64,
            through_seq=middle["seq"],
        )
        == old_contract[1:]
    )
    assert (
        archive.store.message_evidence_window(
            archive.session,
            limit=3,
            metadata_bytes=64,
            through_seq=newest["seq"],
            before_seq=middle["seq"],
        )
        == old_contract[2:]
    )
    assert (
        archive.store.message_evidence_window(
            archive.session,
            limit=3,
            metadata_bytes=64,
            through_seq=0,
        )
        == []
    )


def test_mapping_window_rejects_invalid_bounds_without_changing_old_callers(
    archive: Archive,
) -> None:
    for bounds in (
        {"through_seq": True},
        {"through_seq": -1},
        {"before_seq": 0},
        {"before_seq": False},
    ):
        with pytest.raises(ValueError):
            archive.store.message_evidence_window(
                archive.session, limit=1, metadata_bytes=64, **bounds
            )
    assert archive.store.message_evidence_window(archive.session, limit=1, metadata_bytes=64) == []
