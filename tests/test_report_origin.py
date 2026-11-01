from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import Harness, profile, terminal, unused
from test_runtime import FakeClient, make_runtime

from muli.channels import ChannelError, ChannelsConfig
from muli.conversation import Conversation
from muli.proactive import FrozenRouting
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.reply_origin import ReplyOrigin
from muli.runtime import PreparedRequest, Runtime
from muli.store import Store
from muli.tools import Tool, parameters


def route(target: str = "7", **changes: str) -> dict[str, str]:
    return {
        "name": "onebot",
        "account_id": "bot",
        "kind": "private",
        "target": target,
        "sender_id": target,
        **changes,
    }


def origin(identifier: str = "owner-input", **changes: str) -> ReplyOrigin:
    return ReplyOrigin.load({"kind": "channel", "input_id": identifier, "route": route(**changes)})


def assert_channel_message(message: Message, stored: dict[str, Any]) -> None:
    prefix = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
    assert message.role == stored["role"] == "user"
    assert message.source_message_id == stored["id"]
    assert stored["principal"] == "owner"
    destination = stored["route"]
    assert stored["source"] == f"channel:{destination['name']}:{destination['account_id']}"
    header, separator, content = message.content.partition("\n")
    assert separator and header.startswith(prefix)
    assert json.loads(header.removeprefix(prefix)) == {
        "channel": destination["name"],
        "account_id": destination["account_id"],
        "sender_id": destination["sender_id"],
        "target": destination["target"],
        "kind": destination["kind"],
        "input_id": stored["id"],
    }
    assert content == stored["content"]
    assert message.content.endswith("\n" + stored["content"])


async def wait_for_reply(runtime: Runtime, content: str) -> dict[str, Any]:
    def found() -> dict[str, Any] | None:
        return next(
            (
                message
                for message in runtime.store.messages(runtime.main_session()["id"])
                if message["role"] == "assistant" and message["content"] == content
            ),
            None,
        )

    async with asyncio.timeout(3):
        async with runtime.bus.changed:
            await runtime.bus.changed.wait_for(lambda: found() is not None)
    result = found()
    assert result is not None
    return result


async def wait_for_channel_cursor(harness: Harness, event_id: int) -> None:
    async with asyncio.timeout(3):
        async with harness.manager.changed:
            await harness.manager.changed.wait_for(
                lambda: int(harness.manager._state("events", "0")) >= event_id
            )


async def assistant_event(harness: Harness, identifier: str, destination: dict[str, str]) -> int:
    harness.store.complete_message(
        "main", "原任务报告", id=identifier, delivery_routes=[destination]
    )
    await harness.bus.notify()
    return int(harness.store.db.execute("SELECT max(id) FROM events").fetchone()[0])


def test_origin_is_frozen_and_never_infers_an_agents_destination() -> None:
    destination = route()
    destination["token"] = "MUST_NOT_COPY"
    value = ReplyOrigin.from_input(
        {"id": "a", "principal": "owner", "source": "channel:onebot:bot", "route": destination}
    )
    destination["target"] = "8"
    assert value.delivery_routes() == [route()]
    first = value.delivery_routes()
    first[0]["target"] = "8"
    assert value.delivery_routes() == [route()]
    with pytest.raises(FrozenInstanceError):
        value.input_id = "changed"  # type: ignore[misc]
    for principal in ("agent:child", "external:onebot:7", "system:attention"):
        assert not ReplyOrigin.from_input(
            {"id": "a", "principal": principal, "source": "channel:onebot:bot", "route": route()}
        ).delivery_routes()
    assert not ReplyOrigin.from_input(
        {"id": "a", "principal": "owner", "source": "channel:onebot:new", "route": route()}
    ).delivery_routes()


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"kind": []},
        {"kind": "channel", "input_id": "a", "route": []},
        {"kind": "channel", "input_id": "a", "route": {**route(), "target": ["7"]}},
        {"kind": "channel", "input_id": "a", "route": {**route(), "kind": "unknown"}},
        {"kind": "channel", "input_id": "a" * 513, "route": route()},
        {"kind": "channel", "input_id": None, "route": route()},
    ],
)
def test_malformed_stored_origins_stay_local(value: object) -> None:
    assert ReplyOrigin.load(value) == ReplyOrigin()


