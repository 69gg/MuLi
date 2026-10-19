"""One bounded Portal request, using the already installed system Python/Gio."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit
from uuid import uuid4

SERVICE = "org.freedesktop.portal.Desktop"
DESKTOP = "/org/freedesktop/portal/desktop"
SCREENSHOT = "org.freedesktop.portal.Screenshot"
REQUEST = "org.freedesktop.portal.Request"
REQUEST_ROOT = DESKTOP + "/request/"
PNG = b"\x89PNG\r\n\x1a\n"


def request_path(sender: str, token: str) -> str:
    if not re.fullmatch(r":[0-9]+(?:\.[0-9]+)+", sender) or not re.fullmatch(
        r"[A-Za-z0-9_]+", token
    ):
        raise ValueError("Portal 请求标识无效")
    return REQUEST_ROOT + sender[1:].replace(".", "_") + "/" + token


def local_path(uri: str) -> Path:
    if (
        not isinstance(uri, str)
        or len(uri) > 8192
        or any(ord(char) < 32 or ord(char) == 127 for char in uri)
        or re.search(r"%(?![0-9a-fA-F]{2})", uri)
    ):
        raise ValueError("Portal URI 无效")
    value = urlsplit(uri)
    if (
        value.scheme != "file"
        or value.netloc.lower() not in {"", "localhost"}
        or "?" in uri
        or "#" in uri
    ):
        raise ValueError("Portal 只接受本地 file URI")
    path = os.fsdecode(unquote_to_bytes(value.path))
    if (
        not path.startswith("/")
        or path.startswith("//")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ValueError("Portal 文件路径无效")
    return Path(path)


def open_regular(path: Path, flags: int) -> int:
    if not path.is_absolute() or len(path.parts) < 2:
        raise ValueError("Portal 文件路径必须是绝对路径")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name,
            flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("Portal 结果必须是普通文件")
    return descriptor


def copy_png(uri: str, output: Path, budget: int) -> None:
    if type(budget) is not int or budget < len(PNG):
        raise ValueError("本地采集目录已达到容量限制")
    with os.fdopen(open_regular(local_path(uri), os.O_RDONLY), "rb", buffering=0) as source:
        size = os.fstat(source.fileno()).st_size
        if not 0 < size <= budget:
            raise ValueError("Portal 文件为空或超过容量限制")
        header = source.read(len(PNG))
        if header != PNG:
            raise ValueError("Portal 结果不是 PNG 文件")
        with os.fdopen(open_regular(output, os.O_WRONLY), "wb") as destination:
            details = os.fstat(destination.fileno())
            if details.st_uid != os.getuid() or details.st_nlink != 1 or details.st_mode & 0o077:
                raise ValueError("Portal 输出必须是当前用户的私人文件")
            os.ftruncate(destination.fileno(), 0)
            destination.write(header)
            count = len(header)
            while chunk := source.read(min(65536, budget - count + 1)):
                count += len(chunk)
                if count > budget:
                    raise ValueError("Portal 文件超过容量限制")
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())


def properties(
    connection: Any, Gio: Any, GLib: Any, *, activate: bool
) -> tuple[str, dict[str, Any]]:
    def bus_call(method: str, signature: str, values: tuple[Any, ...], reply: str) -> Any:
        return connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            method,
            GLib.Variant(signature, values),
            GLib.VariantType.new(reply),
            Gio.DBusCallFlags.NO_AUTO_START,
            1500,
            None,
        ).unpack()[0]

    try:
        owner = str(bus_call("GetNameOwner", "(s)", (SERVICE,), "(s)"))
    except GLib.Error as exc:
        if Gio.dbus_error_get_remote_error(exc) != "org.freedesktop.DBus.Error.NameHasNoOwner":
            raise
        if not activate:
            return "", {"state": "unknown", "available": None, "reason": "Portal 服务尚未运行"}
        bus_call("StartServiceByName", "(su)", (SERVICE, 0), "(u)")
        owner = str(bus_call("GetNameOwner", "(s)", (SERVICE,), "(s)"))
    values = connection.call_sync(
        owner,
        DESKTOP,
        "org.freedesktop.DBus.Properties",
        "GetAll",
        GLib.Variant("(s)", (SCREENSHOT,)),
        GLib.VariantType.new("(a{sv})"),
        Gio.DBusCallFlags.NO_AUTO_START,
        1500,
        None,
    ).unpack()[0]
    version = values.get("version")
    if type(version) is not int or version < 1:
        raise ValueError("Portal Screenshot 接口版本无效或不可用")
    targets = values.get("AvailableTargets") if version >= 3 else None
    if version >= 3 and type(targets) is not int:
        raise ValueError("Portal Screenshot 目标属性无效")
    return owner, {
        "state": "ready",
        "available": True,
        "version": version,
        "available_targets": targets,
        "reason": "",
    }


class PortalRequest:
    def __init__(
        self, connection: Any, owner: str, version: int, timeout: float, Gio: Any, GLib: Any
    ) -> None:
        self.connection, self.owner, self.Gio, self.GLib = connection, owner, Gio, GLib
        self.version, self.timeout = version, timeout
        self.loop = GLib.MainLoop()
        self.predicted = request_path(connection.get_unique_name(), "muli_" + uuid4().hex)
        self.handle = ""
        self.pending = True
        self.done = False
        self.cancel_reason = ""
        self.error = ""
        self.uri = ""
        self.responses: dict[str, Any] = {}
        self.closing: set[str] = set()
        self.closed: set[str] = set()
        self.subscriptions: list[int] = []
        self.sources: list[int] = []
        self.temporary = 0

    def finish(self, error: str = "", uri: str = "") -> None:
        if not self.done:
            self.done, self.error, self.uri = True, error, uri
            self.loop.quit()

    def close_request(self, path: str) -> None:
        if path in self.closed:
            if path == self.handle and not self.pending:
                self.finish(self.cancel_reason)
            return
        if path in self.closing:
            return
        self.closing.add(path)
        pending = self.pending

        def closed(connection: Any, result: Any, data: Any) -> None:
            self.closing.discard(path)
            try:
                connection.call_finish(result)
                self.closed.add(path)
                if path == self.handle and not self.pending:
                    self.finish(self.cancel_reason)
            except self.GLib.Error:
                # A predicted object can appear after its first Close failed. Retry only
                # when the original method has since confirmed this same real handle.
                if pending and not self.pending and path == self.handle:
                    self.close_request(path)

        self.connection.call(
            self.owner,
            path,
            REQUEST,
            "Close",
            None,
            self.GLib.VariantType.new("()"),
            self.Gio.DBusCallFlags.NO_AUTO_START,
            700,
            None,
            closed,
            None,
        )

    def cancel(self, reason: str) -> bool:
        if self.done or self.cancel_reason:
            return True
        self.cancel_reason = reason
        self.close_request(self.handle or self.predicted)

        def expired() -> bool:
            self.finish(self.cancel_reason + "；Portal 关闭未确认")
            return True

        self.sources.append(self.GLib.timeout_add(1000, expired))
        return True

    def response(
        self,
        connection: Any,
        sender: str,
        path: str,
        interface: str,
        member: str,
        parameters: Any,
        temporary: bool,
    ) -> None:
        if self.done or self.cancel_reason or sender != self.owner:
            return
        if not path.startswith(REQUEST_ROOT) or (temporary and path == self.predicted):
            return
        if parameters.get_type_string() != "(ua{sv})" or parameters.get_size() > 16384:
            self.cancel("Portal 响应格式无效")
            return
        if not self.handle:
            if len(self.responses) >= 8 and path not in self.responses:
                self.cancel("Portal 响应缓冲超过限制")
            else:
                self.responses.setdefault(path, parameters)
        elif path == self.handle:
            self.accept(parameters)

    def accept(self, parameters: Any) -> None:
        code, values = parameters.unpack()
        if code == 1:
            self.finish("用户取消了 Portal 截图")
        elif code != 0:
            self.finish("Portal 截图未完成")
        else:
            uri = values.get("uri")
            if not isinstance(uri, str) or len(uri) > 8192:
                self.finish("Portal 未返回有效文件 URI")
            else:
                self.finish(uri=uri)

    def returned(self, connection: Any, result: Any, data: Any) -> None:
        if self.done:
            return
        self.pending = False
        try:
            handle = connection.call_finish(result).unpack()[0]
            if not handle.startswith(REQUEST_ROOT):
                raise ValueError("Portal 返回的请求路径无效")
            self.handle = handle
            if self.cancel_reason:
                self.close_request(handle)
                return
            if handle != self.predicted:
                self.subscribe(handle, False)
            connection.signal_unsubscribe(self.temporary)
            self.subscriptions.remove(self.temporary)
            self.temporary = 0
            response = self.responses.get(handle)
            self.responses.clear()
            if response is not None:
                self.accept(response)
        except (self.GLib.Error, ValueError) as exc:
            self.cancel("Portal 请求失败：" + str(exc)[:300])

    def subscribe(self, path: str | None, temporary: bool) -> int:
        identifier = self.connection.signal_subscribe(
            self.owner,
            REQUEST,
            "Response",
            path,
            None,
            self.Gio.DBusSignalFlags.NONE,
            self.response,
            temporary,
        )
        self.subscriptions.append(identifier)
        return int(identifier)

    def run(self) -> str:
        self.subscribe(self.predicted, False)
        self.temporary = self.subscribe(None, True)

        def changed(
            connection: Any,
            sender: str,
            path: str,
            interface: str,
            member: str,
            parameters: Any,
            data: Any,
        ) -> None:
            name, old, new = parameters.unpack()
            if name == SERVICE and new != self.owner:
                self.cancel("Portal 服务已更换或退出")

        self.subscriptions.append(
            self.connection.signal_subscribe(
                "org.freedesktop.DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                "/org/freedesktop/DBus",
                SERVICE,
                self.Gio.DBusSignalFlags.NONE,
                changed,
                None,
            )
        )
        current = self.connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetNameOwner",
            self.GLib.Variant("(s)", (SERVICE,)),
            self.GLib.VariantType.new("(s)"),
            self.Gio.DBusCallFlags.NO_AUTO_START,
            1000,
            None,
        ).unpack()[0]
        if current != self.owner:
            for identifier in self.subscriptions:
                self.connection.signal_unsubscribe(identifier)
            raise ValueError("Portal 服务已更换或退出")
        disconnected = self.connection.connect(
            "closed", lambda *args: self.finish("Portal 总线连接已断开")
        )
        self.sources.append(
            self.GLib.unix_signal_add(
                self.GLib.PRIORITY_DEFAULT,
                signal.SIGTERM,
                lambda: self.cancel("Portal 截图已停止"),
            )
        )
        self.sources.append(
            self.GLib.timeout_add(
                max(1, int(self.timeout * 1000)), lambda: self.cancel("Portal 截图等待超时")
            )
        )
        options = {
            "handle_token": self.GLib.Variant("s", self.predicted.rsplit("/", 1)[1]),
            "modal": self.GLib.Variant("b", True),
        }
        if self.version >= 2:
            options["interactive"] = self.GLib.Variant("b", True)
        self.connection.call(
            self.owner,
            DESKTOP,
            SCREENSHOT,
            "Screenshot",
            self.GLib.Variant("(sa{sv})", ("", options)),
            self.GLib.VariantType.new("(o)"),
            self.Gio.DBusCallFlags.NO_AUTO_START,
            min(5000, max(1, int(self.timeout * 1000))),
            None,
            self.returned,
            None,
        )
        try:
            self.loop.run()
        finally:
            for identifier in self.subscriptions:
                self.connection.signal_unsubscribe(identifier)
            for identifier in self.sources:
                self.GLib.source_remove(identifier)
            self.connection.disconnect(disconnected)
        if self.error:
            raise ValueError(self.error)
        return self.uri


def execute(request: dict[str, Any]) -> dict[str, Any]:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    operation = request.get("operation")
    if operation not in {"probe", "screenshot"}:
        raise ValueError("未知 Portal 操作")
    timeout = float(request.get("timeout", 60))
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("Portal 等待期限无效")
    deadline = time.monotonic() + timeout
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or Gio.dbus_address_get_for_bus_sync(
        Gio.BusType.SESSION, None
    )
    connection = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )
    connection.set_exit_on_close(False)
    try:
        owner, status = properties(connection, Gio, GLib, activate=operation == "screenshot")
        if operation == "screenshot":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Portal 连接或能力探测超时")
            uri = PortalRequest(connection, owner, status["version"], remaining, Gio, GLib).run()
            copy_png(uri, Path(request["output"]), request["max_bytes"])
        return status
    finally:
        if not connection.is_closed():
            connection.close_sync(None)


def main() -> None:
    try:
        raw = sys.stdin.read(16385)
        if len(raw) > 16384:
            raise ValueError("Portal 请求超过限制")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Portal 请求必须是对象")
        print(json.dumps(execute(request), ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:500]}, ensure_ascii=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
