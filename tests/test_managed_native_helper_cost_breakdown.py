"""Fixed helper cost subsets use real offline guards and the existing receipts."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, cast

import pytest
from helpers import managed_native_worker as worker
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_helper_costs import _guard_result, _GuardFixture
from test_managed_native_helper_costs import helper_guard as helper_guard

import muli.platforms.managed_deployment as managed_deployment
import muli.platforms.managed_launcher as managed_launcher
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import JournalControl, Role
from muli.platforms.linux_transaction import SystemdProbe
from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated helper fixture")


@pytest.fixture(autouse=True)
def restore_fixed_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generated helper hook lasts one invocation, including in this process."""
    for owner, name in (
        (managed_deployment._MetadataScope, "_read_record"),
        (managed_deployment._ManagedRecord, "canonical"),
        (managed_deployment, "python_fingerprint"),
        (managed_deployment, "package_fingerprint"),
        (managed_launcher, "python_fingerprint"),
        (managed_launcher, "package_fingerprint"),
        (managed_launcher, "_fingerprint_fd"),
        (SystemdProbe, "_command"),
    ):
        monkeypatch.setattr(owner, name, getattr(owner, name))


async def test_fixed_breakdown_accounts_real_guard_subtrees_without_mixing_owners(
    helper_guard: _GuardFixture,
) -> None:
    guard, host = helper_guard, helper_guard.host
    current, controller, slots = guard.read_record(host.control, host.anchor)
    assert current == host.record() and controller == host.slots["base"] and slots == host.slots
    assert host.backend._check(current) is None
    guard.observer.save()
    facts = worker.read_json(guard.path)
    assert facts["host_record"]["returned"] == facts["checks"]["returned"] == 1
    assert worker.helper_guard_snapshot_facts(facts, guard.observer.source)
    assert "breakdown" in facts, "the fixed cost subset must accompany the original receipt"
    detail = facts["breakdown"]
    assert set(detail) == {"version", "code", "owners", "rpc"}
    assert detail["version"] == 1 and detail["code"] == "observed"
    assert set(detail["owners"]) == {"host_record", "checks"}
    assert set(detail["rpc"]) == {"GetUnit", "Unit", "Service", "other"}
    for owner in detail["owners"].values():
        assert set(owner) == {
            "private_record",
            "record_canonical",
            "python_verify",
            "package_verify",
            "fileproof",
        }
        for name in ("private_record", "record_canonical", "python_verify", "package_verify"):
            assert owner[name]["entered"] > 0
            assert owner[name]["entered"] == owner[name]["returned"] + owner[name]["raised"]
            assert owner[name]["wall_ns"] >= 0 and owner[name]["cpu_ns"] >= 0
        assert owner["private_record"]["successful_bytes"] > 0
        assert set(owner["fileproof"]) == {"python", "package", "other"}
        for proof in owner["fileproof"].values():
            assert proof["entered"] == proof["returned"] + proof["raised"]
            assert proof["returned"] == proof["full_reads"] + proof["proof_hits"]
            assert proof["raised_bytes"] is None
        assert owner["fileproof"]["python"]["full_reads"] > 0
        assert owner["fileproof"]["package"]["full_reads"] > 0
    assert all(row["entered"] == 0 and "cpu_ns" not in row for row in detail["rpc"].values())
    legacy: dict[str, Any] = copy.deepcopy(facts)
    legacy.pop("breakdown")
    assert worker.helper_guard_snapshot_facts(legacy, guard.observer.source)


def _checked(guard: _GuardFixture, operation: Callable[[], Any]) -> Any:
    def original(backend: object, record: TransactionRecordV2) -> Any:
        return operation()

    return guard.observer.check(original)(guard.host.backend, guard.host.record())


