from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, Never

import pytest

from muli.candidate_policy import CandidateError, CandidateLimits
from muli.candidate_process_plan import persist_process, read_process
from muli.platforms.candidate_process_state import AcceptedUnit, PendingProcess
from muli.platforms.installation import FileFingerprint, FileIdentity
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store import Store

LIMIT = 32_768
BOOT = "b" * 32


@pytest.fixture(autouse=True)
def no_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """These receipts describe fixtures; no service or candidate may execute."""

    def forbidden(*args: object, **kwargs: object) -> Never:
        pytest.fail("candidate process plan tests must not start a subprocess")

    async def forbidden_async(*args: object, **kwargs: object) -> Never:
        forbidden(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
    monkeypatch.setattr("muli.process.run_process", forbidden_async)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "muli.sqlite3", durable=True)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def plan(store: Store, tmp_path: Path) -> dict[str, Any]:
    identifier = "a" * 32
    root = tmp_path / f"candidate-{identifier}"
    root.mkdir(mode=0o700)
    item = {
        "id": identifier,
        "action": "switch",
        "operation": "update",
        "status": "applying",
        "phase": "candidate_preparing",
        "candidate_protocol": 1,
        "candidate_attempt": str(root),
        "routing": FrozenRouting.bound(
            ReplyOrigin.load({"kind": "web", "input_id": "original-owner-input"})
        ).dump(),
        "expected_policy": {"revision": "c" * 64},
    }
    store.put("management", item)
    return item


def fingerprint(path: Path, data: bytes) -> FileFingerprint:
    if not path.exists():
        path.write_bytes(data)
    assert path.read_bytes() == data
    return FileFingerprint(
        path=str(path),
        identity=FileIdentity.of(path.stat()),
        digest=hashlib.sha256(data).hexdigest(),
    )


def pending_for(plan: dict[str, Any], step: int = 0) -> PendingProcess:
    root = Path(plan["candidate_attempt"])
    info = root.stat()
    return PendingProcess(
        unit=f"muli-candidate-{step:032x}.service",
        boot=BOOT,
        root=str(root),
        device=info.st_dev,
        inode=info.st_ino,
        uid=info.st_uid,
        python=str(Path(sys.executable).absolute()),
        worker_file=fingerprint(root / "_candidate_worker.py", b"# never executed\n"),
        control_file=fingerprint(root / f"process-{step:032x}.json", b"{}"),
        deadline=1234.0,
        runtime_us=4_000_000,
        limits=CandidateLimits(seconds=5.0, preflight_seconds=2.0),
    )


def at_phase(
    pending: PendingProcess, phase: Literal["pending", "observed", "settled"]
) -> PendingProcess:
    accepted = (
        None
        if phase == "pending"
        else AcceptedUnit(
            invocation="1" * 32,
            cgroup="/app.slice/" + pending.unit,
            pid=321,
            start_ticks=456,
        )
    )
    return PendingProcess.model_validate(
        {**pending.model_dump(), "phase": phase, "accepted": accepted}
    )


def save(store: Store, plan: dict[str, Any], value: PendingProcess) -> dict[str, Any]:
    updated: dict[str, Any] = persist_process(store, plan, value, limit=LIMIT)
    assert not store.db.in_transaction
    assert read_process(updated, LIMIT) == value
    assert store.get("management", plan["id"]) == updated
    event = store.events()[-1]
    assert event["type"] == "management.progress"
    assert event["session_id"] is None
    assert event["data"] == updated
    return updated


def test_only_valid_marked_absence_means_no_start(plan: dict[str, Any], store: Store) -> None:
    before = store.db.total_changes
    assert read_process(plan, LIMIT) is None
    assert store.db.total_changes == before
    assert store.event_position() == 0


