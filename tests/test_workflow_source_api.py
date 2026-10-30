"""来源检查通过真实 ASGI 路由保持登录、分页与维护期只读边界。"""

from __future__ import annotations

from pathlib import Path

import httpx
from test_maintenance_api import host_app
from test_runtime import make_runtime
from test_workflow_sources import saved_source

from muli.runtime import Runtime
from muli.server import create_app
from muli.workflow_sources import WorkflowSources


def objects(runtime: Runtime) -> list[tuple[str, str, str]]:
    return [
        tuple(row)
        for row in runtime.store.db.execute("SELECT kind,id,data FROM objects ORDER BY kind,id")
    ]


async def test_inspection_requires_login_and_preserves_exact_read_only_pages(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    try:
        runtime.config.update(
            {
                "server": {"password": "source-fixture-password"},
                "updates": {"check_on_start": False},
                "attention": {"enabled": False},
            }
        )
        await runtime.reconfigure()
        source = saved_source(runtime)
        expected = WorkflowSources(runtime).inspect(source["id"])
        app = create_app(runtime.config, runtime.data_dir, runtime)
        url = f"/api/workflow-runs/{source['id']}/inspection"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765"
        ) as client:
            assert (await client.get(url)).status_code == 401
            login = await client.post(
                "/api/auth/login", json={"password": "source-fixture-password"}
            )
            assert login.status_code == 200
            before = objects(runtime)
            directory = await client.get(url)
            assert directory.status_code == 200 and directory.json() == expected
            assert directory.headers["cache-control"] == "no-store"
            params: dict[str, str | int] = {
                "node_id": "read",
                "revision": expected["revision"],
                "length_bytes": 32,
            }
            page = await client.get(url, params=params)
            assert page.status_code == 200
            first = page.json()
            assert first["has_more"] and first["producing_run_id"] == "source-execution"
            params["offset_bytes"] = first["next_offset_bytes"]
            following = await client.get(url, params=params)
            assert following.status_code == 200
            assert following.json()["offset_bytes"] == first["next_offset_bytes"]
            changed = await client.get(url, params={**params, "revision": "0" * 64})
            assert changed.status_code == 409
            assert changed.json()["detail"]["code"] == "evidence_changed"
            assert (await client.get(url, params={"offset_bytes": -1})).status_code == 422
            assert (await client.get(url, params={"length_bytes": 0})).status_code == 422
            assert objects(runtime) == before
            assert not seen and not runtime.tasks and not runtime.workflows.active_run_ids
    finally:
        await runtime.close()


async def test_inspection_stays_available_during_passive_maintenance(tmp_path: Path) -> None:
    async with host_app(tmp_path, passive=True) as (app, _, _):
        runtime = app.state.runtime
        source = saved_source(runtime)
        before = objects(runtime)
        assert not runtime.maintenance.admission.allowed
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get(f"/api/workflow-runs/{source['id']}/inspection")
            assert response.status_code == 200
            assert response.json()["run_id"] == source["id"]
            assert (await client.post("/api/workflows", json={})).status_code == 409
            assert not runtime.maintenance.admission.allowed
            assert objects(runtime) == before
            assert not runtime.started and not runtime.tasks
