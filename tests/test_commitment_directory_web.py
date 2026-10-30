from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_commitment_directory import Ledger
from test_maintenance_api import host_app
from test_runtime import make_runtime

from muli.commitments import STATES
from muli.config import ConfigFile
from muli.evidence import json_size
from muli.maintenance_api import passive_request_allowed, request_has_effect
from muli.runtime import Runtime
from muli.server import create_app


async def test_commitment_directory_pages_persisted_tasks_without_teammates(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    for identifier in ("a-old-task", "z-new-task"):
        runtime.store.put("commitment", {"id": identifier, "title": identifier, "status": "open"})
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as client:
            first = await client.get("/api/commitment-directory", params={"limit": 1})
            assert first.status_code == 200
            page = first.json()
            assert [item["id"] for item in page["items"]] == ["z-new-task"]
            assert page["has_more"] and page["next_cursor"]
            final = await client.get(
                "/api/commitment-directory",
                params={"limit": 1, "cursor": page["next_cursor"]},
            )
            assert final.status_code == 200
            assert [item["id"] for item in final.json()["items"]] == ["a-old-task"]
            assert final.json()["has_more"] is False
            assert runtime.tasks == {}


@pytest.fixture
async def commitment_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, Ledger, httpx.AsyncClient]]:
    calls: list[str] = []

    async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        calls.append("external_or_control")
        raise AssertionError("浏览任务目录不能联网、发送或控制任务")

    def no_model_or_wake(*args: Any, **kwargs: Any) -> NoReturn:
        calls.append("model_or_wake")
        raise AssertionError("浏览任务目录不能调用模型或唤醒任务")

    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.update({"updates": {"check_on_start": False}})
    app = create_app(runtime.config, runtime=runtime)
    ledger = Ledger(runtime.store, app.state.local_services.commitments)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(runtime, "client_factory", no_model_or_wake)
    monkeypatch.setattr(runtime, "_wake_main", no_model_or_wake)
    monkeypatch.setattr(runtime, "submit_task_control", forbidden)
    monkeypatch.setattr(app.state.channel_application.manager, "enqueue", forbidden)
    monkeypatch.setattr(app.state.channel_application.manager, "retry", forbidden)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web:
            yield runtime, ledger, web
        assert calls == []
    finally:
        await app.state.close()


async def test_web_directory_reaches_tasks_beyond_the_legacy_100_and_keeps_old_detail(
    commitment_web: tuple[Runtime, Ledger, httpx.AsyncClient],
) -> None:
    runtime, ledger, web = commitment_web
    records = [
        {
            "id": f"task-{index:03d}",
            "title": "未关联队友的目标",
            "status": "open",
            "revision": 7,
            "brief": {
                "version": 1,
                "goal": "private-brief-goal",
                "constraints": ["开头：完整要求中段：结尾"],
                "evidence_refs": [],
            },
        }
        for index in range(121)
    ]
    runtime.store.put_many([("commitment", item) for item in records])
    with runtime.store.db:
        runtime.store.db.executemany(
            "UPDATE objects SET updated=? WHERE kind='commitment' AND id=?",
            [(float(index), item["id"]) for index, item in enumerate(records)],
        )
    before = runtime.store.db.total_changes
    legacy = await web.get("/api/commitments")
    assert legacy.status_code == 200
    assert legacy.json() == {"items": ledger.commitments.list()}
    assert len(legacy.json()["items"]) == 100
    assert records[0]["id"] not in {item["id"] for item in legacy.json()["items"]}
    assert (await web.get("/api/team")).json()["members"] == []
    found: list[str] = []
    cursor: str | None = None
    for _ in range(len(records) + 1):
        params: dict[str, str | int] = {"status": "active", "limit": 20}
        if cursor is not None:
            params["cursor"] = cursor
        response = await web.get("/api/commitment-directory", params=params)
        assert response.status_code == 200
        page = response.json()
        assert page == ledger.commitments.directory(cursor=cursor, limit=20)
        assert page["consistency"] == "bounded_live" and page["omitted_total"] == 0
        assert page["scanned"] <= runtime.config.settings.limits.max_search_results
        assert json_size(page) <= runtime.config.settings.limits.max_output_chars
        assert "private-brief-goal" not in response.text and "完整要求中段" not in response.text
        found.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None and page["stopped"] == "end"
            break
        assert page["next_cursor"] and page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    else:
        pytest.fail("真实有界 API 未能翻页到尾")
    assert found == [item["id"] for item in reversed(records)]
    assert len(found) == len(set(found)) == len(records)
    detail = await web.get(f"/api/commitments/{records[0]['id']}")
    assert detail.status_code == 200
    assert {key: detail.json()[key] for key in records[0]} == records[0]
    assert detail.json()["scope"]["member_count"] == 0
    assert (await web.get("/api/commitments/missing")).status_code == 404
    assert runtime.store.get("commitment", records[0]["id"]) == records[0]
    assert runtime.store.db.total_changes == before and not runtime.tasks
    assert runtime._selected_replans == {}


