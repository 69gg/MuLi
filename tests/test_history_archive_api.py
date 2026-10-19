from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from muli.auth import hash_password
from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.runtime import Runtime
from muli.server import create_app


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("只读归档不能调用模型")


@pytest.fixture
async def archive_case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data", client_factory=no_model)
    app = create_app(config, runtime=runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            yield runtime, client
    finally:
        await runtime.close()


@pytest.mark.parametrize("main", [True, False])
async def test_archive_reaches_before_live_tail_without_mutating_execution(
    archive_case: tuple[Runtime, httpx.AsyncClient], main: bool
) -> None:
    runtime, client = archive_case
    session = runtime.main_session() if main else runtime.new_session("只读旧任务")
    sid = session["id"]
    expected = [
        runtime.store.add_message(
            sid,
            "user",
            f"原始资料 {index}",
            source="system:attention",
            principal="system:attention",
        )
        for index in range(1001)
    ]
    live_path = "/api/conversation" if main else f"/api/sessions/{sid}"
    live = (await client.get(live_path)).json()
    assert len(live["messages"]) == 1000 and live["messages"][0]["id"] == expected[1]["id"]
    changes = runtime.store.db.total_changes
    found: list[str] = []
    params: dict[str, int] = {"limit": 100}
    for _ in range(30):
        response = await client.get(f"/api/sessions/{sid}/messages", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert (
            len(json.dumps(page, ensure_ascii=False))
            <= runtime.config.settings.limits.max_output_chars
        )
        assert page["session_id"] == sid and page["through_seq"] == expected[-1]["seq"]
        records = page["messages"]
        assert records and all(item["principal"] == "system:attention" for item in records)
        assert [item["seq"] for item in records] == sorted(item["seq"] for item in records)
        found[:0] = [item["id"] for item in records]
        if not page["has_older"]:
            assert page["next_before_seq"] is None
            break
        assert page["next_before_seq"] == records[0]["seq"]
        params.update(before_seq=page["next_before_seq"], through_seq=page["through_seq"])
    else:
        pytest.fail("旧资料分页没有到达开头")
    assert found == [item["id"] for item in expected]
    assert runtime.store.db.total_changes == changes
    assert not runtime.tasks and not runtime.workflows.active_run_ids
    assert runtime.store.pending_inputs(sid) == 0
    assert runtime.store.get("session", sid) == session


async def test_archive_small_budget_never_skips_unreturned_messages_and_caps_large_payload(
    archive_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = archive_case
    session = runtime.new_session("长资料")
    sid = session["id"]
    expected = [
        runtime.store.add_message(
            sid,
            "tool",
            '中文🙂\0"\\' * 500,
            provider_data={"large": "native" * 20000},
            images=["inline" * 20000],
            content_provenance="runtime_excerpt",
        )
        for _ in range(4)
    ]
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    params: dict[str, int] = {}
    found: list[str] = []
    for _ in range(10):
        response = await client.get(f"/api/sessions/{sid}/messages", params=params)
        assert response.status_code == 200, response.text
        assert "native" not in response.text and "inline" not in response.text
        page = response.json()
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        assert all(item["content_provenance"] == "runtime_excerpt" for item in page["messages"])
        found[:0] = [item["id"] for item in page["messages"]]
        if not page["has_older"]:
            break
        assert page["messages"] and page["next_before_seq"] == page["messages"][0]["seq"]
        params = {"before_seq": page["next_before_seq"], "through_seq": page["through_seq"]}
    assert found == [item["id"] for item in expected]
    runtime.config.update({"limits": {"max_output_chars": 100000}})
    huge = runtime.store.add_message(sid, "assistant", "长" * 100000)
    for path in (
        f"/api/sessions/{sid}/messages",
        f"/api/sessions/{sid}/messages/{huge['id']}/content",
    ):
        response = await client.get(path)
        assert response.status_code == 200
        assert len(json.dumps(response.json(), ensure_ascii=False)) <= 32000


async def test_content_reads_exact_bytes_and_rejects_changed_deleted_and_wrong_session(
    archive_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = archive_case
    sid = runtime.new_session("正文")["id"]
    other = runtime.new_session("无关")["id"]
    original = '中文🙂\0<script>原文</script>"\\' * 60
    item = runtime.store.add_message(sid, "tool", original, principal="system:attention")
    path = f"/api/sessions/{sid}/messages/{item['id']}/content"
    params: dict[str, Any] = {"length_bytes": 127, "revision": item["evidence_revision"]}
    parts: list[str] = []
    for _ in range(100):
        response = await client.get(path, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["session_id"] == sid and page["message_id"] == item["id"]
        assert page["revision"] == item["evidence_revision"]
        assert page["principal"] == "system:attention" and page["role"] == "tool"
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        parts.append(page["content"])
        if not page["has_more"]:
            break
        assert page["next_offset_bytes"] > page["offset_bytes"]
        params["offset_bytes"] = page["next_offset_bytes"]
    assert "".join(parts) == original
    assert (await client.get(path, params={"offset_bytes": 3})).status_code == 400
    assert (
        await client.get(f"/api/sessions/{other}/messages/{item['id']}/content")
    ).status_code == 404
    old_revision = item["evidence_revision"]
    runtime.store.put("message", {**item, "content": "更正"})
    changed = await client.get(path, params={"revision": old_revision})
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "evidence_changed"
    runtime.store.delete("message", item["id"])
    assert (await client.get(path)).status_code == 404
    assert not runtime.tasks and not runtime.store.pending_inputs(sid)


async def test_empty_deleted_session_and_zero_upper_bound_are_distinct(
    archive_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = archive_case
    sid = runtime.new_session("空归档")["id"]
    path = f"/api/sessions/{sid}/messages"
    empty = (await client.get(path)).json()
    assert empty == {
        "session_id": sid,
        "messages": [],
        "through_seq": 0,
        "next_before_seq": None,
        "has_older": False,
    }
    runtime.store.add_message(sid, "user", "后来追加")
    assert (await client.get(path, params={"through_seq": 0})).json() == empty
    runtime.store.delete("session", sid)
    missing = await client.get(path)
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "evidence_not_found"
    assert runtime.store.get("session", sid) is None


@pytest.mark.parametrize(
    "params",
    [
        {"before_seq": 0},
        {"through_seq": -1},
        {"limit": 0},
        {"limit": 101},
        {"before_seq": "false"},
        {"through_seq": 1 << 53},
        {"before_seq": 1 << 80},
    ],
)
async def test_invalid_archive_queries_reject_before_reading(
    archive_case: tuple[Runtime, httpx.AsyncClient], params: dict[str, Any]
) -> None:
    runtime, client = archive_case
    changes = runtime.store.db.total_changes
    response = await client.get("/api/sessions/absent/messages", params=params)
    assert response.status_code == 422
    assert runtime.store.db.total_changes == changes


async def test_browser_unsafe_sequence_is_an_explicit_error_not_a_rounded_cursor(
    archive_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = archive_case
    sid = runtime.new_session("高序号")["id"]
    item = runtime.store.add_message(sid, "user", "测试已迁移的高序号")
    with runtime.store.db:
        runtime.store.db.execute(
            "UPDATE message_order SET seq=? WHERE message_id=?", ((1 << 53) + 1, item["id"])
        )
    for path in (
        f"/api/sessions/{sid}/messages",
        f"/api/sessions/{sid}/messages/{item['id']}/content",
    ):
        response = await client.get(path)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "archive_sequence_range"


async def test_archive_and_content_require_existing_login(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "updates": {"check_on_start": False},
            "server": {"password_hash": hash_password("archive-test-password")},
        }
    )
    runtime = Runtime(config, tmp_path / "data", client_factory=no_model)
    sid = runtime.new_session("认证")["id"]
    item = runtime.store.add_message(sid, "user", "私人正文")
    app = create_app(config, runtime=runtime)
    paths = [f"/api/sessions/{sid}/messages", f"/api/sessions/{sid}/messages/{item['id']}/content"]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            for path in paths:
                assert (await client.get(path)).status_code == 401
            assert (
                await client.post("/api/auth/login", json={"password": "archive-test-password"})
            ).status_code == 200
            for path in paths:
                response = await client.get(path)
                assert (
                    response.status_code == 200 and response.headers["cache-control"] == "no-store"
                )
            assert not runtime.tasks
    finally:
        await runtime.close()
