"""Authenticated owner management uses the existing bounded watch and revoke chain."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_attachment_memory_policy import offline as offline
from test_attention import attention_inputs, eventually
from test_report_origin import route

from muli.attachment_memory_source import attachment_source
from muli.proactive import FrozenRouting
from muli.providers import ModelConfig
from muli.reply_origin import ReplyOrigin
from muli.store import new_id, now


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


async def test_full_bounded_window_requires_another_read_and_budget_failure_is_not_empty(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        watches = [register(rig, key=str(index), question="完整问题" * 250) for index in range(3)]
        ordered = sorted(item["id"] for item in watches)
        rig.runtime.config.update({"limits": {"max_output_chars": 2500}})
        async with connection(rig) as client:
            failed = await client.get("/api/memory/watches", params={"limit": 3})
            assert failed.status_code == 400 and failed.json()["detail"]["code"] == "output_budget"
            assert "items" not in failed.json()
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
