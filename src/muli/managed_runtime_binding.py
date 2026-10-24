"""Verify a live host's current protocol-2 binding without authorizing effects."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from muli.compatibility import assess_compatibility
from muli.format_evidence import read_format_evidence
from muli.managed_boot import ManagedBootPermit, _transfer_identity_matches
from muli.managed_transaction import (
    ProjectControllerTransferRecord,
    RoleBinding,
    TransactionRecordV2,
    raw_snapshot,
)
from muli.management_projection import _check_lease, _connection_identity, read_projection
from muli.management_transaction import ProjectionReceipt, Role, _snapshot
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.installation import InstallationManifest
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorStore,
    SlotIdentity,
    verify_current_slot,
)
from muli.platforms.managed_probe import transfer_origin_material
from muli.platforms.transaction_files import ConfigurationFile
from muli.service import UserService
from muli.store import Store

if TYPE_CHECKING:
    from muli.maintenance import BootPermit


class RuntimeBindingError(ValueError):
    """A fixed code, without private paths, settings or database evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _current_record(
    permit: BootPermit | ManagedBootPermit,
    record: TransactionRecordV2 | ProjectControllerTransferRecord,
) -> None:
    permit.journal.directory.check()
    _check_lease(permit)
    if permit.journal.read_current() != record:
        raise RuntimeBindingError("runtime_binding_record_changed")
    # This is a lock-free host, not a second controller. Confirm any visible
    # decision left by a preceding owner without taking its journal flock.
    os.fsync(permit.journal.directory.fd)
    if permit.journal.read_current() != record:
        raise RuntimeBindingError("runtime_binding_record_changed")


def _selected_role(
    permit: BootPermit | ManagedBootPermit,
    record: TransactionRecordV2 | ProjectControllerTransferRecord,
    role: Role | None,
) -> Role:
    if isinstance(record, ProjectControllerTransferRecord) and (
        role is None
        or permit.boot == record.identity.origin_boot
        or not isinstance(permit, ManagedBootPermit)
    ):
        raise RuntimeBindingError("runtime_binding_role_changed")
    if role is None:
        if permit.boot != record.identity.origin_boot or (
            record.phase not in {"prepared", "draining", "aborted"}
            and (record.phase, record.intent)
            not in {("activating", "stop_base"), ("rolling_back", "stop_target")}
        ):
            raise RuntimeBindingError("runtime_binding_role_changed")
        return "base"
    expected = (
        ("target" if record.phase == "committed" else "base")
        if record.resolved
        else record.boot_role
    )
    if role != expected or (not record.resolved and permit.boot == record.identity.origin_boot):
        raise RuntimeBindingError("runtime_binding_role_changed")
    return role


def _files(
    permit: BootPermit | ManagedBootPermit,
    binding: RoleBinding,
    deployment: str,
    anchor: DeploymentAnchor,
) -> None:
    limit = permit.journal.limits.max_snapshot_bytes
    with (
        ConfigurationFile(Path(anchor.config_path), limit) as configuration,
        ConfigurationFile(Path(anchor.unit_path), limit) as unit,
        SelectorStore(permit.journal.directory.path) as selector,
    ):
        actual_selector = selector.read()
        if (
            _snapshot(configuration.read().data, limit) != binding.configuration
            or raw_snapshot(unit.read().data, limit) != binding.unit
            or (actual_selector.reference if actual_selector else None) != binding.selector
            or (
                actual_selector is not None
                and (
                    actual_selector.value.deployment != deployment
                    or actual_selector.value.installation != binding.installation
                )
            )
        ):
            raise RuntimeBindingError("runtime_binding_files_changed")


def _origin_permit(
    permit: BootPermit | ManagedBootPermit,
    store: Store,
    record: ProjectControllerTransferRecord,
    anchor: DeploymentAnchor,
) -> DeploymentAnchor:
    origin, _, _ = transfer_origin_material(permit.journal, record, anchor)
    if not isinstance(permit, ManagedBootPermit):
        raise RuntimeBindingError("runtime_binding_role_changed")
    previous = permit.record
    if (
        permit.probation
        or not previous.resolved
        or permit.boot != record.identity.origin_boot
        or permit.anchor != origin
        or permit.installation != record.origin.binding.installation
        or previous.identity.deployment != origin.digest
        or previous.identity.store_instance != record.identity.store_instance
        or previous.identity.main_session != record.identity.main_session
        or previous.position.generation == record.generation
        or record.origin.projected
        != ProjectionReceipt(
            **previous.position.model_dump(), store_instance=previous.identity.store_instance
        )
        or read_projection(store) != record.origin.projected
    ):
        raise RuntimeBindingError("runtime_binding_origin_changed")
    prior = previous.binding(permit.role)
    current = record.origin.binding
    # The source may have an authorized hot configuration. Its actual bytes
    # are frozen in origin; slot/selector/unit must still be this real permit.
    if (
        prior.entry != current.entry
        or prior.installation != current.installation
        or prior.compatibility != current.compatibility
        or prior.selector != current.selector
        or prior.unit != current.unit
    ):
        raise RuntimeBindingError("runtime_binding_origin_changed")
    return origin


