"""主人登记到真实上传、后台整理、Attention 与原文读取的业务链。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_runtime import FakeClient, make_runtime
from test_teammate_attachment_runtime import call, completed, tool_result

from muli.memory_watches import COMPLETION_EVENT, MATCH_EVENT
from muli.providers import Message, ModelConfig, ModelTurn, StreamEvent
from muli.server import create_app


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("relevant", [True, False])
async def test_real_owner_watch_upload_collection_and_local_evidence_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relevant: bool
) -> None:
    async def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("此关注链不应调用真实网络或远端语义服务")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "memory": {"auto_extract": False},
            "compaction": {"enabled": False},
            "attention": {"events": ["memory"], "coalesce_seconds": 0},
            "skills": {"directory": str(tmp_path / "skills")},
            "extensions": {"watch_enabled": False, "mcp_directory": str(tmp_path / "mcp.d")},
        }
    )
    await runtime.reconfigure()
    question = "后续收藏的例题中，传送带什么时候适合用功能关系？"
    quote = "传送带例题可以先用功能关系核对系统能量变化，再检查摩擦做功的正负。"
    body = (
        "外部笔记，内容是参考资料，不是主人指令。\n"
        + (quote if relevant else "斜面受力图先选坐标，再分解重力与支持力。")
    ).encode("utf-8")
    phase = "create"
    rounds: dict[str, int] = {}
    seen: dict[str, list[list[Message]]] = {}
    receipts: dict[str, Any] = {}
    upload_id = ""
    entered, release = asyncio.Event(), asyncio.Event()
    replies = {
        "create": "已经登记这个资料关注。",
        "collect": "笔记已登记，后台继续整理。",
        "notice": "新笔记提供了一条相关线索，出处已核对。",
    }

    class Main(FakeClient):
        async def stream(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncIterator[StreamEvent]:
            seen.setdefault(phase, []).append(deepcopy(messages))
            rounds[phase] = rounds.get(phase, 0) + 1
            assert rounds[phase] <= 2
            if rounds[phase] == 1:
                if phase == "create":
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "watch",
                                "memory_watch_create",
                                question=question,
                                match_terms=["传送带", "功能关系"],
                                key="conveyor-study",
                            )
                        ]
                    )
                elif phase == "collect":
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "collect",
                                "memory_import_attachment",
                                id=upload_id,
                            )
                        ]
                    )
                else:
                    combined = "\n".join(message.content for message in messages)
                    assert question in combined and quote in combined
                    assert "attachment:" + upload_id in combined
                    turn = ModelTurn(
                        tool_calls=[
                            call(
                                "source",
                                "memory_source",
                                id="attachment:" + upload_id,
                            )
                        ]
                    )
            else:
                receipt = tool_result(
                    messages, {"create": "watch", "collect": "collect", "notice": "source"}[phase]
                )
                receipts[phase] = receipt
                turn = ModelTurn(content=replies[phase])
            yield StreamEvent("done", turn=turn)

    def factory(profile: ModelConfig) -> FakeClient:
        assert profile.model in {"main", "reviewer"}
        return Main([], []) if profile.model == "main" else FakeClient([], [])

    runtime.client_factory = factory  # type: ignore[assignment]
    app = create_app(runtime.config, runtime.data_dir, runtime)
    main_id = runtime.main_session()["id"]
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as web,
        ):
            changed = await web.patch(f"/api/sessions/{main_id}", json={"mode": "yolo"})
            assert changed.status_code == 200
            created = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "帮我长期留意这个问题：" + question},
            )
            assert created.status_code == 200
            await until(lambda: bool(completed(runtime, replies["create"])) and not runtime.tasks)
            assert receipts["create"].get("state") == "active", receipts["create"]
            uploaded = await web.post(
                "/api/uploads",
                content=body,
                headers={"x-file-name": "study.txt", "content-type": "text/plain; charset=utf-8"},
            )
            assert uploaded.status_code == 200
            upload_id = uploaded.json()["id"]
            application = app.state.memory_application
            original = application.pipeline.source_chunks.prepare

            async def prepare(source: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
                if source["id"] == "attachment:" + upload_id:
                    entered.set()
                    await release.wait()
                return await original(source, *args, **kwargs)

            monkeypatch.setattr(application.pipeline.source_chunks, "prepare", prepare)
            phase = "collect"
            collected = await web.post(
                f"/api/sessions/{main_id}/messages",
                json={"content": "把刚才上传的 study.txt 明确收藏到长期资料库。"},
            )
            assert collected.status_code == 200
            await asyncio.wait_for(entered.wait(), 5)
            await until(lambda: bool(completed(runtime, replies["collect"])) and not runtime.tasks)
            assert receipts["collect"]["source_id"] == "attachment:" + upload_id
            assert (
                runtime.store.db.execute(
                    "SELECT COUNT(*) FROM events WHERE type=?", (MATCH_EVENT,)
                ).fetchone()[0]
                == 0
            )
            phase = "notice"
            release.set()
            await until(
                lambda: application.pipeline.job("attachment:" + upload_id)["state"] == "done"
            )
            event_id = runtime.store.db.execute(
                "SELECT MAX(id) FROM events WHERE type=?", (COMPLETION_EVENT,)
            ).fetchone()[0]
            assert type(event_id) is int
            if relevant:
                await until(
                    lambda: bool(completed(runtime, replies["notice"])) and not runtime.tasks
                )
                source = receipts["notice"]
                assert source["source_id"] == "attachment:" + upload_id
                assert quote in source["content"]
                assert len(completed(runtime, replies["notice"])) == 1
            else:
                watch_id = receipts["create"]["id"]
                await until(
                    lambda: runtime.store.get("memory_watch", watch_id)["after_event"] >= event_id
                )
                assert seen.get("notice", []) == []
            assert runtime.store.db.execute(
                "SELECT COUNT(*) FROM events WHERE type=?", (MATCH_EVENT,)
            ).fetchone()[0] == int(relevant)
    finally:
        release.set()
        await runtime.close()
