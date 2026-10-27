"""Fresh paused readiness may finish two durable decisions without another observation."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import test_linux_controller as legacy
from test_linux_controller import TARGET
from test_linux_controller import Host as LegacyHost
from test_managed_activation_receipt import original_writer_is_held
from test_managed_authentication_boundaries import bounded_slots as bounded_slots
from test_managed_backend import Host
from test_managed_backend import host as host
from test_managed_observation_authentication import (
    observe_complete_package_scans,
    observe_probes,
)

from muli.managed_controller import ManagedServiceObservation, ManagedTransactionController
from muli.managed_transaction import TransactionRecordV2, TransitionV2
from muli.management_controller import TransactionController
from muli.management_transaction import JournalControl, Revision, Role, TransactionError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real journal and leases")
legacy_host = legacy.host


async def started_role(host: Host, role: Role) -> TransactionRecordV2:
    """Prepare real file effects and start intents; only manager/HTTP effects are faked."""
    installing = host.install_intent()
    await host.backend.install(installing, "target", host.data["target"])
    started = host.move("activating", "start_target")
    await host.backend.start(started, "target")
    if role == "target":
        return started
    stopping = host.move("rolling_back", "stop_target", code="health_failed")
    await host.backend.stop(stopping, "target")
    restoring = host.move("rolling_back", "restore_base", code=stopping.code)
    await host.backend.install(restoring, "base", host.data["base"])
    started = host.move("rolling_back", "start_base", code=restoring.code)
    await host.backend.start(started, "base")
    return started


@pytest.mark.parametrize("role", ["target", "base"])
async def test_fresh_paused_health_finishes_both_durable_decisions_in_one_step(
    host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    import muli.managed_controller as module

    started = await started_role(host, role)
    controller = ManagedTransactionController(
        host.control, host.backend, host.clock, operation_timeout=10.0
    )
    writer = host.writer
    assert writer is not None
    original_writer_is_held(host, writer)
    expected_health = host.proof()
    disk, service = host.backend._disk(), host.probe.snapshot
    commands, http_start, probe_start = (
        list(host.commands),
        len(host.transport.calls),
        host.probe.count,
    )
    observations: list[ManagedServiceObservation] = []
    original_observe = host.backend.observe

    async def observe(record: TransactionRecordV2) -> ManagedServiceObservation:
        same_record = record == started
        assert same_record
        observation = await original_observe(record)
        observations.append(observation)
        return observation

    moves: list[TransactionRecordV2] = []
    yielded: list[str] = []
    loop = asyncio.get_running_loop()
    original_advance = module.advance_managed

    def advance(
        control: JournalControl, expected: Revision, transition: TransitionV2
    ) -> TransactionRecordV2:
        current = control.read_current()
        assert isinstance(current, TransactionRecordV2) and current.position == expected
        assert not yielded
        moved = original_advance(control, expected, transition)
        durable = host.journal.read_current() == moved
        assert durable
        moves.append(moved)
        if len(moves) == 1:
            # A real yield between durable decisions would run this callback.
            loop.call_soon(yielded.append, "yielded")
        return moved

    monkeypatch.setattr(host.backend, "observe", observe)
    monkeypatch.setattr(module, "advance_managed", advance)
    probes = observe_probes(host, monkeypatch)
    scans = observe_complete_package_scans(host, monkeypatch)

    result = await controller.step()

    # Authenticate the full real observation and absence of extra effects first.
    assert len(observations) == 1
    observation = observations[0]
    complete_health = observation.health == expected_health
    assert complete_health
    assert observation.identity_matches and observation.service == "running"
    assert not observation.writer_free and not observation.health_failed
    assert observation.boot == expected_health.boot and started.accepts(expected_health, role)
    assert expected_health.admission == "paused" and expected_health.core_ready
    assert expected_health.generation == started.generation and expected_health.role == role
    assert len(probes) == host.probe.count - probe_start == 2
    for probe in probes:
        same_service = probe.service == service
        assert same_service
        assert role in probe.process_roles and probe.unit_confirmed
        assert probe.disk_unit == started.binding(role).unit.digest
    for root in {
        Path(host.backend.controller_slot.package.root),
        Path(host.slots[role].package.root),
    }:
        assert scans[root] > 0
    same_disk = host.backend._disk() == disk
    same_record = host.record() == result
    assert same_disk and same_record
    assert host.commands == commands
    assert host.transport.calls[http_start:] == ["readiness"]
    assert host.admission == "paused" and result.activation == "pending" and not result.resolved
    assert result.deadline == started.deadline and result.generation == started.generation
    assert result.rollback_attempted == started.rollback_attempted == (role == "base")
    assert result.code == started.code and result.failure_code == started.failure_code
    assert not yielded
    original_writer_is_held(host, writer)

    # The old controller passes every gate above, then returns only the first CAS.
    assert result.phase == ("committed" if role == "target" else "rolled_back")
    assert len(moves) == 2 and result.revision == started.revision + 2
    intermediate, decided = moves
    assert intermediate.revision == started.revision + 1 and intermediate.health is None
    assert (intermediate.phase, intermediate.intent) == (
        ("verifying", "none") if role == "target" else ("rolling_back", "verify_base")
    )
    assert intermediate.activation == "pending" and not intermediate.resolved
    same_decision = decided == result
    same_health = result.health == expected_health
    assert same_decision and same_health


@pytest.mark.parametrize("role", ["target", "base"])
@pytest.mark.parametrize("fault", ["missing", "challenge", "boot", "writer", "health"])
async def test_incomplete_or_invalid_health_keeps_the_durable_intermediate_state(
    host: Host, monkeypatch: pytest.MonkeyPatch, role: Role, fault: str
) -> None:
    started = await started_role(host, role)
    controller = ManagedTransactionController(host.control, host.backend, host.clock)
    original = host.backend.observe
    observations: list[ManagedServiceObservation] = []
    disk, commands = host.backend._disk(), list(host.commands)
    http_start = len(host.transport.calls)

    async def observe(record: TransactionRecordV2) -> ManagedServiceObservation:
        actual = await original(record)
        assert actual.health is not None and record.accepts(actual.health, role)
        observations.append(actual)
        if fault == "missing":
            return replace(actual, health=None)
        if fault in {"challenge", "boot"}:
            value = "0" * 64 if fault == "challenge" else "4" * 32
            return replace(actual, health=actual.health.model_copy(update={fault: value}))
        if fault == "writer":
            return replace(actual, writer_free=True)
        return replace(actual, health_failed=True)

    monkeypatch.setattr(host.backend, "observe", observe)
    intermediate = await controller.step()

    assert len(observations) == 1 and host.transport.calls[http_start:] == ["readiness"]
    assert intermediate.revision == started.revision + 1 and intermediate.health is None
    assert (intermediate.phase, intermediate.intent) == (
        ("verifying", "none") if role == "target" else ("rolling_back", "verify_base")
    )
    assert not intermediate.resolved and intermediate.activation == "pending"
    assert intermediate.code == started.code and intermediate.deadline == started.deadline
    same_record = host.record() == intermediate
    same_disk = host.backend._disk() == disk
    assert same_record and same_disk and host.commands == commands
    assert host.writer is not None
    original_writer_is_held(host, host.writer)

    # The missing/rejected proof is not cached: an independent step must fetch
    # a new complete readiness observation before it can decide, still paused.
    monkeypatch.setattr(host.backend, "observe", original)
    decided = await controller.step()
    assert decided.phase == ("committed" if role == "target" else "rolled_back")
    assert decided.revision == intermediate.revision + 1 and decided.health is not None
    assert decided.activation == "pending" and not decided.resolved
    assert host.transport.calls[http_start:] == ["readiness", "readiness"]
    assert host.commands == commands


@pytest.mark.parametrize("role", ["target", "base"])
@pytest.mark.parametrize("fault", ["backwards", "expired"])
async def test_clock_change_between_durable_decisions_cannot_write_success(
    host: Host, monkeypatch: pytest.MonkeyPatch, role: Role, fault: str
) -> None:
    import muli.managed_controller as module

    started = await started_role(host, role)
    controller = ManagedTransactionController(host.control, host.backend, host.clock)
    disk, commands = host.backend._disk(), list(host.commands)
    http_start = len(host.transport.calls)
    moves: list[TransactionRecordV2] = []
    original = module.advance_managed

    def advance(
        control: JournalControl, expected: Revision, transition: TransitionV2
    ) -> TransactionRecordV2:
        moved = original(control, expected, transition)
        moves.append(moved)
        if len(moves) == 1:
            host.clock.elapsed = -1.0 if fault == "backwards" else started.deadline.seconds
        return moved

    monkeypatch.setattr(module, "advance_managed", advance)
    result = await controller.step()

    assert len(moves) == 2 and moves[0].revision == started.revision + 1
    assert moves[0].phase == ("verifying" if role == "target" else "rolling_back")
    assert result.phase == "recovery_required" and result.health is None and not result.resolved
    assert result.code == ("clock_invalid" if fault == "backwards" else "deadline_exceeded")
    assert result.revision == started.revision + 2 and result.deadline == started.deadline
    same_record = host.record() == result
    same_disk = host.backend._disk() == disk
    assert same_record and same_disk and host.commands == commands
    assert host.transport.calls[http_start:] == ["readiness"]
    assert host.writer is not None
    original_writer_is_held(host, host.writer)


async def test_forward_decision_preserves_rollback_reserve_after_first_cas(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.managed_controller as module

    started = await started_role(host, "target")
    controller = ManagedTransactionController(host.control, host.backend, host.clock)
    original = module.advance_managed
    moves: list[TransactionRecordV2] = []
    commands, http_start = list(host.commands), len(host.transport.calls)

    def advance(
        control: JournalControl, expected: Revision, transition: TransitionV2
    ) -> TransactionRecordV2:
        moved = original(control, expected, transition)
        moves.append(moved)
        if len(moves) == 1:
            host.clock.elapsed = started.deadline.seconds - started.deadline.rollback_reserve
        return moved

    monkeypatch.setattr(module, "advance_managed", advance)
    result = await controller.step()

    assert len(moves) == 2 and moves[0].phase == "verifying"
    assert result.phase == "rolling_back" and result.intent == "stop_target"
    assert result.code == "deadline_exceeded" and result.rollback_attempted
    assert result.health is None and result.deadline == started.deadline
    assert host.record() == result and host.commands == commands
    assert host.transport.calls[http_start:] == ["readiness"]


@pytest.mark.parametrize("failed_cas", [1, 2])
async def test_failed_durable_cas_never_returns_a_fabricated_decision(
    host: Host, monkeypatch: pytest.MonkeyPatch, failed_cas: int
) -> None:
    import muli.managed_controller as module

    started = await started_role(host, "target")
    controller = ManagedTransactionController(host.control, host.backend, host.clock)
    original = module.advance_managed
    attempted: list[TransitionV2] = []
    disk, commands = host.backend._disk(), list(host.commands)
    http_start = len(host.transport.calls)

    def advance(
        control: JournalControl, expected: Revision, transition: TransitionV2
    ) -> TransactionRecordV2:
        attempted.append(transition)
        if len(attempted) == failed_cas:
            raise TransactionError("stale_revision")
        return original(control, expected, transition)

    monkeypatch.setattr(module, "advance_managed", advance)
    with pytest.raises(TransactionError, match="^stale_revision$"):
        await controller.step()

    current = host.record()
    assert len(attempted) == failed_cas
    assert current.revision == started.revision + failed_cas - 1
    assert current.phase == ("activating" if failed_cas == 1 else "verifying")
    assert current.health is None and current.activation == "pending" and not current.resolved
    assert current.deadline == started.deadline and current.generation == started.generation
    same_disk = host.backend._disk() == disk
    assert same_disk and host.commands == commands
    assert host.transport.calls[http_start:] == ["readiness"]
    assert host.writer is not None
    original_writer_is_held(host, host.writer)


async def test_v1_keeps_the_original_separate_verifying_step(legacy_host: LegacyHost) -> None:
    host = legacy_host
    installing = host.target_intent("install_target")
    host.stopped()
    await host.backend.install(installing, "target", TARGET)
    started = host.move("activating", "start_target")
    await host.backend.start(started, "target")
    controller = TransactionController(host.control, host.backend, host.clock)
    commands = [list(command) for command in host.commands]
    http_start = len(host.transport.calls)

    verifying = await controller.step()

    assert verifying.phase == "verifying" and verifying.health is None
    assert verifying.revision == started.revision + 1 and not verifying.resolved
    assert verifying.activation == "pending" and host.record() == verifying
    assert host.commands == commands and host.admission == "paused"

    committed = await controller.step()

    assert committed.phase == "committed" and committed.revision == started.revision + 2
    assert committed.health is not None and committed.health.admission == "paused"
    assert committed.activation == "pending" and not committed.resolved
    assert host.commands == commands and host.record() == committed
    actions = [request.url.path.rsplit("/", 1)[-1] for request in host.transport.calls[http_start:]]
    assert actions == ["readiness", "readiness"]
