"""Calendar notifications retain exact source kind and cannot borrow task proofs."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import muli.reminders as reminders_module
from muli.events import EventBus
from muli.reminders import Reminders
from muli.reply_origin import ReplyOrigin
from muli.store import Store
from muli.system_events import attention_source


async def occurrence(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    timestamp = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(reminders_module, "now", lambda: timestamp)
    engine = Reminders(store, EventBus(store))
    item = engine.create_calendar(
        "当地提醒",
        "Asia/Shanghai",
        "07:30",
        list(range(1, 8)),
        reply_origin=ReplyOrigin("web", input_id="original-owner-input"),
    )
    timestamp = item["due_at"]
    await engine._fire(item, timestamp)
    notice = store.list("notification")[0]
    event = next(event for event in store.events() if event["type"] == "notification.created")
    return item, notice, event


async def test_calendar_notice_is_a_read_only_source_with_original_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path / "calendar.sqlite")
    try:
        item, notice, event = await occurrence(store, monkeypatch)
        source = attention_source(store, event, ["reminder"], max_output_chars=6000)
        assert source is not None
        assert source.kind == "reminder" and source.record_id == notice["id"]
        assert source.detail == "当地提醒\n" and source.routing.classification == "bound"
        assert source.routing.origin.kind == "web"
        assert source.routing.origin.input_id == "original-owner-input"
        assert source.reminder_proof is None
        assert notice["id"].startswith(f"calendar_reminder:{item['id']}:")
        updates = [event for event in store.events() if event["type"] == "reminder.updated"]
        assert len(updates) == 1 and updates[0]["data"]["kind"] == "calendar_reminder"
    finally:
        store.close()


@pytest.mark.parametrize("target", ["event", "notice", "record"])
@pytest.mark.parametrize(
    "field", ["commitment_id", "followup", "followup_revision", "followup_occurrence"]
)
async def test_null_task_fields_on_any_calendar_layer_cannot_downgrade_to_plain_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
) -> None:
    store = Store(tmp_path / "calendar.sqlite")
    try:
        item, notice, event = await occurrence(store, monkeypatch)
        if target == "event":
            event["data"][field] = None
        elif target == "notice":
            store.put("notification", {**notice, field: None})
        else:
            saved = store.get("calendar_reminder", item["id"])
            assert saved is not None
            store.put("calendar_reminder", {**saved, field: None})
        assert attention_source(store, event, ["reminder"], max_output_chars=6000) is None
    finally:
        store.close()


@pytest.mark.parametrize("namespace", [None, "unknown", "reminder"])
async def test_namespace_is_exact_and_never_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str | None,
) -> None:
    store = Store(tmp_path / "calendar.sqlite")
    try:
        item, notice, event = await occurrence(store, monkeypatch)
        saved = store.get("calendar_reminder", item["id"])
        assert saved is not None
        # Even a same-id ordinary record cannot make a calendar ID valid in
        # the other namespace. Both event and persisted notice agree here.
        store.put("reminder", {**saved, "calendar": None})
        notice["reminder_kind"] = namespace
        store.put("notification", notice)
        event["data"]["reminder_kind"] = namespace
        assert attention_source(store, event, ["reminder"], max_output_chars=6000) is None
    finally:
        store.close()


@pytest.mark.parametrize("target", ["event", "notice"])
async def test_missing_namespace_and_explicit_null_are_not_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    store = Store(tmp_path / "calendar.sqlite")
    try:
        _, notice, event = await occurrence(store, monkeypatch)
        changed = deepcopy(event)
        if target == "event":
            del changed["data"]["reminder_kind"]
        else:
            del notice["reminder_kind"]
            store.put("notification", notice)
        assert attention_source(store, changed, ["reminder"], max_output_chars=6000) is None
    finally:
        store.close()
