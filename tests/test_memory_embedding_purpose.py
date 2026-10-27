"""Document/query embedding contracts through real background memory and HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_memory_pipeline import done, message

from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.store import Store


@pytest.fixture
def model_http() -> tuple[httpx.MockTransport, list[tuple[str, dict[str, Any]]]]:
    requests: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "purpose.invalid"
        body = json.loads(request.content)
        path = request.url.path
        requests.append((path, body))
        if path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": [1.0, 0.0]}
                        for index in reversed(range(len(body["input"])))
                    ]
                },
            )
        assert path == "/v1/rerank"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0 / (index + 1)}
                    for index in range(min(body["top_n"], len(body["documents"])))
                ]
            },
        )

    return httpx.MockTransport(respond), requests


@asynccontextmanager
async def memory_pipeline(
    path: Path,
    embedding: ServiceConfig,
    http: httpx.AsyncClient,
    *,
    reranker: ServiceConfig | None = None,
) -> AsyncIterator[MemoryPipeline]:
    store = Store(path)
    try:
        async with MediaService(
            MediaConfig(embedding=embedding, reranker=reranker), client=http
        ) as media:
            pipeline = MemoryPipeline(
                Memory(store), media=media, config=MemoryPipelineConfig(auto_extract=False)
            )
            try:
                await pipeline.start()
                yield pipeline
            finally:
                await pipeline.close()
    finally:
        store.close()


async def test_background_documents_and_cached_queries_use_distinct_embedding_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_http: tuple[httpx.MockTransport, list[tuple[str, dict[str, Any]]]],
) -> None:
    transport, requests = model_http
    embedding = ServiceConfig(
        model="purpose-embedding",
        base_url="https://purpose.invalid/v1",
        dimensions=2,
        document_instruction="文档任务：\n",
        query_instruction="查询任务：\n",
        extra_body={
            "task": "document",
            "user": "local-owner",
            "options": {"side": "document", "document_only": True},
        },
        query_extra_body={"task": "query", "options": {"side": "query"}},
    )
    reranker = ServiceConfig(model="purpose-reranker", base_url="https://purpose.invalid/v1")
    content, query = "我明确喜欢蓝色", "蓝色"
    async with httpx.AsyncClient(transport=transport) as http:
        async with memory_pipeline(
            tmp_path / "memory.db", embedding, http, reranker=reranker
        ) as pipeline:
            receipt = pipeline.ingest(message("purpose-source", content), trusted_owner=True)
            assert (await done(pipeline, "purpose-source"))["state"] == "done"
            assert requests == [
                (
                    "/v1/embeddings",
                    {
                        "model": embedding.model,
                        "input": ["文档任务：\n" + content],
                        "encoding_format": "float",
                        "dimensions": 2,
                        **embedding.extra_body,
                    },
                )
            ]
            source = pipeline.memory.source("purpose-source")
            item = pipeline.memory.get(receipt["memory_id"])
            assert source is not None and source["content"] == content
            assert item is not None and item["content"] == content
            local_queries: list[str] = []
            search = pipeline.memory.search

            def observed_search(
                query: str = "",
                limit: int = 20,
                *,
                hops: int | None = None,
                layer: str | None = None,
            ) -> list[dict[str, Any]]:
                local_queries.append(query)
                return search(query, limit, hops=hops, layer=layer)

            monkeypatch.setattr(pipeline.memory, "search", observed_search)
            result = await pipeline.query(query)
            assert result["degraded"] == []
            assert result["memories"][0]["content"] == content
            assert result["memories"][0]["matched_by"] == "reranked"
            assert requests[1] == (
                "/v1/embeddings",
                {
                    "model": embedding.model,
                    "input": ["查询任务：\n" + query],
                    "encoding_format": "float",
                    "dimensions": 2,
                    "task": "query",
                    "user": "local-owner",
                    "options": {"side": "query"},
                },
            )
            assert requests[2] == (
                "/v1/rerank",
                {
                    "model": reranker.model,
                    "query": query,
                    "documents": [content],
                    "return_documents": False,
                    "top_n": 1,
                },
            )
            assert len(requests) == 3 and local_queries == [query]
            assert await pipeline.query(query) == result
            assert len(requests) == 3 and local_queries == [query]
            assert embedding.extra_body["task"] == "document"
            assert embedding.extra_body["options"]["document_only"] is True


async def test_document_prefix_separates_vectors_until_explicit_reindex_after_reopen(
    tmp_path: Path,
    model_http: tuple[httpx.MockTransport, list[tuple[str, dict[str, Any]]]],
) -> None:
    transport, requests = model_http
    path = tmp_path / "memory.db"
    content = "原始记忆中的蓝色"
    embedding = ServiceConfig(
        model="purpose-embedding",
        base_url="https://purpose.invalid/v1",
        document_instruction="旧文档：",
        query_instruction="查询：",
        extra_body={"task": "document"},
        query_extra_body={"task": "query"},
    )
    async with httpx.AsyncClient(transport=transport) as http:
        async with memory_pipeline(path, embedding, http) as pipeline:
            receipt = pipeline.ingest(message("reopened-source", content), trusted_owner=True)
            assert (await done(pipeline, "reopened-source"))["state"] == "done"
            old_key = pipeline._model_key()
            assert pipeline.memory.vector_count(old_key) == 1
        assert requests[0][1]["input"] == ["旧文档：" + content]

        updated = replace(embedding, document_instruction="新文档：")
        async with memory_pipeline(path, updated, http) as pipeline:
            new_key = pipeline._model_key()
            assert new_key != old_key
            assert pipeline.memory.vector_count(old_key) == 1
            assert pipeline.memory.vector_count(new_key) == 0
            assert len(requests) == 1
            plan = pipeline.start_index(["reopened-source"], "purpose-explicit-reindex")
            async with asyncio.timeout(3):
                async with pipeline.changed:
                    await pipeline.changed.wait_for(
                        lambda: (
                            pipeline.index_status(plan["plan_id"])["state"]
                            in {"done", "failed", "blocked", "cancelled"}
                        )
                    )
            completed = pipeline.index_status(plan["plan_id"])
            assert completed["state"] == "done" and completed["vectors_written"] == 1
            assert requests[1][1]["input"] == ["新文档：" + content]
            assert requests[1][1]["task"] == "document"
            assert pipeline.memory.vector_count(new_key) == 1
            source = pipeline.memory.source("reopened-source")
            item = pipeline.memory.get(receipt["memory_id"])
            assert source is not None and source["content"] == content
            assert item is not None and item["content"] == content
            before = len(requests)
            assert (await pipeline.query("蓝色"))["degraded"] == []
            assert len(requests) == before + 1

        query_only = replace(updated, query_extra_body={"task": "query-v2"})
        async with memory_pipeline(path, query_only, http) as pipeline:
            assert pipeline._model_key() == new_key
            assert pipeline.memory.vector_count(new_key) == 1
            before = len(requests)
            result = await pipeline.query("蓝色")
            assert result["degraded"] == []
            assert result["memories"][0]["matched_by"] == "hybrid"
            assert len(requests) == before + 1
            assert requests[-1][1]["input"] == ["查询：蓝色"]
            assert requests[-1][1]["task"] == "query-v2"


async def test_empty_document_prefix_preserves_legacy_key_and_query_parameters_do_not_change_it(
    model_http: tuple[httpx.MockTransport, list[tuple[str, dict[str, Any]]]],
) -> None:
    transport, requests = model_http
    baseline = ServiceConfig(
        model="purpose-embedding",
        base_url="https://purpose.invalid/v1/",
        dimensions=2,
        extra_body={"task": "document"},
    )
    # Freeze the pre-purpose identity bytes to detect accidental reindex requirements.
    legacy_identity = (
        b'["https://purpose.invalid/v1", "purpose-embedding", 2, "", {"task": "document"}]'
    )
    legacy_key = hashlib.sha256(legacy_identity).hexdigest()
    variants = (
        baseline,
        replace(baseline, document_instruction=""),
        replace(baseline, query_extra_body={"task": "query", "truncate": True}),
        replace(baseline, document_instruction="文档："),
        replace(baseline, query_instruction="查询："),
    )
    keys: list[str] = []
    async with httpx.AsyncClient(transport=transport) as http:
        for variant in variants:
            async with MediaService(MediaConfig(embedding=variant), client=http) as media:
                keys.append(MemoryPipeline.embedding_key(media))
    assert keys[:3] == [legacy_key] * 3
    assert keys[3] != legacy_key and keys[4] != legacy_key
    assert requests == []
