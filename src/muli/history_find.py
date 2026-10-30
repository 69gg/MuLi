"""Bounded, read-only literal search of explicitly selected or host-scoped history."""

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
from muli.management_transaction import _object
from muli.store import _SESSION_RECORD_LIMIT, SQLITE_MAX_INTEGER, Store

_VERSION = 1
_DOMAIN = "history_content_find"
_OWNED_DOMAIN = "owner_history_content_find"
_OMISSIONS = ("invalid_mapping", "missing", "oversized", "invalid_message")
_Current = tuple[int, str, str, str, int]


class _Expired(Exception):
    pass


class _RawLimit(Exception):
    def __init__(self, *, oversized: bool) -> None:
        self.oversized = oversized


@dataclass(frozen=True)
class _OwnerRequest:
    root: str
    run: str
    input_id: str
    runtime_id: str


@dataclass(frozen=True)
class _OwnedScope:
    instance: str
    root: str
    run: str
    runtime_id: str
    query: str


_Scope = tuple[str, str, str] | _OwnedScope


@dataclass(frozen=True)
class _State:
    through: int
    before: int | None = None
    current: _Current | None = None
    omitted: int = 0
    excluded_from: int | None = None


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

    def _window(self, scope: _Scope, state: _State | None, budget: _Budget) -> list[dict[str, Any]]:
        upper = state.through if state is not None else None
        if state is not None and state.current is not None:
            upper = min(state.through, state.current[0])
        # Do not join objects, inspect JSON, or filter malformed mappings here.
        arguments: dict[str, Any] = {
            "through_seq": upper,
            "before_seq": state.before if state is not None else None,
            "limit": budget.candidates + 1,
            "metadata_bytes": budget.mapping_id,
        }
        if isinstance(scope, _OwnedScope):
            return self.store.history_evidence_window(**arguments)
        return self.store.message_evidence_window(scope[1], **arguments)

    @staticmethod
    def _encode(scope: _Scope, state: _State) -> str:
        if isinstance(scope, _OwnedScope):
            value: list[Any] = [
                _VERSION,
                _OWNED_DOMAIN,
                scope.instance,
                scope.root,
                scope.run,
                scope.runtime_id,
                scope.query,
                state.through,
                state.before,
                state.current,
                state.omitted,
                state.excluded_from,
            ]
        else:
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
            if not isinstance(data, list):
                raise ValueError
            excluded_from: int | None = None
            if isinstance(scope, _OwnedScope):
                if len(data) != 12:
                    raise ValueError
                (
                    version,
                    domain,
                    instance,
                    root,
                    run,
                    runtime_id,
                    query,
                    through,
                    before,
                    current,
                    omitted,
                    excluded_from,
                ) = data
                if (
                    type(version) is not int
                    or version != _VERSION
                    or domain != _OWNED_DOMAIN
                    or (instance, root, query) != (scope.instance, scope.root, scope.query)
                ):
                    raise ValueError
                if (run, runtime_id) != (scope.run, scope.runtime_id):
                    raise EvidenceError(
                        "history_find_cursor_expired", "跨会话游标仅可在原主运行内续查，请重新查询"
                    )
                excluded_from = _integer(excluded_from, minimum=1)
            else:
                if len(data) != 9:
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
            return _State(through, before, position, omitted, excluded_from)
        except (ValueError, TypeError, RecursionError, UnicodeError, binascii.Error) as exc:
            if isinstance(exc, EvidenceError) and exc.code == "history_find_cursor_expired":
                raise
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
            excluded_from=SQLITE_MAX_INTEGER if isinstance(scope, _OwnedScope) else None,
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
        session_id: str,
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
                "session_id": session_id,
                "message_id": metadata["id"],
                "revision": metadata["revision"],
                "offset_bytes": offset,
            },
        }

    def _scope_object(
        self,
        kind: str,
        identifier: str,
        budget: _Budget,
        stats: _Stats,
        deadline: float,
    ) -> dict[str, Any] | None:
        """Charge complete authority metadata to the same page's raw budget."""
        _check(deadline)
        row = self.store.db.execute(
            "SELECT typeof(data) AS storage_type,length(CAST(data AS BLOB)) AS size "
            "FROM objects WHERE kind=? AND id=?",
            (kind, identifier),
        ).fetchone()
        if row is None:
            return None
        size = row["size"]
        if row["storage_type"] != "text" or type(size) is not int or size < 1:
            raise EvidenceError("content_unavailable", "资料范围身份元数据已损坏")
        if size > budget.raw - stats.json_bytes:
            raise _RawLimit(oversized=size > budget.raw)
        stats.json_bytes += size
        record = self.store.bounded_object(kind, identifier, max_bytes=size, strict_json=True)
        _check(deadline)
        return record

    def _owner_boundary(
        self, owner: _OwnerRequest, budget: _Budget, stats: _Stats, deadline: float
    ) -> int:
        session = self._scope_object("session", owner.root, budget, stats, deadline)
        run = self._scope_object("run", owner.run, budget, stats, deadline)
        if (
            self.store.existing_main_session_id() != owner.root
            or session is None
            or session.get("kind") != "main"
            or session.get("principal") != "owner"
            or any(
                name in session for name in ("workflow_id", "workflow_run_id", "workflow_node_id")
            )
            or run is None
            or run.get("session_id") != owner.root
            or run.get("status") != "running"
        ):
            raise EvidenceError("history_find_authority_unavailable", "主助手当前执行身份无法核验")
        remaining = budget.raw - stats.json_bytes
        row = self.store.db.execute(
            "SELECT seq,CASE WHEN typeof(delivered_seq)='integer' "
            "THEN delivered_seq END AS delivered_seq,typeof(payload) AS payload_type,"
            "length(CAST(payload AS BLOB)) AS size,"
            "CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB))<=? "
            "THEN CAST(payload AS BLOB) END AS payload FROM ingress "
            "WHERE id=? AND session_id=? AND run_id=? AND state='delivered'",
            (remaining, owner.input_id, owner.root, owner.run),
        ).fetchone()
        if row is None:
            raise EvidenceError(
                "history_find_authority_unavailable", "需要当前运行已接纳的主人原始输入"
            )
        size = row["size"]
        if row["payload_type"] != "text" or type(size) is not int or size < 0:
            raise EvidenceError("history_find_authority_unavailable", "主人原始输入无法完整核验")
        if size > remaining:
            raise _RawLimit(oversized=size > budget.raw)
        stats.json_bytes += size
        try:
            payload = _object(row["payload"])
            valid = (
                payload.get("id") == owner.input_id
                and payload.get("session_id") == owner.root
                and payload.get("principal") == "owner"
                and isinstance(payload.get("source"), str)
                and bool(payload["source"])
                and isinstance(payload.get("content"), str)
            )
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise EvidenceError(
                "history_find_authority_unavailable", "主人原始输入无法完整核验"
            ) from exc
        _check(deadline)
        if not valid or not self._input_mapping(owner.root, owner.input_id, row["delivered_seq"]):
            raise EvidenceError(
                "history_find_authority_unavailable", "当前输入不是已归档的真实主人请求"
            )
        # claim_inputs archives ingress in seq order. The run_id index includes
        # rowid/seq, so its first row is also this run's earliest delivered mapping.
        first = self.store.db.execute(
            "SELECT session_id=? AS same_session,state='delivered' AS delivered,"
            "CASE WHEN typeof(delivered_seq)='integer' THEN delivered_seq END AS delivered_seq,"
            "length(CAST(id AS BLOB)) AS id_bytes,"
            "CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(id AS BLOB) END AS input_id FROM ingress "
            "WHERE run_id=? ORDER BY seq LIMIT 1",
            (owner.root, min(budget.mapping_id, max(0, budget.raw - stats.json_bytes)), owner.run),
        ).fetchone()
        _check(deadline)
        if (
            first is not None
            and type(first["id_bytes"]) is int
            and first["id_bytes"] > budget.raw - stats.json_bytes
        ):
            raise _RawLimit(oversized=first["id_bytes"] > budget.raw)
        if (
            first is None
            or first["same_session"] != 1
            or first["delivered"] != 1
            or not isinstance(first["input_id"], bytes)
        ):
            raise EvidenceError(
                "history_find_authority_unavailable", "当前主运行的原始归档边界无法核验"
            )
        identifier = decode_utf8(first["input_id"], allow_incomplete_tail=False)
        boundary = first["delivered_seq"]
        if not self._input_mapping(owner.root, identifier, boundary):
            raise EvidenceError(
                "history_find_authority_unavailable", "当前主运行的归档边界已经失效"
            )
        stats.json_bytes += len(first["input_id"])
        if stats.json_bytes > budget.raw:
            raise _RawLimit(oversized=False)
        return int(boundary)

    def _input_mapping(self, session_id: str, identifier: str, sequence: object) -> bool:
        return (
            type(sequence) is int
            and 1 <= sequence <= SQLITE_MAX_INTEGER
            and self.store.db.execute(
                "SELECT 1 FROM message_order WHERE seq=? AND message_id=? AND session_id=?",
                (sequence, identifier, session_id),
            ).fetchone()
            is not None
        )

    def _in_scope(
        self,
        session_id: str,
        scope: _OwnedScope,
        cache: dict[str, bool],
        budget: _Budget,
        stats: _Stats,
        deadline: float,
    ) -> bool:
        if session_id == scope.root:
            return True
        if session_id in cache:
            return cache[session_id]
        session = self._scope_object("session", session_id, budget, stats, deadline)
        if session is None:
            raise EvidenceError("history_find_scope_missing", "来源会话身份已缺失")
        if session.get("kind") != "team" or any(
            name in session for name in ("workflow_id", "workflow_run_id", "workflow_node_id")
        ):
            cache[session_id] = False
            return False
        agent = self._scope_object("agent", session_id, budget, stats, deadline)
        if agent is None:
            raise EvidenceError("history_find_scope_missing", "来源队友关联已缺失")
        if any(name in agent for name in ("workflow_id", "workflow_run_id", "workflow_node_id")):
            cache[session_id] = False
            return False
        parent_id: object = agent.get("parent_id")
        role: object = session.get("role")
        if (
            session.get("principal") != "owner"
            or not isinstance(role, str)
            or role not in {"worker", "reviewer"}
            or agent.get("session_id") != session_id
            or agent.get("role") != role
            or not isinstance(parent_id, str)
            or not parent_id
        ):
            raise EvidenceError("history_find_scope_invalid", "来源队友关联无法完整核验")
        included = parent_id == scope.root
        cache[session_id] = included
        return included

    def find_owned(
        self,
        root_session_id: str,
        query: str,
        *,
        run_id: str,
        input_id: str,
        runtime_id: str,
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
        max_results: int,
        max_context_chars: int,
        deadline: float,
    ) -> dict[str, Any]:
        """The Runtime supplies current owner authority, never model-selected scope."""
        return self.find(
            root_session_id,
            query,
            cursor=cursor,
            limit=limit,
            max_output_chars=max_output_chars,
            max_results=max_results,
            max_context_chars=max_context_chars,
            deadline=deadline,
            _owner=_OwnerRequest(root_session_id, run_id, input_id, runtime_id),
        )

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
        _owner: _OwnerRequest | None = None,
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
            instance = self._instance(maximum)
            scope: _Scope
            if _owner is None:
                self.evidence._session(session_id)
                scope = (instance, session_id, _hash(needle))
            else:
                for name, identity in (
                    ("run_id", _owner.run),
                    ("input_id", _owner.input_id),
                    ("runtime_id", _owner.runtime_id),
                ):
                    _text(identity, maximum=maximum, name=name)
                if _owner.root != session_id:
                    raise EvidenceError("invalid_arguments", "主助手范围与宿主身份不匹配")
                scope = _OwnedScope(
                    instance, session_id, _owner.run, _owner.runtime_id, _hash(needle)
                )
            state = self._decode(cursor, scope, budget) if cursor is not None else None
            stats = _Stats()
            boundary: int | None = None
            if _owner is not None:
                stats.json_bytes = len(instance.encode("utf-8"))
                boundary = self._owner_boundary(_owner, budget, stats, deadline)
                if state is not None and state.excluded_from != boundary:
                    raise EvidenceError(
                        "history_find_changed", "主运行的原始排除边界已变化，请重新查询"
                    )
                budget = replace(
                    budget,
                    mapping_id=max(
                        1,
                        min(
                            maximum,
                            (budget.raw - stats.json_bytes) // (2 * (budget.candidates + 1)),
                        ),
                    ),
                )
            if state is not None and state.current is not None:
                if len(state.current[1].encode("utf-8")) > budget.mapping_id:
                    raise EvidenceError("output_budget", "当前消息身份已超过本次映射预算")
            _check(deadline)
            rows = self._window(scope, state, budget)
            if _owner is not None:
                stats.json_bytes += sum(
                    len(value.encode("utf-8"))
                    for row in rows
                    for name in ("session_id", "message_id")
                    if isinstance(value := row[name], str)
                )
                if stats.json_bytes > budget.raw:
                    raise _RawLimit(oversized=False)
            state = state or _State(rows[0]["seq"] if rows else 0, excluded_from=boundary)
            return self._search(
                scope, state, rows, needle, min(limit, candidates), budget, deadline, stats=stats
            )
        except _RawLimit as exc:
            raise EvidenceError("output_budget", "预算不足以完整核查当前主人与资料范围") from exc
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
        *,
        stats: _Stats | None = None,
    ) -> dict[str, Any]:
        stats = stats if stats is not None else _Stats()
        items: list[dict[str, Any]] = []
        scope_cache: dict[str, bool] = {}
        progress = False

        def finish(reason: str | None, *, more: bool = True) -> dict[str, Any]:
            if isinstance(scope, _OwnedScope) and reason == "raw_budget" and not progress:
                raise EvidenceError("output_budget", "预算不足以核查首条候选的完整身份与原件")
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
                session_id = row.get("session_id") if isinstance(scope, _OwnedScope) else scope[1]
                if expected is not None and (sequence != expected[0] or identifier != expected[1]):
                    raise EvidenceError("history_find_changed", "续查中的消息映射已经变化或删除")
                if identifier is None or not isinstance(session_id, str):
                    if expected is not None:
                        raise EvidenceError("history_find_changed", "续查中的来源会话映射已经损坏")
                    omit(sequence, "invalid_mapping")
                    continue
                if isinstance(scope, _OwnedScope):
                    if session_id == scope.root and sequence >= int(state.excluded_from or 0):
                        if expected is not None:
                            raise EvidenceError(
                                "history_find_cursor_invalid", "续查位置属于本次主运行"
                            )
                        state = replace(state, before=sequence, current=None)
                        progress = True
                        continue
                    try:
                        included = self._in_scope(
                            session_id, scope, scope_cache, budget, stats, deadline
                        )
                    except _RawLimit as exc:
                        if not exc.oversized:
                            return finish("raw_budget")
                        if expected is not None:
                            raise EvidenceError(
                                "output_budget", "续查来源身份超过本次核查预算"
                            ) from exc
                        omit(sequence, "oversized")
                        continue
                    except EvidenceError as exc:
                        if expected is not None:
                            raise EvidenceError(
                                "history_find_changed", "续查来源关联已变化或损坏"
                            ) from exc
                        omit(
                            sequence,
                            "missing"
                            if exc.code == "history_find_scope_missing"
                            else "invalid_mapping",
                        )
                        continue
                    if not included:
                        if expected is not None:
                            raise EvidenceError(
                                "history_find_changed", "续查来源已不属于当前直属范围"
                            )
                        state = replace(state, before=sequence, current=None)
                        progress = True
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
                        or record.get("session_id") != session_id
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
                    item = self._item(session_id, metadata, record, fingerprint, found, len(needle))
                    if isinstance(scope, _OwnedScope):
                        item["session_id"] = session_id
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
