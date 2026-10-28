"""原文先落盘，单 worker 提炼；检索和主提示缓存不等待后台模型。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from muli.events import EventBus
from muli.evidence import EvidenceError, validate_integer
from muli.maintenance import Admission
from muli.media import EmbeddingPurpose, MediaConfig, RerankResult
from muli.memory import LAYERS, Memory, normalize_entity
from muli.memory_chunks import ChunkPlan, restore_plan
from muli.memory_context import PromptComposition, compose_context_result
from muli.memory_source_chunks import SourceChunks
from muli.memory_source_failure import (
    MemorySourceFailure,
    attachment_revision,
)
from muli.memory_watches import record_source_completion
from muli.proactive import FrozenRouting
from muli.store import SQLITE_MAX_INTEGER, new_id, now

Extractor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
_INDEX_ACTIVE = frozenset({"queued", "processing", "retry"})
_INDEX_TERMINAL = frozenset({"done", "failed", "blocked", "cancelled"})
_INDEX_ERRORS = ("embedding_changed", "index_failed", "index_timeout", "output_budget")
_INDEX_EXECUTION_FIELDS = ("source_index", "item_index", "attempts", "next_attempt")
_INDEX_COUNTERS = (
    "revision",
    "sources_total",
    "sources_finished",
    "vectors_written",
    "already_current",
    "omitted",
    "source_index",
    "item_index",
    "attempts",
)


class MemoryMedia(Protocol):
    config: MediaConfig

    async def embed(
        self, texts: Sequence[str], *, purpose: EmbeddingPurpose = "document"
    ) -> list[list[float]]: ...

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]: ...


@dataclass(frozen=True, slots=True)
class MemoryPipelineConfig:
    enabled: bool = True
    auto_extract: bool = True
    max_pending_jobs: int = 128
    max_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    job_timeout: float = 60.0
    max_source_chars: int = 64000
    max_facts_per_source: int = 16
    graph_hops: int = 2
    max_graph_edges: int = 256
    max_candidates: int = 256
    max_vector_scan: int = 512
    max_community_members: int = 32
    context_max_chars: int = 6000
    context_limit: int = 6
    query_timeout: float = 5.0
    cache_entries: int = 64

    def __post_init__(self) -> None:
        integers = (
            self.max_pending_jobs,
            self.max_attempts,
            self.max_source_chars,
            self.max_facts_per_source,
            self.max_graph_edges,
            self.max_candidates,
            self.max_vector_scan,
            self.max_community_members,
            self.context_limit,
            self.cache_entries,
        )
        if any(type(value) is not int or value < 1 for value in integers):
            raise ValueError("记忆资源限制必须是正整数")
        if type(self.graph_hops) is not int or not 0 <= self.graph_hops <= 8:
            raise ValueError("记忆 graph_hops 必须在 0 到 8 之间")
        if type(self.context_max_chars) is not int or self.context_max_chars < 0:
            raise ValueError("记忆上下文预算不能小于零")
        for value in (self.job_timeout, self.query_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("记忆超时必须是有限正数")
        if not math.isfinite(self.retry_backoff_seconds) or self.retry_backoff_seconds < 0:
            raise ValueError("记忆重试等待必须是有限非负数")
        if type(self.enabled) is not bool or type(self.auto_extract) is not bool:
            raise ValueError("记忆开关必须是布尔值")


class MemoryPipeline:
    def __init__(
        self,
        memory: Memory,
        bus: EventBus | None = None,
        *,
        extractor: Extractor | None = None,
        media: MemoryMedia | None = None,
        config: MemoryPipelineConfig | None = None,
        admission: Admission | None = None,
        max_plan_bytes: int | None = None,
        failure_routing: Callable[[], FrozenRouting | None] | None = None,
    ) -> None:
        self.memory, self.bus = memory, bus
        self.config = config or MemoryPipelineConfig()
        self.admission = admission or Admission()
        self.extractor, self.media = extractor, media
        self.failure_routing = failure_routing
        self.max_plan_bytes = (
            self.config.max_source_chars * 4 if max_plan_bytes is None else max_plan_bytes
        )
        validate_integer(self.max_plan_bytes, "max_plan_bytes", minimum=1)
        self.memory.configure(
            graph_hops=self.config.graph_hops,
            max_graph_edges=self.config.max_graph_edges,
            max_candidates=self.config.max_candidates,
            max_vector_scan=self.config.max_vector_scan,
            max_community_members=self.config.max_community_members,
        )
        self._wake = asyncio.Event()
        self.changed = asyncio.Condition()
        self._worker: asyncio.Task[None] | None = None
        self._media_lock = asyncio.Lock()
        self._cache: OrderedDict[tuple[str, int, int, tuple[str, ...] | None], dict[str, Any]] = (
            OrderedDict()
        )
        self._cache_revision = memory.revision
        self._last_error = ""
        self._index_closed = False
        self.source_chunks = SourceChunks(memory)

    def ingest(self, message: Mapping[str, Any], *, trusted_owner: bool = False) -> dict[str, Any]:
        if not self.config.enabled:
            return {"state": "disabled", "created": False}
        source, created = self.memory.register_source(
            message,
            trusted_owner=trusted_owner,
            max_chars=self.config.max_source_chars,
        )
        if source is None:
            return {"source_id": message.get("id"), "state": "forgotten", "created": False}
        original = source["content"]
        marker = "\n[原文较长；完整内容保存在原始来源]"
        content = original if len(original) <= 12000 else original[: 12000 - len(marker)] + marker
        quote = original[: 12000 - len(marker)] if len(original) > 12000 else original
        item = self.memory.save(
            content,
            kind="observation",
            source=source["source"],
            layer="episodic",
            source_id=source["id"],
            trusted_owner=source["trusted_owner"],
            quote=quote,
            confidence=1.0,
            dedup_key="episodic:" + source["id"],
        )
        self._queue(source["id"])
        job = self.job(source["id"])
        self._wake.set()
        return {
            "source_id": source["id"],
            "memory_id": item["id"],
            "created": created,
            "state": job["state"] if job else "deferred",
        }

    def _pending(self) -> int:
        jobs = int(
            self.memory.store.db.execute(
                "SELECT count(*) FROM memory_jobs WHERE state IN ('queued','processing','retry')"
            ).fetchone()[0]
        )
        plan = self._current_index_plan()
        return jobs + int(plan is not None and plan["state"] in _INDEX_ACTIVE)

    def _queue(self, source_id: str) -> bool:
        if self.job(source_id) is not None or self._pending() >= self.config.max_pending_jobs:
            return False
        timestamp = now()
        with self.memory.store.db:
            cursor = self.memory.store.db.execute(
                "INSERT OR IGNORE INTO memory_jobs VALUES(?,?,?,0,?,'','{}',?,?)",
                (source_id, source_id, "queued", timestamp, timestamp, timestamp),
            )
        return cursor.rowcount == 1

    def _admit_deferred(self) -> None:
        available = self.config.max_pending_jobs - self._pending()
        if available <= 0:
            return
        rows = self.memory.store.db.execute(
            "SELECT id FROM memory_sources WHERE id NOT IN (SELECT source_id FROM memory_jobs) "
            "ORDER BY created_at,id LIMIT ?",
            (available,),
        ).fetchall()
        for row in rows:
            self._queue(row[0])

    async def start(self) -> None:
        if not self.config.enabled or (self._worker is not None and not self._worker.done()):
            return
        self.recover()
        self._index_closed = False
        self._worker = asyncio.create_task(self._run(), name="muli-memory")
        self._wake.set()

    def recover(self) -> None:
        """Requeue local interrupted work without starting model/media calls."""
        with self.memory.store.db:
            self.memory.store.db.execute(
                "UPDATE memory_jobs SET state='queued',updated_at=? WHERE state='processing'",
                (now(),),
            )
        plan = self._current_index_plan()
        if plan is not None and plan["state"] in _INDEX_ACTIVE:
            try:
                self._index_admit(plan)
            except EvidenceError as error:
                if error.code != "output_budget":
                    raise
                # A smaller output budget may still fit the actual durable
                # prefix but not its next progress/error update. Retire only
                # execution cursors; sources and observed counts stay intact.
                self._block_index(plan, "output_budget")
        self.check_index_model()

    async def close(self) -> None:
        self._index_closed = True
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    def job(self, source_id: str) -> dict[str, Any] | None:
        row = self.memory.store.db.execute(
            "SELECT * FROM memory_jobs WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["checkpoint"] = json.loads(item["checkpoint"])
        return item

    def status(self) -> dict[str, Any]:
        counts = {
            row[0]: row[1]
            for row in self.memory.store.db.execute(
                "SELECT state,count(*) FROM memory_jobs GROUP BY state"
            ).fetchall()
        }
        deferred = self.memory.store.db.execute(
            "SELECT count(*) FROM memory_sources WHERE id NOT IN (SELECT source_id FROM memory_jobs)"
        ).fetchone()[0]
        return {
            "enabled": self.config.enabled,
            "running": self._worker is not None and not self._worker.done(),
            "auto_extract": self.config.auto_extract,
            "extractor_configured": self.extractor is not None,
            "embedding_configured": self.media is not None
            and self.media.config.embedding is not None,
            "reranker_configured": self.media is not None
            and self.media.config.reranker is not None,
            "jobs": counts,
            "deferred": deferred,
            "max_pending_jobs": self.config.max_pending_jobs,
            "last_error": self._last_error,
            "cache_entries": len(self._cache),
            "index_plan": (
                self._index_summary(plan) if (plan := self._current_index_plan()) else None
            ),
        }

    def _index_fit(self, item: dict[str, Any], *, max_bytes: int | None = None) -> None:
        budget = self.max_plan_bytes
        if max_bytes is not None:
            validate_integer(max_bytes, "max_bytes", minimum=1)
            budget = min(budget, max_bytes)
        projection = deepcopy(item)
        for source in projection.get("sources", []):
            record = source.get("chunk_record")
            if record is not None:
                # A selected old source may create chunks after admission.
                # Reserve their complete progress and fixed-size IDs now.
                record["memory_ids"] = ["f" * 32] * len(record["memory_ids"])
                record["next_index"] = record["plan"]["count"]
                for entry in source["items"][source["chunk_offset"] :]:
                    entry["version"] = SQLITE_MAX_INTEGER - 1
        if (
            len(json.dumps(projection, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            > budget
        ):
            raise EvidenceError("output_budget", "完整索引计划超过当前元数据字节预算")

    def _index_admit(self, plan: dict[str, Any]) -> None:
        # Every later mutable field has a finite representation. Reserve it at
        # admission so an otherwise legal near-limit plan cannot kill the sole
        # worker when queued becomes processing or progress/error fields grow.
        projection = {
            **plan,
            **dict.fromkeys(_INDEX_COUNTERS, SQLITE_MAX_INTEGER - 1),
            "state": max(_INDEX_ACTIVE | _INDEX_TERMINAL, key=len),
            "last_error": max(_INDEX_ERRORS, key=len),
            **dict.fromkeys(("created_at", "updated_at", "next_attempt"), -sys.float_info.max),
        }
        self._index_fit(projection)

    def check_index_budget(self, max_bytes: int) -> None:
        """Reject an unreadable current plan before stopping its owning worker."""
        validate_integer(max_bytes, "max_bytes", minimum=1)
        store = self.memory.store
        pointer = store.bounded_object("memory_index_state", "current", max_bytes=max_bytes)
        if pointer is None:
            return
        plan_id = pointer.get("plan_id")
        if not isinstance(plan_id, str):
            raise EvidenceError("content_unavailable", "当前索引计划定位无效")
        if store.bounded_object("memory_index_plan", plan_id, max_bytes=max_bytes) is None:
            raise EvidenceError("content_unavailable", "当前索引计划无法完整核查")

    def _index_plan(self, plan_id: str) -> dict[str, Any] | None:
        if (
            not isinstance(plan_id, str)
            or not plan_id
            or len(plan_id.encode("utf-8")) > self.max_plan_bytes
        ):
            raise ValueError("索引计划 ID 无效")
        plan = self.memory.store.bounded_object(
            "memory_index_plan", plan_id, max_bytes=self.max_plan_bytes
        )
        if plan is None:
            return None
        state = plan.get("state")
        if (
            plan.get("version") != 1
            or type(plan.get("version")) is not int
            or not isinstance(state, str)
            or state not in _INDEX_ACTIVE | _INDEX_TERMINAL
            or not isinstance(plan.get("model_key"), str)
            or not isinstance(plan.get("sources"), list)
            or not isinstance(plan.get("last_error"), str)
        ):
            raise EvidenceError("content_unavailable", "索引计划无法完整核查")
        execution_fields = sum(field in plan for field in _INDEX_EXECUTION_FIELDS)
        if execution_fields not in {0, len(_INDEX_EXECUTION_FIELDS)} or (
            state in _INDEX_ACTIVE and execution_fields == 0
        ):
            raise EvidenceError("content_unavailable", "索引计划执行进度无法核查")
        for field in _INDEX_COUNTERS:
            if field in _INDEX_EXECUTION_FIELDS and field not in plan:
                continue
            value = plan.get(field)
            if not isinstance(value, int):
                raise EvidenceError("content_unavailable", "索引计划进度无法核查")
            validate_integer(value, field, minimum=1 if field == "revision" else 0)
        if plan["sources_total"] != len(plan["sources"]) or (
            execution_fields and plan["source_index"] > len(plan["sources"])
        ):
            raise EvidenceError("content_unavailable", "索引计划来源范围无效")
        if execution_fields:
            deadline = plan.get("next_attempt")
            if (
                not isinstance(deadline, (int, float))
                or isinstance(deadline, bool)
                or not math.isfinite(deadline)
                or deadline < 0
            ):
                raise EvidenceError("content_unavailable", "索引计划等待时间无效")
        for source in plan["sources"]:
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("id"), str)
                or not isinstance(source.get("digest"), str)
                or not isinstance(source.get("items"), list)
            ):
                raise EvidenceError("content_unavailable", "索引计划来源证明无效")
            if "chunk_batch_size" in source and (
                type(source["chunk_batch_size"]) is not int
                or not 1 <= source["chunk_batch_size"] <= 16
            ):
                raise EvidenceError("content_unavailable", "原文块索引批次无效")
            if "chunk_record" in source or "chunk_offset" in source:
                try:
                    record = source["chunk_record"]
                    if not isinstance(record, dict) or set(record) != {
                        "plan",
                        "next_index",
                        "memory_ids",
                    }:
                        raise ValueError
                    chunk_plan = ChunkPlan(**record["plan"])
                    offset = source["chunk_offset"]
                    cursor, identifiers = record["next_index"], record["memory_ids"]
                    if (
                        chunk_plan.source_digest != source["digest"]
                        or type(offset) is not int
                        or not 0 <= offset <= len(source["items"])
                        or len(source["items"]) - offset != chunk_plan.count
                        or type(cursor) is not int
                        or not 0 <= cursor <= chunk_plan.count
                        or not isinstance(identifiers, list)
                        or len(identifiers) != chunk_plan.count
                        or any(
                            not isinstance(value, str) or (value and len(value) != 32)
                            for value in identifiers
                        )
                        or any(identifiers[cursor:])
                    ):
                        raise ValueError
                except (KeyError, TypeError, ValueError) as error:
                    raise EvidenceError("content_unavailable", "原文块索引证明无效") from error
            for item in source["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise EvidenceError("content_unavailable", "索引计划条目证明无效")
                version = item.get("version")
                if not isinstance(version, int):
                    raise EvidenceError("content_unavailable", "索引计划条目版本无效")
                validate_integer(version, "version")
        if (
            execution_fields
            and plan["source_index"] < len(plan["sources"])
            and plan["item_index"] > len(plan["sources"][plan["source_index"]]["items"])
        ):
            raise EvidenceError("content_unavailable", "索引计划条目范围无效")
        return plan

    def _current_index_plan(self) -> dict[str, Any] | None:
        pointer = self.memory.store.bounded_object(
            "memory_index_state", "current", max_bytes=self.max_plan_bytes
        )
        if pointer is None:
            return None
        plan_id = pointer.get("plan_id")
        if not isinstance(plan_id, str):
            raise EvidenceError("content_unavailable", "当前索引计划定位无效")
        return self._index_plan(plan_id)

    @staticmethod
    def _index_summary(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "plan_id": plan["id"],
            **{
                key: plan[key]
                for key in (
                    "state",
                    "model_key",
                    "sources_total",
                    "sources_finished",
                    "vectors_written",
                    "already_current",
                    "omitted",
                    "last_error",
                )
            },
        }

    def index_status(self, plan_id: str) -> dict[str, Any]:
        plan = self._index_plan(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return self._index_summary(plan)

    def _index_save(self, plan: dict[str, Any], *, max_bytes: int | None = None) -> None:
        plan["updated_at"] = now()
        self._index_fit(plan, max_bytes=max_bytes)
        self.memory.store.put("memory_index_plan", plan)

    def _block_index(
        self, plan: dict[str, Any], reason: str, *, max_bytes: int | None = None
    ) -> None:
        blocked = {
            **plan,
            "state": "blocked",
            "last_error": reason,
            "revision": plan["revision"] + 1,
        }
        for field in _INDEX_EXECUTION_FIELDS:
            blocked.pop(field)
        self._index_save(blocked, max_bytes=max_bytes)

    def _source_digest(self, source_id: str) -> str | None:
        row = self.memory.store.db.execute(
            "SELECT CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 "
            "THEN CAST(digest AS BLOB) END AS digest FROM memory_sources s WHERE s.id=? "
            "AND typeof(content)='text' AND length(CAST(content AS BLOB))<=? "
            "AND NOT EXISTS (SELECT 1 FROM memory_forgotten_sources f WHERE f.id=s.id)",
            (source_id, self.config.max_source_chars * 4),
        ).fetchone()
        raw = row["digest"] if row is not None else None
        if not isinstance(raw, bytes):
            return None
        try:
            digest = raw.decode("ascii")
        except UnicodeError:
            return None
        return digest if all(character in "0123456789abcdef" for character in digest) else None

    def _index_source(self, source_id: str) -> dict[str, Any]:
        digest = self._source_digest(source_id)
        row = self.memory.store.db.execute(
            "SELECT CASE WHEN typeof(checkpoint)='text' AND length(CAST(checkpoint AS BLOB))<=? "
            "THEN CAST(checkpoint AS BLOB) END AS checkpoint FROM memory_jobs "
            "WHERE source_id=? AND state='done'",
            (self.max_plan_bytes, source_id),
        ).fetchone()
        if digest is None or row is None or row["checkpoint"] is None:
            raise ValueError("只能重建准确、未遗忘且已完成提炼的来源")
        try:
            checkpoint = json.loads(row["checkpoint"].decode("utf-8"))
            if not isinstance(checkpoint, dict):
                raise ValueError
            identifiers = checkpoint["memory_ids"]
            if (
                not isinstance(checkpoint.get("extraction"), dict)
                or not isinstance(identifiers, list)
                or len(identifiers) > self.config.max_facts_per_source + 1
                or any(not isinstance(value, str) or not value for value in identifiers)
                or len(identifiers) != len(set(identifiers))
            ):
                raise ValueError
            json.dumps(checkpoint, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (KeyError, ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise ValueError("完成作业的有限条目集合无法核查") from error
        original = self.memory.source(source_id)
        if original is None:
            raise ValueError("重建来源已遗忘")
        record = checkpoint.get("source_chunks")
        if record is None:
            record = self.source_chunks.new_record(original)
        chunk_ids = self.source_chunks.validate(original, record)
        backfill = record["next_index"] < record["plan"]["count"]
        episode = self.memory.automatic_episode_id(source_id) if record["plan"]["count"] else None
        items = []
        for identifier in sorted(set(identifiers) | (set() if backfill else set(chunk_ids))):
            if identifier == episode:
                continue
            version_row = self.memory.store.db.execute(
                "SELECT CASE WHEN typeof(version)='integer' AND version>0 THEN version END "
                "FROM memory_meta WHERE memory_id=? AND source_id=? AND status!='superseded'",
                (identifier, source_id),
            ).fetchone()
            items.append(
                {
                    "id": identifier,
                    "version": version_row[0] if version_row and version_row[0] else 0,
                }
            )
        result: dict[str, Any] = {"id": source_id, "digest": digest, "items": items}
        if record["plan"]["count"]:
            result["chunk_batch_size"] = record["plan"]["batch_size"]
        if backfill:
            result.update(chunk_record=deepcopy(record), chunk_offset=len(items))
            items.extend({"id": "_" * 32, "version": 0} for _ in record["memory_ids"])
        return result

    def start_index(self, source_ids: list[str], key: str) -> dict[str, Any]:
        self.admission.require()
        if (
            not isinstance(source_ids, list)
            or not 1 <= len(source_ids) <= self.config.max_pending_jobs
            or not isinstance(key, str)
            or not key.strip()
            or len(key) > self.max_plan_bytes
        ):
            raise ValueError("索引需要准确来源 ID 列表和稳定请求键")
        remaining = self.max_plan_bytes - len(key.encode("utf-8"))
        for value in source_ids:
            if not isinstance(value, str) or not value.strip() or len(value) > remaining:
                raise ValueError("索引来源集合超过当前元数据字节预算")
            remaining -= len(value.encode("utf-8"))
            if remaining < 0:
                raise EvidenceError("output_budget", "完整索引来源集合超过字节预算")
        identifiers = sorted(set(source_ids))
        request = {"source_ids": identifiers, "model_key": self._model_key()}
        self._index_fit({**request, "key": key})
        request_hash = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        plan_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
        previous = self._index_plan(plan_id)
        if previous is not None:
            if previous.get("request_hash") != request_hash:
                raise EvidenceError("evidence_changed", "同一索引请求键的来源或目标模型不同")
            return self._index_summary(previous)
        if self._index_closed or not self.config.enabled or not self._model_key():
            raise ValueError("记忆或 embedding 未启用，未接纳索引计划")
        current = self._current_index_plan()
        if (
            current is not None and current["state"] in _INDEX_ACTIVE
        ) or self._pending() >= self.config.max_pending_jobs:
            raise ValueError("后台记忆配额已满或已有索引计划，请等待或取消")
        sources = []
        for identifier in identifiers:
            sources.append(self._index_source(identifier))
            self._index_fit({"sources": sources})
        plan = {
            "id": plan_id,
            "version": 1,
            "revision": 1,
            "request_hash": request_hash,
            "model_key": request["model_key"],
            "sources": sources,
            "state": "queued",
            "sources_total": len(sources),
            "sources_finished": 0,
            "vectors_written": 0,
            "already_current": 0,
            "omitted": 0,
            "source_index": 0,
            "item_index": 0,
            "attempts": 0,
            "next_attempt": now(),
            "last_error": "",
            "created_at": now(),
            "updated_at": now(),
        }
        self._index_admit(plan)
        self.memory.store.put_many(
            [
                ("memory_index_plan", plan),
                ("memory_index_state", {"id": "current", "plan_id": plan_id}),
            ]
        )
        self._wake.set()
        return self._index_summary(plan)

    def cancel_index(self, plan_id: str) -> dict[str, Any]:
        self.admission.require()
        plan = self._index_plan(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        if plan["state"] in _INDEX_ACTIVE:
            plan.update(state="cancelled", revision=plan["revision"] + 1)
            self._index_save(plan)
            self._wake.set()
        return self._index_summary(plan)

    def check_index_model(
        self, model_key: str | None = None, *, max_bytes: int | None = None
    ) -> None:
        plan = self._current_index_plan()
        key = self._model_key() if model_key is None else model_key
        if plan is not None and plan["state"] in _INDEX_ACTIVE and plan["model_key"] != key:
            self._block_index(plan, "embedding_changed", max_bytes=max_bytes)

    def _index_live(self, plan: dict[str, Any]) -> bool:
        if self._index_closed or not self.config.enabled or not self.admission.allowed:
            return False
        current = self._current_index_plan()
        return (
            current is not None
            and current["id"] == plan["id"]
            and current["revision"] == plan["revision"]
            and current["state"] in _INDEX_ACTIVE
            and current["model_key"] == plan["model_key"] == self._model_key()
        )

    def _index_item(
        self, source_id: str, entry: dict[str, Any], model_key: str
    ) -> dict[str, Any] | None:
        if entry["version"] == 0:
            return None
        row = self.memory.store.db.execute(
            "SELECT CASE WHEN typeof(m.content)='text' "
            "AND length(CAST(m.content AS BLOB))<=? THEN CAST(m.content AS BLOB) END AS content,"
            "EXISTS(SELECT 1 FROM memory_vectors v WHERE v.memory_id=m.id AND v.model_key=?) AS indexed "
            "FROM memories m JOIN memory_meta x ON x.memory_id=m.id "
            "WHERE m.id=? AND x.source_id=? AND typeof(x.version)='integer' "
            "AND x.version=? AND x.status!='superseded'",
            (self.config.max_source_chars * 4, model_key, entry["id"], source_id, entry["version"]),
        ).fetchone()
        if row is None or row["content"] is None:
            return None
        try:
            content = row["content"].decode("utf-8", errors="strict")
        except UnicodeError:
            return None
        if not content.strip() or len(content) > self.config.max_source_chars:
            return None
        return {**entry, "content": content, "indexed": row["indexed"] == 1}

    async def _index_batch(self, plan: dict[str, Any]) -> None:
        if not self._index_live(plan):
            return
        if plan["source_index"] == len(plan["sources"]):
            plan["state"] = "done"
            return
        source = plan["sources"][plan["source_index"]]
        start = plan["item_index"]
        if self._source_digest(source["id"]) != source["digest"]:
            plan["omitted"] += len(source["items"]) - start
            plan["source_index"] += 1
            plan["item_index"] = 0
            # A forgotten/changed source is classified, not indexed successfully.
            if plan["source_index"] == len(plan["sources"]):
                plan["state"] = "done"
            return
        if "chunk_record" in source:
            original = self.memory.source(source["id"])
            if original is None:
                return

            def persist_chunks() -> bool:
                if not self._index_live(plan):
                    return False
                self._index_save(plan)
                return True

            prepared = await self.source_chunks.prepare(
                original,
                source["chunk_record"],
                live=lambda: (
                    self._index_live(plan) and self._source_digest(source["id"]) == source["digest"]
                ),
                persist=persist_chunks,
            )
            if prepared is None or not self._index_live(plan):
                return
            for index, identifier in enumerate(source["chunk_record"]["memory_ids"]):
                item = self.memory.get(identifier) if identifier else None
                source["items"][source["chunk_offset"] + index] = {
                    "id": identifier or "_" * 32,
                    "version": item["version"] if item is not None else 0,
                }
            del source["chunk_record"], source["chunk_offset"]
            self._index_save(plan)
        batch_size = min(
            self.config.max_facts_per_source,
            source.get("chunk_batch_size", self.config.max_facts_per_source),
        )
        entries = source["items"][start : start + batch_size]
        vectors: dict[str, list[float]] = {}
        if entries:
            media = self.media
            if media is None:
                return
            async with self._media_lock:
                if not self._index_live(plan) or self.media is not media:
                    return
                # Query work may hold this lock while a source is forgotten or
                # corrected. Build full HTTP input only after acquiring it.
                inputs = (
                    [
                        item
                        for entry in entries
                        if (item := self._index_item(source["id"], entry, plan["model_key"]))
                        is not None
                        and not item["indexed"]
                    ]
                    if self._source_digest(source["id"]) == source["digest"]
                    else []
                )
                result = (
                    await media.embed([item["content"] for item in inputs], purpose="document")
                    if inputs
                    else []
                )
            if not self._index_live(plan) or self.media is not media:
                return
            if len(result) != len(inputs):
                raise ValueError("向量数量与索引批次数量不一致")
            vectors = {item["id"]: vector for item, vector in zip(inputs, result, strict=True)}
        # No await between the final plan/source/item checks and version-CAS.
        # Each store_vector owns its original commit; a later error must retain
        # this prefix rather than pretending the entire source was finished.
        for entry in entries:
            if not self._index_live(plan):
                return
            item = (
                self._index_item(source["id"], entry, plan["model_key"])
                if self._source_digest(source["id"]) == source["digest"]
                else None
            )
            if item is None:
                plan["omitted"] += 1
            elif item["indexed"]:
                plan["already_current"] += 1
            elif entry["id"] in vectors:
                written = self.memory.store_vector(
                    entry["id"],
                    vectors[entry["id"]],
                    plan["model_key"],
                    expected_version=entry["version"],
                )
                plan["vectors_written" if written else "omitted"] += 1
            else:
                plan["omitted"] += 1
            plan["item_index"] += 1
        if plan["item_index"] == len(source["items"]):
            if self._source_digest(source["id"]) == source["digest"]:
                plan["sources_finished"] += 1
            plan["source_index"] += 1
            plan["item_index"] = 0
        if plan["source_index"] == len(plan["sources"]):
            plan["state"] = "done"

    async def _run_index(self, plan: dict[str, Any]) -> None:
        if not self._index_live(plan):
            return
        plan.update(state="processing", attempts=plan["attempts"] + 1)
        self._index_save(plan)
        try:
            async with asyncio.timeout(self.config.job_timeout):
                await self._index_batch(plan)
        except asyncio.CancelledError:
            # Normal close cancels only the shared worker. The durable plan and
            # successfully committed vectors remain resumable without extraction.
            raise
        except Exception as error:
            if not self._index_live(plan):
                return
            plan.update(
                state="failed" if plan["attempts"] >= self.config.max_attempts else "retry",
                next_attempt=now() + self.config.retry_backoff_seconds * plan["attempts"],
                last_error="index_timeout" if isinstance(error, TimeoutError) else "index_failed",
            )
        else:
            if not self._index_live(plan):
                return
            if plan["state"] == "processing":
                plan["state"] = "queued"
            plan.update(attempts=0, next_attempt=now(), last_error="")
        self._index_save(plan)
        await self._publish()

    def retry_source(
        self, source_id: str, revision: str, *, max_output_chars: int
    ) -> dict[str, Any]:
        """Admit exactly one observed source; unrelated deferred work stays untouched."""
        self.admission.require()
        validate_integer(max_output_chars, "max_output_chars", minimum=1)
        if (
            not isinstance(source_id, str)
            or not 1 <= len(source_id) <= 200
            or not isinstance(revision, str)
            or len(revision) != 64
            or any(character not in "0123456789abcdef" for character in revision)
        ):
            raise EvidenceError("invalid_arguments", "重试需要准确来源 ID 和原文 revision")
        try:
            source_id.encode("utf-8")
        except UnicodeError as error:
            raise EvidenceError("invalid_arguments", "来源 ID 包含无效 Unicode 字符") from error
        if not self.config.enabled or self._index_closed:
            raise ValueError("记忆处理未启用或正在关闭")
        if self.memory.store.db.in_transaction:
            raise EvidenceError("content_unavailable", "存在尚未收口的数据库事务，未重试来源")
        digest = self._source_digest(source_id)
        if digest is None:
            raise EvidenceError("evidence_not_found", "来源已忘记、不存在或超过当前读取上限")
        if digest != revision:
            raise EvidenceError("evidence_changed", "原文版本已变化，请重新核查来源")
        row = self.memory.store.db.execute(
            "SELECT CASE WHEN state IN ('queued','processing','retry','failed','done') "
            "THEN state END AS state FROM memory_jobs WHERE source_id=?",
            (source_id,),
        ).fetchone()
        previous = row["state"] if row is not None else "deferred"
        if previous is None:
            raise EvidenceError("content_unavailable", "来源的整理状态无法核查")
        eligible = previous in {"failed", "deferred"}
        accepted = eligible and self._pending() < self.config.max_pending_jobs
        receipt = {
            "source_id": source_id,
            "revision": revision,
            "previous_state": previous,
            "state": "queued" if accepted else previous,
            "accepted": accepted,
            "reason": (
                "queued"
                if accepted
                else "capacity_full"
                if eligible
                else "already_done"
                if previous == "done"
                else "already_pending"
            ),
        }
        # Reserve the longer boolean spelling before any durable mutation.
        if len(json.dumps({**receipt, "accepted": False}, ensure_ascii=False)) > max_output_chars:
            raise EvidenceError("output_budget", "完整来源重试回执超过当前输出预算")
        if not accepted:
            return receipt
        if previous == "failed":
            timestamp = now()
            with self.memory.store.db:
                updated = self.memory.store.db.execute(
                    "UPDATE memory_jobs SET state='queued',attempts=0,next_attempt=?,"
                    "last_error='',checkpoint=json_remove(checkpoint,'$.source_failure'),"
                    "updated_at=? WHERE source_id=? AND state='failed'",
                    (timestamp, timestamp, source_id),
                )
                if updated.rowcount != 1:
                    raise EvidenceError("evidence_changed", "来源作业已变化，未重新登记")
        elif not self._queue(source_id):
            raise EvidenceError("evidence_changed", "来源作业已变化，未重新登记")
        self._wake.set()
        return receipt

    async def retry(self, source_id: str | None = None) -> dict[str, Any]:
        available = self.config.max_pending_jobs - self._pending()
        if available > 0:
            sql = "SELECT source_id FROM memory_jobs WHERE state='failed'"
            arguments: list[Any] = []
            if source_id is not None:
                sql += " AND source_id=?"
                arguments.append(source_id)
            rows = self.memory.store.db.execute(
                sql + " ORDER BY updated_at LIMIT ?", [*arguments, available]
            ).fetchall()
            with self.memory.store.db:
                self.memory.store.db.executemany(
                    "UPDATE memory_jobs SET state='queued',attempts=0,next_attempt=?,last_error='',"
                    "checkpoint=json_remove(checkpoint,'$.source_failure'),updated_at=? "
                    "WHERE source_id=?",
                    ((now(), now(), row[0]) for row in rows),
                )
        if source_id is not None and self.memory.source(source_id) is not None:
            self._queue(source_id)
        self._admit_deferred()
        self._wake.set()
        await self._publish()
        return self.status()

    async def wait(self, source_id: str) -> dict[str, Any] | None:
        def terminal() -> bool:
            job = self.job(source_id)
            return self.memory.source(source_id) is None or (
                job is not None and job["state"] in {"done", "failed"}
            )

        async with self.changed:
            await self.changed.wait_for(terminal)
        return self.job(source_id)

    def _publish_source_failure(self, source_id: str, revision: str, data: dict[str, Any]) -> bool:
        """Commit the finite proof and its existing status event before any await."""
        if self.bus is None or self.failure_routing is None or self._index_closed:
            return False
        store = self.memory.store
        # Never let optional notification persistence commit someone else's work.
        if store.db.in_transaction:
            return False
        try:
            routing = self.failure_routing()
            if (
                routing is None
                or attachment_revision(
                    store, source_id, max_source_chars=self.config.max_source_chars
                )
                != revision
            ):
                return False
            row = store.db.execute(
                "SELECT CASE WHEN typeof(updated_at) IN ('integer','real') "
                "THEN updated_at END AS updated_at FROM memory_jobs WHERE source_id=? AND state='failed'",
                (source_id,),
            ).fetchone()
            if row is None:
                return False
            proof = MemorySourceFailure.load(
                MemorySourceFailure(
                    new_id(), source_id, revision, row["updated_at"], routing
                ).dump(),
                max_bytes=self.max_plan_bytes,
            )
            if proof is None:
                return False
            payload = {**data, "id": proof.identifier, "source_failure": proof.dump()}
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > self.max_plan_bytes:
                return False
            encoded = json.dumps(proof.dump(), ensure_ascii=False, allow_nan=False)
            with store.db:
                updated = store.db.execute(
                    "UPDATE memory_jobs SET checkpoint=json_set(checkpoint,'$.source_failure',json(?)) "
                    "WHERE source_id=? AND state='failed' AND updated_at=? "
                    "AND typeof(checkpoint)='text' AND length(CAST(checkpoint AS BLOB))<=? "
                    "AND CASE WHEN json_valid(checkpoint) THEN "
                    "length(CAST(json_set(checkpoint,'$.source_failure',json(?)) AS BLOB))<=? END",
                    (
                        encoded,
                        source_id,
                        proof.failed_at,
                        self.max_plan_bytes,
                        encoded,
                        self.max_plan_bytes,
                    ),
                )
                if updated.rowcount != 1:
                    return False
                store._event("memory.pipeline.updated", payload)
            return True
        except Exception:
            # The original failed state is already durable. An unavailable or
            # oversized notice must not replace it or erase successful prefixes.
            return False

    async def _publish(
        self, source_id: str | None = None, *, failed_revision: str | None = None
    ) -> None:
        data = {"source_id": source_id, **self.status()}
        persisted = bool(
            source_id is not None
            and failed_revision is not None
            and self._publish_source_failure(source_id, failed_revision, data)
        )
        async with self.changed:
            self.changed.notify_all()
        if self.bus is not None:
            if persisted:
                await self.bus.notify()
            else:
                await self.bus.emit("memory.pipeline.updated", data)

    def _checkpoint(self, source_id: str, checkpoint: Mapping[str, Any]) -> bool:
        with self.memory.store.db:
            cursor = self.memory.store.db.execute(
                "UPDATE memory_jobs SET checkpoint=?,updated_at=? WHERE source_id=?",
                (json.dumps(checkpoint, ensure_ascii=False, allow_nan=False), now(), source_id),
            )
        return cursor.rowcount > 0

    async def _run(self) -> None:
        while True:
            await self.admission.wait()
            self._wake.clear()
            self._admit_deferred()
            row = self.memory.store.db.execute(
                "SELECT source_id FROM memory_jobs WHERE state IN ('queued','retry') AND next_attempt<=? "
                "ORDER BY next_attempt,created_at,source_id LIMIT 1",
                (now(),),
            ).fetchone()
            if row is None:
                plan = self._current_index_plan()
                if (
                    plan is not None
                    and plan["state"] in _INDEX_ACTIVE
                    and plan["next_attempt"] <= now()
                ):
                    with self.admission.operation():
                        await self._run_index(plan)
                    await asyncio.sleep(0)
                    continue
                deadline = self.memory.store.db.execute(
                    "SELECT min(next_attempt) FROM memory_jobs WHERE state IN ('queued','retry')"
                ).fetchone()[0]
                if plan is not None and plan["state"] in _INDEX_ACTIVE:
                    deadline = (
                        plan["next_attempt"]
                        if deadline is None
                        else min(deadline, plan["next_attempt"])
                    )
                try:
                    if deadline is None:
                        await self._wake.wait()
                    else:
                        await asyncio.wait_for(self._wake.wait(), max(0.0, deadline - now()))
                except TimeoutError:
                    pass
                continue
            source_id = str(row[0])
            with self.admission.operation():
                await self._run_job(source_id)
            await asyncio.sleep(0)

    async def _run_job(self, source_id: str) -> None:
        if self._index_closed:
            return
        revision = self._source_digest(source_id)
        failed_revision: str | None = None
        with self.memory.store.db:
            updated = self.memory.store.db.execute(
                "UPDATE memory_jobs SET state='processing',attempts=attempts+1,updated_at=? "
                "WHERE source_id=? AND state IN ('queued','retry')",
                (now(), source_id),
            )
        if updated.rowcount != 1:
            return
        try:
            async with asyncio.timeout(self.config.job_timeout):
                await self._process(source_id)
        except asyncio.CancelledError:
            with self.memory.store.db:
                self.memory.store.db.execute(
                    "UPDATE memory_jobs SET state='queued',updated_at=? WHERE source_id=? AND state='processing'",
                    (now(), source_id),
                )
            raise
        except Exception as error:
            # Upstream bodies may contain credentials: only persist the exception class.
            self._last_error = type(error).__name__
            job = self.job(source_id)
            if job is not None:
                state = "failed" if job["attempts"] >= self.config.max_attempts else "retry"
                delay = self.config.retry_backoff_seconds * job["attempts"]
                with self.memory.store.db:
                    self.memory.store.db.execute(
                        "UPDATE memory_jobs SET state=?,next_attempt=?,last_error=?,updated_at=? WHERE source_id=?",
                        (state, now() + delay, self._last_error, now(), source_id),
                    )
                if state == "failed":
                    failed_revision = revision
        await self._publish(source_id, failed_revision=failed_revision)

    def _validated(self, payload: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("提炼结果必须是 JSON 对象")
        proposals = payload.get("memories", [])
        if not isinstance(proposals, list) or len(proposals) > self.config.max_facts_per_source:
            raise ValueError("提炼事实数量超限或格式错误")
        accepted: list[dict[str, Any]] = []
        rejected = 0
        for proposal in proposals:
            if not isinstance(proposal, dict):
                raise ValueError("提炼事实必须是对象")
            layer, content, quote = (proposal.get(key) for key in ("layer", "content", "quote"))
            if layer not in LAYERS or not isinstance(content, str) or not isinstance(quote, str):
                raise ValueError("提炼内容、分层或证据格式无效")
            entities, relations = proposal.get("entities", []), proposal.get("relations", [])
            if (
                not isinstance(entities, list)
                or not isinstance(relations, list)
                or any(not isinstance(edge, dict) for edge in relations)
            ):
                raise ValueError("实体和关系需要列表")
            confidence, claim_key = proposal.get("confidence", 0.8), proposal.get("claim_key", "")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not isinstance(claim_key, str)
            ):
                raise ValueError("提炼置信度或事实槽位格式错误")
            self.memory._validate(
                content,
                "fact",
                source["source"],
                entities,
                relations,
                float(confidence),
                layer,
                source["id"],
                quote,
                source["trusted_owner"],
                claim_key,
            )
            if layer in {"identity", "preference"} and not source["trusted_owner"]:
                rejected += 1
                continue
            accepted.append(
                {
                    "content": content,
                    "layer": layer,
                    "quote": quote,
                    "entities": entities,
                    "relations": relations,
                    "confidence": float(confidence),
                    "claim_key": claim_key,
                }
            )
        aliases = payload.get("aliases", {})
        if (
            not isinstance(aliases, dict)
            or len(aliases) > 30
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key.strip()
                or not value.strip()
                or max(len(key), len(value)) > 200
                for key, value in aliases.items()
            )
        ):
            raise ValueError("实体别名格式错误")
        original = normalize_entity(source["content"])
        aliases = {
            alias: canonical
            for alias, canonical in aliases.items()
            if source["trusted_owner"]
            and normalize_entity(alias) in original
            and normalize_entity(canonical) in original
        }
        return {
            "memories": accepted,
            "aliases": aliases,
            "rejected_untrusted_owner_claims": rejected,
        }

    async def _process(self, source_id: str) -> None:
        source, job = self.memory.source(source_id), self.job(source_id)
        if source is None or job is None:
            return
        checkpoint = job["checkpoint"]
        if "extraction" not in checkpoint:
            payload: dict[str, Any] = {"memories": [], "aliases": {}}
            if self.config.auto_extract and self.extractor is not None:
                payload = await self.extractor(deepcopy(source))
            if self.memory.source(source_id) is None:
                return
            checkpoint["extraction"] = self._validated(payload, source)
            if not self._checkpoint(source_id, checkpoint):
                return
        if "memory_ids" not in checkpoint:
            payload = checkpoint["extraction"]
            self.memory.apply_extraction(source_id, payload["memories"], aliases=payload["aliases"])
            rows = self.memory.store.db.execute(
                "SELECT memory_id FROM memory_meta WHERE source_id=? ORDER BY memory_id",
                (source_id,),
            ).fetchall()
            checkpoint["memory_ids"] = [row[0] for row in rows]
            if not self._checkpoint(source_id, checkpoint):
                return
        if "source_chunks" not in checkpoint:
            checkpoint["source_chunks"] = self.source_chunks.new_record(source)
            if not self._checkpoint(source_id, checkpoint):
                return

        def live() -> bool:
            return bool(self._source_digest(source_id) == source["digest"])

        chunk_ids = await self.source_chunks.prepare(
            source,
            checkpoint["source_chunks"],
            live=live,
            persist=lambda: self._checkpoint(source_id, checkpoint),
        )
        if chunk_ids is None:
            if self.memory.source(source_id) is not None:
                raise ValueError("原文块来源已变化或超过当前限额")
            return
        identifiers = list(dict.fromkeys([*checkpoint["memory_ids"], *chunk_ids]))
        if checkpoint["source_chunks"]["plan"]["count"]:
            # Raw chunks already cover the entire source. Do not send the
            # duplicate 12k archive prefix as an oversized embedding input.
            episode = self.memory.automatic_episode_id(source_id)
            identifiers = [identifier for identifier in identifiers if identifier != episode]
        if self.media is not None and self.media.config.embedding is not None:
            policy = restore_plan(
                checkpoint["source_chunks"]["plan"], source["digest"], source["content"]
            )
            await self._embed_source(source, identifiers, batch_size=policy.batch_size)
            if not live():
                return
            checkpoint["embedded"] = True
            if not self._checkpoint(source_id, checkpoint):
                return
        if self._index_closed or not live():
            return
        with self.memory.store.db:
            updated = self.memory.store.db.execute(
                "UPDATE memory_jobs SET state='done',last_error='',updated_at=? "
                "WHERE source_id=? AND state='processing' AND EXISTS "
                "(SELECT 1 FROM memory_sources s WHERE s.id=? AND s.digest=?)",
                (now(), source_id, source_id, source["digest"]),
            )
            if updated.rowcount == 1:
                record_source_completion(
                    self.memory.store,
                    source_id,
                    source["digest"],
                    max_source_chars=self.config.max_source_chars,
                )
        if self.bus is not None and self.memory.source(source_id) is not None:
            await self.bus.emit(
                "memory.updated", {"source_id": source_id, "memory_ids": identifiers}
            )

    async def _embed_source(
        self, source: dict[str, Any], identifiers: list[str], *, batch_size: int
    ) -> None:
        media, model_key = self.media, self._model_key()
        if media is None or media.config.embedding is None:
            return

        def live() -> bool:
            return (
                self.media is media
                and self._model_key() == model_key
                and self._source_digest(source["id"]) == source["digest"]
            )

        for start in range(0, len(identifiers), batch_size):
            async with self._media_lock:
                if not live():
                    raise ValueError("语义服务或原文来源已变化")
                inputs = []
                # Build HTTP input only after acquiring the lock. A query may
                # have held it while the owner forgot or corrected a source.
                for identifier in identifiers[start : start + batch_size]:
                    row = self.memory.store.db.execute(
                        "SELECT version FROM memory_meta WHERE memory_id=? AND source_id=?",
                        (identifier, source["id"]),
                    ).fetchone()
                    if row is None or type(row["version"]) is not int:
                        continue
                    item = self._index_item(
                        source["id"], {"id": identifier, "version": row["version"]}, model_key
                    )
                    if item is not None and not item["indexed"]:
                        inputs.append(item)
                vectors = (
                    await media.embed([item["content"] for item in inputs], purpose="document")
                    if inputs
                    else []
                )
            if not live():
                raise ValueError("在途向量的来源或模型已失效")
            if len(vectors) != len(inputs):
                raise ValueError("向量数量与记忆批次不一致")
            for item, vector in zip(inputs, vectors, strict=True):
                current = self._index_item(source["id"], item, model_key)
                if current is None:
                    raise ValueError("在途向量的条目版本已变化")
                if not self.memory.store_vector(
                    item["id"], vector, model_key, expected_version=item["version"]
                ):
                    raise ValueError("在途向量的条目版本已变化")
            await asyncio.sleep(0)

    def _model_key(self) -> str:
        return self.embedding_key(self.media)

    @staticmethod
    def embedding_key(media: MemoryMedia | None) -> str:
        if media is None or media.config.embedding is None:
            return ""
        config = media.config.embedding
        identity: list[Any] = [
            config.base_url.rstrip("/"),
            config.model,
            config.dimensions,
            config.query_instruction,
            config.extra_body,
        ]
        if config.document_instruction:
            identity.append(config.document_instruction)
        return hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def _cached(
        self, query: str, limit: int, hops: int, source_ids: tuple[str, ...] | None = None
    ) -> dict[str, Any] | None:
        if self._cache_revision != self.memory.revision:
            self._cache.clear()
            self._cache_revision = self.memory.revision
        key = (query, limit, hops, source_ids)
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return deepcopy(self._cache[key])

    def _remember(
        self,
        query: str,
        limit: int,
        hops: int,
        result: dict[str, Any],
        source_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._cached(query, limit, hops, source_ids)
        key = (query, limit, hops, source_ids)
        self._cache[key] = deepcopy(result)
        while len(self._cache) > self.config.cache_entries:
            scoped_key = next(
                (cached_key for cached_key in self._cache if cached_key[3] is not None), None
            )
            if scoped_key is None:
                self._cache.popitem(last=False)
            else:
                del self._cache[scoped_key]

    def _result(
        self,
        items: list[dict[str, Any]],
        degraded: list[str],
        source_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        sources = []
        for identifier in dict.fromkeys(
            item["source_id"] for item in items if item["source_id"] is not None
        ):
            source = self.memory.source(identifier, max_chars=300)
            if source is not None:
                sources.append(source)
        result = {
            "memories": items,
            "communities": self.memory.communities_for(
                [item["id"] for item in items], source_ids=source_ids
            ),
            "sources": sources,
            "degraded": degraded,
            "revision": self.memory.revision,
        }
        if source_ids is not None:
            result["source_ids"] = list(source_ids)
        return result

    async def query(
        self,
        query: str,
        limit: int = 8,
        *,
        hops: int | None = None,
        use_semantic: bool = True,
        source_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        chosen_hops = self.config.graph_hops if hops is None else hops
        self.memory._query(query)
        scope = self.memory.source_scope(source_ids)
        if type(chosen_hops) is not int or not 0 <= chosen_hops <= 8:
            raise ValueError("记忆 hops 必须在 0 到 8 之间")
        limit = max(1, min(limit, self.config.max_candidates))
        cached = self._cached(query, limit, chosen_hops, scope) if use_semantic else None
        if cached is not None:
            return cached
        revision = self.memory.revision
        local = self.memory.search(
            query, self.config.max_candidates, hops=chosen_hops, source_ids=scope
        )
        items = local
        degraded: list[str] = []
        semantic_failed = False

        def refreshed_local() -> dict[str, Any]:
            # Awaited model/lock work may outlive edits or a source-level forget.
            # Return a current local projection once; never retry the network or
            # associate candidates from the old revision with the new cache.
            current = self.memory.search(
                query, self.config.max_candidates, hops=chosen_hops, source_ids=scope
            )
            return self._result(current[:limit], [*degraded, "memory_changed"], scope)

        if use_semantic and query.strip() and self.media is not None:
            try:
                async with asyncio.timeout(self.config.query_timeout):
                    async with self._media_lock:
                        if self.memory.revision != revision:
                            return refreshed_local()
                        semantic: list[dict[str, Any]] = []
                        if self.media.config.embedding is not None:
                            vectors = await self.media.embed([query], purpose="query")
                            if self.memory.revision != revision:
                                return refreshed_local()
                            if len(vectors) != 1:
                                raise ValueError("查询向量数量错误")
                            semantic = self.memory.vector_search(
                                vectors[0],
                                self._model_key(),
                                self.config.max_candidates,
                                source_ids=scope,
                            )
                            if (
                                self.memory.vector_count(self._model_key(), source_ids=scope)
                                > self.config.max_vector_scan
                            ):
                                degraded.append("vector_scan_limited")
                        fused: dict[str, dict[str, Any]] = {}
                        local_ids = {value["id"] for value in local}
                        for ranking in (local, semantic):
                            for rank, item in enumerate(ranking, 1):
                                identifier = item["id"]
                                if identifier not in fused:
                                    fused[identifier] = {**item, "score": 0.0}
                                fused[identifier]["score"] += 1 / (60 + rank)
                                if item["matched_by"] == "semantic" and identifier in local_ids:
                                    fused[identifier]["matched_by"] = "hybrid"
                        items = sorted(
                            fused.values(), key=lambda item: (-item["score"], item["id"])
                        )[: self.config.max_candidates]
                        if items and self.media.config.reranker is not None:
                            ranked = await self.media.rerank(
                                query, [item["content"] for item in items], top_n=limit
                            )
                            if self.memory.revision != revision:
                                return refreshed_local()
                            items = [
                                {
                                    **items[result.index],
                                    "score": result.relevance_score,
                                    "matched_by": "reranked",
                                }
                                for result in ranked
                            ]
            except Exception as error:
                semantic_failed = True
                degraded.append("semantic_unavailable:" + type(error).__name__)
                items = local
        if self.memory.revision != revision:
            return refreshed_local()
        result = self._result(items[:limit], degraded, scope)
        if use_semantic and not semantic_failed:
            self._remember(query, limit, chosen_hops, result, scope)
        return result

    def cached_context(
        self, query: str, max_chars: int | None = None, *, latest_user_id: str | None = None
    ) -> str:
        budget = (
            self.config.context_max_chars
            if max_chars is None
            else min(max_chars, self.config.context_max_chars)
        )
        if budget <= 0 or not self.config.enabled:
            return ""
        return self.context_for_prompt(query, budget, latest_user_id=latest_user_id)

    def context_for_prompt(
        self,
        query: str,
        max_chars: int,
        *,
        latest_user_id: str | None = None,
        limit: int | None = None,
        task_query: str | None = None,
    ) -> str:
        return self.context_for_prompt_result(
            query,
            max_chars,
            latest_user_id=latest_user_id,
            limit=limit,
            task_query=task_query,
        ).with_task

    def context_for_prompt_result(
        self,
        query: str,
        max_chars: int,
        *,
        latest_user_id: str | None = None,
        limit: int | None = None,
        task_query: str | None = None,
    ) -> PromptComposition:
        """宿主已检查当前开关和预算；选材不依赖启动时的旧字符上限。"""
        if max_chars <= 0:
            return PromptComposition("", "")
        limit = self.config.context_limit if limit is None else limit
        cached = self._cached(query, limit, self.config.graph_hops)
        if cached is None:
            for key in reversed(self._cache):
                if key[0] == query and key[2] == self.config.graph_hops and key[3] is None:
                    cached = deepcopy(self._cache[key])
                    break
        return compose_context_result(
            self.memory,
            query,
            limit=limit,
            budget=max_chars,
            latest_user_id=latest_user_id,
            cached=cached["memories"] if cached is not None else None,
            hops=self.config.graph_hops,
            task_query=task_query,
        )
