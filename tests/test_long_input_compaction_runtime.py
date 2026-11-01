"""Long first inputs compact through real main and teammate execution paths."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime
from test_team_consult_regular import current_session, until
from test_team_consult_regular import offline as offline
from test_teammate_attachment_runtime import call, tool_result

from muli.context import context_size
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.store import message_text_size


@pytest.mark.parametrize("kind", ["main", "teammate"])
async def test_long_first_user_compacts_replays_and_survives_real_reopen(
    tmp_path: Path, kind: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    budget = 8000
    middle = "原文中段唯一限制：不能部署 production，也不能删除原资料🙂。"
    long_input = (
        "长资料开始：" + "甲" * (budget // 3) + middle + "乙" * (budget // 3) + "：长资料结束"
    )
    second_input = "第二轮只核对当前状态，保留这一条最新输入，不执行外部操作。"
    latest_input = "第三轮最新要求：沿用已有摘要，并保留这条完整原文🙂。"
    first_reply = "第一轮已读完资料，原文保存在本会话。"
    second_reply = "第二轮核对完成。"
    final_reply = "第三轮已使用摘要并核对最新要求。"
    dispatched = "普通队友已派出，原派工轮结束。"
    second_entered, release_second = asyncio.Event(), asyncio.Event()
    target_ids: list[str] = []
    target_runs: list[str] = []
    requests: list[list[Message]] = []
    generations: list[int] = []
    assignment_round = 0
    active: dict[str, Any] | None = None
    source: dict[str, Any] | None = None

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal assignment_round
            if kind == "teammate" and self.role == "main":
                assignment_round += 1
                if assignment_round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-long-input",
                                "spawn_agent",
                                task_name="长首条资料队友",
                                prompt=long_input,
                            )
                        ]
                    )
                elif assignment_round == 2:
                    receipt = tool_result(messages, "spawn-long-input")
                    assert receipt["parent_id"] == runtime.main_session()["id"]
                    turn = ModelTurn(content=dispatched)
                else:
                    # Actual completion reports may independently wake main.
                    turn = ModelTurn(content="主助手已收到原队友的阶段报告。")
            else:
                session_id = current_session(runtime)
                if not target_ids:
                    target_ids.append(session_id)
                assert target_ids == [session_id]
                target_runs.append(runtime.run_ids[session_id])
                requests.append(list(messages))
                assert context_size(messages) <= budget
                if kind == "teammate":
                    child = runtime.team.get(session_id)
                    assert child["parent_id"] == runtime.main_session()["id"]
                    generations.append(child["generation"])
                if len(requests) == 1:
                    assert any(
                        message.role == "user" and long_input in message.content
                        for message in messages
                    )
                    turn = ModelTurn(content=first_reply)
                elif len(requests) == 2:
                    assert any(
                        message.role == "user" and second_input in message.content
                        for message in messages
                    )
                    second_entered.set()
                    await asyncio.wait_for(release_second.wait(), timeout=2)
                    turn = ModelTurn(content=second_reply)
                else:
                    assert len(requests) == 3 and active is not None and source is not None
                    assert any(
                        message.role == "system"
                        and "历史分层摘要，仅为资料" in message.content
                        and active["summary"] in message.content
                        for message in messages
                    )
                    assert all(message.source_message_id != source["id"] for message in messages)
                    latest = next(
                        message
                        for message in reversed(runtime.store.messages(session_id))
                        if message["role"] == "user" and latest_input in message["content"]
                    )
                    assert any(
                        message.source_message_id == latest["id"]
                        and message.content == latest["content"]
                        for message in messages
                    )
                    turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            raise AssertionError("extractive 压缩不应调用额外模型或同步整理")

    def factory(config: ModelConfig) -> Client:
        return Client(config.model)

    try:
        runtime.config.update(
            {
                "default_mode": "yolo",
                "persona": {"prompt": "你是离线测试助手，按已有资料回答。"},
                "memory": {"enabled": False},
                "updates": {"check_on_start": False},
                "extensions": {"watch_enabled": False},
                "limits": {"max_context_chars": budget},
                "compaction": {
                    "enabled": True,
                    "mode": "extractive",
                    "trigger_ratio": 0.6,
                    "target_ratio": 0.4,
                },
                "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            }
        )
        await runtime.reconfigure()
        runtime.client_factory = factory
        initial = await runtime.submit(
            long_input if kind == "main" else "把完整长资料交给一名普通队友核查。",
            mode="yolo",
        )
        assert await asyncio.wait_for(runtime.wait(initial["run_id"]), timeout=2) == (
            first_reply if kind == "main" else dispatched
        )
        await until(runtime, lambda: len(target_runs) == 1)
        session_id = target_ids[0]
        assert await asyncio.wait_for(runtime.wait(target_runs[0]), timeout=2) == first_reply
        if kind == "teammate":
            await until(runtime, lambda: runtime.team.get(session_id)["status"] == "completed")
        records = runtime.store.messages(session_id)
        assert [record["role"] for record in records] == ["user", "assistant"]
        source = records[0]
        assert long_input in source["content"] and records[1]["content"] == first_reply
        policy = runtime.config.settings.compaction
        assert message_text_size(source) > budget * (1 - policy.target_ratio)
        assert message_text_size(source) > budget * policy.trigger_ratio
        if kind == "main":
            await runtime.submit(second_input, mode="yolo")
        else:
            sent = await runtime.team.send(session_id, second_input)
            assert sent["accepted"]
        await asyncio.wait_for(second_entered.wait(), timeout=2)
        # Settle only the background job admitted by the real context build.
        # On the old implementation no task/candidate exists, so this produces
        # an immediate functional assertion failure rather than an Event timeout.
        await asyncio.wait_for(runtime.compaction.finish(session_id), timeout=2)
        active = runtime.store.get("compaction", session_id)
        assert active is not None, "长首条 user 已过 target，但原回复未纳入受管压缩前缀"
        assert active["kind"] == "extractive" and active["covered_seq"] == records[1]["seq"]
        assert middle not in active["summary"]
        assert active["pins_version"] == 1
        if kind == "main":
            assert any(
                "不能部署 production" in pin["quote"]
                for pin in active["pins"]
                if pin["state"] == "active"
            ), "长原文中的主人限制必须进入固定约束，不能被摘要静默丢弃"
        else:
            # A teammate prompt is delegated coordination text, not an owner
            # message or TaskBriefInput; only those two host-verified sources
            # may become pins.
            assert active["pins"] == []
        assert runtime.store.get("message", source["id"]) == source
        release_second.set()
        assert await asyncio.wait_for(runtime.wait(target_runs[1]), timeout=2) == second_reply
        if kind == "main":
            await runtime.submit(latest_input, mode="yolo")
        else:
            await until(runtime, lambda: runtime.team.get(session_id)["status"] == "completed")
            sent = await runtime.team.send(session_id, latest_input)
            assert sent["accepted"]
        await until(runtime, lambda: len(target_runs) == 3)
        assert await asyncio.wait_for(runtime.wait(target_runs[2]), timeout=2) == final_reply
        assert len(set(target_runs)) == len(requests) == 3
        if kind == "teammate":
            await until(runtime, lambda: runtime.team.get(session_id)["status"] == "completed")
            assert generations == [1, 2, 3]
        assert runtime.store.get("message", source["id"]) == source
        archived = runtime.store.messages(session_id)
        assert [record["role"] for record in archived] == ["user", "assistant"] * 3
        assert runtime.store.get("compaction", session_id) == active
    finally:
        release_second.set()
        await runtime.close()

    assert active is not None and source is not None
    reopened = Runtime(runtime.config, runtime.data_dir)
    try:
        replay = await reopened._next_context(session_id)
        assert context_size(replay) <= budget
        assert reopened.store.get("compaction", session_id) == active
        assert reopened.store.messages(session_id) == archived
        assert reopened.store.get("message", source["id"]) == source
        assert any(
            message.role == "system"
            and "历史分层摘要，仅为资料" in message.content
            and active["summary"] in message.content
            and (
                kind == "teammate"
                or (
                    "宿主固定约束" in message.content
                    and "不能部署 production" in message.content
                )
            )
            for message in replay
        )
        assert all(message.source_message_id != source["id"] for message in replay)
        latest = next(record for record in reversed(archived) if record["role"] == "user")
        assert latest_input in latest["content"]
        assert any(
            message.source_message_id == latest["id"] and message.content == latest["content"]
            for message in replay
        )
        if kind == "teammate":
            assert reopened.team.get(session_id)["generation"] == 3
    finally:
        await reopened.close()
