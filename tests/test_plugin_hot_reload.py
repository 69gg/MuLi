from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI

from muli.config import ConfigFile
from muli.plugins import PluginManager
from muli.runtime import Runtime


def setup(tmp_path: Path, *, hot_reload: bool = True) -> tuple[Runtime, PluginManager, FastAPI]:
    config = ConfigFile(tmp_path / "config.json")
    config.update(
        {
            "plugins": {
                "hot_reload": hot_reload,
                "watch_interval_seconds": 0.02,
                "debounce_seconds": 0,
            },
            "updates": {"check_on_start": False},
        }
    )
    runtime = Runtime(config, tmp_path / "data")
    app = FastAPI()
    return runtime, PluginManager(runtime, app, tmp_path / "plugins"), app


def write_plugin(
    directory: Path,
    identifier: str,
    *,
    prompt: str = "v1",
    tool: str = "demo_v1",
    result: str = "one",
) -> None:
    plugin = directory / identifier
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"id": identifier, "api_version": 1}), encoding="utf-8"
    )
    (plugin / "main.py").write_text(
        "from typing import Any\n"
        "from muli.plugins import PluginContext\n"
        "from muli.tools import Tool, parameters\n\n"
        "async def handle(args: dict[str, Any]) -> str:\n"
        f"    return {result!r}\n\n"
        "def setup(api: PluginContext) -> None:\n"
        f"    api.append_prompt({prompt!r})\n"
        f"    api.add_tool(Tool({tool!r}, 'demo', parameters({{}}), handle))\n"
        "    api.add_route('/ping', lambda: {'value': 'new'}, methods=['GET'])\n",
        encoding="utf-8",
    )


async def test_reload_swaps_tools_prompt_and_routes(tmp_path: Path) -> None:
    runtime, plugins, app = setup(tmp_path)
    directory = tmp_path / "plugins"
    write_plugin(directory, "demo")
    try:
        await plugins.load()
        assert plugins.contexts[0].prompt.strip() == "v1"
        assert await runtime.tools["demo_v1"].handler({}) == "one"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get("/api/plugins/demo/ping")).json() == {"value": "new"}
        write_plugin(directory, "demo", prompt="v2", tool="demo_v2", result="two")
        record = await plugins.reload("demo")
        assert record["status"] == "loaded" and record["generation"] == 2
        assert plugins.contexts[0].prompt.strip() == "v2"
        assert "demo_v1" not in runtime.tools
        assert await runtime.tools["demo_v2"].handler({}) == "two"
    finally:
        await plugins.close()
        await runtime.close()


async def test_failed_reload_keeps_old_generation(tmp_path: Path) -> None:
    runtime, plugins, _ = setup(tmp_path)
    directory = tmp_path / "plugins"
    write_plugin(directory, "demo")
    try:
        await plugins.load()
        (directory / "demo" / "main.py").write_text("raise RuntimeError('bad reload')\n", encoding="utf-8")
        record = await plugins.reload("demo")
        assert record["status"] == "degraded"
        assert plugins.contexts[0].prompt.strip() == "v1"
        assert await runtime.tools["demo_v1"].handler({}) == "one"
        assert plugins._generations["demo"].generation == 1
    finally:
        await plugins.close()
        await runtime.close()


async def test_delete_and_disable_remove_generation(tmp_path: Path) -> None:
    runtime, plugins, _ = setup(tmp_path)
    directory = tmp_path / "plugins"
    write_plugin(directory, "demo")
    try:
        await plugins.load()
        import shutil

        shutil.rmtree(directory / "demo")
        record = await plugins.reload("demo")
        assert record["status"] == "removed"
        assert "demo_v1" not in runtime.tools
        assert not plugins.contexts
        runtime.config.update({"plugins": {"disabled": ["demo"], "hot_reload": True}})
        write_plugin(directory, "demo")
        record = await plugins.reload("demo")
        assert record["status"] == "disabled"
        assert "demo_v1" not in runtime.tools
    finally:
        await plugins.close()
        await runtime.close()


async def test_install_triggers_reload_without_restart(tmp_path: Path) -> None:
    runtime, plugins, _ = setup(tmp_path)
    try:
        await plugins.load()
        result = plugins.install(
            "fresh",
            {
                "plugin.json": json.dumps({"id": "fresh", "api_version": 1}),
                "main.py": (
                    "from typing import Any\n"
                    "from muli.plugins import PluginContext\n"
                    "from muli.tools import Tool, parameters\n\n"
                    "async def handle(args: dict[str, Any]) -> str:\n"
                    "    return 'fresh'\n\n"
                    "def setup(api: PluginContext) -> None:\n"
                    "    api.add_tool(Tool('fresh_tool', 'fresh', parameters({}), handle))\n"
                ),
            },
        )
        assert result["restart_required"] is False and result["reload_pending"] is True
        async with asyncio.timeout(2):
            while "fresh" not in plugins._generations:
                await asyncio.sleep(0.01)
        assert await runtime.tools["fresh_tool"].handler({}) == "fresh"
        assert not plugins.restart_required
    finally:
        await plugins.close()
        await runtime.close()


async def test_watcher_reloads_changed_file(tmp_path: Path) -> None:
    runtime, plugins, _ = setup(tmp_path)
    directory = tmp_path / "plugins"
    write_plugin(directory, "demo")
    try:
        await plugins.load()
        write_plugin(directory, "demo", prompt="watched", tool="demo_v1", result="two")
        async with asyncio.timeout(3):
            while plugins.contexts[0].prompt.strip() != "watched":
                await asyncio.sleep(0.02)
        assert await runtime.tools["demo_v1"].handler({}) == "two"
    finally:
        await plugins.close()
        await runtime.close()
