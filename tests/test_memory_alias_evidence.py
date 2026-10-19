from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.memory import Memory, normalize_entity
from muli.store import Store


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    store = Store(tmp_path / "aliases.sqlite3")
    try:
        yield Memory(store)
    finally:
        store.close()


def sourced(
    memory: Memory,
    source_id: str,
    *,
    alias: str = "Lumi",
    canonical: str = "MuLi",
    subject: str = "MuLi",
    predicate: str = "维护",
    target: str = "项目甲",
    fact_suffix: str = "",
) -> dict[str, Any]:
    declaration = f"{alias} 就是 {canonical}。" if alias else ""
    original = f"{declaration}{subject}{predicate}{target}。{fact_suffix}"
    memory.register_source(
        {
            "id": source_id,
            "content": original,
            "role": "user",
            "principal": "owner",
            "source": "web",
            "session_id": "main",
        },
        trusted_owner=True,
    )
    return memory.apply_extraction(
        source_id,
        [
            {
                "content": f"{subject}{predicate}{target}{fact_suffix}",
                "layer": "semantic",
                "quote": original,
                "relations": [{"subject": subject, "predicate": predicate, "object": target}],
            }
        ],
        aliases={alias: canonical} if alias else {},
    )[0]


def proof_rows(memory: Memory, alias: str = "Lumi") -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in memory.store.db.execute(
            "SELECT * FROM memory_alias_evidence WHERE alias=? ORDER BY memory_id",
            (normalize_entity(alias),),
        )
    ]


def seed(memory: Memory, query: str = "Lumi") -> dict[str, Any]:
    return memory.traverse(query, hops=0)["alias_evidence"][0]


@pytest.mark.parametrize("forgotten", ["first", "second"])
def test_forgetting_either_source_keeps_independent_alias_and_two_hops(
    memory: Memory, forgotten: str
) -> None:
    facts = {name: sourced(memory, name) for name in ("first", "second")}
    tail = sourced(memory, "third", alias="", subject="项目甲", predicate="使用", target="工具乙")
    assert len(proof_rows(memory)) == 2
    assert seed(memory)["has_more"] is True
    assert tail["id"] in {item["id"] for item in memory.search("Lumi", hops=2)}

    remaining = "second" if forgotten == "first" else "first"
    assert memory.forget_source(forgotten)
    assert memory.source(forgotten) is None
    assert memory.source(remaining) is not None
    assert memory.get(facts[remaining]["id"]) is not None
    assert [row["source_id"] for row in proof_rows(memory)] == [remaining]
    assert seed(memory)["source_id"] == remaining
    assert seed(memory)["has_more"] is False
    assert tail["id"] in {item["id"] for item in memory.search("Lumi", hops=2)}
    assert memory.store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_last_source_purges_alias_entity_communities_and_proofs(memory: Memory) -> None:
    sourced(memory, "first")
    sourced(memory, "second")
    assert memory.communities("Lumi")
    assert memory.forget_source("first")
    assert memory.forget_source("second")
    for table in (
        "memory_alias_evidence",
        "memory_aliases",
        "memory_entities",
        "memory_mentions",
        "memory_edges",
        "memory_communities",
        "memory_community_nodes",
        "memory_community_members",
    ):
        assert memory.store.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None, table
    assert memory.traverse("Lumi")["alias_evidence"] == []
    assert not memory.forget_source("second")


def test_visible_edit_preserves_both_original_contributions(memory: Memory) -> None:
    item = sourced(memory, "first")
    sourced(memory, "second")
    before = proof_rows(memory)
    changed = memory.edit(
        item["id"], content="现在的更正正文", source="更正标签", entities=[], relations=[]
    )
    assert changed["source_id"] == "first"
    assert changed["quote"] == item["quote"]
    assert proof_rows(memory) == before
    assert seed(memory)["kind"] == "source"
    assert memory.source("first")["content"] == item["quote"]


def test_alias_only_reference_survives_edit_without_mentions_then_is_forgotten(
    memory: Memory,
) -> None:
    item = sourced(memory, "first")
    memory.edit(item["id"], content="仅保留更正内容", entities=[], relations=[])
    assert memory.traverse("Lumi", hops=0)["nodes"]
    assert seed(memory)["memory_id"] == item["id"]
    memory.forget_source("first")
    assert not memory.traverse("Lumi", hops=0)["nodes"]
    assert not proof_rows(memory)


