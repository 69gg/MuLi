"""Test-only control of one owned probe's Windows console creation mode."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

import pytest

SPAWN_MODE_ENV = "MULI_TEST_UIA_PROBE_SPAWN_MODE"
SpawnMode = Literal["create-no-window", "default"]


def probe_spawn_mode() -> SpawnMode | None:
    value = os.environ.get(SPAWN_MODE_ENV)
    if value is None:
        return None
    if value not in ("create-no-window", "default"):
        raise AssertionError("Unknown owned UIA probe spawn mode")
    return cast(SpawnMode, value)


@dataclass(slots=True)
class ProbeSpawn:
    mode: SpawnMode
    actual_flags: int
    calls: int = 0


def install_probe_spawn(
    monkeypatch: pytest.MonkeyPatch, expected_argv: Sequence[str], mode: SpawnMode | None
) -> ProbeSpawn | None:
    if mode is None:
        return None
    original_flags = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if type(original_flags) is not int or original_flags <= 0:
        raise AssertionError("The controlled probe requires the Windows console flag")
    expected = tuple(expected_argv)
    original_spawn = asyncio.create_subprocess_exec
    observation = ProbeSpawn(mode, original_flags if mode == "create-no-window" else 0)

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        if argv != expected:
            return await original_spawn(*argv, **kwargs)
        if kwargs.get("creationflags") != original_flags:
            raise AssertionError("The owned probe's original console flag changed")
        if any(
            kwargs.get(name) != asyncio.subprocess.PIPE for name in ("stdin", "stdout", "stderr")
        ):
            raise AssertionError("The owned probe must retain all three pipes")
        if observation.calls:
            raise AssertionError("The controlled probe must spawn only once")
        observation.calls += 1
        kwargs["creationflags"] = observation.actual_flags
        print(
            f"uia_probe_spawn mode={observation.mode} actual_flags={observation.actual_flags}",
            flush=True,
        )
        return await original_spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return observation
