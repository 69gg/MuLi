from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.attachments import MAX_ATTACHMENT_IDS
from muli.history_evidence import EvidenceError, HistoryEvidence
from muli.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "journal.sqlite3")
    journal.put("session", {"id": "session", "title": "来源"})
    journal.put("session", {"id": "other", "title": "其它来源"})
    try:
        yield journal
    finally:
        journal.close()


def assert_code(code: str, action: Callable[[], object]) -> None:
    with pytest.raises(EvidenceError) as captured:
        action()
    assert captured.value.code == code
    assert captured.value.message == str(captured.value)
    assert len(str(captured.value)) < 300


def test_attachment_references_are_complete_in_directory_archive_and_sequence_read(
    store: Store,
) -> None:
    identifiers = [f"{number:032x}" for number in range(MAX_ATTACHMENT_IDS)]
    original = store.add_message("session", "user", "资料已交接", attachment_ids=identifiers)
    evidence = HistoryEvidence(store)
    for page in (
        evidence.read("session", max_output_chars=4000)["records"][0],
        evidence.archive("session", max_output_chars=4000)["messages"][0],
        evidence.read_content("session", seq=original["seq"], max_output_chars=4000),
    ):
        assert page["attachment_ids"] == identifiers
        assert "metadata_omitted" not in page
    legacy = store.add_message("session", "assistant", "旧消息没有引用")
    assert (
        evidence.read_content("session", seq=legacy["seq"], max_output_chars=1000)["attachment_ids"]
        == []
    )
    for action in (
        lambda: evidence.read("session", max_output_chars=1000),
        lambda: evidence.archive("session", before_seq=legacy["seq"], max_output_chars=1000),
        lambda: evidence.read_content("session", seq=original["seq"], max_output_chars=1000),
    ):
        assert_code("output_budget", action)


@pytest.mark.parametrize(
    "references",
    [None, "a" * 32, [1], [""], ["x" * 257], [f"{n:032x}" for n in range(MAX_ATTACHMENT_IDS + 1)]],
)
def test_invalid_attachment_evidence_is_never_an_empty_or_partial_success(
    store: Store, references: Any
) -> None:
    record = store.add_message("session", "user", "原消息", attachment_ids=references)
    evidence = HistoryEvidence(store)
    for action in (
        lambda: evidence.read("session", max_output_chars=4000),
        lambda: evidence.archive("session", max_output_chars=4000),
        lambda: evidence.read_content("session", seq=record["seq"], max_output_chars=4000),
    ):
        assert_code("content_unavailable", action)


def test_legacy_opaque_attachment_references_keep_original_order_and_duplicates(
    store: Store,
) -> None:
    references = ["plugin:document/旧资料", "UPPERCASE", "plugin:document/旧资料"]
    record = store.add_message("session", "user", "原有插件信封", attachment_ids=references)
    evidence = HistoryEvidence(store)
    assert (
        evidence.read("session", max_output_chars=2000)["records"][0]["attachment_ids"]
        == references
    )
    assert (
        evidence.read_content("session", seq=record["seq"], max_output_chars=2000)["attachment_ids"]
        == references
    )


def test_oversized_attachment_json_stays_a_bounded_sql_projection(store: Store) -> None:
    record = store.add_message("session", "user", "原消息", attachment_ids=["x" * 100_000])
    row = store.message_evidence_slice(
        "session",
        seq=record["seq"],
        message_id=None,
        offset_bytes=0,
        length_bytes=100,
        metadata_bytes=400,
    )
    assert row is not None
    assert len(row["attachment_ids"]) == 400
    assert row["attachment_ids_bytes"] > 100_000
    assert_code(
        "output_budget",
        lambda: HistoryEvidence(store).read_content(
            "session", seq=record["seq"], max_output_chars=1000
        ),
    )


