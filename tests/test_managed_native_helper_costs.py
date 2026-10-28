"""Offline hooks; only a fresh private Python child exercises kill, never a manager."""

from __future__ import annotations

import asyncio
import atexit
import builtins
import copy
import json
import os
import runpy
import select
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from importlib.machinery import ModuleSpec
from pathlib import Path
from subprocess import PIPE, Popen
from types import CoroutineType, FrameType, FunctionType, SimpleNamespace
from typing import Any, Literal, cast

import pytest
import test_managed_backend as backend_fixture
from helpers import managed_native_worker as worker
from test_managed_native_ci import ci
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_management_helper import Probe as HelperProbe

import muli
import muli.managed_host as managed_host
import muli.platforms.filesystem_identity as filesystem_identity
import muli.platforms.managed_backend as managed_backend
import muli.platforms.managed_deployment as managed_deployment
from muli.managed_controller import ManagedTransactionController
from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import JournalControl, Role, TransactionError, TransactionJournal
from muli.platforms.linux_transaction import ProbeLimits, SystemdProbe, UnitSnapshot
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore, SlotIdentity
from muli.platforms.managed_probe import ManagedSystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated helper fixture")


@pytest.mark.parametrize("include_host_record", [False, True])
async def test_generated_managed_helper_hook_measures_real_checks_without_changing_their_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    include_host_record: bool,
) -> None:
    await _exercise_generated_hook(tmp_path, monkeypatch, fixture, include_host_record)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_native_host_record_observer_preserves_actual_failure_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    cancelled: bool,
) -> None:
    failure = (
        asyncio.CancelledError("private exception body")
        if cancelled
        else RuntimeError("private exception body")
    )
    await _exercise_generated_hook(tmp_path, monkeypatch, fixture, True, host_failure=failure)


async def test_generated_hook_measures_the_actual_runpy_main_host_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
) -> None:
    await _exercise_generated_hook(tmp_path, monkeypatch, fixture, True, through_main=True)


@pytest.mark.parametrize(
    "binding_case",
    [
        "missing",
        "existing",
        "superseded",
        "wrong_origin",
        "replaced_main",
        "replaced_entry",
        "error",
        "cancelled",
    ],
)
async def test_unbound_main_observer_never_exports_zero_host_cost_or_changes_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    binding_case: Literal[
        "missing",
        "existing",
        "superseded",
        "wrong_origin",
        "replaced_main",
        "replaced_entry",
        "error",
        "cancelled",
    ],
) -> None:
    await _exercise_generated_hook(
        tmp_path, monkeypatch, fixture, True, through_main=True, binding_case=binding_case
    )


