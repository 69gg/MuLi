from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_managed_boot as admission_fixture
from test_linux_transaction import FakeBus
from test_managed_boot import _CONFIG, _slot
from test_managed_boot import Host as AdmissionHost

from muli import managed_boot
from muli.format_evidence import read_format_evidence
from muli.managed_boot import ManagedBootError, ManagedBootPermit
from muli.managed_journal import RoleSnapshots
from muli.managed_transaction import TransactionRecordV2, raw_snapshot
from muli.management_transaction import Role, TransactionError, TransactionJournal
from muli.platforms import managed_bootstrap
from muli.platforms.linux_bootstrap import BootSession, BootstrapError, open_boot_session
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore, SelectorValue
from muli.platforms.managed_probe import (
    ManagedSystemdProbe,
    ManagedUserService,
    managed_process_arguments,
)
from muli.service import UserService
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed service bootstrap")
_ROLES: tuple[Role, ...] = ("base", "target")


@dataclass
class Host:
    admission: AdmissionHost
    service: UserService
    managed_service: ManagedUserService
    bus: FakeBus
    proc: Path
    probe: ManagedSystemdProbe
    sessions: list[BootSession] = field(default_factory=list)

    @property
    def journal(self) -> TransactionJournal:
        return self.admission.journal

    def record(
        self, phase: str = "activating", intent: str = "start_target", **kwargs: Any
    ) -> None:
        record = self.admission.record(phase, intent, **kwargs)
        for role in _ROLES:
            binding = record.binding(role)
            selector = (
                None
                if binding.selector is None
                else (
                    SelectorValue(
                        deployment=self.admission.anchor.digest,
                        installation=binding.installation,
                    ).canonical()
                )
            )
            unit = (
                self.managed_service.render().encode()
                if binding.entry == "managed"
                else self.legacy().render().encode()
            )
            with self.journal.locked() as control:
                for name, data in (
                    RoleSnapshots(_CONFIG, selector, unit).entries(record.generation, role).items()
                ):
                    control.directory.replace(name, data)
        self.process("base" if phase in {"rolled_back", "rolling_back", "aborted"} else "target")

    def legacy(self) -> UserService:
        return UserService(
            self.service.config_path,
            self.service.data_dir,
            self.service.path.parent,
            platform="linux",
            executable=self.admission.base.lexical_python,
        )

    def process(self, role: Role) -> None:
        record = self.journal.read_current()
        assert isinstance(record, TransactionRecordV2)
        slot = self.admission.base if role == "base" else self.admission.target
        if record.binding(role).installation == self.admission.base.digest:
            slot = self.admission.base
        arguments = (
            tuple(self.legacy()._arguments())
            if record.binding(role).entry == "legacy"
            else managed_process_arguments(self.admission.anchor, slot)
        )
        process = self.proc / str(os.getpid())
        (process / "cmdline").write_bytes(
            b"\x00".join(os.fsencode(value) for value in arguments) + b"\x00"
        )
        (process / "exe").unlink(missing_ok=True)
        (process / "exe").symlink_to(slot.resolved_python.path)
        self.service.executable = slot.lexical_python

    async def open(self, *, explicit: bool = False) -> BootSession:
        session = await open_boot_session(
            self.service,
            self.journal.database,
            managed_root=self.journal.directory.path if explicit else None,
            managed_probe=self.probe,
        )
        self.sessions.append(session)
        return session


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    database = tmp_path / "data/muli.sqlite3"
    with closing(Store.open_runtime(database, default_mode="auto")) as store:
        evidence = read_format_evidence(store.db)
    base, target = _slot(tmp_path, "base"), _slot(tmp_path, "target")
    config, unit_dir = tmp_path / "config.json", tmp_path / "units"
    config.write_bytes(_CONFIG)
    config.chmod(0o600)
    unit_dir.mkdir()
    root = database.parent / "management/transaction-v1"
    service = UserService(config, database.parent, unit_dir, executable=target.lexical_python)
    managed_service = ManagedUserService(
        config,
        database.parent,
        unit_dir,
        controller=base,
        launcher=root / "launcher.py",
        journal_root=root,
    )
    unit = managed_service.render().encode()
    service.path.write_bytes(unit)
    service.path.chmod(0o600)
    monkeypatch.setattr(admission_fixture, "_UNIT", unit)
    monkeypatch.setattr(
        admission_fixture,
        "_LEGACY_UNIT",
        UserService(config, database.parent, unit_dir, executable=base.lexical_python)
        .render()
        .encode(),
    )
    with TransactionJournal(root, database) as journal:
        with DeploymentStore(root) as source:
            source.publish_slot(base, guard=lambda: None)
            source.publish_slot(target, guard=lambda: None)
            launcher = source.publish_launcher(guard=lambda: None)
            anchor = DeploymentAnchor(
                config_path=str(config),
                data_dir=str(database.parent),
                database_path=str(database),
                unit_path=str(service.path),
                journal_root=str(root),
                controller_slot=base.digest,
                launcher=launcher,
                managed_unit=raw_snapshot(unit, journal.limits.max_snapshot_bytes),
            )
            source.publish_anchor(anchor, guard=lambda: None)
        admitted = AdmissionHost(journal, anchor, base, target, evidence, monkeypatch)
        process = tmp_path / "proc" / str(os.getpid())
        process.mkdir(parents=True)
        fields = ["S", *(["0"] * 18), "100", "0"]
        (process / "stat").write_text(
            f"{os.getpid()} (synthetic current service) " + " ".join(fields), encoding="utf-8"
        )
        (process / "cgroup").write_text("0::/user.slice/example.service\n", encoding="utf-8")
        bus = FakeBus(managed_service)
        bus.rows["MainPID"] = ("u", os.getpid())
        result = Host(
            admitted,
            service,
            managed_service,
            bus,
            process.parent,
            ManagedSystemdProbe(runner=bus, proc_root=process.parent),
        )
        monkeypatch.setattr(managed_boot, "verify_local_filesystems", lambda paths: None)
        result.record()
        try:
            yield result
        finally:
            for session in result.sessions:
                session.close_after_writers()