async def test_web_directory_preserves_two_empty_candidate_pages_and_complete_reader_metadata(
    commitment_web: tuple[Runtime, Ledger, httpx.AsyncClient],
) -> None:
    runtime, ledger, web = commitment_web
    runtime.config.update({"limits": {"max_search_results": 2, "max_context_chars": 4000}})
    older = ledger.put("a", title="目标")
    done = ledger.put("v", status="done", evidence="有验证证据")
    invalid = ledger.put("w", title=["不是元数据文字"])
    broken = ledger.put("x")
    ledger.put(
        "y", control={"epoch": 0, "approved_epoch": 0, "plan": {"text": "plan-private" * 2000}}
    )
    ledger.put("z", brief={"goal": "brief-private" * 2000})
    with runtime.store.db:
        runtime.store.db.execute(
            "UPDATE objects SET data='broken json' WHERE kind='commitment' AND id=?",
            (broken["id"],),
        )
    before = runtime.store.db.total_changes
    cursor: str | None = None
    pages: list[dict[str, Any]] = []
    for _ in range(3):
        params = {"cursor": cursor} if cursor is not None else {}
        response = await web.get("/api/commitment-directory", params=params)
        assert response.status_code == 200
        page = response.json()
        assert page == ledger.commitments.directory(cursor=cursor)
        pages.append(page)
        cursor = page["next_cursor"]
    assert pages[0]["items"] == pages[1]["items"] == []
    assert pages[0]["omitted"] == {"oversized": 2, "invalid": 0, "missing": 0}
    assert pages[1]["omitted"] == {"oversized": 0, "invalid": 2, "missing": 0}
    assert all(page["has_more"] and page["stopped"] == "candidate_budget" for page in pages[:2])
    assert pages[0]["next_cursor"] != pages[1]["next_cursor"]
    assert [item["id"] for item in pages[2]["items"]] == [older["id"]]
    assert pages[2]["has_more"] is False and pages[2]["next_cursor"] is None
    assert pages[2]["omitted_total"] == 4 and all(page["scanned"] == 2 for page in pages)
    assert sum(page["json_bytes"] for page in pages) == sum(
        ledger.size(item["id"]) for item in (broken, invalid, done, older)
    )
    assert runtime.store.db.total_changes == before and not runtime.store.db.in_transaction


async def test_web_directory_keeps_query_validation_and_reader_errors_distinct(
    commitment_web: tuple[Runtime, Ledger, httpx.AsyncClient],
) -> None:
    runtime, ledger, web = commitment_web
    ledger.put("a")
    ledger.put("z")
    for status in STATES:
        ledger.put(f"state-{status}", status=status)
    before = runtime.store.db.total_changes
    for status in (*STATES, "active", "all"):
        response = await web.get(
            "/api/commitment-directory", params={"status": status, "limit": 100}
        )
        assert response.status_code == 200
        assert response.json() == ledger.commitments.directory(status=status, limit=100)
    for invalid in ({"cursor": ""}, {"limit": 0}, {"limit": 101}, {"limit": "many"}):
        response = await web.get("/api/commitment-directory", params=invalid)
        assert response.status_code == 422
    for invalid, code in (
        ({"status": "unknown"}, "invalid_arguments"),
        ({"status": ""}, "invalid_arguments"),
        ({"cursor": "not-a-cursor"}, "commitment_directory_cursor_invalid"),
        ({"cursor": "W10="}, "commitment_directory_cursor_invalid"),
    ):
        response = await web.get("/api/commitment-directory", params=invalid)
        assert response.status_code == 400 and response.json()["detail"]["code"] == code
    first = (await web.get("/api/commitment-directory", params={"limit": 1})).json()
    stale = await web.get(
        "/api/commitment-directory", params={"status": "done", "cursor": first["next_cursor"]}
    )
    assert stale.status_code == 400
    assert stale.json()["detail"]["code"] == "commitment_directory_cursor_invalid"
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    over_budget = "x" * (runtime.config.settings.limits.max_output_chars * 4 + 1)
    response = await web.get("/api/commitment-directory", params={"cursor": over_budget})
    assert response.status_code == 400 and response.json()["detail"]["code"] == "output_budget"
    assert runtime.store.db.total_changes == before and not runtime.store.db.in_transaction


