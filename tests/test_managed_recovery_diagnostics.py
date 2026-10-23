"""Pure observations of the process fixture; no CLI, sockets or child processes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from helpers import managed_recovery_worker as worker

from muli.managed_controller import ManagedTransactionController
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import Role
from muli.platforms.managed_backend import ManagedLinuxBackend

linux_pipe = pytest.mark.skipif(sys.platform != "linux", reason="Linux owned pipe/select fixture")


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
    assert lines == ['controller-step=[4,500,3,250,"rolling_back","restore_base"]']


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
        SimpleNamespace(phase=max(get_args(Phase), key=len), intent=max(get_args(Intent), key=len)),
    )
    lines: list[str] = []
    monkeypatch.setattr(worker, "checkpoint", lines.append)
    backend = object.__new__(worker.ObservedBackend)
    clock = iter(value for _ in range(32) for value in (0, 1_000_000))
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def step() -> TransactionRecordV2:
        backend.check_calls += 10_000_000
        backend.check_seconds += 10_000_000
        return record

    driver = cast(ManagedTransactionController, SimpleNamespace(step=step))
    for index in range(32):
        await worker.observed_step(driver, backend, index, record)
    assert len(lines) == 32
    output = "\n".join(lines) + "\nstart-target\nstop-target\nstart-base\ncontroller-resolved\n"
    assert len(output.encode()) < 4096
    assert all(json.loads(line.split("=", 1)[1])[1:4] == [1_000_000] * 3 for line in lines)


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

    frame = b'controller-step=[0,1985,47,1932,"activating","install_target"]\n'
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
        b'controller-step=[true,1,1,1,"activating","install_target"]\n',
        b'controller-step=[0,1,1,1,"private","install_target"]\n',
        b'controller-step=[1,1,1,1,"activating","install_target"]\n',
        b'controller-step=[0,-1,1,1,"activating","install_target"]\n',
        b'controller-step=[0,1,1,1,"activating","install_target"]\n' * 2,
        b'controller-step=[0,1,1,1,"activating","unknown"]\n',
        b'controller-step=[0,1,1,1,"activating","install_target",0]\n',
        b'controller-step=[0,1,true,1,"activating","install_target"]\n',
        b'controller-step=[0,1,1,1000001,"activating","install_target"]\n',
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

    with checkpoint_pipe(b'controller-step=[0,1,1,1,"activating","install_target"]\n') as process:
        with pytest.raises(AssertionError):
            recovery.expect(process, b"start-target", bytearray(), time.monotonic() + 1)


@linux_pipe
def test_other_recovery_reader_stays_strict() -> None:
    from test_management_process_recovery import checkpoint

    data = b'controller-step=[0,1,1,1,"activating","install_target"]\nstart-target\n'
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
            frame = f'controller-step=[{index},1,1,1,"rolling_back","stop_target"]\n'.encode()
            sender.write(frame + expected + b"\n")
            recovery.expect(process, expected, output, time.monotonic() + 1)
        assert output.count(b"controller-step=") == 2


@linux_pipe
def test_all_32_diagnostics_are_admitted_but_a_33rd_is_rejected() -> None:
    import test_managed_process_recovery as recovery

    frames = b"".join(
        f'controller-step=[{index},1,1,1,"activating","install_target"]\n'.encode()
        for index in range(32)
    )
    with checkpoint_pipe(frames + b"start-target\n") as process:
        output = bytearray()
        recovery.expect(process, b"start-target", output, time.monotonic() + 1)
    with checkpoint_pipe(
        b'controller-step=[32,1,1,1,"activating","start_target"]\nstop-target\n'
    ) as process:
        with pytest.raises(AssertionError):
            recovery.expect(process, b"stop-target", output, time.monotonic() + 1)
