from __future__ import annotations

import json
from collections.abc import Iterator
from os import stat_result
from pathlib import Path
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI

from muli.config import ConfigFile
from muli.plugins import PluginManager
from muli.runtime import Runtime
from muli.server import create_app


def application(directory: Path, *, enabled: bool) -> FastAPI:
    config = ConfigFile(directory / "config.json")
    config.update(
        {
            "plugins": {"enabled": enabled},
            "updates": {"check_on_start": False},
            "onboarding_pending": False,
        }
    )
    return create_app(config, directory / "data")


async def assert_core_available(app: FastAPI) -> None:
    runtime: Runtime = app.state.runtime
    assert runtime.started
    assert not runtime.config.settings.models.main.configured
    assert "fs_read" in runtime.tools
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.post("/api/sessions", json={"title": "插件目录故障隔离"})
        assert response.status_code == 200
        identifier = response.json()["id"]
        detail = await client.get(f"/api/sessions/{identifier}")
        assert detail.status_code == 200
        assert detail.json()["session"]["id"] == identifier
        assert detail.json()["messages"] == []


@pytest.mark.parametrize("root_kind", ["file", "unreadable"], ids=["file", "unreadable"])
async def test_disabled_plugins_never_observe_or_prepare_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_kind: Literal["file", "unreadable"]
) -> None:
    directory = tmp_path / "plugins"
    if root_kind == "file":
        directory.write_bytes(b"existing non-plugin file")
    else:
        directory.mkdir()
    before = directory.stat()
    app = application(tmp_path, enabled=False)
    observations: list[str] = []
    original_mkdir, original_iterdir, original_stat = Path.mkdir, Path.iterdir, Path.stat

    def mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if path == directory:
            observations.append("mkdir")
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def iterdir(path: Path) -> Iterator[Path]:
        if path == directory:
            observations.append("iterdir")
            if root_kind == "unreadable":
                raise PermissionError("private plugin directory detail")
        return original_iterdir(path)

    def stat(path: Path, *, follow_symlinks: bool = True) -> stat_result:
        if path == directory:
            observations.append("stat")
        return original_stat(path, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as observing:
        observing.setattr(Path, "mkdir", mkdir)
        observing.setattr(Path, "iterdir", iterdir)
        observing.setattr(Path, "stat", stat)
        async with app.router.lifespan_context(app):
            await assert_core_available(app)
            assert app.state.plugins.inventory() == []
            assert not app.state.plugins.contexts
            assert observations == []
    after = directory.stat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )
    if root_kind == "file":
        assert directory.read_bytes() == b"existing non-plugin file"


@pytest.mark.parametrize("failure", ["mkdir", "iterdir"], ids=["mkdir", "iterdir"])
async def test_enabled_directory_failure_is_visible_until_a_restarted_manager_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Literal["mkdir", "iterdir"]
) -> None:
    directory = tmp_path / "plugins"
    if failure == "mkdir":
        directory.write_bytes(b"existing non-plugin file")
    else:
        directory.mkdir()
    app = application(tmp_path, enabled=True)
    plugins: PluginManager = app.state.plugins
    original_iterdir = Path.iterdir
    denied = True
    attempts = 0

    def iterdir(path: Path) -> Iterator[Path]:
        nonlocal attempts
        if path == directory:
            attempts += 1
            if denied:
                raise PermissionError("private plugin directory detail")
        return original_iterdir(path)

    with monkeypatch.context() as failing:
        if failure == "iterdir":
            failing.setattr(Path, "iterdir", iterdir)
        async with app.router.lifespan_context(app):
            await assert_core_available(app)
            records = plugins.inventory()
            assert len(records) == 1
            assert records[0]["status"] == "failed"
            error = records[0]["error"]
            assert isinstance(error, str) and error
            assert str(directory) not in error
            assert "private plugin directory detail" not in error
            assert not plugins.contexts
            failure_record = dict(records[0])
            attempts_before_repair = attempts

            if failure == "mkdir":
                directory.unlink()
                directory.mkdir()
            else:
                denied = False
            await plugins.load()
            assert attempts == attempts_before_repair
            assert plugins.inventory() == [failure_record]
            await assert_core_available(app)

    restarted = application(tmp_path, enabled=True)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.plugins is not plugins
        await assert_core_available(restarted)
        assert restarted.state.plugins.inventory() == []
        assert not restarted.state.plugins.contexts
        assert not restarted.state.plugins.restart_required
        assert plugins.inventory() == [failure_record]


@pytest.mark.parametrize("observation", ["directory", "manifest"], ids=["directory", "manifest"])
async def test_unreadable_plugin_entry_keeps_the_next_plugin_and_core_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observation: Literal["directory", "manifest"],
) -> None:
    directory = tmp_path / "plugins"
    broken = directory / "a-broken"
    healthy = directory / "z-healthy"
    for entry in (broken, healthy):
        entry.mkdir(parents=True)
        (entry / "plugin.json").write_text(
            json.dumps({"id": entry.name, "api_version": 1}), encoding="utf-8"
        )
    (broken / "main.py").write_text(
        'raise AssertionError("unreadable plugin must not be imported")\n', encoding="utf-8"
    )
    (healthy / "main.py").write_text(
        "from muli.plugins import PluginContext\n\n"
        "def setup(api: PluginContext) -> None:\n"
        '    api.append_prompt("healthy plugin loaded")\n',
        encoding="utf-8",
    )
    app = application(tmp_path, enabled=True)
    plugins: PluginManager = app.state.plugins
    target = broken if observation == "directory" else broken / "plugin.json"
    original_stat = Path.stat
    attempts = 0

    def stat(path: Path, *, follow_symlinks: bool = True) -> stat_result:
        nonlocal attempts
        if path == target:
            attempts += 1
            raise PermissionError("private plugin leaf detail")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    async with app.router.lifespan_context(app):
        await assert_core_available(app)
        assert attempts > 0
        records = plugins.inventory()
        assert [record["id"] for record in records] == ["a-broken", "z-healthy"]
        assert records[0]["status"] == "failed"
        error = records[0]["error"]
        assert isinstance(error, str) and error
        assert str(target) not in error
        assert "private plugin leaf detail" not in error
        assert records[1]["status"] == "loaded"
        assert [context.identifier for context in plugins.contexts] == ["z-healthy"]
        assert "healthy plugin loaded" in plugins.contexts[0].prompt
