from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_attention import attention_inputs, eventually
from test_runtime import FakeClient, make_runtime

from muli.attention import AttentionInbox
from muli.events import EventBus
from muli.providers import Message, ModelTurn, StreamEvent
from muli.store import Store, new_id
from muli.system_events import (
    SystemEvents,
    SystemEventsSettings,
    attention_source,
    system_log_context,
)


def isolated_logger() -> logging.Logger:
    logger = logging.Logger("system-events-test", logging.DEBUG)
    logger.propagate = False
    return logger


@pytest.fixture
async def system(tmp_path: Path) -> AsyncIterator[SystemEvents]:
    store = Store(tmp_path / "events.sqlite")
    service = SystemEvents(store, EventBus(store), SystemEventsSettings)
    try:
        yield service
    finally:
        await service.close()
        store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "true"},
        {"notify_enabled": 1},
        {"capture_level": "info"},
        {"notify_level": "ERROR"},
        {"queue_capacity": 0},
        {"queue_capacity": True},
        {"max_records": 10_001},
        {"dedup_seconds": float("inf")},
        {"dedup_seconds": True},
        {"extra": True},
    ],
)
def test_settings_reject_unbounded_or_implicit_values(changes: dict[str, Any]) -> None:
    policy = SystemEventsSettings()
    assert policy.enabled and policy.notify_enabled
    assert policy.capture_level == policy.notify_level == "error"
    with pytest.raises(ValueError):
        SystemEventsSettings.model_validate(changes)


def test_settings_keep_the_original_json_schema_defaults_roundtrip_and_frozen_policy() -> None:
    from muli.config import Settings

    defaults: dict[str, Any] = {
        "enabled": True,
        "notify_enabled": True,
        "capture_level": "error",
        "notify_level": "error",
        "queue_capacity": 128,
        "max_records": 1000,
        "dedup_seconds": 60,
    }
    # This is the original public JSON schema, not a copied model definition.
    schema: dict[str, Any] = {
        "additionalProperties": False,
        "properties": {
            "enabled": {"default": True, "title": "Enabled", "type": "boolean"},
            "notify_enabled": {"default": True, "title": "Notify Enabled", "type": "boolean"},
            "capture_level": {
                "default": "error",
                "enum": ["warning", "error", "critical"],
                "title": "Capture Level",
                "type": "string",
            },
            "notify_level": {
                "default": "error",
                "enum": ["warning", "error", "critical"],
                "title": "Notify Level",
                "type": "string",
            },
            "queue_capacity": {
                "default": 128,
                "minimum": 1,
                "maximum": 4096,
                "title": "Queue Capacity",
                "type": "integer",
            },
            "max_records": {
                "default": 1000,
                "minimum": 1,
                "maximum": 10_000,
                "title": "Max Records",
                "type": "integer",
            },
            "dedup_seconds": {
                "default": 60,
                "minimum": 1,
                "maximum": 3600,
                "title": "Dedup Seconds",
                "type": "number",
            },
        },
        "title": "SystemEventsSettings",
        "type": "object",
    }
    policy = SystemEventsSettings()
    assert json.loads(policy.model_dump_json()) == defaults
    assert SystemEventsSettings.model_json_schema() == schema
    assert Settings.model_json_schema()["$defs"]["SystemEventsSettings"] == schema
    custom = defaults | {"enabled": False, "capture_level": "warning", "dedup_seconds": 1.5}
    restored = SystemEventsSettings.model_validate_json(json.dumps(custom))
    assert json.loads(restored.model_dump_json()) == custom
    configured = Settings.model_validate({"system_events": custom})
    assert configured.model_dump(mode="json")["system_events"] == custom
    assert Settings.model_validate_json(configured.model_dump_json()).system_events == restored
    with pytest.raises(ValidationError) as caught:
        policy.enabled = False
    assert caught.value.errors()[0]["type"] == "frozen_instance"


async def test_thresholds_never_format_or_persist_log_payload(system: SystemEvents) -> None:
    class Private:
        def __str__(self) -> str:
            raise AssertionError("不得格式化日志")

        def __repr__(self) -> str:
            raise AssertionError("不得表示日志对象")

    logger = isolated_logger()
    adapter = await system.start(logger)
    adapter.warning("低于录入阈值")
    logger.error("没有实例归属")
    adapter.error(
        Private(),
        Private(),
        extra={"muli_component": "private" * 1000, "muli_code": Private(), "secret": "SECRET"},
        exc_info=(ValueError, ValueError("SECRET"), None),
    )
    await eventually(lambda: system.status()["persisted"] == 1)
    record = system.store.list("system_log")[0]
    assert record["component"] == record["code"] == "unknown"
    assert record["level"] == "error" and record["count"] == 1
    assert "SECRET" not in json.dumps(system.store.events())
    assert "private" not in json.dumps(record)
    assert logger.level == logging.DEBUG and len(logger.handlers) == 1


