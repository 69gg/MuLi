from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_compaction import history

from muli.compaction import Compactor
from muli.config import CompactionSettings
from muli.store import Store


@pytest.mark.parametrize("operation", ["close", "forget"])
async def test_cancelled_close_waiter_does_not_cancel_summary_cleanup_twice(
    tmp_path: Path,
    operation: str,
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    original = history(store)
    entered, cleaning = asyncio.Event(), asyncio.Event()
    release, cleaned, cancelled_twice = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closing: asyncio.Task[None] | None = None

    async def summarize(source: str, max_tokens: int) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled_twice.set()
                raise
            cleaned.set()
        raise AssertionError("cancelled summary must not return a candidate")

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "unchanged-config",
    )
    try:
        compactor.observe("session")
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(
            compactor.close() if operation == "close" else compactor.forget("session")
        )
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not cleaned.is_set() and not closing.done()

        # The child is already settling its first cancellation. Cancelling its
        # observer must be remembered without forwarding another child.cancel().
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 2)

        assert not cancelled_twice.is_set(), "outer close cancellation interrupted summary cleanup"
        assert cleaned.is_set()
        assert compactor._tasks == {} and compactor._ready == {} and compactor._attempted == {}
        assert store.get("compaction", "session") is None
        assert store.list("compaction_snapshot") == []
        assert store.messages("session") == original
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 2)
        await asyncio.wait_for(compactor.close(), 2)
        store.close()


@pytest.mark.parametrize("status", ["idle", "paused"])
async def test_close_rejects_late_summary_that_suppresses_cancellation(
    tmp_path: Path, status: str
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    session = store.main_session()
    session["status"] = status
    store.put("session", session)
    session_id = session["id"]
    original = history(store, session_id)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closing: asyncio.Task[None] | None = None
    calls = 0

    async def summarize(source: str, max_tokens: int) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return "服务适配器吞掉取消后返回的迟到摘要"
        raise AssertionError("summary should settle only after close requests cancellation")

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "unchanged-config",
    )
    try:
        compactor.observe(session_id)
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(compactor.close())
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 2)

        assert store.get("compaction", session_id) is None, "close adopted a revoked late summary"
        assert store.list("compaction_snapshot") == []
        assert store.messages(session_id) == original
        compactor.observe(session_id)
        assert compactor.adopt(session_id) is False
        assert compactor.status(session_id)["running"] is False
        assert calls == 1
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 2)
        await asyncio.wait_for(compactor.close(), 2)
        store.close()


async def test_forget_rejects_done_callback_observe_without_detaching_a_new_job(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    session_id = store.main_session()["id"]
    original = history(store, session_id)
    first_entered, first_cancelled, first_release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    next_entered, next_release, next_settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reobserved = asyncio.Event()
    forgetting: asyncio.Task[None] | None = None
    calls = 0

    async def summarize(source: str, max_tokens: int) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancelled.set()
                await first_release.wait()
                raise
        task = asyncio.current_task()
        assert task is not None
        # Compactor registered its own done callback before this callback. This
        # event therefore observes actual candidate installation, not just HTTP completion.
        task.add_done_callback(lambda _: next_settled.set())
        next_entered.set()
        await next_release.wait()
        return "保留原文允许之后显式创建的新摘要"

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "unchanged-config",
    )

    def reobserve(completed: asyncio.Task[None]) -> None:
        compactor.observe(session_id)
        reobserved.set()

    try:
        compactor.observe(session_id)
        await asyncio.wait_for(first_entered.wait(), 2)
        original_job = next(
            task for task in asyncio.all_tasks() if task.get_name() == "muli-compact-" + session_id
        )
        # Register before forget's gather, immediately after Compactor's own
        # callback. A normal public observe then runs in the vulnerable gap.
        original_job.add_done_callback(reobserve)
        forgetting = asyncio.create_task(compactor.forget(session_id))
        await asyncio.wait_for(first_cancelled.wait(), 2)
        first_release.set()
        await asyncio.wait_for(forgetting, 2)
        await asyncio.wait_for(reobserved.wait(), 2)
        if next_entered.is_set():
            next_release.set()
            await asyncio.wait_for(next_settled.wait(), 2)

        assert store.get("compaction", session_id) is None, "forgotten summary was resurrected"
        assert store.list("compaction_snapshot") == []
        assert calls == 1, "observe admitted a replacement before forget settled"
        assert store.messages(session_id) == original

        # This is derived-summary cleanup, not permanent source forgetting.
        # A new explicit observation after forget returns must remain supported.
        compactor.observe(session_id)
        await asyncio.wait_for(next_entered.wait(), 2)
        next_release.set()
        await asyncio.wait_for(next_settled.wait(), 2)
        active = store.get("compaction", session_id)
        assert active is not None and active["summary"] == "保留原文允许之后显式创建的新摘要"
        assert len(store.list("compaction_snapshot")) == 1
        assert calls == 2 and store.messages(session_id) == original
    finally:
        first_release.set()
        next_release.set()
        if forgetting is not None:
            await asyncio.wait_for(asyncio.gather(forgetting, return_exceptions=True), 2)
        if next_entered.is_set():
            await asyncio.wait_for(next_settled.wait(), 2)
        await asyncio.wait_for(compactor.close(), 2)
        store.close()
