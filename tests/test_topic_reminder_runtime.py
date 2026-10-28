"""Owner tools and actual message entrances drive local, once-only topic notices."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import make_runtime

from muli.channels import IncomingMessage
from muli.providers import ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.server import create_app


def registration() -> dict[str, str]:
    return {"keyword": "露营🌱", "title": "带充电宝", "content": "记得充满电。", "key": "camp"}


def register_turn() -> ModelTurn:
    return ModelTurn(
        tool_calls=[ToolCall("register", "topic_reminder_create", json.dumps(registration()))]
    )


@asynccontextmanager
async def host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turns: list[ModelTurn]
) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient, Any]]:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("topic reminder integration must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    runtime, _ = make_runtime(tmp_path, turns)
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
            "compaction": {"enabled": False},
            "memory": {"auto_extract": False},
            "extensions": {"watch_enabled": False},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "owner_ids": ["7"],
                    "base_url": "https://unused.invalid",
                }
            },
        }
    )
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        yield runtime, web, app.state.channel_application.manager


async def send(
    runtime: Runtime, web: httpx.AsyncClient, text: str, *, mode: str = "yolo"
) -> dict[str, Any]:
    response = await web.post(
        f"/api/sessions/{runtime.main_session()['id']}/messages",
        json={"content": text, "mode": mode},
    )
    assert response.status_code == 200
    return response.json()


async def notices(runtime: Runtime) -> list[dict[str, Any]]:
    async with asyncio.timeout(3), runtime.bus.changed:
        await runtime.bus.changed.wait_for(
            lambda: any(
                item.get("source") == "topic_reminder"
                for item in runtime.store.list("notification")
            )
        )
    return [
        item
        for item in runtime.store.list("notification")
        if item.get("source") == "topic_reminder"
    ]


@pytest.mark.parametrize("entry", ["web", "onebot"])
async def test_registration_does_not_match_itself_and_next_owner_text_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    turns = [
        register_turn(),
        ModelTurn(content="已记住约定"),
        ModelTurn(content="已收到"),
        ModelTurn(content="已收到"),
    ]
    async with host(tmp_path, monkeypatch, turns) as (runtime, web, manager):
        created = await send(runtime, web, "下次我提到露营🌱，提醒我带充电宝")
        assert await runtime.wait(created["run_id"]) == "已记住约定"
        assert not runtime.store.list("notification")
        if entry == "web":
            matched = await send(runtime, web, "我们周末露营🌱吧")
        else:
            matched = await manager.receive(
                "onebot", IncomingMessage("camp-message", "7", "7", "我们周末露营🌱吧")
            )
        assert await runtime.wait(matched["run_id"]) == "已收到"
        result = await notices(runtime)
        assert len(result) == 1
        notice = result[0]
        assert notice["title"] == "带充电宝" and notice["content"] == "记得充满电。"
        assert notice["session_id"] == runtime.main_session()["id"]
        assert notice["matched_input_id"] == matched["message"]["id"]
        following = await send(runtime, web, "再次露营🌱")
        assert await runtime.wait(following["run_id"]) == "已收到"
        assert len(await notices(runtime)) == 1
        status = (await web.get("/api/status")).json()
        assert status["topic_reminders"]["last_error"] is None
        assert runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_topic_registration_obeys_real_tool_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    async with host(tmp_path, monkeypatch, [register_turn(), ModelTurn(content="已处理")]) as (
        runtime,
        web,
        _,
    ):
        started = await send(runtime, web, "下次说露营🌱时提醒带充电宝", mode="ask")
        async with asyncio.timeout(3), runtime.bus.changed:
            await runtime.bus.changed.wait_for(lambda: bool(runtime.permissions.pending))
        assert not runtime.store.list("topic_reminder")
        identifier = next(iter(runtime.permissions.pending))
        await runtime.permissions.resolve(identifier, decision)
        assert await runtime.wait(started["run_id"]) == "已处理"
        assert len(runtime.store.list("topic_reminder")) == int(decision == "approve")


async def test_internal_owner_submit_cannot_register_a_topic_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with host(tmp_path, monkeypatch, [register_turn(), ModelTurn(content="未登记")]) as (
        runtime,
        _web,
        _,
    ):
        started = await runtime.submit("内部脚本也声明 owner，但没有宿主原文证明", mode="yolo")
        assert await runtime.wait(started["run_id"]) == "未登记"
        assert not runtime.store.list("topic_reminder")
        results = [
            event["data"] for event in runtime.store.events() if event["type"] == "tool.completed"
        ]
        assert any("error" in result.get("result", {}) for result in results)


async def test_new_owner_input_supersedes_unstarted_topic_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with host(tmp_path, monkeypatch, [register_turn(), ModelTurn(content="不再登记")]) as (
        runtime,
        web,
        _,
    ):
        started = await send(runtime, web, "下次说露营🌱时提醒带充电宝", mode="ask")
        async with asyncio.timeout(3), runtime.bus.changed:
            await runtime.bus.changed.wait_for(lambda: bool(runtime.permissions.pending))
        identifier = next(iter(runtime.permissions.pending))
        incoming = await send(runtime, web, "先不要登记，取消刚才的约定", mode="ask")
        assert incoming["message"]["id"] != started["message"]["id"]
        assert await runtime.wait(started["run_id"]) == "不再登记"
        assert not runtime.store.list("topic_reminder")
        assert not runtime.permissions.pending
        assert runtime.store.get("approval", identifier)["status"] == "cancelled"


@pytest.mark.parametrize(
    "name", ["topic_reminder_create", "topic_reminder_list", "topic_reminder_cancel"]
)
async def test_topic_tools_reject_handlers_without_a_current_main_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    async with host(tmp_path, monkeypatch, []) as (runtime, _web, _):
        with pytest.raises(PermissionError):
            await runtime.tools[name].handler(registration())
        assert not runtime.store.list("topic_reminder")
