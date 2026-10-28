"""Host-internal explicit excerpts; the caller authenticates pages and write authority."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from typing import Any

from muli import reading_bookmarks as bookmarks
from muli.attachment_records import _MAX_QUERY_CHARS
from muli.evidence import EvidenceError, json_size, validate_integer
from muli.store import SQLITE_MAX_INTEGER, Store

_VERSION = 1
_KIND = "reading_excerpt"
_ORDER = "reading_excerpt_save_event_v1"
_BASE_FIELDS = {
    "id",
    "schema_version",
    "store_instance",
    "main_id",
    "state",
    "generation",
    "created_event_id",
    "created_at",
}
_ACTIVE_FIELDS = _BASE_FIELDS | {"page", "quote", "note", "offset_bytes", "length_bytes"}


def _invalid() -> EvidenceError:
    return EvidenceError("excerpt_arguments_invalid", "摘录的类型、来源页或文字选择无效")


def _text(value: object, *, maximum: int, empty: bool = False) -> str:
    try:
        return bookmarks._text(value, maximum=maximum, empty=empty)
    except EvidenceError as exc:
        raise _invalid() from exc


def _integer(value: object, *, minimum: int = 0, maximum: int = SQLITE_MAX_INTEGER - 1) -> int:
    try:
        return bookmarks._integer(value, minimum=minimum, maximum=maximum)
    except EvidenceError as exc:
        raise _invalid() from exc


def _record_budget(max_output_chars: int) -> int:
    # Text is bounded by the existing output allowance; retain the bookmark metadata cap.
    return min(max_output_chars * 4 + bookmarks._MAX_RECORD_BYTES, SQLITE_MAX_INTEGER)


def _fit(value: dict[str, Any], maximum: int) -> dict[str, Any]:
    if json_size(value) > maximum:
        raise EvidenceError("output_budget", "预算不足以完整返回摘录、批注或真实续页位置")
    return value


class ReadingExcerpts:
    """Persistent, immutable cards scoped to one existing main identity.

    This service is never registered as a model-facing tool. ``page`` and ``content``
    are host-internal values returned by the caller's authenticated archive reader.
    The caller also owns Runtime authority, page delivery, file and policy checks.
    Shape, text selection, local identity, budgets and the transaction are checked here.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self._validation = bookmarks.ReadingBookmarks(store)

    def _context(self, main_id: str) -> bookmarks._Context:
        try:
            return self._validation._context(main_id)
        except EvidenceError as exc:
            raise EvidenceError(
                "excerpt_identity_invalid", "摘录需要当前资料库的真实既有主人身份"
            ) from exc

    @staticmethod
    def _identifier(value: object, context: bookmarks._Context) -> str:
        identifier = _text(value, maximum=bookmarks._MAX_ANCHOR_BYTES)
        if not identifier.startswith(context.prefix):
            raise _invalid()
        suffix = identifier[len(context.prefix) :]
        if re.fullmatch(r"[0-9]{19}", suffix) is None:
            raise _invalid()
        sequence = _integer(int(suffix), minimum=1, maximum=SQLITE_MAX_INTEGER)
        if identifier != context.order_id(sequence):
            raise _invalid()
        return identifier

    def _page(self, value: object, context: bookmarks._Context) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise _invalid()
        try:
            identifier = bookmarks._attachment_id(value.get("attachment_id"))
            page = self._validation._page(value, context, identifier)
        except EvidenceError as exc:
            if exc.code == "output_budget":
                raise
            raise _invalid() from exc
        if page["source_session_id"] != context.main and (
            page["source_agent_id"] != page["source_session_id"]
        ):
            raise _invalid()
        return page

    def _decode_record(
        self,
        record: dict[str, Any],
        context: bookmarks._Context,
        identifier: str,
        max_output_chars: int,
    ) -> dict[str, Any]:
        try:
            state = record.get("state")
            if (
                set(record) != (_ACTIVE_FIELDS if state == "active" else _BASE_FIELDS)
                or state not in {"active", "deleted"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != _VERSION
                or record["id"] != identifier
                or record["store_instance"] != context.instance
                or record["main_id"] != context.main
            ):
                raise _invalid()
            self._identifier(record["id"], context)
            sequence = _integer(record["created_event_id"], minimum=1, maximum=SQLITE_MAX_INTEGER)
            if context.order_id(sequence) != identifier:
                raise _invalid()
            bookmarks._timestamp(record["created_at"])
            generation = _integer(record["generation"], minimum=1)
            if state == "active":
                if generation != 1:
                    raise _invalid()
                page = self._page(record["page"], context)
                quote = _text(record["quote"], maximum=_record_budget(max_output_chars))
                _text(record["note"], maximum=_record_budget(max_output_chars), empty=True)
                start = _integer(record["offset_bytes"])
                length = _integer(record["length_bytes"], minimum=1)
                if (
                    "\x00" in quote
                    or length != len(quote.encode("utf-8"))
                    or not page["offset_bytes"] <= start
                    or start + length > page["next_offset_bytes"]
                ):
                    raise _invalid()
            elif generation != 2:
                raise _invalid()
        except (
            EvidenceError,
            TypeError,
            ValueError,
            KeyError,
            UnicodeError,
            RecursionError,
        ) as exc:
            raise EvidenceError(
                "excerpt_record_invalid", "持久摘录的身份、文字或定位无法完整核对，未自动修复"
            ) from exc
        return record

    def _record(
        self, context: bookmarks._Context, identifier: str, max_output_chars: int
    ) -> dict[str, Any] | None:
        record = self.store.bounded_object(
            _KIND, identifier, max_bytes=_record_budget(max_output_chars)
        )
        return (
            self._decode_record(record, context, identifier, max_output_chars)
            if record is not None
            else None
        )

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
        return {
            **result,
            "quote": record["quote"],
            "quote_kind": "source_excerpt",
            "note": record["note"],
            "note_kind": "annotation",
            "attachment_id": page["attachment_id"],
            "reader": page["reader"],
            "source_revision": page["source_revision"],
            "revision": page["revision"],
            "offset_bytes": record["offset_bytes"],
            "length_bytes": record["length_bytes"],
            "created_event_id": record["created_event_id"],
            "created_at": record["created_at"],
            "read_arguments": {
                "id": page["attachment_id"],
                "revision": page["revision"],
                "offset_bytes": record["offset_bytes"],
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

    def save(
        self,
        main_id: str,
        *,
        page: Mapping[str, object],
        content: str,
        quote: str,
        occurrence: int = 1,
        note: str = "",
        max_output_chars: int,
    ) -> dict[str, Any]:
        """Create a new card from one authenticated page; never overwrite an old card."""
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        if self.store.db.in_transaction:
            raise EvidenceError("excerpt_transaction_active", "摘录写入不能接管已有事务")
        context = self._context(main_id)
        checked = self._page(page, context)
        body = _text(content, maximum=_record_budget(max_output_chars), empty=True)
        selected = _text(quote, maximum=_record_budget(max_output_chars))
        annotation = _text(note, maximum=_record_budget(max_output_chars), empty=True)
        if (
            "\x00" in body
            or "\x00" in selected
            or len(body.encode("utf-8")) != checked["length_bytes"]
        ):
            raise _invalid()
        count = _integer(
            occurrence,
            minimum=1,
            maximum=min(bookmarks._MAX_PAGE_ITEMS, max(1, len(body))),
        )
        position = 0
        found = -1
        for _ in range(count):
            found = body.find(selected, position)
            if found < 0:
                raise EvidenceError("excerpt_quote_unavailable", "原文选择不在这一完整阅读页内")
            position = found + 1
        offset = checked["offset_bytes"] + len(body[:found].encode("utf-8"))
        length = len(selected.encode("utf-8"))
        try:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                if self._context(main_id) != context:
                    raise EvidenceError("excerpt_identity_invalid", "提交前主人或资料库身份已变化")
                try:
                    self._validation._check_attachment(checked["attachment_id"], checked)
                except EvidenceError as exc:
                    if exc.code == "output_budget":
                        raise
                    raise EvidenceError(
                        "excerpt_source_changed", "摘录来源附件的元数据不可用或已变化"
                    ) from exc
                event = self.store._event(
                    "reading_excerpt.saved",
                    {
                        "attachment_id": checked["attachment_id"],
                        "source_message_id": checked["source_message_id"],
                        "generation": 1,
                    },
                    context.main,
                )
                sequence = _integer(event["id"], minimum=1, maximum=SQLITE_MAX_INTEGER)
                identifier = context.order_id(sequence)
                record: dict[str, Any] = {
                    "id": identifier,
                    "schema_version": _VERSION,
                    "store_instance": context.instance,
                    "main_id": context.main,
                    "state": "active",
                    "generation": 1,
                    "created_event_id": sequence,
                    "created_at": bookmarks._timestamp(event["created_at"]),
                    "page": checked,
                    "quote": selected,
                    "note": annotation,
                    "offset_bytes": offset,
                    "length_bytes": length,
                }
                bookmarks._size(record, _record_budget(max_output_chars))
                response = _fit(self._response(identifier, record), max_output_chars)
                if (
                    self.store.db.execute(
                        "SELECT 1 FROM objects WHERE kind=? AND id=?", (_KIND, identifier)
                    ).fetchone()
                    is not None
                ):
                    raise EvidenceError("excerpt_record_invalid", "保存事件的摘录定位已被占用")
                self.store._put(_KIND, record)
            return response
        except sqlite3.Error as exc:
            raise EvidenceError(
                "excerpt_unavailable", "摘录事务未能完成，请先查询持久结果"
            ) from exc

    def read(self, main_id: str, identifier: str, *, max_output_chars: int) -> dict[str, Any]:
        """Read one complete historical card, without claiming current file validity."""
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        context = self._context(main_id)
        identifier = self._identifier(identifier, context)
        return _fit(
            self._response(identifier, self._record(context, identifier, max_output_chars)),
            max_output_chars,
        )

    def list(
        self,
        main_id: str,
        *,
        query: str = "",
        cursor: str | None = None,
        limit: int = 20,
        max_results: int = 100,
        max_output_chars: int,
    ) -> dict[str, Any]:
        """Seek at most the existing 200 candidates; previews are never reading pages."""
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        limit = min(
            _integer(limit, minimum=1, maximum=bookmarks._MAX_PAGE_ITEMS),
            _integer(max_results, minimum=1),
        )
        query = _text(query, maximum=_MAX_QUERY_CHARS * 4, empty=True)
        if len(query) > _MAX_QUERY_CHARS:
            raise _invalid()
        context = self._context(main_id)
        filters = bookmarks._hash([_ORDER, query])
        try:
            upper = self._validation._cursor(cursor, context, filters)
        except EvidenceError as exc:
            raise EvidenceError(
                "excerpt_cursor_invalid", "摘录游标无效、筛选不符或来自另一资料库"
            ) from exc
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
            "WHERE kind=? AND id>=? AND id<? ORDER BY id DESC LIMIT ?",
            (
                bookmarks._MAX_ANCHOR_BYTES,
                _record_budget(max_output_chars),
                _KIND,
                context.prefix,
                upper,
                bookmarks._MAX_SCANNED + 1,
            ),
        )
        with closing(rows):
            row = rows.fetchone()
            while row is not None and result["scanned"] < bookmarks._MAX_SCANNED:
                following = rows.fetchone()
                raw_anchor = row["anchor"]
                if not isinstance(raw_anchor, bytes):
                    raise EvidenceError("excerpt_record_invalid", "摘录定位键超过续页预算")
                try:
                    anchor = self._identifier(raw_anchor.decode("utf-8"), context)
                except (EvidenceError, UnicodeError) as exc:
                    raise EvidenceError("excerpt_record_invalid", "摘录定位键无效") from exc
                item: dict[str, Any] | None = None
                omitted = 0
                try:
                    raw = row["data"]
                    if not isinstance(raw, bytes):
                        raise _invalid()
                    record = json.loads(raw.decode("utf-8"))
                    if not isinstance(record, dict):
                        raise _invalid()
                    self._decode_record(record, context, anchor, max_output_chars)
                    if record["state"] == "active" and (
                        not query or query in record["quote"] or query in record["note"]
                    ):
                        item = {
                            "id": anchor,
                            "generation": record["generation"],
                            "attachment_id": record["page"]["attachment_id"],
                            "quote_preview": record["quote"][:_MAX_QUERY_CHARS],
                            "quote_kind": "source_excerpt",
                            "note_preview": record["note"][:_MAX_QUERY_CHARS],
                            "note_kind": "annotation",
                            "preview": True,
                            "created_event_id": record["created_event_id"],
                            "verification": "not_checked",
                        }
                except (EvidenceError, ValueError, TypeError, UnicodeError, RecursionError):
                    omitted = 1
                candidate = {
                    "items": [*result["items"], item] if item is not None else result["items"],
                    "scanned": result["scanned"] + 1,
                    "omitted": result["omitted"] + omitted,
                    "has_more": following is not None,
                    "next_cursor": (
                        self._validation._encode(context, filters, anchor)
                        if following is not None
                        else None
                    ),
                    "consistency": "live",
                }
                if json_size(candidate) > max_output_chars:
                    if not result["items"]:
                        raise EvidenceError("output_budget", "预算不足以完整返回摘录目录首项及游标")
                    break
                result = candidate
                if len(result["items"]) == limit:
                    break
                row = following
        return result

    def delete(
        self,
        main_id: str,
        identifier: str,
        *,
        expected_generation: int,
        max_output_chars: int,
    ) -> dict[str, Any]:
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        expected = _integer(expected_generation)
        if self.store.db.in_transaction:
            raise EvidenceError("excerpt_transaction_active", "摘录删除不能接管已有事务")
        context = self._context(main_id)
        identifier = self._identifier(identifier, context)
        try:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                if self._context(main_id) != context:
                    raise EvidenceError("excerpt_identity_invalid", "删除前主人或资料库身份已变化")
                previous = self._record(context, identifier, max_output_chars)
                if previous is None:
                    raise EvidenceError("excerpt_unavailable", "没有可删除的真实摘录")
                if previous["generation"] != expected:
                    raise EvidenceError("excerpt_generation_changed", "摘录代次已变化，请重新读取")
                if previous["state"] == "deleted":
                    return _fit(self._response(identifier, previous), max_output_chars)
                record = {key: previous[key] for key in _BASE_FIELDS}
                record.update(state="deleted", generation=2)
                response = _fit(self._response(identifier, record), max_output_chars)
                self.store._event(
                    "reading_excerpt.deleted", {"id": identifier, "generation": 2}, context.main
                )
                self.store._put(_KIND, record)
            return response
        except sqlite3.Error as exc:
            raise EvidenceError(
                "excerpt_unavailable", "摘录删除未能完成，请先查询持久结果"
            ) from exc
