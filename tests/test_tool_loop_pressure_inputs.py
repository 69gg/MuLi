"""Ordinary ready adoption must retain inputs before pressure can inspect them."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_tool_loop_pressure import add_batch, configured, seeded

from muli.compaction import Candidate, digest
from muli.context import ContextLimitError, context_size
from muli.providers import Message


async def test_runtime_ready_adoption_keeps_same_run_inputs_through_final_packing(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, first = seeded(runtime)
        session = runtime.store.get("session", session_id)
        assert session is not None
        runtime.store.put("session", {**session, "status": "running"})
        runtime.config.settings.compaction.trigger_ratio = 0.5
        runtime.compaction.observe(session_id)
        await asyncio.gather(*list(runtime.compaction._tasks.values()))
        assert runtime.compaction.status(session_id)["ready"]
        assert runtime.store.get("compaction", session_id) is None
        second = runtime.store.add_message(
            session_id,
            "user",
            "同轮补充：不得覆盖最初的禁止部署要求。",
            source="web",
            principal="owner",
            run_id="same-run",
        )
        originals = runtime.store.messages(session_id)
        request = await runtime._prepare_request(session_id)
        active = runtime.store.get("compaction", session_id)
        assert active is not None and active["generation"] == 1
        assert active["covered_seq"] < originals[-3]["seq"]
        assert first in active["anchors"]
        for item in (first, second):
            matches = [
                message for message in request.messages if message.source_message_id == item["id"]
            ]
            assert len(matches) == 1 and matches[0].content == item["content"]
        assert context_size(request.messages) <= runtime.config.settings.limits.max_context_chars
        assert runtime.store.messages(session_id) == originals

        # An independently claimed next run releases the prior run's anchors.
        runtime.run_ids[session_id] = "next-run"
        latest = runtime.store.add_message(
            session_id,
            "user",
            "独立下一轮：只读核对当前状态。",
            source="web",
            principal="owner",
            run_id="next-run",
        )
        request = await runtime._prepare_request(session_id)
        assert all(message.source_message_id != first["id"] for message in request.messages)
        assert any(message.source_message_id == latest["id"] for message in request.messages)
        assert runtime.store.get("message", first["id"]) == first
    finally:
        await runtime.close()


async def test_ordinary_candidate_retains_multiple_current_run_inputs_in_its_prefix(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, first = seeded(runtime, batches=1)
        second = runtime.store.add_message(
            session_id,
            "user",
            "同轮中段补充：核完失败回执再继续。",
            source="web",
            principal="owner",
            run_id="same-run",
        )
        for index in range(1, 5):
            add_batch(runtime.store, session_id, index)
        session: dict[str, Any] | None = runtime.store.get("session", session_id)
        assert session is not None
        runtime.store.put("session", {**session, "status": "running"})
        runtime.config.settings.compaction.trigger_ratio = 0.5
        runtime.compaction.observe(session_id)
        await asyncio.gather(*list(runtime.compaction._tasks.values()))
        assert runtime.compaction.status(session_id)["ready"]
        request = await runtime._prepare_request(session_id)
        active = runtime.store.get("compaction", session_id)
        assert active is not None
        assert {item["id"] for item in active["anchors"]} == {first["id"], second["id"]}
        for item in (first, second):
            assert any(
                message.source_message_id == item["id"] and message.content == item["content"]
                for message in request.messages
            )
    finally:
        await runtime.close()


async def test_hook_cannot_silently_remove_a_current_run_input_before_pressure_detection(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, first = seeded(runtime, batches=1)
        calls = 0

        async def context(_session_id: str, messages: list[Message]) -> list[Message]:
            nonlocal calls
            calls += 1
            return [message for message in messages if message.source_message_id != first["id"]]

        runtime.context_hooks.append(context)
        with pytest.raises(ContextLimitError):
            await runtime._prepare_request(session_id)
        assert calls == 1
        assert runtime.store.get("message", first["id"]) == first
        assert runtime.store.get("compaction", session_id) is None
    finally:
        await runtime.close()


@pytest.mark.parametrize("following_run", ["same-run", "other-run", None])
async def test_runtime_loads_a_legacy_single_anchor_only_with_proven_same_run(
    tmp_path: Path, following_run: str | None
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, first = seeded(runtime, batches=2)
        prefix = runtime.store.messages(session_id)[:3]
        candidate = Candidate(
            session_id,
            0,
            0,
            prefix[-1]["seq"],
            digest(prefix),
            len(prefix),
            "旧活动摘要，原始证据仍在本会话。",
            1,
            "extractive",
            first,
            runtime._compaction_fingerprint(),
        )
        runtime.compaction._ready[session_id] = candidate
        assert runtime.compaction.adopt(session_id)
        active = runtime.store.get("compaction", session_id)
        snapshot = runtime.store.get("compaction_snapshot", candidate.snapshot_id)
        assert active is not None and snapshot is not None
        legacy = {key: value for key, value in active.items() if key != "anchors"}
        runtime.store.put_many(
            [
                ("compaction", legacy),
                (
                    "compaction_snapshot",
                    {key: value for key, value in snapshot.items() if key != "anchors"},
                ),
            ]
        )
        second = runtime.store.add_message(
            session_id,
            "user",
            "后续输入：保持目前明确的约束。",
            source="web",
            principal="owner",
            run_id=following_run,
        )
        if following_run is not None:
            runtime.run_ids[session_id] = following_run
        request = await runtime._prepare_request(session_id)
        assert any(message.source_message_id == second["id"] for message in request.messages)
        first_messages = [
            message for message in request.messages if message.source_message_id == first["id"]
        ]
        if following_run == "same-run":
            assert len(first_messages) == 1 and first_messages[0].content == first["content"]
        else:
            assert first_messages == []
        assert runtime.store.get("compaction", session_id) == legacy
        assert runtime.store.get("message", first["id"]) == first
    finally:
        await runtime.close()
