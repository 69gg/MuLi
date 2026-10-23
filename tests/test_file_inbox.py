from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_attention import eventually
from test_runtime import make_runtime

from muli.file_inbox import (
    EVENT_TYPE,
    POLICY_KIND,
    RECORD_KIND,
    SETTINGS_ID,
    SETTINGS_KIND,
    FileInbox,
    FileInboxError,
    FileInboxSettings,
    FileInboxSettingsStore,
    read_record,
)
from muli.platforms.file_watch import DirectoryIdentity, FileMetadata, FileSignal
from muli.runtime import Runtime


class FakeWatch:
    instances: list[FakeWatch] = []

    def __init__(
        self,
        path: Path,
        on_events: Callable[[list[FileSignal]], None],
        on_failure: Callable[[str], None],
        **kwargs: object,
    ) -> None:
        self.path, self.on_events, self.on_failure = path, on_events, on_failure
        self.closed = False
        self.instances.append(self)

    def start(self) -> DirectoryIdentity:
        info = self.path.stat()
        return DirectoryIdentity(info.st_dev, info.st_ino)

    def close(self) -> None:
        self.closed = True

    def check_identity(self) -> None:
        pass

    def metadata(self, name: str) -> FileMetadata | None:
        path = self.path / name
        if not path.is_file() or path.is_symlink():
            return None
        info = path.stat()
        return FileMetadata(
            name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        )


def configured(tmp_path: Path, **settings: Any) -> tuple[Runtime, FileInbox, Path]:
    runtime, _ = make_runtime(tmp_path, [])
    directory = tmp_path / "inbox"
    directory.mkdir()
    runtime.config.update({"workspace_roots": [str(tmp_path)]})
    FileInboxSettingsStore(runtime.store).replace(
        FileInboxSettings(
            enabled=True,
            path=str(directory),
            coalesce_seconds=0,
            **settings,
        )
    )
    return runtime, FileInbox(runtime, watch_factory=FakeWatch), directory


def records(runtime: Runtime) -> list[dict[str, Any]]:
    return runtime.store.list(RECORD_KIND)


async def test_candidate_arrival_old_edit_and_removed(tmp_path: Path) -> None:
    runtime, inbox, directory = configured(tmp_path)
    try:
        await inbox.start()
        (directory / "old").write_text("old")
        await inbox.ingest([FileSignal("close_write", "old")])
        await asyncio.sleep(0)
        assert records(runtime) == []
        (directory / "new").write_text("new")
        await inbox.ingest([FileSignal("create", "new")])
        await asyncio.sleep(0)
        assert records(runtime) == []
        await inbox.ingest([FileSignal("close_write", "new")])
        await eventually(lambda: len(records(runtime)) == 1)
        first = records(runtime)[0]
        assert first["metadata"]["name"] == "new"
        await inbox.ingest([FileSignal("close_write", "new"), FileSignal("moved_to", "new")])
        await asyncio.sleep(0.01)
        assert len(records(runtime)) == 1
        await inbox.ingest(
            [
                FileSignal("create", "removed"),
                FileSignal("removed", "removed"),
                FileSignal("close_write", "removed"),
            ]
        )
        await asyncio.sleep(0)
        assert len(records(runtime)) == 1
        assert inbox.locate(first["id"])["path"] == str(directory / "new")  # type: ignore[index]
    finally:
        await inbox.close()
        await runtime.close()


async def test_budget_gap_persisted_restart_and_rearm(tmp_path: Path) -> None:
    runtime, inbox, directory = configured(tmp_path, max_notices_per_window=1)
    try:
        await inbox.start()
        (directory / "one").write_text("one")
        await inbox.ingest([FileSignal("moved_to", "one")])
        await eventually(lambda: len(records(runtime)) == 2)
        policy = runtime.store.get(POLICY_KIND, "current")
        assert policy is not None
        assert policy["window_count"] == 1
        assert inbox.status()["code"] == "coverage_incomplete"
        assert not inbox.status()["running"]
        await inbox.reconfigure()
        assert runtime.store.get(POLICY_KIND, "current") == policy
        await inbox.fail("coverage_incomplete")
        await asyncio.sleep(0)
        assert len(records(runtime)) == 2
        inbox.settings_store.replace(FileInboxSettings())
        await inbox.reconfigure()
        inbox.settings_store.replace(
            FileInboxSettings(
                enabled=True, path=str(directory), coalesce_seconds=0, max_notices_per_window=1
            )
        )
        await inbox.reconfigure()
        assert inbox.settings_state().revision != policy["revision"]
        assert inbox.status()["window_count"] == 0
    finally:
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize("overflow", [True, False])
async def test_candidate_overflow_or_expiry_single_gap(tmp_path: Path, overflow: bool) -> None:
    runtime, inbox, _ = configured(tmp_path, max_candidates=1, candidate_timeout_seconds=0.1)
    try:
        await inbox.start()
        await inbox.ingest([FileSignal("create", "one")])
        if overflow:
            await inbox.ingest([FileSignal("create", "two")])
        await eventually(lambda: inbox.status()["gap_id"] is not None)
        assert [item["kind"] for item in records(runtime)] == ["gap"]
        for _ in range(10):
            await inbox.ingest([FileSignal("moved_to", "other")])
        await asyncio.sleep(0)
        assert len(records(runtime)) == 1
    finally:
        await inbox.close()
        await runtime.close()


