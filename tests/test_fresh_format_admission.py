from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import muli.store as store_module
from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelManager, ChannelsConfig
from muli.config import ConfigFile
from muli.events import EventBus
from muli.extensions import ExtensionError
from muli.format_admission import (
    CURRENT_FORMAT_STATE,
    PRE_MEMORY_WATCH_FORMAT_STATE,
    FormatAdmissionError,
)
from muli.format_evidence import (
    FORMAT_STATE_KEY,
    FormatEvidenceError,
    read_optional_format_evidence,
)
from muli.memory import Memory
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store
from muli.store_initialization import (
    CreatedDatabase,
    StoreConstructionLock,
    StoreInitializationError,
)
from muli.store_schema import ExistingStoreError

_MUTATIONS = {
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
}


def no_model(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("This test must never construct a model client")


async def no_submit(**kwargs: Any) -> dict[str, Any]:
    raise AssertionError("This test must never submit channel input")


def snapshot(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return tuple(db.iterdump())


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "fresh # directory" / "muli.sqlite3"
    store = Store.open_runtime(path, default_mode="auto")
    try:
        assert store.format_state == CURRENT_FORMAT_STATE
    finally:
        store.close()
    return path


def deny_construction_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[sqlite3.Connection], list[int]]:
    connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []
    mutations: list[int] = []

    def authorizer(action: int, first: str | None, second: str | None, *args: Any) -> int:
        if action in _MUTATIONS or (
            action == sqlite3.SQLITE_PRAGMA and first == "journal_mode" and second is not None
        ):
            mutations.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def guarded(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)
        db.set_authorizer(authorizer)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", guarded)
    return connections, mutations


@pytest.mark.parametrize("opener", ["ordinary", "runtime", "existing"])
def test_pre_watch_marker_is_refused_before_any_construction_write(
    database: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_MEMORY_WATCH_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)
    connections, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises(
        (FormatAdmissionError, ExistingStoreError), match="store_format_upgrade_required"
    ):
        if opener == "ordinary":
            Store(database)
        elif opener == "runtime":
            Store.open_runtime(database, default_mode="auto")
        else:
            Store.open_existing(database)
    assert mutations == [] and snapshot(database) == before
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


def test_pre_watch_runtime_rejects_before_business_recovery(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE runtime_state SET value=? WHERE name=?",
            (PRE_MEMORY_WATCH_FORMAT_STATE.model_dump_json(), FORMAT_STATE_KEY),
        )
        db.commit()
    before = snapshot(database)

    def forbidden_recovery(runtime: Runtime) -> None:
        raise AssertionError("The pre-watch marker must fail before business recovery")

    monkeypatch.setattr(Runtime, "_recover", forbidden_recovery)
    _, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_required"):
        Runtime(ConfigFile(tmp_path / "config.json"), database.parent, no_model)
    assert mutations == [] and snapshot(database) == before


def test_frozen_v2_112_reader_refuses_the_new_marker(database: Path) -> None:
    # See fixtures/format-v2.112.md: this runs frozen reader code with the
    # current unchanged evidence models, not an installed old release package.
    path = Path(__file__).parent / "fixtures" / "format_admission_v2_112.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "34983abfaef33790d6517805a5b11edf6afb8f7a1d56ab282c12199a1f53618d"
    )
    spec = importlib.util.spec_from_file_location("muli_frozen_format_admission_v2_112", path)
    assert spec is not None and spec.loader is not None
    old_reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old_reader)
    assert old_reader.CURRENT_FORMAT_STATE == PRE_MEMORY_WATCH_FORMAT_STATE
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        evidence = read_optional_format_evidence(db)
        assert evidence is not None and evidence.state == CURRENT_FORMAT_STATE
        old_reader.require_current_format(replace(evidence, state=PRE_MEMORY_WATCH_FORMAT_STATE))
        with pytest.raises(old_reader.FormatAdmissionError, match="store_format_unsupported"):
            old_reader.require_current_format(evidence)
        assert db.total_changes == 0
    assert snapshot(database) == before


