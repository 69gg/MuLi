"""A cancelled, unclaimed owner input remains readable through a real tool round."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_history_runtime import history_runtime
from test_channel_ownership_policy import assigned, receive
from test_channels import onebot_body
from test_mobile_channels import onebot_payload
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.providers import Message, ModelTurn, StreamEvent
from muli.server import create_app


async def test_main_reads_cancelled_unclaimed_owner_ingress_without_replaying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set downloads and smaller preserved pages before the real assignment;
    # both original UTF-8 pages must fit the unchanged context budget.
    async with assigned(tmp_path, monkeypatch, receive_files=True, max_output_chars=2000) as (
        runtime,
        app,
        web,
        child_id,
    ):
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        main_id = runtime.main_session()["id"]
        first_text = "第一封：请保留资料上下文，等待主人决定下一步。"
        second_text = (
            "第二封：只核查这份原文和附件引用，不执行。\n"
            + "未交给模型的中文附件说明，请保留原样。\n" * 80
            + "原文结束。"
        )
        attachment_bytes = "这是已接收但未交给模型的中文附件原件。\n".encode()
        network: list[tuple[str, str]] = []
        worker_inputs: list[str] = []
        receipts: dict[str, dict[str, Any]] = {}
        pages: list[dict[str, Any]] = []
        history_schema: dict[str, Any] = {}
        before_read: dict[str, Any] = {}
        main_rounds = 0
        discovered_id = ""
        attachment_id = ""

        async def public_url(url: str) -> tuple[httpx.URL, str, str]:
            parsed = httpx.URL(url)
            assert parsed.host == "cdn.invalid" and parsed.path == "/queued-owner.txt"
            return parsed, parsed.host, parsed.host

        async def platform(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            network.append((request.method, str(request.url)))
            if request.url.host == "onebot.invalid":
                assert request.method == "POST" and request.url.path == "/get_private_file_url"
                assert json.loads(request.content) == {"file_id": "queued-owner-file"}
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "retcode": 0,
                        "data": {"url": "https://cdn.invalid/queued-owner.txt"},
                    },
                )
            assert request.method == "GET" and request.url.host == "cdn.invalid"
            assert request.url.path == "/queued-owner.txt"
            return httpx.Response(200, content=attachment_bytes)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", platform)
        monkeypatch.setattr("muli.channels.http.public_url", public_url)

        def business_snapshot() -> dict[str, Any]:
            """Exclude the expected main run's tool messages and lifecycle writes."""
            db = runtime.store.db
            return {
                "child_ingress": [
                    tuple(row)
                    for row in db.execute(
                        "SELECT * FROM ingress WHERE session_id=? ORDER BY seq", (child_id,)
                    )
                ],
                "business_objects": [
                    tuple(row)
                    for row in db.execute(
                        "SELECT kind,id,data,updated FROM objects WHERE kind IN "
                        "('agent','attachment','channel_ownership','team_report') ORDER BY kind,id"
                    )
                ],
                "child_messages": runtime.store.messages(child_id),
                "child_runs": [
                    tuple(row)
                    for row in db.execute(
                        "SELECT id,data FROM objects WHERE kind='run' "
                        "AND json_extract(data,'$.session_id')=? ORDER BY id",
                        (child_id,),
                    )
                ],
                "run_ids": [
                    row[0]
                    for row in db.execute("SELECT id FROM objects WHERE kind='run' ORDER BY id")
                ],
                "inbound": [
                    tuple(row) for row in db.execute("SELECT * FROM channel_inbound ORDER BY id")
                ],
                "outbox": [
                    tuple(row) for row in db.execute("SELECT * FROM channel_outbox ORDER BY id")
                ],
                "attachment_bytes": app.state.attachments.path(attachment_id).read_bytes(),
                "network": list(network),
                "worker_inputs": list(worker_inputs),
            }

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal main_rounds, discovered_id, before_read, history_schema
                assert all(not message.images for message in messages)
                if self.role == "worker":
                    current = next(item for item in reversed(messages) if item.role == "user")
                    assert current.content.endswith(first_text) and current.source_message_id
                    worker_inputs.append(current.source_message_id)
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise
                    raise AssertionError("首条真实主人消息应被 reclaim 取消，不能继续模型回合")

                assert self.role == "main"
                main_rounds += 1
                if main_rounds == 1:
                    history_schema = next(
                        item["function"]["parameters"]
                        for item in tools
                        if item["function"]["name"] == "channel_history"
                    )
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "reclaim-exact-private",
                                "channel_delegate",
                                action="reclaim",
                                channel="onebot",
                                scope="private",
                                owner_id="7",
                            )
                        ]
                    )
                elif main_rounds == 2:
                    receipts["reclaim"] = tool_result(messages, "reclaim-exact-private")
                    before_read = business_snapshot()
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "cancelled-ingress-directory",
                                "channel_history",
                                section="ingress",
                                channel="onebot",
                                owner_id="7",
                                state="cancelled",
                                limit=20,
                            )
                        ]
                    )
                elif main_rounds == 3:
                    directory = tool_result(messages, "cancelled-ingress-directory")
                    receipts["directory"] = directory
                    candidates = directory.get("items", [])
                    item = next(
                        (
                            item
                            for item in candidates
                            if item.get("state") == "cancelled"
                            and item.get("session_id") == child_id
                        ),
                        None,
                    )
                    # Missing capability still finishes a real tool/model round;
                    # assertions below identify it after checking the safety facts.
                    if item is None:
                        turn = ModelTurn(content="本地核查工具回合结束。")
                    else:
                        discovered_id = item["input_id"]
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "cancelled-ingress-body-first",
                                    "channel_history",
                                    section="ingress",
                                    channel="onebot",
                                    owner_id="7",
                                    input_id=discovered_id,
                                )
                            ]
                        )
                elif main_rounds == 4:
                    first = tool_result(messages, "cancelled-ingress-body-first")
                    pages.append(first)
                    if "error" in first or not first.get("has_more"):
                        turn = ModelTurn(content="本地核查工具回合结束。")
                    else:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "cancelled-ingress-body-next",
                                    "channel_history",
                                    section="ingress",
                                    channel="onebot",
                                    owner_id="7",
                                    input_id=discovered_id,
                                    offset_bytes=first["next_offset_bytes"],
                                    revision=first["revision"],
                                )
                            ]
                        )
                else:
                    assert main_rounds == 5
                    pages.append(tool_result(messages, "cancelled-ingress-body-next"))
                    turn = ModelTurn(content="本地核查工具回合结束。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            await receive(web, first_text, number=501)
            await asyncio.wait_for(entered.wait(), timeout=4)
            assert runtime.team.get(child_id)["generation"] == 2
            first_id = worker_inputs[0]
            first_row = dict(
                runtime.store.db.execute("SELECT * FROM ingress WHERE id=?", (first_id,)).fetchone()
            )
            assert first_row["state"] == "delivered" and first_row["delivered_seq"] is not None

            payload = onebot_payload(second_text)
            payload["message_id"] = 502
            payload["message"].append(
                {
                    "type": "file",
                    "data": {"file_id": "queued-owner-file", "name": "排队原件.txt"},
                }
            )
            raw, headers = onebot_body(payload)
            headers["Content-Type"] = "application/json"
            response = await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            pending = runtime.conversation.pending(child_id)
            assert len(pending) == 1 and pending[0]["content"] == second_text
            second_id = pending[0]["id"]
            assert len(pending[0]["attachment_ids"]) == 1
            attachment_id = pending[0]["attachment_ids"][0]
            second_row = dict(
                runtime.store.db.execute(
                    "SELECT * FROM ingress WHERE id=?", (second_id,)
                ).fetchone()
            )
            assert second_row["state"] == "pending" and second_row["delivered_seq"] is None
            assert second_row["run_id"] is None and not cancelled.is_set()
            assert runtime.store.get("message", second_id) is None
            assert (
                runtime.store.db.execute(
                    "SELECT 1 FROM message_order WHERE message_id=?", (second_id,)
                ).fetchone()
                is None
            )
            queued = next(
                event
                for event in runtime.store.events(session_id=child_id)
                if event["type"] == "message.queued" and event["data"]["message"]["id"] == second_id
            )
            assert queued["data"]["message"]["content"] == second_text
            assert queued["data"]["message"]["attachment_ids"] == [attachment_id]
            attachment = app.state.attachments.get(attachment_id)
            assert attachment["sha256"] == hashlib.sha256(attachment_bytes).hexdigest()
            assert app.state.attachments.path(attachment_id).read_bytes() == attachment_bytes

            accepted = await runtime.submit(
                "准确收回 OneBot 主人7的私聊，然后只核查已接收入站的原文与附件引用；不重放、不外发。"
            )
            await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4)
            assert runtime.store.get("run", accepted["run_id"])["status"] == "completed"
            assert receipts["reclaim"]["state"] == "main"
            assert receipts["reclaim"]["owner_id"] == "7" and cancelled.is_set()
            assert runtime.team.get(child_id)["generation"] == 2
            assert runtime.team.get(child_id)["status"] == "cancelled"
            assert runtime.channel_ownership.destination(
                {
                    "name": "onebot",
                    "account_id": "123",
                    "kind": "private",
                    "sender_id": "7",
                    "target": "7",
                }
            ) == (main_id, None)
            assert (
                dict(
                    runtime.store.db.execute(
                        "SELECT * FROM ingress WHERE id=?", (first_id,)
                    ).fetchone()
                )
                == first_row
            )
            assert dict(
                runtime.store.db.execute(
                    "SELECT * FROM ingress WHERE id=?", (second_id,)
                ).fetchone()
            ) == {**second_row, "state": "cancelled"}
            assert runtime.store.get("message", second_id) is None
            assert (
                runtime.store.db.execute(
                    "SELECT 1 FROM message_order WHERE message_id=?", (second_id,)
                ).fetchone()
                is None
            )
            assert queued in runtime.store.events(session_id=child_id)

            # A genuine platform retry keeps the cancelled original and cannot
            # download again, revive the worker, or start another main run.
            retried = await web.post("/api/channels/onebot/webhook", content=raw, headers=headers)
            assert retried.status_code == 200
            assert business_snapshot() == before_read
            assert worker_inputs == [first_id] and runtime.store.pending_inputs(child_id) == 0
            assert runtime.store.pending_inputs(main_id) == 0
            assert network == [
                ("POST", "https://onebot.invalid/get_private_file_url"),
                ("GET", "https://cdn.invalid/queued-owner.txt"),
            ]
            assert app.state.channel_application.manager.deliveries() == []

            assert "section" in history_schema["properties"], (
                "真实分配、模型屏障、pending 原件和准确收回均已完成；channel_history 缺少 ingress section",
                receipts["directory"],
            )
            directory = receipts["directory"]
            assert "error" not in directory, directory
            assert directory["section"] == "ingress" and directory["coverage"] == "accepted_by_muli"
            assert directory["source"] == "local" and directory["consistency"] == "bounded_live"
            assert directory["main_session_id"] == main_id
            assert directory["channel"] == "onebot" and directory["account_id"] == "123"
            assert directory["owner_id"] == "7"
            assert discovered_id == second_id
            assert len(directory["items"]) == 1
            item = directory["items"][0]
            assert item["input_id"] == second_id and item["session_id"] == child_id
            assert item["state"] == "cancelled" and item["delivered_message_seq"] is None
            assert item["ingress_seq"] == second_row["seq"]
            assert item["attachment_ids"] == [attachment_id] and "content" not in item
            assert main_rounds == 5 and len(pages) == 2
            assert all("error" not in page for page in pages), pages
            assert pages[0]["has_more"] is True and pages[1]["has_more"] is False
            assert pages[0]["offset_bytes"] == 0
            assert pages[1]["offset_bytes"] == pages[0]["next_offset_bytes"]
            assert pages[0]["revision"] == pages[1]["revision"]
            assert "".join(page["content"] for page in pages) == second_text
            for page in pages:
                assert page["input_id"] == second_id and page["state"] == "cancelled"
                assert page["attachment_ids"] == [attachment_id]
                assert page["length_bytes"] == len(page["content"].encode("utf-8"))
                assert page["total_bytes"] == len(second_text.encode("utf-8"))
            encoded = json.dumps([directory, *pages], ensure_ascii=False)
            assert all(key not in encoded for key in ("context_token", '"route"', '"lease"'))
        finally:
            release.set()


