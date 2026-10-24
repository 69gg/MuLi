"""Real managed installation evidence must not replace the original start gates."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_managed_backend import Host
from test_managed_backend import host as host

from muli.managed_controller import (
    BindingConfirmation,
    ManagedServiceObservation,
    ManagedTransactionController,
)
from muli.managed_journal import RoleSnapshots
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Revision, Role, TransactionError
from muli.platforms.linux_transaction import ObservationError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed effects/leases")


def expected_confirmation(record: TransactionRecordV2) -> BindingConfirmation:
    target = record.target
    return BindingConfirmation(
        position=record.position,
        role="target",
        configuration=target.configuration.digest,
        selector=None if target.selector is None else target.selector.digest,
        unit=target.unit.digest,
        loaded_unit=target.unit.digest,
    )


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
async def test_real_install_returns_exact_confirmation_and_original_start_rechecks_all_gates(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.install_intent()
    confirmation = await host.backend.install(record, "target", host.data["target"])

    # This is the first target red: the existing completed install returns None.
    assert isinstance(confirmation, BindingConfirmation)
    assert confirmation == expected_confirmation(record)
    assert host.record() == record and host.commands == []
    assert host.backend._disk().configuration.data == host.data["target"].configuration
    assert host.probe.loaded_unit == host.data["target"].unit

    # A returned installation attestation is not authority to omit any start
    # check. Use the real journal CAS and the unchanged real start implementation.
    started = host.move("activating", "start_target")
    checks: list[tuple[TransactionRecordV2, Role | None]] = []
    original_check = host.backend._check

    def checked(current: TransactionRecordV2, role: Role | None = None) -> None:
        checks.append((current, role))
        original_check(current, role)

    monkeypatch.setattr(host.backend, "_check", checked)
    probes = host.probe.count
    await host.backend.start(started, "target")

    assert len(checks) == 15
    assert checks.count((started, None)) == 4
    assert checks.count((started, "target")) == 11
    assert host.probe.count - probes == 2
    assert host.commands == ["start"]
    assert host.record() == started
    assert host.probe.snapshot.process is not None
    assert host.probe.snapshot.process.argv == host.arguments("target")


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
async def test_real_managed_step_uses_install_confirmation_without_a_second_observation(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.install_intent()
    observed: list[Revision] = []
    confirmations: list[BindingConfirmation | None] = []
    start_checks: list[int] = []
    authenticated: list[Revision] = []
    checks = 0
    original_observe = host.backend.observe
    original_install = host.backend.install
    original_start = host.backend.start
    original_check = host.backend._check

    def checked(current: TransactionRecordV2, role: Role | None = None) -> None:
        nonlocal checks
        checks += 1
        original_check(current, role)

    async def observe(current: TransactionRecordV2) -> ManagedServiceObservation:
        observed.append(current.position)
        return await original_observe(current)

    async def install(
        current: TransactionRecordV2, role: Role, data: RoleSnapshots
    ) -> BindingConfirmation | None:
        confirmation = await original_install(current, role, data)
        confirmations.append(confirmation)
        return confirmation

    async def start(current: TransactionRecordV2, role: Role) -> None:
        before = checks
        assert authenticated == [record.position]
        assert host.record() == current
        assert current.intent == "start_target"
        assert current.revision == record.revision + 1
        await original_start(current, role)
        start_checks.append(checks - before)

    async def verify_continuation() -> None:
        # This injected callback exercises the real driver gate, not kernel
        # helper authentication; managed_host supplies its actual verify_helper.
        assert confirmations == [expected_confirmation(record)]
        original_check(record)
        authenticated.append(record.position)

    monkeypatch.setattr(host.backend, "_check", checked)
    monkeypatch.setattr(host.backend, "observe", observe)
    monkeypatch.setattr(host.backend, "install", install)
    monkeypatch.setattr(host.backend, "start", start)
    driver = ManagedTransactionController(
        host.control, host.backend, host.clock, verify_continuation=verify_continuation
    )

    current = await driver.step()

    assert len(confirmations) == 1
    assert isinstance(confirmations[0], BindingConfirmation)
    assert confirmations[0] == expected_confirmation(record)
    assert current == host.record()
    assert current.position != record.position
    assert (current.phase, current.intent) == ("activating", "start_target")
    assert observed == [record.position]
    assert start_checks == [15]
    assert host.commands == ["start"]


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
@pytest.mark.parametrize("fault", ["helper", "cancel", "cas", "slot", "unit", "cgroup", "deadline"])
async def test_install_continuation_rejects_failure_or_drift_before_any_start(
    host: Host, fault: str
) -> None:
    record = host.install_intent()
    authenticated = 0

    async def verify_continuation() -> None:
        nonlocal authenticated
        authenticated += 1
        assert host.backend._disk().configuration.data == host.data["target"].configuration
        assert host.record() == record and host.commands == []
        # A real await boundary: no installation result authorizes the later
        # effect if helper authentication or actual material changes here.
        await asyncio.sleep(0)
        if fault == "helper":
            raise ObservationError("foreign_helper_process")
        if fault == "cancel":
            raise asyncio.CancelledError
        if fault == "cas":
            host.move("activating", "start_target")
        elif fault == "slot":
            Path(host.slots["base"].declaration.path).write_bytes(b"changed installation")
        elif fault == "unit":
            Path(host.anchor.unit_path).write_bytes(b"foreign unit")
        elif fault == "cgroup":
            host.events.write_bytes(b"populated 1\n")
        elif fault == "deadline":
            host.clock.elapsed = record.deadline.seconds + 1.0

    driver = ManagedTransactionController(
        host.control, host.backend, host.clock, verify_continuation=verify_continuation
    )
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await driver.step()
        assert host.record() == record
    elif fault == "cas":
        with pytest.raises(TransactionError, match="^stale_revision$"):
            await driver.step()
        assert host.record().intent == "start_target"
    else:
        current = await driver.step()
        assert current.code == ("deadline_exceeded" if fault == "deadline" else "backend_failed")
        assert current.phase == ("recovery_required" if fault == "deadline" else "rolling_back")
    assert authenticated == 1
    assert host.commands == []
    assert host.record().deadline == record.deadline


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
@pytest.mark.parametrize(
    "field", ["position", "role", "configuration", "selector", "unit", "loaded_unit"]
)
async def test_changed_install_confirmation_cannot_enter_helper_or_start(
    host: Host, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    record = host.install_intent()
    original_install = host.backend.install
    authenticated = 0

    async def install(
        current: TransactionRecordV2, role: Role, data: RoleSnapshots
    ) -> BindingConfirmation:
        confirmation = await original_install(current, role, data)
        assert isinstance(confirmation, BindingConfirmation)
        if field == "position":
            return replace(
                confirmation,
                position=current.position.model_copy(update={"revision": current.revision - 1}),
            )
        if field == "role":
            return replace(confirmation, role="base")
        if field == "configuration":
            return replace(confirmation, configuration="f" * 64)
        if field == "selector":
            return replace(confirmation, selector=None)
        if field == "unit":
            return replace(confirmation, unit="f" * 64)
        return replace(confirmation, loaded_unit="f" * 64)

    async def verify_continuation() -> None:
        nonlocal authenticated
        authenticated += 1

    monkeypatch.setattr(host.backend, "install", install)
    driver = ManagedTransactionController(
        host.control, host.backend, host.clock, verify_continuation=verify_continuation
    )
    current = await driver.step()
    assert (current.phase, current.intent, current.code) == (
        "rolling_back",
        "stop_target",
        "backend_failed",
    )
    assert authenticated == 0 and host.commands == []
    assert current.deadline == record.deadline


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
@pytest.mark.parametrize("failure", ["lost_reply", "cancel"])
async def test_lost_install_reply_reopens_without_assuming_a_start_was_sent(
    host: Host, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    record = host.install_intent()
    original_install = host.backend.install

    async def install(
        current: TransactionRecordV2, role: Role, data: RoleSnapshots
    ) -> BindingConfirmation:
        confirmation = await original_install(current, role, data)
        assert isinstance(confirmation, BindingConfirmation)
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("controlled_lost_install_reply")

    monkeypatch.setattr(host.backend, "install", install)
    driver = ManagedTransactionController(host.control, host.backend, host.clock)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await driver.step()
        assert host.record() == record
    else:
        current = await driver.step()
        assert (current.phase, current.intent, current.code) == (
            "rolling_back",
            "stop_target",
            "backend_failed",
        )
    assert host.commands == []
    monkeypatch.setattr(host.backend, "install", original_install)
    probes = host.probe.count
    reopened = ManagedTransactionController(host.control, host.backend, host.clock)
    current = await reopened.step()
    assert host.probe.count > probes
    if failure == "cancel":
        assert current.intent == "start_target" and host.commands == ["start"]
    else:
        assert current.intent == "restore_base" and host.commands == []
        current = await reopened.step()
        assert current.intent == "start_base" and current.code == "backend_failed"
        assert host.commands == ["start"]
    assert current.deadline == record.deadline


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
@pytest.mark.parametrize("stopped", [False, True])
async def test_unknown_start_reopens_without_repeating_the_command(
    host: Host, monkeypatch: pytest.MonkeyPatch, stopped: bool
) -> None:
    record = host.install_intent()
    original_start = host.backend.start

    async def start(current: TransactionRecordV2, role: Role) -> None:
        await original_start(current, role)
        raise asyncio.CancelledError

    monkeypatch.setattr(host.backend, "start", start)
    driver = ManagedTransactionController(host.control, host.backend, host.clock)
    with pytest.raises(asyncio.CancelledError):
        await driver.step()
    assert host.record().intent == "start_target" and host.commands == ["start"]
    monkeypatch.setattr(host.backend, "start", original_start)
    if stopped:
        host.stopped()
    reopened = ManagedTransactionController(host.control, host.backend, host.clock)
    current = await reopened.step()
    assert host.commands == ["start"]
    if stopped:
        assert (current.phase, current.code) == ("rolling_back", "ambiguous_start")
    else:
        assert current.phase == "verifying"
    assert current.deadline == record.deadline