def test_full_save_replaces_only_its_own_contribution(memory: Memory) -> None:
    first = sourced(memory, "first")
    second = sourced(memory, "second")
    memory.save(
        "整份替换不再包含别名",
        identifier=first["id"],
        source_id="first",
        quote=first["quote"],
        aliases={},
    )
    assert [row["memory_id"] for row in proof_rows(memory)] == [second["id"]]
    assert seed(memory)["source_id"] == "second"
    memory.forget_source("second")
    assert not memory.traverse("Lumi", hops=0)["nodes"]
    assert memory.source("first") is not None


def test_manual_delete_preserves_another_manual_contribution(memory: Memory) -> None:
    first = memory.save("第一份手工声明", aliases={"Lumi": "MuLi"})
    second = memory.save("第二份手工声明", aliases={"Lumi": "MuLi"})
    assert seed(memory)["kind"] == "manual"
    assert seed(memory)["source_id"] is None
    assert memory.delete(first["id"])
    assert seed(memory)["memory_id"] == second["id"]
    assert memory.delete(second["id"])
    assert not memory.traverse("Lumi")["nodes"]


def test_retried_fact_adds_missing_alias_but_never_duplicates_proof(memory: Memory) -> None:
    memory.register_source({"id": "original", "content": "Lumi 就是 MuLi。MuLi 维护项目甲。"})
    proposal = {"content": "MuLi 维护项目甲", "layer": "semantic", "quote": "MuLi 维护项目甲"}
    first = memory.apply_extraction("original", [proposal])[0]
    revision = memory.revision
    second = memory.apply_extraction("original", [proposal], aliases={"Lumi": "MuLi"})[0]
    assert first["id"] == second["id"]
    assert memory.revision > revision
    assert seed(memory)["source_id"] == "original"
    revision = memory.revision
    third = memory.apply_extraction("original", [proposal], aliases={"Lumi": "MuLi"})[0]
    assert third["id"] == first["id"]
    assert len(proof_rows(memory)) == 1
    assert memory.revision == revision


def test_multiple_facts_from_one_source_are_contributions_not_independent_sources(
    memory: Memory,
) -> None:
    memory.register_source({"id": "original", "content": "Lumi 就是 MuLi。另有第二件事。"})
    items = memory.apply_extraction(
        "original",
        [
            {"content": text, "layer": "semantic", "quote": text}
            for text in ("Lumi 就是 MuLi", "另有第二件事")
        ],
        aliases={"Lumi": "MuLi"},
    )
    assert len({item["id"] for item in items}) == 2
    assert len(proof_rows(memory)) == 2
    assert {row["source_id"] for row in proof_rows(memory)} == {"original"}
    assert seed(memory)["has_more"] is True
    assert "source_count" not in seed(memory)
    memory.forget_source("original")
    assert not proof_rows(memory)
    assert not memory.traverse("Lumi")["nodes"]


def test_conflicting_canonical_rolls_back_new_fact_and_prior_alias_in_same_write(
    memory: Memory,
) -> None:
    sourced(memory, "first")
    before = proof_rows(memory)
    revision = memory.revision
    with pytest.raises(ValueError, match="静默合并"):
        memory.save("失败的新记忆", aliases={"new-name": "新实体", "Lumi": "另一实体"})
    assert proof_rows(memory) == before
    assert not memory.traverse("new-name")["nodes"]
    assert not memory.search("失败的新记忆")
    assert memory.revision == revision


def test_two_foreign_keys_do_not_allow_a_dedup_fact_to_claim_a_different_source(
    memory: Memory,
) -> None:
    first = sourced(memory, "first")
    memory.register_source({"id": "other", "content": "合法但无关的来源"})
    key = memory.store.db.execute(
        "SELECT dedup_key FROM memory_meta WHERE memory_id=?", (first["id"],)
    ).fetchone()[0]
    with pytest.raises(ValueError, match="原始来源不一致"):
        memory.save(
            "重复键冒领",
            source_id="other",
            quote="无关的来源",
            dedup_key=key,
            aliases={"new-name": "MuLi"},
        )
    assert not memory.traverse("new-name")["nodes"]
    assert [row["source_id"] for row in proof_rows(memory)] == ["first"]


