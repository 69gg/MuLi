from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from test_management_http_wiring import Host
from test_management_http_wiring import host as host

from muli.managed_requests import LocalCandidate


def candidate(path: Path) -> dict[str, str]:
    return {
        "prefix": str(path / "retained"),
        "python": str(path / "retained/bin/python"),
        "package_root": str(path / "retained/lib/muli"),
    }


@pytest.mark.parametrize(
    "body",
    [
        {"action": "adopt"},
        {"action": "switch", "candidate": {}},
        {"action": "restart", "candidate": {"prefix": "/v", "python": "/p", "package_root": "/m"}},
        {
            "action": "switch",
            "candidate": {"prefix": "relative", "python": "/p", "package_root": "/m"},
        },
        {
            "action": "adopt",
            "candidate": {
                "prefix": "/v",
                "python": "/p",
                "package_root": "/m",
                "command": "ignored",
            },
        },
        {
            "action": "switch",
            "candidate": {"prefix": "/v", "python": "/p", "package_root": "/m"},
            "changes": {"persona": {"name": "unexpected"}},
        },
    ],
)
async def test_candidate_contract_rejects_before_plan_or_service(
    host: Host, body: dict[str, Any]
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post("/api/management/transactions", json=body)
    assert response.status_code == 422
    assert not host.app.state.management.plans() and not host.raw_actions
    assert list(host.app.state.management.directory.iterdir()) == []


@pytest.mark.parametrize("action", ["adopt", "switch"])
async def test_ordinary_foreground_cannot_queue_managed_handoff(host: Host, action: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/management/transactions",
            json={"action": action, "candidate": candidate(host.runtime.data_dir)},
        )
    assert response.status_code == 409
    assert response.json()["reason"] == "managed_boot_required"
    assert not host.app.state.management.plans() and not host.raw_actions


@pytest.mark.parametrize("action", ["adopt", "switch"])
async def test_public_candidate_is_queued_without_installing_or_claiming_validation(
    host: Host, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    manager = host.app.state.management
    observed: list[str] = []
    monkeypatch.setattr(manager.recovery, "require_managed_request", observed.append)
    paths = candidate(host.runtime.data_dir)
    original = host.runtime.config.path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/management/transactions", json={"action": action, "candidate": paths}
        )
    assert response.status_code == 200
    plan = response.json()
    assert plan["status"] == "pending" and plan["apply_mode"] == "restart_transaction"
    assert plan["candidate"] == paths and "transaction" not in plan
    assert observed == [action] and not host.raw_actions
    assert not Path(paths["prefix"]).exists()
    assert host.runtime.config.path.read_bytes() == original


async def test_self_tools_share_candidate_schema_and_switch_requires_real_approval(
    host: Host,
) -> None:
    tools = host.runtime.tools
    assert tools["self_switch"].requires_approval
    assert tools["self_adopt"].risk == "write"
    assert tools["self_switch"].parameters == LocalCandidate.model_json_schema()
    with pytest.raises(ValueError):
        await tools["self_switch"].handler(
            {"prefix": "relative", "python": "/p", "package_root": "/m"}
        )
    assert not host.app.state.management.plans() and not host.raw_actions