async def _exercise_generated_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    include_host_record: bool,
    *,
    host_failure: BaseException | None = None,
    through_main: bool = False,
    binding_case: Literal[
        "missing",
        "existing",
        "superseded",
        "wrong_origin",
        "replaced_main",
        "replaced_entry",
        "error",
        "cancelled",
    ]
    | None = None,
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
        (managed_host, "_record"),
        (ManagedLinuxBackend, "_check"),
        (ManagedLinuxBackend, "_request"),
        (ManagedLinuxBackend, "install"),
        (ManagedLinuxBackend, "start"),
        (ManagedLinuxBackend, "_command"),
        (SystemdProbe, "_command"),
        (managed_deployment, "verify_slot_files"),
        (managed_backend, "verify_slot_files"),
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
    original_profile = sys.getprofile()

    def foreign_profile(frame: FrameType, event: str, argument: object) -> None:
        pass

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
                    (
                        list(helper_argv)
                        if through_main
                        else [executable, "-I", "-m", "muli.management_helper", str(journal_root)]
                    ),
                )
                monkeypatch.setattr(sys, "argv", list(helper_argv[3:]))
                monkeypatch.setattr(atexit, "register", register)
                if not through_main:
                    # Explicit canonical-only offline entry: it exercises real
                    # files/backend calls, but makes no runpy binding claim.
                    monkeypatch.setattr(sys, "argv", ["muli.management_helper", str(journal_root)])
                if binding_case == "existing":
                    sys.setprofile(foreign_profile)
                if binding_case == "wrong_origin":
                    spec = managed_host.__spec__
                    assert spec is not None
                    monkeypatch.setattr(
                        managed_host,
                        "__spec__",
                        ModuleSpec(spec.name, spec.loader, origin="<foreign-host-source>"),
                    )
                if binding_case in {"replaced_main", "replaced_entry"}:

                    def replaced(*args: Any, **kwargs: Any) -> None:
                        raise AssertionError("canonical replacement must never be invoked")

                    monkeypatch.setattr(
                        managed_host,
                        "main" if binding_case == "replaced_main" else "_entry",
                        replaced,
                    )
                diagnostic_failures: list[BaseException] = []
                if binding_case in {"error", "cancelled"}:
                    original_require = worker.require
                    diagnostic_failure = (
                        asyncio.CancelledError("private observer body")
                        if binding_case == "cancelled"
                        else RuntimeError("private observer body")
                    )

                    def fail_binding(condition: bool, code: str) -> None:
                        if code == "native_profile_main_unconfirmed":
                            diagnostic_failures.append(diagnostic_failure)
                            raise diagnostic_failure
                        original_require(condition, code)

                    monkeypatch.setattr(worker, "require", fail_binding)
                # This archived broad-observer positive control owns its profiler.
                monkeypatch.setattr(worker, "install_helper_guard_cost_hook", lambda: None)
                exec(compile(hook, "<fixture-helper-hook>", "exec"), {})
                # 成本正控显式安装保留的观察器；托管生成 hook 不再安装它。
                worker.install_helper_cost_hook()
                if not through_main or binding_case == "existing":
                    assert sys.getprofile() is (
                        foreign_profile if binding_case == "existing" else original_profile
                    )
                elif binding_case != "superseded":
                    waiting_profile = sys.getprofile()
                    assert waiting_profile is not None and waiting_profile is not original_profile

                    def main() -> None:
                        # Another module's same-named function must not bind
                        # or end the real -m entry's one-shot observation.
                        pass

                    main()
                    assert sys.getprofile() is waiting_profile
                if binding_case == "superseded":
                    assert sys.getprofile() is not foreign_profile
                    sys.setprofile(foreign_profile)
                if include_host_record:
                    observed_directories: list[tuple[Path, ...]] = []

                    def filesystems(directories: Sequence[Path]) -> None:
                        # Only mount admission is controlled, as in the real
                        # host fixture; _record's retained-file checks delegate.
                        observed_directories.append(tuple(directories))

                    monkeypatch.setattr(managed_host, "verify_local_filesystems", filesystems)
                    # runpy imports this function afresh into the real __main__
                    # namespace; only mount admission remains controlled there.
                    monkeypatch.setattr(
                        filesystem_identity, "verify_local_filesystems", filesystems
                    )
                    journal.database.write_bytes(b"")
                    journal.database.chmod(0o600)
                    if through_main and binding_case != "missing":
                        assert host_failure is None
                        authenticated_records: list[TransactionRecordV2] = []

                        def run_at_service_boundary(coroutine: object) -> TransactionRecordV2:
                            # Execute the actual -m module and main, stopping
                            # only where it would enter real systemd operations.
                            assert isinstance(coroutine, CoroutineType)
                            try:
                                frame = coroutine.cr_frame
                                assert frame is not None
                                namespace = frame.f_globals
                                assert namespace is sys.modules["__main__"].__dict__
                                assert namespace is not managed_host.__dict__
                                assert namespace["__name__"] == "__main__"
                                assert namespace["__spec__"].name == "muli.managed_host"
                                assert sys.getprofile() is (
                                    foreign_profile
                                    if binding_case in {"existing", "superseded"}
                                    else original_profile
                                )
                                helper = namespace["run_managed_helper"]
                                assert isinstance(helper, FunctionType)
                                assert helper.__code__ is coroutine.cr_code
                                read_record = cast(
                                    Callable[
                                        [JournalControl, DeploymentAnchor],
                                        tuple[
                                            TransactionRecordV2,
                                            SlotIdentity,
                                            dict[Role, SlotIdentity],
                                        ],
                                    ],
                                    namespace["_record"],
                                )
                                current, controller, slots = read_record(control, host.anchor)
                                assert current == record
                                assert controller == host.slots["base"] and slots == host.slots
                                authenticated_records.append(current)
                                return current
                            finally:
                                coroutine.close()

                        with monkeypatch.context() as local:
                            local.setattr(asyncio, "run", run_at_service_boundary)
                            with pytest.warns(RuntimeWarning, match="found in sys.modules"):
                                with pytest.raises(SystemExit) as exited:
                                    runpy.run_module(
                                        "muli.managed_host", run_name="__main__", alter_sys=True
                                    )
                            assert exited.value.code == 0
                        assert authenticated_records == [record]
                    elif host_failure is None:
                        # This existing fixture deliberately exercises the
                        # canonical import only, not Python's -m entry point.
                        current, controller, slots = managed_host._record(control, host.anchor)
                        assert current == record
                        assert controller == host.slots["base"] and slots == host.slots
                    else:
                        original_anchor = DeploymentStore.verify_anchor
                        authenticated: list[DeploymentAnchor] = []

                        def failing_anchor(
                            store: DeploymentStore, anchor: DeploymentAnchor
                        ) -> None:
                            original_anchor(store, anchor)
                            authenticated.append(anchor)
                            assert host_failure is not None
                            raise host_failure

                        with monkeypatch.context() as local:
                            local.setattr(DeploymentStore, "verify_anchor", failing_anchor)
                            with pytest.raises(type(host_failure)) as caught:
                                managed_host._record(control, host.anchor)
                            assert caught.value is host_failure
                        assert authenticated == [host.anchor]
                    assert observed_directories == [
                        (
                            journal.directory.path,
                            Path(host.anchor.config_path).parent,
                            journal.database.parent,
                            journal.writer_directory.path,
                        )
                    ]
                assert host.backend._check(record) is None
                stale = record.model_copy(update={"revision": record.revision + 1})
                with pytest.raises(TransactionError, match="^stale_revision$"):
                    host.backend._check(stale)
                path = root / "helper-costs.json"
                assert not path.exists()  # No per-check filesystem observation.
                assert len(callbacks) == 1
                callbacks[0]()
                callbacks[0]()  # The same atexit callback is a single write attempt.
                if binding_case is not None:
                    assert not path.exists(), "未绑定真实main时不得发布observed零成本"
                    assert not (root / "helper-costs-checkpoint.json").exists()
                    assert not (root / "helper-entry-checkpoint.json").exists()
                    assert sys.getprofile() is (
                        foreign_profile
                        if binding_case in {"existing", "superseded"}
                        else original_profile
                    )
                    if binding_case in {"error", "cancelled"}:
                        assert diagnostic_failures == [diagnostic_failure]
                    else:
                        assert diagnostic_failures == []
                    return
                facts = json.loads(path.read_text(encoding="utf-8"))
                entry_path = root / "helper-entry-checkpoint.json"
                if through_main:
                    assert worker.read_json(entry_path)["boundary"] == "main_entered"
                else:
                    assert not entry_path.exists(), (
                        "canonical-only calls cannot establish main entry"
                    )
                assert "private exception body" not in path.read_text(encoding="utf-8")
                if include_host_record:
                    assert "host_record" in facts, (
                        "the helper authenticated real retained files outside backend._check, "
                        "but its native costs omitted that host authentication"
                    )
                    assert facts["host_record"]["count"] == 1
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
                    "host_record",
                    "check_breakdown",
                    "slot_files_controller",
                    "slot_files_selected",
                }
                assert facts["version"] == 3 and facts["code"] == "observed"
                assert facts["generation"] == record.generation
                assert facts["helper"] == managed_helper_unit_name(record)
                assert (facts["uid"], facts["pid"], facts["invocation"]) == (
                    os.geteuid(),
                    os.getpid(),
                    invocation,
                )
                assert facts["checks"]["count"] == 2
                for name in (
                    "checks",
                    "bus",
                    "http",
                    "host_record",
                    "slot_files_controller",
                    "slot_files_selected",
                ):
                    assert set(facts[name]) == {"count", "total_ms"}
                    assert all(type(value) is int and value >= 0 for value in facts[name].values())
                assert facts["bus"]["count"] == facts["http"]["count"] == 0
                assert facts["host_record"]["count"] == int(include_host_record)
                assert facts["slot_files_controller"]["count"] == 1
                assert facts["slot_files_selected"] == {"count": 0, "total_ms": 0}
                assert facts["check_breakdown"] == [
                    {
                        "phase": record.phase,
                        "intent": record.intent,
                        "count": 2,
                        "total_ms": facts["checks"]["total_ms"],
                    }
                ]
                source = {
                    key: facts[key] for key in ("generation", "helper", "uid", "pid", "invocation")
                }
                assert worker.helper_costs_facts(facts, source)
                assert path.stat().st_mode & 0o077 == 0
            finally:
                # These tests own their injected profilers; the hook itself
                # must never clear the foreign owner asserted above.
                sys.setprofile(original_profile)
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
        (managed_host, "_record"),
        (ManagedLinuxBackend, "_check"),
        (ManagedLinuxBackend, "_request"),
        (ManagedLinuxBackend, "install"),
        (ManagedLinuxBackend, "start"),
        (ManagedLinuxBackend, "_command"),
        (SystemdProbe, "_command"),
        (managed_deployment, "verify_slot_files"),
        (managed_backend, "verify_slot_files"),
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
                # This offline install fixture has no runpy main; retain the
                # legacy observation entry, not a fabricated main-bound flag.
                monkeypatch.setattr(
                    sys,
                    "orig_argv",
                    [argv[0], "-I", "-m", "muli.management_helper", str(journal_root)],
                )
                monkeypatch.setattr(sys, "argv", ["muli.management_helper", str(journal_root)])
                monkeypatch.setattr(atexit, "register", register)
                exec(compile(hook, "<fixture-helper-hook>", "exec"), {})
                worker.install_helper_cost_hook()
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


