"""Small interactive first-run setup; advanced setup happens with MuLi afterwards."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from muli.config import ConfigFile, Settings
from muli.service import UserService

SOURCE_REPOSITORY = "https://github.com/69gg/MuLi.git"


async def start_background(manager: UserService) -> dict[str, Any]:
    """Install and request login startup; report observed state without claiming readiness."""
    capability = manager.capability()
    if not capability["supported"]:
        return {"accepted": False, **capability, "completed": []}
    completed: list[str] = []
    try:
        for action in ("install", "enable", "start"):
            await manager.action(action)
            completed.append(action)
        return {"accepted": True, "completed": completed, "status": await manager.status()}
    except (OSError, ValueError, TimeoutError) as exc:
        return {"accepted": False, "completed": completed, "reason": str(exc)}


def prompt_settings(
    read: Callable[[str], str] = input,
    secret: Callable[[str], str] = getpass.getpass,
) -> dict[str, Any]:
    def ask(label: str, default: str = "") -> str:
        return read(f"{label}" + (f" [{default}]" if default else "") + "：").strip() or default

    def object_value(label: str) -> dict[str, Any]:
        value = json.loads(ask(label + "（JSON 对象，留空不发送）", "{}"))
        if not isinstance(value, dict):
            raise ValueError("模型参数需要 JSON 对象")
        return value

    user_name = ask("沐璃可以怎么称呼你？", "你")
    protocol = ask("模型协议：openai_chat / openai_responses / anthropic", "openai_chat")
    model = ask("主模型 ID")
    base_url = ask("API 基础地址（包含 /v1 等版本前缀）")
    if not model or not base_url:
        raise ValueError("请填写主模型 ID 和 API 基础地址；只建空配置可使用 --defaults")
    api_key = secret("API key（本地保存，留空表示无密钥）：").strip()
    vision = ask("主模型可以读图？y/n", "n").casefold() in {"y", "yes", "是"}
    effort = ask("reasoning effort（留空不发送）") or None
    thinking = object_value("thinking 参数，如 type / budget_tokens")
    reasoning = object_value("reasoning 参数，如 effort / summary")
    extra = object_value("其它供应商参数，如 enable_thinking / 自定义 budget")
    host = ask("服务监听地址", "127.0.0.1")
    port = int(ask("服务端口", "8765"))
    password = secret("工作台密码（至少8字符，本机回环可留空）：")
    if password and password != secret("再次输入工作台密码："):
        raise ValueError("两次密码不一致")
    return {
        "persona": {"user_name": user_name, "introduction": f"我是沐璃，{user_name}的私人助手。"},
        "models": {
            "main": {
                "protocol": protocol,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "vision": vision,
                "reasoning_effort": effort,
                "thinking": thinking,
                "reasoning": reasoning,
                "extra_body": extra,
            }
        },
        "server": {"host": host, "port": port, "password": password},
        "onboarding_pending": True,
    }


def initialize(
    path: Path,
    data_dir: Path,
    *,
    defaults: bool = False,
    read: Callable[[str], str] = input,
    secret: Callable[[str], str] = getpass.getpass,
) -> Settings:
    path = path.expanduser().resolve()
    if path.exists():
        raise ValueError("配置已存在，未覆盖；可以在工作台或对话中修改")
    changes = {"onboarding_pending": True} if defaults else prompt_settings(read, secret)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".init-", dir=path.parent) as directory:
        candidate = ConfigFile(Path(directory) / "config.json")
        settings = candidate.update(changes)
        # Atomic creation with no overwrite, including a second initializer finishing first.
        os.link(candidate.path, path)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("plugins", "prompts", "logs"):
        (path.parent / name).mkdir(mode=0o700, exist_ok=True)
    return settings


def clone_source(home: Path, repository: str = SOURCE_REPOSITORY) -> dict[str, Any]:
    """Keep a reference checkout outside the installed package; never execute its code."""
    target = home / "source"
    if target.exists():
        return {"available": (target / ".git").exists(), "path": str(target), "created": False}
    if shutil.which("git") is None:
        return {"available": False, "reason": "未安装 Git，可稍后让沐璃准备源码参考"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    with tempfile.TemporaryDirectory(prefix=".source-", dir=home) as directory:
        temporary = Path(directory) / "source"
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "http.proxy=",
                    "clone",
                    "--quiet",
                    "--depth=1",
                    "--single-branch",
                    repository,
                    str(temporary),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "reason": "源码下载未完成；配置已保存，可稍后重试"}
        if result.returncode:
            return {"available": False, "reason": "源码下载未完成；配置已保存，可稍后重试"}
        temporary.rename(target)
    return {"available": True, "path": str(target), "created": True}
