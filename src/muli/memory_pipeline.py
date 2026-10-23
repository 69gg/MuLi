"""原文先落盘，单 worker 提炼；检索和主提示缓存不等待后台模型。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from muli.events import EventBus
from muli.maintenance import Admission
from muli.media import MediaConfig, RerankResult
from muli.memory import LAYERS, Memory, normalize_entity
from muli.memory_context import recalled_for_prompt, render_context
from muli.store import now

Extractor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class MemoryMedia(Protocol):
    config: MediaConfig

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

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
    ) -> None:
        self.memory, self.bus = memory, bus
        self.config = config or MemoryPipelineConfig()
        self.admission = admission or Admission()
        self.extractor, self.media = extractor, media
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
        self._cache: OrderedDict[tuple[str, int, int], dict[str, Any]] = OrderedDict()
        self._cache_revision = memory.revision
        self._last_error = ""

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
        return int(
            self.memory.store.db.execute(
                "SELECT count(*) FROM memory_jobs WHERE state IN ('queued','processing','retry')"
            ).fetchone()[0]
        )

    def _queue(self, source_id: str) -> bool:
        if self.job(source_id) is not None or self._pending() >= self.config.max_pending_jobs:
            return False
        timestamp = now()
        with self.memory.store.db:
            self.memory.store.db.execute(
                "INSERT OR IGNORE INTO memory_jobs VALUES(?,?,?,0,?,'','{}',?,?)",
                (source_id, source_id, "queued", timestamp, timestamp, timestamp),
            )
        return True

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
        self._worker = asyncio.create_task(self._run(), name="muli-memory")
        self._wake.set()

    def recover(self) -> None:
        """Requeue local interrupted work without starting model/media calls."""
        with self.memory.store.db:
            self.memory.store.db.execute(
                "UPDATE memory_jobs SET state='queued',updated_at=? WHERE state='processing'",
                (now(),),
            )

    async def close(self) -> None:
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
        }

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
                    "UPDATE memory_jobs SET state='queued',attempts=0,next_attempt=?,last_error='',updated_at=? "
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

    async def _publish(self, source_id: str | None = None) -> None:
        async with self.changed:
            self.changed.notify_all()
        if self.bus is not None:
            await self.bus.emit(
                "memory.pipeline.updated", {"source_id": source_id, **self.status()}
            )

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
                deadline = self.memory.store.db.execute(
                    "SELECT min(next_attempt) FROM memory_jobs WHERE state IN ('queued','retry')"
                ).fetchone()[0]
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
        with self.memory.store.db:
            self.memory.store.db.execute(
                "UPDATE memory_jobs SET state='processing',attempts=attempts+1,updated_at=? WHERE source_id=?",
                (now(), source_id),
            )
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
        await self._publish(source_id)

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
        if (
            self.media is not None
            and self.media.config.embedding is not None
            and not checkpoint.get("embedded")
        ):
            items = [
                item
                for identifier in checkpoint["memory_ids"]
                if (item := self.memory.get(identifier)) is not None
            ]
            if items:
                async with self._media_lock:
                    vectors = await self.media.embed([item["content"] for item in items])
                if self.memory.source(source_id) is None:
                    return
                if len(vectors) != len(items):
                    raise ValueError("向量数量与记忆数量不一致")
                for item, vector in zip(items, vectors, strict=True):
                    self.memory.store_vector(
                        item["id"], vector, self._model_key(), expected_version=item["version"]
                    )
            checkpoint["embedded"] = True
            if not self._checkpoint(source_id, checkpoint):
                return
        with self.memory.store.db:
            self.memory.store.db.execute(
                "UPDATE memory_jobs SET state='done',last_error='',updated_at=? WHERE source_id=?",
                (now(), source_id),
            )
        if self.bus is not None and self.memory.source(source_id) is not None:
            await self.bus.emit(
                "memory.updated", {"source_id": source_id, "memory_ids": checkpoint["memory_ids"]}
            )

    def _model_key(self) -> str:
        if self.media is None or self.media.config.embedding is None:
            return ""
        config = self.media.config.embedding
        return hashlib.sha256(
            json.dumps(
                [
                    config.base_url.rstrip("/"),
                    config.model,
                    config.dimensions,
                    config.query_instruction,
                    config.extra_body,
                ],
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def _cached(self, query: str, limit: int, hops: int) -> dict[str, Any] | None:
        if self._cache_revision != self.memory.revision:
            self._cache.clear()
            self._cache_revision = self.memory.revision
        key = (query, limit, hops)
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return deepcopy(self._cache[key])

    def _remember(self, query: str, limit: int, hops: int, result: dict[str, Any]) -> None:
        self._cached(query, limit, hops)
        self._cache[(query, limit, hops)] = deepcopy(result)
        while len(self._cache) > self.config.cache_entries:
            self._cache.popitem(last=False)

    def _result(self, items: list[dict[str, Any]], degraded: list[str]) -> dict[str, Any]:
        sources = []
        for identifier in dict.fromkeys(
            item["source_id"] for item in items if item["source_id"] is not None
        ):
            source = self.memory.source(identifier, max_chars=300)
            if source is not None:
                sources.append(source)
        return {
            "memories": items,
            "communities": self.memory.communities_for([item["id"] for item in items]),
            "sources": sources,
            "degraded": degraded,
            "revision": self.memory.revision,
        }

    async def query(
        self,
        query: str,
        limit: int = 8,
        *,
        hops: int | None = None,
        use_semantic: bool = True,
    ) -> dict[str, Any]:
        chosen_hops = self.config.graph_hops if hops is None else hops
        self.memory._query(query)
        if type(chosen_hops) is not int or not 0 <= chosen_hops <= 8:
            raise ValueError("记忆 hops 必须在 0 到 8 之间")
        limit = max(1, min(limit, self.config.max_candidates))
        cached = self._cached(query, limit, chosen_hops) if use_semantic else None
        if cached is not None:
            return cached
        revision = self.memory.revision
        local = self.memory.search(query, self.config.max_candidates, hops=chosen_hops)
        items = local
        degraded: list[str] = []

        def refreshed_local() -> dict[str, Any]:
            # Awaited model/lock work may outlive edits or a source-level forget.
            # Return a current local projection once; never retry the network or
            # associate candidates from the old revision with the new cache.
            current = self.memory.search(query, self.config.max_candidates, hops=chosen_hops)
            return self._result(current[:limit], [*degraded, "memory_changed"])

        if use_semantic and query.strip() and self.media is not None:
            try:
                async with asyncio.timeout(self.config.query_timeout):
                    async with self._media_lock:
                        if self.memory.revision != revision:
                            return refreshed_local()
                        semantic: list[dict[str, Any]] = []
                        if self.media.config.embedding is not None:
                            vectors = await self.media.embed([query])
                            if self.memory.revision != revision:
                                return refreshed_local()
                            if len(vectors) != 1:
                                raise ValueError("查询向量数量错误")
                            semantic = self.memory.vector_search(
                                vectors[0], self._model_key(), self.config.max_candidates
                            )
                            if (
                                self.memory.vector_count(self._model_key())
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
                degraded.append("semantic_unavailable:" + type(error).__name__)
                items = local
        if self.memory.revision != revision:
            return refreshed_local()
        result = self._result(items[:limit], degraded)
        if use_semantic:
            self._remember(query, limit, chosen_hops, result)
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
    ) -> str:
        """宿主已检查当前开关和预算；选材不依赖启动时的旧字符上限。"""
        if max_chars <= 0:
            return ""
        limit = self.config.context_limit if limit is None else limit
        cached = self._cached(query, limit, self.config.graph_hops)
        if cached is None:
            for key in reversed(self._cache):
                if key[0] == query and key[2] == self.config.graph_hops:
                    cached = deepcopy(self._cache[key])
                    break
        recalled = recalled_for_prompt(
            self.memory,
            query,
            limit,
            latest_user_id=latest_user_id,
            cached=cached["memories"] if cached is not None else None,
            hops=self.config.graph_hops,
        )
        return render_context(
            query,
            recalled,
            self.memory.owner_profile(limit),
            limit=limit,
            budget=max_chars,
            memory=self.memory,
        )
