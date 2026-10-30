"""The shared worker serves accepted indexes while ordinary jobs keep arriving."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from test_memory_pipeline import MockMedia, done, extraction, message

from muli.media import EmbeddingPurpose
from muli.memory import Memory
from muli.memory_pipeline import Extractor, MemoryPipeline, MemoryPipelineConfig
from muli.store import Store


async def test_accepted_index_runs_between_continuously_ready_ordinary_jobs(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    second_entered, second_release = asyncio.Event(), asyncio.Event()
    indexed_content = "已完成提炼、等待显式索引的资料原文"

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"] == "index-source":
            return extraction(source, layer="semantic")
        if source["id"] == "ordinary-two":
            second_entered.set()
            await second_release.wait()
        return {"memories": [], "aliases": {}}

    pipeline = MemoryPipeline(
        memory,
        extractor=extract,
        config=MemoryPipelineConfig(max_pending_jobs=2),
    )
    try:
        pipeline.ingest(message("index-source", indexed_content))
        await pipeline.start()
        assert (await done(pipeline, "index-source"))["state"] == "done"
        await pipeline.close()

        media = MockMedia()
        pipeline.media = media
        await pipeline.start()
        accepted = pipeline.start_index(["index-source"], key="fair-index-service")
        assert accepted["state"] == "queued"
        assert pipeline.ingest(message("ordinary-one", "第一条普通来源"))["state"] == "queued"
        assert pipeline.ingest(message("ordinary-two", "第二条普通来源"))["state"] == "deferred"

        # A finite second extraction is the scheduling boundary, not a timeout
        # used to infer starvation. The real queue must first replenish its
        # ordinary slot from the deferred source while the index stays admitted.
        async with asyncio.timeout(3):
            await second_entered.wait()
        first = pipeline.job("ordinary-one")
        assert first is not None and first["state"] == "done"
        assert pipeline.status()["deferred"] == 0
        status = pipeline.index_status(accepted["plan_id"])
        assert status["vectors_written"] > 0, "已接纳的索引计划应在连续就绪的普通作业之间获得服务"
        assert any(indexed_content in values for values in media.embed_calls)
    finally:
        second_release.set()
        await pipeline.close()
        store.close()


_INDEX_CONTENT = "已有提炼结果的索引资料需要新向量"


class _GatedIndexMedia(MockMedia):
    def __init__(self, *, fail_index: bool = False) -> None:
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.index_calls = 0
        self.trace: list[str] = []
        self.fail_index = fail_index

    async def embed(
        self, texts: Sequence[str], *, purpose: EmbeddingPurpose = "document"
    ) -> list[list[float]]:
        vectors = await super().embed(texts, purpose=purpose)
        if any(_INDEX_CONTENT in value for value in texts):
            self.index_calls += 1
            self.trace.append(f"index-{self.index_calls}")
            if self.index_calls == 1:
                self.entered.set()
                await self.release.wait()
            if self.fail_index:
                raise RuntimeError("offline index failure")
        return vectors


@asynccontextmanager
async def _accepted_index(
    tmp_path: Path,
    ordinary_extractor: Extractor,
    media: _GatedIndexMedia,
    *,
    retry_backoff_seconds: float = 0,
) -> AsyncIterator[tuple[MemoryPipeline, str]]:
    store = Store(tmp_path / "memory.db")

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"] == "index-source":
            return extraction(source, layer="semantic")
        return await ordinary_extractor(source)

    pipeline = MemoryPipeline(
        Memory(store),
        extractor=extract,
        config=MemoryPipelineConfig(
            max_pending_jobs=2,
            max_facts_per_source=1,
            retry_backoff_seconds=retry_backoff_seconds,
        ),
    )
    try:
        pipeline.ingest(message("index-source", _INDEX_CONTENT))
        await pipeline.start()
        assert (await done(pipeline, "index-source"))["state"] == "done"
        await pipeline.close()
        pipeline.media = media
        await pipeline.start()
        accepted = pipeline.start_index(["index-source"], key="multi-batch-fairness")
        plan_id = accepted["plan_id"]
        plan = store.get("memory_index_plan", plan_id)
        assert plan is not None
        assert len(plan["sources"][0]["items"]) > pipeline.config.max_facts_per_source
        yield pipeline, plan_id
    finally:
        media.release.set()
        await pipeline.close()
        store.close()


async def _index_state(pipeline: MemoryPipeline, plan_id: str, state: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        async with pipeline.changed:
            await pipeline.changed.wait_for(
                lambda: pipeline.index_status(plan_id)["state"] == state
            )
    return pipeline.index_status(plan_id)


async def test_multi_batch_index_yields_to_a_ready_ordinary_job(tmp_path: Path) -> None:
    media = _GatedIndexMedia()
    ordinary_entered, ordinary_release = asyncio.Event(), asyncio.Event()
    observed: list[dict[str, Any]] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        media.trace.append(source["id"])
        if source["id"] == "ordinary-one":
            return {"memories": [], "aliases": {}}
        observed.append(pipeline.index_status(plan_id))
        ordinary_entered.set()
        await ordinary_release.wait()
        return {"memories": [], "aliases": {}}

    async with _accepted_index(tmp_path, extract, media) as (pipeline, plan_id):
        assert pipeline.ingest(message("ordinary-one", "第一条普通来源"))["state"] == "queued"
        assert pipeline.ingest(message("ordinary-two", "第二条普通来源"))["state"] == "deferred"
        async with asyncio.timeout(3):
            await media.entered.wait()
        assert media.trace == ["ordinary-one", "index-1"]
        first = pipeline.job("ordinary-one")
        second = pipeline.job("ordinary-two")
        assert first is not None and first["state"] == "done"
        assert second is not None and second["state"] == "queued"
        media.release.set()
        try:
            async with asyncio.timeout(3):
                await ordinary_entered.wait()
            assert observed[0]["state"] == "queued"
            assert observed[0]["vectors_written"] == 1
            assert media.index_calls == 1
            assert media.trace == ["ordinary-one", "index-1", "ordinary-two"]
        finally:
            ordinary_release.set()
        assert (await done(pipeline, "ordinary-two"))["state"] == "done"
        status = await _index_state(pipeline, plan_id, "done")
        assert status["vectors_written"] == 2 and media.index_calls == 2
        assert media.trace == ["ordinary-one", "index-1", "ordinary-two", "index-2"]
        assert all(
            len(values) == 1
            for values in media.embed_calls
            if any(_INDEX_CONTENT in value for value in values)
        )


@pytest.mark.parametrize("retry_backoff_seconds", [0.0, 60.0])
async def test_retrying_ordinary_job_gives_a_ready_index_a_turn(
    tmp_path: Path, retry_backoff_seconds: float
) -> None:
    media = _GatedIndexMedia()
    attempts: list[str] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        attempts.append(source["id"])
        raise RuntimeError("offline ordinary failure")

    async with _accepted_index(
        tmp_path, extract, media, retry_backoff_seconds=retry_backoff_seconds
    ) as (pipeline, plan_id):
        assert (
            pipeline.ingest(message("retrying-ordinary", "普通来源的离线提炼会失败"))["state"]
            == "queued"
        )
        async with asyncio.timeout(3):
            await media.entered.wait()
        # Both immediate failures and a future retry leave the accepted index
        # a service opportunity after the very first ordinary attempt.
        assert attempts == ["retrying-ordinary"]
        job = pipeline.job("retrying-ordinary")
        assert job is not None and job["state"] == "retry" and job["attempts"] == 1
        assert media.index_calls == 1
        media.release.set()
        assert (await _index_state(pipeline, plan_id, "done"))["vectors_written"] == 2


@pytest.mark.parametrize("retry_backoff_seconds", [0.0, 60.0])
async def test_retrying_index_gives_a_ready_ordinary_job_a_turn(
    tmp_path: Path, retry_backoff_seconds: float
) -> None:
    media = _GatedIndexMedia(fail_index=True)
    ordinary_entered, ordinary_release = asyncio.Event(), asyncio.Event()
    observed: list[tuple[int, dict[str, Any]]] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        observed.append((media.index_calls, pipeline.index_status(plan_id)))
        ordinary_entered.set()
        await ordinary_release.wait()
        return {"memories": [], "aliases": {}}

    async with _accepted_index(
        tmp_path, extract, media, retry_backoff_seconds=retry_backoff_seconds
    ) as (pipeline, plan_id):
        async with asyncio.timeout(3):
            await media.entered.wait()
        assert (
            pipeline.ingest(message("ordinary", "索引重试期间到达的普通来源"))["state"] == "queued"
        )
        media.release.set()
        try:
            async with asyncio.timeout(3):
                await ordinary_entered.wait()
            index_calls, status = observed[0]
            assert index_calls == 1
            assert status["state"] == "retry" and status["last_error"] == "index_failed"
        finally:
            ordinary_release.set()
        assert (await done(pipeline, "ordinary"))["state"] == "done"
        if retry_backoff_seconds:
            assert media.index_calls == 1
            assert pipeline.index_status(plan_id)["state"] == "retry"
        else:
            assert (await _index_state(pipeline, plan_id, "failed"))["vectors_written"] == 0
            assert media.index_calls == pipeline.config.max_attempts
