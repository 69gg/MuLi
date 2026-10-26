"""Running observations retain fresh authentication at both probe boundaries."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from test_managed_backend import Host
from test_managed_backend import host as host
from test_managed_backend_package_batch import PackageReads, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_deployment_authentication_batch import bounded_slots as bounded_slots

from muli.managed_controller import ManagedProcessIdentity, ManagedServiceObservation
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionJournal
from muli.platforms import managed_launcher
from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity
from muli.platforms.managed_probe import ManagedProbeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed observations")


async def running_target(host: Host) -> TransactionRecordV2:
    """Use the real journal/install guards, with only manager and HTTP faked."""
    record = host.install_intent()
    await host.backend.install(record, "target", host.data["target"])
    host.move("activating", "start_target")
    host.running("target")
    record = host.move("verifying")
    host.probe.count = 0
    return record


def observe_probes(host: Host, monkeypatch: pytest.MonkeyPatch) -> list[ManagedProbeResult]:
    original = host.probe.read_managed
    results: list[ManagedProbeResult] = []

    async def read(
        journal: TransactionJournal,
        record: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        result = await original(journal, record, anchor, controller, slots)
        results.append(result)
        return result

    monkeypatch.setattr(host.probe, "read_managed", read)
    return results


def observe_complete_package_scans(host: Host, monkeypatch: pytest.MonkeyPatch) -> dict[Path, int]:
    expected = {
        Path(slot.package.root): slot.package.model_dump(mode="json")
        for slot in (host.backend.controller_slot, *host.slots.values())
    }
    counts = {root: 0 for root in expected}
    original = managed_launcher.package_fingerprint

    def scan(
        root: Path,
        limits: Mapping[str, int],
        *,
        verified: dict[str, dict[str, Any]] | None = None,
        observed: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, object]:
        # Every admitted scan still runs the complete original bytes/SHA walk.
        # Neither await boundary may inherit a previous package proof.
        assert verified is None and observed is None
        result = original(root, limits)
        complete = result == expected[root]
        assert complete
        counts[root] += 1
        return result

    monkeypatch.setattr(managed_launcher, "package_fingerprint", scan)
    return counts


def assert_actual_payload_reads(reads: PackageReads, scans: dict[Path, int]) -> None:
    for leaf, observed in reads.leaves.items():
        copies = next(count for root, count in scans.items() if leaf.is_relative_to(root))
        assert copies > 0
        assert len(observed.opened) == copies
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.fd_stats >= 2 * copies
        assert observed.path_stats == copies
        assert observed.bytes == reads.sizes[leaf] * copies
    for directory, count in reads.scans.items():
        copies = next(value for root, value in scans.items() if directory.is_relative_to(root))
        assert count == copies


@pytest.mark.parametrize(
    "host",
    [("switch", False), ("adopt", False)],
    indirect=True,
    ids=("distinct-role", "same-role"),
)
async def test_running_observation_authenticates_each_probe_boundary_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = await running_target(host)
    before, disk = host.probe.snapshot, host.backend._disk().digests
    commands = list(host.commands)
    expected_health = host.proof()
    probes = observe_probes(host, monkeypatch)
    scans = observe_complete_package_scans(host, monkeypatch)
    reads = observe_payloads(host, monkeypatch)

    actual = await host.backend.observe(record)

    expected = ManagedServiceObservation(
        identity_matches=True,
        configuration=disk[0],
        selector=disk[1],
        unit=disk[2],
        service="running",
        boot=before.unit.invocation,
        writer_free=False,
        process=ManagedProcessIdentity(
            record.target.entry, record.target.installation, record.target.compatibility
        ),
        health=expected_health,
    )
    # Keep composite comparisons out of pytest's recursive assertion renderer.
    complete = actual == expected
    assert complete
    assert len(probes) == host.probe.count == 2
    for probe in probes:
        same_snapshot = probe.service == before
        assert same_snapshot
        assert probe.process_roles == ("target",)
        assert probe.loaded_units == frozenset({record.target.unit.digest})
        assert probe.disk_unit == record.target.unit.digest and probe.unit_confirmed
    assert host.transport.calls == ["readiness"]
    assert host.commands == commands and host.backend._disk().digests == disk
    same_record = host.record() == record
    assert same_record
    assert host.writer is not None
    host.writer.check()
    assert_actual_payload_reads(reads, scans)
    controller = Path(host.backend.controller_slot.package.root)
    target = Path(host.slots["target"].package.root)
    if target != controller:
        assert scans[target] == 2
    # All full proof, manager, HTTP and real FD/bytes gates above pass first.
    # The old path repeats its controller scan six times; one complete post-
    # probe role authentication should subsume each adjacent controller check.
    assert scans[controller] == 4


async def test_next_observation_independently_reads_every_package_again(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = await running_target(host)
    disk, commands = host.backend._disk().digests, list(host.commands)
    scans = observe_complete_package_scans(host, monkeypatch)
    reads = observe_payloads(host, monkeypatch)

    first = await host.backend.observe(record)
    first_scans = dict(scans)
    first_bytes = {path: observed.bytes for path, observed in reads.leaves.items()}
    second = await host.backend.observe(record)

    same_observation = second == first
    assert same_observation
    assert host.probe.count == 4 and host.transport.calls == ["readiness", "readiness"]
    assert all(first_scans.values())
    assert scans == {root: count * 2 for root, count in first_scans.items()}
    assert_actual_payload_reads(reads, scans)
    for path, observed in reads.leaves.items():
        assert first_bytes[path] > 0
        assert observed.bytes - first_bytes[path] == first_bytes[path]
    assert host.commands == commands and host.backend._disk().digests == disk
    same_record = host.record() == record
    assert same_record
    assert host.writer is not None
    host.writer.check()
