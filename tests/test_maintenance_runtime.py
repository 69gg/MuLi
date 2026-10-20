from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import muli.notifications as notification_module
from muli.config import ConfigFile, DesktopNotifications
from muli.events import EventBus
from muli.maintenance import Admission, BootPermit, MaintenanceError
from muli.management_transaction import (
    ClockReading,
    Deadline,
    ServiceIdentity,
    TransactionJournal,
    Transition,
)
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline
from muli.notifications import DesktopNotifier
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store import Store


class FakeClient:
    def __init__(self, calls: list[str], entered: asyncio.Event, finish: asyncio.Event) -> None:
        self.calls, self.entered, self.finish = calls, entered, finish

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append("stream")
        yield StreamEvent("done", turn=ModelTurn(content="已处理"))

    async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
        self.calls.append("complete")
        self.entered.set()
        await self.finish.wait()
        return ModelTurn(content="summary")


def configured(tmp_path: Path) -> ConfigFile:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "models": {"main": {"model": "fake", "base_url": "https://unused.invalid/v1"}},
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
        }
    )
    return config


async def test_paused_runtime_persists_owner_input_and_one_release_wakes_it(tmp_path: Path) -> None:
    calls: list[str] = []
    entered, finish = asyncio.Event(), asyncio.Event()

    def factory(config: ModelConfig) -> FakeClient:
        return FakeClient(calls, entered, finish)

    runtime = Runtime(configured(tmp_path), tmp_path / "data", client_factory=factory)  # type: ignore[arg-type]
    try:
        runtime.maintenance.admission.pause()
        receipt = await runtime.submit("保留待办", event_id="once")
        runtime._wake_main()
        assert receipt["queued"] and receipt["run_id"] is None
        assert not runtime.tasks and not calls
        session = runtime.main_session()["id"]
        assert runtime.store.pending_inputs(session) == 1
        runtime.maintenance._release()
        task = runtime.tasks[session]
        runtime.maintenance._release()
        assert runtime.tasks[session] is task
        await task
        assert calls == ["stream"]
        assert not runtime.store.pending_inputs(session)
    finally:
        await runtime.close()


async def test_direct_team_workflow_and_step_cannot_bypass_gate(tmp_path: Path) -> None:
    runtime = Runtime(configured(tmp_path), tmp_path / "data")
    try:
        session = runtime.new_session()
        workflow = runtime.workflows.create("test", [{"id": "one", "title": "one", "prompt": "do"}])
        runtime.maintenance.admission.pause()
        with pytest.raises(MaintenanceError, match="execution_paused"):
            await runtime.start(session["id"], "不写半条用户记录")
        with pytest.raises(MaintenanceError, match="execution_paused"):
            await runtime.run_step(session["id"], "不写工具批次", "worker", "ask")
        with pytest.raises(MaintenanceError, match="execution_paused"):
            await runtime.team.spawn("worker", "不启动")
        with pytest.raises(MaintenanceError, match="execution_paused"):
            await runtime.workflows.start(workflow["id"])
        assert not runtime.store.messages(session["id"])
        assert not runtime.tasks and not runtime.store.list("workflow_run")
        assert not runtime.store.list("agent")
        assert await runtime.submit_attention("保留事实", "attention", objects=[]) is None
        with pytest.raises(MaintenanceError, match="transaction_in_progress"):
            await runtime.reconfigure()
    finally:
        await runtime.close()


async def test_summary_and_finalizer_are_real_drain_owners(tmp_path: Path) -> None:
    calls: list[str] = []
    entered, finish = asyncio.Event(), asyncio.Event()

    def factory(config: ModelConfig) -> FakeClient:
        return FakeClient(calls, entered, finish)

    runtime = Runtime(configured(tmp_path), tmp_path / "data", client_factory=factory)  # type: ignore[arg-type]
    summary = asyncio.create_task(runtime._summarize("本地资料", 32))
    try:
        await entered.wait()
        assert not runtime.maintenance.admission.idle
        runtime.maintenance.admission.pause()
        finish.set()
        assert await summary == "summary"
        assert runtime.maintenance.admission.idle
        finalizer = asyncio.create_task(asyncio.Event().wait())
        runtime._session_finalizers.add(finalizer)
        assert not runtime.maintenance_idle()
        finalizer.cancel()
        await asyncio.gather(finalizer, return_exceptions=True)
        runtime._session_finalizers.remove(finalizer)
        assert runtime.maintenance_idle()
    finally:
        summary.cancel()
        await asyncio.gather(summary, return_exceptions=True)
        await runtime.close()


