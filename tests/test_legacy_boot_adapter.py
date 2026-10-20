from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from test_legacy_transaction_bridge import Recovered
from test_legacy_transaction_bridge import recovered as recovered
from test_managed_transaction import deadline

from muli import legacy_boot_adapter, managed_boot
from muli.config import Settings
from muli.legacy_boot_adapter import (
    LegacyBootPermit,
    PendingLegacy,
    confirm_legacy_permit,
    prepare_legacy_permit,
)
from muli.maintenance import BootPermit, Maintenance
from muli.managed_boot import ManagedBootError, ManagedBootPermit
from muli.management_projection import project_final
from muli.management_transaction import (
    TransactionError,
    TransactionRecord,
    Transition,
    _snapshot,
)
from muli.platforms.managed_deployment import resolve_entry
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux legacy writer ownership")


@dataclass
class Host:
    recovered: Recovered
    owner: ManagedBootPermit
    store: Store

    def pending(self, *, action: str = "configure") -> PendingLegacy:
        assert action in {"configure", "restart"}
        return PendingLegacy(
            manifest=self.recovered.manifest,
            previous=self.recovered.previous,
            request_id="next-legacy-" + action,
            action="configure" if action == "configure" else "restart",
            base=Path(self.recovered.manifest.config_path).read_bytes(),
            target=self.recovered.target
            if action == "configure"
            else Path(self.recovered.manifest.config_path).read_bytes(),
            deadline=deadline(),
            generation=secrets.token_hex(16),
        )

    def prepare(
        self, *, action: str = "configure", pending: PendingLegacy | None = None
    ) -> LegacyBootPermit:
        with self.recovered.journal.locked() as control:
            return prepare_legacy_permit(
                control,
                owner=self.owner,
                store=self.store,
                pending=pending or self.pending(action=action),
            )

    def confirm(self, pending: PendingLegacy) -> LegacyBootPermit:
        with self.recovered.journal.locked() as control:
            return confirm_legacy_permit(
                control, owner=self.owner, store=self.store, pending=pending
            )

    def assert_owned(self) -> None:
        self.owner.check_paths(
            Path(self.recovered.manifest.config_path), self.recovered.journal.database
        )
        assert self.owner._database is not None and self.owner._database.fd is not None
        os.fstat(self.owner._database.fd)
        with pytest.raises(TransactionError, match="lock_busy"):
            self.recovered.journal.writer_lease().__enter__()


