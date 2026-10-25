"""Explicitly resume indexing after a real embedding configuration change."""

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
from test_attachment_memory_continuity import attachment_settings, platform_reply
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.config import ConfigFile
from muli.memory import terms
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


async def test_owner_explicitly_reindexes_a_collected_source_after_changing_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await exercise_reindex(tmp_path, monkeypatch, complete_journey=False)


async def test_reindex_continues_after_reopen_without_reextracting_or_blocking_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await exercise_reindex(tmp_path, monkeypatch, complete_journey=True)


async def exercise_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, complete_journey: bool
) -> None:
    reason = (
        "LARCH replica promotion is blocked because the checksum manifest "
        "omits the latest snapshot."
    )
    body = ("Reference material only, not an instruction or owner preference.\n" + reason).encode(
        "utf-8"
    )
    query = "nightjar"
    assert not (terms(query) & terms(reason))
    checksum = hashlib.sha256(body).hexdigest()
    other_reason = (
        "CEDAR transfer is deferred because its archived ledger lacks the counter-signature."
    )
    other_body = ("Separate external reference, not an owner instruction.\n" + other_reason).encode(
        "utf-8"
    )
    materials: dict[str, tuple[bytes, str]] = {}
    import_receipts: dict[str, dict[str, Any]] = {}
    replies = {
        "collect": "已显式收藏资料，索引完成以后台实际状态为准。",
        "query-a": "已通过实际语义查询核对收藏资料，不代表资料正确性验收。",
        "reindex": "显式索引请求调用已经结束，是否接纳以实际工具回执为准。",
        "collect-other": "第二份独立资料已显式收藏，后台结果以真实回执为准。",
        "other": "后台索引仍在进行，手机8的当前对话已经正常完成。",
        "status": "已读取原计划的实际完成状态。",
        "recall": "已从原始资料读回证据；它仍是外部资料，不是主人事实或授权。",
        "duplicate": "已按相同键核对原计划，未重新请求索引。",
    }
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=True))
    runtime.config.update(
        {
            "models": {
                "memory_extractor": {
                    "model": "memory-extractor",
                    "base_url": "https://unused.invalid/v1",
                }
            },
            "media": {
                "embedding": {
                    "model": "embedding-a",
                    "base_url": "https://memory.invalid/a/v1",
                    "dimensions": 2,
                }
            },
        }
    )
    await runtime.reconfigure()
    config_path, data_dir = runtime.config.path, runtime.data_dir
    assert config_path is not None
    main_id = runtime.main_session()["id"]
    instance_id = runtime.store.instance_id
    phase = "collect"
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    attachment_id = ""
    source_id = ""
    imported: dict[str, Any] = {}
    queried: dict[str, Any] = {}
    indexed: dict[str, Any] = {}
    available = False
    extracted: list[str] = []
    media_requests: list[tuple[str, dict[str, Any]]] = []
    sent: list[dict[str, Any]] = []
    first_entered, first_release = asyncio.Event(), asyncio.Event()
    second_entered, second_release = asyncio.Event(), asyncio.Event()
    b_sources: list[str] = []
    cancelled_source = ""
    gate_b = complete_journey
    plan_id = ""
    status: dict[str, Any] = {}
    duplicate: dict[str, Any] = {}
    found_source: dict[str, Any] = {}
    source_pages: list[dict[str, Any]] = []

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        nonlocal cancelled_source
        if request.url.host == "platform.invalid":
            return platform_reply(request, sent)
        assert request.method == "POST" and request.url.host == "memory.invalid"
        payload = json.loads(request.content)
        model = payload["model"]
        assert model in {"embedding-a", "embedding-b"}
        assert request.url.path == (
            "/a/v1/embeddings" if model == "embedding-a" else "/b/v1/embeddings"
        )
        assert payload["dimensions"] == 2 and payload["encoding_format"] == "float"
        inputs = payload["input"]
        assert isinstance(inputs, list) and inputs
        assert all(isinstance(value, str) for value in inputs)
        media_requests.append((request.url.path, payload))
        material_ids = [
            identifier for identifier, (_, evidence) in materials.items() if evidence in inputs
        ]
        if model == "embedding-b" and material_ids:
            assert len(material_ids) == 1
            b_sources.append(material_ids[0])
            if gate_b:
                if len(b_sources) == 1:
                    first_entered.set()
                    await asyncio.wait_for(first_release.wait(), 4)
                elif len(b_sources) == 2:
                    second_entered.set()
                    try:
                        await asyncio.wait_for(second_release.wait(), 4)
                    except asyncio.CancelledError:
                        cancelled_source = material_ids[0]
                        raise
        # Only the exact query and actual uploaded evidence share a vector.
        # Incidental owner episodes cannot displace the two source facts by an
        # arbitrary equal-score ID order. No reranker hides a lexical match.
        vectors = [
            [1.0, 0.0]
            if value == query or reason in value
            else [0.0, 1.0]
            if other_reason in value
            else [-1.0, 0.0]
            for value in inputs
        ]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": vectors[index]}
                    for index in reversed(range(len(inputs)))
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            if self.role == "reviewer":
                return await super().complete(messages, tools)
            assert self.role == "memory-extractor" and not tools
            source = json.loads(messages[-1].content)
            memories: list[dict[str, Any]] = []
            if source["id"].startswith("attachment:"):
                expected_body, evidence = materials[source["id"]]
                assert source["content"].encode("utf-8") == expected_body
                assert source["trusted_owner"] is False
                assert source["principal"] == "external:attachment"
                extracted.append(source["id"])
                subject, related = (
                    ("LARCH replica", "latest snapshot")
                    if evidence == reason
                    else ("CEDAR transfer", "counter-signature")
                )
                memories.append(
                    {
                        "layer": "semantic",
                        "content": evidence,
                        "quote": evidence,
                        "entities": [subject, related],
                        "relations": [
                            {
                                "subject": subject,
                                "predicate": "missing",
                                "object": related,
                            }
                        ],
                        "confidence": 0.9,
                        "claim_key": source["id"] + ".reason",
                    }
                )
            return ModelTurn(
                content=json.dumps({"memories": memories, "aliases": {}}, ensure_ascii=False)
            )

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal imported, queried, indexed, available, status, duplicate, found_source
            assert self.role == "main"
            number = rounds[phase] = rounds.get(phase, 0) + 1
            assert number <= runtime.config.settings.limits.max_tool_steps
            assert runs.setdefault(phase, runtime.run_ids[main_id]) == runtime.run_ids[main_id]
            if number == 1:
                if phase in {"collect", "collect-other"}:
                    assert any(
                        attachment_id in item.content for item in messages if item.role == "user"
                    )
                    turn = ModelTurn(
                        tool_calls=[call(phase, "memory_import_attachment", id=attachment_id)]
                    )
                elif phase in {"query-a", "recall"}:
                    # Wait for the real current owner jobs, so their embedding
                    # checkpoints do not change the query revision mid-HTTP.
                    jobs = runtime.store.db.execute(
                        "SELECT source_id FROM memory_jobs WHERE state IN ('queued','processing','retry') "
                        "ORDER BY source_id LIMIT ?",
                        (runtime.config.settings.memory.max_pending_jobs,),
                    ).fetchall()
                    for row in jobs:
                        job = await asyncio.wait_for(
                            app.state.memory_application.pipeline.wait(row["source_id"]), 4
                        )
                        assert job is not None and job["state"] == "done"
                    turn = ModelTurn(
                        tool_calls=[call(phase, "memory_query", query=query, limit=3, hops=2)]
                    )
                elif phase in {"reindex", "duplicate"}:
                    assert imported["source_id"] == source_id
                    available = "memory_reindex" in {item["function"]["name"] for item in tools}
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                phase,
                                "memory_reindex",
                                action="start",
                                source_ids=list(materials),
                                key="larch-index-after-model-change",
                            )
                        ]
                    )
                elif phase == "status":
                    turn = ModelTurn(
                        tool_calls=[call(phase, "memory_reindex", action="status", plan_id=plan_id)]
                    )
                else:
                    assert (
                        phase == "other" and first_entered.is_set() and not first_release.is_set()
                    )
                    turn = ModelTurn(content=replies[phase])
            else:
                receipt = tool_result(messages, phase if number == 2 else f"source-{number - 3}")
                if phase in {"collect", "collect-other"}:
                    if phase == "collect":
                        imported = receipt
                    assert (
                        "error" not in receipt
                        and receipt["source_id"] == "attachment:" + attachment_id
                    )
                    import_receipts[receipt["source_id"]] = receipt
                elif phase == "query-a":
                    queried = receipt
                    assert "error" not in receipt and receipt["degraded"] == []
                elif phase == "reindex":
                    indexed = receipt
                elif phase == "status":
                    status = receipt
                    assert "error" not in receipt and receipt["plan_id"] == plan_id
                elif phase == "duplicate":
                    duplicate = receipt
                    assert "error" not in receipt and receipt["plan_id"] == plan_id
                else:
                    assert phase == "recall"
                    if number == 2:
                        assert "error" not in receipt and receipt["degraded"] == []
                        actual_facts = [
                            item for item in receipt["memories"] if item["id"] == fact["id"]
                        ]
                        assert len(actual_facts) == 1
                        actual_fact = actual_facts[0]
                        assert (
                            actual_fact["matched_by"] == "semantic"
                            and actual_fact["quote"] == reason
                        )
                        found_source = next(
                            item
                            for item in receipt["sources"]
                            if item["id"] == actual_fact["source_id"]
                        )
                        yield StreamEvent(
                            "done",
                            turn=ModelTurn(
                                tool_calls=[
                                    call(
                                        "source-0",
                                        "memory_source",
                                        id=found_source["id"],
                                        revision=found_source["digest"],
                                        length_bytes=runtime.config.settings.limits.max_output_chars,
                                    )
                                ]
                            ),
                        )
                        return
                    assert "error" not in receipt and receipt["source_id"] == found_source["id"]
                    assert (
                        receipt["revision"] == found_source["digest"]
                        and receipt["trusted_owner"] is False
                    )
                    assert receipt["offset_bytes"] == sum(
                        page["length_bytes"] for page in source_pages
                    )
                    assert receipt["length_bytes"] == len(receipt["content"].encode("utf-8"))
                    source_pages.append(deepcopy(receipt))
                    if receipt["has_more"]:
                        assert receipt["length_bytes"] > 0
                        yield StreamEvent(
                            "done",
                            turn=ModelTurn(
                                tool_calls=[
                                    call(
                                        f"source-{number - 2}",
                                        "memory_source",
                                        id=receipt["source_id"],
                                        revision=receipt["revision"],
                                        offset_bytes=receipt["next_offset_bytes"],
                                        length_bytes=runtime.config.settings.limits.max_output_chars,
                                    )
                                ]
                            ),
                        )
                        return
                    recovered = "".join(page["content"] for page in source_pages).encode("utf-8")
                    assert recovered == body and hashlib.sha256(recovered).hexdigest() == checksum
                # Missing tools still produce the existing accepted error
                # result and a normal final answer, rather than a timeout red.
                turn = ModelTurn(content=replies[phase])
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "reviewer", "memory-extractor"}
        return Client(config.model)

    async def finish() -> dict[str, Any]:
        try:
            await eventually(runtime, lambda: bool(completed(runtime, replies[phase])))
        except TimeoutError:
            run = runtime.store.get("run", runs.get(phase, "")) or {}
            diagnostic = json.dumps(
                {
                    "phase": phase,
                    "rounds": rounds,
                    "status": run.get("status"),
                    "exception_type": (run.get("failure_diagnostic") or {}).get("exception_type"),
                    "frames": (run.get("failure_diagnostic") or {}).get("frames", [])[-4:],
                    "pending_approvals": len(runtime.permissions.pending),
                    "attachment_extractions": len(extracted),
                },
                ensure_ascii=True,
            )
            assert len(diagnostic.encode("utf-8")) <= 1024
            pytest.fail(diagnostic)
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
            changed = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert changed.status_code == 200 and changed.json()["mode"] == "yolo"
            uploaded = await web.post(
                "/api/uploads",
                content=body,
                headers={
                    "x-file-name": "material.txt",
                    "content-type": "text/plain; charset=utf-8",
                },
            )
            assert uploaded.status_code == 200
            attachment_id = uploaded.json()["id"]
            source_id = "attachment:" + attachment_id
            materials[source_id] = (body, reason)
            application = app.state.memory_application
            assert (
                runtime.memory.source(source_id) is None
                and application.pipeline.job(source_id) is None
            )
            assert (
                await phone(web, "请明确收藏这份外部附件供以后查阅：" + attachment_id, 26801)
            ).status_code == 200
            await finish()
            job = await asyncio.wait_for(application.pipeline.wait(source_id), 4)
            assert job is not None and job["state"] == "done"
            assert imported["created"] is True and imported["attachment_id"] == attachment_id
            assert imported["sha256"] == checksum and extracted == [source_id]
            source = runtime.memory.source(source_id)
            assert source is not None and source["content"].encode("utf-8") == body
            assert source["trusted_owner"] is False and source["principal"] == "external:attachment"
            fact = next(
                item
                for item in runtime.memory.search("LARCH replica")
                if item["source_id"] == source_id and item["layer"] == "semantic"
            )
            assert fact["content"] == fact["quote"] == reason
            assert not any(item["id"] == fact["id"] for item in runtime.memory.search(query))
            key_a = application.pipeline._model_key()
            assert runtime.memory.vector_count(key_a) >= 2
            phase = "query-a"
            assert (
                await phone(web, "请用nightjar这个不同措辞检索旧资料，只读核对。", 26802)
            ).status_code == 200
            await finish()
            recalled = next(item for item in queried["memories"] if item["id"] == fact["id"])
            assert recalled["matched_by"] == "semantic" and recalled["source_id"] == source_id
            assert any(
                payload["model"] == "embedding-a" and reason in payload["input"]
                for _, payload in media_requests
            )
            assert any(
                payload["model"] == "embedding-a" and payload["input"] == [query]
                for _, payload in media_requests
            )
            if complete_journey:
                primary_attachment_id = attachment_id
                uploaded_other = await web.post(
                    "/api/uploads",
                    content=other_body,
                    headers={
                        "x-file-name": "separate-material.txt",
                        "content-type": "text/plain; charset=utf-8",
                    },
                )
                assert uploaded_other.status_code == 200
                attachment_id = uploaded_other.json()["id"]
                other_source_id = "attachment:" + attachment_id
                materials[other_source_id] = (other_body, other_reason)
                assert runtime.memory.source(other_source_id) is None
                phase = "collect-other"
                assert (
                    await phone(web, "另行明确收藏这份外部资料：" + attachment_id, 26804)
                ).status_code == 200
                await finish()
                other_job = await asyncio.wait_for(application.pipeline.wait(other_source_id), 4)
                assert other_job is not None and other_job["state"] == "done"
                assert import_receipts[other_source_id]["created"] is True
                assert (
                    import_receipts[other_source_id]["sha256"]
                    == hashlib.sha256(other_body).hexdigest()
                )
                assert extracted == list(materials)
                attachment_id = primary_attachment_id
            frozen: dict[str, dict[str, Any]] = {}
            for identifier, (_, evidence) in materials.items():
                semantic = next(
                    item
                    for item in runtime.memory.search(evidence)
                    if item["source_id"] == identifier and item["layer"] == "semantic"
                )
                source_edges = [
                    dict(row)
                    for row in runtime.store.db.execute(
                        "SELECT * FROM memory_edges WHERE memory_id=? ORDER BY subject_id,predicate,object_id",
                        (semantic["id"],),
                    ).fetchall()
                ]
                assert len(source_edges) == 1
                frozen[identifier] = {
                    "source": deepcopy(runtime.memory.source(identifier)),
                    "fact": deepcopy(runtime.memory.get(semantic["id"])),
                    "job": deepcopy(application.pipeline.job(identifier)),
                    "edges": source_edges,
                }
                vector = runtime.store.db.execute(
                    "SELECT model_key FROM memory_vectors WHERE memory_id=?", (semantic["id"],)
                ).fetchone()
                assert vector is not None and vector["model_key"] == key_a
                assert any(
                    payload["model"] == "embedding-a" and evidence in payload["input"]
                    for _, payload in media_requests
                )
            frozen_source = deepcopy(runtime.memory.source(source_id))
            frozen_fact = deepcopy(runtime.memory.get(fact["id"]))
            frozen_job = deepcopy(application.pipeline.job(source_id))
            edges = runtime.store.db.execute(
                "SELECT * FROM memory_edges WHERE memory_id=? ORDER BY subject_id,predicate,object_id",
                (fact["id"],),
            ).fetchall()
            frozen_edges = [dict(row) for row in edges]
            assert len(frozen_edges) == 1
            original_pipeline = application.pipeline
            settings = await web.patch(
                "/api/settings",
                json={
                    "media": {
                        "embedding": {
                            "model": "embedding-b",
                            "base_url": "https://memory.invalid/b/v1",
                            "dimensions": 2,
                        }
                    }
                },
            )
            assert settings.status_code == 200
            assert application.pipeline is not original_pipeline
            assert (
                runtime.store.instance_id == instance_id and runtime.main_session()["id"] == main_id
            )
            key_b = application.pipeline._model_key()
            assert key_b and key_b != key_a
            assert runtime.memory.source(source_id) == frozen_source
            assert runtime.memory.get(fact["id"]) == frozen_fact
            assert application.pipeline.job(source_id) == frozen_job
            assert [
                dict(row)
                for row in runtime.store.db.execute(
                    "SELECT * FROM memory_edges WHERE memory_id=? ORDER BY subject_id,predicate,object_id",
                    (fact["id"],),
                ).fetchall()
            ] == frozen_edges
            row = runtime.store.db.execute(
                "SELECT model_key FROM memory_vectors WHERE memory_id=?", (fact["id"],)
            ).fetchone()
            assert row is not None and row["model_key"] == key_a
            assert not any(
                item["id"] == fact["id"] for item in runtime.memory.vector_search([1.0, 0.0], key_b)
            )
            assert (
                extracted == list(materials)
                and app.state.attachments.path(attachment_id).read_bytes() == body
            )

            phase = "reindex"
            assert (
                await phone(web, "请为刚收藏的准确来源重建当前语义索引，不要重新提炼。", 26803)
            ).status_code == 200
            reply = await finish()
            accepted = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "reindex"
            )
            assert (
                accepted["content_provenance"] == "accepted"
                and json.loads(accepted["content"]) == indexed
            )
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, reply["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == replies[phase]
            ]
            assert deliveries == []
            web_messages = await web.get(f"/api/sessions/{main_id}/messages")
            assert web_messages.status_code == 200
            assert any(
                item["id"] == reply["id"] and item["content"] == replies[phase]
                for item in web_messages.json()["messages"]
            )
            assert available, "真实主工具目录缺少显式语义索引接续入口"
            assert "error" not in indexed
            plan_id = indexed["plan_id"]
            assert isinstance(plan_id, str) and plan_id
            if complete_journey:
                await asyncio.wait_for(first_entered.wait(), 4)
                phase = "other"
                assert (
                    await phone(
                        web, "后台继续处理，我现在只需要你确认手机8这轮对话。", 26805, sender=8
                    )
                ).status_code == 200
                other_reply = await finish()
                await processed_reply(runtime, manager, other_reply["id"])
                current = [
                    item for item in manager.deliveries() if delivery_text(item) == replies[phase]
                ]
                assert current == []
                web_messages = await web.get(f"/api/sessions/{main_id}/messages")
                assert web_messages.status_code == 200
                assert any(
                    item["id"] == other_reply["id"] and item["content"] == replies[phase]
                    for item in web_messages.json()["messages"]
                )
                assert not first_release.is_set()
                first_release.set()
                await asyncio.wait_for(second_entered.wait(), 4)
                assert len(b_sources) == 2 and b_sources[0] != b_sources[1]
                first_fact = frozen[b_sources[0]]["fact"]
                first_vector = runtime.store.db.execute(
                    "SELECT model_key FROM memory_vectors WHERE memory_id=?", (first_fact["id"],)
                ).fetchone()
                assert first_vector is not None and first_vector["model_key"] == key_b
                assert not second_release.is_set()
                # Normal application shutdown must cancel the actual second
                # HTTP call. It is not a process kill or a fabricated plan row.
                await asyncio.wait_for(app.state.close(), 3)
                assert cancelled_source == b_sources[1]
                gate_b = False
    finally:
        first_release.set()
        second_release.set()
        await runtime.close()

    if not complete_journey:
        return

    runtime = Runtime(ConfigFile(config_path), data_dir, client_factory=factory)  # type: ignore[arg-type]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert (
                runtime.store.instance_id == instance_id and runtime.main_session()["id"] == main_id
            )
            application = app.state.memory_application
            manager = app.state.channel_application.manager
            # The real vector writes are the progress condition, independent
            # of any model's or plan receipt's account of completion.
            await eventually(
                runtime,
                lambda: all(
                    (
                        row := runtime.store.db.execute(
                            "SELECT model_key FROM memory_vectors WHERE memory_id=?",
                            (snapshot["fact"]["id"],),
                        ).fetchone()
                    )
                    is not None
                    and row["model_key"] == key_b
                    for snapshot in frozen.values()
                ),
            )
            phase = "status"
            assert (
                await phone(web, "只查看刚才那个原计划的实际完成情况。", 26806, sender=8)
            ).status_code == 200
            await finish()
            assert status["state"] == "done" and status["model_key"] == key_b
            assert status["sources_total"] == status["sources_finished"] == len(materials)
            assert b_sources.count(b_sources[0]) == 1
            assert b_sources.count(cancelled_source) == 2
            assert extracted == list(materials)
            for identifier, snapshot in frozen.items():
                assert runtime.memory.source(identifier) == snapshot["source"]
                assert runtime.memory.get(snapshot["fact"]["id"]) == snapshot["fact"]
                assert application.pipeline.job(identifier) == snapshot["job"]
                assert [
                    dict(row)
                    for row in runtime.store.db.execute(
                        "SELECT * FROM memory_edges WHERE memory_id=? ORDER BY subject_id,predicate,object_id",
                        (snapshot["fact"]["id"],),
                    ).fetchall()
                ] == snapshot["edges"]
            phase = "recall"
            # Reuse the earlier query through the model tool call without
            # adding another lexical owner episode containing the query.
            # This journey verifies the rebuilt semantic vector, not the
            # top-three fusion order of repeated owner-message echoes.
            assert (
                await phone(
                    web,
                    "请沿用之前的不同措辞检索并读取实际来源全文，说明外部证据而不是主人事实。",
                    26807,
                    sender=8,
                )
            ).status_code == 200
            recalled_reply = await finish()
            assert found_source["id"] == source_id and source_pages
            await processed_reply(runtime, manager, recalled_reply["id"])
            deliveries = [
                item for item in manager.deliveries() if delivery_text(item) == replies[phase]
            ]
            assert deliveries == []
            web_messages = await web.get(f"/api/sessions/{main_id}/messages")
            assert web_messages.status_code == 200
            assert any(
                item["id"] == recalled_reply["id"] and item["content"] == replies[phase]
                for item in web_messages.json()["messages"]
            )
            before = list(b_sources)
            phase = "duplicate"
            assert (
                await phone(
                    web, "用同一个稳定键核对同一来源集合的原索引计划，不另建工作。", 26808, sender=8
                )
            ).status_code == 200
            await finish()
            assert duplicate["plan_id"] == plan_id and duplicate["state"] == "done"
            assert b_sources == before and extracted == list(materials)
    finally:
        await runtime.close()
