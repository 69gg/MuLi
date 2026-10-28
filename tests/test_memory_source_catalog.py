"""Bounded source discovery before a source ID or useful query is known."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_document_memory import tool
from test_runtime import FakeClient
from test_teammate_attachment_runtime import eventually, tool_result

from muli.evidence import EvidenceError, json_size
from muli.memory import Memory
from muli.memory_evidence import MemoryEvidence
from muli.providers import Message, ModelTurn, StreamEvent, ToolCall
from muli.store import Store

OUTPUT_BUDGET = 20_000


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    store = Store(tmp_path / "catalog.db")
    try:
        yield Memory(store)
    finally:
        store.close()


def register(
    memory: Memory,
    identifier: str,
    *,
    state: str = "deferred",
    content: str = "完整来源正文🙂，不是目录预览。",
    principal: str = "external:catalog",
) -> dict[str, Any]:
    source, created = memory.register_source(
        {
            "id": identifier,
            "content": content,
            "source": "catalog-test",
            "session_id": "original-session",
            "role": "tool",
            "principal": principal,
            "created_at": 1234.0,
        }
    )
    assert created and source is not None
    if state != "deferred":
        with memory.store.db:
            memory.store.db.execute(
                "INSERT INTO memory_jobs"
                "(id,source_id,state,attempts,next_attempt,last_error,checkpoint,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    identifier,
                    state,
                    2,
                    1234.0,
                    "RuntimeError" if state == "failed" else "",
                    json.dumps({"private_checkpoint": "MUST-NOT-LEAK"}),
                    1234.0,
                    1234.0,
                ),
            )
    return source


def test_equal_time_sources_have_complete_metadata_and_no_paging_gaps(memory: Memory) -> None:
    states = ["deferred", "queued", "processing", "retry", "failed", "done"]
    originals = {
        f"source-{index}": register(memory, f"source-{index}", state=state)
        for index, state in enumerate(states)
    }
    reader = MemoryEvidence(memory)
    changes, revision = memory.store.db.total_changes, memory.revision
    cursor: str | None = None
    seen: list[str] = []
    for _ in range(len(originals)):
        page = reader.sources(cursor=cursor, limit=2, max_output_chars=OUTPUT_BUDGET)
        assert page["consistency"] == "live" and page["order"] == "newest_registered_first"
        assert page["examined"] == len(page["items"]) == 2 and page["omitted"] == 0
        for item in page["items"]:
            assert set(item) == {
                "source_id",
                "source",
                "session_id",
                "role",
                "principal",
                "trusted_owner",
                "revision",
                "created_at",
                "total_bytes",
                "job",
            }
            original = originals[item["source_id"]]
            assert item["revision"] == original["digest"]
            assert item["total_bytes"] == len(original["content"].encode("utf-8"))
            assert item["principal"] == "external:catalog" and item["trusted_owner"] is False
            assert item["source"] == original["source"]
            assert item["session_id"] == original["session_id"]
            assert item["role"] == original["role"] and item["created_at"] == original["created_at"]
            state = states[int(item["source_id"].removeprefix("source-"))]
            assert item["job"] == {
                "state": state,
                "attempts": 0 if state == "deferred" else 2,
                "last_error": "RuntimeError" if state == "failed" else "",
            }
            seen.append(item["source_id"])
        assert "MUST-NOT-LEAK" not in json.dumps(page)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
        assert isinstance(cursor, str) and cursor
    else:
        pytest.fail("source directory did not reach its end")
    assert seen == list(reversed(originals))
    assert memory.store.db.total_changes == changes and memory.revision == revision


def test_filter_and_invalid_rows_consume_candidates_and_empty_pages_advance(memory: Memory) -> None:
    register(memory, "wanted-failure", state="failed")
    register(memory, "done-middle", state="done")
    register(memory, "invalid-newest", state="failed")
    with memory.store.db:
        memory.store.db.execute(
            "UPDATE memory_sources SET digest='invalid' WHERE id='invalid-newest'"
        )
    reader = MemoryEvidence(memory)
    cursor: str | None = None
    seen: list[str] = []
    cursors: list[str] = []
    for index in range(3):
        page = reader.sources(
            cursor=cursor, limit=1, state="failed", max_output_chars=OUTPUT_BUDGET
        )
        assert page["examined"] == 1
        if index < 2:
            assert page["items"] == [] and page["has_more"]
            assert page["omitted"] == 1
            assert isinstance(page["next_cursor"], str)
            assert page["next_cursor"] not in cursors
            cursors.append(page["next_cursor"])
        else:
            seen.extend(item["source_id"] for item in page["items"])
            assert page["omitted"] == 0
            assert not page["has_more"] and page["next_cursor"] is None
        cursor = page["next_cursor"]
    assert seen == ["wanted-failure"]
    assert memory.forget_source("wanted-failure")
    changes = memory.store.db.total_changes
    page = reader.sources(state="failed", max_output_chars=OUTPUT_BUDGET)
    assert page["items"] == []
    assert memory.source("wanted-failure") is None
    assert memory.store.db.total_changes == changes


def test_cursor_is_bound_to_store_and_filter_but_survives_normal_reopen(tmp_path: Path) -> None:
    path = tmp_path / "original.db"
    store = Store(path)
    try:
        memory = Memory(store)
        register(memory, "old", state="failed")
        register(memory, "new", state="failed")
        first = MemoryEvidence(memory).sources(
            limit=1, state="failed", max_output_chars=OUTPUT_BUDGET
        )
        cursor = first["next_cursor"]
        assert first["has_more"] and isinstance(cursor, str)
        with pytest.raises(EvidenceError):
            MemoryEvidence(memory).sources(
                cursor=cursor, state="done", max_output_chars=OUTPUT_BUDGET
            )
    finally:
        store.close()
    reopened = Store(path)
    other = Store(tmp_path / "other.db")
    try:
        resumed = MemoryEvidence(Memory(reopened)).sources(
            cursor=cursor, state="failed", max_output_chars=OUTPUT_BUDGET
        )
        assert [item["source_id"] for item in resumed["items"]] == ["old"]
        with pytest.raises(EvidenceError):
            MemoryEvidence(Memory(other)).sources(
                cursor=cursor, state="failed", max_output_chars=OUTPUT_BUDGET
            )
    finally:
        reopened.close()
        other.close()


def test_catalog_arguments_fail_explicitly_without_writes(memory: Memory) -> None:
    register(memory, "origin")
    reader = MemoryEvidence(memory)
    changes = memory.store.db.total_changes
    bad: list[dict[str, Any]] = [
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"limit": 1 << 80},
        {"limit": "2"},
        {"state": "unknown"},
        {"state": True},
        {"cursor": True},
        {"cursor": ""},
        {"cursor": "broken-cursor"},
        {"max_output_chars": True},
        {"max_output_chars": 0},
    ]
    for options in bad:
        with pytest.raises(EvidenceError):
            reader.sources(**{"max_output_chars": OUTPUT_BUDGET, **options})
    assert memory.store.db.total_changes == changes


def test_valid_identity_too_large_for_current_budget_is_not_silently_omitted(
    memory: Memory,
) -> None:
    saved = register(memory, "complete-identity", principal="external:" + "界" * 990)
    reader = MemoryEvidence(memory)
    changes = memory.store.db.total_changes
    for budget in (300, 1000):
        with pytest.raises(EvidenceError) as caught:
            reader.sources(max_output_chars=budget)
        assert caught.value.code == "output_budget"
    page = reader.sources(max_output_chars=OUTPUT_BUDGET)
    assert len(page["items"]) == 1 and page["omitted"] == 0
    assert page["items"][0]["principal"] == saved["principal"]
    assert memory.store.db.total_changes == changes


def test_catalog_never_materializes_body_and_live_budget_keeps_complete_prefix(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    oldest = register(memory, "oldest", content="大正文🙂" * 10_000)
    register(memory, "middle", principal="external:" + "界" * 500)
    newest = register(memory, "newest")

    def forbid(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("source catalog must not load original bodies")

    monkeypatch.setattr(memory, "source", forbid)
    reader = MemoryEvidence(memory)
    full = reader.sources(limit=3, max_output_chars=OUTPUT_BUDGET)
    assert [item["source_id"] for item in full["items"]] == ["newest", "middle", "oldest"]
    assert full["items"][-1]["total_bytes"] == len(oldest["content"].encode("utf-8"))
    assert "大正文" not in json.dumps(full, ensure_ascii=False)
    one = reader.sources(limit=1, max_output_chars=OUTPUT_BUDGET)
    budget = json_size(one) + 64
    prefix = reader.sources(limit=3, max_output_chars=budget)
    assert [item["source_id"] for item in prefix["items"]] == [newest["id"]]
    assert json_size(prefix) <= budget and prefix["has_more"]
    assert isinstance(prefix["next_cursor"], str)
    with pytest.raises(EvidenceError) as caught:
        reader.sources(cursor=prefix["next_cursor"], max_output_chars=budget)
    assert caught.value.code == "output_budget"
    resumed = reader.sources(cursor=prefix["next_cursor"], max_output_chars=OUTPUT_BUDGET)
    assert [item["source_id"] for item in resumed["items"]] == ["middle", "oldest"]
    with pytest.raises(EvidenceError) as caught:
        reader.sources(max_output_chars=1)
    assert caught.value.code == "output_budget"
    assert reader.sources(limit=3, max_output_chars=OUTPUT_BUDGET) == full


async def test_main_and_real_teammate_discover_then_read_source_without_known_id(
    tmp_path: Path,
) -> None:
    body = "目录发现之后核对的完整正文🙂。" * 40
    worker_pages: list[dict[str, Any]] = []
    worker_reads: list[dict[str, Any]] = []
    catalog_call, read_call = "worker-source-catalog", "worker-source-read"

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            names = {item["function"]["name"] for item in tools}
            assert {"memory_sources", "memory_source"} <= names
            agent = rig.runtime.team.get("目录核查队友")
            session = rig.runtime.store.get("session", agent["id"])
            assert session is not None and session["kind"] == "team"
            assert agent["parent_id"] == rig.runtime.main_session()["id"]
            if not any(item.tool_call_id == catalog_call for item in messages):
                turn = ModelTurn(tool_calls=[ToolCall(catalog_call, "memory_sources", "{}")])
            elif not any(item.tool_call_id == read_call for item in messages):
                page = tool_result(messages, catalog_call)
                worker_pages.append(page)
                discovered = next(
                    item for item in page["items"] if item["principal"] == "external:catalog"
                )
                turn = ModelTurn(
                    tool_calls=[
                        ToolCall(
                            read_call,
                            "memory_source",
                            json.dumps(
                                {
                                    "id": discovered["source_id"],
                                    "revision": discovered["revision"],
                                }
                            ),
                        )
                    ]
                )
            else:
                result = tool_result(messages, read_call)
                worker_reads.append(result)
                assert result["content"] == body and not result["has_more"]
                turn = ModelTurn(content="已从来源目录找到并核对原文。")
            yield StreamEvent("done", turn=turn)

    async with actual_app(tmp_path, worker=Worker([], [])) as rig:
        original_factory = rig.runtime.client_factory
        saved = register(rig.runtime.memory, "unknown-to-catalog-reader", content=body)
        page = await tool(rig, "memory_sources")
        discovered = next(item for item in page["items"] if item["principal"] == "external:catalog")
        assert discovered["source_id"] == saved["id"]
        main_read = await tool(
            rig, "memory_source", id=discovered["source_id"], revision=discovered["revision"]
        )
        assert main_read["content"] == body and not main_read["has_more"]
        rig.runtime.client_factory = original_factory
        agent = await rig.runtime.team.spawn(
            "目录核查队友", "先浏览长期资料目录，再核查找到的原文。"
        )
        await eventually(
            rig.runtime,
            lambda: (
                rig.runtime.team.get(agent["id"])["status"]
                in {"completed", "failed", "cancelled", "interrupted"}
            ),
        )
        finished = rig.runtime.team.get(agent["id"])
        assert finished["status"] == "completed"
        assert len(worker_pages) == len(worker_reads) == 1
        assert worker_reads[0]["source_id"] == saved["id"]
        run = rig.runtime.store.get("run", finished["run_id"])
        assert run is not None and run["session_id"] == agent["id"]
        replies = {
            item.get("tool_call_id")
            for item in rig.runtime.store.messages(agent["id"])
            if item["role"] == "tool"
        }
        assert {catalog_call, read_call} <= replies
