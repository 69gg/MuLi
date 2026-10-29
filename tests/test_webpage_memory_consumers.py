"""Existing semantic, comparison, context and notice consumers accept webpage scope.

These host-saved fixtures test consumers, not website or model answer quality.
The capture and real provider-backoff journeys live in the separate files.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_memory_source_failure import failure_events, source_for
from test_runtime import FakeClient
from test_teammate_attachment_runtime import eventually
from test_webpage_memory_import import owner_tool
from test_webpage_memory_policy import collect, saved, snapshot

from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.memory_watches import MATCH_EVENT
from muli.providers import ModelConfig, ModelTurn, ToolCall
from muli.system_events import attention_source
from muli.webpage_snapshot import WEBPAGE_NOTICE


async def test_webpage_tail_uses_real_embedding_rerank_and_source_scoped_fallback(
    tmp_path: Path,
) -> None:
    reason = "绛云航标只有核对琉璃仪的校准页后才能启用。"
    body = "无关背景，不包含最终条件。🙂\n" * 1000 + reason
    assert len(body) > 12_000 and reason not in body[:12_000]
    calls: list[tuple[str, dict[str, Any]]] = []
    fail_rerank = False

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "memory.invalid"
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path == "/v1/embeddings":
            assert payload["model"] == "webpage-embedding" and isinstance(payload["input"], list)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": [1.0, 0.0]}
                        for index in range(len(payload["input"]))
                    ]
                },
            )
        assert request.url.path == "/v1/rerank" and payload["model"] == "webpage-reranker"
        if fail_rerank:
            return httpx.Response(503)
        assert isinstance(payload["documents"], list) and payload["documents"]
        ranked = sorted(
            enumerate(payload["documents"]),
            key=lambda item: (-(1.0 if reason in item[1] else 0.1), item[0]),
        )[: payload["top_n"]]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0 if reason in text else 0.1}
                    for index, text in ranked
                ]
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
        actual_app(tmp_path) as rig,
    ):
        rig.runtime.config.update({"memory": {"auto_extract": False, "context_max_chars": 0}})
        await rig.memory.reconfigure()
        media = MediaService(
            MediaConfig(
                embedding=ServiceConfig(
                    model="webpage-embedding", base_url="https://memory.invalid/v1", dimensions=2
                ),
                reranker=ServiceConfig(
                    model="webpage-reranker", base_url="https://memory.invalid/v1"
                ),
            ),
            client=http,
        )
        await rig.memory.media.aclose()
        rig.memory.media = media
        rig.memory.pipeline.media = media
        item = await saved(rig, snapshot(body))
        imported = await collect(rig, item)
        assert "error" not in imported, imported
        source_id = imported["source_id"]
        done = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), 4)
        assert done is not None and done["state"] == "done"
        assert any(
            path == "/v1/embeddings" and any(reason in text for text in payload["input"])
            for path, payload in calls
        )
        unrelated = await saved(rig, snapshot("绛云航标的另一份独立历史说法。"))
        other = await collect(rig, unrelated)
        assert "error" not in other, other
        await asyncio.wait_for(rig.memory.pipeline.wait(other["source_id"]), 4)
        for failing in (False, True):
            fail_rerank = failing
            # Settle real owner-input jobs before an HTTP await can change the
            # query revision; this calls the application's shared read service.
            pending = rig.runtime.store.db.execute(
                "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry')"
            ).fetchall()
            for row in pending:
                await asyncio.wait_for(rig.memory.pipeline.wait(row["source_id"]), 4)
            result = await rig.memory.query(
                "绛云航标" if not failing else "琉璃仪", source_ids=[source_id], limit=4, hops=0
            )
            assert "error" not in result and result["memories"]
            assert all(memory["source_id"] == source_id for memory in result["memories"])
            assert any(reason in memory["quote"] for memory in result["memories"])
            assert any(
                path == "/v1/rerank" and any(reason in text for text in payload["documents"])
                for path, payload in calls
            )
            if not failing:
                assert any(memory["matched_by"] == "reranked" for memory in result["memories"])
            else:
                assert any(code.startswith("semantic_unavailable:") for code in result["degraded"])


async def test_actual_webpage_collection_retains_watch_comparison_and_context_identity(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        # Test the actual completed-source proof without allowing an automatic
        # conversational follow-up to race this consumer-focused test driver.
        await rig.app.state.attention.close()
        rig.runtime.config.update(
            {
                "attention": {"enabled": True, "events": ["memory"]},
                "memory": {"auto_extract": False, "context_max_chars": 0},
            }
        )
        await rig.memory.reconfigure()
        watch_call = "webpage-consumer-watch-create"
        turns = [
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        watch_call,
                        "memory_watch_create",
                        json.dumps(
                            {
                                "question": "绛云航标的启用条件是什么？",
                                "match_terms": ["绛云航标", "琉璃仪"],
                                "key": "webpage-consumer-watch",
                            }
                        ),
                    )
                ]
            ),
            ModelTurn(content="持续关注状态以工具回执为准。"),
        ]

        def factory(profile: ModelConfig) -> FakeClient:
            return FakeClient(turns, [])

        rig.runtime.client_factory = factory  # type: ignore[assignment]
        # The web ingress stamps the actual owner-text range required for a
        # standing watch; a bare Runtime.submit is not that provenance proof.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=rig.app), base_url="http://localhost:8765"
        ) as web:
            response = await web.post(
                f"/api/sessions/{rig.runtime.main_session()['id']}/messages",
                json={
                    "content": "明确持续留意绛云航标与琉璃仪这组短语的后续收藏资料。",
                    "mode": "yolo",
                },
            )
            assert response.status_code == 200
            await rig.settle(response.json())
        watch = rig.result(watch_call)
        assert "error" not in watch, watch
        first = await saved(rig, snapshot("绛云航标需核对琉璃仪。\n期限：七日\n"))
        imported = await collect(rig, first)
        assert "error" not in imported, imported
        job = await asyncio.wait_for(rig.memory.pipeline.wait(imported["source_id"]), 4)
        assert job is not None and job["state"] == "done"

        def matches() -> list[dict[str, Any]]:
            return [
                event
                for event in rig.runtime.store.events(limit=1000)
                if event["type"] == MATCH_EVENT
                and event["data"]["match"]["source_id"] == imported["source_id"]
            ]

        await eventually(rig.runtime, lambda: bool(matches()))
        assert len(matches()) == 1
        event = matches()[0]
        notice = attention_source(
            rig.runtime.store,
            event,
            ("memory",),
            max_output_chars=6000,
            memory_source_chars=rig.memory.pipeline.config.max_source_chars,
        )
        assert notice is not None and notice.memory_watch_proof is not None
        detail = json.loads(notice.detail)
        assert (
            detail["evidence_scope"] == "http_utf8_text_v1" and WEBPAGE_NOTICE in detail["notice"]
        )
        assert ":webpage:v1:" in detail["source"]
        assert notice.memory_watch_proof.watch_id == watch["id"]
        await collect(rig, first)
        assert len(matches()) == 1

        second = await saved(
            rig, snapshot("另一份独立资料。\n期限：十日\n", scope="html_static_text_v1")
        )
        later = await collect(rig, second)
        assert "error" not in later, later
        await asyncio.wait_for(rig.memory.pipeline.wait(later["source_id"]), 4)
        left = rig.runtime.memory.source(imported["source_id"])
        right = rig.runtime.memory.source(later["source_id"])
        assert left is not None and right is not None
        compared = await owner_tool(
            rig,
            "核对这两份已经明确收藏的网页静态资料。",
            "memory_compare_sources",
            left_id=left["id"],
            left_revision=left["digest"],
            right_id=right["id"],
            right_revision=right["digest"],
        )
        assert "error" not in compared and compared["status"] == "changed", compared
        assert compared["left"]["format"] == compared["right"]["format"] == "webpage"
        assert compared["left"]["scope"] == "http_utf8_text_v1"
        assert compared["right"]["scope"] == "html_static_text_v1"
        assert (
            compared["left"]["trusted_owner"] is False
            and compared["right"]["trusted_owner"] is False
        )
        large = rig.memory.pipeline.context_for_prompt_result("绛云航标", 6000).with_task
        assert left["source"] in large and left["id"] in large and '"trusted_owner": false' in large
        small = rig.memory.pipeline.context_for_prompt_result("绛云航标", 64).with_task
        assert left["id"] not in small and "webpage:v1" not in small


async def test_a_real_failed_webpage_job_keeps_scope_without_upstream_error_text(
    tmp_path: Path,
) -> None:
    async def reject(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"].startswith("attachment:"):
            raise RuntimeError("PRIVATE_WEBPAGE_UPSTREAM_BODY")
        return {"memories": [], "aliases": {}}

    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update({"memory": {"max_attempts": 1}})
        await rig.memory.reconfigure()
        rig.memory.pipeline.extractor = reject
        item = await saved(
            rig, snapshot("这段收藏正文不进入失败提醒。", scope="html_static_text_v1")
        )
        imported = await collect(rig, item)
        assert "error" not in imported, imported
        job = await asyncio.wait_for(rig.memory.pipeline.wait(imported["source_id"]), 4)
        assert job is not None and job["state"] == "failed"
        events = [
            event
            for event in failure_events(rig.runtime.store)
            if event["data"]["source_id"] == imported["source_id"]
        ]
        assert len(events) == 1
        notice = source_for(rig.memory.pipeline, events[0])
        assert notice is not None
        detail = json.loads(notice.detail)
        assert (
            detail["evidence_scope"] == "html_static_text_v1" and WEBPAGE_NOTICE in detail["notice"]
        )
        assert "PRIVATE_WEBPAGE_UPSTREAM_BODY" not in notice.detail
        assert "这段收藏正文不进入失败提醒。" not in notice.detail
        replay = await collect(rig, item)
        assert replay["created"] is False and replay["state"] == "failed"
