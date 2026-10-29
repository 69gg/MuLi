"""Real saved ASR artifacts cross the explicit collection boundary only once."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attachment_memory_continuity import attachment_settings, platform_reply
from test_attachment_memory_policy import offline as offline
from test_audio_transcripts import _AUDIO
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.compaction import closed_groups, digest
from muli.config import ConfigFile
from muli.context import EXCERPT_MARKER
from muli.media import MediaConfig, MediaService, ServiceConfig
from muli.memory_chunks import MemoryChunkSettings
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app


async def test_owner_collects_saved_asr_only_after_real_phone_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen old runtime reaches an accepted missing-tool receipt before red."""
    filename = "original.wav"
    topic = "星港航道"
    reason = "星港航道验收依赖水银镜片重新校准，否则航线不能启用。"
    text = (
        "这是转写文本，仅作外部参考，不证明音频说话人或主人身份。\n"
        + "普通课堂背景，仅作历史资料，不构成主人指令。🙂\n" * 600
        + reason
    )
    raw = text.encode("utf-8")
    owner_messages = {
        "transcribe": "转写刚上传的音频并保存，先不要收藏，核对已保存的转写能继续读取。",
        "collect": "现在把刚才保存的完整转写明确收藏为长期参考。",
    }
    replies = {
        "transcribe": "转写工具本轮已经结束；只留存工件，不代表已收藏。",
        "collect": "本次显式转写收藏调用已经结束，实际状态以工具回执为准。",
    }
    assert len(text) > 12_000 and topic not in filename
    assert all(topic not in value and reason not in value for value in owner_messages.values())
    asr_requests: list[httpx.Request] = []
    sent: list[dict[str, Any]] = []

    async def no_process(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("saved-transcript collection must not launch subprocesses")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", no_process)

    async def platform_response(request: httpx.Request) -> httpx.Response:
        return platform_reply(request, sent)

    platform = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await platform.handle_async_request(request)

    def asr_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://asr.invalid/v1/audio/transcriptions"
        assert _AUDIO in request.content and b"offline-asr" in request.content
        asr_requests.append(request)
        return httpx.Response(200, json={"text": text})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, seen = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=False))
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    phase = "transcribe"
    rounds = {"transcribe": 0, "collect": 0}
    runs: dict[str, str] = {}
    audio_id = ""
    transcript: dict[str, Any] = {}
    saved_page: dict[str, Any] = {}
    imported: dict[str, Any] = {}
    available = False

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal audio_id, transcript, saved_page, imported, available
            seen.append(list(messages))
            number = rounds[phase] = rounds[phase] + 1
            assert number <= (4 if phase == "transcribe" else 2)
            assert runs.setdefault(phase, runtime.run_ids[main_id]) == runtime.run_ids[main_id]
            if phase == "transcribe":
                if number == 1:
                    incoming = next(
                        message
                        for message in messages
                        if message.role == "user"
                        and message.content.endswith(owner_messages[phase])
                    )
                    header, content = incoming.content.split("\n", 1)
                    assert header.startswith(
                        "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                    )
                    origin = json.loads(header.split("：", 1)[1])
                    assert origin == {
                        "channel": "onebot",
                        "account_id": "123",
                        "sender_id": "7",
                        "target": "7",
                        "kind": "private",
                        "input_id": incoming.source_message_id,
                    }
                    assert content == owner_messages[phase]
                    turn = ModelTurn(
                        tool_calls=[call("asr-catalog", "attachment_search", name_query=filename)]
                    )
                elif number == 2:
                    page = tool_result(messages, "asr-catalog")
                    assert len(page["items"]) == 1 and page["has_more"] is False
                    item = page["items"][0]
                    assert item["name"] == filename and item["size"] == len(_AUDIO)
                    audio_id = item["id"]
                    turn = ModelTurn(
                        tool_calls=[call("asr-completed", "audio_transcribe", id=audio_id)]
                    )
                elif number == 3:
                    transcript = tool_result(messages, "asr-completed")
                    assert transcript["asr_completed"] is True
                    assert transcript["id"] == audio_id and transcript["has_more"]
                    assert topic not in transcript["text"] and reason not in transcript["text"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "asr-saved-read",
                                "audio_transcript_read",
                                id=transcript["transcript_id"],
                                revision=transcript["revision"],
                                offset_bytes=transcript["next_offset_bytes"],
                            )
                        ]
                    )
                else:
                    saved_page = tool_result(messages, "asr-saved-read")
                    assert "error" not in saved_page
                    assert saved_page["revision"] == transcript["revision"]
                    assert saved_page["offset_bytes"] == transcript["next_offset_bytes"]
                    assert saved_page["next_offset_bytes"] > saved_page["offset_bytes"]
                    start, end = saved_page["offset_bytes"], saved_page["next_offset_bytes"]
                    assert saved_page["content"].encode("utf-8") == raw[start:end]
                    turn = ModelTurn(content=replies[phase])
            elif number == 1:
                available = "memory_import_transcript" in {
                    tool["function"]["name"] for tool in tools
                }
                # Deliberately invoke through the real runtime even when the
                # old catalog lacks it: only the actual receipt is the red gate.
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "collect-saved-asr",
                            "memory_import_transcript",
                            id=transcript["transcript_id"],
                            revision=transcript["revision"],
                        )
                    ]
                )
            else:
                imported = tool_result(messages, "collect-saved-asr")
                turn = ModelTurn(content=replies[phase])
            yield StreamEvent("done", turn=turn)

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model in {"main", "reviewer"}
        return Main([], seen) if profile.model == "main" else FakeClient([], [])

    async def finish() -> dict[str, Any]:
        await eventually(runtime, lambda: bool(completed(runtime, replies[phase])))
        assert await asyncio.wait_for(runtime.wait(runs[phase]), 3) == replies[phase]
        run = runtime.store.get("run", runs[phase])
        assert run is not None and run["status"] == "completed"
        matches = completed(runtime, replies[phase])
        assert len(matches) == 1
        return matches[0]

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(asr_response)) as asr:
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
                    client=asr,
                ),
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
                ) as web,
            ):
                mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
                assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
                uploaded = await web.post(
                    "/api/uploads",
                    content=_AUDIO,
                    headers={"x-file-name": filename, "content-type": "audio/wav"},
                )
                assert uploaded.status_code == 200
                uploaded_id = uploaded.json()["id"]
                assert app.state.attachments.path(uploaded_id).read_bytes() == _AUDIO
                assert (await phone(web, owner_messages[phase], 27101)).status_code == 200
                transcribed = await finish()
                assert audio_id == uploaded_id and len(asr_requests) == 1 and saved_page
                transcript_id = transcript["transcript_id"]
                source_id = "attachment:" + transcript_id
                artifact = app.state.attachments.get(transcript_id)
                assert artifact["source"] == "asr" and artifact["size"] == len(raw)
                assert artifact["sha256"] == transcript["revision"]
                assert app.state.attachments.path(transcript_id).read_bytes() == raw
                stored_asr = next(
                    item
                    for item in runtime.store.messages(main_id)
                    if item.get("tool_call_id") == "asr-completed"
                )
                assert stored_asr["content_provenance"] == "accepted"
                assert json.loads(stored_asr["content"]) == transcript
                application = app.state.memory_application
                assert runtime.memory.source(source_id) is None
                assert application.pipeline.job(source_id) is None
                assert (
                    runtime.store.db.execute(
                        "SELECT count(*) FROM memory_meta WHERE source_id=?", (source_id,)
                    ).fetchone()[0]
                    == 0
                )
                manager = app.state.channel_application.manager
                await processed_reply(runtime, manager, transcribed["id"])
                assert not any(
                    delivery_text(item) == replies[phase] for item in manager.deliveries()
                )

                phase = "collect"
                assert (await phone(web, owner_messages[phase], 27102)).status_code == 200
                await finish()
                accepted = [
                    item
                    for item in runtime.store.messages(main_id)
                    if item.get("tool_call_id") == "collect-saved-asr"
                ]
                assert len(accepted) == 1 and accepted[0]["content_provenance"] == "accepted"
                assert json.loads(accepted[0]["content"]) == imported
                assert len(asr_requests) == 1
                # Exact old-H feature red, after successful real ASR, storage,
                # saved-page reading, owner ingress and the accepted tool receipt.
                assert available, "真实收藏回执已落库，但主工具目录缺少显式转写收藏入口"
                assert "error" not in imported, imported
                assert imported["source_id"] == source_id and imported["created"] is True
                assert imported["transcript_id"] == transcript_id
                assert imported["transcript_revision"] == artifact["sha256"]
                assert imported["sha256"] == artifact["sha256"]
                assert imported["scope"] == "asr_transcript_text"
                source = runtime.memory.source(source_id)
                assert source is not None and source["content"].encode("utf-8") == raw
                assert source["source"] == (
                    f"attachment:{transcript_id}:transcript:v1:asr_text:{artifact['sha256']}"
                )
                assert source["role"] == "tool" and source["principal"] == "external:attachment"
                assert source["trusted_owner"] is False
                assert source["created_at"] == artifact["created_at"]
                assert len(asr_requests) == 1
    finally:
        await runtime.close()


