from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from muli.management_transaction import (
    ClockReading,
    Deadline,
    HealthProof,
    JournalControl,
    ProjectionReceipt,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    Transition,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux flock/fsync primitive")
BASE = b'{ "api_key": "private-base", "port": 10000 }\n'
TARGET = b'{"api_key":"private-target","port":10001}\n'


def identity() -> ServiceIdentity:
    return ServiceIdentity(
        installation="a" * 64,
        service="b" * 64,
        store_instance="store-one",
        main_session="main",
        origin_boot="old-boot",
    )


def deadline() -> Deadline:
    return Deadline(
        created=ClockReading(boot_id="kernel-one", wall=1000.0, monotonic=100.0),
        seconds=60.0,
        rollback_reserve=10.0,
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[TransactionJournal]:
    with TransactionJournal(tmp_path / "journal", tmp_path / "data" / "muli.db") as value:
        yield value


def prepare(journal: TransactionJournal, request: str = "request-one") -> TransactionRecord:
    with journal.writer_lease() as writer, journal.locked() as control:
        return control.prepare(
            request_id=request,
            action="configure",
            identity=identity(),
            base=BASE,
            target=TARGET,
            deadline=deadline(),
            writer=writer,
        )


def verifying(control: JournalControl, record: TransactionRecord) -> TransactionRecord:
    for transition in (
        Transition(phase="draining"),
        Transition(phase="activating", intent="stop_base"),
        Transition(phase="activating", intent="install_target"),
        Transition(phase="activating", intent="start_target"),
        Transition(phase="verifying"),
    ):
        record = control.advance(record.position, transition)
    return record


def proof(record: TransactionRecord, *, boot: str = "target-boot") -> HealthProof:
    return HealthProof(
        generation=record.generation,
        challenge=record.challenge,
        boot=boot,
        installation=record.identity.installation,
        service=record.identity.service,
        store_instance=record.identity.store_instance,
        main_session=record.identity.main_session,
        configuration=record.target.digest,
        core_ready=True,
        admission="paused",
    )


def test_private_roundtrip_preserves_exact_bytes_and_public_view(
    journal: TransactionJournal,
) -> None:
    record = prepare(journal)
    assert journal.read() == record
    assert journal.snapshot(record, "base") == BASE
    assert journal.snapshot(record, "target") == TARGET
    for path in journal.directory.path.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.directory.path.stat().st_mode) == 0o700
    public = str(record.public())
    assert all(
        secret not in public for secret in ("private-base", "private-target", record.challenge)
    )
    with pytest.raises(ValidationError):
        record.phase = "committed"  # type: ignore[misc]


def test_preallocated_generation_binds_durable_management_plan(journal: TransactionJournal) -> None:
    generation = "1" * 32
    with journal.writer_lease() as writer, journal.locked() as control:
        values = {
            "request_id": "preallocated",
            "action": "configure",
            "identity": identity(),
            "base": BASE,
            "target": TARGET,
            "deadline": deadline(),
            "writer": writer,
        }
        record = control.prepare(**values, generation=generation)  # type: ignore[arg-type]
        assert record.generation == generation
        assert journal.snapshot(record, "base") == BASE
        assert control.prepare(**values, generation=generation) == record  # type: ignore[arg-type]
        assert control.prepare(**values) == record  # type: ignore[arg-type]
        with pytest.raises(TransactionError, match="request_conflict"):
            control.prepare(**values, generation="2" * 32)  # type: ignore[arg-type]
        assert control.read() == record


def test_successor_cannot_reuse_predecessor_generation(journal: TransactionJournal) -> None:
    record = prepare(journal)
    with journal.writer_lease() as writer, journal.locked() as control:
        record = control.advance(record.position, Transition(phase="aborted", code="cancelled"))
        with pytest.raises(TransactionError, match="generation_conflict"):
            control.prepare(
                request_id="successor",
                action="configure",
                identity=identity(),
                base=BASE,
                target=TARGET,
                deadline=deadline(),
                writer=writer,
                generation=record.generation,
                projected=ProjectionReceipt(
                    **record.position.model_dump(), store_instance=record.identity.store_instance
                ),
            )
        assert control.read() == record
        assert journal.snapshot(record, "target") == TARGET


@pytest.mark.parametrize("generation", ["", "A" * 32, "a" * 31, "a" * 33, "../" + "a" * 29])
def test_invalid_preallocated_generation_never_writes_snapshots(
    journal: TransactionJournal, generation: str
) -> None:
    with journal.writer_lease() as writer, journal.locked() as control:
        with pytest.raises(TransactionError, match="invalid_generation"):
            control.prepare(
                request_id="preallocated",
                action="configure",
                identity=identity(),
                base=BASE,
                target=TARGET,
                deadline=deadline(),
                writer=writer,
                generation=generation,
            )
        assert control.read() is None
        assert set(os.listdir(journal.directory.fd)) == {"lock"}


def test_distinct_fixed_locks_and_database_alias_share_lease(
    journal: TransactionJournal,
    tmp_path: Path,
) -> None:
    with journal.writer_lease() as writer, journal.locked() as control:
        assert writer.fd is not None and control.fd is not None
        assert os.fstat(writer.fd).st_ino != os.fstat(control.fd).st_ino
        assert not os.get_inheritable(writer.fd) and not os.get_inheritable(control.fd)
        with pytest.raises(TransactionError, match="lock_busy"), journal.locked():
            pytest.fail("second controller acquired the same inode")
        alias = tmp_path / "alias.db"
        alias.symlink_to(tmp_path / "data" / "muli.db")
        with TransactionJournal(tmp_path / "other-journal", alias) as other:
            with other.locked():
                with pytest.raises(TransactionError, match="lock_busy"), other.writer_lease():
                    pytest.fail("same database acquired a second writer lease")
    lock_inode = (journal.directory.path / "lock").stat().st_ino
    with journal.locked():
        assert (journal.directory.path / "lock").stat().st_ino == lock_inode


def test_lock_replacement_and_unowned_writer_are_rejected(journal: TransactionJournal) -> None:
    with journal.writer_lease() as writer, journal.locked() as control:
        path = journal.directory.path / "lock"
        path.unlink()
        path.write_bytes(b"")
        path.chmod(0o600)
        with pytest.raises(TransactionError, match="lock_not_owned"):
            control.prepare(
                request_id="request",
                action="configure",
                identity=identity(),
                base=BASE,
                target=TARGET,
                deadline=deadline(),
                writer=writer,
            )
    with journal.locked() as control:
        with pytest.raises(TransactionError, match="lock_not_owned"):
            control.prepare(
                request_id="request",
                action="configure",
                identity=identity(),
                base=BASE,
                target=TARGET,
                deadline=deadline(),
                writer=journal.writer_lease(),
            )


@pytest.mark.parametrize("phase", ["prepared", "recovery_required", "committed"])
def test_prepare_never_overwrites_unresolved_generation(
    journal: TransactionJournal,
    phase: str,
) -> None:
    record = prepare(journal)
    with journal.writer_lease() as writer, journal.locked() as control:
        if phase == "recovery_required":
            record = control.advance(
                record.position, Transition(phase="recovery_required", code="foreign_state")
            )
        elif phase == "committed":
            record = verifying(control, record)
            record = control.advance(
                record.position, Transition(phase="committed", proof=proof(record))
            )
        same = control.prepare(
            request_id=record.request_id,
            action="configure",
            identity=identity(),
            base=BASE,
            target=TARGET,
            deadline=deadline(),
            writer=writer,
        )
        assert same == record
        with pytest.raises(TransactionError, match="transaction_in_progress"):
            control.prepare(
                request_id="another-request",
                action="configure",
                identity=identity(),
                base=BASE,
                target=TARGET,
                deadline=deadline(),
                writer=writer,
            )
        with pytest.raises(TransactionError, match="request_conflict"):
            control.prepare(
                request_id=record.request_id,
                action="configure",
                identity=identity(),
                base=BASE,
                target=b"{}",
                deadline=deadline(),
                writer=writer,
            )
    assert journal.read() == record


def test_resolved_generation_requires_exact_store_projection_before_replacement(
    journal: TransactionJournal,
) -> None:
    record = prepare(journal)
    with journal.writer_lease() as writer, journal.locked() as control:
        record = control.advance(record.position, Transition(phase="aborted", code="cancelled"))

        def next_generation(projected: ProjectionReceipt | None = None) -> TransactionRecord:
            return control.prepare(
                request_id="next",
                action="configure",
                identity=identity(),
                base=BASE,
                target=TARGET,
                deadline=deadline(),
                writer=writer,
                projected=projected,
            )

        with pytest.raises(TransactionError, match="projection_required"):
            next_generation()
        receipt = ProjectionReceipt(
            **record.position.model_dump(), store_instance=record.identity.store_instance
        )
        next_record = next_generation(receipt)
        assert next_record.generation != record.generation
        assert journal.snapshot(record, "base") == BASE


def test_cas_rejects_stale_and_transition_cannot_mutate_frozen_authority(
    journal: TransactionJournal,
) -> None:
    record = prepare(journal)
    with journal.locked() as control:
        current = control.advance(record.position, Transition(phase="draining"))
        with pytest.raises(TransactionError, match="stale_revision"):
            control.advance(
                record.position, Transition(phase="recovery_required", code="foreign_state")
            )
        with pytest.raises(TransactionError, match="invalid_transition"):
            control.advance(current.position, Transition(phase="committed", proof=proof(current)))
        assert current.deadline == record.deadline and current.identity == record.identity
        with pytest.raises(ValidationError):
            Transition.model_validate({"phase": "draining", "deadline": deadline().model_dump()})


@pytest.mark.parametrize(
    "field,value",
    [
        ("challenge", "stale-challenge"),
        ("boot", "old-boot"),
        ("installation", "c" * 64),
        ("store_instance", "other-store"),
        ("configuration", "d" * 64),
        ("core_ready", False),
        ("admission", "released"),
    ],
)
def test_commit_requires_bound_passive_health_proof(
    journal: TransactionJournal,
    field: str,
    value: str | bool,
) -> None:
    record = prepare(journal)
    with journal.locked() as control:
        record = verifying(control, record)
        invalid = HealthProof.model_validate({**proof(record).model_dump(), field: value})
        with pytest.raises(TransactionError, match="invalid_health_proof"):
            control.advance(record.position, Transition(phase="committed", proof=invalid))
        assert journal.read() == record


def test_committed_never_rolls_back_and_only_matching_release_resolves(
    journal: TransactionJournal,
) -> None:
    record = prepare(journal)
    with journal.locked() as control:
        record = verifying(control, record)
        record = control.advance(
            record.position, Transition(phase="committed", proof=proof(record))
        )
        assert not record.resolved
        with pytest.raises(TransactionError, match="invalid_transition"):
            control.advance(record.position, Transition(phase="rolling_back", intent="stop_target"))
        release = HealthProof.model_validate(
            {**proof(record).model_dump(), "admission": "released"}
        )
        record = control.advance(
            record.position, Transition(phase="committed", activation="released", proof=release)
        )
        assert record.resolved


@pytest.mark.parametrize(
    "payload", [b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e400}', b"not-json"]
)
def test_snapshot_parser_rejects_ambiguous_or_invalid_json(
    journal: TransactionJournal,
    payload: bytes,
) -> None:
    with journal.writer_lease() as writer, journal.locked() as control:
        with pytest.raises(TransactionError, match="invalid_json"):
            control.prepare(
                request_id="request",
                action="configure",
                identity=identity(),
                base=BASE,
                target=payload,
                deadline=deadline(),
                writer=writer,
            )
    assert journal.read() is None


def test_restart_cannot_smuggle_configuration_change(journal: TransactionJournal) -> None:
    with journal.writer_lease() as writer, journal.locked() as control:
        with pytest.raises(TransactionError, match="restart_changes_configuration"):
            control.prepare(
                request_id="request",
                action="restart",
                identity=identity(),
                base=BASE,
                target=TARGET,
                deadline=deadline(),
                writer=writer,
            )


def test_deadline_does_not_renew_after_controller_or_kernel_restart() -> None:
    original = deadline()
    assert original.remaining(ClockReading(boot_id="kernel-one", wall=1.0, monotonic=125.0)) == 35.0
    assert (
        original.remaining(ClockReading(boot_id="kernel-two", wall=1025.0, monotonic=1.0)) == 35.0
    )
    assert original.remaining(ClockReading(boot_id="kernel-two", wall=2000.0, monotonic=1.0)) == 0.0
    with pytest.raises(TransactionError, match="clock_invalid"):
        original.remaining(ClockReading(boot_id="kernel-two", wall=900.0, monotonic=1.0))


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("suffix", ["-base.json", "-target.json", "state.json"])
def test_prepare_crash_boundaries_never_publish_missing_snapshots(
    journal: TransactionJournal,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    when: str,
) -> None:
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
        prepare(journal)
    monkeypatch.undo()
    visible = journal.read()
    if visible is not None:
        assert visible.phase == "prepared"
        assert journal.snapshot(visible, "base") == BASE
        assert journal.snapshot(visible, "target") == TARGET


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_record_replaced_by_special_file_is_never_opened_blocking(
    journal: TransactionJournal,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    prepare(journal)
    path = journal.directory.path / "state.json"
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.symlink_to("outside")
    original = os.open
    seen = False

    def checked_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal seen
        if name == "state.json":
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
            seen = True
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", checked_open)
    with pytest.raises((OSError, TransactionError)):
        journal.read()
    assert seen
