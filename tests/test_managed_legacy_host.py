from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi import FastAPI
from test_legacy_transaction_bridge import Recovered
from test_legacy_transaction_bridge import recovered as recovered

from muli import managed_boot, management_host
from muli.config import Settings
from muli.legacy_boot_adapter import LegacyBootPermit
from muli.managed_boot import ManagedBootError, ManagedBootPermit
from muli.managed_transaction import TransactionRecordV2
from muli.management import SelfManagement
from muli.management_projection import read_projection
from muli.management_transaction import (
    JournalControl,
    ProjectionReceipt,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
    Transition,
)
from muli.platforms.installation import InstallationManifest
from muli.platforms.linux_bootstrap import BootSession
from muli.platforms.linux_transaction import (
    ProcessIdentity,
    ServiceSnapshot,
    SystemdProbe,
    UnitSnapshot,
)
from muli.platforms.managed_deployment import resolve_entry
from muli.plugins import PluginManager
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.runtime import PreparedRequest, Runtime
from muli.service import UserService

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Linux managed-to-legacy host recovery"),
    pytest.mark.parametrize("recovered", ["rolled_back"], indirect=True),
]


class Probe(SystemdProbe):
    """Process observation is synthetic; on-disk identity verification stays real."""

    def __init__(self, service: UserService, boot: str) -> None:
        super().__init__()
        self.service, self.boot, self.calls = service, boot, 0

    async def read(self, service: UserService) -> ServiceSnapshot:
        assert service is self.service
        self.calls += 1
        unit = UnitSnapshot(
            name=service.unit,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            fragment=str(service.path),
            source="",
            drop_ins=(),
            needs_reload=False,
            invocation=self.boot,
            job=(0, "/"),
            service_type="simple",
            pid=os.getpid(),
            cgroup="/fixture/muli.service",
            executable=service.executable,
            argv=tuple(service._arguments()),
            ignore_failure=False,
            kill_mode="control-group",
            send_sigkill=True,
        )
        return ServiceSnapshot(
            unit,
            ProcessIdentity(
                os.getpid(), 1, os.geteuid(), service.executable, unit.argv, unit.cgroup
            ),
        )


@dataclass(slots=True)
class Host:
    seed: Recovered
    runtime: Runtime
    manager: SelfManagement
    owner: ManagedBootPermit
    session: BootSession
    probe: Probe

    async def stage(self, action: Literal["configure", "restart"] = "restart") -> dict[str, Any]:
        session_id = self.runtime.main_session()["id"]
        request = PreparedRequest(
            session_id, [], reply_origin=ReplyOrigin("web", "original-legacy-request")
        )
        token = self.runtime._tool_session.set(session_id)
        request_token = self.runtime._request_context.set(request)
        try:
            return await self.manager.stage(
                action,
                {"persona": {"append": "next legacy configuration"}}
                if action == "configure"
                else None,
                apply_mode="restart_transaction",
            )
        finally:
            self.runtime._request_context.reset(request_token)
            self.runtime._tool_session.reset(token)

    async def apply(self, item: dict[str, Any]) -> None:
        async with self.runtime.configuration_lock:
            await self.manager._apply(item)

    def assert_owned(self) -> None:
        self.owner.check_paths(self.runtime.config.path, self.owner.journal.database)
        assert self.owner._database is not None and self.owner._database.fd is not None
        os.fstat(self.owner._database.fd)
        with pytest.raises(TransactionError, match="lock_busy"):
            self.seed.journal.writer_lease().__enter__()


@pytest.fixture
async def host(
    recovered: Recovered, pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Host]:
    # Absolute-path execution must still exercise the requested worktree source.
    assert (
        Path(management_host.__file__).resolve()
        == (pytestconfig.rootpath / "src/muli/management_host.py").resolve()
    )
    # tmp_path may live on tmpfs/overlay. Only filesystem topology is synthetic;
    # file identities, snapshots, Store admission and exclusive leases stay real.
    monkeypatch.setattr(managed_boot, "verify_local_filesystems", lambda paths: None)
    monkeypatch.setattr(management_host, "verify_local_filesystems", lambda paths: None)
    # The seed fixture owns its journal descriptors. Close its only Store and
    # lease first, then give BootSession a separate real journal handle to own.
    # Runtime alone opens this permit's existing Store, exactly once.
    recovered.store.close()
    recovered.writer.__exit__()
    journal = TransactionJournal(
        recovered.journal.directory.path,
        recovered.journal.database,
        recovered.journal.limits,
        create=False,
    )
    owner: ManagedBootPermit | None = None
    runtime: Runtime | None = None
    manager: SelfManagement | None = None
    session: BootSession | None = None
    try:
        assert recovered.previous.health is not None
        session = BootSession(journal, journal.writer_lease(), Path(recovered.manifest.config_path))
        session.lease.__enter__()
        try:
            owner = ManagedBootPermit.acquire(
                journal,
                boot=recovered.previous.health.boot,
                plan=resolve_entry(journal.directory.path, purpose="service"),
                lease=session.lease,
            )
        except ManagedBootError as error:
            if error._owner is not None:
                session.permit = error._owner
            raise
        assert not owner._opened
        session.permit = owner
        runtime = Runtime(owner.config, Path(recovered.manifest.data_dir), boot_session=session)
        assert owner._opened
        assert runtime.store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        service = recovered.manifest.user_service()
        plugins = PluginManager(runtime, FastAPI(), Path(service.config_path).parent / "plugins")

        async def forbidden(target: Settings) -> None:
            raise AssertionError("a recovery transaction must not hot-apply its candidate")

        manager = SelfManagement(runtime, plugins, service, forbidden)
        probe = Probe(service, owner.boot)
        manager.recovery.probe = probe
        yield Host(recovered, runtime, manager, owner, session, probe)
    finally:
        if manager is not None:
            await manager.close()
        if runtime is not None:
            await runtime.close()
        elif session is not None and not session._closed:
            session.close_after_writers()
        elif session is None:
            if owner is not None:
                owner.close_after_writers()
            journal.__exit__()


