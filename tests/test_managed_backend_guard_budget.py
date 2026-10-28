"""Count real backend guards and retain the low-level effect boundaries."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from test_managed_backend import Host
from test_managed_backend import host as host

from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionError
from muli.platforms.installation import FileFingerprint
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_backend import _DiskBinding
from muli.platforms.managed_deployment import (
    ManagedDeploymentError,
    SelectorSnapshot,
    SelectorStore,
    SelectorValue,
)
from muli.platforms.managed_launcher import PackageProofs
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed effects/leases")


async def test_confirm_uses_seven_real_checks_for_present_selector(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.install_intent()
    before = host.backend._disk()
    checks: list[tuple[TransactionRecordV2, Role | None]] = []
    original = host.backend._check

    def counted(
        current: TransactionRecordV2,
        role: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        checks.append((current, role))
        original(
            current,
            role,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )

    monkeypatch.setattr(host.backend, "_check", counted)
    with host.journal.writer_lease() as lease:
        confirmed = host.backend._confirm(record, "base", lease, host.probe.snapshot.unit.cgroup)

    assert confirmed == before == host.backend._disk()
    # The switch fixture has an existing selector: its low-level file confirm
    # retains two guards; the removed selector wrapper guard was redundant.
    assert checks == [(record, "base")] * 7
    assert host.record() == record and host.commands == []


async def test_install_uses_twenty_nine_real_checks_including_both_confirmations(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.install_intent()
    checks: list[tuple[TransactionRecordV2, Role | None]] = []
    original = host.backend._check

    def counted(
        current: TransactionRecordV2,
        role: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        checks.append((current, role))
        original(
            current,
            role,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )

    monkeypatch.setattr(host.backend, "_check", counted)
    await host.backend.install(record, "target", host.data["target"])

    # Role and writer-held preflight guards include controller authentication;
    # both confirmations and every independent post-probe check remain.
    assert len(checks) == 29
    assert checks.count((record, None)) == 4
    assert checks.count((record, "target")) == 25
    assert host.backend._disk().digests == (
        record.target.configuration.digest,
        record.target.selector.digest if record.target.selector else None,
        record.target.unit.digest,
    )
    assert host.record() == record and host.commands == []


@pytest.mark.parametrize("host", [("switch", True)], indirect=True)
@pytest.mark.parametrize("fault", ["revision", "slot", "lease", "cgroup"])
async def test_component_entry_rechecks_real_evidence_before_any_effect(
    host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    record = host.install_intent()
    before = host.backend._disk()
    entered: list[_DiskBinding] = []
    synchronizations: list[tuple[int, int, int]] = []
    journal = host.control.directory
    assert journal is host.journal.directory
    journal_info = os.fstat(journal.fd)
    read_barrier = (journal.fd, journal_info.st_dev, journal_info.st_ino)
    injected = False
    file_confirm = ConfigurationFile.confirm
    selector_confirm = SelectorStore.confirm
    file_replace = ConfigurationFile.replace
    selector_replace = SelectorStore.replace
    fsync = os.fsync

    def inject() -> None:
        nonlocal injected
        assert not injected
        entered.append(host.backend._disk())
        if fault == "revision":
            host.move("activating", "start_target")
        elif fault == "slot":
            Path(host.slots["base"].declaration.path).write_bytes(b"changed installation")
        elif fault == "lease":
            lease = host.journal.writer_lease()
            path = lease.directory.path / lease.name
            replacement = path.with_name(path.name + ".replacement")
            replacement.write_bytes(b"")
            replacement.chmod(0o600)
            replacement.replace(path)
        else:
            host.events.write_bytes(b"populated 1\n")
        # Mutating the synthetic evidence is not a backend effect. Observe all
        # real syncs after the mutation, including the delegated low-level call.
        injected = True

    def confirm_file(
        file: ConfigurationFile, digest: str, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        if fault == "revision" and file.path == Path(host.anchor.unit_path):
            inject()
        return file_confirm(file, digest, guard=guard)

    def confirm_selector(
        selector: SelectorStore,
        expected: SelectorSnapshot | None,
        *,
        guard: Callable[[], None],
    ) -> SelectorSnapshot | None:
        if fault == "slot":
            inject()
        return selector_confirm(selector, expected, guard=guard)

    def replace_file(
        file: ConfigurationFile,
        digest: str,
        data: bytes,
        *,
        guard: Callable[[], None],
    ) -> ConfigurationSnapshot:
        if fault == "lease" and file.path == Path(host.anchor.config_path):
            inject()
        return file_replace(file, digest, data, guard=guard)

    def replace_selector(
        selector: SelectorStore,
        expected: SelectorSnapshot | None,
        desired: SelectorValue | None,
        *,
        guard: Callable[[], None],
    ) -> SelectorSnapshot | None:
        if fault == "cgroup":
            inject()
        return selector_replace(selector, expected, desired, guard=guard)

    def synchronized(descriptor: int) -> None:
        if injected:
            info = os.fstat(descriptor)
            synchronizations.append((descriptor, info.st_dev, info.st_ino))
        fsync(descriptor)

    monkeypatch.setattr(ConfigurationFile, "confirm", confirm_file)
    monkeypatch.setattr(SelectorStore, "confirm", confirm_selector)
    monkeypatch.setattr(ConfigurationFile, "replace", replace_file)
    monkeypatch.setattr(SelectorStore, "replace", replace_selector)
    monkeypatch.setattr(os, "fsync", synchronized)
    errors: dict[str, type[Exception]] = {
        "revision": TransactionError,
        "slot": ManagedDeploymentError,
        "lease": TransactionError,
        "cgroup": ObservationError,
    }
    with pytest.raises(errors[fault]):
        if fault in {"revision", "slot"}:
            with host.journal.writer_lease() as lease:
                host.backend._confirm(record, "base", lease, host.probe.snapshot.unit.cgroup)
        else:
            await host.backend.install(record, "target", host.data["target"])

    assert injected and len(entered) == 1
    assert host.backend._disk() == entered[0]
    if fault == "cgroup":
        # The configuration was genuinely installed before the selector began;
        # the rejected selector must preserve exactly that legal prefix.
        assert entered[0].configuration.data == host.data["target"].configuration
        assert entered[0].selector == before.selector and entered[0].unit == before.unit
    else:
        assert entered[0] == before
    # JournalControl.read_current retains its original durability read barrier.
    # Require exactly that fd/identity, not a component or parent-directory sync.
    assert synchronizations == [read_barrier] and host.commands == []
