"""Real selector confirmations retain their effect guards without duplicate hashes."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

import pytest
from test_managed_backend import Host
from test_managed_backend import host as host

from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionError
from muli.platforms.installation import FileFingerprint
from muli.platforms.managed_deployment import SelectorStore
from muli.platforms.managed_launcher import PackageProofs
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed selector guards")


async def test_target_confirmation_uses_seven_real_checks(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    before = host.backend._disk()
    assert before.selector is not None
    checks: list[tuple[TransactionRecordV2, Role | None]] = []
    original = host.backend._check

    def checked(
        current: TransactionRecordV2,
        role: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        original(
            current,
            role,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )
        checks.append((current, role))

    monkeypatch.setattr(host.backend, "_check", checked)
    with host.journal.writer_lease() as lease:
        confirmed = host.backend._confirm(record, "target", lease, host.probe.snapshot.unit.cgroup)

    assert confirmed == before == host.backend._disk()
    assert checks == [(record, "target")] * 7
    assert host.record() == record
    assert host.commands == []


@pytest.mark.parametrize("host", [("adopt", False)], indirect=True)
async def test_absent_selector_still_runs_real_guard_before_directory_sync(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = host.install_intent()
    before = host.backend._disk()
    assert before.selector is None
    successful_guards = 0
    selector_syncs: list[tuple[int, int]] = []
    fsync = os.fsync

    with (
        host.journal.writer_lease() as lease,
        SelectorStore(host.journal.directory.path) as selector,
    ):
        assert selector.read() is None

        def guard() -> None:
            nonlocal successful_guards
            host.backend._guard(record, "base", lease, host.probe.snapshot.unit.cgroup)
            successful_guards += 1

        def synchronized(descriptor: int) -> None:
            if descriptor == selector.file.fd:
                selector_syncs.append((descriptor, successful_guards))
            fsync(descriptor)

        monkeypatch.setattr(os, "fsync", synchronized)
        assert selector.confirm(None, guard=guard) is None
        assert selector_syncs == [(selector.file.fd, 1)]

    assert successful_guards == 1
    assert host.backend._disk() == before
    assert host.record() == record
    assert host.commands == []


@pytest.mark.parametrize("fault", ["revision", "lease"])
async def test_selector_file_entry_rejects_real_drift_before_any_component_sync(
    host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    before = host.backend._disk()
    assert before.selector is not None
    with SelectorStore(host.journal.directory.path) as selector:
        selector_path = selector.file.path
    journal = host.control.directory
    assert journal is host.journal.directory
    journal_info = os.fstat(journal.fd)
    read_barrier = (journal.fd, journal_info.st_dev, journal_info.st_ino)
    synchronizations: list[tuple[int, int, int]] = []
    entries: list[ConfigurationSnapshot] = []
    injected = False
    original = ConfigurationFile.confirm
    fsync = os.fsync

    def confirm(
        file: ConfigurationFile, digest: str, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        nonlocal injected
        if file.path == selector_path:
            assert not injected
            entries.append(file.read())
            if fault == "revision":
                host.move("activating", "start_target")
            else:
                writer = host.journal.writer_lease()
                path = writer.directory.path / writer.name
                replacement = path.with_name(path.name + ".replacement")
                replacement.write_bytes(b"")
                replacement.chmod(0o600)
                replacement.replace(path)
            # Injected evidence changes are not component publication effects.
            # From this exact low-level entry onward, delegate every real guard.
            injected = True
        return original(file, digest, guard=guard)

    def synchronized(descriptor: int) -> None:
        if injected:
            info = os.fstat(descriptor)
            synchronizations.append((descriptor, info.st_dev, info.st_ino))
        fsync(descriptor)

    monkeypatch.setattr(ConfigurationFile, "confirm", confirm)
    monkeypatch.setattr(os, "fsync", synchronized)
    code = "stale_revision" if fault == "revision" else "lock_not_owned"
    with host.journal.writer_lease() as lease:
        with pytest.raises(TransactionError, match=f"^{code}$"):
            host.backend._confirm(record, "target", lease, host.probe.snapshot.unit.cgroup)

    assert injected and len(entries) == 1
    assert entries[0].identity == before.selector.identity
    assert entries[0].data == before.selector.data
    assert host.backend._disk() == before
    # Only the original journal-control fd may perform its durability read
    # barrier. A selector fd on the same directory inode is still an effect.
    assert synchronizations == [read_barrier]
    assert host.commands == []
