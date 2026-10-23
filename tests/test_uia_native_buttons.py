"""Validate the native test's owned button contract without a desktop or process."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import test_uia_native as native
from helpers import uia_native_window

from muli.config import Settings


def node(name: str, *, actions: list[str] | None = None, **changes: Any) -> dict[str, Any]:
    return {
        "name": name,
        "protected": False,
        "editable": False,
        "actions": [] if actions is None else actions,
        "read_only": not actions,
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    ["extra-button", "missing", "duplicate", "no-invoke", "read-only"],
)
async def test_native_snapshot_requires_its_unique_button_without_excluding_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    owned = node(uia_native_window.BUTTON_NAME, actions=["invoke"])
    nodes = [
        node('中文🌊 "quote" \\ slash'),
        node("", protected=True),
        node("另一个合法按钮 🌊", actions=["invoke"]),
    ]
    if change != "missing":
        nodes.append(owned)
    if change == "duplicate":
        nodes.append(dict(owned))
    elif change == "no-invoke":
        owned["actions"] = []
    elif change == "read-only":
        owned["read_only"] = True

    target = SimpleNamespace(pid=7, returncode=None)
    probe_process = SimpleNamespace(pid=8, returncode=None)
    reader_process = SimpleNamespace(pid=9, returncode=None)
    reader = SimpleNamespace(closed=False, _process=reader_process)

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        assert argv == ("owned-fake-helper", "-NoProfile", "-NonInteractive", "-Mta")
        assert kwargs["creationflags"] == 1
        return cast(asyncio.subprocess.Process, probe_process)

    class Probe:
        def __init__(self) -> None:
            self.closed = False
            self._process = probe_process

        async def request(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert command == "probe" and payload == {}
            await asyncio.create_subprocess_exec(
                "owned-fake-helper", "-NoProfile", "-NonInteractive", "-Mta", creationflags=1
            )
            return {
                "apartment": "MTA",
                "has_window": False,
                "no_profile": True,
                "non_interactive": True,
                "module_autoloading": "None",
                "powershell_home": "C:\\Windows\\PowerShell",
                "utility_path": (
                    "C:\\Windows\\PowerShell\\Modules\\Microsoft.PowerShell.Utility\\"
                    "Microsoft.PowerShell.Utility.psd1"
                ),
                "dependency_available": True,
                "session_accessible": None,
            }

        async def aclose(self) -> None:
            self.closed = True
            probe_process.returncode = 0

    probe = Probe()
    computers: list[Computer] = []

    class Computer:
        def __init__(self, settings: Settings, **kwargs: Any) -> None:
            self.settings = settings
            self._uia = SimpleNamespace(_new_lease=lambda **_: probe, _lease=reader)
            self.closed = False
            computers.append(self)

        async def targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"targets": [{"name": "owned", "target_id": "owned-target"}]}

        async def snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
            assert arguments == {"target_id": "owned-target"}
            return {"nodes": nodes, "scope": "window"}

        async def release(self, arguments: dict[str, Any]) -> dict[str, Any]:
            reader.closed, reader_process.returncode = True, 0
            return {"ok": True}

        async def aclose(self) -> None:
            self.closed = True
            reader.closed, reader_process.returncode = True, 0

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 1, raising=False)
    monkeypatch.setattr(native, "Computer", Computer)
    form = native.NativeForm(cast(asyncio.subprocess.Process, target), "owned", asyncio.Queue())
    operation = native.test_real_windows_system_module_probe_mta_and_owned_unicode_tree(
        form, tmp_path, monkeypatch
    )
    if change == "extra-button":
        await operation
    else:
        with pytest.raises(AssertionError):
            await operation
    assert probe.closed and computers[0].closed
    assert target.returncode is None
