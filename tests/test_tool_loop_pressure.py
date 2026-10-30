"""Checkpoint planning, fixed-tail budgets and retained protocol boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import pytest
from test_runtime import make_runtime

from muli.compaction import Candidate, digest
from muli.context import ContextLimitError, context_size, minimum_context
from muli.providers import Message, ModelConfig
from muli.providers.codecs import build_request
from muli.runtime import Runtime
from muli.store import Store


async def configured(tmp_path: Path, *, mode: str = "extractive") -> Runtime:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "persona": {"prompt": "按当前原文与权限核查，不重做旧操作。"},
            "memory": {"enabled": False},
            "updates": {"check_on_start": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "compaction": {"enabled": True, "mode": mode, "trigger_ratio": 0.95},
            "limits": {"max_context_chars": 10000, "max_output_chars": 6000},
        }
    )
    await runtime.reconfigure()
    return runtime


def add_batch(
    store: Store,
    session_id: str,
    index: int,
    *,
    result: str = "已保存",
    protected: bool = True,
    provider_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    identifier = f"p{index}"
    call = {"id": identifier, "name": "read_probe", "arguments": json.dumps({"query": "q" * 900})}
    return [
        store.add_message(
            session_id, "assistant", "", tool_calls=[call], provider_data=provider_data or {}
        ),
        store.add_message(
            session_id, "tool", result, tool_call_id=identifier, preserve_content=protected
        ),
    ]


def fill_required_context(runtime: Runtime, session_id: str) -> Callable[[], int]:
    """Add a required host fact, independently of compaction's stored-size trigger."""
    history = runtime._history_context(
        session_id,
        stored=runtime.compaction.project(
            session_id, candidate=runtime.compaction._ready.get(session_id), adopt_ready=False
        ),
        observe=False,
    )
    size = context_size(minimum_context(history.messages))
    budget = runtime.config.settings.limits.max_context_chars
    filler = "宿" * max(1, budget + 512 - size)
    calls = 0

    async def context(_session_id: str, messages: list[Message]) -> list[Message]:
        nonlocal calls
        calls += 1
        return [messages[0], Message(role="system", content=filler), *messages[1:]]

    runtime.context_hooks.append(context)
    return lambda: calls


def seeded(runtime: Runtime, *, batches: int = 4) -> tuple[str, dict[str, Any]]:
    session_id = runtime.new_session(mode="yolo")["id"]
    runtime.run_ids[session_id] = "same-run"
    user = runtime.store.add_message(
        session_id,
        "user",
        "原始限制：保留全部参数与未知结果，只读，不部署，不外发。🙂",
        source="web",
        principal="owner",
        run_id="same-run",
        reply_origin={"kind": "web"},
        attachment_ids=["original-attachment"],
    )
    for index in range(batches):
        add_batch(runtime.store, session_id, index)
    return session_id, user


