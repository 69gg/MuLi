"""Small transactional journal shared by sessions, agents and inbound events."""

from __future__ import annotations

import builtins
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import closing
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
from muli.management_transaction import _object
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store_initialization import (
    CreatedDatabase,
    StoreConstructionLock,
    StoreInitializationError,
)
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
_CONTINUATION_RECORD_LIMIT = 1001  # max_search_results permits at most 1000 candidates.


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
        self._runtime_lock: StoreConstructionLock | None = None
        try:
            with StoreConstructionLock(path) as lock:
                self._open_locked(lock, durable=durable)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            raise

    @classmethod
    def open_runtime(cls, path: Path, *, default_mode: str, durable: bool = False) -> Store:
        """Hold cooperative Runtime ownership until this Store is closed.

        Initialize/certify only a database exclusively created by this call.
        Existing unmarked data cannot start a Runtime. No empty-file or schema
        heuristic can turn it into a newly created, format-certified database.
        """
        store = cls.__new__(cls)
        store._runtime_lock = None
        try:
            store._runtime_lock = StoreConstructionLock(path).__enter__()
            store._open_locked(store._runtime_lock, durable=durable, runtime_mode=default_mode)
        except BaseException:
            store.close()
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
            if creation is not None:
                creation.check_pristine()
            self.db = sqlite3.connect(
                lock.database.as_uri() + "?mode=rw&cache=private",
                uri=True,
                check_same_thread=False,
                timeout=0,
                isolation_level="DEFERRED",
            )
            # Only this call's pristine O_EXCL claim can omit classification.
            # Existing files still pass the reader before any ordinary write.
            evidence = read_optional_format_evidence(self.db) if creation is None else None
            if evidence is not None:
                self._admit_format(evidence)
                self._configure_existing(durable=durable)
            else:
                if runtime_mode is not None and creation is None:
                    raise FormatAdmissionError("store_format_upgrade_required")
                if creation is not None:
                    creation.check_pristine()
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
        if self._format_state is not None and evidence.state != self._format_state:
            raise FormatAdmissionError("store_format_changed")
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
        runtime_owner: bool = False,
    ) -> Store:
        """Internal no-migration construction; not compatibility authorization.

        The host authenticates paths and exclusively owns the writer lease. This
        method neither creates a missing database nor changes its journal mode.
        A supported Runtime additionally retains the cooperative constructor
        lock; ordinary existing-store readers keep their original behavior.
        """
        store = cls.__new__(cls)
        store._runtime_lock = None
        store._migration_allowed = False
        store._format_state = None
        store._format_identity = None
        if runtime_owner:
            try:
                store._runtime_lock = StoreConstructionLock(path).__enter__()
            except StoreInitializationError as error:
                # No SQLite connection has been attempted. The lock factory
                # closes its descriptor before reporting this fixed failure.
                raise ExistingStoreError(error.code) from None
            # Resolve once with the lock. A changed caller symlink must not
            # redirect this connection to another, unlocked database.
            path = store._runtime_lock.database
        try:
            if store._runtime_lock is not None:
                store._runtime_lock.check()
            store.db = sqlite3.connect(
                path.absolute().as_uri() + "?mode=rw&cache=private",
                uri=True,
                check_same_thread=False,
                timeout=0,
                isolation_level="DEFERRED",
            )
            if store._runtime_lock is not None:
                store._runtime_lock.check()
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
            if store._runtime_lock is not None:
                store._runtime_lock.check()
            return store
        except (FormatEvidenceError, FormatAdmissionError) as error:
            store.close()
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
            store.close()
            raise ExistingStoreError("existing_store_unavailable") from None
        except BaseException:
            store.close()
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
        # A partly opened factory has no connection yet. Once one exists,
        # closing it must succeed before another Runtime may acquire this lock.
        if hasattr(self, "db"):
            self.db.close()
        if self._runtime_lock is not None:
            self._runtime_lock.__exit__()
            self._runtime_lock = None

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
        self, kind: str, identifier: str, *, max_bytes: int, strict_json: bool = False
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
            item = _object(bytes(raw)) if strict_json else json.loads(raw.decode("utf-8"))
            if not isinstance(item, dict) or item.get("id") != identifier:
                raise ValueError
            # Digests and tool output must also be valid UTF-8, including escaped strings.
            json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise EvidenceError("content_unavailable", "持久对象无法完整核查") from exc
        return item

    def ingress_audit_window(
        self, *, before_seq: int | None, limit: int
    ) -> Generator[int, None, None]:
        """Seek a finite ingress key window before inspecting any envelope."""
        if type(limit) is not int or not 1 <= limit <= _SESSION_RECORD_LIMIT:
            raise ValueError("入站核查窗口超过既有候选上限")
        if before_seq is not None and (
            type(before_seq) is not int or not 1 <= before_seq <= SQLITE_MAX_INTEGER
        ):
            raise ValueError("入站核查续页位置无效")
        predicate = " WHERE seq<?" if before_seq is not None else ""
        bounds = (before_seq, limit) if before_seq is not None else (limit,)
        with closing(
            self.db.execute(
                "SELECT seq FROM ingress" + predicate + " ORDER BY seq DESC LIMIT ?", bounds
            )
        ) as rows:
            for row in rows:
                yield int(row[0])

    def ingress_audit_probe(
        self,
        main_id: str,
        *,
        seq: int | None = None,
        input_id: str | None = None,
        cached_parents: tuple[str, ...] = (),
        max_bytes: int,
    ) -> dict[str, Any] | None:
        """Size the entire next unit in SQL, returning identities only if it fits.

        The parent lookup is a primary-key point read, with no JSON extraction.
        Cached parents have already consumed this page's raw budget. Even the
        relational identity strings stay in SQLite when the remaining budget
        cannot contain the complete payload, parent and identity working set.
        """
        if type(max_bytes) is not int or not 1 <= max_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("入站核查字节预算必须是正整数")
        if (seq is None) == (input_id is None) or len(cached_parents) >= _SESSION_RECORD_LIMIT:
            raise ValueError("入站核查定位或父对象缓存无效")
        if seq is not None and (type(seq) is not int or not 1 <= seq <= SQLITE_MAX_INTEGER):
            raise ValueError("入站核查序号无效")
        if input_id is not None and (not isinstance(input_id, str) or not input_id):
            raise ValueError("入站核查主键无效")
        predicate, identifier = ("seq=?", seq) if seq is not None else ("id=?", input_id)
        cached = (
            " OR session_id IN (" + ",".join("?" for _ in cached_parents) + ")"
            if cached_parents
            else ""
        )
        parent_needed = "NOT (session_id=?" + cached + ")"
        query = (
            "WITH candidate AS (SELECT *,typeof(id) AS id_type,"
            "typeof(session_id) AS session_id_type,typeof(state) AS state_type,"
            "length(CAST(id AS BLOB)) AS id_bytes,"
            "length(CAST(session_id AS BLOB)) AS session_id_bytes,"
            "length(CAST(state AS BLOB)) AS state_bytes,"
            "typeof(payload) AS payload_type,length(CAST(payload AS BLOB)) AS payload_bytes,"
            + parent_needed
            + " AS parent_needed FROM ingress WHERE "
            + predicate
            + "), sized AS (SELECT *,"
            "CASE WHEN parent_needed THEN (SELECT length(CAST(data AS BLOB)) "
            "FROM objects WHERE kind='agent' AND objects.id=candidate.session_id) END AS parent_bytes,"
            "CASE WHEN parent_needed THEN (SELECT typeof(data) FROM objects "
            "WHERE kind='agent' AND objects.id=candidate.session_id) END AS parent_type "
            "FROM candidate), unit AS (SELECT *,"
            "id_bytes+session_id_bytes+state_bytes+payload_bytes+coalesce(parent_bytes,0) "
            "AS required_bytes FROM sized) SELECT seq,id_type,session_id_type,state_type,"
            "id_bytes,session_id_bytes,state_bytes,payload_type,payload_bytes,"
            "parent_needed,parent_bytes,parent_type,required_bytes,"
            "CASE WHEN required_bytes<=? THEN CAST(id AS BLOB) END AS id,"
            "CASE WHEN required_bytes<=? THEN CAST(session_id AS BLOB) END AS session_id,"
            "CASE WHEN required_bytes<=? THEN CAST(state AS BLOB) END AS state,"
            "CASE WHEN typeof(received) IN ('integer','real') THEN received END AS received,"
            "typeof(delivered_seq) AS delivered_type,"
            "CASE WHEN typeof(delivered_seq)='integer' THEN delivered_seq END AS delivered_seq "
            "FROM unit"
        )
        with closing(
            self.db.execute(
                query, (main_id, *cached_parents, identifier, max_bytes, max_bytes, max_bytes)
            )
        ) as rows:
            row = rows.fetchone()
            return dict(row) if row is not None else None

    def ingress_audit_bytes(
        self, seq: int, *, parent: bool = False, max_bytes: int
    ) -> bytes | None:
        """Read complete original bytes after a SQL byte gate; never parse a prefix."""
        if type(seq) is not int or not 1 <= seq <= SQLITE_MAX_INTEGER:
            raise ValueError("入站核查序号无效")
        if type(max_bytes) is not int or not 1 <= max_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("入站核查字节预算必须是正整数")
        column, source = (
            (
                "data",
                "objects WHERE kind='agent' AND id=(SELECT session_id FROM ingress WHERE seq=?)",
            )
            if parent
            else ("payload", "ingress WHERE seq=?")
        )
        with closing(
            self.db.execute(
                f"SELECT typeof({column}) AS storage_type,length(CAST({column} AS BLOB)) AS size,"
                f"CASE WHEN typeof({column})='text' AND length(CAST({column} AS BLOB))<=? "
                f"THEN CAST({column} AS BLOB) END AS data FROM {source}",
                (max_bytes, seq),
            )
        ) as rows:
            row = rows.fetchone()
        if row is None:
            return None
        if type(row["size"]) is not int:
            raise EvidenceError("content_unavailable", "入站核查对象无法完整读取")
        if row["size"] > max_bytes:
            raise EvidenceError("output_budget", "完整入站核查对象超过字节预算")
        raw = row["data"]
        if row["storage_type"] != "text" or not isinstance(raw, bytes):
            raise EvidenceError("content_unavailable", "入站核查对象无法完整读取")
        return raw

    def commitment_directory_window(
        self, *, before_id: str | None, limit: int, key_bytes: int
    ) -> Generator[dict[str, Any], None, None]:
        """Stream a finite raw-key seek; gate each complete key inside SQLite."""
        if type(limit) is not int or not 1 <= limit <= _CONTINUATION_RECORD_LIMIT:
            raise ValueError("账本目录窗口超过既有候选上限")
        if type(key_bytes) is not int or not 1 <= key_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("账本目录主键字节预算必须是正整数")
        predicate = ""
        bounds: tuple[str, ...] = ()
        if before_id is not None:
            if not isinstance(before_id, str) or not before_id:
                raise ValueError("账本目录续页位置必须是完整主键")
            predicate = " AND objects.id<?"
            bounds = (before_id,)
        # No JSON projection or status filter precedes the finite key window.
        # Yielding keeps only the current bounded key in Python, even when the
        # configured candidate cap is larger than the public item page.
        rows = self.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(id AS BLOB) END AS id FROM objects WHERE kind='commitment'"
            + predicate
            + " ORDER BY objects.id DESC LIMIT ?",
            (key_bytes, *bounds, limit),
        )
        try:
            for row in rows:
                yield dict(row)
        finally:
            rows.close()

    def message_evidence_window(
        self,
        session_id: str,
        *,
        limit: int,
        metadata_bytes: int,
        through_seq: int | None = None,
        before_seq: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Seek raw mappings first; optional sequence bounds never filter on JSON."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("消息窗口需要明确的非空会话身份")
        return self._message_mapping_window(
            session_id,
            limit=limit,
            metadata_bytes=metadata_bytes,
            through_seq=through_seq,
            before_seq=before_seq,
        )

    def history_evidence_window(
        self,
        *,
        limit: int,
        metadata_bytes: int,
        through_seq: int | None = None,
        before_seq: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Seek one global raw mapping window, before any scope or JSON inspection."""
        return self._message_mapping_window(
            None,
            limit=limit,
            metadata_bytes=metadata_bytes,
            through_seq=through_seq,
            before_seq=before_seq,
        )

    def _message_mapping_window(
        self,
        session_id: str | None,
        *,
        limit: int,
        metadata_bytes: int,
        through_seq: int | None,
        before_seq: int | None,
    ) -> builtins.list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= _SESSION_RECORD_LIMIT:
            raise ValueError("消息窗口需要 1–101 条映射")
        if type(metadata_bytes) is not int or not 1 <= metadata_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("映射字节预算必须是正整数")
        predicate = " WHERE session_id=?" if session_id is not None else " WHERE 1=1"
        arguments: tuple[str | int, ...] = (session_id,) if session_id is not None else ()
        if through_seq is not None:
            if type(through_seq) is not int or not 0 <= through_seq <= SQLITE_MAX_INTEGER:
                raise ValueError("映射上界必须是有效的非负序号")
            predicate += " AND seq<=?"
            arguments += (through_seq,)
        if before_seq is not None:
            if type(before_seq) is not int or not 1 <= before_seq <= SQLITE_MAX_INTEGER:
                raise ValueError("映射续页位置必须是有效的正序号")
            predicate += " AND seq<?"
            arguments += (before_seq,)
        session_column = (
            "CASE WHEN typeof(session_id)='text' "
            "AND length(CAST(session_id AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(session_id AS BLOB) END AS session_id,"
            if session_id is None
            else ""
        )
        metadata_arguments = (metadata_bytes,) * (2 if session_id is None else 1)
        rows = self.db.execute(
            "SELECT seq," + session_column + "CASE WHEN typeof(message_id)='text' "
            "AND length(CAST(message_id AS BLOB)) BETWEEN 1 AND ? "
            "THEN CAST(message_id AS BLOB) END "
            "AS message_id FROM message_order" + predicate + " ORDER BY seq DESC LIMIT ?",
            (*metadata_arguments, *arguments, limit),
        )
        mappings: builtins.list[dict[str, Any]] = []
        for row in rows:
            mapping = dict(row)
            for name in ("session_id", "message_id") if session_id is None else ("message_id",):
                raw = mapping[name]
                try:
                    mapping[name] = raw.decode("utf-8") if raw is not None else None
                except UnicodeError:
                    mapping[name] = None
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

    def continuation_rows(
        self,
        main_id: str,
        *,
        after_rowid: int,
        limit: int,
        metadata_bytes: int,
    ) -> Generator[dict[str, Any], None, None]:
        """Limit the indexed parent window before projecting agent/run metadata."""
        if type(after_rowid) is not int or not 0 <= after_rowid <= SQLITE_MAX_INTEGER:
            raise ValueError("延续目录行游标必须是有效非负整数")
        if type(limit) is not int or not 1 <= limit <= _CONTINUATION_RECORD_LIMIT:
            raise ValueError("延续目录需要有限候选窗口及一个前瞻")
        if type(metadata_bytes) is not int or not 1 <= metadata_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("延续目录元数据字节预算必须是正整数")
        columns = [
            "a.rowid AS rowid",
            "typeof(a.id) AS agent_key_type",
            "length(CAST(a.id AS BLOB)) AS agent_key_bytes",
            "CASE WHEN typeof(a.id)='text' AND length(CAST(a.id AS BLOB))<=:budget "
            "THEN CAST(a.id AS BLOB) END AS agent_key",
            "CASE WHEN json_type(a.data,'$.status')='text' "
            "AND json_extract(a.data,'$.status') IN "
            "('queued','running','paused','interrupted','cancelled','failed') "
            "THEN CAST(json_extract(a.data,'$.status') AS BLOB) END AS status",
        ]
        for field, name in (
            ("id", "agent_id"),
            ("session_id", "session_id"),
            ("run_id", "run_id"),
            ("commitment_id", "commitment_id"),
        ):
            field_type = f"json_type(a.data,'$.{field}')"
            value = f"json_extract(a.data,'$.{field}')"
            columns.extend(
                (
                    f"({field_type}) AS {name}_type",
                    f"length(CAST(({value}) AS BLOB)) AS {name}_bytes",
                    f"CASE WHEN ({field_type})='text' "
                    f"AND length(CAST(({value}) AS BLOB))<=:budget "
                    f"THEN CAST(({value}) AS BLOB) END AS {name}",
                )
            )
        for field in ("generation", "completion_revision"):
            value = f"json_extract(a.data,'$.{field}')"
            columns.append(
                f"CASE WHEN json_type(a.data,'$.{field}')='integer' "
                f"AND typeof({value})='integer' AND {value}>=0 AND {value}<:max_integer "
                f"THEN {value} END AS {field}"
            )
        cursor = self.db.execute(
            "WITH candidates AS (SELECT "
            + ",".join(columns)
            + " FROM objects a WHERE kind='agent' "
            "AND json_extract(data,'$.parent_id')=:main_id "
            "AND rowid>:after_rowid ORDER BY rowid LIMIT :limit) "
            "SELECT a.*,CASE WHEN json_valid(r.data) THEN CASE "
            "WHEN json_type(r.data,'$.id')='text' "
            "AND json_extract(r.data,'$.id')=r.id "
            "AND json_type(r.data,'$.session_id')='text' "
            "AND CAST(json_extract(r.data,'$.session_id') AS BLOB)=a.session_id "
            "THEN 1 ELSE 0 END ELSE 0 END AS run_associated"
            + " FROM candidates a LEFT JOIN objects r ON r.kind='run' "
            "AND r.id=CAST(a.run_id AS TEXT) ORDER BY a.rowid",
            {
                "main_id": main_id,
                "after_rowid": after_rowid,
                "limit": limit,
                "budget": metadata_bytes,
                "max_integer": SQLITE_MAX_INTEGER,
            },
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

    def channel_history_rows(
        self,
        session_id: str,
        *,
        name: str,
        account_id: str,
        owner_id: str,
        before_seq: int | None,
        limit: int,
        metadata_bytes: int,
    ) -> Generator[dict[str, Any], None, None]:
        """Take one indexed input window, then prove its exact private-chat scope."""
        if type(limit) is not int or not 1 <= limit <= _SESSION_RECORD_LIMIT:
            raise ValueError("渠道历史需要 1–101 条的有限候选窗口")
        if type(metadata_bytes) is not int or not 1 <= metadata_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("渠道历史元数据字节预算必须是正整数")
        if before_seq is not None and (
            type(before_seq) is not int or not 1 <= before_seq <= SQLITE_MAX_INTEGER
        ):
            raise ValueError("渠道历史序列游标无效")

        def private_route(data: str, path: str) -> str:
            return " AND ".join(
                f"json_type({data},'$.{path}.{field}')='text' "
                f"AND json_extract({data},'$.{path}.{field}')={value}"
                for field, value in (
                    ("name", ":name"),
                    ("account_id", ":account_id"),
                    ("sender_id", ":owner_id"),
                    ("target", ":owner_id"),
                    ("kind", "'private'"),
                )
            )

        def owner_input(data: str, key: str) -> str:
            return (
                f"json_type({data},'$.id')='text' AND json_extract({data},'$.id')={key} "
                f"AND json_type({data},'$.session_id')='text' "
                f"AND (json_extract({data},'$.session_id')=:session_id OR ("
                f"json_extract({data},'$.channel_ownership.main_id')=:session_id "
                f"AND json_extract({data},'$.channel_ownership.agent_id')=json_extract({data},'$.session_id') "
                f"AND EXISTS(SELECT 1 FROM objects child WHERE child.kind='agent' "
                f"AND child.id=json_extract({data},'$.session_id') "
                f"AND json_extract(child.data,'$.parent_id')=:session_id))) "
                f"AND json_extract({data},'$.role')='user' "
                f"AND json_extract({data},'$.source')=:source "
                f"AND json_extract({data},'$.principal')='owner' "
                f"AND ({private_route(data, 'route')}) "
                f"AND (json_type({data},'$.route.reply_to') IS NULL "
                f"OR (json_type({data},'$.route.reply_to')='text' "
                f"AND length(json_extract({data},'$.route.reply_to'))>0))"
            )

        incoming = owner_input("o.data", "o.message_key")
        # Reuse the evidence projection with a zero-byte content window. The CTE
        # supplies an already byte-gated ID and valid JSON, including broken rows.
        evidence_columns = (
            _EVIDENCE_COLUMNS.replace("m.seq", "o.seq")
            .replace("?", "1", 1)
            .replace("?", "0", 1)
            .replace("?", ":budget")
        )
        columns = (
            evidence_columns
            + ",o.id_type,o.id_bytes,"
            + "json_extract(o.data,'$.session_id') AS actual_session_id,"
            + f"CASE WHEN ({incoming}) THEN 'user' END AS channel_role,"
            "CASE WHEN json_type(o.data,'$.created_at') IN ('integer','real') "
            "THEN json_extract(o.data,'$.created_at') END AS created_at,"
            "json_type(o.data,'$.route.reply_to') AS platform_message_id_type,"
            "length(CAST(json_extract(o.data,'$.route.reply_to') AS BLOB)) "
            "AS platform_message_id_bytes,"
            "CASE WHEN json_type(o.data,'$.route.reply_to')='text' "
            "THEN substr(CAST(json_extract(o.data,'$.route.reply_to') AS BLOB),1,"
            "min(:budget,length(CAST(json_extract(o.data,'$.route.reply_to') AS BLOB)))) "
            "END AS platform_message_id"
        )
        delegated = (
            self.db.execute(
                "SELECT 1 FROM objects WHERE kind='channel_ownership' "
                "AND json_extract(data,'$.main_id')=? LIMIT 1",
                (session_id,),
            ).fetchone()
            is not None
        )
        # Once delegation has existed, a bounded global sequence window also
        # includes its original child sessions. Nonmatching records consume the
        # page; neither history bodies nor a second conversation are copied.
        candidate_scope = "" if delegated else "session_id=:session_id AND "
        cursor = self.db.execute(
            "WITH candidates AS (SELECT seq,message_id FROM message_order "
            "WHERE "
            + candidate_scope
            + ("seq<:before_seq " if before_seq is not None else "1=1 ")
            + "ORDER BY seq DESC LIMIT :limit),records AS ("
            "SELECT m.seq,m.message_id AS message_key,typeof(m.message_id) AS id_type,"
            "length(CAST(m.message_id AS BLOB)) AS id_bytes,"
            "CASE WHEN typeof(m.message_id)='text' "
            "AND length(CAST(m.message_id AS BLOB))<=:budget "
            "THEN CAST(m.message_id AS BLOB) END AS id,"
            "CASE WHEN json_valid(v.data) THEN v.data ELSE '{}' END AS data "
            "FROM candidates m LEFT JOIN objects v ON v.kind='message' "
            "AND v.id=m.message_id) SELECT " + columns + " FROM records o ORDER BY o.seq DESC",
            {
                "session_id": session_id,
                "name": name,
                "account_id": account_id,
                "owner_id": owner_id,
                "source": f"channel:{name}:{account_id}",
                "before_seq": before_seq,
                "limit": limit,
                "budget": metadata_bytes,
            },
        )
        try:
            for row in cursor:
                yield dict(row)
        finally:
            cursor.close()

    def channel_delivery_rows(
        self,
        *,
        name: str,
        account_id: str,
        owner_id: str,
        before_rowid: int | None,
        limit: int,
        metadata_bytes: int,
    ) -> Generator[dict[str, Any], None, None]:
        """Classify a finite outbox rowid window without exporting payloads or routes."""
        if type(limit) is not int or not 1 <= limit <= _SESSION_RECORD_LIMIT:
            raise ValueError("渠道投递历史需要 1–101 条的有限候选窗口")
        if type(metadata_bytes) is not int or not 1 <= metadata_bytes <= SQLITE_MAX_INTEGER:
            raise ValueError("渠道投递元数据字节预算必须是正整数")
        if before_rowid is not None and (
            type(before_rowid) is not int or not 1 <= before_rowid <= SQLITE_MAX_INTEGER
        ):
            raise ValueError("渠道投递序列游标无效")
        route_match = " AND ".join(
            f"json_type(o.route_data,'$.{field}')='text' "
            f"AND json_extract(o.route_data,'$.{field}')={value}"
            for field, value in (
                ("name", ":name"),
                ("account_id", ":account_id"),
                ("sender_id", ":owner_id"),
                ("target", ":owner_id"),
                ("kind", "'private'"),
            )
        )
        predicate = "WHERE rowid<:before_rowid " if before_rowid is not None else ""
        # JSON stays in SQLite. Both text and attachment references may have many
        # parts; only the complete byte-gated reference list crosses into Python.
        part_data = "CASE WHEN p.type='object' THEN p.value ELSE '{}' END"
        valid_part = (
            f"p.type='object' AND ((json_type({part_data},'$.text')='text' "
            f"AND json_type({part_data},'$.attachment_id') IS NULL) OR "
            f"(json_type({part_data},'$.text') IS NULL "
            f"AND json_type({part_data},'$.attachment_id')='text'))"
        )
        cursor = self.db.execute(
            "WITH candidates AS (SELECT rowid AS seq,id,name,route,payload,state,"
            "next_part,created_at FROM channel_outbox "
            + predicate
            + "ORDER BY rowid DESC LIMIT :limit),records AS ("
            "SELECT c.*,CASE WHEN json_valid(c.route) THEN c.route ELSE '{}' END AS route_data,"
            "CASE WHEN json_valid(c.payload) THEN c.payload ELSE '{}' END AS data "
            "FROM candidates c),scoped AS (SELECT o.seq,o.id,o.state,o.next_part,o.created_at,"
            f"CASE WHEN o.name=:name AND ({route_match}) "
            "THEN o.data ELSE '{}' END AS data,"
            f"CASE WHEN o.name=:name AND ({route_match}) THEN 1 ELSE 0 END AS channel_match "
            "FROM records o),projected AS (SELECT o.*,"
            "json_type(o.data,'$.parts') AS parts_type,"
            "json_array_length(o.data,'$.parts') AS total_parts,"
            f"(SELECT count(*) FROM json_each(o.data,'$.parts') p "
            f"WHERE CASE WHEN ({valid_part}) THEN 0 ELSE 1 END=1) "
            "AS invalid_parts,"
            f"(SELECT json_group_array(json_extract({part_data},'$.attachment_id')) "
            "FROM json_each(o.data,'$.parts') p "
            f"WHERE json_type({part_data},'$.attachment_id') IS NOT NULL) AS attachment_data "
            "FROM scoped o) SELECT seq,channel_match,typeof(id) AS id_type,"
            "length(CAST(id AS BLOB)) AS id_bytes,"
            "CASE WHEN typeof(id)='text' THEN substr(CAST(id AS BLOB),1,"
            "min(:budget,length(CAST(id AS BLOB)))) END AS id,"
            "typeof(state) AS state_type,length(CAST(state AS BLOB)) AS state_bytes,"
            "CASE WHEN typeof(state)='text' THEN substr(CAST(state AS BLOB),1,"
            "min(:budget,length(CAST(state AS BLOB)))) END AS state,"
            "CASE WHEN typeof(next_part)='integer' THEN next_part END AS confirmed_parts,"
            "CASE WHEN typeof(created_at) IN ('integer','real') THEN created_at END AS created_at,"
            "parts_type,total_parts,invalid_parts,'array' AS attachment_ids_type,"
            "length(CAST(attachment_data AS BLOB)) AS attachment_ids_bytes,"
            "substr(CAST(attachment_data AS BLOB),1,"
            "min(:budget,length(CAST(attachment_data AS BLOB)))) AS attachment_ids "
            "FROM projected ORDER BY seq DESC",
            {
                "name": name,
                "account_id": account_id,
                "owner_id": owner_id,
                "before_rowid": before_rowid,
                "limit": limit,
                "budget": metadata_bytes,
            },
        )
        try:
            for row in cursor:
                yield dict(row)
        finally:
            cursor.close()

    def channel_delivery_slices(
        self,
        *,
        name: str,
        account_id: str,
        owner_id: str,
        delivery_id: str,
        part: int,
        chunk_bytes: int,
        source_bytes: int,
        metadata_bytes: int,
    ) -> Generator[dict[str, Any], None, None]:
        """Read one proven part in bounded windows within one SQLite read snapshot."""
        for value in (chunk_bytes, source_bytes, metadata_bytes):
            if type(value) is not int or not 1 <= value <= SQLITE_MAX_INTEGER:
                raise ValueError("投递正文窗口与字节上限必须是正整数")
        if chunk_bytes > 64 * 1024:
            raise ValueError("投递正文单窗口不能超过 64KiB")
        if type(part) is not int or not 0 <= part < SQLITE_MAX_INTEGER:
            raise ValueError("投递分段序号无效")
        route_match = " AND ".join(
            f"json_type(o.route_data,'$.{field}')='text' "
            f"AND json_extract(o.route_data,'$.{field}')={value}"
            for field, value in (
                ("name", ":name"),
                ("account_id", ":account_id"),
                ("sender_id", ":owner_id"),
                ("target", ":owner_id"),
                ("kind", "'private'"),
            )
        )
        cursor = self.db.execute(
            "WITH RECURSIVE record AS (SELECT rowid AS seq,name,state,next_part,created_at,"
            "CASE WHEN json_valid(route) THEN route ELSE '{}' END AS route_data,"
            "CASE WHEN json_valid(payload) THEN payload ELSE '{}' END AS data "
            "FROM channel_outbox WHERE id=:delivery_id),selected AS ("
            "SELECT o.seq,o.state,o.next_part,o.created_at,"
            "json_type(o.data,'$.parts') AS parts_type,"
            "json_array_length(o.data,'$.parts') AS total_parts,"
            "json_type(o.data,:part_path) AS part_type,"
            "CASE WHEN json_type(o.data,:part_path)='object' "
            "THEN json_extract(o.data,:part_path) ELSE '{}' END AS part_data "
            f"FROM record o WHERE o.name=:name AND ({route_match})),evidence AS ("
            "SELECT s.*,json_type(part_data,'$.text') AS text_type,"
            "json_type(part_data,'$.attachment_id') AS attachment_id_type,"
            "length(CAST(json_extract(part_data,'$.attachment_id') AS BLOB)) AS attachment_id_bytes,"
            "CASE WHEN json_type(part_data,'$.attachment_id')='text' THEN "
            "substr(CAST(json_extract(part_data,'$.attachment_id') AS BLOB),1,"
            "min(:budget,length(CAST(json_extract(part_data,'$.attachment_id') AS BLOB)))) "
            "END AS attachment_id,"
            "CASE WHEN json_type(part_data,'$.text')='text' "
            "THEN length(CAST(json_extract(part_data,'$.text') AS BLOB)) ELSE 0 END AS total_bytes "
            "FROM selected s),windows(start) AS (SELECT 0 UNION ALL "
            "SELECT start+:chunk FROM windows WHERE start+:chunk < "
            "(SELECT CASE WHEN total_bytes<=:source_bytes THEN total_bytes ELSE 0 END FROM evidence)) "
            "SELECT e.seq,typeof(e.state) AS state_type,"
            "length(CAST(e.state AS BLOB)) AS state_bytes,"
            "CASE WHEN typeof(e.state)='text' THEN substr(CAST(e.state AS BLOB),1,"
            "min(:budget,length(CAST(e.state AS BLOB)))) END AS state,"
            "CASE WHEN typeof(e.next_part)='integer' THEN e.next_part END AS confirmed_parts,"
            "CASE WHEN typeof(e.created_at) IN ('integer','real') THEN e.created_at END AS created_at,"
            "e.parts_type,e.total_parts,e.part_type,e.text_type,e.attachment_id_type,"
            "e.attachment_id_bytes,e.attachment_id,e.total_bytes,w.start AS offset_bytes,"
            "CASE WHEN e.text_type='text' AND e.total_bytes<=:source_bytes THEN "
            "substr(CAST(json_extract(e.part_data,'$.text') AS BLOB),w.start+1,"
            "min(:chunk,e.total_bytes-w.start)) ELSE x'' END AS content "
            "FROM evidence e CROSS JOIN windows w ORDER BY w.start",
            {
                "name": name,
                "account_id": account_id,
                "owner_id": owner_id,
                "delivery_id": delivery_id,
                "part_path": f"$.parts[{part}]",
                "chunk": chunk_bytes,
                "source_bytes": source_bytes,
                "budget": metadata_bytes,
            },
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
        destination: tuple[str, ...] | None = None
        with self.db:
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
                    json.dumps(
                        payload.get("channel_ownership"), sort_keys=True, separators=(",", ":")
                    ),
                    str(payload.get("channel_ownership_control", False)),
                    # A finite host proof freezes one independently revocable
                    # source. Equal routes must not merge different failures.
                    json.dumps(
                        payload["attention_memory_failure"],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if payload.get("principal") == "system:attention"
                    and "attention_memory_failure" in payload
                    else "",
                    json.dumps(
                        payload["attention_memory_watch"],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if payload.get("principal") == "system:attention"
                    and "attention_memory_watch" in payload
                    else "",
                )
                if destination is not None and key != destination:
                    break  # Another recipient waits for the next replay-safe boundary.
                destination = key
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
        if "channel_ownership" in payload:
            message["channel_ownership"] = payload["channel_ownership"]
        if "channel_ownership_control" in payload:
            message["channel_ownership_control"] = payload["channel_ownership_control"]
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
