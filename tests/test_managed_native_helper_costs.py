"""Offline startup-hook observation; no interpreter or manager is launched."""

from __future__ import annotations

import atexit
import copy
import json
import os
import sys
from collections.abc import Callable
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
from muli.platforms.linux_transaction import SystemdProbe
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
