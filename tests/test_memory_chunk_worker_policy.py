"""Ordinary source jobs retain revocation, version and paid-prefix boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_memory_pipeline import MockMedia, done, message

from muli.events import EventBus
from muli.memory import Memory
from muli.memory_chunks import MemoryChunkSettings, MemoryChunkSettingsStore
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.store import Store

LONG_TEXT = (
    "".join(f"记录{index:04d}：普通背景资料。\n" for index in range(1000))[:12_980]
    + "尾部唯一原始证据。"
)


def worker_config() -> MemoryPipelineConfig:
    return MemoryPipelineConfig(auto_extract=False, max_attempts=1, retry_backoff_seconds=3600)


async def published(bus: EventBus, source_id: str, after: int) -> None:
    """Wait for the real worker's persisted completion notification, even after forget."""

    def committed() -> bool:
        return (
            bus.store.db.execute(
                "SELECT 1 FROM events WHERE id>? AND type='memory.pipeline.updated' "
                "AND json_extract(data,'$.source_id')=? LIMIT 1",
                (after, source_id),
            ).fetchone()
            is not None
        )

    async with asyncio.timeout(3):
        async with bus.changed:
            await bus.changed.wait_for(committed)


class HeldMedia(MockMedia):
    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = await super().embed(texts)
        if len(self.embed_calls) == 1:
            self.entered.set()
            await self.release.wait()
        return vectors


class FailSecondBatch(MockMedia):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if len(self.embed_calls) == 1:
            self.embed_calls.append(list(texts))
            raise RuntimeError("controlled second embedding batch failure")
        return await super().embed(texts)


