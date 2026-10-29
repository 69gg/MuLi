"""Effect guards reuse adjacent bytes while retaining every independent walk."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from test_managed_backend import Host
from test_managed_backend import host as host
from test_managed_backend_package_batch import PackageReads, observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import observe_python_reads
from test_managed_deployment_authentication_batch import bounded_slots as bounded_slots
from test_managed_host_command_authentication import Command, HostAction, prepare_command
from test_managed_observation_authentication import running_target
from test_managed_start_confirmation_batch import rewrite_with_restored_mtime

from muli.managed_controller import ManagedServiceObservation
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionError, TransactionJournal
from muli.platforms.managed_backend import _FileProofs
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    ManagedDeploymentError,
    SlotIdentity,
)
from muli.platforms.managed_probe import ManagedProbeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real effect file guards")


async def prepare_stop(host: Host, role: Role) -> TransactionRecordV2:
    if role == "base":
        host.move("draining")
        record = host.move("activating", "stop_base")
    else:
        await running_target(host)
        record = host.move("rolling_back", "stop_target", code="backend_failed")
    host.probe.count = 0
    return record


def assert_walks(reads: PackageReads, walks: dict[Path, int]) -> None:
    """Count actual opens, FD/path identities and enumeration before byte budgets."""
    for path, leaf in reads.leaves.items():
        count = next(value for root, value in walks.items() if path.is_relative_to(root))
        assert len(leaf.opened) == count, str(path)
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in leaf.opened)
        assert leaf.fd_stats >= 2 * count, str(path)
        assert leaf.path_stats == count, str(path)
    for path, count in reads.scans.items():
        expected = next(value for root, value in walks.items() if path.is_relative_to(root))
        assert count == expected, str(path)


def expected_payload_bytes(reads: PackageReads, copies: dict[Path, int]) -> dict[str, int]:
    return {
        str(path): size * next(value for root, value in copies.items() if path.is_relative_to(root))
        for path, size in reads.sizes.items()
    }


@pytest.mark.parametrize("role", ["base", "target"])
async def test_stop_reuses_two_adjacent_content_copies_with_five_real_checks(
    host: Host, monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    record = await prepare_stop(host, role)
    controller = Path(host.backend.controller_slot.package.root)
    target = Path(host.slots["target"].package.root)
    assert controller != target
    # A stop-only observation may stop the frozen damaged target without ever
    # treating its package as executable or healthy evidence.
    leaf = target / "batch_payload/first.bin"
    leaf.write_bytes(leaf.read_bytes() + b"damaged opposite or stopped target")
    disk = host.backend._disk()
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    commands = list(host.commands)
    writer = host.writer
    assert writer is not None and writer.fd is not None
    writer.check()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)

    await host.backend.stop(record, role)

    assert checks == [(record, None)] * 5
    assert host.probe.count == 2 and host.commands == [*commands, "stop"]
    assert host.transport.calls == [] and host.backend._stopped(host.probe.snapshot)
    assert host.writer is None and writer.fd is None
    same_record = host.record() == record
    same_disk = host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    assert record.deadline == host.record().deadline
    host.control.check()
    with host.journal.writer_lease() as available:
        available.check()
    assert_walks(reads, {controller: 5, target: 0})
    assert len(python.opened) == 5
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)

    # The old implementation reaches the real successful stop and all five
    # complete walks above, then fails only on its two duplicate byte copies.
    actual = {str(path): item.bytes for path, item in reads.leaves.items()}
    actual["python"] = python.bytes
    expected = expected_payload_bytes(reads, {controller: 3, target: 0})
    expected["python"] = host.backend.controller_slot.resolved_python.identity.size * 3
    assert actual == expected


@pytest.mark.parametrize("command", ["drain", "resume", "activate-target", "activate-base"])
async def test_host_command_reuses_only_the_pre_http_controller_content_copy(
    host: Host, monkeypatch: pytest.MonkeyPatch, command: Command
) -> None:
    record, action, role = await prepare_command(host, command)
    host.probe.count = 0
    selected: Role = "base" if role is None else role
    controller = Path(host.backend.controller_slot.package.root)
    target = Path(host.slots["target"].package.root)
    assert controller != target
    if selected == "base":
        path = target / "batch_payload/first.bin"
        path.write_bytes(path.read_bytes() + b"damaged opposite package")
    disk = host.backend._disk()
    state_path = host.journal.directory.path / "state.json"
    state = state_path.read_bytes()
    commands, http_count = list(host.commands), len(host.transport.calls)
    writer = host.writer
    assert writer is not None
    writer.check()
    reads = observe_payloads(host, monkeypatch)
    python = observe_python_reads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)

    result = await host.backend._host_command(record, action, role)

    assert checks == [(record, value) for value in (None, None, selected, None, selected)]
    assert host.probe.count == 2 and host.transport.calls[http_count:] == [action]
    assert host.commands == commands and host.writer is writer
    writer.check()
    same_record = host.record() == record
    same_disk = host.backend._disk() == disk
    assert same_record and same_disk and state_path.read_bytes() == state
    assert record.deadline == host.record().deadline
    if action == "activate":
        assert result is not None and result.health is not None
        assert record.accepts(result.health, selected, released=True)
        assert result.process is not None
        assert result.process.installation == record.binding(selected).installation
        assert result.boot == host.probe.snapshot.unit.invocation
    else:
        assert result is None
    assert host.admission == ("paused" if action == "drain" else "released")
    target_walks = 2 if selected == "target" else 0
    assert_walks(reads, {controller: 5, target: target_walks})
    assert len(python.opened) == 5 + target_walks
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python.opened)

    # Both selected package reads, both probes, proof and real writer ownership
    # precede the old-code failure: only controller has one redundant byte copy.
    actual = {str(path): item.bytes for path, item in reads.leaves.items()}
    actual["python"] = python.bytes
    expected = expected_payload_bytes(reads, {controller: 4, target: target_walks})
    expected["python"] = host.backend.controller_slot.resolved_python.identity.size * 4
    assert actual == expected


type Window = Literal["stop-first", "stop-second", "host-front"]


async def prepare_window(
    host: Host, window: Window
) -> tuple[TransactionRecordV2, HostAction | None, Role | None]:
    if window == "host-front":
        return await prepare_command(host, "activate-target")
    return await prepare_stop(host, "base"), None, "base"


async def dispatch(
    host: Host, record: TransactionRecordV2, action: HostAction | None, role: Role | None
) -> ManagedServiceObservation | None:
    if action is None:
        assert role is not None
        await host.backend.stop(record, role)
        return None
    return await host.backend._host_command(record, action, role)


def after_probe(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    window: Window,
    callback: Callable[[_FileProofs | None], None],
) -> list[int]:
    """Inject after a real probe and all its post-await authentication returns."""
    original = host.backend._probe
    entered: list[int] = []

    async def probe(
        record: TransactionRecordV2,
        *,
        preflight: Callable[[], None] | None = None,
        postprocess: Callable[[ManagedProbeResult], Role | None] | None = None,
        postproofs: _FileProofs | None = None,
    ) -> ManagedProbeResult:
        result = await original(
            record, preflight=preflight, postprocess=postprocess, postproofs=postproofs
        )
        target = 2 if window == "stop-second" else 1
        if host.probe.count == target and not entered:
            entered.append(target)
            callback(postproofs)
        return result

    monkeypatch.setattr(host.backend, "_probe", probe)
    return entered


@pytest.mark.parametrize("window", ["stop-first", "stop-second", "host-front"])
@pytest.mark.parametrize("fault", ["controller", "python", "replaced", "revision", "deadline"])
async def test_effect_rechecks_content_revision_and_deadline_after_adjacent_probe(
    host: Host, monkeypatch: pytest.MonkeyPatch, window: Window, fault: str
) -> None:
    record, action, role = await prepare_window(host, window)
    host.probe.count = 0
    disk = host.backend._disk()
    commands, http_count = list(host.commands), len(host.transport.calls)
    current = record
    checks = observe_checks(host, monkeypatch)
    reads = observe_payloads(host, monkeypatch)
    controller = Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    before_bytes: list[int] = []

    def changed(proofs: _FileProofs | None) -> None:
        nonlocal current
        assert proofs is not None and proofs.python is not None and proofs.packages is not None
        before_bytes.append(reads.leaves[controller].bytes)
        if fault == "revision":
            current = (
                host.move("committed", proof=record.health)
                if action is not None
                else host.move("activating", "install_target")
            )
        elif fault == "deadline":
            reserve = record.deadline.rollback_reserve if action is None else 0
            host.clock.elapsed = record.deadline.seconds - reserve
        else:
            path = (
                Path(host.backend.controller_slot.resolved_python.path)
                if fault == "python"
                else controller
            )
            contents, before = path.read_bytes(), path.stat()
            if fault == "replaced":
                replacement = path.with_name(path.name + ".replacement")
                replacement.write_bytes(bytes([contents[0] ^ 0xFF]) + contents[1:])
                replacement.chmod(stat.S_IMODE(before.st_mode))
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement.replace(path)
                after = path.stat()
                assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
            else:
                rewrite_with_restored_mtime(path, contents, before)

    entered = after_probe(host, monkeypatch, window, changed)
    expected = (
        TransactionError
        if fault == "revision"
        else TimeoutError
        if fault == "deadline"
        else ManagedDeploymentError
    )
    code = (
        "stale_revision"
        if fault == "revision"
        else ""
        if fault == "deadline"
        else "anchor_unavailable"
    )
    with pytest.raises(expected, match=f"^{code}$"):
        await dispatch(host, record, action, role)

    assert entered == [2 if window == "stop-second" else 1]
    assert len(checks) == (5 if window == "stop-second" else 3)
    assert host.probe.count == (2 if window == "stop-second" else 1)
    assert host.commands == commands and len(host.transport.calls) == http_count
    same_disk = host.backend._disk() == disk
    same_record = host.record() == current
    assert same_disk and same_record and current.deadline == record.deadline
    assert host.writer is not None
    host.writer.check()
    if fault in {"controller", "replaced"}:
        assert reads.leaves[controller].bytes - before_bytes[0] == reads.sizes[controller]


@pytest.mark.parametrize("fault", ["selected", "both"])
async def test_host_front_guard_retains_selected_authentication_and_controller_priority(
    host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    record, action, role = await prepare_window(host, "host-front")
    host.probe.count = 0
    commands, http_count = list(host.commands), len(host.transport.calls)
    checks = observe_checks(host, monkeypatch)
    changed: list[Path] = []

    def change(proofs: _FileProofs | None) -> None:
        assert proofs is not None and proofs.python is not None and proofs.packages is not None
        roles: tuple[Role, ...] = ("target", "base") if fault == "both" else ("target",)
        for selected in roles:
            path = Path(host.slots[selected].package.root) / "batch_payload/first.bin"
            rewrite_with_restored_mtime(path, path.read_bytes(), path.stat())
            changed.append(path)

    entered = after_probe(host, monkeypatch, "host-front", change)
    code = "anchor_unavailable" if fault == "both" else "managed_slot_changed"
    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        await dispatch(host, record, action, role)

    assert entered == [1] and len(changed) == (2 if fault == "both" else 1)
    assert checks == [(record, None), (record, None), (record, "target")]
    assert host.probe.count == 1 and host.commands == commands
    assert len(host.transport.calls) == http_count and host.record() == record
    assert host.writer is not None
    host.writer.check()


@pytest.mark.parametrize("window", ["stop-first", "stop-second", "host-front"])
async def test_effect_reopens_and_rereads_same_bytes_after_package_inode_changes(
    host: Host, monkeypatch: pytest.MonkeyPatch, window: Window
) -> None:
    record, action, role = await prepare_window(host, window)
    host.probe.count = 0
    commands, http_count = list(host.commands), len(host.transport.calls)
    path = Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    original_stat = os.stat
    reads = observe_payloads(host, monkeypatch)
    before_read: list[int] = []

    def replace_leaf(proofs: _FileProofs | None) -> None:
        assert proofs is not None and proofs.python is not None and proofs.packages is not None
        before_read.append(reads.leaves[path].bytes)
        contents, before = path.read_bytes(), original_stat(path)
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(contents)
        replacement.chmod(stat.S_IMODE(before.st_mode))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        after = original_stat(path)
        assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    entered = after_probe(host, monkeypatch, window, replace_leaf)
    result = await dispatch(host, record, action, role)

    assert entered == [2 if window == "stop-second" else 1] and host.probe.count == 2
    assert host.record() == record
    assert reads.leaves[path].bytes > before_read[0]
    assert len(reads.leaves[path].opened) == 5
    assert reads.leaves[path].fd_stats >= 10 and reads.leaves[path].path_stats == 5
    if action is None:
        assert host.commands == [*commands, "stop"] and len(host.transport.calls) == http_count
    else:
        assert host.commands == commands and host.transport.calls[http_count:] == [action]
        assert result is not None and result.health is not None
        assert record.accepts(result.health, "target", released=True)


@pytest.mark.parametrize("operation", ["stop", "host"])
@pytest.mark.parametrize("fault", ["controller", "cancel"])
async def test_next_await_reauthenticates_or_propagates_cancellation_without_effects(
    host: Host, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str
) -> None:
    window: Window = "stop-first" if operation == "stop" else "host-front"
    record, action, role = await prepare_window(host, window)
    host.probe.count = 0
    commands, http_count = list(host.commands), len(host.transport.calls)
    disk = host.backend._disk()
    path = Path(host.backend.controller_slot.package.root) / "batch_payload/first.bin"
    contents, info = path.read_bytes(), path.stat()
    reads = observe_payloads(host, monkeypatch)
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellations: list[asyncio.CancelledError] = []
    first_proofs: list[_FileProofs] = []

    def remember(proofs: _FileProofs | None) -> None:
        assert proofs is not None and proofs.python is not None and proofs.packages is not None
        first_proofs.append(proofs)

    after_probe(host, monkeypatch, window, remember)
    original_read, original_request = host.probe.read_managed, host.backend._request

    async def pause() -> None:
        if operation == "stop":
            assert len(first_proofs) == 1
            assert first_proofs[0].python is None and first_proofs[0].packages is None
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise
        finally:
            settled.set()

    async def read(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        result = await original_read(journal, current, anchor, controller, slots)
        if operation == "stop" and host.probe.count == 2:
            await pause()
        return result

    async def request(
        current: TransactionRecordV2, boot: str, selected_action: str
    ) -> dict[str, object] | None:
        if operation == "host":
            await pause()
        return await original_request(current, boot, selected_action)

    monkeypatch.setattr(host.probe, "read_managed", read)
    monkeypatch.setattr(host.backend, "_request", request)
    task = asyncio.create_task(dispatch(host, record, action, role))
    before_bytes = 0
    try:
        async with asyncio.timeout(host.backend.operation_timeout):
            await entered.wait()
            assert not task.done() and not settled.is_set()
            before_bytes = reads.leaves[path].bytes
            if fault == "cancel":
                task.cancel()
            else:
                rewrite_with_restored_mtime(path, contents, info)
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

    assert task.done() and settled.is_set()
    assert host.commands == commands and host.record() == record
    same_disk = host.backend._disk() == disk
    assert same_disk and host.writer is not None
    host.writer.check()
    if fault == "cancel":
        assert len(host.transport.calls) == http_count
        assert reads.leaves[path].bytes == before_bytes
    else:
        assert reads.leaves[path].bytes - before_bytes == reads.sizes[path]
        expected_calls = [] if operation == "stop" else [action]
        assert host.transport.calls[http_count:] == expected_calls