def costs_result(*, version: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    source = {
        "generation": "a" * 32,
        "helper": "muli-transaction-" + "b" * 64 + ".service",
        "uid": os.geteuid(),
        "pid": os.getpid(),
        "invocation": "c" * 32,
    }
    facts = {
        "version": version,
        "code": "observed",
        **source,
        "checks": {"count": 3, "total_ms": 123},
        "bus": {"count": 2, "total_ms": 45},
        "http": {"count": 0, "total_ms": 0},
    }
    if version in (2, 3):
        facts.update(
            host_record={"count": 2, "total_ms": 87},
            check_breakdown=[{"phase": "prepared", "intent": "none", "count": 3, "total_ms": 123}],
        )
    if version == 3:
        facts.update(
            slot_files_controller={"count": 3, "total_ms": 60},
            slot_files_selected={"count": 2, "total_ms": 30},
        )
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


@pytest.mark.parametrize("version", [1, 2, 3])
def test_costs_are_collected_once_from_private_file_and_bound_to_helper(
    tmp_path: Path, version: int
) -> None:
    result, facts = costs_result(version=version)
    worker.write_json(tmp_path / "helper-costs.json", facts)
    worker.collect_helper_costs(tmp_path, result)
    assert result["resources"]["helper_costs"] == facts
    assert ci.fixture_result(json.dumps(result).encode()) == result


def test_cost_schema_reader_does_not_import_the_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__
    forbidden: list[str] = []

    def checked_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> Any:
        if name == "muli" or name.startswith("muli."):
            forbidden.append(name)
            raise AssertionError("coordinator reader imported the application")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    for version in (1, 2, 3):
        result, facts = costs_result(version=version)
        assert worker.helper_costs_facts(facts, result["resources"]["helper_source"])
        result["resources"]["helper_costs"] = facts
        assert ci.fixture_result(json.dumps(result).encode()) == result
    assert forbidden == []


@pytest.mark.parametrize(
    "damage",
    [
        "host_bool",
        "host_extra",
        "host_missing",
        "unknown_phase",
        "unknown_intent",
        "duplicate_bucket",
        "count_mismatch",
        "time_overflow",
        "row_extra",
        "zero_bucket",
        "too_many_buckets",
        "version_downgrade",
    ],
)
def test_version_two_costs_reject_non_contract_host_and_phase_facts(damage: str) -> None:
    result, facts = costs_result(version=2)
    rows = facts["check_breakdown"]
    if damage == "host_bool":
        facts["host_record"]["count"] = True
    elif damage == "host_extra":
        facts["host_record"]["path"] = "/must-not-export"
    elif damage == "host_missing":
        facts.pop("host_record")
    elif damage == "unknown_phase":
        rows[0]["phase"] = "private exception body"
    elif damage == "unknown_intent":
        rows[0]["intent"] = "unknown"
    elif damage == "duplicate_bucket":
        rows.append(copy.deepcopy(rows[0]))
        facts["checks"]["count"] *= 2
        facts["checks"]["total_ms"] *= 2
    elif damage == "count_mismatch":
        rows[0]["count"] += 1
    elif damage == "time_overflow":
        rows[0]["total_ms"] += 1
    elif damage == "row_extra":
        rows[0]["exception"] = "private exception body"
    elif damage == "zero_bucket":
        rows[0]["count"] = facts["checks"]["count"] = 0
    elif damage == "too_many_buckets":
        facts["check_breakdown"] *= (
            len(worker.HELPER_COST_PHASES) * len(worker.HELPER_COST_INTENTS) + 1
        )
    else:
        facts["version"] = 1
    result["resources"]["helper_costs"] = facts
    assert not worker.helper_costs_facts(facts, result["resources"]["helper_source"])
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(result).encode())


@pytest.mark.parametrize(
    "damage",
    [
        "missing_controller",
        "missing_selected",
        "controller_bool",
        "selected_negative",
        "metric_extra",
        "time_outside_checks",
        "controller_count",
        "selected_count",
        "downgrade",
        "unknown_bucket",
    ],
)
def test_version_three_costs_reject_non_contract_slot_facts(damage: str) -> None:
    result, facts = costs_result(version=3)
    if damage == "missing_controller":
        facts.pop("slot_files_controller")
    elif damage == "missing_selected":
        facts.pop("slot_files_selected")
    elif damage == "controller_bool":
        facts["slot_files_controller"]["count"] = True
    elif damage == "selected_negative":
        facts["slot_files_selected"]["total_ms"] = -1
    elif damage == "metric_extra":
        facts["slot_files_selected"]["path"] = "/must-not-export"
    elif damage == "time_outside_checks":
        facts["slot_files_selected"]["total_ms"] = facts["checks"]["total_ms"]
    elif damage == "controller_count":
        facts["slot_files_controller"]["count"] = facts["checks"]["count"] + 1
    elif damage == "selected_count":
        facts["slot_files_selected"]["count"] = facts["slot_files_controller"]["count"] + 1
    elif damage == "downgrade":
        facts["version"] = 2
    else:
        facts["slot_files_other"] = {"count": 0, "total_ms": 0}
    result["resources"]["helper_costs"] = facts
    assert not worker.helper_costs_facts(facts, result["resources"]["helper_source"])
    with pytest.raises(ci.FixtureError, match="^native_result_invalid$"):
        ci.fixture_result(json.dumps(result).encode())


@pytest.mark.parametrize("version", [1, 2, 3])
def test_partial_cost_sample_never_displaces_existing_facts_at_the_original_byte_limit(
    tmp_path: Path, version: int
) -> None:
    # This is a final-settlement reader/budget fixture, not a claim that a
    # controller ran. The real hook/install/Case path is exercised above.
    result, facts = costs_result(version=version)
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


@pytest.mark.parametrize("version", [1, 2, 3])
def test_coordinator_manager_merge_discards_only_partial_costs_at_the_original_byte_limit(
    fixture: tuple[Any, Any], version: int
) -> None:
    native, commands = fixture
    native.prepare()
    readiness = copy.deepcopy(native.manager_readiness)
    assert readiness == {"elapsed_ms": 0, "late_ready": False}

    # Valid reader materials stand in for an already settled worker reply;
    # collection itself uses the real coordinator and Commands' fixed cat seam.
    result, facts = costs_result(version=version)
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


