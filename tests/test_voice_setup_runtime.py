from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator
from test_runtime import make_runtime
from test_voice import FakeDetector, FakeMedia, FakePlayer, FakeRecorder
from test_voice_configuration import models

from muli.providers import ModelTurn, ToolCall
from muli.server import create_app


async def test_main_can_prepare_and_enroll_through_real_tools_while_team_cannot(
    tmp_path: Path,
) -> None:
    turns = [
        ModelTurn(
            tool_calls=[ToolCall("prepare", "voice_acoustic_prepare", json.dumps(models(tmp_path)))]
        ),
        ModelTurn(tool_calls=[ToolCall("enroll", "voice_setup_enroll", '{"seconds":1}')]),
        ModelTurn(content="准备和参考录音已完成，后台仍关闭。"),
    ]
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update({"updates": {"check_on_start": False}})
    app = create_app(runtime.config, runtime=runtime)
    personal = app.state.personal_io
    worker = personal.voice
    recorder, media, player = FakeRecorder(), FakeMedia(), FakePlayer(True)
    worker.recorder, worker.media, worker.player = recorder, media, player
    worker.wake, worker.speaker = FakeDetector(), FakeDetector()
    try:
        main_id = runtime.main_session()["id"]
        ordinary = runtime.new_session()
        assert "voice_setup_enroll" in runtime.session_tools(main_id)
        assert "voice_setup_enroll" not in runtime.session_tools(ordinary["id"])
        prepare = runtime.session_tools(main_id)["voice_acoustic_prepare"]
        Draft202012Validator.check_schema(prepare.parameters)
        Draft202012Validator(prepare.parameters).validate(models(tmp_path))
        token = runtime._tool_session.set(ordinary["id"])
        try:
            with pytest.raises(PermissionError):
                await prepare.handler(models(tmp_path))
        finally:
            runtime._tool_session.reset(token)
        result = await runtime.submit("请准备已有模型并录制一秒参考，保持后台关闭。", mode="yolo")
        await runtime.wait(result["run_id"])
        messages = runtime.store.messages(main_id)
        prepared = json.loads(
            next(item["content"] for item in messages if item.get("tool_call_id") == "prepare")
        )
        enrolled = json.loads(
            next(item["content"] for item in messages if item.get("tool_call_id") == "enroll")
        )
        assert "prepared" in prepared, prepared
        assert prepared["prepared"] is True and prepared["applied"] is False
        assert enrolled["enrolled"] is True and enrolled["state"] == "completed"
        assert runtime.config.settings.voice.enabled is False
        assert runtime.config.settings.voice.wake_command == []
        assert recorder.calls == [1]
        assert media.audio == [] and player.played == []
        assert runtime.store.get("run", result["run_id"])["status"] == "completed"
        assert len(seen) == 3
    finally:
        await app.state.close()


