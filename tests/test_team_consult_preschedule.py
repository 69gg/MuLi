"""Cancellation before a real control Task starts must release its session slot."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channel_ownership_policy import assigned, receive
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.providers import Message, ModelTurn, StreamEvent


async def test_control_cancelled_before_first_schedule_releases_the_slot_for_a_real_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        main_id = runtime.main_session()["id"]
        incoming = "请咨询主助手；如果咨询取消，正常说明错误并结束，不要发送渠道消息。"
        question = "这次咨询若尚未启动就取消，不得遗留主会话运行槽。"
        owner_text = "网页主人提交新的普通请求，请正常处理，不要被旧咨询清理取消。"
        owner_final = "新的主人普通请求正常完成。"
        child_final = "咨询启动前已取消，原渠道运行正常结束。"
        bound, owner_entered, release_owner = asyncio.Event(), asyncio.Event(), asyncio.Event()
        control_ids: list[str] = []
        control_tasks: list[asyncio.Task[str]] = []
        child_runs: list[str] = []
        owner_runs: list[str] = []
        owner_input_notifications: list[tuple[str, str, str]] = []
        results: list[dict[str, Any]] = []
        available: list[bool] = []
        network_requests: list[str] = []
        rounds = {"child": 0, "owner": 0, "control": 0}
        submission: asyncio.Task[httpx.Response] | None = None
        original_bind = runtime.team.consult.bind

        def cancel_after_real_bind(request: Any, run_id: str, task: asyncio.Task[str]) -> None:
            nonlocal submission
            original_bind(request, run_id, task)
            run = runtime.store.get("run", run_id)
            assert run is not None and run["session_id"] == main_id and run["status"] == "queued"
            same_task = runtime.tasks.get(main_id) is task
            assert same_task and runtime.run_ids[main_id] == run_id
            assert not task.done() and not task.cancelling()
            control_ids.append(run_id)
            control_tasks.append(task)
            assert task.cancel()
            assert task.cancelling() and not task.done()
            # Scheduling the genuine HTTP request does not establish when its
            # middleware admits input relative to the old Task's done callback.
            submission = asyncio.create_task(
                web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={"content": owner_text, "mode": "yolo"},
                )
            )
            bound.set()

        monkeypatch.setattr(runtime.team.consult, "bind", cancel_after_real_bind)

        def observe_owner_input(message: dict[str, Any], trusted_owner: bool) -> None:
            if trusted_owner and message.get("content") == owner_text:
                owner_input_notifications.append(
                    (message["id"], message["session_id"], message["source"])
                )

        runtime.message_hooks.append(observe_owner_input)

        async def no_network(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> NoReturn:
            network_requests.append(str(request.url))
            raise AssertionError("启动前取消旅程不允许真实网络或外发")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                assert len(messages) <= 32 and all(not message.images for message in messages)
                names = {tool["function"]["name"] for tool in tools}
                if self.role == "worker":
                    rounds["child"] += 1
                    child = runtime.team.get(child_id)
                    assert child["parent_id"] == main_id and child["generation"] == 2
                    run_id = runtime.run_ids[child_id]
                    if rounds["child"] == 1:
                        actual = next(
                            message
                            for message in runtime.store.messages(child_id)
                            if message["role"] == "user" and message["content"] == incoming
                        )
                        assert actual["principal"] == "owner"
                        assert actual["source"] == "channel:onebot:123"
                        assert actual["run_id"] == run_id
                        assert actual["channel_ownership"]["agent_id"] == child_id
                        assert any(
                            message.source_message_id == actual["id"] for message in messages
                        )
                        child_runs.append(run_id)
                        available.append("team_consult" in names)
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "cancel-before-control-start",
                                    "team_consult",
                                    question=question,
                                    wait_seconds=2,
                                )
                            ]
                        )
                    else:
                        assert rounds["child"] == 2 and child_runs == [run_id]
                        results.append(tool_result(messages, "cancel-before-control-start"))
                        turn = ModelTurn(content=child_final)
                else:
                    assert self.role == "main"
                    actual = [
                        message
                        for message in runtime.store.messages(main_id)
                        if message["role"] == "user" and message["content"] == owner_text
                    ]
                    ids = {message["id"] for message in actual}
                    if any(message.source_message_id in ids for message in messages):
                        rounds["owner"] += 1
                        assert rounds["owner"] == 1
                        assert len(actual) == 1 and actual[0]["principal"] == "owner"
                        assert actual[0]["source"] == "web"
                        owner_runs.append(runtime.run_ids[main_id])
                        assert owner_runs[0] != control_ids[0]
                        owner_entered.set()
                        await asyncio.wait_for(release_owner.wait(), timeout=3)
                        turn = ModelTurn(content=owner_final)
                    else:
                        rounds["control"] += 1
                        turn = ModelTurn(content="本不应首次运行的咨询控制模型。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            await receive(web, incoming, number=735)
            await asyncio.wait_for(bound.wait(), timeout=3)
            assert available == [True] and len(control_ids) == len(control_tasks) == 1
            assert submission is not None
            response = await asyncio.wait_for(submission, timeout=3)
            assert response.status_code == 200
            assert response.json()["queued"] is True
            await asyncio.wait_for(owner_entered.wait(), timeout=3)
            await eventually(
                runtime, lambda: runtime.team.get(child_id)["status"] not in {"queued", "running"}
            )
            assert control_tasks[0].done() and control_tasks[0].cancelled()
            control = runtime.store.get("run", control_ids[0])
            assert control is not None and control["status"] == "cancelled"
            assert rounds["control"] == 0
            assert runtime.run_ids[main_id] == owner_runs[0]
            owner_task = runtime.tasks[main_id]
            assert not owner_task.done() and not owner_task.cancelling()
            assert len(results) == 1 and "error" in results[0] and "answer" not in results[0]
            child = runtime.team.get(child_id)
            assert child["status"] == "completed" and child["result"] == child_final
            assert child["generation"] == 2 and child["run_id"] == child_runs[0]
            assert runtime.store.get("run", child_runs[0])["status"] == "completed"
            assert runtime.store.pending_inputs(main_id) == 0

            release_owner.set()
            assert await asyncio.wait_for(runtime.wait(owner_runs[0]), timeout=3) == owner_final
            assert runtime.store.get("run", owner_runs[0])["status"] == "completed"
            assert main_id not in runtime.tasks and main_id not in runtime.run_ids
            persisted = next(
                message
                for message in runtime.store.messages(main_id)
                if message["role"] == "user" and message["content"] == owner_text
            )
            assert persisted["run_id"] == owner_runs[0] != control_ids[0]
            assert owner_input_notifications == [(persisted["id"], main_id, "web")]
            assert rounds == {"child": 2, "owner": 1, "control": 0}
            assert app.state.channel_application.manager.deliveries() == []
            assert network_requests == []
        finally:
            release_owner.set()
            runtime.message_hooks.remove(observe_owner_input)
            if submission is not None:
                if not submission.done():
                    submission.cancel()
                await asyncio.gather(submission, return_exceptions=True)
