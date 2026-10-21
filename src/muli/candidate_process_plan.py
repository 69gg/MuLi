"""Attach bounded process receipts to the existing candidate management plan."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from muli.candidate_lineage import check_plan_budget
from muli.candidate_policy import CandidateError
from muli.management_projection import _read_object
from muli.platforms.candidate_process_state import PendingProcess
from muli.platforms.installation import _absolute
from muli.store import Store

CANDIDATE_PROCESS_PROTOCOL = 1


def read_process(plan: dict[str, Any], limit: int) -> PendingProcess | None:
    """Only a protocol-marked plan proves absence means no start was issued."""
    check_plan_budget(plan, limit)
    if (
        type(plan.get("candidate_protocol")) is not int
        or plan.get("candidate_protocol") != CANDIDATE_PROCESS_PROTOCOL
        or plan.get("action") != "switch"
        or plan.get("operation") != "update"
    ):
        raise CandidateError("candidate_process_plan_invalid")
    root = plan.get("candidate_attempt")
    identifier = plan.get("id")
    try:
        if (
            not isinstance(root, str)
            or not isinstance(identifier, str)
            or not identifier
            or Path(_absolute(root)).name != "candidate-" + identifier
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise CandidateError("candidate_process_plan_invalid") from None
    if "candidate_process" not in plan:
        return None
    value = PendingProcess.from_json(
        json.dumps(plan["candidate_process"], ensure_ascii=False, allow_nan=False).encode(),
        limit,
    )
    if value.root != plan.get("candidate_attempt") or Path(value.root).name != "candidate-" + str(
        plan["id"]
    ):
        raise CandidateError("candidate_process_plan_invalid")
    return value


def persist_process(
    store: Store,
    plan: dict[str, Any],
    value: PendingProcess,
    *,
    limit: int,
) -> dict[str, Any]:
    """Synchronously commit before start, permission, or reporting cleanup.

    It never observes policy: a policy change or shutdown must still allow the
    already-owned process to publish its cleanup proof. The immutable plan and
    process bindings remain required throughout that cleanup.
    """
    db = store.db
    if db.in_transaction:
        raise CandidateError("candidate_process_outer_transaction")
    if db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL or db.isolation_level is None:
        raise CandidateError("candidate_process_transaction_mode_unsupported")
    previous = read_process(plan, limit)
    if (
        plan.get("status") != "applying"
        or plan.get("phase") not in {"candidate_preparing", "candidate_cleanup_required"}
        or "transaction" in plan
        or _read_object(store.db, "management", plan["id"], limit) != plan
    ):
        raise CandidateError("candidate_plan_changed")
    if previous is None or previous.binding_digest != value.binding_digest:
        if (
            value.phase != "pending"
            or plan["phase"] != "candidate_preparing"
            or (previous is not None and previous.phase != "settled")
        ):
            raise CandidateError("candidate_process_binding_changed")
    elif (
        (previous.phase == "settled" and value != previous)
        or (previous.phase == "observed" and value.phase == "pending")
        or (previous.accepted is not None and previous.accepted != value.accepted)
    ):
        raise CandidateError("candidate_process_binding_changed")
    updated = {**plan, "candidate_process": value.model_dump(mode="json")}
    read_process(updated, limit)
    store.event("management.progress", updated, objects=[("management", updated)])
    return updated
