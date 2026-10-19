from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.evidence import EvidenceError, decode_utf8
from muli.memory import Memory
from muli.store import Store


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    store = Store(tmp_path / "source-preview.sqlite3")
    try:
        yield Memory(store)
    finally:
        store.close()


def register(memory: Memory, content: str) -> dict[str, Any]:
    source, created = memory.register_source(
        {
            "id": "original",
            "content": content,
            "source": "web\x00渠道",
            "session_id": "main😀",
            "role": "user",
            "principal": "实际主人\x00标识",
            "created_at": 1234.5,
        },
        trusted_owner=True,
    )
    assert source is not None and created
    return source


@pytest.mark.parametrize(
    ("content", "max_chars"),
    [
        ("abcdef", 1),
        ("abcdef", 6),
        ("abcdef", 20),
        ("\x00尾部中文", 1),
        ("首\x00中\x00尾", 3),
        ("中文资料末尾", 2),
        ("中文资料末尾", 6),
        ("😀🦊🐈", 2),
        ("😀🦊🐈", 3),
        ("a😀末尾", 1),
        ("a😀末尾", 2),
        ("a\x00😀尾巴", 3),
        ("e\u0301字👩\u200d💻", 4),
    ],
)
def test_source_preview_matches_python_character_slice_without_changing_identity(
    memory: Memory, content: str, max_chars: int
) -> None:
    original = register(memory, content)
    preview = memory.source("original", max_chars=max_chars)
    assert preview == {
        **original,
        "content": content[:max_chars],
        "truncated": len(content) > max_chars,
    }
    assert memory.source("original") == original
    assert "truncated" not in memory.source("original")


def test_preview_moves_only_bounded_blob_prefix_into_python(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "中文😀\x00资料" * 4000
    original = register(memory, content)
    decoded_sizes: list[int] = []

    def bounded_decode(raw: bytes, *, allow_incomplete_tail: bool) -> str:
        assert len(raw) <= 300 * 4
        decoded_sizes.append(len(raw))
        return decode_utf8(raw, allow_incomplete_tail=allow_incomplete_tail)

    def bounded_text(raw: bytes) -> str:
        # SQLite must not return the full source as a Python TEXT column.
        assert len(raw) <= 1200
        return raw.decode("utf-8", errors="strict")

    monkeypatch.setattr("muli.memory.decode_utf8", bounded_decode)
    memory.store.db.text_factory = bounded_text
    try:
        preview = memory.source("original", max_chars=300)
    finally:
        memory.store.db.text_factory = str
    assert decoded_sizes == [1200]
    assert preview == {**original, "content": content[:300], "truncated": True}


def test_short_blob_can_require_character_truncation_even_when_all_bytes_fit(
    memory: Memory,
) -> None:
    original = register(memory, "a\x00b")
    assert memory.source("original", max_chars=1) == {
        **original,
        "content": "a",
        "truncated": True,
    }


def test_arbitrarily_large_positive_integer_keeps_full_preview_behavior(memory: Memory) -> None:
    original = register(memory, "完整\x00😀原文")
    assert memory.source("original", max_chars=1 << 100) == {**original, "truncated": False}


@pytest.mark.parametrize("max_chars", [1, 4, 1 << 29, 1 << 62, 1 << 100])
def test_sql_substr_length_never_exceeds_actual_blob(memory: Memory, max_chars: int) -> None:
    content = "完整\x00😀原文"
    original = register(memory, content)
    lengths: list[int] = []

    def observed_substr(raw: bytes, start: int, length: int) -> bytes:
        # Observe the argument after SQL evaluation, before an older SQLite
        # implementation could narrow it to a signed 32-bit integer.
        lengths.append(length)
        return raw[start - 1 : start - 1 + length]

    memory.store.db.create_function("substr", 3, observed_substr)
    preview = memory.source("original", max_chars=max_chars)
    assert len(lengths) == 1
    assert 0 <= lengths[0] <= len(content.encode("utf-8"))
    assert preview == {
        **original,
        "content": content[:max_chars],
        "truncated": len(content) > max_chars,
    }


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "3", [], {}])
def test_invalid_source_preview_limit_is_rejected(memory: Memory, limit: Any) -> None:
    register(memory, "原文")
    with pytest.raises(ValueError, match="正整数"):
        memory.source("original", max_chars=limit)


def test_missing_source_returns_none_in_both_read_modes(memory: Memory) -> None:
    assert memory.source("missing") is None
    assert memory.source("missing", max_chars=300) is None


def test_search_sources_uses_bounded_previews_and_preserves_identity(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "检索资料😀\x00" * 1000
    original = register(memory, content)
    sizes: list[int] = []

    def bounded_decode(raw: bytes, *, allow_incomplete_tail: bool) -> str:
        assert len(raw) <= 1000 * 4
        sizes.append(len(raw))
        return decode_utf8(raw, allow_incomplete_tail=allow_incomplete_tail)

    monkeypatch.setattr("muli.memory.decode_utf8", bounded_decode)
    found = memory.search_sources("检索")
    assert found == [{**original, "content": content[:1000], "truncated": True}]
    assert sizes == [4000]


@pytest.mark.parametrize("raw", [b"a\xffb", b"a\xf0", b"\x80invalid"])
def test_malformed_persisted_utf8_is_not_silently_replaced_or_dropped(
    memory: Memory, raw: bytes
) -> None:
    register(memory, "先登记有效原文")
    # Fault injection bypasses register_source to model an invalid legacy archive.
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memory_sources SET content=CAST(? AS TEXT) WHERE id='original'", (raw,)
        )
    with pytest.raises(EvidenceError):
        memory.source("original", max_chars=100)
