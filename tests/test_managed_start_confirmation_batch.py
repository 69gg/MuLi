"""Start may reuse bytes only inside its adjacent writer-held confirmation."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
from test_managed_backend import Host
from test_managed_backend_package_batch import observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import observe_python_reads
from test_managed_backend_python_proof import shared_host as shared_host

from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import FixedLock, Role, TransactionError, TransactionJournal
from muli.platforms import managed_deployment, managed_launcher
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_backend import _DiskBinding, _FileProofs
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    ManagedDeploymentError,
    SlotIdentity,
)
from muli.platforms.managed_probe import ManagedProbeResult
from muli.platforms.transaction_files import ConfigurationFile, ConfigurationSnapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real start file identities")


async def prepare_start(host: Host, role: Role) -> TransactionRecordV2:
    """Use real install/CAS/leases; only the existing manager runner is fake."""
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    if role == "target":
        record = host.move("activating", "start_target")
    else:
        host.move("rolling_back", "stop_target", code="backend_failed")
        record = host.move("rolling_back", "restore_base", code="backend_failed")
        await host.backend.install(record, "base", host.data["base"])
        record = host.move("rolling_back", "start_base", code="backend_failed")
    host.probe.count = 0
    return record


def rewrite_with_restored_mtime(path: Path, contents: bytes, before: os.stat_result) -> None:
    with path.open("r+b") as output:
        output.write(bytes([contents[0] ^ 0xFF]) + contents[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert after.st_ctime_ns != before.st_ctime_ns


def at_start_writer_release(
    host: Host, monkeypatch: pytest.MonkeyPatch, on_release: Callable[[], None]
) -> list[int]:
    """Inject only after the real start writer closes, before its final effect guard."""
    releases: list[int] = []
    original = FixedLock.__exit__

    def released(lease: FixedLock, *args: object) -> None:
        writer = lease.directory is host.journal.writer_directory and lease.fd is not None
        original(lease, *args)
        if writer and not releases:
            assert lease.fd is None
            releases.append(host.probe.count)
            on_release()

    monkeypatch.setattr(FixedLock, "__exit__", released)
    return releases


@pytest.mark.parametrize("role", ["target", "base"])
async def test_start_final_effect_reuses_only_its_adjacent_writer_guard_bytes(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_start(shared_host, role)
    disk, commands = shared_host.backend._disk().digests, list(shared_host.commands)
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    before: dict[Path, tuple[int, int, int, int]] = {}
    scans: dict[Path, int] = {}
    python_before: list[tuple[int, int]] = []

    def released() -> None:
        assert len(checks) == 13
        before.update(
            {
                path: (leaf.bytes, len(leaf.opened), leaf.fd_stats, leaf.path_stats)
                for path, leaf in reads.leaves.items()
            }
        )
        scans.update(reads.scans)
        python_before.append((python.bytes, len(python.opened)))

    releases = at_start_writer_release(shared_host, monkeypatch, released)

    await shared_host.backend.start(record, role)

    assert releases == [2] and len(python_before) == 1
    assert len(checks) == 14 and all(current == record for current, _selected in checks)
    assert sum(selected is None for _current, selected in checks) == 4
    assert sum(selected == role for _current, selected in checks) == 10
    assert shared_host.probe.count == 2 and shared_host.commands == [*commands, "start"]
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.writer is not None
    shared_host.writer.check()

    roots = {
        Path(shared_host.backend.controller_slot.package.root),
        Path(shared_host.slots[role].package.root),
    }
    for path, leaf in reads.leaves.items():
        walks = int(any(path.is_relative_to(root) for root in roots))
        assert len(leaf.opened) - before[path][1] == walks
        assert leaf.fd_stats - before[path][2] >= 2 * walks
        assert leaf.path_stats - before[path][3] == walks
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in leaf.opened)
    for directory, count in reads.scans.items():
        assert count - scans[directory] == int(
            any(directory.is_relative_to(root) for root in roots)
        )
    python_opens = 1 if shared_host.slots[role] == shared_host.backend.controller_slot else 2
    assert len(python.opened) - python_before[0][1] == python_opens
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)

    # All 14 checks, real independent opens and the successful start precede
    # this byte assertion. The old final effect rereads one complete copy.
    actual = {str(path): leaf.bytes - before[path][0] for path, leaf in reads.leaves.items()}
    actual["python"] = python.bytes - python_before[0][0]
    assert actual == {key: 0 for key in actual}


@pytest.mark.parametrize("changed", ["controller", "selected", "both", "python", "replacement"])
async def test_start_final_effect_rejects_file_changes_after_writer_release(
    shared_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    changed: Literal["controller", "selected", "both", "python", "replacement"],
) -> None:
    record = await prepare_start(shared_host, "target")
    disk, commands = shared_host.backend._disk().digests, list(shared_host.commands)
    controller = Path(shared_host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    selected = Path(shared_host.slots["target"].package.root) / "batch_payload/first.bin"
    paths = (
        [Path(shared_host.backend.controller_slot.resolved_python.path)]
        if changed == "python"
        else [selected, controller]
        if changed == "both"
        else [selected]
        if changed == "selected"
        else [controller]
    )
    originals = {path: (path.read_bytes(), path.stat()) for path in paths}
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    before_bytes: dict[Path, int] = {}
    before_python: list[int] = []

    def released() -> None:
        assert len(checks) == 13
        before_bytes.update({path: leaf.bytes for path, leaf in reads.leaves.items()})
        before_python.append(python.bytes)
        if changed == "replacement":
            contents, identity = originals[controller]
            replacement = controller.with_name(controller.name + ".replacement")
            replacement.write_bytes(bytes([contents[0] ^ 0xFF]) + contents[1:])
            replacement.chmod(stat.S_IMODE(identity.st_mode))
            os.utime(replacement, ns=(identity.st_atime_ns, identity.st_mtime_ns))
            replacement.replace(controller)
            after = controller.stat()
            assert (after.st_dev, after.st_ino) != (identity.st_dev, identity.st_ino)
            assert (after.st_size, after.st_mtime_ns) == (identity.st_size, identity.st_mtime_ns)
        else:
            for path in paths:
                contents, identity = originals[path]
                rewrite_with_restored_mtime(path, contents, identity)

    releases = at_start_writer_release(shared_host, monkeypatch, released)
    code = "managed_slot_changed" if changed == "selected" else "anchor_unavailable"

    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        await shared_host.backend.start(record, "target")

    assert releases == [2] and len(before_python) == 1 and len(checks) == 14
    assert shared_host.probe.count == 2 and shared_host.commands == commands
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.writer is None
    with shared_host.journal.writer_lease() as lease:
        lease.check()
    if changed == "python":
        assert python.bytes - before_python[0] >= len(originals[paths[0]][0])
    else:
        damaged = selected if changed == "selected" else controller
        assert reads.leaves[damaged].bytes - before_bytes[damaged] >= reads.sizes[damaged]
        if changed == "both":
            # Two real files changed after the writer closed. Controller
            # authentication must still reject before revisiting selected.
            assert reads.leaves[selected].bytes == before_bytes[selected]


async def test_start_final_effect_rechecks_revision_after_writer_release(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = await prepare_start(shared_host, "target")
    disk, commands = shared_host.backend._disk().digests, list(shared_host.commands)
    checks = observe_checks(shared_host, monkeypatch)
    advanced: list[TransactionRecordV2] = []

    def released() -> None:
        assert len(checks) == 13
        # The actual lease is free: another real writer can enter and advance
        # the persistent journal before command authorization is revisited.
        with shared_host.journal.writer_lease() as lease:
            lease.check()
            advanced.append(shared_host.move("verifying"))

    releases = at_start_writer_release(shared_host, monkeypatch, released)

    with pytest.raises(TransactionError, match="^stale_revision$"):
        await shared_host.backend.start(record, "target")

    assert releases == [2] and len(advanced) == 1 and len(checks) == 14
    assert advanced[0].revision == record.revision + 1
    assert shared_host.record() == advanced[0]
    assert shared_host.backend._disk().digests == disk
    assert shared_host.probe.count == 2 and shared_host.commands == commands
    assert shared_host.writer is None
    with shared_host.journal.writer_lease() as lease:
        lease.check()


@pytest.mark.parametrize("role", ["target", "base"])
async def test_start_final_effect_rechecks_deadline_after_writer_release(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_start(shared_host, role)
    disk, commands = shared_host.backend._disk().digests, list(shared_host.commands)
    checks = observe_checks(shared_host, monkeypatch)

    def released() -> None:
        assert len(checks) == 13
        shared_host.clock.elapsed = record.deadline.seconds - (
            record.deadline.rollback_reserve if role == "target" else 0
        )

    releases = at_start_writer_release(shared_host, monkeypatch, released)
    with pytest.raises(TimeoutError):
        await shared_host.backend.start(record, role)

    assert releases == [2] and len(checks) == 14
    assert shared_host.probe.count == 2 and shared_host.commands == commands
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.writer is None
    with shared_host.journal.writer_lease() as lease:
        lease.check()


@pytest.mark.parametrize("role", ["target", "base"])
async def test_next_start_reads_fresh_bytes_before_its_first_probe(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_start(shared_host, role)
    disk, commands = shared_host.backend._disk().digests, list(shared_host.commands)
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    await shared_host.backend.start(record, role)
    first_bytes = {path: leaf.bytes for path, leaf in reads.leaves.items()}
    first_python, first_probes = python.bytes, shared_host.probe.count
    assert len(checks) == 14 and first_probes == 2
    shared_host.stopped()
    entered: list[int] = []
    entry_bytes: dict[Path, int] = {}
    entry_python: list[int] = []

    def before_read(count: int) -> None:
        if count == first_probes + 1:
            assert not entered and len(checks) == 15
            entered.append(count)
            entry_bytes.update(
                {path: leaf.bytes - first_bytes[path] for path, leaf in reads.leaves.items()}
            )
            entry_python.append(python.bytes - first_python)

    shared_host.probe.before_read = before_read

    await shared_host.backend.start(record, role)

    assert entered == [3] and shared_host.probe.count == 4 and len(checks) == 28
    assert shared_host.commands == [*commands, "start", "start"]
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.writer is not None
    shared_host.writer.check()
    controller = Path(shared_host.backend.controller_slot.package.root)
    for path, leaf in reads.leaves.items():
        assert entry_bytes[path] == (reads.sizes[path] if path.is_relative_to(controller) else 0)
        assert leaf.bytes - first_bytes[path] == first_bytes[path]
    assert entry_python == [shared_host.backend.controller_slot.resolved_python.identity.size]
    assert python.bytes - first_python == first_python


@pytest.mark.parametrize("role", ["target", "base"])
async def test_start_confirmation_reuses_adjacent_guard_bytes_but_keeps_all_guards(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_start(shared_host, role)
    disk = shared_host.backend._disk().digests
    commands = list(shared_host.commands)
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    original = shared_host.backend._confirm
    confirmation_bytes: dict[Path, int] = {}
    confirmation_python: list[int] = []
    confirmation_checks: list[int] = []

    def confirm(
        current: TransactionRecordV2,
        selected: Role,
        lease: FixedLock,
        cgroup: str,
        *,
        proofs: _FileProofs | None = None,
    ) -> _DiskBinding:
        assert not confirmation_checks and selected == role
        lease.check()
        before = {leaf: observed.bytes for leaf, observed in reads.leaves.items()}
        before_python, before_checks = python.bytes, len(checks)
        result = original(current, selected, lease, cgroup, proofs=proofs)
        confirmation_bytes.update(
            {leaf: observed.bytes - before[leaf] for leaf, observed in reads.leaves.items()}
        )
        confirmation_python.append(python.bytes - before_python)
        confirmation_checks.append(len(checks) - before_checks)
        lease.check()
        return result

    monkeypatch.setattr(shared_host.backend, "_confirm", confirm)

    await shared_host.backend.start(record, role)

    assert confirmation_checks == [7]
    assert len(checks) == 14
    assert all(current == record for current, _selected in checks)
    assert sum(selected is None for _current, selected in checks) == 4
    assert sum(selected == role for _current, selected in checks) == 10
    assert shared_host.probe.count == 2
    assert shared_host.commands == [*commands, "start"]
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.probe.snapshot.process is not None
    assert shared_host.probe.snapshot.process.argv == shared_host.arguments(role)
    assert shared_host.writer is not None
    shared_host.writer.check()

    controller = Path(shared_host.backend.controller_slot.package.root)
    selected_root = Path(shared_host.slots[role].package.root)
    for leaf, observed in reads.leaves.items():
        walks = (
            14
            if leaf.is_relative_to(controller)
            else 10
            if leaf.is_relative_to(selected_root)
            else 0
        )
        assert len(observed.opened) == walks, (leaf.name, len(observed.opened), walks)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.fd_stats >= 2 * walks, (leaf.name, observed.fd_stats, walks)
        assert observed.path_stats == walks, (leaf.name, observed.path_stats, walks)
    for directory, count in reads.scans.items():
        walks = (
            14
            if directory.is_relative_to(controller)
            else 10
            if directory.is_relative_to(selected_root)
            else 0
        )
        assert count == walks, (directory.name, count, walks)
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)

    # The real adjacent guard already authenticated both installations. The
    # old start path re-reads one complete payload/Python here; all guards,
    # independent opens, identity checks and successful start above pass first.
    actual = {str(leaf): count for leaf, count in confirmation_bytes.items()}
    actual["python"] = sum(confirmation_python)
    assert actual == {key: 0 for key in actual}


@pytest.mark.parametrize("changed", ["controller", "selected", "both", "python"])
async def test_start_confirmation_rehashes_changed_files_and_keeps_controller_first(
    shared_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    changed: Literal["controller", "selected", "both", "python"],
) -> None:
    record = await prepare_start(shared_host, "target")
    disk = shared_host.backend._disk().digests
    commands = list(shared_host.commands)
    roles: tuple[Role, ...] = (
        ("target", "base")
        if changed == "both"
        else ("target",)
        if changed == "selected"
        else ("base",)
    )
    paths = (
        [Path(shared_host.backend.controller_slot.resolved_python.path)]
        if changed == "python"
        else [
            Path(shared_host.slots[role].package.root) / "batch_payload/first.bin" for role in roles
        ]
    )
    originals = {path: (path.read_bytes(), path.stat()) for path in paths}
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    original = ConfigurationFile.confirm
    entered: list[bool] = []
    before_bytes: dict[Path, int] = {}
    before_python: list[int] = []

    def confirm(
        file: ConfigurationFile, expected: str, *, guard: Callable[[], None]
    ) -> ConfigurationSnapshot:
        if not entered and file.path == Path(shared_host.anchor.config_path):
            entered.append(True)
            # Two initial probe guards and the writer-held guard have passed.
            # The real file confirmation still runs its own original guards.
            assert len(checks) == 3
            before_bytes.update({leaf: observed.bytes for leaf, observed in reads.leaves.items()})
            before_python.append(python.bytes)
            for path in paths:
                contents, identity = originals[path]
                rewrite_with_restored_mtime(path, contents, identity)
        return original(file, expected, guard=guard)

    monkeypatch.setattr(ConfigurationFile, "confirm", confirm)
    code = "managed_slot_changed" if changed == "selected" else "anchor_unavailable"

    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        await shared_host.backend.start(record, "target")

    assert entered == [True] and len(before_python) == 1
    assert shared_host.probe.count == 1
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    assert shared_host.commands == commands
    with shared_host.journal.writer_lease() as lease:
        lease.check()
    if changed == "python":
        assert python.bytes - before_python[0] >= len(originals[paths[0]][0])
    else:
        controller = Path(shared_host.backend.controller_slot.package.root)
        damaged = paths[-1] if changed == "both" else paths[0]
        assert reads.leaves[damaged].bytes - before_bytes[damaged] >= reads.sizes[damaged]
        if changed == "both":
            # Both real files changed, selected first. Controller authentication
            # must fail before the distinct selected package is even revisited.
            selected = paths[0]
            assert not selected.is_relative_to(controller)
            assert reads.leaves[selected].bytes == before_bytes[selected]


@pytest.mark.parametrize("role", ["target", "base"])
async def test_start_confirmation_proof_does_not_cross_the_next_probe_await(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_start(shared_host, role)
    disk, commands = shared_host.backend._disk().digests, list(shared_host.commands)
    reads = observe_payloads(shared_host, monkeypatch)
    python = observe_python_reads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = shared_host.probe.read_managed

    async def gated(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller_slot: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        if shared_host.probe.count == 1:
            assert not entered.is_set()
            entered.set()
            await release.wait()
        return await original(journal, current, anchor, controller_slot, slots)

    monkeypatch.setattr(shared_host.probe, "read_managed", gated)
    before_bytes: dict[Path, int] = {}
    before_opens: dict[Path, int] = {}
    before_scans: dict[Path, int] = {}
    before_python, before_checks = 0, 0
    # The existing backend operation budget bounds this fake-manager gate.
    # TaskGroup owns and joins the operation even when an assertion fails.
    async with asyncio.timeout(shared_host.backend.operation_timeout):
        async with asyncio.TaskGroup() as tasks:
            operation = tasks.create_task(shared_host.backend.start(record, role))
            await entered.wait()
            assert not operation.done()
            before_checks = len(checks)
            before_bytes = {leaf: observed.bytes for leaf, observed in reads.leaves.items()}
            before_opens = {leaf: len(observed.opened) for leaf, observed in reads.leaves.items()}
            before_scans = dict(reads.scans)
            before_python = python.bytes
            release.set()

    assert entered.is_set() and shared_host.probe.count == 2
    assert before_checks == 11 and len(checks) - before_checks == 3
    assert shared_host.commands == [*commands, "start"]
    assert shared_host.record().position == record.position
    assert shared_host.backend._disk().digests == disk
    controller = Path(shared_host.backend.controller_slot.package.root)
    selected_root = Path(shared_host.slots[role].package.root)
    for leaf, observed in reads.leaves.items():
        # The first two post-await guards read fresh bytes. Only the final
        # adjacent writer/effect pair may share them; all three walks remain.
        copies = (
            2 if leaf.is_relative_to(controller) else 1 if leaf.is_relative_to(selected_root) else 0
        )
        walks = (
            3 if leaf.is_relative_to(controller) else 2 if leaf.is_relative_to(selected_root) else 0
        )
        actual = observed.bytes - before_bytes[leaf]
        assert actual == reads.sizes[leaf] * copies, (leaf.name, actual, copies)
        assert len(observed.opened) - before_opens[leaf] == walks
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
    for directory, count in reads.scans.items():
        copies = (
            3
            if directory.is_relative_to(controller)
            else 2
            if directory.is_relative_to(selected_root)
            else 0
        )
        assert count - before_scans[directory] == copies
    python_size = shared_host.backend.controller_slot.resolved_python.identity.size
    assert python.bytes - before_python == 2 * python_size
    assert shared_host.writer is not None
    shared_host.writer.check()


async def test_start_first_probe_reuses_only_its_fresh_post_guard_bytes(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    controller = Path(host.backend.controller_slot.package.root)
    selected = Path(host.slots["target"].package.root)
    assert controller != selected
    assert host.backend.controller_slot.resolved_python == host.slots["target"].resolved_python
    disk, commands = host.backend._disk(), list(host.commands)
    state_path = host.journal.directory.path / "state.json"
    state_bytes = state_path.read_bytes()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    expected = {
        Path(slot.package.root): slot.package.model_dump(mode="json")
        for slot in (host.backend.controller_slot, host.slots["target"])
    }
    complete_reads = {root: 0 for root in expected}
    fingerprint = managed_launcher.package_fingerprint

    def scanned(
        root: Path,
        limits: Mapping[str, int],
        *,
        verified: dict[str, dict[str, Any]] | None = None,
        observed: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, object]:
        # Delegate every real walk, including the existing synchronous batches.
        # A missing exact-slot leaf proof still reads the complete real package.
        result = fingerprint(root, limits, verified=verified, observed=observed)
        complete = result == expected[root]
        assert complete
        if verified is None:
            complete_reads[root] += 1
        return result

    argv_seen: list[tuple[str, ...]] = []
    runner = host.backend.runner

    async def run(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert timeout == host.probe.limits.seconds and limit == host.probe.limits.command_bytes
        assert merge_stderr is False
        argv_seen.append(tuple(argv))
        return await runner(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    monkeypatch.setattr(managed_launcher, "package_fingerprint", scanned)
    monkeypatch.setattr(host.backend, "runner", run)

    assert await host.backend.start(record, "target") is None

    assert argv_seen == [
        (
            "systemctl",
            "--user",
            "--no-block",
            "--job-mode=fail",
            "start",
            host.backend.service.unit,
        )
    ]
    assert host.commands == [*commands, "start"] and host.probe.count == 2
    assert len(checks) == 14 and all(current == record for current, _role in checks)
    assert sum(role is None for _current, role in checks) == 4
    assert sum(role == "target" for _current, role in checks) == 10
    assert host.transport.calls == [] and state_path.read_bytes() == state_bytes
    assert host.record() == record and host.record().deadline == record.deadline
    same_disk = host.backend._disk() == disk
    assert same_disk
    assert host.probe.snapshot.process is not None
    assert host.probe.snapshot.process.argv == host.arguments("target")
    assert host.writer is not None
    host.writer.check()
    host.control.check()

    controller_copies = complete_reads[controller]
    assert controller_copies > 0 and complete_reads[selected] == 2
    for leaf, observed in reads.leaves.items():
        root = controller if leaf.is_relative_to(controller) else selected
        assert leaf.is_relative_to(root)
        walks = 14 if root == controller else 10
        assert len(observed.opened) == walks
        assert observed.fd_stats >= 2 * walks and observed.path_stats == walks
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.bytes == reads.sizes[leaf] * complete_reads[root]
    for directory, count in reads.scans.items():
        root = controller if directory.is_relative_to(controller) else selected
        assert directory.is_relative_to(root)
        assert count == (14 if root == controller else 10)
    assert len(python.opened) == 24
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)
    assert (
        python.bytes
        == controller_copies * host.backend.controller_slot.resolved_python.identity.size
    )
    # All actual bytes, effects, independent opens and 14 checks precede this
    # red: only the first post-probe/writer-guard pair may share controller bytes.
    assert controller_copies == 5


@pytest.mark.parametrize("failure_kind", ["metadata", "error", "cancel"])
async def test_first_probe_does_not_publish_proofs_before_its_final_fence_returns(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    disk, commands = host.backend._disk(), list(host.commands)
    state_path = host.journal.directory.path / "state.json"
    state_bytes = state_path.read_bytes()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    proofs = _FileProofs()
    controller = host.backend.controller_slot
    metadata_path = host.journal.directory.path / f"slot-{controller.digest}.json"
    mode = stat.S_IMODE(metadata_path.stat().st_mode)
    failure = (
        RuntimeError("post-check failed") if failure_kind == "error" else asyncio.CancelledError()
    )
    entered: list[bool] = []
    original = managed_deployment._MetadataScope._finish

    def finish(metadata: managed_deployment._MetadataScope) -> None:
        if host.probe.count == 1:
            assert not entered and len(checks) == 2
            entered.append(True)
            assert proofs.python is None and proofs.packages is None
            if failure_kind == "metadata":
                metadata_path.chmod(0o644)
            original(metadata)
            # Even an exception after the real leaf fence cannot publish the
            # already observed package: the surrounding check has not returned.
            raise failure
        original(metadata)

    monkeypatch.setattr(managed_deployment._MetadataScope, "_finish", finish)
    expected = ManagedDeploymentError if failure_kind == "metadata" else type(failure)
    try:
        with pytest.raises(expected) as caught:
            await host.backend._probe(record, postproofs=proofs)
    finally:
        metadata_path.chmod(mode)

    if failure_kind == "metadata":
        assert str(caught.value) == "managed_slot_unavailable"
    else:
        assert caught.value is failure
    assert entered == [True] and proofs.python is None and proofs.packages is None
    controller_root = Path(controller.package.root)
    for path, observed in reads.leaves.items():
        copies = 2 if path.is_relative_to(controller_root) else 0
        assert observed.bytes == copies * reads.sizes[path]
    assert python.bytes == 2 * controller.resolved_python.identity.size
    assert len(checks) == 2 and host.probe.count == 1
    assert host.commands == commands and host.transport.calls == []
    assert host.record() == record and state_path.read_bytes() == state_bytes
    same_disk = host.backend._disk() == disk
    assert same_disk and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("controller", "anchor_unavailable"),
        ("selected", "managed_slot_changed"),
        ("python", "anchor_unavailable"),
        ("anchor", "anchor_unavailable"),
        ("revision", "stale_revision"),
    ],
)
async def test_start_first_probe_rechecks_changes_made_during_its_await(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, fault: str, code: str
) -> None:
    host = shared_host
    record = expected_record = await prepare_start(host, "target")
    disk, commands, service = host.backend._disk(), list(host.commands), host.probe.snapshot
    original = host.probe.read_managed
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller_slot: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        result = await original(journal, current, anchor, controller_slot, slots)
        assert host.probe.count == 1
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(host.probe, "read_managed", gated)
    task = asyncio.create_task(host.backend.start(record, "target"))
    try:
        async with asyncio.timeout(host.backend.operation_timeout):
            await entered.wait()
            assert not task.done()
            if fault == "revision":
                with host.journal.writer_lease() as lease:
                    lease.check()
                    expected_record = host.move("verifying")
            else:
                path = (
                    Path(host.backend.controller_slot.resolved_python.path)
                    if fault == "python"
                    else host.journal.directory.path / f"anchor-{host.anchor.digest}.json"
                    if fault == "anchor"
                    else Path(host.slots["target" if fault == "selected" else "base"].package.root)
                    / "batch_payload/first.bin"
                )
                rewrite_with_restored_mtime(path, path.read_bytes(), path.stat())
            release.set()
            expected = TransactionError if fault == "revision" else ManagedDeploymentError
            with pytest.raises(expected, match=f"^{code}$"):
                await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert host.probe.count == 1 and host.probe.snapshot == service
    assert host.commands == commands and host.transport.calls == []
    assert host.record() == expected_record and expected_record.deadline == record.deadline
    same_disk = host.backend._disk() == disk
    assert same_disk and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("controller", "anchor_unavailable"),
        ("selected", "managed_slot_changed"),
        ("python", "anchor_unavailable"),
        ("python-replaced", "anchor_unavailable"),
        ("anchor", "anchor_unavailable"),
        ("slot", "managed_slot_unavailable"),
        ("lease", "lock_not_owned"),
        ("deadline", ""),
        ("deadline-controller", "anchor_unavailable"),
        ("cgroup", "cgroup_not_empty"),
    ],
)
async def test_start_first_guard_rechecks_changes_after_the_probe_returns(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, fault: str, code: str
) -> None:
    host = shared_host
    record = await prepare_start(host, "target")
    disk, commands, service = host.backend._disk(), list(host.commands), host.probe.snapshot
    state_path = host.journal.directory.path / "state.json"
    state_bytes = state_path.read_bytes()
    checks = observe_checks(host, monkeypatch)
    original = FixedLock.__enter__
    entered: list[FixedLock] = []
    lock_path = host.journal.writer_directory.path / "lease"
    retained = lock_path.with_name(lock_path.name + ".retained")

    def writer(lease: FixedLock) -> FixedLock:
        result = original(lease)
        if lease.directory is host.journal.writer_directory and not entered:
            assert host.probe.count == 1 and len(checks) == 2
            entered.append(lease)
            if fault in {"deadline", "deadline-controller"}:
                host.clock.elapsed = record.deadline.seconds - record.deadline.rollback_reserve
            if fault == "lease":
                assert not retained.exists()
                lock_path.rename(retained)
                lock_path.write_bytes(b"")
                lock_path.chmod(0o600)
            elif fault == "cgroup":
                host.events.write_bytes(b"populated 1\n")
            elif fault == "python-replaced":
                path = Path(host.backend.controller_slot.resolved_python.path)
                before = path.stat()
                replacement = path.with_name(path.name + ".replacement")
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(stat.S_IMODE(before.st_mode))
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement.replace(path)
                after = path.stat()
                assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
            elif fault in {"controller", "selected", "python", "deadline-controller"}:
                path = (
                    Path(host.backend.controller_slot.resolved_python.path)
                    if fault == "python"
                    else Path(host.slots["target" if fault == "selected" else "base"].package.root)
                    / "batch_payload/first.bin"
                )
                rewrite_with_restored_mtime(path, path.read_bytes(), path.stat())
            elif fault in {"anchor", "slot"}:
                digest = host.anchor.digest if fault == "anchor" else host.slots["target"].digest
                path = host.journal.directory.path / f"{fault}-{digest}.json"
                path.write_bytes(b"{}")
        return result

    monkeypatch.setattr(FixedLock, "__enter__", writer)
    expected = (
        TimeoutError
        if fault == "deadline"
        else TransactionError
        if fault == "lease"
        else ObservationError
        if fault == "cgroup"
        else ManagedDeploymentError
    )
    try:
        with pytest.raises(expected, match=f"^{code}$"):
            await host.backend.start(record, "target")
    finally:
        if retained.exists():
            lock_path.unlink()
            retained.rename(lock_path)

    assert len(entered) == 1 and entered[0].fd is None and len(checks) == 3
    assert host.probe.count == 1 and host.probe.snapshot == service
    assert host.commands == commands and host.transport.calls == []
    assert host.record() == record and state_path.read_bytes() == state_bytes
    same_disk = host.backend._disk() == disk
    assert same_disk and host.writer is None
    with host.journal.writer_lease() as lease:
        lease.check()
    host.control.check()
