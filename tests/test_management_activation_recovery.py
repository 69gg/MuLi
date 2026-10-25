from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

import pytest
from test_management_process_recovery import CASE_SECONDS, checkpoint, cleanup, remaining

from muli.config import ConfigFile
from muli.management_projection import read_projection
from muli.management_transaction import (
    Deadline,
    ServiceIdentity,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.platforms.linux_transaction import clock_reading
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process/nonce recovery")


def prepare(root: Path) -> tuple[TransactionRecord, dict[str, Any], dict[str, Any]]:
    config = ConfigFile(root / "config.json")
    config.update(
        {
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "memory": {"enabled": False, "auto_extract": False},
            "plugins": {"enabled": False},
            "skills": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "attention": {"enabled": False},
            "notifications": {"enabled": False},
            "system_events": {"enabled": False},
            "mcp": {"mcpServers": {}},
        }
    )
    database = root / "data" / "muli.sqlite3"
    with (
        TransactionJournal(root / "journal", database) as journal,
        journal.writer_lease() as writer,
    ):
        store = Store.open_runtime(
            database, default_mode=config.settings.default_mode, durable=True
        )
        try:
            main = store.main_session()
            with journal.locked() as control:
                record = control.prepare(
                    request_id="activation-process-recovery",
                    action="restart",
                    identity=ServiceIdentity(
                        installation="a" * 64,
                        service="b" * 64,
                        store_instance=store.instance_id,
                        main_session=main["id"],
                        origin_boot="fixture-origin",
                    ),
                    base=config.path.read_bytes(),
                    target=config.path.read_bytes(),
                    deadline=Deadline(created=clock_reading(), seconds=30.0, rollback_reserve=5.0),
                    writer=writer,
                )
                # Start at the seam under test; no fixture performs service effects.
                for transition in (
                    Transition(phase="draining"),
                    Transition(phase="activating", intent="stop_base"),
                    Transition(phase="activating", intent="install_target"),
                    Transition(phase="activating", intent="start_target"),
                    Transition(phase="verifying"),
                ):
                    record = control.advance(record.position, transition)
            origin = ReplyOrigin.load(
                {
                    "kind": "channel",
                    "input_id": "original-input",
                    "route": {
                        "name": "telegram",
                        "account_id": "fixture-account",
                        "target": "fixture-owner",
                        "sender_id": "fixture-owner",
                        "kind": "private",
                        "reply_to": "original-message",
                    },
                }
            )
            plan = {
                "id": record.request_id,
                "action": record.action,
                "status": "applying",
                "transaction": record.public(),
                "routing": FrozenRouting.bound(origin).dump(),
            }
            store.event("management.prepared", plan, objects=[("management", plan)])
            unknown = store.put(
                "message",
                {
                    "id": "existing-unknown",
                    "session_id": main["id"],
                    "role": "tool",
                    "content": json.dumps({"status": "unknown"}),
                    "tool_call_id": "original-call",
                    "created_at": 1.0,
                },
            )
            return record, plan, dict(unknown)
        finally:
            store.close()


def test_fresh_controller_observes_released_host_after_activation_caller_is_killed(
    tmp_path: Path,
) -> None:
    before, plan, unknown = prepare(tmp_path)
    repository = Path(__file__).resolve().parents[1]
    worker = repository / "tests" / "helpers" / "management_activation_worker.py"
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

    def spawn(
        role: Literal["host", "first", "fresh"],
        endpoint: int,
        *,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-u", str(worker), role, str(tmp_path), str(endpoint)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            close_fds=True,
            pass_fds=pass_fds,
        )
        processes.append(process)
        return process

    database = tmp_path / "data" / "muli.sqlite3"
    try:
        # Only the host inherits this loopback socket, never a journal/lease fd.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            port = int(listener.getsockname()[1])
            host = spawn("host", listener.fileno(), pass_fds=(listener.fileno(),))
            host_output = bytearray()
            checkpoint(host, b"host-ready", host_output, deadline)
        first = spawn("first", port)
        first_output = bytearray()
        checkpoint(first, b"activated-before-release-record", first_output, deadline)
        with TransactionJournal(tmp_path / "journal", database, create=False) as journal:
            interrupted = journal.read()
            assert interrupted is not None
            assert interrupted.phase == "committed" and interrupted.activation == "pending"
            assert not interrupted.resolved
            assert interrupted.health is not None and interrupted.health.admission == "paused"
            assert interrupted.deadline == before.deadline
            controller_inode = (journal.directory.path / "lock").stat().st_ino
            writer_inode = (journal.writer_directory.path / "lease").stat().st_ino
        first.kill()
        assert first.wait(timeout=remaining(deadline)) == -signal.SIGKILL

        fresh = spawn("fresh", port)
        fresh_output = bytearray()
        checkpoint(fresh, b"fresh-controller-released", fresh_output, deadline)
        assert fresh.wait(timeout=remaining(deadline)) == 0, bytes(fresh_output)
        assert host.poll() is None
        assert host.stdin is not None
        host.stdin.write(b"P")
        host.stdin.flush()
        checkpoint(host, b"projected-once", host_output, deadline)
        assert host.wait(timeout=remaining(deadline)) == 0, bytes(host_output)

        with TransactionJournal(tmp_path / "journal", database, create=False) as journal:
            with journal.writer_lease():
                final = journal.read()
                assert final is not None and final.resolved and final.activation == "released"
                assert final.position.generation == before.generation
                assert final.revision == interrupted.revision + 1
                assert final.deadline == before.deadline and final.challenge == before.challenge
                assert final.identity == before.identity
                assert (journal.directory.path / "lock").stat().st_ino == controller_inode
                assert (journal.writer_directory.path / "lease").stat().st_ino == writer_inode
                store = Store(database, durable=True)
                try:
                    receipt = read_projection(store)
                    assert receipt is not None and receipt.revision == final.revision
                    assert receipt.generation == final.generation
                    assert (
                        receipt.store_instance
                        == store.instance_id
                        == before.identity.store_instance
                    )
                    saved = store.get("management", before.request_id)
                    assert saved is not None and saved["status"] == "completed"
                    assert saved["routing"] == plan["routing"]
                    assert saved["transaction"] == final.public()
                    assert store.get("message", unknown["id"]) == unknown
                    events = store.db.execute(
                        "SELECT data FROM events WHERE type='management.completed'"
                    ).fetchall()
                    assert len(events) == 1 and json.loads(events[0][0]) == saved
                    assert (
                        store.db.execute(
                            "SELECT count(*) FROM objects WHERE kind='management_projection'"
                        ).fetchone()[0]
                        == 1
                    )
                finally:
                    store.close()
    finally:
        cleanup(processes)
