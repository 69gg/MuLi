"""Running observations retain fresh authentication at both probe boundaries."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_managed_backend import Host
from test_managed_backend import host as host
from test_managed_backend_package_batch import PackageReads, observe_checks, observe_payloads
from test_managed_backend_package_batch import package_payloads as package_payloads
from test_managed_backend_python_proof import observe_python_reads
from test_managed_deployment_authentication_batch import bounded_slots as bounded_slots

from muli.managed_controller import ManagedProcessIdentity, ManagedServiceObservation
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionError, TransactionJournal
from muli.platforms import managed_launcher
from muli.platforms.managed_backend import _DiskBinding
from muli.platforms.managed_deployment import DeploymentAnchor, ManagedDeploymentError, SlotIdentity
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


def observe_complete_package_scans(
    host: Host, monkeypatch: pytest.MonkeyPatch, *, adjacent_proofs: bool = False
) -> dict[Path, int]:
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
        # Ordinary observations retain independent full bytes at every await
        # boundary. Effect tests may explicitly observe adjacent byte proofs;
        # they still count every real original walk and its complete digest.
        if adjacent_proofs:
            result = original(root, limits, verified=verified, observed=observed)
        else:
            assert verified is None and observed is None
            result = original(root, limits)
        complete = result == expected[root]
        assert complete
        counts[root] += 1
        return result

    monkeypatch.setattr(managed_launcher, "package_fingerprint", scan)
    return counts


def assert_actual_payload_reads(
    reads: PackageReads,
    scans: dict[Path, int],
    *,
    reused: Mapping[Path, int] | None = None,
) -> None:
    for leaf, observed in reads.leaves.items():
        root = next(root for root in scans if leaf.is_relative_to(root))
        copies = scans[root]
        assert copies > 0
        assert len(observed.opened) == copies
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.fd_stats >= 2 * copies
        assert observed.path_stats == copies
        content_copies = copies - (0 if reused is None else reused.get(root, 0))
        assert content_copies >= 0
        assert observed.bytes == reads.sizes[leaf] * content_copies
    for directory, count in reads.scans.items():
        copies = next(value for root, value in scans.items() if directory.is_relative_to(root))
        assert count == copies


async def test_stop_reuses_one_complete_preflight_before_its_second_probe(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.move("draining")
    record = host.move("activating", "stop_base")
    controller = Path(host.backend.controller_slot.package.root)
    target = Path(host.slots["target"].package.root)
    assert controller != target
    disk, service = host.backend._disk(), host.probe.snapshot
    state_path = host.journal.directory.path / "state.json"
    state_bytes = state_path.read_bytes()
    writer = host.writer
    assert writer is not None and writer.fd is not None
    writer.check()
    writer_info = os.fstat(writer.fd)
    probes = observe_probes(host, monkeypatch)
    scans = observe_complete_package_scans(host, monkeypatch, adjacent_proofs=True)
    python_reads = observe_python_reads(host, monkeypatch)
    reads = observe_payloads(host, monkeypatch)
    checks = observe_checks(host, monkeypatch)
    commands: list[tuple[str, ...]] = []
    original_runner = host.backend.runner

    async def run(
        argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        commands.append(tuple(argv))
        return await original_runner(argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr)

    monkeypatch.setattr(host.backend, "runner", run)

    assert await host.backend.stop(record, "base") is None

    assert commands == [
        (
            "systemctl",
            "--user",
            "--no-block",
            "--job-mode=fail",
            "stop",
            host.backend.service.unit,
        )
    ]
    assert host.commands == ["stop"] and host.transport.calls == []
    assert len(probes) == host.probe.count == 2
    for probe in probes:
        same_service = probe.service == service
        assert same_service and probe.process_roles == ("base",) and probe.unit_confirmed
        assert probe.disk_unit == record.base.unit.digest
    assert host.backend._stopped(host.probe.snapshot)
    assert host.writer is None and writer.fd is None
    current_record = host.record()
    assert current_record == record and current_record.deadline == record.deadline
    assert state_path.read_bytes() == state_bytes and host.backend._disk() == disk
    host.control.check()
    with host.journal.writer_lease() as available:
        available.check()
        assert available.fd is not None
        current = os.fstat(available.fd)
        assert (current.st_dev, current.st_ino) == (writer_info.st_dev, writer_info.st_ino)
    host.control.check()

    copies = scans[controller]
    assert copies > 0 and scans[target] == 0
    assert checks == [(record, None)] * copies
    content_copies = copies - 2
    assert python_reads.bytes == (
        host.backend.controller_slot.resolved_python.identity.size * content_copies
    )
    assert len(python_reads.opened) == copies
    assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in python_reads.opened)
    for leaf, observed in reads.leaves.items():
        expected = copies if leaf.is_relative_to(controller) else 0
        assert leaf.is_relative_to(controller) or leaf.is_relative_to(target)
        expected_content = content_copies if leaf.is_relative_to(controller) else 0
        assert observed.bytes == reads.sizes[leaf] * expected_content
        assert len(observed.opened) == expected
        assert all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for flags in observed.opened)
        assert observed.fd_stats >= 2 * expected and observed.path_stats == expected
    for directory, count in reads.scans.items():
        assert count == (copies if directory.is_relative_to(controller) else 0)
    # All five independent authentication walks and lock/state gates remain;
    # only the two adjacent byte copies use their successful post-probe proofs.
    assert copies == 5


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


def _pause_second_stop_probe(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, asyncio.Future[None], list[BaseException]]:
    entered = asyncio.Event()
    release: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    errors: list[BaseException] = []
    original, calls = host.probe.read_managed, 0

    async def read(
        journal: TransactionJournal,
        record: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            try:
                await release
            except BaseException as error:
                errors.append(error)
                raise
        return await original(journal, record, anchor, controller, slots)

    monkeypatch.setattr(host.probe, "read_managed", read)
    return entered, release, errors


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("package", "anchor_unavailable"),
        ("anchor", "anchor_unavailable"),
        ("slot", "managed_slot_unavailable"),
        ("revision", "stale_revision"),
        ("package-and-observation", "anchor_unavailable"),
    ],
)
async def test_stop_reauthenticates_actual_evidence_after_its_second_await(
    host: Host, monkeypatch: pytest.MonkeyPatch, fault: str, code: str
) -> None:
    host.move("draining")
    record = expected_record = host.move("activating", "stop_base")
    disk, service, writer = host.backend._disk(), host.probe.snapshot, host.writer
    assert writer is not None
    entered, release, errors = _pause_second_stop_probe(host, monkeypatch)
    task = asyncio.create_task(host.backend.stop(record, "base"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        root, controller = host.journal.directory.path, host.backend.controller_slot
        if fault.startswith("package"):
            Path(controller.package.root, "__main__.py").write_bytes(b"changed during await")
            if fault == "package-and-observation":
                host.probe.snapshot = replace(
                    service, unit=replace(service.unit, job=(7, "/job/7"))
                )
        elif fault in {"anchor", "slot"}:
            digest = host.anchor.digest if fault == "anchor" else controller.digest
            (root / f"{fault}-{digest}.json").write_bytes(b"changed private metadata")
        else:
            expected_record = host.move("activating", "install_target")
        release.set_result(None)
        expected = TransactionError if fault == "revision" else ManagedDeploymentError
        with pytest.raises(expected, match=f"^{code}$"):
            await task
    finally:
        if not release.done():
            release.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert host.probe.count == 2 and not errors
    assert host.commands == [] and host.transport.calls == []
    assert host.backend._disk() == disk and host.writer is writer
    assert host.probe.snapshot.process == service.process
    assert host.record() == expected_record and expected_record.deadline == record.deadline
    writer.check()
    host.control.check()


@pytest.mark.parametrize("failure_kind", ["error", "cancel", "task-cancel"])
async def test_stop_preserves_second_probe_failure_and_cancellation_identity(
    host: Host, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    host.move("draining")
    record = host.move("activating", "stop_base")
    disk, service, writer = host.backend._disk(), host.probe.snapshot, host.writer
    assert writer is not None
    state_path = host.journal.directory.path / "state.json"
    state_bytes = state_path.read_bytes()
    entered, release, errors = _pause_second_stop_probe(host, monkeypatch)
    failure = RuntimeError("probe failed") if failure_kind == "error" else asyncio.CancelledError()
    task = asyncio.create_task(host.backend.stop(record, "base"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        if failure_kind == "task-cancel":
            assert task.cancel()
        else:
            release.set_exception(failure)
        with pytest.raises(type(failure)) as caught:
            await task
        assert len(errors) == 1 and caught.value is errors[0]
        if failure_kind != "task-cancel":
            assert caught.value is failure
    finally:
        if not release.done():
            release.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert host.probe.count == 1 and host.commands == [] and host.transport.calls == []
    assert host.probe.snapshot == service and host.backend._disk() == disk
    assert host.record() == record and state_path.read_bytes() == state_bytes
    assert host.writer is writer
    writer.check()
    host.control.check()


@pytest.mark.parametrize("corrupt_controller", [False, True])
async def test_stop_final_effect_guard_rechecks_authentication_before_original_deadline(
    host: Host, monkeypatch: pytest.MonkeyPatch, corrupt_controller: bool
) -> None:
    host.move("draining")
    record = host.move("activating", "stop_base")
    disk, service, writer = host.backend._disk(), host.probe.snapshot, host.writer
    assert writer is not None
    original_disk, reads = host.backend._disk, 0

    def read() -> _DiskBinding:
        nonlocal reads
        result = original_disk()
        reads += 1
        if reads == 2:
            host.clock.elapsed = record.deadline.seconds
            if corrupt_controller:
                Path(host.backend.controller_slot.package.root, "__main__.py").write_bytes(
                    b"changed before final effect"
                )
        return result

    monkeypatch.setattr(host.backend, "_disk", read)
    expected = ManagedDeploymentError if corrupt_controller else TimeoutError
    with pytest.raises(expected) as caught:
        await host.backend.stop(record, "base")
    if corrupt_controller:
        assert str(caught.value) == "anchor_unavailable"
    assert reads == 2 and host.probe.count == 2
    assert host.commands == [] and host.transport.calls == []
    assert host.probe.snapshot == service and original_disk() == disk
    assert host.record() == record and host.record().deadline == record.deadline
    assert host.writer is writer
    writer.check()
    host.control.check()