async def test_thread_burst_has_bounded_queue_and_one_wakeup(
    system: SystemEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    system.settings = lambda: SystemEventsSettings(queue_capacity=8)
    adapter = await system.start(isolated_logger())
    thread_id = threading.get_ident()
    original = system.store._put

    def put(kind: str, item: dict[str, Any]) -> None:
        assert threading.get_ident() == thread_id
        original(kind, item)

    monkeypatch.setattr(system.store, "_put", put)

    def produce() -> None:
        for _ in range(64):
            adapter.error(
                "不保留原文", extra={"muli_component": "runtime", "muli_code": "runtime_failed"}
            )

    threads = [threading.Thread(target=produce) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    before = system.status()
    assert before["queued"] == before["captured"] == 8
    assert before["overflow"] == 248 and before["wakeups"] == 1
    assert not system.store.list("system_log")
    await eventually(lambda: system.status()["persisted"] + system.status()["duplicates"] == 8)
    assert sum(record["count"] for record in system.store.list("system_log")) == 8
    assert system.status()["queued"] == 0 and system.last_error is None


async def test_dedup_updates_evidence_without_second_event_or_notification(
    system: SystemEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("muli.system_events.time.time", lambda: 1200.0)
    adapter = await system.start(isolated_logger())
    for _ in range(3):
        adapter.error("同类错误")
    await eventually(lambda: system.status()["duplicates"] == 2)
    records = system.store.list("system_log")
    assert len(records) == len(system.store.events()) == 1
    record = records[0]
    assert record["count"] == 3 and system.bus.revision == 1
    event = system.store.events()[0]
    source = attention_source(system.store, event, [], SystemEventsSettings())
    assert source is not None and json.loads(source.detail)["count"] == 3


async def test_retention_and_canonical_event_id_reject_expired_or_replayed_events(
    system: SystemEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    system.settings = lambda: SystemEventsSettings(max_records=1)
    monkeypatch.setattr("muli.system_events.time.time", lambda: 1200.0)
    adapter = await system.start(isolated_logger())
    adapter.error("一", extra={"muli_code": "runtime_failed"})
    await eventually(lambda: system.status()["persisted"] == 1)
    old = system.store.events()[0]
    monkeypatch.setattr("muli.system_events.time.time", lambda: 1201.0)
    adapter.error("二", extra={"muli_code": "channel_failed"})
    await eventually(lambda: system.status()["pruned"] == 1)
    assert len(system.store.list("system_log")) == len(system.store.events()) == 1
    assert attention_source(system.store, old, [], SystemEventsSettings()) is None
    monkeypatch.setattr("muli.system_events.time.time", lambda: 1202.0)
    adapter.error("一的同窗重现", extra={"muli_code": "runtime_failed"})
    await eventually(lambda: system.status()["persisted"] == 3)
    current = system.store.events()[0]
    assert current["data"]["id"] == old["data"]["id"]
    assert current["id"] > old["id"]
    assert attention_source(system.store, old, [], SystemEventsSettings()) is None
    forged = system.store.event("system.log", current["data"])
    assert attention_source(system.store, forged, [], SystemEventsSettings()) is None
    assert attention_source(system.store, current, [], SystemEventsSettings()) is not None


async def test_instances_sharing_logger_and_store_do_not_capture_each_other(
    system: SystemEvents,
) -> None:
    other = SystemEvents(system.store, system.bus, SystemEventsSettings)
    base = isolated_logger()
    left, right = await system.start(base), await other.start(base)
    try:
        left.error("left")
        await eventually(lambda: system.status()["persisted"] == 1)
        assert other.status()["captured"] == 0
        await system.close()
        assert len(base.handlers) == 1
        left.error("已关闭")
        right.critical("right")
        await eventually(lambda: other.status()["persisted"] == 1)
        assert system.status()["captured"] == 1
    finally:
        await other.close()
    assert not base.handlers


async def test_close_drops_pending_queue_and_late_adapter_is_inert(system: SystemEvents) -> None:
    logger = isolated_logger()
    adapter = await system.start(logger)
    adapter.error("尚未处理")
    await system.close()
    await system.close()
    adapter.error("停止后")
    await asyncio.sleep(0)
    assert system.status()["dropped_on_close"] == 1
    assert system.status()["queued"] == 0 and not system.status()["running"]
    assert not logger.handlers and not system.store.events()


async def test_reconfigure_applies_capture_and_notify_settings(system: SystemEvents) -> None:
    policy = SystemEventsSettings()
    system.settings = lambda: policy
    base = isolated_logger()
    await system.start(base)
    policy = SystemEventsSettings(capture_level="warning", notify_enabled=False)
    adapter = await system.reconfigure()
    assert adapter is not None
    adapter.warning("warning")
    await eventually(lambda: system.status()["persisted"] == 1)
    event = system.store.events()[0]
    assert attention_source(system.store, event, [], policy) is None
    assert (
        attention_source(system.store, event, [], SystemEventsSettings(notify_level="warning"))
        is not None
    )
    policy = SystemEventsSettings(enabled=False)
    await system.reconfigure()
    adapter.error("disabled")
    await asyncio.sleep(0)
    assert not system.status()["running"] and not base.handlers


async def test_reconfigure_rejects_inflight_old_handler_emit(
    system: SystemEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = SystemEventsSettings(capture_level="warning")
    system.settings = lambda: policy
    adapter = await system.start(isolated_logger())
    entered, release = threading.Event(), threading.Event()
    original = system._capture

    def delayed(fields: dict[str, Any], handler: Any) -> None:
        entered.set()
        if not release.wait(2):
            return
        original(fields, handler)

    monkeypatch.setattr(system, "_capture", delayed)
    thread = threading.Thread(target=lambda: adapter.warning("旧代线程日志"))
    thread.start()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        policy = SystemEventsSettings(capture_level="critical")
        await system.reconfigure()
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    await asyncio.sleep(0)
    assert system.status()["captured"] == 0 and not system.store.events()


@pytest.mark.parametrize("stage", ["persist", "notify", "retention"])
async def test_consumer_failures_have_fixed_status_and_never_log_recursively(
    system: SystemEvents, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    base = isolated_logger()
    adapter = await system.start(base)
    await asyncio.sleep(0)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("CREDENTIAL")

    async def fail_async() -> None:
        raise ValueError("CREDENTIAL")

    if stage == "persist":
        monkeypatch.setattr(system.store, "_put", fail)
    elif stage == "notify":
        monkeypatch.setattr(system.bus, "notify", fail_async)
    else:
        monkeypatch.setattr(system, "_prune", fail)
    adapter.error("单一原错误")
    await eventually(lambda: system.last_error is not None)
    for _ in range(3):
        await asyncio.sleep(0)
    assert system.status()["captured"] == 1 and system.status()["queued"] == 0
    assert "CREDENTIAL" not in json.dumps(system.status())
    if stage == "persist":
        assert not system.store.events() and not system.store.list("system_log")
        assert system.status()["failed"] == 1
    else:
        assert len(system.store.events()) == len(system.store.list("system_log")) == 1
    if stage == "retention":
        assert not system.status()["running"]
        adapter.error("暂停后")
        assert system.status()["captured"] == 1


async def test_attention_uses_independent_log_policy_and_old_constructor_skips_it(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="已知悉系统错误")])
    runtime.config.update({"attention": {"events": [], "coalesce_seconds": 0}})
    policy = SystemEventsSettings()
    inbox = AttentionInbox(runtime, system_settings=lambda: policy)
    system = SystemEvents(runtime.store, runtime.bus, lambda: policy)
    try:
        adapter = await system.start(isolated_logger())
        adapter.error(
            "CREDENTIAL", extra={"muli_component": "runtime", "muli_code": "runtime_failed"}
        )
        await eventually(lambda: system.status()["persisted"] == 1)
        event = runtime.store.events()[0]
        legacy = AttentionInbox(runtime)
        assert legacy._record(event, runtime.config.settings.attention) is None
        await inbox.start()
        await eventually(lambda: len(seen) == 1 and not runtime.tasks)
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1 and "runtime_failed" in inputs[0]["content"]
        assert "CREDENTIAL" not in inputs[0]["content"]
        assert inputs[0]["route"] is None and inputs[0]["principal"] == "system:attention"
    finally:
        await system.close()
        await inbox.close()
        await runtime.close()


async def test_context_and_attention_ingress_failures_are_recorded_without_reactivation(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    policy = SystemEventsSettings()
    inbox = AttentionInbox(runtime, system_settings=lambda: policy)
    system = SystemEvents(runtime.store, runtime.bus, lambda: policy)
    session = runtime.main_session()
    run_id = new_id()
    runtime.store.put("run", {"id": run_id, "session_id": session["id"], "status": "failed"})
    runtime.conversation.enqueue(
        session["id"],
        "系统资料",
        source="system:attention",
        principal="system:attention",
        mode="ask",
        max_pending=4,
        internal=True,
    )
    runtime.store.claim_inputs(session["id"], run_id, limit=4)
    try:
        adapter = await system.start(isolated_logger())
        with system_log_context(run_id=run_id, session_id=session["id"]):
            adapter.error("通知模型失败", extra={"muli_code": "runtime_failed"})
        with system_log_context(notify=False):
            with system_log_context(notify=True):
                adapter.error("通知设施失败", extra={"muli_code": "channel_failed"})
        adapter.error("消费者错误", extra={"muli_component": "system_events"})
        adapter.critical("引用失配", extra={"muli_run_id": run_id, "muli_session_id": new_id()})
        await eventually(lambda: system.status()["persisted"] == 4)
        for event in runtime.store.events():
            assert inbox._record(event, runtime.config.settings.attention, policy) is None
        assert all(record["notify"] is False for record in runtime.store.list("system_log"))
        assert len(attention_inputs(runtime)) == 1
    finally:
        await system.close()
        await inbox.close()
        await runtime.close()


async def test_log_policy_revoked_while_waiting_for_configuration_lock(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="不应运行")])
    runtime.config.update({"attention": {"coalesce_seconds": 0}})
    policy = SystemEventsSettings()
    inbox = AttentionInbox(runtime, system_settings=lambda: policy)
    system = SystemEvents(runtime.store, runtime.bus, lambda: policy)
    entered = asyncio.Event()
    original = runtime.submit_attention

    async def submit(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        entered.set()
        return await original(*args, **kwargs)

    runtime.submit_attention = submit  # type: ignore[method-assign]
    try:
        adapter = await system.start(isolated_logger())
        await inbox.start()
        await eventually(lambda: inbox.status()["waiting"] == "events")
        await runtime.configuration_lock.acquire()
        try:
            adapter.error("等待锁时的新错误")
            await asyncio.wait_for(entered.wait(), 1)
            policy = SystemEventsSettings(notify_enabled=False)
        finally:
            runtime.configuration_lock.release()
        await eventually(lambda: inbox.last_error == "ValueError")
        assert not seen and not attention_inputs(runtime)
        await runtime.bus.notify()
        await eventually(lambda: inbox.cursor == runtime.store.event_position())
        assert not seen and not runtime.store.list("attention_receipt")
    finally:
        await system.close()
        await inbox.close()
        await runtime.close()


async def test_failed_notification_run_does_not_notify_its_own_model_failure(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update({"attention": {"coalesce_seconds": 0}})
    policy = SystemEventsSettings()
    inbox = AttentionInbox(runtime, system_settings=lambda: policy)
    system = SystemEvents(runtime.store, runtime.bus, lambda: policy)
    try:
        adapter = await system.start(isolated_logger())

        class Failed(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                self.seen.append(list(messages))
                session_id = runtime.main_session()["id"]
                adapter.error(
                    "模型凭据不得进入事件",
                    extra={
                        "muli_component": "runtime",
                        "muli_code": "runtime_failed",
                        "muli_run_id": runtime.run_ids[session_id],
                        "muli_session_id": session_id,
                    },
                )
                raise ValueError("假模型失败")
                yield StreamEvent("done", turn=ModelTurn())

        runtime.client_factory = lambda _: Failed([], seen)  # type: ignore[assignment,return-value]
        await inbox.start()
        adapter.error("最初业务错误", extra={"muli_component": "channels"})
        await eventually(
            lambda: (
                len(seen) == 1
                and system.status()["persisted"] == 2
                and not runtime.tasks
                and inbox.cursor == runtime.store.event_position()
            )
        )
        records = runtime.store.list("system_log")
        assert sum(record["notify"] is True for record in records) == 1
        assert len(attention_inputs(runtime)) == 1
        assert "凭据" not in json.dumps(records, ensure_ascii=False)
    finally:
        await system.close()
        await inbox.close()
        await runtime.close()
