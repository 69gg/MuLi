from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.history_evidence import EvidenceError, HistoryEvidence
from muli.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "archive.sqlite3")
    journal.put("session", {"id": "session"})
    journal.put("session", {"id": "other"})
    try:
        yield journal
    finally:
        journal.close()


def test_archive_pages_latest_first_but_returns_each_page_in_sequence_order(store: Store) -> None:
    first = store.add_message("session", "user", "系统资料", principal="system:attention")
    store.add_message("other", "user", "另一会话")
    second = store.add_message("session", "tool", "工具记录", content_provenance="runtime_excerpt")
    third = store.add_message("session", "assistant", "回复")
    store.add_message("other", "assistant", "最高全局序号不属于目标会话")
    evidence = HistoryEvidence(store)
    page = evidence.archive("session", limit=2, max_output_chars=2000)
    assert set(page) == {"session_id", "messages", "through_seq", "next_before_seq", "has_older"}
    assert page["session_id"] == "session" and page["through_seq"] == third["seq"]
    assert [item["id"] for item in page["messages"]] == [second["id"], third["id"]]
    assert page["messages"][0]["content_provenance"] == "runtime_excerpt"
    assert page["has_older"] and page["next_before_seq"] == second["seq"]
    tail = evidence.archive(
        "session",
        before_seq=page["next_before_seq"],
        through_seq=page["through_seq"],
        limit=2,
        max_output_chars=2000,
    )
    assert [item["id"] for item in tail["messages"]] == [first["id"]]
    assert tail["messages"][0]["principal"] == "system:attention"
    assert tail["messages"][0]["role"] == "user"
    assert tail["messages"][0]["content_provenance"] == "unknown"
    assert not tail["has_older"] and tail["next_before_seq"] is None
    assert tail["through_seq"] == page["through_seq"]


def test_archive_reaches_first_persisted_message_beyond_the_existing_tail(store: Store) -> None:
    with store.db:
        for index in range(1001):
            store._put(
                "message",
                {
                    "id": f"message-{index}",
                    "session_id": "session",
                    "role": "user",
                    "content": str(index),
                },
            )
    assert store.messages("session")[0]["id"] == "message-1"
    evidence = HistoryEvidence(store)
    before: int | None = None
    through: int | None = None
    seen: list[str] = []
    for _ in range(21):
        page = evidence.archive(
            "session", before_seq=before, through_seq=through, max_output_chars=32000
        )
        assert len(page["messages"]) <= 50
        seen.extend(item["id"] for item in reversed(page["messages"]))
        through = page["through_seq"]
        before = page["next_before_seq"]
        if not page["has_older"]:
            break
    else:
        pytest.fail("归档没有在有限页数内到达第一条消息")
    assert seen == [f"message-{index}" for index in reversed(range(1001))]
    assert before is None and through == 1001


def test_archive_budget_cuts_only_a_contiguous_newest_prefix(store: Store) -> None:
    records = [
        store.add_message("session", "tool", ('前\0中文🙂\\"\n' * 100) + str(index))
        for index in range(9)
    ]
    evidence = HistoryEvidence(store)
    before: int | None = None
    through: int | None = None
    seen: list[str] = []
    for _ in range(len(records)):
        page = evidence.archive(
            "session", before_seq=before, through_seq=through, limit=7, max_output_chars=1000
        )
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        assert page["messages"]
        seen.extend(item["id"] for item in reversed(page["messages"]))
        through = page["through_seq"]
        if not page["has_older"]:
            assert page["next_before_seq"] is None
            break
        assert page["next_before_seq"] == min(item["seq"] for item in page["messages"])
        assert before is None or page["next_before_seq"] < before
        before = page["next_before_seq"]
    else:
        pytest.fail("预算削减后的归档页没有前进到 EOF")
    assert seen == [item["id"] for item in reversed(records)]


