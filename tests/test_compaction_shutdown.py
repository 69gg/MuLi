from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_attention import eventually
from test_compaction import history
from test_runtime import FakeClient, make_runtime

from muli.compaction import Compactor
from muli.config import CompactionSettings
from muli.providers import Message, ModelTurn
from muli.store import Store


async def test_close_owns_shared_forget_without_cancelling_its_child_cleanup_again(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    session_id = store.main_session()["id"]
    original = history(store, session_id)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned, interrupted = asyncio.Event(), asyncio.Event()
    second_started, close_started = asyncio.Event(), asyncio.Event()
    waiters: list[asyncio.Task[None]] = []

    async def summarize(source: str, max_tokens: int) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            cleaned.set()
        raise AssertionError("cancelled summary must not return")

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "same",
    )

    async def forget_again() -> None:
        second_started.set()
        await compactor.forget(session_id)

    async def close() -> None:
        close_started.set()
        await compactor.close()

    try:
        compactor.observe(session_id)
        await asyncio.wait_for(entered.wait(), 2)
        waiters.append(asyncio.create_task(compactor.forget(session_id)))
        await asyncio.wait_for(cleaning.wait(), 2)
        waiters.append(asyncio.create_task(forget_again()))
        await asyncio.wait_for(second_started.wait(), 2)
        closing = asyncio.create_task(close())
        waiters.append(closing)
        await asyncio.wait_for(close_started.wait(), 2)
        assert not compactor.idle and not any(task.done() for task in waiters)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not interrupted.is_set() and not closing.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*waiters[:2]), 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 2)
        assert cleaned.is_set() and not interrupted.is_set() and compactor.idle
        assert store.messages(session_id) == original
        assert store.get("compaction", session_id) is None
        assert store.list("compaction_snapshot") == []
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), 2)
        await asyncio.wait_for(compactor.close(), 2)
        store.close()


async def test_normal_close_retains_adopted_summary_and_original_history(tmp_path: Path) -> None:
    store = Store(tmp_path / "journal.sqlite3")
    session_id = store.main_session()["id"]
    original = history(store, session_id)

    async def summarize(source: str, max_tokens: int) -> str:
        return "已完成的摘要，保留未知操作与原证据位置"

    compactor = Compactor(
        store,
        summarize,
        CompactionSettings,
        max_chars=lambda: 5000,
        max_jobs=lambda: 1,
        fingerprint=lambda: "same",
    )
    try:
        compactor.observe(session_id)
        await compactor.finish(session_id)
        active = store.get("compaction", session_id)
        snapshots = store.list("compaction_snapshot")
        assert active is not None and len(snapshots) == 1
        await compactor.close()
        await compactor.close()
        assert compactor.idle and store.get("compaction", session_id) == active
        assert store.list("compaction_snapshot") == snapshots
        assert store.messages(session_id) == original
        compactor.observe(session_id)
        assert await compactor.finish(session_id) is False
        with pytest.raises(ValueError, match="压缩器已关闭"):
            await compactor.forget(session_id)
    finally:
        await compactor.close()
        store.close()


@pytest.mark.parametrize("kind", ["main", "teammate"])
async def test_runtime_reconfigure_discards_revoked_summary_and_new_compactor_can_continue(
    tmp_path: Path, kind: str
) -> None:
    runtime, seen = make_runtime(
        tmp_path, [ModelTurn(content="队友工作结束"), ModelTurn(content="主助手收到队友报告")]
    )
    runtime.config.update(
        {
            "persona": {"prompt": "你是本地助手，请完成当前用户的任务。"},
            "limits": {"max_context_chars": 5000},
            "updates": {"check_on_start": False},
            "extensions": {"watch_enabled": False},
            "memory": {"auto_extract": False},
        }
    )
    await runtime.reconfigure()
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reloading: asyncio.Task[None] | None = None
    calls = 0

    class Client(FakeClient):
        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelling.set()
                    await release.wait()
                    return ModelTurn(content="已撤销的旧压缩结果")
            return ModelTurn(content="新压缩服务的有效摘要")

    try:
        main_id = runtime.main_session()["id"]
        if kind == "main":
            session_id = main_id
        else:
            child = await runtime.team.spawn("记录核查", "完成一项短工作", parent_id=main_id)
            await asyncio.wait_for(runtime.team.wait([child["id"]], timeout=2), 3)
            assert runtime.team.get(child["id"])["status"] == "completed"
            await eventually(lambda: runtime.team.idle and not runtime.tasks)
            session_id = child["session_id"]
        history(runtime.store, session_id)
        original = runtime.store.messages(session_id)
        runtime.client_factory = lambda _: Client([], seen)  # type: ignore[assignment]
        previous = runtime.compaction
        previous.observe(session_id)
        await asyncio.wait_for(entered.wait(), 2)
        reloading = asyncio.create_task(runtime.reconfigure())
        await asyncio.wait_for(cancelling.wait(), 2)
        release.set()
        await asyncio.wait_for(reloading, 2)
        assert (runtime.compaction is previous) is False
        assert previous.idle and runtime.store.get("compaction", session_id) is None
        assert runtime.store.list("compaction_snapshot") == []
        runtime.compaction.observe(session_id)
        await runtime.compaction.finish(session_id)
        active = runtime.store.get("compaction", session_id)
        assert active is not None and active["summary"] == "新压缩服务的有效摘要"
        assert calls == 2 and runtime.store.messages(session_id) == original
    finally:
        release.set()
        if reloading is not None:
            await asyncio.wait_for(asyncio.gather(reloading, return_exceptions=True), 2)
        await runtime.close()
