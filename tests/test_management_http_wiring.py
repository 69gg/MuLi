from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from test_maintenance import NOW, move
from test_maintenance_api import prepare

from muli.config import ConfigFile
from muli.maintenance import BootPermit
from muli.management_transaction import TransactionJournal, Transition
from muli.platforms.linux_bootstrap import BootSession
from muli.runtime import Runtime
from muli.server import create_app
from muli.service import UserService


@dataclass(slots=True)
class Host:
    app: FastAPI
    runtime: Runtime
    raw_actions: list[str] = field(default_factory=list)


@pytest.fixture
async def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Host]:
    config = ConfigFile(tmp_path / "settings.json")
    config.update(
        {
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "onboarding_pending": False,
        }
    )
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime.data_dir, runtime)
    state = Host(app, runtime)

    async def raw(self: UserService, action: str) -> dict[str, Any]:
        state.raw_actions.append(action)
        return {"accepted": True}

    monkeypatch.setattr(UserService, "action", raw)
    try:
        yield state
    finally:
        await app.state.close()


@pytest.mark.parametrize(
    "body",
    [{}, {"action": "configure", "changes": {"persona": {"name": "候选配置"}}}],
)
async def test_unsupported_transaction_never_falls_back_to_raw_restart(
    host: Host, body: dict[str, Any]
) -> None:
    config_before = host.runtime.config.path.read_bytes()
    position = host.runtime.store.event_position()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post("/api/management/transactions", json=body)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "transaction_unsupported",
        "reason": "managed_boot_required",
    }
    assert not host.raw_actions and not host.app.state.management.plans()
    assert list(host.app.state.management.directory.iterdir()) == []
    assert host.runtime.config.path.read_bytes() == config_before
    assert host.runtime.store.event_position() == position


@pytest.mark.parametrize(
    "body",
    [
        {"action": "update"},
        {"action": "restart", "changes": {"persona": {"name": "不应应用"}}},
        {"apply_mode": "in_process"},
        {"action": "configure", "changes": {}, "extra": "rejected"},
        {"action": "configure", "changes": []},
        {"action": "configure", "changes": "not-an-object"},
    ],
)
async def test_transaction_endpoint_rejects_invalid_modes_fields_and_changes(
    host: Host, body: dict[str, Any]
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post("/api/management/transactions", json=body)
    assert response.status_code == 422
    assert not host.raw_actions and not host.app.state.management.plans()
    assert list(host.app.state.management.directory.iterdir()) == []


async def test_configure_endpoint_stages_restart_candidate_without_applying_or_raw_service(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    verified: list[str] = []

    async def verify() -> None:
        verified.append("verified")

    monkeypatch.setattr(host.app.state.management.recovery, "verify", verify)
    before = host.runtime.config.path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/management/transactions",
            json={"action": "configure", "changes": {"persona": {"name": "下一代"}}},
        )
    assert response.status_code == 200
    item = response.json()
    assert item["action"] == "configure" and item["apply_mode"] == "restart_transaction"
    assert item["status"] == "pending" and item["changed_sections"] == ["persona"]
    assert verified == ["verified"] and not host.raw_actions
    assert host.runtime.config.path.read_bytes() == before
    assert host.runtime.store.get("management", item["id"]) == item
    path = host.app.state.management.directory / f"{item['id']}.json"
    assert json.loads(path.read_text())["persona"]["name"] == "下一代"


class ObservedLock:
    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock, self.entered = lock, asyncio.Event()

    async def __aenter__(self) -> None:
        self.entered.set()
        await self.lock.acquire()

    async def __aexit__(self, *args: object) -> None:
        self.lock.release()

    def locked(self) -> bool:
        return self.lock.locked()


async def test_raw_http_service_waits_for_configuration_and_rechecks_maintenance(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def guarded(service: UserService, action: str) -> dict[str, Any]:
        calls.append(action)
        return {"accepted": True}

    monkeypatch.setattr("muli.management_cli.guarded_service_action", guarded)
    lock = ObservedLock(host.runtime.configuration_lock)
    host.runtime.configuration_lock = lock  # type: ignore[assignment]
    await lock.lock.acquire()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        pending = asyncio.create_task(client.post("/api/service", json={"action": "stop"}))
        try:
            await asyncio.wait_for(lock.entered.wait(), timeout=1.0)
            assert not calls and not pending.done()
            host.runtime.maintenance.admission.pause()
            lock.lock.release()
            response = await asyncio.wait_for(pending, timeout=1.0)
            assert response.status_code == 409
            assert response.json()["detail"] == "transaction_in_progress"
            assert not calls
        finally:
            if lock.lock.locked():
                lock.lock.release()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux passive boot transaction")
async def test_passive_boot_exposes_read_only_transaction_status_and_blocks_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "settings.json")
    config.update({"updates": {"check_on_start": False}, "onboarding_pending": False})
    data = tmp_path / "data"
    journal = TransactionJournal(data / "management/transaction-v1", data / "muli.sqlite3")
    permit = BootPermit.acquire(
        journal, config.path, boot="origin", installation="a" * 64, service="b" * 64
    )
    origin = Runtime(config, data, boot_permit=permit)
    prepare(journal, origin)
    await origin.close()
    record = move(
        journal,
        Transition(phase="draining"),
        Transition(phase="activating", intent="stop_base"),
        Transition(phase="activating", intent="install_target"),
        Transition(phase="activating", intent="start_target"),
        Transition(phase="verifying"),
    )
    permit = BootPermit.acquire(
        journal, config.path, boot="target", installation="a" * 64, service="b" * 64
    )
    session = BootSession(journal, permit.lease, config.path.absolute(), permit)
    runtime = Runtime(config, data, boot_session=session)
    app = create_app(config, data, runtime, maintenance_clock=lambda: NOW)
    calls: list[str] = []

    async def raw(self: UserService, action: str) -> dict[str, Any]:
        calls.append(action)
        return {"accepted": True}

    monkeypatch.setattr(UserService, "action", raw)
    try:
        async with app.router.lifespan_context(app):
            assert not runtime.started and not runtime.maintenance.admission.allowed
            before = runtime.store.event_position()
            config_before = config.path.read_bytes()
            journal_before = (journal.directory.path / "state.json").read_bytes()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/api/management/transactions/current")
                assert response.status_code == 200
                assert response.json()["transaction"] == record.public()
                assert response.json()["available"] is False
                management = await client.get("/api/management")
                assert management.status_code == 200
                assert management.json()["recovery"]["transaction"] == record.public()
                assert (
                    await client.post("/api/management/transactions", json={})
                ).status_code == 409
                assert (
                    await client.post("/api/service", json={"action": "restart"})
                ).status_code == 409
            assert not calls and not app.state.management.plans()
            assert runtime.store.event_position() == before
            assert config.path.read_bytes() == config_before
            assert (journal.directory.path / "state.json").read_bytes() == journal_before
    finally:
        await app.state.close()
