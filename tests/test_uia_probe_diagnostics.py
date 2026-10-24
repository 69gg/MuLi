"""Finite probe evidence uses fake leases and never starts PowerShell."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

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


def test_packaged_script_has_fourteen_once_only_fixed_stage_points() -> None:
    from muli.tools import uia_computer

    source = Path(uia_computer.__file__).with_name("uia_helper.ps1").read_text(encoding="utf-8")
    lines = [PREFIX.decode() + stage for stage in STAGES]
    assert all(source.count("'" + line + "'") == 1 for line in lines)
    assert len(stage_bytes(*STAGES)) <= 512
    function = source.split("function Write-Stage", 1)[1].split("\nWrite-Stage 0", 1)[0]
    assert function.index("$script:traceMask = $script:traceMask -bor $bit") < function.index(
        "[Console]::Error.WriteLine"
    )
    assert "$script:traceMask -band $bit" in function and "catch { }" in function
    assert "$index -ge $script:traceStages.Length" in function
    assert source.count("Write-Stage ") == len(STAGES)
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


def test_probe_native_getters_keep_their_original_single_calls_between_fixed_stages() -> None:
    from muli.tools import uia_computer

    assert uia_computer.__file__ is not None
    source = Path(uia_computer.__file__).with_name("uia_helper.ps1").read_text(encoding="utf-8")
    points = (
        "Initialize-Uia\n            Check-Deadline",
        "if ($traceProbe) { Write-Stage 11 }",
        "$script:walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker",
        "if ($traceProbe) { Write-Stage 12 }",
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
