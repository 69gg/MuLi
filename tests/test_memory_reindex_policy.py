"""Reindex policy through real owner tools, extraction and media HTTP.

Configuration boundaries exercise ConfigFile + MemoryApplication lifecycle;
the separate continuity journey owns authenticated settings HTTP/reopen.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_runtime import FakeClient
from test_teammate_attachment_runtime import eventually

import muli.memory_pipeline as memory_pipeline
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.store import new_id, now


@dataclass
class IndexRig:
    base: MemoryRig
    fact_count: int
    extracted: list[str] = field(default_factory=list)
    requests: list[tuple[str, list[str]]] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    returned: asyncio.Event = field(default_factory=asyncio.Event)
    held: bool = False
    hold_model: str = ""
    command_calls: list[str] = field(default_factory=list)
    source_count: int = 0
    before_reindex: Callable[[], Awaitable[None]] | None = None

    async def configure(self, model: str, **memory: Any) -> None:
        self.base.runtime.config.update(
            {
                "models": {
                    "memory_extractor": {
                        "model": "index-policy-extractor",
                        "base_url": "https://unused.invalid/v1",
                    }
                },
                "media": {
                    "embedding": {
                        "model": model,
                        "base_url": "https://index.invalid/v1",
                        "dimensions": 2,
                    }
                },
                "memory": memory,
            }
        )
        await self.base.memory.reconfigure()
        recovery = self.base.memory._recovery
        assert recovery is not None
        await asyncio.wait_for(asyncio.shield(recovery), 4)

    async def http(
        self, _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "index.invalid" and request.url.path == "/v1/embeddings"
        payload = json.loads(request.content)
        assert payload["dimensions"] == 2 and payload["encoding_format"] == "float"
        values = payload["input"]
        assert isinstance(values, list) and values
        assert all(isinstance(value, str) for value in values)
        model = payload["model"]
        assert model in {"index-a", "index-b", "index-c"}
        self.requests.append((model, list(values)))
        if model == self.hold_model and any("REINDEX-EVIDENCE" in value for value in values):
            self.held = True
            self.entered.set()
            await self.release.wait()
            self.returned.set()
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [1.0, float(index + 1)]}
                    for index in reversed(range(len(values)))
                ]
            },
        )

    async def command(self, **arguments: Any) -> dict[str, Any]:
        previous = len(self.command_calls)
        receipt = await self.base.runtime.submit(
            "索引策略：" + json.dumps(arguments, ensure_ascii=False), mode="yolo"
        )
        await self.base.settle(receipt)
        assert len(self.command_calls) == previous + 1
        return self.base.result(self.command_calls[-1])

    async def source(self) -> tuple[str, list[str], dict[str, Any]]:
        self.source_count += 1
        lines = [
            f"REINDEX-EVIDENCE-{self.source_count}-{index}: replica {index} requires a checksum review."
            for index in range(self.fact_count)
        ]
        uploaded = await self.base.upload("\n".join(lines).encode("utf-8"))
        result = await self.base.import_attachment(uploaded["id"])
        assert "error" not in result
        source_id = result["source_id"]
        job = await asyncio.wait_for(self.base.memory.pipeline.wait(source_id), 3)
        assert job is not None and job["state"] == "done"
        assert self.extracted.count(source_id) == 1
        rows = self.base.runtime.store.db.execute(
            "SELECT memory_id FROM memory_meta WHERE source_id=? ORDER BY memory_id",
            (source_id,),
        ).fetchall()
        identifiers = [row[0] for row in rows]
        assert len(identifiers) == self.fact_count + 1
        assert self.base.runtime.memory.automatic_episode_id(source_id) in identifiers
        assert len(self.vector_keys(identifiers)) == len(identifiers)
        assert len(set(self.vector_keys(identifiers).values())) == 1
        return source_id, identifiers, job

    def vector_keys(self, identifiers: list[str]) -> dict[str, str]:
        rows = self.base.runtime.store.db.execute(
            "SELECT memory_id,model_key FROM memory_vectors WHERE memory_id IN ("
            + ",".join("?" for _ in identifiers)
            + ")",
            identifiers,
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.base.runtime.store.get("memory_index_plan", plan_id)
        assert plan is not None
        return plan

    async def terminal(self, plan_id: str) -> dict[str, Any]:
        await eventually(
            self.base.runtime,
            lambda: (
                self.plan(plan_id)["state"]
                in {
                    "done",
                    "failed",
                    "blocked",
                    "cancelled",
                }
            ),
        )
        result = await self.command(action="status", plan_id=plan_id)
        assert result["plan_id"] == plan_id
        return result

    async def start(self, source_id: str, key: str = "policy-reindex") -> dict[str, Any]:
        result = await self.command(action="start", source_ids=[source_id], key=key)
        assert "error" not in result and result["plan_id"]
        assert result["sources_total"] == 1
        return result

    def target_requests(self, model: str) -> list[list[str]]:
        return [
            values
            for name, values in self.requests
            if name == model and any("REINDEX-EVIDENCE" in value for value in values)
        ]


@asynccontextmanager
async def indexed_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fact_count: int | None = 2
) -> AsyncIterator[IndexRig]:
    async with actual_app(tmp_path) as base:
        count = (
            fact_count
            if fact_count is not None
            else base.runtime.config.settings.memory.max_facts_per_source
        )
        rig = IndexRig(base, count)

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def complete(
                self,
                messages: list[Message],
                tools: Any = (),
                *,
                attempt_guard: Callable[[], None] | None = None,
            ) -> ModelTurn:
                if self.role != "index-policy-extractor":
                    return await super().complete(messages, tools, attempt_guard=attempt_guard)
                if attempt_guard is not None:
                    attempt_guard()
                assert not tools
                source = json.loads(messages[-1].content)
                memories: list[dict[str, Any]] = []
                if source["id"].startswith("attachment:"):
                    assert source["trusted_owner"] is False
                    rig.extracted.append(source["id"])
                    lines = source["content"].splitlines()
                    assert len(lines) == rig.fact_count
                    memories = [
                        {
                            "layer": "semantic",
                            "content": line,
                            "quote": line,
                            "claim_key": f"{source['id']}.replica.policy.{index}",
                        }
                        for index, line in enumerate(lines)
                    ]
                return ModelTurn(content=json.dumps({"memories": memories, "aliases": {}}))

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                user_index = max(i for i, value in enumerate(messages) if value.role == "user")
                user = messages[user_index]
                if any(value.role == "tool" for value in messages[user_index + 1 :]):
                    turn = ModelTurn(content="本轮已结束，实际进度以工具回执为准。")
                elif user.content.startswith("收藏本附件："):
                    call_id = "index-import-" + new_id()
                    base.calls.append(call_id)
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                call_id,
                                "memory_import_attachment",
                                json.dumps({"id": user.content.removeprefix("收藏本附件：")}),
                            )
                        ]
                    )
                elif user.content.startswith("索引策略："):
                    assert "memory_reindex" in {item["function"]["name"] for item in tools}
                    # Settle ordinary foreground-source jobs before the first
                    # plan occupies its slot. Once the real HTTP barrier holds
                    # the sole worker, replay/cancel must remain foreground work.
                    if not rig.held or rig.release.is_set():
                        jobs = base.runtime.store.db.execute(
                            "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry') "
                            "ORDER BY source_id LIMIT ?",
                            (base.runtime.config.settings.memory.max_pending_jobs,),
                        ).fetchall()
                        for job in jobs:
                            result = await asyncio.wait_for(base.memory.pipeline.wait(job[0]), 3)
                            assert result is not None and result["state"] == "done"
                    if rig.before_reindex is not None:
                        await rig.before_reindex()
                    call_id = "index-policy-" + new_id()
                    rig.command_calls.append(call_id)
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                call_id, "memory_reindex", user.content.removeprefix("索引策略：")
                            )
                        ]
                    )
                else:
                    turn = ModelTurn(content="已处理新的主人输入。")
                yield StreamEvent("done", turn=turn)

        def factory(profile: ModelConfig) -> FakeClient:
            return Client(profile.model)

        async def transport(
            instance: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            return await rig.http(instance, request)

        base.runtime.client_factory = factory  # type: ignore[assignment]
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
        await rig.configure("index-a")
        try:
            yield rig
        finally:
            rig.release.set()


@pytest.mark.parametrize("change", ["cancel", "forget", "edit"])
async def test_inflight_response_cannot_write_after_real_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async with indexed_app(tmp_path, monkeypatch) as rig:
        source_id, identifiers, done = await rig.source()
        old_keys = rig.vector_keys(identifiers)
        await rig.configure("index-b")
        rig.hold_model = "index-b"
        plan = await rig.start(source_id)
        try:
            await asyncio.wait_for(rig.entered.wait(), 3)
            assert rig.held and rig.vector_keys(identifiers) == old_keys
            if change == "cancel":
                cancelled = await rig.command(action="cancel", plan_id=plan["plan_id"])
                assert cancelled["state"] == "cancelled"
            elif change == "forget":
                assert rig.base.runtime.memory.delete(identifiers[0])
                assert rig.base.runtime.memory.source(source_id) is None
            else:
                for identifier in identifiers:
                    previous = rig.base.runtime.memory.get(identifier)
                    assert previous is not None
                    corrected = rig.base.runtime.memory.edit(
                        identifier, content=previous["content"] + "\n主人明确更正了这一条。"
                    )
                    assert corrected["version"] > previous["version"]
        finally:
            rig.release.set()
        await asyncio.wait_for(rig.returned.wait(), 3)
        status = await rig.terminal(plan["plan_id"])
        assert status["vectors_written"] == 0
        assert all(key != plan["model_key"] for key in rig.vector_keys(identifiers).values())
        if change == "cancel":
            assert rig.base.memory.pipeline.job(source_id) == done
        elif change == "forget":
            assert not rig.vector_keys(identifiers)
            assert rig.base.runtime.memory.source(source_id) is None
        else:
            assert rig.base.memory.pipeline.job(source_id) == done


async def test_actual_model_b_c_b_reconfiguration_cannot_revive_old_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch) as rig:
        source_id, identifiers, done = await rig.source()
        await rig.configure("index-b")
        rig.hold_model = "index-b"
        plan = await rig.start(source_id)
        try:
            await asyncio.wait_for(rig.entered.wait(), 3)
            old_pipeline = rig.base.memory.pipeline
            await rig.configure("index-c")
            assert rig.base.memory.pipeline is not old_pipeline
            assert rig.plan(plan["plan_id"])["state"] == "blocked"
            await rig.configure("index-b")
        finally:
            rig.release.set()
        status = await rig.terminal(plan["plan_id"])
        assert status["state"] == "blocked" and status["last_error"] == "embedding_changed"
        replay = await rig.start(source_id)
        assert replay["plan_id"] == plan["plan_id"] and replay["state"] == "blocked"
        assert len(rig.target_requests("index-b")) == 1
        assert not rig.target_requests("index-c")
        assert all(key != plan["model_key"] for key in rig.vector_keys(identifiers).values())
        assert rig.base.memory.pipeline.job(source_id) == done


async def test_real_vector_commit_prefix_survives_later_sqlite_failure_without_finished_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch) as rig:
        source_id, identifiers, done = await rig.source()
        await rig.configure("index-b", max_attempts=1)
        original = rig.base.runtime.memory.store_vector
        committed: list[str] = []
        failures: list[sqlite3.IntegrityError] = []
        target_key = ""

        def write(
            identifier: str,
            vector: list[float],
            model_key: str,
            *,
            expected_version: int | None = None,
        ) -> bool:
            nonlocal target_key
            if identifier not in identifiers:
                return original(identifier, vector, model_key, expected_version=expected_version)
            target_key = model_key
            if committed:
                # The first call really committed. Fail the next real UPDATE,
                # rather than replacing store_vector with a fictitious result.
                with rig.base.runtime.store.db:
                    rig.base.runtime.store.db.execute(
                        "CREATE TRIGGER reject_next_index BEFORE UPDATE ON memory_vectors "
                        "WHEN NEW.model_key<>OLD.model_key "
                        "BEGIN SELECT RAISE(ABORT,'controlled vector interruption'); END"
                    )
            try:
                result = original(identifier, vector, model_key, expected_version=expected_version)
            except sqlite3.IntegrityError as error:
                failures.append(error)
                raise
            assert result is True
            committed.append(identifier)
            database = next(
                row[2]
                for row in rig.base.runtime.store.db.execute("PRAGMA database_list")
                if row[1] == "main"
            )
            with sqlite3.connect(database) as independent:
                assert independent.execute(
                    "SELECT model_key FROM memory_vectors WHERE memory_id=?", (identifier,)
                ).fetchone() == (model_key,)
            return result

        monkeypatch.setattr(rig.base.runtime.memory, "store_vector", write)
        try:
            plan = await rig.start(source_id)
            status = await rig.terminal(plan["plan_id"])
            assert len(committed) == 1 and len(failures) == 1 and target_key == plan["model_key"]
            assert status["state"] == "failed"
            assert status["vectors_written"] == 1 and status["sources_finished"] == 0
            keys = rig.vector_keys(identifiers)
            assert [
                identifier for identifier, key in keys.items() if key == target_key
            ] == committed
            assert rig.base.memory.pipeline.job(source_id) == done
        finally:
            with rig.base.runtime.store.db:
                rig.base.runtime.store.db.execute("DROP TRIGGER IF EXISTS reject_next_index")


async def test_full_pending_capacity_rejects_new_plan_but_replays_exact_existing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch) as rig:
        source_id, identifiers, done = await rig.source()
        await rig.configure("index-b", max_pending_jobs=1)
        rig.hold_model = "index-b"
        plan = await rig.start(source_id)
        try:
            await asyncio.wait_for(rig.entered.wait(), 3)
            exact = await rig.start(source_id)
            assert exact["plan_id"] == plan["plan_id"]
            refused = await rig.command(action="start", source_ids=[source_id], key="another-plan")
            assert "error" in refused
            assert rig.base.memory.pipeline.status()["max_pending_jobs"] == 1
            pending = rig.base.runtime.store.db.execute(
                "SELECT count(*) FROM memory_jobs WHERE state IN ('queued','processing','retry')"
            ).fetchone()[0]
            # The one real processing plan itself owns the only pending slot.
            # New owner sources may defer, but cannot create a second job slot.
            assert pending + 1 == rig.base.runtime.config.settings.memory.max_pending_jobs
            assert len(rig.target_requests("index-b")) == 1
            assert rig.plan(plan["plan_id"])["state"] == "processing"
            assert all(key != plan["model_key"] for key in rig.vector_keys(identifiers).values())
            assert rig.base.memory.pipeline.job(source_id) == done
        finally:
            rig.release.set()
        assert (await rig.terminal(plan["plan_id"]))["state"] == "done"


async def test_same_key_with_different_actual_source_cannot_change_existing_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch) as rig:
        first, _, first_done = await rig.source()
        second, _, second_done = await rig.source()
        await rig.configure("index-b")
        rig.hold_model = "index-b"
        plan = await rig.start(first)
        try:
            await asyncio.wait_for(rig.entered.wait(), 3)
            before = rig.plan(plan["plan_id"])
            refused = await rig.command(action="start", source_ids=[second], key="policy-reindex")
            assert "error" in refused
            assert rig.plan(plan["plan_id"]) == before
            assert rig.base.memory.pipeline.job(first) == first_done
            assert rig.base.memory.pipeline.job(second) == second_done
        finally:
            rig.release.set()
        assert (await rig.terminal(plan["plan_id"]))["state"] == "done"


async def test_episode_plus_maximum_extracted_facts_all_receive_actual_new_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use the configured source limit, not a second independent batch budget.
    async with indexed_app(tmp_path, monkeypatch, fact_count=None) as rig:
        assert rig.fact_count == rig.base.runtime.config.settings.memory.max_facts_per_source
        source_id, identifiers, done = await rig.source()
        extracted = list(rig.extracted)
        await rig.configure("index-b")
        plan = await rig.start(source_id)
        status = await rig.terminal(plan["plan_id"])
        assert status["state"] == "done" and status["sources_finished"] == 1
        assert status["vectors_written"] == len(identifiers)
        assert rig.vector_keys(identifiers) == {
            identifier: plan["model_key"] for identifier in identifiers
        }
        assert len(rig.target_requests("index-b")) >= 2
        assert all(len(values) <= rig.fact_count for values in rig.target_requests("index-b"))
        assert rig.extracted == extracted
        assert rig.base.memory.pipeline.job(source_id) == done


@pytest.mark.parametrize("change", ["forget", "edit"])
async def test_waiting_for_real_query_media_lock_rechecks_evidence_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async with indexed_app(tmp_path, monkeypatch) as rig:
        source_id, identifiers, done = await rig.source()
        await rig.configure("index-b")
        pipeline = rig.base.memory.pipeline
        tool_ready, tool_release = asyncio.Event(), asyncio.Event()
        query_entered, query_release = asyncio.Event(), asyncio.Event()
        batch_waiting = asyncio.Event()
        query = "独立查询只用于占用真实媒体锁"
        original_http = httpx.AsyncHTTPTransport.handle_async_request
        original_acquire = pipeline._media_lock.acquire

        async def before_tool() -> None:
            # The new owner's ordinary memory job has actually completed here;
            # it cannot get ahead of the plan and wait on this same media lock.
            tool_ready.set()
            await tool_release.wait()

        async def http(
            transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            payload = json.loads(request.content)
            if payload["input"] == [query]:
                query_entered.set()
                await query_release.wait()
            return await original_http(transport, request)

        async def acquire() -> bool:
            if pipeline._media_lock.locked() and asyncio.current_task() is pipeline._worker:
                batch_waiting.set()
            return await original_acquire()

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", http)
        monkeypatch.setattr(pipeline._media_lock, "acquire", acquire)
        rig.before_reindex = before_tool
        start = asyncio.create_task(rig.start(source_id))
        query_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            await asyncio.wait_for(tool_ready.wait(), 3)
            query_task = asyncio.create_task(pipeline.query(query, limit=3, hops=0))
            await asyncio.wait_for(query_entered.wait(), 3)
            assert pipeline._media_lock.locked()
            tool_release.set()
            plan = await asyncio.wait_for(asyncio.shield(start), 4)
            rig.before_reindex = None
            await asyncio.wait_for(batch_waiting.wait(), 3)
            assert not query_task.done() and pipeline._media_lock.locked()
            assert not rig.target_requests("index-b")
            if change == "forget":
                assert rig.base.runtime.memory.delete(identifiers[0])
                assert rig.base.runtime.memory.source(source_id) is None
            else:
                for identifier in identifiers:
                    previous = rig.base.runtime.memory.get(identifier)
                    assert previous is not None
                    current = rig.base.runtime.memory.edit(
                        identifier, content=previous["content"] + "\n真实更正使原版本失效。"
                    )
                    assert current["version"] > previous["version"]
            query_release.set()
            await asyncio.wait_for(asyncio.shield(query_task), 3)
            status = await rig.terminal(plan["plan_id"])
            assert status["vectors_written"] == 0
            assert not rig.target_requests("index-b"), (
                "等待真实媒体锁时已失效的正文不能在取得锁后发往 embedding"
            )
            if change == "edit":
                assert pipeline.job(source_id) == done
        finally:
            rig.before_reindex = None
            tool_release.set()
            query_release.set()
            tasks = [start] if query_task is None else [start, query_task]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_legitimate_near_limit_plan_cannot_kill_shared_worker_on_state_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch, fact_count=None) as rig:
        # Five small, real uploaded sources provide enough immutable metadata
        # for the existing configuration's minimum output budget. No trial loop
        # or manufactured plan/checkpoint is needed to tune the boundary.
        sources = [await rig.source() for _ in range(5)]
        source_ids = [source_id for source_id, _, _ in sources]
        timestamp = now()
        monkeypatch.setattr(memory_pipeline, "now", lambda: timestamp)
        await rig.configure("index-b")
        rig.hold_model = "index-b"
        sample = await rig.command(action="start", source_ids=source_ids, key="measure-real-plan")
        assert "error" not in sample and sample["sources_total"] == len(sources)
        try:
            await asyncio.wait_for(rig.entered.wait(), 3)
            observed = rig.plan(sample["plan_id"])
            assert observed["state"] == "processing" and observed["attempts"] == 1
        finally:
            rig.release.set()
        assert (await rig.terminal(sample["plan_id"]))["state"] == "done"

        # Derive the queued byte size from a genuinely created record. Nothing
        # derived here is persisted: the second plan is created by the real tool.
        queued = {**observed, "state": "queued", "attempts": 0}
        queued_bytes = len(json.dumps(queued, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        output_chars = (queued_bytes + 3) // 4
        assert 1000 <= output_chars < rig.base.runtime.config.settings.limits.max_output_chars
        budget = output_chars * 4
        assert queued_bytes <= budget < queued_bytes + len("processing") - len("queued")
        current = rig.plan(sample["plan_id"])
        assert len(json.dumps(current, ensure_ascii=False).encode("utf-8")) <= budget
        for source_id, _, done in sources:
            assert rig.base.memory.pipeline.job(source_id) == done
            assert len(json.dumps(done["checkpoint"], ensure_ascii=False).encode("utf-8")) <= budget
        rig.base.runtime.config.update({"limits": {"max_output_chars": output_chars}})
        await rig.configure("index-b")
        pipeline = rig.base.memory.pipeline
        worker = pipeline._worker
        assert worker is not None and not worker.done()
        waiting: asyncio.Task[None] | None = None
        try:
            result = await rig.command(
                action="start", source_ids=source_ids, key="new-near-limit-plan"
            )
            if "error" not in result:
                waiting = asyncio.create_task(
                    eventually(
                        rig.base.runtime,
                        lambda: (
                            rig.plan(result["plan_id"])["state"]
                            in {"done", "failed", "blocked", "cancelled"}
                        ),
                    )
                )
                finished, _ = await asyncio.wait(
                    {worker, waiting}, timeout=3, return_when=asyncio.FIRST_COMPLETED
                )
                assert finished, "接纳的有限计划必须能收口，不能卡住唯一 worker"
            else:
                assert result.get("code") == "output_budget"
            error = worker.exception() if worker.done() and not worker.cancelled() else None
            assert not worker.done(), "合法近限计划的状态增长不能终止共享 worker：" + (
                type(error).__name__ if error is not None else "stopped"
            )
            # Whole-plan rejection for insufficient progress headroom is valid;
            # either path must leave ordinary owner ingestion operational.
            receipt = await rig.base.runtime.submit("近限索引之后前台与后台仍可继续。", mode="yolo")
            await rig.base.settle(receipt)
            jobs = rig.base.runtime.store.db.execute(
                "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry')"
            ).fetchall()
            for job in jobs:
                completed = await asyncio.wait_for(pipeline.wait(job[0]), 3)
                assert completed is not None and completed["state"] == "done"
            assert not worker.done()
        finally:
            if waiting is not None:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            # Retrieve and settle exactly this owned failed task on the red
            # implementation, so ASGI teardown does not replace the oracle.
            if worker.done() and not worker.cancelled() and worker.exception() is not None:
                failure = worker.exception()
                try:
                    await pipeline.close()
                except Exception as closing:
                    assert closing is failure


async def test_active_plan_survives_same_model_reopen_with_smaller_progress_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch, fact_count=None) as rig:
        sources = [await rig.source() for _ in range(5)]
        source_ids = [source_id for source_id, _, _ in sources]
        timestamp = now()
        monkeypatch.setattr(memory_pipeline, "now", lambda: timestamp)
        await rig.configure("index-b", max_attempts=1, retry_backoff_seconds=0)
        rig.hold_model = "index-b"
        started = await rig.command(
            action="start", source_ids=source_ids, key="resume-smaller-budget"
        )
        assert "error" not in started and started["sources_total"] == len(sources)
        await asyncio.wait_for(rig.entered.wait(), 3)
        old_pipeline = rig.base.memory.pipeline
        observed = rig.plan(started["plan_id"])
        assert observed["state"] == "processing" and observed["attempts"] == 1
        assert observed["last_error"] == ""

        # Close the real in-flight worker rather than forging a resumable record.
        # The model key remains unchanged throughout the budget-only reconfigure.
        await rig.base.memory.close()
        assert old_pipeline._worker is None
        assert rig.plan(started["plan_id"]) == observed
        actual_bytes = len(
            json.dumps(observed, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        output_chars = (actual_bytes + 3) // 4
        assert 1000 <= output_chars < rig.base.runtime.config.settings.limits.max_output_chars
        budget = output_chars * 4
        assert actual_bytes <= budget < actual_bytes + 4
        failed_projection = {**observed, "state": "failed", "last_error": "index_failed"}
        assert len(json.dumps(failed_projection, ensure_ascii=False).encode("utf-8")) > budget
        for source_id, identifiers, done in sources:
            assert old_pipeline.job(source_id) == done
            assert len(json.dumps(done["checkpoint"], ensure_ascii=False).encode("utf-8")) <= budget
            assert all(
                key != observed["model_key"] for key in rig.vector_keys(identifiers).values()
            )

        rig.hold_model = ""
        rig.release.set()
        original_http = rig.http

        async def fail_resumed_embedding(
            instance: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            response = await original_http(instance, request)
            payload = json.loads(request.content)
            if payload["model"] == "index-b" and any(
                "REINDEX-EVIDENCE" in value for value in payload["input"]
            ):
                return httpx.Response(503, json={"error": "controlled embedding failure"})
            return response

        monkeypatch.setattr(
            httpx.AsyncHTTPTransport, "handle_async_request", fail_resumed_embedding
        )
        rig.base.runtime.config.update({"limits": {"max_output_chars": output_chars}})
        await rig.configure("index-b", max_attempts=1, retry_backoff_seconds=0)
        pipeline = rig.base.memory.pipeline
        worker = pipeline._worker
        assert pipeline is not old_pipeline and worker is not None
        waiting = asyncio.create_task(
            eventually(
                rig.base.runtime,
                lambda: rig.plan(started["plan_id"])["state"] in {"failed", "blocked", "cancelled"},
            )
        )
        try:
            finished, _ = await asyncio.wait(
                {worker, waiting}, timeout=3, return_when=asyncio.FIRST_COMPLETED
            )
            assert finished, "缩小预算后的既有计划必须受控收口，不能永久占用活动份额"
            error = worker.exception() if worker.done() and not worker.cancelled() else None
            assert not worker.done(), "恢复既有计划后错误字段增长不能终止共享 worker：" + (
                type(error).__name__ if error is not None else "stopped"
            )
            resumed = rig.plan(started["plan_id"])
            assert resumed["state"] in {"failed", "blocked", "cancelled"}
            assert resumed["id"] == observed["id"] and resumed["sources"] == observed["sources"]
            assert resumed["model_key"] == observed["model_key"]
            assert resumed["vectors_written"] == 0 and resumed["sources_finished"] == 0
            for source_id, identifiers, done in sources:
                assert pipeline.job(source_id) == done
                assert all(
                    key != observed["model_key"] for key in rig.vector_keys(identifiers).values()
                )

            receipt = await rig.base.runtime.submit("缩小预算恢复后普通记忆仍可处理。", mode="yolo")
            await rig.base.settle(receipt)
            jobs = rig.base.runtime.store.db.execute(
                "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry')"
            ).fetchall()
            for job in jobs:
                completed = await asyncio.wait_for(pipeline.wait(job[0]), 3)
                assert completed is not None and completed["state"] == "done"
            assert not worker.done()
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            if worker.done() and not worker.cancelled() and worker.exception() is not None:
                failure = worker.exception()
                try:
                    await pipeline.close()
                except Exception as closing:
                    assert closing is failure


async def test_settings_patch_rejects_unreadable_active_plan_before_closing_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch, fact_count=None) as rig:
        sources = [await rig.source() for _ in range(5)]
        await rig.configure("index-b")
        rig.hold_model = "index-b"
        cancelled = asyncio.Event()
        original_transport = httpx.AsyncHTTPTransport.handle_async_request

        async def observe_actual_http_cancellation(
            instance: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            try:
                return await original_transport(instance, request)
            except asyncio.CancelledError:
                payload = json.loads(request.content)
                if payload["model"] == "index-b" and any(
                    "REINDEX-EVIDENCE" in value for value in payload["input"]
                ):
                    cancelled.set()
                raise

        monkeypatch.setattr(
            httpx.AsyncHTTPTransport, "handle_async_request", observe_actual_http_cancellation
        )
        started = await rig.command(
            action="start",
            source_ids=[source_id for source_id, _, _ in sources],
            key="reject-unreadable-existing-plan",
        )
        assert "error" not in started and started["sources_total"] == len(sources)
        try:
            await asyncio.wait_for(rig.entered.wait(), 3)
            observed = rig.plan(started["plan_id"])
            assert observed["state"] == "processing" and observed["vectors_written"] == 0
            actual_bytes = len(
                json.dumps(observed, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
            output_chars = (actual_bytes - 1) // 4
            config = rig.base.runtime.config
            assert 1000 <= output_chars < config.settings.limits.max_output_chars
            assert output_chars * 4 < actual_bytes
            config_bytes = config.path.read_bytes()
            old_settings = config.settings.model_dump()
            pipeline = rig.base.memory.pipeline
            worker = pipeline._worker
            assert worker is not None and not worker.done()
            request_count = len(rig.target_requests("index-b"))
            assert request_count == 1 and not cancelled.is_set() and not rig.returned.is_set()
            old_keys = [rig.vector_keys(identifiers) for _, identifiers, _ in sources]

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=rig.base.app),
                base_url="http://localhost:8765",
            ) as web:
                response = await web.patch(
                    "/api/settings", json={"limits": {"max_output_chars": output_chars}}
                )

            assert response.status_code == 400
            assert response.json()["detail"]["code"] == "output_budget"
            assert config.path.read_bytes() == config_bytes
            assert config.settings.model_dump() == old_settings
            same_pipeline = rig.base.memory.pipeline is pipeline
            same_worker = pipeline._worker is worker
            assert same_pipeline, "拒绝配置必须保留原管线实例"
            assert same_worker and not worker.done()
            assert not cancelled.is_set() and not rig.returned.is_set()
            assert len(rig.target_requests("index-b")) == request_count
            assert rig.plan(started["plan_id"]) == observed
            for (source_id, identifiers, done), keys in zip(sources, old_keys, strict=True):
                assert pipeline.job(source_id) == done
                assert rig.vector_keys(identifiers) == keys
        finally:
            rig.release.set()


async def test_model_change_and_near_limit_budget_are_preflighted_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with indexed_app(tmp_path, monkeypatch, fact_count=None) as rig:
        sources = [await rig.source() for _ in range(5)]
        await rig.configure("index-b")
        rig.hold_model = "index-b"
        cancelled = asyncio.Event()
        original_transport = httpx.AsyncHTTPTransport.handle_async_request

        async def observe_actual_http_cancellation(
            instance: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            try:
                return await original_transport(instance, request)
            except asyncio.CancelledError:
                payload = json.loads(request.content)
                if payload["model"] == "index-b" and any(
                    "REINDEX-EVIDENCE" in value for value in payload["input"]
                ):
                    cancelled.set()
                raise

        monkeypatch.setattr(
            httpx.AsyncHTTPTransport, "handle_async_request", observe_actual_http_cancellation
        )
        started = await rig.command(
            action="start",
            source_ids=[source_id for source_id, _, _ in sources],
            key="combined-model-and-budget-change",
        )
        assert "error" not in started and started["sources_total"] == len(sources)
        try:
            await asyncio.wait_for(rig.entered.wait(), 3)
            observed = rig.plan(started["plan_id"])
            assert observed["state"] == "processing" and observed["last_error"] == ""
            actual_bytes = len(
                json.dumps(observed, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
            output_chars = (actual_bytes + 3) // 4
            config = rig.base.runtime.config
            assert 1000 <= output_chars < config.settings.limits.max_output_chars
            budget = output_chars * 4
            assert actual_bytes <= budget < actual_bytes + 4
            model_change_projection = {
                **observed,
                "state": "blocked",
                "last_error": "embedding_changed",
            }
            assert (
                len(json.dumps(model_change_projection, ensure_ascii=False).encode("utf-8"))
                > budget
            )
            assert config.settings.media.embedding is not None
            assert config.settings.media.embedding.model == "index-b"
            config_bytes = config.path.read_bytes()
            old_settings = config.settings.model_dump()
            pipeline = rig.base.memory.pipeline
            worker = pipeline._worker
            assert worker is not None and not worker.done()
            old_keys = [rig.vector_keys(identifiers) for _, identifiers, _ in sources]
            request_count = len(rig.target_requests("index-b"))
            assert request_count == 1 and not cancelled.is_set()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=rig.base.app),
                base_url="http://localhost:8765",
            ) as web:
                response = await web.patch(
                    "/api/settings",
                    json={
                        "limits": {"max_output_chars": output_chars},
                        "media": {"embedding": {"model": "index-c"}},
                    },
                )

            assert response.status_code in {200, 400}
            if response.status_code == 400:
                assert response.json()["detail"]["code"] == "output_budget"
                assert config.path.read_bytes() == config_bytes
                assert config.settings.model_dump() == old_settings
                same_pipeline = rig.base.memory.pipeline is pipeline
                same_worker = pipeline._worker is worker
                assert same_pipeline
                assert same_worker
                assert not worker.done()
                assert not cancelled.is_set() and not rig.returned.is_set()
                assert rig.plan(started["plan_id"]) == observed
            else:
                assert config.settings.media.embedding is not None
                assert config.settings.media.embedding.model == "index-c"
                assert config.settings.limits.max_output_chars == output_chars
                current = rig.base.memory.pipeline
                new_pipeline = current is not pipeline
                assert new_pipeline
                recovery = rig.base.memory._recovery
                assert recovery is not None
                await asyncio.wait_for(asyncio.shield(recovery), 4)
                assert current._worker is not None and not current._worker.done()
                blocked = rig.plan(started["plan_id"])
                assert blocked["state"] == "blocked"
                assert blocked["id"] == observed["id"] and blocked["sources"] == observed["sources"]
                assert blocked["model_key"] == observed["model_key"]
                assert blocked["vectors_written"] == observed["vectors_written"] == 0
                assert blocked["sources_finished"] == observed["sources_finished"] == 0
                receipt = await rig.base.runtime.submit(
                    "换模型并缩预算后普通记忆仍可处理。", mode="yolo"
                )
                await rig.base.settle(receipt)
                jobs = rig.base.runtime.store.db.execute(
                    "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry')"
                ).fetchall()
                for job in jobs:
                    completed = await asyncio.wait_for(current.wait(job[0]), 3)
                    assert completed is not None and completed["state"] == "done"
                assert not current._worker.done()

            assert len(rig.target_requests("index-b")) == request_count
            assert not rig.target_requests("index-c")
            for (source_id, identifiers, done), keys in zip(sources, old_keys, strict=True):
                assert rig.base.memory.pipeline.job(source_id) == done
                assert rig.vector_keys(identifiers) == keys
        finally:
            rig.release.set()
