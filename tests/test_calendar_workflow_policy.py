"""Calendar namespaces through real Runtime tools, admission and workflow storage.

The clock callback is invoked directly with a controlled module clock. Models
are fixed offline policies; channel delivery and the complete phone journey are
covered separately, not inferred from these storage and authority regressions.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
from test_attention import eventually
from test_runtime import FakeClient
from test_scheduled_delegation_policy import (
    finished_node,
    owner_turn,
    persisted_result,
    schedule_call,
)
from test_scheduled_delegation_policy import (
    scheduled_runtime as scheduled_runtime,
)
from test_teammate_attachment_runtime import call, tool_result

import muli.scheduled_workflows as scheduled_module
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.workflows import ScheduledOccurrence

CALENDAR = "calendar_workflow_schedule"
OCCURRENCE = "calendar_workflow_occurrence"
TOOL = "workflow_schedule_calendar"


@pytest.fixture
def calendar_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [datetime(2030, 1, 7, 8, tzinfo=UTC).timestamp()]
    monkeypatch.setattr(scheduled_module, "now", lambda: clock[0])
    return clock


def calendar_call(identifier: str) -> ToolCall:
    return ToolCall(
        identifier,
        TOOL,
        json.dumps(
            {
                "name": "地区日历委托",
                "prompt": "整理合成资料并独立评审，不访问外部服务。",
                "timezone": "UTC",
                "local_time": "09:00",
                "weekdays": [7, 6, 5, 4, 3, 2, 1],
            },
            ensure_ascii=False,
        ),
    )


def schedule(runtime: Runtime, identifier: str, kind: str = CALENDAR) -> dict[str, Any]:
    item = runtime.store.get(kind, identifier)
    assert item is not None
    return item


def occurrence(runtime: Runtime, identifier: str) -> dict[str, Any]:
    item = schedule(runtime, identifier)
    result = runtime.store.get(OCCURRENCE, item["last_occurrence_id"])
    assert result is not None
    return result


async def settle(runtime: Runtime, identifier: str, timestamp: float) -> dict[str, Any]:
    current = occurrence(runtime, identifier)
    async with asyncio.timeout(3):
        await runtime.workflows.wait(current["run_id"])
    await runtime.scheduled_workflows.tick(timestamp)
    current = occurrence(runtime, identifier)
    assert current["status"] == "completed" and current["publication"] == "recorded"
    return current


async def test_pending_calendar_reopens_and_missed_days_coalesce_once(
    scheduled_runtime: Runtime, calendar_clock: list[float]
) -> None:
    first = scheduled_runtime
    saved = (await owner_turn(first, [calendar_call("calendar")]))["calendar"]
    assert saved["kind"] == CALENDAR and saved["interval_seconds"] is None
    assert saved["calendar"] == {
        "version": 1,
        "timezone": "UTC",
        "local_time": "09:00",
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
    }
    due_at = saved["due_at"]
    assert due_at == calendar_clock[0] + 3600
    assert first.store.list(OCCURRENCE) == []
    config_path, data_dir = first.config.path, first.data_dir
    await first.close()
    calls: list[str] = []

    def nodes(config: ModelConfig) -> FakeClient:
        calls.append(config.model)
        return finished_node(config)

    second = Runtime(ConfigFile(config_path), data_dir, client_factory=nodes)
    try:
        calendar_clock[0] = due_at + 3 * 86400 + 120
        await second.scheduled_workflows.tick(due_at)
        admitted = occurrence(second, saved["id"])
        assert admitted["due_at"] == due_at
        assert schedule(second, saved["id"])["next_due_at"] > calendar_clock[0]
        completed = await settle(second, saved["id"], calendar_clock[0])
        run = second.store.get("workflow_run", completed["run_id"])
        assert run["schedule_kind"] == CALENDAR and run.get("result_message_id")
        assert calls == ["worker", "reviewer"]
        for _ in range(2):
            await second.scheduled_workflows.tick(calendar_clock[0])
        assert schedule(second, saved["id"])["fired_count"] == 1
        assert [item["id"] for item in second.store.list(OCCURRENCE)] == [completed["id"]]
        assert second.store.list("workflow_occurrence") == []
        assert calls == ["worker", "reviewer"]
    finally:
        await second.close()


async def test_calendar_admission_rolls_back_schedule_occurrence_nodes_and_run_together(
    scheduled_runtime: Runtime, calendar_clock: list[float]
) -> None:
    runtime = scheduled_runtime
    saved = (await owner_turn(runtime, [calendar_call("atomic")]))["atomic"]
    before = schedule(runtime, saved["id"])
    counts = runtime.store.db.execute(
        "SELECT kind,count(*) FROM objects WHERE kind IN ('session','workflow_run',?) "
        "GROUP BY kind ORDER BY kind",
        (OCCURRENCE,),
    ).fetchall()
    runtime.store.db.executescript(
        "CREATE TRIGGER calendar_admission_failure AFTER INSERT ON objects "
        "WHEN NEW.kind='workflow_run' "
        "AND json_extract(NEW.data,'$.schedule_kind')='calendar_workflow_schedule' "
        "BEGIN SELECT RAISE(ABORT,'calendar admission rollback'); END;"
    )
    calendar_clock[0] = saved["due_at"]
    try:
        with pytest.raises(sqlite3.IntegrityError, match="calendar admission rollback"):
            await runtime.scheduled_workflows.tick(calendar_clock[0])
        current = schedule(runtime, saved["id"])
        assert (current["next_due_at"], current["fired_count"], current["last_occurrence_id"]) == (
            before["next_due_at"],
            0,
            None,
        )
        actual = runtime.store.db.execute(
            "SELECT kind,count(*) FROM objects WHERE kind IN ('session','workflow_run',?) "
            "GROUP BY kind ORDER BY kind",
            (OCCURRENCE,),
        ).fetchall()
        assert [tuple(row) for row in actual] == [tuple(row) for row in counts]
        assert not runtime.store.db.in_transaction and not runtime.workflows.active_run_ids
        assert runtime.maintenance.admission.allowed
    finally:
        runtime.store.db.execute("DROP TRIGGER calendar_admission_failure")
        runtime.store.db.commit()
    await runtime.scheduled_workflows.tick(calendar_clock[0])
    admitted = occurrence(runtime, saved["id"])
    with pytest.raises(ValueError, match="已经接纳"):
        runtime.workflows._admit_start(
            current["workflow_id"],
            parent_session_id=current["creator_session_id"],
            scheduled=ScheduledOccurrence(current, admitted, "yolo", schedule_kind=CALENDAR),
        )
    await settle(runtime, saved["id"], calendar_clock[0])
    assert schedule(runtime, saved["id"])["fired_count"] == 1


async def test_same_ids_use_sql_kind_for_pagination_proofs_and_exact_cancel(
    scheduled_runtime: Runtime, calendar_clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = scheduled_runtime
    monkeypatch.setattr(scheduled_module, "new_id", lambda: "shared-schedule")
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            entered.set()
            await release.wait()
            yield StreamEvent("done", turn=ModelTurn(content="有限合成证据。"))

    def nodes(config: ModelConfig) -> FakeClient:
        assert config.model == "worker"
        return WaitingWorker([], [])

    saved = await owner_turn(
        runtime,
        [calendar_call("calendar"), schedule_call("interval", calendar_clock[0] + 3600)],
        nodes=nodes,
    )
    identifier = saved["calendar"]["id"]
    assert identifier == saved["interval"]["id"]
    for kind, fake in [(CALENDAR, "workflow_schedule"), ("workflow_schedule", CALENDAR)]:
        item = schedule(runtime, identifier, kind)
        runtime.store.put(kind, {**item, "kind": fake})
    page = runtime.scheduled_workflows.list(limit=1)
    assert [item["kind"] for item in page["items"]] == [CALENDAR]
    assert page["has_more"] and page["next_after_kind"] == CALENDAR
    assert runtime.scheduled_workflows.list(after_id=identifier)["items"] == []
    following = runtime.scheduled_workflows.list(
        after_id=page["next_after"], after_kind=page["next_after_kind"], limit=1
    )
    assert [item["kind"] for item in following["items"]] == ["workflow_schedule"]
    assert not following["has_more"]
    calendar_clock[0] = saved["calendar"]["due_at"]
    try:
        await runtime.scheduled_workflows.tick(calendar_clock[0])
        async with asyncio.timeout(3):
            await entered.wait()
        admitted = occurrence(runtime, identifier)
        run = runtime.store.get("workflow_run", admitted["run_id"])
        session_id = run["nodes"]["work"]["session_id"]
        session = runtime.store.get("session", session_id)
        binding = session["scheduled_workflow"]
        assert run["schedule_kind"] == binding["schedule_kind"] == CALENDAR
        runtime.scheduled_workflows.guard_session(session_id)
        for bad in [None, "workflow_schedule", "unknown"]:
            runtime.store.put(
                "session", {**session, "scheduled_workflow": {**binding, "schedule_kind": bad}}
            )
            with pytest.raises(PermissionError):
                runtime.scheduled_workflows.guard_session(session_id)
        without_kind = {key: value for key, value in binding.items() if key != "schedule_kind"}
        runtime.store.put("session", {**session, "scheduled_workflow": without_kind})
        with pytest.raises(PermissionError):
            runtime.scheduled_workflows.guard_session(session_id)
        runtime.store.put("session", session)
        for bad in [None, "workflow_schedule", "unknown"]:
            runtime.store.put("workflow_run", {**run, "schedule_kind": bad})
            with pytest.raises(PermissionError):
                runtime.scheduled_workflows.guard_session(session_id)
        runtime.store.put(
            "workflow_run", {key: value for key, value in run.items() if key != "schedule_kind"}
        )
        with pytest.raises(PermissionError):
            runtime.scheduled_workflows.guard_session(session_id)
        runtime.store.put("workflow_run", run)
        runtime.scheduled_workflows.guard_session(session_id)
        cancelled = await runtime.scheduled_workflows.cancel(identifier)
        assert cancelled["kind"] == "workflow_schedule" and not cancelled["enabled"]
        assert schedule(runtime, identifier)["active"]
        assert runtime.store.get("workflow_run", run["id"])["status"] == "running"
        exact = await runtime.scheduled_workflows.cancel(identifier, kind=CALENDAR)
        assert exact["kind"] == CALENDAR and not exact["enabled"]
        assert runtime.store.get("workflow_run", run["id"])["status"] == "cancelled"
        await runtime.scheduled_workflows.tick(calendar_clock[0])
        assert occurrence(runtime, identifier)["status"] == "cancelled"
        assert runtime.store.list("workflow_occurrence") == []
        with pytest.raises(ValueError, match="类型无效"):
            await runtime.scheduled_workflows.cancel(identifier, kind="unknown")
        with pytest.raises(ValueError, match="类型无效"):
            runtime.scheduled_workflows.list(after_kind="unknown")
        with pytest.raises(ValueError, match="类型无效"):
            ScheduledOccurrence({}, {}, "yolo", schedule_kind="unknown")
    finally:
        release.set()


@pytest.mark.parametrize("disabled", [TOOL, "workflow_schedule"])
async def test_disabling_calendar_tool_or_old_umbrella_revokes_future_admission(
    scheduled_runtime: Runtime, calendar_clock: list[float], disabled: str
) -> None:
    runtime = scheduled_runtime
    saved = (await owner_turn(runtime, [calendar_call("revoked")]))["revoked"]
    runtime.config.update({"disabled_tools": [disabled]})
    calendar_clock[0] = saved["due_at"]
    await runtime.scheduled_workflows.tick(calendar_clock[0])
    current = schedule(runtime, saved["id"])
    assert current["next_due_at"] == saved["due_at"] and current["fired_count"] == 0
    assert current["blocked"] == "PermissionError"
    assert runtime.store.list(OCCURRENCE) == [] and runtime.store.list("workflow_run") == []


async def test_admission_pause_and_config_lock_refresh_now_before_calendar_admission(
    scheduled_runtime: Runtime, calendar_clock: list[float]
) -> None:
    runtime = scheduled_runtime
    saved = (await owner_turn(runtime, [calendar_call("clock")]))["clock"]
    due_at = saved["due_at"]
    calendar_clock[0] = due_at
    runtime.maintenance.admission.pause()
    await runtime.scheduled_workflows.tick(due_at)
    assert schedule(runtime, saved["id"])["fired_count"] == 0
    runtime.maintenance.admission.release()
    async with runtime.configuration_lock:
        pending = asyncio.create_task(runtime.scheduled_workflows.tick(due_at))
        await asyncio.sleep(0)
        assert not pending.done()
        calendar_clock[0] = due_at - 1
    await pending
    assert runtime.store.list(OCCURRENCE) == []
    assert schedule(runtime, saved["id"])["next_due_at"] == due_at
    async with runtime.configuration_lock:
        pending = asyncio.create_task(runtime.scheduled_workflows.tick(due_at))
        await asyncio.sleep(0)
        assert not pending.done()
        calendar_clock[0] = due_at + 4 * 86400 + 60
    await pending
    assert occurrence(runtime, saved["id"])["due_at"] == due_at
    assert schedule(runtime, saved["id"])["next_due_at"] > calendar_clock[0]
    await settle(runtime, saved["id"], calendar_clock[0])
    assert schedule(runtime, saved["id"])["fired_count"] == 1


async def test_calendar_and_interval_share_one_active_schedule_budget(
    scheduled_runtime: Runtime, calendar_clock: list[float]
) -> None:
    runtime = scheduled_runtime
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    saved = (await owner_turn(runtime, [calendar_call("calendar-budget")]))["calendar-budget"]
    refused = await owner_turn(
        runtime, [schedule_call("interval-budget", calendar_clock[0] + 3600)]
    )
    assert refused["interval-budget"].get("error")
    assert len(runtime.store.list(CALENDAR)) == 1 and runtime.store.list("workflow_schedule") == []
    await runtime.scheduled_workflows.cancel(saved["id"], kind=CALENDAR)
    accepted = await owner_turn(
        runtime, [schedule_call("interval-released", calendar_clock[0] + 3600)]
    )
    assert accepted["interval-released"]["kind"] == "workflow_schedule"
    assert len(runtime.store.list("workflow_schedule")) == 1


async def test_current_ask_mode_still_requires_real_file_approval(
    scheduled_runtime: Runtime, calendar_clock: list[float], tmp_path: Path
) -> None:
    runtime = scheduled_runtime
    saved = (await owner_turn(runtime, [calendar_call("mode")]))["mode"]
    effect = tmp_path / "unapproved-calendar-effect.txt"

    class Worker(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])
            self.rounds = 0

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.rounds += 1
            if self.rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call("effect", "fs_write", path=str(effect), content="not approved")
                    ]
                )
            else:
                assert self.rounds == 2 and tool_result(messages, "effect").get("error")
                turn = ModelTurn(content="文件写入没有获得当前主人批准。")
            yield StreamEvent("done", turn=turn)

    def nodes(config: ModelConfig) -> FakeClient:
        return Worker() if config.model == "worker" else finished_node(config)

    await owner_turn(runtime, [], mode="ask", nodes=nodes)
    calendar_clock[0] = saved["due_at"]
    await runtime.scheduled_workflows.tick(calendar_clock[0])
    await eventually(lambda: bool(runtime.permissions.pending))
    approval_id = next(iter(runtime.permissions.pending))
    approval = runtime.store.get("approval", approval_id)
    admitted = occurrence(runtime, saved["id"])
    run = runtime.store.get("workflow_run", admitted["run_id"])
    assert run["mode"] == admitted["mode"] == "ask"
    assert approval["tool"] == "fs_write" and not effect.exists()
    await runtime.permissions.resolve(approval_id, "reject")
    await settle(runtime, saved["id"], calendar_clock[0])
    assert not effect.exists() and not runtime.permissions.pending


async def test_interrupted_calendar_occurrence_never_repeats_its_written_file_effect(
    scheduled_runtime: Runtime, calendar_clock: list[float], tmp_path: Path
) -> None:
    runtime = scheduled_runtime
    effect = tmp_path / "already-written-calendar.txt"
    written, release = asyncio.Event(), asyncio.Event()
    node_rounds: list[int] = []

    class Worker(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])
            self.rounds = 0

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.rounds += 1
            node_rounds.append(self.rounds)
            assert len(messages) <= 32
            if self.rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "calendar-effect",
                            "fs_write",
                            path=str(effect),
                            content="one real calendar write",
                        )
                    ]
                )
            else:
                assert self.rounds == 2 and not tool_result(messages, "calendar-effect").get(
                    "error"
                )
                assert effect.read_text(encoding="utf-8") == "one real calendar write"
                written.set()
                await release.wait()
                turn = ModelTurn(content="此节点应由正常关闭收口，不作为完成成果发布。")
            yield StreamEvent("done", turn=turn)

    def nodes(config: ModelConfig) -> FakeClient:
        assert config.model == "worker"
        return Worker()

    saved = (await owner_turn(runtime, [calendar_call("written-calendar")], nodes=nodes))[
        "written-calendar"
    ]
    snapshot_dir = tmp_path / "calendar-running-snapshot"
    snapshot_dir.mkdir()
    config_path = runtime.config.path
    calendar_clock[0] = saved["due_at"]
    try:
        await runtime.scheduled_workflows.tick(calendar_clock[0])
        async with asyncio.timeout(3):
            await written.wait()
        current = occurrence(runtime, saved["id"])
        running_id, occurrence_id = current["run_id"], current["id"]
        run = runtime.store.get("workflow_run", running_id)
        assert run["schedule_kind"] == CALENDAR
        assert run["status"] == current["status"] == "running"
        assert run["nodes"]["work"]["status"] == "running"
        assert not persisted_result(
            runtime, run["nodes"]["work"]["session_id"], "calendar-effect"
        ).get("error")
        file_state = effect.stat()
        # Copy the actual live Store, then close the original Runtime normally;
        # this exercises recovery from an in-flight snapshot, not a process crash.
        with sqlite3.connect(snapshot_dir / "muli.sqlite3") as destination:
            runtime.store.db.backup(destination)
    finally:
        async with asyncio.timeout(3):
            await runtime.close()
        release.set()

    attempted_models: list[str] = []

    def forbidden(config: ModelConfig) -> NoReturn:
        attempted_models.append(config.model)
        raise AssertionError(
            "interrupted calendar occurrence must not retry a model or file effect"
        )

    recovered = Runtime(ConfigFile(config_path), snapshot_dir, client_factory=forbidden)
    try:
        assert recovered.store.get("workflow_run", running_id)["status"] == "interrupted"
        assert occurrence(recovered, saved["id"])["status"] == "running"
        for _ in range(3):
            await recovered.scheduled_workflows.tick(calendar_clock[0])
        settled = occurrence(recovered, saved["id"])
        assert settled["id"] == occurrence_id and settled["status"] == "interrupted"
        assert not recovered.store.get("workflow_run", running_id).get("result_message_id")
        assert (
            len(recovered.store.list(OCCURRENCE)) == len(recovered.store.list("workflow_run")) == 1
        )
        assert recovered.store.list("workflow_occurrence") == []
        restored = schedule(recovered, saved["id"])
        assert restored["calendar"] == saved["calendar"]
        assert restored["fired_count"] == 1 and restored["next_due_at"] > calendar_clock[0]
        assert attempted_models == [] and node_rounds == [1, 2]
        assert not recovered.tasks and not recovered.workflows.active_run_ids
        assert effect.read_text(encoding="utf-8") == "one real calendar write"
        assert (effect.stat().st_ino, effect.stat().st_mtime_ns) == (
            file_state.st_ino,
            file_state.st_mtime_ns,
        )
    finally:
        await recovered.close()
