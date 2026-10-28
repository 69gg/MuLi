"""The narrow consumer uses persisted host evidence, with no model or transport."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import muli.topic_reminders as topic_module
from muli.config import TopicReminderSettings
from muli.conversation import Conversation
from muli.events import EventBus
from muli.evidence import EvidenceError
from muli.maintenance import Admission, MaintenanceError
from muli.owner_text import owner_text_marker
from muli.store import Store
from muli.topic_reminders import TopicReminders


class ObservedEvent(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waits: asyncio.Queue[None] = asyncio.Queue()

    async def wait(self) -> bool:
        self.waits.put_nowait(None)
        return await super().wait()


@dataclass
class Harness:
    store: Store
    bus: EventBus
    engine: TopicReminders
    admission: Admission
    settings: TopicReminderSettings
    main_id: str
    request_id: str
    changed: ObservedEvent

    def admit(
        self,
        text: str,
        *,
        original_chars: int | None = None,
        route: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        source = "web" if route is None else f"channel:{route['name']}:{route['account_id']}"
        payload = Conversation.payload(
            self.main_id,
            text,
            source=source,
            principal="owner",
            mode="auto",
            route=route,
            event_id=event_id,
        )
        marker = owner_text_marker(
            payload,
            len(text) if original_chars is None else original_chars,
            kind="web" if route is None else "channel",
        )
        if marker is not None:
            payload["owner_text"] = marker
        payload.update(overrides or {})
        receipt, _inserted = self.store.enqueue_input(payload, max_pending=200)
        return receipt

    def create(self, keyword: str = "提到这个", *, key: str = "one") -> dict[str, Any]:
        return self.engine.create(
            self.main_id,
            self.request_id,
            keyword,
            "登记时的标题",
            "登记时的内容",
            key=key,
        )

    async def idle(self, *, wake: bool = False) -> None:
        if self.engine.task is None:
            await self.engine.start()
        elif wake:
            self.changed.set()
        async with asyncio.timeout(3):
            await self.changed.waits.get()


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    store = Store(tmp_path / "topic.sqlite")
    bus, admission = EventBus(store), Admission()
    settings = TopicReminderSettings()
    engine = TopicReminders(store, bus, settings=lambda: settings, admission=admission)
    changed = ObservedEvent()
    engine.changed = changed
    host = Harness(
        store,
        bus,
        engine,
        admission,
        settings,
        store.main_session()["id"],
        "",
        changed,
    )
    host.request_id = host.admit("请登记一次性话题提醒")["id"]
    try:
        yield host
    finally:
        await engine.close()
        store.close()


async def test_once_uses_first_new_owner_text_and_never_copies_matching_text(
    harness: Harness,
) -> None:
    h = harness
    rule = h.create()
    first = h.admit("机密原文：提到这个，提到这个")
    h.admit("后来再次提到这个")
    await h.idle()
    notices = h.store.list("notification")
    assert len(notices) == 1
    notice = notices[0]
    assert notice == {
        "id": f"topic_reminder:{rule['id']}",
        "title": "登记时的标题",
        "content": "登记时的内容",
        "source": "topic_reminder",
        "read": False,
        "created_at": notice["created_at"],
        "session_id": h.main_id,
        "topic_reminder_id": rule["id"],
        "matched_input_id": first["id"],
        "matched_seq": first["seq"],
    }
    assert "机密原文" not in json.dumps(notice, ensure_ascii=False)
    saved = h.engine.records(h.main_id)[0]
    assert saved["state"] == "fired" and saved["matched_seq"] == first["seq"]
    assert [event["type"] for event in h.store.events()] == ["notification.created"]
    assert h.bus.revision == 1
    await h.idle(wake=True)
    assert h.store.list("notification") == notices
    assert h.store.list("run") == [] and h.store.list("attention_receipt") == []
    assert h.engine.status()["last_error"] is None


async def test_registration_and_already_accepted_backlog_are_before_anchor(
    harness: Harness,
) -> None:
    h = harness
    h.request_id = h.admit("提到这个，请以后提醒")["id"]
    backlog = h.admit("已经接纳的提到这个")
    rule = h.create()
    assert rule["after_seq"] == backlog["seq"]
    await h.idle()
    assert h.store.list("notification") == []
    fresh = h.admit("登记后的提到这个")
    await h.idle(wake=True)
    assert h.store.list("notification")[0]["matched_input_id"] == fresh["id"]


async def test_multiple_rules_cross_entry_and_exact_unicode_matching(harness: Harness) -> None:
    h = harness
    first, second = h.create("MuLi", key="latin"), h.create("é🌱", key="unicode")
    h.admit("muli e\u0301🌱")
    await h.idle()
    assert h.store.list("notification") == []
    channel = h.admit(
        "渠道原文 MuLi é🌱",
        route={"name": "onebot", "account_id": "123", "target": "7", "sender_id": "7"},
        event_id="one-stable-message",
    )
    await h.idle(wake=True)
    notices = h.store.list("notification")
    assert {notice["topic_reminder_id"] for notice in notices} == {first["id"], second["id"]}
    assert {notice["matched_input_id"] for notice in notices} == {channel["id"]}
    duplicate = h.admit(
        "渠道原文 MuLi é🌱",
        route={"name": "onebot", "account_id": "123", "target": "7", "sender_id": "7"},
        event_id="one-stable-message",
    )
    assert duplicate["seq"] == channel["seq"]
    await h.idle(wake=True)
    assert h.store.list("notification") == notices


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_text": None},
        {"owner_text": {"version": 2, "kind": "web", "chars": 4}},
        {"owner_text": {"version": 1, "kind": "web", "chars": True}},
        {"owner_text": {"version": 1, "kind": "web", "chars": 10000}},
        {"owner_text": {"version": 1, "kind": "web", "chars": 4, "extra": True}},
        {"owner_text": {"version": 1, "kind": "channel", "chars": 4}},
        {"source": "onboarding"},
        {"source": "script"},
        {"principal": "system:attention"},
        {"principal": "external"},
        {"session_id": "teammate-session"},
        {"route": {"name": "onebot", "account_id": "123"}},
    ],
)
async def test_missing_or_invalid_host_evidence_never_matches(
    harness: Harness, overrides: dict[str, Any]
) -> None:
    h = harness
    h.create()
    h.admit("提到这个", overrides=overrides)
    await h.idle()
    assert h.store.list("notification") == []
    assert h.engine.records(h.main_id)[0]["state"] == "active"
    assert h.engine.status()["last_error"] is None


async def test_attachment_suffix_never_matches_but_original_prefix_does(harness: Harness) -> None:
    h = harness
    h.create()
    original = "请查看附件"
    h.admit(original + "\n附件名与下载警告：提到这个", original_chars=len(original))
    h.admit(" \n提到这个", original_chars=2)
    await h.idle()
    assert h.store.list("notification") == []
    owner = "主人原文提到这个"
    fresh = h.admit(owner + "\n附件告警", original_chars=len(owner))
    await h.idle(wake=True)
    assert h.store.list("notification")[0]["matched_input_id"] == fresh["id"]


async def test_create_list_cancel_are_main_scoped_with_explicit_owner_proof(
    harness: Harness,
) -> None:
    h = harness
    h.engine.require_owner_input(h.main_id, h.request_id)
    legacy = h.admit("旧版或脚本主人输入", overrides={"owner_text": None})
    with pytest.raises(PermissionError):
        h.engine.require_owner_input(h.main_id, legacy["id"])
    with pytest.raises(PermissionError):
        h.engine.create(h.main_id, legacy["id"], "词", "标题", key="legacy")
    with pytest.raises(PermissionError):
        h.engine.require_owner_input("other", h.request_id)
    with pytest.raises(PermissionError):
        h.engine.records("other")
    with pytest.raises(PermissionError):
        h.engine.cancel("other", "x")
    assert h.engine.records(h.main_id) == []


async def test_idempotency_conflict_cancel_and_terminal_records_do_not_rearm(
    harness: Harness,
) -> None:
    h = harness
    first = h.create()
    assert h.create() == first
    with pytest.raises(ValueError, match="key"):
        h.create("不同文字")
    cancelled = h.engine.cancel(h.main_id, first["id"])
    assert cancelled["state"] == "cancelled"
    assert h.engine.cancel(h.main_id, first["id"]) == cancelled
    assert h.create() == cancelled
    second = h.create(key="independent")
    h.admit("提到这个")
    await h.idle()
    fired = h.engine.cancel(h.main_id, second["id"])
    assert fired["state"] == "fired"
    assert h.create(key="independent") == fired
    h.admit("再次提到这个")
    await h.idle(wake=True)
    assert len(h.store.list("notification")) == 1


async def test_records_use_bounded_stable_id_pagination(harness: Harness) -> None:
    h = harness
    expected = sorted(h.create(key=str(index))["id"] for index in range(3))
    first = h.engine.records(h.main_id, limit=2)
    second = h.engine.records(h.main_id, after_id=first[-1]["id"], limit=2)
    assert [item["id"] for item in first + second] == expected
    for limit in (0, 101, True):
        with pytest.raises(ValueError):
            h.engine.records(h.main_id, limit=limit)


async def test_restart_recovers_commit_before_wakeup_and_keeps_one_shot(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite"
    store = Store(database)
    settings, admission = TopicReminderSettings(batch_size=1), Admission()
    bus = EventBus(store)
    engine = TopicReminders(store, bus, settings=lambda: settings, admission=admission)
    changed = ObservedEvent()
    engine.changed = changed
    h = Harness(store, bus, engine, admission, settings, store.main_session()["id"], "", changed)
    h.request_id = h.admit("登记")["id"]
    rule = h.create()
    fresh = h.admit("提到这个")
    # The input commit exists, but the former host never reached its wakeup.
    store.close()

    store = Store(database)
    bus = EventBus(store)
    engine = TopicReminders(store, bus, settings=lambda: settings, admission=admission)
    changed = ObservedEvent()
    engine.changed = changed
    try:
        await engine.start()
        async with asyncio.timeout(3):
            await changed.waits.get()
        notices = store.list("notification")
        assert len(notices) == 1 and notices[0]["matched_input_id"] == fresh["id"]
        saved = store.get("topic_reminder", rule["id"])
        assert saved is not None and saved["state"] == "fired"
        await engine.close()
        await engine.start()
        async with asyncio.timeout(3):
            await changed.waits.get()
        assert store.list("notification") == notices
    finally:
        await engine.close()
        store.close()


async def test_notice_rule_and_cursor_rollback_together_then_retry_on_change(
    harness: Harness,
) -> None:
    h = harness
    rule = h.create()
    await h.idle()
    cursor = h.store.get("topic_reminder_state", "current")
    with h.store.db:
        h.store.db.execute(
            "CREATE TRIGGER reject_topic_cursor BEFORE UPDATE ON objects "
            "WHEN NEW.kind='topic_reminder_state' "
            "BEGIN SELECT RAISE(ABORT,'private transaction failure'); END"
        )
    h.admit("提到这个")
    await h.idle(wake=True)
    assert h.store.list("notification") == [] and h.store.events() == []
    assert h.store.get("topic_reminder", rule["id"]) == rule
    assert h.store.get("topic_reminder_state", "current") == cursor
    assert not h.store.db.in_transaction
    assert h.engine.status()["last_error"] == "topic_reminders_failed"
    assert h.bus.revision == 0
    with h.store.db:
        h.store.db.execute("DROP TRIGGER reject_topic_cursor")
    await h.idle(wake=True)
    assert len(h.store.list("notification")) == 1
    assert h.engine.status()["last_error"] is None


async def test_notify_failure_after_commit_does_not_repeat_effects(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    h.create()
    h.admit("提到这个")
    notifications = 0

    async def failed_notify() -> None:
        nonlocal notifications
        notifications += 1
        assert not h.store.db.in_transaction
        raise OSError("private notify failure")

    monkeypatch.setattr(h.bus, "notify", failed_notify)
    await h.idle()
    notices, events = h.store.list("notification"), h.store.events()
    assert len(notices) == 1 and h.engine.status()["last_error"] == "topic_reminders_failed"
    await h.idle(wake=True)
    assert h.store.list("notification") == notices and h.store.events() == events
    assert notifications == 1


async def test_unknown_transaction_is_not_rolled_back_or_committed_and_parks_instance(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    h.create()
    await h.idle()
    h.admit("提到这个")
    h.store.db.execute("BEGIN IMMEDIATE")
    h.store.db.execute(
        "INSERT INTO runtime_state(name,value) VALUES('topic-test-owned-prefix','uncommitted')"
    )
    try:
        await h.idle(wake=True)
        assert h.store.db.in_transaction
        assert h.engine.status()["blocked"] is True
        assert h.engine.status()["last_error"] == "topic_reminders_transaction_unsettled"
        with sqlite3.connect(tmp_path / "topic.sqlite") as reader:
            assert (
                reader.execute(
                    "SELECT count(*) FROM runtime_state WHERE name='topic-test-owned-prefix'"
                ).fetchone()[0]
                == 0
            )
        assert h.store.list("notification") == []
    finally:
        # Only this test owns the injected prefix. The consumer never repairs it.
        h.store.db.rollback()
    await h.idle(wake=True)
    assert h.store.list("notification") == [] and h.engine.status()["blocked"] is True
    with pytest.raises(RuntimeError, match="未知事务"):
        h.create(key="still-parked")
    await h.engine.close()
    replacement = TopicReminders(
        h.store,
        h.bus,
        settings=lambda: h.settings,
        admission=h.admission,
    )
    replacement.changed = ObservedEvent()
    try:
        await replacement.start()
        async with asyncio.timeout(3), h.bus.changed:
            await h.bus.changed.wait_for(lambda: bool(h.store.list("notification")))
        assert replacement.status()["blocked"] is False
    finally:
        await replacement.close()


async def test_maintenance_pauses_consumption_and_writes_but_close_settles(
    harness: Harness,
) -> None:
    h = harness
    rule = h.create()
    await h.idle()
    h.admission.pause()
    h.admit("提到这个")
    h.changed.set()
    await asyncio.sleep(0)
    assert h.store.list("notification") == [] and h.engine.status()["paused"] is True
    with pytest.raises(MaintenanceError):
        h.engine.cancel(h.main_id, rule["id"])
    assert h.admission.release()
    async with asyncio.timeout(3), h.bus.changed:
        await h.bus.changed.wait_for(lambda: bool(h.store.list("notification")))
    h.admission.pause()
    h.changed.set()
    await asyncio.sleep(0)
    await h.engine.close()
    assert h.engine.task is None and h.admission.idle


async def test_failure_has_no_busy_retry_and_safe_status(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    attempts = 0

    def fail_page() -> tuple[bool, bool]:
        nonlocal attempts
        attempts += 1
        raise OSError("private database path and owner text")

    monkeypatch.setattr(h.engine, "_consume_page", fail_page)
    await h.idle()
    for _ in range(3):
        await asyncio.sleep(0)
    assert attempts == 1
    assert h.engine.status()["last_error"] == "topic_reminders_failed"
    await h.idle(wake=True)
    assert attempts == 2
    assert h.engine.task is not None and not h.engine.task.done()


async def test_active_limit_keyword_limit_and_bad_rule_pause_visibly(harness: Harness) -> None:
    h = harness
    h.settings.max_active = 1
    rule = h.create()
    with pytest.raises(ValueError, match="上限"):
        h.create(key="overflow")
    with pytest.raises(ValueError):
        h.create("x" * (h.settings.max_keyword_chars + 1), key="long")
    with h.store.db:
        broken = {**rule, "keyword": ""}
        h.store._put("topic_reminder", broken)
    h.admit("提到这个")
    await h.idle()
    assert h.store.list("notification") == []
    assert h.engine.status()["last_error"] == "topic_reminders_failed"
    assert h.store.get("topic_reminder_state", "current") is None
    h.store.put("topic_reminder", rule)
    await h.idle(wake=True)
    assert len(h.store.list("notification")) == 1


async def test_lowered_active_limit_detects_one_lookahead_without_advancing(
    harness: Harness,
) -> None:
    h = harness
    h.create(key="first")
    h.create(key="second")
    h.settings.max_active = 1
    h.admit("提到这个")
    await h.idle()
    assert h.store.list("notification") == []
    assert h.engine.status()["last_error"] == "topic_reminders_failed"
    h.settings.max_active = 2
    await h.idle(wake=True)
    assert len(h.store.list("notification")) == 2


async def test_sql_projection_excludes_large_unrelated_payload_and_bounds_bad_text(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    h.create()
    # The host descriptor grants no right to parse arbitrary assembled metadata.
    fresh = h.admit(
        "提到这个", overrides={"unused_context": "UNPARSED" * topic_module._INPUT_BYTES}
    )
    original_loads = json.loads
    parsed: list[int] = []

    def bounded_loads(value: str | bytes | bytearray, *args: Any, **kwargs: Any) -> Any:
        assert "UNPARSED" not in (value if isinstance(value, str) else value.decode("utf-8"))
        parsed.append(len(value))
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(topic_module.json, "loads", bounded_loads)
    await h.idle()
    assert h.store.list("notification")[0]["matched_input_id"] == fresh["id"]
    assert max(parsed) <= topic_module._INPUT_BYTES
    monkeypatch.undo()
    second = h.create(key="bad-content")
    invalid = Conversation.payload(
        h.main_id,
        "提到这个",
        source="web",
        principal="owner",
        mode="auto",
    )
    invalid["content"] = "UNPARSED" * topic_module._INPUT_BYTES
    invalid["owner_text"] = {"version": 1, "kind": "web", "chars": 4}
    h.store.enqueue_input(invalid, max_pending=200)
    monkeypatch.setattr(topic_module.json, "loads", bounded_loads)
    await h.idle(wake=True)
    saved = h.store.get("topic_reminder", second["id"])
    assert saved is not None and saved["state"] == "active"
    assert len(h.store.list("notification")) == 1


async def test_oversized_rule_is_rejected_before_python_json_parsing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    rule = h.create()
    h.store.put("topic_reminder", {**rule, "content": "UNPARSED" * topic_module._RULE_BYTES})
    original_loads = json.loads

    def bounded_loads(value: str | bytes | bytearray, *args: Any, **kwargs: Any) -> Any:
        assert "UNPARSED" not in (value if isinstance(value, str) else value.decode("utf-8"))
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(topic_module.json, "loads", bounded_loads)
    h.admit("提到这个")
    await h.idle()
    assert h.store.list("notification") == []
    assert h.engine.status()["last_error"] == "topic_reminders_failed"


async def test_one_page_yields_before_reading_next_page(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    h.settings.batch_size = 1
    h.create()
    for _ in range(3):
        h.admit("尚未命中")
    h.admit("最后提到这个")
    observed: list[str] = []
    original = h.engine._consume_page

    def page() -> tuple[bool, bool]:
        observed.append("page")
        result = original()
        if result[0]:
            asyncio.get_running_loop().call_soon(observed.append, "yield")
        return result

    monkeypatch.setattr(h.engine, "_consume_page", page)
    await h.idle()
    assert observed == ["page", "yield", "page", "yield", "page", "yield", "page"]
    assert len(h.store.list("notification")) == 1


async def test_registration_budget_does_not_prevent_cancelling_an_existing_large_rule(
    harness: Harness,
) -> None:
    h = harness
    h.engine.max_output_chars = lambda: 1000
    with pytest.raises(EvidenceError, match="话题提醒结果超过输出预算"):
        h.engine.create(h.main_id, h.request_id, "词", "标题", "正文" * 600, key="large")
    assert not h.store.list("topic_reminder")
    h.engine.max_output_chars = lambda: 24000
    rule = h.engine.create(h.main_id, h.request_id, "词", "标题", "正文" * 600, key="large")
    h.engine.max_output_chars = lambda: 1000
    cancelled = h.engine.cancel(h.main_id, rule["id"])
    assert cancelled["state"] == "cancelled" and cancelled["content"] == rule["content"]
    assert h.store.get("topic_reminder", rule["id"])["state"] == "cancelled"
    assert not h.store.db.in_transaction


async def test_tool_listing_respects_total_budget_and_can_resume_with_smaller_pages(
    harness: Harness,
) -> None:
    h = harness
    h.engine.max_output_chars = lambda: 24000
    for index in range(8):
        h.engine.create(h.main_id, h.request_id, "词", "标题", "正文" * 150, key=str(index))
    h.engine.max_output_chars = lambda: 1000
    with pytest.raises(EvidenceError, match="话题提醒结果超过输出预算"):
        h.engine.records(h.main_id, limit=8)
    first = h.engine.records(h.main_id, limit=1)
    second = h.engine.records(h.main_id, after_id=first[-1]["id"], limit=1)
    assert len(first) == len(second) == 1 and first[-1]["id"] < second[-1]["id"]
    assert "content" not in first[0]
