"""A real child's published file, explicitly requested on the owner's current phone."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import terminal
from test_mobile_task_control_followup import phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


async def test_main_replies_with_a_real_child_file_on_the_requesting_private_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "核查草稿.txt"
    content = "合成核查成果：本地附件副本。\nFILE-REPLY-53-END\n"
    expected_bytes = content.encode("utf-8")
    expected_digest = hashlib.sha256(expected_bytes).hexdigest()
    assignment = "请让队友生成一份核查草稿，先保留附件，不自动发文件。"
    acknowledgement = "任务已登记，队友将生成本地草稿附件。"
    report_reply = "队友已交回真实草稿附件，等待主人明确索取。"
    other_input = "另一私聊的普通补充，请只确认收到。"
    other_reply = "已收到另一私聊的普通补充。"
    request_files = "请把刚才队友的草稿文件直接发到这里。"
    missing_reply = "已找到队友的真实附件；固定本次私聊来源的文件回复入口尚未提供。"
    queued_reply = "已将原附件交给本次私聊的文件投递队列；送达状态以投递记录为准。"
    stable_key = "requested-child-draft-53"
    text_requests: list[dict[str, Any]] = []
    file_requests: list[dict[str, Any]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid" and request.method == "POST"
        body = json.loads(request.content)
        if request.url.path == "/send_private_msg":
            assert all(part["type"] == "text" for part in body["message"])
            text_requests.append(body)
        else:
            assert request.url.path == "/upload_private_file"
            assert set(body) == {"user_id", "file", "name"}
            file_requests.append(body)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": len(text_requests) + len(file_requests)},
            },
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
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
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
                    "owner_ids": ["7", "8"],
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    phase = "assignment"
    rounds = {"assignment": 0, "report": 0, "other": 0, "files": 0}
    runs: dict[str, str] = {}
    task_id, worker_id = "", ""
    published: dict[str, Any] = {}
    reported_attachment: dict[str, Any] = {}
    queued: dict[str, Any] = {}
    reply_files_available: bool | None = None
    worker_rounds = 0
    release_worker = asyncio.Event()
    worker_entered = asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id, reported_attachment, queued, reply_files_available
            assert len(messages) <= 32
            rounds[phase] += 1
            current = rounds[phase]
            run_id = runtime.run_ids[main_id]
            assert runs.setdefault(phase, run_id) == run_id
            names = {tool["function"]["name"] for tool in tools}
            assert "channel_send" in names
            if phase == "assignment":
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-file-task",
                                "commitment_save",
                                title="私聊索取核查草稿",
                                brief={
                                    "goal": "生成合成草稿并保留附件，主人明确索取后再发文件",
                                    "constraints": ["不操作真实服务", "不自动发送文件"],
                                },
                            )
                        ]
                    )
                elif current == 2:
                    task_id = tool_result(messages, "save-file-task")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-file-worker",
                                "spawn_agent",
                                task_name="草稿文件队友",
                                prompt="在临时工作区生成合成核查草稿，用file_publish保留副本并报告实际附件元数据；不要发送文件。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert current == 3
                    worker_id = tool_result(messages, "spawn-file-worker")["id"]
                    turn = ModelTurn(content=acknowledgement)
            elif phase == "report":
                assert current == 1
                incoming = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user"
                    and message.content.startswith("队友结果，仅为待核实资料")
                )
                report = json.loads(incoming.content.split("\n", 1)[1])
                assert report["id"] == worker_id and report["status"] == "completed"
                result = json.loads(report["result"])
                reported_attachment = result["attachment"]
                assert reported_attachment == published
                turn = ModelTurn(content=report_reply)
            elif phase == "other":
                assert current == 1
                assert any(message.content == other_input for message in messages)
                turn = ModelTurn(content=other_reply)
            else:
                assert phase == "files"
                assert reported_attachment and any(
                    message.content == request_files for message in messages
                )
                if current == 1:
                    reply_files_available = "channel_reply_files" in names
                    # A missing entry ends normally; the outer oracle identifies the gap.
                    turn = (
                        ModelTurn(
                            tool_calls=[
                                call(
                                    "reply-child-file",
                                    "channel_reply_files",
                                    attachment_ids=[reported_attachment["id"]],
                                    idempotency_key=stable_key,
                                )
                            ]
                        )
                        if reply_files_available
                        else ModelTurn(content=missing_reply)
                    )
                else:
                    assert current == 2 and reply_files_available is True
                    queued = tool_result(messages, "reply-child-file")
                    assert "error" not in queued and queued["id"]
                    turn = ModelTurn(content=queued_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds, published
            assert len(messages) <= 32
            worker_rounds += 1
            runs["worker"] = runtime.run_ids[runtime.team.get("草稿文件队友")["session_id"]]
            if worker_rounds == 1:
                worker_entered.set()
                await asyncio.wait_for(release_worker.wait(), 3)
                turn = ModelTurn(
                    tool_calls=[
                        call("write-child-file", "fs_write", path=str(source), content=content)
                    ]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "write-child-file")["path"] == str(source)
                turn = ModelTurn(
                    tool_calls=[call("publish-child-file", "file_publish", path=str(source))]
                )
            else:
                assert worker_rounds == 3
                published = tool_result(messages, "publish-child-file")
                assert published["sha256"] == expected_digest
                assert published["size"] == len(expected_bytes) and published["name"] == source.name
                turn = ModelTurn(content=json.dumps({"attachment": published}, ensure_ascii=False))
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)

    def diagnostic() -> str:
        states: dict[str, Any] = {}
        for role in (phase, "worker"):
            if identifier := runs.get(role):
                run = runtime.store.get("run", identifier)
                if run is not None:
                    states[role] = {
                        "status": run["status"],
                        "error_type": (run.get("failure_diagnostic") or {}).get("exception_type"),
                        "error_present": bool(run.get("error")),
                    }
        return json.dumps(
            {
                "runs": states,
                "rounds": [*rounds.values(), worker_rounds],
                "reply_files_available": reply_files_available,
                "pending_approvals": len(runtime.permissions.pending),
                "file_calls": len(file_requests),
            },
            ensure_ascii=True,
        )[:1024]

    async def wait_reply(content: str) -> None:
        try:
            await eventually(runtime, lambda: bool(completed(runtime, content)))
        except TimeoutError:
            pytest.fail(diagnostic())
        assert await runtime.wait(runs[phase]) == content

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
            await asyncio.wait_for(worker_entered.wait(), 3)
            assert worker_id and runtime.team.get(worker_id)["mode"] == "yolo"
            phase = "report"
            release_worker.set()
            await wait_reply(report_reply)
            worker = runtime.team.get(worker_id)
            assert worker["status"] == "completed" and worker["parent_id"] == main_id
            assert worker["commitment_id"] == task_id
            assert worker["generation"] == worker["completion_revision"] == 1
            assert runtime.store.get("run", worker["run_id"])["status"] == "completed"
            report = runtime.store.get("team_report", worker["run_id"])
            assert report is not None and report["execution_status"] == "completed"
            assert report["status"] == "delivered" and report["agent_id"] == worker_id
            assert report["commitment_id"] == task_id and report["parent_id"] == main_id
            report_body = json.loads(report["content"].split("\n", 1)[1])
            assert json.loads(report_body["result"])["attachment"] == reported_attachment
            worker_messages = runtime.store.messages(worker_id)
            write_result = next(
                item for item in worker_messages if item.get("tool_call_id") == "write-child-file"
            )
            publish_result = next(
                item for item in worker_messages if item.get("tool_call_id") == "publish-child-file"
            )
            assert (
                write_result["content_provenance"]
                == publish_result["content_provenance"]
                == "accepted"
            )
            assert json.loads(publish_result["content"]) == reported_attachment
            attachment_id = reported_attachment["id"]
            assert source.read_bytes() == expected_bytes
            downloaded = await web.get(f"/api/uploads/{attachment_id}/download")
            assert downloaded.status_code == 200 and downloaded.content == expected_bytes
            assert hashlib.sha256(downloaded.content).hexdigest() == expected_digest
            assert file_requests == []

            phase = "other"
            assert (await phone(web, other_input, 702, sender=8)).status_code == 200
            await wait_reply(other_reply)
            other_message = completed(runtime, other_reply)[0]
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, other_message["id"])
            assert other_message["reply_origin"]["route"]["target"] == "8"
            assert file_requests == []

            phase = "files"
            assert (await phone(web, request_files, 703)).status_code == 200
            try:
                await eventually(
                    runtime,
                    lambda: bool(
                        completed(runtime, queued_reply) or completed(runtime, missing_reply)
                    ),
                )
            except TimeoutError:
                pytest.fail(diagnostic())
            expected_reply = queued_reply if reply_files_available else missing_reply
            assert await runtime.wait(runs["files"]) == expected_reply
            assert runtime.store.get("run", runs["files"])["status"] == "completed"
            assert len({runs[key] for key in ("assignment", "report", "other", "files")}) == 4
            assert rounds["assignment"] == 3 and rounds["report"] == rounds["other"] == 1
            assert worker_rounds == 3
            # Target red: all real file/report/phone prerequisites have completed.
            assert reply_files_available is True, (
                "真实队友已写入、发布并经完成报告交回附件，Web副本可下载；"
                "另一私聊主运行已结束，本次7号私聊主运行也正常结束，"
                "但其实际工具目录缺少固定来源的channel_reply_files入口"
            )

            assert rounds["files"] == 2
            stored_tool = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "reply-child-file"
            )
            assert stored_tool["content_provenance"] == "accepted"
            assert json.loads(stored_tool["content"])["id"] == queued["id"]
            delivered = await terminal(manager, queued["id"])
            assert delivered["state"] == "sent" and delivered["name"] == "onebot"
            owner_input = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("principal") == "owner" and item["content"] == request_files
            )
            origin = owner_input["reply_origin"]
            assert origin["kind"] == "channel" and origin["input_id"] == owner_input["id"]
            assert origin["route"]["kind"] == "private"
            assert origin["route"]["target"] == origin["route"]["sender_id"] == "7"
            assert origin["route"]["reply_to"] == "703"
            assert delivered["route"] == origin["route"]
            routing = delivered["payload"]["attention_routing"]
            assert routing["classification"] == "bound"
            assert routing["source_input_id"] == owner_input["id"]
            assert routing["destination"] == {"kind": "channel", "route": origin["route"]}
            assert delivered["payload"]["parts"] == [{"attachment_id": attachment_id}]
            files = [
                item
                for item in manager.deliveries()
                if any(part.get("attachment_id") for part in item["payload"]["parts"])
            ]
            assert [item["id"] for item in files] == [queued["id"]]
            assert len(file_requests) == 1
            actual_file = file_requests[0]
            assert str(actual_file["user_id"]) == "7" and actual_file["name"] == source.name
            assert actual_file["file"].startswith("base64://")
            sent_bytes = base64.b64decode(
                actual_file["file"].removeprefix("base64://"), validate=True
            )
            assert sent_bytes == expected_bytes
            assert hashlib.sha256(sent_bytes).hexdigest() == expected_digest
            assert not any(str(item["user_id"]) == "8" for item in file_requests)
            reply = completed(runtime, queued_reply)[0]
            assert reply["reply_origin"] == origin
            await processed_reply(runtime, manager, reply["id"])
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(item["content"] == queued_reply for item in history.json()["messages"])
            downloaded_again = await web.get(f"/api/uploads/{attachment_id}/download")
            assert downloaded_again.status_code == 200 and downloaded_again.content == sent_bytes
            assert len(file_requests) == 1
    finally:
        release_worker.set()
        await runtime.close()


async def test_unknown_file_reply_survives_reopen_and_exact_envelope_replay_without_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "unknown-reply.txt"
    content = "合成附件：发送结果未知时保留原记录，不自动重发。\nUNKNOWN-FILE-53\n"
    expected_bytes = content.encode("utf-8")
    expected_digest = hashlib.sha256(expected_bytes).hexdigest()
    assignment = "请让队友生成本地草稿附件，先不要发送文件。"
    acknowledgement = "已派队友生成草稿附件，暂不发送。"
    report_reply = "已收到队友实际发布的草稿引用。"
    request_files = "请把这份草稿文件发到本次私聊。"
    unknown_reply = "实际文件投递结果未知，已保留原记录；不会自动重发。"
    reopened_input = "重新打开后，只检查原投递状态并确认收到，不重发任何文件。"
    reopened_reply = "已核原文件投递仍为未知，继续保留且不重发。"
    stable_key = "one-uncertain-file-53"
    files: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "platform.invalid"
        body = json.loads(request.content)
        if request.url.path == "/upload_private_file":
            files.append(body)
            assert len(files) == 1 and str(body["user_id"]) == "7"
            data = base64.b64decode(body["file"].removeprefix("base64://"), validate=True)
            assert data == expected_bytes and hashlib.sha256(data).hexdigest() == expected_digest
            raise httpx.ReadTimeout("one explicit unconfirmed file upload", request=request)
        assert request.url.path == "/send_private_msg"
        texts.append(body)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(texts)}}
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
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
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
                    "owner_ids": ["7", "8"],
                }
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    instance_id = runtime.store.instance_id
    phase = "assignment"
    active_runtime = runtime
    rounds = {"assignment": 0, "report": 0, "files": 0, "reopened": 0}
    runs: dict[str, str] = {}
    worker_id = ""
    worker_rounds = 0
    attachment: dict[str, Any] = {}
    observed_attachment: dict[str, Any] = {}
    queue_results: list[dict[str, Any]] = []
    unknown: dict[str, Any] = {}
    release_worker = asyncio.Event()
    worker_entered = asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_id, observed_attachment, queue_results
            assert len(messages) <= 32 and phase != "reopened"
            rounds[phase] += 1
            current = rounds[phase]
            assert runs.setdefault(phase, runtime.run_ids[main_id]) == runtime.run_ids[main_id]
            if phase == "assignment":
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-unknown-worker",
                                "spawn_agent",
                                task_name="未知投递草稿队友",
                                prompt="真实写入合成草稿，用file_publish保留副本，报告实际附件元数据，不发送文件。",
                            )
                        ]
                    )
                else:
                    assert current == 2
                    worker_id = tool_result(messages, "spawn-unknown-worker")["id"]
                    turn = ModelTurn(content=acknowledgement)
            elif phase == "report":
                assert current == 1
                incoming = next(
                    message
                    for message in reversed(messages)
                    if message.role == "user"
                    and message.content.startswith("队友结果，仅为待核实资料")
                )
                report = json.loads(incoming.content.split("\n", 1)[1])
                assert report["id"] == worker_id and report["status"] == "completed"
                observed_attachment = json.loads(report["result"])["attachment"]
                assert observed_attachment == attachment
                turn = ModelTurn(content=report_reply)
            else:
                assert phase == "files" and observed_attachment
                if current == 1:
                    assert any(message.content == request_files for message in messages)
                    assert any(tool["function"]["name"] == "channel_reply_files" for tool in tools)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                identifier,
                                "channel_reply_files",
                                attachment_ids=[observed_attachment["id"]],
                                idempotency_key=stable_key,
                            )
                            for identifier in ("send-unknown-file", "same-request-same-key")
                        ]
                    )
                elif current == 2:
                    queue_results = [
                        tool_result(messages, identifier)
                        for identifier in ("send-unknown-file", "same-request-same-key")
                    ]
                    assert all("error" not in result for result in queue_results)
                    assert queue_results[0]["id"] == queue_results[1]["id"]
                    actual = await terminal(
                        app.state.channel_application.manager, queue_results[0]["id"]
                    )
                    assert actual["state"] == "unknown"
                    turn = ModelTurn(tool_calls=[call("read-unknown-status", "channel_status")])
                else:
                    assert current == 3
                    status = tool_result(messages, "read-unknown-status")
                    saved = next(
                        item
                        for item in status["deliveries"]
                        if item["id"] == queue_results[0]["id"]
                    )
                    assert saved["state"] == "unknown" and saved["next_part"] == 0
                    assert saved["receipts"] == []
                    turn = ModelTurn(content=unknown_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds, attachment
            assert len(messages) <= 32
            worker_rounds += 1
            runs["worker"] = runtime.run_ids[runtime.team.get("未知投递草稿队友")["session_id"]]
            if worker_rounds == 1:
                worker_entered.set()
                await asyncio.wait_for(release_worker.wait(), 3)
                turn = ModelTurn(
                    tool_calls=[
                        call("write-unknown-file", "fs_write", path=str(source), content=content)
                    ]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "write-unknown-file")["path"] == str(source)
                turn = ModelTurn(
                    tool_calls=[call("publish-unknown-file", "file_publish", path=str(source))]
                )
            else:
                assert worker_rounds == 3
                attachment = tool_result(messages, "publish-unknown-file")
                assert attachment["sha256"] == expected_digest and attachment["size"] == len(
                    expected_bytes
                )
                turn = ModelTurn(content=json.dumps({"attachment": attachment}, ensure_ascii=False))
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)

    def diagnostic() -> str:
        states: dict[str, Any] = {}
        for role in (phase, "worker"):
            if identifier := runs.get(role):
                run = active_runtime.store.get("run", identifier)
                if run is not None:
                    states[role] = {
                        "status": run["status"],
                        "error_type": (run.get("failure_diagnostic") or {}).get("exception_type"),
                        "error_present": bool(run.get("error")),
                    }
        return json.dumps(
            {"runs": states, "rounds": [*rounds.values(), worker_rounds], "file_calls": len(files)},
            ensure_ascii=True,
        )[:1024]

    async def wait_reply(expected: str) -> None:
        try:
            await eventually(active_runtime, lambda: bool(completed(active_runtime, expected)))
        except TimeoutError:
            pytest.fail(diagnostic())
        assert await active_runtime.wait(runs[phase]) == expected

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
            await asyncio.wait_for(worker_entered.wait(), 3)
            phase = "report"
            release_worker.set()
            await wait_reply(report_reply)
            agent = runtime.team.get(worker_id)
            assert agent["status"] == "completed" and agent["parent_id"] == main_id
            old_report = runtime.store.get("team_report", agent["run_id"])
            assert old_report is not None and old_report["execution_status"] == "completed"
            assert old_report["status"] == "delivered"
            phase = "files"
            sent_request = await phone(web, request_files, 802)
            assert sent_request.status_code == 200
            await wait_reply(unknown_reply)
            manager = app.state.channel_application.manager
            unknown = await terminal(manager, queue_results[0]["id"])
            assert unknown["state"] == "unknown" and unknown["next_part"] == 0
            assert unknown["receipts"] == [] and unknown["attempts"] == 1
            assert len(files) == 1
            result_message = completed(runtime, unknown_reply)[0]
            await processed_reply(runtime, manager, result_message["id"])
            for delivery in manager.deliveries():
                assert (await terminal(manager, delivery["id"]))["state"] in {"sent", "unknown"}
            owner = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("principal") == "owner" and item["content"] == request_files
            )
            assert unknown["route"] == owner["reply_origin"]["route"]
            assert unknown["route"]["target"] == "7" and unknown["route"]["reply_to"] == "802"
            assert unknown["payload"]["attention_routing"]["source_input_id"] == owner["id"]
            tools = runtime.store.messages(main_id)
            for identifier in ("send-unknown-file", "same-request-same-key", "read-unknown-status"):
                actual = next(item for item in tools if item.get("tool_call_id") == identifier)
                assert actual["content_provenance"] == "accepted"
            assert rounds == {"assignment": 2, "report": 1, "files": 3, "reopened": 0}
            assert worker_rounds == 3
            old_messages = runtime.store.messages(main_id)
            old_runs = runtime.store.list("run")
            old_inbound = [
                tuple(row)
                for row in runtime.store.db.execute(
                    "SELECT id,name,receipt,fingerprint FROM channel_inbound ORDER BY id"
                )
            ]
            config_path, data_dir = runtime.config.path, runtime.data_dir
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        release_worker.set()
        await runtime.close()

    phase = "reopened"

    class ReopenedMain(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert len(messages) <= 32
            rounds["reopened"] += 1
            current = rounds["reopened"]
            assert (
                runs.setdefault(phase, active_runtime.run_ids[main_id])
                == active_runtime.run_ids[main_id]
            )
            if current == 1:
                assert any(message.content == reopened_input for message in messages)
                turn = ModelTurn(tool_calls=[call("status-after-reopen", "channel_status")])
            else:
                assert current == 2
                status = tool_result(messages, "status-after-reopen")
                saved = next(item for item in status["deliveries"] if item["id"] == unknown["id"])
                assert saved == unknown
                turn = ModelTurn(content=reopened_reply)
            yield StreamEvent("done", turn=turn)

    def reopened_factory(config: ModelConfig) -> FakeClient:
        assert config.model == "main"
        return ReopenedMain([], [])

    reopened = Runtime(ConfigFile(config_path), data_dir, client_factory=reopened_factory)  # type: ignore[arg-type]
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
            reopened_manager = reopened_app.state.channel_application.manager
            assert reopened_manager.delivery(unknown["id"]) == unknown
            assert reopened.store.get("team_report", old_report["id"]) == old_report
            assert (
                reopened.store.messages(main_id) == old_messages
                and reopened.store.list("run") == old_runs
            )
            replayed = await phone(web, request_files, 802)
            assert replayed.status_code == 200 and replayed.json() == sent_request.json()
            assert (
                reopened.store.messages(main_id) == old_messages
                and reopened.store.list("run") == old_runs
            )
            assert [
                tuple(row)
                for row in reopened.store.db.execute(
                    "SELECT id,name,receipt,fingerprint FROM channel_inbound ORDER BY id"
                )
            ] == old_inbound
            assert rounds["reopened"] == 0 and len(files) == 1
            assert (await phone(web, reopened_input, 803, sender=8)).status_code == 200
            await wait_reply(reopened_reply)
            reply = completed(reopened, reopened_reply)[0]
            assert reply["reply_origin"]["route"]["target"] == "8"
            await processed_reply(reopened, reopened_manager, reply["id"])
            matching = [
                item
                for item in reopened_manager.deliveries()
                if any(part.get("text") == reopened_reply for part in item["payload"]["parts"])
            ]
            assert len(matching) == 1
            assert (await terminal(reopened_manager, matching[0]["id"]))["state"] == "sent"
            assert reopened_manager.delivery(unknown["id"]) == unknown and len(files) == 1
            assert [
                item["id"]
                for item in reopened_manager.deliveries()
                if any(part.get("attachment_id") for part in item["payload"]["parts"])
            ] == [unknown["id"]]
            downloaded = await web.get(f"/api/uploads/{observed_attachment['id']}/download")
            assert downloaded.status_code == 200 and downloaded.content == expected_bytes
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert sum(item["content"] == unknown_reply for item in history.json()["messages"]) == 1
            assert any(item["content"] == reopened_reply for item in history.json()["messages"])
            assert rounds["reopened"] == 2 and len(files) == 1
    finally:
        await reopened.close()
