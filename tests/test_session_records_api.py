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
    raise AssertionError("记录目录不能调用模型")


@pytest.fixture
async def records_case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
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


async def test_title_directory_finds_old_record_and_keeps_live_contract_and_identity(
    records_case: tuple[Runtime, httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, client = records_case
    main = runtime.main_session()
    timestamp = 1.0

    def clock() -> float:
        return timestamp

    # Keep runtime JSON dates and the legacy storage order explicit even when
    # the host wall clock gives every quick write the same timestamp.
    monkeypatch.setattr("muli.runtime.now", clock)
    monkeypatch.setattr("muli.store.now", clock)
    old = runtime.new_session("旧的中文 %_ Archive")
    message = runtime.store.add_message(old["id"], "user", "旧资料不是新主人输入")
    expected_times = {old["id"]: timestamp}
    for index in range(201):
        timestamp = float(index + 2)
        session = runtime.new_session(f"较新资料 {index}")
        expected_times[session["id"]] = timestamp
    saved_times = {
        row["id"]: (row["created_at"], row["updated_at"], row["updated"])
        for row in runtime.store.db.execute(
            "SELECT id,updated,json_extract(data,'$.created_at') AS created_at,"
            "json_extract(data,'$.updated_at') AS updated_at FROM objects "
            "WHERE kind='session' AND id<>?",
            (main["id"],),
        )
    }
    assert saved_times == {
        identifier: (value, value, value) for identifier, value in expected_times.items()
    }
    old_list = (await client.get("/api/sessions")).json()
    assert len(old_list["items"]) == 200
    assert old["id"] not in {item["id"] for item in old_list["items"]}
    changes = runtime.store.db.total_changes
    response = await client.get("/api/session-records", params={"query": "中文 %_ archive"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["id"] == old["id"]
    assert response.json()["has_more"] is False
    page = (await client.get(f"/api/sessions/{old['id']}/messages")).json()
    assert page["messages"][0]["id"] == message["id"]
    assert runtime.store.db.total_changes == changes
    assert (await client.get("/api/sessions")).json() == old_list
    assert runtime.main_session()["id"] == main["id"]
    assert not runtime.tasks and not runtime.workflows.active_run_ids
    assert not runtime.store.pending_inputs(main["id"])
    assert not runtime.store.pending_inputs(old["id"])


async def test_directory_budget_and_continuation_use_complete_http_envelope(
    records_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = records_case
    expected = [runtime.new_session(f"目录 {index:02d}") for index in range(8)]
    for index, session in enumerate(expected):
        runtime.store.put(
            "session",
            {**session, "created_at": index + 1, "native_block": "EXCLUDED" * 20_000},
        )
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    found: list[str] = []
    params: dict[str, str | int] = {"query": "目录", "limit": 100}
    for _ in range(10):
        response = await client.get("/api/session-records", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        assert "EXCLUDED" not in response.text
        found.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["items"] and isinstance(page["next_cursor"], str)
        params["cursor"] = page["next_cursor"]
    else:
        pytest.fail("记录目录未在有限页内到达末尾")
    assert found == [item["id"] for item in reversed(expected)]

    runtime.config.update({"limits": {"max_output_chars": 100_000}})
    for index in range(3):
        session = runtime.new_session("旧档案的长标题")
        runtime.store.put("session", {**session, "title": "长标题" * 5000 + str(index)})
    response = await client.get("/api/session-records", params={"query": "长标题"})
    assert response.status_code == 200, response.text
    assert len(json.dumps(response.json(), ensure_ascii=False)) <= 32_000
    assert response.json()["has_more"]
    session = runtime.new_session("旧档案中放不下的标题")
    runtime.store.put("session", {**session, "title": "放不下" * 15_000})
    error = await client.get("/api/session-records", params={"query": "放不下"})
    assert error.status_code == 400 and error.json()["detail"]["code"] == "output_budget"


async def test_directory_cursor_query_binding_and_empty_results_are_distinct(
    records_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = records_case
    for _ in range(2):
        runtime.new_session("字面空格 ")
    page = (await client.get("/api/session-records", params={"query": " ", "limit": 1})).json()
    assert page["has_more"]
    mismatch = await client.get(
        "/api/session-records", params={"query": "", "cursor": page["next_cursor"]}
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "record_cursor_invalid"
    invalid = await client.get("/api/session-records", params={"cursor": "broken"})
    assert invalid.status_code == 400
    empty = await client.get("/api/session-records", params={"query": "没有这个标题"})
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None, "has_more": False}


async def test_directory_preserves_empty_text_and_null_metadata_across_http_pages(
    records_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = records_case
    values: list[tuple[str | None, str | None]] = [(None, None), ("", ""), ("正常标题", "idle")]
    expected: list[dict[str, Any]] = []
    for index, (title, status) in enumerate(values):
        session = runtime.new_session("旧档案")
        expected.append(
            runtime.store.put(
                "session", {**session, "title": title, "status": status, "created_at": index + 1}
            )
        )
    changes = runtime.store.db.total_changes
    params: dict[str, Any] = {"limit": 1}
    for index, record in enumerate(reversed(expected)):
        response = await client.get("/api/session-records", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) == 1
        item = page["items"][0]
        assert (item["id"], item["title"], item["status"]) == (
            record["id"],
            record["title"],
            record["status"],
        )
        assert page["has_more"] is (index < len(expected) - 1)
        params["cursor"] = page["next_cursor"]
    assert params["cursor"] is None
    assert runtime.store.db.total_changes == changes and not runtime.tasks


@pytest.mark.parametrize(
    "params",
    [
        {"query": "🙂" * 121},
        {"cursor": ""},
        {"cursor": "x" * 32_001},
        {"limit": 0},
        {"limit": 101},
        {"limit": "invalid"},
    ],
    ids=[
        "query-too-long",
        "empty-cursor",
        "cursor-too-long",
        "limit-zero",
        "limit-large",
        "limit-text",
    ],
)
async def test_invalid_directory_queries_are_rejected_before_reading(
    records_case: tuple[Runtime, httpx.AsyncClient], params: dict[str, Any]
) -> None:
    runtime, client = records_case
    changes = runtime.store.db.total_changes
    response = await client.get("/api/session-records", params=params)
    assert response.status_code == 422
    assert runtime.store.db.total_changes == changes and not runtime.tasks


async def test_directory_requires_login_and_accepts_unicode_length_boundary(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "updates": {"check_on_start": False},
            "server": {"password_hash": hash_password("record-test-password")},
        }
    )
    runtime = Runtime(config, tmp_path / "data", client_factory=no_model)
    try:
        app = create_app(config, runtime=runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            assert (await client.get("/api/session-records")).status_code == 401
            login = await client.post("/api/auth/login", json={"password": "record-test-password"})
            assert login.status_code == 200
            response = await client.get("/api/session-records", params={"query": "🙂" * 120})
            assert response.status_code == 200 and response.json()["items"] == []
            assert response.headers["cache-control"] == "no-store"
            assert not runtime.tasks and runtime.store.list("session") == []
    finally:
        await runtime.close()
