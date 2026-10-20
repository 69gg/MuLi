from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_management_controller import FakeService

from muli.maintenance import (
    Admission,
    BootPermit,
    Maintenance,
    MaintenanceError,
    configuration_digest,
)
from muli.management_controller import ServiceObservation, TransactionController
from muli.management_transaction import (
    ClockReading,
    Deadline,
    Phase,
    Role,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.store import Store

BASE = b'{"configuration":"base"}'
TARGET = b'{"configuration":"target"}'
NOW = ClockReading(boot_id="kernel", wall=1000.0, monotonic=10.0)


@pytest.fixture
def host(tmp_path: Path) -> Iterator[tuple[TransactionJournal, Path, BootPermit, Maintenance]]:
    if sys.platform != "linux":
        pytest.skip("Linux transaction boot")
    config = tmp_path / "config.json"
    config.write_bytes(BASE)
    database = tmp_path / "data" / "muli.sqlite3"
    store = Store(database)
    main = store.main_session()
    identity = ServiceIdentity(
        installation="a" * 64,
        service="b" * 64,
        store_instance=store.instance_id,
        main_session=main["id"],
        origin_boot="origin",
    )
    store.close()
    with TransactionJournal(tmp_path / "transactions", database) as journal:
        permit = BootPermit.acquire(
            journal, config, boot="origin", installation="a" * 64, service="b" * 64
        )
        maintenance = Maintenance(permit)
        maintenance.prepared(identity.store_instance, identity.main_session)
        with journal.locked() as control:
            control.prepare(
                request_id="request",
                action="configure",
                identity=identity,
                base=BASE,
                target=TARGET,
                deadline=Deadline(created=NOW, seconds=60.0, rollback_reserve=10.0),
                writer=permit.lease,
            )
        yield journal, config, permit, maintenance
        permit.close_after_writers()


def move(journal: TransactionJournal, *transitions: Transition) -> TransactionRecord:
    with journal.locked() as control:
        record = control.read()
        assert record is not None
        for transition in transitions:
            record = control.advance(record.position, transition)
        return record


def target(host: tuple[TransactionJournal, Path, BootPermit, Maintenance]) -> Maintenance:
    journal, path, origin, _ = host
    move(
        journal,
        Transition(phase="draining"),
        Transition(phase="activating", intent="stop_base"),
        Transition(phase="activating", intent="install_target"),
        Transition(phase="activating", intent="start_target"),
        Transition(phase="verifying"),
    )
    origin.close_after_writers()
    path.write_bytes(TARGET)
    permit = BootPermit.acquire(
        journal, path, boot="target", installation="a" * 64, service="b" * 64
    )
    maintenance = Maintenance(permit)
    record = journal.read()
    assert record is not None
    maintenance.prepared(record.identity.store_instance, record.identity.main_session)
    maintenance.owner("fake", lambda: True)
    maintenance.seal({"fake"})
    return maintenance


async def test_admission_retains_real_owner_until_finally_and_release_wakes_once() -> None:
    admission = Admission()
    entered, finish, woke = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> None:
        with admission.operation():
            with admission.operation():
                entered.set()
                await finish.wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    admission.pause()
    assert not admission.idle
    with pytest.raises(MaintenanceError, match="execution_paused"):
        with admission.operation():
            pytest.fail("closed gate")

    async def waiter() -> None:
        await admission.wait()
        woke.set()

    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert not woke.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.idle
    assert admission.release()
    assert not admission.release()
    await waiting
    assert woke.is_set()


@pytest.mark.parametrize("role", ["target", "base"])
async def test_fresh_host_rebinds_blocked_decision_before_owned_activation(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance], role: Role
) -> None:
    journal, path, _, _ = host
    previous = target(host)
    if role == "base":
        assert previous.permit is not None
        previous.permit.close_after_writers()
        move(
            journal,
            Transition(phase="rolling_back", intent="stop_target", code="health_failed"),
            Transition(phase="rolling_back", intent="restore_base"),
            Transition(phase="rolling_back", intent="start_base"),
            Transition(phase="rolling_back", intent="verify_base"),
        )
        path.write_bytes(BASE)
        old = BootPermit.acquire(
            journal, path, boot="base-old", installation="a" * 64, service="b" * 64
        )
        previous = Maintenance(old)
        record = journal.read()
        assert record is not None
        previous.prepared(record.identity.store_instance, record.identity.main_session)
        previous.owner("fake", lambda: True)
        previous.seal({"fake"})
    old_permit = previous.permit
    assert old_permit is not None
    record = journal.read()
    assert record is not None
    nonce = record.challenge if role == "target" else record.rollback_challenge
    assert nonce is not None
    phase: Phase = "committed" if role == "target" else "rolled_back"
    proof = previous.readiness(record.generation, old_permit.boot, nonce)
    blocked = move(
        journal,
        Transition(phase=phase, proof=proof),
        Transition(phase=phase, activation="blocked", code="activation_failed"),
    )
    assert previous.readiness(record.generation, old_permit.boot, nonce).admission == "blocked"
    old_permit.close_after_writers()
    permit = BootPermit.acquire(
        journal, path, boot=role + "-fresh", installation="a" * 64, service="b" * 64
    )
    try:
        maintenance = Maintenance(permit)
        maintenance.prepared(record.identity.store_instance, record.identity.main_session)
        maintenance.owner("fake", lambda: True)
        maintenance.seal({"fake"})
        activated: list[str] = []

        async def components() -> None:
            activated.append(permit.boot)

        maintenance.bind_activation(components)
        with pytest.raises(MaintenanceError, match="activation_not_permitted"):
            await maintenance.activate(record.generation, permit.boot, nonce, lambda: NOW)
        assert not activated and not maintenance.admission.allowed
        paused = maintenance.readiness(record.generation, permit.boot, nonce)
        assert paused.admission == "paused"
        assert journal.read() == blocked

        class HostService(FakeService):
            async def observe(self, current: TransactionRecord) -> ServiceObservation:
                observed = await super().observe(current)
                actual = maintenance.readiness(current.generation, permit.boot, nonce)
                return replace(
                    observed, health=actual, activation_failed=actual.admission == "blocked"
                )

            async def activate(self, current: TransactionRecord, role: Role) -> None:
                self.calls.append("activate:" + role)
                await maintenance.activate(current.generation, permit.boot, nonce, lambda: NOW)

        service = HostService()
        service.boot, service.role, service.configuration = permit.boot, role, path.read_bytes()
        with journal.locked() as control:
            driver = TransactionController(control, service, lambda: NOW)
            rebound = await driver.step()
            assert rebound.activation == "pending" and rebound.health == paused
            assert not activated and not maintenance.admission.allowed
            await driver.step()
            assert activated == [permit.boot] and maintenance.admission.allowed
            final = await driver.step()
            assert final.phase == phase and final.resolved
            assert service.calls == ["activate:" + role]
    finally:
        permit.close_after_writers()


