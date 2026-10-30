"""Authenticated owner management uses the existing bounded watch and revoke chain."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_attachment_memory_policy import offline as offline
from test_attention import attention_inputs, eventually
from test_report_origin import route

import muli.memory_watches as watch_module
from muli.attachment_memory_source import attachment_source
from muli.config import Limits
from muli.evidence import EvidenceError
from muli.proactive import FrozenRouting
from muli.providers import ModelConfig
from muli.reply_origin import ReplyOrigin
from muli.store import Store, new_id, now


def register(
    rig: MemoryRig,
    *,
    key: str = "first",
    preference: str = "default",
    question: str = "留意词甲词乙",
) -> dict[str, Any]:
    main_id = rig.runtime.main_session()["id"]
    payload, _ = rig.runtime.conversation.enqueue(
        main_id,
        question,
        source="channel:onebot:bot" if preference == "channel" else "web",
        principal="owner",
        route=route(reply_to="original-private-message") if preference == "channel" else None,
        mode="ask",
        max_pending=3,
    )
    claimed = rig.runtime.store.claim_inputs(main_id, "registration-" + key, limit=1)
    assert len(claimed) == 1 and claimed[0]["id"] == payload["id"]
    routing = (
        FrozenRouting()
        if preference == "default"
        else FrozenRouting.bound(ReplyOrigin.from_input(payload))
    )
    return rig.memory.watches.create(
        main_id, payload["id"], question, ["词甲", "词乙"], key=key, routing=routing
    )


def connection(rig: MemoryRig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://127.0.0.1:8765"
    )


@pytest.mark.parametrize("preference", ["legacy", "default", "web", "channel"])
async def test_public_page_projects_only_verified_finite_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preference: str
) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig, preference=preference)
        if preference == "legacy":
            watch = {key: value for key, value in watch.items() if key != "routing"}
            watch["version"] = 1
            rig.runtime.store.put("memory_watch", watch)

        def no_model_owner(*args: object, **kwargs: object) -> NoReturn:
            raise AssertionError("Web 管理不得伪造模型工具的 owner 上下文")

        monkeypatch.setattr(rig.memory, "_attachment_guard", no_model_owner)
        runs = rig.runtime.store.list("run")
        async with connection(rig) as client:
            response = await client.get("/api/memory/watches")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            page = response.json()
            (item,) = page["items"]
            assert set(item) == {
                "id",
                "question",
                "match_terms",
                "state",
                "revision",
                "reply_preference",
            }
            fields = ("id", "question", "match_terms", "state", "revision")
            assert {key: item[key] for key in fields} == {
                key: watch[key] for key in ("id", "question", "match_terms", "state", "revision")
            }
            expected = {
                "legacy": {"kind": "default_web"},
                "default": {"kind": "default_web"},
                "web": {"kind": "registered_web"},
                "channel": {"kind": "registered_channel", "channel": "onebot"},
            }[preference]
            assert item["reply_preference"] == expected
            assert page["processing"] == {"enabled": False, "paused": False}
            assert page["after_id"] == "" and not page["has_more"] and page["next_after_id"] is None
            assert "original-private-message" not in response.text
            assert not {"routing", "created_input_id", "after_event", "key"}.intersection(item)
            assert rig.runtime.store.list("run") == runs
            assert (
                rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0]
                == 0
            )


async def test_default_page_keeps_complete_long_records_and_advances_only_returned_ids(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        watches = [
            register(rig, key=str(index), question=f"问题{index}：" + "完整背景" * 250)
            for index in range(3)
        ]
        ordered = sorted(watches, key=lambda item: item["id"])
        rig.runtime.config.update({"limits": {"max_output_chars": 2500}})
        after = ""
        seen: list[dict[str, Any]] = []
        async with connection(rig) as client:
            for index in range(len(watches)):
                response = await client.get(
                    "/api/memory/watches", params={"after_id": after} if after else None
                )
                assert response.status_code == 200
                page = response.json()
                assert response.headers["cache-control"] == "no-store"
                assert len(response.text) <= 2500
                assert page["after_id"] == after and page["items"]
                items = page["items"]
                if index == 0:
                    assert len(items) < len(watches)
                    assert page["has_more"]
                    assert page["next_after_id"] != ordered[-1]["id"]
                expected = ordered[len(seen) : len(seen) + len(items)]
                assert items == [
                    {
                        **{
                            field: watch[field]
                            for field in ("id", "question", "match_terms", "state", "revision")
                        },
                        "reply_preference": {"kind": "default_web"},
                    }
                    for watch in expected
                ]
                seen.extend(items)
                assert page["has_more"] is (len(seen) < len(watches))
                if not page["has_more"]:
                    assert page["next_after_id"] is None
                    break
                assert page["next_after_id"] == items[-1]["id"]
                assert page["next_after_id"] > after
                after = page["next_after_id"]
            else:
                pytest.fail("有限完整记录未能通过实际返回游标读完")
        assert [item["id"] for item in seen] == [item["id"] for item in ordered]
        for watch in watches:
            assert rig.runtime.store.get("memory_watch", watch["id"]) == watch
        target = seen[-1]
        async with connection(rig) as client:
            stopped = await client.post("/api/memory/watches/cancel", json={"id": target["id"]})
            assert stopped.status_code == 200
            assert stopped.json() == {
                "id": target["id"],
                "state": "cancelled",
                "revision": target["revision"] + 1,
            }
            readback = await client.get("/api/memory/watches", params={"after_id": seen[-2]["id"]})
            assert readback.status_code == 200
            assert readback.json()["items"] == [
                {**target, "state": "cancelled", "revision": target["revision"] + 1}
            ]
        for watch in watches:
            assert rig.runtime.store.get("memory_watch", watch["id"]) == (
                {**watch, "state": "cancelled", "revision": watch["revision"] + 1}
                if watch["id"] == target["id"]
                else watch
            )


async def test_full_bounded_window_still_requires_another_read_with_budget_pages(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        watches = [register(rig, key=str(index), question="完整问题" * 250) for index in range(3)]
        ordered = sorted(item["id"] for item in watches)
        rig.runtime.config.update({"limits": {"max_output_chars": 2500}})
        async with connection(rig) as client:
            shortened = await client.get("/api/memory/watches", params={"limit": 3})
            assert shortened.status_code == 200
            page = shortened.json()
            assert 0 < len(page["items"]) < len(watches) and page["has_more"]
            assert page["next_after_id"] == page["items"][-1]["id"]
            after = ""
            seen: list[str] = []
            for expected in ordered:
                response = await client.get(
                    "/api/memory/watches", params={"limit": 1, "after_id": after}
                )
                assert response.status_code == 200
                page = response.json()
                assert len(page["items"]) == 1 and page["items"][0]["id"] == expected
                assert page["has_more"] and page["next_after_id"] == expected
                seen.append(expected)
                after = page["next_after_id"]
            end = (
                await client.get("/api/memory/watches", params={"limit": 1, "after_id": after})
            ).json()
            assert seen == ordered and end["items"] == []
            assert not end["has_more"] and end["next_after_id"] is None
            assert (
                await client.get("/api/memory/watches", params={"limit": 101})
            ).status_code == 422
            assert (
                await client.get("/api/memory/watches", params={"after_id": "wrong"})
            ).status_code == 422


async def test_model_records_keep_the_original_complete_list_budget(tmp_path: Path) -> None:
    async with actual_app(tmp_path) as rig:
        watches = [register(rig, key=str(index), question="完整问题" * 250) for index in range(3)]
        main_id = rig.runtime.main_session()["id"]
        assert len(rig.memory.watch_records(main_id)) == len(watches)
        rig.runtime.config.update({"limits": {"max_output_chars": 2500}})
        with pytest.raises(EvidenceError) as failed:
            rig.memory.watch_records(main_id)
        assert failed.value.code == "output_budget"


@pytest.mark.parametrize("damage", ["routing", "oversized", "duplicate_json"])
async def test_unreturned_tail_must_pass_full_validation_before_any_budget_prefix(
    tmp_path: Path, damage: str
) -> None:
    async with actual_app(tmp_path) as rig:
        watches = [
            register(rig, key=str(index), question="完整问题" * 250, preference="channel")
            for index in range(3)
        ]
        tail = json.loads(json.dumps(max(watches, key=lambda item: item["id"])))
        if damage == "duplicate_json":
            duplicate = json.dumps(tail, ensure_ascii=False)[:-1] + ', "revision": 1}'
            with rig.runtime.store.db:
                rig.runtime.store.db.execute(
                    "UPDATE objects SET data=? WHERE kind='memory_watch' AND id=?",
                    (duplicate, tail["id"]),
                )
        else:
            if damage == "routing":
                tail["routing"]["destination"]["route"]["target"] = "other-recipient"
            else:
                tail["question"] = "大" * 23000
            rig.runtime.store.put("memory_watch", tail)
        rig.runtime.config.update({"limits": {"max_output_chars": 2500}})
        async with connection(rig) as client:
            response = await client.get("/api/memory/watches")
        assert response.status_code == 400 and "items" not in response.json()
        assert "other-recipient" not in response.text
        if damage in {"oversized", "duplicate_json"}:
            assert (
                response.json()["detail"]["code"]
                == {
                    "oversized": "output_budget",
                    "duplicate_json": "content_unavailable",
                }[damage]
            )


async def test_self_consistent_other_main_record_is_not_a_public_budget_prefix(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig)
        foreign = {**watch, "main_session_id": new_id()}
        foreign["id"] = watch_module._identity(
            "memory_watch", foreign["main_session_id"], foreign["created_input_id"], foreign["key"]
        )
        rig.runtime.store.put("memory_watch", foreign)
        assert watch_module._watch(rig.runtime.store, foreign["id"]) == foreign
        async with connection(rig) as client:
            response = await client.get("/api/memory/watches")
        assert response.status_code == 403 and "items" not in response.json()


async def test_each_request_validates_only_its_one_finite_window_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with actual_app(tmp_path) as rig:
        watches = [
            register(rig, key=str(index), question="完整问题" * 250, preference="channel")
            for index in range(3)
        ]
        ordered = sorted(watches, key=lambda item: item["id"])
        outside = {**ordered[-1], "question": "大" * 23000}
        rig.runtime.store.put("memory_watch", outside)
        rig.runtime.config.update({"limits": {"max_output_chars": 2500}})
        read_watch, read_routing = watch_module._watch, watch_module._routing
        load_routing, output_fits = FrozenRouting.load, rig.memory.watches.output_fits
        verified: list[str] = []
        routing: list[str] = []
        loaded: list[str] = []
        expected_inputs = [item["created_input_id"] for item in ordered[:2]]

        def counted_watch(store: Store, identifier: str) -> dict[str, Any]:
            verified.append(identifier)
            return read_watch(store, identifier)

        def counted_routing(store: Store, watch: dict[str, Any]) -> FrozenRouting:
            routing.append(watch["id"])
            return read_routing(store, watch)

        def counted_load(value: object) -> FrozenRouting:
            if isinstance(value, dict) and isinstance(value.get("source_input_id"), str):
                loaded.append(value["source_input_id"])
            return load_routing(value)

        def checked_fit(value: object, max_output_chars: int) -> bool:
            assert loaded == expected_inputs * 2  # Complete validation, then every projection.
            return output_fits(value, max_output_chars)

        monkeypatch.setattr(watch_module, "_watch", counted_watch)
        monkeypatch.setattr(watch_module, "_routing", counted_routing)
        monkeypatch.setattr(FrozenRouting, "load", staticmethod(counted_load))
        monkeypatch.setattr(rig.memory.watches, "output_fits", checked_fit)
        statements: list[str] = []
        rig.runtime.store.db.set_trace_callback(statements.append)
        try:
            async with connection(rig) as client:
                response = await client.get("/api/memory/watches", params={"limit": 2})
        finally:
            rig.runtime.store.db.set_trace_callback(None)
        assert response.status_code == 200
        page = response.json()
        assert 0 < len(page["items"]) < 2 and page["has_more"]
        assert page["next_after_id"] == ordered[0]["id"]
        assert verified == routing == [item["id"] for item in ordered[:2]]
        windows = [
            sql for sql in statements if "FROM objects WHERE kind='memory_watch' AND id>" in sql
        ]
        assert len(windows) == 1 and windows[0].rstrip().endswith("LIMIT 2")


@pytest.mark.parametrize("count", [1, 2])
async def test_complete_final_page_is_admitted_at_its_exact_character_budget(
    tmp_path: Path, count: int
) -> None:
    async with actual_app(tmp_path) as rig:
        for index in range(count):
            register(rig, key=str(index), question="完整背景" * 250)
        async with connection(rig) as client:
            initial = await client.get("/api/memory/watches")
            assert initial.status_code == 200
            complete = initial.json()
            exact = len(json.dumps(complete, ensure_ascii=False, allow_nan=False))
            assert 1000 < exact < rig.runtime.config.settings.limits.max_output_chars
            rig.runtime.config.update({"limits": {"max_output_chars": exact}})
            admitted = await client.get("/api/memory/watches")
            assert admitted.status_code == 200 and admitted.json() == complete
            rig.runtime.config.update({"limits": {"max_output_chars": exact - 1}})
            reduced = await client.get("/api/memory/watches")
            if count == 1:
                assert reduced.status_code == 400
                assert reduced.json()["detail"]["code"] == "output_budget"
                assert "items" not in reduced.json()
            else:
                assert reduced.status_code == 200
                page = reduced.json()
                assert len(page["items"]) == 1 and page["has_more"]
                assert page["next_after_id"] == page["items"][-1]["id"]


async def test_public_page_keeps_the_existing_utf8_byte_cap_with_legal_unicode_records(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        # actual_app uses 6000; isolate the fixed byte gate under the declared
        # default character budget, before admitting each complete watch.
        char_budget = Limits().max_output_chars
        rig.runtime.config.update({"limits": {"max_output_chars": char_budget}})
        watches = [register(rig, key=str(index), question="🙂" * 5500) for index in range(3)]
        assert len(json.dumps(watches, ensure_ascii=False, allow_nan=False)) <= char_budget
        assert sum(len(item["question"].encode("utf-8")) for item in watches) > (
            watch_module.MAX_MATCH_PROOF_BYTES
        )
        async with connection(rig) as client:
            response = await client.get("/api/memory/watches")
        assert response.status_code == 200
        page = response.json()
        assert 0 < len(page["items"]) < len(watches) and page["has_more"]
        assert len(response.content) <= watch_module.MAX_MATCH_PROOF_BYTES
        assert len(json.dumps(page, ensure_ascii=False)) <= (
            rig.runtime.config.settings.limits.max_output_chars
        )
        originals = {item["id"]: item for item in watches}
        for item in page["items"]:
            assert item["question"] == originals[item["id"]]["question"]


async def test_a_single_public_record_that_cannot_fit_is_still_an_error(tmp_path: Path) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig, question="完整背景" * 300)
        rig.runtime.config.update({"limits": {"max_output_chars": 1000}})
        async with connection(rig) as client:
            response = await client.get("/api/memory/watches")
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "output_budget"
        assert "items" not in response.json()
        assert rig.runtime.store.get("memory_watch", watch["id"]) == watch


async def test_empty_end_is_valid_but_overbudget_metadata_is_not_empty_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with actual_app(tmp_path) as rig:
        async with connection(rig) as client:
            empty = await client.get("/api/memory/watches")
            assert empty.status_code == 200 and empty.json()["items"] == []
            assert not empty.json()["has_more"] and empty.json()["next_after_id"] is None
            # The public configuration minimum normally admits this small header.
            # Fault-inject the existing budget provider to test the metadata gate.
            monkeypatch.setattr(rig.memory.watches, "max_output_chars", lambda: 1)
            response = await client.get("/api/memory/watches")
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "output_budget"
        assert "items" not in response.json()


@pytest.mark.parametrize("damage", ["routing", "oversized"])
async def test_unverifiable_record_never_turns_into_web_or_an_empty_page(
    tmp_path: Path, damage: str
) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig, preference="channel")
        if damage == "routing":
            watch["routing"]["destination"]["route"]["target"] = "other-recipient"
        else:
            watch["question"] = "大" * 23000
        rig.runtime.store.put("memory_watch", watch)
        async with connection(rig) as client:
            response = await client.get("/api/memory/watches")
            assert response.status_code == 400 and "items" not in response.json()
            assert "other-recipient" not in response.text


async def test_authenticated_same_origin_json_cancel_is_exact_id_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with actual_app(tmp_path) as rig:
        selected, other = register(rig), register(rig, key="other")
        rig.runtime.config.update({"server": {"password": "watch-owner-password"}})
        notifications: list[None] = []
        notify = rig.runtime.bus.notify

        async def notified() -> None:
            notifications.append(None)
            await notify()

        monkeypatch.setattr(rig.runtime.bus, "notify", notified)
        async with connection(rig) as client:
            body = {"id": selected["id"]}
            assert (await client.get("/api/memory/watches")).status_code == 401
            assert (await client.post("/api/memory/watches/cancel", json=body)).status_code == 401
            assert (
                await client.post("/api/auth/login", json={"password": "watch-owner-password"})
            ).status_code == 200
            assert (
                await client.get("/api/memory/watches", headers={"Origin": "https://other.invalid"})
            ).status_code == 403
            assert (
                await client.post(
                    "/api/memory/watches/cancel",
                    json=body,
                    headers={"Origin": "https://other.invalid"},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/memory/watches/cancel",
                    content="{}",
                    headers={"Content-Type": "text/plain"},
                )
            ).status_code == 415
            for invalid in ({"id": "not-an-id"}, {**body, "revision": 1}, {"id": 1}):
                assert (
                    await client.post("/api/memory/watches/cancel", json=invalid)
                ).status_code == 422
            assert notifications == []
            first = await client.post("/api/memory/watches/cancel", json=body)
            assert first.status_code == 200
            assert first.json() == {
                "id": selected["id"],
                "state": "cancelled",
                "revision": selected["revision"] + 1,
            }
            assert len(notifications) == 1
            repeated = await client.post("/api/memory/watches/cancel", json=body)
            assert repeated.json() == first.json() and len(notifications) == 2
            assert rig.runtime.store.get("memory_watch", other["id"]) == other
            assert not rig.runtime.store.db.in_transaction


async def test_disabled_processing_still_allows_read_and_revoke_but_maintenance_is_read_only(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig)
        rig.runtime.config.update({"memory": {"enabled": False}})
        async with connection(rig) as client:
            assert (await client.get("/api/memory/watches")).json()["processing"][
                "enabled"
            ] is False
            rig.runtime.maintenance.admission.pause()
            try:
                page = await client.get("/api/memory/watches")
                assert page.status_code == 200 and page.json()["processing"]["paused"] is True
                blocked = await client.post("/api/memory/watches/cancel", json={"id": watch["id"]})
                assert blocked.status_code == 409
                assert rig.runtime.store.get("memory_watch", watch["id"]) == watch
            finally:
                rig.runtime.maintenance.admission.release()
            receipt = await client.post("/api/memory/watches/cancel", json={"id": watch["id"]})
            assert receipt.status_code == 200 and receipt.json()["state"] == "cancelled"


@pytest.mark.parametrize("disabled", ["memory_watch_list", "memory_watch_cancel"])
async def test_shared_disabled_tool_policy_is_not_bypassed(tmp_path: Path, disabled: str) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig)
        rig.runtime.config.update({"disabled_tools": [disabled]})
        async with connection(rig) as client:
            read = await client.get("/api/memory/watches")
            assert read.status_code == (400 if disabled == "memory_watch_list" else 200)
            if disabled == "memory_watch_cancel":
                blocked = await client.post("/api/memory/watches/cancel", json={"id": watch["id"]})
                assert blocked.status_code == 400
                assert rig.runtime.store.get("memory_watch", watch["id"]) == watch


@pytest.mark.parametrize("closing", ["application", "runtime"])
async def test_closing_service_refuses_both_read_and_cancel(tmp_path: Path, closing: str) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig)
        target, name = (
            (rig.memory, "_closed") if closing == "application" else (rig.runtime, "_closing")
        )
        previous = getattr(target, name)
        setattr(target, name, True)
        try:
            async with connection(rig) as client:
                assert (await client.get("/api/memory/watches")).status_code == 400
                assert (
                    await client.post("/api/memory/watches/cancel", json={"id": watch["id"]})
                ).status_code == 400
                assert rig.runtime.store.get("memory_watch", watch["id"]) == watch
        finally:
            setattr(target, name, previous)


@pytest.mark.parametrize("identity", ["missing", "wrong_kind", "other_main"])
async def test_web_uses_existing_real_main_without_constructing_another_owner(
    tmp_path: Path, identity: str
) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig)
        store = rig.runtime.store
        main_id = store.existing_main_session_id()
        assert main_id is not None
        main = store.get("session", main_id)
        assert main is not None
        with store.db:
            if identity == "missing":
                store.db.execute("DELETE FROM runtime_state WHERE name='main_session'")
            elif identity == "wrong_kind":
                store._put("session", {**main, "kind": "teammate"})
            else:
                replacement = {**main, "id": new_id()}
                store._put("session", replacement)
                store.db.execute(
                    "UPDATE runtime_state SET value=? WHERE name='main_session'",
                    (replacement["id"],),
                )
        expected_marker = store.existing_main_session_id()
        try:
            async with connection(rig) as client:
                for response in (
                    await client.get("/api/memory/watches"),
                    await client.post("/api/memory/watches/cancel", json={"id": watch["id"]}),
                ):
                    assert response.status_code == (400 if identity == "missing" else 403)
                assert store.existing_main_session_id() == expected_marker
                assert store.get("memory_watch", watch["id"]) == watch
        finally:
            with store.db:
                store._put("session", main)
                store.db.execute(
                    "INSERT OR REPLACE INTO runtime_state(name,value) VALUES('main_session',?)",
                    (main_id,),
                )


async def test_cancel_waits_for_configuration_lock_then_rechecks_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with actual_app(tmp_path) as rig:
        watch = register(rig)
        async with connection(rig) as client:
            task: asyncio.Task[httpx.Response] | None = None
            try:
                async with rig.runtime.configuration_lock:
                    entered = asyncio.Event()
                    acquire = rig.runtime.configuration_lock.acquire

                    async def acquiring() -> bool:
                        entered.set()
                        return await acquire()

                    monkeypatch.setattr(rig.runtime.configuration_lock, "acquire", acquiring)
                    task = asyncio.create_task(
                        client.post("/api/memory/watches/cancel", json={"id": watch["id"]})
                    )
                    await asyncio.wait_for(entered.wait(), 3)
                    assert not task.done()
                    assert rig.runtime.store.get("memory_watch", watch["id"]) == watch
                    rig.runtime.config.update({"disabled_tools": ["memory_watch_cancel"]})
                response = await asyncio.wait_for(task, 3)
                assert response.status_code == 400
                assert rig.runtime.store.get("memory_watch", watch["id"]) == watch
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


async def test_http_cancel_wakes_and_invalidates_an_already_queued_real_lead(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update(
            {
                "models": {"main": {"model": ""}},
                "attention": {"enabled": True, "events": ["memory"], "coalesce_seconds": 0},
            }
        )
        await rig.app.state.attention.start()
        watch = register(rig)
        text = "新收藏资料中，词甲与词乙同时出现。"
        source = attachment_source(new_id(), text, hashlib.sha256(text.encode()).hexdigest(), now())
        rig.memory.pipeline.ingest(source)
        await eventually(lambda: len(attention_inputs(rig.runtime)) == 1)
        pending = attention_inputs(rig.runtime)[0]
        assert pending["state"] == "pending"
        assert pending["attention_memory_watch"]["watch_id"] == watch["id"]
        models: list[str] = []

        def forbidden(profile: ModelConfig) -> NoReturn:
            models.append(profile.model)
            raise AssertionError("取消后的旧资料回访不能进入新模型调用")

        rig.runtime.client_factory = forbidden
        async with connection(rig) as client:
            cancelled = await client.post("/api/memory/watches/cancel", json={"id": watch["id"]})
            assert cancelled.status_code == 200
            rig.runtime.config.update({"models": {"main": {"model": "main"}}})
            await rig.runtime.wake_pending_inputs()
            await eventually(
                lambda: (
                    attention_inputs(rig.runtime)[0]["state"] == "cancelled"
                    and not rig.runtime.tasks
                )
            )
            rows = attention_inputs(rig.runtime)
            assert len(rows) == 1 and rows[0]["id"] == pending["id"]
            assert rows[0]["state"] == "cancelled"
            assert models == [] and not rig.runtime.tasks
            assert (
                rig.runtime.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0]
                == 0
            )
