from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import attention_inputs, eventually
from test_proactive_delivery import configure, external
from test_report_origin import route
from test_runtime import FakeClient, make_runtime
from test_system_error_origin import source_for, start_capture
from test_system_events_runtime import inbox_for

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channel_health import CHANNEL_RECEIVE_FAILURE_CODES
from muli.channels import ChannelError
from muli.channels.types import PollBatch
from muli.providers import Message, ModelTurn, StreamEvent
from muli.store import new_id
from muli.system_event_query import read_system_events
from muli.system_events import SystemEvents, attention_source, system_log_context


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("channel health verification cannot access real network or processes")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


@pytest.mark.parametrize("name", ["telegram", "weixin"])
async def test_actual_receive_failure_waits_for_main_and_reaches_readonly_attention_once(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns = [ModelTurn(content="原工作完成。"), ModelTurn(content="手机入口接收暂时失败。")]
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update(
        {
            "channels": {
                name: {
                    "enabled": True,
                    "base_url": "https://platform.invalid",
                    "owner_ids": ["7"],
                    "token": "123:PRIVATE_TOKEN",
                    "account_id": "bot",
                    "user_id": "user",
                }
            }
        }
    )
    inbox = inbox_for(runtime)
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HeldClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if not seen:
                entered.set()
                await release.wait()
            async for event in super().stream(messages, tools):
                yield event

    async def failed(cursor: str) -> PollBatch:
        if not received.is_set():
            received.set()
            raise ChannelError("PRIVATE_REMOTE_DETAIL_AND_TOKEN", retryable=True)
        await asyncio.Event().wait()
        raise AssertionError("cancelled receive must not return")

    runtime.client_factory = lambda _: HeldClient(turns, seen)  # type: ignore[assignment]
    monkeypatch.setattr(application.manager.adapters[name], "poll", failed)
    try:
        await start_capture(runtime)
        run = await runtime.submit("先完成当前任务", mode="yolo")
        await eventually(entered.is_set)
        with system_log_context(run_id=new_id(), session_id=new_id()):
            await application.start()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        assert record["producer"] == "channel_receiver" and record["component"] == "channels"
        assert record["code"] == name + "_receive_failed"
        assert record["run_id"] is record["session_id"] is None
        assert source_for(runtime, record).classification == "independent"
        assert source_for(runtime, record).origin.delivery_routes() == []
        await inbox.start()
        await eventually(lambda: inbox._waiting == "main")
        assert seen == [] and attention_inputs(runtime) == []
        release.set()
        await runtime.wait(run["run_id"])
        await eventually(lambda: len(seen) == 2 and not runtime.tasks)
        assert len(attention_inputs(runtime)) == 1
        assert "PRIVATE" not in json.dumps([record, attention_inputs(runtime)])
        assert application.manager.status()["channels"][name]["receive_health"]["state"] == "failed"
        assert application.manager.deliveries() == []
        query = read_system_events(runtime.store, {"code": record["code"]}, max_output_chars=10000)
        assert len(query["items"]) == 1
    finally:
        release.set()
        await inbox.close()
        await application.close()
        await runtime.close()


async def test_current_configuration_and_manager_ownership_revoke_late_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"channels": {"telegram": {"enabled": True, "owner_ids": ["7"]}}})
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    old = application.manager
    callback = old.on_receive_fault
    assert callback is not None
    try:
        await start_capture(runtime)
        await application.start()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        assert record["code"] == "telegram_receive_unavailable"
        captured = runtime.system_events.status()["captured"]
        runtime.config.update({"channels": {"telegram": {"enabled": False}}})
        callback("telegram_receive_failed", new_id())
        assert runtime.system_events.status()["captured"] == captured
        await application.reconfigure()
        callback("telegram_receive_failed", new_id())
        assert runtime.system_events.status()["captured"] == captured
        current = application.manager.on_receive_fault
        assert current is not None
        await application.close()
        current("telegram_receive_failed", new_id())
        assert runtime.system_events.status()["captured"] == captured
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "_closing", True)
            callback("telegram_receive_failed", new_id())
            assert runtime.system_events.status()["captured"] == captured
    finally:
        await application.close()
        await runtime.close()


async def quiet_fault(events: SystemEvents, code: str, occurrence: str) -> None:
    events.channel_receive_failed(code, occurrence)


async def test_fixed_channel_facts_keep_suppression_and_original_independent_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config, "7")
    try:
        await start_capture(runtime)
        occurrence = new_id()
        with system_log_context(run_id=new_id(), session_id=new_id()):
            await asyncio.create_task(
                quiet_fault(runtime.system_events, "weixin_receive_failed", occurrence)
            )
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        assert record["run_id"] is record["session_id"] is None
        assert source_for(runtime, record).origin.delivery_routes() == [route("7")]
        runtime.config.update({"channels": {"proactive": {"default_destination": external("8")}}})
        with monkeypatch.context() as patch:
            patch.setattr("muli.system_events.time.time", lambda: record["first_at"] + 3601)
            runtime.system_events.channel_receive_failed("weixin_receive_failed", occurrence)
            await eventually(lambda: runtime.store.get("system_log", record["id"])["count"] == 2)
        assert source_for(
            runtime, runtime.store.get("system_log", record["id"])
        ).origin.delivery_routes() == [route("7")]
        with system_log_context(notify=False):
            await asyncio.create_task(
                quiet_fault(runtime.system_events, "telegram_receive_failed", new_id())
            )
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)
        suppressed = next(
            item for item in runtime.store.list("system_log") if item["id"] != record["id"]
        )
        event = next(
            item for item in runtime.store.events() if item["id"] == suppressed["event_id"]
        )
        assert suppressed["notify"] is False
        assert (
            attention_source(runtime.store, event, (), runtime.config.settings.system_events)
            is None
        )
        assert runtime.system_logger is not None
        runtime.system_logger.error(
            "PRIVATE",
            extra={
                "muli_component": "channels",
                "muli_code": "telegram_receive_failed",
                "producer": "channel_receiver",
                "occurrence_id": new_id(),
            },
        )
        await eventually(lambda: runtime.system_events.status()["persisted"] == 3)
        local = next(
            item for item in runtime.store.list("system_log") if item["producer"] == "legacy_local"
        )
        assert source_for(runtime, local).classification == "legacy_local"
        assert "PRIVATE" not in json.dumps(runtime.store.list("system_log"))
        captured = runtime.system_events.status()["captured"]
        runtime.system_events.channel_receive_failed("channel_failed", new_id())
        runtime.system_events.channel_receive_failed("telegram_receive_failed", "invalid")
        assert runtime.system_events.status()["captured"] == captured
        assert len(CHANNEL_RECEIVE_FAILURE_CODES) == 4
    finally:
        await runtime.close()
