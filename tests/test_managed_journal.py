from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_managed_transaction import CONFIG, UNIT, binding, deadline, identity, manifest

from muli.format_admission import CURRENT_FORMAT_STATE
from muli.managed_journal import RoleSnapshots, advance_managed, prepare_managed, read_snapshots
from muli.managed_transaction import RoleBinding, TransactionRecordV2, TransitionV2
from muli.management_transaction import (
    JournalLimits,
    ProjectionReceipt,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    Transition,
    _snapshot,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux flock/fsync primitive")


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[TransactionJournal]:
    with TransactionJournal(tmp_path / "journal", tmp_path / "data" / "muli.db") as value:
        yield value


def snapshots(slot: str) -> RoleSnapshots:
    return RoleSnapshots(
        configuration=CONFIG,
        selector=json.dumps(
            {"deployment": "d" * 64, "installation": slot * 64},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        unit=UNIT,
    )


def prepare(
    journal: TransactionJournal,
    *,
    request: str = "request",
    projected: ProjectionReceipt | None = None,
) -> TransactionRecordV2:
    with journal.writer_lease() as writer, journal.locked() as control:
        return prepare_managed(
            control,
            request_id=request,
            action="switch",
            identity=identity(),
            base=binding("a"),
            target=binding("b"),
            base_data=snapshots("a"),
            target_data=snapshots("b"),
            observed_format=CURRENT_FORMAT_STATE,
            base_manifest=manifest(),
            target_manifest=manifest(),
            deadline=deadline(),
            writer=writer,
            projected=projected,
        )


def test_one_state_file_and_legacy_reader_cannot_misinterpret_v2(
    journal: TransactionJournal,
) -> None:
    record = prepare(journal)
    assert journal.read_current() == record
    with journal.locked() as control:
        assert control.read_current() == record
    with pytest.raises(TransactionError, match="invalid_record"):
        journal.read()
    assert read_snapshots(journal, record, "base") == snapshots("a")
    assert read_snapshots(journal, record, "target") == snapshots("b")
    assert (journal.directory.path / f"{record.generation}-base-unit.bin").read_bytes() == UNIT
    assert {path.name for path in journal.directory.path.iterdir()} == {
        "lock",
        "state.json",
        *snapshots("a").entries(record.generation, "base"),
        *snapshots("b").entries(record.generation, "target"),
    }


def test_v1_unresolved_is_preserved_and_exact_projection_precedes_v2(
    journal: TransactionJournal,
) -> None:
    with journal.writer_lease() as writer, journal.locked() as control:
        old = control.prepare(
            request_id="legacy",
            action="restart",
            identity=ServiceIdentity(
                installation="a" * 64,
                service="b" * 64,
                store_instance="store",
                main_session="main",
                origin_boot="origin",
            ),
            base=CONFIG,
            target=CONFIG,
            deadline=deadline(),
            writer=writer,
        )
    assert journal.read_current() == old
    before = (journal.directory.path / "state.json").read_bytes()
    with pytest.raises(TransactionError, match="transaction_in_progress"):
        prepare(journal)
    assert (journal.directory.path / "state.json").read_bytes() == before
    with journal.locked() as control:
        old = control.advance(old.position, Transition(phase="aborted", code="cancelled"))
    with pytest.raises(TransactionError, match="projection_required"):
        prepare(journal)
    record = prepare(
        journal, projected=ProjectionReceipt(**old.position.model_dump(), store_instance="store")
    )
    assert record.generation != old.generation
    assert journal.snapshot(old, "base") == CONFIG
    assert journal.read_current() == record


def test_same_request_reuses_identity_without_repairing_corrupted_snapshot(
    journal: TransactionJournal,
) -> None:
    record = prepare(journal)
    assert prepare(journal) == record
    path = journal.directory.path / f"{record.generation}-target-unit.bin"
    path.write_bytes(UNIT + b"# changed\n")
    with pytest.raises(TransactionError, match="snapshot_invalid"):
        prepare(journal)
    assert path.read_bytes() == UNIT + b"# changed\n"
    assert journal.read_current() == record


def test_noncanonical_selector_is_rejected_before_any_snapshot_is_written(
    journal: TransactionJournal,
) -> None:
    selector = json.dumps({"deployment": "d" * 64, "installation": "a" * 64}, indent=2).encode()
    base = RoleBinding.model_validate(
        {
            **binding("a").model_dump(),
            "selector": _snapshot(selector, 4096).model_dump(),
        }
    )
    with journal.writer_lease() as writer, journal.locked() as control:
        with pytest.raises(TransactionError, match="selector_not_canonical"):
            prepare_managed(
                control,
                request_id="request",
                action="switch",
                identity=identity(),
                base=base,
                target=binding("b"),
                base_data=RoleSnapshots(CONFIG, selector, UNIT),
                target_data=snapshots("b"),
                observed_format=CURRENT_FORMAT_STATE,
                base_manifest=manifest(),
                target_manifest=manifest(),
                deadline=deadline(),
                writer=writer,
            )
    assert journal.read_current() is None
    assert {path.name for path in journal.directory.path.iterdir()} == {"lock"}


def test_v2_advance_compares_generation_revision_on_the_same_file(
    journal: TransactionJournal,
) -> None:
    record = prepare(journal)
    with journal.locked() as control:
        changed = advance_managed(control, record.position, TransitionV2(phase="draining"))
        with pytest.raises(TransactionError, match="stale_revision"):
            advance_managed(control, record.position, TransitionV2(phase="draining"))
    assert changed.revision == record.revision + 1
    assert journal.read_current() == changed


@pytest.mark.parametrize("protocol", [None, True, 1.0, 2.0, 3, "1", "2"])
def test_dispatcher_requires_an_explicit_integer_protocol(
    journal: TransactionJournal,
    protocol: object,
) -> None:
    record = prepare(journal)
    values = record.model_dump()
    if protocol is None:
        values.pop("protocol")
    else:
        values["protocol"] = protocol
    path = journal.directory.path / "state.json"
    path.write_text(json.dumps(values))
    with pytest.raises(TransactionError, match="invalid_record"):
        journal.read_current()


@pytest.mark.parametrize(
    "suffix",
    [
        "base-configuration.json",
        "base-selector.json",
        "base-unit.bin",
        "target-configuration.json",
        "target-selector.json",
        "target-unit.bin",
        "state.json",
    ],
)
@pytest.mark.parametrize("when", ["before", "after"])
def test_crash_at_each_write_keeps_old_or_complete_new_generation(
    journal: TransactionJournal,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    when: str,
) -> None:
    old = prepare(journal)
    with journal.locked() as control:
        old = advance_managed(
            control, old.position, TransitionV2(phase="aborted", code="cancelled")
        )
    receipt = ProjectionReceipt(**old.position.model_dump(), store_instance="store")

    class Crash(BaseException):
        pass

    replace = journal.directory.replace

    def interrupted(name: str, data: bytes) -> None:
        if name.endswith(suffix) and when == "before":
            raise Crash
        replace(name, data)
        if name.endswith(suffix) and when == "after":
            raise Crash

    monkeypatch.setattr(journal.directory, "replace", interrupted)
    with pytest.raises(Crash):
        prepare(journal, request="successor", projected=receipt)
    monkeypatch.undo()
    visible = journal.read_current()
    assert isinstance(visible, TransactionRecordV2)
    assert visible == old or (visible.request_id == "successor" and visible.phase == "prepared")
    assert read_snapshots(journal, visible, "base") == snapshots("a")
    assert read_snapshots(journal, visible, "target") == snapshots("b")
    assert read_snapshots(journal, old, "base") == snapshots("a")


def test_failed_durability_barrier_poisons_controller(
    journal: TransactionJournal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare(journal)
    real_fsync = os.fsync

    def failed(fd: int) -> None:
        if fd == journal.directory.fd:
            raise OSError("test barrier failure")
        real_fsync(fd)

    with journal.locked() as control:
        monkeypatch.setattr(os, "fsync", failed)
        with pytest.raises(OSError, match="barrier failure"):
            control.read_current()
        monkeypatch.undo()
        with pytest.raises(TransactionError, match="journal_unconfirmed"):
            control.read_current()


def test_budget_is_checked_before_snapshot_writes(tmp_path: Path) -> None:
    with TransactionJournal(
        tmp_path / "journal", tmp_path / "data" / "muli.db", JournalLimits(max_entries=8)
    ) as journal:
        with pytest.raises(TransactionError, match="directory_budget"):
            prepare(journal)
        assert journal.read_current() is None
        assert {path.name for path in journal.directory.path.iterdir()} == {"lock"}


def test_foreign_writer_lease_rejected_before_writes(
    journal: TransactionJournal,
    tmp_path: Path,
) -> None:
    with TransactionJournal(tmp_path / "other", tmp_path / "other-data" / "muli.db") as other:
        with other.writer_lease() as writer, journal.locked() as control:
            with pytest.raises(TransactionError, match="writer_lease_required"):
                prepare_managed(
                    control,
                    request_id="request",
                    action="switch",
                    identity=identity(),
                    base=binding("a"),
                    target=binding("b"),
                    base_data=snapshots("a"),
                    target_data=snapshots("b"),
                    observed_format=CURRENT_FORMAT_STATE,
                    base_manifest=manifest(),
                    target_manifest=manifest(),
                    deadline=deadline(),
                    writer=writer,
                )
    assert journal.read_current() is None
