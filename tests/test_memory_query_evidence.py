from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_memory_alias_evidence import sourced
from test_memory_pipeline import PausedQueryMedia
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.config import ConfigFile
from muli.events import EventBus
from muli.evidence import EvidenceError, json_size
from muli.local_services import LocalServices
from muli.memory import Memory
from muli.memory_application import MemoryApplication
from muli.memory_evidence import memory_listing, query_result
from muli.memory_pipeline import MemoryPipeline
from muli.providers import ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store


def projection() -> dict[str, Any]:
    path = {
        "revision": 7,
        "seed_id": "entity-1",
        "seed": {
            "entity_id": "entity-1",
            "name": "沐璃",
            "alias": "lumi",
            "kind": "source",
            "memory_id": "alias-record",
            "source_id": "source-1",
        },
        "steps": [
            {
                "memory_id": "edge-1",
                "source_id": "source-2",
                "subject_id": "entity-1",
                "object_id": "entity-2",
                "subject": "沐璃",
                "predicate": "维护",
                "object": "项目甲",
                "from_id": "entity-1",
                "to_id": "entity-2",
                "direction": "forward",
            }
        ],
        "complete": True,
    }
    return {
        "memories": [
            {
                "id": "result",
                "source_id": "source-2",
                "content": '中文🙂\0"\\',
                "trusted_owner": False,
                "graph_via": path,
            }
        ],
        "sources": [
            {
                "id": "source-2",
                "content": "资料",
                "principal": "system:attention",
                "trusted_owner": False,
            }
        ],
        "communities": [{"id": "community", "evidence": ["edge-1"], "summary": "摘要"}],
        "degraded": ["semantic_unavailable:TimeoutError"],
        "revision": 7,
    }


@pytest.mark.parametrize("budget", [1, 200, 300, 400, 800, 1000, 1500, 10000])
def test_full_query_budget_keeps_whole_paths_and_identities(budget: int) -> None:
    original = projection()
    before = deepcopy(original)
    try:
        result = query_result(original, max_output_chars=budget)
    except EvidenceError as exc:
        assert exc.code == "output_budget" and budget < 300
    else:
        assert json_size(result) <= budget
        assert json.loads(json.dumps(result, ensure_ascii=False)) == result
        assert result["revision"] == 7
        for section in ("memories", "sources", "communities"):
            assert result["omitted"][section] == len(original[section]) - len(result[section])
            assert all(item in original[section] for item in result[section])
        if result["memories"]:
            assert result["memories"][0]["graph_via"] == original["memories"][0]["graph_via"]
            assert result["memories"][0]["trusted_owner"] is False
        assert result["truncated"] == any(result["omitted"].values())
    assert original == before


def test_exact_envelope_budget_and_oversized_first_candidate() -> None:
    original = projection()
    full = query_result(original, max_output_chars=10000)
    exact = json_size(full)
    assert query_result(original, max_output_chars=exact) == full
    smaller = query_result(original, max_output_chars=exact - 1)
    assert smaller["truncated"] and json_size(smaller) <= exact - 1
    assert smaller["memories"] == full["memories"]
    large = {"id": "oversized", "content": "\0" * 12000, "principal": "do-not-slice"}
    original["memories"].insert(0, large)
    result = query_result(original, max_output_chars=exact)
    assert result["memories"] == full["memories"]
    assert result["omitted"]["memories"] == 1
    assert result["truncated"]


def test_management_only_budgets_added_paths_preserving_full_records() -> None:
    item = projection()["memories"][0]
    path_size = json_size({"graph_via": item["graph_via"]})
    records = [{**deepcopy(item), "id": str(i), "content": "资料" * 1000} for i in range(100)]
    result = memory_listing(records, revision=7, max_path_chars=path_size)
    assert len(result["items"]) == 100 and result["truncated"]
    assert result["items"][0]["graph_via"] == item["graph_via"]
    assert sum("graph_via" in value for value in result["items"]) == 1
    for before, after in zip(records[1:], result["items"][1:], strict=True):
        assert after == {
            **{k: v for k, v in before.items() if k != "graph_via"},
            "path_omitted": True,
        }
    assert all("graph_via" in value and "path_omitted" not in value for value in records)
    assert json_size(result) > path_size  # This endpoint is not a whole-page tool budget.


