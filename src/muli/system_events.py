"""Bounded application log evidence and trusted sources for the attention inbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Collection, Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from muli.events import EventBus
from muli.proactive import FrozenRouting
from muli.store import Store, new_id

LogLevel = Literal["warning", "error", "critical"]
_LEVELS = {"warning": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL}
_COMPONENTS = frozenset(
    {
        "runtime",
        "update",
        "management",
        "channels",
        "media",
        "plugins",
        "extensions",
        "attention",
        "system_events",
        "unknown",
    }
)
_CODES = frozenset(
    {
        "runtime_failed",
        "update_failed",
        "management_failed",
        "channel_failed",
        "media_failed",
        "plugin_failed",
        "extension_reload_failed",
        "unknown",
    }
)
_EXTRA_FIELDS = ("muli_component", "muli_code", "muli_run_id", "muli_session_id", "muli_notify")
_BATCH_SIZE = 32
_MAX_COUNT = (1 << 63) - 1
_SOURCE_FIELDS = ("id", "level", "component", "code", "run_id", "session_id", "first_at", "notify")


class SystemEventsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(default=True, strict=True)
    notify_enabled: bool = Field(default=True, strict=True)
    capture_level: LogLevel = "error"
    notify_level: LogLevel = "error"
    queue_capacity: int = Field(default=128, ge=1, le=4096, strict=True)
    max_records: int = Field(default=1000, ge=1, le=10_000, strict=True)
    dedup_seconds: float = Field(default=60, ge=1, le=3600, allow_inf_nan=False, strict=True)


def _reference(value: Any) -> str | None:
    return (
        value
        if type(value) is str and len(value) == 32 and re.fullmatch(r"[a-f0-9]{32}", value)
        else None
    )


@dataclass(frozen=True, slots=True)
class _Context:
    run_id: str | None = None
    session_id: str | None = None
    notify: bool = True


_EMPTY_CONTEXT = _Context()
_CONTEXT: ContextVar[_Context] = ContextVar("muli_system_log", default=_EMPTY_CONTEXT)


@contextmanager
def system_log_context(
    *, run_id: str | None = None, session_id: str | None = None, notify: bool = True
) -> Iterator[None]:
    """Attach host-owned attribution; notification suppression survives nested scopes."""
    if (
        (run_id is not None and _reference(run_id) is None)
        or (session_id is not None and _reference(session_id) is None)
        or type(notify) is not bool
    ):
        raise ValueError("日志运行引用或通知标记无效")
    previous = _CONTEXT.get()
    token = _CONTEXT.set(
        _Context(
            run_id or previous.run_id, session_id or previous.session_id, notify and previous.notify
        )
    )
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@dataclass(frozen=True, slots=True)
class _Metadata:
    level: LogLevel
    component: str
    code: str
    captured_at: float
    run_id: str | None
    session_id: str | None
    notify: bool


class SystemLogger(logging.LoggerAdapter[logging.Logger]):
    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        supplied = kwargs.get("extra")
        extra = {
            field: supplied[field]
            for field in _EXTRA_FIELDS
            if type(supplied) is dict and field in supplied
        }
        extra["muli_instance_id"] = self.extra["muli_instance_id"]  # type: ignore[index]
        kwargs["extra"] = extra
        return msg, kwargs


class _Handler(logging.Handler):
    def __init__(self, owner: SystemEvents) -> None:
        super().__init__()
        self.owner = owner

    def emit(self, record: logging.LogRecord) -> None:
        # Never call getMessage/format/handleError here: msg, args and traceback
        # may be unbounded, hostile, or contain credentials.
        self.owner._capture(record.__dict__, self)


class SystemEvents:
    def __init__(
        self, store: Store, bus: EventBus, settings: Callable[[], SystemEventsSettings]
    ) -> None:
        self.store, self.bus, self.settings = store, bus, settings
        # A live owner token also isolates two observers of the same database.
        self._instance_id = new_id()
        self._lock = threading.Lock()
        self._queue: deque[_Metadata] = deque()
        self._policy = settings().model_copy(deep=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._scheduled = False
        self._accepting = False
        self._logger: logging.Logger | None = None
        self._adapter: SystemLogger | None = None
        self._handler: _Handler | None = None
        self._task: asyncio.Task[None] | None = None
        self._counts = dict.fromkeys(
            (
                "captured",
                "overflow",
                "dropped_on_close",
                "failed",
                "persisted",
                "duplicates",
                "pruned",
                "wakeups",
                "dropped_on_failure",
            ),
            0,
        )
        self.last_error: str | None = None

    def _count(self, name: str, amount: int = 1) -> None:
        self._counts[name] = min(_MAX_COUNT, self._counts[name] + amount)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._policy.enabled,
                "notify_enabled": self._policy.notify_enabled,
                "running": self._task is not None and not self._task.done(),
                "queued": len(self._queue),
                **self._counts,
                "last_error": self.last_error,
            }

    async def start(self, logger: logging.Logger) -> SystemLogger:
        if self._handler is not None:
            if self._logger is not logger:
                raise ValueError("系统事件已绑定其它 logger")
            assert self._adapter is not None
            return self._adapter
        self._policy = self.settings().model_copy(deep=True)
        self._logger = logger
        self._adapter = SystemLogger(logger, {"muli_instance_id": self._instance_id})
        self._loop = asyncio.get_running_loop()
        self.last_error = None
        if not self._policy.enabled:
            return self._adapter
        self._wake = asyncio.Event()
        with self._lock:
            self._accepting = True
        self._handler = _Handler(self)
        logger.addHandler(self._handler)
        self._task = asyncio.create_task(self._run(), name="muli-system-events")
        return self._adapter

    async def close(self) -> None:
        handler, self._handler = self._handler, None
        if handler is not None and self._logger is not None:
            self._logger.removeHandler(handler)
        with self._lock:
            self._accepting = False
            self._count("dropped_on_close", len(self._queue))
            self._queue.clear()
            self._scheduled = False
        task, self._task = self._task, None
        try:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            if handler is not None:
                handler.close()

    async def reconfigure(self) -> SystemLogger | None:
        logger = self._logger
        await self.close()
        return await self.start(logger) if logger is not None else None

    def _capture(self, fields: dict[str, Any], handler: _Handler) -> None:
        instance = fields.get("muli_instance_id")
        level = fields.get("levelno")
        if (
            type(instance) is not str
            or len(instance) != 32
            or instance != self._instance_id
            or type(level) is not int
            or level < _LEVELS[self._policy.capture_level]
        ):
            return
        severity: LogLevel = (
            "critical"
            if level >= logging.CRITICAL
            else ("error" if level >= logging.ERROR else "warning")
        )
        component, code = fields.get("muli_component"), fields.get("muli_code")
        component = (
            component
            if type(component) is str and len(component) <= 32 and component in _COMPONENTS
            else "unknown"
        )
        code = code if type(code) is str and len(code) <= 32 and code in _CODES else "unknown"
        context = _CONTEXT.get()
        run_id = fields.get("muli_run_id", context.run_id)
        session_id = fields.get("muli_session_id", context.session_id)
        notify = context.notify and fields.get("muli_notify", True) is True
        if (run_id is not None and _reference(run_id) is None) or (
            session_id is not None and _reference(session_id) is None
        ):
            notify = False
        metadata = _Metadata(
            severity,
            component,
            code,
            time.time(),
            _reference(run_id),
            _reference(session_id),
            notify and component not in {"attention", "system_events"},
        )
        with self._lock:
            if not self._accepting or self._handler is not handler:
                return
            if len(self._queue) >= self._policy.queue_capacity:
                self._count("overflow")
                return
            self._queue.append(metadata)
            self._count("captured")
            if not self._scheduled:
                self._scheduled = True
                assert self._loop is not None
                try:
                    self._loop.call_soon_threadsafe(self._wake.set)
                except RuntimeError:
                    self._count("dropped_on_close", len(self._queue))
                    self._queue.clear()
                    self._scheduled = False
                    self._accepting = False
                else:
                    self._count("wakeups")

    def _notification_allowed(self, item: _Metadata) -> bool:
        if not item.notify:
            return False
        if item.run_id is None:
            return True
        run = self.store.get("run", item.run_id)
        if run is None or (
            item.session_id is not None and run.get("session_id") != item.session_id
        ):
            return False
        return (
            self.store.db.execute(
                "SELECT 1 FROM ingress WHERE run_id=? "
                "AND json_extract(payload,'$.principal')='system:attention' LIMIT 1",
                (item.run_id,),
            ).fetchone()
            is None
        )

    def _persist(self, item: _Metadata) -> bool:
        allowed = self._notification_allowed(item)
        bucket = int(item.captured_at // self._policy.dedup_seconds)
        identity = (
            item.level,
            item.component,
            item.code,
            item.run_id,
            item.session_id,
            allowed,
            bucket,
        )
        identifier = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        with self.store.db:
            record = self.store.get("system_log", identifier)
            duplicate = record is not None
            if record is not None:
                record["count"] = min(_MAX_COUNT, int(record["count"]) + 1)
                record["last_at"] = item.captured_at
            else:
                record = {
                    "id": identifier,
                    "level": item.level,
                    "component": item.component,
                    "code": item.code,
                    "run_id": item.run_id,
                    "session_id": item.session_id,
                    "first_at": item.captured_at,
                    "last_at": item.captured_at,
                    "count": 1,
                    "notify": allowed,
                    "source": "python_logging",
                }
                event = self.store._event(
                    "system.log", {key: record[key] for key in _SOURCE_FIELDS}
                )
                record["event_id"] = event["id"]
            self.store._put("system_log", record)
        self._count("duplicates" if duplicate else "persisted")
        return not duplicate

    def _prune(self) -> bool:
        # Each transaction handles a bounded suffix. Deleting our own events can
        # leave legal cursor gaps, but must never rewind the shared inbox cursor.
        rows = self.store.db.execute(
            "SELECT id,data FROM objects WHERE kind='system_log' ORDER BY updated DESC,id "
            "LIMIT ? OFFSET ?",
            (_BATCH_SIZE, self._policy.max_records),
        ).fetchall()
        with self.store.db:
            for row in rows:
                record = json.loads(row["data"])
                self.store.db.execute(
                    "DELETE FROM objects WHERE kind='system_log' AND id=?", (row["id"],)
                )
                self.store.db.execute(
                    "DELETE FROM events WHERE id=? AND type='system.log'", (record.get("event_id"),)
                )
        self._count("pruned", len(rows))
        return bool(rows)

    def _retention_failed(self) -> None:
        self.last_error = "retention_failed"
        with self._lock:
            self._accepting = False
            self._count("dropped_on_failure", len(self._queue))
            self._queue.clear()
            self._scheduled = False

    async def _run(self) -> None:
        # A lowered cap is applied in bounded transactions, including on restart.
        while True:
            try:
                more = self._prune()
            except Exception:
                self._retention_failed()
                return
            if not more:
                break
            await asyncio.sleep(0)
        while True:
            await self._wake.wait()
            with self._lock:
                batch = [self._queue.popleft() for _ in range(min(_BATCH_SIZE, len(self._queue)))]
                if not self._queue:
                    self._scheduled = False
                    self._wake.clear()
            changed = False
            for item in batch:
                try:
                    changed = self._persist(item) or changed
                except Exception:
                    self._count("failed")
                    self.last_error = "persistence_failed"
            try:
                self._prune()
            except Exception:
                self._retention_failed()
                return
            if changed:
                try:
                    await self.bus.notify()
                except Exception:
                    self.last_error = "notification_failed"
            await asyncio.sleep(0)


@dataclass(frozen=True, slots=True)
class AttentionSource:
    kind: str
    record_id: str
    detail: str
    logical_key: str
    routing: FrozenRouting = FrozenRouting()


def _notification_source(
    store: Store,
    event: dict[str, Any],
    enabled: Collection[str],
    policy: SystemEventsSettings | None,
) -> AttentionSource | None:
    data, identifier = event["data"], event["data"]["id"]
    notice = store.get("notification", identifier)
    if notice is None or any(
        data.get(field) != notice.get(field)
        for field in (
            "id",
            "title",
            "content",
            "source",
            "reminder_id",
            "created_at",
            "routing",
            "producer",
        )
    ):
        return None
    kind = notice.get("source")
    if kind not in {"reminder", "update"} or kind not in enabled:
        return None
    if not isinstance(notice.get("title"), str) or not isinstance(notice.get("content"), str):
        return None
    routing = FrozenRouting.load(notice.get("routing"))
    if kind == "reminder":
        reminder_id = notice.get("reminder_id")
        if not isinstance(reminder_id, str):
            return None
        reminder = store.get("reminder", reminder_id)
        if (
            reminder is None
            or not reminder.get("fired_count")
            or not identifier.startswith(f"reminder:{reminder_id}:")
            or routing != FrozenRouting.load(reminder.get("routing"))
            or routing.classification == "independent"
        ):
            return None
    else:
        update = store.get("update", "latest")
        if (
            update is None
            or update.get("available") is not True
            or update.get("status") != "checked"
            or identifier != "update-" + str(update.get("latest", ""))
            or (
                routing.classification == "independent"
                and notice.get("producer") != "startup_update_check"
            )
        ):
            return None
    return AttentionSource(
        kind,
        identifier,
        notice["title"] + "\n" + notice["content"],
        "notification:" + identifier,
        routing,
    )


def _management_source(
    store: Store,
    event: dict[str, Any],
    enabled: Collection[str],
    policy: SystemEventsSettings | None,
) -> AttentionSource | None:
    if "management" not in enabled:
        return None
    data, identifier = event["data"], event["data"]["id"]
    plan = store.get("management", identifier)
    if (
        plan is None
        or plan.get("status") not in {"completed", "failed"}
        or plan.get("action") not in {"configure", "restart", "update"}
        or any(data.get(field) != plan.get(field) for field in ("status", "action", "routing"))
        or FrozenRouting.load(plan.get("routing")).classification == "independent"
    ):
        return None
    detail = json.dumps({"action": plan["action"], "status": plan["status"]}, ensure_ascii=False)
    return AttentionSource(
        "management",
        identifier,
        detail,
        f"management:{identifier}:{plan['status']}",
        FrozenRouting.load(plan.get("routing")),
    )


def validated_log_record(
    record: dict[str, Any] | None,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate retained evidence independently of whether it should notify."""
    data = event.get("data")
    if (
        record is None
        or type(data) is not dict
        or any(
            key not in record for key in (*_SOURCE_FIELDS, "last_at", "count", "source", "event_id")
        )
        or any(key not in data for key in _SOURCE_FIELDS)
        or event.get("type") != "system.log"
        or event.get("session_id") is not None
        or type(record.get("id")) is not str
        or re.fullmatch(r"[A-Za-z0-9:._+\-]{1,160}", record["id"]) is None
        or record.get("event_id") != event.get("id")
        or record.get("source") != "python_logging"
        or type(event.get("id")) is not int
        or event["id"] <= 0
        or type(record.get("event_id")) is not int
        or type(record.get("notify")) is not bool
        or any(data.get(field) != record.get(field) for field in _SOURCE_FIELDS)
    ):
        return None
    level, component, code = record.get("level"), record.get("component"), record.get("code")
    if (
        type(level) is not str
        or level not in _LEVELS
        or type(component) is not str
        or component not in _COMPONENTS
        or type(code) is not str
        or code not in _CODES
        or type(record.get("count")) is not int
        or not 1 <= record["count"] <= _MAX_COUNT
    ):
        return None
    for field in ("run_id", "session_id"):
        if record.get(field) is not None and _reference(record[field]) is None:
            return None
    for field in ("first_at", "last_at"):
        value = record.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value < 1e12:
            return None
    return {key: record[key] for key in (*_SOURCE_FIELDS, "last_at", "count", "source", "event_id")}