@pytest.mark.parametrize("boundary", ["disabled", "unbound", "changed_account"])
async def test_reopened_runtime_continues_original_ingress_and_rechecks_current_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    # Reuse the proven real assignment/pending/cancellation prefix without
    # manufacturing a session, ingress proof or tool-execution context.
    await test_main_reads_cancelled_unclaimed_owner_ingress_without_replaying_it(
        tmp_path, monkeypatch
    )
    original, _ = make_runtime(tmp_path, [])
    main_id = original.main_session()["id"]
    instance = original.store.instance_id
    saved: dict[str, Any] = {}
    rounds = 0

    class SavePosition(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "before-reopen-directory",
                            "channel_history",
                            section="ingress",
                            channel="onebot",
                            owner_id="7",
                            limit=1,
                        )
                    ]
                )
            elif rounds == 2:
                saved["directory"] = tool_result(messages, "before-reopen-directory")
                item = saved["directory"]["items"][0]
                assert item["state"] == "cancelled"
                saved["input_id"] = item["input_id"]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "before-reopen-body",
                            "channel_history",
                            section="ingress",
                            channel="onebot",
                            owner_id="7",
                            input_id=saved["input_id"],
                        )
                    ]
                )
            else:
                assert rounds == 3
                saved["body"] = tool_result(messages, "before-reopen-body")
                turn = ModelTurn(content="保存核查位置。")
            yield StreamEvent("done", turn=turn)

    original.client_factory = lambda _config: SavePosition([], [])
    app = create_app(original.config, original.data_dir, original)
    async with app.router.lifespan_context(app):
        query = await original.submit("只读保存入站目录和原文字节位置。")
        await asyncio.wait_for(original.wait(query["run_id"]), timeout=4)
        assert saved["directory"]["has_more"] is True
        assert saved["body"]["has_more"] is True
        saved["raw"] = original.store.db.execute(
            "SELECT payload FROM ingress WHERE id=?", (saved["input_id"],)
        ).fetchone()[0]

    # New ConfigFile, Store, Runtime and ChannelManager, after normal full close.
    reopened, _ = make_runtime(tmp_path, [])
    assert reopened is not original and reopened.store is not original.store
    assert reopened.config is not original.config and reopened.store.instance_id == instance
    results: dict[str, dict[str, Any]] = {}
    phase, rounds = "resume", 0

    class Continue(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "after-reopen-directory",
                            "channel_history",
                            section="ingress",
                            channel="onebot",
                            owner_id="7",
                            cursor=saved["directory"]["next_cursor"],
                        ),
                        call(
                            "after-reopen-body",
                            "channel_history",
                            section="ingress",
                            channel="onebot",
                            owner_id="7",
                            input_id=saved["input_id"],
                            offset_bytes=saved["body"]["next_offset_bytes"],
                            revision=saved["body"]["revision"],
                        ),
                    ]
                )
            else:
                assert rounds == 2
                results[phase + "-directory"] = tool_result(messages, "after-reopen-directory")
                results[phase + "-body"] = tool_result(messages, "after-reopen-body")
                turn = ModelTurn(content="按当前核查回执结束。")
            yield StreamEvent("done", turn=turn)

    reopened.client_factory = lambda _config: Continue([], [])
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    async with reopened_app.router.lifespan_context(reopened_app):
        assert (
            reopened_app.state.channel_application.manager
            is not app.state.channel_application.manager
        )
        query = await reopened.submit("按已保存的位置继续只读核查。")
        await asyncio.wait_for(reopened.wait(query["run_id"]), timeout=4)
        directory, next_body = results["resume-directory"], results["resume-body"]
        assert "error" not in directory and directory["items"]
        assert all(item["input_id"] != saved["input_id"] for item in directory["items"])
        assert "error" not in next_body and next_body["has_more"] is False
        assert next_body["state"] == "cancelled"
        assert next_body["revision"] == saved["body"]["revision"]
        assert (
            saved["body"]["content"] + next_body["content"] == json.loads(saved["raw"])["content"]
        )
        assert reopened.store.get("message", saved["input_id"]) is None
        assert reopened.main_session()["id"] == main_id
        config_change: dict[str, Any] = {
            "disabled": {"enabled": False},
            "unbound": {"owner_ids": ["8"]},
            "changed_account": {"account_id": "456"},
        }[boundary]
        reopened.config.update({"channels": {"onebot": config_change}})
        await reopened_app.state.channel_application.reconfigure()
        phase, rounds = "revoked", 0
        query = await reopened.submit("再次用旧位置查询，必须遵守当前绑定。")
        await asyncio.wait_for(reopened.wait(query["run_id"]), timeout=4)
        for result in (results["revoked-directory"], results["revoked-body"]):
            assert result.get("error") and "items" not in result and "content" not in result
        assert (
            reopened.store.db.execute(
                "SELECT payload FROM ingress WHERE id=?", (saved["input_id"],)
            ).fetchone()[0]
            == saved["raw"]
        )
        assert reopened_app.state.channel_application.manager.deliveries() == []


