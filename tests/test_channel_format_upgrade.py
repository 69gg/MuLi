from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    LEGACY_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import FORMAT_STATE_KEY, read_format_evidence
from muli.management_transaction import (
    ClockReading,
    Deadline,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
)
from muli.store import Store
from muli.store_initialization import StoreConstructionLock, StoreInitializationError

LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Linux exclusive fixed upgrade")


def legacy_database(data_dir: Path) -> Path:
    path = data_dir / "muli.sqlite3"
    store = Store.open_runtime(path, default_mode="auto")
    try:
        with store.db:
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (LEGACY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
            )
    finally:
        store.close()
    return path


def snapshot(path: Path, *, omit_marker: bool = False) -> tuple[str, ...]:
    prefix = f"INSERT INTO \"runtime_state\" VALUES('{FORMAT_STATE_KEY}',"
    with closing(sqlite3.connect(path)) as db:
        return tuple(
            statement
            for statement in db.iterdump()
            if not omit_marker or not statement.startswith(prefix)
        )


def upgrade(data_dir: Path, *, managed_root: Path | None = None) -> bool:
    from muli.channel_format_upgrade import upgrade_channel_ownership_format

    return upgrade_channel_ownership_format(data_dir, managed_root=managed_root)


@LINUX
def test_upgrade_preserves_null_receipts_pending_ingress_and_unknown_outbox(tmp_path: Path) -> None:
    path = legacy_database(tmp_path / "data")
    with closing(sqlite3.connect(path)) as db:
        main = read_format_evidence(db).main_session
        db.execute(
            "INSERT INTO channel_inbound(id,name,receipt,created_at,fingerprint) "
            "VALUES('old-event','onebot',NULL,1,'old-fingerprint')"
        )
        db.execute(
            "INSERT INTO ingress(id,session_id,state,payload,received) VALUES(?,?,?,?,?)",
            ("old-input", main, "pending", '{"id":"old-input","text":"旧输入"}', 1.0),
        )
        db.execute(
            "INSERT INTO channel_outbox(id,name,route,payload,state,next_attempt,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "old-delivery",
                "onebot",
                '{"kind":"private","user_id":"fixture"}',
                '{"text":"旧输出"}',
                "unknown",
                0.0,
                1.0,
                1.0,
            ),
        )
        db.commit()
    before = snapshot(path, omit_marker=True)
    assert upgrade(path.parent)
    assert snapshot(path, omit_marker=True) == before
    with closing(sqlite3.connect(path)) as db:
        assert read_format_evidence(db).state == CURRENT_FORMAT_STATE
    assert not upgrade(path.parent)
    assert snapshot(path, omit_marker=True) == before


@LINUX
@pytest.mark.parametrize("lock", ["writer", "other_root_writer", "controller", "constructor"])
def test_fixed_upgrade_refuses_real_cooperative_lock_owners(tmp_path: Path, lock: str) -> None:
    path = legacy_database(tmp_path / "data")
    before = snapshot(path)
    root = (
        tmp_path / "custom-journal"
        if lock == "other_root_writer"
        else path.parent / "management" / "transaction-v1"
    )
    with TransactionJournal(root, path) as journal:
        held = (
            journal.writer_lease()
            if lock in {"writer", "other_root_writer"}
            else journal.locked()
            if lock == "controller"
            else StoreConstructionLock(path)
        )
        with (
            held,
            pytest.raises(
                (TransactionError, StoreInitializationError),
                match="lock_busy|store_initialization_busy",
            ),
        ):
            upgrade(path.parent)
    assert snapshot(path) == before


@LINUX
@pytest.mark.parametrize("source", ["unmarked", "mixed", "unknown"])
def test_fixed_upgrade_never_guesses_a_source_contract(tmp_path: Path, source: str) -> None:
    path = legacy_database(tmp_path / "data")
    with closing(sqlite3.connect(path)) as db:
        if source == "unmarked":
            db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
        else:
            state = LEGACY_FORMAT_STATE.model_copy(
                update={
                    "formats": LEGACY_FORMAT_STATE.formats.model_copy(
                        update={"objects": 2} if source == "mixed" else {"memory": 9}
                    )
                }
            )
            db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (state.model_dump_json(), FORMAT_STATE_KEY),
            )
        db.commit()
    before = snapshot(path)
    with pytest.raises(
        ValueError, match="store_format_upgrade_source_unsupported|format_marker_missing"
    ):
        upgrade(path.parent)
    assert snapshot(path) == before


def test_live_store_rejects_a_marker_change_without_changing_its_admitted_state(
    tmp_path: Path,
) -> None:
    store = Store.open_runtime(tmp_path / "muli.sqlite3", default_mode="auto")
    try:
        with store.db:
            store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name=?",
                (LEGACY_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
            )
        changes = store.db.total_changes
        with pytest.raises(FormatAdmissionError, match="store_format_changed"):
            store.revalidate_format()
        assert store.format_state == CURRENT_FORMAT_STATE
        assert store.db.total_changes == changes
    finally:
        store.close()


