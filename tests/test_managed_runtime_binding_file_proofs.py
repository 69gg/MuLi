"""A running host lends content only within one synchronous binding check."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_managed_boot as boot_fixtures
from test_managed_backend_python_proof import CountedReader, PythonReads
from test_managed_boot import Host
from test_managed_boot import host as host
from test_managed_runtime_binding import Running

import muli
from muli import managed_runtime_binding
from muli.maintenance import Maintenance
from muli.managed_runtime_binding import RuntimeBindingError
from muli.management_transaction import ClockReading
from muli.platforms import managed_deployment
from muli.platforms.managed_deployment import DeploymentAnchor, DeploymentStore, SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux retained host identities")

_PAYLOAD = Path("binding_proof/nested.bin")
_CONTENT = b"retained runtime binding content\n" * 11


@pytest.fixture
def bounded_running(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Running]:
    variant = getattr(request, "param", "same-slot")
    binary = tmp_path / "shared-binding-python"
    binary.write_bytes(b"\x7fELF" + bytes(range(256)) * 257)
    binary.chmod(0o700)
    original = boot_fixtures._slot

    def slot(root: Path, name: str) -> SlotIdentity:
        initial = original(root, name)
        target = binary
        if name == "target" and variant == "different-python":
            target = tmp_path / "other-binding-python"
            target.write_bytes(binary.read_bytes())
            target.chmod(0o700)
        python = Path(initial.lexical_python)
        python.unlink()
        python.symlink_to(target)
        payload = Path(initial.package.root) / _PAYLOAD
        payload.parent.mkdir()
        payload.write_bytes(
            b"different runtime binding bytes\n" * 11
            if name == "target" and variant == "different-content"
            else _CONTENT
        )
        return boot_fixtures.register_local_slot(
            Path(initial.prefix), python, Path(initial.package.root)
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("runtime binding fixture must not execute an interpreter")

    monkeypatch.setattr(boot_fixtures, "_slot", slot)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    source: Host = request.getfixturevalue("host")
    if variant == "different-scope":
        source.target = source.base.model_copy(
            update={"python_bytes": source.base.python_bytes - 1}
        )
        assert source.target.package == source.base.package
        assert source.target.digest != source.base.digest
        with DeploymentStore(source.journal.directory.path) as deployments:
            deployments.publish_slot(source.target, guard=lambda: None)
    record = source.record(
        "committed", "none", activation="released", legacy=variant == "same-slot"
    )
    permit = source.acquire()
    monkeypatch.setattr(managed_runtime_binding, "verify_local_filesystems", lambda paths: ())
    with closing(permit.open_store()) as store:
        yield Running(source, permit, store, record)


def binding_slots(running: Running) -> tuple[SlotIdentity, SlotIdentity]:
    controller = running.host.base
    selected = (
        controller
        if running.record.target.installation == controller.digest
        else running.host.target
    )
    return controller, selected


@dataclass
class BindingReads:
    files: dict[Path, PythonReads]
    scans: dict[Path, int]


def observe_reads(running: Running, monkeypatch: pytest.MonkeyPatch) -> BindingReads:
    slots = binding_slots(running)
    paths = {Path(slot.resolved_python.path) for slot in slots}
    paths.update(Path(slot.package.root) / _PAYLOAD for slot in slots)
    reads = {path: PythonReads() for path in paths}
    identities = {
        (info.st_dev, info.st_ino): reads[path] for path in paths for info in (path.stat(),)
    }
    roots = {Path(slot.package.root) for slot in slots}
    scans = {path: 0 for root in roots for path in (root, root / _PAYLOAD.parent)}
    directories = {(info.st_dev, info.st_ino): path for path in scans for info in (path.stat(),)}
    original_open, original_fdopen, original_scandir = os.open, os.fdopen, os.scandir

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        info = os.fstat(descriptor)
        observed = identities.get((info.st_dev, info.st_ino))
        if observed is not None:
            observed.opened.append(flags)
        return descriptor

    def stream(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original_fdopen(descriptor, *args, **kwargs)
        info = os.fstat(source.fileno())
        observed = identities.get((info.st_dev, info.st_ino))
        return source if observed is None else CountedReader(source, observed)

    def scandir(path: Any = ".") -> Any:
        if isinstance(path, int):
            info = os.fstat(path)
            current = directories.get((info.st_dev, info.st_ino))
        else:
            current = Path(os.fsdecode(path))
        if current in scans:
            scans[current] += 1
        return original_scandir(path)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", stream)
    monkeypatch.setattr(os, "scandir", scandir)
    return BindingReads(reads, scans)


@pytest.mark.parametrize(
    "bounded_running",
    ("same-slot", "same-content", "different-content", "different-python", "different-scope"),
    indirect=True,
)
def test_binding_pair_reopens_each_slot_but_reads_shared_content_once(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = bounded_running
    slots = binding_slots(running)
    reads = observe_reads(running, monkeypatch)
    changes, position = running.store.db.total_changes, running.store.event_position()
    for attempt in (1, 2):
        running.verify(running.record, "target")
        assert running.host.journal.read_current() == running.record
        assert running.store.db.total_changes == changes
        assert running.store.event_position() == position and not running.store.db.in_transaction
        running.permit.lease.check()
        for path, observed in reads.files.items():
            expected_opens = sum(
                path in {Path(slot.resolved_python.path), Path(slot.package.root) / _PAYLOAD}
                for slot in slots
            )
            assert len(observed.opened) == expected_opens * attempt
            assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        for root, count in reads.scans.items():
            assert count == sum(root.is_relative_to(slot.package.root) for slot in slots) * attempt
        # The real no-write/open/walk checks precede this old-code red oracle.
        for path in {Path(slot.resolved_python.path) for slot in slots}:
            assert reads.files[path].bytes == path.stat().st_size * attempt
        for path in {Path(slot.package.root) / _PAYLOAD for slot in slots}:
            scopes = {slot.digest for slot in slots if path == Path(slot.package.root) / _PAYLOAD}
            assert reads.files[path].bytes == path.stat().st_size * len(scopes) * attempt


def change_file(running: Running, fault: str) -> None:
    selected = binding_slots(running)[1]
    binary = Path(selected.resolved_python.path)
    payload = Path(selected.package.root) / _PAYLOAD
    if fault == "python-replace":
        replacement = binary.with_name("replacement-python")
        replacement.write_bytes(binary.read_bytes())
        replacement.chmod(0o700)
        replacement.replace(binary)
    elif fault == "python-link":
        replacement = binary.with_name("different-python")
        replacement.write_bytes(binary.read_bytes())
        replacement.chmod(0o700)
        lexical = Path(selected.lexical_python)
        lexical.unlink()
        lexical.symlink_to(replacement)
    elif fault in {"python-rewrite", "package-rewrite"}:
        path = binary if fault == "python-rewrite" else payload
        before = path.stat()
        path.write_bytes(b"x" * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_ctime_ns != before.st_ctime_ns
    elif fault == "package-new":
        payload.with_name("new.bin").write_bytes(b"unexpected binding member")
    elif fault == "package-delete":
        payload.unlink()
    else:
        payload.unlink()
        payload.symlink_to("../compatibility.json")


@pytest.mark.parametrize("bounded_running", ("same-slot", "same-content"), indirect=True)
@pytest.mark.parametrize(
    "fault",
    (
        "python-rewrite",
        "python-replace",
        "python-link",
        "package-rewrite",
        "package-new",
        "package-delete",
        "package-link",
    ),
)
def test_selected_recheck_rejects_changes_after_controller_authentication(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    running = bounded_running
    original = DeploymentStore.verify_anchor
    entered: list[DeploymentAnchor] = []

    def verify(deployments: DeploymentStore, anchor: DeploymentAnchor, **kwargs: Any) -> None:
        original(deployments, anchor, **kwargs)
        assert not entered
        entered.append(anchor)
        change_file(running, fault)

    monkeypatch.setattr(DeploymentStore, "verify_anchor", verify)
    changes, position = running.store.db.total_changes, running.store.event_position()
    with pytest.raises(RuntimeBindingError):
        running.verify(running.record, "target")
    assert entered == [running.host.anchor]
    assert running.host.journal.read_current() == running.record
    assert running.store.db.total_changes == changes and running.store.event_position() == position


@pytest.mark.parametrize("bounded_running", ("same-slot", "different-content"), indirect=True)
@pytest.mark.parametrize("fault", ("python-rewrite", "package-rewrite"))
def test_next_binding_reads_changed_content_and_rejects_it(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    running = bounded_running
    selected = binding_slots(running)[1]
    path = (
        Path(selected.resolved_python.path)
        if fault == "python-rewrite"
        else Path(selected.package.root) / _PAYLOAD
    )
    reads = observe_reads(running, monkeypatch)
    running.verify(running.record, "target")
    previous_bytes = reads.files[path].bytes
    change_file(running, fault)
    changes, position = running.store.db.total_changes, running.store.event_position()
    with pytest.raises(RuntimeBindingError):
        running.verify(running.record, "target")
    assert reads.files[path].bytes - previous_bytes == path.stat().st_size
    assert running.host.journal.read_current() == running.record
    assert running.store.db.total_changes == changes and running.store.event_position() == position


@pytest.mark.parametrize("identity", ("executable", "prefix", "package"))
def test_selected_recheck_keeps_the_actual_process_identity(
    bounded_running: Running, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    running = bounded_running
    original = DeploymentStore.verify_anchor
    entered: list[DeploymentAnchor] = []

    def verify(deployments: DeploymentStore, anchor: DeploymentAnchor, **kwargs: Any) -> None:
        original(deployments, anchor, **kwargs)
        entered.append(anchor)
        foreign = str(running.host.journal.directory.path / "foreign-process")
        if identity == "package":
            monkeypatch.setattr(muli, "__file__", foreign)
        else:
            monkeypatch.setattr(
                managed_deployment,
                "sys",
                SimpleNamespace(**{**vars(managed_deployment.sys), identity: foreign}),
            )

    monkeypatch.setattr(DeploymentStore, "verify_anchor", verify)
    changes, position = running.store.db.total_changes, running.store.event_position()
    with pytest.raises(RuntimeBindingError, match="^runtime_binding_verification_failed$"):
        running.verify(running.record, "target")
    assert entered == [running.host.anchor]
    assert running.host.journal.read_current() == running.record
    assert running.store.db.total_changes == changes and running.store.event_position() == position


@pytest.mark.parametrize("bounded_running", ("same-slot", "different-content"), indirect=True)
@pytest.mark.parametrize("fault", ("python-rewrite", "package-rewrite"))
async def test_activation_await_requires_fresh_binding_before_release(
    bounded_running: Running, fault: str
) -> None:
    running = bounded_running
    running.record = running.host.record(
        "committed",
        "none",
        legacy=running.record.action == "adopt",
    )
    record = running.record
    maintenance = Maintenance(running.permit)
    maintenance.admission.pause()
    maintenance.prepared(record.identity.store_instance, record.identity.main_session)
    maintenance.seal(set())
    maintenance.bind_managed_verifier(running.verify)
    entered: list[str] = []

    async def activate() -> None:
        entered.append("entered")
        await asyncio.sleep(0)
        change_file(running, fault)
        entered.append("changed")

    def clock() -> ClockReading:
        return record.deadline.created

    maintenance.bind_activation(activate)
    changes, position = running.store.db.total_changes, running.store.event_position()
    with pytest.raises(RuntimeBindingError):
        await maintenance.activate(record.generation, running.permit.boot, record.challenge, clock)
    assert entered == ["entered", "changed"]
    assert not maintenance.admission.allowed and maintenance._activation_failed
    assert running.host.journal.read_current() == record
    assert running.store.db.total_changes == changes and running.store.event_position() == position
    running.permit.lease.check()
