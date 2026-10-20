from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from test_managed_boot import Host
from test_managed_boot import host as host

from muli import managed_runtime_binding
from muli.config import ConfigFile
from muli.maintenance import MaintenanceError
from muli.maintenance_api import install_maintenance_routes
from muli.managed_boot import ManagedBootError
from muli.managed_journal import advance_managed
from muli.managed_runtime_binding import RuntimeBindingError
from muli.managed_transaction import HealthProofV2, TransitionV2, format_digest
from muli.management_host import RecoveryHost
from muli.management_projection import read_projection
from muli.management_transaction import ClockReading, ProjectionReceipt, TransactionError
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.server import create_app
from muli.service import UserService
from muli.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Managed Linux runtime admission")
_CLOCK = ClockReading(boot_id="kernel", wall=2.0, monotonic=2.0)


def runtime_for(host: Host) -> Runtime:
    host.monkeypatch.setattr(managed_runtime_binding, "verify_local_filesystems", lambda paths: ())
    permit = host.acquire()
    return Runtime(permit.config, host.journal.database.parent, boot_permit=permit)


def seal(runtime: Runtime) -> None:
    runtime.maintenance.seal({"runtime"})


async def test_runtime_opens_existing_before_recovery_and_keeps_main_identity(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = Runtime._recover

    def recover(runtime: Runtime) -> None:
        assert runtime.store.migration_allowed is False
        assert runtime.store.format_state == host.evidence.state
        assert runtime.store.instance_id == host.evidence.store_instance
        calls.append("recover")
        original(runtime)

    def forbidden(*args: Any, **kwargs: Any) -> Store:
        raise AssertionError("managed startup cannot create or migrate Store")

    monkeypatch.setattr(Runtime, "_recover", recover)
    monkeypatch.setattr(Store, "open_runtime", forbidden)
    runtime = runtime_for(host)
    try:
        assert calls == ["recover"]
        assert runtime.main_session()["id"] == host.evidence.main_session
        assert runtime.maintenance.permit is not None
        assert runtime.config is runtime.maintenance.permit.config
        assert not runtime.maintenance.admission.allowed
        assert not runtime.maintenance.accepts_external_input
        assert not runtime.started and not runtime.tasks
    finally:
        await runtime.close()


def test_runtime_rejects_unverified_configuration_object_before_store(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit = host.acquire()

    def forbidden(*args: Any, **kwargs: Any) -> Store:
        raise AssertionError("no Store before configuration owner verification")

    monkeypatch.setattr(Store, "open_existing", forbidden)
    other = ConfigFile.from_bytes(permit.config.path, permit.config.path.read_bytes())
    with pytest.raises(ValueError, match="managed_configuration_owner_required"):
        Runtime(other, host.journal.database.parent, boot_permit=permit)


def test_format_removed_between_acquire_and_runtime_never_reaches_recovery(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit = host.acquire()
    host.mutate("DELETE FROM runtime_state WHERE name='format_state_v1'")

    def forbidden(runtime: Runtime) -> None:
        raise AssertionError("no business recovery before fresh format admission")

    monkeypatch.setattr(Runtime, "_recover", forbidden)
    with pytest.raises(ManagedBootError):
        Runtime(permit.config, host.journal.database.parent, boot_permit=permit)


@pytest.mark.parametrize("role", ["target", "base"])
async def test_readiness_requires_fresh_evidence_and_releases_only_after_decision(
    host: Host, role: str
) -> None:
    if role == "base":
        host.record("rolling_back", "verify_base")
    runtime = runtime_for(host)
    maintenance = runtime.maintenance
    seal(runtime)
    calls: list[str] = []

    async def activate() -> None:
        assert not maintenance.admission.allowed
        calls.append("activate")

    maintenance.bind_activation(activate)
    try:
        record = maintenance.current()
        assert record is not None
        permit = maintenance.permit
        assert permit is not None
        nonce = record.challenge if role == "target" else record.rollback_challenge
        assert nonce is not None
        proof = maintenance.readiness(record.generation, permit.boot, nonce)
        assert isinstance(proof, HealthProofV2)
        assert proof.role == role and proof.admission == "paused"
        assert proof.format_digest == format_digest(host.evidence.state)
        with pytest.raises(MaintenanceError, match="activation_not_permitted"):
            await maintenance.activate(record.generation, permit.boot, nonce, lambda: _CLOCK)
        with host.journal.locked() as control:
            if role == "target":
                record = advance_managed(control, record.position, TransitionV2(phase="verifying"))
            record = advance_managed(
                control,
                record.position,
                TransitionV2(phase="committed" if role == "target" else "rolled_back", proof=proof),
            )
        assert not maintenance.accepts_external_input
        released = await maintenance.activate(record.generation, permit.boot, nonce, lambda: _CLOCK)
        assert isinstance(released, HealthProofV2)
        assert released.admission == "released" and calls == ["activate"]
        assert maintenance.accepts_external_input
        await maintenance.activate(record.generation, permit.boot, nonce, lambda: _CLOCK)
        assert calls == ["activate"]
    finally:
        await runtime.close()


async def test_wrong_nonce_precedes_evidence_and_changed_files_cannot_prove_health(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_for(host)
    seal(runtime)
    maintenance = runtime.maintenance
    permit = maintenance.permit
    record = maintenance.current()
    assert permit is not None and record is not None
    verify = maintenance.verify_managed
    calls: list[str] = []

    def check(*args: Any) -> None:
        calls.append("verify")
        verify(*args)

    monkeypatch.setattr(maintenance, "verify_managed", check)
    try:
        with pytest.raises(MaintenanceError, match="transaction_unauthorized"):
            maintenance.readiness(record.generation, permit.boot, "wrong")
        assert not calls
        permit.config_path.write_bytes(b'{"server":{"port":9999}}')
        with pytest.raises(ValueError):
            maintenance.readiness(record.generation, permit.boot, record.challenge)
        assert calls == ["verify"] and not maintenance.admission.allowed
    finally:
        await runtime.close()


@pytest.mark.parametrize("cause", ["fsync", "component"])
async def test_failed_activation_never_opens_gate(
    host: Host, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    host.record("committed", "none")
    runtime = runtime_for(host)
    maintenance = runtime.maintenance
    seal(runtime)
    called: list[str] = []

    async def activate() -> None:
        called.append("activate")
        raise RuntimeError("fake component cannot start")

    maintenance.bind_activation(activate)
    record = maintenance.current()
    permit = maintenance.permit
    assert record is not None and permit is not None
    sync = os.fsync

    def failed(fd: int) -> None:
        if fd == host.journal.directory.fd:
            raise OSError("fake durability failure")
        sync(fd)

    try:
        if cause == "fsync":
            monkeypatch.setattr(os, "fsync", failed)
        with pytest.raises(RuntimeBindingError if cause == "fsync" else RuntimeError):
            await maintenance.activate(
                record.generation, permit.boot, record.challenge, lambda: _CLOCK
            )
        assert called == ([] if cause == "fsync" else ["activate"])
        assert not maintenance.admission.allowed and not maintenance.accepts_external_input
    finally:
        monkeypatch.setattr(os, "fsync", sync)
        await runtime.close()


async def test_cancelled_http_wait_keeps_one_owned_activation(host: Host) -> None:
    host.record("committed", "none")
    runtime = runtime_for(host)
    maintenance = runtime.maintenance
    seal(runtime)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def activate() -> None:
        calls.append("activate")
        entered.set()
        await finish.wait()

    maintenance.bind_activation(activate)
    record = maintenance.current()
    permit = maintenance.permit
    assert record is not None and permit is not None
    task = asyncio.create_task(
        maintenance.activate(record.generation, permit.boot, record.challenge, lambda: _CLOCK)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not maintenance.admission.allowed
        finish.set()
        await maintenance.activate(record.generation, permit.boot, record.challenge, lambda: _CLOCK)
        assert calls == ["activate"] and maintenance.admission.allowed
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


def test_failed_runtime_initialization_closes_store_before_releasing_writer(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Store] = []

    def fail(runtime: Runtime) -> None:
        captured.append(runtime.store)
        with pytest.raises(TransactionError, match="lock_busy"):
            host.journal.writer_lease().__enter__()
        raise ValueError("fake constructor error")

    monkeypatch.setattr(Runtime, "_initialize", fail)
    with pytest.raises(ValueError, match="fake constructor error"):
        runtime_for(host)
    assert captured
    with host.journal.writer_lease():
        pass


def recovery_for(runtime: Runtime) -> RecoveryHost:
    return RecoveryHost(runtime, UserService(runtime.config.path, runtime.data_dir))


def save_plan(runtime: Runtime) -> None:
    record = runtime.maintenance.current()
    assert record is not None
    runtime.store.put(
        "management",
        {
            "id": record.request_id,
            "action": record.action,
            "status": "applying",
            "transaction": {"generation": record.generation},
            "routing": FrozenRouting.bound(ReplyOrigin("web", "input-managed")).dump(),
        },
    )


async def test_configuration_opens_only_after_exact_actual_store_projection(host: Host) -> None:
    record = host.record("committed", "none", activation="released")
    runtime = runtime_for(host)
    seal(runtime)
    recovery = recovery_for(runtime)
    try:
        assert runtime.maintenance.admission.allowed
        assert runtime.maintenance.configuration_blocked
        save_plan(runtime)
        with pytest.raises(MaintenanceError, match="transaction_in_progress"):
            runtime.maintenance.require_configuration()
        await recovery.project(strict=True)
        assert read_projection(runtime.store) == ProjectionReceipt(
            **record.position.model_dump(), store_instance=runtime.store.instance_id
        )
        runtime.maintenance.require_configuration()
        runtime.config.update({"persona": {"append": "热配置仍可使用"}})
        # Historical health cannot claim the old frozen digest after hot reload.
        assert runtime.maintenance.permit is not None
        with pytest.raises(RuntimeBindingError):
            runtime.maintenance.readiness(
                record.generation, runtime.maintenance.permit.boot, record.challenge
            )
        await recovery.project(strict=True)  # The exact receipt remains idempotent.
        assert not runtime.maintenance.configuration_blocked
    finally:
        await runtime.close()


async def test_final_readiness_projects_only_after_nonce_authentication(host: Host) -> None:
    record = host.record("committed", "none", activation="released")
    runtime = runtime_for(host)
    seal(runtime)
    save_plan(runtime)
    recovery = recovery_for(runtime)
    app = FastAPI()
    calls: list[str] = []

    async def project() -> None:
        calls.append("project")
        await recovery.project(strict=True)

    install_maintenance_routes(app, runtime, lambda: _CLOCK, on_resolved=project)
    permit = runtime.maintenance.permit
    assert permit is not None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            parameters = {"generation": record.generation, "boot": permit.boot}
            with pytest.raises(MaintenanceError, match="transaction_unauthorized"):
                await client.get(
                    "/api/internal/management/readiness",
                    params=parameters,
                    headers={"X-MuLi-Nonce": "wrong"},
                )
            assert not calls and read_projection(runtime.store) is None
            response = await client.get(
                "/api/internal/management/readiness",
                params=parameters,
                headers={"X-MuLi-Nonce": record.challenge},
            )
        assert response.status_code == 200 and calls == ["project"]
        assert not runtime.maintenance.configuration_blocked
        result = runtime.store.get("management", record.request_id)
        assert result is not None and result["status"] == "completed"
        assert result["routing"] == FrozenRouting.bound(ReplyOrigin("web", "input-managed")).dump()
    finally:
        await runtime.close()


async def test_authenticated_api_recovers_lost_final_hint_without_static_or_login_effects(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.record("committed", "none", activation="released")
    runtime = runtime_for(host)
    save_plan(runtime)
    app = create_app(runtime.config, runtime.data_dir, runtime, maintenance_clock=lambda: _CLOCK)
    authentication = app.state.authentication
    monkeypatch.setattr(authentication, "authenticated", lambda token: False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get("/api/settings")).status_code == 401
            assert (await client.get("/api/auth/status")).status_code == 200
            await client.get("/missing-static-asset")
            assert read_projection(runtime.store) is None
            assert runtime.maintenance.configuration_blocked
            monkeypatch.setattr(authentication, "authenticated", lambda token: True)
            assert (await client.get("/api/settings")).status_code == 200
            assert read_projection(runtime.store) is not None
            assert not runtime.maintenance.configuration_blocked
    finally:
        await app.state.close()
