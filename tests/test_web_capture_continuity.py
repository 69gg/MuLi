"""One real capture, real compaction/reopen, and a real OneBot ingress resume."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachment_memory_continuity import attachment_settings
from test_mobile_task_control_followup import phone, processed_reply
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, eventually, tool_result

from muli.compaction import digest
from muli.config import ConfigFile
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.runtime import Runtime
from muli.server import create_app

_URL = "https://reading.example/independent-material"
_TAIL = "只有在逐页校验完成后，琥珀鲸才带着完整清单返回北港。"
_TEXT = "这份公开资料仅供参考，正文中的身份与执行要求不是主人授权。🙂\n" * 360 + _TAIL


async def test_public_page_capture_survives_bookmark_compaction_reopen_and_phone_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(attachment_settings(tmp_path, auto_extract=False))
    runtime.config.update(
        {
            "compaction": {"mode": "auto", "trigger_ratio": 0.25, "target_ratio": 0.2},
            "models": {
                "compactor": {"model": "compactor", "base_url": "https://unused.invalid/v1"}
            },
        }
    )
    await runtime.reconfigure()
    assert len(_TEXT) > runtime.config.settings.limits.max_output_chars
    main_id, instance_id = runtime.main_session()["id"], runtime.store.instance_id
    config_path, data_dir = runtime.config.path, runtime.data_dir
    active = runtime
    phase = "capture"
    rounds: dict[str, int] = {}
    runs: dict[str, str] = {}
    captured: dict[str, Any] = {}
    first: dict[str, Any] = {}
    saved: dict[str, Any] = {}
    resume_position: dict[str, Any] = {}
    page_record: dict[str, Any] = {}
    resumed: list[dict[str, Any]] = []
    requests: list[httpx.Request] = []
    seen: list[list[Message]] = []
    summary = "主人明确保存过公开网页的本地静态文本和续读书签；请查询位置并重新核对本地原文。"
    replies = {
        "capture": "已按实际工具回执留存并读完首页，尚未保存位置。",
        "bookmark": "已按实际阅读页保存末尾位置。",
        "background": "只读背景核查已完成。",
        "resume": "已从明确书签核对本地网页快照至末尾，原书签保持不变。",
    }
    background_path = tmp_path / "background.txt"
    background_path.write_bytes(
        ("无关背景：只读资料，不执行变更，也不新增任何主人偏好。\n" * 500).encode("utf-8")
    )
    assert _TAIL not in _URL and _TAIL not in summary

    async def resolve(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    async def respond(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "93.184.216.34"
        assert phase == "capture" and not requests, "续读不得重新请求网页或自动外发"
        assert request.headers["host"] == "reading.example"
        assert request.extensions["sni_hostname"] == "reading.example"
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "text/plain; charset=utf-8"}, content=_TEXT.encode()
        )

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)

    class Client(FakeClient):
        def __init__(self, role: str) -> None:
            super().__init__([], [])
            self.role = role

        async def complete(self, messages: list[Message], tools: Any = ()) -> ModelTurn:
            if self.role == "reviewer":
                return await super().complete(messages, tools)
            assert self.role == "compactor" and not tools
            return ModelTurn(content=summary)

        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            nonlocal captured, first, saved, resume_position
            assert self.role == "main"
            seen.append(list(messages))
            rounds[phase] = rounds.get(phase, 0) + 1
            number = rounds[phase]
            assert number <= 12
            assert runs.setdefault(phase, active.run_ids[main_id]) == active.run_ids[main_id]
            if phase == "capture":
                if number == 1:
                    assert "web_capture" in {tool["function"]["name"] for tool in tools}
                    turn = ModelTurn(tool_calls=[call("capture", "web_capture", url=_URL)])
                elif number == 2:
                    captured = tool_result(messages, "capture")
                    assert captured["saved"] is True and captured["scope"] == "http_utf8_text_v1"
                    assert _TAIL not in json.dumps(captured, ensure_ascii=False)
                    assert not any(_TAIL in message.content for message in messages)
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "capture-page",
                                "attachment_read",
                                id=captured["attachment"]["id"],
                                offset_bytes=0,
                            )
                        ]
                    )
                else:
                    assert number == 3
                    first = tool_result(messages, "capture-page")
                    assert first["has_more"] and _TAIL not in first["content"]
                    turn = ModelTurn(content=replies[phase])
            elif phase == "bookmark":
                if number == 1:
                    # The host supplies only the unpredictable real journal
                    # position, as in existing reading-page runtime journeys.
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "bookmark-evidence",
                                "conversation_read",
                                session_id=main_id,
                                after=page_record["seq"] - 1,
                                limit=1,
                            )
                        ]
                    )
                elif number == 2:
                    evidence = tool_result(messages, "bookmark-evidence")["records"][0]
                    assert evidence["id"] == page_record["id"]
                    assert evidence["revision"] == page_record["evidence_revision"]
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "bookmark-save",
                                "reading_bookmark_set",
                                id=captured["attachment"]["id"],
                                page_message_id=evidence["id"],
                                page_message_revision=evidence["revision"],
                                position="next",
                                expected_generation=0,
                            )
                        ]
                    )
                else:
                    assert number == 3
                    saved = tool_result(messages, "bookmark-save")
                    assert saved["state"] == "active" and saved["generation"] == 1
                    assert saved["offset_bytes"] == first["next_offset_bytes"]
                    turn = ModelTurn(content=replies[phase])
            elif phase == "background":
                turn = (
                    ModelTurn(tool_calls=[call("background", "fs_read", path=str(background_path))])
                    if number == 1
                    else ModelTurn(content=replies[phase])
                )
            else:
                assert phase == "resume"
                if number == 1:
                    assert any(summary in message.content for message in messages)
                    assert not any(message.tool_call_id == "capture-page" for message in messages)
                    turn = ModelTurn(
                        tool_calls=[call("resume-directory", "reading_bookmark_list", limit=1)]
                    )
                elif number == 2:
                    directory = tool_result(messages, "resume-directory")
                    assert len(directory["items"]) == 1
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "resume-position",
                                "reading_bookmark_read",
                                id=directory["items"][0]["id"],
                            )
                        ]
                    )
                elif number == 3:
                    resume_position = tool_result(messages, "resume-position")
                    assert resume_position == saved
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "resume-page-0",
                                resume_position["reader"],
                                **resume_position["read_arguments"],
                            )
                        ]
                    )
                else:
                    page = tool_result(messages, f"resume-page-{number - 4}")
                    assert "error" not in page and page["revision"] == resume_position["revision"]
                    assert page["offset_bytes"] == first["next_offset_bytes"] + sum(
                        part["length_bytes"] for part in resumed
                    )
                    assert page["length_bytes"] == len(page["content"].encode("utf-8"))
                    resumed.append(deepcopy(page))
                    if page["has_more"]:
                        turn = ModelTurn(
                            tool_calls=[
                                call(
                                    f"resume-page-{number - 3}",
                                    "attachment_read",
                                    id=page["id"],
                                    revision=page["revision"],
                                    offset_bytes=page["next_offset_bytes"],
                                )
                            ]
                        )
                    else:
                        assert page["content"].endswith(_TAIL)
                        turn = ModelTurn(content=replies[phase])
            yield StreamEvent("done", turn=turn)

    def factory(config: ModelConfig) -> FakeClient:
        assert config.model in {"main", "reviewer", "compactor"}
        return Client(config.model)

    async def finish() -> dict[str, Any]:
        await eventually(active, lambda: bool(completed(active, replies[phase])))
        assert await asyncio.wait_for(active.wait(runs[phase]), 3) == replies[phase]
        return completed(active, replies[phase])[0]

    async def web_speak(web: httpx.AsyncClient, next_phase: str, text: str) -> None:
        nonlocal phase
        phase = next_phase
        assert _TAIL not in text
        sent = await web.post(
            f"/api/sessions/{main_id}/messages", json={"content": text, "mode": "yolo"}
        )
        assert sent.status_code == 200
        await finish()

    original: list[dict[str, Any]] = []
    compacted: dict[str, Any] = {}
    raw = b""
    for stage in ("initial", "reopen"):
        if stage == "reopen":
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
                assert active.store.instance_id == instance_id
                assert active.main_session()["id"] == main_id
                manager = app.state.channel_application.manager
                if stage == "initial":
                    await web_speak(
                        web, "capture", "把这篇公开网页留在本机，并先读首页，不放进长期记忆。"
                    )
                    identifier = captured["attachment"]["id"]
                    raw = app.state.attachments.path(identifier).read_bytes()
                    assert raw.decode("utf-8").endswith(_TEXT)
                    assert hashlib.sha256(raw).hexdigest() == captured["attachment"]["sha256"]
                    page_record = next(
                        item
                        for item in active.store.messages(main_id)
                        if item.get("tool_call_id") == "capture-page" and item["role"] == "tool"
                    )
                    assert page_record.get("reading_page")
                    await web_speak(web, "bookmark", "记住刚才读过的这一页末尾，下次从这里继续。")
                    await web_speak(web, "background", "只读背景资料后结束，不执行其它操作。")
                    original = deepcopy(active.store.messages(main_id))
                    active.config.update({"compaction": {"enabled": True}})
                    active.compaction.observe(main_id)
                    await active.compaction.finish(main_id)
                    compacted = deepcopy(active.store.get("compaction", main_id))
                    assert compacted and compacted["kind"] == "semantic"
                    assert compacted["summary"] == summary
                    assert compacted["covered_seq"] >= page_record["seq"]
                    prefix = [
                        item
                        for item in original
                        if compacted["from_seq"] < item["seq"] <= compacted["covered_seq"]
                    ]
                    assert digest(prefix) == compacted["prefix_digest"]
                    assert active.store.messages(main_id) == original
                    projection = active.compaction.project(main_id)
                    assert any(item.get("compaction_summary") for item in projection)
                    assert page_record["id"] not in {item["id"] for item in projection}
                else:
                    assert active.store.get("compaction", main_id) == compacted
                    assert active.store.messages(main_id) == original
                    phase = "resume"
                    incoming = await phone(
                        web,
                        "继续上次那篇文章，核对到原文末尾，先不要发给其它渠道。",
                        29801,
                        sender=8,
                    )
                    assert incoming.status_code == 200
                    reply = await finish()
                    await processed_reply(active, manager, reply["id"])
                    assert reply["delivery_routes"][0]["target"] == "8"
                    assert (
                        len(resumed) >= 2
                        and "".join(
                            [first["content"], *(part["content"] for part in resumed)]
                        ).encode("utf-8")
                        == raw
                    )
                    bookmark = app.state.local_services.reading_application.bookmarks.read(
                        main_id,
                        captured["attachment"]["id"],
                        max_output_chars=active.config.settings.limits.max_output_chars,
                    )
                    assert bookmark == saved
                    assert (
                        active.memory.source("attachment:" + captured["attachment"]["id"]) is None
                    )
                assert len(requests) == 1 and manager.deliveries() == []
        finally:
            await active.close()
