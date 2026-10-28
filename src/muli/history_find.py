"""Bounded, read-only literal search of explicitly selected saved history."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Any

from muli.attachment_records import _MAX_QUERY_CHARS
from muli.evidence import EvidenceError, decode_utf8, json_size, validate_integer
from muli.history_evidence import HistoryEvidence
from muli.store import _SESSION_RECORD_LIMIT, SQLITE_MAX_INTEGER, Store

_VERSION = 1
_DOMAIN = "history_content_find"
_OMISSIONS = ("invalid_mapping", "missing", "oversized", "invalid_message")
_Scope = tuple[str, str, str]
_Current = tuple[int, str, str, str, int]


class _Expired(Exception):
    pass


@dataclass(frozen=True)
class _State:
    through: int
    before: int | None = None
    current: _Current | None = None
    omitted: int = 0


@dataclass(frozen=True)
class _Budget:
    output: int
    raw: int
    candidates: int
    mapping_id: int


@dataclass
class _Stats:
    scanned: int = 0
    json_bytes: int = 0
    omitted: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_OMISSIONS, 0))


def _text(value: object, *, maximum: int, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError("invalid_arguments", f"{name} 需要预算内的非空文字")
    if len(value) > maximum:
        raise EvidenceError("output_budget", f"本次预算不足以核查完整 {name}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise EvidenceError("invalid_arguments", f"{name} 不是完整 UTF-8") from exc
    if size > maximum:
        raise EvidenceError("output_budget", f"本次预算不足以核查完整 {name}")
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= SQLITE_MAX_INTEGER:
        raise EvidenceError("history_find_cursor_invalid", "历史查找游标的序号或位置无效")
    return value


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _Expired


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: dict[str, Any]) -> str:
    return _hash(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


class HistoryFind:
    """The caller supplies existing limits, deadline and Runtime authority.

    No identity creation, model, filesystem access, old tool execution or writes.
    Raw mapping seeks and object budgets precede any message inspection.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.evidence = HistoryEvidence(store)

    def _instance(self, maximum: int) -> str:
        row = self.store.db.execute(
            "SELECT CASE WHEN typeof(value)='text' "
            "AND length(CAST(value AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(value AS BLOB) END AS value "
            "FROM runtime_state WHERE name='store_instance_v1'",
            (maximum,),
        ).fetchone()
        if row is None or not isinstance(row["value"], bytes):
            raise EvidenceError("history_find_identity_unavailable", "资料库身份缺失或超过预算")
        return decode_utf8(row["value"], allow_incomplete_tail=False)

    def _window(self, session: str, state: _State | None, budget: _Budget) -> list[dict[str, Any]]:
        upper = state.through if state is not None else None
        if state is not None and state.current is not None:
            upper = min(state.through, state.current[0])
        # Do not join objects, inspect JSON, or filter malformed mappings here.
        return self.store.message_evidence_window(
            session,
            through_seq=upper,
            before_seq=state.before if state is not None else None,
            limit=budget.candidates + 1,
            metadata_bytes=budget.mapping_id,
        )

    @staticmethod
    def _encode(scope: _Scope, state: _State) -> str:
        value = [
            _VERSION,
            _DOMAIN,
            *scope,
            state.through,
            state.before,
            state.current,
            state.omitted,
        ]
        return base64.urlsafe_b64encode(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).decode("ascii")

    def _decode(self, value: object, scope: _Scope, budget: _Budget) -> _State:
        maximum = min(budget.raw, budget.output * 4)
        if not isinstance(value, str) or not value:
            raise EvidenceError("history_find_cursor_invalid", "历史查找游标为空或类型无效")
        if len(value) > maximum:
            raise EvidenceError("output_budget", "本次预算不足以核查完整历史查找游标")
        try:
            data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
            if not isinstance(data, list) or len(data) != 9:
                raise ValueError
            version, domain, instance, session, query, through, before, current, omitted = data
            if type(version) is not int or version != _VERSION or domain != _DOMAIN:
                raise ValueError
            if (instance, session, query) != scope:
                raise ValueError
            through = _integer(through)
            omitted = _integer(omitted)
            if before is not None:
                before = _integer(before, minimum=1)
                if before > through:
                    raise ValueError
            position: _Current | None = None
            if current is not None:
                if not isinstance(current, list) or len(current) != 5:
                    raise ValueError
                sequence, identifier, revision, fingerprint, offset = current
                sequence = _integer(sequence, minimum=1)
                offset = _integer(offset)
                identifier = _text(identifier, maximum=maximum, name="message_id")
                revision = _text(revision, maximum=maximum, name="revision")
                if (
                    sequence > through
                    or (before is not None and sequence >= before)
                    or not isinstance(fingerprint, str)
                    or len(fingerprint) != 64
                    or any(character not in "0123456789abcdef" for character in fingerprint)
                ):
                    raise ValueError
                position = (sequence, identifier, revision, fingerprint, offset)
            return _State(through, before, position, omitted)
        except (ValueError, TypeError, RecursionError, UnicodeError, binascii.Error) as exc:
            raise EvidenceError(
                "history_find_cursor_invalid", "游标无效或不属于本次资料库、会话和查询"
            ) from exc

    def _packet(
        self,
        scope: _Scope,
        state: _State,
        items: list[dict[str, Any]],
        stats: _Stats,
        *,
        reason: str | None,
        more: bool,
    ) -> dict[str, Any]:
        return {
            "items": items,
            "through_seq": state.through,
            "scanned": stats.scanned,
            "json_bytes": stats.json_bytes,
            "omitted": dict(stats.omitted),
            "omitted_total": state.omitted,
            "has_more": more,
            "next_cursor": self._encode(scope, state) if more else None,
            "stopped": reason,
            "consistency": "bounded_live",
        }

    def _fits(
        self, scope: _Scope, state: _State, items: list[dict[str, Any]], budget: _Budget
    ) -> bool:
        current = state.current
        if current is not None:
            current = (SQLITE_MAX_INTEGER, current[1], current[2], current[3], SQLITE_MAX_INTEGER)
        reserved = replace(
            state,
            through=SQLITE_MAX_INTEGER,
            before=SQLITE_MAX_INTEGER,
            current=current,
            omitted=SQLITE_MAX_INTEGER,
        )
        stats = _Stats(budget.candidates, budget.raw, dict.fromkeys(_OMISSIONS, budget.candidates))
        return (
            json_size(
                self._packet(scope, reserved, items, stats, reason="candidate_budget", more=True)
            )
            <= budget.output
        )

    def _metadata(self, record: dict[str, Any], sequence: int, output: int) -> dict[str, Any]:
        # Adapt already bounded in-memory values to the existing reader metadata
        # validator. No SQL JSON projection or provider blocks are decoded here.
        row: dict[str, Any] = {
            "id": record["id"],
            "seq": sequence,
            "content_provenance": (
                record.get("content_provenance")
                if record.get("content_provenance") in ("accepted", "runtime_excerpt")
                else "unknown"
            ),
        }
        for name in ("role", "source", "principal", "evidence_revision", "attachment_ids"):
            value = record.get(name)
            raw = b""
            kind: str | None = None if name not in record else "null"
            if isinstance(value, str):
                kind, raw = "text", value.encode("utf-8")
            elif name == "attachment_ids" and isinstance(value, list):
                kind, raw = "array", json.dumps(value, ensure_ascii=False).encode("utf-8")
            elif value is not None:
                kind = "invalid"
            row[name + "_type"] = kind
            row[name + "_bytes"] = len(raw)
            row[name] = raw
        return self.evidence._metadata(row, output)

    def _invalid_json(self, identifier: str) -> bool:
        # A bounded Python object can still exceed SQLite's JSON nesting limit.
        # Guard json_each with the same lazy CASE pattern as existing Store readers.
        row = self.store.db.execute(
            "SELECT CASE WHEN json_valid(data) THEN EXISTS("
            "SELECT 1 FROM json_each(data) GROUP BY key HAVING count(*)>1"
            ") ELSE 1 END AS invalid FROM objects WHERE kind='message' AND id=?",
            (identifier,),
        ).fetchone()
        return row is None or bool(row["invalid"])

    @staticmethod
    def _item(
        scope: _Scope,
        metadata: dict[str, Any],
        record: dict[str, Any],
        fingerprint: str,
        offset: int,
        length: int,
    ) -> dict[str, Any]:
        return {
            "message_id": metadata["id"],
            "seq": metadata["seq"],
            "revision": metadata["revision"],
            "revision_is_legacy": record.get("evidence_revision") is None,
            "message_fingerprint": fingerprint,
            **{
                key: metadata[key]
                for key in ("role", "source", "principal", "content_provenance", "metadata_omitted")
                if key in metadata
            },
            "offset_bytes": offset,
            "length_bytes": length,
            "read_arguments": {
                "session_id": scope[1],
                "message_id": metadata["id"],
                "revision": metadata["revision"],
                "offset_bytes": offset,
            },
        }

    def find(
        self,
        session_id: str,
        query: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
        max_results: int,
        max_context_chars: int,
        deadline: float,
    ) -> dict[str, Any]:
        for name, value in (
            ("max_output_chars", max_output_chars),
            ("max_results", max_results),
            ("max_context_chars", max_context_chars),
            ("limit", limit),
        ):
            validate_integer(value, name, minimum=1)
        try:
            valid_deadline = type(deadline) in {int, float} and math.isfinite(deadline)
        except OverflowError:
            valid_deadline = False
        if not valid_deadline:
            raise EvidenceError("invalid_arguments", "历史查找需要有限的宿主绝对期限")
        if limit > _SESSION_RECORD_LIMIT - 1:
            raise EvidenceError("invalid_arguments", "单页条数超过既有会话目录上限")
        if not isinstance(query, str) or not 1 <= len(query) <= _MAX_QUERY_CHARS:
            raise EvidenceError("invalid_arguments", "需要现有查询长度内的非空字面文字")
        needle = _text(query, maximum=_MAX_QUERY_CHARS * 4, name="query").encode("utf-8")
        raw = min(max_context_chars * 4, SQLITE_MAX_INTEGER)
        candidates = min(max_results, _SESSION_RECORD_LIMIT - 1, raw - 1)
        budget = _Budget(
            max_output_chars,
            raw,
            candidates,
            max(1, min(max_output_chars * 4, raw // (candidates + 1))),
        )
        maximum = min(raw, max_output_chars * 4)
        session_id = _text(session_id, maximum=maximum, name="session_id")
        if cursor is not None:
            if not isinstance(cursor, str):
                raise EvidenceError("history_find_cursor_invalid", "历史查找游标类型无效")
            if len(cursor) > maximum:
                raise EvidenceError("output_budget", "本次预算不足以核查完整历史查找游标")
        if self.store.db.in_transaction:
            raise EvidenceError("history_find_transaction_active", "查找不能接管已有事务")
        try:
            _check(deadline)
            self.store.db.execute("BEGIN")
            self.evidence._session(session_id)
            scope = (self._instance(maximum), session_id, _hash(needle))
            state = self._decode(cursor, scope, budget) if cursor is not None else None
            if state is not None and state.current is not None:
                if len(state.current[1].encode("utf-8")) > budget.mapping_id:
                    raise EvidenceError("output_budget", "当前消息身份已超过本次映射预算")
            _check(deadline)
            rows = self._window(session_id, state, budget)
            state = state or _State(rows[0]["seq"] if rows else 0)
            return self._search(
                scope, state, rows, needle, min(limit, candidates), budget, deadline
            )
        except _Expired as exc:
            raise EvidenceError(
                "history_find_timeout", "历史查找未在原期限内取得可交付进展"
            ) from exc
        except sqlite3.Error as exc:
            raise EvidenceError("history_find_unavailable", "历史查找的本地只读核验失败") from exc
        finally:
            if self.store.db.in_transaction:
                self.store.db.rollback()

    def _search(
        self,
        scope: _Scope,
        state: _State,
        rows: list[dict[str, Any]],
        needle: bytes,
        limit: int,
        budget: _Budget,
        deadline: float,
    ) -> dict[str, Any]:
        stats = _Stats()
        items: list[dict[str, Any]] = []
        progress = False

        def finish(reason: str | None, *, more: bool = True) -> dict[str, Any]:
            result = self._packet(scope, state, items, stats, reason=reason, more=more)
            if json_size(result) > budget.output:
                raise EvidenceError("output_budget", "预算不足以完整返回历史位置、计数和游标")
            return result

        def omit(sequence: int, reason: str) -> None:
            nonlocal state, progress
            if state.omitted == SQLITE_MAX_INTEGER:
                raise EvidenceError("history_find_cursor_invalid", "遗漏计数无法继续")
            stats.omitted[reason] += 1
            state = replace(state, before=sequence, current=None, omitted=state.omitted + 1)
            progress = True

        if not self._fits(scope, state, [], budget):
            raise EvidenceError("output_budget", "预算不足以返回最小历史查找回包及游标")
        try:
            for row in rows[: budget.candidates]:
                _check(deadline)
                sequence, raw_id = row["seq"], row["message_id"]
                expected = state.current
                stats.scanned += 1
                identifier = raw_id if isinstance(raw_id, str) else None
                if expected is not None and (sequence != expected[0] or identifier != expected[1]):
                    raise EvidenceError("history_find_changed", "续查中的消息映射已经变化或删除")
                if identifier is None:
                    omit(sequence, "invalid_mapping")
                    continue
                size_row = self.store.db.execute(
                    "SELECT typeof(data) AS storage_type,length(CAST(data AS BLOB)) AS size "
                    "FROM objects WHERE kind='message' AND id=?",
                    (identifier,),
                ).fetchone()
                _check(deadline)
                if size_row is None:
                    if expected is not None:
                        raise EvidenceError("history_find_changed", "续查中的消息已删除")
                    omit(sequence, "missing")
                    continue
                size = size_row["size"]
                if size_row["storage_type"] != "text" or type(size) is not int or size < 0:
                    if expected is not None:
                        raise EvidenceError("history_find_changed", "续查中的消息已损坏")
                    omit(sequence, "invalid_message")
                    continue
                if size > budget.raw:
                    if expected is not None:
                        raise EvidenceError("output_budget", "续查中的完整消息超过本次原始对象预算")
                    omit(sequence, "oversized")
                    continue
                remaining = budget.raw - stats.json_bytes
                if size > remaining:
                    return finish("raw_budget")
                stats.json_bytes += size
                try:
                    record = self.store.bounded_object("message", identifier, max_bytes=remaining)
                    _check(deadline)
                    if (
                        record is None
                        or record.get("session_id") != scope[1]
                        or not isinstance(record.get("content"), str)
                        or self._invalid_json(identifier)
                    ):
                        raise EvidenceError("content_unavailable", "消息身份或原文无法核对")
                    metadata = self._metadata(record, sequence, budget.output)
                    fingerprint = _digest(record)
                    body = record["content"].encode("utf-8")
                except EvidenceError as exc:
                    if expected is not None:
                        raise EvidenceError(
                            "history_find_changed", "续查中的完整消息已变化或损坏"
                        ) from exc
                    omit(sequence, "invalid_message")
                    continue
                _check(deadline)
                start = expected[4] if expected is not None else 0
                if expected is not None and (metadata["revision"], fingerprint) != expected[2:4]:
                    raise EvidenceError("history_find_changed", "续查中的完整消息版本或指纹已变化")
                if start > len(body) or (start < len(body) and 0x80 <= body[start] <= 0xBF):
                    raise EvidenceError(
                        "history_find_cursor_invalid", "续查位置不是完整 UTF-8 字符起点"
                    )
                active = replace(
                    state, current=(sequence, identifier, metadata["revision"], fingerprint, start)
                )
                if not self._fits(scope, active, items, budget):
                    if not items:
                        raise EvidenceError("output_budget", "预算不足以完整返回当前消息的续查身份")
                    return finish("output_budget")
                _check(deadline)
                state = active
                while True:
                    _check(deadline)
                    found = body.find(needle, start)
                    _check(deadline)
                    if found < 0:
                        state = replace(state, before=sequence, current=None)
                        progress = True
                        break
                    undelivered = replace(
                        state,
                        current=(sequence, identifier, metadata["revision"], fingerprint, found),
                    )
                    following = found + 1
                    while following < len(body) and 0x80 <= body[following] <= 0xBF:
                        following += 1
                    after = replace(
                        state,
                        current=(
                            sequence,
                            identifier,
                            metadata["revision"],
                            fingerprint,
                            following,
                        ),
                    )
                    item = self._item(scope, metadata, record, fingerprint, found, len(needle))
                    fits = self._fits(scope, after, [*items, item], budget)
                    _check(deadline)
                    if not fits:
                        state = undelivered
                        if not items:
                            raise EvidenceError(
                                "output_budget", "预算不足以完整返回首处命中与续查位置"
                            )
                        return finish("output_budget")
                    items.append(item)
                    state, start, progress = after, following, True
                    if len(items) == limit:
                        return finish("limit")
            _check(deadline)
            if state.current is not None:
                # A current cursor must never silently disappear at an empty raw window.
                raise EvidenceError("history_find_changed", "续查中的消息映射已删除")
            return finish(
                "candidate_budget" if len(rows) > budget.candidates else None,
                more=len(rows) > budget.candidates,
            )
        except _Expired:
            if not progress:
                raise
            return finish("deadline")
