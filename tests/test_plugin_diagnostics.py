from __future__ import annotations

import asyncio
import json
from os import stat_result
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from test_runtime import make_runtime

from muli.config import PluginSettings
from muli.plugins import PluginManager, inspect_plugin
from muli.providers import Message, ModelTurn


def package(root: Path, identifier: str = "guard", *, source: str = "") -> Path:
    directory = root / identifier
    directory.mkdir(parents=True)
    (directory / "plugin.json").write_text(
        json.dumps({"id": identifier, "api_version": 1, "version": "1.0"}), encoding="utf-8"
    )
    (directory / "main.py").write_text(source, encoding="utf-8")
    return directory


async def test_context_failure_blocks_model_until_same_hook_recovers(tmp_path: Path) -> None:
    runtime, seen = make_runtime(tmp_path, [ModelTurn(content="恢复后的回答")])
    manager = PluginManager(runtime, FastAPI(), tmp_path / "plugins")
    directory = package(
        manager.directory,
        source=(
            "from muli.plugins import PluginContext\n"
            "from muli.providers import Message\n"
            "def setup(api: PluginContext) -> None:\n"
            "    async def context(session: str, messages: list[Message]) -> list[Message]:\n"
            "        if (api.directory / 'broken').exists():\n"
            "            raise ValueError('PRIVATE_PLUGIN_MESSAGE')\n"
            "        return messages\n"
            "    api.context_hooks.append(context)\n"
        ),
    )
    broken = directory / "broken"
    broken.touch()
    try:
        await manager.load()
        session = runtime.new_session()
        for _ in range(2):
            result = await runtime.start(session["id"], "继续处理")
            with pytest.raises(ValueError, match="PRIVATE_PLUGIN_MESSAGE"):
                await runtime.tasks[session["id"]]
            assert runtime.store.get("run", result["run_id"])["status"] == "failed"
            record = manager.inventory()[0]
            assert record["status"] == "degraded"
            assert record["error"] == "plugin_context_failed"
            assert record["phase"] == "context" and record["error_type"] == "ValueError"
            assert "PRIVATE" not in json.dumps(record)
            assert seen == []
        broken.unlink()
        await runtime.start(session["id"], "再次处理")
        assert await runtime.tasks[session["id"]] == "恢复后的回答"
        assert len(seen) == 1
        record = manager.inventory()[0]
        assert record["status"] == "loaded" and record["error"] == ""
        assert "phase" not in record and "error_type" not in record
    finally:
        await manager.close()
        await runtime.close()


@pytest.mark.parametrize("failure", ["timeout", "not-list", "not-message"])
async def test_invalid_or_timed_out_hook_is_visible_and_not_skipped(
    tmp_path: Path, failure: str
) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    runtime.config.settings.plugins.hook_timeout = 0.01
    manager = PluginManager(runtime, FastAPI(), tmp_path / "plugins")
    body = {
        "timeout": "await asyncio.Event().wait()",
        "not-list": "return None",
        "not-message": "return [{}]",
    }[failure]
    package(
        manager.directory,
        source=(
            "import asyncio\n"
            "from muli.plugins import PluginContext\n"
            "from muli.providers import Message\n"
            "def setup(api: PluginContext) -> None:\n"
            "    async def context(session: str, messages: list[Message]) -> list[Message]:\n"
            f"        {body}\n"
            "    api.context_hooks.append(context)\n"
        ),
    )
    error = TimeoutError if failure == "timeout" else TypeError
    try:
        await manager.load()
        for _ in range(2):
            with pytest.raises(error):
                await runtime.prepare_context("main", [Message(role="user", content="你好")])
            assert manager.inventory()[0]["error_type"] == error.__name__
            assert manager.inventory()[0]["status"] == "degraded"
    finally:
        await manager.close()
        await runtime.close()


