"""Only same-round interface reads may overlap; all observation fences remain."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

import pytest
from test_linux_helper_probe import NAME, setup
from test_linux_transaction import fixture
from test_managed_backend import Host
from test_managed_backend import Probe as BackendProbe
from test_managed_backend import host as host
from test_managed_host import Environment, Probe
from test_managed_host import current_controller as current_controller
from test_managed_host import environment as environment
from test_managed_host import filesystems as filesystems

import muli.managed_host as managed_host
from muli.platforms.installation import HelperLimits
from muli.platforms.linux_transaction import (
    CommandRunner,
    HelperSnapshot,
    ObservationError,
    ProbeLimits,
    ServiceSnapshot,
    SystemdProbe,
)
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_probe import ManagedSystemdProbe
from muli.process import ProcessOutputLimitError, run_process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed probe boundaries")

type Interface = Literal["Unit", "Service"]
type Key = tuple[int, Interface]
type Kind = Literal["service", "helper"]

UNIT_FIELDS = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "SourcePath",
    "DropInPaths",
    "NeedDaemonReload",
    "InvocationID",
    "Job",
)
SERVICE_FIELDS = ("Type", "MainPID", "ControlGroup", "ExecStart", "KillMode", "SendSIGKILL")
HELPER_UNIT_FIELDS = (
    "Transient",
    "CollectMode",
    "PartOf",
    "BindsTo",
    "StopPropagatedFrom",
    "Requires",
    "Requisite",
    "Conflicts",
    "StartLimitIntervalUSec",
    "StartLimitBurst",
)
HELPER_SERVICE_FIELDS = (
    "Restart",
    "RestartUSec",
    "RuntimeMaxUSec",
    "TimeoutStopUSec",
    "MemoryMax",
    "MemorySwapMax",
    "CPUQuotaPerSecUSec",
    "TasksMax",
    "LimitCORE",
    "UMask",
    "NoNewPrivileges",
    "OOMPolicy",
)


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("Interface-join fixtures must not execute systemd or a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)


class InterfaceRunner:
    """Gate real async calls while retaining the existing typed FakeBus reply."""

    def __init__(self, original: CommandRunner, *, gated: bool = True) -> None:
        self.original = original
        interfaces: tuple[Interface, ...] = ("Unit", "Service")
        self.entered: dict[Key, asyncio.Event] = {
            (round_, interface): asyncio.Event() for round_ in (1, 2) for interface in interfaces
        }
        self.release: dict[Key, asyncio.Event] = {key: asyncio.Event() for key in self.entered}
        self.cleanup_block: set[Key] = set()
        self.cleanup_entered = {key: asyncio.Event() for key in self.entered}
        self.cleanup_release = {key: asyncio.Event() for key in self.entered}
        if not gated:
            self.open_all()
        self.calls: list[tuple[list[str], float, int, bool]] = []
        self.rounds: dict[Interface, int] = {"Unit": 0, "Service": 0}
        self.errors: dict[Key, Exception] = {}
        self.cancelled: set[Key] = set()
        self.settled: set[Key] = set()
        self.active = 0
        self.peak = 0

    def open_all(self) -> None:
        for event in (*self.release.values(), *self.cleanup_release.values()):
            event.set()

    async def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        self.calls.append((list(argv), timeout, limit, merge_stderr))
        assert "GetAll" not in argv and "Environment" not in argv
        assert "--auto-start=no" in argv
        assert "--allow-interactive-authorization=no" in argv
        if "get-property" not in argv:
            return await self.original(
                argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr
            )
        first = argv.index("get-property")
        interface: Interface = "Unit" if argv[first + 3].endswith(".Unit") else "Service"
        self.rounds[interface] += 1
        key: Key = (self.rounds[interface], interface)
        assert key in self.entered, "A failed observation must not add another property round"
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered[key].set()
        try:
            await self.release[key].wait()
            await asyncio.sleep(0)
            if error := self.errors.get(key):
                raise error
            return await self.original(
                argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr
            )
        except asyncio.CancelledError:
            self.cancelled.add(key)
            raise
        finally:
            try:
                if key in self.cleanup_block:
                    self.cleanup_entered[key].set()
                    await self.cleanup_release[key].wait()
            finally:
                self.active -= 1
                self.settled.add(key)


@dataclass(slots=True)
class Case:
    runner: InterfaceRunner
    read: Callable[[], Awaitable[ServiceSnapshot | HelperSnapshot]]


def case(tmp_path: Path, kind: Kind, *, join: bool | None = True, gated: bool = True) -> Case:
    # Explicit true exercises only the primitive. The production route is tested separately.
    limits = ProbeLimits(seconds=1, command_bytes=4096)
    if kind == "helper":
        original, bus, argv, _, proc = setup(tmp_path)
        runner = InterfaceRunner(bus, gated=gated)
        if join is None:
            probe = ManagedSystemdProbe(
                runner=runner,
                limits=limits,
                proc_root=proc,
                cgroup_root=original.cgroup_root,
            )
        else:
            probe = ManagedSystemdProbe(
                runner=runner,
                limits=limits,
                proc_root=proc,
                cgroup_root=original.cgroup_root,
                join_interface_reads=join,
            )

        async def helper_read() -> HelperSnapshot:
            return await probe.read_helper(NAME, argv, HelperLimits())

        return Case(runner, helper_read)

    service, bus, proc = fixture(tmp_path)
    runner = InterfaceRunner(bus, gated=gated)
    if join is None:
        probe = ManagedSystemdProbe(runner=runner, limits=limits, proc_root=proc)
    else:
        probe = ManagedSystemdProbe(
            runner=runner, limits=limits, proc_root=proc, join_interface_reads=join
        )

    async def service_read() -> ServiceSnapshot:
        return await probe.read(service)

    return Case(runner, service_read)


async def entered(runner: InterfaceRunner, round_: int, interface: Interface) -> None:
    # This bounds a missing handshake; no elapsed duration is an acceptance assertion.
    await asyncio.wait_for(runner.entered[(round_, interface)].wait(), 1)


async def settle(task: asyncio.Task[Any], runner: InterfaceRunner) -> None:
    runner.open_all()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["service", "helper"])
async def test_same_round_enters_both_interfaces_and_keeps_next_round_behind_join(
    tmp_path: Path, kind: Kind
) -> None:
    selected = case(tmp_path, kind)
    runner = selected.runner
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await entered(runner, 1, "Service")
        assert runner.active == runner.peak == 2
        assert not runner.entered[(2, "Unit")].is_set()
        runner.release[(1, "Unit")].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        assert not runner.entered[(2, "Unit")].is_set()
        assert not runner.entered[(2, "Service")].is_set()
        runner.release[(1, "Service")].set()
        await entered(runner, 2, "Unit")
        await entered(runner, 2, "Service")
        assert {(1, "Unit"), (1, "Service")} <= runner.settled
        runner.open_all()
        result = await task
        assert result.process is not None and result.process.pid == 123
        assert runner.peak == 2 and runner.active == 0
        assert len(runner.calls) == 5
        assert "GetUnit" in runner.calls[0][0]
        expected = {
            "Unit": UNIT_FIELDS + (HELPER_UNIT_FIELDS if kind == "helper" else ()),
            "Service": SERVICE_FIELDS + (HELPER_SERVICE_FIELDS if kind == "helper" else ()),
        }
        for argv, seconds, limit, merged in runner.calls[1:]:
            first = argv.index("get-property")
            interface = argv[first + 3].rsplit(".", 1)[-1]
            assert tuple(argv[first + 4 :]) == expected[interface]
            assert seconds == 1 and limit == 4096 and merged is False
    finally:
        await settle(task, runner)


@pytest.mark.parametrize("join", [None, False])
async def test_default_and_explicit_false_leave_injected_runner_serial(
    tmp_path: Path, join: bool | None
) -> None:
    selected = case(tmp_path, "service", join=join)
    runner = selected.runner
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await asyncio.sleep(0)
        assert not runner.entered[(1, "Service")].is_set()
        runner.release[(1, "Unit")].set()
        await entered(runner, 1, "Service")
        assert runner.peak == 1
        runner.open_all()
        assert (await task).process is not None
        assert runner.peak == 1 and runner.active == 0
    finally:
        await settle(task, runner)


@pytest.mark.parametrize("probe_type", [SystemdProbe, ManagedSystemdProbe])
async def test_default_probe_preserves_post_await_shared_budget_charging(
    tmp_path: Path, probe_type: type[SystemdProbe]
) -> None:
    service, bus, proc = fixture(tmp_path)
    limits = ProbeLimits(seconds=1, command_bytes=4096)
    total = limits.command_bytes
    used = active = peak = 0
    charges: list[int] = []

    async def shared(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        nonlocal used, active, peak
        available = min(limit, total - used)
        assert available > 0
        active += 1
        peak = max(peak, active)
        try:
            # Like _SetupIO, the remaining allowance is granted before await.
            # A second in-flight call would reuse an allowance that is not reserved.
            assert active == 1, "An injected shared-budget runner must remain serial"
            await asyncio.sleep(0)
            result = await bus(argv, timeout=timeout, limit=available, merge_stderr=merge_stderr)
            charge = 0
            for channel in ("output", "stderr"):
                data = result.get(channel, "")
                assert isinstance(data, str)
                charge += len(data.encode("utf-8"))
            assert charge <= available
            used += charge
            charges.append(charge)
            assert used <= total
            return result
        finally:
            active -= 1

    probe = probe_type(runner=shared, limits=limits, proc_root=proc)
    observed = await probe.read(service)
    assert observed.process is not None
    assert len(charges) == len(bus.calls) == 5
    assert 0 < used == sum(charges) <= total
    assert peak == 1 and active == 0


@pytest.mark.parametrize("kind", ["service", "helper"])
async def test_unit_error_wins_and_blocked_service_is_collected_before_return(
    tmp_path: Path, kind: Kind
) -> None:
    selected = case(tmp_path, kind)
    runner = selected.runner
    original = ObservationError("unit_failed")
    runner.errors[(1, "Unit")] = original
    runner.cleanup_block.add((1, "Service"))
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await entered(runner, 1, "Service")
        runner.release[(1, "Unit")].set()
        await asyncio.wait_for(runner.cleanup_entered[(1, "Service")].wait(), 1)
        assert not task.done(), "The original error cannot escape before sibling cleanup"
        runner.cleanup_release[(1, "Service")].set()
        with pytest.raises(ObservationError) as caught:
            await task
        assert caught.value is original
        assert (1, "Service") in runner.cancelled
        assert runner.settled == {(1, "Unit"), (1, "Service")}
        assert runner.active == 0 and not runner.entered[(2, "Unit")].is_set()
    finally:
        await settle(task, runner)


@pytest.mark.parametrize("unit_fails", [False, True])
async def test_service_failure_does_not_overtake_pending_unit_or_wrap_original_exception(
    tmp_path: Path, unit_fails: bool
) -> None:
    selected = case(tmp_path, "service")
    runner = selected.runner
    unit_error = ObservationError("unit_failed")
    service_error = ProcessOutputLimitError("service_failed")
    runner.errors[(1, "Service")] = service_error
    if unit_fails:
        runner.errors[(1, "Unit")] = unit_error
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await entered(runner, 1, "Service")
        runner.release[(1, "Service")].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done() and (1, "Unit") not in runner.cancelled
        runner.release[(1, "Unit")].set()
        with pytest.raises(Exception) as caught:
            await task
        assert caught.value is (unit_error if unit_fails else service_error)
        assert runner.settled == {(1, "Unit"), (1, "Service")}
        assert runner.active == 0
    finally:
        await settle(task, runner)


@pytest.mark.parametrize("kind", ["service", "helper"])
async def test_external_cancel_collects_both_interfaces_without_starting_second_round(
    tmp_path: Path, kind: Kind
) -> None:
    selected = case(tmp_path, kind)
    runner = selected.runner
    runner.cleanup_block.add((1, "Service"))
    task = asyncio.create_task(selected.read())
    try:
        await entered(runner, 1, "Unit")
        await entered(runner, 1, "Service")
        task.cancel()
        await asyncio.wait_for(runner.cleanup_entered[(1, "Service")].wait(), 1)
        assert not task.done(), "External cancellation must still settle its owned sibling"
        runner.cleanup_release[(1, "Service")].set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.cancelled == runner.settled == {(1, "Unit"), (1, "Service")}
        assert runner.active == 0 and not runner.entered[(2, "Unit")].is_set()
    finally:
        await settle(task, runner)


@pytest.mark.parametrize("custom_runner", [False, True])
async def test_real_helper_constructor_opts_in_only_for_owned_default_runner(
    environment: Environment,
    current_controller: list[str],
    monkeypatch: pytest.MonkeyPatch,
    custom_runner: bool,
) -> None:
    observed: list[tuple[CommandRunner, bool]] = []

    class ReachedConstructor(Exception):
        pass

    def construct(
        *, runner: CommandRunner, limits: ProbeLimits, join_interface_reads: bool = False
    ) -> ManagedSystemdProbe:
        observed.append((runner, join_interface_reads))
        assert limits.seconds == environment.anchor.helper.operation_timeout / 4
        raise ReachedConstructor

    async def foreign(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        raise AssertionError("The constructor route must not execute a command")

    monkeypatch.setattr(managed_host, "ManagedSystemdProbe", construct)
    options: dict[str, Any] = {"runner": foreign} if custom_runner else {}
    with pytest.raises(ReachedConstructor):
        await managed_host.run_managed_helper(
            environment.journal.directory.path, clock=environment.clock, **options
        )
    assert len(observed) == 1
    assert observed[0][0] is (foreign if custom_runner else run_process)
    assert observed[0][1] is (not custom_runner)
    assert current_controller


@pytest.mark.parametrize("falsey", [False, True])
async def test_injected_helper_probe_is_not_reconstructed_or_given_an_opt_in(
    environment: Environment,
    current_controller: list[str],
    monkeypatch: pytest.MonkeyPatch,
    falsey: bool,
) -> None:
    class InjectedProbe(Probe):
        def __bool__(self) -> bool:
            return not falsey

    provided = InjectedProbe()
    captured: list[object] = []

    class ReachedBackend(Exception):
        pass

    def construct(*args: object, **kwargs: object) -> None:
        raise AssertionError("An injected probe must retain its caller's policy")

    class Backend(ManagedLinuxBackend):
        async def __aenter__(self) -> Self:
            captured.append(self.probe)
            await self.client.aclose()
            raise ReachedBackend

    monkeypatch.setattr(managed_host, "ManagedSystemdProbe", construct)
    monkeypatch.setattr(managed_host, "ManagedLinuxBackend", Backend)
    with pytest.raises(ReachedBackend):
        await managed_host.run_managed_helper(
            environment.journal.directory.path, probe=provided, clock=environment.clock
        )
    assert len(captured) == 1 and (captured[0] is provided)
    assert len(provided.helper.reads) == 1 and current_controller
    assert provided._join_interface_reads is False


async def test_real_backend_retains_falsey_injected_probe(host: Host) -> None:
    class FalseyProbe(BackendProbe):
        def __bool__(self) -> bool:
            return False

    provided = FalseyProbe(host)
    provided.snapshot = host.probe.snapshot
    async with ManagedLinuxBackend(
        host.anchor,
        host.slots["base"],
        host.slots,
        host.journal,
        control=host.control,
        clock=host.clock,
        probe=provided,
        runner=host.command,
        transport=host.transport,
        cgroup_root=host.events.parents[2],
    ) as backend:
        assert backend.probe is provided
        observed = await backend.observe(host.record())
        assert observed.service == "running"
        assert provided.count == 2 and host.probe.count == 0
        assert not host.commands
