"""Bound runpy entry and one failure-window observation, without native processes."""

from __future__ import annotations

import asyncio
import atexit
import copy
import json
import os
import runpy
import stat
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import CodeType, CoroutineType, FrameType
from typing import Any

import pytest
import test_managed_backend as backend_fixture
from helpers import managed_native_worker as worker
from test_linux_helper_probe import setup as helper_setup
from test_managed_native_ci import ci
from test_managed_native_ci import fixture as fixture
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_management_helper import Probe as HelperProbe

import muli
import muli.managed_host as managed_host
import muli.platforms.filesystem_identity as filesystem_identity
import muli.platforms.linux_transaction as linux_transaction
import muli.platforms.managed_backend as managed_backend
import muli.platforms.managed_deployment as managed_deployment
import muli.platforms.managed_probe as managed_probe
from muli.failure_diagnostics import validate_failure_diagnostic
from muli.managed_controller import ManagedTransactionController
from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import TransactionJournal
from muli.platforms.installation import HelperLimits
from muli.platforms.linux_transaction import HelperSnapshot, ProbeLimits, SystemdProbe
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux isolated entry fixture")


@dataclass
class EntryEnvironment:
    root: Path
    journal: TransactionJournal
    record: TransactionRecordV2
    anchor: DeploymentAnchor
    controller: SlotIdentity
    argv: tuple[str, ...]
    hook: str


