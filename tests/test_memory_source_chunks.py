"""Full long-source recall through ordinary owner tools and the real pipeline."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_document_memory import tool
from test_memory_pipeline import MockMedia
from test_runtime import FakeClient
from test_teammate_attachment_runtime import eventually

from muli.evidence import EvidenceError
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.store import Store, new_id


async def test_long_attachment_tail_is_recalled_when_model_extraction_is_disabled(
    tmp_path: Path,
) -> None:
    text = (
        "资料开头。\n"
        + "无关背景。" * 2600
        + "\n星港航道的唯一尾部验收条件是水银镜片必须重新校准。"
    )
    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update({"memory": {"auto_extract": False}})
        await rig.memory.reconfigure()
        item = await rig.upload(text.encode("utf-8"))
        imported = await tool(rig, "memory_import_attachment", id=item["id"])
        source_id = "attachment:" + item["id"]
        assert imported["source_id"] == source_id
        finished = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), timeout=3)
        assert finished is not None and finished["state"] == "done"
        found = await tool(rig, "memory_query", query="水银镜片", limit=4)
        assert any(
            entry["source_id"] == source_id and "水银镜片必须重新校准" in entry["content"]
            for entry in found["memories"]
        ), "full source is retained but its unextracted tail is missing from public memory recall"


async def test_full_source_semantic_tail_survives_explicit_model_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = "水银镜片必须重新校准，否则星港航道不能通过验收。"
    text = "资料开头。\n" + "无关背景。" * 2600 + "\n" + evidence
    calls: list[dict[str, Any]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url == "https://chunks.invalid/v1/embeddings"
        payload = json.loads(request.content)
        assert payload["model"] in {"chunks-a", "chunks-b"}
        assert all(len(value) <= 2000 for value in payload["input"])
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": index,
                        "embedding": [1.0, 0.0]
                        if value == "nightjar" or evidence in value
                        else [0.0, 1.0],
                    }
                    for index, value in enumerate(payload["input"])
                ]
            },
        )

    transport = httpx.MockTransport(respond)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update(
            {
                "memory": {"auto_extract": False},
                "media": {
                    "embedding": {"model": "chunks-a", "base_url": "https://chunks.invalid/v1"}
                },
            }
        )
        await rig.memory.reconfigure()
        item = await rig.upload(text.encode("utf-8"))
        imported = await tool(rig, "memory_import_attachment", id=item["id"])
        source_id = imported["source_id"]
        done = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), timeout=4)
        assert done is not None and done["state"] == "done"
        chunk_ids = done["checkpoint"]["source_chunks"]["memory_ids"]
        assert len(chunk_ids) > 4
        assert any(evidence in value for call in calls for value in call["input"])

        async def query() -> dict[str, Any]:
            call_id = "settled-chunk-query-" + new_id()
            turns = [
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            call_id, "memory_query", json.dumps({"query": "nightjar", "limit": 2})
                        )
                    ]
                ),
                ModelTurn(content="已核对长期资料。"),
            ]

            class Settled(FakeClient):
                async def stream(
                    self, messages: list[Message], tools: list[dict[str, Any]]
                ) -> AsyncIterator[StreamEvent]:
                    if turns and turns[0].tool_calls:
                        pending = rig.runtime.store.db.execute(
                            "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry')"
                        ).fetchall()
                        for row in pending:
                            finished = await asyncio.wait_for(
                                rig.memory.pipeline.wait(row[0]), timeout=4
                            )
                            assert finished is not None and finished["state"] == "done"
                    async for event in super().stream(messages, tools):
                        yield event

            def factory(profile: ModelConfig) -> FakeClient:
                return Settled(turns, [])

            rig.runtime.client_factory = factory  # type: ignore[assignment]
            receipt = await rig.runtime.submit("请核对之前保存的资料。", mode="yolo")
            await rig.settle(receipt)
            return rig.result(call_id)

        found = await query()
        assert "memory_changed" not in found["degraded"]
        matching = [
            entry
            for entry in found["memories"]
            if entry["source_id"] == source_id and evidence in entry["content"]
        ]
        assert matching and matching[0]["matched_by"] == "semantic"
        assert matching[0]["trusted_owner"] is False and evidence in matching[0]["quote"]
        rig.runtime.config.update({"media": {"embedding": {"model": "chunks-b"}}})
        await rig.memory.reconfigure()
        missing = await query()
        assert not any(entry["source_id"] == source_id for entry in missing["memories"])
        started = await tool(
            rig,
            "memory_reindex",
            action="start",
            source_ids=[source_id],
            key="full-source-chunks-b",
        )
        assert started["state"] == "queued"
        plan_id = started["plan_id"]
        await eventually(
            rig.runtime, lambda: rig.memory.pipeline.index_status(plan_id)["state"] == "done"
        )
        status = rig.memory.pipeline.index_status(plan_id)
        assert status["omitted"] == 0 and status["sources_finished"] == 1
        assert rig.memory.pipeline.job(source_id) == done
        assert len(
            {
                row[0]
                for row in rig.runtime.store.db.execute(
                    "SELECT memory_id FROM memory_meta WHERE source_id=? AND dedup_key LIKE 'source-chunk:%'",
                    (source_id,),
                ).fetchall()
            }
        ) == len(chunk_ids)
        again = await query()
        assert any(
            entry["source_id"] == source_id and evidence in entry["content"]
            for entry in again["memories"]
        )


async def test_main_can_hot_configure_chunk_policy_without_changing_config_format(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        before = rig.runtime.config.path.read_bytes()
        current = await tool(rig, "memory_chunk_settings_read")
        changed = {**current["settings"], "chunk_chars": 3000, "batch_size": 2}
        result = await tool(rig, "memory_chunk_configure", **changed)
        assert result == {"settings": changed}
        assert rig.runtime.config.path.read_bytes() == before
        assert (await tool(rig, "memory_chunk_settings_read")) == result


async def test_explicit_reindex_backfills_a_valid_legacy_completed_source(tmp_path: Path) -> None:
    store = Store(tmp_path / "legacy.db")
    memory = Memory(store)
    media = MockMedia()
    pipeline = MemoryPipeline(memory, media=media, config=MemoryPipelineConfig(auto_extract=False))
    text = "普通背景。" * 2600 + "LEGACY-TAIL-CLAUSE:长资料的末尾必须被索引。"
    receipt = pipeline.ingest(
        {"id": "legacy-source", "content": text, "role": "tool", "principal": "external:attachment"}
    )
    # Valid pre-chunk completed job fixture: a full retained source, one initial
    # episode, and the original extraction/embedding checkpoint contract.
    checkpoint = {
        "extraction": {"memories": [], "aliases": {}, "rejected_untrusted_owner_claims": 0},
        "memory_ids": [receipt["memory_id"]],
        "embedded": True,
    }
    with store.db:
        store.db.execute(
            "UPDATE memory_jobs SET state='done',checkpoint=? WHERE source_id=?",
            (json.dumps(checkpoint), "legacy-source"),
        )
    original = pipeline.job("legacy-source")
    try:
        await pipeline.start()
        await asyncio.sleep(0)
        assert media.embed_calls == []
        assert memory.search("LEGACY-TAIL-CLAUSE") == []
        plan = pipeline.start_index(["legacy-source"], "explicit-legacy-backfill")
        async with asyncio.timeout(3):
            async with pipeline.changed:
                await pipeline.changed.wait_for(
                    lambda: pipeline.index_status(plan["plan_id"])["state"] == "done"
                )
        status = pipeline.index_status(plan["plan_id"])
        assert status["sources_finished"] == 1 and status["omitted"] == 0
        assert pipeline.job("legacy-source") == original
        assert any(
            "LEGACY-TAIL-CLAUSE" in value for values in media.embed_calls for value in values
        )
        assert all(len(values) <= 4 for values in media.embed_calls)
        assert all(len(value) <= 2000 for values in media.embed_calls for value in values)
        assert any(
            "LEGACY-TAIL-CLAUSE" in item["content"] for item in memory.search("LEGACY-TAIL-CLAUSE")
        )
    finally:
        await pipeline.close()
        store.close()


async def test_old_source_backfill_reserves_future_progress_before_admission(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "budget.db")
    memory = Memory(store)
    pipeline = MemoryPipeline(memory, media=MockMedia(), max_plan_bytes=64000)
    receipt = pipeline.ingest({"id": "budget-source", "content": "有界资料。" * 2600})
    checkpoint = {
        "extraction": {"memories": [], "aliases": {}},
        "memory_ids": [receipt["memory_id"]],
        "embedded": True,
    }
    with store.db:
        store.db.execute(
            "UPDATE memory_jobs SET state='done',checkpoint=? WHERE source_id=?",
            (json.dumps(checkpoint), "budget-source"),
        )
    try:
        first = pipeline.start_index(["budget-source"], "reserved-backfill-a")
        pipeline.cancel_index(first["plan_id"])
        saved = store.get("memory_index_plan", first["plan_id"])
        assert saved is not None
        # This budget fits the actual initial/cancelled record. It does not
        # fit the fixed future chunk IDs, versions and progress counters.
        pipeline.max_plan_bytes = len(json.dumps(saved, ensure_ascii=False).encode("utf-8")) + 10
        assert (
            store.bounded_object(
                "memory_index_plan", first["plan_id"], max_bytes=pipeline.max_plan_bytes
            )
            == saved
        )
        before = store.db.execute("SELECT count(*) FROM memories").fetchone()[0]
        with pytest.raises(EvidenceError) as caught:
            pipeline.start_index(["budget-source"], "reserved-backfill-b")
        assert caught.value.code == "output_budget"
        assert store.db.execute("SELECT count(*) FROM memories").fetchone()[0] == before
        assert store.get("memory_index_state", "current")["plan_id"] == first["plan_id"]
    finally:
        await pipeline.close()
        store.close()


async def test_user_modified_archive_keeps_its_independent_embedding(tmp_path: Path) -> None:
    store = Store(tmp_path / "modified.db")
    memory, media = Memory(store), MockMedia()
    pipeline = MemoryPipeline(memory, media=media, config=MemoryPipelineConfig(auto_extract=False))
    receipt = pipeline.ingest({"id": "modified-source", "content": "原始背景。" * 2600})
    corrected = "用户独立更正过的归档记录仍需进入检索。"
    changed = memory.edit(receipt["memory_id"], content=corrected)
    assert changed["version"] > 1 and memory.automatic_episode_id("modified-source") is None
    try:
        await pipeline.start()
        done = await asyncio.wait_for(pipeline.wait("modified-source"), timeout=3)
        assert done is not None and done["state"] == "done"
        assert sum(value == corrected for values in media.embed_calls for value in values) == 1
        assert all(len(value) <= 2000 for values in media.embed_calls for value in values)
        assert (
            store.db.execute(
                "SELECT 1 FROM memory_vectors WHERE memory_id=?", (receipt["memory_id"],)
            ).fetchone()
            is not None
        )
        assert memory.get(receipt["memory_id"])["content"] == corrected
    finally:
        await pipeline.close()
        store.close()
