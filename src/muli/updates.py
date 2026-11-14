"""Small direct metadata checks and explicitly approved uv-tool updates."""

from __future__ import annotations

import json
import os
import shutil
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
    code: Literal["installation_check_required"]


class Updates:
    def __init__(
        self,
        config: ConfigFile,
        store: Store,
        bus: EventBus,
    ) -> None:
        self.config, self.store, self.bus = config, store, bus

    def capability(self) -> UpdateCapability:
        """The owner always upgrades in place; the database marker is not a gate."""
        return {
            "mode": "in_place",
            "format_eligible": True,
            "code": "installation_check_required",
        }

    def require_in_place_update(self) -> None:
        """Kept as the single update admission point; it no longer blocks."""
        return

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
                notice = {
                    "id": identifier,
                    "title": "沐璃有新版本",
                    "content": f"{item['latest']} 已发布。可以让沐璃直接升级并重启，升级前会请求你批准。",
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

    def _local_wheel(self, version: str) -> Path | None:
        roots: list[Path] = []
        configured = os.environ.get("MULI_WHEELHOUSE", "")
        if configured:
            roots.append(Path(configured))
        base = self.config.path.resolve().parent
        roots.extend((base / "wheelhouse", base / "wheels"))
        for root in roots:
            if not root.is_dir():
                continue
            for pattern in (f"muli_agent-{version}-*.whl", f"muli-agent-{version}-*.whl"):
                matches = sorted(root.glob(pattern))
                if len(matches) == 1:
                    return matches[0]
        return None

    async def _health(self, prefix: Path, version: str, environment: dict[str, str]) -> bool:
        executable = (
            prefix / "Scripts" / "muli.exe" if sys.platform == "win32" else prefix / "bin" / "muli"
        )
        if not executable.is_file():
            return False
        try:
            checked = await run_process(
                [str(executable), "--version"],
                timeout=30,
                limit=1000,
                env=environment,
                merge_stderr=False,
            )
            if checked["exit_code"] or str(checked["output"]).strip() != version:
                return False
            doctor = await run_process(
                [
                    str(executable),
                    "--config",
                    str(self.config.path.resolve()),
                    "doctor",
                ],
                timeout=60,
                limit=24000,
                env=environment,
                merge_stderr=False,
            )
        except (OSError, TimeoutError):
            return False
        if doctor["exit_code"]:
            return False
        try:
            payload = json.loads(str(doctor["output"]))
        except (ValueError, RecursionError):
            return False
        return isinstance(payload, dict) and payload.get("version") == version

    async def _rollback(
        self,
        uv: str,
        prefix: Path,
        old_version: str,
        environment: dict[str, str],
    ) -> str:
        if sys.platform in {"darwin", "win32"}:
            return "notification"
        wheel = self._local_wheel(old_version)
        if wheel is None:
            return "unavailable"
        restored = await run_process(
            [
                uv,
                "tool",
                "install",
                "--force",
                "--offline",
                "--no-python-downloads",
                "--find-links",
                str(wheel.parent),
                f"muli-agent=={old_version}",
            ],
            timeout=120,
            limit=12000,
            env=environment,
        )
        if restored["exit_code"]:
            return "failed"
        return "completed" if await self._health(prefix, old_version, environment) else "failed"

    async def apply(self, version: str) -> dict[str, Any]:
        release_version(version)
        # The owner can always upgrade in place. Keep the old admission helper
        # callable for callers that still gate on it, but it no longer blocks.
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
        old_version = __version__
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
        health = await self._health(prefix, version, environment)
        rollback = "not_needed"
        if not health:
            rollback = await self._rollback(uv, prefix, old_version, environment)
        source = self.config.path.resolve().parent / "source"
        source_status = "absent"
        if (source / ".git").exists():
            # A local edit is never reset or stashed on the user's behalf.
            status = await run_process(
                ["git", "status", "--porcelain"], cwd=source, timeout=10, env=environment
            )
            if status["exit_code"] == 0 and not status["output"].strip():
                # A shallow reference checkout cannot fast-forward across the
                # fetch boundary; fetch the tip and reset to it instead.
                fetched = await run_process(
                    ["git", "-c", "http.proxy=", "fetch", "--depth", "1", "origin", "main"],
                    cwd=source,
                    timeout=45,
                    limit=4000,
                    env=environment,
                )
                if fetched["exit_code"] == 0:
                    reset = await run_process(
                        ["git", "-c", "http.proxy=", "reset", "--hard", "FETCH_HEAD"],
                        cwd=source,
                        timeout=15,
                        limit=4000,
                        env=environment,
                    )
                    source_status = "updated" if reset["exit_code"] == 0 else "update_failed"
                else:
                    source_status = "update_failed"
            else:
                source_status = "local_changes_preserved"
        return {
            "installed": version,
            "previous": old_version,
            "source": source_status,
            "restart_required": health,
            "health": "passed" if health else "failed",
            "ab": False,
            "rollback": rollback,
        }
