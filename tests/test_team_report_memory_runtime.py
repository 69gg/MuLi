"""Real completed Team reports must survive as untrusted memory sources."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.media import MediaConfig, MediaService
from muli.memory_application import MemoryApplication
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent


async def test_completed_direct_child_report_is_recovered_as_a_stable_memory_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Team report memory admission must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    application: MemoryApplication | None = None
    owner_text = "请派一个队友读取现有备份检查资料，说明原因；只核查，不修改、不发布。"
    reason = "夜间备份暂停的原因是校验清单缺少最近一次快照；应先补齐清单再验证。"
    evidence = tmp_path / "backup-observation.txt"
    evidence.write_bytes(reason.encode("utf-8"))
    main_rounds = 0
    worker_rounds = 0
    child_id = ""
    accepted_reads: list[dict[str, Any]] = []
    worker_diagnostic: dict[str, str] = {
        "phase": "not_entered",
        "exception_type": "none",
        "tool_error_type": "not_observed",
    }

    def failure_diagnostic(waited: dict[str, Any]) -> str:
        selected = next(
            (agent for agent in waited.get("agents", []) if agent.get("id") == child_id), {}
        )
        row = runtime.store.db.execute(
            "SELECT json_extract(data,'$.status') FROM objects WHERE kind='run' AND id=?",
            (selected.get("run_id", ""),),
        ).fetchone()
        statuses = {
            "queued",
            "running",
            "completed",
            "failed",
            "paused",
            "cancelled",
            "interrupted",
        }
        rendered = json.dumps(
            {
                "phase": "main_wait_result",
                "main_rounds": main_rounds,
                "worker_rounds": worker_rounds,
                "agent_status": selected.get("status")
                if selected.get("status") in statuses
                else "other",
                "run_status": row[0] if row is not None and row[0] in statuses else "other",
                "wait_timed_out": waited.get("timed_out") is True,
                "worker": worker_diagnostic,
            },
            separators=(",", ":"),
        )
        assert len(rendered.encode("utf-8")) <= 1024
        return rendered

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_rounds, worker_rounds, child_id
            if self.role == "worker":
                worker_rounds += 1
                if worker_rounds == 1:
                    worker_diagnostic["phase"] = "read_requested"
                    turn = ModelTurn(
                        tool_calls=[call("inspect-backup", "fs_read", path=str(evidence))]
                    )
                else:
                    try:
                        worker_diagnostic["phase"] = "read_reply"
                        assert worker_rounds == 2
                        result = tool_result(messages, "inspect-backup")
                        worker_diagnostic["tool_error_type"] = (
                            "none"
                            if "error" not in result
                            else "str"
                            if type(result["error"]) is str
                            else "other"
                        )
                        assert "error" not in result
                        worker_diagnostic["phase"] = "read_shape"
                        # Files.read returns these two exact keys, not the
                        # attachment reader's optional truncation metadata.
                        assert set(result) == {"path", "content"}
                        worker_diagnostic["phase"] = "read_content"
                        assert result["content"] == reason
                        worker_diagnostic["phase"] = "read_path"
                        assert result["path"] == str(evidence.resolve())
                        accepted_reads.append(result)
                        worker_diagnostic["phase"] = "report_ready"
                        turn = ModelTurn(content=reason)
                    except Exception as exc:
                        kind = type(exc).__name__
                        worker_diagnostic["exception_type"] = (
                            kind
                            if kind
                            in {
                                "AssertionError",
                                "KeyError",
                                "TypeError",
                                "StopIteration",
                                "JSONDecodeError",
                                "ValueError",
                            }
                            else "other"
                        )
                        raise
            else:
                assert self.role == "main"
                main_rounds += 1
                if main_rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "delegate-backup",
                                "spawn_agent",
                                task_name="备份核查队友",
                                prompt="读取指定资料并报告实际原因，不修改任何文件："
                                + str(evidence),
                            )
                        ]
                    )
                elif main_rounds == 2:
                    spawned = tool_result(messages, "delegate-backup")
                    assert "error" not in spawned
                    child_id = spawned["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call("await-backup", "wait_agents", targets=[child_id], timeout=2)
                        ]
                    )
                else:
                    if main_rounds == 3:
                        waited = tool_result(messages, "await-backup")
                        assert waited["timed_out"] is False, failure_diagnostic(waited)
                        assert any(
                            agent["id"] == child_id and agent["status"] == "completed"
                            for agent in waited["agents"]
                        ), failure_diagnostic(waited)
                    turn = ModelTurn(content="已收到队友核查结果；资料不是新的操作授权。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Client(config.model)

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        runtime.config.update(
            {
                "updates": {"check_on_start": False},
                "compaction": {"enabled": False},
                "attention": {"enabled": False},
                "memory": {"enabled": True, "auto_extract": False},
                "limits": {
                    "max_context_chars": 16000,
                    "max_output_chars": 6000,
                    "max_pending_runs": 3,
                },
                "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            }
        )
        await runtime.reconfigure()
        application = MemoryApplication(runtime, lambda: MediaService(MediaConfig()))
        receipt = await runtime.submit(owner_text, source="web", mode="yolo")
        await asyncio.wait_for(runtime.wait(receipt["run_id"]), timeout=4)
        assert child_id
        assert worker_rounds == 2 and len(accepted_reads) == 1
        assert reason not in owner_text
        agent = runtime.team.get(child_id)
        assert agent["parent_id"] == runtime.main_session()["id"]
        assert agent["status"] == "completed" and agent["generation"] == 1
        worker_run = runtime.store.get("run", agent["run_id"])
        report = runtime.store.get("team_report", agent["run_id"])
        assert worker_run is not None and report is not None
        assert worker_run["session_id"] == agent["session_id"] == child_id
        assert worker_run["status"] == report["execution_status"] == "completed"
        assert worker_run["result"] == agent["result"] == reason
        assert report["id"] == worker_run["id"] == agent["run_id"]
        assert report["agent_id"] == child_id
        assert report["parent_id"] == agent["parent_id"]
        assert reason == json.loads(report["content"].split("\n", 1)[1])["result"]
        rows = runtime.store.db.execute(
            "SELECT data,created_at FROM events WHERE type='team.finished' AND session_id=? "
            "AND json_extract(data,'$.agent_id')=? AND json_extract(data,'$.run_id')=? "
            "ORDER BY id LIMIT 2",
            (agent["session_id"], child_id, worker_run["id"]),
        ).fetchall()
        assert len(rows) == 1
        completion = json.loads(rows[0]["data"])
        assert completion["status"] == "completed"
        assert completion["generation"] == agent["generation"]
        assert completion["completion_revision"] == agent["completion_revision"] > 0

        # This first slice exercises actual startup backfill, not extraction or
        # natural model quality. No report/source/event is manufactured here.
        await application.start()
        assert application._recovery is not None
        await asyncio.wait_for(asyncio.shield(application._recovery), timeout=4)
        assert application.idle
        source = runtime.memory.source("team-report:" + worker_run["id"])
        assert source is not None, "真实完成报告在恢复结束后没有稳定的长期记忆来源"
        assert source["content"] == report["content"]
        assert source["trusted_owner"] is False
        assert source["role"] == "assistant"
        assert source["principal"] == "agent:" + child_id
        assert source["session_id"] == worker_run["session_id"]
        assert source["source"] == "team:" + child_id
        assert source["created_at"] == rows[0]["created_at"]
    finally:
        if application is not None:
            await application.close()
        await runtime.close()
