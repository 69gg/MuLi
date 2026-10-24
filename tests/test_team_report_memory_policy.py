"""Lifecycle policy over actual completed Team runs, not manufactured reports."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.media import MediaConfig, MediaService
from muli.memory_application import MemoryApplication
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("report memory policy must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


@asynccontextmanager
async def actual_runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    runtime, _ = make_runtime(tmp_path, [])
    reads = 0

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal reads
            if self.role == "worker":
                reads += 1
                if reads % 2:
                    turn = ModelTurn(
                        tool_calls=[call("read-policy-evidence", "fs_read", path=str(evidence))]
                    )
                else:
                    result = tool_result(messages, "read-policy-evidence")
                    assert set(result) == {"path", "content"}
                    assert result["path"] == str(evidence.resolve())
                    assert result["content"] == evidence.read_text(encoding="utf-8")
                    turn = ModelTurn(content=result["content"])
            else:
                assert self.role == "main"
                turn = ModelTurn(content="主会话已独立处理本轮输入。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Client(config.model)

    evidence = tmp_path / "report-policy-evidence.txt"
    if not evidence.exists():
        evidence.write_text("备份延期是由于校验清单缺少最近快照。", encoding="utf-8")
    runtime.client_factory = factory  # type: ignore[assignment]
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"enabled": True, "auto_extract": True},
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    await runtime.reconfigure()
    try:
        yield runtime
    finally:
        await runtime.close()


async def complete_report(runtime: Runtime, agent_id: str | None = None) -> dict[str, Any]:
    if agent_id is None:
        agent = await runtime.team.spawn("资料核查", "读取报告资料并说明原因，不修改文件。")
        agent_id = agent["id"]
    else:
        old_generation = runtime.team.get(agent_id)["generation"]
        sent = await runtime.team.send(agent_id, "重新读取资料并报告新的核查结果。")
        assert sent["accepted"] is True and sent["duplicate"] is False
        await eventually(runtime, lambda: runtime.team.get(agent_id)["generation"] > old_generation)

    def delivered() -> bool:
        agent = runtime.team.get(agent_id)
        report = runtime.store.get("team_report", agent.get("run_id", ""))
        return (
            agent["status"] == "completed"
            and runtime.team.idle
            and report is not None
            and report["status"] == "delivered"
        )

    await eventually(runtime, delivered)
    agent = runtime.team.get(agent_id)
    run = runtime.store.get("run", agent["run_id"])
    report = runtime.store.get("team_report", agent["run_id"])
    assert run is not None and report is not None
    assert run["status"] == report["execution_status"] == agent["status"] == "completed"
    assert run["session_id"] == report["agent_id"] == agent_id
    assert report["parent_id"] == runtime.main_session()["id"]
    assert run["result"] == json.loads(report["content"].split("\n", 1)[1])["result"]
    rows = runtime.store.db.execute(
        "SELECT data FROM events WHERE type='team.finished' AND session_id=? "
        "AND json_extract(data,'$.run_id')=? ORDER BY id LIMIT 2",
        (agent_id, run["id"]),
    ).fetchall()
    assert len(rows) == 1
    completion = json.loads(rows[0][0])
    assert completion["status"] == "completed"
    assert completion["agent_id"] == agent_id
    assert completion["generation"] == agent["generation"] > 0
    assert completion["completion_revision"] == agent["completion_revision"] > 0
    ingress = runtime.store.db.execute(
        "SELECT id FROM ingress WHERE session_id=? "
        "AND json_extract(payload,'$.source')=? "
        "AND json_extract(payload,'$.content')=? LIMIT 2",
        (report["parent_id"], "team:" + agent_id, report["content"]),
    ).fetchall()
    assert len(ingress) == 1
    return copy.deepcopy(report)


def application(runtime: Runtime, extracted: list[str]) -> MemoryApplication:
    app = MemoryApplication(runtime, lambda: MediaService(MediaConfig()))

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        extracted.append(source["id"])
        return {"memories": [], "aliases": {}}

    app.pipeline.extractor = extract
    return app


async def recover(app: MemoryApplication) -> None:
    await app.start()
    assert app._recovery is not None
    await asyncio.wait_for(asyncio.shield(app._recovery), timeout=4)


async def processed(app: MemoryApplication, source_id: str) -> dict[str, Any]:
    assert app.runtime.memory.source(source_id) is not None
    job = await asyncio.wait_for(app.pipeline.wait(source_id), timeout=3)
    assert job is not None and job["state"] == "done"
    return job


async def test_report_ingestion_error_does_not_strand_completed_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with actual_runtime(tmp_path) as runtime:
        app = application(runtime, [])
        attempts: list[str] = []
        original = app.pipeline.ingest

        def failing(message: Any, *, trusted_owner: bool = False) -> dict[str, Any]:
            if str(message.get("id", "")).startswith("team-report:"):
                attempts.append(message["id"])
                raise ValueError("controlled memory observer failure")
            return original(message, trusted_owner=trusted_owner)

        monkeypatch.setattr(app.pipeline, "ingest", failing)
        try:
            await recover(app)
            report = await complete_report(runtime)
            assert attempts == ["team-report:" + report["id"]]
            assert not runtime.team._active and runtime.team.idle
            assert runtime.memory.source(attempts[0]) is None
            # A further foreground run proves that notification/driver cleanup
            # completed; the report's completed state is not merely pre-commit.
            receipt = await runtime.submit(
                "继续处理主会话，不重新执行队友。", mode=runtime.main_session()["mode"]
            )
            await asyncio.wait_for(runtime.wait(receipt["run_id"]), timeout=4)
            assert runtime.store.get("team_report", report["id"])["status"] == "delivered"
        finally:
            await app.close()


async def test_registered_report_gap_is_repaired_after_reopen_without_reextracting_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted: list[str] = []
    async with actual_runtime(tmp_path) as runtime:
        report = await complete_report(runtime)
        source_id = "team-report:" + report["id"]
        app = application(runtime, extracted)
        original = runtime.memory.save
        interrupted = False

        def fail_after_source(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal interrupted
            if kwargs.get("source_id") == source_id:
                interrupted = True
                assert runtime.memory.source(source_id) is not None
                raise ValueError("controlled gap after durable source registration")
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runtime.memory, "save", fail_after_source)
            try:
                await recover(app)
                assert interrupted
                assert runtime.memory.source(source_id) is not None
                assert runtime.memory.automatic_episode_id(source_id) is None
            finally:
                await app.close()
    async with actual_runtime(tmp_path) as reopened:
        app = application(reopened, extracted)
        try:
            await recover(app)
            done = await processed(app, source_id)
            assert reopened.memory.automatic_episode_id(source_id) is not None
            assert extracted.count(source_id) == 1
            assert reopened.store.get("team_report", report["id"])["content"] == report["content"]
        finally:
            await app.close()
    async with actual_runtime(tmp_path) as reopened_again:
        app = application(reopened_again, extracted)
        try:
            await recover(app)
            assert await processed(app, source_id) == done
            assert extracted.count(source_id) == 1
        finally:
            await app.close()


async def test_two_real_generations_keep_historical_source_and_forget_survives_reopen(
    tmp_path: Path,
) -> None:
    extracted: list[str] = []
    async with actual_runtime(tmp_path) as runtime:
        first = await complete_report(runtime)
        (tmp_path / "report-policy-evidence.txt").write_text(
            "第二次核查发现清单已经补齐，但离线验证尚未完成。", encoding="utf-8"
        )
        second = await complete_report(runtime, first["agent_id"])
        assert first["id"] != second["id"]
        assert runtime.team.get(first["agent_id"])["generation"] == 2
        # Neither source has been admitted yet. Backfill must authenticate the
        # older report against its historical completion, not latest agent data.
        app = application(runtime, extracted)
        first_id, second_id = "team-report:" + first["id"], "team-report:" + second["id"]
        try:
            await recover(app)
            await processed(app, first_id)
            await processed(app, second_id)
            original = copy.deepcopy(runtime.memory.source(first_id))
            assert original is not None and original["content"] == first["content"]
            assert runtime.memory.source(second_id)["content"] == second["content"]
            assert original["content"] != second["content"]
            assert original["trusted_owner"] is False
            assert runtime.memory.forget_source(first_id)
            assert runtime.memory.source(first_id) is None
        finally:
            await app.close()
    async with actual_runtime(tmp_path) as reopened:
        app = application(reopened, extracted)
        try:
            await recover(app)
            await processed(app, second_id)
            assert reopened.memory.source(first_id) is None
            assert app.pipeline.job(first_id) is None
            assert (
                reopened.store.db.execute(
                    "SELECT 1 FROM memory_forgotten_sources WHERE id=?", (first_id,)
                ).fetchone()
                is not None
            )
            assert reopened.store.get("team_report", first["id"])["content"] == first["content"]
            assert extracted.count(first_id) == extracted.count(second_id) == 1
        finally:
            await app.close()


async def test_slow_report_extraction_allows_foreground_and_forget_blocks_late_result(
    tmp_path: Path,
) -> None:
    async with actual_runtime(tmp_path) as runtime:
        report = await complete_report(runtime)
        source_id = "team-report:" + report["id"]
        entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        app = application(runtime, [])

        async def slow(source: dict[str, Any]) -> dict[str, Any]:
            if source["id"] != source_id:
                return {"memories": [], "aliases": {}}
            entered.set()
            await release.wait()
            returned.set()
            return {
                "memories": [
                    {
                        "layer": "semantic",
                        "content": source["content"],
                        "quote": source["content"],
                        "claim_key": "backup.reason",
                    }
                ],
                "aliases": {},
            }

        app.pipeline.extractor = slow
        try:
            await recover(app)
            await asyncio.wait_for(entered.wait(), timeout=3)
            receipt = await runtime.submit("请独立回复新的主会话输入。", mode="yolo")
            await asyncio.wait_for(runtime.wait(receipt["run_id"]), timeout=4)
            assert not release.is_set()
            assert runtime.memory.forget_source(source_id)
            # Own the condition before releasing the extractor: wait() alone
            # would return immediately for a forgotten source and fake progress.
            async with app.pipeline.changed:
                release.set()
                await asyncio.wait_for(app.pipeline.changed.wait_for(returned.is_set), timeout=3)
            assert runtime.memory.source(source_id) is None
            assert app.pipeline.job(source_id) is None
            assert (
                runtime.store.db.execute(
                    "SELECT count(*) FROM memory_meta WHERE source_id=?", (source_id,)
                ).fetchone()[0]
                == 0
            )
            assert runtime.store.get("team_report", report["id"])["execution_status"] == "completed"
        finally:
            release.set()
            await app.close()
