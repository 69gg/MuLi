"""SQL→Journal confirmation through real install, SQLite and retained materials.

Only the service observation is controlled. No candidate interpreter, service,
or format-2 Store is started; confirmation must never imply boot admission.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from test_project_controller_transfer_runtime import _acquire_reopened_permit
from test_project_format_transition_migrate import (
    MigrationProbe,
    cgroup_fixture,
    live_files,
    no_service_command,
    unchanged_rows,
)
from test_project_format_transition_prepare import (
    TransitionHost,
    boot_host,
    filesystems,
    prepared_transition,
    transition_host,
)

from muli import managed_format_migration
from muli.format_admission import FormatAdmissionError, require_current_format
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.managed_boot import ManagedBootError
from muli.managed_controller import ManagedTransactionController
from muli.managed_journal import _read_snapshots, advance_managed, read_snapshots
from muli.managed_transaction import (
    ProjectControllerTransferRecord,
    ProjectFormatTransitionRecord,
    TransactionRecordV2,
    TransitionV2,
)
from muli.management_transaction import (
    ClockReading,
    JournalControl,
    TransactionError,
    TransactionJournal,
)
from muli.platforms.linux_transaction import ObservationError, UnitSnapshot
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore, SlotIdentity
from muli.platforms.managed_probe import ManagedProbeResult, ManagedSystemdProbe

__all__ = ["boot_host", "filesystems", "transition_host"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed confirmation")


class ConfirmationProbe(MigrationProbe):
    """The live stopped unit is still source1 even when logical base is fallback2."""

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
        assert journal is self.subject.host.journal and anchor == self.subject.source.anchor
        assert controller.digest == anchor.controller_slot == record.source.binding.installation
        assert slots["target"].digest == record.candidate.installation
        assert slots["base"].digest == record.source.binding.installation
        snapshot = await self.read(self.subject.source.service)
        snapshot = replace(
            snapshot,
            unit=replace(snapshot.unit, active_state="inactive", sub_state="dead", pid=0),
            process=None,
        )
        return ManagedProbeResult(
            snapshot,
            (),
            frozenset({record.source.binding.unit.digest}),
            record.source.binding.unit.digest,
        )


def confirmation_backend(
    subject: TransitionHost,
    record: ProjectFormatTransitionRecord,
    control: JournalControl,
    cgroup_root: Path,
) -> ManagedLinuxBackend:
    with DeploymentStore(subject.host.journal.directory.path) as retained:
        controller = retained.read_slot(subject.source.anchor.controller_slot)
        # The retained physical source/controller remains frozen after the
        # logical base changes. This fixture does not install fallback files.
        slots: dict[Literal["base", "target"], SlotIdentity] = {
            "base": retained.read_slot(record.source.binding.installation),
            "target": retained.read_slot(record.target.installation),
        }
    return ManagedLinuxBackend(
        subject.source.anchor,
        controller,
        slots,
        subject.host.journal,
        control=control,
        probe=ConfirmationProbe(subject),
        runner=no_service_command,
        cgroup_root=cgroup_root,
        clock=lambda: ClockReading(boot_id="kernel", wall=1201.0, monotonic=201.0),
    )


def install_prefix(
    control: JournalControl,
    prepared: ProjectFormatTransitionRecord,
) -> ProjectFormatTransitionRecord:
    current = prepared
    for transition in (
        TransitionV2(phase="draining"),
        TransitionV2(phase="activating", intent="stop_base"),
        TransitionV2(phase="activating", intent="install_target"),
    ):
        advanced = advance_managed(control, current.position, transition)
        assert isinstance(advanced, ProjectFormatTransitionRecord)
        current = advanced
    assert current.revision == 4
    return current


def confirmed_record(
    control: JournalControl,
    before: ProjectFormatTransitionRecord,
) -> ProjectFormatTransitionRecord:
    current = control.read_current()
    assert isinstance(current, ProjectFormatTransitionRecord)
    expected = before.model_dump()
    expected.update(
        revision=5,
        observed_format=before.target_format.model_dump(),
        base=before.fallback.model_dump(),
    )
    assert current.model_dump() == expected, (
        "install returned without exact SQL→Journal confirmation"
    )
    assert current.boot_role is None and not current.resolved
    assert current.health is None and current.activation == "pending"
    assert read_snapshots(control.journal, current, "base") == _read_snapshots(
        control.journal, current.generation, current.fallback, "fallback"
    )
    return current


def frozen_snapshots(
    subject: TransitionHost, record: ProjectFormatTransitionRecord
) -> dict[str, bytes]:
    paths = sorted(subject.host.journal.directory.path.glob(record.generation + "-*"))
    assert len(paths) == 9
    return {path.name: path.read_bytes() for path in paths}


def count_sql_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    writes = {"begin": 0, "receipt": 0, "marker": 0, "commit": 0}

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "BEGIN IMMEDIATE":
                writes["begin"] += 1
            elif sql.startswith("INSERT INTO main.objects"):
                writes["receipt"] += 1
            elif sql.startswith("UPDATE main.runtime_state"):
                writes["marker"] += 1
            elif sql == "COMMIT":
                writes["commit"] += 1
            return super().execute(sql, parameters)

    def open_observed(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(
            path.as_uri() + "?mode=rw&cache=private",
            uri=True,
            timeout=0,
            isolation_level="DEFERRED",
            factory=ObservedConnection,
        )

    monkeypatch.setattr(managed_format_migration, "_open_database", open_observed)
    return writes


async def test_install_confirms_exact_revision_five_without_boot_or_file_installation(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = transition_host
    prepared = await prepared_transition(subject)
    permit = subject.runtime.maintenance.permit
    assert permit is not None
    source_plan = permit._plan
    await subject.runtime.close()
    files, snapshots = live_files(subject), frozen_snapshots(subject, prepared)
    inode = subject.host.journal.database.stat()
    cgroups = tmp_path / "confirmation-cgroups"
    cgroup_fixture(cgroups, populated=False)
    writes = count_sql_writes(monkeypatch)
    with closing(sqlite3.connect(subject.host.journal.database)) as observer:
        business = unchanged_rows(observer)
        with subject.host.journal.locked() as control:
            before = install_prefix(control, prepared)
            data = _read_snapshots(
                control.journal, before.generation, before.candidate, "candidate"
            )
            prefix_bytes = control.journal.directory.read(
                "state.json",
                control.journal.limits.max_record_bytes,
            )
            with pytest.raises(TransactionError, match="^invalid_transition$"):
                advance_managed(
                    control,
                    before.position,
                    TransitionV2(
                        phase="activating",
                        intent="install_target",
                    ),
                )
            assert (
                control.journal.directory.read(
                    "state.json",
                    control.journal.limits.max_record_bytes,
                )
                == prefix_bytes
            )
            async with confirmation_backend(subject, before, control, cgroups) as backend:
                await backend.install(before, "target", data)
                assert read_format_evidence(observer).state == before.target_format
                current = confirmed_record(control, before)
                assert backend.controller_slot.digest == before.source.binding.installation
                assert backend.slots["base"].digest == before.source.binding.installation
                with pytest.raises(TransactionError, match="^invalid_transition$"):
                    advance_managed(
                        control,
                        current.position,
                        TransitionV2(
                            phase="activating",
                            intent="start_target",
                        ),
                    )
                with pytest.raises(TransactionError, match="^managed_transaction_required$"):
                    await ManagedTransactionController(control, backend, backend.clock).step()
                for operation in (backend.start(current, "target"), backend.observe(current)):
                    with pytest.raises(
                        ObservationError, match="^project_format_transition_unsupported$"
                    ):
                        await operation
            with pytest.raises(ManagedBootError, match="^managed_boot_not_permitted$"):
                _acquire_reopened_permit(subject.host, "no-format-source-boot", plan=source_plan)
        assert unchanged_rows(observer) == business
        with pytest.raises(FormatAdmissionError, match="^store_format_unsupported$"):
            require_current_format(read_format_evidence(observer))
    assert writes == {"begin": 1, "receipt": 1, "marker": 1, "commit": 1}
    after = subject.host.journal.database.stat()
    assert (after.st_dev, after.st_ino) == (inode.st_dev, inode.st_ino)
    assert live_files(subject) == files and frozen_snapshots(subject, prepared) == snapshots


async def sql_only_prefix(
    subject: TransitionHost,
    prepared: ProjectFormatTransitionRecord,
    cgroups: Path,
) -> ProjectFormatTransitionRecord:
    """Exercise the explicit helper callback=None recovery prefix, with real locks."""
    journal = subject.host.journal
    files = live_files(subject)
    with journal.locked() as control:
        before = install_prefix(control, prepared)
        async with confirmation_backend(subject, before, control, cgroups) as backend:
            stopped = await backend._probe(before)
            assert backend._stopped(stopped.service)
            with journal.writer_lease() as writer:

                def guard() -> None:
                    backend._guard(before, "target", writer, stopped.service.unit.cgroup)
                    assert live_files(subject) == files

                for _ in range(2):
                    # The second real invocation must inspect the committed pair
                    # and return without replaying SQL or publishing Journal.
                    managed_format_migration.migrate_project_format(
                        journal.database,
                        before,
                        guard=guard,
                        seconds=min(
                            backend.operation_timeout,
                            before.deadline.remaining(backend.clock())
                            - before.deadline.rollback_reserve,
                        ),
                        metadata_bytes=journal.limits.max_record_bytes,
                        confirm=None,
                    )
                    assert control.read_current() == before
    return before


@pytest.mark.parametrize(
    "damage",
    [
        "missing-receipt",
        "other-generation",
        "wrong-configuration",
        "source-marker",
        "inode",
    ],
)
async def test_confirmation_rejects_inexact_sql_pair_without_repair(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    subject = transition_host
    prepared = await prepared_transition(subject)
    await subject.runtime.close()
    journal, database = subject.host.journal, subject.host.journal.database
    cgroups = tmp_path / "confirmation-cgroups"
    cgroup_fixture(cgroups, populated=False)
    files, snapshots = live_files(subject), frozen_snapshots(subject, prepared)
    with closing(sqlite3.connect(database)) as observer:
        source_marker = observer.execute(
            "SELECT value FROM runtime_state WHERE name=?",
            (FORMAT_STATE_KEY,),
        ).fetchone()[0]
    writes = count_sql_writes(monkeypatch)
    before = await sql_only_prefix(subject, prepared, cgroups)
    assert writes == {"begin": 1, "receipt": 1, "marker": 1, "commit": 1}
    if damage == "source-marker":
        with journal.locked() as control:
            data = _read_snapshots(journal, before.generation, before.candidate, "candidate")
            async with confirmation_backend(subject, before, control, cgroups) as backend:
                await backend.install(before, "target", data)
            before = confirmed_record(control, before)
    if damage == "inode":
        moved = database.with_name("before-confirmation.sqlite")
        database.rename(moved)
        with closing(sqlite3.connect(moved)) as old, closing(sqlite3.connect(database)) as new:
            old.backup(new)
        assert database.stat().st_ino != before.migration.database.inode
    with closing(sqlite3.connect(database)) as observer:
        if damage == "missing-receipt":
            observer.execute("DELETE FROM objects WHERE kind='managed_format_transition'")
        elif damage == "other-generation":
            observer.execute(
                "UPDATE objects SET id='other-generation' WHERE kind='managed_format_transition'",
            )
        elif damage == "wrong-configuration":
            observer.execute(
                "UPDATE objects SET data=json_set(data,'$.source_configuration_digest',?) "
                "WHERE kind='managed_format_transition'",
                ("0" * 64,),
            )
        elif damage == "source-marker":
            observer.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (source_marker, FORMAT_STATE_KEY),
            )
            observer.execute("DELETE FROM objects WHERE kind='managed_format_transition'")
        observer.commit()
        rows = list(observer.iterdump())
        with journal.locked() as control:
            state = journal.directory.read("state.json", journal.limits.max_record_bytes)
            assert control.read_current() == before
            data = _read_snapshots(journal, before.generation, before.candidate, "candidate")
            async with confirmation_backend(subject, before, control, cgroups) as backend:
                with pytest.raises(
                    ObservationError,
                    match=(
                        "^transaction_format_database_changed$"
                        if damage == "inode"
                        else "^transaction_format_source_changed$"
                        if damage == "source-marker"
                        else "^transaction_format_receipt_invalid$"
                    ),
                ):
                    await backend.install(before, "target", data)
            assert journal.directory.read("state.json", journal.limits.max_record_bytes) == state
            assert control.read_current() == before
        assert list(observer.iterdump()) == rows
    assert writes == {"begin": 1, "receipt": 1, "marker": 1, "commit": 1}
    assert live_files(subject) == files and frozen_snapshots(subject, prepared) == snapshots


@pytest.mark.parametrize("change", ["source", "candidate", "fallback", "identity", "expired"])
async def test_confirmation_keeps_same_position_identity_and_original_deadline(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    subject = transition_host
    prepared = await prepared_transition(subject)
    await subject.runtime.close()
    journal = subject.host.journal
    cgroups = tmp_path / "confirmation-cgroups"
    cgroup_fixture(cgroups, populated=False)
    writes = count_sql_writes(monkeypatch)
    with closing(sqlite3.connect(journal.database)) as observer:
        rows = list(observer.iterdump())
        with journal.locked() as control:
            before = install_prefix(control, prepared)
            async with confirmation_backend(subject, before, control, cgroups) as backend:
                backend._check(before)
                values = before.model_dump()
                if change == "source":
                    values["source"]["projected"]["revision"] += 1
                elif change in {"candidate", "fallback"}:
                    values[change]["compatibility"] = "0" * 64
                    if change == "candidate":
                        # The logical target is derived from candidate. Keep
                        # this parsed record valid before testing identity CAS.
                        values["target"] = dict(values["candidate"])
                elif change == "identity":
                    values["identity"]["origin_boot"] = "changed-origin-boot"
                else:
                    backend.clock = lambda: ClockReading(
                        boot_id="kernel",
                        wall=9999.0,
                        monotonic=9999.0,
                    )
                current = ProjectFormatTransitionRecord.model_validate(values)
                assert current.position == before.position
                if current != before:
                    # A legitimate parsed, same-position record must not erase
                    # the backend's frozen identity before any SQL/probe effect.
                    control._write(current)
                state = journal.directory.read("state.json", journal.limits.max_record_bytes)
                data = _read_snapshots(journal, before.generation, before.candidate, "candidate")
                with pytest.raises(
                    TimeoutError if change == "expired" else ObservationError,
                    match=None if change == "expired" else "^transaction_format_identity_changed$",
                ):
                    await backend.install(current, "target", data)
                assert (
                    journal.directory.read("state.json", journal.limits.max_record_bytes) == state
                )
                assert control.read_current() == current
        assert list(observer.iterdump()) == rows
    assert writes == {"begin": 0, "receipt": 0, "marker": 0, "commit": 0}


@pytest.mark.parametrize("damage", ["none", "logical-base-slot", "source-snapshot"])
async def test_confirmed_record_real_probe_keeps_physical_source_and_double_read(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    subject = transition_host
    prepared = await prepared_transition(subject)
    await subject.runtime.close()
    journal = subject.host.journal
    cgroups = tmp_path / "confirmation-cgroups"
    cgroup_fixture(cgroups, populated=False)
    with journal.locked() as control:
        before = install_prefix(control, prepared)
        data = _read_snapshots(journal, before.generation, before.candidate, "candidate")
        async with confirmation_backend(subject, before, control, cgroups) as backend:
            await backend.install(before, "target", data)
            current = confirmed_record(control, before)
            controller, slots = backend.controller_slot, dict(backend.slots)
        observed = await ConfirmationProbe(subject).read(subject.source.service)
        unit = replace(observed.unit, active_state="inactive", sub_state="dead", pid=0)
        snapshots = 0
        probe = ManagedSystemdProbe(runner=no_service_command)

        async def unit_path(name: str) -> str:
            assert name == subject.source.service.unit
            return "/synthetic/systemd/unit"

        async def unit_snapshot(path: str) -> UnitSnapshot:
            nonlocal snapshots
            assert path == "/synthetic/systemd/unit"
            snapshots += 1
            return unit

        monkeypatch.setattr(probe, "_unit_path", unit_path)
        monkeypatch.setattr(probe, "_snapshot", unit_snapshot)
        if damage == "logical-base-slot":
            with DeploymentStore(journal.directory.path) as retained:
                slots["base"] = retained.read_slot(current.fallback.installation)
        elif damage == "source-snapshot":
            path = journal.directory.path / f"{current.generation}-source-unit.bin"
            path.write_bytes(path.read_bytes() + b"\n")
        if damage == "none":
            result = await probe.read_managed(
                journal,
                current,
                subject.source.anchor,
                controller,
                slots,
            )
            assert result.service.unit == unit and result.service.process is None
            assert result.disk_unit == current.source.binding.unit.digest
            assert result.unit_confirmed and snapshots == 2
        else:
            with pytest.raises(
                ObservationError if damage == "logical-base-slot" else TransactionError,
                match=(
                    "^managed_probe_identity_changed$"
                    if damage == "logical-base-slot"
                    else "^snapshot_invalid$"
                ),
            ):
                await probe.read_managed(
                    journal,
                    current,
                    subject.source.anchor,
                    controller,
                    slots,
                )
            assert snapshots == 0


@pytest.mark.parametrize("lost_reply", ["before-replace", "after-replace"])
async def test_confirmation_lost_reply_reopens_control_and_only_reads_exact_pair(
    transition_host: TransitionHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost_reply: str,
) -> None:
    subject = transition_host
    prepared = await prepared_transition(subject)
    await subject.runtime.close()
    journal = subject.host.journal
    inode = journal.database.stat()
    files, snapshots = live_files(subject), frozen_snapshots(subject, prepared)
    cgroups = tmp_path / "confirmation-cgroups"
    cgroup_fixture(cgroups, populated=False)
    writes = count_sql_writes(monkeypatch)
    replace_state = journal.directory.replace
    attempts = publications = 0
    armed = True

    def lose_confirmation_reply(name: str, raw: bytes) -> None:
        nonlocal attempts, publications, armed
        confirmation = name == "state.json" and json.loads(raw).get("revision") == 5
        if confirmation:
            attempts += 1
            # This failure must follow the real DB commit, never a fake success flag.
            with closing(sqlite3.connect(journal.database)) as observer:
                assert read_format_evidence(observer).state == prepared.target_format
                rows = observer.execute(
                    "SELECT id FROM objects WHERE kind='managed_format_transition' LIMIT 2"
                ).fetchall()
                assert rows == [(prepared.generation,)]
            if armed and lost_reply == "before-replace":
                armed = False
                raise RuntimeError("controlled confirmation reply loss")
        replace_state(name, raw)
        if confirmation:
            publications += 1
            if armed:
                armed = False
                raise RuntimeError("controlled confirmation reply loss")

    monkeypatch.setattr(journal.directory, "replace", lose_confirmation_reply)
    with closing(sqlite3.connect(journal.database)) as observer:
        business = unchanged_rows(observer)
        with journal.locked() as old_control:
            before = install_prefix(old_control, prepared)
            data = _read_snapshots(journal, before.generation, before.candidate, "candidate")
            async with confirmation_backend(subject, before, old_control, cgroups) as backend:
                with pytest.raises(RuntimeError, match="^controlled confirmation reply loss$"):
                    await backend.install(before, "target", data)
            with pytest.raises(TransactionError, match="^journal_unconfirmed$"):
                old_control.read_current()
            assert writes == {"begin": 1, "receipt": 1, "marker": 1, "commit": 1}
        # Never reuse the poisoned lock owner or the backend's captured rev4 guards.
        with journal.locked() as reopened_control:
            visible = reopened_control.read_current()
            assert isinstance(visible, ProjectFormatTransitionRecord)
            assert visible.revision == (4 if lost_reply == "before-replace" else 5)
            async with confirmation_backend(
                subject, visible, reopened_control, cgroups
            ) as reopened:
                await reopened.install(visible, "target", data)
            current = confirmed_record(reopened_control, before)
            state = journal.directory.read("state.json", journal.limits.max_record_bytes)
            counts = attempts, publications
            async with confirmation_backend(subject, current, reopened_control, cgroups) as replay:
                await replay.install(current, "target", data)
            assert journal.directory.read("state.json", journal.limits.max_record_bytes) == state
            assert (attempts, publications) == counts
            with pytest.raises(TransactionError, match="^stale_revision$"):
                advance_managed(reopened_control, before.position, TransitionV2(phase="draining"))
        assert unchanged_rows(observer) == business
        assert read_format_evidence(observer).state == prepared.target_format
    assert writes == {"begin": 1, "receipt": 1, "marker": 1, "commit": 1}
    assert (attempts, publications) == ((2, 1) if lost_reply == "before-replace" else (1, 1))
    after = journal.database.stat()
    assert (after.st_dev, after.st_ino) == (inode.st_dev, inode.st_ino)
    assert live_files(subject) == files and frozen_snapshots(subject, prepared) == snapshots
