"""One-shot literal reminders over newly admitted, host-marked owner text."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from muli.config import Limits, TopicReminderSettings
from muli.conversation import MAX_CONTENT_CHARS
from muli.events import EventBus
from muli.evidence import EvidenceError
from muli.maintenance import Admission
from muli.owner_text import owner_text_prefix
from muli.reply_origin import MAX_ORIGIN_ID_CHARS, MAX_ROUTE_CHARS
from muli.store import SQLITE_MAX_INTEGER, Store, now

_KIND = "topic_reminder"
_STATE_KIND = "topic_reminder_state"
_STATE_ID = "current"
_VERSION = 1
_TITLE_CHARS = 120
_CONTENT_CHARS = 4000
_KEY_CHARS = 120
_MAIN_ID_CHARS = 96  # Store.existing_main_session_id's durable identity bound.
_SOURCE_CHARS = 120  # Conversation.payload's source and principal bounds.
_PRINCIPAL_CHARS = 256
_MAX_KEYWORD_CHARS = 512  # TopicReminderSettings' absolute upper bound.
_METADATA_CHARS = 1024  # Fixed field names, marker, states and numeric metadata.
# JSON escaping can use six bytes per source character, including control text.
_JSON_BYTES_PER_CHAR = 6
_INPUT_BYTES = _JSON_BYTES_PER_CHAR * (
    MAX_CONTENT_CHARS
    + MAX_ROUTE_CHARS
    + MAX_ORIGIN_ID_CHARS
    + _MAIN_ID_CHARS
    + _SOURCE_CHARS
    + _PRINCIPAL_CHARS
    + _METADATA_CHARS
)
_RULE_BYTES = _JSON_BYTES_PER_CHAR * (
    _TITLE_CHARS
    + _CONTENT_CHARS
    + _KEY_CHARS
    + _MAX_KEYWORD_CHARS
    + _MAIN_ID_CHARS
    + MAX_ORIGIN_ID_CHARS * 2
    + _METADATA_CHARS
)
_STATE_BYTES = _JSON_BYTES_PER_CHAR * (_MAIN_ID_CHARS + _METADATA_CHARS)
_MAIN_BYTES = _JSON_BYTES_PER_CHAR * (MAX_ROUTE_CHARS + _METADATA_CHARS)
_RULE_FIELDS = {
    "id",
    "version",
    "main_session_id",
    "created_input_id",
    "key",
    "keyword",
    "title",
    "content",
    "state",
    "after_seq",
    "created_at",
    "updated_at",
}

# Ignore attachments and assembled context: only the finite proof needed by the
# owner-text reader crosses into Python. SQLite checks bytes before json.loads.
_INPUT_PROJECTION = (
    "CASE WHEN json_valid(payload) THEN json_object("
    "'id',json_extract(payload,'$.id'),"
    "'session_id',json_extract(payload,'$.session_id'),"
    "'content',json_extract(payload,'$.content'),"
    "'principal',json_extract(payload,'$.principal'),"
    "'source',json_extract(payload,'$.source'),"
    "'route',json_extract(payload,'$.route'),"
    "'owner_text',json_extract(payload,'$.owner_text')) END"
)
_INPUT_COLUMNS = (
    "seq,"
    f"CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))<={MAX_ORIGIN_ID_CHARS * _JSON_BYTES_PER_CHAR} "
    "THEN id END AS id,"
    f"CASE WHEN typeof(session_id)='text' AND length(CAST(session_id AS BLOB))<={_MAIN_ID_CHARS * _JSON_BYTES_PER_CHAR} "
    "THEN session_id END AS session_id,"
    "CASE WHEN state IN ('pending','delivered') THEN state END AS state,"
    "CASE WHEN typeof(projected)='text' AND length(CAST(projected AS BLOB))<=? "
    "THEN CAST(projected AS BLOB) END AS data"
)


def _text(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError("话题提醒文字或标识长度无效")
    value.encode("utf-8")
    return value


def _sequence(value: object) -> int:
    if type(value) is not int or not 0 <= value <= SQLITE_MAX_INTEGER:
        raise ValueError("话题提醒序号无效")
    return value


def _timestamp(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("话题提醒时间无效")


def _identifier(main_session_id: str, input_id: str, key: str) -> str:
    encoded = json.dumps([main_session_id, input_id, key], ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class TopicReminders:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        *,
        settings: Callable[[], TopicReminderSettings],
        admission: Admission,
        max_output_chars: Callable[[], int] | None = None,
    ) -> None:
        self.store, self.bus = store, bus
        self.settings, self.admission = settings, admission
        defaults = Limits()
        self.max_output_chars = max_output_chars or (lambda: defaults.max_output_chars)
        self.changed = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self._blocked = False

    def _check_output(self, value: object) -> None:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > self.max_output_chars():
            raise EvidenceError("output_budget", "话题提醒结果超过输出预算，请减少正文或列表条数")

    def status(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "paused": not self.admission.allowed,
            "blocked": self._blocked,
            "last_error": self.last_error,
        }

    def _require_connection(self) -> None:
        if self._blocked or self.store.db.in_transaction:
            self._blocked = True
            self.last_error = "topic_reminders_transaction_unsettled"
            raise RuntimeError("话题提醒发现未知事务，已暂停至重新创建组件")

    def _main(self, identifier: str) -> None:
        _text(identifier, _MAIN_ID_CHARS)
        if identifier != self.store.existing_main_session_id():
            raise PermissionError("话题提醒只属于持久主会话")
        session = self.store.bounded_object("session", identifier, max_bytes=_MAIN_BYTES)
        if session is None or session.get("kind") != "main" or session.get("principal") != "owner":
            raise PermissionError("话题提醒需要真实主会话")

    @staticmethod
    def _owner_input(row: Mapping[str, Any], main_session_id: str) -> dict[str, Any] | None:
        if (
            row["data"] is None
            or row["session_id"] != main_session_id
            or row["state"] not in {"pending", "delivered"}
        ):
            return None
        try:
            payload = json.loads(row["data"])
            if (
                not isinstance(payload, dict)
                or payload.get("id") != row["id"]
                or payload.get("session_id") != main_session_id
                or payload.get("principal") != "owner"
            ):
                return None
            _text(payload.get("content"), MAX_CONTENT_CHARS)
            _text(payload.get("source"), _SOURCE_CHARS)
            _text(payload.get("id"), MAX_ORIGIN_ID_CHARS)
            route = payload.get("route")
            if route is not None and len(json.dumps(route, ensure_ascii=False)) > MAX_ROUTE_CHARS:
                return None
            if owner_text_prefix(payload) is None:
                return None
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return None
        return payload

    def _require_owner_input(self, main_session_id: str, input_id: str) -> None:
        _text(input_id, MAX_ORIGIN_ID_CHARS)
        row = self.store.db.execute(
            f"WITH projected_inputs AS (SELECT seq,id,session_id,state,{_INPUT_PROJECTION} AS projected "
            "FROM ingress WHERE id=?) "
            f"SELECT {_INPUT_COLUMNS} FROM projected_inputs",
            (input_id, _INPUT_BYTES),
        ).fetchone()
        if row is None or self._owner_input(row, main_session_id) is None:
            raise PermissionError("话题提醒需要已接纳的主人原始文字")

    def require_owner_input(self, main_session_id: str, input_id: str) -> None:
        """Extra ingress proof; Runtime must also validate its real tool caller."""
        self.admission.require()
        self._require_connection()
        self._main(main_session_id)
        self._require_owner_input(main_session_id, input_id)

    def _rule(self, identifier: str, main_session_id: str) -> dict[str, Any]:
        item = self.store.bounded_object(_KIND, identifier, max_bytes=_RULE_BYTES)
        if item is None:
            raise KeyError(identifier)
        state = item.get("state")
        fields = _RULE_FIELDS | (
            {"matched_input_id", "matched_seq", "fired_at"}
            if state == "fired"
            else {"cancelled_at"}
            if state == "cancelled"
            else set()
        )
        if (
            set(item) != fields
            or type(item.get("version")) is not int
            or item["version"] != _VERSION
            or item.get("main_session_id") != main_session_id
            or state not in {"active", "fired", "cancelled"}
        ):
            raise ValueError("话题提醒持久记录无效")
        _text(item["created_input_id"], MAX_ORIGIN_ID_CHARS)
        _text(item["key"], _KEY_CHARS)
        _text(item["keyword"], _MAX_KEYWORD_CHARS)
        _text(item["title"], _TITLE_CHARS)
        _text(item["content"], _CONTENT_CHARS, empty=True)
        _sequence(item["after_seq"])
        _timestamp(item["created_at"])
        _timestamp(item["updated_at"])
        if identifier != _identifier(main_session_id, item["created_input_id"], item["key"]):
            raise ValueError("话题提醒持久身份无效")
        if state == "fired":
            _text(item["matched_input_id"], MAX_ORIGIN_ID_CHARS)
            if _sequence(item["matched_seq"]) <= item["after_seq"]:
                raise ValueError("话题提醒命中位置无效")
            _timestamp(item["fired_at"])
        elif state == "cancelled":
            _timestamp(item["cancelled_at"])
        return item

    def _active(
        self, main_session_id: str, settings: TopicReminderSettings
    ) -> list[dict[str, Any]]:
        # Include malformed/nonterminal rows so broken evidence parks consumption
        # visibly instead of silently losing a requested reminder.
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))<=64 "
            "THEN id END AS id FROM objects WHERE kind=? AND "
            "(CASE WHEN json_valid(data) THEN json_extract(data,'$.state') END "
            "NOT IN ('fired','cancelled') OR "
            "CASE WHEN json_valid(data) THEN json_extract(data,'$.state') END IS NULL) "
            "ORDER BY id LIMIT ?",
            (_KIND, settings.max_active + 1),
        ).fetchall()
        if len(rows) > settings.max_active:
            raise ValueError("活跃话题提醒超过配置上限，请先取消或恢复配置")
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["id"] is None:
                raise ValueError("话题提醒持久标识无效")
            item = self._rule(row["id"], main_session_id)
            if len(item["keyword"]) > settings.max_keyword_chars:
                raise ValueError("活跃话题提醒关键词超过当前配置上限")
            result.append(item)
        return result

    def create(
        self,
        main_session_id: str,
        input_id: str,
        keyword: str,
        title: str,
        content: str = "",
        *,
        key: str,
    ) -> dict[str, Any]:
        settings = self.settings()
        _text(keyword, settings.max_keyword_chars)
        title = _text(title, _TITLE_CHARS).strip()
        _text(content, _CONTENT_CHARS, empty=True)
        _text(key, _KEY_CHARS)
        self._require_connection()
        with self.admission.operation(), self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._main(main_session_id)
            self._require_owner_input(main_session_id, input_id)
            identifier = _identifier(main_session_id, input_id, key)
            if (
                self.store.db.execute(
                    "SELECT 1 FROM objects WHERE kind=? AND id=?", (_KIND, identifier)
                ).fetchone()
                is not None
            ):
                previous = self._rule(identifier, main_session_id)
                if any(
                    previous[field] != value
                    for field, value in (
                        ("keyword", keyword),
                        ("title", title),
                        ("content", content),
                    )
                ):
                    raise ValueError("相同话题提醒 key 的登记内容不一致")
                result = previous
                self._check_output(result)
            else:
                if len(self._active(main_session_id, settings)) >= settings.max_active:
                    raise ValueError("活跃话题提醒已达到配置上限")
                timestamp = now()
                result = {
                    "id": identifier,
                    "version": _VERSION,
                    "main_session_id": main_session_id,
                    "created_input_id": input_id,
                    "key": key,
                    "keyword": keyword,
                    "title": title,
                    "content": content,
                    "state": "active",
                    "after_seq": self._latest_seq(),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                self._check_output(result)
                self.store._put(_KIND, result)
        self.changed.set()
        return result

    def cancel(self, main_session_id: str, identifier: str) -> dict[str, Any]:
        _text(identifier, 64)
        self._require_connection()
        with self.admission.operation(), self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._main(main_session_id)
            item = self._rule(identifier, main_session_id)
            if item["state"] == "active":
                item.update(state="cancelled", cancelled_at=now())
                item["updated_at"] = item["cancelled_at"]
                self.store._put(_KIND, item)
        self.changed.set()
        return item

    def records(
        self, main_session_id: str, *, after_id: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        _text(after_id, 64, empty=True)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("话题提醒每页需要 1–100 条")
        self._require_connection()
        self._main(main_session_id)
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))<=64 "
            "THEN id END AS id FROM objects WHERE kind=? AND id>? ORDER BY id LIMIT ?",
            (_KIND, after_id, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["id"] is None:
                raise ValueError("话题提醒持久标识无效")
            item = self._rule(row["id"], main_session_id)
            result.append(
                {
                    key: item[key]
                    for key in (
                        "id",
                        "keyword",
                        "title",
                        "state",
                        "created_at",
                        "updated_at",
                        "matched_input_id",
                        "matched_seq",
                    )
                    if key in item
                }
            )
        self._check_output(result)
        return result

    def _latest_seq(self) -> int:
        return _sequence(
            self.store.db.execute("SELECT coalesce(max(seq),0) FROM ingress").fetchone()[0]
        )

    def _position(self, main_session_id: str, active: list[dict[str, Any]]) -> int:
        state = self.store.bounded_object(_STATE_KIND, _STATE_ID, max_bytes=_STATE_BYTES)
        if state is None:
            return min((item["after_seq"] for item in active), default=self._latest_seq())
        if (
            set(state) != {"id", "version", "main_session_id", "after_seq"}
            or type(state.get("version")) is not int
            or state["version"] != _VERSION
            or state.get("main_session_id") != main_session_id
        ):
            raise ValueError("话题提醒消费位置无效")
        return _sequence(state["after_seq"])

    def _save_position(self, main_session_id: str, position: int) -> None:
        self.store._put(
            _STATE_KIND,
            {
                "id": _STATE_ID,
                "version": _VERSION,
                "main_session_id": main_session_id,
                "after_seq": position,
            },
        )

    def _consume_page(self) -> tuple[bool, bool]:
        self._require_connection()
        settings = self.settings()
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            main_session_id = self.store.existing_main_session_id()
            if main_session_id is None:
                raise ValueError("话题提醒主会话缺失")
            self._main(main_session_id)
            active = self._active(main_session_id, settings)
            position, latest = self._position(main_session_id, active), self._latest_seq()
            if position > latest or any(item["after_seq"] > latest for item in active):
                raise ValueError("话题提醒消费位置超出输入序号")
            if not active:
                self._save_position(main_session_id, latest)
                return False, False
            rows = self.store.db.execute(
                "WITH selected AS (SELECT seq,id,session_id,state,payload FROM ingress "
                "WHERE seq>? ORDER BY seq LIMIT ?), projected_inputs AS ("
                f"SELECT seq,id,session_id,state,{_INPUT_PROJECTION} AS projected FROM selected) "
                f"SELECT {_INPUT_COLUMNS} FROM projected_inputs ORDER BY seq",
                (position, settings.batch_size, _INPUT_BYTES),
            ).fetchall()
            notified = False
            for row in rows:
                position = _sequence(row["seq"])
                payload = self._owner_input(row, main_session_id)
                if payload is None:
                    continue
                prefix = owner_text_prefix(payload)
                assert prefix is not None
                for item in active:
                    if (
                        item["state"] != "active"
                        or position <= item["after_seq"]
                        or item["keyword"] not in prefix
                    ):
                        continue
                    timestamp = now()
                    item.update(
                        state="fired",
                        updated_at=timestamp,
                        fired_at=timestamp,
                        matched_input_id=payload["id"],
                        matched_seq=position,
                    )
                    notice = {
                        "id": f"topic_reminder:{item['id']}",
                        "title": item["title"],
                        "content": item["content"],
                        "source": "topic_reminder",
                        "read": False,
                        "created_at": timestamp,
                        "session_id": main_session_id,
                        "topic_reminder_id": item["id"],
                        "matched_input_id": payload["id"],
                        "matched_seq": position,
                    }
                    self.store._event(
                        "notification.created",
                        notice,
                        main_session_id,
                        objects=[("notification", notice), (_KIND, item)],
                    )
                    notified = True
            self._save_position(main_session_id, position)
            return bool(rows) and position < latest, notified

    async def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="muli-topic-reminders")
        else:
            self.changed.set()

    async def close(self) -> None:
        task = self.task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            if self.task is task:
                self.task = None

    async def _run(self) -> None:
        while True:
            self.changed.clear()
            if not self._blocked:
                try:
                    more = True
                    while more:
                        await self.admission.wait()
                        with self.admission.operation():
                            more, notified = self._consume_page()
                        if notified:
                            await self.bus.notify()
                        self.last_error = None
                        if more:
                            await asyncio.sleep(0)
                except Exception:
                    if self._blocked or self.store.db.in_transaction:
                        self._blocked = True
                        self.last_error = "topic_reminders_transaction_unsettled"
                    else:
                        self.last_error = "topic_reminders_failed"
            # No polling, self-notification, or failure timer. New admission,
            # explicit edits/config changes, or start() may request another pass.
            await self.changed.wait()
