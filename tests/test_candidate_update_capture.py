from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_candidate_preparation import attempt, install_fixture, wheel
from test_candidate_preparation import no_processes as no_processes
from test_install_profile import receipt
from test_managed_boot import _slot
from test_managed_preparation import Host
from test_managed_preparation import host as host
from test_managed_transaction import move, proof, verifying

import muli
from muli import managed_preparation
from muli.candidate_policy import CandidateError, CandidateLimits, CandidateSettings
from muli.candidate_policy_store import CandidatePolicyStore
from muli.candidate_preparation import ReadyCandidate
from muli.candidate_update import CandidateUpdates, CapturedCandidate
from muli.maintenance import MaintenanceError
from muli.managed_boot import ManagedBootPermit
from muli.managed_journal import read_snapshots
from muli.managed_preparation import PreparedManaged
from muli.managed_runtime_binding import verify_running_binding
from muli.managed_transaction import TransactionRecordV2, TransitionV2
from muli.management_projection import project_final
from muli.platforms import install_receipt, installation, managed_deployment
from muli.platforms.install_receipt import InstallReceiptError
from muli.platforms.installation import FileFingerprint
from muli.platforms.managed_deployment import (
    SelectorStore,
    SelectorValue,
    SlotIdentity,
    resolve_entry,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed candidate capture")


def configured_policy(host: Host) -> tuple[CandidatePolicyStore, CandidateSettings]:
    materials, retained = host.root / "wheelhouse", host.root / "candidates"
    materials.mkdir(mode=0o700)
    retained.mkdir(mode=0o700)
    wheel(materials / "muli_agent-2.23.0-py3-none-any.whl")
    uv = host.root / "unexecuted-uv"
    uv.write_bytes(b"synthetic uv, never executed")
    uv.chmod(0o700)
    settings = CandidateSettings(
        enabled=True,
        wheelhouse=str(materials),
        retained_root=str(retained),
        uv_executable=str(uv),
        limits=CandidateLimits(
            wheel_bytes=1024**2,
            compressed_bytes=1024**2,
            expanded_bytes=1024**2,
            candidate_bytes=1024**2,
            cache_bytes=1024**2,
            spare_bytes=1024,
        ),
    )
    policies = CandidatePolicyStore(host.runtime.store)
    policies.replace(settings)
    return policies, settings


def write_receipt(slot: SlotIdentity) -> Path:
    path = Path(slot.prefix) / "uv-receipt.toml"
    path.write_bytes(
        receipt(
            {
                "requirements": [{"name": "muli-agent", "extras": ["mcp"], "specifier": ">=2,<3"}],
                "entrypoints": [{"name": "muli", "install-path": str(path.parent / "bin/muli")}],
            }
        )
    )
    return path


@dataclass
class CaptureHost:
    host: Host
    policies: CandidatePolicyStore
    settings: CandidateSettings
    closing: bool = False
    retired: list[ManagedBootPermit] = field(default_factory=list)
    updates: CandidateUpdates = field(init=False)

    def __post_init__(self) -> None:
        self.updates = CandidateUpdates(
            self.host.runtime, self.host.service, self.policies, closing=lambda: self.closing
        )

    async def capture(self) -> CapturedCandidate:
        async with self.host.runtime.configuration_lock:
            return await self.updates.capture()

    def select_process(self, slot: SlotIdentity) -> None:
        # Only the current interpreter observation is synthetic. Do not mutate
        # global sys.platform or relax actual slot/file/Store/lease validation.
        system = SimpleNamespace(
            platform="linux",
            executable=slot.lexical_python,
            prefix=slot.prefix,
            base_prefix=str(self.host.root / "base-python"),
        )
        for module in (managed_deployment, installation):
            self.host.monkeypatch.setattr(module, "sys", system)
        self.host.monkeypatch.setattr(
            muli, "__file__", str(Path(slot.package.root) / "__init__.py")
        )

    def complete(self, prepared: PreparedManaged, *, boot: str) -> TransactionRecordV2:
        """Complete a fake service boot through real admission and projection."""
        host = self.host
        record = verifying(prepared.record)
        record = move(record, TransitionV2(phase="committed", proof=proof(record, boot=boot)))
        record = move(
            record,
            TransitionV2(
                phase="committed",
                activation="released",
                proof=proof(record, boot=boot, admission="released"),
            ),
        )
        data = read_snapshots(host.journal, record, "target")
        Path(prepared.anchor.config_path).write_bytes(data.configuration)
        Path(prepared.anchor.unit_path).write_bytes(data.unit)
        with SelectorStore(host.journal.directory.path) as selector:
            selector.replace(
                selector.read(),
                SelectorValue.model_validate_json(data.selector),
                guard=lambda: None,
            )
        with host.journal.locked() as control:
            control._write(record)
        previous = host.runtime.maintenance.permit
        assert previous is not None and host.runtime.boot_session is not None
        if isinstance(previous, ManagedBootPermit):
            self.retired.append(previous)
        permit = ManagedBootPermit.acquire(
            host.journal,
            boot=boot,
            plan=resolve_entry(host.journal.directory.path, purpose="service"),
            lease=previous.lease,
        )
        host.runtime.maintenance.permit = permit
        host.runtime.boot_session.permit = permit
        host.runtime.config = permit.config
        host.probe.boot = boot
        assert (
            project_final(
                permit,
                host.runtime.store,
                verify_binding=lambda value, role: verify_running_binding(
                    permit, host.runtime.store, value, role
                ),
            )
            is not None
        )
        return record

    async def switch(
        self, slot: SlotIdentity, *, name: str, candidate: ReadyCandidate | None = None
    ) -> TransactionRecordV2:
        item = self.host.plan("switch", name=name)
        if candidate is not None:
            item.update(
                operation="update",
                candidate_evidence=candidate.evidence_file.model_dump(mode="json"),
            )
            self.host.runtime.store.put("management", item)
        prepared = await self.host.run(item, candidate=slot)
        self.select_process(slot)
        return self.complete(prepared, boot="boot-" + name)


@pytest.fixture
async def capture_host(host: Host) -> AsyncIterator[CaptureHost]:
    policies, settings = configured_policy(host)
    value = CaptureHost(host, policies, settings)
    write_receipt(host.slot)
    # Capture uses the real observe_current method; only its systemd/proc
    # reader is replaced by the established fixture boundary.
    host.monkeypatch.setattr(managed_preparation, "ManagedSystemdProbe", lambda: host.probe)
    try:
        value.complete(await host.run(host.plan("adopt"), candidate=host.slot), boot="adopted")
        yield value
    finally:
        # Rebinding a fake boot must retain old DB descriptors until the actual
        # Runtime Store closes. The writer lease is shared, never duplicated.
        await host.runtime.close()
        for owner in reversed(value.retired):
            owner.close_after_writers()


async def test_capture_uses_actual_current_source_without_creating_a_plan(
    capture_host: CaptureHost,
) -> None:
    host = capture_host.host
    plans = host.runtime.store.list("management", 100)
    position, record = host.runtime.store.event_position(), host.journal.read_current()
    configuration = host.service.config_path.read_bytes()
    captured = await capture_host.capture()
    captured.policy.verify(captured.policy.expected)
    assert captured.lineage is None
    assert captured.policy.source.slot == host.slot
    assert captured.policy.source.receipt.installation == host.slot.digest
    assert captured.policy.original_intent.main.specifier == ">=2,<3"
    assert host.journal.read_current() == record
    assert host.runtime.store.list("management", 100) == plans
    assert host.runtime.store.event_position() == position
    assert host.service.config_path.read_bytes() == configuration
    assert not list(Path(capture_host.settings.retained_root).iterdir())


@pytest.mark.parametrize("change", ["source", "budget", "disabled"])
async def test_frozen_capture_rechecks_the_persisted_policy(
    capture_host: CaptureHost, change: str
) -> None:
    captured = await capture_host.capture()
    settings = capture_host.settings
    if change == "source":
        settings = settings.model_copy(update={"source": "new-owner-source"})
    elif change == "budget":
        settings = settings.model_copy(
            update={"limits": settings.limits.model_copy(update={"tasks": 32})}
        )
    else:
        settings = settings.model_copy(update={"enabled": False})
    capture_host.policies.replace(settings)
    with pytest.raises(CandidateError, match="^candidate_policy_changed$"):
        captured.policy.verify(captured.policy.expected)


async def test_policy_change_during_current_process_observation_is_rejected(
    capture_host: CaptureHost,
) -> None:
    updated = capture_host.settings.model_copy(update={"source": "changed-during-observe"})
    capture_host.host.probe.during = lambda: capture_host.policies.replace(updated)
    with pytest.raises(CandidateError, match="^candidate_policy_changed$"):
        await capture_host.capture()


async def test_current_generation_change_invalidates_the_captured_host_binding(
    capture_host: CaptureHost,
) -> None:
    captured = await capture_host.capture()
    prepared = await capture_host.host.run(capture_host.host.plan("restart", name="next-restart"))
    assert prepared.record.phase == "prepared"
    with pytest.raises(MaintenanceError, match="^transaction_in_progress$"):
        captured.policy.source.verify()


async def test_receipt_change_after_capture_invalidates_the_live_source(
    capture_host: CaptureHost,
) -> None:
    captured = await capture_host.capture()
    path = Path(captured.policy.source.receipt.receipt.path)
    path.write_bytes(path.read_bytes() + b"\n# changed after capture\n")
    with pytest.raises(InstallReceiptError, match="^receipt_changed$"):
        captured.policy.source.verify()


async def test_receipt_change_between_actual_file_observations_is_rejected(
    capture_host: CaptureHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    observe = install_receipt._observe
    path = Path(capture_host.host.slot.prefix) / "uv-receipt.toml"
    calls = 0

    def changing(source: Path, limit: int) -> tuple[bytes, FileFingerprint]:
        nonlocal calls
        result: tuple[bytes, FileFingerprint] = observe(source, limit)
        if source == path:
            calls += 1
            if calls == 1:
                source.write_bytes(result[0] + b"\n# replaced between observations\n")
        return result

    monkeypatch.setattr(install_receipt, "_observe", changing)
    with pytest.raises(InstallReceiptError, match="^receipt_changed$"):
        await capture_host.capture()
    assert calls == 2


@pytest.mark.parametrize("change", ["plan", "evidence"])
async def test_captured_source_rechecks_its_real_current_plan_attachment(
    capture_host: CaptureHost, change: str
) -> None:
    original = await capture_host.capture()
    ready = install_fixture(attempt(original.policy, "installed-update"))
    record = await capture_host.switch(ready.slot, name="installed-update", candidate=ready)
    captured = await capture_host.capture()
    assert captured.lineage is not None
    assert captured.lineage.evidence == ready.evidence
    assert captured.policy.source.previous == ready.evidence
    assert captured.policy.original_intent.main.specifier == ">=2,<3"
    assert captured.policy.source.receipt.parsed.intent.constraints[-1].specifier == "==2.23.0"
    if change == "plan":
        item = capture_host.host.runtime.store.get("management", record.request_id)
        item["candidate_evidence"]["digest"] = "f" * 64
        capture_host.host.runtime.store.put("management", item)
    else:
        path = Path(ready.evidence_file.path)
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(CandidateError, match="^candidate_source_evidence_changed$"):
        captured.policy.source.verify()


async def test_protected_roots_include_distinct_controller_base_and_target(
    capture_host: CaptureHost,
) -> None:
    host = capture_host.host
    base, target = _slot(host.root, "prior-slot"), _slot(host.root, "current-slot")
    write_receipt(base)
    write_receipt(target)
    await capture_host.switch(base, name="prior-switch")
    record = await capture_host.switch(target, name="current-switch")
    captured = await capture_host.capture()
    assert len({host.slot.digest, record.base.installation, record.target.installation}) == 3
    assert set(captured.policy.source.protected_roots) == {
        Path(host.slot.prefix),
        Path(base.prefix),
        Path(target.prefix),
    }
    assert captured.policy.source.slot == target
    assert captured.policy.source.receipt.receipt.path == str(
        Path(target.prefix) / "uv-receipt.toml"
    )
    assert captured.policy.source.previous is None


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_closing_host_rejects_capture_or_its_live_guard(
    capture_host: CaptureHost, stage: str
) -> None:
    captured = await capture_host.capture() if stage == "after" else None
    calls = capture_host.host.probe.calls
    capture_host.closing = True
    with pytest.raises(CandidateError, match="^candidate_host_closing$"):
        if captured is None:
            await capture_host.capture()
        else:
            captured.policy.source.verify()
    assert capture_host.host.probe.calls == calls


async def test_disabled_policy_rejects_before_observing_or_scanning_source(
    capture_host: CaptureHost,
) -> None:
    capture_host.policies.replace(CandidateSettings())
    calls = capture_host.host.probe.calls
    with pytest.raises(CandidateError, match="^candidate_source_not_configured$"):
        await capture_host.capture()
    assert capture_host.host.probe.calls == calls


async def test_legacy_host_cannot_supply_managed_candidate_authority(host: Host) -> None:
    policies, _ = configured_policy(host)
    updates = CandidateUpdates(host.runtime, host.service, policies, closing=lambda: False)
    async with host.runtime.configuration_lock:
        with pytest.raises(MaintenanceError, match="^managed_boot_required$"):
            await updates.capture()
    assert host.journal.read_current() is None
    assert host.runtime.store.list("management", 100) == []
    assert host.probe.calls == 0


async def test_capture_requires_the_actual_configuration_lock(capture_host: CaptureHost) -> None:
    with pytest.raises(MaintenanceError, match="^configuration_lock_required$"):
        await capture_host.updates.capture()
