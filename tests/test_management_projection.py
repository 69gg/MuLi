from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from muli.maintenance import BootPermit
from muli.management_projection import (
    ProjectionError,
    project_final,
    read_projection,
    read_projection_connection,
)
from muli.management_transaction import (
    ClockReading,
    Deadline,
    HealthProof,
    ProjectionReceipt,
    ServiceIdentity,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store import Store
from muli.system_events import attention_source

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux recovery projection")
BASE = b'{"configuration":"base"}'
TARGET = b'{"configuration":"target"}'


@dataclass(slots=True)
class Host:
    store: Store
    permit: BootPermit
    plan: dict[str, Any]

    @property
    def journal(self) -> TransactionJournal:
        return self.permit.journal

    def move(self, *transitions: Transition) -> TransactionRecord:
        with self.journal.locked() as control:
            record = control.read()
            assert record is not None
            for transition in transitions:
                record = control.advance(record.position, transition)
            return record

    def verify_phase(self) -> TransactionRecord:
        return self.move(
            Transition(phase="draining"),
            Transition(phase="activating", intent="stop_base"),
            Transition(phase="activating", intent="install_target"),
            Transition(phase="activating", intent="start_target"),
            Transition(phase="verifying"),
        )

    def proof(
        self, record: TransactionRecord, *, base: bool = False, admission: str = "paused"
    ) -> HealthProof:
        return HealthProof(
            generation=record.generation,
            challenge=record.rollback_challenge if base else record.challenge,
            boot="recovered-base" if base else "new-target",
            installation=record.identity.installation,
            service=record.identity.service,
            store_instance=record.identity.store_instance,
            main_session=record.identity.main_session,
            configuration=(record.base if base else record.target).digest,
            core_ready=True,
            admission=admission,
        )

    def resolve(self, outcome: str) -> TransactionRecord:
        if outcome in {"cancelled", "aborted"}:
            return self.move(
                Transition(
                    phase="aborted", code="cancelled" if outcome == "cancelled" else "drain_timeout"
                )
            )
        record = self.verify_phase()
        base = outcome == "rolled_back"
        if base:
            record = self.move(
                Transition(phase="rolling_back", intent="stop_target", code="health_failed"),
                Transition(phase="rolling_back", intent="restore_base"),
                Transition(phase="rolling_back", intent="start_base"),
                Transition(phase="rolling_back", intent="verify_base"),
            )
        phase = "rolled_back" if base else "committed"
        record = self.move(Transition(phase=phase, proof=self.proof(record, base=base)))
        activation = "released_degraded" if outcome == "degraded" else "released"
        return self.move(
            Transition(
                phase=phase,
                activation=activation,
                proof=self.proof(record, base=base, admission=activation),
            )
        )


@pytest.fixture
def host(tmp_path: Path) -> Iterator[Host]:
    config = tmp_path / "config.json"
    config.write_bytes(BASE)
    database = tmp_path / "data/muli.sqlite3"
    store = Store(database)
    store.db.execute("PRAGMA synchronous=FULL")
    main = store.main_session()
    origin = ReplyOrigin.load(
        {
            "kind": "channel",
            "input_id": "original-input",
            "route": {
                "name": "telegram",
                "account_id": "old-account",
                "target": "owner",
                "sender_id": "owner",
                "kind": "private",
                "reply_to": "original-message",
            },
        }
    )
    with TransactionJournal(tmp_path / "journal", database) as journal:
        permit = BootPermit.acquire(
            journal, config, boot="origin", installation="a" * 64, service="b" * 64
        )
        try:
            with journal.locked() as control:
                record = control.prepare(
                    request_id="original-plan",
                    action="configure",
                    identity=ServiceIdentity(
                        installation=permit.installation,
                        service=permit.service,
                        store_instance=store.instance_id,
                        main_session=main["id"],
                        origin_boot=permit.boot,
                    ),
                    base=BASE,
                    target=TARGET,
                    deadline=Deadline(
                        created=ClockReading(boot_id="kernel", wall=1.0, monotonic=1.0),
                        seconds=180.0,
                        rollback_reserve=60.0,
                    ),
                    writer=permit.lease,
                )
            plan = {
                "id": record.request_id,
                "action": record.action,
                "status": "applying",
                "created_at": 1.0,
                "routing": FrozenRouting.bound(origin).dump(),
                "transaction": record.public(),
                "changed_sections": ["server"],
                "opaque": "preserve original plan metadata",
            }
            store.event("management.queued", plan, objects=[("management", plan)])
            yield Host(store, permit, plan)
        finally:
            store.close()
            permit.close_after_writers()


def completions(store: Store) -> list[dict[str, Any]]:
    return [event for event in store.events() if event["type"] == "management.completed"]


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("committed", "completed"),
        ("degraded", "completed"),
        ("rolled_back", "failed"),
        ("cancelled", "cancelled"),
        ("aborted", "failed"),
    ],
)
def test_final_projection_is_atomic_canonical_and_idempotent(
    host: Host, outcome: str, status: str
) -> None:
    record = host.resolve(outcome)
    # Startup cache is intentionally stale; only a fresh journal read is usable.
    assert host.permit.record is None
    receipt = project_final(host.permit, host.store)
    assert receipt == ProjectionReceipt(
        **record.position.model_dump(), store_instance=record.identity.store_instance
    )
    assert read_projection(host.store) == receipt
    item = host.store.get("management", host.plan["id"])
    assert item is not None
    assert item["status"] == status
    assert item["routing"] == host.plan["routing"]
    assert item["opaque"] == host.plan["opaque"]
    assert item["transaction"] == record.public()
    assert item["result"]["restored_previous"] == (outcome == "rolled_back")
    event = completions(host.store)[0]
    assert event["data"] == item and event["session_id"] is None
    source = attention_source(host.store, event, ["management"])
    if status == "cancelled":
        assert source is None
    else:
        assert source is not None
        assert source.routing.dump() == host.plan["routing"]
    after = host.store.event_position()
    assert project_final(host.permit, host.store) == receipt
    assert host.store.event_position() == after
    assert len(completions(host.store)) == 1


