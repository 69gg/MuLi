"""项目收件的真实宿主旅程；模型仅编排调用，不证明自然影响判断。"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from test_attention import attention_inputs, eventually
from test_channels import terminal
from test_file_inbox_runtime_quality import capture_schemas
from test_mobile_task_control_followup import (
    approve_reply_send,
    delivery_text,
    phone,
    processed_reply,
)
from test_report_origin import route
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, tool_result

from muli.attachments import Attachments
from muli.attention import AttentionInbox
from muli.channels import ChannelManager
from muli.compaction import Compactor
from muli.config import CompactionSettings
from muli.conversation import Conversation
from muli.file_inbox import FileInbox
from muli.local_services import LocalServices
from muli.permissions import Mode
from muli.project_inbox import (
    WEB_PROJECT_ROUTING,
    ProjectInboxBatch,
    ProjectInboxProof,
    ProjectTaskSnapshot,
)
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import PreparedRequest, Runtime
from muli.server import create_app
from muli.store import Store, new_id

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux 原生收件旅程")


@dataclass(frozen=True, slots=True)
class ProjectJourney:
    runtime: Runtime
    web: httpx.AsyncClient
    local: LocalServices
    attachments: Attachments
    source: FileInbox
    attention: AttentionInbox
    manager: ChannelManager
    directory: Path
    requests: list[httpx.Request]

    async def bind(self, project_id: str) -> None:
        response = await self.web.put(
            "/api/file-inbox/settings",
            json={
                "enabled": True,
                "path": str(self.directory),
                "coalesce_seconds": 0,
                "project_id": project_id,
            },
        )
        assert response.status_code == 200, response.text
        assert self.source.status()["running"], self.source.status()

    def move_text(self, text: str, name: str = "新项目资料.txt") -> Path:
        # A real same-filesystem move creates the native arrival event. The
        # watcher never observes the staging write or receives a fabricated fact.
        staged = self.directory.parent / ("staged-" + name)
        staged.write_text(text, encoding="utf-8")
        return staged.replace(self.directory / name)

    async def attachment(self, text: str) -> dict[str, Any]:
        async def chunks() -> AsyncIterator[bytes]:
            yield text.encode("utf-8")

        return await self.attachments.save(chunks(), "原项目依据.txt", "text/plain")


@asynccontextmanager
async def open_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turns: list[ModelTurn] | None = None,
    seen: list[list[Message]] | None = None,
) -> AsyncIterator[ProjectJourney]:
    requests: list[httpx.Request] = []

    def platform(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform.invalid"
        assert request.method == "POST" and request.url.path == "/send_private_msg"
        requests.append(request)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(requests)}}
        )

    transport = httpx.MockTransport(platform)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, turns if turns is not None else [])
    if seen is not None:
        runtime.client_factory = lambda _: FakeClient(turns if turns is not None else [], seen)  # type: ignore[assignment,return-value]
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"coalesce_seconds": 0},
            "extensions": {"watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "account_id": "123",
                    "owner_ids": ["7", "8"],
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                },
                "proactive": {
                    "external_enabled": True,
                    "default_destination": {
                        "kind": "channel",
                        "route": route("7", account_id="123"),
                    },
                },
            },
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    directory = tmp_path / "project-inbox"
    directory.mkdir(exist_ok=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web,
    ):
        yield ProjectJourney(
            runtime,
            web,
            app.state.personal_io.local,
            app.state.channel_application.attachments,
            app.state.file_inbox,
            app.state.attention,
            app.state.channel_application.manager,
            directory,
            requests,
        )


@pytest.fixture
async def project_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ProjectJourney]:
    async with open_journey(tmp_path, monkeypatch) as journey:
        yield journey


async def test_project_native_arrival_keeps_tools_read_only_and_main_mode(
    project_journey: ProjectJourney, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey = project_journey
    runtime = journey.runtime
    document = journey.directory / "新项目资料.txt"
    secret = "PROJECT_BODY_FROM_THE_REAL_FILE_READER"
    final = "当前文件已按主人选择的 yolo 模式读取，资料复核建议只留本机。"
    turns = [
        ModelTurn(content="保留原主会话权限"),
        ModelTurn(tool_calls=[call("read-project", "fs_read", path=str(document))]),
        ModelTurn(content=final),
    ]
    seen: list[list[Message]] = []
    schemas = capture_schemas(runtime, turns, seen)
    owner = await runtime.submit("沿我选择的 yolo 模式处理只读核查", mode="yolo")
    await runtime.wait(owner["run_id"])
    project = await journey.local.commitments.save(
        "资料复核项目", brief={"goal": "核对新旧资料", "constraints": ["不得发布"]}
    )
    await journey.bind(project["id"])
    reads: list[Path] = []
    original_read = runtime.files._read

    def read(path: Path) -> str:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(runtime.files, "_read", read)
    journey.move_text(secret)
    await eventually(lambda: len(seen) == 3 and not runtime.tasks)
    notices = attention_inputs(runtime)
    assert len(notices) == 1
    marker = notices[0]["attention_project"]
    assert marker["snapshot"]["project_id"] == project["id"]
    assert marker["proofs"][0]["record_id"] == journey.source.list_records()[0]["id"]
    assert notices[0]["source"] == notices[0]["principal"] == "system:attention"
    assert secret not in json.dumps(marker, ensure_ascii=False)
    assert secret not in str(seen[1])
    assert reads == [document]
    result = [message for message in seen[-1] if message.tool_call_id == "read-project"]
    assert len(result) == 1
    assert json.loads(result[0].content) == {"path": str(document), "content": secret}
    assert not runtime.store.list("approval")
    assert runtime.store.get("session", owner["session_id"])["mode"] == "yolo"
    assert "channel_send" in schemas[0] and "fs_read" in schemas[1]
    # The pre-fix red is this positive schema observation after a complete real
    # native-watcher -> Attention -> Runtime -> Files read, never a timeout.
    assert "channel_send" not in schemas[1], "项目复核仍暴露 generic Attention 的渠道发送工具"
    replies = [
        item
        for item in runtime.store.messages(owner["session_id"])
        if item["role"] == "assistant" and item["content"] == final
    ]
    assert len(replies) == 1 and replies[0]["delivery_routes"] == []
    await processed_reply(runtime, journey.manager, replies[0]["id"])
    assert journey.manager.deliveries() == []
    assert not journey.requests


async def test_project_hidden_tools_have_known_rejection_and_complete_pairing(
    project_journey: ProjectJourney,
) -> None:
    journey = project_journey
    runtime = journey.runtime
    document = journey.directory / "新项目资料.txt"
    forbidden = journey.directory.parent / "must-not-write.txt"
    body = "HIDDEN_TOOL_PAIRING_NEGATIVE_CONTROL"
    final = "隐藏操作已明确拒绝，正常只读回执已配对。"
    turns = [
        ModelTurn(content="原主会话已建立"),
        ModelTurn(
            tool_calls=[
                call(
                    "hidden-send",
                    "channel_send",
                    channel="onebot",
                    target="8",
                    text="外部资料不能授予发送权限",
                    idempotency_key="hidden-project-send",
                ),
                call("hidden-write", "fs_write", path=str(forbidden), content="must not write"),
                call("ordinary-info", "fs_info", path=str(document)),
            ]
        ),
        ModelTurn(content=final),
    ]
    seen: list[list[Message]] = []
    schemas = capture_schemas(runtime, turns, seen)
    initial = await runtime.submit("只读项目复核", mode="yolo")
    await runtime.wait(initial["run_id"])
    project = await journey.local.commitments.save(
        "隐藏工具负控", brief={"goal": "只读", "constraints": ["不得操作或外发"]}
    )
    await journey.bind(project["id"])
    journey.move_text(body)
    # Observe a real terminal run, whether successful or failed. A pre-fix
    # failure is judged from its durable unknown/rejected pairing, not a wait
    # for a model continuation that the failing driver can never produce.
    await eventually(lambda: len(seen) >= 2 and not runtime.tasks)
    assert "channel_send" not in schemas[1] and "fs_write" not in schemas[1]
    paired = {
        item["tool_call_id"]: json.loads(item["content"])
        for item in runtime.store.messages(initial["session_id"])
        if item.get("tool_call_id") in {"hidden-send", "hidden-write", "ordinary-info"}
    }
    assert set(paired) == {"hidden-send", "hidden-write", "ordinary-info"}
    assert paired["hidden-send"] == {"error": "工具未启用"}
    assert paired["hidden-write"] == {"error": "工具未启用"}
    assert paired["ordinary-info"]["size"] == len(body.encode("utf-8"))
    assert len(seen) == 3 and not forbidden.exists()
    local_final = runtime.store.messages(initial["session_id"])[-1]
    assert local_final["content"] == final and local_final["delivery_routes"] == []
    await processed_reply(runtime, journey.manager, local_final["id"])
    assert journey.manager.deliveries() == [] and not journey.requests


async def test_project_reads_current_full_brief_original_attachment_and_native_arrival(
    project_journey: ProjectJourney,
) -> None:
    journey = project_journey
    runtime = journey.runtime
    original_text = "ORIGINAL_PROJECT_ATTACHMENT_原附件完整依据"
    arrived_text = "NATIVE_PROJECT_ARRIVAL_新资料完整依据"
    attachment = await journey.attachment(original_text)
    project = await journey.local.commitments.save(
        "完整资料项目",
        brief={
            "goal": "原目标",
            "constraints": ["保留来源"],
            "evidence_refs": [{"kind": "attachment", "id": attachment["id"]}],
        },
    )
    seen: list[list[Message]] = []
    schemas: list[set[str]] = []
    final = "已读当前完整要求、原附件及当前新文件；引用已保存，只提出下一步建议。"

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            schemas.append({tool["function"]["name"] for tool in tools})
            if len(seen) == 1:
                turn = ModelTurn(content="沿主人选定的 ask 模式保留原会话")
            elif len(seen) == 2:
                assert original_text not in str(messages) and arrived_text not in str(messages)
                record_id = journey.source.list_records()[0]["id"]
                assert project["id"] in str(messages) and record_id in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call("current-brief", "commitment_read", identifier=project["id"]),
                        call(
                            "inspect-current",
                            "commitment_inspect",
                            identifier=project["id"],
                            section="task",
                        ),
                        call("arrival-location", "file_inbox_get", id=record_id),
                    ]
                )
            elif len(seen) == 3:
                brief_page = tool_result(messages, "current-brief")
                assert brief_page["revision"] == project["revision"]
                assert brief_page["has_more"] is False
                brief = json.loads(brief_page["text"])
                assert brief == project["brief"]
                inspected = tool_result(messages, "inspect-current")
                assert inspected["task_revision"] == project["revision"]
                assert inspected["has_more"] is False
                assert json.loads(inspected["content"])["brief"] == project["brief"]
                location = tool_result(messages, "arrival-location")["record"]
                assert location["current_file_verified"] is False
                assert original_text not in str(messages) and arrived_text not in str(messages)
                reference = brief["evidence_refs"][0]
                assert reference == {"kind": "attachment", "id": attachment["id"]}
                turn = ModelTurn(
                    tool_calls=[
                        call("original-body", "attachment_read", id=reference["id"]),
                        call("arrived-body", "fs_read", path=location["path"]),
                    ]
                )
            else:
                assert len(seen) == 4
                assert tool_result(messages, "original-body")["content"] == original_text
                assert tool_result(messages, "arrived-body")["content"] == arrived_text
                turn = ModelTurn(content=final)
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Client([], [])  # type: ignore[assignment,return-value]
    owner = await runtime.submit("核当前完整项目要求并保留原附件来源", mode="ask")
    await runtime.wait(owner["run_id"])
    await journey.bind(project["id"])
    project = await journey.local.commitments.save(
        project["title"],
        identifier=project["id"],
        brief={
            "goal": "当前完整目标_第二版本不可被原目标替代",
            "constraints": ["不得发布", "原附件和新文件均须核查", "缺失资料明确未核查"],
            "evidence_refs": [{"kind": "attachment", "id": attachment["id"]}],
        },
    )
    journey.move_text(arrived_text)
    approved: list[str] = []
    for name in (
        "commitment_read",
        "commitment_inspect",
        "file_inbox_get",
        "attachment_read",
        "fs_read",
    ):
        await eventually(lambda: bool(runtime.live_approvals(owner["session_id"])))
        approval = runtime.live_approvals(owner["session_id"])
        assert len(approval) == 1 and approval[0]["tool"] == name
        assert approval[0]["id"] not in approved
        approved.append(approval[0]["id"])
        response = await journey.web.post(
            f"/api/approvals/{approval[0]['id']}", json={"decision": "approve"}
        )
        assert response.status_code == 200 and response.json() == {"resolved": True}
    await eventually(lambda: len(seen) == 4 and not runtime.tasks)
    notice = attention_inputs(runtime)[0]
    assert notice["attention_project"]["snapshot"]["revision"] == project["revision"] == 2
    assert arrived_text not in notice["content"] and original_text not in notice["content"]
    assert all("channel_send" not in names for names in schemas[1:])
    records = runtime.store.messages(owner["session_id"])
    archived = next(item for item in records if item["id"] == notice["id"])
    assert archived["attention_project"] == notice["attention_project"]
    replies = [item for item in records if item["role"] == "assistant" and item["content"] == final]
    assert len(replies) == 1 and replies[0]["delivery_routes"] == []
    await processed_reply(runtime, journey.manager, replies[0]["id"])
    assert len(approved) == 5 and not runtime.permissions.pending
    assert journey.local.commitments.get(project["id"]) == project
    assert runtime.store.get("session", owner["session_id"])["mode"] == "ask"
    assert journey.manager.deliveries() == [] and not journey.requests


@pytest.mark.parametrize(
    ("mode", "review_outcome", "decision", "expected_reads"),
    [
        ("ask", "unused", "approve", 1),
        ("ask", "unused", "reject", 0),
        ("auto", "allow", None, 1),
        ("auto", "deny", "reject", 0),
        ("auto", "error", "reject", 0),
    ],
    ids=["ask-approve", "ask-reject", "auto-allow", "auto-deny", "auto-error"],
)
async def test_project_preserves_the_selected_permission_gate(
    project_journey: ProjectJourney,
    monkeypatch: pytest.MonkeyPatch,
    mode: Mode,
    review_outcome: Literal["unused", "allow", "deny", "error"],
    decision: Literal["approve", "reject"] | None,
    expected_reads: int,
) -> None:
    journey = project_journey
    runtime = journey.runtime
    document = journey.directory / "新项目资料.txt"
    body = "MODE_GATE_PROJECT_BODY"
    turns = [
        ModelTurn(content="原主权限模式已选定"),
        ModelTurn(tool_calls=[call("mode-read", "fs_read", path=str(document))]),
        ModelTurn(content="正文状态以实际只读工具回执为准。"),
    ]
    seen: list[list[Message]] = []
    reviews: list[dict[str, Any]] = []

    class Reviewer(FakeClient):
        async def complete(
            self,
            messages: list[Message],
            tools: Any = (),
            *,
            attempt_guard: Callable[[], None] | None = None,
        ) -> ModelTurn:
            assert not tools
            if attempt_guard is not None:
                attempt_guard()
            reviews.append(json.loads(messages[-1].content))
            if review_outcome == "error":
                raise ValueError("离线审查异常负控")
            return ModelTurn(
                content=json.dumps({"allow": review_outcome == "allow", "reason": "有界只读核查"})
            )

    def factory(config: ModelConfig) -> FakeClient:
        return Reviewer([], []) if config.model == "reviewer" else FakeClient(turns, seen)

    runtime.client_factory = factory  # type: ignore[assignment]
    owner = await runtime.submit("沿所选模式读取项目资料", mode=mode)
    await runtime.wait(owner["run_id"])
    project = await journey.local.commitments.save(
        "权限项目", brief={"goal": "核资料", "constraints": ["只读"]}
    )
    await journey.bind(project["id"])
    reads: list[Path] = []
    original_read = runtime.files._read

    def read(path: Path) -> str:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(runtime.files, "_read", read)
    journey.move_text(body)
    await eventually(
        lambda: bool(runtime.live_approvals()) or (len(seen) == 3 and not runtime.tasks)
    )
    if decision is not None:
        approvals = runtime.live_approvals(owner["session_id"])
        assert len(approvals) == 1 and approvals[0]["tool"] == "fs_read"
        assert reads == [] and body not in str(seen)
        requested = next(
            event
            for event in runtime.store.events()
            if event["type"] == "approval.requested" and event["data"]["id"] == approvals[0]["id"]
        )
        assert "approval_notice" not in requested["data"]
        response = await journey.web.post(
            f"/api/approvals/{approvals[0]['id']}", json={"decision": decision}
        )
        assert response.status_code == 200 and response.json() == {"resolved": True}
    await eventually(lambda: len(seen) == 3 and not runtime.tasks)
    assert reads == [document] * expected_reads
    result = tool_result(seen[-1], "mode-read")
    if expected_reads:
        assert result == {"path": str(document), "content": body}
    else:
        assert "拒绝" in result["error"] and body not in str(seen)
    assert len(reviews) == (1 if mode == "auto" else 0)
    if reviews:
        assert reviews[0]["tool"] == "fs_read" and reviews[0]["risk"] == "read"
        assert reviews[0]["arguments"] == {"path": str(document)}
    assert runtime.store.get("session", owner["session_id"])["mode"] == mode
    final = runtime.store.messages(owner["session_id"])[-1]
    await processed_reply(runtime, journey.manager, final["id"])
    assert final["delivery_routes"] == []
    assert journey.manager.deliveries() == [] and not journey.requests


@pytest.mark.parametrize("boundary", ["tool_batch", "final_stream", "approval"])
async def test_project_leaves_signed_phone_input_for_an_independent_next_run(
    project_journey: ProjectJourney,
    monkeypatch: pytest.MonkeyPatch,
    boundary: Literal["tool_batch", "final_stream", "approval"],
) -> None:
    journey = project_journey
    runtime = journey.runtime
    main_id = runtime.main_session()["id"]
    phone_text = "手机主人新输入：本轮请按本私聊原路独立答复。"
    phone_final = "手机新输入已由独立下一运行处理。"
    project_body = "REAL_PROJECT_READ_BEFORE_PHONE_只读证据"
    document = journey.directory / "新项目资料.txt"
    selected_mode: Mode = "ask" if boundary == "approval" else "yolo"
    entered, release = asyncio.Event(), asyncio.Event()
    owner_entered, owner_release = asyncio.Event(), asyncio.Event()
    seen: list[list[Message]] = []
    runs: list[str] = []
    schemas: list[set[str]] = []
    actual_reads: list[dict[str, Any]] = []
    original_handler = runtime.tools["fs_read"].handler

    async def hold_read(arguments: dict[str, Any]) -> Any:
        result = await original_handler(arguments)
        actual_reads.append(result)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(runtime.tools["fs_read"], "handler", hold_read)
    if boundary == "approval":
        original_completed = runtime._tool_completed

        async def hold_superseded(tool_call: ToolCall, result: Any, session_id: str) -> None:
            await original_completed(tool_call, result, session_id)
            if tool_call.id == "in-flight-read":
                # Owner admission wakes the superseded waiter before its HTTP
                # response returns. Keep this incomplete old batch at a real
                # completion boundary until the caller observes durable pending.
                entered.set()
                await release.wait()

        monkeypatch.setattr(runtime, "_tool_completed", hold_superseded)

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            runs.append(runtime.run_ids[main_id])
            schemas.append({tool["function"]["name"] for tool in tools})
            if len(seen) == 1:
                turn = ModelTurn(content="原主人主会话已建立")
            else:
                latest = next(message for message in reversed(messages) if message.role == "user")
                if latest.content.endswith(phone_text):
                    if len(seen) == 3:
                        owner_entered.set()
                        await owner_release.wait()
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "owner-explicit-send",
                                    "channel_send",
                                    channel="onebot",
                                    target="8",
                                    text=phone_final,
                                    idempotency_key="project-owner-followup",
                                )
                            ]
                        )
                    else:
                        assert len(seen) == 4
                        assert "error" not in tool_result(messages, "owner-explicit-send")
                        turn = ModelTurn(content=phone_final)
                else:
                    assert len(seen) == 2
                    if boundary == "final_stream":
                        yield StreamEvent("text_delta", text="本地资料复核正在结算")
                        entered.set()
                        await release.wait()
                        turn = ModelTurn(content="模型原本拟保存的本地资料说明。")
                    else:
                        turn = ModelTurn(
                            tool_calls=[
                                call("in-flight-read", "fs_read", path=str(document)),
                                call("not-started-info", "fs_info", path=str(document)),
                            ]
                        )
            yield StreamEvent("done", turn=turn)

    runtime.client_factory = lambda _: Client([], [])  # type: ignore[assignment,return-value]
    try:
        initial = await runtime.submit("建立本地主会话", mode=selected_mode)
        await runtime.wait(initial["run_id"])
        project = await journey.local.commitments.save(
            "手机插入隔离项目", brief={"goal": "只读复核", "constraints": ["仅Web本地"]}
        )
        await journey.bind(project["id"])
        journey.move_text(project_body)
        approval_id: str | None = None
        if boundary == "approval":
            await eventually(lambda: bool(runtime.live_approvals(main_id)))
            approvals = runtime.live_approvals(main_id)
            assert len(approvals) == 1 and approvals[0]["tool"] == "fs_read"
            approval_id = approvals[0]["id"]
            assert actual_reads == []
        else:
            await asyncio.wait_for(entered.wait(), 3)
        project_run = runtime.run_ids[main_id]
        assert project_run != initial["run_id"]
        if boundary == "tool_batch":
            assert actual_reads == [{"path": str(document), "content": project_body}]
        response = await phone(journey.web, phone_text, 9701, sender=8)
        assert response.status_code == 200 and response.json() == {"ok": True}
        pending = runtime.conversation.pending(main_id)
        assert len(pending) == 1 and pending[0]["content"] == phone_text
        owner_input = pending[0]
        assert owner_input["principal"] == "owner" and owner_input["mode"] == selected_mode
        assert owner_input["source"] == "channel:onebot:123"
        assert owner_input["route"] == route("8", account_id="123", reply_to="9701")
        assert owner_input["state"] == "pending" and len(seen) == 2
        if approval_id is not None:
            assert runtime.store.get("approval", approval_id)["status"] == "cancelled"
        release.set()
        await asyncio.wait_for(owner_entered.wait(), 3)
        assert len(seen) == 3 and runs[2] != project_run
        assert runs == [initial["run_id"], project_run, runs[2]]
        project_result = runtime.store.get("run", project_run)
        assert project_result["status"] == "completed"
        assert "新输入中断" in project_result["result"] and "尚未完成" in project_result["result"]
        local_final = runtime.store.get("message", project_result["result_message_id"])
        assert local_final["interrupted"] is True and local_final["delivery_routes"] == []
        archived_owner = runtime.store.get("message", owner_input["id"])
        assert (
            archived_owner["run_id"] == runs[2] and archived_owner["route"] == owner_input["route"]
        )
        assert archived_owner["source"] == owner_input["source"]
        assert runtime.store.get("run", runs[2])["mode"] == selected_mode
        if boundary != "final_stream":
            records = runtime.store.messages(main_id)
            results = {
                item["tool_call_id"]: json.loads(item["content"])
                for item in records
                if item.get("tool_call_id") in {"in-flight-read", "not-started-info"}
            }
            assert set(results) == {"in-flight-read", "not-started-info"}
            assert results["not-started-info"]["status"] == "not_executed"
            if boundary == "tool_batch":
                assert results["in-flight-read"] == actual_reads[0]
            else:
                assert results["in-flight-read"]["status"] == "not_executed"
                assert actual_reads == []
        await processed_reply(runtime, journey.manager, local_final["id"])
        assert journey.manager.deliveries() == [] and journey.requests == []
        assert "channel_send" not in schemas[1] and "channel_send" in schemas[2]
        owner_release.set()
        await approve_reply_send(runtime, journey.web, runs[2], phone_final)
        await eventually(lambda: not runtime.tasks)
        phone_replies = [
            item
            for item in runtime.store.messages(main_id)
            if item["role"] == "assistant" and item["content"] == phone_final
        ]
        assert len(phone_replies) == 1 and phone_replies[0]["delivery_routes"] == [
            owner_input["route"]
        ]
        await processed_reply(runtime, journey.manager, phone_replies[0]["id"])
        deliveries = journey.manager.deliveries()
        replies = [item for item in deliveries if delivery_text(item) == phone_final]
        assert len(replies) == 1
        assert (await terminal(journey.manager, replies[0]["id"]))["state"] == "sent"
        notices = [item for item in deliveries if item["id"] != replies[0]["id"]]
        assert all(
            selected_mode == "ask" and item["payload"]["approval_notice"]["run_id"] == runs[2]
            for item in notices
        )
        sent = [json.loads(request.content) for request in journey.requests]
        assert (
            sum(
                "".join(part["data"]["text"] for part in payload["message"]) == phone_final
                for payload in sent
            )
            == 1
        )
        assert all(str(payload["user_id"]) == "8" for payload in sent)
        previous_counts = (len(journey.manager.deliveries()), len(journey.requests))
        assert (await phone(journey.web, phone_text, 9701, sender=8)).status_code == 200
        assert len(seen) == 4 and runs[3] == runs[2]
        assert (len(journey.manager.deliveries()), len(journey.requests)) == previous_counts
        archived = [
            item for item in runtime.store.messages(main_id) if item["id"] == owner_input["id"]
        ]
        assert len(archived) == 1 and archived[0]["run_id"] == runs[2]
    finally:
        release.set()
        owner_release.set()


def metadata_batch() -> ProjectInboxBatch:
    """Construct only a serialization fixture, never a canonical arrival."""
    project_id = new_id()
    return ProjectInboxBatch(
        (ProjectInboxProof(new_id(), 1, new_id(), new_id(), new_id(), project_id),),
        ProjectTaskSnapshot(project_id, 1, "open", 0, 0),
    )


def test_project_first_claim_groups_exact_markers_and_archive_preserves_them(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "project-metadata.sqlite3")
    conversation = Conversation(store)
    session_id = store.main_session()["id"]
    original = metadata_batch()
    different_record = replace(
        original,
        proofs=(replace(original.proofs[0], record_id=new_id(), event_id=2),),
    )
    different_binding = replace(
        original, proofs=(replace(original.proofs[0], binding_revision=new_id()),)
    )
    different_project = metadata_batch()
    batches = [original, original, different_record, different_binding, different_project]
    receipts: list[dict[str, Any]] = []
    try:
        for index, batch in enumerate(batches):
            receipt, inserted = conversation.enqueue(
                session_id,
                "项目序列化定位",
                source="system:attention",
                principal="system:attention",
                mode="yolo",
                max_pending=len(batches) + 2,
                event_id=f"metadata-{index}",
                internal=True,
                attention_routing=WEB_PROJECT_ROUTING,
                attention_project=batch,
            )
            assert inserted
            receipts.append(receipt)
        generic, _ = conversation.enqueue(
            session_id,
            "generic Web事项",
            source="system:attention",
            principal="system:attention",
            mode="yolo",
            max_pending=len(batches) + 2,
            internal=True,
            attention_routing=WEB_PROJECT_ROUTING,
        )
        owner, _ = conversation.enqueue(
            session_id,
            "普通Web主人",
            source="web",
            principal="owner",
            mode="yolo",
            max_pending=len(batches) + 2,
        )
        groups = [receipts[:2], [receipts[2]], [receipts[3]], [receipts[4]], [generic], [owner]]
        for expected in groups:
            claimed = store.claim_inputs(session_id, new_id(), limit=len(batches) + 2)
            assert [item["id"] for item in claimed] == [item["id"] for item in expected]
            for actual, receipt in zip(claimed, expected, strict=True):
                assert actual.get("attention_project") == receipt.get("attention_project")
                assert ("attention_project" in actual) is ("attention_project" in receipt)
        assert store.pending_inputs(session_id) == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "invalid",
    [
        "null",
        "future",
        "proof",
        "snapshot",
        "routing",
        "routing_version",
        "routing_float",
        "reply_origin",
        "missing_origin",
        "source",
    ],
)
async def test_project_bad_persisted_marker_is_rejected_without_generic_fallback(
    project_journey: ProjectJourney,
    invalid: Literal[
        "null",
        "future",
        "proof",
        "snapshot",
        "routing",
        "routing_version",
        "routing_float",
        "reply_origin",
        "missing_origin",
        "source",
    ],
) -> None:
    journey = project_journey
    runtime = journey.runtime
    seen: list[list[Message]] = []
    runtime.client_factory = lambda _: FakeClient([], seen)  # type: ignore[assignment]
    session_id = runtime.main_session()["id"]
    payload = Conversation.payload(
        session_id,
        "坏持久标记不能被当作普通主动通知",
        source="system:attention",
        principal="system:attention",
        mode="yolo",
        attention_routing=WEB_PROJECT_ROUTING,
    )
    marker: Any = deepcopy(metadata_batch().dump())
    if invalid == "null":
        marker = None
    elif invalid == "future":
        marker["version"] += 1
    elif invalid == "proof":
        marker["proofs"][0]["version"] += 1
    elif invalid == "snapshot":
        marker["snapshot"]["version"] += 1
    elif invalid == "routing":
        payload["attention_routing"]["destination"]["route"] = route("8", account_id="123")
    elif invalid == "routing_version":
        payload["attention_routing"]["v"] = True
    elif invalid == "routing_float":
        payload["attention_routing"]["v"] = 1.0
    elif invalid == "reply_origin":
        payload["reply_origin"]["route"] = route("8", account_id="123")
    elif invalid == "missing_origin":
        del payload["reply_origin"]
    else:
        payload["source"] = "web"
    payload["attention_project"] = marker
    # Deliberately model disk corruption after the trusted Conversation builder.
    # Store must retain the marker, so first-claim validation can refuse it.
    runtime.store.enqueue_input(
        payload, max_pending=runtime.config.settings.limits.max_pending_runs
    )
    runtime._wake_main()
    run_id = runtime.run_ids[session_id]
    with pytest.raises(ValueError, match="宿主证明"):
        await runtime.wait(run_id)
    await eventually(lambda: not runtime.tasks)
    assert seen == [] and not runtime.permissions.pending
    archived = runtime.store.get("message", payload["id"])
    assert "attention_project" in archived and archived["attention_project"] == marker
    assert archived["attention_routing"] == payload["attention_routing"]
    assert archived["reply_origin"] == payload.get("reply_origin")
    assert runtime.store.get("run", run_id)["status"] == "failed"
    assert journey.manager.deliveries() == [] and journey.requests == []


@pytest.mark.parametrize("boundary", ["source", "principal", "route", "routing"])
def test_conversation_project_marker_requires_exact_trusted_local_metadata(
    boundary: Literal["source", "principal", "route", "routing"],
) -> None:
    arguments: dict[str, Any] = {
        "source": "system:attention",
        "principal": "system:attention",
        "mode": "yolo",
        "attention_routing": WEB_PROJECT_ROUTING,
        "attention_project": metadata_batch(),
    }
    if boundary == "source":
        arguments["source"] = "web"
    elif boundary == "principal":
        arguments["principal"] = "owner"
    elif boundary == "route":
        arguments["route"] = route("8", account_id="123")
    else:
        arguments["attention_routing"] = None
    with pytest.raises(ValueError, match="宿主证明"):
        Conversation.payload("serialization-session", "项目元数据", **arguments)


async def test_runtime_project_admission_rejects_shape_valid_noncanonical_proof(
    project_journey: ProjectJourney,
) -> None:
    journey = project_journey
    batch = metadata_batch()
    assert ProjectInboxBatch.load(batch.dump()) == batch
    with pytest.raises(ValueError, match="证明"):
        await journey.runtime.submit_attention(
            "形状有效但无canonical来源的项目",
            "forged-project",
            objects=[],
            routing=WEB_PROJECT_ROUTING,
            project=batch,
        )
    assert not journey.runtime.tasks and attention_inputs(journey.runtime) == []
    assert journey.manager.deliveries() == [] and journey.requests == []


async def test_project_rechecks_real_tool_risk_after_the_last_await(
    project_journey: ProjectJourney, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey = project_journey
    runtime = journey.runtime
    document = journey.directory / "新项目资料.txt"
    turns = [
        ModelTurn(content="原模式yolo"),
        ModelTurn(tool_calls=[call("last-fence", "fs_read", path=str(document))]),
        ModelTurn(content="最后权限核查拒绝了变为外发风险的调用。"),
    ]
    seen: list[list[Message]] = []
    capture_schemas(runtime, turns, seen)
    initial = await runtime.submit("纯读项目", mode="yolo")
    await runtime.wait(initial["run_id"])
    project = await journey.local.commitments.save(
        "最终fence项目", brief={"goal": "只读", "constraints": []}
    )
    await journey.bind(project["id"])
    entered: list[str] = []
    original_read = runtime.files._read
    original_emit = runtime.bus.emit

    def read(path: Path) -> str:
        entered.append(str(path))
        return original_read(path)

    async def emit(
        kind: str, data: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        result = await original_emit(kind, data, session_id)
        if kind == "tool.started" and data["call_id"] == "last-fence":
            monkeypatch.setattr(runtime.tools["fs_read"], "risk", "external")
        return result

    monkeypatch.setattr(runtime.files, "_read", read)
    monkeypatch.setattr(runtime.bus, "emit", emit)
    journey.move_text("FINAL_FENCE_MUST_NOT_READ")
    await eventually(lambda: len(seen) == 3 and not runtime.tasks)
    assert entered == []
    result = tool_result(seen[-1], "last-fence")
    assert "只能在 Web 本地读取" in result["error"] and result.get("status") != "unknown"
    assert not runtime.permissions.pending


async def test_project_partial_model_failure_keeps_its_persisted_message_local(
    project_journey: ProjectJourney,
) -> None:
    journey = project_journey
    runtime = journey.runtime
    seen: list[list[Message]] = []
    partial = "仅本机保存的项目中断片段"

    class Client(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.append(list(messages))
            if len(seen) == 1:
                yield StreamEvent("done", turn=ModelTurn(content="原主会话"))
                return
            yield StreamEvent("text_delta", text=partial)
            raise ValueError("离线模型中断负控")

    runtime.client_factory = lambda _: Client([], [])  # type: ignore[assignment,return-value]
    initial = await runtime.submit("本机项目", mode="yolo")
    await runtime.wait(initial["run_id"])
    project = await journey.local.commitments.save(
        "中断项目", brief={"goal": "只读", "constraints": []}
    )
    await journey.bind(project["id"])
    journey.move_text("PARTIAL_ERROR_BODY_NOT_PREINJECTED")
    await eventually(lambda: len(seen) == 2 and not runtime.tasks)
    messages = [
        item
        for item in runtime.store.messages(initial["session_id"])
        if item["role"] == "assistant" and item["content"] == partial
    ]
    assert len(messages) == 1 and messages[0]["interrupted"] is True
    assert messages[0]["delivery_routes"] == []
    await processed_reply(runtime, journey.manager, messages[0]["id"])
    assert journey.manager.deliveries() == [] and journey.requests == []


async def test_project_proof_and_original_attachment_survive_compaction_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_body = "ORIGINAL_ATTACHMENT_AFTER_REAL_COMPACTION_AND_REOPEN"
    first_turns = [ModelTurn(content="原附件已登记"), ModelTurn(content="项目资料仅在本机复核")]
    first_seen: list[list[Message]] = []
    async with open_journey(tmp_path, monkeypatch, turns=first_turns, seen=first_seen) as first:
        runtime = first.runtime
        original = await first.attachment(original_body)
        initial = await runtime.submit(
            "原附件是本项目依据，请保留引用", attachment_ids=[original["id"]], mode="yolo"
        )
        await runtime.wait(initial["run_id"])
        main_id = initial["session_id"]
        original_message = runtime.store.get("message", initial["message"]["id"])
        project = await first.local.commitments.save(
            "压缩重开项目",
            brief={
                "goal": "核对到达资料",
                "constraints": ["只读，不发布"],
                "evidence_refs": [{"kind": "attachment", "id": original["id"]}],
            },
        )
        await first.bind(project["id"])
        first.move_text("NATIVE_ARRIVAL_BEFORE_COMPACTION")
        await eventually(lambda: len(first_seen) == 2 and not runtime.tasks)
        notice = runtime.store.get("message", attention_inputs(runtime)[0]["id"])
        marker = deepcopy(notice["attention_project"])
        source_state = first.source.settings_state()
        for number in range(2):
            first_turns.append(ModelTurn(content=f"后续核查 {number}：" + "仍仅本机核查。" * 80))
            following = await runtime.submit(
                f"后续资料 {number}：" + "保留原依据及完整约束。" * 200, mode="yolo"
            )
            await runtime.wait(following["run_id"])

        async def summarize(source: str, max_tokens: int) -> str:
            raise AssertionError("本次真实压缩必须沿离线摘录路径")

        compactor = Compactor(
            runtime.store,
            summarize,
            lambda: CompactionSettings(mode="extractive"),
            max_chars=lambda: 4000,
            max_jobs=lambda: 1,
            fingerprint=lambda: "project-proof-offline",
        )
        try:
            compactor.observe(main_id)
            await compactor.finish(main_id)
            active = runtime.store.get("compaction", main_id)
            assert active is not None and active["covered_seq"] >= notice["seq"]
            assert compactor.project(main_id)[0]["compaction_summary"] is True
            assert runtime.store.get("message", notice["id"]) == notice
            assert runtime.store.get("message", original_message["id"]) == original_message
        finally:
            await compactor.close()
        assert len(runtime.store.list("attention_receipt")) == 1
        assert first.manager.deliveries() == [] and first.requests == []

    reopened_seen: list[list[Message]] = []
    async with open_journey(tmp_path, monkeypatch, turns=[], seen=reopened_seen) as reopened:
        runtime = reopened.runtime
        assert runtime.main_session()["id"] == main_id
        assert reopened.source is not first.source and reopened.source.status()["running"]
        assert reopened.source.settings_state() == source_state
        barrier = await runtime.bus.emit("test.project-reopen-barrier", {})
        await eventually(lambda: reopened.attention.cursor >= barrier["id"])
        assert reopened_seen == []
        assert len(attention_inputs(runtime)) == len(runtime.store.list("attention_receipt")) == 1
        assert runtime.store.get("message", notice["id"])["attention_project"] == marker
        assert runtime.store.get("message", notice["id"])["seq"] == notice["seq"]
        request: PreparedRequest = await runtime._prepare_request(main_id)
        assert request.project_inbox is None

        class Reader(FakeClient):
            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                reopened_seen.append(list(messages))
                if len(reopened_seen) == 1:
                    assert original_body not in str(messages)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "old-owner",
                                "conversation_read",
                                session_id=main_id,
                                seq=original_message["seq"],
                            ),
                            call(
                                "old-project",
                                "conversation_read",
                                session_id=main_id,
                                seq=notice["seq"],
                            ),
                        ]
                    )
                elif len(reopened_seen) == 2:
                    owner_record = tool_result(messages, "old-owner")
                    project_record = tool_result(messages, "old-project")
                    assert owner_record["seq"] == original_message["seq"]
                    assert owner_record["attachment_ids"] == [original["id"]]
                    assert project_record["seq"] == notice["seq"]
                    assert project_record["source"] == "system:attention"
                    assert project_record["content"] == notice["content"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "old-attachment",
                                "attachment_read",
                                id=owner_record["attachment_ids"][0],
                            )
                        ]
                    )
                else:
                    assert len(reopened_seen) == 3
                    assert tool_result(messages, "old-attachment")["content"] == original_body
                    turn = ModelTurn(content="压缩重开后已从原消息定位并读取原附件")
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda _: Reader([], [])  # type: ignore[assignment,return-value]
        resumed = await runtime.submit("请从原消息核查附件，继续只读", mode="yolo")
        assert await runtime.wait(resumed["run_id"]) == "压缩重开后已从原消息定位并读取原附件"
        assert len(reopened_seen) == 3
        assert runtime.store.get("message", notice["id"])["attention_project"] == marker
        assert runtime.store.get("message", original_message["id"]) == original_message
        assert len(runtime.store.list("attention_receipt")) == 1
        final = runtime.store.messages(main_id)[-1]
        await processed_reply(runtime, reopened.manager, final["id"])
        assert final["delivery_routes"] == []
        assert reopened.manager.deliveries() == [] and reopened.requests == []


async def test_interrupted_project_reopens_only_the_original_pending_phone_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    phone_text = "手机持久原文：重开后仍按本私聊原路答复"
    phone_final = "重开后已在独立运行处理原手机输入"
    body = "ACTUAL_PROJECT_READ_BEFORE_NORMAL_CLOSE"
    document = tmp_path / "project-inbox" / "新项目资料.txt"
    first_turns = [
        ModelTurn(content="原会话已建立"),
        ModelTurn(tool_calls=[call("before-close", "fs_read", path=str(document))]),
    ]
    first_seen: list[list[Message]] = []
    async with open_journey(tmp_path, monkeypatch, turns=first_turns, seen=first_seen) as first:
        runtime = first.runtime
        actual: list[dict[str, Any]] = []
        original_handler = runtime.tools["fs_read"].handler

        async def hold_read(arguments: dict[str, Any]) -> Any:
            result = await original_handler(arguments)
            actual.append(result)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(runtime.tools["fs_read"], "handler", hold_read)
        try:
            initial = await runtime.submit("建立持久主会话", mode="yolo")
            await runtime.wait(initial["run_id"])
            main_id = initial["session_id"]
            project = await first.local.commitments.save(
                "重开插入项目", brief={"goal": "只读复核", "constraints": ["仅本机"]}
            )
            await first.bind(project["id"])
            first.move_text(body)
            await asyncio.wait_for(entered.wait(), 3)
            project_run = runtime.run_ids[main_id]
            assert actual == [{"path": str(document), "content": body}]
            response = await phone(first.web, phone_text, 9740, sender=8)
            assert response.status_code == 200 and response.json() == {"ok": True}
            pending = runtime.conversation.pending(main_id)
            assert len(pending) == 1 and pending[0]["content"] == phone_text
            owner_input = deepcopy(pending[0])
            # Hold the ordinary automatic wake so a normal close occurs with
            # the genuine signed owner input still durably pending.
            runtime._main_wake_deferred = True
            release.set()
            await eventually(lambda: not runtime.tasks)
            assert len(first_seen) == 2 and runtime.conversation.pending(main_id) == pending
            run = runtime.store.get("run", project_run)
            final = runtime.store.get("message", run["result_message_id"])
            assert final["interrupted"] is True and "尚未完成" in final["content"]
            assert final["delivery_routes"] == []
            notice = runtime.store.get("message", attention_inputs(runtime)[0]["id"])
            marker = deepcopy(notice["attention_project"])
            await processed_reply(runtime, first.manager, final["id"])
            assert first.manager.deliveries() == [] and first.requests == []
        finally:
            release.set()

    resumed_seen: list[list[Message]] = []
    async with open_journey(
        tmp_path,
        monkeypatch,
        turns=[
            ModelTurn(
                tool_calls=[
                    call(
                        "reopened-owner-send",
                        "channel_send",
                        channel="onebot",
                        target="8",
                        text=phone_final,
                        idempotency_key="project-reopened-owner-followup",
                    )
                ]
            ),
            ModelTurn(content=phone_final),
        ],
        seen=resumed_seen,
    ) as reopened:
        runtime = reopened.runtime
        await eventually(lambda: len(resumed_seen) == 2 and not runtime.tasks)
        assert runtime.main_session()["id"] == main_id
        assert len(attention_inputs(runtime)) == len(runtime.store.list("attention_receipt")) == 1
        assert runtime.store.get("message", notice["id"])["attention_project"] == marker
        assert runtime.store.get("message", notice["id"])["seq"] == notice["seq"]
        assert runtime.conversation.pending(main_id) == []
        restored_owner = runtime.store.get("message", owner_input["id"])
        assert restored_owner["run_id"] != project_run
        assert restored_owner["content"] == phone_text and restored_owner["principal"] == "owner"
        assert restored_owner["source"] == owner_input["source"] == "channel:onebot:123"
        assert (
            restored_owner["route"]
            == owner_input["route"]
            == route("8", account_id="123", reply_to="9740")
        )
        assert runtime.store.get("run", restored_owner["run_id"])["mode"] == "yolo"
        replies = [
            item
            for item in runtime.store.messages(main_id)
            if item["role"] == "assistant" and item["content"] == phone_final
        ]
        assert len(replies) == 1 and replies[0]["delivery_routes"] == [owner_input["route"]]
        await processed_reply(runtime, reopened.manager, replies[0]["id"])
        deliveries = reopened.manager.deliveries()
        assert len(deliveries) == 1
        assert (await terminal(reopened.manager, deliveries[0]["id"]))["state"] == "sent"
        assert len(reopened.requests) == 1
        assert str(json.loads(reopened.requests[0].content)["user_id"]) == "8"
        barrier = await runtime.bus.emit("test.project-reopen-barrier", {})
        await eventually(lambda: reopened.attention.cursor >= barrier["id"])
        assert len(resumed_seen) == 2
        assert (await phone(reopened.web, phone_text, 9740, sender=8)).status_code == 200
        assert len(resumed_seen) == 2
        assert len(reopened.manager.deliveries()) == len(reopened.requests) == 1
