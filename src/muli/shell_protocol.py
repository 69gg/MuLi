"""Private shell command files and an independent, bounded completion channel.

This is a protocol boundary against accidental command/output collisions, not a
sandbox against commands running as the same operating-system user.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTROL_BYTES = 65536
CONTROL_TIMEOUT = 3.0
_UTF8_CODE_PAGE = 65001
_IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")


@dataclass(frozen=True, slots=True)
class ShellSpec:
    executable: str
    flavor: str

    def arguments(self, bootstrap: Path) -> list[str]:
        if self.flavor == "pwsh":
            return [
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(bootstrap),
            ]
        if self.flavor == "cmd":
            return [self.executable, "/D", "/Q", "/V:OFF", "/K"]
        flags = {
            "bash": ["--noprofile", "--norc", "-s"],
            "zsh": ["-f", "-s"],
            "fish": ["--no-config"],
            "sh": ["-s"],
        }
        return [self.executable, *flags[self.flavor]]


def select_shell(executable: str = "") -> ShellSpec:
    if not isinstance(executable, str) or "\x00" in executable:
        raise ValueError("shell 可执行文件无效")
    selected = executable
    if not selected and os.name == "nt":
        selected = shutil.which("pwsh") or os.environ.get("COMSPEC", "") or "cmd"
    elif not selected:
        import pwd

        try:
            selected = pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            selected = ""
        if not selected:
            selected = os.environ.get("SHELL", "")
    resolved = shutil.which(selected) if selected else None
    if resolved is None:
        raise ValueError("找不到用户的默认 shell，请显式配置可执行文件")
    name = Path(resolved).name.lower().removesuffix(".exe")
    flavors = {"sh": "sh", "dash": "sh", "bash": "bash", "zsh": "zsh", "fish": "fish"}
    flavor = {"pwsh": "pwsh", "cmd": "cmd"}.get(name) if os.name == "nt" else flavors.get(name)
    if flavor is None:
        raise ValueError("当前默认 shell 尚不支持持续协议，不会改用另一种 shell")
    return ShellSpec(str(Path(resolved).absolute()), flavor)


def marker(secret: str, command_id: str) -> bytes:
    return b"\x00MuLi-shell:" + secret.encode("ascii") + b":" + command_id.encode("ascii") + b"\x00"


def quote(value: str, flavor: str) -> str:
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("shell 路径包含无效字符")
    if flavor == "pwsh":
        return "'" + value.replace("'", "''") + "'"
    if flavor == "fish":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if flavor == "cmd":
        # CALL performs another percent-expansion pass. Refuse such host paths
        # rather than turn a temporary-path component into command syntax.
        if '"' in value or "%" in value:
            raise ValueError("cmd 的协议路径不支持引号或百分号")
        return '"' + value + '"'
    return "'" + value.replace("'", "'\"'\"'") + "'"


def command_file(root: Path, command_id: str, flavor: str) -> Path:
    if _IDENTIFIER.fullmatch(command_id) is None:
        raise ValueError("shell 命令标识无效")
    suffix = {"cmd": ".cmd", "pwsh": ".ps1", "fish": ".fish"}.get(flavor, ".sh")
    return root / (command_id + suffix)


def write_command(path: Path, command: str, flavor: str, cwd: Path | None) -> None:
    prefix = ""
    if cwd is not None:
        directory = quote(str(cwd), flavor)
        if flavor == "pwsh":
            prefix = f"Set-Location -LiteralPath {directory} -ErrorAction Stop\n"
        elif flavor == "cmd":
            prefix = f"@cd /d {directory}\r\n@if errorlevel 1 exit /b %errorlevel%\r\n"
        elif flavor == "fish":
            prefix = f"cd {directory}; or return $status\n"
        else:
            prefix = f"cd {directory} || return $?\n"
    text = prefix + command + ("\r\n" if flavor == "cmd" else "\n")
    if flavor == "pwsh":
        # Capture the user's final success state before the host formatter runs.
        # LASTEXITCODE is sticky across later successful PowerShell cmdlets.
        text += "$global:__muli_command_ok = $?\n"
    with path.open("x", encoding="utf-8", newline="") as stream:
        stream.write(text)
    path.chmod(0o600)


def dispatch(root: Path, command_id: str, flavor: str) -> bytes:
    script = quote(str(command_file(root, command_id, flavor)), flavor)
    helper = quote(str(Path(__file__).absolute()), flavor)
    python = quote(sys.executable, flavor)
    config = quote(str(root / "control.json"), flavor)
    if flavor == "cmd":
        text = (
            f"@call {script} <nul\r\n"
            '@set "__muli_exit=%errorlevel%"\r\n'
            f'@{python} -I {helper} {config} {command_id} "%__muli_exit%"\r\n'
        )
    elif flavor == "fish":
        text = (
            f"source {script} </dev/null\n"
            "set -g __muli_exit $status\n"
            f'{python} -I {helper} {config} {command_id} "$__muli_exit"\n'
        )
    else:
        text = (
            f". {script} </dev/null\n"
            "__muli_exit=$?\n"
            f'{python} -I {helper} {config} {command_id} "$__muli_exit"\n'
        )
    return text.encode("utf-8")


def cmd_bootstrap(root: Path) -> bytes:
    python = quote(sys.executable, "cmd")
    helper = quote(str(Path(__file__).absolute()), "cmd")
    config = quote(str(root / "control.json"), "cmd")
    # The first two lines are ASCII, independent of the inherited code page.
    # A missing console/code-page capability fails closed before user input.
    return (
        f"@chcp {_UTF8_CODE_PAGE} >nul\r\n"
        "@if errorlevel 1 exit\r\n"
        f"@{python} -I {helper} {config} ready 0\r\n"
    ).encode()


def powershell_bootstrap(root: Path) -> str:
    config = quote(str(root / "control.json"), "pwsh")
    directory = quote(str(root), "pwsh")
    # The shell's OS stdin is DEVNULL. The TCP stream only carries generated
    # identifiers; arbitrary command text lives in a separately sourced file.
    return f"""$ErrorActionPreference = 'Continue'
