"""Late control answers cannot consume a newly admitted owner request."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_ownership_policy import assigned, receive
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.providers import Message, ModelTurn, StreamEvent


async def test_new_owner_input_revokes_a_late_consult_answer_without_being_claimed_by_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        main_id = runtime.main_session()["id"]
        incoming = "请先咨询主助手；只有拿到本次有效答复后，才能显式发回当前私聊。"
        question = "请只读确认原资料的建议；在答复期间若主人有新请求，本次答案就应作废。"
        late_answer = "CONSULT-LATE-ANSWER-不得作为有效答复外发"
        owner_text = "这是网页主人新请求，请在普通主轮回答，不要把它当作咨询问题。"
        owner_final = "网页主人的新请求已由独立普通主轮处理。"
        child_final = "本次咨询已失效，没有向私聊发送迟到答案。"
        invalid_final = "收到了旧答案并尝试外发；此路径应被本测试拒绝。"
        child_requested = asyncio.Event()
        control_entered, release_control = asyncio.Event(), asyncio.Event()
        owner_admitted = asyncio.Event()
        control_runs: list[str] = []
        child_runs: list[str] = []
        owner_runs: list[str] = []
        results: list[dict[str, Any]] = []
        available: list[bool] = []
        sent: list[dict[str, Any]] = []
        rounds: dict[str, int] = {"worker": 0, "owner": 0, "control": 0}

        async def platform(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "onebot.invalid"
            assert request.url.path == "/send_private_msg"
            sent.append(json.loads(request.content))
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9731}}
            )

        transport = httpx.MockTransport(platform)

        async def offline_http(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            return await transport.handle_async_request(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                assert len(messages) <= 32 and all(not item.images for item in messages)
                names = {tool["function"]["name"] for tool in tools}
                if self.role == "worker":
                    rounds["worker"] += 1
                    agent = runtime.team.get(child_id)
                    assert agent["parent_id"] == main_id and agent["generation"] == 2
                    current_run = runtime.run_ids[child_id]
                    if rounds["worker"] == 1:
                        actual = next(
                            item
                            for item in reversed(runtime.store.messages(child_id))
                            if item["role"] == "user" and item["content"] == incoming
                        )
                        assert actual["principal"] == "owner"
                        assert actual["source"] == "channel:onebot:123"
                        assert actual["run_id"] == current_run
                        assert actual["channel_ownership"]["agent_id"] == child_id
                        assert any(item.source_message_id == actual["id"] for item in messages)
                        child_runs.append(current_run)
                        available.append("team_consult" in names)
                        child_requested.set()
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "await-revocable-consult",
                                    "team_consult",
                                    question=question,
                                    wait_seconds=2,
                                )
                            ]
                        )
                    elif rounds["worker"] == 2:
                        assert child_runs == [current_run]
                        result = tool_result(messages, "await-revocable-consult")
                        results.append(result)
                        if result.get("answer") == late_answer:
                            # An incorrectly accepted answer drives a real send;
                            # a mock handler must not hide the resulting outbox.
                            turn = ModelTurn(
                                tool_calls=[
                                    call(
                                        "send-invalidated-answer",
                                        "channel_send",
                                        channel="onebot",
                                        target="7",
                                        text=result["answer"],
                                        idempotency_key="invalidated-consult-answer",
                                    )
                                ]
                            )
                        else:
                            turn = ModelTurn(content=child_final)
                    else:
                        assert rounds["worker"] == 3
                        tool_result(messages, "send-invalidated-answer")
                        turn = ModelTurn(content=invalid_final)
                else:
                    assert self.role == "main"
                    actual_owner = [
                        item
                        for item in runtime.store.messages(main_id)
                        if item["role"] == "user" and item["content"] == owner_text
                    ]
                    owner_ids = {item["id"] for item in actual_owner}
                    if any(item.source_message_id in owner_ids for item in messages):
                        rounds["owner"] += 1
                        assert rounds["owner"] == 1
                        assert len(actual_owner) == 1 and actual_owner[0]["principal"] == "owner"
                        assert actual_owner[0]["source"] == "web"
                        owner_runs.append(runtime.run_ids[main_id])
                        assert owner_runs[0] not in control_runs
                        turn = ModelTurn(content=owner_final)
                    else:
                        rounds["control"] += 1
                        assert rounds["control"] == 1
                        assert any(question in item.content for item in messages)
                        known = runtime.session_tools(main_id)
                        known.update(
                            {tool.name: tool for tool in runtime.team.tools(main_id, "yolo")}
                        )
                        assert names <= {
                            name for name, tool in known.items() if tool.risk == "read"
                        }
                        control_runs.append(runtime.run_ids[main_id])
                        assert runtime.run_ids[child_id] == child_runs[0]
                        control_entered.set()
                        # A provider may deliver a result after cancellation. Keep
                        # that boundary real without cancelling the child's run.
                        while not release_control.is_set():
                            try:
                                await release_control.wait()
                            except asyncio.CancelledError:
                                continue
                        turn = ModelTurn(content=late_answer)
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]

        def observe_owner(message: dict[str, Any], trusted_owner: bool) -> None:
            if trusted_owner and message.get("content") == owner_text:
                owner_admitted.set()

        runtime.message_hooks.append(observe_owner)
        submission: asyncio.Task[httpx.Response] | None = None
        try:
            await receive(web, incoming, number=731)
            await asyncio.wait_for(child_requested.wait(), timeout=3)
            assert available == [True], results
            await asyncio.wait_for(control_entered.wait(), timeout=3)
            assert control_entered.is_set(), results
            assert runtime.team.get(child_id)["status"] == "running"
            assert runtime.run_ids[child_id] == child_runs[0]
            assert results == [] and app.state.channel_application.manager.deliveries() == []
            submission = asyncio.create_task(
                web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={"content": owner_text, "mode": "yolo"},
                )
            )

            await asyncio.wait_for(owner_admitted.wait(), timeout=3)
            row = runtime.store.db.execute(
                "SELECT state,run_id FROM ingress WHERE session_id=? "
                "AND json_extract(payload,'$.content')=?",
                (main_id, owner_text),
            ).fetchone()
            assert row is not None and tuple(row) == ("pending", None)
            assert owner_runs == []
            release_control.set()
            response = await asyncio.wait_for(submission, timeout=3)
            assert response.status_code == 200
            await eventually(
                runtime,
                lambda: (
                    bool(completed(runtime, owner_final))
                    and runtime.team.get(child_id)["status"] == "completed"
                ),
            )
            assert len(results) == 1 and results[0].get("answer") != late_answer
            assert rounds == {"worker": 2, "owner": 1, "control": 1}
            child = runtime.team.get(child_id)
            assert child["generation"] == 2 and child["run_id"] == child_runs[0]
            assert child["result"] == child_final
            report = runtime.store.get("team_report", child_runs[0])
            assert report is not None and report["status"] == "local"
            assert report["execution_status"] == "completed"
            persisted = next(
                item
                for item in runtime.store.messages(main_id)
                if item["role"] == "user" and item["content"] == owner_text
            )
            assert len(owner_runs) == len(control_runs) == 1
            assert persisted["run_id"] == owner_runs[0] != control_runs[0]
            owner_result = await asyncio.wait_for(runtime.wait(owner_runs[0]), timeout=3)
            assert owner_result == owner_final
            assert runtime.store.get("run", owner_runs[0])["status"] == "completed"
            assert runtime.store.pending_inputs(main_id) == 0
            assert app.state.channel_application.manager.deliveries() == [] and sent == []
        finally:
            release_control.set()
            runtime.message_hooks.remove(observe_owner)
            if submission is not None:
                if not submission.done():
                    submission.cancel()
                await asyncio.gather(submission, return_exceptions=True)
