from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import attention_inputs, eventually
from test_extensions import setup_runtime
from test_mcp import FakeClient, settings
from test_mcp_receive import Chunks
from test_proactive_delivery import configure, external
from test_proactive_memory import bind, extracted
from test_report_origin import origin, route
from test_runtime import make_runtime
from test_system_error_origin import source_for, start_capture
from test_system_events_runtime import fail_streams, inbox_for

from muli.mcp_receive_errors import RECEIVE_FAILURE_CODES, ReceiveFailureCode
from muli.proactive import FrozenRouting, guard_delivery
from muli.providers import ModelTurn
from muli.reply_origin import RouteError
from muli.runtime import Runtime
from muli.store import new_id
from muli.system_event_query import read_system_events
from muli.system_events import attention_source, system_log_context, validated_log_record
from muli.tools.mcp import MCPClient, MCPManager, MCPPrepareError
from muli.tools.mcp_receive import ReceiveError, ReceiveState, install_receive_guard

CODE: ReceiveFailureCode = "mcp_receive_budget_exceeded"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("receive notification tests cannot use network or subprocesses")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


async def swallowed_background_receive(state: ReceiveState) -> None:
    stream = Chunks(b"x" * state.policy.max_message_bytes, b"x", b"not reached")

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        install_receive_guard(client, state, streamable=True)
        try:
            async with client.stream("GET", "https://private.invalid/mcp") as value:
                async for _ in value.aiter_raw():
                    pass
        except ReceiveError as error:
            # The host cannot depend on a background consumer propagating this exception.
            assert error.code == CODE
        else:
            raise AssertionError("the raw stream must exceed its frozen budget")
    assert stream.reads == 2 and stream.closed == 1


def model_runtime(tmp_path: Path, *, fail_notice: bool = False) -> tuple[Runtime, list[Any]]:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="连接已暂停，请核对状态")])
    runtime.config.update(
        {
            "mcp": settings(),
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
        }
    )
    if fail_notice:
        fail_streams(runtime, seen, count=None)
    runtime.mcp._factory = lambda *_: FakeClient()
    return runtime, seen


@pytest.mark.parametrize("fail_notice", [False, True], ids=["notice", "notice-fails"])
async def test_background_receive_enters_existing_attention_once_without_a_loop(
    tmp_path: Path, fail_notice: bool
) -> None:
    runtime, seen = model_runtime(tmp_path, fail_notice=fail_notice)
    configure(runtime.config)
    inbox = inbox_for(runtime)
    try:
        await runtime.startup()
        await inbox.start()
        requested = runtime.extensions._requested
        tool = runtime.mcp.tools[0]
        connection = runtime.mcp._connections["test"]
        await swallowed_background_receive(connection.receive)
        await eventually(
            lambda: (
                len(seen) == 1
                and not runtime.tasks
                and inbox.cursor == runtime.store.event_position()
            )
        )
        records = runtime.store.list("system_log")
        fault = next(item for item in records if item["producer"] == "mcp_connection")
        assert fault["code"] == CODE and fault["count"] == 1 and fault["notify"] is True
        assert fault["run_id"] is fault["session_id"] is None
        assert source_for(runtime, fault).origin.delivery_routes() == [route()]
        assert len(attention_inputs(runtime)) == 1
        assert runtime.extensions._requested == requested and not runtime.extensions._dirty
        assert (await tool.handler({"q": "must not be replayed"}))["status"] == "not_executed"
        assert connection.client.calls == []  # type: ignore[attr-defined]
        assert len(records) == (2 if fail_notice else 1)
        if fail_notice:
            assert next(item for item in records if item is not fault)["notify"] is False
        evidence = json.dumps([records, attention_inputs(runtime)], ensure_ascii=False)
        assert "private.invalid" not in evidence and "PRIVATE_PROVIDER_CREDENTIAL" not in evidence
    finally:
        await inbox.close()
        await runtime.close()


async def test_initial_start_and_full_reconfigure_bind_the_actual_runtime_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[FakeClient] = []

    def factory(config: dict[str, Any], **kwargs: Any) -> MCPManager:
        def client(server: dict[str, Any], timeout: float) -> MCPClient:
            value = FakeClient()
            clients.append(value)
            return value

        return MCPManager(config, client_factory=client, **kwargs)

    monkeypatch.setattr("muli.runtime.MCPManager", factory)
    runtime = setup_runtime(tmp_path)
    try:
        await runtime.startup()
        first_manager = runtime.mcp
        first = first_manager._connections["old"]
        first.receive.fail(CODE)
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        await runtime.reconfigure()
        assert runtime.mcp is not first_manager and first_manager._closed
        second = runtime.mcp._connections["old"]
        second.receive.fail(CODE)
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)
        records = runtime.store.list("system_log")
        assert len(records) == 2 and all(item["producer"] == "mcp_connection" for item in records)
        assert first.receive_occurrence_id != second.receive_occurrence_id
        assert clients[0].closed == 1 and len(clients) == 2
    finally:
        await runtime.close()
    assert all(client.closed == 1 for client in clients)


