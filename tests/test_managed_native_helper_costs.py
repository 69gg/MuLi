"""Offline startup-hook observation; no interpreter or manager is launched."""

from __future__ import annotations

import atexit
import copy
import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import test_managed_backend as backend_fixture
from helpers import managed_native_worker as worker
from test_managed_native_ci import ci
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects

from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.management_transaction import TransactionError, TransactionJournal
from muli.platforms.linux_transaction import SystemdProbe, UnitSnapshot
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated helper fixture")


async def test_generated_managed_helper_hook_measures_real_checks_without_changing_their_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: tuple[Any, Any]
) -> None:
    native, _ = fixture
    native.prepare()
    hook = (native.volume / "_muli_native_ci_hook.py").read_text(encoding="utf-8")
    root = tmp_path / "observed-helper"
    root.mkdir(mode=0o700)
    original_slot = backend_fixture._slot

    def slot(directory: Path, name: str) -> SlotIdentity:
        return original_slot(directory, {"base": "a", "target": "b"}[name])

    monkeypatch.setattr(backend_fixture, "_slot", slot)
    callbacks: list[Callable[[], Any]] = []

    def register(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[..., Any]:
        callbacks.append(partial(function, *args, **kwargs))
        return function

    # The generated .pth import sees this same fixture module. This does not
    # claim to execute Python -I, PAM, cgroups, or a real systemd helper.
    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    for owner, name in (
        (ManagedLinuxBackend, "_check"),
        (ManagedLinuxBackend, "_request"),
        (ManagedLinuxBackend, "install"),
        (SystemdProbe, "_command"),
    ):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    fixture_data = {
        "uid": os.geteuid(),
        "memory": worker.MEMORY,
        "tasks": worker.TASKS,
        "harness_memory": worker.HARNESS_MEMORY,
        "harness_tasks": worker.HARNESS_TASKS,
        "seconds": worker.CASE_SECONDS,
        "cleanup_seconds": worker.CLEANUP_SECONDS,
        "harness_seconds": worker.HARNESS_SECONDS,
        "http_bytes": worker.MAX_BYTES,
        "harness_unit": "muli-native-" + "a" * 32 + ".service",
    }
    (root / "fixture.json").write_text(json.dumps(fixture_data), encoding="utf-8")
    (root / "fixture.json").chmod(0o600)
    invocation = "b" * 32
    monkeypatch.setenv("INVOCATION_ID", invocation)
    journal_root = root / "data/management/transaction-v1"
    with TransactionJournal(journal_root, root / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = backend_fixture.Host(root, journal, control, "adopt", change_config=False)
            try:
                record = host.record()
                helper_argv = managed_helper_arguments(host.anchor, host.slots["base"])
                executable = helper_argv[0]
                monkeypatch.setattr(sys, "prefix", str(root / "a"))
                monkeypatch.setattr(sys, "executable", executable)
                monkeypatch.setattr(
                    sys,
                    "orig_argv",
                    list(helper_argv),
                )
                monkeypatch.setattr(sys, "argv", list(helper_argv[3:]))
                monkeypatch.setattr(atexit, "register", register)
                exec(compile(hook, "<fixture-helper-hook>", "exec"), {})
                assert host.backend._check(record) is None
                stale = record.model_copy(update={"revision": record.revision + 1})
                with pytest.raises(TransactionError, match="^stale_revision$"):
                    host.backend._check(stale)
                path = root / "helper-costs.json"
                assert not path.exists()  # No per-check filesystem observation.
                assert len(callbacks) == 1
                callbacks[0]()
                callbacks[0]()  # The same atexit callback is a single write attempt.
                facts = json.loads(path.read_text(encoding="utf-8"))
                assert set(facts) == {
                    "version",
                    "code",
                    "generation",
                    "helper",
                    "uid",
                    "pid",
                    "invocation",
                    "checks",
                    "bus",
                    "http",
                }
                assert facts["version"] == 1 and facts["code"] == "observed"
                assert facts["generation"] == record.generation
                assert facts["helper"] == managed_helper_unit_name(record)
                assert (facts["uid"], facts["pid"], facts["invocation"]) == (
                    os.geteuid(),
                    os.getpid(),
                    invocation,
                )
                assert facts["checks"]["count"] == 2
                for name in ("checks", "bus", "http"):
                    assert set(facts[name]) == {"count", "total_ms"}
                    assert all(type(value) is int and value >= 0 for value in facts[name].values())
                assert facts["bus"]["count"] == facts["http"]["count"] == 0
                assert path.stat().st_mode & 0o077 == 0
            finally:
                host.release_writer()
                await host.backend.__aexit__()


@pytest.mark.parametrize("install_succeeds", [True, False])
async def test_real_install_publishes_second_helper_partial_before_atexit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    install_succeeds: bool,
) -> None:
    from test_managed_install_confirmation import expected_confirmation

    native, _ = fixture
    native.prepare()
    hook = (native.volume / "_muli_native_ci_hook.py").read_text(encoding="utf-8")
    root = tmp_path / "partial-helper"
    root.mkdir(mode=0o700)
    original_slot = backend_fixture._slot

    def slot(directory: Path, name: str) -> SlotIdentity:
        return original_slot(directory, {"base": "a", "target": "b"}[name])

    monkeypatch.setattr(backend_fixture, "_slot", slot)
    fixture_data = {
        "uid": os.geteuid(),
        "memory": worker.MEMORY,
        "tasks": worker.TASKS,
        "harness_memory": worker.HARNESS_MEMORY,
        "harness_tasks": worker.HARNESS_TASKS,
        "seconds": worker.CASE_SECONDS,
        "cleanup_seconds": worker.CLEANUP_SECONDS,
        "harness_seconds": worker.HARNESS_SECONDS,
        "http_bytes": worker.MAX_BYTES,
        "harness_unit": "muli-native-" + "a" * 32 + ".service",
    }
    worker.write_json(root / "fixture.json", fixture_data)
    callbacks: list[Callable[[], Any]] = []

    def register(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[..., Any]:
        callbacks.append(partial(function, *args, **kwargs))
        return function

    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    for owner, name in (
        (ManagedLinuxBackend, "_check"),
        (ManagedLinuxBackend, "_request"),
        (ManagedLinuxBackend, "install"),
        (SystemdProbe, "_command"),
    ):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    invocation = "b" * 32
    monkeypatch.setenv("INVOCATION_ID", invocation)
    journal_root = root / "data/management/transaction-v1"
    with TransactionJournal(journal_root, root / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = backend_fixture.Host(root, journal, control, "switch", change_config=True)
            try:
                record = host.install_intent()
                helper = managed_helper_unit_name(record)
                argv = managed_helper_arguments(host.anchor, host.slots["base"])
                monkeypatch.setattr(sys, "prefix", str(root / "a"))
                monkeypatch.setattr(sys, "executable", argv[0])
                monkeypatch.setattr(sys, "orig_argv", list(argv))
                monkeypatch.setattr(sys, "argv", list(argv[3:]))
                monkeypatch.setattr(atexit, "register", register)
                exec(compile(hook, "<fixture-helper-hook>", "exec"), {})
                assert len(callbacks) == 1

                case = worker.Case(root)
                # The earlier generation is a typed reader fixture only; this
                # test executes the second helper's real install, not two daemons.
                earlier, final_costs = costs_result()
                case.result["generations"] = [
                    *earlier["generations"],
                    {
                        "deployment": record.identity.deployment,
                        "generation": record.generation,
                        "helper": helper,
                    },
                ]
                worker.write_json(root / "helper-costs.json", final_costs)
                snapshot = replace(
                    host.probe.snapshot.unit,
                    name=helper,
                    argv=argv,
                    active_state="active",
                    pid=os.getpid(),
                    invocation=invocation,
                    cgroup=f"/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/{helper}",
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
                        assert fields == (("Result", "s"), ("ExecMainStatus", "i"))
                        return {"Result": "success", "ExecMainStatus": 0}

                # Source identity is established by the original Case path,
                # never copied out of the observer's self-reported JSON.
                assert not await case.helper_empty(HelperProbe(), helper, argv)
                source = copy.deepcopy(case.result["resources"]["helper_source"])
                assert source == {
                    "generation": record.generation,
                    "helper": helper,
                    "uid": os.geteuid(),
                    "pid": os.getpid(),
                    "invocation": invocation,
                }
                if not install_succeeds:
                    failure = TransactionError("controlled_install_failure")
                    original_confirm = host.backend._confirm

                    def failed_confirm(*args: Any, **kwargs: Any) -> Any:
                        original_confirm(*args, **kwargs)
                        raise failure

                    monkeypatch.setattr(host.backend, "_confirm", failed_confirm)
                    with pytest.raises(TransactionError) as caught:
                        await host.backend.install(record, "target", host.data["target"])
                    assert caught.value is failure
                    assert not (root / "helper-costs-checkpoint.json").exists()
                    assert worker.read_json(root / "helper-costs.json") == final_costs
                    assert len(callbacks) == 1
                    return

                confirmation = await host.backend.install(record, "target", host.data["target"])
                assert confirmation == expected_confirmation(record)
                assert host.record() == record
                assert host.backend._disk().configuration.data == host.data["target"].configuration
                assert host.probe.loaded_unit == host.data["target"].unit
                assert not await case.helper_empty(HelperProbe(), helper, argv)
                worker.collect_helper_costs(root, case.result, deadline=case.deadline)
                assert case.result["resources"]["helper_costs"] == {
                    "code": "not_observed",
                    "reason": "identity_changed",
                }
                assert worker.read_json(root / "helper-costs.json") == final_costs
                ci.fixture_result(json.dumps(case.result).encode())

                path = root / "helper-costs-checkpoint.json"
                assert path.is_file(), "真实install已成功返回，不能等atexit才发布在途成本"
                checkpoint = worker.read_json(path)
                assert set(checkpoint) == {"sample", "costs"}
                assert checkpoint["sample"] == {
                    "kind": "install_returned",
                    "revision": record.revision,
                }
                assert worker.helper_costs_facts(checkpoint["costs"], source)
                assert checkpoint["costs"]["checks"]["count"] > 0
                assert case.result["resources"]["helper_source"] == source
                # Refuse to turn the observer's self-reported identity into a
                # trusted source. PID/invocation changes enter the real source
                # observer; an explicitly invalidated generation remains revoked.
                for damage in (
                    "missing_source",
                    "unobserved",
                    "invalid_source",
                    "pid",
                    "invocation",
                ):
                    rejected = worker.Case(root)
                    rejected.result["generations"] = copy.deepcopy(case.result["generations"])
                    if damage == "unobserved":
                        prior = snapshot
                        snapshot = replace(snapshot, pid=0, invocation="0" * 32)
                        assert not await rejected.helper_empty(HelperProbe(), helper, argv)
                        snapshot = prior
                        assert "helper_source" not in rejected.result["resources"]
                    elif damage != "missing_source":
                        assert not await rejected.helper_empty(HelperProbe(), helper, argv)
                    if damage == "invalid_source":
                        rejected._invalid_helper_sources.add(record.generation)
                    elif damage in {"pid", "invocation"}:
                        prior = snapshot
                        snapshot = replace(
                            snapshot,
                            **(
                                {"pid": snapshot.pid + 1}
                                if damage == "pid"
                                else {"invocation": "e" * 32}
                            ),
                        )
                        assert not await rejected.helper_empty(HelperProbe(), helper, argv)
                        snapshot = prior
                    before = copy.deepcopy(rejected.result)
                    rejected._collect_helper_cost_sample(record)
                    assert rejected._helper_cost_samples == []
                    assert rejected.result == before

                for damage in ("future_revision", "bool_revision", "wrong_kind"):
                    rejected = worker.Case(root)
                    rejected.result["generations"] = copy.deepcopy(case.result["generations"])
                    assert not await rejected.helper_empty(HelperProbe(), helper, argv)
                    damaged = copy.deepcopy(checkpoint)
                    if damage == "future_revision":
                        damaged["sample"]["revision"] = record.revision + 1
                    elif damage == "bool_revision":
                        damaged["sample"]["revision"] = True
                    else:
                        damaged["sample"]["kind"] = "helper_exited"
                    worker.write_json(path, damaged)
                    before = copy.deepcopy(rejected.result)
                    rejected._collect_helper_cost_sample(record)
                    assert rejected._helper_cost_samples == []
                    assert rejected.result == before
                worker.write_json(path, checkpoint)

                class PositionObserved(Exception):
                    pass

                requests: list[tuple[str, str]] = []

                async def http(
                    client: Any, method: str, path: str, body: dict[str, Any] | None = None
                ) -> tuple[int, dict[str, Any]]:
                    requests.append((method, path))
                    if method == "POST":
                        assert path == "/api/management/transactions"
                        assert body is not None and body["action"] == "switch"
                        return 200, {"id": record.request_id}
                    assert path == "/api/management/transactions/current"
                    return 200, {"transaction": record.public()}

                async def position_observed() -> None:
                    raise PositionObserved

                import muli.platforms.managed_probe as managed_probe

                monkeypatch.setattr(managed_probe, "ManagedSystemdProbe", HelperProbe)
                monkeypatch.setattr(case, "http", http)
                monkeypatch.setattr(case, "pause", position_observed)
                # Real transaction/journal/deployment and fresh-position source
                # authentication run before the finite pause boundary. No
                # observer cache/resource is inserted by the test.
                before_resources = copy.deepcopy(case.result["resources"])
                with pytest.raises(PositionObserved):
                    await case.transaction(object(), "", "switch", host.slots["target"], "0" * 32)
                assert requests == [
                    ("POST", "/api/management/transactions"),
                    ("GET", "/api/management/transactions/current"),
                ]
                expected = [{"source": source, **checkpoint}]
                assert case._helper_cost_samples == expected
                assert case.result["resources"] == before_resources
                case._collect_helper_cost_sample(record)
                assert case._helper_cost_samples == expected  # One sample per generation.
                worker.collect_helper_costs(
                    root, case.result, deadline=case.deadline, samples=case._helper_cost_samples
                )
                assert case.result["resources"]["helper_cost_samples"] == expected
                assert case.result["resources"]["helper_costs"] == {
                    "code": "not_observed",
                    "reason": "identity_changed",
                }
                assert "helper_cost_history" not in case.result["resources"]
                assert ci.fixture_result(json.dumps(case.result).encode()) == case.result
                assert len(callbacks) == 1  # The atexit callback is never invoked.
            finally:
                host.release_writer()
                await host.backend.__aexit__()


def costs_result() -> tuple[dict[str, Any], dict[str, Any]]:
    source = {
        "generation": "a" * 32,
        "helper": "muli-transaction-" + "b" * 64 + ".service",
        "uid": os.geteuid(),
        "pid": os.getpid(),
        "invocation": "c" * 32,
    }
    facts = {
        "version": 1,
        "code": "observed",
        **source,
        "checks": {"count": 3, "total_ms": 123},
        "bus": {"count": 2, "total_ms": 45},
        "http": {"count": 0, "total_ms": 0},
    }
    result = {
        "completed": False,
        "stage": "transaction",
        "code": "TimeoutError",
        "timeline": [],
        "generations": [
            {"deployment": "d" * 64, "generation": source["generation"], "helper": source["helper"]}
        ],
        "resources": {"helper_source": source},
    }
    return result, facts


def test_costs_are_collected_once_from_private_file_and_bound_to_helper(tmp_path: Path) -> None:
    result, facts = costs_result()
    worker.write_json(tmp_path / "helper-costs.json", facts)
    worker.collect_helper_costs(tmp_path, result)
    assert result["resources"]["helper_costs"] == facts
    assert ci.fixture_result(json.dumps(result).encode()) == result


def test_partial_cost_sample_never_displaces_existing_facts_at_the_original_byte_limit(
    tmp_path: Path,
) -> None:
    # This is a final-settlement reader/budget fixture, not a claim that a
    # controller ran. The real hook/install/Case path is exercised above.
    result, facts = costs_result()
    result["resources"]["helper_costs"] = copy.deepcopy(facts)
    source = copy.deepcopy(result["resources"]["helper_source"])
    samples: list[dict[str, Any]] = [
        {
            "source": source,
            "sample": {"kind": "install_returned", "revision": 3},
            "costs": copy.deepcopy(facts),
        }
    ]
    worker.write_json(tmp_path / "helper-costs.json", facts)
    row: dict[str, Any] = {"stage": "transaction", "elapsed_ms": 1}
    row.update({name: "x" * 100 for name in ("request_id", "boot", "result")})
    result["timeline"] = [dict(row) for _ in range(64)]
    for name in ("helper", "state"):
        for item in result["timeline"]:
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = worker.MAX_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                item[name] = "x" * min(100, capacity)
    used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
    assert worker.MAX_BYTES - 100 < used < worker.MAX_BYTES
    assert ci.fixture_result(json.dumps(result, separators=(",", ":")).encode()) == result
    before = copy.deepcopy(result)
    worker.collect_helper_costs(tmp_path, result, samples=samples)
    assert result == before
    assert "helper_cost_samples" not in result["resources"]
    assert ci.fixture_result(json.dumps(result, separators=(",", ":")).encode()) == result


def test_coordinator_manager_merge_discards_only_partial_costs_at_the_original_byte_limit(
    fixture: tuple[Any, Any],
) -> None:
    native, commands = fixture
    native.prepare()
    readiness = copy.deepcopy(native.manager_readiness)
    assert readiness == {"elapsed_ms": 0, "late_ready": False}

    # Valid reader materials stand in for an already settled worker reply;
    # collection itself uses the real coordinator and Commands' fixed cat seam.
    result, facts = costs_result()
    source = copy.deepcopy(result["resources"]["helper_source"])
    result["resources"].update(
        helper_costs=copy.deepcopy(facts),
        helper_cost_history=[{"source": copy.deepcopy(source), "costs": copy.deepcopy(facts)}],
        helper_cost_samples=[
            {
                "source": source,
                "sample": {"kind": "install_returned", "revision": 3},
                "costs": copy.deepcopy(facts),
            }
        ],
    )
    row: dict[str, Any] = {"stage": "transaction", "elapsed_ms": 1}
    row.update({name: "x" * 100 for name in ("request_id", "boot", "result")})
    result["timeline"] = [dict(row) for _ in range(64)]
    for name in ("helper", "state"):
        for item in result["timeline"]:
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = ci.OUTPUT_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                item[name] = "x" * min(100, capacity)
    wire = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    assert ci.OUTPUT_BYTES - 100 < len(wire) < ci.OUTPUT_BYTES
    assert ci.fixture_result(wire) == result
    merged = copy.deepcopy(result)
    merged["resources"]["manager_readiness"] = readiness
    assert len(json.dumps(merged, sort_keys=True, separators=(",", ":")).encode()) > ci.OUTPUT_BYTES
    expected = copy.deepcopy(merged)
    expected["resources"].pop("helper_cost_samples")
    assert (
        ci.fixture_result(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode())
        == expected
    )

    worker.write_json(native.volume / "result.json", result)
    commands.calls.clear()
    collected = native.collect_result()
    assert commands.calls == [
        ("sudo", "--non-interactive", "cat", str(native.volume / "result.json"))
    ]
    assert collected == expected
    assert ci.fixture_result((native.root / "result.json").read_bytes()) == expected
    assert collected["code"] == result["code"] == "TimeoutError"
    assert collected["timeline"] == result["timeline"]
    assert collected["generations"] == result["generations"]


@pytest.mark.parametrize("damage", ["bool_revision", "four", "source", "extra", "kind"])
def test_partial_cost_artifact_rejects_non_contract_samples(damage: str) -> None:
    result, facts = costs_result()
    row: dict[str, Any] = {
        "source": copy.deepcopy(result["resources"]["helper_source"]),
        "sample": {"kind": "install_returned", "revision": 3},
        "costs": facts,
    }
    result["resources"]["helper_cost_samples"] = [row]
    assert ci.fixture_result(json.dumps(result).encode()) == result
    if damage == "bool_revision":
        row["sample"]["revision"] = True
    elif damage == "four":
        result["resources"]["helper_cost_samples"] *= 4
    elif damage == "source":
        row["source"]["pid"] += 1
    elif damage == "extra":
        row["path"] = "/must-not-export"
    else:
        row["sample"]["kind"] = "helper_exited"
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(result).encode())


@pytest.mark.parametrize("key", ["generation", "helper", "uid", "pid", "invocation"])
def test_other_helper_costs_cannot_become_observed(tmp_path: Path, key: str) -> None:
    result, facts = costs_result()
    facts[key] = facts[key] + 1 if type(facts[key]) is int else "e" + facts[key][1:]
    worker.write_json(tmp_path / "helper-costs.json", facts)
    worker.collect_helper_costs(tmp_path, result)
    assert result["resources"]["helper_costs"] == {
        "code": "not_observed",
        "reason": "identity_changed",
    }
    result["resources"]["helper_costs"] = facts
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(result).encode())


@pytest.mark.parametrize(
    "damage", ["bool", "negative", "extra", "metric_extra", "too_large", "missing"]
)
def test_costs_result_rejects_non_contract_metrics(damage: str) -> None:
    result, facts = costs_result()
    if damage == "bool":
        facts["checks"]["count"] = True
    elif damage == "negative":
        facts["bus"]["total_ms"] = -1
    elif damage == "extra":
        facts["path"] = "/not-exported"
    elif damage == "metric_extra":
        facts["http"]["extra"] = 0
    elif damage == "too_large":
        facts["checks"]["total_ms"] = 2**63
    else:
        facts.pop("checks")
    result["resources"]["helper_costs"] = facts
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(result).encode())


@pytest.mark.parametrize("state", ["missing", "symlink", "public", "oversized", "invalid"])
def test_missing_or_untrusted_costs_never_imply_zero(tmp_path: Path, state: str) -> None:
    result, facts = costs_result()
    path = tmp_path / "helper-costs.json"
    if state == "symlink":
        worker.write_json(tmp_path / "other.json", facts)
        path.symlink_to(tmp_path / "other.json")
    elif state != "missing":
        worker.write_json(path, facts)
        if state == "public":
            path.chmod(0o644)
        elif state == "oversized":
            path.write_bytes(b" " * (worker.MAX_BYTES + 1))
        else:
            path.write_bytes(b"{broken")
    worker.collect_helper_costs(tmp_path, result)
    assert result["resources"]["helper_costs"] == {"code": "not_observed", "reason": "unavailable"}
    assert ci.fixture_result(json.dumps(result).encode()) == result


def test_costs_do_not_displace_existing_result_at_original_byte_limit(tmp_path: Path) -> None:
    result, facts = costs_result()
    worker.write_json(tmp_path / "helper-costs.json", facts)
    # All 64 original timeline rows remain present, even when they leave less
    # space than a complete observer receipt. No source proof is cut to fit.
    row = {"stage": "transaction", "elapsed_ms": 1}
    row.update({name: "x" * 100 for name in ("request_id", "boot", "result")})
    result["timeline"] = [dict(row) for _ in range(64)]
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    assert len(raw) < worker.MAX_BYTES
    # Use legitimate bounded rows and vary only their finite text lengths to
    # bring the existing result just below its original 32 KiB ceiling.
    for name in ("helper", "state"):
        for item in result["timeline"]:
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = worker.MAX_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                item[name] = "x" * min(100, capacity)
    used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
    assert worker.MAX_BYTES - 100 < used < worker.MAX_BYTES
    before = copy.deepcopy(result)
    ci.fixture_result(json.dumps(before, separators=(",", ":")).encode())
    worker.collect_helper_costs(tmp_path, result)
    costs = result["resources"].pop("helper_costs", None)
    assert costs is None or costs == {"code": "not_observed", "reason": "output_budget"}
    assert result == before


def test_observer_setup_rejection_preserves_original_helper_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks: list[object] = []
    originals = (ManagedLinuxBackend._check, SystemdProbe._command, ManagedLinuxBackend._request)
    monkeypatch.setattr(atexit, "register", lambda callback: callbacks.append(callback))
    monkeypatch.setattr(
        sys, "orig_argv", [sys.executable, "-I", "-m", "muli.management_helper", "/wrong"]
    )
    worker.install_helper_cost_hook()
    assert callbacks == []
    assert (
        ManagedLinuxBackend._check,
        SystemdProbe._command,
        ManagedLinuxBackend._request,
    ) == originals


@pytest.mark.parametrize("damage", ["wrong_root", "recover", "missing_run", "extra_argument"])
def test_managed_helper_observer_rejects_nonexact_arguments_before_fixture_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    prefix = tmp_path / "a"
    executable = str(prefix / "bin/python")
    arguments = [
        executable,
        "-I",
        "-m",
        "muli.managed_host",
        "--root",
        str(tmp_path / "data/management/transaction-v1"),
        "run",
    ]
    if damage == "wrong_root":
        arguments[-2] = str(tmp_path / "foreign")
    elif damage == "recover":
        arguments[-1] = "recover"
    elif damage == "missing_run":
        arguments.pop()
    else:
        arguments.append("unexpected")
    callbacks: list[object] = []
    accesses: list[Path] = []

    def fixture_access(root: Path) -> dict[str, Any]:
        accesses.append(root)
        return {}

    originals = (ManagedLinuxBackend._check, SystemdProbe._command, ManagedLinuxBackend._request)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", executable)
    monkeypatch.setattr(sys, "orig_argv", arguments)
    monkeypatch.setattr(worker, "fixture", fixture_access)
    monkeypatch.setattr(atexit, "register", lambda callback: callbacks.append(callback))
    worker.install_helper_cost_hook()
    assert accesses == [] and callbacks == []
    assert (
        ManagedLinuxBackend._check,
        SystemdProbe._command,
        ManagedLinuxBackend._request,
    ) == originals


def test_completed_helper_costs_survive_the_next_live_helper_and_final_settlement(
    tmp_path: Path,
) -> None:
    result, first = costs_result()
    first_source = copy.deepcopy(result["resources"]["helper_source"])
    cache: list[dict[str, Any]] = []
    original = copy.deepcopy(result)
    worker.write_json(tmp_path / "helper-costs.json", first)
    worker.collect_helper_costs(
        tmp_path, result, completed_generation=first_source["generation"], retained=cache
    )
    retained = [{"source": first_source, "costs": first}]
    assert cache == retained and result == original
    worker.collect_helper_costs(
        tmp_path, result, completed_generation=first_source["generation"], retained=cache
    )
    assert cache == retained and result == original

    second_source = {
        **first_source,
        "generation": "e" * 32,
        "helper": "muli-transaction-" + "f" * 64 + ".service",
        "pid": first_source["pid"] + 1,
        "invocation": "a" * 32,
    }
    result["generations"].append(
        {
            "deployment": "d" * 64,
            "generation": second_source["generation"],
            "helper": second_source["helper"],
        }
    )
    result["resources"]["helper_source"] = second_source
    # The next helper is still live: its exit receipt has not replaced the file.
    worker.collect_helper_costs(tmp_path, result, retained=cache)
    assert result["resources"]["helper_source"] == second_source
    assert result["resources"]["helper_costs"] == {
        "code": "not_observed",
        "reason": "identity_changed",
    }
    assert result["resources"]["helper_cost_history"] == retained
    assert ci.fixture_result(json.dumps(result).encode()) == result
    second = {**copy.deepcopy(first), **second_source}
    worker.write_json(tmp_path / "helper-costs.json", second)
    worker.collect_helper_costs(
        tmp_path, result, completed_generation=second_source["generation"], retained=cache
    )
    assert cache == [*retained, {"source": second_source, "costs": second}]
    worker.collect_helper_costs(tmp_path, result, retained=cache)
    assert result["resources"]["helper_cost_history"] == [
        *retained,
        {"source": second_source, "costs": second},
    ]
    assert result["code"] == "TimeoutError"
    assert ci.fixture_result(json.dumps(result).encode()) == result


@pytest.mark.parametrize(
    "damage", ["pid", "invocation", "missing_source", "wrong_completed_generation"]
)
def test_completed_cost_history_never_adopts_a_file_without_the_frozen_source(
    tmp_path: Path,
    damage: str,
) -> None:
    result, facts = costs_result()
    cache: list[dict[str, Any]] = []
    generation = facts["generation"]
    if damage == "pid":
        facts["pid"] += 1
    elif damage == "invocation":
        facts["invocation"] = "f" * 32
    elif damage == "missing_source":
        result["resources"].pop("helper_source")
    else:
        generation = "e" * 32
    worker.write_json(tmp_path / "helper-costs.json", facts)
    original = copy.deepcopy(result)
    worker.collect_helper_costs(tmp_path, result, completed_generation=generation, retained=cache)
    assert cache == [] and result == original
    assert "helper_cost_history" not in result["resources"]
    assert result["code"] == "TimeoutError"
    assert ci.fixture_result(json.dumps(result).encode()) == result


def test_expired_cost_read_preserves_prior_completed_generation_without_new_file_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, facts = costs_result()
    source = copy.deepcopy(result["resources"]["helper_source"])
    cache = [{"source": source, "costs": facts}]
    original = copy.deepcopy(result)
    opens: list[bool] = []

    def expired(deadline: float) -> float:
        raise TimeoutError

    def unexpected_open(*args: Any, **kwargs: Any) -> int:
        opens.append(True)
        raise AssertionError("expired observation must not open a file")

    monkeypatch.setattr(worker, "remaining", expired)
    monkeypatch.setattr(worker.os, "open", unexpected_open)
    worker.collect_helper_costs(
        tmp_path,
        result,
        deadline=1.0,
        completed_generation=source["generation"],
        retained=cache,
    )
    assert result == original and cache == [{"source": source, "costs": facts}]
    worker.collect_helper_costs(tmp_path, result, deadline=1.0, retained=cache)
    assert result["resources"]["helper_cost_history"] == cache
    assert result["resources"]["helper_costs"] == {"code": "not_observed", "reason": "unavailable"}
    assert opens == []
    assert result["code"] == "TimeoutError"
    assert ci.fixture_result(json.dumps(result).encode()) == result


@pytest.mark.parametrize(
    "damage", ["duplicate", "four", "source", "counts", "extra", "foreign_generation", "unobserved"]
)
def test_cost_history_artifact_rejects_non_contract_rows(damage: str) -> None:
    result, facts = costs_result()
    row = {"source": copy.deepcopy(result["resources"]["helper_source"]), "costs": facts}
    history = [row]
    if damage in {"duplicate", "four"}:
        history *= 2 if damage == "duplicate" else 4
    elif damage == "source":
        row["source"]["pid"] = True
    elif damage == "counts":
        facts["checks"]["count"] = True
    elif damage == "extra":
        row["path"] = "/must-not-export"
    elif damage == "foreign_generation":
        row["source"]["generation"] = facts["generation"] = "e" * 32
    else:
        row["costs"] = {"code": "not_observed", "reason": "unavailable"}
    result["resources"]["helper_cost_history"] = history
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(result).encode())