@pytest.mark.parametrize("mode", ["ask", "auto", "yolo"])
async def test_real_runtime_publishes_complete_format_before_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"default_mode": mode})
    recovered = False
    recover = Runtime._recover

    def observe(runtime: Runtime) -> None:
        nonlocal recovered
        assert runtime.store.format_state == CURRENT_FORMAT_STATE
        assert runtime.store.main_session()["mode"] == mode
        assert not runtime.store.migration_allowed
        assert runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        assert runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        recover(runtime)
        recovered = True

    monkeypatch.setattr(Runtime, "_recover", observe)
    runtime = Runtime(config, tmp_path / "data", no_model)
    try:
        assert recovered
        assert set(runtime.store.format_state.formats.model_dump()) == {
            "database",
            "objects",
            "configuration",
            "ingress",
            "outbox",
            "memory",
            "install_profile",
        }
        assert runtime.store.main_session()["principal"] == "owner"
    finally:
        await runtime.close()


@pytest.mark.parametrize("opener", ["ordinary", "runtime", "existing"])
async def test_every_store_entry_reopens_marked_data_without_migration_and_allows_business_writes(
    database: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    with closing(sqlite3.connect(database)) as plugin:
        plugin.execute("CREATE TABLE plugin_private(id TEXT PRIMARY KEY,value TEXT)")
        plugin.execute("CREATE INDEX plugin_private_value ON plugin_private(value)")
        plugin.execute("INSERT INTO plugin_private VALUES('plugin-row','kept')")
        plugin.commit()
    before = snapshot(database)
    _, mutations = deny_construction_writes(monkeypatch)
    store = (
        Store(database)
        if opener == "ordinary"
        else Store.open_runtime(database, default_mode="yolo")
        if opener == "runtime"
        else Store.open_existing(database)
    )
    try:
        assert store.format_state == CURRENT_FORMAT_STATE and not store.migration_allowed
        assert store.main_session()["mode"] == "auto"
        Memory(store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(no_model)) as client:
            manager = ChannelManager(
                store, EventBus(store), ChannelsConfig(), no_submit, client=client
            )
            await manager.close()
        assert mutations == [] and snapshot(database) == before
        assert store.db.total_changes == 0
        assert store.db.execute("SELECT value FROM plugin_private").fetchone()[0] == "kept"
        store.db.set_authorizer(None)
        store.put("fixture", {"id": "normal-business", "status": "completed"})
        assert store.get("fixture", "normal-business")["status"] == "completed"
    finally:
        store.close()


@pytest.mark.parametrize("previous", ["empty_file", "core_only", "complete_legacy"])
def test_existing_unmarked_data_never_receives_a_creation_certificate(
    tmp_path: Path, previous: str
) -> None:
    path = tmp_path / "old.sqlite3"
    if previous == "empty_file":
        path.touch()
    else:
        old = Store(path)
        try:
            assert old.format_state is None and old.migration_allowed
            if previous == "complete_legacy":
                Memory(old)
                old.prepare_channels()
                old.main_session()
        finally:
            old.close()
    before = snapshot(path)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_required"):
        Store.open_runtime(path, default_mode="auto")
    assert snapshot(path) == before
    with closing(sqlite3.connect(path)) as db:
        assert read_optional_format_evidence(db) is None


@pytest.mark.parametrize("failure", ["memory", "channel", "main", "publish"])
def test_failed_preparation_cannot_be_retried_as_fresh_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "failed.sqlite3"

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("injected preparation failure")

    with monkeypatch.context() as patch:
        if failure == "memory":
            patch.setattr("muli.memory.Memory", fail)
        elif failure == "channel":
            patch.setattr(Store, "prepare_channels", fail)
        elif failure == "main":
            patch.setattr(Store, "main_session", fail)
        else:
            patch.setattr(store_module, "publish_created_format", fail)
        with pytest.raises(ValueError, match="injected preparation failure"):
            Store.open_runtime(path, default_mode="auto")
    with closing(sqlite3.connect(path)) as db:
        assert read_optional_format_evidence(db) is None
    before = snapshot(path)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_required"):
        Store.open_runtime(path, default_mode="auto")
    assert snapshot(path) == before


def test_creation_claim_is_rechecked_before_core_schema_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "claim.sqlite3"
    check = CreatedDatabase.check
    checks = 0

    def reject_second_check(creation: CreatedDatabase) -> None:
        nonlocal checks
        checks += 1
        check(creation)
        if checks == 2:
            raise StoreInitializationError("store_creation_identity_changed")

    monkeypatch.setattr(CreatedDatabase, "check", reject_second_check)
    with pytest.raises(StoreInitializationError, match="store_creation_identity_changed"):
        Store.open_runtime(path, default_mode="auto")
    assert checks == 2
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT name FROM sqlite_schema").fetchall() == []


def test_marker_commit_failure_rolls_back_only_the_marker_and_never_reissues_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "commit.sqlite3"
    publish = store_module.publish_created_format

    def fail_commit(db: sqlite3.Connection, creation: CreatedDatabase) -> None:
        def authorizer(action: int, first: str | None, *args: Any) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorizer)
        try:
            publish(db, creation)
        finally:
            db.set_authorizer(None)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "publish_created_format", fail_commit)
        with pytest.raises(sqlite3.DatabaseError):
            Store.open_runtime(path, default_mode="auto")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT 1 FROM memory_state").fetchone() is not None
        assert db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        assert read_optional_format_evidence(db) is None
    before = snapshot(path)
    with pytest.raises(FormatAdmissionError, match="store_format_upgrade_required"):
        Store.open_runtime(path, default_mode="auto")
    assert snapshot(path) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows denies replacing the open creation handle")
