"""Real paused task schedules keep admission and revocation in one transaction."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_scheduled_task_replan_guards import GuardRig, paused_schedule

import muli.scheduled_task_replans as scheduled_replans_module


def ingress_rows(rig: GuardRig) -> list[tuple[Any, ...]]:
    return [
        tuple(row) for row in rig.runtime.store.db.execute("SELECT * FROM ingress ORDER BY seq")
    ]


def control_rows(rig: GuardRig) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in rig.runtime.store.db.execute(
            "SELECT * FROM objects WHERE kind IN "
            "('commitment','commitment_revision','task_control_receipt',"
            "'task_replan_occurrence') ORDER BY kind,id"
        )
    ]


async def test_expired_schedule_never_admits_an_owner_control_or_new_generation(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        before_control = control_rows(rig)
        before_ingress = ingress_rows(rig)
        before_target = rig.runtime.team.get(rig.target_ids[0])
        await rig.runtime.scheduled_task_replans.tick(rig.schedule["expires_at"] + 1)
        assert rig.current()["state"] == "expired"
        assert control_rows(rig) == before_control
        assert ingress_rows(rig) == before_ingress
        assert rig.runtime.store.list("task_replan_occurrence") == []
        assert rig.runtime.team.get(rig.target_ids[0]) == before_target
        assert not rig.forbidden.exists()
        # Repeated real clock delivery cannot turn expiry into admission.
        await rig.runtime.scheduled_task_replans.tick(rig.schedule["expires_at"] + 2)
        assert rig.current()["state"] == "expired"
        assert control_rows(rig) == before_control
        assert ingress_rows(rig) == before_ingress


async def test_occurrence_insert_failure_rolls_back_the_whole_due_admission(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        before_schedule = rig.current()
        before_control = control_rows(rig)
        before_ingress = ingress_rows(rig)
        before_target = rig.runtime.team.get(rig.target_ids[0])
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "CREATE TRIGGER fail_due_occurrence BEFORE INSERT ON objects "
                "WHEN NEW.kind='task_replan_occurrence' "
                "BEGIN SELECT RAISE(ABORT,'occurrence_insert_failed'); END"
            )
        try:
            with pytest.raises(sqlite3.IntegrityError, match="occurrence_insert_failed"):
                await rig.runtime.scheduled_task_replans.tick(rig.due_at)
        finally:
            with rig.runtime.store.db:
                rig.runtime.store.db.execute("DROP TRIGGER fail_due_occurrence")
        assert rig.current() == before_schedule
        assert control_rows(rig) == before_control
        assert ingress_rows(rig) == before_ingress
        assert rig.runtime.store.list("task_replan_occurrence") == []
        assert rig.runtime.team.get(rig.target_ids[0]) == before_target
        assert rig.main_id not in rig.runtime.tasks
        assert not rig.forbidden.exists()

        await rig.admit()
        admitted_control = control_rows(rig)
        admitted_ingress = ingress_rows(rig)
        assert rig.local.task_control.current(rig.task_id)["control"]["epoch"] == 2
        assert len(rig.runtime.store.list("task_replan_occurrence")) == 1
        assert (
            sum(
                receipt["action"] == "request_replan"
                for receipt in rig.runtime.store.list("task_control_receipt")
            )
            == 1
        )
        await rig.runtime.scheduled_task_replans.tick(rig.due_at)
        assert control_rows(rig) == admitted_control
        assert ingress_rows(rig) == admitted_ingress
        assert rig.runtime.team.get(rig.target_ids[0])["generation"] == 1


async def test_owner_input_and_admitted_authorization_revocation_rollback_together(
    tmp_path: Path,
) -> None:
    async with paused_schedule(tmp_path) as rig:
        await rig.admit()
        snapshot = rig.local.task_control.replan_snapshot(rig.main_id, rig.task_id)
        assert rig.local.task_control.is_current_replan(snapshot)
        before_schedule = rig.current()
        before_control = control_rows(rig)
        before_ingress = ingress_rows(rig)
        assert before_schedule["authorization_revoked"] is False
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "CREATE TRIGGER fail_owner_revocation BEFORE UPDATE ON objects "
                "WHEN OLD.kind='task_replan_schedule' "
                "AND json_extract(NEW.data,'$.authorization_revoked')=1 "
                "BEGIN SELECT RAISE(ABORT,'owner_revocation_failed'); END"
            )
        try:
            with pytest.raises(sqlite3.IntegrityError, match="owner_revocation_failed"):
                await rig.runtime.submit(
                    "先别继续原任务，重新说明情况", event_id="transaction-new-owner"
                )
            assert rig.current() == before_schedule
            assert control_rows(rig) == before_control
            assert ingress_rows(rig) == before_ingress
            assert rig.local.task_control.is_current_replan(snapshot)
            assert rig.runtime.team.get(rig.target_ids[0])["generation"] == 1
            assert rig.runtime.store.list("task_plan_delivery") == []
            assert not rig.forbidden.exists()
        finally:
            with rig.runtime.store.db:
                rig.runtime.store.db.execute("DROP TRIGGER fail_owner_revocation")
            # The shared real provider is awaiting this gate; never strand its
            # original selected request when an assertion or SQLite write fails.
            rig.release_due.set()


async def test_expiry_after_actual_read_prevents_approval_and_original_target_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with paused_schedule(tmp_path) as rig:
        await rig.admit()
        run_id = rig.runtime.run_ids[rig.main_id]
        snapshot = rig.local.task_control.replan_snapshot(rig.main_id, rig.task_id)
        assert rig.local.task_control.is_current_replan(snapshot)
        target_before = rig.runtime.team.get(rig.target_ids[0])
        assert target_before["generation"] == target_before["completion_revision"] == 1
        assert target_before["status"] == "paused"
        before_control = rig.local.task_control.current(rig.task_id)
        assert before_control["control"]["epoch"] == 2
        assert before_control["control"]["approved_epoch"] == 0
        assert rig.runtime.store.list("task_plan_delivery") == []

        # Only this manager's authorization clock advances. The real main has
        # already read the full brief and is awaiting its model reply, so this
        # exercises the approval boundary rather than waiting-schedule expiry.
        expires_at = rig.schedule["expires_at"]

        def expired_clock() -> float:
            return float(expires_at)

        monkeypatch.setattr(scheduled_replans_module, "now", expired_clock)
        rig.release_due.set()
        try:
            await asyncio.wait_for(rig.runtime.wait(run_id), 3)
        except (ValueError, PermissionError):
            # A controlled request-level rejection or an ordinary rejected
            # tool result may close this run; neither may authorize the target.
            pass
        finished = rig.runtime.store.get("run", run_id)
        assert finished is not None and finished["status"] in {"completed", "failed", "paused"}
        assert rig.local.task_control.current(rig.task_id) == before_control
        assert not any(
            receipt["actor"] == "main" and receipt["action"] == "approve_plan"
            for receipt in rig.runtime.store.list("task_control_receipt")
        )
        assert rig.runtime.store.list("task_plan_delivery") == []
        assert rig.runtime.team.get(rig.target_ids[0]) == target_before
        assert not rig.forbidden.exists()


async def test_expiry_while_waiting_for_real_configuration_lock_never_admits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with paused_schedule(tmp_path) as rig:
        before_control = control_rows(rig)
        before_ingress = ingress_rows(rig)
        before_target = rig.runtime.team.get(rig.target_ids[0])
        lock = rig.runtime.configuration_lock
        original_acquire = lock.acquire
        waiting = asyncio.Event()

        async def observed_acquire() -> bool:
            waiting.set()
            return await original_acquire()

        def expired_clock() -> float:
            return float(rig.schedule["expires_at"])

        tick_task: asyncio.Task[None] | None = None
        try:
            async with lock:
                # Delegate the real acquire: this is the existing configuration
                # lock, not a replacement lock or a mocked scheduling decision.
                monkeypatch.setattr(lock, "acquire", observed_acquire)
                tick_task = asyncio.create_task(rig.runtime.scheduled_task_replans.tick(rig.due_at))
                await asyncio.wait_for(waiting.wait(), 3)
                assert lock.locked() and not tick_task.done()
                assert control_rows(rig) == before_control
                assert ingress_rows(rig) == before_ingress
                monkeypatch.setattr(scheduled_replans_module, "now", expired_clock)
            await asyncio.wait_for(asyncio.shield(tick_task), 3)
            assert rig.current()["state"] == "expired"
            assert rig.runtime.store.list("task_replan_occurrence") == []
            assert control_rows(rig) == before_control
            assert ingress_rows(rig) == before_ingress
            assert rig.runtime.team.get(rig.target_ids[0]) == before_target
            assert before_target["status"] == "paused" and before_target["generation"] == 1
            assert not rig.forbidden.exists()
        finally:
            if tick_task is not None:
                if not tick_task.done():
                    tick_task.cancel()
                await asyncio.gather(tick_task, return_exceptions=True)
