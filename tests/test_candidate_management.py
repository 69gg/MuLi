from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never, cast

import pytest
from fastapi import FastAPI
from test_managed_launcher import _slot
from test_managed_preparation import Host as ManagedHost
from test_managed_preparation import host as host

import muli.management as management
from muli.candidate_lineage import check_plan_budget
from muli.candidate_policy import (
    CandidateError,
    CandidateSettings,
    ExpectedPolicy,
    ProcessOutput,
    ProcessSpec,
)
from muli.candidate_preparation import CandidateAttempt, FrozenPolicy, ReadyCandidate
from muli.candidate_update import CapturedCandidate
from muli.config import ConfigFile, Settings
from muli.managed_preparation import LocalSlotPaths
from muli.management import SelfManagement
from muli.management_transaction import JournalControl
from muli.platforms import candidate_process
from muli.platforms.installation import FileFingerprint
from muli.platforms.managed_deployment import DeploymentAnchor
from muli.platforms.managed_launcher import fingerprint as file_fingerprint
from muli.plugins import PluginManager
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.runtime import PreparedRequest, Runtime
from muli.service import UserService

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux candidate management")


@pytest.fixture(autouse=True)
def no_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """No service command, installer or accidental inherited runner may execute."""

    def forbidden(*args: object, **kwargs: object) -> Never:
        pytest.fail("candidate management tests must not start a subprocess")

    async def forbidden_async(*args: object, **kwargs: object) -> Never:
        forbidden(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
    monkeypatch.setattr("muli.process.run_process", forbidden_async)
    monkeypatch.setattr(candidate_process, "run_candidate_process", forbidden_async)


@dataclass(slots=True)
class Policy:
    """Only core source inspection is synthetic; policy reads use the real Store."""

    manager: SelfManagement
    settings: CandidateSettings
    expected: ExpectedPolicy

    def verify(self, expected: ExpectedPolicy, *, deadline: float | None = None) -> None:
        if expected != self.expected or self.manager.update_policy.read() != self.settings:
            raise CandidateError("candidate_policy_changed")


@dataclass(frozen=True, slots=True)
class Ready:
    paths: LocalSlotPaths
    evidence_file: FileFingerprint


@dataclass(slots=True)
class Attempt:
    """Fake process receipts, not a claim that an environment was installed."""

    policy: Policy
    request_id: str
    version: str
    configuration: bytes
    ready: Ready
    verified: list[str] = field(default_factory=list)

    def _spec(self, step: str) -> ProcessSpec:
        self.policy.verify(self.policy.expected)
        root = Path(self.policy.settings.retained_root)
        return ProcessSpec(
            argv=("unexecuted-candidate-fixture", step),
            env=(),
            cwd=root,
            private_root=root,
            deadline=asyncio.get_running_loop().time() + 5.0,
            limits=self.policy.settings.limits,
            disk_mode=self.policy.settings.disk_mode,
            observe=lambda: self.policy.verify(self.policy.expected),
        )

    def uv_version_spec(self) -> ProcessSpec:
        return self._spec("uv-version")

    def install_spec(self) -> ProcessSpec:
        return self._spec("install")

    def dependency_spec(self) -> ProcessSpec:
        return self._spec("dependencies")

    def probe_spec(self) -> ProcessSpec:
        return self._spec("probe")

    @staticmethod
    def accept_uv_version(output: ProcessOutput) -> None:
        assert output.returncode == 0

    accept_install = accept_uv_version
    accept_dependencies = accept_uv_version

    def finish(self, output: ProcessOutput) -> ReadyCandidate:
        assert output.returncode == 0
        return cast(ReadyCandidate, self.ready)

    def verify_ready(self, ready: ReadyCandidate) -> None:
        assert ready is self.ready
        self.verified.append(self.request_id)
        self.policy.verify(self.policy.expected)


@dataclass(slots=True)
class CandidateHost:
    manager: SelfManagement
    policy: Policy
    ready: Ready
    attempts: list[Attempt] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    prepared: list[dict[str, Any]] = field(default_factory=list)

    @property
    def runtime(self) -> Runtime:
        return self.manager.runtime

    async def capture(self) -> CapturedCandidate:
        assert self.runtime.configuration_lock.locked()
        self.policy.verify(self.policy.expected)
        return CapturedCandidate(cast(FrozenPolicy, self.policy), None)

    def create(
        self,
        policy: FrozenPolicy,
        *,
        expected: ExpectedPolicy,
        request_id: str,
        version: str,
        configuration: bytes,
    ) -> CandidateAttempt:
        assert policy is self.policy
        self.policy.verify(expected)
        attempt = Attempt(self.policy, request_id, version, configuration, self.ready)
        self.attempts.append(attempt)
        return cast(CandidateAttempt, attempt)

    async def run(self, spec: ProcessSpec) -> ProcessOutput:
        assert not self.runtime.configuration_lock.locked()
        self.steps.append(spec.argv[-1])
        spec.observe()
        return ProcessOutput(0)

    async def prepare(
        self,
        item: dict[str, Any],
        target: Settings,
        *,
        candidate_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        assert self.runtime.configuration_lock.locked()
        assert candidate_guard is not None
        candidate_guard()
        self.prepared.append(item)
        # Accepted preparation is deliberately not a fake completed transaction.
        return item


def configure_host(
    runtime: Runtime,
    service: UserService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    paths: LocalSlotPaths | None = None,
    fake_recovery: bool = True,
) -> CandidateHost:
    async def no_in_place(settings: Settings) -> Never:
        pytest.fail("candidate update must not use in-place configuration")

    plugins = PluginManager(runtime, FastAPI(), tmp_path / "plugins")
    manager = SelfManagement(runtime, plugins, service, no_in_place)
    settings = CandidateSettings(
        enabled=True,
        wheelhouse=str(tmp_path / "unexecuted-wheelhouse"),
        retained_root=str(tmp_path / "retained"),
        uv_executable=str(tmp_path / "unexecuted-uv"),
    )
    manager.update_policy.replace(settings)
    expected = ExpectedPolicy(
        source=settings.source,
        limits=settings.limits,
        disk_mode=settings.disk_mode,
        revision=hashlib.sha256(b"host-approved-fixture-policy").hexdigest(),
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b'{"test_only":true}')
    evidence.chmod(0o600)
    prefix = tmp_path / "retained" / "tools" / "muli-agent"
    ready = Ready(
        paths or LocalSlotPaths(prefix, prefix / "bin/python", prefix / "site-packages/muli"),
        FileFingerprint.model_validate(file_fingerprint(evidence, 1024)),
    )
    host = CandidateHost(manager, Policy(manager, settings, expected), ready)
    monkeypatch.setattr(manager.candidates, "capture", host.capture)
    monkeypatch.setattr(management, "create_attempt", host.create)
    monkeypatch.setattr(candidate_process, "run_candidate_process", host.run)
    if fake_recovery:

        async def project(*, strict: bool = False) -> None:
            return None

        monkeypatch.setattr(manager.recovery, "project", project)
        monkeypatch.setattr(manager.recovery, "require_managed_request", lambda action: None)
        monkeypatch.setattr(manager.recovery, "prepare", host.prepare)
        monkeypatch.setattr(
            manager, "_check_candidate_plan", lambda item: check_plan_budget(item, 32768)
        )
    return host


@pytest.fixture
async def candidate_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CandidateHost]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    host = configure_host(
        runtime, UserService(config.path, runtime.data_dir), tmp_path, monkeypatch
    )
    try:
        yield host
    finally:
        await asyncio.wait_for(host.manager.close(), 2.0)
        await asyncio.wait_for(runtime.close(), 2.0)


async def wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 2.0)


