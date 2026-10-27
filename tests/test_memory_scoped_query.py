from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_document_memory import tool
from test_memory_pipeline import message
from test_runtime import FakeClient
from test_teammate_attachment_runtime import eventually, tool_result

from muli.evidence import EvidenceError, json_size
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.memory import Memory
from muli.memory_evidence import query_result
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.store import Store


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    with closing(Store(tmp_path / "scoped-query.sqlite3")) as store:
        yield Memory(store)


def fact(memory: Memory, identifier: str, content: str) -> dict[str, Any]:
    memory.register_source(message(identifier, content))
    return memory.apply_extraction(
        identifier, [{"content": content, "quote": content, "layer": "semantic"}]
    )[0]


def semantic_http(
    requests: list[tuple[str, dict[str, Any]]],
    *,
    pause: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> httpx.MockTransport:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "scoped.invalid"
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/embeddings":
            if pause is not None and release is not None:
                pause.set()
                await release.wait()
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
        assert request.url.path == "/v1/rerank"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0 / (index + 1)}
                    for index in range(min(body["top_n"], len(body["documents"])))
                ]
            },
        )

    return httpx.MockTransport(respond)


async def test_scoped_semantics_only_reranks_chosen_documents_and_caches_scope(
    memory: Memory,
) -> None:
    a = fact(memory, "source-a", "只有甲资料中的星港验收。")
    b = fact(memory, "source-b", "只有乙资料中的镜片校准。")
    service = ServiceConfig(model="scoped", base_url="https://scoped.invalid/v1")
    requests: list[tuple[str, dict[str, Any]]] = []
    async with httpx.AsyncClient(transport=semantic_http(requests)) as http:
        async with MediaService(
            MediaConfig(embedding=service, reranker=service), client=http
        ) as media:
            pipeline = MemoryPipeline(memory, media=media)
            key = pipeline.embedding_key(media)
            for item in (a, b):
                memory.store_vector(item["id"], [1.0, 0.0], model_key=key)
            try:
                query = "nightjar"
                scoped = await pipeline.query(query, limit=4, source_ids=["source-a"])
                assert scoped["source_ids"] == ["source-a"]
                assert [item["id"] for item in scoped["memories"]] == [a["id"]]
                assert requests[-1][1]["documents"] == [a["content"]]
                assert b["content"] not in json.dumps(scoped, ensure_ascii=False)
                assert pipeline.cached_context(query) == "", (
                    "scoped semantic cache must not seed automatic context"
                )
                calls = len(requests)
                assert (
                    await pipeline.query(query, limit=4, source_ids=["source-a", "source-a"])
                    == scoped
                )
                assert len(requests) == calls

                combined = await pipeline.query(query, limit=4, source_ids=["source-b", "source-a"])
                calls = len(requests)
                assert combined["source_ids"] == ["source-a", "source-b"]
                assert {item["id"] for item in combined["memories"]} == {a["id"], b["id"]}
                assert (
                    await pipeline.query(query, limit=4, source_ids=["source-a", "source-b"])
                    == combined
                )
                assert len(requests) == calls
                assert pipeline.cached_context(query) == ""

                unscoped = await pipeline.query(query, limit=4)
                assert "source_ids" not in unscoped
                baseline = pipeline.cached_context(query)
                assert a["content"] in baseline and b["content"] in baseline
                await pipeline.query(query, limit=2, source_ids=["source-b"])
                assert pipeline.cached_context(query) == baseline, (
                    "latest other-limit scoped cache must not replace global recall"
                )
            finally:
                await pipeline.close()


