"""Durably project a resolved recovery journal into existing management objects.

The writer lease and journal remain authoritative. No service effect, tool
receipt repair, configuration mutation or implicit main-session creation occurs.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from muli.maintenance import BootPermit
from muli.managed_transaction import ProjectControllerTransferRecord, TransactionRecordV2
from muli.management_transaction import ProjectionReceipt, Role, TransactionRecord, _object
from muli.platforms.transaction_files import _open_directory
from muli.store import Store, now

if TYPE_CHECKING:
    from muli.managed_boot import ManagedBootPermit

_RECEIPT_KIND = "management_projection"
_RECEIPT_ID = "transaction-v1"
_RECEIPT_BYTES = 4096
_RECEIPT_FIELDS = {"id", "generation", "revision", "store_instance"}


class ProjectionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _StoreIdentity:
    path: Path
    instance: str
    main: str


def _database_identity(db: sqlite3.Connection) -> _StoreIdentity:
    if sys.platform != "linux":
        raise ProjectionError("projection_platform_unsupported")
    if db.in_transaction:
        raise ProjectionError("projection_outer_transaction")
    if db.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL or db.isolation_level is None:
        raise ProjectionError("projection_transaction_mode_unsupported")
    if db.execute("PRAGMA main.journal_mode").fetchone()[0] != "wal" or db.execute(
        "PRAGMA main.synchronous"
    ).fetchone()[0] not in {2, 3}:
        raise ProjectionError("projection_durability_unsupported")
    with closing(db.execute("PRAGMA database_list")) as cursor:
        rows = cursor.fetchmany(3)
    if len(rows) > 2 or any(row[1] not in {"main", "temp"} for row in rows):
        raise ProjectionError("projection_database_layout_unsupported")
    databases = [row[2] for row in rows if row[1] == "main"]
    if len(databases) != 1 or not databases[0] or not Path(databases[0]).is_absolute():
        raise ProjectionError("projection_database_path_changed")
    path = Path(databases[0])
    directory = _open_directory(path.parent)
    try:
        value = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid():
            raise ProjectionError("projection_database_path_changed")
    finally:
        os.close(directory)
    values = dict(
        db.execute(
            "SELECT name,CASE WHEN typeof(value)='text' "
            "AND length(CAST(value AS BLOB)) BETWEEN 1 AND 96 THEN value END "
            "FROM runtime_state WHERE name IN ('store_instance_v1','main_session')"
        )
    )
    instance, main = values.get("store_instance_v1"), values.get("main_session")
    if not isinstance(instance, str) or not isinstance(main, str):
        raise ProjectionError("projection_store_identity_changed")
    if (
        db.execute(
            "SELECT 1 FROM objects WHERE kind='session' AND id=? "
            "AND json_extract(data,'$.id')=? AND json_extract(data,'$.kind')='main' "
            "AND json_extract(data,'$.principal')='owner'",
            (main, main),
        ).fetchone()
        is None
    ):
        raise ProjectionError("projection_main_identity_changed")
    return _StoreIdentity(path, instance, main)


def _connection_identity(store: Store) -> _StoreIdentity:
    return _database_identity(store.db)


def _read_object(
    db: sqlite3.Connection, kind: str, identifier: str, limit: int
) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT length(CAST(data AS BLOB)),substr(CAST(data AS BLOB),1,?) "
        "FROM objects WHERE kind=? AND id=?",
        (limit + 1, kind, identifier),
    ).fetchone()
    if row is None:
        return None
    if type(row[0]) is not int or row[0] > limit or not isinstance(row[1], bytes):
        raise ProjectionError("projection_object_budget")
    try:
        return _object(row[1])
    except ValueError:
        raise ProjectionError("projection_object_invalid") from None


def _read_projection(db: sqlite3.Connection, identity: _StoreIdentity) -> ProjectionReceipt | None:
    value = _read_object(db, _RECEIPT_KIND, _RECEIPT_ID, _RECEIPT_BYTES)
    if value is None:
        return None
    if set(value) != _RECEIPT_FIELDS or value["id"] != _RECEIPT_ID:
        raise ProjectionError("projection_receipt_invalid")
    try:
        receipt = ProjectionReceipt.model_validate(
            {key: item for key, item in value.items() if key != "id"}
        )
    except ValueError:
        raise ProjectionError("projection_receipt_invalid") from None
    if receipt.store_instance != identity.instance:
        raise ProjectionError("projection_store_identity_changed")
    return receipt


def read_projection(store: Store) -> ProjectionReceipt | None:
    """Read the fixed durable receipt without creating or committing any state."""
    return read_projection_connection(store.db)


def read_projection_connection(db: sqlite3.Connection) -> ProjectionReceipt | None:
    """Read the same fixed receipt from a host-owned existing connection.

    The caller owns the connection and authenticates its file/lease binding.
    This checks the existing identity/durability contract without constructing
    a Store, creating a main session, or opening or closing a connection.
    """
    return _read_projection(db, _database_identity(db))


def _check_lease(permit: BootPermit | ManagedBootPermit) -> None:
    lease = permit.lease
    lease.check()
    expected, actual = os.fstat(permit.journal.writer_directory.fd), os.fstat(lease.directory.fd)
    if lease.name != "lease" or (expected.st_dev, expected.st_ino) != (
        actual.st_dev,
        actual.st_ino,
    ):
        raise ProjectionError("projection_writer_lease_required")


def _check_identity(
    permit: BootPermit | ManagedBootPermit,
    identity: _StoreIdentity,
    record: TransactionRecord | TransactionRecordV2 | ProjectControllerTransferRecord,
) -> None:
    if identity.path != permit.journal.database or (identity.instance, identity.main) != (
        record.identity.store_instance,
        record.identity.main_session,
    ):
        raise ProjectionError("projection_store_identity_changed")


def project_final(
    permit: BootPermit | ManagedBootPermit | None,
    store: Store,
    *,
    verify_binding: Callable[
        [TransactionRecordV2 | ProjectControllerTransferRecord, Role | None], None
    ]
    | None = None,
) -> ProjectionReceipt | None:
    """Atomically project one stable resolved record; repeated exact calls are reads."""
    if permit is None:
        return None
    _check_lease(permit)
    identity = _connection_identity(store)
    if identity.path != permit.journal.database:
        raise ProjectionError("projection_store_identity_changed")
    record = permit.journal.read_current()
    if record is None:
        return None
    _check_identity(permit, identity, record)
    # A pending record is an observation, never a final decision or permission.
    # In particular the new probationary host keeps the old durable receipt.
    if not record.resolved:
        return None
    if isinstance(record, ProjectControllerTransferRecord):
        if isinstance(permit, BootPermit):
            raise ProjectionError("project_controller_transfer_unsupported")
        if permit.boot == record.identity.origin_boot:
            raise ProjectionError("projection_installation_changed")
    # A lock-free read of a visible rename is not its durability receipt. The
    # resolved record must remain exact across the directory sync barrier.
    os.fsync(permit.journal.directory.fd)
    if permit.journal.read_current() != record:
        raise ProjectionError("projection_journal_changed")
    _check_lease(permit)
    receipt = ProjectionReceipt(**record.position.model_dump(), store_instance=identity.instance)
    previous = _read_projection(store.db, identity)
    if previous == receipt:
        return receipt
    if previous is not None and previous.generation == receipt.generation:
        raise ProjectionError("projection_revision_conflict")
    if isinstance(record, (TransactionRecordV2, ProjectControllerTransferRecord)):
        if verify_binding is None:
            raise ProjectionError("projection_binding_required")
        role: Role | None = (
            None
            if permit.boot == record.identity.origin_boot
            else ("target" if record.phase == "committed" else "base")
        )
        verify_binding(record, role)
    elif not isinstance(permit, BootPermit) or (permit.installation, permit.service) != (
        record.identity.installation,
        record.identity.service,
    ):
        raise ProjectionError("projection_installation_changed")
    plan = _read_object(
        store.db, "management", record.request_id, permit.journal.limits.max_record_bytes
    )
    if (
        plan is None
        or plan.get("id") != record.request_id
        or plan.get("action") != record.action
        or not isinstance(plan.get("transaction"), dict)
        or plan["transaction"].get("generation") != record.generation
    ):
        raise ProjectionError("projection_plan_mismatch")
    if record.phase == "committed":
        status = "completed"
    elif record.phase == "aborted" and record.code == "cancelled":
        status = "cancelled"
    else:
        status = "failed"
    final = {
        **plan,
        "status": status,
        "completed_at": now(),
        "transaction": {**plan["transaction"], **record.public()},
        "result": {
            "phase": record.phase,
            "activation": record.activation,
            "code": record.code,
            "failure_code": record.failure_code,
            "restored_previous": record.phase == "rolled_back",
        },
    }
    final.pop("error", None)
    if status == "failed":
        final["error"] = record.failure_code or record.code or "transaction_failed"
    _check_lease(permit)
    if permit.journal.read_current() != record:
        raise ProjectionError("projection_journal_changed")
    store.event(
        "management.completed",
        final,
        objects=[
            ("management", final),
            (_RECEIPT_KIND, {"id": _RECEIPT_ID, **receipt.model_dump()}),
        ],
    )
    return receipt
