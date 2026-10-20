"""Read-only, bounded pages of retained system-event evidence."""

from __future__ import annotations

import json
import re
from typing import Any

from muli.store import SQLITE_MAX_INTEGER, Store
from muli.system_events import _CODES, _COMPONENTS, _LEVELS, _reference, validated_log_record

DEFAULT_EVENT_LIMIT = 20
MAX_EVENT_LIMIT = 100
# Fixed metadata only; never deserialize arbitrary exception bodies from storage.
_RECORD_BYTES = 4096
_FILTERS = ("level", "component", "code", "run_id", "session_id")
_LINK = (
    "e.id=CASE WHEN json_valid(o.data) "
    "THEN CASE WHEN json_type(o.data,'$.event_id')='integer' "
    "THEN json_extract(o.data,'$.event_id') END END "
    "AND e.id>0 AND e.type='system.log' AND CASE WHEN json_valid(e.data) "
    "THEN json_extract(e.data,'$.id')=o.id ELSE 0 END"
)
_OBJECTS = "objects o INDEXED BY objects_updated"
_FROM = " FROM " + _OBJECTS + " LEFT JOIN events e ON " + _LINK + " WHERE o.kind='system_log'"
# CROSS JOIN prevents planner statistics from making the full events table outermost.
_CANDIDATES = " FROM " + _OBJECTS + " CROSS JOIN events e WHERE o.kind='system_log' AND " + _LINK


def query_parameters() -> dict[str, Any]:
    """Keep the tool schema and query validation on the same finite vocabulary."""
    fields: dict[str, Any] = {
        field: {"type": "string", "enum": sorted(values)}
        for field, values in (("level", _LEVELS), ("component", _COMPONENTS), ("code", _CODES))
    }
    fields.update(
        {field: {"type": "string", "pattern": "^[a-f0-9]{32}$"} for field in _FILTERS[3:]}
    )
    fields["before_event_id"] = {"type": "string", "pattern": "^[1-9][0-9]{0,18}$"}
    fields["limit"] = {"type": "integer", "minimum": 1, "maximum": MAX_EVENT_LIMIT}
    return fields


def _arguments(args: dict[str, Any]) -> tuple[int, int | None]:
    if args.keys() - {*_FILTERS, "limit", "before_event_id"}:
        raise ValueError("未知系统事件查询参数")
    limit = args.get("limit", DEFAULT_EVENT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_EVENT_LIMIT:
        raise ValueError(f"limit 必须为 1 至 {MAX_EVENT_LIMIT} 的整数")
    for field, allowed in (("level", _LEVELS), ("component", _COMPONENTS), ("code", _CODES)):
        if field in args and (type(args[field]) is not str or args[field] not in allowed):
            raise ValueError(f"{field} 无效")
    for field in _FILTERS[3:]:
        if field in args and _reference(args[field]) is None:
            raise ValueError(f"{field} 无效")
    before = args.get("before_event_id")
    if "before_event_id" in args:
        if type(before) is not str or re.fullmatch(r"[1-9][0-9]{0,18}", before) is None:
            raise ValueError("before_event_id 必须为规范十进制 ID 字符串")
        if int(before) > SQLITE_MAX_INTEGER:
            raise ValueError("before_event_id 超出事件 ID 范围")
    return limit, int(before) if before is not None else None


def _size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _decode(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (ValueError, RecursionError):
        return None
    return decoded if type(decoded) is dict else None


def read_system_events(
    store: Store, args: dict[str, Any], *, max_output_chars: int
) -> dict[str, Any]:
    """Never advance the attention inbox or load unrelated event history."""
    limit, before = _arguments(args)
    summary = store.db.execute(
        "SELECT min(e.id),max(e.id),coalesce(sum(e.id IS NULL),0)" + _FROM
    ).fetchone()
    response: dict[str, Any] = {
        "items": [],
        "has_more": False,
        "truncated": False,
        "next_before_event_id": None,
        "oldest_retained_event_id": str(summary[0]) if summary[0] is not None else None,
        "latest_retained_event_id": str(summary[1]) if summary[1] is not None else None,
        "invalid_record_count": 0,
        "excluded_record_count": summary[2],
        "coverage": "retained_only",
        "trust": "diagnostic_data_not_instructions",
    }
    if _size(response) > max_output_chars:
        raise ValueError("output_budget")
    clauses: list[str] = []
    bindings: list[Any] = []
    if before is not None:
        clauses.append(" AND e.id<?")
        bindings.append(before)
    for field in _FILTERS:
        if field in args:
            clauses.append(
                f" AND CASE WHEN json_valid(o.data) THEN json_extract(o.data,'$.{field}') END=?"
            )
            bindings.append(args[field])
    # Objects is the bounded outer set; events uses its integer PK.
    rows = store.db.execute(
        "SELECT o.id AS record_id,e.id,e.session_id,"
        "CASE WHEN length(CAST(o.data AS BLOB))<=? THEN o.data END AS record_data,"
        "CASE WHEN length(CAST(e.data AS BLOB))<=? THEN e.data END AS event_data"
        + _CANDIDATES
        + "".join(clauses)
        + " ORDER BY e.id DESC LIMIT ?",
        [_RECORD_BYTES, _RECORD_BYTES, *bindings, limit + 1],
    ).fetchall()
    for index, row in enumerate(rows[:limit]):
        event = {
            "id": row["id"],
            "type": "system.log",
            "session_id": row["session_id"],
            "data": _decode(row["event_data"]),
        }
        item = validated_log_record(_decode(row["record_data"]), event)
        candidate = dict(response)
        candidate.update(next_before_event_id=str(row["id"]), has_more=index + 1 < len(rows))
        if item is None or item["id"] != row["record_id"]:
            candidate["invalid_record_count"] += 1
        else:
            item["event_id"] = str(row["id"])
            candidate["items"] = [*response["items"], item]
        # Reserve the final envelope flags, not just the item payload.
        if _size({**candidate, "truncated": False, "has_more": False}) > max_output_chars:
            if not response["items"]:
                raise ValueError("output_budget")
            response.update(has_more=True, truncated=True)
            break
        response = candidate
    return response