@pytest.mark.parametrize(
    "phase", ["prepared", "verifying", "unreleased", "blocked", "recovery_required"]
)
def test_unresolved_records_never_emit_completion(host: Host, phase: str) -> None:
    if phase == "verifying":
        host.verify_phase()
    elif phase in {"unreleased", "blocked"}:
        record = host.verify_phase()
        host.move(Transition(phase="committed", proof=host.proof(record)))
        if phase == "blocked":
            host.move(Transition(phase="committed", activation="blocked", code="activation_failed"))
    elif phase == "recovery_required":
        host.move(Transition(phase="recovery_required", code="backend_failed"))
    assert project_final(host.permit, host.store) is None
    assert read_projection(host.store) is None
    assert not completions(host.store)
    assert host.store.get("management", host.plan["id"])["status"] == "applying"


def test_no_permit_does_not_touch_state(host: Host) -> None:
    assert project_final(None, host.store) is None
    assert host.store.get("management", host.plan["id"]) == host.plan


@pytest.mark.parametrize(
    "field", ["id", "action", "generation", "missing_generation", "missing_plan"]
)
def test_plan_must_match_request_action_and_generation(host: Host, field: str) -> None:
    host.resolve("committed")
    plan = dict(host.plan)
    if field == "id":
        with host.store.db:
            host.store.db.execute(
                "UPDATE objects SET data=json_set(data,'$.id','another-request') "
                "WHERE kind='management' AND id=?",
                (host.plan["id"],),
            )
    elif field == "missing_plan":
        host.store.delete("management", plan["id"])
    else:
        if field == "action":
            plan["action"] = "restart"
        elif field == "generation":
            plan["transaction"] = {**plan["transaction"], "generation": "another-generation"}
        else:
            plan.pop("transaction")
        host.store.put("management", plan)
    with pytest.raises(ProjectionError, match="projection_plan_mismatch"):
        project_final(host.permit, host.store)
    assert read_projection(host.store) is None
    assert not completions(host.store)


