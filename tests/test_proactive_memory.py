from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_attention import eventually, inbox_for, offline_runtime
from test_channels import Harness, profile, terminal, unused
from test_management import settled
from test_proactive_delivery import configure, external, update
from test_report_origin import origin, route, wait_for_reply
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelError, ChannelsConfig
from muli.config import ConfigFile
from muli.proactive import FrozenRouting, guard_delivery
from muli.proactive_memory import PREFERENCE_SQL, PreferenceEvidence, check_preference
from muli.providers import ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.tools import Tool, parameters


def extracted(
    runtime: Runtime, source_id: str = "source", *, principal: str = "owner", claim: str = ""
) -> dict[str, Any]:
    text = "主人明确的通知偏好：" + source_id
    source, _ = runtime.memory.register_source(
        {"id": source_id, "content": text, "role": "user", "principal": principal},
        trusted_owner=True,
    )
    assert source is not None
    return runtime.memory.apply_extraction(
        source_id,
        [{"content": text, "layer": "preference", "quote": text, "claim_key": claim}],
    )[0]


def changes(fact: dict[str, Any], destination: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "channels": {
            "proactive": {
                "memory_preference": {
                    "memory_id": fact["id"],
                    "version": fact["version"],
                    "destination": destination if destination is not None else external("8"),
                }
            }
        }
    }


def bind(runtime: Runtime, fact: dict[str, Any], destination: dict[str, Any] | None = None) -> None:
    candidate = runtime.config.validate_changes(changes(fact, destination))
    runtime.validate_proactive_settings(candidate)
    runtime.config.replace(candidate)


@pytest.fixture
async def case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, dict[str, Any]]]:
    runtime = offline_runtime(tmp_path)
    configure(runtime.config)
    fact = extracted(runtime)
    try:
        yield runtime, fact
    finally:
        await runtime.close()


