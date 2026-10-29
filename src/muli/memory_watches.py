"""Local, revocable literal leads from newly completed collected sources."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from muli.events import EventBus
from muli.evidence import EvidenceError
from muli.maintenance import Admission
from muli.memory_source_failure import attachment_revision
from muli.store import SQLITE_MAX_INTEGER, Store

if TYPE_CHECKING:
    from muli.config import MemoryWatchSettings

COMPLETION_EVENT = "memory.source.completed"
MATCH_EVENT = "memory.watch.matched"
MAX_MATCH_PROOF_BYTES = 65536
_COMPLETION_BYTES = 2048
_WATCH_KIND = "memory_watch"
_MATCH_KIND = "memory_watch_match"
_COMPLETION_KIND = "memory_source_completion"
_COMPLETION_CHECKPOINT = "source_completion"
_PRODUCER = "memory_pipeline"
_HEX = re.compile(r"[a-f0-9]{64}")
_SOURCE = re.compile(r"attachment:[a-f0-9]{32}")
_WATCH_FIELDS = {
    "id",
    "version",
    "main_session_id",
    "created_input_id",
    "key",
    "question",
    "match_terms",
    "state",
    "revision",
    "created_after_event",
    "after_event",
}
_COMPLETION_FIELDS = {"version", "id", "producer", "source_id", "revision", "event_id"}
_MATCH_FIELDS = {
    "version",
    "id",
    "watch_id",
    "watch_revision",
    "source_id",
    "source_revision",
    "completion_id",
    "completion_event",
    "question",
    "quote",
}


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _identity(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode("ascii")).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("资料关注需要完整非空文字")
    value.encode("utf-8")
    return value


def _position(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or not int(positive) <= value <= SQLITE_MAX_INTEGER:
        raise ValueError("资料关注位置或修订无效")
    return value


def _watch(store: Store, identifier: str) -> dict[str, Any]:
    item = store.bounded_object(
        _WATCH_KIND, identifier, max_bytes=MAX_MATCH_PROOF_BYTES, strict_json=True
    )
    if (
        item is None
        or item.get("id") != identifier
        or set(item) != _WATCH_FIELDS
        or type(item.get("version")) is not int
        or item["version"] != 1
        or item.get("state") not in {"active", "cancelled"}
        or not isinstance(item.get("match_terms"), list)
        or not item["match_terms"]
    ):
        raise ValueError("资料关注记录无法核查")
    for field in ("main_session_id", "created_input_id", "key", "question"):
        _text(item[field])
    for term in item["match_terms"]:
        _text(term)
    if len(set(item["match_terms"])) != len(item["match_terms"]):
        raise ValueError("资料关注条件重复")
    _position(item["revision"], positive=True)
    _position(item["created_after_event"])
    _position(item["after_event"])
    if item["after_event"] < item["created_after_event"]:
        raise ValueError("资料关注消费位置早于登记位置")
    if identifier != _identity(
        _WATCH_KIND, item["main_session_id"], item["created_input_id"], item["key"]
    ):
        raise ValueError("资料关注持久身份无效")
    return item


def _main_current(store: Store, identifier: str) -> bool:
    if identifier != store.existing_main_session_id():
        return False
    session = store.bounded_object("session", identifier, max_bytes=MAX_MATCH_PROOF_BYTES)
    return bool(
        session is not None
        and session.get("kind") == "main"
        and session.get("principal") == "owner"
    )


def _source_text(store: Store, identifier: str, *, max_source_chars: int) -> str | None:
    row = store.db.execute(
        "SELECT CASE WHEN typeof(content)='text' AND length(CAST(content AS BLOB))<=? "
        "THEN CAST(content AS BLOB) END AS content,"
        "CASE WHEN typeof(content)='text' THEN length(content) END AS chars "
        "FROM memory_sources WHERE id=?",
        (min(max_source_chars * 4, SQLITE_MAX_INTEGER), identifier),
    ).fetchone()
    if row is None or row["chars"] is None:
        return None
    if row["chars"] > max_source_chars or row["content"] is None:
        raise EvidenceError("output_budget", "当前原文预算不足，资料关注等待完整核查")
    return str(row["content"].decode("utf-8"))


def record_source_completion(
    store: Store, source_id: str, revision: str, *, max_source_chars: int
) -> None:
    """Append a first completion inside the caller's job-done transaction."""
    if not store.db.in_transaction:
        raise RuntimeError("资料完成身份必须与整理完成同事务保存")
    if (
        store.db.execute(
            "SELECT 1 FROM memory_jobs j JOIN memory_sources s ON s.id=j.source_id "
            "WHERE j.source_id=? AND j.state='done' AND s.digest=? "
            "AND NOT EXISTS (SELECT 1 FROM memory_forgotten_sources f WHERE f.id=s.id)",
            (source_id, revision),
        ).fetchone()
        is None
    ):
        return
    if attachment_revision(store, source_id, max_source_chars=max_source_chars) != revision:
        return
    identifier = _identity(COMPLETION_EVENT, source_id, revision)
    # A retry/replayed callback cannot turn one immutable source into new work.
    if (
        store.db.execute(
            "SELECT 1 FROM objects WHERE kind=? AND id=?", (_COMPLETION_KIND, identifier)
        ).fetchone()
        is not None
    ):
        return
    payload = {
        "version": 1,
        "id": identifier,
        "producer": _PRODUCER,
        "source_id": source_id,
        "revision": revision,
    }
    event = store._event(COMPLETION_EVENT, payload)
    proof = {**payload, "event_id": event["id"]}
    store._put(_COMPLETION_KIND, proof)
    updated = store.db.execute(
        "UPDATE memory_jobs SET checkpoint=json_set(checkpoint,?,json(?)) "
        "WHERE source_id=? AND state='done' AND json_valid(checkpoint)",
        ("$." + _COMPLETION_CHECKPOINT, _encoded(proof).decode("utf-8"), source_id),
    )
    if updated.rowcount != 1:
        raise ValueError("资料完成身份未能绑定当前完成作业")