def test_failed_forget_rolls_back_cascades_and_projection(memory: Memory) -> None:
    sourced(memory, "first")
    sourced(memory, "second")
    before, entry, revision = proof_rows(memory), seed(memory), memory.revision
    target = str(entry["source_id"])
    with memory.store.db:
        memory.store.db.execute(
            "CREATE TRIGGER abort_source_delete BEFORE DELETE ON memory_sources "
            "BEGIN SELECT RAISE(ABORT,'test rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        memory.forget_source(target)
    assert proof_rows(memory) == before
    assert seed(memory) == entry
    assert memory.source(target) is not None
    assert memory.revision == revision
    assert not memory.store.db.execute("SELECT * FROM memory_forgotten_sources").fetchall()


def legacy_reopen(memory: Memory) -> Memory:
    """Remove only the new table/marker to simulate a database written by 2.7."""
    with memory.store.db:
        memory.store.db.execute("DROP TABLE memory_alias_evidence")
        memory.store.db.execute("DELETE FROM memory_state WHERE name='alias_evidence_version'")
    return Memory(memory.store)


def test_legacy_migration_copies_only_known_support_and_does_not_reinfer_lost_source(
    memory: Memory,
) -> None:
    first = sourced(memory, "first")
    sourced(memory, "second")
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memory_aliases SET memory_id=?,source_id='first' WHERE alias='lumi'",
            (first["id"],),
        )
    migrated = legacy_reopen(memory)
    assert [row["source_id"] for row in proof_rows(migrated)] == ["first"]
    assert seed(migrated)["has_more"] is False
    assert proof_rows(Memory(memory.store)) == proof_rows(migrated)
    migrated.forget_source("first")
    assert migrated.source("second") is not None
    assert not migrated.traverse("Lumi")["nodes"]


@pytest.mark.parametrize("reference", ["source_only", "none", "mismatched_source"])
def test_legacy_unproven_lookup_stays_unknown_without_fabricated_memory(
    memory: Memory, reference: str
) -> None:
    fact = sourced(memory, "first")
    memory.register_source({"id": "other", "content": "不同来源"})
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memory_aliases SET source_id=?,memory_id=? WHERE alias='lumi'",
            (
                None if reference == "none" else "other",
                fact["id"] if reference == "mismatched_source" else None,
            ),
        )
    migrated = legacy_reopen(memory)
    assert not proof_rows(migrated)
    entry = seed(migrated)
    assert entry["kind"] == "legacy_unknown"
    assert entry["memory_id"] == (fact["id"] if reference == "mismatched_source" else None)
    assert entry["has_more"] is False
    if reference == "source_only":
        migrated.forget_source("other")
        assert not migrated.traverse("Lumi")["nodes"]
        assert migrated.get(fact["id"]) is not None


def test_legacy_manual_contribution_migrates_but_structural_self_name_does_not(
    memory: Memory,
) -> None:
    item = memory.save("手工声明", aliases={"Lumi": "MuLi"})
    migrated = legacy_reopen(memory)
    assert seed(migrated)["kind"] == "manual"
    assert seed(migrated)["memory_id"] == item["id"]
    assert not proof_rows(migrated, "MuLi")
    assert seed(migrated, "MuLi")["kind"] == "direct"
    migrated.delete(item["id"])
    assert not migrated.traverse("MuLi")["nodes"]


def test_legacy_owned_canonical_row_is_repaired_as_direct_until_last_real_reference(
    memory: Memory,
) -> None:
    first = sourced(memory, "first")
    # The second fact contains only a different alias in its text, so canonical
    # lookup cannot rely on _seeds' lexical fallback after the first source goes.
    memory.register_source({"id": "second", "content": "保留项目甲记录"})
    second = memory.save(
        "保留项目甲记录",
        source_id="second",
        quote="保留项目甲记录",
        aliases={"another-name": "MuLi"},
    )
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memory_aliases SET source_id='first',memory_id=? WHERE alias='muli'",
            (first["id"],),
        )
    migrated = legacy_reopen(memory)
    assert not proof_rows(migrated, "MuLi")
    migrated.forget_source("first")
    entry = seed(migrated, "MuLi")
    assert entry["kind"] == "direct" and entry["source_id"] is None
    assert entry["memory_id"] is None
    assert migrated.get(second["id"]) is not None
    migrated.forget_source("second")
    assert not migrated.traverse("MuLi")["nodes"]