async def reap(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2.0)


async def test_approved_update_uses_canonical_switch_and_original_reply(
    candidate_host: CandidateHost,
) -> None:
    host = candidate_host
    runtime, manager = host.runtime, host.manager
    tool = runtime.tools["self_update"]
    assert tool.requires_approval and tool.risk == "external"
    origin = ReplyOrigin.load(
        {
            "kind": "channel",
            "input_id": "owner-original-input",
            "route": {
                "name": "telegram",
                "account_id": "account",
                "target": "owner",
                "sender_id": "owner",
                "kind": "private",
                "reply_to": "original-message",
            },
        }
    )
    main = runtime.main_session()["id"]
    token = runtime._request_context.set(PreparedRequest(main, [], reply_origin=origin))
    tool_session = runtime._tool_session.set(main)
    try:
        item = await tool.handler(
            {"version": "2.23.0", "expected_policy": host.policy.expected.model_dump(mode="json")}
        )
    finally:
        runtime._tool_session.reset(tool_session)
        runtime._request_context.reset(token)
    assert item["action"] == "switch" and item["operation"] == "update"
    assert item["expected_policy"] == host.policy.expected.model_dump(mode="json")
    assert item["routing"] == FrozenRouting.bound(origin).dump()
    assert "candidate" not in item and not host.steps
    await manager._apply_candidate(item)
    saved = runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "applying"
    assert saved["routing"] == item["routing"]
    assert host.steps == ["uv-version", "install", "dependencies", "probe"]
    assert host.attempts[0].configuration == (manager.directory / f"{item['id']}.json").read_bytes()
    assert host.prepared[0]["id"] == item["id"]
    assert host.prepared[0]["candidate_evidence"] == host.ready.evidence_file.model_dump(
        mode="json"
    )
    assert host.attempts[0].verified == [item["id"], item["id"]]
    assert not [
        event for event in runtime.store.events() if event["type"] == "management.completed"
    ]


