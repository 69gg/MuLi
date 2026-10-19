from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from test_workflows import step

from muli.auth import hash_password
from muli.config import ConfigFile
from muli.runtime import Runtime
from muli.server import create_app


@pytest.fixture
async def case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(tmp_path)],
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
        }
    )
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            yield runtime, client
    finally:
        await runtime.close()


def historical(runtime: Runtime, count: int) -> None:
    runtime.store.put_many(
        [
            (
                "workflow_run",
                {
                    "id": f"history-{index:04d}",
                    "workflow_id": "another-definition",
                    "name": "旧任务",
                    "status": "completed",
                    "result": "旧结果",
                },
            )
            for index in range(count)
        ]
    )


@pytest.mark.parametrize("history_count", [0, 201])
async def test_real_running_workflow_is_reachable_and_cannot_be_deleted(
    case: tuple[Runtime, httpx.AsyncClient], history_count: int
) -> None:
    runtime, client = case
    entered, release = asyncio.Event(), asyncio.Event()

    async def run_step(session_id: str, prompt: str, role: str, mode: str) -> str:
        entered.set()
        await release.wait()
        return "已完成"

    runtime.workflows.run_step = run_step
    definition = runtime.workflows.create("持续执行", [step("work")])
    active = await runtime.workflows.start(definition["id"])
    try:
        await asyncio.wait_for(entered.wait(), 2)
        historical(runtime, history_count)
        if history_count:
            runtime.store.put_many(
                [
                    (
                        "workflow",
                        {"id": f"recent-definition-{i}", "name": f"最近定义{i}", "steps": []},
                    )
                    for i in range(201)
                ]
            )
            old = (await client.get("/api/workflows")).json()
            assert definition["id"] not in {item["id"] for item in old["items"]}
        response = await client.get("/api/workflow-runs/active")
        assert response.status_code == 200
        page = response.json()
        assert [item["id"] for item in page["items"]] == [active["id"]]
        item = page["items"][0]
        assert set(item) == {"id", "workflow_id", "name", "status", "created_at", "updated_at"}
        assert item["status"] == "running" and item["workflow_id"] == definition["id"]
        assert page["has_more"] is False and page["next_after"] is None
        assert active["id"] in runtime.workflows.active_run_ids
        deletion = await client.delete(f"/api/workflows/{definition['id']}")
        assert deletion.status_code == 409
        assert runtime.store.get("workflow", definition["id"]) is not None
        release.set()
        assert (await asyncio.wait_for(runtime.workflows.wait(active["id"]), 2))[
            "status"
        ] == "completed"
        assert (await client.get("/api/workflow-runs/active")).json()["items"] == []
        assert (await client.delete(f"/api/workflows/{definition['id']}")).json() == {
            "deleted": True
        }
        assert runtime.store.get("workflow_run", active["id"])["result"] == "已完成"
        assert len(runtime.store.list("workflow_run", 500)) == history_count + 1
    finally:
        release.set()
        await runtime.workflows.cancel(active["id"])


