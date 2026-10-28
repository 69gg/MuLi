"""The management directory is bounded and cannot disclose delegation authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_maintenance_api import host_app
from test_workflow_active_api import case as case

from muli.auth import hash_password
from muli.config import ConfigFile
from muli.runtime import Runtime
from muli.server import create_app


def schedule(runtime: Runtime, identifier: str) -> dict[str, Any]:
    return runtime.store.put(
        "workflow_schedule",
        {
            "id": identifier,
            "name": "研究计划",
            "version": 1,
            "active": True,
            "next_due_at": 2_000_000_000.0,
            "interval_seconds": None,
            "fired_count": 0,
            "created_at": 100.0,
            "updated_at": 100.0,
            "last_occurrence_id": None,
            "prompt": "private-prompt-not-in-directory",
            "snapshot": {"brief_json": "private-task-snapshot"},
            "reply_origin": {"target_id": "private-channel-target"},
        },
    )


async def test_schedule_directory_pages_without_authorization_payloads(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    for index in range(55):
        schedule(runtime, f"schedule-{index:03d}")
    blocked = runtime.store.get("workflow_schedule", "schedule-000")
    runtime.store.put("workflow_schedule", {**blocked, "blocked": "PermissionError"})
    response = await client.get("/api/workflow-schedules")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    first = response.json()
    assert len(first["items"]) == 50 and first["has_more"] is True
    assert first["next_after"] == "schedule-049"
    assert set(first["items"][0]) == {
        "id",
        "kind",
        "name",
        "version",
        "enabled",
        "blocked",
        "active",
        "due_at",
        "next_due_at",
        "interval_seconds",
        "fired_count",
        "created_at",
        "updated_at",
        "last_run_id",
        "last_status",
    }
    assert "private-" not in json.dumps(first)
    assert first["items"][0]["blocked"] is True
    assert first["items"][1]["blocked"] is False
    assert "PermissionError" not in json.dumps(first)
    runtime.store.delete("workflow_schedule", first["next_after"])
    last = (
        await client.get("/api/workflow-schedules", params={"after_id": first["next_after"]})
    ).json()
    assert [item["id"] for item in last["items"]] == [f"schedule-{i:03d}" for i in range(50, 55)]
    assert last["has_more"] is False and last["next_after"] is None


async def test_calendar_directory_and_cancel_use_the_host_kind_even_when_ids_match(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    ordinary = schedule(runtime, "same-id")
    rule = {"version": 1, "timezone": "Asia/Shanghai", "local_time": "08:00", "weekdays": [1]}
    runtime.store.put(
        "calendar_workflow_schedule",
        {**ordinary, "kind": "workflow_schedule", "calendar": rule, "interval_seconds": None},
    )
    first = (await client.get("/api/workflow-schedules", params={"limit": 1})).json()
    assert len(first["items"]) == 1
    assert first["items"][0]["kind"] == first["next_after_kind"] == "calendar_workflow_schedule"
    assert first["items"][0]["calendar"] == rule
    assert first["next_after"] == "same-id" and first["has_more"] is True
    following = (
        await client.get(
            "/api/workflow-schedules",
            params={
                "after_id": first["next_after"],
                "after_kind": first["next_after_kind"],
                "limit": 1,
            },
        )
    ).json()
    assert [item["kind"] for item in following["items"]] == ["workflow_schedule"]
    assert following["has_more"] is False and following["next_after_kind"] is None
    assert "private-" not in json.dumps(first) + json.dumps(following)
    cancelled = await client.post(
        "/api/workflow-schedules/same-id/cancel",
        params={"kind": "calendar_workflow_schedule"},
        json={},
    )
    assert cancelled.status_code == 200 and cancelled.json()["kind"] == "calendar_workflow_schedule"
    assert runtime.store.get("calendar_workflow_schedule", "same-id")["active"] is False
    assert runtime.store.get("workflow_schedule", "same-id") == ordinary
    assert (
        await client.post(
            "/api/workflow-schedules/same-id/cancel", params={"kind": "unknown"}, json={}
        )
    ).status_code == 422
    assert (
        await client.get("/api/workflow-schedules", params={"after_kind": "unknown"})
    ).status_code == 422
    runtime.store.delete("calendar_workflow_schedule", "same-id")
    assert (
        await client.post(
            "/api/workflow-schedules/same-id/cancel",
            params={"kind": "calendar_workflow_schedule"},
            json={},
        )
    ).status_code == 400
    assert runtime.store.get("workflow_schedule", "same-id") == ordinary


@pytest.mark.parametrize(
    "parameters",
    [{"limit": "0"}, {"limit": "51"}, {"limit": "true"}, {"after_id": "x" * 201}],
)
async def test_schedule_directory_rejects_invalid_page_arguments(
    case: tuple[Runtime, httpx.AsyncClient], parameters: dict[str, str]
) -> None:
    _, client = case
    assert (await client.get("/api/workflow-schedules", params=parameters)).status_code == 422


async def test_exact_schedule_cancel_revokes_future_authority_and_keeps_history(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    original = schedule(runtime, "original")
    other = schedule(runtime, "other")
    url = "/api/workflow-schedules/original/cancel"
    assert (
        await client.post(url, json={}, headers={"origin": "https://elsewhere.invalid"})
    ).status_code == 403
    assert runtime.store.get("workflow_schedule", "original") == original
    cancelled = await client.post(url, json={})
    assert cancelled.status_code == 200
    item = cancelled.json()
    assert item["id"] == "original" and item["enabled"] is False
    assert item["due_at"] is None and item["version"] == 2
    saved = runtime.store.get("workflow_schedule", "original")
    assert saved["prompt"] == original["prompt"] and saved["fired_count"] == 0
    assert runtime.store.get("workflow_schedule", "other") == other
    repeated = await client.post(url, json={})
    assert repeated.status_code == 200 and repeated.json()["version"] == 2


async def test_schedule_management_requires_login(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"server": {"password_hash": hash_password("test-password")}})
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    try:
        schedule(runtime, "private")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            assert (await client.get("/api/workflow-schedules")).status_code == 401
            assert (
                await client.post("/api/workflow-schedules/private/cancel", json={})
            ).status_code == 401
            assert runtime.store.get("workflow_schedule", "private")["active"] is True
            assert (
                await client.post("/api/auth/login", json={"password": "test-password"})
            ).status_code == 200
            assert (await client.get("/api/workflow-schedules")).status_code == 200
    finally:
        await runtime.close()


async def test_maintenance_allows_schedule_read_but_blocks_cancel(tmp_path: Path) -> None:
    async with host_app(tmp_path) as (app, _journal, _record):
        runtime = app.state.runtime
        original = schedule(runtime, "paused")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get("/api/workflow-schedules")).status_code == 200
            assert (
                await client.post("/api/workflow-schedules/paused/cancel", json={})
            ).status_code == 409
            assert runtime.store.get("workflow_schedule", "paused") == original
