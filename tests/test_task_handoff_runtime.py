"""Offline handoff journeys through real Runtime, task control and durable tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channels import terminal
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import completed, eventually, tool_result

from muli.channels import ChannelManager
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.server import create_app


def call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("handoff旅程只允许显式HTTP MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


async def test_owner_replans_failed_unknown_delivery_and_hands_original_evidence_to_draft_worker(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "base_url": "https://platform.invalid",
                    "owner_ids": ["7"],
                }
            },
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime=runtime)
    original_manager = app.state.channel_application.manager
    phase = "initial"
    main_round = old_round = draft_round = 0
    task_id = attachment_id = delivery_id = ""
    old_id = ""
    evidence_seq = 0
    evidence_revision = ""
    handoff_before: dict[str, Any] = {}
    handoff_after: dict[str, Any] = {}
    body_marker = "HANDOFF-ORIGINAL-SOURCE-ONLY"
    corrected = "发布限制：现在只交草稿，不得发布"
    requests: list[httpx.Request] = []
    worker_requests: list[list[Message]] = []
    replacement_started = asyncio.Event()
    draft_release = asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round, task_id, old_id, handoff_before, handoff_after
            nonlocal evidence_seq, evidence_revision
            main_round += 1
            if phase == "initial":
                if main_round == 1:
                    assert body_marker not in str(messages)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save",
                                "commitment_save",
                                title="整理并发布资料",
                                brief={
                                    "goal": "整理资料并发布给绑定主人",
                                    "constraints": ["保留证据，不把排队当送达"],
                                    "evidence_refs": [{"kind": "attachment", "id": attachment_id}],
                                },
                            )
                        ]
                    )
                elif main_round == 2:
                    task_id = tool_result(messages, "save")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-old",
                                "spawn_agent",
                                task_name="原执行者",
                                prompt="整理资料并向绑定主人发布；保持同一逻辑发送键",
                                commitment_id=task_id,
                                attachment_ids=[attachment_id],
                            )
                        ]
                    )
                elif main_round == 3:
                    old_id = tool_result(messages, "spawn-old")["id"]
                    turn = ModelTurn(
                        tool_calls=[call("wait-old", "wait_agents", targets=[old_id], timeout=10)]
                    )
                else:
                    turn = ModelTurn(content="旧执行者失败；已有外发结果未知，暂不重试")
            elif phase == "replan":
                if main_round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call("brief", "commitment_read", identifier=task_id),
                            call("corrected-memory", "memory_query", query="发布限制"),
                            call(
                                "handoff-before",
                                "commitment_inspect",
                                identifier=task_id,
                                section="handoff",
                                agent_id=old_id,
                            ),
                        ]
                    )
                elif main_round == 2:
                    assert corrected in str(tool_result(messages, "corrected-memory"))
                    handoff_before = tool_result(messages, "handoff-before")
                    assert "error" not in handoff_before
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "approve",
                                "task_approve_plan",
                                observed_epoch=2,
                                plan="仅核查已保存资料与未知投递，交草稿；不得重放或再次发布",
                                targets=[],
                            )
                        ]
                    )
                elif main_round == 3:
                    approved = tool_result(messages, "approve")
                    assert approved["current"]["control"]["approved_epoch"] == 2
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "handoff-stale",
                                "commitment_inspect",
                                identifier=task_id,
                                section="handoff",
                                agent_id=old_id,
                                revision=handoff_before["revision"],
                            ),
                            call(
                                "handoff-fresh",
                                "commitment_inspect",
                                identifier=task_id,
                                section="handoff",
                                agent_id=old_id,
                            ),
                        ]
                    )
                elif main_round == 4:
                    assert tool_result(messages, "handoff-stale")["code"] == "evidence_changed"
                    handoff_after = tool_result(messages, "handoff-fresh")
                    assert "error" not in handoff_after
                    assert handoff_after["revision"] != handoff_before["revision"]
                    assert handoff_after["epoch"] == handoff_after["approved_epoch"] == 2
                    assert handoff_after["memory_revision"] == runtime.memory.revision
                    assert handoff_after["agent"]["id"] == old_id
                    assert handoff_after["agent"]["status"] == "failed"
                    assert handoff_after["execution"]["run"]["status"] == "failed"
                    assert handoff_after["execution"]["report"]["execution_status"] == "failed"
                    assert handoff_after["execution"]["side_effect_status"] == "unknown"
                    turn = ModelTurn(content="草稿方案已确认，旧投递仍未知，等待明确派工")
                else:
                    raise AssertionError("重规划通知不应自行派工或重复执行")
            else:
                if main_round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "handoff-web",
                                "commitment_inspect",
                                identifier=task_id,
                                section="handoff",
                                agent_id=old_id,
                                limit=20,
                            )
                        ]
                    )
                elif main_round == 2:
                    handoff = tool_result(messages, "handoff-web")
                    assert handoff["source_completeness"] == "recent_window_only"
                    assert handoff["authority"] == "coordination_data"
                    assert body_marker not in str(handoff)
                    evidence = next(
                        item
                        for item in handoff["items"]
                        if item.get("attachment_ids") == [attachment_id]
                    )
                    assert evidence["availability"] == "available"
                    reference = evidence["read"]
                    assert reference["session_id"] == old_id
                    evidence_seq, evidence_revision = reference["seq"], reference["revision"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-draft",
                                "spawn_agent",
                                task_name="草稿接办者",
                                prompt=f"只交草稿，不得发布。原资料会话={old_id}，原消息seq={evidence_seq}，revision={evidence_revision}。按原seq和revision查证后读取附件，未知投递不得重放。",
                                commitment_id=task_id,
                                attachment_ids=list(evidence["attachment_ids"]),
                            )
                        ]
                    )
                elif main_round == 3:
                    assert "error" not in tool_result(messages, "spawn-draft")
                    turn = ModelTurn(
                        tool_calls=[
                            call("wait-draft", "wait_agents", targets=["草稿接办者"], timeout=10)
                        ]
                    )
                else:
                    assert "草稿依据已核对" in str(messages)
                    turn = ModelTurn(content="草稿已完成；未再次发布，原投递结果仍未知")
            yield StreamEvent("done", turn=turn)

    class OldWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal old_round, delivery_id
            old_round += 1
            if old_round == 1:
                assert attachment_id in str(messages) and body_marker not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "publish",
                            "channel_send",
                            channel="onebot",
                            target="7",
                            text="原计划外发内容",
                            idempotency_key="one-logical-publication",
                        )
                    ]
                )
            elif old_round == 2:
                delivery_id = tool_result(messages, "publish")["id"]
                result = await terminal(manager, delivery_id)
                assert result["state"] == "unknown" and result["attempts"] == 1
                turn = ModelTurn(tool_calls=[call("delivery-state", "channel_status")])
            else:
                assert old_round == 3
                deliveries = tool_result(messages, "delivery-state")["deliveries"]
                assert (
                    next(item for item in deliveries if item["id"] == delivery_id)["state"]
                    == "unknown"
                )
                raise ValueError("固定离线故障：外发结果未知后旧执行者失败")
            yield StreamEvent("done", turn=turn)

    class DraftWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal draft_round
            draft_round += 1
            worker_requests.append(list(messages))
            assert "只交草稿，不得发布" in str(messages)
            assert "task_approve_plan" not in [tool["function"]["name"] for tool in tools]
            if draft_round == 1:
                assert body_marker not in str(messages)
                replacement_started.set()
                await draft_release.wait()
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "original-evidence",
                            "conversation_read",
                            session_id=old_id,
                            seq=evidence_seq,
                            revision=evidence_revision,
                        )
                    ]
                )
            elif draft_round == 2:
                original = tool_result(messages, "original-evidence")
                assert original["seq"] == evidence_seq and original["attachment_ids"] == [
                    attachment_id
                ]
                turn = ModelTurn(
                    tool_calls=[
                        call("source-file", "attachment_read", id=original["attachment_ids"][0])
                    ]
                )
            else:
                assert draft_round == 3
                assert body_marker in tool_result(messages, "source-file")["content"]
                turn = ModelTurn(content="草稿依据已核对；旧投递未知，仅交草稿，不再发布")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model != "worker":
            return Main([], [])
        return OldWorker([], []) if phase == "initial" else DraftWorker([], [])

    async def uncertain_http(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/send_private_msg"
        requests.append(request)
        raise httpx.ReadTimeout("offline unknown external outcome", request=request)

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(uncertain_http)
        ) as channel_client:
            manager = ChannelManager(
                runtime.store,
                runtime.bus,
                original_manager.config,
                original_manager.submit,
                app.state.attachments,
                client=channel_client,
                delivery_guard=original_manager.delivery_guard,
                admission=runtime.maintenance.admission,
            )
            await original_manager.close()
            app.state.channel_application.manager = manager
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
                ) as web,
            ):
                memory = await web.post(
                    "/api/memories", json={"content": "发布限制：可直接发布", "kind": "fact"}
                )
                assert memory.status_code == 200
                memory_id = memory.json()["id"]
                uploaded = await web.post(
                    "/api/uploads",
                    content=body_marker.encode(),
                    headers={"x-file-name": "original.txt", "content-type": "text/plain"},
                )
                assert uploaded.status_code == 200
                attachment_id = uploaded.json()["id"]
                main_id = runtime.main_session()["id"]
                assigned = await web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={
                        "content": "整理并发布资料，交给队友执行",
                        "mode": "yolo",
                        "attachment_ids": [attachment_id],
                    },
                )
                assert assigned.status_code == 200
                await asyncio.wait_for(runtime.wait(assigned.json()["run_id"]), 4)
                old = await wait_agent(runtime, old_id, "failed")
                await eventually(
                    runtime,
                    lambda: bool(completed(runtime, "旧执行者失败；已有外发结果未知，暂不重试")),
                )
                assert old["reply_origin"]["kind"] == "web"
                assert manager.delivery(delivery_id)["state"] == "unknown"
                assert len(requests) == 1
                assert runtime.store.messages(old_id)[0]["attachment_ids"] == [attachment_id]
                before_memory_revision = runtime.memory.revision
                changed = await web.patch(f"/api/memories/{memory_id}", json={"content": corrected})
                assert (
                    changed.status_code == 200 and runtime.memory.revision > before_memory_revision
                )
                paused = await web.post(
                    f"/api/commitments/{task_id}/control",
                    json={
                        "action": "pause",
                        "expected_epoch": 0,
                        "request_id": "pause-before-draft",
                    },
                )
                assert paused.status_code == 200
                phase, main_round = "replan", 0
                requested = await web.post(
                    f"/api/commitments/{task_id}/control",
                    json={
                        "action": "request_replan",
                        "expected_epoch": 1,
                        "request_id": "owner-draft-only",
                        "additional_requirements": "只交草稿，不得发布",
                    },
                )
                assert requested.status_code == 200
                await eventually(
                    runtime,
                    lambda: bool(completed(runtime, "草稿方案已确认，旧投递仍未知，等待明确派工")),
                )
                phase, main_round = "draft", 0
                reassigned = await web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={
                        "content": "按已确认草稿方案读取原执行者handoff，明确交给新队友接办，只交草稿，不得发布",
                        "mode": "yolo",
                    },
                )
                assert reassigned.status_code == 200
                await asyncio.wait_for(replacement_started.wait(), 4)
                assert runtime.team.get("草稿接办者")["commitment_id"] == task_id
                assert runtime.team.get(old_id)["generation"] == old["generation"]
                assert manager.delivery(delivery_id)["state"] == "unknown" and len(requests) == 1
                draft_release.set()
                await eventually(
                    runtime,
                    lambda: bool(completed(runtime, "草稿已完成；未再次发布，原投递结果仍未知")),
                )
                drafted = runtime.team.get("草稿接办者")
                assert drafted["status"] == "completed" and drafted["reply_origin"]["kind"] == "web"
                assert drafted["reply_origin"]["input_id"] == reassigned.json()["message"]["id"]
                assert (
                    completed(runtime, "草稿已完成；未再次发布，原投递结果仍未知")[0][
                        "delivery_routes"
                    ]
                    == []
                )
                assert len(requests) == 1 and manager.delivery(delivery_id)["attempts"] == 1
                assert len(worker_requests) == 3
                old_calls = [
                    call
                    for item in runtime.store.messages(old_id)
                    for call in item.get("tool_calls", [])
                ]
                new_calls = [
                    call
                    for item in runtime.store.messages(drafted["id"])
                    for call in item.get("tool_calls", [])
                ]
                assert [item["name"] for item in old_calls].count("channel_send") == 1
                assert all(
                    item["name"] not in {"channel_send", "audio_transcribe"} for item in new_calls
                )
    finally:
        draft_release.set()
        await runtime.close()
