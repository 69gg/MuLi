from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.local_services import LocalServices
from muli.media import SpeechResult

AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt "


class FakeMedia:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str]] = []

    async def __aenter__(self) -> FakeMedia:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def synthesize(
        self, text: str, voice: str | None = None, style: str = ""
    ) -> SpeechResult:
        self.calls.append((text, voice, style))
        return SpeechResult(AUDIO, "audio/wav", ".wav")


async def local_services(tmp_path: Path) -> tuple[Any, LocalServices]:
    runtime, _ = make_runtime(tmp_path, [])
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", runtime.config)
    return runtime, LocalServices(runtime, attachments)


def delivery_services() -> LocalServices:
    local = object.__new__(LocalServices)
    local.runtime = SimpleNamespace(tool_session_id="s", request_context=lambda _: None)
    local.voice_delivery = None
    local.voice_playback = None
    return local


async def test_voice_speak_is_the_single_external_voice_output_tool(tmp_path: Path) -> None:
    runtime, local = await local_services(tmp_path)
    try:
        tools = {tool.name: tool for tool in local.tools(runtime)}
        assert "voice_speak" in tools and "speech_synthesize" not in tools
        assert tools["voice_speak"].risk == "external"
    finally:
        await runtime.close()


async def test_voice_speak_passes_style_and_saves_the_attachment(tmp_path: Path) -> None:
    runtime, local = await local_services(tmp_path)
    media = FakeMedia()
    saved: list[SpeechResult] = []

    async def save(result: SpeechResult) -> dict[str, Any]:
        saved.append(result)
        return {"id": "a" * 32, "mime_type": result.mime_type}

    local.media = lambda: media  # type: ignore[method-assign]
    local.save_speech = save  # type: ignore[method-assign]
    try:
        handler = next(tool for tool in local.tools(runtime) if tool.name == "voice_speak").handler
        receipt = await handler({"text": "（开心）你好", "style": "轻快"})
        assert media.calls == [("（开心）你好", None, "轻快")]
        assert saved and saved[0].data == AUDIO
        assert receipt["delivery"]["state"] == "saved"
    finally:
        await runtime.close()


async def test_deliver_voice_sends_to_the_current_private_channel() -> None:
    local = delivery_services()
    sent: list[str] = []

    async def deliver(attachment_id: str) -> dict[str, Any]:
        sent.append(attachment_id)
        return {"state": "queued", "delivery_id": "d1"}

    local.voice_delivery = deliver
    local.runtime.request_context = lambda _: SimpleNamespace(
        reply_origin=SimpleNamespace(kind="channel", input_id="i1")
    )
    result = await local.deliver_voice({"id": "a" * 32}, "audio/wav")
    assert sent == ["a" * 32]
    assert result == {"state": "sent", "receipt": {"state": "queued", "delivery_id": "d1"}}


async def test_deliver_voice_plays_a_verified_local_voice_input() -> None:
    local = delivery_services()
    played: list[tuple[str, str]] = []

    async def play(attachment_id: str, mime_type: str) -> dict[str, Any]:
        played.append((attachment_id, mime_type))
        return {"state": "played"}

    local.voice_playback = play
    local._voice_input = lambda _: True  # type: ignore[method-assign]
    local.runtime.request_context = lambda _: SimpleNamespace(
        reply_origin=SimpleNamespace(kind="unattributed", input_id="i2")
    )
    result = await local.deliver_voice({"id": "b" * 32}, "audio/wav")
    assert played == [("b" * 32, "audio/wav")]
    assert result == {"state": "played", "receipt": {"state": "played"}}


async def test_deliver_voice_keeps_the_file_without_a_destination() -> None:
    local = delivery_services()
    result = await local.deliver_voice({"id": "c" * 32}, "audio/wav")
    assert result["state"] == "saved" and result["reason"]


async def test_deliver_voice_falls_back_to_saved_on_delivery_error() -> None:
    local = delivery_services()

    async def deliver(_: str) -> dict[str, Any]:
        raise PermissionError("没有来源")

    local.voice_delivery = deliver
    local.runtime.request_context = lambda _: SimpleNamespace(
        reply_origin=SimpleNamespace(kind="channel", input_id="i3")
    )
    result = await local.deliver_voice({"id": "d" * 32}, "audio/wav")
    assert result["state"] == "saved" and "没有来源" in result["reason"]
