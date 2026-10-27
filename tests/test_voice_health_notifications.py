from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attention import attention_inputs, eventually
from test_proactive_delivery import configure, external
from test_report_origin import route
from test_runtime import make_runtime
from test_system_error_origin import source_for, start_capture
from test_system_events_runtime import inbox_for
from test_voice import FakeDetector, FakeMedia, FakeRecorder

from muli.attachments import Attachments
from muli.channel_application import ChannelApplication
from muli.providers import ModelTurn
from muli.server import create_app
from muli.store import new_id
from muli.system_event_query import read_system_events
from muli.system_events import SystemEvents, attention_source, system_log_context
from muli.voice_health import VOICE_LISTENER_FAILED, VOICE_UNAVAILABLE


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("voice health verification cannot access network or real processes")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


async def test_actual_background_failure_reaches_readonly_attention_without_private_audio(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="后台语音遇到故障，可以检查状态。")])
    runtime.config.update({"voice": {"enabled": True}})
    app = create_app(runtime.config, runtime=runtime)
    personal = app.state.personal_io
    worker = personal.voice
    inbox = inbox_for(runtime)

    class FailedRecorder(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            self.calls.append(seconds)
            raise ValueError("PRIVATE_AUDIO_PATH_AND_SERVICE_KEY")

    recorder = FailedRecorder()
    worker.recorder, worker.media = recorder, FakeMedia()
    worker.wake, worker.speaker = FakeDetector(False), FakeDetector(configured=False)
    try:
        await start_capture(runtime)
        await inbox.start()
        with system_log_context(run_id=new_id(), session_id=new_id()):
            await personal.start()
        await eventually(lambda: len(seen) == 1 and not runtime.tasks)
        await worker.stop()
        records = runtime.store.list("system_log")
        assert len(records) == 1 and recorder.calls == [worker.config.chunk_seconds]
        record = records[0]
        assert record["producer"] == "voice_listener" and record["component"] == "media"
        assert record["code"] == VOICE_LISTENER_FAILED and record["count"] == 1
        assert record["notify"] is True and record["run_id"] is record["session_id"] is None
        assert source_for(runtime, record).classification == "independent"
        assert source_for(runtime, record).origin.delivery_routes() == []
        assert worker.status()["health"]["state"] == "failed"
        assert len(attention_inputs(runtime)) == 1
        assert "PRIVATE_AUDIO" not in json.dumps([records, attention_inputs(runtime)])
        assert worker.media.audio == []
        assert list(worker.files.root.glob("*.wav")) == []
        query = read_system_events(
            runtime.store, {"code": VOICE_LISTENER_FAILED}, max_output_chars=10000
        )
        assert len(query["items"]) == 1
    finally:
        await inbox.close()
        await app.state.close()


async def test_unavailable_start_and_retired_worker_cannot_report_for_its_replacement(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"voice": {"enabled": True}})
    app = create_app(runtime.config, runtime=runtime)
    personal, worker = app.state.personal_io, app.state.personal_io.voice
    recorder = FakeRecorder()
    recorder.status = lambda: {"available": False}  # type: ignore[method-assign]
    worker.recorder, worker.media = recorder, FakeMedia()
    worker.wake, worker.speaker = FakeDetector(), FakeDetector(configured=False)
    callback = worker.on_fault
    assert callback is not None
    try:
        await start_capture(runtime)
        await personal.start()
        await personal.start()
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        assert record["code"] == VOICE_UNAVAILABLE and record["count"] == 1
        assert recorder.calls == [] and worker._task is None
        runtime.config.update({"voice": {"enabled": False}})
        await personal.reconfigure()
        assert personal.voice is not worker and personal.voice.config.enabled is False
        captured = runtime.system_events.status()["captured"]
        callback(VOICE_LISTENER_FAILED, new_id())
        assert runtime.system_events.status()["captured"] == captured
        await personal.close()
        current = personal.voice.on_fault
        assert current is not None
        current(VOICE_UNAVAILABLE, new_id())
        assert runtime.system_events.status()["captured"] == captured
    finally:
        await app.state.close()


