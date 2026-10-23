"""Finite host provenance for local, read-only project attention runs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from muli.store import Store


def project_review_limits() -> tuple[int, int]:
    """Use the existing Attention schema's ceilings when reading older receipts."""
    from muli.config import AttentionSettings

    fields = AttentionSettings.model_json_schema()["properties"]
    return int(fields["batch_size"]["maximum"]), int(fields["max_chars"]["maximum"])


def validate_project_review(
    value: object, *, max_sources: int | None = None, max_chars: int | None = None
) -> dict[str, Any]:
    """Copy an exact host marker; this does not authenticate its source facts."""
    source_limit, char_limit = project_review_limits()
    if max_sources is not None:
        source_limit = min(source_limit, max_sources)
    if max_chars is not None:
        char_limit = min(char_limit, max_chars)

    def reject() -> NoReturn:
        raise ValueError("项目复核宿主证明无效或超过注意事项预算")

    def identifier(item: object) -> bool:
        return isinstance(item, str) and len(item) == 32

    if not isinstance(value, dict) or set(value) != {
        "version",
        "project_id",
        "binding_revision",
        "sources",
        "task_snapshot",
    }:
        reject()
    assert isinstance(value, dict)
    project = value["project_id"]
    sources = value["sources"]
    snapshot = value["task_snapshot"]
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(project, str)
        or not project.strip()
        or len(project) > 256
        or not identifier(value["binding_revision"])
        or not isinstance(sources, list)
        or not 1 <= len(sources) <= source_limit
        or not isinstance(snapshot, dict)
        or set(snapshot) != {"revision", "status", "epoch", "approved_epoch"}
    ):
        reject()
    assert isinstance(sources, list) and isinstance(snapshot, dict)
    if (
        type(snapshot["revision"]) is not int
        or snapshot["revision"] < 1
        or not isinstance(snapshot["status"], str)
        or snapshot["status"] not in {"open", "in_progress", "blocked"}
        or type(snapshot["epoch"]) is not int
        or snapshot["epoch"] < 0
        or type(snapshot["approved_epoch"]) is not int
        or not 0 <= snapshot["approved_epoch"] <= snapshot["epoch"]
    ):
        reject()
    copied: list[dict[str, Any]] = []
    records: set[str] = set()
    previous_event = 0
    source_key: tuple[str, str] | None = None
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source) != {"record_id", "event_id", "scope", "revision", "watcher_generation"}
            or not all(identifier(source[key]) for key in ("record_id", "scope", "revision"))
            or type(source["event_id"]) is not int
            or source["event_id"] <= previous_event
            or type(source["watcher_generation"]) is not int
            or source["watcher_generation"] < 1
            or source["record_id"] in records
        ):
            reject()
        assert isinstance(source, dict)
        key = source["scope"], source["revision"]
        if source_key is not None and source_key != key:
            reject()
        source_key = key
        previous_event = source["event_id"]
        records.add(source["record_id"])
        copied.append(dict(source))
    result = {**value, "sources": copied, "task_snapshot": dict(snapshot)}
    try:
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        serialized.encode("utf-8", errors="strict")
    except (ValueError, UnicodeError):
        reject()
    if len(serialized) > char_limit:
        reject()
    return result


def project_review_key(value: object) -> str:
    """Group only identical finite host facts, independently of JSON field order."""
    marker = validate_project_review(value)
    return json.dumps(
        marker, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def require_project_review_format(store: Store) -> None:
    """Re-read real evidence; never promote a database as an admission side effect."""
    from muli.format_admission import CURRENT_FORMAT_STATE, FormatAdmissionError
    from muli.format_evidence import read_optional_format_evidence

    expected = CURRENT_FORMAT_STATE.model_copy(
        update={
            "formats": CURRENT_FORMAT_STATE.formats.model_copy(update={"objects": 2, "ingress": 2})
        }
    )
    evidence = read_optional_format_evidence(store.db)
    if evidence is None or evidence.state != expected or store.format_state != evidence.state:
        raise FormatAdmissionError("project_review_format_required")
    # A previously admitted Store also retains its constructor-authenticated
    # identity. Revalidation checks that identity as well as the current schema.
    store.revalidate_format()
