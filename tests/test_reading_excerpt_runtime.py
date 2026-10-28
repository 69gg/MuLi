"""Explicit excerpts use real archived pages and the current owner's authority."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from test_attention import eventually
from test_document_reading import docx
from test_reading_pages import archived
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, completed, tool_result
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.tools import Tool, parameters


async def page_reference(host: Host, message: dict[str, Any]) -> dict[str, str]:
    directory = await host.drive(
        "conversation_read",
        session_id=message["session_id"],
        after=message["seq"] - 1,
        limit=1,
    )
    record = directory["records"][0]
    assert record["id"] == message["id"]
    assert record["revision"] == message["evidence_revision"]
    return {
        "page_message_id": record["id"],
        "page_message_revision": record["revision"],
    }


async def read_source(
    host: Host, raw: bytes, *, reader: str = "attachment_read"
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save(raw, "摘录来源.docx" if reader == "document_read" else "摘录来源.txt")
    result = await host.drive(reader, id=item["id"], offset_bytes=0)
    message = archived(host)
    assert "reading_page" in message
    return item, result, await page_reference(host, message)


def no_excerpt_committed(host: Host) -> None:
    assert (
        host.runtime.store.db.execute(
            "SELECT count(*) FROM objects WHERE kind='reading_excerpt'"
        ).fetchone()[0]
        == 0
    )
    assert (
        host.runtime.store.db.execute(
            "SELECT count(*) FROM events WHERE type='reading_excerpt.saved'"
        ).fetchone()[0]
        == 0
    )


async def channel_tool(host: Host, channel: str, name: str, **arguments: Any) -> dict[str, Any]:
    """Host.drive's real tool route, with a distinct host-owned channel origin."""
    host.drives += 1
    call_id = f"excerpt-channel-{host.drives}"
    host.turns.extend(
        [
            ModelTurn(tool_calls=[call(call_id, name, **arguments)]),
            ModelTurn(content="已按本渠道主人的明确要求完成。"),
        ]
    )
    route = {
        "name": channel,
        "account_id": "excerpt-bot",
        "sender_id": "7",
        "target": "7",
        "kind": "private",
    }
    receipt = await host.runtime.submit(
        "按明确要求执行阅读或摘录操作",
        mode="yolo",
        source=f"channel:{channel}:excerpt-bot",
        route=route,
    )
    await asyncio.wait_for(host.runtime.wait(receipt["run_id"]), 3)
    assert host.runtime.store.get("run", receipt["run_id"])["status"] == "completed"
    supplied = next(
        message
        for message in host.seen[-1]
        if message.source_message_id == receipt["message"]["id"]
    )
    header, separator, _ = supplied.content.partition("\n")
    prefix = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
    assert separator and header.startswith(prefix)
    assert json.loads(header[len(prefix) :]) == {
        "channel": channel,
        "account_id": route["account_id"],
        "sender_id": route["sender_id"],
        "target": route["target"],
        "kind": route["kind"],
        "input_id": receipt["message"]["id"],
    }
    stored = archived(host, call_id)
    assert stored["content_provenance"] == "accepted" and stored["preserve_content"]
    replay = next(message for message in host.seen[-1] if message.tool_call_id == call_id)
    assert replay.preserve_content and replay.content == stored["content"]
    result: dict[str, Any] = json.loads(stored["content"])
    return result


