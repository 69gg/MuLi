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
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                session_id TEXT, data TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
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
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def put(self, kind: str, item: dict[str, Any]) -> dict[str, Any]:
        with self.db:
            self.db.execute(
                "INSERT INTO objects VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated",
                (kind, item["id"], json.dumps(item, ensure_ascii=False), now()),
            )
        return item

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
        self, event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        created = now()
        with self.db:
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

    def messages(self, session_id: str) -> builtins.list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM objects WHERE kind='message' AND json_extract(data,'$.session_id')=? ORDER BY updated DESC LIMIT 1000",
            (session_id,),
        ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

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
