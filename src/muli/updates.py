"""Small direct metadata checks and explicitly approved uv-tool updates."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

from muli import __version__
from muli.config import ConfigFile
from muli.events import EventBus
from muli.proactive import FrozenRouting
from muli.process import run_process
from muli.providers.http import read_response
from muli.reply_origin import ReplyOrigin
from muli.store import Store, now


def direct_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.lower()
        not in {
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "ftp_proxy",
        }
    }


def release_version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value) or len(value) > 40:
        raise ValueError("只支持正式发布版本号")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


class Updates:
    def __init__(self, config: ConfigFile, store: Store, bus: EventBus) -> None:
        self.config, self.store, self.bus = config, store, bus

    async def check(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        independent: bool = False,
        reply_origin: ReplyOrigin | None = None,
    ) -> dict[str, Any]:
        if independent and reply_origin is not None:
            raise ValueError("更新检查不能同时绑定请求与独立来源")
        settings = self.config.settings.updates
        owned = client is None
        client = client or httpx.AsyncClient(
            timeout=settings.timeout, trust_env=False, follow_redirects=False
        )
        item: dict[str, Any] = {
            "id": "latest",
            "current": __version__,
            "checked_at": now(),
            "available": False,
        }
        try:
            async with client.stream(
                "GET", settings.metadata_url, timeout=settings.timeout
            ) as response:
                response.raise_for_status()
                data = json.loads(await read_response(response, settings.max_metadata_bytes))
            info = data["info"]
            if info["name"].lower().replace("_", "-") != "muli-agent":
                raise ValueError("更新元数据不属于muli-agent")
            latest = str(info["version"])
            item.update(
                latest=latest,
                available=release_version(latest) > release_version(__version__),
                status="checked",
            )
        except Exception as exc:
            item.update(status="unavailable", error_type=type(exc).__name__)
        finally:
            if owned:
                await client.aclose()
        if item["available"]:
            identifier = "update-" + item["latest"]
            if self.store.get("notification", identifier) is None:
                notice = {
                    "id": identifier,
                    "title": "沐璃有新版本",
                    "content": f"{item['latest']} 已发布，可以让沐璃帮你更新。更新会先征得你的同意。",
                    "source": "update",
                    "read": False,
                    "created_at": now(),
                    "producer": "startup_update_check" if independent else "update_check",
                    "routing": (
                        FrozenRouting.independent(self.config.settings)
                        if independent
                        else FrozenRouting.bound(reply_origin or ReplyOrigin())
                    ).dump(),
                }
                self.store.event(
                    "notification.created",
                    notice,
                    objects=[("update", item), ("notification", notice)],
                )
                await self.bus.notify()
                return item
        self.store.put("update", item)
        return item

    async def apply(self, version: str) -> dict[str, Any]:
        release_version(version)
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError("自动更新需要uv tool安装；当前未找到uv")
        environment = direct_environment()
        directory = await run_process([uv, "tool", "dir"], timeout=10, env=environment)
        if directory["exit_code"]:
            raise ValueError("无法确定uv工具目录")
        root = Path(str(directory["output"]).strip()).resolve()
        prefix = Path(sys.prefix).resolve()
        if not prefix.is_relative_to(root) or prefix.name != "muli-agent":
            raise ValueError(
                "当前不是uv tool安装，已拒绝修改开发环境；请用uv tool install muli-agent"
            )
        result = await run_process(
            [uv, "tool", "upgrade", "--no-python-downloads", f"muli-agent=={version}"],
            timeout=120,
            limit=12000,
            env=environment,
        )
        if result["exit_code"]:
            raise ValueError(
                "uv更新失败，未安排重启；请检查安装环境及原有版本约束，未改写工具安装要求"
            )
        source = self.config.path.resolve().parent / "source"
        source_status = "absent"
        if (source / ".git").exists():
            # A local edit is never reset or stashed on the user's behalf.
            status = await run_process(
                ["git", "status", "--porcelain"], cwd=source, timeout=10, env=environment
            )
            if status["exit_code"] == 0 and not status["output"].strip():
                pulled = await run_process(
                    ["git", "-c", "http.proxy=", "pull", "--ff-only", "--depth", "1"],
                    cwd=source,
                    timeout=45,
                    limit=4000,
                    env=environment,
                )
                source_status = "updated" if pulled["exit_code"] == 0 else "update_failed"
            else:
                source_status = "local_changes_preserved"
        return {"installed": version, "source": source_status, "restart_required": True}
