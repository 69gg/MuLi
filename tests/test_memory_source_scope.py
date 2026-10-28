from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.memory import Memory
from muli.store import Store, new_id


@pytest.fixture
def memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Memory]:
    monkeypatch.setattr("muli.memory.now", lambda: 1_800_000_000.0)
    store = Store(tmp_path / "source-scope.sqlite3")
    try:
        yield Memory(store)
    finally:
        store.close()


def source(memory: Memory, identifier: str) -> None:
    memory.register_source(
        {
            "id": identifier,
            "content": f"{identifier} 的完整原始资料。",
            "source": "web",
            "role": "user",
            "principal": "owner",
            "session_id": "main",
        }
    )


def fact(
    memory: Memory,
    source_id: str,
    content: str,
    *,
    identifier: str | None = None,
    entities: list[str] | None = None,
    relations: list[dict[str, str]] | None = None,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    original = memory.source(source_id)
    assert original is not None
    with pytest.MonkeyPatch.context() as patcher:
        if identifier is not None:
            chosen_ids = iter((identifier,))

            def next_id() -> str:
                return next(chosen_ids, new_id())

            # Control only the first generated fact ID. Entity IDs and any
            # later identifiers still use the normal generator and save path.
            patcher.setattr("muli.memory.new_id", next_id)
        return memory.save(
            content,
            kind="fact",
            source="web",
            source_id=source_id,
            quote=original["content"],
            entities=entities,
            relations=relations,
            aliases=aliases,
        )


def edge(
    memory: Memory,
    source_id: str,
    subject: str,
    target: str,
    *,
    identifier: str | None = None,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    return fact(
        memory,
        source_id,
        f"{subject} 连接 {target}",
        identifier=identifier,
        relations=[{"subject": subject, "predicate": "连接", "object": target}],
        aliases=aliases,
    )


def identifiers(items: list[dict[str, Any]]) -> set[str]:
    return {item["id"] for item in items}


def test_scope_normalizes_exact_ids_without_expanding_or_trimming(memory: Memory) -> None:
    assert memory.source_scope(None) is None
    assert memory.source_scope(["b", "a", "b"]) == ("a", "b")
    assert memory.source_scope(("资料", " a ", "a")) == (" a ", "a", "资料")
    assert memory.source_scope(["unknown"]) == ("unknown",)
    assert memory.source_scope(["x" * 12000]) == ("x" * 12000,)


@pytest.mark.parametrize(
    "invalid",
    [[], (), "inside", {"inside"}, {"id": "inside"}, [None], [1], [""], ["\ud800"]],
)
def test_invalid_scope_is_rejected_by_every_public_entry(memory: Memory, invalid: Any) -> None:
    calls: list[Callable[[], Any]] = [
        lambda: memory.source_scope(invalid),
        lambda: memory.search("backup", source_ids=invalid),
        lambda: memory.traverse("MuLi", source_ids=invalid),
        lambda: memory.vector_search([1.0], "model", source_ids=invalid),
        lambda: memory.vector_count("model", source_ids=invalid),
        lambda: memory.communities_for([], source_ids=invalid),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()


def test_scope_limits_reject_instead_of_truncating(memory: Memory) -> None:
    with pytest.raises(ValueError):
        memory.source_scope(["same"] * (memory.max_candidates + 1))
    with pytest.raises(ValueError):
        memory.source_scope(["x" * 12000, "y"])
    with pytest.raises(ValueError):
        memory.search("", source_ids=["x" * 12001])


def test_source_filter_precedes_lexical_and_recent_candidate_limits(memory: Memory) -> None:
    source(memory, "inside")
    source(memory, "outside")
    inside = fact(memory, "inside", "backup steps", identifier="z-inside")
    for index in range(3):
        item = fact(memory, "outside", "backup steps", identifier=f"a-outside-{index}")
        with memory.store.db:
            memory.store.db.execute(
                "UPDATE memories SET updated_at=? WHERE id=?", (index + 2, item["id"])
            )
    with memory.store.db:
        memory.store.db.execute("UPDATE memories SET updated_at=1 WHERE id=?", (inside["id"],))
    memory.max_candidates = 2

    assert inside["id"] not in identifiers(memory.search("backup", hops=0))
    assert inside["id"] not in identifiers(memory.search(""))
    assert identifiers(memory.search("backup", source_ids=["inside"], hops=0)) == {inside["id"]}
    assert identifiers(memory.search("", source_ids=["inside"])) == {inside["id"]}


def test_source_filter_precedes_vector_scan_and_count(memory: Memory) -> None:
    source(memory, "inside")
    source(memory, "outside")
    inside = fact(memory, "inside", "archived backup", identifier="inside-fact")
    outside = fact(memory, "outside", "recent unrelated", identifier="outside-fact")
    assert memory.store_vector(inside["id"], [1.0, 0.0], "model")
    assert memory.store_vector(outside["id"], [0.0, 1.0], "model")
    with memory.store.db:
        memory.store.db.execute("UPDATE memories SET updated_at=1 WHERE id=?", (inside["id"],))
        memory.store.db.execute("UPDATE memories SET updated_at=2 WHERE id=?", (outside["id"],))
    memory.max_vector_scan = 1

    assert identifiers(memory.vector_search([1.0, 0.0], "model")) == {outside["id"]}
    assert identifiers(memory.vector_search([1.0, 0.0], "model", source_ids=["inside"])) == {
        inside["id"]
    }
    assert memory.vector_count("model") == 2
    assert memory.vector_count("model", source_ids=["inside"]) == 1


def test_alias_only_from_another_source_cannot_seed_scoped_graph(memory: Memory) -> None:
    source(memory, "inside")
    source(memory, "outside")
    edge(memory, "outside", "MuLi", "External", aliases={"Lumi": "MuLi"})
    inside = edge(memory, "inside", "MuLi", "Internal")

    assert memory.traverse("Lumi")["nodes"]
    assert memory.traverse("Lumi", source_ids=["inside"])["nodes"] == []
    assert memory.search("Lumi", source_ids=["inside"]) == []
    canonical = memory.traverse("MuLi", source_ids=["inside"])
    assert {item["memory_id"] for item in canonical["edges"]} == {inside["id"]}
    assert canonical["alias_evidence"][0]["source_id"] == "inside"
    assert canonical["alias_evidence"][0]["memory_id"] == inside["id"]


def test_shared_alias_uses_scoped_contribution_and_full_path(memory: Memory) -> None:
    source(memory, "inside")
    source(memory, "outside")
    edge(memory, "outside", "MuLi", "External", identifier="a-external", aliases={"Lumi": "MuLi"})
    inside = edge(
        memory, "inside", "MuLi", "Internal", identifier="z-internal", aliases={"Lumi": "MuLi"}
    )
    tail = edge(memory, "inside", "Internal", "Destination", identifier="zz-tail")

    assert memory.traverse("Lumi")["alias_evidence"][0]["source_id"] == "outside"
    graph = memory.traverse("Lumi", source_ids=["inside"])
    proof = graph["alias_evidence"][0]
    assert proof["source_id"] == "inside" and proof["memory_id"] == inside["id"]
    assert proof["has_more"] is False
    items = memory.search("Lumi", source_ids=["inside"])
    path = next(item["graph_via"] for item in items if item["id"] == tail["id"])
    assert path["complete"] is True
    assert path["seed"] == proof
    assert {step["source_id"] for step in path["steps"]} == {"inside"}


def test_superseded_alias_contribution_does_not_seed_scoped_graph(memory: Memory) -> None:
    source(memory, "inside")
    alias = edge(memory, "inside", "MuLi", "Internal", aliases={"Lumi": "MuLi"})
    edge(memory, "inside", "MuLi", "Another")
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memory_meta SET status='superseded' WHERE memory_id=?", (alias["id"],)
        )
    assert memory.traverse("Lumi", source_ids=["inside"])["nodes"] == []
    assert memory.traverse("MuLi", source_ids=["inside"])["nodes"]


def test_multihop_query_never_crosses_external_bridge(memory: Memory) -> None:
    source(memory, "inside")
    source(memory, "outside")
    first = edge(memory, "inside", "RootNode", "InnerNode")
    bridge = edge(memory, "outside", "InnerNode", "OuterNode")
    tail = edge(memory, "inside", "OuterNode", "Destination")

    assert tail["id"] in identifiers(memory.search("RootNode", hops=3))
    graph = memory.traverse("RootNode", hops=3, source_ids=["inside"])
    assert {item["memory_id"] for item in graph["edges"]} == {first["id"]}
    assert identifiers(memory.search("RootNode", hops=3, source_ids=["inside"])) == {first["id"]}
    combined = memory.search("RootNode", hops=3, source_ids=["inside", "outside"])
    assert {first["id"], bridge["id"], tail["id"]} <= identifiers(combined)
    path = next(item["graph_via"] for item in combined if item["id"] == tail["id"])
    assert {step["source_id"] for step in path["steps"]} == {"inside", "outside"}


def test_scoped_community_rebuilds_members_without_external_or_manual_text(memory: Memory) -> None:
    source(memory, "inside")
    source(memory, "outside")
    inside = edge(memory, "inside", "MuLi", "Internal")
    outside = edge(memory, "outside", "MuLi", "ExternalSecret")
    manual = memory.save(
        "ManualSecret",
        relations=[{"subject": "MuLi", "predicate": "连接", "object": "ManualSecret"}],
    )
    global_communities = memory.communities_for([inside["id"]])
    assert any(outside["id"] in item["evidence"] for item in global_communities)
    before = memory.store.db.total_changes
    revision = memory.revision

    scoped = memory.communities_for(
        [inside["id"], outside["id"], manual["id"]], source_ids=["inside"]
    )

    assert scoped
    for community in scoped:
        assert community["id"] not in {item["id"] for item in global_communities}
        assert community["evidence"] == [inside["id"]]
        assert community["source_ids"] == ["inside"]
        assert set(community["entities"]) == {"MuLi", "Internal"}
        assert "ExternalSecret" not in community["title"] + community["summary"]
        assert "ManualSecret" not in community["title"] + community["summary"]
    assert memory.store.db.total_changes == before
    assert memory.revision == revision
    assert memory.communities_for([inside["id"]]) == global_communities


def test_scoped_community_filters_before_member_limit(memory: Memory) -> None:
    source(memory, "inside")
    source(memory, "outside")
    inside = edge(memory, "inside", "MuLi", "Internal", identifier="z-internal")
    for index in range(5):
        edge(memory, "outside", "MuLi", f"External{index}", identifier=f"a-external-{index}")
    memory.max_community_members = 2
    projection = memory.communities_for([inside["id"]], source_ids=["inside"])
    assert projection
    assert projection[0]["evidence"] == [inside["id"]]
    assert projection[0]["truncated"] is False


def test_scoped_community_reports_existing_graph_and_member_caps(memory: Memory) -> None:
    source(memory, "inside")
    first = edge(memory, "inside", "MuLi", "First")
    for index in range(4):
        edge(memory, "inside", "MuLi", f"Node{index}")
    memory.max_graph_edges = 1
    memory.max_community_members = 2
    projection = memory.communities_for([first["id"]], source_ids=["inside"])
    assert projection
    assert all(item["truncated"] for item in projection)
    assert all(len(item["evidence"]) <= 4 for item in projection)


def test_combined_scopes_are_order_independent_and_queries_remain_read_only(memory: Memory) -> None:
    for name in ("first", "second"):
        source(memory, name)
    first = edge(memory, "first", "RootNode", "MiddleNode")
    second = edge(memory, "second", "MiddleNode", "Destination")
    for item in (first, second):
        assert memory.store_vector(item["id"], [1.0], "model")
    before = memory.store.db.total_changes
    revision = memory.revision
    scopes = (["second", "first", "second"], ("first", "second"))

    assert memory.search("RootNode", source_ids=scopes[0]) == memory.search(
        "RootNode", source_ids=scopes[1]
    )
    assert memory.traverse("RootNode", source_ids=scopes[0]) == memory.traverse(
        "RootNode", source_ids=scopes[1]
    )
    assert memory.vector_search([1.0], "model", source_ids=scopes[0]) == memory.vector_search(
        [1.0], "model", source_ids=scopes[1]
    )
    assert memory.vector_count("model", source_ids=scopes[0]) == 2
    assert memory.communities_for([first["id"]], source_ids=scopes[0]) == memory.communities_for(
        [first["id"]], source_ids=scopes[1]
    )
    assert memory.store.db.total_changes == before
    assert memory.revision == revision
    assert not memory.store.db.in_transaction


@pytest.mark.parametrize("forgotten", [False, True])
def test_unknown_or_forgotten_scope_is_empty_in_every_retrieval(
    memory: Memory, forgotten: bool
) -> None:
    source(memory, "inside")
    source(memory, "outside")
    inside = edge(memory, "inside", "MuLi", "Internal")
    edge(memory, "outside", "MuLi", "External")
    assert memory.store_vector(inside["id"], [1.0], "model")
    if forgotten:
        assert memory.forget_source("inside")
    scope = ["inside"] if forgotten else ["unknown') OR 1=1 --"]
    assert memory.search("MuLi", source_ids=scope) == []
    assert memory.search("", source_ids=scope) == []
    graph = memory.traverse("MuLi", source_ids=scope)
    assert graph["nodes"] == graph["edges"] == graph["alias_evidence"] == []
    assert memory.vector_search([1.0], "model", source_ids=scope) == []
    assert memory.vector_count("model", source_ids=scope) == 0
    assert memory.communities_for([inside["id"]], source_ids=scope) == []
    assert memory.search("MuLi")


def test_omitted_scope_keeps_existing_global_results(memory: Memory) -> None:
    source(memory, "inside")
    inside = edge(memory, "inside", "MuLi", "Internal")
    manual = memory.save("MuLi 的手动资料", entities=["MuLi"])
    assert memory.store_vector(inside["id"], [1.0], "model")
    assert memory.store_vector(manual["id"], [1.0], "model")
    assert memory.search("MuLi") == memory.search("MuLi", source_ids=None)
    assert memory.traverse("MuLi") == memory.traverse("MuLi", source_ids=None)
    assert memory.vector_search([1.0], "model") == memory.vector_search(
        [1.0], "model", source_ids=None
    )
    assert memory.vector_count("model") == memory.vector_count("model", source_ids=None)
    assert memory.communities_for([inside["id"]]) == memory.communities_for(
        [inside["id"]], source_ids=None
    )
    assert manual["id"] in identifiers(memory.search("MuLi"))
    assert manual["id"] not in identifiers(memory.search("MuLi", source_ids=["inside"]))
