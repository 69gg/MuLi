"""Authenticated draft recovery resolves saved references without reading their bodies."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

import httpx
import pytest

from muli.attachments import MAX_ATTACHMENT_IDS, Attachments
from muli.config import ConfigFile
from muli.providers import ModelConfig
from muli.runtime import Runtime
from muli.server import create_app


def no_model(config: ModelConfig) -> NoReturn:
    raise AssertionError("草稿身份与附件元数据查询不能调用模型")


@asynccontextmanager
async def application_client(
    config: ConfigFile, data: Path, *, raise_app_exceptions: bool = True
) -> AsyncIterator[tuple[Runtime, Attachments, httpx.AsyncClient]]:
    runtime = Runtime(config, data, client_factory=no_model)
    app = create_app(config, runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://127.0.0.1:8765",
        ) as client:
            yield runtime, app.state.attachments, client


async def upload(client: httpx.AsyncClient, name: str, content: bytes) -> dict[str, Any]:
    response = await client.post(
        "/api/uploads",
        content=content,
        headers={"X-File-Name": quote(name), "Content-Type": "text/plain"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_authenticated_status_reports_the_actual_store_identity_after_normal_reopen(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(tmp_path)],
            "server": {"password": "draft-owner-password"},
            "updates": {"check_on_start": False},
        }
    )
    expected: str | None = None
    for _ in range(2):
        async with application_client(config, tmp_path / "data") as (runtime, _, client):
            anonymous = await client.get("/api/status")
            assert anonymous.status_code == 401
            assert "store_instance_id" not in anonymous.json()
            login = await client.post("/api/auth/login", json={"password": "draft-owner-password"})
            assert login.status_code == 200, login.text
            actual = runtime.store.instance_id
            if expected is None:
                expected = actual
            assert actual == expected
            for _ in range(2):
                response = await client.get("/api/status")
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "no-store"
                assert response.json().get("store_instance_id") == expected
                assert not runtime.tasks


async def test_resolve_saved_old_attachments_in_order_without_body_reads_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(tmp_path)],
            "server": {"password": "draft-owner-password"},
            "updates": {"check_on_start": False},
        }
    )
    async with application_client(config, tmp_path / "data") as (runtime, files, client):
        anonymous = await client.get("/api/uploads", params={"ids": "f" * 32})
        assert anonymous.status_code == 401
        login = await client.post("/api/auth/login", json={"password": "draft-owner-password"})
        assert login.status_code == 200, login.text
        raw = "只可定位本地资料，正文不应进入恢复查询🙂".encode()
        first = await upload(client, "原任务要求.txt", raw)
        second = await upload(client, "待比对说明.txt", raw + b" second")
        gone = await upload(client, "原文件已移走.txt", raw + b" missing")
        files.path(gone["id"]).unlink()
        absent = "f" * 32
        assert runtime.store.get("attachment", absent) is None
        for number in range(205):
            runtime.store.put(
                "attachment",
                {
                    "id": f"{number + 1024:032x}",
                    "name": f"后来的资料-{number}.txt",
                    "source": "web",
                    "mime_type": "text/plain",
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "created_at": float(number + 1),
                },
            )
        with runtime.store.db:
            runtime.store.db.execute(
                "UPDATE objects SET updated=0 WHERE kind='attachment' AND id IN (?,?,?)",
                (first["id"], second["id"], gone["id"]),
            )
            runtime.store.db.execute(
                "UPDATE objects SET updated=1 WHERE kind='attachment' AND id NOT IN (?,?,?)",
                (first["id"], second["id"], gone["id"]),
            )
        recent = await client.get("/api/uploads")
        assert recent.status_code == 200, recent.text
        assert set(recent.json()) == {"items", "used_bytes"}
        assert recent.json()["used_bytes"] == sum(item["size"] for item in (first, second, gone))
        assert len(recent.json()["items"]) == 200
        assert {first["id"], second["id"], gone["id"]}.isdisjoint(
            item["id"] for item in recent.json()["items"]
        )
        before_changes = runtime.store.db.total_changes
        before_files = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in files.directory.iterdir()
        }
        requested = [second["id"], absent, first["id"], gone["id"]]
        params = [("ids", identifier) for identifier in requested]
        expected = {"items": [second, first], "unavailable_ids": [absent, gone["id"]]}
        response = await client.get("/api/uploads", params=params)
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 2
        assert response.json() == expected
        original_open = Path.open

        def no_attachment_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path.parent == files.directory:
                raise AssertionError("附件恢复只读元数据，不得打开、读取或写入正文")
            return original_open(path, *args, **kwargs)

        def no_recent_window(*args: Any, **kwargs: Any) -> NoReturn:
            raise AssertionError("按准确附件ID恢复不能改为最近窗口查找")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", no_attachment_open)
            patch.setattr(files, "list", no_recent_window)
            response = await client.get("/api/uploads", params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == expected
        assert raw.decode() not in response.text
        assert runtime.store.db.total_changes == before_changes
        assert {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in files.directory.iterdir()
        } == before_files
        assert runtime.store.list("message") == []
        assert not runtime.tasks


async def test_repeated_id_query_rejects_ambiguous_or_unbounded_requests_before_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    async with application_client(config, tmp_path / "data") as (runtime, files, client):
        # 首次请求让启动消费任务完成游标落盘，再计量后续参数查询本身的写入。
        ready = await client.get("/api/uploads")
        assert ready.status_code == 200, ready.text
        assert runtime.store.get("topic_reminder_state", "current") is not None
        identifiers = [f"{number + 1:032x}" for number in range(MAX_ATTACHMENT_IDS + 1)]
        before_changes = runtime.store.db.total_changes

        def no_lookup(*args: Any, **kwargs: Any) -> NoReturn:
            raise AssertionError("无效附件查询必须在元数据查找前拒绝")

        with monkeypatch.context() as patch:
            patch.setattr(files, "get", no_lookup)
            for supplied in ([identifiers[0], identifiers[0]], ["not-an-id"], identifiers):
                response = await client.get(
                    "/api/uploads", params=[("ids", identifier) for identifier in supplied]
                )
                assert response.status_code == 422, response.text
        selected = identifiers[:MAX_ATTACHMENT_IDS]
        response = await client.get(
            "/api/uploads", params=[("ids", identifier) for identifier in selected]
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [], "unavailable_ids": selected}
        assert runtime.store.db.total_changes == before_changes
        assert not runtime.tasks


async def test_database_and_permission_failures_are_not_missing_references_or_private_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"workspace_roots": [str(tmp_path)], "updates": {"check_on_start": False}})
    async with application_client(config, tmp_path / "data", raise_app_exceptions=False) as (
        runtime,
        files,
        client,
    ):
        item = await upload(client, "只核对元数据.txt", b"private-body-must-stay-local")
        before_changes = runtime.store.db.total_changes
        failures: list[tuple[str, Exception]] = [
            ("get", sqlite3.OperationalError("private-database-path-and-credential")),
            ("path", PermissionError("private-filesystem-path-and-credential")),
        ]
        for method, failure in failures:

            def fail(*args: Any, _failure: Exception = failure, **kwargs: Any) -> NoReturn:
                raise _failure

            with monkeypatch.context() as patch:
                patch.setattr(files, method, fail)
                response = await client.get("/api/uploads", params={"ids": item["id"]})
            assert response.status_code == 500, response.text
            assert "unavailable_ids" not in response.text
            assert "private-" not in response.text
        assert runtime.store.db.total_changes == before_changes
        assert runtime.store.list("message") == []
        assert not runtime.tasks


async def test_authenticated_id_reads_keep_maintenance_and_logout_boundaries(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "workspace_roots": [str(tmp_path)],
            "server": {"password": "draft-owner-password"},
            "updates": {"check_on_start": False},
        }
    )
    async with application_client(config, tmp_path / "data") as (runtime, _, client):
        login = await client.post("/api/auth/login", json={"password": "draft-owner-password"})
        assert login.status_code == 200, login.text
        item = await upload(client, "维护期间的原资料.txt", b"only-local-reference")
        runtime.maintenance.admission.pause()
        before_changes = runtime.store.db.total_changes
        response = await client.get("/api/uploads", params={"ids": item["id"]})
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [item], "unavailable_ids": []}
        assert response.headers["cache-control"] == "no-store"
        assert not runtime.maintenance.admission.allowed
        blocked = await client.post(
            "/api/uploads",
            content=b"new-file-must-not-arrive",
            headers={"X-File-Name": "blocked.txt", "Content-Type": "text/plain"},
        )
        assert blocked.status_code == 409, blocked.text
        assert (await client.get("/api/status")).status_code == 409
        cross_site = await client.get(
            "/api/uploads",
            params={"ids": item["id"]},
            headers={"Origin": "https://untrusted.invalid"},
        )
        assert cross_site.status_code == 403
        assert item["id"] not in cross_site.text
        assert runtime.store.db.total_changes == before_changes
        logout = await client.post("/api/auth/logout", json={})
        assert logout.status_code == 200, logout.text
        anonymous = await client.get("/api/uploads", params={"ids": item["id"]})
        assert anonymous.status_code == 401
        assert item["id"] not in anonymous.text
        assert runtime.store.db.total_changes == before_changes
        assert runtime.store.list("message") == []
        assert not runtime.tasks
