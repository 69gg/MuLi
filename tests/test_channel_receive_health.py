from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import profile, unused

import muli.channels.manager as manager_module
from muli.channels import ChannelError, ChannelManager, ChannelsConfig, IncomingMessage
from muli.channels.adapters import Adapter
from muli.channels.types import PollBatch
from muli.events import EventBus
from muli.store import Store


class ControlledLoop:
    """Hold retry/empty-batch delays without replacing global asyncio.sleep."""

    def __init__(self) -> None:
        self.sleeps: asyncio.Queue[tuple[float, asyncio.Event]] = asyncio.Queue()

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, seconds: float) -> None:
        if seconds == 0:
            await asyncio.sleep(0)
            return
        release = asyncio.Event()
        self.sleeps.put_nowait((seconds, release))
        await release.wait()

    async def checkpoint(self, seconds: float) -> asyncio.Event:
        delay, release = await asyncio.wait_for(self.sleeps.get(), 2)
        assert delay == seconds
        return release


class ControlledAdapter(Adapter):
    def __init__(self, manager: ChannelManager, name: str) -> None:
        super().__init__(manager.config.profile(name), manager.client)
        self.unavailable = False
        self.calls: asyncio.Queue[tuple[str, asyncio.Future[PollBatch]]] = asyncio.Queue()

    def ready(self) -> None:
        if self.unavailable:
            raise ChannelError("PRIVATE ready credentials")

    async def poll(self, cursor: str) -> PollBatch:
        result: asyncio.Future[PollBatch] = asyncio.get_running_loop().create_future()
        self.calls.put_nowait((cursor, result))
        return await result

    async def pending(self, expected_cursor: str = "") -> asyncio.Future[PollBatch]:
        cursor, result = await asyncio.wait_for(self.calls.get(), 2)
        assert cursor == expected_cursor
        return result


class HealthHarness:
    def __init__(
        self,
        path: Path,
        name: str,
        *,
        enabled: bool = True,
        callback_raises: bool = False,
    ) -> None:
        self.store = Store(path / "channels.db")
        self.bus = EventBus(self.store)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(unused))
        self.faults: list[tuple[str, str]] = []
        self.submit_error = False

        def fault(code: str, occurrence: str) -> None:
            self.faults.append((code, occurrence))
            if callback_raises:
                raise RuntimeError("PRIVATE notification failure")

        async def submit(**kwargs: Any) -> dict[str, Any]:
            if self.submit_error:
                raise ValueError("PRIVATE owner input storage failure")
            return {"id": kwargs["event_id"], "state": "pending"}

        config = ChannelsConfig(
            **{name: profile(enabled=enabled, account_id="test-account", user_id="test-user")},
            retry_backoff_seconds=2,
        )
        self.manager = ChannelManager(
            self.store,
            self.bus,
            config,
            submit,
            client=self.client,
            on_receive_fault=fault,
        )
        self.adapter = ControlledAdapter(self.manager, name)
        self.manager.adapters[name] = self.adapter
        self.name = name

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = self.manager.status()["channels"][self.name]["receive_health"]
        return result

    def updates(self) -> list[dict[str, Any]]:
        return [event for event in self.store.events() if event["type"] == "channel.updated"]

    def cursor(self) -> str:
        return self.manager._state("poll:" + self.name + ":" + self.manager._account(self.name))

    async def close(self) -> None:
        await self.manager.close()
        await self.client.aclose()
        self.store.close()


@pytest.mark.parametrize("name", ["telegram", "weixin"])
async def test_repeated_receive_failure_recovers_only_after_a_committed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    clock = ControlledLoop()
    monkeypatch.setattr(manager_module, "asyncio", clock)
    harness = HealthHarness(tmp_path, name, callback_raises=True)
    try:
        await harness.manager.start()
        pending = await harness.adapter.pending()
        await harness.manager.start()
        assert harness.health() == {"state": "idle", "code": None, "occurrence_id": None}
        pending.set_exception(ChannelError("PRIVATE first receive failure"))
        first = await clock.checkpoint(2)
        failed = harness.health()
        assert failed["state"] == "failed" and failed["code"] == name + "_receive_failed"
        assert harness.faults == [(failed["code"], failed["occurrence_id"])]
        assert len(failed["occurrence_id"]) == 32
        assert set(failed["occurrence_id"]) <= set("0123456789abcdef")
        assert "PRIVATE" not in json.dumps([failed, harness.faults])
        assert len(harness.updates()) == 1 and harness.cursor() == ""
        failed["state"] = "caller-mutated"
        assert harness.health()["state"] == "failed"

        first.set()
        pending = await harness.adapter.pending()
        pending.set_exception(ChannelError("PRIVATE first receive failure"))
        repeated = await clock.checkpoint(2)
        assert len(harness.updates()) == 1 and len(harness.faults) == 1

        repeated.set()
        pending = await harness.adapter.pending()
        pending.set_exception(ChannelError("PRIVATE changed receive failure"))
        changed = await clock.checkpoint(2)
        assert len(harness.faults) == 1
        assert harness.health()["occurrence_id"] == harness.faults[0][1]
        assert harness.manager.status()["channels"][name]["last_error"].endswith(
            "changed receive failure"
        )
        before_recovery = len(harness.updates())

        changed.set()
        pending = await harness.adapter.pending()
        pending.set_result(PollBatch((), "committed-cursor"))
        recovered = await clock.checkpoint(1)
        assert harness.cursor() == "committed-cursor"
        assert harness.health() == {"state": "ready", "code": None, "occurrence_id": None}
        assert harness.manager.status()["channels"][name]["last_error"] == ""
        assert len(harness.updates()) == before_recovery + 1

        recovered.set()
        pending = await harness.adapter.pending("committed-cursor")
        pending.set_exception(ChannelError("PRIVATE next failure"))
        await clock.checkpoint(2)
        assert len(harness.faults) == 2 and harness.faults[0][1] != harness.faults[1][1]
        assert harness.health()["occurrence_id"] == harness.faults[1][1]
        assert harness.cursor() == "committed-cursor"
    finally:
        await harness.close()


