from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_managed_boot import Host
from test_managed_boot import host as host

from muli import managed_runtime_binding
from muli.format_evidence import FormatEvidence
from muli.maintenance import BootPermit
from muli.managed_boot import ManagedBootPermit
from muli.managed_runtime_binding import RuntimeBindingError, verify_running_binding
from muli.managed_transaction import TransactionRecordV2, raw_snapshot
from muli.management_projection import ProjectionError, project_final, read_projection
from muli.management_transaction import ProjectionReceipt, Role
from muli.platforms import installation
from muli.platforms.installation import InstallationManifest
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.service import UserService
from muli.store import Store
from muli.system_events import attention_source

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed runtime binding")


@dataclass
class Running:
    host: Host
    permit: ManagedBootPermit
    store: Store
    record: TransactionRecordV2

    def verify(self, record: TransactionRecordV2, role: Role | None) -> None:
        verify_running_binding(self.permit, self.store, record, role)


@pytest.fixture
def running(host: Host, monkeypatch: pytest.MonkeyPatch) -> Iterator[Running]:
    record = host.record("committed", "none", activation="released")
    permit = host.acquire()
    monkeypatch.setattr(managed_runtime_binding, "verify_local_filesystems", lambda paths: ())
    with closing(permit.open_store()) as store:
        yield Running(host, permit, store, record)


def _plan(store: Store, record: TransactionRecordV2) -> dict[str, Any]:
    origin = ReplyOrigin.load(
        {
            "kind": "channel",
            "input_id": "original-input",
            "route": {
                "name": "telegram",
                "account_id": "original-account",
                "target": "owner",
                "sender_id": "owner",
                "kind": "private",
                "reply_to": "original-message",
            },
        }
    )
    assert origin is not None
    plan = {
        "id": record.request_id,
        "action": record.action,
        "status": "applying",
        "transaction": record.public(),
        "routing": FrozenRouting.bound(origin).dump(),
        "private_fixture": "preserved original metadata",
    }
    store.event("management.queued", plan, objects=[("management", plan)])
    return plan


def _completions(store: Store) -> list[dict[str, Any]]:
    return [event for event in store.events() if event["type"] == "management.completed"]


def test_binding_uses_the_existing_store_and_never_creates_main(running: Running) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("binding cannot create main identity")

    running.host.monkeypatch.setattr(running.store, "main_session", forbidden)
    before = running.store.db.total_changes
    running.verify(running.record, "target")
    assert running.store.db.total_changes == before
    assert not running.store.db.in_transaction


def test_previous_target_can_be_the_next_generation_origin(running: Running) -> None:
    previous = running.record
    record = TransactionRecordV2.model_validate(
        {
            **previous.model_dump(),
            "request_id": "next-restart",
            "generation": "b" * 32,
            "revision": 1,
            "action": "restart",
            "base": previous.target.model_dump(),
            "identity": {**previous.identity.model_dump(), "origin_boot": running.permit.boot},
            "phase": "prepared",
            "health": None,
            "activation": "pending",
        }
    )
    with running.host.journal.locked() as control:
        control._write(record)
    before = running.store.db.total_changes
    running.verify(record, None)
    assert running.store.db.total_changes == before
    assert (
        running.permit.role == "target" and running.permit.record.generation == previous.generation
    )
    record = TransactionRecordV2.model_validate(
        {
            **record.model_dump(),
            "revision": 2,
            "phase": "aborted",
            "code": "cancelled",
        }
    )
    with running.host.journal.locked() as control:
        control._write(record)
    plan = _plan(running.store, record)
    assert project_final(running.permit, running.store, verify_binding=running.verify) is not None
    assert running.store.get("management", record.request_id)["status"] == "cancelled"
    assert _completions(running.store)[0]["data"]["routing"] == plan["routing"]


def test_a_nonorigin_boot_cannot_claim_another_generations_same_slot(running: Running) -> None:
    previous = running.record
    assert previous.health is not None
    record = TransactionRecordV2.model_validate(
        {
            **previous.model_dump(),
            "request_id": "foreign-generation",
            "generation": "b" * 32,
            "health": {**previous.health.model_dump(), "generation": "b" * 32},
        }
    )
    with running.host.journal.locked() as control:
        control._write(record)
    with pytest.raises(RuntimeBindingError, match="runtime_binding_installation_changed"):
        running.verify(record, "target")