async def test_fixed_fileproof_counts_actual_reads_hits_empty_files_and_failed_budget(
    helper_guard: _GuardFixture,
) -> None:
    guard = helper_guard
    guard.read_record(guard.host.control, guard.host.anchor)
    python = guard.root / "observed-python"
    python.write_bytes(b"abc")
    python.chmod(0o700)
    package = guard.root / "observed-package"
    package.mkdir(mode=0o700)
    (package / "empty").write_bytes(b"")
    (package / "payload").write_bytes(b"data")
    empty = guard.root / "empty-file"
    empty.write_bytes(b"")
    sentinel = object()

    def operation() -> object:
        proof = managed_launcher.python_fingerprint(python, 3)
        assert managed_launcher.python_fingerprint(python, 3, verified=proof) == proof
        invalid = {**proof, "digest": "z" * 64}
        assert managed_launcher.python_fingerprint(python, 3, verified=invalid) == proof
        with pytest.raises(managed_launcher.LaunchError, match="^managed_file_unsupported$"):
            managed_launcher.python_fingerprint(python, 2, verified=proof)
        observed: dict[str, dict[str, Any]] = {}
        limits = {"entries": 4, "depth": 1, "file_bytes": 4, "total_bytes": 4}
        package_proof = managed_launcher.package_fingerprint(package, limits, observed=observed)
        assert (
            managed_launcher.package_fingerprint(package, limits, verified=observed)
            == package_proof
        )
        empty_proof = managed_launcher.fingerprint(empty, 0)
        assert managed_launcher.fingerprint(empty, 0, verified=empty_proof) == empty_proof
        return sentinel

    assert _checked(guard, operation) is sentinel
    detail = guard.observer.breakdown()
    assert worker._helper_guard_breakdown_facts(detail)
    proofs = detail["owners"]["checks"]["fileproof"]
    assert proofs["python"] == {
        "entered": 4,
        "returned": 3,
        "raised": 1,
        "full_reads": 2,
        "full_read_bytes": 6,
        "proof_hits": 1,
        "proof_hit_bytes": 3,
        "raised_bytes": None,
    }
    assert proofs["package"] == {
        "entered": 4,
        "returned": 4,
        "raised": 0,
        "full_reads": 2,
        "full_read_bytes": 4,
        "proof_hits": 2,
        "proof_hit_bytes": 4,
        "raised_bytes": None,
    }
    assert proofs["other"] == {
        "entered": 2,
        "returned": 2,
        "raised": 0,
        "full_reads": 1,
        "full_read_bytes": 0,
        "proof_hits": 1,
        "proof_hit_bytes": 0,
        "raised_bytes": None,
    }


