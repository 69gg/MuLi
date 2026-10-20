from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from test_attention import eventually
from test_maintenance import NOW, move
from test_maintenance_runtime import FakeClient, configured

from muli.maintenance import BootPermit, MaintenanceError
from muli.management_transaction import (
    Deadline,
    ServiceIdentity,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.runtime import Runtime
from muli.server import create_app


def prepare(journal: TransactionJournal, runtime: Runtime) -> TransactionRecord:
    permit = runtime.maintenance.permit
    assert permit is not None
    with journal.locked() as control:
        record = control.prepare(
            request_id="restart",
            action="restart",
            identity=ServiceIdentity(
                installation=permit.installation,
                service=permit.service,
                store_instance=runtime.store.instance_id,
                main_session=runtime.main_session()["id"],
                origin_boot=permit.boot,
            ),
            base=runtime.config.path.read_bytes(),
            target=runtime.config.path.read_bytes(),
            writer=permit.lease,
            deadline=Deadline(created=NOW, seconds=60.0, rollback_reserve=10.0),
        )
    runtime.maintenance.reconcile()
    return record


@asynccontextmanager
async def host_app(
    tmp_path: Path,
    *,
    passive: bool = False,
    password: bool = False,
    onboarding: bool = False,
) -> AsyncIterator[tuple[FastAPI, TransactionJournal, TransactionRecord]]:
    if sys.platform != "linux":
        pytest.skip("Linux transaction bootstrap")
    config = configured(tmp_path)
    config.update(
        {
            "memory": {"auto_extract": False},
            "server": {"password_hash": "configured" if password else ""},
            "onboarding_pending": onboarding,
        }
    )
    data = tmp_path / "data"
    with TransactionJournal(tmp_path / "transactions", data / "muli.sqlite3") as journal:
        permit = BootPermit.acquire(
            journal, config.path, boot="origin", installation="a" * 64, service="b" * 64
        )
        runtime = Runtime(config, data, boot_permit=permit)
        record = prepare(journal, runtime)
        if passive:
            await runtime.close()
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
            runtime = Runtime(config, data, boot_permit=permit)
        app = create_app(config, data, runtime, maintenance_clock=lambda: NOW)
        try:
            async with app.router.lifespan_context(app):
                yield app, journal, record
        finally:
            await runtime.close()


async def test_internal_nonce_does_not_bypass_host_origin_type_or_body_limit(
    tmp_path: Path,
) -> None:
    async with host_app(tmp_path, password=True) as (app, journal, record):
        record = move(journal, Transition(phase="draining"))
        url = "/api/internal/management/drain"
        body = {"generation": record.generation, "boot": "origin"}
        headers = {"X-MuLi-Nonce": record.challenge}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.post(url, json=body)).status_code == 401
            response = await client.post(url, json=body, headers=headers)
            assert response.status_code == 200  # Exact nonce route needs no Cookie.
            assert response.json()["state"] in {"waiting", "drained"}
            readiness_url = "/api/internal/management/readiness"
            response = await client.get(readiness_url, params=body, headers=headers)
            assert response.status_code == 200
            assert response.json()["boot"] == "origin"
            assert response.json()["admission"] == (
                "released" if app.state.runtime.maintenance.admission.allowed else "paused"
            )
            assert (await client.get(readiness_url, params=body)).status_code == 401
            move(journal, Transition(phase="prepared"))
            assert (
                await client.post("/api/internal/management/resume", json=body, headers=headers)
            ).status_code == 200
            response = await client.get(readiness_url, params=body, headers=headers)
            assert response.status_code == 200 and response.json()["admission"] == "released"
            assert (await client.get("/api/settings")).status_code == 401
            assert (
                await client.post(
                    url, json=body, headers={**headers, "Origin": "http://evil.invalid"}
                )
            ).status_code == 403
            assert (
                await client.post(url, json=body, headers={**headers, "Host": "evil.invalid"})
            ).status_code == 403
            assert (await client.post(url, content=b"{}", headers=headers)).status_code == 415
            assert (
                await client.post(
                    url,
                    content=b"{}",
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                        "Content-Length": "1048577",
                    },
                )
            ).status_code == 413
            assert (
                await client.post(url, json={**body, "path": "/arbitrary"}, headers=headers)
            ).status_code == 422