@pytest.mark.parametrize("action", ["configure", "restart"])
async def test_actual_managed_owner_can_prepare_legacy_successor_after_hot_configuration(
    host: Host, monkeypatch: pytest.MonkeyPatch, action: Literal["configure", "restart"]
) -> None:
    runtime, owner = host.runtime, host.owner
    runtime.maintenance.require_configuration()
    runtime.config.update({"persona": {"append": "accepted hot configuration"}})
    base = runtime.config.path.read_bytes()
    seen: list[str] = []

    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        record = control.read()
        assert record is not None
        view = runtime.maintenance.permit
        assert isinstance(view, LegacyBootPermit) and view.writer_owner is owner
        assert host.session.permit is view and host.session.manifest == manifest
        manifest.verify()
        assert InstallationManifest.load(control.journal.directory.path) == manifest
        assert record.identity.installation == manifest.digest
        seen.append(record.generation)
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    item = await host.stage(action)
    routing = FrozenRouting.bound(ReplyOrigin("web", "original-legacy-request")).dump()
    assert item["routing"] == routing
    await host.apply(item)
    record = owner.journal.read_current()
    assert isinstance(record, TransactionRecord) and record.phase == "prepared"
    assert seen == [record.generation] and host.probe.calls > 0
    assert record.identity.origin_boot == owner.boot
    assert owner.journal.snapshot(record, "base") == base
    if action == "restart":
        assert owner.journal.snapshot(record, "target") == base
    else:
        target = Settings.model_validate_json(owner.journal.snapshot(record, "target"))
        assert target.persona.append == "next legacy configuration"
    stored = runtime.store.get("management", item["id"])
    assert stored is not None and stored["status"] == "applying" and stored["routing"] == routing
    assert not [
        event for event in runtime.store.events() if event["type"] == "management.completed"
    ]
    host.assert_owned()
    with owner.journal.locked() as control:
        final = control.advance(record.position, Transition(phase="aborted", code="drain_timeout"))
    await host.manager.recovery.project(strict=True)
    assert read_projection(runtime.store) == ProjectionReceipt(
        **final.position.model_dump(), store_instance=runtime.store.instance_id
    )
    assert runtime.store.get("management", item["id"])["routing"] == routing
    completions = [
        event for event in runtime.store.events() if event["type"] == "management.completed"
    ]
    assert len(completions) == 1 and completions[0]["data"]["routing"] == routing
    host.assert_owned()
    anchor = owner._database
    assert anchor is not None
    await runtime.close()
    assert host.session._closed and owner._closed and anchor.fd is None
    with host.seed.journal.writer_lease():
        pass


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_legacy_helper_observer_failure_keeps_one_generation_and_routing(
    host: Host, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    entered = asyncio.Event()
    seen: list[str] = []

    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        record = control.read()
        assert record is not None
        seen.append(record.generation)
        if len(seen) > 1:
            return "accepted"
        entered.set()
        if failure == "error":
            raise TimeoutError("synthetic helper observation failed")
        await asyncio.Event().wait()
        return "accepted"

    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    item = await host.stage()
    task = asyncio.create_task(host.apply(item))
    async with asyncio.timeout(2):
        await entered.wait()
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    record = host.owner.journal.read_current()
    assert isinstance(record, TransactionRecord) and seen == [record.generation]
    stored = host.runtime.store.get("management", item["id"])
    assert stored is not None and stored["status"] == "applying"
    assert stored["transaction"]["generation"] == record.generation
    assert stored["routing"] == item["routing"]
    assert (host.manager.directory / f"{item['id']}.json").exists()
    host.assert_owned()
    result = await host.manager.recovery.recover()
    assert result["helper"] == "accepted"
    assert seen == [record.generation, record.generation]
    assert host.owner.journal.read_current() == record


async def test_published_legacy_state_with_lost_return_can_recover_in_the_same_process(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = await host.stage()
    write = JournalControl._write
    interrupted = False
    seen: list[str] = []

    def lost_return(
        control: JournalControl, record: TransactionRecord | TransactionRecordV2
    ) -> TransactionRecord | TransactionRecordV2:
        nonlocal interrupted
        result = write(control, record)
        if (
            isinstance(record, TransactionRecord)
            and record.request_id == item["id"]
            and not interrupted
        ):
            interrupted = True
            raise OSError("synthetic lost acknowledgement after durable V1 publication")
        return result

    async def ensure(control: JournalControl, manifest: InstallationManifest) -> str:
        record = control.read()
        assert record is not None
        manifest.verify()
        seen.append(record.generation)
        return "accepted"

    monkeypatch.setattr(JournalControl, "_write", lost_return)
    monkeypatch.setattr("muli.management_helper.ensure_helper", ensure)
    await host.apply(item)
    record = host.owner.journal.read_current()
    assert interrupted and isinstance(record, TransactionRecord) and record.phase == "prepared"
    stored = host.runtime.store.get("management", item["id"])
    assert stored is not None and stored["status"] == "applying"
    assert stored["transaction"]["generation"] == record.generation
    result = await host.manager.recovery.recover()
    assert result["helper"] == "accepted"
    view = host.runtime.maintenance.permit
    assert isinstance(view, LegacyBootPermit) and view.writer_owner is host.owner
    assert host.session.permit is view and host.session.manifest is not None
    assert seen == [record.generation] and host.owner.journal.read_current() == record
    assert host.runtime.store.get("management", item["id"])["routing"] == item["routing"]
    host.assert_owned()
