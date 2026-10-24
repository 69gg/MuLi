"""Fixed migration effect prerequisites; real temporary SQLite/flock, synthetic service only."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, NoReturn

import pytest
from test_project_controller_transfer_runtime import TerminalProbe
from test_project_format_transition_prepare import (
    TransitionHost,
    boot_host,
    filesystems,
    prepared_transition,
    transition_host,
)

from muli import managed_format_migration
from muli.format_admission import CURRENT_FORMAT_STATE, FormatAdmissionError, require_current_format
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.managed_boot import ManagedBootError
from muli.managed_controller import ManagedTransactionController
from muli.managed_journal import _read_snapshots, advance_managed
from muli.managed_transaction import (
    ProjectControllerTransferRecord,
    ProjectFormatTransitionRecord,
    TransactionRecordV2,
    TransitionV2,
    format_digest,
)
from muli.management_transaction import (
    ClockReading,
    JournalControl,
    TransactionError,
    TransactionJournal,
)
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore, SlotIdentity
from muli.platforms.managed_probe import ManagedProbeResult, managed_process_arguments
from muli.store_initialization import StoreConstructionLock, StoreInitializationError

__all__ = ["boot_host", "filesystems", "transition_host"]
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux migration lease and file identity"
)

_RECEIPT_KIND = "managed_format_transition"


class MigrationProbe(TerminalProbe):
    """Only manager/process observations are controlled; no process is stopped."""

    def __init__(self, subject: TransitionHost, *, running: bool = False) -> None:
        super().__init__()
        self.subject, self.running = subject, running
        self.boot = subject.probe.boot

    async def read_managed(
        self,
        journal: TransactionJournal,
        record: TransactionRecordV2
        | ProjectControllerTransferRecord
        | ProjectFormatTransitionRecord,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Literal["base", "target"], SlotIdentity],
    ) -> ManagedProbeResult:
        assert isinstance(record, ProjectFormatTransitionRecord)
        assert anchor == self.subject.source.anchor and journal is self.subject.host.journal
        assert controller.digest == slots["base"].digest == record.source.binding.installation
        snapshot = await self.read(self.subject.source.service)
        assert snapshot.process is not None
        if self.running:
            snapshot = replace(
                snapshot,
                process=replace(
                    snapshot.process,
                    executable=slots["base"].resolved_python.path,
                    argv=managed_process_arguments(anchor, slots["base"]),
                ),
            )
        else:
            snapshot = replace(
                snapshot,
                unit=replace(
                    snapshot.unit,
                    active_state="inactive",
                    sub_state="dead",
                    pid=0,
                ),
                process=None,
            )
        return ManagedProbeResult(
            snapshot,
            ("base",) if self.running else (),
            frozenset({record.source.binding.unit.digest}),
            record.source.binding.unit.digest,
        )


async def no_service_command(
    argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
) -> NoReturn:
    raise AssertionError("固定 SQL 片不得调用 systemctl、启动服务或安装文件")


def unchanged_rows(db: sqlite3.Connection) -> tuple[list[Any], list[Any], list[Any]]:
    """Small fixture-only oracle; production must not enumerate business data."""
    return (
        db.execute(
            "SELECT kind,id,data,updated FROM objects WHERE kind<>? ORDER BY kind,id",
            (_RECEIPT_KIND,),
        ).fetchall(),
        db.execute(
            "SELECT name,value FROM runtime_state WHERE name<>? ORDER BY name", (FORMAT_STATE_KEY,)
        ).fetchall(),
        db.execute("SELECT * FROM ingress ORDER BY seq").fetchall(),
    )


def live_files(subject: TransitionHost) -> tuple[bytes, bytes, bytes]:
    anchor = subject.source.anchor
    return (
        Path(anchor.config_path).read_bytes(),
        (subject.host.journal.directory.path / "selector.json").read_bytes(),
        Path(anchor.unit_path).read_bytes(),
    )


async def install_via_existing_entry(
    subject: TransitionHost,
    record: ProjectFormatTransitionRecord,
    control: JournalControl,
    backend: ManagedLinuxBackend,
) -> ProjectFormatTransitionRecord:
    # The original implementation rejected this first formal API dispatch.
    # No future import/API, unchecked model_copy or fabricated phase is used.
    current = record
    for transition in (
        TransitionV2(phase="draining"),
        TransitionV2(phase="activating", intent="stop_base"),
        TransitionV2(phase="activating", intent="install_target"),
    ):
        advanced = advance_managed(control, current.position, transition)
        assert isinstance(advanced, ProjectFormatTransitionRecord)
        current = advanced
    data = _read_snapshots(subject.host.journal, current.generation, current.candidate, "candidate")
    await backend.install(current, "target", data)
    return current


def backend_for(
    subject: TransitionHost,
    record: ProjectFormatTransitionRecord,
    control: JournalControl,
    probe: MigrationProbe,
    cgroup_root: Path,
) -> ManagedLinuxBackend:
    with DeploymentStore(subject.host.journal.directory.path) as retained:
        slots = {
            "base": retained.read_slot(record.source.binding.installation),
            "target": retained.read_slot(record.candidate.installation),
        }
    return ManagedLinuxBackend(
        subject.source.anchor,
        slots["base"],
        slots,
        subject.host.journal,
        control=control,
        probe=probe,
        runner=no_service_command,
        cgroup_root=cgroup_root,
        clock=lambda: ClockReading(boot_id="kernel", wall=1201.0, monotonic=201.0),
    )


def cgroup_fixture(root: Path, *, populated: bool) -> None:
    group = root / "synthetic/muli.service"
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text(
        f"populated {int(populated)}\nfrozen 0\n", encoding="ascii"
    )


async def test_fixed_migration_same_inode_receipt_and_observed_one_reentry(
    transition_host: TransitionHost,
    tmp_path: Path,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    files = live_files(subject)
    await subject.runtime.close()
    database = subject.host.journal.database
    identity = database.stat()
    assert (identity.st_dev, identity.st_ino) == (
        record.migration.database.device,
        record.migration.database.inode,
    )
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0)) as db:
        original = unchanged_rows(db)
        assert read_format_evidence(db).state == CURRENT_FORMAT_STATE
        assert db.execute("SELECT 1 FROM objects WHERE kind=?", (_RECEIPT_KIND,)).fetchone() is None
        # Runtime close releases the actual writer; this is not a fake permit.
        with subject.host.journal.writer_lease(), StoreConstructionLock(database):
            pass
        with subject.host.journal.locked() as control:
            async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                installed = await install_via_existing_entry(subject, record, control, backend)
                assert read_format_evidence(db).state == record.target_format
                rows = db.execute(
                    "SELECT id,data FROM objects WHERE kind=?", (_RECEIPT_KIND,)
                ).fetchall()
                assert len(rows) == 1 and rows[0][0] == record.generation
                assert json.loads(rows[0][1]) == {
                    "id": record.generation,
                    "version": 1,
                    "migration_id": "project_review_1_to_2",
                    "generation": record.generation,
                    "database": record.migration.database.model_dump(),
                    "source_format_digest": format_digest(record.source_format),
                    "target_format_digest": format_digest(record.target_format),
                    "store_instance": record.identity.store_instance,
                    "main_session": record.identity.main_session,
                    "source_projection": record.source.projected.model_dump(),
                    "source_configuration_digest": record.source.binding.configuration.digest,
                }
                with pytest.raises(FormatAdmissionError, match="^store_format_unsupported$"):
                    require_current_format(read_format_evidence(db))
                assert (
                    installed.observed_format == CURRENT_FORMAT_STATE
                    and installed.boot_role is None
                )
                assert control.read_current() == installed
                # This counter is only an extra no-write oracle on this same
                # observer connection; commit/inode proof is marker+receipt above.
                version = db.execute("PRAGMA data_version").fetchone()[0]
                data = _read_snapshots(
                    subject.host.journal, installed.generation, installed.candidate, "candidate"
                )
                # A fresh backend must resolve this crash prefix from durable
                # same-inode marker+receipt, without an in-memory success flag.
                async with backend_for(subject, installed, control, probe, cgroup_root) as reopened:
                    await reopened.install(installed, "target", data)
                assert db.execute("PRAGMA data_version").fetchone()[0] == version
                assert (
                    db.execute(
                        "SELECT id,data FROM objects WHERE kind=?", (_RECEIPT_KIND,)
                    ).fetchall()
                    == rows
                )
                assert control.read_current() == installed
        assert unchanged_rows(db) == original
        assert read_format_evidence(db).state == record.target_format
    after = database.stat()
    assert (after.st_dev, after.st_ino) == (identity.st_dev, identity.st_ino)
    assert live_files(subject) == files


@pytest.mark.parametrize("when", ["before", "after"])
async def test_fixed_migration_commit_unknown_resolves_exact_durable_pair_without_retry(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    await subject.runtime.close()
    database = subject.host.journal.database
    inode = database.stat()
    files = live_files(subject)
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    commits = inserts = opens = 0

    class InterruptedCommit(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            nonlocal commits, inserts
            if sql.startswith("INSERT INTO main.objects"):
                inserts += 1
            if sql == "COMMIT":
                commits += 1
                if when == "after":
                    super().execute(sql, parameters)
                raise sqlite3.OperationalError("受控丢失COMMIT回复")
            return super().execute(sql, parameters)

    def open_interrupted(path: Path) -> sqlite3.Connection:
        nonlocal opens
        opens += 1
        return sqlite3.connect(
            path.as_uri() + "?mode=rw&cache=private",
            uri=True,
            timeout=0,
            isolation_level="DEFERRED",
            factory=InterruptedCommit,
        )

    monkeypatch.setattr(managed_format_migration, "_open_database", open_interrupted)
    with closing(sqlite3.connect(database)) as observer:
        original = list(observer.iterdump())
        with subject.host.journal.locked() as control:
            async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                if when == "before":
                    with pytest.raises(
                        ObservationError, match="^transaction_format_commit_unknown$"
                    ):
                        await install_via_existing_entry(subject, record, control, backend)
                    assert list(observer.iterdump()) == original
                else:
                    installed = await install_via_existing_entry(subject, record, control, backend)
                    assert (
                        installed.observed_format == CURRENT_FORMAT_STATE
                        and installed.boot_role is None
                    )
                    assert read_format_evidence(observer).state == record.target_format
                    rows = observer.execute(
                        "SELECT id,data FROM objects WHERE kind=?", (_RECEIPT_KIND,)
                    ).fetchall()
                    assert len(rows) == 1 and rows[0][0] == record.generation
                    assert (
                        json.loads(rows[0][1])["source_projection"]
                        == record.source.projected.model_dump()
                    )
                    data = _read_snapshots(
                        subject.host.journal, installed.generation, installed.candidate, "candidate"
                    )
                    async with backend_for(
                        subject, installed, control, probe, cgroup_root
                    ) as reopened:
                        await reopened.install(installed, "target", data)
        # Neither unknown path automatically reexecutes INSERT or COMMIT.
        assert commits == inserts == 1
        assert opens >= 2
    after = database.stat()
    assert (after.st_dev, after.st_ino) == (inode.st_dev, inode.st_ino)
    assert live_files(subject) == files


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "bad-json",
        "other-generation",
        "wrong-body",
        "oversize",
        "boolean-version",
        "missing-field",
    ],
)
async def test_fixed_migration_committed_pair_rejects_receipt_damage_without_repair(
    transition_host: TransitionHost,
    tmp_path: Path,
    change: str,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    await subject.runtime.close()
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    files = live_files(subject)
    with closing(sqlite3.connect(subject.host.journal.database)) as db:
        with subject.host.journal.locked() as control:
            async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                installed = await install_via_existing_entry(subject, record, control, backend)
            row = db.execute(
                "SELECT data FROM objects WHERE kind=? AND id=?", (_RECEIPT_KIND, record.generation)
            ).fetchone()
            assert row is not None
            if change == "missing":
                db.execute(
                    "DELETE FROM objects WHERE kind=? AND id=?", (_RECEIPT_KIND, record.generation)
                )
            elif change == "other-generation":
                db.execute(
                    "UPDATE objects SET id=? WHERE kind=? AND id=?",
                    ("another-generation", _RECEIPT_KIND, record.generation),
                )
            else:
                value = json.loads(row[0])
                if change == "wrong-body":
                    value["main_session"] = "other-main"
                elif change == "boolean-version":
                    value["version"] = True
                elif change == "missing-field":
                    del value["version"]
                raw = (
                    "{"
                    if change == "bad-json"
                    else "x" * (subject.host.journal.limits.max_record_bytes + 1)
                    if change == "oversize"
                    else json.dumps(value)
                )
                db.execute(
                    "UPDATE objects SET data=? WHERE kind=? AND id=?",
                    (raw, _RECEIPT_KIND, record.generation),
                )
            db.commit()
            before = list(db.iterdump())
            data = _read_snapshots(
                subject.host.journal, installed.generation, installed.candidate, "candidate"
            )
            async with backend_for(subject, installed, control, probe, cgroup_root) as reopened:
                with pytest.raises(ObservationError, match="^transaction_format_receipt_invalid$"):
                    await reopened.install(installed, "target", data)
            assert list(db.iterdump()) == before
            assert control.read_current() == installed
    assert live_files(subject) == files


@pytest.mark.parametrize("damage", ["oversized-text", "invalid-utf8"])
async def test_fixed_migration_bounds_receipt_primary_key_before_python_projection(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    """The valid small receipt body cannot authorize an unbounded/undecodable PK."""
    subject = transition_host
    record = await prepared_transition(subject)
    await subject.runtime.close()
    database = subject.host.journal.database
    inode = database.stat()
    cgroup_root = tmp_path / "receipt-key-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    files = live_files(subject)
    crossed_lengths: list[int] = []
    maximum = subject.host.journal.limits.max_record_bytes
    original_open = managed_format_migration._open_database

    def open_observed(path: Path) -> sqlite3.Connection:
        db = original_open(path)

        def observe(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> tuple[Any, ...]:
            # Observe the real SQL-to-Python conversion without replacing any
            # production query, result or marker/receipt reader.
            crossed_lengths.extend(
                len(value) for value in row if isinstance(value, str) and len(value) > maximum
            )
            return row

        db.row_factory = observe
        return db

    with closing(sqlite3.connect(database)) as observer:
        with subject.host.journal.locked() as control:
            async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                installed = await install_via_existing_entry(subject, record, control, backend)
            original_receipt = observer.execute(
                "SELECT data FROM objects WHERE kind=? AND id=?", (_RECEIPT_KIND, record.generation)
            ).fetchone()[0]
            assert len(original_receipt.encode("utf-8")) <= maximum
            assert json.loads(original_receipt)["id"] == record.generation
            if damage == "oversized-text":
                large_id = "x" * (1024 * 1024)
                assert len(large_id) > maximum
                observer.execute("UPDATE objects SET id=? WHERE kind=?", (large_id, _RECEIPT_KIND))
            else:
                observer.execute(
                    "UPDATE objects SET id=CAST(X'80' AS TEXT) WHERE kind=?", (_RECEIPT_KIND,)
                )
            observer.commit()
            before_receipts = observer.execute(
                "SELECT CAST(id AS BLOB),CAST(data AS BLOB),updated FROM objects WHERE kind=?",
                (_RECEIPT_KIND,),
            ).fetchall()
            before_rows = unchanged_rows(observer)
            before_marker = observer.execute(
                "SELECT CAST(value AS BLOB) FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
            ).fetchone()[0]
            before_state = (subject.host.journal.directory.path / "state.json").read_bytes()
            monkeypatch.setattr(managed_format_migration, "_open_database", open_observed)
            data = _read_snapshots(
                subject.host.journal, installed.generation, installed.candidate, "candidate"
            )
            async with backend_for(subject, installed, control, probe, cgroup_root) as reopened:
                with pytest.raises(ObservationError, match="^transaction_format_receipt_invalid$"):
                    await reopened.install(installed, "target", data)
            assert crossed_lengths == [], (
                "oversized receipt PK crossed the existing SQL byte budget"
            )
            assert (
                observer.execute(
                    "SELECT CAST(id AS BLOB),CAST(data AS BLOB),updated FROM objects WHERE kind=?",
                    (_RECEIPT_KIND,),
                ).fetchall()
                == before_receipts
            )
            assert unchanged_rows(observer) == before_rows
            assert (
                observer.execute(
                    "SELECT CAST(value AS BLOB) FROM runtime_state WHERE name=?",
                    (FORMAT_STATE_KEY,),
                ).fetchone()[0]
                == before_marker
            )
            assert (subject.host.journal.directory.path / "state.json").read_bytes() == before_state
            assert control.read_current() == installed
    after = database.stat()
    assert (after.st_dev, after.st_ino) == (inode.st_dev, inode.st_ino)
    assert live_files(subject) == files


@pytest.mark.parametrize("table", ["objects", "runtime_state"])
async def test_fixed_migration_rejects_writing_table_triggers_before_database_effect(
    transition_host: TransitionHost,
    tmp_path: Path,
    table: str,
) -> None:
    """A schema extension cannot turn the two fixed writes into business deletion."""
    subject = transition_host
    record = await prepared_transition(subject)
    main_id = subject.runtime.store.existing_main_session_id()
    assert main_id is not None
    receipt, inserted = subject.runtime.conversation.enqueue(
        main_id,
        "迁移后仍须处理的真实主人输入",
        source="web",
        principal="owner",
        mode="yolo",
        max_pending=subject.runtime.config.settings.limits.max_pending_runs,
        event_id="migration-trigger-pending",
    )
    assert inserted
    await subject.runtime.close()
    database = subject.host.journal.database
    inode = database.stat()
    files = live_files(subject)
    cgroup_root = tmp_path / "schema-trigger-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    with closing(sqlite3.connect(database)) as db:
        pending = db.execute(
            "SELECT id,state,payload FROM ingress WHERE id=?", (receipt["id"],)
        ).fetchone()
        assert pending is not None and pending[1] == "pending"
        assert json.loads(pending[2])["content"] == "迁移后仍须处理的真实主人输入"
        if table == "objects":
            db.execute(
                "CREATE TRIGGER migration_business_guard AFTER INSERT ON objects "
                "WHEN NEW.kind='managed_format_transition' "
                "BEGIN DELETE FROM ingress; END"
            )
        else:
            db.execute(
                "CREATE TRIGGER migration_business_guard AFTER UPDATE OF value ON runtime_state "
                "WHEN NEW.name='format_state_v1' "
                "BEGIN DELETE FROM ingress; END"
            )
        db.commit()
        original = list(db.iterdump())
        before_rows = unchanged_rows(db)
        before_marker = db.execute(
            "SELECT CAST(value AS BLOB) FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
        ).fetchone()[0]
        assert db.execute("SELECT 1 FROM objects WHERE kind=?", (_RECEIPT_KIND,)).fetchone() is None
        with subject.host.journal.locked() as control:
            async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                with pytest.raises(
                    ObservationError, match="^transaction_format_schema_unsupported$"
                ):
                    await install_via_existing_entry(subject, record, control, backend)
            current = control.read_current()
            assert isinstance(current, ProjectFormatTransitionRecord)
            assert current.observed_format == CURRENT_FORMAT_STATE and current.boot_role is None
        assert list(db.iterdump()) == original
        assert unchanged_rows(db) == before_rows
        assert (
            db.execute(
                "SELECT CAST(value AS BLOB) FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
            ).fetchone()[0]
            == before_marker
        )
        assert db.execute("SELECT 1 FROM objects WHERE kind=?", (_RECEIPT_KIND,)).fetchone() is None
        assert (
            db.execute(
                "SELECT id,state,payload FROM ingress WHERE id=?", (receipt["id"],)
            ).fetchone()
            == pending
        )
    after = database.stat()
    assert (after.st_dev, after.st_ino) == (inode.st_dev, inode.st_ino)
    assert live_files(subject) == files


@pytest.mark.parametrize("role", ["source", "candidate", "fallback"])
async def test_fixed_migration_rejects_any_frozen_role_snapshot_drift(
    transition_host: TransitionHost,
    tmp_path: Path,
    role: str,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    await subject.runtime.close()
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    snapshot = (
        subject.host.journal.directory.path / f"{record.generation}-{role}-configuration.json"
    )
    snapshot.write_bytes(snapshot.read_bytes() + b"\n")
    files = live_files(subject)
    with closing(sqlite3.connect(subject.host.journal.database)) as db:
        before = list(db.iterdump())
        with subject.host.journal.locked() as control:
            async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                with pytest.raises(TransactionError, match="^snapshot_invalid$"):
                    await install_via_existing_entry(subject, record, control, backend)
        assert list(db.iterdump()) == before
    assert live_files(subject) == files


@pytest.mark.parametrize("when", ["before", "after-commit"])
async def test_fixed_migration_rejects_database_inode_replacement_even_with_same_rows(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    await subject.runtime.close()
    database = subject.host.journal.database
    original_inode = database.stat().st_ino
    files = live_files(subject)
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    swaps = 0

    def replace_same_rows(source: sqlite3.Connection) -> None:
        nonlocal swaps
        replacement = database.with_name("inode-replacement.sqlite3")
        with closing(sqlite3.connect(replacement)) as target:
            # Only a temporary fault fixture: production never copies/restores DB.
            source.backup(target, sleep=0)
        replacement.replace(database)
        swaps += 1
        assert database.stat().st_ino != original_inode

    class ReplaceAfterCommit(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if sql == "COMMIT":
                replace_same_rows(self)
            return cursor

    def open_replacing(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(
            path.as_uri() + "?mode=rw&cache=private",
            uri=True,
            timeout=0,
            isolation_level="DEFERRED",
            factory=ReplaceAfterCommit,
        )

    if when == "before":
        with closing(sqlite3.connect(database)) as source:
            before = list(source.iterdump())
            replace_same_rows(source)
    else:
        monkeypatch.setattr(managed_format_migration, "_open_database", open_replacing)
    with subject.host.journal.locked() as control:
        async with backend_for(subject, record, control, probe, cgroup_root) as backend:
            error = ObservationError if when == "before" else ManagedBootError
            code = (
                "transaction_format_database_changed"
                if when == "before"
                else "managed_database_changed"
            )
            with pytest.raises(error, match=f"^{code}$"):
                await install_via_existing_entry(subject, record, control, backend)
        current = control.read_current()
        assert isinstance(current, ProjectFormatTransitionRecord)
        assert current.observed_format == CURRENT_FORMAT_STATE and current.boot_role is None
    assert swaps == 1 and live_files(subject) == files
    if when == "before":
        with closing(sqlite3.connect(database)) as observer:
            assert list(observer.iterdump()) == before


async def test_fixed_migration_prefix_cas_keeps_driver_health_and_boot_closed(
    transition_host: TransitionHost,
    tmp_path: Path,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    permit = subject.runtime.maintenance.permit
    assert permit is not None
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    with subject.host.journal.locked() as control:
        advanced = advance_managed(control, record.position, TransitionV2(phase="draining"))
        assert isinstance(advanced, ProjectFormatTransitionRecord)
        current_bytes = (control.journal.directory.path / "state.json").read_bytes()
        with pytest.raises(TransactionError, match="^stale_revision$"):
            advance_managed(control, record.position, TransitionV2(phase="draining"))
        for invalid in (
            TransitionV2(phase="prepared"),
            TransitionV2(phase="aborted"),
            TransitionV2(phase="activating", intent="start_target"),
            TransitionV2(phase="activating", intent="stop_base", code="backend_failed"),
        ):
            with pytest.raises(TransactionError, match="^invalid_transition$"):
                advance_managed(control, advanced.position, invalid)
        assert (control.journal.directory.path / "state.json").read_bytes() == current_bytes
        assert advanced.boot_role is None and not advanced.resolved
        with pytest.raises(ManagedBootError, match="^managed_boot_not_permitted$"):
            permit._record()
        async with backend_for(subject, advanced, control, probe, cgroup_root) as backend:

            def clock() -> ClockReading:
                return ClockReading(boot_id="kernel", wall=1201.0, monotonic=201.0)

            controller = ManagedTransactionController(control, backend, clock)
            with pytest.raises(TransactionError, match="^managed_transaction_required$"):
                await controller.step()
            for operation in (
                backend.observe(advanced),
                backend.drain(advanced),
                backend.resume(advanced),
                backend.activate(advanced, "target"),
                backend.start(advanced, "target"),
                backend.stop(advanced, "base"),
            ):
                with pytest.raises(
                    ObservationError, match="^project_format_transition_unsupported$"
                ):
                    await operation
        assert (control.journal.directory.path / "state.json").read_bytes() == current_bytes


@pytest.mark.parametrize("barrier", ["running", "populated"])
async def test_fixed_migration_rejects_source_not_stopped_before_database_effect(
    transition_host: TransitionHost,
    tmp_path: Path,
    barrier: str,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    await subject.runtime.close()
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=barrier == "populated")
    probe = MigrationProbe(subject, running=barrier == "running")
    files = live_files(subject)
    with closing(sqlite3.connect(subject.host.journal.database)) as db:
        original = list(db.iterdump())
        with subject.host.journal.locked() as control:
            async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                before = await probe.read_managed(
                    subject.host.journal,
                    record,
                    subject.source.anchor,
                    backend.controller_slot,
                    backend.slots,
                )
                assert (before.service.process is not None) == (barrier == "running")
                assert backend.cgroups.empty(before.service.unit.cgroup) == (barrier != "populated")
                with pytest.raises(
                    ObservationError,
                    match="^transaction_service_not_stopped$"
                    if barrier == "running"
                    else "^cgroup_not_empty$",
                ):
                    await install_via_existing_entry(subject, record, control, backend)
        assert list(db.iterdump()) == original
    assert live_files(subject) == files


@pytest.mark.parametrize("barrier", ["writer", "construction", "marker"])
async def test_fixed_migration_rejects_busy_real_lock_or_changed_marker(
    transition_host: TransitionHost,
    tmp_path: Path,
    barrier: str,
) -> None:
    subject = transition_host
    record = await prepared_transition(subject)
    await subject.runtime.close()
    database = subject.host.journal.database
    cgroup_root = tmp_path / "migration-cgroups"
    cgroup_fixture(cgroup_root, populated=False)
    probe = MigrationProbe(subject)
    files = live_files(subject)
    with closing(sqlite3.connect(database)) as db:
        if barrier == "marker":
            # Same complete format and identity, different original marker bytes.
            db.execute(
                "UPDATE runtime_state SET value=value||? WHERE name=?", ("\n", FORMAT_STATE_KEY)
            )
            db.commit()
            assert read_format_evidence(db).state == record.source_format
        original = list(db.iterdump())
        lock = (
            subject.host.journal.writer_lease()
            if barrier == "writer"
            else StoreConstructionLock(database)
            if barrier == "construction"
            else nullcontext()
        )
        with lock:
            if barrier == "writer":
                with pytest.raises(TransactionError, match="^lock_busy$"):
                    with subject.host.journal.writer_lease():
                        raise AssertionError("独立 fd 应互斥")
            elif barrier == "construction":
                with pytest.raises(StoreInitializationError, match="^store_initialization_busy$"):
                    with StoreConstructionLock(database):
                        raise AssertionError("独立构造锁 fd 应互斥")
            with subject.host.journal.locked() as control:
                async with backend_for(subject, record, control, probe, cgroup_root) as backend:
                    error = (
                        TransactionError
                        if barrier == "writer"
                        else StoreInitializationError
                        if barrier == "construction"
                        else ObservationError
                    )
                    code = (
                        "lock_busy"
                        if barrier == "writer"
                        else "store_initialization_busy"
                        if barrier == "construction"
                        else "transaction_format_source_changed"
                    )
                    with pytest.raises(error, match=f"^{code}$"):
                        await install_via_existing_entry(subject, record, control, backend)
        assert list(db.iterdump()) == original
    assert live_files(subject) == files