@pytest.mark.parametrize("version", [None, True, 1.0, 0, 2, "1"])
def test_old_or_unknown_protocol_is_not_an_unexecuted_receipt(
    plan: dict[str, Any], version: object
) -> None:
    item = {**plan, "candidate_protocol": version}
    if version is None:
        item.pop("candidate_protocol")
    with pytest.raises(CandidateError, match="^candidate_process_plan_invalid$"):
        read_process(item, LIMIT)


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "restart"},
        {"operation": "configure"},
        {"candidate_attempt": None},
        {"candidate_attempt": "relative/candidate-" + "a" * 32},
        {"id": None},
        {"id": ""},
        {"id": "another-plan"},
    ],
)
def test_invalid_plan_cannot_claim_absence(
    plan: dict[str, Any], changes: dict[str, object]
) -> None:
    with pytest.raises(CandidateError, match="^candidate_process_plan_invalid$"):
        read_process({**plan, **changes}, LIMIT)


@pytest.mark.parametrize("field", ["id", "candidate_attempt"])
def test_missing_plan_binding_cannot_claim_absence(plan: dict[str, Any], field: str) -> None:
    item = {**plan}
    item.pop(field)
    with pytest.raises(CandidateError, match="^candidate_process_plan_invalid$"):
        read_process(item, LIMIT)


@pytest.mark.parametrize("suffix", ["/", "/../candidate-" + "a" * 32, "\n"])
def test_noncanonical_attempt_cannot_claim_absence(plan: dict[str, Any], suffix: str) -> None:
    item = {**plan, "candidate_attempt": plan["candidate_attempt"] + suffix}
    with pytest.raises(CandidateError, match="^candidate_process_plan_invalid$"):
        read_process(item, LIMIT)


def test_four_operations_replace_only_a_settled_binding(store: Store, plan: dict[str, Any]) -> None:
    original_routing = plan["routing"]
    original_policy = plan["expected_policy"]
    units: list[str] = []
    phases: tuple[Literal["pending", "observed", "settled"], ...] = (
        "pending",
        "observed",
        "settled",
    )
    for step in range(4):
        pending = pending_for(plan, step)
        units.append(pending.unit)
        for phase in phases:
            plan = save(store, plan, at_phase(pending, phase))
            assert plan["routing"] == original_routing
            assert plan["expected_policy"] == original_policy
            assert plan["phase"] == "candidate_preparing"
            assert "transaction" not in plan
    events = store.events()
    assert len(events) == 12
    assert [event["data"]["candidate_process"]["phase"] for event in events] == [
        "pending",
        "observed",
        "settled",
    ] * 4
    assert [event["data"]["candidate_process"]["unit"] for event in events[::3]] == units
    assert {event["data"]["candidate_process"]["deadline"] for event in events} == {1234.0}


@pytest.mark.parametrize("phase", ["pending", "observed"])
def test_unsettled_unit_cannot_be_replaced(
    store: Store, plan: dict[str, Any], phase: Literal["pending", "observed"]
) -> None:
    pending = pending_for(plan)
    plan = save(store, plan, pending)
    if phase == "observed":
        plan = save(store, plan, at_phase(pending, phase))
    position = store.event_position()
    with pytest.raises(CandidateError, match="^candidate_process_binding_changed$"):
        persist_process(store, plan, pending_for(plan, 1), limit=LIMIT)
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == position


def test_observed_cannot_return_to_pending(store: Store, plan: dict[str, Any]) -> None:
    pending = pending_for(plan)
    plan = save(store, plan, pending)
    plan = save(store, plan, at_phase(pending, "observed"))
    position = store.event_position()
    with pytest.raises(CandidateError, match="^candidate_process_binding_changed$"):
        persist_process(store, plan, pending, limit=LIMIT)
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == position


@pytest.mark.parametrize(
    "changes",
    [
        {"invocation": "2" * 32},
        {"cgroup": "/other.slice/muli-candidate-" + "0" * 32 + ".service"},
        {"pid": 322},
        {"start_ticks": 457},
        {"start_ticks": None},
        None,
    ],
)
def test_accepted_identity_cannot_change_when_settling(
    store: Store, plan: dict[str, Any], changes: dict[str, object] | None
) -> None:
    pending = pending_for(plan)
    plan = save(store, plan, pending)
    observed = at_phase(pending, "observed")
    plan = save(store, plan, observed)
    assert observed.accepted is not None
    changed = PendingProcess.model_validate(
        {
            **observed.model_dump(),
            "phase": "settled",
            "accepted": (
                None if changes is None else {**observed.accepted.model_dump(), **changes}
            ),
        }
    )
    position = store.event_position()
    with pytest.raises(CandidateError, match="^candidate_process_binding_changed$"):
        persist_process(store, plan, changed, limit=LIMIT)
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == position


