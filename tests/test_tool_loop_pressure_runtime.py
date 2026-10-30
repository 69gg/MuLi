"""真实应用的工具调用压力回归；脚本与宿主观察保持分离。

默认 persona 与完整应用装配；16000/6000，extractive 开启，0.95 是显式
非默认后台触发比率。Message 字符预算不等于整个 HTTP body 或模型 token。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime

from muli.config import Persona
from muli.context import context_size, minimum_context
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import message_text_size
from muli.tools import Tool, parameters


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_async(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("本红例不允许真实网络")

    def reject_sync(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("本红例不允许真实网络")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_sync)


class HostObserver:
    """宿主只记录事实；不得向脚本返回 cutoff、原始归档或隐藏状态。"""

    def __init__(self, runtime: Runtime, session_id: str, budget: int) -> None:
        self.runtime, self.session_id, self.budget = runtime, session_id, budget
        self.requests: list[dict[str, Any]] = []

    def capture(self, messages: list[Message], tools: list[dict[str, Any]]) -> None:
        active = self.runtime.store.get("compaction", self.session_id) or {}
        after = int(active.get("covered_seq", 0))
        raw = self.runtime.store.message_prefix(self.session_id, after=after)
        self.requests.append(
            {
                "request": len(self.requests),
                "session_id": self.session_id,
                "run_id": self.runtime.run_ids.get(self.session_id),
                "context_size": context_size(messages),
                "minimum_size": context_size(minimum_context(messages)),
                "raw_prefix_size": sum(message_text_size(record) for record in raw),
                "raw_trigger_size": self.budget
                * self.runtime.config.settings.compaction.trigger_ratio,
                "covered_seq": after,
                "tool_names": sorted(tool["function"]["name"] for tool in tools),
                "last_call_ids": [
                    call.id for message in messages[-2:] for call in message.tool_calls
                ],
            }
        )
        assert context_size(messages) <= self.budget

    def persist(self, path: Path, performed_count: int, failure: str | None) -> None:
        active = self.runtime.store.get("compaction", self.session_id) or {}
        raw = self.runtime.store.message_prefix(
            self.session_id, after=int(active.get("covered_seq", 0))
        )
        path.write_text(
            json.dumps(
                {
                    "executed_configuration": {
                        "persona": "default",
                        "max_context_chars": self.budget,
                        "max_output_chars": self.runtime.config.settings.limits.max_output_chars,
                        "compaction": self.runtime.config.settings.compaction.model_dump(),
                        "note": "0.95 trigger 显式非默认；context_size 是既有 Message 字符指标，非完整 HTTP body/token 计量",
                    },
                    "failure_type": failure,
                    "performed_count": performed_count,
                    "requests": self.requests,
                    "final_raw_prefix_size": sum(message_text_size(record) for record in raw),
                    "raw_trigger_size": self.budget
                    * self.runtime.config.settings.compaction.trigger_ratio,
                    "compaction_status": self.runtime.compaction.status(self.session_id),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


class PressureScript(FakeClient):
    """只读 messages/tools 与固定脚本；完全不持有 Runtime、Store 或观测器。"""

    def __init__(self, owner_text: str, arguments: list[str]) -> None:
        super().__init__([], [])
        self.owner_text, self.arguments = owner_text, arguments
        self.index = 0

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        assert any(
            message.role == "user" and message.content == self.owner_text for message in messages
        )
        assert {
            "conversation_read",
            "attachment_read",
            "commitment_read",
            "commitment_save",
            "commitment_inspect",
            "spawn_agent",
            "context_pressure_probe",
        } <= {tool["function"]["name"] for tool in tools}
        if self.index:
            previous_id = f"p{self.index - 1}"
            previous_calls = [
                call
                for message in messages
                for call in message.tool_calls
                if call.id == previous_id
            ]
            replies = [message for message in messages if message.tool_call_id == previous_id]
            assert len(previous_calls) == len(replies) == 1
            assert previous_calls[0].arguments == self.arguments[self.index - 1]
            assert replies[0].preserve_content is True
            assert json.loads(replies[0].content) == {"receipt": "saved"}
        if self.index == len(self.arguments):
            turn = ModelTurn(content="已逐条核查，原始证据留在本会话。")
        else:
            turn = ModelTurn(
                tool_calls=[
                    ToolCall(f"p{self.index}", "context_pressure_probe", self.arguments[self.index])
                ]
            )
        self.index += 1
        yield StreamEvent("done", turn=turn)


class ObservedClient(FakeClient):
    def __init__(self, script: PressureScript, observer: HostObserver) -> None:
        super().__init__([], [])
        self.script, self.observer = script, observer

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        self.observer.capture(messages, tools)
        async for event in self.script.stream(messages, tools):
            yield event

    async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
        raise AssertionError("extractive 模式不得调用额外摘要模型")


async def test_real_app_main_compacts_consumed_tool_frames_under_pressure(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    budget, steps = 16000, 10
    owner_text = "连续只读核查十条资料；不外发、不删除、不重复已完成操作。"
    queries: list[str] = []
    for index in range(steps):
        prefix = f"资料-{index}:"
        queries.append(prefix + "q" * (768 - len(prefix)))
    arguments = [json.dumps({"query": query}, ensure_ascii=False) for query in queries]
    performed: list[str] = []

    async def probe(args: dict[str, Any]) -> dict[str, str]:
        query = args["query"]
        assert isinstance(query, str) and len(query) == 768
        performed.append(query)
        return {"receipt": "saved"}

    def probe_tools(_runtime: Runtime) -> list[Tool]:
        return [
            Tool(
                "context_pressure_probe",
                "只读核查一个不超过768字符的查询，返回完整有限回执",
                parameters(
                    {"query": {"type": "string", "minLength": 1, "maxLength": 768}},
                    ("query",),
                ),
                probe,
                preserve_output=True,
            )
        ]

    app: Any = None
    failure: str | None = None
    observer: HostObserver | None = None
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
            }
        )
        await runtime.reconfigure()
        runtime.add_tool_provider(probe_tools)
        app = create_app(runtime.config, runtime.data_dir, runtime)
        assert runtime.config.settings.persona == Persona()
        assert runtime.task_control is app.state.local_services.task_control
        session_id = runtime.main_session()["id"]
        observer = HostObserver(runtime, session_id, budget)
        script = PressureScript(owner_text, arguments)
        client = ObservedClient(script, observer)

        def factory(_profile: ModelConfig) -> ObservedClient:
            return client

        runtime.client_factory = factory  # type: ignore[assignment]
        async with app.router.lifespan_context(app):
            try:
                started = await runtime.submit(owner_text, mode="yolo")
                result = await asyncio.wait_for(runtime.wait(started["run_id"]), timeout=10)
                assert result == "已逐条核查，原始证据留在本会话。"
                run = runtime.store.get("run", started["run_id"])
                assert run is not None and run["status"] == "completed"
                assert performed == queries and len(set(performed)) == steps
                records = runtime.store.messages(session_id)
                saved_calls = [call for record in records for call in record.get("tool_calls", [])]
                assert [call["arguments"] for call in saved_calls] == arguments
                assert [call["id"] for call in saved_calls] == [
                    f"p{index}" for index in range(steps)
                ]
                saved_results = [record for record in records if record["role"] == "tool"]
                assert len(saved_results) == steps
                assert all(
                    record["content_provenance"] == "accepted"
                    and record["preserve_content"] is True
                    and json.loads(record["content"]) == {"receipt": "saved"}
                    for record in saved_results
                )
                active = runtime.store.get("compaction", session_id)
                assert active is not None and active["kind"] == "extractive"
                assert any(item["covered_seq"] for item in observer.requests)
                assert {item["run_id"] for item in observer.requests} == {started["run_id"]}
            except BaseException as exc:
                failure = type(exc).__name__
                raise
            finally:
                observer.persist(
                    tmp_path / "context-pressure-evidence.json", len(performed), failure
                )
    finally:
        if app is not None:
            await app.state.close()
        else:
            await runtime.close()
