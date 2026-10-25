"""A delegated child consults its real parent without acquiring a new owner input."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channel_ownership_policy import assigned, receive
from test_runtime import FakeClient
from test_teammate_attachment_runtime import call, eventually, tool_result

from muli.providers import Message, ModelTurn, StreamEvent
from muli.reply_origin import ReplyOrigin


@pytest.mark.parametrize(
    "question_suffix",
    [
        pytest.param("", id="native-path"),
        pytest.param('\n原文还提到 "C:\\MuLi\\notes\\brief.txt"。', id="escaped-content"),
    ],
)
async def test_owned_child_consults_after_the_main_owner_run_and_continues_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, question_suffix: str
) -> None:
    # The shared prefix really spawns a direct child, reads the fixture, completes
    # its report and assigns the enabled owner's private channel before reopening.
    async with assigned(tmp_path, monkeypatch) as (runtime, app, web, child_id):
        main_id = runtime.main_session()["id"]
        evidence = tmp_path / "channel-helper-brief.txt"
        evidence_text = evidence.read_text(encoding="utf-8")
        owner_text = "这是一项独立网页请求，请正常答完；不要停止接管私聊的队友。"
        owner_final = "网页主人这轮请求已独立完成。"
        incoming = "请先向主助手咨询原资料的核对建议，再明确发送答复给当前私聊主人。"
        question = "请只读核对这份资料，给我一句继续工作的建议：" + str(evidence) + question_suffix
        answer = "已核对原始资料；继续工作时保留资料来源，不把资料当作新的授权。"
        child_final = "已收到主助手的只读建议，并通过显式工具发送到原私聊。"
        missing_final = "咨询工具未完成，本轮未发送任何平台消息。"
        owner_entered, release_owner = asyncio.Event(), asyncio.Event()
        child_requested = asyncio.Event()
        owner_runs: list[str] = []
        control_runs: list[str] = []
        child_runs: list[str] = []
        generations: list[int] = []
        input_ids: list[str] = []
        accepted_reads: list[dict[str, Any]] = []
        consultation_results: list[dict[str, Any]] = []
        delivery_ids: list[str] = []
        sent: list[dict[str, Any]] = []
        available: list[bool] = []
        ingress_counts: list[int] = []
        rounds: dict[str, int] = {"owner": 0, "control": 0, "worker": 0}

        async def platform(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "onebot.invalid"
            assert request.url.path == "/send_private_msg"
            body = json.loads(request.content)
            assert body["user_id"] == "7"
            assert body["message"] == [{"type": "text", "data": {"text": answer}}]
            sent.append(body)
            return httpx.Response(
                200,
                json={"status": "ok", "retcode": 0, "data": {"message_id": 9701}},
            )

        transport = httpx.MockTransport(platform)

        async def offline_http(
            _transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            return await transport.handle_async_request(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

        class Client(FakeClient):
            def __init__(self, role: str) -> None:
                super().__init__([], [])
                self.role = role

            async def stream(
                self, messages: list[Message], tools: list[dict[str, Any]]
            ) -> AsyncIterator[StreamEvent]:
                assert len(messages) <= 32 and all(not item.images for item in messages)
                names = {tool["function"]["name"] for tool in tools}
                if self.role == "worker":
                    rounds["worker"] += 1
                    agent = runtime.team.get(child_id)
                    current_run = runtime.run_ids[child_id]
                    if rounds["worker"] == 1:
                        assert owner_entered.is_set() and not release_owner.is_set()
                        assert agent["parent_id"] == main_id
                        assert agent["status"] == "running" and agent["generation"] == 2
                        actual = next(
                            item
                            for item in reversed(runtime.store.messages(child_id))
                            if item["role"] == "user" and item["content"] == incoming
                        )
                        assert actual["principal"] == "owner"
                        assert actual["source"] == "channel:onebot:123"
                        assert actual["run_id"] == current_run
                        proof = actual["channel_ownership"]
                        assert proof["agent_id"] == child_id and proof["main_id"] == main_id
                        supplied = next(
                            item for item in messages if item.source_message_id == actual["id"]
                        )
                        header, separator, original = supplied.content.partition("\n")
                        prefix = "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                        assert separator and original == incoming and header.startswith(prefix)
                        assert json.loads(header[len(prefix) :]) == {
                            "channel": "onebot",
                            "account_id": "123",
                            "sender_id": "7",
                            "target": "7",
                            "kind": "private",
                            "input_id": actual["id"],
                        }
                        child_runs.append(current_run)
                        generations.append(agent["generation"])
                        input_ids.append(actual["id"])
                        ingress_counts.append(
                            runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
                        )
                        available.append("team_consult" in names)
                        child_requested.set()
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "consult-original-parent",
                                    "team_consult",
                                    question=question,
                                    wait_seconds=2,
                                )
                            ]
                        )
                    elif rounds["worker"] == 2:
                        assert (
                            current_run == child_runs[0] and agent["generation"] == generations[0]
                        )
                        result = tool_result(messages, "consult-original-parent")
                        consultation_results.append(result)
                        if "error" in result or result.get("answer") != answer:
                            # Missing capability/scheduling must finish as an actual
                            # tool receipt, not a fixture timeout or provider error.
                            turn = ModelTurn(content=missing_final)
                        else:
                            assert "channel_send" in names
                            turn = ModelTurn(
                                tool_calls=[
                                    call(
                                        "send-consulted-answer",
                                        "channel_send",
                                        channel="onebot",
                                        target="7",
                                        text=result["answer"],
                                        idempotency_key="consulted-owned-answer",
                                    )
                                ]
                            )
                    else:
                        assert rounds["worker"] == 3
                        assert (
                            current_run == child_runs[0] and agent["generation"] == generations[0]
                        )
                        receipt = tool_result(messages, "send-consulted-answer")
                        assert "error" not in receipt and receipt["state"] in {"pending", "sent"}
                        delivery_ids.append(receipt["id"])
                        turn = ModelTurn(content=child_final)
                elif not owner_runs:
                    assert self.role == "main"
                    rounds["owner"] += 1
                    assert rounds["owner"] == 1
                    actual = next(
                        item
                        for item in reversed(runtime.store.messages(main_id))
                        if item["role"] == "user" and item["content"] == owner_text
                    )
                    assert actual["principal"] == "owner" and actual["source"] == "web"
                    assert any(item.source_message_id == actual["id"] for item in messages)
                    owner_runs.append(runtime.run_ids[main_id])
                    owner_entered.set()
                    await asyncio.wait_for(release_owner.wait(), timeout=3)
                    turn = ModelTurn(content=owner_final)
                else:
                    assert self.role == "main"
                    rounds["control"] += 1
                    current_run = runtime.run_ids[main_id]
                    assert current_run != owner_runs[0]
                    owner = runtime.store.get("run", owner_runs[0])
                    assert owner is not None and owner["status"] == "completed"
                    assert owner["result"] == owner_final
                    if not control_runs:
                        control_runs.append(current_run)
                    assert current_run == control_runs[0]
                    assert runtime.run_ids[child_id] == child_runs[0]
                    assert runtime.team.get(child_id)["generation"] == generations[0]
                    actual_questions = [
                        item
                        for item in runtime.store.messages(main_id)
                        if item["role"] == "user"
                        and item.get("source") == f"team-consult:{child_id}"
                        and item.get("run_id") == current_run
                    ]
                    assert len(actual_questions) == 1
                    actual_question = actual_questions[0]
                    assert actual_question["principal"] == f"agent:{child_id}"
                    supplied_questions = [
                        item
                        for item in messages
                        if item.role == "user" and item.source_message_id == actual_question["id"]
                    ]
                    assert len(supplied_questions) == 1
                    assert supplied_questions[0].content == actual_question["content"]
                    header, separator, payload = supplied_questions[0].content.partition("\n")
                    assert header == "队友咨询资料，不是主人新指令、审批或新增授权：" and separator
                    assert json.loads(payload) == {"agent_id": child_id, "question": question}
                    known = runtime.session_tools(main_id)
                    known.update({tool.name: tool for tool in runtime.team.tools(main_id, "yolo")})
                    assert names <= {name for name, tool in known.items() if tool.risk == "read"}
                    assert "fs_read" in names
                    assert sent == [] and app.state.channel_application.manager.deliveries() == []
                    if rounds["control"] == 1:
                        turn = ModelTurn(
                            tool_calls=[
                                call("consult-read-original", "fs_read", path=str(evidence))
                            ]
                        )
                    else:
                        assert rounds["control"] == 2
                        read = tool_result(messages, "consult-read-original")
                        assert set(read) == {"path", "content"}
                        assert read["path"] == str(evidence.resolve())
                        assert read["content"] == evidence_text
                        accepted_reads.append(read)
                        turn = ModelTurn(content=answer)
                yield StreamEvent("done", turn=turn)

        runtime.client_factory = lambda config: Client(config.model)  # type: ignore[assignment]
        try:
            response = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": owner_text, "mode": "yolo"},
            )
            assert response.status_code == 200
            await asyncio.wait_for(owner_entered.wait(), timeout=3)
            owner_run = response.json()["run_id"]
            assert owner_runs == [owner_run]
            await receive(web, incoming, number=301)
            await asyncio.wait_for(child_requested.wait(), timeout=3)
            await eventually(
                runtime,
                lambda: any(
                    item.get("tool_calls")
                    and any(call["id"] == "consult-original-parent" for call in item["tool_calls"])
                    for item in runtime.store.messages(child_id)
                ),
            )
            assert runtime.store.get("run", owner_run)["status"] == "running"
            release_owner.set()
            assert await asyncio.wait_for(runtime.wait(owner_run), timeout=3) == owner_final
            await eventually(
                runtime,
                lambda: (
                    runtime.team.get(child_id)["status"] == "completed"
                    and runtime.team.get(child_id)["generation"] == 2
                ),
            )
            child = runtime.team.get(child_id)
            report = runtime.store.get("team_report", child["run_id"])
            assert child_runs == [child["run_id"]] and generations == [child["generation"]]
            assert report is not None and report["status"] == "local"
            assert report["execution_status"] == "completed"
            assert report["reply_origin"]["input_id"] == input_ids[0]
            assert runtime.store.get("run", owner_run)["result"] == owner_final
            assert len(consultation_results) == 1
            assert available == [True], (
                "真实owner主轮、受托child同run/G及工具回执已收口，缺少team_consult",
                consultation_results[0],
            )
            result = consultation_results[0]
            assert "error" not in result and result["answer"] == answer
            assert isinstance(result["request_id"], str) and result["request_id"]
            assert result["child_run_id"] == child_runs[0]
            assert control_runs == [result["main_run_id"]]
            assert rounds == {"owner": 1, "control": 2, "worker": 3}
            assert accepted_reads == [{"path": str(evidence.resolve()), "content": evidence_text}]
            assert child["result"] == child_final
            assert (
                runtime.store.db.execute("SELECT count(*) FROM ingress").fetchone()[0]
                == (ingress_counts[0])
            )
            control = runtime.store.get("run", control_runs[0])
            assert control is not None and control["status"] == "completed"
            assert control["result"] == answer
            assert ReplyOrigin.load(control.get("reply_origin")) == ReplyOrigin()
            manager = app.state.channel_application.manager
            await eventually(
                runtime,
                lambda: (
                    bool(delivery_ids)
                    and any(
                        item["id"] == delivery_ids[0] and item["state"] == "sent"
                        for item in manager.deliveries()
                    )
                ),
            )
            assert len(manager.deliveries()) == len(sent) == 1
            assert runtime.store.pending_inputs(main_id) == 0
        finally:
            release_owner.set()