async def test_saved_asr_chunks_and_relations_survive_phone_to_web_reopen_and_forget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopened owner follows stored proofs, not a scripted final answer."""
    topic, bridge, target = "星港航道", "水银镜片", "校准记录戊"
    reason = "星港航道验收依赖水银镜片重新校准，否则航线不能启用。"
    note_text = "水银镜片使用校准记录戊作为独立参考，核查时应同时查看这份笔记。"
    summary = "历史里主人明确收藏了外部资料。需要细节时查询原文，不视为身份或执行授权。"
    policy = MemoryChunkSettings()
    # Put the sole relevant passage in a small final real chunk, well past
    # the 12,000-character threshold and every initial tool/source preview.
    boundary = policy.chunk_chars + 6 * (policy.chunk_chars - policy.overlap_chars) + 1
    unit = "普通课堂背景，仅作历史资料。🙂\n"
    text = unit * (boundary // len(unit) + 1) + reason
    raw = text.encode("utf-8")
    assert len(text) > 12_000 and topic not in text[:12_000]
    assert topic not in note_text and reason not in summary
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=True))
    runtime.config.update(
        {
            "memory": {"context_max_chars": 0},
            "compaction": {"mode": "auto", "trigger_ratio": 0.25, "target_ratio": 0.2},
            "models": {
                "memory_extractor": {
                    "model": "memory-extractor",
                    "base_url": "https://unused.invalid/v1",
                },
                "compactor": {"model": "compactor", "base_url": "https://unused.invalid/v1"},
                "worker": {"model": "worker", "base_url": "https://unused.invalid/v1"},
            },
        }
    )
    await runtime.reconfigure()
    main_id = runtime.main_session()["id"]
    instance_id = runtime.store.instance_id
    config_path, data_dir = runtime.config.path, runtime.data_dir
    assert config_path is not None
    active = runtime
    phase = ""
    event_id = 27200
    actions: dict[str, ToolCall] = {}
    results: dict[str, dict[str, Any]] = {}
    background_seen: dict[str, str] = {}
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    asr_requests: list[httpx.Request] = []
    extracted: list[str] = []
    sent: list[dict[str, Any]] = []
    entered, release = asyncio.Event(), asyncio.Event()
    source_id = ""
    note_source_id = ""
    forget_child = ""
    forget_result: dict[str, Any] = {}
    forget_ack = "已收到遗忘队友的完成回报。"

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return platform_reply(request, sent)

    def asr_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://asr.invalid/v1/audio/transcriptions"
        assert _AUDIO in request.content
        asr_requests.append(request)
        return httpx.Response(200, json={"text": text})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    async def settle_query_inputs() -> None:
        rows = active.store.db.execute(
            "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry') "
            "ORDER BY source_id LIMIT ?",
            (active.config.settings.memory.max_pending_jobs,),
        ).fetchall()
        for row in rows:
            done = await asyncio.wait_for(application.pipeline.wait(row["source_id"]), 4)
            assert done is not None and done["state"] == "done"

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role
            self.round = 0

        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            if self.role == "reviewer":
                return await super().complete(messages, tools)
            assert not tools
            if self.role == "compactor":
                return ModelTurn(content=summary)
            assert self.role == "memory-extractor"
            source = json.loads(messages[-1].content)
            memories: list[dict[str, Any]] = []
            if source["id"] in {source_id, note_source_id}:
                extracted.append(source["id"])
                assert source["principal"] == "external:attachment"
                assert source["role"] == "tool" and source["trusted_owner"] is False
                is_asr = source["id"] == source_id
                assert source["content"] == (text if is_asr else note_text)
                if is_asr:
                    assert ":transcript:v1:asr_text:" in source["source"]
                    entered.set()
                    await asyncio.wait_for(release.wait(), 4)
                memories.append(
                    {
                        "layer": "semantic",
                        "content": reason if is_asr else note_text,
                        "quote": reason if is_asr else note_text,
                        "entities": [topic, bridge] if is_asr else [bridge, target],
                        "relations": [
                            {
                                "subject": topic if is_asr else bridge,
                                "predicate": "依赖" if is_asr else "使用",
                                "object": bridge if is_asr else target,
                            }
                        ],
                        "confidence": 0.9,
                    }
                )
            return ModelTurn(
                content=json.dumps({"memories": memories, "aliases": {}}, ensure_ascii=False)
            )

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal forget_child, forget_result
            self.round += 1
            if self.role == "worker":
                assert phase == "forget-transcript" and fact_id
                if self.round == 1:
                    assert any(fact_id in item.content for item in messages if item.role == "user")
                    assert "memory_forget" in {item["function"]["name"] for item in tools}
                    turn = ModelTurn(
                        tool_calls=[call("worker-forget-transcript", "memory_forget", id=fact_id)]
                    )
                else:
                    assert self.round == 2
                    forget_result = tool_result(messages, "worker-forget-transcript")
                    assert forget_result == {"deleted": True}
                    turn = ModelTurn(content="指定转写来源已按回执遗忘，原附件工件保留。")
                yield StreamEvent("done", turn=turn)
                return
            assert self.role == "main"
            latest = next(
                item for item in reversed(active.store.messages(main_id)) if item["role"] == "user"
            )
            source = latest.get("source", "")
            if (
                (self.round == 1 or (phase == "forget-transcript" and self.round > 3))
                and isinstance(source, str)
                and source.startswith("team:")
                and latest.get("principal") == "agent:" + source[5:]
            ):
                # Real completion reports can arrive after the owner's third
                # round, in this driver or a new one. Acknowledge their actual
                # host identity without spawning or forgetting a second time.
                yield StreamEvent("done", turn=ModelTurn(content=forget_ack))
                return
            number = rounds[phase] = rounds.get(phase, 0) + 1
            assert number <= (3 if phase == "forget-transcript" else 2)
            assert runs.setdefault(phase, active.run_ids[main_id]) == active.run_ids[main_id]
            if phase == "forget-transcript":
                assert "memory_forget" not in {item["function"]["name"] for item in tools}
                if number == 1:
                    action = actions[phase]
                    assert action.name == "spawn_agent"
                    assert action.name in {item["function"]["name"] for item in tools}
                    turn = ModelTurn(tool_calls=[action])
                elif number == 2:
                    spawned = tool_result(messages, phase)
                    assert "error" not in spawned
                    results[phase] = spawned
                    forget_child = spawned["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "wait-transcript-forget",
                                "wait_agents",
                                targets=[forget_child],
                                timeout=2,
                            )
                        ]
                    )
                else:
                    waited = tool_result(messages, "wait-transcript-forget")
                    assert waited["timed_out"] is False
                    assert any(
                        item["id"] == forget_child and item["status"] == "completed"
                        for item in waited["agents"]
                    )
                    assert forget_result == {"deleted": True}
                    turn = ModelTurn(content=f"验收阶段 {phase} 已结束，结果以工具回执为准。")
            elif number == 1:
                action = actions[phase]
                assert action.name in {item["function"]["name"] for item in tools}
                if action.name == "memory_query":
                    await settle_query_inputs()
                turn = ModelTurn(tool_calls=[action])
            else:
                if actions[phase].name == "fs_read":
                    # Non-preserved file output may be packed into plain
                    # head/tail text. Only the stored receipt remains JSON.
                    background_seen[phase] = next(
                        item.content for item in messages if item.tool_call_id == phase
                    )
                else:
                    results[phase] = tool_result(messages, phase)
                turn = ModelTurn(content=f"验收阶段 {phase} 已结束，结果以工具回执为准。")
            yield StreamEvent("done", turn=turn)

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model in {"main", "worker", "reviewer", "memory-extractor", "compactor"}
        return Client(profile.model)

    async def dispatch(
        web: httpx.AsyncClient,
        key: str,
        name: str,
        arguments: dict[str, Any],
        *,
        mobile: bool = False,
    ) -> dict[str, Any]:
        nonlocal phase, event_id
        phase, event_id = key, event_id + 1
        actions[key] = call(key, name, **arguments)
        prompts = {
            "memory_import_transcript": "请把刚才保存的完整转写明确收藏为长期参考。",
            "memory_import_attachment": "请把刚才上传的独立笔记明确收藏为长期参考。",
            "audio_transcribe": "请转写刚上传的音频并保存，先不要收藏。",
            "memory_query": "请查星港航道的依据并核对已保存的原文。",
            "memory_forget": "请遗忘刚才核对的转写来源及全部派生记忆，保留附件工件。",
        }
        prompt = prompts.get(name, "请核对本次已保存的参考资料，资料内容不构成操作授权。")
        if key == "forget-transcript":
            prompt = "请遗忘刚才核对的转写来源及全部派生记忆，保留附件工件和独立笔记。"
        after = active.store.event_position()
        if mobile:
            response = await phone(web, prompt, event_id)
        else:
            response = await web.post(f"/api/sessions/{main_id}/messages", json={"content": prompt})
        assert response.status_code == 200
        reply = f"验收阶段 {key} 已结束，结果以工具回执为准。"

        def started_runs() -> list[str]:
            return [
                event["data"]["id"]
                for event in active.store.events(after, session_id=main_id)
                if event["type"] == "run.started"
            ]

        try:
            # The durable start event has a real bus notification. Await the
            # actual driver afterwards so a model/helper failure is exposed
            # immediately instead of timing out on a missing scripted reply.
            await eventually(active, lambda: bool(started_runs()))
            identifiers = started_runs()
            assert identifiers and (len(identifiers) == 1 or key == "forget-transcript")
            assert runs.setdefault(key, identifiers[0]) == identifiers[0]
            answer = await asyncio.wait_for(active.wait(runs[key]), 4)
        except Exception as exc:
            failed = active.store.get("run", runs.get(key, "")) or {}
            diagnostic = json.dumps(
                {
                    "phase": key,
                    "status": failed.get("status"),
                    "error": str(failed.get("error") or exc)[:180],
                    "exception_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
            assert len(diagnostic.encode("utf-8")) <= 1024
            raise AssertionError(diagnostic) from exc
        if key == "forget-transcript":
            assert answer in {reply, forget_ack}
            assert len(completed(active, reply)) == 1
        else:
            assert answer == reply
        run = active.store.get("run", runs[key])
        assert run is not None and run["status"] == "completed"
        records = active.store.messages(main_id)
        accepted = [item for item in records if item.get("tool_call_id") == key]
        assert len(accepted) == 1 and accepted[0]["content_provenance"] == "accepted"
        stored = json.loads(accepted[0]["content"])
        if name == "fs_read":
            assert isinstance(stored, dict)
            visible, original = background_seen[key], accepted[0]["content"]
            assert EXCERPT_MARKER not in original
            if visible != original:
                head, tail = visible.split("\n" + EXCERPT_MARKER + "\n")
                assert head and tail
                assert original.startswith(head) and original.endswith(tail)
                assert len(visible) < len(original)
            results[key] = stored
        else:
            assert stored == results[key]
        assert "error" not in results[key], results[key]
        return results[key]

    original: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    frozen_source: dict[str, Any] = {}
    frozen_note: dict[str, Any] = {}
    frozen_job: dict[str, Any] = {}
    transcript: dict[str, Any] = {}
    artifact_id = ""
    note_id = ""
    fact_id = ""
    async with httpx.AsyncClient(transport=httpx.MockTransport(asr_response)) as asr:
        for stage in ("initial", "recall", "forgotten"):
            if stage != "initial":
                active = Runtime(ConfigFile(config_path), data_dir, client_factory=factory)  # type: ignore[arg-type]
            active.client_factory = factory  # type: ignore[assignment]
            app = create_app(active.config, active.data_dir, active)
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
                    client=asr,
                ),
            )
            try:
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
                    ) as web,
                ):
                    application = app.state.memory_application
                    assert active.store.instance_id == instance_id
                    assert active.main_session()["id"] == main_id
                    assert application._recovery is not None
                    await asyncio.wait_for(asyncio.shield(application._recovery), 4)
                    if stage == "initial":
                        mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
                        assert mode.status_code == 200
                        assert application.pipeline.source_chunks.settings.read() == policy
                        uploaded = await web.post(
                            "/api/uploads",
                            content=_AUDIO,
                            headers={"x-file-name": "original.wav", "content-type": "audio/wav"},
                        )
                        assert uploaded.status_code == 200
                        audio_id = uploaded.json()["id"]
                        catalog = await dispatch(
                            web,
                            "audio-catalog",
                            "attachment_search",
                            {"name_query": "original.wav"},
                            mobile=True,
                        )
                        assert len(catalog["items"]) == 1
                        assert catalog["items"][0]["id"] == audio_id
                        transcript = await dispatch(
                            web,
                            "transcription",
                            "audio_transcribe",
                            {"id": catalog["items"][0]["id"]},
                            mobile=True,
                        )
                        assert transcript["asr_completed"] is True and transcript["has_more"]
                        assert topic not in transcript["text"] and len(asr_requests) == 1
                        artifact_id = transcript["transcript_id"]
                        source_id = "attachment:" + artifact_id
                        assert app.state.attachments.path(artifact_id).read_bytes() == raw
                        assert active.memory.source(source_id) is None
                        assert application.pipeline.job(source_id) is None
                        # The historical transcript remains useful without its
                        # original recording, but proves only the saved text.
                        app.state.attachments.path(audio_id).unlink()
                        note = await web.post(
                            "/api/uploads",
                            content=note_text.encode("utf-8"),
                            headers={"x-file-name": "note.txt", "content-type": "text/plain"},
                        )
                        assert note.status_code == 200
                        note_id = note.json()["id"]
                        note_source_id = "attachment:" + note_id
                        await dispatch(
                            web,
                            "collect-note",
                            "memory_import_attachment",
                            {"id": note_id},
                            mobile=True,
                        )
                        collected = await dispatch(
                            web,
                            "collect-transcript",
                            "memory_import_transcript",
                            {"id": artifact_id, "revision": transcript["revision"]},
                            mobile=True,
                        )
                        assert collected["source_id"] == source_id and collected["created"]
                        await asyncio.wait_for(entered.wait(), 3)
                        assert not release.is_set()
                        assert application.pipeline.job(source_id)["state"] == "processing"
                        release.set()
                        done = await asyncio.wait_for(application.pipeline.wait(source_id), 4)
                        assert done is not None and done["state"] == "done"
                        frozen_job = deepcopy(done)
                        source = active.memory.source(source_id)
                        assert source is not None and source["content"] == text
                        assert source["trusted_owner"] is False and source["role"] == "tool"
                        assert source["source"] == (
                            f"attachment:{artifact_id}:transcript:v1:asr_text:{transcript['revision']}"
                        )
                        frozen_source = deepcopy(source)
                        note_source = active.memory.source(note_source_id)
                        assert note_source is not None and note_source["content"] == note_text
                        frozen_note = deepcopy(note_source)
                        chunk_ids = done["checkpoint"]["source_chunks"]["memory_ids"]
                        assert len(chunk_ids) > 4
                        assert reason in active.memory.get(chunk_ids[-1])["content"]
                        assert extracted.count(source_id) == extracted.count(note_source_id) == 1
                        background = tmp_path / "background.txt"
                        threshold = int(
                            active.config.settings.limits.max_context_chars
                            * active.config.settings.compaction.trigger_ratio
                        )
                        unit_background = "普通无关背景，只读并核对已有结果，不新增主人偏好。\n"
                        background_text = unit_background * (threshold // len(unit_background) + 20)
                        background.write_text(background_text, encoding="utf-8")
                        background_result = await dispatch(
                            web, "background", "fs_read", {"path": str(background)}, mobile=True
                        )
                        assert background_result == {
                            "path": str(background),
                            "content": background_text,
                        }
                        original = active.store.messages(main_id)
                        groups = closed_groups(original)
                        assert sum(map(len, groups)) == len(original)
                        background_record = next(
                            item for item in original if item.get("tool_call_id") == "background"
                        )
                        assert original[-1]["role"] == "assistant"
                        assert not original[-1].get("tool_calls")
                        assert original[-1]["seq"] > background_record["seq"]
                        accepted = next(
                            item
                            for item in original
                            if item.get("tool_call_id") == "collect-transcript"
                        )
                        active.config.update({"compaction": {"enabled": True}})
                        compacted: dict[str, Any] | None = None
                        cuts: list[int] = []
                        # Each adopted snapshot cuts an earliest complete
                        # prefix at max_context * (1 - target_ratio). The long
                        # ASR receipt can fill that prefix before collection;
                        # consume the already-completed background through
                        # successive real snapshots, with a group-count bound.
                        for _ in groups:
                            previous = active.store.get("compaction", main_id) or {}
                            previous_cut = previous.get("covered_seq", 0)
                            active.compaction.observe(main_id)
                            await asyncio.wait_for(active.compaction.finish(main_id), 4)
                            compacted = active.store.get("compaction", main_id)
                            assert compacted is not None, {"cuts": cuts, "target": accepted["seq"]}
                            cuts.append(compacted["covered_seq"])
                            assert cuts[-1] > previous_cut, {
                                "cuts": cuts,
                                "target": accepted["seq"],
                            }
                            status = active.compaction.status(main_id)
                            assert status["running"] is False and status["ready"] is False, status
                            assert compacted["generation"] == previous.get("generation", 0) + 1
                            assert compacted["from_seq"] == previous_cut
                            assert compacted["parent_snapshot_id"] == previous.get("snapshot_id")
                            persisted = active.store.get(
                                "compaction_snapshot", compacted["snapshot_id"]
                            )
                            assert persisted is not None
                            assert persisted["covered_seq"] == compacted["covered_seq"]
                            adopted_prefix = [
                                item
                                for item in original
                                if previous_cut < item["seq"] <= compacted["covered_seq"]
                            ]
                            assert persisted["prefix_digest"] == digest(adopted_prefix)
                            assert active.store.messages(main_id) == original
                            if compacted["covered_seq"] >= accepted["seq"]:
                                break
                            remaining = active.store.message_prefix(
                                main_id, after=compacted["covered_seq"]
                            )
                            assert background_record["id"] in {item["id"] for item in remaining}
                            assert any(
                                item["role"] == "assistant"
                                and not item.get("tool_calls")
                                and item["seq"] > background_record["seq"]
                                for item in remaining
                            )
                        else:
                            pytest.fail(f"real compaction did not reach collection: cuts={cuts}")
                        assert compacted is not None and compacted["kind"] == "semantic"
                        assert compacted["summary"] == summary
                        snapshot = deepcopy(compacted)
                        assert snapshot["covered_seq"] >= accepted["seq"]
                        prefix = [
                            item
                            for item in original
                            if snapshot["from_seq"] < item["seq"] <= snapshot["covered_seq"]
                        ]
                        assert digest(prefix) == snapshot["prefix_digest"]
                        assert active.store.messages(main_id) == original
                        projected = active.compaction.project(main_id)
                        assert any(item.get("compaction_summary") for item in projected)
                        assert accepted["id"] not in {item["id"] for item in projected}
                        assert artifact_id not in str(projected) and reason not in str(projected)
                    elif stage == "recall":
                        assert active.store.get("compaction", main_id) == snapshot
                        assert active.store.messages(main_id) == original
                        assert active.memory.source(source_id) == frozen_source
                        assert active.memory.source(note_source_id) == frozen_note
                        assert application.pipeline.job(source_id) == frozen_job
                        located = await dispatch(
                            web, "locate-asr-receipt", "history_find", {"query": "asr_completed"}
                        )
                        assert len(located["items"]) == 1 and not located["has_more"]
                        hit = located["items"][0]
                        stored_asr = next(
                            item for item in original if item.get("tool_call_id") == "transcription"
                        )
                        assert hit["message_id"] == stored_asr["id"]
                        assert hit["role"] == "tool" and hit["content_provenance"] == "accepted"
                        archive_parts: list[str] = []
                        offset = 0
                        for index in range(8):
                            page = await dispatch(
                                web,
                                f"archive-page-{index}",
                                "conversation_read",
                                {
                                    **hit["read_arguments"],
                                    "offset_bytes": offset,
                                    "length_bytes": active.config.settings.limits.max_output_chars,
                                },
                            )
                            assert page["message_id"] == hit["message_id"]
                            assert page["revision"] == hit["revision"]
                            assert page["content_provenance"] == "accepted"
                            assert page["next_offset_bytes"] > offset
                            archive_parts.append(page["content"])
                            offset = page["next_offset_bytes"]
                            if not page["has_more"]:
                                break
                        else:
                            pytest.fail("accepted ASR receipt did not finish within bounded pages")
                        restored = json.loads("".join(archive_parts))
                        assert restored == transcript
                        artifact_parts: list[str] = []
                        offset = 0
                        for index in range(32):
                            page = await dispatch(
                                web,
                                f"artifact-page-{index}",
                                "audio_transcript_read",
                                {
                                    "id": restored["transcript_id"],
                                    "revision": restored["revision"],
                                    "offset_bytes": offset,
                                },
                            )
                            assert page["revision"] == restored["revision"]
                            assert page["offset_bytes"] == offset
                            assert page["next_offset_bytes"] > offset
                            artifact_parts.append(page["content"])
                            offset = page["next_offset_bytes"]
                            if not page["has_more"]:
                                break
                        else:
                            pytest.fail("saved ASR artifact did not finish within bounded pages")
                        assert "".join(artifact_parts).encode("utf-8") == raw
                        assert len(artifact_parts) >= 2 and offset == len(raw)
                        query = await dispatch(
                            web,
                            "query-relations",
                            "memory_query",
                            {"query": topic, "hops": 2, "limit": 4},
                        )
                        assert "memory_changed" not in query["degraded"]
                        candidate = next(
                            item
                            for item in query["memories"]
                            if item["source_id"] == note_source_id and item["layer"] == "semantic"
                        )
                        path = candidate["graph_via"]
                        assert path["complete"] and path["revision"] == query["revision"]
                        assert len(path["steps"]) == 2
                        first, second = path["steps"]
                        assert first["source_id"] == source_id
                        assert second["source_id"] == note_source_id
                        assert first["subject"] == topic and first["object"] == bridge
                        assert second["subject"] == bridge and second["object"] == target
                        assert first["to_id"] == second["from_id"]
                        for step in path["steps"]:
                            memory = active.memory.get(step["memory_id"])
                            proof_source = active.memory.source(step["source_id"])
                            assert memory is not None and proof_source is not None
                            assert memory["source_id"] == step["source_id"]
                            assert memory["quote"] in proof_source["content"]
                            assert memory["trusted_owner"] is False
                        fact_id = first["memory_id"]
                        reference = next(
                            item for item in query["sources"] if item["id"] == source_id
                        )
                        assert reason not in reference["content"]
                        source_parts: list[str] = []
                        offset = 0
                        for index in range(32):
                            page = await dispatch(
                                web,
                                f"source-page-{index}",
                                "memory_source",
                                {
                                    "id": reference["id"],
                                    "revision": reference["digest"],
                                    "offset_bytes": offset,
                                },
                            )
                            assert page["revision"] == reference["digest"]
                            assert page["source"] == frozen_source["source"]
                            assert page["trusted_owner"] is False and page["offset_bytes"] == offset
                            assert page["next_offset_bytes"] > offset
                            source_parts.append(page["content"])
                            offset = page["next_offset_bytes"]
                            if not page["has_more"]:
                                break
                        else:
                            pytest.fail("collected ASR source did not finish within bounded pages")
                        assert "".join(source_parts).encode("utf-8") == raw
                        assert len(source_parts) >= 2 and offset == len(raw)
                        latest_owner = next(
                            item
                            for item in reversed(active.store.messages(main_id))
                            if item["role"] == "user"
                        )
                        assert (
                            latest_owner["source"] == "web" and latest_owner["principal"] == "owner"
                        )
                        assert any(
                            item.get("source", "").startswith("channel:onebot")
                            and item.get("principal") == "owner"
                            for item in original
                        )
                        duplicate = await dispatch(
                            web,
                            "duplicate-transcript",
                            "memory_import_transcript",
                            {"id": restored["transcript_id"], "revision": restored["revision"]},
                        )
                        assert duplicate["created"] is False and duplicate["source_id"] == source_id
                        assert active.memory.source(source_id) == frozen_source
                        assert application.pipeline.job(source_id) == frozen_job
                        delegated = await dispatch(
                            web,
                            "forget-transcript",
                            "spawn_agent",
                            {
                                "task_name": "遗忘转写资料队友",
                                "prompt": "请调用memory_forget遗忘这条实际转写来源及同源派生记忆："
                                + fact_id
                                + "。保留原附件工件和其它来源的独立笔记。",
                            },
                        )
                        assert delegated["id"] == forget_child
                        assert forget_result == {"deleted": True}
                        agent = active.team.get(forget_child)
                        assert agent["parent_id"] == main_id and agent["status"] == "completed"
                        worker_run = active.store.get("run", agent["run_id"])
                        assert worker_run is not None and worker_run["status"] == "completed"
                        forgotten_call = next(
                            item
                            for item in active.store.messages(forget_child)
                            if item.get("tool_call_id") == "worker-forget-transcript"
                        )
                        assert forgotten_call["content_provenance"] == "accepted"
                        assert json.loads(forgotten_call["content"]) == {"deleted": True}
                        pending_team = tuple(active.team._tasks)
                        if pending_team:
                            _, pending = await asyncio.wait(pending_team, timeout=4)
                            assert not pending
                        main_task = active.tasks.get(main_id)
                        if main_task is not None:
                            acknowledged = await asyncio.wait_for(asyncio.shield(main_task), 4)
                            assert acknowledged in {
                                "验收阶段 forget-transcript 已结束，结果以工具回执为准。",
                                forget_ack,
                            }
                        assert active.team.idle
                        report = active.store.get("team_report", agent["run_id"])
                        assert report is not None and report["status"] == "delivered"
                        assert not active.store.pending_inputs(main_id)
                        assert rounds["forget-transcript"] == 3
                        assert active.memory.source(source_id) is None
                        assert active.memory.get(fact_id) is None
                        assert active.memory.source(note_source_id) == frozen_note
                        assert (
                            active.store.db.execute(
                                "SELECT count(*) FROM memory_meta WHERE source_id=?", (source_id,)
                            ).fetchone()[0]
                            == 0
                        )
                        assert (
                            active.store.db.execute(
                                "SELECT count(*) FROM memory_edges WHERE source_id=?", (source_id,)
                            ).fetchone()[0]
                            == 0
                        )
                    else:
                        assert active.memory.source(source_id) is None
                        replay = await dispatch(
                            web,
                            "forgotten-replay",
                            "memory_import_transcript",
                            {"id": artifact_id, "revision": transcript["revision"]},
                        )
                        assert replay["source_id"] == source_id and replay["state"] == "forgotten"
                        assert (
                            replay["created"] is False and active.memory.source(source_id) is None
                        )
                        assert active.memory.get(fact_id) is None
                        assert application.pipeline.job(source_id) is None
                        assert (
                            active.store.db.execute(
                                "SELECT count(*) FROM memory_forgotten_sources WHERE id=?",
                                (source_id,),
                            ).fetchone()[0]
                            == 1
                        )
                        assert active.memory.source(note_source_id) == frozen_note
                        missing = await dispatch(
                            web,
                            "query-forgotten",
                            "memory_query",
                            {"query": topic, "hops": 2, "limit": 4},
                        )
                        assert all(
                            item.get("source_id") != source_id for item in missing["memories"]
                        )
                        assert all(item["id"] != source_id for item in missing["sources"])
                    assert extracted.count(source_id) == extracted.count(note_source_id) == 1
                    assert app.state.attachments.path(artifact_id).read_bytes() == raw
                    assert len(asr_requests) == 1
                    assert sent == []
                    assert not any(
                        event["type"] == "tool.started"
                        and event["data"].get("name") == "channel_send"
                        for event in active.store.events()
                    )
                    await asyncio.wait_for(app.state.close(), 4)
            finally:
                release.set()
                await active.close()
