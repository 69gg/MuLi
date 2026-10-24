"""Offline handoff journeys through real Runtime, task control and durable tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channels import onebot_body, onebot_event, profile, terminal
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import completed, eventually, tool_result

from muli.channels.manager import encoded
from muli.compaction import closed_groups
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app

_REFERENCE_PREFIX = "原资料定位："


def call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


def received_handoff_reference(messages: list[Message]) -> dict[str, Any]:
    references: list[str] = []
    for message in messages:
        if message.role != "user":
            continue
        # Team._message retains the sender and JSON-encodes the actual prompt.
        assert len(message.content) <= 64000
        _, separator, payload = message.content.partition("\n")
        assert separator
        envelope = json.loads(payload)
        assert isinstance(envelope, dict) and set(envelope) == {"sender", "content"}
        assert isinstance(envelope["sender"], str) and envelope["sender"]
        assert isinstance(envelope["content"], str)
        references.extend(
            line.removeprefix(_REFERENCE_PREFIX)
            for line in envelope["content"].splitlines()
            if line.startswith(_REFERENCE_PREFIX)
        )
    assert len(references) == 1, "接办请求缺少唯一原资料定位"
    assert len(references[0].encode("utf-8")) <= 1024
    reference = json.loads(references[0])
    assert isinstance(reference, dict) and set(reference) == {"session_id", "seq", "revision"}
    assert type(reference["seq"]) is int and reference["seq"] > 0
    assert all(
        isinstance(reference[field], str) and 1 <= len(reference[field]) <= 256
        for field in ("session_id", "revision")
    )
    return reference


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("handoff旅程只允许显式HTTP MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


@pytest.mark.parametrize(
    ("control_channel", "compact_and_reopen"),
    [
        pytest.param("web", False, id="web"),
        pytest.param("onebot", False, id="onebot"),
        pytest.param("onebot", True, id="onebot-compacted-reopened"),
    ],
)
async def test_owner_replans_failed_unknown_delivery_and_hands_original_evidence_to_draft_worker(
    tmp_path: Path,
    control_channel: str,
    compact_and_reopen: bool,
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
    if compact_and_reopen:
        runtime.config.update(
            {
                "limits": {"max_context_chars": 32000},
                "compaction": {
                    "enabled": True,
                    "mode": "extractive",
                    "trigger_ratio": 0.25,
                    "target_ratio": 0.2,
                },
            }
        )
    await runtime.reconfigure()
    phase = "initial"
    main_round = old_round = draft_round = 0
    task_id = attachment_id = delivery_id = ""
    old_id = ""
    handoff_before: dict[str, Any] = {}
    handoff_after: dict[str, Any] = {}
    closed_reference: dict[str, Any] | None = None
    draft_reference: dict[str, Any] = {}
    body_marker = "HANDOFF-ORIGINAL-SOURCE-ONLY"
    corrected = "发布限制：现在只交草稿，不得发布"
    initial_text = "整理并发布资料，交给队友执行"
    publication_text = "原计划外发内容"
    replan_text = "草稿方案已确认，旧投递仍未知，等待明确派工"
    if compact_and_reopen:
        trigger_chars = int(
            runtime.config.settings.limits.max_context_chars
            * runtime.config.settings.compaction.trigger_ratio
        )
        sample_chars = trigger_chars + 1024
        fragment = "已归档的背景资料，只用于核对，不新增操作授权。"
        background = (fragment * (sample_chars // len(fragment) + 1))[:sample_chars]
        initial_text += "\n背景资料：\n" + background
        publication_text += "\n" + background
    chunk_size = runtime.config.settings.channels.onebot.max_message_chars
    publication_parts = [
        publication_text[offset : offset + chunk_size]
        for offset in range(0, len(publication_text), chunk_size)
    ]
    requests: list[httpx.Request] = []
    publication_attempts: list[int] = []
    phone_confirmations: list[str] = []
    phone_replan_replies: list[str] = []
    phone_inputs: dict[str, str] = {}
    phone_receipts: dict[str, str] = {}
    unexpected_requests: list[tuple[str, str]] = []
    worker_requests: list[list[Message]] = []
    replacement_started = asyncio.Event()
    draft_release = asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round, task_id, old_id, handoff_before, handoff_after
            if phase == "reopened-idle":
                unexpected_requests.append((phase, "main"))
                raise AssertionError("正常重开与手机重投不能自行唤醒主模型")
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
                    turn = ModelTurn(content=replan_text)
                else:
                    unexpected_requests.append((phase, "main"))
                    raise AssertionError("重规划通知不应自行派工或重复执行")
            else:
                if main_round == 1:
                    if compact_and_reopen:
                        assert any("历史分层摘要" in message.content for message in messages)
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
                    assert reference == closed_reference
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-draft",
                                "spawn_agent",
                                task_name="草稿接办者",
                                prompt=(
                                    "只交草稿，不得发布。\n"
                                    + _REFERENCE_PREFIX
                                    + json.dumps(reference, ensure_ascii=False)
                                    + "\n按原seq和revision查证后读取附件，未知投递不得重放。"
                                ),
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
            if phase != "initial":
                unexpected_requests.append((phase, "old-worker"))
                raise AssertionError("失败的原代次不能在重开后复活")
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
                            text=publication_text,
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
            nonlocal draft_round, draft_reference
            if phase != "draft":
                unexpected_requests.append((phase, "draft-worker"))
                raise AssertionError("明确派工前不能启动草稿接办者")
            draft_round += 1
            worker_requests.append(list(messages))
            assert "只交草稿，不得发布" in str(messages)
            assert "task_approve_plan" not in [tool["function"]["name"] for tool in tools]
            if draft_round == 1:
                assert body_marker not in str(messages)
                draft_reference = received_handoff_reference(messages)
                assert draft_reference == closed_reference
                replacement_started.set()
                await draft_release.wait()
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "original-evidence",
                            "conversation_read",
                            session_id=draft_reference["session_id"],
                            seq=draft_reference["seq"],
                            revision=draft_reference["revision"],
                        )
                    ]
                )
            elif draft_round == 2:
                original = tool_result(messages, "original-evidence")
                assert original["seq"] == draft_reference["seq"]
                assert original["revision"] == draft_reference["revision"]
                assert original["attachment_ids"] == [attachment_id]
                assert body_marker not in str(messages)
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
        if text in publication_parts:
            publication_attempts.append(publication_parts.index(text))
        if text != publication_parts[0]:
            assert control_channel == "onebot" and body["user_id"] == "7"
            assert text not in publication_parts[1:]
            deliveries = [
                item
                for item in manager.deliveries()
                if "".join(part.get("text", "") for part in item["payload"]["parts"]) == text
            ]
            assert len(deliveries) == 1
            if deliveries[0]["payload"].get("automatic") is True:
                assert text == replan_text
                phone_replan_replies.append(text)
            else:
                phone_confirmations.append(text)
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": "phone-ok"}}
            )
        requests.append(request)
        assert len(requests) == 1
        assert body["user_id"] == "7"
        raise httpx.ReadTimeout("offline unknown external outcome", request=request)

    def phone_envelope(
        event_id: str, text: str, *, sender: int = 7
    ) -> tuple[bytes, dict[str, str]]:
        payload = {
            **onebot_event(sender=sender),
            "self_id": 123,
            "sub_type": "friend",
            "message_id": event_id,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        return onebot_body(payload)

    async def phone_control(event_id: str, text: str) -> dict[str, Any]:
        raw, headers = phone_envelope(event_id, text)
        assert await manager.webhook("onebot", raw, headers) == {"ok": True}
        inbound_id = hashlib.sha256(encoded(["onebot", "123", event_id]).encode()).hexdigest()
        row = runtime.store.db.execute(
            "SELECT receipt FROM channel_inbound WHERE id=?", (inbound_id,)
        ).fetchone()
        assert row is not None and row[0] is not None
        assert phone_inputs.setdefault(event_id, text) == text
        assert phone_receipts.setdefault(event_id, row[0]) == row[0]
        receipt = json.loads(row[0])
        assert isinstance(receipt, dict) and isinstance(receipt["reply_text"], str)
        return receipt

    async def enter_application(stack: AsyncExitStack) -> httpx.AsyncClient:
        channel_client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.MockTransport(uncertain_http))
        )
        # Retain the real composition's manager, adapters and admission closure.
        for adapter in manager.adapters.values():
            adapter.http.client = channel_client
        await stack.enter_async_context(app.router.lifespan_context(app))
        return await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost:8765")
        )

    async def channel_consumed(message_id: str) -> None:
        event_id = next(
            event["id"]
            for event in runtime.store.events(session_id=runtime.main_session()["id"])
            if event["type"] == "message.completed" and event["data"]["message"]["id"] == message_id
        )

        def consumed() -> bool:
            row = runtime.store.db.execute(
                "SELECT value FROM channel_state WHERE name='events'"
            ).fetchone()
            return row is not None and int(row[0]) >= event_id

        async with asyncio.timeout(4), manager.changed:
            await manager.changed.wait_for(consumed)

    runtime.client_factory = factory  # type: ignore[assignment]
    scenario_factory = runtime.client_factory
    # PluginManager captures its base factory while create_app composes the application.
    app = create_app(runtime.config, runtime=runtime)
    original_manager = app.state.channel_application.manager
    manager = original_manager
    try:
        async with AsyncExitStack() as stage:
            web = await enter_application(stage)
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
                    "content": initial_text,
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
            assert old["reply_origin"]["input_id"] == assigned.json()["message"]["id"]
            assert manager.delivery(delivery_id)["state"] == "unknown"
            assert len(requests) == 1
            assert runtime.store.messages(old_id)[0]["attachment_ids"] == [attachment_id]
            before_memory_revision = runtime.memory.revision
            changed = await web.patch(f"/api/memories/{memory_id}", json={"content": corrected})
            assert changed.status_code == 200 and runtime.memory.revision > before_memory_revision
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
                pause_codes = re.findall(r"/暂停 ([a-f0-9]{12}-[1-9][0-9]*)", status["reply_text"])
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
                lambda: bool(completed(runtime, replan_text)),
            )
            replan_messages = completed(runtime, replan_text)
            assert len(replan_messages) == 1
            if control_channel == "onebot":
                replan_message = replan_messages[0]
                await channel_consumed(replan_message["id"])
                automatic = [
                    item
                    for item in manager.deliveries()
                    if item["payload"].get("automatic") is True
                ]
                assert len(automatic) == 1
                replan_delivery = await terminal(manager, automatic[0]["id"])
                # A confirmed part clears the retry counter; the HTTP recorder
                # below proves this automatic reply was actually sent once.
                assert replan_delivery["state"] == "sent" and replan_delivery["attempts"] == 0
                assert replan_delivery["payload"]["parts"] == [{"text": replan_text}]
                expected_route = {
                    "name": "onebot",
                    "account_id": "123",
                    "target": "7",
                    "sender_id": "7",
                    "kind": "private",
                }
                assert replan_delivery["route"] == expected_route
                assert replan_message["delivery_routes"] == [expected_route]
                assert replan_message["reply_origin"] == {
                    "kind": "channel",
                    "input_id": hashlib.sha256(
                        encoded(["onebot", "123", "phone-draft-only"]).encode()
                    ).hexdigest(),
                    "route": expected_route,
                }
                assert (
                    replan_delivery["id"]
                    == hashlib.sha256(
                        encoded([replan_message["id"], "onebot", expected_route]).encode()
                    ).hexdigest()
                )
                assert phone_replan_replies == [replan_text]
            closed_reference = next(
                item["read"]
                for item in handoff_after["items"]
                if item.get("attachment_ids") == [attachment_id]
            )
            if compact_and_reopen:
                assert runtime.task_control is not None
                pending = runtime.tasks.get(main_id)
                if pending is not None:
                    await asyncio.wait_for(asyncio.shield(pending), 4)
                assert runtime.main_session()["status"] == "idle"
                assert not runtime.team.has_deliverable_reports()
                assert runtime.store.pending_inputs(main_id) == 0
                assert runtime.store.pending_inputs(old_id) == 0
                assert runtime.store.list("task_plan_delivery") == []
                assert len(runtime.team.list()) == 1
                approval = next(
                    item
                    for item in runtime.store.list("task_control_receipt")
                    if item["action"] == "approve_plan"
                )
                assert approval["target_count"] == 0
                await eventually(runtime, lambda: len(phone_confirmations) == 3)
                await channel_consumed(replan_messages[0]["id"])
                before_outbox = {item["id"]: item for item in manager.deliveries()}
                assert len(before_outbox) == 5
                assert all(
                    item["state"] == "sent"
                    for identifier, item in before_outbox.items()
                    if identifier != delivery_id
                )
                unknown = before_outbox[delivery_id]
                assert unknown["state"] == "unknown" and unknown["attempts"] == 1
                assert unknown["next_part"] == 0 and unknown["receipts"] == []
                assert [part["text"] for part in unknown["payload"]["parts"]] == publication_parts
                assert len(publication_parts) > 1 and len(requests) == 1
                assert publication_attempts == [0]
                before_records = {
                    identifier: runtime.store.messages(identifier)
                    for identifier in (main_id, old_id)
                }
                original_input = next(
                    item
                    for item in before_records[main_id]
                    if item["id"] == assigned.json()["message"]["id"]
                )
                original_publish = next(
                    item for item in before_records[old_id] if item.get("tool_call_id") == "publish"
                )
                required_coverage = {
                    main_id: original_input["seq"],
                    old_id: original_publish["seq"],
                }
                compactions: dict[str, dict[str, Any]] = {}
                for identifier, records in before_records.items():
                    assert sum(map(len, closed_groups(records))) == len(records)
                    runtime.compaction.observe(identifier)
                    await runtime.compaction.finish(identifier)
                    projected = runtime.compaction.project(identifier)
                    active = runtime.store.get("compaction", identifier)
                    assert active is not None
                    assert active["session_id"] == identifier and active["generation"] > 0
                    assert active["kind"] == "extractive" and active["summary"]
                    assert active["covered_seq"] >= required_coverage[identifier]
                    assert active["covered_seq"] in {
                        group[-1]["seq"] for group in closed_groups(records)
                    }
                    snapshot = runtime.store.get("compaction_snapshot", active["snapshot_id"])
                    assert snapshot is not None
                    assert snapshot["session_id"] == identifier
                    assert snapshot["generation"] == active["generation"]
                    assert projected[0]["id"] == active["snapshot_id"]
                    assert projected[0]["compaction_summary"] is True
                    assert sum(map(len, closed_groups(projected))) == len(projected)
                    assert runtime.store.messages(identifier) == records
                    compactions[identifier] = active
                before_attachment = app.state.attachments.get(attachment_id)
                attachment_hash = hashlib.sha256(
                    app.state.attachments.path(attachment_id).read_bytes()
                ).hexdigest()
                assert attachment_hash == before_attachment["sha256"]
                assert attachment_hash == hashlib.sha256(body_marker.encode()).hexdigest()
                assert manager.delivery(delivery_id) == unknown
                old_before = runtime.team.get(old_id)
                before_control = runtime.task_control.current(task_id)["control"]
                assert before_control["epoch"] == before_control["approved_epoch"] == 2
                before_domain = runtime.store.list("task_control_receipt")
                before_ingress = [
                    dict(row)
                    for row in runtime.store.db.execute("SELECT * FROM ingress ORDER BY seq")
                ]
                old_store = runtime.store
                old_local_services = app.state.local_services
                old_memory_application = app.state.memory_application
                old_adapters = dict(manager.adapters)
                store_instance = runtime.store.instance_id
                config_path, data_dir = runtime.config.path, runtime.data_dir
                frozen_rounds = (main_round, old_round, draft_round)
                phase = "reopened-idle"
                # Consume all three owned contexts before constructing a second writer.
                await stage.aclose()
                with pytest.raises(sqlite3.ProgrammingError):
                    old_store.db.execute("SELECT 1")
                assert runtime.client_factory is scenario_factory
                runtime = Runtime(
                    ConfigFile(config_path), data_dir, client_factory=runtime.client_factory
                )
                assert runtime.client_factory is scenario_factory
                app = create_app(runtime.config, runtime=runtime)
                manager = app.state.channel_application.manager
                assert (runtime.store is old_store) is False
                assert (manager is original_manager) is False
                assert (app.state.local_services is old_local_services) is False
                assert (app.state.memory_application is old_memory_application) is False
                assert all(
                    adapter is not old_adapters[name] for name, adapter in manager.adapters.items()
                )
                web = await enter_application(stage)
                assert runtime.store.instance_id == store_instance
                assert runtime.main_session()["id"] == main_id
                assert runtime.task_control is not None
                assert runtime.task_control.current(task_id)["control"] == before_control
                assert runtime.team.get(old_id) == old_before
                assert len(runtime.team.list()) == 1
                assert runtime.store.pending_inputs(main_id) == 0
                assert runtime.store.pending_inputs(old_id) == 0
                assert not runtime.team.has_deliverable_reports()
                assert app.state.attachments.get(attachment_id) == before_attachment
                assert (
                    hashlib.sha256(
                        app.state.attachments.path(attachment_id).read_bytes()
                    ).hexdigest()
                    == attachment_hash
                )
                for identifier, records in before_records.items():
                    assert runtime.store.messages(identifier) == records
                    assert runtime.store.get("compaction", identifier) == compactions[identifier]
                    assert (
                        runtime.store.get(
                            "compaction_snapshot", compactions[identifier]["snapshot_id"]
                        )
                        is not None
                    )
                for event_id, text in list(phone_inputs.items()):
                    await phone_control(event_id, text)
                assert {item["id"]: item for item in manager.deliveries()} == before_outbox
                assert len(phone_confirmations) == 3
                assert runtime.store.list("task_control_receipt") == before_domain
                assert [
                    dict(row)
                    for row in runtime.store.db.execute("SELECT * FROM ingress ORDER BY seq")
                ] == before_ingress
                stale = await phone_control("phone-stale-pause", phone_inputs["phone-pause"])
                assert stale["mobile_task_control"]["error"] == "epoch_conflict"
                rejected_id = "phone-stranger-pause"
                raw, headers = phone_envelope(rejected_id, phone_inputs["phone-pause"], sender=8)
                # The webhook acknowledges receive-level 403 without retrying it.
                assert await manager.webhook("onebot", raw, headers) == {"ok": True}
                rejected_key = hashlib.sha256(
                    encoded(["onebot", "123", rejected_id]).encode()
                ).hexdigest()
                assert (
                    runtime.store.db.execute(
                        "SELECT 1 FROM channel_inbound WHERE id=?", (rejected_key,)
                    ).fetchone()
                    is None
                )
                await eventually(runtime, lambda: len(phone_confirmations) == 4)
                assert len(manager.deliveries()) == 6
                assert runtime.task_control.current(task_id)["control"] == before_control
                assert runtime.store.list("task_control_receipt") == before_domain
                assert [
                    dict(row)
                    for row in runtime.store.db.execute("SELECT * FROM ingress ORDER BY seq")
                ] == before_ingress
                assert (main_round, old_round, draft_round) == frozen_rounds
                assert main_id not in runtime.tasks and not unexpected_requests
                assert manager.delivery(delivery_id) == unknown and len(requests) == 1
            phase, main_round = "draft", 0
            reassigned = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={
                    "content": "按已确认草稿方案读取原执行者handoff，明确交给新队友接办，只交草稿，不得发布",
                    "mode": "yolo",
                },
            )
            assert reassigned.status_code == 200
            try:
                await asyncio.wait_for(replacement_started.wait(), 4)
            except TimeoutError as error:

                def stalled_diagnostic() -> str:
                    # Read only fixed diagnostic labels, never error text or source bodies.
                    def state_label(value: Any) -> str:
                        return (
                            value
                            if isinstance(value, str)
                            and value
                            in {
                                "queued",
                                "running",
                                "completed",
                                "failed",
                                "paused",
                                "cancelled",
                                "interrupted",
                                "idle",
                                "closed",
                            }
                            else "unknown"
                        )

                    def run_diagnostic(identifier: str) -> dict[str, Any]:
                        run = runtime.store.get("run", identifier) or {}
                        diagnostic = run.get("failure_diagnostic") or {}
                        kind = diagnostic.get("exception_type")
                        return {
                            "status": state_label(run.get("status")),
                            "exception_type": kind
                            if isinstance(kind, str)
                            and kind
                            in {
                                "AssertionError",
                                "TypeError",
                                "ValueError",
                                "KeyError",
                                "IndexError",
                                "RuntimeError",
                                "TimeoutError",
                                "OSError",
                                "PermissionError",
                                "FileNotFoundError",
                                "ConnectionError",
                                "MemoryError",
                                "RecursionError",
                                "ExceptionGroup",
                            }
                            else "unknown",
                        }

                    def last_tool(session_id: str) -> dict[str, Any]:
                        identifiers = {
                            "handoff-web",
                            "spawn-draft",
                            "wait-draft",
                            "original-evidence",
                            "source-file",
                        }
                        message = next(
                            (
                                item
                                for item in reversed(runtime.store.messages(session_id, limit=8))
                                if item.get("role") == "tool"
                                and item.get("tool_call_id") in identifiers
                            ),
                            None,
                        )
                        if message is None:
                            return {"call_id": "not_observed"}
                        try:
                            payload = json.loads(message["content"])
                        except (ValueError, TypeError):
                            payload = None
                        payload = payload if isinstance(payload, dict) else {}
                        code = payload.get("code")
                        failure = payload.get("error")
                        kind = (
                            re.fullmatch(
                                r"工具执行失败：(AssertionError|TypeError|ValueError|KeyError|IndexError|"
                                r"RuntimeError|TimeoutError|OSError|PermissionError|FileNotFoundError)",
                                failure,
                            )
                            if isinstance(failure, str)
                            else None
                        )
                        return {
                            "call_id": message["tool_call_id"],
                            "error": bool(failure),
                            "exception_type": kind[1] if kind else "unknown",
                            "code": code
                            if isinstance(code, str)
                            and code
                            in {
                                "output_budget",
                                "evidence_changed",
                                "task_paused",
                                "invalid_arguments",
                                "content_unavailable",
                                "evidence_not_found",
                            }
                            else "unknown",
                        }

                    try:
                        draft = runtime.team.get("草稿接办者")
                    except ValueError:
                        draft = {}
                    diagnostic = {
                        "phase": "draft",
                        "rounds": {"main": main_round, "old": old_round, "draft": draft_round},
                        "main": run_diagnostic(reassigned.json()["run_id"]),
                        "main_tool": last_tool(main_id),
                        "draft_status": state_label(draft.get("status")),
                        "draft": run_diagnostic(draft.get("run_id", "")),
                        "draft_tool": last_tool(draft["session_id"])
                        if draft
                        else {
                            "call_id": "not_observed",
                        },
                        "pending_approvals": len(runtime.permissions.pending),
                    }
                    note = json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":"))
                    return (
                        note
                        if len(note.encode("utf-8")) <= 1024
                        else ('{"phase":"draft","diagnostic":"output_budget"}')
                    )

                try:
                    error.add_note(stalled_diagnostic())
                except Exception:
                    error.add_note('{"phase":"draft","diagnostic":"unavailable"}')
                raise
            without_reference = list(worker_requests[0])
            user_index = next(
                index for index, message in enumerate(without_reference) if message.role == "user"
            )
            user_message = without_reference[user_index]
            header, _, encoded_prompt = user_message.content.partition("\n")
            envelope = json.loads(encoded_prompt)
            without_reference[user_index] = replace(
                user_message,
                content=header
                + "\n"
                + json.dumps(
                    {
                        **envelope,
                        "content": "\n".join(
                            line
                            for line in envelope["content"].splitlines()
                            if not line.startswith(_REFERENCE_PREFIX)
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
            with pytest.raises(AssertionError, match="接办请求缺少唯一原资料定位"):
                received_handoff_reference(without_reference)
            assert runtime.team.get("草稿接办者")["commitment_id"] == task_id
            assert runtime.team.get("草稿接办者")["id"] != old_id
            assert runtime.team.get(old_id)["generation"] == old["generation"]
            assert runtime.team.get(old_id)["reply_origin"] == old["reply_origin"]
            assert manager.delivery(delivery_id)["state"] == "unknown" and len(requests) == 1
            draft_release.set()
            await eventually(
                runtime,
                lambda: bool(completed(runtime, "草稿已完成；未再次发布，原投递结果仍未知")),
            )
            drafted = runtime.team.get("草稿接办者")
            assert drafted["status"] == "completed" and drafted["reply_origin"]["kind"] == "web"
            assert drafted["reply_origin"]["input_id"] == reassigned.json()["message"]["id"]
            draft_session = runtime.store.get("session", drafted["id"])
            assert draft_session is not None and draft_session["mode"] == "yolo"
            final = completed(runtime, "草稿已完成；未再次发布，原投递结果仍未知")[0]
            assert final["delivery_routes"] == []
            await channel_consumed(final["id"])
            assert len(requests) == 1 and publication_attempts == [0]
            assert manager.delivery(delivery_id)["attempts"] == 1
            assert manager.delivery(delivery_id)["next_part"] == 0
            assert manager.delivery(delivery_id)["receipts"] == []
            assert len(worker_requests) == 3 and not unexpected_requests
            if control_channel == "onebot":
                confirmations = 4 if compact_and_reopen else 3
                await eventually(runtime, lambda: len(phone_confirmations) == confirmations)
                assert sorted(phone_confirmations) == sorted(
                    json.loads(receipt)["reply_text"] for receipt in phone_receipts.values()
                )
                assert len(manager.deliveries()) == confirmations + 2
                assert {
                    item["id"]
                    for item in manager.deliveries()
                    if item["payload"].get("automatic") is True
                } == {replan_delivery["id"]}
                assert manager.delivery(replan_delivery["id"]) == replan_delivery
                assert phone_replan_replies == [replan_text]
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
