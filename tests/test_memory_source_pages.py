from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.evidence import EvidenceError
from muli.memory import Memory
from muli.memory_evidence import MemoryEvidence
from muli.store import Store


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    store = Store(tmp_path / "memory.db")
    try:
        yield Memory(store)
    finally:
        store.close()


def source(memory: Memory, content: str, **metadata: Any) -> dict[str, Any]:
    result, _ = memory.register_source({"id": "origin", "content": content, **metadata})
    assert result is not None
    return result


def test_long_nul_unicode_source_pages_survive_reopen_and_reassemble(tmp_path: Path) -> None:
    path = tmp_path / "reopen.db"
    store = Store(path)
    memory = Memory(store)
    original = ('A\0中文🙂\\"\n\u0001尾' * 130) + "EOF"
    saved = source(memory, original, principal="external:untrusted")
    item = memory.save("派生事实", source_id=saved["id"], quote="中文")
    store.close()
    store = Store(path)
    memory = Memory(store)
    try:
        reader = MemoryEvidence(memory)
        offset, pieces = 0, []
        for _ in range(200):
            page = reader.read(
                item["id"],
                offset_bytes=offset,
                length_bytes=180,
                revision=saved["digest"],
                max_output_chars=1000,
            )
            assert len(json.dumps(page, ensure_ascii=False)) <= 1000
            assert len(page["content"].encode("utf-8")) == page["length_bytes"]
            assert page["length_bytes"] > 0
            assert page["next_offset_bytes"] == offset + page["length_bytes"]
            assert page["truncated"] == (bool(offset) or page["has_more"])
            assert page["principal"] == "external:untrusted" and not page["trusted_owner"]
            offset = page["next_offset_bytes"]
            pieces.append(page["content"])
            if not page["has_more"]:
                break
        else:
            pytest.fail("source paging did not finish")
        assert "".join(pieces) == original
        eof = reader.read(
            item["id"], offset_bytes=offset, revision=saved["digest"], max_output_chars=1000
        )
        assert not eof["has_more"] and eof["content"] == "" and eof["truncated"]
    finally:
        store.close()


@pytest.mark.parametrize(
    "options,code",
    [
        ({"offset_bytes": True}, "invalid_arguments"),
        ({"length_bytes": 0}, "invalid_arguments"),
        ({"offset_bytes": 1}, "invalid_arguments"),
        ({"revision": ""}, "invalid_arguments"),
        ({"offset_bytes": 2, "revision": "current"}, "invalid_offset"),
        ({"offset_bytes": 8, "revision": "current"}, "invalid_offset"),
        ({"length_bytes": 3}, "output_budget"),
        ({"revision": "old"}, "evidence_changed"),
    ],
)
def test_invalid_utf8_windows_and_versions_fail_explicitly(
    memory: Memory,
    options: dict[str, Any],
    code: str,
) -> None:
    saved = source(memory, "🙂中")
    if options.get("revision") == "current":
        options = {**options, "revision": saved["digest"]}
    with pytest.raises(EvidenceError) as caught:
        MemoryEvidence(memory).read("origin", by_memory=False, max_output_chars=1000, **options)
    assert caught.value.code == code


def test_source_metadata_is_whole_or_explicit_budget_error(memory: Memory) -> None:
    source(memory, "中", principal="remote:" + "界" * 990)
    with pytest.raises(EvidenceError) as caught:
        MemoryEvidence(memory).read("origin", by_memory=False, max_output_chars=1000)
    assert caught.value.code == "output_budget"


def test_detail_and_slice_do_not_materialize_full_source(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = source(memory, "正文" * 20_000)
    item = memory.save("摘要", source_id=saved["id"], quote="正文")

    def forbid(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("source() would materialize the whole source")

    monkeypatch.setattr(memory, "source", forbid)
    statements: list[str] = []
    memory.store.db.set_trace_callback(statements.append)
    reader = MemoryEvidence(memory)
    assert "content" not in reader.detail(item["id"])["origin"]
    page = reader.read(item["id"], max_output_chars=1000)
    assert page["has_more"] and len(page["content"]) < 1000
    assert any("substr(CAST(s.content AS BLOB)" in statement for statement in statements)
    assert not any("SELECT * FROM memory_sources" in statement for statement in statements)


def test_exact_budget_boundary_and_small_source_compatibility(memory: Memory) -> None:
    saved = source(memory, "短🙂原文")
    reader = MemoryEvidence(memory)
    complete = reader.read("origin", by_memory=False, max_output_chars=1000)
    budget = len(json.dumps(complete, ensure_ascii=False))
    exact = reader.read("origin", by_memory=False, max_output_chars=budget)
    assert exact == complete and not exact["truncated"]
    assert all(exact[key] == value for key, value in saved.items())
    smaller = reader.read("origin", by_memory=False, max_output_chars=budget - 1)
    assert len(json.dumps(smaller, ensure_ascii=False)) <= budget - 1
    assert smaller["has_more"] and smaller["truncated"]


def test_forgotten_source_never_falls_back_to_conversation(memory: Memory) -> None:
    saved = source(memory, "仍在会话里的资料")
    memory.store.put("session", {"id": "session"})
    memory.store.add_message("session", "user", saved["content"], id=saved["id"])
    assert memory.forget_source(saved["id"])
    with pytest.raises(EvidenceError) as caught:
        MemoryEvidence(memory).read(saved["id"], by_memory=False, max_output_chars=1000)
    assert caught.value.code == "evidence_not_found"
