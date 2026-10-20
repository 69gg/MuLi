from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from test_attention import attention_inputs, eventually, inbox_for, offline_runtime
from test_channels import Harness, profile, terminal, unused
from test_report_origin import origin, route, wait_for_reply
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.channels import ChannelError, ChannelsConfig
from muli.config import ConfigFile, Settings
from muli.management import SelfManagement
from muli.plugins import PluginManager
from muli.proactive import FrozenRouting, guard_delivery
from muli.providers import ModelTurn, ToolCall
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.service import UserService
from muli.store import Store, now
from muli.tools import Tool, parameters
from muli.updates import Updates


def external(target: str = "7", **changes: str) -> dict[str, Any]:
    return {"kind": "channel", "route": route(target, **changes)}


def configure(config: ConfigFile, target: str = "7") -> None:
    config.update(
        {
            "channels": {
                "onebot": {
                    "enabled": True,
                    "base_url": "https://platform.invalid",
                    "owner_ids": ["7", "8"],
                    "token": "TEST_ONLY_TOKEN",
                },
                "proactive": {"external_enabled": True, "default_destination": external(target)},
            }
        }
    )


async def update(runtime: Runtime, *, independent: bool = False) -> dict[str, Any]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"info": {"name": "muli-agent", "version": "99.0.0"}}
            )
        )
    ) as client:
        return await Updates(runtime.config, runtime.store, runtime.bus).check(
            client, independent=independent
        )


async def fire(runtime: Runtime, reply: ReplyOrigin | None = None) -> dict[str, Any]:
    item = runtime.reminders.create("原任务提醒", now() + 60, reply_origin=reply)
    await runtime.reminders._fire(item, item["due_at"])
    notice = runtime.store.get("notification", f"reminder:{item['id']}:{item['due_at']}")
    assert notice is not None
    return notice


