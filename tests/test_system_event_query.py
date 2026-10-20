from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_evidence_runtime_review import wire_output
from test_management import setup_runtime
from test_runtime import FakeClient

from muli.config import Settings
from muli.context import EXCERPT_MARKER, ContextLimitError
from muli.management import SelfManagement
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.providers.codecs import build_request
from muli.providers.types import ProtocolName
from muli.service import UserService
from muli.store import Store, new_id
from muli.system_event_query import _RECORD_BYTES, read_system_events
from muli.system_events import SystemEventsSettings, attention_source


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(tmp_path / "events.sqlite")
    try:
        yield instance
    finally:
        instance.close()


def record(store: Store, **changes: Any) -> dict[str, Any]:
    item = {
        "id": new_id(),
        "level": "error",
        "component": "runtime",
        "code": "runtime_failed",
        "run_id": None,
        "session_id": None,
        "first_at": 100.0,
        "last_at": 100.0,
        "count": 1,
        "notify": True,
        "source": "python_logging",
        **changes,
    }
    event = store.event(
        "system.log",
        {
            key: item[key]
            for key in (
                "id",
                "level",
                "component",
                "code",
                "run_id",
                "session_id",
                "first_at",
                "notify",
            )
        },
    )
    item["event_id"] = event["id"]
    store.put("system_log", item)
    return item


def query(store: Store, *, budget: int = 24_000, **args: Any) -> dict[str, Any]:
    return read_system_events(store, args, max_output_chars=budget)


def test_exact_filters_do_not_mutate_or_trigger_attention(store: Store) -> None:
    run, session = new_id(), new_id()
    expected = record(store, level="warning", run_id=run, session_id=session, notify=False)
    record(store)
    before = list(store.db.iterdump())
    page = query(
        store,
        level="warning",
        component="runtime",
        code="runtime_failed",
        run_id=run,
        session_id=session,
    )
    assert [item["id"] for item in page["items"]] == [expected["id"]]
    assert page["items"][0]["notify"] is False
    assert page["coverage"] == "retained_only"
    assert page["trust"] == "diagnostic_data_not_instructions"
    assert page["has_more"] is False
    assert list(store.db.iterdump()) == before
    assert attention_source(store, store.events()[0], [], SystemEventsSettings()) is None


