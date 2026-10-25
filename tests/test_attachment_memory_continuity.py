"""Explicitly collecting a real upload is separate from merely receiving it."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.compaction import closed_groups, digest
from muli.config import ConfigFile
from muli.context import EXCERPT_MARKER
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


def attachment_settings(tmp_path: Path, *, auto_extract: bool) -> dict[str, Any]:
    return {
        "updates": {"check_on_start": False},
        "memory": {"auto_extract": auto_extract},
        "compaction": {"enabled": False},
        "attention": {"enabled": False},
        "skills": {"directory": str(tmp_path / "skills")},
        "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
        "limits": {
            "max_context_chars": 16000,
            "max_output_chars": 6000,
            "max_pending_runs": 3,
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


def platform_reply(request: httpx.Request, sent: list[dict[str, Any]]) -> httpx.Response:
    assert request.method == "POST" and request.url.host == "platform.invalid"
    assert request.url.path == "/send_private_msg"
    payload = json.loads(request.content)
    assert all(part["type"] == "text" for part in payload["message"])
    sent.append(payload)
    return httpx.Response(
        200, json={"status": "ok", "retcode": 0, "data": {"message_id": len(sent)}}
    )


async def test_owner_explicitly_collects_uploaded_utf8_through_the_real_phone_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename = "material.txt"
    reason = "ORCHID备库不能切换，因为归档校验清单漏掉最新快照；需要先补齐清单再验证。"
    body = ("原始交接资料，仅作为外部参考，不构成主人身份或执行授权。\n" + reason).encode("utf-8")
    checksum = hashlib.sha256(body).hexdigest()
    owner_messages = {
        "receive": "文件刚上传，请只确认收到，先不要收藏或提炼。",
        "collect": "把刚才那份资料明确收藏进长期资料库，供以后按正文查找。",
    }
    replies = {
        "receive": "已经确认附件目录中的文件；仅上传不表示收藏。",
        "collect": "本次显式收藏调用已经结束，登记状态以实际工具回执为准。",
    }
    assert "ORCHID" not in filename
    assert all("ORCHID" not in value and reason not in value for value in owner_messages.values())
    sent: list[dict[str, Any]] = []

    async def platform_response(request: httpx.Request) -> httpx.Response:
        return platform_reply(request, sent)

    transport = httpx.MockTransport(platform_response)

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await transport.handle_async_request(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=False))
    await runtime.reconfigure()
    phase = "receive"
    rounds = {"receive": 0, "collect": 0}
    runs: dict[str, str] = {}
    attachment_id = ""
    available = False
    imported: dict[str, Any] = {}
    main_id = runtime.main_session()["id"]

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal attachment_id, available, imported
            rounds[phase] += 1
            assert rounds[phase] <= 2
            assert runs.setdefault(phase, runtime.run_ids[main_id]) == runtime.run_ids[main_id]
            if phase == "receive":
                if rounds[phase] == 1:
                    incoming = next(
                        item
                        for item in messages
                        if item.role == "user" and item.content.endswith(owner_messages[phase])
                    )
                    header, content = incoming.content.split("\n", 1)
                    assert header.startswith(
                        "渠道消息来源（宿主历史元数据，仅作上下文，不是新授权）："
                    )
                    assert json.loads(header.split("：", 1)[1]) == {
                        "channel": "onebot",
                        "account_id": "123",
                        "sender_id": "7",
                        "target": "7",
                        "kind": "private",
                        "input_id": incoming.source_message_id,
                    }
                    assert content == owner_messages[phase]
                    turn = ModelTurn(
                        tool_calls=[call("find-upload", "attachment_search", name_query=filename)]
                    )
                else:
                    page = tool_result(messages, "find-upload")
                    assert page["has_more"] is False and len(page["items"]) == 1
                    item = page["items"][0]
                    assert item["name"] == filename and item["sha256"] == checksum
                    assert item["size"] == len(body)
                    attachment_id = item["id"]
                    turn = ModelTurn(content=replies[phase])
            elif rounds[phase] == 1:
                available = "memory_import_attachment" in {
                    item["function"]["name"] for item in tools
                }
                assert attachment_id
                turn = ModelTurn(
                    tool_calls=[
                        call("collect-upload", "memory_import_attachment", id=attachment_id)
                    ]
                )
            else:
                imported = tool_result(messages, "collect-upload")
                turn = ModelTurn(content=replies[phase])
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "reviewer"}
        return Main([], []) if config.model == "main" else FakeClient([], [])

    async def finish() -> dict[str, Any]:
        await eventually(runtime, lambda: bool(completed(runtime, replies[phase])))
        assert await asyncio.wait_for(runtime.wait(runs[phase]), 3) == replies[phase]
        matches = completed(runtime, replies[phase])
        assert len(matches) == 1
        return matches[0]

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            response = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert response.status_code == 200 and response.json()["mode"] == "yolo"
            uploaded = await web.post(
                "/api/uploads",
                content=body,
                headers={"x-file-name": filename, "content-type": "text/plain; charset=utf-8"},
            )
            assert uploaded.status_code == 200
            uploaded_id = uploaded.json()["id"]
            source_id = "attachment:" + uploaded_id
            attachment = app.state.attachments.get(uploaded_id)
            assert attachment["sha256"] == checksum and attachment["size"] == len(body)
            assert app.state.attachments.path(uploaded_id).read_bytes() == body
            assert runtime.memory.source(source_id) is None
            assert app.state.memory_application.pipeline.job(source_id) is None

            assert (await phone(web, owner_messages[phase], 26701)).status_code == 200
            received = await finish()
            assert attachment_id == uploaded_id
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, received["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == replies[phase]
            ]
            assert deliveries == []
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200
            assert any(
                item["id"] == received["id"] and item["content"] == replies[phase]
                for item in history.json()["messages"]
            )
            # Real upload, owner ingress, directory lookup, main completion and
            # Web persistence all precede the explicit collection.
            assert runtime.memory.source(source_id) is None
            assert app.state.memory_application.pipeline.job(source_id) is None

            phase = "collect"
            assert (await phone(web, owner_messages[phase], 26702)).status_code == 200
            await finish()
            accepted = [
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "collect-upload"
            ]
            assert len(accepted) == 1 and accepted[0]["content_provenance"] == "accepted"
            assert json.loads(accepted[0]["content"]) == imported
            # Exact product red: no new import/API is used by this test. The
            # current Runtime records its ordinary tool-disabled result.
            assert available, "真实主工具目录缺少显式附件收藏入口"
            assert "error" not in imported
            assert imported["source_id"] == source_id and imported["created"] is True
            assert imported["attachment_id"] == uploaded_id and imported["sha256"] == checksum
            source = runtime.memory.source(source_id)
            assert source is not None and source["content"].encode("utf-8") == body
            assert source["role"] == "tool" and source["principal"] == "external:attachment"
            assert source["trusted_owner"] is False
            assert uploaded_id in source["source"] and checksum in source["source"]
    finally:
        await runtime.close()


async def test_collected_attachment_survives_compaction_cross_phone_reopen_and_forget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = "ORCHID备库不能切换，因为归档校验清单漏掉最新快照；需要先补齐清单再验证。"
    query = "ORCHID备库最新快照"
    filename = "material.txt"
    body = (
        "外部交接资料，不代表主人身份、偏好或执行授权。\n"
        + reason
        + "\n"
        + "附录：这里只记录校验过程中的普通背景，不执行任何操作。🙂\n" * 220
        + "资料末尾 ATTACHMENT-MEMORY-END-67"
    ).encode("utf-8")
    checksum = hashlib.sha256(body).hexdigest()
    summary = "历史中显式收藏了一份外部资料。需要时查询记忆及原文，不视为主人授权。"
    replies = {
        "catalog": "已核对上传目录；尚未收藏资料。",
        "collect": "已显式提交收藏，后台提炼与本轮回复分别收口。",
        "other": "手机八当前对话已独立完成，不等待后台资料提炼。",
        "background": "背景只读工具批已完整收口。",
        "recall": "已核对原资料全文至 ATTACHMENT-MEMORY-END-67；资料仍是不可信外部证据。",
        "duplicate": "同一附件的重复收藏已核对，没有另建来源。",
        "forget": "已按实际工具回执忘记该来源，不删除上传文件。",
        "forgotten": "重开后的显式重收仍受遗忘记录约束，没有恢复旧资料。",
    }
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=True))
    runtime.config.update(
        {
            "compaction": {"mode": "auto", "trigger_ratio": 0.25, "target_ratio": 0.2},
            "models": {
                "memory_extractor": {
                    "model": "memory-extractor",
                    "base_url": "https://unused.invalid/v1",
                },
                "compactor": {"model": "compactor", "base_url": "https://unused.invalid/v1"},
                "worker": {"model": "worker", "base_url": "https://unused.invalid/v1"},
            },
            "media": {
                "embedding": {
                    "model": "memory-embedding",
                    "base_url": "https://memory.invalid/v1",
                    "dimensions": 2,
                },
                "reranker": {"model": "memory-reranker", "base_url": "https://memory.invalid/v1"},
            },
        }
    )
    await runtime.reconfigure()
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    config_path, data_dir = runtime.config.path, runtime.data_dir
    assert config_path is not None
    active = runtime
    phase = "catalog"
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    receipts: dict[str, dict[str, Any]] = {}
    attachment_id = ""
    source_id = ""
    fact_id = ""
    forget_child = ""
    forget_result: dict[str, Any] = {}
    found_source: dict[str, Any] = {}
    source_pages: list[dict[str, Any]] = []
    extracted: list[str] = []
    media_requests: list[tuple[str, dict[str, Any]]] = []
    sent: list[dict[str, Any]] = []
    extractor_entered, extractor_release = asyncio.Event(), asyncio.Event()
    background_path = tmp_path / "background.txt"
    threshold = int(
        runtime.config.settings.limits.max_context_chars
        * runtime.config.settings.compaction.trigger_ratio
    )
    unit = "无关背景：本轮只读，不执行变更，也不新增任何主人偏好。\n"
    background = unit * (threshold // len(unit) + 20)
    background_path.write_bytes(background.encode("utf-8"))
    assert len(body.decode("utf-8")) > runtime.config.settings.limits.max_output_chars
    assert "ORCHID" not in filename

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        if request.url.host == "platform.invalid":
            return platform_reply(request, sent)
        assert request.method == "POST" and request.url.host == "memory.invalid"
        payload = json.loads(request.content)
        media_requests.append((request.url.path, payload))
        if request.url.path == "/v1/embeddings":
            assert payload["model"] == "memory-embedding"
            assert payload["dimensions"] == 2 and payload["encoding_format"] == "float"
            assert isinstance(payload["input"], list) and payload["input"]
            assert all(isinstance(value, str) for value in payload["input"])
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": [1.0, 0.0]}
                        for index in reversed(range(len(payload["input"])))
                    ]
                },
            )
        assert request.url.path == "/v1/rerank"
        assert payload["model"] == "memory-reranker" and payload["query"] == query
        assert payload["return_documents"] is False
        documents = payload["documents"]
        assert isinstance(documents, list) and documents
        assert all(isinstance(value, str) for value in documents)
        assert type(payload["top_n"]) is int and 0 < payload["top_n"] <= len(documents)
        ranked = sorted(
            enumerate(documents), key=lambda item: (-(1.0 if item[1] == reason else 0.1), item[0])
        )[: payload["top_n"]]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0 if value == reason else 0.1}
                    for index, value in ranked
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    async def settle_query_inputs() -> None:
        # Query assertions require a stable memory revision. Finish the real
        # already-queued owner jobs before dispatching the explicit query;
        # otherwise their embedding checkpoint may race the query's HTTP await.
        rows = active.store.db.execute(
            "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry') "
            "ORDER BY source_id LIMIT ?",
            (active.config.settings.memory.max_pending_jobs,),
        ).fetchall()
        for row in rows:
            job = await asyncio.wait_for(application.pipeline.wait(row["source_id"]), 4)
            assert job is not None and job["state"] == "done"

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
            if source["id"].startswith("attachment:"):
                assert source["id"] == source_id and source["content"].encode("utf-8") == body
                assert source["trusted_owner"] is False
                assert source["principal"] == "external:attachment" and source["role"] == "tool"
                extracted.append(source["id"])
                extractor_entered.set()
                await asyncio.wait_for(extractor_release.wait(), 4)
                memories.append(
                    {
                        "layer": "semantic",
                        "content": reason,
                        "quote": reason,
                        "entities": ["ORCHID备库", "最新快照"],
                        "relations": [
                            {"subject": "ORCHID备库", "predicate": "缺少", "object": "最新快照"}
                        ],
                        "confidence": 0.9,
                        "claim_key": "orchid.backup.reason",
                    }
                )
            return ModelTurn(
                content=json.dumps({"memories": memories, "aliases": {}}, ensure_ascii=False)
            )

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal attachment_id, fact_id, found_source, forget_child, forget_result
            self.round += 1
            if self.role == "worker":
                assert phase == "forget" and fact_id
                if self.round == 1:
                    assert any(fact_id in item.content for item in messages if item.role == "user")
                    assert "memory_forget" in {item["function"]["name"] for item in tools}
                    turn = ModelTurn(
                        tool_calls=[call("worker-forget", "memory_forget", id=fact_id)]
                    )
                else:
                    assert self.round == 2
                    forget_result = tool_result(messages, "worker-forget")
                    assert forget_result == {"deleted": True}
                    turn = ModelTurn(content="指定资料已按工具回执遗忘，原上传文件保留。")
                yield StreamEvent("done", turn=turn)
                return
            assert self.role == "main"
            latest = next(
                item for item in reversed(active.store.messages(main_id)) if item["role"] == "user"
            )
            source = latest.get("source", "")
            if (
                (self.round == 1 or (phase == "forget" and self.round > 3))
                and isinstance(source, str)
                and source.startswith("team:")
                and latest.get("principal") == "agent:" + source[5:]
            ):
                # A genuinely late completion can open a new main driver. Its
                # actual team input is an acknowledgment, never another spawn.
                yield StreamEvent("done", turn=ModelTurn(content="已收到遗忘队友的完成回报。"))
                return
            number = rounds[phase] = rounds.get(phase, 0) + 1
            assert number <= active.config.settings.limits.max_tool_steps
            assert runs.setdefault(phase, active.run_ids[main_id]) == active.run_ids[main_id]
            if phase == "catalog":
                if number == 1:
                    turn = ModelTurn(
                        tool_calls=[call("catalog", "attachment_search", name_query=filename)]
                    )
                else:
                    assert number == 2
                    page = tool_result(messages, "catalog")
                    assert len(page["items"]) == 1 and page["has_more"] is False
                    item = page["items"][0]
                    assert item["sha256"] == checksum and item["size"] == len(body)
                    attachment_id = item["id"]
                    turn = ModelTurn(content=replies[phase])
            elif phase in {"collect", "duplicate", "forgotten"}:
                if number == 1:
                    turn = ModelTurn(
                        tool_calls=[call(phase, "memory_import_attachment", id=attachment_id)]
                    )
                elif number == 2:
                    receipt = tool_result(messages, phase)
                    assert "error" not in receipt
                    receipts[phase] = receipt
                    if phase == "forgotten":
                        await settle_query_inputs()
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    "query-forgotten", "memory_query", query=query, limit=3, hops=2
                                )
                            ]
                        )
                    else:
                        turn = ModelTurn(content=replies[phase])
                else:
                    assert phase == "forgotten" and number == 3
                    page = tool_result(messages, "query-forgotten")
                    assert "error" not in page
                    assert all(item.get("source_id") != source_id for item in page["memories"])
                    assert all(item["id"] != source_id for item in page["sources"])
                    turn = ModelTurn(content=replies[phase])
            elif phase == "other":
                assert number == 1 and extractor_entered.is_set() and not extractor_release.is_set()
                turn = ModelTurn(content=replies[phase])
            elif phase == "background":
                if number == 1:
                    turn = ModelTurn(
                        tool_calls=[call("background", "fs_read", path=str(background_path))]
                    )
                else:
                    assert number == 2
                    accepted = next(
                        item
                        for item in active.store.messages(main_id)
                        if item.get("tool_call_id") == "background"
                    )
                    assert accepted["content_provenance"] == "accepted"
                    original_result = accepted["content"]
                    result = json.loads(original_result)
                    assert set(result) == {"path", "content"} and result["content"] == background
                    assert result["content"].encode("utf-8") == background_path.read_bytes()
                    projected = next(
                        item.content
                        for item in reversed(messages)
                        if item.tool_call_id == "background"
                    )
                    if projected != original_result:
                        # Ordinary fs_read is intentionally excerptable. Keep
                        # its exact durable JSON oracle and verify only the
                        # packer's actual balanced head/marker/tail projection.
                        marker = "\n" + EXCERPT_MARKER + "\n"
                        assert projected.count(marker) == 1
                        head, separator, tail = projected.partition(marker)
                        assert separator and head and tail
                        keep = len(head) + len(tail)
                        assert 2 <= keep < len(original_result)
                        assert len(head) == (keep + 1) // 2 and len(tail) == keep // 2
                        assert head == original_result[: len(head)]
                        assert tail == original_result[-len(tail) :]
                    turn = ModelTurn(content=replies[phase])
            elif phase == "recall":
                if number == 1:
                    await settle_query_inputs()
                    turn = ModelTurn(
                        tool_calls=[call("query", "memory_query", query=query, limit=3, hops=2)]
                    )
                elif number == 2:
                    page = tool_result(messages, "query")
                    assert "error" not in page and page["degraded"] == []
                    fact = next(
                        item
                        for item in page["memories"]
                        if item["layer"] == "semantic" and item["content"] == reason
                    )
                    assert fact["quote"] == reason and fact["trusted_owner"] is False
                    assert fact["matched_by"] == "reranked"
                    fact_id = fact["id"]
                    found_source = next(
                        item for item in page["sources"] if item["id"] == fact["source_id"]
                    )
                    # Both the identity and first revision come from actual tool
                    # output, never from the fixture's uploaded-ID closure.
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "source-0",
                                "memory_source",
                                id=found_source["id"],
                                revision=found_source["digest"],
                                length_bytes=active.config.settings.limits.max_output_chars,
                            )
                        ]
                    )
                else:
                    page = tool_result(messages, f"source-{number - 3}")
                    assert "error" not in page and page["trusted_owner"] is False
                    assert page["source_id"] == found_source["id"]
                    assert page["revision"] == found_source["digest"]
                    assert page["offset_bytes"] == sum(
                        part["length_bytes"] for part in source_pages
                    )
                    assert page["length_bytes"] == len(page["content"].encode("utf-8"))
                    assert page["total_bytes"] == len(body)
                    assert page["next_offset_bytes"] == page["offset_bytes"] + page["length_bytes"]
                    assert (
                        len(json.dumps(page, ensure_ascii=False))
                        <= active.config.settings.limits.max_output_chars
                    )
                    source_pages.append(deepcopy(page))
                    if page["has_more"]:
                        assert page["length_bytes"] > 0
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    f"source-{number - 2}",
                                    "memory_source",
                                    id=page["source_id"],
                                    revision=page["revision"],
                                    offset_bytes=page["next_offset_bytes"],
                                    length_bytes=active.config.settings.limits.max_output_chars,
                                )
                            ]
                        )
                    else:
                        recovered = "".join(part["content"] for part in source_pages).encode(
                            "utf-8"
                        )
                        assert (
                            recovered == body and hashlib.sha256(recovered).hexdigest() == checksum
                        )
                        turn = ModelTurn(content=replies[phase])
            else:
                assert phase == "forget"
                if number == 1:
                    assert fact_id
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "delegate-forget",
                                "spawn_agent",
                                task_name="遗忘资料队友",
                                prompt="请调用memory_forget遗忘这条实际资料及同源派生记忆："
                                + fact_id
                                + "。不要删除上传文件。",
                            )
                        ]
                    )
                elif number == 2:
                    spawned = tool_result(messages, "delegate-forget")
                    assert "error" not in spawned
                    forget_child = spawned["id"]
                    turn = ModelTurn(
                        tool_calls=[
                            call("wait-forget", "wait_agents", targets=[forget_child], timeout=2)
                        ]
                    )
                else:
                    assert number == 3
                    waited = tool_result(messages, "wait-forget")
                    assert waited["timed_out"] is False
                    assert any(
                        item["id"] == forget_child and item["status"] == "completed"
                        for item in waited["agents"]
                    )
                    assert forget_result == {"deleted": True}
                    turn = ModelTurn(content=replies[phase])
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "worker", "reviewer", "memory-extractor", "compactor"}
        return Client(config.model)

    async def finish() -> dict[str, Any]:
        try:
            await eventually(active, lambda: bool(completed(active, replies[phase])))
        except TimeoutError:
            run = active.store.get("run", runs.get(phase, "")) or {}
            diagnostic = json.dumps(
                {
                    "phase": phase,
                    "rounds": rounds,
                    "status": run.get("status"),
                    "exception_type": (run.get("failure_diagnostic") or {}).get("exception_type"),
                    "pending_approvals": len(active.permissions.pending),
                    "attachment_extractions": len(extracted),
                    "source_pages": len(source_pages),
                },
                ensure_ascii=True,
            )
            assert len(diagnostic.encode("utf-8")) <= 1024
            pytest.fail(diagnostic)
        assert await asyncio.wait_for(active.wait(runs[phase]), 3) == replies[phase]
        matched = completed(active, replies[phase])
        assert len(matched) == 1
        return matched[0]

    event_id = 26710

    async def speak(
        web: httpx.AsyncClient, next_phase: str, text: str, *, sender: int
    ) -> dict[str, Any]:
        nonlocal phase, event_id
        phase, event_id = next_phase, event_id + 1
        response = await phone(web, text, event_id, sender=sender)
        assert response.status_code == 200
        return await finish()

    snapshot: dict[str, Any] = {}
    original: list[dict[str, Any]] = []
    frozen_source: dict[str, Any] = {}
    for stage in ("initial", "recall", "forgotten"):
        if stage != "initial":
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
                manager = app.state.channel_application.manager
                application = app.state.memory_application
                if stage == "initial":
                    mode = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
                    assert mode.status_code == 200 and mode.json()["mode"] == "yolo"
                    uploaded = await web.post(
                        "/api/uploads",
                        content=body,
                        headers={
                            "x-file-name": filename,
                            "content-type": "text/plain; charset=utf-8",
                        },
                    )
                    assert uploaded.status_code == 200
                    uploaded_id = uploaded.json()["id"]
                    source_id = "attachment:" + uploaded_id
                    assert (
                        active.memory.source(source_id) is None
                        and application.pipeline.job(source_id) is None
                    )
                    await speak(web, "catalog", "只核对刚上传的资料目录，先不要收藏。", sender=7)
                    assert attachment_id == uploaded_id and active.memory.source(source_id) is None
                    assert (
                        application.pipeline.job(source_id) is None
                        and not extractor_entered.is_set()
                    )
                    collected = await speak(
                        web, "collect", "现在明确收藏刚才的附件供以后按正文查找。", sender=7
                    )
                    assert (
                        receipts["collect"]["source_id"] == source_id
                        and receipts["collect"]["created"] is True
                    )
                    assert receipts["collect"]["attachment_id"] == attachment_id
                    assert receipts["collect"]["sha256"] == checksum
                    await asyncio.wait_for(extractor_entered.wait(), 3)
                    source = active.memory.source(source_id)
                    assert source is not None and source["content"].encode("utf-8") == body
                    frozen_source = deepcopy(source)
                    assert source["trusted_owner"] is False and source["role"] == "tool"
                    assert source["principal"] == "external:attachment"
                    assert attachment_id in source["source"] and checksum in source["source"]
                    assert (
                        source["created_at"]
                        == app.state.attachments.get(attachment_id)["created_at"]
                    )
                    await processed_reply(active, manager, collected["id"])
                    delivered = [
                        item
                        for item in manager.deliveries()
                        if delivery_text(item) == replies["collect"]
                    ]
                    assert delivered == []
                    visible = await web.get(f"/api/sessions/{main_id}/messages")
                    assert visible.status_code == 200
                    assert any(
                        item["id"] == collected["id"] and item["content"] == replies["collect"]
                        for item in visible.json()["messages"]
                    )
                    await speak(
                        web,
                        "other",
                        "我换到当前手机说一句话，请直接回答本轮，不要等待后台资料。",
                        sender=8,
                    )
                    assert (
                        not extractor_release.is_set()
                        and application.pipeline.job(source_id)["state"] == "processing"
                    )
                    assert active.store.get("run", runs["other"])["status"] == "completed"
                    extractor_release.set()
                    job = await asyncio.wait_for(application.pipeline.wait(source_id), 4)
                    assert job is not None and job["state"] == "done" and extracted == [source_id]
                    fact = next(
                        item
                        for item in active.memory.search(query)
                        if item["source_id"] == source_id and item["layer"] == "semantic"
                    )
                    assert fact["quote"] == reason and fact["trusted_owner"] is False
                    assert (
                        active.store.db.execute(
                            "SELECT count(*) FROM memory_edges WHERE memory_id=?", (fact["id"],)
                        ).fetchone()[0]
                        == 1
                    )
                    assert any(
                        path == "/v1/embeddings" and reason in payload["input"]
                        for path, payload in media_requests
                    )

                    await speak(
                        web, "background", "只读本地背景文件后结束，不做其它操作。", sender=8
                    )
                    original = active.store.messages(main_id)
                    assert len(original) <= 32 and sum(map(len, closed_groups(original))) == len(
                        original
                    )
                    accepted = next(
                        item for item in original if item.get("tool_call_id") == "collect"
                    )
                    assert (
                        accepted["content_provenance"] == "accepted"
                        and json.loads(accepted["content"]) == receipts["collect"]
                    )
                    active.config.update({"compaction": {"enabled": True}})
                    active.compaction.observe(main_id)
                    await active.compaction.finish(main_id)
                    stored = active.store.get("compaction", main_id)
                    assert (
                        stored is not None
                        and stored["kind"] == "semantic"
                        and stored["summary"] == summary
                    )
                    snapshot = deepcopy(stored)
                    assert snapshot["covered_seq"] >= accepted["seq"]
                    prefix = [
                        item
                        for item in original
                        if snapshot["from_seq"] < item["seq"] <= snapshot["covered_seq"]
                    ]
                    assert (
                        digest(prefix) == snapshot["prefix_digest"]
                        and active.store.messages(main_id) == original
                    )
                    projected = active.compaction.project(main_id)
                    assert any(item.get("compaction_summary") for item in projected)
                    assert accepted["id"] not in {item["id"] for item in projected}
                    assert attachment_id not in str(projected) and reason not in summary
                elif stage == "recall":
                    assert active.store.get("compaction", main_id) == snapshot
                    assert active.store.messages(main_id) == original
                    assert active.memory.source(source_id) == frozen_source
                    assert extracted == [source_id]
                    recalled = await speak(
                        web,
                        "recall",
                        "请按正文查找ORCHID备库最新快照的原因，并核对原资料全文。",
                        sender=8,
                    )
                    assert fact_id and found_source["id"] == source_id and len(source_pages) >= 2
                    assert any(
                        path == "/v1/rerank" and reason in payload["documents"]
                        for path, payload in media_requests
                    )
                    assert recalled["delivery_routes"][0]["target"] == "8"
                    await processed_reply(active, manager, recalled["id"])
                    deliveries = [
                        item
                        for item in manager.deliveries()
                        if delivery_text(item) == replies["recall"]
                    ]
                    assert deliveries == []
                    visible = await web.get(f"/api/sessions/{main_id}/messages")
                    assert visible.status_code == 200
                    assert any(
                        item["content"] == replies["recall"] for item in visible.json()["messages"]
                    )
                    await speak(
                        web, "duplicate", "再次收藏目录中同一份原附件，核对已有记录即可。", sender=8
                    )
                    assert (
                        receipts["duplicate"]["source_id"] == source_id
                        and receipts["duplicate"]["created"] is False
                    )
                    assert active.memory.source(source_id) == frozen_source and extracted == [
                        source_id
                    ]
                    assert (
                        active.store.db.execute(
                            "SELECT count(*) FROM memory_jobs WHERE source_id=?", (source_id,)
                        ).fetchone()[0]
                        == 1
                    )
                    await speak(
                        web,
                        "forget",
                        "请忘记刚核对的那份长期资料及其派生记忆，不删除上传文件。",
                        sender=8,
                    )
                    agent = active.team.get(forget_child)
                    assert agent["parent_id"] == main_id and agent["status"] == "completed"
                    worker_run = active.store.get("run", agent["run_id"])
                    assert worker_run is not None and worker_run["status"] == "completed"
                    forgotten_call = next(
                        item
                        for item in active.store.messages(forget_child)
                        if item.get("tool_call_id") == "worker-forget"
                    )
                    assert forgotten_call["content_provenance"] == "accepted"
                    assert json.loads(forgotten_call["content"]) == {"deleted": True}
                    pending_team = tuple(active.team._tasks)
                    if pending_team:
                        await asyncio.wait_for(asyncio.shield(asyncio.gather(*pending_team)), 4)
                    main_task = active.tasks.get(main_id)
                    if main_task is not None:
                        await asyncio.wait_for(asyncio.shield(main_task), 4)
                    assert (
                        active.memory.source(source_id) is None
                        and active.memory.get(fact_id) is None
                    )
                    assert (
                        active.store.db.execute(
                            "SELECT count(*) FROM memory_forgotten_sources WHERE id=?", (source_id,)
                        ).fetchone()[0]
                        == 1
                    )
                    assert (
                        active.store.db.execute(
                            "SELECT count(*) FROM memory_edges WHERE memory_id=?", (fact_id,)
                        ).fetchone()[0]
                        == 0
                    )
                else:
                    assert active.memory.source(source_id) is None
                    await speak(
                        web,
                        "forgotten",
                        "核对同一附件在遗忘后是否仍保持遗忘，不要求重新提炼。",
                        sender=8,
                    )
                    assert receipts["forgotten"]["source_id"] == source_id
                    assert (
                        receipts["forgotten"]["created"] is False
                        and receipts["forgotten"]["state"] == "forgotten"
                    )
                    assert (
                        active.memory.source(source_id) is None
                        and active.memory.get(fact_id) is None
                    )
                    assert extracted == [source_id]
                assert app.state.attachments.path(attachment_id).read_bytes() == body
                assert app.state.attachments.get(attachment_id)["sha256"] == checksum
                await asyncio.wait_for(app.state.close(), 3)
        finally:
            extractor_release.set()
            await active.close()