async def test_host_failure_is_occurrence_deduplicated_frozen_and_not_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    configure(runtime.config, "7")
    try:
        await start_capture(runtime)
        occurrence = new_id()
        with system_log_context(run_id=new_id(), session_id=new_id()):
            await asyncio.create_task(
                quiet_voice_fault(runtime.system_events, VOICE_LISTENER_FAILED, occurrence)
            )
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        first = runtime.store.list("system_log")[0]
        assert first["run_id"] is first["session_id"] is None
        assert source_for(runtime, first).origin.delivery_routes() == [route("7")]
        runtime.config.update({"channels": {"proactive": {"default_destination": external("8")}}})
        with monkeypatch.context() as patch:
            patch.setattr("muli.system_events.time.time", lambda: first["first_at"] + 3601)
            runtime.system_events.voice_listener_failed(VOICE_LISTENER_FAILED, occurrence)
            await eventually(lambda: runtime.store.get("system_log", first["id"])["count"] == 2)
        repeated = runtime.store.get("system_log", first["id"])
        assert repeated is not None and repeated["count"] == 2
        assert source_for(runtime, repeated).origin.delivery_routes() == [route("7")]
        runtime.system_events.voice_listener_failed(VOICE_LISTENER_FAILED, new_id())
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)
        assert len(runtime.store.list("system_log")) == 2
    finally:
        await runtime.close()


async def test_suppression_and_plain_logger_extra_cannot_claim_voice_delivery(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    try:
        await start_capture(runtime)
        assert runtime.system_logger is not None
        runtime.system_logger.error(
            "PRIVATE",
            extra={
                "muli_component": "media",
                "muli_code": VOICE_LISTENER_FAILED,
                "producer": "voice_listener",
                "occurrence_id": new_id(),
            },
        )
        with system_log_context(notify=False):
            await asyncio.create_task(quiet_voice_fault(runtime.system_events))
        await eventually(lambda: runtime.system_events.status()["persisted"] == 2)
        records = runtime.store.list("system_log")
        claimed = next(item for item in records if item["producer"] == "voice_listener")
        local = next(item for item in records if item["producer"] == "legacy_local")
        assert claimed["notify"] is False
        assert source_for(runtime, local).classification == "legacy_local"
        event = next(item for item in runtime.store.events() if item["id"] == claimed["event_id"])
        assert (
            attention_source(runtime.store, event, (), runtime.config.settings.system_events)
            is None
        )
        assert "PRIVATE" not in json.dumps(records)
        captured = runtime.system_events.status()["captured"]
        runtime.system_events.voice_listener_failed("media_failed", new_id())
        runtime.system_events.voice_listener_failed(VOICE_UNAVAILABLE, "wrong")
        assert runtime.system_events.status()["captured"] == captured
    finally:
        await runtime.close()


async def quiet_voice_fault(
    events: SystemEvents, code: str = VOICE_UNAVAILABLE, occurrence_id: str | None = None
) -> None:
    events.voice_listener_failed(code, occurrence_id or new_id())


async def test_voice_health_obeys_existing_notification_and_capture_thresholds(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    application = ChannelApplication(
        runtime, Attachments(runtime.store, tmp_path / "uploads", runtime.config)
    )
    try:
        await start_capture(runtime)
        assert "voice_listener" in application.status()["proactive"]["independent_sources"]
        runtime.config.update({"system_events": {"notify_enabled": False}})
        runtime.system_logger = await runtime.system_events.reconfigure()
        runtime.system_events.voice_listener_failed(VOICE_UNAVAILABLE, new_id())
        await eventually(lambda: runtime.system_events.status()["persisted"] == 1)
        record = runtime.store.list("system_log")[0]
        event = next(item for item in runtime.store.events() if item["id"] == record["event_id"])
        assert (
            attention_source(runtime.store, event, (), runtime.config.settings.system_events)
            is None
        )
        runtime.config.update({"system_events": {"capture_level": "critical"}})
        runtime.system_logger = await runtime.system_events.reconfigure()
        runtime.system_events.voice_listener_failed(VOICE_UNAVAILABLE, new_id())
        assert runtime.system_events.status()["queued"] == 0
        assert runtime.system_events.status()["persisted"] == 1
    finally:
        await application.close()
        await runtime.close()
