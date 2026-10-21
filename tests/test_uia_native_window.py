"""Pure Win32 call doubles: never create a process, DLL, or desktop window."""

from __future__ import annotations

import asyncio
import ctypes
import os
from collections import deque
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from helpers import uia_native_startup
from helpers import uia_native_window as module


class Function:
    def __init__(self, call: Callable[..., Any]) -> None:
        self.call = call
        self.argtypes: list[Any] = []
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        return self.call(*args)


class WindowAPI:
    def __init__(self) -> None:
        self.window = (1 << 40) if ctypes.sizeof(ctypes.c_void_p) == 8 else 512
        self.instance = self.window + 10
        self.created: list[tuple[Any, ...]] = []
        self.events: list[str] = []
        self.messages: deque[int] = deque()
        self.callback: Any = None
        self.quit = False
        self.fail_create: int | None = None
        self.message_error = False
        self.registered = True
        self.default_arguments: tuple[int, ...] | None = None
        self.default_result = self.window + 500
        self.callback_results: list[int] = []
        self.user = SimpleNamespace(
            RegisterClassW=Function(self.register),
            CreateWindowExW=Function(self.create),
            DefWindowProcW=Function(self.default),
            GetMessageW=Function(self.get_message),
            TranslateMessage=Function(lambda message: self.events.append("translate")),
            DispatchMessageW=Function(self.dispatch),
            PostMessageW=Function(self.post),
            PostQuitMessage=Function(self.post_quit),
            ShowWindow=Function(self.show),
            IsWindow=Function(lambda window: window == self.window),
            DestroyWindow=Function(self.destroy),
            UnregisterClassW=Function(self.unregister),
        )
        self.kernel = SimpleNamespace(GetModuleHandleW=Function(lambda name: self.instance))
        module._configure(self.user, self.kernel)

    def register(self, pointer: Any) -> int:
        description = ctypes.cast(pointer, ctypes.POINTER(module._WindowClass)).contents
        assert description.hInstance == self.instance
        assert description.cbClsExtra == description.cbWndExtra == 0
        callback_type = ctypes.CFUNCTYPE(
            module._LRESULT, module._HANDLE, module._U32, module._WPARAM, module._LPARAM
        )
        self.callback = callback_type(description.lpfnWndProc)
        self.events.append("register")
        return int(self.registered)

    def create(self, *args: Any) -> int:
        self.created.append(args)
        if len(self.created) == self.fail_create:
            return 0
        return self.window if len(self.created) == 1 else self.window + len(self.created)

    def show(self, window: int, state: int) -> int:
        assert window == self.window and state == module.SW_SHOWNOACTIVATE
        self.events.append("show")
        return 0  # Previously invisible is a successful ShowWindow result.

    def post(self, window: int, message: int, wparam: int, lparam: int) -> int:
        assert window == self.window and (wparam, lparam) == (0, 0)
        self.messages.append(message)
        return 1

    def get_message(self, pointer: Any, window: Any, low: int, high: int) -> int:
        assert window is None and low == high == 0
        if self.message_error:
            return -1
        if self.quit or not self.messages:
            return 0
        message = ctypes.cast(pointer, ctypes.POINTER(module._Message)).contents
        message.hwnd, message.message = self.window, self.messages.popleft()
        message.wParam, message.lParam = self.window + 20, -(self.window + 30)
        return 1

    def dispatch(self, pointer: Any) -> int:
        self.events.append("dispatch")
        message = ctypes.cast(pointer, ctypes.POINTER(module._Message)).contents
        result = int(self.callback(message.hwnd, message.message, message.wParam, message.lParam))
        self.callback_results.append(result)
        return result

    def default(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        self.default_arguments = hwnd, message, wparam, lparam
        return self.default_result

    def post_quit(self, status: int) -> None:
        self.events.append("quit:" + str(status))
        self.quit = True

    def destroy(self, window: int) -> int:
        assert window == self.window
        self.events.append("destroy")
        assert self.callback(window, module.WM_DESTROY, 0, 0) == 0
        return 1

    def unregister(self, name: str, instance: int) -> int:
        assert name == "MuLiNativeWindow_" + str(os.getpid()) and instance == self.instance
        self.events.append("unregister")
        return 1


@pytest.fixture(autouse=True)
def no_real_windows_or_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Pure fixture tests must not load Windows DLLs or create processes")

    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


def run(
    api: WindowAPI,
    *,
    ready: Callable[[dict[str, str | int]], None] | None = None,
) -> tuple[list[str], list[dict[str, str | int]]]:
    stages: list[str] = []
    receipts: list[dict[str, str | int]] = []

    def publish(value: dict[str, str | int]) -> None:
        assert api.events[-1] == "dispatch" and api.events.index("show") < len(api.events) - 1
        receipts.append(value)

    module._run_window(
        api.user,
        api.kernel,
        ctypes.CFUNCTYPE,
        "muli-uia-" + "a" * 32,
        mark=stages.append,
        ready=publish if ready is None else ready,
    )
    return stages, receipts


def test_real_message_dispatch_precedes_ready_and_only_owned_controls_are_created() -> None:
    api = WindowAPI()
    stages, receipts = run(api)
    assert receipts == [{"pid": os.getpid(), "title": "muli-uia-" + "a" * 32}]
    assert stages == [
        "class_registered",
        "window_created",
        "controls_ready",
        "window_shown",
        "message_loop_ready",
    ]
    top, label, password_label, password, button = api.created
    assert top[0] == module.WS_EX_TOOLWINDOW | module.WS_EX_NOACTIVATE
    assert top[4:6] == (-20000, -20000) and top[8] is None
    assert label[1:3] == ("STATIC", '中文🌊 "quote" \\ slash')
    assert password_label[1:3] == ("STATIC", "private-password-label")
    assert not password_label[3] & module.WS_VISIBLE
    assert password[1:3] == ("EDIT", "private-password-value")
    assert password[3] & module.ES_PASSWORD and not password[3] & 0x0004  # No ES_MULTILINE.
    assert button[1:3] == ("BUTTON", "Never invoke this button")
    assert all(child[8] == api.window for child in (label, password_label, password, button))
    assert api.events[-3:] == ["destroy", "quit:0", "unregister"]


def test_message_callback_preserves_pointer_sized_handles_arguments_and_result() -> None:
    api = WindowAPI()
    api.messages.append(0x1234)
    run(api)
    assert api.default_arguments == (api.window, 0x1234, api.window + 20, -(api.window + 30))
    assert api.callback_results[0] == api.default_result
    assert api.user.DefWindowProcW.restype is ctypes.c_ssize_t
    assert api.user.GetMessageW.restype is ctypes.c_int32
    assert api.user.CreateWindowExW.restype is ctypes.c_void_p
    pointer = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(module._WindowClass) == (72 if pointer == 8 else 40)
    assert ctypes.sizeof(module._Message) == (48 if pointer == 8 else 32)


def test_message_error_is_not_treated_as_a_successful_message() -> None:
    api = WindowAPI()
    api.message_error = True
    with pytest.raises(AssertionError, match="message loop failed"):
        run(api)
    assert "dispatch" not in api.events and api.events[-1] == "unregister"
    assert "destroy" in api.events


@pytest.mark.parametrize("failed_creation", [1, 4], ids=["top", "password"])
def test_partial_creation_never_publishes_and_releases_the_registered_class(
    failed_creation: int,
) -> None:
    api = WindowAPI()
    api.fail_create = failed_creation
    with pytest.raises(AssertionError, match="creation failed"):
        run(api)
    assert "show" not in api.events and "dispatch" not in api.events
    assert ("destroy" in api.events) == (failed_creation != 1)
    assert api.events[-1] == "unregister"


def test_registration_failure_does_not_destroy_or_unregister_an_unowned_window() -> None:
    api = WindowAPI()
    api.registered = False
    with pytest.raises(AssertionError, match="registration failed"):
        run(api)
    assert not api.created and api.events == ["register"]


def test_callback_exception_becomes_fixed_failure_and_still_closes_owned_window() -> None:
    api = WindowAPI()

    def broken(value: dict[str, str | int]) -> None:
        raise RuntimeError("private-callback-error")

    with pytest.raises(AssertionError, match="window callback failed") as caught:
        run(api, ready=broken)
    assert "private-callback-error" not in str(caught.value)
    assert "quit:1" in api.events and api.events[-1] == "unregister"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_target_arguments_use_the_isolated_interpreter_and_fixed_helper(
    platform: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    title = "muli-uia-" + "a" * 32
    executable = str(tmp_path / "venv-python.exe")
    base_executable = str(tmp_path / "base-python.exe")
    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(platform=platform, executable=executable, _base_executable=base_executable),
    )
    arguments = module.window_arguments(title)
    expected = base_executable if platform == "win32" else executable
    assert arguments == (expected, "-I", "-B", str(Path(module.__file__).absolute()), title)
    with pytest.raises(ValueError, match="Invalid owned"):
        module.window_arguments("another user's window")


@pytest.mark.parametrize("base_executable", [None, "", "relative-python.exe"])
def test_windows_target_requires_an_absolute_base_interpreter_without_launcher_fallback(
    base_executable: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(
            platform="win32",
            executable=str(tmp_path / "venv-python.exe"),
            _base_executable=base_executable,
        ),
    )
    with pytest.raises(ValueError, match="base interpreter unavailable"):
        module.window_arguments("muli-uia-" + "a" * 32)


async def test_dispatched_target_receipt_matches_the_owned_process_without_a_venv_redirector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    title = "muli-uia-" + "a" * 32
    executable, base_executable = str(tmp_path / "venv-python.exe"), str(tmp_path / "python.exe")
    output = BytesIO()
    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(
            platform="win32",
            executable=executable,
            _base_executable=base_executable,
            stdout=SimpleNamespace(buffer=output),
        ),
    )
    arguments = module.window_arguments(title)
    # CPython's Windows venv redirector and the actual interpreter have distinct
    # PIDs. Model that launch boundary, then exercise the real producer + reader.
    target_pid = os.getpid()
    process = SimpleNamespace(
        pid=target_pid if arguments[0] == base_executable else target_pid + 1,
        stdout=asyncio.StreamReader(limit=uia_native_startup.OUTPUT_BYTES),
        stderr=asyncio.StreamReader(limit=uia_native_startup.OUTPUT_BYTES),
        returncode=None,
    )
    api = WindowAPI()
    run(api, ready=module._ready)
    process.stdout.feed_data(output.getvalue())
    owned_process = cast(asyncio.subprocess.Process, process)

    async def create() -> asyncio.subprocess.Process:
        return owned_process

    async def settle(
        owned: asyncio.subprocess.Process, tasks: Sequence[asyncio.Task[None]]
    ) -> None:
        assert owned is owned_process
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        process.returncode = -1

    monkeypatch.setattr(uia_native_startup, "_settle_process", settle)
    async with uia_native_startup.observe_native_form(
        asyncio.create_task(create()), title, 12
    ) as owned:
        assert owned is owned_process and owned.pid == target_pid
        assert process.returncode is None and api.events[-1] == "unregister"
    assert process.returncode == -1


@pytest.mark.parametrize(
    ("platform", "hosted", "title"),
    [("linux", "true", "a" * 32), ("win32", "false", "a" * 32), ("win32", "true", "invalid")],
    ids=["non-windows", "not-hosted", "invalid-owned-title"],
)
def test_main_rejects_outside_the_owned_hosted_fixture_before_loading_dlls(
    platform: str,
    hosted: str,
    title: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "sys", SimpleNamespace(platform=platform, argv=["helper.py", title])
    )
    monkeypatch.setattr(module, "os", SimpleNamespace(environ={"GITHUB_ACTIONS": hosted}))
    with pytest.raises(SystemExit):
        module.main()
