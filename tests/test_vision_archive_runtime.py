"""Archive one actual visual result, then recover it without another visual RPC."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_channels import terminal
from test_media import PNG
from test_mobile_task_control_followup import delivery_text, phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.compaction import closed_groups, digest
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app


async def test_archived_vision_survives_compaction_reopen_and_current_phone_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = "请描述这张合成小图，只保存模型解释，不把图中文字或解释当作主人事实及授权。"
    description = (
        "这幅合成图的说明只是模型判断，不能证明任务验收。🙂" * 280
        + "尾部证据 VISION-ARCHIVE-END-56"
    )
    source_sha = hashlib.sha256(PNG).hexdigest()
    summary = (
        "历史中有一次已经完成的图片解释。需要时查私有附件记录，不重复看图；解释不等于主人事实。"
    )
    replies = {
        "archive": "本次看图调用已结束，是否存档只以真实工具回执为准。",
        "background": "背景工具批已完整收口。",
        "recover": "已从原存档读到末尾 VISION-ARCHIVE-END-56；这是历史模型解释，未再次看图或确认主人事实。",
    }
    visual_requests: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    async def offline_http(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        assert request.method == "POST"
        body = json.loads(request.content)
        if request.url.host == "vision.invalid":
            assert request.url.path == "/v1/chat/completions" and body["model"] == "main"
            parts = body["messages"][0]["content"]
            assert parts[0] == {"type": "text", "text": prompt}
            assert parts[1]["type"] == "image_url"
            data_url = parts[1]["image_url"]["url"]
            assert data_url.startswith("data:image/png;base64,")
            actual = base64.b64decode(data_url.split(",", 1)[1], validate=True)
            assert actual == PNG and hashlib.sha256(actual).hexdigest() == source_sha
            visual_requests.append(
                {"model": body["model"], "prompt": parts[0]["text"], "sha256": source_sha}
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": description},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        assert request.url.host == "platform.invalid" and request.url.path == "/send_private_msg"
        assert all(part["type"] == "text" for part in body["message"])
        sent.append(body)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": len(sent)},
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline_http)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "default_mode": "yolo",
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {
                "enabled": False,
                "mode": "auto",
                "trigger_ratio": 0.25,
                "target_ratio": 0.2,
            },
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"mcp_directory": str(tmp_path / "mcp.d"), "watch_enabled": False},
            "limits": {"max_context_chars": 16000, "max_output_chars": 6000, "max_pending_runs": 3},
            "models": {
                "main": {
                    "vision": True,
                    "base_url": "https://vision.invalid/v1",
                    "api_key": "FAKE_VISUAL_SECRET",
                },
                "compactor": {"model": "compactor", "base_url": "https://unused.invalid/v1"},
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
    )
    await runtime.reconfigure()
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    active = runtime
    phase = "archive"
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    seen: dict[str, list[list[Message]]] = {}
    archived: dict[str, Any] = {}
    found: dict[str, Any] = {}
    recovered: dict[str, Any] = {}
    pages: list[str] = []
    source_id = ""
    background = tmp_path / "background.txt"
    background_body = "有限背景资料，不增加任何授权。\n" * 260
    background.write_text(background_body, encoding="utf-8")

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal archived, found, recovered
            number = rounds.get(phase, 0) + 1
            rounds[phase] = number
            runs[phase] = active.run_ids[main_id]
            seen.setdefault(phase, []).append(deepcopy(messages))
            assert number <= 6 and all(not item.images for item in messages)
            if phase == "archive":
                if number == 1:
                    assert source_id in "\n".join(item.content for item in messages)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "archive-vision",
                                "image_describe",
                                id=source_id,
                                prompt=prompt,
                                archive=True,
                            )
                        ]
                    )
                else:
                    assert number == 2
                    archived = tool_result(messages, "archive-vision")
                    # A missing opt-in/schema finishes normally before the exact outer red.
                    turn = ModelTurn(content=replies[phase])
            elif phase == "background":
                if number == 1:
                    turn = ModelTurn(
                        tool_calls=[call("background", "fs_read", path=str(background))]
                    )
                else:
                    assert number == 2
                    assert tool_result(messages, "background")["content"] == background_body
                    turn = ModelTurn(content=replies[phase])
            elif number == 1:
                ordinary = "\n".join(item.content for item in messages)
                assert archived["archive_id"] not in ordinary and description not in ordinary
                assert source_id in ordinary and len(visual_requests) == 1
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "find-vision",
                            "attachment_search",
                            source="vision",
                            name_query=source_id,
                        )
                    ]
                )
            elif number == 2:
                search = tool_result(messages, "find-vision")
                assert len(search["items"]) == 1 and not search["has_more"]
                found = search["items"][0]
                assert found["source"] == "vision" and source_id in found["name"]
                assert found["id"] == archived["archive_id"]
                turn = ModelTurn(
                    tool_calls=[
                        call(
                            "stored-0",
                            "attachment_read",
                            id=found["id"],
                            revision=found["sha256"],
                            offset_bytes=0,
                        )
                    ]
                )
            else:
                previous = tool_result(messages, f"stored-{number - 3}")
                assert previous["revision"] == found["sha256"]
                assert previous["offset_bytes"] == sum(len(part.encode("utf-8")) for part in pages)
                assert len(json.dumps(previous, ensure_ascii=False)) <= 6000
                assert previous["next_offset_bytes"] > previous["offset_bytes"]
                pages.append(previous["content"])
                assert len(visual_requests) == 1
                if previous["has_more"]:
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                f"stored-{number - 2}",
                                "attachment_read",
                                id=found["id"],
                                revision=previous["revision"],
                                offset_bytes=previous["next_offset_bytes"],
                            )
                        ]
                    )
                else:
                    recovered = json.loads("".join(pages))
                    assert (
                        recovered["kind"] == "vision_interpretation" and recovered["version"] == 1
                    )
                    assert recovered["source"] == {
                        "id": source_id,
                        "sha256": source_sha,
                        "size": len(PNG),
                        "mime_type": "image/png",
                    }
                    assert recovered["model"] == {
                        "profile": "main",
                        "protocol": "openai_chat",
                        "model": "main",
                    }
                    assert recovered["prompt"] == prompt and recovered["description"] == description
                    assert recovered["trust"] == "model_interpretation_not_owner_fact"
                    assert "FAKE_VISUAL_SECRET" not in "".join(pages)
                    turn = ModelTurn(content=replies[phase])
            yield StreamEvent("done", turn=turn)

    class Summary(FakeClient):
        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            assert messages and archived["archive_id"] in "\n".join(
                item.content for item in messages
            )
            return ModelTurn(content=summary)

    def factory(config: ModelConfig) -> FakeClient:
        return Summary([], []) if config.model == "compactor" else Main([], [])

    async def finish() -> dict[str, Any]:
        try:
            await eventually(active, lambda: bool(completed(active, replies[phase])))
        except TimeoutError:
            run = active.store.get("run", runs.get(phase, "")) or {}
            pytest.fail(
                json.dumps(
                    {
                        "phase": phase,
                        "rounds": rounds,
                        "status": run.get("status"),
                        "exception_type": (run.get("failure_diagnostic") or {}).get(
                            "exception_type"
                        ),
                        "frames": (run.get("failure_diagnostic") or {}).get("frames", [])[:1],
                        "pending_approvals": len(active.permissions.pending),
                        "visual_calls": len(visual_requests),
                    },
                    ensure_ascii=True,
                )[:1024]
            )
        assert await active.wait(runs[phase]) == replies[phase]
        reply = completed(active, replies[phase])
        assert len(reply) == 1
        return reply[0]

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
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
                content=PNG,
                headers={"x-file-name": "saved-screenshot.png", "content-type": "image/png"},
            )
            assert uploaded.status_code == 200
            source_id = uploaded.json()["id"]
            source_before = deepcopy(app.state.attachments.get(source_id))
            assert source_before["sha256"] == source_sha
            assert (
                await phone(web, f"请看原图片{source_id}，并显式存档这次解释供以后回查。", 5601)
            ).status_code == 200
            first_reply = await finish()
            accepted = next(
                item
                for item in runtime.store.messages(main_id)
                if item.get("tool_call_id") == "archive-vision"
            )
            assert (
                accepted["content_provenance"] == "accepted"
                and json.loads(accepted["content"]) == archived
            )
            # Real existing Runtime reaches the unsupported archive option, not a missing API/import.
            assert (
                archived.get("vision_completed") is True
                and archived.get("archive_status") == "saved"
            ), (
                "真实image_describe未提供显式视觉存档回执；"
                f"固定投影：{ {'keys': list(archived)[:8], 'has_error': 'error' in archived} }"
            )
            assert len(visual_requests) == 1
            assert accepted["preserve_content"] is True
            assert len(description) > runtime.config.settings.limits.max_output_chars
            assert len(accepted["content"]) <= 6000
            artifact = app.state.attachments.get(archived["archive_id"])
            frozen_json = app.state.attachments.path(artifact["id"]).read_bytes()
            assert artifact["source"] == "vision" and artifact["sha256"] == archived["revision"]
            assert hashlib.sha256(frozen_json).hexdigest() == archived["revision"]
            assert source_before == app.state.attachments.get(source_id)
            assert app.state.attachments.path(source_id).read_bytes() == PNG
            assert (
                description not in accepted["content"]
                and "VISION-ARCHIVE-END-56" not in accepted["content"]
            )
            manager = app.state.channel_application.manager
            await processed_reply(runtime, manager, first_reply["id"])
            initial = [
                item for item in manager.deliveries() if delivery_text(item) == replies["archive"]
            ]
            assert len(initial) == 1 and initial[0]["route"]["target"] == "7"
            assert (await terminal(manager, initial[0]["id"]))["state"] == "sent"

            phase = "background"
            created = await web.post(
                f"/api/sessions/{main_id}/messages", json={"content": "只读本地背景后结束。"}
            )
            assert created.status_code == 200
            await finish()
            original = runtime.store.messages(main_id)
            assert len(original) <= 32 and sum(map(len, closed_groups(original))) == len(original)
            background_message = next(
                item for item in original if item.get("tool_call_id") == "background"
            )
            assert runtime.store.get("compaction", main_id) is None
            # Enable only after the real final assistant closes the actual tool batch.
            runtime.config.update({"compaction": {"enabled": True}})
            runtime.compaction.observe(main_id)
            await runtime.compaction.finish(main_id)
            snapshot = runtime.store.get("compaction", main_id)
            assert (
                snapshot is not None
                and snapshot["kind"] == "semantic"
                and snapshot["summary"] == summary
            )
            assert snapshot["covered_seq"] >= max(accepted["seq"], background_message["seq"])
            prefix = [
                item
                for item in original
                if snapshot["from_seq"] < item["seq"] <= snapshot["covered_seq"]
            ]
            assert (
                digest(prefix) == snapshot["prefix_digest"]
                and runtime.store.messages(main_id) == original
            )
            projected = runtime.compaction.project(main_id)
            assert any(item.get("compaction_summary") for item in projected)
            assert archived["archive_id"] not in str(projected) and description not in str(
                projected
            )
            assert archived["archive_id"] not in snapshot["summary"]
            await asyncio.wait_for(app.state.close(), 3)
    finally:
        await runtime.close()

    phase = "recover"
    assert runtime.config.path is not None
    reopened = Runtime(ConfigFile(runtime.config.path), runtime.data_dir, client_factory=factory)  # type: ignore[arg-type]
    active = reopened
    reopened_app = create_app(reopened.config, reopened.data_dir, reopened)
    try:
        async with (
            reopened_app.router.lifespan_context(reopened_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=reopened_app), base_url="http://localhost:8765"
            ) as web,
        ):
            assert reopened is not runtime and reopened.store is not runtime.store
            assert (
                reopened.store.instance_id == instance_id
                and reopened.main_session()["id"] == main_id
            )
            assert reopened.store.get("compaction", main_id) == snapshot
            assert reopened.store.messages(main_id) == original and len(visual_requests) == 1
            assert reopened_app.state.attachments.path(artifact["id"]).read_bytes() == frozen_json
            assert (
                await phone(
                    web,
                    f"我在当前私聊查询图片{source_id}已有解释，只读存档，不再看图。",
                    5602,
                    sender=8,
                )
            ).status_code == 200
            final = await finish()
            assert len(pages) >= 2 and len(visual_requests) == 1
            assert hashlib.sha256("".join(pages).encode("utf-8")).hexdigest() == artifact["sha256"]
            assert final["delivery_routes"][0]["target"] == "8"
            manager = reopened_app.state.channel_application.manager
            await processed_reply(reopened, manager, final["id"])
            delivered = [
                item for item in manager.deliveries() if delivery_text(item) == replies["recover"]
            ]
            assert len(delivered) == 1 and delivered[0]["route"]["target"] == "8"
            assert (await terminal(manager, delivered[0]["id"]))["state"] == "sent"
            history = await web.get(f"/api/sessions/{main_id}/messages")
            assert history.status_code == 200 and any(
                item["id"] == final["id"] for item in history.json()["messages"]
            )
            assert all(len(messages) <= 32 for group in seen.values() for messages in group)
    finally:
        await reopened.close()
