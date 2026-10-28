"""Offline user journeys through real ingress, Team, tools and report delivery."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_channels import consumed, onebot_body, onebot_event
from test_runtime import FakeClient, make_runtime

from muli.attachments import Attachments
from muli.channels import ChannelManager
from muli.compaction import Compactor
from muli.config import CompactionSettings
from muli.local_services import LocalServices
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app


def call(identifier: str, name: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(identifier, name, json.dumps(arguments, ensure_ascii=False))


def tool_result(messages: list[Message], identifier: str) -> dict[str, Any]:
    message = next(message for message in reversed(messages) if message.tool_call_id == identifier)
    result = json.loads(message.content)
    assert isinstance(result, dict)
    return result


async def eventually(runtime: Runtime, condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(4):
        while not condition():
            revision = runtime.bus.revision
            async with runtime.bus.changed:
                await runtime.bus.changed.wait_for(
                    lambda expected=revision: runtime.bus.revision != expected
                )


def completed(runtime: Runtime, content: str) -> list[dict[str, Any]]:
    return [
        item
        for item in runtime.store.messages(runtime.main_session()["id"])
        if item["role"] == "assistant" and item.get("content") == content
    ]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("此场景只允许显式 MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)


async def test_web_assignment_mobile_supplement_reads_real_files_and_reports_to_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "limits": {"max_output_chars": 1000},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
            "channels": {
                "onebot": {
                    "enabled": True,
                    "base_url": "https://platform.invalid",
                    "webhook_secret": "FAKE_WEBHOOK",
                    "owner_ids": ["7"],
                    "receive_files": True,
                }
            },
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime.data_dir, runtime)
    asr_entered, asr_release = asyncio.Event(), asyncio.Event()
    initial_ids: list[str] = []
    main_seen: list[list[Message]] = []
    worker_seen: list[list[Message]] = []
    sent: list[dict[str, Any]] = []
    downloads: list[str] = []
    asr_requests: list[httpx.Request] = []
    web_secret, mobile_secret = "WEB-BODY-UNIQUE", "MOBILE-BODY-UNIQUE"
    transcript = "前置资料🙂" * 300 + "ASR-END-ONLY"
    main_round = 0
    worker_round = 0
    mobile_id = ""
    transcript_cursor: dict[str, Any] = {}

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal main_round, mobile_id
            main_round += 1
            main_seen.append(list(messages))
            if main_round == 1:
                assert web_secret not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "spawn",
                            "spawn_agent",
                            task_name="资料队友",
                            prompt="读取网页资料并等待补充",
                            attachment_ids=initial_ids,
                        )
                    ]
                )
            elif main_round == 2:
                turn = ModelTurn(
                    tool_calls=[call("wait", "wait_agents", targets=["资料队友"], timeout=10)]
                )
            elif main_round == 3:
                assert tool_result(messages, "wait")["reason"] == "new_input"
                owner = next(
                    item
                    for item in reversed(runtime.store.messages(runtime.main_session()["id"]))
                    if item.get("source", "").startswith("channel:")
                )
                assert owner["principal"] == "owner" and len(owner["attachment_ids"]) == 1
                mobile_id = owner["attachment_ids"][0]
                assert mobile_id in str(messages) and mobile_secret not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "supplement",
                            "send_input",
                            target="资料队友",
                            message="同一任务新增移动资料",
                            attachment_ids=[mobile_id],
                        )
                    ]
                )
            elif main_round == 4:
                assert tool_result(messages, "supplement")["accepted"]
                turn = ModelTurn(content="移动补充已交给原队友")
            else:
                assert main_round == 5
                assert "三份资料已核对" in str(messages)
                turn = ModelTurn(content="网页任务已完成")
            yield StreamEvent("done", turn=turn)

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_round, transcript_cursor
            worker_round += 1
            worker_seen.append(list(messages))
            assert worker_round < 20
            if worker_round == 1:
                assert all(identifier in str(messages) for identifier in initial_ids)
                assert web_secret not in str(messages) and "ASR-END-ONLY" not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call("read-web", "attachment_read", id=initial_ids[0]),
                        call("asr", "audio_transcribe", id=initial_ids[1]),
                    ]
                )
            elif worker_round == 2:
                assert web_secret in tool_result(messages, "read-web")["content"]
                transcript_cursor = tool_result(messages, "asr")
                assert (
                    transcript_cursor["has_more"]
                    and "ASR-END-ONLY" not in transcript_cursor["text"]
                )
                assert mobile_id in str(messages) and mobile_secret not in str(messages)
                turn = ModelTurn(
                    tool_calls=[
                        call("read-mobile", "attachment_read", id=mobile_id),
                        call(
                            "transcript-2",
                            "audio_transcript_read",
                            id=transcript_cursor["transcript_id"],
                            revision=transcript_cursor["revision"],
                            offset_bytes=transcript_cursor["next_offset_bytes"],
                        ),
                    ]
                )
            else:
                assert mobile_secret in tool_result(messages, "read-mobile")["content"]
                page = tool_result(messages, f"transcript-{worker_round - 1}")
                if page["has_more"]:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                f"transcript-{worker_round}",
                                "audio_transcript_read",
                                id=transcript_cursor["transcript_id"],
                                revision=transcript_cursor["revision"],
                                offset_bytes=page["next_offset_bytes"],
                            )
                        ]
                    )
                else:
                    assert "ASR-END-ONLY" in page["content"]
                    turn = ModelTurn(content="三份资料已核对")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], []) if config.model == "worker" else Main([], [])

    async def channel_http(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.host == "93.184.216.34"
            downloads.append(str(request.url))
            return httpx.Response(200, content=mobile_secret.encode())
        assert request.url.path == "/send_private_msg"
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": "delivered"}}
        )

    async def asr_http(request: httpx.Request) -> httpx.Response:
        asr_requests.append(request)
        asr_entered.set()
        await asr_release.wait()
        return httpx.Response(200, json={"text": transcript})

    runtime.client_factory = factory  # type: ignore[assignment]
    original = app.state.channel_application.manager
    try:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(channel_http)) as channel_client,
            httpx.AsyncClient(transport=httpx.MockTransport(asr_http)) as asr_client,
        ):
            manager = ChannelManager(
                runtime.store,
                runtime.bus,
                original.config,
                original.submit,
                app.state.attachments,
                client=channel_client,
                delivery_guard=original.delivery_guard,
                admission=runtime.maintenance.admission,
            )
            await original.close()
            app.state.channel_application.manager = manager
            monkeypatch.setattr(
                app.state.local_services,
                "media",
                lambda: MediaService(
                    MediaConfig(
                        asr=ServiceConfig(
                            model="offline-asr",
                            base_url="https://asr.invalid/v1",
                            response_format="json",
                        )
                    ),
                    client=asr_client,
                ),
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
                ) as web,
            ):
                for name, data, mime in (
                    ("web.txt", web_secret.encode(), "text/plain"),
                    ("voice.wav", b"RIFF\x10\0\0\0WAVEfmt ", "audio/wav"),
                ):
                    response = await web.post(
                        "/api/uploads",
                        content=data,
                        headers={"x-file-name": name, "content-type": mime},
                    )
                    assert response.status_code == 200
                    initial_ids.append(response.json()["id"])
                session_id = runtime.main_session()["id"]
                assigned = await web.post(
                    f"/api/sessions/{session_id}/messages",
                    json={"content": "网页任务，交给队友读取资料", "attachment_ids": initial_ids},
                )
                assert assigned.status_code == 200
                await asyncio.wait_for(asr_entered.wait(), 3)
                await eventually(
                    runtime,
                    lambda: any(
                        event["type"] == "tool.started"
                        and event["data"].get("name") == "wait_agents"
                        for event in runtime.store.events()
                    ),
                )
                payload = onebot_event()
                payload["message"] = [
                    {"type": "text", "data": {"text": "给网页原任务补充资料"}},
                    {
                        "type": "file",
                        "data": {"name": "mobile.txt", "url": "https://93.184.216.34/mobile.txt"},
                    },
                ]
                body, headers = onebot_body(payload)
                headers["Content-Type"] = "application/json"
                for _ in range(2):
                    response = await web.post(
                        "/api/channels/onebot/webhook", content=body, headers=headers
                    )
                    assert response.status_code == 200
                await eventually(runtime, lambda: bool(completed(runtime, "移动补充已交给原队友")))
                agent = runtime.team.get("资料队友")
                assert worker_round == 1 and not asr_release.is_set()
                pending = runtime.conversation.pending(agent["id"])
                assert len(pending) == 1 and pending[0]["attachment_ids"] == [mobile_id]
                assert pending[0]["principal"] == "agent:" + session_id
                assert pending[0]["source"] == "team:" + session_id
                asr_release.set()
                await eventually(runtime, lambda: bool(completed(runtime, "网页任务已完成")))
                await consumed(manager, runtime.store.events()[-1]["id"])
                assert len(asr_requests) == len(downloads) == 1
                assert manager.deliveries() == [] and sent == []
                assert completed(runtime, "网页任务已完成")[0]["delivery_routes"] == []
                assert (
                    completed(runtime, "移动补充已交给原队友")[0]["delivery_routes"][0]["target"]
                    == "7"
                )
                assert agent["reply_origin"]["kind"] == "web"
                assert agent["reply_origin"]["input_id"] == assigned.json()["message"]["id"]
                assert (
                    len(
                        [
                            item
                            for item in runtime.store.messages(agent["id"])
                            if item.get("attachment_ids") == [mobile_id]
                        ]
                    )
                    == 1
                )
    finally:
        asr_release.set()
        await runtime.close()


async def test_pending_handoff_reopens_same_worker_and_recovers_compacted_references(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "models": {"worker": {"model": "worker", "base_url": "https://unused.invalid/v1"}},
        }
    )
    await runtime.reconfigure()
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", runtime.config)
    LocalServices(runtime, attachments)

    async def save(text: str) -> dict[str, Any]:
        async def chunks() -> AsyncIterator[bytes]:
            yield text.encode()

        return await attachments.save(chunks(), "资料.txt", "text/plain")

    original = await save("ORIGINAL-ARCHIVED-BODY")
    supplement = await save("PERSISTED-SUPPLEMENT-BODY")
    first_release, first_entered = asyncio.Event(), asyncio.Event()
    main_turns = [
        ModelTurn(
            tool_calls=[
                call(
                    "spawn",
                    "spawn_agent",
                    task_name="持久队友",
                    prompt="保存本任务资料",
                    attachment_ids=[original["id"]],
                )
            ]
        ),
        ModelTurn(content="任务已派出"),
        ModelTurn(content="首轮结果已收"),
    ]

    class FirstWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert original["id"] in str(messages)
            assert "ORIGINAL-ARCHIVED-BODY" not in str(messages)
            first_entered.set()
            await first_release.wait()
            yield StreamEvent("done", turn=ModelTurn(content="首轮已完成"))

    def first_factory(config: ModelConfig) -> FakeClient:
        return FirstWorker([], []) if config.model == "worker" else FakeClient(main_turns, [])

    runtime.client_factory = first_factory  # type: ignore[assignment]
    try:
        assigned = await runtime.submit("网页派工", attachment_ids=[original["id"]])
        await asyncio.wait_for(first_entered.wait(), 3)
        assert await runtime.wait(assigned["run_id"]) == "任务已派出"
        first_release.set()
        await eventually(runtime, lambda: bool(completed(runtime, "首轮结果已收")))
        await runtime.wait(runtime.run_ids[runtime.main_session()["id"]])
        agent = runtime.team.get("持久队友")
        original_origin = agent["reply_origin"]
        first_message = runtime.store.messages(agent["id"])[0]
        assert first_message["attachment_ids"] == [original["id"]]
        # A semantic summary deliberately drops IDs; durable originals stay intact.
        for number in range(24):
            runtime.store.add_message(agent["id"], "user", f"后续资料{number}" + "有限历史" * 40)
            runtime.store.add_message(agent["id"], "assistant", f"已核查{number}" + "结果" * 40)

        async def summarize(source: str, max_tokens: int) -> str:
            return f"先前资料仍在原消息 seq={first_message['seq']}，需要时按原 seq 查证。"

        compactor = Compactor(
            runtime.store,
            summarize,
            CompactionSettings,
            max_chars=lambda: 4000,
            max_jobs=lambda: 1,
            fingerprint=lambda: "fixed-offline",
        )
        try:
            compactor.observe(agent["id"])
            await compactor.finish(agent["id"])
            assert runtime.store.get("compaction", agent["id"]) is not None
            projected = compactor.project(agent["id"])
            assert projected[0]["compaction_summary"]
            assert original["id"] not in str(projected)
            assert runtime.store.get("message", first_message["id"])["attachment_ids"] == [
                original["id"]
            ]
        finally:
            await compactor.close()
        main_turns.extend(
            [
                ModelTurn(
                    tool_calls=[
                        call(
                            "defer",
                            "send_input",
                            target=agent["id"],
                            message=f"继续同一任务，先按原 seq={first_message['seq']} 核查，再读补充",
                            attachment_ids=[supplement["id"]],
                            wake=False,
                        )
                    ]
                ),
                ModelTurn(content="补充已保存在原邮箱"),
            ]
        )
        deferred = await runtime.submit("补充资料，暂不唤醒队友", attachment_ids=[supplement["id"]])
        assert await runtime.wait(deferred["run_id"]) == "补充已保存在原邮箱"
        pending = runtime.conversation.pending(agent["id"])
        assert len(pending) == 1 and pending[0]["attachment_ids"] == [supplement["id"]]
        pending_id = pending[0]["id"]
        assert runtime.team.get(agent["id"])["status"] == "completed"
    finally:
        first_release.set()
        await runtime.close()

    reopened, _ = make_runtime(tmp_path, [])
    LocalServices(
        reopened, Attachments(reopened.store, reopened.data_dir / "attachments", reopened.config)
    )
    resume_release, resume_entered = asyncio.Event(), asyncio.Event()
    worker_round = 0
    seen: list[list[Message]] = []
    reopened_turns = [
        ModelTurn(tool_calls=[call("resume", "resume_agent", target=agent["id"])]),
        ModelTurn(content="已恢复原队友"),
        ModelTurn(content="恢复任务已完成"),
    ]

    class ResumedWorker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal worker_round
            worker_round += 1
            seen.append(list(messages))
            if worker_round == 1:
                assert original["id"] not in str(messages)
                assert supplement["id"] in str(messages)
                assert "PERSISTED-SUPPLEMENT-BODY" not in str(messages)
                resume_entered.set()
                await resume_release.wait()
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "history",
                            "conversation_read",
                            session_id=agent["id"],
                            seq=first_message["seq"],
                        )
                    ]
                )
            elif worker_round == 2:
                record = tool_result(messages, "history")
                assert record["attachment_ids"] == [original["id"]]
                assert record["seq"] == first_message["seq"]
                turn = ModelTurn(
                    tool_calls=[
                        call("old-file", "attachment_read", id=record["attachment_ids"][0]),
                        call("new-file", "attachment_read", id=supplement["id"]),
                    ]
                )
            else:
                assert worker_round == 3
                assert "ORIGINAL-ARCHIVED-BODY" in tool_result(messages, "old-file")["content"]
                assert "PERSISTED-SUPPLEMENT-BODY" in tool_result(messages, "new-file")["content"]
                turn = ModelTurn(content="压缩前资料与重启补充均已读取")
            yield StreamEvent("done", turn=turn)

    def resumed_factory(config: ModelConfig) -> FakeClient:
        return ResumedWorker([], []) if config.model == "worker" else FakeClient(reopened_turns, [])

    reopened.client_factory = resumed_factory  # type: ignore[assignment]
    try:
        assert reopened.main_session()["id"] == assigned["session_id"]
        assert reopened.team.get(agent["id"])["reply_origin"] == original_origin
        assert reopened.conversation.pending(agent["id"])[0]["id"] == pending_id
        resumed = await reopened.submit("恢复原任务")
        await asyncio.wait_for(resume_entered.wait(), 3)
        assert await reopened.wait(resumed["run_id"]) == "已恢复原队友"
        resume_release.set()
        await eventually(reopened, lambda: bool(completed(reopened, "恢复任务已完成")))
        assert worker_round == 3 and len(seen) == 3
        restored = reopened.team.get(agent["id"])
        assert restored["id"] == agent["id"] and restored["generation"] == agent["generation"] + 1
        assert restored["reply_origin"] == original_origin
        assert completed(reopened, "恢复任务已完成")[0]["delivery_routes"] == []
        assert reopened.conversation.pending(agent["id"]) == []
        assert (
            len([item for item in reopened.store.messages(agent["id"]) if item["id"] == pending_id])
            == 1
        )
    finally:
        resume_release.set()
        await reopened.close()


@pytest.mark.parametrize("main_vision", [False, True])
async def test_handed_off_image_uses_explicit_vision_tool_without_implicit_image_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_vision: bool
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "models": {
                "main": {"vision": main_vision},
                "worker": {"model": "worker", "base_url": "https://unused.invalid/v1"},
                "vision": {"model": "vision", "base_url": "https://vision.invalid/v1"},
            },
        }
    )
    await runtime.reconfigure()
    attachments = Attachments(runtime.store, runtime.data_dir / "attachments", runtime.config)
    services = LocalServices(runtime, attachments)
    image = b"\x89PNG\r\n\x1a\n" + b"bounded-offline-image-fixture"

    async def chunks() -> AsyncIterator[bytes]:
        yield image

    item = await attachments.save(chunks(), "diagram.png", "image/png")
    release, entered = asyncio.Event(), asyncio.Event()
    seen: list[list[Message]] = []
    requests: list[dict[str, Any]] = []
    turns = [
        ModelTurn(
            tool_calls=[
                call(
                    "spawn",
                    "spawn_agent",
                    task_name="看图队友",
                    prompt="按需核查图示",
                    attachment_ids=[item["id"]],
                )
            ]
        ),
        ModelTurn(content="看图任务已派出"),
        ModelTurn(content="图示结果已收"),
    ]
    rounds = 0

    class Worker(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal rounds
            rounds += 1
            seen.append(list(messages))
            assert all(not message.images for message in messages)
            if rounds == 1:
                assert item["id"] in str(messages)
                assert base64.b64encode(image).decode() not in str(messages)
                assert "VISION-ONLY-EVIDENCE" not in str(messages)
                entered.set()
                await release.wait()
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "vision",
                            "image_describe",
                            id=item["id"],
                            prompt="提取本任务所需的图示证据",
                        )
                    ]
                )
            else:
                assert rounds == 2
                assert tool_result(messages, "vision")["description"] == "VISION-ONLY-EVIDENCE"
                turn = ModelTurn(content="图示已经显式读取")
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        return Worker([], []) if config.model == "worker" else FakeClient(turns, [])

    def vision_http(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == ("main" if main_vision else "vision")
        assert (
            body["messages"][0]["content"][1]["image_url"]["url"]
            == "data:image/png;base64," + base64.b64encode(image).decode()
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "VISION-ONLY-EVIDENCE"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    runtime.client_factory = factory  # type: ignore[assignment]
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(vision_http)) as client:
            monkeypatch.setattr(
                services,
                "media",
                lambda: MediaService(
                    MediaConfig(vision=runtime.model_config("vision")),
                    main_model=runtime.model_config("main"),
                    client=client,
                ),
            )
            assigned = await runtime.submit("网页图示交给队友", attachment_ids=[item["id"]])
            await asyncio.wait_for(entered.wait(), 3)
            assert await runtime.wait(assigned["run_id"]) == "看图任务已派出"
            assert requests == []
            release.set()
            await eventually(runtime, lambda: bool(completed(runtime, "图示结果已收")))
            assert len(requests) == 1 and rounds == 2
            assert runtime.team.get("看图队友")["reply_origin"]["kind"] == "web"
    finally:
        release.set()
        await runtime.close()
