"""Protocol/ownership tests never start PowerShell or inspect a desktop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_ax_computer import FakeLease

import muli.tools.computer as computer_module
import muli.tools.uia_computer as module
from muli.config import Settings
from muli.tools.ax_lease import AXLeaseError
from muli.tools.computer import Computer, ComputerError


@pytest.fixture(autouse=True)
def no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("UIA protocol tests must not spawn")),
    )


def record(ref: str = "window", **changes: Any) -> dict[str, Any]:
    return {
        "ref_id": ref,
        "parent_ref_id": None,
        "depth": 0,
        "kind": "window",
        "name": "窗口 中\U0001f30a",
        "role": "ControlType.Window",
        "actions": [],
        "editable": False,
        "enabled": True,
        "protected": False,
        "read_only": True,
        **changes,
    }


def frame(command: str = "targets", rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    values = [record()] if rows is None else rows
    return {
        "scope": "desktop_windows" if command == "targets" else "window",
        "targets" if command == "targets" else "nodes": values,
        "visited": len(values),
        "complete": True,
        "truncated": False,
        "omissions": [],
    }


PROBE = {
    "backend": "uia",
    "apartment": "MTA",
    "dependency_available": True,
    "session_accessible": None,
    "discovery_scope": "desktop_windows",
    "helper_pid": 42,
    "utility_path": "private-system-path",
    "powershell_home": "private-home",
}


def setup(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any], *, budget: int = 24000
) -> tuple[Computer, FakeLease, list[tuple[str, str] | None]]:
    # Do not mutate the process-global sys.platform, including on real Windows CI.
    monkeypatch.setattr(computer_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    owner: list[tuple[str, str] | None] = [("main", "run-1")]
    computer = Computer(
        Settings.model_validate(
            {
                "computer": {"enabled": True, "backend": "auto"},
                "limits": {"max_output_chars": budget},
            }
        ),
        owner=lambda: owner[0],
    )
    lease = FakeLease(responses)
    monkeypatch.setattr(computer._uia, "_new_lease", lambda **kwargs: lease)
    return computer, lease, owner


async def test_auto_readonly_window_tree_and_private_reference_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = record("root")
    password = record(
        "password", parent_ref_id="root", depth=1, kind="node", name="", protected=True
    )
    computer, lease, _ = setup(monkeypatch, [frame(), frame("snapshot", [root, password])])
    try:
        targets = await computer.targets({})
        identifier = targets["targets"][0]["target_id"]
        snapshot = await computer.snapshot({"target_id": identifier})
        assert computer.backend == "uia"
        assert lease.calls == [("targets", {}), ("snapshot", {"target_ref_id": "window"})]
        assert snapshot["scope"] == "window" and snapshot["complete"] is True
        assert snapshot["nodes"][1]["parent_id"] == snapshot["nodes"][0]["id"]
        assert snapshot["nodes"][1]["protected"] is True
        assert snapshot["nodes"][1]["name"] == ""
        assert all(row["read_only"] and row["actions"] == [] for row in snapshot["nodes"])
        for result, field in ((targets, "targets"), (snapshot, "nodes")):
            for row in result[field]:
                assert not {"ref_id", "parent_ref_id", "pid", "handle", "runtime_id"} & row.keys()
        with pytest.raises(ComputerError) as stale:
            await computer.snapshot({"target_id": identifier})
        assert stale.value.code == "target_invalid" and len(lease.calls) == 2
    finally:
        await computer.aclose()


@pytest.mark.parametrize("arguments", [{}, {"target_id": "invented"}, {"pid": 42}])
async def test_snapshot_never_falls_back_to_desktop_or_search(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    computer, lease, _ = setup(monkeypatch, [])
    try:
        with pytest.raises(ComputerError) as rejected:
            await computer.snapshot(arguments)
        assert rejected.value.code == "target_invalid" and not lease.calls
    finally:
        await computer.aclose()


async def test_action_type_and_application_expansion_are_explicitly_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(monkeypatch, [])
    try:
        for arguments in ({"action": "Invoke"}, {"text": "do not type"}):
            with pytest.raises(ComputerError) as rejected:
                await computer.action(arguments)
            assert rejected.value.code == "unsupported_operation"
            assert rejected.value.outcome == "not_started"
        with pytest.raises(ComputerError) as expanded:
            await computer.targets({"application_id": "invented"})
        assert expanded.value.code == "unsupported_operation" and not lease.calls
        tools = {tool.name: tool for tool in computer.tools()}
        assert tools["computer_targets"].risk == tools["computer_snapshot"].risk == "read"
        assert tools["computer_release"].risk == "read"
        assert tools["computer_action"].risk == tools["computer_type"].risk == "computer"
    finally:
        await computer.aclose()


async def test_foreign_owner_cannot_observe_or_release_current_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, owner = setup(monkeypatch, [frame(), frame("snapshot")])
    try:
        targets = await computer.targets({})
        identifier = targets["targets"][0]["target_id"]
        owner[0] = ("worker", "run-2")
        with pytest.raises(ComputerError) as busy:
            await computer.targets({})
        assert busy.value.code == "desktop_busy"
        assert await computer.release({}) == {"released": True} and not lease.closed
        await computer.release_execution("main", "older-run")
        assert not lease.closed
        owner[0] = ("main", "run-1")
        await computer.snapshot({"target_id": identifier})
        await computer.release_execution("main", "run-1")
        assert lease.closed
    finally:
        await computer.aclose()


async def test_owner_changes_during_read_retire_response_and_exact_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, owner = setup(monkeypatch, [frame()])
    lease.gate = asyncio.Event()
    reading = asyncio.create_task(computer.targets({}))
    await lease.entered.wait()
    owner[0] = ("main", "run-2")
    lease.gate.set()
    try:
        with pytest.raises(ComputerError) as stale:
            await reading
        assert stale.value.code == "stale_snapshot"
        assert lease.closed and not computer._uia._targets
    finally:
        await computer.aclose()


async def test_lease_expiry_never_renews_from_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(monkeypatch, [frame(), frame("snapshot")])
    try:
        targets = await computer.targets({})
        identifier = targets["targets"][0]["target_id"]
        lease.expires_at = asyncio.get_running_loop().time() - 1
        with pytest.raises(ComputerError) as expired:
            await computer.snapshot({"target_id": identifier})
        assert expired.value.code == "stale_snapshot"
        assert lease.closed and not computer._uia._targets
    finally:
        await computer.aclose()


async def test_cancellation_during_read_closes_helper_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(monkeypatch, [frame()])
    lease.gate = asyncio.Event()
    reading = asyncio.create_task(computer.targets({}))
    await lease.entered.wait()
    reading.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert lease.closed and not computer._uia._targets
    finally:
        await computer.aclose()


async def test_repeated_close_cancellation_retains_owned_cleanup_until_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(monkeypatch, [frame()])
    await computer.targets({})
    lease.close_gate = asyncio.Event()
    closing = asyncio.create_task(computer.aclose())
    while lease.state != "closing":
        await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not lease.closed
    lease.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert lease.closed
    await computer.aclose()
    with pytest.raises(ComputerError) as closed:
        await computer.targets({})
    assert closed.value.code == "backend_closed"


@pytest.mark.parametrize(
    "changes",
    [
        {"protected": True},
        {"actions": ["Invoke"]},
        {"editable": True},
        {"read_only": False},
        {"parent_ref_id": "missing", "depth": 1},
        {"kind": []},
        {"depth": True},
    ],
    ids=["password-name", "actions", "editable", "readonly", "parent", "kind", "depth"],
)
async def test_malformed_or_actionable_rows_cannot_enter_public_tree(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    computer, lease, _ = setup(monkeypatch, [frame(rows=[record(**changes)])])
    try:
        with pytest.raises(ComputerError) as invalid:
            await computer.targets({})
        assert invalid.value.code == "protocol_error"
        assert lease.closed and not computer._uia._targets
    finally:
        await computer.aclose()


async def test_final_json_hot_budget_retains_only_complete_usable_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [record(str(index), name='中\U0001f30a\n"\\' * 20) for index in range(8)]
    computer, lease, _ = setup(monkeypatch, [frame(rows=rows), frame(rows=rows)])
    try:
        first = await computer.targets({})
        computer.settings.limits.max_output_chars = 1000
        second = await computer.targets({})
        assert len(json.dumps(second, ensure_ascii=False)) <= 1000
        assert second["complete"] is False and second["truncated"] is True
        assert "output_budget" in second["omission_reasons"]
        assert second["targets"] and len(second["targets"]) < len(first["targets"])
        assert set(computer._uia._targets) == {row["target_id"] for row in second["targets"]}
        assert not set(computer._uia._targets) & {row["target_id"] for row in first["targets"]}
        assert len(lease.calls) == 2
    finally:
        await computer.aclose()


async def test_status_coalesces_probe_without_exposing_native_paths_or_claiming_desktop_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(monkeypatch, [{**PROBE, "unexpected": "private-provider-data"}])
    lease.gate = asyncio.Event()
    first = asyncio.create_task(computer.status())
    await lease.entered.wait()
    second = asyncio.create_task(computer.status())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not lease.closed
    lease.gate.set()
    try:
        result = await second
        assert result["available"] is True and result["session_accessible"] is None
        assert result["supported_operations"] == ["targets", "snapshot"]
        assert result["apartment"] == "MTA"
        assert not {"utility_path", "powershell_home", "helper_pid", "unexpected"} & result.keys()
        assert lease.calls == [("probe", {})] and lease.closed
        assert (await computer.status())["available"] is True
        assert len(lease.calls) == 1
    finally:
        await computer.aclose()


@pytest.mark.parametrize("code", ["dependency_missing", "element_unavailable", "read_timeout"])
async def test_native_error_text_is_not_forwarded(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    computer, lease, _ = setup(monkeypatch, [AXLeaseError(code, "secret-provider-data")])
    try:
        with pytest.raises(ComputerError) as failed:
            await computer.targets({})
        assert failed.value.code == code
        assert "secret-provider-data" not in str(failed.value)
        assert failed.value.outcome == "not_started" and lease.closed
    finally:
        await computer.aclose()


async def test_disabled_or_ownerless_read_does_not_allocate_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, owner = setup(monkeypatch, [])
    try:
        owner[0] = None
        with pytest.raises(ComputerError) as caller:
            await computer.targets({})
        assert caller.value.code == "caller_unavailable"
        computer.config.enabled = False
        assert (await computer.status())["code"] == "disabled"
        assert not lease.calls
    finally:
        await computer.aclose()


async def test_trusted_fixed_host_argv_never_adds_execution_policy_or_source_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, _, _ = setup(monkeypatch, [])
    monkeypatch.setattr(module, "_powershell", lambda: Path("system") / "powershell.exe")
    lease = module.UIAComputer._new_lease(computer._uia)
    try:
        assert lease._argv == (
            str(Path("system") / "powershell.exe"),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Mta",
            "-File",
            str(Path(module.__file__).with_name("uia_helper.ps1")),
        )
        assert not any("ExecutionPolicy" in arg or "Command" in arg for arg in lease._argv)
    finally:
        await lease.aclose()
        await computer.aclose()


@pytest.mark.parametrize(
    "platform,backend", [("linux", "atspi"), ("darwin", "ax"), ("other", "unavailable")]
)
def test_auto_dispatch_keeps_existing_platforms_and_configuration_schema(
    monkeypatch: pytest.MonkeyPatch, platform: str, backend: str
) -> None:
    monkeypatch.setattr(computer_module, "sys", SimpleNamespace(platform=platform))
    settings = Settings()
    computer = Computer(settings)
    assert computer.backend == backend
    assert settings.computer.backend == "auto"
    assert Settings.model_validate_json(settings.model_dump_json()).computer.backend == "auto"
    with pytest.raises(ValueError):
        Settings.model_validate({"computer": {"backend": "uia"}})
