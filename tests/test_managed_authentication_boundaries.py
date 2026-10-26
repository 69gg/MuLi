"""Authentication batches end after selected verification and never cross guards."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, Literal

import pytest
import test_managed_backend as fixtures
from test_managed_backend import Host
from test_managed_backend import host as host
from test_managed_backend_python_proof import CountedReader, PythonReads

from muli.managed_journal import RoleSnapshots, prepare_managed
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import ProjectionReceipt, _snapshot
from muli.platforms import managed_launcher
from muli.platforms.managed_deployment import (
    DeploymentStore,
    ManagedDeploymentError,
    SelectorValue,
    SlotIdentity,
    register_local_slot,
)
from muli.platforms.managed_launcher import PackageProofs

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux private deployment identities"
)


@pytest.fixture(autouse=True)
def bounded_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain the real Host/publisher and authenticate a tiny, never-run binary."""
    binary = tmp_path / "shared-python"
    binary.write_bytes(b"\x7fELF" + bytes(range(64)) * 17)
    binary.chmod(0o700)
    original = fixtures._slot

    def slot(root: Path, name: str) -> SlotIdentity:
        initial = original(root, name)
        entry = Path(initial.lexical_python)
        entry.unlink()
        entry.symlink_to(binary)
        return register_local_slot(Path(initial.prefix), entry, Path(initial.package.root))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("authentication fixture must not spawn a process")

    monkeypatch.setattr(fixtures, "_slot", slot)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


def after_package_scan(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    phase: Literal["controller", "selected"],
    inject: Callable[[], None],
) -> list[Path]:
    """Inject only after the original complete package scan has succeeded."""
    wanted = Path(
        host.backend.controller_slot.package.root
        if phase == "controller"
        else host.slots["target"].package.root
    )
    original = managed_launcher.package_fingerprint
    entered: list[Path] = []

    def fingerprint(
        root: Path,
        limits: Mapping[str, int],
        *,
        verified: dict[str, dict[str, Any]] | None = None,
        observed: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, object]:
        result = original(root, limits, verified=verified, observed=observed)
        if root == wanted:
            assert not entered
            entered.append(root)
            inject()
        return result

    monkeypatch.setattr(managed_launcher, "package_fingerprint", fingerprint)
    return entered


def authenticated_leaf(host: Host, leaf: Literal["metadata", "launcher"]) -> Path:
    if leaf == "launcher":
        return Path(host.anchor.launcher.path)
    return host.journal.directory.path / f"slot-{host.backend.controller_slot.digest}.json"


def replace_or_rewrite(path: Path, mutation: Literal["replace", "rewrite"]) -> None:
    """Keep bytes/size/mtime unchanged; inode or ctime must still revoke reuse."""
    before, payload = path.stat(), path.read_bytes()
    if mutation == "replace":
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(payload)
        replacement.chmod(stat.S_IMODE(before.st_mode))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        assert path.stat().st_ino != before.st_ino
    else:
        with path.open("r+b") as output:
            output.write(bytes([payload[0] ^ 1]))
            output.flush()
            output.seek(0)
            output.write(payload[:1])
            output.flush()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        assert after.st_ctime_ns != before.st_ctime_ns
    assert path.read_bytes() == payload


