from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_linux_helper_probe import setup as helper_setup

from muli.candidate_policy import CandidateLimits, ProcessSpec
from muli.platforms import candidate_process as module
from muli.platforms.candidate_process import (
    CandidateObservation,
    CandidateProbe,
    CandidateProcessError,
    run_candidate_process,
)
from muli.platforms.linux_transaction import ObservationError, ProcessIdentity, UnitSnapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux credential and cgroup gate")
NAME = "muli-candidate-" + "a" * 32 + ".service"


def spec_for(tmp_path: Path, **changes: Any) -> ProcessSpec:
    root = tmp_path / "private"
    root.mkdir(mode=0o700, exist_ok=True)
    return replace(
        ProcessSpec(
            argv=(sys.executable, "-I", "-S", "-c", "print('candidate')"),
            env=(),
            cwd=root,
            private_root=root,
            deadline=time.monotonic() + 5,
            limits=CandidateLimits(
                seconds=5.0, preflight_seconds=2.0, stop_seconds=0.2, poll_interval=0.05
            ),
            disk_mode="threshold",
            observe=lambda: None,
        ),
        **changes,
    )


def unit_for(name: str, argv: tuple[str, ...]) -> UnitSnapshot:
    return UnitSnapshot(
        name,
        "loaded",
        "active",
        "running",
        "/run/user/transient/" + name,
        "",
        (),
        False,
        "1" * 32,
        (0, "/"),
        "exec",
        os.getpid(),
        "/app.slice/" + name,
        argv[0],
        argv,
        False,
        "control-group",
        True,
    )