async def test_preference_is_an_exact_readonly_three_primary_key_lookup(
    case: tuple[Runtime, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, fact = case
    bind(runtime, fact)
    expected = PreferenceEvidence(
        fact["id"], fact["version"], "source", runtime.memory.source("source")["digest"]
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("routing must not read memory text, search, graph, or a model")

    for name in ("get", "source", "search", "owner_profile", "graph", "conflicts"):
        monkeypatch.setattr(runtime.memory, name, forbidden)
    before = runtime.store.db.total_changes
    for _ in range(2):
        evidence, reason = check_preference(runtime.config.settings, runtime.store.db)
        assert evidence == expected and reason == "valid"
        frozen = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
        assert frozen.memory_evidence == expected
        guard_delivery(runtime.config.settings, route("8"), frozen.dump(), db=runtime.store.db)
    assert runtime.store.db.total_changes == before
    assert set(expected.dump()) == {"memory_id", "version", "source_id", "source_digest"}
    assert "主人明确" not in json.dumps(frozen.dump(), ensure_ascii=False)
    plan = [
        row["detail"]
        for row in runtime.store.db.execute("EXPLAIN QUERY PLAN " + PREFERENCE_SQL, (fact["id"],))
    ]
    assert len(plan) == 3
    assert all(any(f"SEARCH {alias} " in line for line in plan) for alias in ("m", "f", "s"))
    with pytest.raises(ValueError, match="rollback"):
        with runtime.store.db:
            runtime.store.db.execute(
                "UPDATE memory_meta SET version=version+1 WHERE memory_id=?", (fact["id"],)
            )
            assert check_preference(runtime.config.settings, runtime.store.db) == (
                None,
                "version_changed",
            )
            raise ValueError("rollback")
    assert check_preference(runtime.config.settings, runtime.store.db) == (expected, "valid")


@pytest.mark.parametrize(
    "state", ["manual", "nonowner", "not_preference", "untrusted", "bad_digest"]
)
async def test_unqualified_fact_cannot_be_newly_selected(
    case: tuple[Runtime, dict[str, Any]], state: str
) -> None:
    runtime, fact = case
    if state == "manual":
        fact = runtime.memory.save("手工偏好不是来源证明", kind="preference", trusted_owner=True)
    elif state == "nonowner":
        fact = extracted(runtime, "nonowner", principal="agent")
    elif state == "not_preference":
        fact = runtime.memory.edit(fact["id"], kind="fact")
    elif state == "untrusted":
        original = runtime.memory.source("source")
        fact = runtime.memory.save(
            "非信任事实", kind="preference", source_id="source", quote=original["content"]
        )
    else:
        # Deliberately malformed legacy metadata; supported source registration is immutable.
        with runtime.store.db:
            runtime.store.db.execute("UPDATE memory_sources SET digest='invalid' WHERE id='source'")
    candidate = runtime.config.validate_changes(changes(fact))
    with pytest.raises(ValueError, match="记忆绑定不可用"):
        runtime.validate_proactive_settings(candidate)
    assert runtime.config.settings.channels.proactive.memory_preference is None
    assert FrozenRouting.independent(candidate, runtime.store.db).origin.delivery_routes() == [
        route()
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", "1"),
        ("version", 0),
        ("memory_id", ""),
        ("memory_id", "bad\n"),
        ("trusted_owner", True),
        ("source_id", "source"),
        ("source_digest", "0" * 64),
    ],
)
async def test_configuration_accepts_only_fact_identity_version_and_destination(
    case: tuple[Runtime, dict[str, Any]], field: str, value: Any
) -> None:
    runtime, fact = case
    patch = changes(fact)
    patch["channels"]["proactive"]["memory_preference"][field] = value
    with pytest.raises(ValueError):
        runtime.config.validate_changes(patch)


async def test_configuration_replaces_whole_binding_and_respects_explicit_pause(
    case: tuple[Runtime, dict[str, Any]],
) -> None:
    runtime, fact = case
    runtime.config.update({"channels": {"proactive": {"external_enabled": False}}})
    bind(runtime, fact)
    assert runtime.config.settings.channels.proactive.external_enabled
    paused = changes(fact)
    paused["channels"]["proactive"]["external_enabled"] = False
    target = runtime.config.validate_changes(paused)
    runtime.validate_proactive_settings(target)
    runtime.config.replace(target)
    assert not runtime.config.settings.channels.proactive.external_enabled
    bind(runtime, fact, {"kind": "web"})
    assert (
        runtime.config.settings.channels.proactive.memory_preference.destination.model_dump()
        == {"kind": "web"}
    )
    assert not runtime.config.settings.channels.proactive.external_enabled
    # A partial replacement cannot inherit the old identity, version or route.
    with pytest.raises(ValueError):
        runtime.config.validate_changes(
            {"channels": {"proactive": {"memory_preference": {"destination": external()}}}}
        )
    with pytest.raises(ValueError):
        bind(runtime, fact, external("not-an-owner"))


@pytest.mark.parametrize(
    "revocation", ["edit", "forget", "conflict", "disabled", "unbind", "destination"]
)
async def test_new_items_fall_back_but_old_memory_authority_is_revoked(
    case: tuple[Runtime, dict[str, Any]], revocation: str
) -> None:
    runtime, fact = case
    bind(runtime, fact)
    frozen = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    if revocation == "edit":
        runtime.memory.edit(fact["id"], content="已更正偏好")
    elif revocation == "forget":
        runtime.memory.forget_source("source")
    elif revocation == "conflict":
        # Two real, mutually exclusive preference facts use the same claim slot.
        other = extracted(runtime, "other", claim="notification")
        runtime.memory.save(
            fact["content"],
            kind="preference",
            identifier=fact["id"],
            source_id="source",
            quote=fact["quote"],
            trusted_owner=True,
            claim_key="notification",
        )
        assert runtime.memory.get(other["id"])["status"] == "conflict"
    elif revocation == "disabled":
        runtime.config.update({"memory": {"enabled": False}})
    elif revocation == "unbind":
        runtime.config.update({"channels": {"proactive": {"memory_preference": None}}})
    else:
        bind(runtime, fact, external())
    with pytest.raises(ValueError, match="记忆"):
        guard_delivery(runtime.config.settings, route("8"), frozen.dump(), db=runtime.store.db)
    assert FrozenRouting.independent(
        runtime.config.settings, runtime.store.db
    ).origin.delivery_routes() == [route()]
    # An invalid historical binding must not block safe edits, revocation or removal.
    candidate = runtime.config.validate_changes(
        {
            "persona": {"append": "unrelated"},
            "channels": {"onebot": {"enabled": False, "owner_ids": []}},
        }
    )
    runtime.validate_proactive_settings(candidate)
    candidate = runtime.config.validate_changes(
        {
            "memory": {"enabled": False},
            "channels": {"proactive": {"external_enabled": False, "memory_preference": None}},
        }
    )
    runtime.validate_proactive_settings(candidate)


async def test_conflict_resolution_requires_explicit_current_version_rebinding(
    case: tuple[Runtime, dict[str, Any]],
) -> None:
    runtime, _ = case
    first = extracted(runtime, "first", claim="owner.notifications")
    bind(runtime, first)
    second = extracted(runtime, "second", claim="owner.notifications")
    assert check_preference(runtime.config.settings, runtime.store.db)[1] == "inactive"
    conflict = runtime.memory.conflicts(include_resolved=False)[0]
    runtime.memory.resolve_conflict(conflict["id"], first["id"])
    assert check_preference(runtime.config.settings, runtime.store.db)[1] == "version_changed"
    assert runtime.memory.get(second["id"])["status"] == "superseded"
    bind(runtime, runtime.memory.get(first["id"]))
    assert check_preference(runtime.config.settings, runtime.store.db)[1] == "valid"


async def test_batch_claim_keeps_default_memory_and_distinct_proofs_separate(
    case: tuple[Runtime, dict[str, Any]],
) -> None:
    runtime, first = case
    default = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    bind(runtime, first, external())
    selected = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    revised = runtime.memory.edit(first["id"], content="同一事实的新版本")
    bind(runtime, revised, external())
    newer = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    second = extracted(runtime, "second")
    bind(runtime, second, external())
    other = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    assert len({default.batch_key, selected.batch_key, newer.batch_key, other.batch_key}) == 4
    session = runtime.main_session()["id"]
    for index, frozen in enumerate((default, selected, selected, newer, other)):
        assert FrozenRouting.load(frozen.dump()) == frozen
        runtime.conversation.enqueue(
            session,
            str(index),
            source="system:attention",
            principal="system:attention",
            mode="ask",
            max_pending=10,
            event_id=str(index),
            internal=True,
            attention_routing=frozen,
        )
    assert [
        len(runtime.store.claim_inputs(session, str(index), limit=10)) for index in range(4)
    ] == [1, 2, 1, 1]
    malformed = selected.dump()
    malformed["memory_evidence"]["source_digest"] = "invalid"
    assert FrozenRouting.load(malformed).classification == "legacy_local"


async def test_frozen_proof_survives_restart_and_does_not_turn_into_latest_binding(
    case: tuple[Runtime, dict[str, Any]],
) -> None:
    runtime, fact = case
    bind(runtime, fact)
    await update(runtime, independent=True)
    notice = runtime.store.get("notification", "update-99.0.0")
    assert notice is not None
    frozen = FrozenRouting.load(notice["routing"])
    assert frozen.memory_evidence is not None
    instance = runtime.store.instance_id
    main = runtime.main_session()["id"]
    await runtime.close()
    reopened = Runtime(runtime.config, runtime.data_dir, client_factory=runtime.client_factory)
    try:
        assert reopened.store.instance_id == instance
        assert reopened.main_session()["id"] == main
        persisted = reopened.store.get("notification", notice["id"])
        assert persisted is not None
        assert FrozenRouting.load(persisted["routing"]) == frozen
        reopened.config.update({"channels": {"proactive": {"memory_preference": None}}})
        assert FrozenRouting.independent(reopened.config.settings, reopened.store.db) != frozen
        await update(reopened, independent=True)
        assert reopened.store.get("notification", notice["id"])["routing"] == frozen.dump()
    finally:
        await reopened.close()
    assert FrozenRouting.bound(origin()).memory_evidence is None


async def test_queued_configuration_rechecks_changed_fact_before_apply(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config)
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "channels": {"onebot": {"enabled": False}},
        }
    )
    app = create_app(runtime.config, runtime.data_dir, runtime)
    release = asyncio.Event()
    async with app.router.lifespan_context(app):
        fact = extracted(runtime)

        async def work() -> None:
            await release.wait()

        task = asyncio.create_task(work())
        runtime.tasks["fixture"] = task
        task.add_done_callback(lambda _: runtime.tasks.pop("fixture", None))
        try:
            plan = await app.state.management.stage("configure", changes(fact))
            assert runtime.config.settings.channels.proactive.memory_preference is None
            runtime.memory.edit(fact["id"], content="排队期间更正")
            release.set()
            final = await settled(app.state.management, plan["id"])
            assert final["status"] == "failed" and "version_changed" in final["error"]
            assert runtime.config.settings.channels.proactive.memory_preference is None
        finally:
            release.set()
            await task


