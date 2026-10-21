"""Project only fixed probe stages after the existing lease owner settles."""

from __future__ import annotations

import json
from typing import Any

from muli.tools.ax_lease import MAX_STDERR_BYTES, AXHelperLease

STAGES = (
    "script_entered",
    "utility_importing",
    "utility_ready",
    "probe_received",
    "uia_initializing",
    "uia_ready",
    "probe_encoded",
    "probe_write_returned",
)
_STAGE_LINES = {b"muli-uia-stage=1:" + stage.encode("ascii"): stage for stage in STAGES}
_NOTE_PREFIX = "uia_probe_diagnostic="
_NOTE_BYTES = 4096


def probe_facts(lease: AXHelperLease, *, cleanup_failed: bool) -> dict[str, Any]:
    data = bytes(lease._stderr[:MAX_STDERR_BYTES])
    stages: list[str] = []
    invalid = False
    previous = -1
    for line in data.split(b"\n")[:-1]:
        stage = _STAGE_LINES.get(line.removesuffix(b"\r"))
        if stage is None:
            continue
        position = STAGES.index(stage)
        if position <= previous or stage in stages:
            invalid = True
        if stage not in stages:
            stages.append(stage)
        previous = position

    process = lease._process
    exit_code = process.returncode if process is not None else None
    exit_class = "unavailable" if process is None else "running"
    if type(exit_code) is int:
        exit_class = "zero" if exit_code == 0 else "nonzero"
    readers_settled = all(reader.done() for reader in lease._readers)
    cleanup = (
        not cleanup_failed
        and lease.closed
        and (process is None or type(exit_code) is int)
        and readers_settled
    )
    return {
        "stages": stages,
        "stage_sequence": "invalid" if invalid else "valid" if stages else "unobserved",
        "stderr_prefix_capped": len(data) == MAX_STDERR_BYTES,
        "sent": lease._count
        if type(lease._count) is int and lease._count in {0, 1}
        else "unavailable",
        "lease_closed": lease.closed,
        "process_exists": process is not None,
        "exit": exit_class,
        "readers_settled": readers_settled,
        "cleanup": "confirmed" if cleanup else "unconfirmed",
    }


async def observe_probe(lease: AXHelperLease) -> dict[str, Any]:
    """Keep the original error/outcome, including cancellation and sticky close."""
    failure: BaseException | None = None
    cleanup_failed = False
    try:
        result: dict[str, Any] = await lease.request("probe", {})
        return result
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            await lease.aclose()
        except BaseException:
            cleanup_failed = True
            if failure is None:
                raise
        if failure is not None:
            try:
                encoded = _NOTE_PREFIX + json.dumps(
                    probe_facts(lease, cleanup_failed=cleanup_failed),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if len(encoded.encode("ascii")) <= _NOTE_BYTES:
                    BaseException.add_note(failure, encoded)
            except Exception:
                pass  # Evidence must never replace the original failure.
