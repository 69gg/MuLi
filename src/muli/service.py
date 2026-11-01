"""Current-user background services, with no implicit start or administrator access."""

from __future__ import annotations

import getpass
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from uuid import uuid4

from muli.process import run_process

UNIT = "muli.service"
LAUNCH_LABEL = "muli.agent"
TASK_NAME = "MuLi"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def quote_unit(value: str) -> str:
    if any(character in value for character in "\n\r\x00"):
        raise ValueError("服务路径包含非法字符")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _tail(path: Path, limit: int = 16000) -> str:
    try:
        with path.open("rb") as source:
            source.seek(max(0, source.seek(0, 2) - limit))
            return source.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def windows_task_xml(arguments: list[str], *, enabled: bool = False, logon: bool = True) -> str:
    ET.register_namespace("", TASK_NAMESPACE)
    root = ET.Element("Task", {"version": "1.3", "xmlns": TASK_NAMESPACE})
    registration = ET.SubElement(root, "RegistrationInfo")
    ET.SubElement(registration, "Description").text = "MuLi personal assistant"
    if logon:
        trigger = ET.SubElement(ET.SubElement(root, "Triggers"), "LogonTrigger")
        ET.SubElement(trigger, "Enabled").text = str(enabled).lower()
        ET.SubElement(trigger, "UserId").text = getpass.getuser()
    principal = ET.SubElement(ET.SubElement(root, "Principals"), "Principal", {"id": "Owner"})
    ET.SubElement(principal, "UserId").text = getpass.getuser()
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(root, "Settings")
    ET.SubElement(settings, "AllowStartOnDemand").text = "true"
    restart = ET.SubElement(settings, "RestartOnFailure")
    ET.SubElement(restart, "Interval").text = "PT1M"
    ET.SubElement(restart, "Count").text = "3"
    for key, value in {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "Enabled": "true",
        "Hidden": "true",
        "ExecutionTimeLimit": "PT0S",
    }.items():
        ET.SubElement(settings, key).text = value
    action = ET.SubElement(ET.SubElement(root, "Actions", {"Context": "Owner"}), "Exec")
    ET.SubElement(action, "Command").text = arguments[0]
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(arguments[1:])
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


