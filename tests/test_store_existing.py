from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli import store_schema
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

    # This checks schema diagnostics; scheduling gaps belong to the separate
    # wall-clock budget tests below, rather than racing this exact error code.
    def schema_clock() -> float:
        return 0.0

    with monkeypatch.context() as schema_time:
        schema_time.setattr(store_schema, "monotonic", schema_clock)
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


def schema_budget_note(error: ExistingStoreError) -> dict[str, Any]:
    assert str(error) == error.code == "existing_store_read_budget"
    assert len(error.__notes__) == 1
    prefix = "existing_store_read_observation="
    note = error.__notes__[0]
    assert note.startswith(prefix) and len(note.encode()) <= 512
    value: dict[str, Any] = json.loads(note[len(prefix) :])
    fields = {"group", "phase", "trigger", "steps", "vm_limit"}
    assert set(value) in (fields, fields | {"timing"})
    if "timing" in value:
        assert set(value["timing"]) == {
            "elapsed_ms",
            "before_schema_ms",
            "schema_ms",
            "thread_cpu_ms",
            "before_schema_cpu_ms",
            "schema_cpu_ms",
        }
        assert all(
            item is None or (type(item) is int and 0 <= item <= 2**31 - 1)
            for item in value["timing"].values()
        )
    return value


