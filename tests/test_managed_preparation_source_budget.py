"""A source equal to its controller needs one full file verification per read."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_managed_backend_python_proof import CountedReader, PythonReads
from test_managed_boot import _slot
from test_managed_preparation import Host
from test_managed_preparation import host as host

import muli
from muli.managed_boot import ManagedBootPermit
from muli.platforms import managed_deployment
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    ManagedDeploymentError,
    SlotIdentity,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed source identities")


async def adopt(host: Host) -> ManagedBootPermit:
    host.complete(await host.run(host.plan("adopt"), candidate=host.slot))
    permit = host.prepare._permit()
    assert isinstance(permit, ManagedBootPermit)
    return permit


def observe_python_reads(
    slots: tuple[SlotIdentity, ...], monkeypatch: pytest.MonkeyPatch
) -> PythonReads:
    identities = [Path(slot.resolved_python.path).stat() for slot in slots]
    expected = {(info.st_dev, info.st_ino) for info in identities}
    reads = PythonReads()
    original = os.fdopen

    def stream(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        source = original(descriptor, *args, **kwargs)
        current = os.fstat(source.fileno())
        if (current.st_dev, current.st_ino) in expected:
            return CountedReader(source, reads)
        return source

    monkeypatch.setattr(os, "fdopen", stream)
    return reads


async def test_managed_source_equal_to_controller_hashes_python_once_per_source_read(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit = await adopt(host)
    previous = host.journal.read_current()
    position = host.runtime.store.event_position()
    changes = host.runtime.store.db.total_changes
    reads = observe_python_reads((host.slot,), monkeypatch)
    size = host.slot.resolved_python.identity.size
    async with host.runtime.configuration_lock:
        source = host.prepare._source(permit, None, "switch", None)
        assert source.previous == previous
        assert source.slot == source.controller == host.slot
        assert reads.bytes == size
        repeated = host.prepare._source(permit, None, "switch", None)
        assert repeated == source
        assert reads.bytes == 2 * size
    assert host.journal.read_current() == previous
    assert host.runtime.store.event_position() == position
    assert host.runtime.store.db.total_changes == changes


@pytest.mark.parametrize("material", ["python", "package"])
async def test_managed_source_still_rejects_changed_controller_files(
    host: Host, material: str
) -> None:
    permit = await adopt(host)
    previous = host.journal.read_current()
    changes = host.runtime.store.db.total_changes
    path = (
        Path(host.slot.resolved_python.path)
        if material == "python"
        else Path(host.slot.package.root) / "__init__.py"
    )
    path.write_bytes(path.read_bytes() + b"\nchanged\n")
    async with host.runtime.configuration_lock:
        with pytest.raises(ManagedDeploymentError, match="anchor_unavailable"):
            host.prepare._source(permit, None, "switch", None)
    assert host.journal.read_current() == previous
    assert host.runtime.store.db.total_changes == changes


@pytest.mark.parametrize("identity", ["executable", "package"])
async def test_managed_source_checks_process_after_full_anchor_verification(
    host: Host, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    permit = await adopt(host)
    previous = host.journal.read_current()
    changes = host.runtime.store.db.total_changes
    original = DeploymentStore.verify_anchor
    verified: list[DeploymentAnchor] = []

    def verify(deployments: DeploymentStore, anchor: DeploymentAnchor) -> None:
        original(deployments, anchor)
        verified.append(anchor)
        if identity == "executable":
            monkeypatch.setattr(
                managed_deployment,
                "sys",
                SimpleNamespace(
                    platform="linux",
                    executable=str(host.root / "foreign-python"),
                    prefix=host.slot.prefix,
                    base_prefix=str(host.root / "base-python"),
                ),
            )
        else:
            monkeypatch.setattr(muli, "__file__", str(host.root / "foreign/__init__.py"))

    monkeypatch.setattr(DeploymentStore, "verify_anchor", verify)
    async with host.runtime.configuration_lock:
        with pytest.raises(ManagedDeploymentError, match="managed_process_mismatch"):
            host.prepare._source(permit, None, "switch", None)
    assert len(verified) == 1
    assert host.journal.read_current() == previous
    assert host.runtime.store.db.total_changes == changes


async def test_managed_source_distinct_from_controller_still_hashes_both_slots(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adopt(host)
    target = _slot(host.root, "selected")
    prepared = await host.run(host.plan("switch", name="select"), candidate=target)
    system = SimpleNamespace(
        platform="linux",
        executable=target.lexical_python,
        prefix=target.prefix,
        base_prefix=str(host.root / "base-python"),
    )
    monkeypatch.setattr(managed_deployment, "sys", system)
    monkeypatch.setattr(muli, "__file__", str(Path(target.package.root) / "__init__.py"))
    previous = host.complete(prepared, boot="selected-boot")
    permit = host.prepare._permit()
    assert isinstance(permit, ManagedBootPermit)
    reads = observe_python_reads((host.slot, target), monkeypatch)
    changes = host.runtime.store.db.total_changes
    async with host.runtime.configuration_lock:
        source = host.prepare._source(permit, None, "switch", None)
        assert source.slot == target and source.controller == host.slot
        assert source.slot != source.controller
        assert reads.bytes == (
            host.slot.resolved_python.identity.size + target.resolved_python.identity.size
        )
        path = Path(target.package.root) / "__init__.py"
        path.write_bytes(b"changed selected package\n")
        with pytest.raises(ManagedDeploymentError, match="managed_slot_changed"):
            host.prepare._source(permit, None, "switch", None)
    assert host.journal.read_current() == previous
    assert host.runtime.store.db.total_changes == changes
