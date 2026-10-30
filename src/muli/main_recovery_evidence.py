"""Bounded discovery of already recovered main runs; historical data grants no authority."""

from __future__ import annotations

import base64
import binascii
import json
import math
import sqlite3
from typing import Any

from muli.evidence import EvidenceError, decode_utf8, json_size, validate_integer
from muli.history_find import (
    HistoryFind,
    _Budget,
    _check,
    _Expired,
    _OwnerRequest,
    _RawLimit,
    _Stats,
    _text,
)
from muli.management_transaction import _object
from muli.store import _SESSION_RECORD_LIMIT, SQLITE_MAX_INTEGER, Store

_VERSION = 1
_SECTION = "main_recovery"
_FILTER = "recovered_interrupted_main"


class MainRecoveryEvidence:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.history = HistoryFind(store)

    def _cursor(self, token: str | None, root: str, instance: str, maximum: int) -> int | None:
        if token is None:
            return None
        if not isinstance(token, str) or not token:
            raise EvidenceError("record_cursor_invalid", "主运行恢复目录游标无效")
        if len(token) > maximum:
            raise EvidenceError("output_budget", "主运行恢复目录游标超过当前预算")
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
            if not isinstance(payload, list) or len(payload) != 6:
                raise ValueError
            version, section, database, parent, filters, position = payload
            if (
                type(version) is not int
                or version != _VERSION
                or section != _SECTION
                or database != instance
                or parent != root
                or filters != _FILTER
                or type(position) is not int
                or not 1 <= position <= SQLITE_MAX_INTEGER
                or self.store.db.execute(
                    "SELECT 1 FROM events WHERE id=? AND session_id=?", (position, root)
                ).fetchone()
                is None
            ):
                raise ValueError
            return position
        except (ValueError, TypeError, RecursionError, binascii.Error) as exc:
            raise EvidenceError(
                "record_cursor_invalid", "主运行恢复目录游标无效或不属于本资料库、会话与目录"
            ) from exc

    @staticmethod
    def _encode(root: str, instance: str, position: int) -> str:
        raw = json.dumps(
            [_VERSION, _SECTION, instance, root, _FILTER, position],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def _charge(size: object, budget: _Budget, stats: _Stats) -> bool:
        if type(size) is not int or size < 0 or size > budget.raw - stats.json_bytes:
            return False
        stats.json_bytes += size
        return True

    @staticmethod
    def _unavailable(reason: str) -> dict[str, Any]:
        return {"availability": "unavailable", "scope": "start_only", "reason": reason}

    def _first_input(
        self, root: str, run_id: str, budget: _Budget, stats: _Stats, deadline: float
    ) -> dict[str, Any]:
        _check(deadline)
        row = self.store.main_recovery_first_input(
            run_id, max_bytes=max(0, budget.raw - stats.json_bytes)
        )
        if row is None:
            return self._unavailable("missing")
        if not self._charge(row["size"], budget, stats):
            return self._unavailable("raw_budget")
        try:
            fields = {
                name: decode_utf8(row[name], allow_incomplete_tail=False)
                for name in ("id", "session_id", "state", "run_id")
                if isinstance(row[name], bytes)
            }
            payload = _object(row["payload"])
        except (EvidenceError, ValueError, TypeError, RecursionError, UnicodeError):
            return self._unavailable("invalid_input")
        if (
            fields.get("session_id") != root
            or fields.get("run_id") != run_id
            or fields.get("state") != "delivered"
            or not fields.get("id")
            or payload.get("id") != fields["id"]
            or payload.get("session_id") != root
            or any(
                not isinstance(payload.get(name), str) or not payload[name]
                for name in ("source", "principal")
            )
            or not isinstance(payload.get("content"), str)
            or not self.history._input_mapping(root, fields["id"], row["delivered_seq"])
            or type(row["seq"]) is not int
            or type(row["received"]) not in {int, float}
            or not math.isfinite(row["received"])
        ):
            return self._unavailable("invalid_mapping")
        try:
            record = self.history._scope_object("message", fields["id"], budget, stats, deadline)
        except _RawLimit:
            return self._unavailable("raw_budget")
        except EvidenceError:
            return self._unavailable("archive_changed")
        if record is None:
            return self._unavailable("missing")
        if (
            type(record.get("seq")) is not int
            or record["seq"] != row["delivered_seq"]
            or type(record.get("input_seq")) is not int
        ):
            return self._unavailable("invalid_mapping")
        try:
            expected = self.store.input_archive(
                payload, received=row["received"], run_id=run_id, input_seq=row["seq"]
            )
            actual = {
                key: value
                for key, value in record.items()
                if key not in {"seq", "evidence_revision"}
            }
            # Canonical JSON preserves types, unlike Python's True == 1 comparison.
            if json.dumps(
                actual, sort_keys=True, ensure_ascii=False, allow_nan=False
            ) != json.dumps(expected, sort_keys=True, ensure_ascii=False, allow_nan=False):
                return self._unavailable("archive_changed")
            metadata = self.history._metadata(record, record["seq"], budget.output)
        except (EvidenceError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            return self._unavailable("invalid_input")
        _check(deadline)
        if any(name not in metadata for name in ("source", "principal")):
            raise EvidenceError("output_budget", "原输入完整来源无法在当前输出预算内返回")
        return {
            "availability": "available",
            "scope": "start_only",
            "message_id": metadata["id"],
            "seq": metadata["seq"],
            "revision": metadata["revision"],
            "source": metadata["source"],
            "principal": metadata["principal"],
            "read_arguments": {
                "session_id": root,
                "message_id": metadata["id"],
                "revision": metadata["revision"],
                "offset_bytes": 0,
            },
        }

    def _item(
        self,
        root: str,
        current_run: str,
        event_id: int,
        budget: _Budget,
        stats: _Stats,
        deadline: float,
    ) -> dict[str, Any] | None:
        _check(deadline)
        event = self.store.main_recovery_event(
            root, event_id, max_bytes=max(0, budget.raw - stats.json_bytes)
        )
        if event is None or not self._charge(event["size"], budget, stats):
            return None
        if event["type"] != b"session.recovered" or not isinstance(event["data"], bytes):
            return None
        try:
            data = _object(event["data"])
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return None
        run_id = data.get("run_id")
        if (
            data.get("kind") != "run"
            or not isinstance(run_id, str)
            or not run_id
            or run_id != data.get("object_id")
            or run_id == current_run
            or type(event["created_at"]) not in {int, float}
            or not math.isfinite(event["created_at"])
        ):
            return None
        try:
            run_id.encode("utf-8", errors="strict")
        except UnicodeError:
            return None
        _check(deadline)
        run = self.store.main_recovery_run(run_id, max_bytes=max(0, budget.raw - stats.json_bytes))
        if run is None or not self._charge(run["size"], budget, stats):
            return None
        if not isinstance(run["metadata"], bytes):
            return None
        try:
            identity = _object(run["metadata"])
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return None
        if identity != {"id": run_id, "session_id": root, "status": "interrupted"}:
            return None
        return {
            "run_id": run_id,
            "status": "interrupted",
            "recovery_event_id": event_id,
            "recovered_at": event["created_at"],
            "first_input": self._first_input(root, run_id, budget, stats, deadline),
        }

    def read(
        self,
        root: str,
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
        for name, value in (
            ("limit", limit),
            ("max_output_chars", max_output_chars),
            ("max_results", max_results),
            ("max_context_chars", max_context_chars),
        ):
            validate_integer(value, name, minimum=1)
        try:
            valid_deadline = type(deadline) in {int, float} and math.isfinite(deadline)
        except OverflowError:
            valid_deadline = False
        if not valid_deadline:
            raise EvidenceError("invalid_arguments", "恢复目录需要有限的宿主绝对期限")
        raw = min(max_context_chars * 4, SQLITE_MAX_INTEGER)
        maximum = min(raw, max_output_chars * 4)
        candidates = min(limit, max_results, _SESSION_RECORD_LIMIT - 1)
        budget = _Budget(max_output_chars, raw, candidates, maximum)
        for name, identity in (
            ("session_id", root),
            ("run_id", run_id),
            ("input_id", input_id),
            ("runtime_id", runtime_id),
        ):
            _text(identity, maximum=maximum, name=name)
        if self.store.db.in_transaction:
            raise EvidenceError("main_recovery_transaction_active", "恢复目录不能接管已有事务")
        try:
            _check(deadline)
            self.store.db.execute("BEGIN")
            instance = self.history._instance(maximum)
            before = self._cursor(cursor, root, instance, maximum)
            stats = _Stats(json_bytes=len(instance.encode("utf-8")))
            if cursor is not None:
                stats.json_bytes += len(cursor.encode("utf-8"))
            self.history._owner_boundary(
                _OwnerRequest(root, run_id, input_id, runtime_id), budget, stats, deadline
            )
            rows = self.store.main_recovery_window(root, before_id=before, limit=candidates + 1)
            if not self._charge(sum(len(str(value)) for value in rows), budget, stats):
                raise EvidenceError("output_budget", "事件窗口身份超过当前原始字节预算")
            has_more = len(rows) > candidates
            result: dict[str, Any] = {
                "items": [],
                "omitted": 0,
                "has_more": has_more,
                "next_cursor": self._encode(root, instance, rows[candidates - 1])
                if has_more
                else None,
                "consistency": "live",
                "side_effect_status": "unknown",
            }
            for event_id in rows[:candidates]:
                item = self._item(root, run_id, event_id, budget, stats, deadline)
                if item is None:
                    result["omitted"] += 1
                else:
                    result["items"].append(item)
                if json_size(result) > budget.output:
                    raise EvidenceError("output_budget", "恢复目录无法在输出预算内完整返回")
            _check(deadline)
            if json_size(result) > budget.output:
                raise EvidenceError("output_budget", "恢复目录页元数据超过输出预算")
            return result
        except _RawLimit as exc:
            raise EvidenceError("output_budget", "预算不足以完整核查当前主人身份") from exc
        except _Expired as exc:
            raise EvidenceError(
                "main_recovery_timeout", "恢复目录未在原期限内完成只读核查"
            ) from exc
        except sqlite3.Error as exc:
            raise EvidenceError("main_recovery_unavailable", "恢复目录的本地只读核验失败") from exc
        finally:
            if self.store.db.in_transaction:
                self.store.db.rollback()
