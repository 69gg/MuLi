"""Explicit document collection through real owner tools and persisted memory."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_attachment_memory_policy import offline as offline
from test_document_reading import docx, pdf
from test_runtime import FakeClient

from muli.document_settings import DocumentSettings, DocumentSettingsStore
from muli.providers import ModelConfig, ModelTurn, ToolCall
from muli.store import new_id


async def start_tool(rig: MemoryRig, name: str, **args: Any) -> tuple[dict[str, Any], str]:
    call_id = "document-memory-" + new_id()
    turns = [
        ModelTurn(tool_calls=[ToolCall(call_id, name, json.dumps(args))]),
        ModelTurn(content="本次操作以实际工具回执为准。"),
        ModelTurn(content="已收到后续主人输入。"),
    ]

    def factory(profile: ModelConfig) -> FakeClient:
        return FakeClient(turns, [])

    rig.runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await rig.runtime.submit("请按本次要求处理资料。", mode="yolo")
    return receipt, call_id


async def tool(rig: MemoryRig, name: str, **args: Any) -> dict[str, Any]:
    receipt, call_id = await start_tool(rig, name, **args)
    await rig.settle(receipt)
    return rig.result(call_id)


def pdf_command(rig: MemoryRig, text: str) -> None:
    store = DocumentSettingsStore(rig.runtime.store)
    values = store.read().model_dump()
    values["pdf_command"] = [sys.executable, "-c", f"print({text!r})"]
    store.replace(DocumentSettings.model_validate(values))


async def test_full_document_is_collected_without_waiting_for_background_models(
    tmp_path: Path,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    extracted: list[str] = []
    text = "ORCHID文档核验条件：" + "完整正文🙂。" * 400 + "最后一条需要核对归档清单。"

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if not source["id"].startswith("attachment:"):
            return {"memories": [], "aliases": {}}
        extracted.append(source["id"])
        entered.set()
        await release.wait()
        return {
            "memories": [
                {"layer": "identity", "content": "资料作者是主人", "quote": source["content"]},
                {
                    "layer": "semantic",
                    "content": "ORCHID需要核对归档清单",
                    "quote": "最后一条需要核对归档清单。",
                },
            ],
            "aliases": {"资料作者": "主人"},
        }

    async with actual_app(tmp_path, extractor=extract) as rig:
        rig.runtime.config.update({"limits": {"max_output_chars": 1600}})
        raw = docx(text)
        item = await rig.upload(raw)
        page = await tool(rig, "document_read", id=item["id"])
        assert page["has_more"] and page["content"] != text + "\n"
        rig.absent(item["id"])
        result = await tool(
            rig, "memory_import_attachment", id=item["id"], revision=page["revision"]
        )
        source_id = "attachment:" + item["id"]
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            assert not release.is_set() and result["state"] in {"queued", "deferred"}
            assert result["source_id"] == source_id and result["created"] is True
            for field in ("revision", "source_revision", "format", "scope"):
                assert result[field] == page[field]
            source = rig.runtime.memory.source(source_id)
            assert source is not None and source["content"] == text + "\n"
            assert source["trusted_owner"] is False and source["role"] == "tool"
            assert source["principal"] == "external:attachment"
            assert item["sha256"] in source["source"] and page["revision"] in source["source"]
            assert "docx_main_body_text" in source["source"]
            assert rig.attachments.path(item["id"]).read_bytes() == raw
        finally:
            release.set()
        job = await asyncio.wait_for(rig.memory.pipeline.wait(source_id), timeout=3)
        assert job is not None and job["state"] == "done"
        assert job["checkpoint"]["extraction"]["rejected_untrusted_owner_claims"] == 1
        assert extracted == [source_id]
        assert (
            rig.runtime.store.db.execute(
                "SELECT count(*) FROM memory_aliases WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            == 0
        )
        duplicate = await tool(
            rig, "memory_import_attachment", id=item["id"], revision=page["revision"]
        )
        assert duplicate["created"] is False and rig.memory.pipeline.job(source_id) == job
        assert extracted == [source_id]
        found = await tool(rig, "memory_query", query="ORCHID", limit=2)
        assert any(item["source_id"] == source_id for item in found["memories"])
        # The retained extraction remains readable even when the original
        # attachment is temporarily unavailable; no converter is consulted.
        path = rig.attachments.path(item["id"])
        path.unlink()
        content: list[str] = []
        evidence = await tool(rig, "memory_source", id=source_id)
        for _ in range(16):
            content.append(evidence["content"])
            if not evidence["has_more"]:
                break
            evidence = await tool(
                rig,
                "memory_source",
                id=source_id,
                revision=evidence["revision"],
                offset_bytes=evidence["next_offset_bytes"],
            )
        else:
            raise AssertionError("bounded retained source did not reach its tail")
        assert "".join(content) == text + "\n"
        path.write_bytes(raw)
        episode = rig.runtime.memory.automatic_episode_id(source_id)
        assert episode is not None
        rig.runtime.memory.delete(episode)
        forgotten = await tool(
            rig, "memory_import_attachment", id=item["id"], revision=page["revision"]
        )
        assert forgotten["state"] == "forgotten"
        rig.absent(item["id"])


async def test_changed_extraction_never_appends_or_replaces_a_collected_version(
    tmp_path: Path,
) -> None:
    async with actual_app(tmp_path) as rig:
        item = await rig.upload(pdf())
        pdf_command(rig, "第一版完整正文")
        first = await tool(rig, "document_read", id=item["id"])
        pdf_command(rig, "第二版正文")
        stale = await tool(
            rig, "memory_import_attachment", id=item["id"], revision=first["revision"]
        )
        assert "error" in stale
        rig.absent(item["id"])
        second = await tool(rig, "document_read", id=item["id"])
        assert first["revision"] != second["revision"]
        collected = await tool(
            rig, "memory_import_attachment", id=item["id"], revision=second["revision"]
        )
        assert collected["created"] is True
        source = rig.runtime.memory.source(collected["source_id"])
        assert source is not None and source["content"] == "第二版正文\n"
        pdf_command(rig, "第三版不同正文")
        third = await tool(rig, "document_read", id=item["id"])
        conflict = await tool(
            rig, "memory_import_attachment", id=item["id"], revision=third["revision"]
        )
        assert "error" in conflict
        assert rig.runtime.memory.source(collected["source_id"]) == source


async def test_ascii_pdf_cannot_bypass_the_document_revision_requirement(tmp_path: Path) -> None:
    async with actual_app(tmp_path) as rig:
        item = await rig.upload(pdf())
        assert pdf().isascii()
        result = await tool(rig, "memory_import_attachment", id=item["id"])
        assert "error" in result and "revision" in result["error"]
        rig.absent(item["id"])


@pytest.mark.parametrize(
    "failure", ["missing_revision", "source", "sha", "source_limit", "file_limit"]
)
async def test_invalid_document_never_becomes_a_memory_source(tmp_path: Path, failure: str) -> None:
    async with actual_app(tmp_path) as rig:
        item = await rig.upload(
            docx("".join(f"必须完整保存的正文{index}。\n" for index in range(300)))
        )
        page = await tool(rig, "document_read", id=item["id"])
        args = {"id": item["id"], "revision": page["revision"]}
        if failure == "missing_revision":
            args.pop("revision")
        elif failure == "source":
            rig.runtime.store.put("attachment", {**item, "source": "vision"})
        elif failure == "sha":
            rig.attachments.path(item["id"]).write_bytes(b"X" * item["size"])
        elif failure == "source_limit":
            rig.runtime.config.update({"memory": {"max_source_chars": 1000}})
        else:
            assert item["size"] > 1024
            rig.runtime.config.update({"uploads": {"max_file_bytes": 1024}})
        result = await tool(rig, "memory_import_attachment", **args)
        assert "error" in result
        rig.absent(item["id"])


@pytest.mark.parametrize("revocation", ["owner_input", "reconfigure"])
async def test_document_conversion_await_preserves_owner_and_generation_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    from muli import local_services

    async with actual_app(tmp_path) as rig:
        item = await rig.upload(docx("等待期间收到新的主人输入时不能继续收藏。"))
        page = await tool(rig, "document_read", id=item["id"])
        original = local_services.document_view
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused(*args: Any, **kwargs: Any) -> local_services.DocumentView:
            view = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return view

        monkeypatch.setattr(local_services, "document_view", paused)
        receipt, call_id = await start_tool(
            rig, "memory_import_attachment", id=item["id"], revision=page["revision"]
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            if revocation == "owner_input":
                await rig.runtime.submit("先不要收藏，处理这条新输入。")
            else:
                previous = rig.memory.pipeline
                await rig.memory.reconfigure()
                assert (rig.memory.pipeline is previous) is False
            rig.absent(item["id"])
        finally:
            release.set()
        await rig.settle(receipt)
        result = rig.result(call_id)
        assert "error" in result or result.get("status") == "not_executed"
        rig.absent(item["id"])


async def test_installed_pdf_converter_can_collect_its_complete_text_layer(tmp_path: Path) -> None:
    if shutil.which("pdftotext") is None:
        pytest.skip("installed converter is optional; no download")
    async with actual_app(tmp_path) as rig:
        item = await rig.upload(pdf())
        page = await tool(rig, "document_read", id=item["id"])
        result = await tool(
            rig, "memory_import_attachment", id=item["id"], revision=page["revision"]
        )
        assert result["format"] == "pdf" and result["scope"] == "pdf_text_layer"
        source = rig.runtime.memory.source(result["source_id"])
        assert source is not None and source["content"] == page["content"]
        assert "MULI DOCUMENT END" in source["content"]
