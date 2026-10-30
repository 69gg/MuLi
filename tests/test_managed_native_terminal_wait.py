"""Real native transaction/journal and typed helper absence, without systemd."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import pytest
from helpers import managed_native_worker as worker
from test_linux_transaction import FakeBus
from test_managed_backend import Host
from test_managed_launcher import _slot
from test_managed_native_ci import forbid_real_effects as forbid_real_effects
from test_managed_native_helper_costs import costs_result

from muli.managed_host import managed_helper_arguments, managed_helper_unit_name
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role, TransactionJournal
from muli.platforms.linux_transaction import ObservationError, ProbeLimits, ServiceSnapshot
from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity
from muli.platforms.managed_probe import ManagedProbeResult, ManagedSystemdProbe

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journal/flock fixture")
Listing = Literal["active", "inactive", "empty", "failed", "malformed", "wrong-name", "cancel"]


class TerminalBus(FakeBus):
    """Only the raw manager replies are injected; the probe parses every reply."""

    def __init__(self, host: Host, argv: tuple[str, ...]) -> None:
        super().__init__(host.service)
        self.name = managed_helper_unit_name(host.record())
        self.mode: Listing = "active"
        self.rows.update(
            Id=("s", self.name),
            Type=("s", "exec"),
            ControlGroup=("s", f"/user.slice/user@{os.geteuid()}.service/{self.name}"),
            ExecStart=("a(sasbttttuii)", [[argv[0], list(argv), False, 0, 0, 0, 0, 123, 0, 0]]),
            Result=("s", "success"),
            ExecMainStatus=("i", 0),
        )

    async def __call__(
        self, argv: list[str], *, timeout: float, limit: int, merge_stderr: bool
    ) -> dict[str, Any]:
        if "ListUnitsByPatterns" not in argv:
            return await super().__call__(
                argv, timeout=timeout, limit=limit, merge_stderr=merge_stderr
            )
        self.calls.append(argv)
        assert 0 < timeout <= 30 and limit <= worker.MAX_BYTES and not merge_stderr
        assert "--auto-start=no" in argv and "--allow-interactive-authorization=no" in argv
        assert argv[-4:] == ["asas", "0", "1", self.name]
        if self.mode == "cancel":
            raise asyncio.CancelledError
        if self.mode == "failed":
            return {"exit_code": 1, "output": "", "stderr": "private manager error"}
        if self.mode == "malformed":
            return {"exit_code": 0, "output": "not typed JSON"}
        name = self.name
        if self.mode == "wrong-name":
            name = "muli-transaction-" + ("a" if "a" * 64 not in name else "b") * 64 + ".service"
        inactive = self.mode == "inactive"
        listing = [
            [
                name,
                "",
                "loaded",
                "inactive" if inactive else "active",
                "dead" if inactive else "running",
                "",
                "/org/freedesktop/systemd1/unit/helper",
                0,
                "",
                "/",
            ]
        ]
        return {
            "exit_code": 0,
            "output": json.dumps(
                {"type": "a(ssssssouso)", "data": [[]] if self.mode == "empty" else [listing]}
            ),
        }


@dataclass
class TerminalWait:
    host: Host
    case: worker.Case
    bus: TerminalBus
    probe: ManagedSystemdProbe
    initial: TransactionRecordV2
    argv: tuple[str, ...]

    def resolve(self) -> TransactionRecordV2:
        with self.host.journal.locked() as control:
            self.host.control = control
            self.host.move("verifying")
            self.host.move("committed", proof=self.host.proof())
            self.host.admission = "released"
            return self.host.move("committed", activation="released", proof=self.host.proof())

    async def bind_source(self) -> dict[str, Any]:
        self.case.result["generations"].append(
            {
                "deployment": self.initial.identity.deployment,
                "generation": self.initial.generation,
                "helper": self.bus.name,
            }
        )
        assert not await self.case.helper_empty(self.probe, self.bus.name, self.argv)
        source: dict[str, Any] = self.case.result["resources"]["helper_source"]
        assert source["generation"] == self.initial.generation and source["pid"] == 123
        return copy.deepcopy(source)

    def write_costs(self, source: dict[str, Any]) -> dict[str, Any]:
        _, costs = costs_result()
        costs.update(source)
        worker.write_json(self.case.root / "helper-costs.json", costs)
        worker.write_json(
            self.case.root / "helper-costs-checkpoint.json",
            {
                "sample": {"kind": "install_returned", "revision": self.initial.revision},
                "costs": costs,
            },
        )
        # This real earlier journal position authenticates the existing start
        # sample; the transaction's first HTTP poll will still see resolved.
        record = self.initial
        self.case.note(
            "transaction",
            request_id=record.request_id,
            generation=record.generation,
            revision=record.revision,
            phase=record.phase,
            activation=record.activation,
            intent=record.intent,
            code=record.code,
            failure_code=record.failure_code,
            rollback_attempted=record.rollback_attempted,
        )
        worker.write_json(
            self.case.root / "helper-start-checkpoint.json",
            {
                "sample": {
                    "kind": "start_command_returned",
                    "revision": record.revision,
                    "phase": record.phase,
                    "intent": record.intent,
                    "role": "target",
                    "elapsed_ms": 17,
                },
                "costs": costs,
            },
        )
        self.case._collect_helper_start_sample(record)
        return costs


@pytest.fixture
async def terminal_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TerminalWait]:
    def retained_slot(root: Path, name: str) -> SlotIdentity:
        assert name in {"base", "target"}
        return _slot(root, "a" if name == "base" else "b")

    monkeypatch.setattr("test_managed_backend._slot", retained_slot)
    directory = tmp_path / "data/management/transaction-v1"
    with TransactionJournal(directory, tmp_path / "data/muli.sqlite3") as journal:
        with journal.locked() as control:
            host = Host(tmp_path, journal, control, "adopt", change_config=False)
            host.release_writer()
            host.move("draining")
            host.move("activating", "stop_base")
            host.move("activating", "install_target")
            initial = host.move("activating", "start_target")
            host.running("target")
            host.release_writer()
            argv = managed_helper_arguments(host.anchor, host.slots["base"])
            bus = TerminalBus(host, argv)
        # Leave the controller lock free for Case.transaction's real final lock.
        probe = ManagedSystemdProbe(
            runner=bus,
            limits=ProbeLimits(seconds=worker.CASE_PROBE_SECONDS, command_bytes=worker.MAX_BYTES),
        )

        def create_probe(*, limits: ProbeLimits) -> ManagedSystemdProbe:
            assert 0 < limits.seconds <= worker.CASE_PROBE_SECONDS
            probe.limits = limits
            return probe

        monkeypatch.setattr("muli.platforms.managed_probe.ManagedSystemdProbe", create_probe)
        try:
            yield TerminalWait(host, worker.Case(tmp_path), bus, probe, initial, argv)
        finally:
            host.release_writer()
            await host.backend.__aexit__()


@dataclass
class Polls:
    records: list[TransactionRecordV2] = field(default_factory=list)
    pauses: int = 0


def finite_polls(
    observation: TerminalWait,
    monkeypatch: pytest.MonkeyPatch,
    modes: tuple[Listing, ...],
    *,
    resolved: bool = True,
) -> Polls:
    progress = Polls()
    record = observation.initial
    observation.bus.calls.clear()

    async def http(
        client: Any, method: str, path: str, data: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        nonlocal record
        if method == "POST":
            assert path == "/api/management/transactions"
            assert data is not None and data["action"] == "adopt"
            return 200, {"id": record.request_id, "routing": None}
        assert method == "GET" and path == "/api/management/transactions/current"
        assert len(progress.records) < len(modes), "transaction exceeded the finite poll sequence"
        if not progress.records and resolved:
            record = observation.resolve()
        observation.bus.mode = modes[len(progress.records)]
        progress.records.append(record)
        return 200, {"transaction": record.public()}

    async def pause() -> None:
        progress.pauses += 1
        assert len(progress.records) < len(modes), "unexpected retry after the last manager reply"
        assert not any(
            row.get("state") == "collected" for row in observation.case.result["timeline"]
        )

    monkeypatch.setattr(observation.case, "http", http)
    monkeypatch.setattr(observation.case, "pause", pause)
    return progress


class BarrierPassed(Exception):
    """Stop after the real completed-generation cost reader, before projection."""


def stop_after_costs(observation: TerminalWait, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    original = worker.collect_helper_costs
    calls: list[str] = []

    def collect(root: Path, result: dict[str, Any], **kwargs: Any) -> None:
        assert root == observation.case.root and result is observation.case.result
        assert observation.bus.mode == "empty"
        assert result["timeline"][-1]["state"] == "collected"
        calls.append(kwargs["completed_generation"])
        original(root, result, **kwargs)
        raise BarrierPassed

    monkeypatch.setattr(worker, "collect_helper_costs", collect)
    return calls


async def transact(observation: TerminalWait) -> str:
    return await observation.case.transaction(
        object(),
        "offline-password",
        "adopt",
        observation.host.slots["base"],
        observation.initial.identity.origin_boot,
    )


@pytest.mark.parametrize("state", ["active", "inactive"])
@pytest.mark.parametrize("shape", ["empty", "two-entries", "non-list"])
async def test_resolved_revision_waits_for_exact_absence_without_reading_exec_start(
    terminal_wait: TerminalWait, monkeypatch: pytest.MonkeyPatch, state: Listing, shape: str
) -> None:
    observation = terminal_wait
    source = await observation.bind_source()
    costs = observation.write_costs(source)
    starts = copy.deepcopy(observation.case._helper_start_samples)
    assert len(starts) == 1
    signature, entries = observation.bus.rows["ExecStart"]
    broken: object = [] if shape == "empty" else entries * 2 if shape == "two-entries" else None
    observation.bus.rows["ExecStart"] = (signature, broken)
    progress = finite_polls(observation, monkeypatch, (state, "empty"))
    calls = stop_after_costs(observation, monkeypatch)
    with pytest.raises(BarrierPassed):
        await transact(observation)
    assert len(progress.records) == 2 and progress.pauses == 1
    assert progress.records[0].resolved and progress.records[0] == progress.records[1]
    assert len(observation.bus.calls) == 2
    assert all("ListUnitsByPatterns" in call for call in observation.bus.calls)
    assert calls == [observation.initial.generation]
    assert observation.case.result["resources"]["helper_source"] == source
    assert observation.case._helper_cost_history == [{"source": source, "costs": costs}]
    assert len(observation.case._helper_cost_samples) == 1
    assert observation.case._helper_start_samples == starts
    assert observation.case.result["completed"] is False  # The sentinel precedes projection.


@pytest.mark.parametrize(
    "mode,code",
    [
        ("failed", "systemd_observation_unavailable"),
        ("malformed", "invalid_bus_reply"),
        ("wrong-name", "invalid_helper_listing"),
        ("cancel", None),
    ],
)
async def test_terminal_listing_failure_never_collects_or_reads_final_costs(
    terminal_wait: TerminalWait, monkeypatch: pytest.MonkeyPatch, mode: Listing, code: str | None
) -> None:
    observation = terminal_wait
    source = await observation.bind_source()
    observation.write_costs(source)
    observation.bus.rows["ExecStart"] = ("a(sasbttttuii)", [])
    progress = finite_polls(observation, monkeypatch, (mode,))
    calls = stop_after_costs(observation, monkeypatch)
    expected = asyncio.CancelledError if mode == "cancel" else ObservationError
    with pytest.raises(expected) as caught:
        await transact(observation)
    if code is not None:
        assert caught.value.args == (code,)
    assert len(progress.records) == 1 and progress.pauses == 0
    assert calls == [] and observation.case._helper_cost_history == []
    assert observation.case.result["resources"]["helper_source"] == source
    assert len(observation.bus.calls) == 1 and "ListUnitsByPatterns" in observation.bus.calls[0]
    assert not any(row.get("state") == "collected" for row in observation.case.result["timeline"])


@pytest.mark.parametrize("source_state", ["preserved", "missing", "revoked"])
async def test_terminal_cost_file_never_creates_or_restores_helper_source(
    terminal_wait: TerminalWait, monkeypatch: pytest.MonkeyPatch, source_state: str
) -> None:
    observation = terminal_wait
    source = (
        await observation.bind_source()
        if source_state != "missing"
        else {
            "generation": observation.initial.generation,
            "helper": observation.bus.name,
            "uid": os.geteuid(),
            "pid": 123,
            "invocation": bytes(range(16)).hex(),
        }
    )
    costs = observation.write_costs(source)
    if source_state == "revoked":
        assert len(observation.case._helper_start_samples) == 1
        observation.bus.rows["InvocationID"] = ("ay", [2] * 16)
        assert not await observation.case.helper_empty(
            observation.probe, observation.bus.name, observation.argv
        )
        assert observation.initial.generation in observation.case._invalid_helper_sources
        assert "helper_source" not in observation.case.result["resources"]
        assert observation.case._helper_start_samples == []
        observation.bus.rows["InvocationID"] = ("ay", list(range(16)))
    finite_polls(observation, monkeypatch, ("empty",))
    calls = stop_after_costs(observation, monkeypatch)
    with pytest.raises(BarrierPassed):
        await transact(observation)
    assert calls == [observation.initial.generation]
    if source_state == "preserved":
        assert observation.case.result["resources"]["helper_source"] == source
        assert observation.case._helper_cost_history == [{"source": source, "costs": costs}]
        assert len(observation.case._helper_start_samples) == 1
    else:
        assert "helper_source" not in observation.case.result["resources"]
        assert observation.case._helper_cost_history == observation.case._helper_cost_samples == []
        assert observation.case._helper_start_samples == []
    assert len(observation.bus.calls) == 1 and "ListUnitsByPatterns" in observation.bus.calls[0]


@pytest.mark.parametrize("shape", ["empty", "two-entries", "non-list"])
async def test_nonterminal_revision_keeps_the_real_snapshot_rejection(
    terminal_wait: TerminalWait, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    observation = terminal_wait
    signature, entries = observation.bus.rows["ExecStart"]
    observation.bus.rows["ExecStart"] = (
        signature,
        [] if shape == "empty" else entries * 2 if shape == "two-entries" else None,
    )
    progress = finite_polls(observation, monkeypatch, ("active",), resolved=False)
    calls = stop_after_costs(observation, monkeypatch)
    with pytest.raises(ObservationError, match="^unsupported_exec_start$"):
        await transact(observation)
    assert not progress.records[0].resolved and progress.pauses == 0
    assert calls == [] and len(observation.bus.calls) == 4
    assert "helper_source" not in observation.case.result["resources"]
    assert not any(row.get("state") == "collected" for row in observation.case.result["timeline"])


async def test_absence_keeps_the_existing_transaction_final_chain(
    terminal_wait: TerminalWait, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the final lock/decision/calls; final service and DB use offline seams."""
    observation = terminal_wait
    progress = finite_polls(observation, monkeypatch, ("inactive", "empty"))
    final_calls: list[str] = []

    async def read_managed(
        journal: TransactionJournal,
        record: TransactionRecordV2,
        anchor: DeploymentAnchor,
        controller: SlotIdentity,
        slots: dict[Role, SlotIdentity],
    ) -> ManagedProbeResult:
        assert record == progress.records[-1] and record.resolved
        assert journal.read_current() == record and anchor == observation.host.anchor
        assert controller == slots["base"] == observation.host.slots["base"]
        assert observation.bus.mode == "empty"
        final_calls.append("service")
        snapshot = observation.host.probe.snapshot
        assert snapshot.process is not None
        cgroup = f"/user.slice/user@{os.geteuid()}.service/muli.service"
        service = ServiceSnapshot(
            replace(snapshot.unit, cgroup=cgroup), replace(snapshot.process, cgroup=cgroup)
        )
        return ManagedProbeResult(
            service, ("target",), frozenset({record.target.unit.digest}), record.target.unit.digest
        )

    def read_database(record: TransactionRecordV2, *, expect_latest: bool) -> dict[str, Any]:
        assert record == progress.records[-1] and not expect_latest
        assert final_calls == ["service"]
        final_calls.append("database")
        return {"routing": None}

    monkeypatch.setattr(observation.probe, "read_managed", read_managed)
    monkeypatch.setattr(observation.case, "read_database", read_database)
    boot = await transact(observation)
    assert boot == observation.host.probe.snapshot.unit.invocation
    assert final_calls == ["service", "database"] and progress.pauses == 1
    assert len(observation.bus.calls) == 2
    assert all("ListUnitsByPatterns" in call for call in observation.bus.calls)
    assert observation.case.result["timeline"][-1]["stage"] == "projection"
    assert "helper_source" not in observation.case.result["resources"]
