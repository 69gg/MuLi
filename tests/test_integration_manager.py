from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from muli.config import ConfigFile
from muli.events import EventBus
from muli.integrations.chiliarch import (
    ChiliarchBridge,
    ChiliarchConfig,
    ChiliarchProtocolError,
    DeliveryReceipt,
    Envelope,
    PairingResult,
)
from muli.integrations.manager import IntegrationManager
from muli.permissions import Mode
from muli.store import Store, new_id
from muli.tools.files import Files

if TYPE_CHECKING:
    from muli.runtime import Runtime


class FakeRuntime:
    def __init__(self, directory: Path) -> None:
        self.config = ConfigFile(directory / "config.json")
        self.config.update(
            {
                "workspace_roots": [str(directory)],
                "default_mode": "ask",
                "chiliarch": {
                    "enabled": True,
                    "base_url": "https://chiliarch.example",
                    "agent_id": "agent-1",
                    "credential": "secret",
                },
            }
        )
        self.store = Store(directory / "state.db")
        self.bus = EventBus(self.store)
        self.files = Files(self.config.settings, directory / "data")
        self.tasks: dict[str, asyncio.Task[str]] = {}
        self.runs: dict[str, asyncio.Task[str]] = {}
        self.started: list[tuple[str, str, Mode | None]] = []
        self.release = asyncio.Event()
        self.block = False

    def new_session(self, title: str = "新会话", mode: Mode | None = None) -> dict[str, Any]:
        return self.store.put("session", {"id": new_id(), "title": title, "mode": mode})

    async def start(
        self, session_id: str, content: str, mode: Mode | None = None
    ) -> dict[str, Any]:
        self.started.append((session_id, content, mode))

        async def run() -> str:
            if self.block:
                await self.release.wait()
            return "verified result"

        self.tasks[session_id] = asyncio.create_task(run())
        run_id = new_id()
        self.runs[run_id] = self.tasks[session_id]
        return {"session_id": session_id, "run_id": run_id}

    async def wait(self, run_id: str) -> str:
        return await self.runs[run_id]

    async def stop(self, session_id: str) -> None:
        task = self.tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


class FakeBridge(ChiliarchBridge):
    def __init__(
        self, config: ChiliarchConfig, callback: Callable[[Envelope], Awaitable[None]]
    ) -> None:
        super().__init__(
            replace(config, reconnect_initial_seconds=0.001, reconnect_max_seconds=0.002), callback
        )
        self.sent: list[Envelope] = []
        self.fail_next_send = False

    async def run(self) -> None:
        self.status = "connected"
        await self._stop_event.wait()
        self.status = "stopped"

    async def send(self, envelope: Envelope) -> DeliveryReceipt:
        self.sent.append(envelope)
        if self.fail_next_send:
            self.fail_next_send = False
            raise httpx.ConnectError("disconnected")
        return DeliveryReceipt(Envelope(type="ack", payload={"message_id": envelope.id}), False)


def setup(directory: Path) -> tuple[FakeRuntime, IntegrationManager]:
    runtime = FakeRuntime(directory)
    return runtime, IntegrationManager(cast("Runtime", runtime), bridge_factory=FakeBridge)


async def until(predicate: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 1)


def dispatch(identifier: str, **payload: Any) -> Envelope:
    return Envelope(
        type="task.dispatch",
        payload={"task_id": identifier, "instruction": "整理一条资料", "parameters": {}, **payload},
    )


@pytest.mark.parametrize("introduction", ["", "  ", "我是沐璃，负责整理资料。"])
def test_introduction_uses_persona_without_model_calls(tmp_path: Path, introduction: str) -> None:
    runtime, manager = setup(tmp_path)
    try:
        runtime.config.update(
            {"persona": {"name": "小璃", "user_name": "小明", "introduction": introduction}}
        )
        advertised = manager._config().introduction
        if introduction.strip():
            assert advertised == introduction
        else:
            assert "小璃" in advertised and "小明的私人数字助手" in advertised
        assert "secret" not in advertised and runtime.started == []
    finally:
        runtime.store.close()


def test_dispatch_runs_once_and_reports_ordered_states_with_local_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        await manager.start()
        try:
            first = dispatch("task-1", parameters={"mode": "yolo"})
            await manager._receive(first)
            await manager._receive(first)
            await until(lambda: manager._task("task-1")["state"] == "completed")  # type: ignore[index]
            await manager._receive(dispatch("task-1", parameters={"mode": "yolo"}))
            await until(lambda: manager.status()["outbox"] == 0)
            assert len(runtime.started) == 1
            assert runtime.started[0][2] == "ask"
            bridge = cast(FakeBridge, manager.bridge)
            assert [event.payload["status"] for event in bridge.sent] == [
                "accepted",
                "running",
                "completed",
            ]
            assert bridge.sent[-1].payload["result"]["content"] == "verified result"
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())


def test_conflicting_task_rolls_back_message_and_notification(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        runtime.block = True
        await manager.start()
        try:
            await manager._receive(dispatch("task-1"))
            try:
                await manager._receive(dispatch("task-1", instruction="different work"))
            except ValueError:
                pass
            else:
                raise AssertionError("task identity conflict was not rejected")
            assert runtime.store.db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 1
            assert len(runtime.store.list("notification")) == 1
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())


def test_cancel_before_dispatch_prevents_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        await manager.start()
        try:
            await manager._receive(
                Envelope(type="task.cancel", payload={"task_id": "task-1", "reason": "stop"})
            )
            await manager._receive(dispatch("task-1"))
            await until(lambda: manager.status()["outbox"] == 0)
            assert not runtime.started
            assert manager._task("task-1")["state"] == "cancelled"  # type: ignore[index]
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())


