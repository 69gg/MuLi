"""Real reply collection, compaction, channels, Team and forgetting, offline.

Fixed model replies and HTTP transports establish data flow and lifecycle
boundaries, not natural-model retrieval quality. The actual old-path product
red was frozen separately before these green journeys replaced it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_continuity import platform_reply
from test_mobile_task_control_followup import phone
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.memory_application import MemoryApplication
from muli.memory_evidence import SOURCE_JOB_STATES
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


@asynccontextmanager
async def application(
    runtime: Runtime,
) -> AsyncIterator[tuple[MemoryApplication, httpx.AsyncClient]]:
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        memory = app.state.memory_application
        assert isinstance(memory, MemoryApplication) and memory._recovery is not None
        await asyncio.wait_for(asyncio.shield(memory._recovery), timeout=4)
        assert memory.idle
        yield memory, web


async def owner(runtime: Runtime, text: str) -> dict[str, Any]:
    receipt = await runtime.submit(text, source="web", mode="yolo")
    await asyncio.wait_for(runtime.wait(receipt["run_id"]), timeout=4)
    run = runtime.store.get("run", receipt["run_id"])
    assert run is not None and run["status"] == "completed"
    return receipt


async def settle_jobs(runtime: Runtime, memory: MemoryApplication) -> None:
    rows = runtime.store.db.execute(
        "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry') "
        "ORDER BY source_id LIMIT ?",
        (runtime.config.settings.memory.max_pending_jobs,),
    ).fetchall()
    for row in rows:
        job = await asyncio.wait_for(memory.pipeline.wait(row[0]), timeout=4)
        assert job is not None and job["state"] == "done"


class ReplyCollector(FakeClient):
    """Page the real reply; import sends only its public identity fields."""

    def __init__(self, runtime: Runtime, body: str) -> None:
        super().__init__([], [])
        self.runtime, self.body = runtime, body
        self.original: dict[str, Any] = {}
        self.receipt: dict[str, Any] = {}
        self.pages: list[dict[str, Any]] = []

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        names = {item["function"]["name"] for item in tools}
        assert {"conversation_read", "memory_import_reply"} <= names
        replies = {item.tool_call_id for item in messages if item.role == "tool"}
        if "import-reply" in replies:
            self.receipt.update(tool_result(messages, "import-reply"))
            assert "error" not in self.receipt
            assert self.receipt["message_id"] == self.original["message_id"]
            assert self.receipt["seq"] == self.original["seq"]
            assert self.receipt["message_revision"] == self.original["revision"]
            assert self.receipt["content_bytes"] == len(self.body.encode("utf-8"))
            assert self.receipt["role"] == "assistant" and not self.receipt["trusted_owner"]
            assert self.receipt["state"] in SOURCE_JOB_STATES
            assert "content" not in self.receipt and "memory_id" not in self.receipt
            turn = ModelTurn(content="已按准确历史版本提交完整答复收藏。")
        elif any(str(identifier).startswith("read-reply-") for identifier in replies):
            page = tool_result(messages, f"read-reply-{len(self.pages)}")
            assert "error" not in page
            assert page["offset_bytes"] == sum(part["length_bytes"] for part in self.pages)
            self.pages.append(page)
            if page["has_more"]:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            f"read-reply-{len(self.pages)}",
                            "conversation_read",
                            session_id=page["session_id"],
                            message_id=page["message_id"],
                            revision=page["revision"],
                            offset_bytes=page["next_offset_bytes"],
                        )
                    ]
                )
            else:
                exact = "".join(part["content"] for part in self.pages) == self.body
                assert exact
                self.original.update(self.pages[0])
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "import-reply",
                            "memory_import_reply",
                            message_id=page["message_id"],
                            revision=page["revision"],
                        )
                    ]
                )
        elif "locate-reply" in replies:
            directory = tool_result(messages, "locate-reply")
            assert "error" not in directory
            title = self.body.split("\n", 1)[0]
            entry = next(
                item
                for item in directory["records"]
                if item["role"] == "assistant" and item["content"].startswith(title)
            )
            turn = ModelTurn(
                tool_calls=[
                    call(
                        "read-reply-0",
                        "conversation_read",
                        session_id=self.runtime.main_session()["id"],
                        message_id=entry["id"],
                        revision=entry["revision"],
                    )
                ]
            )
        else:
            turn = ModelTurn(
                tool_calls=[
                    call(
                        "locate-reply",
                        "conversation_read",
                        session_id=self.runtime.main_session()["id"],
                        limit=20,
                    )
                ]
            )
        yield StreamEvent("done", turn=turn)


class SourceReader(FakeClient):
    """Every source ID, revision and byte coordinate comes from real tool results."""

    def __init__(
        self,
        runtime: Runtime,
        memory: MemoryApplication,
        collector: ReplyCollector,
        evidence: str,
        query: str,
        *,
        worker: bool,
    ) -> None:
        super().__init__([], [])
        self.runtime, self.memory, self.collector = runtime, memory, collector
        self.evidence, self.query, self.worker = evidence, query, worker
        self.pages: list[dict[str, Any]] = []
        self.found: dict[str, Any] = {}
        self.run_id = ""
        self.final = "队友已核对来源全文。" if worker else "私聊已核对来源全文。"

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        runtime = self.runtime
        main_id = runtime.main_session()["id"]
        latest = next(
            item for item in reversed(runtime.store.messages(main_id)) if item["role"] == "user"
        )
        if not self.worker and str(latest.get("principal", "")).startswith("agent:"):
            yield StreamEvent("done", turn=ModelTurn(content="已收到队友的只读核查回报。"))
            return
        session_id = runtime.team.get("原件只读核查队友")["id"] if self.worker else main_id
        self.run_id = runtime.run_ids[session_id]
        names = {item["function"]["name"] for item in tools}
        assert {"memory_query", "memory_source"} <= names
        assert all(
            item.source_message_id != self.collector.original["message_id"] for item in messages
        )
        replies = {item.tool_call_id for item in messages if item.role == "tool"}
        if self.worker:
            assert "memory_import_reply" not in names
            if "denied-worker-import" not in replies:
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            call(
                                "denied-worker-import",
                                "memory_import_reply",
                                message_id=self.collector.original["message_id"],
                                revision=self.collector.original["revision"],
                            )
                        ]
                    ),
                )
                return
            assert "error" in tool_result(messages, "denied-worker-import")
        if "query-reply" not in replies:
            await settle_jobs(runtime, self.memory)
            turn = ModelTurn(
                tool_calls=[call("query-reply", "memory_query", query=self.query, limit=4)]
            )
        elif not self.found:
            result = tool_result(messages, "query-reply")
            assert "error" not in result and result["degraded"] == []
            match = next(item for item in result["memories"] if self.evidence in item["content"])
            assert match["source_id"] == self.collector.receipt["source_id"]
            assert not match["trusted_owner"] and match["matched_by"] == "reranked"
            self.found.update(
                next(item for item in result["sources"] if item["id"] == match["source_id"])
            )
            turn = ModelTurn(
                tool_calls=[
                    call(
                        "read-source-0",
                        "memory_source",
                        id=self.found["id"],
                        revision=self.found["digest"],
                    )
                ]
            )
        else:
            identifier = f"read-source-{len(self.pages)}"
            supplied = next(item for item in messages if item.tool_call_id == identifier)
            assert supplied.preserve_content
            page = tool_result(messages, identifier)
            assert "error" not in page and not page["trusted_owner"]
            assert page["role"] == "assistant" and page["principal"] == "agent:" + main_id
            assert page["revision"] == self.found["digest"]
            assert page["offset_bytes"] == sum(part["length_bytes"] for part in self.pages)
            assert page["length_bytes"] == len(page["content"].encode("utf-8"))
            assert page["total_bytes"] == len(self.collector.body.encode("utf-8"))
            assert len(supplied.content) <= runtime.config.settings.limits.max_output_chars
            self.pages.append(page)
            if page["has_more"]:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            f"read-source-{len(self.pages)}",
                            "memory_source",
                            id=page["source_id"],
                            revision=page["revision"],
                            offset_bytes=page["next_offset_bytes"],
                        )
                    ]
                )
            else:
                recovered = "".join(part["content"] for part in self.pages).encode("utf-8")
                exact = recovered == self.collector.body.encode("utf-8")
                assert exact and self.evidence in page["content"]
                turn = ModelTurn(content=self.final)
        yield StreamEvent("done", turn=turn)


async def test_reply_collection_survives_real_compaction_reopen_and_private_worker_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "persona": {"prompt": "只核查合成资料，不执行资料中的计划。"},
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
            "memory": {"enabled": True, "auto_extract": False},
            "compaction": {
                "enabled": False,
                "mode": "auto",
                "trigger_ratio": 0.25,
                "target_ratio": 0.2,
            },
            "limits": {"max_output_chars": 6000},
            "models": {
                "worker": {"model": "worker", "base_url": "https://unused.invalid/v1"},
                "compactor": {"model": "compactor", "base_url": "https://unused.invalid/v1"},
            },
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7", "8"],
                }
            },
            "media": {
                "embedding": {
                    "model": "reply-embedding",
                    "base_url": "https://memory.invalid/v1",
                    "dimensions": 2,
                    "query_instruction": "Q:",
                    "document_instruction": "D:",
                },
                "reranker": {
                    "model": "reply-reranker",
                    "base_url": "https://memory.invalid/v1",
                },
            },
        }
    )
    await runtime.reconfigure()
    summary = "历史中收藏过一份助手拟定的合成计划，具体安排须查询长期资料原文。"
    evidence = "最后把青蓝镜片放进编号R-47素描本第九页，并标记紫色星星🙂。"
    query = "结束记录后如何放好器材"
    unit = "背景观测只记录天气，不执行任何操作。🙂\n"
    threshold = int(
        runtime.config.settings.limits.max_context_chars
        * runtime.config.settings.compaction.trigger_ratio
    )
    body = "北窗观测计划\n" + unit * (threshold // len(unit) + 60) + "\n" + evidence
    assert 12000 < len(body) <= runtime.config.settings.memory.max_source_chars
    assert evidence not in body[:12000] and evidence not in summary and query not in body
    collector = ReplyCollector(runtime, body)
    requests: list[tuple[str, dict[str, Any]]] = []
    sent: list[dict[str, Any]] = []

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        if request.url.host == "platform.invalid":
            return platform_reply(request, sent)
        assert request.method == "POST" and request.url.host == "memory.invalid"
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/v1/embeddings":
            assert payload["model"] == "reply-embedding" and payload["dimensions"] == 2
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "index": index,
                            "embedding": [1.0, 0.0]
                            if evidence in value or value == "Q:" + query
                            else [0.0, 1.0],
                        }
                        for index, value in enumerate(payload["input"])
                    ]
                },
            )
        assert request.url.path == "/v1/rerank" and payload["query"] == query
        ranked = sorted(enumerate(payload["documents"]), key=lambda item: evidence not in item[1])[
            : payload["top_n"]
        ]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0 if evidence in value else 0.1}
                    for index, value in ranked
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    class Summary(FakeClient):
        async def complete(
            self,
            messages: list[Message],
            tools: Any = (),
            *,
            attempt_guard: Callable[[], None] | None = None,
        ) -> ModelTurn:
            if attempt_guard is not None:
                attempt_guard()
            return ModelTurn(content=summary)

    first_turns = [ModelTurn(content=body)]

    def first_factory(config: ModelConfig) -> FakeClient:
        if config.model == "compactor":
            return Summary([], [])
        return collector if not first_turns else FakeClient(first_turns, [])

    runtime.client_factory = first_factory  # type: ignore[assignment]
    main_id = runtime.main_session()["id"]
    try:
        async with application(runtime) as (memory, _web):
            produced = await owner(runtime, "请给一份完整观测计划，本轮只拟定，不收藏也不执行。")
            producing_run = runtime.store.get("run", produced["run_id"])
            assert producing_run is not None and producing_run["result_message_id"]
            source_id = "reply:" + producing_run["result_message_id"]
            assert runtime.memory.source(source_id) is None
            assert memory.pipeline.job(source_id) is None
            await owner(runtime, "现在明确收藏刚才那份完整计划为原文资料，供以后跨渠道查询。")
            receipt, original = collector.receipt, collector.original
            assert receipt["source_id"] == source_id and receipt["created"] is True
            assert original["message_id"] == producing_run["result_message_id"]
            accepted = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "import-reply"
            )
            assert accepted["content_provenance"] == "accepted"
            assert json.loads(accepted["content"]) == receipt
            source = runtime.memory.source(source_id)
            assert source is not None
            exact = source["content"].encode("utf-8") == body.encode("utf-8")
            assert exact and source["role"] == "assistant" and not source["trusted_owner"]
            assert source["session_id"] == main_id and source["principal"] == "agent:" + main_id
            assert receipt["registered_revision"] == source["digest"]
            saved = runtime.store.get("message", original["message_id"])
            assert saved is not None and source["created_at"] == saved["created_at"]
            marker = json.loads(source["source"].removeprefix("reply:v1:"))
            expected = {
                "main_session_id": main_id,
                "message_id": original["message_id"],
                "seq": original["seq"],
                "message_revision": original["revision"],
                "producing_run_id": produced["run_id"],
                "original_input_id": produced["message"]["id"],
                "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
            assert marker == expected
            job = await asyncio.wait_for(memory.pipeline.wait(source_id), timeout=4)
            assert job is not None and job["state"] == "done"
            chunk_ids = job["checkpoint"]["source_chunks"]["memory_ids"]
            assert len(chunk_ids) > 1
            tail_saved = any(
                evidence in runtime.memory.get(identifier)["content"] for identifier in chunk_ids
            )
            assert tail_saved and runtime.memory.automatic_episode_id(source_id) is not None
            runtime.config.update({"compaction": {"enabled": True}})
            runtime.compaction.observe(main_id)
            await runtime.compaction.finish(main_id)
            compressed = runtime.store.get("compaction", main_id)
            assert compressed is not None and compressed["kind"] == "semantic"
            assert compressed["summary"] == summary and compressed["covered_seq"] >= original["seq"]
            assert original["message_id"] not in {
                item["id"] for item in runtime.compaction.project(main_id)
            }
    finally:
        await runtime.close()

    current, _ = make_runtime(tmp_path, [])
    try:
        async with application(current) as (memory, web):
            assert current.main_session()["id"] == main_id
            assert current.compaction.status(main_id)["covered_seq"] >= original["seq"]
            private = SourceReader(current, memory, collector, evidence, query, worker=False)
            worker = SourceReader(current, memory, collector, evidence, query, worker=True)

            def reader_factory(config: ModelConfig) -> FakeClient:
                return worker if config.model == "worker" else private

            current.client_factory = reader_factory  # type: ignore[assignment]
            question = (
                "换个已绑定私聊查之前收藏的资料：结束记录后如何放好器材？请先查记忆再核全文。"
            )
            assert evidence not in question and source_id not in question
            response = await phone(web, question, 26871, sender=8)
            assert response.status_code == 200
            await eventually(current, lambda: bool(completed(current, private.final)))
            await asyncio.wait_for(current.wait(private.run_id), timeout=4)
            incoming = next(
                item for item in current.store.messages(main_id) if item.get("content") == question
            )
            assert incoming["principal"] == "owner" and incoming["source"] == "channel:onebot:123"
            assert incoming["route"]["target"] == "8" and len(private.pages) > 1
            child = await current.team.spawn(
                "原件只读核查队友", "只读查询长期资料，查清观测收尾安排并核对原文全文。"
            )
            waited = await current.team.wait([child["id"]], timeout=3)
            assert waited["timed_out"] is False
            assert current.team.get(child["id"])["status"] == "completed" and len(worker.pages) > 1
            assert child["parent_id"] == main_id
            assert any(
                path == "/v1/embeddings"
                and any(value.startswith("D:") and evidence in value for value in payload["input"])
                for path, payload in requests
            )
            assert any(
                path == "/v1/embeddings" and payload["input"] == ["Q:" + query]
                for path, payload in requests
            )
            assert any(
                path == "/v1/rerank" and any(evidence in value for value in payload["documents"])
                for path, payload in requests
            )
    finally:
        await current.close()


async def test_slow_reply_extraction_does_not_block_owner_or_revive_forgotten_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("慢提炼竞争旅程只能使用固定模型，不能联网")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "persona": {"prompt": "只核查合成计划。"},
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
            "compaction": {"enabled": False},
            "memory": {"enabled": True, "auto_extract": True},
            "limits": {"max_parallel_agents": 1},
            "models": {
                "memory_extractor": {
                    "model": "reply-extractor",
                    "base_url": "https://unused.invalid/v1",
                }
            },
        }
    )
    await runtime.reconfigure()
    evidence = "收尾时把青色纸卡夹在素描本第九页。"
    body = "短时观测计划\n先记录天气。\n" + evidence
    collector = ReplyCollector(runtime, body)
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    phase = "produce"

    class Extractor(FakeClient):
        async def complete(
            self,
            messages: list[Message],
            tools: Any = (),
            *,
            attempt_guard: Callable[[], None] | None = None,
        ) -> ModelTurn:
            if attempt_guard is not None:
                attempt_guard()
            source = json.loads(messages[-1].content)
            proposals: list[dict[str, Any]] = []
            if source["id"].startswith("reply:"):
                assert source["content"] == body and not source["trusted_owner"]
                entered.set()
                await release.wait()
                proposals.append(
                    {
                        "layer": "semantic",
                        "content": evidence,
                        "quote": evidence,
                        "entities": ["纸卡", "素描本"],
                        "relations": [{"subject": "纸卡", "predicate": "夹入", "object": "素描本"}],
                        "confidence": 0.9,
                        "claim_key": "observation.finish",
                    }
                )
                returned.set()
            return ModelTurn(
                content=json.dumps({"memories": proposals, "aliases": {}}, ensure_ascii=False)
            )

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "reply-extractor":
            return Extractor([], [])
        if phase == "collect":
            return collector
        answer = body if phase == "produce" else "当前主人新输入已正常完成。"
        return FakeClient([ModelTurn(content=answer)], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        async with application(runtime) as (memory, web):
            await owner(runtime, "请拟一份小计划，暂不收藏也不执行。")
            phase = "collect"
            await owner(runtime, "现在明确收藏刚才的完整答复为长期原文资料。")
            source_id = collector.receipt["source_id"]
            await asyncio.wait_for(entered.wait(), timeout=4)
            assert not release.is_set() and memory.pipeline.job(source_id)["state"] == "processing"
            phase = "independent"
            await owner(runtime, "请直接答复这条新输入，不要等待后台提炼。")
            assert not release.is_set() and memory.pipeline.job(source_id)["state"] == "processing"
            listing = await web.get("/api/memories", params={"query": "纸卡"})
            assert listing.status_code == 200
            actual = next(
                item for item in listing.json()["items"] if item["source_id"] == source_id
            )
            forgotten = await web.delete(f"/api/memories/{actual['id']}")
            assert forgotten.status_code == 200 and forgotten.json() == {"deleted": True}
            position = runtime.store.event_position()
            release.set()
            await asyncio.wait_for(returned.wait(), timeout=4)
            await eventually(
                runtime,
                lambda: any(
                    event["type"] == "memory.pipeline.updated"
                    and event["data"].get("source_id") == source_id
                    for event in runtime.store.events(after=position)
                ),
            )
            assert runtime.memory.source(source_id) is None
            assert memory.pipeline.job(source_id) is None
            assert (
                runtime.store.db.execute(
                    "SELECT count(*) FROM memory_meta WHERE source_id=?", (source_id,)
                ).fetchone()[0]
                == 0
            )
    finally:
        release.set()
        await runtime.close()

    current, _ = make_runtime(tmp_path, [])
    turns = [
        ModelTurn(
            tool_calls=[
                call(
                    "recollect-forgotten",
                    "memory_import_reply",
                    message_id=collector.original["message_id"],
                    revision=collector.original["revision"],
                )
            ]
        ),
        ModelTurn(tool_calls=[call("query-forgotten", "memory_query", query="纸卡")]),
        ModelTurn(tool_calls=[call("read-forgotten", "memory_source", id=source_id)]),
        ModelTurn(content="已核查遗忘记录，旧来源未恢复。"),
    ]

    class Forgotten(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if turns and turns[0].tool_calls and turns[0].tool_calls[0].name == "memory_query":
                await settle_jobs(current, memory)
            async for event in super().stream(messages, tools):
                yield event

    def reopened_factory(config: ModelConfig) -> FakeClient:
        return Extractor([], []) if config.model == "reply-extractor" else Forgotten(turns, [])

    current.client_factory = reopened_factory  # type: ignore[assignment]
    try:
        async with application(current) as (memory, _web):
            await owner(current, "请再次收藏之前那条完整答复；若已遗忘，按遗忘回执处理并核查原文。")
            records = current.store.messages(current.main_session()["id"])
            results = {
                item["tool_call_id"]: json.loads(item["content"])
                for item in records
                if item.get("tool_call_id")
                in {"recollect-forgotten", "query-forgotten", "read-forgotten"}
            }
            assert results["recollect-forgotten"] == {
                "source_id": source_id,
                "state": "forgotten",
                "created": False,
            }
            found = results["query-forgotten"]
            assert "error" not in found
            assert all(item.get("source_id") != source_id for item in found["memories"])
            assert all(item["id"] != source_id for item in found["sources"])
            assert results["read-forgotten"]["code"] == "evidence_not_found"
            assert current.memory.source(source_id) is None
            assert memory.pipeline.job(source_id) is None
            assert (
                current.store.db.execute(
                    "SELECT 1 FROM memory_forgotten_sources WHERE id=?", (source_id,)
                ).fetchone()
                is not None
            )
    finally:
        await current.close()
