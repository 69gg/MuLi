"""Real owner and child tools must not create a two-way wait dependency."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channel_ownership_policy import assigned, receive
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.providers import Message, ModelTurn, StreamEvent


async def test_child_consult_rejects_a_cycle_with_its_main_owners_real_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        main_id = runtime.main_session()["id"]
        incoming = "本轮请先向主助手咨询；若等待关系形成环，只说明错误并正常结束。"
        question = "主助手正在等本队友完成时，不应再等待主助手的回答。"
        owner_text = "请等待正在处理当前私聊的原队友完成，不要另派任务或取消队友。"
        owner_final = "原队友已经正常完成，主助手的真实等待已结束。"
        child_final = "咨询等待形成循环，已按工具错误处理并正常完成。"
        child_entered, main_waiting, release_child = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        child_runs: list[str] = []
        results: list[dict[str, Any]] = []
        wait_results: list[dict[str, Any]] = []
        available: list[bool] = []
        network_requests: list[str] = []
        rounds = {"main": 0, "child": 0}
        main_wait_observations = 0
        prior_main_runs = {
            row[0]
            for row in runtime.store.db.execute(
                "SELECT id FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=?",
                (main_id,),
            )
        }
        original_wait_for = runtime.bus.changed.wait_for

        async def observed_wait_for(predicate: Callable[[], bool]) -> bool:
            nonlocal main_wait_observations
            if asyncio.current_task() is runtime.tasks.get(main_id):
                calls = [
                    tool_call
                    for message in runtime.store.messages(main_id)
                    for tool_call in message.get("tool_calls", [])
                    if tool_call["id"] == "wait-owned-child"
                ]
                assert len(calls) == 1 and calls[0]["name"] == "wait_agents"
                # The real TeamWait.wait registers its edge before entering
                # this Condition. Only observe; delegate the actual wait.
                main_wait_observations += 1
                main_waiting.set()
            return await original_wait_for(predicate)

        monkeypatch.setattr(runtime.bus.changed, "wait_for", observed_wait_for)

        async def no_network(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> NoReturn:
            network_requests.append(str(request.url))
            raise AssertionError("循环等待旅程不允许真实网络或平台外发")

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
                        child_entered.set()
                        await asyncio.wait_for(release_child.wait(), timeout=3)
                        assert main_waiting.is_set()
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "consult-waiting-parent",
                                    "team_consult",
                                    question=question,
                                    wait_seconds=2,
                                )
                            ]
                        )
                    else:
                        assert rounds["child"] == 2 and child_runs == [run_id]
                        results.append(tool_result(messages, "consult-waiting-parent"))
                        turn = ModelTurn(content=child_final)
                else:
                    assert self.role == "main"
                    rounds["main"] += 1
                    if rounds["main"] == 1:
                        actual = next(
                            message
                            for message in runtime.store.messages(main_id)
                            if message["role"] == "user" and message["content"] == owner_text
                        )
                        assert actual["principal"] == "owner" and actual["source"] == "web"
                        assert any(
                            message.source_message_id == actual["id"] for message in messages
                        )
                        assert "wait_agents" in names
                        assert runtime.team.get(child_id)["status"] == "running"
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "wait-owned-child", "wait_agents", targets=[child_id], timeout=2
                                )
                            ]
                        )
                    else:
                        assert rounds["main"] == 2
                        wait_results.append(tool_result(messages, "wait-owned-child"))
                        turn = ModelTurn(content=owner_final)
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            await receive(web, incoming, number=734)
            await asyncio.wait_for(child_entered.wait(), timeout=3)
            response = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": owner_text, "mode": "yolo"},
            )
            assert response.status_code == 200
            main_run_id = response.json()["run_id"]
            assert isinstance(main_run_id, str)
            await asyncio.wait_for(main_waiting.wait(), timeout=3)
            assert runtime.store.get("run", main_run_id)["status"] == "running"
            assert runtime.team.get(child_id)["status"] == "running"
            release_child.set()
            assert await asyncio.wait_for(runtime.wait(main_run_id), timeout=3) == owner_final
            await eventually(
                runtime, lambda: runtime.team.get(child_id)["status"] not in {"queued", "running"}
            )
            assert available == [True] and main_wait_observations >= 1
            assert len(results) == 1 and "error" in results[0] and "answer" not in results[0]
            assert "循环" in results[0]["error"]
            assert len(wait_results) == 1 and wait_results[0]["timed_out"] is False
            waited = [agent for agent in wait_results[0]["agents"] if agent["id"] == child_id]
            assert len(waited) == 1
            assert waited[0]["status"] == "completed" and waited[0]["generation"] == 2
            assert waited[0]["run_id"] == child_runs[0]
            child = runtime.team.get(child_id)
            assert child["status"] == "completed" and child["result"] == child_final
            assert child["generation"] == 2 and child["run_id"] == child_runs[0]
            assert runtime.store.get("run", child_runs[0])["status"] == "completed"
            report = runtime.store.get("team_report", child_runs[0])
            assert report is not None and report["status"] == "local"
            assert report["execution_status"] == "completed"
            current_main_runs = {
                row[0]
                for row in runtime.store.db.execute(
                    "SELECT id FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=?",
                    (main_id,),
                )
            }
            assert current_main_runs - prior_main_runs == {main_run_id}
            assert rounds == {"main": 2, "child": 2}
            assert app.state.channel_application.manager.deliveries() == []
            assert network_requests == []
        finally:
            release_child.set()
