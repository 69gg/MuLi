"""Fresh protocol-2 admission before a host creates any application writers.

The caller authenticates the actual systemd boot and transfers its writer lease.
EntryPlan is routing evidence, not authority. No service, migration, backup or
normal Runtime startup is performed here.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from pydantic import TypeAdapter

from muli.compatibility import assess_compatibility
from muli.config import ConfigFile
from muli.format_evidence import FormatEvidence, read_format_evidence
from muli.managed_transaction import (
    ProjectControllerTransferRecord,
    TransactionRecordV2,
    parse_managed_record,
    raw_snapshot,
)
from muli.management_projection import read_projection_connection
from muli.management_transaction import (
    FixedLock,
    Identifier,
    ProjectionReceipt,
    Role,
    TransactionJournal,
    _snapshot,
)
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.managed_deployment import (
    _LAUNCHER_BYTES,
    DeploymentAnchor,
    DeploymentStore,
    EntryPlan,
    SelectorStore,
    SlotIdentity,
    verify_current_slot,
    verify_slot_files,
)
from muli.platforms.managed_launcher import fingerprint
from muli.platforms.managed_probe import transfer_origin_material
from muli.platforms.transaction_files import ConfigurationFile, _open_directory
from muli.store import Store
from muli.store_schema import ExistingStoreError

_BOOT = TypeAdapter(Identifier)
_MUTABLE_RECORD_FIELDS = {
    "revision",
    "phase",
    "intent",
    "health",
    "activation",
    "code",
    "failure_code",
}


def _transfer_identity_matches(
    original: TransactionRecordV2 | ProjectControllerTransferRecord,
    current: TransactionRecordV2 | ProjectControllerTransferRecord,
) -> bool:
    """Bind the live Transfer, while leaving later ordinary generations alone."""
    if not isinstance(current, ProjectControllerTransferRecord) and not (
        isinstance(original, ProjectControllerTransferRecord)
        and original.generation == current.generation
    ):
        return True
    return (
        isinstance(original, ProjectControllerTransferRecord)
        and isinstance(current, ProjectControllerTransferRecord)
        and original.model_dump(exclude=_MUTABLE_RECORD_FIELDS)
        == current.model_dump(exclude=_MUTABLE_RECORD_FIELDS)
    )


class ManagedBootError(ValueError):
    """A fixed code. A failed connection close retains its owner until exit."""

    def __init__(self, code: str) -> None:
        self.code = code
        self._owner: ManagedBootPermit | None = None
        super().__init__(code)


def _inode(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _DatabaseAnchor:
    """Keep the actual existing DB inode, without hashing or reading its pages."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.parent: int | None = _open_directory(path.parent)
        self.fd: int | None = None
        try:
            self.fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self.parent,
            )
            self.identity = _inode(os.fstat(self.fd))
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        if self.fd is None or self.parent is None:
            raise ManagedBootError("managed_database_closed")
        current_parent = _open_directory(self.path.parent)
        try:
            info = os.fstat(self.fd)
            current = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
            if (
                _inode(os.fstat(current_parent)) != _inode(os.fstat(self.parent))
                or _inode(info) != self.identity
                or _inode(current) != self.identity
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or not stat.S_ISREG(current.st_mode)
            ):
                raise ManagedBootError("managed_database_changed")
        finally:
            os.close(current_parent)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent is not None:
            os.close(self.parent)
            self.parent = None


@dataclass(frozen=True, slots=True)
class _Observation:
    record: TransactionRecordV2 | ProjectControllerTransferRecord = field(repr=False)
    role: Role
    anchor: DeploymentAnchor = field(repr=False)
    slot: SlotIdentity = field(repr=False)
    configuration: bytes = field(repr=False)


