from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import muli
from muli.maintenance import BootPermit, MaintenanceError
from muli.management_transaction import (
    ClockReading,
    Deadline,
    JournalLimits,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.platforms import linux_bootstrap
from muli.platforms.filesystem_identity import FilesystemError
from muli.platforms.installation import HelperLimits, InstallationManifest, PackageLimits
from muli.platforms.linux_bootstrap import BootstrapError, open_boot_session
from muli.platforms.linux_transaction import (
    ObservationError,
    ProbeLimits,
    ProcessIdentity,
    ServiceSnapshot,
    UnitSnapshot,
)
from muli.service import UserService
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux pre-Store writer ownership")
BASE = b'{"state":"base"}'
TARGET = b'{"state":"target"}'
INVOCATION = "1" * 32


class FakeProbe:
    def __init__(self, service: UserService) -> None:
        self.snapshot = ServiceSnapshot(
            UnitSnapshot(
                name=service.unit,
                load_state="loaded",
                active_state="active",
                sub_state="running",
                fragment=str(service.path),
                source="",
                drop_ins=(),
                needs_reload=False,
                invocation=INVOCATION,
                job=(0, "/"),
                service_type="simple",
                pid=os.getpid(),
                cgroup="/user.slice/muli.service",
                executable=service.executable,
                argv=tuple(service._arguments()),
                ignore_failure=False,
                kill_mode="control-group",
                send_sigkill=True,
            ),
            ProcessIdentity(
                os.getpid(),
                42,
                os.geteuid(),
                str(Path(service.executable).resolve()),
                tuple(service._arguments()),
                "/user.slice/muli.service",
            ),
        )
        self.entered = asyncio.Event()
        self.finish: asyncio.Event | None = None
        self.before: Callable[[], None] | None = None
        self.error: BaseException | None = None
        self.calls = 0

    async def read(self, service: UserService) -> ServiceSnapshot:
        self.calls += 1
        self.entered.set()
        if self.finish is not None:
            await self.finish.wait()
        if self.before is not None:
            self.before()
        if self.error is not None:
            raise self.error
        return self.snapshot


@dataclass(slots=True)
class Host:
    service: UserService
    database: Path
    package: Path
    store_instance: str
    main: str
    checked: list[tuple[Path, ...]]

    @property
    def root(self) -> Path:
        return self.service.data_dir / "management/transaction-v1"

    def capture(self, *, custom_limits: bool = False) -> InstallationManifest:
        return InstallationManifest.capture(
            self.service,
            self.database,
            helper=HelperLimits(seconds=240.0, operation_timeout=28.0) if custom_limits else None,
            journal=JournalLimits(max_record_bytes=65_536) if custom_limits else None,
            package_limits=PackageLimits(entries=32) if custom_limits else None,
        )

    def prepare(self, *, custom_limits: bool = False) -> TransactionRecord:
        manifest = self.capture(custom_limits=custom_limits)
        with TransactionJournal(self.root, self.database, manifest.journal) as journal:
            with journal.locked() as control:
                manifest.publish(control)
                with journal.writer_lease() as lease:
                    return control.prepare(
                        request_id="request-one",
                        action="configure",
                        identity=ServiceIdentity(
                            installation=manifest.digest,
                            service=manifest.service_digest,
                            store_instance=self.store_instance,
                            main_session=self.main,
                            origin_boot="0" * 32,
                        ),
                        base=BASE,
                        target=TARGET,
                        deadline=Deadline(
                            created=ClockReading(boot_id="kernel", wall=1.0, monotonic=1.0),
                            seconds=manifest.helper.seconds,
                            rollback_reserve=manifest.helper.rollback_reserve,
                        ),
                        writer=lease,
                    )

    def move(self, *transitions: Transition) -> TransactionRecord:
        manifest = InstallationManifest.load(self.root)
        with TransactionJournal(self.root, self.database, manifest.journal) as journal:
            with journal.locked() as control:
                record = control.read()
                assert record is not None
                for transition in transitions:
                    record = control.advance(record.position, transition)
                return record

    def target(self) -> TransactionRecord:
        record = self.move(
            Transition(phase="draining"),
            Transition(phase="activating", intent="stop_base"),
            Transition(phase="activating", intent="install_target"),
            Transition(phase="activating", intent="start_target"),
        )
        self.service.config_path.write_bytes(TARGET)
        return record


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    real = tmp_path / "base/bin/python"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"never executed")
    real.chmod(0o700)
    venv = tmp_path / "venv"
    executable = venv / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(real)
    (venv / "pyvenv.cfg").write_bytes(b"home = base\n")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(real.parent.parent))
    package = tmp_path / "installed/muli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"init\n")
    monkeypatch.setattr(muli, "__file__", str(package / "__init__.py"))
    config = tmp_path / "settings.json"
    config.write_bytes(BASE)
    service = UserService(
        config,
        tmp_path / "data",
        tmp_path / "units",
        platform="linux",
        executable=str(executable),
    )
    service.path.parent.mkdir()
    service.path.write_text(service.render())
    database = service.data_dir / "muli.sqlite3"
    store = Store(database)
    main = store.main_session()["id"]
    instance = store.instance_id
    store.close()
    checked: list[tuple[Path, ...]] = []

    def verify(directories: Sequence[Path]) -> None:
        checked.append(tuple(directories))

    monkeypatch.setattr(linux_bootstrap, "verify_local_filesystems", verify)
    return Host(service, database, package, instance, main, checked)


