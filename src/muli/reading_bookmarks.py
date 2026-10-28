"""Finite explicit bookmarks; authority and authentic page receipts belong to the caller."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Literal

from muli.attachments import ATTACHMENT_ID_PATTERN
from muli.evidence import EvidenceError, json_size, validate_integer
from muli.store import SQLITE_MAX_INTEGER, Store, now
from muli.store_schema import read_existing_main

_VERSION = 1
_MAX_RECORD_BYTES = 4096
_MAX_PAGE_BYTES = 2048
_MAX_ORDER_BYTES = 1024
_MAX_METADATA_BYTES = 4096
_MAX_ID_BYTES = 96
_MAX_ANCHOR_BYTES = 128
_MAX_CURSOR_CHARS = 512
_MAX_PAGE_ITEMS = 100
_MAX_SCANNED = 200
_ORDER = "last_set_event_v1"
_PAGE_FIELDS = {
    "store_instance",
    "main_id",
    "attachment_id",
    "reader",
    "source_revision",
    "revision",
    "offset_bytes",
    "length_bytes",
    "next_offset_bytes",
    "total_bytes",
    "has_more",
    "source_session_id",
    "source_agent_id",
    "source_run_id",
    "source_generation",
    "source_message_id",
    "source_message_revision",
}
_BASE_FIELDS = {
    "id",
    "schema_version",
    "store_instance",
    "main_id",
    "attachment_id",
    "generation",
    "state",
}
_ACTIVE_FIELDS = _BASE_FIELDS | {"page", "position", "last_set_event_id", "updated_at"}
_ORDER_FIELDS = {
    "id",
    "schema_version",
    "store_instance",
    "main_id",
    "bookmark_id",
    "attachment_id",
    "generation",
    "event_id",
}


def _invalid() -> EvidenceError:
    return EvidenceError("bookmark_arguments_invalid", "书签参数的类型、身份或页定位无效")


def _integer(value: object, *, minimum: int = 0, maximum: int = SQLITE_MAX_INTEGER - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _text(value: object, *, maximum: int = _MAX_ID_BYTES, empty: bool = False) -> str:
    if not isinstance(value, str) or not int(not empty) <= len(value) <= maximum:
        raise _invalid()
    try:
        if len(value.encode("utf-8", errors="strict")) > maximum:
            raise _invalid()
    except UnicodeError as exc:
        raise _invalid() from exc
    return value


def _attachment_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(ATTACHMENT_ID_PATTERN, value) is None:
        raise _invalid()
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise _invalid()
    return value


def _timestamp(value: object) -> float:
    if type(value) is not int and type(value) is not float:
        raise _invalid()
    try:
        result = float(value)
        if result < 0 or not math.isfinite(result):
            raise _invalid()
        return result
    except (OverflowError, ValueError) as exc:
        raise _invalid() from exc


def _size(value: dict[str, Any], maximum: int) -> None:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > maximum:
            raise EvidenceError("output_budget", "完整书签元数据超过核查字节预算")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise _invalid() from exc


def _fit(value: dict[str, Any], maximum: int) -> dict[str, Any]:
    if json_size(value) > maximum:
        raise EvidenceError("output_budget", "预算不足以完整返回书签及真实续页位置")
    return value


def _hash(values: list[str]) -> str:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Context:
    instance: str
    main: str

    @property
    def prefix(self) -> str:
        return _hash([self.instance, self.main]) + ":"

    def bookmark_id(self, attachment_id: str) -> str:
        return _hash([self.instance, self.main, attachment_id])

    def order_id(self, event_id: int) -> str:
        return f"{self.prefix}{event_id:019d}"


class ReadingBookmarks:
    """Host-internal persistence, never a replacement for Runtime or file guards.

    `set` accepts only metadata already authenticated by its caller. This layer
    checks shape, local identities, current attachment metadata and atomic CAS;
    it does not establish page delivery, root lineage or current owner authority.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def _context(self, main_id: str) -> _Context:
        main = _text(main_id)
        try:
            if self.store.existing_main_session_id() != main:
                raise ValueError
            read_existing_main(self.store.db, main)
            row = self.store.db.execute(
                "SELECT CASE WHEN typeof(value)='text' "
                "AND length(CAST(value AS BLOB)) BETWEEN 1 AND ? THEN value END "
                "FROM runtime_state WHERE name='store_instance_v1'",
                (_MAX_ID_BYTES,),
            ).fetchone()
            if row is None:
                raise ValueError
            instance = _text(row[0])
        except (ValueError, sqlite3.Error, UnicodeError) as exc:
            raise EvidenceError(
                "bookmark_identity_invalid", "书签需要当前资料库的真实既有主人身份"
            ) from exc
        return _Context(instance, main)

    @staticmethod
    def _page(value: object, context: _Context, identifier: str) -> dict[str, Any]:
        if not isinstance(value, Mapping) or len(value) != len(_PAGE_FIELDS):
            raise _invalid()
        if set(value) != _PAGE_FIELDS:
            raise _invalid()
        page = dict(value)
        if (
            _text(page["store_instance"]) != context.instance
            or _text(page["main_id"]) != context.main
            or _attachment_id(page["attachment_id"]) != identifier
            or not isinstance(page["reader"], str)
            or page["reader"] not in {"attachment_read", "document_read"}
        ):
            raise _invalid()
        for field in ("source_revision", "revision"):
            _digest(page[field])
        for field in ("offset_bytes", "length_bytes", "next_offset_bytes", "total_bytes"):
            _integer(page[field])
        if (
            type(page["has_more"]) is not bool
            or page["next_offset_bytes"] != page["offset_bytes"] + page["length_bytes"]
            or page["next_offset_bytes"] > page["total_bytes"]
            or page["has_more"] != (page["next_offset_bytes"] < page["total_bytes"])
            or (page["has_more"] and page["length_bytes"] == 0)
            or (page["reader"] == "attachment_read" and page["source_revision"] != page["revision"])
        ):
            raise _invalid()
        for field in (
            "source_session_id",
            "source_run_id",
            "source_message_id",
            "source_message_revision",
        ):
            _text(page[field])
        if page["source_session_id"] == context.main:
            if page["source_agent_id"] is not None or page["source_generation"] is not None:
                raise _invalid()
        else:
            _text(page["source_agent_id"])
            _integer(page["source_generation"])
        _size(page, _MAX_PAGE_BYTES)
        return page

    def _attachment(self, identifier: str) -> dict[str, Any] | None:
        item = self.store.bounded_object("attachment", identifier, max_bytes=_MAX_METADATA_BYTES)
        if item is None:
            return None
        if _attachment_id(item.get("id")) != identifier:
            raise _invalid()
        _text(item.get("name"), maximum=240)
        _text(item.get("source"), maximum=240, empty=True)
        _text(item.get("mime_type"), maximum=240, empty=True)
        _integer(item.get("size"))
        _digest(item.get("sha256"))
        return item

    def _check_attachment(self, identifier: str, page: dict[str, Any] | None) -> None:
        item = self._attachment(identifier)
        if item is None:
            raise EvidenceError("attachment_unavailable", "书签对应的真实附件元数据不可用")
        if page is not None and (
            item["source"] in {"asr", "vision"}
            or item["sha256"] != page["source_revision"]
            or (page["reader"] == "attachment_read" and item["size"] != page["total_bytes"])
        ):
            raise EvidenceError("revision_changed", "附件元数据与已核阅读页不一致")

    @staticmethod
    def _order(context: _Context, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": context.order_id(record["last_set_event_id"]),
            "schema_version": _VERSION,
            "store_instance": context.instance,
            "main_id": context.main,
            "bookmark_id": record["id"],
            "attachment_id": record["attachment_id"],
            "generation": record["generation"],
            "event_id": record["last_set_event_id"],
        }

    @staticmethod
    def _validate_order(value: dict[str, Any], context: _Context) -> None:
        if (
            set(value) != _ORDER_FIELDS
            or type(value["schema_version"]) is not int
            or value["schema_version"] != _VERSION
            or value["store_instance"] != context.instance
            or value["main_id"] != context.main
        ):
            raise _invalid()
        identifier = _attachment_id(value["attachment_id"])
        sequence = _integer(value["event_id"], minimum=1, maximum=SQLITE_MAX_INTEGER)
        _integer(value["generation"], minimum=1)
        if value["id"] != context.order_id(sequence) or value["bookmark_id"] != context.bookmark_id(
            identifier
        ):
            raise _invalid()

    def _record(self, context: _Context, identifier: str) -> dict[str, Any] | None:
        record = self.store.bounded_object(
            "reading_bookmark", context.bookmark_id(identifier), max_bytes=_MAX_RECORD_BYTES
        )
        if record is None:
            return None
        try:
            state = record.get("state")
            fields = _ACTIVE_FIELDS if state == "active" else _BASE_FIELDS
            if (
                set(record) != fields
                or state not in {"active", "deleted"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != _VERSION
                or record["id"] != context.bookmark_id(identifier)
                or record["store_instance"] != context.instance
                or record["main_id"] != context.main
                or record["attachment_id"] != identifier
            ):
                raise _invalid()
            _integer(record["generation"], minimum=1)
            if state == "active":
                self._page(record["page"], context, identifier)
                if record["position"] not in {"start", "next"}:
                    raise _invalid()
                _timestamp(record["updated_at"])
                _integer(record["last_set_event_id"], minimum=1, maximum=SQLITE_MAX_INTEGER)
                expected = self._order(context, record)
                order = self.store.bounded_object(
                    "reading_bookmark_order", expected["id"], max_bytes=_MAX_ORDER_BYTES
                )
                if order is None:
                    raise _invalid()
                self._validate_order(order, context)
                if order != expected:
                    raise _invalid()
            return record
        except (EvidenceError, TypeError, KeyError) as exc:
            raise EvidenceError(
                "bookmark_record_invalid", "持久书签或活动定位不一致，未自动修复"
            ) from exc

    @staticmethod
    def _response(identifier: str, record: dict[str, Any] | None) -> dict[str, Any]:
        result = {
            "id": identifier,
            "state": record["state"] if record is not None else "empty",
            "generation": record["generation"] if record is not None else 0,
            "verification": "not_checked",
        }
        if record is None or record["state"] != "active":
            return result
        page = record["page"]
        offset = page["offset_bytes" if record["position"] == "start" else "next_offset_bytes"]
        return {
            **result,
            "reader": page["reader"],
            "source_revision": page["source_revision"],
            "revision": page["revision"],
            "offset_bytes": offset,
            "total_bytes": page["total_bytes"],
            "eof": offset == page["total_bytes"],
            "last_set_event_id": record["last_set_event_id"],
            "updated_at": record["updated_at"],
            "read_arguments": {
                "id": identifier,
                "revision": page["revision"],
                "offset_bytes": offset,
            },
            "source_page": {
                "session_id": page["source_session_id"],
                "agent_id": page["source_agent_id"],
                "run_id": page["source_run_id"],
                "generation": page["source_generation"],
                "message_id": page["source_message_id"],
                "message_revision": page["source_message_revision"],
            },
        }

    def read(self, main_id: str, attachment_id: str, *, max_output_chars: int) -> dict[str, Any]:
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        identifier = _attachment_id(attachment_id)
        context = self._context(main_id)
        return _fit(self._response(identifier, self._record(context, identifier)), max_output_chars)

    @staticmethod
    def _anchor(value: object, context: _Context) -> str:
        anchor = _text(value, maximum=_MAX_ANCHOR_BYTES)
        if not context.prefix <= anchor < context.prefix[:-1] + ";":
            raise _invalid()
        return anchor

    @staticmethod
    def _encode(context: _Context, filters: str, anchor: str) -> str:
        value = [_VERSION, context.prefix, filters, anchor]
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        token = base64.urlsafe_b64encode(raw).decode("ascii")
        if len(token) > _MAX_CURSOR_CHARS:
            raise EvidenceError("bookmark_cursor_invalid", "书签续页位置超过有界游标预算")
        return token

    def _cursor(self, value: str | None, context: _Context, filters: str) -> str:
        if value is None:
            return context.prefix[:-1] + ";"
        try:
            if not isinstance(value, str) or not 1 <= len(value) <= _MAX_CURSOR_CHARS:
                raise ValueError
            payload = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
            if (
                not isinstance(payload, list)
                or len(payload) != 4
                or type(payload[0]) is not int
                or payload[:3] != [_VERSION, context.prefix, filters]
            ):
                raise ValueError
            anchor = self._anchor(payload[3], context)
            if self._encode(context, filters, anchor) != value:
                raise ValueError
            return anchor
        except (ValueError, TypeError, RecursionError, UnicodeError, binascii.Error) as exc:
            raise EvidenceError(
                "bookmark_cursor_invalid", "书签游标无效、筛选不匹配或来自另一资料库；请从首页读取"
            ) from exc

    def list(
        self,
        main_id: str,
        *,
        name_query: str = "",
        cursor: str | None = None,
        limit: int = 20,
        max_results: int = 100,
        max_output_chars: int,
    ) -> dict[str, Any]:
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        limit = min(
            _integer(limit, minimum=1, maximum=_MAX_PAGE_ITEMS),
            _integer(max_results, minimum=1),
        )
        query = _text(name_query, maximum=120 * 4, empty=True)
        if len(query) > 120:
            raise _invalid()
        context = self._context(main_id)
        filters = _hash([_ORDER, query])
        upper = self._cursor(cursor, context, filters)
        result: dict[str, Any] = {
            "items": [],
            "scanned": 0,
            "omitted": 0,
            "has_more": False,
            "next_cursor": None,
            "consistency": "live",
        }
        _fit(result, max_output_chars)
        rows = self.store.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))<=? "
            "THEN CAST(id AS BLOB) END AS anchor,"
            "CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CAST(data AS BLOB) END AS data FROM objects "
            "WHERE kind='reading_bookmark_order' AND id>=? AND id<? ORDER BY id DESC LIMIT ?",
            (_MAX_ANCHOR_BYTES, _MAX_ORDER_BYTES, context.prefix, upper, _MAX_SCANNED + 1),
        )
        with closing(rows):
            row = rows.fetchone()
            while row is not None and result["scanned"] < _MAX_SCANNED:
                following = rows.fetchone()
                raw_anchor = row["anchor"]
                if not isinstance(raw_anchor, bytes):
                    raise EvidenceError("bookmark_record_invalid", "书签定位键超过有界续页预算")
                try:
                    anchor = self._anchor(raw_anchor.decode("utf-8"), context)
                except (UnicodeError, EvidenceError) as exc:
                    raise EvidenceError(
                        "bookmark_record_invalid", "书签定位键无法完整核查"
                    ) from exc
                item: dict[str, Any] | None = None
                omitted = 0
                try:
                    raw = row["data"]
                    if not isinstance(raw, bytes):
                        raise _invalid()
                    order = json.loads(raw)
                    if not isinstance(order, dict) or order.get("id") != anchor:
                        raise _invalid()
                    self._validate_order(order, context)
                    identifier = order["attachment_id"]
                    record = self._record(context, identifier)
                    if (
                        record is None
                        or record["state"] != "active"
                        or self._order(context, record) != order
                    ):
                        raise _invalid()
                    try:
                        metadata = self._attachment(identifier)
                    except EvidenceError:
                        metadata = None
                    name = metadata["name"] if metadata is not None else None
                    if not query or (name is not None and query in name):
                        item = {
                            **self._response(identifier, record),
                            "name": name,
                            "metadata_available": metadata is not None,
                        }
                except (EvidenceError, TypeError, ValueError, UnicodeError, RecursionError):
                    omitted = 1
                candidate = {
                    "items": [*result["items"], item] if item is not None else result["items"],
                    "scanned": result["scanned"] + 1,
                    "omitted": result["omitted"] + omitted,
                    "has_more": following is not None,
                    "next_cursor": (
                        self._encode(context, filters, anchor) if following is not None else None
                    ),
                    "consistency": "live",
                }
                if json_size(candidate) > max_output_chars:
                    if not result["items"]:
                        raise EvidenceError("output_budget", "预算不足以完整返回书签和真实续页游标")
                    break
                result = candidate
                if len(result["items"]) == limit:
                    break
                row = following
        return result

    def _write(
        self,
        main_id: str,
        identifier: str,
        *,
        page: Mapping[str, object] | None,
        position: Literal["start", "next"],
        expected_generation: int,
        max_output_chars: int,
    ) -> dict[str, Any]:
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        expected = _integer(expected_generation)
        identifier = _attachment_id(identifier)
        if self.store.db.in_transaction:
            raise EvidenceError("bookmark_transaction_active", "书签写入不能接管已有事务")
        context = self._context(main_id)
        checked_page = self._page(page, context, identifier) if page is not None else None
        if not isinstance(position, str) or position not in {"start", "next"}:
            raise _invalid()
        try:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                if self._context(main_id) != context:
                    raise EvidenceError("bookmark_identity_invalid", "书签提交前资料库身份已变化")
                previous = self._record(context, identifier)
                generation = previous["generation"] if previous is not None else 0
                if generation != expected:
                    raise EvidenceError(
                        "bookmark_generation_changed", "书签代次已变化，请重新读取后决定"
                    )
                if generation >= SQLITE_MAX_INTEGER - 1:
                    raise EvidenceError("bookmark_generation_exhausted", "书签代次已耗尽，未回绕")
                if checked_page is not None or previous is None:
                    self._check_attachment(identifier, checked_page)
                record: dict[str, Any] = {
                    "id": context.bookmark_id(identifier),
                    "schema_version": _VERSION,
                    "store_instance": context.instance,
                    "main_id": context.main,
                    "attachment_id": identifier,
                    "generation": generation + 1,
                    "state": "active" if checked_page is not None else "deleted",
                }
                event = self.store._event(
                    "reading_bookmark.set"
                    if checked_page is not None
                    else "reading_bookmark.deleted",
                    {"id": identifier, "bookmark_id": record["id"], "generation": generation + 1},
                    context.main,
                )
                if checked_page is not None:
                    sequence = _integer(event["id"], minimum=1, maximum=SQLITE_MAX_INTEGER)
                    record.update(
                        page=checked_page,
                        position=position,
                        last_set_event_id=sequence,
                        updated_at=_timestamp(now()),
                    )
                _size(record, _MAX_RECORD_BYTES)
                response = _fit(self._response(identifier, record), max_output_chars)
                self.store._put("reading_bookmark", record)
                if previous is not None and previous["state"] == "active":
                    removed = self.store.db.execute(
                        "DELETE FROM objects WHERE kind='reading_bookmark_order' AND id=?",
                        (context.order_id(previous["last_set_event_id"]),),
                    )
                    if removed.rowcount != 1:
                        raise EvidenceError("bookmark_record_invalid", "书签旧定位已变化")
                if checked_page is not None:
                    order = self._order(context, record)
                    _size(order, _MAX_ORDER_BYTES)
                    occupied = self.store.db.execute(
                        "SELECT 1 FROM objects WHERE kind='reading_bookmark_order' AND id=?",
                        (order["id"],),
                    ).fetchone()
                    if occupied is not None:
                        raise EvidenceError("bookmark_record_invalid", "书签新定位已被占用")
                    self.store._put("reading_bookmark_order", order)
            return response
        except sqlite3.Error as exc:
            raise EvidenceError(
                "bookmark_unavailable", "书签事务未能完成，请先重新读取持久结果"
            ) from exc

    def set(
        self,
        main_id: str,
        attachment_id: str,
        *,
        page: Mapping[str, object],
        position: Literal["start", "next"],
        expected_generation: int,
        max_output_chars: int,
    ) -> dict[str, Any]:
        if not isinstance(page, Mapping):
            raise _invalid()
        return self._write(
            main_id,
            attachment_id,
            page=page,
            position=position,
            expected_generation=expected_generation,
            max_output_chars=max_output_chars,
        )

    def delete(
        self,
        main_id: str,
        attachment_id: str,
        *,
        expected_generation: int,
        max_output_chars: int,
    ) -> dict[str, Any]:
        return self._write(
            main_id,
            attachment_id,
            page=None,
            position="start",
            expected_generation=expected_generation,
            max_output_chars=max_output_chars,
        )
