"""Exercise ownership boundaries through real owner tools and channel admission."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_channel_ownership_policy import assigned
from test_mobile_channels import onebot_payload, telegram_payload
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, eventually

from muli.channels.manager import ChannelManager
from muli.channels.types import ChannelError, IncomingMessage
from muli.channels.weixin import Weixin
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store import new_id

pytestmark = pytest.mark.asyncio


def recorded_tool_result(
    runtime: Runtime, session_id: str, run_id: str, call_id: str
) -> dict[str, Any]:
    history = runtime.store.messages(session_id)
    triggers = [
        item
        for item in history
        if item["role"] == "user"
        and item.get("run_id") == run_id
        and item.get("source") == "web"
        and item.get("principal") == "owner"
    ]
    assert len(triggers) == 1
    records = [
        item for item in history if item.get("tool_call_id") == call_id and item["role"] == "tool"
    ]
    assert len(records) == 1
    assert records[0]["seq"] > triggers[0]["seq"]
    result = json.loads(records[0]["content"])
    assert isinstance(result, dict)
    return result


@asynccontextmanager
async def channel_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> AsyncIterator[tuple[Runtime, Any, httpx.AsyncClient, str]]:
    async with assigned(tmp_path, monkeypatch, owner_ids=["7", "8", "9"]) as host:
        runtime, app, _web, _child_id = host
        hold_poll = asyncio.Event()

        async def offline(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            expected = {
                "telegram": ("telegram.invalid", "/bot123:FAKE_BOUNDARY_TELEGRAM/getUpdates"),
                "weixin": ("ilink.invalid", "/ilink/bot/getupdates"),
            }
            assert channel in expected
            assert request.method == "POST"
            assert (request.url.host, request.url.path) == expected[channel]
            await hold_poll.wait()
            raise AssertionError("关闭应用必须取消其拥有的平台长轮询")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline)
        profile: dict[str, Any] = {
            "enabled": True,
            "account_id": "123",
            "owner_ids": ["7", "8", "9"],
        }
        if channel == "telegram":
            profile.update(base_url="https://telegram.invalid", token="123:FAKE_BOUNDARY_TELEGRAM")
        elif channel == "weixin":
            profile.update(
                base_url="https://ilink.invalid",
                token="FAKE_BOUNDARY_ILINK",
                user_id="wx-bot",
            )
        else:
            assert channel == "qq"
            profile.update(
                base_url="https://qq.invalid",
                app_id="123",
                client_secret="FAKE_BOUNDARY_QQ",
                token_url="https://qq.invalid/app/getAppAccessToken",
            )
        runtime.config.update({"channels": {channel: profile}})
        await app.state.channel_application.reconfigure()
        yield host


async def owner_delegate(
    runtime: Runtime, action: str, channel: str, scope: str = "account", **kwargs: Any
) -> dict[str, Any]:
    """Invoke the real tool in a fresh owner-authored main run."""
    previous = runtime.client_factory
    rounds = 0
    result: dict[str, Any] | None = None
    final = "已核对本次渠道归属工具回执。"
    call_id = "boundary-delegate-" + new_id()

    class Owner(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            assert "channel_delegate" in {item["function"]["name"] for item in tools}
            if rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            call_id,
                            "channel_delegate",
                            action=action,
                            channel=channel,
                            scope=scope,
                            **kwargs,
                        )
                    ]
                )
            else:
                assert rounds == 2
                assert any(item.tool_call_id == call_id for item in messages)
                turn = ModelTurn(content=final)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Owner([], []) if config.model == "main" else cast(FakeClient, previous(config))

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        submitted = await runtime.submit("主人明确要求执行本次渠道归属操作。")
        assert await asyncio.wait_for(runtime.wait(submitted["run_id"]), timeout=4) == final
        await eventually(runtime, lambda: runtime.main_session()["id"] not in runtime.tasks)
        result = recorded_tool_result(
            runtime, runtime.main_session()["id"], submitted["run_id"], call_id
        )
    finally:
        runtime.client_factory = previous
    assert result is not None and "error" not in result
    return result


async def completed_child(runtime: Runtime, tmp_path: Path, name: str) -> str:
    """Spawn, read and wait through real tools instead of creating agent rows."""
    previous = runtime.client_factory
    evidence = tmp_path / (name + ".txt")
    evidence_text = "先核对这份本地资料，再由主人决定后续接入范围。"
    evidence.write_text(evidence_text, encoding="utf-8")
    child_id = ""
    owner_rounds, worker_rounds = 0, 0
    final = "资料队友已真实完成，尚未取得新的渠道归属。"
    read_id, spawn_id, wait_id = ("boundary-" + new_id() for _ in range(3))

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal child_id, owner_rounds, worker_rounds
            if self.role == "worker":
                worker_rounds += 1
                if worker_rounds == 1:
                    turn = ModelTurn(tool_calls=[call(read_id, "fs_read", path=str(evidence))])
                else:
                    assert worker_rounds == 2
                    actual = next(item for item in messages if item.tool_call_id == read_id)
                    assert actual.source_message_id is not None
                    record = runtime.store.get("message", actual.source_message_id)
                    assert record is not None and record["tool_call_id"] == read_id
                    assert record["role"] == "tool"
                    assert json.loads(record["content"]) == {
                        "path": str(evidence.resolve()),
                        "content": evidence_text,
                    }
                    turn = ModelTurn(content="本地资料已核对。")
            else:
                assert self.role == "main"
                owner_rounds += 1
                if owner_rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                spawn_id,
                                "spawn_agent",
                                task_name=name,
                                prompt="只读核对，不外发：" + str(evidence),
                            )
                        ]
                    )
                elif owner_rounds == 2:
                    main_id = runtime.main_session()["id"]
                    spawned = recorded_tool_result(
                        runtime, main_id, runtime.run_ids[main_id], spawn_id
                    )
                    assert "error" not in spawned
                    child_id = spawned["id"]
                    turn = ModelTurn(
                        tool_calls=[call(wait_id, "wait_agents", targets=[child_id], timeout=2)]
                    )
                else:
                    assert owner_rounds == 3
                    main_id = runtime.main_session()["id"]
                    waited = recorded_tool_result(
                        runtime, main_id, runtime.run_ids[main_id], wait_id
                    )
                    assert waited["timed_out"] is False
                    assert any(
                        item["id"] == child_id and item["status"] == "completed"
                        for item in waited["agents"]
                    )
                    turn = ModelTurn(content=final)
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
    try:
        submitted = await runtime.submit("请派一位直属队友读取并完成这份资料。")
        assert await asyncio.wait_for(runtime.wait(submitted["run_id"]), timeout=4) == final
        await finished(runtime, child_id, 1)
        await eventually(runtime, lambda: runtime.main_session()["id"] not in runtime.tasks)
    finally:
        runtime.client_factory = previous
    assert worker_rounds == 2
    assert runtime.team.get(child_id)["parent_id"] == runtime.main_session()["id"]
    return child_id


async def finished(runtime: Runtime, child_id: str, generation: int) -> None:
    await eventually(
        runtime,
        lambda: (
            runtime.team.get(child_id)["generation"] >= generation
            and runtime.team.get(child_id)["status"]
            in {"completed", "failed", "cancelled", "interrupted", "paused"}
            and child_id not in runtime.tasks
            and runtime.team._active.get(child_id) is None
        ),
    )
    agent = runtime.team.get(child_id)
    run = runtime.store.get("run", agent["run_id"]) if agent.get("run_id") else None
    assert agent["generation"] == generation and agent["status"] == "completed", (
        agent["generation"],
        agent["status"],
        agent.get("result"),
        run.get("error") if run is not None else None,
    )


async def entered_run(
    runtime: Runtime, child_id: str, generation: int, entered: asyncio.Event
) -> None:
    await eventually(
        runtime,
        lambda: (
            entered.is_set()
            or (
                runtime.team.get(child_id)["generation"] >= generation
                and runtime.team.get(child_id)["status"]
                in {"completed", "failed", "cancelled", "interrupted", "paused"}
            )
        ),
    )
    if not entered.is_set():
        await finished(runtime, child_id, generation)
        raise AssertionError("真实运行在到达预期门控前结束")


def decoded(
    manager: ChannelManager, channel: str, text: str, *, owner: str = "7", number: int
) -> IncomingMessage:
    adapter = manager.adapters[channel]
    if channel == "onebot":
        payload = onebot_payload(text)
        payload.update(message_id=number, user_id=int(owner))
    elif channel == "telegram":
        payload = telegram_payload(text)
        payload["update_id"] = number
        payload["message"]["message_id"] = number
        payload["message"]["from"]["id"] = int(owner)
        payload["message"]["chat"]["id"] = int(owner)
    elif channel == "qq":
        payload = {
            "id": f"boundary-event-{number}",
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": f"boundary-message-{number}",
                "author": {"user_openid": owner},
                "content": text,
            },
        }
    else:
        assert channel == "weixin" and isinstance(adapter, Weixin)
        message = adapter._message(
            {
                "message_type": 1,
                "message_id": f"boundary-message-{number}",
                "from_user_id": owner,
                "context_token": "FAKE_BOUNDARY_ILINK_CONTEXT",
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            }
        )
        assert message is not None
        return message
    messages = adapter.webhook(payload)
    assert len(messages) == 1
    return messages[0]


async def completed_input(
    runtime: Runtime,
    manager: ChannelManager,
    channel: str,
    child_id: str,
    *,
    number: int,
    owner: str = "7",
) -> dict[str, Any]:
    previous = runtime.client_factory
    generation = runtime.team.get(child_id)["generation"] + 1

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            current = next(item for item in reversed(messages) if item.role == "user")
            assert current.source_message_id is not None
            actual = runtime.store.get("message", current.source_message_id)
            assert actual is not None and actual["principal"] == "owner"
            assert actual["source"] == f"channel:{channel}:123"
            yield StreamEvent("done", turn=ModelTurn(content="仅在本会话核对，不外发。"))

    runtime.client_factory = lambda _config: Worker([], [])  # type: ignore[assignment]
    try:
        receipt = await manager.receive(
            channel, decoded(manager, channel, "主人本次私聊资料。", owner=owner, number=number)
        )
        assert receipt["session_id"] == child_id
        await finished(runtime, child_id, generation)
    finally:
        runtime.client_factory = previous
    proof = runtime.channel_ownership.current_proof(child_id, runtime.team.get(child_id)["run_id"])
    assert proof is not None and set(proof) == {
        "id",
        "epoch",
        "main_id",
        "agent_id",
        "input_id",
        "run_id",
    }
    return proof


async def test_repeated_private_reclaim_cannot_restore_a_previous_run_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with channel_host(tmp_path, monkeypatch, "telegram") as (runtime, app, _web, account_a):
        await owner_delegate(runtime, "reclaim", "onebot")
        private_b = await completed_child(runtime, tmp_path, "私聊资料队友")
        account = await owner_delegate(runtime, "assign", "telegram", agent_id=account_a)
        pin = await owner_delegate(runtime, "reclaim", "telegram", "private", owner_id="7")
        first = await owner_delegate(
            runtime, "assign", "telegram", "private", owner_id="7", agent_id=private_b
        )
        manager = app.state.channel_application.manager
        old_proof = await completed_input(runtime, manager, "telegram", private_b, number=1101)
        runtime.channel_ownership.guard(old_proof, session_id=private_b)
        reclaimed = await owner_delegate(runtime, "reclaim", "telegram", "private", owner_id="7")
        repeated = await owner_delegate(runtime, "reclaim", "telegram", "private", owner_id="7")
        reassigned = await owner_delegate(
            runtime, "assign", "telegram", "private", owner_id="7", agent_id=private_b
        )
        assert runtime.store.get("channel_ownership", account["id"]) == account
        assert runtime.channel_ownership.for_agent(account_a) == account
        # This is the actual old input/run proof, not a reconstructed scope token.
        with pytest.raises(PermissionError):
            runtime.channel_ownership.guard(old_proof, session_id=private_b)
        epochs = [item["epoch"] for item in (pin, first, reclaimed, repeated, reassigned)]
        assert all(after > before for before, after in zip(epochs, epochs[1:], strict=False))
        assert app.state.channel_application.manager.deliveries() == []


@pytest.mark.parametrize("channel", ["telegram", "qq", "weixin"])
async def test_same_account_and_owner_do_not_cross_channel_reclaim_or_running_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    async with channel_host(tmp_path, monkeypatch, channel) as (runtime, app, _web, child_a):
        old_pin = await owner_delegate(runtime, "reclaim", "onebot", "private", owner_id="7")
        await owner_delegate(runtime, "reclaim", "onebot")
        other_account = await owner_delegate(runtime, "assign", channel, agent_id=child_a)
        child_b = await completed_child(runtime, tmp_path, "另一渠道的资料队友")
        await owner_delegate(runtime, "assign", "onebot", agent_id=child_b)
        manager = app.state.channel_application.manager
        entered, release = asyncio.Event(), asyncio.Event()
        seen: list[str] = []

        class Worker(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                current = next(item for item in reversed(messages) if item.role == "user")
                assert current.source_message_id is not None
                actual = runtime.store.get("message", current.source_message_id)
                assert actual is not None and actual["session_id"] == child_a
                assert actual["source"] == f"channel:{channel}:123"
                assert actual["route"]["sender_id"] == "7"
                seen.append(actual["id"])
                if len(seen) == 1:
                    entered.set()
                    await runtime.bus.notify()
                    await release.wait()
                yield StreamEvent("done", turn=ModelTurn(content="当前渠道资料已核对。"))

        runtime.client_factory = lambda _config: Worker([], [])  # type: ignore[assignment]
        try:
            first = await manager.receive(
                channel, decoded(manager, channel, "另一渠道正在处理。", number=2101)
            )
            assert first["session_id"] == child_a
            await entered_run(runtime, child_a, 2, entered)
            active_run = runtime.run_ids[child_a]
            queued = await manager.receive(
                channel, decoded(manager, channel, "另一渠道仍待处理。", number=2102)
            )
            pending = runtime.conversation.pending(child_a)
            assert len(pending) == 1 and pending[0]["id"] == queued["message"]["id"]
            active = runtime.tasks[child_a]
            assert not runtime.channel_ownership.affects_running(old_pin)
            reclaimed = await owner_delegate(runtime, "reclaim", "onebot", "private", owner_id="7")
            assert reclaimed["channel"] == "onebot" and reclaimed["state"] == "main"
            assert runtime.tasks.get(child_a) is active and not active.cancelling()
            assert runtime.store.get("channel_ownership", other_account["id"]) == other_account
            assert runtime.conversation.pending(child_a)[0]["id"] == pending[0]["id"]
            assert (
                runtime.store.db.execute(
                    "SELECT state FROM ingress WHERE id=?", (pending[0]["id"],)
                ).fetchone()[0]
                == "pending"
            )
            release.set()
            await finished(runtime, child_a, 2)
            assert seen == [first["message"]["id"], queued["message"]["id"]]
            delivered = runtime.store.db.execute(
                "SELECT id,state,run_id FROM ingress "
                "WHERE session_id=? AND id IN (?,?) ORDER BY seq",
                (child_a, first["message"]["id"], queued["message"]["id"]),
            ).fetchall()
            assert [tuple(row) for row in delivered] == [
                (first["message"]["id"], "delivered", active_run),
                (queued["message"]["id"], "delivered", active_run),
            ]
            assert runtime.team.get(child_a)["run_id"] == active_run
            assert runtime.team.get(child_b)["generation"] == 1
            assert manager.deliveries() == []
        finally:
            release.set()


@pytest.mark.parametrize("channel", ["telegram", "qq"])
async def test_private_override_and_reclaim_preserve_other_private_and_account_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    async with channel_host(tmp_path, monkeypatch, channel) as (runtime, app, _web, account_a):
        await owner_delegate(runtime, "reclaim", "onebot")
        private_b = await completed_child(runtime, tmp_path, "主人七资料队友")
        private_c = await completed_child(runtime, tmp_path, "主人八资料队友")
        account = await owner_delegate(runtime, "assign", channel, agent_id=account_a)
        seven = await owner_delegate(
            runtime, "assign", channel, "private", owner_id="7", agent_id=private_b
        )
        eight = await owner_delegate(
            runtime, "assign", channel, "private", owner_id="8", agent_id=private_c
        )
        manager = app.state.channel_application.manager
        fallback_proof = await completed_input(
            runtime, manager, channel, account_a, owner="9", number=2201
        )
        seven_proof = await completed_input(runtime, manager, channel, private_b, number=2202)
        assert fallback_proof["id"] == account["id"] and seven_proof["id"] == seven["id"]
        entered, release = asyncio.Event(), asyncio.Event()
        seen: list[str] = []

        class Worker(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                current = next(item for item in reversed(messages) if item.role == "user")
                assert current.source_message_id is not None
                actual = runtime.store.get("message", current.source_message_id)
                assert actual is not None and actual["session_id"] == private_c
                assert actual["route"]["sender_id"] == "8"
                assert actual["channel_ownership"]["id"] == eight["id"]
                seen.append(actual["id"])
                if len(seen) == 1:
                    entered.set()
                    await runtime.bus.notify()
                    await release.wait()
                yield StreamEvent("done", turn=ModelTurn(content="主人八资料已核对。"))

        runtime.client_factory = lambda _config: Worker([], [])  # type: ignore[assignment]
        try:
            first = await manager.receive(
                channel, decoded(manager, channel, "主人八正在处理。", owner="8", number=2203)
            )
            assert first["session_id"] == private_c
            await entered_run(runtime, private_c, 2, entered)
            active_run = runtime.run_ids[private_c]
            queued = await manager.receive(
                channel, decoded(manager, channel, "主人八继续排队。", owner="8", number=2204)
            )
            active = runtime.tasks[private_c]
            reclaimed = await owner_delegate(runtime, "reclaim", channel, "private", owner_id="7")
            assert reclaimed["id"] == seven["id"] and reclaimed["state"] == "main"
            assert runtime.tasks.get(private_c) is active and not active.cancelling()
            assert runtime.store.get("channel_ownership", eight["id"]) == eight
            assert runtime.store.get("channel_ownership", account["id"]) == account
            assert [item["id"] for item in runtime.conversation.pending(private_c)] == [
                queued["message"]["id"]
            ]
            runtime.channel_ownership.guard(fallback_proof, session_id=account_a)
            with pytest.raises(PermissionError):
                runtime.channel_ownership.guard(seven_proof, session_id=private_b)
            previous_route = runtime.store.get("message", seven_proof["input_id"])["route"]
            assert runtime.channel_ownership.destination(previous_route) == (
                runtime.main_session()["id"],
                None,
            )
            release.set()
            await finished(runtime, private_c, 2)
            assert seen == [first["message"]["id"], queued["message"]["id"]]
            delivered = runtime.store.db.execute(
                "SELECT id,state,run_id FROM ingress "
                "WHERE session_id=? AND id IN (?,?) ORDER BY seq",
                (private_c, first["message"]["id"], queued["message"]["id"]),
            ).fetchall()
            assert [tuple(row) for row in delivered] == [
                (first["message"]["id"], "delivered", active_run),
                (queued["message"]["id"], "delivered", active_run),
            ]
            assert runtime.team.get(private_c)["run_id"] == active_run
            assert runtime.team.get(private_b)["generation"] == 2
            assert runtime.team.get(account_a)["generation"] == 2
            assert manager.deliveries() == []
        finally:
            release.set()


@pytest.mark.parametrize("channel", ["telegram", "qq", "weixin"])
async def test_return_control_authorizes_only_the_exact_private_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    async with channel_host(tmp_path, monkeypatch, channel) as (runtime, app, _web, child_id):
        await owner_delegate(runtime, "reclaim", "onebot")
        assigned_private = await owner_delegate(
            runtime, "assign", channel, "private", owner_id="7", agent_id=child_id
        )
        forbidden = tmp_path / "after-private-return.txt"
        call_ids = ["boundary-return-" + new_id() for _ in range(6)]
        controls: list[dict[str, Any]] = []
        rounds = 0

        class Worker(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds
                rounds += 1
                assert rounds == 1
                run_id = runtime.run_ids[child_id]
                proof = runtime.channel_ownership.current_proof(child_id, run_id)
                assert proof is not None and proof["id"] == assigned_private["id"]
                actual = runtime.store.get("message", proof["input_id"])
                assert actual is not None and actual["channel_ownership_control"] is True
                assert actual["source"] == "channel-ownership" and actual["route"] is None
                assert actual["principal"] == "agent:" + runtime.main_session()["id"]
                controls.append(proof)
                names = {item["function"]["name"] for item in tools}
                assert {"channel_delegate", "fs_read"} <= names
                assert not names & {"fs_write", "channel_send", "spawn_agent"}
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            call(
                                call_ids[0],
                                "channel_delegate",
                                action="return",
                                channel="onebot",
                                scope="private",
                                owner_id="7",
                            ),
                            call(
                                call_ids[1],
                                "channel_delegate",
                                action="return",
                                channel=channel,
                                scope="account",
                            ),
                            call(
                                call_ids[2],
                                "channel_delegate",
                                action="return",
                                channel=channel,
                                scope="private",
                                owner_id="8",
                            ),
                            call(
                                call_ids[3],
                                "channel_delegate",
                                action="return",
                                channel=channel,
                                scope="private",
                                owner_id="7",
                                agent_id=child_id,
                            ),
                            call(
                                call_ids[4],
                                "channel_delegate",
                                action="return",
                                channel=channel,
                                scope="private",
                                owner_id="7",
                            ),
                            call(
                                call_ids[5],
                                "fs_write",
                                path=str(forbidden),
                                content="不得写入",
                            ),
                        ]
                    ),
                )

        runtime.client_factory = lambda _config: Worker([], [])  # type: ignore[assignment]
        requested = await owner_delegate(
            runtime, "request_return", channel, "private", owner_id="7"
        )
        assert requested["state"] == "return_requested"
        await finished(runtime, child_id, 2)
        results = {
            item["tool_call_id"]: json.loads(item["content"])
            for item in runtime.store.messages(child_id)
            if item.get("tool_call_id") in call_ids
        }
        assert len(results) == len(call_ids)
        assert all("error" in results[identifier] for identifier in call_ids[:4])
        assert results[call_ids[4]]["state"] == "main"
        assert results[call_ids[5]]["status"] == "not_executed" and not forbidden.exists()
        agent = runtime.team.get(child_id)
        assert agent["result"] == "渠道已归还主助手。"
        report = runtime.store.get("team_report", agent["run_id"])
        assert report is not None and report["status"] == "local"
        renewed = await owner_delegate(
            runtime, "assign", channel, "private", owner_id="7", agent_id=child_id
        )
        assert renewed["epoch"] > assigned_private["epoch"] and len(controls) == 1
        with pytest.raises(PermissionError):
            runtime.channel_ownership.guard(controls[0], session_id=child_id)
        assert app.state.channel_application.manager.deliveries() == []


@pytest.mark.parametrize(("channel", "change"), [("weixin", "profile"), ("qq", "mode")])
async def test_profile_or_mode_aba_does_not_restore_an_old_private_input_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str, change: str
) -> None:
    async with channel_host(tmp_path, monkeypatch, channel) as (runtime, app, web, child_id):
        await owner_delegate(runtime, "reclaim", "onebot")
        initial = await owner_delegate(
            runtime, "assign", channel, "private", owner_id="7", agent_id=child_id
        )
        old_proof = await completed_input(
            runtime, app.state.channel_application.manager, channel, child_id, number=2301
        )
        if change == "profile":
            profile = getattr(runtime.config.settings.channels, channel).model_dump()
            runtime.config.update({"channels": {channel: {"owner_ids": ["8"]}}})
            await app.state.channel_application.reconfigure()
            runtime.config.update({"channels": {channel: profile}})
            await app.state.channel_application.reconfigure()
        else:
            assert change == "mode"
            main_id = runtime.main_session()["id"]
            for mode in ("ask", "yolo"):
                response = await web.patch(f"/api/sessions/{main_id}", json={"mode": mode})
                assert response.status_code == 200
        current = runtime.store.get("channel_ownership", initial["id"])
        assert current is not None and current["state"] == "main"
        assert current["epoch"] > initial["epoch"]
        with pytest.raises(PermissionError):
            runtime.channel_ownership.guard(old_proof, session_id=child_id)
        renewed = await owner_delegate(
            runtime, "assign", channel, "private", owner_id="7", agent_id=child_id
        )
        assert renewed["epoch"] > current["epoch"]
        runtime.channel_ownership.guard(
            runtime.channel_ownership.proof(renewed), session_id=child_id
        )
        with pytest.raises(PermissionError):
            runtime.channel_ownership.guard(old_proof, session_id=child_id)
        assert app.state.channel_application.manager.deliveries() == []


async def test_weixin_replay_keeps_its_first_child_after_reclaim_and_reassignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with channel_host(tmp_path, monkeypatch, "weixin") as (runtime, app, _web, child_a):
        await owner_delegate(runtime, "reclaim", "onebot")
        await owner_delegate(runtime, "assign", "weixin", agent_id=child_a)
        manager = app.state.channel_application.manager
        old_proof = await completed_input(runtime, manager, "weixin", child_a, number=2401)
        original = runtime.store.get("message", old_proof["input_id"])
        assert original is not None
        repeated = decoded(manager, "weixin", "主人本次私聊资料。", number=2401)
        frozen = await manager.receive("weixin", repeated)
        assert frozen["session_id"] == child_a and frozen["message"]["id"] == original["id"]
        child_b = await completed_child(runtime, tmp_path, "微信新接管资料队友")
        await owner_delegate(runtime, "reclaim", "weixin")
        await owner_delegate(runtime, "assign", "weixin", agent_id=child_b)
        inbound_count = manager.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[
            0
        ]
        ingress_count = runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
        replayed = await manager.receive("weixin", repeated)
        assert replayed == frozen
        with pytest.raises(ChannelError) as mismatch:
            await manager.receive(
                "weixin", decoded(manager, "weixin", "同一个平台ID不得改正文。", number=2401)
            )
        assert mismatch.value.status_code == 409
        assert manager.store.db.execute("SELECT count(*) FROM channel_inbound").fetchone()[0] == (
            inbound_count
        )
        assert (
            runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0] == ingress_count
        )
        assert runtime.team.get(child_a)["generation"] == 2
        assert runtime.team.get(child_b)["generation"] == 1
        assert (
            runtime.store.get("message", original["id"])["channel_ownership"]
            == (original["channel_ownership"])
        )
        with pytest.raises(PermissionError):
            runtime.channel_ownership.guard(old_proof, session_id=child_a)
        new_proof = await completed_input(runtime, manager, "weixin", child_b, number=2402)
        assert new_proof["agent_id"] == child_b and new_proof["input_id"] != old_proof["input_id"]
        assert manager.deliveries() == []
