"""Only hosted Windows CI creates and reads its own disposable Win32 window."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from helpers import uia_native_window
from helpers.uia_native_startup import observe_native_form
from helpers.uia_probe_diagnostics import (
    PENDING_SCRIPT,
    PendingHelperLease,
    ProbeHelperLease,
    observe_pending,
    observe_probe,
    pending_received,
)

from muli.config import Settings
from muli.process import _settle_process
from muli.tools import uia_computer as module
from muli.tools.computer import Computer, ComputerError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true",
    reason="Native UIA validation is restricted to a hosted Windows CI temporary window",
)


@dataclass(frozen=True)
class NativeForm:
    process: asyncio.subprocess.Process
    title: str
    button_events: asyncio.Queue[int]


def settings() -> Settings:
    return Settings.model_validate({"computer": {"enabled": True, "backend": "auto"}})


@pytest_asyncio.fixture
async def native_form() -> AsyncIterator[NativeForm]:
    title = "muli-uia-" + uuid4().hex
    button_events: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
    starting = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *uia_native_window.window_arguments(title),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4096,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    )

    async with observe_native_form(
        starting, title, settings().computer.operation_timeout_seconds, button_events=button_events
    ) as process:
        yield NativeForm(process, title, button_events)
    assert process.returncode is not None, "The owned native target must be reaped"


async def discover(computer: Computer, form: NativeForm) -> str:
    result = await computer.targets({})
    matches = [item for item in result["targets"] if item["name"] == form.title]
    assert len(matches) == 1, (
        "UIA must discover the exact owned CI window; no desktop skip/fallback"
    )
    return str(matches[0]["target_id"])


async def test_real_windows_system_module_probe_mta_and_owned_unicode_tree(
    native_form: NativeForm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computer = Computer(settings(), owner=lambda: ("native-ci", "read-1"))
    original_spawn = asyncio.create_subprocess_exec
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    poison = tmp_path / "untrusted-modules"
    poison.mkdir()

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        # Only this owned child sees the replacement. HOME/user profiles stay untouched.
        kwargs["env"] = {**os.environ, "PSModulePath": str(poison)}
        calls.append((argv, kwargs))
        return await original_spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    # Observe only this original factory call; later UIA leases stay unchanged.
    with monkeypatch.context() as construction:
        construction.setattr(module, "AXHelperLease", ProbeHelperLease)
        probe = computer._uia._new_lease(once=True)
    try:
        result = await observe_probe(probe)
        assert result["apartment"] == "MTA" and result["has_window"] is False
        assert result["no_profile"] is True and result["non_interactive"] is True
        assert result["module_autoloading"] == "None"
        assert PureWindowsPath(result["utility_path"]) == PureWindowsPath(
            result["powershell_home"],
            "Modules",
            "Microsoft.PowerShell.Utility",
            "Microsoft.PowerShell.Utility.psd1",
        )
        assert result["dependency_available"] is True and result["session_accessible"] is None
        assert probe.closed and probe._process is not None
        assert probe._process.returncode is not None

        identifier = await discover(computer, native_form)
        snapshot = await computer.snapshot({"target_id": identifier})
        encoded = json.dumps(snapshot, ensure_ascii=False)
        assert len(encoded) <= computer.settings.limits.max_output_chars
        assert '中文🌊 "quote" \\ slash' in [item["name"] for item in snapshot["nodes"]]
        protected = [item for item in snapshot["nodes"] if item["protected"]]
        assert protected and all(item["name"] == "" for item in protected)
        assert "private-password-label" not in encoded and "private-password-value" not in encoded
        assert all(item["editable"] is False for item in snapshot["nodes"])
        assert all(item["actions"] == [] and item["read_only"] for item in protected)
        buttons = [
            item for item in snapshot["nodes"] if item["name"] == uia_native_window.BUTTON_NAME
        ]
        assert len(buttons) == 1
        assert buttons[0]["actions"] == ["invoke"] and buttons[0]["read_only"] is False
        assert snapshot["scope"] == "window"
        lease = computer._uia._lease
        assert lease is not None and lease._process is not None
        assert lease._process.pid != native_form.process.pid
        await computer.release({})
        assert lease.closed and lease._process.returncode is not None
        assert native_form.process.returncode is None, (
            "Reader cleanup must not terminate the target"
        )
        assert calls
        for argv, kwargs in calls:
            assert "-NoProfile" in argv and "-Mta" in argv and "-NonInteractive" in argv
            assert kwargs["creationflags"] & int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert not any("ExecutionPolicy" in arg or arg == "-Command" for arg in argv)
    finally:
        await computer.aclose()


async def test_real_windows_closed_owned_window_cannot_be_researched_by_old_reference(
    native_form: NativeForm,
) -> None:
    computer = Computer(settings(), owner=lambda: ("native-ci", "read-2"))
    try:
        identifier = await discover(computer, native_form)
        await _settle_process(native_form.process, [])
        with pytest.raises(ComputerError) as missing:
            await computer.snapshot({"target_id": identifier})
        assert missing.value.code in {"element_unavailable", "target_changed"}
        assert missing.value.outcome == "not_started" and not computer._uia._targets
        assert computer._uia._lease is None or computer._uia._lease.closed
    finally:
        await computer.aclose()


async def test_real_windows_invoke_receipt_and_exact_owned_button_click_are_separate(
    native_form: NativeForm,
) -> None:
    computer = Computer(settings(), owner=lambda: ("native-ci", "invoke"))
    try:
        identifier = await discover(computer, native_form)
        snapshot = await computer.snapshot({"target_id": identifier})
        buttons = [
            item for item in snapshot["nodes"] if item["name"] == uia_native_window.BUTTON_NAME
        ]
        disabled = [
            item
            for item in snapshot["nodes"]
            if item["name"] == uia_native_window.DISABLED_BUTTON_NAME
        ]
        assert len(buttons) == len(disabled) == 1
        assert buttons[0]["actions"] == ["invoke"] and buttons[0]["read_only"] is False
        assert disabled[0]["enabled"] is False and disabled[0]["actions"] == []
        assert all(item["actions"] == [] for item in snapshot["nodes"] if item["protected"])
        with pytest.raises(ComputerError) as unavailable:
            await computer.action(
                {
                    "snapshot_id": snapshot["snapshot_id"],
                    "node_id": disabled[0]["id"],
                    "action": "invoke",
                }
            )
        assert unavailable.value.code == "unsupported_operation"
        assert unavailable.value.outcome == "not_started" and native_form.button_events.empty()
        # A rejected attempt consumed the whole snapshot. The positive action
        # needs a new explicit observation, never an implicit replay/fallback.
        identifier = await discover(computer, native_form)
        snapshot = await computer.snapshot({"target_id": identifier})
        buttons = [
            item for item in snapshot["nodes"] if item["name"] == uia_native_window.BUTTON_NAME
        ]
        assert len(buttons) == 1
        arguments = {
            "snapshot_id": snapshot["snapshot_id"],
            "node_id": buttons[0]["id"],
            "action": "invoke",
        }
        lease = computer._uia._lease
        assert lease is not None and lease._process is not None
        deadline = asyncio.get_running_loop().time() + computer.config.operation_timeout_seconds
        async with asyncio.timeout_at(deadline):
            assert await computer.action(arguments) == {
                "ok": True,
                "outcome": "accepted",
                "effect_verified": False,
            }
            # WM_COMMAND carries this owned HWND/control ID; readiness is not
            # evidence that Invoke actually reached the button.
            assert await native_form.button_events.get() == 1
        with pytest.raises(ComputerError) as old:
            await computer.action(arguments)
        assert old.value.code == "stale_snapshot" and native_form.button_events.empty()
        assert lease.closed and lease._process.returncode is not None
        assert native_form.process.returncode is None
    finally:
        await computer.aclose()


async def test_real_windows_pending_powershell_cancellation_reaps_only_owned_helper(
    tmp_path: Path,
) -> None:
    # A fixed test-only sleeper stands in for a COM call which does not return.
    # This proves transport/process cancellation, not a real provider's behavior.
    script = tmp_path / "owned-pending.ps1"
    script.write_text(PENDING_SCRIPT, encoding="utf-8")
    configuration = settings().computer
    lease = PendingHelperLease(
        [str(module._powershell()), "-NoProfile", "-NonInteractive", "-Mta", "-File", str(script)],
        timeout=configuration.operation_timeout_seconds,
        ttl=configuration.snapshot_ttl_seconds,
        max_nodes=configuration.max_nodes,
    )
    async with observe_pending(lease) as reading:
        async with asyncio.timeout(configuration.operation_timeout_seconds):
            while not pending_received(lease):
                if reading.done():
                    await reading
                    raise AssertionError("Pending helper unexpectedly returned")
                await asyncio.sleep(0)
        assert lease._process is not None
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert lease.closed and lease._process.returncode is not None
        assert not lease._readers or all(task.done() for task in lease._readers)
