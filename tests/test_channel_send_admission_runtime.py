"""Real Runtime admission, with actual attachment reads and offline transport.

The only held operation is an already-validated voice read. No request, driver,
execution ContextVar, inbound receipt or outbox row is manufactured.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_voice import MP3, audio
from test_channels import terminal
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.channel_application import ChannelApplication
from muli.channels import FileContent, IncomingMessage
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool, parameters


@dataclass
class SendCase:
    runtime: Runtime
    channels: ChannelApplication
    attachment_id: str
    requests: list[str]


@pytest.fixture
async def send_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SendCase]:
    requests: list[str] = []

    async def transport(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        requests.append(request.url.path)
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "offline"}}
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"enabled": False, "auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7", "8"],
                }
            },
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    channels: ChannelApplication = app.state.channel_application
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as web:
            response = await web.patch(
                f"/api/sessions/{runtime.main_session()['id']}", json={"mode": "yolo"}
            )
            assert response.status_code == 200 and response.json()["mode"] == "yolo"
        identifier = await audio(app.state.attachments)
        await channels.start()
        yield SendCase(runtime, channels, identifier, requests)
    finally:
        try:
            await channels.close()
        finally:
            await runtime.close()


def outbox_count(runtime: Runtime) -> int:
    return int(runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0])


async def phone(case: SendCase, event_id: str, *, sender: str = "7") -> dict[str, Any]:
    return await case.channels.manager.receive(
        "onebot", IncomingMessage(event_id, sender, sender, "请核对当前发送。", reply_to=event_id)
    )


async def finish(runtime: Runtime) -> None:
    identifier = runtime.main_session()["id"]
    task = runtime.tasks.get(identifier)
    if task is not None:
        async with asyncio.timeout(2):
            await task
    runs = [item for item in runtime.store.list("run") if item["session_id"] == identifier]
    assert runs and all(item["status"] == "completed" for item in runs), {
        "statuses": [item["status"] for item in runs]
    }


@pytest.mark.parametrize("change", ["revoke", "owner_input", "configuration_aba"])
async def test_voice_read_rechecks_admission_before_any_enqueue(
    send_case: SendCase, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    runtime, channels = send_case.runtime, send_case.channels
    manager = channels.manager
    generation = channels._generation
    original_config = runtime.config.settings.channels.model_dump()
    entered, release = asyncio.Event(), asyncio.Event()
    results: list[dict[str, Any]] = []
    rounds: list[int] = []
    reads: list[FileContent] = []
    original_file = manager._file

    async def held_file(identifier: str, name: str, *, media_mode: str = "file") -> FileContent:
        # Delegate all size/hash/media checks before holding the real await.
        file = await original_file(identifier, name, media_mode=media_mode)
        assert identifier == send_case.attachment_id and name == "onebot"
        assert file.data == MP3 and file.media_mode == "voice"
        caller = runtime.main_session()["id"]
        assert runtime.tasks[caller] is asyncio.current_task()
        assert runtime._tool_execution.get() == (caller, runtime.run_ids[caller])
        prepared = runtime.request_context(caller)
        assert prepared is not None and prepared.reply_origin.kind == "channel"
        assert prepared.reply_origin.delivery_routes()[0]["target"] == "7"
        assert runtime.session_tools(caller)["channel_send"].risk == "external"
        reads.append(file)
        entered.set()
        async with asyncio.timeout(2):
            await release.wait()
        return file

    monkeypatch.setattr(manager, "_file", held_file)

    class Client(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            round_number = len(rounds) + 1
            rounds.append(round_number)
            assert round_number <= 2
            if round_number == 1:
                assert any(item["function"]["name"] == "channel_send" for item in tools)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "voice-send",
                            "channel_send",
                            channel="onebot",
                            target="7",
                            attachment_ids=[send_case.attachment_id],
                            media_mode="voice",
                            idempotency_key="voice-before-change",
                        )
                    ]
                )
            else:
                result = tool_result(messages, "voice-send")
                assert "error" in result and "id" not in result
                results.append(result)
                turn = ModelTurn(content="旧发送已停止，等待重新核对。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model == "main"
        return Client()

    runtime.client_factory = factory
    try:
        await phone(send_case, "voice-origin")
        async with asyncio.timeout(2):
            await entered.wait()
        assert len(reads) == 1 and outbox_count(runtime) == 0
        assert send_case.requests == []
        if change == "owner_input":
            await phone(send_case, "new-owner-input", sender="8")
            assert runtime.store.pending_inputs(runtime.main_session()["id"]) == 1
        else:
            runtime.config.update({"channels": {"onebot": {"owner_ids": ["8"]}}})
            if change == "configuration_aba":
                await channels.reconfigure()
                runtime.config.update({"channels": original_config})
                await channels.reconfigure()
                assert runtime.config.settings.channels.model_dump() == original_config
                assert channels.manager is not manager and channels._generation > generation
    finally:
        release.set()
    await finish(runtime)
    assert len(results) == 1 and rounds == [1, 2] and len(reads) == 1
    assert outbox_count(runtime) == 0 and send_case.requests == []
    accepted = [
        item
        for item in runtime.store.messages(runtime.main_session()["id"])
        if item.get("tool_call_id") == "voice-send" and item["role"] == "tool"
    ]
    assert len(accepted) == 1 and accepted[0]["content_provenance"] == "accepted"


async def test_copied_task_and_retained_handler_cannot_borrow_the_real_execution(
    send_case: SendCase,
) -> None:
    runtime, channels = send_case.runtime, send_case.channels
    retained: list[Tool] = []
    deliveries: list[str] = []
    errors: list[Exception] = []
    arguments: dict[str, Any] = {
        "channel": "onebot",
        "target": "7",
        "text": "真实当前工具发送",
        "idempotency_key": "current",
    }

    async def probe(args: dict[str, Any]) -> dict[str, bool]:
        try:
            caller = runtime.tool_session_id
            assert caller is not None
            assert caller == runtime.main_session()["id"]
            assert runtime.tasks[caller] is asyncio.current_task()
            execution = runtime._tool_execution.get()
            assert execution == (caller, runtime.run_ids[caller])
            prepared = runtime.request_context(caller)
            assert prepared is not None and prepared.reply_origin.kind == "channel"
            tool = runtime.session_tools(caller)["channel_send"]
            assert tool.risk == "external"
            retained.append(tool)

            async def copied() -> None:
                # create_task really copies the active execution ContextVars.
                assert runtime._tool_execution.get() == execution
                assert runtime.request_context(caller) is prepared
                assert runtime.tasks[caller] is not asyncio.current_task()
                with pytest.raises(PermissionError):
                    await tool.handler({**arguments, "idempotency_key": "copied"})

            async with asyncio.timeout(2):
                await asyncio.create_task(copied())
            assert outbox_count(runtime) == 0 and send_case.requests == []
            result = await tool.handler(arguments)
            assert isinstance(result, dict) and isinstance(result.get("id"), str)
            deliveries.append(result["id"])
            return {"checked": True}
        except Exception as error:
            errors.append(error)
            raise

    runtime.add_tool_provider(
        lambda current: [
            Tool("send_admission_probe", "核对真实发送执行身份", parameters({}), probe, "write")
        ],
        replace=True,
        coordinator=True,
    )
    turns = [
        ModelTurn(tool_calls=[call("probe", "send_admission_probe")]),
        ModelTurn(content="仅当前真实工具发送一次。"),
    ]

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert any(item["function"]["name"] == "send_admission_probe" for item in tools)
            if len(self.turns) == 1:
                assert tool_result(messages, "probe") == {"checked": True}
            async for event in super().stream(messages, tools):
                yield event

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model == "main"
        return Client(turns, [])

    runtime.client_factory = factory
    await phone(send_case, "execution-origin")
    await finish(runtime)
    if errors:
        raise errors[0]
    assert len(retained) == len(deliveries) == 1 and outbox_count(runtime) == 1
    assert (await terminal(channels.manager, deliveries[0]))["state"] == "sent"
    assert send_case.requests == ["/send_private_msg"]
    with pytest.raises(PermissionError):
        await retained[0].handler({**arguments, "idempotency_key": "retained"})
    assert outbox_count(runtime) == 1 and send_case.requests == ["/send_private_msg"]
