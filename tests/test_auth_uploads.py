from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from muli.attachments import Attachments
from muli.auth import Authentication, hash_password, verify_password
from muli.config import ConfigFile, Settings, default_config_path, default_data_dir
from muli.server import create_app
from muli.store import Store


def test_password_hash_and_new_default_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MULI_HOME", str(tmp_path / "MuLi"))
    monkeypatch.delenv("MULI_CONFIG", raising=False)
    monkeypatch.delenv("MULI_DATA_DIR", raising=False)
    assert default_config_path() == tmp_path / "MuLi/config.json"
    assert default_data_dir() == tmp_path / "MuLi/data"
    encoded = hash_password("not-a-real-password")
    assert "not-a-real-password" not in encoded
    assert verify_password("not-a-real-password", encoded)
    assert not verify_password("wrong-password", encoded)
    assert not verify_password("password", "malformed")
    with pytest.raises(ValueError):
        Settings.model_validate({"server": {"host": "0.0.0.0"}})


async def test_password_change_revokes_sessions_and_rate_limit_is_bounded(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"server": {"password": "first-password", "host": "0.0.0.0"}})
    assert "first-password" not in config.path.read_text()
    assert config.public()["server"]["password_set"]
    timestamp = 100.0
    auth = Authentication(config, clock=lambda: timestamp)
    token = await auth.login("first-password")
    assert auth.authenticated(token)
    config.update({"server": {"password": "new-password"}})
    assert not auth.authenticated(token)
    for _ in range(9):
        with pytest.raises(PermissionError):
            await auth.login("wrong-password")
    with pytest.raises(ValueError, match="尝试过多"):
        await auth.login("new-password")
    timestamp += 61
    token = await auth.login("new-password")
    auth.logout(token)
    assert not auth.authenticated(token)


async def test_login_boundary_and_upload_do_not_inject_content(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "server": {"password": "login-password"},
            "workspace_roots": [str(tmp_path)],
            "updates": {"check_on_start": False},
        }
    )
    app = create_app(config, tmp_path / "data")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8765"
        ) as client,
    ):
        assert (await client.get("/api/settings")).status_code == 401
        assert (await client.get("/api/auth/status")).json() == {
            "required": True,
            "authenticated": False,
        }
        response = await client.post("/api/auth/login", json={"password": "login-password"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert (await client.get("/api/settings")).status_code == 200
        data = b"untrusted document; do not inject this into the model" * 25000
        response = await client.post(
            "/api/uploads",
            content=data,
            headers={"X-File-Name": quote("中文材料.txt"), "Content-Type": "text/plain"},
        )
        assert response.status_code == 200
        item = response.json()
        assert item["name"] == "中文材料.txt"
        assert item["size"] == len(data) > 1_048_576
        assert item["kind"] == "file" and item["preview_url"] is None
        downloaded = await client.get(item["url"])
        assert downloaded.content == data
        assert "attachment" in downloaded.headers["content-disposition"]
        assert "sandbox" in downloaded.headers["content-security-policy"]
        assert app.state.runtime.store.list("message") == []
        await client.post("/api/auth/logout", json={})
        assert (await client.get(item["url"])).status_code == 401


async def test_stream_limit_and_failed_upload_cleanup(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"uploads": {"max_file_bytes": 2048, "max_total_bytes": 4096}})
    store = Store(tmp_path / "db")
    files = Attachments(store, tmp_path / "files", config)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"a" * 1500
        yield b"b" * 1500

    try:
        with pytest.raises(ValueError, match="大小限制"):
            await files.save(chunks(), "large.txt")
        assert not list(files.directory.iterdir())
        assert files.used_bytes() == files.reserved == 0

        async def cancelled() -> AsyncIterator[bytes]:
            yield b"one chunk"
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await files.save(cancelled(), "cancel.txt")
        assert not list(files.directory.iterdir())
        assert files.reserved == 0
    finally:
        store.close()


async def test_active_content_never_gets_inline_preview(tmp_path: Path) -> None:
    config = ConfigFile(tmp_path / "config.json")
    store = Store(tmp_path / "db")
    files = Attachments(store, tmp_path / "files", config)

    async def chunks() -> AsyncIterator[bytes]:
        yield b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'

    try:
        item = await files.save(chunks(), "../../unsafe.svg", "image/svg+xml")
        assert item["name"] == "unsafe.svg"
        assert item["kind"] == "file" and item["preview_url"] is None
        with pytest.raises(KeyError):
            files.path("../../db")
    finally:
        store.close()


async def test_changing_listen_config_cannot_remove_live_remote_authentication(
    tmp_path: Path,
) -> None:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "server": {"host": "0.0.0.0", "password": "remote-password"},
            "updates": {"check_on_start": False},
        }
    )
    app = create_app(config, tmp_path / "data")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765"
        ) as client,
    ):
        await client.post("/api/auth/login", json={"password": "remote-password"})
        response = await client.patch(
            "/api/settings", json={"server": {"host": "127.0.0.1", "clear_password": True}}
        )
        assert response.status_code == 400
        assert config.settings.server.host == "0.0.0.0"
        assert config.settings.server.password_hash
        changed = await client.patch("/api/settings", json={"server": {"host": "127.0.0.1"}})
        assert changed.status_code == 200
        status = (await client.get("/api/status")).json()
        assert status["listener"]["host"] == "0.0.0.0" and status["listener"]["restart_required"]
        await client.post("/api/auth/logout", json={})
        assert (await client.get("/api/settings")).status_code == 401