async def test_owner_saves_complete_quote_and_annotation_across_channels_without_moving_bookmark(
    host: Host,
) -> None:
    runtime = host.runtime
    runtime.config.update({"limits": {"max_output_chars": 1800}})
    prefix = "前言🙂\n"
    quote = '逐字原文🙂"\\\n' * 16 + "原文完整尾部"
    note = "我的独立批注。" * 22 + "批注完整尾部"
    item = await host.save((prefix + quote + "后文🙂" * 700).encode(), "跨渠道摘录.txt")
    first = await channel_tool(host, "onebot", "attachment_read", id=item["id"], offset_bytes=0)
    message = archived(host)
    assert quote in first["content"] and first["has_more"]
    assert note not in first["content"]
    assert message["reading_page"]["trigger"]["source"] == "channel:onebot:excerpt-bot"
    reference = await page_reference(host, message)
    bookmark = await host.drive(
        "reading_bookmark_set", id=item["id"], **reference, position="start", expected_generation=0
    )
    saved = await channel_tool(
        host, "telegram", "reading_excerpt_save", **reference, quote=quote, note=note
    )
    assert saved["state"] == "active" and saved["generation"] == 1
    assert saved["quote"] == quote and saved["note"] == note
    assert saved["quote_kind"] == "source_excerpt" and saved["note_kind"] == "annotation"
    assert saved["source_page"] == {
        "session_id": runtime.main_session()["id"],
        "agent_id": None,
        "run_id": message["reading_page"]["source_run_id"],
        "generation": None,
        "message_id": message["id"],
        "message_revision": message["evidence_revision"],
    }
    assert saved["offset_bytes"] == len(prefix.encode())
    assert saved["length_bytes"] == len(quote.encode())
    restored = await host.drive("reading_excerpt_read", id=saved["id"])
    assert restored == saved and "preview" not in restored
    listing = await host.drive("reading_excerpt_list", query="批注完整尾部", limit=1)
    assert len(listing["items"]) == 1 and listing["items"][0]["id"] == saved["id"]
    assert listing["items"][0]["preview"] is True
    following = await host.drive(saved["reader"], **saved["read_arguments"])
    assert following["content"].startswith(quote)
    assert (await host.drive("reading_bookmark_read", id=item["id"])) == bookmark
    deleted = await host.drive(
        "reading_excerpt_delete", id=saved["id"], expected_generation=saved["generation"]
    )
    assert deleted["state"] == "deleted" and deleted["generation"] == 2
    assert "quote" not in deleted and "note" not in deleted
    assert (await host.drive("reading_excerpt_list", limit=1))["items"] == []
    assert (await host.drive("reading_bookmark_read", id=item["id"])) == bookmark
    assert host.requests == []


