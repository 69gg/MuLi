from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest
from test_audio_transcripts import Host
from test_audio_transcripts import host as host

from muli.attachment_records import AttachmentRecords
from muli.attachments import Attachments
from muli.evidence import EvidenceError, json_size
from muli.providers import ModelTurn, ToolCall
from muli.store import Store
from muli.transcripts import AudioTranscripts


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    journal = Store(tmp_path / "attachments.sqlite3")
    try:
        yield journal
    finally:
        journal.close()


def save(store: Store, number: int, **fields: Any) -> dict[str, Any]:
    return store.put(
        "attachment",
        {
            "id": f"{number:032x}",
            "name": f"附件-{number}.txt",
            "source": "web",
            "mime_type": "text/plain",
            "size": 1,
            "sha256": "a" * 64,
            "created_at": 1.0,
            **fields,
        },
    )


def pages(reader: AttachmentRecords, **arguments: Any) -> list[dict[str, Any]]:
    cursor: str | None = None
    result: list[dict[str, Any]] = []
    budget = arguments.pop("max_output_chars", 4000)
    for _ in range(100):
        page = reader.read(cursor=cursor, max_output_chars=budget, **arguments)
        assert set(page) == {"items", "next_cursor", "has_more", "scanned"}
        assert json_size(page) <= budget and 0 <= page["scanned"] <= 200
        result.append(page)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            return result
        assert page["scanned"] > 0 and page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    pytest.fail("附件目录没有通过单调游标到达末尾")


