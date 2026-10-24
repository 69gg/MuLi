"""Exact task locators and revocable proofs for task-bound reminder occurrences."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, TypeGuard

from muli.evidence import EvidenceError, validate_integer
from muli.proactive import FrozenRouting
from muli.store import Store


def task_status(store: Store, identifier: str, max_output_chars: int) -> str:
    """Read only bounded identity/status metadata at the exact task primary key."""
    from muli.commitments import STATES

    if not isinstance(identifier, str) or not identifier:
        raise EvidenceError("invalid_arguments", "复盘任务 ID 必须是非空字符串")
    budget = max_output_chars * 4
    try:
        if len(identifier.encode("utf-8", errors="strict")) > budget:
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError("invalid_arguments", "复盘任务 ID 超出核查预算或编码无效") from exc
    row = store.db.execute(
        "SELECT CASE WHEN length(metadata)<=? THEN metadata END FROM ("
        "SELECT CASE WHEN typeof(data)='text' AND json_valid(data) THEN CASE WHEN "
        "json_type(data,'$.id')='text' AND json_type(data,'$.status')='text' "
        "AND length(CAST(json_extract(data,'$.id') AS BLOB))<=? "
        "AND length(CAST(json_extract(data,'$.status') AS BLOB))<=? THEN "
        "CAST(json_object('id',json_extract(data,'$.id'),"
        "'status',json_extract(data,'$.status')) AS BLOB) END END AS metadata "
        "FROM objects WHERE kind='commitment' AND id=?)",
        (budget, budget, budget, identifier),
    ).fetchone()
    if row is None:
        raise EvidenceError("evidence_not_found", "复盘关联的任务不存在")
    try:
        raw = row[0]
        if not isinstance(raw, bytes):
            raise ValueError
        metadata = json.loads(raw.decode("utf-8", errors="strict"))
        if metadata.get("id") != identifier or metadata.get("status") not in STATES:
            raise ValueError
        return str(metadata["status"])
    except (ValueError, TypeError, UnicodeError) as exc:
        raise EvidenceError("content_unavailable", "复盘任务的身份或状态无法完整核查") from exc


def _binding(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "commitment_id", "revision"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or not isinstance(value.get("commitment_id"), str)
        or not value["commitment_id"]
    ):
        raise EvidenceError("content_unavailable", "复盘提醒绑定无法核查")
    validate_integer(value["revision"], "followup.revision", minimum=1)
    return dict(value)


def followup_binding(item: dict[str, Any]) -> dict[str, Any] | None:
    """An unbound reminder retains its host counter, preventing binding ABA."""
    if "followup_revision" not in item and "followup" not in item:
        return None
    revision = item.get("followup_revision")
    if type(revision) is not int:
        raise EvidenceError("content_unavailable", "复盘提醒宿主版本无法核查")
    validate_integer(revision, "followup_revision", minimum=1)
    if "followup" not in item:
        return None
    binding = _binding(item["followup"])
    if binding["revision"] != revision:
        raise EvidenceError("content_unavailable", "复盘提醒绑定版本不一致")
    return binding


@dataclass(frozen=True, slots=True)
class ReminderFollowupProof:
    notice_id: str
    reminder_id: str
    commitment_id: str
    revision: int
    occurrence: int
    due_at: float
    created_at: float
    routing: FrozenRouting

    def binding(self) -> dict[str, Any]:
        return {"version": 1, "commitment_id": self.commitment_id, "revision": self.revision}


def _timestamp(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def read_followup_notice(
    store: Store, identifier: str, max_output_chars: int
) -> ReminderFollowupProof | None:
    """Recheck the same occurrence without requiring enabled or unchanged next due."""
    try:
        notice = store.bounded_object("notification", identifier, max_bytes=max_output_chars * 4)
        if notice is None or notice.get("source") != "reminder":
            return None
        reminder_id = notice.get("reminder_id")
        if not isinstance(reminder_id, str) or not reminder_id:
            return None
        reminder = store.bounded_object("reminder", reminder_id, max_bytes=max_output_chars * 4)
        if reminder is None:
            return None
        binding = _binding(notice.get("followup"))
        if followup_binding(reminder) != binding:
            return None
        occurrence = notice.get("followup_occurrence")
        if not isinstance(occurrence, dict) or set(occurrence) != {"number", "due_at"}:
            return None
        number, due_at, created_at = (
            occurrence["number"],
            occurrence["due_at"],
            notice.get("created_at"),
        )
        validate_integer(number, "followup_occurrence.number", minimum=1)
        fired, last_fired = reminder.get("fired_count"), reminder.get("last_fired_at")
        if (
            type(fired) is not int
            or fired < number
            or not _timestamp(due_at)
            or not _timestamp(created_at)
            or created_at < due_at
            or not _timestamp(last_fired)
            or last_fired < created_at
            or identifier != f"reminder:{reminder_id}:{due_at}"
        ):
            return None
        routing = FrozenRouting.load(notice.get("routing"))
        if (
            routing.classification == "independent"
            or notice.get("routing") != routing.dump()
            or reminder.get("routing") != routing.dump()
            or task_status(store, binding["commitment_id"], max_output_chars)
            in {"done", "cancelled"}
        ):
            return None
        return ReminderFollowupProof(
            identifier,
            reminder_id,
            binding["commitment_id"],
            binding["revision"],
            number,
            due_at,
            created_at,
            routing,
        )
    except EvidenceError:
        # Bad or over-budget optional binding evidence never becomes an ordinary reminder.
        return None
