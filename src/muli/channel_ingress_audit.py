"""Read accepted private ingress without claiming, replaying or changing it."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from typing import Any

from muli.attachments import MAX_ATTACHMENT_IDS
from muli.channel_history import CHANNEL_HISTORY_PAGE_LIMIT, ChannelHistory
from muli.evidence import EvidenceError, content_page, decode_utf8, json_size, validate_integer
from muli.reply_origin import MAX_ROUTE_CHARS, authorize_route
from muli.store import SQLITE_MAX_INTEGER, Store

_DOMAIN = "channel_ingress_audit"
_VERSION = 1
_STATES = {"pending", "delivered", "cancelled"}
_Scope = tuple[str, str, str, str, str, str]


class _Invalid(Exception):
    pass


class _Missing(Exception):
    pass


class _Expired(Exception):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("invalid_constant")


def _json(raw: bytes) -> Any:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique,
            parse_constant=_constant,
        )
        # Also reject exponent overflow and escaped lone surrogates at any depth.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise _Invalid from exc


def _object(raw: bytes) -> dict[str, Any]:
    value = _json(raw)
    if not isinstance(value, dict):
        raise _Invalid
    return value


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _Expired


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value


@dataclass(frozen=True)
class _Budget:
    output: int
    raw: int
    candidates: int


@dataclass(frozen=True)
class _Position:
    before: int | None = None
    omitted: int = 0


@dataclass
class _Stats:
    scanned: int = 0
    json_bytes: int = 0
    omitted: dict[str, int] = field(
        default_factory=lambda: {"invalid": 0, "oversized": 0, "missing": 0}
    )


class ChannelIngressAudit:
    """Runtime supplies live authorization; old rows only prove historical source."""

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _budget(
        max_output_chars: int,
        max_context_chars: int,
        max_search_results: int,
        max_search_seconds: float,
    ) -> tuple[_Budget, float]:
        for name, value in (
            ("max_output_chars", max_output_chars),
            ("max_context_chars", max_context_chars),
            ("max_search_results", max_search_results),
        ):
            validate_integer(value, name, minimum=1)
        try:
            finite = type(max_search_seconds) in {int, float} and math.isfinite(max_search_seconds)
        except OverflowError:
            finite = False
        if not finite or max_search_seconds <= 0:
            raise EvidenceError("invalid_arguments", "入站核查需要有限的正时间预算")
        return (
            _Budget(
                max_output_chars,
                min(max_context_chars * 4, SQLITE_MAX_INTEGER),
                min(max_search_results, CHANNEL_HISTORY_PAGE_LIMIT),
            ),
            time.monotonic() + max_search_seconds,
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.store.db.in_transaction:
            raise EvidenceError("channel_ingress_transaction_active", "入站核查不能接管已有事务")
        owned = False
        try:
            with closing(self.store.db.execute("BEGIN")):
                owned = True
            yield
        except sqlite3.Error as exc:
            raise EvidenceError(
                "channel_ingress_unavailable", "入站日志的本地只读核验失败"
            ) from exc
        finally:
            if owned and self.store.db.in_transaction:
                self.store.db.rollback()

    def _scope(
        self, name: str, account: str, owner: str, main: str, state: str, budget: _Budget
    ) -> _Scope:
        ChannelHistory._scope(name, account, owner, main)
        if not isinstance(state, str) or state not in {*_STATES, "all"}:
            raise EvidenceError("invalid_arguments", "未知入站日志状态")
        with closing(
            self.store.db.execute(
                "SELECT CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) "
                "BETWEEN 1 AND ? THEN CAST(value AS BLOB) END AS value "
                "FROM runtime_state WHERE name='store_instance_v1'",
                (min(budget.raw, budget.output * 4),),
            )
        ) as rows:
            row = rows.fetchone()
        if row is None or not isinstance(row["value"], bytes):
            raise EvidenceError("channel_ingress_identity_unavailable", "资料库身份缺失或超过预算")
        try:
            instance = decode_utf8(row["value"], allow_incomplete_tail=False)
        except EvidenceError as exc:
            raise EvidenceError(
                "channel_ingress_identity_unavailable", "资料库身份无法核查"
            ) from exc
        if not _identifier(instance):
            raise EvidenceError("channel_ingress_identity_unavailable", "资料库身份无法核查")
        return instance, main, name, account, owner, state

    @staticmethod
    def _header(scope: _Scope) -> dict[str, Any]:
        _, main, name, account, owner, _ = scope
        return {
            "section": "ingress",
            "source": "local",
            "coverage": "accepted_by_muli",
            "consistency": "bounded_live",
            "channel": name,
            "account_id": account,
            "owner_id": owner,
            "main_session_id": main,
        }

    @staticmethod
    def _encode(scope: _Scope, position: _Position) -> str:
        raw = json.dumps(
            [_VERSION, _DOMAIN, *scope, position.before, position.omitted],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def _decode(token: str | None, scope: _Scope, maximum: int) -> _Position:
        if token is None:
            return _Position()
        if not isinstance(token, str) or not token:
            raise EvidenceError("channel_ingress_cursor_invalid", "入站核查游标为空或类型无效")
        if len(token) > maximum:
            raise EvidenceError("output_budget", "本次预算不足以核查完整入站游标")
        try:
            value = _json(base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True))
            if not isinstance(value, list) or len(value) != 10:
                raise _Invalid
            version, domain, *fields, before, omitted = value
            if (
                type(version) is not int
                or version != _VERSION
                or domain != _DOMAIN
                or tuple(fields) != scope
                or type(before) is not int
                or not 1 <= before <= SQLITE_MAX_INTEGER
                or type(omitted) is not int
                or not 0 <= omitted <= SQLITE_MAX_INTEGER
            ):
                raise _Invalid
            return _Position(before, omitted)
        except (_Invalid, ValueError, UnicodeError, binascii.Error) as exc:
            raise EvidenceError(
                "channel_ingress_cursor_invalid", "游标不属于当前资料库、主会话、私聊或入站状态"
            ) from exc

    @staticmethod
    def _packet(
        scope: _Scope,
        position: _Position,
        items: list[dict[str, Any]],
        stats: _Stats,
        stopped: str,
        more: bool,
    ) -> dict[str, Any]:
        return {
            **ChannelIngressAudit._header(scope),
            "items": items,
            "scanned": stats.scanned,
            "json_bytes": stats.json_bytes,
            "omitted": dict(stats.omitted),
            "omitted_total": position.omitted,
            "has_more": more,
            "next_cursor": ChannelIngressAudit._encode(scope, position) if more else None,
            "stopped": stopped,
        }

    @staticmethod
    def _probe_valid(row: dict[str, Any]) -> bool:
        return (
            all(row[key + "_type"] == "text" for key in ("id", "session_id", "state", "payload"))
            and all(
                type(row[key + "_bytes"]) is int and row[key + "_bytes"] > 0
                for key in ("id", "session_id", "state", "payload")
            )
            and type(row["required_bytes"]) is int
            and row["required_bytes"] > 0
            and ChannelHistory._created(row["received"])
            and (
                row["delivered_type"] == "null"
                or (
                    row["delivered_type"] == "integer"
                    and type(row["delivered_seq"]) is int
                    and 1 <= row["delivered_seq"] <= SQLITE_MAX_INTEGER
                )
            )
        )

    def _load(
        self,
        row: dict[str, Any],
        stats: _Stats,
        parents: dict[str, dict[str, Any] | None],
        deadline: float,
    ) -> tuple[dict[str, Any], bytes, dict[str, Any] | None]:
        for key in ("id", "session_id", "state"):
            raw = row[key]
            if not isinstance(raw, bytes) or len(raw) != row[key + "_bytes"]:
                raise _Invalid
            try:
                row[key] = decode_utf8(raw, allow_incomplete_tail=False)
            except EvidenceError as exc:
                raise _Invalid from exc
            if not _identifier(row[key]):
                raise _Invalid
        _check(deadline)
        try:
            raw_payload = self.store.ingress_audit_bytes(row["seq"], max_bytes=row["payload_bytes"])
        except EvidenceError as exc:
            raise _Invalid from exc
        if raw_payload is None:
            raise _Missing
        stats.json_bytes += len(raw_payload)
        payload = _object(raw_payload)
        _check(deadline)
        session = row["session_id"]
        if row["parent_needed"]:
            if row["parent_type"] != "text" or not row["parent_bytes"]:
                parents[session] = None
            else:
                try:
                    raw_parent = self.store.ingress_audit_bytes(
                        row["seq"], parent=True, max_bytes=row["parent_bytes"]
                    )
                    if raw_parent is None:
                        parents[session] = None
                    else:
                        stats.json_bytes += len(raw_parent)
                        parents[session] = _object(raw_parent)
                except (EvidenceError, _Invalid):
                    parents[session] = None
        _check(deadline)
        return payload, raw_payload, parents.get(session)

    @staticmethod
    def _metadata(
        scope: _Scope, row: dict[str, Any], payload: dict[str, Any], parent: dict[str, Any] | None
    ) -> dict[str, Any]:
        _, main, name, account, owner, _ = scope
        route = payload.get("route")
        if (
            payload.get("id") != row["id"]
            or payload.get("session_id") != row["session_id"]
            or payload.get("principal") != "owner"
            or payload.get("source") != f"channel:{name}:{account}"
            or row["state"] not in _STATES
            or payload.get("channel_ownership_control", False) is not False
            or not isinstance(route, dict)
            or any(
                route.get(key) != expected
                for key, expected in (
                    ("name", name),
                    ("account_id", account),
                    ("sender_id", owner),
                    ("target", owner),
                    ("kind", "private"),
                )
            )
            or ("reply_to" in route and not _identifier(route["reply_to"]))
            or json_size(route) > MAX_ROUTE_CHARS
        ):
            raise _Invalid
        try:
            authorize_route(
                name, route, account_id=account, owner_ids=[owner], allowed_groups=[], outbound=True
            )
        except ValueError as exc:
            raise _Invalid from exc
        if row["session_id"] != main:
            proof = payload.get("channel_ownership")
            if (
                not isinstance(proof, dict)
                or set(proof) != {"id", "epoch", "main_id", "agent_id"}
                or not _identifier(proof.get("id"))
                or type(proof.get("epoch")) is not int
                or not 1 <= proof["epoch"] <= SQLITE_MAX_INTEGER
                or proof.get("main_id") != main
                or proof.get("agent_id") != row["session_id"]
                or parent is None
                or parent.get("id") != row["session_id"]
                or parent.get("parent_id") != main
                or parent.get("session_id", row["session_id"]) != row["session_id"]
            ):
                raise _Invalid
        attachments = payload.get("attachment_ids", [])
        if (
            not isinstance(attachments, list)
            or len(attachments) > MAX_ATTACHMENT_IDS
            or any(
                not isinstance(value, str) or not value or len(value) > 256 for value in attachments
            )
        ):
            raise _Invalid
        return {
            "input_id": row["id"],
            "ingress_seq": row["seq"],
            "session_id": row["session_id"],
            "state": row["state"],
            "received_at": row["received"],
            "delivered_message_seq": row["delivered_seq"],
            "attachment_ids": attachments,
        }

    def read(
        self,
        name: str,
        account_id: str,
        owner_id: str,
        main_id: str,
        *,
        state: str = "all",
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
        max_context_chars: int,
        max_search_results: int,
        max_search_seconds: float,
    ) -> dict[str, Any]:
        budget, deadline = self._budget(
            max_output_chars, max_context_chars, max_search_results, max_search_seconds
        )
        validate_integer(limit, "limit", minimum=1)
        if limit > CHANNEL_HISTORY_PAGE_LIMIT:
            raise EvidenceError("invalid_arguments", "单页条数超过既有渠道目录上限")
        try:
            with self._transaction():
                _check(deadline)
                scope = self._scope(name, account_id, owner_id, main_id, state, budget)
                position = self._decode(cursor, scope, budget.output)
                _check(deadline)
                return self._read(scope, position, limit, budget, deadline)
        except _Expired as exc:
            raise EvidenceError(
                "channel_ingress_timeout", "入站核查未在原期限内取得可交付进展"
            ) from exc
        except sqlite3.Error as exc:
            raise EvidenceError(
                "channel_ingress_unavailable", "入站日志的本地只读核验失败"
            ) from exc

    def _read(
        self, scope: _Scope, position: _Position, limit: int, budget: _Budget, deadline: float
    ) -> dict[str, Any]:
        stats = _Stats()
        items: list[dict[str, Any]] = []
        parents: dict[str, dict[str, Any] | None] = {}
        progress = False

        def finish(reason: str, more: bool = True) -> dict[str, Any]:
            result = self._packet(scope, position, items, stats, reason, more)
            if json_size(result) > budget.output:
                raise EvidenceError("output_budget", "预算不足以完整返回入站目录、计数和游标")
            return result

        def omit(seq: int, reason: str) -> None:
            nonlocal position, progress
            if position.omitted == SQLITE_MAX_INTEGER:
                raise EvidenceError("channel_ingress_cursor_invalid", "入站遗漏计数无法继续")
            stats.omitted[reason] += 1
            position = _Position(seq, position.omitted + 1)
            progress = True

        if json_size(self._header(scope)) > budget.output:
            raise EvidenceError("output_budget", "预算不足以返回完整入站范围")
        try:
            with closing(
                self.store.ingress_audit_window(
                    before_seq=position.before, limit=budget.candidates + 1
                )
            ) as rows:
                seq = next(rows, None)
                while seq is not None:
                    _check(deadline)
                    if len(items) == limit:
                        return finish("limit")
                    if stats.scanned == budget.candidates:
                        return finish("candidate_budget")
                    remaining = budget.raw - stats.json_bytes
                    if remaining <= 0:
                        return finish("raw_budget")
                    following = next(rows, None)
                    stats.scanned += 1
                    row = self.store.ingress_audit_probe(
                        scope[1], seq=seq, cached_parents=tuple(parents), max_bytes=remaining
                    )
                    if row is not None:
                        # Probe already returned these complete strings, even
                        # when another metadata field later makes the row invalid.
                        stats.json_bytes += sum(
                            len(row[key])
                            for key in ("id", "session_id", "state")
                            if isinstance(row[key], bytes)
                        )
                    _check(deadline)
                    if row is None:
                        omit(seq, "missing")
                    elif not self._probe_valid(row):
                        omit(seq, "invalid")
                    elif row["required_bytes"] > budget.raw:
                        omit(seq, "oversized")
                    elif row["required_bytes"] > remaining:
                        return finish("raw_budget")
                    else:
                        try:
                            payload, _, parent = self._load(row, stats, parents, deadline)
                            item = self._metadata(scope, row, payload, parent)
                            if not isinstance(payload.get("content"), str):
                                raise _Invalid
                        except _Missing:
                            omit(seq, "missing")
                        except _Invalid:
                            omit(seq, "invalid")
                        else:
                            after = _Position(seq, position.omitted)
                            if scope[-1] == "all" or item["state"] == scope[-1]:
                                candidate = self._packet(
                                    scope,
                                    after,
                                    [*items, item],
                                    stats,
                                    "candidate_budget" if following is not None else "end",
                                    following is not None,
                                )
                                if json_size(candidate) > budget.output:
                                    if not items:
                                        raise EvidenceError(
                                            "output_budget", "预算不足以完整返回首项及入站续页游标"
                                        )
                                    return finish("output_budget")
                                items.append(item)
                            position, progress = after, True
                    seq = following
                _check(deadline)
                return finish("end", more=False)
        except _Expired:
            if not progress:
                raise
            return finish("deadline")

    def read_input(
        self,
        name: str,
        account_id: str,
        owner_id: str,
        main_id: str,
        *,
        input_id: str,
        offset_bytes: int = 0,
        revision: str | None = None,
        max_output_chars: int,
        max_context_chars: int,
        max_search_results: int,
        max_search_seconds: float,
    ) -> dict[str, Any]:
        budget, deadline = self._budget(
            max_output_chars, max_context_chars, max_search_results, max_search_seconds
        )
        validate_integer(offset_bytes, "offset_bytes")
        if not _identifier(input_id) or (
            revision is not None and (not isinstance(revision, str) or not revision)
        ):
            raise EvidenceError("invalid_arguments", "入站正文需要准确 input_id 和有效 revision")
        try:
            input_size = len(input_id.encode("utf-8", errors="strict"))
            if revision is not None:
                revision.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise EvidenceError("invalid_arguments", "入站正文定位不是有效 UTF-8") from exc
        if input_size > min(budget.raw, budget.output * 4):
            raise EvidenceError("output_budget", "入站定位超过本次字节预算")
        if offset_bytes and revision is None:
            raise EvidenceError("invalid_arguments", "续读必须提供上一页 revision")
        try:
            with self._transaction():
                _check(deadline)
                scope = self._scope(name, account_id, owner_id, main_id, "all", budget)
                row = self.store.ingress_audit_probe(
                    main_id, input_id=input_id, max_bytes=budget.raw
                )
                _check(deadline)
                if row is None or not self._probe_valid(row):
                    raise _Invalid
                if row["required_bytes"] > budget.raw:
                    # The complete source proof is still unavailable. An exact
                    # ID cannot distinguish an oversized foreign input from absence.
                    raise _Invalid
                payload, raw, parent = self._load(row, _Stats(), {}, deadline)
                metadata = self._metadata(scope, row, payload, parent)
                current = hashlib.sha256(
                    json.dumps(
                        [_DOMAIN, *scope[:-1], input_id, row["seq"]],
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\x00"
                    + raw
                ).hexdigest()
                if revision is not None and revision != current:
                    raise EvidenceError(
                        "evidence_changed", "入站原文已更新；请从 offset_bytes=0 重读"
                    )
                content = payload.get("content")
                if not isinstance(content, str):
                    raise EvidenceError("content_unavailable", "此入站没有可读取的字符串 content")
                content_bytes = content.encode("utf-8")
                _check(deadline)
                result = content_page(
                    {**self._header(scope), **metadata, "revision": current},
                    content_bytes[offset_bytes : offset_bytes + budget.output * 4],
                    offset_bytes=offset_bytes,
                    total_bytes=len(content_bytes),
                    max_output_chars=budget.output,
                )
                _check(deadline)
                return result
        except (_Invalid, _Missing) as exc:
            raise EvidenceError("evidence_not_found", "入站不存在或无法证明属于此主人私聊") from exc
        except _Expired as exc:
            raise EvidenceError("channel_ingress_timeout", "入站正文未在原期限内完成核查") from exc
        except sqlite3.Error as exc:
            raise EvidenceError(
                "channel_ingress_unavailable", "入站日志的本地只读核验失败"
            ) from exc