class ManagedBootPermit:
    """Own the adopted DB lease/identity until the host certifies writers closed.

    Journal descriptors remain caller-owned. A successful open_store transfers
    only the Store to the caller; this object never closes an application writer.
    """

    def __init__(
        self, journal: TransactionJournal, boot: str, plan: EntryPlan, lease: FixedLock
    ) -> None:
        self.journal, self.boot, self._plan, self.lease = journal, boot, plan, lease
        self._database: _DatabaseAnchor | None = None
        self._opened = self._closed = self._cleanup_failed = False
        self._unclosed: Store | sqlite3.Connection | None = None
        self._configuration_bytes: bytes | None = None
        self.record: TransactionRecordV2 | ProjectControllerTransferRecord
        self.anchor: DeploymentAnchor
        self.role: Role
        self.config: ConfigFile
        self.config_path: Path
        self.installation: str
        self.probation: bool

    @classmethod
    def acquire(
        cls, journal: TransactionJournal, *, boot: str, plan: EntryPlan, lease: FixedLock
    ) -> Self:
        permit = cls(journal, boot, plan, lease)
        try:
            _BOOT.validate_python(boot, strict=True)
            permit._check_lease()
            observation = permit._observe()
            permit._database = _DatabaseAnchor(journal.database)
            db = sqlite3.connect(
                journal.database.as_uri() + "?mode=ro&cache=private",
                uri=True,
                timeout=0,
                isolation_level="DEFERRED",
            )
            try:
                permit._admit_connection(db, observation)
            finally:
                try:
                    db.close()
                except BaseException:
                    permit._retain(db)
                    raise permit._cleanup_error() from None
            permit._accept(observation)
            return permit
        except BaseException as error:
            if not permit._cleanup_failed:
                permit.close_after_writers()
            if isinstance(error, ManagedBootError):
                raise
            if isinstance(error, (OSError, ValueError, sqlite3.Error)):
                raise ManagedBootError("managed_boot_verification_failed") from None
            raise

    def _check_lease(self) -> None:
        if self._closed:
            raise ManagedBootError("managed_boot_closed")
        self.journal.directory.check()
        self.journal.writer_directory.check()
        self.lease.check()
        if self.lease.name != "lease" or _inode(os.fstat(self.lease.directory.fd)) != _inode(
            os.fstat(self.journal.writer_directory.fd)
        ):
            raise ManagedBootError("managed_writer_lease_required")

    def _record(self) -> tuple[TransactionRecordV2 | ProjectControllerTransferRecord, Role]:
        self._check_lease()
        raw = self.journal.directory.read("state.json", self.journal.limits.max_record_bytes)
        # A preceding controller may have died after rename but before fsync.
        # Confirm the visible state without contending for its journal lock.
        os.fsync(self.journal.directory.fd)
        if self.journal.directory.read("state.json", self.journal.limits.max_record_bytes) != raw:
            raise ManagedBootError("managed_record_changed")
        record = parse_managed_record(raw, limits=self.journal.limits)
        if isinstance(record, ProjectControllerTransferRecord) and (
            self.boot == record.identity.origin_boot
        ):
            raise ManagedBootError("managed_boot_not_permitted")
        role: Role | None = (
            ("target" if record.phase == "committed" else "base")
            if record.resolved
            else record.boot_role
        )
        plan = self._plan
        if role is None or (not record.resolved and self.boot == record.identity.origin_boot):
            raise ManagedBootError("managed_boot_not_permitted")
        if (
            plan.generation != record.generation
            or plan.deployment != record.identity.deployment
            or plan.role != role
            or plan.installation != record.binding(role).installation
            or type(plan.revision) is not int
            or plan.revision < 1
            or plan.revision > record.revision
            or (
                plan.revision == record.revision
                and plan.state_digest != hashlib.sha256(raw).hexdigest()
            )
        ):
            raise ManagedBootError("managed_entry_changed")
        return record, role

    def _observe(self) -> _Observation:
        record, role = self._record()
        binding = record.binding(role)
        with DeploymentStore(self.journal.directory.path, self.journal.limits) as source:
            anchor = source.read_anchor(record.identity.deployment)
            selected = source.read_slot(binding.installation)
            # The fixed controller must remain usable even when another role is selected.
            verify_slot_files(source.read_slot(anchor.controller_slot))
        if (
            anchor.database_path != str(self.journal.database)
            or anchor.journal_root != str(self.journal.directory.path)
            or anchor.journal != self.journal.limits
            or fingerprint(Path(anchor.launcher.path), _LAUNCHER_BYTES)
            != anchor.launcher.model_dump(mode="json")
        ):
            raise ManagedBootError("managed_anchor_changed")
        if selected.compatibility_digest != binding.compatibility or (
            binding.entry == "managed" and binding.unit != anchor.managed_unit
        ):
            raise ManagedBootError("managed_binding_changed")
        if isinstance(record, ProjectControllerTransferRecord):
            transfer_origin_material(self.journal, record, anchor)
        verify_current_slot(selected)
        plan = self._plan
        argv = (
            selected.lexical_python,
            "-I",
            "-m",
            "muli",
            "--config",
            anchor.config_path,
            "--data-dir",
            anchor.data_dir,
            "--managed-root",
            anchor.journal_root,
            "serve",
        )
        if str(plan.python) != selected.lexical_python or plan.argv != argv:
            raise ManagedBootError("managed_entry_changed")
        verify_local_filesystems(
            (
                Path(anchor.config_path).parent,
                self.journal.database.parent,
                self.journal.directory.path,
                self.journal.writer_directory.path,
                Path(anchor.unit_path).parent,
            )
        )
        limit = self.journal.limits.max_snapshot_bytes
        with ConfigurationFile(Path(anchor.config_path), limit) as file:
            configuration = file.read().data
        with ConfigurationFile(Path(anchor.unit_path), limit) as file:
            unit = file.read().data
        with SelectorStore(self.journal.directory.path) as file:
            selector = file.read()
        if (
            (not record.resolved and _snapshot(configuration, limit) != binding.configuration)
            or raw_snapshot(unit, limit) != binding.unit
            or (selector.reference if selector else None) != binding.selector
            or (
                selector is not None
                and (
                    selector.value.deployment != record.identity.deployment
                    or selector.value.installation != selected.digest
                )
            )
        ):
            raise ManagedBootError("managed_files_changed")
        # Parse only the bytes checked above, never ConfigFile(path)'s default path.
        ConfigFile.from_bytes(Path(anchor.config_path), configuration)
        return _Observation(record, role, anchor, selected, configuration)

    def _admit_connection(self, db: sqlite3.Connection, observation: _Observation) -> None:
        evidence = read_format_evidence(db)
        self._format(evidence, observation)
        if (
            db.in_transaction
            or db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
            or db.isolation_level is None
        ):
            raise ManagedBootError("managed_connection_unsupported")
        with closing(db.execute("PRAGMA database_list")) as cursor:
            databases = [tuple(row) for row in cursor.fetchmany(3)]
        main = [row[2] for row in databases if row[1] == "main"]
        if (
            len(databases) > 2
            or any(row[1] not in {"main", "temp"} for row in databases)
            or main != [str(self.journal.database)]
        ):
            raise ManagedBootError("managed_database_changed")
        if db.execute("PRAGMA main.journal_mode").fetchone()[0] != "wal" or db.execute(
            "PRAGMA main.synchronous"
        ).fetchone()[0] not in {2, 3}:
            raise ManagedBootError("managed_durability_unsupported")
        self._configuration(db, observation)
        self.check_paths(Path(observation.anchor.config_path), self.journal.database)
        fresh = self._observe()
        if (
            fresh.role != observation.role
            or fresh.configuration != observation.configuration
            or fresh.record.revision < observation.record.revision
            or fresh.record.model_dump(exclude=_MUTABLE_RECORD_FIELDS)
            != observation.record.model_dump(exclude=_MUTABLE_RECORD_FIELDS)
        ):
            raise ManagedBootError("managed_record_changed")
        self._configuration(db, fresh)
        self.check_paths(Path(fresh.anchor.config_path), self.journal.database)
        # This synchronous read may observe a legal phase/revision advance.
        self.record = fresh.record

    def _configuration(self, db: sqlite3.Connection, observation: _Observation) -> None:
        record = observation.record
        if (
            _snapshot(observation.configuration, self.journal.limits.max_snapshot_bytes)
            == record.binding(observation.role).configuration
        ):
            return
        if not record.resolved or read_projection_connection(db) != ProjectionReceipt(
            **record.position.model_dump(), store_instance=record.identity.store_instance
        ):
            raise ManagedBootError("managed_configuration_unprojected")

    def _format(self, evidence: FormatEvidence, observation: _Observation) -> None:
        record = observation.record
        if (
            evidence.state != record.observed_format
            or evidence.store_instance != record.identity.store_instance
            or evidence.main_session != record.identity.main_session
            or not assess_compatibility(
                observation.slot.compatibility,
                observation.slot.compatibility,
                evidence.state,
                handoff_protocol=2,
            ).format_compatible
        ):
            raise ManagedBootError("managed_format_changed")

    def _accept(self, observation: _Observation) -> None:
        self.anchor, self.role = observation.anchor, observation.role
        self.config_path = Path(self.anchor.config_path)
        self.config = ConfigFile.from_bytes(self.config_path, observation.configuration)
        self._configuration_bytes = observation.configuration
        self.installation = observation.slot.digest
        self.probation = not self.record.resolved

    def check_paths(self, config_path: Path, database: Path) -> None:
        self._check_lease()
        if database.absolute() != self.journal.database:
            raise ManagedBootError("managed_paths_changed")
        if hasattr(self, "config_path") and config_path.absolute() != self.config_path:
            raise ManagedBootError("managed_paths_changed")
        if self._database is not None:
            self._database.check()

    def open_store(self) -> Store:
        if self._opened:
            raise ManagedBootError("managed_store_already_opened")
        self._opened = True
        try:
            observation = self._observe()
            if isinstance(self.record, ProjectControllerTransferRecord) and (
                not isinstance(observation.record, ProjectControllerTransferRecord)
                or observation.record.model_dump(exclude=_MUTABLE_RECORD_FIELDS)
                != self.record.model_dump(exclude=_MUTABLE_RECORD_FIELDS)
            ):
                raise ManagedBootError("managed_record_changed")
            if observation.configuration != self._configuration_bytes:
                raise ManagedBootError("managed_configuration_changed")
            self.check_paths(Path(observation.anchor.config_path), self.journal.database)
        except ManagedBootError:
            raise
        except (OSError, ValueError, sqlite3.Error):
            raise ManagedBootError("managed_boot_verification_failed") from None
        try:
            store = Store.open_existing(self.journal.database, durable=True)
        except ExistingStoreError:
            # This factory's fixed error is emitted only after its own DB is closed.
            raise ManagedBootError("managed_store_unavailable") from None
        except BaseException as error:
            # Unknown factory failure cannot certify its internal connection close.
            self._cleanup_failed = True
            if isinstance(error, Exception):
                raise self._cleanup_error() from None
            raise
        try:
            self._admit_connection(store.db, observation)
            self._accept(observation)
            return store
        except BaseException as error:
            try:
                store.close()
            except BaseException:
                self._retain(store)
                raise self._cleanup_error() from None
            if isinstance(error, ManagedBootError):
                raise
            if isinstance(error, (OSError, ValueError, sqlite3.Error)):
                raise ManagedBootError("managed_store_verification_failed") from None
            raise

    def _retain(self, owner: Store | sqlite3.Connection) -> None:
        self._cleanup_failed, self._unclosed = True, owner

    def _cleanup_error(self) -> ManagedBootError:
        error = ManagedBootError("managed_boot_cleanup_failed")
        error._owner = self
        return error

    def close_after_writers(self) -> None:
        if self._cleanup_failed:
            raise self._cleanup_error()
        if self._closed:
            return
        if self._database is not None:
            self._database.close()
        self.lease.__exit__()
        self._closed = True
