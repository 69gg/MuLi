"""Canonical origin metadata; application authorization stays in real journeys."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import attention_inputs, eventually
from test_memory_watch_attention import WatchRig, components
from test_report_origin import route

from muli.attachment_memory_source import attachment_source
from muli.attention import _HEADER
from muli.conversation import Conversation
from muli.memory_watches import (
    MATCH_EVENT,
    MAX_MATCH_PROOF_BYTES,
    MemoryWatchMatch,
    MemoryWatchMatchExpired,
    match_current,
    match_routing,
)
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin
from muli.store import new_id, now
from muli.system_events import attention_source


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Origin metadata policy must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def bound_watch(rig: WatchRig, key: str = "original") -> tuple[dict[str, Any], FrozenRouting]:
    runtime = rig.runtime
    main = runtime.main_session()["id"]
    owner, _ = runtime.conversation.enqueue(
        main,
        "持续留意词甲与词乙的原文线索",
        source="channel:onebot:bot",
        principal="owner",
        route=route(),
        mode="ask",
        max_pending=3,
    )
    claimed = runtime.store.claim_inputs(main, "registration-" + key, limit=1)
    assert len(claimed) == 1 and claimed[0]["id"] == owner["id"]
    routing = FrozenRouting.bound(ReplyOrigin.from_input(owner))
    assert routing.classification == "bound"
    receipt = rig.watches.create(
        main,
        owner["id"],
        "持续留意词甲与词乙",
        ["词甲", "词乙"],
        key=key,
        routing=routing,
    )
    return receipt, routing


async def bound_match(
    rig: WatchRig, key: str = "original", *, text: str = "原资料：词甲与词乙同属连续线索。"
) -> tuple[MemoryWatchMatch, dict[str, Any], FrozenRouting]:
    receipt, routing = bound_watch(rig, key)
    source = attachment_source(new_id(), text, hashlib.sha256(text.encode()).hexdigest(), now())
    rig.pipeline.ingest(source)
    await rig.pipeline._run_job(source["id"])
    for _ in range(32):
        more, _ = rig.watches._consume_page()
        if not more:
            break
    else:
        raise AssertionError("Finite origin fixture did not consume its completion")
    event = next(
        event
        for event in rig.runtime.store.events(limit=1000)
        if event["type"] == MATCH_EVENT
        and event["data"]["match"]["watch_id"] == receipt["id"]
        and event["data"]["match"]["source_id"] == source["id"]
    )
    proof = MemoryWatchMatch.load(event["data"]["match"], max_bytes=MAX_MATCH_PROOF_BYTES)
    assert proof is not None
    return proof, event, routing


@pytest.mark.parametrize(
    "change", ["extra", "unknown", "independent", "target", "account", "sender", "input", "version"]
)
async def test_raw_v2_routing_must_be_canonical_and_match_real_owner_input(
    tmp_path: Path, change: str
) -> None:
    rig = components(tmp_path)
    try:
        proof, event, routing = await bound_match(rig)
        original = rig.runtime.store.get("memory_watch", proof.watch_id)
        assert original is not None and match_routing(rig.runtime.store, proof) == routing
        assert (
            attention_source(
                rig.runtime.store,
                event,
                ("memory",),
                max_output_chars=6000,
                memory_source_chars=64000,
            )
            is not None
        )
        forged = deepcopy(original)
        value = forged["routing"]
        if change == "extra":
            value["target"] = "8"
        elif change in {"unknown", "independent"}:
            value["classification"] = change
        elif change in {"target", "account", "sender"}:
            field = {"target": "target", "account": "account_id", "sender": "sender_id"}[change]
            value["destination"]["route"][field] = "8"
        elif change == "input":
            value["source_input_id"] = "another-owner-input"
        else:
            value["v"] = 2
        rig.runtime.store.put("memory_watch", forged)
        assert match_routing(rig.runtime.store, proof) is None
        assert not match_current(
            rig.runtime.store, proof, max_source_chars=64000, max_output_chars=6000
        )
        assert (
            attention_source(
                rig.runtime.store,
                event,
                ("memory",),
                max_output_chars=6000,
                memory_source_chars=64000,
            )
            is None
        )
        with pytest.raises(ValueError):
            rig.watches.records(rig.runtime.main_session()["id"])
        rig.runtime.store.put("memory_watch", original)
        assert match_routing(rig.runtime.store, proof) == routing
    finally:
        await rig.close()


async def test_old_v1_never_acquires_channel_preference_and_same_key_cannot_change_it(
    tmp_path: Path,
) -> None:
    rig = components(tmp_path)
    try:
        proof, _, routing = await bound_match(rig)
        stored = rig.runtime.store.get("memory_watch", proof.watch_id)
        assert stored is not None
        legacy = {key: value for key, value in stored.items() if key != "routing"}
        legacy["version"] = 1
        rig.runtime.store.put("memory_watch", legacy)
        assert match_routing(rig.runtime.store, proof) == FrozenRouting()
        with pytest.raises(ValueError):
            rig.watches.create(
                stored["main_session_id"],
                stored["created_input_id"],
                stored["question"],
                stored["match_terms"],
                key=stored["key"],
                routing=routing,
            )
        assert rig.runtime.store.get("memory_watch", proof.watch_id) == legacy
        replay = rig.watches.create(
            stored["main_session_id"],
            stored["created_input_id"],
            stored["question"],
            stored["match_terms"],
            key=stored["key"],
        )
        assert replay == legacy
    finally:
        await rig.close()


async def test_event_route_and_borrowed_watch_proof_cannot_replace_canonical_origin(
    tmp_path: Path,
) -> None:
    rig = components(tmp_path)
    try:
        proof, event, routing = await bound_match(rig)
        event = deepcopy(event)
        event["data"]["routing"] = FrozenRouting.bound(
            ReplyOrigin.load({"kind": "channel", "input_id": "forged", "route": route("8")})
        ).dump()
        source = attention_source(
            rig.runtime.store,
            event,
            ("memory",),
            max_output_chars=6000,
            memory_source_chars=64000,
        )
        assert source is not None and source.routing == routing
        detail = json.loads(source.detail)
        assert detail["reply_preference"]["origin"] == routing.origin.dump()
        for wrong in (FrozenRouting(), replace(routing, origin=ReplyOrigin())):
            with pytest.raises(MemoryWatchMatchExpired):
                await rig.runtime.submit_attention(
                    "不接纳错误来源",
                    "forged-route",
                    objects=[],
                    memory_watch=proof,
                    routing=wrong,
                )
        with pytest.raises(ValueError):
            rig.runtime.conversation.enqueue(
                rig.runtime.main_session()["id"],
                "不能绕过运行时",
                source="system:attention",
                principal="system:attention",
                mode="ask",
                max_pending=3,
                attention_memory_watch=proof,
                attention_routing=FrozenRouting(),
                internal=True,
            )
        assert attention_inputs(rig.runtime) == []
    finally:
        await rig.close()


async def test_original_owner_record_expiry_is_readonly_and_never_retargets(tmp_path: Path) -> None:
    rig = components(tmp_path)
    try:
        proof, event, routing = await bound_match(rig)
        with rig.runtime.store.db:
            rig.runtime.store.db.execute(
                "UPDATE ingress SET state='pending' WHERE id=?",
                (routing.origin.input_id,),
            )
        before = rig.runtime.store.db.total_changes
        assert match_routing(rig.runtime.store, proof) is None
        assert (
            attention_source(
                rig.runtime.store,
                event,
                ("memory",),
                max_output_chars=6000,
                memory_source_chars=64000,
            )
            is None
        )
        assert rig.runtime.store.db.total_changes == before
        with pytest.raises(MemoryWatchMatchExpired):
            await rig.runtime.submit_attention(
                "旧输入不再是已处理主人输入",
                "expired-original",
                objects=[],
                routing=routing,
                memory_watch=proof,
            )
        assert attention_inputs(rig.runtime) == []
    finally:
        await rig.close()


@pytest.mark.parametrize(
    "field", ["source", "name", "account_id", "sender_id", "target", "extra", "saved_extra"]
)
async def test_saved_reply_origin_cannot_hide_a_changed_raw_owner_source_or_route(
    tmp_path: Path,
    field: str,
) -> None:
    rig = components(tmp_path)
    try:
        proof, _, routing = await bound_match(rig)
        db = rig.runtime.store.db
        original = json.loads(
            db.execute(
                "SELECT payload FROM ingress WHERE id=?",
                (routing.origin.input_id,),
            ).fetchone()[0]
        )
        original["reply_origin"] = routing.origin.dump()
        with db:
            db.execute(
                "UPDATE ingress SET payload=? WHERE id=?",
                (
                    json.dumps(original, ensure_ascii=False),
                    routing.origin.input_id,
                ),
            )
        assert match_routing(rig.runtime.store, proof) == routing
        changed = deepcopy(original)
        if field == "source":
            changed["source"] = "channel:onebot:another-account"
        elif field == "saved_extra":
            changed["reply_origin"]["target"] = "8"
        else:
            changed["route"][field] = "different"
        with db:
            db.execute(
                "UPDATE ingress SET payload=? WHERE id=?",
                (
                    json.dumps(changed, ensure_ascii=False),
                    routing.origin.input_id,
                ),
            )
        assert match_routing(rig.runtime.store, proof) is None
        with pytest.raises(ValueError):
            rig.watches.records(rig.runtime.main_session()["id"])
        assert (
            rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        )
    finally:
        await rig.close()


async def test_static_payload_cannot_grant_bound_watch_preference(tmp_path: Path) -> None:
    rig = components(tmp_path)
    try:
        proof, _, routing = await bound_match(rig)
        with pytest.raises(ValueError):
            Conversation.payload(
                rig.runtime.main_session()["id"],
                proof.detail(),
                source="system:attention",
                principal="system:attention",
                mode="ask",
                attention_memory_watch=proof,
                attention_routing=routing,
            )
        assert attention_inputs(rig.runtime) == []
    finally:
        await rig.close()


async def test_bound_origin_is_whole_budgeted_evidence(
    tmp_path: Path,
) -> None:
    rig = components(tmp_path)
    try:
        proof, _, routing = await bound_match(rig, text="词甲" + "连续原文" * 180 + "词乙")
        await rig.runtime.startup()
        rig.runtime.config.update({"attention": {"max_chars": 512}})
        await rig.inbox.start()
        await eventually(lambda: rig.inbox.status()["waiting"] == "budget")
        assert attention_inputs(rig.runtime) == []
        rig.runtime.config.update({"attention": {"max_chars": 6000}})
        await rig.runtime.bus.notify()
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        first = attention_inputs(rig.runtime)[0]
        assert first["attention_routing"] == routing.dump()
        records = json.loads(first["content"].removeprefix(_HEADER))
        assert len(records) == 1
        detail = json.loads(records[0]["detail"])
        assert detail["reply_preference"]["origin"] == routing.origin.dump()
        assert "不是新授权" in detail["reply_preference"]["notice"]
        assert detail["quote"] == proof.quote and detail["source_id"] == proof.source_id
        assert (
            rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        )
    finally:
        await rig.close()


async def test_same_channel_route_keeps_distinct_watch_proofs_in_separate_claims(
    tmp_path: Path,
) -> None:
    rig = components(tmp_path)
    try:
        one, _, routing = await bound_match(rig, "one")
        two, _, second = await bound_match(rig, "two")
        assert routing.origin.destination_key == second.origin.destination_key
        main = rig.runtime.main_session()["id"]
        for proof, selected in ((one, routing), (two, second)):
            rig.runtime.conversation.enqueue(
                main,
                "当前完整线索：" + proof.detail(),
                source="system:attention",
                principal="system:attention",
                mode="ask",
                max_pending=3,
                attention_memory_watch=proof,
                attention_routing=selected,
                internal=True,
            )
        for index, proof in enumerate((one, two)):
            claimed = rig.runtime.store.claim_inputs(main, "separate-proof-" + str(index), limit=8)
            assert len(claimed) == 1
            # claim_inputs returns the archived Message shape; the independent
            # proof stays on its actual ingress payload, keyed by that same id.
            payload = json.loads(
                rig.runtime.store.db.execute(
                    "SELECT payload FROM ingress WHERE id=?",
                    (claimed[0]["id"],),
                ).fetchone()[0]
            )
            assert payload["attention_memory_watch"] == proof.dump()
        assert (
            rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        )
    finally:
        await rig.close()
