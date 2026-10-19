"""Small transactional journal shared by sessions, agents and inbound events."""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


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
            self.db.execute(
                "INSERT OR IGNORE INTO message_order(message_id,session_id) VALUES(?,?)",
                (item["id"], item["session_id"]),
            )
            item["seq"] = self.db.execute(
                "SELECT seq FROM message_order WHERE message_id=?", (item["id"],)
            ).fetchone()[0]
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
        with self.db:
            rows = self.db.execute(
                "SELECT * FROM ingress WHERE session_id=? AND state='pending' ORDER BY seq LIMIT ?",
                (session_id, limit),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
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
