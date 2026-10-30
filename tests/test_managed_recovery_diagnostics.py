"""Pure observations of the process fixture; no CLI, sockets or child processes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast, get_args

import pytest
from helpers import managed_recovery_worker as worker

from muli.managed_controller import ManagedTransactionController
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Activation, Code, Role
from muli.platforms.installation import FileFingerprint
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import SlotIdentity
from muli.platforms.managed_launcher import PackageProofs

linux_pipe = pytest.mark.skipif(sys.platform != "linux", reason="Linux owned pipe/select fixture")


@pytest.mark.parametrize("cancel", [False, True])
def test_package_observation_passes_exact_proofs_through_and_keeps_selected_failure(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    from muli.platforms import managed_backend, managed_deployment

    record = cast(TransactionRecordV2, object())
    slot = cast(SlotIdentity, object())
    verified: PackageProofs = {}
    observed: PackageProofs = {}
    calls: list[str] = []
    failure = asyncio.CancelledError("private") if cancel else ValueError("private")

    def controller(
        value: SlotIdentity,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        assert value is slot and verified_python is None
        assert verified_packages is verified and observed_packages is observed
        calls.append("controller")

    def selected(
        value: SlotIdentity,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        assert value is slot and verified_python is None
        assert verified_packages is verified and observed_packages is observed
        calls.append("selected")
        raise failure

    def check(
        self: object,
        value: TransactionRecordV2,
        role: Role | None = None,
        *,
        verified_python: FileFingerprint | None = None,
        verified_packages: PackageProofs | None = None,
        observed_packages: PackageProofs | None = None,
    ) -> None:
        assert value is record and role == "target" and verified_python is None
        assert verified_packages is verified and observed_packages is observed
        calls.append("check")
        managed_deployment.verify_slot_files(
            slot, verified_packages=verified_packages, observed_packages=observed_packages
        )
        vars(managed_backend)["verify_slot_files"](
            slot, verified_packages=verified_packages, observed_packages=observed_packages
        )

    monkeypatch.setattr(managed_deployment, "verify_slot_files", controller)
    monkeypatch.setattr(managed_backend, "verify_slot_files", selected)
    monkeypatch.setattr(ManagedLinuxBackend, "_check", check)
    backend = object.__new__(worker.ObservedBackend)
    with pytest.raises(type(failure)) as caught:
        backend._check(record, "target", verified_packages=verified, observed_packages=observed)
    assert caught.value is failure
    assert calls == ["check", "controller", "selected"]
    assert backend.check_calls == backend.controller_calls == backend.selected_calls == 1
    assert managed_deployment.verify_slot_files is controller
    assert vars(managed_backend)["verify_slot_files"] is selected


@pytest.mark.parametrize("failure", [None, ValueError("private failure")])
def test_observed_guard_calls_original_once_and_keeps_its_failure(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    clock = iter([10.0, 10.25])
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    record = cast(TransactionRecordV2, object())
    calls: list[tuple[object, object, Role | None]] = []

    def check(self: object, value: TransactionRecordV2, role: Role | None = None) -> None:
        calls.append((self, value, role))
        if failure is not None:
            raise failure

    monkeypatch.setattr(ManagedLinuxBackend, "_check", check)
    backend = object.__new__(worker.ObservedBackend)
    if failure is None:
        backend._check(record, "base")
    else:
        with pytest.raises(ValueError) as caught:
            backend._check(record, "base")
        assert caught.value is failure
    assert calls == [(backend, record, "base")]
    assert backend.check_calls == 1 and backend.check_seconds == 0.25
    untouched = object.__new__(worker.ObservedBackend)
    assert untouched.check_calls == 0 and untouched.check_seconds == 0


@pytest.mark.parametrize("stage", ["none", "before", "controller", "selected"])
@pytest.mark.parametrize("cancel", [False, True])
def test_file_observation_preserves_original_calls_failures_and_lookup_restoration(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    cancel: bool,
) -> None:
    from muli.platforms import managed_backend, managed_deployment

    clock = 10.0
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock))
    record = cast(TransactionRecordV2, object())
    slot = cast(SlotIdentity, object())
    fingerprint = cast(FileFingerprint, object())
    failure = asyncio.CancelledError("private") if cancel else ValueError("private")
    calls: list[str] = []

    def controller(value: SlotIdentity) -> None:
        nonlocal clock
        assert value is slot
        calls.append("controller")
        clock += 0.25
        if stage == "controller":
            raise failure

    def selected(value: SlotIdentity, *, verified_python: FileFingerprint | None = None) -> None:
        nonlocal clock
        assert value is slot
        assert verified_python is fingerprint
        calls.append("selected")
        clock += 0.5
        if stage == "selected":
            raise failure

    def check(self: object, value: TransactionRecordV2, role: Role | None = None) -> None:
        nonlocal clock
        assert value is record and role == "target"
        calls.append("check")
        clock += 0.125
        if stage == "before":
            raise failure
        managed_deployment.verify_slot_files(slot)
        vars(managed_backend)["verify_slot_files"](slot, verified_python=fingerprint)
        clock += 0.125

    monkeypatch.setattr(managed_deployment, "verify_slot_files", controller)
    monkeypatch.setattr(managed_backend, "verify_slot_files", selected)
    monkeypatch.setattr(ManagedLinuxBackend, "_check", check)
    backend = object.__new__(worker.ObservedBackend)
    if stage == "none":
        backend._check(record, "target")
    else:
        with pytest.raises(type(failure)) as caught:
            backend._check(record, "target")
        assert caught.value is failure
    expected = {
        "none": ["check", "controller", "selected"],
        "before": ["check"],
        "controller": ["check", "controller"],
        "selected": ["check", "controller", "selected"],
    }[stage]
    assert calls == expected
    assert managed_deployment.verify_slot_files is controller
    assert vars(managed_backend)["verify_slot_files"] is selected
    assert backend.check_calls == 1
    assert backend.controller_calls == ("controller" in calls)
    assert backend.selected_calls == ("selected" in calls)
    assert backend.controller_seconds == (0.25 if "controller" in calls else 0)
    assert backend.selected_seconds == (0.5 if "selected" in calls else 0)
    assert backend.check_seconds == clock - 10.0
    untouched = object.__new__(worker.ObservedBackend)
    assert untouched.controller_calls == untouched.selected_calls == 0
    if stage == "none":
        backend._check(record, "target")
        assert calls == expected * 2
        assert backend.check_calls == backend.controller_calls == backend.selected_calls == 2


@pytest.mark.parametrize("failure", [None, ValueError("private body"), asyncio.CancelledError()])
async def test_step_observation_preserves_result_exception_and_single_execution(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException | None
) -> None:
    backend = object.__new__(worker.ObservedBackend)
    record = cast(TransactionRecordV2, SimpleNamespace(phase="rolling_back", intent="restore_base"))
    clock = iter([10.0, 10.5])
    # Patch only the helper's module reference, not asyncio's global time module.
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    lines: list[str] = []
    monkeypatch.setattr(worker, "checkpoint", lines.append)
    calls = 0

    async def step() -> TransactionRecordV2:
        nonlocal calls
        calls += 1
        backend.check_calls, backend.check_seconds = 3, 0.25
        backend.controller_calls, backend.controller_seconds = 3, 0.125
        backend.selected_calls, backend.selected_seconds = 2, 0.0625
        if failure is not None:
            raise failure
        return record

    driver = cast(ManagedTransactionController, SimpleNamespace(step=step))
    if failure is None:
        assert await worker.observed_step(driver, backend, 4, record) is record
    else:
        with pytest.raises(type(failure)) as caught:
            await worker.observed_step(driver, backend, 4, record)
        assert caught.value is failure
    assert calls == 1
    assert lines == ["controller-step=[4,500,3,250,3,125,2,62,4,5]"]


async def test_step_evidence_failure_cannot_mask_original_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = cast(TransactionRecordV2, SimpleNamespace(phase="rolled_back", intent="none"))

    async def step() -> TransactionRecordV2:
        return record

    def broken(value: str) -> None:
        raise OSError("broken fixture pipe")

    monkeypatch.setattr(worker, "checkpoint", broken)
    driver = cast(ManagedTransactionController, SimpleNamespace(step=step))
    assert (
        await worker.observed_step(driver, object.__new__(worker.ObservedBackend), 0, record)
        is record
    )


async def test_all_step_records_fit_original_pipe_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import get_args

    from muli.management_transaction import Intent, Phase

    record = cast(
        TransactionRecordV2,
        SimpleNamespace(phase=get_args(Phase)[-1], intent=get_args(Intent)[-1]),
    )
    lines: list[str] = []
    monkeypatch.setattr(worker, "checkpoint", lines.append)
    backend = object.__new__(worker.ObservedBackend)
    clock = iter(value for _ in range(32) for value in (0, 1_000_000))
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def step() -> TransactionRecordV2:
        backend.check_calls += 10_000_000
        backend.check_seconds += 10_000_000
        backend.controller_calls += 10_000_000
        backend.controller_seconds += 10_000_000
        backend.selected_calls += 10_000_000
        backend.selected_seconds += 10_000_000
        return record

    driver = cast(ManagedTransactionController, SimpleNamespace(step=step))
    for index in range(32):
        await worker.observed_step(driver, backend, index, record)
    assert len(lines) == 32
    output = "\n".join(lines) + "\nstart-target\nstop-target\nstart-base\ncontroller-resolved\n"
    assert len(output.encode()) < 4096
    assert all(json.loads(line.split("=", 1)[1])[1:8] == [1_000_000] * 7 for line in lines)


@contextmanager
def checkpoint_pipe(data: bytes) -> Iterator[subprocess.Popen[bytes]]:
    """An owned small pipe exercises reads without starting a child process."""
    reading, writing = os.pipe()
    try:
        assert os.write(writing, data) == len(data)
    finally:
        os.close(writing)
    with os.fdopen(reading, "rb", buffering=0) as stream:
        yield cast(subprocess.Popen[bytes], SimpleNamespace(stdout=stream))


@linux_pipe
@pytest.mark.parametrize("chunk_bytes", [1024, 1, 7])
def test_diagnostic_lines_do_not_replace_controller_checkpoint(
    monkeypatch: pytest.MonkeyPatch, chunk_bytes: int
) -> None:
    import test_managed_process_recovery as recovery

    frame = b"controller-step=[0,1985,47,1932,47,1500,20,400,2,2]\n"
    data = frame + b"start-target\n"
    monkeypatch.setattr(
        recovery, "os", SimpleNamespace(read=lambda fd, size: os.read(fd, min(size, chunk_bytes)))
    )
    with checkpoint_pipe(data) as process:
        output = bytearray()
        recovery.expect(process, b"start-target", output, time.monotonic() + 1)
        assert bytes(output) == data


@linux_pipe
@pytest.mark.parametrize(
    "prefix",
    [
        b"unexpected diagnostic\n",
        b"controller-step=broken\n",
        b"controller-step=[true,1,1,1,0,0,0,0,2,2]\n",
        b"controller-step=[0,1,1,1,0,0,0,0,999,2]\n",
        b"controller-step=[1,1,1,1,0,0,0,0,2,2]\n",
        b"controller-step=[0,-1,1,1,0,0,0,0,2,2]\n",
        b"controller-step=[0,1,1,1,0,0,0,0,2,2]\n" * 2,
        b"controller-step=[0,1,1,1,0,0,0,0,2,999]\n",
        b"controller-step=[0,1,1,1,0,0,0,0,2,2,0]\n",
        b"controller-step=[0,1,true,1,0,0,0,0,2,2]\n",
        b"controller-step=[0,1,1,1000001,0,0,0,0,2,2]\n",
    ],
    ids=[
        "unknown",
        "json",
        "bool",
        "phase",
        "sequence",
        "negative",
        "duplicate",
        "intent",
        "shape",
        "bool-count",
        "large-duration",
    ],
)
def test_checkpoint_rejects_unknown_or_invalid_frames(prefix: bytes) -> None:
    import test_managed_process_recovery as recovery

    with checkpoint_pipe(prefix + b"start-target\n") as process:
        with pytest.raises(AssertionError):
            recovery.expect(process, b"start-target", bytearray(), time.monotonic() + 1)


@linux_pipe
def test_diagnostic_without_actual_checkpoint_is_not_success() -> None:
    import test_managed_process_recovery as recovery

    with checkpoint_pipe(b"controller-step=[0,1,1,1,0,0,0,0,2,2]\n") as process:
        with pytest.raises(AssertionError):
            recovery.expect(process, b"start-target", bytearray(), time.monotonic() + 1)


@linux_pipe
def test_other_recovery_reader_stays_strict() -> None:
    from test_management_process_recovery import checkpoint

    data = b"controller-step=[0,1,1,1,0,0,0,0,2,2]\nstart-target\n"
    with checkpoint_pipe(data) as process:
        with pytest.raises(AssertionError):
            checkpoint(process, b"start-target", bytearray(), time.monotonic() + 1)


@linux_pipe
def test_checkpoint_keeps_existing_cumulative_byte_limit() -> None:
    import test_managed_process_recovery as recovery

    with checkpoint_pipe(b"start-target\n") as process:
        output = bytearray(b"x" * recovery.HTTP_BYTES)
        with pytest.raises(AssertionError):
            recovery.expect(process, b"start-target", output, time.monotonic() + 1)


@linux_pipe
@pytest.mark.parametrize("tail", [b"extra\n", b"unfinished"])
def test_expected_checkpoint_rejects_already_read_trailing_bytes(tail: bytes) -> None:
    import test_managed_process_recovery as recovery

    with checkpoint_pipe(b"start-target\n" + tail) as process:
        with pytest.raises(AssertionError):
            recovery.expect(process, b"start-target", bytearray(), time.monotonic() + 1)


@linux_pipe
def test_step_sequence_survives_multiple_parent_handshakes() -> None:
    import test_managed_process_recovery as recovery

    reading, writing = os.pipe()
    with (
        os.fdopen(reading, "rb", buffering=0) as stream,
        os.fdopen(writing, "wb", buffering=0) as sender,
    ):
        process = cast(subprocess.Popen[bytes], SimpleNamespace(stdout=stream))
        output = bytearray()
        for index, expected in enumerate((b"start-target", b"stop-target")):
            frame = f"controller-step=[{index},1,1,1,0,0,0,0,4,4]\n".encode()
            sender.write(frame + expected + b"\n")
            recovery.expect(process, expected, output, time.monotonic() + 1)
        assert output.count(b"controller-step=") == 2


@linux_pipe
def test_all_32_diagnostics_are_admitted_but_a_33rd_is_rejected() -> None:
    import test_managed_process_recovery as recovery

    frames = b"".join(
        f"controller-step=[{index},1,1,1,0,0,0,0,2,2]\n".encode() for index in range(32)
    )
    with checkpoint_pipe(frames + b"start-target\n") as process:
        output = bytearray()
        recovery.expect(process, b"start-target", output, time.monotonic() + 1)
    with checkpoint_pipe(b"controller-step=[32,1,1,1,0,0,0,0,2,3]\nstop-target\n") as process:
        with pytest.raises(AssertionError):
            recovery.expect(process, b"stop-target", output, time.monotonic() + 1)


@linux_pipe
@pytest.mark.parametrize("field", [4, 5, 6, 7])
@pytest.mark.parametrize("invalid", [True, -1, 1_000_001])
def test_new_timing_fields_reject_noncanonical_scalars(field: int, invalid: int) -> None:
    import test_managed_process_recovery as recovery

    values = [0, 10, 2, 8, 2, 5, 1, 2, 2, 2]
    values[field] = invalid
    frame = b"controller-step=" + json.dumps(values).encode() + b"\nstart-target\n"
    steps: list[list[int]] = []
    with checkpoint_pipe(frame) as process:
        with pytest.raises(AssertionError):
            recovery.expect(process, b"start-target", bytearray(), time.monotonic() + 1, steps)
    assert steps == []


@linux_pipe
def test_failure_note_contains_only_consumed_validated_steps(
    tmp_path: Path,
) -> None:
    import test_managed_process_recovery as recovery

    values = [0, 10, 2, 8, 2, 5, 1, 2, 2, 2]
    frame = b"controller-step=" + json.dumps(values).encode() + b"\nprivate raw output\n"
    steps: list[list[int]] = []
    with checkpoint_pipe(frame) as process:
        with pytest.raises(AssertionError) as caught:
            recovery.expect(process, b"start-target", bytearray(), time.monotonic() + 1, steps)
    note = recovery.failure_diagnostic(tmp_path, [], time.monotonic(), steps)
    caught.value.add_note(note)
    details = json.loads(note.split("=", 1)[1])
    assert details["steps"] == [values]
    assert "private" not in str(caught.value) and "private" not in note
    assert details["steps"][0][3] - details["steps"][0][5] - details["steps"][0][7] == 1


@linux_pipe
def test_maximum_valid_summary_survives_all_handshakes_and_failure_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_managed_process_recovery as recovery

    values = [
        [index, *([1_000_000] * 7), len(worker.STEP_PHASES) - 1, len(worker.STEP_INTENTS) - 1]
        for index in range(32)
    ]
    frames = b"".join(
        b"controller-step=" + json.dumps(row, separators=(",", ":")).encode() + b"\n"
        for row in values
    )
    handshakes = b"start-target\nstop-target\nstart-base\ncontroller-resolved\n"
    assert len(frames + handshakes) <= recovery.HTTP_BYTES
    steps: list[list[int]] = []
    with checkpoint_pipe(frames + b"controller-resolved\n") as process:
        recovery.expect(process, b"controller-resolved", bytearray(), time.monotonic() + 1, steps)
    assert steps == values
    record = SimpleNamespace(
        revision=1_000_000,
        phase="recovery_required",
        intent="install_target",
        activation=max(get_args(Activation), key=len),
        code=max(get_args(Code), key=len),
    )
    monkeypatch.setattr(
        recovery, "TransactionJournal", lambda *args, **kwargs: nullcontext(object())
    )
    monkeypatch.setattr(recovery, "read_record", lambda journal: record)
    database = SimpleNamespace(
        close=lambda: None,
        execute=lambda *args: SimpleNamespace(
            fetchone=lambda: (b'{"posts":4096,"started":true,"allowed":true}',)
        ),
    )
    monkeypatch.setattr(
        recovery, "sqlite3", SimpleNamespace(connect=lambda *args, **kwargs: database)
    )
    timeline: list[dict[str, str | int]] = [
        {"stage": "wait:controller-resolved", "elapsed_ms": 1_000_000}
    ] * 16
    note = recovery.failure_diagnostic(tmp_path, timeline, time.monotonic(), steps)
    assert len(note.encode()) <= recovery.HTTP_BYTES
    details = json.loads(note.split("=", 1)[1])
    assert details["steps"] == values and details["timeline"] == timeline
    assert details["state"]["phase"] == record.phase
    assert details["http"]["base"]["posts"] == recovery.HTTP_BYTES