async def test_schema_budget_note_records_first_vm_gate_without_an_extra_clock_read(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = 0

    def clock() -> float:
        nonlocal readings
        readings += 1
        return 0.0 if readings == 1 else 2.0

    monkeypatch.setattr(store_schema, "monotonic", clock)
    with closing(sqlite3.connect(database)) as db:
        with pytest.raises(ExistingStoreError) as caught:
            validate_existing_schema(db, limits=ExistingStoreLimits(vm_steps=1))
        note = schema_budget_note(caught.value)
        assert {key: value for key, value in note.items() if key != "timing"} == {
            "group": "all",
            "phase": "setup",
            "trigger": "vm_steps",
            "steps": 1,
            "vm_limit": 1,
        }
        assert readings == 1  # Preserve OR short-circuit; wall-clock state was not sampled.
        assert not db.in_transaction and db.execute("PRAGMA query_only").fetchone()[0] == 0


@pytest.mark.parametrize("expire_before_scan", [True, False])
async def test_schema_budget_note_distinguishes_progress_and_final_wall_gate(
    database: Path, monkeypatch: pytest.MonkeyPatch, expire_before_scan: bool
) -> None:
    now = 0.0
    original = store_schema._validate_definitions

    def clock() -> float:
        return now

    def definitions(
        db: sqlite3.Connection,
        definitions: tuple[store_schema._Definition, ...],
        limits: ExistingStoreLimits,
    ) -> None:
        nonlocal now
        if expire_before_scan:
            now = 2.0
        original(db, definitions, limits)
        now = 2.0

    monkeypatch.setattr(store_schema, "monotonic", clock)
    monkeypatch.setattr(store_schema, "_validate_definitions", definitions)
    with closing(sqlite3.connect(database)) as db:
        previous = db.total_changes
        with pytest.raises(ExistingStoreError) as caught:
            validate_existing_schema(db, "channel")
        note = schema_budget_note(caught.value)
        assert note["group"] == "channel" and note["trigger"] == "wall_clock"
        assert note["phase"] == ("schema" if expire_before_scan else "complete")
        assert 0 <= note["steps"] < note["vm_limit"] == ExistingStoreLimits().vm_steps
        assert db.total_changes == previous and not db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert db.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("delay", ["before_schema", "inside_schema"])
async def test_schema_budget_note_separates_setup_and_schema_time_without_relaxing_wall_gate(
    database: Path, monkeypatch: pytest.MonkeyPatch, delay: str
) -> None:
    """Calibrate equal old observations without claiming the macOS failure's cause."""
    before = snapshot(database)
    connections, mutations = prohibit_writes(monkeypatch)
    wall, cpu = 0.0, 0.0
    clock_readings: list[tuple[str, float]] = []
    outside_readings = 0
    inside_progress = False
    progress_results: list[int] = []
    progress_intervals: list[int] = []
    begin_states: list[bool] = []
    schema_calls: list[None] = []
    statements: list[str] = []
    original = store_schema._validate_definitions

    def clock() -> float:
        nonlocal outside_readings
        if inside_progress:
            stage = "progress"
        else:
            stage = ("deadline", "busy_timeout", "extra")[min(outside_readings, 2)]
            outside_readings += 1
        clock_readings.append((stage, wall))
        return wall

    class BoundaryConnection(sqlite3.Connection):
        def set_progress_handler(
            self, progress_handler: Callable[[], int] | None, n: int = 1000
        ) -> None:
            progress_intervals.append(n)
            if progress_handler is None:
                super().set_progress_handler(None, n)
                return

            def observed() -> int:
                nonlocal inside_progress
                inside_progress = True
                try:
                    result = progress_handler()
                finally:
                    inside_progress = False
                progress_results.append(result)
                return result

            super().set_progress_handler(observed, n)

        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            nonlocal wall, cpu
            result = super().execute(sql, parameters)
            if sql == "BEGIN":
                begin_states.append(self.in_transaction)
                if delay == "before_schema":
                    # The original BEGIN returned; schema admission has not been entered.
                    wall, cpu = 2.0, 0.125
            elif sql == "ROLLBACK":
                # Cleanup is outside the rejecting gate's timing snapshot.
                wall, cpu = 10.0, 1.0
            return result

    def definitions(
        db: sqlite3.Connection,
        definitions: tuple[store_schema._Definition, ...],
        limits: ExistingStoreLimits,
    ) -> None:
        nonlocal wall, cpu
        schema_calls.append(None)
        if delay == "inside_schema":
            # Enter the complete original scan call, then advance only controlled clocks.
            wall, cpu = 2.0, 0.125
        original(db, definitions, limits)

    monkeypatch.setattr(store_schema, "monotonic", clock)
    # Independent diagnostic clocks must not add budget-clock reads, even when unavailable
    # on the old source. The first red must be the missing observation, not an import error.
    monkeypatch.setattr(store_schema, "perf_counter", lambda: wall, raising=False)
    monkeypatch.setattr(store_schema, "thread_time", lambda: cpu, raising=False)
    monkeypatch.setattr(store_schema, "_validate_definitions", definitions)
    with closing(sqlite3.connect(database, factory=BoundaryConnection)) as db:
        db.set_trace_callback(statements.append)
        previous_limits = {
            category: db.getlimit(category)
            for category in (sqlite3.SQLITE_LIMIT_LENGTH, sqlite3.SQLITE_LIMIT_SQL_LENGTH)
        }
        previous_query_only = db.execute("PRAGMA query_only").fetchone()[0]
        previous_busy_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        changes = db.total_changes
        with pytest.raises(ExistingStoreError) as caught:
            validate_existing_schema(db)
        note = schema_budget_note(caught.value)
        assert {key: note[key] for key in ("group", "phase", "trigger", "steps", "vm_limit")} == {
            "group": "all",
            "phase": "schema",
            "trigger": "wall_clock",
            "steps": len(progress_results) * 100,
            "vm_limit": 200_000,
        }
        assert progress_intervals == [100, 0]
        assert progress_results and progress_results[-1] == 1
        assert progress_results[:-1] == [0] * (len(progress_results) - 1)
        assert [(stage, value) for stage, value in clock_readings if stage != "progress"] == [
            ("deadline", 0.0),
            ("busy_timeout", 0.0),
        ]
        assert [value for stage, value in clock_readings if stage == "progress"] == [0.0] * (
            len(progress_results) - 1
        ) + [2.0]
        assert schema_calls == [None] and begin_states == [True]
        assert statements.count("BEGIN") == statements.count("ROLLBACK") == 1
        assert db.total_changes == changes and not db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == previous_query_only
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == previous_busy_timeout
        assert all(db.getlimit(category) == value for category, value in previous_limits.items())
        assert db.execute("SELECT length(?)", ("x" * 100_000,)).fetchone() == (100_000,)
        assert db.execute("SELECT 1").fetchone() == (1,)
        assert all(secret not in caught.value.__notes__[0] for secret in (str(database), "SELECT"))
        clock_readings.clear()
        outside_readings = 0
        progress_results.clear()
        progress_intervals.clear()
        with pytest.raises(ExistingStoreError) as vm_caught:
            validate_existing_schema(db, limits=ExistingStoreLimits(vm_steps=1))
        vm_note = schema_budget_note(vm_caught.value)
        assert vm_note["trigger"] == "vm_steps" and vm_note["steps"] == 1
        assert progress_intervals == [1, 0] and progress_results == [1]
        assert clock_readings == [("deadline", 10.0)]  # VM rejection samples no budget clock.
        assert not db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == previous_query_only
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == previous_busy_timeout
        assert db.execute("SELECT 1").fetchone() == (1,)
    assert mutations == [] and snapshot(database) == before
    assert_closed(connections)
    assert note.get("timing") == {
        "elapsed_ms": 2000,
        "before_schema_ms": 2000 if delay == "before_schema" else 0,
        "schema_ms": 2000 if delay == "inside_schema" else 0,
        "thread_cpu_ms": 125,
        "before_schema_cpu_ms": 125 if delay == "before_schema" else 0,
        "schema_cpu_ms": 125 if delay == "inside_schema" else 0,
    }


@pytest.mark.parametrize("clock_name", ["perf_counter", "thread_time"])
@pytest.mark.parametrize("clock_error", [None, OSError, NotImplementedError, RuntimeError])
async def test_schema_budget_timing_clock_failure_keeps_the_original_rejection(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_name: str,
    clock_error: type[Exception] | None,
) -> None:
    readings = 0

    def clock() -> float:
        nonlocal readings
        readings += 1
        return 0.0

    def unavailable() -> float:
        assert clock_error is not None
        raise clock_error("private_observation_clock_failure")

    monkeypatch.setattr(store_schema, "monotonic", clock)
    monkeypatch.setattr(store_schema, "perf_counter", lambda: 0.0)
    monkeypatch.setattr(store_schema, "thread_time", lambda: 0.0)
    monkeypatch.setattr(store_schema, clock_name, None if clock_error is None else unavailable)
    with closing(sqlite3.connect(database)) as db:
        changes = db.total_changes
        with pytest.raises(ExistingStoreError) as caught:
            validate_existing_schema(db, limits=ExistingStoreLimits(vm_steps=1))
        note = schema_budget_note(caught.value)
        assert note["trigger"] == "vm_steps" and note["steps"] == 1
        assert note["timing"] == {
            "elapsed_ms": None if clock_name == "perf_counter" else 0,
            "before_schema_ms": None if clock_name == "perf_counter" else 0,
            "schema_ms": None,
            "thread_cpu_ms": None if clock_name == "thread_time" else 0,
            "before_schema_cpu_ms": None if clock_name == "thread_time" else 0,
            "schema_cpu_ms": None,
        }
        assert readings == 1
        assert db.total_changes == changes and not db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert db.execute("SELECT 1").fetchone() == (1,)
        assert "private_observation_clock_failure" not in caught.value.__notes__[0]


@pytest.mark.parametrize("ending", [float("nan"), float("inf"), -float("inf"), -1.0, 1e308])
async def test_schema_budget_timing_invalid_samples_are_null_and_large_elapsed_is_bounded(
    database: Path, monkeypatch: pytest.MonkeyPatch, ending: float
) -> None:
    wall, observed = 0.0, 0.0
    original = store_schema._validate_definitions

    def definitions(
        db: sqlite3.Connection,
        definitions: tuple[store_schema._Definition, ...],
        limits: ExistingStoreLimits,
    ) -> None:
        nonlocal wall, observed
        wall, observed = 2.0, ending
        original(db, definitions, limits)

    monkeypatch.setattr(store_schema, "monotonic", lambda: wall)
    monkeypatch.setattr(store_schema, "perf_counter", lambda: observed)
    monkeypatch.setattr(store_schema, "thread_time", lambda: observed)
    monkeypatch.setattr(store_schema, "_validate_definitions", definitions)
    with closing(sqlite3.connect(database)) as db:
        changes = db.total_changes
        with pytest.raises(ExistingStoreError) as caught:
            validate_existing_schema(db)
        note = schema_budget_note(caught.value)
        assert note["phase"] == "schema" and note["trigger"] == "wall_clock"
        expected = 2**31 - 1 if ending == 1e308 else None
        assert note["timing"] == {
            "elapsed_ms": expected,
            "before_schema_ms": 0,
            "schema_ms": expected,
            "thread_cpu_ms": expected,
            "before_schema_cpu_ms": 0,
            "schema_cpu_ms": expected,
        }
        assert db.total_changes == changes and not db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert db.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("failure", ["raise", "oversized"])
async def test_schema_budget_timing_note_failure_preserves_the_original_five_fields(
    database: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = store_schema.json.dumps

    def dumps(value: dict[str, Any], **kwargs: Any) -> str:
        if "timing" in value:
            if failure == "raise":
                raise RuntimeError("private_timing_note_failure")
            return "x" * 513
        return original(value, **kwargs)

    monkeypatch.setattr(store_schema, "monotonic", lambda: 0.0)
    monkeypatch.setattr(store_schema.json, "dumps", dumps)
    with closing(sqlite3.connect(database)) as db:
        with pytest.raises(ExistingStoreError) as caught:
            validate_existing_schema(db, limits=ExistingStoreLimits(vm_steps=1))
        note = schema_budget_note(caught.value)
        assert set(note) == {"group", "phase", "trigger", "steps", "vm_limit"}
        assert note["trigger"] == "vm_steps" and note["steps"] == 1
        assert not db.in_transaction and db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert db.execute("SELECT 1").fetchone() == (1,)
        assert "private_timing_note_failure" not in caught.value.__notes__[0]


async def test_schema_budget_note_does_not_replace_cleanup_failure(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CleanupFailure(sqlite3.Connection):
        def set_progress_handler(
            self, progress_handler: Callable[[], int] | None, n: int = 1000
        ) -> None:
            super().set_progress_handler(progress_handler, n)
            if progress_handler is None:
                raise sqlite3.OperationalError("private cleanup details")

    monkeypatch.setattr(store_schema, "monotonic", lambda: 0.0)
    with closing(sqlite3.connect(database, factory=CleanupFailure)) as db:
        with pytest.raises(ExistingStoreError, match="^existing_store_cleanup_failed$") as caught:
            validate_existing_schema(db, limits=ExistingStoreLimits(vm_steps=1))
        assert not hasattr(caught.value, "__notes__")


async def test_schema_budget_note_failure_preserves_the_original_rejection(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_note(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("private diagnostic details")

    monkeypatch.setattr(store_schema, "monotonic", lambda: 0.0)
    monkeypatch.setattr(store_schema.json, "dumps", fail_note)
    with closing(sqlite3.connect(database)) as db:
        with pytest.raises(ExistingStoreError, match="^existing_store_read_budget$") as caught:
            validate_existing_schema(db, limits=ExistingStoreLimits(vm_steps=1))
        assert not hasattr(caught.value, "__notes__")
        assert not db.in_transaction and db.execute("SELECT 1").fetchone() == (1,)


async def test_unrelated_schema_error_does_not_gain_a_budget_note(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_scan(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("private SQL and database content")

    monkeypatch.setattr(store_schema, "_validate_definitions", fail_scan)
    with closing(sqlite3.connect(database)) as db:
        with pytest.raises(ExistingStoreError, match="^existing_store_unavailable$") as caught:
            validate_existing_schema(db)
        assert not hasattr(caught.value, "__notes__")
        assert not db.in_transaction and db.execute("SELECT 1").fetchone() == (1,)


async def test_current_format_channel_read_budget_precedes_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "current.sqlite3"
    store = Store.open_runtime(path, default_mode="ask")
    before = snapshot(path)
    original = store_schema._validate_definitions
    now = 0.0
    clients: list[None] = []

    def clock() -> float:
        return now

    def definitions(
        db: sqlite3.Connection,
        definitions: tuple[store_schema._Definition, ...],
        limits: ExistingStoreLimits,
    ) -> None:
        nonlocal now
        now = 2.0
        original(db, definitions, limits)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        clients.append(None)
        raise AssertionError("client must not be created after rejected read admission")

    try:
        assert store.format_state is not None
        changes = store.db.total_changes
        monkeypatch.setattr(store_schema, "monotonic", clock)
        monkeypatch.setattr(store_schema, "_validate_definitions", definitions)
        monkeypatch.setattr("muli.channels.manager.httpx.AsyncClient", client)
        with pytest.raises(ExistingStoreError) as caught:
            ChannelManager(store, EventBus(store), ChannelsConfig(), no_submit)
        note = schema_budget_note(caught.value)
        assert note["group"] == "all" and note["phase"] == "schema"
        assert note["trigger"] == "wall_clock" and not clients
        assert store.db.total_changes == changes and snapshot(path) == before
        assert not store.db.in_transaction
        assert store.db.execute("PRAGMA query_only").fetchone()[0] == 0
    finally:
        store.close()


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
