"""Fixed-controller recovery entry; only its authenticated helper drives effects.

recover ensures one bounded transient unit. run verifies that exact unit/PID
before using the shared controller. Neither path opens Runtime or Store, loads
a selected target package, installs candidates, or migrates application data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from muli.compatibility import assess_compatibility
from muli.managed_controller import ManagedTransactionController
from muli.managed_journal import read_snapshots
from muli.managed_transaction import TransactionRecordV2, parse_record_v2
from muli.management_helper import _root, drive_loop, helper_launch_arguments
from muli.management_transaction import (
    ClockReading,
    JournalControl,
    Role,
    TransactionError,
    TransactionJournal,
    _digest,
)
from muli.platforms.filesystem_identity import verify_local_filesystems
from muli.platforms.installation import _read_file
from muli.platforms.linux_transaction import (
    CommandRunner,
    ProbeLimits,
    SystemdProbe,
    clock_reading,
)
from muli.platforms.managed_backend import ManagedLinuxBackend
from muli.platforms.managed_deployment import (
    DeploymentAnchor,
    DeploymentStore,
    SlotIdentity,
    verify_current_slot,
)
from muli.platforms.managed_launcher import resolve_entry
from muli.platforms.managed_probe import ManagedSystemdProbe
from muli.process import run_process

_ROLES: tuple[Role, ...] = ("base", "target")


@dataclass(frozen=True, slots=True)
class _Entry:
    record: TransactionRecordV2
    anchor: DeploymentAnchor
    controller: SlotIdentity


def managed_helper_unit_name(record: TransactionRecordV2) -> str:
    identity = json.dumps(
        [2, record.identity.deployment, record.generation], separators=(",", ":")
    ).encode()
    return "muli-transaction-" + hashlib.sha256(identity).hexdigest() + ".service"


def managed_helper_arguments(anchor: DeploymentAnchor, controller: SlotIdentity) -> tuple[str, ...]:
    if controller.digest != anchor.controller_slot:
        raise TransactionError("foreign_managed_controller")
    return (
        controller.lexical_python,
        "-I",
        "-m",
        "muli.managed_host",
        "--root",
        str(_root(Path(anchor.journal_root))),
        "run",
    )


def _entry(root: Path) -> _Entry:
    root = _root(root)
    # The retained stdlib route verifies the fixed anchor/controller, works with
    # a missing or broken selector, and bounds the preliminary state read.
    plan = resolve_entry(root, purpose="recover")
    with DeploymentStore(root) as store:
        anchor = store.read_anchor(plan.deployment)
        controller = store.read_slot(anchor.controller_slot)
        data = store.directory.read("state.json", anchor.journal.max_record_bytes)
        record = parse_record_v2(data, limits=anchor.journal)
        if (
            record.identity.deployment != anchor.digest
            or plan.installation != controller.digest
            or plan.generation != record.generation
            or plan.revision != record.revision
            or plan.state_digest != _digest(data)
            or Path(anchor.journal_root) != root
        ):
            raise TransactionError("managed_entry_changed")
        store.verify_anchor(anchor)
        verify_current_slot(controller)
        if store.directory.read("state.json", anchor.journal.max_record_bytes) != data:
            raise TransactionError("managed_entry_changed")
        return _Entry(record, anchor, controller)


def _record(
    control: JournalControl, anchor: DeploymentAnchor
) -> tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]]:
    control.check()
    journal = control.journal
    if (
        str(journal.directory.path) != anchor.journal_root
        or str(journal.database) != anchor.database_path
        or Path(anchor.database_path) != Path(anchor.data_dir) / "muli.sqlite3"
        or journal.limits != anchor.journal
    ):
        raise TransactionError("managed_paths_changed")
    journal.directory.check()
    journal.writer_directory.check()
    verify_local_filesystems(
        (
            journal.directory.path,
            Path(anchor.config_path).parent,
            journal.database.parent,
            journal.writer_directory.path,
        )
    )
    # Validate path chains and ordinary owner files without reading SQLite or
    # constructing an application connection/configuration writer.
    _read_file(Path(anchor.config_path), 0, contents=False, private_owner=True)
    _read_file(journal.database, 0, contents=False, private_owner=True)
    with DeploymentStore(journal.directory.path, journal.limits) as store:
        if store.read_anchor(anchor.digest) != anchor:
            raise TransactionError("managed_deployment_changed")
        store.verify_anchor(anchor)
        controller = store.read_slot(anchor.controller_slot)
        record = control.read_current()
        if not isinstance(record, TransactionRecordV2):
            raise TransactionError("managed_transaction_required")
        if (
            record.identity.deployment != anchor.digest
            or record.deadline.seconds != anchor.helper.seconds
            or record.deadline.rollback_reserve != anchor.helper.rollback_reserve
        ):
            raise TransactionError("foreign_managed_deployment")
        slots: dict[Role, SlotIdentity] = {}
        for role in _ROLES:
            binding = record.binding(role)
            slot = store.read_slot(binding.installation)
            if slot.compatibility_digest != binding.compatibility:
                raise TransactionError("managed_compatibility_changed")
            read_snapshots(journal, record, role)
            slots[role] = slot
        if not assess_compatibility(
            slots["base"].compatibility,
            slots["target"].compatibility,
            record.observed_format,
            handoff_protocol=2,
        ).format_compatible:
            raise TransactionError("managed_compatibility_changed")
        # Only controller files are authenticated here. A damaged target may
        # still have to be stopped and replaced by the frozen healthy base.
        if control.read_current() != record:
            raise TransactionError("transaction_changed")
        return record, controller, slots


async def ensure_managed_helper(
    control: JournalControl,
    anchor: DeploymentAnchor,
    *,
    probe: SystemdProbe | None = None,
    runner: CommandRunner = run_process,
) -> Literal["running", "accepted"]:
    """Ensure one authenticated helper; a successful launch is only accepted.

    The application host may be either slot. It need not be the controller,
    whereas the executable selected for the new helper is always the retained
    controller. An existing invalid unit never permits another name or reset.
    """
    record, controller, _ = _record(control, anchor)
    if record.resolved or record.phase == "recovery_required":
        raise TransactionError("transaction_not_pending")
    limits = anchor.helper
    observed = probe or SystemdProbe(
        runner=runner, limits=ProbeLimits(seconds=limits.operation_timeout / 4)
    )
    if observed.limits.seconds > limits.operation_timeout / 4:
        raise TransactionError("invalid_probe_timeout")
    name, argv = managed_helper_unit_name(record), managed_helper_arguments(anchor, controller)
    async with asyncio.timeout(limits.operation_timeout):
        if await observed.helper_exists(name):
            await observed.read_helper(name, argv, limits)
            if _record(control, anchor)[0] != record:
                raise TransactionError("transaction_changed")
            return "running"
        if _record(control, anchor)[0] != record:
            raise TransactionError("transaction_changed")
        result = await runner(
            helper_launch_arguments(name, argv, limits),
            timeout=observed.limits.seconds,
            limit=observed.limits.command_bytes,
            merge_stderr=True,
        )
        if result.get("exit_code") != 0:
            raise TransactionError("helper_start_failed")
        return "accepted"


async def recover(
    root: Path,
    *,
    probe: SystemdProbe | None = None,
    runner: CommandRunner = run_process,
) -> Literal["running", "accepted"]:
    entry = _entry(root)
    anchor = entry.anchor
    with TransactionJournal(
        Path(anchor.journal_root), Path(anchor.database_path), anchor.journal, create=False
    ) as journal:
        with journal.locked() as control:
            if _record(control, anchor)[0] != entry.record:
                raise TransactionError("managed_entry_changed")
            return await ensure_managed_helper(control, anchor, probe=probe, runner=runner)


async def run_managed_helper(
    root: Path,
    *,
    probe: ManagedSystemdProbe | None = None,
    runner: CommandRunner = run_process,
    clock: Callable[[], ClockReading] = clock_reading,
) -> TransactionRecordV2:
    entry = _entry(root)
    anchor, limits = entry.anchor, entry.anchor.helper
    observed = probe or ManagedSystemdProbe(
        runner=runner, limits=ProbeLimits(seconds=limits.operation_timeout / 4)
    )
    if observed.limits.seconds > limits.operation_timeout / 4:
        raise TransactionError("invalid_probe_timeout")
    with TransactionJournal(
        Path(anchor.journal_root), Path(anchor.database_path), anchor.journal, create=False
    ) as journal:
        with journal.locked() as control:
            original, controller, slots = _record(control, anchor)
            if original != entry.record or controller != entry.controller:
                raise TransactionError("managed_entry_changed")

            async def verify_helper() -> None:
                current, current_controller, _ = _record(control, anchor)
                if current.generation != original.generation or current_controller != controller:
                    raise TransactionError("transaction_changed")
                verify_current_slot(controller)
                snapshot = await observed.read_helper(
                    managed_helper_unit_name(original),
                    managed_helper_arguments(anchor, controller),
                    limits,
                )
                if snapshot.process.pid != os.getpid():
                    raise TransactionError("foreign_helper_process")
                verify_current_slot(controller)
                if _record(control, anchor)[0] != current:
                    raise TransactionError("transaction_changed")

            # Authenticate the real helper before creating even a backend
            # client; each subsequent drive step repeats this authentication.
            await verify_helper()
            async with ManagedLinuxBackend(
                anchor,
                controller,
                slots,
                journal,
                control=control,
                clock=clock,
                probe=observed,
                runner=runner,
                operation_timeout=limits.operation_timeout,
            ) as backend:
                driver = ManagedTransactionController(
                    control, backend, clock, operation_timeout=limits.operation_timeout
                )

                def read() -> TransactionRecordV2:
                    return _record(control, anchor)[0]

                return await drive_loop(
                    driver.step, read, backend.observe, limits, clock, verify_helper
                )


def main(arguments: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    if len(values) != 3 or values[0] != "--root" or values[2] not in {"recover", "run"}:
        print("managed_host_invalid_arguments", file=sys.stderr)
        return 2
    try:
        if values[2] == "recover":
            result: object = {"state": asyncio.run(recover(Path(values[1])))}
        else:
            record = asyncio.run(run_managed_helper(Path(values[1])))
            result = {
                "phase": record.phase,
                "activation": record.activation,
                "resolved": record.resolved,
            }
        print(json.dumps(result, separators=(",", ":")))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("managed_host_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
