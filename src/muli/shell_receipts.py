"""Bounded, versioned evidence from normally settled shells; never processes."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections import deque
from contextlib import closing
from typing import Any

from .evidence import json_size
from .management_transaction import _object
from .shell_sessions import (
    OutputCursor,
    ShellSessionError,
    ShellSessions,
    StreamName,
    _Command,
    _Output,
    _read_cursor,
    _receipt_page,
)
from .store import SQLITE_MAX_INTEGER, Store

RECEIPT_KIND = "shell_receipt"
RECEIPT_EVENT = "shell.receipt_stored"
RECEIPT_VERSION = 1
MAX_RAW_BYTES = 100000
MAX_METADATA_BYTES = 512 * 1024
MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
_STATES = {"completed", "stopped", "timed_out", "unknown"}
_METADATA_FIELDS = {"os_epoch", "executable", "flavor", "state", "exit_code", "status_basis", "cwd"}
_RECORD_FIELDS = {"id", "version", "session_id", "command_id", "event_id", "metadata", "output"}
_OUTPUT_FIELDS = {
    "observed",
    "retained",
    "discarded",
    "observed_by_stream",
    "discarded_by_stream",
    "chunks",
}
_STREAMS: tuple[StreamName, StreamName] = ("output", "stderr")


def receipt_key(session_id: str, command_id: str) -> str:
    return json.dumps([session_id, command_id], ensure_ascii=True, separators=(",", ":"))


def _count(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= SQLITE_MAX_INTEGER:
        raise ValueError("invalid shell evidence count")
    return value


def _cursor(value: Any) -> OutputCursor:
    if not isinstance(value, dict) or set(value) != set(_STREAMS):
        raise ValueError("invalid shell evidence cursor")
    return {"output": _count(value["output"]), "stderr": _count(value["stderr"])}


def _json_bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _metadata(record: dict[str, Any], session_id: str, command_id: str) -> dict[str, Any]:
    if (
        set(record) != _RECORD_FIELDS - {"output"}
        or type(record["version"]) is not int
        or record["version"] != RECEIPT_VERSION
        or record["id"] != receipt_key(session_id, command_id)
        or record["session_id"] != session_id
        or record["command_id"] != command_id
        or not _IDENTIFIER.fullmatch(command_id)
    ):
        raise ValueError("unsupported shell evidence")
    _count(record["event_id"], minimum=1)
    if _json_bytes(record) > MAX_METADATA_BYTES:
        raise ValueError("shell evidence metadata exceeds format bound")
    metadata = record["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_FIELDS:
        raise ValueError("invalid shell evidence metadata")
    for key in ("os_epoch", "executable", "flavor", "state", "status_basis", "cwd"):
        value = metadata[key]
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("invalid shell evidence text")
        value.encode("utf-8", errors="strict")
    if (
        not _IDENTIFIER.fullmatch(metadata["os_epoch"])
        or metadata["flavor"] not in {"sh", "bash", "zsh", "fish", "pwsh", "cmd"}
        or metadata["state"] not in _STATES
        or metadata["status_basis"]
        != ("powershell_success" if metadata["flavor"] == "pwsh" else "shell_status")
    ):
        raise ValueError("invalid shell evidence terminal state")
    exit_code = metadata["exit_code"]
    if metadata["state"] == "completed":
        if type(exit_code) is not int or not -(2**31) <= exit_code < 2**32:
            raise ValueError("invalid shell evidence exit status")
    elif exit_code is not None:
        raise ValueError("unfinished shell evidence cannot certify an exit")
    return metadata


def _decode(
    record: dict[str, Any], session_id: str, command_id: str
) -> tuple[dict[str, Any], _Output]:
    if set(record) != _RECORD_FIELDS:
        raise ValueError("unsupported shell evidence")
    metadata = _metadata(
        {key: value for key, value in record.items() if key != "output"}, session_id, command_id
    )
    body = record["output"]
    if not isinstance(body, dict) or set(body) != _OUTPUT_FIELDS:
        raise ValueError("invalid shell evidence output")
    observed = _count(body["observed"])
    discarded = _count(body["discarded"])
    retained = _count(body["retained"])
    observed_streams = _cursor(body["observed_by_stream"])
    discarded_streams = _cursor(body["discarded_by_stream"])
    chunks = body["chunks"]
    if (
        retained > MAX_RAW_BYTES
        or observed != sum(observed_streams[stream] for stream in _STREAMS)
        or discarded != sum(discarded_streams[stream] for stream in _STREAMS)
        or observed != retained + discarded
        or any(discarded_streams[name] > observed_streams[name] for name in _STREAMS)
        or not isinstance(chunks, list)
        or len(chunks) > retained
    ):
        raise ValueError("inconsistent shell evidence counts")
    positions = discarded_streams.copy()
    decoded: deque[tuple[int, StreamName, bytes]] = deque()
    total = 0
    for chunk in chunks:
        if not isinstance(chunk, list) or len(chunk) != 3:
            raise ValueError("invalid shell evidence chunk")
        offset, name, encoded = chunk
        _count(offset)
        if (
            not isinstance(name, str)
            or name not in _STREAMS
            or not isinstance(encoded, str)
            or not encoded.isascii()
            or not encoded
            or len(encoded) > 4 * ((retained - total + 2) // 3)
        ):
            raise ValueError("invalid shell evidence encoding")
        stream = name
        raw = base64.b64decode(encoded, validate=True)
        if (
            not raw
            or base64.b64encode(raw).decode("ascii") != encoded
            or offset != positions[stream]
            or total + len(raw) > retained
            or positions[stream] + len(raw) > observed_streams[stream]
        ):
            raise ValueError("noncontiguous shell evidence output")
        positions[stream] += len(raw)
        total += len(raw)
        decoded.append((offset, stream, raw))
    if total != retained or positions != observed_streams:
        raise ValueError("incomplete shell evidence output")
    return metadata, _Output(
        chunks=decoded,
        observed=observed,
        retained=retained,
        discarded=discarded,
        observed_by_stream=observed_streams,
        discarded_by_stream=discarded_streams,
    )


def _pack(item: _Command, event_id: int, output_limit: int) -> dict[str, Any]:
    if (
        item.shell.state != "closed"
        or item.shell.cleanup_failed
        or not item.done.is_set()
        or item.state not in _STATES
        or (
            item.state == "completed"
            and not (
                item.proof.is_set() and item.stdout_fence.is_set() and item.stderr_fence.is_set()
            )
        )
    ):
        raise ShellSessionError("cleanup_unknown", "shell 未建立可归档的已收口终态")
    item.output.trim(min(MAX_RAW_BYTES, max(1, output_limit)))
    live = ShellSessions._receipt_metadata(item, {"output": 0, "stderr": 0})
    record: dict[str, Any] = {
        "id": receipt_key(item.shell.session_id, item.id),
        "version": RECEIPT_VERSION,
        "session_id": item.shell.session_id,
        "command_id": item.id,
        "event_id": event_id,
        "metadata": {key: live[key] for key in _METADATA_FIELDS},
    }
    metadata_bytes = _json_bytes(record)
    # Nonempty chunks are at most raw bytes. Each chunk needs <=48 bytes for
    # offset/name/JSON punctuation; canonical Base64 uses at most 4*raw bytes.
    worst_bytes = metadata_bytes + 48 * len(item.output.chunks) + 4 * item.output.retained + 2048
    if metadata_bytes > MAX_METADATA_BYTES or worst_bytes > MAX_ENVELOPE_BYTES:
        raise ValueError("shell evidence exceeds format envelope")
    record["output"] = {
        "observed": item.output.observed,
        "retained": item.output.retained,
        "discarded": item.output.discarded,
        "observed_by_stream": item.output.observed_by_stream.copy(),
        "discarded_by_stream": item.output.discarded_by_stream.copy(),
        "chunks": [
            [offset, stream, base64.b64encode(raw).decode("ascii")]
            for offset, stream, raw in item.output.chunks
        ],
    }
    if _json_bytes(record) > MAX_ENVELOPE_BYTES:
        raise ValueError("shell evidence exceeds format envelope")
    _decode(record, item.shell.session_id, item.id)
    return record


class ShellReceipts:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _session_exists(self, session_id: str) -> bool:
        return (
            self.store.db.execute(
                "SELECT 1 FROM objects WHERE kind='session' AND id=?", (session_id,)
            ).fetchone()
            is not None
        )

    def archive(self, shells: ShellSessions, *, max_records: int, output_limit: int) -> None:
        """One synchronous transaction after real shell settlement, before Store close."""
        if not shells._closed or shells.live_sessions():
            raise ShellSessionError("cleanup_unknown", "shell 管理器尚未完成收口")
        _count(max_records, minimum=1)
        wrote = False
        with self.store.db:
            for item in shells._commands.values():
                if not self._session_exists(item.shell.session_id):
                    continue
                key = receipt_key(item.shell.session_id, item.id)
                self.store.db.execute(
                    "DELETE FROM events WHERE type=? AND session_id=? "
                    "AND CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
                    "THEN CASE WHEN json_valid(data) THEN json_extract(data,'$.id') END END=?",
                    (RECEIPT_EVENT, item.shell.session_id, MAX_METADATA_BYTES, key),
                )
                event = self.store._event(
                    RECEIPT_EVENT, {"id": key, "source": RECEIPT_KIND}, item.shell.session_id
                )
                self.store._put(RECEIPT_KIND, _pack(item, event["id"], output_limit))
                wrote = True
            if wrote:
                self._prune(max_records)

    def _prune(self, maximum: int) -> None:
        # Only identities and integer positions leave SQLite, one victim at a
        # time. Canonical AUTOINCREMENT events order both closes and one batch.
        while True:
            victim = self.store.db.execute(
                "SELECT o.rowid AS object_row,e.id AS event_id FROM objects AS o "
                "LEFT JOIN events AS e ON e.id=CASE WHEN typeof(o.data)='text' "
                "AND length(CAST(o.data AS BLOB))<=? THEN CASE WHEN json_valid(o.data) "
                "THEN CASE WHEN json_type(o.data,'$.event_id')='integer' "
                "THEN CASE WHEN typeof(json_extract(o.data,'$.event_id'))='integer' "
                "THEN json_extract(o.data,'$.event_id') END END END END AND e.type=? "
                "AND e.session_id=CASE WHEN typeof(o.data)='text' "
                "AND length(CAST(o.data AS BLOB))<=? THEN CASE WHEN json_valid(o.data) "
                "THEN json_extract(o.data,'$.session_id') END END "
                "AND CASE WHEN typeof(e.data)='text' AND length(CAST(e.data AS BLOB))<=? "
                "THEN CASE WHEN json_valid(e.data) THEN json_extract(e.data,'$.id') END END=o.id "
                "WHERE o.kind=? ORDER BY coalesce(e.id,0) DESC,o.id DESC LIMIT 1 OFFSET ?",
                (
                    MAX_ENVELOPE_BYTES,
                    RECEIPT_EVENT,
                    MAX_ENVELOPE_BYTES,
                    MAX_METADATA_BYTES,
                    RECEIPT_KIND,
                    maximum,
                ),
            ).fetchone()
            if victim is None:
                return
            self.store.db.execute(
                "DELETE FROM events WHERE type=? AND CASE WHEN typeof(data)='text' "
                "AND length(CAST(data AS BLOB))<=? THEN CASE WHEN json_valid(data) "
                "THEN json_extract(data,'$.id') END END="
                "(SELECT id FROM objects WHERE rowid=?)",
                (RECEIPT_EVENT, MAX_METADATA_BYTES, victim["object_row"]),
            )
            self.store.db.execute("DELETE FROM objects WHERE rowid=?", (victim["object_row"],))

    def delete_session(self, session_id: str) -> None:
        """Join the caller's session-deletion transaction; never commit here."""
        prefix = json.dumps([session_id], ensure_ascii=True, separators=(",", ":"))[:-1] + ","
        self.store.db.execute(
            "DELETE FROM objects WHERE kind=? AND (substr(id,1,length(?))=? "
            "OR CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CASE WHEN json_valid(data) THEN json_extract(data,'$.session_id') END END=?)",
            (RECEIPT_KIND, prefix, prefix, MAX_ENVELOPE_BYTES, session_id),
        )
        self.store.db.execute(
            "DELETE FROM events WHERE type=? AND session_id=?", (RECEIPT_EVENT, session_id)
        )

    def history(
        self,
        session_id: str,
        *,
        cursor: int | None = None,
        limit: int,
        output_limit: int,
        receipt_limit: int,
    ) -> dict[str, Any]:
        """List stored headers by archive event; never decode retained output."""
        _count(limit, minimum=1)
        _count(limit + 1, minimum=1)
        _count(output_limit, minimum=1)
        _count(receipt_limit, minimum=1)
        before = None if cursor is None else _count(cursor, minimum=1)
        if not self._session_exists(session_id):
            raise ShellSessionError("not_found", "该会话不存在，无法查询 shell 回执目录")
        result: dict[str, Any] = {
            "session_id": session_id,
            "receipt_source": "stored",
            "shell_state": "closed",
            "shell_pid": None,
            "order": "archive_event_desc",
            "items": [],
            "next_cursor": None,
        }
        if json_size(result) > receipt_limit:
            raise ShellSessionError("output_budget", "shell 回执目录元数据超过当前输出预算")
        prefix = json.dumps([session_id], ensure_ascii=True, separators=(",", ":"))[:-1] + ","
        try:
            # A producer event with a missing/invalid object is unavailable evidence,
            # not proof that this session has no archived commands.
            orphan = self.store.db.execute(
                "SELECT 1 FROM events AS e LEFT JOIN objects AS o ON o.kind=? "
                "AND o.id=CASE WHEN typeof(e.data)='text' "
                "AND length(CAST(e.data AS BLOB))<=? THEN CASE WHEN json_valid(e.data) "
                "THEN json_extract(e.data,'$.id') END END "
                "AND substr(o.id,1,length(?))=? "
                "WHERE e.type=? AND e.session_id=? AND o.id IS NULL LIMIT 1",
                (RECEIPT_KIND, MAX_METADATA_BYTES, prefix, prefix, RECEIPT_EVENT, session_id),
            ).fetchone()
            if orphan is not None:
                raise ValueError("unbound shell receipt event")
            query = (
                "WITH owned AS (SELECT id,CASE WHEN typeof(data)='text' "
                "AND length(CAST(data AS BLOB))<=? THEN CASE WHEN json_valid(data) "
                "THEN data END END AS data FROM objects WHERE kind=? AND "
                "(substr(id,1,length(?))=? OR CASE WHEN typeof(data)='text' "
                "AND length(CAST(data AS BLOB))<=? THEN CASE WHEN json_valid(data) "
                "THEN json_extract(data,'$.session_id') END END=?)), "
                "headers AS (SELECT id,json_remove(data,'$.output') AS header,"
                "CASE WHEN json_type(data,'$.output.observed')='integer' "
                "THEN json_extract(data,'$.output.observed') END AS observed,"
                "CASE WHEN json_type(data,'$.output.retained')='integer' "
                "THEN json_extract(data,'$.output.retained') END AS retained,"
                "CASE WHEN json_type(data,'$.output.discarded')='integer' "
                "THEN json_extract(data,'$.output.discarded') END AS discarded FROM owned) "
                "SELECT h.id,CASE WHEN length(CAST(h.header AS BLOB))<=? "
                "THEN CAST(h.header AS BLOB) END AS header,h.observed,h.retained,h.discarded,"
                "e.id AS position,e.type,e.session_id,"
                "CASE WHEN typeof(e.data)='text' AND length(CAST(e.data AS BLOB))<=? "
                "THEN CAST(e.data AS BLOB) END AS event_data "
                "FROM headers AS h LEFT JOIN events AS e ON e.id=json_extract(h.header,'$.event_id') "
                "WHERE (? IS NULL OR coalesce(e.id,0)<?) "
                "ORDER BY coalesce(e.id,0) DESC,h.id DESC LIMIT ?"
            )
            with closing(
                self.store.db.execute(
                    query,
                    (
                        MAX_ENVELOPE_BYTES,
                        RECEIPT_KIND,
                        prefix,
                        prefix,
                        MAX_ENVELOPE_BYTES,
                        session_id,
                        MAX_METADATA_BYTES,
                        MAX_METADATA_BYTES,
                        before,
                        before,
                        limit + 1,
                    ),
                )
            ) as rows:
                previous: int | None = None
                row = rows.fetchone()
                for _ in range(limit):
                    if row is None:
                        break
                    following = rows.fetchone()
                    if row["header"] is None or row["event_data"] is None:
                        raise ValueError("unavailable shell receipt header")
                    header = _object(row["header"])
                    command_id = header["command_id"]
                    metadata = _metadata(header, session_id, command_id)
                    position = _count(row["position"], minimum=1)
                    if (
                        row["id"] != header["id"]
                        or header["event_id"] != position
                        or row["type"] != RECEIPT_EVENT
                        or row["session_id"] != session_id
                        or _object(row["event_data"])
                        != {"id": header["id"], "source": RECEIPT_KIND}
                    ):
                        raise ValueError("unbound shell receipt identity")
                    observed, retained, discarded = (
                        _count(row[name]) for name in ("observed", "retained", "discarded")
                    )
                    if retained > MAX_RAW_BYTES or observed != retained + discarded:
                        raise ValueError("inconsistent shell receipt counts")
                    entry = {
                        "command_id": command_id,
                        "event_id": position,
                        "os_epoch": metadata["os_epoch"],
                        "state": metadata["state"],
                        "exit_code": metadata["exit_code"],
                        "truncated": discarded > 0 or retained > min(MAX_RAW_BYTES, output_limit),
                    }
                    result["items"].append(entry)
                    result["next_cursor"] = None if following is None else position
                    if json_size(result) > receipt_limit:
                        result["items"].pop()
                        if previous is None:
                            raise ShellSessionError(
                                "output_budget", "shell 回执目录单项超过当前输出预算"
                            )
                        result["next_cursor"] = previous
                        break
                    previous = position
                    row = following
        except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError) as exc:
            if isinstance(exc, ShellSessionError):
                raise
            raise ShellSessionError(
                "content_unavailable", "持久 shell 回执目录无法完整核查"
            ) from exc
        return result

    def read(
        self,
        session_id: str,
        command_id: str,
        *,
        cursor: OutputCursor | None = None,
        output_limit: int,
        receipt_limit: int,
    ) -> dict[str, Any]:
        if not self._session_exists(session_id) or not _IDENTIFIER.fullmatch(command_id):
            raise ShellSessionError("not_found", "该会话没有可读的 shell 命令记录")
        key = receipt_key(session_id, command_id)
        header = self.store.db.execute(
            "SELECT CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CASE WHEN json_valid(data) "
            "THEN length(CAST(json_remove(data,'$.output') AS BLOB)) END END AS size "
            "FROM objects WHERE kind=? AND id=?",
            (MAX_ENVELOPE_BYTES, RECEIPT_KIND, key),
        ).fetchone()
        if header is None:
            raise ShellSessionError("not_found", "该会话没有可读的 shell 命令记录")
        if header["size"] is None or header["size"] > MAX_METADATA_BYTES:
            raise ShellSessionError("content_unavailable", "持久 shell 回执无法完整核查")
        try:
            record = self.store.bounded_object(
                RECEIPT_KIND,
                key,
                max_bytes=MAX_ENVELOPE_BYTES,
                strict_json=True,
            )
            if record is None:
                raise ShellSessionError("not_found", "该会话没有可读的 shell 命令记录")
            metadata, output = _decode(record, session_id, command_id)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            if isinstance(exc, ShellSessionError):
                raise
            raise ShellSessionError("content_unavailable", "持久 shell 回执无法完整核查") from exc
        cursor = _read_cursor(cursor, output)
        output.trim(min(MAX_RAW_BYTES, max(1, output_limit)))
        result = {
            **metadata,
            "session_id": session_id,
            "command_id": command_id,
            "shell_pid": None,
            "shell_state": "closed",
            "receipt_source": "stored",
            "output_scope": "shell",
            "output": "",
            "stderr": "",
            "cursor": cursor.copy(),
            "next_cursor": {
                name: max(cursor[name], output.discarded_by_stream[name]) for name in _STREAMS
            },
            "observed_bytes": output.observed,
            "discarded_bytes": output.discarded,
            "truncated": output.discarded > 0,
        }
        return _receipt_page(output, result, cursor, maximum=receipt_limit, final=True)