async def test_canonical_atomic_rollback_forgery_and_stale_callback(tmp_path: Path) -> None:
    runtime, inbox, directory = configured(tmp_path)
    try:
        await inbox.start()
        old_generation = inbox.generation
        (directory / "one").write_text("external contents must never enter SQLite")
        runtime.store.db.execute(
            "CREATE TRIGGER reject_file BEFORE INSERT ON objects WHEN NEW.kind='file_inbox_record' "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
        await inbox.ingest([FileSignal("moved_to", "one")])
        await eventually(lambda: inbox.status()["last_error"] == "persistence_failed")
        assert records(runtime) == []
        assert not any(event["type"] == EVENT_TYPE for event in runtime.store.events())
        runtime.store.db.execute("DROP TRIGGER reject_file")
        await inbox.reconfigure()
        await inbox.ingest([FileSignal("moved_to", "one")], old_generation)
        await asyncio.sleep(0)
        assert records(runtime) == []
        await inbox.ingest([FileSignal("moved_to", "one")])
        await eventually(lambda: len(records(runtime)) == 1)
        event = next(item for item in runtime.store.events() if item["type"] == EVENT_TYPE)
        assert read_record(runtime.store, event, inbox.settings_state()) is not None
        assert (
            read_record(runtime.store, {**event, "id": event["id"] + 1}, inbox.settings_state())
            is None
        )
        assert "external contents" not in str(records(runtime))
        revision = inbox.settings_state().revision
        await inbox.reconfigure()
        assert inbox.settings_state().revision == revision
        assert read_record(runtime.store, event, inbox.settings_state()) is not None
        inbox.settings_store.replace(FileInboxSettings())
        assert read_record(runtime.store, event, inbox.settings_state()) is None
    finally:
        await inbox.close()
        await runtime.close()


async def test_invalid_filename_and_bounded_retention(tmp_path: Path) -> None:
    runtime, inbox, directory = configured(tmp_path, max_records=1)
    try:
        await inbox.start()
        for name in ("one", "two"):
            (directory / name).write_text(name)
            await inbox.ingest([FileSignal("moved_to", name)])
            await eventually(
                lambda name=name: (
                    bool(records(runtime)) and records(runtime)[0]["metadata"]["name"] == name
                )
            )
        assert len(records(runtime)) == 1
        assert len([event for event in runtime.store.events() if event["type"] == EVENT_TYPE]) == 1
        await inbox.ingest([FileSignal("moved_to", "bad\udcff")])
        await eventually(lambda: inbox.status()["gap_id"] is not None)
        assert len(records(runtime)) == 1
        assert records(runtime)[0]["kind"] == "gap"
    finally:
        await inbox.close()
        await runtime.close()


async def test_pending_admission_survives_record_eviction_then_revocation(tmp_path: Path) -> None:
    from muli.attention import AttentionInbox

    runtime, inbox, directory = configured(tmp_path, max_records=1)
    runtime.config.update({"attention": {"coalesce_seconds": 0}})
    attention = AttentionInbox(runtime, file_inbox_settings=inbox.settings_state)
    # Keep the real admission transaction but leave inputs unclaimed for revocation.
    runtime._wake_main = lambda: None  # type: ignore[method-assign]
    try:
        await inbox.start()
        await attention.start()
        (directory / "one").write_text("one")
        await inbox.ingest([FileSignal("moved_to", "one")])
        await eventually(lambda: len(records(runtime)) == 1)
        await eventually(lambda: runtime.store.pending_inputs(runtime.main_session()["id"]) == 1)
        pending = runtime.store.db.execute(
            "SELECT id FROM ingress WHERE state='pending'"
        ).fetchall()
        assert len(pending) == 1
        identifier = pending[0][0]
        assert runtime.store.get("file_inbox_admission", identifier) is not None
        owner, _ = runtime.conversation.enqueue(
            runtime.main_session()["id"],
            "真实主人输入",
            source="web",
            principal="owner",
            mode="ask",
            max_pending=32,
        )
        (directory / "two").write_text("two")
        await inbox.ingest([FileSignal("moved_to", "two")])
        await eventually(
            lambda: bool(records(runtime)) and records(runtime)[0]["metadata"]["name"] == "two"
        )
        assert runtime.store.get("file_inbox_admission", identifier) is not None
        inbox.settings_store.replace(FileInboxSettings())
        await inbox.reconfigure()
        states = {
            row["id"]: row["state"]
            for row in runtime.store.db.execute("SELECT id,state FROM ingress")
        }
        assert states[identifier] == "cancelled"
        assert states[owner["id"]] == "pending"
    finally:
        await attention.close()
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize(
    "value", ["[]", '{"id":"future","future_version":true}', '"' + "x" * 17000 + '"']
)
async def test_corrupt_record_is_excluded_without_exception(tmp_path: Path, value: str) -> None:
    runtime, inbox, directory = configured(tmp_path)
    try:
        await inbox.start()
        (directory / "one").write_text("one")
        await inbox.ingest([FileSignal("moved_to", "one")])
        await eventually(lambda: len(records(runtime)) == 1)
        event = next(item for item in runtime.store.events() if item["type"] == EVENT_TYPE)
        with runtime.store.db:
            runtime.store.db.execute("UPDATE objects SET data=? WHERE kind=?", (value, RECORD_KIND))
        assert read_record(runtime.store, event, inbox.settings_state()) is None
        assert inbox.list_records() == []
        assert inbox.locate(event["data"]["id"]) is None
    finally:
        await inbox.close()
        await runtime.close()


async def test_retention_uses_exact_event_identity_and_keeps_other_events(tmp_path: Path) -> None:
    runtime, inbox, directory = configured(tmp_path, max_records=1)
    statements: list[str] = []
    try:
        await inbox.start()
        for index in range(40):
            runtime.store.event("unrelated.history", {"id": str(index)})
        (directory / "one").write_text("one")
        await inbox.ingest([FileSignal("moved_to", "one")])
        await eventually(lambda: len(records(runtime)) == 1)
        first = records(runtime)[0]
        plan = runtime.store.db.execute(
            "EXPLAIN QUERY PLAN DELETE FROM events WHERE id=? AND type=? "
            "AND CASE WHEN json_valid(data) THEN json_extract(data,'$.id') END=?",
            (first["event_id"], EVENT_TYPE, first["id"]),
        ).fetchall()
        assert any("SEARCH events USING INTEGER PRIMARY KEY" in str(row[3]) for row in plan)
        runtime.store.db.set_trace_callback(statements.append)
        (directory / "two").write_text("two")
        await inbox.ingest([FileSignal("moved_to", "two")])
        await eventually(lambda: records(runtime)[0]["metadata"]["name"] == "two")
        deletes = [
            statement for statement in statements if statement.startswith("DELETE FROM events")
        ]
        assert len(deletes) == 1
        assert f"id={first['event_id']}" in deletes[0]
        assert (
            runtime.store.db.execute(
                "SELECT count(*) FROM events WHERE type='unrelated.history'"
            ).fetchone()[0]
            == 40
        )
    finally:
        runtime.store.db.set_trace_callback(None)
        await inbox.close()
        await runtime.close()


async def test_native_closed_failure_preserves_original_code(tmp_path: Path) -> None:
    from muli.platforms.file_watch import WatchError

    class ClosingWatch(FakeWatch):
        def check_identity(self) -> None:
            if self.closed:
                raise WatchError("watch_closed")

    runtime, _, directory = configured(tmp_path)
    inbox = FileInbox(runtime, watch_factory=ClosingWatch)
    try:
        await inbox.start()
        watch = ClosingWatch.instances[-1]
        watch.close()
        watch.on_failure("identity_changed")
        # A status read before the driver runs must preserve the original failure.
        assert inbox.settings_state().code == "identity_changed"
        await eventually(lambda: inbox.status()["gap_id"] is not None)
        assert inbox.status()["code"] == "identity_changed"
        assert records(runtime)[0]["code"] == "identity_changed"
        (directory / "late").write_text("late")
        watch.on_events([FileSignal("moved_to", "late")])
        await asyncio.sleep(0)
        assert len(records(runtime)) == 1
        assert inbox.status()["candidates"] == 0
    finally:
        await inbox.close()
        await runtime.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native adapter only")
@pytest.mark.parametrize("code", ["coverage_incomplete", "identity_changed"])
async def test_real_adapter_close_before_failure_keeps_cause_and_correct_pending_policy(
    tmp_path: Path,
    code: str,
) -> None:
    from muli.attention import AttentionInbox
    from muli.platforms.file_watch import NativeFileWatch

    runtime, _, directory = configured(tmp_path)
    runtime.config.update({"attention": {"coalesce_seconds": 0}})
    runtime._wake_main = lambda: None  # type: ignore[method-assign]
    inbox = FileInbox(runtime)
    attention = AttentionInbox(runtime, file_inbox_settings=inbox.settings_state)
    try:
        await inbox.start()
        await attention.start()
        (directory / "one").write_text("one")
        await eventually(lambda: runtime.store.pending_inputs(runtime.main_session()["id"]) == 1)
        pending = runtime.store.db.execute(
            "SELECT id FROM ingress WHERE state='pending'"
        ).fetchone()[0]
        watch = inbox._watch
        assert isinstance(watch, NativeFileWatch)
        # Exercise the real adapter's synchronous close -> failure callback order.
        watch._fail(code)
        assert inbox.settings_state().code == code
        await eventually(lambda: inbox.status()["gap_id"] is not None)
        assert inbox.status()["code"] == code
        state = runtime.store.db.execute(
            "SELECT state FROM ingress WHERE id=?", (pending,)
        ).fetchone()[0]
        assert state == ("pending" if code == "coverage_incomplete" else "cancelled")
        assert any(item["kind"] == "candidate" for item in records(runtime))
        assert [item["code"] for item in records(runtime) if item["kind"] == "gap"] == [code]
    finally:
        await attention.close()
        await inbox.close()
        await runtime.close()


async def test_independent_settings_never_change_main_configuration_bytes(tmp_path: Path) -> None:
    from muli.config import Settings

    runtime, _ = make_runtime(tmp_path, [])
    directory = tmp_path / "inbox"
    directory.mkdir()
    inbox = FileInbox(runtime, watch_factory=FakeWatch)
    before = runtime.config.path.read_bytes()
    try:
        async with runtime.configuration_lock:
            result = await inbox.configure(FileInboxSettings(enabled=True, path=str(directory)))
        assert result["desired"]["enabled"]
        assert result["effective"]["enabled"]
        assert runtime.config.path.read_bytes() == before
        assert "file_inbox" not in Settings.model_fields
        assert "file_inbox" not in Settings.model_validate_json(before).model_dump()
        assert "file_inbox" not in runtime.config.public()
        async with runtime.configuration_lock:
            await inbox.configure(FileInboxSettings())
        assert runtime.config.path.read_bytes() == before
        assert not inbox.status()["running"]
    finally:
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize(
    "data",
    [
        "[]",
        '{"id":"current","schema_version":2,"settings":{}}',
        '{"id":"current","schema_version":true,"settings":{}}',
        '{"id":"current","schema_version":1,"settings":{"unknown":1}}',
        '{"id":"current","schema_version":1,"settings":{"enabled":"yes"}}',
        '"' + "x" * 17000 + '"',
    ],
)
async def test_bad_or_future_settings_are_disabled_and_never_overwritten(
    tmp_path: Path,
    data: str,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    inbox = FileInbox(runtime, watch_factory=FakeWatch)
    try:
        with runtime.store.db:
            runtime.store.db.execute(
                "INSERT INTO objects VALUES(?,?,?,0)", (SETTINGS_KIND, SETTINGS_ID, data)
            )
        position = runtime.store.event_position()
        with pytest.raises(FileInboxError):
            inbox.settings_store.replace(FileInboxSettings())
        assert runtime.store.event_position() == position
        assert (
            runtime.store.db.execute(
                "SELECT data FROM objects WHERE kind=? AND id=?", (SETTINGS_KIND, SETTINGS_ID)
            ).fetchone()[0]
            == data
        )
        await inbox.start()
        status = inbox.status()
        assert not status["running"]
        assert status["desired"] is None
        assert status["settings_error"] in {
            "file_inbox_settings_invalid",
            "file_inbox_settings_budget",
        }
        assert not inbox.settings_state().enabled
        async with runtime.configuration_lock:
            with pytest.raises(FileInboxError):
                await inbox.configure(FileInboxSettings())
        assert runtime.store.event_position() == position
    finally:
        await inbox.close()
        await runtime.close()


def test_settings_outer_transaction_is_not_committed_or_overwritten(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    settings = FileInboxSettingsStore(runtime.store)
    try:
        settings.replace(FileInboxSettings())
        before = settings.read()
        runtime.store.db.execute("BEGIN IMMEDIATE")
        runtime.store._put("uncommitted_fixture", {"id": "one"})
        with pytest.raises(FileInboxError, match="file_inbox_settings_outer_transaction"):
            settings.replace(FileInboxSettings(max_candidates=2))
        assert runtime.store.db.in_transaction
        assert settings.read() == before
        runtime.store.db.rollback()
        assert runtime.store.get("uncommitted_fixture", "one") is None
        assert settings.read() == before
    finally:
        runtime.discard_unstarted()


async def test_configure_permission_and_root_reconfiguration_revocation(tmp_path: Path) -> None:
    from muli.maintenance import MaintenanceError

    runtime, inbox, directory = configured(tmp_path)
    handler = next(
        tool.handler for tool in inbox.tools(runtime) if tool.name == "file_inbox_configure"
    )
    before = runtime.config.path.read_bytes()
    try:
        await inbox.start()
        (directory / "one").write_text("one")
        await inbox.ingest([FileSignal("moved_to", "one")])
        await eventually(lambda: len(records(runtime)) == 1)
        event = next(item for item in runtime.store.events() if item["type"] == EVENT_TYPE)
        with pytest.raises(PermissionError):
            await handler({})
        token = runtime._tool_session.set("not-the-main-session")
        try:
            with pytest.raises(PermissionError):
                await handler({})
        finally:
            runtime._tool_session.reset(token)
        with pytest.raises(MaintenanceError, match="configuration_lock_required"):
            await inbox.configure(FileInboxSettings())
        async with runtime.configuration_lock:
            with pytest.raises(ValueError, match="file_inbox_outside_workspace"):
                await inbox.configure(FileInboxSettings(enabled=True, path=str(tmp_path.parent)))
        assert inbox.desired_settings().enabled
        assert runtime.config.path.read_bytes() == before
        other = tmp_path / "other"
        other.mkdir()
        runtime.config.update({"workspace_roots": [str(other)]})
        assert read_record(runtime.store, event, inbox.settings_state()) is None
        await inbox.reconfigure()
        await eventually(lambda: inbox.status()["gap_id"] is not None)
        assert not inbox.status()["running"]
        assert inbox.desired_settings().enabled
        token = runtime._tool_session.set(runtime.main_session()["id"])
        try:
            result = await handler({})
            assert not result["desired"]["enabled"]
        finally:
            runtime._tool_session.reset(token)
    finally:
        await inbox.close()
        await runtime.close()


async def test_configure_closing_and_atomic_settings_write(tmp_path: Path) -> None:
    from muli.maintenance import MaintenanceError

    runtime, _ = make_runtime(tmp_path, [])
    inbox = FileInbox(runtime, watch_factory=FakeWatch, closing=lambda: True)
    try:
        async with runtime.configuration_lock:
            with pytest.raises(MaintenanceError, match="transaction_unavailable"):
                await inbox.configure(FileInboxSettings())
        assert runtime.store.get(SETTINGS_KIND, SETTINGS_ID) is None
        runtime.store.db.execute(
            "CREATE TRIGGER reject_settings_event BEFORE INSERT ON events "
            "WHEN NEW.type='file_inbox.settings.changed' BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
        with pytest.raises(FileInboxError, match="file_inbox_settings_write_failed"):
            inbox.settings_store.replace(FileInboxSettings())
        assert runtime.store.get(SETTINGS_KIND, SETTINGS_ID) is None
    finally:
        await inbox.close()
        await runtime.close()


async def test_cancelled_configuration_settles_source_before_unlock(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    directory = tmp_path / "inbox"
    directory.mkdir()
    inbox = FileInbox(runtime, watch_factory=FakeWatch)
    closing, release = asyncio.Event(), asyncio.Event()
    original_close = inbox.close

    async def blocked_close() -> None:
        closing.set()
        await release.wait()
        await original_close()

    inbox.close = blocked_close  # type: ignore[method-assign]

    async def apply() -> None:
        async with runtime.configuration_lock:
            await inbox.configure(FileInboxSettings(enabled=True, path=str(directory)))

    task = asyncio.create_task(apply())
    try:
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert runtime.configuration_lock.locked()
        assert inbox.desired_settings().enabled
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime.configuration_lock.locked()
        assert inbox.status()["running"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await original_close()
        await runtime.close()
