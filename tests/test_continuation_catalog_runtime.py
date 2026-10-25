"""Discover an interrupted direct child after a real phone-driven close/reopen."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_continuity import attachment_settings, platform_reply
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


async def test_phone_owner_discovers_an_interrupted_direct_child_after_normal_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment = "请派一个队友读取本地核查资料；先核对，不修改文件，也不要发布结果。"
    question = "上次队友还有哪些没完成？先只核查原始回执，不要继续执行或重新派工。"
    acknowledgement = "队友已经接到只读核查任务，尚未完成。"
    explanation = "已核查上次队友的持久状态与可读证据；未继续执行，也未重新发送旧成果。"
    evidence_body = "合成核查记录：校验清单尚未包含最后一次快照，不能据此判断切换已完成。\n"
    evidence_path = tmp_path / "unfinished-observation.txt"
    evidence_path.write_bytes(evidence_body.encode("utf-8"))
    sent: list[dict[str, Any]] = []

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return platform_reply(request, sent)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=False))
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    await runtime.reconfigure()
    config_path, data_dir = runtime.config.path, runtime.data_dir
    assert config_path is not None
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    phase = "assignment"
    rounds = {"assignment": 0, "discovery": 0, "worker": 0}
    runs: dict[str, str] = {}
    child_id = child_session = child_run = accepted_id = ""
    generation = completion_revision = 0
    accepted_read: dict[str, Any] = {}
    catalog: dict[str, Any] = {}
    message_directory: dict[str, Any] = {}
    original_content: dict[str, Any] = {}
    schema_available = False
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal child_id, child_session, child_run, schema_available
            if self.role == "worker":
                rounds["worker"] += 1
                assert phase == "assignment" and rounds["worker"] <= 2
                if rounds["worker"] == 1:
                    turn = ModelTurn(
                        tool_calls=[call("read-before-close", "fs_read", path=str(evidence_path))]
                    )
                else:
                    accepted_read.update(tool_result(messages, "read-before-close"))
                    assert accepted_read == {
                        "path": str(evidence_path.resolve()),
                        "content": evidence_body,
                    }
                    sessions = [
                        identifier
                        for identifier, task in runtime.tasks.items()
                        if task is asyncio.current_task()
                    ]
                    assert len(sessions) == 1 and sessions[0] != main_id
                    child_session = sessions[0]
                    child_run = runtime.run_ids[child_session]
                    entered.set()
                    try:
                        await asyncio.wait_for(release.wait(), 4)
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise
                    turn = ModelTurn(content="只有测试释放屏障后才可能完成，正常关闭应先取消本轮。")
            else:
                assert self.role == "main"
                rounds[phase] += 1
                runs.setdefault(phase, runtime.run_ids[main_id])
                assert runs[phase] == runtime.run_ids[main_id]
                if phase == "assignment":
                    assert rounds[phase] <= 2
                    if rounds[phase] == 1:
                        incoming = next(
                            m
                            for m in messages
                            if m.role == "user" and m.content.endswith(assignment)
                        )
                        header, content = incoming.content.split("\n", 1)
                        assert header.startswith(
                            "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                        )
                        assert json.loads(header.split("：", 1)[1]) == {
                            "channel": "onebot",
                            "account_id": "123",
                            "sender_id": "7",
                            "target": "7",
                            "kind": "private",
                            "input_id": incoming.source_message_id,
                        }
                        assert content == assignment
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "delegate-read-only",
                                    "spawn_agent",
                                    task_name="未完成核查队友",
                                    prompt="只读取这份合成核查资料，不执行写入："
                                    + str(evidence_path),
                                )
                            ]
                        )
                    else:
                        spawned = tool_result(messages, "delegate-read-only")
                        assert "error" not in spawned
                        child_id, child_session = str(spawned["id"]), str(spawned["session_id"])
                        turn = ModelTurn(content=acknowledgement)
                else:
                    assert phase == "discovery" and rounds[phase] <= 4
                    if rounds[phase] == 1:
                        incoming = next(
                            m for m in messages if m.role == "user" and m.content.endswith(question)
                        )
                        header, content = incoming.content.split("\n", 1)
                        assert header.startswith(
                            "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                        )
                        assert json.loads(header.split("：", 1)[1]) == {
                            "channel": "onebot",
                            "account_id": "123",
                            "sender_id": "8",
                            "target": "8",
                            "kind": "private",
                            "input_id": incoming.source_message_id,
                        }
                        assert content == question
                        tool = next(
                            t for t in tools if t["function"]["name"] == "conversation_read"
                        )
                        schema_available = "section" in tool["function"]["parameters"]["properties"]
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "discover-unfinished",
                                    "conversation_read",
                                    section="continuations",
                                    session_id=main_id,
                                    limit=3,
                                )
                            ]
                        )
                    elif rounds[phase] == 2:
                        catalog.update(tool_result(messages, "discover-unfinished"))
                        if "error" in catalog:
                            # Finish the real phone turn before the outer missing-
                            # feature assertion. An error is never a success oracle.
                            turn = ModelTurn(content=explanation)
                        else:
                            matches = [item for item in catalog["items"] if item["id"] == child_id]
                            assert len(matches) == 1
                            turn = ModelTurn(
                                tool_calls=[
                                    call(
                                        "discover-original-receipt",
                                        "conversation_read",
                                        session_id=matches[0]["session_id"],
                                    )
                                ]
                            )
                    elif rounds[phase] == 3:
                        message_directory.update(tool_result(messages, "discover-original-receipt"))
                        assert "error" not in message_directory
                        assert message_directory["has_more"] is False
                        receipts = [
                            item for item in message_directory["records"] if item["role"] == "tool"
                        ]
                        assert len(receipts) == 1
                        assert receipts[0]["content_truncated"] is False
                        assert json.loads(receipts[0]["content"]) == accepted_read
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "read-original-receipt",
                                    "conversation_read",
                                    session_id=message_directory["session_id"],
                                    message_id=receipts[0]["id"],
                                )
                            ]
                        )
                    else:
                        original_content.update(tool_result(messages, "read-original-receipt"))
                        assert "error" not in original_content
                        assert original_content["has_more"] is False
                        assert json.loads(original_content["content"]) == accepted_read
                        turn = ModelTurn(content=explanation)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "worker", "reviewer"}
        return Client(config.model) if config.model != "reviewer" else FakeClient([], [])

    async def finish(text: str) -> dict[str, Any]:
        try:
            await eventually(runtime, lambda: bool(completed(runtime, text)))
        except TimeoutError:
            record = runtime.store.get("run", runs.get(phase, "")) or {}
            diagnostic = json.dumps(
                {
                    "phase": phase,
                    "rounds": rounds,
                    "status": record.get("status"),
                    "exception_type": (record.get("failure_diagnostic") or {}).get(
                        "exception_type"
                    ),
                    "worker_entered": entered.is_set(),
                    "worker_cancelled": cancelled.is_set(),
                },
                ensure_ascii=True,
            )
            assert len(diagnostic.encode("utf-8")) <= 1024
            pytest.fail(diagnostic)
        assert await asyncio.wait_for(runtime.wait(runs[phase]), 3) == text
        replies = completed(runtime, text)
        assert len(replies) == 1
        return replies[0]

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            changed = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert changed.status_code == 200 and changed.json()["mode"] == "yolo"
            assert (await phone(web, assignment, 26901)).status_code == 200
            reply = await finish(acknowledgement)
            await asyncio.wait_for(entered.wait(), 4)
            agent = runtime.team.get(child_id)
            generation, completion_revision = agent["generation"], agent["completion_revision"]
            assert agent["parent_id"] == main_id and agent["session_id"] == child_session
            assert agent["status"] == "running" and agent["run_id"] == child_run
            assert runtime.store.get("run", child_run)["status"] == "running"
            reads = [
                item
                for item in runtime.store.messages(child_session)
                if item["role"] == "tool" and item.get("tool_call_id") == "read-before-close"
            ]
            assert len(reads) == 1 and json.loads(reads[0]["content"]) == accepted_read
            accepted_id = reads[0]["id"]
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, reply["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == acknowledgement
            ]
            assert deliveries == []
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(
                item["id"] == reply["id"] and item["content"] == acknowledgement
                for item in history.json()["messages"]
            )
            assert not release.is_set() and not cancelled.is_set()
        assert cancelled.is_set(), "normal lifespan close must settle the real running worker"
    finally:
        release.set()
        await runtime.close()

    phase = "discovery"
    runtime = Runtime(ConfigFile(config_path), data_dir, client_factory=factory)  # type: ignore[arg-type]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                runtime.main_session()["id"] == main_id and runtime.store.instance_id == instance_id
            )
            agent = runtime.team.get(child_id)
            assert agent["status"] == "interrupted" and agent["run_id"] == child_run
            assert agent["generation"] == generation
            assert agent["completion_revision"] == completion_revision + 1
            assert runtime.store.get("run", child_run)["session_id"] == child_session
            assert runtime.store.get("run", child_run)["status"] == "cancelled"
            finished = [
                e
                for e in runtime.store.events(session_id=child_session)
                if e["type"] == "team.finished"
            ]
            assert len(finished) == 1
            assert finished[0]["data"]["run_id"] == child_run
            assert finished[0]["data"]["generation"] == generation
            assert finished[0]["data"]["status"] == "interrupted"
            assert runtime.store.get("message", accepted_id)["content"] == reads[0]["content"]
            before_sent = list(sent)
            assert (await phone(web, question, 26902, sender=8)).status_code == 200
            reply = await finish(explanation)
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, reply["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == explanation
            ]
            assert deliveries == [] and sent == before_sent
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(
                item["id"] == reply["id"] and item["content"] == explanation
                for item in history.json()["messages"]
            )
            assert len(runtime.team.list()) == 1 and rounds["worker"] == 2
            final_agent = runtime.team.get(child_id)
            assert final_agent["generation"] == generation
            assert final_agent["completion_revision"] == completion_revision + 1
            effects = [
                e
                for e in runtime.store.events(session_id=child_session)
                if e["type"] == "tool.started"
            ]
            assert len(effects) == 1 and effects[0]["data"]["call_id"] == "read-before-close"
            assert evidence_path.read_bytes() == evidence_body.encode("utf-8")
            # All real ingress, close/reopen, current Web reply and original
            # evidence preconditions precede this precise missing-schema target.
            assert schema_available, "conversation_read lacks the explicit continuations section"
            assert "error" not in catalog
            matches = [item for item in catalog["items"] if item["id"] == child_id]
            assert len(matches) == 1
            assert set(catalog) == {
                "items",
                "omitted",
                "next_cursor",
                "has_more",
                "consistency",
                "side_effect_status",
            }
            assert type(catalog["omitted"]) is int and catalog["omitted"] == 0
            assert catalog["has_more"] is False and catalog["next_cursor"] is None
            assert {
                key: matches[0][key] for key in ("kind", "id", "session_id", "run_id", "status")
            } == {
                "kind": "agent",
                "id": child_id,
                "session_id": child_session,
                "run_id": child_run,
                "status": "interrupted",
            }
            assert matches[0]["availability"] == "available"
            assert matches[0]["generation"] == final_agent["generation"]
            assert matches[0]["completion_revision"] == final_agent["completion_revision"]
            assert catalog["side_effect_status"] == "unknown" and catalog["consistency"] == "live"
            assert message_directory["session_id"] == child_session
            receipts = [item for item in message_directory["records"] if item["role"] == "tool"]
            assert len(receipts) == 1 and receipts[0]["id"] == accepted_id
            assert original_content["message_id"] == accepted_id
            assert json.loads(original_content["content"]) == accepted_read
    finally:
        release.set()
        await runtime.close()