@pytest.mark.parametrize("name", ["telegram", "weixin"])
async def test_unavailable_start_is_one_fault_and_never_polls(tmp_path: Path, name: str) -> None:
    harness = HealthHarness(tmp_path, name)
    harness.adapter.unavailable = True
    try:
        for _ in range(3):
            await harness.manager.start()
        assert harness.adapter.calls.empty()
        assert len(harness.faults) == 1 and harness.faults[0][0] == name + "_receive_unavailable"
        assert len(harness.updates()) == 1
        assert harness.updates()[0]["data"]["channels"][name]["receive_health"] == harness.health()
        assert harness.health() == {
            "state": "failed",
            "code": name + "_receive_unavailable",
            "occurrence_id": harness.faults[0][1],
        }
        for other in ("onebot", "qq"):
            assert "receive_health" not in harness.manager.status()["channels"][other]
        await harness.manager.close()
        assert len(harness.faults) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize("name", ["telegram", "weixin"])
@pytest.mark.parametrize("ending", ["close", "cancel"])
async def test_pending_poll_close_and_cancellation_are_silent(
    tmp_path: Path, name: str, ending: str
) -> None:
    harness = HealthHarness(tmp_path, name)
    try:
        await harness.manager.start()
        pending = await harness.adapter.pending()
        if ending == "close":
            await harness.manager.close()
        else:
            task = next(
                task for task in harness.manager._tasks if task.get_name() == "muli-channel-" + name
            )
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()
        assert pending.cancelled() and harness.faults == []
        assert harness.health() == {"state": "idle", "code": None, "occurrence_id": None}
        assert harness.updates() == []
    finally:
        await harness.close()


@pytest.mark.parametrize("name", ["telegram", "weixin"])
@pytest.mark.parametrize("stage", ["submit", "cursor"])
async def test_received_batch_is_not_healthy_until_local_processing_and_cursor_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, stage: str
) -> None:
    clock = ControlledLoop()
    monkeypatch.setattr(manager_module, "asyncio", clock)
    harness = HealthHarness(tmp_path, name)
    try:
        await harness.manager.start()
        pending = await harness.adapter.pending()
        original: Callable[[str, str], None] = harness.manager._set_state
        if stage == "submit":
            harness.submit_error = True
            batch = PollBatch((IncomingMessage("owner-message", "7", "7", "主人输入"),), "next")
        else:

            def fail_cursor(key: str, value: str) -> None:
                if key.startswith("poll:"):
                    raise ValueError("PRIVATE cursor storage failure")
                original(key, value)

            monkeypatch.setattr(harness.manager, "_set_state", fail_cursor)
            batch = PollBatch((), "next")
        pending.set_result(batch)
        retry = await clock.checkpoint(2)
        assert harness.health()["state"] == "failed" and harness.cursor() == ""
        assert len(harness.faults) == 1 and harness.faults[0][0] == name + "_receive_failed"
        harness.submit_error = False
        if stage == "cursor":
            monkeypatch.setattr(harness.manager, "_set_state", original)
        retry.set()
        pending = await harness.adapter.pending()
        pending.set_result(batch)
        if batch.messages:
            await harness.adapter.pending("next")
        else:
            await clock.checkpoint(1)
        assert harness.health() == {"state": "ready", "code": None, "occurrence_id": None}
        assert harness.cursor() == "next" and len(harness.faults) == 1
    finally:
        await harness.close()


async def test_disabled_polling_channels_stay_idle_and_never_call_adapter(tmp_path: Path) -> None:
    harness = HealthHarness(tmp_path, "telegram", enabled=False)
    try:
        await harness.manager.start()
        assert harness.adapter.calls.empty() and harness.faults == []
        for name in ("telegram", "weixin"):
            assert harness.manager.status()["channels"][name]["receive_health"] == {
                "state": "idle",
                "code": None,
                "occurrence_id": None,
            }
        assert harness.updates() == []
    finally:
        await harness.close()


@pytest.mark.parametrize("name", ["telegram", "weixin"])
@pytest.mark.parametrize("ending", ["close", "cancel"])
@pytest.mark.parametrize("outcome", ["failed", "success"])
async def test_adapter_suppressing_cancellation_cannot_publish_health_or_advance_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, ending: str, outcome: str
) -> None:
    harness = HealthHarness(tmp_path, name)
    entered = asyncio.Event()

    async def late(cursor: str) -> PollBatch:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if outcome == "failed":
                raise ChannelError("PRIVATE retired receiver error") from None
            return PollBatch((), "retired-cursor")
        raise AssertionError("only explicit cancellation can settle this adapter")

    monkeypatch.setattr(harness.adapter, "poll", late)
    try:
        await harness.manager.start()
        await asyncio.wait_for(entered.wait(), 2)
        if ending == "close":
            await harness.manager.close()
        else:
            task = next(
                task for task in harness.manager._tasks if task.get_name() == "muli-channel-" + name
            )
            task.cancel()
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert harness.faults == [] and harness.updates() == []
        assert harness.cursor() == ""
        assert harness.health() == {"state": "idle", "code": None, "occurrence_id": None}
        assert harness.manager.status()["channels"][name]["last_error"] == ""
    finally:
        await harness.close()
