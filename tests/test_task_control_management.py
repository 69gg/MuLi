from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from muli.config import ConfigFile
from muli.local_services import LocalServices
from muli.runtime import Runtime
from muli.server import create_app

ControlFixture = tuple[Runtime, LocalServices, httpx.AsyncClient, str]


@pytest.fixture
async def control_client(tmp_path: Path) -> AsyncIterator[ControlFixture]:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {"updates": {"check_on_start": False}, "server": {"password": "local-control-test"}}
    )
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        local: LocalServices = app.state.local_services
        item = await local.commitments.save(
            "核查本地项目",
            brief={"goal": "核查并整理结果", "constraints": ["保留原文件"], "evidence_refs": []},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            yield runtime, local, client, item["id"]


async def login(client: httpx.AsyncClient) -> None:
    result = await client.post("/api/auth/login", json={"password": "local-control-test"})
    assert result.status_code == 200


def pause_request(epoch: int, request_id: str) -> dict[str, Any]:
    return {"action": "pause", "expected_epoch": epoch, "request_id": request_id}


def configure_without_execution(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime.config.update(
        {"models": {"main": {"model": "fake", "base_url": "https://unused.invalid/v1"}}}
    )

    def leave_input_pending() -> None:
        pass

    monkeypatch.setattr(runtime, "_wake_main", leave_input_pending)


async def test_owner_control_uses_existing_auth_origin_and_json_boundaries(
    control_client: ControlFixture,
) -> None:
    runtime, local, client, identifier = control_client
    path = f"/api/commitments/{identifier}/control"
    before = local.commitments.get(identifier)
    body = pause_request(0, "pause-1")
    assert (await client.post(path, json=body)).status_code == 401
    await login(client)
    cross_origin = await client.post(path, json=body, headers={"origin": "https://outside.invalid"})
    assert cross_origin.status_code == 403
    assert (await client.post(path, content="not-json")).status_code == 415
    assert local.commitments.get(identifier) == before
    assert runtime.tasks == {}
    assert (await client.post("/api/commitments/missing/control", json=body)).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_epoch": True},
        {"expected_epoch": "0"},
        {"expected_epoch": -1},
        {"principal": "owner"},
        {"session_id": "main"},
        {"approved_epoch": 99},
        {"additional_requirements": False},
    ],
)
async def test_control_body_cannot_supply_identity_or_coerce_epoch(
    control_client: ControlFixture, changes: dict[str, Any]
) -> None:
    _, local, client, identifier = control_client
    await login(client)
    before = local.commitments.get(identifier)
    response = await client.post(
        f"/api/commitments/{identifier}/control", json=pause_request(0, "invalid") | changes
    )
    assert response.status_code == 422
    assert local.commitments.get(identifier) == before


