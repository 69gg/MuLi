from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attention import attention_inputs, eventually, inbox_for
from test_proactive_delivery import configure, external
from test_report_origin import route
from test_runtime import FakeClient, make_runtime

from muli.proactive import FrozenRouting
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.system_events import attention_source
from muli.update_evidence import update_evidence, update_notice_available
from muli.updates import Updates


async def check(runtime: Runtime, *, failed: bool = False) -> dict[str, Any]:
    def metadata(request: httpx.Request) -> httpx.Response:
        if failed:
            return httpx.Response(503, content=b"PRIVATE_SERVICE_DETAILS")
        return httpx.Response(200, json={"info": {"name": "muli-agent", "version": "99.0.0"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(metadata)) as client:
        return await Updates(runtime.config, runtime.store, runtime.bus).check(
            client, independent=True
        )


def update_event(runtime: Runtime) -> dict[str, Any]:
    return next(
        event
        for event in runtime.store.events()
        if event["type"] == "notification.created" and event["data"].get("source") == "update"
    )


async def test_pending_update_survives_failed_check_while_main_is_busy_and_recovers_once(
    tmp_path: Path,
) -> None:
    turns = [ModelTurn(content="原工作完成。"), ModelTurn(content="发现新版本，更新前仍需确认。")]
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update({"memory": {"auto_extract": False}})
    inbox = inbox_for(runtime, events=["update"])
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if not seen:
                entered.set()
                await release.wait()
            async for event in super().stream(messages, tools):
                yield event

    runtime.client_factory = lambda _config: HeldClient(turns, seen)  # type: ignore[assignment]
    try:
        run = await runtime.submit("先完成当前工作。", mode="yolo")
        await eventually(entered.is_set)
        good = await check(runtime)
        notice = runtime.store.get("notification", "update-99.0.0")
        assert notice is not None and notice["update_evidence"] == update_evidence(good)
        failure = await check(runtime, failed=True)
        assert failure["status"] == "unavailable" and failure["available"] is False
        assert runtime.store.get("update", "latest") == failure
        await inbox.start()
        await eventually(lambda: inbox._waiting == "main")
        assert attention_inputs(runtime) == [] and len(seen) == 0
        release.set()
        await runtime.wait(run["run_id"])
        await eventually(lambda: len(seen) == 2 and not runtime.tasks)
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1 and "99.0.0" in inputs[0]["content"]
        assert "PRIVATE_SERVICE_DETAILS" not in inputs[0]["content"]
        assert not inputs[0].get("delivery_routes")
        events_before = [
            item["id"] for item in runtime.store.events() if item["type"] == "notification.created"
        ]
        recovered = await check(runtime)
        assert recovered["status"] == "checked" and recovered["available"] is True
        assert runtime.store.get("notification", notice["id"]) == notice
        assert [
            item["id"] for item in runtime.store.events() if item["type"] == "notification.created"
        ] == events_before
        assert len(runtime.store.list("attention_receipt")) == 1
        assert len(seen) == 2
    finally:
        release.set()
        await inbox.close()
        await runtime.close()


async def test_failed_recheck_preserves_the_notices_frozen_default_and_atomic_evidence(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config, "7")
    try:
        await check(runtime)
        event = update_event(runtime)
        notice = runtime.store.get("notification", event["data"]["id"])
        assert notice is not None and event["data"]["update_evidence"] == notice["update_evidence"]
        runtime.config.update({"channels": {"proactive": {"default_destination": external("8")}}})
        await check(runtime, failed=True)
        source = attention_source(runtime.store, event, ["update"], None)
        assert source is not None and source.routing == FrozenRouting.load(notice["routing"])
        assert source.routing.origin.delivery_routes() == [route("7")]
        for invalid_version in (True, 1.0):
            malformed = copy.deepcopy(event)
            malformed["data"]["update_evidence"]["format_version"] = invalid_version
            assert attention_source(runtime.store, malformed, ["update"], None) is None
        changed = copy.deepcopy(notice)
        changed["update_evidence"]["checked_at"] += 1
        runtime.store.put("notification", changed)
        assert attention_source(runtime.store, event, ["update"], None) is None
    finally:
        await runtime.close()


async def test_legacy_notice_keeps_the_original_gate_without_invented_proof(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        await check(runtime)
        event = update_event(runtime)
        legacy = copy.deepcopy(event["data"])
        legacy.pop("update_evidence")
        runtime.store.put("notification", legacy)
        event["data"] = legacy
        assert attention_source(runtime.store, event, ["update"], None) is not None
        malformed = copy.deepcopy(event)
        malformed["data"]["update_evidence"] = None
        assert attention_source(runtime.store, malformed, ["update"], None) is None
        await check(runtime, failed=True)
        assert attention_source(runtime.store, event, ["update"], None) is None
        assert "update_evidence" not in runtime.store.get("notification", legacy["id"])
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"format_version": True},
        {"format_version": 2},
        {"checked_at": True},
        {"checked_at": -1},
        {"checked_at": float("nan")},
        {"checked_at": float("inf")},
        {"checked_at": 10**1000},
        {"current": None},
        {"current": "invalid"},
        {"current": "99.0.0"},
        {"latest": "99.0.0-beta"},
        {"latest": "99.0.0" + "0" * 40},
        {"unexpected": "not copied"},
    ],
)
def test_malformed_new_evidence_cannot_fall_back_to_the_latest_check(
    changes: dict[str, Any],
) -> None:
    item = {
        "current": "2.0.0",
        "latest": "99.0.0",
        "checked_at": 100.0,
        "status": "checked",
        "available": True,
    }
    notice = {"id": "update-99.0.0", "update_evidence": {**update_evidence(item), **changes}}
    assert not update_notice_available(notice, item, "2.0.0")


def test_current_install_and_notice_identity_gate_historical_success() -> None:
    item = {"current": "2.0.0", "latest": "3.0.0", "checked_at": 100.0}
    notice = {"id": "update-3.0.0", "update_evidence": update_evidence(item)}
    assert update_notice_available(notice, {"status": "unavailable"}, "2.1.0")
    assert not update_notice_available(notice, None, "3.0.0")
    assert not update_notice_available(notice, None, "4.0.0")
    assert not update_notice_available({**notice, "id": "update-4.0.0"}, None, "2.1.0")
    for missing in (None, [], {}):
        assert not update_notice_available({**notice, "update_evidence": missing}, item, "2.1.0")
