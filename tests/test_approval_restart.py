"""Old approvals never survive a restart or a different runtime instance."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NoReturn

import pytest

from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.runtime import Runtime
from muli.store import Store


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("审批恢复不能调用模型")


async def test_restart_expires_pending_and_interrupts_claimed_approvals(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    data = tmp_path / "data"
    store = Store.open_runtime(data / "muli.sqlite3", default_mode="ask")
    session = store.main_session("ask")
    sid = str(session["id"])
    store.put("run", {"id": "crashed-run", "session_id": sid, "status": "running"})
    records = {
        "pending": "pending",
        "queued": "queued",
        "running": "running",
        "approved": "approved",
        "rejected": "rejected",
    }
    for identifier, status in records.items():
        store.put(
            "approval",
            {
                "id": identifier,
                "session_id": sid,
                "run_id": "crashed-run",
                "status": status,
                "boot_epoch": "old-boot",
                "arguments": {"path": "keep"},
            },
        )
    before = store.event_position()
    store.close()

    runtime = Runtime(config, data, client_factory=no_model)
    try:
        pending = runtime.store.get("approval", "pending")
        assert pending is not None
        assert pending["status"] == "expired"
        assert pending["expired_reason"] == "服务重启，旧审批不能跨实例生效"
        assert pending["arguments"] == {"path": "keep"}
        for identifier in ("queued", "running"):
            item = runtime.store.get("approval", identifier)
            assert item is not None and item["status"] == "interrupted"
        assert runtime.store.get("approval", "approved")["status"] == "approved"  # type: ignore[index]
        assert runtime.store.get("approval", "rejected")["status"] == "rejected"  # type: ignore[index]

        recovered = runtime.store.events(after=before, session_id=sid)
        approval_events = [event for event in recovered if event["data"].get("kind") == "approval"]
        assert len(approval_events) == 3
        assert all(
            event["data"]["recovery_decision"] == "needs_owner" for event in approval_events
        )
        # The expired approval is no longer a pending blocker for the old run.
        run = runtime.store.get("run", "crashed-run")
        assert run is not None and run["status"] == "interrupted"
        assert run["recovery_decision"] == "resume_safe"
        assert not runtime.permissions.pending
    finally:
        await runtime.close()


async def test_resolve_now_rejects_an_approval_from_another_boot(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data", client_factory=no_model)
    try:
        sid = str(runtime.main_session()["id"])
        approval = {
            "id": "old-boot-approval",
            "session_id": sid,
            "run_id": "run",
            "status": "pending",
            "boot_epoch": "other-boot",
        }
        runtime.store.put("approval", approval)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        runtime.permissions.pending[approval["id"]] = future
        with pytest.raises(ValueError, match="旧实例"):
            runtime.permissions.resolve_now(approval["id"], "approve")
        assert future.done() is False
        assert runtime.store.get("approval", approval["id"])["status"] == "pending"  # type: ignore[index]

        current = {
            **approval,
            "id": "current-boot-approval",
            "boot_epoch": runtime.boot_epoch,
        }
        runtime.store.put("approval", current)
        current_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        runtime.permissions.pending[current["id"]] = current_future
        runtime.permissions.resolve_now(current["id"], "approve")
        assert current_future.result() is True
        assert runtime.store.get("approval", current["id"])["status"] == "approved"  # type: ignore[index]
        runtime.permissions.pending.pop(approval["id"])
    finally:
        await runtime.close()