class UserService:
    def __init__(
        self,
        config_path: Path,
        data_dir: Path,
        unit_dir: Path | None = None,
        *,
        platform: str | None = None,
        executable: str | None = None,
    ) -> None:
        self.config_path, self.data_dir = config_path, data_dir
        self.platform = platform or sys.platform
        self.executable = executable or sys.executable
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        if self.platform == "darwin":
            self.backend, self.unit = "launchd", LAUNCH_LABEL
            self.path = (unit_dir or Path.home() / "Library/LaunchAgents") / f"{LAUNCH_LABEL}.plist"
        elif self.platform == "win32":
            self.backend, self.unit = "task_scheduler", TASK_NAME
            self.path = (unit_dir or data_dir / "service") / "muli-task.xml"
        else:
            self.backend, self.unit = "systemd", UNIT
            self.path = (unit_dir or base / "systemd/user") / UNIT
        self.log_path = data_dir / "logs/service.log"

    def capability(self) -> dict[str, Any]:
        needed: tuple[str, ...]
        if self.platform == "linux":
            needed = ("systemctl",)
        elif self.platform == "darwin":
            needed = ("launchctl",)
        elif self.platform == "win32":
            needed = ("schtasks", "powershell")
        else:
            return {"supported": False, "backend": self.backend, "reason": "没有该平台的服务后端"}
        missing = [name for name in needed if shutil.which(name) is None]
        if self.platform == "win32" and not self._windowless_python().is_file():
            missing.append("当前 Python 环境的 pythonw.exe")
        return {
            "supported": not missing,
            "backend": self.backend,
            "reason": "缺少 " + "、".join(missing) if missing else "",
        }

    @property
    def supported(self) -> bool:
        return bool(self.capability()["supported"])

    def _windowless_python(self) -> Path:
        executable = Path(self.executable)
        return (
            executable
            if executable.name.lower() == "pythonw.exe"
            else executable.with_name("pythonw.exe")
        )

    def _arguments(self) -> list[str]:
        command = [self.executable, "-m", "muli"]
        if self.platform in {"darwin", "win32"}:
            executable = (
                str(self._windowless_python()) if self.platform == "win32" else self.executable
            )
            command = [
                executable,
                "-m",
                "muli.platforms.service_runner",
                "--log-file",
                str(self.log_path.resolve()),
                "--",
            ]
        return command + [
            "--config",
            str(self.config_path.resolve()),
            "--data-dir",
            str(self.data_dir.resolve()),
            "serve",
        ]

    def render(self, *, enabled: bool = False) -> str:
        arguments = self._arguments()
        if self.platform == "darwin":
            payload = {
                "Label": LAUNCH_LABEL,
                "ProgramArguments": arguments,
                "RunAtLoad": enabled,
                "KeepAlive": {"SuccessfulExit": False} if enabled else False,
                "ThrottleInterval": 10,
                "ProcessType": "Background",
                "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            }
            return plistlib.dumps(payload, sort_keys=False).decode("utf-8")
        if self.platform == "win32":
            return windows_task_xml(arguments, enabled=enabled)
        command = " ".join(quote_unit(argument) for argument in arguments)
        return f"""[Unit]
Description=MuLi personal assistant
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={command}
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

    @property
    def _launch_domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _auto_enabled(self) -> bool:
        try:
            if self.platform == "darwin":
                return plistlib.loads(self.path.read_bytes()).get("RunAtLoad") is True
            if self.platform == "win32":
                value = ET.fromstring(self.path.read_text(encoding="utf-8"))
                return (
                    value.findtext(
                        f".//{{{TASK_NAMESPACE}}}LogonTrigger/{{{TASK_NAMESPACE}}}Enabled"
                    )
                    == "true"
                )
        except (OSError, ValueError, ET.ParseError):
            pass
        return False

    async def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            **self.capability(),
            "installed": self.path.exists(),
            "active": False,
            "enabled": False,
            "unit": self.unit,
            "log": "",
            "path": str(self.path),
        }
        if not result["supported"]:
            return result
        try:
            if self.platform == "linux":
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
            elif self.platform == "darwin":
                response = await run_process(
                    ["launchctl", "print", f"{self._launch_domain}/{LAUNCH_LABEL}"],
                    timeout=5,
                )
                result["active"] = response["exit_code"] == 0 and bool(
                    re.search(r"\bstate\s*=\s*running\b", response["output"])
                )
                disabled = await run_process(
                    ["launchctl", "print-disabled", self._launch_domain],
                    timeout=5,
                )
                override = re.search(
                    rf'"{re.escape(LAUNCH_LABEL)}"\s*=>\s*true', disabled["output"]
                )
                result["enabled"] = self._auto_enabled() and not override
                result["log"] = _tail(self.log_path)
            else:
                # The script is constant; configuration values never enter PowerShell source.
                script = (
                    "$s=New-Object -ComObject Schedule.Service;$s.Connect();"
                    "try{$t=$s.GetFolder('\\').GetTask('MuLi');"
                    "@{state=[int]$t.State;xml=$t.Xml}|ConvertTo-Json -Compress}"
                    "catch{exit 1}"
                )
                response = await run_process(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    timeout=5,
                    limit=16000,
                )
                result["installed"] = response["exit_code"] == 0
                if result["installed"]:
                    payload = json.loads(response["output"])
                    if not isinstance(payload, dict) or not isinstance(payload.get("xml"), str):
                        raise ValueError("计划任务返回了无效状态")
                    result["active"] = payload.get("state") == 4
                    root = ET.fromstring(payload["xml"])
                    result["enabled"] = (
                        root.findtext(
                            f".//{{{TASK_NAMESPACE}}}LogonTrigger/{{{TASK_NAMESPACE}}}Enabled"
                        )
                        == "true"
                        and root.findtext(
                            f"{{{TASK_NAMESPACE}}}Settings/{{{TASK_NAMESPACE}}}Enabled"
                        )
                        != "false"
                    )
                result["log"] = _tail(self.log_path)
        except (OSError, ValueError, TimeoutError, ET.ParseError) as exc:
            result["reason"] = f"无法读取服务状态：{exc}"
        return result

    async def _run(self, arguments: list[str]) -> None:
        response = await run_process(arguments, timeout=10, limit=16000)
        if response["exit_code"]:
            raise ValueError(f"{self.backend} 用户服务操作失败，请检查用户会话日志")

    async def action(self, action: str, *, confirm: bool = False) -> dict[str, Any]:
        capability = self.capability()
        if not capability["supported"]:
            raise ValueError(str(capability["reason"]))
        if action not in {
            "install",
            "start",
            "stop",
            "restart",
            "enable",
            "disable",
            "uninstall",
        }:
            raise ValueError("未知服务操作")
        if action == "uninstall":
            if not confirm:
                raise ValueError("卸载用户服务需要 confirm=true；配置、数据和日志会保留")
            return await self._uninstall()
        if action != "install" and not self.path.exists():
            raise ValueError("请先安装用户服务")
        if self.platform == "linux":
            if action == "install":
                _write_private(self.path, self.render())
                await self._run(["systemctl", "--user", "daemon-reload"])
            else:
                await self._run(["systemctl", "--user", "--no-block", action, UNIT])
        elif self.platform == "darwin":
            await self._launch_action(action)
        else:
            await self._task_action(action)
        return {"accepted": True, "action": action, "backend": self.backend}

    async def _uninstall(self) -> dict[str, Any]:
        from muli.managed_cli import managed_record

        root = self.data_dir.absolute() / "management" / "transaction-v1"
        record = managed_record(root)
        if record is not None and not record.resolved:
            raise ValueError("托管事务尚未完成，不能卸载用户服务")
        if self.platform == "linux":
            await self._run(["systemctl", "--user", "disable", UNIT])
            self.path.unlink(missing_ok=True)
            await self._run(["systemctl", "--user", "daemon-reload"])
            await self._run(["systemctl", "--user", "--no-block", "stop", UNIT])
        elif self.platform == "darwin":
            target = f"{self._launch_domain}/{LAUNCH_LABEL}"
            self.path.unlink(missing_ok=True)
            loaded = await run_process(["launchctl", "print", target], timeout=5, limit=1000)
            if loaded["exit_code"] == 0:
                await self._run(["launchctl", "bootout", target])
        else:
            await run_process(
                ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=10, limit=4000
            )
            self.path.unlink(missing_ok=True)
        return {
            "accepted": True,
            "action": "uninstall",
            "backend": self.backend,
            "preserved": [
                str(self.config_path),
                str(self.data_dir),
                str(self.log_path.parent),
            ],
        }

    async def _launch_action(self, action: str) -> None:
        target = f"{self._launch_domain}/{LAUNCH_LABEL}"
        if action in {"install", "enable", "disable"}:
            enabled = self._auto_enabled() if action == "install" else action == "enable"
            _write_private(self.path, self.render(enabled=enabled))
            if action != "install":
                await self._run(["launchctl", action, target])
            return
        loaded = await run_process(["launchctl", "print", target], timeout=5, limit=1000)
        if action == "stop":
            if loaded["exit_code"] == 0:
                await self._run(["launchctl", "bootout", target])
            return
        await self._run(["launchctl", "enable", target])
        if loaded["exit_code"] != 0:
            await self._run(["launchctl", "bootstrap", self._launch_domain, str(self.path)])
        await self._run(
            ["launchctl", "kickstart", *(["-k"] if action == "restart" else []), target]
        )

    async def _task_action(self, action: str) -> None:
        if action in {"install", "enable", "disable"}:
            enabled = self._auto_enabled() if action == "install" else action == "enable"
            _write_private(self.path, self.render(enabled=enabled))
            await self._run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(self.path), "/F"])
            return
        if action == "restart":
            # /End would kill this API process before /Run. A separate scheduler task owns restart.
            helper = "MuLi-restart-" + uuid4().hex
            path = self.path.with_name(helper + ".xml")
            arguments = [
                str(self._windowless_python()),
                "-m",
                "muli.platforms.restart_service",
                "--task-name",
                helper,
                "--log-file",
                str(self.log_path.with_name("restart.log")),
            ]
            _write_private(path, windows_task_xml(arguments, logon=False))
            try:
                await self._run(["schtasks", "/Create", "/TN", helper, "/XML", str(path), "/F"])
                try:
                    await self._run(["schtasks", "/Run", "/TN", helper])
                except BaseException:
                    await run_process(["schtasks", "/Delete", "/TN", helper, "/F"], timeout=5)
                    raise
            finally:
                path.unlink(missing_ok=True)
            return
        if action == "stop" and (await self.status())["active"]:
            await self._run(["schtasks", "/End", "/TN", TASK_NAME])
        if action == "start":
            await self._run(["schtasks", "/Run", "/TN", TASK_NAME])
