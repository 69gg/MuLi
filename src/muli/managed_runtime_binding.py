"""Verify a live host's current protocol-2 binding without authorizing effects."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from muli.compatibility import assess_compatibility
from muli.format_evidence import read_format_evidence
from muli.managed_boot import ManagedBootPermit
from muli.managed_transaction import TransactionRecordV2, raw_snapshot
from muli.management_projection import _check_lease, _connection_identity
from muli.management_transaction import Role, _snapshot
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.installation import InstallationManifest
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SelectorStore,
    SlotIdentity,
    verify_current_process,
    verify_slot_files,
)
from muli.platforms.managed_launcher import PackageProofs
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


def _current_record(permit: BootPermit | ManagedBootPermit, record: TransactionRecordV2) -> None:
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
    permit: BootPermit | ManagedBootPermit, record: TransactionRecordV2, role: Role | None
) -> Role:
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
    record: TransactionRecordV2,
    role: Role,
    anchor: DeploymentAnchor,
) -> None:
    binding = record.binding(role)
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
                    actual_selector.value.deployment != record.identity.deployment
                    or actual_selector.value.installation != binding.installation
                )
            )
        ):
            raise RuntimeBindingError("runtime_binding_files_changed")


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
    record: TransactionRecordV2,
    role: Role | None,
) -> None:
    """Recheck a nonce-authenticated host; origin uses the frozen base binding.

    The caller already authenticated this actual systemd boot and the nonce and
    core identity. No DTO, filesystem observation or callback creates that
    authority here. The supplied Store remains caller-owned and is never closed.
    """
    try:
        _current_record(permit, record)
        selected_role = _selected_role(permit, record, role)
        binding = record.binding(selected_role)
        journal = permit.journal
        controller_packages: PackageProofs = {}
        with DeploymentStore(journal.directory.path, journal.limits) as deployments:
            anchor = deployments.read_anchor(record.identity.deployment)
            deployments.verify_anchor(anchor, observed_packages=controller_packages)
            slot = deployments.read_slot(binding.installation)
            controller = deployments.read_slot(anchor.controller_slot)
        if (
            anchor.digest != record.identity.deployment
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
        # Share only content from this call's complete controller check. The
        # selected slot still independently reopens files and walks its tree.
        verify_slot_files(
            slot,
            verified_python=controller.resolved_python,
            verified_packages=controller_packages,
        )
        verify_current_process(slot)
        del controller_packages
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
        else:
            if binding.entry != "legacy":
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
        _files(permit, record, selected_role, anchor)
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
        _files(permit, record, selected_role, anchor)
        permit.check_paths(Path(anchor.config_path), journal.database)
        _current_record(permit, record)
    except RuntimeBindingError:
        raise
    except (OSError, ValueError, sqlite3.Error):
        raise RuntimeBindingError("runtime_binding_verification_failed") from None