async def test_passive_lifespan_is_local_and_owner_ingress_remains_durable(tmp_path: Path) -> None:
    async with host_app(tmp_path, passive=True) as (app, _, record):
        runtime = app.state.runtime
        assert not runtime.started
        assert not app.state.channel_application.manager.receiving
        assert app.state.integration.bridge is None
        assert app.state.management._worker is None
        assert app.state.memory_application.pipeline._worker is None
        assert not app.state.attention.status()["running"]
        assert not app.state.personal_io.voice.status()["running"]
        headers = {"X-MuLi-Nonce": record.challenge}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            proof = await client.get(
                "/api/internal/management/readiness",
                params={"generation": record.generation, "boot": "target"},
                headers=headers,
            )
            assert proof.status_code == 200 and proof.json()["admission"] == "paused"
            for url in (
                "/api/capture",
                "/api/media",
                "/api/voice",
                "/api/service",
                "/api/unknown/new",
            ):
                assert (await client.get(url)).status_code == 409
            assert (
                await client.patch("/api/settings", json={"persona": {"append": "blocked"}})
            ).status_code == 409
            assert (await client.post("/api/channels/onebot/webhook", json={})).status_code == 409
            response = await client.post(
                f"/api/sessions/{runtime.main_session()['id']}/messages",
                json={"content": "重启期间保留"},
            )
            assert response.status_code == 200 and response.json()["queued"]
            assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 1
            assert not runtime.tasks


async def test_pending_transaction_blocks_configuration_before_components_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with host_app(tmp_path) as (app, _, _):
        runtime = app.state.runtime
        before = runtime.config.path.read_bytes()
        calls: list[str] = []
        close = app.state.memory_application.close

        async def forbidden() -> None:
            calls.append("closed")

        monkeypatch.setattr(app.state.memory_application, "close", forbidden)
        target = runtime.config.validate_changes({"persona": {"append": "blocked"}})
        async with runtime.configuration_lock:
            with pytest.raises(MaintenanceError, match="transaction_in_progress"):
                await app.state.management.apply_settings(target)
            with pytest.raises(MaintenanceError, match="transaction_in_progress"):
                await runtime.extensions.apply_locked(target, persist=True)
        with pytest.raises(MaintenanceError, match="transaction_in_progress"):
            await app.state.management.stage("restart")
        assert not calls and runtime.config.path.read_bytes() == before
        assert not list(app.state.management.directory.glob("*.json"))
        monkeypatch.setattr(app.state.memory_application, "close", close)