@LINUX
@pytest.mark.parametrize(
    "managed",
    ["record", "projection", "explicit_root", "queued_adopt", "prepared_switch", "transaction"],
)
def test_fixed_upgrade_refuses_managed_ownership(tmp_path: Path, managed: str) -> None:
    path = legacy_database(tmp_path / "data")
    root = path.parent / "management" / "transaction-v1"
    if managed == "record":
        with closing(sqlite3.connect(path)) as db:
            evidence = read_format_evidence(db)
        with TransactionJournal(root, path) as journal:
            with journal.locked() as control, journal.writer_lease() as writer:
                control.prepare(
                    request_id="fixture-request",
                    action="restart",
                    identity=ServiceIdentity(
                        installation="a" * 64,
                        service="b" * 64,
                        store_instance=evidence.store_instance,
                        main_session=evidence.main_session,
                        origin_boot="fixture-boot",
                    ),
                    base=b"{}",
                    target=b"{}",
                    deadline=Deadline(
                        created=ClockReading(boot_id="fixture-kernel", wall=1000.0, monotonic=10.0),
                        seconds=60.0,
                        rollback_reserve=10.0,
                    ),
                    writer=writer,
                )
    elif managed == "projection":
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "INSERT INTO objects(kind,id,data,updated) "
                "VALUES('management_projection','transaction-v1','{}',1)"
            )
            db.commit()
    elif managed in {"queued_adopt", "prepared_switch", "transaction"}:
        plan: dict[str, Any] = {
            "id": "fixture-plan",
            "action": "adopt" if managed == "queued_adopt" else "switch",
            "status": "queued" if managed == "queued_adopt" else "applying",
        }
        if managed == "transaction":
            plan.update(action="restart", transaction={"generation": "c" * 32})
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "INSERT INTO objects(kind,id,data,updated) VALUES('management',?,?,1)",
                (plan["id"], json.dumps(plan)),
            )
            db.commit()
    before = snapshot(path)
    with pytest.raises(FormatAdmissionError, match="store_format_managed_upgrade_unsupported"):
        upgrade(path.parent, managed_root=root if managed == "explicit_root" else None)
    assert snapshot(path) == before


@LINUX
def test_commit_denial_rolls_back_the_real_marker_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_database(tmp_path / "data")
    before = snapshot(path)
    connect = sqlite3.connect
    commits: list[bool] = []

    def authorize(action: int, first: str | None, second: str | None, *rest: Any) -> int:
        if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            commits.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def guarded(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.set_authorizer(authorize)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", guarded)
        with pytest.raises(sqlite3.DatabaseError):
            upgrade(path.parent)
    assert commits == [True]
    assert snapshot(path) == before


def test_non_linux_upgrade_refuses_before_touching_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli.channel_format_upgrade import upgrade_channel_ownership_format

    data_dir = tmp_path / "not-created"
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_platform_unsupported"):
        upgrade_channel_ownership_format(data_dir)
    assert not data_dir.exists()


@pytest.mark.parametrize("marked", [True, False])
def test_normal_application_rejects_old_stores_before_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked: bool
) -> None:
    from muli.config import ConfigFile
    from muli.runtime import Runtime
    from muli.server import create_app

    path = legacy_database(tmp_path / "data")
    if not marked:
        with closing(sqlite3.connect(path)) as db:
            db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
            db.commit()
    before = snapshot(path)

    def forbid_recovery(runtime: Runtime) -> None:
        raise AssertionError("Old stores must fail before application recovery")

    monkeypatch.setattr(Runtime, "_recover", forbid_recovery)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_required"):
        create_app(ConfigFile(tmp_path / "config.json"), path.parent)
    assert snapshot(path) == before


@LINUX
async def test_upgraded_store_enters_real_linux_boot_session_and_application_lifespan(
    tmp_path: Path,
) -> None:
    from muli.config import ConfigFile
    from muli.platforms.linux_bootstrap import open_boot_session
    from muli.platforms.linux_transaction import ObservationError, ServiceSnapshot
    from muli.server import create_app
    from muli.service import UserService

    class OfflineForegroundProbe:
        async def read(self, service: UserService) -> ServiceSnapshot:
            raise ObservationError("fixture_foreground")

    path = legacy_database(tmp_path / "data")
    assert upgrade(path.parent)
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    session = await open_boot_session(
        UserService(config.path, path.parent), path, probe=OfflineForegroundProbe()
    )
    try:
        app = create_app(config, path.parent, boot_session=session)
        async with app.router.lifespan_context(app):
            assert app.state.runtime.store.format_state == CURRENT_FORMAT_STATE
            assert app.state.runtime.main_session()["principal"] == "owner"
    finally:
        session.close_after_writers()
    with TransactionJournal(path.parent / "management" / "transaction-v1", path) as journal:
        with journal.writer_lease() as writer:
            writer.check()