def test_claim_only_contiguous_destination_prefix_and_preserve_atomicity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "store.db")
    conversation = Conversation(store)
    try:
        receipts = [
            conversation.enqueue(
                "main",
                content,
                source="channel:onebot:bot",
                principal="owner",
                mode="ask",
                route=destination,
                max_pending=8,
            )[0]
            for content, destination in (
                ("A1", route()),
                ("A2", route()),
                ("B", route("8")),
                ("A3", route()),
            )
        ]
        original = store._put

        def fail_second(kind: str, item: dict[str, Any]) -> None:
            if kind == "message" and item["id"] == receipts[1]["id"]:
                raise ValueError("injected archive failure")
            original(kind, item)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_put", fail_second)
            with pytest.raises(ValueError, match="injected archive"):
                store.claim_inputs("main", "failed-run", limit=8)
        assert store.pending_inputs("main") == 4
        assert store.messages("main") == []
        first = store.claim_inputs("main", "run", limit=8)
        assert [message["content"] for message in first] == ["A1", "A2"]
        assert [message["reply_origin"]["input_id"] for message in first] == [
            receipt["id"] for receipt in receipts[:2]
        ]
        assert [message["content"] for message in store.claim_inputs("main", "run", limit=8)] == [
            "B"
        ]
        assert [message["content"] for message in store.claim_inputs("main", "run", limit=8)] == [
            "A3"
        ]
    finally:
        store.close()


async def test_request_origin_freezes_before_context_hook_await(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    session = runtime.main_session()
    runtime.conversation.enqueue(
        session["id"],
        "A",
        source="channel:onebot:bot",
        principal="owner",
        mode="ask",
        max_pending=8,
        route=route(),
    )
    runtime.store.claim_inputs(session["id"], "run", limit=8)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(identifier: str, messages: list[Message]) -> list[Message]:
        request = runtime.request_context(identifier)
        assert request is not None and request.reply_origin.delivery_routes() == [route()]
        entered.set()
        await release.wait()
        return messages

    runtime.context_hooks.append(hook)
    preparing = asyncio.create_task(runtime._prepare_request(session["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # A host control transaction can archive an owner input while a context
        # hook is suspended; it must not retarget that already prepared request.
        runtime.store.add_message(
            session["id"],
            "user",
            "B",
            source="channel:onebot:bot",
            principal="owner",
            route=route("8"),
        )
        release.set()
        prepared = await asyncio.wait_for(preparing, 2)
        assert prepared.reply_origin.delivery_routes() == [route()]
        assert runtime._delivery_routes(session["id"]) == [route("8")]
        assert prepared.execution_scope is None
    finally:
        release.set()
        await asyncio.gather(preparing, return_exceptions=True)
        await runtime.close()


async def test_two_queued_origins_get_separate_streamed_replies(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="A答复"), ModelTurn(content="B答复")])
    session = runtime.main_session()
    try:
        for content, destination in (("A", route()), ("B", route("8"))):
            runtime.conversation.enqueue(
                session["id"],
                content,
                source="channel:onebot:bot",
                principal="owner",
                mode="ask",
                max_pending=8,
                route=destination,
            )
        identifier = runtime._launch(session, "main", "ask")
        assert await asyncio.wait_for(runtime.wait(identifier), 3) == "B答复"
        replies = [
            message
            for message in runtime.store.messages(session["id"])
            if message["role"] == "assistant"
        ]
        assert [message["delivery_routes"] for message in replies] == [[route()], [route("8")]]
        inputs = [
            message
            for message in runtime.store.messages(session["id"])
            if message["role"] == "user"
        ]
        assert [message["content"] for message in inputs] == ["A", "B"]
        for messages, stored in zip(seen, inputs, strict=True):
            assert_channel_message(messages[-1], stored)
        deltas = [
            event["data"]["content"]
            for event in runtime.store.events()
            if event["type"] == "message.delta"
        ]
        assert deltas == ["A答复", "B答复"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("first_turn", ["answer", "tool"])
async def test_new_owner_replaces_attention_source_at_both_continuation_boundaries(
    tmp_path: Path, first_turn: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {"limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3}}
    )
    entered, release = asyncio.Event(), asyncio.Event()
    routing = FrozenRouting.bound(origin("reminder-owner"))

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(list(messages))
            if len(self.seen) == 1:
                entered.set()
                await release.wait()
                turn = (
                    ModelTurn(content="提醒资料已核对")
                    if first_turn == "answer"
                    else ModelTurn(tool_calls=[ToolCall("read-fact", "probe", "{}")])
                )
            else:
                assert len(self.seen) == 2
                stored = runtime.store.get("message", messages[-1].source_message_id or "")
                assert stored is not None and stored["content"] == "新的主人要求"
                assert_channel_message(messages[-1], stored)
                turn = ModelTurn(content="按新要求回复")
            yield StreamEvent("done", turn=turn)

    async def probe(args: dict[str, Any]) -> dict[str, bool]:
        pytest.fail("New pending owner input must defer the unstarted tool")

    runtime.tools["probe"] = Tool("probe", "核查提醒资料", parameters({}), probe)
    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        notice = await runtime.submit_attention(
            "真实系统提醒", "reminder-trigger", objects=[], routing=routing
        )
        assert notice is not None
        await asyncio.wait_for(entered.wait(), 2)
        run_id = runtime.run_ids[runtime.main_session()["id"]]
        owner = await runtime.submit("新的主人要求", source="channel:onebot:bot", route=route("8"))
        release.set()
        assert await asyncio.wait_for(runtime.wait(run_id), 3) == "按新要求回复"
        messages = runtime.store.messages(owner["session_id"])
        system_input = next(
            message for message in messages if message.get("principal") == "system:attention"
        )
        assert system_input["attention_routing"] == routing.dump()
        replies = [message for message in messages if message["role"] == "assistant"]
        assert replies[0]["attention_routing"] == routing.dump()
        assert replies[0]["delivery_routes"] == ([route()] if first_turn == "answer" else [])
        assert replies[-1]["reply_origin"]["input_id"] == owner["message"]["id"]
        assert replies[-1]["delivery_routes"] == [route("8")]
        assert "attention_routing" not in replies[-1]
        if first_turn == "tool":
            result = next(
                message for message in messages if message.get("tool_call_id") == "read-fact"
            )
            assert json.loads(result["content"])["status"] == "not_executed"
        assert len(seen) == 2 and len(messages) <= 32
    finally:
        release.set()
        await runtime.close()


async def test_new_origin_waits_until_the_whole_tool_batch_is_paired(tmp_path: Path) -> None:
    runtime, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("one", "first", "{}"), ToolCall("two", "second", "{}")]),
            ModelTurn(content="B答复"),
        ],
    )
    entered, release = asyncio.Event(), asyncio.Event()
    executed: list[str] = []

    async def first(args: dict[str, Any]) -> Any:
        executed.append("first")
        entered.set()
        await release.wait()
        request = runtime.request_context(runtime.main_session()["id"])
        assert request is not None and request.reply_origin.delivery_routes() == [route()]
        return "first finished"

    async def second(args: dict[str, Any]) -> Any:
        executed.append("second")
        return "must not execute"

    runtime.add_tool_provider(
        lambda _: [
            Tool("first", "first", parameters({}), first),
            Tool("second", "second", parameters({}), second),
        ]
    )
    try:
        initial = await runtime.submit("A", mode="yolo", source="channel:onebot:bot", route=route())
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.submit("B", source="channel:onebot:bot", route=route("8"))
        release.set()
        await asyncio.wait_for(runtime.wait(initial["run_id"]), 3)
        messages = runtime.store.messages(initial["session_id"])
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "user",
            "assistant",
        ]
        assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == [
            "one",
            "two",
        ]
        assert json.loads(messages[3]["content"])["status"] == "not_executed"
        assert executed == ["first"]
        assert messages[-1]["delivery_routes"] == [route("8")]
    finally:
        release.set()
        await runtime.close()


