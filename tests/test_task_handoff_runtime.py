"""Offline handoff journeys through real Runtime, task control and durable tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channels import onebot_body, onebot_event, profile, terminal
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import completed, eventually, tool_result

from muli.channels.manager import encoded
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.server import create_app


def call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("handoff旅程只允许显式HTTP MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


@pytest.mark.parametrize("control_channel", ["web", "onebot"])
async def test_owner_replans_failed_unknown_delivery_and_hands_original_evidence_to_draft_worker(
    tmp_path: Path,
    control_channel: str,
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
                "onebot": asdict(profile(account_id="123" if control_channel == "onebot" else ""))
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
    phone_confirmations: list[str] = []
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
        body = json.loads(request.content)
        text = "".join(segment["data"]["text"] for segment in body["message"])
        if text != "原计划外发内容":
            assert control_channel == "onebot" and body["user_id"] == "7"
            phone_confirmations.append(text)
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": "phone-ok"}}
            )
        requests.append(request)
        raise httpx.ReadTimeout("offline unknown external outcome", request=request)

    async def phone_control(event_id: str, text: str) -> dict[str, Any]:
        payload = {
            **onebot_event(),
            "self_id": 123,
            "sub_type": "friend",
            "message_id": event_id,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        raw, headers = onebot_body(payload)
        assert await manager.webhook("onebot", raw, headers) == {"ok": True}
        inbound_id = hashlib.sha256(encoded(["onebot", "123", event_id]).encode()).hexdigest()
        row = runtime.store.db.execute(
            "SELECT receipt FROM channel_inbound WHERE id=?", (inbound_id,)
        ).fetchone()
        assert row is not None and row[0] is not None
        receipt = json.loads(row[0])
        assert isinstance(receipt, dict) and isinstance(receipt["reply_text"], str)
        return receipt

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(uncertain_http)
        ) as channel_client:
            manager = original_manager
            # Keep the real composition's current manager/adapter identity and
            # admission closure; only the platform HTTP transport is a substitute.
            for adapter in manager.adapters.values():
                adapter.http.client = channel_client
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
                if control_channel == "web":
                    paused = await web.post(
                        f"/api/commitments/{task_id}/control",
                        json={
                            "action": "pause",
                            "expected_epoch": 0,
                            "request_id": "pause-before-draft",
                        },
                    )
                    assert paused.status_code == 200
                else:
                    before_round = main_round
                    status = await phone_control("phone-status", "/任务")
                    pause_codes = re.findall(
                        r"/暂停 ([a-f0-9]{12}-[1-9][0-9]*)", status["reply_text"]
                    )
                    assert len(pause_codes) == 1
                    pause_text = "/暂停 " + pause_codes[0]
                    paused_phone = await phone_control("phone-pause", pause_text)
                    assert await phone_control("phone-pause", pause_text) == paused_phone
                    assert main_round == before_round
                    assert runtime.task_control is not None
                    assert runtime.task_control.current(task_id)["control"]["epoch"] == 1
                phase, main_round = "replan", 0
                if control_channel == "web":
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
                else:
                    replan_codes = re.findall(
                        r"/调整 ([a-f0-9]{12}-[1-9][0-9]*)", paused_phone["reply_text"]
                    )
                    assert len(replan_codes) == 1
                    adjusted_phone = await phone_control(
                        "phone-draft-only", "/调整 " + replan_codes[0] + " 只交草稿，不得发布"
                    )
                    control_receipt_id = adjusted_phone["mobile_task_control"]["control_receipt_id"]
                    domain_receipt = runtime.store.get("task_control_receipt", control_receipt_id)
                    assert domain_receipt is not None
                    control_input = runtime.store.db.execute(
                        "SELECT payload FROM ingress WHERE id=?", (domain_receipt["ingress_id"],)
                    ).fetchone()
                    assert control_input is not None
                    payload = json.loads(control_input["payload"])
                    assert payload["route"] is None
                    assert payload["task_control"] == {
                        "commitment_id": task_id,
                        "receipt_id": control_receipt_id,
                        "epoch": 2,
                    }
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
                if control_channel == "onebot":
                    await eventually(runtime, lambda: len(phone_confirmations) == 3)
                    assert len(manager.deliveries()) == 4
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