async def test_http_returns_real_paths_and_forgetting_repairs_the_first_reference(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}, "memory": {"auto_extract": False}})
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    first, second = sourced(runtime.memory, "first"), sourced(runtime.memory, "second")
    tail = sourced(
        runtime.memory, "tail", alias="", subject="项目甲", predicate="使用", target="工具乙"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            response = await client.get("/api/memories", params={"query": "Lumi"})
            assert response.status_code == 200
            result = response.json()
            path = next(item["graph_via"] for item in result["items"] if item["id"] == tail["id"])
            assert path["revision"] == result["revision"] == runtime.memory.revision
            assert len(path["steps"]) == 2
            assert path["steps"][0]["to_id"] == path["steps"][1]["from_id"]
            for reference in [path["seed"], *path["steps"]]:
                detail = (await client.get(f"/api/memories/{reference['memory_id']}")).json()
                page = (await client.get(f"/api/memories/{reference['memory_id']}/source")).json()
                assert detail["source_id"] == page["source_id"] == reference["source_id"]
                assert page["principal"] == "owner" and page["trusted_owner"]
            assert (await client.delete(f"/api/memories/{first['id']}")).status_code == 200
            fresh = (await client.get("/api/memories", params={"query": "Lumi"})).json()
            new_path = next(
                item["graph_via"] for item in fresh["items"] if item["id"] == tail["id"]
            )
            assert new_path["seed"]["source_id"] == "second"
            assert new_path["revision"] > path["revision"]
            assert not new_path["seed"]["has_more"]
            config.update({"limits": {"max_output_chars": 1000}})
            bounded = (await client.get("/api/memory/query", params={"query": "Lumi"})).json()
            assert json_size(bounded) <= 1000 and bounded["truncated"]
            assert bounded["revision"] == runtime.memory.revision
            assert (await client.delete(f"/api/memories/{second['id']}")).status_code == 200
            last = (await client.get("/api/memories", params={"query": "Lumi"})).json()
            assert not last["items"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("stage", ["embedding", "rerank"])
async def test_awaited_query_does_not_recache_forgotten_path_proof(
    tmp_path: Path, stage: str
) -> None:
    store = Store(tmp_path / "memory.db")
    memory = Memory(store)
    sourced(memory, "first")
    sourced(memory, "second")
    tail = sourced(memory, "tail", alias="", subject="项目甲", predicate="使用", target="工具乙")
    media = PausedQueryMedia(stage)
    pipeline = MemoryPipeline(memory, EventBus(store), media=media)
    task = asyncio.create_task(pipeline.query("Lumi"))
    try:
        async with asyncio.timeout(3):
            await media.entered.wait()
            memory.forget_source("first")
            media.release.set()
            result = await task
        path = next(item["graph_via"] for item in result["memories"] if item["id"] == tail["id"])
        assert path["seed"]["source_id"] == "second"
        assert path["revision"] == result["revision"] == memory.revision
        assert "memory_changed" in result["degraded"] and not pipeline._cache
        assert media.embedding_attempts == 1
    finally:
        media.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await pipeline.close()
        store.close()


async def test_actual_query_tool_round_preserves_path_and_uses_current_budget(
    tmp_path: Path,
) -> None:
    turns: list[ModelTurn] = []
    runtime, seen = make_runtime(tmp_path, turns)
    application = None
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "测试"},
                "compaction": {"enabled": False},
                "memory": {"enabled": False},
                "limits": {"max_output_chars": 16000, "max_context_chars": 30000},
            }
        )
        await runtime.reconfigure()
        local = LocalServices(
            runtime, Attachments(runtime.store, tmp_path / "attachments", runtime.config)
        )
        application = MemoryApplication(runtime, local.media)
        sourced(runtime.memory, "first")
        tail = sourced(
            runtime.memory, "tail", alias="", subject="项目甲", predicate="使用", target="工具乙"
        )
        turns.extend(
            [
                ModelTurn(tool_calls=[ToolCall("query", "memory_query", '{"query":"Lumi"}')]),
                ModelTurn(content="已核对关系"),
            ]
        )
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "核查路径", mode="yolo")
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "已核对关系"
        output = next(message for message in seen[1] if message.tool_call_id == "query")
        value = json.loads(output.content)
        assert output.preserve_content and not value["truncated"] and "preview" not in value
        path = next(item["graph_via"] for item in value["memories"] if item["id"] == tail["id"])
        assert path["complete"] and len(path["steps"]) == 2
        assert path["revision"] == value["revision"]
        runtime.config.update({"limits": {"max_output_chars": 1000}})
        small = await application.query("Lumi")
        assert json_size(small) <= 1000 and small["truncated"]
        runtime.config.update({"limits": {"max_output_chars": 16000}})
        full = await application.query("Lumi")
        assert not full["truncated"] and full["memories"] == value["memories"]
    finally:
        if application is not None:
            await application.close()
        await runtime.close()


async def test_query_hot_budget_change_returns_structured_error_in_actual_tool_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns: list[ModelTurn] = []
    runtime, seen = make_runtime(tmp_path, turns)
    application = None
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "测试"},
                "compaction": {"enabled": False},
                "memory": {"enabled": False},
                "limits": {"max_output_chars": 16000, "max_context_chars": 30000},
            }
        )
        await runtime.reconfigure()
        local = LocalServices(
            runtime, Attachments(runtime.store, tmp_path / "attachments", runtime.config)
        )
        application = MemoryApplication(runtime, local.media)
        sourced(runtime.memory, "first")
        sourced(
            runtime.memory, "tail", alias="", subject="项目甲", predicate="使用", target="工具乙"
        )
        query = application.query

        async def shrink_after_query(
            text: str,
            limit: int = 8,
            *,
            hops: int | None = None,
            source_ids: Sequence[str] | None = None,
        ) -> dict[str, Any]:
            result = await query(text, limit=limit, hops=hops, source_ids=source_ids)
            assert json_size(result) > 1000
            runtime.config.update({"limits": {"max_output_chars": 1000}})
            return result

        monkeypatch.setattr(application, "query", shrink_after_query)
        turns.extend(
            [
                ModelTurn(tool_calls=[ToolCall("query", "memory_query", '{"query":"Lumi"}')]),
                ModelTurn(content="预算变化，未读取完整链"),
            ]
        )
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "核查路径", mode="yolo")
        assert (
            await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "预算变化，未读取完整链"
        )
        output = next(message for message in seen[1] if message.tool_call_id == "query")
        result = json.loads(output.content)
        assert result["code"] == "output_budget" and result["result_omitted"]
        assert output.preserve_content and len(output.content) <= 1000
        assert "preview" not in result and "memories" not in result
    finally:
        if application is not None:
            await application.close()
        await runtime.close()