def test_running_cancel_reaches_runtime_before_terminal_receipt(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        runtime.block = True
        await manager.start()
        try:
            await manager._receive(dispatch("task-1"))
            await until(lambda: bool(runtime.started))
            session_id = runtime.started[0][0]
            await manager._receive(
                Envelope(type="task.cancel", payload={"task_id": "task-1", "reason": "stop"})
            )
            await until(lambda: manager._task("task-1")["state"] == "cancelled")  # type: ignore[index]
            assert runtime.tasks[session_id].cancelled()
            await until(lambda: manager.status()["outbox"] == 0)
            bridge = cast(FakeBridge, manager.bridge)
            assert bridge.sent[-1].payload["status"] == "cancelled"
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())


async def test_repeated_cancel_during_runtime_cleanup_still_commits_terminal_state(
    tmp_path: Path,
) -> None:
    runtime, manager = setup(tmp_path)
    runtime.block = True
    stopping, release = asyncio.Event(), asyncio.Event()
    original_stop = runtime.stop

    async def delayed_stop(session_id: str) -> None:
        stopping.set()
        await release.wait()
        await original_stop(session_id)

    runtime.stop = delayed_stop  # type: ignore[method-assign]
    await manager.start()
    try:
        await manager._receive(dispatch("task-1"))
        await until(lambda: bool(runtime.started))
        await manager._receive(Envelope(type="task.cancel", payload={"task_id": "task-1"}))
        await asyncio.wait_for(stopping.wait(), 1)
        await manager._receive(Envelope(type="task.cancel", payload={"task_id": "task-1"}))
        release.set()
        await until(lambda: manager._task("task-1")["state"] == "cancelled")  # type: ignore[index]
        await until(lambda: manager.status()["running"] == 0)
        assert runtime.tasks[runtime.started[0][0]].cancelled()
    finally:
        release.set()
        await manager.close()
        runtime.store.close()


async def test_pair_failure_is_safe_and_restarts_existing_identity(tmp_path: Path) -> None:
    runtime, manager = setup(tmp_path)

    class RejectedPairBridge(FakeBridge):
        async def pair(self, code: str) -> PairingResult:
            raise ChiliarchProtocolError("internal-secret-error")

    await manager.start()
    manager.bridge_factory = RejectedPairBridge
    try:
        with pytest.raises(ValueError, match="未确认成功") as error:
            await manager.pair("rejected-code")
        assert "internal-secret-error" not in str(error.value)
        assert runtime.config.settings.chiliarch.agent_id == "agent-1"
        assert runtime.config.settings.chiliarch.credential == "secret"
        await until(lambda: manager.status()["connected"])
    finally:
        await manager.close()
        runtime.store.close()


def test_outbox_retries_the_same_persisted_envelope(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        await manager.start()
        try:
            bridge = cast(FakeBridge, manager.bridge)
            bridge.fail_next_send = True
            await manager._receive(
                Envelope(
                    type="repository.command",
                    payload={"command_id": "command-1", "action": "not-supported", "arguments": {}},
                )
            )
            await until(lambda: manager.status()["outbox"] == 0)
            assert len(bridge.sent) == 2
            assert bridge.sent[0].to_dict() == bridge.sent[1].to_dict()
            assert bridge.sent[-1].payload["status"] == "failed"
            assert not runtime.started
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())


def test_restart_marks_running_interrupted_without_reexecution(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        with runtime.store.db:
            manager._put(
                runtime.store.db,
                "chiliarch_task",
                {
                    "id": "task-1",
                    "state": "running",
                    "payload": dispatch("task-1").payload,
                    "cancel_requested": False,
                },
            )
        await manager.start()
        try:
            await until(lambda: manager.status()["outbox"] == 0)
            assert manager._task("task-1")["state"] == "interrupted"  # type: ignore[index]
            assert not runtime.started
            assert cast(FakeBridge, manager.bridge).sent[0].payload["status"] == "interrupted"
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())


def test_old_identity_tasks_and_receipts_are_isolated_after_repairing(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        envelope = dispatch("task-1")
        runtime.store.ingest(
            envelope.to_dict(),
            namespace=manager._scope,
            apply=lambda db: manager._dispatch(db, envelope),
        )
        runtime.config.update({"chiliarch": {"agent_id": "agent-2", "credential": "new-secret"}})
        await manager.start()
        try:
            await asyncio.sleep(0.01)
            assert not runtime.started
            assert not cast(FakeBridge, manager.bridge).sent
            assert manager.status()["outbox"] == manager.status()["queued"] == 0
            assert (
                runtime.store.db.execute(
                    "SELECT count(*) FROM objects WHERE kind='chiliarch_outbox'"
                ).fetchone()[0]
                == 1
            )
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())


def test_peer_wake_is_notification_only_and_expired_or_outside_tasks_fail(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime, manager = setup(tmp_path)
        await manager.start()
        try:
            await manager._receive(
                Envelope(
                    type="collaboration.message",
                    payload={"message_id": "peer-1", "message": "do work", "wake": True},
                )
            )
            expired = replace(dispatch("expired"), expires_at="2000-01-01T00:00:00Z")
            await manager._receive(expired)
            await manager._receive(dispatch("outside", workdir=str(tmp_path.parent)))
            await until(
                lambda: all(
                    manager._task(identifier)["state"] == "failed"
                    for identifier in ("expired", "outside")
                )
            )  # type: ignore[index]
            assert not runtime.started
            assert len(runtime.store.list("notification")) == 3
        finally:
            await manager.close()
            runtime.store.close()

    asyncio.run(scenario())
