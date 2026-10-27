from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

import muli.memory_chunks as memory_chunks
from muli.evidence import EvidenceError
from muli.memory_chunks import (
    MemoryChunkSettings,
    MemoryChunkSettingsStore,
    iter_chunks,
    plan_chunks,
    restore_plan,
)
from muli.store import Store

DIGEST = "a" * 64
CONTENT = "甲🙂乙\n" * 3200 + "长来源尾部唯一条款。"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "policy.db")
    try:
        yield value
    finally:
        value.close()


def test_unicode_windows_cover_the_full_source_with_fixed_overlap_and_stable_keys() -> None:
    settings = MemoryChunkSettings()
    plan = plan_chunks(DIGEST, CONTENT, settings)
    chunks = list(iter_chunks(CONTENT, plan))
    assert plan.version == 1 and plan.source_digest == DIGEST
    assert plan.content_chars == len(CONTENT) and plan.batch_size == settings.batch_size
    assert len(chunks) == plan.count <= settings.max_chunks
    assert chunks[0].start_char == 0 and chunks[-1].end_char == len(CONTENT)
    assert "尾部唯一条款" in chunks[-1].content
    rebuilt = chunks[0].content
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.start_char == previous.end_char - settings.overlap_chars
        assert (
            current.content[: settings.overlap_chars] == previous.content[-settings.overlap_chars :]
        )
        rebuilt += current.content[settings.overlap_chars :]
    assert rebuilt == CONTENT
    for index, chunk in enumerate(chunks):
        assert chunk.index == index
        assert chunk.content == CONTENT[chunk.start_char : chunk.end_char]
        assert chunk.end_char - chunk.start_char <= settings.chunk_chars
        assert chunk.content.encode("utf-8").decode("utf-8") == chunk.content
        assert DIGEST in chunk.dedup_key
    assert len({chunk.dedup_key for chunk in chunks}) == len(chunks)
    assert list(iter_chunks(CONTENT, plan)) == chunks


def test_threshold_zero_overlap_and_whitespace_windows_preserve_coverage() -> None:
    settings = MemoryChunkSettings(chunk_chars=12_000, overlap_chars=0, max_chunks=2)
    short = plan_chunks(DIGEST, "文" * 12_000, settings)
    assert short.count == 0 and list(iter_chunks("文" * 12_000, short)) == []
    empty = plan_chunks(DIGEST, "", settings)
    assert empty.count == 0 and list(iter_chunks("", empty)) == []
    content = " " * 12_000 + "尾"
    plan = plan_chunks(DIGEST, content, settings)
    chunks = list(iter_chunks(content, plan))
    assert plan.count == 2
    assert chunks[0].content.isspace() and chunks[0].index == 0
    assert chunks[1].start_char == chunks[0].end_char == 12_000
    assert chunks[1].content == "尾" and chunks[1].index == 1
    assert "".join(chunk.content for chunk in chunks) == content


def test_plan_count_is_bounded_before_hashing_or_materializing_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("over-budget plan must be rejected before hashing the full text")

    monkeypatch.setattr(memory_chunks.hashlib, "sha256", forbidden)
    with pytest.raises(ValueError, match="max_chunks"):
        plan_chunks(DIGEST, CONTENT, MemoryChunkSettings(max_chunks=1))


def test_checkpoint_roundtrip_freezes_settings_and_supports_exact_resume() -> None:
    policy = MemoryChunkSettings(chunk_chars=1024, overlap_chars=32, batch_size=3)
    original = plan_chunks(DIGEST, CONTENT, policy)
    checkpoint = json.loads(json.dumps(original.to_dict()))
    changed = MemoryChunkSettings(chunk_chars=512, overlap_chars=128, batch_size=1)
    assert plan_chunks(DIGEST, CONTENT, changed) != original
    restored = restore_plan(checkpoint, DIGEST, CONTENT)
    assert restored == original and restored.batch_size == 3
    windows = list(iter_chunks(CONTENT, restored))
    assert list(iter_chunks(CONTENT, restored, start_index=3)) == windows[3:]
    assert list(iter_chunks(CONTENT, restored, start_index=restored.count)) == []
    for index in (-1, True, restored.count + 1):
        with pytest.raises(ValueError):
            list(iter_chunks(CONTENT, restored, start_index=index))


