from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_managed_boot import _slot
from test_managed_transaction import move, proof, verifying

import muli
from muli import managed_boot, managed_preparation, managed_runtime_binding
from muli.config import ConfigFile, Settings
from muli.maintenance import BootPermit
from muli.managed_boot import ManagedBootPermit
from muli.managed_journal import read_snapshots
from muli.managed_preparation import LocalSlotPaths, ManagedPreparation, PreparedManaged
from muli.managed_runtime_binding import verify_running_binding
from muli.managed_transaction import ManagedAction, TransactionRecordV2, TransitionV2
from muli.management_projection import project_final, read_projection
from muli.management_transaction import (
    ClockReading,
    Deadline,
    JournalControl,
    Role,
    ServiceIdentity,
    TransactionJournal,
    Transition,
)
from muli.platforms import installation, managed_deployment
from muli.platforms.installation import InstallationManifest
from muli.platforms.linux_bootstrap import BootSession
from muli.platforms.linux_transaction import ProcessIdentity, ServiceSnapshot, UnitSnapshot
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    resolve_entry,
)
from muli.platforms.managed_probe import ManagedProbeResult, ManagedSystemdProbe
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux local managed preparation")


class Probe(ManagedSystemdProbe):
    """Only the systemd/process boundary is synthetic; files and Store are real."""

    def __init__(self) -> None:
        super().__init__()
        self.boot = "origin"
        self.pid = os.getpid()
        self.during: Callable[[], None] | None = None
        self.calls = 0

    async def read(self, service: UserService) -> ServiceSnapshot:
        self.calls += 1
        await asyncio.sleep(0)
        if self.during is not None:
            self.during()
        return ServiceSnapshot(
            UnitSnapshot(
                name=service.unit,
                load_state="loaded",
                active_state="active",
                sub_state="running",
                fragment=str(service.path),
                source="",
                drop_ins=(),
                needs_reload=False,
                invocation=self.boot,
                job=(0, "/"),
                service_type="simple",
                pid=self.pid,
                cgroup="/synthetic/muli.service",
                executable=service.executable,
                argv=tuple(service._arguments()),
                ignore_failure=False,
                kill_mode="control-group",
                send_sigkill=True,
            ),
            ProcessIdentity(
                self.pid,
                1,
                os.geteuid(),
                service.executable,
                tuple(service._arguments()),
                "/synthetic/muli.service",
            ),
        )

    async def read_managed(
        self,
        journal: TransactionJournal,
        record: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        service = UserService(
            Path(anchor.config_path),
            Path(anchor.data_dir),
            Path(anchor.unit_path).parent,
            platform="linux",
            executable=controller.lexical_python,
        )
        return ManagedProbeResult(
            await self.read(service),
            ("target", "base"),
            frozenset({record.target.unit.digest, record.base.unit.digest}),
            record.target.unit.digest,
        )


@dataclass
class Host:
    runtime: Runtime
    service: UserService
    journal: TransactionJournal
    slot: SlotIdentity
    probe: Probe
    prepare: ManagedPreparation
    monkeypatch: pytest.MonkeyPatch
    root: Path

    @staticmethod
    def paths(slot: SlotIdentity) -> LocalSlotPaths:
        return LocalSlotPaths(Path(slot.prefix), Path(slot.lexical_python), Path(slot.package.root))

    def plan(self, action: ManagedAction, *, name: str = "request") -> dict[str, Any]:
        origin = ReplyOrigin.load(
            {
                "kind": "channel",
                "input_id": "original-input",
                "route": {
                    "name": "telegram",
                    "account_id": "owner-account",
                    "target": "owner",
                    "sender_id": "owner",
                    "kind": "private",
                    "reply_to": "original-message",
                },
            }
        )
        assert origin is not None
        item = {
            "id": name,
            "action": action,
            "status": "pending",
            "routing": FrozenRouting.bound(origin).dump(),
        }
        self.runtime.store.put("management", item)
        return item

    async def run(
        self,
        item: dict[str, Any],
        *,
        target: Settings | None = None,
        candidate: SlotIdentity | None = None,
    ) -> PreparedManaged:
        async with self.runtime.configuration_lock:
            return await self.prepare.prepare(
                item,
                target or self.runtime.config.settings,
                candidate=self.paths(candidate) if candidate is not None else None,
            )

    def complete(self, prepared: PreparedManaged, *, project: bool = True) -> TransactionRecordV2:
        """Synthetic service completion, using real admission and final projection."""
        record = verifying(prepared.record)
        record = move(record, TransitionV2(phase="committed", proof=proof(record, boot="fresh")))
        record = move(
            record,
            TransitionV2(
                phase="committed",
                activation="released",
                proof=proof(record, boot="fresh", admission="released"),
            ),
        )
        data = read_snapshots(self.journal, record, "target")
        Path(prepared.anchor.config_path).write_bytes(data.configuration)
        Path(prepared.anchor.unit_path).write_bytes(data.unit)
        with SelectorStore(self.journal.directory.path) as selector:
            selector.replace(
                selector.read(),
                SelectorValue.model_validate_json(data.selector),
                guard=lambda: None,
            )
        with self.journal.locked() as control:
            control._write(record)
        previous = self.runtime.maintenance.permit
        assert previous is not None and self.runtime.boot_session is not None
        permit = ManagedBootPermit.acquire(
            self.journal,
            boot="fresh",
            plan=resolve_entry(self.journal.directory.path, purpose="service"),
            lease=previous.lease,
        )
        self.runtime.maintenance.permit = permit
        self.runtime.boot_session.permit = permit
        self.runtime.config = permit.config
        # This branch predates the root's Runtime/Maintenance V2 dispatch. Only
        # that host seam is adapted; preparation itself reads the shared journal.
        self.monkeypatch.setattr(self.runtime.maintenance, "current", self.journal.read_current)
        self.probe.boot = permit.boot
        if project:
            assert (
                project_final(
                    permit,
                    self.runtime.store,
                    verify_binding=lambda value, role: verify_running_binding(
                        permit, self.runtime.store, value, role
                    ),
                )
                is not None
            )
        return record


@pytest.fixture
async def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Host]:
    slot = _slot(tmp_path, "retained")
    base = tmp_path / "base-python"
    base.mkdir()
    system = SimpleNamespace(
        platform="linux",
        executable=slot.lexical_python,
        prefix=slot.prefix,
        base_prefix=str(base),
    )
    monkeypatch.setattr(managed_deployment, "sys", system)
    monkeypatch.setattr(installation, "sys", system)
    monkeypatch.setattr(muli, "__file__", str(Path(slot.package.root) / "__init__.py"))
    config = tmp_path / "config.json"
    config.write_text(
        Settings(updates={"check_on_start": False}).model_dump_json(), encoding="utf-8"
    )
    config.chmod(0o600)
    data = tmp_path / "data"
    data.mkdir()
    database = data / "muli.sqlite3"
    service = UserService(
        config, data, tmp_path / "units", platform="linux", executable=slot.lexical_python
    )
    service.path.parent.mkdir()
    service.path.write_text(service.render(), encoding="utf-8")
    service.path.chmod(0o600)
    journal = TransactionJournal(data / "management/transaction-v1", database)
    manifest = InstallationManifest.capture(service, database, require_database=False)
    permit = BootPermit.acquire(
        journal,
        config,
        boot="origin",
        installation=manifest.digest,
        service=manifest.service_digest,
    )
    session = BootSession(journal, permit.lease, config, permit, manifest)
    runtime = Runtime(ConfigFile(config), data, boot_session=session)
    for module in (managed_preparation, managed_boot, managed_runtime_binding):
        monkeypatch.setattr(module, "verify_local_filesystems", lambda paths: ())
    probe = Probe()
    subject = ManagedPreparation(
        runtime,
        service,
        probe=probe,
        clock=lambda: ClockReading(boot_id="kernel", wall=100.0, monotonic=100.0),
    )
    try:
        yield Host(runtime, service, journal, slot, probe, subject, monkeypatch, tmp_path)
    finally:
        current = runtime.maintenance.permit
        await runtime.close()
        if isinstance(current, ManagedBootPermit):
            current.close_after_writers()