@pytest.mark.parametrize("phase", ["controller", "selected"])
@pytest.mark.parametrize("leaf", ["metadata", "launcher"])
@pytest.mark.parametrize("mutation", ["replace", "rewrite"])
async def test_actual_authenticated_leaf_changes_rejected_at_the_batch_end(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    phase: Literal["controller", "selected"],
    leaf: Literal["metadata", "launcher"],
    mutation: Literal["replace", "rewrite"],
) -> None:
    record, disk = host.record(), host.backend._disk()
    path = authenticated_leaf(host, leaf)
    entered = after_package_scan(
        host, monkeypatch, phase, lambda: replace_or_rewrite(path, mutation)
    )

    code = "managed_slot_unavailable" if leaf == "metadata" else "anchor_unavailable"
    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        host.backend._check(record, "target")

    assert len(entered) == 1
    assert host.record() == record and host.backend._disk() == disk
    assert host.writer is not None
    host.writer.check()
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("mutation", ["permissions", "path"])
async def test_selected_scan_cannot_outlive_the_private_root_chain(
    host: Host, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    record, disk = host.record(), host.backend._disk()
    root = host.journal.directory.path
    retained = root.with_name(root.name + ".retained")

    def change_root() -> None:
        if mutation == "permissions":
            root.chmod(0o755)
        else:
            root.rename(retained)
            root.mkdir(mode=0o700)

    entered = after_package_scan(host, monkeypatch, "selected", change_root)
    try:
        with pytest.raises(ValueError, match="^(?:managed_)?directory_(?:not_private|changed)$"):
            host.backend._check(record, "target")
    finally:
        # Keep the real journal/control/writer descriptors valid for teardown.
        if mutation == "permissions":
            root.chmod(0o700)
        elif retained.exists():
            root.rmdir()
            retained.rename(root)

    assert len(entered) == 1
    assert host.record() == record and host.backend._disk() == disk
    assert host.writer is not None
    host.writer.check()
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


@pytest.mark.parametrize("leaf", ["metadata", "launcher"])
async def test_next_guard_reauthenticates_metadata_even_with_real_prior_package_proofs(
    host: Host, leaf: Literal["metadata", "launcher"]
) -> None:
    record, disk = host.record(), host.backend._disk()
    proofs: PackageProofs = {}
    host.backend._check(record, "target", observed_packages=proofs)
    assert set(proofs) == {host.backend.controller_slot.digest, host.slots["target"].digest}
    path = authenticated_leaf(host, leaf)
    before = path.stat()
    payload = path.read_bytes()
    path.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert after.st_ctime_ns != before.st_ctime_ns

    code = "managed_slot_unavailable" if leaf == "metadata" else "anchor_unavailable"
    with pytest.raises(ManagedDeploymentError, match=f"^{code}$"):
        host.backend._check(record, "target", verified_packages=proofs)

    assert host.record() == record and host.backend._disk() == disk
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


def three_installations(host: Host) -> TransactionRecordV2:
    """Use the existing journal prepare/CAS path with three published digests."""
    old = host.move("aborted", code="cancelled")
    host.release_writer()
    controller = host.backend.controller_slot
    base = controller.model_copy(update={"python_bytes": controller.python_bytes - 1})
    assert base.resolved_python == controller.resolved_python
    assert base.package == controller.package and base.digest != controller.digest
    with DeploymentStore(host.journal.directory.path) as store:
        store.publish_slot(base, guard=host.control.check)
    previous_data = host.data["base"]
    selector = SelectorValue(deployment=host.anchor.digest, installation=base.digest).canonical()
    data = RoleSnapshots(previous_data.configuration, selector, previous_data.unit)
    binding = old.base.model_copy(
        update={"installation": base.digest, "selector": _snapshot(selector, 16384)}
    )
    with host.journal.writer_lease() as writer:
        record = prepare_managed(
            host.control,
            request_id="three-installations",
            action="switch",
            identity=old.identity,
            base=binding,
            target=old.target,
            base_data=data,
            target_data=host.data["target"],
            observed_format=old.observed_format,
            base_manifest=base.compatibility,
            target_manifest=host.slots["target"].compatibility,
            deadline=old.deadline,
            writer=writer,
            projected=ProjectionReceipt(
                **old.position.model_dump(), store_instance=old.identity.store_instance
            ),
        )
    host.slots["base"] = host.backend.slots["base"] = base
    host.data["base"] = data
    return record


async def test_distinct_complete_installations_never_collapse_to_a_shared_python_or_package(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = three_installations(host)
    slots = {slot.digest: slot for slot in (host.backend.controller_slot, *host.slots.values())}
    assert len(slots) == 3
    assert len({slot.resolved_python.digest for slot in slots.values()}) == 1
    expected = {f"slot-{digest}.json": slot.canonical() for digest, slot in slots.items()}
    seen: dict[str, list[bytearray]] = {name: [] for name in expected}
    identities: dict[tuple[int, int], str] = {}
    for name in expected:
        info = (host.journal.directory.path / name).stat()
        identities[info.st_dev, info.st_ino] = name
    original = os.fdopen

    class PayloadReader(CountedReader):
        def __init__(self, source: BinaryIO, payload: bytearray) -> None:
            super().__init__(source, PythonReads())
            self.payload = payload

        def read(self, size: int = -1) -> bytes:
            data = super().read(size)
            self.payload.extend(data)
            return data

    def stream(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original(descriptor, *args, **kwargs)
        info = os.fstat(source.fileno())
        name = identities.get((info.st_dev, info.st_ino))
        if name is None:
            return source
        payload = bytearray()
        seen[name].append(payload)
        return PayloadReader(source, payload)

    monkeypatch.setattr(os, "fdopen", stream)
    proofs: PackageProofs = {}
    host.backend._check(record, "target", observed_packages=proofs)

    assert set(proofs) == {host.backend.controller_slot.digest, host.slots["target"].digest}
    for name, payload in expected.items():
        assert hashlib.sha256(payload).hexdigest() == name.removeprefix("slot-").removesuffix(
            ".json"
        )
        assert [bytes(value) for value in seen[name]] == [payload]
    assert host.record() == record
    assert host.probe.count == 0 and host.commands == [] and host.transport.calls == []


async def test_broken_target_package_allows_base_rollback_but_target_metadata_never_does(
    host: Host,
) -> None:
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    host.move("activating", "start_target")
    host.running("target")
    record = host.move("rolling_back", "stop_target", code="backend_failed")
    Path(host.slots["target"].declaration.path).write_bytes(b"broken installed target")
    observation = await host.backend.observe(record)
    assert observation.service == "running" and observation.health is None
    await host.backend.stop(record, "target")
    restored = host.move("rolling_back", "restore_base", code="backend_failed")
    await host.backend.install(restored, "base", host.data["base"])
    started = host.move("rolling_back", "start_base", code="backend_failed")
    await host.backend.start(started, "base")
    assert host.commands == ["stop", "start"]
    assert host.probe.snapshot.process is not None
    assert host.probe.snapshot.process.argv == host.arguments("base")

    record, disk = host.record(), host.backend._disk()
    before_commands, before_http = list(host.commands), list(host.transport.calls)
    metadata = host.journal.directory.path / f"slot-{host.slots['target'].digest}.json"
    metadata.write_bytes(b"broken target metadata")
    with pytest.raises(ManagedDeploymentError, match="^managed_slot_unavailable$"):
        host.backend._check(record, "base")
    assert host.record() == record and host.backend._disk() == disk
    assert host.commands == before_commands and host.transport.calls == before_http
