from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_attention import attention_inputs, eventually
from test_proactive_delivery import configure, external
from test_proactive_memory import bind, extracted
from test_report_origin import origin, route, wait_for_reply
from test_runtime import FakeClient, make_runtime
from test_system_events_runtime import fail_streams, inbox_for

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.extensions import ExtensionError
from muli.proactive import FrozenRouting, guard_delivery
from muli.proactive_memory import PreferenceEvidence
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.reply_origin import RouteError
from muli.runtime import Runtime, _ExecutionResult
from muli.store import new_id
from muli.system_event_query import read_system_events
from muli.system_events import attention_source, system_log_context, validated_log_record
from muli.tools import Tool, parameters


async def start_capture(runtime: Runtime) -> None:
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "extensions": {"watch_enabled": False},
        }
    )
    runtime.system_logger = await runtime.system_events.start(runtime._system_logger)


def source_for(runtime: Runtime, record: dict[str, Any]) -> FrozenRouting:
    event = next(item for item in runtime.store.events() if item["id"] == record["event_id"])
    source = attention_source(runtime.store, event, (), runtime.config.settings.system_events)
    assert source is not None
    detail = json.loads(source.detail)
    assert not {"routing", "producer", "occurrence_id", "memory_evidence"}.intersection(detail)
    return source.routing