@pytest.mark.parametrize("change", ["role", "generation", "installation", "anchor", "boot"])
def test_a_live_permit_cannot_be_rebound_to_foreign_state(running: Running, change: str) -> None:
    record, role = running.record, "target"
    if change == "role":
        role = "base"
    elif change == "generation":
        record = TransactionRecordV2.model_validate(
            {
                **record.model_dump(),
                "generation": "b" * 32,
                "health": {**record.health.model_dump(), "generation": "b" * 32},
            }
        )
    elif change == "installation":
        running.permit.installation = "b" * 64
    elif change == "anchor":
        running.permit.anchor = running.permit.anchor.model_copy(update={"data_dir": "/foreign"})
    else:
        running.permit.boot = "not-the-origin"
        role = None
    before = running.store.db.total_changes
    with pytest.raises(RuntimeBindingError):
        running.verify(record, role)
    assert running.store.db.total_changes == before


@pytest.mark.parametrize(
    "change", ["configuration", "unit", "selector", "slot", "controller", "marker", "main"]
)
def test_current_files_format_and_main_remain_authoritative(running: Running, change: str) -> None:
    host = running.host
    if change in {"configuration", "unit"}:
        Path(
            host.anchor.config_path if change == "configuration" else host.anchor.unit_path
        ).write_bytes(b"changed")
    elif change == "selector":
        (host.journal.directory.path / "selector.json").unlink()
    elif change in {"slot", "controller"}:
        slot = host.target if change == "slot" else host.base
        Path(slot.package.root, "__init__.py").write_text("changed", encoding="utf-8")
    elif change == "marker":
        host.mutate("DELETE FROM runtime_state WHERE name='format_state_v1'")
    else:
        host.mutate(
            "UPDATE objects SET data=json_set(data,'$.principal','other') WHERE kind='session'"
        )
    before = running.store.db.total_changes
    with pytest.raises(RuntimeBindingError):
        running.verify(running.record, "target")
    assert running.store.db.total_changes == before


def test_a_foreign_store_cannot_borrow_the_live_permit(running: Running, tmp_path: Path) -> None:
    with closing(
        Store.open_runtime(tmp_path / "other/muli.sqlite3", default_mode="chat", durable=True)
    ) as other:
        before = other.db.total_changes
        with pytest.raises(RuntimeBindingError):
            verify_running_binding(running.permit, other, running.record, "target")
        assert other.db.total_changes == before


