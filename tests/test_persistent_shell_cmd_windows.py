"""Real cmd consoles and owned descendants; no desktop automation is required."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

import muli.shell_protocol as protocol_module
import muli.shell_sessions as shell_module
from muli.platforms.windows_shell import WindowsShellJob
from muli.shell_protocol import quote
from muli.shell_sessions import ShellSessions

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires real Windows cmd/console/Job")

_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258


def windows_api() -> Any:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetConsoleCP.argtypes = []
    api.GetConsoleCP.restype = ctypes.c_uint32
    api.GetConsoleOutputCP.argtypes = []
    api.GetConsoleOutputCP.restype = ctypes.c_uint32
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.GetProcessId.argtypes = [ctypes.c_void_p]
    api.GetProcessId.restype = ctypes.c_uint32
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int32
    return api


def cmd_sessions(directory: Path) -> ShellSessions:
    executable = shutil.which(os.environ.get("COMSPEC") or "cmd")
    assert executable is not None, "Windows must provide its actual cmd executable"
    return ShellSessions(
        default_cwd=directory,
        executable=executable,
        max_sessions=lambda: 2,
        max_output_bytes=lambda: 4096,
        max_command_bytes=lambda: 4096,
        max_run_seconds=lambda: 3,
    )


async def completed(
    manager: ShellSessions, session_id: str, receipt: dict[str, Any]
) -> dict[str, Any]:
    for _ in range(3):
        if receipt["state"] != "running":
            break
        receipt = await manager.read(
            session_id, receipt["command_id"], cursor=receipt["next_cursor"], wait_seconds=1
        )
    if receipt["state"] != "completed":
        snapshot = await manager.read(
            session_id, receipt["command_id"], cursor={"output": 0, "stderr": 0}
        )
        # This fixture retains at most 4096 bytes across both streams. Re-read
        # all retained output, rather than dumping an already consumed page.
        diagnostic: dict[str, Any] = {
            "state": snapshot["state"],
            "stdout": snapshot["output"],
            "stderr": snapshot["stderr"],
            "observed_bytes": snapshot["observed_bytes"],
            "discarded_bytes": snapshot["discarded_bytes"],
        }
        item = manager._commands.get(receipt["command_id"])
        if item is not None:
            diagnostic.update(
                proof=item.proof.is_set(),
                stdout_fence=item.stdout_fence.is_set(),
                stderr_fence=item.stderr_fence.is_set(),
            )
        message = json.dumps(diagnostic, ensure_ascii=True)
        if item is not None:
            message = message.replace(item.shell.secret, "[fixture-protocol-secret]")
        pytest.fail(message)
    return await manager.read(session_id, receipt["command_id"])


async def test_forced_cmd_verifies_utf8_and_preserves_unicode_state_in_the_same_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = windows_api()
    original_console = (int(api.GetConsoleCP()), int(api.GetConsoleOutputCP()))
    directory = tmp_path / "工作目录"
    destination = directory / "后续目录"
    destination.mkdir(parents=True)
    protocol_directory = tmp_path / "协议目录"
    protocol_directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(protocol_directory))
    attached: list[int] = []
    spawned: list[int] = []
    actual_attach = WindowsShellJob.attach
    actual_spawn = asyncio.create_subprocess_exec
    actual_bootstrap = shell_module.cmd_bootstrap
    actual_dispatch = shell_module.dispatch
    for name in (
        "__MULI_BOOT_PYTHON",
        "__MULI_BOOT_HELPER",
        "__MULI_BOOT_CONFIG",
        "__MULI_BOOT_ROOT",
    ):
        monkeypatch.setenv(name, "must-be-overridden")
    parent_environment = dict(os.environ)

    def attach(job: WindowsShellJob, pid: int) -> None:
        actual_attach(job, pid)
        attached.append(pid)

    async def spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        assert kwargs["creationflags"] == int(subprocess.CREATE_NEW_CONSOLE)
        assert not kwargs["creationflags"] & int(subprocess.CREATE_NO_WINDOW)
        startup = kwargs["startupinfo"]
        assert startup.dwFlags & int(subprocess.STARTF_USESHOWWINDOW)
        assert startup.wShowWindow == int(subprocess.SW_HIDE)
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.PIPE
        environment = kwargs["env"]
        assert environment is not os.environ
        assert environment["__MULI_BOOT_PYTHON"] == str(Path(sys.executable).absolute())
        assert environment["__MULI_BOOT_HELPER"] == str(Path(protocol_module.__file__).absolute())
        config = Path(environment["__MULI_BOOT_CONFIG"])
        assert config.name == "control.json" and config.parent.parent == protocol_directory
        assert Path(environment["__MULI_BOOT_ROOT"]) == config.parent
        assert dict(os.environ) == parent_environment
        process = await actual_spawn(*args, **kwargs)
        spawned.append(process.pid)
        return process

    def bootstrap() -> bytes:
        assert attached == spawned and len(attached) == 1
        data = actual_bootstrap()
        assert data.isascii()
        return data

    def dispatch(root: Path, command_id: str, flavor: str) -> bytes:
        assert attached == spawned and len(attached) == 1
        assert flavor == "cmd" and root.parent == protocol_directory
        data = actual_dispatch(root, command_id, flavor)
        assert data.isascii()
        return data

    monkeypatch.setattr(WindowsShellJob, "attach", attach)
    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(shell_module, "cmd_bootstrap", bootstrap)
    monkeypatch.setattr(shell_module, "dispatch", dispatch)
    manager = cmd_sessions(directory)
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1

    try:
        assert manager.status("owner")["state"] == "not_started"
        assert spawned == attached == []
        async with asyncio.timeout(4):
            first = await completed(
                manager,
                "owner",
                await manager.execute(
                    "owner",
                    f"@cd /d {quote(str(destination), 'cmd')}\r\n"
                    '@set "MULI_CMD_CONTEXT=沐璃"\r\n'
                    "@echo %MULI_CMD_CONTEXT%\r\n@cd\r\n@exit /b 0",
                    wait_seconds=1,
                    timeout=3,
                    admission_guard=guard,
                ),
            )
            assert first["flavor"] == "cmd" and first["status_basis"] == "shell_status"
            assert first["exit_code"] == 0
            assert type(first["shell_pid"]) is int and first["shell_pid"] > 0
            assert attached == spawned == [first["shell_pid"]]
            assert Path(first["cwd"]).resolve() == destination.resolve()
            assert "沐璃" in first["output"] and str(destination) in first["output"]
            probe = tmp_path / "核验代码页.py"
            probe.write_text(
                "import ctypes, json, os, sys\n"
                "api = ctypes.WinDLL('kernel32', use_last_error=True)\n"
                "api.GetConsoleCP.restype = ctypes.c_uint32\n"
                "api.GetConsoleOutputCP.restype = ctypes.c_uint32\n"
                "sys.stdout.reconfigure(encoding='utf-8')\n"
                "print('CMD_PROBE:' + json.dumps({\n"
                "    'input_cp': int(api.GetConsoleCP()),\n"
                "    'output_cp': int(api.GetConsoleOutputCP()),\n"
                "    'cwd': os.getcwd(),\n"
                "    'value': os.environ.get('MULI_CMD_CONTEXT'),\n"
                "}, ensure_ascii=False), flush=True)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            second = await completed(
                manager,
                "owner",
                await manager.execute(
                    "owner",
                    f"@{quote(sys.executable, 'cmd')} -I {quote(str(probe), 'cmd')}",
                    wait_seconds=1,
                    timeout=3,
                    admission_guard=guard,
                ),
            )
            assert second["shell_pid"] == first["shell_pid"]
            assert second["os_epoch"] == first["os_epoch"]
            assert second["exit_code"] == 7 and second["status_basis"] == "shell_status"
            rows = [line.partition("CMD_PROBE:")[2] for line in second["output"].splitlines()]
            actual = json.loads(next(row for row in rows if row))
            assert actual == {
                "input_cp": 65001,
                "output_cp": 65001,
                "cwd": str(destination),
                "value": "沐璃",
            }
            assert guard_calls == 4 and attached == spawned == [first["shell_pid"]]
    finally:
        await manager.aclose()
        assert dict(os.environ) == parent_environment
        assert (int(api.GetConsoleCP()), int(api.GetConsoleOutputCP())) == original_console


async def test_wrong_cmd_console_code_page_never_reaches_user_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = windows_api()
    original_console = (int(api.GetConsoleCP()), int(api.GetConsoleOutputCP()))
    manager = cmd_sessions(tmp_path)
    actual_bootstrap = shell_module.cmd_bootstrap
    bootstraps = 0
    guards = 0
    effect = tmp_path / "must-not-exist.txt"

    def wrong_code_page() -> bytes:
        nonlocal bootstraps
        original = actual_bootstrap()
        assert original.count(b"@chcp 65001") == 1
        bootstraps += 1
        return original.replace(b"@chcp 65001", b"@chcp 437", 1)

    def guard() -> None:
        nonlocal guards
        guards += 1

    monkeypatch.setattr(shell_module, "cmd_bootstrap", wrong_code_page)
    try:
        async with asyncio.timeout(4):
            with pytest.raises(TimeoutError):
                await manager.execute(
                    "owner",
                    f"@echo USER_EFFECT>{quote(str(effect), 'cmd')}",
                    wait_seconds=1,
                    timeout=3,
                    admission_guard=guard,
                )
            assert bootstraps == guards == 1
            assert not effect.exists()
            assert manager.live_sessions() == ()
            result = manager.status("owner")
            assert result["state"] == "unknown" and result["exit_code"] is None
    finally:
        await manager.aclose()
        assert (int(api.GetConsoleCP()), int(api.GetConsoleOutputCP())) == original_console


async def test_forced_cmd_stop_reaps_the_actual_owned_native_descendant(tmp_path: Path) -> None:
    api = windows_api()
    original_console = (int(api.GetConsoleCP()), int(api.GetConsoleOutputCP()))
    manager = cmd_sessions(tmp_path)
    probe = tmp_path / "等待收口.py"
    probe.write_text(
        "import os, time\nprint('CHILD_READY:' + str(os.getpid()), flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    handle: int | None = None
    try:
        async with asyncio.timeout(4):
            receipt = await manager.execute(
                "owner",
                f"@{quote(sys.executable, 'cmd')} -I -u {quote(str(probe), 'cmd')}",
                wait_seconds=0,
                timeout=3,
                admission_guard=lambda: None,
            )
            output = receipt["output"]
            for _ in range(3):
                if re.search(r"CHILD_READY:(\d+)", output):
                    break
                receipt = await manager.read(
                    "owner", receipt["command_id"], cursor=receipt["next_cursor"], wait_seconds=1
                )
                output += receipt["output"]
            match = re.search(r"CHILD_READY:(\d+)", output)
            assert match is not None, receipt
            child_pid = int(match.group(1))
            assert receipt["flavor"] == "cmd" and receipt["state"] == "running"
            assert child_pid != receipt["shell_pid"]
            handle = api.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED_INFORMATION, False, child_pid)
            assert handle and int(api.GetProcessId(handle)) == child_pid
            assert int(api.WaitForSingleObject(handle, 0)) == _WAIT_TIMEOUT
            stopped = await manager.stop("owner")
            assert stopped["os_epoch"] == receipt["os_epoch"]
            assert stopped["state"] == "stopped" and stopped["exit_code"] is None
            assert int(api.WaitForSingleObject(handle, 0)) == _WAIT_OBJECT_0
            assert manager.live_sessions() == ()
    finally:
        await manager.aclose()
        if handle:
            assert api.CloseHandle(handle)
        assert (int(api.GetConsoleCP()), int(api.GetConsoleOutputCP())) == original_console
