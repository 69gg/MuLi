from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from typing import Any

import pytest
from test_memory_context import fact, records
from test_memory_runtime_quality import (
    MEMORY_HEADING,
    memory_payload,
    memory_records,
)
from test_memory_runtime_quality import (
    memory_runtime as memory_runtime,
)
from test_runtime import FakeClient

from muli.context import ContextLimitError, context_size
from muli.memory_application import MemoryApplication
from muli.memory_context import MemoryContextProvider, _PromptBudget, render_context_result
from muli.providers import Message, ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.tools import Tool, parameters

MemoryRuntime = tuple[Runtime, MemoryApplication, list[list[Message]]]
ESCAPED_TEXT = '中文🙂"\\\n\t\u0001'


def optional_cost(payload: str) -> int:
    required = [Message(role="user", content="必要输入")]
    material = Message(role="system", content=MEMORY_HEADING + payload)
    return context_size([*required, material]) - context_size(required)


def one_unit_reserve(payload: str) -> int:
    prefix, *_ = payload.splitlines()
    first = records(payload)[0]
    minimum = prefix + "\n" + json.dumps({**first, "content": "[节选]"}, ensure_ascii=False)
    # A complete metadata unit plus a little body, measured after JSON escaping.
    return optional_cost(minimum) + len(ESCAPED_TEXT) * 8


def pad_required(messages: list[Message], maximum: int, reserve: int) -> list[Message]:
    assert all(MEMORY_HEADING not in message.content for message in messages)
    skill = Message(role="system", content="完整固定技能指导：")
    required = [*messages, skill]
    padding = maximum - reserve - context_size(required)
    assert padding >= 0
    skill.content += "界" * padding
    assert context_size(required) == maximum - reserve
    return required


async def start_and_wait(runtime: Runtime, session_id: str, query: str) -> None:
    receipt = await runtime.start(session_id, query, mode="yolo")
    assert await asyncio.wait_for(runtime.wait(receipt["run_id"]), 5) == "已核对"
    run = runtime.store.get("run", receipt["run_id"])
    assert run is not None and run["status"] == "completed"


