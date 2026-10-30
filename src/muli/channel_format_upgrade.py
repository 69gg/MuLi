"""Stopped marker-only upgrade; never a managed cross-format transaction."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from contextlib import ExitStack, closing
from pathlib import Path

from muli.compatibility import FormatState
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    LEGACY_FORMAT_STATE,
    PRE_MEMORY_WATCH_FORMAT_STATE,
    PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE,
    PRE_PROJECT_INBOX_FORMAT_STATE,
    PRE_REPLY_MEMORY_FORMAT_STATE,
    PRE_TRANSCRIPT_MEMORY_FORMAT_STATE,
    PRE_WEBPAGE_MEMORY_FORMAT_STATE,
    PREVIOUS_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatReadLimits,
    read_optional_format_evidence,
)
from muli.managed_boot import _DatabaseAnchor
from muli.management_transaction import TransactionJournal
from muli.platforms.managed_deployment import require_unselected_without_record
from muli.store_initialization import StoreConstructionLock
from muli.store_schema import validate_existing_schema


class FormatUpgradeCloseError(FormatAdmissionError):
    """Keep the connection and cooperative leases until explicit successful close."""

    def __init__(self, owner: _UpgradeResources, operation_error: BaseException | None) -> None:
        super().__init__("store_format_upgrade_close_failed")
        self._owner = owner
        self.operation_error = operation_error

    def retry_close(self) -> None:
        """Retry resource cleanup only; never repeat the marker transaction."""
        self._owner.close(self.operation_error)


class _UpgradeResources:
    def __init__(self) -> None:
        self.stack = ExitStack()
        self.db: sqlite3.Connection | None = None

    def close(self, operation_error: BaseException | None = None) -> None:
        if self.db is not None:
            try:
                self.db.close()
            except BaseException as error:
                raise FormatUpgradeCloseError(self, operation_error) from error
            self.db = None
        self.stack.close()


def _reject_managed_sql(db: sqlite3.Connection) -> None:
    if (
        db.execute(
            "SELECT 1 FROM objects WHERE kind='management_projection' AND id='transaction-v1'"
        ).fetchone()
        is not None
        or db.execute(
            "SELECT 1 FROM objects WHERE kind='management' AND CASE "
            "WHEN json_valid(data) THEN json_extract(data,'$.action') IN ('adopt','switch') "
            "OR json_type(data,'$.transaction') IS NOT NULL ELSE 1 END LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise FormatAdmissionError("store_format_managed_upgrade_unsupported")


def _upgrade_marker(
    db: sqlite3.Connection, *, predecessors: tuple[FormatState, ...], check: Callable[[], None]
) -> bool:
    check()
    evidence = read_optional_format_evidence(db)
    if evidence is None:
        raise FormatAdmissionError("store_format_upgrade_source_unsupported")
    validate_existing_schema(db)
    _reject_managed_sql(db)
    check()
    if evidence.state == CURRENT_FORMAT_STATE:
        return False
    if evidence.state not in predecessors:
        raise FormatAdmissionError("store_format_upgrade_source_unsupported")
    db.execute("PRAGMA synchronous=FULL")
    if db.execute("PRAGMA synchronous").fetchone()[0] != 2:
        raise FormatAdmissionError("store_format_durability_unsupported")
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT value FROM runtime_state WHERE name=? AND typeof(value)='text' "
            "AND length(CAST(value AS BLOB)) BETWEEN 1 AND ?",
            (FORMAT_STATE_KEY, FormatReadLimits().marker_bytes),
        ).fetchone()
        if row is None or FormatState.from_json(row[0].encode("utf-8")) != evidence.state:
            raise FormatAdmissionError("store_format_changed")
        _reject_managed_sql(db)
        check()
        cursor = db.execute(
            "UPDATE runtime_state SET value=? WHERE name=? AND value=?",
            (CURRENT_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY, row[0]),
        )
        if cursor.rowcount != 1:
            raise FormatAdmissionError("store_format_changed")
        _reject_managed_sql(db)
        check()
        db.execute("COMMIT")
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
    check()
    after = read_optional_format_evidence(db)
    if (
        after is None
        or after.state != CURRENT_FORMAT_STATE
        or after.store_instance != evidence.store_instance
        or after.main_session != evidence.main_session
    ):
        raise FormatAdmissionError("store_format_changed")
    _reject_managed_sql(db)
    check()
    return True


def _require_darwin_capabilities() -> None:
    if (
        not all(
            hasattr(os, name)
            for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "geteuid")
        )
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise FormatAdmissionError("store_format_upgrade_platform_unsupported")
    try:
        import fcntl
    except ImportError:
        raise FormatAdmissionError("store_format_upgrade_platform_unsupported") from None
    if not all(hasattr(fcntl, name) for name in ("flock", "LOCK_EX", "LOCK_NB")):
        raise FormatAdmissionError("store_format_upgrade_platform_unsupported")


def _check_darwin(database: _DatabaseAnchor) -> None:
    database.check()
    if database.parent is None or database.fd is None:
        raise FormatAdmissionError("store_format_changed")
    directory, info = os.fstat(database.parent), os.fstat(database.fd)
    mode = stat.S_IMODE(directory.st_mode)
    if (
        directory.st_uid != os.geteuid()
        or mode & 0o700 != 0o700
        or mode & 0o022
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise FormatAdmissionError("store_format_upgrade_permissions")
    writer = "." + database.path.name + "-writer"
    try:
        os.stat(writer, dir_fd=database.parent, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
    management: int | None = None
    try:
        try:
            management = os.open(
                "management",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=database.parent,
            )
        except FileNotFoundError:
            return
        current = os.fstat(management)
        if current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) & 0o500 != 0o500:
            raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
        try:
            os.stat("transaction-v1", dir_fd=management, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
        observed = os.stat("management", dir_fd=database.parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
            raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
    except OSError:
        raise FormatAdmissionError("store_format_managed_upgrade_unsupported") from None
    finally:
        if management is not None:
            os.close(management)


def upgrade_channel_ownership_format(data_dir: Path, *, managed_root: Path | None = None) -> bool:
    """Upgrade a stopped exact predecessor; never initialize a store or start a service."""
    if sys.platform not in {"linux", "darwin"}:
        raise FormatAdmissionError("store_format_upgrade_platform_unsupported")
    if managed_root is not None:
        raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
    darwin = sys.platform == "darwin"
    if darwin:
        _require_darwin_capabilities()
    path = data_dir.absolute() / "muli.sqlite3"
    if darwin and ".." in path.parts:
        raise FormatAdmissionError("store_format_upgrade_path")
    owner = _UpgradeResources()
    try:
        database = owner.stack.enter_context(closing(_DatabaseAnchor(path)))
        checks: tuple[Callable[[], None], ...]
        predecessors: tuple[FormatState, ...]
        if darwin:
            _check_darwin(database)
            if database.parent is None:
                raise FormatAdmissionError("store_format_changed")
            construction = owner.stack.enter_context(
                StoreConstructionLock(path, existing_parent=database.parent)
            )
            if construction.database != database.path:
                raise FormatAdmissionError("store_format_changed")

            checks = (construction.check, lambda: _check_darwin(database))
            predecessors = (
                PRE_WEBPAGE_MEMORY_FORMAT_STATE,
                PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE,
                PRE_PROJECT_INBOX_FORMAT_STATE,
                PRE_REPLY_MEMORY_FORMAT_STATE,
            )
        else:
            root = path.parent / "management" / "transaction-v1"
            journal = owner.stack.enter_context(TransactionJournal(root, path))
            control = journal.locked()
            owner.stack.enter_context(control)
            writer_lease = owner.stack.enter_context(journal.writer_lease())
            construction = owner.stack.enter_context(StoreConstructionLock(path))
            if control.read_current() is not None:
                raise FormatAdmissionError("store_format_managed_upgrade_unsupported")
            try:
                require_unselected_without_record(root)
            except ValueError:
                raise FormatAdmissionError("store_format_managed_upgrade_unsupported") from None

            checks = (control.check, writer_lease.check, construction.check, database.check)
            predecessors = (
                LEGACY_FORMAT_STATE,
                PREVIOUS_FORMAT_STATE,
                PRE_MEMORY_WATCH_FORMAT_STATE,
                PRE_TRANSCRIPT_MEMORY_FORMAT_STATE,
                PRE_WEBPAGE_MEMORY_FORMAT_STATE,
                PRE_MEMORY_WATCH_ORIGINS_FORMAT_STATE,
                PRE_PROJECT_INBOX_FORMAT_STATE,
                PRE_REPLY_MEMORY_FORMAT_STATE,
            )

        def check() -> None:
            for verify in checks:
                verify()

        check()
        owner.db = sqlite3.connect(
            path.as_uri() + "?mode=rw&cache=private",
            uri=True,
            timeout=0,
            isolation_level="DEFERRED",
        )
        result = _upgrade_marker(owner.db, predecessors=predecessors, check=check)
    except BaseException as error:
        owner.close(error)
        raise
    else:
        owner.close()
        return result