async def test_existing_effect_request_owns_drain_until_handler_settles(tmp_path: Path) -> None:
    async with host_app(tmp_path) as (app, journal, record):
        entered, finish = asyncio.Event(), asyncio.Event()

        @app.post("/api/fake-effect")
        async def effect() -> dict[str, bool]:
            entered.set()
            await finish.wait()
            return {"done": True}

        # Product static mount is already registered; put this fake route before it.
        app.router.routes.insert(0, app.router.routes.pop())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            operation = asyncio.create_task(client.post("/api/fake-effect", json={}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                record = move(journal, Transition(phase="draining"))
                url = "/api/internal/management/drain"
                kwargs: dict[str, Any] = {
                    "json": {"generation": record.generation, "boot": "origin"},
                    "headers": {"X-MuLi-Nonce": record.challenge},
                }
                response = await client.post(url, **kwargs)
                assert (
                    response.json()["state"] == "waiting"
                    and not response.json()["owners"]["effects"]
                )
                assert app.state.runtime.maintenance.admission.allowed
                finish.set()
                assert (await operation).status_code == 200
                response = await client.post(url, **kwargs)
                assert response.json()["state"] == "drained"
                assert not app.state.runtime.maintenance.admission.allowed
            finally:
                finish.set()
                await asyncio.gather(operation, return_exceptions=True)


async def test_cancelled_capture_observer_keeps_shielded_probe_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with host_app(tmp_path) as (app, journal, record):
        runtime, personal = app.state.runtime, app.state.personal_io
        runtime.config.settings.computer.enabled = True
        personal.capture.config.enabled = True
        personal.capture.config.backend = "portal"
        personal.capture.platform = "linux"
        entered, finish = asyncio.Event(), asyncio.Event()

        async def probe(request: dict[str, Any], timeout: float) -> dict[str, Any]:
            entered.set()
            await finish.wait()
            return {"available": True, "state": "ready"}

        monkeypatch.setattr(personal.capture, "_portal_call", probe)
        observer = asyncio.create_task(personal.capture_status())
        await entered.wait()
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        try:
            record = move(journal, Transition(phase="draining"))
            receipt = runtime.maintenance.drain(record.generation, "origin", record.challenge)
            assert receipt["state"] == "waiting" and not receipt["owners"]["personal_io"]
            finish.set()
            assert personal.capture._probe_task is not None
            await personal.capture._probe_task
            assert (
                runtime.maintenance.drain(record.generation, "origin", record.challenge)["state"]
                == "drained"
            )
        finally:
            finish.set()


@pytest.mark.parametrize("onboarding", [False, True])
async def test_real_passive_host_activates_after_commit_and_defers_configuration_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    onboarding: bool,
) -> None:
    async with host_app(tmp_path, passive=True, onboarding=onboarding) as (app, journal, record):
        runtime = app.state.runtime
        calls: list[str] = []
        mcp: list[str] = []
        entered, finish = asyncio.Event(), asyncio.Event()

        def client_factory(config: Any) -> FakeClient:
            return FakeClient(calls, entered, finish)

        async def start_mcp() -> None:
            mcp.append("start")

        monkeypatch.setattr(runtime, "client_factory", client_factory)
        monkeypatch.setattr(runtime, "_start_mcp", start_mcp)
        before = runtime.config.path.read_bytes()
        params = {"generation": record.generation, "boot": "target"}
        headers = {"X-MuLi-Nonce": record.challenge}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/internal/management/activate", json=params, headers=headers
            )
            assert response.status_code == 409 and not mcp
            proof = runtime.maintenance.readiness(record.generation, "target", record.challenge)
            move(journal, Transition(phase="committed", proof=proof))
            response = await client.post(
                "/api/internal/management/activate", json=params, headers=headers
            )
            assert response.status_code == 200 and response.json()["admission"] == "released"
            assert runtime.started and app.state.channel_application.manager.receiving
            assert mcp == ["start"] and not calls
            assert runtime.config.path.read_bytes() == before
            with pytest.raises(MaintenanceError, match="transaction_in_progress"):
                await app.state.management.stage("restart")
            released = runtime.maintenance.readiness(record.generation, "target", record.challenge)
            move(journal, Transition(phase="committed", activation="released", proof=released))
            # The normal final notification wakes both durable queues and local housekeeping.
            response = await client.get(
                "/api/internal/management/readiness", params=params, headers=headers
            )
            assert response.status_code == 200
            if onboarding:
                await eventually(
                    lambda: not runtime.config.settings.onboarding_pending and not runtime.tasks
                )
                assert calls == ["stream"]
            else:
                assert not calls
            runtime.maintenance.require_configuration()
            assert not runtime.maintenance.configuration_blocked


async def test_passive_legacy_plugin_is_rejected_before_import_or_health_proof(
    tmp_path: Path,
) -> None:
    async with host_app(tmp_path, passive=True) as (app, _, record):
        plugins = app.state.plugins
        package = plugins.directory / "legacy"
        package.mkdir(parents=True)
        (package / "plugin.json").write_text('{"id":"legacy","api_version":1}', encoding="utf-8")
        (package / "main.py").write_text(
            'raise AssertionError("must not import")', encoding="utf-8"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get(
                "/api/internal/management/readiness",
                params={"generation": record.generation, "boot": "target"},
                headers={"X-MuLi-Nonce": record.challenge},
            )
            assert response.status_code == 409
            assert response.json() == {
                "detail": "maintenance_unsupported",
                "reason": "legacy_plugins",
            }
            assert not plugins.contexts and not plugins.inventory()
            assert not app.state.runtime.started