async def test_web_directory_requires_existing_authentication_and_keeps_private_briefs_out(
    commitment_web: tuple[Runtime, Ledger, httpx.AsyncClient],
) -> None:
    runtime, ledger, web = commitment_web
    runtime.config.update({"server": {"password": "local-test-password"}})
    item = ledger.put(
        "private-task", revision=1, brief={"goal": "private-task-body", "constraints": []}
    )
    before = runtime.store.db.total_changes
    for path in ("/api/commitment-directory", f"/api/commitments/{item['id']}"):
        denied = await web.get(path)
        assert denied.status_code == 401 and "private-task-body" not in denied.text
    logged_in = await web.post("/api/auth/login", json={"password": "local-test-password"})
    assert logged_in.status_code == 200
    page = await web.get("/api/commitment-directory")
    assert page.status_code == 200 and "private-task-body" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.json() == ledger.commitments.directory()
    assert (await web.get(f"/api/commitments/{item['id']}")).json()["brief"] == item["brief"]
    assert runtime.store.db.total_changes == before and not runtime.tasks


async def test_web_directory_does_not_construct_a_missing_main_or_task(
    commitment_web: tuple[Runtime, Ledger, httpx.AsyncClient],
) -> None:
    runtime, ledger, web = commitment_web
    item = ledger.put("unbound-task")
    with runtime.store.db:
        runtime.store.db.execute("DELETE FROM runtime_state WHERE name='main_session'")
    assert runtime.store.existing_main_session_id() is None
    before = runtime.store.db.total_changes
    response = await web.get("/api/commitment-directory")
    assert response.status_code == 200
    assert [record["id"] for record in response.json()["items"]] == [item["id"]]
    assert runtime.store.existing_main_session_id() is None
    assert runtime.store.db.total_changes == before and not runtime.tasks
    assert runtime.store.list("run") == runtime.store.list("agent") == []


async def test_web_directory_and_old_detail_remain_readable_on_a_real_passive_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with host_app(tmp_path, passive=True) as (app, _, _):
        runtime = app.state.runtime
        item = runtime.store.put(
            "commitment", {"id": "saved-task", "title": "原任务", "status": "open", "revision": 1}
        )

        async def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
            raise AssertionError("维护期只读任务入口不能联网、发送或控制任务")

        def no_model_or_wake(*args: Any, **kwargs: Any) -> NoReturn:
            raise AssertionError("维护期读取任务不能调用模型或唤醒任务")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
        monkeypatch.setattr(runtime, "client_factory", no_model_or_wake)
        monkeypatch.setattr(runtime, "_wake_main", no_model_or_wake)
        monkeypatch.setattr(runtime, "submit_task_control", forbidden)
        monkeypatch.setattr(app.state.channel_application.manager, "enqueue", forbidden)
        before = runtime.store.db.total_changes
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as web:
            response = await web.get("/api/commitment-directory")
            assert response.status_code == 200
            assert [record["id"] for record in response.json()["items"]] == [item["id"]]
            assert (await web.get(f"/api/commitments/{item['id']}")).status_code == 200
            assert (await web.get("/api/commitments")).json() == {
                "items": app.state.local_services.commitments.list()
            }
            control = await web.post(
                f"/api/commitments/{item['id']}/control",
                json={"action": "pause", "expected_epoch": 0, "request_id": "blocked"},
            )
            assert control.status_code == 409 and control.json()["detail"] == "execution_paused"
            assert (await web.get("/api/commitment-directory/future")).status_code == 409
        assert not runtime.started and not runtime.maintenance.admission.allowed
        assert runtime.store.get("commitment", item["id"]) == item
        assert runtime.store.db.total_changes == before and not runtime.tasks


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("GET", "/api/commitment-directory", True),
        ("POST", "/api/commitment-directory", False),
        ("HEAD", "/api/commitment-directory", False),
        ("GET", "/api/commitment-directory/", False),
        ("GET", "/api/commitment-directory/future", False),
        ("GET", "/api/commitment-directory-extra", False),
    ],
)
def test_only_exact_commitment_directory_get_is_passive(
    method: str, path: str, allowed: bool
) -> None:
    assert passive_request_allowed(method, path, None) is allowed
    assert request_has_effect(method, path, None) is not allowed
