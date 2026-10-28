"""正常重开与完成后预算收紧不得吞掉尚未接纳的资料线索。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
from test_attention import attention_inputs, eventually, reminder
from test_memory_watch_attention import components, matched
from test_runtime import make_runtime

from muli.attachment_memory_source import attachment_source
from muli.providers import ModelTurn
from muli.server import create_app
from muli.store import new_id, now


async def test_generated_match_waits_when_source_budget_shrinks_and_resumes_once(
    tmp_path: Path,
) -> None:
    rig = components(tmp_path)
    try:
        # Drive finite consumer steps without starting its background task.
        rig.inbox._closing = False
        proof, event = await matched(rig, text="词甲" + "连续原文" * 280 + "词乙")
        rig.runtime.config.update({"memory": {"max_source_chars": 1000}})
        for _ in range(32):
            if not await rig.inbox._consume():
                break
        else:
            raise AssertionError("有限事件页未到达等待边界")
        assert rig.inbox.cursor < event["id"]
        assert rig.inbox.status()["waiting"] == "budget"
        assert attention_inputs(rig.runtime) == []
        rig.runtime.config.update({"memory": {"max_source_chars": 64000}})
        for _ in range(32):
            if attention_inputs(rig.runtime):
                break
            await rig.inbox._consume()
        inputs = attention_inputs(rig.runtime)
        assert len(inputs) == 1 and inputs[0]["attention_memory_watch"] == proof.dump()
    finally:
        await rig.close()


@pytest.mark.parametrize("coalesce_seconds", [0, 0.001])
async def test_waiting_watch_does_not_block_an_earlier_ordinary_notice(
    tmp_path: Path, coalesce_seconds: float
) -> None:
    rig = components(tmp_path)
    try:
        rig.inbox._closing = False
        rig.runtime.config.update(
            {"attention": {"events": ["memory", "reminder"], "coalesce_seconds": coalesce_seconds}}
        )
        await reminder(rig.runtime, "先处理这条普通提醒")
        proof, event = await matched(rig, text="词甲" + "连续原文" * 280 + "词乙")
        rig.runtime.config.update({"memory": {"max_source_chars": 1000}})
        for _ in range(32):
            if not await rig.inbox._consume():
                break
        else:
            raise AssertionError("有限事件页未到达预算等待边界")
        rows = attention_inputs(rig.runtime)
        assert len(rows) == 1 and "先处理这条普通提醒" in rows[0]["content"]
        assert rig.inbox.cursor < event["id"]
        assert rig.inbox.status()["waiting"] == "budget"
        main_id = rig.runtime.main_session()["id"]
        claimed = rig.runtime.store.claim_inputs(main_id, "ordinary-prefix", limit=1)
        assert len(claimed) == 1 and claimed[0]["id"] == rows[0]["id"]
        rig.runtime.config.update({"memory": {"max_source_chars": 64000}})
        for _ in range(32):
            if len(attention_inputs(rig.runtime)) == 2:
                break
            await rig.inbox._consume()
        rows = attention_inputs(rig.runtime)
        assert len(rows) == 2 and rows[1]["attention_memory_watch"] == proof.dump()
    finally:
        await rig.close()


async def test_real_application_reopen_keeps_accepted_pending_lead_through_startup_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("正常重开资料关注不能连接真实外部服务")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    first, _ = make_runtime(tmp_path, [])
    first.config.update(
        {
            "models": {"main": {"model": ""}},
            "memory": {"auto_extract": False},
            "attention": {"events": ["memory"], "coalesce_seconds": 0},
            "updates": {"check_on_start": False},
            "compaction": {"enabled": False},
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"watch_enabled": False, "mcp_directory": str(tmp_path / "mcp.d")},
        }
    )
    app = create_app(first.config, first.data_dir, first)
    input_id = ""
    try:
        async with app.router.lifespan_context(app):
            main_id = first.main_session()["id"]
            owner, _ = first.conversation.enqueue(
                main_id,
                "留意后续词甲词乙的资料",
                source="web",
                principal="owner",
                mode="ask",
                max_pending=first.config.settings.limits.max_pending_runs,
            )
            assert len(first.store.claim_inputs(main_id, "recovery-owner", limit=1)) == 1
            application = app.state.memory_application
            application.watches.create(
                main_id, owner["id"], "关注词甲词乙", ["词甲", "词乙"], key="reopen"
            )
            text = "新收藏中词甲与词乙有了可核对的原文线索。"
            source = attachment_source(
                new_id(), text, hashlib.sha256(text.encode()).hexdigest(), now()
            )
            application.pipeline.ingest(source)
            await eventually(lambda: len(attention_inputs(first)) == 1)
            (pending,) = attention_inputs(first)
            assert pending["state"] == "pending" and not first.tasks
            input_id = pending["id"]
        # Inspect the actual normally closed database, without reopening Runtime yet.
        path = first.data_dir / "muli.sqlite3"
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            assert (
                connection.execute("SELECT state FROM ingress WHERE id=?", (input_id,)).fetchone()[
                    0
                ]
                == "pending"
            )
    finally:
        await first.close()

    answer = "重开后继续带出处回访这条资料线索。"
    reopened, seen = make_runtime(tmp_path, [ModelTurn(content=answer)])
    current = create_app(reopened.config, reopened.data_dir, reopened)
    original = reopened.flush_task_plan_deliveries
    yielded = False

    async def flush() -> None:
        nonlocal yielded
        if reopened.memory_watch_guard is None:
            yielded = True
            # Exercise the real startup gap, rather than relying on lucky scheduling.
            for _ in range(8):
                await asyncio.sleep(0)
        await original()

    monkeypatch.setattr(reopened, "flush_task_plan_deliveries", flush)
    try:
        async with current.router.lifespan_context(current):
            assert yielded
            await eventually(lambda: not reopened.tasks)
            rows = attention_inputs(reopened)
            assert len(rows) == 1 and rows[0]["id"] == input_id
            assert rows[0]["state"] == "delivered"
            assert len(seen) == 1
            assert any("关注词甲词乙" in message.content for message in seen[0])
            messages = reopened.store.messages(reopened.main_session()["id"])
            assert [item["content"] for item in messages if item["role"] == "assistant"] == [answer]
    finally:
        await reopened.close()
