"""Hosted CI's own standard Win32 controls, without a second PowerShell host."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_U32 = ctypes.c_uint32
_I32 = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LRESULT = ctypes.c_ssize_t

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_DISABLED = 0x08000000
WS_TABSTOP = 0x00010000
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
ES_PASSWORD = 0x0020
ES_AUTOHSCROLL = 0x0080
SW_SHOWNOACTIVATE = 4
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
BN_CLICKED = 0
BUTTON_ID = 4
BUTTON_NAME = "Owned Invoke button"
DISABLED_BUTTON_NAME = "Owned disabled button"
WM_READY = 0x8001  # WM_APP + 1, private to this owned window.
UNICODE_TEXT = '中文🌊 "quote" \\ slash'
PASSWORD_LABEL = "private-password-label"
PASSWORD_TEXT = "private-password-value"
_TITLE = re.compile(r"muli-uia-[0-9a-f]{32}\Z")


class _WindowClass(ctypes.Structure):
    _fields_ = [
        ("style", _U32),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", _I32),
        ("cbWndExtra", _I32),
        ("hInstance", _HANDLE),
        ("hIcon", _HANDLE),
        ("hCursor", _HANDLE),
        ("hbrBackground", _HANDLE),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", _I32), ("y", _I32)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", _HANDLE),
        ("message", _U32),
        ("wParam", _WPARAM),
        ("lParam", _LPARAM),
        ("time", _U32),
        ("pt", _Point),
        ("lPrivate", _U32),
    ]


def _configure(user: Any, kernel: Any) -> None:
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = _HANDLE
    signatures: dict[str, tuple[list[Any], Any]] = {
        "RegisterClassW": ([ctypes.POINTER(_WindowClass)], ctypes.c_uint16),
        "CreateWindowExW": (
            [
                _U32,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                _U32,
                _I32,
                _I32,
                _I32,
                _I32,
                _HANDLE,
                _HANDLE,
                _HANDLE,
                _HANDLE,
            ],
            _HANDLE,
        ),
        "DefWindowProcW": ([_HANDLE, _U32, _WPARAM, _LPARAM], _LRESULT),
        "GetMessageW": ([ctypes.POINTER(_Message), _HANDLE, _U32, _U32], _I32),
        "TranslateMessage": ([ctypes.POINTER(_Message)], _I32),
        "DispatchMessageW": ([ctypes.POINTER(_Message)], _LRESULT),
        "PostMessageW": ([_HANDLE, _U32, _WPARAM, _LPARAM], _I32),
        "PostQuitMessage": ([_I32], None),
        "ShowWindow": ([_HANDLE, _I32], _I32),
        "IsWindow": ([_HANDLE], _I32),
        "DestroyWindow": ([_HANDLE], _I32),
        "UnregisterClassW": ([ctypes.c_wchar_p, _HANDLE], _I32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(user, name)
        function.argtypes, function.restype = arguments, result


def window_arguments(title: str) -> tuple[str, ...]:
    if not _TITLE.fullmatch(title):
        raise ValueError("Invalid owned UIA window title")
    executable = sys.executable
    if sys.platform == "win32":
        # Windows venv python.exe redirects through another process. This
        # stdlib-only target must itself be the strictly owned/verified PID.
        base_executable: object = getattr(sys, "_base_executable", None)
        if not isinstance(base_executable, str) or not Path(base_executable).is_absolute():
            raise ValueError("Owned UIA base interpreter unavailable")
        executable = base_executable
    return (executable, "-I", "-B", str(Path(__file__).absolute()), title)


def _run_window(
    user: Any,
    kernel: Any,
    callback_factory: Any,
    title: str,
    *,
    mark: Callable[[str], None],
    ready: Callable[[dict[str, str | int]], None],
    invoked: Callable[[int], None] | None = None,
) -> None:
    instance = kernel.GetModuleHandleW(None)
    if not instance:
        raise AssertionError("Owned Win32 module handle unavailable")
    callback_failed = False
    published = False
    button_handle = 0
    clicks = 0

    def dispatch(hwnd: int, message: int, wparam: int, lparam: int) -> int:
        nonlocal published, callback_failed, clicks
        try:
            if message == WM_READY:
                if not published:
                    mark("message_loop_ready")
                    ready({"pid": os.getpid(), "title": title})
                    published = True
                return 0
            if (
                message == WM_COMMAND
                and hwnd == window
                and wparam & 0xFFFF == BUTTON_ID
                and (wparam >> 16) & 0xFFFF == BN_CLICKED
                and button_handle != 0
                and lparam == button_handle
            ):
                clicks += 1
                if invoked is not None:
                    invoked(clicks)
                return 0
            if message == WM_DESTROY:
                user.PostQuitMessage(0)
                return 0
            return int(user.DefWindowProcW(hwnd, message, wparam, lparam))
        except Exception:
            # Exceptions must not escape a native callback or expose their text.
            callback_failed = True
            user.PostQuitMessage(1)
            return 0

    # Keep the stdcall callback alive until its class has been unregistered.
    callback = callback_factory(_LRESULT, _HANDLE, _U32, _WPARAM, _LPARAM)(dispatch)
    class_name = "MuLiNativeWindow_" + str(os.getpid())
    description = _WindowClass(
        lpfnWndProc=ctypes.cast(callback, ctypes.c_void_p).value,
        hInstance=instance,
        lpszClassName=class_name,
    )
    if not user.RegisterClassW(ctypes.byref(description)):
        raise AssertionError("Owned Win32 window registration failed")
    mark("class_registered")
    window: int | None = None
    try:
        window = user.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            class_name,
            title,
            WS_OVERLAPPEDWINDOW,
            -20000,
            -20000,
            320,
            220,
            None,
            None,
            instance,
            None,
        )
        if not window:
            raise AssertionError("Owned Win32 window creation failed")
        mark("window_created")
        controls = (
            ("STATIC", UNICODE_TEXT, WS_VISIBLE, 5, 5, 300, 25),
            # The standard provider takes EDIT's Name from its preceding label,
            # including a hidden label. It must not appear as a separate node.
            ("STATIC", PASSWORD_LABEL, 0, 5, 40, 1, 1),
            (
                "EDIT",
                PASSWORD_TEXT,
                WS_VISIBLE | WS_TABSTOP | ES_PASSWORD | ES_AUTOHSCROLL,
                5,
                40,
                250,
                25,
            ),
            ("BUTTON", BUTTON_NAME, WS_VISIBLE | WS_TABSTOP, 5, 80, 250, 30),
            (
                "BUTTON",
                DISABLED_BUTTON_NAME,
                WS_VISIBLE | WS_TABSTOP | WS_DISABLED,
                5,
                120,
                250,
                30,
            ),
        )
        for identifier, (kind, text, style, x, y, width, height) in enumerate(controls, 1):
            handle = user.CreateWindowExW(
                0,
                kind,
                text,
                WS_CHILD | style,
                x,
                y,
                width,
                height,
                window,
                identifier,
                instance,
                None,
            )
            if not handle:
                raise AssertionError("Owned Win32 control creation failed")
            if identifier == BUTTON_ID:
                button_handle = handle
        mark("controls_ready")
        # ShowWindow returns the previous visibility, not an error status.
        user.ShowWindow(window, SW_SHOWNOACTIVATE)
        mark("window_shown")
        if not user.PostMessageW(window, WM_READY, 0, 0):
            raise AssertionError("Owned Win32 readiness message failed")
        message = _Message()
        while True:
            result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == -1:
                raise AssertionError("Owned Win32 message loop failed")
            if result == 0:
                break
            user.TranslateMessage(ctypes.byref(message))
            user.DispatchMessageW(ctypes.byref(message))
        if callback_failed:
            raise AssertionError("Owned Win32 window callback failed") from None
    finally:
        if window and user.IsWindow(window):
            user.DestroyWindow(window)
        user.UnregisterClassW(class_name, instance)


def _mark(stage: str) -> None:
    sys.stderr.write("muli-native-stage=" + stage + "\n")
    sys.stderr.flush()


def _ready(value: dict[str, str | int]) -> None:
    frame = json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n"
    sys.stdout.buffer.write(frame.encode("utf-8"))
    sys.stdout.buffer.flush()


def _invoked(count: int) -> None:
    _ready({"button_invoked": count})


def main() -> None:
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Owned UIA window is restricted to hosted Windows CI")
    if len(sys.argv) != 2 or not _TITLE.fullmatch(sys.argv[1]):
        raise SystemExit("Invalid owned UIA window arguments")
    _mark("python_entered")
    loader = getattr(ctypes, "WinDLL", None)
    callback_factory = getattr(ctypes, "WINFUNCTYPE", None)
    if loader is None or callback_factory is None:
        raise SystemExit("Owned Win32 APIs unavailable")
    user, kernel = loader("user32", use_last_error=True), loader("kernel32", use_last_error=True)
    _configure(user, kernel)
    _run_window(
        user, kernel, callback_factory, sys.argv[1], mark=_mark, ready=_ready, invoked=_invoked
    )


if __name__ == "__main__":
    main()
