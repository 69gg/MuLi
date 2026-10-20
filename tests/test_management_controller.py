from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest

from muli.management_controller import ServiceObservation, TransactionController
from muli.management_transaction import (
    ClockReading,
    Deadline,
    HealthProof,
    JournalControl,
    Role,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal primitive")
BASE = b'{"credential":"base-secret"}'
TARGET = b'{"credential":"target-secret"}'


@dataclass
class FakeClock:
    elapsed: float = 0.0

    def __call__(self) -> ClockReading:
        return ClockReading(
            boot_id="kernel", wall=1000.0 + self.elapsed, monotonic=100.0 + self.elapsed
        )


class FakeService:
    """An in-memory owner, never a subprocess, systemd connection or HTTP client."""

    def __init__(self) -> None:
        self.configuration = BASE
        self.service: Literal["running", "stopped", "starting", "stopping", "foreign"] = "running"
        self.boot: str | None = "old-boot"
        self.role: Role = "base"
        self.writer_free = False
        self.drained = False
        self.resumed = True
        self.released = False
        self.drain_ready = True
        self.healthy_target = True
        self.healthy_base = True
        self.activation_failed = False
        self.activation_error = False
        self.interrupt = ""
        self.calls: list[str] = []
        self.activations: set[tuple[str, str | None]] = set()

    def checkpoint(self, point: str) -> None:
        if self.interrupt == point:
            self.interrupt = ""
            raise asyncio.CancelledError

    async def observe(self, record: TransactionRecord) -> ServiceObservation:
        health: HealthProof | None = None
        healthy = self.healthy_target if self.role == "target" else self.healthy_base
        if (
            self.service == "running"
            and self.boot is not None
            and self.boot != "old-boot"
            and healthy
        ):
            challenge = record.challenge if self.role == "target" else record.rollback_challenge
            assert challenge is not None
            health = HealthProof(
                generation=record.generation,
                challenge=challenge,
                boot=self.boot,
                installation=record.identity.installation,
                service=record.identity.service,
                store_instance=record.identity.store_instance,
                main_session=record.identity.main_session,
                configuration=hashlib.sha256(self.configuration).hexdigest(),
                core_ready=True,
                admission="released" if self.released else "paused",
            )
        return ServiceObservation(
            identity_matches=True,
            configuration=hashlib.sha256(self.configuration).hexdigest(),
            service=self.service,
            boot=self.boot,
            writer_free=self.writer_free,
            drained=self.drained,
            resumed=self.resumed,
            health=health,
            health_failed=self.boot != "old-boot" and not healthy,
            activation_failed=self.activation_failed,
        )

    async def drain(self, record: TransactionRecord) -> None:
        assert record.phase == "draining"
        self.calls.append("drain")
        self.drained = self.drain_ready
        self.resumed = False

    async def resume(self, record: TransactionRecord) -> None:
        assert record.phase == "prepared"
        self.calls.append("resume")
        self.drained = False
        self.resumed = True

    async def stop(self, record: TransactionRecord, role: Role) -> None:
        assert record.intent in {"stop_base", "stop_target"}
        self.calls.append("stop:" + role)
        self.service, self.boot, self.writer_free = "stopped", None, True
        self.checkpoint("after_stop")

    async def install(self, record: TransactionRecord, role: Role, data: bytes) -> None:
        assert self.service == "stopped" and self.writer_free
        assert record.intent == ("install_target" if role == "target" else "restore_base")
        self.calls.append("install:" + role)
        self.configuration = data
        self.checkpoint("after_install:" + role)

    async def start(self, record: TransactionRecord, role: Role) -> None:
        assert record.intent == ("start_target" if role == "target" else "start_base")
        self.checkpoint("before_start:" + role)
        self.calls.append("start:" + role)
        self.role = role
        self.service, self.boot, self.writer_free = "running", role + "-boot", False
        self.drained, self.released = True, False
        self.checkpoint("after_start:" + role)

    async def activate(self, record: TransactionRecord, role: Role) -> None:
        assert record.phase == ("committed" if role == "target" else "rolled_back")
        if self.activation_error:
            raise RuntimeError("must-not-publish-secret-token")
        owner = record.generation, self.boot
        if owner not in self.activations:
            self.calls.append("activate:" + role)
            self.activations.add(owner)
        self.released = True
        self.checkpoint("after_activate")


@pytest.fixture
def setup(
    tmp_path: Path,
) -> Iterator[tuple[TransactionJournal, JournalControl, FakeClock, FakeService]]:
    clock, service = FakeClock(), FakeService()
    with TransactionJournal(tmp_path / "journal", tmp_path / "data" / "muli.db") as journal:
        with journal.writer_lease() as writer, journal.locked() as control:
            control.prepare(
                request_id="restart-plan",
                action="configure",
                identity=ServiceIdentity(
                    installation="a" * 64,
                    service="b" * 64,
                    store_instance="fixture-db",
                    main_session="main",
                    origin_boot="old-boot",
                ),
                base=BASE,
                target=TARGET,
                deadline=Deadline(created=clock(), seconds=60.0, rollback_reserve=10.0),
                writer=writer,
            )
        with journal.locked() as controller_lock:
            yield journal, controller_lock, clock, service


async def reach(
    controller: TransactionController,
    phase: str,
    intent: str | None = None,
) -> TransactionRecord:
    # Finite deterministic state transitions, not a timing/polling experiment.
    for _ in range(24):
        record = await controller.step()
        if record.phase == phase and (intent is None or record.intent == intent):
            return record
        assert not record.resolved and record.phase != "recovery_required"
    pytest.fail("expected transition was not reached in 24 steps")


async def finish(controller: TransactionController) -> TransactionRecord:
    for _ in range(24):
        record = await controller.step()
        if record.resolved or record.phase == "recovery_required" or record.activation == "blocked":
            return record
    pytest.fail("driver failed to converge in 24 steps")


async def test_target_commit_is_not_resolved_until_owned_activation_releases(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
) -> None:
    _, control, clock, service = setup
    driver = TransactionController(control, service, clock)
    record = await reach(driver, "committed")
    assert not record.resolved and not service.released
    assert "activate:target" not in service.calls
    final = await finish(driver)
    assert final.resolved and final.phase == "committed" and service.configuration == TARGET
    assert service.calls.count("start:target") == 1 and service.calls.count("activate:target") == 1
    assert not final.rollback_attempted


async def test_rollback_decision_requires_base_release_and_keeps_original_error(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
) -> None:
    _, control, clock, service = setup
    service.healthy_target = False
    driver = TransactionController(control, service, clock)
    record = await reach(driver, "rolled_back")
    assert record.rollback_attempted and not record.resolved and not service.released
    assert service.configuration == BASE
    service.activation_error = True
    blocked = await driver.step()
    assert (
        blocked.phase == "rolled_back" and blocked.activation == "blocked" and not blocked.resolved
    )
    assert blocked.failure_code == "health_failed" and blocked.code == "backend_failed"
    assert "must-not-publish-secret-token" not in str(blocked.public())
    before = list(service.calls)
    assert (await driver.step()).activation == "blocked"
    assert service.calls == before


async def test_failed_target_rolls_back_once_and_base_is_actually_released(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
) -> None:
    _, control, clock, service = setup
    service.healthy_target = False
    final = await finish(TransactionController(control, service, clock))
    assert final.phase == "rolled_back" and final.resolved and service.released
    assert service.calls.count("start:target") == service.calls.count("start:base") == 1
    assert service.calls.count("activate:base") == 1


async def test_rollback_failure_retains_unresolved_authority_and_never_retries_target(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
) -> None:
    _, control, clock, service = setup
    service.healthy_target = service.healthy_base = False
    driver = TransactionController(control, service, clock)
    final = await finish(driver)
    assert final.phase == "recovery_required" and final.rollback_attempted
    before = list(service.calls)
    assert await driver.step() == final
    assert service.calls == before and service.calls.count("start:target") == 1


@pytest.mark.parametrize(
    "point",
    [
        "after_stop",
        "after_install:target",
        "before_start:target",
        "after_start:target",
        "after_activate",
    ],
)
async def test_cancelled_owner_reentry_observes_intent_without_blind_start_retry(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    point: str,
) -> None:
    journal, control, clock, service = setup
    service.interrupt = point
    with pytest.raises(asyncio.CancelledError):
        await finish(TransactionController(control, service, clock))
    position = journal.read()
    assert position is not None
    control.__exit__()
    with journal.locked() as successor:
        final = await finish(TransactionController(successor, service, clock))
    assert final.resolved and final.deadline == position.deadline
    if point == "before_start:target":
        assert final.phase == "rolled_back" and "start:target" not in service.calls
    else:
        assert final.phase == "committed" and service.calls.count("start:target") == 1


@pytest.mark.parametrize("already_released", [False, True])
async def test_postcommit_activation_failure_new_boot_converges_without_rollback(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    already_released: bool,
) -> None:
    _, control, clock, service = setup
    driver = TransactionController(control, service, clock)
    await reach(driver, "committed")
    service.activation_failed = True
    blocked = await driver.step()
    assert blocked.activation == "blocked" and not blocked.rollback_attempted
    service.activation_failed = False
    service.boot, service.released = "new-target-boot", already_released
    final = await finish(driver)
    assert final.resolved and final.phase == "committed" and not final.rollback_attempted
    assert "start:base" not in service.calls and "base-secret" not in str(final.public())


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
async def test_deadline_crossed_during_observation_never_records_released_success(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _, control, clock, service = setup
    service.healthy_target = phase != "rolled_back"
    driver = TransactionController(control, service, clock)
    await reach(driver, phase)
    service.released = True
    original = service.observe

    async def late(record: TransactionRecord) -> ServiceObservation:
        value = await original(record)
        clock.elapsed = 61.0
        return value

    monkeypatch.setattr(service, "observe", late)
    final = await driver.step()
    assert (
        not final.resolved and final.activation == "blocked" and final.code == "deadline_exceeded"
    )


async def test_budget_consumed_by_observation_is_reserved_for_single_rollback(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, control, clock, service = setup
    driver = TransactionController(control, service, clock)
    record = await reach(driver, "verifying")
    original = service.observe

    async def delayed(current: TransactionRecord) -> ServiceObservation:
        value = await original(current)
        clock.elapsed = 51.0
        return value

    monkeypatch.setattr(service, "observe", delayed)
    rolling = await driver.step()
    assert rolling.phase == "rolling_back" and rolling.deadline == record.deadline
    monkeypatch.setattr(service, "observe", original)
    final = await finish(driver)
    assert final.phase == "rolled_back" and final.resolved


async def test_clock_invalid_after_new_start_intent_uses_current_revision(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, control, clock, service = setup
    driver = TransactionController(control, service, clock)
    await reach(driver, "activating", "install_target")
    await driver.step()  # Candidate bytes installed; next step persists start intent.
    original = control.advance

    def advance(expected: object, transition: object) -> TransactionRecord:
        from muli.management_transaction import Revision, Transition

        assert isinstance(expected, Revision) and isinstance(transition, Transition)
        result = original(expected, transition)
        if result.intent == "start_target":
            clock.elapsed = -1.0
        return result

    monkeypatch.setattr(control, "advance", advance)
    final = await driver.step()
    assert final.phase == "recovery_required" and final.code == "clock_invalid"
    assert "start:target" not in service.calls and journal.read() == final


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
@pytest.mark.parametrize("failure", ["activation_failed", "health_failed"])
async def test_explicit_owner_failure_overrides_a_contradictory_release_proof(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: str,
) -> None:
    _, control, clock, service = setup
    service.healthy_target = phase != "rolled_back"
    driver = TransactionController(control, service, clock)
    await reach(driver, phase)
    service.released = True
    original = service.observe

    async def failed(record: TransactionRecord) -> ServiceObservation:
        observed = await original(record)
        assert observed.health is not None and observed.health.admission == "released"
        return replace(
            observed,
            activation_failed=failure == "activation_failed",
            health_failed=failure == "health_failed",
        )

    monkeypatch.setattr(service, "observe", failed)
    final = await driver.step()
    assert final.activation == "blocked" and final.code == failure and not final.resolved
    assert final.phase == phase


async def test_busy_writer_and_foreign_configuration_are_never_overwritten(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
) -> None:
    _, control, clock, service = setup
    driver = TransactionController(control, service, clock)
    await reach(driver, "activating", "stop_base")
    await driver.step()  # Stop accepted by the fake owner.
    service.writer_free = False
    same = await driver.step()
    assert same.intent == "stop_base" and service.configuration == BASE
    service.writer_free = True
    service.configuration = b'{"manual-edit":"leave-me"}'
    failed = await driver.step()
    assert (
        failed.phase == "recovery_required"
        and service.configuration == b'{"manual-edit":"leave-me"}'
    )
    assert not any(item.startswith("install:") for item in service.calls)


async def test_drain_timeout_resumes_unchanged_owner_without_forced_stop(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
) -> None:
    _, control, clock, service = setup
    service.drain_ready = False
    driver = TransactionController(control, service, clock)
    await driver.step()
    await driver.step()
    clock.elapsed = 51.0
    final = await finish(driver)
    assert final.phase == "aborted" and final.resolved and service.resumed
    assert service.boot == "old-boot" and not any(
        item.startswith("stop:") for item in service.calls
    )


@pytest.mark.parametrize("elapsed", [0.0, 51.0])
@pytest.mark.parametrize("foreign", ["configuration", "boot", "service"])
async def test_prepared_never_drains_or_resumes_an_unowned_original_instance(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
    elapsed: float,
    foreign: str,
) -> None:
    _, control, clock, service = setup
    original = service.observe

    async def changed(record: TransactionRecord) -> ServiceObservation:
        observed = await original(record)
        assert observed.identity_matches
        if foreign == "configuration":
            return replace(observed, configuration=record.target.digest)
        if foreign == "boot":
            return replace(observed, boot="another-boot")
        return replace(observed, service="stopped")

    monkeypatch.setattr(service, "observe", changed)
    clock.elapsed = elapsed
    final = await TransactionController(control, service, clock).step()
    assert final.phase == "recovery_required" and final.code == "foreign_state"
    assert service.calls == []


async def test_record_fsync_failure_poisoned_owner_and_persistent_failure_prevent_stop(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, control, clock, service = setup
    driver = TransactionController(control, service, clock)
    await driver.step()
    await driver.step()
    fsync, rename = os.fsync, os.replace
    visible = False

    def replaced(
        source: str, target: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        nonlocal visible
        rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if target == "state.json":
            visible = True

    def failed_sync(descriptor: int) -> None:
        if visible and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("fixture-EIO-private-secret")
        fsync(descriptor)

    monkeypatch.setattr(os, "replace", replaced)
    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(OSError, match="fixture-EIO"):
        await driver.step()
    observed = journal.read()
    assert observed is not None and observed.intent == "stop_base"
    with pytest.raises(TransactionError, match="journal_unconfirmed"):
        await driver.step()
    control.__exit__()
    with pytest.raises(OSError), journal.locked():
        pytest.fail("a new owner adopted state despite continuing directory fsync EIO")
    assert "stop:base" not in service.calls
    monkeypatch.undo()
    with journal.locked() as successor:
        new_driver = TransactionController(successor, service, clock)
        await new_driver.step()
    assert service.calls.count("stop:base") == 1


async def test_cached_proof_without_running_owner_never_commits(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, control, clock, service = setup
    driver = TransactionController(control, service, clock)
    await reach(driver, "verifying")
    original = service.observe

    async def stale(record: TransactionRecord) -> ServiceObservation:
        return replace(await original(record), service="stopped", writer_free=True)

    monkeypatch.setattr(service, "observe", stale)
    assert (await driver.step()).phase == "rolling_back"


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
async def test_decision_missing_proof_waits_then_releases_without_extending_deadline(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _, control, clock, service = setup
    service.healthy_target = phase != "rolled_back"
    driver = TransactionController(control, service, clock)
    record = await reach(driver, phase)
    original, calls = service.observe, list(service.calls)

    async def unavailable(record: TransactionRecord) -> ServiceObservation:
        return replace(await original(record), health=None)

    monkeypatch.setattr(service, "observe", unavailable)
    assert await driver.step() == record and service.calls == calls
    monkeypatch.setattr(service, "observe", original)
    result = await finish(driver)
    assert result.resolved and result.phase == phase and result.deadline == record.deadline


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
@pytest.mark.parametrize("action", ["observe", "activate"])
async def test_short_decision_timeout_waits_but_total_deadline_still_blocks(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    action: str,
) -> None:
    _, control, clock, service = setup
    service.healthy_target = phase != "rolled_back"
    driver = TransactionController(control, service, clock, operation_timeout=0.01)
    record = await reach(driver, phase)

    async def timed_out(*args: object) -> ServiceObservation:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(service, action, timed_out)
    assert await driver.step() == record
    clock.elapsed = 61.0
    final = await driver.step()
    assert final.phase == phase and final.activation == "blocked" and not final.resolved
    assert final.code == "deadline_exceeded" and final.deadline == record.deadline


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
async def test_wrong_decision_proof_is_not_treated_as_temporarily_missing(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _, control, clock, service = setup
    service.healthy_target = phase != "rolled_back"
    driver = TransactionController(control, service, clock)
    await reach(driver, phase)
    original = service.observe

    async def wrong(record: TransactionRecord) -> ServiceObservation:
        observed = await original(record)
        assert observed.health is not None
        return replace(
            observed, health=observed.health.model_copy(update={"configuration": "f" * 64})
        )

    monkeypatch.setattr(service, "observe", wrong)
    result = await driver.step()
    assert result.activation == "blocked" and result.code == "health_failed"


async def test_clock_invalid_after_decision_timeout_is_persisted(
    setup: tuple[TransactionJournal, JournalControl, FakeClock, FakeService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, control, clock, service = setup
    driver = TransactionController(control, service, clock, operation_timeout=0.01)
    await reach(driver, "committed")

    async def timed_out(record: TransactionRecord) -> ServiceObservation:
        clock.elapsed = -1.0
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(service, "observe", timed_out)
    final = await driver.step()
    assert final.activation == "blocked" and final.code == "clock_invalid"
