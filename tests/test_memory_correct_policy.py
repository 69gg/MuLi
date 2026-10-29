"""Focused version, provenance and owner-admission correction boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.evidence import EvidenceError
from muli.memory import Memory
from muli.store import SQLITE_MAX_INTEGER, Store


def test_edit_rejects_an_independent_connection_update_before_its_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory.db"
    store = Store(path)
    peer_store = Store(path)
    memory, peer = Memory(store), Memory(peer_store)
    initial = memory.save("原来的待核查结论。", kind="fact")
    winners: list[dict[str, Any]] = []
    original_save = memory._save

    def interleave_legal_edit(**arguments: Any) -> str:
        assert arguments["identifier"] == initial["id"] and not winners
        # The second connection uses the real legacy editor and commits before
        # the first writer. No thread, forged version or SQL mutation is used.
        winner = peer.edit(initial["id"], content="另一连接已经确认并保存的新结论。")
        assert winner["version"] == initial["version"] + 1
        assert len(peer.revisions(initial["id"])) == 1
        winners.append(winner)
        return original_save(**arguments)

    monkeypatch.setattr(memory, "_save", interleave_legal_edit)
    try:
        with pytest.raises(EvidenceError, match="版本已变化") as caught:
            memory.edit(
                initial["id"],
                expected_version=initial["version"],
                content="旧版本请求不得覆盖另一连接的新结论。",
            )
        assert caught.value.code == "evidence_changed"
        assert len(winners) == 1
        assert memory.get(initial["id"]) == peer.get(initial["id"]) == winners[0]
        history = memory.revisions(initial["id"])
        assert len(history) == 1 and history[0]["snapshot"] == initial
    finally:
        peer_store.close()
        store.close()


def test_edit_rejects_an_independent_connection_update_after_its_writer_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory.db"
    store = Store(path)
    peer_store = Store(path)
    memory, peer = Memory(store), Memory(peer_store)
    initial = memory.save("末次读取后仍必须核版本的原结论。", kind="fact")
    winners: list[dict[str, Any]] = []
    original_validate = memory._validate

    def interleave_after_read(*arguments: Any, **keywords: Any) -> tuple[str, str, bool]:
        # _save has already read the existing record and chosen its preserved
        # metadata. The real validation still runs before its first write.
        assert not winners and memory.get(initial["id"])["version"] == initial["version"]
        winner = peer.edit(initial["id"], content="末次读取之后另一连接合法提交的结论。")
        assert winner["version"] == initial["version"] + 1
        assert len(peer.revisions(initial["id"])) == 1
        winners.append(winner)
        return original_validate(*arguments, **keywords)

    monkeypatch.setattr(memory, "_validate", interleave_after_read)
    try:
        with pytest.raises(EvidenceError, match="版本已变化") as caught:
            memory.edit(
                initial["id"],
                expected_version=initial["version"],
                content="末次读取后也不能用旧版本覆盖。",
            )
        assert caught.value.code == "evidence_changed"
        assert len(winners) == 1
        assert memory.get(initial["id"]) == peer.get(initial["id"]) == winners[0]
        history = memory.revisions(initial["id"])
        assert len(history) == 1 and history[0]["snapshot"] == initial
    finally:
        peer_store.close()
        store.close()


@pytest.fixture
def manual_memory(tmp_path: Path) -> Iterator[Memory]:
    store = Store(tmp_path / "manual.db")
    try:
        yield Memory(store)
    finally:
        store.close()


@pytest.mark.parametrize("version", [True, False, 0, -1, 1.0, "1", SQLITE_MAX_INTEGER])
def test_optional_edit_version_is_a_strict_positive_integer(
    manual_memory: Memory, version: Any
) -> None:
    initial = manual_memory.save("版本类型核验用的手动事实。", kind="fact")
    with pytest.raises(EvidenceError) as caught:
        manual_memory.edit(initial["id"], expected_version=version, content="不应保存的内容。")
    assert caught.value.code == "invalid_arguments"
    assert manual_memory.get(initial["id"]) == initial
    assert manual_memory.revisions(initial["id"]) == []


def test_same_value_still_checks_version_and_legacy_edit_keeps_its_default(
    manual_memory: Memory,
) -> None:
    initial = manual_memory.save("没有变动的手动事实。", kind="fact")
    revision = manual_memory.revision
    with pytest.raises(EvidenceError, match="版本已变化"):
        manual_memory.edit(
            initial["id"], expected_version=initial["version"] + 1, content=initial["content"]
        )
    assert manual_memory.revision == revision and manual_memory.revisions(initial["id"]) == []
    assert (
        manual_memory.edit(
            initial["id"], expected_version=initial["version"], content=initial["content"]
        )
        == initial
    )
    assert manual_memory.revision == revision
    # Omitting the optional CAS argument keeps the original management writer.
    current = manual_memory.edit(initial["id"], content="默认旧编辑入口仍能保存的事实。")
    assert current["version"] == initial["version"] + 1
    with pytest.raises(EvidenceError, match="版本已变化"):
        manual_memory.edit(
            current["id"], expected_version=initial["version"], content=current["content"]
        )
    assert manual_memory.get(current["id"]) == current
    assert len(manual_memory.revisions(current["id"])) == 1


def test_failure_after_the_version_dml_rolls_back_graph_history_and_vectors(
    manual_memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = manual_memory.save(
        "原来的图结论。",
        kind="fact",
        entities=["甲"],
        relations=[{"subject": "甲", "predicate": "依赖", "object": "乙"}],
    )
    assert manual_memory.store_vector(initial["id"], [1.0, 0.0], "rollback-vector")
    revision = manual_memory.revision
    original_sync = manual_memory._sync_graph
    failure = RuntimeError("controlled error after the actual graph writer")

    def fail_after_graph(*arguments: Any, **keywords: Any) -> None:
        original_sync(*arguments, **keywords)
        raise failure

    monkeypatch.setattr(manual_memory, "_sync_graph", fail_after_graph)
    with pytest.raises(RuntimeError) as caught:
        manual_memory.edit(
            initial["id"],
            expected_version=initial["version"],
            content="不能留下的更正。",
            entities=["丙"],
            relations=[{"subject": "丙", "predicate": "替代", "object": "丁"}],
        )
    assert caught.value is failure
    assert manual_memory.get(initial["id"]) == initial
    assert manual_memory.revisions(initial["id"]) == []
    assert manual_memory.vector_count("rollback-vector") == 1
    assert manual_memory.revision == revision


_SOURCE_ID = "dedup-correction-reference"
_SOURCE = "备库操作应在替换旧副本前先离线核验新副本。"
_WRONG = "备库操作应先替换旧副本，再核验新副本。"
_WINNER = "另一连接已保存的核查结果必须保留。"
_CORRECTION = "这次过期更正不得冒充已经保存成功。"


@pytest.fixture
def dedup_rig(
    tmp_path: Path,
) -> Iterator[tuple[Memory, Memory, dict[str, Any], dict[str, Any]]]:
    path = tmp_path / "dedup-correction.db"
    store = Store(path)
    peer_store = Store(path)
    try:
        memory, peer = Memory(store), Memory(peer_store)
        original, created = memory.register_source(
            {
                "id": _SOURCE_ID,
                "content": _SOURCE,
                "role": "tool",
                "principal": "external:attachment",
                "source": "independent-review-fixture",
            }
        )
        assert created and original is not None
        proposal: dict[str, Any] = {
            "layer": "semantic",
            "content": _WRONG,
            "quote": _SOURCE,
            "entities": ["备库"],
            "relations": [],
        }
        initial = memory.apply_extraction(_SOURCE_ID, [proposal])[0]
        assert initial["kind"] == "fact" and initial["version"] == 1
        assert initial["trusted_owner"] is False
        yield memory, peer, initial, proposal
    finally:
        peer_store.close()
        store.close()


@pytest.mark.parametrize(
    "use_expected_version", [True, False], ids=["strict-cas", "legacy-explicit-none"]
)
def test_dedup_reassignment_respects_cas_and_legacy_default(
    dedup_rig: tuple[Memory, Memory, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    use_expected_version: bool,
) -> None:
    memory, peer, initial, proposal = dedup_rig
    original_validate = memory._validate
    interleaved: dict[str, Any] = {}

    def interleave_after_validation(*arguments: Any, **keywords: Any) -> tuple[str, str, bool]:
        validated = original_validate(*arguments, **keywords)
        assert not interleaved
        assert memory.get(initial["id"]) == initial
        # _save has already read the initial version and its preserved dedup
        # key. This supported full-save entry updates the same ID and clears
        # its old dedup key through the original writer's default argument.
        winner = peer.save(
            _WINNER,
            kind=initial["kind"],
            source=initial["source"],
            identifier=initial["id"],
            entities=initial["entities"],
            relations=initial["relations"],
            confidence=initial["confidence"],
            source_id=initial["source_id"],
            quote=initial["quote"],
            trusted_owner=initial["trusted_owner"],
            claim_key=initial["claim_key"],
        )
        assert winner["id"] == initial["id"]
        assert winner["version"] == initial["version"] + 1
        # Re-applying the exact legal proposal computes its own real dedup key
        # and creates another ID now that the full save released that key.
        duplicate = peer.apply_extraction(_SOURCE_ID, [proposal])[0]
        assert duplicate["id"] != initial["id"]
        assert duplicate["content"] == initial["content"]
        assert duplicate["source_id"] == initial["source_id"]
        history = peer.revisions(initial["id"])
        assert len(history) == 1 and history[0]["snapshot"] == initial
        assert peer.get(initial["id"]) == winner
        assert peer.revisions(duplicate["id"]) == []
        interleaved.update(
            winner=winner,
            duplicate=duplicate,
            history=history,
            revision=peer.revision,
            source=peer.source(_SOURCE_ID),
        )
        return validated

    monkeypatch.setattr(memory, "_validate", interleave_after_validation)
    result: dict[str, Any] | None = None
    rejection: EvidenceError | None = None
    try:
        result = memory.edit(
            initial["id"],
            expected_version=initial["version"] if use_expected_version else None,
            content=_CORRECTION,
            entities=initial["entities"],
            relations=initial["relations"],
        )
    except EvidenceError as exc:
        rejection = exc

    # Establish the actual persisted outcome even on the intended red case:
    # the peer winner survives and no correction revision has been created.
    assert interleaved
    assert memory.get(initial["id"]) == interleaved["winner"]
    assert memory.revisions(initial["id"]) == interleaved["history"]
    assert memory.get(interleaved["duplicate"]["id"]) == interleaved["duplicate"]
    assert memory.revisions(interleaved["duplicate"]["id"]) == []
    assert memory.source(_SOURCE_ID) == interleaved["source"]
    assert memory.revision == interleaved["revision"]
    if use_expected_version:
        assert rejection is not None, (
            "stale correction bypassed CAS through dedup redirection; "
            f"requested_id={initial['id']}, returned={result}"
        )
        assert rejection.code == "evidence_changed"
        assert result is None
    else:
        # Explicit None preserves the pre-existing writer's dedup redirection.
        assert rejection is None
        assert result is not None
        assert result == interleaved["duplicate"]
        assert result["id"] != initial["id"]
        assert result["content"] != _CORRECTION
