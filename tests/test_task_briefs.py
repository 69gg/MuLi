from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from test_runtime import FakeClient, make_runtime

from muli.attachments import Attachments
from muli.commitments import Commitments, TaskBriefInput
from muli.context import ContextLimitError, context_size
from muli.events import EventBus
from muli.local_services import LocalServices
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.store import Store, now
from muli.tools import Tool, parameters


def attach_services(runtime: Runtime, tmp_path: Path) -> LocalServices:
    return LocalServices(
        runtime, Attachments(runtime.store, tmp_path / "attachments", runtime.config)
    )


def bind(runtime: Runtime, commitment_id: str) -> dict[str, Any]:
    session = runtime.new_session("绑定队友", mode="yolo")
    runtime.store.put("session", {**session, "kind": "team", "role": "worker"})
    runtime.store.put(
        "agent",
        {
            "id": session["id"],
            "session_id": session["id"],
            "parent_id": runtime.main_session()["id"],
            "commitment_id": commitment_id,
            "role": "worker",
            "mode": "yolo",
            "name": "绑定队友",
            "status": "completed",
            "generation": 0,
            "created_at": now(),
            "updated_at": now(),
        },
    )
    return session


@pytest.mark.parametrize("with_services", [False, True])
async def test_brief_guidance_is_only_given_to_main_with_registered_tools(
    tmp_path: Path, with_services: bool
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    if with_services:
        attach_services(runtime, tmp_path)
    try:
        main = runtime.main_session()
        prompt = runtime._history(main["id"])[0].content
        assert ("持续协作的任务说明：" in prompt) is with_services
        if with_services:
            for instruction in (
                "完整目标、逐条限制",
                "evidence_refs",
                "commitment_id",
                "send_input 显式通知",
                "不增加授权",
                "不会自动暂停队友或即时撤销工具调用",
            ):
                assert instruction in prompt
        for kind in ("team", "workflow"):
            session = runtime.new_session()
            runtime.store.put("session", {**session, "kind": kind, "role": "main"})
            assert "持续协作的任务说明：" not in runtime._history(session["id"])[0].content
    finally:
        await runtime.close()


async def test_progress_saves_preserve_full_brief_and_unknown_fields(tmp_path: Path) -> None:
    store = Store(tmp_path / "db")
    ledger = Commitments(store, EventBus(store))
    brief: TaskBriefInput = {"goal": "完整目标", "constraints": ["不能部署", "先核查证据"]}
    try:
        item = await ledger.save("持续任务", brief=brief)
        store.put("commitment", {**item, "future_field": {"preserve": True}})
        updated = await ledger.save("持续任务", identifier=item["id"], next_step="继续核查")
        assert updated["brief"] == item["brief"]
        assert updated["future_field"] == {"preserve": True}
        assert updated["revision"] == 2
        assert ledger.get(item["id"], revision=1)["brief"] == item["brief"]
        assert all("brief" not in item for item in ledger.list())
        assert all("完整目标" not in json.dumps(event) for event in store.events())
        assert ledger.list()[0]["has_brief"] is True
        tool = next(tool for tool in ledger.tools() if tool.name == "commitment_save")
        for key in ("revision", "id", "created_at", "future_field", "role"):
            with pytest.raises(ValueError, match="受保护"):
                await tool.handler({"title": "覆盖", "identifier": item["id"], key: "forged"})
        assert ledger.get(item["id"])["revision"] == 2
    finally:
        store.close()


async def test_evidence_is_resolved_by_host_and_invalid_refs_do_not_commit(tmp_path: Path) -> None:
    store = Store(tmp_path / "db")
    ledger = Commitments(store, EventBus(store))
    message = store.add_message("source-session", "user", "原始资料")
    store.put("attachment", {"id": "file", "name": "evidence.txt"})
    try:
        item = await ledger.save(
            "证据",
            brief={
                "goal": "检查来源",
                "constraints": [],
                "evidence_refs": [
                    {"kind": "message", "id": message["id"]},
                    {"kind": "attachment", "id": "file"},
                ],
            },
        )
        refs = item["brief"]["evidence_refs"]
        assert refs[0] == {
            "kind": "message",
            "id": message["id"],
            "session_id": "source-session",
            "seq": message["seq"],
        }
        assert refs[1] == {"kind": "attachment", "id": "file"}
        with pytest.raises(ValueError, match="不存在"):
            await ledger.save(
                "坏引用",
                brief={
                    "goal": "检查",
                    "constraints": [],
                    "evidence_refs": [{"kind": "message", "id": "missing"}],
                },
            )
        with pytest.raises(ValueError, match="格式无效"):
            await ledger.save(
                "伪造位置",
                brief={
                    "goal": "检查",
                    "constraints": [],
                    "evidence_refs": [
                        {"kind": "message", "id": message["id"], "session_id": "forged", "seq": 1}
                    ],
                },
            )
        assert len(store.list("commitment")) == 1
        store.delete("message", message["id"])
        with pytest.raises(ValueError, match="已删除"):
            ledger.brief_context(item["id"])
    finally:
        store.close()


async def test_paged_read_keeps_json_budget_and_historical_revision(tmp_path: Path) -> None:
    store = Store(tmp_path / "db")
    ledger = Commitments(store, EventBus(store), max_output_chars=lambda: 1000)
    try:
        item = await ledger.save(
            "分页",
            brief={"goal": '引号"反斜杠\\\n控制\x01' * 200, "constraints": ["中段限制不能消失"]},
        )
        expected = json.dumps(item["brief"], ensure_ascii=False, sort_keys=True)
        first = ledger.read(item["id"])
        assert first["has_more"]
        await ledger.save("分页", identifier=item["id"], brief={"goal": "新版", "constraints": []})
        chunks: list[str] = []
        page = first
        while True:
            assert len(json.dumps(page, ensure_ascii=False)) <= 1000
            assert page["revision"] == 1
            chunks.append(page["text"])
            if not page["has_more"]:
                break
            assert page["next_offset"] > page["offset"]
            page = ledger.read(item["id"], offset=page["next_offset"], revision=first["revision"])
        assert "".join(chunks) == expected
        assert json.loads("".join(chunks)) == item["brief"]
        for args in (
            {"offset": -1},
            {"offset": len(expected) + 1},
            {"limit": 1001},
            {"limit": True},
            {"revision": 0},
        ):
            with pytest.raises(ValueError):
                ledger.read(item["id"], **args)
        with pytest.raises(KeyError):
            ledger.read(item["id"], revision=999)
        empty = await ledger.save("旧记录")
        assert ledger.read(empty["id"])["has_brief"] is False
        assert ledger.read(empty["id"])["text"] == ""
    finally:
        store.close()


@pytest.mark.parametrize("escaped_title", [False, True])
async def test_reduced_output_budget_keeps_old_ledger_readable_without_changing_full_text(
    tmp_path: Path, escaped_title: bool
) -> None:
    runtime, _ = make_runtime(tmp_path, [ModelTurn("继续协助")])
    services = attach_services(runtime, tmp_path)
    try:
        item = await services.commitments.save(
            "X" + "\x01" * 239 if escaped_title else "普通标题",
            next_step="x" * 763,
            evidence="证明文字" * 500,
            brief={"goal": "完整目标", "constraints": ["保持完整要求"]},
        )
        runtime.config.update({"limits": {"max_output_chars": 1000}})
        metadata = services.commitments.list()[0]
        assert len(json.dumps(metadata, ensure_ascii=False)) <= 1000
        assert metadata["details_omitted"] is True
        assert metadata["id"] == item["id"]
        assert services.commitments.get(item["id"]) == item
        receipt = await runtime.submit("请继续协助", mode="yolo")
        assert await runtime.wait(receipt["run_id"]) == "继续协助"
        assert services.commitments.get(item["id"]) == item
    finally:
        await runtime.close()


async def test_metadata_paths_do_not_bypass_scoped_brief_reads(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    try:
        own = await services.commitments.save(
            "自己的任务", brief={"goal": "own-private", "constraints": []}
        )
        other = await services.commitments.save(
            "其他任务", brief={"goal": "other-private", "constraints": []}
        )
        session = bind(runtime, own["id"])
        tools = runtime.session_tools(session["id"])
        assert "brief" not in tools["commitment_save"].parameters["properties"]
        result = await runtime._tool(
            ToolCall("list", "commitment_list", "{}"), tools, "yolo", session["id"], "r"
        )
        assert "other-private" not in json.dumps(result)
        result = await runtime._tool(
            ToolCall(
                "save",
                "commitment_save",
                json.dumps({"title": "更新进度", "identifier": other["id"]}),
            ),
            tools,
            "yolo",
            session["id"],
            "r",
        )
        assert "other-private" not in json.dumps(result)
        own_page = await runtime._tool(
            ToolCall("own", "commitment_read", "{}"), tools, "yolo", session["id"], "r"
        )
        assert "own-private" in own_page["text"]
        rejected = await runtime._tool(
            ToolCall("other", "commitment_read", json.dumps({"identifier": other["id"]})),
            tools,
            "yolo",
            session["id"],
            "r",
        )
        assert "绑定" in rejected["error"]
        assert "other-private" not in json.dumps(runtime.store.events())
    finally:
        await runtime.close()


async def test_main_write_guard_uses_actual_caller_even_with_main_schema(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    main = runtime.main_session()
    main_tools = runtime.session_tools(main["id"])
    assert "brief" in main_tools["commitment_save"].parameters["properties"]
    args = {"title": "任务", "brief": {"goal": "完整目标", "constraints": ["只读"]}}
    try:
        for kind in ("team", "workflow"):
            session = runtime.new_session()
            runtime.store.put("session", {**session, "kind": kind, "role": "main"})
            if kind == "team":
                runtime.store.put(
                    "agent",
                    {
                        "id": session["id"],
                        "session_id": session["id"],
                        "parent_id": main["id"],
                        "status": "idle",
                    },
                )
            denied = await runtime._tool(
                ToolCall(kind, "commitment_save", json.dumps(args)),
                main_tools,
                "yolo",
                session["id"],
                "run",
            )
            assert "只有主助手" in denied["error"]
        with pytest.raises(PermissionError):
            await main_tools["commitment_save"].handler(args)
        result = await runtime._tool(
            ToolCall("main", "commitment_save", json.dumps(args)),
            main_tools,
            "yolo",
            main["id"],
            "run",
        )
        assert result["has_brief"] and "brief" not in result
        assert services.commitments.get(result["id"])["brief"]["constraints"] == ["只读"]
        assert runtime.tool_session_id is None
    finally:
        await runtime.close()


async def test_host_caller_context_isolated_nested_and_reset_on_cancel(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()
    callers: list[str | None] = []
    settled: list[str | None] = []

    async def probe(args: dict[str, Any]) -> str:
        callers.append(runtime.tool_session_id)
        if len(callers) == 2:
            entered.set()
        await release.wait()
        assert runtime.tool_session_id == args["expected"]
        return "done"

    tools = {"probe": Tool("probe", "test", parameters({"expected": {"type": "string"}}), probe)}

    async def invoke(identifier: str) -> None:
        try:
            await runtime._tool(
                ToolCall(identifier, "probe", json.dumps({"expected": identifier})),
                tools,
                "yolo",
                identifier,
                "run",
            )
        finally:
            settled.append(runtime.tool_session_id)

    tasks = [asyncio.create_task(invoke("a")), asyncio.create_task(invoke("b"))]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        await asyncio.wait_for(tasks[1], 2)
        assert set(callers) == {"a", "b"} and settled == [None, None]

        async def inner(args: dict[str, Any]) -> str:
            assert runtime.tool_session_id == "inner"
            raise ValueError("inner failed")

        async def outer(args: dict[str, Any]) -> str:
            assert runtime.tool_session_id == "outer"
            await runtime._tool(ToolCall("i", "inner", "{}"), nested, "yolo", "inner", "r")
            assert runtime.tool_session_id == "outer"
            return "outer done"

        nested = {
            "inner": Tool("inner", "", parameters({}), inner),
            "outer": Tool("outer", "", parameters({}), outer),
        }
        assert (
            await runtime._tool(ToolCall("o", "outer", "{}"), nested, "yolo", "outer", "r")
            == "outer done"
        )
        assert runtime.tool_session_id is None
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()


async def test_main_spawned_worker_does_not_inherit_main_write_permission(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    runtime.config.update(
        {"models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}}}
    )
    await runtime.reconfigure()
    main = runtime.main_session()
    main["mode"] = "yolo"
    runtime.store.put("session", main)
    main_save = runtime.session_tools(main["id"])["commitment_save"]
    worker_schemas: list[dict[str, Any]] = []
    worker_results: list[dict[str, Any]] = []

    async def unsafe_forward(args: dict[str, Any]) -> Any:
        assert runtime.tool_session_id != main["id"]
        return await main_save.handler(
            {"title": "forged", "brief": {"goal": "forged", "constraints": []}}
        )

    runtime.add_tool_provider(
        lambda _: [Tool("forward_main_handler", "test", parameters({}), unsafe_forward, "write")]
    )

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            if self.turns and self.turns[0].tool_calls:
                worker_schemas.extend(tools)
            async for event in super().stream(messages, tools):
                yield event

    worker_turns = [
        ModelTurn(tool_calls=[ToolCall("bad", "forward_main_handler", "{}")]),
        ModelTurn(content="worker complete"),
    ]

    def factory(config: ModelConfig) -> FakeClient:
        return Client(
            worker_turns if config.model == "worker" else [ModelTurn(content="main summary")], []
        )

    runtime.client_factory = factory  # type: ignore[assignment]

    async def spawn(args: dict[str, Any]) -> str:
        assert runtime.tool_session_id == main["id"]
        child = await runtime.team.spawn("worker", "工作", parent_id=main["id"], mode="yolo")
        await runtime.team.wait([child["id"]], timeout=2)
        worker_results.extend(runtime.store.messages(child["id"]))
        assert runtime.tool_session_id == main["id"]
        return str(child["id"])

    try:
        spawn_result = await runtime._tool(
            ToolCall("spawn", "host_spawn", "{}"),
            {"host_spawn": Tool("host_spawn", "", parameters({}), spawn)},
            "yolo",
            main["id"],
            "run",
        )
        assert isinstance(spawn_result, str), spawn_result
        schema = next(
            item for item in worker_schemas if item["function"]["name"] == "commitment_save"
        )
        assert "brief" not in schema["function"]["parameters"]["properties"]
        result = next(item for item in worker_results if item["role"] == "tool")
        assert "只有主助手" in result["content"]
        assert services.commitments.list() == []
        assert runtime.tool_session_id is None
    finally:
        await runtime.close()


async def test_scoped_brief_survives_compaction_packing_and_real_reopen(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {"compaction": {"mode": "extractive"}, "limits": {"max_context_chars": 14000}}
    )
    services = attach_services(runtime, tmp_path)
    marker = "唯一中段限制：不能部署 production"
    item = await services.commitments.save(
        "长任务", brief={"goal": "继续核查项目", "constraints": [marker]}
    )
    session = bind(runtime, item["id"])
    runtime.store.add_message(session["id"], "assistant", "准备接收完整项目资料")
    source = runtime.store.add_message(session["id"], "user", "头" * 5000 + marker + "尾" * 5000)
    runtime.store.add_message(session["id"], "assistant", "先检查")
    runtime.store.add_message(session["id"], "user", "继续")
    try:
        runtime.compaction.observe(session["id"])
        await asyncio.wait_for(runtime.compaction.finish(session["id"]), 2)
        active = runtime.store.get("compaction", session["id"])
        assert active is not None and marker not in active["summary"]
        messages = await runtime._next_context(session["id"])
        brief = next(message for message in messages if "完整受控任务说明（" in message.content)
        assert marker in brief.content and "不是新的主人指令或授权" in brief.content
        assert marker not in "".join(
            message.content for message in messages if message is not brief
        )
        assert runtime.store.get("message", source["id"])["content"] == source["content"]  # type: ignore[index]
        assert context_size(messages) <= 14000
    finally:
        await runtime.close()
    reopened = Runtime(runtime.config, runtime.data_dir)
    attach_services(reopened, tmp_path)
    try:
        replay = await reopened._next_context(session["id"])
        assert marker in next(
            message.content for message in replay if "完整受控任务说明（" in message.content
        )
        assert reopened.store.get("agent", session["id"])["commitment_id"] == item["id"]  # type: ignore[index]
    finally:
        await reopened.close()


async def test_brief_budget_fails_explicitly_without_clipping_native_replay(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"limits": {"max_context_chars": 4000}})
    services = attach_services(runtime, tmp_path)
    try:
        with pytest.raises(ContextLimitError, match="完整任务说明"):
            await services.commitments.save("超大", brief={"goal": "x" * 8000, "constraints": []})
        assert services.commitments.list() == []
        item = await services.commitments.save(
            "合法说明", brief={"goal": "g" * 1500, "constraints": ["不可裁剪"]}
        )
        session = bind(runtime, item["id"])
        native = {
            "content_blocks": [
                {"type": "thinking", "signature": "signed-" * 200, "thinking": "native"}
            ]
        }
        messages = [
            Message(role="system", content="system"),
            Message(role="user", content="current"),
            Message(
                role="assistant",
                tool_calls=[ToolCall("a", "inspect", json.dumps({"arg": "p" * 1500}))],
                provider_data=native,
            ),
            Message(role="tool", tool_call_id="a", content="result"),
        ]
        before = json.dumps([asdict(message) for message in messages], ensure_ascii=False)
        with pytest.raises(ContextLimitError, match="完整任务说明"):
            await runtime.prepare_context(session["id"], messages)
        assert json.dumps([asdict(message) for message in messages], ensure_ascii=False) == before
        assert services.commitments.get(item["id"])["brief"]["constraints"] == ["不可裁剪"]
    finally:
        await runtime.close()


async def test_each_context_build_uses_latest_scoped_brief_without_changing_old_snapshot(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    try:
        a = await services.commitments.save(
            "A", brief={"goal": "目标A", "constraints": ["旧限制A"]}
        )
        b = await services.commitments.save(
            "B", brief={"goal": "目标B", "constraints": ["其他限制B"]}
        )
        agent_a, agent_b = bind(runtime, a["id"]), bind(runtime, b["id"])
        old = await runtime.prepare_context(
            agent_a["id"], [Message(role="system", content="system")]
        )
        await services.commitments.save(
            "A", identifier=a["id"], brief={"goal": "目标A", "constraints": ["新限制A"]}
        )
        new = await runtime.prepare_context(
            agent_a["id"], [Message(role="system", content="system")]
        )
        other = await runtime.prepare_context(
            agent_b["id"], [Message(role="system", content="system")]
        )
        assert "旧限制A" in old[1].content and "新限制A" not in old[1].content
        assert "新限制A" in new[1].content and "其他限制B" not in new[1].content
        assert "其他限制B" in other[1].content and "新限制A" not in other[1].content
    finally:
        await runtime.close()


async def test_session_scoped_schemas_preserve_explicit_plugin_replacement(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    attach_services(runtime, tmp_path)

    async def replacement(args: dict[str, Any]) -> str:
        return "explicit replacement"

    def provider(_: Runtime) -> list[Tool]:
        return [Tool("commitment_save", "", parameters({}), replacement, "write")]

    try:
        runtime.add_tool_provider(provider, replace=True)
        main = runtime.main_session()
        assert runtime.session_tools(main["id"])["commitment_save"].handler is replacement
        await runtime.reconfigure()
        assert runtime.session_tools(main["id"])["commitment_save"].handler is replacement
    finally:
        await runtime.close()