async def test_stale_approval_never_queues_or_executes(candidate_host: CandidateHost) -> None:
    host = candidate_host
    stale = host.policy.expected.model_copy(update={"revision": "f" * 64})
    with pytest.raises(CandidateError, match="^candidate_policy_changed$"):
        await host.manager.stage_update("2.23.0", stale)
    assert not host.manager.plans() and not host.attempts and not host.steps


async def test_queued_policy_drift_never_builds_a_process_spec(
    candidate_host: CandidateHost,
) -> None:
    host = candidate_host
    item = await host.manager.stage_update("2.23.0", host.policy.expected)
    async with host.runtime.configuration_lock:
        host.manager.replace_update_policy(
            host.policy.settings.model_copy(update={"source": "changed-after-approval"})
        )
    await host.manager._apply_candidate(item)
    saved = host.runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "failed"
    assert saved["error"] == "candidate_policy_changed"
    assert not host.attempts and not host.steps and not host.prepared


async def test_uv_wait_releases_configuration_lock_and_policy_drift_stops_next_effect(
    candidate_host: CandidateHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = candidate_host
    item = await host.manager.stage_update("2.23.0", host.policy.expected)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(spec: ProcessSpec) -> ProcessOutput:
        assert not host.runtime.configuration_lock.locked()
        host.steps.append(spec.argv[-1])
        entered.set()
        await release.wait()
        return ProcessOutput(0)

    monkeypatch.setattr(candidate_process, "run_candidate_process", blocked)
    task = asyncio.create_task(host.manager._apply_candidate(item))
    try:
        await wait(entered)
        async with asyncio.timeout(2.0), host.runtime.configuration_lock:
            host.manager.replace_update_policy(
                host.policy.settings.model_copy(update={"source": "changed"})
            )
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 2.0)
        saved = host.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "failed"
        assert saved["error"] == "candidate_policy_changed"
        assert host.steps == ["uv-version"] and not host.prepared
    finally:
        release.set()
        await reap(task)


async def test_owner_cancel_during_locked_capture_remains_cancelled(
    candidate_host: CandidateHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = candidate_host
    item = await host.manager.stage_update("2.23.0", host.policy.expected)
    entered, release = asyncio.Event(), asyncio.Event()

    async def capture() -> CapturedCandidate:
        assert host.runtime.configuration_lock.locked()
        entered.set()
        await release.wait()
        return await host.capture()

    monkeypatch.setattr(host.manager.candidates, "capture", capture)
    task = asyncio.create_task(host.manager._apply_candidate(item))
    try:
        await wait(entered)
        cancelled = await host.manager.cancel(item["id"])
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 2.0)
        assert host.runtime.store.get("management", item["id"]) == cancelled
        assert cancelled["status"] == "cancelled"
        assert not host.attempts and not host.steps
        completed = [
            event
            for event in host.runtime.store.events()
            if event["type"] == "management.completed"
        ]
        assert len(completed) == 1 and completed[0]["data"] == cancelled
    finally:
        release.set()
        await reap(task)