@pytest.mark.parametrize(
    "damage",
    [
        {"version": 2},
        {"version": True},
        {"source_digest": "A" * 64},
        {"content_digest": "invalid"},
        {"content_chars": len(CONTENT) + 1},
        {"chunk_chars": 255},
        {"overlap_chars": 2000},
        {"count": 0},
        {"batch_size": True},
        {"extra": "unknown"},
    ],
)
def test_invalid_or_future_checkpoint_is_rejected(damage: dict[str, Any]) -> None:
    checkpoint = plan_chunks(DIGEST, CONTENT, MemoryChunkSettings()).to_dict()
    with pytest.raises(ValueError):
        restore_plan({**checkpoint, **damage}, DIGEST, CONTENT)


def test_changed_source_text_or_missing_checkpoint_cannot_resume_old_windows() -> None:
    plan = plan_chunks(DIGEST, CONTENT, MemoryChunkSettings())
    with pytest.raises(ValueError, match="来源版本"):
        restore_plan(plan.to_dict(), "b" * 64, CONTENT)
    for content in (CONTENT + "新增", "改" + CONTENT[1:]):
        with pytest.raises(ValueError, match="正文长度或摘要"):
            restore_plan(plan.to_dict(), DIGEST, content)
        with pytest.raises(ValueError, match="正文长度或摘要"):
            list(iter_chunks(content, plan, start_index=plan.count))
    with pytest.raises(ValueError, match="缺失字段"):
        restore_plan({}, DIGEST, CONTENT)
    with pytest.raises(ValueError):
        plan_chunks("invalid", CONTENT, MemoryChunkSettings())


@pytest.mark.parametrize(
    "values",
    [
        {"chunk_chars": True},
        {"chunk_chars": 255},
        {"chunk_chars": 12_001},
        {"overlap_chars": -1},
        {"overlap_chars": 2049},
        {"chunk_chars": 256, "overlap_chars": 256},
        {"max_chunks": 0},
        {"max_chunks": 257},
        {"batch_size": 0},
        {"batch_size": 17},
        {"unknown": 1},
    ],
)
def test_chunk_policy_rejects_invalid_types_ranges_and_overlap(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MemoryChunkSettings.model_validate(values)


def test_missing_policy_is_read_only_and_replace_persists_through_reopen(tmp_path: Path) -> None:
    path = tmp_path / "policy.db"
    store = Store(path)
    policy = MemoryChunkSettingsStore(store)
    settings = MemoryChunkSettings(chunk_chars=1024, overlap_chars=64, batch_size=2)
    try:
        before = store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        assert policy.read() == MemoryChunkSettings()
        assert store.get("memory_chunk_settings", "current") is None
        assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == before
        assert policy.replace(settings) == settings
        assert store.get("memory_chunk_settings", "current") == {
            "id": "current",
            "schema_version": 1,
            "settings": settings.model_dump(mode="json"),
        }
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert MemoryChunkSettingsStore(reopened).read() == settings
        events = reopened.db.execute(
            "SELECT count(*) FROM events WHERE type='memory.chunk.settings.changed'"
        ).fetchone()[0]
        assert events == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("damage", ["future", "extra", "oversized"])
def test_invalid_policy_record_is_not_replaced(store: Store, damage: str) -> None:
    item: dict[str, Any] = {"id": "current", "schema_version": 1, "settings": {}}
    if damage == "future":
        item["schema_version"] = 2
    elif damage == "extra":
        item["unexpected"] = True
    else:
        item["settings"] = {"unknown": "x" * 20_000}
    store.put("memory_chunk_settings", item)
    policy = MemoryChunkSettingsStore(store)
    for operation in (policy.read, lambda: policy.replace(MemoryChunkSettings())):
        with pytest.raises(EvidenceError) as caught:
            operation()
        assert caught.value.code == "memory_chunk_settings_unavailable"
    assert store.get("memory_chunk_settings", "current") == item


def test_policy_event_failure_rolls_back_object_and_nested_transaction_is_rejected(
    store: Store,
) -> None:
    policy = MemoryChunkSettingsStore(store)
    with store.db:
        store.db.execute(
            "CREATE TRIGGER reject_chunk_policy_event BEFORE INSERT ON events "
            "WHEN NEW.type='memory.chunk.settings.changed' "
            "BEGIN SELECT RAISE(ABORT,'controlled policy event failure'); END"
        )
    with pytest.raises(EvidenceError) as caught:
        policy.replace(MemoryChunkSettings(batch_size=2))
    assert caught.value.code == "memory_chunk_settings_write_failed"
    assert store.get("memory_chunk_settings", "current") is None
    assert policy.read() == MemoryChunkSettings()
    store.db.execute("BEGIN")
    try:
        with pytest.raises(EvidenceError) as caught:
            policy.replace(MemoryChunkSettings())
        assert caught.value.code == "memory_chunk_settings_transaction"
    finally:
        store.db.rollback()
    assert store.db.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