def test_same_boot_settled_replay_keeps_the_receipt_after_control_removal(
    store: Store, plan: dict[str, Any]
) -> None:
    pending = pending_for(plan)
    plan = save(store, plan, pending)
    plan = save(store, plan, at_phase(pending, "observed"))
    settled = at_phase(pending, "settled")
    plan = save(store, plan, settled)
    Path(settled.control_file.path).unlink()
    updated = save(store, plan, settled)
    assert updated == plan
    assert read_process(updated, LIMIT) == settled
    assert {event["data"]["candidate_process"]["unit"] for event in store.events()} == {
        settled.unit
    }


@pytest.mark.parametrize("phase", ["pending", "observed"])
def test_settled_binding_cannot_be_reopened(
    store: Store, plan: dict[str, Any], phase: Literal["pending", "observed"]
) -> None:
    pending = pending_for(plan)
    plan = save(store, plan, pending)
    plan = save(store, plan, at_phase(pending, "observed"))
    plan = save(store, plan, at_phase(pending, "settled"))
    position = store.event_position()
    with pytest.raises(CandidateError, match="^candidate_process_binding_changed$"):
        persist_process(store, plan, at_phase(pending, phase), limit=LIMIT)
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == position


def test_cleanup_can_settle_without_reopening_preparation(
    store: Store, plan: dict[str, Any]
) -> None:
    pending = pending_for(plan)
    plan = save(store, plan, pending)
    plan = save(store, plan, at_phase(pending, "observed"))
    plan = {**plan, "phase": "candidate_cleanup_required"}
    store.put("management", plan)
    plan = save(store, plan, at_phase(pending, "settled"))
    with pytest.raises(CandidateError, match="^candidate_process_binding_changed$"):
        persist_process(store, plan, pending_for(plan, 1), limit=LIMIT)
    assert store.get("management", plan["id"]) == plan


def test_pending_absence_cleanup_can_be_settled_without_an_accepted_unit(
    store: Store, plan: dict[str, Any]
) -> None:
    pending = pending_for(plan)
    plan = save(store, plan, pending)
    settled = PendingProcess.model_validate({**pending.model_dump(), "phase": "settled"})
    updated = save(store, plan, settled)
    assert updated["candidate_process"]["accepted"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "cancelled"},
        {"phase": "candidate_ready"},
        {"transaction": {"generation": "published"}},
        {"transaction": None},
    ],
)
def test_published_or_inactive_plan_cannot_receive_an_attachment(
    store: Store, plan: dict[str, Any], changes: dict[str, object]
) -> None:
    changed = {**plan, **changes}
    store.put("management", changed)
    with pytest.raises(CandidateError, match="^candidate_plan_changed$"):
        persist_process(store, changed, pending_for(plan), limit=LIMIT)
    assert store.get("management", plan["id"]) == changed
    assert store.event_position() == 0


def test_stale_plan_does_not_overwrite_a_concurrent_change(
    store: Store, plan: dict[str, Any]
) -> None:
    changed = {**plan, "recovery_notice": "new-owner-state"}
    store.put("management", changed)
    with pytest.raises(CandidateError, match="^candidate_plan_changed$"):
        persist_process(store, plan, pending_for(plan), limit=LIMIT)
    assert store.get("management", plan["id"]) == changed
    assert store.event_position() == 0


