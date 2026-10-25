from __future__ import annotations

import errno
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import muli
from muli import managed_boot, management_transaction
from muli.config import ConfigFile
from muli.format_admission import CURRENT_FORMAT_STATE
from muli.format_evidence import FormatEvidenceError, read_format_evidence
from muli.managed_boot import ManagedBootError, ManagedBootPermit
from muli.managed_transaction import (
    HealthProofV2,
    RoleBinding,
    ServiceIdentityV2,
    TransactionRecordV2,
    format_digest,
    raw_snapshot,
)
from muli.management_transaction import (
    ClockReading,
    Deadline,
    ProjectionReceipt,
    Role,
    TransactionError,
    TransactionJournal,
)
from muli.management_transaction import (
    _snapshot as json_snapshot,
)
from muli.platforms import managed_deployment
from muli.platforms.filesystem_identity import FilesystemError
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    EntryPlan,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    register_local_slot,
    resolve_entry,
)
from muli.store import Store
from muli.store_initialization import StoreConstructionLock, StoreInitializationError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed writer admission")
_CONFIG = b'{"server":{"port":8766}}'
_UNIT = b"[Service]\nExecStart=/retained/launcher\n"
_LEGACY_UNIT = b"[Service]\nExecStart=/previous/service\n"
_BOOT = "2" * 32
_ORIGIN = "1" * 32


def _slot(root: Path, name: str) -> SlotIdentity:
    prefix = root / name
    python = prefix / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic interpreter, never executed")
    python.chmod(0o700)
    (prefix / "pyvenv.cfg").write_text("home = /synthetic\n", encoding="utf-8")
    package = prefix / "lib/muli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "compatibility.json").write_text(
        json.dumps(
            {
                "metadata_version": 1,
                "distribution": "muli-agent",
                "release": "2.22.0",
                "handoff_protocol": 2,
                "requires_migration": False,
                "formats": {
                    name: {"reads": [version], "writes": version}
                    for name, version in CURRENT_FORMAT_STATE.formats.model_dump().items()
                },
            }
        ),
        encoding="utf-8",
    )
    return register_local_slot(prefix, python, package)


