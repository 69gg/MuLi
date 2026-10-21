from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from test_integration_manager import FakeBridge
from test_management_http_wiring import Host, ObservedLock
from test_management_http_wiring import host as host

from muli.integrations.attention import POLICY_ID, POLICY_KIND
from muli.maintenance import MaintenanceError
from muli.maintenance_api import passive_request_allowed

POLICY = "/api/integrations/chiliarch/attention"


@pytest.fixture(autouse=True)
def forbid_real_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("this test must not create an external process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


@pytest.fixture
async def integration_host(host: Host) -> AsyncIterator[Host]:
    host.runtime.config.update(
        {
            "chiliarch": {
                "enabled": True,
                "base_url": "https://chiliarch.example",
                "agent_id": "fixture-agent",
                "credential": "fixture-secret",
            },
        }
    )
    host.app.state.integration.bridge_factory = FakeBridge
    await host.app.state.integration.start()
    yield host


async def test_policy_api_requires_owner_same_origin_and_json(integration_host: Host) -> None:
    host = integration_host
    host.runtime.config.update({"server": {"password": "attention-fixture-password"}})
    before = host.runtime.config.path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        for method in ("GET", "PUT"):
            result = await client.request(
                method, POLICY, **({"json": {"enabled": True}} if method == "PUT" else {})
            )
            assert result.status_code == 401
        assert (
            await client.post("/api/auth/login", json={"password": "attention-fixture-password"})
        ).status_code == 200
        for headers in (
            {"Origin": "http://untrusted.invalid"},
            {"Host": "untrusted.invalid"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert (
                await client.put(POLICY, json={"enabled": True}, headers=headers)
            ).status_code == 403
        assert (
            await client.put(POLICY, content="{}", headers={"Content-Type": "text/plain"})
        ).status_code == 415
        assert host.runtime.store.get(POLICY_KIND, POLICY_ID) is None
        assert (await client.get(POLICY)).json()["enabled"] is False
        revision = host.runtime.bus.revision
        response = await client.put(POLICY, json={"enabled": True})
        assert response.status_code == 200 and response.json()["enabled"] is True
        assert host.runtime.bus.revision > revision
        assert "fixture-secret" not in response.text and len(response.json()["revision"]) == 32
        assert (await client.get(POLICY)).json() == response.json()
        assert host.runtime.config.path.read_bytes() == before
        assert host.runtime.config.settings.attention.events == ["reminder", "management", "update"]


@pytest.mark.parametrize(
    "body",
    [{}, {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "scope": "remote"}],
    ids=["missing", "string", "integer", "extra"],
)
async def test_policy_api_rejects_non_host_fields_and_non_boolean(
    integration_host: Host, body: dict[str, Any]
) -> None:
    host = integration_host
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.put(POLICY, json=body)
    assert response.status_code == 422
    assert host.runtime.store.get(POLICY_KIND, POLICY_ID) is None


async def test_bad_policy_is_readable_and_cannot_be_replaced(integration_host: Host) -> None:
    host = integration_host
    value = "not-json-secret"
    host.runtime.store.db.execute(
        "INSERT INTO objects VALUES(?,?,?,0)", (POLICY_KIND, POLICY_ID, value)
    )
    host.runtime.store.db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        status = await client.get(POLICY)
        assert status.status_code == 200 and not status.json()["enabled"]
        assert status.json()["code"] == "chiliarch_attention_policy_invalid"
        changed = await client.put(POLICY, json={"enabled": False})
        assert changed.status_code == 400 and "secret" not in changed.text
    assert (
        host.runtime.store.db.execute(
            "SELECT data FROM objects WHERE kind=? AND id=?", (POLICY_KIND, POLICY_ID)
        ).fetchone()[0]
        == value
    )


async def test_paused_host_allows_status_but_not_policy_mutation(integration_host: Host) -> None:
    host = integration_host
    assert passive_request_allowed("GET", POLICY, None)
    assert not passive_request_allowed("PUT", POLICY, None)
    host.runtime.maintenance.admission.pause()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get(POLICY)).status_code == 200
            assert (await client.put(POLICY, json={"enabled": True})).status_code == 409
        assert host.runtime.store.get(POLICY_KIND, POLICY_ID) is None
    finally:
        host.runtime.maintenance.admission.release()


async def test_tool_uses_owner_context_configuration_lock_and_not_primary_config(
    integration_host: Host,
) -> None:
    host, runtime = integration_host, integration_host.runtime
    tool = runtime.tools["chiliarch_attention_configure"]
    status = runtime.tools["chiliarch_attention_status"]
    assert tool.risk == "write" and status.risk == "read"
    assert set(tool.parameters["properties"]) == {"enabled"}
    assert tool.parameters["additionalProperties"] is False
    before = runtime.config.path.read_bytes()
    other = runtime.new_session()["id"]
    token = runtime._tool_session.set(other)
    try:
        with pytest.raises(PermissionError, match="主人主会话"):
            await tool.handler({"enabled": True})
    finally:
        runtime._tool_session.reset(token)
    assert runtime.store.get(POLICY_KIND, POLICY_ID) is None
    token = runtime._tool_session.set(runtime.main_session()["id"])
    try:
        result = await tool.handler({"enabled": True})
    finally:
        runtime._tool_session.reset(token)
    assert result["enabled"] and (await status.handler({})) == result
    assert runtime.config.path.read_bytes() == before
    with pytest.raises(MaintenanceError, match="configuration_lock_required"):
        host.app.state.integration.replace_attention(False)
    assert "fixture-secret" not in json.dumps(result)


async def test_api_waits_for_configuration_lock_then_rechecks_closing(
    integration_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, runtime = integration_host, integration_host.runtime
    lock = ObservedLock(runtime.configuration_lock)
    runtime.configuration_lock = lock  # type: ignore[assignment]
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = host.app.state.integration.close

    async def close_integration() -> None:
        entered.set()
        await release.wait()
        await original_close()

    monkeypatch.setattr(host.app.state.integration, "close", close_integration)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        await lock.lock.acquire()
        request = asyncio.create_task(client.put(POLICY, json={"enabled": True}))
        await asyncio.wait_for(lock.entered.wait(), 2)
        lock.entered.clear()
        closing = asyncio.create_task(host.app.state.close())
        await asyncio.wait_for(lock.entered.wait(), 2)
        lock.lock.release()
        try:
            response = await asyncio.wait_for(request, 2)
            assert response.status_code == 503
            assert response.json() == {
                "detail": "transaction_unavailable",
                "reason": "service_closing",
            }
            await asyncio.wait_for(entered.wait(), 2)
            assert runtime.store.get(POLICY_KIND, POLICY_ID) is None
        finally:
            release.set()
            await closing
