"""Historical failure checkpoints; all native effects are injected."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from test_managed_native_ci import Commands, ci, worker
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_ci import manager_observation as manager_observation

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only CI coordinator")


def observed_round() -> dict[str, Any]:
    """The first existing pending round, completed at fake monotonic 100.0."""
    return {
        "version": 1,
        "code": "observed",
        "elapsed_ms": 1000,
        "runtime": {"active_state": "activating", "sub_state": "start", "job": 11},
        "manager": {
            "active_state": "activating",
            "sub_state": "start",
            "job": 11,
            "pid": 42,
            "start_ticks": 100,
            "invocation": "0a" * 16,
        },
    }


async def test_last_completed_round_survives_next_query_timeout_without_new_reads(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli import process

    control = manager_observation
    control["states"] = ["activating"]
    original = process.run_process
    calls = 0

    async def query(
        argv: list[str],
        *,
        timeout: float,
        limit: int,
        merge_stderr: bool,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise TimeoutError("private query did not return its byte count")
        assert calls <= 4
        return await original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    monkeypatch.setattr(process, "run_process", query)
    limit = worker.MAX_BYTES - 1024
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        limit,
        started=99.0,
    )
    assert value["code"] == "native_deadline_exceeded" and value["used"] == limit
    assert value["query_status"] is None and value["query_code"] == "timeout"
    assert calls == 4 and control["polls"] == 1 and len(control["files"]) == 1
    assert control["clock"] == 100.1
    assert value.get("last_completed_round") == observed_round()
    wire = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert len(wire) <= 1024 and value["used"] + len(wire) <= worker.MAX_BYTES
    assert ci.manager_failure_facts(value)
    assert b"private" not in wire and control["group"].encode() not in wire


async def test_first_incomplete_round_reports_not_observed_without_recovery_queries(
    manager_observation: dict[str, Any],
) -> None:
    control = manager_observation
    control["states"] = ["activating"]
    control["overrides"]["Id"] = "other.service"
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=99.0,
    )
    assert value["code"] == "native_manager_reply"
    assert control["polls"] == 1 and len(control["calls"]) == 3 and not control["files"]
    assert value.get("last_completed_round") == {"version": 1, "code": "not_observed"}
    assert ci.manager_failure_facts(value)


def test_checkpoint_reply_fits_original_reserve_and_survives_exhausted_fallback(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    checkpoint = observed_round()
    checkpoint["elapsed_ms"] = (worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS) * 1000
    for name in ("runtime", "manager"):
        checkpoint[name].update(sub_state="a" * 64, job=2**31 - 1)
    checkpoint["manager"].update(pid=2**31 - 1, start_ticks=2**64 - 1, invocation="f" * 32)
    summary: dict[str, Any] = {
        "code": "native_deadline_exceeded",
        "stage": "manager_validation",
        "query_status": 0,
        "query_code": "ok",
        "used": 0,
        "last_completed_round": checkpoint,
    }
    largest = summary | {
        "code": max(ci.MANAGER_FAILURE_CODES, key=len),
        "stage": max(ci.MANAGER_STAGES, key=len),
        "query_status": -(2**31),
        "query_code": max(ci.COMMAND_CODES, key=len),
        "used": ci.OUTPUT_BYTES - 1024,
    }
    assert len((json.dumps(largest, sort_keys=True, separators=(",", ":")) + "\n").encode()) <= 1024
    returned = 0
    before = 0

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal returned, before
        if "manager-ready" in argv:
            before = native.setup_bytes
            summary["used"] = int(argv[8])
            assert summary["used"] == ci.OUTPUT_BYTES - before - 1024
            output = (json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n").encode()
            returned = len(output)
            assert returned <= 1024
            return ci.Reply(0, output)
        return commands(argv, seconds=seconds)

    native.run = run
    with pytest.raises(ci.FixtureError, match="^native_deadline_exceeded$"):
        native.execute()
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"]["manager_failure"] == summary
    assert saved["resources"]["setup"]["diagnostic_code"] == "output_budget"
    assert saved["timeline"] == [] and saved["generations"] == []
    assert not any("diagnose-setup" in argv for argv in commands.calls)
    assert native.setup_bytes == before + summary["used"] + returned <= ci.OUTPUT_BYTES
    assert ci.manager_failure_facts(largest)
    assert not ci.manager_failure_facts(summary | {"private": "must not escape"})


@pytest.mark.parametrize("case", ["invocation", "pid", "ticks", "cgroup"])
async def test_incomplete_identity_change_keeps_only_previous_complete_round(
    manager_observation: dict[str, Any],
    case: str,
) -> None:
    control = manager_observation
    control["states"] = ["activating"]
    if case == "invocation":
        control["change_invocation"] = True
    elif case == "ticks":
        control["change_ticks"] = True
    else:
        key, value = {
            "pid": ("MainPID", 0),
            "cgroup": ("ControlGroup", "/changed/user@23145.service"),
        }[case]
        control["final_overrides"] = {key: value}
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=99.0,
    )
    assert value["code"] == "native_manager_changed"
    assert value["last_completed_round"] == observed_round()
    assert control["polls"] == 2 and len(control["calls"]) == 6
    assert len(control["files"]) <= 2 and control["clock"] == 100.1
    assert ci.manager_failure_facts(value)


@pytest.mark.parametrize("case", ["no-pid", "zero-invocation", "old-invocation", "ticks-range"])
async def test_first_unbound_or_unprojectable_round_does_not_invent_a_checkpoint(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from muli import process

    control = manager_observation
    control["states"] = ["activating"]
    previous = "0a" * 16 if case == "old-invocation" else ""
    if case == "no-pid":
        control["overrides"]["MainPID"] = 0
    elif case == "zero-invocation":
        control["overrides"]["InvocationID"] = [0] * 16
    elif case == "ticks-range":
        control["ticks"] = 2**64
    original = process.run_process
    calls = 0

    async def query(
        argv: list[str],
        *,
        timeout: float,
        limit: int,
        merge_stderr: bool,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise TimeoutError
        return await original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    monkeypatch.setattr(process, "run_process", query)
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=99.0,
        previous=previous,
    )
    assert value["code"] == "native_deadline_exceeded"
    assert value["last_completed_round"] == {"version": 1, "code": "not_observed"}
    assert calls == 4 and control["polls"] == 1
    assert len(control["files"]) == (0 if case == "no-pid" else 1)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("version",), True),
        (("version",), 2),
        (("code",), "ready"),
        (("elapsed_ms",), True),
        (("elapsed_ms",), -1),
        (("elapsed_ms",), (worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS) * 1000 + 1),
        (("private",), "secret"),
        (("runtime",), []),
        (("runtime", "private"), "secret"),
        (("manager", "private"), "secret"),
        (("runtime", "active_state"), "failed"),
        (("runtime", "active_state"), []),
        (("runtime", "sub_state"), "a" * 65),
        (("manager", "sub_state"), "private\ntext"),
        (("runtime", "job"), True),
        (("runtime", "job"), -1),
        (("manager", "job"), 2**31),
        (("manager", "pid"), True),
        (("manager", "pid"), 0),
        (("manager", "pid"), 2**31),
        (("manager", "start_ticks"), True),
        (("manager", "start_ticks"), -1),
        (("manager", "start_ticks"), 2**64),
        (("manager", "invocation"), "0" * 32),
        (("manager", "invocation"), "A" * 32),
        (("manager", "invocation"), "0a" * 15),
    ],
)
def test_checkpoint_nested_schema_rejects_unbounded_or_untyped_values(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    checkpoint = observed_round()
    target = checkpoint if len(path) == 1 else checkpoint[path[0]]
    target[path[-1]] = replacement
    summary = {
        "code": "native_deadline_exceeded",
        "stage": "manager_validation",
        "query_status": 0,
        "query_code": "ok",
        "used": 1,
    }
    assert ci.manager_failure_facts(summary)  # Existing five-key artifacts remain readable.
    assert not ci.manager_failure_facts(summary | {"last_completed_round": checkpoint})


@pytest.mark.parametrize("name", ["runtime", "manager"])
def test_checkpoint_inactive_requires_an_existing_job(name: str) -> None:
    checkpoint = observed_round()
    checkpoint[name].update(active_state="inactive", sub_state="dead", job=1)
    assert ci.manager_checkpoint_facts(checkpoint)
    checkpoint[name]["job"] = 0
    assert not ci.manager_checkpoint_facts(checkpoint)


@pytest.mark.parametrize(
    "checkpoint",
    [
        None,
        {},
        {"version": True, "code": "not_observed"},
        {"version": 1, "code": "not_observed", "manager": {}},
        {"version": 1, "code": "observed"},
    ],
)
def test_checkpoint_missing_or_extra_shape_is_rejected(checkpoint: object) -> None:
    assert not ci.manager_checkpoint_facts(checkpoint)


def test_projection_limits_leave_previous_round_and_input_untouched() -> None:
    runtime = {"ActiveState": "activating", "SubState": "start", "Job": [11, "/job"]}
    manager = runtime | {
        "MainPID": 42,
        "ControlGroup": "/private/group",
        "invocation": "0a" * 16,
    }
    historical = worker._manager_checkpoint(
        runtime, manager, (42, 100, "/private/group"), "0a" * 16, 1.0
    )
    assert historical == observed_round()
    for ticks, elapsed in (
        (2**64, 1.0),
        (100, -1.0),
        (100, float("inf")),
        (100, worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS + 1.0),
    ):
        candidate = worker._manager_checkpoint(
            runtime,
            manager,
            (42, ticks, "/private/group"),
            "0a" * 16,
            elapsed,
        )
        assert candidate is None
        assert historical == observed_round() and manager["MainPID"] == 42


async def test_later_complete_but_out_of_projection_range_round_keeps_history(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli import process

    control = manager_observation
    control["states"] = ["activating"]
    original = process.run_process
    calls = 0

    async def query(
        argv: list[str],
        *,
        timeout: float,
        limit: int,
        merge_stderr: bool,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 7:
            raise TimeoutError
        return await original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    monkeypatch.setattr(process, "run_process", query)
    started = control["clock"] - worker.HARNESS_SECONDS - worker.CLEANUP_SECONDS
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=started,
    )
    expected = observed_round() | {
        "elapsed_ms": (worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS) * 1000,
    }
    assert value["code"] == "native_deadline_exceeded"
    assert value["last_completed_round"] == expected
    assert calls == 7 and control["polls"] == 2 and len(control["files"]) == 2


async def test_incomplete_ready_tail_does_not_publish_a_new_complete_round(
    manager_observation: dict[str, Any],
) -> None:
    control = manager_observation
    control["states"] = ["activating", "active"]
    control["bad_kernel"] = True
    value = await worker.manager_state(
        "manager-ready",
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=99.0,
    )
    assert value["code"] != "ready"
    assert value["last_completed_round"] == observed_round()
    assert any(path.name == "memory.max" for path in control["files"])


@pytest.mark.parametrize("mode", ["manager-idle", "manager-ready"])
async def test_success_shapes_and_full_ready_proof_are_unchanged(
    manager_observation: dict[str, Any],
    mode: str,
) -> None:
    control = manager_observation
    control["states"] = ["inactive"] if mode == "manager-idle" else ["activating", "active"]
    control["ticks"] = 2**64  # Projection-only cap must not become a new readiness requirement.
    value = await worker.manager_state(
        mode,
        control["user"],
        control["uid"],
        control["home"],
        175.0,
        worker.MAX_BYTES - 1024,
        started=99.0,
    )
    assert set(value) == (
        {"code", "used", "invocation"}
        if mode == "manager-idle"
        else {"code", "used", "invocation", "group", "pid", "elapsed_ms", "late_ready"}
    )
    assert value["code"] == ("idle" if mode == "manager-idle" else "ready")
    if mode == "manager-ready":
        assert any(path.name == "cpu.max" for path in control["files"])
        assert len([path for path in control["files"] if path.name == "stat"]) >= 3


async def test_external_cancellation_still_propagates_without_a_synthetic_receipt(
    manager_observation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = manager_observation
    control["states"] = ["activating"]
    original = worker.asyncio.sleep

    async def sleep(seconds: float) -> None:
        await original(seconds)
        control["cancel"] = True

    monkeypatch.setattr(worker.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.manager_state(
            "manager-ready",
            control["user"],
            control["uid"],
            control["home"],
            175.0,
            worker.MAX_BYTES - 1024,
            started=99.0,
        )
    assert control["polls"] == 1 and len(control["calls"]) == 4 and len(control["files"]) == 1


@pytest.mark.parametrize("case", ["exact", "one-over", "late"])
def test_checkpoint_transport_preserves_original_packet_and_parent_deadline_gates(
    fixture: tuple[Any, Commands],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    native, commands = fixture
    native.prepare()
    clock = {"now": 100.0}
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock["now"])
    native.execute_deadline = 110.0
    summary = {
        "code": "native_deadline_exceeded",
        "stage": "manager_validation",
        "query_status": 0,
        "query_code": "ok",
        "used": 1,
        "last_completed_round": observed_round(),
    }
    wire = json.dumps(summary).encode().ljust(1024 + (case == "one-over"), b" ")
    before = native.setup_bytes

    def run(argv: Any, *, seconds: float = 5) -> Any:
        assert "manager-ready" in argv and seconds == 5.0
        if case == "late":
            clock["now"] = 105.0
        return ci.Reply(0, wire)

    native.run = run
    expected = {
        "exact": "native_deadline_exceeded",
        "one-over": "native_manager_observation",
        "late": "native_command_timeout",
    }[case]
    with pytest.raises(ci.FixtureError, match=f"^{expected}$"):
        native.observe_manager("manager-ready", started=99.0)
    if case == "exact":
        assert native.manager_failure == summary and native.setup_bytes == before + 1 + 1024
    else:
        assert not native.manager_failure and native.setup_bytes == ci.OUTPUT_BYTES


def test_actual_oversized_fallback_json_does_not_replace_saved_checkpoint(
    fixture: tuple[Any, Commands],
) -> None:
    native, commands = fixture
    native.prepare()
    native.receipt.setup_phase = "manager_show_before"
    summary = {
        "code": "native_deadline_exceeded",
        "stage": "manager_validation",
        "query_status": 0,
        "query_code": "ok",
        "used": 1,
        "last_completed_round": observed_round(),
    }
    native.manager_failure = summary
    native.save_setup_result({})
    before = native.setup_bytes
    returned = charged = calls = 0

    def run(argv: Any, *, seconds: float = 5) -> Any:
        nonlocal returned, charged, calls
        assert "diagnose-setup" in argv
        calls += 1
        reply = commands(argv, seconds=seconds)
        data = json.loads(reply.output)
        charged = data["used"] = int(argv[-1])
        output = json.dumps(data).encode().ljust(1025, b" ")
        returned = len(output)
        assert returned > 1024 and charged == ci.OUTPUT_BYTES - before - 1024
        return ci.Reply(0, output)

    native.run = run
    native.observe_setup(ci.FixtureError("native_deadline_exceeded"))
    saved = ci.fixture_result((native.root / "result.json").read_bytes())
    assert saved["resources"]["manager_failure"] == summary and calls == 1
    assert saved["resources"]["setup"]["diagnostic_code"] == "output_budget"
    assert native.setup_bytes == before + charged + returned > ci.OUTPUT_BYTES
    assert saved["timeline"] == [] and saved["generations"] == []