@pytest.mark.parametrize("fallback", [False, True], ids=["application", "runtime-fallback"])
async def test_real_json_budget_keeps_a_complete_unit_then_restores_both_streams(
    memory_runtime: MemoryRuntime, fallback: bool
) -> None:
    runtime, application, seen = memory_runtime
    query = "JSONBUDGET"
    procedure = fact(
        runtime.memory, "budget-procedure", query + " " + ESCAPED_TEXT * 900, "procedural"
    )
    preference = fact(runtime.memory, "budget-profile", "偏好 " + ESCAPED_TEXT * 900, "preference")
    originals = {
        item["id"]: deepcopy(runtime.memory.get(item["id"])) for item in (procedure, preference)
    }
    settings = runtime.config.settings
    maximum = settings.limits.max_context_chars
    soft_limit = min(settings.memory.context_max_chars, maximum // 4 - len(MEMORY_HEADING))
    broad = application.pipeline.cached_context(query, max_chars=soft_limit)
    reserve = one_unit_reserve(broad)
    assert reserve < optional_cost(broad)
    if fallback:
        runtime.memory_context_provider = None
    tight = True
    hooks_seen: list[list[Message]] = []
    skills_seen: list[list[Message]] = []
    extension = Message(role="developer", content="扩展提供的完整说明。")

    async def hook(session_id: str, messages: list[Message]) -> list[Message]:
        assert all(MEMORY_HEADING not in message.content for message in messages)
        hooks_seen.append(deepcopy(messages))
        return [deepcopy(extension), *messages]

    async def skills(session_id: str, messages: list[Message]) -> list[Message]:
        assert all(MEMORY_HEADING not in message.content for message in messages)
        skills_seen.append(deepcopy(messages))
        return pad_required(messages, maximum, reserve) if tight else messages

    runtime.context_hooks.append(hook)
    runtime.skills_context = skills
    session = runtime.new_session(mode="yolo")
    await start_and_wait(runtime, session["id"], query)
    restricted = seen[-1]
    restricted_payload = memory_payload(restricted)
    selected = memory_records(restricted)
    assert [item["id"] for item in selected] == [procedure["id"]]
    without_memory = [message for message in restricted if MEMORY_HEADING not in message.content]
    assert context_size(without_memory) == maximum - reserve
    assert 0 < context_size(restricted) - context_size(without_memory) <= reserve
    assert context_size(restricted) <= maximum
    assert len(json.dumps(restricted_payload, ensure_ascii=False)) - 2 > len(restricted_payload)

    tight = False
    await start_and_wait(runtime, session["id"], query)
    restored = seen[-1]
    assert memory_payload(restored) == broad
    assert len(restricted_payload) < len(memory_payload(restored))
    assert [item["id"] for item in memory_records(restored)] == [procedure["id"], preference["id"]]
    assert ESCAPED_TEXT in memory_records(restored)[0]["content"]
    assert len(hooks_seen) == len(skills_seen) == len(seen) == 2
    for request in seen:
        assert extension in request and context_size(request) <= maximum
        material_index = next(
            index for index, message in enumerate(request) if MEMORY_HEADING in message.content
        )
        assert all(message.role in {"system", "developer"} for message in request[:material_index])
        for item in memory_records(request):
            original = originals[item["id"]]
            assert original is not None
            for key in (
                "id",
                "source",
                "source_id",
                "layer",
                "trusted_owner",
                "status",
                "conflicts",
                "updated_at",
            ):
                assert item[key] == original[key]
            assert original["content"].startswith(item["content"].removesuffix("[节选]"))
    assert settings.memory.enabled is True
    assert runtime.config.settings.memory.context_max_chars == settings.memory.context_max_chars
    assert application.pipeline.config.context_max_chars == settings.memory.context_max_chars
    assert application.pipeline._worker is None
    for identifier, original in originals.items():
        assert runtime.memory.get(identifier) == original


async def test_removable_old_history_does_not_starve_automatic_memory(
    memory_runtime: MemoryRuntime,
) -> None:
    runtime, application, seen = memory_runtime
    procedure = fact(
        runtime.memory, "history-procedure", "HISTORYBUDGET 先校验再归档", "procedural"
    )
    profile = fact(runtime.memory, "history-profile", "我偏好简洁中文", "preference")
    session = runtime.new_session(mode="yolo")
    maximum = runtime.config.settings.limits.max_context_chars
    old = runtime.store.add_message(session["id"], "user", "旧任务" * maximum)
    runtime.store.add_message(session["id"], "assistant", "旧任务已完成")
    await start_and_wait(runtime, session["id"], "HISTORYBUDGET")
    assert {item["id"] for item in memory_records(seen[-1])} == {procedure["id"], profile["id"]}
    assert context_size(seen[-1]) <= maximum
    assert any(item["id"] == old["id"] for item in runtime.store.messages(session["id"]))
    assert application.pipeline._worker is None


@pytest.mark.parametrize("body", ['""', '"a'], ids=["equal-raw-cost", "shorter-raw-cost"])
async def test_minimum_unit_uses_both_raw_and_request_json_costs(
    memory_runtime: MemoryRuntime, body: str
) -> None:
    runtime, _, _ = memory_runtime
    item = fact(runtime.memory, "quoted-body", body, "procedural")
    full = render_context_result(
        "quoted", [item], [], limit=1, budget=6000, memory=runtime.memory
    ).text
    page = records(full)[0]
    prefix, *_ = full.splitlines()
    excerpt = prefix + "\n" + json.dumps({**page, "content": "[节选]"}, ensure_ascii=False)
    budget = _PromptBudget(
        char_limit=len(excerpt), json_limit=len(json.dumps(excerpt, ensure_ascii=False)) - 2
    )
    assert len(full) <= budget.char_limit
    assert not budget.fits(full) and budget.fits(excerpt)
    narrowed = render_context_result(
        "quoted",
        [item],
        [],
        limit=1,
        budget=budget.char_limit,
        memory=runtime.memory,
        prompt_budget=budget,
    ).text
    assert narrowed and budget.fits(narrowed)
    assert records(narrowed, budget=budget.char_limit) == [{**page, "content": "[节选]"}]
    assert runtime.memory.get(item["id"]) == item


@pytest.mark.parametrize("protected", ["pinned", "native", "pending-result"])
async def test_required_floor_overflow_never_calls_memory_provider_or_changes_evidence(
    memory_runtime: MemoryRuntime, protected: str
) -> None:
    runtime, _, seen = memory_runtime
    maximum = runtime.config.settings.limits.max_context_chars
    session = runtime.new_session(mode="yolo")
    runtime.store.add_message(session["id"], "user", "核对必要资料")
    calls: list[str] = []

    def provider(query: str) -> str:
        calls.append(query)
        return "可选资料"

    runtime.memory_context_provider = provider
    if protected == "pinned":

        async def pinned(session_id: str, messages: list[Message]) -> list[Message]:
            return [*messages, Message(role="system", content="必要说明" + "界" * maximum)]

        runtime.context_hooks.append(pinned)
    else:
        arguments = json.dumps({"raw": "必要参数" * maximum}) if protected == "native" else "{}"
        call = ToolCall("protected", "budget_evidence", arguments)
        runtime.store.add_message(
            session["id"],
            "assistant",
            "",
            tool_calls=[asdict(call)],
            reasoning="原生推理🙂\\",
            provider_data={"opaque": ["原生签名", "\n\u0001"]},
        )
        body = (
            json.dumps({"evidence": "完整证据" * maximum}, ensure_ascii=False)
            if protected == "pending-result"
            else "{}"
        )
        runtime.store.add_message(
            session["id"],
            "tool",
            body,
            tool_call_id=call.id,
            preserve_content=protected == "pending-result",
            content_provenance="accepted",
        )
        if protected == "pending-result":
            runtime.store.add_message(session["id"], "user", "主人追加：先读完整回执")
    original = deepcopy(runtime.store.messages(session["id"]))
    with pytest.raises(ContextLimitError):
        await runtime._next_context(session["id"])
    assert calls == [] and seen == []
    assert runtime.store.messages(session["id"]) == original


@pytest.mark.parametrize("named", [False, True], ids=["legacy", "old-named-renderer"])
async def test_legacy_json_expansion_is_gated_once_without_guessing_callback_signature(
    memory_runtime: MemoryRuntime, named: bool
) -> None:
    runtime, _, seen = memory_runtime
    payload = ESCAPED_TEXT * 80
    maximum = runtime.config.settings.limits.max_context_chars
    reserve = optional_cost(payload) - 1
    assert len(payload) < maximum // 4 - len(MEMORY_HEADING)
    calls: list[str] = []

    def legacy(query: str) -> str:
        calls.append(query)
        return payload

    def renderer(
        query: str, max_chars: int | None = None, *, latest_user_id: str | None = None
    ) -> str:
        calls.append(query)
        assert max_chars is not None and latest_user_id is not None
        return payload

    async def skills(session_id: str, messages: list[Message]) -> list[Message]:
        return pad_required(messages, maximum, reserve)

    runtime.memory_context_provider = MemoryContextProvider(renderer) if named else legacy
    runtime.skills_context = skills
    session = runtime.new_session(mode="yolo")
    await start_and_wait(runtime, session["id"], "旧接口只调用一次")
    assert calls == ["旧接口只调用一次"]
    assert all(MEMORY_HEADING not in message.content for message in seen[-1])
    assert context_size(seen[-1]) == maximum - reserve


@pytest.mark.parametrize("named", [False, True], ids=["legacy", "old-named-renderer"])
@pytest.mark.parametrize("kind", ["type", "context"])
async def test_provider_errors_are_not_compaction_signals_or_retried(
    memory_runtime: MemoryRuntime, monkeypatch: pytest.MonkeyPatch, named: bool, kind: str
) -> None:
    runtime, _, seen = memory_runtime
    calls: list[str] = []
    compactions: list[str] = []
    failure = (
        TypeError("provider failed internally")
        if kind == "type"
        else ContextLimitError("provider owns this error")
    )

    def legacy(query: str) -> str:
        calls.append(query)
        raise failure

    def renderer(
        query: str, max_chars: int | None = None, *, latest_user_id: str | None = None
    ) -> str:
        return legacy(query)

    async def finish(session_id: str) -> bool:
        compactions.append(session_id)
        return True

    runtime.memory_context_provider = MemoryContextProvider(renderer) if named else legacy
    monkeypatch.setattr(runtime.compaction, "finish", finish)
    session = runtime.new_session(mode="yolo")
    runtime.store.add_message(session["id"], "user", "不要重试回调")
    with pytest.raises(type(failure)) as caught:
        await runtime._next_context(session["id"])
    assert caught.value is failure
    assert calls == ["不要重试回调"] and compactions == [] and seen == []


@pytest.mark.parametrize("entrypoint", ["team", "workflow"])
async def test_worker_and_workflow_share_budgeting_with_complete_tool_continuations(
    memory_runtime: MemoryRuntime, entrypoint: str
) -> None:
    runtime, application, seen = memory_runtime
    query = "WORKERBUDGET"
    procedure = fact(
        runtime.memory, "worker-procedure", query + " " + ESCAPED_TEXT * 900, "procedural"
    )
    fact(runtime.memory, "worker-profile", "我的偏好是简洁中文", "preference")
    maximum = runtime.config.settings.limits.max_context_chars
    reserve = one_unit_reserve(
        application.pipeline.cached_context(query, max_chars=maximum // 4 - len(MEMORY_HEADING))
    )
    result = {"evidence": ESCAPED_TEXT * 70, "revision": "stable-evidence"}
    encoded = json.dumps(result, ensure_ascii=False)
    call = ToolCall("worker-read", "budget_evidence", "{}")
    native = ModelTurn(
        tool_calls=[call],
        reasoning="完整推理🙂\\\n",
        provider_data={"opaque": ["完整签名", "\u0001"]},
    )
    turns = [native, ModelTurn(content="已核对")]
    runtime.client_factory = lambda _: FakeClient(turns, seen)  # type: ignore[assignment]
    tool_calls = 0
    skills_calls = 0

    async def evidence(args: dict[str, Any]) -> dict[str, str]:
        nonlocal tool_calls
        tool_calls += 1
        return result

    async def skills(session_id: str, messages: list[Message]) -> list[Message]:
        nonlocal skills_calls
        skills_calls += 1
        return pad_required(messages, maximum, reserve)

    runtime.add_tool_provider(
        lambda _: [
            Tool(
                "budget_evidence",
                "返回完整本地证据",
                parameters({}),
                evidence,
                preserve_output=True,
            )
        ]
    )
    runtime.skills_context = skills
    if entrypoint == "team":
        parent = runtime.new_session(mode="yolo")
        agent = await runtime.team.spawn("预算队友", query, parent_id=parent["id"])
        runner = runtime.team._active[agent["id"]]
        await asyncio.wait_for(asyncio.shield(runner), 5)
        completed = runtime.team.get(agent["id"])
        assert completed["status"] == "completed" and completed["result"] == "已核对"
        session_id = completed["session_id"]
        run_id = completed["run_id"]
    else:
        session = runtime.new_session(mode="yolo")
        session_id = session["id"]
        runtime.store.put("session", {**session, "kind": "workflow", "role": "worker"})
        task = asyncio.create_task(runtime.run_step(session_id, query, "worker", "yolo"))
        assert await asyncio.wait_for(task, 5) == "已核对"
        runs = [item for item in runtime.store.list("run") if item["session_id"] == session_id]
        assert len(runs) == 1
        run_id = runs[0]["id"]
    run = runtime.store.get("run", run_id)
    assert run is not None and run["status"] == "completed"
    assert len(seen) == skills_calls == 2 and tool_calls == 1
    for request in seen:
        assert context_size(request) <= maximum
        assert [item["id"] for item in memory_records(request)] == [procedure["id"]]
    returned = next(message for message in seen[-1] if message.tool_call_id == call.id)
    assert returned.content == encoded and json.loads(returned.content) == result
    assert returned.preserve_content is True
    sent_native = next(message for message in seen[-1] if message.tool_calls)
    assert sent_native.tool_calls == [call]
    assert sent_native.reasoning == native.reasoning
    assert sent_native.provider_data == native.provider_data
    stored = [
        item for item in runtime.store.messages(session_id) if item.get("tool_call_id") == call.id
    ]
    assert len(stored) == 1
    assert stored[0]["content"] == encoded and stored[0]["content_provenance"] == "accepted"
    assert stored[0]["preserve_content"] is True
    assert application.pipeline._worker is None
