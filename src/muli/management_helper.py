"""Bounded Linux transaction helper, independent of the application lifetime.

The sole command-line argument is a private journal root. No Runtime, Store,
model or application configuration writer is constructed in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from muli.managed_transaction import TransactionRecordV2
from muli.management_controller import ControllerBackend, TransactionController
from muli.management_transaction import (
    ClockReading,
    JournalControl,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
)
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.installation import HelperLimits, InstallationManifest
from muli.platforms.linux_controller import LinuxControllerBackend
from muli.platforms.linux_transaction import (
    CommandRunner,
    ProbeLimits,
    SystemdProbe,
    clock_reading,
)
from muli.process import run_process


def _root(path: Path) -> Path:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or len(os.fsencode(path)) > 4096
        or any(character in str(path) for character in "\x00\r\n")
        or path.resolve(strict=True) != path
    ):
        raise TransactionError("invalid_journal_root")
    return path


def helper_unit_name(record: TransactionRecord) -> str:
    identity = json.dumps(
        [record.identity.installation, record.generation], separators=(",", ":")
    ).encode("utf-8")
    return "muli-transaction-" + hashlib.sha256(identity).hexdigest() + ".service"


def helper_arguments(manifest: InstallationManifest, root: Path) -> tuple[str, ...]:
    return (
        manifest.interpreter.executable,
        "-I",
        "-m",
        "muli.management_helper",
        str(_root(root)),
    )


def _verify(manifest: InstallationManifest, journal: TransactionJournal) -> None:
    journal.directory.check()
    journal.writer_directory.check()
    verify_local_filesystems(
        (
            journal.directory.path,
            Path(manifest.config_path).parent,
            journal.database.parent,
            journal.writer_directory.path,
        )
    )
    manifest.verify()
    journal.directory.check()
    journal.writer_directory.check()


def _record(control: JournalControl, manifest: InstallationManifest) -> TransactionRecord:
    _verify(manifest, control.journal)
    record = control.read()  # Includes the journal adoption barrier.
    if record is None:
        raise TransactionError("transaction_missing")
    if (
        control.journal.database != Path(manifest.database_path)
        or control.journal.limits != manifest.journal
        or record.identity.installation != manifest.digest
        or record.identity.service != manifest.service_digest
        or record.deadline.seconds != manifest.helper.seconds
        or record.deadline.rollback_reserve != manifest.helper.rollback_reserve
        or InstallationManifest.load(control.directory.path) != manifest
    ):
        raise TransactionError("foreign_installation")
    return record


def _seconds(value: float) -> str:
    if round(value * 1_000_000) <= 0:
        raise TransactionError("invalid_helper_duration")
    return f"{value:.6f}s"


def _launch_arguments(
    manifest: InstallationManifest, record: TransactionRecord, root: Path
) -> list[str]:
    return helper_launch_arguments(
        helper_unit_name(record), helper_arguments(manifest, root), manifest.helper
    )


def helper_launch_arguments(name: str, argv: tuple[str, ...], limits: HelperLimits) -> list[str]:
    """Share the exact existing resource envelope, without selecting an identity."""
    properties: dict[str, str | int] = {
        "WorkingDirectory": "/",
        "CollectMode": "inactive",
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "Restart": "on-failure",
        "RestartSec": _seconds(limits.restart_delay),
        "StartLimitIntervalSec": _seconds(limits.seconds),
        "StartLimitBurst": limits.restart_burst,
        "RuntimeMaxSec": _seconds(limits.seconds),
        "TimeoutStopSec": _seconds(limits.stop_seconds),
        "MemoryMax": limits.memory_bytes,
        "MemorySwapMax": 0,
        "CPUQuota": f"{limits.cpu_percent}%",
        "TasksMax": limits.tasks,
        "LimitCORE": 0,
        "UMask": "0077",
        "NoNewPrivileges": "yes",
        "OOMPolicy": "kill",
    }
    return [
        "systemd-run",
        "--user",
        "--no-block",
        "--service-type=exec",
        "--expand-environment=no",
        "--unit=" + name,
        *(f"--property={name}={value}" for name, value in properties.items()),
        "--",
        *argv,
    ]


async def ensure_helper(
    control: JournalControl,
    manifest: InstallationManifest,
    *,
    probe: SystemdProbe | None = None,
    runner: CommandRunner = run_process,
) -> Literal["running", "accepted"]:
    """Caller already owns the journal flock; accepted is not running or healthy.

    Never restart/reset an existing unit or pick a second name after conflict.
    An expired transaction may launch solely to persist its deadline outcome;
    the existing controller rejects application effects after that deadline.
    """
    record = _record(control, manifest)
    if record.resolved or record.phase == "recovery_required":
        raise TransactionError("transaction_not_pending")
    limits = manifest.helper
    observed = probe or SystemdProbe(
        runner=runner, limits=ProbeLimits(seconds=limits.operation_timeout / 4)
    )
    if observed.limits.seconds > limits.operation_timeout / 4:
        raise TransactionError("invalid_probe_timeout")
    name = helper_unit_name(record)
    argv = helper_arguments(manifest, control.directory.path)
    async with asyncio.timeout(limits.operation_timeout):
        if await observed.helper_exists(name):
            await observed.read_helper(name, argv, limits)
            if _record(control, manifest) != record:
                raise TransactionError("transaction_changed")
            return "running"
        if _record(control, manifest) != record:
            raise TransactionError("transaction_changed")
        result = await runner(
            _launch_arguments(manifest, record, control.directory.path),
            timeout=observed.limits.seconds,
            limit=observed.limits.command_bytes,
            merge_stderr=True,
        )
        if result.get("exit_code") != 0:
            raise TransactionError("helper_start_failed")
        return "accepted"


async def _drive(
    control: JournalControl,
    backend: ControllerBackend,
    limits: HelperLimits,
    clock: Callable[[], ClockReading],
    verify_helper: Callable[[], Awaitable[None]],
    *,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> TransactionRecord:
    driver = TransactionController(
        control, backend, clock, operation_timeout=limits.operation_timeout
    )
    return await drive_loop(
        driver.step, control.read, backend.observe, limits, clock, verify_helper, wait=wait
    )


async def drive_loop[RecordT: TransactionRecord | TransactionRecordV2](
    step: Callable[[], Awaitable[RecordT]],
    read: Callable[[], RecordT | None],
    observe_final: Callable[[RecordT], Awaitable[object]],
    limits: HelperLimits,
    clock: Callable[[], ClockReading],
    verify_helper: Callable[[], Awaitable[None]],
    *,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RecordT:
    """Drive either typed controller without changing its proof/effect policy."""
    while True:
        await verify_helper()
        before = read()
        record = await step()
        if record.resolved:
            # A lost final hint cannot undo released or leave an implicit phase.
            try:
                async with asyncio.timeout(limits.operation_timeout):
                    await observe_final(record)
            except Exception:
                pass
            return record
        if record.phase == "recovery_required" or record.activation == "blocked":
            return record
        if record == before:
            # Clock failure is durably classified by the next controller step.
            try:
                remaining = record.deadline.remaining(clock())
            except TransactionError as exc:
                if exc.code != "clock_invalid":
                    raise
                continue
            if remaining > 0:
                await wait(min(limits.poll_interval, remaining))


async def run_helper(
    root: Path,
    *,
    probe: SystemdProbe | None = None,
    runner: CommandRunner = run_process,
    clock: Callable[[], ClockReading] = clock_reading,
) -> TransactionRecord:
    """Verify this exact helper before driving the original durable transaction."""
    root = _root(root)
    manifest = InstallationManifest.load(root)
    manifest.verify()
    limits = manifest.helper
    observed = probe or SystemdProbe(
        runner=runner, limits=ProbeLimits(seconds=limits.operation_timeout / 4)
    )
    if observed.limits.seconds > limits.operation_timeout / 4:
        raise TransactionError("invalid_probe_timeout")
    with TransactionJournal(root, Path(manifest.database_path), manifest.journal) as journal:
        with journal.locked() as control:
            original = _record(control, manifest)

            async def verify_helper() -> None:
                current = _record(control, manifest)
                if current.generation != original.generation:
                    raise TransactionError("transaction_changed")
                snapshot = await observed.read_helper(
                    helper_unit_name(original), helper_arguments(manifest, root), limits
                )
                if snapshot.process.pid != os.getpid():
                    raise TransactionError("foreign_helper_process")
                if _record(control, manifest) != current:
                    raise TransactionError("transaction_changed")

            async with LinuxControllerBackend(
                manifest.user_service(),
                journal,
                lambda: _verify(manifest, journal),
                control=control,
                clock=clock,
                probe=observed,
                runner=runner,
                operation_timeout=limits.operation_timeout,
            ) as backend:
                return await _drive(control, backend, limits, clock, verify_helper)


def main() -> int:
    if len(sys.argv) != 2:
        print("helper_invalid_arguments", file=sys.stderr)
        return 2
    try:
        asyncio.run(run_helper(Path(sys.argv[1])))
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Never expose raw paths, configuration, nonce or environment in errors.
        print("helper_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
