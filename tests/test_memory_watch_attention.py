"""Current canonical watch proofs cross real Attention/Runtime boundaries."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import attention_inputs, eventually
from test_memory_pipeline import extraction
from test_runtime import FakeClient, make_runtime

from muli.attachment_memory_source import attachment_source
from muli.attention import AttentionInbox
from muli.conversation import Conversation
from muli.memory_pipeline import MemoryPipeline
from muli.memory_watches import (
    MATCH_EVENT,
    MAX_MATCH_PROOF_BYTES,
    MemoryWatches,
    MemoryWatchMatch,
    MemoryWatchMatchExpired,
)
from muli.proactive import FrozenRouting
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store import new_id, now
from muli.system_events import attention_source


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("memory watch attention must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@dataclass(slots=True)
class WatchRig:
    runtime: Runtime
    inbox: AttentionInbox
    pipeline: MemoryPipeline
    watches: MemoryWatches
    seen: list[list[Message]]

    async def close(self) -> None:
        await self.watches.close()
        await self.pipeline.close()
        await self.inbox.close()
        await self.runtime.close()


def components(tmp_path: Path, turns: list[ModelTurn] | None = None) -> WatchRig:
    runtime, seen = make_runtime(tmp_path, turns or [])
    runtime.config.update(
        {
            "attention": {"events": ["memory"], "coalesce_seconds": 0},
            "memory": {"auto_extract": False},
            "models": {"main": {"model": ""}},
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
        }
    )

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        return extraction(source, layer="semantic")

    pipeline = MemoryPipeline(runtime.memory, runtime.bus, extractor=extract)
    watches = MemoryWatches(
        runtime.store,
        runtime.bus,
        settings=lambda: runtime.config.settings.memory_watches,
        enabled=lambda: True,
        max_source_chars=lambda: runtime.config.settings.memory.max_source_chars,
        max_output_chars=lambda: runtime.config.settings.limits.max_output_chars,
        admission=runtime.maintenance.admission,
    )
    runtime.memory_watch_guard = lambda _: True
    return WatchRig(runtime, AttentionInbox(runtime), pipeline, watches, seen)


async def matched(
    rig: WatchRig, key: str = "first", *, text: str = "新资料原文：词甲与词乙是连续的一段线索。"
) -> tuple[MemoryWatchMatch, dict[str, Any]]:
    runtime = rig.runtime
    main_id = runtime.main_session()["id"]
    question = "请持续关注这两个条件对应的问题：" + key
    owner, _ = runtime.conversation.enqueue(
        main_id,
        question,
        source="web",
        principal="owner",
        mode="ask",
        max_pending=runtime.config.settings.limits.max_pending_runs,
    )
    # Unit fixture uses real ingress provenance but leaves model/tool creation
    # authorization to the application's separate business continuity tests.
    claimed = runtime.store.claim_inputs(main_id, "watch-create-" + key, limit=1)
    assert len(claimed) == 1 and claimed[0]["id"] == owner["id"]
    watch = rig.watches.create(main_id, owner["id"], question, ["词甲", "词乙"], key=key)
    source = attachment_source(new_id(), text, hashlib.sha256(text.encode()).hexdigest(), now())
    rig.pipeline.ingest(source)
    await rig.pipeline._run_job(source["id"])
    for _ in range(32):
        more, _ = rig.watches._consume_page()
        if not more:
            break
    else:
        raise AssertionError("有限 watch 夹具未能消费到终态")
    event = next(
        item
        for item in runtime.store.events(limit=1000)
        if item["type"] == MATCH_EVENT and item["data"]["match"]["watch_id"] == watch["id"]
    )
    proof = MemoryWatchMatch.load(event["data"]["match"], max_bytes=MAX_MATCH_PROOF_BYTES)
    assert proof is not None
    await runtime.bus.notify()
    return proof, event


def revoke(rig: WatchRig, proof: MemoryWatchMatch, change: str) -> None:
    runtime = rig.runtime
    if change == "cancel":
        rig.watches.cancel(runtime.main_session()["id"], proof.watch_id)
    elif change == "forget":
        assert runtime.memory.forget_source(proof.source_id)
    elif change == "disabled":
        runtime.config.update({"memory": {"enabled": False}})
    elif change == "generation":
        # A new live application can accept later durable events; it cannot
        # resurrect a PreparedRequest frozen against the old callback identity.
        runtime.memory_watch_guard = lambda _: True
    elif change == "budget":
        runtime.config.update({"attention": {"max_chars": 512}})
    else:
        raise AssertionError(change)


async def test_only_current_canonical_watch_event_is_admitted_as_local_evidence(
    tmp_path: Path,
) -> None:
    rig = components(tmp_path)
    try:
        proof, event = await matched(rig)
        source = attention_source(
            rig.runtime.store,
            event,
            ("memory",),
            max_output_chars=6000,
            memory_source_chars=64000,
        )
        assert source is not None and source.memory_watch_proof == proof
        assert source.routing == FrozenRouting()
        assert proof.question in source.detail and proof.quote in source.detail
        forged = replace(proof, quote="由事件调用者伪造的引用")
        for invalid in (
            {**event, "data": {"id": proof.identifier, "match": forged.dump()}},
            {**event, "session_id": rig.runtime.main_session()["id"]},
            {**event, "type": "memory.updated"},
        ):
            assert (
                attention_source(
                    rig.runtime.store,
                    invalid,
                    ("memory",),
                    max_output_chars=6000,
                    memory_source_chars=64000,
                )
                is None
            )
        with pytest.raises(ValueError, match="宿主证明"):
            Conversation.payload(
                rig.runtime.main_session()["id"],
                "伪装成主人的回访证明",
                source="web",
                principal="owner",
                mode="ask",
                attention_memory_watch=proof,
            )
        rig.runtime.memory_watch_guard = None
        with pytest.raises(MemoryWatchMatchExpired):
            await rig.runtime.submit_attention(
                proof.detail(), "no-current-watch-application", objects=[], memory_watch=proof
            )
        assert attention_inputs(rig.runtime) == [] and rig.seen == []
    finally:
        await rig.close()


async def test_real_attention_request_contains_the_same_complete_local_lead_once(
    tmp_path: Path,
) -> None:
    answer = "新收藏资料中出现了先前问题的字面线索，请核查原文。"
    rig = components(tmp_path, [ModelTurn(content=answer)])
    try:
        await rig.runtime.startup()
        await rig.inbox.start()
        proof, event = await matched(rig)
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        await rig.runtime.wake_pending_inputs()
        await eventually(lambda: len(rig.seen) == 1 and not rig.runtime.tasks)
        assert any(
            message.role == "user"
            and all(
                value in message.content
                for value in (proof.question, proof.source_id, proof.quote, "memory_source")
            )
            for message in rig.seen[0]
        )
        inputs = attention_inputs(rig.runtime)
        assert len(inputs) == 1 and inputs[0]["state"] == "delivered"
        assert inputs[0]["attention_memory_watch"] == proof.dump()
        messages = rig.runtime.store.messages(rig.runtime.main_session()["id"])
        finals = [message for message in messages if message["role"] == "assistant"]
        assert len(finals) == 1 and finals[0]["content"] == answer
        assert finals[0]["delivery_routes"] == []
        await rig.runtime.bus.emit(MATCH_EVENT, event["data"])
        await eventually(lambda: rig.inbox.cursor == rig.runtime.store.event_position())
        assert len(rig.seen) == 1 and len(attention_inputs(rig.runtime)) == 1
    finally:
        await rig.close()


async def test_attention_start_before_watch_application_retains_durable_event(
    tmp_path: Path,
) -> None:
    rig = components(tmp_path)
    try:
        proof, event = await matched(rig)
        rig.runtime.memory_watch_guard = None
        await rig.runtime.startup()
        await rig.inbox.start()
        await eventually(lambda: rig.inbox.status()["waiting"] == "producer")
        assert rig.inbox.cursor < event["id"] and attention_inputs(rig.runtime) == []
        rig.runtime.memory_watch_guard = lambda _: True
        await rig.runtime.bus.notify()
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        assert attention_inputs(rig.runtime)[0]["attention_memory_watch"] == proof.dump()
    finally:
        await rig.close()


@pytest.mark.parametrize("limit", ["attention", "output"])
async def test_small_budget_defers_complete_event_and_restoration_admits_once(
    tmp_path: Path, limit: str
) -> None:
    rig = components(tmp_path)
    try:
        proof, event = await matched(rig, text="词甲" + "连续原文" * 280 + "词乙")
        changes = (
            {"attention": {"max_chars": 512}}
            if limit == "attention"
            else {"limits": {"max_output_chars": 1000}}
        )
        rig.runtime.config.update(changes)
        await rig.runtime.startup()
        await rig.inbox.start()
        await eventually(lambda: rig.inbox.status()["waiting"] == "budget")
        assert rig.inbox.cursor < event["id"]
        assert attention_inputs(rig.runtime) == [] and rig.seen == []
        rig.runtime.config.update(
            {"attention": {"max_chars": 4000}, "limits": {"max_output_chars": 24000}}
        )
        await rig.runtime.bus.notify()
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        inputs = attention_inputs(rig.runtime)
        assert inputs[0]["attention_memory_watch"] == proof.dump()
        assert proof.question in inputs[0]["content"] and proof.quote in inputs[0]["content"]
        assert inputs[0]["attention_routing"] == FrozenRouting().dump()
        await rig.runtime.bus.emit(MATCH_EVENT, event["data"])
        await eventually(lambda: rig.inbox.cursor == rig.runtime.store.event_position())
        assert len(attention_inputs(rig.runtime)) == 1
        assert len(rig.runtime.store.list("attention_receipt")) == 1
    finally:
        await rig.close()


async def test_attention_coalescing_keeps_each_watch_source_proof_separate(tmp_path: Path) -> None:
    rig = components(tmp_path)
    try:
        await matched(rig, "one")
        await matched(rig, "two")
        events = [
            item for item in rig.runtime.store.events(limit=1000) if item["type"] == MATCH_EVENT
        ]
        assert len(events) == 3
        await rig.runtime.startup()
        await rig.inbox.start()
        main_id = rig.runtime.main_session()["id"]
        for index in range(3):
            await eventually(lambda count=index + 1: len(attention_inputs(rig.runtime)) == count)
            inputs = attention_inputs(rig.runtime)
            proof = inputs[-1]["attention_memory_watch"]
            assert proof == events[index]["data"]["match"]
            assert inputs[-1]["content"].count('"record_id"') == 1
            claimed = await rig.runtime._claim_inputs(main_id, "coalesced-proof-" + str(index))
            assert len(claimed) == 1 and claimed[0]["id"] == inputs[-1]["id"]
            await rig.runtime.bus.notify()
        await eventually(lambda: rig.inbox.cursor == rig.runtime.store.event_position())
        assert rig.seen == [] and len(rig.runtime.store.list("attention_receipt")) == 3
    finally:
        await rig.close()


@pytest.mark.parametrize("change", ["cancel", "forget", "disabled"])
async def test_waiting_admission_rechecks_after_configuration_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    rig = components(tmp_path)
    entered = asyncio.Event()
    original = rig.runtime.submit_attention

    async def submitting(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        entered.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(rig.runtime, "submit_attention", submitting)
    held = False
    try:
        await rig.runtime.startup()
        await rig.inbox.start()
        await eventually(lambda: rig.inbox.status()["waiting"] == "events")
        await rig.runtime.configuration_lock.acquire()
        held = True
        proof, _ = await matched(rig)
        await asyncio.wait_for(entered.wait(), 3)
        revoke(rig, proof, change)
        rig.runtime.configuration_lock.release()
        held = False
        await eventually(lambda: rig.inbox.cursor == rig.runtime.store.event_position())
        assert attention_inputs(rig.runtime) == [] and rig.seen == []
    finally:
        if held:
            rig.runtime.configuration_lock.release()
        await rig.close()


@pytest.mark.parametrize("cancelled", [None, 0, 1])
async def test_same_web_route_keeps_proofs_and_owner_input_in_independent_claims(
    tmp_path: Path, cancelled: int | None
) -> None:
    rig = components(tmp_path)
    try:
        proofs = [(await matched(rig, str(index)))[0] for index in range(2)]
        receipts: list[dict[str, Any]] = []
        for index, proof in enumerate(proofs):
            receipt = await rig.runtime.submit_attention(
                proof.detail(),
                "independent-watch-" + str(index),
                objects=[],
                routing=FrozenRouting(),
                memory_watch=proof,
            )
            assert receipt is not None
            receipts.append(receipt)
        main_id = rig.runtime.main_session()["id"]
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        # Keep the real configured owner admission pending until this test's
        # explicit replay boundaries, without replacing Store.claim_inputs.
        with rig.runtime._stopping_session(main_id):
            owner = await rig.runtime.submit("主人另一个独立问题", event_id="separate-owner-input")
        assert owner["queued"] and owner["run_id"] is None
        if cancelled is not None:
            revoke(rig, proofs[cancelled], "cancel")
        claimed_ids: list[str] = []
        for index in range(3):
            claimed = await rig.runtime._claim_inputs(main_id, "watch-claim-" + str(index))
            assert len(claimed) <= 1
            claimed_ids.extend(item["id"] for item in claimed)
        expected = [
            receipt["id"] for index, receipt in enumerate(receipts) if index != cancelled
        ] + [owner["message"]["id"]]
        assert claimed_ids == expected
        if cancelled is not None:
            dropped = next(
                item
                for item in attention_inputs(rig.runtime)
                if item["id"] == receipts[cancelled]["id"]
            )
            assert dropped["state"] == "cancelled"
        assert rig.seen == []
    finally:
        await rig.close()


@pytest.mark.parametrize("change", ["cancel", "forget", "disabled", "generation", "budget"])
async def test_expiry_after_context_hook_await_never_enters_model(
    tmp_path: Path, change: str
) -> None:
    rig = components(tmp_path, [ModelTurn(content="已撤销的线索不得进入模型")])
    entered, release = asyncio.Event(), asyncio.Event()
    text = "词甲" + "完整原文" * 180 + "词乙"

    async def context(session_id: str, messages: list[Message]) -> list[Message]:
        entered.set()
        await release.wait()
        return messages

    rig.runtime.context_hooks.append(context)
    try:
        await rig.runtime.startup()
        await rig.inbox.start()
        proof, _ = await matched(rig, text=text)
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        await rig.runtime.wake_pending_inputs()
        await asyncio.wait_for(entered.wait(), 3)
        revoke(rig, proof, change)
        release.set()
        await eventually(lambda: not rig.runtime.tasks)
        assert rig.seen == []
        runs = rig.runtime.store.list("run")
        assert len(runs) == 1 and runs[0]["status"] == "cancelled"
        assert runs[0]["cancel_reason"] == "memory_watch_match_expired"
        assert runs[0]["result"] == ""
        assert rig.runtime.store.list("system_log") == []
    finally:
        release.set()
        await rig.close()


async def test_team_report_continuation_retains_original_lifecycle_guard(tmp_path: Path) -> None:
    rig = components(tmp_path)
    context_calls: list[str] = []

    async def context(session_id: str, messages: list[Message]) -> list[Message]:
        context_calls.append(session_id)
        return messages

    try:
        proof, _ = await matched(rig)
        receipt = await rig.runtime.submit_attention(
            proof.detail(), "watch-before-team-report", objects=[], memory_watch=proof
        )
        assert receipt is not None
        main_id = rig.runtime.main_session()["id"]
        await rig.runtime._claim_inputs(main_id, "watch-before-team-report")
        previous = await rig.runtime._prepare_request(main_id)
        report = {
            "id": "watch-evidence-report",
            "parent_id": main_id,
            "agent_id": "watch-evidence-worker",
            "status": "pending",
            "content": "队友补充资料核查证据，不是主人新授权。",
        }
        rig.runtime.store.put("team_report", report)
        await rig.runtime.team.flush_reports(main_id)
        claimed = await rig.runtime._claim_inputs(main_id, "watch-before-team-report")
        assert len(claimed) == 1 and claimed[0]["source"] == "team:" + report["agent_id"]
        rig.runtime.context_hooks.append(context)
        continued = await rig.runtime._prepare_request(
            main_id, previous=previous, claimed_inputs=claimed
        )
        assert continued.memory_watch == proof
        assert (continued.memory_watch_guard is previous.memory_watch_guard) is True
        assert continued.reply_origin == previous.reply_origin
        context_calls.clear()
        revoke(rig, proof, "generation")
        with pytest.raises(MemoryWatchMatchExpired):
            await rig.runtime._prepare_request(main_id, previous=continued, claimed_inputs=claimed)
        assert context_calls == [] and rig.seen == []
    finally:
        await rig.close()


@pytest.mark.parametrize("change", ["cancel", "forget", "disabled", "generation", "budget"])
@pytest.mark.parametrize("late_kind", ["text_delta", "done"])
async def test_revoked_stream_never_publishes_late_chunk_or_persistent_partial(
    tmp_path: Path, change: str, late_kind: str
) -> None:
    rig = components(tmp_path)
    displayed, release = asyncio.Event(), asyncio.Event()
    attempted: list[str] = []
    first, late = "找到同一问题的资料线索，", "这段过期答复不得存档"

    class StreamingClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(list(messages))
            yield StreamEvent("text_delta", text=first)
            displayed.set()
            await release.wait()
            if late_kind == "text_delta":
                attempted.append("text_delta")
                yield StreamEvent("text_delta", text=late)
            attempted.append("done")
            yield StreamEvent("done", turn=ModelTurn(content=first + late))

    rig.runtime.client_factory = lambda _: StreamingClient(  # type: ignore[assignment,return-value]
        [], rig.seen
    )
    try:
        await rig.runtime.startup()
        await rig.inbox.start()
        proof, _ = await matched(rig, text="词甲" + "连续原文" * 180 + "词乙")
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        await rig.runtime.wake_pending_inputs()
        await asyncio.wait_for(displayed.wait(), 3)
        revoke(rig, proof, change)
        release.set()
        await eventually(lambda: not rig.runtime.tasks)
        assert attempted == [late_kind]
        assert len(rig.seen) == 1
        assert any(proof.source_id in message.content for message in rig.seen[0])
        events = rig.runtime.store.events(session_id=rig.runtime.main_session()["id"])
        assert [
            event["data"]["content"] for event in events if event["type"] == "message.delta"
        ] == [first]
        assert not any(event["type"] in {"run.failed", "run.completed"} for event in events)
        assert not any(
            message["role"] == "assistant"
            for message in rig.runtime.store.messages(rig.runtime.main_session()["id"])
        )
        runs = rig.runtime.store.list("run")
        assert len(runs) == 1 and runs[0]["status"] == "cancelled"
        assert runs[0]["cancel_reason"] == "memory_watch_match_expired"
        assert runs[0]["result"] == "" and not runs[0].get("result_message_id")
        assert rig.runtime.store.list("system_log") == []
    finally:
        release.set()
        await rig.close()


async def test_cancelling_pending_watch_preserves_and_answers_real_owner_input(
    tmp_path: Path,
) -> None:
    owner_text, answer = "请回答主人现在的新问题", "新问题已经处理。"
    rig = components(tmp_path, [ModelTurn(content=answer)])
    try:
        await rig.runtime.startup()
        await rig.inbox.start()
        proof, _ = await matched(rig)
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        rig.runtime.config.update({"models": {"main": {"model": "main"}}})
        # Runtime.submit still validates the configured model and journals the
        # true owner input. Resume its driver only after revoking the old notice.
        with rig.runtime._stopping_session(rig.runtime.main_session()["id"]):
            owner = await rig.runtime.submit(owner_text, event_id="owner-after-pending-watch")
            revoke(rig, proof, "cancel")
        assert owner["queued"] and owner["run_id"] is None
        await rig.runtime.wake_pending_inputs()
        await eventually(lambda: len(rig.seen) == 1 and not rig.runtime.tasks)
        assert attention_inputs(rig.runtime)[0]["state"] == "cancelled"
        assert any(
            message.role == "user" and message.source_message_id == owner["message"]["id"]
            for message in rig.seen[0]
        )
        assert all(proof.source_id not in message.content for message in rig.seen[0])
        messages = rig.runtime.store.messages(rig.runtime.main_session()["id"])
        assert (
            len(
                [
                    message
                    for message in messages
                    if message["role"] == "assistant" and message["content"] == answer
                ]
            )
            == 1
        )
        assert rig.runtime.store.pending_inputs(rig.runtime.main_session()["id"]) == 0
    finally:
        await rig.close()
