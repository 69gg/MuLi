"""Validated self-management plans, applied only after foreground work is idle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from muli.config import Settings
from muli.extensions import ExtensionError
from muli.plugins import PluginManager
from muli.providers import Message
from muli.runtime import Runtime
from muli.service import UserService
from muli.store import new_id, now
from muli.tools import Tool, parameters, string
from muli.updates import Updates

ApplySettings = Callable[[Settings], Awaitable[None]]


def fingerprint(settings: Settings) -> str:
    return hashlib.sha256(json.dumps(settings.model_dump(), sort_keys=True).encode()).hexdigest()


class SelfManagement:
    def __init__(
        self,
        runtime: Runtime,
        plugins: PluginManager,
        service: UserService,
        apply_settings: ApplySettings,
    ) -> None:
        self.runtime, self.plugins, self.service, self.apply_settings = (
            runtime,
            plugins,
            service,
            apply_settings,
        )
        self.directory = runtime.data_dir / "management"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.updates = Updates(runtime.config, runtime.store, runtime.bus)
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._check: asyncio.Task[dict[str, Any]] | None = None
        runtime.add_tool_provider(self.tools)
        runtime.context_hooks.append(self.context)

    def plans(self) -> list[dict[str, Any]]:
        return self.runtime.store.list("management", 100)

    async def context(self, session_id: str, messages: list[Message]) -> list[Message]:
        update = self.runtime.store.get("update", "latest")
        if update and update.get("available"):
            messages.insert(
                1,
                Message(
                    role="system",
                    content=f"版本检查资料：当前{update['current']}，可更新到{update['latest']}。在自然时机提醒用户；self_update始终需要真实用户批准。",
                ),
            )
        return messages

    async def stage(
        self, action: str, changes: dict[str, Any] | None = None, version: str = ""
    ) -> dict[str, Any]:
        if action not in {"configure", "restart", "update"}:
            raise ValueError("未知管理操作")
        if any(item["status"] in {"pending", "applying"} for item in self.plans()):
            raise ValueError("已有待应用管理操作，请等待完成或取消后再提交")
        settings = self.runtime.config.settings
        target = (
            self.runtime.config.validate_changes(changes or {})
            if action == "configure"
            else Settings.model_validate(settings.model_dump())
        )
        identifier = new_id()
        # Secrets remain in a private configuration snapshot, never in the public journal.
        path = self.directory / f"{identifier}.json"
        with path.open("x", encoding="utf-8") as handle:
            path.chmod(0o600)
            json.dump(target.model_dump(), handle, ensure_ascii=False)
        item = {
            "id": identifier,
            "action": action,
            "status": "pending",
            "created_at": now(),
            "base": fingerprint(settings),
            "target": fingerprint(target),
            "version": version,
            "changed_sections": sorted(changes or {}),
        }
        self.runtime.store.put("management", item)
        self._wake.set()
        await self.runtime.bus.emit("management.queued", item)
        return item

    async def cancel(self, identifier: str) -> dict[str, Any]:
        item = self.runtime.store.get("management", identifier)
        if item is None or item["status"] != "pending":
            raise ValueError("管理操作不存在或已开始应用")
        item["status"] = "cancelled"
        self.runtime.store.put("management", item)
        (self.directory / f"{identifier}.json").unlink(missing_ok=True)
        await self.runtime.bus.emit("management.completed", item)
        return item

    async def start(self) -> None:
        if self._worker is not None:
            return
        for item in self.plans():
            if item["status"] == "applying":
                current = fingerprint(self.runtime.config.settings)
                item["status"] = (
                    "completed"
                    if item["action"] == "configure" and current == item["target"]
                    else "interrupted"
                )
                self.runtime.store.put("management", item)
        self._worker = asyncio.create_task(self._run())
        self._wake.set()
        if self.runtime.config.settings.updates.check_on_start:
            self._check = asyncio.create_task(self.updates.check())

    async def _idle(self) -> None:
        while self.runtime.busy:
            revision = self.runtime.bus.revision
            tasks = list(self.runtime.tasks.values())
            if tasks:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in tasks), return_exceptions=True
                )
                await asyncio.sleep(0)
            elif self.runtime.busy:

                def changed(expected: int = revision) -> bool:
                    return self.runtime.bus.revision != expected

                async with self.runtime.bus.changed:
                    await self.runtime.bus.changed.wait_for(changed)

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            for item in reversed(self.plans()):
                if item["status"] != "pending":
                    continue
                await self._idle()
                async with self.runtime.configuration_lock:
                    if self.runtime.busy:
                        self._wake.set()
                        continue
                    current = self.runtime.store.get("management", item["id"])
                    if current is None or current["status"] != "pending":
                        continue
                    await self._apply(current)

    async def _apply(self, item: dict[str, Any]) -> None:
        path = self.directory / f"{item['id']}.json"
        try:
            if fingerprint(self.runtime.config.settings) != item["base"]:
                raise ValueError("配置已经被其它入口修改，请重新生成计划")
            target = Settings.model_validate_json(path.read_text(encoding="utf-8"))
            if fingerprint(target) != item["target"]:
                raise ValueError("待应用配置内容已改变")
            item["status"] = "applying"
            self.runtime.store.put("management", item)
            if item["action"] == "configure":
                await self.apply_settings(target)
            elif item["action"] == "restart":
                await self.service.action("restart")
            else:
                item["result"] = await self.updates.apply(item["version"])
                await self.service.action("restart")
            item["status"] = "completed"
        except Exception as exc:
            if (
                item["action"] == "configure"
                and isinstance(exc, ExtensionError)
                and exc.code == "runtime_busy"
            ):
                item["status"] = "pending"
                self._wake.set()
            else:
                item.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        finally:
            if item["status"] not in {"pending", "applying"}:
                path.unlink(missing_ok=True)
            self.runtime.store.put("management", item)
            await self.runtime.bus.emit(
                "management.queued" if item["status"] == "pending" else "management.completed", item
            )

    async def close(self) -> None:
        tasks = [task for task in (self._worker, self._check) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = self._check = None

    def tools(self, runtime: Runtime) -> list[Tool]:
        async def configuration(args: dict[str, Any]) -> Any:
            return runtime.config.public()

        async def configure(args: dict[str, Any]) -> Any:
            return await self.stage("configure", args["changes"])

        async def status(args: dict[str, Any]) -> Any:
            return {
                "plans": self.plans(),
                "plugins": self.plugins.inventory(),
                "plugins_restart_required": self.plugins.restart_required,
                "update": runtime.store.get("update", "latest"),
                "extensions": runtime.extensions.status(),
            }

        async def restart(args: dict[str, Any]) -> Any:
            return await self.stage("restart")

        async def update(args: dict[str, Any]) -> Any:
            from muli.updates import release_version

            release_version(str(args["version"]))
            return await self.stage("update", version=str(args["version"]))

        async def install(args: dict[str, Any]) -> Any:
            return self.plugins.install(str(args["id"]), args["files"])

        return [
            Tool(
                "self_config",
                "查看自身全部配置（密钥隐藏）；配置字段可通过管理API schema查看",
                parameters({}),
                configuration,
            ),
            Tool(
                "self_configure",
                "校验配置补丁并排队，所有当前工作结束后应用；无需结束主身份",
                parameters({"changes": {"type": "object"}}, ("changes",)),
                configure,
                "write",
            ),
            Tool("self_status", "查看自身管理计划、插件和版本检查结果", parameters({}), status),
            Tool(
                "self_restart",
                "校验现有配置后排队重启已安装的常驻服务",
                parameters({}),
                restart,
                "write",
            ),
            Tool(
                "self_update",
                "更新到指定正式版本并重启；无论当前模式都需真实用户批准",
                parameters({"version": string()}, ("version",)),
                update,
                "external",
                requires_approval=True,
            ),
            Tool(
                "plugin_install",
                "安装用户目录中的Python插件；不会修改已安装源码，重启后加载",
                parameters(
                    {
                        "id": string(),
                        "files": {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                    ("id", "files"),
                ),
                install,
                "write",
            ),
        ]
