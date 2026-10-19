from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from muli.config import ConfigFile, Settings
from muli.management import SelfManagement
from muli.plugins import PluginManager
from muli.runtime import Runtime
from muli.service import UserService
from muli.updates import Updates


def setup_runtime(tmp_path: Path) -> tuple[Runtime, PluginManager]:
    config = ConfigFile(tmp_path / "config.json")
    config.update({"updates": {"check_on_start": False}})
    runtime = Runtime(config, tmp_path / "data")
    return runtime, PluginManager(runtime, FastAPI(), tmp_path / "plugins")


async def settled(manager: SelfManagement, identifier: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            revision = manager.runtime.bus.revision
            item = manager.runtime.store.get("management", identifier)
            assert item is not None
            if item["status"] not in {"pending", "applying"}:
                return item

            def changed(expected: int = revision) -> bool:
                return manager.runtime.bus.revision != expected

            async with manager.runtime.bus.changed:
                await manager.runtime.bus.changed.wait_for(changed)


async def test_self_configuration_waits_for_idle_and_keeps_secrets_private(tmp_path: Path) -> None:
    runtime, plugins = setup_runtime(tmp_path)
    applied = asyncio.Event()

    async def apply(settings: Settings) -> None:
        assert not runtime.busy
        runtime.config.replace(settings)
        await runtime.reconfigure()
        applied.set()

    manager = SelfManagement(
        runtime, plugins, UserService(runtime.config.path, runtime.data_dir), apply
    )
    release = asyncio.Event()

    async def work() -> str:
        await release.wait()
        return "finished"

    task = asyncio.create_task(work())
    runtime.tasks["test"] = task
    task.add_done_callback(lambda _: runtime.tasks.pop("test", None))
    try:
        await manager.start()
        item = await manager.stage(
            "configure",
            {
                "persona": {"user_name": "小林"},
                "models": {"main": {"api_key": "private-fixture-key"}},
            },
        )
        await asyncio.sleep(0)
        assert not applied.is_set()
        assert runtime.config.settings.persona.user_name == "你"
        assert "private-fixture-key" not in json.dumps(runtime.store.events())
        snapshot = manager.directory / f"{item['id']}.json"
        if sys.platform != "win32":
            assert snapshot.stat().st_mode & 0o777 == 0o600
        release.set()
        await applied.wait()
        assert (await settled(manager, item["id"]))["status"] == "completed"
        assert runtime.config.settings.persona.user_name == "小林"
        assert runtime.config.settings.models.main.api_key == "private-fixture-key"
        assert not snapshot.exists()
        assert runtime.tools["self_update"].requires_approval
    finally:
        release.set()
        await manager.close()
        await runtime.close()


async def test_configuration_plan_detects_stale_snapshot_and_invalid_changes(
    tmp_path: Path,
) -> None:
    runtime, plugins = setup_runtime(tmp_path)
    called = False

    async def apply(settings: Settings) -> None:
        nonlocal called
        called = True

    manager = SelfManagement(
        runtime, plugins, UserService(runtime.config.path, runtime.data_dir), apply
    )
    try:
        with pytest.raises(ValueError):
            await manager.stage("configure", {"server": {"port": -1}})
        assert manager.plans() == []
        item = await manager.stage("configure", {"persona": {"user_name": "旧计划"}})
        runtime.config.update({"persona": {"user_name": "最新手动修改"}})
        await manager.start()
        result = await settled(manager, item["id"])
        assert result["status"] == "failed" and "其它入口" in result["error"]
        assert not called and runtime.config.settings.persona.user_name == "最新手动修改"
    finally:
        await manager.close()
        await runtime.close()


async def test_plugin_install_is_staged_and_override_survives_reconfigure(tmp_path: Path) -> None:
    runtime, plugins = setup_runtime(tmp_path)
    source = """from typing import Any
from muli.plugins import PluginContext
from muli.tools import Tool, parameters

async def handle(args: dict[str, Any]) -> str:
    return "plugin result"

def setup(api: PluginContext) -> None:
    api.add_tool(Tool("fs_read", "user override", parameters({}), handle))
    api.append_prompt("插件增加的个性化表达")
"""
    try:
        result = plugins.install(
            "personal",
            {"plugin.json": json.dumps({"id": "personal", "api_version": 1}), "main.py": source},
        )
        assert result["restart_required"]
        assert runtime.tools["fs_read"].description != "user override"
        await plugins.load()
        assert plugins.inventory()[0]["status"] == "loaded"
        assert await runtime.tools["fs_read"].handler({}) == "plugin result"
        await runtime.reconfigure()
        assert await runtime.tools["fs_read"].handler({}) == "plugin result"
        with pytest.raises(FileExistsError):
            plugins.install("personal", {"main.py": ""})
        with pytest.raises(ValueError):
            plugins.install("bad", {"../outside.py": "print('bad')"})
        assert not (tmp_path / "outside.py").exists()
    finally:
        await plugins.close()
        await runtime.close()


async def test_broken_plugin_does_not_keep_routes_or_prevent_other_plugins(tmp_path: Path) -> None:
    runtime, plugins = setup_runtime(tmp_path)
    routes = len(plugins.app.routes)
    plugins.install(
        "broken",
        {
            "plugin.json": json.dumps({"id": "broken", "api_version": 1}),
            "main.py": "def setup(api):\n    api.add_route('/broken', lambda: {}, methods=['GET'])\n    raise ValueError('fixture failure')\n",
        },
    )
    try:
        await plugins.load()
        assert plugins.inventory()[0]["status"] == "failed"
        assert len(plugins.app.routes) == routes
        assert "fs_read" in runtime.tools
    finally:
        await plugins.close()
        await runtime.close()


async def test_version_check_is_bounded_and_notifies_once(tmp_path: Path) -> None:
    runtime, _ = setup_runtime(tmp_path)
    updates = Updates(runtime.config, runtime.store, runtime.bus)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"info": {"name": "muli-agent", "version": "99.0.0"}})
    )
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await updates.check(client))["available"]
            assert (await updates.check(client))["available"]
        assert len(runtime.store.list("notification")) == 1
        runtime.config.update({"updates": {"max_metadata_bytes": 1024}})
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 2048))
        ) as client:
            result = await updates.check(client)
        assert result["status"] == "unavailable" and not result["available"]
    finally:
        await runtime.close()


