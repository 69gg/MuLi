"""User-scoped Linux service management; never requires root."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from muli.process import run_process

UNIT = "muli.service"


def quote_unit(value: str) -> str:
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("服务路径包含非法字符")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


class UserService:
    def __init__(self, config_path: Path, data_dir: Path, unit_dir: Path | None = None) -> None:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = (unit_dir or base / "systemd/user") / UNIT
        self.config_path, self.data_dir = config_path, data_dir

    @property
    def supported(self) -> bool:
        return sys.platform == "linux" and shutil.which("systemctl") is not None

    def render(self) -> str:
        executable = quote_unit(sys.executable)
        config, data = (
            quote_unit(str(self.config_path.resolve())),
            quote_unit(str(self.data_dir.resolve())),
        )
        return f"""[Unit]
Description=MuLi personal assistant
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} -m muli --config {config} --data-dir {data} serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true
MemoryMax=512M
CPUQuota=50%
TasksMax=64
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""

    async def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "supported": self.supported,
            "installed": self.path.exists(),
            "active": False,
            "enabled": False,
            "unit": UNIT,
            "log": "",
            "path": str(self.path),
        }
        if not self.supported:
            return result
        for field, operation in (("active", "is-active"), ("enabled", "is-enabled")):
            response = await run_process(
                ["systemctl", "--user", operation, UNIT], timeout=5, limit=1000
            )
            result[field] = response["exit_code"] == 0
        if result["installed"] and shutil.which("journalctl"):
            response = await run_process(
                ["journalctl", "--user", "-u", UNIT, "-n", "40", "--no-pager", "-o", "cat"],
                timeout=5,
                limit=16000,
            )
            result["log"] = response["output"]
        return result

    async def action(self, action: str) -> dict[str, Any]:
        if not self.supported:
            raise ValueError("该系统没有 systemd 用户服务")
        if action == "install":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.render(), encoding="utf-8")
            response = await run_process(["systemctl", "--user", "daemon-reload"], timeout=10)
        elif action in {"start", "stop", "restart", "enable", "disable"}:
            if not self.path.exists():
                raise ValueError("请先安装用户服务")
            response = await run_process(
                ["systemctl", "--user", "--no-block", action, UNIT], timeout=10
            )
        else:
            raise ValueError("未知服务操作")
        if response["exit_code"]:
            raise ValueError("systemd 用户服务操作失败，请检查用户会话日志")
        return {"accepted": True, "action": action}