async def test_close_waits_for_fake_process_cleanup_then_never_retries(
    candidate_host: CandidateHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = candidate_host
    item = await host.manager.stage_update("2.23.0", host.policy.expected)
    entered, cancelling, release, cleaned = (asyncio.Event() for _ in range(4))

    async def blocked(spec: ProcessSpec) -> ProcessOutput:
        host.steps.append(spec.argv[-1])
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            cleaned.set()
            raise
        raise AssertionError("unreachable fixture branch")

    monkeypatch.setattr(candidate_process, "run_candidate_process", blocked)
    await host.manager.start()
    closing: asyncio.Task[None] | None = None
    try:
        await wait(entered)
        closing = asyncio.create_task(host.manager.close())
        await wait(cancelling)
        assert not closing.done() and not cleaned.is_set()
        saved = host.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "applying"
        release.set()
        await asyncio.wait_for(asyncio.shield(closing), 2.0)
        saved = host.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "interrupted"
        assert saved["error"] == "candidate_preparation_interrupted" and cleaned.is_set()
        await host.manager.start()
        await asyncio.sleep(0)
        assert host.steps == ["uv-version"]
    finally:
        release.set()
        if closing is not None:
            await reap(closing)


@pytest.mark.parametrize("confirmed", [False, True])
async def test_unconfirmed_cleanup_blocks_new_plans_instead_of_claiming_failure(
    candidate_host: CandidateHost, monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    host = candidate_host
    item = await host.manager.stage_update("2.23.0", host.policy.expected)
    unit = "muli-candidate-" + "a" * 32 + ".service"

    async def cleanup_failure(spec: ProcessSpec) -> Never:
        host.steps.append(spec.argv[-1])
        raise candidate_process.CandidateProcessError(
            "candidate_cleanup_unconfirmed", unit_name=unit, cleanup_confirmed=confirmed
        )

    monkeypatch.setattr(candidate_process, "run_candidate_process", cleanup_failure)
    await host.manager._apply_candidate(item)
    saved = host.runtime.store.get("management", item["id"])
    assert saved is not None
    snapshot = host.manager.directory / f"{item['id']}.json"
    if confirmed:
        assert saved["status"] == "failed" and not snapshot.exists()
    else:
        assert saved["status"] == "applying" and snapshot.exists()
        assert saved["phase"] == "candidate_cleanup_required"
        assert saved["candidate_unit"] == unit
        assert saved["recovery_notice"] == "candidate_cleanup_unconfirmed"
        with pytest.raises(ValueError, match="已有待应用管理操作"):
            await host.manager.stage("configure", {"persona": {"user_name": "not-applied"}})
        assert not [
            event
            for event in host.runtime.store.events()
            if event["type"] == "management.completed"
        ]
    assert host.steps == ["uv-version"] and not host.prepared


@pytest.mark.parametrize("phase", ["candidate_preparing", "candidate_cleanup_required"])
async def test_startup_keeps_unproven_candidate_tree_blocking_without_a_transaction(
    candidate_host: CandidateHost, phase: str
) -> None:
    host = candidate_host
    item = await host.manager.stage_update("2.23.0", host.policy.expected)
    unit = "muli-candidate-" + "b" * 32 + ".service"
    item.update(status="applying", phase=phase, candidate_unit=unit)
    host.runtime.store.put("management", item)
    await host.manager.start()
    saved = host.runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "applying"
    assert saved["phase"] == "candidate_cleanup_required" and saved["candidate_unit"] == unit
    assert "transaction" not in saved
    with pytest.raises(ValueError, match="已有待应用管理操作"):
        await host.manager.stage("configure", {"persona": {"user_name": "not-applied"}})
    assert not host.attempts and not host.steps


async def test_ready_does_not_prepare_a_different_persisted_plan(
    candidate_host: CandidateHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = candidate_host
    item = await host.manager.stage_update("2.23.0", host.policy.expected)

    async def change_plan(spec: ProcessSpec) -> ProcessOutput:
        output = await host.run(spec)
        if spec.argv[-1] == "probe":
            saved = host.runtime.store.get("management", item["id"])
            assert saved is not None
            host.runtime.store.put("management", {**saved, "version": "2.24.0"})
        return output

    monkeypatch.setattr(candidate_process, "run_candidate_process", change_plan)
    await host.manager._apply_candidate(item)
    saved = host.runtime.store.get("management", item["id"])
    assert saved is not None and saved["status"] == "failed"
    assert saved["error"] == "candidate_plan_changed"
    assert not host.prepared and not host.attempts[0].verified


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_real_prepared_generation_survives_candidate_helper_failure(
    host: ManagedHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = await host.run(host.plan("adopt"), candidate=host.slot)
    host.complete(original)
    selected = _slot(tmp_path, "different-retained-slot")
    candidate = configure_host(
        host.runtime,
        host.service,
        tmp_path,
        monkeypatch,
        paths=ManagedHost.paths(selected),
        fake_recovery=False,
    )
    monkeypatch.setattr("muli.managed_preparation.ManagedSystemdProbe", lambda: host.probe)
    entered = asyncio.Event()

    async def ensure(control: JournalControl, anchor: DeploymentAnchor) -> str:
        entered.set()
        if failure == "error":
            raise CandidateError("candidate_helper_unavailable")
        await asyncio.Event().wait()
        return "accepted"

    monkeypatch.setattr("muli.managed_host.ensure_managed_helper", ensure)
    item = await candidate.manager.stage_update("2.23.0", candidate.policy.expected)
    task = asyncio.create_task(candidate.manager._apply_candidate(item))
    try:
        await wait(entered)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 2.0)
        else:
            await asyncio.wait_for(asyncio.shield(task), 2.0)
        record = host.journal.read_current()
        assert record is not None and record.phase == "prepared"
        assert record.request_id == item["id"]
        saved = host.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "applying"
        assert saved["transaction"]["generation"] == record.generation
        assert saved["routing"] == item["routing"]
        assert saved["action"] == "switch" and saved["operation"] == "update"
        assert not [
            event
            for event in host.runtime.store.events()
            if event["type"] == "management.completed" and event["data"]["id"] == item["id"]
        ]
    finally:
        await reap(task)
        await asyncio.wait_for(candidate.manager.close(), 2.0)


async def test_recovery_prepare_rechecks_candidate_guard_after_its_last_probe(
    host: ManagedHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = await host.run(host.plan("adopt"), candidate=host.slot)
    previous = host.complete(original)
    selected = _slot(tmp_path, "guarded-retained-slot")
    candidate = configure_host(
        host.runtime,
        host.service,
        tmp_path,
        monkeypatch,
        paths=ManagedHost.paths(selected),
        fake_recovery=False,
    )
    monkeypatch.setattr("muli.managed_preparation.ManagedSystemdProbe", lambda: host.probe)
    probes = 0

    def changed_policy() -> None:
        nonlocal probes
        probes += 1
        if probes == 2:
            assert host.runtime.configuration_lock.locked()
            candidate.manager.replace_update_policy(
                candidate.policy.settings.model_copy(update={"source": "changed"})
            )

    host.probe.during = changed_policy
    try:
        item = await candidate.manager.stage_update("2.23.0", candidate.policy.expected)
        await candidate.manager._apply_candidate(item)
        saved = host.runtime.store.get("management", item["id"])
        assert saved is not None and saved["status"] == "failed"
        assert saved["error"] == "candidate_policy_changed" and probes == 2
        assert host.journal.read_current() == previous
        assert "transaction" not in saved
        assert candidate.attempts[0].verified == [item["id"], item["id"]]
    finally:
        await asyncio.wait_for(candidate.manager.close(), 2.0)