class FakeProbe(CandidateProbe):
    def __init__(self) -> None:
        super().__init__(runner=self.run_client)
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.permitted = asyncio.Event()
        self.stopped = asyncio.Event()
        self.cleanup_wait = asyncio.Event()
        self.cleanup_wait.set()
        self.live = True
        self.present = True
        self.fail_proof = False
        self.fail_stop = False
        self.hardkill = False
        self.name = ""
        self.worker: tuple[str, ...] = ()
        self.unit: UnitSnapshot | None = None
        self.client_done = False
        self.client_settled = asyncio.Event()
        self.fail_client_cleanup = False
        self.foreign_cleanup = False
        self.result: dict[str, Any] = {
            "exit_code": 0,
            "output": "ok",
            "stderr": "",
            "truncated": False,
        }
        assert self.run_client.__func__ is FakeProbe.run_client

    async def exists(self, name: str) -> bool:
        self.name = name
        return self.started.is_set() and self.present

    async def waiting(
        self, name: str, argv: tuple[str, ...], limits: CandidateLimits, runtime_us: int
    ) -> CandidateObservation:
        self.calls.append("proof")
        self.worker = argv
        self.unit = unit_for(name, argv)
        if self.fail_proof:
            raise ObservationError("bad_kernel_limits")
        return CandidateObservation(
            self.unit,
            ProcessIdentity(
                os.getpid(),
                1,
                os.geteuid(),
                str(Path(argv[0]).resolve()),
                argv,
                self.unit.cgroup,
            ),
        )

    async def owned(self, name: str, argv: tuple[str, ...]) -> CandidateObservation | None:
        self.calls.append("owned")
        if not self.present:
            return None
        unit = unit_for(name, argv)
        if self.foreign_cleanup:
            unit = replace(unit, invocation="2" * 32)
        if not self.live:
            unit = replace(unit, pid=0, active_state="inactive", sub_state="dead")
        return CandidateObservation(unit)

    async def stop(self, name: str) -> None:
        self.calls.append("stop")
        self.stopped.set()
        await self.cleanup_wait.wait()
        if self.fail_stop:
            raise ObservationError("manager_unavailable")
        if not self.hardkill:
            self.live = False

    async def kill(self, name: str) -> None:
        self.calls.append("kill")
        self.live = False

    def empty(self, cgroup: str) -> bool:
        self.calls.append("empty")
        return not self.live

    async def run_client(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert "--property=Restart=no" in argv and "--property=Type=exec" in argv
        assert "--pipe" in argv and "--wait" in argv and merge_stderr is False
        self.started.set()
        try:
            await self.permitted.wait()
            if not self.client_done:
                await asyncio.Event().wait()
            return self.result
        finally:
            self.client_settled.set()
            if self.fail_client_cleanup:
                raise PermissionError("private cleanup failure")


def checked_client(probe: FakeProbe) -> module.CommandRunner:
    client = probe.run_client
    assert client.__self__ is probe and client.__func__ is FakeProbe.run_client
    return client


@pytest.fixture(autouse=True)
def forbid_real_spawn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append(tuple(args))
        raise AssertionError("fake_backend_must_not_spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    return calls


@pytest.fixture
def fake_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def permit(*args: Any, **kwargs: Any) -> None:
        probe: FakeProbe = kwargs["probe"]
        assert probe.calls[-1] == "proof"
        probe.calls.append("permit")
        kwargs["spec"].observe()
        probe.permitted.set()

    monkeypatch.setattr(module, "_permit", permit)


async def test_client_exit_does_not_replace_cgroup_cleanup(
    tmp_path: Path,
    fake_gate: None,
) -> None:
    probe = FakeProbe()
    probe.client_done = True
    result = await run_candidate_process(
        spec_for(tmp_path), probe=probe, runner=checked_client(probe)
    )
    assert result.returncode == 0 and result.stdout == "ok"
    assert probe.calls.index("proof") < probe.calls.index("permit") < probe.calls.index("stop")
    assert not probe.live and probe.client_settled.is_set()
    assert list((tmp_path / "private").iterdir()) == []


@pytest.mark.parametrize("explicit_default", [False, True])
async def test_wrong_default_runner_hits_hard_spawn_boundary(
    tmp_path: Path,
    forbid_real_spawn: list[tuple[str, ...]],
    explicit_default: bool,
) -> None:
    probe = FakeProbe()
    checked_client(probe)  # Still check the intended fake binding at this entry.
    spec = spec_for(tmp_path)
    with pytest.raises(CandidateProcessError):
        if explicit_default:
            await run_candidate_process(spec, probe=probe, runner=module.run_process)
        else:
            await run_candidate_process(spec, probe=probe)
    assert len(forbid_real_spawn) == 1 and forbid_real_spawn[0][0] == "systemd-run"
    assert not probe.started.is_set() and "permit" not in probe.calls


async def test_stop_grace_escalates_to_all_process_kill(tmp_path: Path, fake_gate: None) -> None:
    probe = FakeProbe()
    probe.client_done = True
    probe.hardkill = True
    await run_candidate_process(spec_for(tmp_path), probe=probe, runner=checked_client(probe))
    assert probe.calls.index("stop") < probe.calls.index("kill")
    assert not probe.live


async def test_bad_kernel_proof_never_permits_effect(tmp_path: Path, fake_gate: None) -> None:
    probe = FakeProbe()
    probe.fail_proof = True
    with pytest.raises(CandidateProcessError, match="candidate_process_failed") as failure:
        await run_candidate_process(spec_for(tmp_path), probe=probe, runner=checked_client(probe))
    assert failure.value.cleanup_confirmed
    assert "permit" not in probe.calls and not probe.live and probe.client_settled.is_set()


async def test_repeated_cancel_waits_for_subtree_and_client(
    tmp_path: Path,
    fake_gate: None,
) -> None:
    probe = FakeProbe()
    probe.cleanup_wait.clear()
    task = asyncio.create_task(
        run_candidate_process(spec_for(tmp_path), probe=probe, runner=checked_client(probe))
    )
    await asyncio.wait_for(probe.permitted.wait(), 2)
    task.cancel()
    await asyncio.wait_for(probe.stopped.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and probe.live and not probe.client_settled.is_set()
    probe.cleanup_wait.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not probe.live and probe.client_settled.is_set()
    assert list((tmp_path / "private").iterdir()) == []


async def test_unconfirmed_cleanup_preserves_attempt_and_fixed_error(
    tmp_path: Path,
    fake_gate: None,
) -> None:
    probe = FakeProbe()
    probe.client_done = True
    probe.fail_stop = True
    with pytest.raises(CandidateProcessError) as failure:
        await run_candidate_process(spec_for(tmp_path), probe=probe, runner=checked_client(probe))
    assert failure.value.code == "candidate_cleanup_unconfirmed"
    assert not failure.value.cleanup_confirmed and failure.value.unit_name.startswith(
        "muli-candidate-"
    )
    assert probe.live and probe.client_settled.is_set()
    assert len(list((tmp_path / "private").iterdir())) == 1
    assert "private" not in str(failure.value)


async def test_client_cleanup_failure_is_not_a_reap_receipt(
    tmp_path: Path, fake_gate: None
) -> None:
    probe = FakeProbe()
    probe.client_done = True
    probe.fail_client_cleanup = True
    with pytest.raises(CandidateProcessError) as failure:
        await run_candidate_process(spec_for(tmp_path), probe=probe, runner=checked_client(probe))
    assert not failure.value.cleanup_confirmed
    assert not probe.live and list((tmp_path / "private").iterdir())


async def test_foreign_invocation_is_never_stopped(tmp_path: Path, fake_gate: None) -> None:
    probe = FakeProbe()
    probe.client_done = True
    probe.foreign_cleanup = True
    with pytest.raises(CandidateProcessError) as failure:
        await run_candidate_process(spec_for(tmp_path), probe=probe, runner=checked_client(probe))
    assert not failure.value.cleanup_confirmed
    assert "stop" not in probe.calls and "kill" not in probe.calls
    assert probe.client_settled.is_set() and list((tmp_path / "private").iterdir())


@pytest.mark.parametrize("bad_result", ["output", "truncated", "returncode"])
async def test_bounded_output_and_result_after_cleanup(
    tmp_path: Path,
    fake_gate: None,
    bad_result: str,
) -> None:
    probe = FakeProbe()
    probe.client_done = True
    if bad_result == "output":
        probe.result["output"] = "x" * 1025
    elif bad_result == "truncated":
        probe.result["truncated"] = True
    else:
        probe.result["exit_code"] = True
    spec = spec_for(
        tmp_path,
        limits=CandidateLimits(
            seconds=5.0,
            preflight_seconds=2.0,
            stop_seconds=0.2,
            poll_interval=0.05,
            output_bytes=1024,
        ),
    )
    with pytest.raises(CandidateProcessError) as failure:
        await run_candidate_process(spec, probe=probe, runner=checked_client(probe))
    assert failure.value.cleanup_confirmed and not probe.live and probe.client_settled.is_set()


async def test_cancel_during_launch_absence_cannot_claim_no_late_unit(tmp_path: Path) -> None:
    probe = FakeProbe()
    probe.present = False
    task = asyncio.create_task(
        run_candidate_process(spec_for(tmp_path), probe=probe, runner=checked_client(probe))
    )
    await asyncio.wait_for(probe.started.wait(), 2)
    task.cancel()
    with pytest.raises(CandidateProcessError) as failure:
        await task
    assert not failure.value.cleanup_confirmed
    assert probe.client_settled.is_set() and list((tmp_path / "private").iterdir())


@pytest.mark.parametrize("change", ["strict", "expired", "cwd", "env", "metadata", "mode"])
async def test_invalid_setup_has_no_launch(tmp_path: Path, change: str) -> None:
    probe = FakeProbe()
    spec = spec_for(tmp_path)
    if change == "strict":
        spec = replace(spec, disk_mode="strict")
    elif change == "expired":
        spec = replace(spec, deadline=time.monotonic() - 1)
    elif change == "cwd":
        spec = replace(spec, cwd=tmp_path)
    elif change == "env":
        spec = replace(spec, env=(("A", "1"), ("A", "2")))
    elif change == "metadata":
        spec = replace(spec, argv=(sys.executable, "x" * spec.limits.metadata_bytes))
    else:
        spec.private_root.chmod(0o755)
    with pytest.raises(CandidateProcessError):
        await run_candidate_process(spec, probe=probe, runner=checked_client(probe))
    assert not probe.started.is_set()


async def test_threshold_observation_failure_stops_owned_process(
    tmp_path: Path, fake_gate: None
) -> None:
    probe = FakeProbe()
    calls = 0

    def observe() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("private directory detail")

    with pytest.raises(CandidateProcessError, match="candidate_process_failed") as failure:
        await run_candidate_process(
            spec_for(tmp_path, observe=observe), probe=probe, runner=checked_client(probe)
        )
    assert failure.value.cleanup_confirmed and not probe.live and calls == 3


async def test_total_deadline_stops_owned_process(tmp_path: Path, fake_gate: None) -> None:
    probe = FakeProbe()
    spec = spec_for(tmp_path, deadline=time.monotonic() + 0.15)
    with pytest.raises(CandidateProcessError, match="candidate_process_timeout") as failure:
        await run_candidate_process(spec, probe=probe, runner=checked_client(probe))
    assert failure.value.cleanup_confirmed and not probe.live and probe.client_settled.is_set()


async def test_final_observation_cannot_hide_expired_deadline(
    tmp_path: Path,
    fake_gate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = FakeProbe()
    probe.client_done = True
    calls = 0
    expired = False
    original = module._remaining

    def remaining(deadline: float) -> float:
        if expired:
            raise TimeoutError
        return original(deadline)

    def observe() -> None:
        nonlocal calls, expired
        calls += 1
        if calls == 3:
            expired = True

    monkeypatch.setattr(module, "_remaining", remaining)
    with pytest.raises(CandidateProcessError, match="candidate_process_timeout") as failure:
        await run_candidate_process(
            spec_for(tmp_path, observe=observe), probe=probe, runner=checked_client(probe)
        )
    assert calls == 3 and failure.value.cleanup_confirmed and not probe.live


def low_level(tmp_path: Path) -> tuple[CandidateProbe, Any, tuple[str, ...], Path, Path]:
    helper, bus, argv, group, proc = helper_setup(tmp_path)
    new_group = group.with_name(NAME)
    group.rename(new_group)
    cgroup = "/user.slice/app.slice/" + NAME
    bus.rows.update(
        Id=("s", NAME),
        ControlGroup=("s", cgroup),
        Restart=("s", "no"),
        MemoryMax=("t", 512 * 1024 * 1024),
        CPUQuotaPerSecUSec=("t", 1_000_000),
        TasksMax=("t", 64),
    )
    (proc / "123/cgroup").write_text("0::" + cgroup + "\n")
    (new_group / "memory.max").write_text(str(512 * 1024 * 1024))
    (new_group / "pids.max").write_text("64")
    (new_group / "cpu.max").write_text("100000 100000")
    return (
        CandidateProbe(runner=bus, proc_root=proc, cgroup_root=helper.cgroup_root),
        bus,
        argv,
        new_group,
        proc,
    )


async def test_actual_typed_proc_kernel_double_observation(tmp_path: Path) -> None:
    probe, bus, argv, _, _ = low_level(tmp_path)
    result = await probe.waiting(NAME, argv, CandidateLimits(), 180_000_000)
    assert result.process is not None and result.process.pid == 123
    assert len(bus.calls) == 9
    assert all("GetUnit" in call or "get-property" in call for call in bus.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Restart", "on-failure"),
        ("Type", "simple"),
        ("Transient", False),
        ("PartOf", ["muli.service"]),
        ("Requires", ["muli.service"]),
        ("MemoryMax", 2**64 - 1),
        ("MemorySwapMax", 1),
        ("TasksMax", 128),
        ("CPUQuotaPerSecUSec", 2_000_000),
        ("RuntimeMaxUSec", 180_000_001),
        ("KillMode", "process"),
        ("SendSIGKILL", False),
        ("OOMPolicy", "continue"),
    ],
)
async def test_resource_or_lifetime_declaration_mismatch_refuses(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    probe, bus, argv, _, _ = low_level(tmp_path)
    bus.rows[field] = bus.rows[field][0], value
    with pytest.raises(ObservationError):
        await probe.waiting(NAME, argv, CandidateLimits(), 180_000_000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory.max", "max"),
        ("memory.swap.max", "1"),
        ("pids.max", "65"),
        ("cpu.max", "100001 100000"),
        ("memory.oom.group", "0"),
    ],
)
async def test_kernel_limits_cannot_be_replaced_by_declared_properties(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    probe, _, argv, group, _ = low_level(tmp_path)
    (group / field).write_text(value)
    with pytest.raises(ObservationError):
        await probe.waiting(NAME, argv, CandidateLimits(), 180_000_000)


async def test_second_observation_rejects_replaced_process(tmp_path: Path) -> None:
    probe, _, argv, _, proc = low_level(tmp_path)
    original = probe._snapshot
    calls = 0

    async def snapshot(path: str) -> UnitSnapshot:
        nonlocal calls
        calls += 1
        if calls == 2:
            (proc / "123/cmdline").write_bytes(b"foreign\x00")
        return await original(path)

    probe._snapshot = snapshot  # type: ignore[method-assign]
    with pytest.raises(ObservationError):
        await probe.waiting(NAME, argv, CandidateLimits(), 180_000_000)


@pytest.mark.parametrize("listing", [[[]], [], [None], [[[]]], [[[], []]], [[], []]])
async def test_absence_requires_successful_exact_structured_listing(listing: object) -> None:
    async def run_client(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert argv[-4:] == ["asas", "0", "1", NAME]
        return {"exit_code": 0, "output": json.dumps({"type": "a(ssssssouso)", "data": listing})}

    probe = CandidateProbe(runner=run_client)
    if listing == [[]]:
        assert not await probe.exists(NAME)
    else:
        with pytest.raises(ObservationError):
            await probe.exists(NAME)
