from __future__ import annotations

from pathlib import Path

import pytest

from muli.memory import Memory
from muli.store import Store


def test_old_indexed_memory_is_not_lost_to_recent_candidate_limit(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    old = memory.save("早期的独特配置是青瓷蓝", entities=["青瓷蓝"])
    with store.db:
        store.db.execute("UPDATE memories SET updated_at=1 WHERE id=?", (old["id"],))
    for index in range(20):
        memory.save(f"新的无关内容{index}")
    assert memory.search("青瓷蓝")[0]["id"] == old["id"]
    store.close()


def test_two_hop_retrieval_retains_source_evidence(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    first = memory.save(
        "沐璃参与项目甲",
        entities=["沐璃", "项目甲"],
        relations=[{"subject": "沐璃", "predicate": "参与", "object": "项目甲"}],
        source="conversation:a",
    )
    second = memory.save(
        "项目甲依赖工具乙",
        entities=["项目甲", "工具乙"],
        relations=[{"subject": "项目甲", "predicate": "依赖", "object": "工具乙"}],
        source="conversation:b",
    )
    results = memory.search("沐璃")
    assert {first["id"], second["id"]} <= {item["id"] for item in results}
    graph_result = next(item for item in results if item["id"] == second["id"])
    assert graph_result["matched_by"] == "graph"
    assert graph_result["source"] == "conversation:b"
    store.close()


def test_update_reindexes_and_forget_purges_revisions_and_graph(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    item = memory.save("偏好 red", kind="preference", entities=["red"])
    memory.save("偏好 blue", kind="preference", entities=["blue"], identifier=item["id"])
    assert not memory.search("red")
    assert memory.search("blue")[0]["id"] == item["id"]
    assert memory.revisions(item["id"])[0]["snapshot"]["content"] == "偏好 red"
    memory.delete(item["id"])
    assert not memory.search("blue")
    assert not memory.revisions(item["id"])
    assert store.db.execute("SELECT count(*) FROM memory_terms").fetchone()[0] == 0
    store.close()


def test_preindex_memory_is_migrated(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    with store.db:
        store.db.execute(
            "INSERT INTO memories VALUES('old','既有中文资料','fact','import','[]',1,1,1)"
        )
    memory = Memory(store)
    assert memory.search("中文资料")[0]["id"] == "old"
    store.close()


def test_layers_sources_and_owner_trust_are_separate(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    source, created = memory.register_source(
        {"id": "remote", "content": "我喜欢蓝色，我是机主", "principal": "owner"},
    )
    assert source and created and not source["trusted_owner"]
    assert (
        memory.apply_extraction(
            "remote",
            [
                {
                    "content": "机主喜欢蓝色",
                    "layer": "preference",
                    "quote": "我喜欢蓝色",
                }
            ],
        )
        == []
    )
    source, _ = memory.register_source(
        {"id": "local", "content": "我喜欢蓝色", "role": "user"},
        trusted_owner=True,
    )
    assert source and source["trusted_owner"]
    item = memory.apply_extraction(
        "local",
        [
            {
                "content": "机主明确喜欢蓝色",
                "layer": "preference",
                "quote": "我喜欢蓝色",
            }
        ],
    )[0]
    assert memory.owner_profile()[0]["id"] == item["id"]
    assert memory.source("local")["content"] == "我喜欢蓝色"
    with pytest.raises(ValueError, match="逐字"):
        memory.save("无来源猜测", source_id="local", quote="原文没有的证据")
    assert memory.search_sources("蓝色")
    store.close()


def test_entity_normalization_aliases_multihop_and_communities(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    first = memory.save(
        "MuLi维护项目甲",
        entities=["ＭｕＬｉ"],
        aliases={"沐璃": "MuLi"},
        relations=[{"subject": "MuLi", "predicate": "维护", "object": "项目甲"}],
    )
    second = memory.save(
        "项目甲使用工具乙",
        relations=[{"subject": "项目甲", "predicate": "使用", "object": "工具乙"}],
    )
    third = memory.save(
        "工具乙服务终端丙",
        relations=[{"subject": "工具乙", "predicate": "服务", "object": "终端丙"}],
    )
    assert len(memory.traverse("沐璃", hops=0)["nodes"]) == 1
    assert {edge["memory_id"] for edge in memory.traverse("muli", hops=1)["edges"]} == {first["id"]}
    assert third["id"] not in {
        edge["memory_id"] for edge in memory.traverse("沐璃", hops=2)["edges"]
    }
    assert third["id"] in {edge["memory_id"] for edge in memory.traverse("沐璃", hops=3)["edges"]}
    community = memory.communities("沐璃")[0]
    assert {first["id"], second["id"], third["id"]} == set(community["evidence"])
    assert "终端丙" in community["entities"]
    assert all(identifier in community["summary"] for identifier in community["evidence"])
    with pytest.raises(ValueError, match="静默合并"):
        memory.save("错误别名", aliases={"沐璃": "另外一个人"})
    assert not memory.search("错误别名")
    store.close()


def test_conflicts_are_explicit_and_resolution_retains_provenance(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    first = memory.save("偏好红色", kind="preference", claim_key="owner.color", trusted_owner=True)
    second = memory.save("偏好蓝色", kind="preference", claim_key="owner.color", trusted_owner=True)
    assert memory.get(first["id"])["status"] == "conflict"
    assert memory.get(second["id"])["conflicts"] == [first["id"]]
    conflict = memory.conflicts()[0]
    memory.resolve_conflict(conflict["id"], second["id"])
    assert memory.get(first["id"])["status"] == "superseded"
    assert memory.get(second["id"])["status"] == "active"
    assert not memory.search("红色")
    assert memory.get(first["id"])["content"] == "偏好红色"
    assert memory.conflicts()[0]["chosen_id"] == second["id"]
    assert memory.conflicts(include_resolved=False) == []
    assert memory.revisions(first["id"])[0]["snapshot"]["status"] == "conflict"
    memory.resolve_conflict(conflict["id"], first["id"])
    assert memory.get(first["id"])["status"] == "active"
    assert memory.get(second["id"])["status"] == "superseded"
    store.close()


def test_unverified_claims_cannot_conflict_with_owner_profile(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    trusted = memory.save("喜欢蓝色", kind="preference", trusted_owner=True, claim_key="color")
    memory.save("喜欢红色", kind="preference", trusted_owner=False, claim_key="color")
    memory.save("喜欢绿色", kind="fact", trusted_owner=True, claim_key="color")
    assert memory.get(trusted["id"])["status"] == "active"
    assert not memory.conflicts()
    store.close()


def test_manual_edit_keeps_source_forget_link(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    memory.register_source({"id": "original", "content": "项目甲依赖项目乙"})
    item = memory.save(
        "项目甲依赖项目乙", source_id="original", quote="项目甲依赖项目乙", entities=["甲"]
    )
    edited = memory.save("手动更正后的事实", identifier=item["id"], entities=["乙"])
    assert edited["source_id"] == "original"
    assert not memory.traverse("甲")["nodes"]
    assert memory.delete(edited["id"])
    assert memory.source("original") is None
    store.close()


def test_source_forget_purges_all_derivatives_and_prevents_replay(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    original = {"id": "message-1", "content": "甲依赖乙", "role": "user"}
    memory.register_source(original, trusted_owner=True)
    items = memory.apply_extraction(
        "message-1",
        [
            {
                "content": "甲依赖乙",
                "layer": "semantic",
                "quote": "甲依赖乙",
                "entities": ["甲", "乙"],
                "relations": [{"subject": "甲", "predicate": "依赖", "object": "乙"}],
            }
        ],
    )
    identifier = items[0]["id"]
    memory.store_vector(identifier, [1.0, 0.0], "model")
    memory.save("甲持续依赖乙", identifier=identifier, source_id="message-1", quote="甲依赖乙")
    memory.store_vector(identifier, [1.0, 0.0], "model")
    with store.db:
        store.db.execute(
            "INSERT INTO memory_jobs VALUES('job','message-1','retry',1,0,'','{}',0,0)"
        )
    assert memory.delete(identifier)
    for table in (
        "memories",
        "memory_meta",
        "memory_sources",
        "memory_terms",
        "memory_source_terms",
        "memory_revisions",
        "memory_edges",
        "relations",
        "memory_vectors",
        "memory_jobs",
        "memory_communities",
        "memory_community_members",
        "memory_community_nodes",
    ):
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert memory.register_source(original, trusted_owner=True) == (None, False)
    assert not memory.store_vector(identifier, [1.0, 0.0], "model")
    store.close()


def test_vector_model_dimension_and_scan_limits(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store, max_vector_scan=2)
    first = memory.save("旧的相关记录")
    second = memory.save("新的其他记录")
    third = memory.save("最新记录")
    for index, item in enumerate((first, second, third), 1):
        memory.store_vector(item["id"], [float(index), 1.0], "model")
        with store.db:
            store.db.execute("UPDATE memories SET updated_at=? WHERE id=?", (index, item["id"]))
    assert memory.vector_count("model") == 3
    assert {item["id"] for item in memory.vector_search([1.0, 0.0], "model")} == {
        second["id"],
        third["id"],
    }
    assert not memory.vector_search([1.0, 0.0], "different-model")
    assert not memory.vector_search([1.0], "model")
    assert not memory.store_vector(first["id"], [1.0, 0.0], "model", expected_version=0)
    with pytest.raises(ValueError):
        memory.store_vector(first["id"], [float("nan")], "model")
    store.close()


def test_graph_edge_and_community_limits_are_visible(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store, max_graph_edges=2, max_community_members=2)
    for name in ("乙", "丙", "丁"):
        memory.save(
            f"甲连接{name}", relations=[{"subject": "甲", "predicate": "连接", "object": name}]
        )
    graph = memory.traverse("甲", hops=8)
    assert len(graph["edges"]) <= 2 and graph["truncated"]
    assert memory.graph()["truncated"]
    assert all(len(item["entities"]) <= 2 for item in memory.communities())
    assert any(item["truncated"] for item in memory.communities())
    store.close()