@pytest.mark.parametrize(
    "changes",
    [{"request_id": "  "}, {"additional_requirements": "暂停不能顺便改写原要求"}],
)
async def test_invalid_control_semantics_leave_task_unchanged(
    control_client: ControlFixture, changes: dict[str, Any]
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    before = local.commitments.get(identifier)
    response = await client.post(
        f"/api/commitments/{identifier}/control", json=pause_request(0, "invalid") | changes
    )
    assert response.status_code == 400
    assert local.commitments.get(identifier) == before
    assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0


async def test_pause_without_model_and_old_retry_return_receipt_with_current_state(
    control_client: ControlFixture,
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    path = f"/api/commitments/{identifier}/control"
    first = await client.post(path, json=pause_request(0, "first"))
    assert first.status_code == 200
    assert first.json()["current"]["control"]["paused"] is True
    second = await client.post(path, json=pause_request(1, "second"))
    assert second.status_code == 200
    saved = local.commitments.get(identifier)
    retried = await client.post(path, json=pause_request(0, "first"))
    assert retried.status_code == 200
    assert retried.json()["receipt"] == first.json()["receipt"]
    assert retried.json()["current"]["control"]["epoch"] == 2
    assert local.commitments.get(identifier) == saved
    assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 0
    assert runtime.tasks == {}
    conflict = await client.post(path, json=pause_request(0, "new-stale-request"))
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["code"] == "epoch_conflict"
    assert detail["current"]["revision"] == saved["revision"]
    assert not detail["message"].startswith("epoch_conflict:")
    reused = await client.post(path, json=pause_request(2, "first"))
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "request_id_conflict"


async def test_replan_rejects_missing_model_or_full_inbox_without_partial_requirements(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    path = f"/api/commitments/{identifier}/control"
    assert (await client.post(path, json=pause_request(0, "pause"))).status_code == 200
    before = local.commitments.get(identifier)
    request = {
        "action": "request_replan",
        "expected_epoch": 1,
        "request_id": "replan",
        "additional_requirements": "新增限制必须完整保留",
    }
    unavailable = await client.post(path, json=request)
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "model_unconfigured"
    assert local.commitments.get(identifier) == before
    configure_without_execution(runtime, monkeypatch)
    runtime.config.update({"limits": {"max_pending_runs": 1}})
    main = runtime.main_session()
    runtime.conversation.enqueue(
        main["id"], "现有主人输入", source="web", principal="owner", mode="auto", max_pending=1
    )
    full = await client.post(path, json=request)
    assert full.status_code == 429
    assert full.json()["detail"]["code"] == "queue_full"
    assert local.commitments.get(identifier) == before
    assert runtime.store.pending_inputs(main["id"]) == 1


async def test_replan_persists_owner_intent_once_and_detail_keeps_plan_text(
    control_client: ControlFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    configure_without_execution(runtime, monkeypatch)
    path = f"/api/commitments/{identifier}/control"
    request = {
        "action": "request_replan",
        "expected_epoch": 0,
        "request_id": "replan",
        "additional_requirements": "新的完整限制 <script>literal</script>",
    }
    rejected = await client.post(path, json=request)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "not_paused"
    assert (await client.post(path, json=pause_request(0, "pause"))).status_code == 200
    request["expected_epoch"] = 1
    response = await client.post(path, json=request)
    assert response.status_code == 200
    receipt = response.json()["receipt"]
    duplicate = await client.post(path, json=request)
    assert duplicate.status_code == 200 and duplicate.json()["receipt"] == receipt
    main_id = runtime.main_session()["id"]
    inputs = runtime.conversation.pending(main_id)
    assert len(inputs) == 1
    assert inputs[0]["principal"] == "owner" and inputs[0].get("route") is None
    assert inputs[0]["task_control"]["receipt_id"] == receipt["id"]
    assert (
        local.commitments.get(identifier)["brief"]["constraints"][-1]
        == request["additional_requirements"]
    )
    snapshot = local.task_control.replan_snapshot(main_id, identifier)
    plan = "只读核查，再向主人说明未验证事项。"
    local.task_control.approve_plan(
        snapshot, snapshot.epoch, plan, [], caller_session_id=main_id, max_targets=1
    )
    detail = (await client.get(f"/api/commitments/{identifier}")).json()
    assert detail["control"]["plan"]["text"] == plan
    assert detail["control"]["paused"] is False
    assert detail["control"]["has_plan"] is True
    assert detail["brief"]["constraints"][-1] == request["additional_requirements"]
    assert runtime.tasks == {}


async def test_activity_conflict_does_not_partially_pause_workflow_scope(
    control_client: ControlFixture,
) -> None:
    runtime, local, client, identifier = control_client
    await login(client)
    main_id = runtime.main_session()["id"]
    runtime.store.put("session", {"id": "worker", "kind": "team", "mode": "auto"})
    runtime.store.put(
        "agent",
        {
            "id": "worker",
            "session_id": "worker",
            "parent_id": main_id,
            "commitment_id": identifier,
            "status": "completed",
            "mode": "auto",
        },
    )
    runtime.store.put(
        "workflow_run",
        {
            "id": "active-dag",
            "parent_session_id": "worker",
            "commitment_id": identifier,
            "status": "queued",
            "nodes": {},
        },
    )
    before = local.commitments.get(identifier)
    response = await client.post(
        f"/api/commitments/{identifier}/control", json=pause_request(0, "blocked-by-dag")
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "workflow_active"
    assert detail["current"]["scope"]["active_workflow_count"] == 1
    assert detail["current"]["scope"]["can_pause"] is False
    assert local.commitments.get(identifier) == before
