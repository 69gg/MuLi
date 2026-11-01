from __future__ import annotations

import json
import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

import muli.platforms.restart_service as restart_module
import muli.service as service_module
from muli.platforms.service_runner import BoundedLog
from muli.service import TASK_NAMESPACE, UserService, quote_unit


def test_systemd_literal_arguments_escape_percent_and_dollar(tmp_path: Path) -> None:
    path = tmp_path / "${HOME} %i" / "config.json"
    service = UserService(path, tmp_path / "data", platform="linux")
    rendered = service.render()
    assert "$${HOME} %%i" in rendered
    assert "MemoryMax=512M" in rendered
    assert "CPUQuota=50%" in rendered
    assert "WantedBy=default.target" in rendered
    assert quote_unit('a"b\\c') == '"a\\"b\\\\c"'
    with pytest.raises(ValueError):
        quote_unit("bad\npath")


def test_launch_agent_literal_arguments_and_opt_in_autostart(tmp_path: Path) -> None:
    service = UserService(
        tmp_path / "配置 & $HOME.json", tmp_path / "资料", tmp_path / "agents", platform="darwin"
    )
    payload = plistlib.loads(service.render().encode())
    assert payload["Label"] == "muli.agent"
    assert payload["RunAtLoad"] is False
    assert payload["KeepAlive"] is False
    enabled = plistlib.loads(service.render(enabled=True).encode())
    assert enabled["RunAtLoad"] is True
    assert enabled["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ThrottleInterval"] == 10
    assert payload["ProgramArguments"][-5:] == [
        "--config",
        str(service.config_path.resolve()),
        "--data-dir",
        str(service.data_dir.resolve()),
        "serve",
    ]
    assert "muli.platforms.service_runner" in payload["ProgramArguments"]
    assert plistlib.loads(service.render(enabled=True).encode())["RunAtLoad"] is True


def test_windows_task_uses_pythonw_current_user_and_disabled_logon(tmp_path: Path) -> None:
    service = UserService(
        tmp_path / '配置 & "name".json',
        tmp_path / "数据",
        platform="win32",
        executable=str(tmp_path / "python.exe"),
    )
    root = ET.fromstring(service.render())
    prefix = f"{{{TASK_NAMESPACE}}}"
    assert root.findtext(f".//{prefix}Command") == str(tmp_path / "pythonw.exe")
    assert root.findtext(f".//{prefix}LogonType") == "InteractiveToken"
    assert root.findtext(f".//{prefix}RunLevel") == "LeastPrivilege"
    assert root.findtext(f".//{prefix}LogonTrigger/{prefix}Enabled") == "false"
    assert root.findtext(f".//{prefix}ExecutionTimeLimit") == "PT0S"
    assert root.findtext(f".//{prefix}MultipleInstancesPolicy") == "IgnoreNew"
    assert "service_runner" in str(root.findtext(f".//{prefix}Arguments"))
    assert service.capability()["supported"] is False


async def test_systemd_install_reload_without_start_or_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    service = UserService(tmp_path / "config", tmp_path / "data", tmp_path, platform="linux")
    await service.action("install")
    assert calls == [["systemctl", "--user", "daemon-reload"]]
    await service.action("enable")
    assert calls[-1] == ["systemctl", "--user", "--no-block", "enable", "muli.service"]
    assert "--now" not in calls[-1]


async def test_launch_agent_install_does_not_load_and_start_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 1 if arguments[1] == "print" else 0, "output": ""}

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(service_module.os, "getuid", lambda: 501, raising=False)
    service = UserService(tmp_path / "config", tmp_path / "data", tmp_path, platform="darwin")
    await service.action("install")
    assert calls == []
    await service.action("start")
    assert calls == [
        ["launchctl", "print", "gui/501/muli.agent"],
        ["launchctl", "enable", "gui/501/muli.agent"],
        ["launchctl", "bootstrap", "gui/501", str(service.path)],
        ["launchctl", "kickstart", "gui/501/muli.agent"],
    ]
    assert plistlib.loads(service.path.read_bytes())["RunAtLoad"] is False


async def test_windows_register_without_run_and_enabled_is_logon_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    (tmp_path / "pythonw.exe").touch()
    service = UserService(
        tmp_path / "config",
        tmp_path / "data",
        tmp_path,
        platform="win32",
        executable=str(tmp_path / "python.exe"),
    )
    await service.action("install")
    assert calls == [["schtasks", "/Create", "/TN", "MuLi", "/XML", str(service.path), "/F"]]
    assert service._auto_enabled() is False
    await service.action("enable")
    assert service._auto_enabled() is True
    assert all("/Run" not in arguments for arguments in calls)
    await service.action("start")
    assert calls[-1] == ["schtasks", "/Run", "/TN", "MuLi"]


