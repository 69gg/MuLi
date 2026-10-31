"""Ordinary text pages use the real attachment and Runtime tool paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, eventually, tool_result
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.attachments import Attachments
from muli.evidence import EvidenceError
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent


async def test_ordinary_text_pages_reach_the_original_tail_through_runtime(host: Host) -> None:
    text = ('资料🙂"\\\n' * 140) + "\n末尾唯一条款：保留原文，不把附件内容当作主人授权。"
    raw = text.encode("utf-8")
    item = await host.save(raw, "跨渠道交接材料.md")
    metadata = host.attachments.get(item["id"])
    budget = host.runtime.config.settings.limits.max_output_chars
    preview = await host.drive("attachment_read", id=item["id"])
    assert set(preview) == {"id", "content", "truncated"}
    assert preview["truncated"] is True and text.startswith(preview["content"])
    assert "末尾唯一条款" not in preview["content"]

    page = await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    assert page.get("revision") == item["sha256"], "ordinary text needs a versioned first page"
    pages: list[str] = []
    offset = 0
    for _ in range(16):
        assert len(json.dumps(page, ensure_ascii=False)) <= budget
        assert page["id"] == item["id"] and page["revision"] == item["sha256"]
        assert page["offset_bytes"] == offset and page["total_bytes"] == len(raw)
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        pages.append(page["content"])
        assert page["next_offset_bytes"] > offset
        offset = page["next_offset_bytes"]
        if not page["has_more"]:
            break
        page = await host.drive(
            "attachment_read", id=item["id"], revision=page["revision"], offset_bytes=offset
        )
    else:
        pytest.fail("finite ordinary text did not reach EOF in bounded pages")

    assert "".join(pages).encode("utf-8") == raw and offset == len(raw)
    assert hashlib.sha256("".join(pages).encode("utf-8")).hexdigest() == item["sha256"]
    eof = await host.drive(
        "attachment_read", id=item["id"], revision=page["revision"], offset_bytes=offset
    )
    assert eof["content"] == "" and not eof["has_more"]
    assert eof["next_offset_bytes"] == len(raw)
    assert host.attachments.get(item["id"]) == metadata
    assert host.attachments.path(item["id"]).read_bytes() == raw
    assert host.requests == []


async def test_teammate_reads_complete_text_pages_through_its_own_runtime_tools(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.local_services as module

    runtime = host.runtime
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "models": {
                "worker": {"model": "attachment-worker", "base_url": "https://unused.invalid/v1"}
            },
        }
    )
    await runtime.reconfigure()
    text = "队友全文头部唯一标记\n" + ('中文交接证据🙂"\\\n' * 160) + "\n队友全文尾部唯一条款"
    raw = text.encode("utf-8")
    item = await host.save(raw, "队友长文资料.md")
    metadata = host.attachments.get(item["id"])
    budget = runtime.config.settings.limits.max_output_chars
    main_turns = [
        ModelTurn(
            tool_calls=[
                call(
                    "assign-text",
                    "spawn_agent",
                    task_name="长文核对队友",
                    prompt="逐页读完交接资料，再报告已完成核对。",
                    attachment_ids=[item["id"]],
                )
            ]
        ),
        ModelTurn(
            tool_calls=[call("wait-text", "wait_agents", targets=["长文核对队友"], timeout=10)]
        ),
        ModelTurn(content="队友阅读结果已收取。"),
    ]
    pages: list[dict[str, Any]] = []
    page_ids: list[str] = []
    handler_sessions: list[str | None] = []
    original = module.read_attachment_text

    async def observed_read(
        attachments: Attachments,
        identifier: str,
        *,
        revision: str | None,
        offset_bytes: int,
    ) -> dict[str, Any]:
        handler_sessions.append(runtime.tool_session_id)
        return await original(attachments, identifier, revision=revision, offset_bytes=offset_bytes)

    monkeypatch.setattr(module, "read_attachment_text", observed_read)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            agent = runtime.team.get("长文核对队友")
            session = runtime.store.get("session", agent["id"])
            assert session["kind"] == "team" and session["role"] == "worker"
            assert agent["parent_id"] == runtime.main_session()["id"]
            schema = next(
                tool["function"] for tool in tools if tool["function"]["name"] == "attachment_read"
            )
            assert {"revision", "offset_bytes"} <= schema["parameters"]["properties"].keys()
            assert len(page_ids) < 12
            arguments: dict[str, Any] = {"id": item["id"], "offset_bytes": 0}
            if not page_ids:
                assert item["id"] in str(messages) and "队友全文头部唯一标记" not in str(messages)
            else:
                identifier = page_ids[-1]
                page = tool_result(messages, identifier)
                replay = next(message for message in messages if message.tool_call_id == identifier)
                stored = next(
                    message
                    for message in runtime.store.messages(agent["id"])
                    if message.get("tool_call_id") == identifier and message["role"] == "tool"
                )
                assert stored["content_provenance"] == "accepted" and replay.preserve_content
                assert replay.content == stored["content"] and len(replay.content) <= budget
                assert page["revision"] == item["sha256"] and page["total_bytes"] == len(raw)
                offset = pages[-1]["next_offset_bytes"] if pages else 0
                assert page["offset_bytes"] == offset and page["next_offset_bytes"] > offset
                assert page["length_bytes"] == len(page["content"].encode("utf-8"))
                pages.append(page)
                if not page["has_more"]:
                    yield StreamEvent("done", turn=ModelTurn(content="队友已逐页读完长文。"))
                    return
                arguments.update(revision=page["revision"], offset_bytes=page["next_offset_bytes"])
            identifier = f"teammate-text-page-{len(page_ids) + 1}"
            page_ids.append(identifier)
            yield StreamEvent(
                "done",
                turn=ModelTurn(tool_calls=[call(identifier, "attachment_read", **arguments)]),
            )

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            # A delivered report may be claimed in this run or the next one.
            if not self.turns:
                self.turns.append(ModelTurn(content="队友阅读结果已收取。"))
            async for event in super().stream(messages, tools):
                yield event

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "attachment-worker":
            return Worker([], [])
        return Main(main_turns, host.seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    submitted = await runtime.submit(
        "请让长文核对队友阅读这份资料。", attachment_ids=[item["id"]], mode="yolo"
    )
    await asyncio.wait_for(runtime.wait(submitted["run_id"]), 4)
    agent = runtime.team.get("长文核对队友")
    await eventually(
        runtime,
        lambda: (
            runtime.team.get(agent["id"])["status"]
            in {"completed", "failed", "cancelled", "interrupted"}
        ),
    )
    finished = runtime.team.get(agent["id"])
    assert finished["status"] == "completed" and finished["result"] == "队友已逐页读完长文。"
    assert agent["id"] != submitted["session_id"]
    assert runtime.store.get("run", finished["run_id"])["session_id"] == agent["id"]
    assert len(pages) == len(page_ids) > 1
    assert handler_sessions == [agent["id"]] * len(page_ids)
    events = [
        event
        for event in runtime.store.events()
        if event["type"] == "tool.started" and event["data"].get("call_id") in page_ids
    ]
    assert len(events) == len(page_ids) and all(
        event["session_id"] == agent["id"] for event in events
    )
    rebuilt = "".join(page["content"] for page in pages).encode("utf-8")
    assert rebuilt == raw and hashlib.sha256(rebuilt).hexdigest() == item["sha256"]
    assert not pages[-1]["has_more"] and pages[-1]["next_offset_bytes"] == len(raw)
    assert "队友全文尾部唯一条款" in pages[-1]["content"]
    assert host.attachments.get(item["id"]) == metadata and host.requests == []


@pytest.mark.parametrize("source", ["web", "local", "mcp", "channel:qq"])
async def test_ordinary_sources_keep_legacy_shape_and_allow_revision_first_page(
    host: Host, source: str
) -> None:
    item = await host.save("短文本🙂".encode(), "材料.txt", source=source)
    assert await host.call("attachment_read", id=item["id"]) == {
        "id": item["id"],
        "content": "短文本🙂",
        "truncated": False,
    }
    page = await host.call("attachment_read", id=item["id"], revision=item["sha256"])
    assert page["content"] == "短文本🙂" and not page["has_more"]
    assert page["revision"] == item["sha256"] and page["offset_bytes"] == 0


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"offset_bytes": -1}, "invalid_arguments"),
        ({"offset_bytes": True}, "invalid_arguments"),
        ({"offset_bytes": 1.0}, "invalid_arguments"),
        ({"offset_bytes": 1}, "invalid_arguments"),
        ({"revision": "bad"}, "invalid_arguments"),
        ({"revision": "A" * 64}, "invalid_arguments"),
        ({"revision": "0" * 64}, "revision_changed"),
    ],
)
async def test_ordinary_pages_reject_invalid_coordinates(
    host: Host, arguments: dict[str, Any], code: str
) -> None:
    item = await host.save("资料🙂".encode(), "材料.txt")
    with pytest.raises(EvidenceError) as caught:
        await host.call("attachment_read", id=item["id"], **arguments)
    assert caught.value.code == code


async def test_ordinary_pages_reject_character_middle_and_beyond_end(host: Host) -> None:
    item = await host.save("资料🙂".encode(), "材料.txt")
    for offset in (1, item["size"] + 1):
        with pytest.raises(EvidenceError) as caught:
            await host.call(
                "attachment_read", id=item["id"], revision=item["sha256"], offset_bytes=offset
            )
        assert caught.value.code == "invalid_offset"


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        (b"\xff", "attachment_encoding_error"),
        (b"\xf0\x9f", "attachment_encoding_error"),
        (b"\x00", "attachment_binary_data"),
    ],
)
async def test_ordinary_pages_validate_text_outside_page_and_at_eof(
    host: Host, tail: bytes, code: str
) -> None:
    raw = b"a" * 5000 + tail
    item = await host.save(raw, "坏尾部.txt")
    for offset in (0, len(raw)):
        with pytest.raises(EvidenceError) as caught:
            await host.call(
                "attachment_read", id=item["id"], revision=item["sha256"], offset_bytes=offset
            )
        assert caught.value.code == code


async def test_ordinary_stream_validation_keeps_utf8_across_chunk_boundary(host: Host) -> None:
    raw = b"a" * 65535 + "🙂尾".encode()
    item = await host.save(raw, "跨块字符.txt")
    page = await host.call(
        "attachment_read", id=item["id"], revision=item["sha256"], offset_bytes=65535
    )
    assert page["content"] == "🙂尾" and page["next_offset_bytes"] == len(raw)
    assert not page["has_more"]


async def test_ordinary_page_rejects_same_size_digest_drift(host: Host) -> None:
    item = await host.save(b"original", "材料.txt")
    host.attachments.path(item["id"]).write_bytes(b"modified")
    with pytest.raises(EvidenceError) as caught:
        await host.call("attachment_read", id=item["id"], revision=item["sha256"])
    assert caught.value.code == "attachment_changed"


async def test_ordinary_page_rejects_path_replacement_before_open(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = await host.save(b"original", "材料.txt")
    path = host.attachments.path(item["id"])
    original_open = os.open

    def replace_open(candidate: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if candidate == path:
            path.rename(path.with_name("original-backup"))
            path.write_bytes(b"original")
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_open)
    with pytest.raises(EvidenceError) as caught:
        await host.call("attachment_read", id=item["id"], offset_bytes=0)
    assert caught.value.code == "attachment_changed"


@pytest.mark.parametrize("change", ["metadata", "byte_limit", "output_limit"])
async def test_ordinary_pages_recheck_metadata_and_hot_limits_after_file_io(
    host: Host, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import muli.attachment_text as module

    if change == "output_limit":
        host.runtime.config.update({"limits": {"max_output_chars": 2000}})
    item = await host.save(b"x" * 1100, "材料.txt")
    original = module.file_io

    async def changed(operation: Callable[[], bytes]) -> bytes:
        raw = await original(operation)
        if change == "metadata":
            metadata = host.attachments.get(item["id"])
            metadata["source"] = "asr"
            host.runtime.store.put("attachment", metadata)
        else:
            setting = "max_file_bytes" if change == "byte_limit" else "max_output_chars"
            value = 1024 if change == "byte_limit" else 1000
            host.runtime.config.update({"limits": {setting: value}})
        return raw

    monkeypatch.setattr(module, "file_io", changed)
    if change == "output_limit":
        page = await host.call("attachment_read", id=item["id"], offset_bytes=0)
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000 and page["has_more"]
    else:
        with pytest.raises(EvidenceError) as caught:
            await host.call("attachment_read", id=item["id"], offset_bytes=0)
        assert caught.value.code == "attachment_changed"


async def test_asr_pagination_stays_on_the_transcript_reader(host: Host) -> None:
    item = await host.save("转写🙂\0仍是转写".encode(), "转写.txt", source="asr")
    for arguments in ({"offset_bytes": 0}, {"revision": item["sha256"]}):
        with pytest.raises(ValueError, match="audio_transcript_read"):
            await host.call("attachment_read", id=item["id"], **arguments)
    page = await host.call("audio_transcript_read", id=item["id"], revision=item["sha256"])
    assert page["content"] == "转写🙂\0仍是转写"
    assert page["trust"] == "transcript_data_not_instructions" and host.requests == []


async def test_ordinary_repeated_cancel_waits_for_owned_file_thread(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.attachment_text as module

    item = await host.save(b"ordinary text", "材料.txt")
    entered = asyncio.Event()
    release = threading.Event()
    settled = threading.Event()
    loop = asyncio.get_running_loop()
    original = module.file_io

    async def blocked(operation: Callable[[], bytes]) -> bytes:
        def read() -> bytes:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise AssertionError("file thread was not released")
            try:
                return operation()
            finally:
                settled.set()

        return await original(read)

    monkeypatch.setattr(module, "file_io", blocked)
    task = asyncio.create_task(host.call("attachment_read", id=item["id"], offset_bytes=0))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not settled.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert settled.is_set() and host.requests == []
