from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from test_runtime import make_runtime
from test_task_briefs import attach_services, bind

from muli.auth import hash_password
from muli.config import ConfigFile
from muli.local_services import LocalServices
from muli.providers import ToolCall
from muli.runtime import Runtime
from muli.server import create_app


@pytest.fixture
async def inspection_case(
    tmp_path: Path,
) -> AsyncIterator[tuple[Runtime, LocalServices, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    app = create_app(config, runtime=runtime)
    services: LocalServices = app.state.local_services
    # ASGITransport deliberately does not enter lifespan or start external services.
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            yield runtime, services, client
    finally:
        await runtime.close()


async def test_inspection_authentication_precedes_task_lookup_and_matches_local_service(
    inspection_case: tuple[Runtime, LocalServices, httpx.AsyncClient],
) -> None:
    runtime, services, client = inspection_case
    item = await services.commitments.save(
        "私密任务", brief={"goal": "private-goal", "constraints": ["private-constraint"]}
    )
    runtime.config.update({"server": {"password_hash": hash_password("test-password")}})
    for identifier in (item["id"], "nonexistent"):
        response = await client.get(f"/api/commitments/{identifier}/inspection")
        assert response.status_code == 401
        assert "private-goal" not in response.text and "私密任务" not in response.text
    login = await client.post("/api/auth/login", json={"password": "test-password"})
    assert login.status_code == 200
    for section in ("overview", "task", "agents", "reports", "workflows", "evidence", "memories"):
        params = {"section": section}
        if section == "memories":
            params["query"] = "显式检索"
        response = await client.get(f"/api/commitments/{item['id']}/inspection", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == services.task_inspection.read(item["id"], **params)
    missing = await client.get("/api/commitments/nonexistent/inspection")
    assert missing.status_code == 404


async def test_handoff_api_auth_scope_versions_and_read_only_metadata(
    inspection_case: tuple[Runtime, LocalServices, httpx.AsyncClient],
) -> None:
    runtime, services, client = inspection_case
    task = await services.commitments.save(
        "私密接办", brief={"goal": "只交草稿", "constraints": ["不得外发"]}
    )
    other = await services.commitments.save("另一任务")
    worker = bind(runtime, task["id"])
    agent = runtime.store.get("agent", worker["id"])
    assert agent is not None
    runtime.store.put("agent", {**agent, "session_id": worker["id"]})
    message = runtime.store.add_message(worker["id"], "user", "附件正文不应进入接办目录")
    runtime.config.update({"server": {"password_hash": hash_password("handoff-password")}})
    url = f"/api/commitments/{task['id']}/inspection"
    params = {"section": "handoff", "agent_id": worker["id"]}
    unauthorized = await client.get(url, params=params)
    assert unauthorized.status_code == 401
    assert "私密接办" not in unauthorized.text
    login = await client.post("/api/auth/login", json={"password": "handoff-password"})
    assert login.status_code == 200
    changes = runtime.store.db.total_changes
    events = runtime.store.event_position()
    response = await client.get(url, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    page = response.json()
    assert page == services.task_inspection.read(task["id"], **params)
    assert page["source_completeness"] == "recent_window_only"
    assert page["execution"]["side_effect_status"] == "unknown"
    assert page["items"][0]["read"]["seq"] == message["seq"]
    assert message["content"] not in response.text
    assert runtime.store.db.total_changes == changes
    assert runtime.store.event_position() == events
    assert not runtime.tasks and not runtime._selected_replans
    cross_task = await client.get(f"/api/commitments/{other['id']}/inspection", params=params)
    assert cross_task.status_code == 404
    empty = await client.get(url, params={**params, "agent_id": ""})
    assert empty.status_code == 422
    runtime.store.put("message", {**message, "content": "同一原序号已有新版本"})
    changed = await client.get(url, params={**params, "revision": page["revision"]})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "evidence_changed"


async def test_task_inspection_pages_full_plan_with_utf8_and_leaves_business_state_unchanged(
    inspection_case: tuple[Runtime, LocalServices, httpx.AsyncClient],
) -> None:
    runtime, services, client = inspection_case
    item = await services.commitments.save(
        "长计划", brief={"goal": "完整目标🙂", "constraints": ["只读，不执行"]}
    )
    plan = '逐条核查🙂\n引号"反斜杠\\\0' * 100
    item = {
        **item,
        "control": {
            "epoch": 1,
            "approved_epoch": 1,
            "replan_request_id": None,
            "plan": {"epoch": 1, "text": plan},
        },
    }
    runtime.store.put("commitment", item)
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    main = runtime.main_session()
    runtime._selected_replans[main["id"]] = "unrelated-selected-task"
    before_objects = [
        tuple(row)
        for row in runtime.store.db.execute("SELECT kind,id,data FROM objects ORDER BY kind,id")
    ]
    before_selection = dict(runtime._selected_replans)
    before_tasks = dict(runtime.tasks)
    before_events = runtime.store.event_position()
    url = f"/api/commitments/{item['id']}/inspection"
    params: dict[str, str | int] = {"section": "task", "length_bytes": 103}
    chunks: list[str] = []
    for _ in range(200):
        response = await client.get(url, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(response.text) <= 1000
        chunks.append(page["content"])
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        if not page["has_more"]:
            break
        assert page["next_offset_bytes"] > page["offset_bytes"]
        params.update(
            offset_bytes=page["next_offset_bytes"],
            revision=page["revision"],
            record_revision=page["record_revision"],
        )
    else:
        pytest.fail("正文没有在有限页数内读完")
    restored = json.loads("".join(chunks))
    assert restored == item and restored["control"]["plan"]["text"] == plan
    assert before_objects == [
        tuple(row)
        for row in runtime.store.db.execute("SELECT kind,id,data FROM objects ORDER BY kind,id")
    ]
    assert runtime._selected_replans == before_selection
    assert runtime.tasks == before_tasks
    assert runtime.store.event_position() == before_events


@pytest.mark.parametrize("field", ["revision", "record_revision"])
async def test_task_inspection_api_rejects_stale_body_versions(
    inspection_case: tuple[Runtime, LocalServices, httpx.AsyncClient],
    field: str,
) -> None:
    _, services, client = inspection_case
    item = await services.commitments.save("版本", brief={"goal": "原始目标", "constraints": []})
    url = f"/api/commitments/{item['id']}/inspection"
    response = await client.get(url, params={"section": "task", "length_bytes": 16})
    assert response.status_code == 200
    page = response.json()
    params = {
        "section": "task",
        "revision": page["revision"],
        "record_revision": page["record_revision"],
        "offset_bytes": page["next_offset_bytes"],
        field: "stale",
    }
    rejected = await client.get(url, params=params)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "evidence_changed"


async def test_task_inspection_api_task_update_invalidates_previous_page(
    inspection_case: tuple[Runtime, LocalServices, httpx.AsyncClient],
) -> None:
    _, services, client = inspection_case
    item = await services.commitments.save("更新", brief={"goal": "旧目标", "constraints": []})
    url = f"/api/commitments/{item['id']}/inspection"
    first = await client.get(url, params={"section": "task", "length_bytes": 16})
    assert first.status_code == 200
    page = first.json()
    await services.commitments.save(
        "更新", identifier=item["id"], brief={"goal": "新目标", "constraints": []}
    )
    changed = await client.get(
        url,
        params={
            "section": "task",
            "revision": page["revision"],
            "record_revision": page["record_revision"],
            "offset_bytes": page["next_offset_bytes"],
        },
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "evidence_changed"


@pytest.mark.parametrize("section", ["agents", "reports", "workflows", "memories"])
async def test_task_inspection_api_missing_record_is_404(
    inspection_case: tuple[Runtime, LocalServices, httpx.AsyncClient],
    section: str,
) -> None:
    _, services, client = inspection_case
    item = await services.commitments.save("缺失记录", brief={"goal": "核查", "constraints": []})
    params = {"section": section, "record_id": "missing"}
    if section == "memories":
        params["query"] = "显式检索"
    response = await client.get(f"/api/commitments/{item['id']}/inspection", params=params)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "evidence_not_found"


async def test_registered_inspection_tool_uses_actual_caller_and_rechecks_scope_on_each_page(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    try:
        own = await services.commitments.save(
            "绑定任务", brief={"goal": "own-private", "constraints": []}
        )
        other = await services.commitments.save(
            "其他任务", brief={"goal": "other-private", "constraints": []}
        )
        worker = bind(runtime, own["id"])
        main = runtime.main_session()
        # Retain the main registration: handler authority must still use actual caller.
        tools = runtime.session_tools(main["id"])
        registered = tools["commitment_inspect"]
        assert registered.risk == "read" and registered.preserve_output
        assert not registered.requires_approval
        assert "commitment_inspect" in runtime.session_tools(worker["id"])
        first = await runtime._tool(
            ToolCall(
                "first", "commitment_inspect", json.dumps({"section": "task", "length_bytes": 16})
            ),
            tools,
            "yolo",
            worker["id"],
            "inspection-run",
        )
        assert "error" not in first and first["id"] == own["id"] and first["has_more"]
        assert runtime._selected_replans == {}
        agent = runtime.store.get("agent", worker["id"])
        assert agent is not None
        runtime.store.put("agent", {**agent, "commitment_id": other["id"]})
        rejected = await runtime._tool(
            ToolCall(
                "next",
                "commitment_inspect",
                json.dumps(
                    {
                        "identifier": own["id"],
                        "section": "task",
                        "revision": first["revision"],
                        "record_revision": first["record_revision"],
                        "offset_bytes": first["next_offset_bytes"],
                    }
                ),
            ),
            tools,
            "yolo",
            worker["id"],
            "inspection-run",
        )
        assert "绑定" in rejected["error"] and "content" not in rejected
        assert "own-private" not in json.dumps(rejected)
        assert runtime.tool_session_id is None and runtime._selected_replans == {}
        assert runtime.tasks == {}
    finally:
        await runtime.close()


async def test_registered_inspection_tool_denies_unbound_caller_and_unknown_arguments(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    services = attach_services(runtime, tmp_path)
    try:
        item = await services.commitments.save(
            "主任务", brief={"goal": "private-goal", "constraints": []}
        )
        main = runtime.main_session()
        tools = runtime.session_tools(main["id"])
        unbound = runtime.new_session("未绑定", mode="yolo")
        denied = await runtime._tool(
            ToolCall("unbound", "commitment_inspect", json.dumps({"identifier": item["id"]})),
            tools,
            "yolo",
            unbound["id"],
            "inspection-run",
        )
        assert "绑定" in denied["error"] and "private-goal" not in json.dumps(denied)
        unknown = await runtime._tool(
            ToolCall(
                "unknown",
                "commitment_inspect",
                json.dumps({"identifier": item["id"], "caller_session_id": main["id"]}),
            ),
            tools,
            "yolo",
            main["id"],
            "inspection-run",
        )
        assert "error" in unknown and "task" not in unknown
        inspected = await runtime._tool(
            ToolCall("main", "commitment_inspect", json.dumps({"identifier": item["id"]})),
            tools,
            "yolo",
            main["id"],
            "inspection-run",
        )
        assert "error" not in inspected and inspected["id"] == item["id"]
        assert runtime._selected_replans == {} and runtime.tasks == {}
    finally:
        await runtime.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic"])
@pytest.mark.parametrize("fits_context", [True, False], ids=["complete-wire", "context-refusal"])
async def test_inspection_body_survives_real_runtime_and_codec_or_refuses_lossy_context(
    tmp_path: Path,
    protocol: str,
    fits_context: bool,
) -> None:
    import asyncio
    from typing import Any, cast

    from test_evidence_runtime_review import wire_output
    from test_runtime import FakeClient

    from muli.context import EXCERPT_MARKER, ContextLimitError
    from muli.providers import Message, ModelClient, ModelConfig, ModelTurn, StreamEvent
    from muli.providers.codecs import build_request

    runtime, _ = make_runtime(tmp_path, [])
    requests = 0
    try:
        runtime.config.update(
            {
                "persona": {"prompt": "离线核查任务，只读资料"},
                "models": {"main": {"protocol": protocol}},
                "limits": {"max_output_chars": 32000, "max_context_chars": 12000},
                "compaction": {"enabled": False},
            }
        )
        await runtime.reconfigure()
        services = attach_services(runtime, tmp_path)
        item = await services.commitments.save(
            "协议核查", brief={"goal": "保留完整计划正文", "constraints": ["不执行计划"]}
        )
        plan = '完整🙂\0"\\\n' * (100 if fits_context else 1000)
        item = {
            **item,
            "control": {
                "epoch": 1,
                "approved_epoch": 1,
                "replan_request_id": None,
                "plan": {"epoch": 1, "text": plan},
            },
        }
        runtime.store.put("commitment", item)
        expected = services.task_inspection.read(item["id"], section="task")
        assert not expected["has_more"]
        assert json.loads(expected["content"])["control"]["plan"]["text"] == plan

        class Reader(FakeClient):
            def __init__(self, config: ModelConfig) -> None:
                super().__init__([], [])
                self.config = config

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                nonlocal requests
                requests += 1
                if requests == 1:
                    assert "commitment_inspect" in [tool["function"]["name"] for tool in tools]
                    yield StreamEvent(
                        "done",
                        turn=ModelTurn(
                            tool_calls=[
                                ToolCall(
                                    "inspect-body",
                                    "commitment_inspect",
                                    json.dumps({"identifier": item["id"], "section": "task"}),
                                )
                            ]
                        ),
                    )
                    return
                assert fits_context, "完整结果放不下时不得再请求模型"
                assert requests == 2
                result = next(message for message in reversed(messages) if message.role == "tool")
                assert result.tool_call_id == "inspect-body" and result.preserve_content is True
                assert EXCERPT_MARKER not in result.content
                assert json.loads(result.content) == expected
                body = build_request(self.config, messages, tools, stream=False)
                assert wire_output(self.config.protocol, body, "inspect-body") == result.content
                assert "preserve_content" not in json.dumps(body)
                assert "preserve_output" not in json.dumps(tools)
                yield StreamEvent("done", turn=ModelTurn(content="已完整核查"))

        runtime.client_factory = lambda config: cast(ModelClient, Reader(config))
        main = runtime.main_session()
        runtime.store.add_message(main["id"], "user", "可淘汰旧上下文" * 2000)
        runtime.store.add_message(main["id"], "assistant", "旧轮结束")
        started = await runtime.start(main["id"], "按任务核查完整计划", mode="yolo")
        if fits_context:
            assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "已完整核查"
            assert requests == 2
        else:
            with pytest.raises(ContextLimitError, match="需完整保留的工具结果"):
                await asyncio.wait_for(runtime.wait(started["run_id"]), 5)
            assert requests == 1
            run = runtime.store.get("run", started["run_id"])
            assert run is not None and run["status"] == "failed"
        stored = next(
            message
            for message in runtime.store.messages(main["id"])
            if message.get("tool_call_id") == "inspect-body" and message["role"] == "tool"
        )
        assert stored["preserve_content"] is True
        assert stored["content_provenance"] == "accepted"
        assert EXCERPT_MARKER not in stored["content"]
        assert json.loads(stored["content"]) == expected
        assert runtime._selected_replans == {}
    finally:
        await runtime.close()
