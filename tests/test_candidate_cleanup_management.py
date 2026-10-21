from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Never, cast

import pytest
from test_candidate_management import CandidateHost, configure_host, reap, wait
from test_candidate_management import candidate_host as candidate_host
from test_managed_launcher import _slot
from test_managed_preparation import Host as ManagedHost
from test_managed_preparation import host as host

import muli.process as process
from muli.candidate_policy import CandidateError, CandidateLimits
from muli.candidate_process_plan import read_process
from muli.managed_preparation import CurrentManagedInstallation, ManagedPreparation
from muli.management_transaction import JournalControl
from muli.platforms import candidate_recovery
from muli.platforms.candidate_process import CandidateObservation, CandidateProbe
from muli.platforms.candidate_process_state import AcceptedUnit, PendingProcess
from muli.platforms.installation import FileFingerprint, FileIdentity
from muli.platforms.linux_transaction import ObservationError, UnitSnapshot, clock_reading
from muli.platforms.managed_deployment import DeploymentAnchor

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed candidate cleanup")
LIMIT = 32_768
NAME = "muli-candidate-" + "a" * 32 + ".service"


@pytest.fixture(autouse=True)
def no_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        pytest.fail("candidate cleanup management tests must not start a subprocess")

    async def forbidden_async(*args: object, **kwargs: object) -> Never:
        forbidden(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
    monkeypatch.setattr("muli.process.run_process", forbidden_async)


@pytest.fixture
def source_probe(candidate_host: CandidateHost, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def observe(subject: ManagedPreparation) -> CurrentManagedInstallation:
        assert subject.runtime is candidate_host.runtime
        assert subject.runtime.configuration_lock.locked()
        calls.append("observe")

        def verify() -> None:
            calls.append("verify")
            candidate_host.policy.verify(candidate_host.policy.expected)
            assert subject.runtime.store.existing_main_session_id() is not None

        return cast(CurrentManagedInstallation, SimpleNamespace(verify=verify))

    monkeypatch.setattr(ManagedPreparation, "observe_current", observe)
    return calls


async def plan_for(
    subject: CandidateHost,
    *,
    phase: str = "candidate_cleanup_required",
    protocol: object = 1,
) -> dict[str, Any]:
    item = await subject.manager.stage_update("2.23.0", subject.policy.expected)
    item.update(
        status="applying",
        phase=phase,
        candidate_attempt=str(
            Path(subject.policy.settings.retained_root) / f"candidate-{item['id']}"
        ),
        recovery_notice="candidate_cleanup_unconfirmed",
    )
    if protocol is not None:
        item["candidate_protocol"] = protocol
    subject.runtime.store.put("management", item)
    return item


def receipt_for(
    item: dict[str, Any], phase: Literal["pending", "observed", "settled"] = "observed"
) -> PendingProcess:
    """Small real private files; unit/proc observations below are explicitly fake."""
    root = Path(item["candidate_attempt"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def fingerprint(name: str, data: bytes) -> FileFingerprint:
        path = root / name
        path.write_bytes(data)
        path.chmod(0o600)
        return FileFingerprint(
            path=str(path),
            identity=FileIdentity.of(path.stat()),
            digest=hashlib.sha256(data).hexdigest(),
        )

    info = root.stat()
    return PendingProcess(
        phase=phase,
        unit=NAME,
        boot=clock_reading().boot_id,
        root=str(root),
        device=info.st_dev,
        inode=info.st_ino,
        uid=info.st_uid,
        python=sys.executable,
        worker_file=fingerprint("_candidate_worker.py", b"# fixture: never executed\n"),
        control_file=fingerprint("process-" + "c" * 32 + ".json", b"{}"),
        deadline=time.monotonic() + 5,
        runtime_us=4_000_000,
        limits=CandidateLimits(
            seconds=5.0, preflight_seconds=2.0, stop_seconds=0.5, poll_interval=0.05
        ),
        accepted=(
            None
            if phase == "pending"
            else AcceptedUnit(
                invocation="1" * 32, cgroup="/app.slice/" + NAME, pid=123, start_ticks=456
            )
        ),
    )


def attach(subject: CandidateHost, item: dict[str, Any], value: PendingProcess) -> dict[str, Any]:
    updated = {**item, "candidate_process": value.model_dump(mode="json")}
    assert read_process(updated, LIMIT) == value
    subject.runtime.store.put("management", updated)
    return updated


class CleanupProbe(CandidateProbe):
    def __init__(self, pending: PendingProcess) -> None:
        async def forbidden(
            argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
        ) -> Never:
            pytest.fail("fake cleanup probe must not execute its command runner")

        super().__init__(runner=forbidden)
        assert self.runner is forbidden
        self.pending = pending
        self.live = True
        self.fail_stop = False
        self.calls: list[str] = []
        self.stopped = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def recoverable(
        self, name: str, argv: tuple[str, ...], limits: CandidateLimits, runtime_us: int
    ) -> CandidateObservation | None:
        assert limits == self.pending.limits and runtime_us == self.pending.runtime_us
        self.calls.append("recoverable")
        return await self.owned(name, argv)

    async def owned(self, name: str, argv: tuple[str, ...]) -> CandidateObservation:
        assert (name, argv) == (self.pending.unit, self.pending.worker)
        self.calls.append("owned")
        unit = UnitSnapshot(
            name,
            "loaded",
            "active",
            "running",
            "/run/transient/" + name,
            "",
            (),
            False,
            "1" * 32,
            (0, "/"),
            "exec",
            123,
            "/app.slice/" + name,
            argv[0],
            argv,
            False,
            "control-group",
            True,
        )
        if not self.live:
            unit = replace(unit, pid=0, active_state="inactive", sub_state="dead")
        return CandidateObservation(unit)

    async def stop(self, name: str) -> None:
        assert name == self.pending.unit
        self.calls.append("stop")
        self.stopped.set()
        await self.release.wait()
        if self.fail_stop:
            raise ObservationError("candidate_fixture_manager_unavailable")
        self.live = False

    async def kill(self, name: str) -> None:
        raise AssertionError("this fixture must settle during the ordinary stop grace")

    def empty(self, cgroup: str) -> bool:
        assert cgroup == "/app.slice/" + self.pending.unit
        self.calls.append("empty")
        return not self.live


def use_probe(monkeypatch: pytest.MonkeyPatch, pending: PendingProcess) -> CleanupProbe:
    probe = CleanupProbe(pending)
    monkeypatch.setattr(candidate_recovery, "CandidateProbe", lambda **kwargs: probe)
    return probe


async def test_recovered_tree_interrupts_once_and_unlocks_without_reinstalling(
    candidate_host: CandidateHost, source_probe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = candidate_host
    item = await plan_for(subject)
    pending = receipt_for(item)
    attach(subject, item, pending)
    before = subject.runtime.config.path.read_bytes()
    with pytest.raises(ValueError, match="已有待应用管理操作"):
        await subject.manager.stage_update("2.23.1", subject.policy.expected)
    probe = use_probe(monkeypatch, pending)
    result = await subject.manager.recover()
    saved = result["candidate_cleanup"]
    assert saved == subject.runtime.store.get("management", item["id"])
    assert saved["status"] == "interrupted" and saved["phase"] == "candidate_cleaned"
    assert saved["error"] == "candidate_preparation_interrupted" and "recovery_notice" not in saved
    assert saved["version"] == item["version"] and saved["routing"] == item["routing"]
    assert read_process(saved, LIMIT).phase == "settled"
    assert probe.calls.count("stop") == 1 and not probe.live
    assert source_probe == ["observe", "verify"]
    assert not (subject.manager.directory / f"{item['id']}.json").exists()
    assert Path(pending.control_file.path).exists()  # Recovery never garbage-collects the attempt.
    completed = [
        event
        for event in subject.runtime.store.events()
        if event["type"] == "management.completed" and event["data"]["id"] == item["id"]
    ]
    assert len(completed) == 1
    queued = await subject.manager.stage_update("2.23.1", subject.policy.expected)
    assert queued["status"] == "pending" and queued["id"] != item["id"]
    assert subject.runtime.config.path.read_bytes() == before
    assert not subject.attempts and not subject.steps and not subject.prepared


async def test_unconfirmed_tree_stays_applying_and_blocks_another_update(
    candidate_host: CandidateHost, source_probe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = candidate_host
    item = await plan_for(subject)
    pending = receipt_for(item)
    attach(subject, item, pending)
    probe = use_probe(monkeypatch, pending)
    probe.fail_stop = True
    result = await subject.manager.recover()
    saved = result["candidate_cleanup"]
    assert saved["status"] == "applying" and saved["phase"] == "candidate_cleanup_required"
    assert read_process(saved, LIMIT) == pending and probe.live
    assert saved["version"] == item["version"] and saved["routing"] == item["routing"]
    assert (subject.manager.directory / f"{item['id']}.json").exists()
    with pytest.raises(ValueError, match="已有待应用管理操作"):
        await subject.manager.stage_update("2.23.1", subject.policy.expected)
    assert not [
        event for event in subject.runtime.store.events() if event["type"] == "management.completed"
    ]
    assert not subject.attempts and not subject.steps and not subject.prepared


@pytest.mark.parametrize("phase", ["candidate_preparing", "candidate_cleanup_required"])
@pytest.mark.parametrize("protocol", [None, 0, True, 1])
async def test_startup_classifies_absence_only_for_the_current_protocol_before_launch(
    candidate_host: CandidateHost, phase: str, protocol: object
) -> None:
    subject = candidate_host
    item = await plan_for(subject, phase=phase, protocol=protocol)
    await subject.manager.start()
    saved = subject.runtime.store.get("management", item["id"])
    assert saved is not None
    safe = type(protocol) is int and protocol == 1 and phase == "candidate_preparing"
    assert saved["status"] == ("interrupted" if safe else "applying")
    assert saved["phase"] == ("candidate_cleaned" if safe else "candidate_cleanup_required")
    assert saved["version"] == item["version"]
    assert (subject.manager.directory / f"{item['id']}.json").exists() is not safe
    if not safe:
        with pytest.raises(ValueError, match="已有待应用管理操作"):
            await subject.manager.stage_update("2.23.1", subject.policy.expected)
    assert not subject.attempts and not subject.steps and not subject.prepared


async def test_startup_consumes_durable_settled_without_deleted_control_files(
    candidate_host: CandidateHost,
) -> None:
    subject = candidate_host
    item = await plan_for(subject)
    settled = receipt_for(item, "settled")
    attach(subject, item, settled)
    Path(settled.control_file.path).unlink()
    await subject.manager.start()
    saved = subject.runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "interrupted"
    assert read_process(saved, LIMIT) == settled
    assert not (subject.manager.directory / f"{item['id']}.json").exists()
    assert not subject.attempts and not subject.steps and not subject.prepared


async def test_concurrent_recovery_is_busy_and_repeated_cancel_keeps_cleanup_owned(
    candidate_host: CandidateHost, source_probe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = candidate_host
    item = await plan_for(subject)
    pending = receipt_for(item)
    attach(subject, item, pending)
    probe = use_probe(monkeypatch, pending)
    probe.release.clear()
    task = asyncio.create_task(subject.manager.recover())
    try:
        await wait(probe.stopped)
        concurrent = await subject.manager.recover()
        assert concurrent["helper"] == "candidate_cleanup_busy"
        assert source_probe == ["observe", "verify"] and not subject.manager.idle
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and probe.live
        assert subject.runtime.store.get("management", item["id"])["status"] == "applying"
        probe.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        saved = subject.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "interrupted"
        assert read_process(saved, LIMIT).phase == "settled"
        assert not probe.live and subject.manager.idle
    finally:
        probe.release.set()
        await reap(task)


async def test_management_close_joins_the_existing_cleanup_before_returning(
    candidate_host: CandidateHost, source_probe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = candidate_host
    item = await plan_for(subject)
    pending = receipt_for(item)
    attach(subject, item, pending)
    probe = use_probe(monkeypatch, pending)
    probe.release.clear()
    recovering = asyncio.create_task(subject.manager.recover())
    closing: asyncio.Task[None] | None = None
    try:
        await wait(probe.stopped)
        closing = asyncio.create_task(subject.manager.close())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done() and not recovering.done() and probe.live
        assert subject.runtime.store.existing_main_session_id() is not None
        probe.release.set()
        await asyncio.wait_for(asyncio.shield(closing), 2)
        await asyncio.wait_for(asyncio.gather(recovering, return_exceptions=True), 2)
        saved = subject.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "interrupted"
        assert read_process(saved, LIMIT).phase == "settled"
        assert not probe.live and subject.manager._candidate_cleanup is None
        assert not subject.attempts and not subject.steps
    finally:
        probe.release.set()
        await reap(recovering)
        if closing is not None:
            await reap(closing)


async def test_cleanup_observer_returns_captured_response_after_the_store_closes(
    candidate_host: CandidateHost, source_probe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = candidate_host
    item = await plan_for(subject)
    pending = receipt_for(item)
    attach(subject, item, pending)
    probe = use_probe(monkeypatch, pending)
    captured, release = asyncio.Event(), asyncio.Event()
    original = process._wait_cleanup

    async def delayed_observer(task: asyncio.Task[None]) -> bool:
        cancelled = await original(task)
        captured.set()
        await release.wait()
        return cancelled

    # Only the HTTP/tool observer is delayed; the recovery owner really finishes.
    monkeypatch.setattr(process, "_wait_cleanup", delayed_observer)
    observing = asyncio.create_task(subject.manager.recover())
    try:
        await wait(captured)
        saved = subject.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "interrupted"
        assert not probe.live and subject.manager.idle
        await asyncio.wait_for(subject.manager.close(), 2)
        await asyncio.wait_for(subject.runtime.close(), 2)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            subject.runtime.store.db.execute("SELECT 1")
        assert not observing.done()
        release.set()
        result = await asyncio.wait_for(observing, 2)
        assert result["candidate_cleanup"] == saved
        assert not subject.attempts and not subject.steps and not subject.prepared
    finally:
        release.set()
        await reap(observing)


@pytest.mark.parametrize("phase", ["pending", "settled"])
async def test_real_committed_process_receipt_survives_lost_return_and_stale_finish(
    candidate_host: CandidateHost, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    subject = candidate_host
    item = await plan_for(subject, phase="candidate_preparing")
    pending = receipt_for(item, "pending" if phase == "pending" else "observed")
    if phase == "settled":
        item = attach(subject, item, pending)
        value = PendingProcess.model_validate({**pending.model_dump(), "phase": "settled"})
    else:
        value = pending
    stale = {**item, "status": "failed", "error": "stale preparation failure"}
    original = subject.runtime.store.event
    lost = False

    def event(
        event_type: str,
        data: dict[str, Any],
        session_id: str | None = None,
        *,
        objects: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        nonlocal lost
        result = original(event_type, data, session_id, objects=objects)
        if not lost and data.get("candidate_process", {}).get("phase") == phase:
            assert not subject.runtime.store.db.in_transaction
            assert subject.runtime.store.get("management", item["id"]) == data
            lost = True
            raise RuntimeError("real commit completed but caller lost its return")
        return result

    monkeypatch.setattr(subject.runtime.store, "event", event)
    with pytest.raises(RuntimeError, match="real commit completed"):
        subject.manager._persist_candidate_process(item, value)
    assert lost and read_process(item, LIMIT) == value
    assert subject.runtime.store.get("management", item["id"]) == item
    snapshot = subject.manager.directory / f"{item['id']}.json"
    await subject.manager._finish_plan(stale, snapshot)
    saved = subject.runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "applying" and snapshot.exists()
    assert read_process(saved, LIMIT) == value
    assert saved["routing"] == item["routing"] and saved["version"] == "2.23.0"
    await subject.manager.start()
    saved = subject.runtime.store.get("management", item["id"])
    assert saved is not None
    assert saved["status"] == ("interrupted" if phase == "settled" else "applying")
    assert read_process(saved, LIMIT) == value
    assert not subject.attempts and not subject.steps and not subject.prepared


async def test_published_transaction_takes_priority_over_leftover_candidate_cleanup(
    host: ManagedHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = await host.run(host.plan("adopt"), candidate=host.slot)
    host.complete(original)
    selected = _slot(tmp_path, "later-retained-slot")
    candidate = configure_host(
        host.runtime,
        host.service,
        tmp_path,
        monkeypatch,
        paths=ManagedHost.paths(selected),
        fake_recovery=False,
    )
    monkeypatch.setattr("muli.managed_preparation.ManagedSystemdProbe", lambda: host.probe)

    async def ensure(control: JournalControl, anchor: DeploymentAnchor) -> str:
        raise CandidateError("candidate_fixture_helper_unavailable")

    monkeypatch.setattr("muli.managed_host.ensure_managed_helper", ensure)
    try:
        item = await candidate.manager.stage_update("2.23.0", candidate.policy.expected)
        await candidate.manager._apply_candidate(item)
        record = host.journal.read_current()
        assert record is not None and record.phase == "prepared" and record.request_id == item["id"]
        stored = host.runtime.store.get("management", item["id"])
        assert stored is not None
        stored["phase"] = "candidate_cleanup_required"
        stored["candidate_process"] = receipt_for(stored).model_dump(mode="json")
        host.runtime.store.put("management", stored)
        calls: list[str] = []

        async def recover() -> dict[str, Any]:
            calls.append("existing-transaction")
            return {**candidate.manager.recovery.current(), "helper": "accepted"}

        async def forbidden_cleanup(pending: PendingProcess, **kwargs: Any) -> Never:
            pytest.fail("published generation must not be replaced by candidate cleanup")

        monkeypatch.setattr(candidate.manager.recovery, "recover", recover)
        monkeypatch.setattr(candidate_recovery, "recover_candidate_process", forbidden_cleanup)
        result = await candidate.manager.recover()
        assert result["helper"] == "accepted" and calls == ["existing-transaction"]
        assert host.journal.read_current() == record
        assert (
            host.runtime.store.get("management", item["id"])["transaction"]["generation"]
            == record.generation
        )
    finally:
        await asyncio.wait_for(candidate.manager.close(), 2)
