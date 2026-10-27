"""A validated activation reply may finish its current durable decision."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Literal

import httpx
import pytest
from test_linux_controller import reply
from test_managed_authentication_boundaries import bounded_slots as bounded_slots
from test_managed_backend import Host
from test_managed_backend import host as host
from test_managed_observation_authentication import (
    observe_complete_package_scans,
    observe_probes,
)

from muli.managed_controller import ManagedServiceObservation, ManagedTransactionController
from muli.managed_transaction import TransactionRecordV2
from muli.management_helper import drive_loop
from muli.management_transaction import FixedLock, Phase, Revision, Role, TransactionError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed activation/leases")


async def reach(controller: ManagedTransactionController, phase: Phase) -> TransactionRecordV2:
    original = controller.backend.observe
    missing = False

    async def observe(record: TransactionRecordV2) -> ManagedServiceObservation:
        nonlocal missing
        observation = await original(record)
        if not missing and (record.phase, record.intent) == ("activating", "start_target"):
            missing = True
            return replace(observation, health=None)
        return observation

    with pytest.MonkeyPatch.context() as patch:
        if phase == "verifying":
            # Only this fixture requests an observable intermediate decision.
            # Keep all real observation gates, then withhold its proof once.
            patch.setattr(controller.backend, "observe", observe)
        for _ in range(16):
            record = await controller.step()
            if record.phase == phase:
                if phase == "verifying":
                    assert missing and record.health is None
                return record
            assert not record.resolved and record.activation != "blocked"
    raise AssertionError("the real controller did not reach the fixture decision")


async def decide(
    host: Host, role: Role = "target"
) -> tuple[ManagedTransactionController, TransactionRecordV2]:
    """Reach a pending decision through real steps, file effects and journal CAS."""
    controller = ManagedTransactionController(
        host.control, host.backend, host.clock, operation_timeout=10.0
    )
    verifying = await reach(controller, "verifying")
    assert verifying.health is None
    if role == "base":
        # A real rejected target proof drives the ordinary one-time rollback.
        host.admission = "blocked"
        rollback = await controller.step()
        assert rollback.phase == "rolling_back" and rollback.intent == "stop_target"
        record = await reach(controller, "rolled_back")
        assert record.rollback_attempted
    else:
        record = await controller.step()
        assert record.phase == "committed" and not record.rollback_attempted
    assert record.activation == "pending" and not record.resolved
    assert record.health is not None and record.health.admission == "paused"
    assert host.admission == "paused" and "activate" not in host.transport.calls
    return controller, record


def original_writer_is_held(host: Host, writer: FixedLock) -> None:
    assert host.writer is writer
    writer.check()
    with pytest.raises(TransactionError, match="^lock_busy$"):
        with host.journal.writer_lease():
            pass
    writer.check()


@pytest.mark.parametrize("role", ["target", "base"])
async def test_valid_activation_reply_releases_the_same_controller_step(
    host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    controller, decided = await decide(host, role)
    writer = host.writer
    assert writer is not None
    original_writer_is_held(host, writer)
    disk, service = host.backend._disk(), host.probe.snapshot
    commands, transport_start = list(host.commands), len(host.transport.calls)
    scans = observe_complete_package_scans(host, monkeypatch)
    probes = observe_probes(host, monkeypatch)
    reply_scans: dict[Path, int] = {}
    reply_probe_counts: list[int] = []

    def activate(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        assert action in {"readiness", "activate"}
        if action == "activate":
            assert not reply_probe_counts
            original_writer_is_held(host, writer)
            reply_probe_counts.append(len(probes))
            reply_scans.update(scans)
            host.admission = "released"
        return reply(host.proof().model_dump(mode="json"))

    host.transport.handler = activate
    result = await controller.step()

    # These gates precede the expected old-code pending/released failure.
    assert host.transport.calls[transport_start:] == ["readiness", "activate"]
    assert len(reply_probe_counts) == 1 and len(probes) >= 4
    assert len(probes) > reply_probe_counts[0]
    assert all(item.service == service and role in item.process_roles for item in probes)
    selected_roots = {
        Path(host.backend.controller_slot.package.root),
        Path(host.slots[role].package.root),
    }
    for root in selected_roots:
        assert scans[root] > reply_scans[root] > 0
    assert host.backend._disk() == disk and host.commands == commands
    original_writer_is_held(host, writer)
    assert host.record() == result
    assert result.generation == decided.generation and result.deadline == decided.deadline

    assert result.activation == "released"
    assert result.resolved and result.phase == decided.phase
    assert result.revision == decided.revision + 1
    assert result.health is not None and result.health.admission == "released"
    assert result.health.boot == service.unit.invocation
    assert result.rollback_attempted == decided.rollback_attempted


@pytest.mark.parametrize("response", ["lost", "paused"])
async def test_unknown_or_paused_activation_reply_is_observed_without_resending(
    host: Host, response: Literal["lost", "paused"]
) -> None:
    controller, decided = await decide(host)
    writer = host.writer
    assert writer is not None
    transport_start = len(host.transport.calls)

    def activate(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        assert action in {"readiness", "activate"}
        if action == "activate" and response == "lost":
            host.admission = "released"
            raise httpx.ReadTimeout("the host accepted activation but its reply was lost")
        return reply(host.proof().model_dump(mode="json"))

    host.transport.handler = activate
    first = await controller.step()
    assert first == decided and not first.resolved
    second = await controller.step()
    if response == "paused":
        assert second == decided and not second.resolved
        host.admission = "released"
        final = await controller.step()
    else:
        final = second
    assert final.resolved and final.activation == "released"
    assert host.record() == final and final.deadline == decided.deadline
    assert host.transport.calls[transport_start:].count("activate") == 1
    original_writer_is_held(host, writer)


async def test_explicit_blocked_activation_reply_keeps_the_durable_decision_blocked(
    host: Host,
) -> None:
    controller, decided = await decide(host)
    writer = host.writer
    assert writer is not None
    transport_start = len(host.transport.calls)

    def activate(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        assert action in {"readiness", "activate"}
        if action == "activate":
            host.admission = "blocked"
        return reply(host.proof().model_dump(mode="json"))

    host.transport.handler = activate
    blocked = await controller.step()
    assert blocked.phase == decided.phase and blocked.activation == "blocked"
    assert not blocked.resolved and not blocked.rollback_attempted
    assert blocked.deadline == decided.deadline and host.record() == blocked
    assert await controller.step() == blocked
    assert host.transport.calls[transport_start:].count("activate") == 1
    original_writer_is_held(host, writer)


@pytest.mark.parametrize("fault", ["writer", "boot", "deadline"])
async def test_activation_reply_cannot_release_after_its_live_evidence_changes(
    host: Host, fault: Literal["writer", "boot", "deadline"]
) -> None:
    controller, decided = await decide(host)
    transport_start = len(host.transport.calls)
    changed: list[str] = []

    def activate(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        assert action in {"readiness", "activate"}
        if action != "activate":
            return reply(host.proof().model_dump(mode="json"))
        assert not changed
        host.admission = "released"
        payload = host.proof().model_dump(mode="json")
        if fault == "writer":
            host.release_writer()
        elif fault == "boot":
            before = host.probe.snapshot
            host.probe.snapshot = replace(before, unit=replace(before.unit, invocation="4" * 32))
        else:
            host.clock.elapsed = decided.deadline.seconds
        changed.append(fault)
        return reply(payload)

    host.transport.handler = activate
    result = await controller.step()
    assert changed == [fault]
    assert not result.resolved and result.activation not in {"released", "released_degraded"}
    assert result.phase == decided.phase and not result.rollback_attempted
    assert result.deadline == decided.deadline and host.record() == result
    assert host.transport.calls[transport_start:] == ["readiness", "activate"]
    if fault == "writer":
        assert host.writer is None
        with host.journal.writer_lease() as available:
            available.check()
    else:
        assert host.writer is not None
        original_writer_is_held(host, host.writer)


async def test_cancelled_activation_observer_keeps_the_decision_for_fresh_observation(
    host: Host,
) -> None:
    controller, decided = await decide(host)
    writer = host.writer
    assert writer is not None
    transport_start = len(host.transport.calls)

    def activate(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        assert action in {"readiness", "activate"}
        if action == "activate":
            host.admission = "released"
            raise asyncio.CancelledError
        return reply(host.proof().model_dump(mode="json"))

    host.transport.handler = activate
    with pytest.raises(asyncio.CancelledError):
        await controller.step()
    assert host.record() == decided and not decided.resolved
    original_writer_is_held(host, writer)

    final = await controller.step()
    assert final.resolved and final.activation == "released"
    assert final.deadline == decided.deadline and host.record() == final
    assert host.transport.calls[transport_start:].count("activate") == 1


async def test_helper_loop_still_observes_final_after_a_released_activation_reply(
    host: Host,
) -> None:
    controller, decided = await decide(host)
    writer = host.writer
    assert writer is not None
    finalized: list[Revision] = []

    async def verify() -> None:
        host.backend._check(host.record())

    async def observe_final(record: TransactionRecordV2) -> ManagedServiceObservation:
        assert record.resolved and host.record() == record
        observed = await host.backend.observe(record)
        assert observed.service == "running" and not observed.writer_free
        assert observed.health is not None and observed.health.admission == "released"
        finalized.append(record.position)
        return observed

    async def wait(seconds: float) -> None:
        assert 0 < seconds <= host.anchor.helper.poll_interval

    result = await drive_loop(
        controller.step,
        host.record,
        observe_final,
        host.anchor.helper,
        host.clock,
        verify,
        wait=wait,
    )
    assert result.resolved and result.deadline == decided.deadline
    assert finalized == [result.position]
    assert host.transport.calls.count("activate") == 1
    original_writer_is_held(host, writer)