def unlocked(host: Host) -> None:
    with TransactionJournal(host.root, host.database) as journal:
        with journal.writer_lease() as lease:
            lease.check()


async def test_current_service_has_real_lease_before_probe_and_explicit_close(host: Host) -> None:
    probe = FakeProbe(host.service)

    def before() -> None:
        with TransactionJournal(host.root, host.database) as contender:
            with pytest.raises(TransactionError, match="lock_busy"):
                contender.writer_lease().__enter__()

    probe.before = before
    session = await open_boot_session(host.service, host.database, probe=probe)
    assert session.permit is not None and session.manifest is not None
    assert session.permit.lease is session.lease
    assert session.permit.boot == INVOCATION
    assert session.permit.record is None and not session.permit.probation
    assert session.unsupported_reason is None
    assert session.journal.directory.path == host.root
    assert host.checked == [
        (
            host.service.config_path.parent,
            host.database.parent,
            host.root,
            session.journal.writer_directory.path,
        )
    ]
    session.check_paths(host.service.config_path, host.database)
    for config, database in (
        (host.service.config_path.with_name("other.json"), host.database),
        (host.service.config_path, host.database.with_name("other.db")),
    ):
        with pytest.raises(BootstrapError, match="boot_paths_changed"):
            session.check_paths(config, database)
    assert not (host.root / "installation.json").exists()
    assert not (host.root / "state.json").exists()
    session.close_after_writers()
    session.close_after_writers()
    with pytest.raises(BootstrapError, match="boot_session_closed"):
        session.check_paths(host.service.config_path, host.database)
    unlocked(host)


@pytest.mark.parametrize("failure", ["other_pid", "no_process", "zero_boot", "bad_boot", "stopped"])
async def test_foreground_keeps_lease_without_installed_identity(host: Host, failure: str) -> None:
    probe = FakeProbe(host.service)
    if failure == "other_pid":
        probe.snapshot = replace(
            probe.snapshot, unit=replace(probe.snapshot.unit, pid=os.getpid() + 1)
        )
    elif failure == "no_process":
        probe.snapshot = replace(probe.snapshot, process=None)
    elif failure in {"zero_boot", "bad_boot"}:
        invocation = "0" * 32 if failure == "zero_boot" else "not-a-boot"
        probe.snapshot = replace(
            probe.snapshot, unit=replace(probe.snapshot.unit, invocation=invocation)
        )
    else:
        probe.snapshot = replace(
            probe.snapshot, unit=replace(probe.snapshot.unit, active_state="failed")
        )
    session = await open_boot_session(host.service, host.database, probe=probe)
    try:
        assert session.permit is None and session.manifest is None
        assert session.unsupported_reason == "foreground_process"
        assert not host.checked
        with TransactionJournal(host.root, host.database) as contender:
            with pytest.raises(TransactionError, match="lock_busy"):
                contender.writer_lease().__enter__()
    finally:
        session.close_after_writers()


async def test_database_alias_and_other_root_still_share_one_writer_lease(host: Host) -> None:
    first = await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    other = UserService(
        host.service.config_path,
        host.service.data_dir.with_name("other-data"),
        host.service.path.parent,
        platform="linux",
        executable=host.service.executable,
    )
    alias = host.service.config_path.with_name("database-alias")
    alias.symlink_to(host.database)
    try:
        with pytest.raises(TransactionError, match="lock_busy"):
            await open_boot_session(other, alias, probe=FakeProbe(other))
        other_database = other.data_dir / "muli.sqlite3"
        independent = await open_boot_session(other, other_database, probe=FakeProbe(other))
        try:
            first.lease.check()
            independent.lease.check()
            assert not other_database.exists()
        finally:
            independent.close_after_writers()
    finally:
        first.close_after_writers()


