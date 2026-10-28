"""Authenticated archived text shares the existing host page proof contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_attention import eventually
from test_document_reading import configure_document, docx, pdf
from test_document_reading import pdf_host as pdf_host
from test_reading_pages import archived, real_page
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.evidence import EvidenceError
from muli.providers import ModelConfig, ModelTurn
from muli.store import new_id
from muli.tools import Tool


def arguments(host: Host, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": message["id"],
        "message_revision": message["evidence_revision"],
        "before_seq": message["seq"] + 1,
        "main_id": host.runtime.main_session()["id"],
    }


async def test_content_is_exact_and_comes_from_one_bounded_authenticated_read(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    raw = ('真实原文🙂"\\\n组合e\u0301 与 é 不混同。' * 300).encode()
    item = await host.save(raw, "精确原文.txt")
    result = await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    message = archived(host)
    pages, store = host.services.reading_pages, host.runtime.store
    args = arguments(host, message)
    legacy = pages.source(**args)
    assert message["content_provenance"] == "accepted" and "reading_page" in message
    original_slice, original_get = store.message_evidence_slice, store.get
    slices = 0
    unbounded_messages: list[str] = []

    def observed_slice(*positional: Any, **keywords: Any) -> Any:
        nonlocal slices
        slices += 1
        return original_slice(*positional, **keywords)

    def observed_get(kind: str, identifier: str) -> dict[str, Any] | None:
        if kind == "message":
            unbounded_messages.append(identifier)
        return original_get(kind, identifier)

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "message_evidence_slice", observed_slice)
        scoped.setattr(store, "get", observed_get)
        page, policy, content = pages.source_content(**args)
    assert slices == 1 and unbounded_messages == []
    assert (page, policy) == legacy and len(page) == 17
    assert pages.source(**args) == legacy
    assert "content" not in page and "document_policy" not in page
    assert type(content) is str and content == result["content"]
    assert content.encode() == raw[page["offset_bytes"] : page["next_offset_bytes"]]
    assert len(content.encode()) == page["length_bytes"] and page["has_more"]


@pytest.mark.parametrize("kind", ["docx", "pdf"])
async def test_archived_document_content_and_original_policy_need_no_extraction(
    pdf_host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    from muli import document_reading

    pdf_host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    unit = '已交付文档正文🙂"\\\n'
    repeats = 180
    text = unit * repeats
    marker = tmp_path / "page-converter.calls"
    if kind == "docx":
        raw = docx(text)
    else:
        raw = pdf()
        command = (
            "import sys; from pathlib import Path; "
            f"assert sys.stdin.buffer.read().hex()=={raw.hex()!r}; "
            'assert sys.argv[1:]==["-enc","UTF-8","-eol","unix","-","-"]; '
            f"p=Path({str(marker)!r}); "
            'p.write_text((p.read_text() if p.exists() else "")+"1"); '
            f"sys.stdout.buffer.write({unit.encode()!r}*{repeats})"
        )
        configure_document(pdf_host, {"pdf_command": [sys.executable, "-c", command]})
    original = document_reading.extract_docx
    extractions = 0

    def counted(data: bytes, **keywords: Any) -> bytes:
        nonlocal extractions
        extractions += 1
        return original(data, **keywords)

    monkeypatch.setattr(document_reading, "extract_docx", counted)
    item = await pdf_host.save(raw, f"完整归档.{kind}")
    delivered = await pdf_host.drive("document_read", id=item["id"])
    message = archived(pdf_host)
    pages = pdf_host.services.reading_pages
    args = arguments(pdf_host, message)
    legacy = pages.source(**args)
    assert legacy[1] == pages.document_policy()
    configure_document(pdf_host, {"max_xml_depth": 64})
    assert legacy[1] != pages.document_policy()
    page, policy, content = pages.source_content(**args)
    assert (page, policy) == legacy and pages.source(**args) == legacy
    assert content == delivered["content"]
    assert page["reader"] == "document_read" and page["source_revision"] == item["sha256"]
    assert page["revision"] == delivered["revision"]
    assert extractions == (1 if kind == "docx" else 0)
    if kind == "pdf":
        assert marker.read_text() == "1"


@pytest.mark.parametrize("change", ["body", "version", "assistant_call", "provenance"])
async def test_content_rejects_the_same_archive_edits_as_the_existing_source(
    host: Host, change: str
) -> None:
    _, _, message = await real_page(host)
    pages, store = host.services.reading_pages, host.runtime.store
    args = arguments(host, message)
    pages.source(**args)
    if change == "version":
        store.put("message", dict(message))
    else:
        with store.db:
            if change == "body":
                changed = json.loads(message["content"])
                changed["content"] = "编辑后的正文不能借用旧页凭据。"
                store.db.execute(
                    "UPDATE objects SET data=json_set(data,'$.content',?) "
                    "WHERE kind='message' AND id=?",
                    (json.dumps(changed, ensure_ascii=False), message["id"]),
                )
            elif change == "assistant_call":
                store.db.execute(
                    "UPDATE objects SET data=json_set(data,'$.tool_calls[0].name','document_read') "
                    "WHERE kind='message' AND id=?",
                    (message["reading_page"]["assistant_id"],),
                )
            else:
                store.db.execute(
                    "UPDATE objects SET data=json_set(data,'$.content_provenance','runtime_excerpt') "
                    "WHERE kind='message' AND id=?",
                    (message["id"],),
                )
    with pytest.raises(EvidenceError) as legacy:
        pages.source(**args)
    assert legacy.value.code == "reading_page_unavailable"
    with pytest.raises(EvidenceError) as current:
        pages.source_content(**args)
    assert current.value.code == legacy.value.code


@pytest.mark.parametrize("boundary", ["same_assistant", "same_tool", "bool", "other_root"])
async def test_content_preserves_batch_and_root_boundaries(host: Host, boundary: str) -> None:
    _, _, message = await real_page(host)
    pages = host.services.reading_pages
    args = arguments(host, message)
    assert pages.source(**args)[0]["source_message_id"] == message["id"]
    if boundary == "same_assistant":
        args["before_seq"] = message["reading_page"]["assistant_seq"]
    elif boundary == "same_tool":
        args["before_seq"] = message["seq"]
    elif boundary == "bool":
        args["before_seq"] = True
    else:
        args["main_id"] = new_id()
    with pytest.raises(EvidenceError) as legacy:
        pages.source(**args)
    assert legacy.value.code == "reading_page_unavailable"
    with pytest.raises(EvidenceError) as current:
        pages.source_content(**args)
    assert current.value.code == legacy.value.code


async def test_copied_plugin_json_is_not_authenticated_content(host: Host) -> None:
    host.runtime.config.update({"limits": {"max_output_chars": 1800}})
    item = await host.save("真实读取再复制 JSON 也不能获得凭据🙂".encode(), "复制原文.txt")
    original = host.runtime.tools["attachment_read"]

    async def copied(args: dict[str, Any]) -> dict[str, Any]:
        return dict(await original.handler(args))

    host.runtime.add_tool_provider(
        lambda runtime: [
            Tool(
                original.name,
                original.description,
                original.parameters,
                copied,
                preserve_output=True,
            )
        ],
        replace=True,
    )
    result = await host.drive("attachment_read", id=item["id"], offset_bytes=0)
    message = archived(host)
    assert result["content"] == "真实读取再复制 JSON 也不能获得凭据🙂"
    assert message["content_provenance"] == "accepted" and "reading_page" not in message
    args = arguments(host, message)
    with pytest.raises(EvidenceError) as legacy:
        host.services.reading_pages.source(**args)
    assert legacy.value.code == "reading_page_unavailable"
    with pytest.raises(EvidenceError) as current:
        host.services.reading_pages.source_content(**args)
    assert current.value.code == legacy.value.code


async def test_old_completed_team_content_keeps_the_original_actor_after_a_new_generation(
    host: Host,
) -> None:
    item, _, _ = await real_page(host)
    runtime = host.runtime
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    worker_turns = [
        ModelTurn(
            tool_calls=[call("content-team-page", "attachment_read", id=item["id"], offset_bytes=0)]
        ),
        ModelTurn(content="第一代次真实阅读已完成。"),
        ModelTurn(content="第二代次完成，历史阅读者不变。"),
    ]

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return FakeClient(worker_turns, host.seen)
        assert config.model == "main"
        return FakeClient([ModelTurn(content="已收到阅读队友报告。")], host.seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    main_id = runtime.main_session()["id"]
    assert runtime.main_session()["mode"] == "yolo"
    agent = await runtime.team.spawn(
        "归档正文队友", "真实读取交付附件", parent_id=main_id, attachment_ids=[item["id"]]
    )
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(lambda: runtime.team.idle and not runtime.tasks)
    first = runtime.team.get(agent["id"])
    assert first["status"] == "completed" and first["generation"] == 1
    message = next(
        entry
        for entry in runtime.store.messages(agent["id"])
        if entry["role"] == "tool" and entry.get("tool_call_id") == "content-team-page"
    )
    args = arguments(host, message)
    legacy = host.services.reading_pages.source(**args)
    await runtime.team.send(agent["id"], "只完成下一代次，不重读旧页。", wake=True)
    await runtime.team.wait([agent["id"]], timeout=2)
    await eventually(lambda: runtime.team.idle and not runtime.tasks)
    second = runtime.team.get(agent["id"])
    assert second["status"] == "completed" and second["generation"] == 2
    assert second["run_id"] != first["run_id"] and not worker_turns
    assert host.services.reading_pages.source(**args) == legacy
    page, policy, content = host.services.reading_pages.source_content(**args)
    assert (page, policy) == legacy
    assert content == json.loads(message["content"])["content"]
    assert page["source_session_id"] == page["source_agent_id"] == agent["id"]
    assert page["source_run_id"] == first["run_id"] and page["source_generation"] == 1
    assert page["main_id"] == main_id
    runtime.store.put("agent", {**second, "parent_id": new_id()})
    with pytest.raises(EvidenceError) as caught:
        host.services.reading_pages.source_content(**args)
    assert caught.value.code == "reading_page_unavailable"
