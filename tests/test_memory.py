from __future__ import annotations

from pathlib import Path

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