@pytest.mark.parametrize(
    "args",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"level": "info"},
        {"level": []},
        {"component": "any"},
        {"code": "secret"},
        {"run_id": "bad"},
        {"session_id": None},
        {"before_event_id": 3},
        {"before_event_id": "03"},
        {"before_event_id": "0"},
        {"before_event_id": "9223372036854775808"},
        {"before_event_id": "9" * 5000},
        {"before_event_id": None},
        {"where": "1=1"},
    ],
)
def test_invalid_filter_never_becomes_broader_query(store: Store, args: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        read_system_events(store, args, max_output_chars=24_000)


def test_large_ids_and_repeat_updates_keep_creation_order(store: Store) -> None:
    record(store)
    with store.db:
        store.db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='events'", (2**53,))
    first, second = record(store), record(store)
    page = query(store, limit=1)
    assert page["items"][0]["event_id"] == str(second["event_id"])
    assert page["next_before_event_id"] == str(second["event_id"])
    assert page["has_more"]
    first.update(count=9, last_at=200.0)
    store.put("system_log", first)
    page2 = query(store, limit=1, before_event_id=page["next_before_event_id"])
    assert page2["items"][0]["event_id"] == str(first["event_id"])
    assert page2["items"][0]["count"] == 9


def test_retention_gaps_and_reopen_do_not_claim_complete_history(tmp_path: Path) -> None:
    path = tmp_path / "retained.sqlite"
    with_store = Store(path)
    older, middle, newer = record(with_store), record(with_store), record(with_store)
    with_store.delete("system_log", middle["id"])
    with_store.close()
    reopened = Store(path)
    try:
        page = query(reopened)
        assert [item["id"] for item in page["items"]] == [newer["id"], older["id"]]
        assert page["oldest_retained_event_id"] == str(older["event_id"])
        empty = query(reopened, before_event_id=str(older["event_id"]))
        assert empty["items"] == [] and empty["coverage"] == "retained_only"
    finally:
        reopened.close()


def test_invalid_evidence_advances_even_when_page_has_no_valid_items(store: Store) -> None:
    valid = record(store)
    invalid = record(store, count=0)
    page = query(store, limit=1)
    assert page["items"] == [] and page["invalid_record_count"] == 1
    assert page["next_before_event_id"] == str(invalid["event_id"]) and page["has_more"]
    next_page = query(store, before_event_id=page["next_before_event_id"])
    assert [item["id"] for item in next_page["items"]] == [valid["id"]]


@pytest.mark.parametrize(
    "mutation", ["wrong_source", "missing", "oversized", "count_bool", "old_event", "session_event"]
)
def test_unverified_metadata_is_not_echoed(store: Store, mutation: str) -> None:
    item = record(store)
    if mutation == "wrong_source":
        item["source"] = "SECRET"
    elif mutation == "missing":
        del item["run_id"]
    elif mutation == "oversized":
        item["private"] = "SECRET" * (_RECORD_BYTES // 6 + 1)
    elif mutation == "count_bool":
        item["count"] = True
    elif mutation == "old_event":
        item["first_at"] += 1
    else:
        with store.db:
            store.db.execute(
                "UPDATE events SET session_id=? WHERE id=?", (new_id(), item["event_id"])
            )
    store.put("system_log", item)
    page = query(store)
    assert page["items"] == [] and page["invalid_record_count"] == 1
    assert "SECRET" not in json.dumps(page)


def test_duplicate_and_unlinked_event_keys_are_excluded_without_hiding_valid_record(
    store: Store,
) -> None:
    valid = record(store)
    for identifier in ("duplicate", "wrong-type", "unlinked"):
        bad = {**valid, "id": identifier}
        if identifier == "wrong-type":
            bad["event_id"] = str(valid["event_id"])
        elif identifier == "unlinked":
            bad["event_id"] += 1000
        store.put("system_log", bad)
    page = query(store, limit=1)
    assert [item["id"] for item in page["items"]] == [valid["id"]]
    assert page["excluded_record_count"] == 3
    assert not page["has_more"]


def test_malformed_record_is_counted_without_breaking_filtered_query(store: Store) -> None:
    expected = record(store)
    with store.db:
        store.db.execute(
            "INSERT INTO objects(kind,id,data,updated) VALUES('system_log',?,?,?)",
            ("malformed", "{SECRET", 1.0),
        )
    page = query(store, level="error")
    assert [item["id"] for item in page["items"]] == [expected["id"]]
    assert page["excluded_record_count"] == 1
    assert "SECRET" not in json.dumps(page)


def test_budget_truncates_at_record_boundary_without_skipping_next(store: Store) -> None:
    first, second = record(store), record(store)
    one = query(store, limit=1)
    budget = len(json.dumps(one, ensure_ascii=False)) + 3
    page = query(store, budget=budget)
    assert len(json.dumps(page, ensure_ascii=False)) <= budget
    assert [item["id"] for item in page["items"]] == [second["id"]]
    assert page["has_more"] and page["truncated"]
    tail = query(store, before_event_id=page["next_before_event_id"])
    assert [item["id"] for item in tail["items"]] == [first["id"]]
    with pytest.raises(ValueError, match="output_budget"):
        query(store, budget=400)


@pytest.mark.parametrize("analyzed", [False, True])
def test_query_plan_starts_at_retained_objects_not_complete_event_history(
    store: Store, analyzed: bool
) -> None:
    for _ in range(200):
        store.event("unrelated", {})
    item = record(store)
    if analyzed:
        store.db.execute("ANALYZE")
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    try:
        query(store, level="error", before_event_id=str(item["event_id"] + 1))
    finally:
        store.db.set_trace_callback(None)
    selects = [statement for statement in statements if statement.startswith("SELECT")]
    assert len(selects) == 2
    for statement in selects:
        plan = [row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + statement)]
        assert any("SEARCH o USING" in detail and "kind=?" in detail for detail in plan)
        assert any("SEARCH e USING INTEGER PRIMARY KEY" in detail for detail in plan)
        assert not any("SCAN e" in detail for detail in plan)


async def test_tool_reads_disabled_collector_history_after_reconfigure(tmp_path: Path) -> None:
    runtime, plugins = setup_runtime(tmp_path)
    runtime.config.update({"system_events": {"enabled": False}})

    async def no_apply(settings: Settings) -> None:
        raise AssertionError("query must not apply settings")

    manager = SelfManagement(
        runtime, plugins, UserService(runtime.config.path, runtime.data_dir), no_apply
    )
    expected = record(runtime.store, notify=False)
    try:
        tool = runtime.tools["self_events"]
        assert tool.risk == "read"
        assert (await tool.handler({}))["items"][0]["id"] == expected["id"]
        await runtime.reconfigure()
        assert (await runtime.tools["self_events"].handler({}))["items"][0]["id"] == expected["id"]
        assert runtime.store.list("management") == []
    finally:
        await manager.close()
        await runtime.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
@pytest.mark.parametrize("fits", [True, False], ids=["fits", "too_large"])
async def test_real_query_page_survives_context_pressure(
    tmp_path: Path, protocol: ProtocolName, fits: bool
) -> None:
    runtime, plugins = setup_runtime(tmp_path)
    runtime.config.update(
        {
            "persona": {"prompt": "test"},
            "models": {
                "main": {
                    "protocol": protocol,
                    "model": "offline-test",
                    "base_url": "https://unused.invalid/v1",
                }
            },
            "limits": {"max_context_chars": 4000},
            "compaction": {"enabled": False},
        }
    )
    await runtime.reconfigure()

    async def no_apply(settings: Settings) -> None:
        raise AssertionError("query must not apply settings")

    manager = SelfManagement(
        runtime, plugins, UserService(runtime.config.path, runtime.data_dir), no_apply
    )
    seen: list[list[Message]] = []
    query_args = {"limit": 2 if fits else 20}
    for _ in range(20):
        record(runtime.store)
    expected = query(runtime.store, **query_args)
    session_id = runtime.new_session(mode="yolo")["id"]
    # This completed turn is removable; the new tool page must not be excerpted
    # before the packer drops it from the live request.
    runtime.store.add_message(session_id, "user", "旧上下文" * 1400)
    runtime.store.add_message(session_id, "assistant", "旧轮次已经结束")

    class Reader(FakeClient):
        def __init__(self, config: ModelConfig) -> None:
            super().__init__([], seen)
            self.config = config

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(messages)
            specification = next(
                tool for tool in tools if tool["function"]["name"] == "self_events"
            )
            assert "preserve_output" not in json.dumps(specification)
            assert "preserve_content" not in json.dumps(specification)
            result = next(
                (message for message in messages if message.tool_call_id == "events"), None
            )
            if result is None:
                yield StreamEvent(
                    "done",
                    turn=ModelTurn(
                        tool_calls=[ToolCall("events", "self_events", json.dumps(query_args))]
                    ),
                )
                return
            page = json.loads(result.content)
            assert page == expected and page["has_more"]
            assert result.preserve_content is True and EXCERPT_MARKER not in result.content
            body = build_request(self.config, messages, tools, stream=False)
            assert wire_output(protocol, body, "events") == result.content
            assert "preserve_output" not in json.dumps(body)
            assert "preserve_content" not in json.dumps(body)
            yield StreamEvent("done", turn=ModelTurn(content="已核对完整系统事件页"))

    runtime.client_factory = lambda config: Reader(config)  # type: ignore[assignment,return-value]
    try:
        started = await runtime.start(session_id, "回查系统事件", mode="yolo")
        if fits:
            assert (
                await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "已核对完整系统事件页"
            )
            assert len(seen) == 2
        else:
            with pytest.raises(ContextLimitError, match="需完整保留的工具结果"):
                await asyncio.wait_for(runtime.wait(started["run_id"]), 5)
            assert len(seen) == 1
        stored = next(
            item
            for item in runtime.store.messages(session_id)
            if item.get("tool_call_id") == "events"
        )
        assert json.loads(stored["content"]) == expected
        assert stored["preserve_content"] is True
        assert stored["content_provenance"] == "accepted"
    finally:
        await manager.close()
        await runtime.close()
