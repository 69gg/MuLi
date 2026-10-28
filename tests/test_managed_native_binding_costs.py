"""Offline binding diagnostics: no systemd, interpreter launch or external service."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastapi import FastAPI
from helpers import managed_native_worker as worker
from test_managed_boot import Host
from test_managed_boot import host as host
from test_managed_native_ci import ci
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_runtime_binding import Running, _plan
from test_managed_runtime_binding_file_proofs import bounded_running as bounded_running
from test_managed_runtime_binding_file_proofs import observe_reads

from muli import managed_runtime_binding, server
from muli.maintenance import Maintenance
from muli.managed_host import managed_helper_unit_name
from muli.management_projection import project_final
from muli.management_transaction import ClockReading, Role
from muli.runtime import Runtime

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Native fixture")


def observer_for(
    running: Running, monkeypatch: pytest.MonkeyPatch, original: Callable[..., Any] | None = None
) -> worker.BindingCosts:
    monkeypatch.setenv("INVOCATION_ID", running.permit.boot)
    observer = worker.BindingCosts(
        running.host.journal.database.parent.parent,
        original or managed_runtime_binding.verify_running_binding,
        running.permit.boot,
    )
    # Lower-level observation tests set this explicitly. The generated-hook
    # regression below independently exercises the real Runtime closure proof.
    observer.bound = True
    return observer


def result_for(running: Running, observer: worker.BindingCosts) -> dict[str, Any]:
    record, slot = running.record, running.host.base
    description = {key: getattr(slot.package, key) for key in ("files", "size", "digest")}
    return {
        "completed": False,
        "stage": "transaction",
        "code": "TimeoutError",
        "timeline": [
            {
                "stage": "projection",
                "elapsed_ms": 1,
                "generation": record.generation,
                "pid": observer.source["pid"],
                "boot": observer.source["boot"],
            },
            {
                "stage": "transaction",
                "elapsed_ms": 2,
                "generation": record.generation,
                "revision": record.revision,
                "intent": record.intent,
                "phase": record.phase,
                "code": record.code,
                "failure_code": record.failure_code,
                "rollback_attempted": record.rollback_attempted,
            },
        ],
        "generations": [
            {
                "deployment": record.identity.deployment,
                "generation": record.generation,
                "helper": managed_helper_unit_name(record),
            }
        ],
        "resources": {
            "binding_inputs": worker.binding_inputs(
                {"a": slot, "b": running.host.target}, description
            ),
        },
    }


def test_generated_serve_hook_reaches_the_actual_runtime_closure_before_factory_return(
    host: Host, fixture: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    native, _ = fixture
    native.prepare()
    settings = worker.read_json(native.volume / "fixture.json")
    settings["uid"] = os.geteuid()
    worker.write_json(native.volume / "fixture.json", settings)
    record = host.record("committed", "none", activation="released", legacy=True)
    permit = host.acquire()
    monkeypatch.setenv("INVOCATION_ID", permit.boot)
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            prefix=str(native.volume / "a"),
            executable=str(native.volume / "a/bin/python"),
        ),
    )
    monkeypatch.setattr(worker, "require_environment", lambda root, uid: {})
    monkeypatch.setattr(managed_runtime_binding, "verify_local_filesystems", lambda paths: ())
    monkeypatch.setattr(Runtime, "_initialize", lambda self: None)
    original = managed_runtime_binding.verify_running_binding
    entered: list[object] = []

    def verify(*args: Any, **kwargs: Any) -> None:
        entered.append(args[2])
        original(*args, **kwargs)

    def minimal_app(config: Any, runtime: Runtime, *args: Any) -> FastAPI:
        app = FastAPI()
        app.state.runtime = runtime
        return app

    monkeypatch.setattr(managed_runtime_binding, "verify_running_binding", verify)
    monkeypatch.setattr(server, "_create_app", minimal_app)
    monkeypatch.setattr(server, "create_app", server.create_app)
    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    monkeypatch.setattr(sys, "argv", ["muli", "serve"])
    exec((native.volume / "_muli_native_ci_hook.py").read_text(), {})
    app = server.create_app(boot_permit=permit, data_dir=host.journal.database.parent)
    runtime = app.state.runtime
    try:
        observer = app.user_middleware[0].kwargs["observer"]
        assert observer.bound
        for attempt in (1, 2):
            runtime.maintenance.verify_managed(record, "target")
            assert len(entered) == attempt and entered[-1] is record
            assert sum(row["entered"] for row in observer.rows.values()) == attempt
        assert observer.snapshot(final=False)["scope"] == "prefix"
    finally:
        runtime.store.close()


def test_each_observed_call_reopens_and_reads_with_a_fresh_binding_proof(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = bounded_running
    observer = observer_for(running, monkeypatch)
    reads = observe_reads(running, monkeypatch)
    for attempt in (1, 2):
        observer.verify(running.permit, running.store, running.record, "target")
        assert all(len(observed.opened) == attempt * 2 for observed in reads.files.values())
        assert all(
            observed.bytes == path.stat().st_size * attempt
            for path, observed in reads.files.items()
        )
        assert sum(row["entered"] for row in observer.rows.values()) == attempt


def test_a_preexisting_foreign_runtime_closure_cannot_export_zero_cost(
    tmp_path: Path,
) -> None:
    observer = worker.BindingCosts(tmp_path, lambda *args: None, "a" * 32)
    app = FastAPI()
    app.state.runtime = SimpleNamespace(
        maintenance=SimpleNamespace(
            _managed_verifier=lambda record, role: None,
        )
    )
    observer.attach(app)
    assert not observer.bound
    assert observer.snapshot(final=False) == worker.binding_unavailable("unbound")


@pytest.mark.parametrize("outcome", ["return", "raise", "cancel"])
def test_original_is_called_once_and_keeps_return_or_exception_identity(
    bounded_running: Running,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["return", "raise", "cancel"],
) -> None:
    running, sentinel = bounded_running, object()
    failure = asyncio.CancelledError("private") if outcome == "cancel" else RuntimeError("private")
    entered: list[object] = []

    def original(*args: Any, **kwargs: Any) -> object:
        entered.append(args[2])
        if outcome != "return":
            raise failure
        return sentinel

    observer = observer_for(running, monkeypatch, original)
    arguments = running.permit, running.store, running.record, "target"
    if outcome == "return":
        assert observer.verify(*arguments) is sentinel
    else:
        with pytest.raises(type(failure)) as caught:
            observer.verify(*arguments)
        assert caught.value is failure
    assert len(entered) == 1 and entered[0] is running.record
    row = observer.snapshot(final=False)["rows"][0]
    assert (row["entered"], row["returned"], row["raised"]) == (
        1,
        int(outcome == "return"),
        int(outcome != "return"),
    )


async def test_activation_still_authenticates_four_times_and_exact_projection_is_a_read(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = bounded_running
    running.record = running.host.record("committed", "none", legacy=True)
    record = running.record
    observer = observer_for(running, monkeypatch)
    maintenance = Maintenance(running.permit)
    maintenance.admission.pause()
    maintenance.prepared(record.identity.store_instance, record.identity.main_session)
    maintenance.seal(set())

    def verify(current: Any, role: Role | None) -> None:
        observer.verify(running.permit, running.store, current, role)

    async def activate() -> None:
        await asyncio.sleep(0)

    def clock() -> ClockReading:
        return record.deadline.created

    maintenance.bind_managed_verifier(verify)
    maintenance.bind_activation(activate)
    await maintenance.activate(record.generation, running.permit.boot, record.challenge, clock)
    assert sum(row["entered"] for row in observer.rows.values()) == 4
    assert project_final(running.permit, running.store, verify_binding=verify) is None
    assert sum(row["entered"] for row in observer.rows.values()) == 4
    # Activation returns health; the controller must still persist its release
    # decision before the existing projection can acknowledge a resolved record.
    record = running.host.record(
        "committed", "none", revision=record.revision + 1, activation="released", legacy=True
    )
    running.record = record
    _plan(running.store, record)
    first = project_final(running.permit, running.store, verify_binding=verify)
    assert first is not None
    assert sum(row["entered"] for row in observer.rows.values()) == 5
    changes = running.store.db.total_changes
    assert project_final(running.permit, running.store, verify_binding=verify) == first
    assert running.store.db.total_changes == changes
    assert sum(row["entered"] for row in observer.rows.values()) == 5


@pytest.mark.parametrize("fault", ["raises", "backwards", "boolean"])
def test_bad_clocks_only_disable_diagnostics(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    sentinel, calls = object(), []

    def original(*args: Any, **kwargs: Any) -> object:
        calls.append(True)
        return sentinel

    def clock() -> int:
        if fault == "raises":
            raise OSError("clock unavailable")
        if fault == "boolean":
            return True
        return 2 if not calls else 1

    observer = observer_for(bounded_running, monkeypatch, original)
    monkeypatch.setattr(
        worker,
        "time",
        SimpleNamespace(
            **{**vars(worker.time), "perf_counter_ns": clock},
        ),
    )
    assert (
        observer.verify(
            bounded_running.permit, bounded_running.store, bounded_running.record, "target"
        )
        is sentinel
    )
    assert calls == [True]
    assert observer.snapshot(final=False) == worker.binding_unavailable("clock_invalid")


async def test_publication_is_bounded_and_only_normal_shutdown_is_final(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = bounded_running
    observer = observer_for(running, monkeypatch, lambda *args: None)
    writes: list[dict[str, Any]] = []
    messages: list[object] = []
    message = {"type": "http.response.body", "body": b"unchanged"}
    failure = asyncio.CancelledError("private lifespan cancellation")

    def write(path: Path, value: dict[str, Any]) -> None:
        writes.append(copy.deepcopy(value))

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send(message if scope["type"] == "http" else {"type": "lifespan.shutdown.complete"})
        if scope.get("fail"):
            raise failure

    async def send(value: object) -> None:
        messages.append(value)

    monkeypatch.setattr(worker, "write_json", write)
    middleware = worker.BindingCostMiddleware(app, observer)
    for revision in range(1, 13):
        before = len(writes)
        observer.verify(
            running.permit,
            running.store,
            running.record.model_copy(
                update={"revision": revision},
            ),
            "target",
        )
        assert len(writes) == before  # Every binding itself remains memory-only.
        await middleware({"type": "http"}, None, send)
        assert messages[-1] is message
    assert len(writes) == 8 and all(value["scope"] == "prefix" for value in writes)
    assert observer.publications["count"] == 8
    with pytest.raises(asyncio.CancelledError) as caught:
        await middleware({"type": "lifespan", "fail": True}, None, send)
    assert caught.value is failure
    assert not observer.terminal and len(writes) == 8
    await middleware({"type": "lifespan"}, None, send)
    await middleware({"type": "http"}, None, send)
    assert len(writes) == 9 and writes[-1]["scope"] == "final"
    assert writes[-1]["prior_publications"]["count"] == 8
    assert sum(row["entered"] for row in writes[-1]["rows"]) == 12


def test_failed_publications_also_consume_the_finite_allowance(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = bounded_running
    observer = observer_for(running, monkeypatch, lambda *args: None)
    attempted: list[Path] = []

    def unavailable(path: Path, value: dict[str, Any]) -> None:
        attempted.append(path)
        raise OSError("private write failure")

    monkeypatch.setattr(worker, "write_json", unavailable)
    for revision in range(1, 13):
        observer.verify(
            running.permit,
            running.store,
            running.record.model_copy(
                update={"revision": revision},
            ),
            "target",
        )
        observer.publish()
        observer.publish()  # No retry of this same position.
    assert len(attempted) == 8
    observer.publish(final=True)
    observer.publish(final=True)
    assert len(attempted) == 9
    assert observer.snapshot(final=False) == worker.binding_unavailable()


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "missing",
        "source",
        "oversize",
        "mode",
        "clock",
        "unbound",
        "future",
        "no_proof",
    ],
)
def test_collection_requires_the_existing_source_and_never_turns_absence_into_zero(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, damage: str
) -> None:
    running = bounded_running
    observer = observer_for(running, monkeypatch)
    observer.verify(running.permit, running.store, running.record, "target")
    result = result_for(running, observer)
    source = dict(observer.source)
    costs = observer.snapshot(final=False)
    if damage == "source":
        costs["source"]["pid"] += 1
    elif damage == "clock":
        costs = worker.binding_unavailable("clock_invalid")
    elif damage == "unbound":
        observer.bound = False
        costs = observer.snapshot(final=False)
    elif damage == "future":
        costs["rows"][0]["revision"] += 1
    elif damage == "no_proof":
        result["timeline"] = result["timeline"][1:]
    path = tmp_path / f"binding-costs-{source['pid']}-{source['invocation']}.json"
    if damage != "missing":
        worker.write_json(path, costs)
    if damage == "oversize":
        path.write_bytes(b"x" * (worker.MAX_BYTES + 1))
    elif damage == "mode":
        path.chmod(0o644)
    original = copy.deepcopy(result)
    worker.collect_binding_costs(tmp_path, result, [source], deadline=None)
    observed = result["resources"].pop("binding_costs")
    assert result == original
    if damage == "no_proof":
        assert observed == worker.binding_unavailable()
    else:
        assert observed["code"] == "partial"
        sample = observed["sources"][0]["costs"]
        assert sample["code"] == ("observed" if damage == "none" else "not_observed")
        if damage == "none":
            assert sample["scope"] == "prefix" and sample["rows"][0]["entered"] == 1
        else:
            assert "rows" not in sample
    result["resources"]["binding_costs"] = observed
    assert ci.fixture_result(json.dumps(result).encode()) == result


@pytest.mark.parametrize("future_first", [False, True])
def test_a_future_source_snapshot_preserves_other_authenticated_history(
    bounded_running: Running,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    future_first: bool,
) -> None:
    running = bounded_running
    observer = observer_for(running, monkeypatch)
    observer.verify(running.permit, running.store, running.record, "target")
    result = result_for(running, observer)
    history_source = dict(observer.source)
    history_costs = observer.snapshot(final=False)
    future_source = {
        **history_source,
        "pid": history_source["pid"] + 1,
        "invocation": "3" * 32,
        "boot": "3" * 32,
    }
    later = running.record.model_copy(update={"generation": "b" * 32})
    result["generations"].append(
        {
            "deployment": later.identity.deployment,
            "generation": later.generation,
            "helper": managed_helper_unit_name(later),
        }
    )
    result["timeline"].extend(
        [
            {
                **result["timeline"][0],
                "elapsed_ms": 3,
                "generation": later.generation,
                "pid": future_source["pid"],
                "boot": future_source["boot"],
            },
            {**result["timeline"][1], "elapsed_ms": 4, "generation": later.generation},
        ]
    )
    future_costs = copy.deepcopy(history_costs)
    future_costs["source"] = future_source
    future_costs["rows"][0].update(
        generation=later.generation,
        revision=later.revision + 1,
    )
    for source, costs in ((history_source, history_costs), (future_source, future_costs)):
        worker.write_json(
            tmp_path / f"binding-costs-{source['pid']}-{source['invocation']}.json",
            costs,
        )
    sources = [future_source, history_source] if future_first else [history_source, future_source]
    worker.collect_binding_costs(tmp_path, result, sources, deadline=None)
    observed = result["resources"]["binding_costs"]
    assert observed["code"] == "partial"
    assert [sample["source"] for sample in observed["sources"]] == sources
    by_pid = {sample["source"]["pid"]: sample["costs"] for sample in observed["sources"]}
    assert by_pid[history_source["pid"]] == history_costs
    assert by_pid[future_source["pid"]] == worker.binding_unavailable("identity_changed")
    assert ci.fixture_result(json.dumps(result).encode()) == result


def test_input_facts_reuse_actual_registered_values_without_a_new_scan(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    running, slot = bounded_running, bounded_running.host.base
    description = {key: getattr(slot.package, key) for key in ("files", "size", "digest")}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("input projection attempted a new scan")

    with monkeypatch.context() as context:
        context.setattr(os, "scandir", forbidden)
        facts = worker.binding_inputs({"a": slot, "b": running.host.target}, description)
    assert facts["code"] == "observed"
    assert facts["slots"]["a"]["python"]["size"] == slot.resolved_python.identity.size
    assert facts["slots"]["a"]["python"]["size"] != slot.python_bytes
    assert facts["slots"]["a"]["package"] == description
    assert (
        worker.binding_inputs({"a": slot, "b": running.host.target}, None)
        == worker.binding_unavailable()
    )
    damaged = copy.deepcopy(facts)
    damaged["slots"]["b"]["package"]["size"] += 1
    assert not worker.binding_inputs_facts(damaged)


def test_optional_binding_facts_cannot_displace_the_original_failure_or_timeline(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer = observer_for(bounded_running, monkeypatch)
    observer.verify(bounded_running.permit, bounded_running.store, bounded_running.record, "target")
    result = result_for(bounded_running, observer)
    inputs = result["resources"].pop("binding_inputs")
    row = {"stage": "transaction", "elapsed_ms": 1}
    row.update({name: "x" * 100 for name in ("request_id", "boot", "result")})
    result["timeline"] = [dict(row) for _ in range(64)]
    result["resources"]["cleanup"] = "verified"
    for name in ("helper", "state"):
        for item in result["timeline"]:
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = worker.MAX_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                item[name] = "x" * min(100, capacity)
    before = copy.deepcopy(result)
    result["resources"].update(
        binding_inputs=inputs,
        binding_costs={
            "version": 1,
            "code": "partial",
            "reason": "observed_sources_only",
            "sources": [{"source": observer.source, "costs": observer.snapshot(final=False)}],
        },
    )
    worker.trim_binding_diagnostics(result)
    for name in ("binding_costs", "binding_inputs"):
        facts = result["resources"].pop(name, None)
        assert facts is None or facts == worker.binding_unavailable("output_budget")
    assert result == before
    assert ci.fixture_result(json.dumps(result, separators=(",", ":")).encode()) == result
