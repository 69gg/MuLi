"""Invoke uses captured UIA references; these tests never launch a process."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_ax_computer import ACCEPTED, FakeLease
from test_runtime import make_runtime
from test_uia_computer import frame, no_process, record, setup

import muli.tools.computer as computer_module
import muli.tools.uia_computer as uia_module
from muli.providers import ToolCall
from muli.tools.ax_lease import AXLeaseError
from muli.tools.computer import Computer, ComputerError
from muli.tools.uia_computer import UIAComputer

__all__ = ["no_process"]


def button(ref: str = "button", **changes: Any) -> dict[str, Any]:
    return record(
        ref,
        **{
            "parent_ref_id": "root",
            "depth": 1,
            "kind": "node",
            "name": "Owned button",
            "role": "ControlType.Button",
            "actions": ["invoke"],
            "read_only": False,
            **changes,
        },
    )


async def observe(computer: Computer) -> dict[str, str]:
    discovered = await computer.targets({})
    snapshot = await computer.snapshot({"target_id": discovered["targets"][0]["target_id"]})
    node = next(item for item in snapshot["nodes"] if item["actions"] == ["invoke"])
    return {"snapshot_id": snapshot["snapshot_id"], "node_id": node["id"], "action": "invoke"}


async def test_captured_button_invokes_once_and_consumes_the_whole_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button()]), ACCEPTED]
    )
    try:
        arguments = await observe(computer)
        result = await computer.action(arguments)
        assert result == ACCEPTED
        assert lease.calls[-1] == ("action", {"ref_id": "button", "action": "invoke"})
        assert lease.closed and lease.closes == 1
        with pytest.raises(ComputerError) as stale:
            await computer.action(arguments)
        assert stale.value.code == "stale_snapshot" and stale.value.outcome == "not_started"
        assert len(lease.calls) == 3
    finally:
        await computer.aclose()


def test_native_lease_outcome_is_preserved_without_private_error_text() -> None:
    error = UIAComputer._error(
        AXLeaseError("action_unknown_outcome", "private-provider-value", "unknown")
    )
    assert error.code == "action_unknown_outcome" and error.outcome == "unknown"
    assert "private-provider-value" not in json.dumps(error.as_result())


@pytest.mark.parametrize(
    "change", [{"node_id": "invented"}, {"action": "press"}, {"text": "never type"}]
)
async def test_invalid_attempt_consumes_valid_snapshot_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, str]
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button()])]
    )
    try:
        original = await observe(computer)
        with pytest.raises(ComputerError) as rejected:
            await computer.action({**original, **change})
        assert rejected.value.code in {"stale_node", "unsupported_operation"}
        assert rejected.value.outcome == "not_started" and len(lease.calls) == 2
        with pytest.raises(ComputerError) as stale:
            await computer.action(original)
        assert stale.value.code == "stale_snapshot" and len(lease.calls) == 2
    finally:
        await computer.aclose()


async def test_observation_attempt_revokes_snapshot_before_waiting_for_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button()]), frame()]
    )
    try:
        original = await observe(computer)
        await computer._uia._lock.acquire()
        entered = asyncio.Event()

        async def discover_again() -> dict[str, Any]:
            entered.set()
            return await computer.targets({})

        pending = asyncio.create_task(discover_again())
        await entered.wait()
        try:
            with pytest.raises(ComputerError) as stale:
                await computer.action(original)
            assert stale.value.code == "stale_snapshot" and len(lease.calls) == 2
        finally:
            computer._uia._lock.release()
            await pending
    finally:
        await computer.aclose()


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        ("protected_element", "not_started"),
        ("element_disabled", "not_started"),
        ("target_changed", "not_started"),
        ("stale_node", "not_started"),
        ("action_unknown_outcome", "unknown"),
    ],
)
async def test_native_rejection_and_unknown_stay_distinct_and_never_retry(
    monkeypatch: pytest.MonkeyPatch, code: str, outcome: str
) -> None:
    computer, lease, _ = setup(
        monkeypatch,
        [
            frame(),
            frame("snapshot", [record("root"), button()]),
            AXLeaseError(code, "private-provider-value", outcome),
        ],
    )
    try:
        arguments = await observe(computer)
        with pytest.raises(ComputerError) as rejected:
            await computer.action(arguments)
        assert rejected.value.code == code and rejected.value.outcome == outcome
        assert "private-provider-value" not in json.dumps(rejected.value.as_result())
        assert len(lease.calls) == 3 and lease.closed
    finally:
        await computer.aclose()


@pytest.mark.parametrize(
    "change",
    [
        {"role": "ControlType.Edit"},
        {"protected": True, "name": ""},
        {"enabled": False},
        {"editable": True},
        {"read_only": True},
        {"actions": ["Invoke"]},
    ],
)
async def test_inconsistent_helper_capability_is_rejected_without_publishing_credentials(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button(**change)])]
    )
    try:
        discovered = await computer.targets({})
        with pytest.raises(ComputerError) as invalid:
            await computer.snapshot({"target_id": discovered["targets"][0]["target_id"]})
        assert invalid.value.code == "protocol_error"
        assert not computer._uia._nodes and computer._uia._snapshot_id is None and lease.closed
    finally:
        await computer.aclose()


async def test_output_budget_retains_only_nodes_the_caller_actually_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [record("root"), *[button(str(i), name="x" * 500) for i in range(8)]]
    computer, lease, _ = setup(monkeypatch, [frame(), frame("snapshot", records)], budget=2048)
    try:
        discovered = await computer.targets({})
        snapshot = await computer.snapshot({"target_id": discovered["targets"][0]["target_id"]})
        assert snapshot["truncated"] and 0 < len(snapshot["nodes"]) < len(records)
        assert len(json.dumps(snapshot, ensure_ascii=False)) <= 2048
        assert set(computer._uia._nodes) == {item["id"] for item in snapshot["nodes"]}
        assert len(lease.calls) == 2
    finally:
        await computer.aclose()


async def test_two_attempts_cannot_dispatch_from_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button()]), ACCEPTED]
    )
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        original = await observe(computer)
        lease.gate, lease.entered = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(computer.action(original))
        await lease.entered.wait()
        with pytest.raises(ComputerError) as stale:
            await computer.action(original)
        assert stale.value.code == "stale_snapshot" and len(lease.calls) == 2
        lease.gate.set()
        assert await pending == ACCEPTED and len(lease.calls) == 3
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await computer.aclose()


async def test_cancellation_after_dispatch_consumes_snapshot_and_reaps_owned_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button()])]
    )
    dispatched = asyncio.Event()
    waiting = asyncio.Event()
    request = lease.request

    async def pending_request(
        command: str,
        payload: dict[str, Any],
        *,
        deadline: float | None = None,
        before_send: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if command != "action":
            return await request(command, payload, deadline=deadline, before_send=before_send)
        assert before_send is not None
        before_send()
        lease.calls.append((command, payload))
        dispatched.set()
        await waiting.wait()
        raise AssertionError("A cancelled Invoke must not obtain a replacement receipt")

    monkeypatch.setattr(lease, "request", pending_request)
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        arguments = await observe(computer)
        pending = asyncio.create_task(computer.action(arguments))
        await dispatched.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert lease.closed and len(lease.calls) == 3
        assert computer._uia._snapshot_id is None and not computer._uia._nodes
        with pytest.raises(ComputerError) as stale:
            await computer.action(arguments)
        assert stale.value.code == "stale_snapshot" and len(lease.calls) == 3
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await computer.aclose()


@pytest.mark.parametrize("cause", ["owner", "expiry", "guard"])
async def test_last_send_gate_rejects_changes_after_lease_preparation(
    monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    denied = False

    def capture() -> Callable[[], None]:
        def guard() -> None:
            if denied:
                raise ComputerError("paused", "动作已暂停")

        return guard

    computer, lease, owner = setup(
        monkeypatch,
        [frame(), frame("snapshot", [record("root"), button()]), ACCEPTED],
        dispatch_guard=capture,
    )
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        original = await observe(computer)
        lease.gate, lease.entered = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(computer.action(original))
        await lease.entered.wait()
        if cause == "owner":
            owner[0] = ("main", "replacement-run")
        elif cause == "expiry":
            lease.expires_at = asyncio.get_running_loop().time()
        else:
            denied = True
        lease.gate.set()
        with pytest.raises(ComputerError) as rejected:
            await pending
        assert rejected.value.code == ("paused" if cause == "guard" else "stale_snapshot")
        assert rejected.value.outcome == "not_started" and len(lease.calls) == 2 and lease.closed
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await computer.aclose()


@pytest.mark.parametrize("response", [{}, {"ok": True, "outcome": "accepted"}])
async def test_bad_action_receipt_is_unknown_without_replay(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    computer, lease, _ = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button()]), response]
    )
    try:
        original = await observe(computer)
        with pytest.raises(ComputerError) as rejected:
            await computer.action(original)
        assert rejected.value.code == "action_unknown_outcome"
        assert rejected.value.outcome == "unknown" and len(lease.calls) == 3 and lease.closed
    finally:
        await computer.aclose()


@pytest.mark.parametrize(
    "response", [ACCEPTED, AXLeaseError("action_unknown_outcome", "x", "unknown")]
)
async def test_cleanup_failure_does_not_turn_accepted_or_unknown_into_retryable_error(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    computer, lease, owner = setup(
        monkeypatch, [frame(), frame("snapshot", [record("root"), button()]), response]
    )
    original_close = lease.aclose

    async def uncertain() -> None:
        raise OSError("private cleanup error")

    try:
        original = await observe(computer)
        monkeypatch.setattr(lease, "aclose", uncertain)
        if isinstance(response, AXLeaseError):
            with pytest.raises(ComputerError) as rejected:
                await computer.action(original)
            assert rejected.value.outcome == "unknown"
        else:
            assert await computer.action(original) == ACCEPTED
        assert computer._uia._lease is lease and computer._uia._cleanup_error
        owner[0] = ("other", "run")
        with pytest.raises(ComputerError) as occupied:
            await computer.targets({})
        assert occupied.value.code == "desktop_busy" and len(lease.calls) == 3
    finally:
        monkeypatch.setattr(lease, "aclose", original_close)
        await computer.aclose()


@pytest.mark.parametrize("cause", ["input", "closing"])
async def test_real_runtime_last_guard_blocks_a_prepared_invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    monkeypatch.setattr(computer_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(uia_module, "sys", SimpleNamespace(platform="win32"))
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"computer": {"enabled": True, "backend": "auto"}})
    await runtime.reconfigure()
    lease = FakeLease([frame(), frame("snapshot", [record("root"), button()]), ACCEPTED])
    monkeypatch.setattr(runtime.computer._uia, "_new_lease", lambda **kwargs: lease)
    session = runtime.new_session(mode="yolo")["id"]
    runtime.run_ids[session] = "run"
    pending: asyncio.Task[Any] | None = None

    async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return await runtime._tool(
            ToolCall(name, name, json.dumps(args)), runtime.tools, "yolo", session, "run"
        )

    try:
        discovered = await call("computer_targets", {})
        snapshot = await call(
            "computer_snapshot", {"target_id": discovered["targets"][0]["target_id"]}
        )
        arguments = {
            "snapshot_id": snapshot["snapshot_id"],
            "node_id": snapshot["nodes"][1]["id"],
            "action": "invoke",
        }
        lease.gate, lease.entered = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(call("computer_action", arguments))
        await lease.entered.wait()
        if cause == "input":
            monkeypatch.setattr(runtime.store, "pending_inputs", lambda identifier: [{}])
        else:
            runtime._closing = True
        lease.gate.set()
        rejected = await pending
        assert rejected["code"] == ("input_pending" if cause == "input" else "stale_snapshot")
        assert rejected["outcome"] == "not_started" and len(lease.calls) == 2 and lease.closed
        assert runtime._tool_execution.get() is None
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        runtime._closing = False
        runtime.run_ids.clear()
        await runtime.close()
