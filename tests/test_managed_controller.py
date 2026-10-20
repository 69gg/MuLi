from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest
from test_managed_transaction import CONFIG, UNIT, binding, deadline, identity, manifest, proof

from muli.format_admission import CURRENT_FORMAT_STATE
from muli.managed_controller import (
    BindingConfirmation,
    ManagedProcessIdentity,
    ManagedServiceObservation,
    ManagedTransactionController,
)
from muli.managed_journal import RoleSnapshots, advance_managed, prepare_managed, read_snapshots
from muli.managed_transaction import ManagedAction, TransactionRecordV2, TransitionV2, raw_snapshot
from muli.management_transaction import (
    ClockReading,
    JournalControl,
    Role,
    TransactionJournal,
    _snapshot,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal primitive")


@dataclass
class FakeClock:
    elapsed: float = 0.0

    def __call__(self) -> ClockReading:
        return ClockReading(
            boot_id="kernel", wall=1100.0 + self.elapsed, monotonic=100.0 + self.elapsed
        )


class FakeManagedService:
    """Only in-memory file/process evidence; no service, HTTP or subprocess."""

    def __init__(self, record: TransactionRecordV2, data: RoleSnapshots) -> None:
        self.configuration, self.selector, self.unit = data.configuration, data.selector, data.unit
        selected = record.base
        self.process: ManagedProcessIdentity | None = ManagedProcessIdentity(
            selected.entry, selected.installation, selected.compatibility
        )
        self.service: Literal["running", "stopped", "starting", "stopping", "foreign"] = "running"
        self.boot: str | None = record.identity.origin_boot
        self.role: Role = "base"
        self.writer_free = False
        self.drained = False
        self.resumed = True
        self.released = False
        self.healthy_target = True
        self.healthy_base = True
        self.confirmation: BindingConfirmation | None = None
        self.interrupt = ""
        self.fail = ""
        self.calls: list[str] = []

    def checkpoint(self, point: str) -> None:
        if self.fail == point:
            self.fail = ""
            raise RuntimeError("private-fixture-error")
        if self.interrupt == point:
            self.interrupt = ""
            raise asyncio.CancelledError

    def files(self) -> RoleSnapshots:
        return RoleSnapshots(self.configuration, self.selector, self.unit)

    def confirm(self, record: TransactionRecordV2, role: Role) -> None:
        assert self.service == "stopped" and self.writer_free and self.process is None
        selected = record.binding(role)
        self.files().verify(selected, 4096)
        self.calls.extend(
            f"confirm:{component}:{role}" for component in ("config", "selector", "unit")
        )
        self.calls.append("loaded:" + role)
        self.confirmation = BindingConfirmation(
            position=record.position,
            role=role,
            configuration=selected.configuration.digest,
            selector=selected.selector.digest if selected.selector else None,
            unit=selected.unit.digest,
            loaded_unit=selected.unit.digest,
        )

    async def observe(self, record: TransactionRecordV2) -> ManagedServiceObservation:
        healthy = self.healthy_target if self.role == "target" else self.healthy_base
        health = None
        if self.service == "running" and self.boot != record.identity.origin_boot and healthy:
            assert self.boot is not None
            health = proof(
                record,
                role=self.role,
                boot=self.boot,
                admission="released" if self.released else "paused",
            )
        return ManagedServiceObservation(
            identity_matches=True,
            configuration=_snapshot(self.configuration, 4096).digest,
            selector=_snapshot(self.selector, 4096).digest if self.selector is not None else None,
            unit=raw_snapshot(self.unit, 4096).digest,
            service=self.service,
            boot=self.boot,
            writer_free=self.writer_free,
            process=self.process,
            confirmation=self.confirmation,
            drained=self.drained,
            resumed=self.resumed,
            health=health,
            health_failed=self.boot != record.identity.origin_boot and not healthy,
        )

    async def drain(self, record: TransactionRecordV2) -> None:
        assert record.phase == "draining"
        self.calls.append("drain")
        self.drained, self.resumed = True, False

    async def resume(self, record: TransactionRecordV2) -> None:
        assert record.phase == "prepared"
        self.calls.append("resume")
        self.drained, self.resumed = False, True

    async def stop(self, record: TransactionRecordV2, role: Role) -> None:
        assert record.intent in {"stop_base", "stop_target"}
        self.calls.append("stop:" + role)
        self.service, self.boot, self.writer_free, self.process = "stopped", None, True, None
        self.confirmation = None

    async def install(self, record: TransactionRecordV2, role: Role, data: RoleSnapshots) -> None:
        assert record.intent == ("install_target" if role == "target" else "restore_base")
        assert self.service == "stopped" and self.writer_free and self.process is None
        data.verify(record.binding(role), 4096)
        self.calls.append("install:" + role)
        self.confirmation = None
        # Rollback reverses forward config -> selector -> unit installation.
        components = (
            ("configuration", "selector", "unit")
            if role == "target"
            else ("unit", "selector", "configuration")
        )
        for component in components:
            setattr(self, component, getattr(data, component))
            self.calls.append(component + ":" + role)
            self.checkpoint("after_" + component + ":" + role)
        self.confirm(record, role)

    async def start(self, record: TransactionRecordV2, role: Role) -> None:
        assert record.intent == ("start_target" if role == "target" else "start_base")
        self.checkpoint("before_start:" + role)
        # The command boundary repeats guards, not just a cached observation.
        self.confirm(record, role)
        self.calls.append("start:" + role)
        selected = record.binding(role)
        self.process = ManagedProcessIdentity(
            selected.entry, selected.installation, selected.compatibility
        )
        self.role = role
        self.service, self.boot, self.writer_free = "running", role + "-boot", False
        self.released = False
        self.checkpoint("after_start:" + role)

    async def activate(self, record: TransactionRecordV2, role: Role) -> None:
        assert record.phase == ("committed" if role == "target" else "rolled_back")
        self.calls.append("activate:" + role)
        self.released = True
        self.checkpoint("after_activate")


Scenario = tuple[TransactionJournal, JournalControl, FakeClock, FakeManagedService]


@contextmanager
def scenario(
    tmp_path: Path, *, action: ManagedAction = "switch", changed_config: bool = False
) -> Iterator[Scenario]:
    first = binding("a", legacy=action == "adopt")
    last = binding(
        "b" if action == "switch" else "a",
        configuration=b'{"credential":"other-fixture","port":8765}' if changed_config else CONFIG,
    )
    base_data = RoleSnapshots(
        CONFIG,
        None
        if first.selector is None
        else json.dumps(
            {"deployment": "d" * 64, "installation": "a" * 64},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        b"[Service]\nExecStart=/original/python -m muli\n" if action == "adopt" else UNIT,
    )
    target_data = RoleSnapshots(
        b'{"credential":"other-fixture","port":8765}' if changed_config else CONFIG,
        json.dumps(
            {"deployment": "d" * 64, "installation": ("b" if action == "switch" else "a") * 64},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        UNIT,
    )
    with TransactionJournal(tmp_path / "journal", tmp_path / "data" / "muli.db") as journal:
        with journal.writer_lease() as writer, journal.locked() as control:
            record = prepare_managed(
                control,
                request_id="managed-plan",
                action=action,
                identity=identity(),
                base=first,
                target=last,
                base_data=base_data,
                target_data=target_data,
                observed_format=CURRENT_FORMAT_STATE,
                base_manifest=manifest(),
                target_manifest=manifest(),
                deadline=deadline(),
                writer=writer,
            )
        with journal.locked() as owner:
            yield journal, owner, FakeClock(), FakeManagedService(record, base_data)


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Scenario]:
    with scenario(tmp_path) as value:
        yield value


async def reach(
    controller: ManagedTransactionController, phase: str, intent: str | None = None
) -> TransactionRecordV2:
    for _ in range(28):
        record = await controller.step()
        if record.phase == phase and (intent is None or record.intent == intent):
            return record
        assert not record.resolved and record.phase != "recovery_required"
    pytest.fail("expected managed phase not reached in 28 steps")


async def finish(controller: ManagedTransactionController) -> TransactionRecordV2:
    for _ in range(28):
        record = await controller.step()
        if record.resolved or record.phase == "recovery_required" or record.activation == "blocked":
            return record
    pytest.fail("managed controller failed to converge in 28 steps")


async def test_identical_configuration_still_switches_selector_actual_slot_and_confirms(
    setup: Scenario,
) -> None:
    journal, control, clock, service = setup
    record = control.read_current()
    assert isinstance(record, TransactionRecordV2)
    assert record.base.configuration == record.target.configuration
    final = await finish(ManagedTransactionController(control, service, clock))
    assert final.resolved and final.phase == "committed"
    assert service.files() == read_snapshots(journal, final, "target")
    assert service.process is not None and service.process.installation == final.target.installation
    assert service.calls.count("start:target") == service.calls.count("activate:target") == 1
    assert (
        service.calls.index("configuration:target")
        < service.calls.index("selector:target")
        < service.calls.index("unit:target")
    )
    assert service.calls.index("loaded:target") < service.calls.index("start:target")


@pytest.mark.parametrize("action", ["adopt", "configure", "restart"])
async def test_other_actions_share_phase_engine(tmp_path: Path, action: ManagedAction) -> None:
    with scenario(tmp_path, action=action, changed_config=action == "configure") as (
        _,
        control,
        clock,
        service,
    ):
        final = await finish(ManagedTransactionController(control, service, clock))
        assert final.resolved and final.phase == "committed"
        assert service.calls.count("start:target") == service.calls.count("install:target") == 1


@pytest.mark.parametrize("action", ["adopt", "switch"])
@pytest.mark.parametrize(
    "point",
    [
        "after_configuration:target",
        "after_selector:target",
        "after_unit:target",
        "after_start:target",
        "after_activate",
    ],
)
async def test_new_owner_completes_only_persisted_prefix_without_repeated_start(
    tmp_path: Path, point: str, action: ManagedAction
) -> None:
    with scenario(tmp_path, action=action, changed_config=action == "switch") as (
        journal,
        control,
        clock,
        service,
    ):
        service.interrupt = point
        with pytest.raises(asyncio.CancelledError):
            await finish(ManagedTransactionController(control, service, clock))
        interrupted = journal.read_current()
        assert isinstance(interrupted, TransactionRecordV2)
        assert not interrupted.rollback_attempted and interrupted.failure_code is None
        control.__exit__()
        service.confirmation = None
        with journal.locked() as successor:
            final = await finish(ManagedTransactionController(successor, service, clock))
        assert (
            final.resolved and final.phase == "committed" and final.deadline == interrupted.deadline
        )
        assert service.calls.count("start:target") == 1
        assert service.calls.count("activate:target") == 1


@pytest.mark.parametrize("field", ["configuration", "selector", "unit"])
async def test_foreign_component_never_reconciled(setup: Scenario, field: str) -> None:
    _, control, clock, service = setup
    driver = ManagedTransactionController(control, service, clock)
    await reach(driver, "activating", "install_target")
    setattr(service, field, b'{"foreign":true}' if field != "unit" else b"# foreign unit\n")
    before = service.files()
    failed = await driver.step()
    assert failed.phase == "recovery_required" and failed.code == "foreign_state"
    assert service.files() == before and "install:target" not in service.calls


@pytest.mark.parametrize("location", ["running", "busy", "wrong_intent"])
async def test_mixed_prefix_outside_quiescent_install_window_is_foreign(
    tmp_path: Path, location: str
) -> None:
    with scenario(tmp_path, action="adopt") as (_, control, clock, service):
        driver = ManagedTransactionController(control, service, clock)
        record = await reach(
            driver, "activating", "install_target" if location != "wrong_intent" else "stop_base"
        )
        target = read_snapshots(control.journal, record, "target")
        service.selector = target.selector  # target selector + legacy unit
        if location == "running":
            service.service, service.boot = "running", "origin"
            service.process = ManagedProcessIdentity(
                record.base.entry, record.base.installation, record.base.compatibility
            )
        if location == "busy":
            service.writer_free = False
        if location == "wrong_intent":
            service.service, service.boot, service.writer_free, service.process = (
                "stopped",
                None,
                True,
                None,
            )
        failed = await driver.step()
        assert failed.phase == "recovery_required" and failed.code == "foreign_state"
        assert "install:target" not in service.calls


async def test_frozen_components_in_unpermitted_order_are_not_a_legal_prefix(
    tmp_path: Path,
) -> None:
    with scenario(tmp_path, changed_config=True) as (_, control, clock, service):
        driver = ManagedTransactionController(control, service, clock)
        record = await reach(driver, "activating", "install_target")
        service.selector = read_snapshots(control.journal, record, "target").selector
        # Both components are individually frozen; selector changed before config.
        failed = await driver.step()
        assert failed.phase == "recovery_required" and failed.code == "foreign_state"
        assert "install:target" not in service.calls


@pytest.mark.parametrize("action", ["adopt", "switch"])
@pytest.mark.parametrize(
    "point", ["after_configuration:target", "after_selector:target", "after_unit:target"]
)
async def test_partial_install_failure_only_advances_restore_then_reconciles_once(
    tmp_path: Path, action: ManagedAction, point: str
) -> None:
    with scenario(tmp_path, action=action, changed_config=action == "switch") as (
        journal,
        control,
        clock,
        service,
    ):
        driver = ManagedTransactionController(control, service, clock)
        await reach(driver, "activating", "install_target")
        service.fail = point
        failed = await driver.step()
        assert failed.phase == "rolling_back" and failed.intent == "stop_target"
        assert failed.failure_code == "backend_failed" and failed.rollback_attempted
        before = list(service.calls)
        restoring = await driver.step()
        assert restoring.phase == "rolling_back" and restoring.intent == "restore_base"
        assert service.calls == before  # The prefix hook performs no effect.
        final = await finish(driver)
        assert (
            final.resolved
            and final.phase == "rolled_back"
            and final.failure_code == "backend_failed"
        )
        assert final.deadline == failed.deadline and service.files() == read_snapshots(
            journal, final, "base"
        )
        assert service.calls.count("install:base") == service.calls.count("start:base") == 1
        assert "start:target" not in service.calls and service.calls.count("stop:base") == 1
        assert "private-fixture-error" not in str(final.public())


@pytest.mark.parametrize("unsafe", ["running", "busy", "unknown"])
async def test_restore_prefix_shortcut_rejects_nonquiescent_or_foreign_state(
    tmp_path: Path, unsafe: str
) -> None:
    with scenario(tmp_path, action="adopt") as (_, control, clock, service):
        driver = ManagedTransactionController(control, service, clock)
        record = await reach(driver, "activating", "install_target")
        service.selector = read_snapshots(control.journal, record, "target").selector
        advance_managed(
            control,
            record.position,
            TransitionV2(phase="rolling_back", intent="stop_target", code="backend_failed"),
        )
        if unsafe == "running":
            service.service, service.boot = "running", "origin"
            service.process = ManagedProcessIdentity(
                record.base.entry, record.base.installation, record.base.compatibility
            )
        elif unsafe == "busy":
            service.writer_free = False
        else:
            service.unit = b"# unknown unit\n"
        before = list(service.calls)
        failed = await driver.step()
        assert failed.phase == "recovery_required" and failed.code == "foreign_state"
        assert service.calls == before


@pytest.mark.parametrize("missing", ["all", "loaded", "revision", "role"])
async def test_matching_bytes_without_fresh_full_confirmation_do_not_start(
    setup: Scenario, missing: str
) -> None:
    _, control, clock, service = setup
    driver = ManagedTransactionController(control, service, clock)
    record = await reach(driver, "activating", "install_target")
    desired = read_snapshots(control.journal, record, "target")
    service.configuration, service.selector, service.unit = (
        desired.configuration,
        desired.selector,
        desired.unit,
    )
    service.confirm(record, "target")
    confirmation = service.confirmation
    assert confirmation is not None
    service.confirmation = (
        None
        if missing == "all"
        else replace(confirmation, loaded_unit="f" * 64)
        if missing == "loaded"
        else replace(
            confirmation,
            position=record.position.model_copy(update={"revision": record.revision - 1}),
        )
        if missing == "revision"
        else replace(confirmation, role="base")
    )
    unchanged = await driver.step()
    assert unchanged == record and "start:target" not in service.calls
    assert service.calls.count("install:target") == 1
    assert (await finish(driver)).resolved


@pytest.mark.parametrize("field", ["installation", "compatibility", "entry"])
async def test_matching_files_cannot_hide_wrong_running_process(
    setup: Scenario, field: str
) -> None:
    _, control, clock, service = setup
    driver = ManagedTransactionController(control, service, clock)
    await reach(driver, "verifying")
    assert service.process is not None
    changes: dict[str, object] = {field: "legacy" if field == "entry" else "f" * 64}
    service.process = replace(service.process, **changes)
    failed = await driver.step()
    assert failed.phase == "recovery_required" and failed.code == "foreign_state"
    assert "activate:target" not in service.calls


async def test_origin_boot_cannot_supply_target_health(
    setup: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, control, clock, service = setup
    driver = ManagedTransactionController(control, service, clock)
    await reach(driver, "verifying")
    original = service.observe

    async def old(record: TransactionRecordV2) -> ManagedServiceObservation:
        return replace(
            await original(record),
            boot=record.identity.origin_boot,
            health=proof(record, boot=record.identity.origin_boot),
        )

    monkeypatch.setattr(service, "observe", old)
    failed = await driver.step()
    assert failed.phase == "rolling_back" and failed.code == "health_failed"
    assert "activate:target" not in service.calls


async def test_start_intent_before_command_is_ambiguous_not_an_automatic_retry(
    setup: Scenario,
) -> None:
    journal, control, clock, service = setup
    service.interrupt = "before_start:target"
    with pytest.raises(asyncio.CancelledError):
        await finish(ManagedTransactionController(control, service, clock))
    current = journal.read_current()
    assert isinstance(current, TransactionRecordV2) and current.intent == "start_target"
    assert "start:target" not in service.calls
    final = await finish(ManagedTransactionController(control, service, clock))
    assert final.resolved and final.phase == "rolled_back" and final.deadline == current.deadline
    assert "start:target" not in service.calls and service.calls.count("start:base") == 1


async def test_adopt_rollback_restores_legacy_unit_absence_and_actual_process(
    tmp_path: Path,
) -> None:
    with scenario(tmp_path, action="adopt") as (journal, control, clock, service):
        service.healthy_target = False
        final = await finish(ManagedTransactionController(control, service, clock))
        assert final.resolved and final.phase == "rolled_back" and final.rollback_attempted
        assert service.files() == read_snapshots(journal, final, "base")
        assert (
            service.selector is None
            and service.process is not None
            and service.process.entry == "legacy"
        )
        assert (
            service.calls.index("unit:base")
            < service.calls.index("selector:base")
            < service.calls.index("configuration:base")
        )
        assert service.calls.count("start:base") == service.calls.count("activate:base") == 1


@pytest.mark.parametrize("action", ["adopt", "switch"])
@pytest.mark.parametrize(
    "point", ["after_unit:base", "after_selector:base", "after_configuration:base"]
)
async def test_cancelled_reverse_prefix_resumes_base_without_retrying_target(
    tmp_path: Path, action: ManagedAction, point: str
) -> None:
    with scenario(tmp_path, action=action, changed_config=action == "switch") as (
        journal,
        control,
        clock,
        service,
    ):
        service.healthy_target = False
        driver = ManagedTransactionController(control, service, clock)
        restoring = await reach(driver, "rolling_back", "restore_base")
        service.interrupt = point
        with pytest.raises(asyncio.CancelledError):
            await driver.step()
        interrupted = control.read_current()
        assert interrupted == restoring
        control.__exit__()
        service.confirmation = None
        with journal.locked() as successor:
            final = await finish(ManagedTransactionController(successor, service, clock))
        assert (
            final.resolved and final.phase == "rolled_back" and final.deadline == restoring.deadline
        )
        assert final.failure_code == "health_failed" and service.files() == read_snapshots(
            journal, final, "base"
        )
        assert service.calls.count("start:target") == service.calls.count("start:base") == 1


async def test_observation_crossing_deadline_cannot_commit(
    setup: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, control, clock, service = setup
    driver = ManagedTransactionController(control, service, clock)
    await reach(driver, "verifying")
    original = service.observe

    async def late(record: TransactionRecordV2) -> ManagedServiceObservation:
        observed = await original(record)
        clock.elapsed = 121.0
        return observed

    monkeypatch.setattr(service, "observe", late)
    failed = await driver.step()
    assert failed.phase == "rolling_back" and failed.code == "deadline_exceeded"


async def test_missing_postcommit_observation_does_not_repeat_activation(
    setup: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, control, clock, service = setup
    driver = ManagedTransactionController(control, service, clock)
    committed = await reach(driver, "committed")
    await driver.step()
    assert service.released
    original = service.observe

    async def missing(record: TransactionRecordV2) -> ManagedServiceObservation:
        return replace(await original(record), health=None)

    monkeypatch.setattr(service, "observe", missing)
    assert await driver.step() == committed
    assert service.calls.count("activate:target") == 1
    monkeypatch.undo()
    final = await driver.step()
    assert final.resolved and final.deadline == committed.deadline
    assert service.calls.count("activate:target") == 1