async def test_http_requires_confirmation_and_preserves_local_only_setup(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}})
    app = create_app(runtime.config, runtime=runtime)
    worker = app.state.personal_io.voice
    recorder, media = FakeRecorder(), FakeMedia()
    worker.recorder, worker.media = recorder, media
    worker.wake, worker.speaker = FakeDetector(), FakeDetector(False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as web:
            for confirmed, expected in [(False, 400), (1, 422)]:
                response = await web.post(
                    "/api/voice/setup",
                    json={"operation": "enroll", "confirmed": confirmed, "seconds": 1},
                )
                assert response.status_code == expected
            assert recorder.calls == []
            runtime.config.update({"disabled_tools": ["voice_setup_enroll"]})
            blocked = await web.post(
                "/api/voice/setup", json={"operation": "enroll", "confirmed": True}
            )
            assert blocked.status_code == 403 and recorder.calls == []
            runtime.config.update({"disabled_tools": []})
            foreign = await web.post(
                "/api/voice/setup",
                json={"operation": "enroll", "confirmed": True},
                headers={"Origin": "https://foreign.invalid"},
            )
            assert foreign.status_code == 403 and recorder.calls == []
            enrolled = await web.post(
                "/api/voice/setup", json={"operation": "enroll", "confirmed": True, "seconds": 1}
            )
            assert enrolled.status_code == 200 and enrolled.json()["enrolled"] is True
            verified = await web.post(
                "/api/voice/setup", json={"operation": "verify", "confirmed": True, "seconds": 1}
            )
            assert verified.status_code == 200
            assert verified.json()["wake_detected"] is True
            assert verified.json()["speaker_verified"] is False
            assert "path" not in verified.json()
            status = (await web.get("/api/voice")).json()
            assert status["setup"]["state"] == "completed" and status["enabled"] is False
            assert "setup_next_step" in status
            assert media.audio == [] and recorder.calls == [1, 1]
            assert runtime.tasks == {}
            assert runtime.store.messages(runtime.main_session()["id"]) == []
    finally:
        await app.state.close()


async def test_http_cancel_settles_recording_without_stopping_other_work(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}})
    app = create_app(runtime.config, runtime=runtime)
    worker = app.state.personal_io.voice
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Blocked(FakeRecorder):
        async def record(self, path: Path, seconds: float) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker.recorder, worker.wake, worker.speaker = Blocked(), FakeDetector(), FakeDetector()
    identifier = str(uuid4())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as web:
            recording = asyncio.create_task(
                web.post(
                    "/api/voice/setup",
                    json={"id": identifier, "operation": "enroll", "confirmed": True, "seconds": 1},
                )
            )
            async with asyncio.timeout(2):
                await entered.wait()
                rejected = await web.post("/api/voice/setup/cancel")
                assert rejected.status_code == 415 and not cancelled.is_set()
                incomplete = await web.post("/api/voice/setup/cancel", json={})
                assert incomplete.status_code == 422 and not cancelled.is_set()
                stopped = await web.post("/api/voice/setup/cancel", json={"id": identifier})
                response = await recording
            assert stopped.status_code == 200 and stopped.json()["state"] == "cancelled"
            assert response.status_code == 200 and response.json()["state"] == "cancelled"
            assert response.json()["id"] == stopped.json()["id"] == identifier
            assert cancelled.is_set()
            assert list(worker.files.root.glob("*.wav")) == []
            assert worker.status()["closed"] is False
            assert runtime._closing is False
            entered.clear()
            cancelled.clear()
            successor_id = str(uuid4())
            successor = asyncio.create_task(
                web.post(
                    "/api/voice/setup",
                    json={
                        "id": successor_id,
                        "operation": "enroll",
                        "confirmed": True,
                        "seconds": 1,
                    },
                )
            )
            async with asyncio.timeout(2):
                await entered.wait()
                late = await web.post("/api/voice/setup/cancel", json={"id": identifier})
                assert late.status_code == 409 and not cancelled.is_set() and not successor.done()
                actual = await web.post("/api/voice/setup/cancel", json={"id": successor_id})
                successor_response = await successor
            assert actual.json()["state"] == "cancelled"
            assert successor_response.json()["id"] == successor_id
    finally:
        await app.state.close()


@pytest.mark.parametrize("operation", ["prepare", "enroll"])
async def test_new_owner_input_revokes_waiting_setup_before_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    name = "voice_acoustic_prepare" if operation == "prepare" else "voice_setup_enroll"
    arguments = models(tmp_path) if operation == "prepare" else {"seconds": 1}
    turns = [
        ModelTurn(tool_calls=[ToolCall("setup", name, json.dumps(arguments))]),
        ModelTurn(content="已按新要求停止保存。"),
        ModelTurn(content="继续处理新要求。"),
    ]
    runtime, _ = make_runtime(tmp_path, turns)
    runtime.config.update({"updates": {"check_on_start": False}})
    app = create_app(runtime.config, runtime=runtime)
    personal, entered, release = app.state.personal_io, asyncio.Event(), asyncio.Event()
    worker = personal.voice
    worker.wake, worker.speaker = FakeDetector(), FakeDetector()

    if operation == "prepare":
        original = personal.prepare_voice_acoustic

        async def wait_before_lock(settings: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            entered.set()
            await release.wait()
            return await original(settings, **kwargs)

        monkeypatch.setattr(personal, "prepare_voice_acoustic", wait_before_lock)
    else:

        class HeldRecorder(FakeRecorder):
            async def record(self, path: Path, seconds: float) -> None:
                await super().record(path, seconds)
                entered.set()
                await release.wait()

        worker.recorder = HeldRecorder()
    try:
        first = await runtime.submit("请准备并保存语音设置。", mode="yolo")
        async with asyncio.timeout(3):
            await entered.wait()
            await runtime.submit("新要求：停止保存配置和参考录音。", mode="yolo")
            release.set()
            await runtime.wait(first["run_id"])
        assert list(worker.files.root.glob("acoustic-*.json")) == []
        assert list(worker.files.root.glob("*.wav")) == []
        assert not (worker.files.root / "enrollment.txt").exists()
        messages = runtime.store.messages(runtime.main_session()["id"])
        receipt = json.loads(
            next(item["content"] for item in messages if item.get("tool_call_id") == "setup")
        )
        assert "新输入" in receipt["error"]
    finally:
        release.set()
        await app.state.close()
