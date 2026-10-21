from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from test_candidate_process import FakeProbe, checked_client, low_level, spec_for, unit_for

from muli.candidate_policy import CandidateError, CandidateLimits
from muli.platforms import candidate_process as backend
from muli.platforms import candidate_process_state as state_module
from muli.platforms import candidate_recovery as recovery
from muli.platforms.candidate_process import (
    CandidateObservation,
    CandidateProcessError,
    run_candidate_process,
)
from muli.platforms.candidate_process_state import (
    AcceptedUnit,
    PendingProcess,
    PersistProcess,
    ProcessPersistence,
)
from muli.platforms.linux_transaction import ObservationError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux candidate ownership")
NAME = "muli-candidate-" + "a" * 32 + ".service"
BOOT = "b" * 32


@pytest.fixture(autouse=True)
def boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("candidate_recovery_fake_must_not_spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(state_module, "current_boot", lambda: BOOT)
    monkeypatch.setattr(recovery, "current_boot", lambda: BOOT)


@pytest.fixture
def fake_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def permit(*args: Any, **kwargs: Any) -> None:
        probe: FakeProbe = kwargs["probe"]
        checked_client(probe)
        probe.calls.append("permit")
        probe.permitted.set()

    monkeypatch.setattr(backend, "_permit", permit)


class RecoveryProbe(FakeProbe):
    def __init__(self) -> None:
        super().__init__()
        checked_client(self)
        self.started.set()  # This object only observes an earlier manager request.
        self.bad_resources = False

    async def recoverable(
        self, name: str, argv: tuple[str, ...], limits: CandidateLimits, runtime_us: int
    ) -> CandidateObservation | None:
        checked_client(self)
        self.calls.append("recoverable")
        if self.bad_resources:
            raise ObservationError("candidate_resource_limits")
        return await self.owned(name, argv)


def pending_for(
    tmp_path: Path, phase: Literal["pending", "observed"] = "pending"
) -> PendingProcess:
    spec = spec_for(tmp_path)
    control = spec.private_root / ("process-" + "c" * 32 + ".json")
    control.write_text('{"argv":["private-command"],"env":{"PRIVATE_TOKEN":"secret"}}')
    control.chmod(0o600)
    worker = (
        sys.executable,
        "-I",
        "-S",
        str(Path(backend.__file__).with_name("_candidate_worker.py")),
        str(control),
    )
    state = ProcessPersistence.create(spec, NAME, worker, 4_000_000, lambda value: None)
    if phase == "observed":
        state.accept(AcceptedUnit.capture(unit_for(NAME, worker)))
    return state.value


async def test_real_backend_persists_before_launch_permission_and_after_reap(
    tmp_path: Path, fake_gate: None
) -> None:
    probe = FakeProbe()
    probe.client_done = True
    saved: list[PendingProcess] = []
    secrets: list[str] = []

    def persist(value: PendingProcess) -> None:
        if value.phase == "pending":
            assert not probe.started.is_set()
            body = json.loads(Path(value.control_file.path).read_bytes())
            secrets.append(body["nonce"])
        elif value.phase == "observed":
            assert probe.started.is_set() and not probe.permitted.is_set()
        else:
            assert not probe.live and probe.client_settled.is_set()
        saved.append(value)

    result = await run_candidate_process(
        spec_for(
            tmp_path,
            argv=("/private/candidate-command", "private-argument"),
            env=(("PRIVATE_TOKEN", "private-secret"),),
        ),
        probe=probe,
        runner=checked_client(probe),
        persist=persist,
    )
    assert result.returncode == 0
    assert [value.phase for value in saved] == ["pending", "observed", "settled"]
    assert len({value.binding_digest for value in saved}) == 1
    assert saved[1].accepted is not None and saved[1].accepted.start_ticks == 1
    data = saved[-1].model_dump_json()
    assert all(
        private not in data
        for private in [*secrets, "private-argument", "private-secret", "PRIVATE_TOKEN"]
    )
    assert not Path(saved[-1].control_file.path).exists()


async def test_pending_commit_failure_or_async_callback_cannot_launch(
    tmp_path: Path, fake_gate: None
) -> None:
    probe = FakeProbe()

    def persist(value: PendingProcess) -> None:
        raise RuntimeError("private commit detail")

    with pytest.raises(CandidateProcessError, match="candidate_process_unavailable"):
        await run_candidate_process(
            spec_for(tmp_path), probe=probe, runner=checked_client(probe), persist=persist
        )
    assert not probe.started.is_set() and not probe.permitted.is_set()


async def test_lost_pending_commit_return_retains_cross_boot_recovery_evidence(
    tmp_path: Path, fake_gate: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = FakeProbe()
    saved: list[PendingProcess] = []

    def persist(value: PendingProcess) -> None:
        saved.append(value)  # The durable plan already contains this pending identity.
        raise RuntimeError("pending committed but return lost")

    with pytest.raises(CandidateProcessError, match="candidate_process_unavailable"):
        await run_candidate_process(
            spec_for(tmp_path), probe=probe, runner=checked_client(probe), persist=persist
        )
    assert [value.phase for value in saved] == ["pending"]
    assert not probe.started.is_set() and not probe.permitted.is_set()
    pending = saved[0]
    assert Path(pending.control_file.path).exists()
    pending.check_files()
    monkeypatch.setattr(recovery, "current_boot", lambda: "d" * 32)
    observer = RecoveryProbe()
    observer.present = observer.live = False
    settled = await recovery.recover_candidate_process(
        pending, persist=saved.append, probe=observer
    )
    assert settled.phase == "settled" and saved[-1] == settled
    assert "stop" not in observer.calls and "kill" not in observer.calls


async def test_settled_replay_does_not_need_removed_control_or_manager(tmp_path: Path) -> None:
    value = pending_for(tmp_path, "observed").model_copy(update={"phase": "settled"})
    Path(value.control_file.path).unlink()
    observer = RecoveryProbe()

    def persist(entry: PendingProcess) -> None:
        raise AssertionError("settled_proof_must_not_be_rewritten")

    result = await recovery.recover_candidate_process(value, persist=persist, probe=observer)
    assert result == value and observer.calls == [] and observer.live


async def test_async_persistence_is_rejected_without_leaking_coroutine(
    tmp_path: Path, fake_gate: None
) -> None:
    probe = FakeProbe()

    async def persist(value: PendingProcess) -> None:
        raise AssertionError("async_callback_must_not_run")

    with pytest.raises(CandidateProcessError, match="candidate_process_unavailable"):
        await run_candidate_process(
            spec_for(tmp_path),
            probe=probe,
            runner=checked_client(probe),
            persist=cast(PersistProcess, persist),
        )
    assert not probe.started.is_set() and not probe.permitted.is_set()


async def test_lost_observed_commit_return_never_sends_permission(
    tmp_path: Path, fake_gate: None
) -> None:
    probe = FakeProbe()
    saved: list[PendingProcess] = []

    def persist(value: PendingProcess) -> None:
        saved.append(value)
        if value.phase == "observed":
            raise RuntimeError("committed but return lost")

    with pytest.raises(CandidateProcessError, match="candidate_process_failed") as failed:
        await run_candidate_process(
            spec_for(tmp_path), probe=probe, runner=checked_client(probe), persist=persist
        )
    assert failed.value.cleanup_confirmed
    assert not probe.permitted.is_set() and not probe.live and probe.client_settled.is_set()
    assert [value.phase for value in saved] == ["pending", "observed", "settled"]


async def test_lost_settled_commit_return_keeps_evidence_for_recovery(
    tmp_path: Path, fake_gate: None
) -> None:
    probe = FakeProbe()
    probe.client_done = True
    saved: list[PendingProcess] = []

    def persist(value: PendingProcess) -> None:
        if value.phase == "settled":
            raise RuntimeError("commit return lost")
        saved.append(value)

    with pytest.raises(CandidateProcessError) as failed:
        await run_candidate_process(
            spec_for(tmp_path), probe=probe, runner=checked_client(probe), persist=persist
        )
    assert failed.value.cleanup_confirmed and not probe.live and probe.client_settled.is_set()
    value = saved[-1]
    assert value.phase == "observed" and Path(value.control_file.path).exists()
    observer = RecoveryProbe()
    observer.present = observer.live = False
    result = await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
    assert result.phase == "settled" and saved[-1] == result
    assert "stop" not in observer.calls and not observer.permitted.is_set()


async def test_expired_pending_absence_is_not_a_settled_start(tmp_path: Path) -> None:
    value = pending_for(tmp_path).model_copy(update={"deadline": 0.001})
    observer = RecoveryProbe()
    observer.present = observer.live = False
    saved: list[PendingProcess] = []
    with pytest.raises(CandidateProcessError, match="candidate_start_unconfirmed") as failed:
        await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
    assert not failed.value.cleanup_confirmed and failed.value.unit_name == NAME
    assert saved == [] and "stop" not in observer.calls and "empty" not in observer.calls


@pytest.mark.parametrize("already_observed", [False, True])
async def test_recovery_stops_only_the_persisted_start_and_settles(
    tmp_path: Path, already_observed: bool
) -> None:
    value = pending_for(tmp_path, "observed" if already_observed else "pending")
    observer = RecoveryProbe()
    observer.hardkill = True
    saved: list[PendingProcess] = []
    result = await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
    assert result.phase == "settled" and result.binding_digest == value.binding_digest
    assert "stop" in observer.calls and "kill" in observer.calls
    assert not observer.live and not observer.permitted.is_set()
    assert [entry.phase for entry in saved] == (
        ["settled"] if already_observed else ["observed", "settled"]
    )


@pytest.mark.parametrize("empty", [False, True])
async def test_previously_observed_absence_still_requires_empty_cgroup(
    tmp_path: Path, empty: bool
) -> None:
    value = pending_for(tmp_path, "observed")
    observer = RecoveryProbe()
    observer.present, observer.live = False, not empty
    saved: list[PendingProcess] = []
    if empty:
        assert (
            await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
        ).phase == "settled"
    else:
        with pytest.raises(CandidateProcessError) as failed:
            await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
        assert not failed.value.cleanup_confirmed and saved == []
    assert "stop" not in observer.calls and "empty" in observer.calls


@pytest.mark.parametrize("failure", ["invocation", "resources", "stop"])
async def test_foreign_or_unobservable_tree_never_gets_a_settled_receipt(
    tmp_path: Path, failure: str
) -> None:
    value = pending_for(tmp_path, "observed")
    observer = RecoveryProbe()
    observer.foreign_cleanup = failure == "invocation"
    observer.bad_resources = failure == "resources"
    observer.fail_stop = failure == "stop"
    saved: list[PendingProcess] = []
    with pytest.raises(CandidateProcessError) as failed:
        await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
    assert not failed.value.cleanup_confirmed and saved == []
    assert ("stop" in observer.calls) == (failure == "stop")


@pytest.mark.parametrize("changed", ["root", "control"])
async def test_recovery_rejects_changed_private_files_before_any_manager_effect(
    tmp_path: Path, changed: str
) -> None:
    value = pending_for(tmp_path, "observed")
    if changed == "root":
        root = Path(value.root)
        root.rename(root.with_name("retained-old"))
        root.mkdir(mode=0o700)
    else:
        Path(value.control_file.path).write_text("changed private content")
    observer = RecoveryProbe()
    saved: list[PendingProcess] = []
    with pytest.raises(CandidateProcessError) as failed:
        await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
    assert not failed.value.cleanup_confirmed and saved == [] and observer.calls == []


@pytest.mark.parametrize("present", [False, True])
async def test_new_kernel_boot_requires_actual_boot_and_no_same_name_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool
) -> None:
    value = pending_for(tmp_path)
    monkeypatch.setattr(recovery, "current_boot", lambda: "d" * 32)
    observer = RecoveryProbe()
    observer.present = observer.live = present
    saved: list[PendingProcess] = []
    if present:
        with pytest.raises(CandidateProcessError, match="candidate_invocation_changed"):
            await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
        assert saved == []
    else:
        result = await recovery.recover_candidate_process(
            value, persist=saved.append, probe=observer
        )
        assert result.phase == "settled" and result.accepted is None and saved == [result]
    assert "stop" not in observer.calls and "kill" not in observer.calls


async def test_final_boot_change_cannot_unlock_after_empty_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = pending_for(tmp_path, "observed")
    boots = iter([BOOT, "d" * 32])
    monkeypatch.setattr(recovery, "current_boot", lambda: next(boots))
    observer = RecoveryProbe()
    saved: list[PendingProcess] = []
    with pytest.raises(CandidateProcessError, match="candidate_boot_changed"):
        await recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
    assert not observer.live and saved == []


async def test_repeated_cancel_waits_for_cleanup_and_persistence(tmp_path: Path) -> None:
    value = pending_for(tmp_path, "observed")
    observer = RecoveryProbe()
    observer.cleanup_wait.clear()
    saved: list[PendingProcess] = []
    task = asyncio.create_task(
        recovery.recover_candidate_process(value, persist=saved.append, probe=observer)
    )
    await asyncio.wait_for(observer.stopped.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and observer.live and saved == []
    observer.cleanup_wait.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert not observer.live and [entry.phase for entry in saved] == ["settled"]


async def test_recovery_commit_failure_is_not_reported_as_cleanup_success(tmp_path: Path) -> None:
    value = pending_for(tmp_path, "observed")
    observer = RecoveryProbe()

    def persist(entry: PendingProcess) -> None:
        raise RuntimeError("private DB failure")

    with pytest.raises(CandidateProcessError) as failed:
        await recovery.recover_candidate_process(value, persist=persist, probe=observer)
    assert not failed.value.cleanup_confirmed and not observer.live
    assert str(failed.value) == "candidate_cleanup_unconfirmed"
    assert Path(value.control_file.path).exists()


async def test_recoverable_reuses_the_typed_resource_parser(tmp_path: Path) -> None:
    probe, bus, argv, _, _ = low_level(tmp_path)

    async def exists(name: str) -> bool:
        assert name == NAME
        return True  # The reused helper bus's listing is restricted to its helper name.

    probe.exists = exists  # type: ignore[method-assign]
    assert await probe.recoverable(NAME, argv, CandidateLimits(), 180_000_000) is not None
    bus.rows["MemorySwapMax"] = ("t", 1)
    with pytest.raises(ObservationError, match="candidate_resource_limits"):
        await probe.recoverable(NAME, argv, CandidateLimits(), 180_000_000)
    assert not any("StopUnit" in call or "KillUnit" in call for call in bus.calls)


def test_private_attachment_roundtrips_with_a_fixed_budget(tmp_path: Path) -> None:
    value = pending_for(tmp_path, "observed")
    data = value.model_dump_json().encode()
    assert len(data) < value.limits.metadata_bytes
    assert PendingProcess.from_json(data, len(data)) == value
    with pytest.raises(CandidateError, match="candidate_process_state_invalid"):
        PendingProcess.from_json(data, len(data) - 1)
    duplicate = b'{"version":1,' + data[1:]
    with pytest.raises(CandidateError, match="candidate_process_state_invalid"):
        PendingProcess.from_json(duplicate, len(duplicate))


@pytest.mark.parametrize(
    "changes",
    [
        {"unit": "muli.service"},
        {"version": 2},
        {"phase": "observed"},
        {"python": "relative-python"},
        {"runtime_us": 180_000_001},
        {"argv": ["must-not-be-persisted"]},
    ],
)
def test_private_attachment_cannot_name_arbitrary_units_or_commands(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    value = pending_for(tmp_path)
    body = {**value.model_dump(mode="json"), **changes}
    data = json.dumps(body).encode()
    with pytest.raises(CandidateError, match="candidate_process_state_invalid"):
        PendingProcess.from_json(data, len(data))
