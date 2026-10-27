"""The real private journal rejects publication races before a fresh CI observation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import managed_native_worker as worker
from test_managed_backend import Host
from test_managed_native_ci import ci

from muli.management_transaction import TransactionError, TransactionJournal

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux private journal fixture")


async def test_real_atomic_replacement_defers_only_the_unaccepted_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TransactionJournal(tmp_path / "journal", tmp_path / "muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "adopt", change_config=False)
            try:
                expected = host.record()
                state = journal.directory.path / "state.json"
                data = state.read_bytes()
                original_stat = os.stat
                armed = True
                pauses: list[float] = []
                case = worker.Case(tmp_path)
                deadline = case.deadline

                def racing_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
                    nonlocal armed
                    if (
                        armed
                        and path == "state.json"
                        and kwargs.get("dir_fd") == journal.directory.fd
                    ):
                        armed = False
                        # Preserve a valid record but really replace its inode
                        # between the reader's opened snapshot and final check.
                        control._replace("state.json", data)
                    return original_stat(path, *args, **kwargs)

                async def pause() -> None:
                    pauses.append(case.deadline)

                monkeypatch.setattr(os, "stat", racing_stat)
                monkeypatch.setattr(case, "pause", pause)
                with pytest.raises(TransactionError) as original:
                    case.record(journal)
                assert original.value.code == "file_changed"
                armed = True
                assert await case.observed_record(journal) is None
                assert not armed and pauses == [deadline]
                assert case.deadline == deadline
                assert await case.observed_record(journal) == expected
                assert pauses == [deadline] and journal.read_current() == expected
                saved = (tmp_path / "result.json").read_bytes()
                assert ci.fixture_result(saved)["timeline"] == case.result["timeline"]
                assert case.result["timeline"][0]["state"] == "file_changed"
                assert not any(
                    row["stage"] in {"transaction", "projection"} for row in case.result["timeline"]
                )
            finally:
                host.release_writer()
                await host.backend.__aexit__()


@pytest.mark.parametrize("kind", ["invalid_record", "file_budget", "file_not_private_regular"])
async def test_invalid_private_journal_is_never_retried_or_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    with TransactionJournal(tmp_path / "journal", tmp_path / "muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "adopt", change_config=False)
            state = journal.directory.path / "state.json"
            data = state.read_bytes()
            try:
                if kind == "invalid_record":
                    state.write_bytes(b"not-json")
                elif kind == "file_budget":
                    state.write_bytes(b"x" * (journal.limits.max_record_bytes + 1))
                else:
                    state.chmod(0o644)
                case = worker.Case(tmp_path)
                deadline = case.deadline

                async def forbidden_pause() -> None:
                    raise AssertionError("invalid metadata or JSON must not be retried")

                monkeypatch.setattr(case, "pause", forbidden_pause)
                with pytest.raises(TransactionError) as caught:
                    await case.observed_record(journal)
                assert caught.value.code == kind and case.deadline == deadline
                saved = json.loads((tmp_path / "result.json").read_bytes())
                assert ci.fixture_result(json.dumps(saved).encode()) == saved
                assert saved["timeline"][0]["stage"] == "record_read_failed"
                assert saved["timeline"][0]["state"] == kind
            finally:
                state.chmod(0o600)
                state.write_bytes(data)
                host.release_writer()
                await host.backend.__aexit__()


async def test_repeated_atomic_replacement_exhausts_original_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TransactionJournal(tmp_path / "journal", tmp_path / "muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "adopt", change_config=False)
            try:
                data = (journal.directory.path / "state.json").read_bytes()
                original_stat = os.stat
                clock = [100.0]
                pauses: list[float] = []

                async def advance(seconds: float) -> None:
                    pauses.append(seconds)
                    clock[0] += seconds

                def racing_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
                    if path == "state.json" and kwargs.get("dir_fd") == journal.directory.fd:
                        control._replace("state.json", data)
                    return original_stat(path, *args, **kwargs)

                # Control only the worker's clock and sleep; use its real pause,
                # remaining-deadline check and private-file identity validation.
                monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
                monkeypatch.setattr(worker, "asyncio", SimpleNamespace(sleep=advance))
                case = worker.Case(tmp_path, seconds=worker.POLL_SECONDS * 1.5)
                deadline = case.deadline
                monkeypatch.setattr(os, "stat", racing_stat)
                assert await case.observed_record(journal) is None
                assert await case.observed_record(journal) is None
                with pytest.raises(worker.NativeError, match="^native_deadline_exceeded$"):
                    await case.observed_record(journal)
                assert case.deadline == deadline and clock[0] == deadline
                assert len(pauses) == 2
                assert sum(pauses) == pytest.approx(worker.POLL_SECONDS * 1.5)
                assert not any(
                    row["stage"] in {"transaction", "projection"} for row in case.result["timeline"]
                )
            finally:
                host.release_writer()
                await host.backend.__aexit__()