def test_attempt_from_another_root_cannot_be_attached(
    store: Store, plan: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "different-retained-root" / f"candidate-{plan['id']}"
    root.mkdir(parents=True, mode=0o700)
    foreign = pending_for({**plan, "candidate_attempt": str(root)})
    with pytest.raises(CandidateError, match="^candidate_process_plan_invalid$"):
        persist_process(store, plan, foreign, limit=LIMIT)
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == 0


@pytest.mark.parametrize("attachment", [None, [], {}, {"version": 2}])
def test_bad_attachment_is_not_treated_as_absent(plan: dict[str, Any], attachment: object) -> None:
    with pytest.raises(CandidateError, match="^candidate_process_state_invalid$"):
        read_process({**plan, "candidate_process": attachment}, LIMIT)


@pytest.mark.parametrize("tail", ["x" * LIMIT, "中" * (LIMIT // 3), '"' * (LIMIT // 2)])
def test_plan_budget_counts_utf8_escaping_and_final_headroom(
    store: Store, plan: dict[str, Any], tail: str
) -> None:
    oversized = {**plan, "notes": tail}
    with pytest.raises(CandidateError, match="^candidate_plan_budget$"):
        read_process(oversized, LIMIT)
    with pytest.raises(CandidateError, match="^candidate_plan_budget$"):
        persist_process(store, oversized, pending_for(plan), limit=LIMIT)
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == 0


def test_attachment_must_also_fit_before_any_plan_or_event_write(
    store: Store, plan: dict[str, Any]
) -> None:
    pending = pending_for(plan)
    notes = "x" * (LIMIT - 4096 - len(json.dumps(plan, ensure_ascii=False).encode()) - 32)
    crowded = {**plan, "notes": notes}
    assert read_process(crowded, LIMIT) is None
    store.put("management", crowded)
    with pytest.raises(CandidateError, match="^candidate_plan_budget$"):
        persist_process(store, crowded, pending, limit=LIMIT)
    assert store.get("management", plan["id"]) == crowded
    assert store.event_position() == 0


def test_persistence_reads_the_exact_bounded_plan_without_store_get(
    store: Store, plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_get(*args: object, **kwargs: object) -> Never:
        pytest.fail("attachment persistence must not read an unbounded object")

    monkeypatch.setattr(store, "get", forbidden_get)
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    try:
        updated = persist_process(store, plan, pending_for(plan), limit=LIMIT)
    finally:
        store.db.set_trace_callback(None)
    reads = [statement for statement in statements if statement.startswith("SELECT")]
    assert len(reads) == 1
    assert "substr(CAST(data AS BLOB),1,32769)" in reads[0]
    assert f"kind='management' AND id='{plan['id']}'" in reads[0]
    assert store.events()[-1]["data"] == updated


def test_failed_progress_event_rolls_back_the_attachment(
    store: Store, plan: dict[str, Any]
) -> None:
    store.db.execute(
        "CREATE TRIGGER reject_candidate_progress BEFORE INSERT ON events "
        "WHEN NEW.type='management.progress' "
        "BEGIN SELECT RAISE(ABORT,'fixture injected event failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="fixture injected event failure"):
        persist_process(store, plan, pending_for(plan), limit=LIMIT)
    assert not store.db.in_transaction
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == 0


def test_outer_transaction_is_neither_committed_nor_rolled_back(
    store: Store, plan: dict[str, Any]
) -> None:
    store._put("unrelated", {"id": "caller-owned", "value": "not committed"})
    assert store.db.in_transaction
    with pytest.raises(CandidateError, match="^candidate_process_outer_transaction$"):
        persist_process(store, plan, pending_for(plan), limit=LIMIT)
    assert store.db.in_transaction
    assert store.get("unrelated", "caller-owned") is not None
    assert store.get("management", plan["id"]) == plan
    assert store.event_position() == 0
    store.db.rollback()
    assert store.get("unrelated", "caller-owned") is None
    assert store.get("management", plan["id"]) == plan


@pytest.mark.parametrize("mode", ["autocommit", "isolation_none"])
def test_unsupported_connection_mode_cannot_split_plan_and_event(
    store: Store, plan: dict[str, Any], mode: str
) -> None:
    if mode == "autocommit":
        store.db.autocommit = True
    else:
        store.db.isolation_level = None
    try:
        assert not store.db.in_transaction
        with pytest.raises(
            CandidateError, match="^candidate_process_transaction_mode_unsupported$"
        ):
            persist_process(store, plan, pending_for(plan), limit=LIMIT)
        assert not store.db.in_transaction
        assert store.get("management", plan["id"]) == plan
        assert store.event_position() == 0
    finally:
        store.db.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
        store.db.isolation_level = "DEFERRED"
