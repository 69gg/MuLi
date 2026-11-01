"""User-owned Python extensions outside the installed distribution.

Plugins run as trusted local code, not in a security sandbox. All registered tools
still pass through the runtime permission gate when requested by a model. A
successful reload swaps one generation at a time; a failed load leaves the old
generation serving requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from muli import prompt_layers
from muli.config import PluginSettings
from muli.providers import Message
from muli.runtime import ClientFactory, ContextHook, Runtime, ToolProvider
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
    routes: list[tuple[str, Callable[..., Any], list[str]]] = field(default_factory=list)

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
        if any(item[0] == path for item in self.routes):
            raise ValueError("插件路由路径重复")
        self.routes.append((path, endpoint, list(methods)))


@dataclass(eq=False)
class PluginGeneration:
    identifier: str
    generation: int
    context: PluginContext
    module_name: str
    tools: list[Tool]
    routes: list[Any] = field(default_factory=list)
    model_factory: ClientFactory | None = None
    active_calls: int = 0
    state: str = "active"
    fingerprints: tuple[Any, ...] = ()


class PluginManager:
    def __init__(self, runtime: Runtime, app: FastAPI, directory: Path) -> None:
        self.runtime, self.app, self.directory = runtime, app, directory
        self.records: list[dict[str, Any]] = []
        self._generations: dict[str, PluginGeneration] = {}
        self._generation_counts: dict[str, int] = {}
        self._draining: set[PluginGeneration] = set()
        self._tools: dict[str, Tool] = {}
        # 绑定方法每次访问都是新对象，运行时用 `is` 识别工具归属，这里固定同一个可调用对象。
        self._tool_provider: ToolProvider = lambda _runtime: list(self._tools.values())
        self.context_hook = self.context
        self._shadowed: dict[str, tuple[Tool, Any, bool]] = {}
        self._base_factory = runtime.client_factory
        self._started = False
        self._closed = False
        self._restart_required = False
        self._loaded_settings = runtime.config.settings.plugins.model_dump()
        self._reload_lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self._drain_tasks: set[asyncio.Task[Any]] = set()
        self._fingerprints: dict[str, tuple[Any, ...]] = {}
        self._digest_cache: dict[str, tuple[int, int, str]] = {}
        self._failures: dict[str, int] = {}
        self._next_attempt: dict[str, float] = {}

    @property
    def contexts(self) -> list[PluginContext]:
        return [generation.context for generation in self._generations.values()]

    @property
    def restart_required(self) -> bool:
        return self._restart_required

    def inventory(self) -> list[dict[str, Any]]:
        return list(self.records)

    def _record(self, identifier: str) -> dict[str, Any]:
        for record in self.records:
            if record.get("id") == identifier:
                return record
        record = {"id": identifier, "status": "failed", "error": ""}
        self.records.append(record)
        return record

    async def load(self) -> None:
        if self._started:
            return
        self._started = True
        settings = self.runtime.config.settings.plugins
        self._loaded_settings = settings.model_dump()
        self.runtime.add_tool_provider(self._tool_provider, replace=True)
        if self.context_hook not in self.runtime.context_hooks:
            self.runtime.context_hooks.append(self.context_hook)
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
            try:
                if not directory.is_dir() or directory.is_symlink():
                    continue
                if not (directory / "plugin.json").is_file():
                    continue
            except OSError:
                self._record(directory.name).update(
                    status="failed",
                    error="plugin_entry_unavailable",
                    error_type="OSError",
                )
                continue
            await self._activate(directory.name, settings)
        if settings.hot_reload:
            self._watch_task = asyncio.create_task(self._watch(), name="muli-plugin-watch")

    async def _activate(self, identifier: str, settings: PluginSettings) -> None:
        record = self._record(identifier)
        self._fingerprints[identifier] = self._fingerprints_for(self.directory / identifier)
        try:
            data = _manifest(self.directory / identifier, settings.max_source_bytes)
            if identifier in settings.disabled or data.get("enabled", True) is False:
                record.update(
                    name=str(data.get("name", identifier))[:120],
                    version=str(data.get("version", ""))[:80],
                    status="disabled",
                    error="",
                )
                return
            generation = await self._load_generation(
                identifier, settings, self._next_generation(identifier)
            )
            if generation is None:
                return
            try:
                self._install_routes(generation)
            except BaseException:
                await self._retire(generation)
                raise
            self._generations[identifier] = generation
            self._fingerprints[identifier] = generation.fingerprints
            self._refresh_tools()
            self._refresh_factory()
            record.update(
                name=str(data.get("name", identifier))[:120],
                version=str(data.get("version", ""))[:80],
                status="loaded",
                error="",
                tools=[tool.name for tool in generation.tools],
                generation=generation.generation,
            )
        except OSError:
            record.update(
                status="failed",
                error="plugin_entry_unavailable",
                error_type="OSError",
            )
        except Exception as exc:  # user extension failure must not prevent core startup
            record.update(
                status="failed",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                error_type=type(exc).__name__,
            )
            if "API版本" in str(exc):
                self._restart_required = True

    def _next_generation(self, identifier: str) -> int:
        # 计数器跨插件移除保留，旧代次退役时不会复用同名模块并误删新代次。
        current = self._generations.get(identifier)
        previous = max(
            self._generation_counts.get(identifier, 0),
            current.generation if current is not None else 0,
        )
        generation = previous + 1
        self._generation_counts[identifier] = generation
        return generation

    async def _load_generation(
        self, identifier: str, settings: PluginSettings, generation: int
    ) -> PluginGeneration | None:
        directory = self.directory / identifier
        data = _manifest(directory, settings.max_source_bytes)
        if identifier in settings.disabled or data.get("enabled", True) is False:
            return None
        entry = _entry(directory, data, settings.max_source_bytes)
        fingerprints = self._fingerprints_for(directory)
        module_name = f"_muli_plugin_{identifier}_g{generation}"
        spec = importlib.util.spec_from_file_location(
            module_name, entry, submodule_search_locations=[str(directory)]
        )
        if spec is None or spec.loader is None:
            raise ValueError("无法加载插件入口")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        context = PluginContext(identifier, directory, self.runtime, self.app)
        try:
            # 始终编译当前源码：.pyc 只校验秒级 mtime 与文件大小，同秒等长改写会命中旧字节码。
            source = entry.read_bytes()
            exec(compile(source, str(entry), "exec"), module.__dict__)
            async with asyncio.timeout(settings.hook_timeout):
                result = module.setup(context)
                if inspect.isawaitable(result):
                    await result
            other_tools = {
                tool.name
                for generation in self._generations.values()
                if generation.identifier != identifier
                for tool in generation.tools
            }
            collisions = other_tools.intersection(tool.name for tool in context.tools)
            if collisions:
                raise ValueError("多个插件覆盖相同工具：" + ",".join(sorted(collisions)))
            for hook in context.startup_hooks:
                await hook()
        except BaseException:
            for hook in reversed(context.shutdown_hooks):
                try:
                    async with asyncio.timeout(settings.hook_timeout):
                        await hook()
                except Exception:
                    continue
            self._drop_modules(module_name)
            raise
        result = PluginGeneration(
            identifier=identifier,
            generation=generation,
            context=context,
            module_name=module_name,
            tools=list(context.tools),
            model_factory=context.model_factory,
            fingerprints=fingerprints,
        )
        result.tools = [self._wrap_tool(result, tool) for tool in context.tools]
        # 目录和能力清单按对象身份核对；对外暴露的必须是实际注册的包装对象。
        context.tools = list(result.tools)
        return result

    def _wrap_tool(self, generation: PluginGeneration, tool: Tool) -> Tool:
        original = tool.handler

        async def handler(arguments: dict[str, Any]) -> Any:
            if generation.state == "retired":
                raise RuntimeError("插件代次已退役")
            generation.active_calls += 1
            try:
                return await original(arguments)
            finally:
                generation.active_calls -= 1
                self._retire_if_drained(generation)

        return replace(tool, handler=handler)

    @staticmethod
    def _drop_modules(module_name: str) -> None:
        for name in tuple(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                sys.modules.pop(name, None)

    def _install_routes(self, generation: PluginGeneration) -> None:
        try:
            for path, endpoint, methods in generation.context.routes:
                before = len(self.app.router.routes)
                self.app.add_api_route(
                    f"/api/plugins/{generation.identifier}{path}",
                    endpoint,
                    methods=methods,
                )
                if len(self.app.router.routes) > before:
                    generation.routes.append(self.app.router.routes[-1])
        except BaseException:
            self._remove_routes(generation)
            raise

    @staticmethod
    def _remove_routes(generation: PluginGeneration) -> None:
        for route in generation.routes:
            try:
                generation.context.app.router.routes.remove(route)
            except ValueError:
                continue
        generation.routes.clear()

    def _refresh_tools(self) -> None:
        desired = {
            tool.name: tool
            for generation in self._generations.values()
            for tool in generation.tools
        }
        for name in list(self.runtime.tools):
            if (
                self.runtime._tool_factories.get(name) is self._tool_provider
                and name not in desired
            ):
                shadowed = self._shadowed.pop(name, None)
                if shadowed is not None:
                    tool, owner, coordinator = shadowed
                    self.runtime.tools[name] = tool
                    if owner is None:
                        self.runtime._tool_factories.pop(name, None)
                    else:
                        self.runtime._tool_factories[name] = owner
                    if coordinator:
                        self.runtime._coordinator_tools.add(name)
                    else:
                        self.runtime._coordinator_tools.discard(name)
                else:
                    del self.runtime.tools[name]
                    self.runtime._tool_factories.pop(name, None)
                    self.runtime._coordinator_tools.discard(name)
        for name, tool in desired.items():
            if (
                name in self.runtime.tools
                and self.runtime._tool_factories.get(name) is not self._tool_provider
                and name not in self._shadowed
            ):
                self._shadowed[name] = (
                    self.runtime.tools[name],
                    self.runtime._tool_factories.get(name),
                    name in self.runtime._coordinator_tools,
                )
            self.runtime.tools[name] = tool
            self.runtime._tool_factories[name] = self._tool_provider
            self.runtime._coordinator_tools.add(name)
        self._tools = desired

    def _refresh_factory(self) -> None:
        for generation in reversed(list(self._generations.values())):
            if generation.model_factory is not None:
                self.runtime.client_factory = generation.model_factory
                return
        self.runtime.client_factory = self._base_factory

    async def reload(self, identifier: str) -> dict[str, Any]:
        if not self._started or self._closed or not PLUGIN_ID.fullmatch(identifier):
            return self._record(identifier)
        settings = self.runtime.config.settings.plugins
        async with self._reload_lock:
            record = self._record(identifier)
            old = self._generations.get(identifier)
            directory = self.directory / identifier
            if not settings.enabled or identifier in settings.disabled:
                if old is not None:
                    await self._retire(old)
                record.update(status="disabled", error="", reload_pending=False)
                return record
            if not directory.is_dir() or not (directory / "plugin.json").is_file():
                if old is not None:
                    await self._retire(old)
                record.update(status="removed", error="", reload_pending=False)
                return record
            try:
                # Read the manifest before switching generations; a later
                # failure must not leave the old generation already replaced.
                data = _manifest(directory, settings.max_source_bytes)
                generation = await self._load_generation(
                    identifier, settings, self._next_generation(identifier)
                )
                if generation is None:
                    if old is not None:
                        await self._retire(old)
                    record.update(status="disabled", error="", reload_pending=False)
                    return record
                if old is not None:
                    self._remove_routes(old)
                try:
                    self._install_routes(generation)
                except BaseException:
                    await self._retire(generation)
                    raise
            except Exception as exc:
                if old is not None and not old.routes:
                    try:
                        self._install_routes(old)
                    except Exception:
                        pass
                self._failures[identifier] = self._failures.get(identifier, 0) + 1
                delay = min(60.0, 2.0 ** self._failures[identifier])
                self._next_attempt[identifier] = time.monotonic() + delay
                record.update(
                    status="degraded" if old is not None else "failed",
                    error=f"{type(exc).__name__}: {str(exc)[:300]}",
                    error_type=type(exc).__name__,
                    reload_pending=False,
                )
                if "API版本" in str(exc):
                    self._restart_required = True
                return record
            self._generations[identifier] = generation
            self._fingerprints[identifier] = generation.fingerprints
            self._refresh_tools()
            self._refresh_factory()
            record.update(
                name=str(data.get("name", identifier))[:120],
                version=str(data.get("version", ""))[:80],
                status="loaded",
                error="",
                tools=[tool.name for tool in generation.tools],
                generation=generation.generation,
                reload_pending=False,
            )
            self._failures.pop(identifier, None)
            self._next_attempt.pop(identifier, None)
            if old is not None:
                old.state = "draining"
                self._draining.add(old)
                self._schedule_drain(old)
            return record

    def _retire_if_drained(self, generation: PluginGeneration) -> None:
        if generation.state == "draining" and generation.active_calls == 0:
            self._schedule_retire(generation)

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._drain_tasks.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self._drain_tasks.discard(completed)
            if not completed.cancelled():
                # Retrieve background failures so they cannot become unhandled.
                completed.exception()

        task.add_done_callback(finished)

    def _schedule_retire(self, generation: PluginGeneration) -> None:
        self._track_task(asyncio.create_task(self._retire(generation), name="muli-plugin-retire"))

    def _schedule_drain(self, generation: PluginGeneration) -> None:
        self._track_task(asyncio.create_task(self._drain(generation), name="muli-plugin-drain"))

    async def _drain(self, generation: PluginGeneration) -> None:
        deadline = time.monotonic() + self.runtime.config.settings.plugins.drain_timeout
        while generation.active_calls and time.monotonic() < deadline and not self._closed:
            await asyncio.sleep(0.05)
        await self._retire(generation)

    async def _retire(self, generation: PluginGeneration) -> None:
        if generation.state == "retired":
            return
        generation.state = "retired"
        settings = self.runtime.config.settings.plugins
        for hook in reversed(generation.context.shutdown_hooks):
            try:
                async with asyncio.timeout(settings.hook_timeout):
                    await hook()
            except Exception:
                continue
        self._remove_routes(generation)
        # 只清理本代次模块，不能按插件前缀删除可能已复用的新代次模块。
        self._drop_modules(generation.module_name)
        self._draining.discard(generation)
        if self._generations.get(generation.identifier) is generation:
            self._generations.pop(generation.identifier, None)
            self._refresh_tools()
            self._refresh_factory()

    async def context(self, session_id: str, messages: list[Message]) -> list[Message]:
        for generation in list(self._generations.values()):
            if generation.state == "retired":
                continue
            context = generation.context
            record = self._record(generation.identifier)
            generation.active_calls += 1
            try:
                if context.prompt:
                    block = prompt_layers.plugin_prompt_block(context.prompt)
                    if messages and messages[0].role in {"system", "developer"}:
                        messages = [
                            replace(
                                messages[0],
                                content=messages[0].content + "\n\n" + block,
                            ),
                            *messages[1:],
                        ]
                    else:
                        messages = [Message(role="system", content=block), *messages]
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
            finally:
                generation.active_calls -= 1
                self._retire_if_drained(generation)
            if record.get("error") == "plugin_context_failed":
                record.update(status="loaded", error="")
                record.pop("phase", None)
                record.pop("error_type", None)
        return messages

    def _fingerprints_for(self, directory: Path) -> tuple[Any, ...]:
        settings = self.runtime.config.settings.plugins
        files: list[tuple[str, int, int, str]] = []
        try:
            candidates = [directory / "plugin.json", *directory.rglob("*.py")]
            for path in sorted(candidates):
                if any(
                    part in {"__pycache__", ".venv", "venv", "node_modules", ".git"}
                    for part in path.relative_to(directory).parts
                ):
                    continue
                if path.is_symlink() or not path.is_file() or path.suffix == ".pyc":
                    continue
                metadata = path.stat()
                key = str(path)
                cached = self._digest_cache.get(key)
                if (
                    cached is not None
                    and cached[0] == metadata.st_size
                    and cached[1] == metadata.st_mtime_ns
                ):
                    length = min(metadata.st_size, settings.max_source_bytes + 1)
                    digest = cached[2]
                else:
                    with path.open("rb") as source:
                        data = source.read(settings.max_source_bytes + 1)
                    length = len(data)
                    digest = hashlib.sha256(data[: settings.max_source_bytes]).hexdigest()
                    self._digest_cache[key] = (metadata.st_size, metadata.st_mtime_ns, digest)
                files.append(
                    (
                        path.relative_to(directory).as_posix(),
                        length,
                        metadata.st_mtime_ns,
                        digest,
                    )
                )
        except OSError:
            return ()
        return tuple(files)

    def _scan_fingerprints(self) -> dict[str, tuple[Any, ...]]:
        result: dict[str, tuple[Any, ...]] = {}
        try:
            directories = sorted(self.directory.iterdir())
        except OSError:
            return result
        for directory in directories:
            try:
                if not directory.is_dir() or directory.is_symlink():
                    continue
                if not (directory / "plugin.json").is_file():
                    continue
            except OSError:
                continue
            result[directory.name] = self._fingerprints_for(directory)
        return result

    async def _watch(self) -> None:
        while not self._closed:
            try:
                settings = self.runtime.config.settings.plugins
                await asyncio.sleep(settings.watch_interval_seconds)
                current = await asyncio.to_thread(self._scan_fingerprints)
                changed = [
                    identifier
                    for identifier, value in current.items()
                    if self._fingerprints.get(identifier) != value
                ]
                removed = [
                    identifier for identifier in self._fingerprints if identifier not in current
                ]
                if changed or removed:
                    await asyncio.sleep(settings.debounce_seconds)
                    current = await asyncio.to_thread(self._scan_fingerprints)
                    for identifier in [*changed, *removed]:
                        if identifier in current and current[identifier] != self._fingerprints.get(
                            identifier
                        ):
                            reloaded = await self._reload_watched(identifier)
                        elif identifier not in current:
                            reloaded = await self._reload_watched(identifier)
                        else:
                            reloaded = False
                        if reloaded:
                            # 实际重载后使用新代次保存的基线；跳过退避或失败时保留旧基线。
                            generation = self._generations.get(identifier)
                            if generation is not None:
                                self._fingerprints[identifier] = generation.fingerprints
                            elif identifier in current:
                                self._fingerprints[identifier] = current[identifier]
                            else:
                                self._fingerprints.pop(identifier, None)
                    for identifier, value in current.items():
                        if identifier not in changed and identifier not in removed:
                            self._fingerprints[identifier] = value
                else:
                    self._fingerprints = current
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._record("__watch__").update(
                    status="degraded",
                    error=f"{type(error).__name__}: {str(error)[:300]}",
                    error_type=type(error).__name__,
                )

    async def _reload_watched(self, identifier: str) -> bool:
        if time.monotonic() < self._next_attempt.get(identifier, 0.0):
            return False
        record = await self.reload(identifier)
        return record.get("status") in {"loaded", "disabled", "removed"}

    async def reconfigure(self) -> None:
        if not self._started or self._closed:
            return
        settings = self.runtime.config.settings.plugins
        current = settings.model_dump()
        changed = current != self._loaded_settings
        self._loaded_settings = current
        if changed and not settings.hot_reload:
            # 未开启热重载时，插件启停或源码变化只登记待重启，不在运行中替换。
            if self._watch_task is not None:
                self._watch_task.cancel()
                await asyncio.gather(self._watch_task, return_exceptions=True)
                self._watch_task = None
            self._restart_required = True
            return
        identifiers: set[str] = set(self._generations)
        try:
            identifiers.update(
                entry.name
                for entry in self.directory.iterdir()
                if entry.is_dir() and (entry / "plugin.json").is_file()
            )
        except OSError:
            pass
        for identifier in sorted(identifiers):
            await self.reload(identifier)
        if settings.hot_reload and self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch(), name="muli-plugin-watch")
        elif not settings.hot_reload and self._watch_task is not None:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
            self._watch_task = None

    async def close(self) -> None:
        self._closed = True
        if self._watch_task is not None:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
            self._watch_task = None
        for task in tuple(self._drain_tasks):
            task.cancel()
        await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        self._drain_tasks.clear()
        for generation in reversed(list(self._generations.values())):
            await self._retire(generation)
        # A reload can leave an old generation draining after the current one
        # has been removed; close must still run its shutdown hooks.
        for generation in tuple(self._draining):
            await self._retire(generation)
        self._draining.clear()
        self.runtime.client_factory = self._base_factory

    def install(self, identifier: str, files: dict[str, str]) -> dict[str, Any]:
        if not PLUGIN_ID.fullmatch(identifier) or not files or len(files) > 30:
            raise ValueError("插件ID或文件数量无效")
        directory = self.directory / identifier
        if directory.exists():
            raise FileExistsError("插件已存在；修改请使用文件工具或重新加载")
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
        temporary: list[Path] = []
        try:
            # Publish each file atomically; manifest last so no partial plugin is imported.
            for name in [*(name for name in files if name != "plugin.json"), "plugin.json"]:
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                pending = path.with_name(path.name + ".tmp")
                pending.write_text(files[name], encoding="utf-8")
                pending.chmod(0o600)
                temporary.append(pending)
                os.replace(pending, path)
            temporary.clear()
        except BaseException:
            import shutil

            for pending in temporary:
                pending.unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
            raise
        settings = self.runtime.config.settings.plugins
        restart = not settings.hot_reload
        if self._started:
            record_entry = {
                "name": str(data.get("name", identifier))[:120],
                "version": str(data.get("version", ""))[:80],
                "status": "installed",
                "restart_required": restart,
            }
            if settings.hot_reload:
                record_entry["reload_pending"] = True
            record = self._record(identifier)
            record.pop("error", None)
            record.update(record_entry)
            if settings.hot_reload:
                try:
                    task = asyncio.get_running_loop().create_task(
                        self.reload(identifier), name="muli-plugin-reload"
                    )
                except RuntimeError:
                    self._restart_required = True
                else:
                    self._track_task(task)
            else:
                self._restart_required = True
        return {
            "id": identifier,
            "path": str(directory),
            "status": "installed",
            "restart_required": restart,
            "reload_pending": settings.hot_reload,
        }
