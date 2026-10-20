"""Real AX lease + host/Runtime, plain JSONL child; no native desktop API."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_ax_computer import action
from test_runtime import make_runtime

import muli.tools.ax_computer as ax_module
from muli.config import Settings
from muli.providers import ToolCall
from muli.tools.ax_lease import AXHelperLease
from muli.tools.computer import Computer, ComputerError

HELPER = r"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any

def node(ref: str, *, kind: str = "node", parent: str | None = None) -> dict[str, Any]:
    return {"ref_id": ref, "parent_ref_id": parent, "depth": 1 if parent else 0,
            "parent_observed": True, "kind": kind, "name": "确认", "role": "AXButton",
            "subrole": None, "pid": 42, "actions": ["AXPress"], "editable": False,
            "enabled": True, "protected": None}

for line in sys.stdin:
    request = json.loads(line)
    command, payload = request["command"], request["payload"]
    with Path(sys.argv[1]).open("a", encoding="utf-8") as output:
        output.write(command + "\n")
    if command == "action":
        if sys.argv[2] == "lost":
            os._exit(0)
        result = {"ok": True, "outcome": "accepted", "effect_verified": False}
    else:
        if command == "targets":
            rows = ([node("app-2", kind="application"), node("window", kind="window", parent="app-2")]
                    if payload else [node("app-1", kind="application")])
            scope = "application_windows" if payload else "foreground_application"
        else:
            rows = [node("button")]
            scope = "window" if payload else "foreground_application"
        result = {"targets" if command == "targets" else "nodes": rows,
                  "scope": scope, "complete": True, "truncated": False,
                  "visited": len(rows), "omissions": []}
    print(json.dumps({"v": 1, "request_id": request["request_id"], "result": result}), flush=True)
"""


def install_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, computer: Computer, *, mode: str = "ok"
) -> tuple[list[AXHelperLease], Path]:
    script, log = tmp_path / "fake_ax_child.py", tmp_path / "commands.txt"
    script.write_text(HELPER, encoding="utf-8")
    leases: list[AXHelperLease] = []

    def create(**kwargs: Any) -> AXHelperLease:
        lease = AXHelperLease(
            [sys.executable, str(script), str(log), mode],
            timeout=2,
            ttl=10,
            max_nodes=100,
            once=kwargs.get("once", False),
        )
        leases.append(lease)
        return lease

    monkeypatch.setattr(computer._ax, "_new_lease", create)
    return leases, log


async def test_real_transport_preserves_app_window_snapshot_and_single_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ax_module, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)
    )
    computer = Computer(
        Settings.model_validate({"computer": {"enabled": True, "backend": "ax"}}),
        owner=lambda: ("owner", "run"),
    )
    leases, log = install_child(tmp_path, monkeypatch, computer)
    try:
        apps = await computer.targets({})
        windows = await computer.targets({"application_id": apps["targets"][0]["application_id"]})
        snapshot = await computer.snapshot({"target_id": windows["targets"][1]["target_id"]})
        result = await computer.action(action(snapshot))
        assert result == {"ok": True, "outcome": "accepted", "effect_verified": False}
        assert len(leases) == 1 and leases[0].closed
        assert leases[0]._process is not None and leases[0]._process.returncode is not None
        assert log.read_text(encoding="utf-8").splitlines() == [
            "targets",
            "targets",
            "snapshot",
            "action",
        ]
        assert not computer._ax._snapshot_id and not computer._ax._targets
    finally:
        await computer.aclose()


async def test_real_transport_lost_action_receipt_is_unknown_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ax_module, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)
    )
    computer = Computer(
        Settings.model_validate({"computer": {"enabled": True, "backend": "ax"}}),
        owner=lambda: ("owner", "run"),
    )
    leases, log = install_child(tmp_path, monkeypatch, computer, mode="lost")
    try:
        snapshot = await computer.snapshot({})
        with pytest.raises(ComputerError) as caught:
            await computer.action(action(snapshot))
        assert caught.value.code == "action_unknown_outcome" and caught.value.outcome == "unknown"
        assert leases[0].closed
        assert log.read_text(encoding="utf-8").splitlines() == ["snapshot", "action"]
    finally:
        await computer.aclose()


async def test_runtime_final_guard_after_real_lease_start_writes_no_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ax_module, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)
    )
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"computer": {"backend": "ax", "enabled": True}})
    await runtime.reconfigure()
    leases, log = install_child(tmp_path, monkeypatch, runtime.computer)
    session = runtime.new_session(mode="yolo")["id"]
    runtime.run_ids[session] = "run"
    entered, proceed = asyncio.Event(), asyncio.Event()
    pending: asyncio.Task[Any] | None = None
    try:
        snapshot = await runtime._tool(
            ToolCall("read", "computer_snapshot", "{}"), runtime.tools, "yolo", session, "run"
        )
        original_start = leases[0]._start

        async def gated_start() -> None:
            await original_start()
            entered.set()
            await proceed.wait()

        monkeypatch.setattr(leases[0], "_start", gated_start)
        pending = asyncio.create_task(
            runtime._tool(
                ToolCall("write", "computer_action", json.dumps(action(snapshot))),
                runtime.tools,
                "yolo",
                session,
                "run",
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        monkeypatch.setattr(runtime.store, "pending_inputs", lambda identifier: 1)
        proceed.set()
        result = await asyncio.wait_for(pending, 2)
        assert result["code"] == "input_pending" and result["outcome"] == "not_started"
        assert log.read_text(encoding="utf-8").splitlines() == ["snapshot"]
        assert leases[0].closed and runtime._tool_execution.get() is None
    finally:
        proceed.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        runtime.run_ids.clear()
        await runtime.close()