async def test_update_refuses_to_change_development_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = setup_runtime(tmp_path)
    calls: list[list[str]] = []

    async def command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        assert all(
            key.lower() not in {"http_proxy", "https_proxy", "all_proxy"} for key in kwargs["env"]
        )
        return {"exit_code": 0, "output": str(tmp_path / "unrelated-tools")}

    monkeypatch.setattr("muli.updates.shutil.which", lambda _: "uv")
    monkeypatch.setattr("muli.updates.run_process", command)
    try:
        with pytest.raises(ValueError, match="开发环境"):
            await Updates(runtime.config, runtime.store, runtime.bus).apply("2.0.0")
        assert calls == [["uv", "tool", "dir"]]
    finally:
        await runtime.close()


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_update_retains_uv_tool_requirements_and_reports_constraint_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    runtime, _ = setup_runtime(tmp_path)
    root = tmp_path / "tools"
    calls: list[list[str]] = []

    async def command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(argv)
        if argv[1:] == ["tool", "dir"]:
            return {"exit_code": 0, "output": str(root)}
        return {"exit_code": exit_code, "output": ""}

    monkeypatch.setattr("muli.updates.shutil.which", lambda _: "uv")
    monkeypatch.setattr("muli.updates.run_process", command)
    monkeypatch.setattr("muli.updates.sys.prefix", str(root / "muli-agent"))
    try:
        updates = Updates(runtime.config, runtime.store, runtime.bus)
        if exit_code:
            with pytest.raises(ValueError, match="原有版本约束"):
                await updates.apply("2.0.0")
        else:
            result = await updates.apply("2.0.0")
            assert result == {"installed": "2.0.0", "source": "absent", "restart_required": True}
        assert calls == [
            ["uv", "tool", "dir"],
            ["uv", "tool", "upgrade", "--no-python-downloads", "muli-agent==2.0.0"],
        ]
    finally:
        await runtime.close()


async def test_new_plugin_is_visible_before_restart_without_executing_it(tmp_path: Path) -> None:
    runtime, plugins = setup_runtime(tmp_path)
    try:
        await plugins.load()
        installed = plugins.install(
            "pending",
            {
                "plugin.json": '{"id":"pending","api_version":1,"entry":"main.py"}',
                "main.py": 'raise RuntimeError("must not execute during installation")\n',
            },
        )
        assert installed["restart_required"] and plugins.restart_required
        assert plugins.inventory() == [
            {
                "id": "pending",
                "name": "pending",
                "version": "",
                "status": "installed",
                "restart_required": True,
            }
        ]
        assert not plugins.contexts
    finally:
        await plugins.close()
        await runtime.close()
