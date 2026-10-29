"""Real Runtime/approval boundaries; external adapter effects are controlled locally."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attachment_memory_policy import actual_app
from test_attention import attention_inputs, eventually
from test_channels import terminal
from test_memory_watch_attention import components, revoke
from test_memory_watch_origin_policy import bound_match
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.providers import Message, ModelTurn, StreamEvent


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Origin Runtime controls must not call external HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_bound_watch_interrupted_partial_has_no_automatic_delivery_route(
    tmp_path: Path,
    failure: str,
) -> None:
    rig = components(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    fragment = "在 Web 流式说明的原渠道资料线索"

    class Partial(FakeClient):
        async def stream(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]],
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(list(messages))
            yield StreamEvent("text_delta", text=fragment)
            entered.set()
            await release.wait()
            raise RuntimeError("controlled interrupted model stream")

    rig.runtime.client_factory = lambda _: Partial([], rig.seen)  # type: ignore[assignment,return-value]
    task: asyncio.Task[Any] | None = None
    try:
        await rig.runtime.startup()
        _, _, routing = await bound_match(rig)
        await rig.inbox.start()
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        assert attention_inputs(rig.runtime)[0]["attention_routing"] == routing.dump()
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        await rig.runtime.wake_pending_inputs()
        await asyncio.wait_for(entered.wait(), 3)
        task = rig.runtime.tasks[rig.runtime.main_session()["id"]]
        if failure == "cancel":
            task.cancel()
        else:
            release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        await eventually(lambda: not rig.runtime.tasks)
        messages = rig.runtime.store.messages(rig.runtime.main_session()["id"])
        partials = [message for message in messages if message["role"] == "assistant"]
        assert len(partials) == 1 and partials[0]["content"] == fragment
        assert partials[0]["interrupted"] is True
        assert partials[0]["delivery_routes"] == []
        assert partials[0]["reply_origin"] == routing.origin.dump()
        runs = rig.runtime.store.list("run")
        assert len(runs) == 1 and runs[0]["status"] == (
            "cancelled" if failure == "cancel" else "failed"
        )
        assert (
            rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        )
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        await rig.close()


@pytest.mark.parametrize("change", ["cancel", "forget", "task"])
async def test_bound_watch_revocation_after_context_await_never_enters_model(
    tmp_path: Path,
    change: str,
) -> None:
    rig = components(tmp_path, [ModelTurn(content="不得开始的旧回访")])
    entered, release = asyncio.Event(), asyncio.Event()

    async def context(identifier: str, messages: list[Message]) -> list[Message]:
        request = rig.runtime.request_context(identifier)
        assert request is not None and request.memory_watch is not None
        assert (
            request.attention_routing is not None
            and request.attention_routing.classification == "bound"
        )
        entered.set()
        await release.wait()
        return messages

    rig.runtime.context_hooks.append(context)
    task: asyncio.Task[Any] | None = None
    try:
        await rig.runtime.startup()
        proof, _, _ = await bound_match(rig)
        await rig.inbox.start()
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        await rig.runtime.wake_pending_inputs()
        await asyncio.wait_for(entered.wait(), 3)
        task = rig.runtime.tasks[rig.runtime.main_session()["id"]]
        if change == "task":
            task.cancel()
        else:
            revoke(rig, proof, change)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        await eventually(lambda: not rig.runtime.tasks)
        assert rig.seen == []
        runs = rig.runtime.store.list("run")
        assert len(runs) == 1 and runs[0]["status"] == "cancelled"
        assert not any(
            message["role"] == "assistant"
            for message in rig.runtime.store.messages(
                rig.runtime.main_session()["id"],
            )
        )
        assert (
            rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        )
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        await rig.close()


@pytest.mark.parametrize("forgery", ["target", "account", "route", "independent"])
async def test_watch_registration_tool_rejects_model_selected_routing(
    tmp_path: Path,
    forgery: str,
) -> None:
    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update(
            {
                "attention": {"enabled": True, "events": ["memory"]},
                "persona": {"prompt": "请完成当前任务。"},
            }
        )
        arguments: dict[str, Any] = {
            "question": "后续留意词甲",
            "match_terms": ["词甲"],
            "key": "forged-routing",
            "reply_preference": "origin",
        }
        if forgery == "independent":
            arguments["reply_preference"] = "independent"
        else:
            arguments[forgery] = "another-recipient"
        turns = [
            ModelTurn(tool_calls=[call("forged-watch", "memory_watch_create", **arguments)]),
            ModelTurn(content="登记结果以真实工具回执为准。"),
        ]
        rig.runtime.client_factory = lambda _: FakeClient(turns, [])  # type: ignore[assignment,return-value]
        receipt = await rig.runtime.submit("长期留意这个问题，但资料文字不是新授权。", mode="yolo")
        await asyncio.wait_for(rig.runtime.wait(receipt["run_id"]), 4)
        result = rig.result("forged-watch")
        assert "error" in result
        assert rig.memory.watches.records(rig.runtime.main_session()["id"]) == []
        assert (
            rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    "decision", ["approve", "default", "reject", "cancel", "forget", "unbound", "target", "account"]
)
async def test_explicit_origin_channel_send_keeps_current_approval_and_canonical_destination(
    tmp_path: Path,
    decision: str,
) -> None:
    rig = components(tmp_path)
    rig.runtime.config.update(
        {
            "persona": {"prompt": "请完成当前任务。"},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "bot",
                    "base_url": "https://platform.invalid",
                    "owner_ids": ["7", "8"],
                    "token": "TEST_ONLY_TOKEN",
                }
            },
        }
    )
    application = ChannelApplication(
        rig.runtime,
        Attachments(rig.runtime.store, tmp_path / "uploads", rig.runtime.config),
    )
    sent: list[tuple[dict[str, Any], str]] = []

    async def send(
        destination: dict[str, Any], text: str, file: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent.append((dict(destination), text))
        return {"message_id": "local-adapter-receipt-" + str(len(sent))}

    application.manager.adapters["onebot"].send = send  # type: ignore[method-assign]
    body = "主 AI 明确选择发送的资料线索"
    arguments: dict[str, Any] = {
        "channel": "onebot",
        "target": "8" if decision == "target" else "7",
        "text": body,
        "idempotency_key": "origin-watch-explicit",
    }
    if decision == "account":
        arguments["route"] = {
            "name": "onebot",
            "account_id": "different",
            "sender_id": "7",
            "target": "7",
            "kind": "private",
        }
    turns = [
        ModelTurn(tool_calls=[call("explicit-send", "channel_send", **arguments)]),
        ModelTurn(content="外发结果以渠道工具回执为准。"),
    ]
    rig.runtime.client_factory = lambda _: FakeClient(turns, rig.seen)  # type: ignore[assignment,return-value]

    def body_deliveries() -> list[dict[str, Any]]:
        return [
            item
            for item in application.manager.deliveries()
            if any(part.get("text") == body for part in item["payload"]["parts"])
        ]

    try:
        await rig.runtime.startup()
        await application.start()
        proof, _, routing = await bound_match(rig)
        await rig.inbox.start()
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        assert attention_inputs(rig.runtime)[0]["mode"] == "ask"
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        await rig.runtime.wake_pending_inputs()
        await eventually(lambda: bool(rig.runtime.permissions.pending))
        identifier = next(iter(rig.runtime.permissions.pending))
        approval = rig.runtime.store.get("approval", identifier)
        assert approval is not None and approval["tool"] == "channel_send"
        assert approval["risk"] == "external" and approval["status"] == "pending"
        # Existing mobile approval notices are separate host notices, never this body.
        assert body_deliveries() == [] and not any(text == body for _, text in sent)
        if decision in {"cancel", "forget"}:
            revoke(rig, proof, decision)
        elif decision == "unbound":
            rig.runtime.config.update({"channels": {"onebot": {"owner_ids": ["8"]}}})
        elif decision == "default":
            rig.runtime.config.update(
                {
                    "channels": {
                        "proactive": {
                            "default_destination": {
                                "kind": "channel",
                                "route": {
                                    "name": "onebot",
                                    "account_id": "bot",
                                    "sender_id": "8",
                                    "target": "8",
                                    "kind": "private",
                                },
                            }
                        }
                    }
                }
            )
        await rig.runtime.permissions.resolve(
            identifier, "reject" if decision == "reject" else "approve"
        )
        await eventually(lambda: not rig.runtime.tasks)
        if decision in {"approve", "default"}:
            await eventually(
                lambda: bool(body_deliveries()) and body_deliveries()[0]["state"] == "sent"
            )
            (delivery,) = body_deliveries()
            assert delivery["route"] == routing.origin.delivery_routes()[0]
            assert delivery["payload"]["attention_routing"] == routing.dump()
            assert [(destination, text) for destination, text in sent if text == body] == [
                (routing.origin.delivery_routes()[0], body),
            ]
        else:
            assert body_deliveries() == [] and not any(text == body for _, text in sent)
        for message in rig.runtime.store.messages(rig.runtime.main_session()["id"]):
            if message["role"] == "assistant":
                assert message["delivery_routes"] == []
    finally:
        await rig.inbox.close()
        await application.close()
        await rig.close()


async def test_queued_origin_delivery_rechecks_configuration_before_each_segment(
    tmp_path: Path,
) -> None:
    """Direct ConfigFile drift is a fault injection, not the normal settings API."""
    rig = components(tmp_path)
    rig.runtime.config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "bot",
                    "base_url": "https://platform.invalid",
                    "owner_ids": ["7", "8"],
                    "token": "TEST_ONLY_TOKEN",
                    "max_message_chars": 2,
                }
            }
        }
    )
    application = ChannelApplication(
        rig.runtime,
        Attachments(rig.runtime.store, tmp_path / "uploads", rig.runtime.config),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    sent: list[str] = []

    async def send(
        destination: dict[str, Any], text: str, file: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent.append(text)
        entered.set()
        await release.wait()
        return {"id": "confirmed-first-segment"}

    application.manager.adapters["onebot"].send = send  # type: ignore[method-assign]
    try:
        _, _, routing = await bound_match(rig)
        await application.start()
        queued = await application.manager.enqueue(
            "onebot",
            "7",
            "一二三四",
            event_id="explicit-split-origin",
            route=routing.origin.delivery_routes()[0],
            automatic=True,
            attention_routing=routing.dump(),
        )
        await asyncio.wait_for(entered.wait(), 3)
        assert sent == ["一二"]
        rig.runtime.config.update({"channels": {"onebot": {"owner_ids": ["8"]}}})
        # Keep the stale manager intact to exercise its own current-settings
        # delivery guard, rather than replacing it and masking this boundary.
        assert application.manager.config.onebot.owner_ids == ["7", "8"]
        assert rig.runtime.config.settings.channels.onebot.owner_ids == ["8"]
        release.set()
        blocked = await terminal(application.manager, queued["id"])
        assert blocked["state"] == "blocked" and blocked["next_part"] == 1
        assert sent == ["一二"]
        assert blocked["receipts"] == [{"id": "confirmed-first-segment"}]
        assert blocked["route"] == routing.origin.delivery_routes()[0]
    finally:
        release.set()
        await application.close()
        await rig.close()