def test_proofs_and_remaining_projection_survive_a_real_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "reopen.sqlite3"
    store = Store(path)
    try:
        memory = Memory(store)
        sourced(memory, "first")
        sourced(memory, "second")
        memory.forget_source("first")
    finally:
        store.close()
    reopened = Store(path)
    try:
        memory = Memory(reopened)
        assert [row["source_id"] for row in proof_rows(memory)] == ["second"]
        assert seed(memory)["source_id"] == "second"
        memory.forget_source("second")
        assert not memory.traverse("Lumi")["nodes"]
    finally:
        reopened.close()


def assert_real_path(memory: Memory, path: dict[str, Any], max_hops: int) -> None:
    assert path["complete"] is True
    assert 1 <= len(path["steps"]) <= max_hops
    previous = path["seed_id"]
    for step in path["steps"]:
        assert step["from_id"] == previous
        edge = memory.store.db.execute(
            "SELECT source_id FROM memory_edges WHERE memory_id=? AND subject_id=? "
            "AND predicate=? AND object_id=?",
            (step["memory_id"], step["subject_id"], step["predicate"], step["object_id"]),
        ).fetchone()
        assert edge is not None and edge["source_id"] == step["source_id"]
        if step["direction"] == "forward":
            assert step["from_id"] == step["subject_id"]
            assert step["to_id"] == step["object_id"]
        else:
            assert step["direction"] == "reverse"
            assert step["from_id"] == step["object_id"]
            assert step["to_id"] == step["subject_id"]
        previous = step["to_id"]


def test_alias_to_two_hops_returns_actual_source_chain_and_revision(memory: Memory) -> None:
    first = sourced(memory, "first")
    second = sourced(
        memory, "second", alias="", subject="项目甲", predicate="使用", target="工具乙"
    )
    graph = memory.traverse("Lumi", hops=2)
    assert len(graph["paths"]) == len(graph["edges"])
    assert graph["revision"] == memory.revision
    assert graph["alias_evidence"][0]["memory_id"] == first["id"]
    for path in graph["paths"]:
        assert_real_path(memory, path, 2)
    result = next(item for item in memory.search("Lumi", hops=2) if item["id"] == second["id"])
    assert result["matched_by"] == "graph"
    via = result["graph_via"]
    assert via["seed"] == graph["alias_evidence"][0]
    assert via["seed"]["name"] == "MuLi"
    assert via["revision"] == graph["revision"]
    assert [step["source_id"] for step in via["steps"]] == ["first", "second"]
    assert [step["memory_id"] for step in via["steps"]] == [first["id"], second["id"]]
    assert [(step["subject"], step["object"]) for step in via["steps"]] == [
        ("MuLi", "项目甲"),
        ("项目甲", "工具乙"),
    ]
    assert "alias_quote" not in via["seed"]
    assert_real_path(memory, via, 2)


def test_reverse_edge_path_preserves_predicate_direction(memory: Memory) -> None:
    sourced(memory, "first", subject="项目甲", predicate="属于", target="MuLi")
    sourced(memory, "second", alias="", subject="工具乙", predicate="服务", target="项目甲")
    graph = memory.traverse("Lumi", hops=2)
    path = next(path for path in graph["paths"] if len(path["steps"]) == 2)
    assert [step["direction"] for step in path["steps"]] == ["reverse", "reverse"]
    assert_real_path(memory, path, 2)


def test_cycle_and_multiple_seeds_have_real_contiguous_paths_with_limits(memory: Memory) -> None:
    sourced(memory, "first")
    sourced(memory, "second", alias="", subject="项目甲", predicate="使用", target="工具乙")
    sourced(memory, "third", alias="", subject="工具乙", predicate="反馈", target="MuLi")
    memory.save(
        "无关侧边", relations=[{"subject": "项目甲", "predicate": "旁连", "object": "旁支"}]
    )
    graph = memory.traverse("Lumi 工具乙", hops=8)
    assert len(graph["alias_evidence"]) >= 2
    roots = {entry["entity_id"] for entry in graph["alias_evidence"]}
    for path in graph["paths"]:
        assert path["seed_id"] in roots
        assert_real_path(memory, path, 8)
        assert len({step["from_id"] for step in path["steps"][:-1]}) == len(path["steps"][:-1])
    assert graph == memory.traverse("Lumi 工具乙", hops=8)