def test_rare_search_advances_an_empty_bounded_window_without_legacy_list_reads(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    save(store, 1, source="asr", name="转写-original-audio.txt")
    for number in range(2, 213):
        save(store, number)
    with store.db:
        store.db.execute(
            "UPDATE objects SET updated=0 WHERE kind='attachment' AND id=?", (f"{1:032x}",)
        )
        store.db.execute(
            "UPDATE objects SET updated=1 WHERE kind='attachment' AND id<>?", (f"{1:032x}",)
        )
    assert f"{1:032x}" not in {item["id"] for item in store.list("attachment")}

    def legacy_read(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("search must seek bounded metadata, not materialize the legacy list")

    monkeypatch.setattr(store, "list", legacy_read)
    monkeypatch.setattr(store, "get", legacy_read)
    before = store.db.total_changes
    schema = list(store.db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name"))
    result = pages(AttachmentRecords(store), source="asr", name_query="original-audio")
    assert result[0]["items"] == [] and result[0]["scanned"] == 200 and result[0]["has_more"]
    assert result[1]["scanned"] == 12 and not result[1]["has_more"]
    assert [item["id"] for page in result for item in page["items"]] == [f"{1:032x}"]
    assert store.db.total_changes == before
    assert list(store.db.execute("SELECT name,sql FROM sqlite_schema ORDER BY name")) == schema


@pytest.mark.parametrize("budget", [1000, 1700, 8000])
def test_final_json_budget_preserves_complete_escaped_metadata_and_whitelist(
    store: Store, budget: int
) -> None:
    name = '中文🙂"' * 15
    for number in range(1, 26):
        save(store, number, name=name, private_note="not a public directory field")
    result = pages(AttachmentRecords(store), max_output_chars=budget, limit=7)
    items = [item for page in result for item in page["items"]]
    assert [item["id"] for item in items] == [f"{number:032x}" for number in range(25, 0, -1)]
    assert all(item["name"] == name for item in items)
    assert all(
        set(item) == {"id", "name", "source", "mime_type", "size", "sha256", "created_at", "url"}
        for item in items
    )
    assert items[0]["url"] == f"/api/uploads/{items[0]['id']}/download"


def test_deleted_anchor_inserts_on_both_sides_and_updated_changes_never_repeat_ids(
    store: Store,
) -> None:
    for number in (900, 700, 500, 300):
        save(store, number)
    reader = AttachmentRecords(store)
    first = reader.read(limit=2, max_output_chars=4000)
    assert [item["id"] for item in first["items"]] == [f"{900:032x}", f"{700:032x}"]
    store.delete("attachment", f"{700:032x}")
    save(store, 800)  # Already passed; a refreshed first page can see it.
    save(store, 600)  # Not passed; belongs to the continuation.
    save(store, 500, created_at=1.0, name="同一updated排序不能移位.txt")
    with store.db:
        store.db.execute("UPDATE objects SET updated=12345 WHERE kind='attachment'")
    remaining = reader.read(cursor=first["next_cursor"], limit=100, max_output_chars=4000)
    assert [item["id"] for item in remaining["items"]] == [
        f"{number:032x}" for number in (600, 500, 300)
    ]
    assert not remaining["has_more"] and remaining["scanned"] == 3
    assert reader.read(limit=1, max_output_chars=4000)["items"][0]["id"] == f"{900:032x}"


@pytest.mark.parametrize(
    ("source", "query", "expected"),
    [
        (None, "", [4, 3, 2, 1]),
        ("", "", [4]),
        ("asr", "Ä%_", [1]),
        ("ASR", "", [2]),
        ("asr", "ä", []),
        ("asr", "%", [1]),
        ("asr", "_", [1]),
        (None, "  ", [3]),
    ],
)
def test_filters_are_exact_literal_case_sensitive_and_preserve_empty_source(
    store: Store, source: str | None, query: str, expected: list[int]
) -> None:
    save(store, 1, source="asr", name="Ä%_.txt")
    save(store, 2, source="ASR", name="uppercase.txt")
    save(store, 3, name="  unchanged  .txt")
    save(store, 4, source="")
    result = pages(AttachmentRecords(store), source=source, name_query=query)
    assert [item["id"] for page in result for item in page["items"]] == [
        f"{number:032x}" for number in expected
    ]


def test_cursor_survives_reopen_and_a_smaller_output_budget(store: Store, tmp_path: Path) -> None:
    for number in range(1, 9):
        save(store, number)
    first = AttachmentRecords(store).read(limit=2, max_output_chars=4000)
    store.close()
    reopened = Store(tmp_path / "attachments.sqlite3")
    try:
        remaining: list[dict[str, Any]] = []
        cursor = first["next_cursor"]
        for _ in range(8):
            page = AttachmentRecords(reopened).read(cursor=cursor, limit=100, max_output_chars=1000)
            assert json_size(page) <= 1000
            remaining.extend(page["items"])
            if not page["has_more"]:
                break
            assert page["next_cursor"] != cursor
            cursor = page["next_cursor"]
        assert [item["id"] for item in remaining] == [
            f"{number:032x}" for number in range(6, 0, -1)
        ]
    finally:
        reopened.close()


@pytest.mark.parametrize("token", ["", "!bad!", "🙂", "[[]]", "A" * 513])
def test_malformed_cursor_is_rejected(store: Store, token: str) -> None:
    with pytest.raises(EvidenceError) as caught:
        AttachmentRecords(store).read(cursor=token, max_output_chars=4000)
    assert caught.value.code == "attachment_cursor_invalid"


def test_cursor_length_is_checked_before_decode(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("oversized tokens must not reach the decoder")

    monkeypatch.setattr("muli.attachment_records.base64.b64decode", forbidden)
    with pytest.raises(EvidenceError, match="游标无效"):
        AttachmentRecords(store).read(cursor="A" * 513, max_output_chars=4000)


def test_cursor_is_bound_to_database_filters_version_and_last_key(
    store: Store, tmp_path: Path
) -> None:
    save(store, 1, source="asr")
    save(store, 2, source="asr")
    reader = AttachmentRecords(store)
    token = reader.read(source="asr", limit=1, max_output_chars=4000)["next_cursor"]
    for arguments in ({"source": "web"}, {"source": "asr", "name_query": "x"}):
        with pytest.raises(EvidenceError, match="筛选不匹配"):
            reader.read(cursor=token, max_output_chars=4000, **arguments)
    other = Store(tmp_path / "other.sqlite3")
    try:
        with pytest.raises(EvidenceError, match="另一资料库"):
            AttachmentRecords(other).read(cursor=token, source="asr", max_output_chars=4000)
    finally:
        other.close()
    payload = json.loads(base64.urlsafe_b64decode(token))
    for index, value in ((0, True), (0, 2), (3, "not-an-attachment-id"), (3, [])):
        changed = [*payload]
        changed[index] = value
        corrupt = base64.urlsafe_b64encode(json.dumps(changed).encode("utf-8")).decode("ascii")
        with pytest.raises(EvidenceError, match="游标无效"):
            reader.read(cursor=corrupt, source="asr", max_output_chars=4000)


@pytest.mark.parametrize(
    "arguments",
    [
        {"source": False},
        {"source": "x" * 121},
        {"name_query": None},
        {"name_query": "🙂" * 121},
        {"name_query": "\ud800"},
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_invalid_arguments_are_not_coerced(store: Store, arguments: dict[str, Any]) -> None:
    with pytest.raises(EvidenceError):
        AttachmentRecords(store).read(max_output_chars=4000, **arguments)


def test_exact_budget_includes_cursor_and_scanned_and_cannot_skip_the_first_match(
    store: Store,
) -> None:
    save(store, 1)
    save(store, 2, name='引号"' * 30)
    reader = AttachmentRecords(store)
    first = reader.read(limit=1, max_output_chars=8000)
    exact = json_size(first)
    assert reader.read(limit=1, max_output_chars=exact) == first
    with pytest.raises(EvidenceError) as caught:
        reader.read(limit=1, max_output_chars=exact - 1)
    assert caught.value.code == "output_budget"


@pytest.mark.parametrize(
    "fields", [{"size": True}, {"sha256": "bad"}, {"source": None}, {"created_at": float("nan")}]
)
def test_corrupt_metadata_is_not_skipped(store: Store, fields: dict[str, Any]) -> None:
    save(store, 1)
    save(store, 2, **fields)
    with pytest.raises(EvidenceError) as caught:
        AttachmentRecords(store).read(max_output_chars=4000)
    assert caught.value.code == "attachment_metadata_invalid"


def test_oversized_metadata_stops_before_the_unconsumed_row_instead_of_skipping_it(
    store: Store,
) -> None:
    save(store, 1, private_note="x" * 10000)
    save(store, 2)
    reader = AttachmentRecords(store)
    first = reader.read(max_output_chars=1000)
    assert [item["id"] for item in first["items"]] == [f"{2:032x}"]
    assert first["has_more"] and first["scanned"] == 1
    with pytest.raises(EvidenceError) as caught:
        reader.read(cursor=first["next_cursor"], max_output_chars=1000)
    assert caught.value.code == "output_budget"


async def test_lost_asr_response_is_found_beyond_200_rows_and_reopened_without_another_asr(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.text = "丢失回执但保留的全文🙂" * 80
    monkeypatch.setattr("muli.attachments.new_id", lambda: f"{1:032x}")
    committed = asyncio.Event()
    original_save = host.attachments.save

    async def lost_return(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await original_save(*args, **kwargs)
        committed.set()
        await asyncio.Event().wait()
        raise AssertionError("the response handoff must remain cancelled")

    monkeypatch.setattr(host.attachments, "save", lost_return)
    operation = asyncio.create_task(host.transcribe())
    try:
        await asyncio.wait_for(committed.wait(), 3)
    finally:
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert len(host.requests) == 1
    for number in range(2, 243):
        save(host.runtime.store, number)
    with host.runtime.store.db:
        host.runtime.store.db.execute(
            "UPDATE objects SET updated=0 WHERE kind='attachment' AND id=?", (f"{1:032x}",)
        )
        host.runtime.store.db.execute(
            "UPDATE objects SET updated=1 WHERE kind='attachment' AND id<>?", (f"{1:032x}",)
        )
    old_list = await host.runtime.tools["attachment_list"].handler({"limit": 200})
    assert isinstance(old_list, list) and f"{1:032x}" not in {item["id"] for item in old_list}

    def no_content(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("attachment_search must read only SQLite metadata")

    with monkeypatch.context() as patch:
        patch.setattr(host.services, "read_bytes", no_content)
        patch.setattr(host.services, "media", no_content)
        patch.setattr(host.attachments, "path", no_content)
        tool = host.runtime.tools["attachment_search"]
        first = await tool.handler({"source": "asr", "name_query": host.audio_id})
        assert first["items"] == [] and first["scanned"] == 200 and first["has_more"]
        found = await tool.handler(
            {"source": "asr", "name_query": host.audio_id, "cursor": first["next_cursor"]}
        )
        assert [item["id"] for item in found["items"]] == [f"{1:032x}"]
        assert not found["has_more"] and len(host.requests) == 1
    artifact = found["items"][0]
    await host.runtime.close()
    reopened = Store.open_runtime(
        host.runtime.data_dir / "muli.sqlite3",
        default_mode=host.runtime.config.settings.default_mode,
    )
    try:
        attachments = Attachments(reopened, host.attachments.directory, host.runtime.config)
        transcript = AudioTranscripts(attachments)
        content: list[str] = []
        offset = 0
        for _ in range(30):
            page = await transcript.read(
                artifact["id"], revision=artifact["sha256"], offset_bytes=offset
            )
            content.append(page["content"])
            offset = page["next_offset_bytes"]
            if not page["has_more"]:
                break
        assert "".join(content) == host.text and len(host.requests) == 1
    finally:
        reopened.close()


async def test_runtime_preserves_complete_search_cursor_and_observes_current_output_budget(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(1, 10):
        save(host.runtime.store, number, source="asr")

    def no_content(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("search must not read files or call media")

    monkeypatch.setattr(host.services, "read_bytes", no_content)
    monkeypatch.setattr(host.services, "media", no_content)
    monkeypatch.setattr(host.attachments, "path", no_content)
    host.runtime.config.update({"limits": {"max_output_chars": 2000}})
    tool = host.runtime.tools["attachment_search"]
    first = await tool.handler({"source": "asr", "limit": 1})
    host.runtime.config.update({"limits": {"max_output_chars": 1000}})
    expected = await tool.handler({"source": "asr", "cursor": first["next_cursor"], "limit": 100})
    host.turns.extend(
        [
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "search",
                        "attachment_search",
                        json.dumps({"source": "asr", "cursor": first["next_cursor"], "limit": 100}),
                    )
                ]
            ),
            ModelTurn(content="已找到可续读的转写工件。"),
        ]
    )
    receipt = await host.runtime.submit("找回转写附件", mode="yolo")
    await asyncio.wait_for(host.runtime.wait(receipt["run_id"]), 3)
    stored = next(
        item
        for item in host.runtime.store.messages(receipt["session_id"])
        if item["role"] == "tool"
    )
    assert json.loads(stored["content"]) == expected and len(stored["content"]) <= 1000
    assert expected["has_more"] and expected["next_cursor"] and expected["scanned"] > 0
    replay = next(message for message in host.seen[-1] if message.tool_call_id == "search")
    assert replay.content == stored["content"] and replay.preserve_content
    assert tool.risk == "read" and tool.preserve_output and len(host.requests) == 0
