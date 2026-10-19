from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.compaction import Compactor, closed_groups
from muli.config import CompactionSettings
from muli.providers import Message, ModelTurn, StreamEvent
from muli.store import Store


def history(store: Store, session: str = "session") -> list[dict[str, Any]]:
    store.add_message(session, "user", "待办承诺：检查证据后继续。" + "约束" * 500)
    store.add_message(
        session,
        "assistant",
        "计划",
        tool_calls=[
            {"id": "a", "name": "read", "arguments": "{}"},
            {"id": "b", "name": "check", "arguments": "{}"},
        ],
        provider_data={
            "content_blocks": [
                {"type": "thinking", "signature": "signed-original", "thinking": "native"}
            ]
        },
    )
    store.add_message(session, "tool", "已执行证据" * 300, tool_call_id="a")
    store.add_message(session, "tool", "结果未知，先检查" * 300, tool_call_id="b")
    store.add_message(session, "user", "最新要求必须完整保留")
    return store.messages(session)


async def finish(compactor: Compactor) -> None:
    await asyncio.gather(*list(compactor._tasks.values()))


async def test_background_summary_installs_only_closed_prefix_and_keeps_live_suffix(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    original = history(store)
    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[str] = []

    async def summarize(source: str, max_tokens: int) -> str:
        seen.append(source)
        entered.set()
        await release.wait()
        return "待办承诺：继续检查；b 的结果未知，不能重做。"

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 2,
        fingerprint=lambda: "config",
    )
    try:
        compactor.observe("session")
        assert compactor.status("session")["running"]
        await asyncio.wait_for(entered.wait(), 2)
        assert store.get("compaction", "session") is None
        live = store.add_message("session", "assistant", "并行产生的新后缀")
        release.set()
        await finish(compactor)
        assert compactor.adopt("session")
        projected = compactor.project("session")
        assert projected[0]["compaction_summary"]
        assert "待办承诺" in projected[0]["content"]
        assert projected[-1]["id"] == live["id"]
        assert store.messages("session")[: len(original)] == original
        active = store.get("compaction", "session")
        assert active is not None
        assert active["covered_seq"] == original[3]["seq"]
        assert "seq=" in seen[0] and "结果未知" in seen[0]
    finally:
        release.set()
        await compactor.close()
        store.close()


@pytest.mark.parametrize("change", ["prefix", "generation", "config"])
async def test_candidate_rejected_when_snapshot_no_longer_matches(
    tmp_path: Path, change: str
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    records = history(store)
    fingerprint = "original"

    async def summarize(source: str, max_tokens: int) -> str:
        return "摘要"

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: fingerprint,
    )
    try:
        compactor.observe("session")
        await finish(compactor)
        if change == "prefix":
            store.put("message", {**records[0], "content": "用户更正过的资料"})
        elif change == "generation":
            store.put("compaction", {"id": "session", "generation": 99})
        else:
            fingerprint = "changed"
        assert not compactor.adopt("session")
        assert not store.list("compaction_snapshot")
    finally:
        await compactor.close()
        store.close()


async def test_semantic_failure_falls_back_once_and_later_layers_keep_provenance(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    history(store)
    calls = 0

    async def summarize(source: str, max_tokens: int) -> str:
        nonlocal calls
        calls += 1
        raise TimeoutError

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "config",
    )
    try:
        compactor.observe("session")
        compactor.observe("session")
        await finish(compactor)
        compactor.project("session")
        first = store.get("compaction", "session")
        assert first and first["kind"] == "extractive" and first["level"] == 1
        assert "可追溯摘录" in first["summary"] and calls == 1
        history(store)
        compactor.observe("session")
        await finish(compactor)
        compactor.project("session")
        second = store.get("compaction", "session")
        assert second and second["level"] == 2
        assert second["parent_snapshot_id"] == first["snapshot_id"]
        assert "前层摘要" in second["summary"] and calls == 2
        assert not compactor._attempted
    finally:
        await compactor.close()
        store.close()


