"""Finite probe evidence uses fake leases and never starts PowerShell."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from helpers import uia_probe_diagnostics as diagnostic
from test_uia_pending_observation import _Harness

from muli.tools import ax_lease, uia_computer
from muli.tools.ax_lease import MAX_STDERR_BYTES, AXHelperLease, AXLeaseError

STAGES = (
    "script_entered",
    "utility_importing",
    "utility_ready",
    "input_waiting",
    "input_line_received",
    "request_decoded",
    "probe_received",
    "uia_initializing",
    "uia_ready",
    "walker_initializing",
    "walker_ready",
    "probe_process_ready",
    "probe_encoded",
    "probe_write_returned",
)
PREFIX = b"muli-uia-stage=1:"
NOTE_PREFIX = "uia_probe_diagnostic="
UIA_INITIALIZATION_STAGES = ("uia_host_validated", "uia_add_type_returned")
ALL_STAGES = (*STAGES[:8], *UIA_INITIALIZATION_STAGES, *STAGES[8:])


def stage_bytes(*stages: str) -> bytes:
    return b"".join(PREFIX + stage.encode("ascii") + b"\r\n" for stage in stages)


@pytest.fixture(autouse=True)
def forbid_processes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Portable probe evidence must not start a real process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    yield


@dataclass
class _Process:
    returncode: int | None = None


class _Probe:
    def __init__(
        self,
        *,
        failure: BaseException | None = None,
        cleanup_failure: BaseException | None = None,
        stderr: bytes = b"",
    ) -> None:
        self.failure, self.cleanup_failure = failure, cleanup_failure
        self.closed = False
        self._stderr = bytearray(stderr)
        self._process = _Process()
        self._readers: list[asyncio.Task[None]] = []
        self._count = 0
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.calls = 0

    async def request(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert command == "probe" and payload == {}
        self.calls += 1
        self._count = 1
        if self.failure is not None:
            raise self.failure
        return {"probe": "complete"}

    async def aclose(self) -> None:
        self.cleanup_entered.set()
        await self.cleanup_release.wait()
        if self.cleanup_failure is not None:
            raise self.cleanup_failure
        self._process.returncode = 7
        self.closed = True


def note(error: BaseException) -> dict[str, Any]:
    notes = [value for value in getattr(error, "__notes__", ()) if value.startswith(NOTE_PREFIX)]
    assert len(notes) == 1 and len(notes[0].encode()) <= 4096
    return cast(dict[str, Any], json.loads(notes[0][len(NOTE_PREFIX) :]))


async def test_native_probe_failure_attaches_fixed_evidence_only_after_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_uia_native as native

    failure = AXLeaseError("read_timeout", "original private failure")
    probe = _Probe(failure=failure, stderr=stage_bytes(*STAGES[:4]) + b"private raw stderr\n")
    probe.cleanup_release.clear()

    class Computer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._uia = SimpleNamespace(_new_lease=lambda **_: probe)

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(native, "Computer", Computer)
    form = native.NativeForm(cast(asyncio.subprocess.Process, object()), "owned", asyncio.Queue())
    operation = asyncio.create_task(
        native.test_real_windows_system_module_probe_mta_and_owned_unicode_tree(
            form, tmp_path, monkeypatch
        )
    )
    try:
        async with asyncio.timeout(2):
            await probe.cleanup_entered.wait()
        assert not getattr(failure, "__notes__", ())
        probe.cleanup_release.set()
        with pytest.raises(AXLeaseError) as caught:
            await operation
        assert caught.value is failure and caught.value.outcome == "not_started"
        result = note(caught.value)
        assert result["stages"] == list(STAGES[:4])
        assert result["cleanup"] == "confirmed" and result["exit"] == "nonzero"
        assert result["sent"] == 1 and result["readers_settled"] is True
        assert "private" not in json.dumps(result)
        assert probe.calls == 1 and probe.closed
    finally:
        probe.cleanup_release.set()
        await asyncio.gather(operation, return_exceptions=True)


def test_packaged_script_keeps_sixteen_stage_names_and_fourteen_live_points() -> None:
    from muli.tools import uia_computer

    source = Path(uia_computer.__file__).with_name("uia_helper.ps1").read_text(encoding="utf-8")
    lines = [PREFIX.decode() + stage for stage in ALL_STAGES]
    assert all(source.count("'" + line + "'") == 1 for line in lines)
    assert len(stage_bytes(*STAGES)) <= 512
    assert len(stage_bytes(*ALL_STAGES)) <= MAX_STDERR_BYTES
    function = source.split("function Write-Stage", 1)[1].split("\nWrite-Stage 0", 1)[0]
    assert function.index("$script:traceMask = $script:traceMask -bor $bit") < function.index(
        "[Console]::Error.WriteLine"
    )
    assert "$script:traceMask -band $bit" in function and "catch { }" in function
    assert "$index -ge $script:traceStages.Length" in function
    assert source.count("Write-Stage ") == len(ALL_STAGES) - 2
    assert "Write-Stage 11" not in source and "Write-Stage 12" not in source
    assert "if (-not $script:traceProbeSeen)" in source
    assert source.index("Write-Stage 6") < source.index("$writer.WriteLine($json)")
    assert source.index("Write-Stage 7") > source.index("$writer.WriteLine($json)")


def test_first_input_stages_surround_the_original_read_and_decode_only() -> None:
    from muli.tools import uia_computer

    source = Path(uia_computer.__file__).with_name("uia_helper.ps1").read_text(encoding="utf-8")
    loop = source.split("for ($requestCount = 0; $requestCount -lt 16; $requestCount++)", 1)[1]
    points = (
        "if ($requestCount -eq 0) { Write-Stage 8 }",
        "$line = Read-BoundedLine",
        "if ($null -eq $line) { break }",
        "if ($requestCount -eq 0) { Write-Stage 9 }",
        "$request = Microsoft.PowerShell.Utility\\ConvertFrom-Json -InputObject $line",
        "if ($requestCount -eq 0) { Write-Stage 10 }",
        "if ($request.v -ne 1",
        "Write-Stage 3",
    )
    positions = [loop.index(point) for point in points]
    assert positions == sorted(positions)
    assert all(source.count(point) == 1 for point in (points[0], points[3], points[5]))
    assert source.index("Write-Stage 2") < source.index(points[0])


def test_probe_skips_walker_and_keeps_the_real_process_ready_stage() -> None:
    from muli.tools import uia_computer

    assert uia_computer.__file__ is not None
    source = Path(uia_computer.__file__).with_name("uia_helper.ps1").read_text(encoding="utf-8")
    guard = "if ([string]$request.command -cne 'probe') {"
    walker = "$script:walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker"
    initialization = source.split(guard, 1)[1].split("\n            }", 1)[0]
    assert walker in initialization
    assert "$script:rows = [Collections.Generic.List[object]]::new()" in initialization
    assert "$script:reasons = [Collections.Generic.HashSet[string]]::new()" in initialization
    assert "$script:visited = 0" in initialization
    points = (
        "Initialize-Uia\n            Check-Deadline",
        guard,
        walker,
        "switch -CaseSensitive ([string]$request.command)",
        "'probe' {",
        "$process = [Diagnostics.Process]::GetCurrentProcess()",
        "try { $hasWindow = ($process.MainWindowHandle -ne [IntPtr]::Zero) }",
        "finally { $process.Dispose() }",
        "if ($traceProbe) { Write-Stage 13 }",
        "$json = Encode-Result $requestId @{",
        "Write-Stage 6",
    )
    positions = [source.index(point) for point in points]
    assert positions == sorted(positions)
    assert all(source.count(point) == 1 for point in points[1:10])
    assert source.index("$traceProbe = $true") < positions[0]


def test_probe_initialization_stages_surround_existing_checks_and_assembly_load() -> None:
    source = Path(uia_computer.__file__).with_name("uia_helper.ps1").read_text(encoding="utf-8")
    trace = source.split("$script:traceStages = @(", 1)[1].split("\n)", 1)[0]
    assert all(trace.count("'" + PREFIX.decode() + stage + "'") == 1 for stage in STAGES)
    for stage in UIA_INITIALIZATION_STAGES:
        assert trace.count("'" + PREFIX.decode() + stage + "'") == 1
    assert trace.index("probe_process_ready") < trace.index(UIA_INITIALIZATION_STAGES[0])
    initialization = source.split("function Initialize-Uia {", 1)[1].split(
        "\nfunction Check-Deadline", 1
    )[0]
    points = (
        "Write-Stage 4",
        "$PSVersionTable.PSVersion.Major -ne 5",
        "[Threading.Thread]::CurrentThread.GetApartmentState()",
        "Write-Stage 14",
        "Microsoft.PowerShell.Utility\\Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes",
        "Write-Stage 15",
        "[System.Windows.Automation.AutomationElement].Assembly.GlobalAssemblyCache",
        "[System.Windows.Automation.ControlType].Assembly.GlobalAssemblyCache",
        "$script:ready = $true",
        "Write-Stage 5",
    )
    positions = [initialization.index(point) for point in points]
    assert positions == sorted(positions)
    assert initialization.count(points[4]) == 1


def test_probe_parser_accepts_new_initialization_markers_and_original_fourteen_stages() -> None:
    for stages in (STAGES, ALL_STAGES):
        result = diagnostic.probe_facts(
            cast(AXHelperLease, _Probe(stderr=stage_bytes(*stages))), cleanup_failed=False
        )
        assert result["stages"] == list(stages)
        assert result["stage_sequence"] == "valid"


def test_every_bounded_prefix_keeps_only_complete_in_order_input_stage_lines() -> None:
    from helpers.uia_probe_diagnostics import probe_facts

    frame = stage_bytes(*STAGES)
    assert len(frame) <= 512 < MAX_STDERR_BYTES
    boundaries = [len(stage_bytes(*STAGES[:count])) for count in range(1, len(STAGES) + 1)]
    for end in range(len(frame) + 1):
        complete = sum(boundary <= end for boundary in boundaries)
        # Fill the retained prefix with an unknown complete line, leaving only
        # this many marker bytes; later complete markers must not leak through.
        capped = b"x" * (MAX_STDERR_BYTES - end - 1) + b"\n" + frame
        for data in (frame[:end], capped):
            result = probe_facts(cast(AXHelperLease, _Probe(stderr=data)), cleanup_failed=False)
            assert result["stages"] == list(STAGES[:complete])
            assert result["stage_sequence"] == ("valid" if complete else "unobserved")


def test_old_eight_stage_prefix_remains_valid_without_new_input_markers() -> None:
    from helpers.uia_probe_diagnostics import probe_facts

    stages = tuple(
        stage
        for stage in STAGES
        if stage
        not in {
            "input_waiting",
            "input_line_received",
            "request_decoded",
            "walker_initializing",
            "walker_ready",
            "probe_process_ready",
        }
    )
    result = probe_facts(
        cast(AXHelperLease, _Probe(stderr=stage_bytes(*stages))), cleanup_failed=False
    )
    assert len(stages) == 8
    assert result["stages"] == list(stages) and result["stage_sequence"] == "valid"


def test_old_eleven_stage_prefix_remains_valid_without_native_getter_markers() -> None:
    from helpers.uia_probe_diagnostics import probe_facts

    stages = tuple(
        stage
        for stage in STAGES
        if stage not in {"walker_initializing", "walker_ready", "probe_process_ready"}
    )
    result = probe_facts(
        cast(AXHelperLease, _Probe(stderr=stage_bytes(*stages))), cleanup_failed=False
    )
    assert len(stages) == 11
    assert result["stages"] == list(stages) and result["stage_sequence"] == "valid"


def test_projection_is_bounded_and_never_exports_unknown_stderr() -> None:
    from helpers.uia_probe_diagnostics import probe_facts

    probe = _Probe(stderr=stage_bytes(*STAGES) + b"secret-provider-path=private\n")
    probe.closed, probe._process.returncode = True, 0
    result = probe_facts(cast(AXHelperLease, probe), cleanup_failed=False)
    assert result == {
        "stages": list(STAGES),
        "stage_sequence": "valid",
        "stderr_prefix_capped": False,
        "sent": 0,
        "lease_closed": True,
        "process_exists": True,
        "exit": "zero",
        "readers_settled": True,
        "cleanup": "confirmed",
    }
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    ("data", "stages", "sequence"),
    [
        (b"private\n", [], "unobserved"),
        (PREFIX + b"script_entered", [], "unobserved"),
        (stage_bytes(STAGES[0], STAGES[0]), [STAGES[0]], "invalid"),
        (stage_bytes(STAGES[2], STAGES[0]), [STAGES[2], STAGES[0]], "invalid"),
        (stage_bytes(STAGES[0], "unrecognized", STAGES[2]), [STAGES[0], STAGES[2]], "valid"),
        (stage_bytes("input_waiting", "input_waiting"), ["input_waiting"], "invalid"),
        (
            stage_bytes("request_decoded", "input_line_received"),
            ["request_decoded", "input_line_received"],
            "invalid",
        ),
        (stage_bytes("walker_ready", "walker_ready"), ["walker_ready"], "invalid"),
        (
            stage_bytes("probe_process_ready", "walker_ready"),
            ["probe_process_ready", "walker_ready"],
            "invalid",
        ),
    ],
    ids=[
        "unknown",
        "partial",
        "duplicate",
        "out-of-order",
        "fixed-only",
        "input-duplicate",
        "input-out-of-order",
        "walker-duplicate",
        "native-getter-out-of-order",
    ],
)
def test_only_complete_known_lines_contribute_stage_evidence(
    data: bytes, stages: list[str], sequence: str
) -> None:
    from helpers.uia_probe_diagnostics import probe_facts

    result = probe_facts(cast(AXHelperLease, _Probe(stderr=data)), cleanup_failed=False)
    assert result["stages"] == stages and result["stage_sequence"] == sequence


def test_retained_prefix_cap_cannot_find_later_markers_or_claim_reap() -> None:
    from helpers.uia_probe_diagnostics import probe_facts

    probe = _Probe(stderr=b"x" * MAX_STDERR_BYTES + b"\n" + stage_bytes(*STAGES))
    result = probe_facts(cast(AXHelperLease, probe), cleanup_failed=False)
    assert result["stages"] == [] and result["stderr_prefix_capped"] is True
    assert result["cleanup"] == "unconfirmed" and result["exit"] == "running"
    assert len(json.dumps(result).encode()) < 4096


async def test_cleanup_error_never_replaces_original_fault_or_claims_reap() -> None:
    from helpers.uia_probe_diagnostics import observe_probe

    failure = AXLeaseError("action_unknown_outcome", "private", "unknown")
    probe = _Probe(failure=failure, cleanup_failure=RuntimeError("private cleanup"))
    with pytest.raises(AXLeaseError) as caught:
        await observe_probe(cast(AXHelperLease, probe))
    assert caught.value is failure and caught.value.outcome == "unknown"
    assert note(failure)["cleanup"] == "unconfirmed"
    assert probe.calls == 1 and not probe.closed


async def test_projection_failure_cannot_replace_the_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from helpers import uia_probe_diagnostics as diagnostic

    def unavailable(*args: object, **kwargs: object) -> dict[str, Any]:
        raise RuntimeError("private diagnostic fault")

    failure = AXLeaseError("read_timeout", "private")
    probe = _Probe(failure=failure)
    monkeypatch.setattr(diagnostic, "probe_facts", unavailable)
    with pytest.raises(AXLeaseError) as caught:
        await diagnostic.observe_probe(cast(AXHelperLease, probe))
    assert caught.value is failure and not getattr(failure, "__notes__", ())
    assert probe.closed and probe.calls == 1


async def test_cancellation_waits_for_owned_cleanup_and_remains_cancellation() -> None:
    from helpers.uia_probe_diagnostics import observe_probe

    failure = asyncio.CancelledError("private cancellation")
    probe = _Probe(failure=failure)
    probe.cleanup_release.clear()
    operation = asyncio.create_task(observe_probe(cast(AXHelperLease, probe)))
    try:
        async with asyncio.timeout(2):
            await probe.cleanup_entered.wait()
        assert not operation.done() and not getattr(failure, "__notes__", ())
        probe.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await operation
        assert caught.value is failure and note(failure)["cleanup"] == "confirmed"
        assert probe.closed and probe.calls == 1
    finally:
        probe.cleanup_release.set()
        await asyncio.gather(operation, return_exceptions=True)


async def test_success_does_not_swallow_owned_cleanup_failure() -> None:
    from helpers.uia_probe_diagnostics import observe_probe

    failure = RuntimeError("cleanup failed")
    probe = _Probe(cleanup_failure=failure)
    with pytest.raises(RuntimeError) as caught:
        await observe_probe(cast(AXHelperLease, probe))
    assert caught.value is failure and not getattr(failure, "__notes__", ())
    assert probe.calls == 1 and not probe.closed


@pytest.fixture
async def owned_probe(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Harness]:
    with monkeypatch.context() as construction:
        construction.setattr(diagnostic, "PendingHelperLease", diagnostic.ProbeHelperLease)
        case = _Harness()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", case.spawn)
    monkeypatch.setattr(ax_lease, "_settle_process", case.settle)
    try:
        yield case
    finally:
        case.spawn_release.set()
        case.cleanup_release.set()
        case.writer.drain_release.set()
        try:
            try:
                await case.lease.aclose()
            except AXLeaseError:
                if case.cleanup_failure is None:
                    raise
        finally:
            for reader in case.lease._readers:
                reader.cancel()
            await asyncio.gather(*case.lease._readers, return_exceptions=True)


async def _failed_owned_probe_facts(
    case: _Harness, *, stderr: bytes, exit_code: int | None
) -> dict[str, Any]:
    failure = AXLeaseError("read_timeout", "private original timeout")
    case.process.stderr.feed_data(stderr)
    operation = asyncio.create_task(diagnostic.observe_probe(case.lease))
    try:
        async with asyncio.timeout(2):
            await case.writer.drain_entered.wait()
            while bytes(case.lease._stderr) != stderr[:MAX_STDERR_BYTES]:
                await asyncio.sleep(0)
            case.process.returncode = exit_code
            assert case.lease._pending is not None
            case.lease._pending.set_exception(failure)
            with pytest.raises(AXLeaseError) as caught:
                await operation
        assert caught.value is failure and caught.value.outcome == "not_started"
        assert case.spawns == case.cleanups == 1
        assert case.lease._process is cast(object, case.process) and case.lease.closed
        assert len(case.writer.frames) == case.writer.drains == 1
        assert case.writer.closes == case.writer.waits == 1
        assert case.process.returncode == 7
        facts = note(failure)
        assert facts["cleanup"] == "confirmed"
        assert "private" not in json.dumps(facts)
        return facts
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await case.lease.aclose()


async def test_probe_distinguishes_zero_stderr_while_owned_process_was_running(
    owned_probe: _Harness,
) -> None:
    facts = await _failed_owned_probe_facts(owned_probe, stderr=b"", exit_code=None)
    before, after = facts["before_cleanup"], facts["after_cleanup"]
    assert before["exit"] == "running" and before["stderr_bytes"] == 0
    assert before["stderr_kind"] == "empty" and before["phase"] == "response"
    assert before["script_stages"] == [] and before["stage_sequence"] == "unobserved"
    assert after["exit"] == "nonzero" and after["readers_settled"] is True
    assert after["phase"] == "settled" and owned_probe.lease._once is True


async def test_probe_records_first_complete_stage_arrival_on_original_stderr_reader(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = owned_probe
    lease = cast(diagnostic.ProbeHelperLease, case.lease)
    clock = [100.0]
    monkeypatch.setattr(lease, "_observation_time", lambda: clock[0])
    operation = asyncio.create_task(diagnostic.observe_probe(lease))
    received = b""

    async def feed(data: bytes, elapsed: float) -> None:
        nonlocal received
        clock[0] = 100.0 + elapsed
        received += data
        case.process.stderr.feed_data(data)
        async with asyncio.timeout(2):
            while bytes(lease._stderr) != received:
                await asyncio.sleep(0)

    try:
        async with asyncio.timeout(2):
            await case.writer.drain_entered.wait()
        await feed(stage_bytes("script_entered", "uia_initializing"), 0.125)
        assert lease._snapshot()["stage_arrival_ms"] == {
            "script_entered": 125,
            "uia_initializing": 125,
        }
        partial = stage_bytes(UIA_INITIALIZATION_STAGES[0])
        await feed(partial[:-1], 0.125)
        assert UIA_INITIALIZATION_STAGES[0] not in lease._snapshot()["stage_arrival_ms"]
        await feed(partial[-1:], 0.250)
        await feed(
            stage_bytes(*UIA_INITIALIZATION_STAGES) + b"private-host-value=hidden\r\n",
            0.500,
        )
        expected = {
            "script_entered": 125,
            "uia_initializing": 125,
            UIA_INITIALIZATION_STAGES[0]: 250,
            UIA_INITIALIZATION_STAGES[1]: 500,
        }
        snapshot = lease._snapshot()
        assert snapshot["stage_arrival_ms"] == expected
        assert snapshot["stderr_observation"] == "attached"
        assert bytes(lease._observed_stderr) == received
        assert snapshot["stage_sequence"] == "invalid"  # Duplicate kept by original parser.
        assert bytes(lease._stderr) == received  # Original reader retained every byte unchanged.
        failure = AXLeaseError("read_timeout", "private original failure")
        assert lease._pending is not None
        lease._pending.set_exception(failure)
        async with asyncio.timeout(2):
            with pytest.raises(AXLeaseError) as caught:
                await operation
        assert caught.value is failure
        facts = note(failure)
        assert facts["before_cleanup"]["stage_arrival_ms"] == expected
        assert facts["after_cleanup"]["stage_arrival_ms"] == expected
        assert facts["cleanup"] == "confirmed" and "private" not in json.dumps(facts)
        assert case.spawns == case.cleanups == case.writer.drains == 1
        assert len(case.writer.frames) == case.writer.closes == case.writer.waits == 1
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"private-host-value=hidden\r\n", []),
        (PREFIX + b"uia_host_validated\r", []),
        (stage_bytes(*ALL_STAGES), list(ALL_STAGES)),
        (b"x" * MAX_STDERR_BYTES + stage_bytes(*ALL_STAGES), []),
    ],
    ids=["unknown", "incomplete", "all-known", "outside-retained-prefix"],
)
async def test_stage_arrival_records_only_complete_fixed_lines_in_original_prefix(
    owned_probe: _Harness, stderr: bytes, expected: list[str]
) -> None:
    facts = await _failed_owned_probe_facts(owned_probe, stderr=stderr, exit_code=None)
    for phase in ("before_cleanup", "after_cleanup"):
        arrivals = facts[phase]["stage_arrival_ms"]
        assert list(arrivals) == expected
        assert all(type(value) is int and 0 <= value <= 2**31 - 1 for value in arrivals.values())
    assert owned_probe.lease._budget.used == len(stderr)
    assert "private" not in json.dumps(facts)


async def test_stage_arrival_clock_failure_keeps_original_pipe_and_error_instance(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = owned_probe
    lease = cast(diagnostic.ProbeHelperLease, case.lease)
    operation = asyncio.create_task(diagnostic.observe_probe(lease))
    failure = AXLeaseError("read_timeout", "private original failure")
    stderr = stage_bytes("uia_initializing", *UIA_INITIALIZATION_STAGES)

    def unavailable() -> float:
        raise RuntimeError("private observation fault")

    try:
        async with asyncio.timeout(2):
            await case.writer.drain_entered.wait()
        with monkeypatch.context() as observation:
            observation.setattr(lease, "_observation_time", unavailable)
            case.process.stderr.feed_data(stderr)
            async with asyncio.timeout(2):
                while bytes(lease._stderr) != stderr:
                    await asyncio.sleep(0)
        assert lease._snapshot()["stage_arrival_ms"] == {}
        assert lease._snapshot()["script_stages"] == [
            "uia_initializing",
            *UIA_INITIALIZATION_STAGES,
        ]
        assert lease._pending is not None
        lease._pending.set_exception(failure)
        async with asyncio.timeout(2):
            with pytest.raises(AXLeaseError) as caught:
                await operation
        assert caught.value is failure
        assert note(failure)["cleanup"] == "confirmed"
        assert case.spawns == case.cleanups == case.writer.drains == 1
        assert lease._budget.used == len(stderr)
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_probe_distinguishes_unknown_stderr_after_owned_process_had_exited(
    owned_probe: _Harness,
) -> None:
    stderr = b"private unknown host output\r\n"
    facts = await _failed_owned_probe_facts(owned_probe, stderr=stderr, exit_code=7)
    before, after = facts["before_cleanup"], facts["after_cleanup"]
    assert before["exit"] == "nonzero" and before["stderr_bytes"] == len(stderr)
    assert before["stderr_kind"] == "unrecognized" and before["phase"] == "response"
    assert before["script_stages"] == [] and before["stage_sequence"] == "unobserved"
    assert after["exit"] == "nonzero" and after["readers_settled"] is True


@pytest.mark.parametrize(
    ("data", "stages", "kind", "capped"),
    [
        (b"", [], "empty", False),
        (PREFIX + b"script_entered", [], "unrecognized", False),
        (b"private unknown line\r\n", [], "unrecognized", False),
        (stage_bytes(*STAGES), list(STAGES), "known_stage", False),
        (b"x" * MAX_STDERR_BYTES + stage_bytes(*STAGES), [], "unrecognized", True),
    ],
    ids=["empty", "partial", "unknown", "known", "capped"],
)
async def test_probe_snapshot_keeps_only_bounded_counts_and_known_complete_stages(
    owned_probe: _Harness, data: bytes, stages: list[str], kind: str, capped: bool
) -> None:
    lease = cast(diagnostic.ProbeHelperLease, owned_probe.lease)
    lease._stderr.extend(data)
    facts = lease._snapshot()
    assert facts["stderr_bytes"] == min(len(data), MAX_STDERR_BYTES)
    assert facts["stderr_kind"] == kind and facts["stderr_prefix_capped"] is capped
    assert facts["script_stages"] == stages
    assert facts["stage_sequence"] == ("valid" if stages else "unobserved")
    assert facts["phase"] == "not_started" and facts["elapsed_ms"] == 0
    assert set(facts["phase_ms"]) == {"spawn", "send", "response", "cleanup"}
    assert "private" not in json.dumps(facts)


async def test_probe_late_spawn_preserves_original_cancel_and_adopts_same_process(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = owned_probe
    case.spawn_release.clear()
    original: list[asyncio.CancelledError] = []
    request = case.lease.request

    async def capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await request(*args, **kwargs)
        except asyncio.CancelledError as error:
            original.append(error)
            raise

    monkeypatch.setattr(case.lease, "request", capture)
    operation = asyncio.create_task(diagnostic.observe_probe(case.lease))
    try:
        async with asyncio.timeout(2):
            await case.spawn_entered.wait()
            operation.cancel("private cancellation")
            while case.lease.before_cleanup is None:
                await asyncio.sleep(0)
            before = case.lease.before_cleanup
            assert before["spawn"] == "pending" and before["phase"] == "spawn"
            assert before["process_exists"] is False and before["sent"] == 0
            assert not operation.done()
            case.spawn_release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await operation
        facts = note(caught.value)
        assert len(original) == 1 and caught.value is original[0]
        assert facts["before_cleanup"] == before
        assert facts["after_cleanup"]["spawn"] == "returned"
        assert facts["after_cleanup"]["phase"] == "settled"
        for phase in ("before_cleanup", "after_cleanup"):
            assert facts[phase]["stderr_observation"] == "unavailable"
            assert facts[phase]["stderr_eof_observed"] is None
        assert facts["cleanup"] == "confirmed"
        assert case.spawns == case.cleanups == 1 and case.lease._process is cast(
            object, case.process
        )
        assert len(case.writer.frames) == case.writer.drains == 0
        assert case.writer.closes == case.writer.waits == 1
        assert "private" not in json.dumps(facts)
    finally:
        case.spawn_release.set()
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_observed_probe_full_stage_note_stays_within_original_budget(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = cast(diagnostic.ProbeHelperLease, owned_probe.lease)
    monkeypatch.setattr(
        lease,
        "_observation_time",
        lambda: 100.0 if lease.state == "new" else 100.0 + float(2**31),
    )
    facts = await _failed_owned_probe_facts(
        owned_probe, stderr=stage_bytes(*ALL_STAGES), exit_code=None
    )
    for phase in ("before_cleanup", "after_cleanup"):
        assert facts[phase]["script_stages"] == list(ALL_STAGES)
        assert facts[phase]["stderr_kind"] == "known_stage"
        assert facts[phase]["stderr_bytes"] == len(stage_bytes(*ALL_STAGES))
        assert list(facts[phase]["stage_arrival_ms"]) == list(ALL_STAGES)
        assert set(facts[phase]["stage_arrival_ms"].values()) == {2**31 - 1}


async def test_probe_cancelled_readers_do_not_claim_pipe_eof(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = owned_probe

    async def settle(
        process: asyncio.subprocess.Process, readers: list[asyncio.Task[None]], **kwargs: Any
    ) -> None:
        assert process is cast(object, case.process) and process.stdin is not None
        case.cleanups += 1
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        process.stdin.close()
        await process.stdin.wait_closed()
        case.process.returncode = 7

    monkeypatch.setattr(ax_lease, "_settle_process", settle)
    facts = await _failed_owned_probe_facts(case, stderr=b"", exit_code=None)
    assert facts["after_cleanup"]["readers_settled"] is True
    assert all(reader.cancelled() for reader in case.lease._readers)
    assert not case.process.stdout.at_eof() and not case.process.stderr.at_eof()
    assert facts["after_cleanup"]["stderr_eof_observed"] is not True


@pytest.mark.parametrize("split_before_cleanup", [False, True])
async def test_probe_observes_same_stderr_reads_after_original_reader_is_cancelled(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch, split_before_cleanup: bool
) -> None:
    case = owned_probe
    lease = cast(diagnostic.ProbeHelperLease, case.lease)
    stderr = case.process.stderr
    complete = stage_bytes(*STAGES[:2])
    boundary = len(PREFIX) + 5 if split_before_cleanup else 0
    early, late = complete[:boundary], complete[boundary:]
    failure = AXLeaseError("read_timeout", "private original timeout")
    original_transport = getattr(stderr, "_transport", None)

    async def settle(
        process: asyncio.subprocess.Process, readers: list[asyncio.Task[None]], **kwargs: Any
    ) -> None:
        case.cleanups += 1
        case.cleanup_entered.set()
        assert (process is cast(object, case.process)) is True
        assert (process.stderr is stderr) is True
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        assert process.stdin is not None
        process.stdin.close()
        await process.stdin.wait_closed()
        stderr.feed_data(late)
        assert await stderr.read(4096) == late
        stderr.feed_eof()
        assert await stderr.read(4096) == b""
        case.process.stdout.feed_eof()
        case.process.returncode = 7

    monkeypatch.setattr(ax_lease, "_settle_process", settle)
    stderr.feed_data(early)
    operation = asyncio.create_task(diagnostic.observe_probe(lease))
    try:
        async with asyncio.timeout(2):
            await case.writer.drain_entered.wait()
            while not lease._observed_stderr_attached or bytes(lease._stderr) != early:
                await asyncio.sleep(0)
            assert lease._pending is not None
            lease._pending.set_exception(failure)
            with pytest.raises(AXLeaseError) as caught:
                await operation
        assert caught.value is failure
        facts = note(failure)
        before, after = facts["before_cleanup"], facts["after_cleanup"]
        assert before["stderr_bytes"] == len(early)
        assert before["script_stages"] == [] and before["stderr_eof_observed"] is False
        assert after["stderr_bytes"] == len(complete)
        assert after["script_stages"] == list(STAGES[:2])
        assert after["stage_sequence"] == "valid" and after["stderr_eof_observed"] is True
        assert after["stderr_observation"] == "attached"
        assert facts["stages"] == list(STAGES[:2]) and facts["cleanup"] == "confirmed"
        assert lease.before_cleanup == before and bytes(lease._stderr) == early
        assert bytes(lease._observed_stderr) == complete
        assert lease._budget.used == len(early)
        assert (case.process.stderr is stderr) is True
        assert (getattr(stderr, "_transport", None) is original_transport) is True
        assert case.spawns == case.cleanups == 1
        assert len(case.writer.frames) == case.writer.drains == 1
        assert case.writer.closes == case.writer.waits == 1
        assert "private" not in json.dumps(facts)
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("kind", ["cancel", "error"])
async def test_probe_stderr_observer_preserves_original_read_failure(
    owned_probe: _Harness, kind: str
) -> None:
    lease = cast(diagnostic.ProbeHelperLease, owned_probe.lease)
    failure: BaseException = (
        asyncio.CancelledError("private cancellation")
        if kind == "cancel"
        else OSError("private read failure")
    )
    calls: list[int] = []

    async def read(n: int = -1) -> bytes:
        calls.append(n)
        raise failure

    stream = cast(asyncio.StreamReader, SimpleNamespace(read=read))
    lease._observe_stderr_reads(stream)
    with pytest.raises(type(failure)) as caught:
        await stream.read(n=4096)
    assert caught.value is failure and calls == [4096]
    assert lease._observed_stderr_attached and not lease._observed_stderr_failed
    assert not lease._observed_stderr_eof and not lease._observed_stderr


async def test_probe_stderr_observer_keeps_one_bounded_prefix_and_nonzero_read_eof(
    owned_probe: _Harness,
) -> None:
    lease = cast(diagnostic.ProbeHelperLease, owned_probe.lease)
    chunks = [
        stage_bytes(STAGES[0]),
        b"private" * MAX_STDERR_BYTES,
        stage_bytes(STAGES[1]),
        b"",
        b"",
    ]
    calls: list[int] = []

    async def read(n: int = -1) -> bytes:
        chunk = chunks[len(calls)]
        calls.append(n)
        return chunk

    stream = cast(asyncio.StreamReader, SimpleNamespace(read=read))
    lease._observe_stderr_reads(stream)
    wrapped_read = stream.read
    lease._observe_stderr_reads(stream)
    assert (stream.read is wrapped_read) is True
    for size, chunk in zip((17, 4096, 4096, 0), chunks[:4], strict=True):
        assert (await stream.read(size) is chunk) is True
    assert not lease._observed_stderr_eof
    assert await stream.read(4096) == b"" and lease._observed_stderr_eof
    assert calls == [17, 4096, 4096, 0, 4096]
    assert bytes(lease._observed_stderr) == b"".join(chunks[:3])[:MAX_STDERR_BYTES]
    facts = lease._snapshot()
    assert facts["stderr_bytes"] == MAX_STDERR_BYTES and facts["stderr_prefix_capped"] is True
    assert facts["script_stages"] == [STAGES[0]] and facts["stage_sequence"] == "valid"
    assert facts["stderr_eof_observed"] is True and "private" not in json.dumps(facts)
    assert not lease._stderr and lease._budget.used == 0


async def test_probe_relative_phase_times_are_floored_bounded_and_frozen_after_cleanup(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = cast(diagnostic.ProbeHelperLease, owned_probe.lease)
    clock = [100.0]
    monkeypatch.setattr(lease, "_observation_time", lambda: clock[0])
    lease._observe("spawn")
    clock[0] += 0.00175
    lease._observe("send")
    clock[0] += 0.00225
    lease._observe("response")
    clock[0] += 0.00350
    before = lease._snapshot()
    assert before["elapsed_ms"] == 7
    assert before["phase_ms"] == {"spawn": 1, "send": 2, "response": 3, "cleanup": 0}
    lease._observe("cleanup")
    clock[0] += 0.00425
    lease._observe("settled")
    after = lease._snapshot()
    assert after["elapsed_ms"] == 11
    assert after["phase_ms"] == {"spawn": 1, "send": 2, "response": 3, "cleanup": 4}
    clock[0] += 10_000
    assert lease._snapshot() == after
    assert diagnostic._milliseconds(-1.0) == diagnostic._milliseconds(0.0009) == 0
    assert diagnostic._milliseconds(0.0019) == 1
    assert diagnostic._milliseconds(float(2**31)) == 2**31 - 1


@pytest.mark.parametrize("component", ["snapshot", "projection", "clock", "cleanup"])
async def test_probe_observation_faults_preserve_original_error_and_cleanup_owner(
    owned_probe: _Harness, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    case = owned_probe
    failure = AXLeaseError("read_timeout", "private original timeout")

    def unavailable(*args: object, **kwargs: object) -> Any:
        raise RuntimeError("private observation error")

    if component == "snapshot":
        monkeypatch.setattr(case.lease, "_snapshot", unavailable)
    elif component == "projection":
        monkeypatch.setattr(diagnostic, "probe_facts", unavailable)
    elif component == "clock":
        monkeypatch.setattr(case.lease, "_observation_time", unavailable)
    else:
        case.cleanup_failure = RuntimeError("private cleanup error")
    operation = asyncio.create_task(diagnostic.observe_probe(case.lease))
    try:
        async with asyncio.timeout(2):
            await case.writer.drain_entered.wait()
            assert case.lease._pending is not None
            case.lease._pending.set_exception(failure)
            with pytest.raises(AXLeaseError) as caught:
                await operation
        assert caught.value is failure and caught.value.outcome == "not_started"
        assert case.spawns == case.cleanups == 1 and case.lease._process is cast(
            object, case.process
        )
        if component == "cleanup":
            assert note(failure)["cleanup"] == "unconfirmed" and not case.lease.closed
        else:
            assert case.lease.closed and not getattr(failure, "__notes__", ())
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_native_first_probe_keeps_original_factory_argv_and_one_owned_lifecycle(
    owned_probe: _Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_uia_native as native

    case = owned_probe
    # This checks the private factory binding restored by monkeypatch.context.
    factories = vars(uia_computer)
    original = factories["AXHelperLease"]
    constructed: list[tuple[list[str], dict[str, Any]]] = []
    executable = tmp_path / "private-owned-host.exe"
    monkeypatch.setattr(uia_computer, "_powershell", lambda: executable)

    def construct(argv: list[str], **kwargs: Any) -> AXHelperLease:
        constructed.append((argv, kwargs))
        return case.lease

    monkeypatch.setattr(native, "ProbeHelperLease", construct)
    form = native.NativeForm(cast(asyncio.subprocess.Process, object()), "owned", asyncio.Queue())
    failure = AXLeaseError("read_timeout", "private original timeout")
    operation = asyncio.create_task(
        native.test_real_windows_system_module_probe_mta_and_owned_unicode_tree(
            form, tmp_path, monkeypatch
        )
    )
    try:
        async with asyncio.timeout(2):
            await case.writer.drain_entered.wait()
            assert case.lease._pending is not None
            case.lease._pending.set_exception(failure)
            with pytest.raises(AXLeaseError) as caught:
                await operation
        assert caught.value is failure and factories["AXHelperLease"] is original
        assert len(constructed) == 1
        argv, kwargs = constructed[0]
        module_file = uia_computer.__file__
        assert module_file is not None
        expected = [
            str(executable),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Mta",
            "-File",
            str(Path(module_file).with_name("uia_helper.ps1")),
        ]
        assert (argv == expected) is True
        configuration = native.settings().computer
        assert kwargs == {
            "timeout": configuration.operation_timeout_seconds,
            "ttl": configuration.snapshot_ttl_seconds,
            "max_nodes": configuration.max_nodes,
            "once": True,
        }
        assert case.spawns == case.cleanups == 1
        assert len(case.writer.frames) == case.writer.drains == 1
        assert case.lease._once is True and note(failure)["cleanup"] == "confirmed"
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
