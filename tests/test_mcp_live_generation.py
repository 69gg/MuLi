from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn, Self, cast

import httpx
import pytest
from test_extensions import server, setup_runtime
from test_mcp import FakeClient
from test_runtime import FakeClient as FakeModel

from muli.config import Settings
from muli.management import SelfManagement
from muli.proactive import FrozenRouting
from muli.providers import Message, ModelClient, ModelTurn, StreamEvent, ToolCall
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool
from muli.tools.mcp import MCPManager


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("本组不得连接 MCP、模型或启动子进程")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


@pytest.fixture
async def host(tmp_path: Path) -> AsyncIterator[tuple[Runtime, FakeClient, FakeClient]]:
    runtime = setup_runtime(tmp_path)
    runtime.config.update(
        {
            "compaction": {"enabled": False},
            "models": {"main": {"model": "offline", "base_url": "https://unused.invalid"}},
        }
    )
    old, new = FakeClient(names=["before"]), FakeClient(names=["after"])
    runtime.mcp = MCPManager(
        runtime.config.settings.mcp,
        client_factory=lambda source, _: old if source["url"].endswith("/old") else new,
    )
    try:
        await runtime.extensions.load_initial()
        yield runtime, old, new
    finally:
        await runtime.close()


async def publish(runtime: Runtime, *, remove: bool = False) -> None:
    target = runtime.config.settings.model_copy(deep=True)
    target.mcp = {"mcpServers": {} if remove else {"old": server("new")}}
    async with runtime.configuration_lock:
        await runtime.extensions.apply_locked(target, persist=True)


