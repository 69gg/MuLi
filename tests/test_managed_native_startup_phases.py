"""Eight startup points reach real retained-file entry checks, without systemd."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Any, NoReturn

import pytest
from helpers import managed_native_worker as worker
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_helper_costs import _guard_result, _GuardFixture
from test_managed_native_helper_costs import helper_guard as helper_guard
from test_management_helper import Probe as HelperProbe

import muli
from muli.managed_controller import ManagedTransactionController
from muli.managed_transaction import TransactionRecordV2
from muli.platforms.linux_transaction import ProbeLimits
from muli.platforms.managed_deployment import SlotIdentity
from muli.platforms.managed_probe import ManagedSystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated helper entry")


def namespace(guard: _GuardFixture) -> dict[str, Any]:
    assert isinstance(guard.run_helper, FunctionType)
    return guard.run_helper.__globals__


def current_package(guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        muli, "__file__", str(Path(guard.host.slots["base"].package.root) / "__init__.py")
    )


async def test_generated_eight_points_execute_the_actual_helper_coroutine_and_entry(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, helper = helper_guard, HelperProbe()
    actual = namespace(guard)
    entry = actual["_entry"]
    assert (entry.__wrapped__.__globals__ is actual) is True
    assert (actual is sys.modules["muli.managed_host"].__dict__) is False
    failure = RuntimeError("PRIVATE: stop only at the real controller step")
    process_checks: list[str] = []
    original_process = actual["verify_current_process"]

    def verify(slot: SlotIdentity) -> None:
        original_process(slot)
        process_checks.append(slot.digest)
        startup = guard.observer.startup
        assert startup is not None and startup.points["entry_dispatch"] is not None

    class Probe(ManagedSystemdProbe):
        async def read_helper(self, name: str, argv: tuple[str, ...], limits: Any) -> Any:
            return await helper.read_helper(name, argv, limits)

    async def step(controller: ManagedTransactionController) -> TransactionRecordV2:
        raise failure

    current_package(guard, monkeypatch)
    monkeypatch.setitem(actual, "verify_current_process", verify)
    monkeypatch.setattr(ManagedTransactionController, "step", step)
    guard.host.release_writer()
    guard.host.control.__exit__()
    with pytest.raises(RuntimeError) as caught:
        # This awaits the real coroutine; capture()/close() alone cannot supply entry points.
        await guard.run_helper(
            guard.root / "data/management/transaction-v1",
            probe=Probe(limits=ProbeLimits(seconds=guard.host.anchor.helper.operation_timeout / 4)),
            clock=guard.host.clock,
        )
    assert caught.value is failure
    assert process_checks == [guard.host.slots["base"].digest] * 5
    assert guard.observer.totals["host_record"]["returned"] == 5
    guard.observer.save()
    facts = worker.read_json(guard.path)
    startup = facts["startup"]
    assert startup["state"] == "complete" and startup["reason"] == ""
    assert set(startup["points"]) == {
        "hook_entered",
        "worker_import_returned",
        "unit_birth_returned",
        "host_import_dispatch",
        "host_import_returned",
        "main_entered",
        "entry_dispatch",
        "entry_returned",
    }
    assert all(point is not None for point in startup["points"].values())
    assert worker.helper_startup_facts(startup)
    assert worker.helper_guard_snapshot_facts(facts, guard.observer.source)
    assert guard.observer.reason == "" and "PRIVATE" not in json.dumps(facts)
    assert facts["attempts"] == {"prefix": 0, "final": 1}
    assert not (guard.root / "helper-entry-checkpoint.json").exists()


@pytest.mark.parametrize("clock_failure", [False, True])
async def test_real_entry_returns_same_object_once_and_clock_failure_stays_optional(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch, clock_failure: bool
) -> None:
    guard = helper_guard
    values: list[Any] = []
    actual = namespace(guard)
    entry, original_type = actual["_entry"], actual["_Entry"]

    def returned(*args: Any) -> Any:
        value = original_type(*args)
        values.append(value)
        return value

    def failed_clock() -> NoReturn:
        raise asyncio.CancelledError("PRIVATE clock failure")

    current_package(guard, monkeypatch)
    monkeypatch.setitem(actual, "_Entry", returned)
    with monkeypatch.context() as local:
        if clock_failure:
            local.setattr(
                worker,
                "time",
                SimpleNamespace(**(vars(worker.time) | {"monotonic_ns": failed_clock})),
            )
        value = entry(guard.root / "data/management/transaction-v1")
    assert len(values) == 1 and (value is values[0]) is True
    assert value.record == guard.host.record() and value.controller == guard.host.slots["base"]
    startup = guard.observer.startup
    assert startup is not None
    first = startup.snapshot()
    repeated = entry(guard.root / "data/management/transaction-v1")
    assert len(values) == 2 and (repeated is values[1]) is True
    assert startup.snapshot() == first, "repeated business calls must not add a ninth point"
    assert guard.observer.first_record is None
    guard.observer.save()
    assert not guard.path.exists(), "startup cannot bypass the original first-record gate"
    assert guard.observer.reason == "" and worker.helper_startup_facts(first)
    if clock_failure:
        assert first["state"] == "partial" and first["reason"] == "clock_invalid"
        assert first["points"]["entry_dispatch"] is first["points"]["entry_returned"] is None
    else:
        assert first["state"] == "complete"


@pytest.mark.parametrize("cancelled", [False, True])
async def test_real_entry_authentication_failure_preserves_exception_without_fabricated_return(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    guard = helper_guard
    actual = namespace(guard)
    failure = asyncio.CancelledError("PRIVATE") if cancelled else RuntimeError("PRIVATE")
    calls: list[str] = []
    original = actual["verify_current_process"]

    def verify(slot: SlotIdentity) -> None:
        original(slot)
        calls.append(slot.digest)
        raise failure

    current_package(guard, monkeypatch)
    monkeypatch.setitem(actual, "verify_current_process", verify)
    with pytest.raises(type(failure)) as caught:
        actual["_entry"](guard.root / "data/management/transaction-v1")
    assert caught.value is failure and calls == [guard.host.slots["base"].digest]
    assert guard.observer.first_record is None
    guard.observer.save()
    assert not guard.path.exists()
    guard.read_record(guard.host.control, guard.host.anchor)
    observed = guard.observer.snapshot("normal_exit")
    assert observed["code"] == "observed"
    facts = observed["startup"]
    assert facts["state"] == "partial" and facts["reason"] == "entry_raised"
    assert facts["points"]["entry_dispatch"] is not None
    assert facts["points"]["entry_returned"] is None
    assert worker.helper_startup_facts(facts) and guard.observer.reason == ""
    assert "PRIVATE" not in json.dumps(facts)


@pytest.mark.parametrize("helper_guard", ["replaced_entry", "foreign_entry_globals"], indirect=True)
async def test_entry_code_or_actual_globals_mismatch_does_not_corrupt_original_guard(
    helper_guard: _GuardFixture,
) -> None:
    guard = helper_guard
    guard.read_record(guard.host.control, guard.host.anchor)
    guard.host.backend._check(guard.host.record())
    facts = worker.read_json(guard.path)
    assert guard.observer.bound and guard.observer.reason == ""
    assert facts["startup"] == worker.helper_startup_unavailable("unbound")
    assert worker.helper_guard_snapshot_facts(facts, guard.observer.source)


def complete_startup() -> dict[str, Any]:
    return {
        "version": 1,
        "clock": "monotonic_ns/process_time_ns",
        "state": "complete",
        "reason": "",
        "points": {
            name: {"wall_ns": 100 + index, "cpu_ns": 10 + index}
            for index, name in enumerate(worker.HELPER_STARTUP_POINTS)
        },
    }


@pytest.mark.parametrize("early", [[], [("hook_entered", None, None)], [("hook_entered", True, 1)]])
def test_missing_or_failed_early_points_stay_null_and_unbound_points_are_not_exported(
    early: list[tuple[str, object, object]],
) -> None:
    startup = worker.HelperStartup(early)
    assert startup.snapshot() == worker.helper_startup_unavailable("unbound")
    startup.bound = True
    for name in worker.HELPER_STARTUP_POINTS[3:]:
        startup.mark(name)
    value = startup.snapshot()
    assert value["state"] == "partial" and value["reason"] in {"points_missing", "clock_invalid"}
    assert all(value["points"][name] is None for name in worker.HELPER_STARTUP_POINTS[:3])
    assert worker.helper_startup_facts(value)


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "extra",
        "clock",
        "point",
        "bool",
        "negative",
        "wall",
        "cpu",
        "missing",
        "reason",
        "partial",
        "unavailable",
    ],
)
def test_startup_decoder_rejects_forged_or_inconsistent_fixed_fields(change: str) -> None:
    value = complete_startup()
    assert worker.helper_startup_facts(value)
    point = value["points"]["entry_returned"]
    if change == "version":
        value["version"] = True
    elif change == "extra":
        value["private"] = "PRIVATE"
    elif change == "clock":
        value["clock"] = "perf_counter_ns"
    elif change == "point":
        value["points"]["ninth"] = point
    elif change == "bool":
        point["wall_ns"] = True
    elif change == "negative":
        point["cpu_ns"] = -1
    elif change in {"wall", "cpu"}:
        point[change + "_ns"] = 0
    elif change == "missing":
        value["points"]["entry_returned"] = None
    elif change == "reason":
        value["reason"] = "PRIVATE"
    else:
        value["state"], value["reason"] = change, "points_missing"
    assert not worker.helper_startup_facts(value)


async def test_legacy_snapshot_and_partial_startup_remain_readable_and_bad_optional_is_rejected(
    helper_guard: _GuardFixture,
) -> None:
    guard = helper_guard
    guard.read_record(guard.host.control, guard.host.anchor)
    guard.host.backend._check(guard.host.record())
    facts = worker.read_json(guard.path)
    assert facts["startup"]["state"] == "partial"
    assert facts["startup"]["points"]["entry_dispatch"] is None
    assert worker.helper_guard_snapshot_facts(facts, guard.observer.source)
    legacy = copy.deepcopy(facts)
    legacy.pop("startup")
    assert worker.helper_guard_snapshot_facts(legacy, guard.observer.source)
    result, retained = _guard_result(guard), []
    sources = [{"source": dict(guard.observer.source), "observation": None}]
    worker.collect_helper_guard_costs(guard.root, result, sources, retained, deadline=None)
    accepted = copy.deepcopy(retained)
    facts["startup"]["points"]["private-path"] = "PRIVATE"
    assert not worker.helper_guard_snapshot_facts(facts, guard.observer.source)
    guard.path.write_text(json.dumps(facts), encoding="utf-8")
    worker.collect_helper_guard_costs(guard.root, result, sources, retained, deadline=None)
    assert retained == accepted


async def test_optional_startup_snapshot_failure_preserves_the_real_guard_receipt(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = helper_guard
    guard.read_record(guard.host.control, guard.host.anchor)
    guard.host.backend._check(guard.host.record())
    assert guard.observer.startup is not None

    def failed() -> NoReturn:
        raise asyncio.CancelledError("PRIVATE optional statistics")

    monkeypatch.setattr(guard.observer.startup, "snapshot", failed)
    guard.observer.save()
    facts = worker.read_json(guard.path)
    assert facts["code"] == "observed" and facts["host_record"]["returned"] == 1
    assert facts["startup"] == worker.helper_startup_unavailable("statistics_failed")
    assert worker.helper_guard_snapshot_facts(facts, guard.observer.source)
    assert guard.observer.reason == "" and "PRIVATE" not in json.dumps(facts)


async def test_startup_yields_snapshot_budget_before_breakdown_without_another_publication(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = helper_guard
    guard.read_record(guard.host.control, guard.host.anchor)
    guard.host.backend._check(guard.host.record())
    first = worker.read_json(guard.path)
    assert "startup" in first and "breakdown" in first
    base = copy.deepcopy(first)
    base.pop("startup")
    lowered = len(json.dumps(base, sort_keys=True, separators=(",", ":")).encode()) + 128
    assert lowered < len(guard.path.read_bytes()) < worker.MAX_BYTES
    monkeypatch.setattr(worker, "MAX_BYTES", lowered)
    guard.observer.save()
    guard.observer.save()
    final = worker.read_json(guard.path)
    assert "startup" not in final and "breakdown" in final
    assert final["attempts"] == {"prefix": 1, "final": 1}
    assert final["prior_publications"]["count"] == 1
    assert worker.helper_guard_snapshot_facts(final, guard.observer.source)


async def test_startup_yields_aggregate_budget_before_breakdown_and_base_evidence(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = helper_guard
    guard.read_record(guard.host.control, guard.host.anchor)
    guard.host.backend._check(guard.host.record())
    result = _guard_result(guard)
    worker.collect_helper_guard_costs(
        guard.root,
        result,
        [{"source": dict(guard.observer.source), "observation": None}],
        [],
        deadline=None,
    )
    expected = copy.deepcopy(result)
    expected["resources"]["helper_guard_costs"]["sources"][0]["costs"].pop("startup")
    lowered = len(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode())
    assert lowered < worker.MAX_BYTES
    monkeypatch.setattr(worker, "MAX_BYTES", lowered)
    worker.trim_binding_diagnostics(result)
    assert result == expected
    assert worker.helper_guard_costs_facts(result["resources"]["helper_guard_costs"], result)