async def test_active_keyset_pages_keep_all_states_without_results_or_prompts(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    for index in range(55):
        runtime.store.put(
            "workflow_run",
            {
                "id": f"active-{index:03d}",
                "workflow_id": "definition",
                "name": "并发任务",
                "status": ("pending", "queued", "running")[index % 3],
                "created_at": index,
                "updated_at": index,
                "nodes": {"one": {"prompt": "不得出现在摘要" * 1000}},
                "result": "secret-output" * 1000,
            },
        )
    historical(runtime, 201)
    first = (await client.get("/api/workflow-runs/active")).json()
    assert len(first["items"]) == 50 and first["has_more"]
    assert first["next_after"] == "active-049"
    assert {item["status"] for item in first["items"]} == {"pending", "queued", "running"}
    assert "secret-output" not in json.dumps(first, ensure_ascii=False)
    assert "不得出现在摘要" not in json.dumps(first, ensure_ascii=False)
    # Changing updated ordering and removing the anchor must not shift a keyset page.
    runtime.store.delete("workflow_run", first["next_after"])
    for item in first["items"][:10]:
        runtime.store.put("workflow_run", {**item, "status": "completed", "updated_at": 999999})
    last = (
        await client.get("/api/workflow-runs/active", params={"after_id": first["next_after"]})
    ).json()
    assert [item["id"] for item in last["items"]] == [f"active-{i:03d}" for i in range(50, 55)]
    assert last["next_after"] is None and not last["has_more"]
    runtime.store.put(
        "workflow_run",
        {"id": "a-new", "name": "新任务", "status": "running", "workflow_id": "other"},
    )
    fresh = (await client.get("/api/workflow-runs/active", params={"limit": 1})).json()
    assert fresh["items"][0]["id"] == "a-new" and fresh["next_after"] == "a-new"
    assert fresh["items"][0]["created_at"] is None and fresh["items"][0]["updated_at"] is None


@pytest.mark.parametrize(
    "parameters",
    [
        {"limit": "0"},
        {"limit": "51"},
        {"limit": "-1"},
        {"limit": "true"},
        {"limit": "1.5"},
        {"after_id": "x" * 201},
    ],
    ids=["zero", "oversized", "negative", "boolean", "fraction", "oversized-cursor"],
)
async def test_active_endpoint_rejects_invalid_page_parameters(
    case: tuple[Runtime, httpx.AsyncClient], parameters: dict[str, str]
) -> None:
    _, client = case
    assert (await client.get("/api/workflow-runs/active", params=parameters)).status_code == 422


async def test_legacy_pending_blocks_definition_delete_but_terminal_history_does_not(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    definition = runtime.workflows.create("旧等待状态", [step("work")])
    runtime.store.put(
        "workflow_run", {"id": "legacy", "workflow_id": definition["id"], "status": "pending"}
    )
    historical(runtime, 201)
    assert (await client.delete(f"/api/workflows/{definition['id']}")).status_code == 409
    runtime.store.put(
        "workflow_run", {"id": "legacy", "workflow_id": definition["id"], "status": "interrupted"}
    )
    assert (await client.delete(f"/api/workflows/{definition['id']}")).json()["deleted"]
    assert runtime.store.get("workflow_run", "legacy")["status"] == "interrupted"


async def test_same_definition_has_two_independent_run_ids_and_exact_cancel(
    case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = case
    entered = asyncio.Event()
    running = 0
    release = asyncio.Event()

    async def run_step(session_id: str, prompt: str, role: str, mode: str) -> str:
        nonlocal running
        running += 1
        if running == 2:
            entered.set()
        await release.wait()
        return "保留另一执行"

    runtime.workflows.run_step = run_step
    definition = runtime.workflows.create("并行同名执行", [step("work")])
    first = await runtime.workflows.start(definition["id"])
    second = await runtime.workflows.start(definition["id"])
    try:
        await asyncio.wait_for(entered.wait(), 2)
        page = (await client.get("/api/workflow-runs/active")).json()
        assert {item["id"] for item in page["items"]} == {first["id"], second["id"]}
        assert (
            await client.post(f"/api/workflow-runs/{first['id']}/cancel", json={})
        ).status_code == 200
        active = (await client.get("/api/workflow-runs/active")).json()
        assert [item["id"] for item in active["items"]] == [second["id"]]
        assert runtime.store.get("workflow_run", second["id"])["status"] == "running"
        assert (await client.delete(f"/api/workflows/{definition['id']}")).status_code == 409
        release.set()
        assert (await asyncio.wait_for(runtime.workflows.wait(second["id"]), 2))[
            "status"
        ] == "completed"
    finally:
        release.set()
        await runtime.workflows.cancel(first["id"])
        await runtime.workflows.cancel(second["id"])


async def test_active_listing_uses_existing_auth_and_no_store_boundary(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "updates": {"check_on_start": False},
            "server": {"password_hash": hash_password("test-password")},
        }
    )
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            assert (await client.get("/api/workflow-runs/active")).status_code == 401
            assert (
                await client.post("/api/auth/login", json={"password": "test-password"})
            ).status_code == 200
            response = await client.get("/api/workflow-runs/active")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    finally:
        await runtime.close()