async def test_normal_cancellation_does_not_mark_plugin_failed(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, [])
    manager = PluginManager(runtime, FastAPI(), tmp_path / "plugins")
    package(manager.directory, source="def setup(api) -> None:\n    pass\n")
    entered = asyncio.Event()

    async def waiting(session: str, messages: list[Message]) -> list[Message]:
        entered.set()
        await asyncio.Event().wait()
        return messages

    try:
        await manager.load()
        manager.contexts[0].context_hooks.append(waiting)
        task = asyncio.create_task(manager.context("main", []))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.inventory()[0]["status"] == "loaded"
        assert manager.inventory()[0]["error"] == ""
    finally:
        await manager.close()
        await runtime.close()


def test_inspection_is_targeted_and_does_not_execute_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "must-not-exist"
    root = tmp_path / "plugins"
    directory = package(root, source=f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("inspection must not enumerate other plugins")

    monkeypatch.setattr(Path, "iterdir", forbidden)
    result = inspect_plugin(root, "guard", PluginSettings())
    assert result == {
        "id": "guard",
        "configured_enabled": True,
        "code_executed": False,
        "runtime_health": "not_checked",
        "name": "guard",
        "version": "1.0",
        "entry": "main.py",
        "status": "ready",
    }
    assert not marker.exists() and directory.is_dir()


@pytest.mark.parametrize("disabled_by", ["all", "config", "manifest"])
def test_inspection_distinguishes_disabled_structure(tmp_path: Path, disabled_by: str) -> None:
    root = tmp_path / "plugins"
    directory = package(root)
    settings = PluginSettings(
        enabled=disabled_by != "all", disabled=["guard"] if disabled_by == "config" else []
    )
    if disabled_by == "manifest":
        (directory / "plugin.json").write_text(
            '{"id":"guard","api_version":1,"enabled":false}', encoding="utf-8"
        )
    result = inspect_plugin(root, "guard", settings)
    assert result["status"] == "disabled"
    assert result["configured_enabled"] is (disabled_by == "manifest")
    assert result["runtime_health"] == "not_checked"


@pytest.mark.parametrize("invalid", ["missing", "json", "version", "escape", "symlink", "large"])
def test_inspection_rejects_invalid_structure_without_executing_code(
    tmp_path: Path, invalid: str
) -> None:
    root = tmp_path / "plugins"
    directory = package(root)
    manifest = directory / "plugin.json"
    entry = directory / "main.py"
    if invalid == "missing":
        entry.unlink()
    elif invalid == "json":
        manifest.write_text("{PRIVATE_INVALID_JSON", encoding="utf-8")
    elif invalid == "version":
        manifest.write_text('{"id":"guard","api_version":2}', encoding="utf-8")
    elif invalid == "escape":
        manifest.write_text(
            '{"id":"guard","api_version":1,"entry":"../outside.py"}', encoding="utf-8"
        )
        (root / "outside.py").write_text("raise AssertionError('PRIVATE')", encoding="utf-8")
    elif invalid == "symlink":
        entry.unlink()
        entry.symlink_to(manifest)
    else:
        entry.write_text("#" * 1025, encoding="utf-8")
    result = inspect_plugin(root, "guard", PluginSettings(max_source_bytes=1024))
    assert result["status"] == "invalid" and result["error"] == "plugin_structure_invalid"
    assert result["code_executed"] is False and "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("identifier", ["../guard", "", "Guard", "a/b"])
def test_inspection_rejects_invalid_identifier(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(ValueError, match="plugin_id_invalid"):
        inspect_plugin(tmp_path, identifier, PluginSettings())


def test_manifest_growth_after_stat_cannot_bypass_read_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plugins"
    manifest = package(root) / "plugin.json"
    before = manifest.stat()
    manifest.write_text(
        json.dumps({"id": "guard", "api_version": 1, "description": "x" * 2048}),
        encoding="utf-8",
    )
    original = Path.stat

    def stat(path: Path, *, follow_symlinks: bool = True) -> stat_result:
        return before if path == manifest else original(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    result = inspect_plugin(root, "guard", PluginSettings(max_source_bytes=1024))
    assert result["status"] == "invalid" and result["error"] == "plugin_structure_invalid"