$__muli_config = Get-Content -Raw -LiteralPath {config} | ConvertFrom-Json
$__muli_client = [Net.Sockets.TcpClient]::new('127.0.0.1', [int]$__muli_config.port)
$__muli_stream = $__muli_client.GetStream()
$__muli_reader = [IO.StreamReader]::new($__muli_stream, [Text.UTF8Encoding]::new($false))
$__muli_writer = [IO.StreamWriter]::new($__muli_stream, [Text.UTF8Encoding]::new($false))
$__muli_writer.AutoFlush = $true
$__muli_writer.WriteLine((@{{op='ready';secret=$__muli_config.secret;epoch=$__muli_config.epoch}} | ConvertTo-Json -Compress))
while (($__muli_id = $__muli_reader.ReadLine()) -ne $null) {{
    if ($__muli_id -notmatch '^[a-f0-9]{{32}}$') {{ break }}
    $__muli_path = Join-Path {directory} ($__muli_id + '.ps1')
    $global:__muli_command_ok = $null
    try {{
        . $__muli_path | Out-Default
        if ($null -eq $global:__muli_command_ok) {{ break }}
        if ($global:__muli_command_ok) {{ $__muli_code = 0 }}
        else {{ $__muli_code = 1 }}
    }} catch {{
        $__muli_code = 1
        [Console]::Error.WriteLine($_.Exception.Message)
    }}
    $__muli_fence = [Text.Encoding]::ASCII.GetBytes(([char]0) + 'MuLi-shell:' + $__muli_config.secret + ':' + $__muli_id + ([char]0))
    $__muli_stdout = [Console]::OpenStandardOutput()
    $__muli_stderr = [Console]::OpenStandardError()
    $__muli_stdout.Write($__muli_fence, 0, $__muli_fence.Length)
    $__muli_stdout.Flush()
    $__muli_stderr.Write($__muli_fence, 0, $__muli_fence.Length)
    $__muli_stderr.Flush()
    $__muli_writer.WriteLine((@{{op='complete';secret=$__muli_config.secret;epoch=$__muli_config.epoch;command_id=$__muli_id;exit_code=$__muli_code;cwd=(Get-Location).ProviderPath}} | ConvertTo-Json -Compress))
}}
$__muli_client.Dispose()
"""


def _require_cmd_utf8() -> None:
    # This module also runs by filename under Python -I. Keep the ready probe
    # independent of package imports and run it inside cmd's actual console.
    import ctypes

    loader = getattr(ctypes, "WinDLL", None)
    if os.name != "nt" or loader is None:
        raise OSError("无法核验 cmd 的 UTF-8 控制台")
    api = loader("kernel32", use_last_error=True)
    api.GetConsoleCP.argtypes = []
    api.GetConsoleCP.restype = ctypes.c_uint32
    api.GetConsoleOutputCP.argtypes = []
    api.GetConsoleOutputCP.restype = ctypes.c_uint32
    if (
        int(api.GetConsoleCP()) != _UTF8_CODE_PAGE
        or int(api.GetConsoleOutputCP()) != _UTF8_CODE_PAGE
    ):
        raise OSError("cmd 输入与输出控制台尚未确认使用 UTF-8")


def _report(config_path: str, command_id: str, status: str) -> None:
    ready = command_id == "ready"
    if not ready and _IDENTIFIER.fullmatch(command_id) is None:
        raise ValueError("invalid command")
    with Path(config_path).open("rb") as stream:
        raw = stream.read(CONTROL_BYTES + 1)
    if len(raw) > CONTROL_BYTES:
        raise ValueError("invalid control configuration")
    config: dict[str, Any] = json.loads(raw)
    code = int(status)
    if not -(2**31) <= code < 2**32:
        raise ValueError("invalid exit status")
    payload: dict[str, Any] = {
        "op": "ready" if ready else "complete",
        "secret": config["secret"],
        "epoch": config["epoch"],
    }
    if not ready:
        payload.update(
            {
                "command_id": command_id,
                "exit_code": code,
                "cwd": os.getcwd(),
            }
        )
    elif code != 0:
        raise ValueError("bootstrap failed")
    else:
        _require_cmd_utf8()
    data = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(data) > CONTROL_BYTES:
        raise ValueError("control response too large")
    if not ready:
        fence = marker(config["secret"], command_id)
        os.write(1, fence)
        os.write(2, fence)
    with socket.create_connection(("127.0.0.1", int(config["port"])), CONTROL_TIMEOUT) as client:
        client.sendall(data + b"\n")
        if client.recv(1) != b"1":
            raise ValueError("completion was not acknowledged")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(2)
    try:
        _report(sys.argv[1], sys.argv[2], sys.argv[3])
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(1) from None