async def test_channel_status_includes_the_actual_autonomous_error_producer(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    try:
        await start_capture(runtime)
        runtime.system_events.extension_watch_failed()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        assert source_for(runtime, record).classification == "independent"
        sources = application.status()["proactive"]["independent_sources"]
        assert record["producer"] in sources
        assert "startup_update_check" in sources and "runtime_request" not in sources
        assert seen == []
    finally:
        await application.close()
        await runtime.close()


async def test_request_failure_keeps_A_while_B_queues_and_attention_final_routes_once(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    configure(runtime.config)
    inbox = inbox_for(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            index = len(self.seen)
            self.seen.append(messages)
            if index == 0:
                entered.set()
                await release.wait()
                raise ValueError("PRIVATE_FAILURE_TEXT")
            yield StreamEvent(
                "done", turn=ModelTurn(content="B已处理" if index == 1 else "A故障已告知")
            )

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        await start_capture(runtime)
        await inbox.start()
        first = await runtime.submit("A任务", source="channel:onebot:bot", route=route())
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.submit("B任务", source="channel:onebot:bot", route=route("8"))
        release.set()
        with pytest.raises(ValueError, match="PRIVATE_FAILURE_TEXT"):
            await runtime.wait(first["run_id"])
        report = await wait_for_reply(runtime, "A故障已告知")
        await eventually(lambda: not runtime.tasks)
        records = runtime.store.list("system_log")
        assert len(records) == 1 and records[0]["producer"] == "runtime_request"
        selected = source_for(runtime, records[0])
        assert selected.origin == origin(first["message"]["id"])
        assert report["delivery_routes"] == [route()]
        assert report["attention_routing"] == selected.dump()
        assert len(attention_inputs(runtime)) == 1
        assert "PRIVATE_FAILURE_TEXT" not in json.dumps(records)
        assert "PRIVATE_FAILURE_TEXT" not in attention_inputs(runtime)[0]["content"]
    finally:
        release.set()
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize("first_turn", ["answer", "tool"])
@pytest.mark.parametrize("failure", ["claim", "before_prepared", "new_request"])
async def test_new_claim_and_preparation_cannot_reuse_previous_request_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_turn: str, failure: str
) -> None:
    turn = (
        ModelTurn(content="A已处理")
        if first_turn == "answer"
        else ModelTurn(tool_calls=[ToolCall("probe-1", "probe", "{}")])
    )
    runtime, seen = make_runtime(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()
    probe_calls = 0

    async def probe(args: dict[str, Any]) -> dict[str, bool]:
        nonlocal probe_calls
        probe_calls += 1
        return {"ok": True}

    runtime.tools["probe"] = Tool("probe", "只读探针", parameters({}), probe)

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(messages)
            if len(self.seen) == 1:
                entered.set()
                await release.wait()
                yield StreamEvent("done", turn=turn)
            else:
                raise ValueError("new_request_failed")

    runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment,return-value]
    claims, preparations = 0, 0
    original_claim, original_origin = runtime._claim_inputs, runtime._reply_origin

    async def claim(session_id: str, run_id: str) -> list[dict[str, Any]]:
        nonlocal claims
        claims += 1
        result = await original_claim(session_id, run_id)
        if claims == 2 and failure == "claim":
            raise ValueError("new_claim_failed")
        return result

    def reply_origin(session_id: str) -> Any:
        nonlocal preparations
        preparations += 1
        if preparations == 2 and failure == "before_prepared":
            raise ValueError("before_prepared_failed")
        return original_origin(session_id)

    monkeypatch.setattr(runtime, "_claim_inputs", claim)
    monkeypatch.setattr(runtime, "_reply_origin", reply_origin)
    try:
        await start_capture(runtime)
        first = await runtime.submit("A任务", source="channel:onebot:bot", route=route())
        await asyncio.wait_for(entered.wait(), 2)
        second = await runtime.submit("B任务", source="channel:onebot:bot", route=route("8"))
        release.set()
        with pytest.raises(ValueError, match="failed"):
            await runtime.wait(first["run_id"])
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        selected = source_for(runtime, record)
        if failure == "new_request":
            assert selected.origin == origin(second["message"]["id"], target="8", sender_id="8")
            assert record["producer"] == "runtime_request"
        else:
            assert selected.classification == record["producer"] == "legacy_local"
        assert runtime.store.pending_inputs(first["session_id"]) == 0
        # B arrived before A's tool batch: the actual boundary must preserve
        # not_executed, rather than accidentally exercise an invalid handler.
        assert probe_calls == 0
        results = [
            message
            for message in runtime.store.messages(first["session_id"])
            if message.get("tool_call_id") == "probe-1"
        ]
        if first_turn == "tool":
            assert len(results) == 1
            assert json.loads(results[0]["content"])["status"] == "not_executed"
        else:
            assert results == []
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("suppress_child", [False, True])
async def test_hook_failure_uses_snapshot_before_hook_await_and_child_task_cannot_copy_it(
    tmp_path: Path, suppress_child: bool
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    session_id = runtime.main_session()["id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(sid: str, messages: list[Message]) -> list[Message]:
        async def child() -> None:
            runtime.system_events.clear_request(sid)
            runtime.system_events.bind_request(
                sid, origin("forged-child", target="8", sender_id="8")
            )
            assert runtime.system_logger is not None
            runtime.system_logger.error(
                "child failure", extra={"muli_component": "runtime", "muli_code": "runtime_failed"}
            )

        with system_log_context(notify=not suppress_child):
            await asyncio.create_task(child())
        entered.set()
        await release.wait()
        raise ValueError("hook_failed")

    runtime.context_hooks.append(hook)
    try:
        await start_capture(runtime)
        first = await runtime.submit("A任务", source="channel:onebot:bot", route=route())
        await asyncio.wait_for(entered.wait(), 2)
        # Change the stored latest input while the already-frozen hook is suspended.
        runtime.conversation.enqueue(
            session_id,
            "B已归档",
            source="channel:onebot:bot",
            principal="owner",
            mode="ask",
            max_pending=4,
            route=route("8"),
        )
        runtime.store.claim_inputs(session_id, first["run_id"], limit=4)
        release.set()
        with pytest.raises(ValueError, match="hook_failed"):
            await runtime.wait(first["run_id"])
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)
        records = runtime.store.list("system_log")
        parent = next(item for item in records if item["producer"] == "runtime_request")
        child = next(item for item in records if item["producer"] == "legacy_local")
        assert source_for(runtime, parent).origin == origin(first["message"]["id"])
        assert child["run_id"] is None and child["session_id"] is None
        assert child["notify"] is not suppress_child
        assert FrozenRouting.load(child["routing"]).classification == "legacy_local"
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("kind", ["chat", "workflow"])
async def test_non_main_and_workflow_prepared_requests_do_not_gain_external_failure_route(
    tmp_path: Path, kind: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    session = runtime.new_session()
    if kind == "workflow":
        session.update(kind="main", workflow_run_id=new_id())
        runtime.store.put("session", session)
    runtime.store.add_message(session["id"], "user", "任务", reply_origin=origin().dump())
    fail_streams(runtime, [], count=None)
    try:
        await start_capture(runtime)
        with pytest.raises(ValueError, match="PRIVATE_PROVIDER_CREDENTIAL"):
            await runtime._drive(
                session["id"], new_id(), "main", "ask", workflow_step=kind == "workflow"
            )
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        assert (
            source_for(runtime, runtime.store.list("system_log")[0]).classification
            == "legacy_local"
        )
    finally:
        await runtime.close()


async def test_complete_origin_is_part_of_dedup_and_new_canonical_fields_are_validated(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    session_id, run_id = runtime.main_session()["id"], new_id()
    runtime.store.put("run", {"id": run_id, "session_id": session_id, "status": "failed"})
    try:
        await start_capture(runtime)
        token = runtime.system_events.begin_run(run_id, session_id, main=True, notify=True)
        try:
            assert runtime.system_logger is not None
            for identifier in ("input-one", "input-two"):
                runtime.system_events.bind_request(session_id, origin(identifier))
                runtime.system_logger.error(
                    "opaque", extra={"muli_component": "runtime", "muli_code": "runtime_failed"}
                )
        finally:
            runtime.system_events.end_run(token)
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)
        record = runtime.store.list("system_log")[0]
        event = next(item for item in runtime.store.events() if item["id"] == record["event_id"])
        assert validated_log_record(record, event) is not None
        invalid = {**record, "routing": FrozenRouting.independent(runtime.config.settings).dump()}
        assert validated_log_record(invalid, event) is None
        data = {**event["data"], "routing": invalid["routing"]}
        assert validated_log_record(invalid, {**event, "data": data}) is None
        # Old records remain queryable, but never inherit a current default.
        fields = {"producer", "occurrence_id", "routing"}
        legacy = {key: value for key, value in record.items() if key not in fields}
        old_event = {
            **event,
            "data": {key: value for key, value in event["data"].items() if key not in fields},
        }
        assert validated_log_record(legacy, old_event) is not None
        runtime.store.put("system_log", legacy)
        source = attention_source(
            runtime.store, old_event, (), runtime.config.settings.system_events
        )
        assert source is not None and source.routing.classification == "legacy_local"
    finally:
        await runtime.close()


async def test_watch_first_durable_write_freezes_memory_evidence_and_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config)
    try:
        await start_capture(runtime)
        runtime.system_events.extension_watch_failed()
        # Capture has not selected a recipient or queried memory on the log thread.
        queued = runtime.system_events._queue[0]
        assert queued.routing.classification == "legacy_local"
        fact = extracted(runtime)
        bind(runtime, fact)
        before = runtime.store.event_position()
        original = runtime.store._put

        def fail(kind: str, item: dict[str, Any]) -> None:
            if kind == "system_log":
                raise ValueError("injected_record_failure")
            original(kind, item)

        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_put", fail)
            with pytest.raises(ValueError, match="injected_record_failure"):
                runtime.system_events._persist(queued)
        assert runtime.store.event_position() == before
        assert runtime.store.list("system_log") == []
        assert runtime.system_events._persist(queued)
        record = runtime.store.list("system_log")[0]
        selected = source_for(runtime, record)
        assert selected.classification == "independent" and selected.memory_evidence is not None
        assert selected.origin.delivery_routes() == [route("8")]
        runtime.memory.forget_source("source")
        runtime.config.update({"channels": {"proactive": {"default_destination": external()}}})
        assert not runtime.system_events._persist(
            replace(queued, captured_at=queued.captured_at + 120)
        )
        frozen = runtime.store.list("system_log")[0]
        assert frozen["routing"] == record["routing"] and frozen["count"] == 2
        with pytest.raises(RouteError, match="记忆依据已失效"):
            guard_delivery(
                runtime.config.settings, route("8"), frozen["routing"], db=runtime.store.db
            )
    finally:
        await runtime.close()


async def test_bad_log_reference_cannot_execute_object_comparison(tmp_path: Path) -> None:
    class Poison:
        def __eq__(self, other: object) -> bool:
            raise AssertionError("不得比较任意日志对象")

    runtime, _ = make_runtime(tmp_path, [])
    try:
        await start_capture(runtime)
        session_id = runtime.main_session()["id"]
        token = runtime.system_events.begin_run(new_id(), session_id, main=True, notify=True)
        try:
            assert runtime.system_logger is not None
            runtime.system_events.bind_request(session_id, origin())
            runtime.system_logger.error(
                "opaque",
                extra={
                    "muli_component": "runtime",
                    "muli_code": "runtime_failed",
                    "muli_run_id": Poison(),
                },
            )
        finally:
            runtime.system_events.end_run(token)
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        assert runtime.store.list("system_log")[0]["notify"] is False
    finally:
        await runtime.close()


@pytest.mark.parametrize("stage", ["before_first_prepared", "after_execute"])
async def test_run_without_current_prepared_request_stays_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config)
    original_emit = runtime.bus.emit

    async def emit(
        kind: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        if kind == "run.started" and stage == "before_first_prepared":
            assert session_id is not None
            runtime.store.cancel_inputs(session_id)
            raise ValueError("before_first_prepared_failed")
        return await original_emit(kind, data, session_id)

    async def execute(
        session_id: str, run_id: str, role: str, mode: Any, workflow: bool
    ) -> _ExecutionResult:
        await runtime._claim_inputs(session_id, run_id)
        prepared = await runtime._prepare_request(session_id)
        assert prepared.reply_origin.kind == "channel"
        return _ExecutionResult("请求已结束")

    first_lookup = True

    def children(run_id: str, *, after_id: str) -> list[str]:
        nonlocal first_lookup
        if first_lookup:
            first_lookup = False
            raise ValueError("after_execute_failed")
        return []

    monkeypatch.setattr(runtime.bus, "emit", emit)
    if stage == "after_execute":
        monkeypatch.setattr(runtime, "_execute", execute)
        monkeypatch.setattr(runtime.workflows, "child_run_ids", children)
    try:
        await start_capture(runtime)
        receipt = await runtime.submit("A任务", source="channel:onebot:bot", route=route())
        with pytest.raises(ValueError, match="failed"):
            await runtime.wait(receipt["run_id"])
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        assert (
            source_for(runtime, runtime.store.list("system_log")[0]).classification
            == "legacy_local"
        )
    finally:
        await runtime.close()


@pytest.mark.parametrize("producer", ["bound", "memory"])
async def test_large_valid_route_and_proof_remain_readable_as_small_event_metadata(
    tmp_path: Path, producer: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    long = "\U0001f600" * 200
    chosen = origin(
        "\x01" * 512 if producer == "bound" else "independent",
        account_id=long,
        target=long,
        sender_id=long,
        **({"reply_to": long} if producer == "bound" else {}),
    )
    assert chosen.kind == "channel"
    try:
        await start_capture(runtime)
        if producer == "memory":
            runtime.system_events._independent_routing = lambda: FrozenRouting(
                "independent",
                chosen,
                "a" * 64,
                PreferenceEvidence(long, 1, long, "b" * 64),
            )
            runtime.system_events.extension_watch_failed()
        else:
            session_id, run_id = runtime.main_session()["id"], new_id()
            runtime.store.put("run", {"id": run_id, "session_id": session_id})
            token = runtime.system_events.begin_run(run_id, session_id, main=True, notify=True)
            try:
                runtime.system_events.bind_request(session_id, chosen)
                assert runtime.system_logger is not None
                runtime.system_logger.error(
                    "opaque", extra={"muli_component": "runtime", "muli_code": "runtime_failed"}
                )
            finally:
                runtime.system_events.end_run(token)
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        item = runtime.store.list("system_log")[0]
        assert len(json.dumps(item, ensure_ascii=False).encode()) > 4096
        page = read_system_events(runtime.store, {}, max_output_chars=2048)
        assert page["invalid_record_count"] == 0 and len(page["items"]) == 1
        assert page["items"][0]["id"] == item["id"]
        assert not {"routing", "producer", "occurrence_id", "memory_evidence"}.intersection(
            page["items"][0]
        )
    finally:
        await runtime.close()


class WatchSteps:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.calls: asyncio.Queue[asyncio.Future[str]] = asyncio.Queue()

    async def fingerprint(self, settings: Any) -> str:
        result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.calls.put_nowait(result)
        return await result

    async def next(self) -> asyncio.Future[str]:
        self.runtime.extensions._wake.set()
        return await asyncio.wait_for(self.calls.get(), 2)


async def test_autonomous_watch_reports_each_real_failure_transition_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config)
    await start_capture(runtime)
    ext = runtime.extensions
    runtime.config.update(
        {
            "extensions": {
                "watch_enabled": True,
                "debounce_seconds": 0,
                "scan_interval_seconds": 0.25,
            }
        }
    )
    ext.phase, ext._observed = "loaded", "original"
    steps = WatchSteps(runtime)
    monkeypatch.setattr(ext, "fingerprint", steps.fingerprint)
    task = asyncio.create_task(ext._watch())
    try:
        (await steps.next()).set_exception(ExtensionError("source_changed", "PRIVATE_DIRECTORY"))
        current = await steps.next()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        current.set_exception(ExtensionError("source_changed", "PRIVATE_DIRECTORY"))
        current = await steps.next()
        assert runtime.system_events.status()["captured"] == 1
        current.set_exception(ExtensionError("source_unavailable", "PRIVATE_DIRECTORY"))
        current = await steps.next()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)

        # A real successful publication (not merely one successful scan) recovers phase.
        async def publish(settings: Any, *, persist: bool) -> None:
            ext.phase, ext._error = "loaded", None
            ext.generation += 1

        monkeypatch.setattr(ext, "apply_locked", publish)
        current.set_result("new-generation")
        current = await steps.next()
        assert ext.phase == "loaded"
        current.set_exception(ExtensionError("source_changed", "PRIVATE_DIRECTORY"))
        await steps.next()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 3)
        records = runtime.store.list("system_log")
        assert len({item["occurrence_id"] for item in records}) == 3
        assert all(item["producer"] == "extensions_watch" for item in records)
        assert all(
            source_for(runtime, item).origin.delivery_routes() == [route()] for item in records
        )
        assert "PRIVATE_DIRECTORY" not in json.dumps(records)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_same_watch_failure_during_busy_queued_turns_is_only_one_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    await start_capture(runtime)
    runtime.config.update(
        {
            "extensions": {
                "watch_enabled": True,
                "debounce_seconds": 0,
                "scan_interval_seconds": 0.25,
            }
        }
    )
    ext = runtime.extensions
    ext.phase, ext._observed = "loaded", "original"
    busy = asyncio.create_task(asyncio.Event().wait())
    runtime.tasks["busy-fixture"] = busy
    ext.request_reload()
    steps = WatchSteps(runtime)
    monkeypatch.setattr(ext, "fingerprint", steps.fingerprint)
    task = asyncio.create_task(ext._watch())
    try:
        current = await steps.next()
        for _ in range(3):
            current.set_exception(ExtensionError("source_changed", "same-source"))
            current = await steps.next()
            assert ext.phase == "queued" and ext._dirty
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        assert runtime.system_events.status()["captured"] == 1
    finally:
        task.cancel()
        busy.cancel()
        await asyncio.gather(task, busy, return_exceptions=True)
        runtime.tasks.pop("busy-fixture", None)
        await runtime.close()


@pytest.mark.parametrize("trigger", ["directory", "manual", "catalog", "direct"])
async def test_only_directory_generated_publish_failure_may_use_independent_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    await start_capture(runtime)
    runtime.config.update(
        {
            "extensions": {
                "watch_enabled": True,
                "debounce_seconds": 0,
                "scan_interval_seconds": 0.25,
            }
        }
    )
    ext = runtime.extensions
    ext.phase, ext._observed = "loaded", "original"
    steps = WatchSteps(runtime)
    monkeypatch.setattr(ext, "fingerprint", steps.fingerprint)

    async def failed(settings: Any, *, persist: bool) -> None:
        raise ext._failure(ExtensionError("candidate_failed", "PRIVATE_DIRECTORY"))

    monkeypatch.setattr(ext, "apply_locked", failed)
    task: asyncio.Task[None] | None = None
    try:
        if trigger == "direct":
            with pytest.raises(ExtensionError):
                await ext.apply_locked(runtime.config.settings, persist=True)
        else:
            if trigger == "manual":
                ext.request_reload()
            elif trigger == "catalog":
                ext._catalog_changed("catalog")
            task = asyncio.create_task(ext._watch())
            (await steps.next()).set_result(
                "new-directory" if trigger == "directory" else "original"
            )
            await steps.next()
        assert ext.phase == "failed"
        if trigger == "directory":
            await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
            assert (
                source_for(runtime, runtime.store.list("system_log")[0]).classification
                == "independent"
            )
        else:
            assert runtime.system_events.status()["captured"] == 0
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