async def test_background_report_returns_to_assignment_origin_after_other_channel(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "limits": {"max_parallel_agents": 2},
        }
    )
    await runtime.reconfigure()
    entered, release = asyncio.Event(), asyncio.Event()
    main_turns = [
        ModelTurn(
            tool_calls=[
                ToolCall(
                    "spawn",
                    "spawn_agent",
                    json.dumps({"task_name": "worker", "prompt": "执行A任务"}),
                )
            ]
        ),
        ModelTurn(content="已派出"),
        ModelTurn(content="B答复"),
        ModelTurn(content="A任务报告"),
    ]

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            entered.set()
            await release.wait()
            yield StreamEvent("done", turn=ModelTurn(content="A任务结果"))

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], []) if config.model == "worker" else FakeClient(main_turns, [])

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        initial = await runtime.submit(
            "A派工", mode="yolo", source="channel:onebot:bot", route=route()
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(runtime.wait(initial["run_id"]), 3) == "已派出"
        followup = await runtime.submit("B新话题", source="channel:onebot:bot", route=route("8"))
        assert await asyncio.wait_for(runtime.wait(followup["run_id"]), 3) == "B答复"
        release.set()
        report_reply = await wait_for_reply(runtime, "A任务报告")
        assert report_reply["delivery_routes"] == [route()]
        agent = runtime.team.get("worker")
        assert agent["reply_origin"]["input_id"] == initial["message"]["id"]
        report = runtime.store.get("team_report", agent["run_id"])
        assert report is not None and report["reply_origin"] == agent["reply_origin"]
        messages = runtime.store.messages(initial["session_id"])
        archived = next(
            message for message in messages if str(message.get("source", "")).startswith("team:")
        )
        assert archived["principal"] == "agent:" + agent["id"]
        assert archived["reply_origin"] == agent["reply_origin"]
        assert runtime._latest_main_trigger(initial["session_id"])["content"] == "B新话题"  # type: ignore[index]
    finally:
        release.set()
        await runtime.close()


async def test_nested_agents_send_resume_and_reopen_keep_original_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    monkeypatch.setattr(runtime.team, "start", lambda **_kwargs: None)
    main = runtime.main_session()
    token = runtime._request_context.set(PreparedRequest(main["id"], [], reply_origin=origin()))
    try:
        parent = await runtime.team.spawn("parent", "A任务", parent_id=main["id"])
    finally:
        runtime._request_context.reset(token)
    try:
        child = await runtime.team.spawn("child", "子任务", parent_id=parent["id"])
        token = runtime._request_context.set(
            PreparedRequest(main["id"], [], reply_origin=origin("B", target="8"))
        )
        try:
            await runtime.team.send(parent["id"], "来自B的补充", sender=main["id"], wake=False)
        finally:
            runtime._request_context.reset(token)
        runtime.store.put("agent", {**runtime.team.get(parent["id"]), "status": "completed"})
        await runtime.team.resume(parent["id"])
        assert runtime.team.get(parent["id"])["reply_origin"] == origin().dump()
        assert child["reply_origin"] == origin().dump()
        for tool in runtime.team.tools(main["id"], "ask"):
            assert "reply_origin" not in tool.parameters.get("properties", {})
    finally:
        await runtime.close()
    store = Store(runtime.data_dir / "muli.sqlite3")
    try:
        assert store.get("agent", parent["id"])["reply_origin"] == origin().dump()  # type: ignore[index]
        assert store.get("agent", child["id"])["reply_origin"] == origin().dump()  # type: ignore[index]
    finally:
        store.close()


async def test_old_report_without_origin_stays_local_after_new_channel_input(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn(content="旧报告只在本地")])
    main = runtime.main_session()
    runtime.store.add_message(
        main["id"],
        "user",
        "最近B",
        source="channel:onebot:bot",
        principal="owner",
        route=route("8"),
    )
    report = {
        "id": "old-report",
        "parent_id": main["id"],
        "agent_id": "old-agent",
        "status": "pending",
        "content": "旧队友结果",
    }
    runtime.store.put("team_report", report)
    try:
        await runtime.team._deliver_report(report)
        reply = await wait_for_reply(runtime, "旧报告只在本地")
        assert reply["delivery_routes"] == []
        assert reply["reply_origin"]["kind"] == "unattributed"
    finally:
        await runtime.close()


