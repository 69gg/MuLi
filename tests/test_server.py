from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from muli.config import ConfigFile
from muli.runtime import Runtime
from muli.server import create_app


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)]})
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as connection:
            yield connection


async def test_workspace_and_status_without_configured_model(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "沐璃" in response.text
    status = (await client.get("/api/status")).json()
    assert status["configured"] is False
    assert status["computer"]["available"] is False
    assert status["running"] == 0
    assert "frame-ancestors" in response.headers["content-security-policy"]


async def test_secret_roundtrip_is_redacted_and_preserved(client: httpx.AsyncClient) -> None:
    response = await client.patch(
        "/api/settings",
        json={
            "models": {
                "main": {
                    "model": "test",
                    "base_url": "https://unused.invalid/v1",
                    "api_key": "not-a-real-key",
                    "extra_headers": {"X-Secret": "sensitive-header-token-fixture"},
                }
            }
        },
    )
    assert response.status_code == 200
    assert "not-a-real-key" not in response.text
    assert "sensitive-header-token-fixture" not in response.text
    assert response.json()["models"]["main"]["extra_headers"] == {"X-Secret": "••••"}
    assert response.json()["models"]["main"]["api_key_set"] is True
    await client.patch("/api/settings", json={"models": {"main": {"api_key": ""}}})
    assert (await client.get("/api/settings")).json()["models"]["main"]["api_key_set"] is True
    await client.patch("/api/settings", json={"models": {"main": {"clear_api_key": True}}})
    assert (await client.get("/api/settings")).json()["models"]["main"]["api_key_set"] is False


async def test_cross_site_and_dns_rebinding_rejected(client: httpx.AsyncClient) -> None:
    assert (
        await client.post("/api/sessions", json={}, headers={"Origin": "https://untrusted.example"})
    ).status_code == 403
    assert (
        await client.get("/api/settings", headers={"Host": "rebind.example"})
    ).status_code == 403
    assert (
        await client.post("/api/sessions", content="{}", headers={"Content-Type": "text/plain"})
    ).status_code == 415
    assert (
        await client.post("/api/sessions", json={}, headers={"Origin": "http://127.0.0.1:8765"})
    ).status_code == 200


async def test_session_management_and_failed_send_do_not_fake_reply(
    client: httpx.AsyncClient,
) -> None:
    session = (await client.post("/api/sessions", json={"title": "工作", "mode": "ask"})).json()
    identifier = session["id"]
    assert (
        await client.patch(f"/api/sessions/{identifier}", json={"title": "重命名"})
    ).status_code == 200
    response = await client.post(f"/api/sessions/{identifier}/messages", json={"content": "你好"})
    assert response.status_code == 400
    detail = (await client.get(f"/api/sessions/{identifier}")).json()
    assert detail["messages"] == []
    assert detail["session"]["title"] == "重命名"
    assert (await client.delete(f"/api/sessions/{identifier}")).json()["deleted"]
    assert (await client.get(f"/api/sessions/{identifier}")).status_code == 404


async def test_memory_graph_evidence_and_cascading_forget(client: httpx.AsyncClient) -> None:
    item = (
        await client.post(
            "/api/memories",
            json={
                "content": "沐璃使用Python",
                "kind": "fact",
                "entities": ["沐璃", "Python"],
                "relations": [{"subject": "沐璃", "predicate": "使用", "object": "Python"}],
            },
        )
    ).json()
    graph = (await client.get("/api/memories/graph")).json()
    assert graph["edges"][0]["memory_id"] == item["id"]
    assert (await client.get(f"/api/memories/{item['id']}")).json()["content"] == item["content"]
    assert (await client.get("/api/memories", params={"query": "Python"})).json()["items"][0][
        "id"
    ] == item["id"]
    await client.delete(f"/api/memories/{item['id']}")
    assert (await client.get("/api/memories/graph")).json()["edges"] == []


async def test_workflow_cycle_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/workflows",
        json={
            "name": "cycle",
            "steps": [
                {"id": "a", "title": "A", "prompt": "test", "role": "worker", "depends_on": ["b"]},
                {
                    "id": "b",
                    "title": "B",
                    "prompt": "test",
                    "role": "reviewer",
                    "depends_on": ["a"],
                },
            ],
        },
    )
    assert response.status_code == 400
    assert (await client.get("/api/workflows")).json()["items"] == []


async def test_chunked_body_limit_before_json_decode(client: httpx.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 524288

    response = await client.post(
        "/api/sessions", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


async def test_export_contains_messages_and_revision_evidence(client: httpx.AsyncClient) -> None:
    item = (await client.post("/api/memories", json={"content": "旧偏好"})).json()
    await client.patch(f"/api/memories/{item['id']}", json={"content": "新偏好"})
    revisions = (await client.get(f"/api/memories/{item['id']}/revisions")).json()
    assert revisions["items"][0]["snapshot"]["content"] == "旧偏好"
    response = await client.get("/api/export")
    assert response.status_code == 200
    assert "application/x-ndjson" in response.headers["content-type"]
    assert "新偏好" in response.text
    assert "memory_revisions" in response.text


async def test_api_reference_is_local_and_schema_is_available(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/docs")
    assert response.status_code == 200
    assert 'src="/api-docs.mjs"' in response.text
    assert "cdn." not in response.text
    assert (await client.get("/api-docs.mjs")).status_code == 200
    schema = (await client.get("/api/openapi.json")).json()
    assert "post" in schema["paths"]["/api/sessions/{session_id}/messages"]
