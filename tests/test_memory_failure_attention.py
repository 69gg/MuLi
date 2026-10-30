"""Real Attention/Runtime boundaries for collected-source failure notices."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import attention_inputs, eventually
from test_memory_source_failure import collect, failure_events, pipeline_for, reject
from test_runtime import FakeClient, make_runtime

from muli.attention import AttentionInbox
from muli.media import MediaConfig, MediaService
from muli.memory_application import MemoryApplication
from muli.memory_pipeline import MemoryPipeline
from muli.memory_source_failure import MemorySourceFailure, MemorySourceFailureExpired
from muli.proactive import FrozenRouting
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("memory failure attention must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def components(runtime: Runtime) -> tuple[AttentionInbox, MemoryPipeline]:
    runtime.config.update(
        {
            "attention": {"events": ["memory"], "coalesce_seconds": 0},
            "memory": {"auto_extract": False},
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
        }
    )
    inbox = AttentionInbox(runtime)
    pipeline = pipeline_for(runtime.store, bus=runtime.bus)
    pipeline.failure_routing = lambda: FrozenRouting.independent(
        runtime.config.settings, runtime.store.db
    )
    return inbox, pipeline


async def fail(pipeline: MemoryPipeline) -> str:
    source_id = collect(pipeline)
    await pipeline._run_job(source_id)
    return source_id


async def test_one_private_notice_survives_reconfigure_and_real_store_reopen(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content="资料已保存，整理失败，请核查来源。")]
    )
    inbox, pipeline = components(runtime)
    event: dict[str, Any]
    try:
        await runtime.startup()
        await inbox.start()
        source_id = await fail(pipeline)
        event = failure_events(runtime.store)[0]
        await eventually(lambda: len(seen) == 1 and not runtime.tasks)
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1 and inputs[0]["state"] == "delivered"
        assert source_id in inputs[0]["content"]
        assert "私有收藏正文" not in inputs[0]["content"]
        assert "PRIVATE_SOURCE_OR_UPSTREAM_CREDENTIAL" not in inputs[0]["content"]
        assert inputs[0]["mode"] == "ask" and inputs[0]["route"] is None
        assert inputs[0]["attention_memory_failure"] == event["data"]["source_failure"]
        await inbox.reconfigure()
        await pipeline._publish(source_id)
        await eventually(lambda: inbox.cursor == runtime.store.event_position())
        assert len(seen) == 1 and len(runtime.store.list("attention_receipt")) == 1
    finally:
        await pipeline.close()
        await inbox.close()
        await runtime.close()

    reopened, new_seen = make_runtime(tmp_path, [ModelTurn(content="不应重复处理")])
    current_inbox, current_pipeline = components(reopened)
    try:
        await reopened.startup()
        await current_inbox.start()
        current_pipeline.recover()
        await reopened.bus.emit("memory.pipeline.updated", event["data"])
        await eventually(lambda: current_inbox.cursor == reopened.store.event_position())
        assert new_seen == []
        assert len(reopened.store.list("attention_receipt")) == 1
        assert len(attention_inputs(reopened)) == 1
    finally:
        await current_pipeline.close()
        await current_inbox.close()
        await reopened.close()


@pytest.mark.parametrize("change", ["forget", "retry", "revision", "recovery"])
async def test_admission_rechecks_proof_after_waiting_for_configuration_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="不应接纳过期提醒")])
    inbox, pipeline = components(runtime)
    entered = asyncio.Event()
    original = runtime.submit_attention

    async def submitting(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        entered.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, "submit_attention", submitting)
    held = False
    try:
        await runtime.startup()
        await inbox.start()
        # The persisted cursor is current before the consumer's first wake.
        # Wait until startup has really released configuration_lock, otherwise
        # this fixture blocks that wake instead of exercising admission's wait.
        await eventually(
            lambda: (
                inbox.status()["waiting"] == "events"
                and inbox.cursor == runtime.store.event_position()
            )
        )
        await runtime.configuration_lock.acquire()
        held = True
        source_id = await fail(pipeline)
        await asyncio.wait_for(entered.wait(), 3)
        if change == "forget":
            runtime.memory.forget_source(source_id)
        elif change == "retry":
            source = runtime.memory.source(source_id)
            assert source is not None
            pipeline.retry_source(source_id, source["digest"], max_output_chars=6000)
        else:
            with runtime.store.db:
                if change == "revision":
                    runtime.store.db.execute(
                        "UPDATE memory_sources SET digest=? WHERE id=?", ("0" * 64, source_id)
                    )
                else:
                    runtime.store.db.execute(
                        "UPDATE memory_jobs SET state='processing' WHERE source_id=?", (source_id,)
                    )
            if change == "recovery":
                pipeline.recover()
        runtime.configuration_lock.release()
        held = False
        await eventually(lambda: inbox.cursor == runtime.store.event_position())
        assert seen == [] and attention_inputs(runtime) == []
    finally:
        if held:
            runtime.configuration_lock.release()
        await pipeline.close()
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize("change", ["forget", "retry", "disabled", "events"])
async def test_unconfigured_queued_source_notice_is_cancelled_without_losing_other_input(
    tmp_path: Path, change: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="仅处理另一条普通系统事项。")])
    inbox, pipeline = components(runtime)
    runtime.config.update({"models": {"main": {"model": ""}}})
    try:
        await runtime.startup()
        await inbox.start()
        source_id = await fail(pipeline)
        await eventually(lambda: len(attention_inputs(runtime)) == 1)
        assert attention_inputs(runtime)[0]["state"] == "pending" and seen == []
        other = await runtime.submit_attention(
            "另一条普通系统事项", "ordinary-system-input", objects=[]
        )
        assert other is not None and other["state"] == "pending"
        if change == "forget":
            runtime.memory.forget_source(source_id)
        elif change == "retry":
            source = runtime.memory.source(source_id)
            assert source is not None
            pipeline.retry_source(source_id, source["digest"], max_output_chars=6000)
        elif change == "disabled":
            runtime.config.update({"memory": {"enabled": False}})
        else:
            runtime.config.update({"attention": {"events": []}})
        runtime.config.update({"models": {"main": {"model": "main"}}})
        await runtime.wake_pending_inputs()
        await eventually(lambda: len(seen) == 1 and not runtime.tasks)
        inputs = attention_inputs(runtime)
        source_input = next(item for item in inputs if "attention_memory_failure" in item)
        other_input = next(item for item in inputs if item["id"] == other["id"])
        assert source_input["state"] == "cancelled" and other_input["state"] == "delivered"
        assert all("memory_source_failed" not in message.content for message in seen[0])
    finally:
        await pipeline.close()
        await inbox.close()
        await runtime.close()


async def test_expiry_during_context_preparation_stops_before_any_model_call(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="不应处理过期资料")])
    inbox, pipeline = components(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def context(session_id: str, messages: list[Message]) -> list[Message]:
        entered.set()
        await release.wait()
        return messages

    runtime.context_hooks.append(context)
    try:
        await runtime.startup()
        await inbox.start()
        source_id = await fail(pipeline)
        await asyncio.wait_for(entered.wait(), 3)
        runtime.memory.forget_source(source_id)
        release.set()
        await eventually(
            lambda: not runtime.tasks and inbox.cursor == runtime.store.event_position()
        )
        assert seen == []
        assert any(
            run.get("cancel_reason") == "memory_source_failure_expired"
            for run in runtime.store.list("run")
        )
    finally:
        release.set()
        await pipeline.close()
        await inbox.close()
        await runtime.close()


async def test_team_report_cannot_extend_expired_notice_proof_into_context(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox, pipeline = components(runtime)
    runtime.config.update({"models": {"main": {"model": ""}}})
    context_calls: list[str] = []

    async def context(session_id: str, messages: list[Message]) -> list[Message]:
        context_calls.append(session_id)
        return messages

    try:
        await runtime.startup()
        source_id = await fail(pipeline)
        proof = MemorySourceFailure.load(
            failure_events(runtime.store)[0]["data"]["source_failure"], max_bytes=4096
        )
        assert proof is not None
        receipt = await runtime.submit_attention(
            "已保存资料，整理失败：" + source_id,
            "source-notice-before-team-report",
            objects=[],
            routing=proof.routing,
            memory_failure=proof,
        )
        assert receipt is not None and receipt["state"] == "pending"
        session_id = runtime.main_session()["id"]
        notice = await runtime._claim_inputs(session_id, "source-notice-request")
        assert len(notice) == 1 and notice[0]["id"] == receipt["id"]
        previous = await runtime._prepare_request(session_id)
        assert previous.memory_failure == proof

        # Use the durable host report handoff, including its internal ingress
        # admission; a team source string alone is not the test's delivery path.
        report = {
            "id": "source-evidence-report",
            "parent_id": session_id,
            "agent_id": "source-evidence-worker",
            "status": "pending",
            "content": "队友补充资料核查结果，没有新的主人指令。",
        }
        runtime.store.put("team_report", report)
        await runtime.team.flush_reports(session_id)
        reports = await runtime._claim_inputs(session_id, "source-notice-request")
        assert len(reports) == 1
        assert reports[0]["source"] == "team:" + report["agent_id"]
        assert reports[0]["principal"] == "agent:" + report["agent_id"]
        assert runtime._attention_memory_failure(session_id) is None
        runtime.context_hooks.append(context)
        continued = await runtime._prepare_request(
            session_id, previous=previous, claimed_inputs=reports
        )
        assert context_calls == [session_id]
        assert continued.memory_failure == proof
        assert continued.reply_origin == previous.reply_origin
        assert continued.attention_routing == previous.attention_routing

        context_calls.clear()
        assert runtime.memory.forget_source(source_id)
        with pytest.raises(MemorySourceFailureExpired):
            await runtime._prepare_request(session_id, previous=continued, claimed_inputs=reports)
        assert context_calls == [] and seen == []
        assert not runtime.tasks and runtime.store.list("run") == []
        assert runtime.store.pending_inputs(session_id) == 0
        delivered = runtime.store.get("team_report", report["id"])
        assert delivered is not None and delivered["status"] == "delivered"
        assert runtime.store.list("system_log") == []
    finally:
        await pipeline.close()
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize("expiry", ["current", "before_claim", "after_claim"])
async def test_new_owner_input_is_processed_when_previous_notice_expires_around_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expiry: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox, pipeline = components(runtime)
    streaming, finish_notice = asyncio.Event(), asyncio.Event()
    claimed, finish_flush = asyncio.Event(), asyncio.Event()
    owner_id = ""
    owner_content, owner_answer = "请回答新主人的独立问题", "已处理新的主人问题。"
    original_flush = runtime.flush_task_plan_deliveries

    async def flush_after_real_claim() -> None:
        await original_flush()
        if not owner_id or claimed.is_set():
            return
        row = runtime.store.db.execute(
            "SELECT session_id,state FROM ingress WHERE id=?", (owner_id,)
        ).fetchone()
        if (
            row is not None
            and row["state"] == "delivered"
            and asyncio.current_task() is runtime.tasks.get(row["session_id"])
        ):
            # Store.claim_inputs and message publication have already happened.
            # Pause this real driver's extension flush, never claim or its guard.
            claimed.set()
            await finish_flush.wait()

    class ContinuationClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(list(messages))
            if len(self.seen) == 1:
                streaming.set()
                await finish_notice.wait()
                yield StreamEvent("done", turn=ModelTurn(content="资料整理失败，请核查来源。"))
                return
            assert any(
                message.role == "user"
                and message.source_message_id == owner_id
                and owner_content in message.content
                for message in messages
            )
            yield StreamEvent("done", turn=ModelTurn(content=owner_answer))

    monkeypatch.setattr(runtime, "flush_task_plan_deliveries", flush_after_real_claim)
    runtime.client_factory = lambda _: ContinuationClient([], seen)  # type: ignore[assignment,return-value]
    try:
        await runtime.startup()
        await inbox.start()
        source_id = await fail(pipeline)
        await asyncio.wait_for(streaming.wait(), 3)
        session_id = runtime.main_session()["id"]
        notice_run_id = runtime.run_ids[session_id]
        owner = await runtime.submit(owner_content, event_id="owner-during-source-notice")
        owner_id = owner["message"]["id"]
        assert owner["queued"] and owner["message"]["principal"] == "owner"
        assert runtime.store.pending_inputs(session_id) == 1
        if expiry == "before_claim":
            assert runtime.memory.forget_source(source_id)
        finish_notice.set()
        await asyncio.wait_for(claimed.wait(), 3)
        row = runtime.store.db.execute(
            "SELECT state,run_id FROM ingress WHERE id=?", (owner_id,)
        ).fetchone()
        assert row is not None and row["state"] == "delivered"
        assert runtime.store.pending_inputs(session_id) == 0
        assert (row["run_id"] == notice_run_id) is (expiry != "before_claim")
        if expiry == "after_claim":
            assert runtime.memory.forget_source(source_id)
        finish_flush.set()
        try:
            await eventually(
                lambda: not runtime.tasks and inbox.cursor == runtime.store.event_position()
            )
        except TimeoutError:
            # Keep one bounded, payload-free snapshot before cleanup destroys the
            # actual waiter. Do not turn an unexplained wait into a longer one.
            def task_view(task: asyncio.Task[Any] | None) -> dict[str, Any]:
                if task is None:
                    return {"present": False}
                return {
                    "done": task.done(),
                    "cancelling": task.cancelling(),
                    "stack": [
                        {
                            "file": Path(frame.f_code.co_filename).name,
                            "function": frame.f_code.co_name,
                            "line": frame.f_lineno,
                        }
                        for frame in task.get_stack(limit=2)
                    ],
                }

            pytest.fail(
                "owner_notice_settlement="
                + json.dumps(
                    {
                        "seen": len(seen),
                        "drivers": {
                            identifier: task_view(task)
                            for identifier, task in runtime.tasks.items()
                        },
                        "finalizers": [task_view(task) for task in runtime._session_finalizers],
                        "attention": inbox.status(),
                        "attention_task": task_view(inbox._task),
                        "bus_revision": runtime.bus.revision,
                        "event_position": runtime.store.event_position(),
                        "events": [
                            dict(row)
                            for row in runtime.store.db.execute(
                                "SELECT id,type FROM events ORDER BY id DESC LIMIT 8"
                            )
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

        # An archived/delivered receipt alone does not mean the model handled it.
        # Wait only for terminal drivers so loss fails these business assertions.
        assert len(seen) == 2
        assert any(
            message.role == "user"
            and message.source_message_id == owner_id
            and owner_content in message.content
            for message in seen[1]
        )
        messages = runtime.store.messages(session_id)
        archived = [message for message in messages if message["id"] == owner_id]
        assert len(archived) == 1 and archived[0]["content"] == owner_content
        replies = [
            message
            for message in messages
            if message["role"] == "assistant" and message["content"] == owner_answer
        ]
        assert len(replies) == 1 and replies[0]["reply_origin"] == archived[0]["reply_origin"]
        assert any(
            run["status"] == "completed" and run.get("result") == owner_answer
            for run in runtime.store.list("run")
        )
        assert runtime.store.pending_inputs(session_id) == 0
        assert runtime.store.list("system_log") == []
        assert runtime.system_events.status()["persisted"] == 0
        assert len(attention_inputs(runtime)) == 1 and len(failure_events(runtime.store)) == 1
    finally:
        finish_notice.set()
        finish_flush.set()
        await pipeline.close()
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize("forgotten", [None, 0, 1])
async def test_same_route_host_submissions_keep_distinct_claims_and_revoke_only_their_source(
    tmp_path: Path, forgotten: int | None
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox, pipeline = components(runtime)
    runtime.config.update({"models": {"main": {"model": ""}}})
    try:
        source_ids = [await fail(pipeline), await fail(pipeline)]
        proofs = [
            MemorySourceFailure.load(event["data"]["source_failure"], max_bytes=4096)
            for event in failure_events(runtime.store)
        ]
        assert len(proofs) == 2 and all(proof is not None for proof in proofs)
        receipts: list[dict[str, Any]] = []
        for index, proof in enumerate(proofs):
            assert proof is not None
            receipt = await runtime.submit_attention(
                "已保存资料，整理失败：" + proof.source_id,
                "host-collected-failure-" + str(index),
                objects=[],
                routing=proof.routing,
                memory_failure=proof,
            )
            assert receipt is not None
            receipts.append(receipt)
        assert receipts[0]["attention_routing"] == receipts[1]["attention_routing"]
        if forgotten is not None:
            runtime.memory.forget_source(source_ids[forgotten])
        session_id = runtime.main_session()["id"]
        claimed = await runtime._claim_inputs(session_id, "first-proof-claim")
        expected = 1 if forgotten == 0 else 0
        assert len(claimed) == 1 and claimed[0]["id"] == receipts[expected]["id"]
        active = runtime._attention_memory_failure(session_id)
        assert active is not None and active.source_id == source_ids[expected]
        remaining = await runtime._claim_inputs(session_id, "second-proof-claim")
        if forgotten is None:
            assert len(remaining) == 1 and remaining[0]["id"] == receipts[1]["id"]
        else:
            assert remaining == []
            inputs = attention_inputs(runtime)
            dropped = next(item for item in inputs if item["id"] == receipts[forgotten]["id"])
            assert dropped["state"] == "cancelled"
        assert seen == []
    finally:
        await pipeline.close()
        await inbox.close()
        await runtime.close()


async def test_notification_model_failure_does_not_create_a_memory_or_system_loop(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox, pipeline = components(runtime)

    class Broken(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(list(messages))
            raise ValueError("PRIVATE_NOTICE_MODEL_FAILURE")
            yield StreamEvent("done", turn=ModelTurn(content="unreachable"))

    runtime.client_factory = lambda _: Broken([], seen)  # type: ignore[assignment,return-value]
    try:
        await runtime.startup()
        await inbox.start()
        await fail(pipeline)
        await eventually(
            lambda: (
                len(seen) == 1
                and not runtime.tasks
                and runtime.system_events.status()["persisted"] == 1
                and inbox.cursor == runtime.store.event_position()
            )
        )
        assert len(attention_inputs(runtime)) == 1 and len(failure_events(runtime.store)) == 1
        logs = runtime.store.list("system_log")
        assert len(logs) == 1 and logs[0]["notify"] is False
        assert "PRIVATE_NOTICE_MODEL_FAILURE" not in json.dumps(logs)
    finally:
        await pipeline.close()
        await inbox.close()
        await runtime.close()


async def test_memory_event_selection_does_not_reinterpret_an_existing_explicit_list(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="不应主动处理未选择的记忆事项")])
    inbox, pipeline = components(runtime)
    runtime.config.update({"attention": {"events": ["reminder", "management", "update"]}})
    try:
        await runtime.startup()
        await inbox.start()
        source_id = await fail(pipeline)
        await eventually(lambda: inbox.cursor == runtime.store.event_position())
        assert seen == [] and attention_inputs(runtime) == []
        assert len(failure_events(runtime.store)) == 1
        job = pipeline.job(source_id)
        assert job is not None and job["state"] == "failed"
    finally:
        await pipeline.close()
        await inbox.close()
        await runtime.close()


async def test_application_lifecycle_rejects_a_retained_old_pipeline_callback(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {"updates": {"check_on_start": False}, "memory": {"auto_extract": True, "max_attempts": 1}}
    )
    application = MemoryApplication(runtime, lambda: MediaService(MediaConfig()))
    old = application.pipeline
    callback = old.failure_routing
    assert callback is not None and callback() is None
    try:
        await application.start()
        old.extractor = reject
        # The callback belongs to the live application, not to a copied caller context.
        assert callback() is not None
        source_id = collect(old)
        await asyncio.wait_for(old.wait(source_id), 3)
        event = failure_events(runtime.store)[0]
        await application.reconfigure()
        assert application.pipeline is not old and callback() is None
        assert application.pipeline.failure_routing is not None
        assert application.pipeline.failure_routing() is not None
        assert not old._publish_source_failure(
            source_id, event["data"]["source_failure"]["revision"], {"source_id": source_id}
        )
        assert len(failure_events(runtime.store)) == 1
    finally:
        await application.close()
        await runtime.close()


async def test_forgetting_while_waiting_for_capacity_never_enters_model_stream(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox, pipeline = components(runtime)
    entered = asyncio.Event()

    class CapacityClient(FakeClient):
        async def __aenter__(self) -> CapacityClient:
            # Runtime has prepared the real request. The unchanged capacity
            # semaphore is its next suspension before stream entry.
            entered.set()
            return self

    runtime.client_factory = lambda _: CapacityClient(  # type: ignore[assignment,return-value]
        [ModelTurn(content="失效来源不应进入模型")], seen
    )
    capacity = runtime.capacity
    held = 0
    try:
        await runtime.startup()
        capacity = runtime.capacity
        for _ in range(runtime.config.settings.limits.max_parallel_agents):
            await capacity.acquire()
            held += 1
        await inbox.start()
        source_id = await fail(pipeline)
        await asyncio.wait_for(entered.wait(), 3)
        assert capacity.locked() and seen == []
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1 and inputs[0]["state"] == "delivered"
        assert runtime.memory.forget_source(source_id)
        while held:
            capacity.release()
            held -= 1
        await eventually(
            lambda: not runtime.tasks and inbox.cursor == runtime.store.event_position()
        )
        assert seen == []
        runs = runtime.store.list("run")
        assert len(runs) == 1
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["cancel_reason"] == "memory_source_failure_expired"
        assert runs[0]["result"] == ""
        assert runtime.store.list("system_log") == []
        assert runtime.system_events.status()["persisted"] == 0
        assert len(attention_inputs(runtime)) == 1 and len(failure_events(runtime.store)) == 1
    finally:
        while held:
            capacity.release()
            held -= 1
        await pipeline.close()
        await inbox.close()
        await runtime.close()


@pytest.mark.parametrize("late_kind", ["text_delta", "done"])
async def test_forgetting_after_first_stream_chunk_rejects_late_output_without_error_loop(
    tmp_path: Path, late_kind: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox, pipeline = components(runtime)
    displayed, release = asyncio.Event(), asyncio.Event()
    attempted: list[str] = []
    first, late = "原文已保存，", "迟到的整理失败结论"

    class StreamingClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(list(messages))
            yield StreamEvent("text_delta", text=first)
            # The consumer has emitted the first chunk before requesting this
            # next event; only the provider is paused, never Runtime's guard.
            displayed.set()
            await release.wait()
            if late_kind == "text_delta":
                attempted.append("text_delta")
                yield StreamEvent("text_delta", text=late)
            attempted.append("done")
            yield StreamEvent("done", turn=ModelTurn(content=first + late))

    runtime.client_factory = lambda _: StreamingClient([], seen)  # type: ignore[assignment,return-value]
    try:
        await runtime.startup()
        await inbox.start()
        source_id = await fail(pipeline)
        await asyncio.wait_for(displayed.wait(), 3)
        session_id = runtime.main_session()["id"]
        shown = [
            event["data"]["content"]
            for event in runtime.store.events(session_id=session_id)
            if event["type"] == "message.delta"
        ]
        assert shown == [first] and len(seen) == 1
        assert runtime.memory.forget_source(source_id)
        release.set()
        await eventually(
            lambda: not runtime.tasks and inbox.cursor == runtime.store.event_position()
        )
        events = runtime.store.events(session_id=session_id)
        assert attempted == [late_kind]
        assert [
            event["data"]["content"] for event in events if event["type"] == "message.delta"
        ] == [first]
        assert not any(event["type"] in {"run.failed", "run.completed"} for event in events)
        assert [
            message
            for message in runtime.store.messages(session_id)
            if message["role"] == "assistant"
        ] == []
        runs = runtime.store.list("run")
        assert len(runs) == 1 and runs[0]["status"] == "cancelled"
        assert runs[0]["cancel_reason"] == "memory_source_failure_expired"
        assert runs[0]["result"] == "" and not runs[0].get("result_message_id")
        assert runtime.store.list("system_log") == []
        assert runtime.system_events.status()["persisted"] == 0
        assert len(seen) == 1 and len(attention_inputs(runtime)) == 1
        assert len(failure_events(runtime.store)) == 1
    finally:
        release.set()
        await pipeline.close()
        await inbox.close()
        await runtime.close()