async def test_reclaim_while_real_read_permission_waits_delivers_no_old_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with assigned(tmp_path, monkeypatch, owner_ids=["7", "8"]) as (
        runtime,
        app,
        web,
        child_id,
    ):
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        first_text = "主人8的原件，允许当前受托队友按既有只读权限核查。"
        next_text = "主人7要求查询另一已绑定主人的本地记录。"
        worker_rounds, main_rounds = 0, 0
        earlier: dict[str, Any] = {}
        delivered_results: list[dict[str, Any]] = []
        handler_calls: list[dict[str, Any]] = []
        original_check = runtime.permissions.check
        original_history = runtime.tools["channel_history"].handler

        async def checked(*args: Any, **kwargs: Any) -> None:
            await original_check(*args, **kwargs)
            if args[1] == "channel_history" and args[4] == child_id and worker_rounds == 3:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        async def history(arguments: dict[str, Any]) -> dict[str, Any]:
            handler_calls.append(dict(arguments))
            return await original_history(arguments)

        monkeypatch.setattr(runtime.permissions, "check", checked)
        monkeypatch.setattr(runtime.tools["channel_history"], "handler", history)

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal worker_rounds, main_rounds
                if self.role == "worker":
                    current = next(item for item in reversed(messages) if item.role == "user")
                    if current.content.endswith(first_text):
                        turn = ModelTurn(content="主人8的原件仅在网页处理。")
                    else:
                        assert current.content.endswith(next_text)
                        worker_rounds += 1
                        if worker_rounds == 1:
                            turn = ModelTurn(
                                tool_calls=[
                                    call(
                                        "active-cross-owner-directory",
                                        "channel_history",
                                        section="ingress",
                                        channel="onebot",
                                        owner_id="8",
                                    )
                                ]
                            )
                        else:
                            assert worker_rounds in {2, 3}
                            prior = (
                                "active-cross-owner-directory"
                                if worker_rounds == 2
                                else "active-cross-owner-body"
                            )
                            result = tool_result(messages, prior)
                            delivered_results.append(result)
                            if worker_rounds == 2:
                                earlier["id"] = result["items"][0]["input_id"]
                            turn = ModelTurn(
                                tool_calls=[
                                    call(
                                        "active-cross-owner-body"
                                        if worker_rounds == 2
                                        else "read-after-revoke",
                                        "channel_history",
                                        section="ingress",
                                        channel="onebot",
                                        owner_id="8",
                                        input_id=earlier["id"],
                                    )
                                ]
                            )
                else:
                    main_rounds += 1
                    if main_rounds == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "revoke-waiting-reader",
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
                        receipt = tool_result(messages, "revoke-waiting-reader")
                        assert receipt["state"] == "main" and receipt["owner_id"] == "7"
                        turn = ModelTurn(content="旧读取已停止。")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            await receive(web, first_text, owner_id="8", number=601)
            await eventually(
                runtime,
                lambda: (
                    runtime.team.get(child_id)["status"] == "completed"
                    and runtime.team.get(child_id)["generation"] == 2
                ),
            )
            await receive(web, next_text, number=602)
            await asyncio.wait_for(entered.wait(), timeout=4)
            old_run = runtime.run_ids[child_id]
            assert len(delivered_results) == 2 and len(handler_calls) == 2
            assert delivered_results[0]["owner_id"] == "8"
            assert delivered_results[1]["content"] == first_text
            original_payload = runtime.store.db.execute(
                "SELECT payload FROM ingress WHERE id=?", (earlier["id"],)
            ).fetchone()[0]
            accepted = await runtime.submit("只收回主人7私聊，撤销尚未派发的读取。")
            await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4)
            assert cancelled.is_set() and worker_rounds == 3 and len(handler_calls) == 2
            assert runtime.store.get("run", old_run)["status"] == "cancelled"
            assert runtime.team.get(child_id)["generation"] == 3
            interrupted = [
                item
                for item in runtime.store.messages(child_id)
                if item.get("tool_call_id") == "read-after-revoke"
            ]
            # Protocol repair records uncertainty; it is not a dispatched
            # history result and never reaches a further worker model round.
            assert len(interrupted) == 1 and interrupted[0]["interrupted"] is True
            unknown = json.loads(interrupted[0]["content"])
            assert unknown["status"] == "not_executed" and set(unknown) == {"status", "reason"}
            assert len(delivered_results) == 2
            assert not any(
                event["type"] in {"tool.started", "tool.completed"}
                and event["data"].get("call_id") == "read-after-revoke"
                for event in runtime.store.events(session_id=child_id)
            )
            assert (
                runtime.store.db.execute(
                    "SELECT payload FROM ingress WHERE id=?", (earlier["id"],)
                ).fetchone()[0]
                == original_payload
            )
            assert app.state.channel_application.manager.deliveries() == []
        finally:
            release.set()