@pytest.mark.parametrize("raw", ['["\\ud800"]', '["\\udfff"]', "[" * 900 + '"opaque"' + "]" * 900])
def test_sql_valid_but_unusable_attachment_json_is_a_fixed_error(store: Store, raw: str) -> None:
    # Raw SQL bypasses Python's serializer: SQLite's valid JSON can still contain
    # escaped lone surrogates or a nested array outside the envelope's shape.
    assert store.db.execute("SELECT json_valid(?)", (raw,)).fetchone()[0] == 1
    record = store.add_message("session", "user", "保留原消息")
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.attachment_ids',json(?)) "
            "WHERE kind='message' AND id=?",
            (raw, record["id"]),
        )
    row = store.message_evidence_slice(
        "session",
        seq=record["seq"],
        message_id=None,
        offset_bytes=0,
        length_bytes=100,
        metadata_bytes=4000,
    )
    assert row is not None and row["attachment_ids_type"] == "array"
    assert row["attachment_ids_bytes"] == len(row["attachment_ids"])
    evidence = HistoryEvidence(store)
    for action in (
        lambda: evidence.read("session", max_output_chars=4000),
        lambda: evidence.archive("session", max_output_chars=4000),
        lambda: evidence.read_content("session", seq=record["seq"], max_output_chars=4000),
    ):
        assert_code("content_unavailable", action)


def test_directory_keeps_small_content_and_existing_sequence_pagination(store: Store) -> None:
    first = store.add_message("session", "user", "第一条", source="channel:fake", principal="owner")
    store.add_message("other", "user", "不在此目录")
    second = store.add_message("session", "assistant", "第二条")
    evidence = HistoryEvidence(store)
    page = evidence.read("session", limit=1, max_output_chars=1000)
    record = page["records"][0]
    assert record["id"] == first["id"]
    assert record["content"] == "第一条" and not record["content_truncated"]
    assert record["source"] == "channel:fake" and record["principal"] == "owner"
    assert record["revision"] == first["evidence_revision"]
    assert record["content_provenance"] == "unknown"
    assert page["has_more"] and page["next_after"] == first["seq"]
    tail = evidence.read("session", after=page["next_after"], max_output_chars=1000)
    assert [item["id"] for item in tail["records"]] == [second["id"]]
    assert not tail["has_more"] and tail["next_after"] == second["seq"]


def test_first_long_record_keeps_location_and_does_not_hide_following_records(store: Store) -> None:
    long = store.add_message("session", "tool", "前" * 4000 + "中段证据" + "后" * 4000)
    short = store.add_message("session", "assistant", "后续消息")
    evidence = HistoryEvidence(store)
    page = evidence.read("session", max_output_chars=1000)
    assert len(json.dumps(page, ensure_ascii=False)) <= 1000
    assert page["records"][0]["id"] == long["id"]
    assert page["records"][0]["content_truncated"]
    assert page["records"][0]["content_bytes"] == len(long["content"].encode("utf-8"))
    assert [item["id"] for item in page["records"]] in [[long["id"]], [long["id"], short["id"]]]
    if page["has_more"]:
        following = evidence.read("session", after=page["next_after"], max_output_chars=1000)
        assert [item["id"] for item in following["records"]] == [short["id"]]
    body = evidence.read_content(
        "session",
        seq=long["seq"],
        offset_bytes=12_000,
        revision=long["evidence_revision"],
        max_output_chars=1000,
    )
    assert body["content"].startswith("中段证据")


def test_default_directory_limit_does_not_prematurely_cut_a_small_single_message(
    store: Store,
) -> None:
    record = store.add_message("session", "assistant", "普通小消息" * 40)
    page = HistoryEvidence(store).read("session", max_output_chars=1000)
    assert page["records"][0]["content"] == record["content"]
    assert not page["records"][0]["content_truncated"] and not page["has_more"]


def test_utf8_nul_escapes_pages_reassemble_and_survive_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "reopened.sqlite3"
    journal = Store(path)
    journal.put("session", {"id": "session"})
    original = ('A\0中文🙂\\"\n\u0001尾' * 80) + "EOF"
    record = journal.add_message("session", "tool", original)
    journal.close()
    journal = Store(path)
    try:
        evidence = HistoryEvidence(journal)
        offset = 0
        pieces: list[str] = []
        for _ in range(30):
            page = evidence.read_content(
                "session",
                message_id=record["id"],
                offset_bytes=offset,
                length_bytes=700,
                revision=record["evidence_revision"],
                max_output_chars=1000,
            )
            assert len(json.dumps(page, ensure_ascii=False)) <= 1000
            assert page["length_bytes"] == len(page["content"].encode("utf-8"))
            assert page["next_offset_bytes"] == offset + page["length_bytes"]
            pieces.append(page["content"])
            offset = page["next_offset_bytes"]
            if not page["has_more"]:
                break
            assert page["length_bytes"] > 0
        else:
            pytest.fail("分页未到达 EOF")
        assert "".join(pieces) == original
        assert offset == len(original.encode("utf-8"))
    finally:
        journal.close()


