from __future__ import annotations

import json
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelManager, ChannelProfile, ChannelsConfig
from muli.config import ConfigFile
from muli.events import EventBus
from muli.format_evidence import FormatEvidenceError, read_format_evidence
from muli.memory import Memory
from muli.runtime import Runtime
from muli.store import Store
from muli.store_schema import (
    ExistingStoreError,
    ExistingStoreLimits,
    SchemaGroup,
    validate_existing_schema,
)


async def test_format_observation_survives_only_its_fixed_error_mapping(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(":memory:") as db:
        with pytest.raises(FormatEvidenceError) as caught:
            read_format_evidence(db)
    source = caught.value
    original_note = source.__notes__[0]
    source.add_note("private extra note")

    def fail(db: sqlite3.Connection, **kwargs: Any) -> None:
        raise source

    monkeypatch.setattr("muli.store.read_optional_format_evidence", fail)
    with pytest.raises(ExistingStoreError) as mapped:
        Store.open_existing(database)
    assert str(mapped.value) == source.code
    assert mapped.value.__notes__ == [original_note]


async def test_arbitrary_format_notes_are_not_forwarded_by_store(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FormatEvidenceError("format_read_timeout")
    source.add_note("format_read_observation=private forged note")

    def fail(db: sqlite3.Connection, **kwargs: Any) -> None:
        raise source

    monkeypatch.setattr("muli.store.read_optional_format_evidence", fail)
    with pytest.raises(ExistingStoreError) as mapped:
        Store.open_existing(database)
    assert str(mapped.value) == "existing_store_read_budget"
    assert not hasattr(mapped.value, "__notes__")


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


async def no_submit(**kwargs: Any) -> dict[str, Any]:
    raise AssertionError("No inbound submission is expected")


def no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError("No platform request is expected")


@pytest.fixture
async def database(tmp_path: Path) -> Path:
    path = tmp_path / "existing # directory" / "muli.sqlite3"
    store = Store(path)
    try:
        Memory(store)
        store.main_session()
        async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
            manager = ChannelManager(
                store, EventBus(store), ChannelsConfig(), no_submit, client=client
            )
            await manager.close()
    finally:
        store.close()
    return path


def snapshot(path: Path) -> tuple[str, ...]:
    """The fixture is tiny: compare logical rows/schema, including committed WAL."""
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return tuple(db.iterdump())
    finally:
        db.close()


def prohibit_writes(
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

    monkeypatch.setattr("muli.store.sqlite3.connect", guarded)
    return connections, mutations


def assert_closed(connections: list[sqlite3.Connection]) -> None:
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


async def test_existing_constructors_never_write_and_business_remains_writable(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(database)
    permissions = stat.S_IMODE(database.stat().st_mode)
    connections, mutations = prohibit_writes(monkeypatch)
    store = Store.open_existing(database, durable=True)
    try:
        assert store.migration_allowed is False
        memory = Memory(store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
            config = ChannelsConfig(
                telegram=ChannelProfile(
                    enabled=True,
                    base_url="https://platform.invalid",
                    token="1:fake",
                    owner_ids=["7"],
                )
            )
            manager = ChannelManager(store, EventBus(store), config, no_submit, client=client)
            await manager.close()
            # Reconfiguration constructs another manager with the same Store.
            manager = ChannelManager(store, EventBus(store), config, no_submit, client=client)
            try:
                main = store.main_session("changed-mode-is-not-written")
                assert main["kind"] == "main" and main["principal"] == "owner"
                assert mutations == [] and store.db.total_changes == 0
                assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
                assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 3000
                assert store.db.execute("PRAGMA query_only").fetchone()[0] == 0
                assert not store.db.in_transaction
                assert snapshot(database) == before
                store.db.set_authorizer(None)
                store.put("fixture", {"id": "post-admission", "value": True})
                saved = memory.save("正常业务仍可新增记忆")
                delivery = await manager.enqueue("telegram", "7", "正常出站", event_id="one")
                assert store.get("fixture", "post-admission") is not None
                assert saved["id"] and delivery["state"] == "pending"
            finally:
                await manager.close()
    finally:
        store.close()
    assert stat.S_IMODE(database.stat().st_mode) == permissions
    assert_closed(connections)


async def test_existing_open_has_no_directory_or_permission_operations(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Existing open must not create directories or chmod")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "chmod", forbidden)
    store = Store.open_existing(database)
    store.close()


@pytest.mark.parametrize("nested", [False, True])
def test_missing_database_is_never_created(tmp_path: Path, nested: bool) -> None:
    path = (tmp_path / "missing-parent" if nested else tmp_path) / "missing.db"
    with pytest.raises(ExistingStoreError, match="existing_store_unavailable"):
        Store.open_existing(path)
    assert not path.exists()
    if nested:
        assert not path.parent.exists()


async def test_non_wal_database_is_rejected_without_switching_mode(database: Path) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    before = snapshot(database)
    with pytest.raises(ExistingStoreError, match="existing_store_journal_unsupported"):
        Store.open_existing(database)
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert snapshot(database) == before


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE inbox",
        "DROP TABLE memory_community_nodes",
        "DROP TABLE channel_outbox",
        "DROP INDEX objects_updated",
        "DROP INDEX memory_meta_claim",
        "DROP INDEX channel_outbox_ready",
        "DELETE FROM runtime_state WHERE name='store_instance_v1'",
        "DELETE FROM runtime_state WHERE name='main_session'",
        "DELETE FROM runtime_state WHERE name='message_sequence_v1'",
        "DELETE FROM memory_state WHERE name='alias_evidence_version'",
        "DELETE FROM memory_state WHERE name='revision'",
        "UPDATE memory_state SET value='invalid' WHERE name='alias_evidence_version'",
        "UPDATE runtime_state SET value='bad' WHERE name='message_sequence_v1'",
        "ALTER TABLE channel_inbound DROP COLUMN fingerprint",
    ],
)
async def test_all_three_groups_rejected_before_any_constructor_write(
    database: Path, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(sql)
    before = snapshot(database)
    connections, mutations = prohibit_writes(monkeypatch)
    with pytest.raises(ExistingStoreError):
        Store.open_existing(database)
    assert mutations == []
    assert snapshot(database) == before
    assert_closed(connections)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX ingress_system_log_run; "
        "CREATE INDEX ingress_system_log_run ON ingress(run_id) WHERE run_id IS NULL;",
        "DROP INDEX agents_parent; "
        "CREATE INDEX agents_parent ON objects(json_extract(data,'$.status')) WHERE kind='agent';",
        "DROP INDEX team_completion_revision; "
        "CREATE INDEX team_completion_revision ON events(session_id,json_extract(data,'$.completion_revision')) "
        "WHERE type='team.finished';",
        "DROP TABLE inbox; CREATE TABLE inbox (id TEXT,envelope TEXT NOT NULL,received REAL NOT NULL);",
    ],
)
async def test_same_names_do_not_hide_changed_constraints_or_index_definitions(
    database: Path, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript(sql)
    before = snapshot(database)
    connections, mutations = prohibit_writes(monkeypatch)
    with pytest.raises(ExistingStoreError, match="existing_store_schema_unsupported"):
        Store.open_existing(database)
    assert mutations == [] and snapshot(database) == before
    assert_closed(connections)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "other"},
        {"kind": "conversation"},
        {"principal": "external:telegram:7"},
        {"principal": None},
    ],
)
async def test_existing_main_requires_actual_owner_object(
    database: Path, change: dict[str, Any]
) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        identifier = db.execute(
            "SELECT value FROM runtime_state WHERE name='main_session'"
        ).fetchone()[0]
        session = json.loads(
            db.execute(
                "SELECT data FROM objects WHERE kind='session' AND id=?", (identifier,)
            ).fetchone()[0]
        )
        db.execute(
            "UPDATE objects SET data=? WHERE kind='session' AND id=?",
            (json.dumps({**session, **change}), identifier),
        )
    before = snapshot(database)
    with pytest.raises(ExistingStoreError, match="existing_store_identity_invalid"):
        Store.open_existing(database)
    assert snapshot(database) == before


@pytest.mark.parametrize("value", ["", "x" * 97, b"binary"])
async def test_invalid_store_identity_is_not_replaced(database: Path, value: str | bytes) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE runtime_state SET value=? WHERE name='store_instance_v1'", (value,))
    before = snapshot(database)
    with pytest.raises(ExistingStoreError):
        Store.open_existing(database)
    assert snapshot(database) == before


async def test_oversized_invalid_memory_marker_never_returns_its_body(database: Path) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE memory_state SET value=zeroblob(32768) WHERE name='revision'")
    before = snapshot(database)
    with pytest.raises(ExistingStoreError) as error:
        Store.open_existing(database)
    assert str(error.value) == error.value.code
    assert snapshot(database) == before


async def test_latest_committed_wal_marker_is_checked(database: Path) -> None:
    writer = sqlite3.connect(database)
    try:
        writer.execute("UPDATE memory_state SET value=2 WHERE name='alias_evidence_version'")
        writer.commit()
        assert Path(str(database) + "-wal").exists()
        with pytest.raises(ExistingStoreError, match="existing_store_marker_invalid"):
            Store.open_existing(database)
        assert (
            writer.execute(
                "SELECT value FROM memory_state WHERE name='alias_evidence_version'"
            ).fetchone()[0]
            == 2
        )
    finally:
        writer.close()


async def test_missing_derived_rows_remain_missing_and_unknown_remains_unknown(
    database: Path,
) -> None:
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO memories VALUES('legacy','无派生资料','fact','import','[]',1,1,1)")
        db.execute("INSERT INTO relations VALUES('legacy','甲','依赖','乙')")
        db.execute(
            "INSERT INTO objects VALUES('message','unknown',?,1)",
            (json.dumps({"id": "unknown", "outcome": "unknown"}),),
        )
    before = snapshot(database)
    store = Store.open_existing(database)
    try:
        Memory(store)
        assert store.get("message", "unknown") == {"id": "unknown", "outcome": "unknown"}
        for table in ("memory_meta", "memory_terms", "memory_edges", "message_order"):
            assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert snapshot(database) == before
    finally:
        store.close()


async def test_subsequent_memory_and_manager_validation_precedes_any_client_creation(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store.open_existing(database)
    try:
        with store.db:
            store.db.execute("DELETE FROM memory_state WHERE name='alias_evidence_version'")
        with pytest.raises(ExistingStoreError):
            Memory(store)
        assert (
            store.db.execute(
                "SELECT 1 FROM memory_state WHERE name='alias_evidence_version'"
            ).fetchone()
            is None
        )
        with store.db:
            store.db.execute("ALTER TABLE channel_inbound DROP COLUMN fingerprint")

        def client_would_leak(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            raise AssertionError("Schema validation must precede client creation")

        monkeypatch.setattr("muli.channels.manager.httpx.AsyncClient", client_would_leak)
        with pytest.raises(ExistingStoreError):
            ChannelManager(store, EventBus(store), ChannelsConfig(), no_submit)
    finally:
        store.close()


async def test_real_channel_application_reconfigure_inherits_store_policy(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ddl: list[int] = []

    def authorizer(action: int, *args: Any) -> int:
        if action in _MUTATIONS - {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        }:
            ddl.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def existing(
        cls: type[Store], path: Path, *, default_mode: str, durable: bool = False
    ) -> Store:
        assert path == database
        store = Store.open_existing(path, durable=durable)
        store.db.set_authorizer(authorizer)
        return store

    def no_model(config: Any) -> Any:
        raise AssertionError("No model may be constructed")

    # Test-only injection: this slice does not expose a Runtime setting or mode.
    monkeypatch.setattr(Store, "open_runtime", classmethod(existing))
    config = ConfigFile(tmp_path / "config.json")
    runtime = Runtime(config, database.parent, no_model)
    app: ChannelApplication | None = None
    try:
        attachments = Attachments(runtime.store, tmp_path / "uploads", config)
        app = ChannelApplication(runtime, attachments)
        first = app.manager
        await app.reconfigure()
        assert app.manager is not first
        assert app.manager.store is runtime.store and not runtime.store.migration_allowed
        assert ddl == []
    finally:
        if app is not None:
            await app.close()
        await runtime.close()


async def test_main_marker_removed_after_open_is_never_recreated(database: Path) -> None:
    store = Store.open_existing(database)
    try:
        with store.db:
            store.db.execute("DELETE FROM runtime_state WHERE name='main_session'")
        changes = store.db.total_changes
        with pytest.raises(ExistingStoreError):
            store.main_session()
        assert store.db.total_changes == changes and store.existing_main_session_id() is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "limits",
    [
        ExistingStoreLimits(source_bytes=1),
        ExistingStoreLimits(schema_objects=1),
        ExistingStoreLimits(sql_bytes=1),
        ExistingStoreLimits(main_object_bytes=1),
        ExistingStoreLimits(vm_steps=1),
    ],
)
async def test_read_budgets_reject_without_partial_migration(
    database: Path, monkeypatch: pytest.MonkeyPatch, limits: ExistingStoreLimits
) -> None:
    before = snapshot(database)
    connections, mutations = prohibit_writes(monkeypatch)
    with pytest.raises(ExistingStoreError):
        Store.open_existing(database, limits=limits)
    assert mutations == [] and snapshot(database) == before
    assert_closed(connections)


async def test_elapsed_read_budget_rejects_without_partial_migration(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(database)
    connections, mutations = prohibit_writes(monkeypatch)
    readings = 0

    def clock() -> float:
        nonlocal readings
        readings += 1
        return 0.0 if readings == 1 else 1.0

    monkeypatch.setattr("muli.store_schema.monotonic", clock)
    with pytest.raises(ExistingStoreError, match="existing_store_read_budget"):
        Store.open_existing(database, limits=ExistingStoreLimits(seconds=0.5))
    assert readings >= 2
    assert mutations == [] and snapshot(database) == before
    assert_closed(connections)


@pytest.mark.parametrize("group", ["all", "core", "memory", "channel"])
async def test_schema_validation_avoids_rescanning_unrelated_objects(
    database: Path, group: SchemaGroup
) -> None:
    """A small valid store with extra schema fits a bounded instruction budget."""
    with closing(sqlite3.connect(database)) as db:
        for index in range(100):
            db.execute(f"CREATE TABLE extra_fixture_{index} (value TEXT)")
        db.execute("CREATE VIEW extra_large_view AS SELECT '" + "x" * 20_000 + "' AS value")
        before = db.total_changes
        validate_existing_schema(db, group, limits=ExistingStoreLimits(vm_steps=10_000))
        assert db.total_changes == before
        assert not db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0


async def test_validation_restores_limits_and_leaves_caller_transaction_untouched(
    database: Path,
) -> None:
    store = Store.open_existing(database)
    try:
        db = store.db
        previous = {
            category: db.getlimit(category)
            for category in (sqlite3.SQLITE_LIMIT_LENGTH, sqlite3.SQLITE_LIMIT_SQL_LENGTH)
        }
        with pytest.raises(ExistingStoreError):
            validate_existing_schema(db, limits=ExistingStoreLimits(vm_steps=1))
        assert not db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 3000
        assert all(db.getlimit(category) == value for category, value in previous.items())
        assert db.execute("SELECT length(?)", ("x" * 100_000,)).fetchone()[0] == 100_000
        db.execute("INSERT INTO objects VALUES('fixture','uncommitted','{}',1)")
        with pytest.raises(ExistingStoreError, match="existing_store_outer_transaction"):
            validate_existing_schema(db)
        assert db.in_transaction and store.get("fixture", "uncommitted") == {}
        db.rollback()
    finally:
        store.close()


async def test_read_uncommitted_setting_is_rejected_not_rewritten(database: Path) -> None:
    store = Store.open_existing(database)
    try:
        store.db.execute("PRAGMA read_uncommitted=1")
        with pytest.raises(ExistingStoreError, match="existing_store_isolation_unsupported"):
            validate_existing_schema(store.db)
        assert store.db.execute("PRAGMA read_uncommitted").fetchone()[0] == 1
        assert store.db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert not store.db.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [("autocommit", True), ("isolation_level", None), ("text_factory", bytes)],
)
async def test_changed_connection_mode_is_rejected_not_rewritten(
    database: Path, name: str, value: Any
) -> None:
    store = Store.open_existing(database)
    try:
        setattr(store.db, name, value)
        with pytest.raises(ExistingStoreError, match="existing_store_connection_unsupported"):
            validate_existing_schema(store.db)
        assert getattr(store.db, name) == value
        assert not store.db.in_transaction
    finally:
        store.close()


async def test_failed_read_transaction_cleanup_discards_owned_connection(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def guarded(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = connect(*args, **kwargs)

        def authorizer(action: int, first: str | None, *args: Any) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and first == "ROLLBACK":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorizer)
        connections.append(db)
        return db

    monkeypatch.setattr("muli.store.sqlite3.connect", guarded)
    with pytest.raises(ExistingStoreError, match="existing_store_cleanup_failed"):
        Store.open_existing(database)
    assert_closed(connections)
