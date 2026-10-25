"""A real rolled-back delegation must not terminate the shared reminder clock."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_reminders import ObservedEvent
from test_runtime import make_runtime
from test_scheduled_task_replan_guards import paused_schedule
from test_task_control_runtime import wait_agent

import muli.reminders as reminders_module
from muli.providers import ModelTurn, ToolCall
from muli.store import now


async def test_rolled_back_delegation_keeps_clock_alive_and_recovers_on_real_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = now()
    due_at = timestamp + 60
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    runtime, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "schedule-clock-recovery",
                        "workflow_schedule",
                        json.dumps(
                            {
                                "name": "真实事务回滚后的委托",
                                "prompt": "整理合成资料并由另一节点评审，不调用外部服务",
                                "due_at": due_at,
                            }
                        ),
                    )
                ]
            ),
            ModelTurn(content="已登记委托"),
        ],
    )
    runtime.config.update(
        {
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "updates": {"check_on_start": False},
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
        }
    )
    await runtime.reconfigure()
    changed = ObservedEvent()
    runtime.reminders.changed = changed
    failed, admitted = asyncio.Event(), asyncio.Event()
    failures: list[sqlite3.IntegrityError] = []
    node_roles: list[str] = []
    successful_admissions = 0
    original_admit = runtime.workflows._admit_start
    trigger_installed = False

    def observe_admission(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal successful_admissions
        try:
            run = original_admit(*args, **kwargs)
        except sqlite3.IntegrityError as error:
            # The real SQLite trigger has already unwound put_many's owning
            # transaction. This observer never manufactures an admission error.
            assert not runtime.store.db.in_transaction
            failures.append(error)
            failed.set()
            raise
        successful_admissions += 1
        admitted.set()
        return run

    async def finish_node(session_id: str, prompt: str, role: str, mode: str) -> str:
        node_roles.append(role)
        return "合成资料已整理" if role == "worker" else "合成资料已独立评审"

    monkeypatch.setattr(runtime.workflows, "_admit_start", observe_admission)
    monkeypatch.setattr(runtime.workflows, "run_step", finish_node)
    try:
        # Use the real owner submit path, including a host-issued Web input ID;
        # start() without that source would correctly fail schedule authorization.
        started = await runtime.submit("一分钟后执行合成委托", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 3) == "已登记委托"
        main_id = runtime.main_session()["id"]
        replies = [
            message
            for message in runtime.store.messages(main_id)
            if message.get("tool_call_id") == "schedule-clock-recovery"
        ]
        assert len(replies) == 1 and replies[0]["content_provenance"] == "accepted"
        receipt = json.loads(replies[0]["content"])
        assert isinstance(receipt, dict) and "error" not in receipt
        schedules = runtime.store.list("workflow_schedule")
        assert len(schedules) == 1 and schedules[0]["id"] == receipt["id"]
        scheduled = schedules[0]
        assert scheduled["reply_origin"]["kind"] == "web"
        assert scheduled["reply_origin"]["input_id"] == started["message"]["id"]
        assert scheduled["next_due_at"] == due_at and scheduled["fired_count"] == 0

        # The workflow_run is the last object in the real admission put_many.
        # Aborting here also rolls back its schedule, occurrence and node writes.
        with runtime.store.db:
            runtime.store.db.execute(
                "CREATE TRIGGER fail_clock_admission AFTER INSERT ON objects "
                "WHEN NEW.kind='workflow_run' "
                "AND json_extract(NEW.data,'$.schedule_id')='"
                + scheduled["id"]
                + "' BEGIN SELECT RAISE(ABORT,'clock_admission_rolled_back'); END"
            )
        trigger_installed = True
        healthy = runtime.reminders.create("独立普通提醒", due_at + 0.02)
        await runtime.reminders.start()
        clock = runtime.reminders.task
        assert clock is not None
        await asyncio.wait_for(changed.waits.get(), 3)
        timestamp = due_at
        changed.set()
        await asyncio.wait_for(failed.wait(), 3)
        assert len(failures) == 1 and "clock_admission_rolled_back" in str(failures[0])
        assert runtime.store.get("workflow_schedule", scheduled["id"]) == scheduled
        assert runtime.store.list("workflow_occurrence") == []
        assert runtime.store.list("workflow_run") == []
        assert runtime.workflows._tasks == {} and node_roles == []
        # Exact product red: all real rollback prerequisites above must pass.
        assert not clock.done(), "真实接纳回滚不应终止唯一提醒时钟"

        # No changed.set here: the healthy reminder's original finite deadline
        # must progress while the failed delegation is excluded from busy retry.
        timestamp = healthy["due_at"]
        async with asyncio.timeout(3), runtime.bus.changed:
            await runtime.bus.changed.wait_for(lambda: bool(runtime.store.list("notification")))
        notices = runtime.store.list("notification")
        assert len(notices) == 1 and notices[0]["reminder_id"] == healthy["id"]
        assert runtime.store.get("reminder", healthy["id"])["fired_count"] == 1  # type: ignore[index]
        assert len(failures) == 1 and successful_admissions == 0
        assert not clock.done()

        with runtime.store.db:
            runtime.store.db.execute("DROP TRIGGER fail_clock_admission")
        trigger_installed = False
        # A real persisted reminder edit supplies the existing change signal.
        runtime.reminders.update(healthy["id"], {"title": "已核实的普通提醒"})
        await asyncio.wait_for(admitted.wait(), 3)
        occurrences = runtime.store.list("workflow_occurrence")
        assert len(occurrences) == 1
        occurrence = occurrences[0]
        assert occurrence["schedule_id"] == scheduled["id"]
        await asyncio.wait_for(runtime.workflows.wait(occurrence["run_id"]), 3)
        assert node_roles == ["worker", "reviewer"]
        assert successful_admissions == 1 and len(failures) == 1
        assert len(runtime.store.list("workflow_run")) == 1
        assert runtime.store.get("workflow_schedule", scheduled["id"])["fired_count"] == 1  # type: ignore[index]
        assert len(runtime.store.list("notification")) == 1
        assert not clock.done()
        await runtime.close()
        assert runtime.reminders.task is None and runtime.workflows._closed
        assert runtime.tasks == {} and runtime.workflows._tasks == {}
    finally:
        if trigger_installed:
            with runtime.store.db:
                runtime.store.db.execute("DROP TRIGGER fail_clock_admission")
        # Red-version teardown only: consume the exact already-settled injected
        # failure so the assertion above is not masked and real Runtime cleanup
        # can continue. The successful path never takes this branch.
        task = runtime.reminders.task
        if task is not None and task.done() and not task.cancelled():
            error = task.exception()
            if any(error is injected for injected in failures):
                runtime.reminders.task = None
        await runtime.close()


async def test_unsettled_transaction_blocks_all_clock_effects_even_after_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject an unfinished transaction, not a real disk/rollback failure."""
    timestamp = now()
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    runtime, _ = make_runtime(tmp_path, [])
    changed = ObservedEvent()
    runtime.reminders.changed = changed
    due_at = timestamp + 60
    first = runtime.reminders.create("未收口事务边界甲", due_at)
    second = runtime.reminders.create("未收口事务边界乙", due_at)
    before = {item["id"]: item for item in (first, second)}
    injected = asyncio.Event()
    fire_ids: list[str] = []
    workflow_ticks: list[float] = []
    original_fire = runtime.reminders._fire
    original_tick = runtime.scheduled_workflows.tick
    database = runtime.store.db.execute("PRAGMA database_list").fetchone()[2]

    async def leave_transaction_open(item: dict[str, Any], fire_at: float) -> None:
        fire_ids.append(item["id"])
        if len(fire_ids) == 1:
            assert not runtime.store.db.in_transaction
            runtime.store.db.execute("BEGIN")
            runtime.store._put("clock_test_sentinel", {"id": "unfinished-prefix"})
            assert runtime.store.db.in_transaction
            injected.set()
            raise sqlite3.OperationalError("test_transaction_not_settled")
        # Keep the actual healthy reminder transaction: it would incorrectly
        # commit the sentinel too if the clock continued on this connection.
        await original_fire(item, fire_at)

    async def observe_workflow_tick(tick_at: float) -> None:
        workflow_ticks.append(tick_at)
        await original_tick(tick_at)

    async def clock_boundary(task: asyncio.Task[None]) -> None:
        if task.done():
            return
        waiter = asyncio.create_task(changed.waits.get())
        try:
            done, _ = await asyncio.wait(
                {task, waiter}, timeout=3, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "未收口事务应进入可关闭的隔离等待或停止时钟"
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def assert_no_effects() -> None:
        assert runtime.store.db.in_transaction, "健康事务不能顺带提交未知前缀"
        assert len(fire_ids) == 1 and workflow_ticks == []
        assert runtime.store.list("notification") == []
        assert runtime.store.events() == []
        assert {item["id"]: item for item in runtime.store.list("reminder")} == before
        # The independent connection distinguishes an uncommitted prefix from
        # a sentinel silently committed by a subsequent healthy transaction.
        reader = sqlite3.connect(database)
        try:
            assert (
                reader.execute(
                    "SELECT COUNT(*) FROM objects WHERE kind='clock_test_sentinel'"
                ).fetchone()[0]
                == 0
            )
        finally:
            reader.close()

    monkeypatch.setattr(runtime.reminders, "_fire", leave_transaction_open)
    monkeypatch.setattr(runtime.scheduled_workflows, "tick", observe_workflow_tick)
    try:
        await runtime.reminders.start()
        clock = runtime.reminders.task
        assert clock is not None
        await asyncio.wait_for(changed.waits.get(), 3)
        workflow_ticks.clear()  # The initial healthy, pre-due catalog pass is allowed.
        timestamp = due_at
        changed.set()
        await asyncio.wait_for(injected.wait(), 3)
        await clock_boundary(clock)
        assert_no_effects()
        # A real existing clock signal cannot certify that an unknown DB
        # transaction rolled back. No data mutation is used to produce it.
        changed.set()
        await clock_boundary(clock)
        assert_no_effects()
    finally:
        # Stop the producer before rolling back the transaction owned by this
        # injection. This teardown is not evidence of real I/O-error recovery.
        try:
            await runtime.reminders.close()
        finally:
            if runtime.store.db.in_transaction:
                runtime.store.db.rollback()
            await runtime.close()


async def test_committed_scheduled_input_wakes_original_main_after_notify_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with paused_schedule(tmp_path) as rig:
        runtime = rig.runtime
        # Settle the original parked-generation report before attributing a
        # subsequent driver launch to this newly committed scheduled input.
        await asyncio.wait_for(asyncio.gather(*tuple(runtime.team._tasks)), 3)
        if existing := runtime.run_ids.get(rig.main_id):
            if rig.main_id in runtime.tasks:
                await asyncio.wait_for(runtime.wait(existing), 3)
        before_runs = {
            run["id"] for run in runtime.store.list("run") if run["session_id"] == rig.main_id
        }
        assert runtime.store.pending_inputs(rig.main_id) == 0
        assert rig.main_id not in runtime.tasks
        changed = ObservedEvent()
        runtime.reminders.changed = changed
        rig.phase = "due"
        monkeypatch.setattr(reminders_module, "now", lambda: rig.due_at)
        notify_failed = asyncio.Event()
        failures: list[OSError] = []
        committed: dict[str, str] = {}
        original_notify = runtime.bus.notify

        async def fail_after_real_committed_notification() -> None:
            await original_notify()
            occurrences = runtime.store.list("task_replan_occurrence")
            if failures or not occurrences:
                return
            assert len(occurrences) == 1
            occurrence = occurrences[0]
            assert occurrence["schedule_id"] == rig.schedule["id"]
            receipt = runtime.store.get("task_control_receipt", occurrence["owner_receipt_id"])
            assert receipt is not None and receipt["action"] == "request_replan"
            assert receipt["scheduled_replan"]["occurrence_id"] == occurrence["id"]
            ingress = runtime.store.db.execute(
                "SELECT id,session_id,state,payload FROM ingress WHERE id=?",
                (occurrence["ingress_id"],),
            ).fetchone()
            assert ingress is not None and ingress["state"] == "pending"
            assert ingress["session_id"] == rig.main_id
            assert json.loads(ingress["payload"])["task_control"]["receipt_id"] == (receipt["id"])
            assert not runtime.store.db.in_transaction
            committed.update(
                occurrence_id=occurrence["id"],
                receipt_id=receipt["id"],
                ingress_id=ingress["id"],
            )
            error = OSError("test_scheduled_notify_after_commit")
            failures.append(error)
            notify_failed.set()
            raise error

        monkeypatch.setattr(runtime.bus, "notify", fail_after_real_committed_notification)
        try:
            # The real shared clock executes the original scheduled tick and
            # commits owner control before the observer injects this one error.
            await runtime.reminders.start()
            await asyncio.wait_for(notify_failed.wait(), 3)
            assert len(failures) == 1 and committed
            main_run = runtime.run_ids.get(rig.main_id)
            # Exact wake target: the old implementation leaves the committed
            # input pending here, without a new main driver; no timeout is red.
            assert main_run is not None and main_run not in before_runs
            assert rig.main_id in runtime.tasks
            await asyncio.wait_for(rig.due_ready.wait(), 3)
            ingress = runtime.store.db.execute(
                "SELECT state,run_id FROM ingress WHERE id=?", (committed["ingress_id"],)
            ).fetchone()
            assert ingress is not None
            assert ingress["state"] == "delivered" and ingress["run_id"] == main_run
            assert runtime.team.get(rig.target_ids[0])["generation"] == 1
            assert runtime.store.list("task_plan_delivery") == []
            before_occurrence = runtime.store.get(
                "task_replan_occurrence", committed["occurrence_id"]
            )
            before_receipt = runtime.store.get("task_control_receipt", committed["receipt_id"])
            # Revisit the real due entry while the actual read-selected main
            # request is in flight. A consumed occurrence cannot re-admit it.
            await runtime._scheduled_tick(rig.due_at)
            assert runtime.store.list("task_replan_occurrence") == [before_occurrence]
            assert runtime.store.get("task_control_receipt", committed["receipt_id"]) == (
                before_receipt
            )
            assert runtime.run_ids[rig.main_id] == main_run
            assert (
                runtime.store.db.execute(
                    "SELECT COUNT(*) FROM ingress WHERE id=?", (committed["ingress_id"],)
                ).fetchone()[0]
                == 1
            )

            rig.release_due.set()
            assert await asyncio.wait_for(runtime.wait(main_run), 3) == "已收到"
            target = await wait_agent(runtime, rig.target_ids[0], "completed")
            assert "error" not in rig.approval_result
            assert target["generation"] == target["completion_revision"] == 2
            state = runtime.task_control_current(rig.task_id)["control"]
            assert state["epoch"] == state["approved_epoch"] == 2
            receipts = [
                item
                for item in runtime.store.list("task_control_receipt")
                if item["commitment_id"] == rig.task_id
            ]
            assert sum(item["action"] == "request_replan" for item in receipts) == 1
            assert sum(item["action"] == "approve_plan" for item in receipts) == 1
            assert len(runtime.store.list("task_replan_occurrence")) == 1
            assert len(runtime.store.list("task_plan_delivery")) == 1
            writes = [
                event
                for event in runtime.store.events()
                if event["type"] == "tool.started"
                and event["session_id"] == rig.target_ids[0]
                and event["data"].get("call_id") == "resume-write"
            ]
            assert len(writes) == 1
            assert rig.forbidden.read_bytes() == "不应执行".encode()
            assert len(failures) == 1
        finally:
            rig.release_due.set()
