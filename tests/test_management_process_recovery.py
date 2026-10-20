from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from muli.management_transaction import TransactionJournal
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux flock/SIGKILL recovery")
CASE_SECONDS = 15.0
OUTPUT_BYTES = 4096
CLEANUP_SECONDS = 2.0


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    assert seconds > 0, "process recovery checkpoint exceeded the case deadline"
    return seconds


def checkpoint(
    process: subprocess.Popen[bytes],
    expected: bytes,
    output: bytearray,
    deadline: float,
) -> None:
    assert process.stdout is not None
    start = len(output)
    while b"\n" not in output[start:]:
        ready, _, _ = select.select([process.stdout], [], [], remaining(deadline))
        assert ready, f"worker checkpoint timed out: {bytes(output)!r}"
        chunk = os.read(process.stdout.fileno(), min(1024, OUTPUT_BYTES - len(output) + 1))
        assert chunk, f"worker exited before its checkpoint: {bytes(output)!r}"
        output.extend(chunk)
        assert len(output) <= OUTPUT_BYTES, "worker exceeded its bounded output budget"
    assert bytes(output[start:]) == expected + b"\n", bytes(output)


def cleanup(processes: list[subprocess.Popen[bytes]]) -> None:
    # Kill all owned children before waiting, even if a previous assertion failed.
    for process in processes:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    unreaped: list[int] = []
    for process in reversed(processes):
        try:
            process.wait(timeout=CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            unreaped.append(process.pid)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except BrokenPipeError:
                        pass
    assert not unreaped, f"failed to reap owned recovery workers: {unreaped}"


def test_killed_writer_releases_real_locks_for_independent_fail_closed_takeover(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    worker = repository / "tests" / "helpers" / "management_recovery_worker.py"
    environment = dict(os.environ)
    environment.update(
        MULI_HOME=str(tmp_path / "muli-home"),
        PYTHONPATH=str(repository / "src"),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    for name in ("MULI_CONFIG", "MULI_DATA_DIR", "PYTHONOPTIMIZE"):
        environment.pop(name, None)
    processes: list[subprocess.Popen[bytes]] = []
    deadline = time.monotonic() + CASE_SECONDS
    database = tmp_path / "data" / "muli.sqlite3"
    try:
        owner = subprocess.Popen(
            [sys.executable, "-u", str(worker), "owner", str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            close_fds=True,
        )
        processes.append(owner)
        checkpoint(owner, b"owner-ready", bytearray(), deadline)
        with TransactionJournal(tmp_path / "journal", database, create=False) as journal:
            before = journal.read()
            assert before is not None and before.phase == "prepared"
            controller_inode = (journal.directory.path / "lock").stat().st_ino
            writer_inode = (journal.writer_directory.path / "lease").stat().st_ino
        assert database.with_name(database.name + "-wal").stat().st_size > 0

        controller = subprocess.Popen(
            [sys.executable, "-u", str(worker), "controller", str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            close_fds=True,
        )
        processes.append(controller)
        controller_output = bytearray()
        checkpoint(controller, b"both-locks-busy", controller_output, deadline)
        owner.kill()
        assert owner.wait(timeout=remaining(deadline)) == -signal.SIGKILL
        assert controller.stdin is not None
        controller.stdin.write(b"G")
        controller.stdin.flush()
        checkpoint(controller, b"takeover-failed-closed", controller_output, deadline)
        assert controller.wait(timeout=remaining(deadline)) == 0, bytes(controller_output)

        with TransactionJournal(tmp_path / "journal", database, create=False) as journal:
            with journal.writer_lease():
                after = journal.read()
                assert after is not None and after.phase == "recovery_required"
                assert after.code == "foreign_state" and not after.resolved
                assert after.generation == before.generation and after.identity == before.identity
                assert after.deadline == before.deadline and after.challenge == before.challenge
                assert after.revision == before.revision + 1
                assert (journal.directory.path / "lock").stat().st_ino == controller_inode
                assert (journal.writer_directory.path / "lease").stat().st_ino == writer_inode
                assert journal.snapshot(after, "base") == (tmp_path / "config.json").read_bytes()
                store = Store(database, durable=True)
                try:
                    assert store.instance_id == before.identity.store_instance
                    assert store.existing_main_session_id() == before.identity.main_session
                    assert store.pending_inputs(before.identity.main_session) == 1
                    row = store.db.execute(
                        "SELECT state,payload FROM ingress WHERE id='durable-input'"
                    ).fetchone()
                    assert row is not None and row["state"] == "pending"
                    assert json.loads(row["payload"]) == {
                        "id": "durable-input",
                        "session_id": before.identity.main_session,
                        "content": "committed before owner exit",
                        "source": "web",
                        "principal": "owner",
                    }
                    plan = store.get("management", before.request_id)
                    assert plan is not None and plan["status"] == "applying"
                    assert plan["transaction"] == before.public()
                    assert store.get("management_projection", "transaction-v1") is None
                    assert (
                        store.db.execute(
                            "SELECT count(*) FROM events WHERE type='management.completed'"
                        ).fetchone()[0]
                        == 0
                    )
                finally:
                    store.close()
    finally:
        cleanup(processes)
