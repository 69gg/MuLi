from __future__ import annotations

import json
import sqlite3
from typing import Any, NoReturn

import pytest
from test_memory_context import fact, records
from test_memory_context import memory as memory

from muli.memory import Memory
from muli.memory_context import render_context


def conflict_seed(memory: Memory) -> tuple[dict[str, Any], dict[str, Any]]:
    peer = fact(memory, "old-source", "旧说法：直接覆盖。", "procedural", claim_key="backup")
    seed = fact(memory, "new-source", "QUERY 新说法：先校验。", "procedural", claim_key="backup")
    return seed, peer


def test_composition_is_bounded_read_only_and_reuses_shared_seed(
    memory: Memory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, peer = conflict_seed(memory)
    profile = fact(memory, "profile", "我喜欢简洁中文。", "preference")
    statements: list[str] = []
    projected: list[tuple[Any, ...]] = []

    def row_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> sqlite3.Row:
        projected.append(row)
        return sqlite3.Row(cursor, row)

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("组合不可展开旧记忆、全冲突、图或来源正文")

    for name in ("get", "source", "conflicts", "search", "traverse"):
        monkeypatch.setattr(memory, name, forbidden)
    db = memory.store.db
    writes = db.total_changes
    db.execute("BEGIN")
    factory = db.row_factory
    db.row_factory = row_factory
    db.set_trace_callback(statements.append)
    try:
        rendered = render_context(
            "QUERY", [seed, profile], [profile, seed], limit=3, budget=3000, memory=memory
        )
    finally:
        db.row_factory = factory
        db.set_trace_callback(None)
    assert db.in_transaction and db.total_changes == writes
    db.rollback()
    groups = records(rendered)
    assert any(
        {item["id"] for item in group.get("memories", [])} == {seed["id"], peer["id"]}
        for group in groups
    )
    compositions = [sql for sql in statements if sql.startswith("WITH requested(slot,id)")]
    assert len(compositions) == 2, "跨流重复seed只能各查一次"
    assert projected and all(
        len(value) <= 3000 * 4 for row in projected for value in row if isinstance(value, bytes)
    )
    plan = db.execute("EXPLAIN QUERY PLAN " + compositions[0]).fetchall()
    assert any(
        "memory_conflicts" in row[3] and "left_id=? AND right_id=?" in row[3] for row in plan
    )
    assert not any("SCAN memory_conflicts" in row[3] for row in plan)
    assert not any(" OR c.right_id=" in sql for sql in compositions)


def test_forgotten_seed_from_cache_cannot_leave_a_pending_locator(memory: Memory) -> None:
    seed, peer = conflict_seed(memory)
    assert memory.forget_source(seed["source_id"])
    rendered = render_context("QUERY", [seed], [], limit=1, budget=3000, memory=memory)
    assert rendered == ""
    assert seed["id"] not in rendered and seed["source_id"] not in rendered
    remaining = memory.get(peer["id"])
    assert remaining is not None and remaining["status"] == "active"


@pytest.mark.parametrize("damage", ["source", "trust", "layer", "metadata", "encoding", "blob"])
def test_invalid_peer_never_becomes_single_sided_fact(memory: Memory, damage: str) -> None:
    seed, peer = conflict_seed(memory)
    with memory.store.db:
        if damage == "source":
            memory.store.db.execute(
                "UPDATE memory_sources SET trusted_owner=0 WHERE id=?", (peer["source_id"],)
            )
        elif damage in {"trust", "layer"}:
            field, value = ("trusted_owner", 0) if damage == "trust" else ("layer", "semantic")
            memory.store.db.execute(
                f"UPDATE memory_meta SET {field}=? WHERE memory_id=?", (value, peer["id"])
            )
        elif damage == "metadata":
            memory.store.db.execute(
                "UPDATE memories SET source=? WHERE id=?", ("X" * 20000, peer["id"])
            )
        elif damage == "encoding":
            memory.store.db.execute(
                "UPDATE memories SET content=CAST(? AS TEXT) WHERE id=?", (b"\x80", peer["id"])
            )
        else:
            memory.store.db.execute(
                "UPDATE memory_meta SET claim_key=? WHERE memory_id=?", (b"backup", peer["id"])
            )
    rendered = render_context("QUERY", [seed], [], limit=2, budget=3000, memory=memory)
    selected = records(rendered)
    assert len(selected) == 1 and selected[0]["availability"] == "pending"
    assert selected[0]["memories"] == [] and "content" not in selected[0]["seed"]
    assert seed["content"] not in rendered and peer["content"] not in rendered


def test_source_less_legacy_memories_keep_existing_trust_without_upgrade(memory: Memory) -> None:
    peer = memory.save("旧手工事实", kind="procedure", claim_key="legacy", trusted_owner=False)
    seed = memory.save(
        "QUERY 新手工事实", kind="procedure", claim_key="legacy", trusted_owner=False
    )
    selected = records(render_context("QUERY", [seed], [], limit=2, budget=3000, memory=memory))
    assert selected[0]["availability"] == "paired"
    assert {item["id"] for item in selected[0]["memories"]} == {seed["id"], peer["id"]}
    assert all(
        item["source_id"] is None and item["trusted_owner"] is False
        for item in selected[0]["memories"]
    )


def test_failed_seed_consumes_stream_budget_without_scanning_replacements(memory: Memory) -> None:
    seed, _ = conflict_seed(memory)
    replacement = fact(memory, "replacement", "QUERY 无冲突流程", "procedural")
    assert memory.forget_source(seed["source_id"])
    rendered = render_context("QUERY", [seed, replacement], [], limit=1, budget=3000, memory=memory)
    assert rendered == ""


def test_two_streams_share_max_candidate_seed_budget(memory: Memory) -> None:
    memory.configure(
        graph_hops=2,
        max_graph_edges=memory.max_graph_edges,
        max_candidates=2,
        max_vector_scan=memory.max_vector_scan,
        max_community_members=memory.max_community_members,
    )
    seeds = [
        fact(memory, f"bounded-{index}", f"独立事实{index}", "procedural") for index in range(6)
    ]
    assert memory.forget_source(seeds[0]["source_id"])
    statements: list[str] = []
    memory.store.db.set_trace_callback(statements.append)
    try:
        selected = records(
            render_context("QUERY", seeds[:3], seeds[3:], limit=3, budget=3000, memory=memory)
        )
    finally:
        memory.store.db.set_trace_callback(None)
    assert len([sql for sql in statements if sql.startswith("WITH requested(slot,id)")]) == 4
    assert not {seeds[0]["id"], seeds[2]["id"], seeds[5]["id"]} & {item["id"] for item in selected}


@pytest.mark.parametrize(
    "changes",
    [{"version": True}, {"conflicts": [b"bad"]}, {"id": "\ud800"}],
)
def test_invalid_seed_shape_fails_closed_without_query(
    memory: Memory, changes: dict[str, Any]
) -> None:
    seed, _ = conflict_seed(memory)
    statements: list[str] = []
    memory.store.db.set_trace_callback(statements.append)
    try:
        assert memory.prompt_evidence({**seed, **changes}, max_bytes=3000) == (None, None)
    finally:
        memory.store.db.set_trace_callback(None)
    assert statements == []


def test_seed_and_peer_bodies_are_sql_byte_windows(memory: Memory) -> None:
    seed, peer = conflict_seed(memory)
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memories SET content=? WHERE id=?", ("🙂" * 10000, peer["id"])
        )
    current, partner = memory.prompt_evidence(seed, max_bytes=1001)
    assert current is not None and partner is not None
    assert partner["content"] == "🙂" * 250 + "[节选]"
    assert partner["conflicts_partial"] is True
    assert current["updated_at"] == seed["updated_at"]


