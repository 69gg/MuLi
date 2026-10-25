"""An unnamed, non-breakaway Windows Job owned by one shell OS generation.

The caller starts a profile-free shell waiting for host input, attaches it here,
and only then dispatches user commands. No PID-tree enumeration or adoption.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from typing import Any

_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_SIZE = ctypes.c_size_t
_U64 = ctypes.c_uint64
_I64 = ctypes.c_int64
_KILL_ON_JOB_CLOSE = 0x00002000
_SET_QUOTA = 0x0100
_TERMINATE = 0x0001
_QUERY_LIMITED = 0x1000


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _I64),
        ("PerJobUserTimeLimit", _I64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE),
        ("MaximumWorkingSetSize", _SIZE),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", _U64),
        ("WriteOperationCount", _U64),
        ("OtherOperationCount", _U64),
        ("ReadTransferCount", _U64),
        ("WriteTransferCount", _U64),
        ("OtherTransferCount", _U64),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", _SIZE),
        ("JobMemoryLimit", _SIZE),
        ("PeakProcessMemoryUsed", _SIZE),
        ("PeakJobMemoryUsed", _SIZE),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", _I64),
        ("TotalKernelTime", _I64),
        ("ThisPeriodTotalUserTime", _I64),
        ("ThisPeriodTotalKernelTime", _I64),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


def _error() -> OSError:
    code = int(getattr(ctypes, "get_last_error", lambda: 0)())
    return OSError(code, "无法确认或收口 shell 的 Windows Job 归属")


def cmd_console_options() -> dict[str, Any]:
    """Give cmd its own hidden console, leaving the host console untouched."""
    if os.name != "nt":
        raise OSError("cmd 控制台仅能在 Windows 上创建")
    factory = getattr(subprocess, "STARTUPINFO", None)
    use_show = getattr(subprocess, "STARTF_USESHOWWINDOW", None)
    hide = getattr(subprocess, "SW_HIDE", None)
    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", None)
    if not callable(factory) or any(
        type(value) is not int for value in (use_show, hide, new_console)
    ):
        raise OSError("cmd 隐藏控制台能力不可用")
    startup = factory()
    startup.dwFlags |= use_show
    startup.wShowWindow = hide
    return {
        "creationflags": new_console,
        "startupinfo": startup,
    }


class WindowsShellJob:
    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if os.name != "nt" or loader is None:
            raise OSError("Windows Job 仅能在 Windows 上使用")
        self._api = loader("kernel32", use_last_error=True)
        self._api.CreateJobObjectW.argtypes = [_HANDLE, ctypes.c_wchar_p]
        self._api.CreateJobObjectW.restype = _HANDLE
        self._api.SetInformationJobObject.argtypes = [_HANDLE, _DWORD, _HANDLE, _DWORD]
        self._api.SetInformationJobObject.restype = _BOOL
        self._api.OpenProcess.argtypes = [_DWORD, _BOOL, _DWORD]
        self._api.OpenProcess.restype = _HANDLE
        self._api.GetProcessId.argtypes = [_HANDLE]
        self._api.GetProcessId.restype = _DWORD
        self._api.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
        self._api.AssignProcessToJobObject.restype = _BOOL
        self._api.IsProcessInJob.argtypes = [_HANDLE, _HANDLE, ctypes.POINTER(_BOOL)]
        self._api.IsProcessInJob.restype = _BOOL
        self._api.TerminateJobObject.argtypes = [_HANDLE, _DWORD]
        self._api.TerminateJobObject.restype = _BOOL
        self._api.QueryInformationJobObject.argtypes = [
            _HANDLE,
            _DWORD,
            _HANDLE,
            _DWORD,
            ctypes.POINTER(_DWORD),
        ]
        self._api.QueryInformationJobObject.restype = _BOOL
        self._api.CloseHandle.argtypes = [_HANDLE]
        self._api.CloseHandle.restype = _BOOL
        self._handle: int | None = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise _error()
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise _error()
        self.pid: int | None = None

    def attach(self, pid: int) -> None:
        if self._handle is None or self.pid is not None or type(pid) is not int or pid <= 0:
            raise ValueError("Windows shell Job 状态无效")
        process = self._api.OpenProcess(_SET_QUOTA | _TERMINATE | _QUERY_LIMITED, False, pid)
        if not process:
            raise _error()
        try:
            if int(self._api.GetProcessId(process)) != pid:
                raise _error()
            if not self._api.AssignProcessToJobObject(self._handle, process):
                raise _error()
            inside = _BOOL()
            if not self._api.IsProcessInJob(process, self._handle, ctypes.byref(inside)):
                raise _error()
            if not inside.value:
                raise _error()
            self.pid = pid
        finally:
            self._api.CloseHandle(process)

    def terminate(self) -> None:
        if self._handle is not None and not self._api.TerminateJobObject(self._handle, 1):
            raise _error()

    def active_processes(self) -> int:
        if self._handle is None:
            return 0
        result, size = _Accounting(), _DWORD()
        if not self._api.QueryInformationJobObject(
            self._handle, 1, ctypes.byref(result), ctypes.sizeof(result), ctypes.byref(size)
        ):
            raise _error()
        return int(result.ActiveProcesses)

    def close(self) -> None:
        handle = self._handle
        if handle is not None:
            if not self._api.CloseHandle(handle):
                raise _error()
            self._handle = None
