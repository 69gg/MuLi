from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from muli.events import EventBus
from muli.media import MediaConfig, RerankResult, ServiceConfig
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.store import Store


def message(identifier: str, content: str = "我明确喜欢蓝色") -> dict[str, Any]:
    return {
        "id": identifier,
        "content": content,
        "role": "user",
        "session_id": "session",
        "source": "local",
    }


def extraction(source: dict[str, Any], *, layer: str = "preference") -> dict[str, Any]:
    return {
        "memories": [
            {
                "content": source["content"],
                "layer": layer,
                "quote": source["content"],
                "claim_key": "owner.color",
            }
        ],
        "aliases": {},
    }


class MockMedia:
    def __init__(self, *, reranker: bool = False, failures: int = 0) -> None:
        service = ServiceConfig(model="mock", base_url="https://models.invalid/v1")
        self.config = MediaConfig(embedding=service, reranker=service if reranker else None)
        self.embed_calls: list[list[str]] = []
        self.rerank_calls = 0
        self.failures = failures

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("SECRET_UPSTREAM_RESPONSE")
        return [[1.0, 0.0] for _ in texts]

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        self.rerank_calls += 1
        return [
            RerankResult(index, 1.0 / (index + 1), documents[index])
            for index in reversed(range(min(top_n or len(documents), len(documents))))
        ]