@pytest.mark.parametrize("error", [ObservationError("unsupported_service_layout"), TimeoutError()])
async def test_unsupported_observation_preserves_foreground_serve(
    host: Host, error: Exception
) -> None:
    probe = FakeProbe(host.service)
    probe.error = error
    session = await open_boot_session(host.service, host.database, probe=probe)
    try:
        assert session.permit is None and session.manifest is None
        assert session.unsupported_reason in {
            "unsupported_service_layout",
            "systemd_observation_timeout",
        }
        assert not host.checked
    finally:
        session.close_after_writers()


async def test_initial_missing_database_does_not_create_it(host: Host) -> None:
    host.database.unlink()
    session = await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    try:
        assert session.manifest is not None and session.permit is not None
        assert not host.database.exists()
        assert session.permit.record is None
    finally:
        session.close_after_writers()


@pytest.mark.parametrize("resolved", [False, True])
async def test_any_record_rejects_missing_database_before_probe(host: Host, resolved: bool) -> None:
    host.prepare()
    if resolved:
        host.move(Transition(phase="aborted", code="cancelled"))
    host.database.unlink()
    probe = FakeProbe(host.service)
    with pytest.raises(BootstrapError, match="bootstrap_database_missing"):
        await open_boot_session(host.service, host.database, probe=probe)
    assert probe.calls == 0
    assert not host.database.exists()
    unlocked(host)


async def test_pending_missing_manifest_is_not_synthesized(host: Host) -> None:
    host.prepare()
    (host.root / "installation.json").unlink()
    probe = FakeProbe(host.service)
    with pytest.raises(BootstrapError, match="pending_manifest_missing"):
        await open_boot_session(host.service, host.database, probe=probe)
    assert probe.calls == 0
    assert not (host.root / "installation.json").exists()
    unlocked(host)


