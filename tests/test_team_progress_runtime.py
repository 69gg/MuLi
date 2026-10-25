"""A running child's real parent handoff, separate from its terminal report."""

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

from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


async def test_running_child_asks_main_on_its_original_phone_while_another_owner_run_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastmcp")
    from mcp.shared import _httpx_utils

    uri = "research:///team/progress-52"
    original = "合成资料：本次只核查草稿，尚未发布。证据标记 ORIGINAL-PROGRESS-52。"
    question = "请主人确认下一步只补出处，还是还要补对照表？"
    assignment = "请让资料队友核查草稿，执行中有澄清先告诉我。"
    acknowledgement = "已安排资料队友核查，阶段问题会回到本次私聊。"
    other_input = "这里是另一私聊，请只记住我刚才这条普通补充。"
    other_reply = "已收到这条普通补充。"
    worker_done = "本轮核查已停在等待明确补充的边界，没有发布或重做未知操作。"
    requests: list[dict[str, Any]] = []
    streams: list[Chunks] = []
    sent: list[dict[str, Any]] = []

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
                "capabilities": {"resources": {}},
                "serverInfo": {"name": "synthetic-progress-evidence", "version": "1"},
            }
        elif method == "resources/list":
            result = {"resources": [{"uri": uri, "name": "合成草稿资料", "mimeType": "text/plain"}]}
        elif method == "resources/read":
            assert message["params"]["uri"] == uri
            result = {"contents": [{"uri": uri, "mimeType": "text/plain", "text": original}]}
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
                    "evidence": {"enabled": True, "url": "https://mcp.invalid/mcp", "timeout": 2}
                }
            },
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7", "8"],
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    release_worker = asyncio.Event()
    other_entered = asyncio.Event()
    release_other = asyncio.Event()
    progress_attempted = asyncio.Event()
    finish_worker = asyncio.Event()
    main_rounds = 0
    worker_rounds = 0
    worker_id = ""
    worker_run = ""
    progress_message = ""
    stage_reply = ""
    final_reply = ""
    reading: dict[str, Any] = {}
    progress_result: dict[str, Any] = {}
    runs: dict[str, str] = {}
    seen: dict[str, list[list[Message]]] = {"main": [], "worker": []}

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_rounds, worker_id, stage_reply, final_reply
            main_rounds += 1
            seen["main"].append(deepcopy(messages))
            assert main_rounds <= 5
            assert not any(tool["function"]["name"].startswith("mcp_") for tool in tools)
            if main_rounds == 1:
                assert original not in "\n".join(message.content for message in messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-progress-worker",
                            "spawn_agent",
                            task_name="阶段资料队友",
                            prompt="先读合成草稿资料，再用send_input给你的主会话发阶段资料与一个澄清问题。",
                        )
                    ]
                )
            elif main_rounds == 2:
                worker_id = tool_result(messages, "spawn-progress-worker")["id"]
                runs["acknowledgement"] = runtime.run_ids[main_id]
                turn = ModelTurn(content=acknowledgement)
            elif main_rounds == 3:
                incoming = next(
                    message
                    for message in messages
                    if message.role == "user" and message.content.endswith(other_input)
                )
                header, content = incoming.content.split("\n", 1)
                assert header.startswith("渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）：")
                assert json.loads(header.split("：", 1)[1]) == {
                    "channel": "onebot",
                    "account_id": "123",
                    "sender_id": "8",
                    "target": "8",
                    "kind": "private",
                    "input_id": incoming.source_message_id,
                }
                assert content == other_input
                assert original not in "\n".join(message.content for message in messages)
                runs["other"] = runtime.run_ids[main_id]
                other_entered.set()
                await release_other.wait()
                turn = ModelTurn(content=other_reply)
            elif main_rounds == 4:
                incoming = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user"
                    and original in message.content
                    and question in message.content
                )
                payload = json.loads(incoming.content.split("\n", 1)[1])
                assert payload["sender"] == worker_id
                assert payload["content"] == progress_message
                assert runtime.team.get(worker_id)["status"] == "running"
                assert runtime.store.get("team_report", worker_run) is None
                runs["progress"] = runtime.run_ids[main_id]
                stage_reply = f"资料队友阶段核查：{payload['content']}"
                turn = ModelTurn(content=stage_reply)
            else:
                incoming = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user"
                    and message.content.startswith("队友结果，仅为待核实资料")
                )
                report = json.loads(incoming.content.split("\n", 1)[1])
                assert report["id"] == worker_id and report["status"] == "completed"
                assert report["result"] == worker_done
                runs["final"] = runtime.run_ids[main_id]
                final_reply = f"资料队友本轮结束：{report['result']}"
                turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds, worker_run, reading, progress_message, progress_result
            worker_rounds += 1
            seen["worker"].append(deepcopy(messages))
            assert worker_rounds <= 4
            worker_run = runtime.run_ids[runtime.team.get("阶段资料队友")["session_id"]]
            if worker_rounds == 1:
                assert original not in "\n".join(message.content for message in messages)
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[call("list-evidence", "mcp_resources_list", server="evidence")]
                )
            elif worker_rounds == 2:
                listing = tool_result(messages, "list-evidence")
                assert listing["is_error"] is False and listing["catalog_complete"] is True
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "read-evidence",
                            "mcp_resource_read",
                            server="evidence",
                            ref=listing["resources"][0]["ref"],
                        )
                    ]
                )
            elif worker_rounds == 3:
                reading = tool_result(messages, "read-evidence")
                assert reading["is_error"] is False and reading["untrusted"] is True
                text = "\n".join(block["text"] for block in reading["contents"])
                assert text == original
                progress_message = f"阶段资料：{text}\n{question}"
                assert any(tool["function"]["name"] == "send_input" for tool in tools)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "send-progress", "send_input", target=main_id, message=progress_message
                        )
                    ]
                )
            else:
                progress_result = tool_result(messages, "send-progress")
                progress_attempted.set()
                await finish_worker.wait()
                turn = ModelTurn(content=worker_done)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], []) if config.model == "worker" else Main([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)

    def diagnostic() -> str:
        states = []
        for identifier in list(dict.fromkeys([*runs.values(), worker_run]))[-3:]:
            run = runtime.store.get("run", identifier)
            if run is not None:
                states.append(
                    {
                        "status": run["status"],
                        "error_type": (run.get("failure_diagnostic") or {}).get("exception_type"),
                        "error_present": bool(run.get("error")),
                    }
                )
        return json.dumps(
            {
                "runs": states,
                "rounds": [main_rounds, worker_rounds],
                "progress_keys": list(progress_result)[:8],
                "progress_has_error": "error" in progress_result,
                "pending_approvals": len(runtime.permissions.pending),
            },
            ensure_ascii=True,
        )[:1024]

    async def reach(event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(event.wait(), 3)
        except TimeoutError:
            pytest.fail(diagnostic())

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
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
            assert (await phone(web, assignment, 701)).status_code == 200
            await wait_reply(acknowledgement)
            assert await runtime.wait(runs["acknowledgement"]) == acknowledgement
            assert worker_id and runtime.team.get(worker_id)["mode"] == "yolo"
            assert (await phone(web, other_input, 702, sender=8)).status_code == 200
            await reach(other_entered)
            assert runtime.store.get("run", runs["other"])["status"] == "running"
            release_worker.set()
            await reach(progress_attempted)

            worker = runtime.team.get(worker_id)
            assert worker["status"] == "running" and worker["parent_id"] == main_id
            assert worker["run_id"] == worker_run and worker["generation"] == 1
            assert runtime.store.get("run", worker_run)["status"] == "running"
            assert runtime.store.get("team_report", worker_run) is None
            assert runtime.store.get("run", runs["other"])["status"] == "running"
            stored = runtime.store.messages(worker_id)
            actual_read = next(
                item for item in stored if item.get("tool_call_id") == "read-evidence"
            )
            actual_send = next(
                item for item in stored if item.get("tool_call_id") == "send-progress"
            )
            assert (
                actual_read["content_provenance"] == actual_send["content_provenance"] == "accepted"
            )
            assert actual_read["preserve_content"] is True
            assert json.loads(actual_read["content"]) == reading
            assert reading["contents"][0]["text"] == original
            assert json.loads(actual_send["content"]) == progress_result
            methods = [item["method"] for item in requests]
            assert methods.count("resources/list") == methods.count("resources/read") == 1
            assert "tools/call" not in methods and "tools/list" not in methods
            original_owner = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("principal") == "owner" and item["content"] == assignment
            )
            assert worker["reply_origin"] == original_owner["reply_origin"]
            # Target red: the real existing tool currently rejects its child's main parent.
            assert progress_result.get("accepted") is True, (
                "真实资料读取与完整send_input回执均已入库，child和phone8主run仍运行且无最终报告；"
                f"但固定直接main parent仍被拒绝：{progress_result}"
            )

            release_other.set()
            try:
                await eventually(
                    runtime, lambda: bool(stage_reply) and bool(completed(runtime, stage_reply))
                )
            except TimeoutError:
                pytest.fail(diagnostic())
            assert await runtime.wait(runs["progress"]) == stage_reply
            assert runs["progress"] == runs["other"]
            assert runtime.team.get(worker_id)["status"] == "running"
            assert runtime.store.get("run", worker_run)["status"] == "running"
            assert runtime.store.get("team_report", worker_run) is None
            incoming = next(
                item
                for item in runtime.store.messages(main_id)
                if item["id"] == progress_result["receipt_id"]
            )
            assert incoming["principal"] == f"agent:{worker_id}"
            assert incoming["reply_origin"] == original_owner["reply_origin"]
            stage_message = completed(runtime, stage_reply)[0]
            assert stage_message["reply_origin"] == worker["reply_origin"]
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, stage_message["id"])
            stages = [item for item in manager.deliveries() if delivery_text(item) == stage_reply]
            assert stages == []
            assert (
                sum(
                    "".join(part["data"]["text"] for part in item["message"]) == stage_reply
                    for item in sent
                )
                == 0
            )
            assert not any(
                str(item["user_id"]) == "8"
                and original in "".join(part["data"]["text"] for part in item["message"])
                for item in sent
            )
            other_message = completed(runtime, other_reply)[0]
            assert other_message["reply_origin"] != stage_message["reply_origin"]
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(item["content"] == stage_reply for item in history.json()["messages"])

            finish_worker.set()
            try:
                await eventually(
                    runtime, lambda: bool(final_reply) and bool(completed(runtime, final_reply))
                )
            except TimeoutError:
                pytest.fail(diagnostic())
            assert await runtime.wait(runs["final"]) == final_reply
            worker = runtime.team.get(worker_id)
            assert worker["status"] == "completed" and worker["generation"] == 1
            report = runtime.store.get("team_report", worker_run)
            assert report is not None and report["execution_status"] == "completed"
            assert report["status"] == "delivered" and report["agent_id"] == worker_id
            assert main_rounds == 5 and worker_rounds == 4
            assert all(len(messages) <= 32 for views in seen.values() for messages in views)
            assert (
                len([item for item in manager.deliveries() if delivery_text(item) == stage_reply])
                == 0
            )
    finally:
        release_worker.set()
        release_other.set()
        finish_worker.set()
        await runtime.close()
    assert streams and all(stream.closed == 1 for stream in streams)