@pytest.mark.parametrize("version", [1, 2, 3])
def test_costs_do_not_displace_existing_result_at_original_byte_limit(
    tmp_path: Path, version: int
) -> None:
    result, facts = costs_result(version=version)
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
    originals = (
        ManagedLinuxBackend._check,
        ManagedLinuxBackend.start,
        ManagedLinuxBackend._command,
        SystemdProbe._command,
        ManagedLinuxBackend._request,
        managed_host._record,
        managed_deployment.verify_slot_files,
        managed_backend.verify_slot_files,
    )
    monkeypatch.setattr(atexit, "register", lambda callback: callbacks.append(callback))
    monkeypatch.setattr(
        sys, "orig_argv", [sys.executable, "-I", "-m", "muli.management_helper", "/wrong"]
    )
    worker.install_helper_cost_hook()
    assert callbacks == []
    assert (
        ManagedLinuxBackend._check,
        ManagedLinuxBackend.start,
        ManagedLinuxBackend._command,
        SystemdProbe._command,
        ManagedLinuxBackend._request,
        managed_host._record,
        managed_deployment.verify_slot_files,
        managed_backend.verify_slot_files,
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

    originals = (
        ManagedLinuxBackend._check,
        ManagedLinuxBackend.start,
        ManagedLinuxBackend._command,
        SystemdProbe._command,
        ManagedLinuxBackend._request,
        managed_host._record,
        managed_deployment.verify_slot_files,
        managed_backend.verify_slot_files,
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", executable)
    monkeypatch.setattr(sys, "orig_argv", arguments)
    monkeypatch.setattr(worker, "fixture", fixture_access)
    monkeypatch.setattr(atexit, "register", lambda callback: callbacks.append(callback))
    worker.install_helper_cost_hook()
    assert accesses == [] and callbacks == []
    assert (
        ManagedLinuxBackend._check,
        ManagedLinuxBackend.start,
        ManagedLinuxBackend._command,
        SystemdProbe._command,
        ManagedLinuxBackend._request,
        managed_host._record,
        managed_deployment.verify_slot_files,
        managed_backend.verify_slot_files,
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


@pytest.mark.parametrize("version", [1, 2, 3])
def test_cost_history_never_displaces_existing_result_at_the_original_byte_limit(
    tmp_path: Path, version: int
) -> None:
    result, facts = costs_result(version=version)
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


class _SlotCostClock:
    """Charge deterministic time only to real, delegated slot verification."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.fail_on_slot_exit = False
        self.fail_next = False
        self.calls: list[tuple[str, SlotIdentity, dict[str, Any]]] = []
        self.errors: list[BaseException] = []

    def __call__(self) -> float:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("private slot timing failure")
        return self.elapsed

    def observe(self, name: str, original: Callable[..., None]) -> Callable[..., None]:
        def verify(*args: Any, **kwargs: Any) -> None:
            slot = args[0] if args else kwargs["slot"]
            assert isinstance(slot, SlotIdentity)
            self.calls.append((name, slot, dict(kwargs)))
            try:
                return original(*args, **kwargs)
            except BaseException as error:
                self.errors.append(error)
                raise
            finally:
                # Binary fractions avoid millisecond truncation ambiguity.
                self.elapsed += 0.125 if name == "controller" else 0.25
                if self.fail_on_slot_exit:
                    self.fail_next = True

        return verify


@pytest.fixture
async def slot_cost_observer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
) -> AsyncIterator[
    tuple[backend_fixture.Host, _SlotCostClock, Callable[[], dict[str, Any] | None]]
]:
    native, _ = fixture
    native.prepare()
    hook = (native.volume / "_muli_native_ci_hook.py").read_text(encoding="utf-8")
    root = tmp_path / "slot-cost-helper"
    root.mkdir(mode=0o700)
    original_slot = backend_fixture._slot

    def slot(directory: Path, name: str) -> SlotIdentity:
        return original_slot(directory, {"base": "a", "target": "b"}[name])

    monkeypatch.setattr(backend_fixture, "_slot", slot)
    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    callbacks: list[Callable[[], Any]] = []

    def register(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[..., Any]:
        callbacks.append(partial(function, *args, **kwargs))
        return function

    for owner, name in (
        (managed_host, "_record"),
        (ManagedLinuxBackend, "_check"),
        (ManagedLinuxBackend, "_request"),
        (ManagedLinuxBackend, "install"),
        (ManagedLinuxBackend, "start"),
        (ManagedLinuxBackend, "_command"),
        (SystemdProbe, "_command"),
        (managed_deployment, "verify_slot_files"),
        (managed_backend, "verify_slot_files"),
    ):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    worker.write_json(
        root / "fixture.json",
        {
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
        },
    )
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    journal_root = root / "data/management/transaction-v1"
    with TransactionJournal(journal_root, root / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = backend_fixture.Host(root, journal, control, "switch", change_config=False)
            try:
                # _record still authenticates actual retained files; only mount
                # admission is controlled, as in the existing host fixture.
                monkeypatch.setattr(managed_host, "verify_local_filesystems", lambda paths: None)
                journal.database.write_bytes(b"")
                journal.database.chmod(0o600)
                argv = managed_helper_arguments(host.anchor, host.slots["base"])
                monkeypatch.setattr(sys, "prefix", str(root / "a"))
                monkeypatch.setattr(sys, "executable", argv[0])
                monkeypatch.setattr(
                    sys,
                    "orig_argv",
                    [argv[0], "-I", "-m", "muli.management_helper", str(journal_root)],
                )
                monkeypatch.setattr(sys, "argv", ["muli.management_helper", str(journal_root)])
                monkeypatch.setattr(atexit, "register", register)
                clock = _SlotCostClock()
                for module, name in (
                    (managed_deployment, "controller"),
                    (managed_backend, "selected"),
                ):
                    monkeypatch.setattr(
                        module, "verify_slot_files", clock.observe(name, module.verify_slot_files)
                    )
                # Only the worker's observation clock changes; pytest, the
                # actual backend and its deadlines retain their real clocks.
                monkeypatch.setattr(
                    worker, "time", SimpleNamespace(**(vars(worker.time) | {"perf_counter": clock}))
                )
                exec(compile(hook, "<fixture-slot-cost-hook>", "exec"), {})
                worker.install_helper_cost_hook()
                assert len(callbacks) == 1
                assert clock.calls == []

                def save() -> dict[str, Any] | None:
                    callbacks[0]()
                    path = root / "helper-costs.json"
                    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

                yield host, clock, save
            finally:
                host.release_writer()
                await host.backend.__aexit__()


async def test_native_slot_costs_exclude_host_authentication_and_measure_both_real_bindings(
    slot_cost_observer: tuple[
        backend_fixture.Host, _SlotCostClock, Callable[[], dict[str, Any] | None]
    ],
) -> None:
    host, clock, save = slot_cost_observer
    record = host.record()
    assert host.slots["base"].digest != host.slots["target"].digest
    current, controller, slots = managed_host._record(host.control, host.anchor)
    assert current == record and controller == host.slots["base"] and slots == host.slots
    observed_packages: dict[str, Any] = {}
    assert host.backend._check(record, "target", observed_packages=observed_packages) is None
    assert len(observed_packages) == 2  # Both complete real package scans delegated.
    stale = record.model_copy(update={"revision": record.revision + 1})
    with pytest.raises(TransactionError, match="^stale_revision$"):
        host.backend._check(stale, "target")
    assert managed_host._record(host.control, host.anchor)[0] == record
    assert [name for name, _, _ in clock.calls] == [
        "controller",
        "controller",
        "selected",
        "controller",
    ]
    for name, actual, kwargs in clock.calls[1:3]:
        assert actual == host.slots["base" if name == "controller" else "target"]
        assert kwargs["observed_packages"] is observed_packages
    facts = save()
    assert facts is not None and facts["code"] == "observed"
    assert facts["checks"] == {"count": 2, "total_ms": 375}
    assert facts["host_record"] == {"count": 2, "total_ms": 250}
    assert facts.get("slot_files_controller") == {"count": 1, "total_ms": 125}, (
        "complete controller verification inside _check is missing or includes host._record"
    )
    assert facts.get("slot_files_selected") == {"count": 1, "total_ms": 250}, (
        "the separately bound selected-slot verifier was not measured"
    )
    assert facts["version"] == 3
    source = {key: facts[key] for key in ("generation", "helper", "uid", "pid", "invocation")}
    result = {
        "completed": False,
        "stage": "transaction",
        "code": "TimeoutError",
        "timeline": [],
        "generations": [
            {
                "deployment": host.anchor.digest,
                "generation": record.generation,
                "helper": facts["helper"],
            }
        ],
        "resources": {"helper_source": source},
    }
    retained: list[dict[str, Any]] = []
    root = Path(host.slots["base"].prefix).parent
    worker.collect_helper_costs(
        root, result, completed_generation=record.generation, retained=retained
    )
    partial = {"sample": {"kind": "install_returned", "revision": record.revision}, "costs": facts}
    # Reader-only partial fixture; actual install-returned publication retains
    # its separate real-install regression above.
    assert worker.helper_cost_sample_facts(partial, source)
    samples = [{"source": source, **partial}]
    worker.collect_helper_costs(root, result, retained=retained, samples=samples)
    assert result["resources"]["helper_costs"] == facts
    assert result["resources"]["helper_cost_history"] == [{"source": source, "costs": facts}]
    assert result["resources"]["helper_cost_samples"] == samples
    assert ci.fixture_result(json.dumps(result).encode()) == result


async def test_native_slot_costs_preserve_the_actual_bad_target_error_once(
    slot_cost_observer: tuple[
        backend_fixture.Host, _SlotCostClock, Callable[[], dict[str, Any] | None]
    ],
) -> None:
    host, clock, save = slot_cost_observer
    target = Path(host.slots["target"].package.root) / "__init__.py"
    target.write_bytes(b"# actual changed target package\n")
    with pytest.raises(
        managed_deployment.ManagedDeploymentError, match="^managed_slot_changed$"
    ) as caught:
        host.backend._check(host.record(), "target")
    assert clock.errors == [caught.value]
    assert clock.errors[0] is caught.value
    assert [name for name, _, _ in clock.calls] == ["controller", "selected"]
    assert managed_host._record(host.control, host.anchor)[0] == host.record()
    facts = save()
    assert facts is not None and facts["checks"] == {"count": 1, "total_ms": 375}
    assert facts.get("slot_files_controller") == {"count": 1, "total_ms": 125}
    assert facts.get("slot_files_selected") == {"count": 1, "total_ms": 250}, (
        "failed real selected verification must be timed once without replacing its exception"
    )


@pytest.mark.parametrize("bad_target", [False, True])
async def test_native_slot_costs_timer_failure_never_changes_authentication_or_exports_zero(
    slot_cost_observer: tuple[
        backend_fixture.Host, _SlotCostClock, Callable[[], dict[str, Any] | None]
    ],
    bad_target: bool,
) -> None:
    host, clock, save = slot_cost_observer
    if bad_target:
        target = Path(host.slots["target"].package.root) / "__init__.py"
        target.write_bytes(b"# actual changed target package\n")
    clock.fail_on_slot_exit = True
    if bad_target:
        with pytest.raises(
            managed_deployment.ManagedDeploymentError, match="^managed_slot_changed$"
        ) as caught:
            host.backend._check(host.record(), "target")
        assert clock.errors == [caught.value]
        assert clock.errors[0] is caught.value
    else:
        assert host.backend._check(host.record(), "target") is None
        assert clock.errors == []
    assert [name for name, _, _ in clock.calls] == ["controller", "selected"]
    assert save() is None, "lost slot costs must never be exported as observed zero cost"


@dataclass
class _GuardFixture:
    root: Path
    host: backend_fixture.Host
    read_record: Callable[..., tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]]]
    observer: worker.HelperGuardCosts
    run_helper: Callable[..., Awaitable[TransactionRecordV2]]
    interpreter: str

    @property
    def path(self) -> Path:
        source = self.observer.source
        return self.root / f"helper-guard-costs-{source['pid']}-{source['invocation']}.json"


@pytest.fixture
async def helper_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    request: pytest.FixtureRequest,
) -> AsyncIterator[_GuardFixture]:
    """Enter the real prepare-generated hook and real runpy main, without a manager."""
    native, _ = fixture
    native.prepare()
    root = tmp_path / "guard-helper"
    root.mkdir(mode=0o700)
    original_slot = backend_fixture._slot
    monkeypatch.setattr(
        backend_fixture,
        "_slot",
        lambda directory, name: original_slot(directory, {"base": "a", "target": "b"}[name]),
    )
    worker.write_json(
        root / "fixture.json",
        {
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
        },
    )
    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    monkeypatch.setattr(ManagedLinuxBackend, "_check", ManagedLinuxBackend._check)
    monkeypatch.setattr(filesystem_identity, "verify_local_filesystems", lambda paths: None)
    callbacks: list[Callable[..., Any]] = []
    namespaces: list[dict[str, Any]] = []

    def register(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[..., Any]:
        assert not args and not kwargs
        callbacks.append(function)
        return function

    profile, interpreter = sys.getprofile(), sys.executable
    mode = getattr(request, "param", None)

    def foreign(frame: FrameType, event: str, argument: object) -> None:
        pass

    journal_root = root / "data/management/transaction-v1"
    with TransactionJournal(journal_root, root / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = backend_fixture.Host(root, journal, control, "switch", change_config=False)
            try:
                journal.database.write_bytes(b"")
                journal.database.chmod(0o600)
                argv = managed_helper_arguments(host.anchor, host.slots["base"])
                monkeypatch.setattr(sys, "prefix", str(root / "a"))
                monkeypatch.setattr(sys, "executable", argv[0])
                monkeypatch.setattr(sys, "orig_argv", list(argv))
                monkeypatch.setattr(sys, "argv", list(argv[3:]))
                monkeypatch.setattr(atexit, "register", register)
                if mode == "wrong_origin":
                    spec = managed_host.__spec__
                    assert spec is not None
                    monkeypatch.setattr(
                        managed_host,
                        "__spec__",
                        ModuleSpec(spec.name, spec.loader, origin="<foreign>"),
                    )
                elif mode in {"replaced_main", "replaced_record"}:

                    def replaced(*args: Any, **kwargs: Any) -> None:
                        raise AssertionError("a canonical replacement must not run")

                    monkeypatch.setattr(
                        managed_host, "main" if mode == "replaced_main" else "_record", replaced
                    )
                sys.setprofile(None)
                hook = (native.volume / "_muli_native_ci_hook.py").read_text(encoding="utf-8")
                exec(compile(hook, "<actual-generated-guard-hook>", "exec"), {})
                assert len(callbacks) == 1 and sys.getprofile() is not None
                if mode == "superseded":
                    sys.setprofile(foreign)

                def capture(coroutine: object) -> TransactionRecordV2:
                    assert isinstance(coroutine, CoroutineType)
                    try:
                        assert coroutine.cr_frame is not None
                        namespace = coroutine.cr_frame.f_globals
                        assert namespace is sys.modules["__main__"].__dict__
                        assert namespace is not managed_host.__dict__
                        namespaces.append(namespace)
                        return host.record()
                    finally:
                        coroutine.close()

                with monkeypatch.context() as local:
                    local.setattr(asyncio, "run", capture)
                    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
                        with pytest.raises(SystemExit) as exited:
                            runpy.run_module(
                                "muli.managed_host", run_name="__main__", alter_sys=True
                            )
                    assert exited.value.code == 0
                assert sys.getprofile() is (foreign if mode == "superseded" else None)
                observer = cast(worker.HelperGuardCosts, callbacks[0].__self__)
                assert observer.bound is (mode is None) and len(namespaces) == 1
                yield _GuardFixture(
                    root,
                    host,
                    namespaces[0]["_record"],
                    observer,
                    namespaces[0]["run_managed_helper"],
                    interpreter,
                )
            finally:
                sys.setprofile(profile)
                host.release_writer()
                await host.backend.__aexit__()


def _guard_result(guard: _GuardFixture) -> dict[str, Any]:
    record, source = guard.host.record(), dict(guard.observer.source)
    return {
        "completed": False,
        "stage": "transaction",
        "code": "TimeoutError",
        "timeline": [
            {
                "stage": "transaction",
                "elapsed_ms": 1,
                "generation": record.generation,
                "revision": record.revision,
                "phase": record.phase,
                "intent": record.intent,
                "code": record.code,
                "failure_code": record.failure_code,
                "rollback_attempted": record.rollback_attempted,
            },
            {
                "stage": "helper_exit",
                "elapsed_ms": 2,
                "helper": source["helper"],
                "state": "active",
                "pid": source["pid"],
                "result": "success",
                "exit_status": 0,
                "observation": "present",
            },
        ],
        "generations": [
            {
                "deployment": record.identity.deployment,
                "generation": record.generation,
                "helper": source["helper"],
            }
        ],
        "resources": {"helper_source": source},
    }


async def test_narrow_generated_hook_measures_real_guards_and_preserves_each_await_side(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, ticks = helper_guard, {"wall": 0, "cpu": 0}

    def tick(name: str, amount: int) -> int:
        ticks[name] += amount
        return ticks[name]

    monkeypatch.setattr(
        worker,
        "time",
        SimpleNamespace(
            **(
                vars(worker.time)
                | {
                    "perf_counter_ns": partial(tick, "wall", 100),
                    "process_time_ns": partial(tick, "cpu", 10),
                }
            )
        ),
    )
    host, observer = guard.host, guard.observer
    record, controller, slots = guard.read_record(host.control, host.anchor)
    assert record == host.record() and controller == host.slots["base"] and slots == host.slots
    sentinel = object()
    observed: list[int] = []

    async def read(*args: Any, **kwargs: Any) -> Any:
        observed.append(observer.totals["checks"]["entered"])
        await asyncio.sleep(0)
        observed.append(observer.totals["checks"]["entered"])
        return sentinel

    monkeypatch.setattr(host.backend.probe, "read_managed", read)
    assert await host.backend._probe(record) is sentinel
    assert observed == [1, 1] and observer.totals["checks"]["entered"] == 2
    await asyncio.sleep(0)
    assert guard.read_record(host.control, host.anchor)[0] == record
    observer.save()
    facts = worker.read_json(guard.path)
    assert facts["host_record"] == {
        "entered": 2,
        "returned": 2,
        "raised": 0,
        "wall_ns": 200,
        "cpu_ns": 20,
    }
    assert facts["checks"] == facts["host_record"]
    assert facts["first_record"] == {
        "outcome": "returned",
        "position": {
            key: getattr(record, key) for key in ("generation", "revision", "phase", "intent")
        },
        "wall_ns": 100,
        "cpu_ns": 10,
    }
    assert facts["unpositioned_raised"] == observer.metric()
    assert {row["metric"] for row in facts["rows"]} == {"host_record", "checks"}
    assert worker.helper_guard_snapshot_facts(facts, observer.source)
    assert not (guard.root / "helper-costs.json").exists()
    assert not (guard.root / "helper-entry-checkpoint.json").exists()
    result, retained = _guard_result(guard), []
    worker.collect_helper_guard_costs(
        guard.root,
        result,
        [{"source": dict(observer.source), "observation": None}],
        retained,
        deadline=None,
    )
    assert result["resources"]["helper_guard_costs"]["sources"][0]["costs"] == facts
    assert ci.fixture_result(json.dumps(result).encode()) == result


@pytest.mark.parametrize(
    "helper_guard",
    ["wrong_origin", "replaced_main", "replaced_record", "superseded"],
    indirect=True,
)
async def test_narrow_unbound_actual_main_does_not_publish_or_replace_foreign_profile(
    helper_guard: _GuardFixture,
) -> None:
    guard = helper_guard
    assert guard.read_record(guard.host.control, guard.host.anchor)[0] == guard.host.record()
    assert guard.host.backend._check(guard.host.record()) is None
    guard.observer.save()
    assert not guard.observer.bound and not guard.path.exists()
    assert guard.observer.rows == {}


async def test_narrow_actual_helper_read_await_keeps_both_real_host_authentications(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, counts, helper = helper_guard, [], HelperProbe()
    failure = RuntimeError("stop before effects")

    class Probe(ManagedSystemdProbe):
        async def read_helper(self, name: str, argv: tuple[str, ...], limits: Any) -> Any:
            counts.append(guard.observer.totals["host_record"]["entered"])
            await asyncio.sleep(0)
            counts.append(guard.observer.totals["host_record"]["entered"])
            return await helper.read_helper(name, argv, limits)

    async def step(controller: ManagedTransactionController) -> TransactionRecordV2:
        raise failure

    guard.host.release_writer()
    guard.host.control.__exit__()
    monkeypatch.setattr(
        muli, "__file__", str(Path(guard.host.slots["base"].package.root) / "__init__.py")
    )
    monkeypatch.setattr(ManagedTransactionController, "step", step)
    with pytest.raises(RuntimeError) as caught:
        await guard.run_helper(
            guard.root / "data/management/transaction-v1",
            probe=Probe(limits=ProbeLimits(seconds=guard.host.anchor.helper.operation_timeout / 4)),
            clock=guard.host.clock,
        )
    assert caught.value is failure
    assert counts == [2, 2, 4, 4]
    assert guard.observer.totals["host_record"]["returned"] == 5


@pytest.mark.parametrize("cancelled", [False, True])
async def test_narrow_real_guard_failures_keep_identity_and_host_failure_has_no_fabricated_position(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    guard, failure = (
        helper_guard,
        asyncio.CancelledError("PRIVATE") if cancelled else RuntimeError("PRIVATE"),
    )
    host, observer = guard.host, guard.observer
    record = guard.read_record(host.control, host.anchor)[0]
    original_anchor = DeploymentStore.verify_anchor

    def anchor(store: DeploymentStore, selected: DeploymentAnchor) -> None:
        original_anchor(store, selected)
        raise failure

    with monkeypatch.context() as local:
        local.setattr(DeploymentStore, "verify_anchor", anchor)
        with pytest.raises(type(failure)) as caught:
            guard.read_record(host.control, host.anchor)
        assert caught.value is failure
    original_authenticate = DeploymentStore._authenticate

    def authenticate(*args: Any, **kwargs: Any) -> None:
        original_authenticate(*args, **kwargs)
        raise failure

    with monkeypatch.context() as local:
        local.setattr(DeploymentStore, "_authenticate", authenticate)
        with pytest.raises(type(failure)) as caught:
            host.backend._check(record)
        assert caught.value is failure
    observer.save()
    facts = worker.read_json(guard.path)
    assert facts["host_record"]["entered"] == 2 and facts["host_record"]["raised"] == 1
    assert facts["unpositioned_raised"]["entered"] == facts["unpositioned_raised"]["raised"] == 1
    assert facts["checks"]["entered"] == facts["checks"]["raised"] == 1
    assert "PRIVATE" not in json.dumps(facts)
    assert all(
        row["costs"]["raised"] == 0 for row in facts["rows"] if row["metric"] == "host_record"
    )
    assert worker.helper_guard_snapshot_facts(facts, observer.source)


async def test_narrow_observer_preserves_original_return_object_and_first_failed_record(
    helper_guard: _GuardFixture,
) -> None:
    guard, failure = helper_guard, asyncio.CancelledError("PRIVATE")
    host, observer = guard.host, guard.observer
    value = (host.record(), host.slots["base"], host.slots)

    def original(
        control: JournalControl, anchor: DeploymentAnchor
    ) -> tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]]:
        return value

    def failed(control: JournalControl, anchor: DeploymentAnchor) -> Any:
        raise failure

    with pytest.raises(asyncio.CancelledError) as caught:
        observer.record(failed)(host.control, host.anchor)
    assert caught.value is failure
    assert observer.record(original)(host.control, host.anchor) is value
    observer.save()
    facts = worker.read_json(guard.path)
    assert (
        facts["first_record"]["outcome"] == "raised" and facts["first_record"]["position"] is None
    )
    assert facts["unpositioned_raised"]["raised"] == 1
    assert worker.helper_guard_snapshot_facts(facts, observer.source)


async def test_narrow_publication_has_two_finite_prefixes_one_tail_and_no_fsync(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, snapshots, syncs = helper_guard, [], []
    original_replace, original_fsync = Path.replace, os.fsync

    def replace_file(path: Path, target: Path) -> Path:
        result = original_replace(path, target)
        if target.name.startswith("helper-guard-costs-"):
            snapshots.append(worker.read_json(target))
        return result

    def fsync(descriptor: int) -> None:
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code is worker.HelperGuardCosts.publish.__code__:
                syncs.append(descriptor)
                break
            frame = frame.f_back
        original_fsync(descriptor)

    monkeypatch.setattr(Path, "replace", replace_file)
    monkeypatch.setattr(os, "fsync", fsync)
    host, observer = guard.host, guard.observer
    record = guard.read_record(host.control, host.anchor)[0]
    for _ in range(4):
        host.backend._check(record)
    assert len(snapshots) == 1 and snapshots[0]["checks"]["entered"] == 1
    installing = host.install_intent()
    for _ in range(4):
        host.backend._check(installing)
    observer.save()
    observer.save()
    assert [row["boundary"] for row in snapshots] == [*worker.HELPER_GUARD_PREFIXES, "normal_exit"]
    assert snapshots[-1]["checks"]["entered"] == 8
    assert snapshots[-1]["attempts"] == {"prefix": 2, "final": 1}
    assert snapshots[-1]["prior_publications"]["count"] == 2 and syncs == []


async def test_narrow_failed_publications_consume_allowance_and_preserve_old_prefix(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, attempts = helper_guard, []
    host, observer = guard.host, guard.observer
    record = guard.read_record(host.control, host.anchor)[0]
    host.backend._check(record)
    original = guard.path.read_bytes()
    replace_file = Path.replace

    def failed(path: Path, target: Path) -> Path:
        if target == guard.path:
            attempts.append(target)
            raise OSError("PRIVATE")
        return replace_file(path, target)

    monkeypatch.setattr(Path, "replace", failed)
    installing = host.install_intent()
    for _ in range(3):
        host.backend._check(installing)
    observer.save()
    observer.save()
    assert len(attempts) == 2 and observer.attempts == {"prefix": 2, "final": 1}
    assert guard.path.read_bytes() == original


async def test_narrow_clock_failure_keeps_actual_authentication_without_zero_receipt(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = helper_guard

    def failed() -> int:
        raise asyncio.CancelledError("PRIVATE")

    monkeypatch.setattr(
        worker, "time", SimpleNamespace(**(vars(worker.time) | {"perf_counter_ns": failed}))
    )
    assert guard.read_record(guard.host.control, guard.host.anchor)[0] == guard.host.record()
    assert guard.host.backend._check(guard.host.record()) is None
    guard.observer.save()
    assert not guard.path.exists() and guard.observer.reason == "clock_invalid"


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_source",
        "future_revision",
        "future_clock",
        "bad_count",
        "public",
        "oversized",
        "symlink",
    ],
)
async def test_narrow_collection_rejects_bad_snapshot_without_erasing_accepted_history(
    helper_guard: _GuardFixture, damage: str
) -> None:
    guard = helper_guard
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    result, retained = _guard_result(guard), []
    sources = [{"source": dict(guard.observer.source), "observation": None}]
    worker.collect_helper_guard_costs(guard.root, result, sources, retained, deadline=None)
    accepted = copy.deepcopy(retained)
    facts = worker.read_json(guard.path)
    if damage == "wrong_source":
        facts["source"]["pid"] += 1
    elif damage == "future_revision":
        facts["rows"][0]["position"]["revision"] += 1
    elif damage == "future_clock":
        facts["monotonic_ns"] = 2**63 - 1
    elif damage == "bad_count":
        facts["checks"]["entered"] = True
    guard.path.write_text(json.dumps(facts), encoding="utf-8")
    if damage == "public":
        guard.path.chmod(0o644)
    elif damage == "oversized":
        guard.path.write_bytes(b" " * (worker.MAX_BYTES + 1))
    elif damage == "symlink":
        other = guard.path.with_name("other.json")
        guard.path.rename(other)
        guard.path.symlink_to(other)
    worker.collect_helper_guard_costs(guard.root, result, sources, retained, deadline=None)
    assert retained == accepted
    assert result["resources"]["helper_guard_costs"]["sources"] == accepted
    assert ci.fixture_result(json.dumps(result).encode()) == result


@pytest.mark.parametrize(
    "damage", ["wrong_root", "recover", "missing_run", "extra_argument", "foreign_profile"]
)
def test_narrow_rejects_inexact_entry_or_foreign_profile_before_fixture_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    prefix = tmp_path / "a"
    executable = str(prefix / "bin/python")
    argv = [
        executable,
        "-I",
        "-m",
        "muli.managed_host",
        "--root",
        str(tmp_path / "data/management/transaction-v1"),
        "run",
    ]
    if damage == "wrong_root":
        argv[-2] = str(tmp_path / "foreign")
    elif damage == "recover":
        argv[-1] = "recover"
    elif damage == "missing_run":
        argv.pop()
    elif damage == "extra_argument":
        argv.append("unexpected")
    accesses: list[Path] = []
    callbacks: list[object] = []
    original = ManagedLinuxBackend._check

    def foreign(frame: FrameType, event: str, argument: object) -> None:
        pass

    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", executable)
    monkeypatch.setattr(sys, "orig_argv", argv)
    monkeypatch.setattr(worker, "fixture", lambda root: accesses.append(root))
    monkeypatch.setattr(atexit, "register", lambda callback: callbacks.append(callback))
    previous = sys.getprofile()
    try:
        owner = foreign if damage == "foreign_profile" else None
        sys.setprofile(owner)
        worker.install_helper_guard_cost_hook()
        assert accesses == callbacks == [] and ManagedLinuxBackend._check is original
        assert sys.getprofile() is owner
    finally:
        sys.setprofile(previous)


async def test_narrow_missing_next_generation_keeps_authenticated_history(
    helper_guard: _GuardFixture,
) -> None:
    guard = helper_guard
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    result, retained = _guard_result(guard), []
    old_source = {"source": dict(guard.observer.source), "observation": None}
    worker.collect_helper_guard_costs(guard.root, result, [old_source], retained, deadline=None)
    accepted = copy.deepcopy(retained[0])
    # Reader-only next-source fixture: it does not claim to launch a second helper.
    next_record = record.model_copy(update={"generation": "c" * 32})
    source = {
        **guard.observer.source,
        "generation": next_record.generation,
        "helper": managed_helper_unit_name(next_record),
        "pid": os.getpid() + 1,
    }
    result["generations"].append(
        {
            "deployment": record.identity.deployment,
            "generation": source["generation"],
            "helper": source["helper"],
        }
    )
    result["timeline"].extend(
        [
            {**result["timeline"][0], "generation": source["generation"]},
            {**result["timeline"][1], "helper": source["helper"], "pid": source["pid"]},
        ]
    )
    result["resources"]["helper_source"] = source
    worker.collect_helper_guard_costs(
        guard.root,
        result,
        [old_source, {"source": source, "observation": None}],
        retained,
        deadline=None,
    )
    assert retained[0] == accepted and retained[1]["costs"] == worker.binding_unavailable()
    assert result["resources"]["helper_guard_costs"]["sources"] == retained
    assert ci.fixture_result(json.dumps(result).encode()) == result
    guard.observer.save()
    worker.collect_helper_guard_costs(guard.root, result, [old_source], retained, deadline=None)
    assert retained[0]["costs"]["scope"] == "final"
    assert retained[0]["collected_ns"] >= accepted["collected_ns"]
    assert retained[1]["costs"] == worker.binding_unavailable()
    assert ci.fixture_result(json.dumps(result).encode()) == result


async def test_narrow_late_source_and_snapshot_keep_separate_original_deadline_facts(
    helper_guard: _GuardFixture,
) -> None:
    guard = helper_guard
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    result = _guard_result(guard)
    result["timeline"] = result["timeline"][:1]
    started = worker.time.monotonic_ns() - 61 * 10**9
    observation = {
        "source": dict(guard.observer.source),
        "kind": "failure_collection",
        "started_ns": started,
        "case_deadline_ns": started + 60 * 10**9,
        "observed_ns": started + 60 * 10**9 + 10**7,
        "elapsed_ms": 60010,
    }
    result["resources"]["helper_source_observation"] = observation
    costs = worker.read_json(guard.path)
    costs["monotonic_ns"] = observation["case_deadline_ns"] + 5 * 10**6
    guard.path.write_text(json.dumps(costs), encoding="utf-8")
    worker.collect_helper_guard_costs(
        guard.root,
        result,
        [{"source": dict(guard.observer.source), "observation": observation}],
        [],
        deadline=None,
    )
    sample = result["resources"]["helper_guard_costs"]["sources"][0]
    assert sample["observation"] == observation
    assert (
        observation["case_deadline_ns"]
        < sample["costs"]["monotonic_ns"]
        < observation["observed_ns"]
        < sample["collected_ns"]
    )
    assert ci.fixture_result(json.dumps(result).encode()) == result
    del result["resources"]["helper_source_observation"]
    assert not worker.helper_guard_costs_facts(result["resources"]["helper_guard_costs"], result)


async def test_narrow_costs_yield_the_original_total_result_budget(
    helper_guard: _GuardFixture,
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
    costs = result["resources"].pop("helper_guard_costs")
    row = {
        "stage": "transaction",
        "elapsed_ms": 1,
        **{name: "x" * 100 for name in ("request_id", "boot", "result")},
    }
    result["timeline"].extend(dict(row) for _ in range(62))
    for name in ("helper", "state"):
        for item in result["timeline"]:
            if name in item:
                continue
            used = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
            capacity = worker.MAX_BYTES - 1 - used - len(f',"{name}":""')
            if capacity >= 0:
                item[name] = "x" * min(100, capacity)
    before = copy.deepcopy(result)
    original_data = json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
    assert len(original_data) < worker.MAX_BYTES
    assert ci.fixture_result(original_data) == before
    result["resources"]["helper_guard_costs"] = costs
    worker.trim_binding_diagnostics(result)
    final_data = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    assert len(final_data) <= worker.MAX_BYTES
    assert ci.fixture_result(final_data) == result
    observed = result["resources"].pop("helper_guard_costs", None)
    assert observed is None or observed == worker.binding_unavailable("output_budget")
    assert result == before


async def test_narrow_unproved_source_cannot_authorize_a_read(
    helper_guard: _GuardFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, reads = helper_guard, []
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    guard.host.backend._check(record)
    result = _guard_result(guard)
    result["timeline"] = result["timeline"][:1]
    monkeypatch.setattr(
        worker, "_read_helper_cost_json", lambda *args, **kwargs: reads.append(args)
    )
    worker.collect_helper_guard_costs(
        guard.root,
        result,
        [{"source": dict(guard.observer.source), "observation": None}],
        [],
        deadline=None,
    )
    assert reads == [] and result["resources"]["helper_guard_costs"] == worker.binding_unavailable()


@pytest.mark.parametrize("with_prefix", [False, True])
async def test_narrow_owned_child_kill_retains_only_a_handshaken_prefix(
    helper_guard: _GuardFixture, with_prefix: bool
) -> None:
    """Kill only our fresh Python child; no systemd, desktop or user process is used."""
    guard = helper_guard
    record = guard.read_record(guard.host.control, guard.host.anchor)[0]
    child_root = guard.root / "owned-child"
    child_root.mkdir(mode=0o700)
    record_path = child_root / "record.json"
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    record_path.chmod(0o600)
    # Publication/kill coverage only; the real generated hook and authorizations
    # have their separate cases above. No pytest state or observer is inherited.
    code = """
from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path

sys.path[:0] = sys.argv[1:3]
from helpers import managed_native_worker as worker
from muli.managed_transaction import TransactionRecordV2

root = Path(sys.argv[3])
record = TransactionRecordV2.model_validate_json((root / "record.json").read_bytes())
invocation = "b" * 32
os.environ["INVOCATION_ID"] = invocation
observer = worker.HelperGuardCosts(root, invocation)
observer.bound = True
atexit.register(observer.save)

def read_record(control: object, anchor: object) -> tuple[TransactionRecordV2, None, dict[str, object]]:
    return record, None, {}

def check_record(backend: object, current: TransactionRecordV2) -> None:
    pass

if sys.argv[4] == "1":
    observer.record(read_record)(None, None)
    observer.check(check_record)(None, record)
os.write(1, b"ready")
os.read(0, 1)
"""
    # This saved constructor permits this one owned child while the ordinary
    # native command entry points remain forbidden by forbid_real_effects.
    child = Popen(
        [
            guard.interpreter,
            "-I",
            "-B",
            "-c",
            code,
            str(Path(muli.__file__).resolve().parent.parent),
            str(Path(worker.__file__).resolve().parent.parent),
            str(child_root),
            "1" if with_prefix else "0",
        ],
        cwd=child_root,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
    )
    ready = b""
    try:
        assert child.stdout is not None
        readable, _, _ = select.select([child.stdout], [], [], 5)
        if readable:
            ready = os.read(child.stdout.fileno(), 5)
    finally:
        try:
            child.kill()
            _, errors = child.communicate(timeout=5)
        finally:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
    assert ready == b"ready", errors.decode(errors="replace")
    assert child.returncode == -signal.SIGKILL
    source = {
        "generation": record.generation,
        "helper": managed_helper_unit_name(record),
        "uid": os.geteuid(),
        "pid": child.pid,
        "invocation": "b" * 32,
    }
    path = child_root / f"helper-guard-costs-{child.pid}-{'b' * 32}.json"
    if with_prefix:
        costs = worker._read_helper_cost_json(path, deadline=None)
        assert costs["scope"] == "prefix" and costs["attempts"] == {"prefix": 1, "final": 0}
        assert worker.helper_guard_snapshot_facts(costs, source)
    else:
        assert not path.exists()
    result = _guard_result(guard)
    result["resources"]["helper_source"] = source
    result["timeline"][-1]["pid"] = child.pid
    retained: list[dict[str, Any]] = []
    worker.collect_helper_guard_costs(
        child_root, result, [{"source": source, "observation": None}], retained, deadline=None
    )
    saved = result["resources"]["helper_guard_costs"]["sources"][0]["costs"]
    if with_prefix:
        assert saved.get("scope") == "prefix"
    else:
        assert saved == worker.binding_unavailable()
    assert ci.fixture_result(json.dumps(result).encode()) == result