async def test_runtime_closing_revokes_notification_before_the_manager_close_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = model_runtime(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    settle = runtime.maintenance.settle_activation

    async def gated_settle() -> None:
        entered.set()
        await release.wait()
        await settle()

    monkeypatch.setattr(runtime.maintenance, "settle_activation", gated_settle)
    closing: asyncio.Task[None] | None = None
    try:
        await runtime.startup()
        connection = runtime.mcp._connections["test"]
        closing = asyncio.create_task(runtime.close())
        async with asyncio.timeout(2):
            await entered.wait()
        assert runtime._closing and not runtime.mcp._closed
        assert runtime.system_events.status()["running"]
        connection.receive.fail(CODE)
        assert runtime.system_events.status()["captured"] == 0
        assert connection.receive.code == CODE and not connection.usable
        assert connection in runtime.mcp._retiring
    finally:
        release.set()
        if closing is not None:
            await closing
        await runtime.close()
    assert connection.closed


async def test_schema_generation_reuses_the_connection_occurrence_until_a_new_connection() -> None:
    client = FakeClient()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    seen: list[tuple[str, str]] = []
    manager.set_receive_failed_callback(lambda code, occurrence: seen.append((code, occurrence)))
    try:
        original = (await manager.start())[0]
        connection = manager._connections["test"]
        occurrence = connection.receive_occurrence_id
        candidate = await manager.prepare(settings(), refresh={"test"})
        assert manager.publish(candidate) == ()
        assert manager._connections["test"] is connection
        assert connection.receive_occurrence_id == occurrence and not seen
        connection.receive.fail(CODE)
        connection.receive.fail("mcp_receive_failed")
        assert seen == [(CODE, occurrence)] and connection.receive.code == CODE
        assert (await original.handler({"q": "old"}))["status"] == "not_executed"
        assert (await manager.tools[0].handler({"q": "new"}))["status"] == "not_executed"
        assert client.calls == []
        await manager.retire((connection,))
        manager._factory = lambda *_: FakeClient()
        candidate = await manager.prepare(settings())
        manager.publish(candidate)
        replacement = manager._connections["test"]
        replacement.receive.fail(CODE)
        assert seen == [(CODE, occurrence), (CODE, replacement.receive_occurrence_id)]
        assert occurrence != replacement.receive_occurrence_id
    finally:
        await manager.close()


@pytest.mark.parametrize("phase", ["candidate", "retired", "closing", "stdio"])
async def test_unpublished_retired_closing_and_stdio_failures_cannot_claim_current_http_source(
    phase: str,
) -> None:
    config = settings()
    if phase == "stdio":
        config = settings(transport="stdio", command="python", args=[])
        config["mcpServers"]["test"].pop("url")
    manager = MCPManager(config, client_factory=lambda *_: FakeClient())
    seen: list[str] = []
    manager.set_receive_failed_callback(lambda code, _: seen.append(code))
    try:
        if phase == "candidate":
            candidate = await manager.prepare(config)
            candidate.connections["test"].receive.fail(CODE)
            with pytest.raises(MCPPrepareError, match=f"^{CODE}$"):
                manager.publish(candidate)
            await manager.discard(candidate)
        else:
            await manager.start()
            connection = manager._connections["test"]
            if phase == "retired":
                candidate = await manager.prepare(settings(url="https://other.invalid/mcp"))
                retired = manager.publish(candidate)
                connection.receive.fail(CODE)
                await manager.retire(retired)
            elif phase == "closing":
                await manager.close()
                connection.receive.fail(CODE)
            else:
                connection.receive.fail(CODE)
        assert seen == []
    finally:
        await manager.close()


async def test_callback_failure_cannot_undo_the_admitted_failure_or_its_cleanup() -> None:
    manager: MCPManager
    entered, release = asyncio.Event(), asyncio.Event()

    class Failing(FakeClient):
        async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
            await super().call_tool(*args, **kwargs)
            entered.set()
            await release.wait()
            raise manager._connections["test"].receive.fail(CODE)

    def fail(code: str, occurrence: str) -> None:
        assert manager._connections["test"] in manager._retiring
        raise ValueError("PRIVATE_CALLBACK_FAILURE")

    client = Failing()
    manager = MCPManager(settings(), client_factory=lambda *_: client)
    manager.set_receive_failed_callback(fail)
    call: asyncio.Task[Any] | None = None
    try:
        tool = (await manager.start())[0]
        connection = manager._connections["test"]

        async def invoke() -> Any:
            return await tool.handler({"q": "admitted"})

        call = asyncio.create_task(invoke())
        async with asyncio.timeout(2):
            await entered.wait()
        release.set()
        result = await call
        assert result["is_error"] and result.get("status") != "not_executed"
        assert connection.receive.code == CODE and not connection.usable
        assert manager.status()[0]["error_code"] == CODE
        assert (await tool.handler({"q": "not retried"}))["status"] == "not_executed"
        assert len(client.calls) == 1
        assert await manager.retire((connection,)) and client.closed == 1
    finally:
        release.set()
        if call is not None:
            await asyncio.gather(call, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("code", sorted(RECEIVE_FAILURE_CODES))
async def test_each_fixed_code_uses_canonical_system_evidence_and_the_bounded_query(
    tmp_path: Path, code: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        await start_capture(runtime)
        runtime.system_events.mcp_receive_failed(code, new_id())
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        event = next(value for value in runtime.store.events() if value["id"] == record["event_id"])
        assert validated_log_record(record, event) is not None
        assert record["component"] == "extensions" and record["code"] == code
        assert source_for(runtime, record).classification == "independent"
        page = read_system_events(runtime.store, {"code": code}, max_output_chars=8000)
        assert page["items"][0]["id"] == record["id"]
        assert "routing" not in json.dumps(page) and "occurrence_id" not in json.dumps(page)
    finally:
        await runtime.close()


async def test_capture_is_pure_then_first_durable_write_freezes_route_and_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config)
    try:
        await start_capture(runtime)
        before = runtime.store.db.total_changes
        occurrence = new_id()
        with system_log_context(notify=False):
            runtime.system_events.mcp_receive_failed(CODE, occurrence)
        queued = runtime.system_events._queue[0]
        assert runtime.store.db.total_changes == before
        assert queued.routing.classification == "legacy_local" and queued.notify is False
        fact = extracted(runtime)
        bind(runtime, fact)
        position = runtime.store.event_position()
        put = runtime.store._put

        def fail(kind: str, value: dict[str, Any]) -> None:
            if kind == "system_log":
                raise OSError("PRIVATE_STORAGE_ERROR")
            put(kind, value)

        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_put", fail)
            with pytest.raises(OSError, match="PRIVATE_STORAGE_ERROR"):
                runtime.system_events._persist(queued)
        assert runtime.store.event_position() == position and not runtime.store.list("system_log")
        assert runtime.system_events._persist(queued)
        record = runtime.store.list("system_log")[0]
        frozen = FrozenRouting.load(record["routing"])
        assert frozen.memory_evidence is not None and frozen.origin.delivery_routes() == [
            route("8")
        ]
        runtime.memory.forget_source("source")
        runtime.config.update({"channels": {"proactive": {"default_destination": external()}}})
        assert not runtime.system_events._persist(
            replace(queued, captured_at=queued.captured_at + 120, notify=True)
        )
        current = runtime.store.list("system_log")[0]
        assert current["id"] == record["id"] and current["count"] == 2
        assert current["routing"] == record["routing"] and current["notify"] is False
        with pytest.raises(RouteError, match="记忆依据已失效"):
            guard_delivery(
                runtime.config.settings, route("8"), current["routing"], db=runtime.store.db
            )
    finally:
        await runtime.close()


async def test_request_and_copied_task_context_cannot_lend_their_bound_destination(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config)
    try:
        await start_capture(runtime)
        session_id = runtime.main_session()["id"]
        token = runtime.system_events.begin_run(new_id(), session_id, main=True, notify=True)
        try:
            runtime.system_events.bind_request(session_id, origin())
            await asyncio.create_task(emit_failure(runtime, new_id()))
            with system_log_context(notify=False):
                with system_log_context(notify=True):
                    await asyncio.create_task(emit_failure(runtime, new_id()))
        finally:
            runtime.system_events.end_run(token)
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)
        records = runtime.store.list("system_log")
        assert all(item["run_id"] is item["session_id"] is None for item in records)
        assert {item["notify"] for item in records} == {False, True}
        assert all(item["producer"] == "mcp_connection" for item in records)
        assert (
            source_for(runtime, next(item for item in records if item["notify"])).classification
            == "independent"
        )
    finally:
        await runtime.close()


async def emit_failure(runtime: Runtime, occurrence: str) -> None:
    runtime.system_events.mcp_receive_failed(CODE, occurrence)


async def test_raw_logging_cannot_claim_mcp_producer_and_invalid_inputs_are_not_formatted(
    tmp_path: Path,
) -> None:
    class Private:
        def __str__(self) -> str:
            raise AssertionError("must not format a private value")

        def __hash__(self) -> int:
            raise AssertionError("must not hash a private value")

    runtime, _ = make_runtime(tmp_path, [])
    try:
        await start_capture(runtime)
        runtime.system_events.mcp_receive_failed(Private(), new_id())  # type: ignore[arg-type]
        runtime.system_events.mcp_receive_failed(CODE, Private())  # type: ignore[arg-type]
        runtime.system_events.mcp_receive_failed("private arbitrary code", new_id())
        runtime.system_events.mcp_receive_failed(CODE, "x" * 33)
        assert runtime.system_events.status()["captured"] == 0
        assert runtime.system_logger is not None
        runtime.system_logger.error(
            Private(),
            Private(),
            extra={
                "muli_component": "extensions",
                "muli_code": CODE,
                "producer": "mcp_connection",
                "occurrence_id": new_id(),
            },
            exc_info=(ValueError, ValueError("PRIVATE_EXCEPTION_BODY"), None),
        )
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        assert record["producer"] == "legacy_local" and record["occurrence_id"] is None
        assert source_for(runtime, record).classification == "legacy_local"
        assert "PRIVATE_EXCEPTION_BODY" not in json.dumps(runtime.store.events())
    finally:
        await runtime.close()


@pytest.mark.parametrize("policy", [{"capture_level": "critical"}, {"enabled": False}])
async def test_capture_policy_still_prevents_recording(
    tmp_path: Path, policy: dict[str, Any]
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"system_events": policy})
    try:
        await start_capture(runtime)
        runtime.system_events.mcp_receive_failed(CODE, new_id())
        assert runtime.system_events.status()["captured"] == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize("policy", [{"notify_level": "critical"}, {"notify_enabled": False}])
