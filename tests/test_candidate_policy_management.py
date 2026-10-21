from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from muli.candidate_policy import CandidateSettings
from muli.config import ConfigFile
from muli.maintenance import MaintenanceError
from muli.maintenance_api import passive_request_allowed, request_has_effect
from muli.server import create_app

POLICY_PATH = "/api/management/update-policy"


@pytest.fixture
async def policy_app(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}, "server": {"password": "policy-fixture"}})
    app = create_app(config, tmp_path / "data")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client,
    ):
        assert (
            await client.post("/api/auth/login", json={"password": "policy-fixture"})
        ).status_code == 200
        yield app, client


async def test_policy_is_managed_without_changing_legacy_configuration(
    policy_app: tuple[FastAPI, httpx.AsyncClient], tmp_path: Path
) -> None:
    app, client = policy_app
    runtime, management = app.state.runtime, app.state.management
    before = runtime.config.path.read_bytes()
    policy = CandidateSettings(
        enabled=True,
        wheelhouse=str(tmp_path / "uncreated-materials"),
        retained_root=str(tmp_path / "uncreated-retained"),
        uv_executable=str(tmp_path / "not-executed-uv"),
    ).model_dump(mode="json")
    response = await client.put(POLICY_PATH, json=policy)
    assert response.status_code == 200
    assert response.json() == policy
    assert (await client.get(POLICY_PATH)).json() == policy
    configuration = await runtime.tools["self_config"].handler({})
    assert configuration["candidate_update_policy"] == policy
    assert "candidate_update_policy" not in (await client.get("/api/settings")).json()
    assert runtime.config.path.read_bytes() == before
    assert not management.plans()
    assert not (tmp_path / "uncreated-materials").exists()
    assert not (tmp_path / "uncreated-retained").exists()
    tool = runtime.tools["self_update_configure"]
    assert tool.risk == "write"
    # A full replacement intentionally resets omitted fields to their defaults.
    result = await tool.handler({"source": "replacement"})
    assert result == CandidateSettings(source="replacement").model_dump(mode="json")
    assert (await client.get(POLICY_PATH)).json() == result
    assert runtime.config.path.read_bytes() == before


async def test_policy_endpoint_keeps_owner_origin_and_json_boundaries(
    policy_app: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    app, client = policy_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
    ) as stranger:
        assert (await stranger.get(POLICY_PATH)).status_code == 401
        assert (await stranger.put(POLICY_PATH, json={})).status_code == 401
    assert (
        await client.put(POLICY_PATH, json={}, headers={"Origin": "https://foreign.invalid"})
    ).status_code == 403
    assert (
        await client.put(POLICY_PATH, json={}, headers={"Host": "foreign.invalid"})
    ).status_code == 403
    assert (
        await client.put(POLICY_PATH, content="{}", headers={"Content-Type": "text/plain"})
    ).status_code == 415
    assert app.state.management.update_policy.read() == CandidateSettings()


@pytest.mark.parametrize(
    "body",
    [{"enabled": True}, {"extra": True}, {"limits": {"memory_bytes": 2 * 1024**3}}],
)
async def test_invalid_policy_does_not_mutate_existing_configuration(
    policy_app: tuple[FastAPI, httpx.AsyncClient], body: dict[str, Any]
) -> None:
    app, client = policy_app
    before = app.state.runtime.config.path.read_bytes()
    assert (await client.put(POLICY_PATH, json=body)).status_code == 422
    assert app.state.management.update_policy.read() == CandidateSettings()
    assert app.state.runtime.config.path.read_bytes() == before


async def test_policy_replace_waits_for_configuration_lock_and_rejects_closing(
    policy_app: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    app, client = policy_app
    management, runtime = app.state.management, app.state.runtime
    with pytest.raises(MaintenanceError, match="configuration_lock_required"):
        management.replace_update_policy(CandidateSettings(source="rejected"))
    async with runtime.configuration_lock:
        pending = asyncio.create_task(client.put(POLICY_PATH, json={"source": "queued"}))
        await asyncio.sleep(0.02)
        assert not pending.done()
        management._closing = True
    try:
        response = await asyncio.wait_for(pending, 2)
        assert response.status_code == 503
        assert response.json()["reason"] == "service_closing"
        assert management.update_policy.read() == CandidateSettings()
    finally:
        management._closing = False


def test_passive_policy_read_does_not_allow_policy_mutation() -> None:
    assert passive_request_allowed("GET", POLICY_PATH, None)
    assert not request_has_effect("GET", POLICY_PATH, None)
    assert not passive_request_allowed("PUT", POLICY_PATH, None)
    assert request_has_effect("PUT", POLICY_PATH, None)
