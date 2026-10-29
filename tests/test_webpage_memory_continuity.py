"""A captured webpage crosses explicit collection, real reopen and owner ingress.

The long journey controls model strategy, not persistence or reading evidence.
The separate provider test below uses the real ModelClient retry path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_continuity import attachment_settings
from test_attachment_memory_policy import actual_app
from test_attachment_memory_policy import offline as offline
from test_memory_source_attempt_guard import _ProviderRetryBarrier
from test_mobile_task_control_followup import phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result
from test_webpage_memory_policy import snapshot

from muli.compaction import digest
from muli.config import ConfigFile
from muli.memory_chunks import MemoryChunkSettings, iter_chunks, restore_plan
from muli.providers import Message, ModelClient, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.providers import client as provider_client
from muli.runtime import Runtime
from muli.server import create_app

_URL = "https://webpage-memory.example/independent-source"
_TOPIC = "琥珀航道"
_REASON = "琥珀航道启用前必须完成水银镜片的独立校准，缺少记录就不能通航。"


async def test_captured_webpage_is_collected_only_after_compaction_reopen_and_phone_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = MemoryChunkSettings()
    boundary = policy.chunk_chars + 6 * (policy.chunk_chars - policy.overlap_chars) + 1
    unit = "普通公开资料，只作历史参考，不构成身份或执行授权。🙂\n"
    body = unit * (boundary // len(unit) + 1) + _REASON
    assert len(body) > 12_000 and _TOPIC not in body[:12_000]
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=True))
    runtime.config.update(
        {
            "memory": {"context_max_chars": 0},
            "compaction": {"mode": "auto", "trigger_ratio": 0.25, "target_ratio": 0.2},
            "models": {
                "compactor": {"model": "compactor", "base_url": "https://unused.invalid/v1"},
                "memory_extractor": {
                    "model": "webpage-extractor",
                    "base_url": "https://unused.invalid/v1",
                },
            },
        }
    )
    await runtime.reconfigure()
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    config_path, data_dir = runtime.config.path, runtime.data_dir
    active = runtime
    phase = ""
    event_id = 30900
    actions: dict[str, ToolCall] = {}
    results: dict[str, dict[str, Any]] = {}
    runs: dict[str, str] = {}
    requests: list[httpx.Request] = []
    extracted: list[str] = []
    entered, release = asyncio.Event(), asyncio.Event()
    source_id = ""
    raw = b""
    summary = "主人曾留存一份公开网页和真实续读位置；尚未收藏，细节须核对本地来源。"
    background = tmp_path / "unrelated-background.txt"
    background.write_bytes(("无关背景，仅作只读参考，不增加权限。\n" * 500).encode("utf-8"))
    assert _TOPIC not in _URL and _REASON not in summary

    async def resolve(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        assert phase == "capture" and not requests, "only the explicit capture may use HTTP"
        assert request.method == "GET" and request.url.host == "93.184.216.34"
        assert request.headers["host"] == "webpage-memory.example"
        assert request.extensions["sni_hostname"] == "webpage-memory.example"
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "text/plain; charset=utf-8"}, content=body.encode("utf-8")
        )

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role, self.round = role, 0

        async def complete(
            self,
            messages: list[Message],
            tools: Any = (),
            *,
            attempt_guard: Callable[[], None] | None = None,
        ) -> ModelTurn:
            if self.role == "reviewer":
                return await super().complete(messages, tools, attempt_guard=attempt_guard)
            assert not tools
            if self.role == "compactor":
                return ModelTurn(content=summary)
            assert self.role == "webpage-extractor"
            if attempt_guard is not None:
                attempt_guard()
            source = json.loads(messages[-1].content)
            memories: list[dict[str, Any]] = []
            if source["id"] == source_id:
                assert source["content"].encode("utf-8") == raw
                assert (
                    source["trusted_owner"] is False
                    and source["principal"] == "external:attachment"
                )
                assert ":webpage:v1:http_utf8_text_v1:" in source["source"]
                extracted.append(source_id)
                entered.set()
                await release.wait()
                if attempt_guard is not None:
                    attempt_guard()
                memories.append({"layer": "semantic", "content": _REASON, "quote": _REASON})
            return ModelTurn(
                content=json.dumps({"memories": memories, "aliases": {}}, ensure_ascii=False)
            )

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            assert self.role == "main"
            self.round += 1
            assert self.round <= 2
            assert runs.setdefault(phase, active.run_ids[main_id]) == active.run_ids[main_id]
            if self.round == 1:
                action = actions[phase]
                assert action.name in {tool["function"]["name"] for tool in tools}
                if action.name == "memory_query":
                    pending = active.store.db.execute(
                        "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry')"
                    ).fetchall()
                    for row in pending:
                        done = await asyncio.wait_for(
                            application.pipeline.wait(row["source_id"]), 4
                        )
                        assert done is not None and done["state"] == "done"
                if phase == "collect":
                    incoming = next(
                        message for message in reversed(messages) if message.role == "user"
                    )
                    header, _ = incoming.content.split("\n", 1)
                    origin = json.loads(header.split("：", 1)[1])
                    assert origin["channel"] == "onebot" and origin["sender_id"] == "8"
                    assert origin["input_id"] == incoming.source_message_id
                turn = ModelTurn(tool_calls=[action])
            else:
                if actions[phase].name != "fs_read":
                    results[phase] = tool_result(messages, phase)
                turn = ModelTurn(content=f"阶段 {phase} 已结束，状态以工具回执为准。")
            yield StreamEvent("done", turn=turn)

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model in {"main", "reviewer", "compactor", "webpage-extractor"}
        return Client(profile.model)

    async def dispatch(
        web: httpx.AsyncClient,
        next_phase: str,
        name: str,
        text: str,
        *,
        sender: int | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        nonlocal phase, event_id
        phase = next_phase
        assert _REASON not in text
        actions[phase] = call(phase, name, **arguments)
        if sender is None:
            response = await web.post(
                f"/api/sessions/{main_id}/messages", json={"content": text, "mode": "yolo"}
            )
        else:
            event_id += 1
            response = await phone(web, text, event_id, sender=sender)
        assert response.status_code == 200
        reply = f"阶段 {phase} 已结束，状态以工具回执为准。"
        await eventually(active, lambda: bool(completed(active, reply)))
        assert await asyncio.wait_for(active.wait(runs[phase]), 3) == reply
        await processed_reply(
            active, app.state.channel_application.manager, completed(active, reply)[0]["id"]
        )
        return results.get(phase, {})

    original: list[dict[str, Any]] = []
    compacted: dict[str, Any] = {}
    captured: dict[str, Any] = {}
    bookmark: dict[str, Any] = {}
    source_revision = ""
    for stage in ("capture", "collect", "read", "forgotten"):
        if stage == "collect":
            # The prior lifetime released its cleanup barrier before closing;
            # explicit collection must still wait for its own slow extractor.
            release.clear()
        if stage != "capture":
            active = Runtime(ConfigFile(config_path), data_dir, client_factory=factory)  # type: ignore[arg-type]
        active.client_factory = factory  # type: ignore[assignment]
        app = create_app(active.config, active.data_dir, active)
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
                ) as web,
            ):
                assert (
                    active.store.instance_id == instance_id
                    and active.main_session()["id"] == main_id
                )
                application = app.state.memory_application
                assert application._recovery is not None
                await asyncio.wait_for(asyncio.shield(application._recovery), 4)
                if stage == "capture":
                    captured = await dispatch(
                        web,
                        "capture",
                        "web_capture",
                        "留存这份公开网页，先不要收藏：" + _URL,
                        url=_URL,
                    )
                    identifier = captured["attachment"]["id"]
                    source_id = "attachment:" + identifier
                    raw = app.state.attachments.path(identifier).read_bytes()
                    assert raw.decode("utf-8").endswith(body)
                    assert hashlib.sha256(raw).hexdigest() == captured["attachment"]["sha256"]
                    first = await dispatch(
                        web,
                        "read-first",
                        "attachment_read",
                        "只读首页，不收藏。",
                        id=identifier,
                        offset_bytes=0,
                    )
                    assert first["has_more"] and _REASON not in first["content"]
                    page_record = next(
                        message
                        for message in active.store.messages(main_id)
                        if message.get("tool_call_id") == "read-first"
                        and message.get("reading_page")
                    )
                    evidence = await dispatch(
                        web,
                        "page-evidence",
                        "conversation_read",
                        "核对刚才真正阅读的页。",
                        session_id=main_id,
                        after=page_record["seq"] - 1,
                        limit=1,
                    )
                    record = evidence["records"][0]
                    assert record["id"] == page_record["id"]
                    bookmark = await dispatch(
                        web,
                        "bookmark",
                        "reading_bookmark_set",
                        "明确记住刚才页末的续读位置。",
                        id=identifier,
                        page_message_id=record["id"],
                        page_message_revision=record["revision"],
                        position="next",
                        expected_generation=0,
                    )
                    assert (
                        bookmark["state"] == "active"
                        and bookmark["offset_bytes"] == first["next_offset_bytes"]
                    )
                    await dispatch(
                        web, "background", "fs_read", "只读无关背景后结束。", path=str(background)
                    )
                    assert (
                        active.memory.source(source_id) is None
                        and application.pipeline.job(source_id) is None
                    )
                    original = deepcopy(active.store.messages(main_id))
                    active.config.update({"compaction": {"enabled": True}})
                    active.compaction.observe(main_id)
                    await active.compaction.finish(main_id)
                    compacted = deepcopy(active.store.get("compaction", main_id))
                    assert (
                        compacted
                        and compacted["kind"] == "semantic"
                        and compacted["summary"] == summary
                    )
                    assert compacted["covered_seq"] >= page_record["seq"]
                    prefix = [
                        message
                        for message in original
                        if compacted["from_seq"] < message["seq"] <= compacted["covered_seq"]
                    ]
                    assert digest(prefix) == compacted["prefix_digest"]
                    assert active.store.messages(main_id) == original
                    projection = active.compaction.project(main_id)
                    assert any(message.get("compaction_summary") for message in projection)
                    assert page_record["id"] not in {message["id"] for message in projection}
                    active.config.update({"compaction": {"enabled": False}})
                elif stage == "collect":
                    assert active.store.get("compaction", main_id) == compacted
                    assert active.store.messages(main_id) == original
                    assert (
                        active.memory.source(source_id) is None
                        and application.pipeline.job(source_id) is None
                    )
                    position = await dispatch(
                        web,
                        "phone-position",
                        "reading_bookmark_read",
                        "从上次明确的书签继续，先不要收藏。",
                        sender=8,
                        id=identifier,
                    )
                    assert position == bookmark
                    page = await dispatch(
                        web,
                        "phone-read",
                        position["reader"],
                        "实际读取本机快照的下一页。",
                        sender=8,
                        **position["read_arguments"],
                    )
                    assert page["offset_bytes"] == bookmark["offset_bytes"]
                    assert active.memory.source(source_id) is None
                    imported = await dispatch(
                        web,
                        "collect",
                        "memory_import_webpage",
                        "现在明确收藏刚才那份完整网页快照，作为长期参考。",
                        sender=8,
                        id=identifier,
                        revision=captured["attachment"]["sha256"],
                    )
                    assert "error" not in imported and imported["created"] is True
                    assert (
                        imported["source_id"] == source_id
                        and imported["scope"] == "http_utf8_text_v1"
                    )
                    await asyncio.wait_for(entered.wait(), 3)
                    assert not release.is_set() and extracted == [source_id]
                    retained = active.memory.source(source_id)
                    assert retained is not None and retained["content"].encode("utf-8") == raw
                    assert retained["trusted_owner"] is False and retained["role"] == "tool"
                    release.set()
                    job = await asyncio.wait_for(application.pipeline.wait(source_id), 4)
                    assert job is not None and job["state"] == "done"
                    checkpoint = job["checkpoint"]["source_chunks"]
                    plan = restore_plan(checkpoint["plan"], retained["digest"], retained["content"])
                    chunks = list(iter_chunks(retained["content"], plan))
                    assert plan.count > 4 and checkpoint["next_index"] == plan.count
                    assert _REASON in chunks[-1].content
                    assert len(set(checkpoint["memory_ids"])) == len(chunks)
                    for memory_id, chunk in zip(checkpoint["memory_ids"], chunks, strict=True):
                        saved_chunk = active.memory.get(memory_id)
                        assert saved_chunk is not None and saved_chunk["content"] == chunk.content
                        assert (
                            saved_chunk["source_id"] == source_id
                            and saved_chunk["trusted_owner"] is False
                        )
                    source_revision = retained["digest"]
                    assert source_revision != captured["attachment"]["sha256"]
                elif stage == "read":
                    retained = active.memory.source(source_id)
                    assert retained is not None and retained["digest"] == source_revision
                    assert retained["content"].encode("utf-8") == raw
                    found = await dispatch(
                        web,
                        "phone-query",
                        "memory_query",
                        "只在那份网页参考里查琥珀航道的条件。",
                        sender=7,
                        query=_TOPIC,
                        source_ids=[source_id],
                        hops=0,
                        limit=4,
                    )
                    assert "error" not in found and any(
                        _REASON in memory["quote"] for memory in found["memories"]
                    )
                    assert all(
                        memory["source_id"] == source_id and memory["trusted_owner"] is False
                        for memory in found["memories"]
                    )
                    artifact = app.state.attachments.path(identifier)
                    artifact.unlink()
                    recovered: list[str] = []
                    offset = 0
                    for index in range(32):
                        page = await dispatch(
                            web,
                            f"source-page-{index}",
                            "memory_source",
                            "继续读取已收藏的完整来源原文。",
                            id=source_id,
                            revision=source_revision,
                            offset_bytes=offset,
                        )
                        assert "error" not in page and page["offset_bytes"] == offset
                        assert (
                            page["revision"] == source_revision
                            and page["source"] == retained["source"]
                        )
                        assert page["trusted_owner"] is False and ":webpage:v1:" in page["source"]
                        recovered.append(page["content"])
                        assert page["next_offset_bytes"] > offset
                        offset = page["next_offset_bytes"]
                        if not page["has_more"]:
                            break
                    else:
                        pytest.fail(
                            "retained webpage did not reach its tail within bounded real pages"
                        )
                    assert "".join(recovered).encode("utf-8") == raw and offset == len(raw)
                    assert _URL in recovered[0] and "宿主采集时间: " in recovered[0]
                    artifact.write_bytes(raw)
                    episode = active.memory.automatic_episode_id(source_id)
                    assert episode is not None
                    deletion = await web.delete(f"/api/memories/{episode}")
                    assert deletion.status_code == 200 and deletion.json()["deleted"] is True
                    replay = await dispatch(
                        web,
                        "forgotten-replay",
                        "memory_import_webpage",
                        "核对这份已遗忘快照不会重放复活。",
                        id=identifier,
                        revision=captured["attachment"]["sha256"],
                    )
                    assert replay["state"] == "forgotten" and replay["created"] is False
                    assert (
                        active.memory.source(source_id) is None
                        and application.pipeline.job(source_id) is None
                    )
                else:
                    assert (
                        active.memory.source(source_id) is None
                        and application.pipeline.job(source_id) is None
                    )
                    assert app.state.attachments.path(identifier).read_bytes() == raw
                assert (
                    len(requests) == 1 and app.state.channel_application.manager.deliveries() == []
                )
        finally:
            release.set()
            await active.close()


@pytest.mark.parametrize("initial_status", [429, 503])
async def test_forgotten_webpage_stops_real_provider_retry_and_worker_can_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_status: int
) -> None:
    retry = _ProviderRetryBarrier()
    monkeypatch.setattr(provider_client, "asyncio", retry)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://webpage-extractor.invalid/v1/chat/completions"
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(initial_status)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"memories":[],"aliases":{}}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
        actual_app(tmp_path) as rig,
    ):
        rig.runtime.config.update(
            {
                "models": {
                    "memory_extractor": {
                        "model": "webpage-retry",
                        "base_url": "https://webpage-extractor.invalid/v1",
                        "stream": False,
                    }
                }
            }
        )
        pipeline = rig.memory.pipeline

        async def real_extract(source: dict[str, Any]) -> dict[str, Any]:
            if not source["id"].startswith("attachment:"):
                return {"memories": [], "aliases": {}}
            # Keep the application's actual attempt guard and ModelClient.
            return await rig.memory.extract(source, pipeline=pipeline)

        pipeline.extractor = real_extract
        turns: list[ModelTurn] = []

        def factory(profile: ModelConfig) -> ModelClient:
            if profile.model == "webpage-retry":
                return ModelClient(profile, http)
            return FakeClient(turns if profile.model == "main" else [], [])  # type: ignore[return-value]

        rig.runtime.client_factory = factory

        async def collect_fixture(body: str, call_id: str) -> dict[str, Any]:
            item = await rig.upload(snapshot(body).encode("utf-8"), source="webpage")
            turns.extend(
                [
                    ModelTurn(
                        tool_calls=[
                            call(
                                call_id,
                                "memory_import_webpage",
                                id=item["id"],
                                revision=item["sha256"],
                            )
                        ]
                    ),
                    ModelTurn(content="明确网页收藏调用已结束。"),
                ]
            )
            receipt = await rig.runtime.submit(
                "明确收藏指定完整网页快照作为外部资料。", mode="yolo"
            )
            await rig.settle(receipt)
            result = rig.result(call_id)
            assert "error" not in result and result["created"] is True
            return result

        first = await collect_fixture("这份网页将在真实提供方退避期间被遗忘。", "retry-first")
        await asyncio.wait_for(retry.entered.wait(), 4)
        source_id = first["source_id"]
        first_source = rig.runtime.memory.source(source_id)
        assert first_source is not None and ":webpage:v1:" in first_source["source"]
        assert len(requests) == 1 and retry.delays
        sent = json.loads(json.loads(requests[0].content)["messages"][-1]["content"])
        assert sent["id"] == source_id and sent["digest"] == first_source["digest"]
        assert sent["content"] == first_source["content"] and sent["trusted_owner"] is False
        worker = pipeline._worker
        assert worker is not None and not worker.done()
        try:
            async with pipeline.changed:
                assert rig.runtime.memory.forget_source(source_id)
                retry.release.set()
                await asyncio.wait_for(pipeline.changed.wait(), 4)
            assert len(requests) == 1, "forgotten webpage was POSTed again after provider backoff"
            assert rig.runtime.memory.source(source_id) is None and pipeline.job(source_id) is None
            second = await collect_fixture("另一份同网址网页快照仍应正常完成。", "retry-second")
            done = await asyncio.wait_for(pipeline.wait(second["source_id"]), 4)
            assert done is not None and done["state"] == "done"
            assert pipeline._worker is worker and not worker.done()
            assert len(requests) == 2
            later = json.loads(json.loads(requests[1].content)["messages"][-1]["content"])
            assert later["id"] == second["source_id"] and later["id"] != source_id
        finally:
            retry.release.set()