@pytest.mark.parametrize("capacity", [1, 2])
async def test_scoped_cache_cannot_evict_global_recall_at_capacity(
    memory: Memory, capacity: int
) -> None:
    selected = fact(memory, "source-a", "甲资料独有的星港校验。")
    other = fact(memory, "source-b", "乙资料独有的镜片维护。")
    service = ServiceConfig(model="scoped", base_url="https://scoped.invalid/v1")
    requests: list[tuple[str, dict[str, Any]]] = []
    async with httpx.AsyncClient(transport=semantic_http(requests)) as http:
        async with MediaService(
            MediaConfig(embedding=service, reranker=service), client=http
        ) as media:
            pipeline = MemoryPipeline(
                memory, media=media, config=MemoryPipelineConfig(cache_entries=capacity)
            )
            for item in (selected, other):
                memory.store_vector(item["id"], [1.0, 0.0], model_key=pipeline.embedding_key(media))
            try:
                query = "nightjar"
                await pipeline.query(query, limit=4)
                baseline = pipeline.cached_context(query)
                assert selected["content"] in baseline and other["content"] in baseline
                for identifier in ("source-a", "source-b", "source-a"):
                    found = await pipeline.query(query, limit=4, source_ids=[identifier])
                    assert {item["source_id"] for item in found["memories"]} == {identifier}
                    assert pipeline.cached_context(query) == baseline
                    assert len(pipeline._cache) <= capacity
                    assert (query, 4, pipeline.config.graph_hops, None) in pipeline._cache
                calls = len(requests)
                await pipeline.query(query, limit=4)
                assert len(requests) == calls, (
                    "global query remains cached after scoped cache churn"
                )
                if capacity == 2:
                    # A later global write must replace a scoped entry before
                    # it considers evicting an older global query.
                    await pipeline.query(query, limit=4, source_ids=["source-b"])
                    await pipeline.query("windlass", limit=4)
                    assert pipeline.cached_context(query) == baseline
                    assert selected["content"] in pipeline.cached_context("windlass")
                    assert len(pipeline._cache) == 2
                    assert all(key[3] is None for key in pipeline._cache)
                    calls = len(requests)
                    await pipeline.query(query, limit=4)
                    await pipeline.query("windlass", limit=4)
                    assert len(requests) == calls
            finally:
                await pipeline.close()


async def test_forgetting_during_embedding_refreshes_only_original_scope(memory: Memory) -> None:
    a = fact(memory, "source-a", "共同核查主题：甲资料正文。")
    b = fact(memory, "source-b", "共同核查主题：乙资料正文。")
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[tuple[str, dict[str, Any]]] = []
    service = ServiceConfig(model="scoped", base_url="https://scoped.invalid/v1")
    async with httpx.AsyncClient(
        transport=semantic_http(requests, pause=entered, release=release)
    ) as http:
        async with MediaService(
            MediaConfig(embedding=service, reranker=service), client=http
        ) as media:
            pipeline = MemoryPipeline(memory, media=media)
            memory.store_vector(a["id"], [1.0, 0.0], model_key=pipeline.embedding_key(media))
            pending = asyncio.create_task(pipeline.query("共同核查", source_ids=["source-a"]))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert memory.forget_source("source-a")
                release.set()
                result = await asyncio.wait_for(pending, 2)
                assert result["source_ids"] == ["source-a"] and result["memories"] == []
                assert "memory_changed" in result["degraded"]
                assert b["content"] not in json.dumps(result, ensure_ascii=False)
                assert len(requests) == 1 and not pipeline._cache
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await pipeline.close()


@pytest.mark.parametrize("source_ids", [[], "source-a", [""], [False], ["x" * 12001]])
async def test_invalid_scope_never_calls_semantic_service(memory: Memory, source_ids: Any) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []
    service = ServiceConfig(model="scoped", base_url="https://scoped.invalid/v1")
    async with httpx.AsyncClient(transport=semantic_http(requests)) as http:
        async with MediaService(MediaConfig(embedding=service), client=http) as media:
            pipeline = MemoryPipeline(memory, media=media)
            try:
                with pytest.raises(ValueError):
                    await pipeline.query("backup", source_ids=source_ids)
                assert requests == [] and not pipeline._cache
            finally:
                await pipeline.close()


def test_scope_metadata_is_complete_or_the_whole_output_is_rejected() -> None:
    result = {
        "memories": [],
        "sources": [],
        "communities": [],
        "degraded": [],
        "revision": 1,
        "source_ids": ["source-" + "中" * 200],
    }
    with pytest.raises(EvidenceError, match="输出预算"):
        query_result(result, max_output_chars=200)
    page = query_result(result, max_output_chars=1000)
    assert page["source_ids"] == result["source_ids"] and json_size(page) <= 1000
    page["source_ids"].clear()
    assert result["source_ids"]