def _completion(
    store: Store, identifier: str, *, source_id: str, revision: str, event_id: int
) -> dict[str, Any] | None:
    proof = store.bounded_object(
        _COMPLETION_KIND, identifier, max_bytes=_COMPLETION_BYTES, strict_json=True
    )
    if (
        proof is None
        or set(proof) != _COMPLETION_FIELDS
        or type(proof.get("version")) is not int
        or proof["version"] != 1
        or proof.get("producer") != _PRODUCER
        or proof.get("source_id") != source_id
        or proof.get("revision") != revision
        or proof.get("event_id") != event_id
        or type(proof.get("event_id")) is not int
        or event_id < 1
        or identifier != _identity(COMPLETION_EVENT, source_id, revision)
    ):
        return None
    row = store.db.execute(
        "SELECT CASE WHEN length(CAST(json_extract(j.checkpoint,?) AS BLOB))<=? "
        "THEN CAST(json_extract(j.checkpoint,?) AS BLOB) END AS proof "
        "FROM memory_jobs j JOIN memory_sources s ON s.id=j.source_id "
        "WHERE j.source_id=? AND j.state='done' AND s.digest=? AND json_valid(j.checkpoint) "
        "AND NOT EXISTS (SELECT 1 FROM memory_forgotten_sources f WHERE f.id=s.id)",
        (
            "$." + _COMPLETION_CHECKPOINT,
            _COMPLETION_BYTES,
            "$." + _COMPLETION_CHECKPOINT,
            source_id,
            revision,
        ),
    ).fetchone()
    if row is None or not isinstance(row["proof"], bytes) or json.loads(row["proof"]) != proof:
        return None
    event = store.db.execute(
        "SELECT CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
        "THEN CAST(data AS BLOB) END AS data FROM events "
        "WHERE id=? AND type=? AND session_id IS NULL",
        (_COMPLETION_BYTES, event_id, COMPLETION_EVENT),
    ).fetchone()
    expected = {key: value for key, value in proof.items() if key != "event_id"}
    return (
        proof
        if event is not None
        and event["data"] is not None
        and (json.loads(event["data"]) == expected)
        else None
    )