async def test_delivered_ingress_does_not_claim_the_provider_received_a_failed_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("此场景只允许本地 Runtime 和模型替身")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime = await history_runtime(tmp_path)
    # Suppress optional failure notifications so they cannot start a new run.
    runtime.config.update(
        {"system_events": {"enabled": False}, "channels": {"weixin": {"enabled": False}}}
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    source = "领取归档成功，但还没准备好模型请求的中文原件。"
    provider_calls = 0
    original_prepare = runtime._prepare_request

    async def failed_prepare(session_id: str, **kwargs: Any) -> Any:
        assert session_id == main_id
        raise ValueError("离线验证：领取后准备请求失败")

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "claimed-directory",
                            "channel_history",
                            section="ingress",
                            channel="onebot",
                            owner_id="7",
                            state="delivered",
                        ),
                        call(
                            "claimed-body",
                            "channel_history",
                            section="ingress",
                            channel="onebot",
                            owner_id="7",
                            input_id=original["id"],
                        ),
                    ]
                )
            else:
                assert provider_calls == 2
                results["directory"] = tool_result(messages, "claimed-directory")
                results["body"] = tool_result(messages, "claimed-body")
                turn = ModelTurn(content="只按入站状态核查。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Reader([], [])
    results: dict[str, dict[str, Any]] = {}
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as web,
    ):
        mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
        assert mode.status_code == 200
        monkeypatch.setattr(runtime, "_prepare_request", failed_prepare)
        await receive(web, source, number=701)
        failed_run = runtime.run_ids[main_id]
        with pytest.raises(ValueError, match="领取后准备请求失败"):
            await asyncio.wait_for(runtime.wait(failed_run), timeout=4)
        assert provider_calls == 0 and runtime.store.get("run", failed_run)["status"] == "failed"
        original = next(
            item for item in runtime.store.messages(main_id) if item["content"] == source
        )
        original_row = dict(
            runtime.store.db.execute(
                "SELECT * FROM ingress WHERE id=?", (original["id"],)
            ).fetchone()
        )
        assert original_row["state"] == "delivered"
        assert original_row["delivered_seq"] == original["seq"]
        monkeypatch.setattr(runtime, "_prepare_request", original_prepare)
        accepted = await runtime.submit("只读核查刚才入站的状态与原文。")
        await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4)
        assert provider_calls == 2
        item = results["directory"]["items"][0]
        assert item["input_id"] == original["id"] and item["state"] == "delivered"
        assert item["delivered_message_seq"] == original["seq"]
        assert results["body"]["state"] == "delivered" and results["body"]["content"] == source
        assert (
            dict(
                runtime.store.db.execute(
                    "SELECT * FROM ingress WHERE id=?", (original["id"],)
                ).fetchone()
            )
            == original_row
        )
        assert app.state.channel_application.manager.deliveries() == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"state": "all"},
        {"input_id": "unknown"},
        {"section": "ingress", "direction": "incoming"},
        {"section": "ingress", "delivery_id": "unknown"},
        {"section": "ingress", "part": 0},
        {"section": "ingress", "offset_bytes": 0},
        {"section": "ingress", "input_id": "unknown", "state": "all"},
        {"section": "ingress", "input_id": "unknown", "limit": 1},
    ],
)
async def test_real_history_tool_rejects_mixed_ingress_and_archive_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("参数拒绝不应访问平台或供应商")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime = await history_runtime(tmp_path)
    runtime.config.update({"channels": {"weixin": {"enabled": False}}})
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    rounds = 0
    results: list[dict[str, Any]] = []

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "mixed-ingress-arguments",
                            "channel_history",
                            channel="onebot",
                            owner_id="7",
                            **arguments,
                        )
                    ]
                )
            else:
                assert rounds == 2
                results.append(tool_result(messages, "mixed-ingress-arguments"))
                turn = ModelTurn(content="按参数拒绝回执结束。")
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _config: Reader([], [])
    app = create_app(runtime.config, runtime.data_dir, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as web,
    ):
        mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
        assert mode.status_code == 200
        accepted = await runtime.submit("只读核查参数，不执行外发或恢复。")
        await asyncio.wait_for(runtime.wait(accepted["run_id"]), timeout=4)
        assert len(results) == 1 and results[0].get("error")
        assert "items" not in results[0] and "content" not in results[0]
        assert app.state.channel_application.manager.deliveries() == []