async def test_adopt_persists_plan_first_and_freezes_exact_original_files(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = host.plan("adopt")
    original = host.service.config_path.read_bytes(), host.service.path.read_bytes()
    replaced = JournalControl._replace
    seen: list[str] = []

    def snapshot(control: JournalControl, name: str, data: bytes) -> None:
        saved = host.runtime.store.get("management", item["id"])
        assert saved["status"] == "applying"
        assert saved["transaction"]["generation"] in name or name == "state.json"
        assert saved["routing"] == item["routing"]
        assert host.runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert not host.runtime.store.db.in_transaction
        seen.append(name)
        replaced(control, name, data)

    monkeypatch.setattr(JournalControl, "_replace", snapshot)
    result = await host.run(item, candidate=host.slot)
    assert host.journal.read_current() == result.record
    assert result.record.action == "adopt" and result.record.phase == "prepared"
    assert result.record.base.installation == result.record.target.installation == host.slot.digest
    assert read_snapshots(host.journal, result.record, "base").unit == original[1]
    assert result.record.base.selector is None
    assert host.service.config_path.read_bytes() == original[0]
    assert host.service.path.read_bytes() == original[1]
    assert not (host.journal.directory.path / "selector.json").exists()
    assert seen[-1] == "state.json" and host.probe.calls == 1
    assert not [e for e in host.runtime.store.events() if e["type"] == "management.completed"]


async def test_current_installation_observation_does_not_create_plan_or_generation(
    host: Host,
) -> None:
    adopted = host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    plans = host.runtime.store.list("management", 100)
    position = host.runtime.store.event_position()
    async with host.runtime.configuration_lock:
        observed = await host.prepare.observe_current()
        repeated = await host.prepare.observe_current()
    assert observed.record == adopted
    assert observed.slot == host.slot
    assert observed.binding_revision == repeated.binding_revision
    assert len(observed.binding_revision) == 64
    observed.verify()
    host.runtime.store.put("notes", {"id": "concurrent", "text": "new user information"})
    observed.verify()
    assert host.journal.read_current() == adopted
    assert host.runtime.store.list("management", 100) == plans
    assert host.runtime.store.event_position() == position


async def test_current_installation_observation_rejects_legacy_without_adopting(
    host: Host,
) -> None:
    async with host.runtime.configuration_lock:
        with pytest.raises(ValueError, match="managed_boot_required"):
            await host.prepare.observe_current()
    assert host.journal.read_current() is None
    assert not host.runtime.store.list("management", 100)


async def test_current_installation_observation_requires_exact_projection(host: Host) -> None:
    host.complete(await host.run(host.plan("adopt"), candidate=host.slot), project=False)
    async with host.runtime.configuration_lock:
        # Maintenance already blocks configuration until this exact final is projected.
        with pytest.raises(ValueError, match="transaction_in_progress"):
            await host.prepare.observe_current()


async def test_current_installation_observation_rechecks_after_process_await(host: Host) -> None:
    host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    original = host.service.path.read_bytes()

    def drift() -> None:
        host.service.path.write_bytes(original + b"\n")

    host.probe.during = drift
    async with host.runtime.configuration_lock:
        with pytest.raises(ValueError, match="managed_preparation_files_changed"):
            await host.prepare.observe_current()


async def test_current_installation_observation_tracks_hot_configuration(host: Host) -> None:
    host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    async with host.runtime.configuration_lock:
        original = await host.prepare.observe_current()
        host.runtime.config.update({"updates": {"check_on_start": True}})
        with pytest.raises(ValueError, match="managed_source_changed"):
            original.verify()
        changed = await host.prepare.observe_current()
    assert original.binding_revision != changed.binding_revision
    assert original.slot == changed.slot
    changed.verify()


@pytest.mark.parametrize("action", ["restart", "configure", "switch"])
async def test_managed_actions_capture_actual_base_after_exact_projection(
    host: Host, action: ManagedAction
) -> None:
    first = await host.run(host.plan("adopt", name="adopt"), candidate=host.slot)
    old = host.complete(first)
    assert read_projection(host.runtime.store) is not None
    host.runtime.config.update({"persona": {"user_name": "current hot configuration"}})
    current = host.runtime.config.path.read_bytes()
    if action == "switch":
        current = (
            b" \n"
            + json.dumps(
                host.runtime.config.settings.model_dump(),
                ensure_ascii=False,
                separators=(",", ": "),
            ).encode()
            + b"\n"
        )
        host.runtime.config.path.write_bytes(current)
    target = host.runtime.config.validate_changes({"persona": {"user_name": "new selection"}})
    candidate = _slot(host.root, "candidate") if action == "switch" else None
    result = await host.run(
        host.plan(action, name="next"),
        target=target if action == "configure" else host.runtime.config.settings,
        candidate=candidate,
    )
    assert result.record.generation != old.generation
    assert result.anchor == first.anchor
    assert read_snapshots(host.journal, result.record, "base").configuration == current
    assert result.record.base.installation == host.slot.digest
    assert result.record.target.installation == (candidate or host.slot).digest
    assert (result.record.base.configuration == result.record.target.configuration) == (
        action != "configure"
    )
    if action == "switch":
        assert read_snapshots(host.journal, result.record, "target").configuration == current
    assert host.runtime.config.path.read_bytes() == current
    saved = host.runtime.store.get("management", "next")
    assert saved["transaction"]["generation"] == result.record.generation
    assert saved["status"] == "applying"


@pytest.mark.parametrize("projected", [False, True])
async def test_unresolved_or_unprojected_generation_has_no_new_preparation(
    host: Host, projected: bool
) -> None:
    first = await host.run(host.plan("adopt", name="first"), candidate=host.slot)
    if not projected:
        host.complete(first, project=False)
    else:
        host.monkeypatch.setattr(host.runtime.maintenance, "current", host.journal.read_current)
    item = host.plan("restart", name="second")
    before = host.runtime.store.db.total_changes
    raw = (host.journal.directory.path / "state.json").read_bytes()
    with pytest.raises(ValueError, match="transaction_in_progress|projection_required"):
        await host.run(item)
    assert host.runtime.store.db.total_changes == before
    assert (host.journal.directory.path / "state.json").read_bytes() == raw


@pytest.mark.parametrize("change", ["candidate", "configuration", "plan", "marker", "pid", "boot"])
async def test_probe_await_revalidates_before_any_preparing_commit(host: Host, change: str) -> None:
    item = host.plan("adopt")
    before = host.runtime.store.event_position()

    def changed() -> None:
        if change == "candidate":
            Path(host.slot.package.root, "__init__.py").write_bytes(b"changed after observation")
        elif change == "configuration":
            host.runtime.config.path.write_bytes(b"{}")
        elif change == "plan":
            host.runtime.store.put("management", {**item, "routing": FrozenRouting().dump()})
        elif change == "marker":
            with host.runtime.store.db:
                host.runtime.store.db.execute(
                    "DELETE FROM runtime_state WHERE name='format_state_v1'"
                )
        elif change == "pid":
            host.probe.pid += 1
        else:
            host.probe.boot = "other-boot"

    host.probe.during = changed
    with pytest.raises(ValueError):
        await host.run(item, candidate=host.slot)
    assert host.journal.read_current() is None
    assert host.runtime.store.event_position() == before
    assert host.runtime.store.get("management", item["id"])["status"] == "pending"


@pytest.mark.parametrize("when", ["snapshot", "state"])
async def test_failure_after_plan_commit_preserves_recoverable_generation(
    host: Host, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    item = host.plan("adopt")
    replace = JournalControl._replace

    def failing(control: JournalControl, name: str, data: bytes) -> None:
        replace(control, name, data)
        if (when == "snapshot" and name.endswith("-target-unit.bin")) or (
            when == "state" and name == "state.json"
        ):
            host.runtime.config.path.write_bytes(b"{}")

    monkeypatch.setattr(JournalControl, "_replace", failing)
    with pytest.raises(ValueError):
        await host.run(item, candidate=host.slot)
    saved = host.runtime.store.get("management", item["id"])
    assert saved["status"] == "applying" and saved["routing"] == item["routing"]
    generation = saved["transaction"]["generation"]
    current = host.journal.read_current()
    if when == "snapshot":
        assert current is None
        assert not (host.journal.directory.path / "state.json").exists()
    else:
        assert current is not None and current.generation == generation
        assert current.phase == "prepared"
    assert not [e for e in host.runtime.store.events() if e["type"] == "management.completed"]


async def test_plan_commit_failure_never_publishes_static_or_state(host: Host) -> None:
    item = host.plan("adopt")
    position = host.runtime.store.event_position()

    def deny_commit(
        action: int,
        first: str | None,
        second: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
            else sqlite3.SQLITE_OK
        )

    host.runtime.store.db.set_authorizer(deny_commit)
    try:
        with pytest.raises(sqlite3.Error):
            await host.run(item, candidate=host.slot)
    finally:
        host.runtime.store.db.set_authorizer(None)
    assert {p.name for p in host.journal.directory.path.iterdir()} == {"lock"}
    assert host.runtime.store.get("management", item["id"])["status"] == "pending"
    assert host.runtime.store.event_position() == position
    assert not host.runtime.store.db.in_transaction


async def test_cancellation_before_preparation_has_no_durable_effect(host: Host) -> None:
    item = host.plan("adopt")

    def cancel() -> None:
        raise asyncio.CancelledError

    host.probe.during = cancel
    before = host.runtime.store.db.total_changes
    with pytest.raises(asyncio.CancelledError):
        await host.run(item, candidate=host.slot)
    assert host.journal.read_current() is None
    assert host.runtime.store.db.total_changes == before


@pytest.mark.parametrize(
    "action,candidate", [("adopt", False), ("restart", True), ("switch", False)]
)
async def test_candidate_paths_are_explicit_and_action_specific(
    host: Host, action: ManagedAction, candidate: bool
) -> None:
    item = host.plan(action)
    before = host.runtime.store.db.total_changes
    with pytest.raises(ValueError, match="managed_candidate_paths_required"):
        await host.run(item, candidate=host.slot if candidate else None)
    assert host.runtime.store.db.total_changes == before
    assert host.probe.calls == 0


async def test_legacy_resolved_record_requires_real_exact_projection_before_adoption(
    host: Host,
) -> None:
    item = host.plan("restart", name="legacy")
    permit = host.runtime.maintenance.permit
    assert isinstance(permit, BootPermit)
    with host.journal.locked() as control:
        old = control.prepare(
            request_id=item["id"],
            action="restart",
            identity=ServiceIdentity(
                installation=permit.installation,
                service=permit.service,
                store_instance=host.runtime.store.instance_id,
                main_session=host.runtime.main_session()["id"],
                origin_boot=permit.boot,
            ),
            base=host.runtime.config.path.read_bytes(),
            target=host.runtime.config.path.read_bytes(),
            deadline=Deadline(
                created=ClockReading(boot_id="kernel", wall=100.0, monotonic=100.0),
                seconds=180.0,
                rollback_reserve=60.0,
            ),
            writer=permit.lease,
        )
        old = control.advance(old.position, Transition(phase="aborted", code="cancelled"))
    host.runtime.store.put("management", {**item, "transaction": old.public()})
    assert project_final(permit, host.runtime.store) is not None
    result = await host.run(host.plan("adopt", name="next"), candidate=host.slot)
    assert result.record.protocol == 2 and result.record.generation != old.generation
    assert host.journal.snapshot(old, "base") == host.runtime.config.path.read_bytes()


@pytest.mark.parametrize("foreign", [False, True])
async def test_failed_adoption_can_reuse_only_the_actual_retained_legacy_installation(
    host: Host, foreign: bool
) -> None:
    prepared = await host.run(host.plan("adopt", name="first"), candidate=host.slot)
    record = move(prepared.record, TransitionV2(phase="aborted", code="cancelled"))
    with host.journal.locked() as control:
        control._write(record)
    permit = host.runtime.maintenance.permit
    assert isinstance(permit, BootPermit)
    assert (
        project_final(
            permit,
            host.runtime.store,
            verify_binding=lambda value, role: verify_running_binding(
                permit, host.runtime.store, value, role
            ),
        )
        is not None
    )
    host.monkeypatch.setattr(host.runtime.maintenance, "current", host.journal.read_current)
    if foreign:
        permit.installation = "f" * 64
    item = host.plan("adopt", name="second")
    before = host.runtime.store.db.total_changes
    if foreign:
        with pytest.raises(ValueError, match="runtime_binding_installation_changed"):
            await host.run(item, candidate=host.slot)
        assert host.runtime.store.db.total_changes == before
        assert host.journal.read_current() == record
    else:
        result = await host.run(item, candidate=host.slot)
        assert result.anchor == prepared.anchor
        assert result.record.base.entry == "legacy"


async def test_local_candidate_must_support_the_actual_persistent_formats(host: Host) -> None:
    result = await host.run(host.plan("adopt", name="first"), candidate=host.slot)
    host.complete(result)
    candidate = _slot(host.root, "incompatible")
    declaration = Path(candidate.declaration.path)
    metadata = json.loads(declaration.read_bytes())
    metadata["formats"]["outbox"] = {"reads": [2], "writes": 2}
    declaration.write_text(json.dumps(metadata), encoding="utf-8")
    item = host.plan("switch", name="second")
    before = host.runtime.store.db.total_changes
    previous = host.journal.read_current()
    with pytest.raises(ValueError, match="format"):
        await host.run(item, candidate=candidate)
    assert host.journal.read_current() == previous
    assert host.runtime.store.db.total_changes == before


async def test_existing_prepared_request_never_starts_a_new_deadline(host: Host) -> None:
    item = host.plan("adopt")
    first = await host.run(item, candidate=host.slot)
    host.monkeypatch.setattr(host.runtime.maintenance, "current", host.journal.read_current)
    before = host.runtime.store.db.total_changes
    with pytest.raises(ValueError, match="transaction_in_progress"):
        await host.run(item, candidate=host.slot)
    assert host.journal.read_current() == first.record
    assert host.runtime.store.db.total_changes == before


@pytest.mark.parametrize("during_probe", [False, True])
async def test_actual_admitted_schema_is_rechecked_before_preparation(
    host: Host, during_probe: bool
) -> None:
    item = host.plan("adopt")

    def break_schema() -> None:
        with host.runtime.store.db:
            host.runtime.store.db.execute("DROP INDEX ingress_system_log_run")

    if during_probe:
        host.probe.during = break_schema
    else:
        break_schema()
    position = host.runtime.store.event_position()
    with pytest.raises(ValueError):
        await host.run(item, candidate=host.slot)
    assert host.journal.read_current() is None
    assert host.runtime.store.event_position() == position
    assert host.runtime.store.get("management", item["id"])["status"] == "pending"


async def test_switch_cannot_smuggle_configuration_changes(host: Host) -> None:
    previous = await host.run(host.plan("adopt", name="first"), candidate=host.slot)
    old = host.complete(previous)
    item = host.plan("switch", name="second")
    candidate = _slot(host.root, "candidate")
    target = host.runtime.config.validate_changes({"persona": {"user_name": "not a slot change"}})
    before = host.runtime.store.db.total_changes
    calls = host.probe.calls
    with pytest.raises(ValueError, match="managed_switch_changes_configuration"):
        await host.run(item, candidate=candidate, target=target)
    assert host.journal.read_current() == old
    assert host.runtime.store.db.total_changes == before
    assert host.probe.calls == calls
