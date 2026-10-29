"""Owner corrections retain permission, source and ambiguous-result boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from test_attachment_memory_policy import MemoryRig, actual_app
from test_attachment_memory_policy import offline as offline
from test_memory_correct_runtime import _CORRECT, _NEW_RELATION, _OLD_RELATION, _SOURCE, _WRONG
from test_runtime import FakeClient
from test_teammate_attachment_runtime import eventually
from test_webpage_memory_import import owner_tool

import muli.memory as memory_module
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.store import new_id
from muli.tools import Tool, parameters


def correction(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "expected_version": item["version"],
        "content": _CORRECT,
        "entities": ["琥珀备库", "离线验证新副本"],
        "relations": [dict(_NEW_RELATION)],
    }


@pytest.fixture
async def fact_rig(tmp_path: Path) -> AsyncIterator[tuple[MemoryRig, dict[str, Any]]]:
    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        proposals = []
        if source["content"] == _SOURCE:
            assert source["principal"] == "external:attachment"
            proposals = [
                {
                    "layer": "semantic",
                    "content": _WRONG,
                    "quote": _SOURCE,
                    "entities": ["琥珀备库", "覆盖旧副本"],
                    "relations": [dict(_OLD_RELATION)],
                }
            ]
        return {"memories": proposals, "aliases": {}}

    async with actual_app(tmp_path, extractor=extract) as rig:
        rig.runtime.config.update(
            {
                "persona": {"prompt": "你是本地助手，请完成当前用户的任务。"},
                "memory": {"context_max_chars": 0},
            }
        )
        uploaded = await rig.upload(_SOURCE.encode("utf-8"))
        imported = await rig.import_attachment(uploaded["id"])
        assert "error" not in imported
        source_id = imported["source_id"]
        await eventually(
            rig.runtime,
            lambda: bool((job := rig.memory.pipeline.job(source_id)) and job["state"] == "done"),
        )
        facts = [
            item
            for item in rig.runtime.memory.search("琥珀备库")
            if item["source_id"] == source_id and item["kind"] == "fact"
        ]
        assert len(facts) == 1
        item = rig.runtime.memory.get(facts[0]["id"])
        assert item is not None and item["content"] == _WRONG
        assert rig.runtime.memory.store_vector(item["id"], [1.0, 0.0], "policy-vector")
        yield rig, item


def unchanged(rig: MemoryRig, before: dict[str, Any]) -> None:
    assert rig.runtime.memory.get(before["id"]) == before
    assert rig.runtime.memory.revisions(before["id"]) == []
    assert rig.runtime.memory.vector_count("policy-vector") == 1


async def test_stale_query_cannot_overwrite_a_real_management_edit(
    fact_rig: tuple[MemoryRig, dict[str, Any]],
) -> None:
    rig, before = fact_rig
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://localhost:8765"
    ) as web:
        response = await web.patch(
            f"/api/memories/{before['id']}", json={"content": "管理页已保存另外的核查结论。"}
        )
    assert response.status_code == 200
    winner = response.json()
    assert winner["version"] == before["version"] + 1
    history = rig.runtime.memory.revisions(before["id"])
    result = await owner_tool(
        rig, "只按刚才查到的版本更正这一条。", "memory_correct", **correction(before)
    )
    assert result["code"] == "evidence_changed"
    assert rig.runtime.memory.get(before["id"]) == winner
    assert rig.runtime.memory.revisions(before["id"]) == history


@pytest.mark.parametrize(
    "damage", ["missing_version", "bool_version", "extra_source", "blank_content"]
)
async def test_invalid_correction_has_no_revision_graph_or_vector_effect(
    fact_rig: tuple[MemoryRig, dict[str, Any]], damage: str
) -> None:
    rig, before = fact_rig
    arguments = correction(before)
    if damage == "missing_version":
        del arguments["expected_version"]
    elif damage == "bool_version":
        arguments["expected_version"] = True
    elif damage == "extra_source":
        arguments["trusted_owner"] = True
    else:
        arguments["content"] = " \n "
    result = await owner_tool(rig, "更正这条记录，不能改来源身份。", "memory_correct", **arguments)
    assert "error" in result
    unchanged(rig, before)


async def test_same_value_is_not_a_revision_and_completed_replay_is_stale(
    fact_rig: tuple[MemoryRig, dict[str, Any]],
) -> None:
    rig, before = fact_rig
    same = {
        "id": before["id"],
        "expected_version": before["version"],
        **{key: before[key] for key in ("content", "entities", "relations")},
    }
    result = await owner_tool(rig, "核对后保持这条原值。", "memory_correct", **same)
    assert result == {
        "id": before["id"],
        "old_version": before["version"],
        "new_version": before["version"],
        "changed": False,
    }
    unchanged(rig, before)
    arguments = correction(before)
    changed = await owner_tool(rig, "现在明确更正这一条。", "memory_correct", **arguments)
    assert changed["changed"] is True
    current = rig.runtime.memory.get(before["id"])
    history = rig.runtime.memory.revisions(before["id"])
    replay = await owner_tool(rig, "核查旧版本重放结果。", "memory_correct", **arguments)
    assert replay["code"] == "evidence_changed"
    assert rig.runtime.memory.get(before["id"]) == current
    assert rig.runtime.memory.revisions(before["id"]) == history


async def test_automatic_original_episode_is_not_a_correctable_fact(
    fact_rig: tuple[MemoryRig, dict[str, Any]],
) -> None:
    rig, before = fact_rig
    identifier = rig.runtime.memory.automatic_episode_id(before["source_id"])
    assert identifier is not None
    episode = rig.runtime.memory.get(identifier)
    assert episode is not None and episode["kind"] == "observation"
    result = await owner_tool(
        rig, "核查原文副本不能混作一条提炼结论更正。", "memory_correct", **correction(episode)
    )
    assert result["code"] == "invalid_arguments"
    assert rig.runtime.memory.get(identifier) == episode
    unchanged(rig, before)


async def test_correction_preserves_external_identity_and_source_forget_cannot_revive_it(
    fact_rig: tuple[MemoryRig, dict[str, Any]],
) -> None:
    rig, before = fact_rig
    original = rig.runtime.memory.source(before["source_id"])
    result = await owner_tool(
        rig, "只更正这条外部资料结论。", "memory_correct", **correction(before)
    )
    assert result["changed"] is True
    current = rig.runtime.memory.get(before["id"])
    assert current is not None and current["kind"] == "fact" and current["trusted_owner"] is False
    assert rig.runtime.memory.source(before["source_id"]) == original
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://localhost:8765"
    ) as web:
        deleted = await web.delete(f"/api/memories/{before['id']}")
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
    assert rig.runtime.memory.source(before["source_id"]) is None
    assert rig.runtime.memory.get(before["id"]) is None
    assert rig.runtime.memory.revisions(before["id"]) == []
    identifier = before["source_id"].removeprefix("attachment:")
    repeated = await owner_tool(
        rig, "核查同一旧附件是否仍被遗忘。", "memory_import_attachment", id=identifier
    )
    assert repeated["created"] is False and repeated["state"] == "forgotten"
    assert rig.runtime.memory.source(before["source_id"]) is None


async def test_direct_and_other_real_main_tool_cannot_proxy_a_correction(
    fact_rig: tuple[MemoryRig, dict[str, Any]],
) -> None:
    rig, before = fact_rig
    with pytest.raises(PermissionError):
        await rig.runtime.tools["memory_correct"].handler(correction(before))
    observations: list[str] = []

    async def proxy(arguments: dict[str, Any]) -> Any:
        active = rig.runtime._active_tool.get()
        assert active is not None and active.name == "correction_proxy" and active.handler is proxy
        candidate = next(
            tool for tool in rig.memory.index_tools(rig.runtime) if tool.name == "memory_correct"
        )
        observations.append(active.name)
        return await candidate.handler(correction(before))

    def provider(runtime: Runtime) -> list[Tool]:
        assert runtime is rig.runtime
        return [Tool("correction_proxy", "合成代理边界。", parameters({}), proxy, "write")]

    rig.runtime.add_tool_provider(provider)
    result = await owner_tool(rig, "核查代理工具不得借更正身份。", "correction_proxy")
    assert "error" in result and observations == ["correction_proxy"]
    unchanged(rig, before)


async def test_complete_small_receipt_budget_is_checked_before_any_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with actual_app(tmp_path) as rig:
        rig.runtime.config.update(
            {
                "persona": {"prompt": "你是本地助手，请完成当前用户的任务。"},
                "memory": {"context_max_chars": 0},
                "limits": {"max_output_chars": 1000},
            }
        )
        # Only this manual fixture's ID allocator is controlled. Memory.save
        # really stores the record; no source, permission or budget is forged.
        identifier = "B" * (rig.runtime.config.settings.limits.max_output_chars + 1)
        with monkeypatch.context() as scoped:
            scoped.setattr(memory_module, "new_id", lambda: identifier)
            before = rig.runtime.memory.save("手动保存的普通事实。", kind="fact")
        assert before["id"] == identifier
        assert rig.runtime.memory.store_vector(identifier, [1.0, 0.0], "policy-vector")
        result = await owner_tool(
            rig, "只更正这条手动事实。", "memory_correct", **correction(before)
        )
        assert result["code"] == "output_budget"
        unchanged(rig, before)


async def begin_correction(
    rig: MemoryRig, before: dict[str, Any], *, mode: Literal["ask", "yolo"] = "ask"
) -> tuple[dict[str, Any], str]:
    call_id = "correct-approval-" + new_id()
    turns = [
        ModelTurn(tool_calls=[ToolCall(call_id, "memory_correct", json.dumps(correction(before)))]),
        ModelTurn(content="本轮更正状态以真实回执为准。"),
    ]

    def factory(profile: ModelConfig) -> FakeClient:
        return FakeClient(turns if profile.model == "main" else [], [])

    rig.runtime.client_factory = factory  # type: ignore[assignment]
    receipt = await rig.runtime.submit("请按本轮权限更正这一条。", mode=mode)
    return receipt, call_id


async def begin_approval(
    rig: MemoryRig, before: dict[str, Any]
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    receipt, call_id = await begin_correction(rig, before)
    await eventually(rig.runtime, lambda: bool(rig.runtime.live_approvals()))
    pending = rig.runtime.live_approvals()
    assert len(pending) == 1 and pending[0]["tool"] == "memory_correct"
    assert pending[0]["risk"] == "write" and pending[0]["run_id"] == receipt["run_id"]
    return receipt, call_id, pending[0]


@pytest.mark.parametrize(
    "action",
    ["approve", "owner_input", "mode", "reconfigure", "disabled", "memory_disabled", "cancel"],
)
async def test_actual_ask_wait_obeys_owner_lifecycle_and_current_configuration(
    fact_rig: tuple[MemoryRig, dict[str, Any]], action: str
) -> None:
    rig, before = fact_rig
    receipt, call_id, approval = await begin_approval(rig, before)
    unchanged(rig, before)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://localhost:8765"
    ) as web:
        if action == "cancel":
            await rig.runtime.stop(rig.runtime.main_session()["id"])
            assert rig.runtime.store.get("run", receipt["run_id"])["status"] == "cancelled"
            unchanged(rig, before)
            return
        if action == "owner_input":
            await rig.runtime.submit("先别更正，处理我这条新要求。", mode="ask")
        else:
            if action == "mode":
                assert rig.runtime.main_session()["mode"] == "ask"
                response = await web.patch(
                    f"/api/sessions/{rig.runtime.main_session()['id']}", json={"mode": "yolo"}
                )
                assert response.status_code == 409
                assert response.json() == {"detail": "运行中不能变更会话权限"}
                assert rig.runtime.main_session()["mode"] == "ask"
                unchanged(rig, before)
            elif action == "reconfigure":
                await rig.memory.reconfigure()
            elif action == "disabled":
                rig.runtime.config.update({"disabled_tools": ["memory_correct"]})
            elif action == "memory_disabled":
                rig.runtime.config.update({"memory": {"enabled": False}})
            approved = await web.post(
                f"/api/approvals/{approval['id']}", json={"decision": "approve"}
            )
            assert approved.status_code == 200 and approved.json() == {"resolved": True}
        await rig.settle(receipt)
    result = rig.result(call_id)
    if action in {"approve", "mode"}:
        assert result["changed"] is True
        assert rig.runtime.memory.get(before["id"])["content"] == _CORRECT
        assert len(rig.runtime.memory.revisions(before["id"])) == 1
    else:
        assert "error" in result or result.get("status") == "not_executed", result
        unchanged(rig, before)


async def test_cancelling_after_the_real_commit_keeps_the_committed_correction(
    fact_rig: tuple[MemoryRig, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, before = fact_rig
    entered, release = asyncio.Event(), asyncio.Event()
    original_emit = rig.runtime.bus.emit

    async def paused(
        kind: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        if kind == "memory.updated" and data.get("memory_ids") == [before["id"]]:
            entered.set()
            await release.wait()
        return await original_emit(kind, data, session_id)

    monkeypatch.setattr(rig.runtime.bus, "emit", paused)
    receipt, _ = await begin_correction(rig, before, mode="yolo")
    try:
        await asyncio.wait_for(entered.wait(), 3)
        main_id = rig.runtime.main_session()["id"]
        run_id = receipt["run_id"]
        assert rig.runtime.run_ids[main_id] == run_id
        assert rig.runtime.memory.get(before["id"])["content"] == _CORRECT
        await rig.runtime.stop(main_id)
        assert rig.runtime.store.get("run", run_id)["status"] == "cancelled"
        assert rig.runtime.memory.get(before["id"])["version"] == before["version"] + 1
        assert len(rig.runtime.memory.revisions(before["id"])) == 1
    finally:
        release.set()


async def test_real_worker_catalog_and_dispatch_both_refuse_correction(tmp_path: Path) -> None:
    before: dict[str, Any] = {}
    call_id = "worker-correct-" + new_id()
    catalogs: list[set[str]] = []

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            catalogs.append({tool["function"]["name"] for tool in tools})
            assert "memory_correct" not in catalogs[-1]
            replies = [message for message in messages if message.tool_call_id == call_id]
            if not replies:
                turn = ModelTurn(
                    tool_calls=[ToolCall(call_id, "memory_correct", json.dumps(correction(before)))]
                )
            else:
                assert len(replies) == 1 and json.loads(replies[0].content) == {
                    "error": "工具未启用"
                }
                turn = ModelTurn(content="更正未执行。")
            yield StreamEvent("done", turn=turn)

    async with actual_app(tmp_path, worker=Worker([], [])) as rig:
        rig.runtime.config.update({"persona": {"prompt": "你是本地助手，请完成当前用户的任务。"}})
        before = rig.runtime.memory.save("给队友边界验证用的手动事实。", kind="fact")
        agent = await rig.runtime.team.spawn("核查更正权限", "准确报告更正工具的拒绝结果。")
        await eventually(
            rig.runtime, lambda: rig.runtime.team.get(agent["id"])["status"] == "completed"
        )
        replies = [
            item
            for item in rig.runtime.store.messages(agent["id"])
            if item.get("tool_call_id") == call_id
        ]
        assert len(catalogs) == 2 and len(replies) == 1
        assert json.loads(replies[0]["content"]) == {"error": "工具未启用"}
        assert rig.runtime.memory.get(before["id"]) == before
        assert rig.runtime.memory.revisions(before["id"]) == []
