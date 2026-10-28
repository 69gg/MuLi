"""Install preflight guards cannot lend authentication across a manager await."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from test_managed_backend import Host
from test_managed_backend_package_batch import observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import shared_host as shared_host

from muli.managed_controller import BindingConfirmation
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import FixedLock, Role, TransactionError, TransactionJournal
from muli.platforms.linux_transaction import ObservationError
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    ManagedDeploymentError,
    SelectorStore,
    SelectorValue,
    SlotIdentity,
)
from muli.platforms.managed_probe import ManagedProbeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux real install guards")


@pytest.mark.parametrize("fault", ["controller", "selected", "both"])
async def test_install_authenticates_both_roles_before_its_first_ipc(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    record = shared_host.install_intent()
    before = shared_host.backend._disk()
    reads = observe_payloads(shared_host, monkeypatch)
    roles: tuple[Role, ...] = (
        ("base", "target") if fault == "both" else ("base" if fault == "controller" else "target",)
    )
    for role in roles:
        leaf = Path(shared_host.slots[role].package.root) / "batch_payload/first.bin"
        leaf.write_bytes(b"damaged retained package")

    code = "managed_slot_changed" if fault == "selected" else "anchor_unavailable"
    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        await shared_host.backend.install(record, "target", shared_host.data["target"])

    assert shared_host.probe.count == 0 and shared_host.commands == []
    assert shared_host.backend._disk() == before and shared_host.record() == record
    controller = Path(shared_host.backend.controller_slot.package.root)
    for leaf, observed in reads.leaves.items():
        if leaf.is_relative_to(controller):
            assert observed.bytes > 0
        elif fault != "selected":
            # Controller corruption retains precedence over selected corruption.
            assert observed.bytes == 0


@pytest.mark.parametrize("boundary", [1, 2])
@pytest.mark.parametrize("fault", ["controller", "selected", "revision"])
async def test_install_reauthenticates_changes_during_each_initial_probe_await(
    shared_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
    fault: Literal["controller", "selected", "revision"],
) -> None:
    record = shared_host.install_intent()
    before = shared_host.backend._disk()
    reads = observe_payloads(shared_host, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = shared_host.probe.read_managed

    async def gated(
        journal: TransactionJournal,
        current: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        if shared_host.probe.count + 1 == boundary:
            entered.set()
            await release.wait()
        return await original(journal, current, anchor, controller, slots)

    monkeypatch.setattr(shared_host.probe, "read_managed", gated)
    error = TransactionError if fault == "revision" else ManagedDeploymentError
    code = {
        "controller": "anchor_unavailable",
        "selected": "managed_slot_changed",
        "revision": "stale_revision",
    }[fault]

    async def rejected() -> None:
        with pytest.raises(error, match=f"^{code}$"):
            await shared_host.backend.install(record, "target", shared_host.data["target"])

    changed_leaf: Path | None = None
    bytes_before_change = 0
    moved: TransactionRecordV2 | None = None
    async with asyncio.timeout(shared_host.backend.operation_timeout):
        async with asyncio.TaskGroup() as tasks:
            operation = tasks.create_task(rejected())
            await entered.wait()
            assert not operation.done()
            if fault == "revision":
                moved = shared_host.move("activating", "start_target")
            else:
                role: Role = "base" if fault == "controller" else "target"
                changed_leaf = (
                    Path(shared_host.slots[role].package.root) / "batch_payload/first.bin"
                )
                contents, identity = changed_leaf.read_bytes(), changed_leaf.stat()
                bytes_before_change = reads.leaves[changed_leaf].bytes
                with changed_leaf.open("r+b") as output:
                    output.write(bytes([contents[0] ^ 0xFF]) + contents[1:])
                os.utime(changed_leaf, ns=(identity.st_atime_ns, identity.st_mtime_ns))
                after = changed_leaf.stat()
                assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
                    identity.st_dev,
                    identity.st_ino,
                    identity.st_size,
                    identity.st_mtime_ns,
                )
                assert after.st_ctime_ns != identity.st_ctime_ns
            release.set()

    assert entered.is_set() and shared_host.probe.count == boundary
    assert shared_host.backend._disk() == before and shared_host.commands == []
    if changed_leaf is not None:
        assert reads.leaves[changed_leaf].bytes - bytes_before_change >= reads.sizes[changed_leaf]
        assert shared_host.record() == record
    else:
        assert moved is not None and moved.revision == record.revision + 1
        assert shared_host.record() == moved and moved.deadline == record.deadline


@pytest.mark.parametrize("fault", ["deadline", "lease", "cgroup", "config", "selector", "unit"])
async def test_writer_preflight_failure_prevents_its_probe_ipc(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    record = shared_host.install_intent()
    original = FixedLock.__enter__
    injected: list[tuple[str, str | None, str]] = []

    def entered(lease: FixedLock) -> FixedLock:
        result = original(lease)
        if lease.directory is shared_host.journal.writer_directory and lease.name == "lease":
            assert shared_host.probe.count == 1 and not injected
            if fault == "deadline":
                shared_host.clock.elapsed += record.deadline.seconds
            elif fault == "lease":
                path = lease.directory.path / lease.name
                replacement = path.with_name(path.name + ".replacement")
                replacement.write_bytes(b"")
                replacement.chmod(0o600)
                replacement.replace(path)
            elif fault == "cgroup":
                shared_host.events.write_bytes(b"populated 1\n")
            elif fault == "selector":
                with SelectorStore(shared_host.journal.directory.path) as selector:
                    selector.replace(
                        selector.read(),
                        SelectorValue(deployment="f" * 64, installation="f" * 64),
                        guard=lambda: None,
                    )
            else:
                path = (
                    shared_host.anchor.config_path
                    if fault == "config"
                    else shared_host.anchor.unit_path
                )
                Path(path).write_bytes(b"foreign binding bytes")
            injected.append(shared_host.backend._disk().digests)
        return result

    monkeypatch.setattr(FixedLock, "__enter__", entered)
    errors: dict[str, type[Exception]] = {
        "deadline": TimeoutError,
        "lease": TransactionError,
        "cgroup": ObservationError,
        "config": ObservationError,
        "selector": ObservationError,
        "unit": ObservationError,
    }
    with pytest.raises(errors[fault]):
        await shared_host.backend.install(record, "target", shared_host.data["target"])

    assert len(injected) == 1 and shared_host.backend._disk().digests == injected[0]
    assert shared_host.probe.count == 1 and shared_host.commands == []
    assert shared_host.record() == record


@pytest.mark.parametrize("populated_after_reload", [False, True])
async def test_reload_retains_independent_guards_on_both_sides_of_its_await(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch, populated_after_reload: bool
) -> None:
    record = shared_host.install_intent()
    # The fake manager has an older loaded unit; disk and all authentication are real.
    shared_host.probe.loaded_unit = shared_host.legacy.render().encode()
    reads = observe_payloads(shared_host, monkeypatch)
    checks = observe_checks(shared_host, monkeypatch)
    original = shared_host.backend.runner
    command_checks: list[int] = []
    command_bytes: dict[Path, int] = {}
    post_reload: list[int] = []

    async def runner(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        assert argv == ["systemctl", "--user", "daemon-reload"]
        # The original pre-command strong guard remains distinct from the one
        # after this await. Both read selected as well as controller bytes.
        assert checks[-1] == (record, "target")
        result = await original(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)
        command_checks.append(len(checks))
        command_bytes.update({leaf: observed.bytes for leaf, observed in reads.leaves.items()})
        if populated_after_reload:
            shared_host.events.write_bytes(b"populated 1\n")
        return result

    def before_read(index: int) -> None:
        if index == 4:
            assert len(command_checks) == 1 and len(checks) - command_checks[0] == 1
            assert checks[-1] == (record, "target")
            for leaf, observed in reads.leaves.items():
                assert observed.bytes - command_bytes[leaf] == reads.sizes[leaf]
            post_reload.append(index)

    monkeypatch.setattr(shared_host.backend, "runner", runner)
    shared_host.probe.before_read = before_read
    if populated_after_reload:
        with pytest.raises(ObservationError, match="^cgroup_not_empty$"):
            await shared_host.backend.install(record, "target", shared_host.data["target"])
        assert shared_host.probe.count == 3 and not post_reload
        assert len(checks) - command_checks[0] == 1
    else:
        confirmation = await shared_host.backend.install(
            record, "target", shared_host.data["target"]
        )
        binding = record.target
        assert confirmation == BindingConfirmation(
            record.position,
            "target",
            binding.configuration.digest,
            None if binding.selector is None else binding.selector.digest,
            binding.unit.digest,
            binding.unit.digest,
        )
        assert shared_host.probe.count == 4 and post_reload == [4]
    assert shared_host.commands == ["daemon-reload"] and shared_host.record() == record
    disk = shared_host.backend._disk()
    assert disk.configuration.data == shared_host.data["target"].configuration
    assert disk.selector is not None and disk.selector.value == shared_host.selector("target")
    assert disk.unit.data == shared_host.data["target"].unit


async def test_next_install_reauthenticates_both_packages_before_its_first_ipc(
    shared_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = shared_host.install_intent()
    first = await shared_host.backend.install(record, "target", shared_host.data["target"])
    shared_host.probe.count = 0
    reads = observe_payloads(shared_host, monkeypatch)
    initial_probes: list[int] = []

    def before_read(index: int) -> None:
        if index == 1:
            for leaf, observed in reads.leaves.items():
                assert observed.bytes == reads.sizes[leaf] and len(observed.opened) == 1
            initial_probes.append(index)

    shared_host.probe.before_read = before_read
    repeated = await shared_host.backend.install(record, "target", shared_host.data["target"])

    assert repeated == first and initial_probes == [1]
    assert shared_host.probe.count == 3 and shared_host.commands == []
    assert shared_host.record() == record