def test_short_window_retreats_only_an_incomplete_final_codepoint(store: Store) -> None:
    record = store.add_message("session", "tool", "A🙂中\0Z")
    evidence = HistoryEvidence(store)
    first = evidence.read_content(
        "session", message_id=record["id"], length_bytes=3, max_output_chars=1000
    )
    assert first["content"] == "A" and first["next_offset_bytes"] == 1
    second = evidence.read_content(
        "session",
        message_id=record["id"],
        offset_bytes=1,
        length_bytes=4,
        revision=first["revision"],
        max_output_chars=1000,
    )
    assert second["content"] == "🙂" and second["next_offset_bytes"] == 5
    tail = evidence.read_content(
        "session",
        message_id=record["id"],
        offset_bytes=5,
        revision=first["revision"],
        max_output_chars=1000,
    )
    assert tail["content"] == "中\0Z" and not tail["has_more"]


@pytest.mark.parametrize("offset", [2, 3, 4, 6, 7])
def test_middle_of_utf8_codepoint_is_rejected(store: Store, offset: int) -> None:
    record = store.add_message("session", "tool", "A🙂中")
    assert_code(
        "invalid_offset",
        lambda: HistoryEvidence(store).read_content(
            "session",
            message_id=record["id"],
            offset_bytes=offset,
            revision=record["evidence_revision"],
            max_output_chars=1000,
        ),
    )


def test_empty_exact_eof_and_impossible_nonempty_page(store: Store) -> None:
    evidence = HistoryEvidence(store)
    empty = store.add_message("session", "user", "")
    assert (
        evidence.read_content("session", message_id=empty["id"], max_output_chars=1000)[
            "length_bytes"
        ]
        == 0
    )
    record = store.add_message("session", "tool", "🙂")
    page = evidence.read_content(
        "session",
        message_id=record["id"],
        offset_bytes=4,
        revision=record["evidence_revision"],
        max_output_chars=1000,
    )
    assert page["content"] == "" and not page["has_more"] and page["next_offset_bytes"] == 4
    assert_code(
        "invalid_offset",
        lambda: evidence.read_content(
            "session",
            message_id=record["id"],
            offset_bytes=5,
            revision=record["evidence_revision"],
            max_output_chars=1000,
        ),
    )
    assert_code(
        "output_budget",
        lambda: evidence.read_content(
            "session", message_id=record["id"], length_bytes=3, max_output_chars=1000
        ),
    )
    assert_code(
        "output_budget",
        lambda: evidence.read_content("session", message_id=record["id"], max_output_chars=100),
    )


def test_sql_projection_does_not_decode_provider_data_images_or_tool_arguments(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = store.add_message(
        "session",
        "assistant",
        "可读正文",
        images=[{"data": "Y" * 100_000}],
        provider_data={"opaque": "X" * 100_000},
        tool_calls=[{"arguments": "secret-native-argument"}],
    )
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)

    def no_loads(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("证据投影不应在 Python 解码整条 JSON")

    with monkeypatch.context() as scoped:
        scoped.setattr(json, "loads", no_loads)
        evidence = HistoryEvidence(store)
        directory = evidence.read("session", max_output_chars=1000)
        page = evidence.read_content("session", message_id=record["id"], max_output_chars=1000)
    store.db.set_trace_callback(None)
    assert page["content"] == "可读正文" and directory["records"][0]["content"] == "可读正文"
    assert "secret-native-argument" not in json.dumps(page, ensure_ascii=False)
    assert all("SELECT data" not in query and "SELECT o.data" not in query for query in statements)
    projected = store.message_evidence_slice(
        "session",
        message_id=record["id"],
        seq=None,
        offset_bytes=0,
        length_bytes=5,
        metadata_bytes=1000,
    )
    assert len(projected["content"]) == 5
    assert (
        "images" not in projected and "provider_data" not in projected and "data" not in projected
    )


def test_revision_changes_on_same_length_edit_source_edit_and_forged_token(store: Store) -> None:
    record = store.add_message("session", "tool", "原文", evidence_revision="forged")
    assert record["evidence_revision"] != "forged"
    first_revision = record["evidence_revision"]
    store.put("message", {**record, "content": "改写", "evidence_revision": first_revision})
    evidence = HistoryEvidence(store)
    assert_code(
        "evidence_changed",
        lambda: evidence.read_content(
            "session", message_id=record["id"], revision=first_revision, max_output_chars=1000
        ),
    )
    current = store.get("message", record["id"])
    second_revision = current["evidence_revision"]
    store.put("message", {**current, "principal": "system:attention"})
    assert_code(
        "evidence_changed",
        lambda: evidence.read_content(
            "session", message_id=record["id"], revision=second_revision, max_output_chars=1000
        ),
    )


def test_legacy_revision_is_stable_and_reading_does_not_write(store: Store) -> None:
    record = store.add_message("session", "tool", "旧档案")
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_remove(data,'$.evidence_revision') WHERE kind='message' AND id=?",
            (record["id"],),
        )
    evidence = HistoryEvidence(store)
    before = store.db.total_changes
    page = evidence.read_content("session", message_id=record["id"], max_output_chars=1000)
    directory = evidence.read("session", max_output_chars=1000)
    assert page["revision"] == f"legacy:{store.instance_id}:{record['seq']}"
    assert directory["records"][0]["revision"] == page["revision"]
    assert store.db.total_changes == before
    store.put("message", store.get("message", record["id"]))
    assert_code(
        "evidence_changed",
        lambda: evidence.read_content(
            "session", message_id=record["id"], revision=page["revision"], max_output_chars=1000
        ),
    )