async def test_close_await_cannot_commit_stale_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config)
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "channels": {"onebot": {"enabled": False}},
        }
    )
    app = create_app(runtime.config, runtime.data_dir, runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    async with app.router.lifespan_context(app):
        fact = extracted(runtime)
        target = runtime.config.validate_changes(changes(fact))
        original = app.state.memory_application.close

        async def closing() -> None:
            await original()
            entered.set()
            await release.wait()

        monkeypatch.setattr(app.state.memory_application, "close", closing)

        async def apply() -> None:
            async with runtime.configuration_lock:
                await app.state.management.apply_settings(target)

        task = asyncio.create_task(apply())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            runtime.memory.forget_source("source")
            release.set()
            with pytest.raises(ValueError, match="missing"):
                await task
            assert runtime.config.settings.channels.proactive.memory_preference is None
            assert (
                ConfigFile(runtime.config.path).settings.channels.proactive.memory_preference
                is None
            )
            assert app.state.attention.status()["running"] and runtime.started
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_explicit_report_keeps_frozen_memory_scope_and_revocation_blocks_transport(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(tool_calls=[ToolCall("read", "read_evidence", "{}")]),
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "send-revoked",
                        "channel_send",
                        json.dumps(
                            {
                                "channel": "onebot",
                                "target": "8",
                                "text": "按原偏好整理的报告",
                                "idempotency_key": "revoked-memory-report",
                            }
                        ),
                    )
                ]
            ),
            ModelTurn(content="按原偏好整理的报告"),
        ],
    )
    configure(runtime.config)
    fact = extracted(runtime)
    bind(runtime, fact)

    async def read(args: dict[str, Any]) -> str:
        runtime.memory.forget_source("source")
        return "证据已更正；不改动本轮冻结地址"

    runtime.add_tool_provider(lambda _: [Tool("read_evidence", "读取证据", parameters({}), read)])
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    sent: list[str] = []

    async def send(destination: Any, text: str, file: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(text)
        raise AssertionError("revoked memory authority must not reach HTTP")

    application.manager.adapters["onebot"].send = send  # type: ignore[method-assign]
    inbox = inbox_for(runtime)
    try:
        await application.start()
        await update(runtime, independent=True)
        await inbox.start()
        await eventually(lambda: bool(runtime.permissions.pending))
        approval_id = next(iter(runtime.permissions.pending))
        approval = runtime.store.get("approval", approval_id)
        assert approval is not None and approval["tool"] == "channel_send"
        assert approval["risk"] == "external" and sent == []
        await runtime.permissions.resolve(approval_id, "approve")
        await wait_for_reply(runtime, "按原偏好整理的报告")
        await eventually(lambda: not runtime.tasks)
        deliveries = application.manager.deliveries()
        assert all(
            "approval_notice" in delivery["payload"] and delivery["state"] == "blocked"
            for delivery in deliveries
        )
        result = next(
            message
            for message in runtime.store.messages(runtime.main_session()["id"])
            if message.get("tool_call_id") == "send-revoked"
        )
        assert "记忆" in json.loads(result["content"])["error"]
        assert sent == []
        assert application.status()["proactive"]["next_destination"] == external()
        assert application.status()["proactive"]["memory_status"]["reason"] == "missing"
    finally:
        await inbox.close()
        await application.close()
        await runtime.close()


async def test_outbox_blocked_and_unknown_survive_reopen_and_require_explicit_retry(
    case: tuple[Runtime, dict[str, Any]], tmp_path: Path
) -> None:
    runtime, fact = case
    bind(runtime, fact)
    frozen = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(owner_ids=["7", "8"])), unused)

    def attach() -> None:
        harness.manager._active.add("onebot")
        harness.manager.delivery_guard = lambda chosen, meta: guard_delivery(
            runtime.config.settings, chosen, meta, db=runtime.store.db
        )

    attach()
    try:
        first = await harness.manager.enqueue(
            "onebot",
            "8",
            "pending",
            event_id="first",
            route=route("8"),
            automatic=True,
            attention_routing=frozen.dump(),
        )
        unknown = await harness.manager.enqueue(
            "onebot",
            "8",
            "unknown",
            event_id="unknown",
            route=route("8"),
            automatic=True,
            attention_routing=frozen.dump(),
        )
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET state='unknown' WHERE id=?", (unknown["id"],)
            )
        runtime.config.update({"channels": {"proactive": {"memory_preference": None}}})
        assert harness.manager._block_revoked() == (True, False)
        await harness.close()
        harness = Harness(tmp_path, ChannelsConfig(onebot=profile(owner_ids=["7", "8"])), unused)
        attach()
        assert harness.manager.delivery(first["id"])["state"] == "blocked"
        duplicate = await harness.manager.enqueue(
            "onebot",
            "8",
            "pending",
            event_id="first",
            route=route("8"),
            automatic=True,
            attention_routing=frozen.dump(),
        )
        assert duplicate["state"] == "blocked"
        bind(runtime, fact)
        assert harness.manager._block_revoked() == (False, False)
        assert harness.manager.delivery(first["id"])["state"] == "blocked"
        assert (await harness.manager.retry(first["id"]))["state"] == "pending"
        with pytest.raises(ChannelError, match="结果未知"):
            await harness.manager.retry(unknown["id"])
        assert harness.manager.delivery(unknown["id"])["state"] == "unknown"
    finally:
        await harness.close()