@pytest.fixture
def host(recovered: Recovered, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    recovered.store.close()
    monkeypatch.setattr(managed_boot, "verify_local_filesystems", lambda paths: None)
    owner = ManagedBootPermit.acquire(
        recovered.journal,
        boot=recovered.previous.health.boot if recovered.previous.health else "running-legacy",
        plan=resolve_entry(recovered.journal.directory.path, purpose="service"),
        lease=recovered.writer,
    )
    store = owner.open_store()
    recovered.store = store
    try:
        yield Host(recovered, owner, store)
    finally:
        store.close()
        if owner._cleanup_failed:
            # Synthetic failure teardown only: the test already proved sticky
            # production close refuses. The real temporary Store is now closed.
            assert owner._database is not None
            owner._database.close()
        else:
            owner.close_after_writers()


@pytest.mark.parametrize("recovered", ["aborted", "rolled_back"], indirect=True)
@pytest.mark.parametrize("action", ["configure", "restart"])
async def test_returned_v1_view_proves_origin_without_losing_managed_database_anchor(
    host: Host, action: str
) -> None:
    owner = host.owner
    original_record, original_installation = owner.record, owner.installation
    anchor = owner._database
    assert anchor is not None
    current = Settings(default_mode="yolo").model_dump_json().encode()
    Path(host.recovered.manifest.config_path).write_bytes(current)
    view = host.prepare(action=action)
    assert isinstance(view, BootPermit)
    assert view.writer_owner is owner and view.lease is owner.lease
    assert view.boot == owner.boot and view.journal is owner.journal
    assert view.installation == host.recovered.manifest.digest
    assert view.service == host.recovered.manifest.service_digest
    assert view.installation != owner.installation
    assert view.record is not None and view.record.identity.origin_boot == owner.boot
    assert view.record.base != host.recovered.previous.base.configuration
    assert view.journal.snapshot(view.record, "base") == current
    assert not view.probation and view.role is None
    assert owner.record == original_record and owner.installation == original_installation
    assert not hasattr(owner, "service") and owner._database is anchor
    host.assert_owned()

    maintenance = Maintenance(view)
    maintenance.prepared(host.store.instance_id, host.store.main_session()["id"])
    maintenance.owner("runtime", lambda: True)
    maintenance.seal({"runtime"})
    with view.journal.locked() as control:
        record = control.advance(view.record.position, Transition(phase="draining"))
    assert maintenance.drain(record.generation, owner.boot, record.challenge)["state"] == "drained"
    proof = maintenance.readiness(record.generation, owner.boot, record.challenge)
    assert proof.installation == view.installation and proof.admission == "paused"
    host.assert_owned()
    host.store.close()
    # Closing a business writer alone never releases the managed inode/lease.
    host.assert_owned()
    view.close_after_writers()
    assert anchor.fd is None and owner._closed
    with view.journal.writer_lease():
        pass


def test_failed_bridge_leaves_original_owner_and_store_live(host: Host) -> None:
    original_record = host.owner.record
    host.store.db.execute("DELETE FROM objects WHERE kind='management_projection'")
    host.store.db.commit()
    with pytest.raises(TransactionError, match="projection_required"):
        host.prepare()
    assert host.recovered.journal.read_current() == original_record
    assert host.owner.record == original_record and not hasattr(host.owner, "service")
    assert host.store.db.execute("SELECT 1").fetchone()[0] == 1
    host.assert_owned()


def test_bridge_cancellation_does_not_release_original_owner(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Cancelled(BaseException):
        pass

    def interrupted(*args: object, **kwargs: object) -> TransactionRecord:
        raise Cancelled

    monkeypatch.setattr(legacy_boot_adapter, "prepare_legacy_after_managed", interrupted)
    with pytest.raises(Cancelled):
        host.prepare()
    assert host.recovered.journal.read_current() == host.recovered.previous
    host.assert_owned()


def test_v1_projection_uses_manifest_identity_while_original_owner_remains_retained(
    host: Host,
) -> None:
    view = host.prepare()
    assert view.record is not None
    routing = FrozenRouting.bound(ReplyOrigin("web", "legacy-owner-input")).dump()
    host.store.put(
        "management",
        {
            "id": view.record.request_id,
            "action": view.record.action,
            "status": "applying",
            "transaction": {"generation": view.record.generation},
            "routing": routing,
        },
    )
    with view.journal.locked() as control:
        current = control.advance(
            view.record.position, Transition(phase="aborted", code="cancelled")
        )
    receipt = project_final(view, host.store)
    assert receipt is not None and receipt.generation == current.generation
    plan = host.store.get("management", current.request_id)
    assert plan is not None and plan["status"] == "cancelled"
    assert plan["routing"] == routing
    host.assert_owned()


def test_sticky_managed_close_failure_cannot_be_bypassed_by_the_v1_view(host: Host) -> None:
    view = host.prepare()
    host.owner._retain(host.store)
    with pytest.raises(ManagedBootError, match="managed_boot_cleanup_failed") as failure:
        view.close_after_writers()
    assert failure.value._owner is host.owner
    host.assert_owned()
    assert host.store.db.execute("SELECT 1").fetchone()[0] == 1


def test_path_checks_keep_the_original_database_inode_guard(host: Host) -> None:
    view = host.prepare()
    database = view.journal.database
    displaced = database.with_suffix(".displaced")
    database.rename(displaced)
    database.write_bytes(b"foreign database, never opened")
    try:
        with pytest.raises(ManagedBootError, match="managed_database_changed"):
            view.check_paths(Path(host.recovered.manifest.config_path), database)
        with pytest.raises(TransactionError, match="lock_busy"):
            view.journal.writer_lease().__enter__()
    finally:
        database.unlink()
        displaced.rename(database)


def test_lost_return_requires_a_fresh_controller_and_retains_the_real_owner(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = host.pending()
    journal = host.owner.journal
    original = journal.directory.replace

    def lost_return(name: str, data: bytes) -> None:
        original(name, data)
        if name == "state.json":
            raise OSError("synthetic lost return after real durable state replacement")

    monkeypatch.setattr(journal.directory, "replace", lost_return)
    with journal.locked() as poisoned:
        with pytest.raises(OSError, match="synthetic lost return"):
            prepare_legacy_permit(poisoned, owner=host.owner, store=host.store, pending=pending)
        with pytest.raises(TransactionError, match="journal_unconfirmed"):
            confirm_legacy_permit(poisoned, owner=host.owner, store=host.store, pending=pending)
    before = (journal.directory.path / "state.json").read_bytes()
    view = host.confirm(pending)
    assert view.record is not None and view.record.generation == pending.generation
    assert view.writer_owner is host.owner and view.lease is host.owner.lease
    assert (journal.directory.path / "state.json").read_bytes() == before
    assert poisoned._poisoned
    assert host.owner.record == pending.previous and not hasattr(host.owner, "service")
    host.assert_owned()


def test_unconfirmed_state_cannot_be_adopted_until_directory_fsync_succeeds(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = host.pending()
    journal = host.owner.journal
    original = os.fsync
    failing = True

    def fsync(descriptor: int) -> None:
        raw = (journal.directory.path / "state.json").read_bytes()
        if failing and descriptor == journal.directory.fd and b'"protocol":1' in raw:
            raise OSError("synthetic persistent directory EIO")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="persistent directory EIO"):
        host.prepare(pending=pending)
    visible = journal.read_current()
    assert isinstance(visible, TransactionRecord) and visible.generation == pending.generation
    with pytest.raises(OSError, match="persistent directory EIO"):
        host.confirm(pending)
    host.assert_owned()
    failing = False
    assert host.confirm(pending).record == visible
    host.assert_owned()


@pytest.mark.parametrize("phase", ["prepared", "draining", "aborted", "stop_base", "stop_target"])
def test_same_origin_can_confirm_legitimate_controller_progress(host: Host, phase: str) -> None:
    pending = host.pending()
    view = host.prepare(pending=pending)
    assert view.record is not None
    record = view.record
    with view.journal.locked() as control:
        if phase == "aborted":
            record = control.advance(record.position, Transition(phase="aborted", code="cancelled"))
        elif phase != "prepared":
            record = control.advance(record.position, Transition(phase="draining"))
            if phase != "draining":
                record = control.advance(
                    record.position, Transition(phase="activating", intent="stop_base")
                )
            if phase == "stop_target":
                record = control.advance(
                    record.position,
                    Transition(phase="rolling_back", intent="stop_target", code="health_failed"),
                )
    assert host.confirm(pending).record == record
    host.assert_owned()


@pytest.mark.parametrize("mutation", ["request", "generation", "action", "target", "deadline"])
def test_confirm_rejects_a_different_immutable_attempt(host: Host, mutation: str) -> None:
    pending = host.pending()
    view = host.prepare(pending=pending)
    if mutation == "request":
        other = replace(pending, request_id="another-request")
    elif mutation == "generation":
        other = replace(pending, generation=secrets.token_hex(16))
    elif mutation == "action":
        other = replace(pending, action="restart")
    elif mutation == "target":
        other = replace(pending, target=Settings(default_mode="yolo").model_dump_json().encode())
    else:
        other = replace(pending, deadline=pending.deadline.model_copy(update={"seconds": 70.0}))
    with pytest.raises(TransactionError, match="legacy_attempt_changed"):
        host.confirm(other)
    assert host.owner.journal.read_current() == view.record
    host.assert_owned()


@pytest.mark.parametrize(
    "mutation", ["receipt", "config", "unit", "selector", "snapshot", "manifest"]
)
def test_confirm_rechecks_the_actual_store_and_frozen_files(host: Host, mutation: str) -> None:
    pending = host.pending()
    view = host.prepare(pending=pending)
    assert view.record is not None
    root = host.owner.journal.directory.path
    if mutation == "receipt":
        host.store.db.execute("DELETE FROM objects WHERE kind='management_projection'")
        host.store.db.commit()
    elif mutation == "config":
        Path(pending.manifest.config_path).write_bytes(pending.target)
    elif mutation == "unit":
        Path(pending.manifest.unit_path).write_bytes(b"[Service]\nExecStart=/foreign\n")
    elif mutation == "selector":
        (root / "selector.json").write_bytes(host.recovered.selector.canonical())
    elif mutation == "snapshot":
        (root / f"{pending.generation}-target.json").write_bytes(b"{}")
    else:
        (root / "installation.json").write_bytes(b"{}")
    with pytest.raises((OSError, ValueError)):
        host.confirm(pending)
    assert host.owner.journal.read_current() == view.record
    host.assert_owned()


def test_confirm_rejects_a_target_decision_even_when_restart_configuration_is_unchanged(
    host: Host,
) -> None:
    from test_management_transaction import proof, verifying

    pending = host.pending(action="restart")
    view = host.prepare(pending=pending)
    assert view.record is not None
    with view.journal.locked() as control:
        verifying_record = verifying(control, view.record)
        decided = control.advance(
            verifying_record.position, Transition(phase="committed", proof=proof(verifying_record))
        )
    with pytest.raises(TransactionError, match="legacy_origin_not_permitted"):
        host.confirm(pending)
    assert host.owner.journal.read_current() == decided
    host.assert_owned()


def test_confirmation_does_not_authorize_a_consistently_rewritten_base(host: Host) -> None:
    pending = host.pending()
    view = host.prepare(pending=pending)
    assert view.record is not None
    journal = host.owner.journal
    foreign = Settings(default_mode="yolo").model_dump_json().encode()
    changed = view.record.model_copy(
        update={"base": _snapshot(foreign, journal.limits.max_snapshot_bytes)}
    )
    with journal.locked() as control:
        control._replace(f"{pending.generation}-base.json", foreign)
        Path(pending.manifest.config_path).write_bytes(foreign)
        control._write(changed)
    assert changed.generation == pending.generation and changed.target == view.record.target
    with pytest.raises(TransactionError, match="legacy_attempt_changed"):
        host.confirm(pending)
    assert journal.read_current() == changed
    assert journal.snapshot(changed, "base") == foreign
    assert Path(pending.manifest.config_path).read_bytes() == foreign
    host.assert_owned()


def test_prepare_refuses_a_configuration_change_after_authorization(host: Host) -> None:
    pending = host.pending()
    foreign = Settings(default_mode="yolo").model_dump_json().encode()
    Path(pending.manifest.config_path).write_bytes(foreign)
    with pytest.raises(TransactionError, match="configuration_changed"):
        host.prepare(pending=pending)
    assert host.owner.journal.read_current() == pending.previous
    assert Path(pending.manifest.config_path).read_bytes() == foreign
    host.assert_owned()
