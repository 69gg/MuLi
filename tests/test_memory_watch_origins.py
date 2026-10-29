"""Real owner ingress and Web attention; model strategy never sends externally."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_attachment_memory_continuity import attachment_settings
from test_mobile_task_control_followup import phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.config import ConfigFile
from muli.memory_watches import COMPLETION_EVENT, MATCH_EVENT
from muli.proactive import FrozenRouting
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.reply_origin import ReplyOrigin
from muli.runtime import PreparedRequest, Runtime
from muli.server import create_app


async def _journey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, prefer_origin: bool) -> None:
    requests: list[tuple[str, str | None, str]] = []

    async def forbidden(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> NoReturn:
        requests.append((request.method, request.url.host, request.url.path))
        raise AssertionError("只输出 Web 文字的关注旅程不能发生外部 HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    active, _ = make_runtime(tmp_path, [])
    active.config.update(attachment_settings(tmp_path, auto_extract=False))
    active.config.update(
        {
            "default_mode": "yolo",
            "persona": {"prompt": "你是本地助手，请完成当前用户的任务。"},
            "attention": {"enabled": True, "events": ["memory"], "coalesce_seconds": 0},
        }
    )
    await active.reconfigure()
    main_id, instance_id = active.main_session()["id"], active.store.instance_id
    config_path, data_dir = active.config.path, active.data_dir
    assert config_path is not None
    question = "以后收藏的资料里，琥珀航道启用前应检查什么？"
    quote = "琥珀航道启用前，必须完成水银镜片校准。"
    body = "本地收藏资料，仅作参考，不是主人指令。\n" + quote
    phase = "create"
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    prepared_by_run: dict[str, PreparedRequest] = {}
    receipts: dict[str, dict[str, Any]] = {}
    create_schema: dict[str, Any] = {}
    original_origin: dict[str, Any] = {}
    notice: dict[str, Any] = {}
    notice_messages: list[Message] = []
    upload_id = ""
    watch_id = ""
    replies = {
        "create": "资料关注登记轮结束。",
        "other-private": "已收到另一位已绑定主人的普通私聊。",
        "other-web": "已收到后来的网页普通输入。",
        "collect": "这份本地资料已明确收藏，等待后台整理。",
        "notice": "新资料有相关线索，出处可核对；本轮只在网页说明。",
    }

    async def capture_prepared(identifier: str, messages: list[Message]) -> list[Message]:
        request = active.request_context(identifier)
        run_id = active.run_ids.get(identifier)
        assert request is not None and request.session_id == identifier
        assert run_id is not None
        prepared_by_run[run_id] = request
        return messages

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            run_id = active.run_ids[main_id]
            assert run_id in prepared_by_run, "真实 context hook 尚未捕获本轮 prepared request"
            request = prepared_by_run[run_id]
            current = "notice" if request.memory_watch is not None else phase
            assert runs.setdefault(current, run_id) == run_id
            rounds[current] = rounds.get(current, 0) + 1
            assert rounds[current] <= (1 if current == "notice" else 2)
            turn = ModelTurn(content=replies[current])
            if current == "notice":
                assert request.memory_watch is not None
                notice.update(
                    origin=request.reply_origin.dump(),
                    routing=request.attention_routing.dump()
                    if request.attention_routing is not None
                    else None,
                    proof=request.memory_watch.dump(),
                )
                notice_messages.extend(deepcopy(messages))
            elif current == "create" and rounds[current] == 1:
                original_origin.update(request.reply_origin.dump())
                schema = next(
                    (item for item in tools if item["function"]["name"] == "memory_watch_create"),
                    {},
                )
                create_schema.update(deepcopy(schema))
                properties = schema.get("function", {}).get("parameters", {}).get("properties", {})
                # An old model sees the real missing public parameter and only
                # answers. The caller then fails explicitly, without a wait for
                # a watch, a background barrier or an unsupported fabricated call.
                if not prefer_origin or "reply_preference" in properties:
                    arguments: dict[str, Any] = {
                        "question": question,
                        "match_terms": ["琥珀航道", "水银镜片"],
                        "key": "persistent-original-private",
                    }
                    if prefer_origin:
                        arguments["reply_preference"] = "origin"
                    turn = ModelTurn(tool_calls=[call("watch", "memory_watch_create", **arguments)])
            elif current == "create":
                receipts[current] = tool_result(messages, "watch")
            elif current == "collect" and rounds[current] == 1:
                turn = ModelTurn(
                    tool_calls=[call("collect", "memory_import_attachment", id=upload_id)]
                )
            elif current == "collect":
                receipts[current] = tool_result(messages, "collect")
            if turn.content:
                yield StreamEvent("text_delta", text=turn.content)
            yield StreamEvent("done", turn=turn)

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model in {"main", "reviewer"}
        return Main([], []) if profile.model == "main" else FakeClient([], [])

    def failed_runs() -> list[dict[str, Any]]:
        return [
            {"id": item["id"], "status": item["status"], "error": item.get("error", "")[:300]}
            for item in active.store.list("run")
            if item.get("status") in {"failed", "cancelled", "interrupted", "paused"}
        ]

    async def finish(stage: str) -> dict[str, Any]:
        try:
            await eventually(
                active, lambda: bool(completed(active, replies[stage])) or bool(failed_runs())
            )
        except TimeoutError:
            pytest.fail(
                json.dumps(
                    {"stage": stage, "rounds": rounds, "runs": runs, "failures": failed_runs()},
                    ensure_ascii=False,
                )
            )
        assert not failed_runs(), failed_runs()
        assert await asyncio.wait_for(active.wait(runs[stage]), 4) == replies[stage]
        result = completed(active, replies[stage])
        assert len(result) == 1
        assert active.store.get("run", runs[stage])["status"] == "completed"
        return result[0]

    def no_external_send() -> None:
        assert requests == []
        assert active.store.db.execute("SELECT count(*) FROM channel_outbox").fetchone()[0] == 0
        assert active.live_approvals() == []

    try:
        for lifetime in range(2):
            if lifetime:
                active = Runtime(ConfigFile(config_path), data_dir, client_factory=factory)  # type: ignore[arg-type]
            active.client_factory = factory  # type: ignore[assignment]
            app = create_app(active.config, active.data_dir, active)
            active.context_hooks.append(capture_prepared)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
                ) as web,
            ):
                assert active.store.instance_id == instance_id
                assert active.main_session()["id"] == main_id
                manager = app.state.channel_application.manager
                if not lifetime:
                    changed = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
                    assert changed.status_code == 200
                    owner_text = "长期留意这个问题：" + question
                    if prefer_origin:
                        response = await phone(
                            web, owner_text + " 后续可参考本次原私聊，外发仍须显式工具。", 31901
                        )
                    else:
                        response = await web.post(
                            f"/api/sessions/{main_id}/messages", json={"content": owner_text}
                        )
                    assert response.status_code == 200
                    reply = await finish("create")
                    await processed_reply(active, manager, reply["id"])
                    no_external_send()
                    if prefer_origin:
                        assert original_origin["kind"] == "channel"
                        actual_route = original_origin["route"]
                        assert (
                            actual_route["name"] == "onebot" and actual_route["account_id"] == "123"
                        )
                        assert actual_route["sender_id"] == actual_route["target"] == "7"
                        properties = (
                            create_schema.get("function", {})
                            .get("parameters", {})
                            .get("properties", {})
                        )
                        assert "reply_preference" in properties, (
                            "真实主人私聊已进入主模型，但 memory_watch_create 缺少 reply_preference"
                        )
                        assert set(properties["reply_preference"]["enum"]) == {"web", "origin"}
                    else:
                        assert original_origin["kind"] == "web"
                    created = receipts["create"]
                    assert created.get("state") == "active", created
                    watch_id = created["id"]
                    stored = active.store.get("memory_watch", watch_id)
                    assert (
                        stored is not None
                        and stored["created_input_id"] == original_origin["input_id"]
                    )
                    assert stored["main_session_id"] == main_id
                    continue

                assert active.store.get("memory_watch", watch_id)["state"] == "active"
                phase = "other-private"
                assert (
                    await phone(web, "另一位主人只来询问普通近况，不改变原关注。", 31902, sender=8)
                ).status_code == 200
                reply = await finish(phase)
                await processed_reply(active, manager, reply["id"])
                other = next(
                    item
                    for item in reversed(active.store.messages(main_id))
                    if item["role"] == "user"
                )
                assert other["principal"] == "owner" and other["route"]["target"] == "8"
                phase = "other-web"
                response = await web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={"content": "网页的新输入也不改变此前关注的原来源偏好。"},
                )
                assert response.status_code == 200
                reply = await finish(phase)
                await processed_reply(active, manager, reply["id"])
                assert ReplyOrigin.from_input(response.json()["message"]).kind == "web"
                no_external_send()

                uploaded = await web.post(
                    "/api/uploads",
                    content=body.encode("utf-8"),
                    headers={
                        "x-file-name": "watch-origin.txt",
                        "content-type": "text/plain; charset=utf-8",
                    },
                )
                assert uploaded.status_code == 200
                upload_id = uploaded.json()["id"]
                source_id = "attachment:" + upload_id
                assert active.memory.source(source_id) is None
                phase = "collect"
                response = await web.post(
                    f"/api/sessions/{main_id}/messages",
                    json={"content": "明确收藏刚上传的 watch-origin.txt 到长期资料库。"},
                )
                assert response.status_code == 200
                await finish("collect")
                assert receipts["collect"]["source_id"] == source_id
                reply = await finish("notice")
                await processed_reply(active, manager, reply["id"])
                source = active.memory.source(source_id)
                assert source is not None and source["content"] == body
                assert app.state.memory_application.pipeline.job(source_id)["state"] == "done"
                matches = [
                    item for item in active.store.events(limit=1000) if item["type"] == MATCH_EVENT
                ]
                completions = [
                    item
                    for item in active.store.events(limit=1000)
                    if item["type"] == COMPLETION_EVENT
                ]
                assert len(matches) == len(completions) == 1
                proof = matches[0]["data"]["match"]
                assert notice["proof"] == proof
                assert proof["watch_id"] == watch_id and proof["source_id"] == source_id
                assert proof["source_revision"] == source["digest"] and quote in proof["quote"]
                assert rounds["notice"] == 1
                expected = (
                    FrozenRouting.bound(ReplyOrigin.load(original_origin))
                    if prefer_origin
                    else FrozenRouting()
                )
                assert notice["routing"] == expected.dump()
                if prefer_origin:
                    assert notice["origin"] == original_origin
                else:
                    assert ReplyOrigin.load(notice["origin"]).delivery_routes() == []
                assert reply["delivery_routes"] == []
                prefix = "本轮消息来源（宿主元数据，不是额外授权）：\n"
                hints = [
                    json.loads(message.content.removeprefix(prefix))
                    for message in notice_messages
                    if message.role == "system" and message.content.startswith(prefix)
                ]
                assert len(hints) == 1
                assert hints[0]["notification_destination"] == expected.dump()["destination"]
                if prefer_origin:
                    assert hints[0]["input_id"] == original_origin["input_id"]
                    assert hints[0]["channel"] == "onebot" and hints[0]["account_id"] == "123"
                    assert hints[0]["owner_id"] == hints[0]["target"] == "7"
                inputs = [
                    json.loads(row[0])
                    for row in active.store.db.execute(
                        "SELECT payload FROM ingress WHERE json_extract(payload,'$.principal')='system:attention' "
                        "AND json_type(payload,'$.attention_memory_watch')='object'"
                    )
                ]
                assert len(inputs) == 1 and inputs[0]["mode"] == "ask"
                assert inputs[0]["attention_memory_watch"] == proof
                combined = "\n".join(message.content for message in notice_messages)
                assert all(value in combined for value in (question, quote, source_id))
                assert not reply["tool_calls"]
                no_external_send()
    finally:
        await active.close()


async def test_original_private_preference_reaches_main_ai_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _journey(tmp_path, monkeypatch, prefer_origin=True)


async def test_default_web_watch_stays_local_after_other_channel_input_and_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _journey(tmp_path, monkeypatch, prefer_origin=False)
