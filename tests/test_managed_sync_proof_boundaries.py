"""Adjacent byte reuse never grants a cached effect or crosses an actual await."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from test_managed_backend import Host
from test_managed_backend_package_batch import observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import observe_python_reads
from test_managed_backend_python_proof import shared_host as shared_host
from test_managed_start_confirmation_batch import rewrite_with_restored_mtime
from test_managed_sync_proof_continuity import (
    ReadSnapshot,
    assert_span_walks,
    expected_confirmation,
    prepare_stopped,
    snapshot,
)

from muli.managed_controller import BindingConfirmation, ManagedServiceObservation
from muli.managed_journal import RoleSnapshots
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import FixedLock, Role, TransactionError, TransactionJournal
from muli.platforms.installation import FileFingerprint
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_backend import _DiskBinding, _FileProofs
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    ManagedDeploymentError,
    SlotIdentity,
)
from muli.platforms.managed_launcher import PackageProofs
from muli.platforms.managed_probe import ManagedProbeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real synchronous boundaries")

type Window = Literal[
    "observe",
    "observe-probe",
    "install-preflight",
    "install-files",
    "install-final",
    "install-probe",
]

_HANDOFF_PROBES: dict[Window, int] = {
    "observe": 2,
    "observe-probe": 2,
    "install-preflight": 1,
    "install-files": 2,
    "install-final": 3,
    "install-probe": 2,
}


async def prepare_window(host: Host, window: Window) -> TransactionRecordV2:
    return await prepare_stopped(host, "target", installed=window in {"observe", "observe-probe"})


async def dispatch(
    host: Host, record: TransactionRecordV2, window: Window
) -> BindingConfirmation | ManagedServiceObservation:
    if window in {"observe", "observe-probe"}:
        return await host.backend.observe(record)
    return await host.backend.install(record, "target", host.data["target"])


def at_handoff(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    window: Window,
    callback: Callable[[], None],
) -> tuple[list[int], list[_FileProofs]]:
    """Inject after a real post-check or the actual final file confirmation."""
    original = host.backend._probe
    entered: list[int] = []
    batches: list[_FileProofs] = []

    if window == "observe-probe":
        original_confirm = host.backend._confirm

        def confirm(
            current: TransactionRecordV2,
            selected: Role,
            lease: FixedLock,
            cgroup: str,
            *,
            proofs: _FileProofs | None = None,
        ) -> _DiskBinding:
            result = original_confirm(current, selected, lease, cgroup, proofs=proofs)
            assert host.probe.count == 2 and not entered
            assert proofs is not None and proofs.python is not None and proofs.packages is not None
            entered.append(host.probe.count)
            batches.append(proofs)
            callback()
            return result

        monkeypatch.setattr(host.backend, "_confirm", confirm)
        return entered, batches

    if window == "install-probe":
        original_files = host.backend._install_files

        def install_files(
            current: TransactionRecordV2,
            selected: Role,
            data: RoleSnapshots,
            lease: FixedLock,
            cgroup: str,
            *,
            proofs: _FileProofs | None = None,
        ) -> None:
            original_files(current, selected, data, lease, cgroup, proofs=proofs)
            assert host.probe.count == 2 and not entered
            assert proofs is not None and proofs.python is not None and proofs.packages is not None
            entered.append(host.probe.count)
            batches.append(proofs)
            callback()

        monkeypatch.setattr(host.backend, "_install_files", install_files)
        return entered, batches

    async def probe(
        current: TransactionRecordV2,
        *,
        preflight: Callable[[], None] | None = None,
        postprocess: Callable[[ManagedProbeResult], Role | None] | None = None,
        postproofs: _FileProofs | None = None,
    ) -> ManagedProbeResult:
        result = await original(
            current, preflight=preflight, postprocess=postprocess, postproofs=postproofs
        )
        target = _HANDOFF_PROBES[window]
        if host.probe.count == target and not entered:
            entered.append(target)
            if postproofs is not None:
                assert postproofs.python is not None and postproofs.packages is not None
                batches.append(postproofs)
            callback()
        return result

    monkeypatch.setattr(host.backend, "_probe", probe)
    return entered, batches


def replace_leaf(path: Path, *, changed: bool) -> None:
    contents, before = path.read_bytes(), path.stat()
    replacement = path.with_name(path.name + ".replacement")
    replacement.write_bytes(bytes([contents[0] ^ 0xFF]) + contents[1:] if changed else contents)
    replacement.chmod(stat.S_IMODE(before.st_mode))
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    after = path.stat()
    assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


@pytest.mark.parametrize("condition", ["nonstopped", "busy", "cgroup", "disk", "unit"])
async def test_observe_skipped_or_unconfirmed_paths_keep_original_probe_boundaries(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    host = shared_host
    record = await prepare_window(host, "observe-probe")
    if condition == "nonstopped":
        host.probe.snapshot = replace(
            host.probe.snapshot,
            unit=replace(host.probe.snapshot.unit, active_state="activating", sub_state="start"),
        )
    elif condition == "busy":
        host.writer = host.journal.writer_lease().__enter__()
    elif condition == "cgroup":
        host.events.write_bytes(b"populated 1\n")
    elif condition == "disk":
        Path(host.anchor.config_path).write_bytes(b"foreign binding bytes")
    else:
        host.probe.loaded_unit = b"different loaded unit\n"

    state_path = host.journal.directory.path / "state.json"
    state, disk = state_path.read_bytes(), host.backend._disk()
    commands, events = list(host.commands), host.events.read_bytes()
    checks = observe_checks(host, monkeypatch)
    entered, batches = at_handoff(host, monkeypatch, "observe-probe", lambda: None)
    original_probe = host.backend._probe
    preflights: list[bool] = []
    postproofs_seen: list[_FileProofs] = []

    async def probe(
        current: TransactionRecordV2,
        *,
        preflight: Callable[[], None] | None = None,
        postprocess: Callable[[ManagedProbeResult], Role | None] | None = None,
        postproofs: _FileProofs | None = None,
    ) -> ManagedProbeResult:
        preflights.append(preflight is not None)
        if postproofs is not None:
            postproofs_seen.append(postproofs)
        return await original_probe(
            current, preflight=preflight, postprocess=postprocess, postproofs=postproofs
        )

    monkeypatch.setattr(host.backend, "_probe", probe)
    try:
        observation = await host.backend.observe(record)
        if condition == "busy":
            assert host.writer is not None
            host.writer.check()
    finally:
        host.release_writer()

    assert observation.confirmation is None and observation.writer_free == (condition != "busy")
    assert (
        observation.service
        == {
            "nonstopped": "starting",
            "busy": "stopping",
            "cgroup": "stopping",
            "disk": "stopped",
            "unit": "stopped",
        }[condition]
    )
    if condition == "unit":
        # A completed confirmation may lend content even when the manager has
        # not loaded this unit. The original final probe withholds confirmation.
        assert entered == [2] and len(batches) == 1
        assert preflights == [False, False, True] and host.probe.count == 3
        roles: list[Role | None] = [None] * 4 + ["target"] * 7 + [None] * 2
    else:
        assert entered == batches == []
        assert preflights == [False, False] and host.probe.count == 2
        roles = [None] * 4
    assert [selected for _, selected in checks] == roles
    assert all(current is record for current, _ in checks)
    assert all(batch.python is None and batch.packages is None for batch in postproofs_seen)
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    assert host.record().deadline == record.deadline and host.events.read_bytes() == events
    assert host.commands == commands and host.transport.calls == [] and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize("fault", ["running", "busy", "error", "disk"])
async def test_install_initial_proof_is_discarded_on_early_exit(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    host = shared_host
    record = await prepare_window(host, "install-preflight")
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    failure = RuntimeError("post_probe_failure")
    disk: list[tuple[str, str | None, str]] = []
    if fault == "running":
        host.probe.snapshot = replace(
            host.probe.snapshot,
            unit=replace(host.probe.snapshot.unit, active_state="active", sub_state="running"),
        )

    def change() -> None:
        if fault == "busy":
            # A distinct real flock owns the original lease before install
            # attempts acquisition. Its ownership must survive the failure.
            host.writer = host.journal.writer_lease().__enter__()
        elif fault == "disk":
            Path(host.anchor.config_path).write_bytes(b"foreign binding bytes")
        disk.append(host.backend._disk().digests)
        if fault == "error":
            raise failure

    entered, batches = at_handoff(host, monkeypatch, "install-preflight", change)
    errors: dict[str, type[Exception]] = {
        "running": ObservationError,
        "busy": TransactionError,
        "error": RuntimeError,
        "disk": ObservationError,
    }
    code = {
        "running": "transaction_service_not_stopped",
        "busy": "lock_busy",
        "error": "post_probe_failure",
        "disk": "foreign_managed_binding",
    }[fault]
    try:
        with pytest.raises(errors[fault], match=f"^{code}$") as caught:
            await dispatch(host, record, "install-preflight")
        if fault == "error":
            assert caught.value is failure
        if fault == "busy":
            assert host.writer is not None
            host.writer.check()
    finally:
        host.release_writer()

    assert entered == [1] and len(batches) == 1 and len(disk) == 1
    assert batches[0].python is None and batches[0].packages is None
    assert host.probe.count == 1 and host.backend._disk().digests == disk[0]
    assert host.record() == record and state_path.read_bytes() == state
    assert host.commands == [] and host.transport.calls == [] and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize("shared_host", ["different-binary"], indirect=True)
async def test_install_initial_handoff_still_reads_a_distinct_selected_python(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = shared_host
    record = await prepare_window(host, "install-preflight")
    controller_python = host.backend.controller_slot.resolved_python
    selected_python = host.slots["target"].resolved_python
    assert controller_python.digest == selected_python.digest
    assert controller_python != selected_python
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    before: list[ReadSnapshot] = []
    after: list[ReadSnapshot] = []
    original = host.backend._check
    entered, batches = at_handoff(
        host,
        monkeypatch,
        "install-preflight",
        lambda: before.append(snapshot(reads, python, checks)),
    )

    def checked(
        current: TransactionRecordV2,
        selected: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        original(
            current,
            selected,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )
        if entered and not after:
            after.append(snapshot(reads, python, checks))

    monkeypatch.setattr(host.backend, "_check", checked)

    confirmation = await host.backend.install(record, "target", host.data["target"])

    assert confirmation == expected_confirmation(record, "target")
    assert len(before) == len(after) == 1 and len(checks) == 29
    assert_span_walks(host, "target", reads, python, before[0], after[0], 1)
    assert after[0].python_bytes - before[0].python_bytes == selected_python.identity.size
    controller_root = Path(host.backend.controller_slot.package.root)
    selected_root = Path(host.slots["target"].package.root)
    for path, size in reads.sizes.items():
        expected = size if path.is_relative_to(selected_root) else 0
        assert after[0].leaves[path][0] - before[0].leaves[path][0] == expected
        if path.is_relative_to(controller_root):
            assert expected == 0
    assert entered == [1] and len(batches) == 1
    assert batches[0].python is None and batches[0].packages is None
    assert host.probe.count == 3 and host.commands == [] and host.transport.calls == []
    assert host.record() == record and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize(
    "window",
    [
        "observe",
        "observe-probe",
        "install-preflight",
        "install-files",
        "install-final",
        "install-probe",
    ],
)
@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("controller", "anchor_unavailable"),
        ("selected", "managed_slot_changed"),
        ("both", "anchor_unavailable"),
        ("python", "anchor_unavailable"),
        ("python-replaced", "anchor_unavailable"),
        ("controller-replaced", "anchor_unavailable"),
        ("selected-replaced", "managed_slot_changed"),
        ("anchor", "anchor_unavailable"),
        ("slot", "managed_slot_unavailable"),
        ("permission", "managed_slot_unavailable"),
        ("controller-parent", "anchor_unavailable"),
        ("selected-parent", "managed_slot_changed"),
        ("revision", "stale_revision"),
        ("lease", "lock_not_owned"),
        ("deadline", ""),
        ("deadline-controller", "anchor_unavailable"),
    ],
)
async def test_handoff_retains_fresh_files_metadata_and_effect_authorization(
    shared_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    window: Window,
    fault: str,
    code: str,
) -> None:
    host = shared_host
    record = current = await prepare_window(host, window)
    commands = list(host.commands)
    state_path = host.journal.directory.path / "state.json"
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    controller = Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    selected = Path(host.slots["target"].package.root) / "batch_payload/first.bin"
    metadata = host.journal.directory.path / f"slot-{host.slots['target'].digest}.json"
    mode = stat.S_IMODE(metadata.stat().st_mode)
    lock = host.journal.writer_directory.path / "lease"
    retained_lock = lock.with_name(lock.name + ".retained")
    moved: dict[Path, Path] = {}
    checkpoint: list[tuple[tuple[str, str | None, str], bytes, int, int]] = []
    before_payload: dict[Path, int] = {}

    def change() -> None:
        nonlocal current
        checkpoint.append(
            (host.backend._disk().digests, state_path.read_bytes(), len(checks), python.bytes)
        )
        before_payload.update({path: leaf.bytes for path, leaf in reads.leaves.items()})
        if fault in {"deadline", "deadline-controller"}:
            host.clock.elapsed = record.deadline.seconds - record.deadline.rollback_reserve
        if fault == "revision":
            current = host.move("activating", "start_target")
        elif fault == "lease":

            def replace_lock() -> None:
                lock.rename(retained_lock)
                lock.write_bytes(b"")
                lock.chmod(0o600)

            if window == "install-preflight":
                # Probe 1 returns before acquisition. Replace only after the
                # real flock belongs to this operation, before its preflight.
                original_enter = FixedLock.__enter__
                replaced: list[FixedLock] = []

                def acquired(lease: FixedLock) -> FixedLock:
                    result = original_enter(lease)
                    if (
                        lease.directory is host.journal.writer_directory
                        and lease.name == "lease"
                        and not replaced
                    ):
                        replaced.append(lease)
                        replace_lock()
                    return result

                monkeypatch.setattr(FixedLock, "__enter__", acquired)
            else:
                replace_lock()
        elif fault == "permission":
            metadata.chmod(0o644)
        elif fault in {"controller-parent", "selected-parent"}:
            parent = (selected if fault == "selected-parent" else controller).parent
            retained = parent.with_name(parent.name + ".retained")
            parent.rename(retained)
            moved[parent] = retained
            parent.symlink_to(retained.name, target_is_directory=True)
        elif fault in {"python-replaced", "controller-replaced", "selected-replaced"}:
            path = (
                Path(host.backend.controller_slot.resolved_python.path)
                if fault == "python-replaced"
                else selected
                if fault == "selected-replaced"
                else controller
            )
            replace_leaf(path, changed=fault != "python-replaced")
        elif fault in {"controller", "selected", "both", "python", "deadline-controller"}:
            paths = (
                [selected, controller]
                if fault == "both"
                else [selected]
                if fault == "selected"
                else [Path(host.backend.controller_slot.resolved_python.path)]
                if fault == "python"
                else [controller]
            )
            for path in paths:
                rewrite_with_restored_mtime(path, path.read_bytes(), path.stat())
        elif fault in {"anchor", "slot"}:
            digest = host.anchor.digest if fault == "anchor" else host.slots["target"].digest
            (host.journal.directory.path / f"{fault}-{digest}.json").write_bytes(b"{}")

    entered, batches = at_handoff(host, monkeypatch, window, change)
    expected = (
        TimeoutError
        if fault == "deadline"
        else TransactionError
        if fault in {"revision", "lease"}
        else ManagedDeploymentError
    )
    # Observe has already completed its last effect guard in this new window.
    # Its original final probe authenticates controller/metadata with role=None;
    # do not introduce a later selected-file, lease or deadline authorization.
    after_last_effect = window == "observe-probe" and fault in {
        "selected",
        "selected-replaced",
        "selected-parent",
        "lease",
        "deadline",
    }
    try:
        if after_last_effect:
            result = await dispatch(host, record, window)
            assert isinstance(result, ManagedServiceObservation)
            assert result.confirmation == expected_confirmation(record, "target")
            assert result.service == "stopped" and result.writer_free
        else:
            with pytest.raises(expected, match=f"^{code}$"):
                await dispatch(host, record, window)
    finally:
        metadata.chmod(mode)
        for parent, retained in moved.items():
            parent.unlink()
            retained.rename(parent)
        if retained_lock.exists():
            lock.unlink()
            retained_lock.rename(lock)

    assert entered == [_HANDOFF_PROBES[window]] and len(checkpoint) == 1
    disk, state, before_checks, before_python = checkpoint[0]
    # Install's file-to-probe handoff preserves the original role=None preflight.
    # Its selected/effect authorization belongs to the later final confirmation,
    # after the third probe and its independent role=None post-check.
    final_confirm_failure = window == "install-probe" and fault in {
        "selected",
        "selected-replaced",
        "selected-parent",
        "lease",
        "deadline",
    }
    extra_checks = 0 if window == "observe" and fault == "lease" else 1
    if final_confirm_failure:
        extra_checks = 3
    elif after_last_effect:
        extra_checks = 2
    assert len(checks) - before_checks == extra_checks
    assert host.probe.count == entered[0] + int(final_confirm_failure or after_last_effect)
    if window == "observe-probe":
        assert before_checks == 11
        assert [selected for _, selected in checks] == (
            [None] * 4 + ["target"] * 7 + [None] * extra_checks
        )
        assert reads.leaves[selected].bytes == before_payload[selected]
    assert host.commands == commands and host.transport.calls == [] and host.writer is None
    same_record = host.record() == current
    assert same_record and current.deadline == record.deadline
    assert host.backend._disk().digests == disk
    for batch in batches:
        assert batch.python is None and batch.packages is None
    if fault != "revision":
        assert state_path.read_bytes() == state
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()
    if fault in {"controller", "controller-replaced", "both", "deadline-controller"}:
        assert (
            reads.leaves[controller].bytes - before_payload[controller] == reads.sizes[controller]
        )
    elif fault in {"selected", "selected-replaced"}:
        copies = 0 if after_last_effect else 1
        assert (
            reads.leaves[selected].bytes - before_payload[selected]
            == reads.sizes[selected] * copies
        )
    elif fault == "python":
        assert (
            python.bytes - before_python
            == host.backend.controller_slot.resolved_python.identity.size
        )
    if fault == "both":
        # Controller failure still wins before any selected payload read.
        assert reads.leaves[selected].bytes == before_payload[selected]
    if after_last_effect:
        # These old successful paths still independently authenticate content
        # after the third real manager read; only its sync preflight may borrow.
        assert (
            reads.leaves[controller].bytes - before_payload[controller] == reads.sizes[controller]
        )
        assert (
            python.bytes - before_python
            == host.backend.controller_slot.resolved_python.identity.size
        )


@pytest.mark.parametrize("window", ["install-probe", "observe-probe"])
@pytest.mark.parametrize("stage", ["files", "preflight"])
@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
async def test_file_proof_is_discarded_on_sync_handoff_failure(
    shared_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    window: Window,
    stage: Literal["files", "preflight"],
    cancel: bool,
) -> None:
    host = shared_host
    record = await prepare_window(host, window)
    state_path = host.journal.directory.path / "state.json"
    state, commands = state_path.read_bytes(), list(host.commands)
    checks = observe_checks(host, monkeypatch)
    original_check = host.backend._check
    failure = asyncio.CancelledError() if cancel else RuntimeError("sync handoff failed")
    disk: list[tuple[str, str | None, str]] = []

    def checked(
        current: TransactionRecordV2,
        selected: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        assert selected is None and host.probe.count == 2
        original_check(
            current,
            selected,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )
        # Authentication really succeeded; the caller must still revoke its
        # borrowed content if the synchronous preflight now fails or cancels.
        raise failure

    def change() -> None:
        disk.append(host.backend._disk().digests)
        if stage == "files":
            # The real final file confirmation populated this batch, but its
            # synchronous caller has not yet reached the probe preflight.
            raise failure
        monkeypatch.setattr(host.backend, "_check", checked)

    entered, batches = at_handoff(host, monkeypatch, window, change)
    with pytest.raises(type(failure)) as caught:
        await dispatch(host, record, window)

    assert caught.value is failure
    assert entered == [2] and len(batches) == len(disk) == 1
    assert batches[0].python is None and batches[0].packages is None
    roles: list[Role | None] = (
        [None] * 4 + ["target"] * 7
        if window == "observe-probe"
        else ["target", None, "target", None] + ["target"] * 16
    )
    assert [selected for _, selected in checks] == roles + ([None] if stage == "preflight" else [])
    assert host.probe.count == 2 and host.backend._disk().digests == disk[0]
    same_record = host.record() == record
    assert same_record and state_path.read_bytes() == state
    assert host.record().deadline == record.deadline
    assert host.commands == commands and host.transport.calls == [] and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize("window", ["install-probe", "observe-probe"])
async def test_file_probe_rehashes_controller_leaf_after_mode_change(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    host = shared_host
    record = await prepare_window(host, window)
    path = Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    mode = stat.S_IMODE(path.stat().st_mode)
    state_path = host.journal.directory.path / "state.json"
    state, commands = state_path.read_bytes(), list(host.commands)
    reads = observe_payloads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    before: list[int] = []

    def change() -> None:
        before.append(reads.leaves[path].bytes)
        path.chmod(mode ^ stat.S_IXUSR)
        assert stat.S_IMODE(path.stat().st_mode) != mode

    entered, batches = at_handoff(host, monkeypatch, window, change)

    result = await dispatch(host, record, window)

    confirmation = result.confirmation if isinstance(result, ManagedServiceObservation) else result
    assert confirmation == expected_confirmation(record, "target")
    assert entered == [2] and len(before) == len(batches) == 1
    assert batches[0].python is None and batches[0].packages is None
    assert host.probe.count == 3 and len(checks) == (13 if window == "observe-probe" else 29)
    # Changed leaf mode revokes its synchronous content reuse; the next probe's
    # post-await check independently reads the same bytes once more.
    assert reads.leaves[path].bytes - before[0] == 2 * reads.sizes[path]
    same_record = host.record() == record
    assert same_record and state_path.read_bytes() == state
    assert host.backend._disk().digests == (
        confirmation.configuration,
        confirmation.selector,
        confirmation.unit,
    )
    assert host.commands == commands and host.transport.calls == [] and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize(
    "window",
    [
        "observe",
        "observe-probe",
        "install-preflight",
        "install-files",
        "install-final",
        "install-probe",
    ],
)
async def test_same_bytes_on_new_package_inode_are_reopened_and_fully_read(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    host = shared_host
    record = await prepare_window(host, window)
    path = Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    reads = observe_payloads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    before: list[tuple[int, int, int, int]] = []
    next_guard: list[tuple[int, int, int, int]] = []
    original = host.backend._check

    def change() -> None:
        replace_leaf(path, changed=False)
        leaf = reads.leaves[path]
        before.append((leaf.bytes, len(leaf.opened), leaf.fd_stats, leaf.path_stats))

    entered, _batches = at_handoff(host, monkeypatch, window, change)

    def checked(
        current: TransactionRecordV2,
        selected: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        original(
            current,
            selected,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )
        if entered and not next_guard:
            leaf = reads.leaves[path]
            next_guard.append((leaf.bytes, len(leaf.opened), leaf.fd_stats, leaf.path_stats))

    monkeypatch.setattr(host.backend, "_check", checked)

    result = await dispatch(host, record, window)

    confirmation = result.confirmation if isinstance(result, ManagedServiceObservation) else result
    assert confirmation == expected_confirmation(record, "target")
    assert len(before) == len(next_guard) == 1
    assert next_guard[0][0] - before[0][0] == reads.sizes[path]
    assert next_guard[0][1] - before[0][1] == 1
    assert next_guard[0][2] - before[0][2] >= 2
    assert next_guard[0][3] - before[0][3] == 1
    assert len(checks) == (13 if window in {"observe", "observe-probe"} else 29)
    assert host.commands == [] and host.transport.calls == [] and host.record() == record
    assert host.writer is None


@pytest.mark.parametrize(
    "window",
    [
        "observe",
        "observe-probe",
        "install-preflight",
        "install-files",
        "install-final",
        "install-probe",
    ],
)
async def test_handoff_rechecks_empty_cgroup_before_file_confirmation(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    host = shared_host
    record = await prepare_window(host, window)
    disk: list[tuple[str, str | None, str]] = []
    checks = observe_checks(host, monkeypatch)

    def populate() -> None:
        disk.append(host.backend._disk().digests)
        host.events.write_bytes(b"populated 1\n")

    entered, batches = at_handoff(host, monkeypatch, window, populate)
    if window == "observe":
        result = await host.backend.observe(record)
        assert result.service == "stopping" and result.confirmation is None
    elif window == "observe-probe":
        result = await host.backend.observe(record)
        # The final confirmation preceded this mutation. The original probe
        # has no later cgroup effect guard and must not gain one through reuse.
        assert result.service == "stopped" and result.writer_free
        assert result.confirmation == expected_confirmation(record, "target")
        assert [selected for _, selected in checks] == [None] * 4 + ["target"] * 7 + [None] * 2
    else:
        with pytest.raises(ObservationError, match="^cgroup_not_empty$"):
            await dispatch(host, record, window)

    assert entered == [_HANDOFF_PROBES[window]]
    expected_probes = entered[0] + int(window in {"install-probe", "observe-probe"})
    assert host.probe.count == expected_probes and host.backend._disk().digests == disk[0]
    assert host.commands == [] and host.transport.calls == [] and host.record() == record
    assert host.writer is None
    assert all(batch.python is None and batch.packages is None for batch in batches)


@pytest.mark.parametrize(
    "window",
    [
        "observe",
        "observe-probe",
        "install-preflight",
        "install-files",
        "install-final",
        "install-probe",
    ],
)
@pytest.mark.parametrize("fault", ["controller", "python", "cancel"])
async def test_next_real_await_discards_bytes_and_rejects_changed_payload(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, window: Window, fault: str
) -> None:
    host = shared_host
    record = await prepare_window(host, window)
    path = (
        Path(host.backend.controller_slot.resolved_python.path)
        if fault == "python"
        else Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    )
    contents, identity = path.read_bytes(), path.stat()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handoffs, batches = at_handoff(host, monkeypatch, window, lambda: None)
    original = host.probe.read_managed
    next_probe = _HANDOFF_PROBES[window] + 1
    cancellations: list[asyncio.CancelledError] = []

    async def probe(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        result = await original(journal, current, anchor, controller, slots)
        if host.probe.count == next_probe:
            assert handoffs and not entered.is_set()
            for batch in batches:
                assert batch.python is None and batch.packages is None
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError as error:
                cancellations.append(error)
                raise
            finally:
                settled.set()
        return result

    async def operation() -> None:
        await dispatch(host, record, window)
        if window == "install-final":
            # Final confirmation returns synchronously; the next public call
            # must independently reauthenticate its own first observation.
            await host.backend.observe(record)

    monkeypatch.setattr(host.probe, "read_managed", probe)
    task = asyncio.create_task(operation())
    before_payload = before_python = 0
    disk: tuple[str, str | None, str] | None = None
    try:
        async with asyncio.timeout(host.backend.operation_timeout):
            await entered.wait()
            assert not task.done() and not settled.is_set()
            disk = host.backend._disk().digests
            before_payload = 0 if fault == "python" else reads.leaves[path].bytes
            before_python = python.bytes
            if fault == "cancel":
                task.cancel()
            elif fault == "controller":
                replace_leaf(path, changed=True)
                release.set()
            else:
                rewrite_with_restored_mtime(path, contents, identity)
                release.set()
            expected = asyncio.CancelledError if fault == "cancel" else ManagedDeploymentError
            with pytest.raises(expected) as caught:
                await task
            if fault == "cancel":
                assert len(cancellations) == 1 and caught.value is cancellations[0]
            else:
                assert str(caught.value) == "anchor_unavailable"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.done() and settled.is_set() and host.probe.count == next_probe
    assert host.backend._disk().digests == disk and host.record() == record
    assert host.commands == [] and host.transport.calls == [] and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()
    if fault == "controller":
        assert reads.leaves[path].bytes - before_payload == reads.sizes[path]
    elif fault == "python":
        assert (
            python.bytes - before_python
            == host.backend.controller_slot.resolved_python.identity.size
        )
    else:
        assert reads.leaves[path].bytes == before_payload and python.bytes == before_python
