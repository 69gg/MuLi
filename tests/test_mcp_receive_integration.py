from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_extensions import setup_runtime
from test_mcp import FakeClient, settings
from test_mcp_receive import Chunks

from muli.auth import hash_password
from muli.config import Settings
from muli.extensions import ExtensionError
from muli.management import SelfManagement
from muli.mcp_policy import MIN_MESSAGE_BYTES, ReceivePolicy
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools.mcp import MCPManager, MCPPrepareError


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("MCP receive integration cannot use network or subprocesses")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


async def test_changed_limit_creates_a_connection_and_drains_the_old_call() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Held(FakeClient):
        async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            return await super().call_tool(*args, **kwargs)

    old, new = Held(), FakeClient()
    clients = iter([old, new])
    manager = MCPManager(
        settings(),
        receive_policy=ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES),
        client_factory=lambda *_: next(clients),
    )
    pending: asyncio.Task[Any] | None = None
    try:
        original = (await manager.start())[0]
        old_connection = manager._connections["test"]
        pending = asyncio.create_task(original.handler({"q": "old"}))
        async with asyncio.timeout(2):
            await entered.wait()
        changed = ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES * 2)
        candidate = await manager.prepare(settings(), receive_policy=changed)
        retired = manager.publish(candidate)
        assert old_connection.receive.policy.max_message_bytes == MIN_MESSAGE_BYTES
        assert manager._connections["test"].receive.policy == changed
        assert manager.effective_receive_policy == changed and manager.draining == 1
        assert (await original.handler({"q": "revoked"}))["status"] == "not_executed"
        with pytest.raises(MCPPrepareError, match="^generation_draining$"):
            await manager.prepare(settings(), receive_policy=ReceivePolicy())
        assert not old.closed and not new.closed
        release.set()
        assert (await pending)["content"][0]["text"] == "结果"
        assert await manager.retire(retired)
        assert old.closed == 1 and new.closed == 0
        assert (await manager.tools[0].handler({"q": "new"}))["content"][0]["text"] == "结果"
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await manager.close()


async def test_stdio_is_not_restarted_for_an_http_receive_policy_change() -> None:
    client = FakeClient()
    config = settings(transport="stdio", command="python", args=[])
    config["mcpServers"]["test"].pop("url")
    manager = MCPManager(config, client_factory=lambda *_: client)
    try:
        await manager.start()
        connection = manager._connections["test"]
        candidate = await manager.prepare(
            config, receive_policy=ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES)
        )
        assert manager.publish(candidate) == ()
        assert manager._connections["test"] is connection and client.entered == 1
        assert (await manager.tools[0].handler({"q": "same stdio"}))["content"]
    finally:
        await manager.close()


async def test_bad_candidate_and_last_publish_latch_do_not_poison_the_current_connection() -> None:
    old, new = FakeClient(), FakeClient()
    clients = iter([old, new])
    manager = MCPManager(settings(), client_factory=lambda *_: next(clients))
    try:
        tool = (await manager.start())[0]
        connection = manager._connections["test"]
        candidate = await manager.prepare(
            settings(), receive_policy=ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES)
        )
        failed = candidate.connections["test"]
        failed.receive.fail("mcp_receive_budget_exceeded")
        with pytest.raises(MCPPrepareError, match="^mcp_receive_budget_exceeded$"):
            manager.publish(candidate)
        assert connection.usable and connection.receive.code is None
        assert manager._connections["test"] is connection
        assert (await tool.handler({"q": "old"}))["content"]
        await manager.discard(candidate)
        assert new.closed == 1 and old.closed == 0
    finally:
        await manager.close()


async def test_current_background_latch_blocks_dispatch_and_connection_reuse() -> None:
    client = FakeClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        tool = (await manager.start())[0]
        connection = manager._connections["test"]
        connection.receive.fail("mcp_receive_budget_exceeded")
        result = await tool.handler({"q": "not posted"})
        assert (
            result["status"] == "not_executed" and result["code"] == "mcp_receive_budget_exceeded"
        )
        assert client.calls == [] and not connection.usable
        assert manager.status()[0]["error_code"] == "mcp_receive_budget_exceeded"
        with pytest.raises(MCPPrepareError, match="^generation_draining$"):
            await manager.prepare(settings())
        await manager.retire((connection,))
    finally:
        await manager.close()


