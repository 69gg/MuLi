"""Fixed-role workers for the Linux process-death recovery regression only."""

from __future__ import annotations

import asyncio
import hashlib
import select
import sys
from pathlib import Path
from typing import NoReturn

from muli.management_controller import ServiceObservation, TransactionController
from muli.management_transaction import (
    Deadline,
    FixedLock,
    JournalControl,
    Role,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
)
from muli.platforms.linux_transaction import clock_reading
from muli.store import Store

BASE = b'{"fixture":"base"}'
TARGET = b'{"fixture":"target"}'
GATE_SECONDS = 10.0


def checkpoint(message: bytes) -> None:
    sys.stdout.buffer.write(message + b"\n")
    sys.stdout.buffer.flush()


def expect_busy(lock: FixedLock) -> None:
    try:
        with lock:
            pass
    except TransactionError as exc:
        assert exc.code == "lock_busy"
    else:
        raise AssertionError("owner did not retain its fixed lock")


def owner(root: Path) -> None:
    config = root / "config.json"
    config.write_bytes(BASE)
    database = root / "data" / "muli.sqlite3"
    with (
        TransactionJournal(root / "journal", database) as journal,
        journal.writer_lease() as writer,
    ):
        store = Store(database, durable=True)
        try:
            # Leave committed data in a real open WAL when the process is killed.
            store.db.execute("PRAGMA wal_autocheckpoint=0")
            main = store.main_session()
            with journal.locked() as control:
                record = control.prepare(
                    request_id="process-recovery",
                    action="configure",
                    identity=ServiceIdentity(
                        installation="a" * 64,
                        service="b" * 64,
                        store_instance=store.instance_id,
                        main_session=main["id"],
                        origin_boot="fixture-owner",
                    ),
                    base=BASE,
                    target=TARGET,
                    deadline=Deadline(created=clock_reading(), seconds=30.0, rollback_reserve=5.0),
                    writer=writer,
                )
                plan = {
                    "id": record.request_id,
                    "action": record.action,
                    "status": "applying",
                    "transaction": record.public(),
                }
                store.event("management.prepared", plan, objects=[("management", plan)])
                _, created = store.enqueue_input(
                    {
                        "id": "durable-input",
                        "session_id": main["id"],
                        "content": "committed before owner exit",
                        "source": "web",
                        "principal": "owner",
                    },
                    max_pending=1,
                )
                assert created and not store.db.in_transaction
                assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
                checkpoint(b"owner-ready")
                # There is no graceful release command: the parent must kill us.
                if select.select([sys.stdin.buffer], [], [], GATE_SECONDS)[0]:
                    raise AssertionError("owner must exit through the injected SIGKILL")
                raise TimeoutError("owner kill checkpoint expired")
        finally:
            store.close()


class StoppedService:
    """A stopped fixture host; no systemd, HTTP, launcher or service effects."""

    def __init__(self, journal: TransactionJournal, config: Path) -> None:
        self.journal, self.config = journal, config
        self.observations = 0
        self.effects: list[str] = []

    async def observe(self, record: TransactionRecord) -> ServiceObservation:
        self.observations += 1
        with self.journal.writer_lease():
            writer_free = True
        return ServiceObservation(
            identity_matches=True,
            configuration=hashlib.sha256(self.config.read_bytes()).hexdigest(),
            service="stopped",
            boot=None,
            writer_free=writer_free,
        )

    def forbidden(self, operation: str) -> NoReturn:
        self.effects.append(operation)
        raise AssertionError("service effects are forbidden in this process-death fixture")

    async def drain(self, record: TransactionRecord) -> None:
        self.forbidden("drain")

    async def resume(self, record: TransactionRecord) -> None:
        self.forbidden("resume")

    async def stop(self, record: TransactionRecord, role: Role) -> None:
        self.forbidden("stop")

    async def install(self, record: TransactionRecord, role: Role, data: bytes) -> None:
        self.forbidden("install")

    async def start(self, record: TransactionRecord, role: Role) -> None:
        self.forbidden("start")

    async def activate(self, record: TransactionRecord, role: Role) -> None:
        self.forbidden("activate")


async def drive(control: JournalControl, root: Path) -> None:
    before = control.read()
    assert before is not None and before.phase == "prepared"
    assert control.journal.snapshot(before, "base") == BASE
    assert control.journal.snapshot(before, "target") == TARGET
    backend = StoppedService(control.journal, root / "config.json")
    controller = TransactionController(control, backend, clock_reading, operation_timeout=1.0)
    after = await controller.step()
    assert after.phase == "recovery_required" and after.code == "foreign_state"
    assert not after.resolved and after.health is None
    assert after.identity == before.identity and after.generation == before.generation
    assert after.deadline == before.deadline and after.challenge == before.challenge
    assert after.revision == before.revision + 1
    assert await controller.step() == after
    assert backend.observations == 1 and not backend.effects


def controller(root: Path) -> None:
    database = root / "data" / "muli.sqlite3"
    with TransactionJournal(root / "journal", database, create=False) as journal:
        expect_busy(journal.locked())
        expect_busy(journal.writer_lease())
    checkpoint(b"both-locks-busy")
    if not select.select([sys.stdin.buffer], [], [], GATE_SECONDS)[0]:
        raise TimeoutError("takeover checkpoint expired")
    assert sys.stdin.buffer.read(1) == b"G"
    # Reopen from disk after the other process died; retain no owner/fake state.
    with TransactionJournal(root / "journal", database, create=False) as journal:
        with journal.locked() as control:
            asyncio.run(drive(control, root))
    checkpoint(b"takeover-failed-closed")


def main() -> None:
    assert sys.platform == "linux" and len(sys.argv) == 3
    role, root = sys.argv[1], Path(sys.argv[2])
    if role == "owner":
        owner(root)
    elif role == "controller":
        controller(root)
    else:
        raise AssertionError("unknown fixed worker role")


if __name__ == "__main__":
    main()
