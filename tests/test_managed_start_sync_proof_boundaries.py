"""Start confirmation lends content only to its adjacent None probe preflight."""

from __future__ import annotations

import asyncio
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from test_managed_backend import Host
from test_managed_backend_package_batch import observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import observe_python_reads
from test_managed_backend_python_proof import shared_host as shared_host
from test_managed_start_confirmation_batch import prepare_start, rewrite_with_restored_mtime
from test_managed_sync_proof_boundaries import replace_leaf
from test_managed_sync_proof_continuity import ReadSnapshot, snapshot

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

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real start file boundaries")


def start_roles(role: Role) -> list[Role | None]:
    return [None, None] + [role] * 8 + [None, None] + [role, role]


@dataclass
class StartHandoff:
    entered: list[int] = field(default_factory=list)
    borrowed: list[_FileProofs] = field(default_factory=list)
    outputs: list[_FileProofs] = field(default_factory=list)
    leases: list[FixedLock] = field(default_factory=list)
    read_entries: list[int] = field(default_factory=list)

    def discarded(self) -> None:
        assert all(batch.python is None and batch.packages is None for batch in self.borrowed)
        assert all(batch.python is None and batch.packages is None for batch in self.outputs)
        assert all(lease.fd is None for lease in self.leases)


def at_start_probe_handoff(
    host: Host, monkeypatch: pytest.MonkeyPatch, callback: Callable[[], None]
) -> StartHandoff:
    """Inject only after the real start confirmation fully returned with proofs."""
    result = StartHandoff()
    original_confirm, original_probe = host.backend._confirm, host.backend._probe
    original_read = host.probe.read_managed

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        disk = original_confirm(current, selected, lease, cgroup, proofs=proofs)
        assert host.probe.count == 1 and not result.entered
        assert proofs is not None and proofs.python is not None and proofs.packages is not None
        result.entered.append(host.probe.count)
        result.borrowed.append(proofs)
        result.leases.append(lease)
        lease.check()
        callback()
        return disk

    async def probe(
        current: TransactionRecordV2,
        *,
        preflight: Callable[[], None] | None = None,
        postprocess: Callable[[ManagedProbeResult], Role | None] | None = None,
        postproofs: _FileProofs | None = None,
    ) -> ManagedProbeResult:
        if result.entered:
            assert host.probe.count == 1 and not result.outputs
            assert preflight is not None and postproofs is not None
            assert all(postproofs is not batch for batch in result.borrowed)
            result.outputs.append(postproofs)
        return await original_probe(
            current, preflight=preflight, postprocess=postprocess, postproofs=postproofs
        )

    async def read(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        if result.entered:
            assert host.probe.count == 1 and not result.read_entries
            assert len(result.borrowed) == len(result.outputs) == 1
            assert result.borrowed[0].python is None and result.borrowed[0].packages is None
            assert result.outputs[0].python is None and result.outputs[0].packages is None
            result.read_entries.append(host.probe.count)
        return await original_read(journal, current, anchor, controller, slots)

    monkeypatch.setattr(host.backend, "_confirm", confirm)
    monkeypatch.setattr(host.backend, "_probe", probe)
    monkeypatch.setattr(host.probe, "read_managed", read)
    return result


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
async def test_start_probe_retains_original_file_and_effect_guard_order(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, fault: str, code: str
) -> None:
    host = shared_host
    record = current = await prepare_start(host, "target")
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
            current = host.move("verifying")
        elif fault == "lease":
            lock.rename(retained_lock)
            lock.write_bytes(b"")
            lock.chmod(0o600)
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

    handoff = at_start_probe_handoff(host, monkeypatch, change)
    expected = (
        TimeoutError
        if fault == "deadline"
        else TransactionError
        if fault in {"revision", "lease"}
        else ManagedDeploymentError
    )
    try:
        with pytest.raises(expected, match=f"^{code}$"):
            await host.backend.start(record, "target")
    finally:
        metadata.chmod(mode)
        for parent, retained in moved.items():
            parent.unlink()
            retained.rename(parent)
        if retained_lock.exists():
            lock.unlink()
            retained_lock.rename(lock)

    assert handoff.entered == [1] and len(checkpoint) == 1
    disk, state, before_checks, before_python = checkpoint[0]
    assert before_checks == 10
    # None preflight/post-check never gain selected, lease or deadline authority.
    # These cases must wait for the original writer-held role guard #13.
    at_role_guard = fault in {
        "selected",
        "selected-replaced",
        "selected-parent",
        "lease",
        "deadline",
    }
    extra_checks = 3 if at_role_guard else 1
    expected_roles = start_roles("target")[:10] + [None]
    if at_role_guard:
        expected_roles += [None, "target"]
    assert len(checks) - before_checks == extra_checks
    assert [selected for _, selected in checks] == expected_roles
    assert all(current is record for current, _selected in checks)
    assert host.probe.count == 1 + int(at_role_guard)
    assert handoff.read_entries == ([1] if at_role_guard else [])
    assert host.commands == commands and host.transport.calls == [] and host.writer is None
    same_record = host.record() == current
    assert same_record and current.deadline == record.deadline
    assert host.backend._disk().digests == disk
    handoff.discarded()
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
        assert reads.leaves[selected].bytes - before_payload[selected] == reads.sizes[selected]
    elif fault == "python":
        assert (
            python.bytes - before_python
            == host.backend.controller_slot.resolved_python.identity.size
        )
    if fault == "both":
        assert reads.leaves[selected].bytes == before_payload[selected]
    if at_role_guard:
        # The second read's post-check remains a complete independent read.
        assert (
            reads.leaves[controller].bytes - before_payload[controller] == reads.sizes[controller]
        )
        assert (
            python.bytes - before_python
            == host.backend.controller_slot.resolved_python.identity.size
        )


@pytest.mark.parametrize("change", ["inode", "mode"])
async def test_start_probe_fully_rehashes_legal_changed_package_identity(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, change: Literal["inode", "mode"]
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    path = Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    mode = stat.S_IMODE(path.stat().st_mode)
    state_path = host.journal.directory.path / "state.json"
    state, disk, commands = state_path.read_bytes(), host.backend._disk(), list(host.commands)
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    before: list[ReadSnapshot] = []
    next_guard: list[ReadSnapshot] = []
    original_check = host.backend._check

    def changed() -> None:
        if change == "inode":
            replace_leaf(path, changed=False)
        else:
            path.chmod(mode ^ stat.S_IXUSR)
            assert stat.S_IMODE(path.stat().st_mode) != mode
        before.append(snapshot(reads, python, checks))

    handoff = at_start_probe_handoff(host, monkeypatch, changed)

    def checked(
        current: TransactionRecordV2,
        selected: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        original_check(
            current,
            selected,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )
        if handoff.entered and not next_guard:
            assert selected is None and len(checks) == 11
            next_guard.append(snapshot(reads, python, checks))

    monkeypatch.setattr(host.backend, "_check", checked)

    assert await host.backend.start(record, "target") is None

    assert len(before) == len(next_guard) == 1
    start, end = before[0], next_guard[0]
    assert end.checks - start.checks == 1
    assert end.leaves[path][0] - start.leaves[path][0] == reads.sizes[path]
    assert end.leaves[path][1] - start.leaves[path][1] == 1
    assert end.leaves[path][2] - start.leaves[path][2] >= 2
    assert end.leaves[path][3] - start.leaves[path][3] == 1
    assert reads.leaves[path].bytes - start.leaves[path][0] == 2 * reads.sizes[path]
    assert [selected for _, selected in checks] == start_roles("target")
    assert host.probe.count == 2 and handoff.read_entries == [1]
    assert host.commands == [*commands, "start"] and host.transport.calls == []
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    handoff.discarded()
    assert host.writer is not None
    host.writer.check()


async def test_start_probe_preserves_later_cgroup_effect_guard(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    state_path = host.journal.directory.path / "state.json"
    state, disk, commands = state_path.read_bytes(), host.backend._disk(), list(host.commands)
    checks = observe_checks(host, monkeypatch)

    def populate() -> None:
        host.events.write_bytes(b"populated 1\n")

    handoff = at_start_probe_handoff(host, monkeypatch, populate)

    with pytest.raises(ObservationError, match="^cgroup_not_empty$"):
        await host.backend.start(record, "target")

    assert handoff.entered == handoff.read_entries == [1] and host.probe.count == 2
    assert [selected for _, selected in checks] == start_roles("target")[:13]
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    assert host.commands == commands and host.transport.calls == [] and host.writer is None
    handoff.discarded()
    with host.journal.writer_lease() as lease:
        lease.check()


@pytest.mark.parametrize("stage", ["confirm", "preflight"])
@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
async def test_start_probe_discards_proofs_on_same_object_sync_failure(
    shared_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    stage: Literal["confirm", "preflight"],
    cancel: bool,
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    state_path = host.journal.directory.path / "state.json"
    state, disk, commands = state_path.read_bytes(), host.backend._disk(), list(host.commands)
    checks = observe_checks(host, monkeypatch)
    original_check = host.backend._check
    failure = asyncio.CancelledError() if cancel else RuntimeError("sync start handoff failed")

    def checked(
        current: TransactionRecordV2,
        selected: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        assert selected is None and host.probe.count == 1
        original_check(
            current,
            selected,
            verified_python=verified_python,
            verified_packages=verified_packages,
            observed_packages=observed_packages,
        )
        raise failure

    def change() -> None:
        if stage == "confirm":
            raise failure
        monkeypatch.setattr(host.backend, "_check", checked)

    handoff = at_start_probe_handoff(host, monkeypatch, change)
    with pytest.raises(type(failure)) as caught:
        await host.backend.start(record, "target")

    assert caught.value is failure
    assert handoff.entered == [1] and not handoff.read_entries
    assert len(handoff.outputs) == int(stage == "preflight")
    handoff.discarded()
    assert [selected for _, selected in checks] == start_roles("target")[
        : 10 + int(stage == "preflight")
    ]
    assert host.probe.count == 1 and host.commands == commands and host.transport.calls == []
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
async def test_start_discards_second_probe_output_when_its_caller_raises(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    state_path = host.journal.directory.path / "state.json"
    state, disk, commands = state_path.read_bytes(), host.backend._disk(), list(host.commands)
    checks = observe_checks(host, monkeypatch)
    handoff = at_start_probe_handoff(host, monkeypatch, lambda: None)
    original_probe = host.backend._probe
    failure = asyncio.CancelledError() if cancel else RuntimeError("post-probe caller failed")

    async def probe(
        current: TransactionRecordV2,
        *,
        preflight: Callable[[], None] | None = None,
        postprocess: Callable[[ManagedProbeResult], Role | None] | None = None,
        postproofs: _FileProofs | None = None,
    ) -> ManagedProbeResult:
        result = await original_probe(
            current, preflight=preflight, postprocess=postprocess, postproofs=postproofs
        )
        if handoff.entered:
            assert host.probe.count == 2 and len(checks) == 12
            assert len(handoff.outputs) == 1
            assert handoff.outputs[0].python is not None
            assert handoff.outputs[0].packages is not None
            raise failure
        return result

    monkeypatch.setattr(host.backend, "_probe", probe)
    with pytest.raises(type(failure)) as caught:
        await host.backend.start(record, "target")

    assert caught.value is failure and handoff.entered == handoff.read_entries == [1]
    assert [selected for _, selected in checks] == start_roles("target")[:12]
    assert host.probe.count == 2 and host.commands == commands and host.transport.calls == []
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state and host.writer is None
    handoff.discarded()
    with host.journal.writer_lease() as lease:
        lease.check()


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
async def test_start_confirmation_discards_inflight_proofs_when_guard_raises(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    state_path = host.journal.directory.path / "state.json"
    state, disk, commands = state_path.read_bytes(), host.backend._disk(), list(host.commands)
    checks = observe_checks(host, monkeypatch)
    original_confirm, original_guard = host.backend._confirm, host.backend._guard
    failure = asyncio.CancelledError() if cancel else RuntimeError("confirm guard failed")
    batches: list[_FileProofs] = []
    leases: list[FixedLock] = []

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        assert proofs is not None and proofs.python is not None and proofs.packages is not None
        assert len(checks) == 3 and host.probe.count == 1
        batches.append(proofs)
        leases.append(lease)

        def guarded(*args: Any, **kwargs: Any) -> None:
            original_guard(*args, **kwargs)
            raise failure

        monkeypatch.setattr(host.backend, "_guard", guarded)
        return original_confirm(current, selected, lease, cgroup, proofs=proofs)

    monkeypatch.setattr(host.backend, "_confirm", confirm)
    with pytest.raises(type(failure)) as caught:
        await host.backend.start(record, "target")

    assert caught.value is failure and len(batches) == len(leases) == 1
    assert batches[0].python is None and batches[0].packages is None and leases[0].fd is None
    assert [selected for _, selected in checks] == start_roles("target")[:4]
    assert host.probe.count == 1 and host.commands == commands and host.transport.calls == []
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()


@pytest.mark.parametrize("fault", ["controller", "python", "cancel"])
async def test_start_second_real_await_requires_fresh_content_and_cleans_cancellation(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    state_path = host.journal.directory.path / "state.json"
    state, disk, commands = state_path.read_bytes(), host.backend._disk(), list(host.commands)
    path = (
        Path(host.backend.controller_slot.resolved_python.path)
        if fault == "python"
        else Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    )
    contents, identity = path.read_bytes(), path.stat()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    handoff = at_start_probe_handoff(host, monkeypatch, lambda: None)
    original_read = host.probe.read_managed
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellations: list[asyncio.CancelledError] = []

    async def read(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        result = await original_read(journal, current, anchor, controller, slots)
        if host.probe.count == 2:
            assert handoff.entered == handoff.read_entries == [1] and not entered.is_set()
            assert all(
                batch.python is None and batch.packages is None for batch in handoff.borrowed
            )
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError as error:
                cancellations.append(error)
                raise
            finally:
                settled.set()
        return result

    monkeypatch.setattr(host.probe, "read_managed", read)
    task = asyncio.create_task(host.backend.start(record, "target"))
    before_payload = before_python = 0
    try:
        async with asyncio.timeout(host.backend.operation_timeout):
            await entered.wait()
            assert not task.done() and not settled.is_set()
            assert [selected for _, selected in checks] == start_roles("target")[:11]
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

    assert task.done() and settled.is_set() and host.probe.count == 2
    assert [selected for _, selected in checks] == start_roles("target")[
        : 11 if fault == "cancel" else 12
    ]
    same_record, same_disk = host.record() == record, host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    assert host.commands == commands and host.transport.calls == [] and host.writer is None
    handoff.discarded()
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