async def test_background_failure_is_queryable_without_queueing_an_automatic_reconnect(
    tmp_path: Path,
) -> None:
    runtime = setup_runtime(tmp_path)
    client = FakeClient()
    use_clients(runtime, client)
    app = create_app(runtime.config, runtime.data_dir, runtime=runtime)
    try:
        await runtime.extensions.load_initial()
        requested = runtime.extensions._requested
        runtime.mcp._connections["old"].receive.fail("mcp_receive_budget_exceeded")
        tool = next(t for t in app.state.management.tools(runtime) if t.name == "self_status")
        value = await tool.handler({})
        assert value["extensions"]["mcp"][0]["error_code"] == "mcp_receive_budget_exceeded"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as api:
            status = (await api.get("/api/extensions")).json()
        assert status["mcp"][0]["error_code"] == "mcp_receive_budget_exceeded"
        assert runtime.extensions._requested == requested
        assert not runtime.extensions._dirty and client.entered == 1
    finally:
        await app.state.close()


async def test_admitted_receive_failure_is_not_claimed_to_be_unexecuted_or_retried() -> None:
    manager: MCPManager

    class Failing(FakeClient):
        async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
            await super().call_tool(*args, **kwargs)
            raise manager._connections["test"].receive.fail("mcp_receive_budget_exceeded")

    client = Failing()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    try:
        tool = (await manager.start())[0]
        first = await tool.handler({"q": "possibly posted"})
        assert first["is_error"] and first.get("status") != "not_executed"
        assert len(client.calls) == 1
        assert (await tool.handler({"q": "no retry"}))["status"] == "not_executed"
        assert len(client.calls) == 1
    finally:
        await manager.close()


def use_clients(runtime: Runtime, *clients: FakeClient) -> None:
    items = iter(clients)
    runtime.mcp = MCPManager(runtime.config.settings.mcp, client_factory=lambda *_: next(items))


@pytest.mark.parametrize("initial", [False, True], ids=["reload", "startup"])
async def test_policy_drift_while_preparing_does_not_publish_or_save_configuration(
    tmp_path: Path, initial: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Held(FakeClient):
        async def list_tools(self) -> list[Any]:
            entered.set()
            await release.wait()
            return await super().list_tools()

    runtime = setup_runtime(tmp_path)
    old, new = FakeClient(), Held()
    use_clients(runtime, new if initial else old, new)
    task: asyncio.Task[None] | None = None
    try:
        if not initial:
            await runtime.extensions.load_initial()
        runtime.mcp_policy.replace(ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES))
        before = runtime.config.path.read_bytes()
        generation = runtime.extensions.generation

        async def apply() -> None:
            if initial:
                await runtime.extensions.load_initial()
            else:
                target = runtime.config.settings.model_copy(deep=True)
                target.skills.enabled = not target.skills.enabled
                async with runtime.configuration_lock:
                    await runtime.extensions.apply_locked(target, persist=True)

        task = asyncio.create_task(apply())
        async with asyncio.timeout(2):
            await entered.wait()
            runtime.mcp_policy.replace(ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES * 2))
            release.set()
            if initial:
                await task
            else:
                with pytest.raises(ExtensionError, match="^mcp_policy_changed$"):
                    await task
        assert runtime.extensions.generation == generation
        assert runtime.config.path.read_bytes() == before and new.closed == 1
        if not initial:
            assert (await runtime.mcp.tools[0].handler({"q": "still old"}))["content"]
            assert old.closed == 0
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_skills_fast_path_cannot_acknowledge_a_pending_policy(tmp_path: Path) -> None:
    runtime = setup_runtime(tmp_path)
    old, new = FakeClient(), FakeClient()
    use_clients(runtime, old, new)
    try:
        await runtime.extensions.load_initial()
        runtime.mcp_policy.replace(ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES))
        runtime.extensions._queue_reload(directory=True)
        async with runtime.configuration_lock:
            assert await runtime.extensions._apply_skills_locked(runtime.config.settings) is None
            await runtime.extensions.apply_locked(runtime.config.settings, persist=False)
        assert runtime.mcp.effective_receive_policy == runtime.mcp_policy.read()
        assert runtime.extensions.receive_policy_status()["pending"] is False
        assert new.entered == 1
    finally:
        await runtime.close()


