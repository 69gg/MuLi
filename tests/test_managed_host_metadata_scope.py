"""Host metadata proofs stay inside one synchronous, fully fenced record read."""

from __future__ import annotations

import errno
import os
import sys
from collections import Counter
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

import pytest
from test_managed_authentication_boundaries import (
    after_package_scan,
    replace_or_rewrite,
    three_installations,
)
from test_managed_backend import Host, track_slot_scans
from test_managed_backend_python_proof import observe_python_reads
from test_managed_deployment_authentication_batch import bounded_slots as bounded_slots
from test_managed_deployment_authentication_batch import observe_metadata
from test_managed_host import filesystems as filesystems
from test_managed_observation_authentication import observe_complete_package_scans

import muli
import muli.managed_host as host_module
import muli.platforms.managed_deployment as deployment_module
from muli.managed_journal import RoleSnapshots
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import (
    JournalControl,
    Role,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
)
from muli.platforms import managed_launcher
from muli.platforms.managed_deployment import ManagedDeploymentError, SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux private host metadata")


@pytest.fixture
async def metadata_host(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Host]:
    variant = getattr(request, "param", "switch")
    mode: Literal["adopt", "switch"] = "adopt" if variant == "adopt" else "switch"
    with TransactionJournal(tmp_path / "journal", tmp_path / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            value = Host(tmp_path, journal, control, mode, change_config=False)
            try:
                if variant == "three":
                    three_installations(value)
                # The host checks the private ordinary file, never opens SQLite.
                journal.database.write_bytes(b"")
                journal.database.chmod(0o600)
                yield value
            finally:
                value.release_writer()
                await value.backend.__aexit__()


async def test_typed_entry_authenticates_controller_once_and_keeps_process_and_final_state(
    metadata_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = metadata_host
    root, controller = host.journal.directory.path, host.backend.controller_slot
    package = Path(controller.package.root)
    state_path = root / "state.json"
    state_bytes, record = state_path.read_bytes(), host.record()
    original_fingerprint = managed_launcher.package_fingerprint
    scans = observe_complete_package_scans(host, monkeypatch)
    python_reads = observe_python_reads(host, monkeypatch)
    original_route = host_module.resolve_entry
    original_process = deployment_module.verify_current_process
    routes: list[managed_launcher.LaunchPlan] = []
    processes: list[SlotIdentity] = []

    def unchanged() -> None:
        pass

    after_route: Callable[[], None] = unchanged
    after_process: Callable[[], None] = unchanged
    observing_positive = True

    def route(path: Path, *, purpose: str) -> managed_launcher.LaunchPlan:
        before_scans, before_python = dict(scans), python_reads.bytes
        plan = original_route(path, purpose=purpose)
        assert path == root and purpose == "recover" and plan.role == "controller"
        assert plan.deployment == host.anchor.digest and plan.installation == controller.digest
        assert plan.generation == record.generation and plan.revision == record.revision
        assert plan.state_digest == managed_launcher.digest(state_bytes)
        assert plan.python == Path(controller.lexical_python)
        if observing_positive:
            assert python_reads.bytes - before_python == controller.resolved_python.identity.size
            assert {path: scans[path] - count for path, count in before_scans.items()} == {
                path: int(path == package) for path in scans
            }
        routes.append(plan)
        after_route()
        return plan

    def process(slot: SlotIdentity) -> None:
        processes.append(slot)
        original_process(slot)
        after_process()

    monkeypatch.setattr(sys, "executable", controller.lexical_python)
    monkeypatch.setattr(sys, "prefix", controller.prefix)
    monkeypatch.setattr(muli, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(host_module, "resolve_entry", route)
    monkeypatch.setattr(host_module, "verify_current_process", process)
    monkeypatch.setattr(deployment_module, "verify_current_process", process)

    entry = host_module._entry(root)

    assert entry.anchor == host.anchor and entry.controller == controller
    assert entry.record == record and state_path.read_bytes() == state_bytes
    assert processes == [controller] and len(routes) == 1
    typed_scans = scans[package] - len(routes)
    typed_python_bytes = python_reads.bytes - controller.resolved_python.identity.size
    typed_python_opens = len(python_reads.opened) - len(routes)
    assert typed_scans > 0 and typed_python_opens == typed_scans
    assert typed_python_bytes == controller.resolved_python.identity.size * typed_scans
    assert scans[Path(host.slots["target"].package.root)] == 0
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python_reads.opened)
    # Fault cases must reach the real verifier's rejection, not the positive spy.
    observing_positive = False
    monkeypatch.setattr(managed_launcher, "package_fingerprint", original_fingerprint)

    leaf = package / "__main__.py"
    original_leaf = leaf.read_bytes()

    def damage_package() -> None:
        leaf.write_bytes(original_leaf + b"\n# changed after actual routing\n")

    after_route = damage_package
    try:
        with monkeypatch.context() as local:
            local.setattr(sys, "executable", host.slots["target"].lexical_python)
            with pytest.raises(ManagedDeploymentError, match="^anchor_unavailable$"):
                host_module._entry(root)
    finally:
        leaf.write_bytes(original_leaf)
        after_route = unchanged
    # Actual typed package authentication must precede a wrong-process error.
    assert processes == [controller]

    with monkeypatch.context() as local:
        local.setattr(sys, "executable", host.slots["target"].lexical_python)
        with pytest.raises(ManagedDeploymentError, match="^managed_process_mismatch$"):
            host_module._entry(root)
    assert processes == [controller, controller]

    def change_final_state() -> None:
        state_path.write_bytes(state_bytes + b"\n")

    after_process = change_final_state
    try:
        with pytest.raises(TransactionError, match="^managed_entry_changed$"):
            host_module._entry(root)
    finally:
        state_path.write_bytes(state_bytes)
        after_process = unchanged
    assert processes == [controller] * 3 and len(routes) == 4
    assert host.record() == record and state_path.read_bytes() == state_bytes
    host.control.check()
    if host.writer is not None:
        host.writer.check()
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []
    # All real routing, bytes, process and refusal checks precede the old-code red:
    # its typed entry verifies the same complete controller twice, instead of once.
    assert typed_scans == 1


@pytest.mark.parametrize("metadata_host", ["adopt", "switch", "three"], indirect=True)
async def test_each_complete_slot_is_read_and_parsed_once_per_real_record(
    metadata_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = metadata_host
    record, disk = host.record(), host.backend._disk()
    reads = observe_metadata(host, monkeypatch)
    python_reads = observe_python_reads(host, monkeypatch)
    scans = track_slot_scans(monkeypatch)
    root = host.journal.directory.path
    slots = {slot.digest: slot for slot in (host.backend.controller_slot, *host.slots.values())}
    expected = {
        path.name: path for path in reads.expected if path.name.startswith(("anchor-", "slot-"))
    }
    strict_payloads = {reads.expected[path]: name for name, path in expected.items()}
    strict_parses: Counter[str] = Counter()
    slot_parses: Counter[str] = Counter()
    direct_reads: Counter[str] = Counter()
    snapshots: list[Role] = []
    walked: list[Path] = []
    current_reads = 0
    journal_syncs = 0
    original_object = deployment_module.object_bytes
    original_parse = SlotIdentity.model_validate_json
    original_at = managed_launcher.read_at
    original_directory = managed_launcher._open_directory
    original_current = JournalControl.read_current
    original_snapshots = host_module.read_snapshots
    original_sync = os.fsync

    def object_bytes(data: bytes) -> dict[str, Any]:
        result = original_object(data)
        if data in strict_payloads:
            strict_parses[strict_payloads[data]] += 1
        return result

    def parse_slot(
        cls: type[SlotIdentity], data: str | bytes | bytearray, **kwargs: Any
    ) -> SlotIdentity:
        result = original_parse(data, **kwargs)
        slot_parses[result.digest] += 1
        return result

    def read_at(
        descriptor: int, name: str, limit: int, *, private: bool = False
    ) -> tuple[bytes, dict[str, int]]:
        result = original_at(descriptor, name, limit, private=private)
        if name in expected or name == deployment_module.LAUNCHER_NAME:
            direct_reads[name] += 1
        return result

    def open_directory(path: Path) -> int:
        descriptor = original_directory(path)
        if path == root:
            walked.append(path)
        return descriptor

    def current(control: JournalControl) -> TransactionRecord | TransactionRecordV2 | None:
        nonlocal current_reads
        result = original_current(control)
        current_reads += 1
        return result

    def snapshot(
        journal: TransactionJournal, value: TransactionRecordV2, role: Role
    ) -> RoleSnapshots:
        result = original_snapshots(journal, value, role)
        snapshots.append(role)
        return result

    def synchronized(descriptor: int) -> None:
        nonlocal journal_syncs
        original_sync(descriptor)
        if descriptor == host.journal.directory.fd:
            journal_syncs += 1

    monkeypatch.setattr(deployment_module, "object_bytes", object_bytes)
    monkeypatch.setattr(SlotIdentity, "model_validate_json", classmethod(parse_slot))
    monkeypatch.setattr(deployment_module, "read_at", read_at)
    monkeypatch.setattr(managed_launcher, "read_at", read_at)
    monkeypatch.setattr(managed_launcher, "_open_directory", open_directory)
    monkeypatch.setattr(JournalControl, "read_current", current)
    monkeypatch.setattr(host_module, "read_snapshots", snapshot)
    monkeypatch.setattr(os, "fsync", synchronized)

    for invocation in (1, 2):
        actual, controller, roles = host_module._record(host.control, host.anchor)
        assert actual == record and controller == host.backend.controller_slot
        assert roles == host.slots
        reads.complete()
        assert scans == [Path(controller.package.root)] * invocation
        assert python_reads.bytes == controller.resolved_python.identity.size * invocation
        assert len(python_reads.opened) == invocation
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python_reads.opened)
        assert snapshots == ["base", "target"] * invocation
        assert current_reads == journal_syncs == 2 * invocation
        assert host.backend._disk() == disk
        host.control.check()
        if host.writer is not None:
            host.writer.check()
        assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []
        # Byte, nofollow, identity, package, snapshot and durability assertions
        # above must pass before old production fails this real-read oracle.
        assert {path.name: len(items) for path, items in reads.reads.items()} == {
            path.name: invocation for path in reads.expected
        }
        assert strict_parses == Counter({name: invocation for name in expected})
        assert slot_parses == Counter({digest: invocation for digest in slots})
        assert direct_reads == Counter(
            {**dict.fromkeys(expected, invocation), deployment_module.LAUNCHER_NAME: invocation}
        )
        assert walked == [root] * (2 * invocation)


@pytest.mark.parametrize("entry", ["host", "backend"])
async def test_entry_keeps_its_launcher_and_controller_metadata_error_order(
    metadata_host: Host, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    host = metadata_host
    record = host.record()
    root = host.journal.directory.path
    (root / f"slot-{host.backend.controller_slot.digest}.json").write_bytes(b"bad controller")
    Path(host.anchor.launcher.path).write_bytes(b"bad launcher")
    names: list[str] = []
    original = deployment_module.read_at

    def read_at(
        descriptor: int, name: str, limit: int, *, private: bool = False
    ) -> tuple[bytes, dict[str, int]]:
        names.append(name)
        return original(descriptor, name, limit, private=private)

    monkeypatch.setattr(deployment_module, "read_at", read_at)

    code = "anchor_unavailable" if entry == "host" else "managed_slot_unavailable"
    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        if entry == "host":
            host_module._record(host.control, host.anchor)
        else:
            host.backend._check(record, "target")

    assert names == [
        f"anchor-{host.anchor.digest}.json",
        deployment_module.LAUNCHER_NAME
        if entry == "host"
        else f"slot-{host.backend.controller_slot.digest}.json",
    ]

    assert host.record() == record
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("metadata_host", ["adopt"], indirect=True)
@pytest.mark.parametrize("boundary", ["controller", "base_snapshot"])
async def test_cached_slot_is_rechecked_at_its_original_read_before_later_errors(
    metadata_host: Host, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    host = metadata_host
    controller_path = (
        host.journal.directory.path / f"slot-{host.backend.controller_slot.digest}.json"
    )
    entered: list[str] = []

    def corrupt() -> None:
        assert not entered
        entered.append(boundary)
        controller_path.write_bytes(b"bad metadata before later failure")
        if boundary == "controller":
            (host.journal.directory.path / "state.json").write_bytes(b"bad later journal")

    if boundary == "controller":
        after_package_scan(host, monkeypatch, "controller", corrupt)
    else:
        original = host_module.read_snapshots

        def snapshots(
            journal: TransactionJournal, record: TransactionRecordV2, role: Role
        ) -> RoleSnapshots:
            if role == "target":
                raise AssertionError("damaged cached slot reached the later snapshot read")
            result = original(journal, record, role)
            corrupt()
            return result

        monkeypatch.setattr(host_module, "read_snapshots", snapshots)

    with pytest.raises(ManagedDeploymentError, match="^managed_slot_unavailable$"):
        host_module._record(host.control, host.anchor)
    assert entered == [boundary]
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("leaf", ["controller", "target", "launcher"])
@pytest.mark.parametrize("mutation", ["replace", "rewrite"])
async def test_final_snapshot_cannot_outlive_any_actual_metadata_leaf(
    metadata_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    leaf: str,
    mutation: Literal["replace", "rewrite"],
) -> None:
    host = metadata_host
    record, disk = host.record(), host.backend._disk()
    digest = (
        host.backend.controller_slot.digest if leaf == "controller" else host.slots["target"].digest
    )
    path = (
        Path(host.anchor.launcher.path)
        if leaf == "launcher"
        else host.journal.directory.path / f"slot-{digest}.json"
    )
    original = host_module.read_snapshots
    snapshots_seen: list[Role] = []

    def snapshots(
        journal: TransactionJournal, value: TransactionRecordV2, role: Role
    ) -> RoleSnapshots:
        result = original(journal, value, role)
        snapshots_seen.append(role)
        if role == "target":
            replace_or_rewrite(path, mutation)
        return result

    monkeypatch.setattr(host_module, "read_snapshots", snapshots)
    code = "anchor_unavailable" if leaf == "launcher" else "managed_slot_unavailable"
    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        host_module._record(host.control, host.anchor)
    assert snapshots_seen == ["base", "target"]
    assert host.record() == record and host.backend._disk() == disk
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("mutation", ["permissions", "ancestor"])
async def test_last_current_read_is_still_inside_the_private_root_fence(
    metadata_host: Host, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    host = metadata_host
    record = host.record()
    root = host.journal.directory.path
    ancestor = root.parent
    retained = ancestor.with_name(ancestor.name + ".retained")
    original = JournalControl.read_current
    reads = 0

    def current(control: JournalControl) -> TransactionRecord | TransactionRecordV2 | None:
        nonlocal reads
        result = original(control)
        reads += 1
        if reads == 2:
            if mutation == "permissions":
                root.chmod(0o755)
            else:
                ancestor.rename(retained)
                ancestor.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(JournalControl, "read_current", current)
    try:
        with pytest.raises((OSError, ValueError)):
            host_module._record(host.control, host.anchor)
    finally:
        if mutation == "permissions":
            root.chmod(0o700)
        elif retained.exists():
            ancestor.rmdir()
            retained.rename(ancestor)
    assert reads == 2 and host.record() == record
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("leaf", ["anchor", "target_permissions"])
async def test_final_current_cannot_outlive_anchor_or_leaf_permissions(
    metadata_host: Host, monkeypatch: pytest.MonkeyPatch, leaf: str
) -> None:
    host = metadata_host
    record = host.record()
    root = host.journal.directory.path
    path = root / (
        f"anchor-{host.anchor.digest}.json"
        if leaf == "anchor"
        else f"slot-{host.slots['target'].digest}.json"
    )
    original_mode = path.stat().st_mode & 0o777
    original = JournalControl.read_current
    reads = 0

    def current(control: JournalControl) -> TransactionRecord | TransactionRecordV2 | None:
        nonlocal reads
        result = original(control)
        reads += 1
        if reads == 2:
            if leaf == "anchor":
                replace_or_rewrite(path, "replace")
            else:
                path.chmod(0o644)
        return result

    monkeypatch.setattr(JournalControl, "read_current", current)
    code = "anchor_unavailable" if leaf == "anchor" else "managed_slot_unavailable"
    try:
        with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
            host_module._record(host.control, host.anchor)
    finally:
        path.chmod(original_mode)
    assert reads == 2 and host.record() == record
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("barrier", [1, 2])
async def test_journal_sync_error_keeps_first_error_and_poisons_control(
    metadata_host: Host, monkeypatch: pytest.MonkeyPatch, barrier: int
) -> None:
    host = metadata_host
    original_snapshot = host_module.read_snapshots
    original_sync = os.fsync
    synchronizations = 0

    def snapshots(
        journal: TransactionJournal, record: TransactionRecordV2, role: Role
    ) -> RoleSnapshots:
        result = original_snapshot(journal, record, role)
        if role == "target":
            replace_or_rewrite(Path(host.anchor.launcher.path), "replace")
        return result

    def synchronized(descriptor: int) -> None:
        nonlocal synchronizations
        if descriptor == host.journal.directory.fd:
            synchronizations += 1
            if synchronizations == barrier:
                raise OSError(errno.EIO, "first journal barrier failure")
        original_sync(descriptor)

    monkeypatch.setattr(host_module, "read_snapshots", snapshots)
    monkeypatch.setattr(os, "fsync", synchronized)
    with pytest.raises(OSError, match="first journal barrier failure"):
        host_module._record(host.control, host.anchor)
    with pytest.raises(TransactionError, match="^journal_unconfirmed$"):
        host.control.check()
    assert synchronizations == barrier
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []
