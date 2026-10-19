from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.config import ConfigFile
from muli.runtime import Runtime
from muli.server import create_app


@pytest.mark.parametrize("has_brief", [False, True])
async def test_task_detail_requires_login_and_returns_complete_saved_revision(
    tmp_path: Path, has_brief: bool
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {"server": {"password": "local-test-password"}, "updates": {"check_on_start": False}}
    )
    runtime = Runtime(config, tmp_path / "data")
    item: dict[str, Any] = {
        "id": "task-record",
        "title": "准备文档",
        "revision": 7,
        "status": "in_progress",
        "next_step": "检查引用",
        "assignee": "worker",
    }
    if has_brief:
        item["brief"] = {
            "version": 1,
            "goal": "完整任务目标",
            "constraints": ["开头" + "完整正文" * 3000 + "结尾"],
            "evidence_refs": [],
        }
    runtime.store.put("commitment", item)
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as client:
            path = "/api/commitments/task-record"
            denied = await client.get(path)
            assert denied.status_code == 401
            assert "完整正文" not in denied.text
            logged_in = await client.post(
                "/api/auth/login", json={"password": "local-test-password"}
            )
            assert logged_in.status_code == 200
            response = await client.get(path)
            assert response.status_code == 200
            detail = response.json()
            assert {key: detail[key] for key in item} == item
            assert detail["control"]["epoch"] == 0
            assert detail["control"]["paused"] is False
            assert detail["scope"]["member_count"] == 0
            assert (await client.get("/api/commitments/missing")).status_code == 404
            assert runtime.store.get("commitment", item["id"]) == item
            assert runtime.tasks == {}