def test_deletion_reinsertion_and_wrong_session_do_not_reuse_old_cursor(store: Store) -> None:
    record = store.add_message("session", "tool", "旧正文")
    evidence = HistoryEvidence(store)
    assert_code(
        "evidence_not_found",
        lambda: evidence.read_content("other", seq=record["seq"], max_output_chars=1000),
    )
    assert_code(
        "evidence_not_found",
        lambda: evidence.read_content("other", message_id=record["id"], max_output_chars=1000),
    )
    store.delete("message", record["id"])
    assert_code(
        "evidence_not_found",
        lambda: evidence.read_content("session", message_id=record["id"], max_output_chars=1000),
    )
    store.put("message", {**record, "content": "新正文"})
    assert_code(
        "evidence_changed",
        lambda: evidence.read_content(
            "session",
            message_id=record["id"],
            revision=record["evidence_revision"],
            max_output_chars=1000,
        ),
    )
    store.delete("session", "session")
    assert_code(
        "evidence_not_found",
        lambda: evidence.read_content("session", message_id=record["id"], max_output_chars=1000),
    )


def test_same_message_cannot_migrate_and_legacy_index_json_mismatch_is_not_read(
    store: Store,
) -> None:
    record = store.add_message("session", "user", "不能跨会话")
    with pytest.raises(ValueError, match="不能迁移"):
        store.put("message", {**record, "session_id": "other"})
    assert store.get("message", record["id"]) == record
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.session_id','other') WHERE kind='message' AND id=?",
            (record["id"],),
        )
    evidence = HistoryEvidence(store)
    for session_id in ("session", "other"):
        assert evidence.read(session_id, max_output_chars=1000)["records"] == []

        def read_same_session(actual_session: str = session_id) -> dict[str, Any]:
            return evidence.read_content(
                actual_session, message_id=record["id"], max_output_chars=1000
            )

        assert_code(
            "evidence_not_found",
            read_same_session,
        )


def test_source_and_principal_preserved_or_explicitly_omitted_without_identity_guess(
    store: Store,
) -> None:
    system = store.add_message(
        "session", "user", "系统事实", source="system:attention", principal="system:attention"
    )
    evidence = HistoryEvidence(store)
    page = evidence.read_content("session", message_id=system["id"], max_output_chars=1000)
    assert (
        page["role"] == "user"
        and page["principal"] == "system:attention"
        and page["source"] == "system:attention"
    )
    huge = store.add_message(
        "session", "user", "仍可读取", source="来源" * 5000, principal="身份" * 5000
    )
    page = evidence.read_content("session", message_id=huge["id"], max_output_chars=1000)
    assert page["content"] == "仍可读取" and "source" not in page and "principal" not in page
    assert set(page["metadata_omitted"]) == {"source", "principal"}
    assert len(json.dumps(page, ensure_ascii=False)) <= 1000
    assert store.get("message", huge["id"])["principal"] == "身份" * 5000