def test_default_is_local_and_destination_replacement_is_atomic(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    assert config.settings.channels.proactive.model_dump() == {
        "external_enabled": False,
        "default_destination": {"kind": "web"},
    }
    assert not FrozenRouting.independent(config.settings).origin.delivery_routes()
    configure(config)
    assert FrozenRouting.independent(config.settings).origin.delivery_routes() == [route()]
    config.update({"channels": {"proactive": {"default_destination": {"kind": "web"}}}})
    assert config.settings.channels.proactive.default_destination.model_dump() == {"kind": "web"}
    # Selecting WebUI is a future default change, not revocation of frozen A.
    assert config.settings.channels.proactive.external_enabled
    config.update({"channels": {"proactive": {"default_destination": external("8")}}})
    assert FrozenRouting.independent(config.settings).origin.delivery_routes() == [route("8")]
    assert "TEST_ONLY_TOKEN" not in json.dumps(FrozenRouting.independent(config.settings).dump())


def test_api_and_agent_can_enable_external_default_with_one_patch(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    changes = {
        "channels": {
            "onebot": {"owner_ids": ["7"]},
            "proactive": {"default_destination": external()},
        }
    }
    config.update(changes)
    assert config.settings.channels.proactive.external_enabled
    assert "external_enabled" not in changes["channels"]["proactive"]
    config.update(
        {"channels": {"proactive": {"external_enabled": False, "default_destination": external()}}}
    )
    assert not config.settings.channels.proactive.external_enabled
    # Choosing another complete external address while paused is a fresh enable
    # action; callers preserving pause must send its explicit false in that patch.
    config.update(
        {
            "channels": {
                "onebot": {"owner_ids": ["7", "8"]},
                "proactive": {"default_destination": external("8")},
            }
        }
    )
    assert config.settings.channels.proactive.external_enabled


@pytest.mark.parametrize(
    "destination",
    [
        external("unknown"),
        external(account_id="other"),
        external(kind="group", target="group"),
        {"kind": "channel", "route": {**route(), "reply_to": "message"}},
        {"kind": "channel", "route": {**route(), "token": "secret"}},
        {"kind": "web", "route": route()},
        external(sender_id="8"),
        external(target="\n"),
    ],
)
def test_new_destination_uses_complete_candidate_binding(
    tmp_path: Path, destination: dict[str, Any]
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    with pytest.raises(ValueError):
        config.update(
            {
                "channels": {
                    "onebot": {"owner_ids": ["7"]},
                    "proactive": {"external_enabled": True, "default_destination": destination},
                }
            }
        )
    assert not config.path.exists()


def test_candidate_can_bind_new_group_and_later_revoke_without_retargeting(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "channels": {
                "onebot": {"account_id": "new", "owner_ids": ["7"], "allowed_groups": ["group"]},
                "proactive": {
                    "external_enabled": True,
                    "default_destination": external(
                        "group", account_id="new", kind="group", sender_id="7"
                    ),
                },
            }
        }
    )
    frozen = FrozenRouting.independent(config.settings)
    config.update({"channels": {"onebot": {"owner_ids": [], "allowed_groups": []}}})
    assert FrozenRouting.independent(config.settings) == frozen
    # Re-enabling a revoked external default does require a valid candidate binding.
    config.update({"channels": {"proactive": {"external_enabled": False}}})
    with pytest.raises(ValueError, match="绑定"):
        config.update({"channels": {"proactive": {"external_enabled": True}}})


@pytest.mark.parametrize("value", [None, [], {}, {"v": 1, "classification": []}])
def test_bad_or_legacy_routing_cannot_choose_external_destination(value: object) -> None:
    assert FrozenRouting.load(value).classification == "legacy_local"
    assert not FrozenRouting.load(value).reply_origin("input").delivery_routes()
    with pytest.raises(ValueError):
        guard_delivery(Settings(), route(), value if value is not None else {})


async def test_producer_atomic_rollback_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = offline_runtime(tmp_path)
    configure(runtime.config)
    original = runtime.store._event

    def fail_after_event(event_type: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(event_type, *args, **kwargs)
        if event_type in {"reminder.updated", "message.completed"} or (
            event_type == "notification.created" and args[0].get("source") == "update"
        ):
            raise ValueError("injected transaction failure")
        return result

    try:
        item = runtime.reminders.create("不能半写", now() + 60, reply_origin=origin())
        before = runtime.store.event_position()
        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_event", fail_after_event)
            with pytest.raises(ValueError, match="injected"):
                await runtime.reminders._fire(item, item["due_at"])
            with pytest.raises(ValueError, match="injected"):
                await update(runtime, independent=True)
            with pytest.raises(ValueError, match="injected"):
                runtime.store.complete_message("rollback", "不能留下孤立模型终答")
        assert runtime.store.event_position() == before
        assert runtime.store.get("reminder", item["id"])["fired_count"] == 0
        assert not runtime.store.list("notification") and not runtime.store.list("update")
        assert runtime.store.messages("rollback") == []
        await runtime.reminders._fire(item, item["due_at"])
        await update(runtime, independent=True)
        message = runtime.store.complete_message(
            "rollback", "原子完整终答", delivery_routes=[route()]
        )
        database = Path(runtime.store.db.execute("PRAGMA database_list").fetchone()[2])
    finally:
        await runtime.close()
    reopened = Store(database)
    try:
        events = reopened.events(before)
        assert [event["type"] for event in events] == [
            "notification.created",
            "reminder.updated",
            "notification.created",
            "message.completed",
        ]
        assert events[-1]["data"]["message"] == reopened.get("message", message["id"])
        notice = reopened.get("notification", "update-99.0.0")
        assert notice is not None and FrozenRouting.load(
            notice["routing"]
        ).origin.delivery_routes() == [route()]
    finally:
        reopened.close()


async def test_only_registered_startup_source_uses_default_and_freezes_at_production(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content=f"报告{i}") for i in range(4)])
    configure(runtime.config)
    inbox = inbox_for(runtime)
    try:
        await update(runtime, independent=True)
        runtime.config.update({"channels": {"proactive": {"default_destination": external("8")}}})
        await fire(runtime, origin())
        await fire(runtime, ReplyOrigin("web", "web-request"))
        await fire(runtime)
        await inbox.start()
        await eventually(lambda: len(seen) == 4 and not runtime.tasks)
        inputs = attention_inputs(runtime)
        assert [item["attention_routing"]["classification"] for item in inputs] == [
            "independent",
            "bound",
            "bound",
            "legacy_local",
        ]
        assert all(
            item["principal"] == "system:attention" and item["route"] is None for item in inputs
        )
        replies = [
            m
            for m in runtime.store.messages(runtime.main_session()["id"])
            if m["role"] == "assistant"
        ]
        assert [m["delivery_routes"] for m in replies] == [[route()], [route()], [], []]
        assert all("policy_fingerprint" not in item["content"] for item in inputs)
        assert len(runtime.store.list("attention_receipt")) == 4
    finally:
        await inbox.close()
        await runtime.close()


async def test_direct_update_and_preexisting_reminder_stay_local(tmp_path: Path) -> None:
    runtime = offline_runtime(tmp_path)
    configure(runtime.config)
    inbox = inbox_for(runtime)
    try:
        await update(runtime)
        notice = runtime.store.get("notification", "update-99.0.0")
        assert (
            notice is not None
            and FrozenRouting.load(notice["routing"]).classification == "legacy_local"
        )
        # Rechecking the same version independently cannot rewrite the old notice.
        await update(runtime, independent=True)
        assert runtime.store.get("notification", notice["id"]) == notice
        item = runtime.reminders.create("旧记录", now() + 60)
        item.pop("routing")
        runtime.store.put("reminder", item)
        await runtime.reminders._fire(item, item["due_at"])
        await inbox.start()
        await eventually(lambda: bool(attention_inputs(runtime)))
        assert len(attention_inputs(runtime)) == 1
        assert not attention_inputs(runtime)[0]["reply_origin"].get("route")
        assert attention_inputs(runtime)[0]["state"] == "pending"
    finally:
        await inbox.close()
        await runtime.close()


async def test_canonical_adapters_reject_reminder_or_management_claiming_independence(
    tmp_path: Path,
) -> None:
    runtime = offline_runtime(tmp_path)
    configure(runtime.config)
    inbox = inbox_for(runtime)
    try:
        notice = await fire(runtime, origin())
        notice["routing"] = FrozenRouting.independent(runtime.config.settings).dump()
        runtime.store.put("notification", notice)
        event = runtime.store.event("notification.created", notice)
        assert inbox._record(event, runtime.config.settings.attention) is None
        plan = {
            "id": "bad-independent",
            "status": "completed",
            "action": "configure",
            "routing": notice["routing"],
        }
        event = runtime.store.event("management.completed", plan, objects=[("management", plan)])
        assert inbox._record(event, runtime.config.settings.attention) is None
        # Matching a fake event's routing to no canonical routing also fails.
        canonical = await fire(runtime, ReplyOrigin("web", "owner-web"))
        event = runtime.store.event(
            "notification.created", {**canonical, "routing": notice["routing"]}
        )
        assert inbox._record(event, runtime.config.settings.attention) is None
    finally:
        await inbox.close()
        await runtime.close()


async def test_tools_capture_prepared_request_not_newest_input_and_management_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    plugins = PluginManager(runtime, FastAPI(), tmp_path / "plugins")

    async def no_apply(settings: Settings) -> None:
        raise AssertionError("本测试不执行管理操作")

    management = SelfManagement(
        runtime, plugins, UserService(runtime.config.path, runtime.data_dir), no_apply
    )
    session = runtime.main_session()["id"]
    runtime.store.add_message(
        session, "user", "请求A", principal="owner", source="channel:onebot:bot", route=route()
    )
    prepared = await runtime._prepare_request(session)
    runtime.store.add_message(
        session, "user", "后来B", principal="owner", source="channel:onebot:bot", route=route("8")
    )
    token = runtime._tool_session.set(session)
    request_token = runtime._request_context.set(prepared)
    try:
        assert prepared.reply_origin.delivery_routes() == [route()]
        reminder = await runtime.tools["reminder_schedule"].handler(
            {"title": "原请求提醒", "due_at": now() + 60}
        )
        assert FrozenRouting.load(reminder["routing"]).origin.delivery_routes() == [route()]
        plan = await management.stage("configure", {"persona": {"name": "新名字"}})
        assert FrozenRouting.load(plan["routing"]).origin.delivery_routes() == [route()]
        await management.cancel(plan["id"])
        original = runtime.store._event

        def fail(event_type: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            result = original(event_type, *args, **kwargs)
            if event_type == "management.queued":
                raise ValueError("injected management failure")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(runtime.store, "_event", fail)
            with pytest.raises(ValueError, match="injected"):
                await management.stage("configure", {"persona": {"name": "不能半写"}})
        assert len(management.plans()) == 1
        schema = runtime.tools["reminder_schedule"].schema()
        assert "reply_origin" not in json.dumps(schema) and "routing" not in json.dumps(schema)
    finally:
        runtime._request_context.reset(request_token)
        runtime._tool_session.reset(token)
        await management.close()
        await runtime.close()


async def test_real_runtime_only_complete_final_is_external_and_tokens_remain_streamed(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(
        tmp_path,
        [
            ModelTurn(content="正在核实", tool_calls=[ToolCall("read", "read_evidence", "{}")]),
            ModelTurn(content="核实后的完整报告"),
        ],
    )
    configure(runtime.config)
    sent: list[str] = []

    async def read(args: dict[str, Any]) -> str:
        assert runtime.request_context(
            runtime.main_session()["id"]
        ).reply_origin.delivery_routes() == [route()]
        runtime.config.update({"channels": {"proactive": {"default_destination": external("8")}}})
        return "可验证事实"

    async def send(
        destination: dict[str, Any], text: str, file: Any, **kwargs: Any
    ) -> dict[str, Any]:
        assert destination == route()
        sent.append(text)
        return {"id": "receipt"}

    runtime.add_tool_provider(lambda _: [Tool("read_evidence", "读取事实", parameters({}), read)])
    # ChannelApplication uses the actual runtime store and durable event consumer.
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    application.manager.adapters["onebot"].send = send  # type: ignore[method-assign]
    inbox = inbox_for(runtime)
    try:
        await application.start()
        await update(runtime, independent=True)
        await inbox.start()
        await wait_for_reply(runtime, "核实后的完整报告")
        await eventually(lambda: len(sent) == 1 and not runtime.tasks)
        replies = [
            m
            for m in runtime.store.messages(runtime.main_session()["id"])
            if m["role"] == "assistant"
        ]
        assert replies[0]["delivery_routes"] == []
        assert replies[-1]["delivery_routes"] == [route()]
        assert sent == ["核实后的完整报告"]
        deltas = [
            e["data"]["content"] for e in runtime.store.events() if e["type"] == "message.delta"
        ]
        assert deltas == ["正在核实", "核实后的完整报告"]
        status = application.status()["proactive"]
        assert status["next_destination"] == external("8") and status["model_configured"]
    finally:
        await inbox.close()
        await application.close()
        await runtime.close()


@pytest.mark.parametrize(
    "revocation", ["pause", "attention", "owner", "group", "account", "inactive"]
)
async def test_revocation_is_durable_blocked_and_never_resets_unknown(
    tmp_path: Path, revocation: str
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    configure(config)
    destination = route("group", kind="group", sender_id="7") if revocation == "group" else route()
    if revocation == "group":
        config.update(
            {
                "channels": {
                    "onebot": {"allowed_groups": ["group"]},
                    "proactive": {"default_destination": {"kind": "channel", "route": destination}},
                }
            }
        )
    frozen = FrozenRouting.independent(config.settings)
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(allowed_groups=["group"])), unused)
    harness.manager._active.add("onebot")
    harness.manager.delivery_guard = lambda chosen, meta: guard_delivery(
        config.settings, chosen, meta
    )
    try:
        first = await harness.manager.enqueue(
            "onebot",
            destination["target"],
            "报告",
            event_id="first",
            route=destination,
            automatic=True,
            attention_routing=frozen.dump(),
        )
        unknown = await harness.manager.enqueue(
            "onebot",
            destination["target"],
            "未知",
            event_id="unknown",
            route=destination,
            automatic=True,
            attention_routing=frozen.dump(),
        )
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET state='unknown' WHERE id=?", (unknown["id"],)
            )
        if revocation == "pause":
            config.update({"channels": {"proactive": {"external_enabled": False}}})
        elif revocation == "attention":
            config.update({"attention": {"enabled": False}})
        elif revocation in {"owner", "group", "account"}:
            changes = (
                {"owner_ids": []}
                if revocation == "owner"
                else {"allowed_groups": []}
                if revocation == "group"
                else {"account_id": "replacement"}
            )
            harness.manager.config = replace(
                harness.manager.config, onebot=replace(harness.manager.config.onebot, **changes)
            )
        else:
            harness.manager._active.clear()
        changed, more = harness.manager._block_revoked()
        assert changed and not more
        assert harness.manager.delivery(first["id"])["state"] == "blocked"
        assert harness.manager.delivery(unknown["id"])["state"] == "unknown"
        with pytest.raises(ChannelError):
            await harness.manager.retry(first["id"])
        with pytest.raises(ChannelError, match="结果未知"):
            await harness.manager.retry(unknown["id"])
        # Duplicate completion facts cannot reset either terminal state.
        duplicate = await harness.manager.enqueue(
            "onebot",
            destination["target"],
            "报告",
            event_id="first",
            route=destination,
            automatic=True,
            attention_routing=frozen.dump(),
        )
        assert duplicate["state"] == "blocked"
        duplicate_unknown = await harness.manager.enqueue(
            "onebot",
            destination["target"],
            "未知",
            event_id="unknown",
            route=destination,
            automatic=True,
            attention_routing=frozen.dump(),
        )
        assert duplicate_unknown["state"] == "unknown"
        assert harness.manager.delivery(unknown["id"])["state"] == "unknown"
    finally:
        await harness.close()


async def test_default_change_keeps_frozen_route_and_reenable_requires_explicit_retry(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    configure(config)
    frozen = FrozenRouting.independent(config.settings)
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(owner_ids=["7", "8"])), unused)
    harness.manager._active.add("onebot")
    harness.manager.delivery_guard = lambda chosen, meta: guard_delivery(
        config.settings, chosen, meta
    )
    try:
        item = await harness.manager.enqueue(
            "onebot",
            "7",
            "A",
            event_id="A",
            route=route(),
            automatic=True,
            attention_routing=frozen.dump(),
        )
        config.update({"channels": {"proactive": {"default_destination": external("8")}}})
        assert harness.manager._block_revoked() == (False, False)
        assert harness.manager.delivery(item["id"])["route"] == route()
        config.update({"channels": {"proactive": {"external_enabled": False}}})
        assert harness.manager._block_revoked() == (True, False)
        config.update({"channels": {"proactive": {"external_enabled": True}}})
        assert harness.manager._block_revoked() == (False, False)
        assert harness.manager.delivery(item["id"])["state"] == "blocked"
        retried = await harness.manager.retry(item["id"])
        assert retried["state"] == "pending" and retried["route"] == route()
    finally:
        await harness.close()


