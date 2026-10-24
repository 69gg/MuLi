"""A real mobile/Team/locked-SDK journey for links absent from resources/list."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import terminal
from test_mcp_receive import Chunks
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.server import create_app
from muli.tools.mcp import _FastMCPClient


async def test_search_resource_link_reaches_explicit_read_and_original_phone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils
    from mcp.types import ResourceLink

    uri = "research:///notes/result-51"
    title = "合成观测资料原文"
    query = "合成潮汐观测资料"
    original_text = "离线合成原文：潮汐观测采用三次取样，结论标记 ORIGINAL-EVIDENCE-51。"
    acknowledgement = "已让资料队友检索并按链接读取原文。"
    requests: list[dict[str, Any]] = []
    streams: list[Chunks] = []
    typed_links: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []
    original_call = _FastMCPClient.call_tool

    async def observe_sdk_call(
        client: _FastMCPClient,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        raise_on_error: bool,
    ) -> Any:
        result = await original_call(
            client, name, arguments, timeout=timeout, raise_on_error=raise_on_error
        )
        typed_links.extend(
            block.model_dump(mode="json", by_alias=True)
            for block in result.content
            if isinstance(block, ResourceLink)
        )
        return result

    def mcp_response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "mcp.invalid"
        assert request.headers["accept-encoding"] == "identity"
        if request.method != "POST":
            body = Chunks()
            streams.append(body)
            return httpx.Response(204 if request.method == "DELETE" else 405, stream=body)
        message = json.loads(request.content)
        requests.append(message)
        method = message["method"]
        if method == "notifications/initialized":
            body = Chunks()
            streams.append(body)
            return httpx.Response(202, stream=body)
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "synthetic-search", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "search_documents",
                        "description": "检索合成资料，返回原文资源链接。",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ]
            }
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "tools/call":
            assert message["params"]["name"] == "search_documents"
            assert message["params"]["arguments"] == {"query": query}
            result = {
                "isError": False,
                "content": [
                    {"type": "text", "text": "找到一份合成资料；请显式读原文。"},
                    {
                        "type": "resource_link",
                        "uri": uri,
                        "name": title,
                        "description": "这份检索结果不出现在普通资源目录中。",
                        "mimeType": "text/plain",
                    },
                ],
            }
        elif method == "resources/read":
            assert message["params"]["uri"] == uri
            result = {"contents": [{"uri": uri, "mimeType": "text/plain", "text": original_text}]}
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        raw = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        body = Chunks(raw[:7], raw[7:])
        streams.append(body)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=body)

    def mcp_http(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(mcp_response), trust_env=False, **kwargs
        )

    async def platform_response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        assert all(part["type"] == "text" for part in body["message"])
        sent.append(body)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(sent)}}
        )

    platform = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await platform.handle_async_request(request)

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", mcp_http)
    monkeypatch.setattr(_FastMCPClient, "call_tool", observe_sdk_call)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "mcp": {
                "mcpServers": {
                    "search": {"enabled": True, "url": "https://mcp.invalid/mcp", "timeout": 2}
                }
            },
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7"],
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    release_worker = asyncio.Event()
    main_rounds = 0
    worker_rounds = 0
    worker_id = ""
    final_reply = ""
    runs: dict[str, str] = {}
    search_result: dict[str, Any] = {}
    ordinary: dict[str, Any] = {}
    directory: dict[str, Any] = {}
    reading: dict[str, Any] = {}
    seen: dict[str, list[list[Message]]] = {"main": [], "worker": []}
    observed_runs: list[str] = []
    observed_sessions: list[str] = []

    def observe_run(session_id: str) -> None:
        identifier = runtime.run_ids.get(session_id)
        if identifier and identifier not in observed_runs:
            observed_runs.append(identifier)
        if session_id not in observed_sessions:
            observed_sessions.append(session_id)

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_rounds, worker_id, final_reply
            observe_run(main_id)
            main_rounds += 1
            seen["main"].append(deepcopy(messages))
            assert main_rounds <= 3
            assert not any(tool["function"]["name"].startswith("mcp_") for tool in tools)
            if main_rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-search",
                            "spawn_agent",
                            task_name="资料队友",
                            prompt="检索并读原文",
                        )
                    ]
                )
            elif main_rounds == 2:
                worker_id = tool_result(messages, "spawn-search")["id"]
                runs["acknowledgement"] = runtime.run_ids[main_id]
                turn = ModelTurn(content=acknowledgement)
            else:
                report_message = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user"
                    and message.content.startswith("队友结果，仅为待核实资料")
                )
                report = json.loads(report_message.content.split("\n", 1)[1])
                assert report["id"] == worker_id and report["status"] == "completed"
                final_reply = f"资料队友回报：{report['result']}"
                runs["final"] = runtime.run_ids[main_id]
                turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds, ordinary, search_result, directory, reading
            observe_run(runtime.team.get("资料队友")["session_id"])
            worker_rounds += 1
            seen["worker"].append(deepcopy(messages))
            assert worker_rounds <= 5
            if worker_rounds == 1:
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[call("ordinary-resources", "mcp_resources_list", server="search")]
                )
            elif worker_rounds == 2:
                ordinary = tool_result(messages, "ordinary-resources")
                assert ordinary["resources"] == [] and ordinary["catalog_complete"] is True
                remote = next(
                    tool["function"]["name"]
                    for tool in tools
                    if "_search_documents_" in tool["function"]["name"]
                )
                turn = ModelTurn(tool_calls=[call("search-result", remote, query=query)])
            elif worker_rounds == 3:
                search_result = tool_result(messages, "search-result")
                assert search_result["is_error"] is False and not search_result.get("truncated")
                if search_result.get("resource_links_available") is True:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "result-resources",
                                "mcp_resources_list",
                                server="search",
                                source="tool_result",
                            )
                        ]
                    )
                else:
                    # End the existing real run; the outer assertion owns the feature red.
                    turn = ModelTurn(content="搜索链接尚不能跟进，未读取原文。")
            elif worker_rounds == 4:
                directory = tool_result(messages, "result-resources")
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "read-original",
                            "mcp_resource_read",
                            server="search",
                            ref=directory["resources"][0]["ref"],
                        )
                    ]
                )
            else:
                reading = tool_result(messages, "read-original")
                text = "\n".join(item["text"] for item in reading["contents"])
                turn = ModelTurn(content=f"已读原文：{text}")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], []) if config.model == "worker" else Main([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)

    def diagnostic() -> str:
        states: list[dict[str, Any]] = []
        for identifier in observed_runs[-3:]:
            run = runtime.store.get("run", identifier)
            if run:
                failure = run.get("failure_diagnostic") or {}
                states.append(
                    {
                        "status": run["status"],
                        "error_type": failure.get("exception_type"),
                        "error_present": bool(run.get("error")),
                    }
                )
        results: list[dict[str, Any]] = []
        for session_id in observed_sessions[:2]:
            message = next(
                (
                    item
                    for item in reversed(runtime.store.messages(session_id, limit=4))
                    if item["role"] == "tool"
                ),
                None,
            )
            if message is None:
                continue
            result = json.loads(message["content"])
            results.append(
                {
                    "role": message["role"],
                    "provenance": message.get("content_provenance"),
                    "result_kind": type(result).__name__,
                    "code": result.get("code") if isinstance(result, dict) else None,
                    "has_error": isinstance(result, dict) and "error" in result,
                    "keys": list(result)[:8] if isinstance(result, dict) else [],
                }
            )
        worker_state: dict[str, Any] = {}
        if worker_id:
            agent = runtime.team.get(worker_id)
            report = runtime.store.get("team_report", agent.get("run_id", "")) or {}
            worker_state = {
                "status": agent["status"],
                "mode": agent["mode"],
                "report_status": report.get("status"),
                "execution_status": report.get("execution_status"),
            }
        return json.dumps(
            {
                "runs": states,
                "rounds": [main_rounds, worker_rounds],
                "worker": worker_state,
                "pending_approvals": len(runtime.permissions.pending),
                "tools": results,
            },
            ensure_ascii=True,
        )[:1024]

    async def wait_reply(content: str) -> None:
        try:
            await eventually(runtime, lambda: bool(completed(runtime, content)))
        except TimeoutError:
            pytest.fail(diagnostic())

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            # The persistent main session predates the default-mode config update.
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
            assert (await phone(web, f"请查找{query}并读取原文后告诉我。", 601)).status_code == 200
            await wait_reply(acknowledgement)
            assert await runtime.wait(runs["acknowledgement"]) == acknowledgement
            assert runtime.team.get(worker_id)["mode"] == "yolo"
            release_worker.set()
            try:
                await eventually(
                    runtime,
                    lambda: runtime.team.get(worker_id)["status"] in {"completed", "failed"},
                )
            except TimeoutError:
                pytest.fail(diagnostic())
            worker = runtime.team.get(worker_id)
            assert worker["status"] == "completed", diagnostic()
            try:
                await eventually(
                    runtime, lambda: bool(final_reply) and bool(completed(runtime, final_reply))
                )
            except TimeoutError:
                pytest.fail(diagnostic())
            assert await runtime.wait(runs["final"]) == final_reply
            report = runtime.store.get("team_report", worker["run_id"])
            assert report is not None and report["status"] == "delivered"
            assert report["parent_id"] == main_id and report["agent_id"] == worker_id
            assert report["execution_status"] == "completed"
            assert runtime.store.get("run", worker["run_id"])["status"] == "completed"
            actual_search = [
                item
                for item in runtime.store.messages(worker["session_id"])
                if item.get("tool_call_id") == "search-result"
            ]
            assert len(actual_search) == 1 and actual_search[0]["content_provenance"] == "accepted"
            assert json.loads(actual_search[0]["content"]) == search_result
            assert len(typed_links) == 1 and typed_links[0]["type"] == "resource_link"
            assert typed_links[0]["uri"] == uri and typed_links[0]["name"] == title
            assert main_rounds == 3 and worker_rounds in {3, 5}
            assert all(len(messages) <= 32 for requests_ in seen.values() for messages in requests_)
            assert all(original_text not in message.content for message in seen["worker"][0])
            owner = next(
                item for item in runtime.store.messages(main_id) if item.get("principal") == "owner"
            )
            final_message = completed(runtime, final_reply)[0]
            assert final_message["reply_origin"] == owner["reply_origin"] == worker["reply_origin"]
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, final_message["id"])
            outgoing = [item for item in manager.deliveries() if delivery_text(item) == final_reply]
            assert len(outgoing) == 1
            delivery = await terminal(manager, outgoing[0]["id"])
            assert delivery["state"] == "sent" and delivery["route"]["target"] == "7"
            assert (
                sum(
                    "".join(part["data"]["text"] for part in item["message"]) == final_reply
                    for item in sent
                )
                == 1
            )
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200 and final_reply in history.text
            methods = [item["method"] for item in requests]
            assert methods.count("resources/list") == methods.count("tools/call") == 1
            # Target red: a typed successful search link was lost after real SDK decoding.
            assert search_result.get("resource_links_available") is True, (
                "真实搜索、accepted 工具回执、队友报告与原私聊回报均完成，"
                f"但普通目录外的 typed resource_link 仍不可发现：{search_result}"
            )
            assert worker_rounds == 5 and methods.count("resources/read") == 1
            assert directory["untrusted"] is True and directory["catalog_complete"] is True
            assert len(directory["resources"]) == 1
            assert directory["resources"][0]["name"] == title
            assert directory["resources"][0]["uri"] == uri
            assert reading["untrusted"] is True and reading["is_error"] is False
            assert reading["contents"][0]["text"] == original_text
            assert original_text in final_reply
            stored_read = next(
                item
                for item in runtime.store.messages(worker["session_id"])
                if item.get("tool_call_id") == "read-original"
            )
            assert (
                stored_read["content_provenance"] == "accepted" and stored_read["preserve_content"]
            )
            assert json.loads(stored_read["content"]) == reading
    finally:
        release_worker.set()
        await runtime.close()
    assert streams and all(stream.closed == 1 for stream in streams)
