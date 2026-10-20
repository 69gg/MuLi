"""Return a failed adoption to a new legacy transaction, never rewrite its history.

The host must authenticate the currently running service boot before calling.
This boundary independently checks the real Store projection, restored files,
current interpreter and retained installation under the existing two locks.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Literal

from muli.config import Settings
from muli.managed_journal import _collect, read_snapshots
from muli.managed_transaction import TransactionRecordV2, raw_snapshot
from muli.management_projection import _connection_identity, read_projection
from muli.management_transaction import (
    Deadline,
    FixedLock,
    JournalControl,
    ProjectionReceipt,
    ServiceIdentity,
    TransactionError,
    TransactionRecord,
    _identity,
    _snapshot,
)
from muli.platforms.installation import InstallationManifest
from muli.platforms.managed_deployment import DeploymentStore, SelectorStore, verify_current_slot
from muli.platforms.transaction_files import ConfigurationFile
from muli.store import Store


def prepare_legacy_after_managed(
    control: JournalControl,
    *,
    store: Store,
    writer: FixedLock,
    manifest: InstallationManifest,
    origin_boot: str,
    request_id: str,
    action: Literal["configure", "restart"],
    target: bytes,
    deadline: Deadline,
    generation: str | None = None,
) -> TransactionRecord:
    control.check()
    writer.check()
    journal = control.journal
    if writer.name != "lease" or _identity(os.fstat(writer.directory.fd)) != _identity(
        os.fstat(journal.writer_directory.fd)
    ):
        raise TransactionError("writer_lease_required")
    previous = control.read_current()
    if not isinstance(previous, TransactionRecordV2):
        raise TransactionError("managed_transaction_required")
    if not previous.resolved:
        raise TransactionError("transaction_in_progress")
    if previous.phase not in {"aborted", "rolled_back"} or previous.base.entry != "legacy":
        raise TransactionError("legacy_entry_required")
    if previous.request_id == request_id:
        raise TransactionError("request_conflict")
    if generation is not None and (
        not isinstance(generation, str) or re.fullmatch(r"[a-f0-9]{32}", generation) is None
    ):
        raise TransactionError("invalid_generation")
    generation = generation or secrets.token_hex(16)
    if generation == previous.generation:
        raise TransactionError("generation_conflict")
    actual_store = _connection_identity(store)
    if (
        actual_store.path != journal.database
        or actual_store.instance != previous.identity.store_instance
        or actual_store.main != previous.identity.main_session
    ):
        raise TransactionError("store_changed")
    receipt = ProjectionReceipt(
        **previous.position.model_dump(), store_instance=actual_store.instance
    )
    if read_projection(store) != receipt:
        raise TransactionError("projection_required")
    store.revalidate_format()
    if store.format_state != previous.observed_format:
        raise TransactionError("format_changed")
    # A failed adoption must still have its frozen evidence; this is not an
    # escape hatch for a corrupt managed record or a broken deployment anchor.
    old = read_snapshots(journal, previous, "base")
    read_snapshots(journal, previous, "target")
    with DeploymentStore(journal.directory.path, journal.limits) as deployments:
        anchor = deployments.read_anchor(previous.identity.deployment)
        deployments.verify_anchor(anchor)
        slot = deployments.read_slot(previous.base.installation)
        verify_current_slot(slot)
    if (
        slot.compatibility_digest != previous.base.compatibility
        or anchor.config_path != manifest.config_path
        or anchor.data_dir != manifest.data_dir
        or anchor.database_path != manifest.database_path
        or anchor.unit_path != manifest.unit_path
        or Path(manifest.database_path) != journal.database
        or slot.lexical_python != manifest.interpreter.executable
        or slot.package.root != manifest.package.root
        or manifest.journal != journal.limits
    ):
        raise TransactionError("legacy_installation_changed")
    manifest.verify()
    limit = journal.limits.max_snapshot_bytes
    target_ref = _snapshot(target, limit)
    Settings.model_validate_json(target)
    if action == "restart" and target_ref != previous.base.configuration:
        raise TransactionError("restart_changes_configuration")

    def guard() -> None:
        control.check()
        writer.check()

    with (
        ConfigurationFile(Path(anchor.config_path), limit) as config,
        ConfigurationFile(Path(anchor.unit_path), limit) as unit,
        SelectorStore(journal.directory.path) as selector,
    ):
        configuration = config.confirm(previous.base.configuration.digest, guard=guard)
        restored_unit = unit.confirm(previous.base.unit.digest, guard=guard)
        if (
            configuration.data != old.configuration
            or raw_snapshot(restored_unit.data, limit) != previous.base.unit
            or selector.read() is not None
        ):
            raise TransactionError("legacy_entry_changed")
        selector.confirm(None, guard=guard)
        record = TransactionRecord(
            request_id=request_id,
            generation=generation,
            revision=1,
            action=action,
            identity=ServiceIdentity(
                installation=manifest.digest,
                service=manifest.service_digest,
                store_instance=actual_store.instance,
                main_session=actual_store.main,
                origin_boot=origin_boot,
            ),
            base=previous.base.configuration,
            target=target_ref,
            deadline=deadline,
            challenge=secrets.token_hex(32),
        )
        installation = manifest.canonical()
        if (
            max(len(record.model_dump_json().encode()), len(installation))
            > journal.limits.max_record_bytes
        ):
            raise TransactionError("record_budget")
        incoming = {
            f"{generation}-base.json": old.configuration,
            f"{generation}-target.json": target,
            "installation.json": installation,
        }
        _collect(control, previous, incoming)
        for name, data in incoming.items():
            control._replace(name, data)
        # Confirm again after preparation writes; no await or new authorization
        # can turn a manual file edit into permission to overwrite state.
        config.confirm(previous.base.configuration.digest, guard=guard)
        unit.confirm(previous.base.unit.digest, guard=guard)
        selector.confirm(None, guard=guard)
        manifest.verify()
        store.revalidate_format()
        if control.read_current() != previous or read_projection(store) != receipt:
            raise TransactionError("stale_revision")
        return control._write(record)
