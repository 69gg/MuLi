from __future__ import annotations

import sys
from typing import Any, Literal

import httpx
import pytest
from test_managed_preparation import Host as PreparationHost
from test_managed_preparation import host as _preparation_host
from test_management_http_wiring import Host
from test_management_http_wiring import host as host

from muli.config import Settings
from muli.maintenance import MaintenanceError
from muli.managed_journal import read_snapshots
from muli.management_host import RecoveryHost
from muli.platforms.linux_controller import transaction_url

preparation_host = _preparation_host


def next_port(settings: Settings) -> int:
    port = settings.server.port
    return port + 1 if port < 65535 else port - 1


def listener_change(settings: Settings, field: str) -> dict[str, Any]:
    return {"server": {field: "127.0.0.2" if field == "host" else next_port(settings)}}


@pytest.mark.parametrize("field", ["host", "port"])
async def test_listener_patch_refuses_before_stopping_or_persisting(
    host: Host, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    before = host.runtime.config.path.read_bytes()
    position = host.runtime.store.event_position()
    effects: list[str] = []

    async def close() -> None:
        effects.append("close")
        raise AssertionError("listener PATCH must not stop components")

    def replace(target: Settings) -> Settings:
        effects.append("replace")
        raise AssertionError("listener PATCH must not write configuration")

    original = host.app.state.integration.close
    monkeypatch.setattr(host.app.state.integration, "close", close)
    monkeypatch.setattr(host.runtime.config, "replace", replace)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.patch(
                "/api/settings", json=listener_change(host.runtime.config.settings, field)
            )
        assert response.status_code == 409
        assert response.json() == {"detail": "listener_restart_required"}
        assert not effects and not host.app.state.management.plans()
        assert host.runtime.config.path.read_bytes() == before
        assert host.runtime.store.event_position() == position
    finally:
        monkeypatch.setattr(host.app.state.integration, "close", original)


@pytest.mark.parametrize("field", ["host", "port"])
async def test_listener_self_configuration_uses_transaction_without_hot_applying(
    host: Host, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    manager = host.app.state.management
    checks: list[str] = []

    async def verify() -> None:
        checks.append("verify")

    monkeypatch.setattr(manager.recovery, "verify", verify)
    before = host.runtime.config.path.read_bytes()
    change = listener_change(host.runtime.config.settings, field)
    item = await manager.stage("configure", change)
    assert item["apply_mode"] == "restart_transaction"
    assert item["status"] == "pending" and checks == ["verify"]
    assert host.runtime.config.path.read_bytes() == before and not host.raw_actions
    assert host.runtime.store.get("management", item["id"]) == item


async def test_password_configuration_still_applies_in_process(host: Host) -> None:
    before = host.runtime.config.settings.server.model_copy(deep=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.patch(
            "/api/settings", json={"server": {"password_hash": "new-hash"}}
        )
    assert response.status_code == 200
    assert host.runtime.config.settings.server.password_hash == "new-hash"
    assert (host.runtime.config.settings.server.host, host.runtime.config.settings.server.port) == (
        before.host,
        before.port,
    )
    assert not host.app.state.management.plans() and not host.raw_actions


@pytest.mark.parametrize("field", ["host", "port"])
async def test_stage_refuses_stored_listener_drift_before_creating_plan(
    host: Host, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    manager = host.app.state.management
    host.runtime.config.update(listener_change(host.runtime.config.settings, field))
    before = host.runtime.config.path.read_bytes()
    position = host.runtime.store.event_position()

    async def verify() -> None:
        raise AssertionError("listener drift must be rejected before service probing")

    monkeypatch.setattr(manager.recovery, "verify", verify)
    with pytest.raises(MaintenanceError, match="transaction_listener_changed"):
        await manager.stage("restart")
    assert host.runtime.config.path.read_bytes() == before
    assert host.runtime.store.event_position() == position and not manager.plans()
    assert not list(manager.directory.iterdir()) and not host.raw_actions


@pytest.mark.parametrize("current", [False, True])
async def test_non_loopback_control_endpoint_is_rejected_before_queue(
    host: Host, monkeypatch: pytest.MonkeyPatch, current: bool
) -> None:
    manager = host.app.state.management
    change = {"server": {"host": "192.0.2.1", "password_hash": "private-test-hash"}}
    if current:
        host.runtime.config.update(change)
        settings = host.runtime.config.settings
        manager.recovery = RecoveryHost(
            host.runtime,
            manager.service,
            bound_address=(settings.server.host, settings.server.port),
        )
    before = host.runtime.config.path.read_bytes()
    position = host.runtime.store.event_position()

    async def verify() -> None:
        raise AssertionError("unsupported HTTP control address must not probe a service")

    monkeypatch.setattr(manager.recovery, "verify", verify)
    with pytest.raises(MaintenanceError) as raised:
        if current:
            await manager.stage("restart")
        else:
            await manager.stage("configure", change)
    assert (raised.value.code, raised.value.reason) == (
        "transaction_unsupported",
        "transaction_http_not_loopback",
    )
    assert host.runtime.config.path.read_bytes() == before
    assert host.runtime.store.event_position() == position and not manager.plans()
    assert not list(manager.directory.iterdir()) and not host.raw_actions


async def test_stage_rechecks_listener_after_identity_probe_await(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = host.app.state.management

    async def verify() -> None:
        host.runtime.config.update(listener_change(host.runtime.config.settings, "port"))

    monkeypatch.setattr(manager.recovery, "verify", verify)
    position = host.runtime.store.event_position()
    with pytest.raises(MaintenanceError, match="transaction_listener_changed"):
        await manager.stage("restart")
    assert host.runtime.store.event_position() == position and not manager.plans()
    assert not list(manager.directory.iterdir()) and not host.raw_actions


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux preparation ownership fixture")
@pytest.mark.parametrize("action", ["restart", "adopt"])
async def test_prepare_refuses_listener_drift_before_any_preparing_write(
    preparation_host: PreparationHost, action: Literal["restart", "adopt"]
) -> None:
    host = preparation_host
    settings = host.runtime.config.settings
    recovery = RecoveryHost(
        host.runtime, host.service, bound_address=(settings.server.host, settings.server.port)
    )
    item = host.plan(action)
    if action == "adopt":
        item["candidate"] = {
            "prefix": host.slot.prefix,
            "python": host.slot.lexical_python,
            "package_root": host.slot.package.root,
        }
    host.runtime.config.update(listener_change(settings, "port"))
    position = host.runtime.store.event_position()
    before = host.runtime.config.path.read_bytes()
    async with host.runtime.configuration_lock:
        with pytest.raises(MaintenanceError, match="transaction_listener_changed"):
            await recovery.prepare(item, host.runtime.config.settings)
    assert host.journal.read_current() is None
    assert host.runtime.store.event_position() == position
    assert host.runtime.config.path.read_bytes() == before
    assert not host.probe.calls


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux preparation ownership fixture")
async def test_legacy_prepare_rechecks_bound_listener_after_probe_await(
    preparation_host: PreparationHost,
) -> None:
    host = preparation_host
    settings = host.runtime.config.settings
    recovery = RecoveryHost(
        host.runtime, host.service, bound_address=(settings.server.host, settings.server.port)
    )
    recovery.probe = host.probe
    host.monkeypatch.setattr("muli.management_host.verify_local_filesystems", lambda paths: ())
    host.probe.during = lambda: host.runtime.config.update(listener_change(settings, "port"))
    item = host.plan("restart")
    position = host.runtime.store.event_position()
    async with host.runtime.configuration_lock:
        with pytest.raises(MaintenanceError, match="transaction_listener_changed"):
            await recovery.prepare(item, settings)
    assert host.probe.calls == 1 and host.journal.read_current() is None
    assert host.runtime.store.event_position() == position


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux preparation ownership fixture")
async def test_managed_listener_configuration_freezes_old_and_new_control_addresses(
    preparation_host: PreparationHost,
) -> None:
    host = preparation_host
    adopted = await host.run(host.plan("adopt", name="adopt"), candidate=host.slot)
    host.complete(adopted)
    current = host.runtime.config.settings
    recovery = RecoveryHost(
        host.runtime, host.service, bound_address=(current.server.host, current.server.port)
    )
    target = host.runtime.config.validate_changes(listener_change(current, "port"))
    original = host.runtime.config.path.read_bytes()
    async with host.runtime.configuration_lock:
        prepared = await host.prepare.prepare(
            host.plan("configure"), target, listener_guard=recovery.require_listener
        )
    base = read_snapshots(host.journal, prepared.record, "base")
    candidate = read_snapshots(host.journal, prepared.record, "target")
    assert base.configuration == original
    assert Settings.model_validate_json(candidate.configuration) == target
    assert transaction_url(base.configuration, "drain") == transaction_url(original, "drain")
    assert transaction_url(candidate.configuration, "readiness") == transaction_url(
        target.model_dump_json().encode(), "readiness"
    )
    assert transaction_url(base.configuration, "readiness") != transaction_url(
        candidate.configuration, "readiness"
    )
    assert host.runtime.config.path.read_bytes() == original


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux preparation ownership fixture")
async def test_managed_prepare_rechecks_listener_after_final_probe_await(
    preparation_host: PreparationHost,
) -> None:
    host = preparation_host
    adopted = await host.run(host.plan("adopt", name="adopt"), candidate=host.slot)
    previous = host.complete(adopted)
    current = host.runtime.config.settings
    recovery = RecoveryHost(
        host.runtime, host.service, bound_address=(current.server.host, current.server.port)
    )
    item = host.plan("restart")
    position = host.runtime.store.event_position()
    host.probe.during = lambda: host.runtime.config.update(listener_change(current, "port"))
    async with host.runtime.configuration_lock:
        with pytest.raises(MaintenanceError, match="transaction_listener_changed"):
            await host.prepare.prepare(item, current, listener_guard=recovery.require_listener)
    assert host.journal.read_current() == previous
    assert host.runtime.store.event_position() == position
