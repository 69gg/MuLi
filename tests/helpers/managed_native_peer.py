"""Fixed native-CI user-manager peer observation; not a general D-Bus client.

The caller must bracket this helper with the existing proc and PID1 identity
checks and run it inside the original deadline/cgroup. Importing does no IO.
Library wire/malloc is cgroup-limited, not covered by the pipe-byte allowance.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import re
import socket
import stat
import struct
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

if TYPE_CHECKING or __package__:
    from .managed_native_worker import (
        CLEANUP_SECONDS,
        CONTEXT_REPLY_BYTES,
        SOCKET_SUB_STATES,
        NativeError,
        remaining,
        require,
    )
else:
    # The isolated worker CLI loads this exact sibling without changing sys.path.
    # Reuse its live error/clock helpers rather than importing a second worker.
    from __main__ import (
        CLEANUP_SECONDS,
        CONTEXT_REPLY_BYTES,
        SOCKET_SUB_STATES,
        NativeError,
        remaining,
        require,
    )

SocketState = Literal[
    "dead",
    "start-pre",
    "start-chown",
    "start-post",
    "listening",
    "running",
    "stop-pre",
    "stop-pre-sigterm",
    "stop-pre-sigkill",
    "stop-post",
    "final-sigterm",
    "final-sigkill",
    "failed",
    "cleaning",
]
Reason = Literal["none", "timeout", "identity_changed", "invalid_reply", "unavailable"]
_MANAGER = b"/org/freedesktop/systemd1"
_MANAGER_INTERFACE = b"org.freedesktop.systemd1.Manager"
_UNIT_INTERFACE = b"org.freedesktop.systemd1.Unit"
_SOCKET_INTERFACE = b"org.freedesktop.systemd1.Socket"
_PROPERTIES = b"org.freedesktop.DBus.Properties"


@dataclass(frozen=True, slots=True)
class PeerFacts:
    code: Literal["observed", "not_observed"]
    reason: Reason
    sub_state: SocketState | None = None
    control_pid: int | None = None


class _Error(ctypes.Structure):
    # v255 sd_bus_error: borrowed/owned const char*, const char*, int.
    # Void pointers prevent ctypes from implicitly copying arbitrary C strings.
    _fields_ = [
        ("name", ctypes.c_void_p),
        ("message", ctypes.c_void_p),
        ("need_free", ctypes.c_int),
    ]


class _Function(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *arguments: object) -> object: ...


class _Library:
    """Only explicitly typed non-variadic v255 symbols are bound."""

    def __init__(self, library: ctypes.CDLL) -> None:
        pointer = ctypes.c_void_p
        pointer_out = ctypes.POINTER(pointer)

        def bind(name: str, arguments: list[object], result: object = ctypes.c_int) -> _Function:
            function = cast(_Function, getattr(library, name))
            function.argtypes = arguments
            function.restype = result
            return function

        self.new = bind("sd_bus_new", [pointer_out])
        self.address = bind("sd_bus_set_address", [pointer, ctypes.c_char_p])
        self.client = bind("sd_bus_set_bus_client", [pointer, ctypes.c_int])
        self.start = bind("sd_bus_start", [pointer])
        self.fd = bind("sd_bus_get_fd", [pointer])
        self.close = bind("sd_bus_close", [pointer], None)
        self.unref = bind("sd_bus_unref", [pointer], pointer)
        self.new_method = bind(
            "sd_bus_message_new_method_call",
            [
                pointer,
                pointer_out,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
            ],
        )
        self.auto_start = bind("sd_bus_message_set_auto_start", [pointer, ctypes.c_int])
        self.interactive = bind(
            "sd_bus_message_set_allow_interactive_authorization", [pointer, ctypes.c_int]
        )
        self.append = bind("sd_bus_message_append_basic", [pointer, ctypes.c_char, pointer])
        self.call = bind(
            "sd_bus_call",
            [pointer, pointer, ctypes.c_uint64, ctypes.POINTER(_Error), pointer_out],
        )
        self.signature = bind("sd_bus_message_has_signature", [pointer, ctypes.c_char_p])
        self.read = bind("sd_bus_message_read_basic", [pointer, ctypes.c_char, pointer])
        self.enter = bind(
            "sd_bus_message_enter_container", [pointer, ctypes.c_char, ctypes.c_char_p]
        )
        self.exit = bind("sd_bus_message_exit_container", [pointer])
        self.at_end = bind("sd_bus_message_at_end", [pointer, ctypes.c_int])
        self.message_unref = bind("sd_bus_message_unref", [pointer], pointer)
        self.error_free = bind("sd_bus_error_free", [ctypes.POINTER(_Error)], None)


def _load_library() -> _Library:
    # Fixed Linux SONAME, without find_library/ldconfig or a CLI fallback.
    try:
        return _Library(ctypes.CDLL("libsystemd.so.0"))
    except AttributeError:
        raise OSError("native_peer_unavailable") from None


def _number(result: object) -> int:
    require(type(result) is int, "native_diagnostic_invalid")
    assert isinstance(result, int)
    if result < 0:
        if result in {-errno.ETIMEDOUT, -errno.ETIME}:
            raise NativeError("native_deadline_exceeded")
        raise OSError("native_peer_unavailable")
    return result


def _one(result: object) -> None:
    require(_number(result) == 1, "native_diagnostic_invalid")


def _release(function: _Function, *arguments: object) -> None:
    # Native cleanup returns no useful error. A bounded Python-side failure
    # must not replace the first diagnostic failure or skip later releases.
    try:
        function(*arguments)
    except (OSError, ValueError):
        pass


def _bounded_ascii(address: ctypes.c_void_p, limit: int) -> bytes:
    require(address.value is not None, "native_diagnostic_invalid")
    assert address.value is not None
    value = bytearray()
    # Read only one byte at a time until NUL; never copy max bytes blindly
    # past an allocation, and never use c_char_p.value's unbounded conversion.
    for offset in range(limit + 1):
        byte = ctypes.c_ubyte.from_address(address.value + offset).value
        if byte == 0:
            return bytes(value)
        require(offset < limit and byte < 128, "native_diagnostic_invalid")
        value.append(byte)
    raise NativeError("native_diagnostic_invalid")


def _socket_identity(uid: int) -> tuple[int, int, int]:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in ("run", "user", str(uid), "systemd"):
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        info = os.stat("private", dir_fd=descriptor, follow_symlinks=False)
        require(
            stat.S_ISSOCK(info.st_mode) and info.st_uid == uid,
            "native_process_changed",
        )
        return info.st_dev, info.st_ino, info.st_uid
    finally:
        os.close(descriptor)


def _peer_identity(descriptor: int) -> tuple[int, int]:
    # fromfd duplicates the borrowed sd-bus FD. Only that duplicate is closed.
    with socket.fromfd(descriptor, socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _ = struct.unpack("3i", data)
    return pid, uid


def _request(
    api: _Library,
    bus: ctypes.c_void_p,
    deadline: float,
    path: bytes,
    interface: bytes,
    member: bytes,
    arguments: tuple[bytes, ...],
    value_type: Literal["o", "s", "u"],
) -> bytes | int:
    message, reply = ctypes.c_void_p(), ctypes.c_void_p()
    error = _Error()
    try:
        remaining(deadline)
        _number(api.new_method(bus, ctypes.byref(message), None, path, interface, member))
        require(message.value is not None, "native_diagnostic_invalid")
        _number(api.auto_start(message, 0))
        _number(api.interactive(message, 0))
        for argument in arguments:
            # append_basic('s') takes the char[] address, NOT char**.
            buffer = ctypes.create_string_buffer(argument)
            _number(api.append(message, b"s", ctypes.cast(buffer, ctypes.c_void_p)))
        timeout = max(1, math.floor(remaining(deadline) * 1_000_000))
        _number(api.call(bus, message, timeout, ctypes.byref(error), ctypes.byref(reply)))
        remaining(deadline)
        require(reply.value is not None, "native_diagnostic_invalid")
        variant = value_type != "o"
        _one(api.signature(reply, b"v" if variant else b"o"))
        if variant:
            _one(api.enter(reply, b"v", value_type.encode("ascii")))
        if value_type == "u":
            integer = ctypes.c_uint32()
            _one(api.read(reply, b"u", ctypes.byref(integer)))
            result: bytes | int = integer.value
        else:
            address = ctypes.c_void_p()
            # read_basic('s'/'o') writes a borrowed const char* through char**.
            _one(api.read(reply, value_type.encode("ascii"), ctypes.byref(address)))
            result = _bounded_ascii(address, CONTEXT_REPLY_BYTES)
        _one(api.at_end(reply, 0 if variant else 1))
        if variant:
            _one(api.exit(reply))
            _one(api.at_end(reply, 1))
        remaining(deadline)
        return result
    finally:
        if reply.value is not None:
            _release(api.message_unref, reply)
        if message.value is not None:
            _release(api.message_unref, message)
        _release(api.error_free, ctypes.byref(error))


def read_manager_peer(pid: int, uid: int, deadline: float) -> PeerFacts:
    """Read one already loaded dbus socket's substate/PID; no activation or retries.

    This does not authenticate proc/exe/cgroup or infer READY. The eventual
    caller must supply the outer same-process window and hard process deadline.
    """
    bus = ctypes.c_void_p()
    api: _Library | None = None
    try:
        require(
            sys.platform == "linux"
            and os.geteuid() == 0
            and type(pid) is int
            and 0 < pid < 2**31
            and type(uid) is int
            and 0 < uid < 2**31
            and type(deadline) in (int, float)
            and math.isfinite(deadline),
            "native_diagnostic_invalid",
        )
        require(remaining(deadline) <= CLEANUP_SECONDS, "native_diagnostic_invalid")
        before = _socket_identity(uid)
        api = _load_library()
        _number(api.new(ctypes.byref(bus)))
        require(bus.value is not None, "native_diagnostic_invalid")
        _number(api.address(bus, f"unix:path=/run/user/{uid}/systemd/private".encode("ascii")))
        _number(api.client(bus, 0))
        _number(api.start(bus))
        remaining(deadline)
        descriptor = _number(api.fd(bus))
        require(_peer_identity(descriptor) == (pid, uid), "native_process_changed")
        path = _request(
            api,
            bus,
            deadline,
            _MANAGER,
            _MANAGER_INTERFACE,
            b"GetUnit",
            (b"dbus.socket",),
            "o",
        )
        require(
            isinstance(path, bytes)
            and re.fullmatch(rb"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", path) is not None,
            "native_diagnostic_invalid",
        )
        assert isinstance(path, bytes)
        state = _request(
            api,
            bus,
            deadline,
            path,
            _PROPERTIES,
            b"Get",
            (_UNIT_INTERFACE, b"SubState"),
            "s",
        )
        require(
            isinstance(state, bytes) and state.decode("ascii") in SOCKET_SUB_STATES,
            "native_diagnostic_invalid",
        )
        assert isinstance(state, bytes)
        control_pid = _request(
            api,
            bus,
            deadline,
            path,
            _PROPERTIES,
            b"Get",
            (_SOCKET_INTERFACE, b"ControlPID"),
            "u",
        )
        require(type(control_pid) is int and 0 <= control_pid < 2**31, "native_diagnostic_invalid")
        assert isinstance(control_pid, int)
        require(
            _peer_identity(descriptor) == (pid, uid) and _socket_identity(uid) == before,
            "native_process_changed",
        )
        remaining(deadline)
        return PeerFacts("observed", "none", cast(SocketState, state.decode("ascii")), control_pid)
    except (OSError, ValueError, struct.error, ctypes.ArgumentError) as error:
        reason: Reason = (
            "timeout"
            if isinstance(error, TimeoutError)
            or isinstance(error, NativeError)
            and str(error) == "native_deadline_exceeded"
            else "identity_changed"
            if isinstance(error, NativeError) and str(error) == "native_process_changed"
            else "invalid_reply"
            if isinstance(error, ValueError)
            else "unavailable"
        )
        return PeerFacts("not_observed", reason)
    finally:
        if api is not None and bus.value is not None:
            _release(api.close, bus)
            _release(api.unref, bus)