async def test_notification_pause_preserves_queue_and_tracks_actual_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notification_module,
        "notification_capability",
        lambda: {
            "supported": True,
            "available": True,
            "command": "/fake/notify-send",
        },
    )
    store = Store(tmp_path / "test.sqlite3")
    admission = Admission(allowed=False)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def send(argv: list[str], *, timeout: float, limit: int) -> dict[str, Any]:
        calls.append(argv[-2])
        entered.set()
        await finish.wait()
        return {"exit_code": 0}

    notifier = DesktopNotifier(
        store, EventBus(store), DesktopNotifications(enabled=True), runner=send, admission=admission
    )
    try:
        notifier._enqueue({"title": "尚未领取", "content": "不丢"})
        await notifier.start()
        await asyncio.sleep(0)
        assert notifier.status()["queued"] == 1 and not calls
        admission.release()
        await entered.wait()
        assert not admission.idle and notifier.status()["sending"]
        admission.pause()
        finish.set()
        await notifier._queue.join()
        assert admission.idle and calls == ["尚未领取"]
        notifier._enqueue({"title": "第二条", "content": "继续保留"})
        await asyncio.sleep(0)
        assert notifier.status()["queued"] == 1 and notifier.status()["dropped"] == 0
    finally:
        await notifier.close()
        store.close()


async def test_memory_job_is_not_claimed_while_paused_and_inflight_job_finishes(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "test.sqlite3")
    admission = Admission()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await finish.wait()
        return {"memories": []}

    pipeline = MemoryPipeline(Memory(store), extractor=extract, admission=admission)
    try:
        pipeline.ingest(
            {"id": "one", "role": "user", "content": "一", "source": "web"}, trusted_owner=True
        )
        await pipeline.start()
        await entered.wait()
        assert not admission.idle
        admission.pause()
        pipeline.ingest(
            {"id": "two", "role": "user", "content": "二", "source": "web"}, trusted_owner=True
        )
        finish.set()
        await pipeline.wait("one")
        await asyncio.sleep(0)
        job = pipeline.job("two")
        assert job and job["state"] == "queued" and job["attempts"] == 0
        assert admission.idle
        admission.release()
        result = await pipeline.wait("two")
        assert result and result["state"] == "done"
    finally:
        await pipeline.close()
        store.close()


async def test_passive_runtime_never_starts_external_components_and_holds_writer_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux boot lease")
    config = configured(tmp_path)
    data = tmp_path / "data"
    original = Runtime(config, data)
    identity = ServiceIdentity(
        installation="a" * 64,
        service="b" * 64,
        store_instance=original.store.instance_id,
        main_session=original.main_session()["id"],
        origin_boot="origin",
    )
    await original.close()
    with TransactionJournal(tmp_path / "transactions", data / "muli.sqlite3") as journal:
        with journal.writer_lease() as lease, journal.locked() as control:
            record = control.prepare(
                request_id="restart",
                action="restart",
                identity=identity,
                base=config.path.read_bytes(),
                target=config.path.read_bytes(),
                writer=lease,
                deadline=Deadline(
                    created=ClockReading(boot_id="kernel", wall=1000.0, monotonic=10.0),
                    seconds=60.0,
                    rollback_reserve=10.0,
                ),
            )
            for transition in (
                Transition(phase="draining"),
                Transition(phase="activating", intent="stop_base"),
                Transition(phase="activating", intent="install_target"),
                Transition(phase="activating", intent="start_target"),
                Transition(phase="verifying"),
            ):
                record = control.advance(record.position, transition)
        permit = BootPermit.acquire(
            journal, config.path, boot="passive", installation="a" * 64, service="b" * 64
        )
        runtime = Runtime(config, data, boot_permit=permit)
        calls: list[str] = []

        async def forbidden() -> None:
            calls.append("external")
            raise AssertionError("probation must remain local")

        monkeypatch.setattr(runtime, "_start_mcp", forbidden)
        monkeypatch.setattr(runtime.notifications, "start", forbidden)
        monkeypatch.setattr(runtime.reminders, "start", forbidden)
        try:
            await runtime.startup()
            receipt = await runtime.submit("被动时仍持久接收", event_id="during-boot")
            assert receipt["queued"] and not runtime.tasks
            assert not runtime.started and not calls
            with pytest.raises(Exception, match="lock_busy"):
                with journal.writer_lease():
                    pytest.fail("writer lease escaped")
        finally:
            await runtime.close()
        with journal.writer_lease():
            pass