async def test_ordinary_chunk_job_forgotten_while_waiting_for_media_lock_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "memory.db")
    memory, bus, media = Memory(store), EventBus(store), MockMedia()
    pipeline = MemoryPipeline(memory, bus, media=media, config=worker_config())
    waiting = asyncio.Event()
    acquire = pipeline._media_lock.acquire

    async def observed_acquire() -> bool:
        if pipeline._media_lock.locked() and asyncio.current_task() is pipeline._worker:
            waiting.set()
        return await acquire()

    await pipeline._media_lock.acquire()
    held = True
    monkeypatch.setattr(pipeline._media_lock, "acquire", observed_acquire)
    source_id = "ordinary-waiting-source"
    after = store.event_position()
    try:
        pipeline.ingest(message(source_id, LONG_TEXT))
        await pipeline.start()
        await asyncio.wait_for(waiting.wait(), 3)
        job = pipeline.job(source_id)
        assert job is not None and job["state"] == "processing"
        record = job["checkpoint"]["source_chunks"]
        assert record["next_index"] == record["plan"]["count"] > 0
        assert not media.embed_calls
        assert memory.forget_source(source_id)
        pipeline._media_lock.release()
        held = False
        await published(bus, source_id, after)
        assert memory.source(source_id) is None and pipeline.job(source_id) is None
        assert media.embed_calls == []
        assert store.db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 0
        assert (
            store.db.execute(
                "SELECT count(*) FROM memory_meta WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        if held:
            pipeline._media_lock.release()
        await pipeline.close()
        store.close()


@pytest.mark.parametrize("change", ["edit", "forget"])
async def test_inflight_chunk_batch_cannot_write_old_versions_and_retry_uses_current_text(
    tmp_path: Path, change: str
) -> None:
    store = Store(tmp_path / "memory.db")
    memory, bus, media = Memory(store), EventBus(store), HeldMedia()
    pipeline = MemoryPipeline(memory, bus, media=media, config=worker_config())
    source_id = "ordinary-inflight-source"
    after = store.event_position()
    try:
        receipt = pipeline.ingest(message(source_id, LONG_TEXT))
        await pipeline.start()
        await asyncio.wait_for(media.entered.wait(), 3)
        job = pipeline.job(source_id)
        assert job is not None
        record = job["checkpoint"]["source_chunks"]
        chunk_ids = record["memory_ids"]
        first_chunk_id = chunk_ids[0]
        assert len(media.embed_calls) == 1
        assert len(media.embed_calls[0]) == min(record["plan"]["batch_size"], len(chunk_ids))
        assert store.db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 0
        archive = memory.get(receipt["memory_id"])
        assert archive is not None and archive["content"] not in media.embed_calls[0]
        previous = memory.get(first_chunk_id)
        assert previous is not None and previous["content"] in media.embed_calls[0]
        corrected = "真实更正后的条目；旧请求的向量不得回写。"
        if change == "edit":
            current = memory.edit(first_chunk_id, content=corrected)
            assert current["version"] > previous["version"]
        else:
            assert memory.forget_source(source_id)
        media.release.set()
        await published(bus, source_id, after)
        assert store.db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 0
        if change == "forget":
            assert memory.source(source_id) is None and pipeline.job(source_id) is None
            await pipeline.retry(source_id)
            assert memory.source(source_id) is None and pipeline.job(source_id) is None
            assert len(media.embed_calls) == 1
        else:
            failed = pipeline.job(source_id)
            assert failed is not None and failed["state"] == "failed"
            assert failed["last_error"] == "ValueError"
            await pipeline.retry(source_id)
            finished = await done(pipeline, source_id)
            assert finished["state"] == "done"
            assert corrected in media.embed_calls[1]
            assert previous["content"] not in media.embed_calls[1]
            assert memory.get(first_chunk_id)["version"] == current["version"]
            assert memory.get(receipt["memory_id"]) == archive
            assert finished["checkpoint"]["source_chunks"]["memory_ids"] == chunk_ids
            vectors = store.db.execute(
                "SELECT v.memory_id FROM memory_vectors v "
                "JOIN memory_meta x ON x.memory_id=v.memory_id WHERE x.source_id=?",
                (source_id,),
            ).fetchall()
            assert {row[0] for row in vectors} == set(chunk_ids)
    finally:
        media.release.set()
        await pipeline.close()
        store.close()


async def test_failed_batch_reopens_with_frozen_plan_paid_prefix_and_isolated_equal_digest_sources(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.db"
    extracted: list[str] = []

    async def forbidden_extract(source: dict[str, Any]) -> dict[str, Any]:
        extracted.append(source["id"])
        raise AssertionError("auto_extract=False must not invoke source extraction")

    store = Store(path)
    memory, media = Memory(store), FailSecondBatch()
    pipeline = MemoryPipeline(
        memory, extractor=forbidden_extract, media=media, config=worker_config()
    )
    source_id = "resume-first-source"
    try:
        pipeline.ingest(message(source_id, LONG_TEXT))
        await pipeline.start()
        failed = await done(pipeline, source_id)
        assert failed["state"] == "failed" and failed["last_error"] == "RuntimeError"
        record = failed["checkpoint"]["source_chunks"]
        chunk_ids = record["memory_ids"]
        assert record["next_index"] == record["plan"]["count"] == len(chunk_ids)
        assert record["plan"]["chunk_chars"] == 2000 and record["plan"]["batch_size"] == 4
        batch_size = record["plan"]["batch_size"]
        assert [len(batch) for batch in media.embed_calls] == [
            batch_size,
            len(chunk_ids) - batch_size,
        ]
        original_ids = failed["checkpoint"]["memory_ids"]
        assert len(original_ids) == 1
        archive = memory.get(original_ids[0])
        assert archive is not None
        paid = store.db.execute(
            "SELECT v.memory_id FROM memory_vectors v "
            "JOIN memory_meta x ON x.memory_id=v.memory_id WHERE x.source_id=?",
            (source_id,),
        ).fetchall()
        assert {row[0] for row in paid} == set(chunk_ids[:batch_size])
        paid_texts, pending_texts = media.embed_calls
        assert set(paid_texts).isdisjoint(pending_texts)
        assert archive["content"] not in [*paid_texts, *pending_texts]
        assert all(
            len(text) <= record["plan"]["chunk_chars"] for text in [*paid_texts, *pending_texts]
        )
        assert extracted == []
        MemoryChunkSettingsStore(store).replace(MemoryChunkSettings(batch_size=2))
    finally:
        await pipeline.close()
        store.close()

    reopened = Store(path)
    memory, resumed_media = Memory(reopened), MockMedia()
    resumed = MemoryPipeline(
        memory, extractor=forbidden_extract, media=resumed_media, config=worker_config()
    )
    try:
        assert MemoryChunkSettingsStore(reopened).read().batch_size == 2
        saved = resumed.job(source_id)
        assert saved is not None and saved["checkpoint"]["source_chunks"] == record
        await resumed.start()
        await resumed.retry(source_id)
        finished = await done(resumed, source_id)
        assert finished["state"] == "done"
        assert finished["checkpoint"]["source_chunks"] == record
        assert finished["checkpoint"]["memory_ids"] == original_ids
        assert resumed_media.embed_calls == [pending_texts]
        assert len(resumed_media.embed_calls[0]) == len(chunk_ids) - batch_size
        assert set(paid_texts).isdisjoint(resumed_media.embed_calls[0])
        assert memory.get(original_ids[0]) == archive
        assert reopened.db.execute(
            "SELECT count(*) FROM memory_meta WHERE source_id=?", (source_id,)
        ).fetchone()[0] == len(original_ids) + len(chunk_ids)
        vectors = reopened.db.execute(
            "SELECT v.memory_id FROM memory_vectors v "
            "JOIN memory_meta x ON x.memory_id=v.memory_id WHERE x.source_id=?",
            (source_id,),
        ).fetchall()
        assert {row[0] for row in vectors} == set(chunk_ids)

        second_id = "resume-second-source"
        resumed.ingest(message(second_id, LONG_TEXT))
        second = await done(resumed, second_id)
        assert second["state"] == "done"
        first_source, second_source = memory.source(source_id), memory.source(second_id)
        assert first_source is not None and second_source is not None
        assert first_source["digest"] == second_source["digest"]
        second_record = second["checkpoint"]["source_chunks"]
        assert second_record["plan"]["batch_size"] == 2
        assert second_record["plan"]["chunk_chars"] == record["plan"]["chunk_chars"]
        assert second_record["plan"]["count"] == record["plan"]["count"]
        assert set(second_record["memory_ids"]).isdisjoint(chunk_ids)
        for identifier in chunk_ids:
            assert memory.get(identifier)["source_id"] == source_id
        for identifier in second_record["memory_ids"]:
            assert memory.get(identifier)["source_id"] == second_id
        for left, right in zip(chunk_ids, second_record["memory_ids"], strict=True):
            assert memory.get(left)["content"] == memory.get(right)["content"]
        assert all(len(batch) <= 2 for batch in resumed_media.embed_calls[1:])
        assert sum(len(batch) for batch in resumed_media.embed_calls[1:]) == len(
            second_record["memory_ids"]
        )
        second_archive_ids = second["checkpoint"]["memory_ids"]
        assert len(second_archive_ids) == 1 and memory.get(second_archive_ids[0]) is not None
        assert reopened.db.execute(
            "SELECT count(*) FROM memory_meta WHERE source_id=?", (second_id,)
        ).fetchone()[0] == len(second_archive_ids) + len(second_record["memory_ids"])
        second_vectors = reopened.db.execute(
            "SELECT v.memory_id FROM memory_vectors v "
            "JOIN memory_meta x ON x.memory_id=v.memory_id WHERE x.source_id=?",
            (second_id,),
        ).fetchall()
        assert {row[0] for row in second_vectors} == set(second_record["memory_ids"])
        assert extracted == []
    finally:
        await resumed.close()
        reopened.close()