def test_cost_history_never_displaces_existing_result_at_the_original_byte_limit(
    tmp_path: Path,
) -> None:
    result, facts = costs_result()
    cache: list[dict[str, Any]] = []
    worker.write_json(tmp_path / "helper-costs.json", facts)
    worker.collect_helper_costs(
        tmp_path, result, completed_generation=facts["generation"], retained=cache
    )
    worker.collect_helper_costs(tmp_path, result, retained=cache)
    retained = copy.deepcopy(result["resources"]["helper_cost_history"])
    second_source = {
        **result["resources"]["helper_source"],
        "generation": "e" * 32,
        "helper": "muli-transaction-" + "f" * 64 + ".service",
    }
    result["resources"]["helper_source"] = second_source
    result["resources"].pop("helper_costs")
    result["generations"].append(
        {
            "deployment": "d" * 64,
            "generation": second_source["generation"],
            "helper": second_source["helper"],
        }
    )
    second = {**facts, **second_source}
    worker.write_json(tmp_path / "helper-costs.json", second)
    worker.collect_helper_costs(
        tmp_path, result, completed_generation=second_source["generation"], retained=cache
    )
    row = {"stage": "transaction", "elapsed_ms": 1}
    row.update({name: "x" * 100 for name in ("request_id", "boot", "result")})
    result["timeline"] = [dict(row) for _ in range(64)]
    for name in ("helper", "state"):
        for item in result["timeline"]:
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = worker.MAX_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                item[name] = "x" * min(100, capacity)
    used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
    assert worker.MAX_BYTES - 100 < used < worker.MAX_BYTES
    before = copy.deepcopy(result)
    worker.collect_helper_costs(tmp_path, result, retained=cache)
    costs = result["resources"].pop("helper_costs", None)
    assert costs is None or costs == {"code": "not_observed", "reason": "output_budget"}
    assert result["resources"]["helper_cost_history"] == retained
    assert result == before
    assert ci.fixture_result(json.dumps(result, separators=(",", ":")).encode()) == result
