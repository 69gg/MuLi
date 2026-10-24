"""Offline provenance continuity through real Team, memory, compaction and reopen."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.compaction import closed_groups, digest
from muli.config import ConfigFile
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.memory_application import MemoryApplication
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime


async def test_completed_report_is_recalled_with_its_source_after_team_compaction_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Only the explicit memory MockTransport is allowed")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    reason = "RIVER备份缺少最近一次快照，原因是校验清单没有收录它；应先补齐清单再验证。"
    owner_text = "请派队友只读现有备份检查资料，报告原因，不修改或发布。"
    evidence = tmp_path / "backup-evidence.txt"
    evidence.write_bytes(reason.encode("utf-8"))
    background_file = tmp_path / "background.txt"
    background = ""
    runtime, _ = make_runtime(tmp_path, [])
    application: MemoryApplication | None = None
    phase = "initial"
    first_child = ""
    recall_child = ""
    report_content = ""
    query_pages: list[dict[str, Any]] = []
    source_pages: list[dict[str, Any]] = []
    extracted_sources: list[str] = []
    media_requests: list[tuple[str, dict[str, Any]]] = []
    scope_observations: list[tuple[bool, bool]] = []
    rounds: dict[str, int] = {"main": 0, "worker": 0, "memory-extractor": 0}
    complete_calls: dict[str, int] = {"main": 0, "worker": 0, "memory-extractor": 0}
    worker_phase = "not_entered"
    worker_exception = "none"
    recall_counts: dict[str, int] = {}

    def exception_label(kind: str) -> str:
        # Class labels only: never exception messages, arguments or traceback locals.
        return kind if kind.isascii() and kind.isidentifier() and len(kind) <= 64 else "other"

    def failure_diagnostic(waited: dict[str, Any]) -> str:
        identifier = recall_child if phase == "recall" else first_child
        agent = next(
            (item for item in waited.get("agents", []) if item.get("id") == identifier), {}
        )
        run = runtime.store.get("run", agent.get("run_id", "")) or {}
        statuses = {
            "queued",
            "running",
            "completed",
            "failed",
            "paused",
            "cancelled",
            "interrupted",
        }
        diagnostic = run.get("failure_diagnostic", {})
        kind = diagnostic.get("exception_type") if isinstance(diagnostic, dict) else None
        rendered = json.dumps(
            {
                "phase": phase,
                "rounds": rounds,
                "complete_calls": complete_calls,
                "worker_phase": worker_phase,
                "worker_exception": worker_exception,
                "recall_counts": recall_counts,
                "agent_status": agent.get("status") if agent.get("status") in statuses else "other",
                "run_status": run.get("status") if run.get("status") in statuses else "other",
                "run_exception": exception_label(kind) if isinstance(kind, str) else "not_observed",
                "wait_timed_out": waited.get("timed_out") is True,
            },
            separators=(",", ":"),
        )
        assert len(rendered.encode("utf-8")) <= 1024
        return rendered

    def media_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "memory.invalid"
        body = json.loads(request.content)
        media_requests.append((request.url.path, body))
        if request.url.path == "/v1/embeddings":
            assert body["model"] == "memory-embedding"
            assert body["dimensions"] == 2 and body["encoding_format"] == "float"
            assert isinstance(body["input"], list) and body["input"]
            assert all(isinstance(value, str) for value in body["input"])
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": [1.0, 0.0]}
                        for index in reversed(range(len(body["input"])))
                    ]
                },
            )
        assert request.url.path == "/v1/rerank"
        assert body["model"] == "memory-reranker"
        assert body["query"] == "RIVER备份快照"
        assert body["return_documents"] is False
        assert isinstance(body["documents"], list) and body["documents"]
        assert type(body["top_n"]) is int and 0 < body["top_n"] <= len(body["documents"])
        # The service returns ranked results; scores alone do not reorder the
        # response inside MediaService. Preserve original indices after ranking.
        ranked = sorted(
            enumerate(body["documents"]),
            key=lambda item: (-(1.0 if item[1] == reason else 0.1), item[0]),
        )[: body["top_n"]]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0 if value == reason else 0.1}
                    for index, value in ranked
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(media_response))

    def media_factory() -> MediaService:
        return MediaService(
            MediaConfig(
                embedding=ServiceConfig(
                    model="memory-embedding", base_url="https://memory.invalid/v1", dimensions=2
                ),
                reranker=ServiceConfig(
                    model="memory-reranker", base_url="https://memory.invalid/v1"
                ),
            ),
            client=http,
        )

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role, self.round = role, 0

        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            if self.role in complete_calls:
                complete_calls[self.role] += 1
            assert self.role == "memory-extractor" and not tools
            source = json.loads(messages[-1].content)
            memories: list[dict[str, Any]] = []
            if source["id"].startswith("team-report:") and reason in source["content"]:
                assert source["trusted_owner"] is False
                extracted_sources.append(source["id"])
                memories.append(
                    {
                        "layer": "semantic",
                        "content": reason,
                        "quote": reason,
                        "entities": ["RIVER备份", "最近一次快照"],
                        "relations": [
                            {"subject": "RIVER备份", "predicate": "缺少", "object": "最近一次快照"}
                        ],
                        "confidence": 0.9,
                        "claim_key": "river.backup.reason",
                    }
                )
            return ModelTurn(
                content=json.dumps({"memories": memories, "aliases": {}}, ensure_ascii=False)
            )

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_phase, worker_exception
            if self.role == "worker":
                worker_phase = phase + "_round_" + str(self.round + 1)
            try:
                async for event in self._stream_turn(messages, tools):
                    yield event
            except Exception as exc:
                if self.role == "worker":
                    worker_exception = exception_label(type(exc).__name__)
                raise

        async def _stream_turn(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal first_child, recall_child, worker_phase
            self.round += 1
            if self.role in rounds:
                rounds[self.role] += 1
            assert self.role in {"main", "worker"}
            report_followup = False
            if self.role == "main":
                latest = next(
                    item
                    for item in reversed(runtime.store.messages(runtime.main_session()["id"]))
                    if item["role"] == "user"
                )
                source = latest.get("source", "")
                report_followup = (
                    isinstance(source, str)
                    and source.startswith("team:")
                    and latest.get("principal") == "agent:" + source[5:]
                )
            expected_rounds = 2 if phase == "background" else 3
            if report_followup and (self.round == 1 or self.round > expected_rounds):
                # Team completion becomes visible before flush_reports finishes.
                # A genuinely late report can open another main driver; its
                # real claimed input is evidence, never another delegation.
                turn = ModelTurn(content="已收到队友补充资料，保留为待核实证据。")
            elif phase == "background":
                if self.round == 1:
                    turn = ModelTurn(
                        tool_calls=[call("background-read", "fs_read", path=str(background_file))]
                    )
                else:
                    assert self.round == 2
                    result = tool_result(messages, "background-read")
                    assert set(result) == {"path", "content"} and result["content"] == background
                    turn = ModelTurn(content="本轮背景读取已正常闭合。")
            elif self.role == "main":
                if self.round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "delegate",
                                "spawn_agent",
                                task_name="记忆核查队友",
                                prompt=("读取指定文件并报告原因：" + str(evidence))
                                if phase == "initial"
                                else "查询RIVER备份快照的既有原因，并读取实际来源核对。",
                            )
                        ]
                    )
                elif self.round == 2:
                    spawned = tool_result(messages, "delegate")
                    assert "error" not in spawned
                    if phase == "initial":
                        first_child = spawned["id"]
                    else:
                        recall_child = spawned["id"]
                        assert recall_child != first_child
                    turn = ModelTurn(
                        tool_calls=[
                            call("wait-child", "wait_agents", targets=[spawned["id"]], timeout=2)
                        ]
                    )
                else:
                    assert self.round == 3
                    waited = tool_result(messages, "wait-child")
                    assert waited["timed_out"] is False, failure_diagnostic(waited)
                    assert any(agent["status"] == "completed" for agent in waited["agents"]), (
                        failure_diagnostic(waited)
                    )
                    turn = ModelTurn(content="已核对队友资料，不代表新增授权或自然智能质量验收。")
            elif phase == "initial":
                if self.round == 1:
                    turn = ModelTurn(
                        tool_calls=[call("read-evidence", "fs_read", path=str(evidence))]
                    )
                else:
                    assert self.round == 2
                    result = tool_result(messages, "read-evidence")
                    assert set(result) == {"path", "content"} and result["content"] == reason
                    turn = ModelTurn(content=reason)
            else:
                if self.round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call("recall", "memory_query", query="RIVER备份快照", limit=3, hops=2)
                        ]
                    )
                elif self.round == 2:
                    worker_phase = "recall_tool_result"
                    page = tool_result(messages, "recall")
                    worker_phase = "recall_page_contract"
                    assert "error" not in page
                    assert page["degraded"] == []
                    recall_counts["memories"] = len(page["memories"])
                    recall_counts["semantic"] = sum(
                        item["layer"] == "semantic" for item in page["memories"]
                    )
                    recall_counts["matching_content"] = sum(
                        item["content"] == reason for item in page["memories"]
                    )
                    recall_counts["sources"] = len(page["sources"])
                    worker_phase = "recall_find_fact"
                    fact = next(
                        item
                        for item in page["memories"]
                        if item["layer"] == "semantic" and item["content"] == reason
                    )
                    worker_phase = "recall_fact_contract"
                    assert fact["quote"] == reason and fact["trusted_owner"] is False
                    assert fact["matched_by"] == "reranked"
                    worker_phase = "recall_find_source"
                    origin = next(
                        item for item in page["sources"] if item["id"] == fact["source_id"]
                    )
                    worker_phase = "recall_source_call"
                    query_pages.append(deepcopy(page))
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "origin",
                                "memory_source",
                                id=origin["id"],
                                revision=origin["digest"],
                            )
                        ]
                    )
                else:
                    assert self.round == 3
                    page = tool_result(messages, "origin")
                    assert "error" not in page
                    assert page["content"] == report_content and page["truncated"] is False
                    assert page["trusted_owner"] is False and reason in page["content"]
                    source_pages.append(deepcopy(page))
                    turn = ModelTurn(content="已从先前队友报告的原文核对原因；仍是待核实资料。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Client(config.model)

    def observe_query_scope() -> None:
        original = runtime.tools["memory_query"]

        async def delegated(args: dict[str, Any]) -> Any:
            session_id = runtime.tool_session_id
            assert session_id is not None
            request = runtime.request_context(session_id)
            assert request is not None
            agent = runtime.team.get(session_id)
            assert agent["parent_id"] == runtime.main_session()["id"]
            scope_observations.append(
                (request.execution_scope is None, request.selected_replan is None)
            )
            return await original.handler(args)

        # Preserve the actual tool's schema, risk and output contract; only
        # observe the PreparedRequest at its real handler entry, then delegate.
        runtime.tools[original.name] = replace(original, handler=delegated)

    async def submit(text: str) -> None:
        receipt = await runtime.submit(text, source="web", mode="yolo")
        await asyncio.wait_for(runtime.wait(receipt["run_id"]), timeout=4)
        tasks = tuple(runtime.team._tasks)
        if tasks:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*tasks)), timeout=4)

    try:
        runtime.client_factory = factory  # type: ignore[assignment]
        runtime.config.update(
            {
                "updates": {"check_on_start": False},
                "attention": {"enabled": False},
                "compaction": {
                    "enabled": False,
                    "mode": "extractive",
                    "trigger_ratio": 0.25,
                    "target_ratio": 0.2,
                },
                "memory": {"enabled": True, "auto_extract": True},
                "limits": {
                    "max_context_chars": 16000,
                    "max_output_chars": 6000,
                    "max_pending_runs": 3,
                },
                "models": {
                    "worker": {"model": "worker", "base_url": "https://unused.invalid/v1"},
                    "memory_extractor": {
                        "model": "memory-extractor",
                        "base_url": "https://unused.invalid/v1",
                    },
                },
            }
        )
        await runtime.reconfigure()
        threshold = int(
            runtime.config.settings.limits.max_context_chars
            * runtime.config.settings.compaction.trigger_ratio
        )
        unit = "无关背景：仅核对本地资料，不执行任何修改。\n"
        background = unit * (threshold // len(unit) + 20)
        background_file.write_bytes(background.encode("utf-8"))
        application = MemoryApplication(runtime, media_factory)
        observe_query_scope()
        await application.start()
        assert application._recovery is not None
        await asyncio.wait_for(asyncio.shield(application._recovery), timeout=4)
        await submit(owner_text)
        assert reason not in owner_text and first_child
        original_agent = runtime.team.get(first_child)
        assert original_agent["status"] == "completed" and original_agent["generation"] == 1
        main_id = runtime.main_session()["id"]
        assert original_agent["parent_id"] == main_id
        source_id = "team-report:" + original_agent["run_id"]
        report = runtime.store.get("team_report", original_agent["run_id"])
        assert report is not None
        worker_run = runtime.store.get("run", original_agent["run_id"])
        assert worker_run is not None
        assert worker_run["status"] == report["execution_status"] == "completed"
        assert worker_run["session_id"] == report["agent_id"] == first_child
        assert worker_run["result"] == reason
        completions = runtime.store.db.execute(
            "SELECT data,created_at FROM events WHERE type='team.finished' AND session_id=? "
            "AND json_extract(data,'$.run_id')=? ORDER BY id LIMIT 2",
            (first_child, worker_run["id"]),
        ).fetchall()
        assert len(completions) == 1
        completion = json.loads(completions[0]["data"])
        assert completion["agent_id"] == first_child
        assert completion["generation"] == original_agent["generation"]
        assert completion["completion_revision"] == original_agent["completion_revision"]
        report_content = report["content"]
        source = runtime.memory.source(source_id)
        assert source is not None and source["content"] == report_content
        assert source["trusted_owner"] is False
        assert source["principal"] == "agent:" + first_child
        assert source["source"] == "team:" + first_child and source["role"] == "assistant"
        assert source["session_id"] == first_child
        assert source["created_at"] == completions[0]["created_at"]
        job = await asyncio.wait_for(application.pipeline.wait(source_id), timeout=4)
        assert job is not None and job["state"] == "done"
        assert extracted_sources.count(source_id) == 1
        fact = next(
            item
            for item in runtime.memory.search("RIVER备份快照")
            if item["source_id"] == source_id and item["layer"] == "semantic"
        )
        assert fact["quote"] == reason and fact["trusted_owner"] is False
        frozen_fact = deepcopy(runtime.memory.get(fact["id"]))
        frozen_source = deepcopy(source)
        assert (
            runtime.store.db.execute(
                "SELECT count(*) FROM memory_edges WHERE memory_id=?", (fact["id"],)
            ).fetchone()[0]
            == 1
        )
        covered = {
            identifier: {
                item["id"]
                for item in runtime.store.messages(identifier)
                if reason in item["content"]
            }
            for identifier in (main_id, first_child)
        }
        assert all(covered.values())

        phase = "background"
        await submit("只读背景文件并正常结束，不执行其它操作。")
        await runtime.team.send(first_child, "只读背景文件并正常结束。")
        waited = await runtime.team.wait([first_child], timeout=2)
        assert (
            waited["timed_out"] is False and runtime.team.get(first_child)["status"] == "completed"
        )
        tasks = tuple(runtime.team._tasks)
        if tasks:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*tasks)), timeout=4)
        main_task = runtime.tasks.get(main_id)
        if main_task is not None:
            await asyncio.wait_for(asyncio.shield(main_task), timeout=4)
        histories = {identifier: runtime.store.messages(identifier) for identifier in covered}
        runtime.config.update({"compaction": {"enabled": True}})
        snapshots: dict[str, dict[str, Any]] = {}
        for identifier, original in histories.items():
            assert sum(map(len, closed_groups(original))) == len(original)
            runtime.compaction.observe(identifier)
            await runtime.compaction.finish(identifier)
            snapshot = runtime.store.get("compaction", identifier)
            assert snapshot is not None and snapshot["kind"] == "extractive"
            prefix = [
                item
                for item in original
                if snapshot["from_seq"] < item["seq"] <= snapshot["covered_seq"]
            ]
            assert digest(prefix) == snapshot["prefix_digest"]
            projected = runtime.compaction.project(identifier)
            assert any(item.get("compaction_summary") for item in projected)
            assert not (covered[identifier] & {item["id"] for item in projected})
            assert runtime.store.messages(identifier) == original
            snapshots[identifier] = deepcopy(snapshot)

        instance_id = runtime.store.instance_id
        config_path, data_dir = runtime.config.path, runtime.data_dir
        await application.close()
        await runtime.close()
        runtime = Runtime(ConfigFile(config_path), data_dir, client_factory=factory)  # type: ignore[arg-type]
        assert runtime.store.instance_id == instance_id and runtime.main_session()["id"] == main_id
        for identifier, snapshot in snapshots.items():
            assert runtime.store.get("compaction", identifier) == snapshot
            assert runtime.store.messages(identifier) == histories[identifier]
        assert runtime.memory.source(source_id) == frozen_source
        assert runtime.memory.get(fact["id"]) == frozen_fact
        application = MemoryApplication(runtime, media_factory)
        observe_query_scope()
        await application.start()
        assert application._recovery is not None
        await asyncio.wait_for(asyncio.shield(application._recovery), timeout=4)
        phase = "recall"
        await submit("派新队友查询RIVER备份快照的既有原因和原始依据，不执行修改。")
        assert recall_child and scope_observations and all(all(pair) for pair in scope_observations)
        assert len(query_pages) == len(source_pages) == 1
        assert source_pages[0]["source_id"] == source_id
        assert source_pages[0]["content"] == frozen_source["content"]
        assert extracted_sources.count(source_id) == 1
        assert any(
            path == "/v1/embeddings" and reason in body["input"] for path, body in media_requests
        )
        assert any(
            path == "/v1/rerank" and reason in body["documents"] for path, body in media_requests
        )
    finally:
        if application is not None:
            await application.close()
        await runtime.close()
        await http.aclose()