async def test_segment_revocation_preserves_receipts_and_passive_reply_reservation(
    tmp_path: Path,
) -> None:
    enabled = True
    frozen = FrozenRouting.bound(origin(name="qq", reply_to="passive-message"))
    harness = Harness(
        tmp_path,
        ChannelsConfig(qq=profile(app_id="bot", client_secret="test", max_message_chars=2)),
        unused,
    )
    harness.manager._active.add("qq")
    sent: list[int] = []

    def guard(chosen: Any, metadata: object) -> None:
        if not enabled:
            raise ValueError("暂停发送")

    async def send(chosen: Any, text: str, file: Any, *, key: str, sequence: int) -> dict[str, Any]:
        nonlocal enabled
        sent.append(sequence)
        enabled = False
        return {"sequence": sequence}

    harness.manager.delivery_guard = guard
    harness.manager.adapters["qq"].send = send  # type: ignore[method-assign]
    try:
        item = await harness.manager.enqueue(
            "qq",
            "7",
            "一二三四",
            event_id="split",
            route=frozen.origin.delivery_routes()[0],
            automatic=True,
            attention_routing=frozen.dump(),
        )
        worker = asyncio.create_task(harness.manager._outbox())
        harness.manager._tasks.append(worker)
        blocked = await terminal(harness.manager, item["id"])
        assert blocked["state"] == "blocked" and blocked["next_part"] == 1
        assert sent == [0] and blocked["receipts"] == [{"sequence": 0}]
        enabled = True
        await harness.manager.retry(item["id"])
        complete = await terminal(harness.manager, item["id"])
        assert complete["state"] == "sent" and sent == [0, 1]
        state = harness.store.db.execute(
            "SELECT value FROM channel_state WHERE name LIKE 'qq_sequence:%'"
        ).fetchone()[0]
        assert state == "2"
    finally:
        await harness.close()