@pytest.mark.parametrize("reason", ["foreground", "layout", "filesystem"])
async def test_pending_cannot_use_unsupported_fallback(
    host: Host, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    host.prepare()
    host.target()
    probe = FakeProbe(host.service)
    if reason == "foreground":
        probe.snapshot = replace(probe.snapshot, process=None)
    elif reason == "layout":
        probe.error = ObservationError("unsupported_service_layout")
    else:

        def reject(directories: Sequence[Path]) -> None:
            raise FilesystemError("filesystem_unsupported")

        monkeypatch.setattr(linux_bootstrap, "verify_local_filesystems", reject)

    def no_configuration(*args: object, **kwargs: object) -> None:
        pytest.fail("unsupported pending boot must not read configuration")

    monkeypatch.setattr("muli.maintenance.configuration_digest", no_configuration)
    with pytest.raises(BootstrapError, match="pending_boot_unsupported"):
        await open_boot_session(host.service, host.database, probe=probe)
    unlocked(host)


async def test_unsupported_filesystem_only_disables_transaction_capability(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(directories: Sequence[Path]) -> None:
        raise FilesystemError("filesystem_unsupported")

    monkeypatch.setattr(linux_bootstrap, "verify_local_filesystems", reject)
    session = await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    try:
        assert session.permit is None and session.manifest is None
        assert session.unsupported_reason == "filesystem_unsupported"
        session.lease.check()
    finally:
        session.close_after_writers()


@pytest.mark.parametrize("role", ["target", "base"])
async def test_pending_reuses_published_budgets_and_existing_role_rules(
    host: Host, role: str
) -> None:
    host.prepare(custom_limits=True)
    host.target()
    if role == "base":
        host.move(
            Transition(phase="verifying"),
            Transition(phase="rolling_back", intent="stop_target", code="health_failed"),
            Transition(phase="rolling_back", intent="restore_base"),
            Transition(phase="rolling_back", intent="start_base"),
        )
        host.service.config_path.write_bytes(BASE)
    original = (host.root / "installation.json").read_bytes()
    session = await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    try:
        assert session.permit is not None and session.manifest is not None
        assert session.permit.role == role and session.permit.probation
        assert session.manifest.helper.operation_timeout == 28.0
        assert session.manifest.helper.seconds == 240.0
        assert session.journal.limits.max_record_bytes == 65_536
        assert session.manifest.package_limits.entries == 32
        assert (host.root / "installation.json").read_bytes() == original
    finally:
        session.close_after_writers()


@pytest.mark.parametrize(
    "phase", ["prepared", "draining", "stop_base", "install_target", "recovery_required"]
)
async def test_existing_boot_rules_reject_unpermitted_phase(host: Host, phase: str) -> None:
    host.prepare()
    if phase in {"draining", "stop_base", "install_target"}:
        host.move(Transition(phase="draining"))
    if phase in {"stop_base", "install_target"}:
        host.move(Transition(phase="activating", intent="stop_base"))
    if phase == "install_target":
        host.move(Transition(phase="activating", intent="install_target"))
    if phase == "recovery_required":
        host.move(Transition(phase="recovery_required", code="backend_failed"))
    with pytest.raises(BootstrapError, match="boot_not_permitted"):
        await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    unlocked(host)


@pytest.mark.parametrize(
    "change", ["package", "config_path", "data_path", "unit_path", "database_path"]
)
async def test_pending_does_not_capture_replacement_identity(host: Host, change: str) -> None:
    host.prepare()
    host.target()
    if change == "package":
        (host.package / "__init__.py").write_bytes(b"new installation\n")
    else:
        manifest = InstallationManifest.load(host.root)
        field = {
            "config_path": "config_path",
            "data_path": "data_dir",
            "unit_path": "unit_path",
            "database_path": "database_path",
        }[change]
        path = Path(getattr(manifest, field))
        changed_path = (
            path.parent / "foreign" / path.name
            if change == "unit_path"
            else path.parent / "foreign"
        )
        altered = manifest.model_copy(update={field: str(changed_path)})
        with TransactionJournal(host.root, host.database) as journal:
            journal.directory.replace("installation.json", altered.canonical())
    with pytest.raises(BootstrapError, match="pending_installation_invalid|boot_paths_changed"):
        await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    unlocked(host)


@pytest.mark.parametrize(
    "reason", ["configuration_changed", "store_identity_changed", "boot_reused"]
)
async def test_pending_preserves_existing_identity_checks(host: Host, reason: str) -> None:
    host.prepare()
    host.target()
    probe = FakeProbe(host.service)
    if reason == "configuration_changed":
        host.service.config_path.write_bytes(b"{}")
    elif reason == "store_identity_changed":
        store = Store(host.database)
        with store.db:
            store.db.execute(
                "UPDATE runtime_state SET value='foreign-store' WHERE name='store_instance_v1'"
            )
        store.close()
    else:
        probe.snapshot = replace(
            probe.snapshot, unit=replace(probe.snapshot.unit, invocation="0" * 32)
        )
        # This is a real, non-zero origin InvocationID, rather than an absent boot.
        with TransactionJournal(host.root, host.database) as journal:
            record = journal.read()
            assert record is not None
            changed = record.model_copy(
                update={"identity": record.identity.model_copy(update={"origin_boot": INVOCATION})}
            )
            journal.directory.replace("state.json", changed.model_dump_json().encode())
        probe.snapshot = replace(
            probe.snapshot, unit=replace(probe.snapshot.unit, invocation=INVOCATION)
        )
    with pytest.raises(BootstrapError, match=reason):
        await open_boot_session(host.service, host.database, probe=probe)
    unlocked(host)


async def test_resolved_changed_installation_preserves_old_evidence(host: Host) -> None:
    host.prepare()
    record = host.move(Transition(phase="aborted", code="cancelled"))
    old_manifest = (host.root / "installation.json").read_bytes()
    old_record = (host.root / "state.json").read_bytes()
    (host.package / "__init__.py").write_bytes(b"next installation\n")
    session = await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    try:
        assert session.manifest is not None and session.permit is not None
        assert session.manifest.digest != record.identity.installation
        assert session.permit.record == record and not session.permit.probation
        assert (host.root / "installation.json").read_bytes() == old_manifest
        assert (host.root / "state.json").read_bytes() == old_record
    finally:
        session.close_after_writers()


async def test_invalid_manifest_is_not_an_absent_installation(host: Host) -> None:
    host.prepare()
    with TransactionJournal(host.root, host.database) as journal:
        journal.directory.replace("installation.json", b'{"invalid":true}')
    probe = FakeProbe(host.service)
    with pytest.raises(BootstrapError, match="installation_manifest_invalid"):
        await open_boot_session(host.service, host.database, probe=probe)
    assert probe.calls == 0
    unlocked(host)


async def test_cancellation_cleans_lease_and_does_not_turn_into_fallback(host: Host) -> None:
    probe = FakeProbe(host.service)
    probe.finish = asyncio.Event()
    task = asyncio.create_task(open_boot_session(host.service, host.database, probe=probe))
    await asyncio.wait_for(probe.entered.wait(), 1)
    with TransactionJournal(host.root, host.database) as contender:
        with pytest.raises(TransactionError, match="lock_busy"):
            contender.writer_lease().__enter__()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unlocked(host)


@pytest.mark.parametrize("change", ["service_paths", "manifest"])
async def test_probe_await_cannot_adopt_changed_startup_inputs(host: Host, change: str) -> None:
    probe = FakeProbe(host.service)
    if change == "service_paths":
        original = host.service.config_path
        probe.before = lambda: setattr(
            host.service, "config_path", original.with_name("other.json")
        )
    else:

        def publish() -> None:
            manifest = host.capture()
            with TransactionJournal(host.root, host.database) as journal:
                with journal.locked() as control:
                    manifest.publish(control)

        probe.before = publish
    with pytest.raises(BootstrapError, match="boot_paths_changed|installation_manifest_changed"):
        await open_boot_session(host.service, host.database, probe=probe)
    unlocked(host)


async def test_default_probe_uses_published_action_subbudget(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.prepare(custom_limits=True)
    host.target()
    seen: list[ProbeLimits] = []

    def factory(*, limits: ProbeLimits) -> FakeProbe:
        seen.append(limits)
        return FakeProbe(host.service)

    monkeypatch.setattr(linux_bootstrap, "SystemdProbe", factory)
    session = await open_boot_session(host.service, host.database)
    try:
        assert len(seen) == 1 and seen[0].seconds == 7.0
        assert session.manifest is not None and session.manifest.helper.operation_timeout == 28.0
    finally:
        session.close_after_writers()


async def test_pending_modified_budget_cannot_replace_record_identity(host: Host) -> None:
    host.prepare(custom_limits=True)
    host.target()
    manifest = InstallationManifest.load(host.root)
    changed = manifest.model_copy(
        update={"helper": manifest.helper.model_copy(update={"operation_timeout": 24.0})}
    )
    with TransactionJournal(host.root, host.database, manifest.journal) as journal:
        journal.directory.replace("installation.json", changed.canonical())
    with pytest.raises(BootstrapError, match="foreign_installation"):
        await open_boot_session(host.service, host.database, probe=FakeProbe(host.service))
    unlocked(host)


async def test_boot_permission_uses_record_advanced_during_probe(host: Host) -> None:
    host.prepare()
    probe = FakeProbe(host.service)

    def advance() -> None:
        host.target()

    probe.before = advance
    session = await open_boot_session(host.service, host.database, probe=probe)
    try:
        assert session.permit is not None and session.permit.role == "target"
        assert session.permit.record is not None
        assert session.permit.record.intent == "start_target"
    finally:
        session.close_after_writers()


async def test_unexpected_probe_failure_releases_instead_of_downgrading(host: Host) -> None:
    probe = FakeProbe(host.service)
    probe.error = RuntimeError("fake_probe_failure")
    with pytest.raises(RuntimeError, match="fake_probe_failure"):
        await open_boot_session(host.service, host.database, probe=probe)
    unlocked(host)


def test_boot_permit_adopts_only_the_real_owned_fixed_writer_lease(
    host: Host, tmp_path: Path
) -> None:
    with TransactionJournal(host.root, host.database) as journal:
        manifest = host.capture()
        for kind in ("unowned", "journal_lock", "foreign_writer"):
            foreign: TransactionJournal | None = None
            if kind == "unowned":
                lease = journal.writer_lease()
                expected = "lock_not_owned"
            elif kind == "journal_lock":
                lease = journal.locked().__enter__()
                expected = "foreign_writer_lease"
            else:
                foreign = TransactionJournal(
                    tmp_path / "foreign-journal", tmp_path / "other.sqlite3"
                )
                lease = foreign.writer_lease().__enter__()
                expected = "foreign_writer_lease"
            try:
                with pytest.raises((TransactionError, MaintenanceError), match=expected):
                    BootPermit.acquire(
                        journal,
                        host.service.config_path,
                        boot=INVOCATION,
                        installation=manifest.digest,
                        service=manifest.service_digest,
                        lease=lease,
                    )
                assert lease.fd is None
            finally:
                lease.__exit__()
                if foreign is not None:
                    foreign.__exit__()