async def test_running_team_uses_new_generation_after_its_old_model_batch(
    tmp_path: Path,
) -> None:
    runtime = setup_runtime(tmp_path)
    runtime.config.update(
        {
            "compaction": {"enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid"}},
        }
    )
    old, new = FakeClient(names=["before"]), FakeClient(names=["after"])
    runtime.mcp = MCPManager(
        runtime.config.settings.mcp,
        client_factory=lambda source, _: old if source["url"].endswith("/old") else new,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    schemas: list[list[str]] = []
    old_name = ""

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schemas.append([tool["function"]["name"] for tool in tools])
            if len(schemas) == 1:
                entered.set()
                await release.wait()
                turn = ModelTurn(tool_calls=[ToolCall("old-call", old_name, '{"q":"旧轮"}')])
            elif len(schemas) == 2:
                turn = ModelTurn(
                    tool_calls=[ToolCall("new-call", runtime.mcp.tools[0].name, '{"q":"新轮"}')]
                )
            else:
                turn = ModelTurn(content="队友完成")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: cast(ModelClient, Client())
    try:
        await runtime.extensions.load_initial()
        old_name = runtime.mcp.tools[0].name
        parent = runtime.new_session(mode="yolo")
        agent = await runtime.team.spawn("live-worker", "核对目录更新", parent_id=parent["id"])
        async with asyncio.timeout(3):
            await entered.wait()
            assert runtime.busy and runtime.team.get(agent["id"])["status"] == "running"
            target = runtime.config.settings.model_copy(deep=True)
            target.mcp = {"mcpServers": {"old": server("new")}}
            async with runtime.configuration_lock:
                await runtime.extensions.apply_locked(target, persist=True)
            release.set()
            await runtime.team.wait([agent["id"]], timeout=2)
        assert runtime.team.get(agent["id"])["status"] == "completed"
        new_name = runtime.mcp.tools[0].name
        assert old_name in schemas[0] and new_name not in schemas[0]
        assert new_name in schemas[1] and old_name not in schemas[1]
        results = {
            item["tool_call_id"]: json.loads(item["content"])
            for item in runtime.store.messages(agent["id"])
            if item["role"] == "tool"
        }
        assert results["old-call"]["status"] == "not_executed"
        assert results["old-call"]["code"] == "generation_changed"
        assert len(results) == 2 and results["new-call"]["content"][0]["text"] == "结果"
        assert old.calls == [] and len(new.calls) == 1
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("remove", [False, True], ids=["replace", "delete"])
async def test_approval_does_not_authorize_an_endpoint_revoked_while_waiting(
    host: tuple[Runtime, FakeClient, FakeClient], remove: bool
) -> None:
    runtime, old, new = host
    name = runtime.mcp.tools[0].name
    model = FakeModel(
        [
            ModelTurn(tool_calls=[ToolCall("approval-call", name, '{"q":"待批准"}')]),
            ModelTurn(content="按新目录重新规划"),
        ],
        [],
    )
    runtime.client_factory = lambda _: cast(ModelClient, model)
    session = runtime.new_session(mode="ask")
    started = await runtime.start(session["id"], "读取外部资料")
    async with asyncio.timeout(3):
        async with runtime.bus.changed:
            await runtime.bus.changed.wait_for(lambda: bool(runtime.permissions.pending))
        approval = next(iter(runtime.permissions.pending))
        await publish(runtime, remove=remove)
        await runtime.permissions.resolve(approval, "approve")
        assert await runtime.wait(started["run_id"]) == "按新目录重新规划"
    result = next(item for item in runtime.store.messages(session["id"]) if item["role"] == "tool")
    assert result["tool_call_id"] == "approval-call"
    assert json.loads(result["content"])["code"] == "generation_changed"
    assert old.calls == new.calls == []


async def test_only_the_admitted_first_call_of_an_old_batch_finishes(
    host: tuple[Runtime, FakeClient, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, old, new = host
    app = create_app(runtime.config, runtime=runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    original = old.call_tool

    async def waiting(
        name: str, arguments: dict[str, Any], *, timeout: float, raise_on_error: bool
    ) -> Any:
        entered.set()
        await release.wait()
        return await original(name, arguments, timeout=timeout, raise_on_error=raise_on_error)

    monkeypatch.setattr(old, "call_tool", waiting)
    name = runtime.mcp.tools[0].name
    model = FakeModel(
        [
            ModelTurn(
                tool_calls=[
                    ToolCall("entered", name, '{"q":"已进入"}'),
                    ToolCall("not-entered", name, '{"q":"尚未进入"}'),
                ]
            ),
            ModelTurn(content="批次配对完整"),
        ],
        [],
    )
    runtime.client_factory = lambda _: cast(ModelClient, model)
    session = runtime.new_session(mode="yolo")
    started = await runtime.start(session["id"], "执行两项")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await publish(runtime)
        assert runtime.mcp.draining == 1 and old.closed == 0
        current = runtime.config.path.read_bytes()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as api:
            response = await api.patch(
                "/api/settings", json={"mcp": {"mcpServers": {"old": server("third")}}}
            )
        assert response.status_code == 409 and response.json()["detail"] == "generation_draining"
        assert runtime.config.path.read_bytes() == current
        assert old.entered == new.entered == 1 and runtime.extensions.status()["phase"] != "failed"
        release.set()
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 3) == "批次配对完整"
        results = {
            item["tool_call_id"]: json.loads(item["content"])
            for item in runtime.store.messages(session["id"])
            if item["role"] == "tool"
        }
        assert not results["entered"]["is_error"]
        assert results["not-entered"]["code"] == "generation_changed"
        assert len(old.calls) == 1 and new.calls == []
        replay = [item.tool_call_id for item in model.seen[1] if item.role == "tool"]
        assert replay == ["entered", "not-entered"]
    finally:
        release.set()
        await app.state.management.close()


async def test_capacity_wait_observes_the_current_catalog_before_starting_the_model(
    host: tuple[Runtime, FakeClient, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = host
    ready = asyncio.Event()
    original = runtime._prepare_request

    async def prepared(session_id: str) -> Any:
        value = await original(session_id)
        ready.set()
        return value

    monkeypatch.setattr(runtime, "_prepare_request", prepared)
    runtime.capacity = asyncio.Semaphore(0)
    schemas: list[list[str]] = []

    class Client(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schemas.append([item["function"]["name"] for item in tools])
            async for event in super().stream(messages, tools):
                yield event

    client = Client([ModelTurn(content="新目录")], [])
    runtime.client_factory = lambda _: cast(ModelClient, client)
    old_name = runtime.mcp.tools[0].name
    session = runtime.new_session(mode="yolo")
    started = await runtime.start(session["id"], "等待配额")
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert schemas == []
        await publish(runtime)
        new_name = runtime.mcp.tools[0].name
        runtime.capacity.release()
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 2) == "新目录"
        assert old_name not in schemas[0] and new_name in schemas[0]
    finally:
        runtime.capacity.release()


async def test_publishing_does_not_restore_tools_in_the_final_budget_round(
    host: tuple[Runtime, FakeClient, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, old, new = host
    runtime.config.settings.limits.max_tool_steps = 1
    entered, release = asyncio.Event(), asyncio.Event()
    original = old.call_tool

    async def waiting(
        name: str, arguments: dict[str, Any], *, timeout: float, raise_on_error: bool
    ) -> Any:
        entered.set()
        await release.wait()
        return await original(name, arguments, timeout=timeout, raise_on_error=raise_on_error)

    monkeypatch.setattr(old, "call_tool", waiting)
    schemas: list[list[dict[str, Any]]] = []

    class Client(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schemas.append(tools)
            async for event in super().stream(messages, tools):
                yield event

    client = Client(
        [
            ModelTurn(
                tool_calls=[ToolCall("last-effect", runtime.mcp.tools[0].name, '{"q":"一次"}')]
            ),
            ModelTurn(content="最后解释轮"),
        ],
        [],
    )
    runtime.client_factory = lambda _: cast(ModelClient, client)
    session = runtime.new_session(mode="yolo")
    started = await runtime.start(session["id"], "只调用一次")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await publish(runtime)
        release.set()
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 2) == "最后解释轮"
        assert schemas[0] and schemas[1] == []
        assert len(old.calls) == 1 and new.calls == []
    finally:
        release.set()


@pytest.mark.parametrize("kind", ["main", "reviewer", "attention", "disabled"])
async def test_each_new_round_keeps_the_existing_tool_filters(
    host: tuple[Runtime, FakeClient, FakeClient], kind: str
) -> None:
    runtime, old, new = host
    entered, release = asyncio.Event(), asyncio.Event()
    schemas: list[dict[str, dict[str, Any]]] = []
    calls: list[str] = []

    async def read(arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append("read")
        return {"status": "read"}

    async def forbidden(arguments: dict[str, Any]) -> NoReturn:
        raise AssertionError("filtered tools cannot be dispatched")

    for name, risk, handler in (
        ("offline_round_read", "read", read),
        ("offline_round_hidden", "read", forbidden),
        ("offline_round_write", "write", forbidden),
    ):
        runtime.tools[name] = Tool(name, name, {"type": "object"}, handler, risk)
    runtime.config.settings.disabled_tools = ["offline_round_hidden"] + (
        ["offline_round_write"] if kind == "disabled" else []
    )
    runtime.config.settings.models.reviewer = runtime.config.settings.models.main.model_copy()

    class Client:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            schemas.append({tool["function"]["name"]: tool for tool in tools})
            if len(schemas) == 1:
                entered.set()
                await release.wait()
                turn = ModelTurn(
                    tool_calls=[ToolCall("filtered-round", "offline_round_read", "{}")]
                )
            else:
                turn = ModelTurn(content="过滤保持")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: cast(ModelClient, Client())
    operation: asyncio.Task[str] | None = None
    session = (
        runtime.main_session()
        if kind in {"main", "attention"}
        else runtime.new_session(mode="yolo")
    )
    old_name = runtime.mcp.tools[0].name
    try:
        if kind == "reviewer":
            operation = asyncio.create_task(
                runtime.run_step(session["id"], "独立检查", "reviewer", "yolo")
            )
        elif kind == "attention":
            assert (
                await runtime.submit_attention("仅供参考的系统资料", "filter-notice", objects=[])
                is not None
            )
            operation = runtime.tasks[session["id"]]
        else:
            await runtime.start(session["id"], "核对两轮过滤", mode="yolo")
            operation = runtime.tasks[session["id"]]
        await asyncio.wait_for(entered.wait(), 2)
        await publish(runtime)
        new_name = runtime.mcp.tools[0].name
        release.set()
        if kind == "attention":
            async with asyncio.timeout(2), runtime.bus.changed:
                await runtime.bus.changed.wait_for(lambda: bool(runtime.permissions.pending))
            approval = next(iter(runtime.permissions.pending))
            await runtime.permissions.resolve(approval, "approve")
        assert await asyncio.wait_for(operation, 2) == "过滤保持"
        assert len(schemas) == 2 and calls == ["read"]
        for schema in schemas:
            assert "offline_round_read" in schema and "offline_round_hidden" not in schema
            assert "offline_round_write" not in schema
            if kind in {"reviewer", "attention"}:
                assert "spawn_agent" not in schema and "workflow_create" not in schema
        if kind == "disabled":
            assert old_name in schemas[0] and new_name not in schemas[0]
            assert new_name in schemas[1] and old_name not in schemas[1]
        else:
            assert all(old_name not in schema and new_name not in schema for schema in schemas)
        assert old.calls == new.calls == []
    finally:
        release.set()
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


async def test_failed_configuration_save_keeps_the_old_handler_authorized(
    host: tuple[Runtime, FakeClient, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, old, new = host
    before = runtime.config.path.read_bytes()
    tool = runtime.mcp.tools[0]

    def failed(target: Settings) -> NoReturn:
        raise OSError("synthetic save failure")

    monkeypatch.setattr(runtime.config, "replace", failed)
    with pytest.raises(ValueError, match="config_save_failed"):
        await publish(runtime)
    assert runtime.config.path.read_bytes() == before
    assert runtime.mcp.tools[0] is tool and old.closed == 0 and new.closed == 1
    assert not (await tool.handler({"q": "仍是原代"}))["is_error"]


async def test_self_configure_publishes_a_pure_extension_plan_during_its_own_run(
    host: tuple[Runtime, FakeClient, FakeClient],
) -> None:
    runtime, _, _ = host
    app = create_app(runtime.config, runtime=runtime)
    manager: SelfManagement = app.state.management
    entered, release = asyncio.Event(), asyncio.Event()
    changes = {"mcp": {"mcpServers": {"old": server("new")}}}

    class Client(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if not self.turns[0].tool_calls:
                entered.set()
                await release.wait()
            async for event in super().stream(messages, tools):
                yield event

    client = Client(
        [
            ModelTurn(
                tool_calls=[
                    ToolCall("configure", "self_configure", json.dumps({"changes": changes}))
                ]
            ),
            ModelTurn(content="任务继续"),
        ],
        [],
    )
    runtime.client_factory = lambda _: cast(ModelClient, client)
    try:
        await manager.start()
        session = runtime.new_session(mode="yolo")
        started = await runtime.start(session["id"], "调整MCP后继续")
        async with asyncio.timeout(3):
            await entered.wait()
            async with runtime.bus.changed:
                await runtime.bus.changed.wait_for(
                    lambda: bool(manager.plans()) and manager.plans()[0]["status"] == "completed"
                )
        assert runtime.busy and runtime.tasks[session["id"]].done() is False
        assert runtime.config.settings.mcp == changes["mcp"]
        assert (
            manager.plans()[0]["routing"]
            == FrozenRouting.bound(ReplyOrigin.from_input(started["message"])).dump()
        )
        release.set()
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 2) == "任务继续"
    finally:
        release.set()
        await manager.close()


async def test_settings_api_can_publish_extensions_but_keeps_mixed_configuration_idle_gate(
    host: tuple[Runtime, FakeClient, FakeClient],
) -> None:
    runtime, _, new = host
    app = create_app(runtime.config, runtime=runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    class Client(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            entered.set()
            await release.wait()
            async for event in super().stream(messages, tools):
                yield event

    client = Client([ModelTurn(content="继续")], [])
    runtime.client_factory = lambda _: cast(ModelClient, client)
    session = runtime.new_session(mode="yolo")
    started = await runtime.start(session["id"], "保持运行")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as api:
            response = await api.patch(
                "/api/settings",
                json={"persona": {"user_name": "混合变更"}, "mcp": {"mcpServers": {}}},
            )
            assert response.status_code == 409 and new.entered == 0
            response = await api.patch(
                "/api/settings", json={"mcp": {"mcpServers": {"old": server("new")}}}
            )
            assert response.status_code == 200 and new.entered == 1 and runtime.busy
        release.set()
        assert await asyncio.wait_for(runtime.wait(started["run_id"]), 2) == "继续"
    finally:
        release.set()
        await app.state.management.close()


async def test_later_extension_plan_does_not_overtake_an_earlier_idle_plan(
    host: tuple[Runtime, FakeClient, FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, new = host
    app = create_app(runtime.config, runtime=runtime)
    manager: SelfManagement = app.state.management
    entered, idle, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = manager._idle

    async def waiting_idle() -> None:
        idle.set()
        await original()

    monkeypatch.setattr(manager, "_idle", waiting_idle)

    class Client(FakeModel):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            entered.set()
            await release.wait()
            async for event in super().stream(messages, tools):
                yield event

    client = Client([ModelTurn(content="退出等待")], [])
    runtime.client_factory = lambda _: cast(ModelClient, client)
    session = runtime.new_session(mode="yolo")
    await runtime.start(session["id"], "长任务")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        earlier = await manager.stage("configure", {"persona": {"user_name": "先前计划"}})
        await manager.start()
        await asyncio.wait_for(idle.wait(), 2)
        with pytest.raises(ValueError, match="已有待应用管理操作"):
            await manager.stage("configure", {"mcp": {"mcpServers": {"old": server("new")}}})
        await asyncio.sleep(0)
        assert manager.runtime.store.get("management", earlier["id"])["status"] == "pending"
        assert len(manager.plans()) == 1 and runtime.busy
        assert new.entered == 0 and runtime.config.settings.persona.user_name != "先前计划"
    finally:
        await manager.close()
        release.set()