@pytest.mark.parametrize("enabled,mode", [(False, "auto"), (True, "extractive")])
async def test_disabled_or_extractive_never_invokes_model(
    tmp_path: Path, enabled: bool, mode: str
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    history(store)

    async def forbidden(source: str, max_tokens: int) -> str:
        pytest.fail("不应该请求模型")

    settings = CompactionSettings(enabled=enabled, mode=mode)  # type: ignore[arg-type]
    compactor = Compactor(
        store,
        forbidden,
        lambda: settings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "config",
    )
    try:
        compactor.observe("session")
        await finish(compactor)
        compactor.project("session")
        assert bool(store.get("compaction", "session")) == enabled
    finally:
        await compactor.close()
        store.close()


async def test_below_threshold_is_idle_and_close_cancels_background_request(tmp_path: Path) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def summarize(source: str, max_tokens: int) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "unreachable"

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "config",
    )
    try:
        store.add_message("small", "user", "短消息")
        compactor.observe("small")
        assert not compactor._tasks
        history(store)
        compactor.observe("session")
        await asyncio.wait_for(entered.wait(), 2)
        await compactor.close()
        assert cancelled.is_set() and not compactor._tasks and not compactor._ready
        assert not store.list("compaction_snapshot")
    finally:
        await compactor.close()
        store.close()


def test_partial_native_tool_batch_is_never_selected(tmp_path: Path) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    try:
        records = history(store)
        groups = closed_groups(records[:3])
        assert groups == [[records[0]]]
        assert closed_groups(records)[1] == records[1:4]
    finally:
        store.close()


async def test_forget_settles_job_before_deleting_all_summary_layers(tmp_path: Path) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    history(store)
    entered = asyncio.Event()

    async def summarize(source: str, max_tokens: int) -> str:
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "config",
    )
    try:
        store.put(
            "compaction_snapshot", {"id": "old", "session_id": "session", "summary": "旧资料"}
        )
        compactor.observe("session")
        await asyncio.wait_for(entered.wait(), 2)
        await compactor.forget("session")
        assert not compactor._tasks and not compactor._ready
        assert not store.list("compaction_snapshot") and not store.get("compaction", "session")
        assert store.messages("session")  # The session-delete transaction owns original messages.
    finally:
        await compactor.close()
        store.close()


@pytest.mark.parametrize("foreground", ["main", "team"])
async def test_background_summary_never_takes_the_only_foreground_slot(
    tmp_path: Path, foreground: str
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update({"limits": {"max_parallel_agents": 1, "max_context_chars": 5000}})
    await runtime.reconfigure()
    entered, release = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            entered.set()
            await release.wait()
            return ModelTurn(content="历史摘要")

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            yield StreamEvent("done", turn=ModelTurn(content="即时回复"))

    runtime.client_factory = lambda config: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        old = runtime.new_session(mode="yolo")
        history(runtime.store, old["id"])
        runtime.compaction.observe(old["id"])
        await asyncio.wait_for(entered.wait(), 2)
        if foreground == "main":
            started = await runtime.submit("新的短问题", mode="yolo")
            assert await asyncio.wait_for(runtime.wait(started["run_id"]), 2) == "即时回复"
        else:
            parent = runtime.new_session(mode="yolo")
            child = await runtime.team.spawn("及时执行", "新的短任务", parent_id=parent["id"])
            outcome = await asyncio.wait_for(runtime.team.wait([child["id"]], timeout=2), 3)
            assert outcome["agents"][0]["status"] == "completed"
        assert not release.is_set()
        assert runtime.compaction.status(old["id"])["running"]
    finally:
        release.set()
        await runtime.close()


async def test_final_stream_idle_boundary_adopts_ready_summary_and_releases_job_budget(
    tmp_path: Path,
) -> None:
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(
        {"limits": {"max_parallel_agents": 1, "max_pending_runs": 1, "max_context_chars": 5000}}
    )
    await runtime.reconfigure()
    prepared, release = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            prepared.set()
            return ModelTurn(content="已完成的摘要")

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            await release.wait()
            yield StreamEvent("done", turn=ModelTurn(content="最终答复"))

    runtime.client_factory = lambda config: Client([], seen)  # type: ignore[assignment,return-value]
    try:
        session = runtime.new_session(mode="yolo")
        history(runtime.store, session["id"])
        started = await runtime.start(session["id"], "继续")
        await asyncio.wait_for(prepared.wait(), 2)
        jobs = list(runtime.compaction._tasks.values())
        await asyncio.gather(*jobs)
        # Gather's callback order is not the condition under test: explicitly settle
        # the done callback while the foreground stream remains alive.
        for job in jobs:
            runtime.compaction._done(session["id"], job)
        assert runtime.compaction.status(session["id"])["ready"]
        assert runtime.store.get("compaction", session["id"]) is None
        release.set()
        assert await runtime.wait(started["run_id"]) == "最终答复"
        assert runtime.store.get("compaction", session["id"]) is not None
        assert not runtime.compaction.status(session["id"])["ready"]
        other = runtime.new_session(mode="yolo")
        history(runtime.store, other["id"])
        runtime.compaction.observe(other["id"])
        assert runtime.compaction.status(other["id"])["running"]
    finally:
        release.set()
        await runtime.close()
