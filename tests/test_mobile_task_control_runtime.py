from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_mobile_task_control import event, short
from test_runtime import make_runtime
from test_task_briefs import attach_services

from muli.mobile_task_control import MobileControlSource
from muli.runtime import Runtime
from muli.store import now
from muli.task_control import ControlConflict


async def test_mobile_status_final_guard_holds_lock_and_never_wakes_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    runtime.store.prepare_channels()
    await services.commitments.save("手机核对", brief={"goal": "核查", "constraints": []})
    inbound_id = "a" * 64
    with runtime.store.db:
        runtime.store.db.execute(
            "INSERT INTO channel_inbound(id,name,created_at,fingerprint) VALUES(?,?,?,?)",
            (inbound_id, "telegram", now(), "f" * 64),
        )
    guards: list[bool] = []
    wakes: list[bool] = []

    def guard() -> None:
        guards.append(runtime.configuration_lock.locked())

    monkeypatch.setattr(runtime, "_wake_main", lambda: wakes.append(True))
    try:
        receipt = await runtime.submit_mobile_task_control(
            inbound_id=inbound_id,
            source=MobileControlSource("telegram", "100", "7", "7"),
            text="/任务",
            admission_guard=guard,
        )
        assert guards == [True] and not wakes and not seen and not runtime.tasks
        assert "手机核对" in receipt["reply_text"]
        assert receipt["mobile_task_control"]["version"] == 1
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0
        assert runtime.store.db.execute(
            "SELECT receipt IS NOT NULL FROM channel_inbound WHERE id=?", (inbound_id,)
        ).fetchone()[0]
    finally:
        await runtime.close()


def stage(runtime: Runtime, inbound_id: str) -> None:
    with runtime.store.db:
        runtime.store.db.execute(
            "INSERT OR IGNORE INTO channel_inbound(id,name,created_at,fingerprint) VALUES(?,?,?,?)",
            (inbound_id, "telegram", now(), "f" * 64),
        )


