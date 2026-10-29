"""Explicit corrections through real owner ingress, sources and Runtime tools.

The missing tool is the baseline entry red. Later correction and reopen
assertions are exercised only after that boundary succeeds.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_continuity import attachment_settings, platform_reply
from test_mobile_task_control_followup import phone
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import completed, eventually

from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent, ToolCall
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import new_id

_SOURCE = "琥珀备库替换旧副本前，必须先离线验证新副本。原文仅提供外部资料。"
_WRONG = "琥珀备库应先覆盖旧副本，再验证新副本。"
_CORRECT = "琥珀备库应先离线验证新副本，再决定是否替换旧副本。"
_OLD_RELATION = {"subject": "琥珀备库", "predicate": "首先执行", "object": "覆盖旧副本"}
_NEW_RELATION = {"subject": "琥珀备库", "predicate": "首先执行", "object": "离线验证新副本"}


async def owner_tool(
    runtime: Runtime,
    web: httpx.AsyncClient,
    text: str,
    name: str,
    message_id: int,
    *,
    sender: int = 7,
    **arguments: Any,
) -> dict[str, Any]:
    """Invoke even an absent tool without bypassing the real owner request."""
    call_id = "memory-correct-" + new_id()
    reply = "本轮操作结束，以真实工具回执为准：" + call_id
    turns = [
        ModelTurn(tool_calls=[ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))]),
        ModelTurn(content=reply),
    ]
    run_id = ""

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal run_id
            current = runtime.run_ids[runtime.main_session()["id"]]
            assert not run_id or current == run_id
            run_id = current
            async for event in super().stream(messages, tools):
                yield event

    def factory(profile: ModelConfig) -> FakeClient:
        return Main(turns, []) if profile.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    response = await phone(web, text, message_id, sender=sender)
    assert response.status_code == 200
    await eventually(runtime, lambda: bool(completed(runtime, reply)))
    assert await asyncio.wait_for(runtime.wait(run_id), timeout=3) == reply
    run = runtime.store.get("run", run_id)
    assert run is not None and run["status"] == "completed"
    accepted = [
        item
        for item in runtime.store.messages(runtime.main_session()["id"])
        if item.get("tool_call_id") == call_id
    ]
    assert len(accepted) == 1 and accepted[0]["content_provenance"] == "accepted"
    result = json.loads(accepted[0]["content"])
    assert isinstance(result, dict)
    return result


async def test_owner_corrects_one_existing_fact_and_other_channel_reads_it_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    async def offline_http(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return platform_reply(request, sent)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=True))
    runtime.config.update(
        {
            "default_mode": "yolo",
            "persona": {"prompt": "你是本地助手，请完成当前用户的任务。"},
            "memory": {"context_max_chars": 0},
        }
    )
    await runtime.reconfigure()
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    config_path, data_dir = runtime.config.path, runtime.data_dir
    source_id = ""
    extracted: list[str] = []

    async def extract(source: dict[str, Any]) -> dict[str, Any]:
        if source["id"] != source_id:
            return {"memories": [], "aliases": {}}
        assert source["content"] == _SOURCE and source["trusted_owner"] is False
        extracted.append(source["id"])
        # Only the model's fallible proposal is controlled. Registration,
        # validation, fact creation and the job terminal state remain real.
        return {
            "memories": [
                {
                    "layer": "semantic",
                    "content": _WRONG,
                    "quote": _SOURCE,
                    "entities": ["琥珀备库", "覆盖旧副本"],
                    "relations": [_OLD_RELATION],
                }
            ],
            "aliases": {},
        }

    app = create_app(runtime.config, data_dir, runtime)
    application = app.state.memory_application
    application.pipeline.extractor = extract
    try:
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
                content=_SOURCE.encode("utf-8"),
                headers={"x-file-name": "reference.txt", "content-type": "text/plain"},
            )
            assert uploaded.status_code == 200
            identifier = uploaded.json()["id"]
            source_id = "attachment:" + identifier
            assert runtime.memory.source(source_id) is None
            imported = await owner_tool(
                runtime,
                web,
                "请明确收藏这份外部资料。",
                "memory_import_attachment",
                58101,
                id=identifier,
            )
            assert "error" not in imported and imported["source_id"] == source_id
            await eventually(
                runtime,
                lambda: bool(
                    (job := application.pipeline.job(source_id)) and job["state"] == "done"
                ),
            )
            assert extracted == [source_id]
            found = await owner_tool(
                runtime,
                web,
                "查找琥珀备库的那条备份结论。",
                "memory_query",
                58102,
                query="琥珀备库",
            )
            assert "error" not in found
            facts = [
                item
                for item in found["memories"]
                if item["source_id"] == source_id and item["kind"] == "fact"
            ]
            assert len(facts) == 1 and facts[0]["content"] == _WRONG
            before = facts[0]
            fact_id, version = before["id"], before["version"]
            assert before["relations"] == [_OLD_RELATION]
            original = runtime.memory.source(source_id)
            assert original is not None and original["content"] == _SOURCE
            assert original["principal"] == "external:attachment"
            assert original["role"] == "tool" and original["trusted_owner"] is False
            source = await owner_tool(
                runtime,
                web,
                "核对这条结论的完整来源原文。",
                "memory_source",
                58103,
                id=source_id,
            )
            assert "error" not in source and source["content"] == _SOURCE
            assert source["field"] == "content" and source["offset_bytes"] == 0
            assert source["length_bytes"] == source["total_bytes"] == len(_SOURCE.encode("utf-8"))
            assert source["next_offset_bytes"] == source["total_bytes"]
            assert source["has_more"] is False
            assert source["truncated"] is False and source["revision"] == original["digest"]
            assert runtime.memory.revisions(fact_id) == []
            assert fact_id in {item["id"] for item in runtime.memory.search("覆盖", hops=0)}
            # A local vector fixture proves invalidation, not a paid provider call.
            assert runtime.memory.store_vector(fact_id, [1.0, 0.0], "fixture-vector")

            result = await owner_tool(
                runtime,
                web,
                "刚才那条提炼把顺序记反了。只更正这条为先离线验证新副本，再决定是否替换旧副本。",
                "memory_correct",
                58104,
                id=fact_id,
                expected_version=version,
                content=_CORRECT,
                entities=["琥珀备库", "离线验证新副本"],
                relations=[_NEW_RELATION],
            )
            # Formal entry red: persistence, graph and reopen assertions below
            # are not evidence until this real accepted tool result succeeds.
            assert "error" not in result, (
                f"memory correction entry gap; later boundaries untested: {result}"
            )
            assert result["id"] == fact_id and result["changed"] is True
            assert result["old_version"] == version and result["new_version"] == version + 1
            changed = runtime.memory.get(fact_id)
            assert changed is not None and changed["content"] == _CORRECT
            assert changed["version"] == version + 1 and changed["relations"] == [_NEW_RELATION]
            for key in ("source_id", "source", "trusted_owner", "quote", "kind", "layer"):
                assert changed[key] == before[key]
            assert runtime.memory.source(source_id) == original
            history = runtime.memory.revisions(fact_id)
            assert len(history) == 1 and history[0]["snapshot"]["content"] == _WRONG
            assert runtime.memory.vector_count("fixture-vector") == 0
            assert all(item["id"] != fact_id for item in runtime.memory.search("覆盖", hops=0))
            assert _OLD_RELATION not in changed["relations"]
    finally:
        await runtime.close()

    reopened = Runtime(ConfigFile(config_path), data_dir)
    reopened_app = create_app(reopened.config, data_dir, reopened)
    reopened_app.state.memory_application.pipeline.extractor = extract
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert reopened.store.instance_id == instance_id
            assert reopened.main_session()["id"] == main_id
            found = await owner_tool(
                reopened,
                web,
                "从另一条主人私聊核查琥珀备库的更正结果。",
                "memory_query",
                58105,
                sender=8,
                query="琥珀备库",
            )
            assert "error" not in found
            current = next(item for item in found["memories"] if item["id"] == fact_id)
            assert current["content"] == _CORRECT and current["version"] == version + 1
            assert current["relations"] == [_NEW_RELATION]
            assert reopened.memory.source(source_id) == original
            assert reopened.memory.revisions(fact_id) == history
            assert extracted == [source_id]
    finally:
        await reopened.close()
