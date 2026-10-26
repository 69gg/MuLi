"""An offered answer remains revocable until its original child consumes it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_ownership_policy import assigned, receive
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.providers import Message, ModelTurn, StreamEvent


async def test_new_owner_revokes_an_offered_answer_after_its_own_run_has_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        main_id = runtime.main_session()["id"]
        incoming = "请先咨询主助手，仅本次仍有效的回答才可以发给我。"
        question = "请只读确认资料中的建议，等待本次原运行接收答案。"
        offered_answer = "OFFERED-ANSWER-新主人输入后不得再兑现"
        owner_text = "网页主人现在提交独立新请求，请按普通主轮完成。"
        owner_final = "新的网页主人请求已经处理完毕。"
        child_final = "此前咨询答案已撤销，本轮未向渠道发送。"
        held, release, replayed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        main_runs: dict[str, str] = {}
        child_runs: list[str] = []
        available: list[bool] = []
        results: list[dict[str, Any]] = []
        consumed_after: list[tuple[int, str]] = []
        consultation_times: list[float] = []
        requests: list[str] = []
        rounds = {"worker": 0, "control": 0, "owner": 0}
        deferred = 0
        intercepted = False
        original_notify = runtime.bus.notify

        async def gated_notify() -> None:
            nonlocal deferred, intercepted
            control_id = main_runs.get("control")
            control = runtime.store.get("run", control_id) if control_id else None
            if (
                not intercepted
                and control is not None
                and control["status"] == "completed"
                and main_id not in runtime.tasks
                and main_id not in runtime.run_ids
            ):
                intercepted = True
                held.set()
                try:
                    await asyncio.wait_for(release.wait(), timeout=3)
                finally:
                    # These are post-commit wakeups, not execution effects.
                    # Replay every deferred call before releasing the fixture.
                    release.set()
                    count, deferred = deferred + 1, 0
                    for _ in range(count):
                        await original_notify()
                    replayed.set()
                return
            if held.is_set() and not release.is_set():
                deferred += 1
                return
            await original_notify()

        monkeypatch.setattr(runtime.bus, "notify", gated_notify)

        async def platform(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            assert request.url.host == "onebot.invalid"
            assert request.url.path == "/send_private_msg"
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9732}}
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
                assert len(messages) <= 32 and all(not message.images for message in messages)
                names = {tool["function"]["name"] for tool in tools}
                if self.role == "worker":
                    rounds["worker"] += 1
                    child = runtime.team.get(child_id)
                    assert child["generation"] == 2 and child["parent_id"] == main_id
                    run_id = runtime.run_ids[child_id]
                    if rounds["worker"] == 1:
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
                        consultation_times.append(asyncio.get_running_loop().time())
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "offered-consult",
                                    "team_consult",
                                    question=question,
                                    wait_seconds=2,
                                )
                            ]
                        )
                    elif rounds["worker"] == 2:
                        assert child_runs == [run_id]
                        result = tool_result(messages, "offered-consult")
                        results.append(result)
                        consultation_times.append(asyncio.get_running_loop().time())
                        owner = runtime.store.get("run", main_runs["owner"])
                        assert owner is not None
                        consumed_after.append(
                            (runtime.store.pending_inputs(main_id), owner["status"])
                        )
                        if result.get("answer") == offered_answer:
                            turn = ModelTurn(
                                tool_calls=[
                                    call(
                                        "send-old-offer",
                                        "channel_send",
                                        channel="onebot",
                                        target="7",
                                        text=offered_answer,
                                        idempotency_key="revoked-offer",
                                    )
                                ]
                            )
                        else:
                            turn = ModelTurn(content=child_final)
                    else:
                        assert rounds["worker"] == 3
                        tool_result(messages, "send-old-offer")
                        turn = ModelTurn(content="错误地兑现了已撤销的咨询答案。")
                else:
                    assert self.role == "main"
                    owner_messages = [
                        message
                        for message in runtime.store.messages(main_id)
                        if message["role"] == "user" and message["content"] == owner_text
                    ]
                    owner_ids = {message["id"] for message in owner_messages}
                    if any(message.source_message_id in owner_ids for message in messages):
                        rounds["owner"] += 1
                        assert rounds["owner"] == 1
                        assert (
                            len(owner_messages) == 1 and owner_messages[0]["principal"] == "owner"
                        )
                        main_runs["owner"] = runtime.run_ids[main_id]
                        assert main_runs["owner"] != main_runs["control"]
                        turn = ModelTurn(content=owner_final)
                    else:
                        rounds["control"] += 1
                        assert rounds["control"] == 1
                        assert any(question in message.content for message in messages)
                        known = runtime.session_tools(main_id)
                        known.update(
                            {tool.name: tool for tool in runtime.team.tools(main_id, "yolo")}
                        )
                        assert names <= {
                            name for name, tool in known.items() if tool.risk == "read"
                        }
                        main_runs["control"] = runtime.run_ids[main_id]
                        turn = ModelTurn(content=offered_answer)
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            await receive(web, incoming, number=732)
            await asyncio.wait_for(held.wait(), timeout=3)
            assert available == [True] and results == []
            control = runtime.store.get("run", main_runs["control"])
            assert control is not None and control["status"] == "completed"
            assert control["result"] == offered_answer
            assert main_id not in runtime.tasks and main_id not in runtime.run_ids
            assert runtime.run_ids[child_id] == child_runs[0]

            response = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": owner_text, "mode": "yolo"},
            )
            assert response.status_code == 200
            owner_run_id = response.json()["run_id"]
            assert isinstance(owner_run_id, str)
            assert await asyncio.wait_for(runtime.wait(owner_run_id), timeout=3) == owner_final
            assert main_runs["owner"] == owner_run_id
            assert completed(runtime, owner_final)
            assert runtime.store.pending_inputs(main_id) == 0
            assert results == [] and main_id not in runtime.tasks
            assert deferred > 0
            owner_run = runtime.store.get("run", main_runs["owner"])
            assert owner_run is not None and owner_run["status"] == "completed"
            release.set()
            await eventually(runtime, lambda: runtime.team.get(child_id)["status"] == "completed")
            assert len(results) == 1 and results[0].get("answer") != offered_answer
            assert "error" in results[0]
            assert len(consultation_times) == 2
            assert consultation_times[1] - consultation_times[0] < 2
            assert consumed_after == [(0, "completed")]
            assert rounds == {"worker": 2, "control": 1, "owner": 1}
            child = runtime.team.get(child_id)
            assert child["generation"] == 2 and child["run_id"] == child_runs[0]
            assert child["result"] == child_final
            assert runtime.store.get("run", main_runs["owner"]) == owner_run
            archived_owner = next(
                message
                for message in runtime.store.messages(main_id)
                if message["role"] == "user" and message["content"] == owner_text
            )
            assert archived_owner["run_id"] == main_runs["owner"] != main_runs["control"]
            assert app.state.channel_application.manager.deliveries() == [] and requests == []
        finally:
            release.set()
            if intercepted:
                await asyncio.wait_for(replayed.wait(), timeout=3)