@dataclass(frozen=True, slots=True)
class MemoryWatchMatch:
    identifier: str
    watch_id: str
    watch_revision: int
    source_id: str
    source_revision: str
    completion_id: str
    completion_event: int
    question: str
    quote: str

    def dump(self) -> dict[str, Any]:
        return {
            "version": 1,
            "id": self.identifier,
            "watch_id": self.watch_id,
            "watch_revision": self.watch_revision,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "completion_id": self.completion_id,
            "completion_event": self.completion_event,
            "question": self.question,
            "quote": self.quote,
        }

    @classmethod
    def load(cls, value: object, *, max_bytes: int) -> MemoryWatchMatch | None:
        if not isinstance(value, dict) or set(value) != _MATCH_FIELDS:
            return None
        try:
            if (
                type(value["version"]) is not int
                or value["version"] != 1
                or any(
                    not isinstance(value[field], str) or _HEX.fullmatch(value[field]) is None
                    for field in ("id", "watch_id", "source_revision", "completion_id")
                )
                or not isinstance(value["source_id"], str)
                or _SOURCE.fullmatch(value["source_id"]) is None
                or len(_encoded(value)) > min(max_bytes, MAX_MATCH_PROOF_BYTES)
            ):
                return None
            _position(value["watch_revision"], positive=True)
            _position(value["completion_event"], positive=True)
            _text(value["question"])
            _text(value["quote"])
            if value["id"] != _identity(
                _MATCH_KIND,
                value["watch_id"],
                value["watch_revision"],
                value["source_id"],
                value["source_revision"],
            ) or value["completion_id"] != _identity(
                COMPLETION_EVENT, value["source_id"], value["source_revision"]
            ):
                return None
            return cls(
                *(
                    value[field]
                    for field in (
                        "id",
                        "watch_id",
                        "watch_revision",
                        "source_id",
                        "source_revision",
                        "completion_id",
                        "completion_event",
                        "question",
                        "quote",
                    )
                )
            )
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None

    def detail(self) -> str:
        return json.dumps(
            {
                "question": self.question,
                "source_id": self.source_id,
                "revision": self.source_revision,
                "quote": self.quote,
                "read_tool": "memory_source",
                "notice": "新收藏资料中的字面线索；尚未验证问题答案。可按需核查原文和关系。",
            },
            ensure_ascii=False,
        )


def match_current(
    store: Store,
    proof: MemoryWatchMatch,
    *,
    max_source_chars: int,
    max_output_chars: int,
    defer_budget: bool = False,
) -> bool:
    """Recheck the current owner watch and exact source, never trust event text."""
    try:
        payload = {"id": proof.identifier, "match": proof.dump()}
        if (
            MemoryWatchMatch.load(proof.dump(), max_bytes=max_output_chars * 4) != proof
            or len(_encoded(payload).decode("utf-8")) > max_output_chars
            or len(_encoded(payload)) > MAX_MATCH_PROOF_BYTES
        ):
            return False
        canonical = store.bounded_object(
            _MATCH_KIND, proof.identifier, max_bytes=MAX_MATCH_PROOF_BYTES, strict_json=True
        )
        watch = _watch(store, proof.watch_id)
        if (
            canonical != proof.dump()
            or watch["state"] != "active"
            or watch["revision"] != proof.watch_revision
            or watch["question"] != proof.question
            or not _main_current(store, watch["main_session_id"])
            or proof.completion_event <= watch["created_after_event"]
        ):
            return False
        if (
            _completion(
                store,
                proof.completion_id,
                source_id=proof.source_id,
                revision=proof.source_revision,
                event_id=proof.completion_event,
            )
            is None
        ):
            return False
        source = _source_text(store, proof.source_id, max_source_chars=max_source_chars)
        return bool(
            source is not None
            and attachment_revision(store, proof.source_id, max_source_chars=max_source_chars)
            == proof.source_revision
            and proof.quote in source
            and all(term in proof.quote for term in watch["match_terms"])
        )
    except EvidenceError as error:
        if defer_budget and error.code == "output_budget":
            raise
        return False
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, sqlite3.Error):
        return False