async def test_recorded_failure_cannot_bypass_notification_policy(
    tmp_path: Path, policy: dict[str, Any]
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"system_events": policy})
    try:
        await start_capture(runtime)
        runtime.system_events.mcp_receive_failed(CODE, new_id())
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        event = next(value for value in runtime.store.events() if value["id"] == record["event_id"])
        assert (
            attention_source(runtime.store, event, (), runtime.config.settings.system_events)
            is None
        )
    finally:
        await runtime.close()


async def test_full_queue_and_closed_handler_keep_the_existing_best_effort_limits(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"system_events": {"queue_capacity": 1}})
    try:
        await start_capture(runtime)
        runtime.system_events.mcp_receive_failed(CODE, new_id())
        runtime.system_events.mcp_receive_failed(CODE, new_id())
        assert runtime.system_events.status()["queued"] == 1
        assert runtime.system_events.status()["overflow"] == 1
        await runtime.system_events.close()
        runtime.system_events.mcp_receive_failed(CODE, new_id())
        assert runtime.system_events.status()["captured"] == 1
        assert runtime.system_events.status()["dropped_on_close"] == 1
        assert not runtime.store.list("system_log")
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "field,value",
    [("producer", "future_producer"), ("occurrence_id", "bad"), ("code", "runtime_failed")],
)
async def test_canonical_validation_rejects_bad_or_unknown_mcp_sources(
    tmp_path: Path, field: str, value: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        await start_capture(runtime)
        runtime.system_events.mcp_receive_failed(CODE, new_id())
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        event = next(item for item in runtime.store.events() if item["id"] == record["event_id"])
        changed = {**record, field: value}
        assert validated_log_record(changed, event) is None
        assert (
            validated_log_record(changed, {**event, "data": {**event["data"], field: value}})
            is None
        )
        fields = {"producer", "occurrence_id", "routing"}
        legacy = {key: item for key, item in record.items() if key not in fields}
        legacy_event = {
            **event,
            "data": {key: item for key, item in event["data"].items() if key not in fields},
        }
        assert validated_log_record(legacy, legacy_event) is not None
        runtime.store.put("system_log", legacy)
        source = attention_source(
            runtime.store, legacy_event, (), runtime.config.settings.system_events
        )
        assert source is not None and source.routing.classification == "legacy_local"
    finally:
        await runtime.close()
