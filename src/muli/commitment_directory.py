"""Bounded, read-only metadata traversal of the durable work ledger."""

from __future__ import annotations

import base64
import binascii
import json
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import Any

from muli.evidence import EvidenceError, decode_utf8, json_size, validate_integer
from muli.store import _CONTINUATION_RECORD_LIMIT, _SESSION_RECORD_LIMIT, SQLITE_MAX_INTEGER, Store

_VERSION = 1
_DOMAIN = "commitment_directory"
_OMISSIONS = ("oversized", "invalid", "missing")
_Scope = tuple[str, str]


class _Expired(Exception):
    pass


@dataclass(frozen=True)
class _State:
    before: str | None = None
    omitted: int = 0


@dataclass(frozen=True)
class _Budget:
    output: int
    raw: int
    candidates: int
    key: int


@dataclass
class _Stats:
    scanned: int = 0
    json_bytes: int = 0
    omitted: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_OMISSIONS, 0))


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _Expired


class CommitmentDirectory:
    """Seek immutable keys before inspecting complete, byte-gated JSON objects.

    Runtime supplies the existing authority and limits. This reader never opens
    briefs, resolves references, selects a replan or writes task state.
    """

    def __init__(
        self,
        store: Store,
        project: Callable[[dict[str, Any], int], dict[str, Any]],
        states: tuple[str, ...],
    ) -> None:
        self.store, self.project, self.states = store, project, states

    def _instance(self, maximum: int) -> str:
        row = self.store.db.execute(
            "SELECT CASE WHEN typeof(value)='text' "
            "AND length(CAST(value AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(value AS BLOB) END AS value "
            "FROM runtime_state WHERE name='store_instance_v1'",
            (maximum,),
        ).fetchone()
        if row is None or not isinstance(row["value"], bytes):
            raise EvidenceError(
                "commitment_directory_identity_unavailable", "资料库身份缺失或超过预算"
            )
        return decode_utf8(row["value"], allow_incomplete_tail=False)

    @staticmethod
    def _encode(scope: _Scope, state: _State) -> str:
        value = [_VERSION, _DOMAIN, *scope, state.before, state.omitted]
        return base64.urlsafe_b64encode(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).decode("ascii")

    @staticmethod
    def _decode(value: object, scope: _Scope, maximum: int) -> _State:
        if not isinstance(value, str) or not value:
            raise EvidenceError("commitment_directory_cursor_invalid", "账本目录游标为空或类型无效")
        if len(value) > maximum:
            raise EvidenceError("output_budget", "本次预算不足以核查完整账本目录游标")
        try:
            data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
            if not isinstance(data, list) or len(data) != 6:
                raise ValueError
            version, domain, instance, status, before, omitted = data
            if (
                type(version) is not int
                or version != _VERSION
                or domain != _DOMAIN
                or (instance, status) != scope
                or not isinstance(before, str)
                or not before
                or len(before.encode("utf-8")) > maximum
                or type(omitted) is not int
                or not 0 <= omitted <= SQLITE_MAX_INTEGER
            ):
                raise ValueError
            return _State(before, omitted)
        except (ValueError, TypeError, RecursionError, UnicodeError, binascii.Error) as exc:
            raise EvidenceError(
                "commitment_directory_cursor_invalid", "游标无效或不属于本次资料库、状态和目录用途"
            ) from exc

    def _packet(
        self,
        scope: _Scope,
        state: _State,
        items: list[dict[str, Any]],
        stats: _Stats,
        *,
        reason: str,
        more: bool,
    ) -> dict[str, Any]:
        return {
            "items": items,
            "has_more": more,
            "next_cursor": self._encode(scope, state) if more else None,
            "scanned": stats.scanned,
            "json_bytes": stats.json_bytes,
            "omitted": dict(stats.omitted),
            "omitted_total": state.omitted,
            "stopped": reason,
            "consistency": "bounded_live",
        }

    def _room(
        self, scope: _Scope, state: _State, items: list[dict[str, Any]], budget: _Budget
    ) -> int:
        # Reserve the complete cursor and the largest counters before projecting
        # any item. A later omission or stop reason cannot invalidate this page.
        reserved = replace(state, omitted=SQLITE_MAX_INTEGER)
        stats = _Stats(budget.candidates, budget.raw, dict.fromkeys(_OMISSIONS, budget.candidates))
        shell = self._packet(scope, reserved, items, stats, reason="candidate_budget", more=True)
        return budget.output - json_size(shell) - (2 if items else 0)

    def _valid_record(self, item: dict[str, Any]) -> bool:
        if not isinstance(item.get("status"), str) or item["status"] not in self.states:
            return False
        for name in ("title", "next_step", "assignee", "evidence", "source_id"):
            if name in item and not isinstance(item[name], str):
                return False
        if "revision" in item and (
            type(item["revision"]) is not int or not 1 <= item["revision"] <= SQLITE_MAX_INTEGER
        ):
            return False
        for name in ("created_at", "updated_at"):
            if name in item:
                try:
                    if type(item[name]) not in {int, float} or not math.isfinite(item[name]):
                        return False
                except OverflowError:
                    return False
        if "has_brief" in item and type(item["has_brief"]) is not bool:
            return False
        if "control" in item:
            control = item["control"]
            if (
                not isinstance(control, dict)
                or type(control.get("epoch")) is not int
                or type(control.get("approved_epoch")) is not int
                or not 0 <= control["approved_epoch"] <= control["epoch"]
            ):
                return False
        return True

    def directory(
        self,
        *,
        status: str = "active",
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
        max_results: int,
        max_context_chars: int,
        deadline: float,
    ) -> dict[str, Any]:
        if not isinstance(status, str) or status not in {*self.states, "active", "all"}:
            raise EvidenceError("invalid_arguments", "未知承诺状态")
        for name, value in (
            ("max_output_chars", max_output_chars),
            ("max_results", max_results),
            ("max_context_chars", max_context_chars),
            ("limit", limit),
        ):
            validate_integer(value, name, minimum=1)
        if limit > _SESSION_RECORD_LIMIT - 1:
            raise EvidenceError("invalid_arguments", "单页条数超过既有目录上限")
        try:
            valid_deadline = type(deadline) in {int, float} and math.isfinite(deadline)
        except OverflowError:
            valid_deadline = False
        if not valid_deadline:
            raise EvidenceError("invalid_arguments", "账本目录需要有限的宿主绝对期限")
        raw = min(max_context_chars * 4, SQLITE_MAX_INTEGER)
        budget = _Budget(
            max_output_chars,
            raw,
            min(max_results, _CONTINUATION_RECORD_LIMIT - 1),
            min(raw, max_output_chars * 4),
        )
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise EvidenceError("commitment_directory_cursor_invalid", "账本目录游标为空或类型无效")
        if cursor is not None and len(cursor) > budget.key:
            raise EvidenceError("output_budget", "本次预算不足以核查完整账本目录游标")
        if self.store.db.in_transaction:
            raise EvidenceError("commitment_directory_transaction_active", "目录不能接管已有事务")
        owned = False
        try:
            _check(deadline)
            self.store.db.execute("BEGIN")
            owned = True
            scope = (self._instance(budget.key), status)
            state = self._decode(cursor, scope, budget.key) if cursor is not None else _State()
            _check(deadline)
            return self._read(scope, state, limit, budget, deadline)
        except _Expired as exc:
            raise EvidenceError(
                "commitment_directory_timeout", "账本目录未在原期限内取得可交付进展"
            ) from exc
        except sqlite3.Error as exc:
            raise EvidenceError(
                "commitment_directory_unavailable", "账本目录的本地只读核验失败"
            ) from exc
        finally:
            if owned and self.store.db.in_transaction:
                self.store.db.rollback()

    def _read(
        self, scope: _Scope, state: _State, limit: int, budget: _Budget, deadline: float
    ) -> dict[str, Any]:
        stats = _Stats()
        items: list[dict[str, Any]] = []
        progress = False

        def finish(reason: str, *, more: bool = True) -> dict[str, Any]:
            result = self._packet(scope, state, items, stats, reason=reason, more=more)
            if json_size(result) > budget.output:
                raise EvidenceError("output_budget", "预算不足以完整返回目录、计数和游标")
            return result

        def omit(identifier: str, reason: str) -> None:
            nonlocal state, progress
            if state.omitted == SQLITE_MAX_INTEGER:
                raise EvidenceError("commitment_directory_cursor_invalid", "遗漏计数无法继续")
            stats.omitted[reason] += 1
            state = _State(identifier, state.omitted + 1)
            progress = True

        if self._room(scope, state, [], budget) < 0:
            raise EvidenceError("output_budget", "预算不足以返回最小账本目录回包及游标")
        try:
            with closing(
                self.store.commitment_directory_window(
                    before_id=state.before, limit=budget.candidates + 1, key_bytes=budget.key
                )
            ) as rows:
                for row in rows:
                    _check(deadline)
                    if len(items) == limit:
                        return finish("limit")
                    if stats.scanned == budget.candidates:
                        return finish("candidate_budget")
                    stats.scanned += 1
                    raw_id = row["id"]
                    if not isinstance(raw_id, bytes):
                        raise EvidenceError(
                            "key_budget",
                            "原始任务 ID 无法在预算内完整核查，目录停在此处，未声称查全",
                        )
                    try:
                        identifier = decode_utf8(raw_id, allow_incomplete_tail=False)
                    except EvidenceError as exc:
                        raise EvidenceError(
                            "commitment_directory_key_invalid",
                            "原始任务 ID 不是完整 UTF-8，无法安全续页",
                        ) from exc
                    after = replace(state, before=identifier)
                    if self._room(scope, after, items, budget) < 0:
                        if not items:
                            raise EvidenceError(
                                "output_budget", "预算不足以完整返回原始任务 ID 的续页游标"
                            )
                        return finish("output_budget")
                    size_row = self.store.db.execute(
                        "SELECT typeof(data) AS storage_type,length(CAST(data AS BLOB)) AS size "
                        "FROM objects WHERE kind='commitment' AND id=?",
                        (identifier,),
                    ).fetchone()
                    _check(deadline)
                    if size_row is None:
                        omit(identifier, "missing")
                        continue
                    size = size_row["size"]
                    if size_row["storage_type"] != "text" or type(size) is not int or size <= 0:
                        omit(identifier, "invalid")
                        continue
                    if size > budget.raw:
                        omit(identifier, "oversized")
                        continue
                    remaining = budget.raw - stats.json_bytes
                    if size > remaining:
                        return finish("raw_budget")
                    stats.json_bytes += size
                    try:
                        record = self.store.bounded_object(
                            "commitment", identifier, max_bytes=remaining
                        )
                    except EvidenceError:
                        omit(identifier, "invalid")
                        continue
                    _check(deadline)
                    if record is None:
                        omit(identifier, "missing")
                        continue
                    if not self._valid_record(record):
                        omit(identifier, "invalid")
                        continue
                    matches = scope[1] == "all" or (
                        record["status"] not in {"done", "cancelled"}
                        if scope[1] == "active"
                        else record["status"] == scope[1]
                    )
                    if matches:
                        try:
                            item = self.project(record, self._room(scope, after, items, budget))
                        except ValueError as exc:
                            if not items:
                                raise EvidenceError(
                                    "output_budget", "预算不足以完整返回首项元数据和续页游标"
                                ) from exc
                            return finish("output_budget")
                        _check(deadline)
                        items.append(item)
                    state, progress = after, True
            _check(deadline)
            return finish("end", more=False)
        except _Expired:
            if not progress:
                raise
            return finish("deadline")
