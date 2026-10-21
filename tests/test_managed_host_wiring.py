from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from test_managed_boot import _slot
from test_managed_preparation import Host
from test_managed_preparation import host as host

from muli import managed_host, managed_preparation
from muli.config import Settings
from muli.managed_journal import advance_managed
from muli.managed_requests import LocalCandidate
from muli.managed_transaction import TransactionRecordV2, TransitionV2
from muli.management import SelfManagement
from muli.management_transaction import JournalControl
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore
from muli.plugins import PluginManager


def manager_for(host: Host, monkeypatch: pytest.MonkeyPatch) -> SelfManagement:
    plugins = PluginManager(host.runtime, FastAPI(), host.root / "plugins")

    async def forbidden(target: Settings) -> None:
        raise AssertionError("managed transaction cannot fall back to hot configuration")

    manager = SelfManagement(host.runtime, plugins, host.service, forbidden)
    monkeypatch.setattr(
        managed_preparation, "ManagedPreparation", lambda runtime, service: host.prepare
    )
    return manager


def specification(host: Host) -> LocalCandidate:
    return LocalCandidate(
        prefix=host.slot.prefix,
        python=host.slot.lexical_python,
        package_root=host.slot.package.root,
    )


async def apply(manager: SelfManagement, item: dict[str, Any]) -> None:
    async with manager.runtime.configuration_lock:
        await manager._apply(item)


@pytest.mark.parametrize("failure", [False, True])
async def test_public_adoption_keeps_prepared_generation_after_helper_observer_failure(
    host: Host, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    manager = manager_for(host, monkeypatch)
    seen: list[str] = []

    async def ensure(control: JournalControl, anchor: DeploymentAnchor) -> str:
        record = control.read_current()
        assert isinstance(record, TransactionRecordV2)
        stored = host.runtime.store.get("management", record.request_id)
        assert stored is not None and stored["transaction"] == record.public()
        seen.append(record.generation)
        if failure:
            raise TimeoutError("observer failed after prepared")
        return "accepted"

    monkeypatch.setattr(managed_host, "ensure_managed_helper", ensure)
    try:
        item = await manager.stage("adopt", candidate=specification(host))
        await apply(manager, item)
        record = host.journal.read_current()
        assert isinstance(record, TransactionRecordV2) and record.phase == "prepared"
        stored = host.runtime.store.get("management", item["id"])
        assert stored is not None and stored["status"] == "applying"
        assert stored["transaction"]["generation"] == record.generation
        assert host.runtime.maintenance.configuration_blocked
        await manager.recovery.recover()
        assert seen == [record.generation, record.generation]
        assert host.journal.read_current() == record
        assert not [
            event
            for event in host.runtime.store.events()
            if event["type"] == "management.completed"
        ]
    finally:
        await manager.close()


@pytest.mark.parametrize("action", ["restart", "configure", "switch"])
async def test_public_managed_successor_uses_same_journal_and_actual_preparation(
    host: Host, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    first = await host.run(host.plan("adopt", name="old-adoption"), candidate=host.slot)
    host.complete(first)
    manager = manager_for(host, monkeypatch)
    seen: list[str] = []

    async def ensure(control: JournalControl, anchor: DeploymentAnchor) -> str:
        record = control.read_current()
        assert isinstance(record, TransactionRecordV2)
        assert record.action == action and record.identity.deployment == first.anchor.digest
        seen.append(record.generation)
        return "accepted"

    monkeypatch.setattr(managed_host, "ensure_managed_helper", ensure)
    target = _slot(host.root, "next-package") if action == "switch" else None
    candidate = (
        LocalCandidate(
            prefix=target.prefix, python=target.lexical_python, package_root=target.package.root
        )
        if target
        else None
    )
    try:
        item = await manager.stage(
            action,
            {"persona": {"append": "下一代配置"}} if action == "configure" else None,
            apply_mode="restart_transaction",
            candidate=candidate,
        )
        await apply(manager, item)
        record = host.journal.read_current()
        assert isinstance(record, TransactionRecordV2) and record.action == action
        assert seen == [record.generation] and record.generation != first.record.generation
        assert record.target.installation == (target or host.slot).digest
        assert host.runtime.store.get("management", item["id"])["status"] == "applying"
    finally:
        await manager.close()


async def test_public_aborted_adoption_can_return_to_legacy_restart(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await host.run(host.plan("adopt", name="old-adoption"), candidate=host.slot)
    with host.journal.locked() as control:
        advance_managed(
            control, first.record.position, TransitionV2(phase="aborted", code="cancelled")
        )
    manager = manager_for(host, monkeypatch)
    seen: list[str] = []

    async def verify() -> Any:
        assert host.runtime.boot_session is not None
        return host.runtime.boot_session.manifest

    async def ensure(control: JournalControl, manifest: Any) -> str:
        record = control.read()
        assert record is not None and record.protocol == 1
        seen.append(record.generation)
        return "accepted"

    monkeypatch.setattr(manager.recovery, "verify", verify)
    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    try:
        await manager.recovery.project(strict=True)
        item = await manager.stage("restart")
        await apply(manager, item)
        record = host.journal.read_current()
        assert record is not None and record.protocol == 1 and seen == [record.generation]
        assert host.runtime.store.get("management", item["id"])["status"] == "applying"
        with DeploymentStore(host.journal.directory.path) as deployments:
            assert deployments.read_anchor(first.anchor.digest) == first.anchor
    finally:
        await manager.close()
