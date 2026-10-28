"""Explicit reading positions through real ingress, tool archival and file reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from test_document_reading import docx
from test_runtime import FakeClient
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.providers import Message, ModelTurn, StreamEvent
from muli.tools import Tool


def source_page(host: Host) -> dict[str, Any]:
    return next(
        message
        for message in reversed(host.runtime.store.messages(host.runtime.main_session()["id"]))
        if message["role"] == "tool"
        and message.get("tool_call_id")
        and json.loads(message["content"]).get("revision")
    )


def bookmark_arguments(item: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "page_message_id": page["id"],
        "page_message_revision": page["evidence_revision"],
        "position": "next",
        "expected_generation": 0,
    }


async def test_explicit_bookmark_resumes_real_utf8_without_advancing_on_reads(host: Host) -> None:
    assert "reading_bookmark_set" in host.runtime.tools
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    text = "真实续读位置🙂\n" * 500 + "这是真正的文末。"
    item = await host.save(text.encode(), "同一份资料.txt")
    first = await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    page = source_page(host)
    before = await host.drive("reading_bookmark_list", limit=1)
    assert before["items"] == []
    saved = await host.drive("reading_bookmark_set", **bookmark_arguments(item, page))
    assert "error" not in saved, saved
    assert saved["state"] == "active" and saved["generation"] == 1
    assert saved["offset_bytes"] == first["next_offset_bytes"]
    listing = await host.drive("reading_bookmark_list", limit=1)
    assert len(listing["items"]) == 1 and listing["items"][0]["id"] == item["id"]
    position = await host.drive("reading_bookmark_read", id=item["id"])
    assert position["verification"] == "not_checked"
    following = await host.drive(
        "attachment_read",
        id=item["id"],
        revision=first["revision"],
        offset_bytes=position["offset_bytes"],
    )
    assert (
        following["content"]
        == text.encode()[position["offset_bytes"] : following["next_offset_bytes"]].decode()
    )
    again = await host.drive("reading_bookmark_read", id=item["id"])
    assert again == position
    deleted = await host.drive("reading_bookmark_delete", id=item["id"], expected_generation=1)
    assert deleted["state"] == "deleted" and deleted["generation"] == 2
    assert (await host.drive("reading_bookmark_list", limit=1))["items"] == []
    assert (await host.drive("reading_bookmark_read", id=item["id"]))["generation"] == 2


async def test_bookmark_cannot_trust_a_plugin_result_with_the_same_json(host: Host) -> None:
    assert "reading_bookmark_set" in host.runtime.tools
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save("伪装结果不能提供宿主页证据🙂".encode(), "资料.txt")
    original = await host.call("attachment_read", id=item["id"], offset_bytes=0)

    async def forged(args: dict[str, Any]) -> dict[str, Any]:
        return dict(original)

    old = host.runtime.tools["attachment_read"]
    host.runtime.add_tool_provider(
        lambda runtime: [
            Tool(old.name, old.description, old.parameters, forged, preserve_output=True)
        ],
        replace=True,
    )
    result = await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    assert result == original
    page = source_page(host)
    rejected = await host.drive("reading_bookmark_set", **bookmark_arguments(item, page))
    assert rejected.get("error") and rejected.get("code") == "reading_page_unavailable"
    assert (await host.drive("reading_bookmark_read", id=item["id"]))["generation"] == 0


async def test_document_bookmark_saves_without_running_the_extractor_again(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muli import document_reading

    assert "reading_bookmark_set" in host.runtime.tools
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    count = 0
    original = document_reading.extract_docx

    def extract(*args: Any, **kwargs: Any) -> bytes:
        nonlocal count
        count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(document_reading, "extract_docx", extract)
    item = await host.save(docx("真实文档🙂" * 500), "资料.docx")
    first = await host.drive("document_read", id=item["id"])
    page = source_page(host)
    assert count == 1
    saved = await host.drive("reading_bookmark_set", **bookmark_arguments(item, page))
    assert "error" not in saved, saved
    current = await host.drive("reading_bookmark_read", id=item["id"])
    assert count == 1 and current["offset_bytes"] == first["next_offset_bytes"]
    following = await host.drive(
        "document_read",
        id=item["id"],
        revision=current["revision"],
        offset_bytes=current["offset_bytes"],
    )
    assert count == 2 and following["offset_bytes"] == current["offset_bytes"]


@pytest.mark.parametrize("change", ["file", "metadata", "policy", "disabled", "new_input"])
async def test_saving_revalidates_source_and_admission_after_real_file_wait(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from muli import reading_application
    from muli.document_settings import DocumentSettings, DocumentSettingsStore

    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    raw = (
        docx("等待保存的完整文档🙂" * 100)
        if change == "policy"
        else "等待保存的真实文本🙂".encode()
    )
    item = await host.save(raw, "等待期间的资料")
    await host.drive(
        "document_read" if change == "policy" else "attachment_read", id=item["id"], offset_bytes=0
    )
    page = source_page(host)
    entered, release = asyncio.Event(), asyncio.Event()
    original = reading_application.file_io

    async def held(operation: Callable[[], bytes]) -> bytes:
        entered.set()
        await release.wait()
        return await original(operation)

    monkeypatch.setattr(reading_application, "file_io", held)
    saving = asyncio.create_task(
        host.drive("reading_bookmark_set", **bookmark_arguments(item, page))
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "file":
            host.attachments.path(item["id"]).write_bytes(b"x" * len(raw))
        elif change == "metadata":
            host.runtime.store.put("attachment", {**item, "name": "名字在等待时改变了"})
        elif change == "policy":
            documents = DocumentSettingsStore(host.runtime.store)
            settings = documents.read().model_dump()
            settings["timeout_seconds"] += 1
            documents.replace(DocumentSettings.model_validate(settings))
        elif change == "disabled":
            host.runtime.config.update({"disabled_tools": ["reading_bookmark_set"]})
        else:
            await host.runtime.submit("新的要求：先别保存这个位置", mode="yolo")
        release.set()
        result = await asyncio.wait_for(saving, 3)
    finally:
        release.set()
        if not saving.done():
            await host.runtime.stop(host.runtime.main_session()["id"])
            await asyncio.gather(saving, return_exceptions=True)
    assert result.get("error"), result
    assert (await host.drive("reading_bookmark_read", id=item["id"]))["state"] == "empty"
    assert (
        host.runtime.store.db.execute(
            "SELECT count(*) FROM events WHERE type='reading_bookmark.set'"
        ).fetchone()[0]
        == 0
    )


async def test_stopping_while_verifying_the_file_never_commits_a_late_bookmark(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_teammate_attachment_runtime import call

    from muli import reading_application
    from muli.providers import ModelTurn

    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save("取消中的真实资料🙂".encode(), "取消.txt")
    await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    page = source_page(host)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def held(operation: Callable[[], bytes]) -> bytes:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("held file verification returned without cancellation")

    monkeypatch.setattr(reading_application, "file_io", held)
    host.turns.append(
        ModelTurn(
            tool_calls=[
                call("cancel-bookmark", "reading_bookmark_set", **bookmark_arguments(item, page))
            ]
        )
    )
    receipt = await host.runtime.submit("明确保存位置，测试等待期间停止", mode="yolo")
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(host.runtime.stop(receipt["session_id"]), 3)
    assert cancelled.is_set()
    assert host.runtime.store.get("run", receipt["run_id"])["status"] == "cancelled"
    assert (await host.drive("reading_bookmark_read", id=item["id"]))["generation"] == 0


async def test_one_owner_request_can_save_a_page_on_the_next_model_round(host: Host) -> None:
    from test_teammate_attachment_runtime import call

    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save(("同一次请求的资料🙂" * 100).encode(), "一次请求.txt")
    rounds = 0

    class OneRequestClient(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            host.seen.append(messages)
            if rounds == 1:
                turn = ModelTurn(
                    tool_calls=[call("one-read", "attachment_read", id=item["id"], offset_bytes=0)]
                )
            elif rounds == 2:
                page = source_page(host)
                turn = ModelTurn(
                    tool_calls=[
                        call("one-save", "reading_bookmark_set", **bookmark_arguments(item, page))
                    ]
                )
            else:
                assert rounds == 3
                turn = ModelTurn(content="已保存续读位置。")
            yield StreamEvent("done", turn=turn)

    host.runtime.client_factory = lambda config: OneRequestClient([], host.seen)
    receipt = await host.runtime.submit("读这一页，帮我保存末尾位置", mode="yolo")
    await asyncio.wait_for(host.runtime.wait(receipt["run_id"]), 3)
    messages = host.runtime.store.messages(receipt["session_id"])
    owners = [
        message
        for message in messages
        if message["role"] == "user" and message.get("principal") == "owner"
    ]
    assert len(owners) == 1 and rounds == 3
    saved = next(message for message in messages if message.get("tool_call_id") == "one-save")
    result = json.loads(saved["content"])
    assert result["state"] == "active" and result["generation"] == 1
    original_page = next(
        message for message in messages if message.get("tool_call_id") == "one-read"
    )
    assert result["source_page"]["message_id"] == original_page["id"]