def test_lexical_match_does_not_claim_graph_path_and_zero_hops_keeps_seed_proof(
    memory: Memory,
) -> None:
    sourced(memory, "first")
    direct = memory.save("Lumi 是词法命中")
    result = next(item for item in memory.search("Lumi") if item["id"] == direct["id"])
    assert result["matched_by"] == "lexical"
    assert "graph_via" not in result
    graph = memory.traverse("Lumi", hops=0)
    assert graph["paths"] == [] and graph["edges"] == []
    assert graph["alias_evidence"]
    assert graph["truncated"] is False


@pytest.mark.parametrize("bound", ["nodes", "edges"])
def test_truncation_keeps_only_complete_paths_and_actual_seeds(memory: Memory, bound: str) -> None:
    sourced(memory, "first")
    sourced(memory, "second", alias="", subject="项目甲", predicate="使用", target="工具乙")
    sourced(memory, "third", alias="", subject="项目甲", predicate="使用", target="工具丙")
    memory.max_candidates = 2 if bound == "nodes" else 16
    memory.max_graph_edges = 1 if bound == "edges" else 16
    graph = memory.traverse("Lumi", hops=8)
    assert graph["truncated"] is True
    assert len(graph["nodes"]) <= memory.max_candidates
    assert len(graph["edges"]) <= memory.max_graph_edges
    assert len(graph["paths"]) == len(graph["edges"])
    for path in graph["paths"]:
        assert_real_path(memory, path, 8)


