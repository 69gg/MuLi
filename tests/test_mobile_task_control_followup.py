"""Offline journeys for the main assistant's mobile replan reply, not task completion."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from test_channels import onebot_body, terminal
from test_mobile_channels import onebot_payload
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.channels import ChannelManager
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app


async def phone(
    web: httpx.AsyncClient, text: str, message_id: int, *, sender: int = 7
) -> httpx.Response:
    payload = onebot_payload(text)
    payload.update(message_id=message_id, user_id=sender)
    body, headers = onebot_body(payload)
    headers["Content-Type"] = "application/json"
    return await web.post("/api/channels/onebot/webhook", content=body, headers=headers)


def delivery_text(item: dict[str, Any]) -> str:
    return "".join(part.get("text", "") for part in item["payload"]["parts"])


def command_code(manager: ChannelManager, action: str) -> str:
    matches = [
        match.group(1)
        for item in manager.deliveries()
        if (match := re.search(rf"/{action} ([a-f0-9]{{12}}-1)", delivery_text(item)))
    ]
    assert len(matches) == 1
    return matches[0]


async def processed_reply(runtime: Runtime, manager: ChannelManager, message_id: str) -> None:
    event = next(
        event
        for event in runtime.store.events(session_id=runtime.main_session()["id"])
        if event["type"] == "message.completed"
        and event["data"].get("message", {}).get("id") == message_id
    )
    async with asyncio.timeout(3), manager.changed:
        await manager.changed.wait_for(lambda: int(manager._state("events", "0")) >= event["id"])


def assert_one_replan(runtime: Runtime, task_id: str, main_id: str) -> None:
    receipts = runtime.store.list("task_control_receipt")
    assert (
        len(
            [
                item
                for item in receipts
                if item["commitment_id"] == task_id and item["action"] == "request_replan"
            ]
        )
        == 1
    )
    assert (
        runtime.store.db.execute(
            "SELECT count(*) FROM ingress WHERE session_id=? "
            "AND json_extract(payload,'$.source')='task-control' "
            "AND json_extract(payload,'$.principal')='owner'",
            (main_id,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("previous_origin", "outcome"),
    [
        ("web", "sent"),
        ("other_private", "sent"),
        ("web", "blocked"),
        ("other_private", "unknown"),
    ],
    ids=["web-to-mobile", "other-private-to-mobile", "revoked-before-reopen", "unknown-not-resent"],
)
async def test_mobile_replan_reply_survives_reopen_without_stealing_an_old_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_origin: Literal["web", "other_private"],
    outcome: Literal["sent", "blocked", "unknown"],
) -> None:
    initial_reply = "网页原任务已登记，原队友正在核查。"
    old_private_reply = "已收到另一私聊说明，原任务仍只核查。"
    requirement = "只交草稿，不得发布"
    plan = "核查现状后只交草稿，不得发布；不重做结果未知的操作。"
    followup = "新方案已确认：只核查现状并交草稿，不得发布；原队友按原任务继续。"
    requests: list[dict[str, Any]] = []
    followup_attempts: list[httpx.Request] = []

    async def platform(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.url.path == "/send_private_msg" and request.method == "POST"
        payload = json.loads(request.content)
        requests.append(payload)
        text = "".join(segment["data"]["text"] for segment in payload["message"])
        if text == followup:
            followup_attempts.append(request)
            if outcome == "unknown":
                assert len(followup_attempts) == 1
                raise httpx.ReadTimeout("one explicit unconfirmed replan reply", request=request)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(requests)}}
        )

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        # Keep the real ChannelApplication manager, adapter and authorization closures.
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7", "8"],
                }
            },
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    worker_entered, finish_old_request = asyncio.Event(), asyncio.Event()
    task_id = ""
    worker_id = ""
    first_round = 0
    forbidden_path = tmp_path / "must-not-be-published.txt"

    class InitialMain(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal first_round, task_id, worker_id
            first_round += 1
            if first_round == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "save-task",
                            "commitment_save",
                            title="网页持续任务",
                            brief={"goal": "核查项目现状", "constraints": ["不可部署"]},
                        )
                    ]
                )
            elif first_round == 2:
                task_id = tool_result(messages, "save-task")["id"]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-original",
                            "spawn_agent",
                            task_name="原队友",
                            prompt="核查项目现状，等待完整后续要求。",
                            commitment_id=task_id,
                        )
                    ]
                )
            elif first_round == 3:
                worker_id = tool_result(messages, "spawn-original")["id"]
                turn = ModelTurn(content=initial_reply)
            else:
                assert first_round == 4 and previous_origin == "other_private"
                turn = ModelTurn(content=old_private_reply)
            yield StreamEvent("done", turn=turn)

    class InitialWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            worker_entered.set()
            await finish_old_request.wait()
            yield StreamEvent(
                "done",
                turn=ModelTurn(
                    tool_calls=[
                        call(
                            "old-write", "fs_write", path=str(forbidden_path), content="not allowed"
                        )
                    ]
                ),
            )

    def initial_factory(config: ModelConfig) -> FakeClient:
        return InitialWorker([], []) if config.model == "worker" else InitialMain([], [])

    runtime.client_factory = initial_factory  # type: ignore[assignment]
    main_id = runtime.main_session()["id"]
    adjustment: asyncio.Task[httpx.Response] | None = None
    adjustment_text = ""
    paused_report_id = ""
    original_worker_origin: dict[str, Any] = {}
    original_generation = 0
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assigned = await web.post(
                f"/api/sessions/{main_id}/messages", json={"content": "请核查项目现状，不可部署。"}
            )
            assert assigned.status_code == 200
            assert await runtime.wait(assigned.json()["run_id"]) == initial_reply
            await asyncio.wait_for(worker_entered.wait(), 3)
            worker = runtime.team.get(worker_id)
            assert worker["commitment_id"] == task_id and worker["parent_id"] == main_id
            original_worker_origin = worker["reply_origin"]
            original_generation = worker["generation"]
            assert original_worker_origin["kind"] == "web"
            if previous_origin == "other_private":
                old_input = await phone(web, "给原任务补充说明，继续保持只读。", 100, sender=8)
                assert old_input.status_code == 200
                await eventually(runtime, lambda: bool(completed(runtime, old_private_reply)))

            manager = app.state.channel_application.manager
            assert (await phone(web, "/任务", 101)).status_code == 200
            pause_code = command_code(manager, "暂停")
            assert (await phone(web, f"/暂停 {pause_code}", 102)).status_code == 200
            finish_old_request.set()
            await wait_agent(runtime, worker_id, "paused")
            assert not forbidden_path.exists()
            old_result = next(
                item
                for item in runtime.store.messages(worker_id)
                if item.get("tool_call_id") == "old-write"
            )
            assert json.loads(old_result["content"])["code"] == "task_paused"
            paused_report = next(
                item
                for item in runtime.store.list("team_report")
                if item["agent_id"] == worker_id and item["execution_status"] == "paused"
            )
            assert paused_report["status"] == "pending"
            assert paused_report["reply_origin"] == original_worker_origin
            paused_report_id = paused_report["id"]
            adjustment_text = f"/调整 {command_code(manager, '调整')} {requirement}"
            persisted = asyncio.Event()
            never_released = asyncio.Event()
            notify = runtime.bus.notify

            async def stop_after_durable_control() -> None:
                await notify()
                if (
                    not persisted.is_set()
                    and runtime.task_control_current(task_id)["control"]["epoch"] == 2
                    and runtime.store.pending_inputs(main_id) == 1
                ):
                    # Freeze a real post-commit await, then use normal application close.
                    persisted.set()
                    await never_released.wait()

            with monkeypatch.context() as control_patch:
                control_patch.setattr(runtime.bus, "notify", stop_after_durable_control)
                adjustment = asyncio.create_task(phone(web, adjustment_text, 103))
                await asyncio.wait_for(persisted.wait(), 3)
                assert_one_replan(runtime, task_id, main_id)
                assert first_round == (4 if previous_origin == "other_private" else 3)
                await asyncio.wait_for(app.state.close(), 3)
                adjustment.cancel()
                result = await asyncio.gather(adjustment, return_exceptions=True)
                assert isinstance(result[0], BaseException)
    finally:
        if adjustment is not None:
            adjustment.cancel()
            await asyncio.gather(adjustment, return_exceptions=True)
        await runtime.close()

    config_path, data_dir = runtime.config.path, runtime.data_dir
    if outcome == "blocked":
        # A genuine persisted binding change while the original application is closed.
        ConfigFile(config_path).update({"channels": {"onebot": {"owner_ids": ["8"]}}})
    main_seen: list[list[Message]] = []
    worker_seen: list[list[Message]] = []
    allow_replan, resumed_worker_entered = asyncio.Event(), asyncio.Event()
    hold_resumed_worker = asyncio.Event()

    class ReopenedMain(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            main_seen.append(list(messages))
            if len(main_seen) == 1:
                await allow_replan.wait()
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "read-current", "commitment_read", json.dumps({"identifier": task_id})
                        )
                    ]
                )
            elif len(main_seen) == 2:
                assert "error" not in tool_result(messages, "read-current")
                assert any(
                    "完整受控任务说明" in message.content
                    and requirement in message.content
                    and '"epoch":2' in message.content
                    for message in messages
                )
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "approve-current",
                            "task_approve_plan",
                            observed_epoch=2,
                            plan=plan,
                            targets=[worker_id],
                        )
                    ]
                )
            else:
                assert len(main_seen) == 3
                approved = tool_result(messages, "approve-current")
                assert approved["current"]["control"]["paused"] is False
                turn = ModelTurn(content=followup)
            yield StreamEvent("done", turn=turn)

    class ResumedWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            worker_seen.append(list(messages))
            assert any(
                requirement in message.content and plan in message.content for message in messages
            )
            resumed_worker_entered.set()
            await hold_resumed_worker.wait()
            yield StreamEvent("done", turn=ModelTurn(content="不属于本片验收的队友完成结果"))

    def reopened_factory(config: ModelConfig) -> FakeClient:
        return ResumedWorker([], []) if config.model == "worker" else ReopenedMain([], [])

    reopened = Runtime(ConfigFile(config_path), data_dir, client_factory=reopened_factory)  # type: ignore[arg-type]
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    followup_id = ""
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert reopened.main_session()["id"] == main_id
            assert reopened.team.get(worker_id)["commitment_id"] == task_id
            assert reopened.team.get(worker_id)["reply_origin"] == original_worker_origin
            assert (await phone(web, adjustment_text, 103)).status_code == 200
            assert_one_replan(reopened, task_id, main_id)
            receipts_before = reopened.store.list("task_control_receipt")
            replan_receipt = next(
                receipt for receipt in receipts_before if receipt["action"] == "request_replan"
            )
            ingress_before = reopened.store.db.execute(
                "SELECT payload FROM ingress WHERE id=?", (replan_receipt["ingress_id"],)
            ).fetchone()[0]
            # The other sender is also a current owner: the original event's
            # envelope, not merely owner membership, must keep its destination.
            assert (await phone(web, adjustment_text, 103, sender=8)).status_code == 409
            assert reopened.store.list("task_control_receipt") == receipts_before
            assert (
                reopened.store.db.execute(
                    "SELECT payload FROM ingress WHERE id=?", (replan_receipt["ingress_id"],)
                ).fetchone()[0]
                == ingress_before
            )
            replan_run_id = reopened.run_ids[main_id]
            allow_replan.set()
            await eventually(reopened, lambda: bool(completed(reopened, followup)))
            await asyncio.wait_for(resumed_worker_entered.wait(), 3)
            assert await reopened.wait(replan_run_id) == followup
            assert len(main_seen) == 3 and len(worker_seen) == 1
            current = reopened_app.state.local_services.commitments.get(task_id)
            assert current["brief"]["constraints"] == ["不可部署", requirement]
            assert current["control"]["epoch"] == current["control"]["approved_epoch"] == 2
            assert current["control"]["plan"]["text"] == plan
            restored_worker = reopened.team.get(worker_id)
            assert restored_worker["id"] == worker_id and restored_worker["parent_id"] == main_id
            assert restored_worker["generation"] == original_generation + 1
            assert restored_worker["reply_origin"] == original_worker_origin
            deliveries = reopened.store.list("task_plan_delivery")
            assert len(deliveries) == 1 and deliveries[0]["state"] == "delivered"
            assert deliveries[0]["target_session_id"] == worker_id
            notices = [
                item
                for item in reopened.store.messages(worker_id)
                if item.get("principal") == "system:task_control"
            ]
            assert len(notices) == 1 and notices[0]["task_control"]["epoch"] == 2
            assert reopened.store.get("team_report", paused_report_id)["status"] == "delivered"
            assert any(
                item.get("principal") == "agent:" + worker_id
                and item.get("reply_origin") == original_worker_origin
                for item in reopened.store.messages(main_id)
            )
            final = completed(reopened, followup)
            assert len(final) == 1
            visible = await web.get(f"/api/sessions/{main_id}")
            assert visible.status_code == 200
            assert any(item.get("content") == followup for item in visible.json()["messages"])
            manager = reopened_app.state.channel_application.manager
            await processed_reply(reopened, manager, final[0]["id"])
            replies = [item for item in manager.deliveries() if delivery_text(item) == followup]
            assert len(replies) == 1, "真实重开后的最终重规划说明必须进入本次发令私聊 outbox"
            reply = await terminal(manager, replies[0]["id"])
            followup_id = reply["id"]
            assert reply["state"] == outcome
            assert reply["payload"]["automatic"] is True
            assert reply["route"]["account_id"] == "123"
            assert reply["route"]["target"] == reply["route"]["sender_id"] == "7"
            assert final[0]["reply_origin"]["kind"] == "channel"
            assert final[0]["reply_origin"]["route"] == reply["route"]
            assert len(followup_attempts) == (0 if outcome == "blocked" else 1)
            assert all(
                str(item["user_id"]) == "7"
                for item in requests
                if any(segment["data"]["text"] == followup for segment in item["message"])
            )
            assert (await phone(web, adjustment_text, 103)).status_code == 200
            assert_one_replan(reopened, task_id, main_id)
            assert len(completed(reopened, followup)) == 1 and len(main_seen) == 3
            assert len(reopened.store.list("task_plan_delivery")) == 1
            assert len(reopened.store.messages(main_id)) <= 32
    finally:
        await reopened.close()

    if outcome == "unknown":
        unexpected: list[list[Message]] = []

        def never_model(config: ModelConfig) -> FakeClient:
            return FakeClient([], unexpected)

        recovered = Runtime(ConfigFile(config_path), data_dir, client_factory=never_model)  # type: ignore[arg-type]
        recovered_app = create_app(recovered.config, recovered.data_dir, recovered)
        try:
            async with (
                recovered_app.router.lifespan_context(recovered_app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=recovered_app),
                    base_url="http://localhost:8765",
                ) as web,
            ):
                manager = recovered_app.state.channel_application.manager
                assert manager.delivery(followup_id)["state"] == "unknown"
                assert (await phone(web, adjustment_text, 103)).status_code == 200
                known_deliveries = {item["id"] for item in manager.deliveries()}
                assert (await phone(web, "/任务", 104)).status_code == 200
                status_reply = next(
                    item
                    for item in manager.deliveries()
                    if item["id"] not in known_deliveries and "任务列表：" in delivery_text(item)
                )
                assert (await terminal(manager, status_reply["id"]))["state"] == "sent"
                assert manager.delivery(followup_id)["state"] == "unknown"
                assert len(followup_attempts) == 1 and not unexpected
                assert_one_replan(recovered, task_id, main_id)
                assert len(recovered.store.list("task_plan_delivery")) == 1
        finally:
            await recovered.close()