def test_character_budget_downgrades_whole_pair_without_empty_body_pages(memory: Memory) -> None:
    seed, _ = conflict_seed(memory)
    full = render_context("QUERY", [seed], [], limit=2, budget=3000, memory=memory)
    group = records(full)[0]
    assert group["availability"] == "paired"
    prefix = full.splitlines()[0] + "\n"
    pending = {**group, "availability": "pending", "memories": []}
    budget = len(prefix) + len(json.dumps(pending, ensure_ascii=False))
    rendered = render_context("QUERY", [seed], [], limit=2, budget=budget, memory=memory)
    assert records(rendered, budget=budget)[0]["availability"] == "pending"
    assert seed["content"] not in rendered
    assert render_context("QUERY", [seed], [], limit=2, budget=len(prefix) + 1, memory=memory) == ""


def test_resolving_one_pair_does_not_claim_three_way_conflict_is_gone(memory: Memory) -> None:
    first, _ = conflict_seed(memory)
    third = fact(memory, "third-source", "第三个不同方案", "procedural", claim_key="backup")
    conflicts = memory.conflicts(first["id"], include_resolved=False)
    choice = next(
        item for item in conflicts if third["id"] not in {item["left_id"], item["right_id"]}
    )
    memory.resolve_conflict(choice["id"], first["id"])
    current = memory.get(first["id"])
    assert current is not None and current["status"] == "conflict"
    rendered = render_context("QUERY", [current], [], limit=2, budget=3000, memory=memory)
    group = records(rendered)[0]
    assert group["availability"] == "paired" and group["partial"] is True
    assert {item["id"] for item in group["memories"]} == {first["id"], third["id"]}


def test_untrusted_preference_never_becomes_profile_via_a_pair(memory: Memory) -> None:
    memory.save("未验证旧偏好", kind="preference", claim_key="untrusted", trusted_owner=False)
    seed = memory.save(
        "QUERY未验证新偏好", kind="preference", claim_key="untrusted", trusted_owner=False
    )
    assert render_context("QUERY", [seed], [seed], limit=2, budget=3000, memory=memory) == ""
