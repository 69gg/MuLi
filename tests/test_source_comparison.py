"""Literal retained-source changes, authenticated pages and owned worker cleanup."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import muli.source_comparison as comparison
from muli.attachment_memory_source import attachment_source
from muli.document_reading import document_revision
from muli.evidence import EvidenceError, json_size
from muli.memory import Memory
from muli.memory_evidence import MemoryEvidence
from muli.source_comparison import ComparisonPolicy, compare_sources
from muli.store import Store

POLICY = ComparisonPolicy(
    max_source_chars=64000,
    max_input_bytes=2_000_000,
    max_lines=2000,
    max_output_chars=24000,
    timeout_seconds=5.0,
)


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    store = Store(tmp_path / "comparison.db")
    try:
        yield Memory(store)
    finally:
        store.close()


def save(
    memory: Memory, identifier: str, content: str, *, format_name: str = "text"
) -> dict[str, Any]:
    raw = content.encode("utf-8")
    document: dict[str, str] | None = None
    if format_name == "text":
        checksum = hashlib.sha256(raw).hexdigest()
    else:
        checksum = hashlib.sha256(("original-binary:" + identifier).encode()).hexdigest()
        document = {
            "format": format_name,
            "scope": {"docx": "docx_main_body_text", "pdf": "pdf_text_layer"}[format_name],
            "source_revision": checksum,
            "revision": document_revision(checksum, raw),
        }
    source, created = memory.register_source(
        attachment_source(identifier, content, checksum, 7.0, document=document)
    )
    assert source is not None and created
    return source


async def compare(
    memory: Memory, left: dict[str, Any], right: dict[str, Any], **options: Any
) -> dict[str, Any]:
    return await compare_sources(
        memory,
        left_id=left["id"],
        left_revision=left["digest"],
        right_id=right["id"],
        right_revision=right["digest"],
        policy=options.pop("policy", POLICY),
        **options,
    )


def assert_original_spans(memory: Memory, page: dict[str, Any], originals: dict[str, str]) -> None:
    for item in page["items"]:
        for name in ("left", "right"):
            side = item[name]
            raw = originals[name].encode("utf-8")
            start, length = side["offset_bytes"], side["length_bytes"]
            assert raw[start : start + length].decode("utf-8") == side["content"]
            assert side["read_arguments"]["revision"] == page[name]["revision"]
            if length:
                read = MemoryEvidence(memory).read(
                    side["read_arguments"]["id"],
                    by_memory=False,
                    revision=side["read_arguments"]["revision"],
                    offset_bytes=start,
                    length_bytes=length,
                    max_output_chars=POLICY.max_output_chars,
                )
                assert read["content"] == side["content"]
            else:
                assert side["content"] == "" and "length_bytes" not in side["read_arguments"]


async def test_unicode_insert_delete_replace_reproduce_both_originals(memory: Memory) -> None:
    texts = {
        "left": "相同开头🙂\n旧期限七日\n锚点甲\n删除这条\n锚点乙\n结尾",
        "right": "相同开头🙂\n新期限三日\n锚点甲\n锚点乙\n新增要求\n结尾",
    }
    left, right = save(memory, "a" * 32, texts["left"]), save(memory, "b" * 32, texts["right"])
    changes, revision = memory.store.db.total_changes, memory.revision
    page = await compare(memory, left, right)
    assert page["status"] == "changed" and not page["has_more"]
    assert page["next_cursor"] is None
    assert {item["kind"] for item in page["items"]} == {"insert", "delete", "replace"}
    assert_original_spans(memory, page, texts)
    assert page["left"]["source_id"] == left["id"]
    assert page["left"]["format"] == "text" and page["left"]["scope"] == "utf8_text"
    assert page["left"]["document_revision"] is None
    assert not page["left"]["trusted_owner"]
    assert memory.store.db.total_changes == changes and memory.revision == revision


async def test_identical_retained_text_survives_reopen_without_original_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reopen.db"
    store = Store(path)
    memory = Memory(store)
    left = save(memory, "a" * 32, "相同\r\n保留末行🙂")
    right = save(memory, "b" * 32, "相同\r\n保留末行🙂")
    store.close()
    store = Store(path)
    try:
        page = await compare(Memory(store), left, right)
        assert page["status"] == "equal" and page["items"] == []
        assert page["has_more"] is False and page["next_cursor"] is None
    finally:
        store.close()


async def test_document_extraction_and_memory_revisions_are_not_interchangeable(
    memory: Memory,
) -> None:
    left = save(memory, "a" * 32, "相同\n旧文字🙂\n", format_name="docx")
    right = save(memory, "b" * 32, "相同\n新文字🙂\n", format_name="pdf")
    page = await compare(memory, left, right)
    for name, source, scope in (
        ("left", left, "docx_main_body_text"),
        ("right", right, "pdf_text_layer"),
    ):
        identity = page[name]
        assert identity["revision"] == source["digest"]
        assert identity["scope"] == scope
        assert identity["document_revision"] in source["source"]
        assert identity["source_revision"] in source["source"]
        assert (
            len({identity["revision"], identity["source_revision"], identity["document_revision"]})
            == 3
        )
    with pytest.raises(EvidenceError) as caught:
        await compare(memory, {**left, "digest": page["left"]["document_revision"]}, right)
    assert caught.value.code == "evidence_changed"


def changed_texts() -> tuple[str, str]:
    left = "开头\n" + "".join(f"锚点{index}\n旧{index}🙂\n" for index in range(16)) + "结尾"
    right = "开头\n" + "".join(f"锚点{index}\n新{index}🙂\n" for index in range(16)) + "结尾"
    return left, right


async def test_budgeted_pages_return_every_changed_span_once(memory: Memory) -> None:
    text_left, text_right = changed_texts()
    left, right = save(memory, "a" * 32, text_left), save(memory, "b" * 32, text_right)
    policy = replace(POLICY, max_output_chars=4000)
    cursor: str | None = None
    seen: list[str] = []
    for _ in range(32):
        page = await compare(memory, left, right, policy=policy, cursor=cursor)
        assert json_size(page) <= policy.max_output_chars
        assert page["status"] == "changed" and page["items"]
        assert_original_spans(memory, page, {"left": text_left, "right": text_right})
        seen.extend(item["left"]["content"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    else:
        pytest.fail("finite comparison did not complete")
    assert seen == [f"旧{index}🙂\n" for index in range(16)]


async def test_cursor_cannot_skip_changes_reverse_pair_or_change_policy(
    memory: Memory, tmp_path: Path
) -> None:
    text_left, text_right = changed_texts()
    left, right = save(memory, "a" * 32, text_left), save(memory, "b" * 32, text_right)
    policy = replace(POLICY, max_output_chars=4000)
    first = await compare(memory, left, right, policy=policy)
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    decoded = base64.urlsafe_b64decode(cursor)
    signature_size = hashlib.sha256().digest_size
    altered = json.loads(decoded[signature_size:])
    altered["position"] = 1000
    forged = base64.urlsafe_b64encode(
        decoded[:signature_size] + json.dumps(altered).encode()
    ).decode()
    for one, two, selected, token in (
        (left, right, policy, forged),
        (right, left, policy, cursor),
        (left, right, replace(policy, max_lines=1999), cursor),
    ):
        with pytest.raises(EvidenceError) as caught:
            await compare(memory, one, two, policy=selected, cursor=token)
        assert caught.value.code == "comparison_cursor_invalid"
    other = Store(tmp_path / "other.db")
    try:
        second_memory = Memory(other)
        one = save(second_memory, "a" * 32, text_left)
        two = save(second_memory, "b" * 32, text_right)
        with pytest.raises(EvidenceError) as caught:
            await compare(second_memory, one, two, policy=policy, cursor=cursor)
        assert caught.value.code == "comparison_cursor_invalid"
    finally:
        other.close()


@pytest.mark.parametrize("damage", ["content", "digest", "principal", "marker", "metadata"])
async def test_saved_provenance_cannot_be_substituted(memory: Memory, damage: str) -> None:
    left, right = save(memory, "a" * 32, "原文\n"), save(memory, "b" * 32, "修改稿\n")
    updates = {
        "content": ("content", "被改动正文"),
        "digest": ("digest", "f" * 64),
        "principal": ("principal", "owner"),
        "marker": ("source", left["source"].replace("a" * 32, "c" * 32)),
        "metadata": ("source", "x" * 100_000),
    }
    column, value = updates[damage]
    with memory.store.db:
        memory.store.db.execute(
            f"UPDATE memory_sources SET {column}=? WHERE id=?", (value, left["id"])
        )
    with pytest.raises(EvidenceError) as caught:
        await compare(memory, left, right)
    assert caught.value.code in {"comparison_source_invalid", "evidence_changed"}


async def test_sql_gates_apply_before_loading_bodies_and_no_source_fallback(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    left, right = save(memory, "a" * 32, "完整正文" * 100), save(memory, "b" * 32, "修改稿")

    def forbid(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("comparison must not call unbounded source()")

    async def no_worker(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("oversized source must be rejected before starting a worker")

    monkeypatch.setattr(memory, "source", forbid)
    monkeypatch.setattr(comparison, "run_process", no_worker)
    statements: list[str] = []
    memory.store.db.set_trace_callback(statements.append)
    for policy in (replace(POLICY, max_source_chars=10), replace(POLICY, max_input_bytes=10)):
        with pytest.raises(EvidenceError) as caught:
            await compare(memory, left, right, policy=policy)
        assert caught.value.code == "comparison_input_budget"
    assert any("CASE WHEN typeof(s.content)='text'" in sql for sql in statements)
    assert not any("SELECT * FROM memory_sources" in sql for sql in statements)


@pytest.mark.parametrize("case", ["lines", "block"])
async def test_excessive_line_count_or_single_block_fails_without_omitting_changes(
    memory: Memory, case: str
) -> None:
    if case == "lines":
        left, right = save(memory, "a" * 32, "a\n" * 20), save(memory, "b" * 32, "b\n" * 20)
        policy, code = replace(POLICY, max_lines=10), "comparison_line_budget"
    else:
        left = save(memory, "a" * 32, "旧" * 5000)
        right = save(memory, "b" * 32, "新" * 5000)
        policy, code = replace(POLICY, max_output_chars=4000), "comparison_block_budget"
    with pytest.raises(EvidenceError) as caught:
        await compare(memory, left, right, policy=policy)
    assert caught.value.code == code


@pytest.mark.parametrize("change", ["forget", "body", "policy"])
async def test_await_fence_rejects_forgotten_changed_or_reconfigured_sources(
    memory: Memory, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    left, right = save(memory, "a" * 32, "旧\n"), save(memory, "b" * 32, "新\n")
    original = comparison.run_process
    changed = False

    async def intervene(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal changed
        result = await original(*args, **kwargs)
        if change == "forget":
            assert memory.forget_source(left["id"])
        elif change == "body":
            with memory.store.db:
                memory.store.db.execute(
                    "UPDATE memory_sources SET content='替换' WHERE id=?", (left["id"],)
                )
        changed = True
        return result

    def check() -> None:
        if change == "policy" and changed:
            raise EvidenceError("comparison_policy_changed", "策略已经改变")

    monkeypatch.setattr(comparison, "run_process", intervene)
    with pytest.raises(EvidenceError) as caught:
        await compare(memory, left, right, check=check)
    assert (
        caught.value.code
        == {
            "forget": "evidence_not_found",
            "body": "comparison_source_invalid",
            "policy": "comparison_policy_changed",
        }[change]
    )


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "output"])
async def test_worker_deadline_cancel_and_output_flood_are_reaped(
    memory: Memory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    left, right = save(memory, "a" * 32, "旧\n"), save(memory, "b" * 32, "新\n")
    worker = tmp_path / "controlled_worker.py"
    flood = "sys.stdout.write('x' * 100000); sys.stdout.flush();\n" if outcome == "output" else ""
    worker.write_text("import sys, time\nsys.stdin.buffer.read()\n" + flood + "time.sleep(30)\n")
    monkeypatch.setattr(comparison, "_WORKER", worker)
    created = asyncio.Event()
    children: list[asyncio.subprocess.Process] = []
    start = asyncio.create_subprocess_exec

    async def record(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await start(*args, **kwargs)
        children.append(process)
        created.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    task = asyncio.create_task(
        compare(
            memory,
            left,
            right,
            policy=replace(
                POLICY,
                timeout_seconds=POLICY.timeout_seconds if outcome == "output" else 0.1,
                max_output_chars=4000,
            ),
        )
    )
    if outcome == "cancel":
        await asyncio.wait_for(created.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(EvidenceError) as caught:
            await task
        assert caught.value.code == (
            "comparison_timeout" if outcome == "timeout" else "comparison_worker_budget"
        )
    assert children and all(process.returncode is not None for process in children)
