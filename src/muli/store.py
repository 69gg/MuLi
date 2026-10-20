"""Small transactional journal shared by sessions, agents and inbound events."""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, Literal

from muli.reply_origin import ReplyOrigin

LifecycleKind = Literal["session", "run", "approval", "workflow_run"]
_LIFECYCLE_KINDS = ("session", "run", "approval", "workflow_run")
_LIFECYCLE_PAGE_SIZE = 100
_UNFINISHED_LIFECYCLE = (
    "kind IN ('session','run','approval','workflow_run') "
    "AND json_extract(data,'$.status') IN ('pending','queued','running') "
)
SQLITE_MAX_INTEGER = (1 << 63) - 1
_SESSION_RECORD_LIMIT = 101  # A public page of at most 100 plus one lookahead.


def _session_record_time(field: str) -> str:
    """Modern session dates are finite positive JSON numbers, never coerced text."""
    value = f"json_extract(data,'$.{field}')"
    return (
        f"CASE WHEN json_type(data,'$.{field}') IN ('integer','real') "
        f"AND {value}>0 AND {value}<={sys.float_info.max!r} THEN {value} END"
    )


_SESSION_RECORD_CREATED = _session_record_time("created_at")
_SESSION_RECORD_SORT = f"coalesce(({_SESSION_RECORD_CREATED}),0)"
_SESSION_RECORD_UPDATED = _session_record_time("updated_at")
_SESSION_RECORD_TITLE = (
    "CASE WHEN json_type(data,'$.title')='text' THEN json_extract(data,'$.title') END"
)


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def message_text_size(record: dict[str, Any]) -> int:
    """Text context accounting excludes inline media, whose bytes have their own limit."""
    data = dict(record)
    if data.get("images"):
        data["images"] = [
            {**image, "data": f"[inline image: {len(image.get('data', ''))} base64 chars]"}
            for image in data["images"]
        ]
    return len(json.dumps(data, ensure_ascii=False))


