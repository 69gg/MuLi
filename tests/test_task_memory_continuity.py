from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from test_memory_context import fact
from test_memory_runtime_quality import MEMORY_HEADING, memory_payload, memory_records
from test_runtime import FakeClient, make_runtime
from test_task_briefs import attach_services
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.context import context_size
from muli.media import MediaConfig, MediaService
from muli.memory import terms
from muli.memory_application import MemoryApplication
from muli.memory_context import recalled_for_prompt
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.task_control import TaskSnapshot


@dataclass
class ChildObservation:
    session_id: str
    scope: TaskSnapshot
    agent: dict[str, Any]
    query: str
    latest_user_id: str
    baseline: str
    budget: int
    current_ids: list[str]
    goal_ids: list[str]
    merged_ids: list[str]


@dataclass
class BoundRequest:
    observations: list[ChildObservation] = field(default_factory=list)
    requests: list[list[Message]] = field(default_factory=list)
    background: list[dict[str, Any]] = field(default_factory=list)
    current: dict[str, Any] | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    skills_calls: int = 0
    baseline_request_sizes: list[int] = field(default_factory=list)
    tool_evidence: str = ""
    request_limit: int = 0


async def bound_request(
    tmp_path: Path,
    *,
    goal: str,
    child_prompt: str,
    background_contents: list[str],
    current_content: str | None = None,
    request_boundary: str | None = None,
) -> BoundRequest:
    runtime, _ = make_runtime(tmp_path, [])
    application: MemoryApplication | None = None
    result = BoundRequest()
    constraints = ["只核对已有资料，不部署，不改变文件"]
    main_round = 0
    worker_round = 0
    commitment_id: str | None = None
    evidence_file = tmp_path / "current-tool-evidence.json"
    if request_boundary == "whole-request":
        result.tool_evidence = json.dumps(
            {"evidence": "本轮实际文件资料🙂" * 100, "instruction": "只读，不部署"},
            ensure_ascii=False,
        )
        evidence_file.write_bytes(result.tool_evidence.encode("utf-8"))

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round, worker_round, commitment_id
            if self.role == "worker":
                worker_round += 1
                result.requests.append(list(messages))
                if request_boundary == "whole-request" and worker_round == 1:
                    turn = ModelTurn(
                        tool_calls=[call("current-read", "fs_read", path=str(evidence_file))]
                    )
                else:
                    if request_boundary == "whole-request":
                        received = tool_result(messages, "current-read")
                        assert "error" not in received
                        assert received["content"] == result.tool_evidence
                        assert json.loads(received["content"])["instruction"] == "只读，不部署"
                    turn = ModelTurn(content="已核对本轮资料")
            else:
                main_round += 1
                if main_round == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-brief",
                                "commitment_save",
                                title="资料核对",
                                brief={"goal": goal, "constraints": constraints},
                            )
                        ]
                    )
                elif main_round == 2:
                    saved = tool_result(messages, "save-brief")
                    assert "error" not in saved
                    commitment_id = saved["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-bound",
                                "spawn_agent",
                                task_name="核对队友",
                                prompt=child_prompt,
                                commitment_id=commitment_id,
                            )
                        ]
                    )
                else:
                    if main_round == 3:
                        spawned = tool_result(messages, "spawn-bound")
                        assert "error" not in spawned
                        assert spawned["commitment_id"] == commitment_id
                    turn = ModelTurn(content="已收到队友资料")
            if turn.content:
                yield StreamEvent("text_delta", text=turn.content)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Client(config.model)

    # The real Runtime/Team drivers remain in use; only the model is offline.
    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        runtime.config.update(
            {
                "compaction": {"enabled": False},
                "memory": {"auto_extract": False, "context_limit": 3},
                "limits": {
                    "max_context_chars": 16000,
                    "max_output_chars": 6000,
                    "max_pending_runs": 3,
                },
                "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            }
        )
        await runtime.reconfigure()
        result.request_limit = runtime.config.settings.limits.max_context_chars
        attach_services(runtime, tmp_path)
        application = MemoryApplication(runtime, lambda: MediaService(MediaConfig()))
        for index, content in enumerate(background_contents):
            created = fact(runtime.memory, f"task-background-{index}", content, "procedural")
            frozen = runtime.memory.get(created["id"])
            assert frozen is not None
            result.background.append(deepcopy(frozen))
        if current_content is not None:
            created = fact(runtime.memory, "current-intent", current_content, "semantic")
            result.current = deepcopy(runtime.memory.get(created["id"]))
            assert result.current is not None
        created = fact(runtime.memory, "owner-profile", "我偏好简洁中文。", "preference")
        frozen_profile = runtime.memory.get(created["id"])
        assert frozen_profile is not None
        result.profile = deepcopy(frozen_profile)

        async def observe(session_id: str, messages: list[Message]) -> list[Message]:
            session = runtime.store.get("session", session_id)
            if session is None or session.get("kind") != "team":
                return messages
            request = runtime.request_context(session_id)
            assert request is not None and request.execution_scope is not None
            assert request.selected_replan is None
            latest = next(item for item in reversed(messages) if item.role == "user")
            assert isinstance(latest.source_message_id, str)
            agent = runtime.team.get(session_id)
            settings = runtime.config.settings
            # Use the existing Runtime budget, not a new allowance for this test.
            budget = min(
                settings.memory.context_max_chars,
                max(0, min(12000, settings.limits.max_context_chars // 4) - len(MEMORY_HEADING)),
            )
            assert application is not None
            baseline = application._context(
                latest.content, budget, latest_user_id=latest.source_message_id
            )
            current = recalled_for_prompt(
                runtime.memory,
                latest.content,
                settings.memory.context_limit,
                latest_user_id=latest.source_message_id,
                hops=settings.memory.graph_hops,
            )
            result.observations.append(
                ChildObservation(
                    session_id,
                    request.execution_scope,
                    deepcopy(agent),
                    latest.content,
                    latest.source_message_id,
                    baseline,
                    budget,
                    [item["id"] for item in current],
                    [
                        item["id"]
                        for item in runtime.memory.search(goal, settings.memory.context_limit)
                    ],
                    [
                        item["id"]
                        for item in runtime.memory.search(
                            goal + " " + latest.content, settings.memory.context_limit
                        )
                    ],
                )
            )
            if request_boundary == "rewritten-original-plus-pristine-copy":
                backup = deepcopy(messages[0])
                messages[0].content += "\n扩展说明"
                return [messages[0], backup, *messages[1:]]
            return messages

        runtime.context_hooks.append(observe)
        if request_boundary is not None:

            async def skills(session_id: str, messages: list[Message]) -> list[Message]:
                session = runtime.store.get("session", session_id)
                if session is None or session.get("kind") != "team":
                    return messages
                result.skills_calls += 1
                if request_boundary == "rewritten-host":
                    return [
                        replace(
                            messages[0], content="扩展提供的固定说明。\n" + messages[0].content
                        ),
                        *messages[1:],
                    ]
                if request_boundary == "duplicated-host":
                    return [messages[0], replace(messages[0]), *messages[1:]]
                if request_boundary == "rewritten-original-plus-pristine-copy":
                    return messages
                assert request_boundary == "whole-request"
                if not any(item.tool_call_id == "current-read" for item in messages):
                    return messages
                observed = result.observations[-1]
                prefix, separator, _ = messages[0].content.partition(MEMORY_HEADING)
                assert separator
                # Measure the original baseline, even against an implementation
                # which already appended task material before extension hooks.
                baseline = [
                    replace(messages[0], content=prefix + MEMORY_HEADING + observed.baseline),
                    *messages[1:],
                    Message(role="system", content="固定技能资料："),
                ]
                maximum = runtime.config.settings.limits.max_context_chars
                remaining = maximum - context_size(baseline) - 1
                assert remaining > 0
                padding = replace(baseline[-1], content=baseline[-1].content + "界" * remaining)
                baseline[-1] = padding
                assert context_size(baseline) == maximum - 1
                result.baseline_request_sizes.append(context_size(baseline))
                # Keep the actual host message intact.  Only the optional task
                # addition may be discarded by Runtime's final budget gate.
                return [*messages, padding]

            runtime.skills_context = skills
        # The owner text deliberately does not contain the later task keywords.
        receipt = await runtime.submit("启动委托", mode="yolo", source="web")
        assert isinstance(receipt["run_id"], str)
        await asyncio.wait_for(runtime.wait(receipt["run_id"]), 4)
        await eventually(
            runtime,
            lambda: (
                bool(result.observations)
                and runtime.team.get(result.observations[0].session_id)["status"] == "completed"
            ),
        )
        expected_rounds = 2 if request_boundary == "whole-request" else 1
        assert len(result.observations) == len(result.requests) == expected_rounds
        if request_boundary is not None:
            assert result.skills_calls == expected_rounds
        observed = result.observations[0]
        scope, agent = observed.scope, observed.agent
        assert scope.session_id == agent["session_id"] == observed.session_id
        assert scope.commitment_id == agent["commitment_id"] == commitment_id
        assert agent["parent_id"] == runtime.main_session()["id"]
        assert agent["generation"] == 1 and agent["status"] == "running"
        run = runtime.store.get("run", agent["run_id"])
        assert run is not None and run["status"] == "completed"
        assert run["session_id"] == observed.session_id
        brief = json.loads(scope.brief_json)
        assert brief["goal"] == goal and brief["constraints"] == constraints
        assert any(
            message.role == "system" and scope.context() in message.content
            for message in result.requests[0]
        ), "必须先证明真实队友已收到完整冻结任务，而非测试手造scope"
        assert child_prompt in observed.query and "队内任务消息" in observed.query
        for frozen in [*result.background, result.profile]:
            assert runtime.memory.get(frozen["id"]) == frozen
        if result.current is not None:
            assert runtime.memory.get(result.current["id"]) == result.current
        assert application.pipeline._worker is None
        return result
    finally:
        if application is not None:
            await application.close()
        await runtime.close()


async def test_bound_teammate_adds_goal_background_only_to_unused_memory_budget(
    tmp_path: Path,
) -> None:
    result = await bound_request(
        tmp_path,
        goal="orchid",
        child_prompt="继续整理",
        background_contents=["orchid uses cedar compiler and vault cache."],
    )
    observed = result.observations[0]
    assert observed.current_ids == [], "当前召回非空时不能启动任务补位"
    baseline = [json.loads(line) for line in observed.baseline.splitlines()[1:]]
    assert [item["id"] for item in baseline] == [result.profile["id"]]
    payload = memory_payload(result.requests[0])
    assert payload.startswith(observed.baseline), "既有基线必须逐字保留"
    assert len(payload) <= observed.budget
    pages = memory_records(result.requests[0])
    expected = result.background[0]
    assert expected["id"] in observed.goal_ids
    assert expected["id"] in {item["id"] for item in pages}, (
        "真实任务目标已有背景记忆，当前输入无命中且有剩余额度，自动页仍缺该冻结fact"
    )
    page = next(item for item in pages if item["id"] == expected["id"])
    for key in ("id", "source_id", "source", "status", "content"):
        assert page[key] == expected[key]


@pytest.mark.parametrize("goal_kind", ["more-than-64-words", "few-words-higher-score"])
async def test_current_query_evidence_survives_large_and_high_scoring_task_goals(
    tmp_path: Path, goal_kind: str
) -> None:
    goal = (
        " ".join(f"a{index:03d}" for index in range(70))
        if goal_kind == "more-than-64-words"
        else "amber birch cedar"
    )
    result = await bound_request(
        tmp_path,
        goal=goal,
        child_prompt="zzzz",
        current_content="zzzz exact current evidence.",
        background_contents=[f"{goal} context{i}" for i in range(3)],
    )
    observed = result.observations[0]
    assert result.current is not None
    assert result.current["id"] in observed.current_ids
    assert result.current["id"] not in observed.merged_ids, (
        "夹具必须真实证明拼接目标会因64词门或排名挤掉当前事实"
    )
    assert "zzzz" in terms(observed.query)
    if goal_kind == "more-than-64-words":
        assert len(terms(goal)) > 64
        assert "zzzz" not in sorted(terms(goal + " " + observed.query))[:64]
    else:
        assert len(terms(goal)) < 64
        assert len(terms(goal)) > len(terms("zzzz"))
    payload = memory_payload(result.requests[0])
    assert payload == observed.baseline, "任务背景不得改变已有当前查询的任何页面或正文"
    assert len(payload) <= observed.budget
    pages = memory_records(result.requests[0])
    current = next(item for item in pages if item["id"] == result.current["id"])
    for key in ("source_id", "status", "content"):
        assert current[key] == result.current[key]
    assert result.profile["id"] in {item["id"] for item in pages}
    assert not {item["id"] for item in result.background} & {item["id"] for item in pages}


async def test_task_background_is_discarded_when_only_the_whole_request_is_full(
    tmp_path: Path,
) -> None:
    result = await bound_request(
        tmp_path,
        goal="orchid",
        child_prompt="继续整理",
        background_contents=[
            "orchid archive background. " + "cedarcompiler vaultcache immutablememo. " * 25
        ],
        request_boundary="whole-request",
    )
    expected = result.background[0]
    first = result.requests[0]
    assert expected["id"] in {item["id"] for item in memory_records(first)}, (
        "先证真实小请求有任务补位，不能以始终不补制造预算假绿"
    )
    observed = result.observations[-1]
    assert observed.current_ids == []
    assert expected["id"] in observed.goal_ids
    assert len(memory_payload(first)) <= observed.budget
    assert len(memory_payload(first)) > len(observed.baseline)
    assert result.baseline_request_sizes == [result.request_limit - 1]
    request = result.requests[-1]
    assert context_size(request) == result.request_limit - 1
    assert memory_payload(request) == observed.baseline
    assert [item["id"] for item in memory_records(request)] == [result.profile["id"]]
    assert expected["id"] not in memory_payload(request)
    current_tool = next(item for item in request if item.tool_call_id == "current-read")
    payload = json.loads(current_tool.content)
    assert payload["content"] == result.tool_evidence
    assert json.loads(payload["content"])["instruction"] == "只读，不部署"
    calls = [
        item for message in request for item in message.tool_calls if item.id == "current-read"
    ]
    assert len(calls) == 1 and calls[0].name == "fs_read"
    assert json.loads(calls[0].arguments)["path"].endswith("current-tool-evidence.json")
    scope = observed.scope
    assert any(
        message.role == "system"
        and "不是新的主人指令或授权" in message.content
        and scope.context() in message.content
        for message in request
    )
    assert result.skills_calls == len(result.observations) == len(result.requests) == 2


@pytest.mark.parametrize(
    "boundary",
    ["rewritten-host", "duplicated-host", "rewritten-original-plus-pristine-copy"],
)
async def test_extension_changed_or_duplicated_host_keeps_the_original_memory_baseline(
    tmp_path: Path, boundary: str
) -> None:
    result = await bound_request(
        tmp_path,
        goal="orchid",
        child_prompt="继续整理",
        background_contents=["orchid uses cedar compiler and vault cache."],
        request_boundary=boundary,
    )
    observed = result.observations[0]
    assert observed.current_ids == []
    assert result.background[0]["id"] in observed.goal_ids
    assert context_size(result.requests[0]) <= result.request_limit
    hosts = [
        message
        for message in result.requests[0]
        if message.role == "system" and MEMORY_HEADING in message.content
    ]
    if boundary == "rewritten-original-plus-pristine-copy":
        assert len(hosts) == 2
        assert hosts[0].content.endswith("\n扩展说明")
    assert all(result.background[0]["id"] not in message.content for message in hosts), (
        "原宿主对象已被扩展改写，不能借其未改写副本继续补入任务背景"
    )
    if boundary == "rewritten-original-plus-pristine-copy":
        assert hosts[0].content.partition(MEMORY_HEADING)[2] == (observed.baseline + "\n扩展说明")
        assert hosts[1].content.partition(MEMORY_HEADING)[2] == observed.baseline
    else:
        assert memory_payload(result.requests[0]) == observed.baseline
        assert [item["id"] for item in memory_records(result.requests[0])] == [result.profile["id"]]
    assert result.skills_calls == len(result.observations) == len(result.requests) == 1
