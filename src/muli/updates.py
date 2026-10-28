"""Small direct metadata checks and explicitly approved uv-tool updates."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

import httpx

from muli import __version__
from muli.config import ConfigFile
from muli.events import EventBus
from muli.proactive import FrozenRouting
from muli.process import run_process
from muli.providers.http import read_response
from muli.reply_origin import ReplyOrigin
from muli.store import Store, now
from muli.update_evidence import release_version as release_version
from muli.update_evidence import update_evidence


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


class UpdateCapability(TypedDict):
    mode: Literal["in_place"]
    format_eligible: bool
    code: Literal[
        "installation_check_required", "verified_candidate_required", "format_verification_failed"
    ]


class Updates:
    def __init__(self, config: ConfigFile, store: Store, bus: EventBus) -> None:
        self.config, self.store, self.bus = config, store, bus

    def capability(self) -> UpdateCapability:
        """Format eligibility only; this neither inspects uv nor authorizes effects."""
        try:
            self.store.revalidate_format()
        except (ValueError, sqlite3.Error):
            return {
                "mode": "in_place",
                "format_eligible": False,
                "code": "format_verification_failed",
            }
        marked = self.store.format_state is not None
        return {
            "mode": "in_place",
            "format_eligible": not marked,
            "code": "verified_candidate_required" if marked else "installation_check_required",
        }

    def require_in_place_update(self) -> None:
        capability = self.capability()
        if not capability["format_eligible"]:
            raise ValueError(capability["code"])

    def candidate_configured(self) -> bool:
        """A notice hint only; actual boot, materials and policy need preview."""
        from muli.candidate_policy import CandidateError
        from muli.candidate_policy_store import CandidatePolicyStore

        try:
            return CandidatePolicyStore(self.store).read().enabled
        except CandidateError:
            return False

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
                eligible = self.capability()["format_eligible"]
                notice = {
                    "id": identifier,
                    "title": "沐璃有新版本",
                    "content": f"{item['latest']} 已发布。"
                    + (
                        "可以让沐璃检查安装条件并在你同意后更新。"
                        if eligible
                        else "已配置本地候选来源，可以让沐璃先预检，再经你同意后准备和切换。"
                        if self.candidate_configured()
                        else "当前资料库需要托管候选更新，先配置来源并核验候选格式。"
                    ),
                    "source": "update",
                    "update_evidence": update_evidence(item),
                    "read": False,
                    "created_at": now(),
                    "producer": "startup_update_check" if independent else "update_check",
                    "routing": (
                        FrozenRouting.independent(self.config.settings, self.store.db)
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
        # Approval is not evidence that the target preserves the admitted format.
        # Recheck here as well as at planning, before discovering or invoking uv.
        self.require_in_place_update()
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
        self.require_in_place_update()
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