@dataclass
class Host:
    journal: TransactionJournal
    anchor: DeploymentAnchor
    base: SlotIdentity
    target: SlotIdentity
    evidence: managed_boot.FormatEvidence
    monkeypatch: pytest.MonkeyPatch
    permits: list[ManagedBootPermit] = field(default_factory=list)

    def record(
        self,
        phase: str = "activating",
        intent: str = "start_target",
        *,
        revision: int = 1,
        activation: str = "pending",
        legacy: bool = False,
    ) -> TransactionRecordV2:
        target = self.base if legacy else self.target
        limit = self.journal.limits.max_snapshot_bytes

        def binding(slot: SlotIdentity, *, old: bool = False) -> RoleBinding:
            return RoleBinding(
                entry="legacy" if old else "managed",
                installation=slot.digest,
                compatibility=slot.compatibility_digest,
                configuration=json_snapshot(_CONFIG, limit),
                selector=None
                if old
                else json_snapshot(
                    SelectorValue(
                        deployment=self.anchor.digest, installation=slot.digest
                    ).canonical(),
                    limit,
                ),
                unit=raw_snapshot(_LEGACY_UNIT if old else _UNIT, limit),
            )

        base, candidate = binding(self.base, old=legacy), binding(target)
        selected_role: Role = (
            "base" if phase in {"rolling_back", "rolled_back", "aborted"} else "target"
        )
        selected = base if selected_role == "base" else candidate
        rollback = phase in {"rolling_back", "rolled_back"}
        health = None
        if phase in {"committed", "rolled_back"}:
            health = HealthProofV2(
                generation="a" * 32,
                challenge="rollback" if rollback else "target",
                boot=_BOOT,
                role=selected_role,
                deployment=self.anchor.digest,
                installation=selected.installation,
                compatibility=selected.compatibility,
                configuration=selected.configuration.digest,
                selector=selected.selector.digest if selected.selector else None,
                unit=selected.unit.digest,
                store_instance=self.evidence.store_instance,
                main_session=self.evidence.main_session,
                format_digest=format_digest(self.evidence.state),
                core_ready=True,
                admission=activation if activation != "pending" else "paused",
            )
        record = TransactionRecordV2.model_validate(
            {
                "request_id": "request",
                "generation": "a" * 32,
                "revision": revision,
                "action": "adopt" if legacy else "switch",
                "identity": ServiceIdentityV2(
                    deployment=self.anchor.digest,
                    store_instance=self.evidence.store_instance,
                    main_session=self.evidence.main_session,
                    origin_boot=_ORIGIN,
                ),
                "base": base,
                "target": candidate,
                "observed_format": self.evidence.state,
                "deadline": Deadline(
                    created=ClockReading(boot_id="kernel", wall=1.0, monotonic=1.0),
                    seconds=180.0,
                    rollback_reserve=60.0,
                ),
                "phase": phase,
                "intent": intent,
                "challenge": "target",
                "rollback_challenge": "rollback" if rollback else None,
                "rollback_attempted": rollback,
                "health": health,
                "activation": activation,
            }
        )
        with self.journal.locked() as control:
            control._write(record)
        with SelectorStore(self.journal.directory.path) as selector:
            selector.replace(
                selector.read(),
                None
                if selected.selector is None
                else SelectorValue(
                    deployment=self.anchor.digest,
                    installation=selected.installation,
                ),
                guard=lambda: None,
            )
        Path(self.anchor.config_path).write_bytes(_CONFIG)
        Path(self.anchor.unit_path).write_bytes(
            _LEGACY_UNIT if legacy and selected_role == "base" else _UNIT
        )
        slot = self.base if selected_role == "base" or legacy else self.target
        # Patch only this module's sys reference; never change the real process platform.
        self.monkeypatch.setattr(
            managed_deployment,
            "sys",
            SimpleNamespace(
                platform="linux",
                executable=slot.lexical_python,
                prefix=slot.prefix,
                base_prefix="/base-python",
            ),
        )
        self.monkeypatch.setattr(muli, "__file__", str(Path(slot.package.root) / "__init__.py"))
        return record

    def plan(self) -> EntryPlan:
        return resolve_entry(self.journal.directory.path, purpose="service")

    def acquire(self, plan: EntryPlan | None = None, *, boot: str = _BOOT) -> ManagedBootPermit:
        permit = ManagedBootPermit.acquire(
            self.journal,
            boot=boot,
            plan=plan if plan is not None else self.plan(),
            lease=self.journal.writer_lease().__enter__(),
        )
        self.permits.append(permit)
        return permit

    def mutate(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        with closing(sqlite3.connect(self.journal.database)) as db, db:
            db.execute(sql, parameters)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    database = tmp_path / "data/muli.sqlite3"
    store = Store.open_runtime(database, default_mode="auto")
    try:
        evidence = read_format_evidence(store.db)
    finally:
        store.close()
    base, target = _slot(tmp_path, "base"), _slot(tmp_path, "target")
    config, unit = tmp_path / "config.json", tmp_path / "units/muli.service"
    config.write_bytes(_CONFIG)
    config.chmod(0o600)
    unit.parent.mkdir()
    unit.write_bytes(_UNIT)
    unit.chmod(0o600)
    journal = TransactionJournal(tmp_path / "journal", database)
    try:
        with DeploymentStore(journal.directory.path) as deployment:
            deployment.publish_slot(base, guard=lambda: None)
            deployment.publish_slot(target, guard=lambda: None)
            launcher = deployment.publish_launcher(guard=lambda: None)
            anchor = DeploymentAnchor(
                config_path=str(config),
                data_dir=str(database.parent),
                database_path=str(database),
                unit_path=str(unit),
                journal_root=str(journal.directory.path),
                controller_slot=base.digest,
                launcher=launcher,
                managed_unit=raw_snapshot(_UNIT, 16384),
            )
            deployment.publish_anchor(anchor, guard=lambda: None)
        result = Host(journal, anchor, base, target, evidence, monkeypatch)
        monkeypatch.setattr(managed_boot, "verify_local_filesystems", lambda paths: ())
        result.record()
        yield result
    finally:
        if "result" in locals():
            for permit in result.permits:
                # Only test teardown bypasses sticky failure, after forcibly closing
                # these synthetic connections. Production retains them until exit.
                if permit._unclosed is not None:
                    db = (
                        permit._unclosed.db
                        if isinstance(permit._unclosed, Store)
                        else permit._unclosed
                    )
                    sqlite3.Connection.close(db)
                    if isinstance(permit._unclosed, Store):
                        # The connection is now forcibly closed; release its new
                        # Runtime lock only in this synthetic-failure teardown.
                        lock = permit._unclosed._runtime_lock
                        if lock is not None:
                            lock.__exit__()
                            permit._unclosed._runtime_lock = None
                permit._cleanup_failed = False
                permit.close_after_writers()
        journal.__exit__()


def _snapshot(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return tuple(db.iterdump())


def test_verified_config_bytes_never_read_or_default_the_path(tmp_path: Path) -> None:
    path = tmp_path / "missing/config.json"
    config = ConfigFile.from_bytes(path, _CONFIG)
    assert config.path == path and config.settings.server.port == 8766
    assert not path.parent.exists()
    with pytest.raises(ValueError):
        ConfigFile.from_bytes(path, b"not configuration JSON")


def test_real_existing_store_is_read_only_until_returned_then_accepts_business_writes(
    host: Host,
) -> None:
    before = _snapshot(host.journal.database)
    permit = host.acquire()
    assert permit.probation and permit.role == "target"
    assert permit.config.settings.server.port == 8766
    assert permit.record.observed_format == host.evidence.state
    with closing(permit.open_store()) as store:
        assert not store.migration_allowed and store.db.total_changes == 0
        assert read_format_evidence(store.db) == host.evidence
        assert _snapshot(host.journal.database) == before
        store.put("test_private", {"id": "business", "value": "after admission"})
        assert store.get("test_private", "business")["value"] == "after admission"
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            Store.open_runtime(host.journal.database, default_mode="auto")
    with StoreConstructionLock(host.journal.database) as released:
        released.check()
    # Closing the Store releases only its Runtime lock, not the boot writer.
    with pytest.raises(TransactionError, match="lock_busy"):
        with host.journal.writer_lease():
            pass
    with pytest.raises(ManagedBootError, match="managed_store_already_opened"):
        permit.open_store()
    permit.close_after_writers()
    permit.close_after_writers()
    with host.journal.writer_lease():
        pass


def test_runtime_owner_busy_is_a_closed_factory_failure_not_a_sticky_boot_owner(
    host: Host,
) -> None:
    with closing(
        Store.open_runtime(host.journal.database, default_mode="auto", durable=True)
    ) as store:
        permit = host.acquire()
        with pytest.raises(ManagedBootError, match="managed_store_unavailable"):
            permit.open_store()
        assert not permit._cleanup_failed and permit._unclosed is None
        assert store.db.execute("SELECT 1").fetchone()[0] == 1
        permit.close_after_writers()
        with host.journal.writer_lease():
            pass
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            Store.open_runtime(host.journal.database, default_mode="auto")


def test_entry_can_legally_advance_before_or_during_admission(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = host.plan()
    host.record("verifying", "none", revision=2)
    original = managed_boot.read_format_evidence

    def advance(db: sqlite3.Connection) -> managed_boot.FormatEvidence:
        evidence = original(db)
        host.record("verifying", "none", revision=3)
        return evidence

    monkeypatch.setattr(managed_boot, "read_format_evidence", advance)
    permit = host.acquire(plan)
    assert permit.record.revision == 3 and permit.probation


def test_a_fresh_read_cannot_go_backwards_even_above_the_entry_revision(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = host.plan()
    host.record("verifying", "none", revision=3)
    original = managed_boot.read_format_evidence

    def reversed_revision(db: sqlite3.Connection) -> managed_boot.FormatEvidence:
        evidence = original(db)
        host.record("verifying", "none", revision=2)
        return evidence

    monkeypatch.setattr(managed_boot, "read_format_evidence", reversed_revision)
    with pytest.raises(ManagedBootError, match="managed_record_changed"):
        host.acquire(plan)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "b" * 32),
        ("deployment", "b" * 64),
        ("installation", "b" * 64),
        ("role", "base"),
        ("revision", 2),
        ("state_digest", "b" * 64),
    ],
)
def test_entry_suggestion_cannot_authorize_another_record(
    host: Host, field: str, value: object
) -> None:
    plan = replace(host.plan(), **{field: value})
    with pytest.raises(ManagedBootError, match="managed_entry_changed"):
        host.acquire(plan)
    with host.journal.writer_lease():
        pass


@pytest.mark.parametrize(
    "phase,intent",
    [
        ("prepared", "none"),
        ("draining", "none"),
        ("activating", "stop_base"),
        ("activating", "install_target"),
        ("rolling_back", "stop_target"),
        ("rolling_back", "restore_base"),
        ("recovery_required", "none"),
    ],
)
def test_unresolved_nonboot_phases_do_not_open_the_database(
    host: Host, monkeypatch: pytest.MonkeyPatch, phase: str, intent: str
) -> None:
    plan = host.plan()
    host.record(phase, intent, revision=2)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("No SQLite connection is allowed in this phase")

    monkeypatch.setattr(
        managed_boot, "sqlite3", SimpleNamespace(connect=forbidden, Error=sqlite3.Error)
    )
    with pytest.raises(ManagedBootError, match="managed_boot_not_permitted"):
        host.acquire(plan)


def test_origin_boot_is_not_new_target_health(host: Host) -> None:
    with pytest.raises(ManagedBootError, match="managed_boot_not_permitted"):
        host.acquire(boot=_ORIGIN)


@pytest.mark.parametrize(
    "phase,activation,legacy,role",
    [
        ("committed", "released", False, "target"),
        ("rolled_back", "released_degraded", False, "base"),
        ("aborted", "pending", False, "base"),
        ("aborted", "pending", True, "base"),
    ],
)
def test_resolved_records_admit_only_the_terminal_role(
    host: Host, phase: str, activation: str, legacy: bool, role: str
) -> None:
    host.record(phase, "none", activation=activation, legacy=legacy)
    permit = host.acquire(boot=_ORIGIN)
    assert permit.role == role and not permit.probation
    with closing(permit.open_store()) as store:
        assert store.db.total_changes == 0


def _receipt(host: Host, record: TransactionRecordV2, **changes: object) -> None:
    value = {
        "id": "transaction-v1",
        **ProjectionReceipt(
            **record.position.model_dump(), store_instance=record.identity.store_instance
        ).model_dump(),
        **changes,
    }
    host.mutate(
        "INSERT OR REPLACE INTO objects(kind,id,data,updated) VALUES(?,?,?,?)",
        ("management_projection", "transaction-v1", json.dumps(value), 1.0),
    )


@pytest.mark.parametrize(
    "phase,activation,legacy",
    [
        ("committed", "released", False),
        ("rolled_back", "released_degraded", False),
        ("aborted", "pending", False),
        ("aborted", "pending", True),
    ],
)
def test_projected_terminal_boot_uses_current_config_without_mutating_history(
    host: Host, phase: str, activation: str, legacy: bool
) -> None:
    record = host.record(phase, "none", activation=activation, legacy=legacy)
    _receipt(host, record)
    current = b'{"server":{"port":8767}}'
    Path(host.anchor.config_path).write_bytes(current)
    before = _snapshot(host.journal.database)
    permit = host.acquire(boot=_ORIGIN)
    assert not permit.probation and permit.config.settings.server.port == 8767
    with closing(permit.open_store()) as store:
        assert store.db.total_changes == 0
        assert permit.config.settings.server.port == 8767
    assert _snapshot(host.journal.database) == before
    assert host.journal.read_current() == record
    assert record.binding(permit.role).configuration == json_snapshot(
        _CONFIG, host.journal.limits.max_snapshot_bytes
    )


@pytest.mark.parametrize(
    "change", ["missing", "generation", "revision", "store_instance", "extra", "json", "oversize"]
)
def test_config_drift_requires_the_same_connections_exact_terminal_receipt(
    host: Host, change: str
) -> None:
    record = host.record("committed", "none", activation="released")
    if change != "missing":
        values: dict[str, object] = {
            "generation": "b" * 32,
            "revision": record.revision + 1,
            "store_instance": "different-instance",
            "extra": "not part of the receipt",
        }
        _receipt(host, record, **({change: values[change]} if change in values else {}))
        if change in {"json", "oversize"}:
            host.mutate(
                "UPDATE objects SET data=? WHERE kind='management_projection' AND id='transaction-v1'",
                ("not JSON" if change == "json" else " " * 4097,),
            )
    Path(host.anchor.config_path).write_bytes(b'{"server":{"port":8767}}')
    before = _snapshot(host.journal.database)
    with pytest.raises(ManagedBootError):
        host.acquire()
    assert _snapshot(host.journal.database) == before
    with host.journal.writer_lease():
        pass


@pytest.mark.parametrize("phase", ["verifying", "committed"])
def test_even_a_matching_receipt_does_not_relax_unresolved_configuration(
    host: Host, phase: str
) -> None:
    record = host.record(phase, "none", activation="pending")
    _receipt(host, record)
    Path(host.anchor.config_path).write_bytes(b'{"server":{"port":8767}}')
    with pytest.raises(ManagedBootError, match="managed_files_changed"):
        host.acquire()


def test_config_must_stay_exact_between_acquire_and_store_handoff(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.record("committed", "none", activation="released")
    _receipt(host, record)
    first = b'{"server":{"port":8767}}'
    Path(host.anchor.config_path).write_bytes(first)
    permit = host.acquire()
    config = permit.config
    Path(host.anchor.config_path).write_bytes(b'{"server":{"port":8768}}')

    def forbidden(*args: Any, **kwargs: Any) -> Store:
        raise AssertionError("changed startup config must fail before opening a business Store")

    monkeypatch.setattr(managed_boot.Store, "open_existing", forbidden)
    with pytest.raises(ManagedBootError, match="managed_configuration_changed"):
        permit.open_store()
    assert permit.config is config and config.settings.server.port == 8767


def test_receipt_is_rechecked_on_the_returned_store_connection(host: Host) -> None:
    record = host.record("committed", "none", activation="released")
    _receipt(host, record)
    Path(host.anchor.config_path).write_bytes(b'{"server":{"port":8767}}')
    permit = host.acquire()
    host.mutate("DELETE FROM objects WHERE kind='management_projection' AND id='transaction-v1'")
    with pytest.raises(ManagedBootError, match="managed_configuration_unprojected"):
        permit.open_store()


def test_projected_config_cannot_change_during_the_admission_snapshot(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.record("committed", "none", activation="released")
    _receipt(host, record)
    Path(host.anchor.config_path).write_bytes(b'{"server":{"port":8767}}')
    original = managed_boot.read_format_evidence

    def changed(db: sqlite3.Connection) -> managed_boot.FormatEvidence:
        evidence = original(db)
        Path(host.anchor.config_path).write_bytes(b'{"server":{"port":8768}}')
        return evidence

    monkeypatch.setattr(managed_boot, "read_format_evidence", changed)
    with pytest.raises(ManagedBootError, match="managed_record_changed"):
        host.acquire()


def test_rollback_does_not_require_the_failed_target_package(host: Host) -> None:
    host.record("rolling_back", "start_base", revision=2)
    Path(host.target.package.root, "__init__.py").write_text("broken target", encoding="utf-8")
    permit = host.acquire()
    assert permit.role == "base" and permit.probation
    with closing(permit.open_store()) as store:
        assert store.db.total_changes == 0


@pytest.mark.parametrize(
    "component", ["slot", "process", "configuration", "selector", "unit", "controller"]
)
def test_actual_selected_identity_and_bytes_are_rechecked(host: Host, component: str) -> None:
    plan = host.plan()
    if component in {"slot", "controller"}:
        slot = host.target if component == "slot" else host.base
        Path(slot.package.root, "__init__.py").write_text("changed", encoding="utf-8")
    elif component == "process":
        host.monkeypatch.setattr(
            managed_deployment,
            "sys",
            SimpleNamespace(
                platform="linux",
                executable=host.base.lexical_python,
                prefix=host.base.prefix,
                base_prefix="/base",
            ),
        )
    elif component == "selector":
        with SelectorStore(host.journal.directory.path) as selector:
            selector.replace(
                selector.read(),
                SelectorValue(deployment=host.anchor.digest, installation=host.base.digest),
                guard=lambda: None,
            )
    else:
        Path(
            host.anchor.config_path if component == "configuration" else host.anchor.unit_path
        ).write_bytes(b"changed")
    before = _snapshot(host.journal.database)
    with pytest.raises(ManagedBootError):
        host.acquire(plan)
    assert _snapshot(host.journal.database) == before


@pytest.mark.parametrize(
    "sql,parameters",
    [
        ("DELETE FROM runtime_state WHERE name='format_state_v1'", ()),
        ("UPDATE runtime_state SET value=? WHERE name='format_state_v1'", ("not JSON",)),
        ("UPDATE runtime_state SET name='format_state_v2' WHERE name='format_state_v1'", ()),
        ("UPDATE runtime_state SET value='foreign' WHERE name='store_instance_v1'", ()),
        ("UPDATE objects SET data=json_set(data,'$.principal','other') WHERE kind='session'", ()),
        ("UPDATE objects SET data=json_set(data,'$.kind','child') WHERE kind='session'", ()),
        ("UPDATE objects SET data=json_set(data,'$.id','different') WHERE kind='session'", ()),
    ],
)
def test_missing_invalid_or_changed_persisted_evidence_never_becomes_legacy(
    host: Host, sql: str, parameters: tuple[object, ...]
) -> None:
    host.mutate(sql, parameters)
    before = _snapshot(host.journal.database)
    with pytest.raises(ManagedBootError):
        host.acquire()
    assert _snapshot(host.journal.database) == before


def test_metadata_failures_and_unsupported_filesystems_release_the_adopted_lease(
    host: Host,
) -> None:
    plan = host.plan()

    def unsupported(paths: object) -> None:
        raise FilesystemError("filesystem_unsupported")

    host.monkeypatch.setattr(managed_boot, "verify_local_filesystems", unsupported)
    with pytest.raises(ManagedBootError):
        host.acquire(plan)
    with host.journal.writer_lease():
        pass


def test_writer_from_another_database_is_rejected(host: Host, tmp_path: Path) -> None:
    with TransactionJournal(tmp_path / "other-journal", tmp_path / "other.sqlite3") as other:
        lease = other.writer_lease().__enter__()
        with pytest.raises(ManagedBootError, match="managed_writer_lease_required"):
            ManagedBootPermit.acquire(host.journal, boot=_BOOT, plan=host.plan(), lease=lease)
        assert lease.fd is None


def test_database_replaced_before_open_store_never_reaches_the_factory(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit = host.acquire()
    host.journal.database.rename(host.journal.database.with_suffix(".old"))
    host.journal.database.write_bytes(b"foreign")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Store must not open a replacement DB")

    monkeypatch.setattr(Store, "open_existing", forbidden)
    with pytest.raises(ManagedBootError, match="managed_database_changed"):
        permit.open_store()


def test_same_identity_database_replaced_during_store_open_is_closed(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit = host.acquire()
    original = Store.open_existing
    opened: list[Store] = []

    def replaced(path: Path, *, durable: bool, runtime_owner: bool = False) -> Store:
        data = path.read_bytes()
        path.rename(path.with_suffix(".old"))
        path.write_bytes(data)
        store = original(path, durable=durable, runtime_owner=runtime_owner)
        opened.append(store)
        return store

    monkeypatch.setattr(Store, "open_existing", replaced)
    with pytest.raises(ManagedBootError, match="managed_database_changed"):
        permit.open_store()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].db.execute("SELECT 1")


def test_marker_removed_after_acquire_cannot_reach_runtime_recovery(host: Host) -> None:
    permit = host.acquire()
    host.mutate("DELETE FROM runtime_state WHERE name='format_state_v1'")
    before = _snapshot(host.journal.database)
    with pytest.raises(ManagedBootError):
        permit.open_store()
    assert _snapshot(host.journal.database) == before


@pytest.mark.parametrize("stage", ["acquire", "open_store"])
def test_visible_released_record_requires_a_successful_durability_barrier(
    host: Host, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    record = host.record("committed", "none", activation="released")
    plan = host.plan()
    permit = host.acquire(plan) if stage == "open_store" else None
    original_fsync = os.fsync

    def unavailable(descriptor: int) -> None:
        if descriptor == host.journal.directory.fd:
            raise OSError(errno.EIO, "synthetic journal directory fsync failure")
        original_fsync(descriptor)

    with host.journal.locked() as control:
        local_os = SimpleNamespace(**{**vars(os), "fsync": unavailable})
        monkeypatch.setattr(management_transaction, "os", local_os)
        monkeypatch.setattr(managed_boot, "os", local_os)
        updated = TransactionRecordV2.model_validate(
            {**record.model_dump(), "revision": record.revision + 1}
        )
        with pytest.raises(OSError) as caught:
            control._write(updated)
        assert caught.value.errno == errno.EIO
    # The actual rename is visible even though its directory fsync failed.
    assert host.journal.read_current() == updated
    entered: list[str] = []
    original_connect, original_open = sqlite3.connect, Store.open_existing

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        entered.append("sqlite")
        return original_connect(*args, **kwargs)

    def open_store(path: Path, *, durable: bool, runtime_owner: bool = False) -> Store:
        entered.append("store")
        return original_open(path, durable=durable, runtime_owner=runtime_owner)

    monkeypatch.setattr(
        managed_boot, "sqlite3", SimpleNamespace(**{**vars(sqlite3), "connect": connect})
    )
    monkeypatch.setattr(Store, "open_existing", open_store)
    with pytest.raises(ManagedBootError, match="managed_boot_verification_failed"):
        if permit is None:
            host.acquire(plan)
        else:
            permit.open_store()
    assert entered == []


def test_configuration_changed_during_the_sqlite_snapshot_is_rechecked(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = managed_boot.read_format_evidence

    def changed(db: sqlite3.Connection) -> managed_boot.FormatEvidence:
        evidence = original(db)
        Path(host.anchor.config_path).write_bytes(b'{"server":{"port":9999}}')
        return evidence

    monkeypatch.setattr(managed_boot, "read_format_evidence", changed)
    with pytest.raises(ManagedBootError, match="managed_files_changed"):
        host.acquire()


def test_changed_identity_in_a_new_revision_is_not_a_legal_phase_advance(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = managed_boot.read_format_evidence

    def changed(db: sqlite3.Connection) -> managed_boot.FormatEvidence:
        evidence = original(db)
        record = host.journal.read_current()
        assert isinstance(record, TransactionRecordV2)
        updated = TransactionRecordV2.model_validate(
            {
                **record.model_dump(),
                "revision": record.revision + 1,
                "identity": {**record.identity.model_dump(), "main_session": "different"},
            }
        )
        with host.journal.locked() as control:
            control._write(updated)
        return evidence

    monkeypatch.setattr(managed_boot, "read_format_evidence", changed)
    with pytest.raises(ManagedBootError, match="managed_record_changed"):
        host.acquire()


def test_failed_store_close_retains_writer_and_database_fd(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit = host.acquire()

    def failed_read(db: sqlite3.Connection) -> managed_boot.FormatEvidence:
        raise FormatEvidenceError("format_marker_invalid")

    def failed_close(store: Store) -> None:
        raise OSError("synthetic close failure")

    monkeypatch.setattr(managed_boot, "read_format_evidence", failed_read)
    monkeypatch.setattr(Store, "close", failed_close)
    with pytest.raises(ManagedBootError, match="managed_boot_cleanup_failed") as caught:
        permit.open_store()
    assert caught.value._owner is permit
    assert permit._database is not None and permit._database.fd is not None
    with pytest.raises(ManagedBootError, match="managed_boot_cleanup_failed"):
        permit.close_after_writers()
    with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
        with StoreConstructionLock(host.journal.database):
            pass
    with pytest.raises(TransactionError, match="lock_busy"):
        with host.journal.writer_lease():
            pass


def test_failed_read_only_connection_close_also_retains_the_lease(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenClose(sqlite3.Connection):
        def close(self) -> None:
            raise OSError("synthetic close failure")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return sqlite3.connect(*args, **kwargs, factory=BrokenClose)

    monkeypatch.setattr(
        managed_boot,
        "sqlite3",
        SimpleNamespace(
            connect=connect,
            Error=sqlite3.Error,
            Connection=sqlite3.Connection,
            LEGACY_TRANSACTION_CONTROL=sqlite3.LEGACY_TRANSACTION_CONTROL,
        ),
    )
    with pytest.raises(ManagedBootError, match="managed_boot_cleanup_failed") as caught:
        host.acquire()
    permit = caught.value._owner
    assert permit is not None
    host.permits.append(permit)
    assert permit._database is not None and permit._database.fd is not None
    with pytest.raises(TransactionError, match="lock_busy"):
        with host.journal.writer_lease():
            pass
