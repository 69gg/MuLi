"""Exercise owner and coordinator prompt boundaries through real requests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_memory_correct_prompt import default_guidance, prompt, prompt_runtime
from test_runtime import FakeClient

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store_schema import ExistingStoreError
from muli.tools import Tool, parameters


async def captured_request(
    runtime: Runtime, seen: list[list[Message]]
) -> tuple[list[Message], set[str]]:
    catalogs: list[set[str]] = []
    answer = "已核对本轮提示与工具目录，没有执行工具。"

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            catalogs.append({tool["function"]["name"] for tool in tools})
            async for event in super().stream(messages, tools):
                yield event

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model == "main"
        return Main([ModelTurn(content=answer)], seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await runtime.submit("只核对当前说明与可用目录，不执行工具。", mode="yolo")
    assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5) == answer
    assert receipt["session_id"] == runtime.store.existing_main_session_id()
    assert len(seen) == len(catalogs) == 1 and seen[0][0].role == "system"
    run = runtime.store.get("run", receipt["run_id"])
    assert run is not None and run["status"] == "completed"
    return seen[0], catalogs[0]


async def test_real_main_without_owner_principal_has_no_correction_guidance(tmp_path: Path) -> None:
    async with prompt_runtime(tmp_path) as (runtime, seen):
        catalogs: list[set[str]] = []

        class Main(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                catalogs.append({tool["function"]["name"] for tool in tools})
                async for event in super().stream(messages, tools):
                    yield event

        def factory(profile: ModelConfig) -> FakeClient:
            return Main([ModelTurn(content="损坏主身份不应触达模型。")], seen)

        runtime.client_factory = factory  # type: ignore[assignment]
        main = runtime.main_session()
        default_guidance(prompt(runtime, main["id"]), present=True)
        previous_runs = runtime.store.list("run")
        runtime.store.put("session", {**main, "principal": "external:prompt-boundary"})
        assert main["id"] == runtime.store.existing_main_session_id()
        assert "memory_correct" in runtime.session_tools(main["id"])
        default_guidance(prompt(runtime, main["id"]), present=False)

        with pytest.raises(ExistingStoreError) as rejected:
            await runtime.submit("只核对当前说明，不修复身份或执行工具。", mode="yolo")

        assert rejected.value.code == "existing_store_identity_invalid"
        assert seen == [] and catalogs == []
        assert runtime.store.list("run") == previous_runs
        assert runtime.tasks == {} and runtime.run_ids == {}
        assert runtime.store.existing_main_session_id() == main["id"]
        current = runtime.store.get("session", main["id"])
        assert current is not None and current["kind"] == "main"
        assert current["principal"] == "external:prompt-boundary"


async def test_noncoordinator_write_replacement_has_no_correction_guidance(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    async def replacement(arguments: dict[str, Any]) -> str:
        calls.append(arguments)
        return "synthetic replacement"

    def provider(runtime: Runtime) -> list[Tool]:
        return [
            Tool("memory_correct", "synthetic replacement", parameters({}), replacement, "write")
        ]

    async with prompt_runtime(tmp_path) as (runtime, seen):
        main = runtime.main_session()
        default_guidance(prompt(runtime, main["id"]), present=True)
        runtime.add_tool_provider(provider, replace=True, coordinator=False)
        registered = runtime.session_tools(main["id"])["memory_correct"]
        assert registered.handler is replacement and registered.risk == "write"

        messages, catalog = await captured_request(runtime, seen)

        assert "memory_correct" not in catalog
        default_guidance(messages[0].content, present=False)
        assert calls == []
