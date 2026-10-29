from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import muli.format_evidence as evidence_module
import muli.store as store_module
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_MEMORY_WATCH_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatEvidence,
    FormatEvidenceError,
    FormatReadLimits,
)
from muli.store import Store
from muli.store_schema import ExistingStoreError


@pytest.fixture
def admitted_store(tmp_path: Path) -> Iterator[Store]:
    store = Store.open_runtime(tmp_path / "data" / "muli.sqlite3", default_mode="auto")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def controlled_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [100.0]
    monkeypatch.setattr(evidence_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store_module, "monotonic", lambda: clock[0])
    return clock


def test_admitted_revalidation_recovers_timeout_with_a_fresh_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data" / "muli.sqlite3"
    store = Store.open_runtime(path, default_mode="auto")
    writer = sqlite3.connect(path)
    clock = [100.0]
    limits = FormatReadLimits()
    first_elapsed = limits.seconds + 0.208
    rows = evidence_module._rows
    read = store_module.read_optional_format_evidence
    admit = store._admit_format
    attempts: list[FormatReadLimits] = []
    failures: list[tuple[str, bool]] = []
    begins: list[bool] = []
    rollbacks: list[bool] = []
    markers: list[bytes] = []
    admitted: list[FormatEvidence] = []
    replacement = json.dumps(CURRENT_FORMAT_STATE.model_dump(), indent=2)
    previous = store.db.execute(
        "SELECT value FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
    ).fetchone()[0]
    changes = store.db.total_changes
    pragmas = {
        name: store.db.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("query_only", "busy_timeout")
    }
    previous_limits = {
        category: store.db.getlimit(category)
        for category in (sqlite3.SQLITE_LIMIT_LENGTH, sqlite3.SQLITE_LIMIT_SQL_LENGTH)
    }

    def observed_rows(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        if sql == "BEGIN":
            begins.append(db.in_transaction)
        result = rows(db, sql, parameters, limit=limit)
        if sql == "ROLLBACK":
            rollbacks.append(db.in_transaction)
        if FORMAT_STATE_KEY in parameters:
            raw = result[0][2]
            assert isinstance(raw, bytes)
            markers.append(raw)
            if len(markers) == 1:
                # Commit an equivalent marker while the first real snapshot is open.
                with writer:
                    writer.execute(
                        "UPDATE runtime_state SET value=? WHERE name=?",
                        (replacement, FORMAT_STATE_KEY),
                    )
                clock[0] += first_elapsed
        return result

    def observed_read(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits or FormatReadLimits())
        if len(attempts) == 2:
            assert not db.in_transaction
            assert {name: db.execute(f"PRAGMA {name}").fetchone()[0] for name in pragmas} == pragmas
            restored_limits = {category: db.getlimit(category) for category in previous_limits}
            assert restored_limits == previous_limits
            # An expired first reader's leaked progress handler would interrupt this query.
            assert (
                db.execute(
                    "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<20) "
                    "SELECT sum(x) FROM n"
                ).fetchone()[0]
                == 210
            )
        try:
            return read(db, limits=limits)
        except FormatEvidenceError as error:
            failures.append((error.code, db.in_transaction))
            raise

    def observed_admission(evidence: FormatEvidence) -> None:
        admitted.append(evidence)
        admit(evidence)

    monkeypatch.setattr(evidence_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store_module, "monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(evidence_module, "_rows", observed_rows)
    monkeypatch.setattr(store_module, "read_optional_format_evidence", observed_read)
    monkeypatch.setattr(store, "_admit_format", observed_admission)
    try:
        store.revalidate_format()

        assert failures == [("format_read_timeout", False)]
        assert begins == rollbacks == [False, False]
        assert markers == [previous.encode("utf-8"), replacement.encode("utf-8")]
        assert len(attempts) == 2
        assert attempts[0] == limits
        assert attempts[1].seconds == pytest.approx(2 * limits.seconds - first_elapsed)
        assert attempts[1].model_dump(exclude={"seconds"}) == limits.model_dump(exclude={"seconds"})
        assert len(admitted) == 1 and admitted[0].state == CURRENT_FORMAT_STATE
        assert store.format_state == CURRENT_FORMAT_STATE and not store.migration_allowed
        assert store.db.total_changes == changes and not store.db.in_transaction
        assert {
            name: store.db.execute(f"PRAGMA {name}").fetchone()[0] for name in pragmas
        } == pragmas
    finally:
        writer.close()
        store.close()


def test_quick_revalidation_reads_and_admits_once(
    admitted_store: Store, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    read = store_module.read_optional_format_evidence
    admit = admitted_store._admit_format
    attempts: list[FormatReadLimits | None] = []
    admitted: list[FormatEvidence] = []

    def observed_read(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits)
        return read(db, limits=limits)

    def observed_admission(evidence: FormatEvidence) -> None:
        admitted.append(evidence)
        admit(evidence)

    monkeypatch.setattr(store_module, "read_optional_format_evidence", observed_read)
    monkeypatch.setattr(admitted_store, "_admit_format", observed_admission)
    admitted_store.revalidate_format()
    assert attempts == [FormatReadLimits()]
    assert len(admitted) == 1 and admitted[0].state == CURRENT_FORMAT_STATE
    assert controlled_clock == [100.0] and not admitted_store.db.in_transaction


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("overrun", [0.0, 0.125], ids=["at-budget", "past-budget"])
def test_successful_read_cleanup_must_finish_before_the_outer_budget(
    admitted_store: Store,
    controlled_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    attempt: int,
    overrun: float,
) -> None:
    store, clock = admitted_store, controlled_clock
    started = clock[0]
    single = FormatReadLimits().seconds
    rows = evidence_module._rows
    restore_busy = f"PRAGMA busy_timeout={store.db.execute('PRAGMA busy_timeout').fetchone()[0]}"
    begins: list[bool] = []
    admitted: list[FormatEvidence] = []
    identity = store._format_identity

    def delayed_cleanup(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        result = rows(db, sql, parameters, limit=limit)
        if sql == "BEGIN":
            begins.append(db.in_transaction)
        if FORMAT_STATE_KEY in parameters and len(begins) == 1 and attempt == 2:
            clock[0] += single + 0.208
        if sql == restore_busy and len(begins) == attempt:
            # The real rollback, limits and final PRAGMA restoration have finished.
            clock[0] = started + 2 * single + overrun
        return result

    monkeypatch.setattr(evidence_module, "_rows", delayed_cleanup)
    monkeypatch.setattr(store, "_admit_format", admitted.append)
    with pytest.raises(FormatEvidenceError, match="^format_read_timeout$"):
        store.revalidate_format()
    assert len(begins) == attempt and admitted == []
    assert store.format_state == CURRENT_FORMAT_STATE and store._format_identity == identity
    assert not store.migration_allowed and not store.db.in_transaction


@pytest.mark.parametrize("overrun", [0.0, 0.125], ids=["at-budget", "past-budget"])
@pytest.mark.parametrize("location", ["query", "cleanup"])
def test_first_timeout_exhausting_the_outer_budget_is_not_retried(
    admitted_store: Store,
    controlled_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    overrun: float,
    location: str,
) -> None:
    rows = evidence_module._rows
    read = store_module.read_optional_format_evidence
    failures: list[FormatEvidenceError] = []
    begins: list[bool] = []
    started = controlled_clock[0]
    single = FormatReadLimits().seconds
    restore_busy = (
        f"PRAGMA busy_timeout={admitted_store.db.execute('PRAGMA busy_timeout').fetchone()[0]}"
    )

    def exhaust_budget(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        result = rows(db, sql, parameters, limit=limit)
        if sql == "BEGIN":
            begins.append(db.in_transaction)
        if FORMAT_STATE_KEY in parameters:
            controlled_clock[0] += 2 * single + overrun if location == "query" else single + 0.208
        if sql == restore_busy and location == "cleanup":
            controlled_clock[0] = started + 2 * single + overrun
        return result

    def observed_read(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        try:
            return read(db, limits=limits)
        except FormatEvidenceError as error:
            failures.append(error)
            raise

    monkeypatch.setattr(evidence_module, "_rows", exhaust_budget)
    monkeypatch.setattr(store_module, "read_optional_format_evidence", observed_read)
    with pytest.raises(FormatEvidenceError) as caught:
        admitted_store.revalidate_format()
    assert len(failures) == len(begins) == 1 and caught.value is failures[0]
    assert caught.value.code == str(caught.value) == "format_read_timeout"
    assert len(caught.value.__notes__) == 1
    assert not admitted_store.db.in_transaction


def test_two_real_timeouts_keep_the_last_reader_error_and_stop(
    admitted_store: Store, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = evidence_module._rows
    read = store_module.read_optional_format_evidence
    attempts: list[FormatReadLimits] = []
    failures: list[FormatEvidenceError] = []
    rollbacks: list[bool] = []

    def delayed_marker(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        result = rows(db, sql, parameters, limit=limit)
        if FORMAT_STATE_KEY in parameters:
            controlled_clock[0] += attempts[-1].seconds + 0.001
        if sql == "ROLLBACK":
            rollbacks.append(db.in_transaction)
        return result

    def observed_read(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits or FormatReadLimits())
        try:
            return read(db, limits=limits)
        except FormatEvidenceError as error:
            failures.append(error)
            raise

    monkeypatch.setattr(evidence_module, "_rows", delayed_marker)
    monkeypatch.setattr(store_module, "read_optional_format_evidence", observed_read)
    with pytest.raises(FormatEvidenceError) as caught:
        admitted_store.revalidate_format()
    assert len(attempts) == len(failures) == 2 and caught.value is failures[1]
    assert caught.value.code == str(caught.value) == "format_read_timeout"
    assert len(caught.value.__notes__) == 1 and rollbacks == [False, False]
    assert not admitted_store.db.in_transaction


def test_two_classified_timeouts_cannot_start_a_third_read_with_time_remaining(
    admitted_store: Store, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    read = store_module.read_optional_format_evidence
    failures = [FormatEvidenceError("format_read_timeout") for _ in range(2)]
    attempts: list[FormatReadLimits | None] = []

    def classified_timeout(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits)
        read(db, limits=limits)
        assert not db.in_transaction
        controlled_clock[0] += 0.125
        # Test the independent attempt cap, without consuming the outer wall budget.
        raise failures[len(attempts) - 1]

    monkeypatch.setattr(store_module, "read_optional_format_evidence", classified_timeout)
    with pytest.raises(FormatEvidenceError) as caught:
        admitted_store.revalidate_format()
    assert len(attempts) == 2 and caught.value is failures[1]
    assert controlled_clock[0] - 100.0 < 2 * FormatReadLimits().seconds
    assert not admitted_store.db.in_transaction


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize(
    "code",
    [
        "format_cleanup_failed",
        "format_query_budget",
        "format_read_failed",
        "format_marker_invalid",
        "format_isolation_unsupported",
    ],
)
def test_non_timeout_errors_keep_the_original_object_and_are_not_retried(
    admitted_store: Store,
    controlled_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    attempt: int,
    code: str,
) -> None:
    read = store_module.read_optional_format_evidence
    failure = FormatEvidenceError(code)
    failure.add_note("fixed test note")
    attempts: list[FormatReadLimits | None] = []

    def fail_after_cleanup(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits)
        read(db, limits=limits)
        assert not db.in_transaction
        if len(attempts) < attempt:
            raise FormatEvidenceError("format_read_timeout")
        raise failure

    monkeypatch.setattr(store_module, "read_optional_format_evidence", fail_after_cleanup)
    with pytest.raises(FormatEvidenceError) as caught:
        admitted_store.revalidate_format()
    assert caught.value is failure and failure.code == str(failure) == code
    assert failure.__notes__ == ["fixed test note"] and len(attempts) == attempt
    assert controlled_clock == [100.0] and not admitted_store.db.in_transaction


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, RuntimeError])
def test_cancellation_and_unknown_errors_are_not_wrapped_or_retried(
    admitted_store: Store,
    controlled_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    attempt: int,
    error_type: type[BaseException],
) -> None:
    read = store_module.read_optional_format_evidence
    failure = error_type("fixed test failure")
    attempts: list[FormatReadLimits | None] = []

    def fail_after_cleanup(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits)
        read(db, limits=limits)
        assert not db.in_transaction
        if len(attempts) < attempt:
            raise FormatEvidenceError("format_read_timeout")
        raise failure

    monkeypatch.setattr(store_module, "read_optional_format_evidence", fail_after_cleanup)
    with pytest.raises(error_type) as caught:
        admitted_store.revalidate_format()
    assert caught.value is failure and len(attempts) == attempt
    assert controlled_clock == [100.0] and not admitted_store.db.in_transaction


def test_real_rollback_failure_takes_priority_over_timeout_without_retry(
    admitted_store: Store, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = evidence_module._rows
    begins: list[bool] = []
    rollbacks: list[bool] = []

    def failed_rollback(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        if sql == "ROLLBACK":
            rollbacks.append(db.in_transaction)
            # End the real snapshot so SQLite itself rejects the reader's ROLLBACK.
            db.rollback()
        result = rows(db, sql, parameters, limit=limit)
        if sql == "BEGIN":
            begins.append(db.in_transaction)
        if FORMAT_STATE_KEY in parameters:
            controlled_clock[0] += FormatReadLimits().seconds + 0.208
        return result

    monkeypatch.setattr(evidence_module, "_rows", failed_rollback)
    with pytest.raises(FormatEvidenceError) as caught:
        admitted_store.revalidate_format()
    assert caught.value.code == str(caught.value) == "format_cleanup_failed"
    assert begins == rollbacks == [True]
    observation = json.loads(caught.value.__notes__[0].split("=", 1)[1])
    assert observation["failure_phase"] == "cleanup"
    assert not admitted_store.db.in_transaction


def test_timeout_with_an_active_transaction_is_not_rolled_back_or_retried(
    admitted_store: Store, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    read = store_module.read_optional_format_evidence
    failure = FormatEvidenceError("format_read_timeout")
    attempts: list[FormatReadLimits | None] = []

    def active_transaction_timeout(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits)
        read(db, limits=limits)
        db.execute("BEGIN")
        raise failure

    monkeypatch.setattr(store_module, "read_optional_format_evidence", active_transaction_timeout)
    try:
        with pytest.raises(FormatEvidenceError) as caught:
            admitted_store.revalidate_format()
        assert caught.value is failure and len(attempts) == 1
        assert admitted_store.db.in_transaction and controlled_clock == [100.0]
    finally:
        admitted_store.db.rollback()


@pytest.mark.parametrize(
    ("change", "code", "admissions"),
    [
        ("deleted", "store_format_marker_missing", 0),
        ("invalid", "format_marker_invalid", 0),
        ("old", "store_format_changed", 1),
        ("identity", "store_format_identity_changed", 1),
        ("owner", "format_main_object_invalid", 0),
        ("schema", "existing_store_schema_unsupported", 1),
    ],
)
def test_second_snapshot_rejects_changed_evidence_without_changing_admitted_state(
    tmp_path: Path,
    admitted_store: Store,
    controlled_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    code: str,
    admissions: int,
) -> None:
    store = admitted_store
    writer = sqlite3.connect(tmp_path / "data" / "muli.sqlite3")
    rows = evidence_module._rows
    admit = store._admit_format
    state, identity = store.format_state, store._format_identity
    assert identity is not None
    total_changes = store.db.total_changes
    begins: list[bool] = []
    admitted: list[FormatEvidence] = []
    changed = False

    def change_after_first_marker(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        nonlocal changed
        result = rows(db, sql, parameters, limit=limit)
        if sql == "BEGIN":
            begins.append(db.in_transaction)
        if FORMAT_STATE_KEY in parameters and not changed:
            changed = True
            with writer:
                if change == "deleted":
                    writer.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
                elif change in {"invalid", "old"}:
                    raw = (
                        "invalid JSON"
                        if change == "invalid"
                        else PRE_MEMORY_WATCH_FORMAT_STATE.model_dump_json()
                    )
                    writer.execute(
                        "UPDATE runtime_state SET value=? WHERE name=?", (raw, FORMAT_STATE_KEY)
                    )
                elif change == "identity":
                    writer.execute(
                        "UPDATE runtime_state SET value='other' WHERE name='store_instance_v1'"
                    )
                elif change == "owner":
                    writer.execute(
                        "UPDATE objects SET data=json_set(data,'$.principal','channel:other') "
                        "WHERE kind='session' AND id=?",
                        (identity[1],),
                    )
                else:
                    writer.execute("ALTER TABLE channel_inbound ADD COLUMN changed_contract TEXT")
            controlled_clock[0] += FormatReadLimits().seconds + 0.208
        return result

    def observed_admission(evidence: FormatEvidence) -> None:
        admitted.append(evidence)
        admit(evidence)

    monkeypatch.setattr(evidence_module, "_rows", change_after_first_marker)
    monkeypatch.setattr(store, "_admit_format", observed_admission)
    try:
        expected_errors = (FormatEvidenceError, FormatAdmissionError, ExistingStoreError)
        with pytest.raises(expected_errors) as caught:
            store.revalidate_format()
        assert caught.value.code == str(caught.value) == code
        assert begins == [True, True] and len(admitted) == admissions
        assert store.format_state is state and store._format_identity == identity
        assert not store.migration_allowed and not store.db.in_transaction
        assert store.db.total_changes == total_changes
    finally:
        writer.close()


def test_timeout_from_admission_is_not_treated_as_a_read_recovery(
    admitted_store: Store, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    read = store_module.read_optional_format_evidence
    failure = FormatEvidenceError("format_read_timeout")
    attempts: list[FormatReadLimits | None] = []
    admitted: list[FormatEvidence] = []

    def observed_read(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        attempts.append(limits)
        return read(db, limits=limits)

    def failed_admission(evidence: FormatEvidence) -> None:
        admitted.append(evidence)
        raise failure

    monkeypatch.setattr(store_module, "read_optional_format_evidence", observed_read)
    monkeypatch.setattr(admitted_store, "_admit_format", failed_admission)
    with pytest.raises(FormatEvidenceError) as caught:
        admitted_store.revalidate_format()
    assert caught.value is failure and len(attempts) == len(admitted) == 1
    assert controlled_clock == [100.0] and not admitted_store.db.in_transaction


def test_original_schema_admission_keeps_its_separate_budget(
    admitted_store: Store, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    admit = admitted_store._admit_format
    admitted: list[FormatEvidence] = []

    def later_admission(evidence: FormatEvidence) -> None:
        admitted.append(evidence)
        controlled_clock[0] += 2 * FormatReadLimits().seconds + 0.125
        admit(evidence)

    monkeypatch.setattr(admitted_store, "_admit_format", later_admission)
    admitted_store.revalidate_format()
    assert len(admitted) == 1 and admitted_store.format_state == CURRENT_FORMAT_STATE
    assert not admitted_store.migration_allowed and not admitted_store.db.in_transaction


def test_unadmitted_legacy_store_does_not_read_or_recover_format(
    tmp_path: Path, controlled_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "legacy.sqlite3")
    changes = store.db.total_changes

    def unexpected_read(
        db: sqlite3.Connection, *, limits: FormatReadLimits | None = None
    ) -> FormatEvidence | None:
        raise AssertionError("An unadmitted Store must not enter read recovery")

    monkeypatch.setattr(store_module, "read_optional_format_evidence", unexpected_read)
    try:
        store.revalidate_format()
        assert store.format_state is None and store.migration_allowed
        assert store.db.total_changes == changes and controlled_clock == [100.0]
    finally:
        store.close()


@pytest.mark.parametrize("entry", ["ordinary", "runtime", "existing", "required", "optional"])
def test_initial_classification_and_public_readers_keep_a_single_attempt(
    tmp_path: Path,
    controlled_clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    path = tmp_path / "data" / "muli.sqlite3"
    Store.open_runtime(path, default_mode="auto").close()
    rows = evidence_module._rows
    markers: list[bool] = []

    def first_read_timeout(
        db: sqlite3.Connection,
        sql: str,
        parameters: Sequence[object] = (),
        *,
        limit: int = 1,
    ) -> list[tuple[Any, ...]]:
        result = rows(db, sql, parameters, limit=limit)
        if FORMAT_STATE_KEY in parameters:
            markers.append(db.in_transaction)
            controlled_clock[0] += FormatReadLimits().seconds + 0.208
        return result

    monkeypatch.setattr(evidence_module, "_rows", first_read_timeout)
    with closing(sqlite3.connect(path)) as db:
        before = tuple(db.iterdump())
        with pytest.raises((FormatEvidenceError, ExistingStoreError)) as caught:
            if entry == "ordinary":
                Store(path).close()
            elif entry == "runtime":
                Store.open_runtime(path, default_mode="auto").close()
            elif entry == "existing":
                Store.open_existing(path).close()
            elif entry == "required":
                evidence_module.read_format_evidence(db)
            else:
                evidence_module.read_optional_format_evidence(db)
        expected = "existing_store_read_budget" if entry == "existing" else "format_read_timeout"
        assert caught.value.code == str(caught.value) == expected
        assert markers == [True] and not db.in_transaction
        assert tuple(db.iterdump()) == before
