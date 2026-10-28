"""Offline startup-hook observation; no interpreter or manager is launched."""

from __future__ import annotations

import asyncio
import atexit
import builtins
import copy
import json
import os
import runpy
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from functools import partial
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import CoroutineType, FrameType, FunctionType, SimpleNamespace
from typing import Any, Literal, cast

import pytest
import test_managed_backend as backend_fixture
from helpers import managed_native_worker as worker
from test_managed_native_ci import ci
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects

import muli.managed_host as managed_host
import muli.platforms.filesystem_identity as filesystem_identity
import muli.platforms.managed_backend as managed_backend
import muli.platforms.managed_deployment as managed_deployment
from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import JournalControl, Role, TransactionError, TransactionJournal
from muli.platforms.linux_transaction import SystemdProbe, UnitSnapshot
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore, SlotIdentity

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