def _log_source(
    store: Store,
    event: dict[str, Any],
    enabled: Collection[str],
    policy: SystemEventsSettings | None,
) -> AttentionSource | None:
    if policy is None or not policy.enabled or not policy.notify_enabled:
        return None
    identifier = event["data"]["id"]
    record = validated_log_record(store.get("system_log", identifier), event)
    if (
        record is None
        or record["notify"] is not True
        or _LEVELS[record["level"]] < _LEVELS[policy.notify_level]
        or record["component"] in {"attention", "system_events"}
    ):
        return None
    detail = json.dumps(
        {key: record[key] for key in (*_SOURCE_FIELDS[1:], "last_at", "count")}, ensure_ascii=False
    )
    return AttentionSource(
        "system_log", identifier, detail, f"system.log:{event['id']}:{identifier}"
    )


_ADAPTERS: dict[
    str,
    Callable[
        [Store, dict[str, Any], Collection[str], SystemEventsSettings | None],
        AttentionSource | None,
    ],
] = {
    "notification.created": _notification_source,
    "management.completed": _management_source,
    "system.log": _log_source,
}


def attention_source(
    store: Store,
    event: dict[str, Any],
    enabled: Collection[str],
    system_policy: SystemEventsSettings | None = None,
) -> AttentionSource | None:
    if event.get("session_id") is not None or not isinstance(event.get("data"), dict):
        return None
    identifier = event["data"].get("id")
    if (
        not isinstance(identifier, str)
        or re.fullmatch(r"[A-Za-z0-9:._+\-]{1,160}", identifier) is None
    ):
        return None
    adapter = _ADAPTERS.get(event.get("type", ""))
    return adapter(store, event, enabled, system_policy) if adapter else None