async def test_pressure_uses_actual_remaining_json_budget_and_keeps_original_anchor(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, user = seeded(runtime)
        originals = runtime.store.messages(session_id)
        calls = fill_required_context(runtime, session_id)
        request = await runtime._prepare_request(session_id)
        active = runtime.store.get("compaction", session_id)
        assert active is not None
        maximum = runtime.config.settings.limits.max_context_chars
        assert context_size(request.messages) <= maximum
        assert (
            len(active["summary"])
            < int(maximum * runtime.config.settings.compaction.target_ratio) // 2
        )
        assert calls() == 2
        assert runtime.run_ids[session_id] == "same-run"
        assert runtime.store.messages(session_id) == originals
        anchor = next(
            message for message in request.messages if message.source_message_id == user["id"]
        )
        assert anchor.content == user["content"]
        assert active["anchors"] == [user]
        latest = originals[-2:]
        assert active["covered_seq"] < latest[0]["seq"]
        assert [
            message.source_message_id
            for message in request.messages
            if message.source_message_id in {item["id"] for item in latest}
        ] == [item["id"] for item in latest]
    finally:
        await runtime.close()


@pytest.mark.parametrize("state", ["ready", "active"])
async def test_oversized_derived_summary_can_shrink_without_advancing_cutoff(
    tmp_path: Path, state: str
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, user = seeded(runtime, batches=2)
        prefix = runtime.store.messages(session_id)[:3]
        candidate = Candidate(
            session_id,
            0,
            0,
            prefix[-1]["seq"],
            digest(prefix),
            len(prefix),
            '"\\🙂' * 800,
            1,
            "extractive",
            user,
            runtime._compaction_fingerprint(),
        )
        runtime.compaction._ready[session_id] = candidate
        if state == "active":
            assert runtime.compaction.adopt(session_id)
        fill_required_context(runtime, session_id)
        request = await runtime._prepare_request(session_id)
        active = runtime.store.get("compaction", session_id)
        assert active is not None
        assert active["generation"] == 2 and active["level"] == 2
        assert active["covered_seq"] == candidate.cutoff
        assert active["parent_snapshot_id"] == candidate.snapshot_id
        assert len(active["summary"]) < len(candidate.summary)
        assert context_size(request.messages) <= runtime.config.settings.limits.max_context_chars
        snapshots = runtime.store.list("compaction_snapshot")
        assert len(snapshots) == 2
    finally:
        await runtime.close()


@pytest.mark.parametrize("protected", [False, True])
async def test_latest_whole_batch_and_following_owner_inputs_are_not_consumed(
    tmp_path: Path, protected: bool
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, user = seeded(runtime, batches=2)
        latest = add_batch(runtime.store, session_id, 2, protected=protected)
        additions = [
            runtime.store.add_message(
                session_id, "user", text, principal="owner", source="web", run_id="same-run"
            )
            for text in ("补充：先保留草稿。", "再补充：任何未知结果都要先查回执。")
        ]
        candidate = await runtime.compaction.pressure_candidate(session_id, [user, *additions])
        assert candidate is not None and candidate.cutoff < latest[0]["seq"]
        projected = runtime.compaction.project(session_id, candidate=candidate)
        by_id = {item["id"]: item for item in projected}
        for item in [user, *latest, *additions]:
            assert by_id[item["id"]] == item
        assert runtime.store.get("compaction", session_id) is None
    finally:
        await runtime.close()


async def test_required_callback_error_never_restarts_hooks_or_installs_a_checkpoint(
    tmp_path: Path,
) -> None:
    runtime = await configured(tmp_path)
    error = ContextLimitError("扩展自己的错误")
    calls = 0

    async def broken(_session_id: str, _messages: list[Message]) -> list[Message]:
        nonlocal calls
        calls += 1
        raise error

    try:
        session_id, _ = seeded(runtime)
        runtime.context_hooks.append(broken)
        with pytest.raises(ContextLimitError) as caught:
            await runtime._prepare_request(session_id)
        assert caught.value is error and calls == 1
        assert not runtime.store.list("compaction_snapshot")
    finally:
        await runtime.close()


@pytest.mark.parametrize("boundary", ["disabled", "single-user", "unconsumed-result"])
async def test_unrecoverable_pressure_does_not_install_or_call_a_summary_model(
    tmp_path: Path, boundary: str
) -> None:
    runtime = await configured(tmp_path, mode="auto")
    model_calls = 0

    async def summarize(_source: str, _tokens: int) -> str:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("必保尾部不够时不能请求摘要模型")

    runtime.compaction.summarize = summarize
    try:
        if boundary == "single-user":
            session_id = runtime.new_session()["id"]
            runtime.store.add_message(session_id, "user", "必" * 11000)
        else:
            session_id, _ = seeded(runtime)
            if boundary == "unconsumed-result":
                add_batch(runtime.store, session_id, 5, result="原" * 11000, protected=True)
            else:
                runtime.config.settings.compaction.enabled = False
                fill_required_context(runtime, session_id)
        with pytest.raises(ContextLimitError):
            await runtime._prepare_request(session_id)
        assert model_calls == 0
        assert runtime.store.get("compaction", session_id) is None
        assert not runtime.compaction._ready
    finally:
        await runtime.close()


@pytest.mark.parametrize("boundary", ["owner-input", "forget", "close", "fingerprint", "cancel"])
async def test_late_semantic_result_cannot_install_an_unapproved_pressure_plan(
    tmp_path: Path, boundary: str
) -> None:
    runtime = await configured(tmp_path, mode="auto")
    entered, release = asyncio.Event(), asyncio.Event()

    async def summarize(_source: str, _tokens: int) -> str:
        entered.set()
        await release.wait()
        return '摘要含转义 "\\ 和 🙂，未知回执仍未知。' * 100

    runtime.compaction.summarize = summarize
    preparing: asyncio.Task[Any] | None = None
    try:
        session_id, _ = seeded(runtime)
        fill_required_context(runtime, session_id)
        preparing = asyncio.create_task(runtime._prepare_request(session_id))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if boundary == "owner-input":
            runtime.store.enqueue_input(
                {
                    "id": "new-owner-input",
                    "session_id": session_id,
                    "content": "另一渠道的新要求：停止旧操作，先看本条。",
                    "source": "web",
                    "principal": "owner",
                    "reply_origin": {"kind": "web"},
                },
                max_pending=runtime.config.settings.limits.max_pending_runs,
            )
        elif boundary == "forget":
            await runtime.compaction.forget(session_id)
        elif boundary == "close":
            await runtime.compaction.close()
        elif boundary == "fingerprint":
            runtime.config.settings.compaction.target_ratio = 0.3
        else:
            preparing.cancel()
        release.set()
        with pytest.raises((ContextLimitError, asyncio.CancelledError)):
            await asyncio.wait_for(preparing, timeout=2)
        assert not runtime.store.list("compaction_snapshot")
        assert runtime.store.get("compaction", session_id) is None
        if boundary == "owner-input":
            assert runtime.store.pending_inputs(session_id) == 1
        await runtime.compaction.close()
        assert not runtime.compaction._tasks and not runtime.compaction._ready
    finally:
        release.set()
        if preparing is not None and not preparing.done():
            preparing.cancel()
            await asyncio.gather(preparing, return_exceptions=True)
        await runtime.close()


def native(
    protocol: Literal["openai_chat", "openai_responses", "anthropic"],
    identifier: str,
    arguments: str,
) -> dict[str, Any]:
    if protocol == "openai_chat":
        return {
            "protocol": protocol,
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning_content": "原生思考",
                "tool_calls": [
                    {
                        "id": identifier,
                        "type": "function",
                        "function": {"name": "read_probe", "arguments": arguments},
                    }
                ],
            },
        }
    if protocol == "openai_responses":
        return {
            "protocol": protocol,
            "output_items": [
                {"type": "reasoning", "id": "opaque-native", "encrypted_content": "signed-native"},
                {
                    "type": "function_call",
                    "id": "native-item",
                    "call_id": identifier,
                    "name": "read_probe",
                    "arguments": arguments,
                },
            ],
        }
    return {
        "protocol": protocol,
        "content_blocks": [
            {"type": "thinking", "thinking": "原生思考", "signature": "signed-native"},
            {
                "type": "tool_use",
                "id": identifier,
                "name": "read_probe",
                "input": json.loads(arguments),
            },
        ],
    }


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
async def test_pressure_retains_the_latest_native_batch_in_each_real_codec(
    tmp_path: Path, protocol: Literal["openai_chat", "openai_responses", "anthropic"]
) -> None:
    runtime = await configured(tmp_path)
    try:
        session_id, _ = seeded(runtime, batches=3)
        arguments = json.dumps({"query": "q" * 900})
        latest = add_batch(
            runtime.store, session_id, 3, provider_data=native(protocol, "p3", arguments)
        )
        canonical = runtime._history_context(session_id, observe=False).messages
        retained = [
            message
            for message in canonical
            if message.source_message_id in {item["id"] for item in latest}
        ]
        original = deepcopy(retained)
        fill_required_context(runtime, session_id)
        request = await runtime._prepare_request(session_id)
        suffix = [
            message
            for message in request.messages
            if message.source_message_id in {item["id"] for item in latest}
        ]
        assert suffix == original
        config = ModelConfig(
            model="offline",
            base_url="https://unused.invalid/v1",
            protocol=protocol,
            max_output_tokens=256,
        )
        actual = build_request(config, request.messages, [], stream=False)
        expected = build_request(config, original, [], stream=False)
        key = "input" if protocol == "openai_responses" else "messages"
        assert actual[key][-len(expected[key]) :] == expected[key]
    finally:
        await runtime.close()
