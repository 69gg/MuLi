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

from muli.managed_host import managed_helper_unit_name
from muli.management_transaction import TransactionError, TransactionJournal
from muli.platforms.linux_transaction import SystemdProbe
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated helper fixture")


async def test_generated_helper_hook_measures_real_checks_without_changing_their_result(
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
                executable = host.slots["base"].lexical_python
                monkeypatch.setattr(sys, "prefix", str(root / "a"))
                monkeypatch.setattr(sys, "executable", executable)
                monkeypatch.setattr(
                    sys,
                    "orig_argv",
                    [executable, "-I", "-m", "muli.management_helper", str(journal_root)],
                )
                monkeypatch.setattr(sys, "argv", ["-m", str(journal_root)])
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