async def test_bad_policy_keeps_core_and_does_not_reconnect_unprotected(tmp_path: Path) -> None:
    runtime = setup_runtime(tmp_path)
    client = FakeClient()
    use_clients(runtime, client)
    try:
        with runtime.store.db:
            runtime.store.db.execute(
                "INSERT INTO objects(kind,id,data,updated) VALUES('mcp_policy','receive-v1',?,0)",
                ('{"id":"receive-v1","schema_version":2,"policy":{}}',),
            )
        await runtime.extensions.load_initial()
        assert runtime.tools and client.entered == 0
        status = runtime.extensions.receive_policy_status()
        assert status == {
            "desired": None,
            "effective": None,
            "pending": True,
            "error": "mcp_policy_invalid",
        }
        assert runtime.extensions.status()["error"]["code"] == "mcp_policy_invalid"
    finally:
        await runtime.close()


async def test_failed_configuration_save_keeps_effective_policy_and_old_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = setup_runtime(tmp_path)
    old, new = FakeClient(), FakeClient()
    use_clients(runtime, old, new)
    try:
        await runtime.extensions.load_initial()
        previous = runtime.mcp.effective_receive_policy
        runtime.mcp_policy.replace(ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES))

        def rejected(target: Settings) -> NoReturn:
            raise OSError("injected save failure")

        monkeypatch.setattr(runtime.config, "replace", rejected)
        async with runtime.configuration_lock:
            with pytest.raises(ExtensionError, match="^config_save_failed$"):
                await runtime.extensions.apply_locked(runtime.config.settings, persist=True)
        assert runtime.mcp.effective_receive_policy == previous
        assert runtime.extensions.receive_policy_status()["pending"] is True
        assert old.closed == 0 and new.closed == 1
        assert (await runtime.mcp.tools[0].handler({"q": "old"}))["content"]
    finally:
        await runtime.close()


