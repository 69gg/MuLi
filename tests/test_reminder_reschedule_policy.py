"""Bounded reschedule policy checks through real main Runtime tool turns.

Reminder records are synthetic boundary fixtures, not a claimed mobile journey.
The model calls the registered tool; no handler, receipt or request is fabricated.
The separate runtime journey owns scheduler, routing and close/reopen evidence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import eventually
from test_reminders import fire_due
from test_runtime import FakeClient, make_runtime
from test_task_briefs import attach_services
from test_teammate_attachment_runtime import call, tool_result

from muli.local_services import LocalServices
from muli.permissions import Mode
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.reply_origin import ReplyOrigin
from muli.runtime import Runtime
from muli.store import now

TOOL = "reminder_reschedule"
Case = tuple[Runtime, LocalServices]
BeforeFinish = Callable[[], Awaitable[None]]


@pytest.fixture
async def reschedule_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Case]:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("提醒改期边界仅用本地 Runtime，不允许外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
        }
    )
    await runtime.reconfigure()
    services = attach_services(runtime, tmp_path)
    try:
        yield runtime, services
    finally:
        runtime.maintenance.admission.release()
        await runtime.close()


def original_origin() -> ReplyOrigin:
    # Host-owned synthetic source; the actual main input below has no channel.
    origin = ReplyOrigin.load(
        {
            "kind": "channel",
            "input_id": "synthetic-original-owner-input",
            "route": {
                "name": "onebot",
                "account_id": "123",
                "target": "7",
                "sender_id": "7",
                "kind": "private",
                "reply_to": "original-message",
            },
        }
    )
    assert origin.kind == "channel"
    return origin


def state(runtime: Runtime) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int]]:
    return (
        sorted(runtime.store.list("reminder"), key=lambda item: item["id"]),
        sorted(runtime.store.list("notification"), key=lambda item: item["id"]),
        [event["id"] for event in runtime.store.events() if event["type"] == "reminder.updated"],
    )


async def execute(
    runtime: Runtime,
    calls: list[ToolCall],
    *,
    available: bool = True,
    mode: Mode = "yolo",
    before_finish: BeforeFinish | None = None,
) -> dict[str, dict[str, Any]]:
    seen: list[list[Message]] = []
    rounds = 0

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            seen.append(list(messages))
            if rounds == 1:
                selected = [item["function"] for item in tools if item["function"]["name"] == TOOL]
                assert bool(selected) is available
                if available:
                    schema = selected[0]["parameters"]
                    assert set(schema["properties"]) == {"id", "due_at", "kind"}
                    assert schema["properties"]["kind"] == {
                        "type": "string",
                        "enum": ["reminder", "calendar_reminder"],
                    }
                    assert set(schema["required"]) == {"id", "due_at"}
                    assert schema["additionalProperties"] is False
                    assert runtime.session_tools(runtime.main_session()["id"])[TOOL].risk == "write"
                turn = ModelTurn(tool_calls=calls)
            else:
                assert rounds == 2
                for item in calls:
                    tool_result(messages, item.id)
                turn = ModelTurn(content="本轮改期已按真实工具结果结束。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model == "main"
        return Client([], seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    main_id = runtime.main_session()["id"]
    started = await runtime.start(main_id, "请按本轮指定参数改期，失败时保留原约定。", mode=mode)
    async with asyncio.timeout(3):
        if before_finish is not None:
            await before_finish()
        await runtime.wait(started["run_id"])
    saved = runtime.store.get("run", started["run_id"])
    assert saved is not None and saved["status"] == "completed"
    assert rounds == 2
    # Verify the results persisted by the real driver, not just the fake's view.
    messages = runtime.store.messages(main_id)
    results: dict[str, dict[str, Any]] = {}
    for item in calls:
        record = next(
            message
            for message in reversed(messages)
            if message.get("role") == "tool" and message.get("tool_call_id") == item.id
        )
        value = json.loads(record["content"])
        assert isinstance(value, dict)
        results[item.id] = value
    return results


async def test_ordinary_reschedule_keeps_identity_routing_content_interval_and_counts(
    reschedule_case: Case,
) -> None:
    runtime, _ = reschedule_case
    item = runtime.reminders.create(
        "保留原约定", now() + 600, "不能换标题或来源", 120, reply_origin=original_origin()
    )
    # A real fire supplies nonzero occurrence history; no Attention consumer runs.
    await fire_due(runtime.reminders, item)
    before = runtime.reminders.update(item["id"], {"enabled": False})
    assert before["fired_count"] == 1 and before["last_fired_at"] == item["due_at"]
    notices = runtime.store.list("notification")
    due_at = now() + 1800
    result = await execute(runtime, [call("ordinary", TOOL, id=item["id"], due_at=due_at)])
    expected = {**before, "due_at": due_at, "enabled": True}
    assert result["ordinary"] == expected
    assert runtime.store.get("reminder", item["id"]) == expected
    assert "followup" not in expected and "followup_revision" not in expected
    assert runtime.store.list("notification") == notices


async def test_valid_binding_advances_and_unbound_host_counter_never_resets(
    reschedule_case: Case,
) -> None:
    runtime, services = reschedule_case
    task = await services.commitments.save("可继续只读复盘的目标")
    bound = runtime.reminders.create(
        "保留准确目标", now() + 600, commitment_id=task["id"], reply_origin=original_origin()
    )
    unbound = runtime.reminders.create(
        "曾关联后解绑", now() + 700, commitment_id=task["id"], reply_origin=original_origin()
    )
    unbound = runtime.reminders.update(unbound["id"], {"commitment_id": None, "enabled": False})
    assert unbound["followup_revision"] == 2 and "followup" not in unbound
    due_at = now() + 1800
    result = await execute(
        runtime,
        [
            call("bound", TOOL, id=bound["id"], due_at=due_at),
            call("unbound", TOOL, id=unbound["id"], due_at=due_at),
        ],
    )
    expected_bound = {
        **bound,
        "due_at": due_at,
        "followup_revision": 2,
        "followup": {**bound["followup"], "revision": 2},
    }
    expected_unbound = {**unbound, "due_at": due_at, "enabled": True, "followup_revision": 3}
    assert result["bound"] == runtime.store.get("reminder", bound["id"]) == expected_bound
    assert result["unbound"] == runtime.store.get("reminder", unbound["id"]) == expected_unbound
    assert "followup" not in expected_unbound
    assert services.commitments.get(task["id"]) == task


async def test_terminal_damaged_and_missing_bound_targets_leave_all_reminders_unchanged(
    reschedule_case: Case,
) -> None:
    runtime, services = reschedule_case
    calls: list[ToolCall] = []
    due_at = now() + 1800
    for status in ("done", "cancelled"):
        task = await services.commitments.save(f"后来进入{status}的目标")
        item = runtime.reminders.create(f"终态{status}", now() + 600, commitment_id=task["id"])
        await services.commitments.save(
            task["title"], identifier=task["id"], status=status, evidence="合成边界目标明确终止"
        )
        calls.append(call(status, TOOL, id=item["id"], due_at=due_at))
    task = await services.commitments.save("损坏绑定的准确目标")
    damaged = runtime.reminders.create("不允许降级普通提醒", now() + 600, commitment_id=task["id"])
    # Honest corruption control: alter only the binding version of a real item.
    runtime.store.put("reminder", {**damaged, "followup": {**damaged["followup"], "version": 2}})
    calls.append(call("damaged", TOOL, id=damaged["id"], due_at=due_at))
    missing_task = await services.commitments.save("随后缺失的准确目标")
    missing = runtime.reminders.create("任务缺失", now() + 600, commitment_id=missing_task["id"])
    assert runtime.store.delete("commitment", missing_task["id"])
    calls.extend(
        [
            call("missing-task", TOOL, id=missing["id"], due_at=due_at),
            call("missing-reminder", TOOL, id="missing-reminder", due_at=due_at),
        ]
    )
    before = state(runtime)
    result = await execute(runtime, calls)
    assert all(value.get("error") for value in result.values())
    assert state(runtime) == before


async def test_invalid_due_and_schema_fields_cannot_partially_edit_the_original(
    reschedule_case: Case,
) -> None:
    runtime, _ = reschedule_case
    item = runtime.reminders.create("无效参数不改约定", now() + 600)
    invalid: dict[str, Any] = {
        "past": now() - 60,
        "nan": float("nan"),
        "infinity": float("inf"),
        "negative-infinity": float("-inf"),
        "string": "tomorrow",
        "boolean": True,
        "null": None,
    }
    # NaN/Infinity deliberately exercise the parser's nonstandard JSON inputs;
    # they are not claimed to be legal protocol JSON or schema-only failures.
    calls = [call(key, TOOL, id=item["id"], due_at=value) for key, value in invalid.items()]
    calls.append(call("extra-field", TOOL, id=item["id"], due_at=now() + 1800, title="不能覆盖"))
    before = state(runtime)
    result = await execute(runtime, calls)
    assert all(value.get("error") for value in result.values())
    assert state(runtime) == before
    started = {
        event["data"]["call_id"]
        for event in runtime.store.events()
        if event["type"] == "tool.started"
    }
    assert not started.intersection({"string", "boolean", "null", "extra-field"})


async def test_ask_disabled_and_last_await_maintenance_keep_the_write_unexecuted(
    reschedule_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = reschedule_case
    item = runtime.reminders.create("权限门前不能改期", now() + 600)
    due_at = now() + 1800
    before = state(runtime)

    async def reject_approval() -> None:
        await eventually(lambda: bool(runtime.permissions.pending))
        identifier = next(iter(runtime.permissions.pending))
        approval = runtime.store.get("approval", identifier)
        assert approval is not None and approval["tool"] == TOOL and approval["risk"] == "write"
        assert state(runtime) == before
        await runtime.permissions.resolve(identifier, "reject")

    result = await execute(
        runtime,
        [call("ask", TOOL, id=item["id"], due_at=due_at)],
        mode="ask",
        before_finish=reject_approval,
    )
    assert result["ask"].get("error") and state(runtime) == before
    runtime.config.update({"disabled_tools": [TOOL]})
    await runtime.reconfigure()
    result = await execute(
        runtime, [call("disabled", TOOL, id=item["id"], due_at=due_at)], available=False
    )
    assert result["disabled"].get("error") and state(runtime) == before
    runtime.config.update({"disabled_tools": []})
    await runtime.reconfigure()
    original_emit = runtime.bus.emit
    paused = False

    async def pause_after_started(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        nonlocal paused
        event = await original_emit(event_type, data, session_id)
        if event_type == "tool.started" and data.get("call_id") == "maintenance":
            assert data["name"] == TOOL
            runtime.maintenance.admission.pause()
            paused = True
        return event

    monkeypatch.setattr(runtime.bus, "emit", pause_after_started)
    try:
        result = await execute(runtime, [call("maintenance", TOOL, id=item["id"], due_at=due_at)])
        assert paused and result["maintenance"].get("error")
        assert state(runtime) == before
    finally:
        runtime.maintenance.admission.release()
    assert not any(
        event["type"] == "tool.started" and event["data"].get("call_id") in {"ask", "disabled"}
        for event in runtime.store.events()
    )
