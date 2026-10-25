"""Corrupt occurrence evidence must not escape the real reminder source reader."""

from __future__ import annotations

import json
from pathlib import Path

from muli.commitments import Commitments
from muli.events import EventBus
from muli.reminder_followup import read_followup_notice
from muli.reminders import Reminders
from muli.reply_origin import ReplyOrigin
from muli.store import Store, now
from muli.system_events import attention_source


async def test_oversized_integer_timestamp_rejects_only_the_corrupt_notice(tmp_path: Path) -> None:
    """Exercise real fire/SQL/readers, without claiming a complete Runtime journey."""
    store = Store(tmp_path / "state.db")
    bus = EventBus(store)
    reminders = Reminders(store, bus)
    budget = 6000
    try:
        task = await Commitments(store, bus).save(
            "只读目标复盘", brief={"goal": "核查现有依据", "constraints": ["不执行任务"]}
        )
        item = reminders.create(
            "应拒绝的损坏复盘",
            now() + 60,
            commitment_id=task["id"],
            reply_origin=ReplyOrigin("web"),
        )
        await reminders._fire(item, item["due_at"])
        identifier = f"reminder:{item['id']}:{item['due_at']}"
        event = next(
            event
            for event in store.events()
            if event["type"] == "notification.created" and event["data"]["id"] == identifier
        )
        assert read_followup_notice(store, identifier, budget) is not None
        assert attention_source(store, event, ["reminder"], max_output_chars=budget) is not None

        notice = store.get("notification", identifier)
        assert notice is not None
        # A small, valid JSON integer exceeds conversion to a finite Python float.
        # Keep the real notice and its canonical event identical, so the source
        # cannot reject merely because the event and object disagree.
        notice["followup_occurrence"]["due_at"] = 10**400
        raw = json.dumps(notice, ensure_ascii=False)
        assert len(raw.encode("utf-8")) < budget * 4
        with store.db:
            store.db.execute(
                "UPDATE objects SET data=? WHERE kind='notification' AND id=?", (raw, identifier)
            )
            store.db.execute("UPDATE events SET data=? WHERE id=?", (raw, event["id"]))
        decoded = store.bounded_object("notification", identifier, max_bytes=budget * 4)
        assert decoded is not None and decoded["followup_occurrence"]["due_at"] == 10**400

        assert read_followup_notice(store, identifier, budget) is None
        current_event = next(current for current in store.events() if current["id"] == event["id"])
        assert attention_source(store, current_event, ["reminder"], max_output_chars=budget) is None

        # The same production source reader still selects a subsequent real,
        # ordinary notice; this is source isolation, not a model-consumption claim.
        following = reminders.create("后续有效提醒", now() + 120)
        await reminders._fire(following, following["due_at"])
        following_id = f"reminder:{following['id']}:{following['due_at']}"
        following_event = next(
            current
            for current in store.events()
            if current["type"] == "notification.created" and current["data"]["id"] == following_id
        )
        assert (
            attention_source(store, following_event, ["reminder"], max_output_chars=budget)
            is not None
        )
    finally:
        await reminders.close()
        store.close()
