from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_attention import attention_inputs, eventually, offline_runtime

from muli.attention import AttentionInbox
from muli.config import Settings
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store import Store
from muli.system_events import attention_source

PROGRESS = "management.progress"
COMPLETED = "management.completed"
PRIVATE = "PRIVATE_ERROR_DO_NOT_EXECUTE_OR_FORWARD"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "attention.sqlite")
    try:
        yield value
    finally:
        value.close()


def routing(kind: str) -> FrozenRouting:
    if kind == "legacy":
        return FrozenRouting()
    if kind == "independent":
        return FrozenRouting.independent(Settings())
    return FrozenRouting.bound(
        ReplyOrigin.load(
            {
                "kind": kind,
                "input_id": "original-update-input",
                **(
                    {
                        "route": {
                            "name": "telegram",
                            "account_id": "original-account",
                            "target": "original-owner",
                            "sender_id": "original-owner",
                            "kind": "private",
                            "reply_to": "original-message",
                        }
                    }
                    if kind == "channel"
                    else {}
                ),
            }
        )
    )


def record(
    store: Store,
    *,
    event_type: str = PROGRESS,
    destination: str = "channel",
    **changes: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = {
        "id": "candidate-plan",
        "action": "switch",
        "operation": "update",
        "status": "applying" if event_type == PROGRESS else "interrupted",
        "phase": "candidate_cleanup_required" if event_type == PROGRESS else "candidate_cleaned",
        "routing": routing(destination).dump(),
        "error": PRIVATE,
        "result": {"token": PRIVATE},
        **changes,
    }
    event = store.event(event_type, plan, objects=[("management", plan)])
    return event, plan


@pytest.mark.parametrize("destination", ["legacy", "web", "channel"])
@pytest.mark.parametrize("event_type", [PROGRESS, COMPLETED], ids=["blocked", "cleaned"])
def test_cleanup_notices_use_only_exact_state_and_original_routing(
    store: Store, destination: str, event_type: str
) -> None:
    event, plan = record(store, event_type=event_type, destination=destination)
    source = attention_source(store, event, ["management"])
    assert source is not None
    assert source.kind == "management" and source.record_id == plan["id"]
    assert source.logical_key == f"management:{plan['id']}:{plan['phase']}"
    assert json.loads(source.detail) == {
        field: plan[field] for field in ("action", "operation", "status", "phase")
    }
    assert source.routing == routing(destination)
    assert PRIVATE not in source.detail and "completed" not in source.detail


@pytest.mark.parametrize("field", ["action", "operation", "status", "phase", "routing"])
@pytest.mark.parametrize("change", ["missing", "different"])
def test_progress_cannot_borrow_missing_or_changed_event_fields(
    store: Store, field: str, change: str
) -> None:
    event, plan = record(store)
    data = dict(plan)
    if change == "missing":
        data.pop(field)
    else:
        data[field] = routing("web").dump() if field == "routing" else "other"
    assert attention_source(store, {**event, "data": data}, ["management"]) is None


def test_missing_routing_on_both_plan_and_event_is_not_candidate_evidence(store: Store) -> None:
    event, plan = record(store)
    plan.pop("routing")
    store.put("management", plan)
    assert attention_source(store, {**event, "data": plan}, ["management"]) is None


@pytest.mark.parametrize(
    ("event_type", "changes"),
    [
        (PROGRESS, {"action": "restart"}),
        (PROGRESS, {"action": "update"}),
        (PROGRESS, {"operation": None}),
        (PROGRESS, {"status": "pending"}),
        (PROGRESS, {"phase": "candidate_preparing"}),
        (PROGRESS, {"status": "interrupted", "phase": "candidate_cleaned"}),
        (COMPLETED, {"action": "configure"}),
        (COMPLETED, {"operation": None}),
        (COMPLETED, {"phase": "ordinary_interrupted"}),
        (COMPLETED, {"status": "applying", "phase": "candidate_cleanup_required"}),
        (COMPLETED, {"status": "cancelled"}),
    ],
    ids=[
        "restart-progress",
        "legacy-update-progress",
        "no-operation",
        "pending",
        "preparing",
        "wrong-progress-event",
        "other-interrupted-action",
        "old-interrupted",
        "other-interrupted-phase",
        "wrong-terminal-event",
        "cancelled",
    ],
)
def test_other_progress_and_historical_interrupted_plans_do_not_notify(
    store: Store, event_type: str, changes: dict[str, Any]
) -> None:
    event, _ = record(store, event_type=event_type, **changes)
    assert attention_source(store, event, ["management"]) is None


@pytest.mark.parametrize("event_type", [PROGRESS, COMPLETED], ids=["blocked", "cleaned"])
def test_cleanup_notices_keep_existing_subscription_and_source_gates(
    store: Store, event_type: str
) -> None:
    event, plan = record(store, event_type=event_type)
    assert attention_source(store, event, []) is None
    assert attention_source(store, {**event, "session_id": "other-session"}, ["management"]) is None
    assert (
        attention_source(store, {**event, "data": {**plan, "id": "missing"}}, ["management"])
        is None
    )
    independent, _ = record(store, event_type=event_type, destination="independent")
    assert attention_source(store, independent, ["management"]) is None


def test_stale_progress_cannot_replace_the_current_cleanup_result(store: Store) -> None:
    old, _ = record(store)
    current, _ = record(store, event_type=COMPLETED)
    assert attention_source(store, old, ["management"]) is None
    assert attention_source(store, current, ["management"]) is not None


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_existing_unrouted_management_terminal_contract_is_preserved(
    store: Store, status: str
) -> None:
    plan = {"id": "legacy-plan", "action": "configure", "status": status, "error": PRIVATE}
    event = store.event(COMPLETED, plan, objects=[("management", plan)])
    source = attention_source(store, event, ["management"])
    assert source is not None
    assert source.logical_key == f"management:legacy-plan:{status}"
    assert json.loads(source.detail) == {"action": "configure", "status": status}
    assert source.routing == FrozenRouting() and PRIVATE not in source.detail


@pytest.mark.parametrize("destination", ["legacy", "channel"])
async def test_real_attention_deduplicates_each_cleanup_phase_and_retains_reply_evidence(
    tmp_path: Path, destination: str
) -> None:
    # A real Runtime/Store/inbox, with model construction forbidden by this fixture.
    runtime = offline_runtime(tmp_path)
    runtime.config.update({"attention": {"events": ["management"]}})
    inbox = AttentionInbox(runtime)
    try:
        _, plan = record(runtime.store, destination=destination)
        await inbox.start()
        await eventually(lambda: len(attention_inputs(runtime)) == 1)
        duplicate = await runtime.bus.emit(PROGRESS, plan)
        await eventually(lambda: inbox.cursor >= duplicate["id"])
        assert len(attention_inputs(runtime)) == 1
        assert len(runtime.store.list("attention_receipt")) == 1

        # Drain only the persisted input; no model, tool or external message is sent.
        main_id = runtime.main_session()["id"]
        assert len(runtime.store.claim_inputs(main_id, "observed-first-notice", limit=1)) == 1
        _, cleaned = record(runtime.store, event_type=COMPLETED, destination=destination)
        await runtime.bus.notify()
        await eventually(lambda: len(attention_inputs(runtime)) == 2)
        repeated = await runtime.bus.emit(COMPLETED, cleaned)
        await eventually(lambda: inbox.cursor >= repeated["id"])

        inputs = attention_inputs(runtime)
        assert len(inputs) == 2 and not runtime.tasks and inbox.last_error is None
        for item, expected in zip(inputs, (plan, cleaned), strict=True):
            assert item["principal"] == item["source"] == "system:attention"
            assert item["mode"] == "ask" and item["route"] is None
            assert FrozenRouting.load(item["attention_routing"]) == routing(destination)
            assert PRIVATE not in item["content"]
            detail = json.loads(item["content"].split("\n")[-1])[0]["detail"]
            assert json.loads(detail)["phase"] == expected["phase"]
            assert json.loads(detail)["status"] == expected["status"]
        expected_receipts = {
            hashlib.sha256(f"management:{plan['id']}:{phase}".encode()).hexdigest()
            for phase in ("candidate_cleanup_required", "candidate_cleaned")
        }
        assert {item["id"] for item in runtime.store.list("attention_receipt")} == expected_receipts
    finally:
        await inbox.close()
        await runtime.close()