_EVIDENCE_METADATA = ("role", "source", "principal", "evidence_revision")
# Older SQLite versions narrow substr lengths to 32 bits. The actual BLOB size
# is sufficient for any requested window and keeps large valid budgets portable.
_EVIDENCE_COLUMNS = (
    "o.id,m.seq,json_type(o.data,'$.content') AS content_type,"
    "length(CAST(json_extract(o.data,'$.content') AS BLOB)) AS content_bytes,"
    "coalesce(substr(CAST(json_extract(o.data,'$.content') AS BLOB),?,"
    "min(?,length(CAST(json_extract(o.data,'$.content') AS BLOB)))),x'') AS content,"
    "CASE json_extract(o.data,'$.content_provenance') "
    "WHEN 'accepted' THEN 'accepted' WHEN 'runtime_excerpt' THEN 'runtime_excerpt' "
    "ELSE 'unknown' END AS content_provenance,"
    + ",".join(
        f"json_type(o.data,'$.{field}') AS {field}_type,"
        f"CASE WHEN json_type(o.data,'$.{field}')='text' THEN "
        f"coalesce(substr(CAST(json_extract(o.data,'$.{field}') AS BLOB),1,"
        f"min(?,length(CAST(json_extract(o.data,'$.{field}') AS BLOB)))),x'') END AS {field},"
        f"length(CAST(json_extract(o.data,'$.{field}') AS BLOB)) AS {field}_bytes"
        for field in _EVIDENCE_METADATA
    )
)
_EVIDENCE_FROM = (
    " FROM message_order m JOIN objects o ON o.kind='message' AND o.id=m.message_id "
    "WHERE m.session_id=? AND json_type(o.data,'$.session_id')='text' "
    "AND json_extract(o.data,'$.session_id')=m.session_id "
)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=3000;
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(kind,id)
            );
            CREATE INDEX IF NOT EXISTS objects_updated ON objects(kind,updated DESC);
            CREATE INDEX IF NOT EXISTS agents_parent ON objects(json_extract(data,'$.parent_id')) WHERE kind='agent';
            CREATE INDEX IF NOT EXISTS agents_status ON objects(json_extract(data,'$.status')) WHERE kind='agent';
            CREATE INDEX IF NOT EXISTS agents_commitment ON objects(json_extract(data,'$.commitment_id')) WHERE kind='agent';
            CREATE INDEX IF NOT EXISTS workflow_runs_status ON objects(json_extract(data,'$.status')) WHERE kind='workflow_run';
            CREATE INDEX IF NOT EXISTS objects_unfinished_lifecycle ON objects(kind,id)
                WHERE kind IN ('session','run','approval','workflow_run')
                AND json_extract(data,'$.status') IN ('pending','queued','running');
            CREATE INDEX IF NOT EXISTS workflow_runs_by_parent
                ON objects(json_extract(data,'$.parent_run_id'),id) WHERE kind='workflow_run';
            CREATE INDEX IF NOT EXISTS task_plan_pending ON objects(id) WHERE kind='task_plan_delivery' AND json_extract(data,'$.state')='pending';
            CREATE TABLE IF NOT EXISTS message_order (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS message_order_session ON message_order(session_id,seq);
            CREATE TABLE IF NOT EXISTS runtime_state (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingress (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                received REAL NOT NULL, delivered_seq INTEGER, run_id TEXT
            );
            CREATE INDEX IF NOT EXISTS ingress_pending ON ingress(session_id,state,seq);
            CREATE INDEX IF NOT EXISTS ingress_system_log_run
                ON ingress(run_id) WHERE run_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                session_id TEXT, data TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
            CREATE UNIQUE INDEX IF NOT EXISTS team_completion_revision
                ON events(session_id,json_extract(data,'$.completion_revision'))
                WHERE type='team.finished';
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, envelope TEXT NOT NULL, received REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL,
                source TEXT NOT NULL, entities TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, confidence REAL NOT NULL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS relations (
                memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
                PRIMARY KEY(memory_id,subject,predicate,object)
            );
            CREATE INDEX IF NOT EXISTS relations_subject ON relations(subject);
            CREATE INDEX IF NOT EXISTS relations_object ON relations(object);
        """)
        # The expression is shared with the page predicate/order. The kind prefix
        # lets SQLite choose this partial index over the older updated index.
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS sessions_created_directory "
            f"ON objects(kind,({_SESSION_RECORD_SORT}) DESC,id DESC) WHERE kind='session'"
        )
        # Old archives have no insertion sequence. Preserve their existing order once;
        # all subsequent messages use a database sequence, independent of wall time.
        if (
            self.db.execute(
                "SELECT 1 FROM runtime_state WHERE name='message_sequence_v1'"
            ).fetchone()
            is None
        ):
            self.db.execute(
                "INSERT OR IGNORE INTO message_order(message_id,session_id) "
                "SELECT id,json_extract(data,'$.session_id') FROM objects WHERE kind='message' "
                "ORDER BY updated,rowid"
            )
            self.db.execute(
                "INSERT INTO runtime_state(name,value) VALUES('message_sequence_v1','done')"
            )
        self.db.execute(
            "INSERT OR IGNORE INTO runtime_state(name,value) VALUES('store_instance_v1',?)",
            (new_id(),),
        )
        self.db.commit()

    @property
    def instance_id(self) -> str:
        row = self.db.execute(
            "SELECT value FROM runtime_state WHERE name='store_instance_v1'"
        ).fetchone()
        assert row is not None
        return str(row[0])

    def close(self) -> None:
        self.db.close()

    def put(self, kind: str, item: dict[str, Any]) -> dict[str, Any]:
        with self.db:
            self._put(kind, item)
        return item

    def _put(self, kind: str, item: dict[str, Any]) -> None:
        """Write inside the caller's transaction; never commit a nested transaction."""
        if kind == "message":
            previous = self.db.execute(
                "SELECT session_id FROM message_order WHERE message_id=?", (item["id"],)
            ).fetchone()
            if previous is not None and previous["session_id"] != item["session_id"]:
                raise ValueError("同一消息不能迁移到另一个会话")
            self.db.execute(
                "INSERT OR IGNORE INTO message_order(message_id,session_id) VALUES(?,?)",
                (item["id"], item["session_id"]),
            )
            item["seq"] = self.db.execute(
                "SELECT seq FROM message_order WHERE message_id=?", (item["id"],)
            ).fetchone()[0]
            # A caller cannot preserve a cursor across edits by replaying its old token.
            item["evidence_revision"] = new_id()
        self.db.execute(
            "INSERT INTO objects VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated",
            (kind, item["id"], json.dumps(item, ensure_ascii=False), now()),
        )

    def put_many(self, items: builtins.list[tuple[str, dict[str, Any]]]) -> None:
        with self.db:
            for kind, item in items:
                self._put(kind, item)

    def get(self, kind: str, identifier: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT data FROM objects WHERE kind=? AND id=?", (kind, identifier)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM objects WHERE kind=? ORDER BY updated DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def session_record_rows(
        self,
        *,
        query: str,
        after: tuple[int | float, str] | None,
        limit: int,
        metadata_bytes: int,
    ) -> Generator[dict[str, Any], None, None]:
        """Yield creation-ordered metadata without loading whole session JSON in Python."""
        if type(limit) is not int or not 1 <= limit <= _SESSION_RECORD_LIMIT:
            raise ValueError("记录目录查询需要 1–101 条的有界窗口")
        if type(metadata_bytes) is not int or metadata_bytes < 1:
            raise ValueError("记录目录元数据字节预算必须是正整数")
        metadata_bytes = min(metadata_bytes, SQLITE_MAX_INTEGER)
        predicate = ""
        bounds: tuple[int | float | str, ...] = ()
        if after is not None:
            # The scalar bound lets SQLite seek the expression index; the tuple
            # keeps ties strict without depending on the anchor row still existing.
            predicate = (
                f"AND ({_SESSION_RECORD_SORT})<=? AND (({_SESSION_RECORD_SORT}),objects.id)<(?,?) "
            )
            bounds = (after[0], *after)
        columns = [
            "substr(CAST(id AS BLOB),1,min(?,length(CAST(id AS BLOB)))) AS id",
            "length(CAST(id AS BLOB)) AS id_bytes",
            f"({_SESSION_RECORD_CREATED}) AS created_at",
            f"({_SESSION_RECORD_UPDATED}) AS updated_at",
            f"({_SESSION_RECORD_SORT}) AS sort_created_at",
        ]
        for field in ("title", "status"):
            value = f"json_extract(data,'$.{field}')"
            columns.extend(
                (
                    f"json_type(data,'$.{field}') AS {field}_type",
                    f"CASE WHEN json_type(data,'$.{field}')='text' THEN "
                    f"coalesce(substr(CAST({value} AS BLOB),1,"
                    f"min(?,length(CAST({value} AS BLOB)))),x'') "
                    f"END AS {field}",
                    f"length(CAST({value} AS BLOB)) AS {field}_bytes",
                )
            )
        cursor = self.db.execute(
            "SELECT "
            + ",".join(columns)
            + " FROM objects WHERE kind='session' "
            + f"AND (?='' OR instr(lower({_SESSION_RECORD_TITLE}),lower(?))>0) "
            + predicate
            + f"ORDER BY ({_SESSION_RECORD_SORT}) DESC,objects.id DESC LIMIT ?",
            (metadata_bytes, metadata_bytes, metadata_bytes, query, query, *bounds, limit),
        )
        try:
            for row in cursor:
                yield dict(row)
        finally:
            cursor.close()

    @staticmethod
    def _lifecycle_page(after_id: str, limit: int) -> None:
        if (
            not isinstance(after_id, str)
            or type(limit) is not int
            or not 1 <= limit <= _LIFECYCLE_PAGE_SIZE
        ):
            raise ValueError(f"生命周期查询需要字符串游标和 1–{_LIFECYCLE_PAGE_SIZE} 的分页数量")

    def unfinished_object_ids(
        self,
        kind: LifecycleKind,
        *,
        after_id: str = "",
        limit: int = _LIFECYCLE_PAGE_SIZE,
        workflow_id: str | None = None,
    ) -> builtins.list[str]:
        """Select the unfinished scope before paging, without materializing its bodies."""
        self._lifecycle_page(after_id, limit)
        if kind not in _LIFECYCLE_KINDS or (workflow_id is not None and kind != "workflow_run"):
            raise ValueError("生命周期类型或工作流筛选无效")
        scope = "AND json_extract(data,'$.workflow_id')=? " if workflow_id is not None else ""
        values: tuple[Any, ...] = (kind, after_id)
        if workflow_id is not None:
            values += (workflow_id,)
        rows = self.db.execute(
            "SELECT id FROM objects WHERE "
            + _UNFINISHED_LIFECYCLE
            + "AND kind=? AND id>? "
            + scope
            + "ORDER BY id LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def workflow_child_ids(
        self, parent_run_id: str, *, after_id: str = "", limit: int = _LIFECYCLE_PAGE_SIZE
    ) -> builtins.list[str]:
        """Page every child of one exact execution, including completed children."""
        self._lifecycle_page(after_id, limit)
        rows = self.db.execute(
            "SELECT id FROM objects WHERE kind='workflow_run' "
            "AND json_extract(data,'$.parent_run_id')=? AND id>? ORDER BY id LIMIT ?",
            (parent_run_id, after_id, limit),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def has_unfinished_workflow(self, workflow_id: str) -> bool:
        return bool(self.unfinished_object_ids("workflow_run", workflow_id=workflow_id, limit=1))

    def workflow_run_summary(self, identifier: str) -> dict[str, Any] | None:
        """Read management metadata without loading node prompts, results or tool data."""
        row = self.db.execute(
            "SELECT id,json_extract(data,'$.workflow_id') AS workflow_id,"
            "json_extract(data,'$.name') AS name,json_extract(data,'$.status') AS status,"
            "json_extract(data,'$.created_at') AS created_at,"
            "json_extract(data,'$.updated_at') AS updated_at "
            "FROM objects WHERE kind='workflow_run' AND id=?",
            (identifier,),
        ).fetchone()
        return dict(row) if row is not None else None

    def delete(self, kind: str, identifier: str) -> bool:
        with self.db:
            return bool(
                self.db.execute(
                    "DELETE FROM objects WHERE kind=? AND id=?", (kind, identifier)
                ).rowcount
            )

    def event(
        self,
        event_type: str,
        data: dict[str, Any],
        session_id: str | None = None,
        *,
        objects: builtins.list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        with self.db:
            return self._event(event_type, data, session_id, objects=objects)

    def _event(
        self,
        event_type: str,
        data: dict[str, Any],
        session_id: str | None = None,
        *,
        objects: builtins.list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Append inside the caller's transaction without an intermediate commit."""
        created = now()
        for kind, item in objects or []:
            self._put(kind, item)
        cursor = self.db.execute(
            "INSERT INTO events(type,session_id,data,created_at) VALUES(?,?,?,?)",
            (event_type, session_id, json.dumps(data, ensure_ascii=False), created),
        )
        return {
            "id": cursor.lastrowid,
            "type": event_type,
            "session_id": session_id,
            "data": data,
            "created_at": created,
        }

    def event_position(self) -> int:
        """Read the latest durable event ID without loading any event payload."""
        return int(self.db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0])

    def events(
        self, after: int = 0, session_id: str | None = None, limit: int = 500
    ) -> builtins.list[dict[str, Any]]:
        if session_id:
            rows = self.db.execute(
                "SELECT * FROM events WHERE id>? AND session_id=? ORDER BY id LIMIT ?",
                (after, session_id, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)
            ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def team_completions(
        self, session_id: str, after: int, *, limit: int = 1
    ) -> builtins.list[dict[str, Any]]:
        """Read a bounded revision prefix using the partial completion index."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE type='team.finished' AND session_id=? "
            "AND json_extract(data,'$.completion_revision')>? "
            "ORDER BY json_extract(data,'$.completion_revision') LIMIT ?",
            (session_id, after, max(0, limit)),
        ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def recent_events(self, session_id: str, limit: int = 3000) -> builtins.list[dict[str, Any]]:
        """Return the newest session events in chronological order for initial display."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, max(0, limit)),
        ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in reversed(rows)]

    def add_message(self, session_id: str, role: str, content: str, **extra: Any) -> dict[str, Any]:
        message = {
            "id": new_id(),
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": now(),
            **extra,
        }
        self.put("message", message)
        return message

    def messages(
        self, session_id: str, *, after: int = 0, limit: int = 1000
    ) -> builtins.list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT o.data,m.seq FROM message_order m JOIN objects o "
            "ON o.kind='message' AND o.id=m.message_id "
            "WHERE m.session_id=? AND m.seq>? ORDER BY m.seq DESC LIMIT ?",
            (session_id, after, max(0, limit)),
        ).fetchall()
        return [{**json.loads(row["data"]), "seq": row["seq"]} for row in reversed(rows)]

    def message_previews(
        self, session_id: str, *, limit: int = 5, max_chars: int = 1000
    ) -> builtins.list[dict[str, Any]]:
        """Project small display records in SQL, without loading native blocks or images."""
        max_chars = max(0, max_chars)
        rows = self.db.execute(
            "SELECT o.id,m.seq,json_extract(o.data,'$.role') AS role,"
            "json_extract(o.data,'$.created_at') AS created_at,"
            "substr(CAST(json_extract(o.data,'$.content') AS BLOB),1,?) AS preview,"
            "length(CAST(json_extract(o.data,'$.content') AS BLOB)) AS content_bytes "
            "FROM message_order m JOIN objects o ON o.kind='message' AND o.id=m.message_id "
            "WHERE m.session_id=? ORDER BY m.seq DESC LIMIT ?",
            (max_chars * 4 + 1, session_id, max(0, limit)),
        ).fetchall()
        result: builtins.list[dict[str, Any]] = []
        for row in reversed(rows):
            # UTF-8 needs at most four bytes per code point. Ignore only an
            # incomplete final code point; BLOB slicing also preserves embedded NUL.
            raw = bytes(row["preview"] or b"")
            text = raw.decode("utf-8", errors="ignore")
            result.append(
                {
                    **{key: row[key] for key in ("id", "seq", "role", "created_at")},
                    "content": text[:max_chars],
                    "truncated": row["content_bytes"] > len(raw) or len(text) > max_chars,
                }
            )
        return result

    def message_prefix(
        self,
        session_id: str,
        *,
        after: int = 0,
        through: int | None = None,
        limit: int = 1000,
        max_chars: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT o.data,m.seq FROM message_order m JOIN objects o "
            "ON o.kind='message' AND o.id=m.message_id WHERE m.session_id=? "
            "AND m.seq>? AND (? IS NULL OR m.seq<=?) ORDER BY m.seq LIMIT ?",
            (session_id, after, through, through, limit),
        )
        result: builtins.list[dict[str, Any]] = []
        total = 0
        for row in rows:
            record = {**json.loads(row["data"]), "seq": row["seq"]}
            size = message_text_size(record)
            if max_chars is not None and result and total + size > max_chars:
                break
            total += size
            result.append(record)
        return result

    def message_evidence_directory(
        self,
        session_id: str,
        *,
        after: int,
        limit: int,
        preview_bytes: int,
        metadata_bytes: int,
    ) -> Generator[dict[str, Any], None, None]:
        """Yield bounded SQL projections, without decoding native blocks or media in Python."""
        cursor = self.db.execute(
            "SELECT " + _EVIDENCE_COLUMNS + _EVIDENCE_FROM + "AND m.seq>? ORDER BY m.seq LIMIT ?",
            (
                1,
                preview_bytes,
                *([metadata_bytes] * len(_EVIDENCE_METADATA)),
                session_id,
                after,
                limit,
            ),
        )
        try:
            for row in cursor:
                yield dict(row)
        finally:
            cursor.close()

    def message_evidence_max_seq(self, session_id: str) -> int:
        """Return the latest valid mapping without materializing message JSON in Python."""
        row = self.db.execute(
            "SELECT m.seq" + _EVIDENCE_FROM + "ORDER BY m.seq DESC LIMIT 1", (session_id,)
        ).fetchone()
        return int(row["seq"]) if row is not None else 0

    def message_evidence_archive(
        self,
        session_id: str,
        *,
        before_seq: int | None,
        through_seq: int,
        limit: int,
        preview_bytes: int,
        metadata_bytes: int,
    ) -> Generator[dict[str, Any], None, None]:
        """Yield a bounded descending window using the existing session/sequence index."""
        predicate = "AND m.seq<=? "
        bounds: tuple[int, ...] = (through_seq,)
        if before_seq is not None:
            predicate += "AND m.seq<? "
            bounds += (before_seq,)
        cursor = self.db.execute(
            "SELECT "
            + _EVIDENCE_COLUMNS
            + _EVIDENCE_FROM
            + predicate
            + "ORDER BY m.seq DESC LIMIT ?",
            (
                1,
                preview_bytes,
                *([metadata_bytes] * len(_EVIDENCE_METADATA)),
                session_id,
                *bounds,
                limit,
            ),
        )
        try:
            for row in cursor:
                yield dict(row)
        finally:
            cursor.close()

    def message_evidence_slice(
        self,
        session_id: str,
        *,
        message_id: str | None,
        seq: int | None,
        offset_bytes: int,
        length_bytes: int,
        metadata_bytes: int,
    ) -> dict[str, Any] | None:
        """Read metadata and one content window from a single current SQLite row."""
        if (message_id is None) == (seq is None):
            raise ValueError("message_id 和 seq 必须且只能提供一个")
        predicate = "AND m.message_id=?" if message_id is not None else "AND m.seq=?"
        row = self.db.execute(
            "SELECT " + _EVIDENCE_COLUMNS + _EVIDENCE_FROM + predicate,
            (
                offset_bytes + 1,
                length_bytes,
                *([metadata_bytes] * len(_EVIDENCE_METADATA)),
                session_id,
                message_id if message_id is not None else seq,
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    def replay_messages(
        self, session_id: str, *, after: int = 0, limit: int = 1000
    ) -> builtins.list[dict[str, Any]]:
        """A bounded tail starting at a full user turn, never an orphan tool result.

        If the current turn alone exceeds the row budget, fail explicitly instead
        of dropping the user's request and silently replaying an unrelated suffix.
        The archive and its pagination API retain every original message.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError("回放消息条数预算必须是正整数")
        records = self.messages(session_id, after=after, limit=limit)
        earliest = self.message_prefix(session_id, after=after, limit=1)
        if not records or not earliest or records[0]["seq"] == earliest[0]["seq"]:
            return records
        for index, record in enumerate(records):
            if record["role"] == "user":
                return records[index:]
        raise ValueError("当前完整用户轮次超过回放消息条数预算；原始会话仍保留，未发送残缺上下文")

    def main_session(self, mode: str = "auto") -> dict[str, Any]:
        """One durable local identity; old conversation sessions remain accessible."""
        with self.db:
            row = self.db.execute(
                "SELECT value FROM runtime_state WHERE name='main_session'"
            ).fetchone()
            if row:
                session = self.get("session", row[0])
                if session is None:
                    raise ValueError("主会话记录缺失；请恢复数据，不会静默创建另一个身份")
                return session
            session = {
                "id": new_id(),
                "title": "沐璃",
                "kind": "main",
                "principal": "owner",
                "mode": mode,
                "status": "idle",
                "created_at": now(),
                "updated_at": now(),
            }
            self._put("session", session)
            self.db.execute(
                "INSERT INTO runtime_state(name,value) VALUES('main_session',?)", (session["id"],)
            )
        return session

    def enqueue_input(
        self,
        payload: dict[str, Any],
        *,
        max_pending: int,
        quarantine: bool = False,
        objects: builtins.list[tuple[str, dict[str, Any]]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.db:
            return self._enqueue_input(
                payload, max_pending=max_pending, quarantine=quarantine, objects=objects
            )

    def _enqueue_input(
        self,
        payload: dict[str, Any],
        *,
        max_pending: int,
        quarantine: bool = False,
        objects: builtins.list[tuple[str, dict[str, Any]]] | None = None,
        apply_objects_on_duplicate: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Insert inside a caller-owned transaction; control retries must be read-only."""
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        previous = self.db.execute("SELECT * FROM ingress WHERE id=?", (payload["id"],)).fetchone()
        if previous:
            if previous["payload"] != serialized:
                raise ValueError("相同输入 ID 的内容或来源不一致")
            if apply_objects_on_duplicate:
                for kind, item in objects or []:
                    self._put(kind, item)
            return {
                **payload,
                "state": previous["state"],
                "seq": previous["seq"],
                "run_id": previous["run_id"],
            }, False
        state = "quarantined" if quarantine else "pending"
        count = self.db.execute(
            "SELECT count(*) FROM ingress WHERE session_id=? AND state=?",
            (payload["session_id"], state),
        ).fetchone()[0]
        if count >= max_pending:
            raise ValueError("待处理输入已达到配置上限，请稍后再试")
        cursor = self.db.execute(
            "INSERT INTO ingress(id,session_id,state,payload,received) VALUES(?,?,?,?,?)",
            (payload["id"], payload["session_id"], state, serialized, now()),
        )
        sequence = cursor.lastrowid
        for kind, item in objects or []:
            self._put(kind, item)
        return {**payload, "state": state, "seq": sequence}, True

    def pending_inputs(self, session_id: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM ingress WHERE session_id=? AND state='pending'", (session_id,)
            ).fetchone()[0]
        )

    def claim_inputs(
        self, session_id: str, run_id: str, *, limit: int
    ) -> builtins.list[dict[str, Any]]:
        """At a replay-safe boundary, atomically append inputs and advance delivery state."""
        result: builtins.list[dict[str, Any]] = []
        destination: tuple[str, str] | None = None
        with self.db:
            rows = self.db.execute(
                "SELECT * FROM ingress WHERE session_id=? AND state='pending' ORDER BY seq LIMIT ?",
                (session_id, limit),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                origin = ReplyOrigin.from_input(payload)
                if destination is not None and origin.destination_key != destination:
                    break  # Another recipient waits for the next replay-safe boundary.
                destination = origin.destination_key
                message = self._archive_input(
                    payload, received=row["received"], run_id=run_id, input_seq=row["seq"]
                )
                self.db.execute(
                    "UPDATE ingress SET state='delivered',delivered_seq=?,run_id=? WHERE seq=?",
                    (message["seq"], run_id, row["seq"]),
                )
                result.append(message)
        return result

    def _archive_input(
        self,
        payload: dict[str, Any],
        *,
        received: float,
        run_id: str | None,
        input_seq: int | None = None,
    ) -> dict[str, Any]:
        """Archive an input in the caller's transaction, preserving trusted provenance."""
        message = {
            "id": payload["id"],
            "session_id": payload["session_id"],
            "role": "user",
            "content": payload["content"],
            "created_at": received,
            "source": payload["source"],
            "principal": payload["principal"],
            "route": payload.get("route"),
            "attachment_ids": payload.get("attachment_ids", []),
            "delivery_status": "delivered",
            "run_id": run_id,
            "reply_origin": ReplyOrigin.from_input(payload).dump(),
        }
        if input_seq is not None:
            message["input_seq"] = input_seq
        if "task_control" in payload:
            message["task_control"] = payload["task_control"]
        if self.get("message", payload["id"]) is not None:
            raise ValueError("输入消息 ID 已经存档，不能覆盖历史")
        self._put("message", message)
        return message

    def cancel_inputs(self, session_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE ingress SET state='cancelled' WHERE session_id=? AND state='pending'",
                (session_id,),
            )

    def ingest(
        self,
        envelope: dict[str, Any],
        *,
        apply: Callable[[sqlite3.Connection], None] | None = None,
        namespace: str = "",
    ) -> bool:
        """Persist envelope and its notification in one transaction before upstream ACK."""
        serialized = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        identifier = f"{namespace}:{envelope['id']}" if namespace else envelope["id"]
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO inbox VALUES(?,?,?)",
                (identifier, serialized, now()),
            )
            if not cursor.rowcount:
                existing = self.db.execute(
                    "SELECT envelope FROM inbox WHERE id=?", (identifier,)
                ).fetchone()
                if existing is None or json.loads(existing[0]) != envelope:
                    raise ValueError("相同 chiliarch 消息 ID 的内容不一致")
                return False
            if apply is not None:
                apply(self.db)
            notice = {
                "id": identifier,
                "title": str(envelope.get("type", "chiliarch")),
                "content": json.dumps(envelope.get("payload", {}), ensure_ascii=False),
                "source": "chiliarch",
                "read": False,
                "created_at": now(),
            }
            self.db.execute(
                "INSERT INTO objects VALUES('notification',?,?,?)",
                (notice["id"], json.dumps(notice, ensure_ascii=False), now()),
            )
        return True
