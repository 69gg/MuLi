from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

import httpx
import pytest

from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.runtime import Runtime
from muli.server import create_app


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("通知历史读取不能调用模型")


@pytest.fixture
async def notification_case(
    tmp_path: Path,
) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data", client_factory=no_model)
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client:
            yield runtime, client


def save(runtime: Runtime, identifier: str, created: Any, **fields: Any) -> dict[str, Any]:
    return runtime.store.put(
        "notification",
        {
            "id": identifier,
            "title": f"通知 {identifier}",
            "content": "保存过的正文，不是新的主人授权",
            "source": "assistant",
            "read": False,
            "created_at": created,
            **fields,
        },
    )


async def test_old_unread_is_reachable_beyond_200_and_read_does_not_move_the_cursor(
    notification_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = notification_case
    expected = [save(runtime, f"notice-{index:03d}", index // 3 + 1) for index in range(215)]
    legacy = (await client.get("/api/notifications")).json()
    assert len(legacy["items"]) == 200
    assert expected[0]["id"] not in {item["id"] for item in legacy["items"]}
    changes = runtime.store.db.total_changes
    params: dict[str, str | int] = {"status": "unread", "limit": 23}
    found: list[str] = []
    for page_number in range(20):
        response = await client.get("/api/notification-directory", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert page["consistency"] == "bounded_live"
        assert page["scanned"] <= 100
        assert all(item["read"] is False for item in page["items"])
        assert "保存过的正文" not in response.text
        found.extend(item["id"] for item in page["items"])
        if page_number == 0:
            assert runtime.store.db.total_changes == changes
            marked = await client.patch(f"/api/notifications/{found[-1]}", json={"read": True})
            assert marked.status_code == 200
        if not page["has_more"]:
            assert page["next_cursor"] is None and page["stopped"] == "end"
            break
        assert page["next_cursor"] != params.get("cursor")
        params["cursor"] = page["next_cursor"]
    else:
        pytest.fail("旧未读没有在有限连续页内出现")
    assert found == [item["id"] for item in reversed(expected)]
    assert len(found) == len(set(found)) == 215
    assert runtime.store.get("notification", expected[0]["id"]) == expected[0]
    assert not runtime.tasks and not runtime.store.pending_inputs(runtime.main_session()["id"])


async def test_missing_time_bad_and_oversized_rows_are_explicit_and_do_not_block_older_rows(
    notification_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = notification_case
    save(runtime, "top", 10)
    save(runtime, "z-missing-time", None)
    save(runtime, "y-oversized", 7, content="大" * 30_000)
    save(runtime, "a-last", None)
    with runtime.store.db:
        runtime.store.db.execute(
            "INSERT INTO objects VALUES('notification',?,?,?)", ("x-bad", "{broken", 1)
        )
    params: dict[str, str | int] = {"limit": 1}
    found: list[dict[str, Any]] = []
    omitted = {"invalid": 0, "oversized": 0, "status_unknown": 0}
    for _ in range(10):
        response = await client.get("/api/notification-directory", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        found.extend(page["items"])
        for reason in omitted:
            omitted[reason] += page["omitted"][reason]
        if not page["has_more"]:
            break
        assert page["next_cursor"] != params.get("cursor")
        params["cursor"] = page["next_cursor"]
    else:
        pytest.fail("坏/巨型记录堵塞了目录续页")
    assert [item["id"] for item in found] == ["top", "z-missing-time", "a-last"]
    assert found[1]["created_at"] is found[2]["created_at"] is None
    assert omitted == {"invalid": 1, "oversized": 1, "status_unknown": 0}
    for identifier, code in (
        ("x-bad", "content_unavailable"),
        ("y-oversized", "notification_record_oversized"),
    ):
        error = await client.get(f"/api/notifications/{identifier}")
        assert error.status_code == 400 and error.json()["detail"]["code"] == code


async def test_detail_reconstructs_exact_saved_json_and_rejects_changed_revision(
    notification_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = notification_case
    saved = save(
        runtime,
        "original",
        1,
        content='中文🙂\0"\\\n' * 400,
        source="reminder",
        reminder_id="deleted-or-rescheduled-reminder",
        routing={"channel": "saved-preference-is-not-a-delivery-receipt"},
    )
    raw = runtime.store.db.execute(
        "SELECT CAST(data AS BLOB) FROM objects WHERE kind='notification' AND id='original'"
    ).fetchone()[0]
    runtime.config.update({"limits": {"max_output_chars": 1000}})
    changes = runtime.store.db.total_changes
    params: dict[str, str | int] = {"length_bytes": 503}
    chunks: list[str] = []
    revision = hashlib.sha256(raw).hexdigest()
    for _ in range(100):
        response = await client.get("/api/notifications/original", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(json.dumps(page, ensure_ascii=False)) <= 1000
        assert page["id"] == "original" and page["revision"] == revision
        assert page["format"] == "json"
        assert page["offset_bytes"] == params.get("offset_bytes", 0)
        assert page["length_bytes"] == len(page["content"].encode("utf-8"))
        chunks.append(page["content"])
        if not page["has_more"]:
            assert page["next_offset_bytes"] == len(raw)
            break
        assert page["next_offset_bytes"] > page["offset_bytes"]
        params.update(offset_bytes=page["next_offset_bytes"], revision=revision)
    else:
        pytest.fail("原 JSON 没有在字节页中完整返回")
    assert "".join(chunks).encode("utf-8") == raw
    assert json.loads("".join(chunks)) == saved
    assert runtime.store.db.total_changes == changes
    assert runtime.store.get("notification", saved["id"]) == saved
    await client.patch("/api/notifications/original", json={"read": True})
    stale = await client.get("/api/notifications/original", params=params)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "evidence_changed"


async def test_unread_sparse_window_can_continue_and_cursor_is_filter_bound(
    notification_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = notification_case
    save(runtime, "old-unread", 1)
    for index in range(110):
        save(runtime, f"read-{index:03d}", index + 2, read=True)
    first = await client.get("/api/notification-directory", params={"status": "unread"})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["items"] == [] and page["has_more"]
    assert page["stopped"] == "candidate_budget" and page["scanned"] == 100
    mismatch = await client.get(
        "/api/notification-directory", params={"status": "all", "cursor": page["next_cursor"]}
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "notification_cursor_invalid"
    second = await client.get(
        "/api/notification-directory",
        params={"status": "unread", "cursor": page["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert [item["id"] for item in second.json()["items"]] == ["old-unread"]
    assert second.json()["has_more"] is False


async def test_detail_never_invents_an_identity_and_missing_index_only_degrades_directory(
    notification_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = notification_case
    saved = save(runtime, "good", 1)
    with runtime.store.db:
        runtime.store.db.execute(
            "INSERT INTO objects VALUES('notification',?,?,?)",
            ("mismatched", json.dumps({"id": "another", "content": "wrong"}), 1),
        )
    error = await client.get("/api/notifications/mismatched")
    assert error.status_code == 400 and error.json()["detail"]["code"] == "content_unavailable"
    with runtime.store.db:
        runtime.store.db.execute("DROP INDEX IF EXISTS notifications_created_directory")
    missing = await client.get("/api/notification-directory")
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "notification_directory_unavailable"
    detail = await client.get(f"/api/notifications/{saved['id']}")
    assert detail.status_code == 200
    assert json.loads(detail.json()["content"]) == saved
    assert (
        runtime.store.db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='notifications_created_directory'"
        ).fetchone()
        is None
    )


async def test_detail_follows_the_exact_encoded_legacy_id_with_slashes(
    notification_case: tuple[Runtime, httpx.AsyncClient],
) -> None:
    runtime, client = notification_case
    identifier = "legacy:来源/子项/../仅是通知 ID?保存#原文"
    saved = save(runtime, identifier, 1, source="chiliarch")
    directory = await client.get("/api/notification-directory")
    assert directory.status_code == 200, directory.text
    item = directory.json()["items"][0]
    assert item["id"] == identifier
    response = await client.get(
        "/api/notifications/" + quote(item["id"], safe=""),
        params={"revision": item["revision"]},
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["id"] == identifier and page["has_more"] is False
    assert json.loads(page["content"]) == saved
    assert runtime.store.get("notification", identifier) == saved