def test_files_are_rechecked_after_the_format_snapshot(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = managed_runtime_binding.read_format_evidence

    def changed(db: sqlite3.Connection) -> FormatEvidence:
        evidence = original(db)
        Path(running.host.anchor.config_path).write_bytes(b'{"server":{"port":9000}}')
        return evidence

    monkeypatch.setattr(managed_runtime_binding, "read_format_evidence", changed)
    with pytest.raises(RuntimeBindingError, match="runtime_binding_files_changed"):
        running.verify(running.record, "target")


def test_binding_does_not_adopt_a_visible_but_unconfirmed_decision(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync
    entered: list[bool] = []

    def unavailable(descriptor: int) -> None:
        if descriptor == running.host.journal.directory.fd:
            raise OSError("synthetic directory sync failure")
        original(descriptor)

    def forbidden(db: sqlite3.Connection) -> FormatEvidence:
        entered.append(True)
        raise AssertionError("unconfirmed state cannot inspect the business connection")

    monkeypatch.setattr(
        managed_runtime_binding, "os", SimpleNamespace(**{**vars(os), "fsync": unavailable})
    )
    monkeypatch.setattr(managed_runtime_binding, "read_format_evidence", forbidden)
    before = running.store.db.total_changes
    with pytest.raises(RuntimeBindingError, match="runtime_binding_verification_failed"):
        running.verify(running.record, "target")
    assert not entered and running.store.db.total_changes == before


@pytest.mark.parametrize(
    "outcome,activation,status",
    [
        ("committed", "released", "completed"),
        ("committed", "released_degraded", "completed"),
        ("rolled_back", "released", "failed"),
        ("aborted", "pending", "failed"),
    ],
)
def test_v2_projection_reuses_one_atomic_receipt_and_original_routing(
    host: Host, monkeypatch: pytest.MonkeyPatch, outcome: str, activation: str, status: str
) -> None:
    record = host.record(outcome, "none", activation=activation)
    permit = host.acquire()
    monkeypatch.setattr(managed_runtime_binding, "verify_local_filesystems", lambda paths: ())
    with closing(permit.open_store()) as store:
        plan = _plan(store, record)
        unknown = store.add_message(
            host.evidence.main_session,
            "tool",
            "unknown external operation",
            tool_call_id="unknown-call",
            status="unknown",
        )
        called: list[Role | None] = []

        def verify(current: TransactionRecordV2, role: Role | None) -> None:
            called.append(role)
            verify_running_binding(permit, store, current, role)

        receipt = project_final(permit, store, verify_binding=verify)
        assert receipt == ProjectionReceipt(
            **record.position.model_dump(), store_instance=host.evidence.store_instance
        )
        assert read_projection(store) == receipt
        final = store.get("management", record.request_id)
        assert final is not None and final["status"] == status
        assert final["transaction"] == record.public()
        assert (
            final["routing"] == plan["routing"]
            and final["private_fixture"] == plan["private_fixture"]
        )
        assert final["result"]["restored_previous"] == (outcome == "rolled_back")
        assert store.get("message", unknown["id"]) == unknown
        event = _completions(store)[0]
        source = attention_source(store, event, ["management"])
        assert source is not None and source.routing.dump() == plan["routing"]
        before = store.db.total_changes
        assert project_final(permit, store, verify_binding=verify) == receipt
        assert store.db.total_changes == before and len(_completions(store)) == 1
        assert called == ["target" if outcome == "committed" else "base"]


def test_new_v2_projection_requires_the_host_binding_callback(running: Running) -> None:
    plan = _plan(running.store, running.record)
    with pytest.raises(ProjectionError, match="projection_binding_required"):
        project_final(running.permit, running.store)
    assert running.store.get("management", plan["id"]) == plan
    assert read_projection(running.store) is None and not _completions(running.store)


def test_journal_changed_by_a_verifier_cannot_be_projected(running: Running) -> None:
    plan = _plan(running.store, running.record)

    def changed(record: TransactionRecordV2, role: Role | None) -> None:
        running.verify(record, role)
        updated = TransactionRecordV2.model_validate(
            {
                **record.model_dump(),
                "revision": record.revision + 1,
            }
        )
        with running.host.journal.locked() as control:
            control._write(updated)

    with pytest.raises(ProjectionError, match="projection_journal_changed"):
        project_final(running.permit, running.store, verify_binding=changed)
    assert running.store.get("management", plan["id"]) == plan
    assert read_projection(running.store) is None and not _completions(running.store)


@pytest.mark.parametrize(
    "phase,blocked",
    [("verifying", False), ("committed", False), ("committed", True), ("recovery_required", False)],
)
def test_unreleased_or_unresolved_v2_cannot_project_completion(
    host: Host, phase: str, blocked: bool
) -> None:
    record = host.record(phase, "none")
    if blocked:
        record = TransactionRecordV2.model_validate(
            {**record.model_dump(), "activation": "blocked"}
        )
        with host.journal.locked() as control:
            control._write(record)
    # The already-authenticated original host only holds the real writer here;
    # unresolved early phases deliberately cannot acquire a new boot permit.
    lease = host.journal.writer_lease().__enter__()
    permit = BootPermit(
        host.journal, Path(host.anchor.config_path), "1" * 32, "a" * 64, "b" * 64, lease, None, None
    )
    try:
        with closing(Store.open_existing(host.journal.database, durable=True)) as store:
            plan = _plan(store, record)

            def forbidden(current: TransactionRecordV2, role: Role | None) -> None:
                raise AssertionError("unresolved journal cannot write a final projection")

            assert project_final(permit, store, verify_binding=forbidden) is None
            assert store.get("management", plan["id"]) == plan
            assert read_projection(store) is None and not _completions(store)
    finally:
        permit.close_after_writers()


@pytest.mark.parametrize("stage", ["receipt", "event", "commit"])
def test_v2_projection_failure_rolls_back_all_three_writes(
    running: Running, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    store, record = running.store, running.record
    plan = _plan(store, record)
    original = store._put

    def fail_receipt(kind: str, item: dict[str, Any]) -> None:
        original(kind, item)
        if kind == "management_projection":
            raise RuntimeError("synthetic receipt failure")

    def deny_commit(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
            else sqlite3.SQLITE_OK
        )

    # Verify before taking over this dedicated fixture's authorizer; the format
    # reader owns/clears callbacks by contract and cannot preserve an unknown one.
    running.verify(record, "target")
    if stage == "receipt":
        monkeypatch.setattr(store, "_put", fail_receipt)
    elif stage == "event":
        store.db.execute(
            "CREATE TEMP TRIGGER fail_v2 BEFORE INSERT ON events WHEN NEW.type='management.completed' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    else:
        store.db.set_authorizer(deny_commit)
    try:
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            project_final(running.permit, store, verify_binding=lambda current, role: None)
    finally:
        store.db.set_authorizer(None)
        monkeypatch.setattr(store, "_put", original)
        if stage == "event":
            store.db.execute("DROP TRIGGER fail_v2")
    assert store.get("management", plan["id"]) == plan
    assert read_projection(store) is None and not _completions(store)
    assert not store.db.in_transaction
    assert project_final(running.permit, store, verify_binding=running.verify) is not None


@pytest.mark.parametrize("outcome,role", [("aborted", None), ("rolled_back", "base")])
def test_real_legacy_permit_projects_failed_adoption_without_a_published_manifest(
    host: Host, monkeypatch: pytest.MonkeyPatch, outcome: str, role: Role | None
) -> None:
    record = host.record(
        outcome,
        "none",
        legacy=True,
        activation="released" if outcome == "rolled_back" else "pending",
    )
    monkeypatch.setattr(
        installation,
        "sys",
        SimpleNamespace(
            platform="linux",
            executable=host.base.lexical_python,
            prefix=host.base.prefix,
            base_prefix=sys.base_prefix,
        ),
    )
    service = UserService(
        Path(host.anchor.config_path),
        Path(host.anchor.data_dir),
        Path(host.anchor.unit_path).parent,
        platform="linux",
        executable=host.base.lexical_python,
    )
    unit = service.render().encode("utf-8")
    Path(host.anchor.unit_path).write_bytes(unit)
    base = {
        **record.base.model_dump(),
        "unit": raw_snapshot(unit, host.journal.limits.max_snapshot_bytes),
    }
    values = {**record.model_dump(), "base": base}
    if record.health is not None:
        values["health"] = {**record.health.model_dump(), "unit": base["unit"].digest}
    record = TransactionRecordV2.model_validate(values)
    manifest = InstallationManifest.capture(
        service,
        host.journal.database,
        helper=host.anchor.helper,
        journal=host.anchor.journal,
        package_limits=host.anchor.package_limits,
    )
    # Build the real ordinary BootPermit before this fixture publishes its V2
    # adoption, just as an original running service owns its lease at prepare.
    (host.journal.directory.path / "state.json").unlink()
    permit = BootPermit.acquire(
        host.journal,
        Path(host.anchor.config_path),
        boot=record.identity.origin_boot if role is None else "2" * 32,
        installation=manifest.digest,
        service=manifest.service_digest,
    )
    try:
        with host.journal.locked() as control:
            control._write(record)
        assert not (host.journal.directory.path / "installation.json").exists()
        monkeypatch.setattr(managed_runtime_binding, "verify_local_filesystems", lambda paths: ())
        with closing(Store.open_existing(host.journal.database, durable=True)) as store:
            plan = _plan(store, record)
            before = store.db.total_changes
            verify_running_binding(permit, store, record, role)
            assert store.db.total_changes == before

            def verify(current: TransactionRecordV2, selected: Role | None) -> None:
                assert selected == role
                verify_running_binding(permit, store, current, selected)

            assert project_final(permit, store, verify_binding=verify) is not None
            assert store.get("management", plan["id"])["status"] == "failed"
            event = _completions(store)[0]
            assert event["data"]["routing"] == plan["routing"]
            source = attention_source(store, event, ["management"])
            assert source is not None and source.routing.dump() == plan["routing"]
            permit.installation = "e" * 64
            with pytest.raises(RuntimeBindingError, match="runtime_binding_installation_changed"):
                verify_running_binding(permit, store, record, role)
    finally:
        permit.close_after_writers()
