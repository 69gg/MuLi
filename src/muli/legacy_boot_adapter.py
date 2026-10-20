"""A legacy transaction view that retains its managed writer's cleanup owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from muli.legacy_transaction_bridge import (
    confirm_legacy_after_managed,
    prepare_legacy_after_managed,
)
from muli.maintenance import BootPermit
from muli.managed_boot import ManagedBootPermit
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Deadline, JournalControl, TransactionError
from muli.platforms.installation import InstallationManifest
from muli.store import Store


@dataclass(frozen=True, slots=True)
class PendingLegacy:
    """Host-owned authorization for one bridge; never another persistent state."""

    manifest: InstallationManifest = field(repr=False)
    previous: TransactionRecordV2 = field(repr=False)
    request_id: str
    action: Literal["configure", "restart"]
    base: bytes = field(repr=False)
    target: bytes = field(repr=False)
    deadline: Deadline = field(repr=False)
    generation: str


class LegacyBootPermit(BootPermit):
    """Logical V1 identity only; the retained managed owner closes its resources."""

    def __init__(self, owner: ManagedBootPermit, manifest: InstallationManifest) -> None:
        self.writer_owner: ManagedBootPermit = owner
        super().__init__(
            owner.journal,
            Path(manifest.config_path),
            owner.boot,
            manifest.digest,
            manifest.service_digest,
            owner.lease,
            None,
            None,
        )

    def check_paths(self, config_path: Path, database: Path) -> None:
        self.writer_owner.check_paths(config_path, database)

    def close_after_writers(self) -> None:
        # Never release the raw lease underneath a sticky managed cleanup failure.
        self.writer_owner.close_after_writers()


def _check_owner(
    control: JournalControl,
    owner: ManagedBootPermit,
    pending: PendingLegacy,
) -> None:
    control.check()
    if control.journal is not owner.journal:
        raise TransactionError("foreign_journal")
    owner.check_paths(Path(pending.manifest.config_path), Path(pending.manifest.database_path))
    if (
        owner.anchor.digest != pending.previous.identity.deployment
        or owner.installation != pending.previous.base.installation
    ):
        raise TransactionError("legacy_owner_changed")


def prepare_legacy_permit(
    control: JournalControl,
    *,
    owner: ManagedBootPermit,
    store: Store,
    pending: PendingLegacy,
) -> LegacyBootPermit:
    """Return a publishable V1 view only after the existing bridge commits.

    The host authenticated this running raw service and holds its configuration
    lock. It must synchronously publish the returned view before any await, and
    still close every business writer before asking this view to release ownership.
    Exceptions leave the original owner untouched, including uncertain cleanup.
    """
    _check_owner(control, owner, pending)
    if control.read_current() != pending.previous:
        raise TransactionError("stale_revision")
    # Construct before the durable write. Nothing after its successful return
    # performs I/O, validation, a lock transfer or a second acquisition.
    view = LegacyBootPermit(owner, pending.manifest)
    record = prepare_legacy_after_managed(
        control,
        store=store,
        writer=owner.lease,
        manifest=pending.manifest,
        origin_boot=owner.boot,
        request_id=pending.request_id,
        action=pending.action,
        target=pending.target,
        deadline=pending.deadline,
        generation=pending.generation,
        expected_base=pending.base,
    )
    view.record = record
    return view


def confirm_legacy_permit(
    control: JournalControl,
    *,
    owner: ManagedBootPermit,
    store: Store,
    pending: PendingLegacy,
) -> LegacyBootPermit:
    """Recover a lost return using the retained owner, attempt and actual Store."""
    _check_owner(control, owner, pending)
    view = LegacyBootPermit(owner, pending.manifest)
    record = confirm_legacy_after_managed(
        control,
        store=store,
        writer=owner.lease,
        manifest=pending.manifest,
        previous=pending.previous,
        origin_boot=owner.boot,
        request_id=pending.request_id,
        action=pending.action,
        base=pending.base,
        target=pending.target,
        deadline=pending.deadline,
        generation=pending.generation,
    )
    _check_owner(control, owner, pending)
    view.record = record
    return view