def test_drain_is_unavailable_until_all_owners_are_bound_and_idle(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    journal, _, permit, maintenance = host
    record = move(journal, Transition(phase="draining"))
    idle = False
    maintenance.owner("fake", lambda: idle)
    assert maintenance.drain(record.generation, permit.boot, record.challenge)["state"] == "waiting"
    maintenance.seal({"fake"})
    assert maintenance.drain(record.generation, permit.boot, record.challenge)["state"] == "waiting"
    assert maintenance.admission.allowed  # Waiting does not stop parent-to-child work.
    idle = True
    assert maintenance.drain(record.generation, permit.boot, record.challenge)["state"] == "drained"
    assert not maintenance.admission.allowed
    wakes: list[str] = []
    maintenance.on_release(lambda: wakes.append("wake"))
    move(journal, Transition(phase="prepared"))
    maintenance.resume(record.generation, permit.boot, record.challenge)
    maintenance.resume(record.generation, permit.boot, record.challenge)
    assert wakes == ["wake"]
    with pytest.raises(MaintenanceError, match="transaction_in_progress"):
        maintenance.require_configuration()


def test_origin_readiness_reobserves_drain_and_resume_without_cached_command_receipts(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    journal, _, permit, maintenance = host
    maintenance.owner("fake", lambda: True)
    maintenance.seal({"fake"})
    record = move(journal, Transition(phase="draining"))
    maintenance.drain(record.generation, permit.boot, record.challenge)
    proof = maintenance.readiness(record.generation, permit.boot, record.challenge)
    assert proof.admission == "paused"
    assert proof.boot == record.identity.origin_boot
    assert proof.configuration == record.base.digest and proof.challenge == record.challenge
    assert not record.accepts(proof, "target") and not record.accepts(proof, "base")
    move(journal, Transition(phase="prepared"))
    maintenance.resume(record.generation, permit.boot, record.challenge)
    assert (
        maintenance.readiness(record.generation, permit.boot, record.challenge).admission
        == "released"
    )
    move(journal, Transition(phase="draining"), Transition(phase="activating", intent="stop_base"))
    assert (
        maintenance.readiness(record.generation, permit.boot, record.challenge).boot == permit.boot
    )
    move(journal, Transition(phase="activating", intent="install_target"))
    with pytest.raises(MaintenanceError, match="foreign_state"):
        maintenance.readiness(record.generation, permit.boot, record.challenge)
    move(journal, Transition(phase="rolling_back", intent="stop_target"))
    assert (
        maintenance.readiness(record.generation, permit.boot, record.challenge).boot == permit.boot
    )
    move(journal, Transition(phase="rolling_back", intent="restore_base"))
    with pytest.raises(MaintenanceError, match="foreign_state"):
        maintenance.readiness(record.generation, permit.boot, record.challenge)


async def test_origin_readiness_rechecks_real_owners_and_effects_before_paused_proof(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    journal, _, permit, maintenance = host
    idle = True
    maintenance.owner("fake", lambda: idle)
    maintenance.seal({"fake"})
    record = move(journal, Transition(phase="draining"))
    maintenance.drain(record.generation, permit.boot, record.challenge)
    idle = False
    with pytest.raises(MaintenanceError, match="maintenance_incomplete"):
        maintenance.readiness(record.generation, permit.boot, record.challenge)
    idle = True
    maintenance.admission.release()
    with maintenance.admission.operation():
        maintenance.admission.pause()
        with pytest.raises(MaintenanceError, match="maintenance_incomplete"):
            maintenance.readiness(record.generation, permit.boot, record.challenge)
    assert (
        maintenance.readiness(record.generation, permit.boot, record.challenge).admission
        == "paused"
    )


@pytest.mark.parametrize("field", ["nonce", "boot", "generation"])
def test_nonce_and_current_identity_are_required(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
    field: str,
) -> None:
    journal, _, permit, maintenance = host
    record = move(journal, Transition(phase="draining"))
    values = {"generation": record.generation, "boot": permit.boot, "nonce": record.challenge}
    values[field] = "wrong"
    with pytest.raises(MaintenanceError, match="transaction_unauthorized"):
        maintenance.drain(**values)
    assert maintenance.admission.allowed


def test_pending_boot_cannot_reenter_before_start_intent(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    journal, path, permit, _ = host
    with pytest.raises(TransactionError, match="lock_busy"):
        BootPermit.acquire(journal, path, boot="another", installation="a" * 64, service="b" * 64)
    permit.close_after_writers()
    with pytest.raises(MaintenanceError, match="boot_not_permitted"):
        BootPermit.acquire(journal, path, boot="another", installation="a" * 64, service="b" * 64)


@pytest.mark.parametrize("wrong", ["configuration", "store", "installation"])
def test_passive_boot_rejects_foreign_facts_before_store_constructor(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
    wrong: str,
) -> None:
    maintenance = target(host)
    permit = maintenance.permit
    assert permit is not None
    permit.close_after_writers()
    if wrong == "configuration":
        host[1].write_bytes(BASE)
    if wrong == "store":
        with sqlite3.connect(host[0].database) as db:
            db.execute("UPDATE runtime_state SET value='foreign' WHERE name='store_instance_v1'")
    with pytest.raises(MaintenanceError):
        BootPermit.acquire(
            host[0],
            host[1],
            boot="next",
            installation=("f" if wrong == "installation" else "a") * 64,
            service="b" * 64,
        )


async def test_activation_requires_durable_decision_and_survives_observer_cancellation(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    maintenance = target(host)
    journal = host[0]
    record = journal.read()
    assert record is not None
    calls: list[str] = []
    entered, finish = asyncio.Event(), asyncio.Event()

    async def activate() -> None:
        calls.append("activate")
        entered.set()
        await finish.wait()

    maintenance.bind_activation(activate)
    arguments = (record.generation, "target", record.challenge, lambda: NOW)
    with pytest.raises(MaintenanceError, match="activation_not_permitted"):
        await maintenance.activate(*arguments)
    proof = maintenance.readiness(*arguments[:3])
    assert proof.admission == "paused"
    move(journal, Transition(phase="committed", proof=proof))
    observed = asyncio.create_task(maintenance.activate(*arguments))
    await entered.wait()
    observed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observed
    assert not maintenance.admission.allowed
    finish.set()
    await maintenance.settle_activation()
    assert (await maintenance.activate(*arguments)).admission == "released"
    assert calls == ["activate"]
    assert maintenance.permit is not None
    maintenance.permit.close_after_writers()


async def test_activation_failure_stays_paused_without_repeating_effects(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    maintenance = target(host)
    record = host[0].read()
    assert record is not None
    args = (record.generation, "target", record.challenge)
    move(host[0], Transition(phase="committed", proof=maintenance.readiness(*args)))
    calls: list[str] = []

    async def fail() -> None:
        calls.append("fail")
        raise RuntimeError("component failed")

    maintenance.bind_activation(fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="component failed"):
            await maintenance.activate(*args, lambda: NOW)
    assert calls == ["fail"]
    assert maintenance.readiness(*args).admission == "blocked"
    assert maintenance.permit is not None
    maintenance.permit.close_after_writers()


def test_configuration_reader_rejects_fifo_and_symlink_without_waiting(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux nofollow/nonblock")
    path = tmp_path / "config.json"
    path.write_bytes(BASE)
    assert configuration_digest(path, 1024) == hashlib.sha256(BASE).hexdigest()
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(MaintenanceError, match="configuration_not_regular"):
        configuration_digest(path, 1024)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        configuration_digest(path, 1024)


async def test_release_callback_failure_blocks_proof_without_repeating_activation(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    maintenance = target(host)
    record = host[0].read()
    assert record is not None
    args = record.generation, "target", record.challenge
    move(host[0], Transition(phase="committed", proof=maintenance.readiness(*args)))
    calls: list[str] = []

    async def activate() -> None:
        calls.append("activate")

    def failing_wake() -> None:
        calls.append("wake")
        raise RuntimeError("wake failed")

    maintenance.bind_activation(activate)
    maintenance.on_release(failing_wake)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="wake failed"):
                await maintenance.activate(*args, lambda: NOW)
        assert calls == ["activate", "wake"]
        assert not maintenance.admission.allowed
        assert maintenance.readiness(*args).admission == "blocked"
    finally:
        assert maintenance.permit is not None
        maintenance.permit.close_after_writers()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("failure", ["blocked", "deadline"])
async def test_activation_rechecks_decision_before_external_start_and_before_release(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
    monkeypatch: pytest.MonkeyPatch,
    when: str,
    failure: str,
) -> None:
    maintenance = target(host)
    journal = host[0]
    record = journal.read()
    assert record is not None
    args = record.generation, "target", record.challenge
    move(journal, Transition(phase="committed", proof=maintenance.readiness(*args)))
    reading = NOW
    calls: list[str] = []

    def invalidate() -> None:
        nonlocal reading
        if failure == "blocked":
            move(
                journal,
                Transition(phase="committed", activation="blocked", code="activation_failed"),
            )
        else:
            reading = ClockReading(boot_id="kernel", wall=1061.0, monotonic=71.0)

    async def activate() -> None:
        calls.append("activate")
        if when == "after":
            invalidate()

    maintenance.bind_activation(activate)
    create_task = asyncio.create_task

    def gated_task(coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = create_task(coro, **kwargs)
        if kwargs.get("name") == "muli-maintenance-activation" and when == "before":
            invalidate()
        return task

    monkeypatch.setattr(asyncio, "create_task", gated_task)
    try:
        with pytest.raises(MaintenanceError, match="activation_not_permitted|deadline_exceeded"):
            await maintenance.activate(*args, lambda: reading)
        assert calls == ([] if when == "before" else ["activate"])
        assert not maintenance.admission.allowed
        assert maintenance.readiness(*args).admission == "blocked"
    finally:
        assert maintenance.permit is not None
        maintenance.permit.close_after_writers()


async def test_activation_requires_successful_durability_barrier(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maintenance = target(host)
    journal = host[0]
    record = journal.read()
    assert record is not None
    args = record.generation, "target", record.challenge
    move(journal, Transition(phase="committed", proof=maintenance.readiness(*args)))
    calls: list[str] = []

    async def activate() -> None:
        calls.append("external")

    def fail_sync(descriptor: int) -> None:
        raise OSError("sync failed")

    maintenance.bind_activation(activate)
    try:
        monkeypatch.setattr(os, "fsync", fail_sync)
        with pytest.raises(OSError, match="sync failed"):
            await maintenance.activate(*args, lambda: NOW)
        assert not calls and not maintenance.admission.allowed
    finally:
        assert maintenance.permit is not None
        maintenance.permit.close_after_writers()


async def test_resolved_journal_reconciles_lost_notification_at_existing_entry(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    journal, _, permit, maintenance = host
    wakes: list[str] = []
    maintenance.on_configuration_release(lambda: wakes.append("configuration"))
    maintenance.reconcile()
    waiting = asyncio.create_task(maintenance.wait_configuration())
    await asyncio.sleep(0)
    assert not waiting.done()
    # Simulate controller persistence followed by a crash before its final GET.
    record = move(journal, Transition(phase="aborted", code="deadline_exceeded"))
    assert record.resolved and not wakes
    maintenance.require_configuration()
    await waiting
    maintenance.require_configuration()
    assert wakes == ["configuration"] and permit.lease


async def test_rolled_back_base_uses_independent_challenge_and_release_receipt(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    journal, path, origin, _ = host
    record = move(
        journal,
        Transition(phase="draining"),
        Transition(phase="activating", intent="stop_base"),
        Transition(phase="rolling_back", intent="stop_target", code="health_failed"),
        Transition(phase="rolling_back", intent="restore_base"),
        Transition(phase="rolling_back", intent="start_base"),
        Transition(phase="rolling_back", intent="verify_base"),
    )
    origin.close_after_writers()
    permit = BootPermit.acquire(journal, path, boot="base", installation="a" * 64, service="b" * 64)
    maintenance = Maintenance(permit)
    maintenance.prepared(record.identity.store_instance, record.identity.main_session)
    maintenance.seal(set())
    calls: list[str] = []

    async def activate() -> None:
        calls.append("base")

    maintenance.bind_activation(activate)
    assert record.rollback_challenge is not None
    args = record.generation, "base", record.rollback_challenge
    try:
        with pytest.raises(MaintenanceError, match="transaction_unauthorized"):
            maintenance.readiness(record.generation, "base", record.challenge)
        proof = maintenance.readiness(*args)
        move(journal, Transition(phase="rolled_back", proof=proof))
        assert (await maintenance.activate(*args, lambda: NOW)).admission == "released"
        assert not journal.read().resolved  # type: ignore[union-attr]
        move(
            journal,
            Transition(
                phase="rolled_back", activation="released", proof=maintenance.readiness(*args)
            ),
        )
        assert journal.read().resolved  # type: ignore[union-attr]
        assert calls == ["base"]
    finally:
        permit.close_after_writers()


def test_unsupported_owner_keeps_original_service_admitted(
    host: tuple[TransactionJournal, Path, BootPermit, Maintenance],
) -> None:
    journal, _, permit, maintenance = host
    record = move(journal, Transition(phase="draining"))
    maintenance.seal(set())
    maintenance.unsupported("legacy_plugins", lambda: True)
    with pytest.raises(MaintenanceError, match="maintenance_unsupported"):
        maintenance.drain(record.generation, permit.boot, record.challenge)
    assert maintenance.admission.allowed
