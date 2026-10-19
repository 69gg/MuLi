from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import aclosing, closing
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.runtime import Runtime
from muli.server import create_app
from muli.store import SQLITE_MAX_INTEGER, Store


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("事件位置和订阅校验不能调用模型")


@pytest.fixture
async def event_case(tmp_path: Path) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
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


@pytest.fixture
def stream_calls(
    event_case: tuple[Runtime, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[int, str | None]]:
    runtime, _ = event_case
    runtime.store.put("session", {"id": "sse-session"})
    calls: list[tuple[int, str | None]] = []

    def stream(after: int = 0, session_id: str | None = None) -> AsyncIterator[str]:
        calls.append((after, session_id))

        async def body() -> AsyncIterator[str]:
            yield ": test-complete\n\n"

        return body()

    monkeypatch.setattr(runtime.bus, "stream", stream)
    return calls


def test_store_position_reads_only_latest_id_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    with closing(Store(path)) as store:
        assert store.event_position() == 0
        first = store.event("first", {"payload": "not part of the position"})
        exact = (1 << 53) + 1
        store.db.execute(
            "INSERT INTO events(id,type,data,created_at) VALUES(?,?,?,1)",
            (exact, "latest", "not JSON: the position must not decode the payload"),
        )
        columns: list[str] = []

        def observe(cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> sqlite3.Row:
            columns.extend(description[0] for description in cursor.description)
            assert all(isinstance(value, int) for value in values)
            return sqlite3.Row(cursor, values)

        store.db.row_factory = observe
        changes = store.db.total_changes
        assert store.event_position() == exact
        assert store.db.total_changes == changes
        assert columns and "data" not in columns
        store.db.row_factory = sqlite3.Row
        store.db.commit()

    with closing(Store(path)) as reopened:
        assert reopened.event_position() == exact
        reopened.db.execute("DELETE FROM events WHERE id=?", (exact,))
        assert reopened.event_position() == first["id"]


@pytest.mark.parametrize("position", [0, (1 << 53) + 1, SQLITE_MAX_INTEGER])
async def test_position_http_is_exact_projected_and_does_not_create_a_session(
    event_case: tuple[Runtime, httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
    position: int,
) -> None:
    runtime, client = event_case
    if position:
        runtime.store.db.execute(
            "INSERT INTO events(id,type,data,created_at) VALUES(?,?,?,1)",
            (position, "position-only", "not JSON: must not load an event payload"),
        )
    changes = runtime.store.db.total_changes

    def no_session() -> NoReturn:
        raise AssertionError("读取事件位置不能创建主会话")

    def no_events(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("读取事件位置不能加载事件列表")

    monkeypatch.setattr(runtime, "main_session", no_session)
    monkeypatch.setattr(runtime.store, "events", no_events)
    response = await client.get("/api/ui/event-position")
    assert response.status_code == 200, response.text
    assert response.json() == {"after": str(position)}
    assert response.headers["cache-control"] == "no-store"
    assert runtime.store.db.total_changes == changes
    assert runtime.store.list("session") == []
    assert not runtime.tasks and not runtime.workflows.active_run_ids


@pytest.mark.parametrize("old_id", [1, (1 << 53) + 1])
async def test_position_bridges_later_events_through_the_existing_bus(
    event_case: tuple[Runtime, httpx.AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
    old_id: int,
) -> None:
    runtime, client = event_case
    runtime.store.db.execute(
        "INSERT INTO events(id,type,data,created_at) VALUES(?,?,?,1)",
        (old_id, "old", json.dumps({"note": "old-payload"})),
    )
    position = (await client.get("/api/ui/event-position")).json()["after"]
    assert position == str(old_id)
    later = await runtime.bus.emit("later", {"note": "new-payload"})
    original_stream = runtime.bus.stream

    async def one_frame(after: int = 0, session_id: str | None = None) -> AsyncIterator[str]:
        async with aclosing(original_stream(after, session_id)) as source:
            yield await anext(source)

    monkeypatch.setattr(runtime.bus, "stream", one_frame)
    async with asyncio.timeout(2):
        response = await client.get("/api/events", params={"after": position})
    assert response.status_code == 200, response.text
    assert response.text.startswith(f"id: {later['id']}\n")
    data = next(
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )
    assert json.loads(data) == later
    assert "old-payload" not in response.text


@pytest.mark.parametrize("path", ["/api/events", "/api/sessions/sse-session/events"])
@pytest.mark.parametrize(
    ("query", "header", "expected_status"),
    [
        ("-1", None, 422),
        (str(SQLITE_MAX_INTEGER + 1), None, 422),
        ("not-an-integer", None, 422),
        ("1.5", None, 422),
        ("-1", "0", 422),
        (str(SQLITE_MAX_INTEGER + 1), "0", 422),
        ("0", "-1", 400),
        ("0", str(SQLITE_MAX_INTEGER + 1), 400),
        ("0", "not-an-integer", 400),
        ("0", "1.0", 400),
        (str(SQLITE_MAX_INTEGER), "-1", 400),
        (str(SQLITE_MAX_INTEGER), str(SQLITE_MAX_INTEGER + 1), 400),
    ],
)
async def test_invalid_sse_cursors_are_rejected_before_stream_or_store_access(
    event_case: tuple[Runtime, httpx.AsyncClient],
    stream_calls: list[tuple[int, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    query: str,
    header: str | None,
    expected_status: int,
) -> None:
    runtime, client = event_case

    def no_events(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("无效 HTTP 游标不能进入事件查询")

    monkeypatch.setattr(runtime.store, "events", no_events)
    changes = runtime.store.db.total_changes
    response = await client.get(
        path,
        params={"after": query},
        headers={} if header is None else {"Last-Event-ID": header},
    )
    assert response.status_code == expected_status, response.text
    assert response.headers["content-type"].startswith("application/json")
    if expected_status == 400:
        assert response.json() == {"detail": "事件游标无效"}
    assert stream_calls == []
    assert runtime.store.db.total_changes == changes


@pytest.mark.parametrize("path", ["/api/events", "/api/sessions/sse-session/events"])
@pytest.mark.parametrize(
    ("query", "header", "expected_after"),
    [
        (None, None, 0),
        ("0", None, 0),
        (str(SQLITE_MAX_INTEGER), None, SQLITE_MAX_INTEGER),
        ("17", str(SQLITE_MAX_INTEGER), SQLITE_MAX_INTEGER),
        (str(SQLITE_MAX_INTEGER), "0", SQLITE_MAX_INTEGER),
        ("0", "", 0),
        ("0", str((1 << 53) + 1), (1 << 53) + 1),
        ("18", "17", 18),
    ],
)
async def test_valid_sse_cursors_preserve_exact_max_selection_and_session_scope(
    event_case: tuple[Runtime, httpx.AsyncClient],
    stream_calls: list[tuple[int, str | None]],
    path: str,
    query: str | None,
    header: str | None,
    expected_after: int,
) -> None:
    runtime, client = event_case
    response = await client.get(
        path,
        params={} if query is None else {"after": query},
        headers={} if header is None else {"Last-Event-ID": header},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    session_id = "sse-session" if "/sessions/" in path else None
    assert stream_calls == [(expected_after, session_id)]
    assert runtime.store.events(expected_after, session_id) == []


@pytest.mark.parametrize(
    "path", ["/api/ui/event-position", "/api/events", "/api/sessions/sse-session/events"]
)
async def test_event_subscription_endpoints_require_login(
    event_case: tuple[Runtime, httpx.AsyncClient],
    stream_calls: list[tuple[int, str | None]],
    path: str,
) -> None:
    runtime, client = event_case
    runtime.config.update({"server": {"password": "event-subscription-test-password"}})
    response = await client.get(path)
    assert response.status_code == 401
    assert stream_calls == []
    login = await client.post(
        "/api/auth/login", json={"password": "event-subscription-test-password"}
    )
    assert login.status_code == 200
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
