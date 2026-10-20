"""Recovery diagnostics and explicit takeover without application construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from muli.managed_cli import check_cli_paths, managed_record
from muli.management_transaction import Role, TransactionError, TransactionJournal
from muli.platforms.installation import InstallationManifest
from muli.platforms.transaction_files import ConfigurationFile
from muli.service import UserService


def _published(root: Path) -> InstallationManifest | None:
    try:
        return InstallationManifest.load(root)
    except FileNotFoundError:
        return None


def _check_paths(
    service: UserService, journal: TransactionJournal, manifest: InstallationManifest
) -> None:
    if (
        manifest.config_path != str(service.config_path.absolute())
        or manifest.data_dir != str(service.data_dir.absolute())
        or manifest.database_path != str(journal.database)
        or manifest.unit_path != str(service.path.absolute())
    ):
        raise TransactionError("boot_paths_changed")


def _check_unit(service: UserService, *, installing: bool) -> None:
    # The unit name is shared across data roots. Never choose its transaction
    # authority from caller paths until its actual standard entry matches them.
    expected = service.render().encode("utf-8")
    try:
        with ConfigurationFile(service.path, len(expected)) as source:
            actual = source.read().data
    except FileNotFoundError:
        if installing:
            return
        raise TransactionError("service_unit_missing") from None
    except (OSError, ValueError):
        raise TransactionError("service_identity_unverified") from None
    if actual != expected:
        raise TransactionError("service_identity_changed")


async def management_action(
    service: UserService,
    action: Literal["status", "recover"],
    *,
    managed_root: Path | None = None,
) -> dict[str, Any]:
    if service.platform != "linux":
        return {"supported": False, "reason": "platform_unsupported", "transaction": None}
    root = managed_root or service.data_dir.absolute() / "management" / "transaction-v1"
    managed = managed_record(root, required=managed_root is not None)
    if managed is not None:
        check_cli_paths(root, managed, service.config_path, service.data_dir)
        result: dict[str, Any] = {"transaction": managed.public()}
        if action == "status" or managed.resolved or managed.phase == "recovery_required":
            return result
        # Importable selected code only routes recovery; the retained controller
        # verifies its own process and starts the separately bounded helper.
        from muli.platforms.managed_launcher import execute, resolve_entry

        execute(resolve_entry(root.absolute(), purpose="recover"))
    try:
        root.lstat()
    except FileNotFoundError:
        return {"supported": False, "reason": "managed_boot_required", "transaction": None}
    manifest = _published(root)
    with TransactionJournal(
        root,
        service.data_dir / "muli.sqlite3",
        manifest.journal if manifest else None,
        create=False,
    ) as journal:
        if manifest is not None:
            _check_paths(service, journal, manifest)
        record = journal.read()
        result = {"transaction": record.public() if record is not None else None}
        if (
            action == "status"
            or record is None
            or record.resolved
            or record.phase == "recovery_required"
        ):
            return result
        if manifest is None:
            raise TransactionError("pending_manifest_missing")
        try:
            with journal.locked() as control:
                from muli.management_helper import ensure_helper

                result["helper"] = await ensure_helper(control, manifest)
        except TransactionError as exc:
            if exc.code != "lock_busy":
                raise
            result["helper"] = "controller_busy"
        return result


async def guarded_service_action(
    service: UserService, action: str, *, managed_root: Path | None = None
) -> dict[str, Any]:
    if service.platform != "linux":
        return await service.action(action)
    root = managed_root or service.data_dir.absolute() / "management" / "transaction-v1"
    managed = managed_record(root, required=managed_root is not None)
    if managed is not None:
        anchor = check_cli_paths(root, managed, service.config_path, service.data_dir)
        if not managed.resolved:
            raise TransactionError("transaction_in_progress")
        if action == "install":
            raise TransactionError("managed_unit_replacement_forbidden")
        with (
            TransactionJournal(
                root, Path(anchor.database_path), anchor.journal, create=False
            ) as journal,
            journal.locked() as control,
        ):
            if control.read_current() != managed:
                raise TransactionError("stale_revision")
            role: Role = "target" if managed.phase == "committed" else "base"
            binding = managed.binding(role)
            if str(service.path.absolute()) != anchor.unit_path:
                raise TransactionError("service_identity_changed")
            with ConfigurationFile(service.path, anchor.journal.max_snapshot_bytes) as unit:
                unit.confirm(binding.unit.digest, guard=control.check)
            return await service.action(action)
    _check_unit(service, installing=action == "install")
    manifest = _published(root)
    with (
        TransactionJournal(
            root, service.data_dir / "muli.sqlite3", manifest.journal if manifest else None
        ) as journal,
        journal.locked() as control,
    ):
        if manifest is not None:
            _check_paths(service, journal, manifest)
        record = control.read()
        if record is not None and not record.resolved:
            raise TransactionError("transaction_in_progress")
        _check_unit(service, installing=action == "install")
        return await service.action(action)