async def test_each_segment_revalidates_memory_and_retains_successful_receipts(
    case: tuple[Runtime, dict[str, Any]], tmp_path: Path
) -> None:
    runtime, fact = case
    bind(runtime, fact)
    frozen = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    harness = Harness(
        tmp_path, ChannelsConfig(onebot=profile(owner_ids=["7", "8"], max_message_chars=2)), unused
    )
    harness.manager._active.add("onebot")
    harness.manager.delivery_guard = lambda chosen, meta: guard_delivery(
        runtime.config.settings, chosen, meta, db=runtime.store.db
    )
    sent: list[str] = []

    async def send(destination: Any, text: str, file: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(text)
        runtime.memory.edit(fact["id"], content="发送首段后更正")
        return {"id": "confirmed-first"}

    harness.manager.adapters["onebot"].send = send  # type: ignore[method-assign]
    try:
        item = await harness.manager.enqueue(
            "onebot",
            "8",
            "一二三四",
            event_id="split",
            route=route("8"),
            automatic=True,
            attention_routing=frozen.dump(),
        )
        harness.manager._tasks.append(asyncio.create_task(harness.manager._outbox()))
        blocked = await terminal(harness.manager, item["id"])
        assert blocked["state"] == "blocked" and blocked["next_part"] == 1
        assert sent == ["一二"] and blocked["receipts"] == [{"id": "confirmed-first"}]
        with pytest.raises(ChannelError, match="version_changed"):
            await harness.manager.retry(item["id"])
    finally:
        await harness.close()


async def test_forgetting_during_attachment_read_blocks_before_http(
    case: tuple[Runtime, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, fact = case
    bind(runtime, fact)
    frozen = FrozenRouting.independent(runtime.config.settings, runtime.store.db)
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(owner_ids=["7", "8"])), unused)
    harness.manager._active.add("onebot")
    harness.manager.delivery_guard = lambda chosen, meta: guard_delivery(
        runtime.config.settings, chosen, meta, db=runtime.store.db
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def chunks() -> AsyncIterator[bytes]:
        yield b"bounded evidence"

    attachment = await harness.attachments.save(chunks(), "evidence.txt")
    original = harness.manager._file

    async def reading(identifier: str, name: str, *, media_mode: str = "file") -> Any:
        entered.set()
        await release.wait()
        return await original(identifier, name, media_mode=media_mode)

    monkeypatch.setattr(harness.manager, "_file", reading)
    try:
        item = await harness.manager.enqueue(
            "onebot",
            "8",
            attachment_ids=[attachment["id"]],
            event_id="file",
            route=route("8"),
            automatic=True,
            attention_routing=frozen.dump(),
        )
        harness.manager._tasks.append(asyncio.create_task(harness.manager._outbox()))
        await asyncio.wait_for(entered.wait(), 2)
        runtime.memory.forget_source("source")
        release.set()
        blocked = await terminal(harness.manager, item["id"])
        assert blocked["state"] == "blocked" and blocked["next_part"] == 0
        assert blocked["receipts"] == []
    finally:
        release.set()
        await harness.close()


async def test_paused_or_disabled_invalid_binding_can_be_removed_without_fulltext(
    case: tuple[Runtime, dict[str, Any]],
) -> None:
    runtime, fact = case
    bind(runtime, fact)
    runtime.config.update(
        {"memory": {"enabled": False}, "channels": {"proactive": {"external_enabled": False}}}
    )
    candidate = runtime.config.validate_changes(
        {"channels": {"proactive": {"external_enabled": True}}}
    )
    with pytest.raises(ValueError, match="memory_disabled"):
        runtime.validate_proactive_settings(candidate)
    runtime.memory.forget_source("source")
    candidate = runtime.config.validate_changes({"memory": {"enabled": True}})
    with pytest.raises(ValueError, match="missing"):
        runtime.validate_proactive_settings(candidate)
    candidate = runtime.config.validate_changes(
        {"memory": {"enabled": True}, "channels": {"proactive": {"memory_preference": None}}}
    )
    runtime.validate_proactive_settings(candidate)
    runtime.config.replace(candidate)
    assert check_preference(candidate, runtime.store.db) == (None, "not_configured")
