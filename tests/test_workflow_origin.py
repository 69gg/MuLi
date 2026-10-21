"""Workflow results keep creation provenance without widening reports or budgets."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_channels import Harness, profile, unused
from test_report_origin import origin, route, wait_for_channel_cursor, wait_for_reply
from test_runtime import FakeClient, make_runtime
from test_workflow_scope import start_blocked_workflow
from test_workflows import step

from muli.channels import ChannelError, ChannelsConfig
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.reply_origin import ReplyOrigin
from muli.runtime import PreparedRequest, Runtime
from muli.workflows import JsonObject


@pytest.fixture(autouse=True)
def no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("workflow origin tests must not spawn processes")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


def workflow_call(name: str) -> ModelTurn:
    return ModelTurn(
        tool_calls=[
            ToolCall(
                "create-" + name,
                "workflow_create",
                json.dumps({"name": name, "steps": [step("work")]}),
            )
        ]
    )


def reports(runtime: Runtime, session_id: str) -> list[JsonObject]:
    return [
        message
        for message in runtime.store.messages(session_id)
        if message["role"] == "assistant" and "workflow_run_id" in message
    ]


def put_children(
    runtime: Runtime, parent_session_id: str, parent_run_id: str, children: list[JsonObject]
) -> None:
    runtime.store.put_many(
        [
            (
                "workflow_run",
                {
                    "name": item["id"],
                    "status": "completed",
                    "nodes": {},
                    "result": "evidence",
                    **item,
                    "parent_run_id": parent_run_id,
                    "parent_session_id": parent_session_id,
                },
            )
            for item in children
        ]
    )


async def start_stored_children(runtime: Runtime, children: list[JsonObject]) -> JsonObject:
    async def execute(
        session_id: str, run_id: str, role: str, mode: str, workflow_step: bool
    ) -> str:
        put_children(runtime, session_id, run_id, children)
        return "arranged"

    # The real driver still owns waiting, all-success admission, budget and publication.
    runtime._execute = execute  # type: ignore[method-assign]
    # This fixture replaces execution, so use the direct local entry rather
    # than enqueueing ingress that the replaced executor cannot claim.
    return await runtime.start(runtime.main_session()["id"], "collect stored evidence", "yolo")


async def test_creation_freezes_origin_before_definition_or_start_can_change_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    frozen = origin("A", target="7")
    request_token = runtime._request_context.set(PreparedRequest(parent, [], reply_origin=frozen))
    session_token = runtime._tool_session.set(parent)
    create = runtime.workflows.create

    def change_context(*args: Any, **kwargs: Any) -> JsonObject:
        runtime._request_context.set(
            PreparedRequest(parent, [], reply_origin=origin("B", target="8"))
        )
        return create(*args, **kwargs)

    async def complete(session_id: str, prompt: str, role: str, mode: str) -> str:
        return "evidence"

    monkeypatch.setattr(runtime.workflows, "create", change_context)
    runtime.workflows.run_step = complete
    tool = runtime._workflow_tool("yolo", parent, "parent-run")
    try:
        started = await tool.handler({"name": "creation", "steps": [step("work")]})
        finished = await asyncio.wait_for(runtime.workflows.wait(started["id"]), 2)
        assert finished["reply_origin"] == frozen.dump()
        assert "reply_origin" not in tool.parameters["properties"]
        assert not any("reply_origin" in node for node in finished["nodes"].values())
    finally:
        runtime._tool_session.reset(session_token)
        runtime._request_context.reset(request_token)
        await runtime.close()


@pytest.mark.parametrize("remaining", [0, 1, 2, 10])
async def test_all_origins_share_one_text_budget_and_separator_only_tail_stays_local(
    tmp_path: Path, remaining: int
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    budget = 1000
    runtime.config.update({"limits": {"max_output_chars": budget}})
    first_origin = origin("a" * 512, target="7")
    second_origin = origin("b" * 512, target="8")
    children = [
        {
            "id": "a",
            "name": "A",
            "result": "🙂" * (budget - remaining - 2),
            "reply_origin": first_origin.dump(),
        },
        {"id": "b", "name": "B", "result": "B evidence", "reply_origin": second_origin.dump()},
    ]
    try:
        started = await start_stored_children(runtime, children)
        result = await asyncio.wait_for(runtime.wait(started["run_id"]), 2)
        expected = "\n\n".join(item["name"] + "\n" + item["result"] for item in children)[:budget]
        assert result == expected and len(result) == budget
        actual = reports(runtime, started["session_id"])
        assert sum(len(item["content"]) for item in actual) <= budget
        assert len(actual) == (2 if remaining > 2 else 1)
        for message in actual:
            expected_origin = first_origin if message["workflow_run_id"] == "a" else second_origin
            assert message["content"].strip()
            assert message["reply_origin"] == expected_origin.dump()
            assert message["delivery_routes"] == expected_origin.delivery_routes()
            assert json.loads(json.dumps(message, ensure_ascii=False)) == message
        if remaining <= 2:
            omitted = runtime.store.get("workflow_run", "b")
            assert omitted is not None and "result_message_id" not in omitted
    finally:
        await runtime.close()


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
async def test_later_unsuccessful_child_prevents_every_partial_report_after_budget_is_full(
    tmp_path: Path, status: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    children = [
        {"id": "a", "result": "success" * 1000, "reply_origin": origin().dump()},
        {"id": "b", "status": status, "reply_origin": origin("B", target="8").dump()},
    ]
    try:
        started = await start_stored_children(runtime, children)
        with pytest.raises(ValueError, match="子工作流未成功完成"):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 2)
        assert not reports(runtime, started["session_id"])
        assert all(
            "result_message_id" not in item for item in runtime.store.list("workflow_run", 10)
        )
    finally:
        await runtime.close()


async def test_parent_cancellation_while_waiting_never_publishes_partial_reports(
    tmp_path: Path,
) -> None:
    scenario = await start_blocked_workflow(tmp_path)
    try:
        scenario.release_parent_final.set()
        await scenario.runtime.stop(scenario.parent_id)
        assert not reports(scenario.runtime, scenario.parent_id)
        child = scenario.runtime.store.get("workflow_run", scenario.child_id)
        assert child is not None and child["status"] == "cancelled"
        assert "result_message_id" not in child
    finally:
        await scenario.close()


async def test_batch_transaction_failure_rolls_back_messages_events_and_markers_then_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    put_children(
        runtime,
        parent,
        "parent",
        [
            {"id": "a", "reply_origin": origin().dump()},
            {"id": "b", "reply_origin": origin("B", target="8").dump()},
        ],
    )
    before = runtime.store.event_position()
    put = runtime.store._put

    def fail_second_marker(kind: str, value: JsonObject) -> None:
        if kind == "workflow_run" and value["id"] == "b" and "result_message_id" in value:
            raise OSError("second marker write failed")
        put(kind, value)

    fragments = [("a", "A evidence"), ("b", "B evidence")]
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_put", fail_second_marker)
            with pytest.raises(OSError, match="second marker"):
                runtime.workflows.publish_results("parent", parent, fragments)
        assert not reports(runtime, parent)
        assert runtime.store.events(before) == []
        assert all(
            "result_message_id" not in item for item in runtime.store.list("workflow_run", 10)
        )
        runtime.workflows.publish_results("parent", parent, fragments)
        committed = reports(runtime, parent)
        assert len(committed) == 2
        position = runtime.store.event_position()
        runtime.workflows.publish_results("parent", parent, fragments)
        assert reports(runtime, parent) == committed and runtime.store.event_position() == position
    finally:
        await runtime.close()


async def test_postcommit_notify_cancellation_keeps_the_entire_batch_and_restart_does_not_republish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    notify = runtime.bus.notify
    interrupted = False

    async def cancel_after_commit() -> None:
        nonlocal interrupted
        if not interrupted and len(reports(runtime, parent)) == 2:
            interrupted = True
            raise asyncio.CancelledError
        await notify()

    monkeypatch.setattr(runtime.bus, "notify", cancel_after_commit)
    try:
        started = await start_stored_children(runtime, [{"id": "a"}, {"id": "b"}])
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runtime.wait(started["run_id"]), 2)
        committed = reports(runtime, parent)
        assert interrupted and len(committed) == 2
        for message in committed:
            child = runtime.store.get("workflow_run", message["workflow_run_id"])
            assert child is not None and child["result_message_id"] == message["id"]
        position = runtime.store.event_position()
    finally:
        await runtime.close()
    second = Runtime(runtime.config, runtime.data_dir, client_factory=runtime.client_factory)
    try:
        assert reports(second, parent) == committed
        assert second.store.event_position() == position
        assert not second.tasks and not second.workflows.active_run_ids
    finally:
        await second.close()


async def test_restart_keeps_completed_but_unpublished_child_local_without_replaying_nodes(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    put_children(
        runtime, parent, "lost-parent", [{"id": "finished", "reply_origin": origin().dump()}]
    )
    runtime.store.put("run", {"id": "lost-parent", "session_id": parent, "status": "running"})
    await runtime.close()
    second = Runtime(runtime.config, runtime.data_dir, client_factory=runtime.client_factory)
    try:
        child = second.store.get("workflow_run", "finished")
        assert child is not None and child["status"] == "completed"
        assert child["result"] == "evidence" and "result_message_id" not in child
        assert not reports(second, parent)
        assert not second.tasks and not second.workflows.active_run_ids
    finally:
        await second.close()


@pytest.mark.parametrize(
    "field,value",
    [("parent_run_id", "other-run"), ("parent_session_id", "other-session"), ("status", "failed")],
)
async def test_foreign_or_unfinished_child_rejects_the_whole_publication_batch(
    tmp_path: Path, field: str, value: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    put_children(runtime, parent, "parent", [{"id": "a"}, {"id": "b"}])
    child = runtime.store.get("workflow_run", "b")
    assert child is not None
    runtime.store.put("workflow_run", {**child, field: value})
    try:
        with pytest.raises(ValueError, match="父运行不匹配"):
            runtime.workflows.publish_results("parent", parent, [("a", "A"), ("b", "B")])
        assert not reports(runtime, parent)
        first = runtime.store.get("workflow_run", "a")
        assert first is not None and "result_message_id" not in first
    finally:
        await runtime.close()


@pytest.mark.parametrize("value", [None, {}, {"kind": "channel", "input_id": "old", "route": {}}])
async def test_old_or_invalid_workflow_provenance_only_produces_local_results(
    tmp_path: Path, value: object
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    child: JsonObject = {"id": "old"}
    if value is not None:
        child["reply_origin"] = value
    try:
        started = await start_stored_children(runtime, [child])
        await asyncio.wait_for(runtime.wait(started["run_id"]), 2)
        message = reports(runtime, started["session_id"])[0]
        assert message["reply_origin"] == ReplyOrigin().dump()
        assert message["delivery_routes"] == []
    finally:
        await runtime.close()


async def test_team_workflow_keeps_only_the_existing_parent_report_route(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    entered, release = asyncio.Event(), asyncio.Event()
    main_turns = [
        ModelTurn(
            tool_calls=[
                ToolCall(
                    "spawn", "spawn_agent", json.dumps({"task_name": "worker", "prompt": "A work"})
                )
            ]
        ),
        ModelTurn(content="delegated"),
        ModelTurn(content="B reply"),
        ModelTurn(content="team report"),
    ]
    worker_turns = [workflow_call("team-workflow"), ModelTurn(content="worker arranged")]

    def factory(config: ModelConfig) -> FakeClient:
        return FakeClient(worker_turns if config.model == "worker" else main_turns, [])

    async def work(session_id: str, prompt: str, role: str, mode: str) -> str:
        entered.set()
        await release.wait()
        return "workflow evidence"

    runtime.client_factory = factory  # type: ignore[assignment]
    runtime.workflows.run_step = work
    try:
        initial = await runtime.submit(
            "A dispatch", mode="yolo", source="channel:onebot:bot", route=route()
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(runtime.wait(initial["run_id"]), 2) == "delegated"
        followup = await runtime.submit("B topic", source="channel:onebot:bot", route=route("8"))
        assert await asyncio.wait_for(runtime.wait(followup["run_id"]), 2) == "B reply"
        release.set()
        reply = await wait_for_reply(runtime, "team report")
        assert reply["delivery_routes"] == [route()]
        agent = runtime.team.get("worker")
        child = runtime.store.list("workflow_run", 10)[0]
        assert child["parent_session_id"] == agent["id"]
        assert child["reply_origin"] == agent["reply_origin"]
        assert child["reply_origin"]["input_id"] == initial["message"]["id"]
        assert "result_message_id" not in child
        assert not reports(runtime, initial["session_id"])
        summary = next(
            message
            for message in runtime.store.messages(agent["id"])
            if message["content"] == "team-workflow\nworkflow evidence"
        )
        assert not summary.get("delivery_routes")
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("revocation", ["owner", "account", "group"])
async def test_actual_workflow_message_uses_existing_outbox_revocation_guards(
    tmp_path: Path, revocation: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    destination = route(kind="group", target="group") if revocation == "group" else route()
    original = ReplyOrigin.load({"kind": "channel", "input_id": "creation", "route": destination})
    put_children(runtime, parent, "parent", [{"id": "work", "reply_origin": original.dump()}])
    runtime.workflows.publish_results("parent", parent, [("work", "actual workflow evidence")])
    message = reports(runtime, parent)[0]
    configured = (
        profile(owner_ids=["8"])
        if revocation == "owner"
        else profile(account_id="new")
        if revocation == "account"
        else profile()
    )
    sink = Harness(tmp_path / "sink", ChannelsConfig(onebot=configured), unused)
    try:
        await sink.manager.start()
        event = await sink.bus.emit("message.completed", {"message": message}, parent)
        await wait_for_channel_cursor(sink, event["id"])
        item = sink.manager.deliveries()[0]
        assert item["state"] == "blocked" and item["route"] == destination
        with pytest.raises(ChannelError):
            await sink.manager.retry(item["id"])
        duplicate = await sink.bus.emit("message.completed", {"message": message}, parent)
        await wait_for_channel_cursor(sink, duplicate["id"])
        assert len(sink.manager.deliveries()) == 1
        assert sink.manager.delivery(item["id"])["state"] == "blocked"  # type: ignore[index]
    finally:
        await sink.close()
        await runtime.close()


async def test_replayed_workflow_message_never_requeues_a_recovered_unknown_delivery(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    put_children(runtime, parent, "parent", [{"id": "work", "reply_origin": origin().dump()}])
    runtime.workflows.publish_results("parent", parent, [("work", "actual workflow evidence")])
    message = reports(runtime, parent)[0]
    sink = Harness(tmp_path / "sink", ChannelsConfig(onebot=profile()), unused)
    try:
        item = await sink.manager.enqueue(
            "onebot", "7", message["content"], event_id=message["id"], route=route()
        )
        with sink.store.db:
            sink.store.db.execute(
                "UPDATE channel_outbox SET state='sending' WHERE id=?", (item["id"],)
            )
        await sink.manager.start()
        event = await sink.bus.emit("message.completed", {"message": message}, parent)
        await wait_for_channel_cursor(sink, event["id"])
        assert sink.manager.delivery(item["id"])["state"] == "unknown"  # type: ignore[index]
        assert len(sink.manager.deliveries()) == 1
        with pytest.raises(ChannelError, match="结果未知"):
            await sink.manager.retry(item["id"])
    finally:
        await sink.close()
        await runtime.close()


async def test_standalone_web_workflow_does_not_pick_the_latest_channel(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    parent = runtime.main_session()["id"]
    runtime.store.add_message(
        parent, "user", "old channel", source="channel:onebot:bot", principal="owner", route=route()
    )

    async def complete(session_id: str, prompt: str, role: str, mode: str) -> str:
        return "standalone evidence"

    runtime.workflows.run_step = complete
    try:
        definition = runtime.workflows.create("standalone", [step("work")])
        started = await runtime.workflows.start(definition["id"])
        finished = await asyncio.wait_for(runtime.workflows.wait(started["id"]), 2)
        assert finished["reply_origin"] == ReplyOrigin().dump()
        assert not reports(runtime, parent)
    finally:
        await runtime.close()


async def test_one_parent_run_keeps_two_origins_and_reports_after_all_children_and_tools(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    first_final, release_first_final = asyncio.Event(), asyncio.Event()
    last_final, release_last_final = asyncio.Event(), asyncio.Event()
    entered = {name: asyncio.Event() for name in ("A", "B")}
    release = {name: asyncio.Event() for name in ("A", "B")}
    turns = [
        workflow_call("A"),
        ModelTurn(content="A scheduled"),
        workflow_call("B"),
        ModelTurn(content="B scheduled"),
    ]

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if turns[0].content == "A scheduled":
                first_final.set()
                await release_first_final.wait()
            elif turns[0].content == "B scheduled":
                last_final.set()
                await release_last_final.wait()
            async for event in super().stream(messages, tools):
                yield event

    async def work(session_id: str, prompt: str, role: str, mode: str) -> str:
        session = runtime.store.get("session", session_id)
        assert session is not None
        child = runtime.store.get("workflow_run", session["workflow_run_id"])
        assert child is not None
        name = child["name"]
        entered[name].set()
        await release[name].wait()
        return name + " evidence"

    runtime.workflows.run_step = work
    runtime.client_factory = lambda _: Client(turns, [])  # type: ignore[assignment,return-value]
    try:
        first = await runtime.submit(
            "A work", mode="yolo", source="channel:onebot:bot", route=route()
        )
        await asyncio.wait_for(first_final.wait(), 2)
        second = await runtime.submit("B work", source="channel:onebot:bot", route=route("8"))
        assert second["run_id"] == first["run_id"]
        release_first_final.set()
        await asyncio.wait_for(last_final.wait(), 2)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        children = {item["name"]: item for item in runtime.store.list("workflow_run", 10)}
        release["B"].set()
        await asyncio.wait_for(runtime.workflows.wait(children["B"]["id"]), 2)
        assert not reports(runtime, first["session_id"])
        release_last_final.set()
        release["A"].set()
        result = await asyncio.wait_for(runtime.wait(first["run_id"]), 3)
        messages = runtime.store.messages(first["session_id"])
        actual = reports(runtime, first["session_id"])
        assert len(actual) == 2
        for name, submitted, destination in (("A", first, route()), ("B", second, route("8"))):
            message = next(
                item for item in actual if item["workflow_run_id"] == children[name]["id"]
            )
            assert message["content"] == name + "\n" + name + " evidence"
            assert message["reply_origin"]["input_id"] == submitted["message"]["id"]
            assert message["delivery_routes"] == [destination]
            child = runtime.store.get("workflow_run", children[name]["id"])
            assert child is not None and child["result_message_id"] == message["id"]
            earlier = messages[: messages.index(message)]
            assert {"create-A", "create-B"} <= {
                item["tool_call_id"] for item in earlier if item["role"] == "tool"
            }
        ordered = sorted(children.values(), key=lambda child: child["id"])
        assert result == "\n\n".join(
            child["name"] + "\n" + child["name"] + " evidence" for child in ordered
        )
    finally:
        release_first_final.set()
        release_last_final.set()
        for event in release.values():
            event.set()
        await runtime.close()
