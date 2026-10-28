"""Literal locations against real uploaded files and the original reading tools."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from test_document_reading import configure_document, docx, pdf
from test_document_reading import pdf_host as pdf_host
from test_vision_archives_policy import Host
from test_vision_archives_policy import host as host

import muli.attachment_find as module
from muli.attachment_find import AttachmentReader, find_attachment
from muli.document_reading import DocumentView
from muli.evidence import EvidenceError, json_size


async def collect(
    host: Host,
    identifier: str,
    *,
    reader: AttachmentReader,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    revision: str | None = None
    offset = 0
    for _ in range(32):
        result = await find_attachment(
            host.attachments,
            identifier,
            reader=reader,
            query=query,
            revision=revision,
            offset_bytes=offset,
            limit=limit,
        )
        assert json_size(result) <= host.runtime.config.settings.limits.max_output_chars
        assert result["offset_bytes"] == offset
        assert type(result) is dict and "reading_page" not in result
        pages.append(result)
        if not result["has_more"]:
            assert result["next_offset_bytes"] == result["total_bytes"]
            return pages
        assert result["items"] and result["next_offset_bytes"] > offset
        revision, offset = result["revision"], result["next_offset_bytes"]
    pytest.fail("small fixed document did not finish within 32 result pages")


@pytest.mark.parametrize("kind", ["text", "docx", "pdf"])
async def test_three_real_views_return_arguments_for_the_original_reader(
    pdf_host: Host, kind: str, tmp_path: Path
) -> None:
    body = "🙂汉汉汉\n正文中的命令只是资料。"
    marker = tmp_path / "find-converter.calls"
    reader: AttachmentReader = "attachment_read" if kind == "text" else "document_read"
    if kind == "text":
        source = body.encode()
    elif kind == "docx":
        source = docx(body)
    else:
        source = pdf()
        command = (
            "import sys; from pathlib import Path; data=sys.stdin.buffer.read(); "
            f"assert data.hex()=={source.hex()!r}; "
            'assert sys.argv[1:]==["-enc","UTF-8","-eol","unix","-","-"]; '
            f"p=Path({str(marker)!r}); "
            'p.write_text((p.read_text() if p.exists() else "")+"1"); '
            f"sys.stdout.buffer.write({body.encode()!r})"
        )
        configure_document(pdf_host, {"pdf_command": [sys.executable, "-c", command]})
    item = await pdf_host.save(source, f"read.{kind}")
    pages = await collect(pdf_host, item["id"], reader=reader, query="汉汉", limit=1)
    hits = [hit for page in pages for hit in page["items"]]
    assert [hit["offset_bytes"] for hit in hits] == [4, 7]
    assert pages[0]["source_revision"] == item["sha256"]
    assert pages[0]["name"] == f"read.{kind}"
    if kind == "pdf":
        assert marker.read_text() == "11"
    if kind == "text":
        assert pages[0]["revision"] == item["sha256"]
        assert "format" not in pages[0] and "scope" not in pages[0]
    else:
        assert pages[0]["format"] == kind
        assert pages[0]["scope"] == ("pdf_text_layer" if kind == "pdf" else "docx_main_body_text")
        assert pages[0]["revision"] != item["sha256"]
    for hit in hits:
        assert hit["length_bytes"] == 6
        assert hit["next_offset_bytes"] == hit["offset_bytes"] + 6
        assert hit["read_arguments"] == {
            "id": item["id"],
            "revision": pages[0]["revision"],
            "offset_bytes": hit["offset_bytes"],
        }
        original = await pdf_host.call(reader, **hit["read_arguments"])
        assert original["content"].startswith("汉汉")
        assert original["revision"] == pages[0]["revision"]
    if kind == "pdf":
        assert marker.read_text() == "1111"
    assert not pdf_host.seen


@pytest.mark.parametrize("limit", [1, 100])
async def test_overlap_pages_and_full_output_budget_never_drop_a_hit(
    host: Host, limit: int
) -> None:
    item = await host.save(("🙂" + "汉" * 9).encode(), "overlap.txt")
    pages = await collect(host, item["id"], reader="attachment_read", query="汉汉", limit=limit)
    assert [hit["offset_bytes"] for page in pages for hit in page["items"]] == [
        4,
        7,
        10,
        13,
        16,
        19,
        22,
        25,
    ]
    if limit == 100:
        assert pages[0]["has_more"] and 1 <= len(pages[0]["items"]) < 8
    for before, after in zip(pages, pages[1:], strict=False):
        assert before["next_offset_bytes"] == after["items"][0]["offset_bytes"]


async def test_ascii_overlaps_survive_a_smaller_current_result_cap(host: Host) -> None:
    host.runtime.config.update({"limits": {"max_search_results": 1}})
    item = await host.save(b"aaaa", "overlap.txt")
    pages = await collect(host, item["id"], reader="attachment_read", query="aa", limit=100)
    assert [hit["offset_bytes"] for page in pages for hit in page["items"]] == [0, 1, 2]
    assert [len(page["items"]) for page in pages] == [1, 1, 1]


@pytest.mark.parametrize(
    ("query", "starts"),
    [
        ("Aa", [0]),
        ("aa", [3]),
        ("aA", [6]),
        ("\n", [8]),
        ("é", [9]),
        ("e\u0301", [12]),
        ("%_", [16]),
        ("a.b", [19]),
        ("AA", [23]),
        ("a.*b", []),
    ],
)
async def test_case_unicode_and_special_characters_are_literal(
    host: Host, query: str, starts: list[int]
) -> None:
    item = await host.save("Aa aa aA\né e\u0301 %_ a.b AAB".encode(), "literal.txt")
    result = await find_attachment(
        host.attachments, item["id"], reader="attachment_read", query=query
    )
    assert [hit["offset_bytes"] for hit in result["items"]] == starts
    assert not result["has_more"]


async def test_match_can_cross_the_real_reader_page_boundary(host: Host) -> None:
    item = await host.save(b"a" * 1800, "boundary.txt")
    first = await host.call("attachment_read", id=item["id"], offset_bytes=0)
    boundary = first["next_offset_bytes"]
    assert 0 < boundary < 1800
    result = await find_attachment(
        host.attachments,
        item["id"],
        reader="attachment_read",
        query="aaaa",
        revision=first["revision"],
        offset_bytes=boundary - 1,
        limit=1,
    )
    hit = result["items"][0]
    assert hit["offset_bytes"] == boundary - 1
    assert hit["next_offset_bytes"] > boundary
    assert result["next_offset_bytes"] == boundary


@pytest.mark.parametrize("body", [b"", b"absent"])
async def test_no_match_and_eof_are_complete_empty_results(host: Host, body: bytes) -> None:
    item = await host.save(body, "empty.txt")
    for offset in [0, len(body)]:
        result = await find_attachment(
            host.attachments,
            item["id"],
            reader="attachment_read",
            query="needle",
            revision=item["sha256"],
            offset_bytes=offset,
        )
        assert result["items"] == [] and not result["has_more"]
        assert result["next_offset_bytes"] == result["total_bytes"] == len(body)


async def test_optional_name_is_omitted_before_losing_the_only_hit(host: Host) -> None:
    query = '"' * 120
    item = await host.save(query.encode(), "n" * 236 + ".txt")
    result = await find_attachment(
        host.attachments, item["id"], reader="attachment_read", query=query
    )
    assert "name" not in result
    assert len(result["items"]) == 1 and result["query"] == query
    assert json_size(result) <= 1000 and not result["has_more"]


async def test_an_escaped_query_that_cannot_fit_never_returns_a_success_cursor(host: Host) -> None:
    query = "\x01" * 120
    item = await host.save(query.encode(), "budget.txt")
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(host.attachments, item["id"], reader="attachment_read", query=query)
    assert caught.value.code == "output_budget"


@pytest.mark.parametrize("name", [None, "x" * 241, "../rename.txt", "bad\x00name", "\ud800"])
async def test_corrupt_display_names_do_not_replace_the_exact_attachment(
    host: Host, name: object
) -> None:
    item = await host.save(b"needle", "valid.txt")
    damaged = {**host.attachments.get(item["id"]), "name": name}
    with host.runtime.store.db:
        host.runtime.store.db.execute(
            "UPDATE objects SET data=? WHERE kind='attachment' AND id=?",
            (json.dumps(damaged, ensure_ascii=True), item["id"]),
        )
    result = await find_attachment(
        host.attachments, item["id"], reader="attachment_read", query="needle"
    )
    assert "name" not in result and result["id"] == item["id"]
    assert result["items"][0]["read_arguments"]["id"] == item["id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "../outside"},
        {"reader": "auto"},
        {"query": ""},
        {"query": "\x00"},
        {"query": "\ud800"},
        {"query": "a" * 121},
        {"query": True},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"offset_bytes": True},
        {"offset_bytes": -1},
        {"offset_bytes": 1},
        {"offset_bytes": (1 << 63) - 1},
        {"revision": "F" * 64},
    ],
)
async def test_invalid_arguments_do_not_produce_locations(
    host: Host, changes: dict[str, Any]
) -> None:
    item = await host.save("🙂needle".encode(), "args.txt")
    arguments: dict[str, Any] = {
        "identifier": item["id"],
        "reader": "attachment_read",
        "query": "needle",
    }
    changes = dict(changes)
    if "id" in changes:
        changes["identifier"] = changes.pop("id")
    arguments.update(changes)
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(host.attachments, **arguments)
    assert caught.value.code == "invalid_arguments"


async def test_query_bound_is_unicode_characters_and_offsets_are_utf8_boundaries(
    host: Host,
) -> None:
    query = "🙂" * 120
    item = await host.save(query.encode(), "utf8.txt")
    result = await find_attachment(
        host.attachments, item["id"], reader="attachment_read", query=query
    )
    assert result["items"][0]["length_bytes"] == 480
    for offset in [1, len(query.encode()) + 1]:
        with pytest.raises(EvidenceError) as caught:
            await find_attachment(
                host.attachments,
                item["id"],
                reader="attachment_read",
                query="🙂",
                revision=item["sha256"],
                offset_bytes=offset,
            )
        assert caught.value.code == "invalid_offset"


@pytest.mark.parametrize("reader", ["attachment_read", "document_read"])
@pytest.mark.parametrize("source", ["asr", "vision"])
async def test_specialized_archives_are_not_reinterpreted(
    host: Host, reader: AttachmentReader, source: str
) -> None:
    item = await host.save(b"needle", "derived.txt", source=source)
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(host.attachments, item["id"], reader=reader, query="needle")
    assert caught.value.code == (
        "attachment_unavailable" if reader == "attachment_read" else "document_unavailable"
    )


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        (b"\xff", "attachment_encoding_error"),
        (b"\xf0", "attachment_encoding_error"),
        (b"\x00", "attachment_binary_data"),
    ],
)
async def test_early_hits_do_not_hide_invalid_text_at_the_real_file_tail(
    host: Host, tail: bytes, code: str
) -> None:
    item = await host.save(b"needle" + b"a" * 65536 + tail, "invalid-tail.txt")
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(
            host.attachments, item["id"], reader="attachment_read", query="needle"
        )
    assert caught.value.code == code


async def test_same_extracted_text_in_different_source_files_has_distinct_revisions(
    host: Host,
) -> None:
    first = await host.save(docx("needle", target="one.xml"), "first.docx")
    second = await host.save(docx("needle", target="two.xml"), "second.docx")
    left = await find_attachment(
        host.attachments, first["id"], reader="document_read", query="needle"
    )
    right = await find_attachment(
        host.attachments, second["id"], reader="document_read", query="needle"
    )
    assert left["source_revision"] != right["source_revision"]
    assert left["revision"] != right["revision"]
    for result in [left, right]:
        original = await host.call("document_read", **result["items"][0]["read_arguments"])
        assert original["content"] == "needle\n"
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(
            host.attachments,
            second["id"],
            reader="document_read",
            query="needle",
            revision=left["revision"],
        )
    assert caught.value.code == "revision_changed"


async def test_identical_files_keep_each_read_argument_bound_to_its_own_id(host: Host) -> None:
    first = await host.save(b"needle", "first.txt")
    second = await host.save(b"needle", "second.txt")
    assert first["sha256"] == second["sha256"]
    for item in [first, second]:
        result = await find_attachment(
            host.attachments, item["id"], reader="attachment_read", query="needle"
        )
        assert result["id"] == item["id"]
        assert result["items"][0]["read_arguments"]["id"] == item["id"]


async def test_each_find_extracts_once_and_a_later_reader_really_extracts_again(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muli.document_reading as documents

    original = documents.extract_docx
    calls = 0

    def counted(data: bytes, **arguments: Any) -> bytes:
        nonlocal calls
        calls += 1
        return original(data, **arguments)

    monkeypatch.setattr(documents, "extract_docx", counted)
    item = await host.save(docx("needle needle needle"), "counts.docx")
    first = await find_attachment(
        host.attachments, item["id"], reader="document_read", query="needle", limit=1
    )
    assert calls == 1 and first["has_more"]
    second = await find_attachment(
        host.attachments,
        item["id"],
        reader="document_read",
        query="needle",
        revision=first["revision"],
        offset_bytes=first["next_offset_bytes"],
        limit=1,
    )
    assert calls == 2 and second["items"][0]["offset_bytes"] == 7
    original_page = await host.call("document_read", **second["items"][0]["read_arguments"])
    assert calls == 3 and original_page["content"].startswith("needle")


@pytest.mark.parametrize("change", ["metadata", "delete", "file_limit", "output", "results"])
async def test_text_rechecks_current_metadata_and_limits_after_actual_verified_io(
    host: Host, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    host.runtime.config.update({"limits": {"max_output_chars": 4000}})
    item = await host.save(b"needle " * 200, "held.txt")
    entered, release = threading.Event(), threading.Event()
    original = module.read_verified_window

    def held(path: Path, **arguments: Any) -> bytes:
        result = original(path, **arguments)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(module, "read_verified_window", held)
    task = asyncio.create_task(
        find_attachment(host.attachments, item["id"], reader="attachment_read", query="needle")
    )
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        if change == "metadata":
            host.runtime.store.put(
                "attachment", {**host.attachments.get(item["id"]), "name": "changed.txt"}
            )
        elif change == "delete":
            host.runtime.store.delete("attachment", item["id"])
        elif change == "file_limit":
            host.runtime.config.update({"limits": {"max_file_bytes": 1024}})
        elif change == "output":
            host.runtime.config.update({"limits": {"max_output_chars": 1000}})
        else:
            host.runtime.config.update({"limits": {"max_search_results": 1}})
        release.set()
        if change in ("metadata", "delete", "file_limit"):
            with pytest.raises(EvidenceError) as caught:
                await task
            assert caught.value.code == "attachment_changed"
        else:
            result = await task
            assert result["has_more"]
            if change == "output":
                assert json_size(result) <= 1000
            else:
                assert len(result["items"]) == 1 and result["next_offset_bytes"] == 7
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["metadata", "document_policy"])
async def test_outer_document_await_does_not_authorize_a_stale_verified_view(
    host: Host, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    item = await host.save(docx("needle"), "held.docx")
    entered, release = asyncio.Event(), asyncio.Event()
    original = module.document_view

    async def held(*arguments: Any, **keywords: Any) -> DocumentView:
        result = await original(*arguments, **keywords)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(module, "document_view", held)
    task = asyncio.create_task(
        find_attachment(host.attachments, item["id"], reader="document_read", query="needle")
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "document_policy":
            configure_document(host, {"max_xml_depth": 64})
        else:
            host.runtime.store.put(
                "attachment", {**host.attachments.get(item["id"]), "name": "changed.docx"}
            )
        release.set()
        with pytest.raises(EvidenceError) as caught:
            await task
        assert caught.value.code == "document_changed"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_same_size_file_replacement_is_not_a_verified_match(host: Host) -> None:
    item = await host.save(b"needle remaining", "changed.txt")
    host.attachments.path(item["id"]).write_bytes(b"NEEDLE remaining")
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(
            host.attachments, item["id"], reader="attachment_read", query="needle"
        )
    assert caught.value.code == "attachment_changed"


@pytest.mark.parametrize("change", ["same_size_write", "same_bytes_new_inode"])
async def test_real_source_changes_during_the_actual_docx_extraction_are_rejected(
    host: Host, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import muli.document_reading as documents

    source = docx("needle")
    item = await host.save(source, "replace.docx")
    path = host.attachments.path(item["id"])
    original_inode = path.stat().st_ino
    entered, release = threading.Event(), threading.Event()
    original = documents.extract_docx
    calls = 0

    def held(data: bytes, **arguments: Any) -> bytes:
        nonlocal calls
        calls += 1
        result = original(data, **arguments)
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(documents, "extract_docx", held)
    task = asyncio.create_task(
        find_attachment(host.attachments, item["id"], reader="document_read", query="needle")
    )
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        if change == "same_size_write":
            changed = bytearray(source)
            changed[-1] ^= 1
            path.write_bytes(changed)
            assert path.stat().st_ino == original_inode
            assert path.stat().st_size == len(source)
        else:
            replacement = path.with_name(path.name + ".replacement")
            replacement.write_bytes(source)
            replacement.replace(path)
            assert path.stat().st_ino != original_inode
            assert path.read_bytes() == source
        assert host.attachments.get(item["id"])["sha256"] == item["sha256"]
        release.set()
        with pytest.raises(EvidenceError) as caught:
            await task
        assert caught.value.code == "document_changed" and calls == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_search_deadline_fails_without_claiming_no_matches(host: Host) -> None:
    item = await host.save(b"needle", "deadline.txt")
    host.runtime.config.update({"limits": {"max_search_seconds": 1e-12}})
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(
            host.attachments, item["id"], reader="attachment_read", query="absent"
        )
    assert caught.value.code == "attachment_find_timeout"


async def test_empty_pdf_text_layer_keeps_its_actual_scope(pdf_host: Host) -> None:
    configure_document(
        pdf_host,
        {"pdf_command": [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]},
    )
    item = await pdf_host.save(pdf(), "image-only.pdf")
    result = await find_attachment(
        pdf_host.attachments, item["id"], reader="document_read", query="needle"
    )
    assert result["items"] == [] and result["total_bytes"] == 0
    assert not result["has_more"] and result["next_offset_bytes"] == 0
    assert result["format"] == "pdf" and result["scope"] == "pdf_text_layer"


async def test_find_preserves_the_actual_pdf_conversion_deadline(pdf_host: Host) -> None:
    configure_document(
        pdf_host,
        {
            "pdf_command": [sys.executable, "-c", "import time; time.sleep(5)"],
            "timeout_seconds": 0.1,
        },
    )
    item = await pdf_host.save(pdf(), "deadline.pdf")
    with pytest.raises(EvidenceError) as caught:
        await find_attachment(
            pdf_host.attachments, item["id"], reader="document_read", query="absent"
        )
    assert caught.value.code == "document_timeout"


async def test_cancel_waits_for_owned_verified_file_io_to_settle(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = await host.save(b"needle", "cancel.txt")
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    original = module.read_verified_window

    def held(path: Path, **arguments: Any) -> bytes:
        entered.set()
        assert release.wait(3)
        try:
            return original(path, **arguments)
        finally:
            settled.set()

    monkeypatch.setattr(module, "read_verified_window", held)
    task = asyncio.create_task(
        find_attachment(host.attachments, item["id"], reader="attachment_read", query="needle")
    )
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not settled.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert settled.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX owned process reaping assertion")
async def test_cancel_reaps_the_real_pdf_converter(pdf_host: Host, tmp_path: Path) -> None:
    source = pdf()
    marker = tmp_path / "find-converter.pid"
    command = (
        "import os,sys,time; from pathlib import Path; "
        f"assert sys.stdin.buffer.read().hex()=={source.hex()!r}; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(5)"
    )
    configure_document(pdf_host, {"pdf_command": [sys.executable, "-c", command]})
    item = await pdf_host.save(source, "cancel.pdf")
    task = asyncio.create_task(
        find_attachment(pdf_host.attachments, item["id"], reader="document_read", query="needle")
    )
    try:
        async with asyncio.timeout(3):
            while not marker.exists():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