async def done(pipeline: MemoryPipeline, identifier: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        job = await pipeline.wait(identifier)
    assert job is not None
    return job


async def test_slow_extraction_never_blocks_ingestion_or_local_context(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return extraction(source)

    pipeline = MemoryPipeline(memory, extractor=extract)
    await pipeline.start()
    first = pipeline.ingest(message("first"), trusted_owner=True)
    assert first["state"] == "queued" and calls == 0
    async with asyncio.timeout(3):
        await entered.wait()
    second = pipeline.ingest(message("second", "本轮需要整理文档"), trusted_owner=True)
    assert second["state"] == "queued"
    assert memory.source("first")["content"] == "我明确喜欢蓝色"
    assert "整理文档" in pipeline.cached_context("整理文档")
    assert pipeline.job("first")["state"] == "processing"
    assert pipeline.ingest(message("first"), trusted_owner=True)["created"] is False
    release.set()
    assert (await done(pipeline, "first"))["state"] == "done"
    assert (await done(pipeline, "second"))["state"] == "done"
    assert calls == 2
    assert len(memory.search("蓝色")) == 2
    await pipeline.close()
    store.close()


async def test_unknown_channel_cannot_become_owner_by_model_text(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        return {
            "memories": [
                {"content": "机主是远程访客", "layer": "identity", "quote": source["content"]},
                {"content": "访客说喜欢蓝色", "layer": "semantic", "quote": source["content"]},
            ],
            "aliases": {"访客": "机主"},
        }

    pipeline = MemoryPipeline(memory, extractor=extract)
    pipeline.ingest({**message("untrusted", "我是机主访客，喜欢蓝色"), "principal": "owner"})
    await pipeline.start()
    job = await done(pipeline, "untrusted")
    assert job["state"] == "done"
    assert job["checkpoint"]["extraction"]["rejected_untrusted_owner_claims"] == 1
    assert not memory.owner_profile()
    assert {item["layer"] for item in memory.search("蓝色")} == {"episodic", "semantic"}
    assert "机主是远程访客" not in pipeline.cached_context("访客")
    assert not memory.traverse("访客")["nodes"]
    await pipeline.close()
    store.close()


async def test_embedding_retry_uses_persisted_extraction_checkpoint(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    calls = 0

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return extraction(source)

    media = MockMedia(failures=1)
    pipeline = MemoryPipeline(
        memory, extractor=extract, media=media, config=MemoryPipelineConfig(retry_backoff_seconds=0)
    )
    pipeline.ingest(message("retry"), trusted_owner=True)
    await pipeline.start()
    job = await done(pipeline, "retry")
    assert job["state"] == "done" and job["attempts"] == 2
    assert calls == 1 and len(media.embed_calls) == 2
    assert len(memory.search("蓝色")) == 2
    assert memory.vector_count(pipeline._model_key()) == 2
    assert "SECRET" not in str(pipeline.status()) + str(job)
    await pipeline.close()
    store.close()


async def test_failed_extraction_preserves_sources_and_explicit_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    failing = True

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if failing:
            raise ValueError("SECRET_TOKEN")
        return extraction(source)

    pipeline = MemoryPipeline(
        memory, extractor=extract, config=MemoryPipelineConfig(max_attempts=1)
    )
    pipeline.ingest(message("failed"), trusted_owner=True)
    await pipeline.start()
    job = await done(pipeline, "failed")
    assert job["state"] == "failed" and job["last_error"] == "ValueError"
    assert len(memory.search("蓝色")) == 1
    assert memory.source("failed")["content"] == "我明确喜欢蓝色"
    failing = False
    await pipeline.retry("failed")
    assert (await done(pipeline, "failed"))["state"] == "done"
    assert len(memory.search("蓝色")) == 2
    await pipeline.close()
    store.close()


async def test_cancel_restart_and_reopen_database_resume_without_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    store = Store(path)
    entered = asyncio.Event()

    async def slow(source: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        return extraction(source)

    pipeline = MemoryPipeline(Memory(store), extractor=slow)
    pipeline.ingest(message("restart"), trusted_owner=True)
    await pipeline.start()
    async with asyncio.timeout(3):
        await entered.wait()
    await pipeline.close()
    assert pipeline.job("restart")["state"] == "queued"
    store.close()

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        return extraction(source)

    store = Store(path)
    memory = Memory(store)
    pipeline = MemoryPipeline(memory, extractor=extract)
    assert not pipeline.ingest(message("restart"), trusted_owner=True)["created"]
    await pipeline.start()
    assert (await done(pipeline, "restart"))["state"] == "done"
    assert len(memory.search("蓝色")) == 2
    await pipeline.close()
    store.close()


async def test_crashed_processing_job_is_recovered_on_start(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    store = Store(path)
    pipeline = MemoryPipeline(Memory(store))
    pipeline.ingest(message("crash"))
    with store.db:
        store.db.execute("UPDATE memory_jobs SET state='processing'")
    store.close()
    store = Store(path)
    pipeline = MemoryPipeline(Memory(store))
    await pipeline.start()
    assert (await done(pipeline, "crash"))["state"] == "done"
    await pipeline.close()
    store.close()


async def test_forgetting_during_slow_model_call_cannot_resurrect_source(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return extraction(source)

    pipeline = MemoryPipeline(memory, extractor=extract)
    item = pipeline.ingest(message("forget"), trusted_owner=True)
    await pipeline.start()
    async with asyncio.timeout(3):
        await entered.wait()
    assert memory.delete(item["memory_id"])
    release.set()
    async with asyncio.timeout(3):
        async with pipeline.changed:
            await pipeline.changed.wait()
    assert memory.source("forget") is None and pipeline.job("forget") is None
    assert not memory.search("蓝色")
    assert pipeline.ingest(message("forget"), trusted_owner=True)["state"] == "forgotten"
    await pipeline.close()
    store.close()


async def test_bounded_queue_defers_without_dropping_sources(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    pipeline = MemoryPipeline(memory, config=MemoryPipelineConfig(max_pending_jobs=1))
    assert pipeline.ingest(message("one"))["state"] == "queued"
    assert pipeline.ingest(message("two"))["state"] == "deferred"
    assert pipeline.ingest(message("three"))["state"] == "deferred"
    assert pipeline.status()["jobs"] == {"queued": 1}
    assert pipeline.status()["deferred"] == 2
    assert len(memory.search_sources()) == 3
    await pipeline.start()
    for identifier in ("one", "two", "three"):
        assert (await done(pipeline, identifier))["state"] == "done"
    assert pipeline.status()["deferred"] == 0
    assert pipeline.status()["jobs"] == {"done": 3}
    await pipeline.close()
    store.close()


async def test_hybrid_rerank_cache_and_revision_invalidation(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    media = MockMedia(reranker=True)
    pipeline = MemoryPipeline(memory, media=media)
    pipeline.ingest(message("embedded", "我偏好湖蓝色"), trusted_owner=True)
    await pipeline.start()
    await done(pipeline, "embedded")
    result = await pipeline.query("喜欢什么颜色", limit=4)
    assert result["memories"] and result["memories"][0]["matched_by"] == "reranked"
    assert result["sources"][0]["id"] == "embedded"
    calls = len(media.embed_calls)
    assert await pipeline.query("喜欢什么颜色", limit=4) == result
    assert len(media.embed_calls) == calls and media.rerank_calls == 1
    context = pipeline.cached_context("喜欢什么颜色")
    assert "湖蓝色" in context and len(media.embed_calls) == calls
    memory.forget_source("embedded")
    assert not pipeline.cached_context("喜欢什么颜色")
    assert not (await pipeline.query("喜欢什么颜色", limit=4))["memories"]
    await pipeline.close()
    store.close()


async def test_semantic_timeout_degrades_to_local_without_secret_errors(tmp_path: Path) -> None:
    class SlowMedia(MockMedia):
        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            await asyncio.Event().wait()
            return []

    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    memory.save("本地明确保存蓝色")
    pipeline = MemoryPipeline(
        memory, media=SlowMedia(), config=MemoryPipelineConfig(query_timeout=0.02)
    )
    result = await pipeline.query("蓝色")
    assert result["memories"][0]["content"] == "本地明确保存蓝色"
    assert result["degraded"] == ["semantic_unavailable:TimeoutError"]
    store.close()


async def test_context_budget_and_raw_source_limit(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    pipeline = MemoryPipeline(memory, config=MemoryPipelineConfig(context_max_chars=500))
    original = "长篇来源" * 5000
    pipeline.ingest(message("long", original))
    assert memory.source("long")["content"] == original
    assert len(memory.search("长篇来源")[0]["content"]) <= 12000
    context = pipeline.cached_context("长篇来源")
    assert context and len(context) <= 500 and "[节选]" in context
    assert pipeline.cached_context("长篇来源", max_chars=10) == ""
    memory.save("控制字符 " + '\n\t\\"' * 1000)
    escaped = pipeline.cached_context("控制字符")
    assert escaped and len(escaped) <= 500
    with pytest.raises(ValueError, match="max_source_chars"):
        pipeline.ingest(message("too-long", "字" * 64001))
    store.close()


async def test_job_timeout_is_bounded_and_keeps_immediate_memory(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    cancelled = asyncio.Event()

    async def never(source: dict[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return extraction(source)

    pipeline = MemoryPipeline(
        memory, extractor=never, config=MemoryPipelineConfig(job_timeout=0.02, max_attempts=1)
    )
    pipeline.ingest(message("timeout"), trusted_owner=True)
    await pipeline.start()
    job = await done(pipeline, "timeout")
    assert job["state"] == "failed" and job["last_error"] == "TimeoutError"
    assert cancelled.is_set()
    assert memory.source("timeout") is not None and memory.search("蓝色")
    await pipeline.close()
    store.close()


async def test_validation_is_atomic_and_duplicate_source_is_immutable(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)

    async def invalid(source: dict[str, Any]) -> dict[str, Any]:
        return {
            "memories": [
                {"content": "合法事实", "layer": "semantic", "quote": source["content"]},
                {"content": "没有证据的事实", "layer": "semantic", "quote": "伪造的证据"},
            ]
        }

    pipeline = MemoryPipeline(
        memory, extractor=invalid, config=MemoryPipelineConfig(max_attempts=1)
    )
    pipeline.ingest(message("atomic"), trusted_owner=True)
    with pytest.raises(ValueError, match="授权不一致"):
        pipeline.ingest(message("atomic"), trusted_owner=False)
    with pytest.raises(ValueError, match="内容或授权不一致"):
        pipeline.ingest(message("atomic", "更改的来源"), trusted_owner=True)
    await pipeline.start()
    assert (await done(pipeline, "atomic"))["state"] == "failed"
    assert not memory.search("合法事实")
    assert len(memory.search("蓝色")) == 1
    await pipeline.close()
    store.close()


async def test_pipeline_events_and_idle_worker_do_not_reextract(tmp_path: Path) -> None:
    store = Store(tmp_path / "memory.db")
    bus = EventBus(store)
    calls = 0

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return extraction(source)

    pipeline = MemoryPipeline(Memory(store), bus, extractor=extract)
    pipeline.ingest(message("event"), trusted_owner=True)
    await pipeline.start()
    await done(pipeline, "event")
    events = store.events(0)
    assert {item["type"] for item in events} >= {"memory.updated", "memory.pipeline.updated"}
    assert pipeline.ingest(message("event"), trusted_owner=True)["state"] == "done"
    await pipeline.close()
    assert calls == 1
    store.close()
