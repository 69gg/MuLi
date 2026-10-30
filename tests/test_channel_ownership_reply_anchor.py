"""A delegated QQ reply keeps its real input's passive-message capability."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.channel_application import ChannelApplication
from muli.channels.crypto import sign_qq
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import now


@dataclass
class QQAssignment:
    runtime: Runtime
    channels: ChannelApplication
    web: httpx.AsyncClient
    main_id: str
    child_id: str
    account_id: str
    owner_id: str
    secret: str
    http_calls: list[tuple[str, str]]
    sent: list[dict[str, Any]]

    async def receive(self, text: str, *, message_id: str) -> None:
        payload = {
            "id": "event-" + message_id,
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": message_id,
                "author": {"user_openid": self.owner_id},
                "content": text,
            },
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timestamp = str(int(now()))
        response = await self.web.post(
            "/api/channels/qq/webhook",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Bot-Appid": self.account_id,
                "X-Signature-Timestamp": timestamp,
                "X-Signature-Ed25519": sign_qq(self.secret, timestamp.encode() + raw),
            },
        )
        assert response.status_code == 200 and response.json() == {"op": 12}

    def effects(self) -> dict[str, Any]:
        return {
            "outbox": [
                tuple(row)
                for row in self.runtime.store.db.execute(
                    "SELECT id,name,route,payload,state,next_part,receipts "
                    "FROM channel_outbox ORDER BY id"
                )
            ],
            "http": list(self.http_calls),
            "quota": [
                tuple(row)
                for row in self.runtime.store.db.execute(
                    "SELECT name,value FROM channel_state "
                    "WHERE name LIKE 'qq_sequence:%' ORDER BY name"
                )
            ],
        }


@pytest.fixture
async def qq_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[QQAssignment]:
    account_id, owner_id = "qq-anchor-app", "qq-anchor-owner"
    secret = "FAKE_QQ_REPLY_ANCHOR_SECRET"
    http_calls: list[tuple[str, str]] = []
    sent: list[dict[str, Any]] = []

    async def platform(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "qq.invalid"
        body = json.loads(request.content)
        http_calls.append((request.url.path, request.content.decode("utf-8")))
        if request.url.path == "/app/getAppAccessToken":
            assert body == {"appId": account_id, "clientSecret": secret}
            return httpx.Response(
                200, json={"access_token": "FAKE_QQ_ANCHOR_TOKEN", "expires_in": "7200"}
            )
        assert request.url.path == f"/v2/users/{owner_id}/messages"
        sent.append(body)
        return httpx.Response(200, json={"id": "qq-anchor-sent-" + str(len(sent))})

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "memory": {"auto_extract": False},
            "extensions": {"watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "channels": {
                "qq": {
                    "enabled": True,
                    "account_id": account_id,
                    "app_id": account_id,
                    "owner_ids": [owner_id],
                    "base_url": "https://qq.invalid",
                    "token_url": "https://qq.invalid/app/getAppAccessToken",
                    "client_secret": secret,
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    child_id = ""
    rounds = 0
    assignment: dict[str, Any] | None = None
    initial = "创建直属资料队友，等它正常完成后，把QQ主人私聊交给原队友。"
    prefix_final = "已按工具回执选择QQ私聊受托队友。"
    prepared_final = "准备工作已完成，等待主人选择职责。"

    class Prefix(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal child_id, rounds, assignment
            names = {tool["function"]["name"] for tool in tools}
            if self.role == "worker":
                turn = ModelTurn(content=prepared_final)
            else:
                assert self.role == "main"
                rounds += 1
                if rounds == 1:
                    assert "spawn_agent" in names
                    assert any(item.role == "user" and item.content == initial for item in messages)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-qq-anchor-child",
                                "spawn_agent",
                                task_name="QQ回复凭据队友",
                                prompt="核对已有要求后正常报告，等待主人决定后续职责。",
                            )
                        ]
                    )
                elif rounds == 2:
                    spawned = tool_result(messages, "spawn-qq-anchor-child")
                    assert "error" not in spawned
                    child_id = spawned["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "wait-qq-anchor-child", "wait_agents", targets=[child_id], timeout=2
                            )
                        ]
                    )
                elif rounds == 3:
                    waited = tool_result(messages, "wait-qq-anchor-child")
                    assert waited["timed_out"] is False
                    child = runtime.team.get(child_id)
                    assert child["parent_id"] == main_id and child["status"] == "completed"
                    assert child["result"] == prepared_final and child_id not in runtime.tasks
                    assert "channel_delegate" in names
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "assign-qq-anchor-child",
                                "channel_delegate",
                                action="assign",
                                channel="qq",
                                scope="private",
                                owner_id=owner_id,
                                agent_id=child_id,
                            )
                        ]
                    )
                else:
                    if rounds == 4:
                        assignment = tool_result(messages, "assign-qq-anchor-child")
                    turn = ModelTurn(content=prefix_final)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Prefix(config.model)

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
        assert mode.status_code == 200
        response = await web.post(
            f"/api/sessions/{main_id}/messages", json={"content": initial, "mode": "yolo"}
        )
        assert response.status_code == 200
        assert (
            await asyncio.wait_for(runtime.wait(response.json()["run_id"]), timeout=4)
            == prefix_final
        )
        await eventually(
            runtime, lambda: main_id not in runtime.tasks and child_id not in runtime.tasks
        )
        assert assignment is not None and "error" not in assignment
        assert assignment["agent_id"] == child_id and assignment["owner_id"] == owner_id
        assert assignment["account_id"] == account_id and assignment["state"] == "active"
        assert runtime.team.get(child_id)["generation"] == 1
        channels = app.state.channel_application
        assert channels.manager.status()["channels"]["qq"]["active"] is True
        assert http_calls == [] and channels.manager.deliveries() == []
        yield QQAssignment(
            runtime,
            channels,
            web,
            main_id,
            child_id,
            account_id,
            owner_id,
            secret,
            http_calls,
            sent,
        )


@pytest.mark.parametrize("variant", ["replace_reply_to", "remove_reply_to", "omit_route"])
async def test_owned_qq_reply_preserves_its_input_anchor_and_rejects_explicit_changes(
    qq_assignment: QQAssignment, variant: str
) -> None:
    case = qq_assignment
    runtime, manager = case.runtime, case.channels.manager
    incoming = "只使用本次QQ私聊实际消息的被动回复凭据，不借用别的消息。"
    message_id = "qq-anchor-current-message"
    valid_text = "这是使用原始消息凭据的明确回复。"
    followup_text = "该回复必须保留原始QQ消息凭据。"
    final = "省略路由由宿主沿用来源，显式变造或删除凭据应被拒绝。"
    rounds = 0
    results: dict[str, dict[str, Any]] = {}
    original: dict[str, Any] = {}
    proof: dict[str, Any] = {}
    before: dict[str, Any] = {}

    class Child(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds, original, proof, before
            rounds += 1
            assert "channel_send" in {tool["function"]["name"] for tool in tools}
            if rounds == 1:
                actual = [
                    item
                    for item in runtime.store.messages(case.child_id)
                    if item["role"] == "user" and item["content"] == incoming
                ]
                assert len(actual) == 1
                original = actual[0]
                run_id = runtime.run_ids[case.child_id]
                assert original["principal"] == "owner"
                assert original["source"] == f"channel:qq:{case.account_id}"
                assert original["run_id"] == run_id
                assert original["route"]["reply_to"] == message_id
                assert original["route"]["target"] == case.owner_id
                assert any(item.source_message_id == original["id"] for item in messages)
                current = runtime.channel_ownership.current_proof(case.child_id, run_id)
                assert current is not None
                proof = current
                assert proof["input_id"] == original["id"] and proof["run_id"] == run_id
                assert original["channel_ownership"] == {
                    key: value for key, value in proof.items() if key not in {"input_id", "run_id"}
                }
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "send-qq-real-anchor",
                            "channel_send",
                            channel="qq",
                            target=case.owner_id,
                            text=valid_text,
                            route=original["route"],
                            idempotency_key="qq-real-anchor",
                        )
                    ]
                )
            elif rounds == 2:
                results["valid"] = tool_result(messages, "send-qq-real-anchor")
                assert "error" not in results["valid"]
                await eventually(
                    runtime, lambda: manager.delivery(results["valid"]["id"])["state"] == "sent"
                )
                assert case.sent == [
                    {"msg_type": 0, "content": valid_text, "msg_id": message_id, "msg_seq": 1}
                ]
                before = case.effects()
                arguments: dict[str, Any] = {
                    "channel": "qq",
                    "target": case.owner_id,
                    "text": followup_text,
                    "idempotency_key": "qq-followup-anchor-" + variant,
                }
                if variant != "omit_route":
                    route = dict(original["route"])
                    if variant == "replace_reply_to":
                        route["reply_to"] = "qq-another-passive-message"
                    else:
                        route.pop("reply_to")
                    arguments["route"] = route
                turn = ModelTurn(
                    tool_calls=[call("send-qq-followup-anchor", "channel_send", **arguments)]
                )
            else:
                assert rounds == 3
                results["followup"] = tool_result(messages, "send-qq-followup-anchor")
                turn = ModelTurn(content=final)
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Child([], [])  # type: ignore[assignment]
    await case.receive(incoming, message_id=message_id)
    await eventually(
        runtime,
        lambda: (
            runtime.team.get(case.child_id)["generation"] == 2
            and runtime.team.get(case.child_id)["status"] == "completed"
        ),
    )
    assert rounds == 3 and set(results) == {"valid", "followup"}
    delivery = manager.delivery(results["valid"]["id"])
    assert delivery is not None and delivery["state"] == "sent"
    assert delivery["payload"]["channel_ownership"] == proof
    assert delivery["route"] == original["route"]
    assert runtime.store.get("run", proof["run_id"])["status"] == "completed"
    ingress = runtime.store.db.execute(
        "SELECT run_id,state FROM ingress WHERE id=?", (proof["input_id"],)
    ).fetchone()
    assert ingress is not None and tuple(ingress) == (proof["run_id"], "delivered")
    assert len(before["quota"]) == 1 and before["quota"][0][1] == "1"
    if variant == "omit_route":
        assert "error" not in results["followup"], (
            "受托队友省略route时，宿主应沿用本批真实私聊的完整回复凭据",
            results["followup"],
        )
        await eventually(
            runtime, lambda: manager.delivery(results["followup"]["id"])["state"] == "sent"
        )
        second = manager.delivery(results["followup"]["id"])
        assert second is not None and second["id"] != delivery["id"]
        assert second["route"] == original["route"]
        assert second["payload"]["channel_ownership"] == proof
        assert case.sent == [
            {"msg_type": 0, "content": valid_text, "msg_id": message_id, "msg_seq": 1},
            {"msg_type": 0, "content": followup_text, "msg_id": message_id, "msg_seq": 2},
        ]
        after = case.effects()
        assert len(manager.deliveries()) == len(after["outbox"]) == 2
        assert after["http"][:-1] == before["http"]
        assert after["quota"] == [(before["quota"][0][0], "2")]
    else:
        assert "error" in results["followup"], (
            "真实受托QQ输入已正控发送，但显式变造或删除的原消息凭据仍被接纳",
            variant,
            results["followup"],
        )
        assert case.effects() == before
        assert len(case.sent) == len(manager.deliveries()) == 1


async def test_web_main_can_still_send_qq_without_a_passive_reply_anchor(
    qq_assignment: QQAssignment,
) -> None:
    case = qq_assignment
    runtime, manager = case.runtime, case.channels.manager
    text = "这是网页主人明确要求主助手向已绑定私聊发送的新消息。"
    final = "主助手主动发送已按原合同入队。"
    receipt: dict[str, Any] = {}
    rounds = 0

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal receipt, rounds
            rounds += 1
            if rounds == 1:
                actual = next(
                    item
                    for item in runtime.store.messages(case.main_id)
                    if item["role"] == "user" and item["content"] == text
                )
                assert actual["source"] == "web" and actual["principal"] == "owner"
                assert actual["run_id"] == runtime.run_ids[case.main_id]
                assert any(item.source_message_id == actual["id"] for item in messages)
                assert "channel_send" in {tool["function"]["name"] for tool in tools}
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "web-main-active-qq-send",
                            "channel_send",
                            channel="qq",
                            target=case.owner_id,
                            text=text,
                            idempotency_key="web-main-active-qq-send",
                        )
                    ]
                )
            else:
                assert rounds == 2
                receipt = tool_result(messages, "web-main-active-qq-send")
                turn = ModelTurn(content=final)
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Main([], [])  # type: ignore[assignment]
    response = await case.web.post(
        f"/api/sessions/{case.main_id}/messages", json={"content": text, "mode": "yolo"}
    )
    assert response.status_code == 200
    assert await asyncio.wait_for(runtime.wait(response.json()["run_id"]), timeout=4) == final
    assert "error" not in receipt
    await eventually(runtime, lambda: manager.delivery(receipt["id"])["state"] == "sent")
    assert case.sent == [{"msg_type": 0, "content": text}]
    delivery = manager.delivery(receipt["id"])
    assert delivery is not None and "reply_to" not in delivery["route"]
    assert "channel_ownership" not in delivery["payload"]
    assert case.effects()["quota"] == []
    assert runtime.team.get(case.child_id)["generation"] == 1


async def test_owned_qq_dispatch_and_manual_retry_reject_a_tampered_reply_anchor(
    qq_assignment: QQAssignment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli.channels import ChannelError, FileContent

    case = qq_assignment
    runtime, manager = case.runtime, case.channels.manager
    incoming = "按本次QQ私聊原消息凭据回复文字和已保存附件。"
    message_id = "qq-anchor-dispatch-current-message"
    valid_text = "先用完整原始消息凭据确认本轮私聊可真实发送。"
    content = b"bounded QQ reply attachment"
    read, release = asyncio.Event(), asyncio.Event()
    dispatched: dict[str, dict[str, Any]] = {}
    original_delivery = manager.delivery
    original_file = manager._file

    def observed_delivery(identifier: str) -> dict[str, Any] | None:
        item = original_delivery(identifier)
        if (
            item is not None
            and item["state"] == "sending"
            and item["next_part"] == 0
            and item["payload"]["parts"][0].get("attachment_id") == attachment["id"]
        ):
            # Keep the claim's reread; later inspection returns fresh snapshots.
            dispatched.setdefault(identifier, item)
        return item

    async def held_file(identifier: str, name: str, *, media_mode: str = "file") -> FileContent:
        file = await original_file(identifier, name, media_mode=media_mode)
        assert identifier == attachment["id"] and name == "qq"
        assert file.data == content and file.media_mode == "file"
        read.set()
        await release.wait()
        return file

    async def chunks() -> AsyncIterator[bytes]:
        yield content

    attachment = await case.channels.attachments.save(chunks(), "qq-reply.txt", "text/plain")
    monkeypatch.setattr(manager, "delivery", observed_delivery)
    monkeypatch.setattr(manager, "_file", held_file)
    rounds = 0
    results: dict[str, dict[str, Any]] = {}
    original: dict[str, Any] = {}
    proof: dict[str, Any] = {}

    class Child(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds, original, proof
            rounds += 1
            assert "channel_send" in {tool["function"]["name"] for tool in tools}
            if rounds == 1:
                actual = [
                    item
                    for item in runtime.store.messages(case.child_id)
                    if item["role"] == "user" and item["content"] == incoming
                ]
                assert len(actual) == 1
                original = actual[0]
                run_id = runtime.run_ids[case.child_id]
                assert original["principal"] == "owner"
                assert original["source"] == f"channel:qq:{case.account_id}"
                assert original["run_id"] == run_id
                assert original["route"]["reply_to"] == message_id
                assert original["route"]["target"] == case.owner_id
                assert any(item.source_message_id == original["id"] for item in messages)
                current = runtime.channel_ownership.current_proof(case.child_id, run_id)
                assert current is not None
                proof = current
                assert proof["input_id"] == original["id"] and proof["run_id"] == run_id
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "qq-dispatch-original-text",
                            "channel_send",
                            channel="qq",
                            target=case.owner_id,
                            text=valid_text,
                            route=original["route"],
                            idempotency_key="qq-dispatch-original-text",
                        )
                    ]
                )
            elif rounds == 2:
                results["text"] = tool_result(messages, "qq-dispatch-original-text")
                assert "error" not in results["text"]
                await eventually(
                    runtime, lambda: manager.delivery(results["text"]["id"])["state"] == "sent"
                )
                assert case.sent == [
                    {"msg_type": 0, "content": valid_text, "msg_id": message_id, "msg_seq": 1}
                ]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "qq-dispatch-original-file",
                            "channel_send",
                            channel="qq",
                            target=case.owner_id,
                            attachment_ids=[attachment["id"]],
                            route=original["route"],
                            idempotency_key="qq-dispatch-original-file",
                        )
                    ]
                )
            else:
                assert rounds == 3
                results["file"] = tool_result(messages, "qq-dispatch-original-file")
                assert "error" not in results["file"]
                turn = ModelTurn(content="文字已送达，附件以投递记录的实际状态为准。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Child([], [])  # type: ignore[assignment]
    try:
        await case.receive(incoming, message_id=message_id)
        await asyncio.wait_for(read.wait(), timeout=3)
        await eventually(
            runtime,
            lambda: (
                "file" in results
                and runtime.team.get(case.child_id)["generation"] == 2
                and runtime.team.get(case.child_id)["status"] == "completed"
            ),
        )
        assert rounds == 3
        text = manager.delivery(results["text"]["id"])
        assert text is not None and text["state"] == "sent"
        assert text["route"] == original["route"]
        assert text["payload"]["channel_ownership"] == proof
        file_id = results["file"]["id"]
        queued = manager.delivery(file_id)
        assert queued is not None and queued["state"] == "sending"
        assert queued["route"] == original["route"]
        assert queued["payload"]["channel_ownership"] == proof
        assert queued["payload"]["parts"] == [{"attachment_id": attachment["id"], "sequence": 1}]
        assert queued["next_part"] == 0 and queued["receipts"] == []
        in_flight = dispatched[file_id]
        assert (in_flight is queued) is False
        assert in_flight["state"] == "sending" and in_flight["next_part"] == 0
        assert in_flight["route"] == queued["route"]
        assert in_flight["payload"] == queued["payload"]
        runtime.channel_ownership.guard(proof, session_id=case.child_id)
        before = case.effects()
        assert len(before["outbox"]) == 2
        assert len(before["quota"]) == 1 and before["quota"][0][1] == "2"

        # Keep the real stored input and proof intact. Fault-inject only the
        # queued anchor after its first guard and actual file validation.
        route = {**queued["route"], "reply_to": "qq-anchor-dispatch-another-message"}
        in_flight["route"] = route
        with runtime.store.db:
            runtime.store.db.execute(
                "UPDATE channel_outbox SET route=? WHERE id=?", (json.dumps(route), file_id)
            )
        release.set()
        await eventually(runtime, lambda: manager.delivery(file_id)["state"] == "blocked")
        blocked = manager.delivery(file_id)
        assert blocked is not None and blocked["route"] == route
        assert blocked["payload"] == queued["payload"]
        assert blocked["payload"]["channel_ownership"] == proof
        assert blocked["next_part"] == 0 and blocked["receipts"] == []
        assert blocked["attempts"] == 1 and "回复凭据" in blocked["last_error"]
        actual = runtime.store.get("message", proof["input_id"])
        assert actual is not None and actual["route"] == original["route"]
        assert actual["run_id"] == proof["run_id"]
        runtime.channel_ownership.guard(proof, session_id=case.child_id)
        after = case.effects()
        assert after["http"] == before["http"]
        assert after["quota"] == before["quota"]
        assert case.sent == [
            {"msg_type": 0, "content": valid_text, "msg_id": message_id, "msg_seq": 1}
        ]
        with pytest.raises(ChannelError, match="回复凭据"):
            await manager.retry(file_id)
        assert case.effects() == after
    finally:
        release.set()
