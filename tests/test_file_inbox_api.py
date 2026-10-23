from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from muli.auth import hash_password
from muli.config import ConfigFile
from muli.runtime import Runtime
from muli.server import create_app


@pytest.fixture
async def inbox_api(
    tmp_path: Path,
) -> AsyncIterator[tuple[FastAPI, Runtime, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            yield app, runtime, client


async def _wait_for(runtime: Runtime, predicate: Callable[[], bool]) -> None:
    # Wait for actual committed-event wakeups, with a small hard test deadline.
    async with runtime.bus.changed:
        await asyncio.wait_for(runtime.bus.changed.wait_for(predicate), timeout=2)


def _names(app: FastAPI) -> set[str]:
    return {
        item["metadata"]["name"]
        for item in app.state.file_inbox.list_records(100)
        if item["kind"] == "candidate"
    }


async def test_inbox_default_is_quiet_and_authenticated_before_record_lookup(
    inbox_api: tuple[FastAPI, Runtime, httpx.AsyncClient],
) -> None:
    _, runtime, client = inbox_api
    response = await client.get("/api/file-inbox")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"] == []
    assert response.json()["status"]["running"] is False
    assert response.json()["status"]["enabled"] is False
    tools = (await client.get("/api/status")).json()["tools"]
    assert {"file_inbox_list", "file_inbox_get", "file_inbox_configure"} <= {
        item["name"] for item in tools
    }
    settings = (await client.get("/api/file-inbox/settings")).json()
    assert settings["desired"]["enabled"] is False
    assert "file_inbox" not in (await client.get("/api/settings")).json()
    channel_status = (await client.get("/api/channels")).json()
    assert "file_inbox" in channel_status["proactive"]["independent_sources"]
    runtime.config.update({"server": {"password_hash": hash_password("test-inbox-password")}})
    for path in (
        "/api/file-inbox",
        "/api/file-inbox/settings",
        "/api/file-inbox/" + "a" * 32,
    ):
        assert (await client.get(path)).status_code == 401
    assert (
        await client.put("/api/file-inbox/settings", json={"enabled": False})
    ).status_code == 401
    login = await client.post("/api/auth/login", json={"password": "test-inbox-password"})
    assert login.status_code == 200
    assert (await client.get("/api/file-inbox")).status_code == 200
    assert (await client.get("/api/file-inbox/" + "a" * 32)).status_code == 404
    for limit in (0, 101):
        assert (await client.get("/api/file-inbox", params={"limit": limit})).status_code == 422


@pytest.mark.skipif(sys.platform != "linux", reason="native inotify backend is Linux only")
async def test_real_directory_events_follow_natural_configuration_and_rearm(
    inbox_api: tuple[FastAPI, Runtime, httpx.AsyncClient], tmp_path: Path
) -> None:
    app, runtime, client = inbox_api
    directory = tmp_path / "资料"
    directory.mkdir()
    old = directory / "已有资料.txt"
    old.write_text("existing body", encoding="utf-8")
    changes: dict[str, Any] = {"enabled": True, "path": str(directory), "coalesce_seconds": 0}
    config_bytes = runtime.config.path.read_bytes()
    enabled = await client.put("/api/file-inbox/settings", json=changes)
    assert enabled.status_code == 200, enabled.text
    assert app.state.file_inbox.status()["running"] is True
    assert _names(app) == set()

    old.write_text("edited old body", encoding="utf-8")
    content = "PRIVATE_BODY_must_not_be_in_host_metadata"
    first = directory / "新资料.txt"
    first.write_text(content, encoding="utf-8")
    await _wait_for(runtime, lambda: "新资料.txt" in _names(app))
    assert _names(app) == {"新资料.txt"}
    listing = await client.get("/api/file-inbox")
    assert listing.status_code == 200
    assert content not in listing.text and "edited old body" not in listing.text
    record = next(item for item in listing.json()["items"] if item["kind"] == "candidate")
    detail = await client.get(f"/api/file-inbox/{record['id']}")
    assert detail.status_code == 200
    assert detail.json()["path"] == str(first)
    assert detail.json()["external_data"] is True
    assert content not in detail.text

    disabled = await client.put("/api/file-inbox/settings", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert app.state.file_inbox.status()["running"] is False
    (directory / "停用期间.txt").write_text("not observed", encoding="utf-8")
    reenabled = await client.put("/api/file-inbox/settings", json=changes)
    assert reenabled.status_code == 200, reenabled.text
    assert app.state.file_inbox.status()["running"] is True
    assert _names(app) == set()
    assert (await client.get(f"/api/file-inbox/{record['id']}")).status_code == 404
    (directory / "重启观察后.txt").write_text("new arrival", encoding="utf-8")
    await _wait_for(runtime, lambda: "重启观察后.txt" in _names(app))
    assert _names(app) == {"重启观察后.txt"}
    assert runtime.config.path.read_bytes() == config_bytes
    # A normal configuration save must also keep the old strict JSON shape.
    runtime.config.update({"default_mode": "auto"})
    assert "file_inbox" not in runtime.config.path.read_text(encoding="utf-8")
    assert (await client.get("/api/file-inbox/settings")).json()["desired"]["path"] == str(
        directory
    )


@pytest.mark.skipif(sys.platform != "linux", reason="native inotify backend is Linux only")
async def test_workspace_restriction_and_symlink_do_not_start_a_watch(
    inbox_api: tuple[FastAPI, Runtime, httpx.AsyncClient], tmp_path: Path
) -> None:
    app, _, client = inbox_api
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(allowed, target_is_directory=True)
    # Both directories are temporary. Tightening the access root leaves the
    # existing tmp_path directory outside it without touching personal data.
    response = await client.patch("/api/settings", json={"workspace_roots": [str(allowed)]})
    assert response.status_code == 200, response.text
    response = await client.put(
        "/api/file-inbox/settings", json={"enabled": True, "path": str(tmp_path)}
    )
    assert response.status_code == 400
    assert "file_inbox_outside_workspace" in response.text
    assert (await client.get("/api/file-inbox/settings")).json()["desired"]["enabled"] is False
    assert app.state.file_inbox.status()["running"] is False
    await client.patch("/api/settings", json={"workspace_roots": [str(tmp_path)]})
    response = await client.put(
        "/api/file-inbox/settings", json={"enabled": True, "path": str(alias)}
    )
    assert response.status_code == 200, response.text
    assert app.state.file_inbox.status()["running"] is False
    assert _names(app) == set()


@pytest.mark.skipif(sys.platform != "linux", reason="native inotify backend is Linux only")
async def test_real_ancestor_loss_keeps_its_cause_and_stops_observation(
    inbox_api: tuple[FastAPI, Runtime, httpx.AsyncClient], tmp_path: Path
) -> None:
    app, runtime, client = inbox_api
    ancestor = tmp_path / "temporary-parent"
    directory = ancestor / "inbox"
    directory.mkdir(parents=True)
    response = await client.put(
        "/api/file-inbox/settings",
        json={"enabled": True, "path": str(directory), "coalesce_seconds": 0},
    )
    assert response.status_code == 200, response.text
    (directory / "before.txt").write_text("candidate", encoding="utf-8")
    await _wait_for(runtime, lambda: "before.txt" in _names(app))
    moved = tmp_path / "temporary-parent-moved"
    ancestor.rename(moved)
    await _wait_for(
        runtime,
        lambda: any(item["kind"] == "gap" for item in app.state.file_inbox.list_records(100)),
    )
    listing = (await client.get("/api/file-inbox")).json()
    gaps = [item for item in listing["items"] if item["kind"] == "gap"]
    assert len(gaps) == 1
    assert gaps[0]["code"] == "identity_changed"
    assert listing["status"]["code"] == "identity_changed"
    assert listing["status"]["running"] is False
    moved.rename(ancestor)
    (directory / "after.txt").write_text("not observed after invalidation", encoding="utf-8")
    # Reading status after the path returns cannot silently rebind or turn the
    # original failure into the generic error from checking a closed watcher.
    listing = (await client.get("/api/file-inbox")).json()
    assert listing["status"]["code"] == "identity_changed"
    assert listing["status"]["running"] is False
    assert "after.txt" not in _names(app)
