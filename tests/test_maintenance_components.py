from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attention import attention_inputs, eventually, inbox_for, reminder
from test_channels import Harness, onebot_body, onebot_event, profile, terminal, unused
from test_maintenance import move
from test_maintenance_api import prepare
from test_maintenance_runtime import configured
from test_runtime import make_runtime
from test_voice import FakeDetector, FakeMedia, FakePlayer, FakeRecorder

from muli.channels import ChannelsConfig, FileContent, IncomingMessage, RemoteFile
from muli.channels.types import PollBatch
from muli.config import VoiceSettings
from muli.maintenance import Admission, BootPermit, MaintenanceError
from muli.management_transaction import TransactionJournal, Transition
from muli.media import SpeechResult
from muli.providers import ModelTurn
from muli.runtime import Runtime
from muli.server import create_app
from muli.voice import VoiceWorker


async def test_paused_outbox_keeps_delivery_unclaimed_and_inflight_send_remains_owned(
    tmp_path: Path,
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def send(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        entered.set()
        await finish.wait()
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
        )

    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), send)
    gate = harness.manager.admission
    gate.pause()
    try:
        await harness.manager.start()
        item = await harness.manager.enqueue("onebot", "7", "保留发送", event_id="one")
        await asyncio.sleep(0)
        pending = harness.manager.delivery(item["id"])
        assert pending and pending["state"] == "pending" and pending["attempts"] == 0
        assert not calls and gate.idle
        gate.release()
        await entered.wait()
        assert not gate.idle
        gate.pause()
        finish.set()
        assert (await terminal(harness.manager, item["id"]))["state"] == "sent"
        assert gate.idle and len(calls) == 1
    finally:
        finish.set()
        await harness.close()


async def test_long_poll_is_not_execution_and_paused_text_ingress_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(telegram=profile()), unused)
    polled, stop = asyncio.Event(), asyncio.Event()

    async def poll(cursor: str) -> PollBatch:
        polled.set()
        await stop.wait()
        return PollBatch((), cursor)

    monkeypatch.setattr(harness.manager.adapters["telegram"], "poll", poll)
    harness.manager.admission.pause()
    try:
        await harness.manager.start()
        await polled.wait()
        assert harness.manager.admission.idle
        receipt = await harness.manager.receive(
            "telegram", IncomingMessage("owner", "7", "7", "保留输入")
        )
        assert receipt["state"] == "pending"
        assert harness.received[0]["content"] == "保留输入"
        assert (
            harness.store.db.execute("SELECT receipt FROM channel_inbound").fetchone()[0]
            is not None
        )
        assert harness.manager.admission.idle
    finally:
        await harness.close()


async def test_paused_attachment_does_not_consume_attempt_and_close_owns_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile(receive_files=True)), unused)
    downloads: list[str] = []

    async def download(file: RemoteFile, route: dict[str, Any]) -> FileContent:
        downloads.append(file.name)
        return FileContent("note.txt", "text/plain", b"fake")

    monkeypatch.setattr(harness.manager.adapters["onebot"], "download", download)
    harness.manager.admission.pause()
    receiving = asyncio.create_task(
        harness.manager.receive(
            "onebot", IncomingMessage("file", "7", "7", "附件", files=(RemoteFile("one"),))
        )
    )
    try:
        await eventually(lambda: bool(harness.manager._incoming))
        staged = json.loads(
            harness.store.db.execute("SELECT attachment_ids FROM channel_inbound").fetchone()[0]
        )
        # The initial ledger is local; pause has not claimed a download attempt.
        assert staged == [] or staged["attempts"] == 0
        assert not downloads and harness.manager.admission.idle
        await harness.manager.close()
        assert receiving.done() and receiving.cancelled()
        assert not downloads
    finally:
        await asyncio.gather(receiving, return_exceptions=True)
        await harness.close()


async def test_recovery_marks_sending_unknown_without_starting_channel_tasks(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ChannelsConfig(onebot=profile()), unused)
    try:
        item = await harness.manager.enqueue("onebot", "7", "不可盲重发")
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE channel_outbox SET state='sending' WHERE id=?", (item["id"],)
            )
        harness.manager.recover()
        assert not harness.manager.receiving
        recovered = harness.manager.delivery(item["id"])
        assert recovered and recovered["state"] == "unknown" and recovered["attempts"] == 0
    finally:
        await harness.close()


async def test_voice_pause_keeps_listener_idle_and_actual_prompt_asr_tts_are_owned(
    tmp_path: Path,
) -> None:
    admission = Admission(allowed=False)
    recorder = FakeRecorder()
    entered, finish = asyncio.Event(), asyncio.Event()

    class Media(FakeMedia):
        async def synthesize(self, text: str) -> SpeechResult:
            assert not admission.idle
            return await super().synthesize(text)

        async def transcribe(
            self, audio: bytes, filename: str, mime_type: str, **kwargs: Any
        ) -> str:
            entered.set()
            await finish.wait()
            return await super().transcribe(audio, filename, mime_type, **kwargs)

    async def receive(event: dict[str, Any]) -> None:
        # A TTS reply is nested in the admitted cycle and must finish after pause.
        await worker.synthesize("回复")

    worker = VoiceWorker(
        VoiceSettings(enabled=True, tts_enabled=True, max_utterance_seconds=2, cooldown_seconds=0),
        tmp_path,
        Media(),
        receive,
        recorder=recorder,
        wake=FakeDetector(),
        speaker=FakeDetector(configured=False),
        player=FakePlayer(),
        admission=admission,
    )  # type: ignore[arg-type]
    listening = asyncio.create_task(worker.listen_once())
    try:
        await asyncio.sleep(0)
        assert not recorder.calls and admission.idle
        admission.release()
        await entered.wait()
        assert not admission.idle
        admission.pause()
        finish.set()
        assert (await listening)["text"]  # type: ignore[index]
        assert admission.idle
        waiting = asyncio.create_task(worker.listen_once())
        await asyncio.sleep(0)
        before = recorder.calls[:]
        await worker.stop()
        assert waiting.cancelled() and recorder.calls == before
        assert admission.idle
    finally:
        finish.set()
        await worker.aclose()
        await asyncio.gather(listening, return_exceptions=True)