async def test_revocation_during_attachment_read_blocks_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    configure(config)
    frozen = FrozenRouting.independent(config.settings)
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), unused)
    harness.manager._active.add("onebot")
    harness.manager.delivery_guard = lambda chosen, metadata: guard_delivery(
        config.settings, chosen, metadata
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def chunks() -> AsyncIterator[bytes]:
        yield b"bounded attachment"

    attachment = await harness.attachments.save(chunks(), "evidence.txt", "text/plain")
    original = harness.manager._file

    async def reading(identifier: str, name: str, *, media_mode: str = "file") -> Any:
        entered.set()
        await release.wait()
        return await original(identifier, name, media_mode=media_mode)

    monkeypatch.setattr(harness.manager, "_file", reading)
    try:
        item = await harness.manager.enqueue(
            "onebot",
            "7",
            attachment_ids=[attachment["id"]],
            event_id="file-await",
            route=route(),
            automatic=True,
            attention_routing=frozen.dump(),
        )
        harness.manager._tasks.append(asyncio.create_task(harness.manager._outbox()))
        await asyncio.wait_for(entered.wait(), 2)
        config.update({"channels": {"proactive": {"external_enabled": False}}})
        release.set()
        blocked = await terminal(harness.manager, item["id"])
        assert blocked["state"] == "blocked" and blocked["next_part"] == 0
        assert blocked["receipts"] == []
    finally:
        release.set()
        await harness.close()
