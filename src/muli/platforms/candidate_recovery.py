"""Finish one existing plan's candidate subtree; never start/reinstall a command."""

from __future__ import annotations

import asyncio
import sys

from muli.platforms.candidate_process import (
    CandidateProbe,
    CandidateProcessError,
    _settle_subtree,
)
from muli.platforms.candidate_process_state import (
    AcceptedUnit,
    PendingProcess,
    PersistProcess,
    ProcessPersistence,
    current_boot,
)
from muli.platforms.linux_transaction import ProbeLimits
from muli.process import _wait_cleanup


async def recover_candidate_process(
    pending: PendingProcess,
    *,
    persist: PersistProcess,
    probe: CandidateProbe | None = None,
) -> PendingProcess:
    """Consume a typed private plan attachment under the host's management lock.

    This is not a tool/API accepting a unit name from the model. The host first
    authenticates the owner and binds this attachment to its existing plan and
    attempt. A same-boot unobserved absent start remains ambiguous indefinitely.
    """
    if sys.platform != "linux":
        raise CandidateProcessError("candidate_platform_unsupported", cleanup_confirmed=False)
    if pending.phase == "settled":
        # The authenticated Store already owns this durable proof. Its control
        # file may have been removed after that commit; never start/stop again.
        return pending
    state = ProcessPersistence(pending, persist)
    observer = probe or CandidateProbe(
        limits=ProbeLimits(
            seconds=min(5.0, pending.limits.stop_seconds / 4),
            command_bytes=pending.limits.output_bytes,
        )
    )

    async def recover_owned() -> PendingProcess:
        try:
            async with asyncio.timeout(2 * pending.limits.stop_seconds):
                pending.check_files()
                boot = current_boot()
                before = await observer.recoverable(
                    pending.unit, pending.worker, pending.limits, pending.runtime_us
                )
                if boot != pending.boot:
                    # A new kernel has neither the old request nor its processes.
                    # A current same-name object remains a conflict, never a kill target.
                    if before is not None or await observer.exists(pending.unit):
                        raise CandidateProcessError("candidate_invocation_changed")
                    pending.check_files()
                    if current_boot() != boot:
                        raise CandidateProcessError("candidate_boot_changed")
                    return state.settle()
                if before is not None:
                    state.accept(AcceptedUnit.capture(before.unit, before.process))
                elif pending.accepted is None:
                    raise CandidateProcessError("candidate_start_unconfirmed")
                if not await _settle_subtree(
                    observer,
                    pending.unit,
                    pending.worker,
                    pending.limits,
                    state.value.accepted,
                    state.accept,
                ):
                    raise CandidateProcessError("candidate_cleanup_unconfirmed")
                state.value.check_files()
                if current_boot() != boot:
                    raise CandidateProcessError("candidate_boot_changed")
                return state.settle()
        except Exception as error:
            code = (
                error.code
                if isinstance(error, CandidateProcessError)
                else "candidate_cleanup_unconfirmed"
            )
            raise CandidateProcessError(
                code, unit_name=pending.unit, cleanup_confirmed=False
            ) from None

    task = asyncio.create_task(recover_owned())

    async def settle() -> None:
        await task

    cancelled = await _wait_cleanup(asyncio.create_task(settle()))
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