@pytest.mark.parametrize("provenance", [None, "accepted", "runtime_excerpt", "invented"])
def test_provenance_is_host_metadata_and_never_inferred_from_tool_json(
    store: Store, provenance: str | None
) -> None:
    extra = {"content_provenance": provenance} if provenance is not None else {}
    record = store.add_message(
        "session", "tool", '{"truncated":true,"preview":"合法工具原文"}', **extra
    )
    page = HistoryEvidence(store).read_content(
        "session", message_id=record["id"], max_output_chars=1000
    )
    assert page["content_provenance"] == (
        provenance if provenance in {"accepted", "runtime_excerpt"} else "unknown"
    )
    assert not page["has_more"] and page["content"] == record["content"]


@pytest.mark.parametrize("content", [None, 42, ["不是正文"], {"text": "不能拼接"}])
def test_non_string_content_has_location_but_cannot_be_misrepresented_as_text(
    store: Store, content: Any
) -> None:
    record = store.add_message("session", "tool", content)
    evidence = HistoryEvidence(store)
    directory = evidence.read("session", max_output_chars=1000)
    assert directory["records"][0]["content_unavailable"]
    assert directory["records"][0]["content"] is None
    assert_code(
        "content_unavailable",
        lambda: evidence.read_content("session", message_id=record["id"], max_output_chars=1000),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"message_id": "x", "seq": 1},
        {"message_id": ""},
        {"seq": True},
        {"seq": 0},
        {"seq": 1, "offset_bytes": -1},
        {"seq": 1, "offset_bytes": True},
        {"seq": 1, "offset_bytes": 1 << 80},
        {"seq": 1, "length_bytes": 0},
        {"seq": 1, "length_bytes": True},
        {"seq": 1, "offset_bytes": 1},
        {"seq": 1, "revision": ""},
    ],
)
def test_invalid_range_arguments_are_small_structured_errors(
    store: Store, kwargs: dict[str, Any]
) -> None:
    assert_code(
        "invalid_arguments",
        lambda: HistoryEvidence(store).read_content("session", max_output_chars=1000, **kwargs),
    )


def test_directory_and_range_account_for_json_escaping_and_eof_metadata(store: Store) -> None:
    record = store.add_message("session", "tool", '\0\\"\n' * 100)
    evidence = HistoryEvidence(store)
    for budget in (400, 500, 701, 1000):
        directory = evidence.read("session", limit=1, max_output_chars=budget)
        page = evidence.read_content("session", message_id=record["id"], max_output_chars=budget)
        assert len(json.dumps(directory, ensure_ascii=False)) <= budget
        assert len(json.dumps(page, ensure_ascii=False)) <= budget
        assert page["length_bytes"] > 0
        assert record["content"].startswith(page["content"])
    empty = evidence.read("session", after=record["seq"], max_output_chars=1000)
    assert empty["records"] == [] and not empty["has_more"] and empty["next_after"] == record["seq"]


@pytest.mark.parametrize("budget", [None, 1000])
def test_full_directory_preview_uses_its_actual_truncated_false_size(
    store: Store, budget: int | None
) -> None:
    record = store.add_message("session", "assistant", "a" * 20)
    evidence = HistoryEvidence(store)
    page = evidence.read("session", limit=1, max_output_chars=2000)
    target = len(json.dumps(page, ensure_ascii=False)) - 1 if budget is None else budget
    if budget is not None:
        extra = target - len(json.dumps(page, ensure_ascii=False)) + 1
        store.put("message", {**record, "content": record["content"] + "a" * extra})
    result = evidence.read("session", limit=1, max_output_chars=target)
    assert len(json.dumps(result, ensure_ascii=False)) <= target
    assert result["records"][0]["content_truncated"]
    assert not result["has_more"]


def test_invalid_utf8_archive_is_not_replaced_or_silently_dropped(store: Store) -> None:
    record = store.add_message("session", "tool", "旧档案编码损坏")
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.content',json_extract(?,'$')) "
            "WHERE kind='message' AND id=?",
            ('"\\ud800"', record["id"]),
        )
    assert_code(
        "archive_encoding_error",
        lambda: HistoryEvidence(store).read_content(
            "session", message_id=record["id"], max_output_chars=1000
        ),
    )