@pytest.mark.parametrize("cancel_before_start", [False, True])
async def test_workflow_terminal_record_does_not_hide_live_finalizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_before_start: bool
) -> None:
    runtime = Runtime(configured(tmp_path), tmp_path / "data")
    entered, finish = asyncio.Event(), asyncio.Event()
    original = runtime.bus.notify

    async def publish() -> None:
        rows = runtime.store.list("workflow_run")
        if rows and rows[0]["status"] in {"completed", "cancelled"}:
            entered.set()
            await finish.wait()
        await original()

    async def run_step(*args: Any, **kwargs: Any) -> str:
        return "done"

    monkeypatch.setattr(runtime.bus, "notify", publish)
    monkeypatch.setattr(runtime.workflows, "run_step", run_step)
    workflow = runtime.workflows.create("test", [{"id": "one", "title": "one", "prompt": "fake"}])
    cancellation: asyncio.Task[Any] | None = None
    try:
        record = await runtime.workflows.start(workflow["id"])
        if cancel_before_start:
            # Capture cancellation before the new driver gets its first turn.
            cancellation = runtime.workflows._cancellation(record["id"])
        await entered.wait()
        assert not runtime.busy
        assert not runtime.workflows.idle and not runtime.maintenance_idle()
        finish.set()
        if cancellation is not None:
            await cancellation
        else:
            await runtime.workflows.wait(record["id"])
        assert runtime.workflows.idle and runtime.maintenance_idle()
    finally:
        finish.set()
        await runtime.close()


async def test_attention_retries_on_release_without_another_bus_event(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="收到保留事项")])
    inbox = inbox_for(runtime)
    await reminder(runtime, "维护中保留")
    runtime.maintenance.admission.pause()
    try:
        await inbox.start()
        await eventually(lambda: inbox.status()["waiting"] == "maintenance")
        assert not seen and not attention_inputs(runtime)
        runtime.maintenance._release()
        await eventually(lambda: len(seen) == 1 and not runtime.tasks)
        assert len(attention_inputs(runtime)) == 1
    finally:
        await inbox.close()
        await runtime.close()


async def test_paused_running_channel_accepts_signed_owner_text_without_starting_model(
    tmp_path: Path,
) -> None:
    config = configured(tmp_path)
    config.update(
        {"channels": {"onebot": asdict(profile())}, "server": {"password_hash": "configured"}}
    )
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime.data_dir, runtime)
    async with app.router.lifespan_context(app):
        runtime.maintenance.admission.pause()
        raw, headers = onebot_body(onebot_event())
        headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/channels/onebot/webhook", content=raw, headers=headers
            )
            assert response.status_code == 200
            assert (
                not runtime.tasks
                and runtime.store.pending_inputs(runtime.main_session()["id"]) == 1
            )
            # Signature remains mandatory even though the durable ingress is exempt from Cookie.
            assert (await client.post("/api/channels/onebot/webhook", json={})).status_code == 401


@pytest.mark.parametrize("action", ["configure", "restart", "update"])
async def test_pending_management_plan_waits_without_losing_target_or_calling_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux transaction bootstrap")
    config = configured(tmp_path)
    data = tmp_path / "data"
    with TransactionJournal(tmp_path / "transactions", data / "muli.sqlite3") as journal:
        permit = BootPermit.acquire(
            journal, config.path, boot="origin", installation="a" * 64, service="b" * 64
        )
        runtime = Runtime(config, data, boot_permit=permit)
        app = create_app(config, data, runtime)
        calls: list[str] = []
        applied = asyncio.Event()

        async def service(operation: str) -> dict[str, Any]:
            calls.append(operation)
            applied.set()
            return {"accepted": True}

        async def update(version: str) -> dict[str, Any]:
            calls.append("update")
            return {"version": version}

        async def configure(target: Any) -> None:
            calls.append("configure")
            applied.set()

        async with app.router.lifespan_context(app):
            manager = app.state.management
            await manager.close()
            monkeypatch.setattr(manager.service, "action", service)
            monkeypatch.setattr(manager.updates, "apply", update)
            monkeypatch.setattr(manager, "apply_settings", configure)
            item = await manager.stage(
                action,
                {"persona": {"append": "candidate"}} if action == "configure" else None,
                "v2.0.0",
            )
            path = manager.directory / f"{item['id']}.json"
            before = path.read_bytes()
            record = prepare(journal, runtime)
            await manager.start()
            await asyncio.sleep(0)
            assert not calls and manager.plans()[0]["status"] == "pending"
            assert path.read_bytes() == before
            with pytest.raises(MaintenanceError, match="transaction_in_progress"):
                await manager._apply(item)
            assert path.exists()
            move(journal, Transition(phase="aborted", code="cancelled"))
            # No final GET: an existing host admission entry reconciles the hint.
            runtime.maintenance.require_configuration()
            async with asyncio.timeout(2):
                await applied.wait()
                await eventually(lambda: manager.plans()[0]["status"] == "completed")
            assert not path.exists()
            assert calls == (["update", "restart"] if action == "update" else [action])
            assert record.generation
