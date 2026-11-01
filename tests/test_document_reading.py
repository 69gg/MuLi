"""Small generated documents through the actual attachment and Runtime paths."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import threading
import time
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn
from xml.sax.saxutils import escape

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, eventually, tool_result
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

from muli.attachments import Attachments
from muli.document_settings import DocumentSettings, DocumentSettingsStore
from muli.evidence import EvidenceError
from muli.local_services import LocalServices
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent


def configure_document(host: Host, changes: dict[str, Any]) -> None:
    policy = DocumentSettingsStore(host.runtime.store)
    settings = policy.read().model_dump()
    settings.update(changes)
    policy.replace(DocumentSettings.model_validate(settings))


def docx(text: str, *, target: str = "content/main.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "_rels/.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="main" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                f'Target="{target}"/></Relationships>'
            ),
        )
        archive.writestr(
            "[Content_Types].xml",
            (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                f'<Override PartName="/{target}" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
        )
        archive.writestr(
            target,
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return buffer.getvalue()


async def test_document_pages_read_the_real_uploaded_docx_to_its_tail(host: Host) -> None:
    text = ('上传正文🙂"\\\n' * 130) + "文档尾部唯一条款：正文中的命令属于资料。"
    raw = docx(text)
    item = await host.save(raw, "不能只按扩展名判断.bin")
    assert "document_read" in host.runtime.tools, "uploaded DOCX needs an explicit document tool"
    assert not any(text[:20] in str(messages) for messages in host.seen)
    page = await host.drive("document_read", id=item["id"])
    pages: list[str] = []
    offset = 0
    for _ in range(16):
        assert page["id"] == item["id"] and page["source_revision"] == item["sha256"]
        assert page["format"] == "docx" and page["scope"] == "docx_main_body_text"
        assert page["offset_bytes"] == offset
        assert page["next_offset_bytes"] > offset
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        assert (
            len(json.dumps(page, ensure_ascii=False))
            <= host.runtime.config.settings.limits.max_output_chars
        )
        pages.append(page["content"])
        offset = page["next_offset_bytes"]
        if not page["has_more"]:
            break
        page = await host.drive(
            "document_read", id=item["id"], revision=page["revision"], offset_bytes=offset
        )
    else:
        raise AssertionError("finite extracted text did not reach EOF")
    assert "".join(pages) == text + "\n"
    assert offset == page["total_bytes"] == len((text + "\n").encode("utf-8"))
    eof = await host.drive(
        "document_read", id=item["id"], revision=page["revision"], offset_bytes=offset
    )
    assert eof["content"] == "" and not eof["has_more"]
    assert host.requests == []
    assert host.attachments.path(item["id"]).read_bytes() == raw


async def test_main_assigns_document_to_a_real_teammate_for_complete_reading(host: Host) -> None:
    runtime = host.runtime
    runtime.config.update(
        {
            "models": {
                "worker": {"model": "document-worker", "base_url": "https://unused.invalid/v1"}
            }
        }
    )
    await runtime.reconfigure()
    text = "队友原文头部" + "中文交接资料🙂" * 100 + "队友原文末条"
    item = await host.save(docx(text), "队友资料.docx")
    pages: list[dict[str, Any]] = []
    ids: list[str] = []
    main_turns = [
        ModelTurn(
            tool_calls=[
                call(
                    "document-spawn",
                    "spawn_agent",
                    task_name="文档队友",
                    prompt="读完附件正文并报告。",
                    attachment_ids=[item["id"]],
                )
            ]
        ),
        ModelTurn(
            tool_calls=[call("document-wait", "wait_agents", targets=["文档队友"], timeout=10)]
        ),
        ModelTurn(content="已收到文档队友报告。"),
    ]

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            agent = runtime.team.get("文档队友")
            assert runtime.store.get("session", agent["id"])["role"] == "worker"
            assert any(tool["function"]["name"] == "document_read" for tool in tools)
            assert len(ids) < 12
            arguments: dict[str, Any] = {"id": item["id"]}
            if ids:
                page = tool_result(messages, ids[-1])
                assert page["source_revision"] == item["sha256"]
                assert page["offset_bytes"] == (pages[-1]["next_offset_bytes"] if pages else 0)
                if pages:
                    assert page["revision"] == pages[0]["revision"]
                pages.append(page)
                if not page["has_more"]:
                    yield StreamEvent("done", turn=ModelTurn(content="已核对到原文末条。"))
                    return
                arguments.update(revision=page["revision"], offset_bytes=page["next_offset_bytes"])
            else:
                assert item["id"] in str(messages) and "队友原文头部" not in str(messages)
            identifier = f"document-worker-page-{len(ids)}"
            ids.append(identifier)
            yield StreamEvent(
                "done", turn=ModelTurn(tool_calls=[call(identifier, "document_read", **arguments)])
            )

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if not self.turns:
                self.turns.append(ModelTurn(content="已收到文档队友报告。"))
            async for event in super().stream(messages, tools):
                yield event

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], []) if config.model == "document-worker" else Main(main_turns, host.seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    submitted = await runtime.submit(
        "让文档队友阅读附件。", attachment_ids=[item["id"]], mode="yolo"
    )
    await asyncio.wait_for(runtime.wait(submitted["run_id"]), 4)
    await eventually(
        runtime,
        lambda: (
            runtime.team.get("文档队友")["status"]
            in {"completed", "failed", "cancelled", "interrupted"}
        ),
    )
    agent = runtime.team.get("文档队友")
    assert agent["status"] == "completed" and agent["result"] == "已核对到原文末条。"
    assert agent["parent_id"] == runtime.main_session()["id"]
    assert "".join(page["content"] for page in pages) == text + "\n"
    assert len(pages) == len(ids) > 1
    events = [
        event
        for event in runtime.store.events()
        if event["type"] == "tool.started" and event["data"].get("call_id") in ids
    ]
    assert len(events) == len(ids) and all(event["session_id"] == agent["id"] for event in events)
    assert host.requests == []


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"revision": "0" * 64}, "revision_changed"),
        ({"offset_bytes": 1}, "invalid_arguments"),
        ({"offset_bytes": True}, "invalid_arguments"),
        ({"revision": "invalid"}, "invalid_arguments"),
    ],
)
async def test_document_rejects_invalid_coordinates(
    host: Host, arguments: dict[str, Any], code: str
) -> None:
    item = await host.save(docx("中文正文"), "read.docx")
    with pytest.raises(EvidenceError) as caught:
        await host.call("document_read", id=item["id"], **arguments)
    assert caught.value.code == code


async def test_document_checks_utf8_offset_source_revision_and_raw_file(host: Host) -> None:
    item = await host.save(docx("中文正文" * 100), "read.docx")
    first = await host.call("document_read", id=item["id"])
    with pytest.raises(EvidenceError) as caught:
        await host.call("document_read", id=item["id"], revision=first["revision"], offset_bytes=1)
    assert caught.value.code == "invalid_offset"
    host.attachments.path(item["id"]).write_bytes(b"changed")
    with pytest.raises(EvidenceError) as caught:
        await host.call(
            "document_read",
            id=item["id"],
            revision=first["revision"],
            offset_bytes=first["next_offset_bytes"],
        )
    assert caught.value.code == "document_changed"


@pytest.mark.parametrize("source", ["asr", "vision"])
async def test_document_does_not_reinterpret_specialized_archives(host: Host, source: str) -> None:
    item = await host.save(docx("正文"), "derived.docx", source=source)
    with pytest.raises(EvidenceError) as caught:
        await host.call("document_read", id=item["id"])
    assert caught.value.code == "document_unavailable"


async def test_document_rechecks_configuration_after_the_real_parse_thread(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.document_reading as module

    item = await host.save(docx("正文"), "read.docx")
    entered, release = threading.Event(), threading.Event()
    original = module.extract_docx

    def held(data: bytes, **kwargs: Any) -> bytes:
        entered.set()
        assert release.wait(2)
        return original(data, **kwargs)

    monkeypatch.setattr(module, "extract_docx", held)
    task = asyncio.create_task(host.call("document_read", id=item["id"]))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        configure_document(host, {"max_xml_depth": 64})
        release.set()
        with pytest.raises(EvidenceError) as caught:
            await task
        assert caught.value.code == "document_changed"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def pdf_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Host]:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("document reading must remain offline")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    turns: list[ModelTurn] = []
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update(
        {
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "updates": {"check_on_start": False},
            "limits": {"max_output_chars": 1000},
        }
    )
    attachments = Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    services = LocalServices(runtime, attachments)
    try:
        yield Host(runtime, attachments, services, "", turns, seen)
    finally:
        await runtime.close()


def pdf() -> bytes:
    stream = b"BT /F1 12 Tf 10 50 Td (MULI DOCUMENT END) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, data in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode() + data + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(result)


@pytest.fixture
def installed_pdf_diagnostic(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import muli.document_reading as module

    diagnostic: dict[str, Any] = {}
    original = module.run_process

    async def observed(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        diagnostic["resolved_path"] = shutil.which(argv[0])
        result = await original(argv, **kwargs)
        diagnostic.update(exit_code=result["exit_code"], stderr=result["stderr"][:2048])
        return result

    monkeypatch.setattr(module, "run_process", observed)
    return diagnostic


async def test_real_installed_pdf_converter_reads_generated_document(
    pdf_host: Host, installed_pdf_diagnostic: dict[str, Any]
) -> None:
    converter = shutil.which("pdftotext")
    if converter is None:
        pytest.skip("optional local pdftotext is not installed; never downloaded by this test")
    configure_document(pdf_host, {"pdf_command": [converter]})
    item = await pdf_host.save(pdf(), "sample.pdf")
    result = await pdf_host.drive("document_read", id=item["id"])
    assert "error" not in result, {"result": result, **installed_pdf_diagnostic}
    assert result["format"] == "pdf" and result["scope"] == "pdf_text_layer"
    assert result["source_revision"] == item["sha256"]
    assert "MULI DOCUMENT END" in result["content"] and not result["has_more"]


async def test_pdf_converter_gets_only_verified_stdin_bytes(pdf_host: Host) -> None:
    raw = pdf()
    command = (
        "import sys; data=sys.stdin.buffer.read(); "
        f"assert data.hex()=={raw.hex()!r}; "
        'assert sys.argv[1:]==["-enc","UTF-8","-eol","unix","-","-"]; '
        'sys.stdout.buffer.write("本地PDF正文🙂".encode())'
    )
    configure_document(pdf_host, {"pdf_command": [sys.executable, "-c", command]})
    item = await pdf_host.save(raw, "sample.pdf")
    result = await pdf_host.drive("document_read", id=item["id"])
    assert result["content"] == "本地PDF正文🙂"


@pytest.mark.parametrize(
    "command,code",
    [
        (["not-installed-muli-document-converter"], "document_converter_unavailable"),
        ([sys.executable, "-c", "import os; os.write(1, bytes([255]))"], "document_encoding"),
        ([sys.executable, "-c", "raise SystemExit(7)"], "document_conversion_failed"),
        ([sys.executable, "-c", 'print("x"*2048)'], "document_budget"),
    ],
)
async def test_pdf_conversion_errors_do_not_become_partial_text(
    pdf_host: Host, command: list[str], code: str
) -> None:
    configure_document(pdf_host, {"pdf_command": command, "max_extracted_bytes": 1024})
    item = await pdf_host.save(pdf(), "sample.pdf")
    with pytest.raises(EvidenceError) as caught:
        await pdf_host.call("document_read", id=item["id"])
    assert caught.value.code == code


@pytest.mark.skipif(os.name != "posix", reason="POSIX owned process reaping assertion")
async def test_pdf_timeout_reaps_actual_owned_converter(pdf_host: Host, tmp_path: Path) -> None:
    marker = tmp_path / "converter.pid"
    command = (
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(5)"
    )
    configure_document(
        pdf_host, {"pdf_command": [sys.executable, "-c", command], "timeout_seconds": 0.5}
    )
    item = await pdf_host.save(pdf(), "sample.pdf")
    with pytest.raises(EvidenceError) as caught:
        await pdf_host.call("document_read", id=item["id"])
    assert caught.value.code == "document_timeout"
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


async def test_pdf_converter_missing_reports_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import muli.document_reading as document_reading
    import muli.system_dependencies as dependencies

    async def missing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise FileNotFoundError

    monkeypatch.setattr(document_reading, "run_process", missing)
    original_hint = dependencies.install_hint
    monkeypatch.setattr(
        dependencies,
        "install_hint",
        lambda tools, *, platform=None: original_hint(tools, platform="linux"),
    )
    monkeypatch.setattr(
        dependencies.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"pacman", "sudo"} else None,
    )
    with pytest.raises(EvidenceError) as caught:
        await document_reading._read_pdf(b"%PDF-1.4", DocumentSettings(), time.monotonic() + 30)
    assert caught.value.code == "document_converter_unavailable"
    assert "poppler" in str(caught.value)
    assert "sudo -n" in str(caught.value)


async def test_pdf_converter_missing_without_sudo_reports_plain_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import muli.document_reading as document_reading
    import muli.system_dependencies as dependencies

    async def missing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise FileNotFoundError

    monkeypatch.setattr(document_reading, "run_process", missing)
    original_hint = dependencies.install_hint
    monkeypatch.setattr(
        dependencies,
        "install_hint",
        lambda tools, *, platform=None: original_hint(tools, platform="linux"),
    )
    monkeypatch.setattr(
        dependencies.shutil,
        "which",
        lambda name: "/usr/bin/pacman" if name == "pacman" else None,
    )
    with pytest.raises(EvidenceError) as caught:
        await document_reading._read_pdf(b"%PDF-1.4", DocumentSettings(), time.monotonic() + 30)
    message = str(caught.value)
    assert "pacman" in message and "poppler" in message
    assert "sudo" not in message
