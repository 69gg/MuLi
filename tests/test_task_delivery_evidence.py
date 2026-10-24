"""Real publication prerequisites for explicit, traceable task delivery records."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_task_control_runtime import wait_agent
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli import runtime as runtime_module
from muli.auth import hash_password
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import PreparedRequest, Runtime
from muli.server import create_app


async def test_real_task_publication_keeps_exact_execution_evidence_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("交付证据旅程只使用本地 ASGI，不允许外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    store_instance = runtime.store.instance_id
    source = tmp_path / "delivery-draft.txt"
    content = "核查草稿：这是保存在本地的独立交付副本。"
    changed_content = "原件后来发生变化；不能因此替换已经发布的副本。"
    expected_bytes = content.encode("utf-8")
    expected_digest = hashlib.sha256(expected_bytes).hexdigest()
    initial_reply = "任务已登记，队友将准备本地草稿，不发送文件。"
    completion_reply = "已读取实际附件副本；尚未登记交付验收单。"
    worker_result = "已保留附件副本；工作区原件随后修改，没有发送文件。"
    task_id, worker_id, attachment_id = "", "", ""
    phase = "assignment"
    active_runtime = runtime
    main_rounds: dict[str, int] = {"assignment": 0, "completion": 0}
    main_runs: dict[str, str] = {}
    worker_rounds = 0
    worker_entered, release_worker = asyncio.Event(), asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id
            main_rounds[phase] += 1
            current = main_rounds[phase]
            run_id = active_runtime.run_ids[main_id]
            assert main_runs.setdefault(phase, run_id) == run_id
            if phase == "assignment":
                if current == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-task",
                                "commitment_save",
                                title="有独立副本的核查草稿",
                                brief={
                                    "goal": "整理核查草稿并留下可追溯的本地附件副本",
                                    "constraints": ["不发送文件", "不操作真实服务"],
                                },
                            )
                        ]
                    )
                elif current == 2:
                    task_id = tool_result(messages, "save-task")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-worker",
                                "spawn_agent",
                                task_name="交付草稿队友",
                                prompt="在临时工作区写草稿并保存附件副本，不发送文件。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert current == 3
                    worker_id = tool_result(messages, "spawn-worker")["id"]
                    turn = ModelTurn(content=initial_reply)
            else:
                assert phase == "completion" and attachment_id
                if current == 1:
                    assert any(worker_result in message.content for message in messages)
                    turn = ModelTurn(
                        tool_calls=[call("read-snapshot", "attachment_read", id=attachment_id)]
                    )
                else:
                    assert current == 2
                    read = tool_result(messages, "read-snapshot")
                    assert read == {"id": attachment_id, "content": content, "truncated": False}
                    turn = ModelTurn(content=completion_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal attachment_id, worker_rounds
            worker_rounds += 1
            if worker_rounds == 1:
                worker_entered.set()
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[call("write-source", "fs_write", path=str(source), content=content)]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "write-source")["path"] == str(source)
                turn = ModelTurn(
                    tool_calls=[call("publish-snapshot", "file_publish", path=str(source))]
                )
            elif worker_rounds == 3:
                published = tool_result(messages, "publish-snapshot")
                attachment_id = published["id"]
                assert published["sha256"] == expected_digest
                assert published["size"] == len(expected_bytes)
                # A second real tool changes the original, never the saved attachment.
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "change-original",
                            "fs_write",
                            path=str(source),
                            content=changed_content,
                            overwrite=True,
                        )
                    ]
                )
            else:
                assert worker_rounds == 4
                assert tool_result(messages, "change-original")["path"] == str(source)
                turn = ModelTurn(content=worker_result)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    finalize = runtime_module.completed_publication
    locator_checks = 0

    def check_exact_locators(
        host: Runtime,
        request: PreparedRequest,
        session_id: str,
        run_id: str,
        assistant: dict[str, Any],
        invocation: ToolCall,
        result: Any,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        nonlocal locator_checks
        proof = finalize(host, request, session_id, run_id, assistant, invocation, result, message)
        if invocation.name == "file_publish":
            assert proof is not None
            # Each local negative changes one field of the actual invocation/record.
            earlier = next(
                item
                for item in host.store.messages(session_id)
                if any(tool["id"] == "write-source" for tool in item.get("tool_calls", []))
            )
            assert (
                finalize(
                    host,
                    request,
                    session_id,
                    run_id,
                    {**assistant, "id": earlier["id"]},
                    invocation,
                    result,
                    message,
                )
                is None
            )
            assert (
                finalize(
                    host,
                    request,
                    session_id,
                    run_id,
                    assistant,
                    replace(
                        invocation, arguments=json.dumps({"path": str(tmp_path / "other.txt")})
                    ),
                    result,
                    message,
                )
                is None
            )
            assert (
                finalize(
                    host,
                    request,
                    session_id,
                    run_id,
                    assistant,
                    replace(invocation, name="fs_write"),
                    result,
                    message,
                )
                is None
            )
            assert (
                finalize(
                    host,
                    request,
                    session_id,
                    run_id,
                    assistant,
                    invocation,
                    result,
                    {**message, "content": "{}"},
                )
                is None
            )
            locator_checks += 1
        return proof

    monkeypatch.setattr(runtime_module, "completed_publication", check_exact_locators)
    published_message: dict[str, Any] = {}
    publication_call: dict[str, Any] = {}
    completed_event: dict[str, Any] = {}
    finished: dict[str, Any] = {}
    report: dict[str, Any] = {}
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assigned = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "请整理草稿，保留独立附件副本及实际核查依据，不发送文件。"},
            )
            assert assigned.status_code == 200
            assert await runtime.wait(assigned.json()["run_id"]) == initial_reply
            await asyncio.wait_for(worker_entered.wait(), 3)
            phase = "completion"
            release_worker.set()
            finished = await wait_agent(runtime, worker_id, "completed")
            await eventually(runtime, lambda: bool(completed(runtime, completion_reply)))
            assert await runtime.wait(main_runs["completion"]) == completion_reply
            assert main_rounds == {"assignment": 3, "completion": 2} and worker_rounds == 4
            assert finished["parent_id"] == main_id and finished["commitment_id"] == task_id
            assert finished["generation"] == finished["completion_revision"] == 1
            assert runtime.store.get("run", finished["run_id"])["status"] == "completed"
            report = runtime.store.get("team_report", finished["run_id"])
            assert report is not None
            assert report["execution_status"] == "completed" and report["status"] == "delivered"
            assert report["agent_id"] == worker_id and report["parent_id"] == main_id
            assert report["commitment_id"] == task_id
            finished_events = [
                event["data"]
                for event in runtime.store.events(session_id=worker_id)
                if event["type"] == "team.finished"
            ]
            assert len(finished_events) == 1
            completed_event = finished_events[0]
            assert completed_event["run_id"] == report["id"] == finished["run_id"]
            assert completed_event["generation"] == completed_event["completion_revision"] == 1
            worker_messages = runtime.store.messages(worker_id)
            results = [
                message
                for message in worker_messages
                if message.get("tool_call_id") == "publish-snapshot"
            ]
            calls = [
                message
                for message in worker_messages
                if any(call["id"] == "publish-snapshot" for call in message.get("tool_calls", []))
            ]
            assert len(results) == len(calls) == 1
            published_message, publication_call = results[0], calls[0]
            assert published_message["role"] == "tool"
            assert published_message["content_provenance"] == "accepted"
            actual_result = json.loads(published_message["content"])
            assert actual_result["id"] == attachment_id
            assert actual_result["sha256"] == expected_digest
            assert publication_call["tool_calls"][0] == {
                "id": "publish-snapshot",
                "name": "file_publish",
                "arguments": json.dumps({"path": str(source)}, ensure_ascii=False),
            }
            assert source.read_text(encoding="utf-8") == changed_content
            assert app.state.attachments.path(attachment_id).read_bytes() == expected_bytes
            assert app.state.local_services.commitments.get(task_id)["status"] == "open"
            assert app.state.channel_application.manager.deliveries() == []
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    reopened = Runtime(
        ConfigFile(runtime.config.path),
        runtime.data_dir,
        client_factory=factory,  # type: ignore[arg-type]
    )
    active_runtime = reopened
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert reopened.store.instance_id == store_instance
            assert reopened.main_session()["id"] == main_id
            assert reopened.store.get("team_report", report["id"]) == report
            downloaded = await web.get(f"/api/uploads/{attachment_id}/download")
            assert downloaded.status_code == 200 and downloaded.content == expected_bytes
            assert reopened.store.get("message", published_message["id"]) == published_message
            assert reopened_app.state.channel_application.manager.deliveries() == []
            # All provenance comes from the actual execution, not a model-supplied locator.
            proof = reopened.store.get("task_file_publication", attachment_id)
            assert proof is not None, "真实任务已发布附件并完成，但缺少准确执行与工具结果的宿主关联"
            assert proof["version"] == 1 and proof["state"] == "recorded"
            assert proof["commitment_id"] == task_id and proof["task_revision"] == 1
            assert proof["epoch"] == 0
            assert proof["agent_id"] == worker_id and proof["run_id"] == report["id"]
            assert proof["generation"] == completed_event["generation"]
            assert proof["completion_revision"] == completed_event["completion_revision"]
            assert proof["attachment_id"] == attachment_id
            assert proof["sha256"] == expected_digest and proof["size"] == len(expected_bytes)
            assert proof["source_path"] == str(source)
            assert proof["call_id"] == "publish-snapshot"
            assert proof["assistant_message_id"] == publication_call["id"]
            assert proof["tool_message_id"] == published_message["id"]
            assert locator_checks == 1
            assert main_rounds == {"assignment": 3, "completion": 2} and worker_rounds == 4
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "verification_control", ["stable", "owner_pause", "changed_copy", "receipt_commit"]
)
async def test_completed_delivery_is_explicitly_recorded_and_inspected_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verification_control: str
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("验收单旅程只使用临时资料库与本地 ASGI")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    password = "temporary-delivery-test"
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "server": {"password_hash": hash_password(password)},
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    instance_id = runtime.store.instance_id
    source = tmp_path / "acceptance-draft.txt"
    content = "核查结果：草稿已保留独立副本；外部数据的正确性尚未验。"
    raw = content.encode("utf-8")
    changed_raw = bytes([raw[0] ^ 1]) + raw[1:]
    assert len(changed_raw) == len(raw) and changed_raw.decode("utf-8") != content
    digest = hashlib.sha256(raw).hexdigest()
    brief = {"goal": "留下可追溯的核查草稿", "constraints": ["不得发送文件", "核对外部数据"]}
    judgments = {
        "goal": {"status": "satisfied", "reason": "已读取草稿副本；这是模型判断。"},
        "constraints": [
            {"index": 0, "status": "satisfied", "reason": "本轮仅创建本地附件。"},
            {"index": 1, "status": "unverified", "reason": "未连接外部数据，不能宣称通过。"},
        ],
    }
    initial_reply = "队友将核查草稿，完成后显式登记依据。"
    final_reply = "核查调用已结束，按实际登记结果说明。"
    worker_reply = "草稿副本已发布；外部数据尚未核查。"
    task_id = worker_id = attachment_id = delivery_id = ""
    assignment_done = False
    main_rounds = worker_rounds = 0
    main_completion_run = ""
    registration: dict[str, Any] = {}
    tool_body: dict[str, Any] = {}
    saved_arguments: dict[str, Any] = {}
    replay_case = ""
    replay_rounds: dict[str, int] = {}
    replay_results: dict[str, dict[str, Any]] = {}
    record_tool_available = False
    verification_reads = commit_failures = 0
    attempted_delivery_id = ""
    worker_entered, release_worker = asyncio.Event(), asyncio.Event()

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id, main_rounds, main_completion_run
            nonlocal registration, tool_body, delivery_id, record_tool_available, saved_arguments
            if replay_case:
                replay_rounds[replay_case] = replay_rounds.get(replay_case, 0) + 1
                if replay_rounds[replay_case] == 1:
                    arguments = json.loads(json.dumps(saved_arguments, ensure_ascii=False))
                    if replay_case == "conflict":
                        arguments["judgments"]["goal"]["reason"] = "同一键不能替换原判断理由。"
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "repeat-delivery",
                                "task_delivery_record",
                                json.dumps(arguments, ensure_ascii=False),
                            )
                        ]
                    )
                else:
                    assert replay_rounds[replay_case] == 2
                    replay_results[replay_case] = tool_result(messages, "repeat-delivery")
                    turn = ModelTurn(content=f"验收重放 {replay_case} 已按实际结果结束。")
                yield StreamEvent("done", turn=turn)
                return
            main_rounds += 1
            if not assignment_done:
                if main_rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save-delivery-task",
                                "commitment_save",
                                title="显式交付验收",
                                brief=brief,
                            )
                        ]
                    )
                elif main_rounds == 2:
                    task_id = tool_result(messages, "save-delivery-task")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn-delivery-worker",
                                "spawn_agent",
                                task_name="验收草稿队友",
                                prompt="写草稿并发布本地副本，不发文件；外部数据仍未验。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert main_rounds == 3
                    worker_id = tool_result(messages, "spawn-delivery-worker")["id"]
                    turn = ModelTurn(content=initial_reply)
            else:
                main_completion_run = runtime.run_ids[main_id]
                if main_rounds == 4:
                    assert any(worker_reply in message.content for message in messages)
                    turn = ModelTurn(
                        tool_calls=[call("read-delivery", "attachment_read", id=attachment_id)]
                    )
                elif main_rounds == 5:
                    assert tool_result(messages, "read-delivery") == {
                        "id": attachment_id,
                        "content": content,
                        "truncated": False,
                    }
                    agent = runtime.team.get(worker_id)
                    report = runtime.store.get("team_report", agent["run_id"])
                    assert report is not None and report["execution_status"] == "completed"
                    record_tool_available = any(
                        tool["function"]["name"] == "task_delivery_record" for tool in tools
                    )
                    saved_arguments = {
                        "identifier": task_id,
                        "observed_revision": 1,
                        "observed_epoch": 0,
                        "report_id": report["id"],
                        "attachment_id": attachment_id,
                        "idempotency_key": "first-delivery",
                        "judgments": judgments,
                    }
                    turn = ModelTurn(
                        tool_calls=[
                            ToolCall(
                                "record-delivery",
                                "task_delivery_record",
                                json.dumps(
                                    saved_arguments,
                                    ensure_ascii=False,
                                ),
                            )
                        ]
                    )
                elif main_rounds == 6:
                    registration = tool_result(messages, "record-delivery")
                    if registration.get("error"):
                        # Let the real main run end; the feature assertion is outside the model.
                        turn = ModelTurn(content=final_reply)
                    else:
                        delivery_id = registration["id"]
                        turn = ModelTurn(
                            tool_calls=[
                                ToolCall(
                                    "inspect-delivery",
                                    "commitment_inspect",
                                    json.dumps(
                                        {
                                            "identifier": task_id,
                                            "section": "deliveries",
                                            "record_id": delivery_id,
                                        },
                                        ensure_ascii=False,
                                    ),
                                )
                            ]
                        )
                else:
                    assert main_rounds == 7
                    page = tool_result(messages, "inspect-delivery")
                    assert page["has_more"] is False
                    tool_body = json.loads(page["content"])
                    turn = ModelTurn(content=final_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds, attachment_id
            worker_rounds += 1
            assert all(tool["function"]["name"] != "task_delivery_record" for tool in tools)
            if worker_rounds == 1:
                worker_entered.set()
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[
                        call("write-delivery", "fs_write", path=str(source), content=content)
                    ]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "write-delivery")["path"] == str(source)
                turn = ModelTurn(
                    tool_calls=[call("publish-delivery", "file_publish", path=str(source))]
                )
            else:
                assert worker_rounds == 3
                published = tool_result(messages, "publish-delivery")
                attachment_id = published["id"]
                assert published["size"] == len(raw) and published["sha256"] == digest
                turn = ModelTurn(content=worker_reply)
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    read_bytes = app.state.local_services.task_delivery.read_bytes
    put = runtime.store._put

    async def verify_then_control(identifier: str, maximum: int) -> tuple[dict[str, Any], bytes]:
        nonlocal verification_reads
        if verification_control == "changed_copy":
            path = app.state.attachments.path(identifier)
            assert path.read_bytes() == raw
            assert runtime.store.get("task_file_publication", identifier)["state"] == "recorded"
            path.write_bytes(changed_raw)
        item, data = await read_bytes(identifier, maximum)
        verification_reads += 1
        if verification_control == "owner_pause":
            paused = await web.post(
                f"/api/commitments/{task_id}/control",
                json={"action": "pause", "expected_epoch": 0, "request_id": "verification-pause"},
            )
            assert paused.status_code == 200, paused.text
        return item, data

    def fail_receipt_commit(kind: str, item: dict[str, Any]) -> None:
        nonlocal commit_failures, attempted_delivery_id
        if kind == "task_delivery_receipt" and commit_failures == 0:
            attempted_delivery_id = item["delivery_id"]
            assert runtime.store.db.in_transaction
            assert runtime.store.get("task_delivery", attempted_delivery_id) is not None
            commit_failures += 1
            raise RuntimeError("受控中断验收单与去重回执的原子提交")
        put(kind, item)

    monkeypatch.setattr(app.state.local_services.task_delivery, "read_bytes", verify_then_control)
    if verification_control == "receipt_commit":
        monkeypatch.setattr(runtime.store, "_put", fail_receipt_commit)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            login = await web.post("/api/auth/login", json={"password": password})
            assert login.status_code == 200
            started = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "整理核查草稿，逐条标明依据与未验项，不发文件。"},
            )
            assert started.status_code == 200
            assert await runtime.wait(started.json()["run_id"]) == initial_reply
            await asyncio.wait_for(worker_entered.wait(), 3)
            assignment_done = True
            release_worker.set()
            finished = await wait_agent(runtime, worker_id, "completed")
            try:
                await eventually(runtime, lambda: bool(completed(runtime, final_reply)))
            except TimeoutError as exc:
                run = (
                    runtime.store.bounded_object("run", main_completion_run, max_bytes=32768)
                    if main_completion_run
                    else None
                ) or {}
                failure = run.get("failure_diagnostic")
                if not isinstance(failure, dict):
                    failure = {}
                row = runtime.store.db.execute(
                    "SELECT o.id,json_extract(o.data,'$.tool_call_id'),"
                    "substr(CAST(json_extract(o.data,'$.content') AS BLOB),1,1000) "
                    "FROM message_order m JOIN objects o ON o.kind='message' AND o.id=m.message_id "
                    "WHERE m.session_id=? AND json_extract(o.data,'$.role')='tool' "
                    "ORDER BY m.seq DESC LIMIT 1",
                    (main_id,),
                ).fetchone()
                diagnostic = {
                    "main_rounds": main_rounds,
                    "worker_rounds": worker_rounds,
                    "run_id": main_completion_run,
                    "status": run.get("status"),
                    "error": str(run.get("error", ""))[:300],
                    "exception_type": failure.get("exception_type"),
                    "frames": failure.get("frames", [])[:4],
                    "last_tool_result": (
                        {
                            "id": row[0],
                            "call_id": row[1],
                            "content": bytes(row[2] or b"").decode("utf-8", errors="replace"),
                        }
                        if row is not None
                        else None
                    ),
                }
                raise AssertionError(json.dumps(diagnostic, ensure_ascii=False)) from exc
            assert await runtime.wait(main_completion_run) == final_reply
            assert finished["parent_id"] == main_id and finished["commitment_id"] == task_id
            assert finished["generation"] == finished["completion_revision"] == 1
            report = runtime.store.get("team_report", finished["run_id"])
            assert report is not None and report["status"] == "delivered"
            assert runtime.store.get("run", finished["run_id"])["status"] == "completed"
            proof = runtime.store.get("task_file_publication", attachment_id)
            assert proof is not None and proof["state"] == "recorded"
            assert proof["run_id"] == report["id"] and proof["agent_id"] == worker_id
            assert app.state.attachments.path(attachment_id).read_bytes() == (
                changed_raw if verification_control == "changed_copy" else raw
            )
            metadata = app.state.attachments.get(attachment_id)
            assert metadata["size"] == len(raw) and metadata["sha256"] == digest
            assert source.read_text(encoding="utf-8") == content
            assert app.state.local_services.commitments.get(task_id)["status"] == "open"
            assert app.state.channel_application.manager.deliveries() == []
            assert verification_reads == 1
            if verification_control != "stable":
                if verification_control == "receipt_commit":
                    assert registration == {"error": "工具执行失败：RuntimeError"}
                    assert commit_failures == 1 and attempted_delivery_id
                    assert runtime.store.get("task_delivery", attempted_delivery_id) is None
                else:
                    assert registration.get("code") == "evidence_changed", registration
                    assert commit_failures == 0
                task = app.state.local_services.commitments.get(task_id)
                if verification_control == "owner_pause":
                    assert task["revision"] == 2 and task["control"]["epoch"] == 1
                else:
                    assert task["revision"] == 1
                    assert app.state.local_services.task_control._state(task)["epoch"] == 0
                assert (
                    runtime.store.db.execute(
                        "SELECT COUNT(*) FROM objects WHERE kind IN ('task_delivery','task_delivery_receipt')"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    runtime.store.db.execute(
                        "SELECT COUNT(*) FROM events WHERE type='task.delivery_recorded'"
                    ).fetchone()[0]
                    == 0
                )
                assert main_rounds == 6 and worker_rounds == 3
                await asyncio.wait_for(app.state.close(), 3)
                return
            # The first target red must be this real feature boundary, after every prerequisite.
            assert registration.get("id"), (
                "真实任务完成、附件完整读取和宿主发布证明均已成立，但主助手无法显式登记验收单："
                f"tool_available={record_tool_available}, result={registration}"
            )
            assert delivery_id == registration["id"] == tool_body["id"]
            assert tool_body["commitment_id"] == task_id
            assert tool_body["task_revision"] == 1 and tool_body["epoch"] == 0
            assert tool_body["agent_id"] == worker_id and tool_body["run_id"] == report["id"]
            assert tool_body["brief"] == app.state.local_services.commitments.get(task_id)["brief"]
            assert tool_body["model_judgments"] == judgments
            assert tool_body["host_facts"]["attachment_id"] == attachment_id
            assert tool_body["host_facts"]["size"] == len(raw)
            assert tool_body["host_facts"]["sha256"] == digest
            assert tool_body["host_facts"]["verification"] == "at_record_time"
            assert tool_body["recorded_by"] == {
                "session_id": main_id,
                "run_id": main_completion_run,
            }
            assert main_rounds == 7 and worker_rounds == 3
            assert app.state.channel_application.manager.deliveries() == []
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    reopened = Runtime(
        ConfigFile(runtime.config.path),
        runtime.data_dir,
        client_factory=factory,  # type: ignore[arg-type]
    )
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert reopened.store.instance_id == instance_id
            assert reopened.main_session()["id"] == main_id
            url = f"/api/commitments/{task_id}/inspection"
            unauthorized = await web.get(url, params={"section": "deliveries"})
            assert unauthorized.status_code == 401
            login = await web.post("/api/auth/login", json={"password": password})
            assert login.status_code == 200
            directory = await web.get(url, params={"section": "deliveries"})
            assert directory.status_code == 200, directory.text
            items = directory.json()["items"]
            assert len(items) == 1 and items[0]["id"] == delivery_id and items[0]["current"] is True
            body = await web.get(url, params={"section": "deliveries", "record_id": delivery_id})
            assert body.status_code == 200, body.text
            assert body.json()["has_more"] is False
            assert json.loads(body.json()["content"]) == tool_body
            assert reopened_app.state.local_services.commitments.get(task_id)["status"] == "open"
            assert reopened_app.state.channel_application.manager.deliveries() == []
            assert main_rounds == 7 and worker_rounds == 3

            async def no_repeat_read(identifier: str, maximum: int) -> NoReturn:
                raise AssertionError("准确重复验收只读原单，不得重新读取文件")

            monkeypatch.setattr(
                reopened_app.state.local_services.task_delivery, "read_bytes", no_repeat_read
            )
            for case in ("exact", "conflict", "historical"):
                replay_case = case
                if case == "historical":
                    paused = await web.post(
                        f"/api/commitments/{task_id}/control",
                        json={
                            "action": "pause",
                            "expected_epoch": 0,
                            "request_id": "historical-delivery",
                        },
                    )
                    assert paused.status_code == 200, paused.text
                    stale_page = await web.get(
                        url,
                        params={"section": "deliveries", "revision": directory.json()["revision"]},
                    )
                    assert stale_page.status_code == 409
                replayed = await web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={"content": f"核对验收去重结果 {case}，不重做任务、不发文件。"},
                )
                assert replayed.status_code == 200, replayed.text
                assert (
                    await reopened.wait(replayed.json()["run_id"])
                    == f"验收重放 {case} 已按实际结果结束。"
                )
                if case == "conflict":
                    assert replay_results[case].get("code") == "delivery_key_conflict", (
                        replay_results[case]
                    )
                else:
                    assert replay_results[case] == tool_body
                assert reopened.store.get("task_delivery", delivery_id) == tool_body
                assert (
                    reopened.store.db.execute(
                        "SELECT COUNT(*) FROM objects WHERE kind='task_delivery'"
                    ).fetchone()[0]
                    == 1
                )
                assert (
                    reopened.store.db.execute(
                        "SELECT COUNT(*) FROM objects WHERE kind='task_delivery_receipt'"
                    ).fetchone()[0]
                    == 1
                )
                assert (
                    reopened.store.db.execute(
                        "SELECT COUNT(*) FROM events WHERE type='task.delivery_recorded'"
                    ).fetchone()[0]
                    == 1
                )
            directory = await web.get(url, params={"section": "deliveries"})
            assert directory.status_code == 200, directory.text
            items = directory.json()["items"]
            assert (
                len(items) == 1 and items[0]["id"] == delivery_id and items[0]["current"] is False
            )
            historical = await web.get(
                url, params={"section": "deliveries", "record_id": delivery_id}
            )
            assert historical.status_code == 200, historical.text
            assert historical.json()["current"] is False
            assert json.loads(historical.json()["content"]) == tool_body
            task = reopened_app.state.local_services.commitments.get(task_id)
            assert task["status"] == "open" and task["control"]["epoch"] == 1
            assert verification_reads == 1 and replay_rounds == {
                "exact": 2,
                "conflict": 2,
                "historical": 2,
            }
            assert main_rounds == 7 and worker_rounds == 3
            assert reopened_app.state.channel_application.manager.deliveries() == []
            await asyncio.wait_for(reopened_app.state.close(), 3)
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "fault", ["pause_during_copy", "pause_after_copy", "cancel", "unknown", "excerpt", "commit"]
)
async def test_publication_prefix_cannot_finalize_without_a_complete_current_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("发布证明负控不允许外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    source = tmp_path / "prefix.txt"
    content = "真实工具先写入，再实际复制；失败前缀不能冒充完整交付。"
    task_id, worker_id = "", ""
    assignment_done = False
    main_rounds = worker_rounds = commit_failures = 0
    worker_entered, release_worker = asyncio.Event(), asyncio.Event()
    attachment: dict[str, Any] = {}
    initial_reply = "已交给队友，只保留本地副本。"

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal task_id, worker_id, main_rounds
            if assignment_done:
                turn = ModelTurn(content="已看到实际执行状态；没有登记验收单。")
            else:
                main_rounds += 1
                if main_rounds == 1:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "save",
                                "commitment_save",
                                title="发布失败前缀",
                                brief={"goal": "保留可核查副本", "constraints": ["不发送文件"]},
                            )
                        ]
                    )
                elif main_rounds == 2:
                    task_id = tool_result(messages, "save")["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "spawn",
                                "spawn_agent",
                                task_name="副本队友",
                                prompt="写入并发布本地副本。",
                                commitment_id=task_id,
                            )
                        ]
                    )
                else:
                    assert main_rounds == 3
                    worker_id = tool_result(messages, "spawn")["id"]
                    turn = ModelTurn(content=initial_reply)
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_rounds
            worker_rounds += 1
            if worker_rounds == 1:
                worker_entered.set()
                await release_worker.wait()
                turn = ModelTurn(
                    tool_calls=[call("write", "fs_write", path=str(source), content=content)]
                )
            elif worker_rounds == 2:
                assert tool_result(messages, "write")["path"] == str(source)
                turn = ModelTurn(tool_calls=[call("publish", "file_publish", path=str(source))])
            else:
                assert worker_rounds == 3 and fault in {"unknown", "excerpt"}
                result = tool_result(messages, "publish")
                if fault == "unknown":
                    assert result["status"] == "unknown"
                else:
                    assert result["truncated"] is True
                turn = ModelTurn(content="副本可能存在，但未取得完整成功依据。")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        if config.model == "worker":
            return Worker([], [])
        return Main([], []) if config.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    emit = runtime.bus.emit
    import_file = app.state.attachments.import_file
    put = runtime.store._put

    async def pause() -> None:
        response = await web.post(
            f"/api/commitments/{task_id}/control",
            json={"action": "pause", "expected_epoch": 0, "request_id": "copy-pause"},
        )
        assert response.status_code == 200

    async def copy_then_pause(path: Path, *, source: str = "local") -> dict[str, Any]:
        item = await import_file(path, source=source)
        await pause()
        return item

    async def observe_publication(
        event_type: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        event = await emit(event_type, data, session_id)
        if event_type == "attachment.published":
            attachment.update(data)
            if fault == "pause_after_copy":
                await pause()
            elif fault == "cancel":
                raise asyncio.CancelledError
            elif fault == "unknown":
                data["status"] = "unknown"
            elif fault == "excerpt":
                data["diagnostic"] = "x" * (runtime.config.settings.limits.max_output_chars + 1)
        return event

    def fail_record_commit(kind: str, item: dict[str, Any]) -> None:
        nonlocal commit_failures
        if kind == "task_file_publication" and item.get("state") == "recorded":
            commit_failures += 1
            raise RuntimeError("受控中断证明与实际结果的共同提交")
        put(kind, item)

    monkeypatch.setattr(runtime.bus, "emit", observe_publication)
    if fault == "pause_during_copy":
        monkeypatch.setattr(app.state.attachments, "import_file", copy_then_pause)
    if fault == "commit":
        monkeypatch.setattr(runtime.store, "_put", fail_record_commit)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assigned = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "写入草稿并保留副本，不发送。"},
            )
            assert assigned.status_code == 200
            assert await runtime.wait(assigned.json()["run_id"]) == initial_reply
            await asyncio.wait_for(worker_entered.wait(), 3)
            assignment_done = True
            release_worker.set()
            expected_status = (
                "paused"
                if fault.startswith("pause_")
                else "cancelled"
                if fault == "cancel"
                else "failed"
                if fault == "commit"
                else "completed"
            )
            finished = await wait_agent(runtime, worker_id, expected_status)
            assert attachment and app.state.attachments.path(
                attachment["id"]
            ).read_bytes() == content.encode("utf-8")
            proof = runtime.store.get("task_file_publication", attachment["id"])
            if fault == "pause_during_copy":
                assert proof is None
            else:
                assert proof is not None and proof["state"] == "pending"
                assert not {"assistant_message_id", "call_id", "tool_message_id"} & proof.keys()
            results = [
                item
                for item in runtime.store.messages(worker_id)
                if item.get("tool_call_id") == "publish"
            ]
            assert len(results) == 1
            result = results[0]
            if fault in {"cancel", "commit"}:
                assert json.loads(result["content"])["status"] == "unknown"
                assert result.get("content_provenance") != "accepted"
                assert result["interrupted"] is True
            elif fault == "excerpt":
                assert result["content_provenance"] == "runtime_excerpt"
            else:
                assert result["content_provenance"] == "accepted"
            if fault.startswith("pause_"):
                current = app.state.local_services.commitments.get(task_id)
                assert current["control"]["epoch"] == 1 and current["revision"] == 2
            if fault == "commit":
                assert commit_failures == 1
                assert runtime.store.get("run", finished["run_id"])["status"] == "failed"
            assert app.state.channel_application.manager.deliveries() == []
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()