async def test_fixed_fileproof_partial_failure_keeps_exception_identity_and_unknown_bytes(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, failure = helper_guard, RuntimeError("PRIVATE partial read")
    path = guard.root / "partial-payload"
    path.write_bytes(b"abcdef")
    original_fdopen, read_bytes = os.fdopen, []

    class PartialReader:
        def __init__(self, stream: BinaryIO) -> None:
            self.stream = stream

        def __enter__(self) -> PartialReader:
            return self

        def __exit__(self, *args: Any) -> None:
            self.stream.close()

        def read(self, size: int) -> bytes:
            if read_bytes:
                raise failure
            data = self.stream.read(1)
            read_bytes.append(len(data))
            return data

    def operation() -> None:
        with managed_launcher.directory(path.parent) as parent:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                before = managed_launcher.identity(os.fstat(descriptor))

                def fdopen(current: int, *args: Any, **kwargs: Any) -> Any:
                    stream = original_fdopen(current, *args, **kwargs)
                    return (
                        PartialReader(cast(BinaryIO, stream)) if current == descriptor else stream
                    )

                with monkeypatch.context() as local:
                    local.setattr(os, "fdopen", fdopen)
                    with pytest.raises(RuntimeError) as caught:
                        managed_launcher._fingerprint_fd(
                            parent, path.name, path, 6, descriptor, before
                        )
                    assert caught.value is failure
                os.fstat(descriptor)  # The original caller still owns the live FD.
            finally:
                os.close(descriptor)

    _checked(guard, operation)
    proof = guard.observer.breakdown()["owners"]["checks"]["fileproof"]["other"]
    assert read_bytes == [1]
    assert proof == {
        "entered": 1,
        "returned": 0,
        "raised": 1,
        "full_reads": 0,
        "full_read_bytes": 0,
        "proof_hits": 0,
        "proof_hit_bytes": 0,
        "raised_bytes": None,
    }
    assert guard.observer.reason == guard.observer.detail_reason == ""


async def test_fixed_fileproof_adds_no_leaf_clocks_opens_or_identity_queries(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, clock_reads = helper_guard, []
    path = guard.root / "leaf-payload"
    path.write_bytes(b"actual bytes")
    raw = managed_launcher._fingerprint_fd.__wrapped__
    originals = {name: getattr(os, name) for name in ("open", "fstat", "stat", "fdopen")}
    calls: list[str] = []

    def clock() -> int:
        clock_reads.append(None)
        return len(clock_reads)

    def counted(name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return originals[name](*args, **kwargs)

        return call

    monkeypatch.setattr(
        worker,
        "time",
        SimpleNamespace(
            **(vars(worker.time) | {"perf_counter_ns": clock, "process_time_ns": clock})
        ),
    )

    def operation() -> None:
        with managed_launcher.directory(path.parent) as parent:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                before = managed_launcher.identity(os.fstat(descriptor))
                for name in originals:
                    monkeypatch.setattr(os, name, counted(name))
                calls.clear()
                first = raw(parent, path.name, path, 32, descriptor, before)
                expected = list(calls)
                os.lseek(descriptor, 0, os.SEEK_SET)
                calls.clear()
                clocks_before = len(clock_reads)
                second = managed_launcher._fingerprint_fd(
                    parent, path.name, path, 32, descriptor, before
                )
                assert second == first and calls == expected
                assert "open" not in calls and len(clock_reads) == clocks_before
                calls.clear()
                clocks_before = len(clock_reads)
                assert (
                    managed_launcher._fingerprint_fd(
                        parent, path.name, path, 32, descriptor, before, verified=first
                    )
                    == first
                )
                assert calls == expected and len(clock_reads) == clocks_before
            finally:
                for name, original in originals.items():
                    monkeypatch.setattr(os, name, original)
                os.close(descriptor)

    _checked(guard, operation)
    proof = guard.observer.breakdown()["owners"]["checks"]["fileproof"]["other"]
    assert proof["full_reads"] == proof["proof_hits"] == 1
    assert guard.observer.detail_reason == ""


@pytest.mark.parametrize("cancelled", [False, True])
async def test_fixed_sync_owners_preserve_method_return_exception_and_cancellation_identity(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    guard, observer = helper_guard, helper_guard.observer
    result = (guard.host.record(), guard.host.slots["base"], guard.host.slots)
    sentinel, calls = object(), []
    failure = asyncio.CancelledError("PRIVATE") if cancelled else RuntimeError("PRIVATE")

    class Target:
        def method(self, selected: object, *, fail: bool = False) -> object:
            calls.append((self, selected, fail))
            if fail:
                raise failure
            return sentinel

    original = Target.method
    monkeypatch.setattr(Target, "method", observer.detail_sync("record_canonical", original))
    target = Target()
    assert target.method.__self__ is target
    assert Target.method.__wrapped__ is original

    def record(
        control: JournalControl, anchor: DeploymentAnchor
    ) -> tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]]:
        assert target.method(sentinel) is sentinel
        return result

    assert observer.record(record)(guard.host.control, guard.host.anchor) is result
    before = copy.deepcopy(observer.breakdown()["owners"]["host_record"])
    assert observer.detail_owner is None
    await asyncio.sleep(0)
    assert target.method(sentinel) is sentinel
    assert observer.breakdown()["owners"]["host_record"] == before

    def check() -> None:
        with pytest.raises(type(failure)) as caught:
            target.method(sentinel, fail=True)
        assert caught.value is failure

    _checked(guard, check)
    detail = observer.breakdown()
    assert detail["owners"]["host_record"] == before
    assert detail["owners"]["host_record"]["record_canonical"]["returned"] == 1
    assert detail["owners"]["checks"]["record_canonical"]["raised"] == 1
    assert observer.detail_owner is None and observer.detail_role == "other"
    assert len(calls) == 3 and all(call[0] is target and call[1] is sentinel for call in calls)
    assert observer.reason == observer.detail_reason == ""


class _Reply(str):
    pass


async def test_fixed_rpc_preserves_runner_concurrency_return_identity_and_has_no_cpu(
    helper_guard: _GuardFixture,
) -> None:
    observer, reply = helper_guard.observer, _Reply("PRIVATE output")
    bus, manager = "org.freedesktop.systemd1", "/org/freedesktop/systemd1"
    unit_path = manager + "/unit/owned_2eservice"
    entered: list[str] = []
    unit_started, service_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def runner(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        assert kwargs["merge_stderr"] is False
        interface = next(
            (argument for argument in argv if argument in {bus + ".Unit", bus + ".Service"}), ""
        )
        entered.append(interface)
        if interface:
            (unit_started if interface.endswith(".Unit") else service_started).set()
            await release.wait()
        return {"exit_code": 0, "output": reply}

    probe = SystemdProbe(runner=runner)
    assert probe.runner is runner
    assert (
        await probe._command(
            "call", bus, manager, bus + ".Manager", "GetUnit", "s", "owned.service"
        )
        is reply
    )
    async with asyncio.TaskGroup() as tasks:
        unit = tasks.create_task(
            probe._command("get-property", bus, unit_path, bus + ".Unit", "Id")
        )
        service = tasks.create_task(
            probe._command("get-property", bus, unit_path, bus + ".Service", "MainPID")
        )
        await unit_started.wait()
        await service_started.wait()
        assert observer.breakdown() == {
            "version": 1,
            "code": "unavailable",
            "detail_reason": "in_flight",
        }
        release.set()
    assert unit.result() is reply and service.result() is reply
    assert await probe._command("unexpected", "PRIVATE argv") is reply
    detail = observer.breakdown()
    assert worker._helper_guard_breakdown_facts(detail)
    assert all(
        set(row) == {"entered", "returned", "raised", "wall_ns"}
        and row["entered"] == row["returned"] == 1
        and row["raised"] == 0
        and row["wall_ns"] >= 0
        for row in detail["rpc"].values()
    )
    assert len(entered) == 4 and probe.runner is runner
    assert "PRIVATE" not in json.dumps(detail)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_fixed_rpc_propagates_exact_failure_and_cancellation(
    helper_guard: _GuardFixture, cancelled: bool
) -> None:
    observer = helper_guard.observer
    failure = asyncio.CancelledError("PRIVATE") if cancelled else RuntimeError("PRIVATE")
    calls: list[list[str]] = []

    async def runner(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        raise failure

    with pytest.raises(type(failure)) as caught:
        await SystemdProbe(runner=runner)._command("unexpected")
    assert caught.value is failure and len(calls) == 1
    metric = observer.breakdown()["rpc"]["other"]
    assert metric["entered"] == metric["raised"] == 1 and metric["returned"] == 0
    assert observer.reason == observer.detail_reason == ""


@pytest.mark.parametrize("component", ["sync", "proof", "rpc", "snapshot"])
async def test_fixed_statistics_failure_never_changes_guard_or_rpc_result(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    guard, observer = helper_guard, helper_guard.observer
    failure = asyncio.CancelledError("PRIVATE statistics")

    def failed(*args: Any, **kwargs: Any) -> Any:
        raise failure

    method = {
        "sync": "finish_detail",
        "proof": "finish_fileproof",
        "rpc": "rpc_clock",
        "snapshot": "breakdown",
    }[component]
    monkeypatch.setattr(observer, method, failed)
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    assert record == guard.host.record()
    assert guard.host.backend._check(record) is None
    reply = _Reply("PRIVATE output")
    calls: list[list[str]] = []

    async def runner(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        return {"exit_code": 0, "output": reply}

    assert await SystemdProbe(runner=runner)._command("unexpected") is reply
    observer.save()
    facts = worker.read_json(guard.path)
    assert facts["code"] == "observed" and facts["breakdown"] == {
        "version": 1,
        "code": "unavailable",
        "detail_reason": "statistics_failed",
    }
    assert facts["host_record"]["returned"] == facts["checks"]["returned"] == 1
    assert worker.helper_guard_snapshot_facts(facts, observer.source)
    assert observer.reason == "" and len(calls) == 1 and "PRIVATE" not in json.dumps(facts)


@pytest.mark.parametrize("kind", ["sync", "rpc"])
async def test_fixed_detail_clock_failure_stays_local_to_the_optional_breakdown(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    guard, observer = helper_guard, helper_guard.observer

    def failed() -> int:
        raise asyncio.CancelledError("PRIVATE clock")

    with monkeypatch.context() as local:
        local.setattr(
            worker,
            "time",
            SimpleNamespace(**(vars(worker.time) | {"perf_counter_ns": failed})),
        )
        assert (observer.detail_clock() if kind == "sync" else observer.rpc_clock()) is None
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    assert record == guard.host.record() and guard.host.backend._check(record) is None
    observer.save()
    facts = worker.read_json(guard.path)
    assert facts["breakdown"] == {
        "version": 1,
        "code": "unavailable",
        "detail_reason": "clock_invalid",
    }
    assert worker.helper_guard_snapshot_facts(facts, observer.source)
    assert observer.reason == ""


@pytest.mark.parametrize(
    "damage",
    [
        "extra_owner",
        "missing_bucket",
        "boolean",
        "count",
        "negative",
        "oversized",
        "raised_bytes",
        "empty_bytes",
        "rpc_cpu",
        "private_bytes",
        "unavailable_reason",
    ],
)
async def test_fixed_breakdown_rejects_forged_or_unbounded_fields(
    helper_guard: _GuardFixture, damage: str
) -> None:
    guard = helper_guard
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    facts = worker.read_json(guard.path)
    assert worker.helper_guard_snapshot_facts(facts, guard.observer.source)
    detail = facts["breakdown"]
    owner = detail["owners"]["checks"]
    proof = owner["fileproof"]["other"]
    if damage == "extra_owner":
        detail["owners"]["foreign"] = {}
    elif damage == "missing_bucket":
        owner.pop("private_record")
    elif damage == "boolean":
        proof["entered"] = True
    elif damage == "count":
        proof["entered"] += 1
    elif damage == "negative":
        detail["rpc"]["other"]["wall_ns"] = -1
    elif damage == "oversized":
        detail["rpc"]["other"]["wall_ns"] = 2**63
    elif damage == "raised_bytes":
        proof["raised_bytes"] = 0
    elif damage == "empty_bytes":
        proof.update({key: 0 for key in proof if key != "raised_bytes"})
        proof["full_read_bytes"] = 1
    elif damage == "rpc_cpu":
        detail["rpc"]["other"]["cpu_ns"] = 0
    elif damage == "private_bytes":
        owner["private_record"]["raised"] = owner["private_record"]["entered"]
        owner["private_record"]["returned"] = 0
    else:
        facts["breakdown"] = {"version": 1, "code": "unavailable", "detail_reason": ["PRIVATE"]}
    assert not worker.helper_guard_snapshot_facts(facts, guard.observer.source)
    facts.pop("breakdown")
    assert worker.helper_guard_snapshot_facts(facts, guard.observer.source)


async def test_fixed_forged_detail_cannot_erase_previously_accepted_receipt(
    helper_guard: _GuardFixture,
) -> None:
    guard = helper_guard
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    result, retained = _guard_result(guard), []
    sources = [{"source": dict(guard.observer.source), "observation": None}]
    worker.collect_helper_guard_costs(guard.root, result, sources, retained, deadline=None)
    accepted = copy.deepcopy(retained)
    facts = worker.read_json(guard.path)
    facts["breakdown"]["rpc"]["other"]["cpu_ns"] = 0
    guard.path.write_text(json.dumps(facts), encoding="utf-8")
    worker.collect_helper_guard_costs(guard.root, result, sources, retained, deadline=None)
    assert retained == accepted
    assert result["resources"]["helper_guard_costs"]["sources"] == accepted


async def test_fixed_detail_yields_snapshot_budget_without_extra_publications(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, snapshots = helper_guard, []
    original = Path.replace

    def replace(path: Path, target: Path) -> Path:
        value = original(path, target)
        if target == guard.path:
            snapshots.append(worker.read_json(target))
        return value

    monkeypatch.setattr(Path, "replace", replace)
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    guard.host.backend._check(guard.host.install_intent())
    legacy = copy.deepcopy(snapshots[-1])
    legacy.pop("breakdown")
    legacy.pop("startup", None)
    lowered = len(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()) + 128
    assert lowered < worker.MAX_BYTES
    monkeypatch.setattr(worker, "MAX_BYTES", lowered)
    guard.observer.save()
    guard.observer.save()
    assert [row["boundary"] for row in snapshots] == [*worker.HELPER_GUARD_PREFIXES, "normal_exit"]
    assert all("breakdown" in row for row in snapshots[:2])
    final = snapshots[-1]
    assert "breakdown" not in final and "startup" not in final and final["checks"]["returned"] == 2
    assert final["attempts"] == {"prefix": 2, "final": 1}
    assert final["prior_publications"]["count"] == 2
    assert len(guard.path.read_bytes()) <= lowered
    assert worker.helper_guard_snapshot_facts(final, guard.observer.source)


async def test_fixed_detail_yields_aggregate_budget_before_original_guard_receipt(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = helper_guard
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    result = _guard_result(guard)
    worker.collect_helper_guard_costs(
        guard.root,
        result,
        [{"source": dict(guard.observer.source), "observation": None}],
        [],
        deadline=None,
    )
    expected = copy.deepcopy(result)
    expected["resources"]["helper_guard_costs"]["sources"][0]["costs"].pop("breakdown")
    expected["resources"]["helper_guard_costs"]["sources"][0]["costs"].pop("startup", None)
    lowered = len(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode())
    assert lowered < worker.MAX_BYTES
    monkeypatch.setattr(worker, "MAX_BYTES", lowered)
    worker.trim_binding_diagnostics(result)
    assert result == expected
    assert worker.helper_guard_costs_facts(result["resources"]["helper_guard_costs"], result)
