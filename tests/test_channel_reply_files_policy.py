"""Reply-file admission through actual Runtime tool executions, with offline transport.

The trusted probe runs inside the real driver and retains its real tool table.
It never manufactures a PreparedRequest, execution ContextVar, run or origin.
The separate channel journey owns platform-sent and close/reopen evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.channel_application import ChannelApplication
from muli.channels import IncomingMessage
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool, parameters

Probe = Callable[[str, Tool], Awaitable[None]]
Case = tuple[Runtime, ChannelApplication, list[str]]


@pytest.fixture
async def reply_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Case]:
    hold_telegram_poll = asyncio.Event()

    async def transport(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST"
        if request.url.path.endswith("/getUpdates"):
            await hold_telegram_poll.wait()
            raise AssertionError("正常关闭应取消 Telegram 长轮询")
        assert request.url.path in {"/upload_private_file", "/send_private_msg"}
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
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7", "8"],
                    "allowed_groups": ["99"],
                }
            },
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    channels: ChannelApplication = app.state.channel_application
    identifiers: list[str] = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as web:
            response = await web.patch(
                f"/api/sessions/{runtime.main_session()['id']}", json={"mode": "yolo"}
            )
            assert response.status_code == 200 and response.json()["mode"] == "yolo"
        assert runtime.main_session()["mode"] == "yolo"
        for index in range(2):

            async def chunks(
                value: bytes = f"fixture-copy-{index}".encode(),
            ) -> AsyncIterator[bytes]:
                yield value

            item = await app.state.attachments.save(chunks(), f"copy-{index}.txt", "text/plain")
            identifiers.append(item["id"])
        await channels.start()
        yield runtime, channels, identifiers
    finally:
        try:
            await channels.close()
        finally:
            await runtime.close()


def outbox(runtime: Runtime) -> list[tuple[Any, ...]]:
    # Delivery workers may update state concurrently; only immutable content is
    # relevant to an admission failure and these fixtures have a tiny outbox.
    return [
        tuple(row)
        for row in runtime.store.db.execute(
            "SELECT id,name,route,payload FROM channel_outbox ORDER BY id"
        )
    ]


def arguments(identifier: str, key: str = "same-logical-send") -> dict[str, Any]:
    return {"attachment_ids": [identifier], "idempotency_key": key}


async def rejected(runtime: Runtime, tool: Tool, args: dict[str, Any]) -> None:
    before = outbox(runtime)
    with pytest.raises((PermissionError, ValueError)):
        await tool.handler(args)
    assert outbox(runtime) == before


def install_probe(runtime: Runtime, probe: Probe) -> list[Exception]:
    errors: list[Exception] = []

    async def handler(args: dict[str, Any]) -> dict[str, bool]:
        caller = runtime.tool_session_id
        assert caller is not None
        assert runtime.tasks[caller] is asyncio.current_task()
        assert runtime._tool_execution.get() == (caller, runtime.run_ids[caller])
        assert runtime.request_context(caller) is not None
        table = runtime.session_tools(caller)
        tool = table["channel_reply_files"]
        assert tool.risk == "external"
        try:
            await probe(caller, tool)
        except Exception as exc:
            errors.append(exc)
            raise
        return {"checked": True}

    runtime.add_tool_provider(
        lambda current: [
            Tool("reply_policy_probe", "真实回复附件边界", parameters({}), handler, "write")
        ],
        replace=True,
    )

    class Client(FakeClient):
        def __init__(self) -> None:
            super().__init__([], [])
            self.rounds = 0

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.rounds += 1
            assert self.rounds <= 4
            if self.rounds == 1:
                assert any(item["function"]["name"] == "channel_reply_files" for item in tools)
                turn = ModelTurn(tool_calls=[call("probe", "reply_policy_probe")])
            else:
                assert tool_result(messages, "probe") == {"checked": True}
                # No incidental assistant text delivery can obscure the file
                # outbox oracle. This is still a complete real model turn.
                turn = ModelTurn()
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "worker"}
        return Client()

    runtime.client_factory = factory
    return errors


async def finish(runtime: Runtime, identifier: str, errors: list[Exception]) -> None:
    task = runtime.tasks.get(identifier)
    if task is not None:
        async with asyncio.timeout(2):
            await task
    if errors:
        raise errors[0]
    runs = [item for item in runtime.store.list("run") if item["session_id"] == identifier]
    assert runs and all(item["status"] == "completed" for item in runs), {
        "run_statuses": [item["status"] for item in runs]
    }


async def phone(case: Case, event_id: str, *, reply_to: str | None = None) -> None:
    runtime, channels, _ = case
    await channels.manager.receive(
        "onebot",
        IncomingMessage(
            event_id, "7", "7", "请把这份附件回复给我。", reply_to=reply_to or event_id
        ),
    )
    assert runtime.main_session()["mode"] == "yolo"


async def positive(case: Case, tool: Tool, key: str = "same-logical-send") -> dict[str, Any]:
    runtime, channels, identifiers = case
    request = runtime.request_context(runtime.main_session()["id"])
    assert request is not None and request.reply_origin.kind == "channel"
    result = await tool.handler(arguments(identifiers[0], key))
    assert isinstance(result, dict) and isinstance(result.get("id"), str)
    saved = channels.manager.delivery(result["id"])
    assert saved is not None and saved["route"] == request.reply_origin.delivery_routes()[0]
    assert saved["payload"]["parts"] == [{"attachment_id": identifiers[0]}]
    assert saved["route"]["target"] == "7"
    return saved


async def test_current_main_reply_does_not_authorize_copied_or_retained_context(
    reply_case: Case,
) -> None:
    runtime, _, identifiers = reply_case
    retained: list[Tool] = []

    async def probe(caller: str, tool: Tool) -> None:
        assert caller == runtime.main_session()["id"]
        await positive(reply_case, tool)
        retained.append(tool)
        # A child Task inherits ContextVars, but is not the real main driver.
        await asyncio.create_task(rejected(runtime, tool, arguments(identifiers[0], "copied")))

    errors = install_probe(runtime, probe)
    await phone(reply_case, "current-origin")
    await finish(runtime, runtime.main_session()["id"], errors)
    assert len(retained) == 1
    await rejected(runtime, retained[0], arguments(identifiers[0], "retained"))


async def test_real_worker_cannot_borrow_its_inherited_phone_origin(reply_case: Case) -> None:
    runtime, _, identifiers = reply_case
    worker_ids: list[str] = []
    worker_checks: list[str] = []

    async def probe(caller: str, tool: Tool) -> None:
        if caller == runtime.main_session()["id"]:
            await positive(reply_case, tool)
            child = await runtime.team.spawn(
                "附件边界队友", "只验证原路入口不可借用。", parent_id=caller
            )
            worker_ids.append(child["id"])
            # spawn schedules Team._run; its real Runtime driver may not exist
            # yet. Wait through the public team lifecycle before inspecting it.
            await runtime.team.wait([child["id"]], timeout=2)
            await finish(runtime, child["id"], errors)
        else:
            agent = runtime.team.get(caller)
            assert agent["parent_id"] == runtime.main_session()["id"]
            assert runtime.request_context(caller).reply_origin.kind == "channel"
            await rejected(runtime, tool, arguments(identifiers[0], "worker"))
            worker_checks.append(caller)

    errors = install_probe(runtime, probe)
    await phone(reply_case, "worker-origin")
    await finish(runtime, runtime.main_session()["id"], errors)
    assert len(worker_ids) == 1
    assert worker_checks == worker_ids


async def test_web_unattributed_and_group_requests_never_choose_a_private_fallback(
    reply_case: Case,
) -> None:
    runtime, channels, identifiers = reply_case
    observed: list[str] = []

    async def probe(caller: str, tool: Tool) -> None:
        origin = runtime.request_context(caller).reply_origin
        observed.append(
            origin.delivery_routes()[0]["kind"] if origin.kind == "channel" else origin.kind
        )
        await rejected(runtime, tool, arguments(identifiers[0]))

    errors = install_probe(runtime, probe)
    for source in ("web", "legacy-owner-input"):
        await runtime.submit("只在当前本地会话核对附件。", source=source, event_id=source)
        await finish(runtime, runtime.main_session()["id"], errors)
    # OneBot now rejects groups before admission. Preserve this tool-origin
    # boundary with a channel whose configured owner group is actually admitted.
    runtime.config.update(
        {
            "channels": {
                "telegram": {
                    "enabled": True,
                    "account_id": "123",
                    "token": "123:FAKE_TOKEN",
                    "base_url": "https://platform.invalid",
                    "owner_ids": ["7"],
                    "allowed_groups": ["99"],
                }
            }
        }
    )
    await channels.reconfigure()
    await channels.manager.receive(
        "telegram", IncomingMessage("group-input", "7", "99", "只在群里核对。", kind="group")
    )
    await finish(runtime, runtime.main_session()["id"], errors)
    assert observed == ["web", "unattributed", "group"]
    assert not outbox(runtime)


async def test_idempotency_is_bound_to_the_exact_input_not_only_its_destination(
    reply_case: Case,
) -> None:
    runtime, channels, identifiers = reply_case
    deliveries: list[str] = []

    async def probe(caller: str, tool: Tool) -> None:
        first = await positive(reply_case, tool)
        duplicate = await positive(reply_case, tool)
        assert first["id"] == duplicate["id"]
        await rejected(runtime, tool, arguments(identifiers[1]))
        deliveries.append(first["id"])

    errors = install_probe(runtime, probe)
    # Two actual owner inputs deliberately share the same full destination and
    # reply_to. The frozen input_id, rather than a guessed route difference,
    # must distinguish their stable keys.
    for identifier in ("new-owner-one", "new-owner-two"):
        await phone(reply_case, identifier, reply_to="same-passive-envelope")
        await finish(runtime, runtime.main_session()["id"], errors)
    assert len(deliveries) == 2 and deliveries[0] != deliveries[1]
    assert channels.manager.delivery(deliveries[0])["route"]["reply_to"] == "same-passive-envelope"
    assert len(outbox(runtime)) == 2


async def test_bad_references_and_live_revocation_never_partially_enqueue(reply_case: Case) -> None:
    runtime, _, identifiers = reply_case

    async def probe(caller: str, tool: Tool) -> None:
        await positive(reply_case, tool)
        for refs in ([identifiers[0], "0" * 32], [identifiers[0], identifiers[0]], []):
            await rejected(runtime, tool, {"attachment_ids": refs, "idempotency_key": "bad-files"})
        previous = runtime.config.settings.disabled_tools
        try:
            runtime.config.update({"disabled_tools": [*previous, "channel_reply_files"]})
            await rejected(runtime, tool, arguments(identifiers[0], "disabled"))
        finally:
            runtime.config.update({"disabled_tools": previous})
        runtime.config.update({"channels": {"onebot": {"owner_ids": ["8"]}}})
        await rejected(runtime, tool, arguments(identifiers[0], "revoked"))

    errors = install_probe(runtime, probe)
    await phone(reply_case, "revocation-origin")
    await finish(runtime, runtime.main_session()["id"], errors)
