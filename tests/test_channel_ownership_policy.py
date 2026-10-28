"""Ownership is exercised by real drivers; only device HTTP and waits are controlled."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_ownership_runtime import complete_assignment
from test_channels import onebot_body
from test_mobile_channels import onebot_payload
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.permissions import Mode
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


@asynccontextmanager
async def assigned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: Mode = "yolo",
    owner_ids: list[str] | None = None,
    receive_files: bool = False,
    max_output_chars: int = 6000,
) -> AsyncIterator[tuple[Runtime, Any, httpx.AsyncClient, str]]:
    await complete_assignment(
        tmp_path,
        monkeypatch,
        owner_mode=mode,
        owner_ids=owner_ids,
        receive_files=receive_files,
        max_output_chars=max_output_chars,
    )
    runtime, _ = make_runtime(tmp_path, [])
    child = runtime.team.list()[0]
    assert child["status"] == "completed" and child["generation"] == 1
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        yield runtime, app, web, child["id"]


async def receive(
    web: httpx.AsyncClient, text: str, *, owner_id: str = "7", number: int = 201
) -> None:
    payload = onebot_payload(text)
    payload["message_id"] = number
    payload["user_id"] = int(owner_id)
    raw, headers = onebot_body(payload)
    headers["Content-Type"] = "application/json"
    response = await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
    assert response.status_code == 200


async def test_child_return_completes_locally_and_pairs_remaining_calls_without_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        forbidden = tmp_path / "after-return.txt"
        calls = 0

        class Client(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal calls
                calls += 1
                assert runtime.run_ids[child_id] and "channel_delegate" in {
                    item["function"]["name"] for item in tools
                }
                assert "fs_write" in {item["function"]["name"] for item in tools}
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[
                            call(
                                "return-owned-channel",
                                "channel_delegate",
                                action="return",
                                channel="onebot",
                                scope="account",
                            ),
                            call(
                                "after-return-effect",
                                "fs_write",
                                path=str(forbidden),
                                content="不得写入",
                            ),
                        ]
                    ),
                )

        runtime.client_factory = lambda _config: Client([], [])  # type: ignore[assignment]
        main_id = runtime.main_session()["id"]
        await receive(web, "请将接入归还主助手，归还后不得再执行本批操作。")
        await eventually(runtime, lambda: runtime.team.get(child_id)["status"] == "completed")
        agent = runtime.team.get(child_id)
        assert calls == 1 and agent["generation"] == 2
        assert agent["result"] == "渠道已归还主助手。" and not forbidden.exists()
        messages = runtime.store.messages(child_id)
        results = {item["tool_call_id"]: item for item in messages if item.get("tool_call_id")}
        returned = json.loads(results["return-owned-channel"]["content"])
        skipped = json.loads(results["after-return-effect"]["content"])
        assert returned["state"] == "main" and "error" not in returned
        assert skipped["status"] == "not_executed"
        report = runtime.store.get("team_report", agent["run_id"])
        assert (
            report is not None
            and report["status"] == "local"
            and report["execution_status"] == "completed"
        )
        assert runtime.store.pending_inputs(main_id) == 0 and main_id not in runtime.tasks
        assert app.state.channel_application.manager.deliveries() == []


async def test_reclaim_cancels_the_actual_child_approval_before_a_late_decision_can_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch, mode="ask") as (runtime, _app, web, child_id):
        forbidden = tmp_path / "unapproved-child-write.txt"
        main_id = runtime.main_session()["id"]
        main_rounds = 0

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal main_rounds
                if self.role == "worker":
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "held-owner-write",
                                "fs_write",
                                path=str(forbidden),
                                content="不得写入",
                            )
                        ]
                    )
                else:
                    main_rounds += 1
                    if main_rounds == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "approved-reclaim",
                                    "channel_delegate",
                                    action="reclaim",
                                    channel="onebot",
                                    scope="account",
                                )
                            ]
                        )
                    else:
                        assert main_rounds == 2
                        receipt = tool_result(messages, "approved-reclaim")
                        assert "error" not in receipt and receipt["state"] == "main"
                        turn = ModelTurn(content="接入已收回，旧审批不再有效。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        await receive(web, "请写下这份资料，但仍需我审批。")
        await eventually(runtime, lambda: bool(runtime.live_approvals(child_id)))
        old = runtime.live_approvals(child_id)[0]
        assert old["tool"] == "fs_write" and old["run_id"] == runtime.run_ids[child_id]
        assert not forbidden.exists()
        owner = await runtime.submit("现在强制收回接入，撤销队友尚未执行的操作。")
        await eventually(runtime, lambda: bool(runtime.live_approvals(main_id)))
        reclaim = runtime.live_approvals(main_id)[0]
        assert reclaim["tool"] == "channel_delegate"
        await runtime.permissions.resolve(reclaim["id"], "approve")
        assert (
            await asyncio.wait_for(runtime.wait(owner["run_id"]), timeout=4)
            == "接入已收回，旧审批不再有效。"
        )
        assert runtime.store.get("approval", old["id"])["status"] == "cancelled"
        with pytest.raises(ValueError):
            await runtime.permissions.resolve(old["id"], "approve")
        assert not forbidden.exists() and runtime.live_approvals(child_id) == []


async def test_reclaim_during_real_attachment_read_blocks_dispatch_after_bytes_are_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        read, release = asyncio.Event(), asyncio.Event()
        http_calls = 0

        async def platform(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            nonlocal http_calls
            http_calls += 1
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": 1}}
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", platform)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"complete owned attachment"

        attachment = await app.state.attachments.save(chunks(), "owned.txt", "text/plain")
        manager = app.state.channel_application.manager
        original = manager._file

        async def held_file(identifier: str, name: str, *, media_mode: str = "file") -> Any:
            result = await original(identifier, name, media_mode=media_mode)
            assert result.data == b"complete owned attachment"
            read.set()
            await release.wait()
            return result

        monkeypatch.setattr(manager, "_file", held_file)
        rounds: dict[str, int] = {}
        delivery_id = ""

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal delivery_id
                rounds[self.role] = rounds.get(self.role, 0) + 1
                if self.role == "worker":
                    if rounds[self.role] == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "owned-file-send",
                                    "channel_send",
                                    channel="onebot",
                                    target="7",
                                    attachment_ids=[attachment["id"]],
                                    idempotency_key="held-owned-file",
                                )
                            ]
                        )
                    else:
                        assert rounds[self.role] == 2
                        receipt = tool_result(messages, "owned-file-send")
                        assert "error" not in receipt
                        delivery_id = receipt["id"]
                        turn = ModelTurn(content="附件已进入队列，未冒称送达。")
                elif rounds[self.role] == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "file-reclaim",
                                "channel_delegate",
                                action="reclaim",
                                channel="onebot",
                                scope="account",
                            )
                        ]
                    )
                else:
                    assert (
                        rounds[self.role] == 2
                        and tool_result(messages, "file-reclaim")["state"] == "main"
                    )
                    turn = ModelTurn(content="已撤销旧接入资格。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            await receive(web, "请将这份已保存资料显式发送给主人。")
            await asyncio.wait_for(read.wait(), timeout=3)
            await eventually(
                runtime,
                lambda: bool(delivery_id) and runtime.team.get(child_id)["status"] == "completed",
            )
            accepted = await runtime.submit("收回接入，文件虽已读取但不得继续外发。")
            assert (
                await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4)
                == "已撤销旧接入资格。"
            )
            release.set()
            await eventually(runtime, lambda: manager.delivery(delivery_id)["state"] == "blocked")
            delivery = manager.delivery(delivery_id)
            assert delivery["next_part"] == 0 and delivery["receipts"] == [] and http_calls == 0
        finally:
            release.set()


async def test_private_reclaim_stops_owner_seven_but_keeps_owner_eight_pending_for_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch, owner_ids=["7", "8"]) as (
        runtime,
        app,
        web,
        child_id,
    ):
        entered, release = asyncio.Event(), asyncio.Event()
        main_rounds = 0
        main_id = runtime.main_session()["id"]
        owner_seven, owner_eight = "主人7的正在处理消息。", "主人8的排队消息不可被别人的收回取消。"
        eight_id = ""
        seen: list[str] = []

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal main_rounds
                if self.role == "worker":
                    current = next(item for item in reversed(messages) if item.role == "user")
                    if current.content.endswith(owner_seven):
                        seen.append("7")
                        entered.set()
                        await release.wait()
                        raise AssertionError("被收回的7旧轮应取消，不能产生迟到回复")
                    assert (
                        current.content.endswith(owner_eight)
                        and current.source_message_id == eight_id
                    )
                    stored = runtime.store.get("message", eight_id)
                    assert (
                        stored["route"]["sender_id"] == "8"
                        and stored["run_id"] == runtime.run_ids[child_id]
                    )
                    seen.append("8")
                    turn = ModelTurn(content="主人8的原消息仍由原受托队友处理。")
                else:
                    main_rounds += 1
                    if main_rounds == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "reclaim-seven-only",
                                    "channel_delegate",
                                    action="reclaim",
                                    channel="onebot",
                                    scope="private",
                                    owner_id="7",
                                )
                            ]
                        )
                    else:
                        assert main_rounds == 2
                        result = tool_result(messages, "reclaim-seven-only")
                        assert (
                            "error" not in result
                            and result["owner_id"] == "7"
                            and result["state"] == "main"
                        )
                        turn = ModelTurn(content="仅主人7已收回。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            await receive(web, owner_seven)
            await asyncio.wait_for(entered.wait(), timeout=3)
            await receive(web, owner_eight, owner_id="8", number=202)
            pending = runtime.conversation.pending(child_id)
            assert len(pending) == 1 and pending[0]["content"] == owner_eight
            eight_id = pending[0]["id"]
            assert runtime.team.get(child_id)["generation"] == 2
            accepted = await runtime.submit("只收回主人7的私聊，8的接管不变。")
            assert (
                await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4)
                == "仅主人7已收回。"
            )
            await eventually(
                runtime,
                lambda: (
                    runtime.team.get(child_id)["generation"] == 3
                    and runtime.team.get(child_id)["status"] == "completed"
                ),
            )
            assert seen == ["7", "8"]
            assert (
                runtime.store.db.execute(
                    "SELECT state FROM ingress WHERE id=?", (eight_id,)
                ).fetchone()[0]
                == "delivered"
            )
            child = runtime.team.get(child_id)
            report = runtime.store.get("team_report", child["run_id"])
            assert (
                report is not None
                and report["status"] == "local"
                and report["reply_origin"]["input_id"] == eight_id
            )
            assert runtime.channel_ownership.destination(
                {
                    "name": "onebot",
                    "account_id": "123",
                    "kind": "private",
                    "sender_id": "7",
                    "target": "7",
                }
            ) == (main_id, None)
            target, proof = runtime.channel_ownership.destination(
                {
                    "name": "onebot",
                    "account_id": "123",
                    "kind": "private",
                    "sender_id": "8",
                    "target": "8",
                }
            )
            assert target == child_id and proof is not None
            assert app.state.channel_application.manager.deliveries() == []
        finally:
            release.set()


async def test_request_return_wakes_the_idle_child_without_authorizing_other_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, app, _web, child_id):
        forbidden = tmp_path / "control-notification-is-not-an-owner.txt"
        main_id = runtime.main_session()["id"]
        main_rounds, child_rounds = 0, 0

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal main_rounds, child_rounds
                if self.role == "worker":
                    child_rounds += 1
                    assert child_rounds == 1
                    actual = runtime.store.messages(child_id)[-1]
                    assert actual["role"] == "user" and actual["principal"] == "agent:" + main_id
                    assert actual["source"] == "channel-ownership" and actual["route"] is None
                    assert actual["channel_ownership_control"] is True
                    assert actual["run_id"] == runtime.run_ids[child_id]
                    assert any(item.source_message_id == actual["id"] for item in messages)
                    names = {item["function"]["name"] for item in tools}
                    assert {"channel_delegate", "fs_read"} <= names
                    assert not names & {
                        "fs_write",
                        "channel_send",
                        "spawn_agent",
                        "send_input",
                        "workflow_create",
                    }
                    yield StreamEvent(
                        "done",
                        turn=ModelTurn(
                            tool_calls=[
                                call(
                                    "control-cannot-reclaim",
                                    "channel_delegate",
                                    action="reclaim",
                                    channel="onebot",
                                    scope="account",
                                ),
                                call(
                                    "control-cannot-write",
                                    "fs_write",
                                    path=str(forbidden),
                                    content="不可写入",
                                ),
                                call(
                                    "control-actual-return",
                                    "channel_delegate",
                                    action="return",
                                    channel="onebot",
                                    scope="account",
                                ),
                            ]
                        ),
                    )
                else:
                    main_rounds += 1
                    if main_rounds == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "notify-idle-child",
                                    "channel_delegate",
                                    action="request_return",
                                    channel="onebot",
                                    scope="account",
                                )
                            ]
                        )
                    else:
                        assert main_rounds == 2
                        result = tool_result(messages, "notify-idle-child")
                        assert "error" not in result and result["state"] == "return_requested"
                        turn = ModelTurn(content="已请求队友归还，结果以真实状态为准。")
                    yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        accepted = await runtime.submit("请让空闲的接管队友归还接入。")
        assert (
            await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4)
            == "已请求队友归还，结果以真实状态为准。"
        )
        await eventually(
            runtime,
            lambda: (
                runtime.team.get(child_id)["generation"] == 2
                and runtime.team.get(child_id)["status"] == "completed"
            ),
        )
        child = runtime.team.get(child_id)
        assert child["result"] == "渠道已归还主助手。" and child_rounds == 1
        results = {
            item["tool_call_id"]: json.loads(item["content"])
            for item in runtime.store.messages(child_id)
            if item.get("tool_call_id")
        }
        assert (
            "error" in results["control-cannot-reclaim"]
            and "error" in results["control-cannot-write"]
        )
        assert results["control-actual-return"]["state"] == "main" and not forbidden.exists()
        report = runtime.store.get("team_report", child["run_id"])
        assert report is not None and report["status"] == "local"
        assert (
            runtime.store.pending_inputs(main_id) == 0
            and app.state.channel_application.manager.deliveries() == []
        )


async def test_owned_batch_cannot_select_another_bound_owner_at_enqueue_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch, owner_ids=["7", "8"]) as (
        runtime,
        app,
        web,
        child_id,
    ):
        assert runtime.config.settings.channels.onebot.owner_ids == ["7", "8"]
        read, release = asyncio.Event(), asyncio.Event()
        sent: list[dict[str, Any]] = []
        manager = app.state.channel_application.manager
        dispatched: dict[str, dict[str, Any]] = {}
        original_send = manager._send_item

        async def observed_send(item: dict[str, Any]) -> None:
            dispatched[item["id"]] = item
            await original_send(item)

        monkeypatch.setattr(manager, "_send_item", observed_send)

        async def platform(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            value = json.loads(request.content)
            assert request.url.path == "/send_private_msg" and value["user_id"] == "7"
            sent.append(value)
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(sent)}}
            )

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", platform)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"bounded saved copy"

        attachment = await app.state.attachments.save(chunks(), "scope.txt", "text/plain")
        original = manager._file

        async def held_file(identifier: str, name: str, *, media_mode: str = "file") -> Any:
            value = await original(identifier, name, media_mode=media_mode)
            assert value.data == b"bounded saved copy"
            read.set()
            await release.wait()
            return value

        monkeypatch.setattr(manager, "_file", held_file)
        rounds = 0
        text_id, file_id = "", ""

        class Client(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal rounds, text_id, file_id
                rounds += 1
                if rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "wrong-owner-send",
                                "channel_send",
                                channel="onebot",
                                target="8",
                                text="不得给8",
                                idempotency_key="wrong-owner",
                            ),
                            call(
                                "same-batch-owner-send",
                                "channel_send",
                                channel="onebot",
                                target="7",
                                text="只给本批7",
                                idempotency_key="same-owner",
                            ),
                        ]
                    )
                elif rounds == 2:
                    assert "error" in tool_result(messages, "wrong-owner-send")
                    valid = tool_result(messages, "same-batch-owner-send")
                    assert "error" not in valid
                    text_id = valid["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "same-owner-file",
                                "channel_send",
                                channel="onebot",
                                target="7",
                                attachment_ids=[attachment["id"]],
                                idempotency_key="same-owner-file",
                            )
                        ]
                    )
                else:
                    assert rounds == 3
                    valid = tool_result(messages, "same-owner-file")
                    assert "error" not in valid
                    file_id = valid["id"]
                    turn = ModelTurn(content="仅发送到本批主人，另一个绑定主人不是本批目的地。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _config: Client([], [])  # type: ignore[assignment]
        try:
            await receive(web, "本轮来自主人7，不能改投给另一个主人8。")
            await asyncio.wait_for(read.wait(), timeout=3)
            await eventually(
                runtime,
                lambda: bool(file_id) and runtime.team.get(child_id)["status"] == "completed",
            )
            assert len(sent) == 1 and manager.delivery(text_id)["state"] == "sent"
            assert len(manager.deliveries()) == 2
            actual = manager.delivery(file_id)
            assert actual["route"]["target"] == actual["route"]["sender_id"] == "7"
            assert actual["payload"]["channel_ownership"]["input_id"]
            # Fault-inject a different chat into the actual in-flight item after
            # the first fence and real file read. No receipt or proof is invented.
            route = {**actual["route"], "target": "8", "sender_id": "8"}
            dispatched[file_id]["route"] = route
            with runtime.store.db:
                runtime.store.db.execute(
                    "UPDATE channel_outbox SET route=? WHERE id=?", (json.dumps(route), file_id)
                )
            release.set()
            await eventually(runtime, lambda: manager.delivery(file_id)["state"] == "blocked")
            assert len(sent) == 1 and manager.delivery(file_id)["receipts"] == []
        finally:
            release.set()


async def test_web_child_message_cannot_bypass_the_owned_ingress_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch) as (runtime, _app, web, child_id):
        before_agent = runtime.team.get(child_id)
        before_lease = runtime.channel_ownership.for_agent(child_id)
        before_messages = runtime.store.messages(child_id)
        before_ingress = [
            tuple(row)
            for row in runtime.store.db.execute(
                "SELECT * FROM ingress WHERE session_id=? ORDER BY seq", (child_id,)
            )
        ]
        before_runs = [
            tuple(row)
            for row in runtime.store.db.execute(
                "SELECT id,data FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=? ORDER BY rowid",
                (child_id,),
            )
        ]
        response = await web.post(
            f"/api/sessions/{child_id}/messages",
            json={"content": "绕过接管渠道另派普通任务", "mode": "yolo"},
        )
        assert 400 <= response.status_code < 500
        assert runtime.team.get(child_id) == before_agent
        assert runtime.channel_ownership.for_agent(child_id) == before_lease
        assert runtime.store.messages(child_id) == before_messages
        assert [
            tuple(row)
            for row in runtime.store.db.execute(
                "SELECT * FROM ingress WHERE session_id=? ORDER BY seq", (child_id,)
            )
        ] == before_ingress
        assert [
            tuple(row)
            for row in runtime.store.db.execute(
                "SELECT id,data FROM objects WHERE kind='run' AND json_extract(data,'$.session_id')=? ORDER BY rowid",
                (child_id,),
            )
        ] == before_runs
        assert child_id not in runtime.tasks and runtime.store.pending_inputs(child_id) == 0
