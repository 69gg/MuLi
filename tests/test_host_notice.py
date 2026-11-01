from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from muli.host_notice import HostNoticeDelivery, host_template
from muli.proactive import FrozenRouting
from muli.reply_origin import ReplyOrigin


def route() -> dict[str, str]:
    return {
        "name": "onebot",
        "account_id": "bot",
        "kind": "private",
        "target": "7",
        "sender_id": "7",
    }


def routing() -> dict[str, Any]:
    return FrozenRouting.bound(
        ReplyOrigin.load({"kind": "channel", "input_id": "owner", "route": route()})
    ).dump()


def record(
    kind: str,
    *,
    receipt_id: str = "r1",
    record_id: str = "record-1",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "event_id": "e1",
        "kind": kind,
        "record_id": record_id,
        "detail": json.dumps(detail or {}, ensure_ascii=False),
        "routing": routing(),
    }


def delivery(mode: str, manager: Any = None) -> HostNoticeDelivery:
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            settings=SimpleNamespace(attention=SimpleNamespace(host_delivery=mode))
        )
    )
    return HostNoticeDelivery(runtime, lambda: manager)


class Manager:
    def __init__(self, state: str = "pending") -> None:
        self.state = state
        self.calls: list[dict[str, Any]] = []

    async def enqueue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"args": args, "kwargs": kwargs})
        return {"state": self.state}


def test_only_fixed_host_templates_are_eligible() -> None:
    assert host_template(record("update")) is not None
    assert host_template(record("reminder")) is not None
    assert (
        host_template(record("management", detail={"status": "completed", "action": "update"}))
        is not None
    )
    assert host_template(record("system_log", detail={"code": "runtime_failed"})) is not None
    assert host_template(record("system_log", detail={"code": "free_text"})) is None
    assert host_template(record("memory")) is None
    assert host_template(record("file_inbox")) is None
    assert host_template(record("management", detail={"status": "started"})) is None


async def test_enforce_delivers_with_frozen_route_and_removes_receipt() -> None:
    manager = Manager()
    outcome = await delivery("enforce", manager).deliver([record("update")], guard=lambda: None)
    assert outcome.delivered == {"r1"}
    assert outcome.receipts["r1"]["host_delivery"]["template"] == "update"
    assert manager.calls[0]["kwargs"]["automatic"] is True
    assert manager.calls[0]["kwargs"]["event_id"] == "host-notice:r1"
    assert manager.calls[0]["args"][0] == "onebot"
    assert manager.calls[0]["args"][1] == "7"


async def test_management_notification_is_recorded_without_external_text() -> None:
    manager = Manager()
    outcome = await delivery("enforce", manager).deliver(
        [record("management", detail={"status": "completed", "action": "restart"})],
        guard=lambda: None,
    )
    assert outcome.delivered == {"r1"}
    assert outcome.notifications[0]["source"] == "host_notice"
    assert "重启" not in outcome.notifications[0]["content"]


async def test_shadow_records_candidate_without_sending() -> None:
    manager = Manager()
    outcome = await delivery("shadow", manager).deliver([record("update")], guard=lambda: None)
    assert not manager.calls
    assert not outcome.delivered
    assert outcome.receipts["r1"]["host_delivery"]["mode"] == "shadow"


async def test_off_and_non_template_records_do_not_touch_manager() -> None:
    manager = Manager()
    outcome = await delivery("off", manager).deliver([record("update")], guard=lambda: None)
    assert not outcome.receipts and not manager.calls
    outcome = await delivery("enforce", manager).deliver([record("memory")], guard=lambda: None)
    assert not outcome.receipts and not manager.calls


async def test_blocked_route_falls_back_to_attention() -> None:
    manager = Manager(state="blocked")
    outcome = await delivery("enforce", manager).deliver([record("update")], guard=lambda: None)
    assert not outcome.delivered
    assert outcome.receipts["r1"]["host_fallback"]["reason"] == "revoked"


async def test_delivery_error_falls_back_without_claiming_success() -> None:
    class Broken(Manager):
        async def enqueue(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("offline")

    outcome = await delivery("enforce", Broken()).deliver([record("update")], guard=lambda: None)
    assert not outcome.delivered
    assert outcome.receipts["r1"]["host_fallback"]["reason"] == "delivery_error"


async def test_guard_failure_propagates_before_any_send() -> None:
    manager = Manager()

    def guard() -> None:
        raise ValueError("policy changed")

    with pytest.raises(ValueError):
        await delivery("enforce", manager).deliver([record("update")], guard=guard)
    assert not manager.calls
