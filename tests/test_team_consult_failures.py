"""A failed control model must not cancel its caller or another real child."""

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


async def test_consult_model_failure_only_returns_an_error_to_its_original_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        main_id = runtime.main_session()["id"]
        owner_text = "请派一名独立队友处理另一项工作，然后正常结束本轮，不要停止任何队友。"
        owner_final = "独立队友已真实启动，本轮主请求完成。"
        unrelated_name = "独立工作中的真实队友"
        unrelated_prompt = "UNRELATED-CONSULT-FAILURE-WORK：保持本任务运行，最后正常报告。"
        unrelated_final = "独立工作已正常完成，没有被另一项咨询失败取消。"
        incoming = "请咨询主助手；若咨询失败，说明错误并结束本轮，不要外发。"
        question = "请只读核对建议；本次控制模型故障不能停止其他真实任务。"
        child_final = "咨询失败只作为本次工具错误处理，当前渠道运行正常结束。"
        unrelated_entered, release_unrelated = asyncio.Event(), asyncio.Event()
        unrelated_cancelled = asyncio.Event()
        unrelated_ids: list[str] = []
        unrelated_runs: list[str] = []
        child_runs: list[str] = []
        control_runs: list[str] = []
        consultation_results: list[dict[str, Any]] = []
        available: list[bool] = []
        network_requests: list[str] = []
        rounds = {"main": 0, "child": 0, "unrelated": 0, "control": 0}

        async def no_network(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> NoReturn:
            network_requests.append(str(request.url))
            raise AssertionError("本旅程不允许平台外发或真实网络")

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
                if self.role == "main":
                    if any(question in message.content for message in messages):
                        rounds["control"] += 1
                        assert rounds["control"] == 1
                        control_runs.append(runtime.run_ids[main_id])
                        assert runtime.store.get("run", owner_run_id)["status"] == "completed"
                        assert runtime.team.get(unrelated_ids[0])["status"] == "running"
                        assert runtime.run_ids[unrelated_ids[0]] == unrelated_runs[0]
                        known = runtime.session_tools(main_id)
                        known.update(
                            {tool.name: tool for tool in runtime.team.tools(main_id, "yolo")}
                        )
                        assert names <= {
                            name for name, tool in known.items() if tool.risk == "read"
                        }
                        raise RuntimeError("受控咨询模型故障")
                    rounds["main"] += 1
                    if rounds["main"] == 1:
                        assert "spawn_agent" in names
                        actual = next(
                            message
                            for message in runtime.store.messages(main_id)
                            if message["role"] == "user" and message["content"] == owner_text
                        )
                        assert actual["principal"] == "owner" and actual["source"] == "web"
                        assert any(
                            message.source_message_id == actual["id"] for message in messages
                        )
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "spawn-unrelated-worker",
                                    "spawn_agent",
                                    task_name=unrelated_name,
                                    prompt=unrelated_prompt,
                                )
                            ]
                        )
                    elif rounds["main"] == 2:
                        spawned = tool_result(messages, "spawn-unrelated-worker")
                        assert "error" not in spawned and spawned["id"] != child_id
                        unrelated_ids.append(spawned["id"])
                        assert runtime.team.get(spawned["id"])["parent_id"] == main_id
                        turn = ModelTurn(content=owner_final)
                    else:
                        # The unrelated child's ordinary terminal report may
                        # wake its parent after the isolation assertions.
                        turn = ModelTurn(content="已收到独立队友正常完成的报告。")
                else:
                    assert self.role == "worker"
                    if any(
                        unrelated_prompt in message.content
                        for message in messages
                        if message.role == "user"
                    ):
                        rounds["unrelated"] += 1
                        assert rounds["unrelated"] == 1
                        matches = [
                            agent
                            for agent in runtime.team.list()
                            if agent["name"] == unrelated_name
                        ]
                        assert len(matches) == 1
                        unrelated = matches[0]
                        assert unrelated["parent_id"] == main_id
                        assert unrelated["generation"] == 1 and unrelated["status"] == "running"
                        unrelated_runs.append(runtime.run_ids[unrelated["id"]])
                        assert unrelated["run_id"] == unrelated_runs[0]
                        unrelated_entered.set()
                        try:
                            await asyncio.wait_for(release_unrelated.wait(), timeout=3)
                        except asyncio.CancelledError:
                            unrelated_cancelled.set()
                            raise
                        turn = ModelTurn(content=unrelated_final)
                    else:
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
                                        "failed-control",
                                        "team_consult",
                                        question=question,
                                        wait_seconds=2,
                                    )
                                ]
                            )
                        else:
                            assert rounds["child"] == 2 and child_runs == [run_id]
                            consultation_results.append(tool_result(messages, "failed-control"))
                            turn = ModelTurn(content=child_final)
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            response = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": owner_text, "mode": "yolo"},
            )
            assert response.status_code == 200
            owner_run_id = response.json()["run_id"]
            assert isinstance(owner_run_id, str)
            await asyncio.wait_for(unrelated_entered.wait(), timeout=3)
            assert await asyncio.wait_for(runtime.wait(owner_run_id), timeout=3) == owner_final
            assert len(unrelated_ids) == len(unrelated_runs) == 1
            unrelated_task = runtime.tasks[unrelated_ids[0]]
            assert not unrelated_task.done() and not unrelated_task.cancelling()
            assert runtime.team.get(unrelated_ids[0])["status"] == "running"

            await receive(web, incoming, number=733)
            await eventually(
                runtime,
                lambda: runtime.team.get(child_id)["status"] not in {"queued", "running"},
            )
            child = runtime.team.get(child_id)
            assert child["status"] == "completed"
            assert available == [True]
            assert len(control_runs) == len(consultation_results) == 1
            assert "error" in consultation_results[0] and "answer" not in consultation_results[0]
            control = runtime.store.get("run", control_runs[0])
            assert control is not None and control["status"] == "failed"
            assert "RuntimeError" in control["error"]
            assert child["generation"] == 2 and child["run_id"] == child_runs[0]
            assert child["result"] == child_final
            assert runtime.store.get("run", child_runs[0])["status"] == "completed"
            report = runtime.store.get("team_report", child_runs[0])
            assert report is not None and report["status"] == "local"
            assert report["execution_status"] == "completed"
            assert runtime.store.get("run", owner_run_id)["result"] == owner_final
            assert runtime.store.get("run", owner_run_id)["status"] == "completed"
            assert runtime.team.get(unrelated_ids[0])["status"] == "running"
            same_unrelated_task = runtime.tasks.get(unrelated_ids[0]) is unrelated_task
            assert same_unrelated_task
            assert not unrelated_task.done() and not unrelated_task.cancelling()
            assert not unrelated_cancelled.is_set()
            assert runtime.store.get("run", unrelated_runs[0])["status"] == "running"

            release_unrelated.set()
            assert (
                await asyncio.wait_for(runtime.wait(unrelated_runs[0]), timeout=3)
                == unrelated_final
            )
            await eventually(
                runtime, lambda: runtime.team.get(unrelated_ids[0])["status"] == "completed"
            )
            unrelated = runtime.team.get(unrelated_ids[0])
            assert unrelated["generation"] == 1 and unrelated["run_id"] == unrelated_runs[0]
            assert unrelated["result"] == unrelated_final
            assert not unrelated_cancelled.is_set()
            assert rounds["child"] == 2 and rounds["control"] == rounds["unrelated"] == 1
            assert app.state.channel_application.manager.deliveries() == []
            assert network_requests == []
        finally:
            release_unrelated.set()
