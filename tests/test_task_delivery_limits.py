"""Reader corruption boundaries, not registration or completed-task evidence.

The small delivery is an explicit reader fixture in a real temporary Store.
Only its SQL primary key is damaged; no report, history or host proof is made.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from test_task_control import Rig
from test_task_control import rig as rig

from muli.evidence import EvidenceError
from muli.memory import Memory
from muli.task_delivery import delivery_prefix
from muli.task_inspection import TaskInspection


@pytest.mark.parametrize("damage", ["oversized-text", "invalid-utf8"])
def test_delivery_directory_bounds_primary_key_before_python_projection(
    rig: Rig,
    damage: str,
) -> None:
    inspection = TaskInspection(rig.ledger, rig.control, Memory(rig.store))
    task = rig.ledger.get(rig.identifier)
    prefix = delivery_prefix(rig.identifier)
    identifier = prefix + "a" * 32
    record: dict[str, Any] = {
        "id": identifier,
        "version": 1,
        "commitment_id": rig.identifier,
        "task_revision": task["revision"],
        "epoch": 0,
        "agent_id": "reader-fixture-agent",
        "run_id": "reader-fixture-run",
        "generation": 1,
        "completion_revision": 1,
        "brief": task["brief"],
        "host_facts": {},
        "model_judgments": {},
        "recorded_by": {},
        "created_at": 1.0,
    }
    rig.store.put("task_delivery", record)
    baseline = inspection.read(rig.identifier, section="deliveries", limit=1)
    assert [item["id"] for item in baseline["items"]] == [identifier]
    assert baseline["items"][0]["current"] is False
    assert baseline["has_more"] is False
    budget = rig.ledger.max_output_chars() * 4
    db = rig.store.db
    if damage == "oversized-text":
        large_id = prefix + "x" * (1024 * 1024)
        assert len(large_id) > budget
        db.execute(
            "UPDATE objects SET id=? WHERE kind='task_delivery' AND id=?",
            (large_id, identifier),
        )
    else:
        db.execute(
            "UPDATE objects SET id=CAST(? AS TEXT) WHERE kind='task_delivery' AND id=?",
            (prefix.encode("ascii") + b"\x80", identifier),
        )
    db.commit()
    before = [
        tuple(row)
        for row in db.execute(
            "SELECT CAST(id AS BLOB),CAST(data AS BLOB),updated "
            "FROM objects WHERE kind='task_delivery' ORDER BY id"
        )
    ]
    assert len(before) == 1
    changes = db.total_changes
    original_factory = db.row_factory
    crossed_lengths: list[int] = []

    def observe(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> sqlite3.Row:
        # Preserve the actual production query/result; only observe decoding.
        crossed_lengths.extend(
            len(value) for value in row if isinstance(value, str) and len(value) > budget
        )
        return sqlite3.Row(cursor, row)

    error_code: str | None = None
    try:
        db.row_factory = observe
        try:
            inspection.read(rig.identifier, section="deliveries", limit=1)
        except EvidenceError as error:
            error_code = error.code
    finally:
        db.row_factory = original_factory
    assert crossed_lengths == [], "directory PK crossed the existing SQL-to-Python byte budget"
    assert error_code == "content_unavailable"
    assert db.total_changes == changes
    assert [
        tuple(row)
        for row in db.execute(
            "SELECT CAST(id AS BLOB),CAST(data AS BLOB),updated "
            "FROM objects WHERE kind='task_delivery' ORDER BY id"
        )
    ] == before


def test_delivery_directory_work_does_not_grow_with_unrelated_tasks(rig: Rig) -> None:
    """Count VM work on the same empty task directory, not elapsed performance."""
    inspection = TaskInspection(rig.ledger, rig.control, Memory(rig.store))
    task = rig.ledger.get(rig.identifier)
    db = rig.store.db

    def reader_record(identifier: str, task_id: str) -> dict[str, Any]:
        return {
            "id": identifier,
            "version": 1,
            "commitment_id": task_id,
            "task_revision": task["revision"],
            "epoch": 0,
            "agent_id": "reader-fixture-agent",
            "run_id": "reader-fixture-run",
            "generation": 1,
            "completion_revision": 1,
            "brief": task["brief"],
            "host_facts": {},
            "model_judgments": {},
            "recorded_by": {},
            "created_at": 1.0,
        }

    def measured_empty_directory() -> tuple[dict[str, Any], int]:
        steps = 0

        def count() -> int:
            nonlocal steps
            steps += 1
            return 0

        # This isolated fixture connection has no other progress owner. Count
        # every VM step without interrupting or changing any production query.
        db.set_progress_handler(count, 1)
        try:
            result = inspection.read(rig.identifier, section="deliveries", limit=1)
        finally:
            db.set_progress_handler(None, 0)
        assert result["items"] == [] and result["has_more"] is False
        return result, steps

    baseline, baseline_steps = measured_empty_directory()
    assert baseline_steps > 0
    for index in range(64):
        other_task_id = f"unrelated-task-{index}"
        rig.store.put(
            "task_delivery",
            reader_record(delivery_prefix(other_task_id) + f"{index:032x}", other_task_id),
        )
    changes = db.total_changes
    after, after_steps = measured_empty_directory()
    assert after == baseline
    assert db.total_changes == changes
    # Allow generous B-tree/point-read variation without allowing all unrelated
    # delivery JSON to become the current task's per-query work.
    assert after_steps <= baseline_steps * 2 + 64, (baseline_steps, after_steps)
    own_id = delivery_prefix(rig.identifier) + "f" * 32
    rig.store.put("task_delivery", reader_record(own_id, rig.identifier))
    own = inspection.read(rig.identifier, section="deliveries", limit=1)
    assert [item["id"] for item in own["items"]] == [own_id]
    assert own["has_more"] is False