def test_connection_path_replacement_does_not_initialize_the_existing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, other = tmp_path / "claimed.sqlite3", tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(other)) as db:
        db.execute("CREATE TABLE original(value TEXT)")
        db.execute("INSERT INTO original VALUES('retained')")
        db.commit()
    other.chmod(0o640)
    before = snapshot(other)
    connect = sqlite3.connect

    def replace_then_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        path.rename(tmp_path / "detached-created.sqlite3")
        other.rename(path)
        return connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", replace_then_connect)
        with pytest.raises(StoreInitializationError, match="store_creation_identity_changed"):
            Store.open_runtime(path, default_mode="auto")
    assert snapshot(path) == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.parametrize(
    "change",
    ["deleted", "invalid", "partial", "unknown_metadata", "unknown_domain", "identity", "owner"],
)
def test_admitted_state_never_becomes_legacy_after_its_evidence_is_changed(
    database: Path, change: str
) -> None:
    store = Store(database)
    try:
        with store.db:
            if change == "deleted":
                store.db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
            elif change == "identity":
                store.db.execute(
                    "UPDATE runtime_state SET value='other' WHERE name='store_instance_v1'"
                )
            elif change == "owner":
                store.db.execute(
                    "UPDATE objects SET data=json_set(data,'$.principal','channel:other') "
                    "WHERE kind='session' AND id=?",
                    (store.main_session()["id"],),
                )
            else:
                data = CURRENT_FORMAT_STATE.model_dump()
                if change == "partial":
                    del data["formats"]["outbox"]
                elif change == "unknown_metadata":
                    data["metadata_version"] = 2
                elif change == "unknown_domain":
                    data["formats"]["ingress"] = 9
                raw = "invalid JSON" if change == "invalid" else json.dumps(data)
                store.db.execute(
                    "UPDATE runtime_state SET value=? WHERE name=?", (raw, FORMAT_STATE_KEY)
                )
        changes = store.db.total_changes
        with pytest.raises((FormatAdmissionError, FormatEvidenceError, ExistingStoreError)):
            store.revalidate_format()
        assert store.format_state == CURRENT_FORMAT_STATE and not store.migration_allowed
        assert store.db.total_changes == changes
    finally:
        store.close()


@pytest.mark.parametrize("name", ["text_future", "blob_current", "blob_future", "text_and_blob"])
def test_unknown_or_blob_marker_family_never_enters_legacy_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    path = tmp_path / "invalid.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE runtime_state(name TEXT PRIMARY KEY,value TEXT NOT NULL)")
        markers: list[tuple[str | bytes, str]] = [
            ("format_state_v2" if name == "text_future" else b"format_state_v1", "{}")
        ]
        if name == "blob_future":
            markers = [(b"format_state_v2", "{}")]
        elif name == "text_and_blob":
            markers.append((FORMAT_STATE_KEY, CURRENT_FORMAT_STATE.model_dump_json()))
        db.executemany("INSERT INTO runtime_state VALUES(?,?)", markers)
        db.commit()
    before = snapshot(path)
    connections, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises(FormatEvidenceError, match="format_marker_unknown"):
        Store.open_runtime(path, default_mode="auto")
    assert mutations == [] and snapshot(path) == before
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


