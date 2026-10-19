"""Keep BLOB substring lengths portable across SQLite integer implementations."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from muli.history_evidence import HistoryEvidence
from muli.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "evidence-substr.sqlite3")
    journal.put("session", {"id": "session"})
    try:
        yield journal
    finally:
        journal.close()


@pytest.mark.parametrize("reader", ["archive", "directory", "slice"])
@pytest.mark.parametrize("requested", [3, sys.maxsize])
def test_substr_receives_no_length_larger_than_its_actual_blob(
    store: Store, reader: str, requested: int
) -> None:
    content = "A\0中文🙂尾"
    record = store.add_message(
        "session", "tool", content, source="web\0来源", principal="system:attention"
    )
    observations: list[tuple[int, int]] = []

    def observed_substr(value: bytes | None, start: int | None, length: int | None) -> bytes | None:
        if value is None or start is None or length is None:
            return None
        assert start >= 1
        observations.append((len(value), length))
        return value[start - 1 : start - 1 + length]

    # Observe the actual SQL arguments instead of relying on the newer local
    # SQLite version to reproduce older sqlite3_value_int() narrowing behavior.
    store.db.create_function("substr", 3, observed_substr)
    if reader == "archive":
        rows = list(
            store.message_evidence_archive(
                "session",
                before_seq=None,
                through_seq=record["seq"],
                limit=2,
                preview_bytes=requested,
                metadata_bytes=requested,
            )
        )
    elif reader == "directory":
        rows = list(
            store.message_evidence_directory(
                "session", after=0, limit=2, preview_bytes=requested, metadata_bytes=requested
            )
        )
    else:
        row = store.message_evidence_slice(
            "session",
            message_id=record["id"],
            seq=None,
            offset_bytes=2,
            length_bytes=requested,
            metadata_bytes=requested,
        )
        assert row is not None
        rows = [row]
    assert len(rows) == 1 and len(observations) == 5
    assert all(
        0 <= requested_length <= actual_bytes for actual_bytes, requested_length in observations
    )
    offset = 2 if reader == "slice" else 0
    assert rows[0]["content"] == content.encode("utf-8")[offset : offset + requested]
    assert rows[0]["content_bytes"] == len(content.encode("utf-8"))
    assert rows[0]["evidence_revision"] == record["evidence_revision"].encode("utf-8")[:requested]


def test_large_archive_budget_preserves_complete_content_and_revision(store: Store) -> None:
    original = store.add_message("session", "assistant", "完整\0🙂原文")
    result = HistoryEvidence(store).archive(
        "session", limit=sys.maxsize - 1, max_output_chars=sys.maxsize - 1
    )
    record = result["messages"][0]
    assert record["content"] == original["content"] and not record["content_truncated"]
    assert record["revision"] == original["evidence_revision"]
    assert result["next_before_seq"] is None and not result["has_older"]