def _legacy_installation(
    permit: BootPermit, record: TransactionRecordV2, anchor: DeploymentAnchor, slot: SlotIdentity
) -> None:
    # An ordinary supported boot can have a captured, not yet published, V1
    # manifest. Recreate it from actual files and the frozen budgets; loading an
    # arbitrary record's paths alone is not proof of this permit's identity.
    if record.base.entry != "legacy":
        raise RuntimeBindingError("runtime_binding_legacy_required")
    manifest = InstallationManifest.capture(
        UserService(
            Path(anchor.config_path),
            Path(anchor.data_dir),
            Path(anchor.unit_path).parent,
            platform="linux",
            executable=slot.lexical_python,
        ),
        permit.journal.database,
        helper=anchor.helper,
        journal=anchor.journal,
        package_limits=anchor.package_limits,
    )
    if (
        (manifest.digest, manifest.service_digest) != (permit.installation, permit.service)
        or manifest.interpreter.executable != slot.lexical_python
        or manifest.interpreter.prefix != slot.prefix
        or manifest.package.root != slot.package.root
    ):
        raise RuntimeBindingError("runtime_binding_installation_changed")


def verify_running_binding(
    permit: BootPermit | ManagedBootPermit,
    store: Store,
    record: TransactionRecordV2 | ProjectControllerTransferRecord,
    role: Role | None,
) -> None:
    """Recheck a nonce-authenticated host; origin uses the frozen base binding.

    The caller already authenticated this actual systemd boot and the nonce and
    core identity. No DTO, filesystem observation or callback creates that
    authority here. The supplied Store remains caller-owned and is never closed.
    """
    try:
        _current_record(permit, record)
        origin = isinstance(record, ProjectControllerTransferRecord) and role is None
        selected_role = None if origin else _selected_role(permit, record, role)
        if origin:
            assert isinstance(record, ProjectControllerTransferRecord)
            if permit.boot != record.identity.origin_boot or (
                record.phase not in {"prepared", "draining"}
                and (record.phase, record.intent)
                not in {("activating", "stop_base"), ("rolling_back", "stop_target")}
            ):
                raise RuntimeBindingError("runtime_binding_role_changed")
            binding = record.origin.binding
        else:
            assert selected_role is not None
            binding = record.binding(selected_role)
        journal = permit.journal
        with DeploymentStore(journal.directory.path, journal.limits) as deployments:
            anchor = deployments.read_anchor(record.identity.deployment)
            if origin:
                assert isinstance(record, ProjectControllerTransferRecord)
                anchor = _origin_permit(permit, store, record, anchor)
            deployments.verify_anchor(anchor)
            slot = deployments.read_slot(binding.installation)
        if (
            anchor.digest
            != (
                record.origin.deployment
                if origin and isinstance(record, ProjectControllerTransferRecord)
                else record.identity.deployment
            )
            or slot.digest != binding.installation
            or slot.compatibility_digest != binding.compatibility
            or anchor.config_path != str(permit.config_path)
            or anchor.database_path != str(journal.database)
            or anchor.journal_root != str(journal.directory.path)
            or anchor.journal != journal.limits
            or (binding.entry == "managed" and binding.unit != anchor.managed_unit)
        ):
            raise RuntimeBindingError("runtime_binding_installation_changed")
        permit.check_paths(Path(anchor.config_path), journal.database)
        verify_current_slot(slot)
        if isinstance(permit, ManagedBootPermit):
            if (
                permit.anchor.digest != anchor.digest
                or permit.installation != slot.digest
                or (
                    role is not None
                    and (
                        permit.role != selected_role
                        or permit.record.generation != record.generation
                    )
                )
            ):
                raise RuntimeBindingError("runtime_binding_installation_changed")
            if not origin and not _transfer_identity_matches(permit.record, record):
                raise RuntimeBindingError("runtime_binding_record_changed")
        else:
            if isinstance(record, ProjectControllerTransferRecord) or binding.entry != "legacy":
                raise RuntimeBindingError("runtime_binding_legacy_required")
            _legacy_installation(permit, record, anchor, slot)
        verify_local_filesystems(
            (
                Path(anchor.config_path).parent,
                journal.database.parent,
                journal.directory.path,
                journal.writer_directory.path,
                Path(anchor.unit_path).parent,
            )
        )
        _files(permit, binding, anchor.digest, anchor)
        evidence = read_format_evidence(store.db)
        identity = _connection_identity(store)
        if (
            identity.path != journal.database
            or (identity.instance, identity.main)
            != (record.identity.store_instance, record.identity.main_session)
            or (evidence.store_instance, evidence.main_session)
            != (identity.instance, identity.main)
            or evidence.state != record.observed_format
            or not assess_compatibility(
                slot.compatibility, slot.compatibility, evidence.state, handoff_protocol=2
            ).format_compatible
        ):
            raise RuntimeBindingError("runtime_binding_format_changed")
        _files(permit, binding, anchor.digest, anchor)
        permit.check_paths(Path(anchor.config_path), journal.database)
        _current_record(permit, record)
    except RuntimeBindingError:
        raise
    except (OSError, ValueError, sqlite3.Error):
        raise RuntimeBindingError("runtime_binding_verification_failed") from None
