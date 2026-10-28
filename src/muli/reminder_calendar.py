"""Bounded local-calendar recurrence, including missing and repeated wall times."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, time, timedelta
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MAX_CANDIDATE_DATES = 15
_LOCAL_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


class CalendarRule(TypedDict):
    version: Literal[1]
    timezone: str
    local_time: str
    weekdays: list[int]


def _validated_rule(value: object) -> tuple[CalendarRule, ZoneInfo]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "timezone",
        "local_time",
        "weekdays",
    }:
        raise ValueError("calendar_rule_invalid")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("calendar_rule_version_invalid")
    zone_key, local_time, selected = value["timezone"], value["local_time"], value["weekdays"]
    if not isinstance(zone_key, str) or not zone_key:
        raise ValueError("calendar_timezone_required")
    if not isinstance(local_time, str) or _LOCAL_TIME.fullmatch(local_time) is None:
        raise ValueError("calendar_local_time_invalid")
    if not isinstance(selected, list) or not 1 <= len(selected) <= 7:
        raise ValueError("calendar_weekdays_invalid")
    weekdays: list[int] = []
    for day in selected:
        if type(day) is not int or not 1 <= day <= 7 or day in weekdays:
            raise ValueError("calendar_weekdays_invalid")
        weekdays.append(day)
    try:
        zone = ZoneInfo(zone_key)
    except (ZoneInfoNotFoundError, ValueError, OSError, UnicodeError) as exc:
        raise ValueError("calendar_timezone_unavailable") from exc
    return {
        "version": 1,
        "timezone": zone_key,
        "local_time": local_time,
        "weekdays": sorted(weekdays),
    }, zone


def normalize_calendar(value: object) -> CalendarRule:
    """Validate one complete rule and return fresh JSON-compatible fields."""
    return _validated_rule(value)[0]


def next_after(value: object, reference: object) -> float:
    """Find the first canonical occurrence strictly after a finite timestamp.

    Each date has at most one canonical occurrence: its earlier valid UTC
    instant. Select that instant before testing whether it is still future;
    otherwise a reference between folds would incorrectly select fold 1.
    Missing wall times skip their date. At most 15 local dates are inspected.
    """
    if type(reference) is not int and type(reference) is not float:
        raise ValueError("calendar_reference_invalid")
    try:
        finite = math.isfinite(reference)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("calendar_reference_invalid")
    rule, zone = _validated_rule(value)
    hour, minute = (int(part) for part in rule["local_time"].split(":"))
    try:
        first_date = datetime.fromtimestamp(reference, zone).date()
        for offset in range(_MAX_CANDIDATE_DATES):
            candidate_date = first_date + timedelta(days=offset)
            if candidate_date.isoweekday() not in rule["weekdays"]:
                continue
            wall = datetime.combine(candidate_date, time(hour, minute))
            instants: set[float] = set()
            for fold in (0, 1):
                instant = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                restored = instant.astimezone(zone).replace(tzinfo=None)
                if restored == wall:
                    instants.add(instant.timestamp())
            if instants:
                canonical = min(instants)
                if canonical > reference:
                    return canonical
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("calendar_reference_out_of_range") from exc
    raise ValueError("calendar_next_unavailable")
