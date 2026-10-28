"""User-owned Python extensions outside the installed distribution.

Plugins run as trusted local code, not in a security sandbox. All registered tools
still pass through the runtime permission gate when requested by a model.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from muli.config import PluginSettings
from muli.providers import Message
from muli.runtime import ClientFactory, ContextHook, Runtime
from muli.tools import Tool

LifecycleHook = Callable[[], Awaitable[None]]
PLUGIN_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def _manifest(directory: Path, max_source_bytes: int) -> dict[str, Any]:
    if not PLUGIN_ID.fullmatch(directory.name):
        raise ValueError("插件目录名称无效")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("插件目录无效")
    manifest = directory / "plugin.json"
    if (
        manifest.is_symlink()
        or not manifest.is_file()
        or manifest.stat().st_size > max_source_bytes
    ):
        raise ValueError("插件清单无效或过大")
    with manifest.open("rb") as source:
        raw = source.read(max_source_bytes + 1)
    if len(raw) > max_source_bytes:
        raise ValueError("插件清单无效或过大")
    data = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("id") != directory.name
        or data.get("api_version") != 1
    ):
        raise ValueError("插件ID或API版本不匹配")
    return data


def _entry(directory: Path, data: dict[str, Any], max_source_bytes: int) -> Path:
    entry = directory / str(data.get("entry", "main.py"))
    if (
        entry.is_symlink()
        or not entry.resolve().is_relative_to(directory.resolve())
        or entry.suffix != ".py"
        or not entry.is_file()
    ):
        raise ValueError("插件入口必须是插件目录内的Python文件")
    if entry.stat().st_size > max_source_bytes:
        raise ValueError("插件入口超过大小限制")
    return entry


def inspect_plugin(directory: Path, identifier: str, settings: PluginSettings) -> dict[str, Any]:
    """Check one installed structure without importing or executing its entry."""
    if not PLUGIN_ID.fullmatch(identifier):
        raise ValueError("plugin_id_invalid")
    result: dict[str, Any] = {
        "id": identifier,
        "configured_enabled": settings.enabled and identifier not in settings.disabled,
        "code_executed": False,
        "runtime_health": "not_checked",
    }
    try:
        package = directory / identifier
        data = _manifest(package, settings.max_source_bytes)
        entry = _entry(package, data, settings.max_source_bytes)
        enabled = result["configured_enabled"] and data.get("enabled", True) is not False
        result.update(
            name=str(data.get("name", identifier))[:120],
            version=str(data.get("version", ""))[:80],
            entry=entry.relative_to(package).as_posix(),
            status="ready" if enabled else "disabled",
        )
    except (OSError, ValueError) as error:
        result.update(
            status="invalid", error="plugin_structure_invalid", error_type=type(error).__name__
        )
    return result


@dataclass
class PluginContext:
    identifier: str
    directory: Path
    runtime: Runtime
    app: FastAPI
    tools: list[Tool] = field(default_factory=list)
    context_hooks: list[ContextHook] = field(default_factory=list)
    startup_hooks: list[LifecycleHook] = field(default_factory=list)
    shutdown_hooks: list[LifecycleHook] = field(default_factory=list)
    model_factory: ClientFactory | None = None
    prompt: str = ""

    def add_tool(self, tool: Tool) -> None:
        if any(item.name == tool.name for item in self.tools):
            raise ValueError("插件内工具名称重复")
        self.tools.append(tool)

    def append_prompt(self, text: str) -> None:
        if len(self.prompt) + len(text) > 32000:
            raise ValueError("插件提示词超过限制")
        self.prompt += "\n" + text

    def add_route(self, path: str, endpoint: Callable[..., Any], *, methods: list[str]) -> None:
        if not path.startswith("/") or ".." in path or "?" in path:
            raise ValueError("插件路由必须是相对插件入口的路径")
        self.app.add_api_route(f"/api/plugins/{self.identifier}{path}", endpoint, methods=methods)


class PluginManager:
    def __init__(self, runtime: Runtime, app: FastAPI, directory: Path) -> None:
        self.runtime, self.app, self.directory = runtime, app, directory
        self.contexts: list[PluginContext] = []
        self.records: list[dict[str, Any]] = []
        self._tools: dict[str, Tool] = {}
        self._base_factory = runtime.client_factory
        self._started = False
        self._installed_since_start = False
        self._loaded_settings = runtime.config.settings.plugins.model_dump()

    @property
    def restart_required(self) -> bool:
        return (
            self._installed_since_start
            or self.runtime.config.settings.plugins.model_dump() != self._loaded_settings
        )

    def inventory(self) -> list[dict[str, Any]]:
        return list(self.records)

    async def load(self) -> None:
        if self._started:
            return
        self._started = True
        self._installed_since_start = False
        self._loaded_settings = self.runtime.config.settings.plugins.model_dump()
        settings = self.runtime.config.settings.plugins
        if not settings.enabled:
            return
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directories = sorted(self.directory.iterdir())
        except OSError:
            self.records.append(
                {
                    "id": "__directory__",
                    "name": "插件目录",
                    "status": "failed",
                    "error": "plugin_directory_unavailable",
                }
            )
            return
        for directory in directories:
            record: dict[str, Any] = {"id": directory.name, "status": "failed", "error": ""}
            recorded = False
            route_count = len(self.app.router.routes)
            module_name = f"_muli_plugin_{directory.name}"
            context: PluginContext | None = None
            try:
                if not directory.is_dir() or directory.is_symlink():
                    continue
                manifest = directory / "plugin.json"
                if not manifest.is_file():
                    continue
                self.records.append(record)
                recorded = True
                data = _manifest(directory, settings.max_source_bytes)
                record.update(
                    name=str(data.get("name", directory.name))[:120],
                    version=str(data.get("version", ""))[:80],
                )
                if directory.name in settings.disabled or data.get("enabled", True) is False:
                    record["status"] = "disabled"
                    continue
                entry = _entry(directory, data, settings.max_source_bytes)
                spec = importlib.util.spec_from_file_location(
                    module_name, entry, submodule_search_locations=[str(directory)]
                )
                if spec is None or spec.loader is None:
                    raise ValueError("无法加载插件入口")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                context = PluginContext(directory.name, directory, self.runtime, self.app)
                async with asyncio.timeout(settings.hook_timeout):
                    result = module.setup(context)
                    if inspect.isawaitable(result):
                        await result
                    collisions = set(self._tools).intersection(tool.name for tool in context.tools)
                    if collisions:
                        raise ValueError("多个插件覆盖相同工具：" + ",".join(sorted(collisions)))
                    for hook in context.startup_hooks:
                        await hook()
                self.contexts.append(context)
                self._tools.update({tool.name: tool for tool in context.tools})
                if context.model_factory is not None:
                    self.runtime.client_factory = context.model_factory
                record["status"] = "loaded"
                record["tools"] = [tool.name for tool in context.tools]
            except Exception as exc:  # user extension failure must not prevent core startup
                if not recorded:
                    if not isinstance(exc, OSError):
                        raise
                    self.records.append(record)
                if context is not None:
                    for hook in reversed(context.shutdown_hooks):
                        try:
                            async with asyncio.timeout(settings.hook_timeout):
                                await hook()
                        except Exception:
                            continue
                del self.app.router.routes[route_count:]
                sys.modules.pop(module_name, None)
                record["error"] = (
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                    if recorded
                    else "plugin_entry_unavailable"
                )
        self.runtime.add_tool_provider(lambda _: list(self._tools.values()), replace=True)
        self.runtime.context_hooks.append(self.context)

    async def context(self, session_id: str, messages: list[Message]) -> list[Message]:
        for context in self.contexts:
            record = next(item for item in self.records if item["id"] == context.identifier)
            try:
                if context.prompt:
                    messages = [Message(role="system", content=context.prompt), *messages]
                for hook in context.context_hooks:
                    async with asyncio.timeout(self.runtime.config.settings.plugins.hook_timeout):
                        result = await hook(session_id, messages)
                    if not isinstance(result, list) or any(
                        not isinstance(item, Message) for item in result
                    ):
                        raise TypeError("插件上下文钩子必须返回消息列表")
                    messages = result
            except Exception as error:
                record.update(
                    status="degraded",
                    error="plugin_context_failed",
                    phase="context",
                    error_type=type(error).__name__[:80],
                )
                raise
            if record.get("error") == "plugin_context_failed":
                record.update(status="loaded", error="")
                record.pop("phase", None)
                record.pop("error_type", None)
        return messages

    async def close(self) -> None:
        for context in reversed(self.contexts):
            for hook in reversed(context.shutdown_hooks):
                try:
                    async with asyncio.timeout(self.runtime.config.settings.plugins.hook_timeout):
                        await hook()
                except Exception:
                    continue
        self.runtime.client_factory = self._base_factory

    def install(self, identifier: str, files: dict[str, str]) -> dict[str, Any]:
        if not PLUGIN_ID.fullmatch(identifier) or not files or len(files) > 30:
            raise ValueError("插件ID或文件数量无效")
        directory = self.directory / identifier
        if directory.exists():
            raise FileExistsError("插件已存在；修改请使用文件工具并重启")
        total = sum(len(content.encode()) for content in files.values())
        if total > self.runtime.config.settings.plugins.max_source_bytes:
            raise ValueError("插件源码超过限制")
        for name in files:
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or "\x00" in name
            ):
                raise ValueError("插件文件路径无效")
        data = json.loads(files.get("plugin.json", "{}"))
        if data.get("id") != identifier or data.get("api_version") != 1:
            raise ValueError("需要匹配ID且api_version为1的plugin.json")
        entry = str(data.get("entry", "main.py"))
        if entry not in files or not entry.endswith(".py"):
            raise ValueError("缺少Python入口")
        for name, content in files.items():
            if name.endswith(".py"):
                compile(content, name, "exec")
        directory.mkdir(mode=0o700, parents=True)
        try:
            # Manifest is published last, so a concurrent startup never imports a partial plugin.
            for name in [*(name for name in files if name != "plugin.json"), "plugin.json"]:
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(files[name], encoding="utf-8")
                path.chmod(0o600)
        except BaseException:
            import shutil

            shutil.rmtree(directory)
            raise
        if self._started:
            self._installed_since_start = True
            self.records.append(
                {
                    "id": identifier,
                    "name": str(data.get("name", identifier))[:120],
                    "version": str(data.get("version", ""))[:80],
                    "status": "installed",
                    "restart_required": True,
                }
            )
        return {
            "id": identifier,
            "path": str(directory),
            "status": "installed",
            "restart_required": True,
        }
