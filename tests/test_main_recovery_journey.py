"""A bound phone inspects a real Web run's durable unfinished snapshot.

SQLite backup captures the committed in-flight boundary; this is not an OS-kill
or power-loss experiment. The original Runtime is then cleaned up normally.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_continuity import attachment_settings, platform_reply
from test_channels import terminal
from test_mobile_task_control_followup import delivery_text, phone
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.config import ConfigFile
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool, parameters


async def test_bound_phone_reads_recovered_main_input_and_saved_results_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "网页旧请求：核查合成回执，暂不发布，也不要重做结果未知的步骤。"
    question = "上次你自己做到哪了？先查原请求和回执，不要继续旧执行，只回复这个私聊。"
    explanation = "已核对上次原请求和已保存回执；一项有记录，另一项结果未知，未重做或发布旧成果。"
    saved_result = {"checkpoint": "合成核查记录已保存", "side_effects_verified": False}
    sent: list[dict[str, Any]] = []
    old_calls = {"saved": 0, "unknown": 0}
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return platform_reply(request, sent)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    async def save_once(arguments: dict[str, Any]) -> dict[str, Any]:
        assert arguments == {}
        old_calls["saved"] += 1
        return saved_result

    async def unfinished_once(arguments: dict[str, Any]) -> dict[str, Any]:
        assert arguments == {}
        old_calls["unknown"] += 1
        entered.set()
        try:
            await release.wait()
        finally:
            cancelled.set()
        return {"state": "only-an-explicit-release-could-complete-this-fixture"}

    def original_tools(_settings: Any) -> list[Tool]:
        return [
            Tool("save_before_boundary", "受控合成回执", parameters({}), save_once),
            Tool("unfinished_before_boundary", "受控未完边界", parameters({}), unfinished_once),
        ]

    first, _ = make_runtime(tmp_path, [])
    first.config.update(attachment_settings(tmp_path, auto_extract=False))
    first.config.update(
        {
            "default_mode": "yolo",
            "persona": {"prompt": "只核对合成资料，保留未知结果。"},
            "memory": {"enabled": False},
        }
    )
    await first.reconfigure()
    first.add_tool_provider(original_tools)
    main_id, instance = first.main_session()["id"], first.store.instance_id
    config_path = first.config.path
    assert config_path is not None
    source_rounds = 0

    class Source(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal source_rounds
            source_rounds += 1
            if source_rounds == 1:
                assert messages[-1].role == "user" and messages[-1].content == original
                turn = ModelTurn(tool_calls=[call("saved-old-call", "save_before_boundary")])
            else:
                assert source_rounds == 2
                assert tool_result(messages, "saved-old-call") == saved_result
                turn = ModelTurn(
                    tool_calls=[call("unknown-old-call", "unfinished_before_boundary")]
                )
            yield StreamEvent("done", turn=turn)

    first.client_factory = lambda _: Source([], [])  # type: ignore[assignment]
    app = create_app(first.config, first.data_dir, first)
    snapshot = tmp_path / "unfinished-snapshot"
    snapshot.mkdir()
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            accepted = await web.post(
                f"/api/sessions/{main_id}/messages", json={"content": original, "mode": "yolo"}
            )
            assert accepted.status_code == 200
            receipt = accepted.json()
            old_run, old_input = receipt["run_id"], receipt["message"]["id"]
            await asyncio.wait_for(entered.wait(), 4)
            assert source_rounds == 2 and old_calls == {"saved": 1, "unknown": 1}
            assert first.store.get("run", old_run)["status"] == "running"
            archived = first.store.get("message", old_input)
            assert archived is not None and archived["run_id"] == old_run
            assert archived["source"] == "web" and archived["principal"] == "owner"
            known = next(
                item
                for item in first.store.messages(main_id)
                if item.get("tool_call_id") == "saved-old-call"
            )
            assert known["content_provenance"] == "accepted"
            assert json.loads(known["content"]) == saved_result
            assert not any(
                item.get("tool_call_id") == "unknown-old-call"
                for item in first.store.messages(main_id)
            )
            assert first.live_approvals() == [] and sent == []
            assert not first.store.db.in_transaction
            with closing(sqlite3.connect(snapshot / "muli.sqlite3")) as destination:
                first.store.db.backup(destination)
        assert cancelled.is_set()
    finally:
        await first.close()
        release.set()

    current_runs: list[str] = []
    catalog: dict[str, Any] = {}
    read_original: dict[str, Any] = {}
    directory: dict[str, Any] = {}
    read_known: dict[str, Any] = {}
    read_unknown: dict[str, Any] = {}
    send_receipt: dict[str, Any] = {}
    rounds = 0

    class Reader(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            assert rounds <= 6
            if rounds == 1:
                current = runtime.run_ids[main_id]
                assert current != old_run
                current_runs.append(current)
                incoming = next(
                    item
                    for item in messages
                    if item.role == "user" and item.content.endswith(question)
                )
                header, body = incoming.content.split("\n", 1)
                assert body == question
                assert json.loads(header.split("：", 1)[1]) == {
                    "channel": "onebot",
                    "account_id": "123",
                    "sender_id": "8",
                    "target": "8",
                    "kind": "private",
                    "input_id": incoming.source_message_id,
                }
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "discover-main-snapshot",
                            "conversation_read",
                            session_id=main_id,
                            section="main_recovery",
                            limit=100,
                        )
                    ]
                )
            elif rounds == 2:
                catalog.update(tool_result(messages, "discover-main-snapshot"))
                assert "error" not in catalog, catalog
                matches = [item for item in catalog["items"] if item["run_id"] == old_run]
                assert (
                    len(matches) == 1 and matches[0]["first_input"]["availability"] == "available"
                )
                first_input = matches[0]["first_input"]
                assert (
                    first_input["scope"] == "start_only" and first_input["message_id"] == old_input
                )
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "read-old-web-input",
                            "conversation_read",
                            **first_input["read_arguments"],
                        )
                    ]
                )
            elif rounds == 3:
                read_original.update(tool_result(messages, "read-old-web-input"))
                assert read_original["content"] == original
                assert read_original["source"] == "web" and read_original["principal"] == "owner"
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "locate-saved-results",
                            "conversation_read",
                            session_id=main_id,
                            after=read_original["seq"],
                            limit=20,
                        )
                    ]
                )
            elif rounds == 4:
                directory.update(tool_result(messages, "locate-saved-results"))
                assert "error" not in directory
                known_record = next(
                    item for item in directory["records"] if item["id"] == known["id"]
                )
                unknown_record = next(
                    item
                    for item in directory["records"]
                    if item["role"] == "tool"
                    and json.loads(item["content"]).get("status") == "unknown"
                )
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "read-saved-known",
                            "conversation_read",
                            session_id=main_id,
                            message_id=known_record["id"],
                            revision=known_record["revision"],
                        ),
                        call(
                            "read-saved-unknown",
                            "conversation_read",
                            session_id=main_id,
                            message_id=unknown_record["id"],
                            revision=unknown_record["revision"],
                        ),
                    ]
                )
            elif rounds == 5:
                read_known.update(tool_result(messages, "read-saved-known"))
                read_unknown.update(tool_result(messages, "read-saved-unknown"))
                assert json.loads(read_known["content"]) == saved_result
                assert json.loads(read_unknown["content"])["status"] == "unknown"
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "reply-current-phone",
                            "channel_send",
                            channel="onebot",
                            target="8",
                            text=explanation,
                            idempotency_key="recovered-main-phone-explanation",
                        )
                    ]
                )
            else:
                send_receipt.update(tool_result(messages, "reply-current-phone"))
                assert "error" not in send_receipt
                turn = ModelTurn(content=explanation)
            yield StreamEvent("done", turn=turn)

    runtime = Runtime(
        ConfigFile(config_path),
        snapshot,
        client_factory=lambda _: Reader([], []),  # type: ignore[arg-type]
    )
    runtime.add_tool_provider(original_tools)
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        assert runtime.main_session()["id"] == main_id and runtime.store.instance_id == instance
        assert runtime.store.get("run", old_run)["status"] == "interrupted"
        assert runtime.store.get("message", known["id"]) == known
        assert runtime.store.get("message", old_input) == archived
        recovered = next(
            event
            for event in runtime.store.events(session_id=main_id)
            if event["type"] == "session.recovered"
            and event["data"]["kind"] == "run"
            and event["data"]["run_id"] == old_run
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert not runtime.tasks and old_calls == {"saved": 1, "unknown": 1}
            manager = app.state.channel_application.manager
            assert manager.deliveries() == [] and sent == []
            assert (await phone(web, question, 31201, sender=8)).status_code == 200
            await eventually(runtime, lambda: bool(completed(runtime, explanation)))
            assert len(current_runs) == 1
            assert await asyncio.wait_for(runtime.wait(current_runs[0]), 4) == explanation
            delivery = await terminal(manager, send_receipt["id"])
            assert delivery["state"] == "sent" and delivery_text(delivery) == explanation
            assert len(sent) == 1 and sent[0]["user_id"] == "8"
            assert sent[0]["message"] == [{"type": "text", "data": {"text": explanation}}]
            assert rounds == 6 and old_calls == {"saved": 1, "unknown": 1}
            assert runtime.store.get("run", old_run)["status"] == "interrupted"
            assert runtime.store.get("run", current_runs[0])["status"] == "completed"
            old_ingress = runtime.store.db.execute(
                "SELECT state,run_id,delivered_seq FROM ingress WHERE id=?", (old_input,)
            ).fetchone()
            assert tuple(old_ingress) == ("delivered", old_run, archived["seq"])
            unknown = [
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "unknown-old-call"
            ]
            assert len(unknown) == 1 and json.loads(unknown[0]["content"])["status"] == "unknown"
            current_input = runtime.store.db.execute(
                "SELECT delivered_seq FROM ingress WHERE run_id=? AND state='delivered'",
                (current_runs[0],),
            ).fetchone()
            assert current_input is not None
            assert known["seq"] < unknown[0]["seq"] < current_input["delivered_seq"]
            assert runtime.store.get("message", known["id"]) == known
            assert runtime.live_approvals() == []
            later_tools = [
                event["data"]["name"]
                for event in runtime.store.events(after=recovered["id"], session_id=main_id)
                if event["type"] == "tool.started"
            ]
            assert set(later_tools) == {"conversation_read", "channel_send"}
    finally:
        await runtime.close()
