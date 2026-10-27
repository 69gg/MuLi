"""Small immutable update facts, independent from a later metadata check."""

from __future__ import annotations

import math
import re
from typing import Any

_EVIDENCE_FIELDS = frozenset({"format_version", "current", "latest", "checked_at"})


def release_version(value: str) -> tuple[int, int, int]:
    if len(value) > 40 or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("只支持正式发布版本号")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def update_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Select host-validated facts only, never a copy of remote metadata."""
    return {
        "format_version": 1,
        **{name: item[name] for name in ("current", "latest", "checked_at")},
    }


def update_notice_available(
    notice: dict[str, Any], latest_check: dict[str, Any] | None, current_version: str
) -> bool:
    """Validate a notice's own facts; historical notices retain their old gate."""
    if "update_evidence" not in notice:
        if (
            latest_check is None
            or latest_check.get("available") is not True
            or latest_check.get("status") != "checked"
        ):
            return False
        evidence = latest_check
    else:
        evidence = notice["update_evidence"]
        if (
            type(evidence) is not dict
            or evidence.keys() != _EVIDENCE_FIELDS
            or type(evidence.get("format_version")) is not int
            or evidence["format_version"] != 1
            or not isinstance(evidence.get("current"), str)
        ):
            return False
        checked_at = evidence.get("checked_at")
        if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
            return False
        try:
            if checked_at < 0 or not math.isfinite(checked_at):
                return False
        except OverflowError:
            return False
    target = evidence.get("latest")
    if not isinstance(target, str) or notice.get("id") != "update-" + target:
        return False
    try:
        version = release_version(target)
        if "update_evidence" in notice and version <= release_version(evidence["current"]):
            return False
        return version > release_version(current_version)
    except ValueError:
        return False