def test_migration_and_alias_operations_never_need_network(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("记忆别名操作不可访问网络")

    monkeypatch.setattr("socket.create_connection", no_network)
    first = sourced(memory, "first")
    sourced(memory, "second")
    migrated = legacy_reopen(memory)
    assert migrated.get(first["id"]) is not None
    assert migrated.search("Lumi")
    migrated.forget_source("first")
    migrated.forget_source("second")


def test_normalized_alias_retries_share_one_contribution(memory: Memory) -> None:
    item = memory.save("已规范化的别名", aliases={"Ｌｕｍｉ": "ＭｕＬｉ", "lumi": "MuLi"})
    assert [row["memory_id"] for row in proof_rows(memory)] == [item["id"]]
    assert seed(memory, "Ｌｕｍｉ")["canonical"] == "muli"
    assert seed(memory)["has_more"] is False


def test_failed_migration_rolls_back_marker_and_proofs_then_can_retry(memory: Memory) -> None:
    sourced(memory, "first")
    with memory.store.db:
        memory.store.db.execute("DELETE FROM memory_alias_evidence")
        memory.store.db.execute("DELETE FROM memory_state WHERE name='alias_evidence_version'")
        memory.store.db.execute(
            "CREATE TRIGGER abort_proof_insert BEFORE INSERT ON memory_alias_evidence "
            "BEGIN SELECT RAISE(ABORT,'migration rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="migration rollback"):
        Memory(memory.store)
    assert not proof_rows(memory)
    assert not memory.store.db.execute(
        "SELECT 1 FROM memory_state WHERE name='alias_evidence_version'"
    ).fetchone()
    with memory.store.db:
        memory.store.db.execute("DROP TRIGGER abort_proof_insert")
    migrated = Memory(memory.store)
    assert [row["source_id"] for row in proof_rows(migrated)] == ["first"]


def test_migration_and_seed_evidence_do_not_read_source_bodies(memory: Memory) -> None:
    sourced(memory, "first")
    with memory.store.db:
        memory.store.db.execute("DELETE FROM memory_alias_evidence")
        memory.store.db.execute("DELETE FROM memory_state WHERE name='alias_evidence_version'")

    def no_source_body(
        operation: int,
        table: str | None,
        column: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if operation == sqlite3.SQLITE_READ and table == "memory_sources" and column == "content":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    memory.store.db.set_authorizer(no_source_body)
    try:
        migrated = Memory(memory.store)
        assert seed(migrated)["source_id"] == "first"
    finally:
        memory.store.db.set_authorizer(None)


def test_canonical_name_embedded_in_phrase_is_a_direct_seed_not_an_alias_assertion(
    memory: Memory,
) -> None:
    sourced(memory, "original", alias="", subject="项目甲", predicate="使用", target="工具乙")
    graph = memory.traverse("请查看项目甲的相关资料", hops=1)
    assert graph["alias_evidence"]
    entry = graph["alias_evidence"][0]
    assert entry["kind"] == "direct" and entry["alias"] is None
    assert entry["memory_id"] is None and entry["source_id"] is None
    assert graph["paths"][0]["seed_id"] == entry["entity_id"]


def test_failed_full_replacement_restores_original_alias_and_fact(memory: Memory) -> None:
    first = sourced(memory, "first")
    memory.save("另外的声明", aliases={"other-name": "另一个人"})
    before, revision = proof_rows(memory), memory.revision
    with pytest.raises(ValueError, match="静默合并"):
        memory.save(
            "替换未提交",
            identifier=first["id"],
            source_id="first",
            quote=first["quote"],
            aliases={"Lumi": "MuLi", "other-name": "不一致的人"},
        )
    assert memory.get(first["id"]) == first
    assert proof_rows(memory) == before
    assert seed(memory)["memory_id"] == first["id"]
    assert memory.revisions(first["id"]) == []
    assert memory.revision == revision


def test_forgotten_source_cannot_recreate_alias_from_late_extraction(memory: Memory) -> None:
    first = sourced(memory, "first")
    memory.forget_source("first")
    assert (
        memory.apply_extraction(
            "first",
            [{"content": first["content"], "layer": "semantic", "quote": first["quote"]}],
            aliases={"Lumi": "MuLi"},
        )
        == []
    )
    assert not proof_rows(memory)
    assert not memory.traverse("Lumi")["nodes"]


@pytest.mark.parametrize(
    ("legacy_kind", "forget_first"),
    [
        ("source_only", "fresh"),
        ("source_only", "legacy"),
        ("no_references", "fresh"),
        ("mismatched_source", "fresh"),
        ("mismatched_source", "legacy"),
    ],
)
def test_new_contribution_does_not_replace_legacy_lookup_lifetime(
    memory: Memory, legacy_kind: str, forget_first: str
) -> None:
    original = sourced(memory, "original")
    keeper = memory.save("保留规范实体", entities=["MuLi"])
    memory.register_source({"id": "legacy", "content": "旧的 Lumi 名称资料"})
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memory_aliases SET source_id=?,memory_id=? WHERE alias='lumi'",
            (
                None if legacy_kind == "no_references" else "legacy",
                original["id"] if legacy_kind == "mismatched_source" else None,
            ),
        )
    migrated = legacy_reopen(memory)
    before = seed(migrated)
    assert before["kind"] == "legacy_unknown"
    fresh = sourced(migrated, "fresh")
    assert seed(migrated)["memory_id"] == fresh["id"]
    assert seed(migrated)["kind"] == "source"
    projection = migrated.store.db.execute(
        "SELECT memory_id,source_id FROM memory_aliases WHERE alias='lumi'"
    ).fetchone()
    assert dict(projection) == {"memory_id": before["memory_id"], "source_id": before["source_id"]}
    if forget_first == "fresh":
        migrated.forget_source("fresh")
        assert seed(migrated) == before
        if legacy_kind == "mismatched_source":
            # Either FK owns this one legacy lookup; deleting its memory must
            # retire it even while its inconsistent legacy source still exists.
            migrated.forget_source("original")
            assert migrated.source("legacy") is not None
        else:
            migrated.forget_source("legacy")
        if legacy_kind == "no_references":
            assert seed(migrated) == before
            migrated.forget_source("original")
            migrated.delete(keeper["id"])
    else:
        migrated.forget_source("legacy")
        assert seed(migrated)["memory_id"] == fresh["id"]
        assert seed(migrated)["source_id"] == "fresh"
        migrated.forget_source("fresh")
    assert not migrated.traverse("Lumi", hops=0)["nodes"]
