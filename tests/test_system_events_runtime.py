"""Integration contract for the host wiring; run after Runtime/Settings are connected."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_attention import attention_inputs, eventually
from test_runtime import FakeClient, make_runtime

from muli.attention import AttentionInbox
from muli.providers import Message, ModelTurn, StreamEvent
from muli.runtime import Runtime


def inbox_for(runtime: Runtime) -> AttentionInbox:
    runtime.config.update(
        {
            "attention": {"coalesce_seconds": 0},
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
        }
    )
    return AttentionInbox(runtime, system_settings=lambda: runtime.config.settings.system_events)


def fail_streams(runtime: Runtime, seen: list[list[Message]], *, count: int | None) -> None:
    class Failed(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            self.seen.append(list(messages))
            if count is None or len(self.seen) <= count:
                raise ValueError("PRIVATE_PROVIDER_CREDENTIAL")
            yield StreamEvent("done", turn=ModelTurn(content="已告知系统运行失败"))

    runtime.client_factory = lambda _: Failed([], seen)  # type: ignore[assignment,return-value]


@pytest.mark.parametrize("notification_fails", [False, True])
async def test_runtime_failure_wakes_once_and_notification_failure_cannot_reactivate(
    tmp_path: Path, notification_fails: bool
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox = inbox_for(runtime)
    fail_streams(runtime, seen, count=None if notification_fails else 1)
    try:
        await runtime.startup()
        await inbox.start()
        receipt = await runtime.submit("真实主人任务", event_id="owner-first")
        with pytest.raises(ValueError, match="PRIVATE_PROVIDER_CREDENTIAL"):
            await runtime.wait(receipt["run_id"])
        expected = 2 if notification_fails else 1
        await eventually(
            lambda: (
                len(seen) == 2
                and not runtime.tasks
                and runtime.system_events.status()["persisted"] == expected
                and inbox.cursor == runtime.store.event_position()
            )
        )
        records = runtime.store.list("system_log")
        assert len(records) == expected
        owner = next(record for record in records if record["run_id"] == receipt["run_id"])
        assert owner["component"] == "runtime" and owner["code"] == "runtime_failed"
        assert owner["session_id"] == receipt["session_id"] and owner["notify"] is True
        assert sum(record["notify"] is True for record in records) == 1
        inputs = attention_inputs(runtime)
        assert len(inputs) == 1 and inputs[0]["route"] is None
        assert "PRIVATE_PROVIDER_CREDENTIAL" not in inputs[0]["content"]
        assert "PRIVATE_PROVIDER_CREDENTIAL" not in json.dumps(records)
        if notification_fails:
            row = runtime.store.db.execute(
                "SELECT run_id FROM ingress WHERE json_extract(payload,'$.principal')='system:attention'"
            ).fetchone()
            assert row is not None and row["run_id"]
            failed = next(record for record in records if record["run_id"] == row["run_id"])
            assert failed["notify"] is False
    finally:
        await inbox.close()
        await runtime.close()
    assert runtime.system_events.status()["running"] is False
    assert runtime.system_events.status()["queued"] == 0


async def test_new_owner_run_keeps_its_own_failure_attribution_after_failed_attention(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox = inbox_for(runtime)
    fail_streams(runtime, seen, count=None)
    receipts: list[dict[str, Any]] = []
    try:
        await runtime.startup()
        await inbox.start()
        for index in range(2):
            receipt = await runtime.submit(f"主人任务{index}", event_id=f"owner-{index}")
            receipts.append(receipt)
            with pytest.raises(ValueError, match="PRIVATE_PROVIDER_CREDENTIAL"):
                await runtime.wait(receipt["run_id"])
            expected = (index + 1) * 2
            await eventually(
                lambda expected=expected: (
                    len(seen) == expected
                    and not runtime.tasks
                    and runtime.system_events.status()["persisted"] == expected
                    and inbox.cursor == runtime.store.event_position()
                )
            )
        records = runtime.store.list("system_log")
        assert len(records) == 4 and len(attention_inputs(runtime)) == 2
        owner_ids = {receipt["run_id"] for receipt in receipts}
        assert len(owner_ids) == 2
        assert {record["run_id"] for record in records if record["notify"]} == owner_ids
        assert all(record["session_id"] == receipts[0]["session_id"] for record in records)
    finally:
        await inbox.close()
        await runtime.close()


async def test_capture_and_notification_can_be_disabled_and_reenabled(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="恢复后的独立通知")])
    runtime.config.update({"system_events": {"enabled": False}})
    inbox = inbox_for(runtime)
    try:
        await runtime.startup()
        await inbox.start()
        assert runtime.system_logger is not None
        runtime.system_logger.error("disabled")
        assert not runtime.system_events.status()["running"]
        assert not runtime.store.list("system_log")

        runtime.config.update(
            {
                "system_events": {
                    "enabled": True,
                    "capture_level": "warning",
                    "notify_level": "warning",
                    "notify_enabled": False,
                }
            }
        )
        await runtime.reconfigure()
        await inbox.reconfigure()
        assert runtime.system_logger is not None
        runtime.system_logger.warning("只记录", extra={"muli_code": "management_failed"})
        await eventually(
            lambda: (
                runtime.system_events.status()["persisted"] == 1
                and inbox.cursor == runtime.store.event_position()
            )
        )
        assert not seen and not attention_inputs(runtime)

        runtime.config.update({"system_events": {"notify_enabled": True}})
        await runtime.reconfigure()
        await inbox.reconfigure()
        assert runtime.system_logger is not None
        runtime.system_logger.warning("恢复通知", extra={"muli_code": "channel_failed"})
        await eventually(lambda: len(seen) == 1 and not runtime.tasks)
        assert len(attention_inputs(runtime)) == 1
        assert runtime.system_events.status()["persisted"] == 2
        assert len(runtime.store.list("system_log")) == 2
    finally:
        await inbox.close()
        await runtime.close()


async def test_preclaim_attention_failure_is_not_notified_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox = inbox_for(runtime)
    original = runtime.bus.emit
    failed_run_id: str | None = None

    async def emit(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        nonlocal failed_run_id
        if event_type == "run.started" and failed_run_id is None:
            assert session_id is not None
            failed_run_id = data["id"]
            # Remove the pending input without assigning run_id. This represents
            # a failure before claim while avoiding an unrelated pending-run retry.
            runtime.store.cancel_inputs(session_id)
            raise ValueError("启动事件在claim之前失败")
        return await original(event_type, data, session_id)

    monkeypatch.setattr(runtime.bus, "emit", emit)
    try:
        await runtime.startup()
        await inbox.start()
        assert runtime.system_logger is not None
        runtime.system_logger.error("独立原始事件", extra={"muli_component": "channels"})
        await eventually(
            lambda: (
                failed_run_id is not None
                and not runtime.tasks
                and runtime.system_events.status()["persisted"] == 2
                and inbox.cursor == runtime.store.event_position()
            )
        )
        records = runtime.store.list("system_log")
        failure = next(record for record in records if record["run_id"] == failed_run_id)
        assert failure["notify"] is False and not seen
        assert len(attention_inputs(runtime)) == 1
        assert (
            runtime.store.db.execute(
                "SELECT 1 FROM ingress WHERE run_id=?", (failed_run_id,)
            ).fetchone()
            is None
        )
    finally:
        await inbox.close()
        await runtime.close()


async def test_collector_starts_before_components_and_stops_before_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    inbox_for(runtime)
    started = runtime.notifications.start
    closed = runtime.store.close
    checked: list[str] = []

    async def start_component() -> None:
        assert runtime.system_events.status()["running"]
        assert runtime.system_logger is not None
        runtime.system_logger.error("启动组件失败资料", extra={"muli_component": "media"})
        checked.append("start")
        await started()

    def close_store() -> None:
        assert not runtime.system_events.status()["running"]
        assert runtime.system_events.status()["queued"] == 0
        checked.append("close")
        closed()

    monkeypatch.setattr(runtime.notifications, "start", start_component)
    monkeypatch.setattr(runtime.store, "close", close_store)
    try:
        await runtime.startup()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
    finally:
        await runtime.close()
    assert checked == ["start", "close"]
