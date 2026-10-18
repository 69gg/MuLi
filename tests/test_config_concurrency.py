from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.config import ConfigFile
from muli.integrations.chiliarch import Envelope
from muli.integrations.manager import IntegrationManager
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import Store


@pytest.fixture
async def workspace(
    tmp_path: Path,
) -> AsyncIterator[tuple[Runtime, IntegrationManager, httpx.AsyncClient, Path, Path]]:
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(old_root)],
            "models": {"main": {"model": "before", "base_url": "https://unused.invalid/v1"}},
        }
    )
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            yield runtime, app.state.integration, client, old_root, new_root


async def test_settings_close_blocks_start_until_new_roots_are_active(
    workspace: tuple[Runtime, IntegrationManager, httpx.AsyncClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, integration, client, old_root, new_root = workspace
    closing, release = asyncio.Event(), asyncio.Event()
    original_close = integration.close
    observed: list[tuple[str, list[Path]]] = []

    async def paused_close() -> None:
        closing.set()
        await release.wait()
        await original_close()

    async def execute(
        session_id: str, run_id: str, role: str, mode: str, workflow_step: bool
    ) -> str:
        observed.append((runtime.model_config("main").model, runtime.files.roots))
        with pytest.raises(PermissionError):
            runtime.files.path(str(old_root))
        assert runtime.files.path(str(new_root)) == new_root
        return "current boundaries"

    monkeypatch.setattr(integration, "close", paused_close)
    monkeypatch.setattr(runtime, "_execute", execute)
    session = runtime.new_session()
    updating = asyncio.create_task(
        client.patch(
            "/api/settings",
            json={
                "workspace_roots": [str(new_root)],
                "models": {"main": {"model": "after"}},
            },
        )
    )
    await asyncio.wait_for(closing.wait(), 1)
    starting = asyncio.create_task(
        client.post(f"/api/sessions/{session['id']}/messages", json={"content": "check roots"})
    )
    try:
        await asyncio.sleep(0.01)
        assert not starting.done()
        assert not runtime.tasks
        assert runtime.files.roots == [old_root]
        release.set()
        changed, started = await asyncio.wait_for(asyncio.gather(updating, starting), 2)
        assert changed.status_code == started.status_code == 200
        assert await runtime.wait(started.json()["run_id"]) == "current boundaries"
        assert observed == [("after", [new_root])]
    finally:
        release.set()
        await asyncio.gather(updating, starting, return_exceptions=True)


async def test_concurrent_settings_updates_are_serialized(
    workspace: tuple[Runtime, IntegrationManager, httpx.AsyncClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, integration, client, old_root, new_root = workspace
    closing, release = asyncio.Event(), asyncio.Event()
    original_close = integration.close
    observed: list[str] = []

    async def paused_close() -> None:
        observed.append(runtime.config.settings.models.main.model)
        if len(observed) == 1:
            closing.set()
            await release.wait()
        await original_close()

    monkeypatch.setattr(integration, "close", paused_close)
    first = asyncio.create_task(
        client.patch(
            "/api/settings",
            json={
                "models": {"main": {"model": "first"}},
                "workspace_roots": [str(new_root)],
            },
        )
    )
    await asyncio.wait_for(closing.wait(), 1)
    second = asyncio.create_task(
        client.patch(
            "/api/settings",
            json={
                "models": {"main": {"model": "second"}},
            },
        )
    )
    try:
        await asyncio.sleep(0.01)
        assert not second.done()
        assert runtime.config.settings.models.main.model == "first"
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert [response.status_code for response in responses] == [200, 200]
        assert [response.json()["models"]["main"]["model"] for response in responses] == [
            "first",
            "second",
        ]
        assert observed == ["first", "second"]
        assert runtime.files.roots == [new_root]
        assert ConfigFile(runtime.config.path).settings.models.main.model == "second"
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.parametrize("operation", ["pair", "workflow"])
async def test_pair_and_manual_workflow_start_share_configuration_lock(
    workspace: tuple[Runtime, IntegrationManager, httpx.AsyncClient, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    runtime, integration, client, _, _ = workspace
    invoked = asyncio.Event()

    async def pair(code: str, name: str | None = None) -> dict[str, Any]:
        invoked.set()
        return {"paired": True}

    async def start(identifier: str) -> dict[str, Any]:
        invoked.set()
        return {"id": identifier}

    monkeypatch.setattr(integration, "pair", pair)
    monkeypatch.setattr(runtime.workflows, "start", start)
    await runtime.configuration_lock.acquire()
    pending = asyncio.create_task(
        client.post(
            "/api/integrations/chiliarch/pair"
            if operation == "pair"
            else "/api/workflows/fake/run",
            json={"code": "fake"} if operation == "pair" else {},
        )
    )
    try:
        await asyncio.sleep(0.01)
        assert not invoked.is_set()
    finally:
        runtime.configuration_lock.release()
    assert (await asyncio.wait_for(pending, 1)).status_code == 200
    assert invoked.is_set()


async def test_closing_remote_start_waiting_on_config_lock_does_not_deadlock(
    workspace: tuple[Runtime, IntegrationManager, httpx.AsyncClient, Path, Path],
) -> None:
    runtime, integration, _, _, _ = workspace
    integration._closing = False
    await integration._receive(
        Envelope(
            type="task.dispatch",
            payload={
                "task_id": "pending",
                "instruction": "fake",
                "parameters": {},
            },
        )
    )
    async with runtime.configuration_lock:
        pending = asyncio.create_task(integration._execute("pending"))
        integration._active["pending"] = pending
        await asyncio.sleep(0)
        assert not runtime.tasks
        await asyncio.wait_for(integration.close(), 1)
    assert not runtime.tasks
    assert integration._task("pending")["state"] == "interrupted"  # type: ignore[index]


def test_recent_events_returns_latest_in_order_without_changing_replay(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    try:
        recorded = [store.event("sample", {"position": index}, "s") for index in range(6)]
        store.event("other", {}, "elsewhere")
        assert [event["id"] for event in store.recent_events("s", 3)] == [
            event["id"] for event in recorded[-3:]
        ]
        assert [event["id"] for event in store.events(recorded[0]["id"], "s", 2)] == [
            event["id"] for event in recorded[1:3]
        ]
    finally:
        store.close()
