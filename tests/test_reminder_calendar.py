"""Real time zones and independent historical UTC expectations, without network I/O."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from muli.reminder_calendar import next_after, normalize_calendar


def rule(
    zone: str = "Asia/Shanghai",
    local_time: str = "07:30",
    weekdays: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "timezone": zone,
        "local_time": local_time,
        "weekdays": list(range(1, 8)) if weekdays is None else weekdays,
    }


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()


def test_normalization_is_a_fresh_sorted_rule_and_does_not_mutate_the_caller() -> None:
    original = rule(weekdays=[5, 1, 3])
    normalized = normalize_calendar(original)
    assert normalized == {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "local_time": "07:30",
        "weekdays": [1, 3, 5],
    }
    normalized["weekdays"].append(7)
    assert original["weekdays"] == [5, 1, 3]


@pytest.mark.parametrize(
    "weekdays,reference,expected",
    [
        (list(range(1, 8)), utc(2024, 1, 1), utc(2024, 1, 1, 23, 30)),
        ([1, 2, 3, 4, 5], utc(2024, 1, 5), utc(2024, 1, 7, 23, 30)),
        ([1], utc(2024, 1, 1), utc(2024, 1, 7, 23, 30)),
    ],
    ids=["daily-local-date", "weekdays-over-weekend", "weekly"],
)
def test_local_calendar_dates_produce_the_expected_utc_instant(
    weekdays: list[int], reference: float, expected: float
) -> None:
    assert next_after(rule(weekdays=weekdays), reference) == expected


@pytest.mark.parametrize(
    "reference,expected",
    [
        (utc(2024, 1, 31), utc(2024, 1, 31, 23, 30)),
        (utc(2024, 2, 29), utc(2024, 2, 29, 23, 30)),
        (utc(2024, 12, 31), utc(2024, 12, 31, 23, 30)),
    ],
    ids=["month-boundary", "leap-day", "year-boundary"],
)
def test_month_and_year_boundaries_use_local_dates(reference: float, expected: float) -> None:
    assert next_after(rule(), reference) == expected


def test_packaged_tzdata_supplies_real_zones_when_system_paths_are_empty() -> None:
    script = """
import json
from datetime import UTC, datetime
import zoneinfo
from muli.reminder_calendar import next_after
assert zoneinfo.TZPATH == ()
rule = {"version": 1, "timezone": "America/New_York", "local_time": "01:30", "weekdays": list(range(1, 8))}
reference = datetime(2024, 11, 3, 6, tzinfo=UTC).timestamp()
print(json.dumps(next_after(rule, reference)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONTZPATH": ""},
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == utc(2024, 11, 4, 6, 30)


@pytest.mark.parametrize("local_time", ["０７:３０", "07:30 ", "07:30:00", "24:00", "07:60"])
def test_only_ascii_hour_and_minute_are_accepted(local_time: str) -> None:
    with pytest.raises(ValueError, match="^calendar_local_time_invalid$"):
        normalize_calendar(rule(local_time=local_time))


@pytest.mark.parametrize("weekdays", [[True], [], [1, 1], [0], [8]])
def test_weekday_booleans_duplicates_and_out_of_range_values_are_rejected(
    weekdays: list[int],
) -> None:
    with pytest.raises(ValueError, match="^calendar_weekdays_invalid$"):
        normalize_calendar(rule(weekdays=weekdays))


def test_explicit_iana_zone_and_exact_rule_shape_are_required() -> None:
    for changes in ({"timezone": ""}, {"timezone": "Not/A_Real_Zone"}, {"version": True}):
        with pytest.raises(ValueError):
            normalize_calendar({**rule(), **changes})
    missing_zone = rule()
    del missing_zone["timezone"]
    with pytest.raises(ValueError, match="^calendar_rule_invalid$"):
        normalize_calendar(missing_zone)
    with pytest.raises(ValueError, match="^calendar_rule_invalid$"):
        normalize_calendar({**rule(), "commitment_id": "not-a-calendar-field"})


@pytest.mark.parametrize("reference", [True, float("nan"), float("inf"), 10**400])
def test_reference_must_be_a_finite_non_boolean_timestamp(reference: object) -> None:
    with pytest.raises(ValueError, match="^calendar_reference_invalid$"):
        next_after(rule(), reference)


def test_finite_but_unrepresentable_reference_is_not_a_valid_schedule() -> None:
    with pytest.raises(ValueError, match="^calendar_reference_out_of_range$"):
        next_after(rule(), 1e20)


@pytest.mark.parametrize(
    "reference,expected",
    [
        (utc(2024, 11, 3, 5), utc(2024, 11, 3, 5, 30)),
        (utc(2024, 11, 3, 5, 30), utc(2024, 11, 4, 6, 30)),
        (utc(2024, 11, 3, 6), utc(2024, 11, 4, 6, 30)),
    ],
    ids=["before-both-folds", "exactly-earlier-fold", "between-folds"],
)
def test_each_local_date_chooses_its_earlier_fold_before_checking_the_reference(
    reference: float, expected: float
) -> None:
    actual = next_after(rule("America/New_York", "01:30"), reference)
    assert actual == expected
    local = datetime.fromtimestamp(actual, ZoneInfo("America/New_York"))
    assert (local.hour, local.minute, local.fold) == (1, 30, 0)


@pytest.mark.parametrize(
    "weekdays,reference,expected",
    [
        (list(range(1, 8)), utc(2024, 3, 9, 8), utc(2024, 3, 11, 6, 30)),
        ([7], utc(2024, 3, 3, 8), utc(2024, 3, 17, 6, 30)),
    ],
    ids=["missing-wall-time-skips-date", "weekly-gap-needs-fifteenth-date"],
)
def test_spring_gap_is_not_silently_shifted_to_another_wall_time(
    weekdays: list[int], reference: float, expected: float
) -> None:
    assert next_after(rule("America/New_York", "02:30", weekdays), reference) == expected


@pytest.mark.parametrize(
    "reference,expected,hours",
    [
        (utc(2024, 3, 9, 12, 30), utc(2024, 3, 10, 11, 30), 23),
        (utc(2024, 11, 2, 11, 30), utc(2024, 11, 3, 12, 30), 25),
    ],
)
def test_same_daily_wall_time_is_not_a_fixed_twenty_four_hour_interval(
    reference: float, expected: float, hours: int
) -> None:
    actual = next_after(rule("America/New_York"), reference)
    assert actual == expected and actual - reference == hours * 3600


def test_real_half_hour_change_does_not_assume_dst_always_changes_by_one_hour() -> None:
    reference = utc(2024, 10, 4, 21)
    actual = next_after(rule("Australia/Lord_Howe"), reference)
    assert actual == utc(2024, 10, 5, 20, 30)
    assert actual - reference == 23.5 * 3600