def assert_unlocked(host: Host) -> None:
    with host.journal.writer_lease():
        pass


@pytest.mark.parametrize("explicit", [False, True])
async def test_fresh_actual_service_identity_precedes_configuration_and_sqlite(
    host: Host, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    # A protocol-2 boot must not consult a stale or damaged V1 installation.
    host.journal.directory.replace("installation.json", b"not a legacy manifest")
    original = managed_boot.ManagedBootPermit.acquire
    observed: list[str] = []

    def acquire(*args: Any, **kwargs: Any) -> ManagedBootPermit:
        assert len(host.bus.calls) == 5
        with pytest.raises(TransactionError, match="lock_busy"):
            host.journal.writer_lease().__enter__()
        observed.append("admission")
        return original(*args, **kwargs)

    monkeypatch.setattr(managed_bootstrap.ManagedBootPermit, "acquire", acquire)
    session = await host.open(explicit=explicit)
    assert observed == ["admission"]
    assert isinstance(session.permit, ManagedBootPermit)
    assert session.permit.role == "target" and session.permit.probation
    assert session.permit.lease is session.lease
    assert session.manifest is None and session.unsupported_reason is None
    assert (
        host.journal.directory.path / "installation.json"
    ).read_bytes() == b"not a legacy manifest"
    with closing(session.permit.open_store()) as store:
        assert store.db.total_changes == 0 and not store.migration_allowed
        store.put("private_test", {"id": "accepted", "value": "after fresh admission"})
    session.close_after_writers()
    assert_unlocked(host)


async def test_legacy_raw_unit_rollback_dispatches_v2_without_a_managed_root_flag(
    host: Host,
) -> None:
    host.record("rolled_back", "none", legacy=True)
    legacy = host.legacy()
    host.bus = FakeBus(legacy)
    host.bus.rows["MainPID"] = ("u", os.getpid())
    host.probe = ManagedSystemdProbe(runner=host.bus, proc_root=host.proc)
    session = await host.open()
    assert isinstance(session.permit, ManagedBootPermit)
    assert session.permit.role == "base" and session.permit.probation
    assert "--managed-root" not in tuple(legacy._arguments())
    assert session.permit._plan.argv != tuple(legacy._arguments())
    with pytest.raises(TransactionError, match="lock_busy"):
        host.journal.writer_lease().__enter__()


async def test_a_valid_other_role_cannot_admit_this_boot(host: Host) -> None:
    host.process("base")
    with pytest.raises(BootstrapError, match="managed_boot_process_mismatch"):
        await host.open()
    assert_unlocked(host)


@pytest.mark.parametrize("change", ["pid", "boot", "state", "config_path", "unit_path"])
async def test_wrong_host_identity_never_opens_application_store(
    host: Host, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    if change == "pid":
        host.bus.rows["MainPID"] = ("u", 0)
    elif change == "boot":
        host.bus.rows["InvocationID"] = ("ay", [0] * 16)
    elif change == "state":
        host.bus.rows["ActiveState"] = ("s", "inactive")
    elif change == "config_path":
        host.service.config_path = host.service.config_path.with_name("different.json")
    else:
        host.service.path = host.service.path.parent / "different.service"

    def forbidden(*args: Any, **kwargs: Any) -> ManagedBootPermit:
        pytest.fail("unverified current service reached configuration/SQLite admission")

    monkeypatch.setattr(managed_bootstrap.ManagedBootPermit, "acquire", forbidden)
    with pytest.raises(BootstrapError):
        await host.open()
    assert_unlocked(host)


async def test_same_generation_progress_during_probe_uses_fresh_admission(host: Host) -> None:
    def advance() -> None:
        with host.journal.locked() as control:
            current = control.read_current()
            assert isinstance(current, TransactionRecordV2)
            control._write(
                TransactionRecordV2.model_validate(
                    {**current.model_dump(), "phase": "verifying", "intent": "none", "revision": 2}
                )
            )

    host.bus.before_second = advance
    session = await host.open()
    assert isinstance(session.permit, ManagedBootPermit)
    assert session.permit.record.revision == 2 and session.permit.record.phase == "verifying"


async def test_caller_paths_are_rechecked_after_the_probe_await(host: Host) -> None:
    def change() -> None:
        host.service.data_dir = host.service.data_dir.with_name("foreign-data")

    host.bus.before_second = change
    with pytest.raises(BootstrapError, match="boot_paths_changed"):
        await host.open()
    assert_unlocked(host)


@pytest.mark.parametrize("phase,role", [("committed", "target"), ("rolled_back", "base")])
async def test_resolved_boot_uses_the_durable_terminal_role(
    host: Host, phase: str, role: Role
) -> None:
    host.record(phase, "none", activation="released")
    session = await host.open()
    assert isinstance(session.permit, ManagedBootPermit)
    assert session.permit.role == role and not session.permit.probation


async def test_cancel_during_observation_releases_only_the_untransferred_lease(host: Host) -> None:
    host.bus.block = True
    task = asyncio.create_task(host.open())
    await asyncio.wait_for(host.bus.started.wait(), 1)
    with pytest.raises(TransactionError, match="lock_busy"):
        host.journal.writer_lease().__enter__()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.bus.cancelled and host.sessions == []
    assert_unlocked(host)


async def test_explicit_managed_root_cannot_fall_back_to_an_absent_record(host: Host) -> None:
    (host.journal.directory.path / "state.json").unlink()
    with pytest.raises(BootstrapError, match="managed_record_required"):
        await host.open(explicit=True)
    assert host.bus.calls == []
    assert_unlocked(host)


async def test_missing_explicit_root_is_not_created(host: Host) -> None:
    missing = host.journal.directory.path.parent / "missing-root"
    with pytest.raises(BootstrapError, match="managed_journal_unavailable"):
        await open_boot_session(host.service, host.journal.database, managed_root=missing)
    assert not missing.exists()


async def test_close_refusal_retains_journal_and_lease_and_can_never_mark_closed(
    host: Host,
) -> None:
    session = await host.open()
    assert isinstance(session.permit, ManagedBootPermit)
    permit = session.permit
    permit._cleanup_failed = True
    try:
        for _ in range(2):
            with pytest.raises(ManagedBootError, match="managed_boot_cleanup_failed") as caught:
                session.close_after_writers()
            assert caught.value._owner is permit and not session._closed
            session.journal.directory.check()
            with pytest.raises(TransactionError, match="lock_busy"):
                host.journal.writer_lease().__enter__()
    finally:
        # Test-only confirmation: no application Store was opened by this case.
        permit._cleanup_failed = False


async def test_acquire_cleanup_failure_keeps_its_real_connection_and_journal_owner(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenClose(sqlite3.Connection):
        def close(self) -> None:
            raise OSError("synthetic connection close failure")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return sqlite3.connect(*args, **kwargs, factory=BrokenClose)

    monkeypatch.setattr(
        managed_boot, "sqlite3", SimpleNamespace(**{**vars(sqlite3), "connect": connect})
    )
    with pytest.raises(ManagedBootError, match="managed_boot_cleanup_failed") as caught:
        await host.open()
    permit = caught.value._owner
    assert permit is not None and permit._unclosed is not None
    assert not permit._closed
    permit.journal.directory.check()
    with pytest.raises(TransactionError, match="lock_busy"):
        host.journal.writer_lease().__enter__()
    # Only synthetic cleanup bypasses the injected close failure after confirming
    # the actual connection closed; production retains this owner until exit.
    connection = permit._unclosed
    assert isinstance(connection, sqlite3.Connection)
    sqlite3.Connection.close(connection)
    permit._cleanup_failed = False
    permit.close_after_writers()
    permit.journal.__exit__()
    assert_unlocked(host)