async def test_windows_status_uses_registered_task_xml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pythonw.exe").touch()
    service = UserService(
        tmp_path / "config",
        tmp_path / "data",
        tmp_path,
        platform="win32",
        executable=str(tmp_path / "python.exe"),
    )

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        assert arguments[0] == "powershell"
        return {
            "exit_code": 0,
            "output": json.dumps({"state": 4, "xml": service.render(enabled=True)}),
        }

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    status = await service.status()
    assert status["installed"] is status["active"] is status["enabled"] is True


async def test_service_probe_errors_are_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        raise TimeoutError("session bus unavailable")

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    status = await UserService(tmp_path / "config", tmp_path, platform="linux").status()
    assert status["active"] is False
    assert "session bus" in status["reason"]


def test_windowless_log_rotates_with_finite_storage(tmp_path: Path) -> None:
    path = tmp_path / "service.log"
    log = BoundedLog(path, max_bytes=16)
    assert log.write("沐璃" * 2) == 4
    log.write("下一条日志")
    log.flush()
    assert path.stat().st_size <= 16
    assert path.with_suffix(".log.1").stat().st_size <= 16
    assert not log.isatty()


async def test_windows_self_restart_is_owned_by_independent_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    descriptors: list[str] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        if "/XML" in arguments:
            descriptors.append(
                Path(arguments[arguments.index("/XML") + 1]).read_text(encoding="utf-8")
            )
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    (tmp_path / "pythonw.exe").touch()
    service = UserService(
        tmp_path / "config",
        tmp_path / "data",
        tmp_path,
        platform="win32",
        executable=str(tmp_path / "python.exe"),
    )
    service.path.write_text(service.render(), encoding="utf-8")
    await service.action("restart")
    assert len(calls) == 2
    assert "/Create" in calls[0] and "/Run" in calls[1]
    helper = calls[0][calls[0].index("/TN") + 1]
    assert helper.startswith("MuLi-restart-")
    assert calls[1][-1] == helper
    assert all("/End" not in arguments for arguments in calls)
    assert "muli.platforms.restart_service" in descriptors[0]
    assert "LogonTrigger" not in descriptors[0]
    assert list(tmp_path.glob("MuLi-restart-*.xml")) == []


async def test_windows_restart_helper_waits_for_stop_starts_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    state = iter(("4", "3"))

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 0, "output": next(state) if arguments[0] == "powershell" else ""}

    monkeypatch.setattr(restart_module, "run_process", run)
    helper = "MuLi-restart-" + "a" * 32
    await restart_module.restart(helper)
    assert calls[1] == ["schtasks", "/End", "/TN", "MuLi"]
    assert calls[3] == ["schtasks", "/Run", "/TN", "MuLi"]
    assert calls[4] == ["schtasks", "/Delete", "/TN", helper, "/F"]


async def test_windows_restart_helper_deletes_itself_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 1 if "/End" in arguments else 0, "output": "4"}

    monkeypatch.setattr(restart_module, "run_process", run)
    helper = "MuLi-restart-" + "b" * 32
    with pytest.raises(ValueError, match="无法停止"):
        await restart_module.restart(helper)
    assert calls[-1] == ["schtasks", "/Delete", "/TN", helper, "/F"]
    assert all("/Run" not in arguments for arguments in calls)


async def test_uninstall_requires_confirmation_and_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    service = UserService(tmp_path / "config.json", tmp_path / "data", tmp_path, platform="linux")
    service.path.write_text(service.render(), encoding="utf-8")
    with pytest.raises(ValueError, match="confirm"):
        await service.action("uninstall")
    assert service.path.exists()
    result = await service.action("uninstall", confirm=True)
    assert result["action"] == "uninstall"
    assert not service.path.exists()
    assert str(service.data_dir) in result["preserved"]
    assert str(service.config_path) in result["preserved"]
    assert calls[0] == ["systemctl", "--user", "disable", "muli.service"]
    assert calls[-2] == ["systemctl", "--user", "daemon-reload"]
    assert calls[-1] == ["systemctl", "--user", "--no-block", "stop", "muli.service"]


async def test_launchd_uninstall_boots_out_and_removes_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(service_module.os, "getuid", lambda: 501, raising=False)
    service = UserService(tmp_path / "config", tmp_path / "data", tmp_path, platform="darwin")
    service.path.write_text(service.render(), encoding="utf-8")
    await service.action("uninstall", confirm=True)
    assert calls == [
        ["launchctl", "print", "gui/501/muli.agent"],
        ["launchctl", "bootout", "gui/501/muli.agent"],
    ]
    assert not service.path.exists()


async def test_windows_uninstall_deletes_task_and_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def run(arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(service_module, "run_process", run)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/bin/" + name)
    (tmp_path / "pythonw.exe").touch()
    service = UserService(
        tmp_path / "config",
        tmp_path / "data",
        tmp_path,
        platform="win32",
        executable=str(tmp_path / "python.exe"),
    )
    service.path.write_text(service.render(), encoding="utf-8")
    await service.action("uninstall", confirm=True)
    assert calls == [["schtasks", "/Delete", "/TN", "MuLi", "/F"]]
    assert not service.path.exists()
