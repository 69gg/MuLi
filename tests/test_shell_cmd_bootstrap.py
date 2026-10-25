"""Cmd protocol writes keep Unicode host paths outside the stdin byte stream."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import muli.shell_protocol as protocol_module


def test_cmd_bootstrap_carries_unicode_paths_only_in_the_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "宿主 Python" / "python.exe"
    helper = tmp_path / "宿主帮助目录" / "shell_protocol.py"
    root = tmp_path / "协议 &!^目录"
    paths = {
        "__MULI_BOOT_PYTHON": str(python),
        "__MULI_BOOT_HELPER": str(helper),
        "__MULI_BOOT_CONFIG": str(root / "control.json"),
        "__MULI_BOOT_ROOT": str(root),
    }
    monkeypatch.setattr(protocol_module.sys, "executable", str(python))
    monkeypatch.setattr(protocol_module, "__file__", str(helper))
    for name in paths:
        monkeypatch.setenv(name, "stale")
        monkeypatch.setenv(name.lower(), "stale-alternate-case")
    monkeypatch.setenv("MULI_BOOTSTRAP_KEEP", "继承环境")
    parent_environment = dict(os.environ)

    environment = protocol_module.cmd_environment(root)
    assert environment is not os.environ
    assert environment["MULI_BOOTSTRAP_KEEP"] == "继承环境"
    assert {key for key in environment if key.upper() in paths} == set(paths)
    for name, value in paths.items():
        assert environment[name] == value
    assert dict(os.environ) == parent_environment

    bootstrap = protocol_module.cmd_bootstrap()
    assert bootstrap.isascii()
    assert bootstrap.splitlines() == [
        b"@chcp 65001 >nul",
        b"@if errorlevel 1 exit",
        b'@"%__MULI_BOOT_PYTHON%" -I "%__MULI_BOOT_HELPER%" "%__MULI_BOOT_CONFIG%" ready 0',
    ]


@pytest.mark.parametrize("component", ["协议目录", "协议 &!^目录"])
def test_cmd_dispatch_carries_unicode_paths_only_in_the_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    python = tmp_path / "宿主 Python" / "python.exe"
    helper = tmp_path / "宿主帮助目录" / "shell_protocol.py"
    root = tmp_path / component
    command_id = "0123456789abcdef" * 2
    monkeypatch.setattr(protocol_module.sys, "executable", str(python))
    monkeypatch.setattr(protocol_module, "__file__", str(helper))

    data = protocol_module.dispatch(root, command_id, "cmd")
    assert data.isascii()
    assert data.splitlines() == [
        b'@call "%__MULI_BOOT_ROOT%\\' + command_id.encode("ascii") + b'.cmd" <nul',
        b'@set "__muli_exit=%errorlevel%"',
        b'@"%__MULI_BOOT_PYTHON%" -I "%__MULI_BOOT_HELPER%" "%__MULI_BOOT_CONFIG%" '
        + command_id.encode("ascii")
        + b' "%__muli_exit%"',
    ]


def test_cmd_user_batch_file_preserves_utf8_outside_the_ascii_dispatch(tmp_path: Path) -> None:
    root = tmp_path / "协议目录"
    root.mkdir()
    destination = tmp_path / "后续工作目录"
    path = protocol_module.command_file(root, "0123456789abcdef" * 2, "cmd")
    command = '@set "MULI_CMD_CONTEXT=沐璃"\r\n@echo %MULI_CMD_CONTEXT%'

    protocol_module.write_command(path, command, "cmd", destination)

    data = path.read_bytes()
    assert command.encode("utf-8") in data
    assert str(destination).encode("utf-8") in data
    assert data.decode("utf-8").endswith(command + "\r\n")


@pytest.mark.parametrize("command_id", ["g" * 32, "a" * 32 + "\r\n@echo injected"])
def test_cmd_dispatch_rejects_non_identifier_input(tmp_path: Path, command_id: str) -> None:
    with pytest.raises(ValueError, match="shell 命令标识无效"):
        protocol_module.dispatch(tmp_path, command_id, "cmd")


@pytest.mark.parametrize("component", ["python", "helper", "config"])
@pytest.mark.parametrize("invalid", ['"', "%"])
def test_cmd_environment_keeps_existing_host_path_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str, invalid: str
) -> None:
    python = tmp_path / "python.exe"
    helper = tmp_path / "shell_protocol.py"
    root = tmp_path / "协议目录"
    if component == "python":
        python = tmp_path / ("python" + invalid + ".exe")
    elif component == "helper":
        helper = tmp_path / ("shell_protocol" + invalid + ".py")
    else:
        root = tmp_path / ("协议" + invalid + "目录")
    monkeypatch.setattr(protocol_module.sys, "executable", str(python))
    monkeypatch.setattr(protocol_module, "__file__", str(helper))
    parent_environment = dict(os.environ)

    with pytest.raises(ValueError, match="cmd 的协议路径不支持引号或百分号"):
        protocol_module.cmd_environment(root)
    assert dict(os.environ) == parent_environment


@pytest.mark.parametrize("component", ["python", "helper", "root"])
@pytest.mark.parametrize("invalid", ['"', "%"])
def test_cmd_dispatch_keeps_existing_host_path_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str, invalid: str
) -> None:
    python = tmp_path / "python.exe"
    helper = tmp_path / "shell_protocol.py"
    root = tmp_path / "协议目录"
    if component == "python":
        python = tmp_path / ("python" + invalid + ".exe")
    elif component == "helper":
        helper = tmp_path / ("shell_protocol" + invalid + ".py")
    else:
        root = tmp_path / ("协议" + invalid + "目录")
    monkeypatch.setattr(protocol_module.sys, "executable", str(python))
    monkeypatch.setattr(protocol_module, "__file__", str(helper))

    with pytest.raises(ValueError, match="cmd 的协议路径不支持引号或百分号"):
        protocol_module.dispatch(root, "0123456789abcdef" * 2, "cmd")
