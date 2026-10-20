from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from test_installation import Installation
from test_installation import installation as installation
from test_proactive_memory import changes, extracted

from muli.config import ConfigFile, Settings
from muli.maintenance import BootPermit
from muli.management import SelfManagement
from muli.management_projection import project_final
from muli.management_transaction import JournalControl, TransactionJournal, Transition
from muli.platforms.installation import InstallationManifest
from muli.platforms.linux_bootstrap import BootSession
from muli.plugins import PluginManager
from muli.runtime import Runtime

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux host recovery preparation")


@dataclass(slots=True)
class Host:
    runtime: Runtime
    manager: SelfManagement
    manifest: InstallationManifest
    journal: TransactionJournal


@pytest.fixture
async def host(installation: Installation, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Host]:
    service = installation.service
    service.config_path.write_text(Settings(updates={"check_on_start": False}).model_dump_json())
    config = ConfigFile(service.config_path)
    database = service.data_dir / "muli.sqlite3"
    manifest = InstallationManifest.capture(service, database, require_database=False)
    journal = TransactionJournal(service.data_dir / "management/transaction-v1", database)
    permit = BootPermit.acquire(
        journal,
        config.path,
        boot="origin",
        installation=manifest.digest,
        service=manifest.service_digest,
    )
    session = BootSession(journal, permit.lease, config.path.absolute(), permit, manifest)
    runtime = Runtime(config, service.data_dir, boot_session=session)
    plugins = PluginManager(runtime, FastAPI(), service.config_path.parent / "plugins")

    async def apply(target: Settings) -> None:
        raise AssertionError("transaction must not use in-process configuration")

    manager = SelfManagement(runtime, plugins, service, apply)

    async def verified() -> InstallationManifest:
        manifest.verify()
        return manifest

    monkeypatch.setattr(manager.recovery, "verify", verified)
    try:
        yield Host(runtime, manager, manifest, journal)
    finally:
        await manager.close()
        await runtime.close()


async def apply(host: Host, item: dict[str, Any]) -> None:
    async with host.runtime.configuration_lock:
        await host.manager._apply(item)


async def test_plan_generation_precedes_prepared_and_helper_acceptance(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        record = control.read()
        assert record is not None
        plan = host.runtime.store.get("management", record.request_id)
        assert plan["transaction"]["generation"] == record.generation
        assert host.runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert InstallationManifest.load(control.directory.path) == manifest
        calls.append(record.generation)
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    item = await host.manager.stage("restart")
    await apply(host, item)
    record = host.journal.read()
    assert record is not None and record.phase == "prepared"
    assert calls == [record.generation]
    plan = host.runtime.store.get("management", item["id"])
    assert plan["status"] == "applying"
    assert not [
        event for event in host.runtime.store.events() if event["type"] == "management.completed"
    ]
    assert host.runtime.maintenance.configuration_blocked


@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_interrupted_helper_observer_never_erases_durable_generation(
    host: Host, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    entered = asyncio.Event()

    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        entered.set()
        if failure == "error":
            raise ValueError("simulated unavailable controller")
        await asyncio.Event().wait()
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    item = await host.manager.stage("restart")
    task = asyncio.create_task(apply(host, item))
    await entered.wait()
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    record = host.journal.read()
    assert record is not None
    saved = host.runtime.store.get("management", item["id"])
    assert saved["transaction"]["generation"] == record.generation
    assert saved["status"] == "applying"
    with host.journal.locked() as control:
        final = control.advance(record.position, Transition(phase="aborted", code="drain_timeout"))
    receipt = project_final(host.runtime.maintenance.permit, host.runtime.store)
    assert receipt is not None and receipt.revision == final.revision
    projected = host.runtime.store.get("management", item["id"])
    assert projected["routing"] == saved["routing"]
    assert projected["status"] == "failed"
    completions = [
        event for event in host.runtime.store.events() if event["type"] == "management.completed"
    ]
    assert len(completions) == 1
    await host.manager.recovery.project(strict=True)
    assert (
        len(
            [
                event
                for event in host.runtime.store.events()
                if event["type"] == "management.completed"
            ]
        )
        == 1
    )


async def test_restart_uses_original_configuration_bytes(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    original = host.runtime.config.path.read_bytes()
    item = await host.manager.stage("restart")
    await apply(host, item)
    record = host.journal.read()
    assert record is not None
    assert host.journal.snapshot(record, "base") == original
    assert host.journal.snapshot(record, "target") == original


async def test_restart_configuration_only_prepares_candidate(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    original = host.runtime.config.path.read_bytes()
    item = await host.manager.stage(
        "configure", {"persona": {"user_name": "测试称呼"}}, apply_mode="restart_transaction"
    )
    await apply(host, item)
    record = host.journal.read()
    assert record is not None
    assert host.runtime.config.path.read_bytes() == original
    candidate = Settings.model_validate_json(host.journal.snapshot(record, "target"))
    assert candidate.persona.user_name == "测试称呼"


@pytest.mark.parametrize("during_probe", [False, True])
async def test_configuration_rechecks_preference_after_queue_and_probe(
    host: Host, monkeypatch: pytest.MonkeyPatch, during_probe: bool
) -> None:
    fact = extracted(host.runtime)
    item = await host.manager.stage(
        "configure", changes(fact, {"kind": "web"}), apply_mode="restart_transaction"
    )
    original = host.runtime.config.path.read_bytes()

    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        raise AssertionError("revoked evidence must never dispatch a controller")

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    if during_probe:

        async def verified() -> InstallationManifest:
            host.runtime.memory.forget_source("source")
            await asyncio.sleep(0)
            return host.manifest

        monkeypatch.setattr(host.manager.recovery, "verify", verified)
    else:
        host.runtime.memory.forget_source("source")
    await apply(host, item)
    assert host.journal.read() is None
    assert host.runtime.config.path.read_bytes() == original
    assert host.runtime.config.settings.channels.proactive.memory_preference is None
    assert host.runtime.store.get("management", item["id"])["status"] == "failed"
    assert not [
        event for event in host.runtime.store.events() if event["type"] == "management.preparing"
    ]


async def test_queued_restart_waits_for_configuration_gate_without_losing_target(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = asyncio.Event()

    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        called.set()
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    item = await host.manager.stage("restart")
    target = host.manager.directory / f"{item['id']}.json"
    original = target.read_bytes()
    host.runtime.maintenance.admission.pause()
    await host.manager.start()
    await asyncio.sleep(0)
    assert host.journal.read() is None and not called.is_set()
    assert target.read_bytes() == original
    assert host.runtime.store.get("management", item["id"])["status"] == "pending"
    host.runtime.maintenance.admission.release()
    host.runtime.maintenance.reconcile()
    async with asyncio.timeout(2):
        await called.wait()
    record = host.journal.read()
    assert record is not None and record.request_id == item["id"]
    assert target.exists()  # Only final projection may remove the private candidate.