@pytest.mark.parametrize("field", ["store_instance_v1", "main_session"])
def test_actual_runtime_state_is_required_without_creating_main(
    host: Host, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    host.resolve("committed")
    with host.store.db:
        host.store.db.execute("DELETE FROM runtime_state WHERE name=?", (field,))
    before = host.store.event_position()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not create or use cached main identity")

    monkeypatch.setattr(host.store, "main_session", forbidden)
    with pytest.raises(ProjectionError, match="projection_store_identity_changed"):
        project_final(host.permit, host.store)
    assert host.store.event_position() == before
    assert (
        host.store.db.execute("SELECT value FROM runtime_state WHERE name=?", (field,)).fetchone()
        is None
    )


def test_same_store_ids_in_another_database_do_not_prove_path(host: Host, tmp_path: Path) -> None:
    host.resolve("committed")
    other = Store(tmp_path / "other.sqlite3")
    try:
        other.db.execute("PRAGMA synchronous=FULL")
        for row in host.store.db.execute(
            "SELECT name,value FROM runtime_state WHERE name IN ('main_session','store_instance_v1')"
        ):
            with other.db:
                other.db.execute(
                    "INSERT OR REPLACE INTO runtime_state(name,value) VALUES(?,?)", tuple(row)
                )
        original_main = host.store.get("session", host.journal.read().identity.main_session)
        assert original_main is not None
        other.put("session", original_main)
        with pytest.raises(ProjectionError, match="projection_store_identity_changed"):
            project_final(host.permit, other)
        assert not completions(other)
    finally:
        other.close()


def test_missing_or_foreign_writer_lease_is_rejected(host: Host, tmp_path: Path) -> None:
    host.resolve("committed")
    with TransactionJournal(tmp_path / "other-journal", tmp_path / "other-data/muli.db") as other:
        with other.writer_lease() as lease:
            original = host.permit.lease
            host.permit.lease = lease
            try:
                with pytest.raises(ProjectionError, match="projection_writer_lease_required"):
                    project_final(host.permit, host.store)
            finally:
                host.permit.lease = original


@pytest.mark.parametrize("mode", ["normal", "off", "delete", "autocommit", "no_isolation"])
def test_unsupported_sqlite_durability_is_not_silently_changed(host: Host, mode: str) -> None:
    host.resolve("committed")
    if mode == "normal":
        host.store.db.execute("PRAGMA synchronous=NORMAL")
    elif mode == "off":
        host.store.db.execute("PRAGMA synchronous=OFF")
    elif mode == "delete":
        host.store.db.execute("PRAGMA journal_mode=DELETE")
    elif mode == "autocommit":
        host.store.db.autocommit = True
    else:
        host.store.db.isolation_level = None
    before = (
        host.store.db.execute("PRAGMA journal_mode").fetchone()[0],
        host.store.db.execute("PRAGMA synchronous").fetchone()[0],
        host.store.db.autocommit,
        host.store.db.isolation_level,
    )
    with pytest.raises(ProjectionError, match="unsupported"):
        project_final(host.permit, host.store)
    with pytest.raises(ProjectionError, match="unsupported"):
        read_projection(host.store)
    after = (
        host.store.db.execute("PRAGMA journal_mode").fetchone()[0],
        host.store.db.execute("PRAGMA synchronous").fetchone()[0],
        host.store.db.autocommit,
        host.store.db.isolation_level,
    )
    assert before == after
    assert not completions(host.store)


def test_outer_transaction_is_neither_committed_nor_rolled_back(host: Host) -> None:
    host.resolve("committed")
    host.store.db.execute("INSERT INTO runtime_state(name,value) VALUES('caller-pending','keep')")
    with pytest.raises(ProjectionError, match="projection_outer_transaction"):
        project_final(host.permit, host.store)
    with pytest.raises(ProjectionError, match="projection_outer_transaction"):
        read_projection(host.store)
    with pytest.raises(ProjectionError, match="projection_outer_transaction"):
        read_projection_connection(host.store.db)
    assert host.store.db.in_transaction
    assert (
        host.store.db.execute(
            "SELECT value FROM runtime_state WHERE name='caller-pending'"
        ).fetchone()[0]
        == "keep"
    )
    host.store.db.rollback()
    assert (
        host.store.db.execute(
            "SELECT value FROM runtime_state WHERE name='caller-pending'"
        ).fetchone()
        is None
    )
    assert read_projection(host.store) is None


def test_connection_reader_reuses_the_fixed_receipt_without_owning_the_connection(
    host: Host,
) -> None:
    host.resolve("committed")
    receipt = project_final(host.permit, host.store)
    assert receipt is not None
    before = host.store.db.total_changes
    assert read_projection_connection(host.store.db) == receipt
    assert host.store.db.total_changes == before
    assert not host.store.db.in_transaction
    host.store.put("private_fixture", {"id": "still-open"})
    assert host.store.get("private_fixture", "still-open") == {"id": "still-open"}


@pytest.mark.parametrize("stage", ["receipt", "event", "commit"])
def test_failed_write_rolls_back_plan_receipt_and_completion(
    host: Host, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    host.resolve("committed")
    original = host.store._put

    def failed_put(kind: str, item: dict[str, Any]) -> None:
        original(kind, item)
        if kind == "management_projection":
            raise RuntimeError("injected receipt write failure")

    def deny_commit(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
            else sqlite3.SQLITE_OK
        )

    if stage == "receipt":
        monkeypatch.setattr(host.store, "_put", failed_put)
    elif stage == "event":
        host.store.db.execute(
            "CREATE TEMP TRIGGER projection_fail BEFORE INSERT ON events "
            "WHEN NEW.type='management.completed' BEGIN SELECT RAISE(ABORT,'injected event failure'); END"
        )
    else:
        host.store.db.set_authorizer(deny_commit)
    try:
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            project_final(host.permit, host.store)
    finally:
        host.store.db.set_authorizer(None)
        monkeypatch.setattr(host.store, "_put", original)
        if stage == "event":
            host.store.db.execute("DROP TRIGGER projection_fail")
    assert not host.store.db.in_transaction
    assert host.store.get("management", host.plan["id"]) == host.plan
    assert read_projection(host.store) is None
    assert not completions(host.store)
    assert project_final(host.permit, host.store) is not None
    assert len(completions(host.store)) == 1


@pytest.mark.parametrize("change", ["fsync_failure", "revision_change"])
def test_journal_durability_barrier_precedes_database_changes(
    host: Host, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    record = host.resolve("cancelled")
    original = os.fsync
    changed = False

    def sync(descriptor: int) -> None:
        nonlocal changed
        if descriptor == host.journal.directory.fd and not changed:
            changed = True
            if change == "fsync_failure":
                raise OSError("injected journal sync failure")
            original(descriptor)
            with host.journal.locked() as control:
                control._write(record.model_copy(update={"revision": record.revision + 1}))
        else:
            original(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises((OSError, ProjectionError)):
        project_final(host.permit, host.store)
    assert changed
    assert host.store.get("management", host.plan["id"]) == host.plan
    assert read_projection(host.store) is None
    assert not completions(host.store)


@pytest.mark.parametrize("delta", [-1, 1])
def test_same_generation_nonexact_receipt_is_never_replaced(host: Host, delta: int) -> None:
    record = host.resolve("committed")
    receipt = ProjectionReceipt(
        generation=record.generation,
        revision=record.revision + delta,
        store_instance=record.identity.store_instance,
    )
    host.store.put("management_projection", {"id": "transaction-v1", **receipt.model_dump()})
    with pytest.raises(ProjectionError, match="projection_revision_conflict"):
        project_final(host.permit, host.store)
    assert read_projection(host.store) == receipt
    assert not completions(host.store)


def test_new_generation_replaces_only_validated_previous_receipt(host: Host) -> None:
    host.resolve("committed")
    previous = ProjectionReceipt(
        generation="old-generation", revision=5, store_instance=host.store.instance_id
    )
    host.store.put("management_projection", {"id": "transaction-v1", **previous.model_dump()})
    projected = project_final(host.permit, host.store)
    assert projected is not None and projected.generation != previous.generation
    assert read_projection(host.store) == projected
    assert len(completions(host.store)) == 1


def test_exact_receipt_allows_manifest_successor_before_its_prepare(host: Host) -> None:
    host.resolve("committed")
    receipt = project_final(host.permit, host.store)
    before = host.store.event_position()
    host.permit.installation = "c" * 64
    host.permit.service = "d" * 64
    assert project_final(host.permit, host.store) == receipt
    assert host.store.event_position() == before


def test_new_projection_requires_current_permit_installation(host: Host) -> None:
    host.resolve("committed")
    host.permit.installation = "c" * 64
    with pytest.raises(ProjectionError, match="projection_installation_changed"):
        project_final(host.permit, host.store)
    assert read_projection(host.store) is None


@pytest.mark.parametrize("bad", ["extra", "revision", "store", "large"])
def test_invalid_receipt_is_not_overwritten_or_returned(host: Host, bad: str) -> None:
    record = host.resolve("committed")
    value = {
        "id": "transaction-v1",
        "generation": record.generation,
        "revision": record.revision,
        "store_instance": record.identity.store_instance,
    }
    if bad == "extra":
        value["extra"] = "not a receipt"
    elif bad == "revision":
        value["revision"] = "1"
    elif bad == "store":
        value["store_instance"] = "foreign-store"
    else:
        value["extra"] = "x" * 5000
    host.store.put("management_projection", value)
    with pytest.raises(ProjectionError):
        read_projection(host.store)
    with pytest.raises(ProjectionError):
        project_final(host.permit, host.store)
    assert not completions(host.store)


def test_tool_unknown_receipt_is_not_rewritten(host: Host) -> None:
    host.resolve("rolled_back")
    record = host.journal.read()
    assert record is not None
    message = {
        "id": "existing-tool-receipt",
        "session_id": record.identity.main_session,
        "role": "tool",
        "content": json.dumps({"status": "unknown"}),
        "tool_call_id": "old-call",
        "created_at": 1.0,
    }
    stored = dict(host.store.put("message", message))
    project_final(host.permit, host.store)
    assert host.store.get("message", message["id"]) == stored
