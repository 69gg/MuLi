"""Integration contract for the host wiring; run after Runtime/Settings are connected."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_attention import attention_inputs, eventually
from test_channel_ownership_policy import assigned, receive
from test_runtime import FakeClient, make_runtime

from muli.attention import AttentionInbox
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store import new_id


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


async def consume_until_idle(
    runtime: Runtime, inbox: AttentionInbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumed = asyncio.Event()
    original_consume = inbox._consume

    async def observe_consume() -> bool:
        progressed = await original_consume()
        if (
            inbox.cursor == runtime.store.event_position()
            and not runtime.tasks
            and runtime.system_events.status()["queued"] == 0
        ):
            consumed.set()
        return progressed

    monkeypatch.setattr(inbox, "_consume", observe_consume)
    await inbox.start()
    await asyncio.wait_for(consumed.wait(), 3)
    await inbox.close()


async def test_team_failure_with_durable_parent_report_does_not_reenter_main_via_system_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, main_requests = make_runtime(tmp_path, [])
    # Construct the real consumer before the failure, but start it only after
    # the original report and the asynchronous log have both fully settled.
    inbox = inbox_for(runtime)
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    await runtime.reconfigure()

    def factory(config: ModelConfig) -> FakeClient:
        class Client(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                if config.model == "worker":
                    raise ValueError("offline-worker-failure")
                self.seen.append(list(messages))
                assert len(self.seen) == 1, "原失败已有父报告，故障日志不应再次调用主模型"
                yield StreamEvent("done", turn=ModelTurn(content="terminal-report-handled"))

        return Client([], main_requests)

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        await runtime.startup()
        main_id = runtime.main_session()["id"]
        worker = await runtime.team.spawn("failed-worker", "offline failure", parent_id=main_id)
        team_tasks = tuple(runtime.team._tasks)
        assert team_tasks
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in team_tasks)), 3)
        if main_task := runtime.tasks.get(main_id):
            await asyncio.wait_for(asyncio.shield(main_task), 3)
        worker = runtime.team.get(worker["id"])
        failed_run_id = worker["run_id"]
        assert worker["status"] == "failed"
        assert runtime.store.get("run", failed_run_id)["status"] == "failed"
        report = runtime.store.get("team_report", failed_run_id)
        assert report is not None
        assert report["agent_id"] == worker["id"] and report["parent_id"] == main_id
        assert report["execution_status"] == "failed" and report["status"] == "delivered"
        report_inputs = runtime.store.db.execute(
            "SELECT id,state,run_id FROM ingress WHERE session_id=? "
            "AND json_extract(payload,'$.source')=? "
            "AND json_extract(payload,'$.principal')=?",
            (main_id, "team:" + worker["id"], "agent:" + worker["id"]),
        ).fetchall()
        assert len(report_inputs) == 1 and report_inputs[0]["state"] == "delivered"
        assert len(main_requests) == 1 and not runtime.attention_busy

        def log_persisted() -> bool:
            return runtime.system_events.status()["queued"] == 0 and any(
                record.get("run_id") == failed_run_id for record in runtime.store.list("system_log")
            )

        async with asyncio.timeout(3), runtime.bus.changed:
            await runtime.bus.changed.wait_for(log_persisted)
        failure_logs = [
            record
            for record in runtime.store.list("system_log")
            if record.get("run_id") == failed_run_id
        ]
        assert len(failure_logs) == 1
        failure_log = failure_logs[0]
        assert failure_log["session_id"] == worker["id"]
        assert failure_log["component"] == "runtime" and failure_log["code"] == "runtime_failed"

        consumed = asyncio.Event()
        original_consume = inbox._consume

        async def observe_consume() -> bool:
            progressed = await original_consume()
            if (
                inbox.cursor == runtime.store.event_position()
                and not runtime.tasks
                and runtime.system_events.status()["queued"] == 0
            ):
                consumed.set()
            return progressed

        # Observe a completed pass through the real scheduler; do not inject a
        # log, report, attention receipt or ingress to manufacture the evidence.
        monkeypatch.setattr(inbox, "_consume", observe_consume)
        await inbox.start()
        await asyncio.wait_for(consumed.wait(), 3)
        await inbox.close()
        log_receipts = [
            receipt
            for receipt in runtime.store.list("attention_receipt")
            if receipt["kind"] == "system_log" and receipt["record_id"] == failure_log["id"]
        ]
        log_input_ids = {
            hashlib.sha256(
                f"{main_id}\0system:attention\0{receipt['batch_id']}".encode()
            ).hexdigest()
            for receipt in log_receipts
        }
        log_inputs = [
            (row["id"], row["run_id"])
            for row in runtime.store.db.execute(
                "SELECT id,run_id FROM ingress WHERE session_id=? "
                "AND json_extract(payload,'$.source')='system:attention' "
                "AND json_extract(payload,'$.principal')='system:attention'",
                (main_id,),
            )
            if row["id"] in log_input_ids
        ]
        assert log_inputs == [], f"failed_run={failed_run_id}; system_log={failure_log['id']}"
        assert log_receipts == [] and len(main_requests) == 1
        assert runtime.store.get("team_report", failed_run_id) == report
        assert runtime.system_events.status()["queued"] == 0
    finally:
        await inbox.close()
        await runtime.close()


async def test_component_error_during_real_team_run_remains_notifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, main_requests = make_runtime(tmp_path, [])
    inbox = inbox_for(runtime)
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    await runtime.reconfigure()

    def factory(config: ModelConfig) -> FakeClient:
        class Client(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                if config.model == "worker":
                    assert runtime.system_logger is not None
                    runtime.system_logger.error(
                        "offline-component-failure",
                        extra={"muli_component": "media", "muli_code": "media_failed"},
                    )
                else:
                    self.seen.append(list(messages))
                yield StreamEvent("done", turn=ModelTurn(content="handled"))

        return Client([], main_requests)

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        await runtime.startup()
        main_id = runtime.main_session()["id"]
        worker = await runtime.team.spawn("successful-worker", "offline task", parent_id=main_id)
        await asyncio.wait_for(
            asyncio.gather(*(asyncio.shield(task) for task in tuple(runtime.team._tasks))), 3
        )
        if main_task := runtime.tasks.get(main_id):
            await asyncio.wait_for(asyncio.shield(main_task), 3)
        worker = runtime.team.get(worker["id"])
        assert worker["status"] == "completed" and len(main_requests) == 1
        report = runtime.store.get("team_report", worker["run_id"])
        assert report is not None and report["status"] == "delivered"
        async with asyncio.timeout(3), runtime.bus.changed:
            await runtime.bus.changed.wait_for(
                lambda: (
                    runtime.system_events.status()["persisted"] == 1
                    and runtime.system_events.status()["queued"] == 0
                )
            )
        record = runtime.store.list("system_log")[0]
        assert record["run_id"] == worker["run_id"] and record["session_id"] == worker["id"]
        assert record["component"] == "media" and record["code"] == "media_failed"
        assert record["notify"] is True
        await consume_until_idle(runtime, inbox, monkeypatch)
        receipts = runtime.store.list("attention_receipt")
        assert len(receipts) == 1 and receipts[0]["record_id"] == record["id"]
        assert len(attention_inputs(runtime)) == 1 and len(main_requests) == 2
        assert runtime.store.get("team_report", worker["run_id"]) == report
    finally:
        await inbox.close()
        await runtime.close()


async def test_owned_team_failure_with_local_report_keeps_system_attention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shared prefix assigns a real, completed child through the owner's
    # channel_delegate tool; the next real webhook reopens that same child.
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        inbox: AttentionInbox = app.state.attention
        runtime.config.update({"attention": {"enabled": True, "coalesce_seconds": 0}})
        # First enable skips old history. Establish that cursor before this
        # failure, then let this same application consumer run after it settles.
        await inbox.start()
        await inbox.close()
        main_id = runtime.main_session()["id"]
        incoming = "受托渠道本轮仅核查本地状态。"
        main_requests: list[list[Message]] = []
        worker_requests: list[list[Message]] = []
        entered, release = asyncio.Event(), asyncio.Event()

        def factory(config: ModelConfig) -> FakeClient:
            class Client(FakeClient):
                async def stream(
                    self, messages: list[Message], tools: list[dict[str, Any]]
                ) -> AsyncIterator[StreamEvent]:
                    if config.model == "worker":
                        worker_requests.append(list(messages))
                        entered.set()
                        await release.wait()
                        raise ValueError("offline-owned-worker-failure")
                    self.seen.append(list(messages))
                    assert len(self.seen) == 1, "本地队友报告仅应产生一次独立故障提醒"
                    trigger = runtime._latest_main_trigger(main_id)
                    assert trigger is not None
                    assert trigger["source"] == trigger["principal"] == "system:attention"
                    yield StreamEvent("done", turn=ModelTurn(content="owned-failure-noted"))

            return Client([], main_requests)

        runtime.client_factory = factory  # type: ignore[assignment]
        try:
            lease = runtime.channel_ownership.for_agent(child_id)
            assert lease is not None and lease["state"] == "active"
            assert lease["main_id"] == main_id and lease["agent_id"] == child_id
            manager = app.state.channel_application.manager
            assert manager.deliveries() == []
            await receive(web, incoming)
            await asyncio.wait_for(entered.wait(), 3)
            worker = runtime.team.get(child_id)
            failed_run_id = worker["run_id"]
            assert worker["generation"] == 2 and worker["status"] == "running"
            supplied = next(
                item
                for item in runtime.store.messages(child_id)
                if item["role"] == "user" and item["content"] == incoming
            )
            proof = runtime.channel_ownership.proof(lease)
            assert supplied["channel_ownership"] == worker["channel_ownership"] == proof
            assert supplied["run_id"] == failed_run_id
            assert supplied["principal"] == "owner" and supplied["route"]["name"] == "onebot"
            assert supplied["route"]["sender_id"] == supplied["route"]["target"] == "7"
            assert len(worker_requests) == 1 and any(
                message.source_message_id == supplied["id"] for message in worker_requests[0]
            )
            team_tasks = tuple(runtime.team._tasks)
            assert team_tasks and not main_requests
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in team_tasks)), 3
            )
            worker = runtime.team.get(child_id)
            assert worker["status"] == "failed" and worker["run_id"] == failed_run_id
            assert runtime.store.get("run", failed_run_id)["status"] == "failed"
            report = runtime.store.get("team_report", failed_run_id)
            assert report is not None and report["status"] == "local"
            assert report["agent_id"] == child_id and report["parent_id"] == main_id
            assert report["execution_status"] == "failed" and report["channel_ownership"] == proof
            assert report["reply_origin"]["input_id"] == supplied["id"]
            report_input_id = hashlib.sha256(
                f"{main_id}\0team:{child_id}\0result:{failed_run_id}".encode()
            ).hexdigest()
            assert (
                runtime.store.db.execute(
                    "SELECT 1 FROM ingress WHERE id=?", (report_input_id,)
                ).fetchone()
                is None
            )
            assert not main_requests and not runtime.attention_busy
            assert runtime.store.pending_inputs(main_id) == 0 and main_id not in runtime.tasks

            def log_persisted() -> bool:
                return runtime.system_events.status()["queued"] == 0 and any(
                    record.get("run_id") == failed_run_id
                    for record in runtime.store.list("system_log")
                )

            async with asyncio.timeout(3), runtime.bus.changed:
                await runtime.bus.changed.wait_for(log_persisted)
            records = [
                record
                for record in runtime.store.list("system_log")
                if record.get("run_id") == failed_run_id
            ]
            assert len(records) == 1
            record = records[0]
            assert record["session_id"] == child_id
            assert record["component"] == "runtime" and record["code"] == "runtime_failed"
            await consume_until_idle(runtime, inbox, monkeypatch)
            receipts = [
                item
                for item in runtime.store.list("attention_receipt")
                if item["kind"] == "system_log" and item["record_id"] == record["id"]
            ]
            assert record["notify"] is True
            assert len(receipts) == 1
            notice_input_id = hashlib.sha256(
                f"{main_id}\0system:attention\0{receipts[0]['batch_id']}".encode()
            ).hexdigest()
            notices = runtime.store.db.execute(
                "SELECT id,run_id,state,payload FROM ingress WHERE session_id=? "
                "AND json_extract(payload,'$.source')='system:attention' "
                "AND json_extract(payload,'$.principal')='system:attention'",
                (main_id,),
            ).fetchall()
            assert len(notices) == 1 and notices[0]["id"] == notice_input_id
            notice = notices[0]
            assert notice["state"] == "delivered" and len(main_requests) == 1
            assert runtime.store.get("run", notice["run_id"])["status"] == "completed"
            assert runtime.store.get("team_report", failed_run_id) == report
            assert runtime.channel_ownership.for_agent(child_id) == lease
            assert manager.deliveries() == [] and runtime.system_events.status()["queued"] == 0
        finally:
            release.set()
            await inbox.close()


@pytest.mark.parametrize("missing_proof", ["team_kind_only", "old_agent_run", "no_active_runner"])
async def test_failure_without_exact_live_team_report_owner_keeps_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_proof: str
) -> None:
    runtime, main_requests = make_runtime(tmp_path, [])
    inbox = inbox_for(runtime)
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    await runtime.reconfigure()

    def factory(config: ModelConfig) -> FakeClient:
        class Client(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                if config.model == "worker":
                    raise ValueError("offline-worker-failure")
                self.seen.append(list(messages))
                yield StreamEvent("done", turn=ModelTurn(content="handled"))

        return Client([], main_requests)

    runtime.client_factory = factory  # type: ignore[assignment]
    original_emit = runtime.bus.emit
    invalidated: list[str] = []

    async def invalidate_report_owner(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        agent = data.get("agent", {})
        if not invalidated and event_type == "team.updated" and agent.get("status") == "running":
            # Team has registered its real driver and persisted the generation,
            # but the first await has not yet allowed that driver to start.
            if missing_proof == "old_agent_run":
                runtime.store.put("agent", {**agent, "run_id": new_id()})
            elif missing_proof == "no_active_runner":
                runner = runtime.team._active.pop(agent["id"])
                assert runner is asyncio.current_task()
            invalidated.append(missing_proof)
        return await original_emit(event_type, data, session_id)

    monkeypatch.setattr(runtime.bus, "emit", invalidate_report_owner)
    try:
        await runtime.startup()
        main_id = runtime.main_session()["id"]
        if missing_proof == "team_kind_only":
            session = runtime.new_session()
            session["kind"] = "team"
            runtime.store.put("session", session)
            runtime.store.add_message(session["id"], "user", "offline task")
            failed_run_id = runtime._launch(session, "worker", "ask")
            with pytest.raises(ValueError, match="offline-worker-failure"):
                await runtime.wait(failed_run_id)
            assert runtime.store.get("agent", session["id"]) is None
            assert runtime.store.get("team_report", failed_run_id) is None
            assert len(main_requests) == 0
        else:
            worker = await runtime.team.spawn("unproven-worker", "offline task", parent_id=main_id)
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in tuple(runtime.team._tasks))), 3
            )
            if main_task := runtime.tasks.get(main_id):
                await asyncio.wait_for(asyncio.shield(main_task), 3)
            worker = runtime.team.get(worker["id"])
            failed_run_id = worker["run_id"]
            assert invalidated == [missing_proof]
            assert worker["status"] == "failed" and len(main_requests) == 1
        baseline_requests = len(main_requests)
        async with asyncio.timeout(3), runtime.bus.changed:
            await runtime.bus.changed.wait_for(
                lambda: (
                    runtime.system_events.status()["persisted"] == 1
                    and runtime.system_events.status()["queued"] == 0
                )
            )
        record = runtime.store.list("system_log")[0]
        assert record["run_id"] == failed_run_id
        assert record["component"] == "runtime" and record["code"] == "runtime_failed"
        assert record["notify"] is True
        await consume_until_idle(runtime, inbox, monkeypatch)
        receipts = runtime.store.list("attention_receipt")
        assert len(receipts) == 1 and receipts[0]["record_id"] == record["id"]
        assert len(attention_inputs(runtime)) == 1
        assert len(main_requests) == baseline_requests + 1
    finally:
        await inbox.close()
        await runtime.close()


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


async def test_old_attention_run_remains_suppressed_after_over_one_hundred_new_inputs(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    inbox_for(runtime)
    session = runtime.main_session()
    run_id = new_id()
    runtime.store.put("run", {"id": run_id, "session_id": session["id"], "status": "failed"})
    runtime.conversation.enqueue(
        session["id"],
        "历史注意事项",
        source="system:attention",
        principal="system:attention",
        mode="ask",
        max_pending=1,
        internal=True,
    )
    runtime.store.claim_inputs(session["id"], run_id, limit=1)
    previous = runtime.store.db.execute("SELECT max(seq) FROM ingress").fetchone()[0]
    for index in range(101):
        runtime.conversation.enqueue(
            session["id"],
            "后续主人输入",
            source="web",
            principal="owner",
            mode="ask",
            max_pending=1,
            event_id=f"later-owner-{index}",
        )
        runtime.store.cancel_inputs(session["id"])
    newer_count = runtime.store.db.execute(
        "SELECT count(*) FROM ingress WHERE seq>?", (previous,)
    ).fetchone()[0]
    assert newer_count == 101
    try:
        await runtime.startup()
        assert runtime.system_logger is not None
        runtime.system_logger.error(
            "历史运行晚到的错误",
            extra={
                "muli_component": "runtime",
                "muli_code": "runtime_failed",
                "muli_run_id": run_id,
                "muli_session_id": session["id"],
            },
        )
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        assert record["run_id"] == run_id and record["notify"] is False
        assert not seen and not runtime.tasks
    finally:
        await runtime.close()


async def test_exact_run_attribution_query_uses_its_partial_index(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        rows = runtime.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM ingress WHERE run_id=? "
            "AND json_extract(payload,'$.principal')='system:attention' LIMIT 1",
            (new_id(),),
        ).fetchall()
        detail = "\n".join(row["detail"] for row in rows)
        assert "USING INDEX ingress_system_log_run" in detail
        assert "SCAN ingress" not in detail
    finally:
        await runtime.close()
