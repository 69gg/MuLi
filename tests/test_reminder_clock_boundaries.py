"""Real reminder commits and wakeups bound clock failure recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_attention import attention_inputs
from test_reminders import ObservedEvent
from test_runtime import make_runtime

import muli.reminders as reminders_module
from muli.attention import AttentionInbox
from muli.events import EventBus
from muli.reminders import Reminders
from muli.store import Store, now


async def idle(changed: ObservedEvent) -> None:
    await asyncio.wait_for(changed.waits.get(), 3)


async def notice_for(store: Store, bus: EventBus, identifier: str) -> None:
    async with asyncio.timeout(3), bus.changed:
        await bus.changed.wait_for(
            lambda: any(
                notice["reminder_id"] == identifier for notice in store.list("notification")
            )
        )


async def close_clock(engine: Reminders, store: Store, injected: list[Exception]) -> None:
    # On the old red implementation only, retrieve the exact settled injected
    # failure so close() cannot replace the behavioral assertion with that error.
    task = engine.task
    if task is not None and task.done() and not task.cancelled():
        error = task.exception()
        if any(error is selected for selected in injected):
            engine.task = None
    try:
        await engine.close()
    finally:
        store.close()


async def test_failed_reminder_id_waits_for_change_while_healthy_timeout_progresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = now()

    def clock_now() -> float:
        return timestamp

    monkeypatch.setattr(reminders_module, "now", clock_now)
    store = Store(tmp_path / "clock.sqlite")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    changed = ObservedEvent()
    engine.changed = changed
    broken = engine.create("仅此提醒事务失败", timestamp + 60)
    healthy = engine.create("健康提醒保留原到期唤醒", broken["due_at"] + 0.02)
    failed = asyncio.Event()
    injected: list[Exception] = []
    attempts: list[str] = []
    original_fire = engine._fire

    async def observe_fire(item: dict[str, Any], fire_at: float) -> None:
        attempts.append(item["id"])
        try:
            await original_fire(item, fire_at)
        except sqlite3.IntegrityError as error:
            assert not store.db.in_transaction
            injected.append(error)
            failed.set()
            raise

    monkeypatch.setattr(engine, "_fire", observe_fire)
    with store.db:
        store.db.execute(
            "CREATE TRIGGER reject_one_notice AFTER INSERT ON objects "
            "WHEN NEW.kind='notification' "
            "AND json_extract(NEW.data,'$.reminder_id')='"
            + broken["id"]
            + "' BEGIN SELECT RAISE(ABORT,'one_notice_rolled_back'); END"
        )
    trigger_installed = True
    try:
        await engine.start()
        task = engine.task
        assert task is not None
        await idle(changed)
        timestamp = broken["due_at"]
        changed.set()
        await asyncio.wait_for(failed.wait(), 3)
        assert len(injected) == 1 and "one_notice_rolled_back" in str(injected[0])
        assert store.get("reminder", broken["id"]) == broken
        assert store.list("notification") == [] and store.events() == []
        assert not task.done(), "单个提醒真实事务失败不能杀死唯一时钟"
        await idle(changed)

        # No changed.set: only the healthy item's existing finite timeout wakes
        # this loop. Timeout must not retry the failed ID or create a busy loop.
        timestamp = healthy["due_at"]
        await notice_for(store, bus, healthy["id"])
        await idle(changed)
        assert attempts == [broken["id"], healthy["id"]]
        assert store.get("reminder", broken["id"]) == broken
        healthy_after = store.get("reminder", healthy["id"])
        assert healthy_after is not None and healthy_after["fired_count"] == 1
        assert not healthy_after["enabled"] and not task.done()

        with store.db:
            store.db.execute("DROP TRIGGER reject_one_notice")
        trigger_installed = False
        # A real edit to the naturally disabled healthy item is valid even
        # though its due_at is now past, and supplies the original change signal.
        engine.update(healthy["id"], {"title": "真实编辑允许一次恢复"})
        await notice_for(store, bus, broken["id"])
        await idle(changed)
        assert attempts == [broken["id"], healthy["id"], broken["id"]]
        recovered = store.get("reminder", broken["id"])
        assert recovered is not None and recovered["fired_count"] == 1
        notices = store.list("notification")
        assert len(notices) == 2
        assert {notice["reminder_id"] for notice in notices} == {
            broken["id"],
            healthy["id"],
        }
        created = [event for event in store.events() if event["type"] == "notification.created"]
        assert len(created) == 2
        engine.update(healthy["id"], {"title": "重复真实编辑不补发"})
        await idle(changed)
        assert attempts == [broken["id"], healthy["id"], broken["id"]]
        assert store.list("notification") == notices and not task.done()
    finally:
        if trigger_installed:
            with store.db:
                store.db.execute("DROP TRIGGER reject_one_notice")
        await close_clock(engine, store, injected)


async def test_committed_notice_survives_notify_failure_without_repeating_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = now()

    def clock_now() -> float:
        return timestamp

    monkeypatch.setattr(reminders_module, "now", clock_now)
    store = Store(tmp_path / "clock.sqlite")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    changed = ObservedEvent()
    engine.changed = changed
    item = engine.create("已经提交的提醒", timestamp + 60)
    failed = asyncio.Event()
    injected: list[Exception] = []
    fire_attempts = 0
    original_fire, original_notify = engine._fire, bus.notify

    async def observe_fire(selected: dict[str, Any], fire_at: float) -> None:
        nonlocal fire_attempts
        fire_attempts += 1
        await original_fire(selected, fire_at)

    async def fail_after_real_notification() -> None:
        await original_notify()
        if not injected:
            error = OSError("test_notify_after_commit")
            injected.append(error)
            failed.set()
            raise error

    monkeypatch.setattr(engine, "_fire", observe_fire)
    monkeypatch.setattr(bus, "notify", fail_after_real_notification)
    try:
        await engine.start()
        task = engine.task
        assert task is not None
        await idle(changed)
        timestamp = item["due_at"]
        changed.set()
        await asyncio.wait_for(failed.wait(), 3)
        assert len(injected) == 1 and bus.revision == 1
        assert not store.db.in_transaction
        notices = store.list("notification")
        assert len(notices) == 1 and notices[0]["reminder_id"] == item["id"]
        committed = store.get("reminder", item["id"])
        assert committed is not None
        assert committed["fired_count"] == 1 and not committed["enabled"]
        events = store.events()
        assert [event["type"] for event in events] == [
            "notification.created",
            "reminder.updated",
        ]
        assert not task.done(), "提交后的通知异常不能终止时钟或重新执行提醒"
        await idle(changed)

        for title in ("第一次真实编辑", "第二次真实编辑"):
            engine.update(item["id"], {"title": title})
            await idle(changed)
            current = store.get("reminder", item["id"])
            assert current is not None
            assert current["fired_count"] == 1 and not current["enabled"]
            assert current["last_fired_at"] == committed["last_fired_at"]
            assert store.list("notification") == notices
            assert store.events() == events
            assert fire_attempts == 1 and not task.done()
    finally:
        await close_clock(engine, store, injected)


async def test_clock_diagnostic_advances_real_attention_without_input_or_self_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = now()

    def clock_now() -> float:
        return timestamp

    monkeypatch.setattr(reminders_module, "now", clock_now)
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "attention": {"events": [], "coalesce_seconds": 0},
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
    changed = ObservedEvent()
    runtime.reminders.changed = changed
    inbox = AttentionInbox(runtime, system_settings=lambda: runtime.config.settings.system_events)
    ready, failed, consumed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    injected: list[Exception] = []
    attempts = 0
    original_wake = runtime.wake_pending_inputs
    original_put = runtime.store._put
    original_fire = runtime.reminders._fire

    async def observe_initial_wake() -> None:
        await original_wake()
        ready.set()

    def observe_cursor(kind: str, item: dict[str, Any]) -> None:
        original_put(kind, item)
        if kind == "attention_state":
            logs = runtime.store.list("system_log")
            if logs and item["after"] >= max(log["event_id"] for log in logs):
                consumed.set()

    async def observe_fire(item: dict[str, Any], fire_at: float) -> None:
        nonlocal attempts
        attempts += 1
        try:
            await original_fire(item, fire_at)
        except sqlite3.IntegrityError as error:
            assert not runtime.store.db.in_transaction
            injected.append(error)
            failed.set()
            raise

    monkeypatch.setattr(runtime, "wake_pending_inputs", observe_initial_wake)
    monkeypatch.setattr(runtime.store, "_put", observe_cursor)
    monkeypatch.setattr(runtime.reminders, "_fire", observe_fire)
    item = runtime.reminders.create("只产生时钟诊断", timestamp + 60)
    with runtime.store.db:
        runtime.store.db.execute(
            "CREATE TRIGGER reject_diagnostic_notice AFTER INSERT ON objects "
            "WHEN NEW.kind='notification' "
            "AND json_extract(NEW.data,'$.reminder_id')='"
            + item["id"]
            + "' BEGIN SELECT RAISE(ABORT,'diagnostic_notice_rolled_back'); END"
        )
    try:
        # Use Runtime's actual bound log callback and SystemEvents worker. No
        # synthetic log, cursor, Attention receipt or principal is injected.
        runtime.system_logger = await runtime.system_events.start(runtime._system_logger)
        await inbox.start()
        await asyncio.wait_for(ready.wait(), 3)
        await runtime.reminders.start()
        task = runtime.reminders.task
        assert task is not None
        await idle(changed)
        timestamp = item["due_at"]
        changed.set()
        await asyncio.wait_for(failed.wait(), 3)
        assert len(injected) == 1 and not task.done()
        await asyncio.wait_for(consumed.wait(), 3)
        await idle(changed)

        logs = runtime.store.list("system_log")
        assert len(logs) == 1
        log = logs[0]
        assert log["component"] == "runtime" and log["code"] == "clock_failed"
        assert log["notify"] is False and log["count"] == 1
        assert inbox.cursor >= log["event_id"] and inbox.last_error is None
        assert runtime.system_events.status()["persisted"] == 1
        assert runtime.system_events.last_error is None
        assert attention_inputs(runtime) == [] and runtime.store.list("attention_receipt") == []
        assert runtime.store.list("notification") == []
        assert runtime.store.get("reminder", item["id"]) == item
        assert runtime.store.list("run") == [] and seen == []
        assert attempts == 1 and not task.done()
    finally:
        await inbox.close()
        with runtime.store.db:
            runtime.store.db.execute("DROP TRIGGER reject_diagnostic_notice")
        task = runtime.reminders.task
        if task is not None and task.done() and not task.cancelled():
            error = task.exception()
            if any(error is selected for selected in injected):
                runtime.reminders.task = None
        await runtime.close()


async def test_real_change_during_timeout_cancellation_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = now()

    def clock_now() -> float:
        return timestamp

    monkeypatch.setattr(reminders_module, "now", clock_now)
    store = Store(tmp_path / "clock.sqlite")
    bus = EventBus(store)
    engine = Reminders(store, bus)
    broken = engine.create("等待真实编辑恢复", timestamp + 60)
    healthy = engine.create("提供原有限超时", broken["due_at"] + 0.02)
    control = engine.create("超时边界仍合法的未来控制项", broken["due_at"] + 120)
    armed = False
    edited = asyncio.Event()
    resumed_wait = asyncio.Event()
    cancelled_waits: list[asyncio.CancelledError] = []

    class ChangeDuringTimeout(ObservedEvent):
        async def wait(self) -> bool:
            if edited.is_set():
                resumed_wait.set()
            try:
                return await super().wait()
            except asyncio.CancelledError as error:
                if armed and not edited.is_set():
                    # This is the real wait_for timeout cancelling its original
                    # Event waiter. A persisted edit races before TimeoutError
                    # is delivered back to the clock, not a synthetic wakeup.
                    cancelled_waits.append(error)
                    updated = engine.update(control["id"], {"title": "边界中的真实编辑"})
                    assert updated["due_at"] > clock_now() and updated["enabled"]
                    assert self.is_set()
                    edited.set()
                raise

    changed = ChangeDuringTimeout()
    engine.changed = changed
    failed = asyncio.Event()
    injected: list[Exception] = []
    attempts: list[str] = []
    original_fire = engine._fire

    async def observe_fire(item: dict[str, Any], fire_at: float) -> None:
        attempts.append(item["id"])
        try:
            await original_fire(item, fire_at)
        except sqlite3.IntegrityError as error:
            assert not store.db.in_transaction
            injected.append(error)
            failed.set()
            raise

    monkeypatch.setattr(engine, "_fire", observe_fire)
    with store.db:
        store.db.execute(
            "CREATE TRIGGER reject_racing_notice AFTER INSERT ON objects "
            "WHEN NEW.kind='notification' "
            "AND json_extract(NEW.data,'$.reminder_id')='"
            + broken["id"]
            + "' BEGIN SELECT RAISE(ABORT,'racing_notice_rolled_back'); END"
        )
    trigger_installed = True
    try:
        await engine.start()
        task = engine.task
        assert task is not None
        await idle(changed)
        timestamp = broken["due_at"]
        changed.set()
        await asyncio.wait_for(failed.wait(), 3)
        assert len(injected) == 1 and attempts == [broken["id"]]
        assert store.get("reminder", broken["id"]) == broken
        assert store.list("notification") == [] and not task.done()
        # First reach the actual healthy-item timeout wait, then make recovery
        # possible. The clock still holds the failed ID until a real change.
        await idle(changed)
        with store.db:
            store.db.execute("DROP TRIGGER reject_racing_notice")
        trigger_installed = False
        armed = True
        await asyncio.wait_for(edited.wait(), 3)
        await asyncio.wait_for(resumed_wait.wait(), 3)

        updated_control = store.get("reminder", control["id"])
        assert updated_control is not None
        assert updated_control["title"] == "边界中的真实编辑"
        assert updated_control["due_at"] > timestamp and updated_control["enabled"]
        assert len(cancelled_waits) == 1 and not task.done()
        assert store.get("reminder", healthy["id"]) == healthy
        # Exact red boundary: the next real idle wait proves this is a complete
        # clock iteration after the persisted update, not a scheduler timing guess.
        assert attempts == [broken["id"], broken["id"]], (
            "超时取消窗口中的真实 changed 不能被下一轮 clear 吞掉"
        )
        recovered = store.get("reminder", broken["id"])
        assert recovered is not None and recovered["fired_count"] == 1
        notices = store.list("notification")
        assert len(notices) == 1 and notices[0]["reminder_id"] == broken["id"]
    finally:
        armed = False
        if trigger_installed:
            with store.db:
                store.db.execute("DROP TRIGGER reject_racing_notice")
        await close_clock(engine, store, injected)


async def test_queued_clock_diagnostic_cannot_commit_a_later_unsettled_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real queued logging must not commit a deliberately unfinished test prefix."""
    timestamp = now()

    def clock_now() -> float:
        return timestamp

    monkeypatch.setattr(reminders_module, "now", clock_now)
    runtime, seen = make_runtime(tmp_path, [])
    changed = ObservedEvent()
    runtime.reminders.changed = changed
    due_at = timestamp + 60
    runtime.reminders.create("排队诊断与事务边界甲", due_at)
    runtime.reminders.create("排队诊断与事务边界乙", due_at)
    # Follow the actual catalog order rather than assuming creation-order SQL.
    ordered = runtime.store.list("reminder")
    assert len(ordered) == 2
    first, second = ordered
    before = {item["id"]: item for item in ordered}
    injected = asyncio.Event()
    failures: list[Exception] = []
    attempts: list[str] = []
    rolled_back = False
    original_fire = runtime.reminders._fire
    database = runtime.store.db.execute("PRAGMA database_list").fetchone()[2]

    async def observe_fire(item: dict[str, Any], fire_at: float) -> None:
        nonlocal rolled_back
        attempts.append(item["id"])
        if item["id"] == first["id"]:
            try:
                await original_fire(item, fire_at)
            except sqlite3.IntegrityError as error:
                assert not runtime.store.db.in_transaction
                assert runtime.store.get("reminder", first["id"]) == first
                assert runtime.store.list("notification") == []
                failures.append(error)
                rolled_back = True
                raise
            raise AssertionError("第一提醒必须真实触发 SQLite 回滚")
        assert item["id"] == second["id"] and rolled_back
        status = runtime.system_events.status()
        assert status["captured"] == status["queued"] == 1
        assert status["persisted"] == 0
        assert not runtime.store.db.in_transaction
        runtime.store.db.execute("BEGIN")
        runtime.store._put("clock_test_sentinel", {"id": "queued-diagnostic-prefix"})
        assert runtime.store.db.in_transaction
        error = sqlite3.OperationalError("test_later_transaction_not_settled")
        failures.append(error)
        injected.set()
        raise error

    async def consumer_boundary(consumer: asyncio.Task[None], wake: ObservedEvent) -> None:
        # Observe an actual subsequent log-consumer wait or its settled failure.
        # This does not require a particular safe drop/park implementation.
        waiter = asyncio.create_task(wake.waits.get())
        try:
            done, _ = await asyncio.wait(
                {consumer, waiter}, timeout=3, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "日志消费者应完成本次有界消费或安全停止"
            if consumer in done:
                consumer.result()
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    monkeypatch.setattr(runtime.reminders, "_fire", observe_fire)
    with runtime.store.db:
        runtime.store.db.execute(
            "CREATE TRIGGER reject_queued_diagnostic_notice AFTER INSERT ON objects "
            "WHEN NEW.kind='notification' "
            "AND json_extract(NEW.data,'$.reminder_id')='"
            + first["id"]
            + "' BEGIN SELECT RAISE(ABORT,'queued_diagnostic_notice_rolled_back'); END"
        )
    try:
        runtime.system_logger = await runtime.system_events.start(runtime._system_logger)
        # start() has created but not yet scheduled the real consumer. Preserve
        # its Event behavior and observe only its existing waiting boundaries.
        wake = ObservedEvent()
        monkeypatch.setattr(runtime.system_events, "_wake", wake)
        consumer = runtime.system_events._task
        assert consumer is not None
        await idle(wake)
        await runtime.reminders.start()
        clock = runtime.reminders.task
        assert clock is not None
        await idle(changed)
        timestamp = due_at
        changed.set()
        await asyncio.wait_for(injected.wait(), 3)
        assert rolled_back and len(failures) == 2
        assert isinstance(failures[0], sqlite3.IntegrityError)
        assert "queued_diagnostic_notice_rolled_back" in str(failures[0])
        assert attempts == [first["id"], second["id"]]
        await consumer_boundary(consumer, wake)

        # A second connection distinguishes uncommitted test data from a prefix
        # silently committed by an asynchronous system-log transaction.
        reader = sqlite3.connect(database)
        try:
            visible = reader.execute(
                "SELECT count(*) FROM objects WHERE kind='clock_test_sentinel' AND id=?",
                ("queued-diagnostic-prefix",),
            ).fetchone()[0]
        finally:
            reader.close()
        assert visible == 0, "已排队诊断不能替未知事务提交前缀"
        assert runtime.store.db.in_transaction
        assert runtime.store.list("notification") == []
        assert {item["id"]: item for item in runtime.store.list("reminder")} == before
        assert runtime.store.list("run") == [] and seen == []
        assert runtime.system_events.status()["captured"] == 1
        assert not clock.done()
    finally:
        # This test owns only the deliberately injected transaction. Stop both
        # producers before rolling it back; this is not an I/O-recovery claim.
        try:
            await runtime.reminders.close()
        finally:
            await runtime.system_events.close()
            if runtime.store.db.in_transaction:
                runtime.store.db.rollback()
            with runtime.store.db:
                runtime.store.db.execute("DROP TRIGGER reject_queued_diagnostic_notice")
            await runtime.close()
