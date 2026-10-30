"""Bounded, read-only notification history and exact saved-JSON byte pages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from time import monotonic
from typing import Any

from muli.evidence import EvidenceError, content_page, decode_utf8, json_size, validate_integer
from muli.management_transaction import _object
from muli.store import SQLITE_MAX_INTEGER, Store
from muli.store_schema import NOTIFICATION_RECORD_BYTES, ExistingStoreLimits

_VERSION = 1
_DOMAIN = "notification_history"
_MAX_ITEMS = 100
_OMISSIONS = ("invalid", "oversized", "status_unknown")
_Position = tuple[int | float, str]


class NotificationHistory:
    def __init__(self, store: Store) -> None:
        self.store = store

    @contextmanager
    def _reading(self, deadline: float) -> Iterator[None]:
        if type(deadline) not in {int, float} or not math.isfinite(deadline):
            raise EvidenceError("invalid_arguments", "通知读取需要有限的宿主绝对期限")
        if self.store.db.in_transaction:
            raise EvidenceError("notification_read_unavailable", "通知读取不能接管已有事务")
        maximum = ExistingStoreLimits().vm_steps
        interval = min(100, maximum)
        steps = 0
        installed = begun = False
        timeout = int(self.store.db.execute("PRAGMA busy_timeout").fetchone()[0])

        def progress() -> int:
            nonlocal steps
            steps += interval
            return int(steps >= maximum or monotonic() >= deadline)

        try:
            if monotonic() >= deadline:
                raise EvidenceError("notification_read_budget", "通知读取期限已到，未声称查全")
            self.store.db.set_progress_handler(progress, interval)
            installed = True
            remaining = max(0, int((deadline - monotonic()) * 1000))
            self.store.db.execute(f"PRAGMA busy_timeout={min(timeout, remaining)}")
            self.store.db.execute("BEGIN")
            begun = True
            yield
            if monotonic() >= deadline:
                raise EvidenceError("notification_read_budget", "通知读取超过期限，未声称查全")
        except sqlite3.Error as exc:
            raise EvidenceError(
                "notification_read_budget", "通知读取未在本地 SQLite 工作量和时间预算内完成"
            ) from exc
        finally:
            if installed:
                self.store.db.set_progress_handler(None, 0)
            if begun and self.store.db.in_transaction:
                self.store.db.rollback()
            self.store.db.execute(f"PRAGMA busy_timeout={timeout}")

    def _encode(self, status: str, position: _Position) -> str:
        raw = json.dumps(
            [_VERSION, _DOMAIN, self.store.instance_id, status, *position],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def _cursor(self, cursor: str | None, status: str, maximum: int) -> _Position | None:
        if cursor is None:
            return None
        if isinstance(cursor, str) and len(cursor) > maximum:
            raise EvidenceError("output_budget", "当前预算不足以核查完整通知目录游标")
        try:
            if not isinstance(cursor, str) or not cursor:
                raise ValueError
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(value, list) or len(value) != 6:
                raise ValueError
            version, domain, instance, original_status, created, identifier = value
            if (
                type(version) is not int
                or version != _VERSION
                or domain != _DOMAIN
                or instance != self.store.instance_id
                or original_status != status
                or type(created) not in {int, float}
                or created < 0
                or (type(created) is int and created > SQLITE_MAX_INTEGER)
                or not math.isfinite(created)
                or not isinstance(identifier, str)
                or not identifier
            ):
                raise ValueError
            identifier.encode("utf-8", errors="strict")
            return created, identifier
        except (ValueError, TypeError, OverflowError, RecursionError, binascii.Error) as exc:
            raise EvidenceError(
                "notification_cursor_invalid", "通知游标无效或不属于本次资料库和筛选，请从首页重读"
            ) from exc

    @staticmethod
    def _record(raw: Any, identifier: str) -> dict[str, Any]:
        try:
            if not isinstance(raw, bytes):
                raise ValueError
            record = _object(raw)
            if record.get("id") != identifier:
                raise ValueError
            # Validate escaped Unicode without normalizing the original bytes.
            # Legal JSON exponents may overflow Python floats; directory times
            # come from the guarded SQLite projection, never this parsed value.
            json.dumps(record, ensure_ascii=False).encode("utf-8", errors="strict")
            return record
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise EvidenceError(
                "content_unavailable", "通知原 JSON 或保存身份无法完整核查，未补造缺失字段"
            ) from exc

    def directory(
        self,
        *,
        status: str = "all",
        cursor: str | None = None,
        limit: int = 20,
        max_output_chars: int,
        max_results: int,
        max_context_chars: int,
        deadline: float,
    ) -> dict[str, Any]:
        if not isinstance(status, str) or status not in {"all", "unread"}:
            raise EvidenceError("invalid_arguments", "通知状态仅支持 all 或 unread")
        for name, value in (
            ("limit", limit),
            ("max_output_chars", max_output_chars),
            ("max_results", max_results),
            ("max_context_chars", max_context_chars),
        ):
            validate_integer(value, name, minimum=1)
        if limit > _MAX_ITEMS:
            raise EvidenceError("invalid_arguments", "通知目录每页数量必须是 1–100 的整数")
        candidates = min(max_results, _MAX_ITEMS)
        raw_budget = min(max_context_chars * 4, NOTIFICATION_RECORD_BYTES)
        key_budget = min(max_output_chars * 4, SQLITE_MAX_INTEGER)
        items: list[dict[str, Any]] = []
        scanned = json_bytes = 0
        omitted = dict.fromkeys(_OMISSIONS, 0)
        with self._reading(deadline):
            position = self._cursor(cursor, status, key_budget)
            if not self.store.notification_history_index():
                raise EvidenceError(
                    "notification_directory_unavailable",
                    "通知历史索引未准备或不符合声明；仍可按准确 ID 核查原文",
                )

            def packet(reason: str, more: bool) -> dict[str, Any]:
                return {
                    "items": items,
                    "has_more": more,
                    "next_cursor": self._encode(status, position)
                    if more and position is not None
                    else None,
                    "scanned": scanned,
                    "json_bytes": json_bytes,
                    "omitted": dict(omitted),
                    "stopped": reason,
                    "consistency": "bounded_live",
                }

            def finish(reason: str, more: bool = True) -> dict[str, Any]:
                result = packet(reason, more)
                if json_size(result) > max_output_chars:
                    raise EvidenceError(
                        "output_budget", "预算不足以完整返回通知目录、遗漏和真实游标"
                    )
                return result

            def room(after: _Position, selected: list[dict[str, Any]]) -> int:
                shell = {
                    "items": selected,
                    "has_more": True,
                    "next_cursor": self._encode(status, after),
                    "scanned": candidates,
                    "json_bytes": raw_budget,
                    "omitted": dict.fromkeys(_OMISSIONS, candidates),
                    "stopped": "candidate_budget",
                    "consistency": "bounded_live",
                }
                return max_output_chars - json_size(shell)

            with closing(
                self.store.notification_directory_rows(
                    after=position, limit=candidates + 1, key_bytes=key_budget
                )
            ) as rows:
                for row in rows:
                    if len(items) == limit:
                        return finish("limit")
                    if scanned == candidates:
                        return finish("candidate_budget")
                    if monotonic() >= deadline:
                        raise EvidenceError(
                            "notification_read_budget", "通知读取期限已到，未声称查全"
                        )
                    if not isinstance(row["id"], bytes):
                        raise EvidenceError(
                            "output_budget", "原始通知 ID 超过预算，未截断 ID 生成游标"
                        )
                    identifier = decode_utf8(row["id"], allow_incomplete_tail=False)
                    if not identifier:
                        raise EvidenceError("content_unavailable", "原始通知缺少可续读的 ID")
                    # Use the actual indexed SQLite value: large JSON integers
                    # can be projected as REAL and cannot be rebound as Python ints.
                    after = (row["sort_created_at"], identifier)
                    if room(after, items) < 0:
                        if not scanned:
                            raise EvidenceError(
                                "output_budget", "预算不足以完整返回通知 ID 和续页游标"
                            )
                        return finish("output_budget")
                    stored = self.store.notification_record_bytes(identifier, max_bytes=1)
                    if stored is None or stored["storage_type"] != "text" or stored["size"] <= 0:
                        omitted["invalid"] += 1
                    elif stored["size"] > raw_budget:
                        omitted["oversized"] += 1
                    else:
                        size = stored["size"]
                        if size > raw_budget - json_bytes:
                            return finish("raw_budget")
                        stored = self.store.notification_record_bytes(identifier, max_bytes=size)
                        json_bytes += size
                        try:
                            if stored is None or not isinstance(stored["raw"], bytes):
                                raise EvidenceError("content_unavailable", "通知原文无法完整读取")
                            raw: bytes = stored["raw"]
                            record = self._record(raw, identifier)
                            if any(
                                record.get(field) is not None and not isinstance(record[field], str)
                                for field in ("title", "source")
                            ):
                                raise EvidenceError("content_unavailable", "通知摘要不是有效文本")
                        except EvidenceError:
                            omitted["invalid"] += 1
                        else:
                            read = record.get("read")
                            read = read if type(read) is bool else None
                            if status == "unread" and read is None:
                                omitted["status_unknown"] += 1
                            elif status == "all" or read is False:
                                item = {
                                    "id": identifier,
                                    "title": record.get("title"),
                                    "source": record.get("source"),
                                    "created_at": row["sort_created_at"] or None,
                                    "read": read,
                                    "revision": hashlib.sha256(raw).hexdigest(),
                                }
                                if room(after, [*items, item]) < 0:
                                    if items:
                                        return finish("output_budget")
                                    omitted["oversized"] += 1
                                else:
                                    items.append(item)
                    position = after
                    scanned += 1
            return finish("end", False)

    def read(
        self,
        identifier: str,
        *,
        offset_bytes: int = 0,
        length_bytes: int | None = None,
        revision: str | None = None,
        max_output_chars: int,
        max_context_chars: int,
        deadline: float,
    ) -> dict[str, Any]:
        if not isinstance(identifier, str) or not identifier:
            raise EvidenceError("invalid_arguments", "需要准确的通知 ID")
        try:
            identifier.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise EvidenceError("invalid_arguments", "通知 ID 包含无效 Unicode") from exc
        for name, value in (
            ("offset_bytes", offset_bytes),
            ("max_output_chars", max_output_chars),
            ("max_context_chars", max_context_chars),
        ):
            validate_integer(value, name, minimum=0 if name == "offset_bytes" else 1)
        if length_bytes is not None:
            validate_integer(length_bytes, "length_bytes", minimum=1)
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise EvidenceError("invalid_arguments", "revision 必须是非空字符串")
        if offset_bytes and revision is None:
            raise EvidenceError("invalid_arguments", "续读必须提供上一页 revision")
        with self._reading(deadline):
            row = self.store.notification_record_bytes(
                identifier, max_bytes=min(max_context_chars * 4, NOTIFICATION_RECORD_BYTES)
            )
            if row is None:
                raise EvidenceError("evidence_not_found", "通知不存在或已删除")
            if row["size"] > min(max_context_chars * 4, NOTIFICATION_RECORD_BYTES):
                raise EvidenceError(
                    "notification_record_oversized",
                    "保存通知的完整 JSON 超过本次核查字节预算，未返回伪完整正文",
                )
            self._record(row["raw"], identifier)
            raw = row["raw"]
            current = hashlib.sha256(raw).hexdigest()
            if revision is not None and revision != current:
                raise EvidenceError("evidence_changed", "保存通知已变化；请从零重新核查当前版本")
            maximum = min(length_bytes or max_output_chars * 4, max_output_chars * 4)
            return content_page(
                {"id": identifier, "revision": current, "format": "json"},
                raw[offset_bytes : offset_bytes + maximum],
                offset_bytes=offset_bytes,
                total_bytes=len(raw),
                max_output_chars=max_output_chars,
            )
