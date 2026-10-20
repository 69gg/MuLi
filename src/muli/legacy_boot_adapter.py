"""A legacy transaction view that retains its managed writer's cleanup owner."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from muli.legacy_transaction_bridge import prepare_legacy_after_managed
from muli.maintenance import BootPermit
from muli.managed_boot import ManagedBootPermit
from muli.management_transaction import Deadline, JournalControl, TransactionError
from muli.platforms.installation import InstallationManifest
from muli.store import Store


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


def prepare_legacy_permit(
    control: JournalControl,
    *,
    owner: ManagedBootPermit,
    store: Store,
    manifest: InstallationManifest,
    request_id: str,
    action: Literal["configure", "restart"],
    target: bytes,
    deadline: Deadline,
    generation: str | None = None,
) -> LegacyBootPermit:
    """Return a publishable V1 view only after the existing bridge commits.

    The host authenticated this running raw service and holds its configuration
    lock. It must synchronously publish the returned view before any await, and
    still close every business writer before asking this view to release ownership.
    Exceptions leave the original owner untouched, including uncertain cleanup.
    """
    control.check()
    if control.journal is not owner.journal:
        raise TransactionError("foreign_journal")
    owner.check_paths(Path(manifest.config_path), Path(manifest.database_path))
    # Construct before the durable write. Nothing after its successful return
    # performs I/O, validation, a lock transfer or a second acquisition.
    view = LegacyBootPermit(owner, manifest)
    record = prepare_legacy_after_managed(
        control,
        store=store,
        writer=owner.lease,
        manifest=manifest,
        origin_boot=owner.boot,
        request_id=request_id,
        action=action,
        target=target,
        deadline=deadline,
        generation=generation,
    )
    view.record = record
    return view
