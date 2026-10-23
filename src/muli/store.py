"""Small transactional journal shared by sessions, agents and inbound events."""

from __future__ import annotations

import builtins
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, Literal

from muli.compatibility import FormatState
from muli.evidence import EvidenceError
from muli.format_admission import (
    FormatAdmissionError,
    publish_created_format,
    require_current_format,
)
from muli.format_evidence import (
    FormatEvidence,
    FormatEvidenceError,
    FormatReadLimits,
    read_optional_format_evidence,
)
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store_initialization import CreatedDatabase, StoreConstructionLock
from muli.store_schema import (
    CHANNEL_SCHEMA,
    CORE_SCHEMA,
    SESSION_DIRECTORY_SCHEMA,
    ExistingStoreError,
    ExistingStoreLimits,
    read_existing_main,
    validate_existing_schema,
)
from muli.store_schema import (
    SESSION_RECORD_CREATED as _SESSION_RECORD_CREATED,
)
from muli.store_schema import (
    SESSION_RECORD_SORT as _SESSION_RECORD_SORT,
)
from muli.store_schema import (
    session_record_time as _session_record_time,
)

LifecycleKind = Literal["session", "run", "approval", "workflow_run"]
_LIFECYCLE_KINDS = ("session", "run", "approval", "workflow_run")
_LIFECYCLE_PAGE_SIZE = 100
_UNFINISHED_LIFECYCLE = (
    "kind IN ('session','run','approval','workflow_run') "
    "AND json_extract(data,'$.status') IN ('pending','queued','running') "
)
SQLITE_MAX_INTEGER = (1 << 63) - 1
_SESSION_RECORD_LIMIT = 101  # A public page of at most 100 plus one lookahead.
_ATTACHMENT_RECORD_LIMIT = 201  # At most 200 scanned rows plus one lookahead.


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


