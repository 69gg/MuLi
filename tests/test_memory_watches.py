"""Finite local completion consumption; product admission lives in Runtime tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from muli.attachment_memory_source import attachment_source
from muli.config import MemoryWatchSettings
from muli.events import EventBus
from muli.evidence import EvidenceError
from muli.maintenance import Admission
from muli.memory import Memory
from muli.memory_pipeline import MemoryPipeline, MemoryPipelineConfig
from muli.memory_watches import (
    COMPLETION_EVENT,
    MATCH_EVENT,
    MAX_MATCH_PROOF_BYTES,
    MemoryWatches,
    MemoryWatchMatch,
    match_current,
    record_source_completion,
)
from muli.store import SQLITE_MAX_INTEGER, Store, new_id, now


@dataclass
class Harness:
    store: Store
    bus: EventBus
    pipeline: MemoryPipeline
    watches: MemoryWatches
    settings: MemoryWatchSettings
    budget: list[int]
    main_id: str

    def create(self, *, key: str = "first", terms: list[str] | None = None) -> dict[str, Any]:
        return self.watches.create(
            self.main_id,
            "real-owner-input",
            "传送带什么时候用功能关系？",
            ["传送带", "功能关系"] if terms is None else terms,
            key=key,
        )

    def collect(self, content: str = "例题：传送带题目先分析功能关系，再核对滑动阶段。") -> str:
        source = attachment_source(
            new_id(), content, hashlib.sha256(content.encode("utf-8")).hexdigest(), now()
        )
        self.pipeline.ingest(source)
        return str(source["id"])

    def consume(self) -> None:
        more = True
        while more:
            more, _ = self.watches._consume_page()

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.store.events(limit=1000) if event["type"] == kind]

    def matches(self) -> list[MemoryWatchMatch]:
        proofs = [
            MemoryWatchMatch.load(event["data"]["match"], max_bytes=MAX_MATCH_PROOF_BYTES)
            for event in self.events(MATCH_EVENT)
        ]
        assert all(proof is not None for proof in proofs)
        return [proof for proof in proofs if proof is not None]


def harness_at(path: Path) -> Harness:
    store = Store(path)
    bus, settings, budget = EventBus(store), MemoryWatchSettings(), [6000]
    pipeline = MemoryPipeline(Memory(store), bus, config=MemoryPipelineConfig(auto_extract=False))
    watches = MemoryWatches(
        store,
        bus,
        settings=lambda: settings,
        enabled=lambda: True,
        max_source_chars=lambda: 64000,
        max_output_chars=lambda: budget[0],
        admission=Admission(),
    )
    return Harness(store, bus, pipeline, watches, settings, budget, store.main_session()["id"])


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = harness_at(tmp_path / "watches.sqlite3")
    try:
        yield harness
    finally:
        await harness.watches.close()
        await harness.pipeline.close()
        harness.store.close()


async def test_completion_is_canonical_and_match_keeps_one_current_continuous_quote(
    rig: Harness,
) -> None:
    watch = rig.create()
    source_id = rig.collect()
    await rig.pipeline._run_job(source_id)
    events = rig.events(COMPLETION_EVENT)
    job = rig.pipeline.job(source_id)
    assert len(events) == 1 and job is not None and job["state"] == "done"
    completion = job["checkpoint"]["source_completion"]
    assert completion["event_id"] == events[0]["id"]
    assert rig.store.get("memory_source_completion", completion["id"]) == completion
    assert "例题" not in json.dumps(events, ensure_ascii=False)
    rig.consume()
    (proof,) = rig.matches()
    assert proof.watch_id == watch["id"] and proof.source_id == source_id
    assert all(term in proof.quote for term in watch["match_terms"])
    source = rig.pipeline.memory.source(source_id)
    assert source is not None and proof.quote in source["content"]
    assert match_current(rig.store, proof, max_source_chars=64000, max_output_chars=6000)
    assert json.loads(proof.detail())["read_tool"] == "memory_source"
    assert rig.watches.records(rig.main_id)[0]["created_after_event"] < proof.completion_event


async def test_completion_event_failure_rolls_back_done_and_first_token(
    rig: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = rig.collect()
    original = rig.store._event

    def reject_completion(
        event_type: str,
        data: dict[str, Any],
        session_id: str | None = None,
        *,
        objects: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        if event_type == COMPLETION_EVENT:
            raise RuntimeError("injected completion transaction failure")
        return original(event_type, data, session_id, objects=objects)

    monkeypatch.setattr(rig.store, "_event", reject_completion)
    await rig.pipeline._run_job(source_id)
    job = rig.pipeline.job(source_id)
    assert job is not None and job["state"] == "retry"
    assert "source_completion" not in job["checkpoint"]
    assert rig.events(COMPLETION_EVENT) == []
    assert rig.store.list("memory_source_completion") == []
    monkeypatch.setattr(rig.store, "_event", original)
    await rig.pipeline._run_job(source_id)
    assert len(rig.events(COMPLETION_EVENT)) == 1
    assert (rig.pipeline.job(source_id) or {})["state"] == "done"


async def test_done_survives_loss_of_async_wakeup_and_reopen_continues_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "restart.sqlite3"
    initial = harness_at(path)
    initial.create()
    source_id = initial.collect()
    original = initial.bus.emit

    async def stop_before_wakeup(
        event_type: str,
        data: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if event_type == "memory.updated":
            raise asyncio.CancelledError
        return await original(event_type, data, session_id)

    monkeypatch.setattr(initial.bus, "emit", stop_before_wakeup)
    try:
        with pytest.raises(asyncio.CancelledError):
            await initial.pipeline._run_job(source_id)
        assert (initial.pipeline.job(source_id) or {})["state"] == "done"
        assert len(initial.events(COMPLETION_EVENT)) == 1
        assert initial.matches() == []
    finally:
        await initial.watches.close()
        await initial.pipeline.close()
        initial.store.close()

    reopened = harness_at(path)
    try:
        reopened.consume()
        assert len(reopened.matches()) == 1
        reopened.consume()
        event = reopened.events(COMPLETION_EVENT)[0]
        await reopened.bus.emit(COMPLETION_EVENT, event["data"])
        reopened.consume()
        assert len(reopened.matches()) == 1
    finally:
        await reopened.watches.close()
        await reopened.pipeline.close()
        reopened.store.close()


async def test_old_completed_source_and_forged_reemission_are_not_new_work(rig: Harness) -> None:
    source_id = rig.collect()
    await rig.pipeline._run_job(source_id)
    old = rig.events(COMPLETION_EVENT)[0]
    rig.create()
    await rig.bus.emit(COMPLETION_EVENT, old["data"])
    rig.consume()
    assert rig.matches() == []
    assert rig.pipeline.ingest(rig.pipeline.memory.source(source_id) or {})["created"] is False
    await rig.pipeline._run_job(source_id)
    assert len(rig.events(COMPLETION_EVENT)) == 2  # One host completion and one forged copy.
    assert len(rig.store.list("memory_source_completion")) == 1


async def test_previously_queued_collection_can_complete_after_watch_creation(rig: Harness) -> None:
    source_id = rig.collect()
    rig.create()
    await rig.pipeline._run_job(source_id)
    rig.consume()
    assert [proof.source_id for proof in rig.matches()] == [source_id]


async def test_queued_or_legacy_done_without_a_token_is_never_backfilled(rig: Harness) -> None:
    source_id = rig.collect()
    source = rig.pipeline.memory.source(source_id)
    assert source is not None
    with rig.store.db:
        rig.store.db.execute("BEGIN IMMEDIATE")
        record_source_completion(rig.store, source_id, source["digest"], max_source_chars=64000)
    assert rig.events(COMPLETION_EVENT) == []
    with rig.store.db:
        rig.store.db.execute("UPDATE memory_jobs SET state='done' WHERE source_id=?", (source_id,))
    rig.create()
    await rig.pipeline._run_job(source_id)
    rig.consume()
    assert rig.events(COMPLETION_EVENT) == [] and rig.matches() == []


async def test_and_terms_must_share_a_bounded_quote_and_later_occurrences_are_considered(
    rig: Harness,
) -> None:
    rig.settings.max_quote_chars = 32
    rig.create()
    distant = rig.collect("传送带" + "普通背景" * 40 + "功能关系")
    local = rig.collect("传送带" + "普通背景" * 40 + "后面的传送带例题使用功能关系。")
    partial = rig.collect("只有功能关系的讲义，并未讨论另一条件。")
    for source_id in (distant, local, partial):
        await rig.pipeline._run_job(source_id)
    rig.consume()
    proofs = rig.matches()
    assert len(proofs) == 1 and proofs[0].source_id == local
    assert len(proofs[0].quote) <= 32 and "传送带" in proofs[0].quote


@pytest.mark.parametrize("change", ["cancel", "forget", "digest", "quote", "checkpoint"])
async def test_current_evidence_is_revoked_by_each_authority_boundary(
    rig: Harness,
    change: str,
) -> None:
    watch = rig.create()
    source_id = rig.collect()
    await rig.pipeline._run_job(source_id)
    rig.consume()
    (proof,) = rig.matches()
    if change == "cancel":
        cancelled = rig.watches.cancel(rig.main_id, watch["id"])
        assert cancelled["state"] == "cancelled" and cancelled["revision"] == 2
        assert (
            rig.watches.create(
                rig.main_id,
                "real-owner-input",
                watch["question"],
                watch["match_terms"],
                key="first",
            )["state"]
            == "cancelled"
        )
    elif change == "forget":
        rig.pipeline.memory.forget_source(source_id)
    elif change == "quote":
        with rig.store.db:
            rig.store.db.execute(
                "UPDATE memory_sources SET content=content||'改变原文' WHERE id=?", (source_id,)
            )
    elif change == "digest":
        with rig.store.db:
            rig.store.db.execute(
                "UPDATE memory_sources SET digest=? WHERE id=?", ("0" * 64, source_id)
            )
    else:
        with rig.store.db:
            rig.store.db.execute(
                "UPDATE memory_jobs SET checkpoint=json_remove(checkpoint,'$.source_completion') "
                "WHERE source_id=?",
                (source_id,),
            )
    assert not match_current(rig.store, proof, max_source_chars=64000, max_output_chars=6000)


@pytest.mark.parametrize("change", ["watch_id", "missing", "kind", "principal", "pointer"])
async def test_match_is_bound_to_the_watch_key_and_the_current_real_owner_main(
    rig: Harness,
    change: str,
) -> None:
    watch = rig.create()
    source_id = rig.collect()
    await rig.pipeline._run_job(source_id)
    rig.consume()
    (proof,) = rig.matches()
    with rig.store.db:
        if change == "watch_id":
            rig.store.db.execute(
                "UPDATE objects SET data=json_set(data,'$.id',?) WHERE kind='memory_watch' AND id=?",
                ("0" * 64, watch["id"]),
            )
        elif change == "missing":
            rig.store.db.execute(
                "DELETE FROM objects WHERE kind='session' AND id=?", (rig.main_id,)
            )
        elif change == "pointer":
            rig.store.db.execute(
                "UPDATE runtime_state SET value=? WHERE name='main_session'", (new_id(),)
            )
        else:
            rig.store.db.execute(
                "UPDATE objects SET data=json_set(data,?,?) WHERE kind='session' AND id=?",
                ("$." + change, "agent" if change == "kind" else "agent:forged", rig.main_id),
            )
    assert not match_current(rig.store, proof, max_source_chars=64000, max_output_chars=6000)


async def test_small_output_budget_defers_without_advancing_past_the_lead(rig: Harness) -> None:
    watch = rig.create()
    source_id = rig.collect()
    await rig.pipeline._run_job(source_id)
    event_id = rig.events(COMPLETION_EVENT)[0]["id"]
    rig.budget[0] = 200
    rig.consume()
    stored = rig.store.get("memory_watch", watch["id"])
    assert stored is not None and stored["after_event"] < event_id
    assert rig.watches.deferred and rig.matches() == []
    rig.budget[0] = 6000
    rig.consume()
    assert len(rig.matches()) == 1
    rig.consume()
    assert len(rig.matches()) == 1 and not rig.watches.deferred


async def test_reduced_output_budget_revokes_an_existing_whole_payload(rig: Harness) -> None:
    rig.create()
    source_id = rig.collect()
    await rig.pipeline._run_job(source_id)
    rig.consume()
    (proof,) = rig.matches()
    payload = {"id": proof.identifier, "match": proof.dump()}
    exact = len(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    assert match_current(rig.store, proof, max_source_chars=64000, max_output_chars=exact)
    assert not match_current(rig.store, proof, max_source_chars=64000, max_output_chars=exact - 1)


async def test_reduced_source_budget_defers_the_unread_completion_until_restored(
    rig: Harness,
) -> None:
    watch = rig.create()
    source_id = rig.collect("传送带和功能关系" + "普通原文" * 20)
    await rig.pipeline._run_job(source_id)
    event_id = rig.events(COMPLETION_EVENT)[0]["id"]
    rig.watches.max_source_chars = lambda: 10
    rig.consume()
    assert rig.watches.deferred and rig.matches() == []
    assert (rig.store.get("memory_watch", watch["id"]) or {})["after_event"] < event_id
    rig.watches.max_source_chars = lambda: 64000
    rig.consume()
    assert len(rig.matches()) == 1


async def test_registration_limits_and_key_identity_are_atomic(rig: Harness) -> None:
    watch = rig.create()
    assert rig.create() == watch
    with pytest.raises(ValueError, match="key"):
        rig.create(terms=["另一条件"])
    rig.settings.max_active = 1
    with pytest.raises(ValueError, match="上限"):
        rig.create(key="second")
    with pytest.raises(ValueError):
        rig.create(key="invalid", terms=["重复", "重复"])
    rig.budget[0] = 1000
    receipt = rig.watches.cancel(rig.main_id, watch["id"])
    assert receipt == {"id": watch["id"], "state": "cancelled", "revision": 2}
    assert (rig.store.get("memory_watch", watch["id"]) or {})["state"] == "cancelled"


async def test_lowest_legal_output_budget_can_cancel_a_large_question_without_losing_it(
    rig: Harness,
) -> None:
    question = "请长期保留这个问题的完整背景。" * 200
    watch = rig.watches.create(
        rig.main_id,
        "real-owner-input",
        question,
        ["传送带", "功能关系"],
        key="large",
    )
    assert len(json.dumps(watch, ensure_ascii=False)) > 1000
    rig.budget[0] = 1000
    receipt = rig.watches.cancel(rig.main_id, watch["id"])
    assert receipt == {"id": watch["id"], "state": "cancelled", "revision": 2}
    assert rig.watches.cancel(rig.main_id, watch["id"]) == receipt
    stored = rig.store.bounded_object(
        "memory_watch",
        watch["id"],
        max_bytes=MAX_MATCH_PROOF_BYTES,
        strict_json=True,
    )
    assert stored is not None and stored["question"] == question
    assert stored["match_terms"] == watch["match_terms"] and stored["state"] == "cancelled"


async def test_registration_reserves_the_exact_fixed_storage_boundary_for_cancellation(
    rig: Harness,
) -> None:
    rig.budget[0] = 100000
    template = rig.watches.create(
        rig.main_id,
        "real-owner-input",
        "x",
        ["a"],
        key="template",
    )
    reserved = {
        **template,
        "state": "cancelled",
        "revision": 2,
        "after_event": SQLITE_MAX_INTEGER,
    }
    reserved_size = len(json.dumps(reserved, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    question = "x" * (MAX_MATCH_PROOF_BYTES - reserved_size + 1)
    watch = rig.watches.create(
        rig.main_id,
        "real-owner-input",
        question,
        ["a"],
        key="boundary",
    )
    final_reserved = {
        **watch,
        "state": "cancelled",
        "revision": 2,
        "after_event": SQLITE_MAX_INTEGER,
    }
    assert (
        len(json.dumps(final_reserved, ensure_ascii=False).encode("utf-8")) == MAX_MATCH_PROOF_BYTES
    )
    before = len(rig.store.list("memory_watch"))
    with pytest.raises(EvidenceError, match="固定存储预算"):
        rig.watches.create(
            rig.main_id,
            "real-owner-input",
            question + "x",
            ["a"],
            key="overflow",
        )
    assert len(rig.store.list("memory_watch")) == before
    rig.budget[0] = 1000
    assert rig.watches.cancel(rig.main_id, watch["id"]) == {
        "id": watch["id"],
        "state": "cancelled",
        "revision": 2,
    }
    stored = rig.store.bounded_object("memory_watch", watch["id"], max_bytes=MAX_MATCH_PROOF_BYTES)
    assert stored is not None and stored["question"] == question


async def test_worker_uses_completion_wakeup_and_does_not_poll_when_idle(rig: Harness) -> None:
    rig.create()
    entered = asyncio.Event()
    original = rig.watches._wait
    waits = 0

    async def waiting(revision: int) -> None:
        nonlocal waits
        if revision == rig.bus.revision:
            waits += 1
            entered.set()
        await original(revision)

    rig.watches._wait = waiting  # type: ignore[method-assign]
    await rig.watches.start()
    await asyncio.wait_for(entered.wait(), 3)
    assert waits == 1
    entered.clear()
    source_id = rig.collect()
    await rig.pipeline._run_job(source_id)
    async with asyncio.timeout(3):
        while not rig.matches():
            await asyncio.sleep(0)
    await asyncio.wait_for(entered.wait(), 3)
    before = waits
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert waits == before and len(rig.matches()) == 1