async def test_owner_answer_after_reopen_continues_the_same_child_without_replaying_unknown_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "合成草稿资料 ORIGINAL-PERSISTED-PROGRESS-52；尚未发布。"
    source = tmp_path / "original-draft.txt"
    source.write_text(original, encoding="utf-8")
    assignment = "请安排资料队友核查这份草稿，有澄清先问我，再等明确补充。"
    acknowledgement = "资料核查已交给队友，阶段问题会回本次私聊。"
    question = "请确认只补出处，还是也补对照表？"
    answer = "只补出处，不要对照表，也不要重做结果未知的投递。"
    answer_acknowledgement = "回答已交回原资料队友，继续同一任务。"
    first_done = "本轮资料已核查，等待主人明确补充；没有发布。"
    boundary_reply = "首轮已在澄清边界结束，等待主人明确回答。"
    sent: list[dict[str, Any]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        body = json.loads(request.content)
        sent.append(body)
        text = "".join(part["data"]["text"] for part in body["message"])
        if text == boundary_reply:
            assert (
                sum(
                    "".join(part["data"]["text"] for part in item["message"]) == boundary_reply
                    for item in sent
                )
                == 1
            )
            raise httpx.ReadTimeout("one explicit unconfirmed boundary reply", request=request)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(sent)}}
        )

    platform = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await platform.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "extensions": {"watch_enabled": False},
            "limits": {
                "max_context_chars": 16000,
                "max_output_chars": 6000,
                "max_pending_runs": 3,
            },
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
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
    instance_id = runtime.store.instance_id
    begin_read, finish_first, finish_resumed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    resumed_read = asyncio.Event()
    worker_id = ""
    first_worker_run = ""
    second_worker_run = ""
    stage_reply = ""
    final_reply = ""
    first_main_rounds = first_worker_rounds = 0
    resumed_main_rounds = resumed_worker_rounds = 0
    runs: dict[str, str] = {}
    seen: list[list[Message]] = []
    active_runtime = runtime

    class FirstMain(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal first_main_rounds, worker_id, stage_reply
            first_main_rounds += 1
            seen.append(deepcopy(messages))
            runs["current_main"] = runtime.run_ids[main_id]
            assert first_main_rounds <= 5
            if first_main_rounds == 1:
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn-persistent-progress",
                            "spawn_agent",
                            task_name="持久澄清队友",
                            prompt=f"读取草稿 {source}，再向主会话交阶段资料并询问是否补对照表。",
                        )
                    ]
                )
            elif first_main_rounds == 2:
                worker_id = tool_result(messages, "spawn-persistent-progress")["id"]
                runs["assigned"] = runtime.run_ids[main_id]
                turn = ModelTurn(content=acknowledgement)
            elif first_main_rounds == 3:
                incoming = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user" and question in message.content
                )
                payload = json.loads(incoming.content.split("\n", 1)[1])
                assert payload["sender"] == worker_id and original in payload["content"]
                assert runtime.team.get(worker_id)["status"] == "running"
                assert runtime.store.get("team_report", first_worker_run) is None
                stage_reply = f"队友已读资料：{payload['content']}"
                runs["question"] = runtime.run_ids[main_id]
                turn = ModelTurn(content=stage_reply)
            elif first_main_rounds == 4:
                report = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user"
                    and message.content.startswith("队友结果，仅为待核实资料")
                )
                payload = json.loads(report.content.split("\n", 1)[1])
                assert payload["id"] == worker_id and payload["result"] == first_done
                runs["boundary"] = runtime.run_ids[main_id]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "send-unconfirmed-boundary",
                            "channel_send",
                            channel="onebot",
                            target="7",
                            text=boundary_reply,
                            idempotency_key="progress-boundary-once",
                        )
                    ]
                )
            else:
                delivery = tool_result(messages, "send-unconfirmed-boundary")
                assert isinstance(delivery["id"], str) and "error" not in delivery
                turn = ModelTurn(content=boundary_reply)
            yield StreamEvent("done", turn=turn)

    class FirstWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal first_worker_rounds, first_worker_run
            first_worker_rounds += 1
            seen.append(deepcopy(messages))
            assert first_worker_rounds <= 3
            first_worker_run = runtime.run_ids[runtime.team.get("持久澄清队友")["id"]]
            if first_worker_rounds == 1:
                assert original not in "\n".join(message.content for message in messages)
                await begin_read.wait()
                turn = ModelTurn(
                    tool_calls=[call("original-before-close", "fs_read", path=str(source))]
                )
            elif first_worker_rounds == 2:
                evidence = tool_result(messages, "original-before-close")
                assert evidence["content"] == original
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "question-before-close",
                            "send_input",
                            target=main_id,
                            message=f"阶段资料：{evidence['content']}\n{question}",
                        )
                    ]
                )
            else:
                assert tool_result(messages, "question-before-close")["accepted"] is True
                await finish_first.wait()
                turn = ModelTurn(content=first_done)
            yield StreamEvent("done", turn=turn)

    def first_factory(config: ModelConfig) -> FakeClient:
        return FirstWorker([], []) if config.model == "worker" else FirstMain([], [])

    def diagnostic() -> str:
        states = []
        for role, identifier in (
            ("main", runs.get("current_main", "")),
            ("worker", second_worker_run or first_worker_run),
        ):
            run = active_runtime.store.get("run", identifier)
            if run is not None:
                failure = run.get("failure_diagnostic") or {}
                states.append(
                    {
                        "role": role,
                        "status": run["status"],
                        "error_type": failure.get("exception_type"),
                        "last_frame": failure.get("frames", [])[-1:],
                    }
                )
        return json.dumps(
            {
                "runs": states,
                "rounds": [
                    first_main_rounds,
                    first_worker_rounds,
                    resumed_main_rounds,
                    resumed_worker_rounds,
                ],
            }
        )[:1024]

    async def wait_reply(content: str) -> None:
        try:
            await eventually(active_runtime, lambda: bool(completed(active_runtime, content)))
        except TimeoutError:
            pytest.fail(diagnostic())

    runtime.client_factory = first_factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    original_origin: dict[str, Any] = {}
    old_report: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
            assert (await phone(web, assignment, 801)).status_code == 200
            await wait_reply(acknowledgement)
            assert await runtime.wait(runs["assigned"]) == acknowledgement
            begin_read.set()
            try:
                await eventually(
                    runtime, lambda: bool(stage_reply) and bool(completed(runtime, stage_reply))
                )
            except TimeoutError:
                pytest.fail(diagnostic())
            assert await runtime.wait(runs["question"]) == stage_reply
            agent = runtime.team.get(worker_id)
            assert agent["status"] == "running" and agent["generation"] == 1
            assert runtime.store.get("team_report", first_worker_run) is None
            original_origin = deepcopy(agent["reply_origin"])
            manager = app.state.channel_application.manager
            stage_message = completed(runtime, stage_reply)[0]
            await processed_reply(runtime, manager, stage_message["id"])
            stages = [item for item in manager.deliveries() if delivery_text(item) == stage_reply]
            assert stages == []
            finish_first.set()
            await wait_reply(boundary_reply)
            assert await runtime.wait(runs["boundary"]) == boundary_reply
            boundary_message = completed(runtime, boundary_reply)[0]
            await processed_reply(runtime, manager, boundary_message["id"])
            boundaries = [
                item for item in manager.deliveries() if delivery_text(item) == boundary_reply
            ]
            assert len(boundaries) == 1
            unknown = deepcopy(await terminal(manager, boundaries[0]["id"]))
            assert unknown["state"] == "unknown" and unknown["attempts"] == 1
            agent = runtime.team.get(worker_id)
            assert agent["status"] == "completed" and agent["generation"] == 1
            old_report = deepcopy(runtime.store.get("team_report", first_worker_run))
            assert (
                old_report["execution_status"] == "completed"
                and old_report["status"] == "delivered"
            )
            assert runtime.conversation.pending(worker_id) == []
            assert first_main_rounds == 5 and first_worker_rounds == 3
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        begin_read.set()
        finish_first.set()
        await runtime.close()

    def completion_proof() -> dict[str, Any]:
        events = reopened.store.team_completions(worker_id, 1, limit=2)
        assert len(events) == 1 and events[0]["session_id"] == worker_id
        data = events[0]["data"]
        assert {
            key: data[key]
            for key in ("run_id", "agent_id", "generation", "completion_revision", "status")
        } == {
            "run_id": second_worker_run,
            "agent_id": worker_id,
            "generation": 2,
            "completion_revision": 2,
            "status": "completed",
        }
        return data

    class ResumedMain(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal resumed_main_rounds, final_reply
            resumed_main_rounds += 1
            seen.append(deepcopy(messages))
            runs["current_main"] = reopened.run_ids[main_id]
            assert resumed_main_rounds <= 3
            if resumed_main_rounds == 1:
                incoming = next(
                    message
                    for message in messages
                    if message.role == "user" and message.content.endswith(answer)
                )
                header, content = incoming.content.split("\n", 1)
                assert header.startswith("渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）：")
                assert json.loads(header.split("：", 1)[1]) == {
                    "channel": "onebot",
                    "account_id": "123",
                    "sender_id": "7",
                    "target": "7",
                    "kind": "private",
                    "input_id": incoming.source_message_id,
                }
                assert content == answer
                turn = ModelTurn(
                    tool_calls=[
                        call("answer-same-child", "send_input", target=worker_id, message=answer)
                    ]
                )
            elif resumed_main_rounds == 2:
                receipt = tool_result(messages, "answer-same-child")
                assert receipt["accepted"] is True and receipt["agent_id"] == worker_id
                assert receipt["duplicate"] is False
                runs["answered"] = reopened.run_ids[main_id]
                turn = ModelTurn(content=answer_acknowledgement)
            else:
                incoming = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user"
                    and message.content.startswith("队友结果，仅为待核实资料")
                )
                payload = json.loads(incoming.content.split("\n", 1)[1])
                report = reopened.store.get("team_report", second_worker_run)
                completion = completion_proof()
                assert report["id"] == completion["run_id"] and report["agent_id"] == worker_id
                assert report["execution_status"] == "completed"
                assert payload["id"] == worker_id and payload["status"] == "completed"
                assert original in payload["result"] and answer in payload["result"]
                runs["finished"] = reopened.run_ids[main_id]
                final_reply = f"原资料队友已按主人回答完成核查：{payload['result']}"
                turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    class ResumedWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal resumed_worker_rounds, second_worker_run
            resumed_worker_rounds += 1
            seen.append(deepcopy(messages))
            assert resumed_worker_rounds <= 2
            agent = reopened.team.get(worker_id)
            assert agent["generation"] == 2 and agent["reply_origin"] == original_origin
            second_worker_run = reopened.run_ids[worker_id]
            assert second_worker_run != first_worker_run
            supplement = next(
                message
                for message in reversed(messages)
                if message.role == "user" and answer in message.content
            )
            payload = json.loads(supplement.content.split("\n", 1)[1])
            assert payload == {"sender": main_id, "content": answer}
            if resumed_worker_rounds == 1:
                assert tool_result(messages, "original-before-close")["content"] == original
                turn = ModelTurn(
                    tool_calls=[call("original-after-reopen", "fs_read", path=str(source))]
                )
            else:
                evidence = tool_result(messages, "original-after-reopen")
                assert evidence["content"] == original
                resumed_read.set()
                await finish_resumed.wait()
                turn = ModelTurn(
                    content=f"已核原资料：{evidence['content']}；执行本次补充：{payload['content']}"
                )
            yield StreamEvent("done", turn=turn)

    def resumed_factory(config: ModelConfig) -> FakeClient:
        return ResumedWorker([], []) if config.model == "worker" else ResumedMain([], [])

    reopened = Runtime(
        ConfigFile(runtime.config.path), runtime.data_dir, client_factory=resumed_factory
    )  # type: ignore[arg-type]
    active_runtime = reopened
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                reopened.store.instance_id == instance_id
                and reopened.main_session()["id"] == main_id
            )
            agent = reopened.team.get(worker_id)
            assert agent["status"] == "completed" and agent["generation"] == 1
            assert agent["reply_origin"] == original_origin and agent["parent_id"] == main_id
            assert resumed_main_rounds == resumed_worker_rounds == 0
            manager = reopened_app.state.channel_application.manager
            assert manager.delivery(unknown["id"]) == unknown
            assert (await phone(web, answer, 802)).status_code == 200
            await wait_reply(answer_acknowledgement)
            assert await reopened.wait(runs["answered"]) == answer_acknowledgement
            try:
                await asyncio.wait_for(resumed_read.wait(), 3)
            except TimeoutError:
                pytest.fail(diagnostic())
            agent = reopened.team.get(worker_id)
            assert agent["status"] == "running" and agent["generation"] == 2
            assert reopened.store.get("team_report", second_worker_run) is None
            assert reopened.store.get("team_report", first_worker_run) == old_report
            stored = reopened.store.messages(worker_id)
            actual_read = next(
                item for item in stored if item.get("tool_call_id") == "original-after-reopen"
            )
            assert actual_read["content_provenance"] == "accepted"
            assert json.loads(actual_read["content"])["content"] == original
            assert len(reopened.store.list("agent")) == 1
            finish_resumed.set()
            try:
                await eventually(
                    reopened, lambda: bool(final_reply) and bool(completed(reopened, final_reply))
                )
            except TimeoutError:
                pytest.fail(diagnostic())
            assert await reopened.wait(runs["finished"]) == final_reply
            final_message = completed(reopened, final_reply)[0]
            assert final_message["reply_origin"] == original_origin
            await processed_reply(reopened, manager, final_message["id"])
            finals = [item for item in manager.deliveries() if delivery_text(item) == final_reply]
            assert finals == []
            assert manager.delivery(unknown["id"]) == unknown
            assert (
                sum(
                    "".join(part["data"]["text"] for part in item["message"]) == boundary_reply
                    for item in sent
                )
                == 1
            )
            assert (
                sum(
                    "".join(part["data"]["text"] for part in item["message"]) == final_reply
                    for item in sent
                )
                == 0
            )
            restored = reopened.team.get(worker_id)
            report = reopened.store.get("team_report", second_worker_run)
            assert restored["id"] == worker_id and restored["generation"] == 2
            assert restored["status"] == "completed" and restored["reply_origin"] == original_origin
            assert report["execution_status"] == "completed" and report["status"] == "delivered"
            completion = completion_proof()
            assert report["id"] == completion["run_id"] and report["agent_id"] == worker_id
            assert reopened.store.get("team_report", first_worker_run) == old_report
            assert resumed_main_rounds == 3 and resumed_worker_rounds == 2
            current_calls = [
                call["name"]
                for item in reopened.store.messages(worker_id)
                if item["role"] == "assistant"
                for call in item.get("tool_calls", [])
            ]
            assert current_calls == ["fs_read", "send_input", "fs_read"]
            main_calls = [
                call["name"]
                for item in reopened.store.messages(main_id)
                for call in item.get("tool_calls", [])
            ]
            assert main_calls == ["spawn_agent", "channel_send", "send_input"]
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(item["content"] == final_reply for item in history.json()["messages"])
            assert all(len(messages) <= 32 for messages in seen)
            await asyncio.wait_for(reopened_app.state.close(), 3)
    finally:
        finish_resumed.set()
        await reopened.close()