async def test_report_origin_and_ingress_handoff_commit_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    monkeypatch.setattr(runtime, "_wake_main", lambda: None)
    monkeypatch.setattr(runtime.team, "start", lambda **_kwargs: None)
    report = {
        "id": "report",
        "parent_id": runtime.main_session()["id"],
        "agent_id": "child",
        "status": "pending",
        "content": "队友资料",
        "reply_origin": origin().dump(),
    }
    runtime.store.put("team_report", report)
    original = runtime.store._put

    def fail_delivery(kind: str, item: dict[str, Any]) -> None:
        if kind == "team_report" and item["status"] == "delivered":
            raise ValueError("injected handoff failure")
        original(kind, item)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_put", fail_delivery)
            await runtime.team._deliver_report(report)
        assert runtime.store.get("team_report", "report")["status"] == "pending"  # type: ignore[index]
        assert runtime.store.pending_inputs(report["parent_id"]) == 0
        await runtime.team._deliver_report(report)
        await runtime.team._deliver_report(report)
        assert runtime.store.pending_inputs(report["parent_id"]) == 1
        pending = runtime.conversation.pending(report["parent_id"])
        assert pending[0]["reply_origin"] == origin().dump()
        assert pending[0]["principal"] == "agent:child"
    finally:
        await runtime.close()


