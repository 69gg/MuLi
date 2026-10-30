"""A released host reply cannot replace fresh lock and deadline evidence."""

from __future__ import annotations

import stat
import sys

import httpx
import pytest
from test_linux_controller import reply
from test_managed_activation_receipt import decide, original_writer_is_held
from test_managed_authentication_boundaries import bounded_slots as bounded_slots
from test_managed_backend import Host
from test_managed_backend import host as host

from muli.managed_controller import ManagedServiceObservation
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed activation/leases")


async def test_invalid_writer_lock_is_not_accepted_as_a_busy_writer(host: Host) -> None:
    controller, decided = await decide(host)
    writer = host.writer
    assert writer is not None
    original_writer_is_held(host, writer)
    lock_path = writer.directory.path / writer.name
    mode = stat.S_IMODE(lock_path.stat().st_mode)
    disk, commands = host.backend._disk(), list(host.commands)
    transport_start = len(host.transport.calls)
    invalid: list[str] = []

    def activate(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        assert action in {"readiness", "activate"}
        if action == "activate":
            assert not invalid
            host.admission = "released"
            lock_path.chmod(mode | stat.S_IRGRP)
            # The real lock rejects metadata before attempting the busy flock.
            with pytest.raises(TransactionError, match="^invalid_lock$"):
                with host.journal.writer_lease():
                    pytest.fail("an invalid lock file cannot grant a lease")
            invalid.append("invalid_lock")
        return reply(host.proof().model_dump(mode="json"))

    host.transport.handler = activate
    try:
        result = await controller.step()
    finally:
        lock_path.chmod(mode)

    assert invalid == ["invalid_lock"] and host.admission == "released"
    assert host.transport.calls[transport_start:] == ["readiness", "activate"]
    assert result.phase == decided.phase and result.activation == "blocked"
    assert result.code == "backend_failed" and not result.resolved
    assert result.revision == decided.revision + 1 and not result.rollback_attempted
    assert result.deadline == decided.deadline and host.record() == result
    assert host.backend._disk() == disk and host.commands == commands
    original_writer_is_held(host, writer)


async def test_clock_invalid_after_a_complete_receipt_cannot_record_release(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, decided = await decide(host)
    writer = host.writer
    assert writer is not None
    disk, commands = host.backend._disk(), list(host.commands)
    transport_start = len(host.transport.calls)
    receipts: list[ManagedServiceObservation | None] = []
    original_activate = host.backend.activate

    async def observed_activate(
        record: TransactionRecordV2, role: Role
    ) -> ManagedServiceObservation | None:
        receipt = await original_activate(record, role)
        receipts.append(receipt)
        return receipt

    def activate(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        assert action in {"readiness", "activate"}
        if action == "activate":
            host.admission = "released"
            host.clock.elapsed = -1.0
        return reply(host.proof().model_dump(mode="json"))

    monkeypatch.setattr(host.backend, "activate", observed_activate)
    host.transport.handler = activate
    result = await controller.step()

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt is not None and receipt.health is not None
    assert receipt.health.admission == "released" and not receipt.writer_free
    assert host.transport.calls[transport_start:] == ["readiness", "activate"]
    assert result.phase == decided.phase and result.activation == "blocked"
    assert result.code == "clock_invalid" and not result.resolved
    assert result.revision == decided.revision + 1 and not result.rollback_attempted
    assert result.deadline == decided.deadline and host.record() == result
    assert host.backend._disk() == disk and host.commands == commands
    original_writer_is_held(host, writer)