async def test_mobile_waiting_for_configuration_rechecks_guard_before_receipt_or_control(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    runtime.store.prepare_channels()
    item = await services.commitments.save("准入核查", brief={"goal": "核查", "constraints": []})
    inbound_id = event("waiting-owner")
    stage(runtime, inbound_id)
    allowed = True
    guards: list[bool] = []

    def guard() -> None:
        guards.append(runtime.configuration_lock.locked())
        if not allowed:
            raise PermissionError("owner binding revoked")

    await runtime.configuration_lock.acquire()
    waiting = asyncio.create_task(
        runtime.submit_mobile_task_control(
            inbound_id=inbound_id,
            source=MobileControlSource("telegram", "100", "7", "7"),
            text="/任务",
            admission_guard=guard,
        )
    )
    try:
        await asyncio.sleep(0)
        assert not waiting.done() and not guards
        allowed = False
        runtime.configuration_lock.release()
        with pytest.raises(PermissionError, match="revoked"):
            await waiting
        assert guards == [True]
        assert (
            runtime.store.db.execute(
                "SELECT receipt FROM channel_inbound WHERE id=?", (inbound_id,)
            ).fetchone()[0]
            is None
        )
        assert services.task_control.current(item["id"])["control"]["epoch"] == 0
        assert not seen and not runtime.tasks
    finally:
        if runtime.configuration_lock.locked():
            runtime.configuration_lock.release()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await runtime.close()


async def test_saved_adjustment_retries_interrupted_notifications_after_real_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    runtime.store.prepare_channels()
    item = await services.commitments.save("持久控制", brief={"goal": "核查", "constraints": []})
    source = MobileControlSource("telegram", "100", "7", "7")
    guards: list[bool] = []
    wakes: list[bool] = []

    def guard() -> None:
        guards.append(runtime.configuration_lock.locked())

    monkeypatch.setattr(runtime, "_wake_main", lambda: wakes.append(True))
    status_id, pause_id, adjust_id = (
        event("restart-status"),
        event("restart-pause"),
        event("restart-adjust"),
    )
    original_notify = runtime.bus.notify
    try:
        for inbound_id, text in (
            (status_id, "/任务"),
            (pause_id, f"/暂停 {short(status_id)}"),
        ):
            stage(runtime, inbound_id)
            await runtime.submit_mobile_task_control(
                inbound_id=inbound_id, source=source, text=text, admission_guard=guard
            )
        stage(runtime, adjust_id)
        wakes.clear()

        async def interrupt_notify() -> None:
            assert runtime.store.db.execute(
                "SELECT receipt IS NOT NULL FROM channel_inbound WHERE id=?", (adjust_id,)
            ).fetchone()[0]
            raise asyncio.CancelledError("notification interrupted after durable receipt")

        monkeypatch.setattr(runtime.bus, "notify", interrupt_notify)
        text = f"/调整 {short(pause_id)} 只交草稿，不得发布"
        with pytest.raises(asyncio.CancelledError, match="notification interrupted"):
            await runtime.submit_mobile_task_control(
                inbound_id=adjust_id, source=source, text=text, admission_guard=guard
            )
        assert not wakes and not seen
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 1
        saved = json.loads(
            runtime.store.db.execute(
                "SELECT receipt FROM channel_inbound WHERE id=?", (adjust_id,)
            ).fetchone()[0]
        )
        assert services.task_control.current(item["id"])["control"]["epoch"] == 2
    finally:
        monkeypatch.setattr(runtime.bus, "notify", original_notify)
        await runtime.close()

    def record_existing_wakeup(self: Runtime) -> None:
        wakes.append(True)

    monkeypatch.setattr(Runtime, "_wake_main", record_existing_wakeup)
    reopened, reopened_seen = make_runtime(tmp_path, [])
    reopened_services = attach_services(reopened, tmp_path)
    try:
        wakes.clear()

        def reopened_guard() -> None:
            guards.append(reopened.configuration_lock.locked())

        repeated = await reopened.submit_mobile_task_control(
            inbound_id=adjust_id, source=source, text=text, admission_guard=reopened_guard
        )
        assert repeated == saved and wakes == [True] and all(guards)
        assert reopened.store.pending_inputs(reopened.main_session()["id"]) == 1
        assert reopened_services.task_control.current(item["id"])["control"]["epoch"] == 2
        assert reopened_services.commitments.get(item["id"])["brief"]["constraints"] == [
            "只交草稿，不得发布"
        ]
        assert not reopened_seen and not reopened.tasks
    finally:
        await reopened.close()


async def test_pure_pause_keeps_existing_pending_input_wakeup_without_new_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    runtime.store.prepare_channels()
    item = await services.commitments.save(
        "已有待处理输入", brief={"goal": "核查", "constraints": []}
    )
    source = MobileControlSource("telegram", "100", "7", "7")
    wakes: list[bool] = []
    monkeypatch.setattr(runtime, "_wake_main", lambda: wakes.append(True))

    def guard() -> None:
        assert runtime.configuration_lock.locked()

    status_id, pause_id = event("pending-status"), event("pending-pause")
    try:
        runtime.conversation.enqueue(
            runtime.main_session()["id"],
            "已有输入",
            source="web",
            principal="owner",
            mode="auto",
            event_id="existing-owner-input",
            max_pending=8,
        )
        stage(runtime, status_id)
        await runtime.submit_mobile_task_control(
            inbound_id=status_id, source=source, text="/任务", admission_guard=guard
        )
        assert not wakes
        stage(runtime, pause_id)
        await runtime.submit_mobile_task_control(
            inbound_id=pause_id,
            source=source,
            text=f"/暂停 {short(status_id)}",
            admission_guard=guard,
        )
        assert wakes == [True] and not seen
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 1
        assert services.task_control.current(item["id"])["control"]["epoch"] == 1
    finally:
        await runtime.close()


async def test_mobile_cached_status_still_requires_current_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    attach_services(runtime, tmp_path)
    runtime.store.prepare_channels()
    inbound_id = event("cached-status")
    stage(runtime, inbound_id)
    source = MobileControlSource("telegram", "100", "7", "7")
    wakes: list[bool] = []
    monkeypatch.setattr(runtime, "_wake_main", lambda: wakes.append(True))

    def guard() -> None:
        assert runtime.configuration_lock.locked()

    try:
        first = await runtime.submit_mobile_task_control(
            inbound_id=inbound_id, source=source, text="/任务", admission_guard=guard
        )

        def rejected() -> None:
            assert runtime.configuration_lock.locked()
            raise PermissionError("current binding rejected")

        with pytest.raises(PermissionError):
            await runtime.submit_mobile_task_control(
                inbound_id=inbound_id, source=source, text="/任务", admission_guard=rejected
            )
        repeated = await runtime.submit_mobile_task_control(
            inbound_id=inbound_id, source=source, text="/任务", admission_guard=guard
        )
        assert repeated == first and not wakes and not seen
    finally:
        await runtime.close()


@pytest.mark.parametrize("damage", ["json", "version", "oversized"])
async def test_cached_bad_mobile_receipt_is_narrow_rejection_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    runtime.store.prepare_channels()
    item = await services.commitments.save("坏回执拒绝", brief={"goal": "核查", "constraints": []})
    inbound_id = event("bad-cached-receipt")
    stage(runtime, inbound_id)
    source = MobileControlSource("telegram", "100", "7", "7")
    notifications: list[bool] = []

    def guard() -> None:
        assert runtime.configuration_lock.locked()

    async def notify() -> None:
        notifications.append(True)

    monkeypatch.setattr(runtime, "_notify_task_control", notify)
    try:
        first = await runtime.submit_mobile_task_control(
            inbound_id=inbound_id, source=source, text="/任务", admission_guard=guard
        )
        if damage == "json":
            damaged = "{"
        elif damage == "version":
            first["mobile_task_control"]["version"] = 99
            damaged = json.dumps(first)
        else:
            damaged = "x" * (runtime.config.settings.limits.max_output_chars * 4 + 1)
        with runtime.store.db:
            runtime.store.db.execute(
                "UPDATE channel_inbound SET receipt=? WHERE id=?", (damaged, inbound_id)
            )
        failures: list[ControlConflict] = []
        for _ in range(2):
            with pytest.raises(ControlConflict) as caught:
                await runtime.submit_mobile_task_control(
                    inbound_id=inbound_id, source=source, text="/任务", admission_guard=guard
                )
            failures.append(caught.value)
        assert [failure.code for failure in failures] == ["reference_invalid", "reference_invalid"]
        assert (
            runtime.store.db.execute(
                "SELECT receipt FROM channel_inbound WHERE id=?", (inbound_id,)
            ).fetchone()[0]
            == damaged
        )
        assert services.task_control.current(item["id"])["control"]["epoch"] == 0
        assert not notifications and not seen and not runtime.tasks
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0

        from muli.mobile_task_control import MobileControlRejected

        assert all(isinstance(failure, MobileControlRejected) for failure in failures)
    finally:
        await runtime.close()


@pytest.mark.parametrize("kind", ["value", "conflict"])
async def test_mobile_notification_failure_preserves_original_error_after_durable_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    runtime.store.prepare_channels()
    item = await services.commitments.save(
        "通知故障保持", brief={"goal": "核查", "constraints": []}
    )
    source = MobileControlSource("telegram", "100", "7", "7")
    status_id, pause_id = event("notify-error-status"), event("notify-error-pause")
    failure: ValueError = (
        ValueError("operational notification failure")
        if kind == "value"
        else ControlConflict("notification_conflict", "operational notification conflict")
    )
    notifications: list[bool] = []
    wakes: list[bool] = []
    original_notify = runtime._notify_task_control
    monkeypatch.setattr(runtime, "_wake_main", lambda: wakes.append(True))

    def guard() -> None:
        assert runtime.configuration_lock.locked()

    async def broken_notify() -> None:
        notifications.append(runtime.configuration_lock.locked())
        assert runtime.store.db.execute(
            "SELECT receipt IS NOT NULL FROM channel_inbound WHERE id=?", (pause_id,)
        ).fetchone()[0]
        raise failure

    try:
        stage(runtime, status_id)
        await runtime.submit_mobile_task_control(
            inbound_id=status_id, source=source, text="/任务", admission_guard=guard
        )
        stage(runtime, pause_id)
        monkeypatch.setattr(runtime, "_notify_task_control", broken_notify)
        text = f"/暂停 {short(status_id)}"
        with pytest.raises(type(failure)) as caught:
            await runtime.submit_mobile_task_control(
                inbound_id=pause_id, source=source, text=text, admission_guard=guard
            )
        assert caught.value is failure and notifications == [False]
        assert not wakes and not seen
        assert services.task_control.current(item["id"])["control"]["epoch"] == 1
        saved = runtime.store.db.execute(
            "SELECT receipt FROM channel_inbound WHERE id=?", (pause_id,)
        ).fetchone()[0]
        monkeypatch.setattr(runtime, "_notify_task_control", original_notify)
        repeated = await runtime.submit_mobile_task_control(
            inbound_id=pause_id, source=source, text=text, admission_guard=guard
        )
        assert repeated == json.loads(saved) and wakes == [True]
        assert services.task_control.current(item["id"])["control"]["epoch"] == 1
        assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0
    finally:
        monkeypatch.setattr(runtime, "_notify_task_control", original_notify)
        await runtime.close()