@pytest.mark.parametrize("malformed", ["json", "partial", "version", "oversized"])
def test_malformed_persisted_marker_rejects_before_any_legacy_write(
    database: Path, monkeypatch: pytest.MonkeyPatch, malformed: str
) -> None:
    data = CURRENT_FORMAT_STATE.model_dump()
    if malformed == "partial":
        del data["formats"]["outbox"]
    elif malformed == "version":
        data["formats"]["objects"] = 9
    raw = (
        "{"
        if malformed == "json"
        else " " * 16_385
        if malformed == "oversized"
        else json.dumps(data)
    )
    with closing(sqlite3.connect(database)) as db:
        db.execute("UPDATE runtime_state SET value=? WHERE name=?", (raw, FORMAT_STATE_KEY))
        db.commit()
    before = snapshot(database)
    _, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises((FormatEvidenceError, FormatAdmissionError)):
        Store(database)
    assert mutations == [] and snapshot(database) == before


@pytest.mark.parametrize("marked", [False, True])
def test_noncanonical_table_name_is_rejected_instead_of_becoming_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked: bool
) -> None:
    path = tmp_path / "case.sqlite3"
    store = Store.open_runtime(path, default_mode="auto") if marked else Store(path)
    store.close()
    with closing(sqlite3.connect(path)) as db:
        # SQLite may reject a direct case-only rename as a name collision.
        db.execute("ALTER TABLE runtime_state RENAME TO temporary_runtime_state")
        db.execute("ALTER TABLE temporary_runtime_state RENAME TO RUNTIME_STATE")
        db.commit()
    before = snapshot(path)
    _, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises(FormatEvidenceError, match="format_schema_unsupported"):
        Store(path)
    assert mutations == [] and snapshot(path) == before


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX channel_outbox_ready",
        "DROP TABLE memory_alias_evidence",
        "DELETE FROM memory_state WHERE name='alias_evidence_version'",
        "DELETE FROM runtime_state WHERE name='main_session'",
    ],
)
def test_marked_missing_structure_or_local_evidence_is_not_repaired(
    database: Path, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    with closing(sqlite3.connect(database)) as db:
        db.execute(sql)
        db.commit()
    before = snapshot(database)
    connections, mutations = deny_construction_writes(monkeypatch)
    with pytest.raises((FormatEvidenceError, ExistingStoreError)):
        Store(database)
    assert mutations == [] and snapshot(database) == before
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


def test_constructor_lock_is_fixed_noninheritable_and_blocks_a_second_process(
    tmp_path: Path,
) -> None:
    path = tmp_path / "locked.sqlite3"
    # One bounded child only; it performs no runtime, model or service work.
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from muli.store_initialization import StoreConstructionLock,StoreInitializationError\n"
        "try:\n with StoreConstructionLock(Path(sys.argv[1])): pass\n"
        "except StoreInitializationError as e:\n print(e.code)\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    with StoreConstructionLock(path) as lock:
        assert lock.fd is not None and not os.get_inheritable(lock.fd)
        inode = lock.path.stat().st_ino
        child = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert child.stdout.strip() == "store_initialization_busy" and not child.stderr
        with pytest.raises(StoreInitializationError, match="store_initialization_busy"):
            Store(path)
        assert not path.exists()
    with StoreConstructionLock(path) as again:
        assert again.path.stat().st_ino == inode


@pytest.mark.parametrize("opener", ["ordinary", "runtime"])
def test_second_real_constructor_cannot_migrate_the_database_before_marker_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    finish = Store._finish_runtime_creation
    observed = False
    script = (
        "import sys\nfrom pathlib import Path\nfrom muli.store import Store\n"
        "from muli.store_initialization import StoreInitializationError\n"
        "try:\n"
        " s=Store(Path(sys.argv[1])) if sys.argv[2]=='ordinary' else "
        "Store.open_runtime(Path(sys.argv[1]),default_mode='yolo')\n"
        " s.close()\n"
        "except StoreInitializationError as e:\n print(e.code)\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    def before_publication(
        store: Store, creation: CreatedDatabase, lock: StoreConstructionLock, default_mode: str
    ) -> None:
        nonlocal observed
        assert path.exists() and read_optional_format_evidence(store.db) is None
        before = tuple(store.db.iterdump())
        child = subprocess.run(
            [sys.executable, "-c", script, str(path), opener],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert child.stdout.strip() == "store_initialization_busy" and not child.stderr
        assert tuple(store.db.iterdump()) == before
        observed = True
        finish(store, creation, lock, default_mode)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_finish_runtime_creation", before_publication)
        store = Store.open_runtime(path, default_mode="auto")
    try:
        assert observed and store.format_state == CURRENT_FORMAT_STATE
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert reopened.format_state == CURRENT_FORMAT_STATE and not reopened.migration_allowed
        assert reopened.main_session()["mode"] == "auto"
    finally:
        reopened.close()


async def test_channel_reconfigure_checks_format_before_closing_its_current_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data", no_model)
    app = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    original = app.manager.close
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True
        await original()

    monkeypatch.setattr(app.manager, "close", close)
    try:
        with runtime.store.db:
            runtime.store.db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
        with pytest.raises(FormatAdmissionError, match="store_format_marker_missing"):
            await app.reconfigure()
        assert not closed
    finally:
        await app.close()
        await runtime.close()


async def test_server_checks_format_before_closing_components_or_saving_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data", no_model)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    closed = False
    original = app.state.attention.close

    async def close() -> None:
        nonlocal closed
        closed = True
        await original()

    monkeypatch.setattr(app.state.attention, "close", close)
    try:
        before = runtime.config.settings.model_dump()
        target = runtime.config.validate_changes({"persona": {"append": "uncommitted"}})
        with runtime.store.db:
            runtime.store.db.execute("DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,))
        async with runtime.configuration_lock:
            with pytest.raises(FormatAdmissionError, match="store_format_marker_missing"):
                await app.state.management.apply_settings(target)
        assert not closed and runtime.config.settings.model_dump() == before
    finally:
        await app.state.close()


async def test_server_rechecks_format_after_component_close_before_saving_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(ConfigFile(tmp_path / "config.json"), tmp_path / "data", no_model)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    before = runtime.config.settings.model_dump()
    target = runtime.config.validate_changes({"persona": {"append": "must not persist"}})
    original_close = app.state.integration.close
    replace = runtime.config.replace
    saved: list[dict[str, Any]] = []
    changed = False

    async def close_then_change() -> None:
        nonlocal changed
        await original_close()
        if not changed:
            changed = True
            with runtime.store.db:
                runtime.store.db.execute(
                    "DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
                )

    def record_replace(settings: Any) -> Any:
        saved.append(settings.model_dump())
        return replace(settings)

    monkeypatch.setattr(app.state.integration, "close", close_then_change)
    monkeypatch.setattr(runtime.config, "replace", record_replace)
    try:
        async with runtime.configuration_lock:
            with pytest.raises(FormatAdmissionError, match="store_format_marker_missing"):
                await app.state.management.apply_settings(target)
        assert changed and target.model_dump() not in saved
        assert runtime.config.settings.model_dump() == before
        assert ConfigFile(runtime.config.path).settings.model_dump() == before
    finally:
        await app.state.close()


async def test_extension_candidate_is_discarded_when_format_changes_during_final_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"skills": {"directory": str(tmp_path / "skills")}})
    runtime = Runtime(config, tmp_path / "data", no_model)
    before = config.settings.model_dump()
    fingerprints = discarded = 0
    discard = runtime.mcp.discard

    async def fingerprint(settings: Any) -> str:
        nonlocal fingerprints
        fingerprints += 1
        if fingerprints == 2:
            with runtime.store.db:
                runtime.store.db.execute(
                    "DELETE FROM runtime_state WHERE name=?", (FORMAT_STATE_KEY,)
                )
        await asyncio.sleep(0)
        return "same"

    async def record_discard(candidate: Any) -> None:
        nonlocal discarded
        discarded += 1
        await discard(candidate)

    def no_publish(*args: Any) -> Any:
        raise AssertionError("An invalid store must not publish candidate tools")

    monkeypatch.setattr(runtime.extensions, "fingerprint", fingerprint)
    monkeypatch.setattr(runtime.mcp, "discard", record_discard)
    monkeypatch.setattr(runtime.mcp, "publish", no_publish)
    try:
        async with runtime.configuration_lock:
            with pytest.raises(ExtensionError):
                await runtime.extensions.apply_locked(config.settings, persist=True)
        assert fingerprints == 2 and discarded == 1
        assert config.settings.model_dump() == before
    finally:
        await runtime.close()