async def test_local_reply_and_explicit_inactive_outbox_have_independent_lifecycles(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    harness = Harness(tmp_path, ChannelsConfig(), unused)
    try:
        await harness.manager.start()
        event_id = await assistant_event(harness, "report-reply", route())
        await wait_for_channel_cursor(harness, event_id)
        assert harness.store.get("message", "report-reply")["content"] == "原任务报告"
        assert harness.manager.deliveries() == [] and calls == []
        blocked = await harness.manager.enqueue(
            "onebot",
            "7",
            "原任务报告",
            event_id="explicit-report-reply",
            route=route(),
            automatic=True,
            attention_routing=FrozenRouting.bound(origin()).dump(),
        )
        assert blocked["state"] == "blocked" and "未启用" in blocked["last_error"]
        assert (await terminal(harness.manager, blocked["id"]))["state"] == "blocked"
        with pytest.raises(ChannelError, match="未启用"):
            await harness.manager.retry(blocked["id"])
    finally:
        await harness.close()
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), handler)
    try:
        await harness.manager.start()
        event_id = await assistant_event(harness, "report-reply", route())
        await wait_for_channel_cursor(harness, event_id)
        assert calls == []
        assert harness.manager.delivery(blocked["id"])["state"] == "blocked"  # type: ignore[index]
        await harness.manager.retry(blocked["id"])
        assert (await terminal(harness.manager, blocked["id"]))["state"] == "sent"
        assert len(calls) == 1
    finally:
        await harness.close()


@pytest.mark.parametrize("revocation", ["owner", "account", "group"])
async def test_local_report_does_not_send_and_explicit_revoked_origin_is_blocked(
    tmp_path: Path, revocation: str
) -> None:
    destination = route(kind="group", target="group") if revocation == "group" else route()
    configured = (
        profile(owner_ids=["8"])
        if revocation == "owner"
        else profile(account_id="new")
        if revocation == "account"
        else profile()
    )
    harness = Harness(tmp_path, ChannelsConfig(onebot=configured), unused)
    try:
        await harness.manager.start()
        event_id = await assistant_event(harness, "report-reply", destination)
        await wait_for_channel_cursor(harness, event_id)
        assert harness.store.get("message", "report-reply")["content"] == "原任务报告"
        assert harness.manager.deliveries() == []
        frozen = ReplyOrigin.load(
            {"kind": "channel", "input_id": "report-input", "route": destination}
        )
        item = await harness.manager.enqueue(
            "onebot",
            destination["target"],
            "原任务报告",
            event_id="explicit-report-reply",
            route=destination,
            automatic=True,
            attention_routing=FrozenRouting.bound(frozen).dump(),
        )
        assert item["state"] == "blocked" and item["route"] == destination
        with pytest.raises(ChannelError):
            await harness.manager.retry(item["id"])
        assert harness.manager.delivery(item["id"])["state"] == "blocked"  # type: ignore[index]
    finally:
        await harness.close()


async def test_local_report_replay_never_requeues_explicit_unknown_outbox(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), unused)
    try:
        item = await harness.manager.enqueue(
            "onebot", "7", "原任务报告", event_id="uncertain", route=route()
        )
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET state='sending' WHERE id=?", (item["id"],)
            )
        await harness.manager.start()
        event_id = await assistant_event(harness, "uncertain", route())
        await wait_for_channel_cursor(harness, event_id)
        assert harness.store.get("message", "uncertain")["content"] == "原任务报告"
        assert harness.manager.delivery(item["id"])["state"] == "unknown"  # type: ignore[index]
        assert len(harness.manager.deliveries()) == 1
        with pytest.raises(ChannelError, match="结果未知"):
            await harness.manager.retry(item["id"])
    finally:
        await harness.close()


async def test_qq_blocked_reply_reserves_passive_sequence_only_on_explicit_retry(
    tmp_path: Path,
) -> None:
    configured = profile(enabled=False, app_id="app", client_secret="FAKE_SECRET")
    harness = Harness(tmp_path, ChannelsConfig(qq=configured), unused)
    destination = route(name="qq", account_id="app", reply_to="original-input")
    try:
        item = await harness.manager.enqueue(
            "qq", "7", "原任务报告", event_id="report", route=destination, automatic=True
        )
        assert item["state"] == "blocked"
        assert "sequence" not in item["payload"]["parts"][0]
        harness.manager.config = replace(
            harness.manager.config, qq=replace(configured, enabled=True)
        )
        # No worker is started: this test isolates the durable reservation. The
        # actual QQ transport is covered by the existing channels suite.
        harness.manager._active.add("qq")
        retried = await harness.manager.retry(item["id"])
        assert retried["state"] == "pending" and retried["payload"]["parts"][0]["sequence"] == 0
        duplicate = await harness.manager.enqueue(
            "qq", "7", "原任务报告", event_id="report", route=destination, automatic=True
        )
        assert duplicate["id"] == item["id"]
        assert duplicate["payload"]["parts"][0]["sequence"] == 0
    finally:
        await harness.close()
