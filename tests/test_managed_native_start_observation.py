"""Positive start boundaries on real small journals, without managers or processes."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import test_managed_backend as backend_fixture
from helpers import managed_native_worker as worker
from test_managed_native_ci import ci
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_helper_costs import _SlotCostClock, costs_result
from test_managed_native_helper_costs import slot_cost_observer as slot_cost_observer

from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.managed_transaction import TransactionRecordV2
from muli.platforms.linux_transaction import SystemdProbe, UnitSnapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated helper fixture")
BOUNDARIES = ("start_entered", "start_command_entered", "start_command_returned")


@dataclass
class StartObservation:
    host: backend_fixture.Host
    clock: _SlotCostClock
    record: TransactionRecordV2
    case: worker.Case
    probe: SystemdProbe
    argv: tuple[str, ...]
    checkpoints: list[dict[str, Any]]
    install: bytes


@pytest.fixture
async def start_observation(
    slot_cost_observer: tuple[
        backend_fixture.Host, _SlotCostClock, Callable[[], dict[str, Any] | None]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> StartObservation:
    host, clock, _ = slot_cost_observer
    installed = host.install_intent()
    await host.backend.install(installed, "target", host.data["target"])
    record = host.move("activating", "start_target")
    root = host.journal.directory.path.parents[2]
    install = (root / "helper-costs-checkpoint.json").read_bytes()
    helper = managed_helper_unit_name(record)
    argv = managed_helper_arguments(host.anchor, host.slots["base"])
    snapshot = replace(
        host.probe.snapshot.unit,
        name=helper,
        argv=argv,
        active_state="active",
        pid=os.getpid(),
        invocation="b" * 32,
        cgroup=f"/user.slice/user@{os.geteuid()}.service/{helper}",
    )

    class HelperProbe(SystemdProbe):
        async def helper_exists(self, name: str) -> bool:
            assert name == helper
            return True

        async def _unit_path(self, name: str) -> str:
            assert name == helper
            return "/fixed/helper"

        async def _snapshot(self, path: str) -> UnitSnapshot:
            assert path == "/fixed/helper"
            return snapshot

        async def _properties(
            self, path: str, interface: str, fields: tuple[tuple[str, str], ...]
        ) -> dict[str, Any]:
            assert path == "/fixed/helper" and interface == "Service"
            return {"Result": "success", "ExecMainStatus": 0}

    case = worker.Case(root)
    case.result["generations"] = [
        {
            "deployment": record.identity.deployment,
            "generation": record.generation,
            "helper": helper,
        }
    ]
    case.note(
        "transaction",
        generation=record.generation,
        revision=record.revision,
        phase=record.phase,
        intent=record.intent,
        code=record.code,
        failure_code=record.failure_code,
        rollback_attempted=record.rollback_attempted,
    )
    probe = HelperProbe()
    assert not await case.helper_empty(probe, helper, argv)
    checkpoints: list[dict[str, Any]] = []
    original_replace = Path.replace

    def capture(path: Path, target: Path) -> Path:
        result = original_replace(path, target)
        if target == root / "helper-start-checkpoint.json":
            checkpoints.append(worker.read_json(target))
        return result

    monkeypatch.setattr(Path, "replace", capture)
    return StartObservation(host, clock, record, case, probe, argv, checkpoints, install)


async def test_native_start_publishes_three_positive_boundaries_once(
    start_observation: StartObservation,
) -> None:
    observation = start_observation
    host, case, record = observation.host, observation.case, observation.record
    commands = len(host.commands)
    assert await host.backend.start(record, "target") is None
    assert [row["sample"]["kind"] for row in observation.checkpoints] == list(BOUNDARIES)
    assert host.commands[commands:] == ["start"]
    assert host.record() == record
    assert (case.root / "helper-costs-checkpoint.json").read_bytes() == observation.install
    source = case.result["resources"]["helper_source"]
    previous = -1
    for checkpoint in observation.checkpoints:
        sample = checkpoint["sample"]
        assert sample == {
            "kind": sample["kind"],
            "revision": record.revision,
            "phase": "activating",
            "intent": "start_target",
            "role": "target",
            "elapsed_ms": sample["elapsed_ms"],
        }
        assert sample["elapsed_ms"] >= previous
        previous = sample["elapsed_ms"]
        assert worker.helper_start_sample_facts(checkpoint, source)
    assert (
        observation.checkpoints[0]["costs"]["checks"]["count"]
        < (observation.checkpoints[-1]["costs"]["checks"]["count"])
    )
    case._collect_helper_cost_sample(record)
    case._collect_helper_start_sample(record)
    worker.collect_helper_costs(
        case.root,
        case.result,
        samples=case._helper_cost_samples,
        start_samples=case._helper_start_samples,
    )
    assert case.result["resources"]["helper_cost_samples"][0]["sample"]["kind"] == (
        "install_returned"
    )
    assert case.result["resources"]["helper_start_samples"] == [
        {"source": source, **observation.checkpoints[-1]}
    ]
    assert ci.fixture_result(json.dumps(case.result).encode()) == case.result


@pytest.mark.parametrize("stage", ["preflight", "preflight_cancel", "command", "command_cancel"])
async def test_native_start_failure_and_cancellation_never_forge_command_return(
    start_observation: StartObservation, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    observation = start_observation
    host = observation.host
    failure: BaseException = (
        asyncio.CancelledError("private failure")
        if stage.endswith("cancel")
        else RuntimeError("private failure")
    )
    calls: list[list[str]] = []

    async def command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        if not stage.startswith("preflight"):
            raise failure
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(host.backend, "runner", command)
    if stage.startswith("preflight"):

        def reject(*args: Any, **kwargs: Any) -> Any:
            raise failure

        monkeypatch.setattr(host.backend, "_desired", reject)
    with pytest.raises(type(failure)) as caught:
        await host.backend.start(observation.record, "target")
    assert caught.value is failure
    expected = BOUNDARIES[:1] if stage.startswith("preflight") else BOUNDARIES[:2]
    assert [row["sample"]["kind"] for row in observation.checkpoints] == list(expected)
    assert len(calls) == (0 if stage.startswith("preflight") else 1)
    assert host.record() == observation.record

    # The diagnostic context must end even when the original call is cancelled.
    async def succeeds(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(host.backend, "runner", succeeds)
    await host.backend._command("start")
    assert len(observation.checkpoints) == len(expected)


@pytest.mark.parametrize("damage", ["clock", "snapshot", "publish", "read"])
async def test_native_start_observer_errors_preserve_success(
    start_observation: StartObservation, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    observation = start_observation
    if damage == "clock":
        observation.clock.fail_next = True
    elif damage == "snapshot":

        def bad_snapshot(value: object, source: object) -> bool:
            raise RuntimeError("private observer failure")

        monkeypatch.setattr(worker, "helper_costs_facts", bad_snapshot)
    elif damage == "publish":
        original_replace = Path.replace

        def bad_publish(path: Path, target: Path) -> Path:
            if target.name == "helper-start-checkpoint.json":
                raise RuntimeError("private observer failure")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", bad_publish)
    else:

        def bad_read(path: Path, *, deadline: float | None) -> Any:
            raise RuntimeError("private observer failure")

        monkeypatch.setattr(worker, "_read_helper_cost_json", bad_read)
    commands = len(observation.host.commands)
    assert await observation.host.backend.start(observation.record, "target") is None
    assert observation.host.commands[commands:] == ["start"]
    observation.case._collect_helper_start_sample(observation.record)
    assert observation.case._helper_start_samples == []


@pytest.mark.parametrize("boundary", BOUNDARIES)
async def test_native_start_observer_cancellation_propagates_and_clears_context(
    start_observation: StartObservation, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    observation = start_observation
    failure = asyncio.CancelledError("private observer interruption")
    original_replace = Path.replace

    def interrupted(path: Path, target: Path) -> Path:
        if (
            target.name == "helper-start-checkpoint.json"
            and worker.read_json(path)["sample"]["kind"] == boundary
        ):
            raise failure
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupted)
    commands = len(observation.host.commands)
    with pytest.raises(asyncio.CancelledError) as caught:
        await observation.host.backend.start(observation.record, "target")
    assert caught.value is failure
    assert not any(
        row["sample"]["kind"] == "start_command_returned" for row in observation.checkpoints
    )
    assert observation.host.commands[commands:] == (
        ["start"] if boundary == "start_command_returned" else []
    )
    checkpoints = copy.deepcopy(observation.checkpoints)
    monkeypatch.setattr(Path, "replace", original_replace)

    async def succeeds(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(observation.host.backend, "runner", succeeds)
    await observation.host.backend._command("start")
    assert observation.checkpoints == checkpoints


@pytest.mark.parametrize("scope", ["other_backend", "other_task", "nested"])
async def test_native_start_command_scope_excludes_unrelated_or_nested_commands(
    start_observation: StartObservation, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    observation = start_observation
    backend = observation.host.backend
    failure = RuntimeError("actual outer command failed")
    calls = 0
    other = object.__new__(type(backend))
    other.probe, other.service = backend.probe, backend.service

    async def succeeds(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        return {"exit_code": 0, "output": ""}

    other.runner = succeeds

    async def command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls > 1:
            return {"exit_code": 0, "output": ""}
        if scope == "other_backend":
            await other._command("start")
        elif scope == "other_task":
            await asyncio.create_task(backend._command("start"))
        else:
            await backend._command("start")
        raise failure

    monkeypatch.setattr(backend, "runner", command)
    with pytest.raises(RuntimeError) as caught:
        await backend.start(observation.record, "target")
    assert caught.value is failure
    assert [row["sample"]["kind"] for row in observation.checkpoints] == list(BOUNDARIES[:2])
    assert calls == (1 if scope == "other_backend" else 2)


@pytest.mark.parametrize(
    "damage",
    [
        "generation",
        "helper",
        "uid",
        "pid",
        "invocation",
        "future_revision",
        "unknown_revision",
        "bool_revision",
        "phase",
        "intent",
        "role",
        "kind",
        "negative_elapsed",
        "extra",
    ],
)
async def test_native_start_collector_rejects_unbound_identity_or_revision_mode(
    start_observation: StartObservation, damage: str
) -> None:
    observation = start_observation
    await observation.host.backend.start(observation.record, "target")
    path = observation.case.root / "helper-start-checkpoint.json"
    value = worker.read_json(path)
    if damage in {"generation", "helper", "invocation"}:
        value["costs"][damage] = "e" * len(value["costs"][damage])
    elif damage in {"uid", "pid"}:
        value["costs"][damage] += 1
    elif damage == "future_revision":
        value["sample"]["revision"] += 1
    elif damage == "unknown_revision":
        value["sample"]["revision"] -= 1
    elif damage == "bool_revision":
        value["sample"]["revision"] = True
    elif damage in {"phase", "intent", "role", "kind"}:
        value["sample"][damage] = "unknown"
    elif damage == "negative_elapsed":
        value["sample"]["elapsed_ms"] = -1
    else:
        value["sample"]["private"] = "must not escape"
    worker.write_json(path, value)
    before = copy.deepcopy(observation.case.result)
    observation.case._collect_helper_start_sample(observation.record)
    assert observation.case._helper_start_samples == []
    assert observation.case.result == before


async def test_native_start_latest_boundary_never_moves_back_and_revocation_removes_it(
    start_observation: StartObservation,
) -> None:
    observation = start_observation
    await observation.host.backend.start(observation.record, "target")
    case = observation.case
    path = case.root / "helper-start-checkpoint.json"
    checkpoints = copy.deepcopy(observation.checkpoints)
    for checkpoint in reversed(checkpoints):
        worker.write_json(path, checkpoint)
        case._collect_helper_start_sample(observation.record)
        assert case._helper_start_samples == [
            {"source": case.result["resources"]["helper_source"], **checkpoints[-1]}
        ]
    case._invalid_helper_sources.add(observation.record.generation)
    case.result["resources"].pop("helper_source")
    case._collect_helper_start_sample(observation.record)
    assert case._helper_start_samples == []


async def test_native_start_new_rollback_revision_replaces_the_same_generation_only(
    start_observation: StartObservation,
) -> None:
    observation = start_observation
    host, case = observation.host, observation.case
    await host.backend.start(observation.record, "target")
    case._collect_helper_start_sample(observation.record)
    previous = copy.deepcopy(case._helper_start_samples)
    stopping = host.move("rolling_back", "stop_target", code="backend_failed")
    await host.backend.stop(stopping, "target")
    restoring = host.move("rolling_back", "restore_base", code="backend_failed")
    await host.backend.install(restoring, "base", host.data["base"])
    record = host.move("rolling_back", "start_base", code="backend_failed")
    case.note(
        "transaction",
        generation=record.generation,
        revision=record.revision,
        phase=record.phase,
        intent=record.intent,
        code=record.code,
        failure_code=record.failure_code,
        rollback_attempted=record.rollback_attempted,
    )
    await host.backend.start(record, "base")
    case._collect_helper_start_sample(record)
    retained = copy.deepcopy(case._helper_start_samples)
    assert len(retained) == 1
    assert retained[0]["sample"]["revision"] == record.revision
    assert retained[0]["sample"]["role"] == "base"
    assert retained[0]["sample"]["kind"] == "start_command_returned"
    worker.write_json(
        case.root / "helper-start-checkpoint.json",
        {key: previous[0][key] for key in ("sample", "costs")},
    )
    case._collect_helper_start_sample(record)
    assert case._helper_start_samples == retained
    worker.collect_helper_costs(case.root, case.result, start_samples=retained)
    assert ci.fixture_result(json.dumps(case.result).encode()) == case.result


@pytest.mark.parametrize("field", ["pid", "invocation"])
async def test_native_start_actual_source_revocation_immediately_removes_cached_samples(
    start_observation: StartObservation, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    observation = start_observation
    await observation.host.backend.start(observation.record, "target")
    case = observation.case
    case._collect_helper_start_sample(observation.record)
    assert case._helper_start_samples
    snapshot = await observation.probe._snapshot("/fixed/helper")
    changed = replace(
        snapshot, **({"pid": snapshot.pid + 1} if field == "pid" else {"invocation": "e" * 32})
    )

    async def drift(path: str) -> UnitSnapshot:
        return changed

    monkeypatch.setattr(observation.probe, "_snapshot", drift)
    assert not await case.helper_empty(
        observation.probe, managed_helper_unit_name(observation.record), observation.argv
    )
    assert case._helper_start_samples == []
    assert "helper_source" not in case.result["resources"]


async def test_native_start_existing_poll_collects_progress_without_a_new_revision(
    start_observation: StartObservation, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = start_observation
    await observation.host.backend.start(observation.record, "target")
    case, record = observation.case, observation.record
    path = case.root / "helper-start-checkpoint.json"
    worker.write_json(path, observation.checkpoints[0])
    rounds: list[str] = []
    identity_observations: list[str] = []

    class Finished(Exception):
        pass

    async def http(
        client: object, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        return (200, {"id": record.request_id}) if method == "POST" else (0, {})

    async def helper_empty(probe: SystemdProbe, name: str, argv: tuple[str, ...]) -> bool:
        identity_observations.append(name)
        return False  # Already authenticated by the real Case fixture above.

    async def pause() -> None:
        samples = getattr(case, "_helper_start_samples", [])
        assert samples, "existing poll lost progress at an unchanged journal revision"
        rounds.append(samples[0]["sample"]["kind"])
        if len(rounds) == 3:
            raise Finished
        worker.write_json(path, observation.checkpoints[len(rounds)])

    monkeypatch.setattr(case, "http", http)
    monkeypatch.setattr(case, "helper_empty", helper_empty)
    monkeypatch.setattr(case, "pause", pause)
    with pytest.raises(Finished):
        await case.transaction(
            None, "private", "switch", observation.host.slots["target"], "c" * 32
        )
    assert rounds == list(BOUNDARIES)
    assert len(identity_observations) == 1  # Sampling adds no bus observations.
    assert observation.host.record() == record


async def test_native_start_final_collection_reads_the_latest_fixed_file(
    start_observation: StartObservation, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = start_observation
    await observation.host.backend.start(observation.record, "target")

    async def drive(case: worker.Case) -> None:
        case.result = copy.deepcopy(observation.case.result)
        raise TimeoutError("private failure")

    monkeypatch.setattr(worker.Case, "drive", drive)
    monkeypatch.setenv(
        "MULI_NATIVE_EXECUTE_DEADLINE", str(worker.time.monotonic() + worker.HARNESS_SECONDS)
    )
    assert await worker.run("drive", observation.case.root) == 1
    result = worker.read_json(observation.case.root / "result.json")
    assert result["code"] == "TimeoutError" and result["completed"] is False
    assert result["resources"].get("helper_start_samples") == [
        {"source": result["resources"]["helper_source"], **observation.checkpoints[-1]}
    ]
    assert ci.fixture_result(json.dumps(result).encode()) == result


def _start_row(result: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    source = result["resources"]["helper_source"]
    result["timeline"] = [
        {
            "stage": "transaction",
            "elapsed_ms": 1,
            "generation": source["generation"],
            "revision": 5,
            "phase": "activating",
            "intent": "start_target",
            "code": None,
            "failure_code": None,
            "rollback_attempted": False,
        }
    ]
    return {
        "source": copy.deepcopy(source),
        "sample": {
            "kind": "start_command_returned",
            "revision": 5,
            "phase": "activating",
            "intent": "start_target",
            "role": "target",
            "elapsed_ms": 1,
        },
        "costs": copy.deepcopy(facts),
    }


def test_native_start_optional_budget_preserves_install_complete_costs_and_cleanup(
    tmp_path: Path,
) -> None:
    result, facts = costs_result(version=3)
    start = _start_row(result, facts)
    source = result["resources"]["helper_source"]
    result["resources"].update(
        helper_costs=copy.deepcopy(facts),
        helper_cost_history=[{"source": copy.deepcopy(source), "costs": copy.deepcopy(facts)}],
        helper_cost_samples=[
            {
                "source": copy.deepcopy(source),
                "sample": {"kind": "install_returned", "revision": 3},
                "costs": copy.deepcopy(facts),
            }
        ],
        cleanup="verified",
    )
    while len(result["timeline"]) < 64:
        result["timeline"].append({"stage": "transaction", "elapsed_ms": 1, "result": "x" * 100})
    for name in ("helper", "state", "request_id", "boot", "generation"):
        for row in result["timeline"][1:]:
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = worker.MAX_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                row[name] = "x" * min(100, capacity)
    wire = json.dumps(result, separators=(",", ":")).encode()
    assert worker.MAX_BYTES - 100 < len(wire) < worker.MAX_BYTES
    assert ci.fixture_result(wire) == result
    before = copy.deepcopy(result)
    worker.write_json(tmp_path / "helper-costs.json", facts)
    worker.collect_helper_costs(tmp_path, result, start_samples=[start])
    assert result == before


def test_native_start_manager_merge_drops_starts_before_install_or_complete_costs(
    fixture: tuple[Any, Any],
) -> None:
    native, _ = fixture
    native.prepare()
    result, facts = costs_result(version=3)
    start = _start_row(result, facts)
    source = result["resources"]["helper_source"]
    result["resources"].update(
        helper_costs=copy.deepcopy(facts),
        helper_cost_history=[{"source": copy.deepcopy(source), "costs": copy.deepcopy(facts)}],
        helper_cost_samples=[
            {
                "source": copy.deepcopy(source),
                "sample": {"kind": "install_returned", "revision": 3},
                "costs": copy.deepcopy(facts),
            }
        ],
        helper_start_samples=[start],
        cleanup="verified",
    )
    while len(result["timeline"]) < 64:
        result["timeline"].append({"stage": "transaction", "elapsed_ms": 1, "result": "x" * 100})
    for name in ("helper", "state", "request_id", "boot", "generation"):
        for row in result["timeline"][1:]:
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = ci.OUTPUT_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                row[name] = "x" * min(100, capacity)
    wire = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    assert ci.OUTPUT_BYTES - 100 < len(wire) < ci.OUTPUT_BYTES
    assert ci.fixture_result(wire) == result
    expected = copy.deepcopy(result)
    expected["resources"]["manager_readiness"] = copy.deepcopy(native.manager_readiness)
    assert len(json.dumps(expected, separators=(",", ":")).encode()) > ci.OUTPUT_BYTES
    expected["resources"].pop("helper_start_samples")
    assert ci.fixture_result(json.dumps(expected, separators=(",", ":")).encode()) == expected
    worker.write_json(native.volume / "result.json", result)
    assert native.collect_result() == expected


@pytest.mark.parametrize(
    "damage", ["future", "unknown", "mode", "source", "duplicate", "four", "extra"]
)
def test_native_start_artifact_parser_rejects_unknown_or_unbound_rows(damage: str) -> None:
    result, facts = costs_result(version=3)
    start = _start_row(result, facts)
    result["resources"]["helper_start_samples"] = [start]
    assert ci.fixture_result(json.dumps(result).encode()) == result
    if damage in {"future", "unknown"}:
        start["sample"]["revision"] += 1 if damage == "future" else -1
    elif damage == "mode":
        start["sample"]["role"] = "base"
    elif damage == "source":
        start["source"]["pid"] += 1
        start["costs"]["pid"] += 1
    elif damage in {"duplicate", "four"}:
        result["resources"]["helper_start_samples"] *= 2 if damage == "duplicate" else 4
    else:
        start["sample"]["private"] = "must not escape"
    with pytest.raises(ci.FixtureError, match="native_result_invalid"):
        ci.fixture_result(json.dumps(result).encode())