async def test_real_attachment_tool_and_http_queries_keep_selected_source_after_reopen(
    tmp_path: Path,
) -> None:
    selected_id = ""
    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update({"memory": {"auto_extract": False}})
        await rig.memory.reconfigure()
        sources: list[str] = []
        for text in ("备份步骤：先校验星港原始清单。", "备份步骤：乙资料独有的其它流程。"):
            attachment = await rig.upload(text.encode("utf-8"))
            imported = await tool(rig, "memory_import_attachment", id=attachment["id"])
            sources.append(imported["source_id"])
            await asyncio.wait_for(rig.memory.pipeline.wait(imported["source_id"]), 3)
        selected_id = sources[0]
        found = await tool(rig, "memory_query", query="备份步骤", source_ids=[selected_id])
        assert found["memories"] and all(
            item["source_id"] == selected_id for item in found["memories"]
        )
        assert found["source_ids"] == [selected_id]
        assert "乙资料独有" not in json.dumps(found, ensure_ascii=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=rig.app), base_url="http://localhost:8765"
        ) as client:
            combined = await client.get(
                "/api/memory/query",
                params=[
                    ("query", "备份步骤"),
                    *(("source_id", value) for value in reversed(sources)),
                ],
            )
            assert combined.status_code == 200 and combined.json()["source_ids"] == sorted(sources)
            assert {item["source_id"] for item in combined.json()["memories"]} == set(sources)
            invalid = await client.get(
                "/api/memory/query", params={"query": "备份步骤", "source_id": ""}
            )
            assert invalid.status_code == 400
            missing = await client.get(
                "/api/memory/query", params={"query": "备份步骤", "source_id": "unknown-source"}
            )
            assert missing.status_code == 200 and missing.json()["memories"] == []
        store_path = rig.runtime.data_dir / "muli.sqlite3"
    with closing(Store(store_path)) as store:
        pipeline = MemoryPipeline(Memory(store), config=MemoryPipelineConfig(auto_extract=False))
        try:
            found = await pipeline.query("备份步骤", source_ids=[selected_id])
            assert found["memories"] and {item["source_id"] for item in found["memories"]} == {
                selected_id
            }
            assert "乙资料独有" not in json.dumps(found, ensure_ascii=False)
        finally:
            await pipeline.close()


async def test_main_and_real_teammate_share_the_same_scoped_query(tmp_path: Path) -> None:
    replies: list[dict[str, Any]] = []
    arguments = {"query": "备份步骤", "source_ids": ["scoped-team-source"]}

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schema = next(
                item["function"] for item in tools if item["function"]["name"] == "memory_query"
            )
            assert "source_ids" in schema["parameters"]["properties"]
            if not any(item.tool_call_id == "scoped-read" for item in messages):
                turn = ModelTurn(
                    tool_calls=[ToolCall("scoped-read", "memory_query", json.dumps(arguments))]
                )
            else:
                replies.append(tool_result(messages, "scoped-read"))
                turn = ModelTurn(content="已按指定来源查询。")
            yield StreamEvent("done", turn=turn)

    async with actual_app(tmp_path, worker=Worker([], [])) as rig:
        rig.runtime.config.update({"memory": {"auto_extract": False}})
        await rig.memory.reconfigure()
        original_factory = rig.runtime.client_factory
        selected = fact(rig.runtime.memory, "scoped-team-source", "备份步骤：主人指定来源的步骤。")
        fact(rig.runtime.memory, "other-team-source", "备份步骤：其他来源的独特正文。")
        main_reply = await tool(rig, "memory_query", **arguments)
        rig.runtime.client_factory = original_factory
        agent = await rig.runtime.team.spawn(
            "来源核查队友", "只查询 scoped-team-source 的备份步骤。"
        )
        await eventually(
            rig.runtime,
            lambda: (
                rig.runtime.team.get(agent["id"])["status"]
                in {"completed", "failed", "cancelled", "interrupted"}
            ),
        )
        assert rig.runtime.team.get(agent["id"])["status"] == "completed"
        assert len(replies) == 1
        for reply in (main_reply, replies[0]):
            assert reply["source_ids"] == arguments["source_ids"]
            assert [item["id"] for item in reply["memories"]] == [selected["id"]]
            assert "其他来源的独特正文" not in json.dumps(reply, ensure_ascii=False)
