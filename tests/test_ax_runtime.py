from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_ax_computer import ACCEPTED, FakeLease, action, frame
from test_runtime import make_runtime

import muli.tools.ax_computer as ax_module
from muli.providers import ModelTurn, ToolCall
from muli.runtime import Runtime


async def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    monkeypatch.setattr(ax_module.sys, "platform", "darwin")
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"computer": {"enabled": True, "backend": "ax"}})
    await runtime.reconfigure()
    return runtime


async def test_runtime_binds_real_session_run_and_rechecks_pending_input_at_native_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await configured(tmp_path, monkeypatch)
    lease = FakeLease([frame(), ACCEPTED])
    monkeypatch.setattr(runtime.computer._ax, "_new_lease", lambda **kwargs: lease)
    session = runtime.new_session(mode="yolo")["id"]
    runtime.run_ids[session] = "run-one"
    try:
        snapshot = await runtime._tool(
            ToolCall("read", "computer_snapshot", "{}"), runtime.tools, "yolo", session, "run-one"
        )
        assert runtime.computer._ax._lease_owner == (session, "run-one")
        assert runtime._tool_execution.get() is None
        lease.gate, lease.entered = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(
            runtime._tool(
                ToolCall("act", "computer_action", json.dumps(action(snapshot))),
                runtime.tools,
                "yolo",
                session,
                "run-one",
            )
        )
        await lease.entered.wait()
        # An input arriving after the ordinary Runtime guard but before IPC write.
        monkeypatch.setattr(runtime.store, "pending_inputs", lambda identifier: [{}])
        lease.gate.set()
        result = await pending
        assert result["code"] == "input_pending" and result["outcome"] == "not_started"
        assert len(lease.calls) == 1 and lease.closed
        assert runtime._tool_execution.get() is None
    finally:
        runtime.run_ids.clear()
        await runtime.close()


async def test_new_run_cannot_use_predecessor_reference_and_old_release_preserves_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await configured(tmp_path, monkeypatch)
    lease = FakeLease([frame()])
    monkeypatch.setattr(runtime.computer._ax, "_new_lease", lambda **kwargs: lease)
    session = runtime.new_session(mode="yolo")["id"]
    runtime.run_ids[session] = "old"
    try:
        snapshot = await runtime._tool(
            ToolCall("read", "computer_snapshot", "{}"), runtime.tools, "yolo", session, "old"
        )
        runtime.run_ids[session] = "new"
        result = await runtime._tool(
            ToolCall("stale", "computer_action", json.dumps(action(snapshot))),
            runtime.tools,
            "yolo",
            session,
            "new",
        )
        assert result["code"] == "desktop_busy" and not lease.closed
        await runtime.computer.release_execution(session, "old")
        second = FakeLease([frame()])
        monkeypatch.setattr(runtime.computer._ax, "_new_lease", lambda **kwargs: second)
        await runtime._tool(
            ToolCall("new-read", "computer_snapshot", "{}"), runtime.tools, "yolo", session, "new"
        )
        await runtime.computer.release_execution(session, "old")
        assert not second.closed
    finally:
        runtime.run_ids.clear()
        await runtime.close()


async def test_reconfigure_waits_for_old_computer_cleanup_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await configured(tmp_path, monkeypatch)
    old = runtime.computer
    entered, release = asyncio.Event(), asyncio.Event()
    original = old.aclose

    async def close() -> None:
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(old, "aclose", close)
    pending = asyncio.create_task(runtime.reconfigure())
    await entered.wait()
    assert runtime.computer is old
    release.set()
    await pending
    assert runtime.computer is not old and old._ax._closed
    await runtime.close()


@pytest.mark.parametrize("failure", [False, True])
async def test_driver_persists_terminal_state_even_if_native_cleanup_is_cancelled_or_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="已完成")])
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def cleanup(session_id: str, run_id: str) -> None:
        calls.append((session_id, run_id))
        entered.set()
        await release.wait()
        if failure:
            raise OSError("fake cleanup error")

    monkeypatch.setattr(runtime.computer, "release_execution", cleanup)
    session = runtime.new_session(mode="yolo")["id"]
    started = await runtime.start(session, "完成任务")
    task = runtime.tasks[session]
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = runtime.store.get("run", started["run_id"])
    assert run["status"] == "completed" and run["result"] == "已完成" and run["finished_at"]
    assert ("cleanup_error" in run) == failure
    assert calls == [(session, started["run_id"])]
    assert session not in runtime.tasks and session not in runtime.run_ids
    assert runtime.store.get("session", session)["status"] == "idle"
    await runtime.close()