async def test_late_candidate_latch_is_checked_before_persisting_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = setup_runtime(tmp_path)
    old, new = FakeClient(), FakeClient()
    use_clients(runtime, old, new)
    try:
        await runtime.extensions.load_initial()
        runtime.mcp_policy.replace(ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES))
        original = runtime.config.path.read_bytes()
        prepare = runtime.mcp.prepare
        candidate: Any = None

        async def observe(*args: Any, **kwargs: Any) -> Any:
            nonlocal candidate
            candidate = await prepare(*args, **kwargs)
            return candidate

        fingerprint = runtime.extensions.fingerprint

        async def fail_after_last_await(settings: Settings) -> str:
            result = await fingerprint(settings)
            if candidate is not None:
                candidate.connections["old"].receive.fail("mcp_receive_budget_exceeded")
            return result

        monkeypatch.setattr(runtime.mcp, "prepare", observe)
        monkeypatch.setattr(runtime.extensions, "fingerprint", fail_after_last_await)
        target = runtime.config.settings.model_copy(deep=True)
        target.skills.enabled = not target.skills.enabled
        async with runtime.configuration_lock:
            with pytest.raises(ExtensionError, match="^mcp_receive_budget_exceeded$"):
                await runtime.extensions.apply_locked(target, persist=True)
        assert runtime.config.path.read_bytes() == original and old.closed == 0
        assert new.closed == 1
        assert (await runtime.mcp.tools[0].handler({"q": "old"}))["content"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("lost_return", ["notify", "reload"])
async def test_authenticated_api_and_tool_save_desired_without_claiming_effective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_return: str
) -> None:
    runtime = setup_runtime(tmp_path)
    runtime.config.update({"server": {"password_hash": hash_password("fixture-password")}})
    client = FakeClient()
    use_clients(runtime, client)
    app = create_app(runtime.config, runtime.data_dir, runtime=runtime)
    manager: SelfManagement = app.state.management
    try:
        await runtime.extensions.load_initial()
        original = runtime.config.path.read_bytes()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as api:
            assert (
                await api.put(
                    "/api/management/mcp-receive-policy",
                    json={"max_message_bytes": MIN_MESSAGE_BYTES},
                )
            ).status_code == 401
            assert (
                await api.post("/api/auth/login", json={"password": "fixture-password"})
            ).status_code == 200
            result = await api.put(
                "/api/management/mcp-receive-policy", json={"max_message_bytes": MIN_MESSAGE_BYTES}
            )
            assert result.status_code == 200, result.text
            value = result.json()
            assert value["desired"] == {"max_message_bytes": MIN_MESSAGE_BYTES}
            assert value["effective"] != value["desired"] and value["pending"]
            assert (await api.get("/api/management/mcp-receive-policy")).json() == value
            assert (
                await api.put(
                    "/api/management/mcp-receive-policy", json={"max_message_bytes": True}
                )
            ).status_code == 422
        tool = next(t for t in manager.tools(runtime) if t.name == "self_mcp_receive_configure")
        assert tool.risk == "write" and "preserve_output" not in json.dumps(tool.schema())
        changed = await tool.handler({"max_message_bytes": MIN_MESSAGE_BYTES * 2})
        configuration = next(t for t in manager.tools(runtime) if t.name == "self_config")
        assert (await configuration.handler({}))["mcp_receive_policy"] == changed
        assert runtime.extensions._full_reload and runtime.extensions._dirty
        assert runtime.config.path.read_bytes() == original and client.entered == 1

        async def lost_notify() -> NoReturn:
            raise RuntimeError("notification return lost")

        request_reload = runtime.extensions.request_reload

        def lost_reload(**kwargs: Any) -> NoReturn:
            request_reload(**kwargs)
            raise RuntimeError("notification return lost")

        if lost_return == "notify":
            monkeypatch.setattr(runtime.bus, "notify", lost_notify)
        else:
            monkeypatch.setattr(runtime.extensions, "request_reload", lost_reload)
        position = runtime.store.event_position()
        with pytest.raises(RuntimeError, match="notification return lost"):
            await tool.handler({"max_message_bytes": MIN_MESSAGE_BYTES * 3})
        assert runtime.mcp_policy.read().max_message_bytes == MIN_MESSAGE_BYTES * 3
        assert runtime.store.event_position() == position + 1
    finally:
        monkeypatch.undo()
        await app.state.close()


async def test_actual_fastmcp_factory_latches_background_get_before_another_tool_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils

    entered, release, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    streams: list[Chunks] = []

    class Background(Chunks):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await release.wait()
            yield b":" + b"x" * MIN_MESSAGE_BYTES

        async def aclose(self) -> None:
            await super().aclose()
            failed.set()

    def response(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "GET":
            body = Background()
            streams.append(body)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        if request.method == "DELETE":
            return httpx.Response(204, stream=Chunks())
        message = json.loads(request.content)
        method = message["method"]
        calls.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202, stream=Chunks())
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-stream", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "local",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "local result"}]}
        else:
            raise AssertionError(method)
        payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        body = Chunks(payload[:3], payload[3:])
        streams.append(body)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": "local-session"},
            stream=body,
        )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        # The original public factory boundary supplies the real SDK headers,
        # auth and timeouts; only I/O is local and does not consult proxy env.
        return httpx.AsyncClient(
            transport=httpx.MockTransport(response),
            follow_redirects=True,
            trust_env=False,
            **kwargs,
        )

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", factory)
    manager = MCPManager(
        settings(), receive_policy=ReceivePolicy(max_message_bytes=MIN_MESSAGE_BYTES)
    )
    try:
        tools = await manager.start()
        assert len(tools) == 1
        async with asyncio.timeout(3):
            await entered.wait()
            assert (await tools[0].handler({}))["content"][0]["text"] == "local result"
            release.set()
            await failed.wait()
        # There is no await between the stream's latch and the final dispatch fence.
        result = await tools[0].handler({})
        assert (
            result["status"] == "not_executed" and result["code"] == "mcp_receive_budget_exceeded"
        )
        assert calls.count("tools/call") == 1
    finally:
        release.set()
        await manager.close()
    assert all(stream.closed == 1 for stream in streams)