@pytest.fixture
async def entry_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: tuple[Any, Any],
    request: pytest.FixtureRequest,
) -> AsyncIterator[EntryEnvironment]:
    native, _ = fixture
    native.prepare()
    hook = (native.volume / "_muli_native_ci_hook.py").read_text(encoding="utf-8")
    root = tmp_path / "entry-helper"
    root.mkdir(mode=0o700)
    original_slot = backend_fixture._slot

    def slot(directory: Path, name: str) -> SlotIdentity:
        return original_slot(directory, {"base": "a", "target": "b"}[name])

    monkeypatch.setattr(backend_fixture, "_slot", slot)
    generation = getattr(request, "param", None)
    if generation is not None:
        monkeypatch.setattr(
            backend_fixture,
            "prepare_managed",
            partial(backend_fixture.prepare_managed, generation=generation),
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
    journal_root = root / "data/management/transaction-v1"
    with TransactionJournal(journal_root, root / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = backend_fixture.Host(root, journal, control, "switch", change_config=False)
            try:
                record, anchor, controller = host.record(), host.anchor, host.slots["base"]
                journal.database.write_bytes(b"")
                journal.database.chmod(0o600)
            finally:
                host.release_writer()
                await host.backend.__aexit__()
        yield EntryEnvironment(
            root,
            journal,
            record,
            anchor,
            controller,
            managed_helper_arguments(anchor, controller),
            hook,
        )


@pytest.mark.parametrize(
    "entry", ["managed_host", "management_helper", "serve", "other_module", "unisolated_helper"]
)
@pytest.mark.parametrize("existing_profile", [False, True])
async def test_generated_hook_omits_broad_cost_observer_and_preserves_birth_and_serve(
    entry_environment: EntryEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    existing_profile: bool,
) -> None:
    """执行实际生成文本；吞异常不能掩盖成本安装器被调用。"""
    env = entry_environment
    argv = list(env.argv)
    if entry == "management_helper":
        argv = [argv[0], "-I", "-m", "muli.management_helper", str(env.journal.directory.path)]
    elif entry == "serve":
        argv = [argv[0], "-I", "-m", "muli", "serve"]
    elif entry == "other_module":
        argv = [argv[0], "-I", "-m", "muli"]
    elif entry == "unisolated_helper":
        argv.remove("-I")
    calls = {"cost": 0, "guard": 0, "birth": 0, "serve": 0}

    def install_cost() -> None:
        calls["cost"] += 1
        raise RuntimeError("成本观察器不应由生成 hook 安装")

    def install_birth() -> None:
        calls["birth"] += 1

    def install_guard(*, startup_points: object = None) -> None:
        assert isinstance(startup_points, list)
        assert [point[0] for point in startup_points] == list(worker.HELPER_STARTUP_POINTS[:3])
        calls["guard"] += 1

    def install_service() -> None:
        calls["serve"] += 1

    def foreign_profile(frame: FrameType, event: str, argument: object) -> None:
        pass

    bindings = tuple(
        (owner, name, getattr(owner, name))
        for owner, name in (
            (managed_host, "main"),
            (managed_host, "_entry"),
            (managed_host, "_record"),
            (managed_host, "run_managed_helper"),
            (ManagedLinuxBackend, "_check"),
            (ManagedLinuxBackend, "_request"),
            (ManagedLinuxBackend, "install"),
            (ManagedLinuxBackend, "start"),
            (ManagedLinuxBackend, "_command"),
            (SystemdProbe, "_command"),
            (managed_deployment, "verify_slot_files"),
            (managed_backend, "verify_slot_files"),
        )
    )
    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    monkeypatch.setattr(worker, "install_helper_cost_hook", install_cost)
    monkeypatch.setattr(worker, "install_helper_guard_cost_hook", install_guard)
    monkeypatch.setattr(worker, "install_unit_birth_hook", install_birth)
    monkeypatch.setattr(worker, "install_hook", install_service)
    monkeypatch.setattr(sys, "prefix", env.controller.prefix)
    monkeypatch.setattr(sys, "executable", argv[0])
    monkeypatch.setattr(sys, "orig_argv", argv)
    monkeypatch.setattr(sys, "argv", argv[3:])
    original_profile = sys.getprofile()
    try:
        profile = foreign_profile if existing_profile else None
        sys.setprofile(profile)
        exec(compile(env.hook, "<fixture-helper-hook>", "exec"), {})
        assert calls == {
            "cost": 0,
            "guard": int(entry == "managed_host"),
            "birth": int(entry in {"managed_host", "management_helper"}),
            "serve": int(entry == "serve"),
        }
        assert sys.getprofile() is profile
        assert all(getattr(owner, name) is original for owner, name, original in bindings)
    finally:
        sys.setprofile(original_profile)


@pytest.mark.parametrize(
    "failure_at",
    ["entry", "read", "cancel", "pending", "after_record", "after_process", "step", "publish"],
)
async def test_real_runpy_entry_preserves_original_calls_failure_and_cancellation(
    entry_environment: EntryEnvironment, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    env = entry_environment
    failure: BaseException = (
        asyncio.CancelledError("PRIVATE_ENTRY_BODY")
        if failure_at == "cancel"
        else RuntimeError("PRIVATE_ENTRY_BODY")
    )
    calls: list[str] = []
    callbacks: list[Callable[[], Any]] = []
    checkpoints: list[dict[str, Any]] = []
    entry_sync_modes: list[int] = []
    cost_sync_modes: list[int] = []
    coroutines: list[CoroutineType] = []
    helper = HelperProbe()
    entered = asyncio.Event()
    release: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    original_replace = Path.replace
    original_fsync = os.fsync
    (publish_code,) = (
        code
        for code in worker.install_helper_cost_hook.__code__.co_consts
        if isinstance(code, CodeType) and code.co_name == "publish"
    )

    def fsync(descriptor: int) -> None:
        # Scope this spy to the real private publisher and its declared target;
        # journal fsyncs keep executing and do not count as helper publication.
        caller = sys._getframe(1)
        if caller.f_code is publish_code:
            target = caller.f_locals.get("path")
            if target == env.root / "helper-entry-checkpoint.json":
                entry_sync_modes.append(os.fstat(descriptor).st_mode)
            elif target == env.root / "helper-costs.json":
                cost_sync_modes.append(os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    def register(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[..., Any]:
        callbacks.append(partial(function, *args, **kwargs))
        return function

    class Probe(managed_probe.ManagedSystemdProbe):
        async def read_helper(
            self, name: str, argv: tuple[str, ...], limits: HelperLimits
        ) -> HelperSnapshot:
            calls.append("read")
            if failure_at == "pending":
                entered.set()
                await release
            if failure_at in {"read", "cancel"}:
                raise failure
            result = await helper.read_helper(name, argv, limits)
            if failure_at == "after_record":
                Path(env.controller.package.root, "__main__.py").write_text(
                    "changed", encoding="utf-8"
                )
            elif failure_at == "after_process":
                monkeypatch.setattr(sys, "executable", "foreign-interpreter")
            return result

    async def step(controller: ManagedTransactionController) -> TransactionRecordV2:
        calls.append("step")
        raise failure

    def capture_run(coroutine: object) -> TransactionRecordV2:
        assert isinstance(coroutine, CoroutineType)
        coroutines.append(coroutine)
        return env.record

    def capture(path: Path, target: Path) -> Path:
        if target.name == "helper-entry-checkpoint.json" and failure_at == "publish":
            raise RuntimeError("PRIVATE_OBSERVER_BODY")
        result = original_replace(path, target)
        if target.name == "helper-entry-checkpoint.json":
            checkpoints.append(worker.read_json(target))
        return result

    for owner, name in (
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
    monkeypatch.setitem(sys.modules, "managed_native_worker", worker)
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    monkeypatch.setattr(sys, "prefix", env.controller.prefix)
    monkeypatch.setattr(sys, "executable", env.argv[0])
    monkeypatch.setattr(sys, "orig_argv", list(env.argv))
    monkeypatch.setattr(sys, "argv", list(env.argv[3:]))
    monkeypatch.setattr(muli, "__file__", str(Path(env.controller.package.root) / "__init__.py"))
    monkeypatch.setattr(atexit, "register", register)
    monkeypatch.setattr(managed_host, "ManagedSystemdProbe", Probe)
    monkeypatch.setattr(managed_probe, "ManagedSystemdProbe", Probe)
    monkeypatch.setattr(ManagedTransactionController, "step", step)
    monkeypatch.setattr(filesystem_identity, "verify_local_filesystems", lambda paths: None)
    monkeypatch.setattr(Path, "replace", capture)
    monkeypatch.setattr(os, "fsync", fsync)
    profile = sys.getprofile()
    try:
        # Keep this explicit broad-entry positive control independent of the
        # new generated narrow observer's one-shot profiler.
        monkeypatch.setattr(worker, "install_helper_guard_cost_hook", lambda: None)
        exec(compile(env.hook, "<fixture-helper-hook>", "exec"), {})
        # 入口正控继续验证真实 runpy；观察器由夹具显式安装。
        worker.install_helper_cost_hook()
        with monkeypatch.context() as local:
            local.setattr(asyncio, "run", capture_run)
            with pytest.warns(RuntimeWarning, match="found in sys.modules"):
                with pytest.raises(SystemExit) as exited:
                    runpy.run_module("muli.managed_host", run_name="__main__", alter_sys=True)
            assert exited.value.code == 0
        assert sys.getprofile() is profile and len(coroutines) == 1
        if failure_at == "entry":
            Path(env.controller.package.root, "__main__.py").write_text("changed", encoding="utf-8")
        expected_error = (
            asyncio.CancelledError
            if failure_at == "cancel"
            else (
                ValueError
                if failure_at in {"entry", "after_record", "after_process"}
                else RuntimeError
            )
        )
        pending: asyncio.Task[Any] | None = None
        if failure_at == "pending":
            pending = asyncio.create_task(coroutines[0])
            await entered.wait()
            assert checkpoints[-1]["boundary"] == "read_helper_dispatch"
            assert checkpoints[-1]["outcome"] == "progress"
            release.set_exception(failure)
        with pytest.raises(expected_error) as caught:
            await (pending if pending is not None else coroutines[0])
        if failure_at in {"read", "cancel", "pending", "step", "publish"}:
            assert caught.value is failure
        assert calls == (
            ["read", "read", "step"]
            if failure_at in {"step", "publish"}
            else ([] if failure_at == "entry" else ["read"])
        )
        assert env.journal.read_current() == env.record
        assert entry_sync_modes == []  # Entry progress/failure checkpoints are not durable.
        assert cost_sync_modes == []
        assert len(callbacks) == 1
        callbacks[0]()  # The existing exit-cost publisher keeps both durability calls.
        assert entry_sync_modes == []
        assert len(cost_sync_modes) == 2
        assert stat.S_ISREG(cost_sync_modes[0]) and stat.S_ISDIR(cost_sync_modes[1])
        if failure_at == "publish":
            assert checkpoints == []
            return
        expected = {
            "entry": "entry_dispatch",
            "read": "read_helper_dispatch",
            "cancel": "read_helper_dispatch",
            "pending": "read_helper_dispatch",
            "after_record": "after_record_dispatch",
            "after_process": "after_process_dispatch",
            "step": "step_dispatch",
        }[failure_at]
        assert checkpoints[0]["boundary"] == "main_entered"
        last = checkpoints[-1]
        assert last["boundary"] == expected
        assert last["outcome"] == ("cancelled" if failure_at == "cancel" else "failed")
        assert validate_failure_diagnostic(last["diagnostic"]) is not None
        assert "PRIVATE_" not in json.dumps(last)
        source = {key: last[key] for key in ("generation", "helper", "uid", "pid", "invocation")}
        assert worker.helper_entry_checkpoint_facts(
            last, source, validate_diagnostic=validate_failure_diagnostic
        )
        assert all(frame["module"] != "muli.managed_host" for frame in last["diagnostic"]["frames"])
        assert len(callbacks) == 1
    finally:
        for coroutine in coroutines:
            coroutine.close()
        sys.setprofile(profile)


def _case(env: EntryEnvironment) -> worker.Case:
    case = worker.Case(env.root)
    record = env.record
    case.result["code"] = "TimeoutError"
    case.result["generations"] = [
        {
            "deployment": record.identity.deployment,
            "generation": record.generation,
            "helper": managed_helper_unit_name(record),
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
    return case


@pytest.fixture
def failure_probe(
    entry_environment: EntryEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SystemdProbe, Any, Path]:
    env = entry_environment
    directory = tmp_path / "helper-evidence"
    directory.mkdir()
    probe, bus, _, old_group, proc = helper_setup(directory)
    helper = managed_helper_unit_name(env.record)
    cgroup = f"/user.slice/user@{os.geteuid()}.service/{helper}"
    group = probe.cgroup_root / cgroup.lstrip("/")
    group.parent.mkdir(parents=True, exist_ok=True)
    old_group.rename(group)
    limits = env.anchor.helper
    for key, value in {
        "Id": helper,
        "ControlGroup": cgroup,
        "FragmentPath": f"/run/user/{os.geteuid()}/systemd/transient/{helper}",
    }.items():
        bus.rows[key] = ("s", value)
    bus.rows["ExecStart"] = (
        "a(sasbttttuii)",
        [[env.argv[0], list(env.argv), False, 0, 0, 0, 0, 123, 0, 0]],
    )
    for key, value in {
        "StartLimitIntervalUSec": int(limits.seconds * 10**6),
        "StartLimitBurst": limits.restart_burst,
        "RestartUSec": int(limits.restart_delay * 10**6),
        "RuntimeMaxUSec": int(limits.seconds * 10**6),
        "TimeoutStopUSec": int(limits.stop_seconds * 10**6),
        "MemoryMax": limits.memory_bytes,
        "CPUQuotaPerSecUSec": int(limits.cpu_percent * 10_000),
        "TasksMax": limits.tasks,
    }.items():
        bus.rows[key] = (bus.rows[key][0], value)
    (proc / "123/cmdline").write_bytes(b"\x00".join(os.fsencode(arg) for arg in env.argv) + b"\x00")
    (proc / "123/cgroup").write_text("0::" + cgroup + "\n", encoding="utf-8")
    (proc / "123/exe").unlink()
    (proc / "123/exe").symlink_to(Path(env.argv[0]).resolve())
    for key, value in {
        "memory.max": str(limits.memory_bytes),
        "pids.max": str(limits.tasks),
        "cpu.max": f"{int(limits.cpu_percent * 1000)} 100000",
    }.items():
        (group / key).write_text(value + "\n", encoding="utf-8")
    original = SystemdProbe

    def factory(*, limits: ProbeLimits) -> SystemdProbe:
        assert 0 < limits.seconds <= worker.CASE_PROBE_SECONDS
        return original(runner=bus, limits=limits, proc_root=proc, cgroup_root=probe.cgroup_root)

    monkeypatch.setattr(linux_transaction, "SystemdProbe", factory)
    return probe, bus, group


async def test_failure_window_authenticates_pending_source_once_without_transaction_progress(
    entry_environment: EntryEnvironment,
    failure_probe: tuple[SystemdProbe, Any, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, (_, bus, _) = entry_environment, failure_probe
    real_monotonic: Callable[[], float] = worker.time.monotonic
    clock_origin: float = real_monotonic()

    def monotonic() -> float:
        return float(worker.HARNESS_SECONDS) + (real_monotonic() - clock_origin)

    def monotonic_ns() -> int:
        return int(monotonic() * 10**9)

    # Keep the model clock moving, independent of a newly booted runner's uptime.
    monkeypatch.setattr(worker.time, "monotonic", monotonic)
    monkeypatch.setattr(worker.time, "monotonic_ns", monotonic_ns)
    case = _case(env)
    # This modelled Case deadline has passed; the original drive collection grant remains.
    case.start -= worker.CASE_SECONDS
    case.deadline = case.start + worker.CASE_SECONDS
    before = copy.deepcopy(case.result["timeline"])
    source = {
        "generation": env.record.generation,
        "helper": managed_helper_unit_name(env.record),
        "uid": os.geteuid(),
        "pid": 123,
        "invocation": bytes(range(16)).hex(),
    }
    value = {
        "version": 1,
        **source,
        "revision": env.record.revision,
        "boundary": "read_helper_dispatch",
        "monotonic_ns": worker.time.monotonic_ns(),
        "outcome": "progress",
    }
    worker.write_json(env.root / "helper-entry-checkpoint.json", value)
    collection_deadline: float = worker.time.monotonic() + worker.CASE_PROBE_SECONDS
    assert 0 < case.start < case.deadline < worker.time.monotonic() < collection_deadline
    await case._collect_failure_helper_source(deadline=collection_deadline)
    assert len(bus.calls) == 5  # read_helper's full double-sided RPC/proc/kernel proof.
    assert not any("ListUnitsByPatterns" in call or "ExecMainStatus" in call for call in bus.calls)
    assert case.result["resources"]["helper_source"] == source
    observation = case.result["resources"]["helper_source_observation"]
    assert (
        observation["kind"] == "failure_collection"
        and observation["elapsed_ms"] >= worker.CASE_SECONDS * 1000
    )
    assert value["monotonic_ns"] > observation["case_deadline_ns"]
    assert case.result["resources"]["helper_entry_checkpoint"] == value
    assert case.result["timeline"] == before and env.journal.read_current() == env.record
    assert ci.fixture_result(json.dumps(case.result).encode()) == case.result
    await case._collect_failure_helper_source(deadline=collection_deadline)
    assert len(bus.calls) == 5


@pytest.mark.parametrize(
    "damage",
    [
        "kernel",
        "membership",
        "invocation",
        "pid",
        "old_invocation",
        "cgroup",
        "journal_generation",
        "revoked",
        "old_generation",
        "deadline",
    ],
)
async def test_failure_window_rejects_bad_or_revoked_source_without_retry(
    entry_environment: EntryEnvironment, failure_probe: tuple[SystemdProbe, Any, Path], damage: str
) -> None:
    env, (probe, bus, group) = entry_environment, failure_probe
    case = _case(env)
    if damage == "kernel":
        (group / "memory.max").write_text("max\n", encoding="utf-8")
    elif damage == "membership":
        cgroup = f"/user.slice/{managed_helper_unit_name(env.record)}"
        bus.rows["ControlGroup"] = ("s", cgroup)
        group.rename(probe.cgroup_root / cgroup.lstrip("/"))
        (probe.proc_root / "123/cgroup").write_text("0::" + cgroup + "\n", encoding="utf-8")
    elif damage == "invocation":
        bus.before_second = lambda: bus.rows.update(InvocationID=("ay", [1] * 16))
    elif damage == "revoked":
        case._invalid_helper_sources.add(env.record.generation)
    elif damage == "old_generation":
        case.result["generations"][-1]["generation"] = "e" * 32
    elif damage in {"pid", "old_invocation", "cgroup"}:
        case.result["resources"]["helper_source"] = {
            "generation": env.record.generation,
            "helper": managed_helper_unit_name(env.record),
            "uid": os.geteuid(),
            "pid": 124 if damage == "pid" else 123,
            "invocation": "e" * 32 if damage == "old_invocation" else bytes(range(16)).hex(),
        }
        if damage == "cgroup":
            case._helper_source_groups[env.record.generation] = "/previous/group"
    elif damage == "journal_generation":

        def changed_generation() -> None:
            value = env.record.model_dump(mode="json")
            value["generation"] = "e" * 32
            worker.write_json(env.journal.directory.path / "state.json", value)

        bus.before_second = changed_generation
    deadline = worker.time.monotonic() + (worker.CASE_PROBE_SECONDS if damage != "deadline" else -1)
    try:
        await case._collect_failure_helper_source(deadline=deadline)
    except (ValueError, TimeoutError):
        pass
    calls = len(bus.calls)
    await case._collect_failure_helper_source(
        deadline=worker.time.monotonic() + worker.CASE_PROBE_SECONDS
    )
    assert len(bus.calls) == calls <= 5
    assert "helper_source_observation" not in case.result["resources"]
    assert "helper_entry_checkpoint" not in case.result["resources"]
    if damage != "journal_generation":
        assert env.journal.read_current() == env.record
    if damage in {"pid", "old_invocation", "cgroup"}:
        assert env.record.generation in case._invalid_helper_sources
        assert "helper_source" not in case.result["resources"]


@pytest.mark.parametrize(
    "entry_environment",
    ["e" * 32, "a" * 32],
    indirect=True,
    ids=["generation-first-e", "generation-first-other"],
)
@pytest.mark.parametrize(
    "damage",
    [
        "generation",
        "helper",
        "pid",
        "invocation",
        "future_revision",
        "future_time",
        "unknown_revision",
        "symlink",
        "hardlink",
        "public",
        "oversized",
        "fifo",
    ],
)
async def test_failure_entry_reader_rejects_unbound_future_or_dangerous_files(
    entry_environment: EntryEnvironment, failure_probe: tuple[SystemdProbe, Any, Path], damage: str
) -> None:
    env = entry_environment
    case = _case(env)
    await case._collect_failure_helper_source(
        deadline=worker.time.monotonic() + worker.CASE_PROBE_SECONDS
    )
    source = case.result["resources"]["helper_source"]
    observation = case.result["resources"]["helper_source_observation"]
    value = {
        "version": 1,
        **source,
        "revision": env.record.revision,
        "boundary": "entry_dispatch",
        "monotonic_ns": observation["observed_ns"],
        "outcome": "progress",
    }
    path = env.root / "helper-entry-checkpoint.json"
    worker.write_json(path, value)
    case._collect_helper_entry_checkpoint(
        env.record, deadline=worker.time.monotonic() + worker.CASE_PROBE_SECONDS
    )
    assert case.result["resources"].pop("helper_entry_checkpoint") == value
    if damage in {"generation", "helper", "invocation"}:
        previous = value[damage]
        value[damage] = ("f" if previous[0] == "e" else "e") + previous[1:]
        assert value[damage] != previous
    elif damage == "pid":
        value[damage] += 1
    elif damage in {"future_revision", "unknown_revision"}:
        value["revision"] += 1 if damage == "future_revision" else -1
    elif damage == "future_time":
        value["monotonic_ns"] += 1
    worker.write_json(path, value)
    if damage == "symlink":
        path.rename(path.with_name("foreign.json"))
        path.symlink_to(path.with_name("foreign.json"))
    elif damage == "hardlink":
        os.link(path, path.with_name("other.json"))
    elif damage == "public":
        path.chmod(0o644)
    elif damage == "oversized":
        path.write_bytes(b"x" * (worker.MAX_BYTES + 1))
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path)
    try:
        case._collect_helper_entry_checkpoint(
            env.record, deadline=worker.time.monotonic() + worker.CASE_PROBE_SECONDS
        )
    except (OSError, ValueError):
        pass
    assert "helper_entry_checkpoint" not in case.result["resources"]


@pytest.mark.parametrize(
    "damage",
    [
        "source",
        "window",
        "future_revision",
        "future_time",
        "unknown_boundary",
        "extra",
        "diagnostic",
    ],
)
async def test_entry_artifact_parser_requires_current_source_window_and_exact_fields(
    entry_environment: EntryEnvironment, failure_probe: tuple[SystemdProbe, Any, Path], damage: str
) -> None:
    env = entry_environment
    case = _case(env)
    await case._collect_failure_helper_source(
        deadline=worker.time.monotonic() + worker.CASE_PROBE_SECONDS
    )
    source = case.result["resources"]["helper_source"]
    observation = case.result["resources"]["helper_source_observation"]
    value = {
        "version": 1,
        **source,
        "revision": env.record.revision,
        "boundary": "main_entered",
        "monotonic_ns": observation["observed_ns"],
        "outcome": "progress",
    }
    case.result["resources"]["helper_entry_checkpoint"] = value
    assert ci.fixture_result(json.dumps(case.result).encode()) == case.result
    if damage == "source":
        value["pid"] += 1
    elif damage == "window":
        observation["kind"] = "case"
    elif damage == "future_revision":
        value["revision"] += 1
    elif damage == "future_time":
        value["monotonic_ns"] += 1
    elif damage == "unknown_boundary":
        value["boundary"] = "step_entered"
    elif damage == "extra":
        value["private"] = "MUST_NOT_ESCAPE"
    else:
        value["outcome"] = "failed"
        value["diagnostic"] = {"message": "MUST_NOT_ESCAPE"}
    with pytest.raises(ci.FixtureError, match="native_result_invalid"):
        ci.fixture_result(json.dumps(case.result).encode())


async def test_failure_probe_interruption_preserves_saved_first_error_and_cleanup_boundary(
    entry_environment: EntryEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = entry_environment
    original = _case(env)
    calls: list[str] = []

    async def drive(case: worker.Case) -> None:
        case.result = copy.deepcopy(original.result)
        raise TimeoutError("PRIVATE_FIRST_BODY")

    async def interrupted(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append("probe")
        assert worker.read_json(env.root / "result.json")["code"] == "TimeoutError"
        raise asyncio.CancelledError("PRIVATE_DIAGNOSTIC_BODY")

    async def cleanup(case: worker.Case) -> None:
        calls.append("cleanup")

    def probe(*, limits: ProbeLimits) -> SystemdProbe:
        return SystemdProbe(runner=interrupted, limits=limits)

    monkeypatch.setattr(worker.Case, "drive", drive)
    monkeypatch.setattr(worker.Case, "cleanup", cleanup)
    monkeypatch.setattr(linux_transaction, "SystemdProbe", probe)
    monkeypatch.setenv(
        "MULI_NATIVE_EXECUTE_DEADLINE",
        str(worker.time.monotonic() + worker.HARNESS_SECONDS + worker.CLEANUP_SECONDS),
    )
    assert await worker.run("drive", env.root) == 1
    result = worker.read_json(env.root / "result.json")
    assert result["code"] == "TimeoutError" and not result["completed"]
    assert "PRIVATE_" not in json.dumps(result)
    assert calls == ["probe"]
    monkeypatch.setenv(
        "MULI_NATIVE_CLEANUP_DEADLINE", str(worker.time.monotonic() + worker.CLEANUP_SECONDS / 2)
    )
    assert await worker.run("cleanup", env.root) == 0
    assert calls == ["probe", "cleanup"]
