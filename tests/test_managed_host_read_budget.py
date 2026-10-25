"""The loop's scheduling read does not repeat adjacent full authentication."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from test_managed_host import Backend, Environment, Probe
from test_managed_host import backend as backend
from test_managed_host import current_controller as current_controller
from test_managed_host import environment as environment
from test_managed_host import filesystems as filesystems

import muli.managed_host as host_module
import muli.platforms.managed_launcher as launcher_module
from muli.managed_host import run_managed_helper
from muli.managed_journal import read_snapshots
from muli.managed_transaction import TransactionRecordV2
from muli.management_transaction import (
    ClockReading,
    JournalControl,
    Role,
    ServiceIdentity,
    TransactionError,
    TransactionJournal,
    TransactionRecord,
)
from muli.platforms.installation import HelperLimits
from muli.platforms.managed_deployment import DeploymentAnchor, SlotIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed helper/real locks")


async def test_helper_loop_reads_fresh_record_without_a_third_complete_authentication(
    environment: Environment,
    backend: type[Backend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete_checks = 0
    python_checks = 0
    current_reads = 0
    scheduling_reads = 0
    original_record = host_module._record
    original_python = launcher_module.python_fingerprint
    original_current = JournalControl.read_current
    original_loop = host_module.drive_loop

    def record(
        control: JournalControl, anchor: DeploymentAnchor
    ) -> tuple[TransactionRecordV2, SlotIdentity, dict[Role, SlotIdentity]]:
        nonlocal complete_checks
        result = original_record(control, anchor)
        complete_checks += 1
        return result

    def python(
        path: Path, max_bytes: int, *, verified: dict[str, Any] | None = None
    ) -> dict[str, object]:
        nonlocal python_checks
        result = original_python(path, max_bytes, verified=verified)
        python_checks += 1
        return result

    def current(control: JournalControl) -> TransactionRecord | TransactionRecordV2 | None:
        nonlocal current_reads
        result = original_current(control)
        current_reads += 1
        return result

    async def loop(
        step: Callable[[], Awaitable[TransactionRecordV2]],
        read: Callable[[], TransactionRecordV2 | None],
        observe_final: Callable[[TransactionRecordV2], Awaitable[object]],
        limits: HelperLimits,
        clock: Callable[[], ClockReading],
        verify_helper: Callable[[], Awaitable[None]],
    ) -> TransactionRecordV2:
        async def verified() -> None:
            before = complete_checks, python_checks
            await verify_helper()
            # Both real _record calls, including their Python hashes, remain
            # on the two sides of the real helper observation await.
            assert (complete_checks, python_checks) == (before[0] + 2, before[1] + 2)

        def fresh_read() -> TransactionRecordV2:
            nonlocal scheduling_reads
            before = complete_checks, python_checks, current_reads
            value = read()
            assert isinstance(value, TransactionRecordV2)
            assert value == environment.journal.read_current()
            assert current_reads > before[2]
            assert (complete_checks, python_checks) == before[:2], (
                "scheduling read repeated complete authentication after the helper's post-await check",
                complete_checks - before[0],
                python_checks - before[1],
            )
            scheduling_reads += 1
            return value

        return await original_loop(step, fresh_read, observe_final, limits, clock, verified)

    monkeypatch.setattr(host_module, "_record", record)
    monkeypatch.setattr(launcher_module, "python_fingerprint", python)
    monkeypatch.setattr(JournalControl, "read_current", current)
    monkeypatch.setattr(host_module, "drive_loop", loop)

    final = await run_managed_helper(
        environment.journal.directory.path, probe=Probe(), clock=environment.clock
    )

    assert final.resolved and final.phase == "committed"
    assert final.deadline == environment.record.deadline
    assert scheduling_reads > 0
    assert backend.instances[-1].closed
    assert backend.instances[-1].calls.count("start:target") == 1


@pytest.mark.parametrize("replacement", ["missing", "legacy"])
async def test_helper_scheduling_read_rejects_missing_or_legacy_record(
    environment: Environment,
    backend: type[Backend],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    original = environment.record
    base = read_snapshots(environment.journal, original, "base").configuration
    target = read_snapshots(environment.journal, original, "target").configuration
    root = environment.journal.directory.path.parent / "legacy-journal"
    with TransactionJournal(root, root.parent / "legacy-data/muli.sqlite3") as journal:
        with journal.writer_lease() as writer, journal.locked() as control:
            legacy = control.prepare(
                request_id=original.request_id,
                action="configure",
                identity=ServiceIdentity(
                    installation=environment.slots["base"].digest,
                    service=environment.anchor.digest,
                    store_instance=original.identity.store_instance,
                    main_session=original.identity.main_session,
                    origin_boot=original.identity.origin_boot,
                ),
                base=base,
                target=target,
                deadline=original.deadline,
                writer=writer,
            )
    controls: list[JournalControl] = []
    original_current = JournalControl.read_current

    def current(control: JournalControl) -> TransactionRecord | TransactionRecordV2 | None:
        value = original_current(control)
        controls.append(control)
        return value

    async def loop(
        step: Callable[[], Awaitable[TransactionRecordV2]],
        read: Callable[[], TransactionRecordV2 | None],
        observe_final: Callable[[TransactionRecordV2], Awaitable[object]],
        limits: HelperLimits,
        clock: Callable[[], ClockReading],
        verify_helper: Callable[[], Awaitable[None]],
    ) -> TransactionRecordV2:
        await verify_helper()
        control = controls[-1]
        distinct_journal = control.journal is not journal
        assert distinct_journal
        assert control.read_current() == original
        if replacement == "missing":
            (control.directory.path / "state.json").unlink()
            assert control.read_current() is None
        else:
            control._write(legacy)
            assert control.read_current() == legacy
        read()
        raise AssertionError("invalid scheduling record was accepted")

    monkeypatch.setattr(JournalControl, "read_current", current)
    monkeypatch.setattr(host_module, "drive_loop", loop)

    with pytest.raises(TransactionError, match="^managed_transaction_required$"):
        await run_managed_helper(
            environment.journal.directory.path, probe=Probe(), clock=environment.clock
        )

    assert len(backend.instances) == 1
    assert backend.instances[0].closed and not backend.instances[0].calls
