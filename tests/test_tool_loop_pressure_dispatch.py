"""Use actual spawn and workflow tools to reach the shared pressure boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime
from test_team_consult_regular import current_session
from test_teammate_attachment_runtime import call, tool_result
from test_tool_loop_pressure_runtime import HostObserver
from test_tool_loop_pressure_runtime import offline as offline

from muli.config import Persona
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.task_control import OwnerControlRequest
from muli.tools import Tool, parameters


class LeafScript:
    """Only actual messages and fixed arguments drive the leaf model script."""

    def __init__(self, owner_text: str, arguments: list[str]) -> None:
        self.owner_text, self.arguments = owner_text, arguments
        self.index = 0

    def next(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelTurn:
        assert any(
            message.role == "user" and message.content == self.owner_text for message in messages
        )
        assert {"conversation_read", "attachment_read", "context_pressure_probe"} <= {
            tool["function"]["name"] for tool in tools
        }
        if self.index:
            identifier = f"p{self.index - 1}"
            previous = [
                tool_call
                for message in messages
                for tool_call in message.tool_calls
                if tool_call.id == identifier
            ]
            replies = [message for message in messages if message.tool_call_id == identifier]
            assert len(previous) == len(replies) == 1
            assert previous[0].arguments == self.arguments[self.index - 1]
            assert json.loads(replies[0].content) == {"receipt": "saved"}
        if self.index == len(self.arguments):
            return ModelTurn(content="叶节点逐条核查完成。")
        turn = ModelTurn(
            tool_calls=[
                ToolCall(f"p{self.index}", "context_pressure_probe", self.arguments[self.index])
            ]
        )
        self.index += 1
        return turn


@pytest.mark.parametrize("path", ["worker", "nested", "workflow"])
async def test_actual_dispatch_uses_the_shared_pressure_checkpoint(
    tmp_path: Path, path: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    budget, steps = 16000, 12
    leaf_prompt = "压力叶任务：逐条只读核查资料，不外发、不删除、不重做。"
    parent_prompt = "协调父任务：将只读压力核查交给一名子队友，原始限制保持。"
    arguments = [
        json.dumps({"query": f"{index}:" + "q" * (768 - len(f"{index}:"))})
        for index in range(steps)
    ]
    performed: list[tuple[str, str]] = []
    scripts: dict[str, LeafScript] = {}
    observers: dict[str, HostObserver] = {}
    dispatch_rounds: dict[str, int] = {}
    leaf_runs: dict[str, str] = {}
    leaf_done = asyncio.Event()

    async def probe(args: dict[str, Any]) -> dict[str, str]:
        performed.append((current_session(runtime), args["query"]))
        return {"receipt": "saved"}

    def probe_tools(_runtime: Runtime) -> list[Tool]:
        return [
            Tool(
                "context_pressure_probe",
                "只读有限查询并保存完整短回执",
                parameters(
                    {"query": {"type": "string", "minLength": 1, "maxLength": 768}},
                    ("query",),
                ),
                probe,
                preserve_output=True,
            )
        ]

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            session_id = current_session(runtime)
            coordinating = any(
                message.role == "user" and parent_prompt in message.content for message in messages
            )
            if self.role == "main" or coordinating:
                round_index = dispatch_rounds.get(session_id, 0)
                dispatch_rounds[session_id] = round_index + 1
                if round_index == 0:
                    if self.role == "main" and path == "workflow":
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "dispatch-workflow",
                                    "workflow_create",
                                    name="真实压力节点",
                                    steps=[
                                        {
                                            "id": "leaf",
                                            "title": "压力叶节点",
                                            "prompt": leaf_prompt,
                                            "role": "worker",
                                            "depends_on": [],
                                        }
                                    ],
                                )
                            ]
                        )
                    else:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "dispatch-child",
                                    "spawn_agent",
                                    task_name="压力父队友"
                                    if self.role == "main" and path == "nested"
                                    else "压力叶队友",
                                    prompt=parent_prompt
                                    if self.role == "main" and path == "nested"
                                    else leaf_prompt,
                                )
                            ]
                        )
                else:
                    if round_index == 1:
                        tool_result(
                            messages,
                            "dispatch-workflow"
                            if self.role == "main" and path == "workflow"
                            else "dispatch-child",
                        )
                    turn = ModelTurn(content="原始派工已完成，等待真实叶节点报告。")
            else:
                if session_id not in scripts:
                    owner = next(message for message in messages if message.role == "user")
                    scripts[session_id] = LeafScript(owner.content, arguments)
                    observers[session_id] = HostObserver(runtime, session_id, budget)
                    leaf_runs[session_id] = runtime.run_ids[session_id]
                observers[session_id].capture(messages, tools)
                turn = scripts[session_id].next(messages, tools)
                if not turn.tool_calls:
                    leaf_done.set()
            yield StreamEvent("done", turn=turn)

        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            raise AssertionError("extractive 派工压力回归不调用额外摘要模型")

    app: Any = None
    try:
        runtime.config.update(
            {
                "default_mode": "yolo",
                "memory": {"enabled": False},
                "updates": {"check_on_start": False},
                "attention": {"enabled": False},
                "extensions": {"watch_enabled": False},
                "compaction": {"enabled": True, "mode": "extractive", "trigger_ratio": 0.95},
                "limits": {"max_context_chars": budget, "max_output_chars": 6000},
                "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            }
        )
        await runtime.reconfigure()
        runtime.add_tool_provider(probe_tools)
        runtime.client_factory = lambda profile: Client(profile.model)  # type: ignore[assignment]
        app = create_app(runtime.config, runtime.data_dir, runtime)
        assert runtime.config.settings.persona == Persona()
        async with app.router.lifespan_context(app):
            initial = await runtime.submit("请按原始限制完成真实派工和只读核查。", mode="yolo")
            await asyncio.wait_for(runtime.wait(initial["run_id"]), timeout=10)
            await asyncio.wait_for(leaf_done.wait(), timeout=10)
            assert len(leaf_runs) == 1
            leaf_id, leaf_run = next(iter(leaf_runs.items()))
            assert (
                await asyncio.wait_for(runtime.wait(leaf_run), timeout=10) == "叶节点逐条核查完成。"
            )
            assert performed == [(leaf_id, json.loads(item)["query"]) for item in arguments]
            records = runtime.store.messages(leaf_id)
            calls = [tool_call for record in records for tool_call in record.get("tool_calls", [])]
            assert [tool_call["arguments"] for tool_call in calls] == arguments
            active = runtime.store.get("compaction", leaf_id)
            assert active is not None and active["anchors"]
            assert any(item["covered_seq"] for item in observers[leaf_id].requests)
            assert {item["run_id"] for item in observers[leaf_id].requests} == {leaf_run}
            if path == "workflow":
                session = runtime.store.get("session", leaf_id)
                assert session is not None and session["workflow_run_id"]
                assert runtime.store.get("agent", leaf_id) is None
            else:
                agent = runtime.team.get(leaf_id)
                if path == "worker":
                    assert agent["parent_id"] == initial["session_id"]
                else:
                    parent = runtime.team.get(agent["parent_id"])
                    assert parent["parent_id"] == initial["session_id"]
    finally:
        if app is not None:
            await app.state.close()
        else:
            await runtime.close()


async def test_real_task_epoch_revokes_a_worker_waiting_for_pressure_summary(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()
    performed: list[str] = []
    arguments = [json.dumps({"query": f"{index}:" + "q" * 760}) for index in range(12)]
    script: LeafScript | None = None
    app: Any = None

    async def summarize(_source: str, _tokens: int) -> str:
        entered.set()
        await release.wait()
        return "按已保存证据继续，未知结果先核原文。"

    async def probe(args: dict[str, Any]) -> dict[str, str]:
        performed.append(args["query"])
        return {"receipt": "saved"}

    def probe_tools(_runtime: Runtime) -> list[Tool]:
        return [
            Tool(
                "context_pressure_probe",
                "只读核查一条有限查询",
                parameters({"query": {"type": "string", "maxLength": 768}}, ("query",)),
                probe,
                preserve_output=True,
            )
        ]

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal script
            if self.role == "worker":
                if script is None:
                    script = LeafScript(
                        next(message.content for message in messages if message.role == "user"),
                        arguments,
                    )
                turn = script.next(messages, tools)
            else:
                turn = ModelTurn(content="已收到队友的暂停状态资料。")
            yield StreamEvent("done", turn=turn)

    try:
        runtime.config.update(
            {
                "default_mode": "yolo",
                "memory": {"enabled": False},
                "updates": {"check_on_start": False},
                "attention": {"enabled": False},
                "extensions": {"watch_enabled": False},
                "compaction": {"enabled": True, "mode": "auto", "trigger_ratio": 0.95},
                "limits": {"max_context_chars": 16000, "max_output_chars": 6000},
                "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            }
        )
        await runtime.reconfigure()
        runtime.compaction.summarize = summarize
        runtime.add_tool_provider(probe_tools)
        runtime.client_factory = lambda profile: Client(profile.model)  # type: ignore[assignment]
        app = create_app(runtime.config, runtime.data_dir, runtime)
        services = app.state.local_services
        commitment = await services.commitments.save(
            "受控压力任务",
            brief={
                "goal": "逐条只读核查资料",
                "constraints": ["不能外发", "不能删除", "先核未知回执"],
            },
        )
        child = await runtime.team.spawn(
            "真实受控压力队友",
            "逐条核查十二条只读资料，遵守完整任务限制。",
            commitment_id=commitment["id"],
        )
        await asyncio.wait_for(entered.wait(), timeout=10)
        driver = runtime.team._active[child["id"]]
        planned = runtime.compaction._ready.get(child["id"])
        assert planned is not None and planned.requires_preview
        fingerprint = runtime._compaction_fingerprint()
        await runtime.submit_task_control(
            commitment["id"], OwnerControlRequest("pause", 0, "pressure-owner-pause")
        )
        assert runtime._compaction_fingerprint() == fingerprint
        release.set()
        await asyncio.wait_for(asyncio.shield(driver), timeout=10)
        parked = runtime.team.get(child["id"])
        assert parked["status"] == "paused" and parked["generation"] == 1
        run = runtime.store.get("run", parked["run_id"])
        assert run is not None and run["status"] == "paused" and run["observed_epoch"] == 0
        assert 0 < len(performed) < len(arguments)
        assert len(set(performed)) == len(performed)
        assert runtime.store.get("compaction", child["id"]) is None
        assert not runtime.compaction._ready
    finally:
        release.set()
        if app is not None:
            await app.state.close()
        else:
            await runtime.close()