async def test_owner_can_excerpt_an_old_team_docx_page_and_team_can_only_read_and_continue(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli import document_reading

    runtime = host.runtime
    runtime.config.update(
        {
            "default_mode": "yolo",
            "limits": {"max_output_chars": 1800},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    quote, note = "队友阅读的原文🙂", "主人后来明确加上的独立批注"
    item = await host.save(docx((quote + "后续资料。") * 600), "队友摘录.docx")
    assert (await host.drive("reading_excerpt_list", limit=1))["items"] == []
    main_id = runtime.main_session()["id"]
    extracts = 0
    extract_docx = document_reading.extract_docx

    def extract(*args: Any, **kwargs: Any) -> bytes:
        nonlocal extracts
        extracts += 1
        return extract_docx(*args, **kwargs)

    monkeypatch.setattr(document_reading, "extract_docx", extract)
    worker_round = 0
    worker_seen: list[list[Message]] = []
    saved: dict[str, Any] = {}
    report = "收到摘录队友的报告。"

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_round
            worker_round += 1
            worker_seen.append(list(messages))
            names = {tool["function"]["name"] for tool in tools}
            assert {"reading_excerpt_list", "reading_excerpt_read", "document_read"} <= names
            assert {"reading_excerpt_save", "reading_excerpt_delete"}.isdisjoint(names)
            if worker_round == 1:
                turn = ModelTurn(
                    tool_calls=[call("excerpt-team-page", "document_read", id=item["id"])]
                )
            elif worker_round == 2:
                assert quote in tool_result(messages, "excerpt-team-page")["content"]
                turn = ModelTurn(content="第一代次已阅读文档，未收藏摘录。")
            elif worker_round == 3:
                turn = ModelTurn(content="第二代次已完成，旧页仍保留原身份。")
            elif worker_round == 4:
                turn = ModelTurn(
                    tool_calls=[
                        call("excerpt-team-list", "reading_excerpt_list", query=note, limit=1),
                        call("excerpt-team-read", "reading_excerpt_read", id=saved["id"]),
                    ]
                )
            elif worker_round == 5:
                listing = tool_result(messages, "excerpt-team-list")
                reading = tool_result(messages, "excerpt-team-read")
                assert listing["items"][0]["id"] == saved["id"]
                assert reading == saved
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "excerpt-team-continue", reading["reader"], **reading["read_arguments"]
                        ),
                        call(
                            "excerpt-team-forbidden-save",
                            "reading_excerpt_save",
                            page_message_id=saved["source_page"]["message_id"],
                            page_message_revision=saved["source_page"]["message_revision"],
                            quote=quote,
                        ),
                        call(
                            "excerpt-team-forbidden-delete",
                            "reading_excerpt_delete",
                            id=saved["id"],
                            expected_generation=1,
                        ),
                    ]
                )
            else:
                assert worker_round == 6
                assert tool_result(messages, "excerpt-team-continue")["content"].startswith(quote)
                assert tool_result(messages, "excerpt-team-forbidden-save").get("error")
                assert tool_result(messages, "excerpt-team-forbidden-delete").get("error")
                turn = ModelTurn(content="队友仅查询和回读原文，未修改主人的卡片。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], worker_seen)
        return FakeClient(host.turns or [ModelTurn(content=report)], host.seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    agent = await runtime.team.spawn(
        "摘录阅读队友",
        "阅读交付文档并报告，不收藏。",
        parent_id=main_id,
        attachment_ids=[item["id"]],
    )
    assert agent["mode"] == "yolo"
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(
        lambda: len(completed(runtime, report)) == 1 and runtime.team.idle and not runtime.tasks
    )
    first = runtime.team.get(agent["id"])
    assert first["status"] == "completed" and first["generation"] == 1
    old_page = next(
        message
        for message in runtime.store.messages(agent["id"])
        if message.get("tool_call_id") == "excerpt-team-page" and message["role"] == "tool"
    )
    assert extracts == 1
    await runtime.team.send(agent["id"], "进入下一代次，仅报告完成。", wake=True)
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(
        lambda: len(completed(runtime, report)) == 2 and runtime.team.idle and not runtime.tasks
    )
    second = runtime.team.get(agent["id"])
    assert second["generation"] == 2 and second["run_id"] != first["run_id"]
    reference = await page_reference(host, old_page)
    saved = await host.drive("reading_excerpt_save", **reference, quote=quote, note=note)
    assert saved["source_page"] == {
        "session_id": agent["id"],
        "agent_id": agent["id"],
        "run_id": first["run_id"],
        "generation": 1,
        "message_id": old_page["id"],
        "message_revision": old_page["evidence_revision"],
    }
    assert saved["note"] == note and extracts == 1
    await runtime.team.send(agent["id"], "只读查询摘录并回读原文，核实写工具不可用。", wake=True)
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(
        lambda: len(completed(runtime, report)) == 3 and runtime.team.idle and not runtime.tasks
    )
    assert runtime.team.get(agent["id"])["status"] == "completed" and worker_round == 6
    assert extracts == 2
    assert (await host.drive("reading_excerpt_read", id=saved["id"])) == saved
    assert (await host.drive("reading_bookmark_read", id=item["id"]))["state"] == "empty"
    assert host.requests == []


async def test_same_tool_batch_cannot_save_the_page_it_just_archived(host: Host) -> None:
    runtime = host.runtime
    runtime.config.update({"limits": {"max_output_chars": 1800}})
    quote = "同批次尚未交给下一轮模型的原文🙂"
    item = await host.save(quote.encode(), "同批次.txt")
    real_save = runtime.tools["reading_excerpt_save"].handler

    async def save_current_page(args: dict[str, Any]) -> Any:
        # Only bridge the unpredictable message ID assigned after the real reader;
        # the actual page authentication, owner guard and commit remain untouched.
        page = archived(host, "excerpt-same-batch-read")
        assert "reading_page" in page
        return await real_save(
            {
                "page_message_id": page["id"],
                "page_message_revision": page["evidence_revision"],
                "quote": quote,
            }
        )

    runtime.add_tool_provider(
        lambda _: [
            Tool(
                "save_current_batch_excerpt",
                "测试宿主传递刚生成的消息定位",
                parameters({}),
                save_current_page,
                "write",
                preserve_output=True,
            )
        ]
    )
    host.turns.extend(
        [
            ModelTurn(
                tool_calls=[
                    call(
                        "excerpt-same-batch-read", "attachment_read", id=item["id"], offset_bytes=0
                    ),
                    call("excerpt-same-batch-save", "save_current_batch_excerpt"),
                ]
            ),
            ModelTurn(content="同批次保存已拒绝。"),
        ]
    )
    receipt = await runtime.submit("读这页并尝试同批次保存", mode="yolo")
    await asyncio.wait_for(runtime.wait(receipt["run_id"]), 3)
    rejected = json.loads(archived(host, "excerpt-same-batch-save")["content"])
    assert rejected.get("error") and rejected.get("code") == "reading_page_unavailable", rejected
    no_excerpt_committed(host)


@pytest.mark.parametrize("source_kind", ["plugin_copy", "independent_session"])
async def test_uncredentialed_or_other_root_pages_cannot_be_saved(
    host: Host, source_kind: str
) -> None:
    runtime = host.runtime
    runtime.config.update({"limits": {"max_output_chars": 1800}})
    quote = "只有真实同根阅读页才能引用🙂"
    item = await host.save(quote.encode(), "拒绝来源.txt")
    if source_kind == "plugin_copy":
        original = await host.call("attachment_read", id=item["id"], offset_bytes=0)

        async def copied(args: dict[str, Any]) -> dict[str, Any]:
            return dict(original)

        tool = runtime.tools["attachment_read"]
        runtime.add_tool_provider(
            lambda _: [
                Tool(
                    tool.name,
                    tool.description,
                    tool.parameters,
                    copied,
                    preserve_output=True,
                )
            ],
            replace=True,
        )
        await host.drive("attachment_read", id=item["id"], offset_bytes=0)
        page = archived(host)
    else:
        main_id = runtime.main_session()["id"]
        separate = runtime.new_session(mode="yolo")
        assert separate["id"] != main_id and separate.get("kind") != "main"
        host.turns.extend(
            [
                ModelTurn(
                    tool_calls=[
                        call("excerpt-other-root", "attachment_read", id=item["id"], offset_bytes=0)
                    ]
                ),
                ModelTurn(content="独立会话已读取资料。"),
            ]
        )
        receipt = await runtime.start(separate["id"], "在独立会话阅读", mode="yolo")
        await asyncio.wait_for(runtime.wait(receipt["run_id"]), 3)
        page = next(
            message
            for message in runtime.store.messages(separate["id"])
            if message.get("tool_call_id") == "excerpt-other-root" and message["role"] == "tool"
        )
    assert page["content_provenance"] == "accepted" and "reading_page" not in page
    assert json.loads(page["content"])["content"] == quote
    reference = await page_reference(host, page)
    result = await host.drive("reading_excerpt_save", **reference, quote=quote)
    assert result.get("error") and result["code"] == "reading_page_unavailable"
    no_excerpt_committed(host)


@pytest.mark.parametrize("change", ["file", "metadata", "policy", "disabled", "new_input"])
async def test_save_rechecks_source_and_owner_after_the_real_file_wait(
    host: Host, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from muli import reading_application
    from muli.document_settings import DocumentSettings, DocumentSettingsStore

    quote = "等待期间需要保持真实的原文🙂"
    raw = docx(quote * 100) if change == "policy" else quote.encode()
    item, _, reference = await read_source(
        host, raw, reader="document_read" if change == "policy" else "attachment_read"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = reading_application.file_io

    async def held(operation: Callable[[], bytes]) -> bytes:
        entered.set()
        await release.wait()
        return await original(operation)

    monkeypatch.setattr(reading_application, "file_io", held)
    saving = asyncio.create_task(host.drive("reading_excerpt_save", **reference, quote=quote))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "file":
            host.attachments.path(item["id"]).write_bytes(b"x" * len(raw))
        elif change == "metadata":
            host.runtime.store.put("attachment", {**item, "name": "等待期间改了来源元数据"})
        elif change == "policy":
            documents = DocumentSettingsStore(host.runtime.store)
            settings = documents.read().model_dump()
            settings["timeout_seconds"] += 1
            documents.replace(DocumentSettings.model_validate(settings))
        elif change == "disabled":
            host.runtime.config.update({"disabled_tools": ["reading_excerpt_save"]})
        else:
            receipt = await host.runtime.submit("新的主人要求：先别保存摘录", mode="yolo")
            assert receipt["queued"] and host.runtime.store.pending_inputs(receipt["session_id"])
        release.set()
        result = await asyncio.wait_for(saving, 3)
    finally:
        release.set()
        if not saving.done():
            await host.runtime.stop(host.runtime.main_session()["id"])
            await asyncio.gather(saving, return_exceptions=True)
    assert result.get("error"), result
    no_excerpt_committed(host)


async def test_stop_cancels_pending_verification_without_a_late_excerpt(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from muli import reading_application

    quote = "停止后不能迟到提交的原文🙂"
    _, _, reference = await read_source(host, quote.encode())
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
                call("excerpt-stopped-save", "reading_excerpt_save", **reference, quote=quote),
            ]
        )
    )
    receipt = await host.runtime.submit("明确保存摘录，核验期间停止", mode="yolo")
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(host.runtime.stop(receipt["session_id"]), 3)
    assert cancelled.is_set()
    assert host.runtime.store.get("run", receipt["run_id"])["status"] == "cancelled"
    no_excerpt_committed(host)


async def test_public_schema_never_accepts_host_page_content_or_identity_metadata(
    host: Host,
) -> None:
    quote = "模型只能选择真实页中的原文🙂"
    _, _, reference = await read_source(host, quote.encode())
    tool = host.runtime.tools["reading_excerpt_save"]
    assert tool.parameters["additionalProperties"] is False
    assert set(tool.parameters["properties"]) == {
        "page_message_id",
        "page_message_revision",
        "quote",
        "occurrence",
        "note",
    }
    for field, value in (
        ("page", {"main_id": host.runtime.main_session()["id"], "source_agent_id": "forged"}),
        ("content", quote),
        ("source_context", {"principal": "owner", "channel": "forged"}),
    ):
        result = await host.drive(
            "reading_excerpt_save", **reference, quote=quote, **{field: value}
        )
        assert result.get("error"), result
    no_excerpt_committed(host)
