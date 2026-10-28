"""Explicit source recovery uses existing jobs without widening the requested scope."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_document_memory import tool
from test_runtime import FakeClient
from test_teammate_attachment_runtime import eventually, tool_result

from muli.evidence import EvidenceError
from muli.maintenance import MaintenanceError
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.store import Store


@pytest.fixture
def pipeline(tmp_path: Path) -> Iterator[MemoryPipeline]:
    store = Store(tmp_path / "memory.db")
    try:
        yield MemoryPipeline(Memory(store), config=MemoryPipelineConfig(auto_extract=False))
    finally:
        store.close()


def saved(pipeline: MemoryPipeline, identifier: str, state: str = "failed") -> dict[str, Any]:
    receipt = pipeline.ingest(
        {
            "id": identifier,
            "content": "可核查的完整资料🙂。",
            "role": "tool",
            "principal": "external:recovery",
        }
    )
    with pipeline.memory.store.db:
        if state == "deferred":
            pipeline.memory.store.db.execute(
                "DELETE FROM memory_jobs WHERE source_id=?", (identifier,)
            )
        else:
            checkpoint = {
                "extraction": {"memories": [], "aliases": {}},
                "memory_ids": [receipt["memory_id"]],
            }
            pipeline.memory.store.db.execute(
                "UPDATE memory_jobs SET state=?,attempts=3,last_error='RuntimeError',checkpoint=? "
                "WHERE source_id=?",
                (state, json.dumps(checkpoint), identifier),
            )
    source = pipeline.memory.source(identifier)
    assert source is not None
    return source


def test_one_source_admission_preserves_checkpoint_and_other_sources(
    pipeline: MemoryPipeline,
) -> None:
    selected = saved(pipeline, "selected")
    saved(pipeline, "other-failed")
    saved(pipeline, "other-deferred", "deferred")
    before = pipeline.job("selected")
    other = pipeline.job("other-failed")
    assert before is not None
    pipeline._wake.clear()
    receipt = pipeline.retry_source("selected", selected["digest"], max_output_chars=6000)
    assert receipt == {
        "source_id": "selected",
        "revision": selected["digest"],
        "previous_state": "failed",
        "state": "queued",
        "accepted": True,
        "reason": "queued",
    }
    queued = pipeline.job("selected")
    assert queued is not None and queued["checkpoint"] == before["checkpoint"]
    assert queued["attempts"] == 0 and queued["last_error"] == ""
    assert pipeline.job("other-failed") == other
    assert pipeline.job("other-deferred") is None and pipeline._wake.is_set()
    pipeline._wake.clear()
    changes = pipeline.memory.store.db.total_changes
    repeated = pipeline.retry_source("selected", selected["digest"], max_output_chars=6000)
    assert not repeated["accepted"] and repeated["reason"] == "already_pending"
    assert pipeline.job("selected") == queued and not pipeline._wake.is_set()
    assert pipeline.memory.store.db.total_changes == changes


@pytest.mark.parametrize("state", ["queued", "processing", "retry", "done"])
def test_active_or_completed_work_is_not_reset(pipeline: MemoryPipeline, state: str) -> None:
    source = saved(pipeline, "selected", state)
    before = pipeline.job("selected")
    changes = pipeline.memory.store.db.total_changes
    result = pipeline.retry_source("selected", source["digest"], max_output_chars=6000)
    assert result["accepted"] is False and result["state"] == state
    assert result["reason"] == ("already_done" if state == "done" else "already_pending")
    assert pipeline.job("selected") == before
    assert pipeline.memory.store.db.total_changes == changes


@pytest.mark.parametrize("state", ["failed", "deferred"])
def test_capacity_limits_do_not_admit_any_source(pipeline: MemoryPipeline, state: str) -> None:
    source = saved(pipeline, "selected", state)
    saved(pipeline, "occupied", "queued")
    pipeline.config = replace(pipeline.config, max_pending_jobs=1)
    before = pipeline.job("selected")
    changes = pipeline.memory.store.db.total_changes
    result = pipeline.retry_source("selected", source["digest"], max_output_chars=6000)
    assert not result["accepted"] and result["reason"] == "capacity_full"
    assert result["state"] == state and pipeline.job("selected") == before
    assert pipeline.memory.store.db.total_changes == changes


def test_deferred_admission_creates_only_the_selected_job(pipeline: MemoryPipeline) -> None:
    source = saved(pipeline, "selected", "deferred")
    saved(pipeline, "not-selected", "deferred")
    result = pipeline.retry_source("selected", source["digest"], max_output_chars=6000)
    assert result["accepted"] and result["previous_state"] == "deferred"
    assert pipeline.job("selected")["state"] == "queued"  # type: ignore[index]
    assert pipeline.job("not-selected") is None


def test_bad_version_forgotten_source_and_budget_fail_before_writes(
    pipeline: MemoryPipeline,
) -> None:
    identifier = "\0" * 190
    source = saved(pipeline, identifier)
    before = pipeline.job(identifier)
    changes = pipeline.memory.store.db.total_changes
    for revision, budget, code in [
        ("0" * 64, 6000, "evidence_changed"),
        (source["digest"], 1000, "output_budget"),
    ]:
        with pytest.raises(EvidenceError) as caught:
            pipeline.retry_source(identifier, revision, max_output_chars=budget)
        assert caught.value.code == code
        assert pipeline.job(identifier) == before
        assert pipeline.memory.store.db.total_changes == changes
    pipeline.memory.forget_source(identifier)
    changes = pipeline.memory.store.db.total_changes
    with pytest.raises(EvidenceError) as caught:
        pipeline.retry_source(identifier, source["digest"], max_output_chars=6000)
    assert caught.value.code == "evidence_not_found"
    assert pipeline.memory.store.db.total_changes == changes


async def test_disabled_closed_maintenance_and_unfinished_transaction_refuse_admission(
    pipeline: MemoryPipeline,
) -> None:
    source = saved(pipeline, "selected")
    before = pipeline.job("selected")
    pipeline.config = replace(pipeline.config, enabled=False)
    with pytest.raises(ValueError):
        pipeline.retry_source("selected", source["digest"], max_output_chars=6000)
    pipeline.config = replace(pipeline.config, enabled=True)
    pipeline.admission.pause()
    with pytest.raises(MaintenanceError):
        pipeline.retry_source("selected", source["digest"], max_output_chars=6000)
    pipeline.admission.release()
    pipeline.memory.store.db.execute("INSERT INTO memory_state VALUES('uncommitted-marker',1)")
    assert pipeline.memory.store.db.in_transaction
    with pytest.raises(EvidenceError):
        pipeline.retry_source("selected", source["digest"], max_output_chars=6000)
    assert pipeline.memory.store.db.in_transaction
    pipeline.memory.store.db.rollback()
    assert pipeline.job("selected") == before
    await pipeline.close()
    with pytest.raises(ValueError):
        pipeline.retry_source("selected", source["digest"], max_output_chars=6000)
    assert pipeline.job("selected") == before


async def test_real_owner_tool_resumes_failed_work_without_replaying_completed_source(
    tmp_path: Path,
) -> None:
    failing = True
    attempts: list[str] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"] == "repair-selected":
            attempts.append(source["id"])
            if failing:
                raise RuntimeError("SECRET-MUST-NOT-LEAK")
        return {"memories": [], "aliases": {}}

    async with actual_app(tmp_path, extractor=extract) as rig:
        pipeline = rig.memory.pipeline
        pipeline.config = replace(pipeline.config, max_attempts=1)
        pipeline.ingest({"id": "repair-selected", "content": "需要恢复的准确资料", "role": "tool"})
        failed = await asyncio.wait_for(pipeline.wait("repair-selected"), 3)
        assert failed is not None and failed["state"] == "failed"
        page = await tool(rig, "memory_sources", state="failed")
        selected = next(item for item in page["items"] if item["source_id"] == "repair-selected")
        assert selected["job"]["last_error"] == "RuntimeError"
        failing = False
        result = await tool(
            rig,
            "memory_retry_source",
            source_id=selected["source_id"],
            revision=selected["revision"],
        )
        assert result["accepted"] and result["state"] == "queued"
        completed = await asyncio.wait_for(pipeline.wait("repair-selected"), 3)
        assert completed is not None and completed["state"] == "done"
        repeated = await tool(
            rig,
            "memory_retry_source",
            source_id=selected["source_id"],
            revision=selected["revision"],
        )
        assert repeated["accepted"] is False and repeated["reason"] == "already_done"
        assert attempts == ["repair-selected", "repair-selected"]
        assert "SECRET" not in json.dumps([page, result, repeated])


async def test_actual_teammate_cannot_retry_owner_sources(tmp_path: Path) -> None:
    call_id = "worker-forbidden-source-retry"

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert "memory_retry_source" not in {item["function"]["name"] for item in tools}
            if not any(item.tool_call_id == call_id for item in messages):
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall(
                            call_id,
                            "memory_retry_source",
                            json.dumps({"source_id": "selected", "revision": source["digest"]}),
                        )
                    ]
                )
            else:
                assert tool_result(messages, call_id) == {"error": "工具未启用"}
                turn = ModelTurn(content="未向队友开放来源重试，未执行。")
            yield StreamEvent("done", turn=turn)

    async with actual_app(tmp_path, worker=Worker([], [])) as rig:
        source = saved(rig.memory.pipeline, "selected")
        before = rig.memory.pipeline.job("selected")
        handler = next(
            item.handler
            for item in rig.memory.index_tools(rig.runtime)
            if item.name == "memory_retry_source"
        )
        with pytest.raises(PermissionError):
            await handler({"source_id": "selected", "revision": source["digest"]})
        agent = await rig.runtime.team.spawn("来源重试边界", "尝试重试来源，准确报告工具结果。")
        await eventually(
            rig.runtime,
            lambda: rig.runtime.team.get(agent["id"])["status"] in {"completed", "failed"},
        )
        assert rig.runtime.team.get(agent["id"])["status"] == "completed"
        assert rig.memory.pipeline.job("selected") == before


async def test_source_http_catalog_read_and_retry_share_current_proof(tmp_path: Path) -> None:
    async with actual_app(tmp_path) as rig:
        source = saved(rig.memory.pipeline, "api-selected")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(rig.app), base_url="http://localhost:8765"
        ) as client:
            page = (await client.get("/api/memory/sources", params={"state": "failed"})).json()
            selected = next(item for item in page["items"] if item["source_id"] == source["id"])
            text = ""
            offset = 0
            for _ in range(len(source["content"])):
                response = await client.get(
                    "/api/memory/source",
                    params={
                        "id": source["id"],
                        "revision": selected["revision"],
                        "length_bytes": 8,
                        "offset_bytes": offset,
                    },
                )
                assert response.status_code == 200
                window = response.json()
                assert window["source_id"] == source["id"] and "memory_id" not in window
                text += window["content"]
                if not window["has_more"]:
                    break
                assert window["next_offset_bytes"] > offset
                offset = window["next_offset_bytes"]
            assert text == source["content"]
            body = {"source_id": source["id"], "revision": source["digest"]}
            before = rig.memory.pipeline.job(source["id"])
            stale = await client.post(
                "/api/memory/sources/retry", json={**body, "revision": "0" * 64}
            )
            assert stale.status_code == 409 and rig.memory.pipeline.job(source["id"]) == before
            extra = await client.post("/api/memory/sources/retry", json={**body, "all": True})
            assert extra.status_code == 422
            assert (
                await client.get("/api/memory/sources", params={"limit": 101})
            ).status_code == 422
            accepted = await client.post("/api/memory/sources/retry", json=body)
            assert accepted.status_code == 200 and accepted.json()["accepted"] is True
            job = await asyncio.wait_for(rig.memory.pipeline.wait(source["id"]), 3)
            assert job is not None and job["state"] == "done"
            repeated = await client.post("/api/memory/sources/retry", json=body)
            assert repeated.json()["reason"] == "already_done"
            rig.runtime.config.update({"disabled_tools": ["memory_retry_source"]})
            assert (await client.post("/api/memory/sources/retry", json=body)).status_code == 400
