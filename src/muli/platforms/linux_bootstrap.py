"""Acquire the Linux DB writer before any ConfigFile or Store construction.

This observes a running service only. It does not publish installations, prepare
transactions, open Store or start a helper. The host closes the returned session
explicitly after every writer has stopped.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from muli.maintenance import BootPermit, MaintenanceError
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import FixedLock, TransactionJournal, TransactionRecord
from muli.platforms.filesystem_identity import FilesystemError, verify_local_filesystems
from muli.platforms.installation import HelperLimits, InstallationManifest, _read_file
from muli.platforms.linux_transaction import (
    ObservationError,
    ProbeLimits,
    ServiceSnapshot,
    SystemdProbe,
)
from muli.service import UserService

if TYPE_CHECKING:
    from muli.managed_boot import ManagedBootPermit
    from muli.platforms.managed_bootstrap import ManagedServiceProbe


class BootstrapError(ValueError):
    """Only fixed codes, never configuration or observation contents."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        self.code, self.reason = code, reason
        super().__init__(code)


class ServiceProbe(Protocol):
    async def read(self, service: UserService) -> ServiceSnapshot: ...


@dataclass(slots=True)
class BootSession:
    journal: TransactionJournal
    lease: FixedLock
    config_path: Path
    permit: BootPermit | ManagedBootPermit | None = None
    manifest: InstallationManifest | None = None
    unsupported_reason: str | None = None
    _closed: bool = False

    def check_paths(self, config_path: Path, database: Path) -> None:
        if self._closed:
            raise BootstrapError("boot_session_closed")
        self.lease.check()
        if (
            config_path.absolute() != self.config_path
            or database.resolve() != self.journal.database
        ):
            raise BootstrapError("boot_paths_changed")

    def close_after_writers(self) -> None:
        if self._closed:
            return
        if self.permit is not None:
            # A managed permit may still own an unclosed SQLite connection.
            # Its refusal must retain both lease and journal descriptors.
            self.permit.close_after_writers()
        else:
            self.lease.__exit__()
        self.journal.__exit__()
        self._closed = True


def _manifest(root: Path) -> InstallationManifest | None:
    try:
        return InstallationManifest.load(root)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise BootstrapError("installation_manifest_invalid") from None


def _database(journal: TransactionJournal, record: TransactionRecord | None) -> None:
    try:
        _read_file(journal.database, 0, contents=False, private_owner=True)
    except FileNotFoundError:
        if record is not None:
            raise BootstrapError("bootstrap_database_missing") from None
    except (OSError, ValueError):
        raise BootstrapError("bootstrap_database_unavailable") from None


def _paths(manifest: InstallationManifest, service: UserService, database: Path) -> None:
    if (
        manifest.config_path != str(service.config_path.absolute())
        or manifest.data_dir != str(service.data_dir.absolute())
        or manifest.database_path != str(database)
        or manifest.unit_path != str(service.path.absolute())
    ):
        raise BootstrapError("boot_paths_changed")


def _current_process(snapshot: ServiceSnapshot) -> bool:
    unit, process = snapshot.unit, snapshot.process
    return (
        process is not None
        and unit.pid == os.getpid()
        and process.pid == os.getpid()
        and unit.active_state in {"active", "activating"}
        and unit.sub_state in {"running", "start", "start-post"}
        and re.fullmatch(r"[a-f0-9]{32}", unit.invocation) is not None
        and unit.invocation != "0" * 32
    )


async def open_boot_session(
    service: UserService,
    database: Path,
    *,
    probe: ServiceProbe | None = None,
    managed_root: Path | None = None,
    managed_probe: ManagedServiceProbe | None = None,
) -> BootSession:
    if sys.platform != "linux":
        raise BootstrapError("platform_unsupported")
    root = (
        managed_root.absolute()
        if managed_root is not None
        else service.data_dir.absolute() / "management" / "transaction-v1"
    )
    try:
        journal = TransactionJournal(root, database, create=managed_root is None)
    except (OSError, ValueError):
        if managed_root is not None:
            raise BootstrapError("managed_journal_unavailable") from None
        raise
    try:
        lease = journal.writer_lease().__enter__()
    except BaseException:
        journal.__exit__()
        raise
    session = BootSession(journal, lease, service.config_path.absolute())
    paths = (service.config_path, service.data_dir, service.path, service.executable)
    try:
        # The one journal selects the protocol before any legacy manifest read.
        # A rollback launched by the raw V1 unit has no --managed-root flag.
        current = journal.read_current()
        if isinstance(current, TransactionRecordV2):
            from muli.platforms.managed_bootstrap import admit_managed_boot

            return await admit_managed_boot(session, service, current, probe=managed_probe)
        if managed_root is not None:
            raise BootstrapError("managed_record_required")
        if current is None:
            from muli.platforms.managed_deployment import require_unselected_without_record

            require_unselected_without_record(journal.directory.path)
        published = _manifest(root)
        if published is not None:
            journal.limits = published.journal
        record = journal.read()
        _database(journal, record)
        if record is not None and not record.resolved and published is None:
            raise BootstrapError("pending_manifest_missing")
        helper = published.helper if published else HelperLimits()
        observer = (
            probe
            if probe is not None
            else SystemdProbe(limits=ProbeLimits(seconds=helper.operation_timeout / 4))
        )
        snapshot: ServiceSnapshot | None = None
        try:
            async with asyncio.timeout(helper.operation_timeout / 4):
                snapshot = await observer.read(service)
            if not _current_process(snapshot):
                session.unsupported_reason = "foreground_process"
        except TimeoutError:
            session.unsupported_reason = "systemd_observation_timeout"
        except ObservationError as exc:
            session.unsupported_reason = str(exc)
        if paths != (service.config_path, service.data_dir, service.path, service.executable):
            raise BootstrapError("boot_paths_changed")
        record = journal.read()
        _database(journal, record)
        if _manifest(root) != published:
            raise BootstrapError("installation_manifest_changed")
        pending = record is not None and not record.resolved
        if pending and published is None:
            raise BootstrapError("pending_manifest_missing")
        if session.unsupported_reason is None:
            try:
                verify_local_filesystems(
                    (
                        service.config_path.absolute().parent,
                        journal.database.parent,
                        journal.directory.path,
                        journal.writer_directory.path,
                    )
                )
            except FilesystemError as exc:
                session.unsupported_reason = exc.code
        if session.unsupported_reason is not None:
            if pending:
                raise BootstrapError("pending_boot_unsupported", session.unsupported_reason)
            return session
        if pending:
            assert published is not None
            _paths(published, service, journal.database)
            try:
                published.verify()
            except (OSError, ValueError):
                raise BootstrapError("pending_installation_invalid") from None
            manifest = published
        else:
            try:
                manifest = InstallationManifest.capture(
                    service,
                    database,
                    helper=published.helper if published else None,
                    journal=journal.limits,
                    package_limits=published.package_limits if published else None,
                    require_database=record is not None,
                )
            except (OSError, ValueError):
                session.unsupported_reason = "installation_unavailable"
                return session
        assert snapshot is not None
        try:
            permit = BootPermit.acquire(
                journal,
                service.config_path,
                boot=snapshot.unit.invocation,
                installation=manifest.digest,
                service=manifest.service_digest,
                lease=lease,
            )
        except MaintenanceError as exc:
            raise BootstrapError(exc.code) from None
        session.permit, session.manifest = permit, manifest
        return session
    except BaseException:
        session.close_after_writers()
        raise
