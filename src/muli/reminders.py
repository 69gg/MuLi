"""Durable deadline scheduling; idle reminders never call a model."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any

from muli.events import EventBus
from muli.evidence import validate_integer
from muli.maintenance import Admission
from muli.proactive import FrozenRouting
from muli.reminder_calendar import next_after, normalize_calendar
from muli.reminder_followup import followup_binding, task_status
from muli.reply_origin import ReplyOrigin
from muli.store import Store, new_id, now

_REMINDER_KINDS = ("reminder", "calendar_reminder")
_FOLLOWUP_FIELDS = ("commitment_id", "followup", "followup_revision", "followup_occurrence")


def reminder_kind(value: object) -> str:
    if not isinstance(value, str) or value not in _REMINDER_KINDS:
        raise ValueError("未知提醒类型")
    return value


def _calendar_rule(item: dict[str, Any]) -> dict[str, Any]:
    if any(field in item for field in _FOLLOWUP_FIELDS) or item.get("interval_seconds") is not None:
        raise ValueError("日历提醒不能绑定任务或固定间隔")
    return dict(normalize_calendar(item.get("calendar")))


class Reminders:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        *,
        admission: Admission | None = None,
        max_output_chars: Callable[[], int] | None = None,
    ) -> None:
        from muli.config import Limits

        self.store, self.bus = store, bus
        self.admission = admission or Admission()
        defaults = Limits()
        self.max_output_chars = max_output_chars or (lambda: defaults.max_output_chars)
        self.changed = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        # Optional explicit delegations share this clock, not Attention's read-only input.
        self.workflow_tick: Callable[[float], Awaitable[None]] | None = None
        self.workflow_due: Callable[[float], float | None] | None = None
        self._workflow_recover: Callable[[], None] | None = None
        self._clock_log: Callable[[bool], None] | None = None
        self._clock_blocked = False

    def create(
        self,
        title: str,
        due_at: float,
        content: str = "",
        interval_seconds: int | None = None,
        *,
        reply_origin: ReplyOrigin | None = None,
        commitment_id: str | None = None,
    ) -> dict[str, Any]:
        if not title.strip() or len(title) > 120 or len(content) > 4000:
            raise ValueError("提醒标题或内容长度无效")
        if not math.isfinite(due_at) or due_at <= now():
            raise ValueError("提醒时间必须在未来")
        if interval_seconds is not None and not 60 <= interval_seconds <= 31_536_000:
            raise ValueError("重复间隔需为60秒至一年")
        item = self._record(title, content, due_at, interval_seconds, reply_origin)
        if commitment_id is not None:
            self._bind(item, commitment_id, 1)
        self.store.put("reminder", item)
        self.changed.set()
        return item

    def create_calendar(
        self,
        title: str,
        timezone: str,
        local_time: str,
        weekdays: list[int],
        content: str = "",
        *,
        reply_origin: ReplyOrigin | None = None,
    ) -> dict[str, Any]:
        if not title.strip() or len(title) > 120 or len(content) > 4000:
            raise ValueError("提醒标题或内容长度无效")
        rule = normalize_calendar(
            {
                "version": 1,
                "timezone": timezone,
                "local_time": local_time,
                "weekdays": weekdays,
            }
        )
        item = self._record(title, content, next_after(rule, now()), None, reply_origin)
        item["calendar"] = rule
        self.store.put("calendar_reminder", item)
        self.changed.set()
        return {**item, "kind": "calendar_reminder"}

    @staticmethod
    def _record(
        title: str,
        content: str,
        due_at: float,
        interval_seconds: int | None,
        reply_origin: ReplyOrigin | None,
    ) -> dict[str, Any]:
        return {
            "id": new_id(),
            "title": title.strip(),
            "content": content,
            "due_at": due_at,
            "interval_seconds": interval_seconds,
            "enabled": True,
            "created_at": now(),
            "last_fired_at": None,
            "fired_count": 0,
            "routing": FrozenRouting.bound(reply_origin or ReplyOrigin()).dump(),
        }

    def update(
        self, identifier: str, changes: dict[str, Any], *, kind: str = "reminder"
    ) -> dict[str, Any]:
        kind = reminder_kind(kind)
        item = self.store.get(kind, identifier)
        if item is None:
            raise KeyError(identifier)
        item.pop("kind", None)  # Namespace is host metadata, never a persisted selector.
        if kind == "calendar_reminder":
            _calendar_rule(item)
            if set(changes) & {"interval_seconds", "commitment_id"}:
                raise ValueError("日历提醒只能调整下一次时间或提醒内容")
        if set(changes) - {
            "title",
            "content",
            "due_at",
            "interval_seconds",
            "enabled",
            "commitment_id",
        }:
            raise ValueError("未知提醒字段")
        binding = followup_binding(item)
        candidate = {
            **item,
            **{key: value for key, value in changes.items() if key != "commitment_id"},
        }
        if not isinstance(candidate["enabled"], bool):
            raise ValueError("enabled 必须是布尔值")
        if (
            not isinstance(candidate["title"], str)
            or not candidate["title"].strip()
            or len(candidate["title"]) > 120
            or not isinstance(candidate["content"], str)
            or len(candidate["content"]) > 4000
        ):
            raise ValueError("提醒标题或内容无效")
        due = candidate["due_at"]
        if not isinstance(due, (float, int)) or not math.isfinite(due):
            raise ValueError("提醒时间无效")
        if candidate["enabled"] and due <= now():
            raise ValueError("启用提醒需要一个未来时间")
        interval = candidate["interval_seconds"]
        if interval is not None and (type(interval) is not int or not 60 <= interval <= 31_536_000):
            raise ValueError("重复间隔无效")
        if "followup_revision" in item or changes.get("commitment_id") is not None:
            revision = item.get("followup_revision", 0) + 1
            validate_integer(revision, "followup_revision", minimum=1)
            selected = (
                changes.get("commitment_id")
                if "commitment_id" in changes
                else (binding["commitment_id"] if binding is not None else None)
            )
            candidate.pop("followup", None)
            candidate["followup_revision"] = revision
            if selected is not None:
                self._bind(candidate, selected, revision)
        self.store.put(kind, candidate)
        self.changed.set()
        return {**candidate, "kind": kind} if kind == "calendar_reminder" else candidate

    def _bind(self, item: dict[str, Any], identifier: str, revision: int) -> None:
        task_status(self.store, identifier, self.max_output_chars())
        item["followup_revision"] = revision
        item["followup"] = {"version": 1, "commitment_id": identifier, "revision": revision}

    def delete(self, identifier: str, *, kind: str = "reminder") -> bool:
        deleted = self.store.delete(reminder_kind(kind), identifier)
        self.changed.set()
        return deleted

    async def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="muli-reminders")

    async def close(self) -> None:
        task = self.task
        if task is None:
            return
        settled = task.done()
        try:
            if not settled:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except Exception:
            if not settled:
                raise  # A new shutdown failure still belongs to its caller.
            self._log_clock(True)
        finally:
            if self.task is task:
                self.task = None

    def _log_clock(self, failed: bool) -> None:
        if self._park_unsettled_transaction():
            return  # Even an asynchronous log could commit this connection's prefix.
        if self._clock_log is not None:
            try:
                self._clock_log(failed)
            except Exception:
                pass  # Optional diagnostics cannot terminate the clock.

    def _park_unsettled_transaction(self) -> bool:
        # An open transaction after failure has an unknown owner. Never roll it
        # back or let a later clock effect commit it; a change cannot clear this.
        if not self._clock_blocked and self.store.db.in_transaction:
            self._clock_blocked = True
        return self._clock_blocked

    def records(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT kind,id,data FROM objects WHERE kind IN (?,?) ORDER BY updated DESC LIMIT ?",
            (*_REMINDER_KINDS, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(row["data"])
            if not isinstance(item, dict) or item.get("id") != row["id"]:
                raise ValueError("提醒记录身份无效")
            result.append({**item, "kind": row["kind"]})
        return result

    def _active_reminders(self) -> list[dict[str, Any]]:
        return [item for item in self.records(10000) if item["enabled"]]

    async def _fire(self, item: dict[str, Any], timestamp: float) -> None:
        await self.admission.wait()
        with self.admission.operation():
            await self._fire_admitted(item, now())

    async def _fire_admitted(self, item: dict[str, Any], timestamp: float) -> None:
        kind = reminder_kind(item.get("kind", "reminder"))
        current = self.store.get(kind, item["id"])
        if (
            not current
            or not current["enabled"]
            or current["due_at"] != item["due_at"]
            or current["due_at"] > timestamp
        ):
            return
        calendar = _calendar_rule(current) if kind == "calendar_reminder" else None
        current.pop("kind", None)
        binding = followup_binding(current) if calendar is None else None
        selected = current
        notice = {
            "id": f"{kind}:{selected['id']}:{selected['due_at']}",
            "title": selected["title"],
            "content": selected["content"],
            "source": "reminder",
            "read": False,
            "created_at": timestamp,
            "reminder_id": item["id"],
            "routing": FrozenRouting.load(current.get("routing")).dump(),
        }
        if calendar is not None:
            notice["reminder_kind"] = kind
        if binding is not None:
            notice["followup"] = binding
            notice["followup_occurrence"] = {
                "number": current["fired_count"] + 1,
                "due_at": selected["due_at"],
            }
        interval = selected["interval_seconds"]
        current["last_fired_at"] = timestamp
        current["fired_count"] += 1
        if calendar is not None:
            current["due_at"] = next_after(calendar, timestamp)
        elif interval:
            # Coalesce missed occurrences after downtime; never emit a catch-up storm.
            missed = math.floor(max(0, timestamp - selected["due_at"]) / interval) + 1
            current["due_at"] = selected["due_at"] + missed * interval
        else:
            current["enabled"] = False
        with self.store.db:
            self.store._event(
                "notification.created",
                notice,
                objects=[("notification", notice), (kind, current)],
            )
            self.store._event("reminder.updated", {**current, "kind": kind})
        await self.bus.notify()

    async def _run(self) -> None:
        failed_reminders: set[tuple[str, str]] = set()
        retry_reminders: set[tuple[str, str]] = set()
        catalog_failed = False
        retry_catalog = False
        while True:
            await self.admission.wait()
            self.changed.clear()
            if self._clock_blocked:
                await self.changed.wait()
                continue
            timestamp = now()
            if not catalog_failed:
                try:
                    active = self._active_reminders()
                    identifiers = {(item["kind"], item["id"]) for item in active}
                except Exception:
                    if self._park_unsettled_transaction():
                        continue
                    if not retry_catalog:
                        self._log_clock(True)
                    catalog_failed, retry_catalog = True, False
                else:
                    failed_reminders.intersection_update(identifiers)
                    retry_reminders.intersection_update(identifiers)
                    for item in active:
                        if self._clock_blocked:
                            break
                        identifier = (item["kind"], item["id"])
                        if identifier in failed_reminders:
                            continue
                        try:
                            if item["due_at"] <= timestamp:
                                await self._fire(item, timestamp)
                                if identifier in retry_reminders:
                                    retry_reminders.remove(identifier)
                                    self._log_clock(False)
                        except Exception:
                            if self._park_unsettled_transaction():
                                break
                            if identifier not in retry_reminders:
                                self._log_clock(True)
                            retry_reminders.discard(identifier)
                            failed_reminders.add(identifier)
            if self._clock_blocked:
                continue
            if self.workflow_tick is not None:
                await self.workflow_tick(timestamp)
            if self._clock_blocked:
                continue
            deadlines: list[float] = []
            if not catalog_failed:
                try:
                    active = self._active_reminders()
                    identifiers = {(item["kind"], item["id"]) for item in active}
                    failed_reminders.intersection_update(identifiers)
                    retry_reminders.intersection_update(identifiers)
                    deadlines = [
                        float(item["due_at"])
                        for item in active
                        if (item["kind"], item["id"]) not in failed_reminders
                    ]
                except Exception:
                    if self._park_unsettled_transaction():
                        continue
                    if not retry_catalog:
                        self._log_clock(True)
                    catalog_failed, retry_catalog = True, False
                else:
                    if retry_catalog:
                        retry_catalog = False
                        self._log_clock(False)
            if self._clock_blocked:
                continue
            if self.workflow_due is not None:
                due = self.workflow_due(timestamp)
                if due is not None:
                    deadlines.append(due)
            if self._clock_blocked:
                continue
            delay = max(0.01, min(deadlines) - now()) if deadlines else None
            try:
                if delay is None:
                    await self.changed.wait()
                else:
                    await asyncio.wait_for(self.changed.wait(), delay)
            except TimeoutError:
                if not self.changed.is_set():
                    continue
            # Only an actual change permits another attempt. Another healthy
            # deadline is not evidence that a failed admission became safe.
            retry_reminders.update(failed_reminders)
            failed_reminders.clear()
            retry_catalog = retry_catalog or catalog_failed
            catalog_failed = False
            if self._workflow_recover is not None:
                self._workflow_recover()
