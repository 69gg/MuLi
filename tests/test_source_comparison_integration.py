from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_runtime import make_runtime

from muli.attachment_memory_source import attachment_source
from muli.providers import ModelTurn, ToolCall
from muli.runtime import Runtime
from muli.server import create_app


def saved(runtime: Runtime, identifier: str, text: str) -> dict[str, Any]:
    source, _ = runtime.memory.register_source(
        attachment_source(identifier, text, hashlib.sha256(text.encode()).hexdigest(), 1),
        trusted_owner=False,
    )
    assert source is not None
    return source


def arguments(left: dict[str, Any], right: dict[str, Any]) -> dict[str, str]:
    return {
        "left_id": left["id"],
        "left_revision": left["digest"],
        "right_id": right["id"],
        "right_revision": right["digest"],
    }


@pytest.mark.parametrize("view", ["changes", "overview"])
async def test_owner_api_compares_saved_bodies_and_rereads_exact_utf8_positions(
    tmp_path: Path,
    view: str,
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}, "memory": {"enabled": False}})
    app = create_app(runtime.config, runtime=runtime)
    left = saved(runtime, "a" * 32, "申请说明🙂\n期限：七日\n共同条款\n")
    right = saved(runtime, "b" * 32, "申请说明🙂\n期限：十日\n共同条款\n新增要求")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            response = await client.post(
                "/api/memory/sources/compare", json={**arguments(left, right), "view": view}
            )
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["status"] == "changed" and not page["has_more"]
            assert {item["kind"] for item in page["items"]} == {"replace", "insert"}
            for item in page["items"]:
                for side in ("left", "right"):
                    change = item[side]
                    if not change["length_bytes"]:
                        if view == "changes":
                            assert change["content"] == ""
                        continue
                    read = await client.get("/api/memory/source", params=change["read_arguments"])
                    assert read.status_code == 200
                    if view == "changes":
                        assert read.json()["content"] == change["content"]
                    else:
                        assert "content" not in change
                        assert read.json()["content"] in {
                            "期限：七日\n",
                            "期限：十日\n",
                            "新增要求",
                        }
            if view == "overview":
                assert page["totals"]["change_groups"] == 2
            equal = await client.post("/api/memory/sources/compare", json=arguments(left, left))
            assert equal.status_code == 200 and equal.json()["status"] == "equal"
            invalid = await client.post(
                "/api/memory/sources/compare",
                json={**arguments(left, right), "right_revision": "0" * 64},
            )
            assert invalid.status_code >= 400 and "期限" not in invalid.text
            extra = await client.post(
                "/api/memory/sources/compare", json={**arguments(left, right), "unrelated": True}
            )
            assert extra.status_code == 422
            bad_view = await client.post(
                "/api/memory/sources/compare", json={**arguments(left, right), "view": "summary"}
            )
            assert bad_view.status_code == 422
            assert (
                runtime.store.db.execute("SELECT count(*) FROM memory_sources").fetchone()[0] == 2
            )
    finally:
        await app.state.close()


@pytest.mark.parametrize("member", [False, True], ids=["main", "teammate"])
@pytest.mark.parametrize("view", ["changes", "overview"])
async def test_real_agent_tool_round_preserves_exact_comparison_without_auto_ingest(
    tmp_path: Path, member: bool, view: str
) -> None:
    turns: list[ModelTurn] = []
    runtime, seen = make_runtime(tmp_path, turns)
    runtime.config.update(
        {
            "updates": {"check_on_start": False},
            "persona": {"prompt": "测试"},
            "memory": {"enabled": False},
            "compaction": {"enabled": False},
            "limits": {"max_output_chars": 12000, "max_context_chars": 32000},
        }
    )
    await runtime.reconfigure()
    app = create_app(runtime.config, runtime=runtime)
    left = saved(runtime, "a" * 32, "旧要求🙂\n共同条款\n")
    right = saved(runtime, "b" * 32, "新要求🙂\n共同条款\n")
    turns.extend(
        [
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "compare",
                        "memory_compare_sources",
                        json.dumps({**arguments(left, right), "view": view}),
                    )
                ]
            ),
        ]
    )
    if view == "overview":
        turns.append(
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        f"read-{name}",
                        "memory_source",
                        json.dumps(
                            {
                                "id": source["id"],
                                "revision": source["digest"],
                                "offset_bytes": 0,
                                "length_bytes": len(text.encode()),
                            }
                        ),
                    )
                    for name, source, text in (
                        ("left", left, "旧要求🙂\n"),
                        ("right", right, "新要求🙂\n"),
                    )
                ]
            )
        )
    turns.append(ModelTurn(content="已核对原文"))
    try:
        if member:
            child = await runtime.team.spawn(
                "资料核对",
                "比较两份已收藏资料",
                parent_id=runtime.main_session()["id"],
            )
            await runtime.team.wait([child["id"]], timeout=5)
            assert runtime.team.get(child["id"])["status"] == "completed"
        else:
            session = runtime.new_session(mode="yolo")
            started = await runtime.start(session["id"], "比较两份已收藏资料", mode="yolo")
            assert await asyncio.wait_for(runtime.wait(started["run_id"]), 5) == "已核对原文"
        result = next(message for message in seen[-1] if message.tool_call_id == "compare")
        value = json.loads(result.content)
        assert result.preserve_content and value["status"] == "changed"
        assert value["left"]["revision"] == left["digest"]
        assert value["right"]["revision"] == right["digest"]
        assert len(value["items"]) == 1 and not value["has_more"]
        if view == "overview":
            assert "旧要求" not in result.content and "新要求" not in result.content
            for name, expected in (("left", "旧要求🙂\n"), ("right", "新要求🙂\n")):
                read = next(
                    message for message in seen[-1] if message.tool_call_id == f"read-{name}"
                )
                assert json.loads(read.content)["content"] == expected
        assert runtime.store.db.execute("SELECT count(*) FROM memory_sources").fetchone()[0] == 2
    finally:
        await app.state.close()