_EVIDENCE_METADATA = ("role", "source", "principal", "evidence_revision", "attachment_ids")
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
        f"CASE WHEN json_type(o.data,'$.{field}')="
        f"'{('array' if field == 'attachment_ids' else 'text')}' THEN "
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
    def __init__(self, path: Path, *, durable: bool = False) -> None:
        try:
            with StoreConstructionLock(path) as lock:
                self._open_locked(lock, durable=durable)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            raise

    @classmethod
    def open_runtime(cls, path: Path, *, default_mode: str, durable: bool = False) -> Store:
        """Initialize/certify only a database exclusively created by this call.

        Existing unmarked data keeps legacy behavior. No empty-file or schema
        heuristic can turn it into a newly created, format-certified database.
        """
        store = cls.__new__(cls)
        try:
            with StoreConstructionLock(path) as lock:
                store._open_locked(lock, durable=durable, runtime_mode=default_mode)
        except BaseException:
            if hasattr(store, "db"):
                store.db.close()
            raise
        return store

    def _open_locked(
        self, lock: StoreConstructionLock, *, durable: bool, runtime_mode: str | None = None
    ) -> None:
        self._migration_allowed = True
        self._format_state: FormatState | None = None
        self._format_identity: tuple[str, str] | None = None
        lock.check()
        creation = CreatedDatabase.claim(lock.database)
        try:
            self.db = sqlite3.connect(
                lock.database.as_uri() + "?mode=rw&cache=private",
                uri=True,
                check_same_thread=False,
                timeout=0,
                isolation_level="DEFERRED",
            )
            if creation is not None:
                creation.check()
            # Classification occurs under the constructor lock and before
            # any ordinary DDL, backfill, chmod or new identity.
            evidence = read_optional_format_evidence(self.db)
            if evidence is not None:
                self._admit_format(evidence)
                self._configure_existing(durable=durable)
            else:
                if creation is not None:
                    creation.check()
                lock.database.chmod(0o600)
                self._initialize(durable=durable)
                if creation is not None and runtime_mode is not None:
                    self._finish_runtime_creation(creation, lock, runtime_mode)
            lock.check()
        finally:
            if creation is not None:
                creation.__exit__()

    def _finish_runtime_creation(
        self, creation: CreatedDatabase, lock: StoreConstructionLock, default_mode: str
    ) -> None:
        # Memory is synchronous and owns no external resources. Reuse its actual
        # initializer on this proven-new database, not a second schema mirror.
        from muli.memory import Memory

        creation.check()
        Memory(self)
        creation.check()
        self.prepare_channels()
        self.main_session(default_mode)
        validate_existing_schema(self.db)
        lock.check()
        creation.check()
        publish_created_format(self.db, creation)
        evidence = read_optional_format_evidence(self.db)
        if evidence is None:
            raise FormatAdmissionError("store_format_marker_missing")
        self._admit_format(evidence)

    def _admit_format(self, evidence: FormatEvidence) -> None:
        require_current_format(evidence)
        validate_existing_schema(self.db)
        identity = evidence.store_instance, evidence.main_session
        if self._format_identity is not None and identity != self._format_identity:
            raise FormatAdmissionError("store_format_identity_changed")
        self._format_state, self._format_identity = evidence.state, identity
        self._migration_allowed = False

    def revalidate_format(self) -> None:
        """Admitted data never falls back to legacy if its marker is removed."""
        if self._format_state is not None:
            evidence = read_optional_format_evidence(self.db)
            if evidence is None:
                raise FormatAdmissionError("store_format_marker_missing")
            self._admit_format(evidence)

    @property
    def format_state(self) -> FormatState | None:
        """Last admitted contract; callers revalidate before protected effects."""
        return self._format_state

    def prepare_channels(self) -> None:
        """Shared local schema preparation; never create a transport or recover."""
        self.revalidate_format()
        if not self.migration_allowed:
            validate_existing_schema(self.db, "channel")
            return
        self.db.executescript(CHANNEL_SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(channel_inbound)")}
        if "fingerprint" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE channel_inbound ADD COLUMN fingerprint TEXT")

    @classmethod
    def open_existing(
        cls,
        path: Path,
        *,
        durable: bool = False,
        limits: ExistingStoreLimits | None = None,
    ) -> Store:
        """Internal no-migration construction; not compatibility authorization.

        The host authenticates paths and exclusively owns the writer lease. This
        method neither creates a missing database nor changes its journal mode.
        """
        store = cls.__new__(cls)
        store._migration_allowed = False
        store._format_state = None
        store._format_identity = None
        try:
            store.db = sqlite3.connect(
                path.absolute().as_uri() + "?mode=rw&cache=private",
                uri=True,
                check_same_thread=False,
                timeout=0,
                isolation_level="DEFERRED",
            )
        except sqlite3.Error:
            raise ExistingStoreError("existing_store_unavailable") from None
        try:
            format_limits = (
                FormatReadLimits(seconds=limits.seconds, vm_steps=limits.vm_steps)
                if limits is not None
                else None
            )
            evidence = read_optional_format_evidence(store.db, limits=format_limits)
            if evidence is not None:
                store._admit_format(evidence)
            # No SQL, even a connection PRAGMA, precedes bounded validation.
            validate_existing_schema(store.db, limits=limits)
            store._configure_existing(durable=durable)
            return store
        except (FormatEvidenceError, FormatAdmissionError) as error:
            store.db.close()
            codes = {
                "format_read_timeout": "existing_store_read_budget",
                "format_query_budget": "existing_store_read_budget",
                "format_cleanup_failed": "existing_store_cleanup_failed",
                "format_read_failed": "existing_store_unavailable",
                "format_isolation_unsupported": "existing_store_isolation_unsupported",
                "format_connection_unsupported": "existing_store_connection_unsupported",
                "format_transaction_active": "existing_store_outer_transaction",
            }
            code = codes.get(error.code, error.code)
            mapped = ExistingStoreError(code)
            if isinstance(error, FormatEvidenceError):
                error.copy_observation_to(mapped)
            raise mapped from None
        except sqlite3.Error:
            store.db.close()
            raise ExistingStoreError("existing_store_unavailable") from None
        except BaseException:
            store.db.close()
            raise

    @property
    def migration_allowed(self) -> bool:
        return self._migration_allowed

    def _configure_existing(self, *, durable: bool) -> None:
        self._configure_connection(durable=durable)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=3000")

    def _configure_connection(self, *, durable: bool) -> None:
        self.db.row_factory = sqlite3.Row
        if durable:
            # This is a connection policy, not a persistent journal-mode change.
            self.db.execute("PRAGMA synchronous=FULL")
            if self.db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise ValueError("store_durability_unsupported")

    def _initialize(self, *, durable: bool) -> None:
        self._configure_connection(durable=durable)
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=3000;
        """
            + CORE_SCHEMA
        )
        # The expression is shared with the page predicate/order. The kind prefix
        # lets SQLite choose this partial index over the older updated index.
        self.db.execute(SESSION_DIRECTORY_SCHEMA)
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

    def bounded_object(
        self, kind: str, identifier: str, *, max_bytes: int
    ) -> dict[str, Any] | None:
        """Point-read complete JSON only after SQLite has checked its byte budget."""
        if type(max_bytes) is not int or not 1 <= max_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("对象字节预算必须是正整数")
        row = self.db.execute(
            "SELECT length(CAST(data AS BLOB)) AS size,"
            "CASE WHEN typeof(data)='text' AND length(CAST(data AS BLOB))<=? "
            "THEN CAST(data AS BLOB) END AS data FROM objects WHERE kind=? AND id=?",
            (max_bytes, kind, identifier),
        ).fetchone()
        if row is None:
            return None
        if row["size"] > max_bytes:
            raise EvidenceError("output_budget", "完整持久对象超过核查字节预算")
        try:
            raw = row["data"]
            if raw is None:
                raise ValueError
            item = json.loads(raw.decode("utf-8"))
            if not isinstance(item, dict) or item.get("id") != identifier:
                raise ValueError
            # Digests and tool output must also be valid UTF-8, including escaped strings.
            json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise EvidenceError("content_unavailable", "持久对象无法完整核查") from exc
        return item

    def message_evidence_window(
        self, session_id: str, *, limit: int, metadata_bytes: int
    ) -> builtins.list[dict[str, Any]]:
        """Take raw indexed mappings first: broken rows consume the finite window."""
        if type(limit) is not int or not 1 <= limit <= _SESSION_RECORD_LIMIT:
            raise ValueError("消息窗口需要 1–101 条映射")
        if type(metadata_bytes) is not int or not 1 <= metadata_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("映射字节预算必须是正整数")
        rows = self.db.execute(
            "SELECT seq,CASE WHEN typeof(message_id)='text' "
            "AND length(CAST(message_id AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(message_id AS BLOB) END "
            "AS message_id FROM message_order WHERE session_id=? ORDER BY seq DESC LIMIT ?",
            (metadata_bytes, session_id, limit),
        )
        mappings: builtins.list[dict[str, Any]] = []
        for row in rows:
            mapping = dict(row)
            raw = mapping["message_id"]
            try:
                mapping["message_id"] = raw.decode("utf-8") if raw is not None else None
            except UnicodeError:
                mapping["message_id"] = None
            mappings.append(mapping)
        return mappings

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

    def attachment_record_rows(
        self, *, before_id: str | None, limit: int, metadata_bytes: int
    ) -> Generator[dict[str, Any], None, None]:
        """Seek the attachment primary key before reading a bounded metadata window."""
        if type(limit) is not int or not 1 <= limit <= _ATTACHMENT_RECORD_LIMIT:
            raise ValueError("附件目录查询需要 1–201 条的有界窗口")
        if type(metadata_bytes) is not int or metadata_bytes < 1:
            raise ValueError("附件目录元数据字节预算必须是正整数")
        predicate = "AND id<? " if before_id is not None else ""
        bounds = (before_id,) if before_id is not None else ()
        # Do not filter JSON before LIMIT: a rare match would scan the whole
        # directory. Nor parse unbounded JSON inside SQLite to project fields.
        cursor = self.db.execute(
            "SELECT substr(CAST(id AS BLOB),1,33) AS id,"
            "length(CAST(id AS BLOB)) AS id_bytes,"
            "substr(CAST(data AS BLOB),1,min(?,length(CAST(data AS BLOB)))) AS metadata,"
            "length(CAST(data AS BLOB)) AS metadata_bytes "
            "FROM objects WHERE kind='attachment' " + predicate + "ORDER BY id DESC LIMIT ?",
            (min(metadata_bytes, SQLITE_MAX_INTEGER), *bounds, limit),
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
        message = self._message(session_id, role, content, **extra)
        self.put("message", message)
        return message

    def complete_message(self, session_id: str, content: str, **extra: Any) -> dict[str, Any]:
        """Commit a model response and the consumer wakeup evidence as one fact."""
        message = self._message(session_id, "assistant", content, **extra)
        self.event(
            "message.completed", {"message": message}, session_id, objects=[("message", message)]
        )
        return message

    @staticmethod
    def _message(session_id: str, role: str, content: str, **extra: Any) -> dict[str, Any]:
        message = {
            "id": new_id(),
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": now(),
            **extra,
        }
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

    def existing_main_session_id(self) -> str | None:
        """Read the identity marker without constructing or loading a session."""
        row = self.db.execute(
            "SELECT CASE WHEN typeof(value)='text' "
            "AND length(CAST(value AS BLOB)) BETWEEN 1 AND 96 THEN value END "
            "FROM runtime_state WHERE name='main_session'"
        ).fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None

    def main_session(self, mode: str = "auto") -> dict[str, Any]:
        """One durable local identity; old conversation sessions remain accessible."""
        if not self.migration_allowed:
            return read_existing_main(self.db, self.existing_main_session_id())
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
        destination: tuple[str, str, str] | None = None
        project_key: str | None = None
        batch: builtins.list[tuple[sqlite3.Row, dict[str, Any]]] = []
        rows = self.db.execute(
            "SELECT * FROM ingress WHERE session_id=? AND state='pending' ORDER BY seq LIMIT ?",
            (session_id, limit),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            origin = ReplyOrigin.from_input(payload)
            routing = FrozenRouting.load(payload.get("attention_routing"))
            key = (
                *origin.destination_key,
                routing.batch_key if "attention_routing" in payload else "",
            )
            if destination is not None and (
                key != destination or ("project_review" in payload) != (project_key is not None)
            ):
                break  # Another recipient or execution segment waits for the next boundary.
            marker_key: str | None = None
            if "project_review" in payload:
                from muli.project_review import project_review_key

                marker_key = project_review_key(payload["project_review"])
            if destination is not None and marker_key != project_key:
                break  # Even local projects must retain their complete, distinct host facts.
            destination = key
            project_key = marker_key
            batch.append((row, payload))
        if any("project_review" in payload for _, payload in batch):
            from muli.project_review import require_project_review_format

            # The bounded evidence reader owns its read transaction. Check this
            # exact batch synchronously before any archive or delivery DML.
            require_project_review_format(self)
        with self.db:
            for row, payload in batch:
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
        if "attention_routing" in payload:
            message["attention_routing"] = FrozenRouting.load(payload["attention_routing"]).dump()
        if "project_review" in payload:
            from muli.project_review import validate_project_review

            message["project_review"] = validate_project_review(payload["project_review"])
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
        notification: dict[str, Any] | None = None,
    ) -> bool:
        """Persist envelope, notice and its event atomically before upstream ACK.

        ``notification`` is host-produced metadata, never a remote envelope field.
        The caller only wakes the bus after this transaction; duplicates do not
        append another event.
        """
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
            notice = (
                notification
                if notification is not None
                else {
                    "id": identifier,
                    "title": str(envelope.get("type", "chiliarch")),
                    "content": json.dumps(envelope.get("payload", {}), ensure_ascii=False),
                    "source": "chiliarch",
                    "read": False,
                    "created_at": now(),
                }
            )
            self.db.execute(
                "INSERT INTO objects VALUES('notification',?,?,?)",
                (notice["id"], json.dumps(notice, ensure_ascii=False), now()),
            )
            self._event(
                "notification.created",
                {
                    "id": notice["id"],
                    "source": notice["source"],
                    "type": envelope["type"],
                    **{
                        field: notice[field]
                        for field in ("created_at", "producer", "attention")
                        if field in notice
                    },
                },
            )
        return True
