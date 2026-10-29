"""Collected-source terminal failures keep bounded, revocable host evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_memory_pipeline import MockMedia

from muli.attachment_memory_source import attachment_source
from muli.config import Settings
from muli.document_reading import document_revision
from muli.events import EventBus
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.memory_source_failure import (
    FAILURE_CHECKPOINT,
    MemorySourceFailure,
    attachment_revision,
    failure_current,
)
from muli.proactive import FrozenRouting
from muli.store import Store, new_id, now
from muli.system_events import AttentionSource, attention_source


async def reject(source: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("PRIVATE_SOURCE_OR_UPSTREAM_CREDENTIAL")


def pipeline_for(store: Store, *, attempts: int = 1, bus: EventBus | None = None) -> MemoryPipeline:
    return MemoryPipeline(
        Memory(store),
        bus or EventBus(store),
        extractor=reject,
        config=MemoryPipelineConfig(max_attempts=attempts),
        failure_routing=lambda: FrozenRouting.independent(Settings()),
    )


@pytest.fixture
def pipeline(tmp_path: Path) -> Iterator[MemoryPipeline]:
    store = Store(tmp_path / "memory.db")
    try:
        yield pipeline_for(store)
    finally:
        store.close()


def collect(pipeline: MemoryPipeline, *, format_name: str = "text") -> str:
    identifier, text = new_id(), "私有收藏正文🙂：核对所有附件摘要。"
    if format_name in {"text", "asr"}:
        checksum = hashlib.sha256(text.encode()).hexdigest()
        document = None
    else:
        checksum = hashlib.sha256(b"original-document-bytes").hexdigest()
        document = {
            "format": format_name,
            "scope": {"pdf": "pdf_text_layer", "docx": "docx_main_body_text"}[format_name],
            "source_revision": checksum,
            "revision": document_revision(checksum, text.encode()),
        }
    source = attachment_source(
        identifier, text, checksum, now(), document=document, transcript=format_name == "asr"
    )
    pipeline.ingest(source)
    return source["id"]


def failure_events(store: Store) -> list[dict[str, Any]]:
    return [
        event
        for event in store.events(limit=1000)
        if event["type"] == "memory.pipeline.updated" and "source_failure" in event["data"]
    ]


def source_for(pipeline: MemoryPipeline, event: dict[str, Any]) -> AttentionSource | None:
    return attention_source(
        pipeline.memory.store,
        event,
        ("memory",),
        max_output_chars=6000,
        memory_source_chars=pipeline.config.max_source_chars,
    )


@pytest.mark.parametrize("format_name", ["text", "pdf", "docx", "asr"])
async def test_real_collected_source_has_one_complete_private_terminal_proof(
    pipeline: MemoryPipeline, format_name: str
) -> None:
    source_id = collect(pipeline, format_name=format_name)
    await pipeline._run_job(source_id)
    job = pipeline.job(source_id)
    events = failure_events(pipeline.memory.store)
    assert job is not None and job["state"] == "failed" and len(events) == 1
    assert job["last_error"] == "RuntimeError"
    source = source_for(pipeline, events[0])
    assert source is not None and source.memory_failure_proof is not None
    proof = source.memory_failure_proof
    assert proof.dump() == job["checkpoint"][FAILURE_CHECKPOINT]
    assert proof.source_id == source_id
    assert (
        attachment_revision(pipeline.memory.store, source_id, max_source_chars=64000)
        == proof.revision
    )
    assert proof.routing.classification == "independent"
    detail = json.loads(source.detail)
    assert detail["source_id"] == source_id and detail["revision"] == proof.revision
    assert "私有收藏正文" not in source.detail
    assert "PRIVATE_SOURCE_OR_UPSTREAM_CREDENTIAL" not in json.dumps(events)
    if format_name == "asr":
        assert detail["evidence_scope"] == "asr_transcript_text"
        assert ":transcript:v1:asr_text:" in detail["source"]
        assert "不证明原音频" in detail["notice"]
    else:
        assert "evidence_scope" not in detail
    await pipeline._publish(source_id)
    await pipeline._run_job(source_id)
    assert len(failure_events(pipeline.memory.store)) == 1


async def test_retry_attempts_do_not_notify_until_the_actual_terminal_transition(
    pipeline: MemoryPipeline,
) -> None:
    pipeline.config = replace(pipeline.config, max_attempts=2)
    source_id = collect(pipeline)
    await pipeline._run_job(source_id)
    job = pipeline.job(source_id)
    assert job is not None and job["state"] == "retry"
    assert failure_events(pipeline.memory.store) == []
    await pipeline._run_job(source_id)
    assert len(failure_events(pipeline.memory.store)) == 1


@pytest.mark.parametrize(
    "damage",
    ["chat", "report", "id", "principal", "session", "trusted", "marker", "content", "digest"],
)
async def test_uncollected_or_unverifiable_source_cannot_claim_failure_notice(
    pipeline: MemoryPipeline, damage: str
) -> None:
    identifier, text = new_id(), "完整来源正文"
    source = attachment_source(identifier, text, hashlib.sha256(text.encode()).hexdigest(), now())
    if damage == "chat":
        source.update(id="owner-message", role="user", principal="owner", source="web")
    elif damage == "report":
        source.update(id="team-report:" + new_id(), role="assistant", principal="agent:worker")
    elif damage == "id":
        source["id"] = "attachment:not-a-real-id"
    elif damage == "principal":
        source["principal"] = "external:forged"
    elif damage == "session":
        source["session_id"] = "borrowed-session"
    elif damage == "marker":
        source["source"] = "attachment:" + identifier + ":" + "0" * 64
    elif damage == "content":
        source["content"] += "被替换的正文"
    pipeline.ingest(source)
    if damage in {"trusted", "digest"}:
        with pipeline.memory.store.db:
            if damage == "trusted":
                pipeline.memory.store.db.execute(
                    "UPDATE memory_sources SET trusted_owner=1 WHERE id=?", (source["id"],)
                )
            else:
                pipeline.memory.store.db.execute(
                    "UPDATE memory_sources SET digest=? WHERE id=?", ("0" * 64, source["id"])
                )
    await pipeline._run_job(source["id"])
    assert failure_events(pipeline.memory.store) == []
    job = pipeline.job(source["id"])
    assert job is not None and job["state"] == "failed"


async def test_retry_revokes_old_period_preserves_prefix_and_allows_a_new_failure(
    pipeline: MemoryPipeline,
) -> None:
    async def empty(source: dict[str, Any]) -> dict[str, Any]:
        return {"memories": [], "aliases": {}}

    pipeline.extractor = empty
    pipeline.media = MockMedia(failures=2)
    source_id = collect(pipeline)
    await pipeline._run_job(source_id)
    first = failure_events(pipeline.memory.store)[0]
    previous = pipeline.job(source_id)
    assert previous is not None
    prefix = {
        key: value for key, value in previous["checkpoint"].items() if key != FAILURE_CHECKPOINT
    }
    assert "memory_ids" in prefix and "source_chunks" in prefix
    revision = first["data"]["source_failure"]["revision"]
    receipt = pipeline.retry_source(source_id, revision, max_output_chars=6000)
    queued = pipeline.job(source_id)
    assert receipt["accepted"] and queued is not None and queued["checkpoint"] == prefix
    assert source_for(pipeline, first) is None
    await pipeline._run_job(source_id)
    events = failure_events(pipeline.memory.store)
    assert len(events) == 2 and events[0]["data"]["id"] != events[1]["data"]["id"]
    assert source_for(pipeline, first) is None and source_for(pipeline, events[1]) is not None
    source = pipeline.memory.source(source_id)
    assert source is not None and "私有收藏正文" in source["content"]


@pytest.mark.parametrize("change", ["forget", "revision", "state", "proof", "content"])
async def test_old_terminal_event_is_rejected_after_source_or_job_changes(
    pipeline: MemoryPipeline, change: str
) -> None:
    source_id = collect(pipeline)
    await pipeline._run_job(source_id)
    event = failure_events(pipeline.memory.store)[0]
    if change == "forget":
        pipeline.memory.forget_source(source_id)
    else:
        with pipeline.memory.store.db:
            if change == "revision":
                pipeline.memory.store.db.execute(
                    "UPDATE memory_sources SET digest=? WHERE id=?", ("0" * 64, source_id)
                )
            elif change == "content":
                pipeline.memory.store.db.execute(
                    "UPDATE memory_sources SET content=content||'changed' WHERE id=?", (source_id,)
                )
            elif change == "state":
                pipeline.memory.store.db.execute(
                    "UPDATE memory_jobs SET state='done' WHERE source_id=?", (source_id,)
                )
            else:
                pipeline.memory.store.db.execute(
                    "UPDATE memory_jobs SET checkpoint='{}' WHERE source_id=?", (source_id,)
                )
    assert source_for(pipeline, event) is None


async def test_close_cancel_and_index_failures_do_not_create_collected_source_notices(
    pipeline: MemoryPipeline,
) -> None:
    async def cancelled(source: dict[str, Any]) -> dict[str, Any]:
        raise asyncio.CancelledError

    source_id = collect(pipeline)
    pipeline.extractor = cancelled
    with pytest.raises(asyncio.CancelledError):
        await pipeline._run_job(source_id)
    assert failure_events(pipeline.memory.store) == []
    pipeline.config = replace(pipeline.config, auto_extract=False)
    await pipeline._run_job(source_id)
    pipeline.media = MockMedia(failures=1)
    summary = pipeline.start_index([source_id], "index-only-failure")
    plan = pipeline._index_plan(summary["plan_id"])
    assert plan is not None
    await pipeline._run_index(plan)
    assert pipeline.index_status(summary["plan_id"])["state"] == "failed"
    assert failure_events(pipeline.memory.store) == []
    await pipeline.close()
    other = collect(pipeline)
    await pipeline._run_job(other)
    assert failure_events(pipeline.memory.store) == []


@pytest.mark.parametrize("fault", ["budget", "event_write", "callback"])
async def test_optional_notice_failure_keeps_the_original_failed_state(
    pipeline: MemoryPipeline, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    source_id = collect(pipeline)
    if fault == "budget":
        pipeline.max_plan_bytes = 128
    elif fault == "callback":

        def broken() -> FrozenRouting:
            raise ValueError("PRIVATE_CALLBACK")

        pipeline.failure_routing = broken
    else:
        original = pipeline.memory.store._event

        def write(
            event_type: str, data: dict[str, Any], *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            if "source_failure" in data:
                raise ValueError("PRIVATE_PERSISTENCE")
            return original(event_type, data, *args, **kwargs)

        monkeypatch.setattr(pipeline.memory.store, "_event", write)
    await pipeline._run_job(source_id)
    job = pipeline.job(source_id)
    assert job is not None and job["state"] == "failed" and job["last_error"] == "RuntimeError"
    assert FAILURE_CHECKPOINT not in job["checkpoint"]
    assert failure_events(pipeline.memory.store) == []


async def test_reopening_and_recovering_keeps_the_same_durable_failure_period(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reopen.db"
    store = Store(path)
    pipeline = pipeline_for(store)
    source_id = collect(pipeline)
    await pipeline._run_job(source_id)
    event = failure_events(store)[0]
    proof = MemorySourceFailure.load(event["data"]["source_failure"], max_bytes=4096)
    assert proof is not None
    await pipeline.close()
    store.close()
    reopened = Store(path)
    try:
        current = pipeline_for(reopened)
        current.recover()
        await current._publish(source_id)
        assert len(failure_events(reopened)) == 1
        assert failure_current(
            reopened, proof, max_source_chars=64000, max_output_chars=6000, verify_attachment=True
        )
        assert source_for(current, event) is not None
    finally:
        reopened.close()
