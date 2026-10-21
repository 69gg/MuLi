from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import httpx
import pytest
from test_management_http_wiring import Host, ObservedLock
from test_management_http_wiring import host as host

from muli.maintenance import MaintenanceError
from muli.maintenance_api import passive_request_allowed, request_has_effect

RECOVER = "/api/management/transactions/recover"


async def test_recover_requires_owner_and_existing_http_boundaries(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.runtime.config.update({"server": {"password": "recovery-fixture-password"}})
    calls: list[str] = []
    result = {"available": False, "transaction": None, "reason": "managed_boot_required"}

    async def recover() -> dict[str, Any]:
        calls.append("recover")
        return result

    monkeypatch.setattr(host.app.state.management.recovery, "recover", recover)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(RECOVER, json={}, headers={"X-MuLi-Nonce": "not-owner-auth"})
        assert response.status_code == 401 and not calls
        assert (
            await client.post("/api/auth/login", json={"password": "recovery-fixture-password"})
        ).status_code == 200
        for headers in (
            {"Origin": "http://untrusted.invalid"},
            {"Host": "untrusted.invalid"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert (await client.post(RECOVER, json={}, headers=headers)).status_code == 403
        assert (
            await client.post(RECOVER, content="{}", headers={"Content-Type": "text/plain"})
        ).status_code == 415
        assert (
            await client.post(RECOVER, json={"generation": "caller-cannot-pick-a-generation"})
        ).status_code == 422
        assert not calls
        response = await client.post(RECOVER, json={})
        assert response.status_code == 200 and response.json() == result
    assert calls == ["recover"] and not host.raw_actions


async def test_paused_recovery_delegates_without_opening_other_business_writes(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, manager = host.runtime, host.app.state.management
    runtime.maintenance.admission.pause()
    calls: list[str] = []
    result = {"available": True, "transaction": {"generation": "current"}, "helper": "accepted"}

    async def recover() -> dict[str, Any]:
        assert not runtime.maintenance.admission.allowed
        calls.append("recover")
        return result

    monkeypatch.setattr(manager.recovery, "recover", recover)
    before = runtime.config.path.read_bytes()
    position = runtime.store.event_position()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(RECOVER, json={})
        assert response.status_code == 200 and response.json() == result
        for path in (
            "/api/service",
            "/api/extensions/reload",
            "/api/management/transactions",
            RECOVER + "/extra",
        ):
            assert (await client.post(path, json={})).status_code == 409
        assert (await client.patch(RECOVER, json={})).status_code == 409
    assert calls == ["recover"] and not runtime.maintenance.admission.allowed
    assert runtime.config.path.read_bytes() == before and not manager.plans()
    assert runtime.store.event_position() == position and not host.raw_actions


async def test_recovery_tool_reuses_current_host_recovery_without_staging(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, manager = host.runtime, host.app.state.management
    calls: list[str] = []
    result = {"transaction": {"generation": "unchanged"}, "helper": "controller_busy"}

    async def recover() -> dict[str, Any]:
        calls.append("recover")
        return result

    async def stage(*args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("recovery must not stage a new generation")

    monkeypatch.setattr(manager.recovery, "recover", recover)
    monkeypatch.setattr(manager, "stage", stage)
    runtime.maintenance.admission.pause()
    position = runtime.store.event_position()
    tool = runtime.tools["self_recover"]
    assert tool.risk == "write"
    assert tool.parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    assert await tool.handler({}) is result
    assert calls == ["recover"] and not manager.plans()
    assert runtime.store.event_position() == position and not host.raw_actions


async def test_recovery_entry_keeps_existing_failure_semantics(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def recover() -> dict[str, Any]:
        raise MaintenanceError("transaction_unavailable")

    monkeypatch.setattr(host.app.state.management.recovery, "recover", recover)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(RECOVER, json={})
    assert response.status_code == 503 and response.json() == {"detail": "transaction_unavailable"}
    with pytest.raises(MaintenanceError, match="transaction_unavailable"):
        await host.runtime.tools["self_recover"].handler({})


async def test_application_close_waits_for_recovery_and_keeps_store_readable(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = host.runtime
    lock = ObservedLock(runtime.configuration_lock)
    runtime.configuration_lock = lock  # type: ignore[assignment]
    entered, release = asyncio.Event(), asyncio.Event()
    main = runtime.store.existing_main_session_id()
    result = {"helper": "accepted"}

    async def recover() -> dict[str, Any]:
        entered.set()
        await release.wait()
        assert runtime.store.existing_main_session_id() == main
        return result

    monkeypatch.setattr(host.app.state.management.recovery, "recover", recover)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        pending = asyncio.create_task(client.post(RECOVER, json={}))
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            lock.entered.clear()
            closing = asyncio.create_task(host.app.state.close())
            await asyncio.wait_for(lock.entered.wait(), timeout=2)
            assert lock.locked() and not pending.done() and not closing.done()
            assert runtime.store.existing_main_session_id() == main
            release.set()
            response = await asyncio.wait_for(asyncio.shield(pending), timeout=2)
            assert response.status_code == 200 and response.json() == result
            await asyncio.wait_for(asyncio.shield(closing), timeout=2)
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                runtime.store.db.execute("SELECT 1")
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)


async def test_recovery_queued_after_application_close_starts_is_rejected(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = host.runtime
    lock = ObservedLock(runtime.configuration_lock)
    runtime.configuration_lock = lock  # type: ignore[assignment]
    calls: list[str] = []

    async def recover() -> dict[str, Any]:
        calls.append("recover")
        raise AssertionError("closing application must not begin recovery")

    monkeypatch.setattr(host.app.state.management.recovery, "recover", recover)
    runtime.maintenance.admission.pause()
    await lock.lock.acquire()
    closing = asyncio.create_task(host.app.state.close())
    pending: asyncio.Task[httpx.Response] | None = None
    try:
        await asyncio.wait_for(lock.entered.wait(), timeout=2)
        assert not closing.done()
        lock.entered.clear()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
        ) as client:
            pending = asyncio.create_task(client.post(RECOVER, json={}))
            await asyncio.wait_for(lock.entered.wait(), timeout=2)
            assert not calls and not pending.done()
            lock.lock.release()
            response = await asyncio.wait_for(asyncio.shield(pending), timeout=2)
            assert response.status_code == 503
            assert response.json() == {
                "detail": "transaction_unavailable",
                "reason": "service_closing",
            }
        await asyncio.wait_for(asyncio.shield(closing), timeout=2)
        assert not calls and not host.raw_actions
    finally:
        if lock.lock.locked():
            lock.lock.release()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "PUT", "DELETE"])
@pytest.mark.parametrize("path", [RECOVER, RECOVER + "/", RECOVER + "/extra"])
def test_passive_recovery_route_is_exact(method: str, path: str) -> None:
    allowed = method == "POST" and path == RECOVER
    assert passive_request_allowed(method, path, None) is allowed
    assert request_has_effect(method, path, None) is not allowed
