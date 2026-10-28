"""Explicit stopped-service plugin changes through existing recovery authority."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

from muli.config import Settings
from muli.managed_boot import _DatabaseAnchor
from muli.managed_cli import check_cli_paths, managed_record
from muli.managed_transaction import TransactionRecordV2
from muli.management_cli import _check_paths, _published
from muli.management_projection import read_projection_connection
from muli.management_transaction import (
    ProjectionReceipt,
    Role,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    _object,
)
from muli.platforms.installation import InstallationManifest
from muli.platforms.linux_controller import CgroupObservation
from muli.platforms.linux_transaction import ServiceSnapshot, SystemdProbe
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SlotIdentity,
    verify_current_slot,
)
from muli.platforms.managed_launcher import absolute, resolve_entry
from muli.platforms.managed_probe import ManagedSystemdProbe
from muli.platforms.transaction_files import ConfigurationFile
from muli.plugins import PLUGIN_ID
from muli.service import UserService


def _projection_guard(
    stack: ExitStack,
    journal: TransactionJournal,
    record: TransactionRecord | TransactionRecordV2 | None,
) -> Callable[[], None]:
    if record is None:
        return lambda: None
    database = stack.enter_context(closing(_DatabaseAnchor(journal.database)))
    database.check()
    db = stack.enter_context(
        closing(
            sqlite3.connect(
                journal.database.as_uri() + "?mode=ro&cache=private",
                uri=True,
                timeout=0,
                isolation_level="DEFERRED",
            )
        )
    )
    expected = ProjectionReceipt(
        **record.position.model_dump(), store_instance=record.identity.store_instance
    )

    def check() -> None:
        database.check()
        if (
            read_projection_connection(db) != expected
            or db.execute(
                "SELECT 1 FROM runtime_state WHERE name='main_session' AND typeof(value)='text' "
                "AND value=?",
                (record.identity.main_session,),
            ).fetchone()
            is None
        ):
            raise TransactionError("plugin_projection_required")
        database.check()

    check()
    return check


def _legacy_identity(
    service: UserService,
    journal: TransactionJournal,
    record: TransactionRecord | None,
    manifest: InstallationManifest,
    *,
    published: bool,
) -> Callable[[], None]:
    def check() -> None:
        current = _published(journal.directory.path)
        if current != (manifest if published else None):
            raise TransactionError("plugin_installation_changed")
        _check_paths(service, journal, manifest)
        manifest.verify(require_database=record is not None)
        if record is not None and (
            record.identity.installation != manifest.digest
            or record.identity.service != manifest.service_digest
        ):
            raise TransactionError("plugin_installation_changed")

    check()
    return check


def _managed_identity(
    service: UserService,
    journal: TransactionJournal,
    record: TransactionRecordV2,
    anchor: DeploymentAnchor,
) -> tuple[Callable[[], None], Callable[[], Awaitable[ServiceSnapshot]]]:
    role: Role = "target" if record.phase == "committed" else "base"
    binding = record.binding(role)
    roles: tuple[Role, ...] = ("base", "target")
    with DeploymentStore(journal.directory.path, journal.limits) as deployments:
        controller = deployments.read_slot(anchor.controller_slot)
        slots: dict[Role, SlotIdentity] = {
            selected: deployments.read_slot(record.binding(selected).installation)
            for selected in roles
        }

    def check() -> None:
        if (
            check_cli_paths(journal.directory.path, record, service.config_path, service.data_dir)
            != anchor
            or str(service.path.absolute()) != anchor.unit_path
        ):
            raise TransactionError("managed_paths_changed")
        with DeploymentStore(journal.directory.path, journal.limits) as deployments:
            if (
                deployments.read_anchor(anchor.digest) != anchor
                or deployments.read_slot(anchor.controller_slot) != controller
            ):
                raise TransactionError("plugin_installation_changed")
            deployments.verify_anchor(anchor)
            verify_current_slot(deployments.read_slot(binding.installation))
        plan = resolve_entry(journal.directory.path, purpose="cli", arguments=("plugins",))
        if (
            plan.installation != binding.installation
            or plan.deployment != anchor.digest
            or (plan.generation, plan.revision) != (record.generation, record.revision)
        ):
            raise TransactionError("plugin_installation_changed")
        with ConfigurationFile(service.path, journal.limits.max_snapshot_bytes) as unit:
            if unit.read().digest != binding.unit.digest:
                raise TransactionError("service_identity_changed")

    probe = ManagedSystemdProbe()

    async def observe() -> ServiceSnapshot:
        result = await probe.read_managed(journal, record, anchor, controller, slots)
        if not result.unit_confirmed or result.disk_unit != binding.unit.digest:
            raise TransactionError("service_identity_changed")
        return result.service

    check()
    return check, observe


def _stopped(snapshot: ServiceSnapshot) -> None:
    if (
        snapshot.unit.active_state not in {"inactive", "failed"}
        or snapshot.unit.pid != 0
        or snapshot.process is not None
        or snapshot.unit.job != (0, "/")
    ):
        raise TransactionError("plugin_service_not_stopped")


async def _set_plugin_disabled(
    service: UserService,
    identifier: str,
    disabled: bool,
    *,
    managed_root: Path | None,
) -> dict[str, Any]:
    root = absolute(str(managed_root or service.data_dir.absolute() / "management/transaction-v1"))
    database = absolute(str(service.data_dir.absolute() / "muli.sqlite3"))
    if database.resolve() != database:
        raise TransactionError("plugin_database_path_changed")
    managed = managed_record(root, required=managed_root is not None)
    anchor = (
        check_cli_paths(root, managed, service.config_path, service.data_dir)
        if managed is not None
        else None
    )
    existing = _published(root) if managed is None else None
    if managed is not None and not managed.resolved:
        raise TransactionError("transaction_in_progress")
    manifest = None
    if managed is None:
        manifest = existing or InstallationManifest.capture(
            service, database, require_database=False
        )
    limits = anchor.journal if anchor is not None else manifest.journal if manifest else None
    with (
        TransactionJournal(root, database, limits, create=managed is None) as journal,
        journal.locked() as control,
        journal.writer_lease() as writer,
        ExitStack() as stack,
    ):
        record = control.read_current()
        if (managed is not None and record != managed) or (
            managed is None and isinstance(record, TransactionRecordV2)
        ):
            raise TransactionError("stale_revision")
        if record is not None and not record.resolved:
            raise TransactionError("transaction_in_progress")
        if isinstance(record, TransactionRecordV2):
            if anchor is None:
                raise TransactionError("managed_transaction_required")
            identity, observe = _managed_identity(service, journal, record, anchor)
        else:
            if manifest is None or (record is not None and existing is None):
                raise TransactionError("pending_manifest_missing")
            identity = _legacy_identity(
                service, journal, record, manifest, published=existing is not None
            )
            probe = SystemdProbe()

            async def observe_legacy() -> ServiceSnapshot:
                return await probe.read(service)

            observe = observe_legacy

        projection = _projection_guard(stack, journal, record)
        before = await observe()
        _stopped(before)
        observation = SystemdProbe()
        cgroups = CgroupObservation(observation.cgroup_root, observation.limits.file_bytes)

        def guard() -> None:
            control.check()
            writer.check()
            if control.read_current() != record:
                raise TransactionError("stale_revision")
            if (
                database.resolve() != database
                or managed_record(root, required=managed is not None) != managed
            ):
                raise TransactionError("plugin_authority_changed")
            identity()
            projection()
            if not cgroups.empty(before.unit.cgroup):
                raise TransactionError("plugin_service_not_stopped")

        guard()
        after = await observe()
        _stopped(after)
        if after != before:
            raise TransactionError("plugin_service_changed")
        with ConfigurationFile(service.config_path, journal.limits.max_snapshot_bytes) as source:
            snapshot = source.read()
            values = _object(snapshot.data)
            settings = Settings.model_validate(values)
            present = identifier in settings.plugins.disabled
            guard()
            if present == disabled:
                # No rewrite or durability claim for an already-requested setting.
                if source.read() != snapshot:
                    raise TransactionError("configuration_changed")
                changed = False
            else:
                entries = settings.plugins.disabled
                values.setdefault("plugins", {})["disabled"] = (
                    [*entries, identifier]
                    if disabled
                    else [entry for entry in entries if entry != identifier]
                )
                Settings.model_validate(values)
                data = json.dumps(values, ensure_ascii=False, allow_nan=False, indent=2).encode()
                source.replace(snapshot.digest, data, guard=guard)
                changed = True
        return {
            "id": identifier,
            "disabled": disabled,
            "changed": changed,
            "code": "plugin_state_saved" if changed else "plugin_state_unchanged",
            "effective": "next_start",
        }


async def set_plugin_disabled(
    service: UserService,
    identifier: str,
    disabled: bool,
    *,
    managed_root: Path | None = None,
) -> dict[str, Any]:
    """Change one disabled-list member; never load plugins or control a service."""
    if sys.platform != "linux" or service.platform != "linux":
        raise TransactionError("platform_unsupported")
    if not isinstance(identifier, str) or not PLUGIN_ID.fullmatch(identifier):
        raise TransactionError("plugin_identifier_invalid")
    if type(disabled) is not bool:
        raise TransactionError("plugin_state_invalid")
    try:
        return await _set_plugin_disabled(service, identifier, disabled, managed_root=managed_root)
    except TransactionError:
        raise
    except (OSError, ValueError, sqlite3.Error, TimeoutError):
        # Library errors can include paths or complete settings. Keep them local.
        raise TransactionError("plugin_recovery_unverified") from None