def input_match(
    store: Store, identifier: str, *, max_output_chars: int
) -> tuple[bool, MemoryWatchMatch | None]:
    try:
        row = store.db.execute(
            "SELECT json_type(payload,'$.attention_memory_watch') AS marked,"
            "CASE WHEN length(CAST(json_extract(payload,'$.attention_memory_watch') AS BLOB))<=? "
            "THEN CAST(json_extract(payload,'$.attention_memory_watch') AS BLOB) END AS proof "
            "FROM ingress WHERE id=? AND json_extract(payload,'$.principal')='system:attention'",
            (min(max_output_chars * 4, MAX_MATCH_PROOF_BYTES), identifier),
        ).fetchone()
        if row is None or row["marked"] is None:
            return False, None
        if row["marked"] != "object" or not isinstance(row["proof"], bytes):
            return True, None
        return True, MemoryWatchMatch.load(json.loads(row["proof"]), max_bytes=max_output_chars * 4)
    except (ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
        return True, None


class MemoryWatchMatchExpired(ValueError):
    """A lead was cancelled or its source disappeared before model/output admission."""


class MemoryWatches:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        *,
        settings: Callable[[], MemoryWatchSettings],
        enabled: Callable[[], bool],
        max_source_chars: Callable[[], int],
        max_output_chars: Callable[[], int],
        admission: Admission,
    ) -> None:
        self.store, self.bus, self.settings, self.enabled = store, bus, settings, enabled
        self.max_source_chars, self.max_output_chars, self.admission = (
            max_source_chars,
            max_output_chars,
            admission,
        )
        self.changed = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.last_error: str | None = None
        self.deferred = False
        self._closed = True

    def _connection(self) -> None:
        if self.store.db.in_transaction:
            raise RuntimeError("资料关注发现未收口事务")

    def _main(self, identifier: str) -> None:
        if not _main_current(self.store, identifier):
            raise PermissionError("资料关注需要真实主会话")

    def _output(self, value: object) -> None:
        if (
            len(_encoded(value)) > MAX_MATCH_PROOF_BYTES
            or len(_encoded(value).decode("utf-8")) > self.max_output_chars()
        ):
            raise EvidenceError("output_budget", "完整资料关注结果超过当前输出预算")

    @staticmethod
    def _storage(value: object) -> None:
        if len(_encoded(value)) > MAX_MATCH_PROOF_BYTES:
            raise EvidenceError("output_budget", "完整资料关注及撤销记录超过固定存储预算")

    def _active(self) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))=64 THEN id END AS id "
            "FROM objects WHERE kind=? AND (CASE WHEN json_valid(data) THEN "
            "json_extract(data,'$.state') END IS NOT 'cancelled') ORDER BY id LIMIT ?",
            (_WATCH_KIND, self.settings().max_active + 1),
        ).fetchall()
        if len(rows) > self.settings().max_active:
            raise ValueError("资料关注数量超过当前配置上限")
        return [_watch(self.store, _text(row["id"])) for row in rows]

    def create(
        self,
        main_session_id: str,
        input_id: str,
        question: str,
        match_terms: list[str],
        *,
        key: str,
    ) -> dict[str, Any]:
        policy = self.settings()
        for value in (main_session_id, input_id, question, key):
            _text(value)
        if (
            not isinstance(match_terms, list)
            or not 1 <= len(match_terms) <= policy.max_match_terms
            or any(len(_text(term)) > policy.max_term_chars for term in match_terms)
            or len(set(match_terms)) != len(match_terms)
        ):
            raise ValueError("资料关注需要有限且不重复的字面条件")
        self._connection()
        with self.admission.operation(), self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._main(main_session_id)
            identifier = _identity(_WATCH_KIND, main_session_id, input_id, key)
            previous = self.store.bounded_object(
                _WATCH_KIND, identifier, max_bytes=MAX_MATCH_PROOF_BYTES, strict_json=True
            )
            if previous is not None:
                result = _watch(self.store, identifier)
                if result["question"] != question or result["match_terms"] != match_terms:
                    raise ValueError("相同资料关注 key 的登记内容不一致")
            else:
                if len(self._active()) >= policy.max_active:
                    raise ValueError("资料关注已达到配置上限")
                position = self.store.event_position()
                result = {
                    "id": identifier,
                    "version": 1,
                    "main_session_id": main_session_id,
                    "created_input_id": input_id,
                    "key": key,
                    "question": question,
                    "match_terms": list(match_terms),
                    "state": "active",
                    "revision": 1,
                    "created_after_event": position,
                    "after_event": position,
                }
            # Reserve a complete future tombstone and the largest cursor before
            # admitting a watch. Later output settings cannot prevent revocation.
            cancelled = {
                **result,
                "state": "cancelled",
                "revision": result["revision"] + int(result["state"] == "active"),
                "after_event": SQLITE_MAX_INTEGER,
            }
            _position(cancelled["revision"], positive=True)
            self._storage(cancelled)
            self._output(result)
            self.store._put(_WATCH_KIND, result)
        self.changed.set()
        return result

    def cancel(self, main_session_id: str, identifier: str) -> dict[str, Any]:
        self._connection()
        with self.admission.operation(), self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._main(main_session_id)
            result = _watch(self.store, identifier)
            if result["main_session_id"] != main_session_id:
                raise PermissionError("资料关注属于其它会话")
            if result["state"] == "active":
                result.update(state="cancelled", revision=result["revision"] + 1)
            _position(result["revision"], positive=True)
            self._storage(result)
            receipt = {field: result[field] for field in ("id", "state", "revision")}
            self._output(receipt)
            self.store._put(_WATCH_KIND, result)
        self.changed.set()
        return receipt

    def records(
        self, main_session_id: str, *, after_id: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("资料关注每页需要 1–100 条")
        if not isinstance(after_id, str) or len(after_id) > 64:
            raise ValueError("资料关注续页位置无效")
        self._connection()
        self._main(main_session_id)
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))=64 THEN id END AS id "
            "FROM objects WHERE kind=? AND id>? ORDER BY id LIMIT ?",
            (_WATCH_KIND, after_id, limit),
        ).fetchall()
        result = [_watch(self.store, _text(row["id"])) for row in rows]
        if any(item["main_session_id"] != main_session_id for item in result):
            raise PermissionError("资料关注属于其它会话")
        self._output(result)
        return result

    def status(self) -> dict[str, Any]:
        return {
            "running": self.task is not None and not self.task.done(),
            "enabled": self.enabled(),
            "deferred": self.deferred,
            "paused": not self.admission.allowed,
            "last_error": self.last_error,
        }

    def _lead(self, watch: Mapping[str, Any], row: sqlite3.Row) -> MemoryWatchMatch | None:
        if row["data"] is None:
            return None
        try:
            payload = json.loads(row["data"])
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        if (
            not isinstance(payload, dict)
            or set(payload) != _COMPLETION_FIELDS - {"event_id"}
            or payload.get("producer") != _PRODUCER
            or not isinstance(payload.get("id"), str)
            or not isinstance(payload.get("source_id"), str)
            or not isinstance(payload.get("revision"), str)
            or _completion(
                self.store,
                payload["id"],
                source_id=payload["source_id"],
                revision=payload["revision"],
                event_id=row["id"],
            )
            is None
        ):
            return None
        text = _source_text(
            self.store, payload["source_id"], max_source_chars=self.max_source_chars()
        )
        if (
            text is None
            or attachment_revision(
                self.store, payload["source_id"], max_source_chars=self.max_source_chars()
            )
            != payload["revision"]
        ):
            return None
        terms = watch["match_terms"]
        positions = [text.find(term) for term in terms]
        maximum = self.settings().max_quote_chars
        while all(position >= 0 for position in positions):
            first = min(range(len(positions)), key=positions.__getitem__)
            start = positions[first]
            end = max(position + len(term) for position, term in zip(positions, terms, strict=True))
            if end - start <= maximum:
                break
            # Earlier occurrences cannot fit this right boundary; skip them in one seek.
            positions[first] = text.find(terms[first], max(start + 1, end - maximum))
        else:
            return None
        # Preserve one exact continuous quote. The bounded context never stitches terms.
        spare = maximum - (end - start)
        left = max(0, start - spare // 2)
        quote = text[left : min(len(text), end + spare - (start - left))]
        return MemoryWatchMatch(
            _identity(
                _MATCH_KIND,
                watch["id"],
                watch["revision"],
                payload["source_id"],
                payload["revision"],
            ),
            watch["id"],
            watch["revision"],
            payload["source_id"],
            payload["revision"],
            payload["id"],
            row["id"],
            watch["question"],
            quote,
        )

    def _consume_page(self) -> tuple[bool, bool]:
        self._connection()
        self.deferred = False
        if not self.enabled():
            return False, False
        latest = self.store.event_position()
        more, notified = False, False
        for watch in self._active():
            self._main(watch["main_session_id"])
            if watch["after_event"] > latest:
                raise ValueError("资料关注消费位置超出事件日志")
            rows = self.store.db.execute(
                "SELECT id,CASE WHEN type=? AND session_id IS NULL AND typeof(data)='text' "
                "AND length(CAST(data AS BLOB))<=? THEN CAST(data AS BLOB) END AS data "
                "FROM events WHERE id>? ORDER BY id LIMIT ?",
                (
                    COMPLETION_EVENT,
                    _COMPLETION_BYTES,
                    watch["after_event"],
                    self.settings().batch_size,
                ),
            ).fetchall()
            position = watch["after_event"]
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                for row in rows:
                    try:
                        lead = self._lead(watch, row)
                    except EvidenceError:
                        self.deferred = True
                        break
                    if (
                        lead is not None
                        and self.store.bounded_object(
                            _MATCH_KIND, lead.identifier, max_bytes=MAX_MATCH_PROOF_BYTES
                        )
                        is None
                    ):
                        payload = {"id": lead.identifier, "match": lead.dump()}
                        try:
                            self._output(payload)
                        except EvidenceError:
                            self.deferred = True
                            break
                        self.store._event(
                            MATCH_EVENT, payload, objects=[(_MATCH_KIND, lead.dump())]
                        )
                        notified = True
                    position = row["id"]
                if position != watch["after_event"]:
                    self.store._put(_WATCH_KIND, {**watch, "after_event": position})
            if rows and position == rows[-1]["id"] and position < latest:
                more = True
        return more, notified

    async def start(self) -> None:
        self._closed = False
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="muli-memory-watches")
        else:
            self.changed.set()

    async def close(self) -> None:
        self._closed = True
        task = self.task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if self.task is task:
                self.task = None

    async def _wait(self, revision: int) -> None:
        async def bus_changed() -> None:
            async with self.bus.changed:
                await self.bus.changed.wait_for(lambda: self.bus.revision != revision)

        waiters = [asyncio.create_task(bus_changed()), asyncio.create_task(self.changed.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _run(self) -> None:
        while not self._closed:
            revision = self.bus.revision
            self.changed.clear()
            try:
                await self.admission.wait()
                with self.admission.operation():
                    more, notified = self._consume_page()
                self.last_error = None
                if notified:
                    await self.bus.notify()
                if more:
                    await asyncio.sleep(0)
                    continue
            except Exception as error:
                self.last_error = "memory_watches_" + type(error).__name__
            await self._wait(revision)
