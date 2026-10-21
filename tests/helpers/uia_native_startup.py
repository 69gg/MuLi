"""Bounded startup observations for the hosted CI's own native-window fixture."""

from __future__ import annotations

import asyncio
import json
from asyncio import timeout_at
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from muli.process import _OutputBudget, _settle_process, _wait_cleanup

OUTPUT_BYTES = 4096
STAGE_PREFIX = b"muli-native-stage="
SCRIPT_STAGES = (
    "script_entered",
    "system_module_loaded",
    "assemblies_loaded",
    "form_constructed",
    "controls_ready",
    "before_message_loop",
    "handle_created",
    "loaded",
    "activated",
    "shown",
    "python_entered",
    "class_registered",
    "window_created",
    "window_shown",
    "message_loop_ready",
)


@dataclass
class _Trace:
    clock: Callable[[], float]
    started: float
    stages: list[dict[str, str | int]] = field(default_factory=list)
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_eof: bool = False
    stderr_eof: bool = False
    spawn: str = "pending"
    exit_before_cleanup: int | None = None
    exit_after_cleanup: int | None = None
    cleanup: str = "pending"
    readers_settled: bool = False
    reader_failed: bool = False
    output_budget: bool = False
    budget: _OutputBudget = field(default_factory=lambda: _OutputBudget(OUTPUT_BYTES))

    def mark(self, stage: str) -> None:
        if stage not in (*SCRIPT_STAGES, "spawn_returned", "readiness_received"):
            return
        if any(item["stage"] == stage for item in self.stages):
            return
        self.stages.append(
            {"stage": stage, "elapsed_ms": round((self.clock() - self.started) * 1000)}
        )

    def charge(self, data: bytes, *, stderr: bool) -> None:
        if stderr:
            self.stderr_bytes += len(data)
        else:
            self.stdout_bytes += len(data)
        try:
            self.budget.charge(data)
        except ValueError:
            self.output_budget = True
            raise AssertionError("Owned native-window output budget exceeded") from None

    def note(self) -> str:
        result = {
            "stages": self.stages,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_eof": self.stdout_eof,
            "stderr_eof": self.stderr_eof,
            "spawn": self.spawn,
            "exit_before_cleanup": self.exit_before_cleanup,
            "exit_after_cleanup": self.exit_after_cleanup,
            "cleanup": self.cleanup,
            "readers_settled": self.readers_settled,
            "reader_failed": self.reader_failed,
            "output_budget": self.output_budget,
        }
        prefix = "uia_native_startup_diagnostic="
        note = prefix + json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        if len(note.encode("utf-8")) > OUTPUT_BYTES:
            return prefix + '{"status":"output_budget"}'
        return note


async def _read_stderr(stream: asyncio.StreamReader, trace: _Trace) -> None:
    pending = bytearray()
    while chunk := await stream.read(OUTPUT_BYTES):
        trace.charge(chunk, stderr=True)
        pending.extend(chunk)
        while b"\n" in pending:
            frame, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            if frame.startswith(STAGE_PREFIX):
                try:
                    stage = frame[len(STAGE_PREFIX) :].removesuffix(b"\r").decode("ascii")
                except UnicodeError:
                    continue
                trace.mark(stage)
    trace.stderr_eof = True


async def _read_ready(process: asyncio.subprocess.Process, title: str, trace: _Trace) -> None:
    assert process.stdout is not None
    try:
        line = await process.stdout.readline()
    except ValueError:
        trace.output_budget = True
        raise AssertionError("Owned native-window readiness exceeded its output budget") from None
    trace.charge(line, stderr=False)
    trace.stdout_eof = not line
    assert line and len(line) <= OUTPUT_BYTES, (
        "Owned native-window fixture did not publish readiness"
    )
    try:
        ready = json.loads(line.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError):
        raise AssertionError("Owned native-window readiness was invalid") from None
    assert ready == {"pid": process.pid, "title": title}, (
        "Owned native-window readiness did not match"
    )
    trace.mark("readiness_received")


async def _read_buttons(
    process: asyncio.subprocess.Process, trace: _Trace, events: asyncio.Queue[int]
) -> None:
    """Continue the same owned stdout after readiness, under its original budget."""
    assert process.stdout is not None
    received = False
    while True:
        try:
            line = await process.stdout.readline()
        except ValueError:
            trace.output_budget = True
            raise AssertionError("Owned button event exceeded its output budget") from None
        trace.charge(line, stderr=False)
        if not line:
            trace.stdout_eof = True
            return
        try:
            event = json.loads(line.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError):
            raise AssertionError("Owned button event was invalid") from None
        assert (
            not received
            and isinstance(event, dict)
            and set(event) == {"button_invoked"}
            and type(event["button_invoked"]) is int
            and event["button_invoked"] == 1
        ), "Owned button event did not prove exactly one click"
        assert events.maxsize == 1 and events.empty(), "Owned button event queue was invalid"
        events.put_nowait(event["button_invoked"])
        received = True


@asynccontextmanager
async def observe_native_form(
    starting: asyncio.Task[asyncio.subprocess.Process],
    title: str,
    timeout: float,
    *,
    clock: Callable[[], float] | None = None,
    button_events: asyncio.Queue[int] | None = None,
) -> AsyncIterator[asyncio.subprocess.Process]:
    loop = asyncio.get_running_loop()
    clock = loop.time if clock is None else clock
    trace = _Trace(clock, clock())
    readers: list[asyncio.Task[None]] = []
    failure: BaseException | None = None
    try:
        # One original operation deadline includes creation and every stage.
        async with timeout_at(loop.time() + timeout):
            process = await asyncio.shield(starting)
            trace.spawn = "returned"
            trace.mark("spawn_returned")
            assert process.stderr is not None
            readers = [
                asyncio.create_task(_read_ready(process, title, trace)),
                asyncio.create_task(_read_stderr(process.stderr, trace)),
            ]
            await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
            # A simultaneous stderr failure takes precedence over readiness.
            if readers[1].done():
                readers[1].result()
            await readers[0]
            if button_events is not None:
                readers.append(asyncio.create_task(_read_buttons(process, trace, button_events)))
        yield process
    except BaseException as error:
        failure = error
        raise
    finally:

        async def settle() -> None:
            try:
                adopted = await asyncio.shield(starting)
            except Exception:
                trace.spawn, trace.cleanup = "failed", "not_started"
                return
            trace.spawn = "returned"
            trace.mark("spawn_returned")
            trace.exit_before_cleanup = adopted.returncode
            await _settle_process(adopted, readers)
            trace.exit_after_cleanup = adopted.returncode
            trace.readers_settled = all(task.done() for task in readers)
            trace.reader_failed = any(
                not task.cancelled() and task.exception() is not None for task in readers
            )
            trace.cleanup = "reaped" if adopted.returncode is not None else "incomplete"

        cancelled = False
        try:
            cancelled = await _wait_cleanup(asyncio.create_task(settle()))
        except BaseException:
            if trace.cleanup == "pending":
                trace.cleanup = "failed"
            if failure is None:
                failure = AssertionError("Owned native-window fixture cleanup failed")
                raise failure from None
        finally:
            if failure is not None:
                failure.add_note(trace.note())
        if failure is None and (trace.reader_failed or trace.cleanup == "incomplete"):
            settlement_error = AssertionError(
                "Owned native-window fixture did not settle successfully"
            )
            settlement_error.add_note(trace.note())
            raise settlement_error
        if cancelled and failure is None:
            cancellation = asyncio.CancelledError()
            cancellation.add_note(trace.note())
            raise cancellation
