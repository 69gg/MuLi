"""Small fake ASR responses exercise the real private artifact and tool boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import pytest
from test_runtime import make_runtime

from muli.attachments import Attachments
from muli.evidence import EvidenceError
from muli.local_services import LocalServices
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.providers import Message, ModelTurn, ProviderError, ToolCall
from muli.runtime import Runtime
from muli.store import Store
from muli.transcripts import AudioTranscripts

_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt "


@dataclass
class Host:
    runtime: Runtime
    attachments: Attachments
    services: LocalServices
    audio_id: str
    turns: list[ModelTurn]
    seen: list[list[Message]]
    text: str = "短转写🙂"
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    async def transcribe(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.runtime.tools["audio_transcribe"].handler(
                {"id": self.audio_id, "language": "zh"}
            ),
        )

    async def read(self, identifier: str, **arguments: Any) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.runtime.tools["audio_transcript_read"].handler(
                {"id": identifier, **arguments}
            ),
        )


@pytest.fixture
async def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Host]:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("ASR tests require MockTransport")

    async def no_process(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("ASR artifact tests must not launch subprocesses")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_process)
    turns: list[ModelTurn] = []
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    attachments = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    services = LocalServices(runtime, attachments)

    async def chunks() -> AsyncIterator[bytes]:
        yield _AUDIO

    audio = await attachments.save(chunks(), "original.wav", source="web")
    fixture = Host(runtime, attachments, services, audio["id"], turns, seen)

    def handler(request: httpx.Request) -> httpx.Response:
        fixture.requests.append(request)
        return httpx.Response(fixture.status, json={"text": fixture.text})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(
                services,
                "media",
                lambda: MediaService(
                    MediaConfig(
                        asr=ServiceConfig(
                            model="offline-asr",
                            base_url="https://asr.invalid/v1",
                            response_format="json",
                        )
                    ),
                    client=client,
                ),
            )
            yield fixture
    finally:
        await runtime.close()


@pytest.mark.parametrize("text", ["", "短转写🙂"])
async def test_original_short_text_is_compatible_and_privately_archived(
    host: Host, text: str
) -> None:
    host.text = text
    result = await host.transcribe()
    assert result["id"] == host.audio_id and result["text"] == text
    assert result["asr_completed"] is True and not result["has_more"]
    assert result["field"] == "text" and not result["truncated"]
    artifact = host.attachments.get(result["transcript_id"])
    path = host.attachments.path(artifact["id"])
    assert path.read_bytes() == text.encode("utf-8")
    assert artifact["sha256"] == result["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact["source"] == "asr"
    assert artifact["name"] == f"转写-{host.audio_id}.txt"
    assert result["url"] == f"/api/uploads/{artifact['id']}/download"
    assert str(path) not in json.dumps(result, ensure_ascii=False)
    assert len(host.requests) == 1 and host.attachments.path(host.audio_id).read_bytes() == _AUDIO
    assert host.runtime.tools["audio_transcribe"].risk == "read"
    assert host.runtime.tools["audio_transcript_read"].risk == "read"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_long_escaped_utf8_result_reassembles_from_bounded_pages_once(host: Host) -> None:
    host.text = ('证据🙂"\\\n\0' * 250) + "末尾不可丢"
    result = await host.transcribe()
    assert result["has_more"] and result["truncated"]
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000
    assert result["total_bytes"] == len(host.text.encode("utf-8"))
    texts = [result["text"]]
    offset = result["next_offset_bytes"]
    for _ in range(100):
        page = await host.read(
            result["transcript_id"], revision=result["revision"], offset_bytes=offset
        )
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        assert page["offset_bytes"] == offset and page["revision"] == result["revision"]
        texts.append(page["content"])
        assert page["next_offset_bytes"] > offset
        offset = page["next_offset_bytes"]
        if not page["has_more"]:
            break
    else:
        pytest.fail("bounded pages made no finite progress")
    assert "".join(texts) == host.text and offset == result["total_bytes"]
    assert len(host.requests) == 1
    eof = await host.read(result["transcript_id"], revision=result["revision"], offset_bytes=offset)
    assert eof["content"] == "" and not eof["has_more"]


async def test_reopen_reads_same_artifact_without_another_asr(host: Host) -> None:
    host.text = "完整结果🙂" * 150
    result = await host.transcribe()
    await host.runtime.close()
    reopened = Store.open_runtime(
        host.runtime.data_dir / "muli.sqlite3",
        default_mode=host.runtime.config.settings.default_mode,
    )
    try:
        attachments = Attachments(reopened, host.attachments.directory, host.runtime.config)
        found = next(item for item in attachments.list() if item["source"] == "asr")
        assert found["id"] == result["transcript_id"]
        page = await AudioTranscripts(attachments).read(found["id"], revision=result["revision"])
        assert host.text.startswith(page["content"])
        assert page["revision"] == result["revision"] and len(host.requests) == 1
    finally:
        reopened.close()


async def test_real_runtime_preserves_reference_in_store_and_next_request(host: Host) -> None:
    host.text = ('长结果"🙂\n' * 250) + "完整文件结尾"
    host.turns.extend(
        [
            ModelTurn(
                tool_calls=[ToolCall("asr", "audio_transcribe", json.dumps({"id": host.audio_id}))]
            ),
            ModelTurn(content="转写已留存，可继续读取。"),
        ]
    )
    receipt = await host.runtime.submit("转写这份附件", mode="yolo")
    await asyncio.wait_for(host.runtime.wait(receipt["run_id"]), 3)
    records = host.runtime.store.messages(receipt["session_id"])
    stored = next(item for item in records if item["role"] == "tool")
    result = json.loads(stored["content"])
    assert result["transcript_id"] and result["revision"] and result["has_more"]
    assert len(stored["content"]) <= 1000 and len(host.requests) == 1
    replay = next(message for message in host.seen[-1] if message.tool_call_id == "asr")
    assert replay.content == stored["content"] and replay.preserve_content
    assert host.attachments.path(result["transcript_id"]).read_text(encoding="utf-8") == host.text


@pytest.mark.parametrize("kind", ["file", "quota", "response"])
async def test_post_asr_limit_failure_is_explicit_and_never_repeats_asr(
    host: Host, kind: str
) -> None:
    host.text = "x" * 1100
    patch: dict[str, Any]
    if kind == "response":
        patch = {"media": {"asr": {"max_response_bytes": 1024}}}
    else:
        patch = {"uploads": {"max_file_bytes" if kind == "file" else "max_total_bytes": 1024}}
    host.runtime.config.update(patch)
    result = await host.transcribe()
    assert result["asr_completed"] is True and result["code"] == "transcript_save_failed"
    assert "不要自动重复转写" in result["error"] and len(host.requests) == 1
    assert [item["id"] for item in host.attachments.list()] == [host.audio_id]
    assert not list(host.attachments.directory.glob(".upload-*"))
    assert host.attachments.reserved == 0


async def test_failed_preview_keeps_saved_reference_and_never_repeats_asr(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_page(*args: Any, **kwargs: Any) -> NoReturn:
        raise EvidenceError("output_budget", "fake budget failure")

    monkeypatch.setattr(host.services.transcripts, "_page", fail_page)
    result = await host.transcribe()
    assert result["asr_completed"] is True and result["code"] == "output_budget"
    assert len(host.requests) == 1
    assert host.attachments.path(result["transcript_id"]).read_text(encoding="utf-8") == host.text
    assert result["revision"] == host.attachments.get(result["transcript_id"])["sha256"]


async def test_limit_shrinking_during_save_keeps_reference_but_does_not_claim_readable(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.text = "x" * 1100
    original = host.attachments.save

    async def shrink(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(*args, **kwargs)
        host.runtime.config.update({"media": {"asr": {"max_response_bytes": 1024}}})
        return result

    monkeypatch.setattr(host.attachments, "save", shrink)
    result = await host.transcribe()
    assert result["asr_completed"] is True and result["code"] == "transcript_limit_changed"
    assert "text" not in result and len(host.requests) == 1
    assert host.attachments.path(result["transcript_id"]).read_text(encoding="utf-8") == host.text
    with pytest.raises(EvidenceError, match="有界转写工件"):
        await host.read(result["transcript_id"], revision=result["revision"])


async def test_upstream_failure_does_not_claim_completed_or_create_an_artifact(host: Host) -> None:
    host.status = 503
    with pytest.raises(ProviderError):
        await host.transcribe()
    assert len(host.requests) == 1
    assert [item["id"] for item in host.attachments.list()] == [host.audio_id]


async def test_saved_artifact_survives_cancelled_response_handoff(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = asyncio.Event()
    original = host.attachments.save

    async def after_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(*args, **kwargs)
        committed.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(host.attachments, "save", after_save)
    task = asyncio.create_task(host.transcribe())
    try:
        await asyncio.wait_for(committed.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    artifact = next(item for item in host.attachments.list() if item["source"] == "asr")
    page = await host.read(artifact["id"], revision=artifact["sha256"])
    assert page["content"] == host.text and len(host.requests) == 1


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"offset_bytes": -1}, "invalid_arguments"),
        ({"offset_bytes": True}, "invalid_arguments"),
        ({"offset_bytes": 1}, "invalid_arguments"),
        ({"offset_bytes": 1, "revision": "0" * 64}, "revision_changed"),
        ({"offset_bytes": 1000}, "invalid_arguments"),
        ({"revision": "bad"}, "invalid_arguments"),
    ],
)
async def test_reader_rejects_invalid_coordinates(
    host: Host, arguments: dict[str, Any], code: str
) -> None:
    result = await host.transcribe()
    with pytest.raises(EvidenceError) as caught:
        await host.read(result["transcript_id"], **arguments)
    assert caught.value.code == code
    assert len(host.requests) == 1


async def test_reader_rejects_mid_character_and_beyond_end_and_non_asr(host: Host) -> None:
    result = await host.transcribe()
    for offset in (1, result["total_bytes"] + 1):
        with pytest.raises(EvidenceError) as caught:
            await host.read(
                result["transcript_id"], revision=result["revision"], offset_bytes=offset
            )
        assert caught.value.code == "invalid_offset"
    with pytest.raises(EvidenceError, match="转写工件"):
        await host.read(host.audio_id)


async def test_reader_uses_hot_output_budget_and_rejects_hot_byte_limit(host: Host) -> None:
    host.runtime.config.update({"limits": {"max_output_chars": 2000}})
    host.text = ('资料"🙂\n' * 250) + "末尾"
    result = await host.transcribe()
    host.runtime.config.update({"limits": {"max_output_chars": 1000}})
    page = await host.read(result["transcript_id"], revision=result["revision"])
    assert len(json.dumps(page, ensure_ascii=False)) <= 1000 and page["has_more"]
    host.runtime.config.update({"uploads": {"max_file_bytes": 1024}})
    with pytest.raises(EvidenceError, match="有界转写工件"):
        await host.read(result["transcript_id"], revision=result["revision"])
    assert len(host.requests) == 1


async def test_reader_rejects_same_size_hash_drift(host: Host) -> None:
    result = await host.transcribe()
    path = host.attachments.path(result["transcript_id"])
    path.write_bytes(b"x" * path.stat().st_size)
    with pytest.raises(EvidenceError) as caught:
        await host.read(result["transcript_id"], revision=result["revision"])
    assert caught.value.code == "transcript_changed"


async def test_reader_rejects_metadata_change_during_owned_file_read(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.transcripts as module

    result = await host.transcribe()
    original = module.file_io

    async def changed(operation: Callable[[], bytes]) -> bytes:
        raw = await original(operation)
        item = host.attachments.get(result["transcript_id"])
        item["sha256"] = "0" * 64
        host.runtime.store.put("attachment", item)
        return raw

    monkeypatch.setattr(module, "file_io", changed)
    with pytest.raises(EvidenceError, match="元数据"):
        await host.read(result["transcript_id"], revision=result["revision"])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO replacement is POSIX-only")
async def test_fifo_replacement_before_open_is_nonblocking_and_rejected(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await host.transcribe()
    path = host.attachments.path(result["transcript_id"])
    original_open = os.open

    def replace_open(candidate: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if candidate == path:
            assert flags & os.O_NONBLOCK
            path.unlink()
            os.mkfifo(path)
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_open)
    with pytest.raises(EvidenceError, match="变化"):
        await asyncio.wait_for(host.read(result["transcript_id"]), 1)


async def test_repeated_cancel_settles_real_file_thread_and_cleans_partial(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.attachments as module

    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = module.file_io

    async def blocked(operation: Callable[[], Any]) -> Any:
        def write() -> Any:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise AssertionError("file thread was not released")
            return operation()

        return await original(write)

    monkeypatch.setattr(module, "file_io", blocked)
    task = asyncio.create_task(host.transcribe())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert len(host.requests) == 1 and host.attachments.reserved == 0
    assert [item["id"] for item in host.attachments.list()] == [host.audio_id]
    assert not list(host.attachments.directory.glob(".upload-*"))
