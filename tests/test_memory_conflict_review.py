from __future__ import annotations

import json
from pathlib import Path

from test_memory_context import fact, records

from muli.memory import Memory
from muli.memory_context import render_context
from muli.store import Store


def test_overlapping_pairs_do_not_repeat_body_or_displace_independent_profile(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "conflict-review.sqlite3")
    try:
        memory = Memory(store)
        independent = fact(memory, "independent", "我偏好简短中文。", "preference")
        candidates = [
            fact(memory, name, text, "preference", claim_key="backup.strategy")
            for name, text in (
                ("strategy-a", "备份方案甲。"),
                ("strategy-b", "OVERLAPREVIEW 备份方案乙。"),
                ("strategy-c", "备份方案丙。"),
            )
        ]
        seed = memory.get(candidates[1]["id"])
        assert seed is not None and len(seed["conflicts"]) == 2
        selected = records(
            render_context(
                "OVERLAPREVIEW",
                [seed],
                memory.owner_profile(4),
                limit=4,
                budget=6000,
                memory=memory,
            )
        )
        body_ids = [
            member["id"]
            for unit in selected
            if unit.get("type") == "conflict"
            for member in unit["memories"]
        ]
        assert len(body_ids) == len(set(body_ids)), "同一证据正文不能跨冲突组合重复占位"
        assert any(unit.get("id") == independent["id"] for unit in selected)
        assert (
            sum(
                len(unit["memories"]) or 1 if unit.get("type") == "conflict" else 1
                for unit in selected
            )
            <= 4
        )
    finally:
        store.close()


def test_shared_pending_is_not_duplicated_when_pair_cannot_fit(tmp_path: Path) -> None:
    store = Store(tmp_path / "pending-review.sqlite3")
    try:
        memory = Memory(store)
        profile = fact(memory, "short-profile", "我喜欢简洁。", "preference")
        fact(
            memory,
            "large-peer",
            "旧备份偏好。",
            "preference",
            claim_key="backup.strategy",
            source_name="X" * 1000,
        )
        created = fact(
            memory,
            "short-seed",
            "SHAREDREVIEW 新备份偏好。",
            "preference",
            claim_key="backup.strategy",
        )
        seed = memory.get(created["id"])
        assert seed is not None
        single = render_context(
            "SHAREDREVIEW", [seed], [seed, profile], limit=1, budget=6000, memory=memory
        )
        prefix, pending = single.splitlines()
        assert json.loads(pending)["availability"] == "pending"
        budget = len(prefix + "\n") + 2 * len(pending) + 1
        selected = records(
            render_context(
                "SHAREDREVIEW", [seed], [seed, profile], limit=2, budget=budget, memory=memory
            ),
            budget=budget,
        )
        notices = [unit for unit in selected if unit.get("type") == "conflict"]
        assert len(notices) == 1 and notices[0]["availability"] == "pending"
        assert notices[0]["seed_id"] == seed["id"]
        assert any(unit.get("id") == profile["id"] for unit in selected)
    finally:
        store.close()


def test_active_status_with_current_unresolved_pair_cannot_emit_single_side(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "status-review.sqlite3")
    try:
        memory = Memory(store)
        fact(memory, "old-status", "旧方案直接覆盖。", "procedural", claim_key="nas.backup")
        created = fact(
            memory,
            "new-status",
            "STATUSREVIEW 先离线校验。",
            "procedural",
            claim_key="nas.backup",
        )
        with store.db:
            store.db.execute(
                "UPDATE memory_meta SET status='active' WHERE memory_id=?", (created["id"],)
            )
        seed = memory.get(created["id"])
        assert seed is not None and seed["status"] == "active" and seed["conflicts"]
        context = render_context("STATUSREVIEW", [seed], [], limit=2, budget=3000, memory=memory)
        selected = records(context, budget=3000)
        assert not any(unit.get("id") == seed["id"] for unit in selected)
        assert seed["content"] not in context
    finally:
        store.close()
