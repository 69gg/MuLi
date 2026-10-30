"""Real Case/probe parsing across collection of an owned transient helper."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from helpers import managed_native_worker as worker
from test_linux_helper_probe import NAME, HelperBus, setup
from test_managed_native_ci import ci
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_helper_costs import costs_result

from muli import failure_diagnostics
from muli.platforms.linux_transaction import ObservationError, SystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux transient helper fixture")


class CollectionBus:
    def __init__(self, original: HelperBus) -> None:
        self.original = original
        self.window = ""
        self.recheck = "empty"
        self.listings = 0
        self.lookups = 0
        self.failed = False
        self.calls: list[list[str]] = []

    def arm(self, window: str, recheck: str = "empty") -> None:
        self.window, self.recheck = window, recheck
        self.listings = self.lookups = 0
        self.failed = False
        self.calls.clear()

    async def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        self.calls.append(argv)
        if "ListUnitsByPatterns" in argv:
            assert argv[-4:] == ["asas", "0", "1", NAME]
            self.listings += 1
            if self.failed:
                if self.recheck == "cancel":
                    raise asyncio.CancelledError
                if self.recheck == "failed":
                    return {"exit_code": 1, "output": "", "stderr": "not exported"}
                if self.recheck == "malformed":
                    return {"exit_code": 0, "output": "not typed JSON"}
                if self.recheck == "empty":
                    return {
                        "exit_code": 0,
                        "output": json.dumps({"type": "a(ssssssouso)", "data": [[]]}),
                    }
                if self.recheck == "wrong_name":
                    listing = copy.deepcopy(self.original.listing)
                    assert isinstance(listing, list) and isinstance(listing[0], list)
                    listing[0][0][0] = "muli-transaction-" + "b" * 64 + ".service"
                    return {
                        "exit_code": 0,
                        "output": json.dumps({"type": "a(ssssssouso)", "data": listing}),
                    }
        if "GetUnit" in argv:
            self.lookups += 1
        at_window = (
            self.window == "lookup"
            and "GetUnit" in argv
            and self.lookups == 1
            or self.window == "snapshot_service"
            and "get-property" in argv
            and "MainPID" in argv
            or self.window == "details"
            and "get-property" in argv
            and "ExecMainStatus" in argv
        )
        if self.window and not self.failed and at_window:
            self.failed = True
            return {
                "exit_code": 1,
                "output": "",
                "stderr": f"Call failed: Unit {NAME} not loaded.\n",
                "truncated": False,
            }
        return await self.original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)


@dataclass(slots=True)
class CollectionCase:
    case: worker.Case
    probe: SystemdProbe
    bus: CollectionBus
    argv: tuple[str, ...]
    source: dict[str, Any]


@pytest.fixture
async def collection(tmp_path: Path) -> CollectionCase:
    probe, original, argv, _, _ = setup(tmp_path)
    original.rows.update(
        ControlGroup=("s", f"/user.slice/user@{os.geteuid()}.service/{NAME}"),
        Result=("s", "success"),
        ExecMainStatus=("i", 0),
    )
    original.listing = [
        [
            [
                NAME,
                "",
                "loaded",
                "active",
                "running",
                "",
                "/org/freedesktop/systemd1/unit/helper",
                0,
                "",
                "/",
            ]
        ]
    ]
    bus = CollectionBus(original)
    probe.runner = bus
    root = tmp_path / "case"
    root.mkdir()
    case = worker.Case(root)
    case.result["generations"] = [
        {
            "deployment": "d" * 64,
            "generation": "a" * 32,
            "helper": NAME,
        }
    ]
    # This positive control uses the actual Case and typed SystemdProbe parser
    # to bind source identity before any collection race is injected.
    assert not await case.helper_empty(probe, NAME, argv)
    source = copy.deepcopy(case.result["resources"]["helper_source"])
    assert source["helper"] == NAME and source["pid"] == 123
    assert source["uid"] == os.geteuid()
    return CollectionCase(case, probe, bus, argv, source)


@pytest.mark.parametrize("window", ["lookup", "snapshot_service", "details"])
async def test_actual_helper_collection_requires_a_fresh_exact_empty_listing(
    collection: CollectionCase, window: str
) -> None:
    collection.bus.arm(window)
    assert await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.failed and collection.bus.listings == 2
    assert collection.case.result["resources"]["helper_source"] == collection.source
    final = collection.case.result["timeline"][-1]
    assert final["stage"] == "helper_exit" and final["state"] == "collected"
    assert final["helper"] == NAME
    assert final["observation"] == "absent_after_present"


async def test_prepared_helper_absence_does_not_claim_a_start_or_exit(
    collection: CollectionCase, tmp_path: Path
) -> None:
    root = tmp_path / "sequence"
    root.mkdir()
    case = worker.Case(root)
    case.result["generations"] = copy.deepcopy(collection.case.result["generations"])
    case.note(
        "transaction",
        request_id="b" * 32,
        generation="a" * 32,
        phase="prepared",
        activation="pending",
        revision=1,
        intent="none",
        code=None,
        failure_code=None,
        rollback_attempted=False,
    )
    listing = collection.bus.original.listing
    collection.bus.original.listing = [[]]
    assert await case.helper_empty(collection.probe, NAME, collection.argv)
    assert case.result["timeline"][-1]["observation"] == "never_observed"
    assert "helper_source" not in case.result["resources"]

    collection.bus.original.listing = listing
    assert not await case.helper_empty(collection.probe, NAME, collection.argv)
    present = case.result["timeline"][-1]
    assert present["state"] == "active" and present["observation"] == "present"
    source = copy.deepcopy(case.result["resources"]["helper_source"])

    collection.bus.original.listing = [[]]
    assert await case.helper_empty(collection.probe, NAME, collection.argv)
    observations = [
        row["observation"] for row in case.result["timeline"] if row["stage"] == "helper_exit"
    ]
    assert observations == ["never_observed", "present", "absent_after_present"]
    assert case.result["resources"]["helper_source"] == source
    assert case.result["completed"] is False
    assert ci.fixture_result(json.dumps(case.result).encode()) == case.result

    legacy = copy.deepcopy(case.result)
    for row in legacy["timeline"]:
        row.pop("observation", None)
    assert ci.fixture_result(json.dumps(legacy).encode()) == legacy


@pytest.mark.parametrize(
    ("stage", "state", "observation"),
    [
        ("helper_exit", "collected", "present"),
        ("helper_exit", "active", "never_observed"),
        ("helper_exit", "active", "absent_after_present"),
        ("helper_exit", "collected", "unknown"),
        ("transaction", "collected", "never_observed"),
    ],
)
def test_helper_observation_rejects_unknown_or_conflicting_semantics(
    tmp_path: Path, stage: str, state: str, observation: str
) -> None:
    case = worker.Case(tmp_path)
    with pytest.raises(worker.NativeError, match="^native_fact_invalid$"):
        case.note(stage, helper=NAME, state=state, observation=observation)
    case.result["timeline"] = [
        {
            "elapsed_ms": 0,
            "stage": stage,
            "helper": NAME,
            "state": state,
            "observation": observation,
        }
    ]
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(case.result).encode())


@pytest.mark.parametrize("recheck", ["exists", "failed", "malformed", "wrong_name", "cancel"])
async def test_failed_helper_observation_never_treats_an_unproved_listing_as_absence(
    collection: CollectionCase, recheck: str
) -> None:
    collection.bus.arm("lookup", recheck)
    expected = asyncio.CancelledError if recheck == "cancel" else ObservationError
    with pytest.raises(expected) as caught:
        await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    if recheck != "cancel":
        assert caught.value.args == ("systemd_observation_unavailable",)
    assert collection.bus.listings == 2
    assert collection.case.result["resources"]["helper_source"] == collection.source
    assert collection.case.result["timeline"][-1]["state"] == "active"


async def test_malformed_snapshot_cannot_be_reclassified_as_collected(
    collection: CollectionCase,
) -> None:
    collection.bus.arm("disabled")
    collection.bus.original.rows["InvocationID"] = ("ay", [0])
    with pytest.raises(ObservationError, match="^invalid_invocation$"):
        await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.listings == 1
    assert collection.case.result["resources"]["helper_source"] == collection.source


@pytest.mark.parametrize("shape", ["empty", "two-entries", "non-list"])
async def test_unsupported_exec_start_cannot_be_reclassified_as_collected(
    collection: CollectionCase, shape: str
) -> None:
    collection.bus.arm("disabled")
    signature, valid_entries = collection.bus.original.rows["ExecStart"]
    assert isinstance(valid_entries, list) and len(valid_entries) == 1
    entries: object
    if shape == "empty":
        entries = []
    elif shape == "two-entries":
        entries = [copy.deepcopy(valid_entries[0]), copy.deepcopy(valid_entries[0])]
    else:
        entries = None
    # Retain the accepted typed envelope while the real parser rejects its data.
    collection.bus.original.rows["ExecStart"] = (signature, entries)
    source = collection.case.result["resources"]["helper_source"]
    timeline = copy.deepcopy(collection.case.result["timeline"])
    with pytest.raises(ObservationError, match="^unsupported_exec_start$") as caught:
        await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert caught.value.args == ("unsupported_exec_start",)
    assert not collection.bus.failed and collection.bus.listings == 1
    assert collection.bus.lookups == 1 and len(collection.bus.calls) == 4
    assert collection.case.result["resources"]["helper_source"] is source
    assert source == collection.source
    assert collection.case.result["timeline"] == timeline
    assert collection.case.result["completed"] is False


async def test_helper_argv_drift_keeps_original_source_revocation_and_error(
    collection: CollectionCase,
) -> None:
    collection.bus.arm("disabled")
    entries = copy.deepcopy(collection.bus.original.rows["ExecStart"][1])
    entries[0][1] = ["foreign-controller"]
    collection.bus.original.rows["ExecStart"] = ("a(sasbttttuii)", entries)
    with pytest.raises(worker.NativeError, match="^native_helper_identity$"):
        await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.listings == 1
    assert "helper_source" not in collection.case.result["resources"]


async def test_collection_after_later_rpc_failure_never_restores_a_revoked_source(
    collection: CollectionCase,
) -> None:
    collection.bus.arm("details")
    collection.bus.original.rows["InvocationID"] = ("ay", [2] * 16)
    assert await collection.case.helper_empty(collection.probe, NAME, collection.argv)
    assert collection.bus.failed and collection.bus.listings == 2
    assert collection.source["generation"] in collection.case._invalid_helper_sources
    assert "helper_source" not in collection.case.result["resources"]
    assert collection.case.result["timeline"][-1]["state"] == "collected"


@pytest.mark.parametrize("extractor_fails", [False, True], ids=["safe-frames", "extractor-failure"])
async def test_worker_reports_owned_probe_frames_without_exporting_private_failure_data(
    collection: CollectionCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extractor_fails: bool,
) -> None:
    secret = "private-password-not-for-export"
    collection.bus.original.rows["InvocationID"] = ("ay", [0])

    async def drive(case: worker.Case) -> None:
        case.note("helper_exit")
        try:
            raise RuntimeError(secret + str(tmp_path))
        except RuntimeError:
            # Keep a private chained exception while the real typed probe
            # rejects the malformed snapshot inside a package-owned frame.
            await case.helper_empty(collection.probe, NAME, collection.argv)

    if extractor_fails:

        def fail_capture(error: BaseException) -> failure_diagnostics.FailureDiagnostic:
            raise RuntimeError(secret + str(tmp_path))

        monkeypatch.setattr(failure_diagnostics, "capture_failure", fail_capture)
    monkeypatch.setattr(worker.Case, "drive", drive)
    monkeypatch.setenv(
        "MULI_NATIVE_EXECUTE_DEADLINE",
        str(worker.time.monotonic() + worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS),
    )
    assert await worker.run("drive", tmp_path) == 1
    persisted = (tmp_path / "result.json").read_text()
    result = json.loads(persisted)
    assert result["code"] == "ObservationError" and result["completed"] is False
    assert "failure_diagnostic" not in result
    stderr = capsys.readouterr().err
    assert "native_drive_failed:helper_exit:ObservationError" in stderr
    assert secret not in stderr + persisted and str(tmp_path) not in stderr + persisted
    prefix = "native_failure_diagnostic:"
    lines = [line.removeprefix(prefix) for line in stderr.splitlines() if line.startswith(prefix)]
    assert len(lines) == (0 if extractor_fails else 1)
    if lines:
        assert len(lines[0].encode("utf-8")) <= failure_diagnostics.MAX_FAILURE_BYTES
        diagnostic = failure_diagnostics.validate_failure_diagnostic(json.loads(lines[0]))
        assert diagnostic is not None
        assert any(
            frame["module"] == "muli.platforms.linux_transaction"
            and frame["function"] == "SystemdProbe._snapshot"
            and type(frame["line"]) is int
            for frame in diagnostic["frames"]
        )


@pytest.mark.parametrize("location", ["poll", "helper_snapshot"])
async def test_outer_cancel_records_real_wait_despite_stale_helper_exit_stage(
    collection: CollectionCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    entered = asyncio.Event()
    cancelled: list[asyncio.CancelledError] = []
    timelines: list[list[dict[str, Any]]] = []
    outer: asyncio.Timeout | None = None
    original_timeout = asyncio.timeout

    def timeout(seconds: float | None) -> asyncio.Timeout:
        nonlocal outer
        if outer is None:
            outer = original_timeout(None)
            return outer
        return original_timeout(seconds)

    async def sleep(seconds: float) -> None:
        # Expire the actual outer asyncio timeout at the real poll await,
        # without spending 60 seconds or changing any fixture budget constant.
        assert outer is not None
        outer.reschedule(asyncio.get_running_loop().time())
        await asyncio.Future[None]()

    async def query(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        if "MainPID" in argv:
            entered.set()
            await asyncio.Future[None]()
        return await collection.bus(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    async def drive(case: worker.Case) -> None:
        case.note("helper_exit")
        timelines.append(copy.deepcopy(case.result["timeline"]))
        try:
            if location == "poll":
                with case.await_operation("http"):
                    await case.pause()
            else:
                await case.helper_empty(collection.probe, NAME, collection.argv)
        except asyncio.CancelledError as error:
            cancelled.append(error)
            assert case._cancelled_await is not None
            assert case._cancelled_await[0] is error
            raise

    monkeypatch.setattr(worker.Case, "drive", drive)
    monkeypatch.setenv(
        "MULI_NATIVE_EXECUTE_DEADLINE",
        str(worker.time.monotonic() + worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS),
    )
    if location == "poll":
        monkeypatch.setattr(asyncio, "timeout", timeout)
        monkeypatch.setattr(asyncio, "sleep", sleep)
        assert await worker.run("drive", tmp_path) == 1
    else:
        collection.bus.arm("disabled")
        monkeypatch.setattr(collection.probe, "runner", query)
        task = asyncio.create_task(worker.run("drive", tmp_path))
        try:
            async with asyncio.timeout(worker.PROBE_SECONDS):
                await entered.wait()
                task.cancel("private cancellation reason")
                assert await task == 1
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert collection.bus.listings == 1 and collection.bus.lookups == 1
    result = ci.fixture_result((tmp_path / "result.json").read_bytes())
    assert len(cancelled) == 1
    assert result["stage"] == "helper_exit" and result["completed"] is False
    assert result["code"] == ("TimeoutError" if location == "poll" else "CancelledError")
    assert result["timeline"] == timelines[0]
    facts = result["resources"]["worker_await"]
    assert worker.worker_await_facts(facts) and facts["operation"] == location
    assert "private" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


async def test_worker_wait_normal_return_errors_and_unrequested_cancel_export_no_location(
    tmp_path: Path,
) -> None:
    case = worker.Case(tmp_path)
    token = object()

    async def value() -> object:
        await asyncio.sleep(0)
        return token

    with case.await_operation("http"), case.await_operation("managed_read"):
        observed = await value()
    assert observed is token and case._cancelled_await is None
    for error in (RuntimeError("private failure"), asyncio.CancelledError("not a task cancel")):
        with pytest.raises(type(error)) as caught, case.await_operation("http"):
            raise error
        assert caught.value is error
        case.record_worker_await(error)
        assert "worker_await" not in case.result["resources"]
    assert case._worker_await_depth == 0


async def test_http_own_timeout_is_not_an_outer_worker_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WaitingClient:
        @asynccontextmanager
        async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[None]:
            await asyncio.Future[None]()
            yield None

    case = worker.Case(tmp_path)
    monkeypatch.setattr(worker, "HTTP_SECONDS", 0)
    with pytest.raises(TimeoutError) as caught:
        await case.http(WaitingClient(), "GET", "/private-path")
    case.record_worker_await(caught.value)
    assert case._cancelled_await is None
    assert "worker_await" not in case.result["resources"]


def _cancelled_worker_wait() -> dict[str, Any]:
    return {
        "version": 1,
        "operation": "poll",
        "outcome": "cancelled",
        "started_ns": 1,
        "cancelled_ns": 2,
        "deadline_ns": worker.CASE_SECONDS * 10**9,
    }


def _encode_result(result: dict[str, Any]) -> bytes:
    return json.dumps(result, sort_keys=True, separators=(",", ":")).encode()


def _padded_result(original: dict[str, Any], size: int) -> dict[str, Any]:
    result = copy.deepcopy(original)
    rows = [
        {
            "stage": "helper_exit",
            "elapsed_ms": 0,
            "helper": "a" * 100,
            "boot": "b" * 100,
            "request_id": "c" * 100,
            "result": "d" * 100,
            "state": "",
            "phase": "",
            "activation": "",
        }
        for _ in range(50)
    ]
    result["timeline"].extend(rows)
    padding = size - len(_encode_result(result))
    assert 0 <= padding <= len(rows) * 3 * 100
    for row in rows:
        for key in ("state", "phase", "activation"):
            width = min(padding, 100)
            row[key] = "a" * width
            padding -= width
    assert padding == 0 and len(_encode_result(result)) == size
    assert ci.fixture_result(_encode_result(result)) == result
    return result


@pytest.mark.parametrize(
    "damage",
    [
        {"operation": "private/path"},
        {"outcome": "returned"},
        {"started_ns": True},
        {"cancelled_ns": 0},
        {"cancelled_ns": (worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS) * 10**9 + 1},
        {"deadline_ns": worker.CASE_SECONDS * 10**9 + 1},
        {"payload": "private"},
    ],
)
def test_worker_cancel_location_rejects_unknown_fields_types_and_times(
    tmp_path: Path, damage: dict[str, Any]
) -> None:
    case = worker.Case(tmp_path)
    case.result["code"] = "TimeoutError"
    facts = {**_cancelled_worker_wait(), **damage}
    assert not worker.worker_await_facts(facts)
    case.result["resources"]["worker_await"] = facts
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(case.result).encode())


def test_worker_cancel_location_requires_an_uncompleted_cancellation_result(tmp_path: Path) -> None:
    case = worker.Case(tmp_path)
    case.result["resources"]["worker_await"] = _cancelled_worker_wait()
    for completed, code in ((True, "TimeoutError"), (False, ""), (False, "ObservationError")):
        case.result.update(completed=completed, code=code)
        with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
            ci.fixture_result(json.dumps(case.result).encode())


@pytest.mark.parametrize(
    "diagnostic",
    [
        "helper_costs",
        "helper_cost_history",
        "helper_cost_samples",
        "helper_start_samples",
        "helper_source_observation",
        "helper_entry_checkpoint",
    ],
)
def test_worker_cancel_location_yields_before_real_helper_cost_collection_budget(
    tmp_path: Path, diagnostic: str
) -> None:
    original, facts = costs_result(version=3)
    source = copy.deepcopy(original["resources"]["helper_source"])
    original["resources"]["cleanup"] = "verified"
    original["timeline"] = [
        {
            "stage": "transaction",
            "elapsed_ms": 1,
            "generation": source["generation"],
            "revision": 3,
            "phase": "activating",
            "intent": "start_target",
            "code": None,
            "failure_code": None,
            "rollback_attempted": False,
        }
    ]
    retained: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    starts: list[dict[str, Any]] = []
    row = {"source": source, "costs": copy.deepcopy(facts)}
    if diagnostic == "helper_cost_history":
        retained.append(row)
    elif diagnostic == "helper_cost_samples":
        samples.append({**row, "sample": {"kind": "install_returned", "revision": 3}})
    elif diagnostic == "helper_start_samples":
        starts.append(
            {
                **row,
                "sample": {
                    "kind": "start_command_entered",
                    "revision": 3,
                    "phase": "activating",
                    "intent": "start_target",
                    "role": "target",
                    "elapsed_ms": 1,
                },
            }
        )
    elif diagnostic in {"helper_source_observation", "helper_entry_checkpoint"}:
        original["resources"]["helper_source_observation"] = {
            "source": copy.deepcopy(source),
            "kind": "failure_collection",
            "started_ns": 10**9,
            "observed_ns": 61 * 10**9,
            "case_deadline_ns": 61 * 10**9,
            "elapsed_ms": 60_000,
        }
        if diagnostic == "helper_entry_checkpoint":
            original["resources"]["helper_entry_checkpoint"] = {
                "version": 1,
                **source,
                "revision": 3,
                "boundary": "read_helper_dispatch",
                "monotonic_ns": 60 * 10**9,
                "outcome": "progress",
            }
    worker.write_json(tmp_path / "helper-costs.json", facts)
    expected = copy.deepcopy(original)
    expected["resources"]["helper_costs"] = copy.deepcopy(facts)
    for name, rows in (
        ("helper_cost_history", retained),
        ("helper_cost_samples", samples),
        ("helper_start_samples", starts),
    ):
        if rows:
            expected["resources"][name] = copy.deepcopy(rows)
    expected = _padded_result(expected, worker.MAX_BYTES - 1)
    original["timeline"] = copy.deepcopy(expected["timeline"])

    # The same private cost file and existing facts fit without the new field.
    baseline = copy.deepcopy(original)
    worker.collect_helper_costs(
        tmp_path, baseline, retained=retained, samples=samples, start_samples=starts
    )
    assert baseline == expected
    assert ci.fixture_result(_encode_result(baseline)) == baseline

    candidate = copy.deepcopy(original)
    candidate["resources"]["worker_await"] = _cancelled_worker_wait()
    assert len(_encode_result(candidate)) <= worker.MAX_BYTES
    assert ci.fixture_result(_encode_result(candidate)) == candidate
    enlarged = copy.deepcopy(expected)
    enlarged["resources"]["worker_await"] = _cancelled_worker_wait()
    assert len(_encode_result(enlarged)) > worker.MAX_BYTES
    worker.collect_helper_costs(
        tmp_path, candidate, retained=retained, samples=samples, start_samples=starts
    )
    assert candidate["resources"].get(diagnostic) == expected["resources"][diagnostic]
    assert candidate["resources"].get("helper_source") == source
    assert candidate["resources"]["helper_costs"] == facts
    assert candidate["code"] == "TimeoutError" and candidate["timeline"] == original["timeline"]
    assert candidate["resources"]["cleanup"] == "verified"
    assert "worker_await" not in candidate["resources"]
    assert candidate == expected
    worker.write_json(tmp_path / "collected-result.json", candidate)
    saved = (tmp_path / "collected-result.json").read_bytes()
    assert len(saved) <= worker.MAX_BYTES and ci.fixture_result(saved) == expected


def test_worker_cancel_location_survives_helper_cost_collection_with_space(tmp_path: Path) -> None:
    result, facts = costs_result(version=3)
    result["resources"]["worker_await"] = _cancelled_worker_wait()
    worker.write_json(tmp_path / "helper-costs.json", facts)
    worker.collect_helper_costs(tmp_path, result)
    assert result["resources"]["helper_costs"] == facts
    assert result["resources"]["worker_await"] == _cancelled_worker_wait()
    assert ci.fixture_result(_encode_result(result)) == result


def test_worker_cancel_location_yields_to_original_result_and_collector_merge_budget(
    tmp_path: Path, fixture: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, _ = fixture
    native.root.mkdir(mode=0o700)
    native.receipt = ci.Receipt("muli-ci-" + "1" * 12)
    case = worker.Case(tmp_path)
    case.note("helper_exit")
    case.result["code"] = "TimeoutError"
    facts = _cancelled_worker_wait()

    original = _padded_result(case.result, worker.MAX_BYTES - 1)
    candidate = copy.deepcopy(original)
    candidate["resources"]["worker_await"] = facts
    worker.write_json(tmp_path / "worker-result.json", candidate)
    assert ci.fixture_result((tmp_path / "worker-result.json").read_bytes()) == original

    small = copy.deepcopy(case.result)
    small["resources"]["worker_await"] = facts
    monkeypatch.setattr(native, "call", lambda argv: ci.Reply(0, _encode_result(small)))
    assert native.collect_result() == small
    delta = len(_encode_result(small)) - len(_encode_result(case.result))
    original = _padded_result(case.result, ci.OUTPUT_BYTES - delta - 1)
    candidate = copy.deepcopy(original)
    candidate["resources"]["worker_await"] = facts
    assert len(_encode_result(candidate)) == ci.OUTPUT_BYTES - 1
    monkeypatch.setattr(native, "call", lambda argv: ci.Reply(0, _encode_result(candidate)))
    native.manager_readiness = {"elapsed_ms": 1, "late_ready": False}
    merged = native.collect_result()
    expected = copy.deepcopy(original)
    expected["resources"]["manager_readiness"] = native.manager_readiness
    assert merged == expected
    saved = (native.root / "result.json").read_bytes()
    assert len(saved) <= ci.OUTPUT_BYTES and ci.fixture_result(saved) == expected