def test_archive_empty_window_and_missing_session_have_distinct_results(store: Store) -> None:
    evidence = HistoryEvidence(store)
    empty = evidence.archive("session", max_output_chars=1000)
    assert empty == {
        "session_id": "session",
        "messages": [],
        "through_seq": 0,
        "next_before_seq": None,
        "has_older": False,
    }
    record = store.add_message("session", "user", "存在的正文")
    assert evidence.archive("session", through_seq=0, max_output_chars=1000) == empty
    earlier = evidence.archive("session", before_seq=record["seq"], max_output_chars=1000)
    assert earlier["messages"] == [] and earlier["next_before_seq"] is None
    assert earlier["through_seq"] == record["seq"]
    store.delete("session", "session")
    with pytest.raises(EvidenceError) as caught:
        evidence.archive("session", max_output_chars=1000)
    assert caught.value.code == "evidence_not_found"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"before_seq": True},
        {"before_seq": 0},
        {"before_seq": -1},
        {"before_seq": 1 << 80},
        {"before_seq": "1"},
        {"before_seq": 1.0},
        {"through_seq": True},
        {"through_seq": -1},
        {"through_seq": 1 << 80},
        {"limit": True},
        {"limit": 0},
        {"limit": 1 << 80},
        {"max_output_chars": True},
        {"max_output_chars": 0},
        {"max_output_chars": 1 << 80},
    ],
)
def test_archive_rejects_invalid_and_sqlite_overflowing_parameters(
    store: Store, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(EvidenceError) as caught:
        HistoryEvidence(store).archive("session", **{"max_output_chars": 1000, **kwargs})
    assert caught.value.code == "invalid_arguments"


async def test_archive_boundary_survives_appends_deletion_and_revision_between_pages(
    store: Store,
) -> None:
    records = [store.add_message("session", "user", f"旧正文{index}") for index in range(8)]
    captured, release = asyncio.Event(), asyncio.Event()
    evidence = HistoryEvidence(store)

    async def first_page() -> dict[str, Any]:
        result = evidence.archive("session", limit=3, max_output_chars=2000)
        captured.set()
        await release.wait()
        return result

    task = asyncio.create_task(first_page())
    try:
        await asyncio.wait_for(captured.wait(), 2)
        new = store.add_message("session", "assistant", "首次读取后的新消息")
        store.add_message("other", "user", "另一会话的新消息")
        store.delete("message", records[2]["id"])
        changed = store.put("message", {**records[1], "content": "更正后的当前事实"})
        release.set()
        page = await asyncio.wait_for(task, 2)
        through = page["through_seq"]
        seen: list[dict[str, Any]] = []
        for _ in range(4):
            assert page["through_seq"] == records[-1]["seq"]
            seen.extend(reversed(page["messages"]))
            if not page["has_older"]:
                break
            page = evidence.archive(
                "session",
                before_seq=page["next_before_seq"],
                through_seq=through,
                limit=3,
                max_output_chars=2000,
            )
        else:
            pytest.fail("归档分页未到达当前剩余记录末尾")
        assert [item["id"] for item in seen] == [
            item["id"] for item in reversed(records) if item["id"] != records[2]["id"]
        ]
        edited = next(item for item in seen if item["id"] == changed["id"])
        assert (
            edited["content"] == changed["content"]
            and edited["revision"] == changed["evidence_revision"]
        )
        assert new["id"] not in {item["id"] for item in seen}
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("limit", [1, 2])
def test_archive_fits_final_envelope_at_eof_and_truncation_boolean_boundaries(
    store: Store, limit: int
) -> None:
    store.add_message("session", "assistant", '中文\0\\"\n' * 20)
    store.add_message("session", "assistant", "x" * 20)
    evidence = HistoryEvidence(store)
    full = evidence.archive("session", limit=limit, max_output_chars=2000)
    size = len(json.dumps(full, ensure_ascii=False))
    for budget in range(size - 20, size + 2):
        page = evidence.archive("session", limit=limit, max_output_chars=budget)
        assert len(json.dumps(page, ensure_ascii=False)) <= budget
        assert page["messages"]
        assert page["next_before_seq"] == (
            min(item["seq"] for item in page["messages"]) if page["has_older"] else None
        )
        for item in page["messages"]:
            assert item["content_truncated"] == (
                len(item["content"].encode("utf-8")) < item["content_bytes"]
            )


def test_archive_budget_includes_session_name_and_never_uses_an_empty_nonadvancing_page(
    store: Store,
) -> None:
    identifier = '会话\0\\"' * 12
    store.put("session", {"id": identifier})
    store.add_message(identifier, "user", "全文" * 100)
    evidence = HistoryEvidence(store)
    for budget in (1, 120, 300):
        with pytest.raises(EvidenceError) as caught:
            evidence.archive(identifier, max_output_chars=budget)
        assert caught.value.code == "output_budget"
    page = evidence.archive(identifier, max_output_chars=1000)
    assert page["session_id"] == identifier
    assert len(json.dumps(page, ensure_ascii=False)) <= 1000
    assert page["messages"] and not page["has_older"] and page["next_before_seq"] is None


def test_archive_projects_bounded_columns_without_loading_native_payloads_in_python(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = store.add_message(
        "session",
        "assistant",
        "正文🙂\0尾",
        provider_data={"opaque": "native" * 100_000},
        images=[{"data": "image" * 100_000}],
        tool_calls=[{"arguments": "do-not-expose" * 1000}],
    )
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)

    def no_full_json(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("归档不应在 Python 解码整条持久消息")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(json, "loads", no_full_json)
            page = HistoryEvidence(store).archive("session", max_output_chars=1000)
    finally:
        store.db.set_trace_callback(None)
    assert page["messages"][0]["content"] == item["content"]
    assert len(json.dumps(page, ensure_ascii=False)) <= 1000
    assert all("SELECT o.data" not in query and "SELECT data" not in query for query in statements)
    assert "do-not-expose" not in json.dumps(page)
    selects = [query for query in statements if query.startswith("SELECT o.id,m.seq")]
    assert len(selects) == 1
    plan = store.db.execute("EXPLAIN QUERY PLAN " + selects[0]).fetchall()
    assert any("message_order_session" in row["detail"] for row in plan)
    assert not any("TEMP B-TREE" in row["detail"] for row in plan)


def test_archive_resolves_true_session_boundary_and_skips_corrupt_index_mapping(
    store: Store,
) -> None:
    first = store.add_message("session", "user", "真实会话内容")
    wrong = store.add_message("session", "user", "索引与JSON归属不符")
    with store.db:
        store.db.execute(
            "UPDATE objects SET data=json_set(data,'$.session_id','other') "
            "WHERE kind='message' AND id=?",
            (wrong["id"],),
        )
    evidence = HistoryEvidence(store)
    page = evidence.archive("session", max_output_chars=1000)
    assert page["through_seq"] == first["seq"]
    assert [item["id"] for item in page["messages"]] == [first["id"]]
    assert evidence.archive("other", max_output_chars=1000)["messages"] == []
    assert not page["has_older"]


def test_archive_preserves_or_explicitly_omits_identity_and_never_infers_owner(
    store: Store,
) -> None:
    first = store.add_message(
        "session", "user", "外部资料", source="channel:untrusted", principal="external:someone"
    )
    huge = store.add_message(
        "session", "user", "巨大身份元数据", source="来源" * 1000, principal="身份" * 1000
    )
    evidence = HistoryEvidence(store)
    page = evidence.archive("session", max_output_chars=2000)
    rows = {item["id"]: item for item in page["messages"]}
    assert rows[first["id"]]["source"] == "channel:untrusted"
    assert rows[first["id"]]["principal"] == "external:someone"
    assert "source" not in rows[huge["id"]] and "principal" not in rows[huge["id"]]
    assert set(rows[huge["id"]]["metadata_omitted"]) == {"source", "principal"}
    assert all(item.get("principal") != "owner" for item in rows.values())


def test_archive_revision_links_to_utf8_body_and_rejects_subsequent_edits(store: Store) -> None:
    content = ('A\0中文🙂\\"\n' * 300) + "结束"
    record = store.add_message("session", "tool", content)
    evidence = HistoryEvidence(store)
    archive = evidence.archive("session", max_output_chars=1000)
    item = archive["messages"][0]
    assert item["content_truncated"] and item["revision"] == record["evidence_revision"]
    pieces: list[str] = []
    offset = 0
    for _ in range(100):
        page = evidence.read_content(
            "session",
            message_id=item["id"],
            offset_bytes=offset,
            revision=item["revision"],
            max_output_chars=1000,
        )
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        pieces.append(page["content"])
        offset = page["next_offset_bytes"]
        if not page["has_more"]:
            break
    else:
        pytest.fail("长正文续读没有到达EOF")
    assert "".join(pieces) == content and offset == len(content.encode("utf-8"))
    store.put("message", {**record, "content": "当前新正文"})
    with pytest.raises(EvidenceError) as caught:
        evidence.read_content(
            "session", message_id=item["id"], revision=item["revision"], max_output_chars=1000
        )
    assert caught.value.code == "evidence_changed"


def test_archive_is_read_only_and_legacy_cursor_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    store = Store(path)
    try:
        store.put("session", {"id": "session"})
        first = store.add_message("session", "tool", "旧消息")
        second = store.add_message("session", "assistant", "较新消息")
        with store.db:
            store.db.execute(
                "UPDATE objects SET data=json_remove(data,'$.evidence_revision') "
                "WHERE kind='message' AND id=?",
                (first["id"],),
            )
        changes = store.db.total_changes
        evidence = HistoryEvidence(store)
        page = evidence.archive("session", limit=1, max_output_chars=1000)
        legacy = evidence.archive(
            "session",
            before_seq=page["next_before_seq"],
            through_seq=page["through_seq"],
            max_output_chars=1000,
        )["messages"][0]
        assert page["messages"][0]["id"] == second["id"]
        assert legacy["revision"].startswith("legacy:")
        assert store.db.total_changes == changes
    finally:
        store.close()
    store = Store(path)
    try:
        changes = store.db.total_changes
        restored = HistoryEvidence(store).archive(
            "session",
            before_seq=page["next_before_seq"],
            through_seq=page["through_seq"],
            max_output_chars=1000,
        )
        assert restored["messages"][0] == legacy
        assert store.db.total_changes == changes
    finally:
        store.close()


@pytest.mark.parametrize("value", [None, 42, ["非字符串"]])
def test_archive_keeps_location_for_unavailable_content(store: Store, value: Any) -> None:
    record = store.add_message("session", "tool", value)
    page = HistoryEvidence(store).archive("session", max_output_chars=1000)
    item = page["messages"][0]
    assert item["id"] == record["id"] and item["content_unavailable"]
    assert item["content"] is None and item["content_bytes"] is None


def test_archive_keeps_forward_read_contract_and_complete_signed_integer_sequences(
    store: Store,
) -> None:
    first = store.add_message("session", "user", "第一条")
    second = store.add_message("session", "assistant", "最后一条")
    largest = (1 << 63) - 1
    with store.db:
        store.db.execute(
            "UPDATE message_order SET seq=? WHERE message_id=?", (largest, second["id"])
        )
    evidence = HistoryEvidence(store)
    forward = evidence.read("session", limit=1, max_output_chars=1000)
    page = evidence.archive("session", limit=1, max_output_chars=1000)
    assert page["through_seq"] == largest and page["next_before_seq"] == largest
    assert page["messages"][0]["seq"] == largest
    tail = evidence.archive(
        "session", before_seq=largest, through_seq=largest, max_output_chars=1000
    )
    assert tail["messages"][0]["id"] == first["id"] and not tail["has_older"]
    assert evidence.read("session", limit=1, max_output_chars=1000) == forward


def test_archive_derived_byte_and_row_limits_cannot_overflow_sqlite(store: Store) -> None:
    record = store.add_message("session", "assistant", "小正文")
    page = HistoryEvidence(store).archive(
        "session", limit=(1 << 63) - 2, max_output_chars=(1 << 63) - 2
    )
    assert page["messages"][0]["content"] == record["content"]
    assert not page["has_older"] and page["next_before_seq"] is None
